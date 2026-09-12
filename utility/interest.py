"""Interest / APY posting for savings accounts.

Savings currently never earn yield. Checking is a demand product and
credit is a charge product (PR #43 / hardcoded $5000). Distinct from:

- Overdraft (PR #46) — debit/NSF facility, not yield
- Scheduled transfers (PR #36) — outbound calendar instructions
- Credit-limit (PR #43) — revolving card ceiling
- Velocity (PR #20) — rolling-window outbound caps

Daily-balance accrual:

    accrued += last_observed_balance * (effective_apy / 100) * days / 365

Monthly posting of accrued cents, idempotent by occurrence id
``{account}:{yyyy-mm}`` for the completed period. Catch-up is ``one``:
downtime collapses into a single credit, not one per missed month.

Default APY is 0% (unenrolled) so existing balances never change until
staff sets a rate or approves a customer request.

Stores are pluggable (memory for tests, sqlite WAL for the default).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from flask import jsonify, request, session

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
SAVINGS_ACCOUNT_TYPES = frozenset({'savings'})
NON_YIELD_ACCOUNT_TYPES = frozenset({'checkin', 'checking', 'credit'})
REQUEST_STATUSES = frozenset({'pending', 'approved', 'denied'})
POST_STATUSES = frozenset({'posted', 'skipped', 'failed'})
MONEY_QUANTUM = Decimal('0.01')
RATE_QUANTUM = Decimal('0.01')
ACCRUAL_QUANTUM = Decimal('0.000001')
SECONDS_PER_DAY = 86400
DEFAULT_STORE_PATH = os.path.join('SystemLogs', 'interest.sqlite')

_SERVICE: Optional['InterestService'] = None


class AccountError(ValueError):
    pass


class AmountError(ValueError):
    pass


class RateError(ValueError):
    pass


class InterestError(ValueError):
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


def parse_apy(value: Any, *, allow_zero: bool = False) -> Decimal:
    if value is None or (isinstance(value, str) and not str(value).strip()):
        raise RateError('invalid_apy')
    try:
        rate = Decimal(str(value).strip().replace(',', '').replace('%', ''))
    except (InvalidOperation, ValueError):
        raise RateError('invalid_apy') from None
    if not rate.is_finite():
        raise RateError('invalid_apy')
    rate = rate.quantize(RATE_QUANTUM, rounding=ROUND_HALF_EVEN)
    if rate < 0 or (rate == 0 and not allow_zero):
        raise RateError('invalid_apy')
    return rate


def money_str(value: Decimal) -> str:
    return str(value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN))


def rate_str(value: Decimal) -> str:
    return str(value.quantize(RATE_QUANTUM, rounding=ROUND_HALF_EVEN))


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


def _env_apy(name: str, default: str) -> Decimal:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return Decimal(default)
    return parse_apy(raw, allow_zero=True)


def _own_account_set(own_accounts: Optional[Iterable[Any]]) -> set:
    found = set()
    for item in own_accounts or ():
        try:
            found.add(normalize_account(item))
        except AccountError:
            continue
    return found


def savings_accounts_from_customer_payload(accounts: Any) -> List[str]:
    found: List[str] = []
    if not isinstance(accounts, dict):
        return found
    item = accounts.get('savings')
    if isinstance(item, dict) and item.get('Account') not in (None, 'None', ''):
        try:
            found.append(normalize_account(item['Account']))
        except AccountError:
            return found
    return found


def balances_from_customer_payload(accounts: Any) -> Dict[str, Decimal]:
    found: Dict[str, Decimal] = {}
    if not isinstance(accounts, dict):
        return found
    item = accounts.get('savings')
    if isinstance(item, dict) and item.get('Account') not in (None, 'None', ''):
        try:
            found[normalize_account(item['Account'])] = parse_balance(item.get('Balance'))
        except (AccountError, AmountError):
            return found
    return found


def is_savings_account_type(account_type: Any) -> bool:
    return str(account_type or '').strip().lower() in SAVINGS_ACCOUNT_TYPES


def utc_datetime(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def period_id(ts: float) -> str:
    dt = utc_datetime(ts)
    return f'{dt.year:04d}-{dt.month:02d}'


def previous_period(ts: float) -> str:
    dt = utc_datetime(ts)
    if dt.month == 1:
        return f'{dt.year - 1:04d}-12'
    return f'{dt.year:04d}-{dt.month - 1:02d}'


def period_start_ts(period: str) -> float:
    year, month = (int(part) for part in period.split('-', 1))
    return datetime(year, month, 1, tzinfo=timezone.utc).timestamp()


def next_period(period: str) -> str:
    year, month = (int(part) for part in period.split('-', 1))
    if month == 12:
        return f'{year + 1:04d}-01'
    return f'{year:04d}-{month + 1:02d}'


def days_between(start: float, end: float) -> int:
    if end <= start:
        return 0
    return int((end - start) // SECONDS_PER_DAY)


def whole_days_advance(start: float, days: int) -> float:
    return start + (days * SECONDS_PER_DAY)


@dataclass(frozen=True)
class InterestPolicy:
    default_apy: Decimal = Decimal('0.00')
    min_apy: Decimal = Decimal('0.01')
    max_apy: Decimal = Decimal('10.00')
    days_in_year: int = 365
    min_post: Decimal = Decimal('0.01')
    request_max_increment: Decimal = Decimal('2.00')
    max_pending_requests: int = 3
    promo_max: Decimal = Decimal('3.00')
    promo_max_seconds: int = 90 * 24 * 3600
    catch_up: str = 'one'

    @classmethod
    def from_env(cls) -> 'InterestPolicy':
        return cls(
            default_apy=_env_apy('INTEREST_DEFAULT_APY', '0.00'),
            min_apy=_env_apy('INTEREST_MIN_APY', '0.01'),
            max_apy=_env_apy('INTEREST_MAX_APY', '10.00'),
            days_in_year=_env_int('INTEREST_DAYS_IN_YEAR', 365),
            min_post=_env_money('INTEREST_MIN_POST', '0.01'),
            request_max_increment=_env_apy('INTEREST_REQUEST_MAX', '2.00'),
            max_pending_requests=_env_int('INTEREST_MAX_PENDING', 3),
            promo_max=_env_apy('INTEREST_PROMO_MAX', '3.00'),
            promo_max_seconds=_env_int('INTEREST_PROMO_MAX_SECONDS', 90 * 24 * 3600),
            catch_up=(os.environ.get('INTEREST_CATCH_UP') or 'one').strip().lower() or 'one',
        )


@dataclass
class InterestAccount:
    account_id: str
    userid: str
    account: str
    base_apy: Decimal
    enabled: bool
    accrued: Decimal = Decimal('0')
    last_balance: Decimal = Decimal('0.00')
    last_accrual_at: float = 0.0
    opened_at: float = 0.0
    last_posted_period: str = ''
    last_posted_at: float = 0.0
    ytd_posted: Decimal = Decimal('0.00')
    ytd_year: int = 0
    promo_apy: Decimal = Decimal('0.00')
    promo_until: float = 0.0
    promo_reason: str = ''

    def effective_apy(self, now: float) -> Decimal:
        extra = self.promo_apy if self.promo_until and now < self.promo_until else Decimal('0.00')
        if not self.enabled:
            return extra
        return self.base_apy + extra

    def to_dict(self, now: float, *, policy: Optional[InterestPolicy] = None) -> Dict[str, Any]:
        enrolled = bool(self.enabled and self.base_apy > 0)
        payload = {
            'account_id': self.account_id,
            'userid': self.userid,
            'account': self.account,
            'base_apy': rate_str(self.base_apy),
            'effective_apy': rate_str(self.effective_apy(now)),
            'enabled': self.enabled,
            'enrolled': enrolled,
            'accrued': money_str(self.accrued),
            'last_balance': money_str(self.last_balance),
            'last_accrual_at': self.last_accrual_at,
            'opened_at': self.opened_at,
            'last_posted_period': self.last_posted_period or None,
            'last_posted_at': self.last_posted_at or None,
            'ytd_posted': money_str(self.ytd_posted),
            'promo_apy': rate_str(self.promo_apy) if self.promo_until and now < self.promo_until else '0.00',
            'promo_until': self.promo_until if self.promo_until and now < self.promo_until else None,
            'promo_reason': self.promo_reason if self.promo_until and now < self.promo_until else '',
        }
        if policy is not None:
            payload['min_post'] = money_str(policy.min_post)
        return payload


@dataclass
class InterestRequest:
    request_id: str
    userid: str
    account: str
    requested_apy: Decimal
    status: str
    reason: str = ''
    created_at: float = 0.0
    decided_at: float = 0.0
    decided_by: str = ''
    note: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'request_id': self.request_id,
            'userid': self.userid,
            'account': self.account,
            'requested_apy': rate_str(self.requested_apy),
            'status': self.status,
            'reason': self.reason,
            'created_at': self.created_at,
            'decided_at': self.decided_at or None,
            'decided_by': self.decided_by or None,
            'note': self.note,
        }


@dataclass
class InterestPosting:
    posting_id: str
    occurrence_id: str
    userid: str
    account: str
    amount: Decimal
    period: str
    apy: Decimal
    created_at: float
    status: str = 'posted'
    remark: str = 'interest credited'

    def to_dict(self) -> Dict[str, Any]:
        return {
            'posting_id': self.posting_id,
            'occurrence_id': self.occurrence_id,
            'userid': self.userid,
            'account': self.account,
            'amount': money_str(self.amount),
            'period': self.period,
            'apy': rate_str(self.apy),
            'created_at': self.created_at,
            'status': self.status,
            'remark': self.remark,
        }


class MemoryInterestStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.accounts: Dict[str, InterestAccount] = {}
        self.by_account: Dict[str, str] = {}
        self.requests: Dict[str, InterestRequest] = {}
        self.postings: Dict[str, InterestPosting] = {}
        self.by_occurrence: Dict[str, str] = {}

    def put_account(self, item: InterestAccount) -> None:
        with self._lock:
            self.accounts[item.account_id] = item
            self.by_account[item.account] = item.account_id

    def get_account(self, account_id: str) -> Optional[InterestAccount]:
        with self._lock:
            item = self.accounts.get(account_id)
            return None if item is None else _copy_account(item)

    def get_by_account(self, account: str) -> Optional[InterestAccount]:
        with self._lock:
            account_id = self.by_account.get(account)
            if not account_id:
                return None
            return _copy_account(self.accounts[account_id])

    def update_account(self, item: InterestAccount) -> None:
        self.put_account(item)

    def list_accounts(self, userid: Optional[str] = None) -> List[InterestAccount]:
        with self._lock:
            items = list(self.accounts.values())
            if userid is not None:
                items = [item for item in items if item.userid == userid]
            return [_copy_account(item) for item in items]

    def put_request(self, item: InterestRequest) -> None:
        with self._lock:
            self.requests[item.request_id] = item

    def get_request(self, request_id: str) -> Optional[InterestRequest]:
        with self._lock:
            return self.requests.get(request_id)

    def update_request(self, item: InterestRequest) -> None:
        self.put_request(item)

    def list_requests(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[InterestRequest]:
        with self._lock:
            items = list(self.requests.values())
            if userid is not None:
                items = [item for item in items if item.userid == userid]
            if status is not None:
                items = [item for item in items if item.status == status]
            return items

    def put_posting(self, item: InterestPosting) -> None:
        with self._lock:
            self.postings[item.posting_id] = item
            self.by_occurrence[item.occurrence_id] = item.posting_id

    def get_by_occurrence(self, occurrence_id: str) -> Optional[InterestPosting]:
        with self._lock:
            posting_id = self.by_occurrence.get(occurrence_id)
            if not posting_id:
                return None
            return self.postings[posting_id]

    def list_postings(self, userid: Optional[str] = None, account: Optional[str] = None) -> List[InterestPosting]:
        with self._lock:
            items = list(self.postings.values())
            if userid is not None:
                items = [item for item in items if item.userid == userid]
            if account is not None:
                items = [item for item in items if item.account == account]
            return items


def _copy_account(item: InterestAccount) -> InterestAccount:
    return InterestAccount(
        account_id=item.account_id,
        userid=item.userid,
        account=item.account,
        base_apy=item.base_apy,
        enabled=item.enabled,
        accrued=item.accrued,
        last_balance=item.last_balance,
        last_accrual_at=item.last_accrual_at,
        opened_at=item.opened_at,
        last_posted_period=item.last_posted_period,
        last_posted_at=item.last_posted_at,
        ytd_posted=item.ytd_posted,
        ytd_year=item.ytd_year,
        promo_apy=item.promo_apy,
        promo_until=item.promo_until,
        promo_reason=item.promo_reason,
    )


class SqliteInterestStore:
    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        return conn

    def _init(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS interest_accounts (
                    account_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL UNIQUE,
                    base_apy TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    accrued TEXT NOT NULL,
                    last_balance TEXT NOT NULL,
                    last_accrual_at REAL NOT NULL,
                    opened_at REAL NOT NULL,
                    last_posted_period TEXT NOT NULL,
                    last_posted_at REAL NOT NULL,
                    ytd_posted TEXT NOT NULL,
                    ytd_year INTEGER NOT NULL,
                    promo_apy TEXT NOT NULL,
                    promo_until REAL NOT NULL,
                    promo_reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS interest_requests (
                    request_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL,
                    requested_apy TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    decided_at REAL NOT NULL,
                    decided_by TEXT NOT NULL,
                    note TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS interest_postings (
                    posting_id TEXT PRIMARY KEY,
                    occurrence_id TEXT NOT NULL UNIQUE,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    period TEXT NOT NULL,
                    apy TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    remark TEXT NOT NULL
                );
                """
            )

    def put_account(self, item: InterestAccount) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO interest_accounts (
                    account_id, userid, account, base_apy, enabled, accrued,
                    last_balance, last_accrual_at, opened_at, last_posted_period,
                    last_posted_at, ytd_posted, ytd_year, promo_apy, promo_until,
                    promo_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._account_row(item),
            )

    def get_account(self, account_id: str) -> Optional[InterestAccount]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM interest_accounts WHERE account_id=?', (account_id,)
            ).fetchone()
        return None if row is None else self._account_from_row(row)

    def get_by_account(self, account: str) -> Optional[InterestAccount]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM interest_accounts WHERE account=?', (account,)
            ).fetchone()
        return None if row is None else self._account_from_row(row)

    def update_account(self, item: InterestAccount) -> None:
        self.put_account(item)

    def list_accounts(self, userid: Optional[str] = None) -> List[InterestAccount]:
        sql = 'SELECT * FROM interest_accounts'
        params: Tuple[Any, ...] = ()
        if userid is not None:
            sql += ' WHERE userid=?'
            params = (userid,)
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._account_from_row(row) for row in rows]

    def put_request(self, item: InterestRequest) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO interest_requests (
                    request_id, userid, account, requested_apy, status, reason,
                    created_at, decided_at, decided_by, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.request_id, item.userid, item.account, rate_str(item.requested_apy),
                    item.status, item.reason, item.created_at, item.decided_at,
                    item.decided_by, item.note,
                ),
            )

    def get_request(self, request_id: str) -> Optional[InterestRequest]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM interest_requests WHERE request_id=?', (request_id,)
            ).fetchone()
        return None if row is None else self._request_from_row(row)

    def update_request(self, item: InterestRequest) -> None:
        self.put_request(item)

    def list_requests(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[InterestRequest]:
        clauses = []
        params: List[Any] = []
        if userid is not None:
            clauses.append('userid=?')
            params.append(userid)
        if status is not None:
            clauses.append('status=?')
            params.append(status)
        sql = 'SELECT * FROM interest_requests'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._request_from_row(row) for row in rows]

    def put_posting(self, item: InterestPosting) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO interest_postings (
                    posting_id, occurrence_id, userid, account, amount, period,
                    apy, created_at, status, remark
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.posting_id, item.occurrence_id, item.userid, item.account,
                    money_str(item.amount), item.period, rate_str(item.apy),
                    item.created_at, item.status, item.remark,
                ),
            )

    def get_by_occurrence(self, occurrence_id: str) -> Optional[InterestPosting]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM interest_postings WHERE occurrence_id=?', (occurrence_id,)
            ).fetchone()
        return None if row is None else self._posting_from_row(row)

    def list_postings(self, userid: Optional[str] = None, account: Optional[str] = None) -> List[InterestPosting]:
        clauses = []
        params: List[Any] = []
        if userid is not None:
            clauses.append('userid=?')
            params.append(userid)
        if account is not None:
            clauses.append('account=?')
            params.append(account)
        sql = 'SELECT * FROM interest_postings'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._posting_from_row(row) for row in rows]

    @staticmethod
    def _account_row(item: InterestAccount) -> Tuple[Any, ...]:
        return (
            item.account_id, item.userid, item.account, rate_str(item.base_apy),
            1 if item.enabled else 0, str(item.accrued), money_str(item.last_balance),
            item.last_accrual_at, item.opened_at, item.last_posted_period or '',
            item.last_posted_at, money_str(item.ytd_posted), item.ytd_year,
            rate_str(item.promo_apy), item.promo_until, item.promo_reason,
        )

    @staticmethod
    def _account_from_row(row: sqlite3.Row) -> InterestAccount:
        return InterestAccount(
            account_id=row['account_id'],
            userid=row['userid'],
            account=row['account'],
            base_apy=Decimal(row['base_apy']),
            enabled=bool(row['enabled']),
            accrued=Decimal(row['accrued']),
            last_balance=Decimal(row['last_balance']),
            last_accrual_at=float(row['last_accrual_at']),
            opened_at=float(row['opened_at']),
            last_posted_period=row['last_posted_period'] or '',
            last_posted_at=float(row['last_posted_at']),
            ytd_posted=Decimal(row['ytd_posted']),
            ytd_year=int(row['ytd_year']),
            promo_apy=Decimal(row['promo_apy']),
            promo_until=float(row['promo_until']),
            promo_reason=row['promo_reason'] or '',
        )

    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> InterestRequest:
        return InterestRequest(
            request_id=row['request_id'],
            userid=row['userid'],
            account=row['account'],
            requested_apy=Decimal(row['requested_apy']),
            status=row['status'],
            reason=row['reason'] or '',
            created_at=float(row['created_at']),
            decided_at=float(row['decided_at']),
            decided_by=row['decided_by'] or '',
            note=row['note'] or '',
        )

    @staticmethod
    def _posting_from_row(row: sqlite3.Row) -> InterestPosting:
        return InterestPosting(
            posting_id=row['posting_id'],
            occurrence_id=row['occurrence_id'],
            userid=row['userid'],
            account=row['account'],
            amount=Decimal(row['amount']),
            period=row['period'],
            apy=Decimal(row['apy']),
            created_at=float(row['created_at']),
            status=row['status'],
            remark=row['remark'] or '',
        )


