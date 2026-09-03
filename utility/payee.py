"""Beneficiary / payee allowlist for outbound money movement.

Customers may only send funds (or issue a cashier cheque) to a destination that
is either one of their own accounts or a registered payee. Registration is a
separate capability so other features (step-up, velocity, dual-control) can
share the same destination policy without each growing a private nicknames list.

Stores:
- MemoryPayeeStore — tests
- SqlitePayeeStore — restart-safe default (SystemLogs/payees.sqlite)

Not a MySQL table: create_database.py is destructive, and this stays merge-safe
against unmerged SQL-layer PRs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

EMPLOYEE_TYPES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
GATED_OPS = frozenset({'transfer', 'cheque'})
STATUSES = frozenset({'active', 'cooling', 'removed'})
DEFAULT_DB_PATH = os.path.join('SystemLogs', 'payees.sqlite')
NICKNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 '\-]{0,39}$")

_service: Optional['PayeeService'] = None
_owned_resolver: Optional[Callable[[str], Iterable]] = None
_service_lock = threading.Lock()


class PayeeError(ValueError):
    """Rejected payee input or registry mutation (not an allowlist miss)."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def role_from_session(session) -> str:
    if (session or {}).get('usertype') in EMPLOYEE_TYPES:
        return 'employee'
    return 'customer'


def _env_int(name: str, default: str) -> int:
    raw = os.getenv(name, default)
    if raw in ('', 'none', 'None'):
        return 0
    return int(raw)


def _env_bool(name: str, default: str = 'true') -> bool:
    return os.getenv(name, default).strip().lower() not in {'0', 'false', 'no', 'off'}


def normalize_account(value) -> str:
    """Canonical account id: positive integer digits, matching existing int() paths."""
    if value is None or isinstance(value, bool):
        raise PayeeError('invalid_account', 'Enter a valid account number')
    if isinstance(value, int):
        if value <= 0:
            raise PayeeError('invalid_account', 'Enter a valid account number')
        text = str(value)
    elif isinstance(value, float):
        if value <= 0 or not value.is_integer():
            raise PayeeError('invalid_account', 'Enter a valid account number')
        text = str(int(value))
    elif isinstance(value, str):
        text = value.strip().replace(' ', '').replace('-', '').replace(',', '')
        if not text or not text.isdigit() or int(text) <= 0:
            raise PayeeError('invalid_account', 'Enter a valid account number')
        text = str(int(text))
    else:
        raise PayeeError('invalid_account', 'Enter a valid account number')
    if len(text) > 16:
        raise PayeeError('invalid_account', 'Enter a valid account number')
    return text


def normalize_nickname(value) -> str:
    if value is None or not isinstance(value, str):
        raise PayeeError('invalid_nickname', 'Enter a payee nickname')
    text = ' '.join(value.strip().split())
    if not text or not NICKNAME_RE.match(text):
        raise PayeeError(
            'invalid_nickname',
            'Nickname must be 1-40 letters, numbers, spaces, hyphens, or apostrophes.',
        )
    return text


def nickname_key(nickname: str) -> str:
    return normalize_nickname(nickname).casefold()


def new_payee_id() -> str:
    return uuid.uuid4().hex[:16]


def owned_account_numbers(accounts) -> Set[str]:
    """Pull canonical account numbers out of `/loadCustomer` Accounts payloads."""
    found: Set[str] = set()
    if not isinstance(accounts, dict):
        return found
    for info in accounts.values():
        if not isinstance(info, dict) or 'Account' not in info:
            continue
        try:
            found.add(normalize_account(info['Account']))
        except PayeeError:
            continue
    return found


def set_owned_resolver(fn: Optional[Callable[[str], Iterable]]) -> None:
    global _owned_resolver
    _owned_resolver = fn


def resolve_owned_accounts(userid: Optional[str], fallback: Optional[Iterable] = None) -> Set[str]:
    if fallback is not None:
        canonical: Set[str] = set()
        for item in fallback:
            try:
                canonical.add(normalize_account(item))
            except PayeeError:
                continue
        return canonical
    if not userid or _owned_resolver is None:
        return set()
    try:
        return resolve_owned_accounts(userid, fallback=_owned_resolver(userid))
    except Exception:
        return set()


