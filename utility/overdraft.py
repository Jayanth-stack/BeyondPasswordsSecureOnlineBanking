"""Overdraft protection for checking and savings.

Checking/savings currently hard-fail at $0. Credit accounts keep their
separate credit-line check (hardcoded $5000 here; per-account lines are
PR #43). Distinct from:

- Credit-limit policy (revolving card line; checking/savings skip)
- Velocity (rolling-window dollar/count)
- Account freeze (outbound lock of any type)
- Dual-control approval thresholds

Available to spend on a deposit account is:

    balance + effective_overdraft - open_reservations

Default is unenrolled (limit $0), so existing NSF behaviour is preserved
until staff opts the account in or approves a customer request.

When a debit would take the account negative, an optional NSF fee is
added to the amount that must fit under available. A courtesy buffer
waives the fee for tiny overdrafts.

Stores are pluggable (memory for tests, sqlite WAL for the default).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from flask import jsonify, request, session

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
CHARGE_OPERATIONS = frozenset({'transfer', 'withdraw', 'cheque', 'approve'})
INBOUND_OPERATIONS = frozenset({'deposit', 'request'})
DEPOSIT_ACCOUNT_TYPES = frozenset({'checkin', 'checking', 'savings'})
REQUEST_STATUSES = frozenset({'pending', 'approved', 'denied'})
RESERVATION_OPEN = 'open'
RESERVATION_COMMITTED = 'committed'
RESERVATION_RELEASED = 'released'
MONEY_QUANTUM = Decimal('0.01')


class AccountError(ValueError):
    pass


class AmountError(ValueError):
    pass


class OverdraftError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


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


def parse_money(value: Any, *, allow_zero: bool = False) -> Decimal:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise AmountError('invalid_amount')
    try:
        amount = Decimal(str(value).strip().replace(',', '').replace('$', ''))
    except (InvalidOperation, ValueError):
        raise AmountError('invalid_amount') from None
    if not amount.is_finite():
        raise AmountError('invalid_amount')
    amount = amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    if amount < 0 or (amount == 0 and not allow_zero):
        raise AmountError('invalid_amount')
    return amount


def parse_balance(value: Any) -> Decimal:
    if value is None or (isinstance(value, str) and not str(value).strip()):
        return Decimal('0.00')
    try:
        amount = Decimal(str(value).strip().replace(',', '').replace('$', ''))
    except (InvalidOperation, ValueError):
        raise AmountError('invalid_amount') from None
    if not amount.is_finite():
        raise AmountError('invalid_amount')
    return amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def parse_signed_money(value: Any) -> Decimal:
    if value is None or (isinstance(value, str) and not str(value).strip()):
        raise AmountError('invalid_amount')
    try:
        amount = Decimal(str(value).strip().replace(',', '').replace('$', ''))
    except (InvalidOperation, ValueError):
        raise AmountError('invalid_amount') from None
    if not amount.is_finite() or amount == 0:
        raise AmountError('invalid_amount')
    return amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def money_str(value: Decimal) -> str:
    return str(value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN))


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return int(raw)


def _env_money(name: str, default: str) -> Decimal:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return Decimal(default)
    return parse_money(raw, allow_zero=True)


def _own_account_set(own_accounts: Optional[Iterable[Any]]) -> set:
    found = set()
    for item in own_accounts or ():
        try:
            found.add(normalize_account(item))
        except AccountError:
            continue
    return found


def deposit_accounts_from_customer_payload(accounts: Any) -> List[str]:
    found: List[str] = []
    if not isinstance(accounts, dict):
        return found
    for key in ('savings', 'checkin', 'checking'):
        item = accounts.get(key)
        if isinstance(item, dict) and item.get('Account') not in (None, 'None', ''):
            try:
                found.append(normalize_account(item['Account']))
            except AccountError:
                continue
    return found


def balances_from_customer_payload(accounts: Any) -> Dict[str, Decimal]:
    found: Dict[str, Decimal] = {}
    if not isinstance(accounts, dict):
        return found
    for key in ('savings', 'checkin', 'checking'):
        item = accounts.get(key)
        if isinstance(item, dict) and item.get('Account') not in (None, 'None', ''):
            try:
                found[normalize_account(item['Account'])] = parse_balance(item.get('Balance'))
            except (AccountError, AmountError):
                continue
    return found


def is_deposit_account_type(account_type: Any) -> bool:
    return str(account_type or '').strip().lower() in DEPOSIT_ACCOUNT_TYPES


@dataclass(frozen=True)
class OverdraftPolicy:
    default_limit: Decimal = Decimal('0.00')
    min_limit: Decimal = Decimal('50.00')
    max_limit: Decimal = Decimal('2500.00')
    fee: Decimal = Decimal('0.00')
    courtesy: Decimal = Decimal('5.00')
    request_max_increment: Decimal = Decimal('1000.00')
    max_pending_requests: int = 3
    reservation_ttl_seconds: int = 86400
    temp_increase_max_seconds: int = 90 * 24 * 3600
    max_temp_increase: Decimal = Decimal('500.00')

    @classmethod
    def from_env(cls) -> 'OverdraftPolicy':
        return cls(
            default_limit=_env_money('OVERDRAFT_DEFAULT', '0.00'),
            min_limit=_env_money('OVERDRAFT_MIN', '50.00'),
            max_limit=_env_money('OVERDRAFT_MAX', '2500.00'),
            fee=_env_money('OVERDRAFT_FEE', '0.00'),
            courtesy=_env_money('OVERDRAFT_COURTESY', '5.00'),
            request_max_increment=_env_money('OVERDRAFT_REQUEST_MAX', '1000.00'),
            max_pending_requests=_env_int('OVERDRAFT_MAX_PENDING', 3),
            reservation_ttl_seconds=_env_int('OVERDRAFT_RESERVE_TTL', 86400),
            temp_increase_max_seconds=_env_int('OVERDRAFT_TEMP_MAX_SECONDS', 90 * 24 * 3600),
            max_temp_increase=_env_money('OVERDRAFT_TEMP_MAX', '500.00'),
        )


@dataclass(frozen=True)
class OverdraftDecision:
    action: str
    reason: str = 'ok'
    available: Optional[Decimal] = None
    limit: Optional[Decimal] = None
    reserved: Optional[Decimal] = None
    fee: Optional[Decimal] = None
    enrolled: bool = False
    facility: Optional['OverdraftFacility'] = None

    @property
    def blocked(self) -> bool:
        return self.action == 'block'

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            'action': self.action,
            'reason': self.reason,
            'enrolled': self.enrolled,
        }
        if self.available is not None:
            payload['available'] = money_str(self.available)
        if self.limit is not None:
            payload['limit'] = money_str(self.limit)
        if self.reserved is not None:
            payload['reserved'] = money_str(self.reserved)
        if self.fee is not None:
            payload['fee'] = money_str(self.fee)
        if self.facility is not None:
            payload['facility'] = self.facility.to_dict()
        return payload


@dataclass
class OverdraftFacility:
    facility_id: str
    userid: str
    account: str
    base_limit: Decimal
    enabled: bool
    temp_increase: Decimal
    temp_expires_at: Optional[float]
    temp_reason: Optional[str]
    created_at: float
    updated_at: float
    updated_by: Optional[str]
    updated_by_type: Optional[str]

    def effective_limit(self, now: float) -> Decimal:
        if not self.enabled:
            return Decimal('0.00')
        extra = Decimal('0.00')
        if self.temp_increase > 0 and self.temp_expires_at and self.temp_expires_at > now:
            extra = self.temp_increase
        return self.base_limit + extra

    def to_dict(
        self,
        *,
        now: Optional[float] = None,
        balance: Optional[Decimal] = None,
        reserved: Optional[Decimal] = None,
        fee: Decimal = Decimal('0.00'),
        courtesy: Decimal = Decimal('0.00'),
    ) -> Dict[str, Any]:
        clock = time.time() if now is None else now
        limit = self.effective_limit(clock)
        held = reserved or Decimal('0.00')
        available = None
        utilized = Decimal('0.00')
        if balance is not None:
            available = balance + limit - held
            if balance < 0:
                utilized = -balance
        temp_active = bool(
            self.enabled and self.temp_increase > 0 and self.temp_expires_at and self.temp_expires_at > clock
        )
        return {
            'facility_id': self.facility_id,
            'userid': self.userid,
            'account': self.account,
            'enrolled': bool(self.enabled and limit > 0),
            'enabled': self.enabled,
            'base_limit': money_str(self.base_limit),
            'effective_limit': money_str(limit),
            'temp_increase': money_str(self.temp_increase) if temp_active else '0.00',
            'temp_expires_at': self.temp_expires_at if temp_active else None,
            'temp_reason': self.temp_reason if temp_active else None,
            'utilized': money_str(utilized),
            'reserved': money_str(held),
            'available': money_str(available) if available is not None else None,
            'balance': money_str(balance) if balance is not None else None,
            'fee': money_str(fee),
            'courtesy': money_str(courtesy),
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'updated_by': self.updated_by,
            'updated_by_type': self.updated_by_type,
        }


@dataclass
class OverdraftRequest:
    request_id: str
    userid: str
    account: str
    requested_limit: Decimal
    current_limit: Decimal
    reason: str
    status: str
    created_at: float
    decided_at: Optional[float] = None
    decided_by: Optional[str] = None
    decision_note: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'request_id': self.request_id,
            'userid': self.userid,
            'account': self.account,
            'requested_limit': money_str(self.requested_limit),
            'current_limit': money_str(self.current_limit),
            'reason': self.reason,
            'status': self.status,
            'created_at': self.created_at,
            'decided_at': self.decided_at,
            'decided_by': self.decided_by,
            'decision_note': self.decision_note,
        }


@dataclass
class Reservation:
    reservation_id: str
    userid: str
    account: str
    amount: Decimal
    operation: str
    status: str
    created_at: float
    expires_at: float
    closed_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'reservation_id': self.reservation_id,
            'userid': self.userid,
            'account': self.account,
            'amount': money_str(self.amount),
            'operation': self.operation,
            'status': self.status,
            'created_at': self.created_at,
            'expires_at': self.expires_at,
            'closed_at': self.closed_at,
        }


class MemoryOverdraftStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._facilities: Dict[str, OverdraftFacility] = {}
        self._by_account: Dict[str, str] = {}
        self._requests: Dict[str, OverdraftRequest] = {}
        self._reservations: Dict[str, Reservation] = {}

    def put_facility(self, facility: OverdraftFacility) -> None:
        with self._lock:
            self._facilities[facility.facility_id] = facility
            self._by_account[facility.account] = facility.facility_id

    def get_facility(self, facility_id: str) -> Optional[OverdraftFacility]:
        with self._lock:
            return self._facilities.get(facility_id)

    def get_by_account(self, account: str) -> Optional[OverdraftFacility]:
        with self._lock:
            facility_id = self._by_account.get(account)
            if not facility_id:
                return None
            return self._facilities.get(facility_id)

    def update_facility(self, facility: OverdraftFacility) -> None:
        self.put_facility(facility)

    def list_facilities(self, userid: Optional[str] = None) -> List[OverdraftFacility]:
        with self._lock:
            rows = list(self._facilities.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def put_request(self, item: OverdraftRequest) -> None:
        with self._lock:
            self._requests[item.request_id] = item

    def get_request(self, request_id: str) -> Optional[OverdraftRequest]:
        with self._lock:
            return self._requests.get(request_id)

    def update_request(self, item: OverdraftRequest) -> None:
        self.put_request(item)

    def list_requests(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[OverdraftRequest]:
        with self._lock:
            rows = list(self._requests.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def put_reservation(self, item: Reservation) -> None:
        with self._lock:
            self._reservations[item.reservation_id] = item

    def get_reservation(self, reservation_id: str) -> Optional[Reservation]:
        with self._lock:
            return self._reservations.get(reservation_id)

    def update_reservation(self, item: Reservation) -> None:
        self.put_reservation(item)

    def list_reservations(self, account: Optional[str] = None, status: Optional[str] = None) -> List[Reservation]:
        with self._lock:
            rows = list(self._reservations.values())
        if account is not None:
            rows = [row for row in rows if row.account == account]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        rows.sort(key=lambda item: item.created_at)
        return rows


class SqliteOverdraftStore:
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
        return conn

    def _init(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS facilities (
                    facility_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL UNIQUE,
                    base_limit TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    temp_increase TEXT NOT NULL,
                    temp_expires_at REAL,
                    temp_reason TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    updated_by TEXT,
                    updated_by_type TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS overdraft_requests (
                    request_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL,
                    requested_limit TEXT NOT NULL,
                    current_limit TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    decided_at REAL,
                    decided_by TEXT,
                    decision_note TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reservations (
                    reservation_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    closed_at REAL
                )
                """
            )
            conn.execute('CREATE INDEX IF NOT EXISTS idx_od_facilities_user ON facilities(userid)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_od_requests_user ON overdraft_requests(userid, status)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_od_reservations_acct ON reservations(account, status)')
            conn.commit()

    def put_facility(self, facility: OverdraftFacility) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO facilities (
                    facility_id, userid, account, base_limit, enabled, temp_increase,
                    temp_expires_at, temp_reason, created_at, updated_at,
                    updated_by, updated_by_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._facility_row(facility),
            )
            conn.commit()

    def get_facility(self, facility_id: str) -> Optional[OverdraftFacility]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM facilities WHERE facility_id = ?', (facility_id,)
            ).fetchone()
        return self._facility_from_row(row) if row else None

    def get_by_account(self, account: str) -> Optional[OverdraftFacility]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM facilities WHERE account = ?', (account,)
            ).fetchone()
        return self._facility_from_row(row) if row else None

    def update_facility(self, facility: OverdraftFacility) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE facilities SET
                    userid=?, account=?, base_limit=?, enabled=?, temp_increase=?,
                    temp_expires_at=?, temp_reason=?, updated_at=?,
                    updated_by=?, updated_by_type=?
                WHERE facility_id=?
                """,
                (
                    facility.userid, facility.account, money_str(facility.base_limit),
                    1 if facility.enabled else 0, money_str(facility.temp_increase),
                    facility.temp_expires_at, facility.temp_reason, facility.updated_at,
                    facility.updated_by, facility.updated_by_type, facility.facility_id,
                ),
            )
            conn.commit()

    def list_facilities(self, userid: Optional[str] = None) -> List[OverdraftFacility]:
        sql = 'SELECT * FROM facilities'
        params: Tuple[Any, ...] = ()
        if userid is not None:
            sql += ' WHERE userid = ?'
            params = (userid,)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._facility_from_row(row) for row in rows]

    def put_request(self, item: OverdraftRequest) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO overdraft_requests (
                    request_id, userid, account, requested_limit, current_limit,
                    reason, status, created_at, decided_at, decided_by, decision_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.request_id, item.userid, item.account,
                    money_str(item.requested_limit), money_str(item.current_limit),
                    item.reason, item.status, item.created_at, item.decided_at,
                    item.decided_by, item.decision_note,
                ),
            )
            conn.commit()

    def get_request(self, request_id: str) -> Optional[OverdraftRequest]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM overdraft_requests WHERE request_id = ?', (request_id,)
            ).fetchone()
        return self._request_from_row(row) if row else None

    def update_request(self, item: OverdraftRequest) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE overdraft_requests SET
                    requested_limit=?, current_limit=?, reason=?, status=?,
                    decided_at=?, decided_by=?, decision_note=?
                WHERE request_id=?
                """,
                (
                    money_str(item.requested_limit), money_str(item.current_limit),
                    item.reason, item.status, item.decided_at, item.decided_by,
                    item.decision_note, item.request_id,
                ),
            )
            conn.commit()

    def list_requests(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[OverdraftRequest]:
        clauses = []
        params: List[Any] = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if status is not None:
            clauses.append('status = ?')
            params.append(status)
        sql = 'SELECT * FROM overdraft_requests'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._request_from_row(row) for row in rows]

    def put_reservation(self, item: Reservation) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO reservations (
                    reservation_id, userid, account, amount, operation, status,
                    created_at, expires_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.reservation_id, item.userid, item.account, money_str(item.amount),
                    item.operation, item.status, item.created_at, item.expires_at,
                    item.closed_at,
                ),
            )
            conn.commit()

    def get_reservation(self, reservation_id: str) -> Optional[Reservation]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM reservations WHERE reservation_id = ?', (reservation_id,)
            ).fetchone()
        return self._reservation_from_row(row) if row else None

    def update_reservation(self, item: Reservation) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                'UPDATE reservations SET status=?, closed_at=? WHERE reservation_id=?',
                (item.status, item.closed_at, item.reservation_id),
            )
            conn.commit()

    def list_reservations(self, account: Optional[str] = None, status: Optional[str] = None) -> List[Reservation]:
        clauses = []
        params: List[Any] = []
        if account is not None:
            clauses.append('account = ?')
            params.append(account)
        if status is not None:
            clauses.append('status = ?')
            params.append(status)
        sql = 'SELECT * FROM reservations'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at ASC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._reservation_from_row(row) for row in rows]

    @staticmethod
    def _facility_row(facility: OverdraftFacility) -> Tuple[Any, ...]:
        return (
            facility.facility_id, facility.userid, facility.account,
            money_str(facility.base_limit), 1 if facility.enabled else 0,
            money_str(facility.temp_increase), facility.temp_expires_at,
            facility.temp_reason, facility.created_at, facility.updated_at,
            facility.updated_by, facility.updated_by_type,
        )

    @staticmethod
    def _facility_from_row(row: sqlite3.Row) -> OverdraftFacility:
        return OverdraftFacility(
            facility_id=row['facility_id'],
            userid=row['userid'],
            account=row['account'],
            base_limit=Decimal(row['base_limit']),
            enabled=bool(row['enabled']),
            temp_increase=Decimal(row['temp_increase']),
            temp_expires_at=row['temp_expires_at'],
            temp_reason=row['temp_reason'],
            created_at=row['created_at'],
            updated_at=row['updated_at'],
            updated_by=row['updated_by'],
            updated_by_type=row['updated_by_type'],
        )

    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> OverdraftRequest:
        return OverdraftRequest(
            request_id=row['request_id'],
            userid=row['userid'],
            account=row['account'],
            requested_limit=Decimal(row['requested_limit']),
            current_limit=Decimal(row['current_limit']),
            reason=row['reason'],
            status=row['status'],
            created_at=row['created_at'],
            decided_at=row['decided_at'],
            decided_by=row['decided_by'],
            decision_note=row['decision_note'],
        )

    @staticmethod
    def _reservation_from_row(row: sqlite3.Row) -> Reservation:
        return Reservation(
            reservation_id=row['reservation_id'],
            userid=row['userid'],
            account=row['account'],
            amount=Decimal(row['amount']),
            operation=row['operation'],
            status=row['status'],
            created_at=row['created_at'],
            expires_at=row['expires_at'],
            closed_at=row['closed_at'],
        )


