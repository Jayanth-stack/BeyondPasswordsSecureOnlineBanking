"""Account freeze and cashier-cheque stop-payment.

Temporary outbound lock, distinct from employee deactivate/close
(`Accounts.active` / `/deactivateAccount`). Outstanding cheques are killed
with a stop-payment rather than relying on the unmerged settlement hold.

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import jsonify, request, session

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
OUTBOUND_OPERATIONS = frozenset({'transfer', 'withdraw', 'cheque', 'approve', 'request'})
INBOUND_OPERATIONS = frozenset({'deposit'})
FREEZE_REASONS = frozenset({'customer', 'lost', 'employee', 'fraud', 'legal'})
CUSTOMER_REASONS = frozenset({'customer', 'lost'})
EMPLOYEE_ONLY_REASONS = frozenset({'employee', 'fraud', 'legal'})
SELF_UNFREEZE_REASONS = frozenset({'customer', 'lost'})
FREEZE_STATUSES = frozenset({'active', 'released'})
STOP_STATUSES = frozenset({'active', 'cancelled', 'honored'})
SCOPE_ACCOUNT = 'account'
SCOPE_CUSTOMER = 'customer'
WILDCARD_ACCOUNT = '*'


class AccountError(ValueError):
    pass


class ChequeError(ValueError):
    pass


class FreezeError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def normalize_account(value: Any, *, required: bool = True, allow_wildcard: bool = False) -> str:
    if value is None:
        text = ''
    else:
        text = str(value).strip()
    if allow_wildcard and text in {'*', 'all', 'customer'}:
        return WILDCARD_ACCOUNT
    if not text:
        if required:
            raise AccountError('invalid_account')
        return ''
    if text.endswith('.0') and text[:-2].isdigit():
        text = text[:-2]
    if not text.isdigit() or not (1 <= len(text) <= 16):
        raise AccountError('invalid_account')
    return str(int(text))


def normalize_cheque(value: Any) -> str:
    if value is None:
        text = ''
    else:
        text = str(value).strip()
    if text.endswith('.0') and text[:-2].isdigit():
        text = text[:-2]
    if not text.isdigit() or not (1 <= len(text) <= 16):
        raise ChequeError('invalid_cheque')
    return str(int(text))


def normalize_reason(value: Any, *, default: str = 'customer') -> str:
    text = str(value or default).strip().lower() or default
    if text not in FREEZE_REASONS:
        raise FreezeError('invalid_reason', 'Unknown freeze/stop reason.')
    return text


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


def _own_account_set(own_accounts: Optional[Iterable[Any]]) -> set:
    found = set()
    for item in own_accounts or ():
        try:
            found.add(normalize_account(item))
        except AccountError:
            continue
    return found


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


@dataclass(frozen=True)
class FreezeDecision:
    action: str
    reason: str = 'ok'
    freeze: Optional['Freeze'] = None

    @property
    def blocked(self) -> bool:
        return self.action == 'block'


@dataclass
class Freeze:
    freeze_id: str
    userid: str
    account: str
    scope: str
    reason: str
    actor: str
    actor_type: str
    note: str
    status: str
    created_at: float
    released_at: Optional[float] = None
    released_by: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'freeze_id': self.freeze_id,
            'userid': self.userid,
            'account': self.account,
            'scope': self.scope,
            'reason': self.reason,
            'actor': self.actor,
            'actor_type': self.actor_type,
            'note': self.note,
            'status': self.status,
            'created_at': self.created_at,
            'released_at': self.released_at,
            'released_by': self.released_by,
        }


@dataclass
class StopPayment:
    stop_id: str
    userid: str
    cheque_no: str
    account: str
    reason: str
    actor: str
    actor_type: str
    note: str
    status: str
    created_at: float
    cancelled_at: Optional[float] = None
    cancelled_by: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'stop_id': self.stop_id,
            'userid': self.userid,
            'cheque_no': self.cheque_no,
            'account': self.account,
            'reason': self.reason,
            'actor': self.actor,
            'actor_type': self.actor_type,
            'note': self.note,
            'status': self.status,
            'created_at': self.created_at,
            'cancelled_at': self.cancelled_at,
            'cancelled_by': self.cancelled_by,
        }


@dataclass(frozen=True)
class FreezePolicy:
    enabled: bool = True
    outbound_blocked: bool = True
    inbound_blocked: bool = False
    customer_self_freeze: bool = True
    customer_self_unfreeze: bool = True
    stop_payment_enabled: bool = True
    customer_stop_payment: bool = True
    operations: frozenset = field(default_factory=lambda: frozenset(OUTBOUND_OPERATIONS))
    max_open_freezes: int = 20
    max_open_stops: int = 50

    @classmethod
    def from_env(cls) -> 'FreezePolicy':
        operations = os.environ.get('FREEZE_OPERATIONS', 'transfer,withdraw,cheque,approve,request')
        parsed = frozenset(part.strip() for part in operations.split(',') if part.strip())
        return cls(
            enabled=_env_bool('FREEZE_ENABLED', True),
            outbound_blocked=_env_bool('FREEZE_BLOCK_OUTBOUND', True),
            inbound_blocked=_env_bool('FREEZE_BLOCK_INBOUND', False),
            customer_self_freeze=_env_bool('FREEZE_CUSTOMER_SELF', True),
            customer_self_unfreeze=_env_bool('FREEZE_CUSTOMER_UNFREEZE', True),
            stop_payment_enabled=_env_bool('STOP_PAYMENT_ENABLED', True),
            customer_stop_payment=_env_bool('STOP_PAYMENT_CUSTOMER', True),
            operations=parsed or frozenset(OUTBOUND_OPERATIONS),
            max_open_freezes=max(1, _env_int('FREEZE_MAX_OPEN', 20)),
            max_open_stops=max(1, _env_int('STOP_PAYMENT_MAX_OPEN', 50)),
        )

    def evaluate_outbound(self, operation: str, freeze: Optional[Freeze]) -> FreezeDecision:
        if not self.enabled or freeze is None:
            return FreezeDecision('proceed', 'ok')
        if operation in INBOUND_OPERATIONS:
            if self.inbound_blocked:
                return FreezeDecision('block', freeze.reason, freeze)
            return FreezeDecision('proceed', 'inbound_allowed', freeze)
        if operation not in self.operations:
            return FreezeDecision('proceed', 'ungated_operation')
        if not self.outbound_blocked:
            return FreezeDecision('proceed', 'outbound_disabled', freeze)
        return FreezeDecision('block', freeze.reason, freeze)


class MemoryFreezeStore:
    def __init__(self) -> None:
        self._freezes: Dict[str, Freeze] = {}
        self._stops: Dict[str, StopPayment] = {}
        self._lock = threading.Lock()

    def put_freeze(self, freeze: Freeze) -> None:
        with self._lock:
            self._freezes[freeze.freeze_id] = freeze

    def get_freeze(self, freeze_id: str) -> Optional[Freeze]:
        with self._lock:
            return self._freezes.get(freeze_id)

    def update_freeze(self, freeze: Freeze) -> None:
        with self._lock:
            self._freezes[freeze.freeze_id] = freeze

    def list_freezes(
        self,
        userid: Optional[str] = None,
        statuses: Optional[Iterable[str]] = None,
        account: Optional[str] = None,
    ) -> List[Freeze]:
        wanted = set(statuses) if statuses is not None else None
        with self._lock:
            rows = list(self._freezes.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if account is not None:
            rows = [row for row in rows if row.account == account]
        if wanted is not None:
            rows = [row for row in rows if row.status in wanted]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows

    def active_for_account(self, account: str) -> Optional[Freeze]:
        with self._lock:
            matches = [
                row for row in self._freezes.values()
                if row.status == 'active' and row.account == account
            ]
        if not matches:
            return None
        matches.sort(key=lambda row: row.created_at, reverse=True)
        return matches[0]

    def active_customer_scope(self, userid: str) -> Optional[Freeze]:
        with self._lock:
            matches = [
                row for row in self._freezes.values()
                if row.status == 'active' and row.userid == userid and row.scope == SCOPE_CUSTOMER
            ]
        if not matches:
            return None
        matches.sort(key=lambda row: row.created_at, reverse=True)
        return matches[0]

    def put_stop(self, stop: StopPayment) -> None:
        with self._lock:
            self._stops[stop.stop_id] = stop

    def get_stop(self, stop_id: str) -> Optional[StopPayment]:
        with self._lock:
            return self._stops.get(stop_id)

    def update_stop(self, stop: StopPayment) -> None:
        with self._lock:
            self._stops[stop.stop_id] = stop

    def list_stops(
        self,
        userid: Optional[str] = None,
        statuses: Optional[Iterable[str]] = None,
        cheque_no: Optional[str] = None,
    ) -> List[StopPayment]:
        wanted = set(statuses) if statuses is not None else None
        with self._lock:
            rows = list(self._stops.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if cheque_no is not None:
            rows = [row for row in rows if row.cheque_no == cheque_no]
        if wanted is not None:
            rows = [row for row in rows if row.status in wanted]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows

    def active_stop(self, cheque_no: str) -> Optional[StopPayment]:
        with self._lock:
            matches = [
                row for row in self._stops.values()
                if row.status == 'active' and row.cheque_no == cheque_no
            ]
        if not matches:
            return None
        matches.sort(key=lambda row: row.created_at, reverse=True)
        return matches[0]


class SqliteFreezeStore:
    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def _init(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS freezes (
                    freeze_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    actor_type TEXT NOT NULL,
                    note TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    released_at REAL,
                    released_by TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stops (
                    stop_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    cheque_no TEXT NOT NULL,
                    account TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    actor_type TEXT NOT NULL,
                    note TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    cancelled_at REAL,
                    cancelled_by TEXT
                )
                """
            )
            conn.execute('CREATE INDEX IF NOT EXISTS idx_freezes_user ON freezes(userid, status)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_freezes_account ON freezes(account, status)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_stops_user ON stops(userid, status)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_stops_cheque ON stops(cheque_no, status)')
            conn.commit()

    def put_freeze(self, freeze: Freeze) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO freezes (
                    freeze_id, userid, account, scope, reason, actor, actor_type,
                    note, status, created_at, released_at, released_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._freeze_row(freeze),
            )
            conn.commit()

    def get_freeze(self, freeze_id: str) -> Optional[Freeze]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM freezes WHERE freeze_id = ?', (freeze_id,)
            ).fetchone()
        return self._freeze_from_row(row) if row else None

    def update_freeze(self, freeze: Freeze) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE freezes SET
                    userid=?, account=?, scope=?, reason=?, actor=?, actor_type=?,
                    note=?, status=?, created_at=?, released_at=?, released_by=?
                WHERE freeze_id=?
                """,
                self._freeze_row(freeze)[1:] + (freeze.freeze_id,),
            )
            conn.commit()

    def list_freezes(
        self,
        userid: Optional[str] = None,
        statuses: Optional[Iterable[str]] = None,
        account: Optional[str] = None,
    ) -> List[Freeze]:
        clauses = []
        params: List[Any] = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if account is not None:
            clauses.append('account = ?')
            params.append(account)
        if statuses is not None:
            wanted = list(statuses)
            clauses.append('status IN (%s)' % ','.join('?' * len(wanted)))
            params.extend(wanted)
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM freezes %s ORDER BY created_at DESC' % where, params
            ).fetchall()
        return [self._freeze_from_row(row) for row in rows]

    def active_for_account(self, account: str) -> Optional[Freeze]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM freezes
                WHERE status = 'active' AND account = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (account,),
            ).fetchone()
        return self._freeze_from_row(row) if row else None

    def active_customer_scope(self, userid: str) -> Optional[Freeze]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM freezes
                WHERE status = 'active' AND userid = ? AND scope = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (userid, SCOPE_CUSTOMER),
            ).fetchone()
        return self._freeze_from_row(row) if row else None

    def put_stop(self, stop: StopPayment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO stops (
                    stop_id, userid, cheque_no, account, reason, actor, actor_type,
                    note, status, created_at, cancelled_at, cancelled_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._stop_row(stop),
            )
            conn.commit()

    def get_stop(self, stop_id: str) -> Optional[StopPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM stops WHERE stop_id = ?', (stop_id,)
            ).fetchone()
        return self._stop_from_row(row) if row else None

    def update_stop(self, stop: StopPayment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE stops SET
                    userid=?, cheque_no=?, account=?, reason=?, actor=?, actor_type=?,
                    note=?, status=?, created_at=?, cancelled_at=?, cancelled_by=?
                WHERE stop_id=?
                """,
                self._stop_row(stop)[1:] + (stop.stop_id,),
            )
            conn.commit()

    def list_stops(
        self,
        userid: Optional[str] = None,
        statuses: Optional[Iterable[str]] = None,
        cheque_no: Optional[str] = None,
    ) -> List[StopPayment]:
        clauses = []
        params: List[Any] = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if cheque_no is not None:
            clauses.append('cheque_no = ?')
            params.append(cheque_no)
        if statuses is not None:
            wanted = list(statuses)
            clauses.append('status IN (%s)' % ','.join('?' * len(wanted)))
            params.extend(wanted)
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM stops %s ORDER BY created_at DESC' % where, params
            ).fetchall()
        return [self._stop_from_row(row) for row in rows]

    def active_stop(self, cheque_no: str) -> Optional[StopPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM stops
                WHERE status = 'active' AND cheque_no = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (cheque_no,),
            ).fetchone()
        return self._stop_from_row(row) if row else None

    @staticmethod
    def _freeze_row(freeze: Freeze) -> Tuple[Any, ...]:
        return (
            freeze.freeze_id,
            freeze.userid,
            freeze.account,
            freeze.scope,
            freeze.reason,
            freeze.actor,
            freeze.actor_type,
            freeze.note,
            freeze.status,
            freeze.created_at,
            freeze.released_at,
            freeze.released_by,
        )

    @staticmethod
    def _freeze_from_row(row: sqlite3.Row) -> Freeze:
        return Freeze(
            freeze_id=row['freeze_id'],
            userid=row['userid'],
            account=row['account'],
            scope=row['scope'],
            reason=row['reason'],
            actor=row['actor'],
            actor_type=row['actor_type'],
            note=row['note'],
            status=row['status'],
            created_at=row['created_at'],
            released_at=row['released_at'],
            released_by=row['released_by'],
        )

    @staticmethod
    def _stop_row(stop: StopPayment) -> Tuple[Any, ...]:
        return (
            stop.stop_id,
            stop.userid,
            stop.cheque_no,
            stop.account,
            stop.reason,
            stop.actor,
            stop.actor_type,
            stop.note,
            stop.status,
            stop.created_at,
            stop.cancelled_at,
            stop.cancelled_by,
        )

    @staticmethod
    def _stop_from_row(row: sqlite3.Row) -> StopPayment:
        return StopPayment(
            stop_id=row['stop_id'],
            userid=row['userid'],
            cheque_no=row['cheque_no'],
            account=row['account'],
            reason=row['reason'],
            actor=row['actor'],
            actor_type=row['actor_type'],
            note=row['note'],
            status=row['status'],
            created_at=row['created_at'],
            cancelled_at=row['cancelled_at'],
            cancelled_by=row['cancelled_by'],
        )


