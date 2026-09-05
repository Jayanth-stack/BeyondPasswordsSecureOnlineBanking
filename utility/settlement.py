"""Delayed settlement / transaction-hold capability.

Outbound customer money movement can be accepted now and executed later.
First-time destinations get an extra hold (ACH-style new-payee delay) without
depending on the unmerged payee allowlist. Known destinations use the base
hold (default 0 = immediate). Customers can cancel before release.

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from flask import jsonify, request, session

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
GATED_OPERATIONS = frozenset({'transfer', 'withdraw', 'cheque'})
HOLD_STATUSES = frozenset({'held', 'settled', 'cancelled', 'failed'})
MONEY_QUANTUM = Decimal('0.01')
MAX_AMOUNT = Decimal('1000000000')


class AmountError(ValueError):
    pass


class AccountError(ValueError):
    pass


class HoldError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def parse_money(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise AmountError('invalid_amount')
    text = str(value).strip()
    if not text or any(ch in text for ch in 'eE+'):
        raise AmountError('invalid_amount')
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise AmountError('invalid_amount') from exc
    if amount.as_tuple().exponent < -2:
        raise AmountError('invalid_amount')
    quantized = amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    if quantized != amount:
        raise AmountError('invalid_amount')
    if quantized <= 0 or quantized > MAX_AMOUNT:
        raise AmountError('invalid_amount')
    return quantized


def canonical_amount(value: Any) -> str:
    return format(parse_money(value).quantize(MONEY_QUANTUM), 'f')


def normalize_account(value: Any, *, required: bool = True) -> str:
    if value is None:
        text = ''
    else:
        text = str(value).strip()
    if not text:
        if required:
            raise AccountError('invalid_account')
        return ''
    if text.endswith('.0') and text[:-2].isdigit():
        text = text[:-2]
    if not text.isdigit() or not (1 <= len(text) <= 16):
        raise AccountError('invalid_account')
    return str(int(text))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return int(raw)


def _env_decimal(name: str, default: Decimal) -> Decimal:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return Decimal(str(raw).strip())


def _own_account_set(own_accounts: Optional[Iterable[Any]]) -> set:
    found = set()
    for item in own_accounts or ():
        try:
            found.add(normalize_account(item))
        except AccountError:
            continue
    return found


@dataclass(frozen=True)
class HoldDecision:
    action: str
    seconds: int = 0
    reason: str = 'policy'

    @property
    def should_hold(self) -> bool:
        return self.action == 'hold' and self.seconds > 0


@dataclass
class Hold:
    hold_id: str
    userid: str
    usertype: str
    operation: str
    from_account: str
    to_account: str
    amount: str
    reason: str
    status: str
    created_at: float
    release_at: float
    settled_at: Optional[float] = None
    cancelled_at: Optional[float] = None
    result: Optional[str] = None
    last_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        remaining = max(0, int(round(self.release_at - time.time())))
        return {
            'hold_id': self.hold_id,
            'userid': self.userid,
            'operation': self.operation,
            'from_account': self.from_account,
            'to_account': self.to_account,
            'amount': self.amount,
            'reason': self.reason,
            'status': self.status,
            'created_at': self.created_at,
            'release_at': self.release_at,
            'settled_at': self.settled_at,
            'cancelled_at': self.cancelled_at,
            'result': self.result,
            'last_error': self.last_error,
            'seconds_remaining': remaining if self.status == 'held' else 0,
        }

    def fingerprint(self) -> Tuple[str, str, str, str, str]:
        return (self.userid, self.operation, self.from_account, self.to_account, self.amount)


@dataclass(frozen=True)
class HoldPolicy:
    enabled: bool = True
    customer_only: bool = True
    operations: frozenset = field(default_factory=lambda: frozenset(GATED_OPERATIONS))
    base_hold_seconds: int = 0
    new_destination_seconds: int = 86400
    amount_threshold: Decimal = Decimal('0')
    skip_own_accounts: bool = True
    max_open_holds: int = 20

    @classmethod
    def from_env(cls) -> 'HoldPolicy':
        operations = os.environ.get('HOLD_OPERATIONS', 'transfer,withdraw,cheque')
        parsed = frozenset(part.strip() for part in operations.split(',') if part.strip())
        return cls(
            enabled=_env_bool('HOLD_ENABLED', True),
            customer_only=_env_bool('HOLD_CUSTOMER_ONLY', True),
            operations=parsed or frozenset(GATED_OPERATIONS),
            base_hold_seconds=max(0, _env_int('HOLD_SECONDS', 0)),
            new_destination_seconds=max(0, _env_int('HOLD_NEW_DESTINATION_SECONDS', 86400)),
            amount_threshold=_env_decimal('HOLD_AMOUNT_THRESHOLD', Decimal('0')),
            skip_own_accounts=_env_bool('HOLD_ALLOW_OWN_ACCOUNTS', True),
            max_open_holds=max(1, _env_int('HOLD_MAX_OPEN', 20)),
        )

    def evaluate(
        self,
        *,
        usertype: str,
        operation: str,
        amount: Decimal,
        destination_known: bool,
        own_account: bool,
        has_destination: bool,
    ) -> HoldDecision:
        if not self.enabled:
            return HoldDecision('proceed', 0, 'disabled')
        if operation not in self.operations:
            return HoldDecision('proceed', 0, 'ungated_operation')
        if self.customer_only and usertype in EMPLOYEE_ROLES:
            return HoldDecision('proceed', 0, 'employee')
        if self.amount_threshold > 0 and amount < self.amount_threshold:
            return HoldDecision('proceed', 0, 'below_threshold')
        if own_account and self.skip_own_accounts:
            return HoldDecision('proceed', 0, 'own_account')

        seconds = self.base_hold_seconds
        reason = 'policy'
        if has_destination and not destination_known:
            seconds = max(seconds, self.new_destination_seconds)
            if self.new_destination_seconds > 0:
                reason = 'new_destination'
        if seconds <= 0:
            return HoldDecision('proceed', 0, 'immediate')
        return HoldDecision('hold', seconds, reason)


class MemorySettlementStore:
    def __init__(self) -> None:
        self._holds: Dict[str, Hold] = {}
        self._destinations: Dict[Tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def put_hold(self, hold: Hold) -> None:
        with self._lock:
            self._holds[hold.hold_id] = hold

    def get_hold(self, hold_id: str) -> Optional[Hold]:
        with self._lock:
            return self._holds.get(hold_id)

    def update_hold(self, hold: Hold) -> None:
        with self._lock:
            self._holds[hold.hold_id] = hold

    def list_holds(self, userid: Optional[str] = None, statuses: Optional[Iterable[str]] = None) -> List[Hold]:
        wanted = set(statuses) if statuses is not None else None
        with self._lock:
            rows = list(self._holds.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if wanted is not None:
            rows = [row for row in rows if row.status in wanted]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows

    def count_open(self, userid: str) -> int:
        return len(self.list_holds(userid, statuses={'held'}))

    def due_holds(self, now: float, userid: Optional[str] = None) -> List[Hold]:
        rows = self.list_holds(userid, statuses={'held'})
        return [row for row in rows if row.release_at <= now]

    def remember_destination(self, userid: str, account: str, when: float) -> None:
        if not account:
            return
        with self._lock:
            key = (userid, account)
            self._destinations[key] = min(when, self._destinations.get(key, when))

    def destination_known(self, userid: str, account: str) -> bool:
        if not account:
            return False
        with self._lock:
            return (userid, account) in self._destinations


class SqliteSettlementStore:
    def __init__(self, path: str) -> None:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS holds (
                    hold_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    usertype TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    from_account TEXT NOT NULL,
                    to_account TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    release_at REAL NOT NULL,
                    settled_at REAL,
                    cancelled_at REAL,
                    result TEXT,
                    last_error TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS destinations (
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL,
                    first_settled_at REAL NOT NULL,
                    PRIMARY KEY (userid, account)
                )
                """
            )
            conn.commit()

    def put_hold(self, hold: Hold) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO holds (
                    hold_id, userid, usertype, operation, from_account, to_account,
                    amount, reason, status, created_at, release_at, settled_at,
                    cancelled_at, result, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._row(hold),
            )
            conn.commit()

    def get_hold(self, hold_id: str) -> Optional[Hold]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM holds WHERE hold_id = ?', (hold_id,)).fetchone()
        return self._from_row(row) if row else None

    def update_hold(self, hold: Hold) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE holds SET
                    userid=?, usertype=?, operation=?, from_account=?, to_account=?,
                    amount=?, reason=?, status=?, created_at=?, release_at=?,
                    settled_at=?, cancelled_at=?, result=?, last_error=?
                WHERE hold_id=?
                """,
                self._row(hold)[1:] + (hold.hold_id,),
            )
            conn.commit()

    def list_holds(self, userid: Optional[str] = None, statuses: Optional[Iterable[str]] = None) -> List[Hold]:
        sql = 'SELECT * FROM holds'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if statuses is not None:
            status_list = list(statuses)
            clauses.append('status IN ({})'.format(','.join('?' * len(status_list))))
            params.extend(status_list)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._from_row(row) for row in rows]

    def count_open(self, userid: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM holds WHERE userid = ? AND status = 'held'",
                (userid,),
            ).fetchone()
        return int(row['n']) if row else 0

    def due_holds(self, now: float, userid: Optional[str] = None) -> List[Hold]:
        sql = "SELECT * FROM holds WHERE status = 'held' AND release_at <= ?"
        params: List[Any] = [now]
        if userid is not None:
            sql += ' AND userid = ?'
            params.append(userid)
        sql += ' ORDER BY release_at ASC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._from_row(row) for row in rows]

    def remember_destination(self, userid: str, account: str, when: float) -> None:
        if not account:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO destinations (userid, account, first_settled_at)
                VALUES (?, ?, ?)
                ON CONFLICT(userid, account) DO UPDATE SET
                    first_settled_at = MIN(destinations.first_settled_at, excluded.first_settled_at)
                """,
                (userid, account, when),
            )
            conn.commit()

    def destination_known(self, userid: str, account: str) -> bool:
        if not account:
            return False
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT 1 FROM destinations WHERE userid = ? AND account = ?',
                (userid, account),
            ).fetchone()
        return row is not None

    @staticmethod
    def _row(hold: Hold) -> Tuple[Any, ...]:
        return (
            hold.hold_id,
            hold.userid,
            hold.usertype,
            hold.operation,
            hold.from_account,
            hold.to_account,
            hold.amount,
            hold.reason,
            hold.status,
            hold.created_at,
            hold.release_at,
            hold.settled_at,
            hold.cancelled_at,
            hold.result,
            hold.last_error,
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Hold:
        return Hold(
            hold_id=row['hold_id'],
            userid=row['userid'],
            usertype=row['usertype'],
            operation=row['operation'],
            from_account=row['from_account'],
            to_account=row['to_account'],
            amount=row['amount'],
            reason=row['reason'],
            status=row['status'],
            created_at=row['created_at'],
            release_at=row['release_at'],
            settled_at=row['settled_at'],
            cancelled_at=row['cancelled_at'],
            result=row['result'],
            last_error=row['last_error'],
        )


class SettlementService:
    def __init__(
        self,
        policy: Optional[HoldPolicy] = None,
        store: Optional[Any] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.policy = policy or HoldPolicy()
        self.store = store or MemorySettlementStore()
        self.clock = clock

    def destination_known(self, userid: str, account: str) -> bool:
        try:
            account = normalize_account(account, required=False)
        except AccountError:
            return False
        return self.store.destination_known(userid, account)

    def remember_destination(self, userid: str, account: Any) -> None:
        try:
            dest = normalize_account(account, required=False)
        except AccountError:
            return
        if dest:
            self.store.remember_destination(userid, dest, self.clock())

    def evaluate(
        self,
        *,
        userid: str,
        usertype: str,
        operation: str,
        from_account: Any,
        to_account: Any = None,
        amount: Any,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> Tuple[HoldDecision, Decimal, str, str]:
        money = parse_money(amount)
        needs_dest = operation in {'transfer', 'cheque'}
        source = normalize_account(from_account, required=True)
        dest = normalize_account(to_account, required=needs_dest)
        owned = _own_account_set(own_accounts)
        decision = self.policy.evaluate(
            usertype=usertype or 'customer',
            operation=operation,
            amount=money,
            destination_known=bool(dest) and self.store.destination_known(userid, dest),
            own_account=bool(dest) and dest in owned,
            has_destination=bool(dest),
        )
        return decision, money, source, dest

    def place(
        self,
        *,
        userid: str,
        usertype: str,
        operation: str,
        from_account: str,
        to_account: str,
        amount: Decimal,
        reason: str,
        seconds: int,
    ) -> Hold:
        open_holds = self.store.list_holds(userid, statuses={'held'})
        if len(open_holds) >= self.policy.max_open_holds:
            raise HoldError('hold_limit', 'Too many held transfers. Cancel or wait for release.')
        amount_text = format(amount.quantize(MONEY_QUANTUM), 'f')
        fingerprint = (userid, operation, from_account, to_account, amount_text)
        for existing in open_holds:
            if existing.fingerprint() == fingerprint:
                raise HoldError(
                    'hold_duplicate',
                    'An identical transfer is already on hold.',
                    hold=existing,
                )
        now = self.clock()
        hold = Hold(
            hold_id=uuid.uuid4().hex,
            userid=userid,
            usertype=usertype or 'customer',
            operation=operation,
            from_account=from_account,
            to_account=to_account,
            amount=amount_text,
            reason=reason,
            status='held',
            created_at=now,
            release_at=now + seconds,
        )
        self.store.put_hold(hold)
        return hold

    def cancel(self, userid: str, hold_id: str) -> Hold:
        hold = self.store.get_hold(hold_id)
        if hold is None:
            raise HoldError('hold_not_found', 'Hold not found.')
        if hold.userid != userid:
            raise HoldError('hold_forbidden', 'Hold does not belong to this user.')
        if hold.status != 'held':
            raise HoldError('hold_not_open', 'Hold is no longer cancellable.', hold=hold)
        hold.status = 'cancelled'
        hold.cancelled_at = self.clock()
        self.store.update_hold(hold)
        return hold

    def settle_due(
        self,
        executor: Callable[[Hold], Any],
        *,
        now: Optional[float] = None,
        userid: Optional[str] = None,
    ) -> List[Hold]:
        when = self.clock() if now is None else now
        settled: List[Hold] = []
        for hold in self.store.due_holds(when, userid=userid):
            try:
                result = executor(hold)
            except Exception as exc:
                hold.last_error = str(exc)
                self.store.update_hold(hold)
                continue
            hold.status = 'settled'
            hold.settled_at = when
            hold.result = '' if result is None else str(result)
            hold.last_error = None
            self.store.update_hold(hold)
            if hold.to_account:
                self.store.remember_destination(hold.userid, hold.to_account, when)
            settled.append(hold)
        return settled

    def list_for(self, userid: str, *, include_closed: bool = True) -> List[Hold]:
        statuses = None if include_closed else {'held'}
        return self.store.list_holds(userid, statuses=statuses)

    def snapshot(self, userid: str) -> Dict[str, Any]:
        holds = self.list_for(userid)
        open_holds = [hold.to_dict() for hold in holds if hold.status == 'held']
        return {
            'enabled': self.policy.enabled,
            'base_hold_seconds': self.policy.base_hold_seconds,
            'new_destination_seconds': self.policy.new_destination_seconds,
            'amount_threshold': format(self.policy.amount_threshold, 'f'),
            'operations': sorted(self.policy.operations),
            'open_count': len(open_holds),
            'holds': [hold.to_dict() for hold in holds],
        }


def default_store() -> Any:
    kind = os.environ.get('HOLD_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemorySettlementStore()
    path = os.environ.get('HOLD_DB', 'SystemLogs/settlement.sqlite')
    return SqliteSettlementStore(path)


def build_service() -> SettlementService:
    return SettlementService(HoldPolicy.from_env(), default_store())


def held_payload(hold: Hold, now: Optional[float] = None) -> Dict[str, Any]:
    remaining = max(0, int(round(hold.release_at - (time.time() if now is None else now))))
    if hold.reason == 'new_destination':
        message = (
            'Transfer held until the destination cooling-off period ends. '
            'You can cancel it before release.'
        )
    else:
        message = 'Transfer held for delayed settlement. You can cancel it before release.'
    payload = hold.to_dict()
    payload.update({
        'message': message,
        'error': 'held',
        'status': 'held',
        'retry_after': remaining,
    })
    return payload


def enforce_hold(
    service: SettlementService,
    *,
    userid: str,
    usertype: str,
    operation: str,
    from_account: Any,
    to_account: Any = None,
    amount: Any,
    own_accounts: Optional[Iterable[Any]] = None,
) -> Optional[Tuple[Dict[str, Any], int, Dict[str, str]]]:
    """Return (body, status, headers) to short-circuit, or None to run the existing handler."""
    try:
        decision, money, source, dest = service.evaluate(
            userid=userid,
            usertype=usertype,
            operation=operation,
            from_account=from_account,
            to_account=to_account,
            amount=amount,
            own_accounts=own_accounts,
        )
    except AmountError:
        return {'message': 'Enter a valid amount', 'error': 'invalid_amount'}, 400, {}
    except AccountError:
        return {'message': 'Invalid account', 'error': 'invalid_account'}, 400, {}

    if not decision.should_hold:
        return None

    try:
        hold = service.place(
            userid=userid,
            usertype=usertype,
            operation=operation,
            from_account=source,
            to_account=dest,
            amount=money,
            reason=decision.reason,
            seconds=decision.seconds,
        )
    except HoldError as exc:
        status = 409 if exc.code in {'hold_limit', 'hold_duplicate'} else 400
        body = {'message': exc.message, 'error': exc.code}
        existing = exc.extra.get('hold')
        if existing is not None:
            body['hold'] = existing.to_dict()
        return body, status, {}

    headers = {}
    remaining = max(0, int(round(hold.release_at - service.clock())))
    if remaining:
        headers['Retry-After'] = str(remaining)
    return held_payload(hold, now=service.clock()), 202, headers


def remember_if_destination(service: SettlementService, userid: str, to_account: Any) -> None:
    service.remember_destination(userid, to_account)


def _session_userid() -> Optional[str]:
    return session.get('userid')


def _require_session_user() -> Tuple[Optional[str], Optional[Tuple[Any, int]]]:
    userid = _session_userid()
    if not userid:
        return None, (jsonify({'message': 'Unauthorized access or session expired'}), 401)
    values = request.get_json(silent=True) or {}
    claimed = values.get('userid')
    if claimed is not None and str(claimed) != str(userid):
        return None, (jsonify({'message': 'User ID mismatch', 'error': 'userid_mismatch'}), 403)
    return str(userid), None


def handle_list_holds(service: SettlementService, executor: Optional[Callable[[Hold], Any]] = None):
    userid, error = _require_session_user()
    if error:
        return error
    if executor is not None:
        service.settle_due(executor)
    return jsonify({'Holds': service.snapshot(userid)}), 200


def handle_cancel_hold(service: SettlementService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    hold_id = str(values.get('hold_id') or '').strip()
    if not hold_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_hold_id'}), 400
    try:
        hold = service.cancel(userid, hold_id)
    except HoldError as exc:
        status = {'hold_not_found': 404, 'hold_forbidden': 403, 'hold_not_open': 409}.get(exc.code, 400)
        return jsonify({'message': exc.message, 'error': exc.code}), status
    return jsonify({'message': 'Hold cancelled', 'hold': hold.to_dict()}), 200


def handle_settle_due(service: SettlementService, executor: Callable[[Hold], Any]):
    userid, error = _require_session_user()
    if error:
        return error
    settled = service.settle_due(executor)
    mine = [hold.to_dict() for hold in settled if hold.userid == userid]
    return jsonify({'message': 'Settled due holds', 'settled': mine, 'Holds': service.snapshot(userid)}), 200


def attach_hold_routes(
    app,
    service: SettlementService,
    executor: Optional[Callable[[Hold], Any]] = None,
) -> None:
    @app.route('/listHolds', methods=['POST', 'GET'])
    def list_holds():
        return handle_list_holds(service, executor)

    @app.route('/cancelHold', methods=['POST', 'GET'])
    def cancel_hold():
        return handle_cancel_hold(service)

    @app.route('/settleDue', methods=['POST', 'GET'])
    def settle_due():
        if executor is None:
            return jsonify({'message': 'Settlement executor is not configured'}), 503
        return handle_settle_due(service, executor)


def own_accounts_from_customer_payload(accounts: Any) -> List[str]:
    if not isinstance(accounts, dict):
        return []
    found = []
    for key in ('savings', 'checkin', 'credit'):
        item = accounts.get(key)
        if isinstance(item, dict) and item.get('Account') not in (None, 'None', ''):
            try:
                found.append(normalize_account(item['Account']))
            except AccountError:
                continue
    return found