@dataclass(frozen=True)
class PayeePolicy:
    enabled: bool = True
    cooling_seconds: int = 0
    max_payees: int = 20
    customer_only: bool = True
    allow_own_accounts: bool = True
    operations: Tuple[str, ...] = ('transfer', 'cheque')

    @classmethod
    def from_env(cls) -> 'PayeePolicy':
        ops_raw = os.getenv('PAYEE_OPERATIONS', 'transfer,cheque')
        operations = tuple(
            part.strip() for part in ops_raw.split(',') if part.strip() in GATED_OPS
        ) or ('transfer', 'cheque')
        return cls(
            enabled=_env_bool('PAYEE_ENABLED', 'true'),
            cooling_seconds=max(0, _env_int('PAYEE_COOLING_SECONDS', '0')),
            max_payees=max(1, _env_int('PAYEE_MAX', '20')),
            customer_only=_env_bool('PAYEE_CUSTOMER_ONLY', 'true'),
            allow_own_accounts=_env_bool('PAYEE_ALLOW_OWN_ACCOUNTS', 'true'),
            operations=operations,
        )


@dataclass
class Payee:
    payee_id: str
    owner_id: str
    account: str
    nickname: str
    created_at: float
    ready_at: float
    status: str = 'active'

    def live_status(self, now: Optional[float] = None) -> str:
        if self.status == 'removed':
            return 'removed'
        when = time.time() if now is None else now
        if when < self.ready_at:
            return 'cooling'
        return 'active'

    def retry_after(self, now: Optional[float] = None) -> Optional[int]:
        if self.live_status(now) != 'cooling':
            return None
        when = time.time() if now is None else now
        return max(1, int(self.ready_at - when))

    def to_dict(self, now: Optional[float] = None) -> Dict[str, Any]:
        status = self.live_status(now)
        return {
            'payee_id': self.payee_id,
            'owner_id': self.owner_id,
            'account': self.account,
            'nickname': self.nickname,
            'status': status,
            'created_at': self.created_at,
            'ready_at': self.ready_at,
            'retry_after': self.retry_after(now) if status == 'cooling' else None,
        }


@dataclass(frozen=True)
class PayeeDecision:
    allowed: bool
    code: str
    message: str
    operation: str = ''
    payee: Optional[Payee] = None
    retry_after: Optional[int] = None
    payees: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict:
        payload = {
            'error': None if self.allowed else self.code,
            'code': self.code,
            'message': self.message,
            'operation': self.operation,
            'retry_after': self.retry_after,
        }
        if self.payee is not None:
            payload['payee'] = self.payee.to_dict()
        if self.payees:
            payload['payees'] = self.payees
        return payload


class MemoryPayeeStore:
    def __init__(self):
        self._rows: Dict[str, Payee] = {}
        self._lock = threading.Lock()

    def put(self, payee: Payee) -> None:
        with self._lock:
            self._rows[payee.payee_id] = payee

    def get(self, payee_id: str) -> Optional[Payee]:
        with self._lock:
            return self._rows.get(payee_id)

    def list_for_owner(self, owner_id: str, include_removed: bool = False) -> List[Payee]:
        with self._lock:
            rows = [p for p in self._rows.values() if p.owner_id == owner_id]
        if not include_removed:
            rows = [p for p in rows if p.status != 'removed']
        rows.sort(key=lambda p: (p.nickname.casefold(), p.created_at))
        return rows

    def find_by_account(self, owner_id: str, account: str) -> Optional[Payee]:
        for payee in self.list_for_owner(owner_id):
            if payee.account == account:
                return payee
        return None

    def find_by_nickname(self, owner_id: str, key: str) -> Optional[Payee]:
        for payee in self.list_for_owner(owner_id):
            if nickname_key(payee.nickname) == key:
                return payee
        return None