class OverdraftService:
    def __init__(
        self,
        policy: Optional[OverdraftPolicy] = None,
        store: Optional[Any] = None,
        clock: Any = time.time,
        is_deposit_loader: Optional[Callable[[str, Optional[str]], bool]] = None,
        balance_loader: Optional[Callable[[str], Optional[Tuple[str, Decimal]]]] = None,
    ) -> None:
        self.policy = policy or OverdraftPolicy()
        self.store = store or MemoryOverdraftStore()
        self.clock = clock
        self.is_deposit_loader = is_deposit_loader
        self.balance_loader = balance_loader

    def _actor_is_employee(self, actor_type: str) -> bool:
        return (actor_type or '') in EMPLOYEE_ROLES

    def _assert_ownership(
        self,
        *,
        account: str,
        actor_type: str,
        own_accounts: Optional[Iterable[Any]],
    ) -> None:
        if self._actor_is_employee(actor_type) or own_accounts is None:
            return
        if account not in _own_account_set(own_accounts):
            raise OverdraftError('overdraft_forbidden', 'Overdraft facility does not belong to this customer.')

    def _assert_staff(self, actor_type: str) -> None:
        if not self._actor_is_employee(actor_type):
            raise OverdraftError('overdraft_forbidden', 'Only staff can change an overdraft limit.')

    def is_deposit_account(self, account: str, userid: Optional[str] = None, account_type: Optional[str] = None) -> bool:
        if is_deposit_account_type(account_type):
            return True
        if str(account_type or '').strip().lower() == 'credit':
            return False
        if self.store.get_by_account(account) is not None:
            return True
        if callable(self.is_deposit_loader):
            try:
                return bool(self.is_deposit_loader(account, userid))
            except Exception:
                return False
        return False

    def _load_balance(self, account: str) -> Optional[Tuple[str, Decimal]]:
        if not callable(self.balance_loader):
            return None
        try:
            result = self.balance_loader(account)
        except Exception:
            return None
        if not result:
            return None
        account_type, balance = result
        if isinstance(balance, Decimal):
            return str(account_type or ''), balance
        try:
            return str(account_type or ''), parse_balance(balance)
        except AmountError:
            return None

    def ensure_facility(self, *, userid: str, account: Any, enabled: bool = False) -> OverdraftFacility:
        account_text = normalize_account(account)
        existing = self.store.get_by_account(account_text)
        if existing is not None:
            return existing
        now = float(self.clock())
        facility = OverdraftFacility(
            facility_id=uuid.uuid4().hex,
            userid=str(userid),
            account=account_text,
            base_limit=self.policy.default_limit,
            enabled=bool(enabled and self.policy.default_limit > 0),
            temp_increase=Decimal('0.00'),
            temp_expires_at=None,
            temp_reason=None,
            created_at=now,
            updated_at=now,
            updated_by=None,
            updated_by_type=None,
        )
        self.store.put_facility(facility)
        return facility

    def _validate_limit(self, amount: Decimal, *, allow_zero: bool = False) -> Decimal:
        if allow_zero and amount == 0:
            return amount
        if amount < self.policy.min_limit or amount > self.policy.max_limit:
            raise OverdraftError(
                'limit_out_of_range',
                'Overdraft limit is outside the allowed range.',
                min=money_str(self.policy.min_limit),
                max=money_str(self.policy.max_limit),
            )
        return amount

    def set_limit(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        limit: Any,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> OverdraftFacility:
        self._assert_staff(actor_type)
        account_text = normalize_account(account)
        self._assert_ownership(account=account_text, actor_type=actor_type, own_accounts=own_accounts)
        amount = self._validate_limit(parse_money(limit, allow_zero=True), allow_zero=True)
        facility = self.ensure_facility(userid=owner_userid, account=account_text)
        now = float(self.clock())
        facility.base_limit = amount
        facility.enabled = amount > 0
        if not facility.enabled:
            facility.temp_increase = Decimal('0.00')
            facility.temp_expires_at = None
            facility.temp_reason = None
        facility.updated_at = now
        facility.updated_by = actor
        facility.updated_by_type = actor_type
        self.store.update_facility(facility)
        return facility

    def adjust_limit(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        delta: Any,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> OverdraftFacility:
        account_text = normalize_account(account)
        facility = self.ensure_facility(userid=owner_userid, account=account_text)
        change = parse_signed_money(delta)
        new_limit = facility.base_limit + change
        if new_limit < 0:
            new_limit = Decimal('0.00')
        return self.set_limit(
            owner_userid=owner_userid,
            actor=actor,
            actor_type=actor_type,
            account=account_text,
            limit=new_limit,
            own_accounts=own_accounts,
        )

    def revoke(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> OverdraftFacility:
        return self.set_limit(
            owner_userid=owner_userid,
            actor=actor,
            actor_type=actor_type,
            account=account,
            limit=Decimal('0.00'),
            own_accounts=own_accounts,
        )

    def grant_temp_increase(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        amount: Any,
        seconds: Any = None,
        reason: Any = None,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> OverdraftFacility:
        self._assert_staff(actor_type)
        account_text = normalize_account(account)
        self._assert_ownership(account=account_text, actor_type=actor_type, own_accounts=own_accounts)
        bump = parse_money(amount)
        if bump > self.policy.max_temp_increase:
            raise OverdraftError(
                'temp_increase_too_large',
                'Temporary overdraft increase exceeds the allowed maximum.',
                max=money_str(self.policy.max_temp_increase),
            )
        ttl = int(seconds) if seconds not in (None, '') else 30 * 24 * 3600
        if ttl <= 0 or ttl > self.policy.temp_increase_max_seconds:
            raise OverdraftError('invalid_ttl', 'Temporary overdraft duration is invalid.')
        facility = self.ensure_facility(userid=owner_userid, account=account_text)
        if not facility.enabled or facility.base_limit <= 0:
            raise OverdraftError('not_enrolled', 'Account is not enrolled in overdraft protection.')
        now = float(self.clock())
        combined = facility.effective_limit(now) + bump
        if combined > self.policy.max_limit:
            raise OverdraftError(
                'limit_out_of_range',
                'Temporary increase would exceed the maximum overdraft.',
                max=money_str(self.policy.max_limit),
            )
        facility.temp_increase = bump
        facility.temp_expires_at = now + ttl
        facility.temp_reason = str(reason or 'temporary_increase')[:120]
        facility.updated_at = now
        facility.updated_by = actor
        facility.updated_by_type = actor_type
        self.store.update_facility(facility)
        return facility

    def revoke_temp_increase(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> OverdraftFacility:
        self._assert_staff(actor_type)
        account_text = normalize_account(account)
        self._assert_ownership(account=account_text, actor_type=actor_type, own_accounts=own_accounts)
        facility = self.ensure_facility(userid=owner_userid, account=account_text)
        now = float(self.clock())
        facility.temp_increase = Decimal('0.00')
        facility.temp_expires_at = None
        facility.temp_reason = None
        facility.updated_at = now
        facility.updated_by = actor
        facility.updated_by_type = actor_type
        self.store.update_facility(facility)
        return facility

    def request_increase(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        requested_limit: Any,
        reason: Any = None,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> OverdraftRequest:
        if self._actor_is_employee(actor_type):
            raise OverdraftError('overdraft_forbidden', 'Staff should set the overdraft directly.')
        account_text = normalize_account(account)
        self._assert_ownership(account=account_text, actor_type=actor_type, own_accounts=own_accounts)
        target = self._validate_limit(parse_money(requested_limit))
        facility = self.ensure_facility(userid=owner_userid, account=account_text)
        now = float(self.clock())
        current = facility.effective_limit(now)
        if target <= current:
            raise OverdraftError('limit_not_increase', 'Requested overdraft must be higher than the current limit.')
        increment = target - facility.base_limit
        if increment > self.policy.request_max_increment:
            raise OverdraftError(
                'request_too_large',
                'Requested overdraft increase exceeds the allowed increment.',
                max=money_str(self.policy.request_max_increment),
            )
        pending = self.store.list_requests(userid=owner_userid, status='pending')
        pending = [item for item in pending if item.account == account_text]
        if len(pending) >= self.policy.max_pending_requests:
            raise OverdraftError('request_limit', 'Too many pending overdraft requests.')
        for item in pending:
            if item.requested_limit == target:
                raise OverdraftError('request_duplicate', 'A matching overdraft request is already pending.')
        req = OverdraftRequest(
            request_id=uuid.uuid4().hex,
            userid=str(owner_userid),
            account=account_text,
            requested_limit=target,
            current_limit=current,
            reason=str(reason or 'customer_request')[:160],
            status='pending',
            created_at=now,
        )
        self.store.put_request(req)
        return req

    def decide_request(
        self,
        *,
        actor: str,
        actor_type: str,
        request_id: str,
        approve: bool,
        note: Any = None,
    ) -> OverdraftRequest:
        self._assert_staff(actor_type)
        req = self.store.get_request(str(request_id or '').strip())
        if req is None:
            raise OverdraftError('request_not_found', 'Overdraft request was not found.')
        if req.status != 'pending':
            raise OverdraftError('request_not_pending', 'Overdraft request is no longer pending.')
        now = float(self.clock())
        req.decided_at = now
        req.decided_by = actor
        req.decision_note = str(note or ('approved' if approve else 'denied'))[:160]
        req.status = 'approved' if approve else 'denied'
        if approve:
            self.set_limit(
                owner_userid=req.userid,
                actor=actor,
                actor_type=actor_type,
                account=req.account,
                limit=req.requested_limit,
            )
        self.store.update_request(req)
        return req

    def _expire_reservations(self, account: str) -> None:
        now = float(self.clock())
        for item in self.store.list_reservations(account=account, status=RESERVATION_OPEN):
            if item.expires_at <= now:
                item.status = RESERVATION_RELEASED
                item.closed_at = now
                self.store.update_reservation(item)

    def open_reserved(self, account: str) -> Decimal:
        self._expire_reservations(account)
        now = float(self.clock())
        total = Decimal('0.00')
        for item in self.store.list_reservations(account=account, status=RESERVATION_OPEN):
            if item.expires_at > now:
                total += item.amount
        return total

    def _fee_for(self, *, amount: Decimal, balance: Decimal, reserved: Decimal) -> Decimal:
        projected = balance - reserved - amount
        if projected >= 0:
            return Decimal('0.00')
        used = -projected
        if used <= self.policy.courtesy:
            return Decimal('0.00')
        return self.policy.fee

    def evaluate(
        self,
        *,
        operation: str,
        account: Any = None,
        amount: Any = None,
        balance: Any = None,
        account_type: Optional[str] = None,
        userid: Optional[str] = None,
    ) -> OverdraftDecision:
        op = str(operation or '').strip().lower()
        if op in INBOUND_OPERATIONS or op not in CHARGE_OPERATIONS:
            return OverdraftDecision(action='allow', reason='not_gated')
        if account in (None, ''):
            return OverdraftDecision(action='allow', reason='no_account')
        try:
            account_text = normalize_account(account)
        except AccountError:
            return OverdraftDecision(action='block', reason='invalid_account')

        loaded_type = None
        loaded_balance = None
        loaded = self._load_balance(account_text)
        if loaded is not None:
            loaded_type, loaded_balance = loaded
        kind = (account_type or loaded_type or '').strip().lower()
        if kind == 'credit' or not self.is_deposit_account(account_text, userid, kind):
            return OverdraftDecision(action='allow', reason='not_deposit_account')

        if balance is None:
            balance = loaded_balance
        if balance is None:
            return OverdraftDecision(action='allow', reason='balance_unknown')

        try:
            charge = parse_money(amount)
            bal = parse_balance(balance)
        except AmountError:
            return OverdraftDecision(action='block', reason='invalid_amount')

        facility = self.store.get_by_account(account_text)
        now = float(self.clock())
        limit = facility.effective_limit(now) if facility is not None else Decimal('0.00')
        reserved = self.open_reserved(account_text)
        fee = self._fee_for(amount=charge, balance=bal, reserved=reserved)
        available = bal + limit - reserved
        needed = charge + fee
        enrolled = bool(facility is not None and facility.enabled and limit > 0)
        if needed > available:
            return OverdraftDecision(
                action='block',
                reason='overdraft_exceeded',
                available=available,
                limit=limit,
                reserved=reserved,
                fee=fee,
                enrolled=enrolled,
                facility=facility,
            )
        return OverdraftDecision(
            action='allow',
            reason='ok',
            available=available,
            limit=limit,
            reserved=reserved,
            fee=fee,
            enrolled=enrolled,
            facility=facility,
        )

    def reserve(
        self,
        *,
        owner_userid: str,
        account: Any,
        amount: Any,
        operation: str,
        balance: Any = None,
        account_type: Optional[str] = None,
    ) -> Optional[Reservation]:
        decision = self.evaluate(
            operation=operation,
            account=account,
            amount=amount,
            balance=balance,
            account_type=account_type,
            userid=owner_userid,
        )
        if decision.reason in {'not_gated', 'not_deposit_account', 'no_account', 'balance_unknown'}:
            return None
        if decision.blocked:
            raise OverdraftError(
                decision.reason,
                'Debit would exceed available funds including overdraft.',
                available=money_str(decision.available) if decision.available is not None else None,
                limit=money_str(decision.limit) if decision.limit is not None else None,
                fee=money_str(decision.fee) if decision.fee is not None else None,
            )
        account_text = normalize_account(account)
        now = float(self.clock())
        item = Reservation(
            reservation_id=uuid.uuid4().hex,
            userid=str(owner_userid),
            account=account_text,
            amount=parse_money(amount),
            operation=str(operation or 'transfer'),
            status=RESERVATION_OPEN,
            created_at=now,
            expires_at=now + self.policy.reservation_ttl_seconds,
        )
        self.store.put_reservation(item)
        return item

    def _close_matching(
        self,
        *,
        account: Any,
        amount: Any,
        new_status: str,
        operation: Optional[str] = None,
    ) -> Optional[Reservation]:
        try:
            account_text = normalize_account(account)
            target = parse_money(amount)
        except (AccountError, AmountError):
            return None
        self._expire_reservations(account_text)
        now = float(self.clock())
        for item in self.store.list_reservations(account=account_text, status=RESERVATION_OPEN):
            if item.amount != target:
                continue
            if operation and item.operation != operation:
                continue
            item.status = new_status
            item.closed_at = now
            self.store.update_reservation(item)
            return item
        return None

    def capture_matching(self, account: Any, amount: Any, operation: Optional[str] = None) -> Optional[Reservation]:
        return self._close_matching(
            account=account, amount=amount, new_status=RESERVATION_COMMITTED, operation=operation
        )

    def void_matching(self, account: Any, amount: Any, operation: Optional[str] = None) -> Optional[Reservation]:
        return self._close_matching(
            account=account, amount=amount, new_status=RESERVATION_RELEASED, operation=operation
        )

    def snapshot(
        self,
        userid: str,
        balances: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = float(self.clock())
        parsed_balances: Dict[str, Decimal] = {}
        for key, value in (balances or {}).items():
            try:
                parsed_balances[normalize_account(key)] = parse_balance(value)
            except (AccountError, AmountError):
                continue
        facilities = []
        seen = set()
        for facility in self.store.list_facilities(userid=userid):
            reserved = self.open_reserved(facility.account)
            facilities.append(facility.to_dict(
                now=now,
                balance=parsed_balances.get(facility.account),
                reserved=reserved,
                fee=self.policy.fee,
                courtesy=self.policy.courtesy,
            ))
            seen.add(facility.account)
        for account, balance in parsed_balances.items():
            if account in seen:
                continue
            reserved = self.open_reserved(account)
            available = balance - reserved
            facilities.append({
                'facility_id': None,
                'userid': userid,
                'account': account,
                'enrolled': False,
                'enabled': False,
                'base_limit': '0.00',
                'effective_limit': '0.00',
                'temp_increase': '0.00',
                'temp_expires_at': None,
                'temp_reason': None,
                'utilized': '0.00',
                'reserved': money_str(reserved),
                'available': money_str(available),
                'balance': money_str(balance),
                'fee': money_str(self.policy.fee),
                'courtesy': money_str(self.policy.courtesy),
                'created_at': None,
                'updated_at': None,
                'updated_by': None,
                'updated_by_type': None,
            })
        requests = [item.to_dict() for item in self.store.list_requests(userid=userid)]
        return {
            'policy': {
                'default_limit': money_str(self.policy.default_limit),
                'min_limit': money_str(self.policy.min_limit),
                'max_limit': money_str(self.policy.max_limit),
                'fee': money_str(self.policy.fee),
                'courtesy': money_str(self.policy.courtesy),
            },
            'facilities': facilities,
            'requests': requests,
        }


_DEFAULT_SERVICE: Optional[OverdraftService] = None


def default_store() -> Any:
    path = os.environ.get('OVERDRAFT_STORE') or os.path.join('SystemLogs', 'overdraft.sqlite')
    return SqliteOverdraftStore(path)


def build_service(
    is_deposit_loader=None,
    balance_loader=None,
    store=None,
    policy=None,
) -> OverdraftService:
    return OverdraftService(
        policy or OverdraftPolicy.from_env(),
        store or default_store(),
        is_deposit_loader=is_deposit_loader,
        balance_loader=balance_loader,
    )


def set_service(service: Optional[OverdraftService]) -> None:
    global _DEFAULT_SERVICE
    _DEFAULT_SERVICE = service


def get_service() -> OverdraftService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = build_service()
    return _DEFAULT_SERVICE


def debit_allowed(
    account: Any,
    amount: Any,
    balance: Any,
    account_type: Any = 'checkin',
) -> bool:
    """Used by the money handler. True if the debit may proceed.

    Credit accounts skip (existing $5000 ceiling stays). Checking/savings
    fall back to the historical `amount <= balance` NSF rule if the store
    cannot run.
    """
    kind = str(account_type or '').strip().lower()
    if kind == 'credit':
        return True
    try:
        service = get_service()
        service.capture_matching(account, amount)
        decision = service.evaluate(
            operation='approve',
            account=account,
            amount=amount,
            balance=balance,
            account_type=kind or 'checkin',
        )
        if decision.blocked:
            return False
        return True
    except Exception:
        try:
            return parse_balance(balance) >= parse_money(amount)
        except Exception:
            return False


def _blocked_payload(decision: OverdraftDecision, operation: str) -> Dict[str, Any]:
    messages = {
        'overdraft_exceeded': 'Debit would exceed available funds including overdraft.',
        'invalid_amount': 'Enter a valid amount.',
        'invalid_account': 'Invalid account.',
    }
    payload = {
        'message': messages.get(decision.reason, 'Overdraft check blocked this debit.'),
        'error': decision.reason,
        'operation': operation,
    }
    payload.update({k: v for k, v in decision.to_dict().items() if k not in {'action', 'facility'}})
    if decision.facility is not None:
        payload['facility'] = decision.facility.to_dict()
    return payload


def enforce_overdraft(
    service: OverdraftService,
    *,
    operation: str,
    account: Any = None,
    amount: Any = None,
    balance: Any = None,
    account_type: Optional[str] = None,
    userid: Optional[str] = None,
    reserve: bool = False,
) -> Optional[Tuple[Dict[str, Any], int]]:
    """Return (body, status) to short-circuit, or None to run the existing handler."""
    if account not in (None, ''):
        try:
            normalize_account(account, required=True)
        except AccountError:
            return {'message': 'Invalid account', 'error': 'invalid_account'}, 400
    if amount not in (None, '') and str(operation or '') in CHARGE_OPERATIONS:
        try:
            parse_money(amount)
        except AmountError:
            return {'message': 'Enter a valid amount', 'error': 'invalid_amount'}, 400

    if reserve:
        try:
            service.reserve(
                owner_userid=str(userid or ''),
                account=account,
                amount=amount,
                operation=operation,
                balance=balance,
                account_type=account_type,
            )
        except OverdraftError as exc:
            status = 403 if exc.code == 'overdraft_exceeded' else 400
            body = {'message': exc.message, 'error': exc.code, 'operation': operation}
            body.update({k: v for k, v in exc.extra.items() if v is not None})
            return body, status
        except AccountError:
            return {'message': 'Invalid account', 'error': 'invalid_account'}, 400
        except AmountError:
            return {'message': 'Enter a valid amount', 'error': 'invalid_amount'}, 400
        return None

    decision = service.evaluate(
        operation=operation,
        account=account,
        amount=amount,
        balance=balance,
        account_type=account_type,
        userid=userid,
    )
    if not decision.blocked:
        return None
    status = 400 if decision.reason in {'invalid_amount', 'invalid_account'} else 403
    return _blocked_payload(decision, operation), status


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


def _error_status(code: str) -> int:
    return {
        'request_duplicate': 409,
        'request_limit': 409,
        'request_not_pending': 409,
        'overdraft_forbidden': 403,
        'overdraft_exceeded': 403,
        'not_enrolled': 409,
        'request_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_ttl': 400,
        'limit_out_of_range': 400,
        'limit_not_increase': 400,
        'request_too_large': 400,
        'temp_increase_too_large': 400,
        'missing_customer_id': 400,
    }.get(code, 400)


def _error_body(exc: OverdraftError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    for key, value in exc.extra.items():
        if value is not None:
            body[key] = value
    return body


def _own_accounts(loader, userid, actor_type):
    if actor_type == 'customer' and callable(loader):
        return loader(userid)
    return None


def handle_set_limit(service: OverdraftService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    try:
        facility = service.set_limit(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            limit=values.get('limit'),
            own_accounts=_own_accounts(own_accounts_loader, userid, actor_type),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Enter a valid amount', 'error': 'invalid_amount'}), 400
    except OverdraftError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Overdraft limit updated',
        'facility': facility.to_dict(now=float(service.clock()), fee=service.policy.fee, courtesy=service.policy.courtesy),
        'Overdrafts': service.snapshot(owner),
    }), 200


def handle_adjust_limit(service: OverdraftService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    try:
        facility = service.adjust_limit(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            delta=values.get('delta'),
            own_accounts=_own_accounts(own_accounts_loader, userid, actor_type),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Enter a valid amount', 'error': 'invalid_amount'}), 400
    except OverdraftError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Overdraft limit adjusted',
        'facility': facility.to_dict(now=float(service.clock()), fee=service.policy.fee, courtesy=service.policy.courtesy),
        'Overdrafts': service.snapshot(owner),
    }), 200


def handle_revoke(service: OverdraftService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    try:
        facility = service.revoke(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            own_accounts=_own_accounts(own_accounts_loader, userid, actor_type),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except OverdraftError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Overdraft protection revoked',
        'facility': facility.to_dict(now=float(service.clock()), fee=service.policy.fee, courtesy=service.policy.courtesy),
        'Overdrafts': service.snapshot(owner),
    }), 200


def handle_temp_increase(service: OverdraftService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    try:
        facility = service.grant_temp_increase(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            amount=values.get('amount'),
            seconds=values.get('seconds'),
            reason=values.get('reason'),
            own_accounts=_own_accounts(own_accounts_loader, userid, actor_type),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Enter a valid amount', 'error': 'invalid_amount'}), 400
    except OverdraftError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Temporary overdraft granted',
        'facility': facility.to_dict(now=float(service.clock()), fee=service.policy.fee, courtesy=service.policy.courtesy),
        'Overdrafts': service.snapshot(owner),
    }), 200


def handle_revoke_temp(service: OverdraftService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    try:
        facility = service.revoke_temp_increase(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            own_accounts=_own_accounts(own_accounts_loader, userid, actor_type),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except OverdraftError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Temporary overdraft revoked',
        'facility': facility.to_dict(now=float(service.clock()), fee=service.policy.fee, courtesy=service.policy.courtesy),
        'Overdrafts': service.snapshot(owner),
    }), 200


def handle_request_increase(service: OverdraftService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values) or userid
    try:
        req = service.request_increase(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            requested_limit=values.get('requested_limit') or values.get('limit'),
            reason=values.get('reason'),
            own_accounts=_own_accounts(own_accounts_loader, userid, actor_type),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Enter a valid amount', 'error': 'invalid_amount'}), 400
    except OverdraftError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Overdraft protection requested',
        'request': req.to_dict(),
        'Overdrafts': service.snapshot(owner),
    }), 201


def handle_decide_request(service: OverdraftService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    decision = str(values.get('decision') or '').strip().lower()
    approve = decision in {'approve', 'approved', 'yes', '1', 'true'}
    deny = decision in {'deny', 'denied', 'no', '0', 'false'}
    if not approve and not deny:
        return jsonify({'message': 'Decision must be approve or deny', 'error': 'invalid_decision'}), 400
    try:
        req = service.decide_request(
            actor=userid,
            actor_type=actor_type,
            request_id=str(values.get('request_id') or ''),
            approve=approve,
            note=values.get('note'),
        )
    except OverdraftError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Overdraft request ' + req.status,
        'request': req.to_dict(),
        'Overdrafts': service.snapshot(req.userid),
    }), 200


def handle_list(service: OverdraftService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Overdrafts': service.snapshot(owner)}), 200


def attach_overdraft_routes(app, service: OverdraftService, own_accounts_loader=None) -> None:
    @app.route('/setOverdraft', methods=['POST', 'GET'])
    def set_overdraft_route():
        return handle_set_limit(service, own_accounts_loader=own_accounts_loader)

    @app.route('/adjustOverdraft', methods=['POST', 'GET'])
    def adjust_overdraft_route():
        return handle_adjust_limit(service, own_accounts_loader=own_accounts_loader)

    @app.route('/revokeOverdraft', methods=['POST', 'GET'])
    def revoke_overdraft_route():
        return handle_revoke(service, own_accounts_loader=own_accounts_loader)

    @app.route('/grantTempOverdraft', methods=['POST', 'GET'])
    def grant_temp_overdraft_route():
        return handle_temp_increase(service, own_accounts_loader=own_accounts_loader)

    @app.route('/revokeTempOverdraft', methods=['POST', 'GET'])
    def revoke_temp_overdraft_route():
        return handle_revoke_temp(service, own_accounts_loader=own_accounts_loader)

    @app.route('/requestOverdraft', methods=['POST', 'GET'])
    def request_overdraft_route():
        return handle_request_increase(service, own_accounts_loader=own_accounts_loader)

    @app.route('/decideOverdraftRequest', methods=['POST', 'GET'])
    def decide_overdraft_route():
        return handle_decide_request(service)

    @app.route('/listOverdrafts', methods=['POST', 'GET'])
    def list_overdrafts_route():
        return handle_list(service)