class FreezeService:
    def __init__(
        self,
        policy: Optional[FreezePolicy] = None,
        store: Optional[Any] = None,
        clock: Any = time.time,
    ) -> None:
        self.policy = policy or FreezePolicy()
        self.store = store or MemoryFreezeStore()
        self.clock = clock

    def _actor_is_employee(self, actor_type: str) -> bool:
        return (actor_type or '') in EMPLOYEE_ROLES

    def find_active(self, account: Any = None, userid: Optional[str] = None) -> Optional[Freeze]:
        try:
            normalized = normalize_account(account, required=False)
        except AccountError:
            normalized = ''
        if normalized:
            found = self.store.active_for_account(normalized)
            if found is not None:
                return found
        if userid:
            return self.store.active_customer_scope(userid)
        return None

    def is_frozen(self, account: Any = None, userid: Optional[str] = None) -> bool:
        return self.find_active(account=account, userid=userid) is not None

    def freeze_account(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any = None,
        reason: Any = None,
        note: Any = '',
        scope: str = SCOPE_ACCOUNT,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> Freeze:
        if not self.policy.enabled:
            raise FreezeError('freeze_disabled', 'Account freeze is disabled.')
        employee = self._actor_is_employee(actor_type)
        if not employee and not self.policy.customer_self_freeze:
            raise FreezeError('freeze_forbidden', 'Customers cannot freeze accounts.')
        reason_text = normalize_reason(reason, default='employee' if employee else 'customer')
        if not employee and reason_text in EMPLOYEE_ONLY_REASONS:
            raise FreezeError('freeze_forbidden', 'Customers cannot apply that freeze reason.')
        scope_text = SCOPE_CUSTOMER if str(scope).strip().lower() in {'customer', 'all', '*'} else SCOPE_ACCOUNT
        if scope_text == SCOPE_CUSTOMER:
            account_text = WILDCARD_ACCOUNT
        else:
            account_text = normalize_account(account, required=True)
            if not employee and own_accounts is not None:
                owned = _own_account_set(own_accounts)
                if account_text not in owned:
                    raise FreezeError('freeze_forbidden', 'Account does not belong to this customer.')
        existing = self.find_active(
            account=account_text if scope_text == SCOPE_ACCOUNT else None,
            userid=owner_userid,
        )
        if existing is not None and (
            scope_text == SCOPE_CUSTOMER
            or existing.account == account_text
            or existing.scope == SCOPE_CUSTOMER
        ):
            raise FreezeError('freeze_duplicate', 'Account is already frozen.', freeze=existing)

        open_count = len(self.store.list_freezes(owner_userid, statuses={'active'}))
        if open_count >= self.policy.max_open_freezes:
            raise FreezeError('freeze_limit', 'Too many active freezes.')

        freeze = Freeze(
            freeze_id=uuid.uuid4().hex,
            userid=str(owner_userid),
            account=account_text,
            scope=scope_text,
            reason=reason_text,
            actor=str(actor),
            actor_type=str(actor_type or 'customer'),
            note=str(note or '')[:240],
            status='active',
            created_at=float(self.clock()),
        )
        self.store.put_freeze(freeze)
        return freeze

    def unfreeze(
        self,
        *,
        freeze_id: str,
        actor: str,
        actor_type: str,
        owner_userid: Optional[str] = None,
    ) -> Freeze:
        freeze = self.store.get_freeze(str(freeze_id or '').strip())
        if freeze is None:
            raise FreezeError('freeze_not_found', 'Freeze not found.')
        employee = self._actor_is_employee(actor_type)
        if not employee:
            if freeze.userid != str(owner_userid or actor):
                raise FreezeError('freeze_forbidden', 'Freeze does not belong to this user.')
            if not self.policy.customer_self_unfreeze:
                raise FreezeError('freeze_forbidden', 'Customers cannot unfreeze accounts.')
            if freeze.reason not in SELF_UNFREEZE_REASONS:
                raise FreezeError('freeze_locked', 'Only bank staff can release this freeze.')
        if freeze.status != 'active':
            raise FreezeError('freeze_not_active', 'Freeze is already released.', freeze=freeze)
        freeze.status = 'released'
        freeze.released_at = float(self.clock())
        freeze.released_by = str(actor)
        self.store.update_freeze(freeze)
        return freeze

    def stop_payment(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        cheque_no: Any,
        account: Any = None,
        reason: Any = 'customer',
        note: Any = '',
    ) -> StopPayment:
        if not self.policy.stop_payment_enabled:
            raise FreezeError('stop_disabled', 'Stop-payment is disabled.')
        employee = self._actor_is_employee(actor_type)
        if not employee and not self.policy.customer_stop_payment:
            raise FreezeError('stop_forbidden', 'Customers cannot stop cheques.')
        cheque = normalize_cheque(cheque_no)
        account_text = normalize_account(account, required=False)
        existing = self.store.active_stop(cheque)
        if existing is not None:
            raise FreezeError('stop_duplicate', 'A stop-payment is already active for this cheque.', stop=existing)
        open_count = len(self.store.list_stops(owner_userid, statuses={'active'}))
        if open_count >= self.policy.max_open_stops:
            raise FreezeError('stop_limit', 'Too many active stop-payments.')
        reason_text = str(reason or 'customer').strip().lower() or 'customer'
        if reason_text not in FREEZE_REASONS:
            reason_text = 'customer'
        stop = StopPayment(
            stop_id=uuid.uuid4().hex,
            userid=str(owner_userid),
            cheque_no=cheque,
            account=account_text,
            reason=reason_text,
            actor=str(actor),
            actor_type=str(actor_type or 'customer'),
            note=str(note or '')[:240],
            status='active',
            created_at=float(self.clock()),
        )
        self.store.put_stop(stop)
        return stop

    def cancel_stop(
        self,
        *,
        stop_id: str,
        actor: str,
        actor_type: str,
        owner_userid: Optional[str] = None,
    ) -> StopPayment:
        stop = self.store.get_stop(str(stop_id or '').strip())
        if stop is None:
            raise FreezeError('stop_not_found', 'Stop-payment not found.')
        employee = self._actor_is_employee(actor_type)
        if not employee and stop.userid != str(owner_userid or actor):
            raise FreezeError('stop_forbidden', 'Stop-payment does not belong to this user.')
        if stop.status != 'active':
            raise FreezeError('stop_not_active', 'Stop-payment is no longer active.', stop=stop)
        stop.status = 'cancelled'
        stop.cancelled_at = float(self.clock())
        stop.cancelled_by = str(actor)
        self.store.update_stop(stop)
        return stop

    def honor_stop(self, cheque_no: Any) -> Optional[StopPayment]:
        try:
            cheque = normalize_cheque(cheque_no)
        except ChequeError:
            return None
        stop = self.store.active_stop(cheque)
        if stop is None:
            return None
        stop.status = 'honored'
        stop.cancelled_at = float(self.clock())
        stop.cancelled_by = 'deposit'
        self.store.update_stop(stop)
        return stop

    def evaluate(
        self,
        *,
        operation: str,
        account: Any = None,
        userid: Optional[str] = None,
    ) -> FreezeDecision:
        freeze = self.find_active(account=account, userid=userid)
        return self.policy.evaluate_outbound(operation, freeze)

    def snapshot(self, userid: str) -> Dict[str, Any]:
        freezes = self.store.list_freezes(userid)
        stops = self.store.list_stops(userid)
        active = [row.to_dict() for row in freezes if row.status == 'active']
        frozen_accounts = sorted({
            row['account'] for row in active if row['account'] != WILDCARD_ACCOUNT
        })
        return {
            'enabled': self.policy.enabled,
            'outbound_blocked': self.policy.outbound_blocked,
            'inbound_blocked': self.policy.inbound_blocked,
            'stop_payment_enabled': self.policy.stop_payment_enabled,
            'operations': sorted(self.policy.operations),
            'customer_frozen': any(row['scope'] == SCOPE_CUSTOMER for row in active),
            'frozen_accounts': frozen_accounts,
            'open_freeze_count': len(active),
            'open_stop_count': len([row for row in stops if row.status == 'active']),
            'freezes': [row.to_dict() for row in freezes],
            'stops': [row.to_dict() for row in stops],
        }


def default_store() -> Any:
    kind = os.environ.get('FREEZE_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryFreezeStore()
    path = os.environ.get('FREEZE_DB', 'SystemLogs/freeze.sqlite')
    return SqliteFreezeStore(path)


def build_service() -> FreezeService:
    return FreezeService(FreezePolicy.from_env(), default_store())


def _blocked_payload(decision: FreezeDecision, operation: str) -> Dict[str, Any]:
    freeze = decision.freeze
    reason = decision.reason
    if operation in INBOUND_OPERATIONS:
        message = 'This account is frozen and cannot receive that credit.'
    else:
        message = 'This account is frozen. Outbound transfers, withdrawals, and cheques are blocked.'
    payload = {
        'message': message,
        'error': 'account_frozen',
        'operation': operation,
        'reason': reason,
    }
    if freeze is not None:
        payload['freeze'] = freeze.to_dict()
    return payload


def enforce_freeze(
    service: FreezeService,
    *,
    operation: str,
    account: Any = None,
    userid: Optional[str] = None,
) -> Optional[Tuple[Dict[str, Any], int]]:
    """Return (body, status) to short-circuit, or None to run the existing handler."""
    try:
        if account not in (None, '', WILDCARD_ACCOUNT):
            normalize_account(account, required=True)
    except AccountError:
        return {'message': 'Invalid account', 'error': 'invalid_account'}, 400

    decision = service.evaluate(operation=operation, account=account, userid=userid)
    if not decision.blocked:
        return None
    return _blocked_payload(decision, operation), 403


def enforce_stop(
    service: FreezeService,
    *,
    cheque_no: Any,
) -> Optional[Tuple[Dict[str, Any], int]]:
    if not service.policy.stop_payment_enabled:
        return None
    try:
        cheque = normalize_cheque(cheque_no)
    except ChequeError:
        return {'message': 'Invalid cheque number', 'error': 'invalid_cheque'}, 400
    stop = service.store.active_stop(cheque)
    if stop is None:
        return None
    return {
        'message': 'This cashier cheque has a stop-payment and cannot be deposited.',
        'error': 'cheque_stopped',
        'stop': stop.to_dict(),
    }, 403


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


def _owner_userid(actor: str, actor_type: str, values: Dict[str, Any]) -> str:
    if actor_type in EMPLOYEE_ROLES:
        return str(values.get('customer_id') or values.get('owner') or '').strip()
    return actor


def handle_freeze_account(service: FreezeService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    own_accounts = None
    if actor_type == 'customer' and callable(own_accounts_loader):
        own_accounts = own_accounts_loader(userid)
    try:
        freeze = service.freeze_account(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            reason=values.get('reason'),
            note=values.get('note') or '',
            scope=str(values.get('scope') or SCOPE_ACCOUNT),
            own_accounts=own_accounts,
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except FreezeError as exc:
        status = {
            'freeze_duplicate': 409,
            'freeze_limit': 409,
            'freeze_forbidden': 403,
            'freeze_disabled': 403,
            'invalid_reason': 400,
        }.get(exc.code, 400)
        body = {'message': exc.message, 'error': exc.code}
        if exc.extra.get('freeze') is not None:
            body['freeze'] = exc.extra['freeze'].to_dict()
        return jsonify(body), status
    return jsonify({'message': 'Account frozen', 'freeze': freeze.to_dict(), 'Freezes': service.snapshot(owner)}), 200


def handle_unfreeze_account(service: FreezeService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    freeze_id = str(values.get('freeze_id') or '').strip()
    if not freeze_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_freeze_id'}), 400
    try:
        freeze = service.unfreeze(
            freeze_id=freeze_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
            owner_userid=userid,
        )
    except FreezeError as exc:
        status = {
            'freeze_not_found': 404,
            'freeze_forbidden': 403,
            'freeze_locked': 403,
            'freeze_not_active': 409,
        }.get(exc.code, 400)
        return jsonify({'message': exc.message, 'error': exc.code}), status
    return jsonify({'message': 'Account unfrozen', 'freeze': freeze.to_dict(), 'Freezes': service.snapshot(freeze.userid)}), 200


def handle_list_freezes(service: FreezeService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Freezes': service.snapshot(owner)}), 200


def handle_stop_payment(service: FreezeService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values) or userid
    if actor_type in EMPLOYEE_ROLES and not str(values.get('customer_id') or '').strip():
        owner = str(values.get('userid') or userid)
    try:
        stop = service.stop_payment(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            cheque_no=values.get('cheque_no'),
            account=values.get('account'),
            reason=values.get('reason') or 'customer',
            note=values.get('note') or '',
        )
    except ChequeError:
        return jsonify({'message': 'Invalid cheque number', 'error': 'invalid_cheque'}), 400
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except FreezeError as exc:
        status = {
            'stop_duplicate': 409,
            'stop_limit': 409,
            'stop_forbidden': 403,
            'stop_disabled': 403,
        }.get(exc.code, 400)
        body = {'message': exc.message, 'error': exc.code}
        if exc.extra.get('stop') is not None:
            body['stop'] = exc.extra['stop'].to_dict()
        return jsonify(body), status
    return jsonify({'message': 'Stop-payment placed', 'stop': stop.to_dict(), 'Freezes': service.snapshot(owner)}), 200


def handle_cancel_stop(service: FreezeService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    stop_id = str(values.get('stop_id') or '').strip()
    if not stop_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_stop_id'}), 400
    try:
        stop = service.cancel_stop(
            stop_id=stop_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
            owner_userid=userid,
        )
    except FreezeError as exc:
        status = {
            'stop_not_found': 404,
            'stop_forbidden': 403,
            'stop_not_active': 409,
        }.get(exc.code, 400)
        return jsonify({'message': exc.message, 'error': exc.code}), status
    return jsonify({'message': 'Stop-payment cancelled', 'stop': stop.to_dict(), 'Freezes': service.snapshot(stop.userid)}), 200


def handle_list_stops(service: FreezeService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    snapshot = service.snapshot(owner)
    return jsonify({'Stops': snapshot['stops'], 'Freezes': snapshot}), 200


def attach_freeze_routes(app, service: FreezeService, own_accounts_loader=None) -> None:
    @app.route('/freezeAccount', methods=['POST', 'GET'])
    def freeze_account_route():
        return handle_freeze_account(service, own_accounts_loader=own_accounts_loader)

    @app.route('/unfreezeAccount', methods=['POST', 'GET'])
    def unfreeze_account_route():
        return handle_unfreeze_account(service)

    @app.route('/listFreezes', methods=['POST', 'GET'])
    def list_freezes_route():
        return handle_list_freezes(service)

    @app.route('/stopPayment', methods=['POST', 'GET'])
    def stop_payment_route():
        return handle_stop_payment(service)

    @app.route('/cancelStopPayment', methods=['POST', 'GET'])
    def cancel_stop_route():
        return handle_cancel_stop(service)

    @app.route('/listStopPayments', methods=['POST', 'GET'])
    def list_stops_route():
        return handle_list_stops(service)
