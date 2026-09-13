"""Period statements on an append-only money journal.

`Accounts.transaction_history` is still a concatenated HTML blob, and
PR #10's MySQL `AccountLedger` is a separate unmerged branch. This
module is the reusable foundation those surfaces can share later:

- typed journal entries (kind / direction / running balance)
- period resolution (monthly / YTD / custom)
- immutable statement snapshots keyed by occurrence id

Distinct from:

- PR #10 parameterized SQL + MySQL ledger (unmerged)
- Interest posting (PR #49) — yield credits, not period documents
- Audit trail (PR #24) — admin security events
- Scheduled transfers (PR #36) — outbound calendar instructions

Existing HTML history and `/getTransactionHistory` stay unchanged.
Observe hooks are fail-open so a journal miss never blocks a debit.

Stores are pluggable (memory for tests, sqlite WAL by default).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
PERIOD_KINDS = frozenset({'monthly', 'custom', 'ytd'})
ENTRY_KINDS = frozenset({
    'credit', 'debit', 'transfer_in', 'transfer_out',
    'deposit', 'withdraw', 'cheque', 'open', 'interest',
})
CREDIT_KINDS = frozenset({'credit', 'transfer_in', 'deposit', 'open', 'interest'})
DEBIT_KINDS = frozenset({'debit', 'transfer_out', 'withdraw', 'cheque'})
REQUEST_STATUSES = frozenset({'pending', 'approved', 'denied'})
DELIVERY_KINDS = frozenset({'mail', 'branch', 'electronic'})
MONEY_QUANTUM = Decimal('0.01')
SECONDS_PER_DAY = 86400
DEFAULT_STORE_PATH = os.path.join('SystemLogs', 'statement.sqlite')

_SERVICE: Optional['StatementService'] = None


class AccountError(ValueError):
    pass


class AmountError(ValueError):
    pass


class PeriodError(ValueError):
    pass


class StatementError(ValueError):
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


def money_str(value: Decimal) -> str:
    return str(value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN))


def utc_datetime(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def month_id(ts: float) -> str:
    dt = utc_datetime(ts)
    return f'{dt.year:04d}-{dt.month:02d}'


def year_id(ts: float) -> str:
    return f'{utc_datetime(ts).year:04d}'


def parse_month(value: Any) -> Tuple[int, int]:
    text = str(value or '').strip()
    try:
        year_s, month_s = text.split('-', 1)
        year, month = int(year_s), int(month_s)
    except (TypeError, ValueError):
        raise PeriodError('invalid_period') from None
    if month < 1 or month > 12 or year < 1970 or year > 2100:
        raise PeriodError('invalid_period')
    return year, month


def month_start_ts(year: int, month: int) -> float:
    return datetime(year, month, 1, tzinfo=timezone.utc).timestamp()


def month_end_ts(year: int, month: int) -> float:
    if month == 12:
        return datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp()
    return datetime(year, month + 1, 1, tzinfo=timezone.utc).timestamp()


def previous_month(year: int, month: int) -> Tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def next_month(year: int, month: int) -> Tuple[int, int]:
    if month == 12:
        return year + 1, 1
    return year, month + 1


def months_between(start_year: int, start_month: int, end_year: int, end_month: int) -> List[str]:
    found: List[str] = []
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        found.append(f'{year:04d}-{month:02d}')
        year, month = next_month(year, month)
        if len(found) > 240:
            break
    return found


def parse_when(value: Any, *, end_of_day: bool = False) -> float:
    if value is None or (isinstance(value, str) and not str(value).strip()):
        raise PeriodError('invalid_period')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = float(value)
        if ts <= 0:
            raise PeriodError('invalid_period')
        return ts
    text = str(value).strip()
    try:
        if text.isdigit() or (text.replace('.', '', 1).isdigit() and text.count('.') < 2):
            ts = float(text)
            if ts > 1e12:
                ts = ts / 1000.0
            if ts <= 0:
                raise PeriodError('invalid_period')
            return ts
    except ValueError:
        pass
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%d/%m/%Y', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            dt = datetime.strptime(text.replace('Z', ''), fmt).replace(tzinfo=timezone.utc)
            if end_of_day and fmt in {'%Y-%m-%d', '%Y/%m/%d', '%d/%m/%Y'}:
                dt = dt + timedelta(days=1)
            return dt.timestamp()
        except ValueError:
            continue
    try:
        year, month = parse_month(text)
        if end_of_day:
            return month_end_ts(year, month)
        return month_start_ts(year, month)
    except PeriodError:
        raise PeriodError('invalid_period') from None


def normalize_kind(value: Any) -> str:
    kind = str(value or '').strip().lower()
    aliases = {
        'transfer-in': 'transfer_in',
        'transfer-out': 'transfer_out',
        'xfer_in': 'transfer_in',
        'xfer_out': 'transfer_out',
        'withdrawal': 'withdraw',
        'bonus': 'open',
        'cashier_cheque': 'cheque',
        'check': 'cheque',
    }
    kind = aliases.get(kind, kind)
    if kind not in ENTRY_KINDS:
        raise StatementError('invalid_kind', 'Unknown journal kind')
    return kind


def normalize_direction(value: Any, kind: Optional[str] = None) -> str:
    text = str(value or '').strip().lower()
    if text in {'credit', 'cr', 'in', '+'}:
        return 'credit'
    if text in {'debit', 'dr', 'out', '-'}:
        return 'debit'
    if kind in CREDIT_KINDS:
        return 'credit'
    if kind in DEBIT_KINDS:
        return 'debit'
    raise StatementError('invalid_kind', 'Unknown journal direction')


def normalize_period_kind(value: Any) -> str:
    kind = str(value or 'monthly').strip().lower()
    aliases = {'month': 'monthly', 'year': 'ytd', 'year_to_date': 'ytd', 'range': 'custom'}
    kind = aliases.get(kind, kind)
    if kind not in PERIOD_KINDS:
        raise PeriodError('invalid_period')
    return kind


def normalize_delivery(value: Any) -> str:
    text = str(value or 'mail').strip().lower()
    if text not in DELIVERY_KINDS:
        raise StatementError('invalid_delivery', 'Delivery must be mail, branch, or electronic')
    return text


def signed_amount(amount: Decimal, direction: str) -> Decimal:
    if direction == 'debit':
        return -amount
    return amount


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


def accounts_from_customer_payload(accounts: Any) -> List[str]:
    found: List[str] = []
    if not isinstance(accounts, dict):
        return found
    for key in ('savings', 'checkin', 'checking', 'credit'):
        item = accounts.get(key)
        if isinstance(item, dict) and item.get('Account') not in (None, 'None', ''):
            try:
                found.append(normalize_account(item['Account']))
            except AccountError:
                continue
    return found


def html_escape(value: Any) -> str:
    text = '' if value is None else str(value)
    return (
        text.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )


@dataclass(frozen=True)
class PeriodSpec:
    kind: str
    period_id: str
    start_ts: float
    end_ts: float
    interim: bool = False

    @property
    def occurrence_suffix(self) -> str:
        return f'{self.kind}:{self.period_id}'

    def to_dict(self) -> Dict[str, Any]:
        return {
            'kind': self.kind,
            'period': self.period_id,
            'start': self.start_ts,
            'end': self.end_ts,
            'interim': self.interim,
        }


@dataclass(frozen=True)
class StatementPolicy:
    max_custom_days: int = 366
    min_custom_days: int = 1
    max_statements_per_account: int = 48
    max_pending_requests: int = 3
    allow_interim: bool = True
    max_description: int = 240

    @classmethod
    def from_env(cls) -> 'StatementPolicy':
        return cls(
            max_custom_days=_env_int('STATEMENT_MAX_CUSTOM_DAYS', 366),
            min_custom_days=_env_int('STATEMENT_MIN_CUSTOM_DAYS', 1),
            max_statements_per_account=_env_int('STATEMENT_MAX_PER_ACCOUNT', 48),
            max_pending_requests=_env_int('STATEMENT_MAX_PENDING_REQUESTS', 3),
            allow_interim=os.environ.get('STATEMENT_ALLOW_INTERIM', '1').strip() not in {'0', 'false', 'no'},
        )


def resolve_period(
    kind: Any,
    *,
    period: Any = None,
    start: Any = None,
    end: Any = None,
    now: Optional[float] = None,
    policy: Optional[StatementPolicy] = None,
) -> PeriodSpec:
    policy = policy or StatementPolicy()
    now = float(now if now is not None else datetime.now(tz=timezone.utc).timestamp())
    period_kind = normalize_period_kind(kind)
    now_dt = utc_datetime(now)

    if period_kind == 'monthly':
        if period:
            year, month = parse_month(period)
        else:
            year, month = previous_month(now_dt.year, now_dt.month)
        start_ts = month_start_ts(year, month)
        end_ts = month_end_ts(year, month)
        if start_ts >= now:
            raise StatementError('period_in_future', 'Cannot generate a future statement')
        interim = end_ts > now
        if interim and not policy.allow_interim:
            raise StatementError('period_not_closed', 'Current-month statements are disabled')
        if interim:
            end_ts = now
        return PeriodSpec('monthly', f'{year:04d}-{month:02d}', start_ts, end_ts, interim)

    if period_kind == 'ytd':
        if period:
            try:
                year = int(str(period).strip()[:4])
            except ValueError:
                raise PeriodError('invalid_period') from None
        else:
            year = now_dt.year
        if year < 1970 or year > 2100:
            raise PeriodError('invalid_period')
        start_ts = datetime(year, 1, 1, tzinfo=timezone.utc).timestamp()
        year_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp()
        if start_ts >= now:
            raise StatementError('period_in_future', 'Cannot generate a future statement')
        interim = year_end > now
        end_ts = now if interim else year_end
        if interim and not policy.allow_interim and year == now_dt.year:
            end_ts = datetime(year, now_dt.month, 1, tzinfo=timezone.utc).timestamp()
            if end_ts <= start_ts:
                raise StatementError('period_not_closed', 'Year-to-date statements are disabled')
            interim = False
        return PeriodSpec('ytd', f'{year:04d}', start_ts, end_ts, interim)

    start_ts = parse_when(start)
    end_ts = parse_when(end, end_of_day=True)
    if end_ts <= start_ts:
        raise StatementError('invalid_period', 'Statement end must be after start')
    span_days = (end_ts - start_ts) / SECONDS_PER_DAY
    if span_days > policy.max_custom_days:
        raise StatementError('range_too_long', 'Custom range exceeds the allowed window')
    if span_days < policy.min_custom_days:
        raise StatementError('invalid_period', 'Custom range is too short')
    if start_ts >= now:
        raise StatementError('period_in_future', 'Cannot generate a future statement')
    if end_ts > now:
        end_ts = now
    start_label = utc_datetime(start_ts).strftime('%Y-%m-%d')
    end_label = utc_datetime(end_ts - 1).strftime('%Y-%m-%d')
    return PeriodSpec('custom', f'{start_label}:{end_label}', start_ts, end_ts, False)


@dataclass
class JournalEntry:
    entry_id: str
    userid: str
    account: str
    kind: str
    direction: str
    amount: Decimal
    balance: Decimal
    counterparty: str = ''
    description: str = ''
    source_id: str = ''
    posted_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'entry_id': self.entry_id,
            'userid': self.userid,
            'account': self.account,
            'kind': self.kind,
            'direction': self.direction,
            'amount': money_str(self.amount),
            'balance': money_str(self.balance),
            'counterparty': self.counterparty,
            'description': self.description,
            'source_id': self.source_id,
            'posted_at': self.posted_at,
        }


@dataclass
class Statement:
    statement_id: str
    occurrence_id: str
    userid: str
    account: str
    period_kind: str
    period_id: str
    period_start: float
    period_end: float
    opening_balance: Decimal
    closing_balance: Decimal
    credits: Decimal
    debits: Decimal
    entry_count: int
    interim: bool
    generated_at: float
    generated_by: str
    entries: List[JournalEntry] = field(default_factory=list)

    def to_dict(self, *, include_entries: bool = False) -> Dict[str, Any]:
        payload = {
            'statement_id': self.statement_id,
            'occurrence_id': self.occurrence_id,
            'userid': self.userid,
            'account': self.account,
            'kind': self.period_kind,
            'period': self.period_id,
            'start': self.period_start,
            'end': self.period_end,
            'opening_balance': money_str(self.opening_balance),
            'closing_balance': money_str(self.closing_balance),
            'credits': money_str(self.credits),
            'debits': money_str(self.debits),
            'entry_count': self.entry_count,
            'interim': self.interim,
            'generated_at': self.generated_at,
            'generated_by': self.generated_by,
        }
        if include_entries:
            payload['entries'] = [row.to_dict() for row in self.entries]
        return payload


@dataclass
class StatementRequest:
    request_id: str
    userid: str
    account: str
    statement_id: str
    delivery: str
    status: str
    reason: str
    created_at: float
    decided_at: float = 0.0
    decided_by: str = ''
    note: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'request_id': self.request_id,
            'userid': self.userid,
            'account': self.account,
            'statement_id': self.statement_id,
            'delivery': self.delivery,
            'status': self.status,
            'reason': self.reason,
            'created_at': self.created_at,
            'decided_at': self.decided_at,
            'decided_by': self.decided_by,
            'note': self.note,
        }


def _copy_entry(item: JournalEntry) -> JournalEntry:
    return JournalEntry(
        entry_id=item.entry_id,
        userid=item.userid,
        account=item.account,
        kind=item.kind,
        direction=item.direction,
        amount=item.amount,
        balance=item.balance,
        counterparty=item.counterparty,
        description=item.description,
        source_id=item.source_id,
        posted_at=item.posted_at,
    )


def _copy_statement(item: Statement, *, include_entries: bool = True) -> Statement:
    return Statement(
        statement_id=item.statement_id,
        occurrence_id=item.occurrence_id,
        userid=item.userid,
        account=item.account,
        period_kind=item.period_kind,
        period_id=item.period_id,
        period_start=item.period_start,
        period_end=item.period_end,
        opening_balance=item.opening_balance,
        closing_balance=item.closing_balance,
        credits=item.credits,
        debits=item.debits,
        entry_count=item.entry_count,
        interim=item.interim,
        generated_at=item.generated_at,
        generated_by=item.generated_by,
        entries=[_copy_entry(row) for row in item.entries] if include_entries else list(item.entries),
    )


class MemoryStatementStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.entries: Dict[str, JournalEntry] = {}
        self.by_source: Dict[str, str] = {}
        self.statements: Dict[str, Statement] = {}
        self.by_occurrence: Dict[str, str] = {}
        self.requests: Dict[str, StatementRequest] = {}

    def put_entry(self, item: JournalEntry) -> JournalEntry:
        with self._lock:
            if item.source_id and item.source_id in self.by_source:
                return _copy_entry(self.entries[self.by_source[item.source_id]])
            self.entries[item.entry_id] = _copy_entry(item)
            if item.source_id:
                self.by_source[item.source_id] = item.entry_id
            return _copy_entry(item)

    def get_entry(self, entry_id: str) -> Optional[JournalEntry]:
        with self._lock:
            item = self.entries.get(entry_id)
            return None if item is None else _copy_entry(item)

    def get_by_source(self, source_id: str) -> Optional[JournalEntry]:
        with self._lock:
            entry_id = self.by_source.get(source_id)
            if not entry_id:
                return None
            return _copy_entry(self.entries[entry_id])

    def last_entry_before(self, account: str, ts: float) -> Optional[JournalEntry]:
        with self._lock:
            matches = [
                item for item in self.entries.values()
                if item.account == account and item.posted_at < ts
            ]
            if not matches:
                return None
            matches.sort(key=lambda item: (item.posted_at, item.entry_id))
            return _copy_entry(matches[-1])

    def last_entry(self, account: str) -> Optional[JournalEntry]:
        with self._lock:
            matches = [item for item in self.entries.values() if item.account == account]
            if not matches:
                return None
            matches.sort(key=lambda item: (item.posted_at, item.entry_id))
            return _copy_entry(matches[-1])

    def list_entries(
        self,
        *,
        userid: Optional[str] = None,
        account: Optional[str] = None,
        start: Optional[float] = None,
        end: Optional[float] = None,
    ) -> List[JournalEntry]:
        with self._lock:
            items = list(self.entries.values())
            if userid is not None:
                items = [item for item in items if item.userid == userid]
            if account is not None:
                items = [item for item in items if item.account == account]
            if start is not None:
                items = [item for item in items if item.posted_at >= start]
            if end is not None:
                items = [item for item in items if item.posted_at < end]
            items.sort(key=lambda item: (item.posted_at, item.entry_id))
            return [_copy_entry(item) for item in items]

    def put_statement(self, item: Statement) -> None:
        with self._lock:
            self.statements[item.statement_id] = _copy_statement(item)
            self.by_occurrence[item.occurrence_id] = item.statement_id

    def get_statement(self, statement_id: str) -> Optional[Statement]:
        with self._lock:
            item = self.statements.get(statement_id)
            return None if item is None else _copy_statement(item)

    def get_by_occurrence(self, occurrence_id: str) -> Optional[Statement]:
        with self._lock:
            statement_id = self.by_occurrence.get(occurrence_id)
            if not statement_id:
                return None
            return _copy_statement(self.statements[statement_id])

    def list_statements(self, userid: Optional[str] = None, account: Optional[str] = None) -> List[Statement]:
        with self._lock:
            items = list(self.statements.values())
            if userid is not None:
                items = [item for item in items if item.userid == userid]
            if account is not None:
                items = [item for item in items if item.account == account]
            items.sort(key=lambda item: item.generated_at, reverse=True)
            return [_copy_statement(item) for item in items]

    def put_request(self, item: StatementRequest) -> None:
        with self._lock:
            self.requests[item.request_id] = item

    def get_request(self, request_id: str) -> Optional[StatementRequest]:
        with self._lock:
            return self.requests.get(request_id)

    def update_request(self, item: StatementRequest) -> None:
        self.put_request(item)

    def list_requests(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[StatementRequest]:
        with self._lock:
            items = list(self.requests.values())
            if userid is not None:
                items = [item for item in items if item.userid == userid]
            if status is not None:
                items = [item for item in items if item.status == status]
            items.sort(key=lambda item: item.created_at, reverse=True)
            return items


class SqliteStatementStore:
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
                CREATE TABLE IF NOT EXISTS statement_journal (
                    entry_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    balance TEXT NOT NULL,
                    counterparty TEXT NOT NULL,
                    description TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    posted_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS statement_journal_source
                    ON statement_journal(source_id) WHERE source_id != '';
                CREATE INDEX IF NOT EXISTS statement_journal_account_time
                    ON statement_journal(account, posted_at);
                CREATE TABLE IF NOT EXISTS statements (
                    statement_id TEXT PRIMARY KEY,
                    occurrence_id TEXT NOT NULL UNIQUE,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL,
                    period_kind TEXT NOT NULL,
                    period_id TEXT NOT NULL,
                    period_start REAL NOT NULL,
                    period_end REAL NOT NULL,
                    opening_balance TEXT NOT NULL,
                    closing_balance TEXT NOT NULL,
                    credits TEXT NOT NULL,
                    debits TEXT NOT NULL,
                    entry_count INTEGER NOT NULL,
                    interim INTEGER NOT NULL,
                    generated_at REAL NOT NULL,
                    generated_by TEXT NOT NULL,
                    entries_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS statement_requests (
                    request_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL,
                    statement_id TEXT NOT NULL,
                    delivery TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    decided_at REAL NOT NULL,
                    decided_by TEXT NOT NULL,
                    note TEXT NOT NULL
                );
                """
            )

    def put_entry(self, item: JournalEntry) -> JournalEntry:
        with self._lock, self._connect() as conn:
            if item.source_id:
                row = conn.execute(
                    'SELECT * FROM statement_journal WHERE source_id=?', (item.source_id,)
                ).fetchone()
                if row is not None:
                    return self._entry_from_row(row)
            conn.execute(
                """
                INSERT INTO statement_journal (
                    entry_id, userid, account, kind, direction, amount, balance,
                    counterparty, description, source_id, posted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.entry_id, item.userid, item.account, item.kind, item.direction,
                    money_str(item.amount), money_str(item.balance), item.counterparty,
                    item.description, item.source_id, item.posted_at,
                ),
            )
        return _copy_entry(item)

    def get_entry(self, entry_id: str) -> Optional[JournalEntry]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM statement_journal WHERE entry_id=?', (entry_id,)
            ).fetchone()
        return None if row is None else self._entry_from_row(row)

    def get_by_source(self, source_id: str) -> Optional[JournalEntry]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM statement_journal WHERE source_id=?', (source_id,)
            ).fetchone()
        return None if row is None else self._entry_from_row(row)

    def last_entry_before(self, account: str, ts: float) -> Optional[JournalEntry]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM statement_journal
                WHERE account=? AND posted_at < ?
                ORDER BY posted_at DESC, entry_id DESC LIMIT 1
                """,
                (account, ts),
            ).fetchone()
        return None if row is None else self._entry_from_row(row)

    def last_entry(self, account: str) -> Optional[JournalEntry]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM statement_journal
                WHERE account=?
                ORDER BY posted_at DESC, entry_id DESC LIMIT 1
                """,
                (account,),
            ).fetchone()
        return None if row is None else self._entry_from_row(row)

    def list_entries(
        self,
        *,
        userid: Optional[str] = None,
        account: Optional[str] = None,
        start: Optional[float] = None,
        end: Optional[float] = None,
    ) -> List[JournalEntry]:
        clauses = []
        params: List[Any] = []
        if userid is not None:
            clauses.append('userid=?')
            params.append(userid)
        if account is not None:
            clauses.append('account=?')
            params.append(account)
        if start is not None:
            clauses.append('posted_at>=?')
            params.append(start)
        if end is not None:
            clauses.append('posted_at<?')
            params.append(end)
        sql = 'SELECT * FROM statement_journal'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY posted_at ASC, entry_id ASC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._entry_from_row(row) for row in rows]

    def put_statement(self, item: Statement) -> None:
        payload = json.dumps([row.to_dict() for row in item.entries])
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO statements (
                    statement_id, occurrence_id, userid, account, period_kind,
                    period_id, period_start, period_end, opening_balance,
                    closing_balance, credits, debits, entry_count, interim,
                    generated_at, generated_by, entries_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.statement_id, item.occurrence_id, item.userid, item.account,
                    item.period_kind, item.period_id, item.period_start, item.period_end,
                    money_str(item.opening_balance), money_str(item.closing_balance),
                    money_str(item.credits), money_str(item.debits), item.entry_count,
                    1 if item.interim else 0, item.generated_at, item.generated_by, payload,
                ),
            )

    def get_statement(self, statement_id: str) -> Optional[Statement]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM statements WHERE statement_id=?', (statement_id,)
            ).fetchone()
        return None if row is None else self._statement_from_row(row)

    def get_by_occurrence(self, occurrence_id: str) -> Optional[Statement]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM statements WHERE occurrence_id=?', (occurrence_id,)
            ).fetchone()
        return None if row is None else self._statement_from_row(row)

    def list_statements(self, userid: Optional[str] = None, account: Optional[str] = None) -> List[Statement]:
        clauses = []
        params: List[Any] = []
        if userid is not None:
            clauses.append('userid=?')
            params.append(userid)
        if account is not None:
            clauses.append('account=?')
            params.append(account)
        sql = 'SELECT * FROM statements'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY generated_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._statement_from_row(row) for row in rows]

    def put_request(self, item: StatementRequest) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO statement_requests (
                    request_id, userid, account, statement_id, delivery, status,
                    reason, created_at, decided_at, decided_by, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.request_id, item.userid, item.account, item.statement_id,
                    item.delivery, item.status, item.reason, item.created_at,
                    item.decided_at, item.decided_by, item.note,
                ),
            )

    def get_request(self, request_id: str) -> Optional[StatementRequest]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM statement_requests WHERE request_id=?', (request_id,)
            ).fetchone()
        return None if row is None else self._request_from_row(row)

    def update_request(self, item: StatementRequest) -> None:
        self.put_request(item)

    def list_requests(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[StatementRequest]:
        clauses = []
        params: List[Any] = []
        if userid is not None:
            clauses.append('userid=?')
            params.append(userid)
        if status is not None:
            clauses.append('status=?')
            params.append(status)
        sql = 'SELECT * FROM statement_requests'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._request_from_row(row) for row in rows]

    @staticmethod
    def _entry_from_row(row: sqlite3.Row) -> JournalEntry:
        return JournalEntry(
            entry_id=row['entry_id'],
            userid=row['userid'],
            account=row['account'],
            kind=row['kind'],
            direction=row['direction'],
            amount=Decimal(row['amount']),
            balance=Decimal(row['balance']),
            counterparty=row['counterparty'] or '',
            description=row['description'] or '',
            source_id=row['source_id'] or '',
            posted_at=float(row['posted_at']),
        )

    @staticmethod
    def _statement_from_row(row: sqlite3.Row) -> Statement:
        raw = json.loads(row['entries_json'] or '[]')
        entries = [
            JournalEntry(
                entry_id=item.get('entry_id', ''),
                userid=item.get('userid', ''),
                account=item.get('account', ''),
                kind=item.get('kind', ''),
                direction=item.get('direction', ''),
                amount=Decimal(str(item.get('amount') or '0')),
                balance=Decimal(str(item.get('balance') or '0')),
                counterparty=item.get('counterparty', ''),
                description=item.get('description', ''),
                source_id=item.get('source_id', ''),
                posted_at=float(item.get('posted_at') or 0),
            )
            for item in raw
        ]
        return Statement(
            statement_id=row['statement_id'],
            occurrence_id=row['occurrence_id'],
            userid=row['userid'],
            account=row['account'],
            period_kind=row['period_kind'],
            period_id=row['period_id'],
            period_start=float(row['period_start']),
            period_end=float(row['period_end']),
            opening_balance=Decimal(row['opening_balance']),
            closing_balance=Decimal(row['closing_balance']),
            credits=Decimal(row['credits']),
            debits=Decimal(row['debits']),
            entry_count=int(row['entry_count']),
            interim=bool(row['interim']),
            generated_at=float(row['generated_at']),
            generated_by=row['generated_by'] or '',
            entries=entries,
        )

    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> StatementRequest:
        return StatementRequest(
            request_id=row['request_id'],
            userid=row['userid'],
            account=row['account'],
            statement_id=row['statement_id'],
            delivery=row['delivery'],
            status=row['status'],
            reason=row['reason'] or '',
            created_at=float(row['created_at']),
            decided_at=float(row['decided_at']),
            decided_by=row['decided_by'] or '',
            note=row['note'] or '',
        )


class StatementService:
    def __init__(
        self,
        policy: Optional[StatementPolicy] = None,
        store: Any = None,
        *,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.policy = policy or StatementPolicy()
        self.store = store or MemoryStatementStore()
        self.clock = clock or (lambda: datetime.now(tz=timezone.utc).timestamp())

    def _assert_owner(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: str,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if actor != owner_userid:
            raise StatementError('userid_mismatch', 'User ID mismatch')
        owned = _own_account_set(own_accounts)
        if owned and account not in owned:
            raise StatementError('statement_forbidden', 'Account is not owned by this customer')

    def observe(
        self,
        account: Any,
        amount: Any,
        kind: Any,
        *,
        direction: Any = None,
        userid: Optional[str] = None,
        balance: Any = None,
        counterparty: Any = '',
        description: Any = '',
        source_id: Any = None,
        posted_at: Optional[float] = None,
    ) -> Optional[JournalEntry]:
        try:
            account_no = normalize_account(account)
            parsed_amount = parse_money(amount)
            entry_kind = normalize_kind(kind)
            entry_direction = normalize_direction(direction, entry_kind)
        except (AccountError, AmountError, StatementError):
            return None
        source = str(source_id or '').strip()
        if source:
            existing = self.store.get_by_source(source)
            if existing is not None:
                return existing
        last = self.store.last_entry(account_no)
        if balance is not None and str(balance).strip() != '':
            try:
                running = parse_balance(balance)
            except AmountError:
                running = (last.balance if last else Decimal('0.00')) + signed_amount(parsed_amount, entry_direction)
        else:
            prior = last.balance if last else Decimal('0.00')
            running = prior + signed_amount(parsed_amount, entry_direction)
        note = str(description or '').strip()
        if len(note) > self.policy.max_description:
            note = note[: self.policy.max_description]
        entry = JournalEntry(
            entry_id=str(uuid.uuid4()),
            userid=str(userid or (last.userid if last else '')),
            account=account_no,
            kind=entry_kind,
            direction=entry_direction,
            amount=parsed_amount,
            balance=running.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN),
            counterparty=str(counterparty or '').strip(),
            description=note,
            source_id=source,
            posted_at=float(posted_at if posted_at is not None else self.clock()),
        )
        return self.store.put_entry(entry)

    def journal(
        self,
        *,
        userid: Optional[str] = None,
        account: Any = None,
        start: Optional[float] = None,
        end: Optional[float] = None,
    ) -> List[JournalEntry]:
        account_no = normalize_account(account) if account not in (None, '') else None
        return self.store.list_entries(userid=userid, account=account_no, start=start, end=end)

    def _compose(self, owner_userid: str, account: str, spec: PeriodSpec, actor: str) -> Statement:
        prior = self.store.last_entry_before(account, spec.start_ts)
        opening = prior.balance if prior else Decimal('0.00')
        rows = self.store.list_entries(account=account, start=spec.start_ts, end=spec.end_ts)
        credits = sum((row.amount for row in rows if row.direction == 'credit'), Decimal('0.00'))
        debits = sum((row.amount for row in rows if row.direction == 'debit'), Decimal('0.00'))
        closing = rows[-1].balance if rows else opening
        return Statement(
            statement_id=str(uuid.uuid4()),
            occurrence_id=f'{account}:{spec.occurrence_suffix}',
            userid=owner_userid,
            account=account,
            period_kind=spec.kind,
            period_id=spec.period_id,
            period_start=spec.start_ts,
            period_end=spec.end_ts,
            opening_balance=opening,
            closing_balance=closing,
            credits=credits.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN),
            debits=debits.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN),
            entry_count=len(rows),
            interim=spec.interim,
            generated_at=self.clock(),
            generated_by=actor,
            entries=rows,
        )

    def generate(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        kind: Any = 'monthly',
        period: Any = None,
        start: Any = None,
        end: Any = None,
        force: bool = False,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> Tuple[Statement, bool]:
        owner = str(owner_userid or '').strip()
        if not owner:
            raise StatementError('missing_customer_id', 'Customer id is required')
        account_no = normalize_account(account)
        self._assert_owner(
            owner_userid=owner, actor=actor, actor_type=actor_type,
            account=account_no, own_accounts=own_accounts,
        )
        spec = resolve_period(kind, period=period, start=start, end=end, now=self.clock(), policy=self.policy)
        occurrence = f'{account_no}:{spec.occurrence_suffix}'
        existing = self.store.get_by_occurrence(occurrence)
        if existing is not None and not (force and actor_type in EMPLOYEE_ROLES):
            return existing, False
        existing_count = len(self.store.list_statements(userid=owner, account=account_no))
        if existing is None and existing_count >= self.policy.max_statements_per_account:
            raise StatementError('statement_limit', 'Statement limit reached for this account')
        item = self._compose(owner, account_no, spec, actor)
        if existing is not None:
            item.statement_id = existing.statement_id
            item.occurrence_id = existing.occurrence_id
        self.store.put_statement(item)
        return item, existing is None

    def get(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        statement_id: Any,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> Statement:
        item = self.store.get_statement(str(statement_id or '').strip())
        if item is None:
            raise StatementError('statement_not_found', 'Statement not found')
        if actor_type not in EMPLOYEE_ROLES and item.userid != actor:
            raise StatementError('statement_forbidden', 'Statement is not owned by this customer')
        if owner_userid and actor_type in EMPLOYEE_ROLES and item.userid != owner_userid:
            raise StatementError('statement_forbidden', 'Statement does not belong to this customer')
        self._assert_owner(
            owner_userid=item.userid, actor=actor, actor_type=actor_type,
            account=item.account, own_accounts=own_accounts,
        )
        return item

    def request(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        statement_id: Any = None,
        delivery: Any = 'mail',
        reason: Any = '',
        kind: Any = 'monthly',
        period: Any = None,
        start: Any = None,
        end: Any = None,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> StatementRequest:
        owner = str(owner_userid or '').strip()
        if not owner:
            raise StatementError('missing_customer_id', 'Customer id is required')
        account_no = normalize_account(account)
        self._assert_owner(
            owner_userid=owner, actor=actor, actor_type=actor_type,
            account=account_no, own_accounts=own_accounts,
        )
        if statement_id:
            statement = self.get(
                owner_userid=owner, actor=actor, actor_type=actor_type,
                statement_id=statement_id, own_accounts=own_accounts,
            )
        else:
            statement, _created = self.generate(
                owner_userid=owner, actor=actor, actor_type=actor_type,
                account=account_no, kind=kind, period=period, start=start, end=end,
                own_accounts=own_accounts,
            )
        pending = self.store.list_requests(userid=owner, status='pending')
        if any(item.statement_id == statement.statement_id and item.delivery == normalize_delivery(delivery) for item in pending):
            raise StatementError('request_duplicate', 'An official copy is already pending for this statement')
        if len(pending) >= self.policy.max_pending_requests:
            raise StatementError('request_limit', 'Too many pending statement requests')
        item = StatementRequest(
            request_id=str(uuid.uuid4()),
            userid=owner,
            account=account_no,
            statement_id=statement.statement_id,
            delivery=normalize_delivery(delivery),
            status='pending',
            reason=str(reason or '').strip()[: self.policy.max_description],
            created_at=self.clock(),
        )
        self.store.put_request(item)
        return item

    def decide_request(
        self,
        *,
        actor: str,
        actor_type: str,
        request_id: Any,
        approve: bool,
        note: Any = '',
    ) -> StatementRequest:
        if actor_type not in EMPLOYEE_ROLES:
            raise StatementError('statement_forbidden', 'Only staff can fulfill official statement requests')
        item = self.store.get_request(str(request_id or '').strip())
        if item is None:
            raise StatementError('request_not_found', 'Statement request not found')
        if item.status != 'pending':
            raise StatementError('request_not_pending', 'Statement request is no longer pending')
        item.status = 'approved' if approve else 'denied'
        item.decided_at = self.clock()
        item.decided_by = actor
        item.note = str(note or '').strip()[: self.policy.max_description]
        self.store.update_request(item)
        return item

    def available_periods(self, account: str) -> List[str]:
        first = None
        rows = self.store.list_entries(account=account)
        if rows:
            first = rows[0].posted_at
        now = self.clock()
        now_dt = utc_datetime(now)
        end_year, end_month = now_dt.year, now_dt.month
        if not self.policy.allow_interim:
            end_year, end_month = previous_month(end_year, end_month)
        if first is None:
            return [f'{end_year:04d}-{end_month:02d}']
        start_dt = utc_datetime(first)
        return months_between(start_dt.year, start_dt.month, end_year, end_month)

    def snapshot(self, userid: str, *, account: Any = None) -> Dict[str, Any]:
        owner = str(userid or '').strip()
        account_no = normalize_account(account, required=False) if account not in (None, '') else None
        statements = self.store.list_statements(userid=owner, account=account_no)
        requests = self.store.list_requests(userid=owner)
        accounts: Dict[str, Dict[str, Any]] = {}
        seen = {item.account for item in statements}
        journal_accounts = {item.account for item in self.store.list_entries(userid=owner)}
        if account_no:
            seen.add(account_no)
        seen.update(journal_accounts)
        for acct in sorted(seen):
            latest = next((item for item in statements if item.account == acct), None)
            last = self.store.last_entry(acct)
            accounts[acct] = {
                'account': acct,
                'entry_count': len(self.store.list_entries(account=acct)),
                'latest_statement': None if latest is None else latest.to_dict(),
                'available_periods': self.available_periods(acct),
                'current_period': month_id(self.clock()),
                'last_balance': money_str(last.balance) if last else '0.00',
            }
        return {
            'statements': [item.to_dict() for item in statements],
            'requests': [item.to_dict() for item in requests],
            'accounts': accounts,
        }


def set_service(service: Optional[StatementService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[StatementService]:
    return _SERVICE


def build_service(*, clock: Optional[Callable[[], float]] = None, store: Any = None) -> StatementService:
    if store is None:
        path = os.environ.get('STATEMENT_STORE') or DEFAULT_STORE_PATH
        if (os.environ.get('STATEMENT_STORE_BACKEND') or 'sqlite').strip().lower() == 'memory':
            store = MemoryStatementStore()
        else:
            store = SqliteStatementStore(path)
    return StatementService(StatementPolicy.from_env(), store, clock=clock)


def observe_movement(
    account: Any,
    amount: Any,
    kind: Any,
    *,
    direction: Any = None,
    userid: Optional[str] = None,
    balance: Any = None,
    counterparty: Any = '',
    description: Any = '',
    source_id: Any = None,
) -> None:
    service = get_service()
    if service is None:
        return
    try:
        service.observe(
            account, amount, kind,
            direction=direction, userid=userid, balance=balance,
            counterparty=counterparty, description=description, source_id=source_id,
        )
    except Exception:
        return


def observe_transfer(
    from_account: Any,
    to_account: Any,
    amount: Any,
    *,
    from_userid: Optional[str] = None,
    to_userid: Optional[str] = None,
    from_balance: Any = None,
    to_balance: Any = None,
    source_id: Any = None,
    deposit: bool = False,
) -> None:
    service = get_service()
    if service is None:
        return
    try:
        base = str(source_id or uuid.uuid4())
        if deposit:
            service.observe(
                to_account, amount, 'deposit', direction='credit',
                userid=to_userid, balance=to_balance, counterparty=from_account,
                description='deposit', source_id=f'{base}:deposit:{to_account}',
            )
            return
        service.observe(
            from_account, amount, 'transfer_out', direction='debit',
            userid=from_userid, balance=from_balance, counterparty=to_account,
            description='transfer out', source_id=f'{base}:out:{from_account}',
        )
        service.observe(
            to_account, amount, 'transfer_in', direction='credit',
            userid=to_userid, balance=to_balance, counterparty=from_account,
            description='transfer in', source_id=f'{base}:in:{to_account}',
        )
    except Exception:
        return


def _flask():
    from flask import jsonify, request, session
    return jsonify, request, session


def _require_session_user():
    jsonify, request, session = _flask()
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
        'statement_limit': 409,
        'statement_forbidden': 403,
        'userid_mismatch': 403,
        'statement_not_found': 404,
        'request_not_found': 404,
        'invalid_account': 400,
        'invalid_amount': 400,
        'invalid_period': 400,
        'invalid_kind': 400,
        'invalid_delivery': 400,
        'period_in_future': 400,
        'period_not_closed': 400,
        'range_too_long': 400,
        'missing_customer_id': 400,
    }.get(code, 400)


def _error_body(exc: StatementError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    for key, value in exc.extra.items():
        if value is not None:
            body[key] = value
    return body


def _own_accounts(loader, userid, actor_type):
    if actor_type == 'customer' and callable(loader):
        return loader(userid)
    return None


def handle_generate(service: StatementService, own_accounts_loader=None):
    jsonify, request, session = _flask()
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    try:
        item, created = service.generate(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account') or values.get('account_no'),
            kind=values.get('kind') or values.get('period_kind') or 'monthly',
            period=values.get('period'),
            start=values.get('start'),
            end=values.get('end'),
            force=bool(values.get('force')) and actor_type in EMPLOYEE_ROLES,
            own_accounts=_own_accounts(own_accounts_loader, userid, actor_type),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except PeriodError:
        return jsonify({'message': 'Invalid statement period', 'error': 'invalid_period'}), 400
    except AmountError:
        return jsonify({'message': 'Enter a valid amount', 'error': 'invalid_amount'}), 400
    except StatementError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Statement generated' if created else 'Statement already generated',
        'already_generated': not created,
        'statement': item.to_dict(include_entries=True),
        'Statements': service.snapshot(owner),
    }), (201 if created else 200)


def handle_list(service: StatementService):
    jsonify, request, session = _flask()
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
    return jsonify({'Statements': service.snapshot(owner, account=values.get('account'))}), 200


def handle_get(service: StatementService, own_accounts_loader=None):
    jsonify, request, session = _flask()
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values) or userid
    try:
        item = service.get(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            statement_id=values.get('statement_id'),
            own_accounts=_own_accounts(own_accounts_loader, userid, actor_type),
        )
    except StatementError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Statement',
        'statement': item.to_dict(include_entries=True),
        'Statements': service.snapshot(item.userid),
    }), 200


def handle_request(service: StatementService, own_accounts_loader=None):
    jsonify, request, session = _flask()
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values) or userid
    try:
        req = service.request(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account') or values.get('account_no'),
            statement_id=values.get('statement_id'),
            delivery=values.get('delivery') or 'mail',
            reason=values.get('reason'),
            kind=values.get('kind') or 'monthly',
            period=values.get('period'),
            start=values.get('start'),
            end=values.get('end'),
            own_accounts=_own_accounts(own_accounts_loader, userid, actor_type),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except PeriodError:
        return jsonify({'message': 'Invalid statement period', 'error': 'invalid_period'}), 400
    except StatementError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Official statement requested',
        'request': req.to_dict(),
        'Statements': service.snapshot(owner),
    }), 201


def handle_decide(service: StatementService):
    jsonify, request, session = _flask()
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    decision = str(values.get('decision') or '').strip().lower()
    approve = decision in {'approve', 'approved', 'yes', '1', 'true', 'mailed'}
    deny = decision in {'deny', 'denied', 'no', '0', 'false'}
    if not approve and not deny:
        return jsonify({'message': 'Decision must be approve or deny', 'error': 'invalid_decision'}), 400
    try:
        req = service.decide_request(
            actor=userid,
            actor_type=actor_type,
            request_id=values.get('request_id'),
            approve=approve,
            note=values.get('note'),
        )
    except StatementError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Statement request ' + req.status,
        'request': req.to_dict(),
        'Statements': service.snapshot(req.userid),
    }), 200


def attach_statement_routes(app, service: StatementService, own_accounts_loader=None) -> None:
    @app.route('/generateStatement', methods=['POST', 'GET'])
    def generate_statement_route():
        return handle_generate(service, own_accounts_loader=own_accounts_loader)

    @app.route('/listStatements', methods=['POST', 'GET'])
    def list_statements_route():
        return handle_list(service)

    @app.route('/getStatement', methods=['POST', 'GET'])
    def get_statement_route():
        return handle_get(service, own_accounts_loader=own_accounts_loader)

    @app.route('/requestStatement', methods=['POST', 'GET'])
    def request_statement_route():
        return handle_request(service, own_accounts_loader=own_accounts_loader)

    @app.route('/decideStatementRequest', methods=['POST', 'GET'])
    def decide_statement_route():
        return handle_decide(service)