class SqlitePayeeStore:
    """Dedicated sqlite registry. Not PR #12 DurableStore / PR #24 audit db."""

    def __init__(self, path: str = DEFAULT_DB_PATH):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payees (
                payee_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                account TEXT NOT NULL,
                nickname TEXT NOT NULL,
                nickname_key TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                ready_at REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_payee_owner ON payees(owner_id, status)'
        )
        self._conn.commit()

    def put(self, payee: Payee) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO payees(
                    payee_id, owner_id, account, nickname, nickname_key,
                    status, created_at, ready_at
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(payee_id) DO UPDATE SET
                    account=excluded.account,
                    nickname=excluded.nickname,
                    nickname_key=excluded.nickname_key,
                    status=excluded.status,
                    ready_at=excluded.ready_at
                """,
                (
                    payee.payee_id,
                    payee.owner_id,
                    payee.account,
                    payee.nickname,
                    nickname_key(payee.nickname),
                    payee.status,
                    payee.created_at,
                    payee.ready_at,
                ),
            )
            self._conn.commit()

    def get(self, payee_id: str) -> Optional[Payee]:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM payees WHERE payee_id = ?', (payee_id,)
            ).fetchone()
        return _row_to_payee(row) if row else None

    def list_for_owner(self, owner_id: str, include_removed: bool = False) -> List[Payee]:
        sql = 'SELECT * FROM payees WHERE owner_id = ?'
        params: Tuple = (owner_id,)
        if not include_removed:
            sql += " AND status != 'removed'"
        sql += ' ORDER BY nickname COLLATE NOCASE, created_at'
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_payee(row) for row in rows]

    def find_by_account(self, owner_id: str, account: str) -> Optional[Payee]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM payees WHERE owner_id = ? AND account = ? AND status != 'removed'",
                (owner_id, account),
            ).fetchone()
        return _row_to_payee(row) if row else None

    def find_by_nickname(self, owner_id: str, key: str) -> Optional[Payee]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM payees WHERE owner_id = ? AND nickname_key = ? AND status != 'removed'",
                (owner_id, key),
            ).fetchone()
        return _row_to_payee(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_payee(row) -> Payee:
    return Payee(
        payee_id=row['payee_id'],
        owner_id=row['owner_id'],
        account=row['account'],
        nickname=row['nickname'],
        created_at=float(row['created_at']),
        ready_at=float(row['ready_at']),
        status=row['status'],
    )


class PayeeService:
    def __init__(
        self,
        store=None,
        policy: Optional[PayeePolicy] = None,
        now: Optional[Callable[[], float]] = None,
    ):
        self.store = store if store is not None else MemoryPayeeStore()
        self.policy = policy if policy is not None else PayeePolicy()
        self._now = now or time.time
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> 'PayeeService':
        path = os.getenv('PAYEE_STORE', DEFAULT_DB_PATH)
        if path in {':memory:', 'memory'}:
            store = MemoryPayeeStore()
        else:
            store = SqlitePayeeStore(path)
        return cls(store=store, policy=PayeePolicy.from_env())

    def snapshot(self, owner_id: str) -> dict:
        now = self._now()
        payees = [p.to_dict(now) for p in self.store.list_for_owner(owner_id)]
        return {
            'enabled': self.policy.enabled,
            'cooling_seconds': self.policy.cooling_seconds,
            'max_payees': self.policy.max_payees,
            'allow_own_accounts': self.policy.allow_own_accounts,
            'operations': list(self.policy.operations),
            'payees': payees,
            'count': len(payees),
        }

    def add(self, owner_id: str, account, nickname: str) -> PayeeDecision:
        if not owner_id:
            raise PayeeError('invalid_owner', 'Missing owner')
        acct = normalize_account(account)
        nick = normalize_nickname(nickname)
        with self._lock:
            live = self.store.list_for_owner(owner_id)
            if len(live) >= self.policy.max_payees:
                return PayeeDecision(
                    False,
                    'payee_limit',
                    f'Payee limit ({self.policy.max_payees}) reached. Remove one before adding another.',
                    payees=[p.to_dict(self._now()) for p in live],
                )
            if self.store.find_by_account(owner_id, acct):
                return PayeeDecision(
                    False,
                    'payee_duplicate_account',
                    'That account is already a registered payee.',
                    payees=[p.to_dict(self._now()) for p in live],
                )
            if self.store.find_by_nickname(owner_id, nickname_key(nick)):
                return PayeeDecision(
                    False,
                    'payee_duplicate_nickname',
                    'That nickname is already in use.',
                    payees=[p.to_dict(self._now()) for p in live],
                )
            now = self._now()
            ready = now + self.policy.cooling_seconds
            payee = Payee(
                payee_id=new_payee_id(),
                owner_id=str(owner_id),
                account=acct,
                nickname=nick,
                created_at=now,
                ready_at=ready,
                status='active',
            )
            self.store.put(payee)
            live = self.store.list_for_owner(owner_id)
            status = payee.live_status(now)
            message = (
                f'Payee added. Cooling period {self.policy.cooling_seconds}s before it can receive funds.'
                if status == 'cooling'
                else 'Payee added.'
            )
            return PayeeDecision(
                True,
                'ok',
                message,
                payee=payee,
                retry_after=payee.retry_after(now),
                payees=[p.to_dict(now) for p in live],
            )

    def remove(self, owner_id: str, payee_id: str) -> PayeeDecision:
        if not owner_id or not payee_id:
            raise PayeeError('invalid_payee', 'Missing payee')
        with self._lock:
            payee = self.store.get(str(payee_id))
            if payee is None or payee.owner_id != str(owner_id) or payee.status == 'removed':
                return PayeeDecision(False, 'payee_not_found', 'Payee not found.')
            removed = Payee(
                payee_id=payee.payee_id,
                owner_id=payee.owner_id,
                account=payee.account,
                nickname=payee.nickname,
                created_at=payee.created_at,
                ready_at=payee.ready_at,
                status='removed',
            )
            self.store.put(removed)
            live = self.store.list_for_owner(owner_id)
            return PayeeDecision(
                True,
                'ok',
                'Payee removed.',
                payee=removed,
                payees=[p.to_dict(self._now()) for p in live],
            )

    def authorize(
        self,
        owner_id: str,
        destination,
        role: str = 'customer',
        owned_accounts: Optional[Iterable] = None,
        operation: str = 'transfer',
    ) -> PayeeDecision:
        if not self.policy.enabled or operation not in self.policy.operations:
            return PayeeDecision(True, 'ok', 'ok', operation)
        if self.policy.customer_only and role == 'employee':
            return PayeeDecision(True, 'ok', 'ok', operation)
        try:
            dest = normalize_account(destination)
        except PayeeError as exc:
            return PayeeDecision(False, exc.code, exc.message, operation)
        owned = resolve_owned_accounts(owner_id, fallback=owned_accounts)
        if self.policy.allow_own_accounts and dest in owned:
            return PayeeDecision(True, 'ok', 'ok', operation)
        payee = self.store.find_by_account(str(owner_id), dest)
        now = self._now()
        if payee is None:
            return PayeeDecision(
                False,
                'payee_not_registered',
                'Destination account is not a registered payee. Add it from Payees first.',
                operation,
            )
        status = payee.live_status(now)
        if status == 'cooling':
            retry = payee.retry_after(now)
            wait = f'{retry}s' if retry is not None else 'later'
            return PayeeDecision(
                False,
                'payee_cooling',
                f'Payee "{payee.nickname}" is in a cooling period. Try again in {wait}.',
                operation,
                payee=payee,
                retry_after=retry,
            )
        return PayeeDecision(True, 'ok', 'ok', operation, payee=payee)


def get_service() -> PayeeService:
    global _service
    with _service_lock:
        if _service is None:
            _service = PayeeService.from_env()
        return _service


def set_service(service: Optional[PayeeService]) -> None:
    global _service
    with _service_lock:
        _service = service


def payee_snapshot(session) -> dict:
    userid = (session or {}).get('userid') or ''
    try:
        return get_service().snapshot(str(userid))
    except Exception:
        policy = get_service().policy
        return {
            'enabled': policy.enabled,
            'cooling_seconds': policy.cooling_seconds,
            'max_payees': policy.max_payees,
            'allow_own_accounts': policy.allow_own_accounts,
            'operations': list(policy.operations),
            'payees': [],
            'count': 0,
        }


def enforce_payee(
    session,
    destination,
    owned_accounts: Optional[Iterable] = None,
    operation: str = 'transfer',
) -> Optional[Tuple[dict, int, dict]]:
    """Allowlist check. Returns (payload, status, headers) when denied, else None."""
    session = session or {}
    userid = session.get('userid')
    if not userid:
        return (
            {'error': 'unauthorized', 'code': 'unauthorized', 'message': 'Unauthorized access or session expired'},
            401,
            {},
        )
    decision = get_service().authorize(
        str(userid),
        destination,
        role=role_from_session(session),
        owned_accounts=owned_accounts,
        operation=operation,
    )
    if decision.allowed:
        return None
    headers = {}
    if decision.retry_after:
        headers['Retry-After'] = str(decision.retry_after)
    status = 400 if decision.code in {'invalid_account', 'invalid_nickname'} else 403
    return decision.as_dict(), status, headers


def _require_customer_session(session_data: Optional[dict], values: Optional[dict]) -> Optional[Dict[str, Any]]:
    session_data = session_data or {}
    values = values or {}
    if not session_data.get('userid'):
        return {'status': 401, 'body': {'message': 'Unauthorized access or session expired'}}
    if session_data.get('usertype') != 'customer':
        return {'status': 403, 'body': {'error': 'forbidden', 'code': 'forbidden', 'message': 'Customers only'}}
    if 'userid' in values and values.get('userid') != session_data.get('userid'):
        return {'status': 401, 'body': {'message': 'User ID mismatch'}}
    return None


def handle_list_request(session_data: Optional[dict], values: Optional[dict] = None) -> Dict[str, Any]:
    denied = _require_customer_session(session_data, values)
    if denied:
        return denied
    snap = get_service().snapshot(str(session_data['userid']))
    snap['message'] = 'ok'
    return {'status': 200, 'body': snap}


def handle_add_request(session_data: Optional[dict], values: Optional[dict] = None) -> Dict[str, Any]:
    denied = _require_customer_session(session_data, values)
    if denied:
        return denied
    values = values or {}
    if 'account' not in values or 'nickname' not in values:
        return {'status': 400, 'body': {'message': 'Some data missing'}}
    try:
        decision = get_service().add(str(session_data['userid']), values.get('account'), values.get('nickname'))
    except PayeeError as exc:
        return {
            'status': 400,
            'body': {'error': exc.code, 'code': exc.code, 'message': exc.message},
        }
    status = 200 if decision.allowed else (409 if decision.code.startswith('payee_duplicate') or decision.code == 'payee_limit' else 400)
    body = decision.as_dict()
    if decision.allowed:
        body['message'] = decision.message
    return {'status': status, 'body': body}


def handle_remove_request(session_data: Optional[dict], values: Optional[dict] = None) -> Dict[str, Any]:
    denied = _require_customer_session(session_data, values)
    if denied:
        return denied
    values = values or {}
    payee_id = values.get('payee_id') or values.get('id')
    if not payee_id:
        return {'status': 400, 'body': {'message': 'Some data missing'}}
    try:
        decision = get_service().remove(str(session_data['userid']), str(payee_id))
    except PayeeError as exc:
        return {
            'status': 400,
            'body': {'error': exc.code, 'code': exc.code, 'message': exc.message},
        }
    status = 200 if decision.allowed else 404
    return {'status': status, 'body': decision.as_dict()}


def attach_payee_routes(app: Any, service: Optional[PayeeService] = None) -> Any:
    """Mount payee CRUD on a Flask app. Used by tests and app.py."""
    from flask import jsonify, request, session

    if service is not None:
        set_service(service)

    @app.route('/listPayees', methods=['POST', 'GET'])
    def list_payees():
        values = request.get_json(silent=True) or {}
        result = handle_list_request(dict(session), values)
        return jsonify(result['body']), result['status']

    @app.route('/addPayee', methods=['POST', 'GET'])
    def add_payee():
        values = request.get_json(silent=True) or {}
        result = handle_add_request(dict(session), values)
        return jsonify(result['body']), result['status']

    @app.route('/removePayee', methods=['POST', 'GET'])
    def remove_payee():
        values = request.get_json(silent=True) or {}
        result = handle_remove_request(dict(session), values)
        return jsonify(result['body']), result['status']

    return app