def _credit_succeeded(result: Any) -> bool:
    if result is None or result is True:
        return True
    if isinstance(result, dict):
        return True
    if result in {'Success', 'Amount Credited', 'done', 'ok'}:
        return True
    return False


class InterestService:
    def __init__(
        self,
        policy: Optional[InterestPolicy] = None,
        store: Any = None,
        *,
        clock: Optional[Callable[[], float]] = None,
        is_savings_loader: Optional[Callable[..., bool]] = None,
        balance_loader: Optional[Callable[[str], Optional[Tuple[str, Any]]]] = None,
        credit_executor: Optional[Callable[[str, Decimal, str], Any]] = None,
    ) -> None:
        self.policy = policy or InterestPolicy()
        self.store = store or MemoryInterestStore()
        self.clock = clock or time.time
        self.is_savings_loader = is_savings_loader
        self.balance_loader = balance_loader
        self.credit_executor = credit_executor

    def _actor_is_employee(self, actor_type: str) -> bool:
        return str(actor_type or '').strip().lower() in EMPLOYEE_ROLES

    def _assert_staff(self, actor_type: str) -> None:
        if not self._actor_is_employee(actor_type):
            raise InterestError('interest_forbidden', 'Staff only', error='interest_forbidden')

    def _assert_ownership(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: str,
        own_accounts: Optional[Iterable[Any]],
    ) -> None:
        if self._actor_is_employee(actor_type):
            return
        if actor != owner_userid:
            raise InterestError('interest_forbidden', 'Not your account', error='interest_forbidden')
        owned = _own_account_set(own_accounts)
        if owned and account not in owned:
            raise InterestError('interest_forbidden', 'Not your account', error='interest_forbidden')

    def is_savings(self, account: str, userid: Optional[str] = None, account_type: Optional[str] = None) -> bool:
        if account_type is not None:
            if is_savings_account_type(account_type):
                return True
            if str(account_type).strip().lower() in NON_YIELD_ACCOUNT_TYPES:
                return False
        if callable(self.is_savings_loader):
            try:
                return bool(self.is_savings_loader(account, userid))
            except TypeError:
                return bool(self.is_savings_loader(account))
        return True

    def _load_state(self, account: str) -> Optional[Tuple[str, Decimal]]:
        if not callable(self.balance_loader):
            return None
        loaded = self.balance_loader(account)
        if not loaded:
            return None
        account_type, balance = loaded
        return str(account_type or ''), parse_balance(balance)

    def _validate_apy(self, rate: Decimal, *, allow_zero: bool = False) -> Decimal:
        if rate == 0 and allow_zero:
            return rate
        if rate < self.policy.min_apy or rate > self.policy.max_apy:
            raise InterestError(
                'apy_out_of_range',
                'APY is outside the allowed product range',
                min=rate_str(self.policy.min_apy),
                max=rate_str(self.policy.max_apy),
            )
        return rate

    def ensure_account(
        self,
        *,
        userid: str,
        account: Any,
        enabled: bool = False,
        balance: Optional[Any] = None,
        account_type: Optional[str] = None,
    ) -> InterestAccount:
        normalized = normalize_account(account)
        if not self.is_savings(normalized, userid, account_type):
            raise InterestError('not_savings', 'Interest applies to savings accounts only')
        existing = self.store.get_by_account(normalized)
        now = float(self.clock())
        if existing:
            if existing.userid != userid:
                existing.userid = userid
                self.store.update_account(existing)
            return existing
        loaded = None if balance is not None else self._load_state(normalized)
        opening_balance = parse_balance(balance) if balance is not None else (
            loaded[1] if loaded else Decimal('0.00')
        )
        item = InterestAccount(
            account_id=str(uuid.uuid4()),
            userid=userid,
            account=normalized,
            base_apy=self.policy.default_apy,
            enabled=enabled and self.policy.default_apy > 0,
            accrued=Decimal('0'),
            last_balance=opening_balance,
            last_accrual_at=now,
            opened_at=now,
            ytd_year=utc_datetime(now).year,
        )
        self.store.put_account(item)
        return item

    def _roll_ytd(self, item: InterestAccount, now: float) -> None:
        year = utc_datetime(now).year
        if item.ytd_year != year:
            item.ytd_posted = Decimal('0.00')
            item.ytd_year = year

    def accrue(self, item: InterestAccount, now: Optional[float] = None) -> InterestAccount:
        now = float(self.clock() if now is None else now)
        self._roll_ytd(item, now)
        if not item.last_accrual_at:
            item.last_accrual_at = now
            self.store.update_account(item)
            return item
        days = days_between(item.last_accrual_at, now)
        if days <= 0:
            return item
        rate = item.effective_apy(item.last_accrual_at)
        if item.last_balance > 0 and rate > 0:
            daily = (item.last_balance * rate) / (Decimal(100) * Decimal(self.policy.days_in_year))
            item.accrued = (item.accrued + (daily * Decimal(days))).quantize(
                ACCRUAL_QUANTUM, rounding=ROUND_HALF_EVEN
            )
        item.last_accrual_at = whole_days_advance(item.last_accrual_at, days)
        self.store.update_account(item)
        return item

    def observe(
        self,
        account: Any,
        balance: Any = None,
        *,
        userid: Optional[str] = None,
        account_type: Optional[str] = None,
    ) -> Optional[InterestAccount]:
        try:
            normalized = normalize_account(account)
        except AccountError:
            return None
        loaded = None if balance is not None and account_type is not None else self._load_state(normalized)
        resolved_type = account_type if account_type is not None else (loaded[0] if loaded else None)
        if resolved_type and not self.is_savings(normalized, userid, resolved_type):
            return None
        existing = self.store.get_by_account(normalized)
        if existing is None:
            if not userid:
                return None
            try:
                existing = self.ensure_account(
                    userid=userid,
                    account=normalized,
                    account_type=resolved_type,
                    balance=balance if balance is not None else (loaded[1] if loaded else None),
                )
            except InterestError:
                return None
        now = float(self.clock())
        self.accrue(existing, now)
        if balance is not None:
            existing.last_balance = parse_balance(balance)
        elif loaded:
            existing.last_balance = loaded[1]
        self.store.update_account(existing)
        return existing

    def set_apy(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        apy: Any,
        own_accounts: Optional[Iterable[Any]] = None,
        account_type: Optional[str] = None,
        balance: Any = None,
    ) -> InterestAccount:
        self._assert_staff(actor_type)
        normalized = normalize_account(account)
        rate = parse_apy(apy, allow_zero=True)
        self._validate_apy(rate, allow_zero=True)
        self._assert_ownership(
            owner_userid=owner_userid, actor=actor, actor_type=actor_type,
            account=normalized, own_accounts=own_accounts,
        )
        item = self.ensure_account(
            userid=owner_userid, account=normalized, account_type=account_type, balance=balance,
        )
        self.observe(normalized, balance, userid=owner_userid, account_type=account_type)
        item = self.store.get_by_account(normalized)
        item.base_apy = rate
        item.enabled = rate > 0
        self.store.update_account(item)
        return item

    def adjust_apy(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        delta: Any,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> InterestAccount:
        self._assert_staff(actor_type)
        normalized = normalize_account(account)
        raw = str(delta).strip().replace('%', '').replace(',', '')
        if not raw or raw in {'+', '-'}:
            raise RateError('invalid_apy')
        signed = parse_apy(raw.lstrip('+-'), allow_zero=False)
        if raw.startswith('-'):
            signed = -signed
        item = self.store.get_by_account(normalized)
        if item is None or not item.enabled:
            raise InterestError('not_enrolled', 'Account is not enrolled in interest')
        return self.set_apy(
            owner_userid=owner_userid,
            actor=actor,
            actor_type=actor_type,
            account=normalized,
            apy=item.base_apy + signed,
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
    ) -> InterestAccount:
        return self.set_apy(
            owner_userid=owner_userid,
            actor=actor,
            actor_type=actor_type,
            account=account,
            apy='0',
            own_accounts=own_accounts,
        )

    def grant_promo(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        apy: Any,
        seconds: Any = None,
        reason: Any = '',
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> InterestAccount:
        self._assert_staff(actor_type)
        normalized = normalize_account(account)
        bump = parse_apy(apy, allow_zero=False)
        if bump > self.policy.promo_max:
            raise InterestError(
                'promo_too_large',
                'Promotional APY exceeds the product cap',
                max=rate_str(self.policy.promo_max),
            )
        ttl = self.policy.promo_max_seconds if seconds in (None, '') else int(seconds)
        if ttl <= 0 or ttl > self.policy.promo_max_seconds:
            raise InterestError('invalid_ttl', 'Promotional duration is out of range')
        item = self.ensure_account(userid=owner_userid, account=normalized)
        if not item.enabled:
            raise InterestError('not_enrolled', 'Account is not enrolled in interest')
        self._assert_ownership(
            owner_userid=owner_userid, actor=actor, actor_type=actor_type,
            account=normalized, own_accounts=own_accounts,
        )
        now = float(self.clock())
        self.accrue(item, now)
        item.promo_apy = bump
        item.promo_until = now + ttl
        item.promo_reason = str(reason or '')[:120]
        if item.effective_apy(now) > self.policy.max_apy:
            raise InterestError('apy_out_of_range', 'Effective APY would exceed the product cap')
        self.store.update_account(item)
        return item

    def revoke_promo(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> InterestAccount:
        self._assert_staff(actor_type)
        normalized = normalize_account(account)
        item = self.store.get_by_account(normalized)
        if item is None:
            raise InterestError('not_enrolled', 'Account is not enrolled in interest')
        self._assert_ownership(
            owner_userid=owner_userid, actor=actor, actor_type=actor_type,
            account=normalized, own_accounts=own_accounts,
        )
        self.accrue(item)
        item.promo_apy = Decimal('0.00')
        item.promo_until = 0.0
        item.promo_reason = ''
        self.store.update_account(item)
        return item

    def request_apy(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        requested_apy: Any,
        reason: Any = '',
        own_accounts: Optional[Iterable[Any]] = None,
        account_type: Optional[str] = None,
    ) -> InterestRequest:
        if self._actor_is_employee(actor_type):
            raise InterestError('interest_forbidden', 'Customers request APY changes')
        normalized = normalize_account(account)
        rate = parse_apy(requested_apy, allow_zero=False)
        self._validate_apy(rate)
        self._assert_ownership(
            owner_userid=owner_userid, actor=actor, actor_type=actor_type,
            account=normalized, own_accounts=own_accounts,
        )
        item = self.ensure_account(
            userid=owner_userid, account=normalized, account_type=account_type,
        )
        now = float(self.clock())
        current = item.effective_apy(now)
        if rate <= current:
            raise InterestError('apy_not_increase', 'Requested APY must be higher than the current rate')
        if rate - item.base_apy > self.policy.request_max_increment:
            raise InterestError(
                'request_too_large',
                'Requested increase exceeds the per-request cap',
                max=rate_str(self.policy.request_max_increment),
            )
        pending = self.store.list_requests(userid=owner_userid, status='pending')
        if any(req.account == normalized for req in pending):
            raise InterestError('request_duplicate', 'A pending APY request already exists')
        if len(pending) >= self.policy.max_pending_requests:
            raise InterestError('request_limit', 'Too many pending APY requests')
        req = InterestRequest(
            request_id=str(uuid.uuid4()),
            userid=owner_userid,
            account=normalized,
            requested_apy=rate,
            status='pending',
            reason=str(reason or '')[:200],
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
        note: Any = '',
    ) -> InterestRequest:
        self._assert_staff(actor_type)
        req = self.store.get_request(str(request_id or ''))
        if req is None:
            raise InterestError('request_not_found', 'APY request not found')
        if req.status != 'pending':
            raise InterestError('request_not_pending', 'APY request is no longer pending')
        now = float(self.clock())
        req.decided_at = now
        req.decided_by = actor
        req.note = str(note or '')[:200]
        if approve:
            self.set_apy(
                owner_userid=req.userid,
                actor=actor,
                actor_type=actor_type,
                account=req.account,
                apy=req.requested_apy,
            )
            req.status = 'approved'
        else:
            req.status = 'denied'
        self.store.update_request(req)
        return req

    def _due_period(self, item: InterestAccount, now: float) -> Optional[str]:
        completed = previous_period(now)
        opened_period = period_id(item.opened_at or now)
        if completed < opened_period and completed != opened_period:
            # still in the opening month — nothing completed yet
            if period_id(now) == opened_period:
                return None
        if item.last_posted_period and completed <= item.last_posted_period:
            return None
        if not item.last_posted_period and period_id(now) == opened_period:
            return None
        return completed

    def _occurrence(self, account: str, period: str) -> str:
        return f'{account}:{period}'

    def post_due(
        self,
        *,
        owner_userid: Optional[str] = None,
        account: Any = None,
        force: bool = False,
        actor_type: str = 'system',
    ) -> List[InterestPosting]:
        if account is not None:
            items = [self.store.get_by_account(normalize_account(account))]
            items = [item for item in items if item is not None]
        elif owner_userid:
            items = self.store.list_accounts(userid=owner_userid)
        else:
            items = self.store.list_accounts()
        posted: List[InterestPosting] = []
        now = float(self.clock())
        for item in items:
            if owner_userid and item.userid != owner_userid:
                continue
            self.observe(item.account, userid=item.userid)
            item = self.store.get_by_account(item.account)
            period = self._due_period(item, now)
            if period is None and not force:
                continue
            if period is None:
                period = period_id(now)
            occurrence = self._occurrence(item.account, period)
            existing = self.store.get_by_occurrence(occurrence)
            if existing is not None:
                continue
            amount = item.accrued.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
            if amount < self.policy.min_post:
                continue
            remark = f'interest credited for {period}'
            result = None
            if callable(self.credit_executor):
                try:
                    result = self.credit_executor(item.account, amount, remark)
                except Exception:
                    result = 'failed'
                if not _credit_succeeded(result):
                    continue
            posting = InterestPosting(
                posting_id=str(uuid.uuid4()),
                occurrence_id=occurrence,
                userid=item.userid,
                account=item.account,
                amount=amount,
                period=period,
                apy=item.effective_apy(now),
                created_at=now,
                status='posted',
                remark=remark,
            )
            self.store.put_posting(posting)
            leftover = item.accrued - amount
            if leftover < 0:
                leftover = Decimal('0')
            item.accrued = leftover.quantize(ACCRUAL_QUANTUM, rounding=ROUND_HALF_EVEN)
            item.last_posted_period = period
            item.last_posted_at = now
            self._roll_ytd(item, now)
            item.ytd_posted = (item.ytd_posted + amount).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
            item.last_balance = (item.last_balance + amount).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
            self.store.update_account(item)
            posted.append(posting)
        return posted

    def snapshot(
        self,
        userid: str,
        *,
        balances: Optional[Dict[str, Any]] = None,
        accounts: Optional[Iterable[Any]] = None,
        post_due: bool = True,
    ) -> Dict[str, Any]:
        now = float(self.clock())
        known = set()
        for account in accounts or ():
            try:
                known.add(normalize_account(account))
            except AccountError:
                continue
        for account in (balances or {}):
            try:
                known.add(normalize_account(account))
            except AccountError:
                continue
        for account in known:
            balance = None if not balances else balances.get(account)
            if balance is None and balances:
                balance = balances.get(int(account)) if account.isdigit() else None
            try:
                self.ensure_account(
                    userid=userid,
                    account=account,
                    balance=balance,
                    account_type='savings',
                )
            except InterestError:
                continue
            self.observe(account, balance, userid=userid, account_type='savings')
        if post_due:
            try:
                self.post_due(owner_userid=userid)
            except Exception:
                pass
        facilities = []
        for item in self.store.list_accounts(userid=userid):
            self.accrue(item, now)
            facilities.append(item.to_dict(now, policy=self.policy))
        requests = [req.to_dict() for req in self.store.list_requests(userid=userid)]
        postings = [row.to_dict() for row in self.store.list_postings(userid=userid)]
        postings.sort(key=lambda row: row['created_at'], reverse=True)
        return {
            'accounts': facilities,
            'requests': requests,
            'postings': postings[:20],
            'policy': {
                'default_apy': rate_str(self.policy.default_apy),
                'min_apy': rate_str(self.policy.min_apy),
                'max_apy': rate_str(self.policy.max_apy),
                'min_post': money_str(self.policy.min_post),
                'days_in_year': self.policy.days_in_year,
                'request_max_increment': rate_str(self.policy.request_max_increment),
                'promo_max': rate_str(self.policy.promo_max),
            },
        }


def set_service(service: Optional[InterestService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[InterestService]:
    return _SERVICE


def build_service(
    *,
    is_savings_loader: Optional[Callable[..., bool]] = None,
    balance_loader: Optional[Callable[[str], Optional[Tuple[str, Any]]]] = None,
    credit_executor: Optional[Callable[[str, Decimal, str], Any]] = None,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
) -> InterestService:
    if store is None:
        path = os.environ.get('INTEREST_STORE') or DEFAULT_STORE_PATH
        if (os.environ.get('INTEREST_STORE_BACKEND') or 'sqlite').strip().lower() == 'memory':
            store = MemoryInterestStore()
        else:
            store = SqliteInterestStore(path)
    return InterestService(
        InterestPolicy.from_env(),
        store,
        clock=clock,
        is_savings_loader=is_savings_loader,
        balance_loader=balance_loader,
        credit_executor=credit_executor,
    )


def observe_interest(account: Any, balance: Any = None, *, userid: Optional[str] = None, account_type: Optional[str] = None) -> None:
    service = get_service()
    if service is None:
        return
    try:
        service.observe(account, balance, userid=userid, account_type=account_type)
    except Exception:
        return


def _require_session_user():
    userid = session.get('userid')
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
        'interest_forbidden': 403,
        'not_savings': 403,
        'not_enrolled': 409,
        'request_not_found': 404,
        'invalid_apy': 400,
        'invalid_account': 400,
        'invalid_ttl': 400,
        'apy_out_of_range': 400,
        'apy_not_increase': 400,
        'request_too_large': 400,
        'promo_too_large': 400,
        'missing_customer_id': 400,
    }.get(code, 400)


def _error_body(exc: InterestError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    for key, value in exc.extra.items():
        if value is not None:
            body[key] = value
    return body


def _own_accounts(loader, userid, actor_type):
    if actor_type == 'customer' and callable(loader):
        return loader(userid)
    return None


def _account_payload(service: InterestService, item: InterestAccount) -> Dict[str, Any]:
    return item.to_dict(float(service.clock()), policy=service.policy)


def handle_set_apy(service: InterestService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    try:
        item = service.set_apy(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            apy=values.get('apy') if values.get('apy') is not None else values.get('rate'),
            own_accounts=_own_accounts(own_accounts_loader, userid, actor_type),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except RateError:
        return jsonify({'message': 'Enter a valid APY', 'error': 'invalid_apy'}), 400
    except InterestError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'APY updated',
        'account': _account_payload(service, item),
        'Interest': service.snapshot(owner, post_due=False),
    }), 200


def handle_adjust_apy(service: InterestService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    try:
        item = service.adjust_apy(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            delta=values.get('delta'),
            own_accounts=_own_accounts(own_accounts_loader, userid, actor_type),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except RateError:
        return jsonify({'message': 'Enter a valid APY', 'error': 'invalid_apy'}), 400
    except InterestError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'APY adjusted',
        'account': _account_payload(service, item),
        'Interest': service.snapshot(owner, post_due=False),
    }), 200


def handle_revoke(service: InterestService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    try:
        item = service.revoke(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            own_accounts=_own_accounts(own_accounts_loader, userid, actor_type),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except InterestError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Interest enrollment revoked',
        'account': _account_payload(service, item),
        'Interest': service.snapshot(owner, post_due=False),
    }), 200


def handle_grant_promo(service: InterestService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    try:
        item = service.grant_promo(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            apy=values.get('apy') if values.get('apy') is not None else values.get('amount'),
            seconds=values.get('seconds'),
            reason=values.get('reason'),
            own_accounts=_own_accounts(own_accounts_loader, userid, actor_type),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except RateError:
        return jsonify({'message': 'Enter a valid APY', 'error': 'invalid_apy'}), 400
    except InterestError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    except (TypeError, ValueError):
        return jsonify({'message': 'Invalid promotional duration', 'error': 'invalid_ttl'}), 400
    return jsonify({
        'message': 'Promotional APY granted',
        'account': _account_payload(service, item),
        'Interest': service.snapshot(owner, post_due=False),
    }), 200


def handle_revoke_promo(service: InterestService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    try:
        item = service.revoke_promo(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            own_accounts=_own_accounts(own_accounts_loader, userid, actor_type),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except InterestError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Promotional APY revoked',
        'account': _account_payload(service, item),
        'Interest': service.snapshot(owner, post_due=False),
    }), 200


def handle_request_apy(service: InterestService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values) or userid
    try:
        req = service.request_apy(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            requested_apy=values.get('requested_apy') or values.get('apy'),
            reason=values.get('reason'),
            own_accounts=_own_accounts(own_accounts_loader, userid, actor_type),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except RateError:
        return jsonify({'message': 'Enter a valid APY', 'error': 'invalid_apy'}), 400
    except InterestError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'APY change requested',
        'request': req.to_dict(),
        'Interest': service.snapshot(owner, post_due=False),
    }), 201


def handle_decide_request(service: InterestService):
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
    except InterestError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'APY request ' + req.status,
        'request': req.to_dict(),
        'Interest': service.snapshot(req.userid, post_due=False),
    }), 200


def handle_list(service: InterestService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Interest': service.snapshot(owner)}), 200


def handle_post_due(service: InterestService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    elif values.get('customer_id') and str(values.get('customer_id')) != userid:
        return jsonify({'message': 'User ID mismatch', 'error': 'userid_mismatch'}), 403
    force = bool(values.get('force')) and actor_type in EMPLOYEE_ROLES
    try:
        posted = service.post_due(
            owner_userid=owner,
            account=values.get('account'),
            force=force,
            actor_type=actor_type,
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except InterestError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Interest posting complete',
        'posted': [row.to_dict() for row in posted],
        'Interest': service.snapshot(owner, post_due=False),
    }), 200


def attach_interest_routes(app, service: InterestService, own_accounts_loader=None) -> None:
    @app.route('/setApy', methods=['POST', 'GET'])
    def set_apy_route():
        return handle_set_apy(service, own_accounts_loader=own_accounts_loader)

    @app.route('/adjustApy', methods=['POST', 'GET'])
    def adjust_apy_route():
        return handle_adjust_apy(service, own_accounts_loader=own_accounts_loader)

    @app.route('/revokeApy', methods=['POST', 'GET'])
    def revoke_apy_route():
        return handle_revoke(service, own_accounts_loader=own_accounts_loader)

    @app.route('/grantPromoApy', methods=['POST', 'GET'])
    def grant_promo_route():
        return handle_grant_promo(service, own_accounts_loader=own_accounts_loader)

    @app.route('/revokePromoApy', methods=['POST', 'GET'])
    def revoke_promo_route():
        return handle_revoke_promo(service, own_accounts_loader=own_accounts_loader)

    @app.route('/requestApy', methods=['POST', 'GET'])
    def request_apy_route():
        return handle_request_apy(service, own_accounts_loader=own_accounts_loader)

    @app.route('/decideApyRequest', methods=['POST', 'GET'])
    def decide_apy_route():
        return handle_decide_request(service)

    @app.route('/listInterest', methods=['POST', 'GET'])
    def list_interest_route():
        return handle_list(service)

    @app.route('/postDueInterest', methods=['POST', 'GET'])
    def post_due_route():
        return handle_post_due(service)
