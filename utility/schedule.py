"""Scheduled and recurring outbound transfers.

Customers register a calendar instruction; when it is due the existing money
handler runs (same employee-approval queue as `/fundTransfer`). This is not
the destination cooling hold (PR #30), not the payee allowlist (PR #26), and
not a freeze (PR #34). Immediate `/fundTransfer` is unchanged.

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
"""

from __future__ import annotations

import calendar
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from flask import jsonify, request, session

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
INTERVALS = frozenset({'once', 'daily', 'weekly', 'monthly'})
INTERVAL_ALIASES = {
    'once': 'once',
    'one': 'once',
    '1': 'once',
    'daily': 'daily',
    'day': 'daily',
    '1d': 'daily',
    'weekly': 'weekly',
    'week': 'weekly',
    '7d': 'weekly',
    'monthly': 'monthly',
    'month': 'monthly',
    '1m': 'monthly',
}
ACTIVE_STATUSES = frozenset({'active', 'paused'})
SCHEDULE_STATUSES = frozenset({'active', 'paused', 'cancelled', 'completed', 'failed'})
OCCURRENCE_STATUSES = frozenset({'settled', 'skipped', 'failed'})
MONEY_QUANTUM = Decimal('0.01')
MAX_AMOUNT = Decimal('1000000000')
CATCH_UP_ONE = 'one'
CATCH_UP_ALL = 'all'
CATCH_UP_SKIP = 'skip'


class AmountError(ValueError):
    pass


class AccountError(ValueError):
    pass


class WhenError(ValueError):
    pass


class ScheduleError(ValueError):
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


def normalize_interval(value: Any, *, default: str = 'once') -> str:
    text = str(value or default).strip().lower() or default
    mapped = INTERVAL_ALIASES.get(text, text)
    if mapped not in INTERVALS:
        raise ScheduleError('invalid_interval', 'Unknown recurrence interval.')
    return mapped


def parse_when(value: Any, *, required: bool = False) -> Optional[float]:
    if value is None or value == '':
        if required:
            raise WhenError('invalid_when')
        return None
    if isinstance(value, bool):
        raise WhenError('invalid_when')
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp < 0:
            raise WhenError('invalid_when')
        return stamp
    text = str(value).strip()
    if not text:
        if required:
            raise WhenError('invalid_when')
        return None
    if text.replace('.', '', 1).isdigit():
        return float(text)
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise WhenError('invalid_when') from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


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


def _env_money(name: str, default: str) -> Decimal:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return parse_money(default)
    return parse_money(raw)


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


def add_calendar_months(stamp: float, months: int) -> float:
    dt = datetime.fromtimestamp(stamp, tz=timezone.utc)
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return datetime(
        year, month, day, dt.hour, dt.minute, dt.second, dt.microsecond, tzinfo=timezone.utc
    ).timestamp()


def next_occurrence(stamp: float, interval: str) -> float:
    if interval == 'once':
        return stamp
    if interval == 'daily':
        return stamp + 86400.0
    if interval == 'weekly':
        return stamp + 7 * 86400.0
    if interval == 'monthly':
        return add_calendar_months(stamp, 1)
    raise ScheduleError('invalid_interval', 'Unknown recurrence interval.')


def advance_past(stamp: float, interval: str, now: float) -> float:
    """First occurrence strictly after `now`, starting from `stamp`."""
    if interval == 'once':
        return stamp
    cursor = stamp
    # Guard against infinite loops on bad clocks.
    for _ in range(10000):
        if cursor > now:
            return cursor
        nxt = next_occurrence(cursor, interval)
        if nxt <= cursor:
            return cursor + 86400.0
        cursor = nxt
    return cursor


def occurrence_id_for(schedule_id: str, due_at: float) -> str:
    return '%s:%d' % (schedule_id, int(due_at))


@dataclass(frozen=True)
class SchedulePolicy:
    enabled: bool = True
    min_lead_seconds: int = 60
    max_open: int = 20
    max_amount: Decimal = field(default_factory=lambda: Decimal('10000.00'))
    catch_up: str = CATCH_UP_ONE
    skip_on_error: bool = True
    allow_same_account: bool = False

    @classmethod
    def from_env(cls) -> 'SchedulePolicy':
        catch_up = os.environ.get('SCHEDULE_CATCH_UP', CATCH_UP_ONE).strip().lower() or CATCH_UP_ONE
        if catch_up not in {CATCH_UP_ONE, CATCH_UP_ALL, CATCH_UP_SKIP}:
            catch_up = CATCH_UP_ONE
        return cls(
            enabled=_env_bool('SCHEDULE_ENABLED', True),
            min_lead_seconds=max(0, _env_int('SCHEDULE_MIN_LEAD_SECONDS', 60)),
            max_open=max(1, _env_int('SCHEDULE_MAX_OPEN', 20)),
            max_amount=_env_money('SCHEDULE_MAX_AMOUNT', '10000.00'),
            catch_up=catch_up,
            skip_on_error=_env_bool('SCHEDULE_SKIP_ON_ERROR', True),
            allow_same_account=_env_bool('SCHEDULE_ALLOW_SAME_ACCOUNT', False),
        )


@dataclass
class Schedule:
    schedule_id: str
    userid: str
    from_account: str
    to_account: str
    amount: str
    interval: str
    status: str
    start_at: float
    next_run: float
    created_at: float
    actor: str
    actor_type: str
    note: str = ''
    end_at: Optional[float] = None
    max_occurrences: Optional[int] = None
    run_count: int = 0
    last_run_at: Optional[float] = None
    last_result: str = ''
    cancelled_at: Optional[float] = None
    cancelled_by: Optional[str] = None
    completed_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'schedule_id': self.schedule_id,
            'userid': self.userid,
            'from_account': self.from_account,
            'to_account': self.to_account,
            'amount': self.amount,
            'interval': self.interval,
            'status': self.status,
            'start_at': self.start_at,
            'next_run': self.next_run,
            'created_at': self.created_at,
            'actor': self.actor,
            'actor_type': self.actor_type,
            'note': self.note,
            'end_at': self.end_at,
            'max_occurrences': self.max_occurrences,
            'run_count': self.run_count,
            'last_run_at': self.last_run_at,
            'last_result': self.last_result,
            'cancelled_at': self.cancelled_at,
            'cancelled_by': self.cancelled_by,
            'completed_at': self.completed_at,
        }

    def fingerprint(self) -> Tuple[str, str, str, str, str]:
        return (self.userid, self.from_account, self.to_account, self.amount, self.interval)


@dataclass
class Occurrence:
    occurrence_id: str
    schedule_id: str
    userid: str
    due_at: float
    ran_at: float
    status: str
    result: str
    from_account: str
    to_account: str
    amount: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'occurrence_id': self.occurrence_id,
            'schedule_id': self.schedule_id,
            'userid': self.userid,
            'due_at': self.due_at,
            'ran_at': self.ran_at,
            'status': self.status,
            'result': self.result,
            'from_account': self.from_account,
            'to_account': self.to_account,
            'amount': self.amount,
        }


class MemoryScheduleStore:
    def __init__(self) -> None:
        self._schedules: Dict[str, Schedule] = {}
        self._occurrences: Dict[str, Occurrence] = {}
        self._lock = threading.Lock()

    def put_schedule(self, schedule: Schedule) -> None:
        with self._lock:
            self._schedules[schedule.schedule_id] = schedule

    def get_schedule(self, schedule_id: str) -> Optional[Schedule]:
        with self._lock:
            return self._schedules.get(schedule_id)

    def update_schedule(self, schedule: Schedule) -> None:
        with self._lock:
            self._schedules[schedule.schedule_id] = schedule

    def list_schedules(
        self,
        userid: Optional[str] = None,
        statuses: Optional[Iterable[str]] = None,
    ) -> List[Schedule]:
        wanted = set(statuses) if statuses is not None else None
        with self._lock:
            rows = list(self._schedules.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if wanted is not None:
            rows = [row for row in rows if row.status in wanted]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows

    def due(self, now: float, userid: Optional[str] = None) -> List[Schedule]:
        rows = self.list_schedules(userid=userid, statuses={'active'})
        return [row for row in rows if row.next_run <= now]

    def put_occurrence(self, occurrence: Occurrence) -> None:
        with self._lock:
            self._occurrences[occurrence.occurrence_id] = occurrence

    def get_occurrence(self, occurrence_id: str) -> Optional[Occurrence]:
        with self._lock:
            return self._occurrences.get(occurrence_id)

    def list_occurrences(
        self,
        schedule_id: Optional[str] = None,
        userid: Optional[str] = None,
        limit: int = 50,
    ) -> List[Occurrence]:
        with self._lock:
            rows = list(self._occurrences.values())
        if schedule_id is not None:
            rows = [row for row in rows if row.schedule_id == schedule_id]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        rows.sort(key=lambda row: row.ran_at, reverse=True)
        return rows[:limit]


class SqliteScheduleStore:
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
                CREATE TABLE IF NOT EXISTS schedules (
                    schedule_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    from_account TEXT NOT NULL,
                    to_account TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    interval_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    start_at REAL NOT NULL,
                    next_run REAL NOT NULL,
                    created_at REAL NOT NULL,
                    actor TEXT NOT NULL,
                    actor_type TEXT NOT NULL,
                    note TEXT NOT NULL,
                    end_at REAL,
                    max_occurrences INTEGER,
                    run_count INTEGER NOT NULL,
                    last_run_at REAL,
                    last_result TEXT NOT NULL,
                    cancelled_at REAL,
                    cancelled_by TEXT,
                    completed_at REAL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS occurrences (
                    occurrence_id TEXT PRIMARY KEY,
                    schedule_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    due_at REAL NOT NULL,
                    ran_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT NOT NULL,
                    from_account TEXT NOT NULL,
                    to_account TEXT NOT NULL,
                    amount TEXT NOT NULL
                )
                """
            )
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_schedules_user ON schedules(userid, status)'
            )
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(status, next_run)'
            )
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_occ_schedule ON occurrences(schedule_id, due_at)'
            )
            conn.commit()

    def put_schedule(self, schedule: Schedule) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO schedules (
                    schedule_id, userid, from_account, to_account, amount, interval_name,
                    status, start_at, next_run, created_at, actor, actor_type, note,
                    end_at, max_occurrences, run_count, last_run_at, last_result,
                    cancelled_at, cancelled_by, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._schedule_row(schedule),
            )
            conn.commit()

    def get_schedule(self, schedule_id: str) -> Optional[Schedule]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM schedules WHERE schedule_id = ?', (schedule_id,)
            ).fetchone()
        return self._schedule_from_row(row) if row else None

    def update_schedule(self, schedule: Schedule) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE schedules SET
                    userid=?, from_account=?, to_account=?, amount=?, interval_name=?,
                    status=?, start_at=?, next_run=?, created_at=?, actor=?, actor_type=?,
                    note=?, end_at=?, max_occurrences=?, run_count=?, last_run_at=?,
                    last_result=?, cancelled_at=?, cancelled_by=?, completed_at=?
                WHERE schedule_id=?
                """,
                self._schedule_row(schedule)[1:] + (schedule.schedule_id,),
            )
            conn.commit()

    def list_schedules(
        self,
        userid: Optional[str] = None,
        statuses: Optional[Iterable[str]] = None,
    ) -> List[Schedule]:
        clauses = []
        params: List[Any] = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if statuses is not None:
            wanted = list(statuses)
            clauses.append('status IN (%s)' % ','.join('?' * len(wanted)))
            params.extend(wanted)
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM schedules %s ORDER BY created_at DESC' % where, params
            ).fetchall()
        return [self._schedule_from_row(row) for row in rows]

    def due(self, now: float, userid: Optional[str] = None) -> List[Schedule]:
        clauses = ['status = ?', 'next_run <= ?']
        params: List[Any] = ['active', now]
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM schedules WHERE %s ORDER BY next_run ASC'
                % ' AND '.join(clauses),
                params,
            ).fetchall()
        return [self._schedule_from_row(row) for row in rows]

    def put_occurrence(self, occurrence: Occurrence) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO occurrences (
                    occurrence_id, schedule_id, userid, due_at, ran_at, status,
                    result, from_account, to_account, amount
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._occurrence_row(occurrence),
            )
            conn.commit()

    def get_occurrence(self, occurrence_id: str) -> Optional[Occurrence]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM occurrences WHERE occurrence_id = ?', (occurrence_id,)
            ).fetchone()
        return self._occurrence_from_row(row) if row else None

    def list_occurrences(
        self,
        schedule_id: Optional[str] = None,
        userid: Optional[str] = None,
        limit: int = 50,
    ) -> List[Occurrence]:
        clauses = []
        params: List[Any] = []
        if schedule_id is not None:
            clauses.append('schedule_id = ?')
            params.append(schedule_id)
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM occurrences %s ORDER BY ran_at DESC LIMIT ?' % where,
                params + [int(limit)],
            ).fetchall()
        return [self._occurrence_from_row(row) for row in rows]

    @staticmethod
    def _schedule_row(schedule: Schedule) -> Tuple[Any, ...]:
        return (
            schedule.schedule_id,
            schedule.userid,
            schedule.from_account,
            schedule.to_account,
            schedule.amount,
            schedule.interval,
            schedule.status,
            schedule.start_at,
            schedule.next_run,
            schedule.created_at,
            schedule.actor,
            schedule.actor_type,
            schedule.note,
            schedule.end_at,
            schedule.max_occurrences,
            schedule.run_count,
            schedule.last_run_at,
            schedule.last_result,
            schedule.cancelled_at,
            schedule.cancelled_by,
            schedule.completed_at,
        )

    @staticmethod
    def _schedule_from_row(row: sqlite3.Row) -> Schedule:
        return Schedule(
            schedule_id=row['schedule_id'],
            userid=row['userid'],
            from_account=row['from_account'],
            to_account=row['to_account'],
            amount=row['amount'],
            interval=row['interval_name'],
            status=row['status'],
            start_at=row['start_at'],
            next_run=row['next_run'],
            created_at=row['created_at'],
            actor=row['actor'],
            actor_type=row['actor_type'],
            note=row['note'],
            end_at=row['end_at'],
            max_occurrences=row['max_occurrences'],
            run_count=row['run_count'],
            last_run_at=row['last_run_at'],
            last_result=row['last_result'],
            cancelled_at=row['cancelled_at'],
            cancelled_by=row['cancelled_by'],
            completed_at=row['completed_at'],
        )

    @staticmethod
    def _occurrence_row(occurrence: Occurrence) -> Tuple[Any, ...]:
        return (
            occurrence.occurrence_id,
            occurrence.schedule_id,
            occurrence.userid,
            occurrence.due_at,
            occurrence.ran_at,
            occurrence.status,
            occurrence.result,
            occurrence.from_account,
            occurrence.to_account,
            occurrence.amount,
        )

    @staticmethod
    def _occurrence_from_row(row: sqlite3.Row) -> Occurrence:
        return Occurrence(
            occurrence_id=row['occurrence_id'],
            schedule_id=row['schedule_id'],
            userid=row['userid'],
            due_at=row['due_at'],
            ran_at=row['ran_at'],
            status=row['status'],
            result=row['result'],
            from_account=row['from_account'],
            to_account=row['to_account'],
            amount=row['amount'],
        )


class ScheduleService:
    def __init__(
        self,
        policy: Optional[SchedulePolicy] = None,
        store: Optional[Any] = None,
        clock: Any = time.time,
        executor: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> None:
        self.policy = policy or SchedulePolicy()
        self.store = store or MemoryScheduleStore()
        self.clock = clock
        self.executor = executor
        self._run_lock = threading.Lock()

    def _now(self) -> float:
        return float(self.clock())

    def _actor_is_employee(self, actor_type: str) -> bool:
        return (actor_type or '') in EMPLOYEE_ROLES

    def create(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        from_account: Any,
        to_account: Any,
        amount: Any,
        interval: Any = 'once',
        start_at: Any = None,
        end_at: Any = None,
        max_occurrences: Any = None,
        note: str = '',
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> Schedule:
        if not self.policy.enabled:
            raise ScheduleError('schedule_disabled', 'Scheduled transfers are disabled.')
        owner = str(owner_userid or '').strip()
        if not owner:
            raise ScheduleError('missing_customer_id', 'Customer id is required.')
        source = normalize_account(from_account)
        dest = normalize_account(to_account)
        if source == dest and not self.policy.allow_same_account:
            raise ScheduleError('schedule_same_account', 'Source and destination must differ.')
        money = canonical_amount(amount)
        if parse_money(money) > self.policy.max_amount:
            raise ScheduleError(
                'per_txn_exceeded',
                'Amount exceeds the scheduled-transfer maximum.',
                max_amount=canonical_amount(self.policy.max_amount),
            )
        cadence = normalize_interval(interval)
        now = self._now()
        when = parse_when(start_at)
        if when is None:
            when = now + self.policy.min_lead_seconds
        if when < now + self.policy.min_lead_seconds - 0.001:
            raise ScheduleError(
                'schedule_too_soon',
                'Start time must be at least %s seconds from now.' % self.policy.min_lead_seconds,
                min_lead_seconds=self.policy.min_lead_seconds,
            )
        until = parse_when(end_at)
        if until is not None and until <= when:
            raise ScheduleError('invalid_end_at', 'End time must be after the first run.')
        limit = None
        if max_occurrences not in (None, '', 0, '0'):
            try:
                limit = int(max_occurrences)
            except (TypeError, ValueError) as exc:
                raise ScheduleError('invalid_max_occurrences', 'Invalid occurrence cap.') from exc
            if limit < 1:
                raise ScheduleError('invalid_max_occurrences', 'Invalid occurrence cap.')
        if cadence == 'once':
            limit = 1

        is_employee = self._actor_is_employee(actor_type)
        if not is_employee:
            owned = _own_account_set(own_accounts)
            if not owned or source not in owned:
                raise ScheduleError('schedule_forbidden', 'You can only schedule from your own accounts.')

        open_rows = self.store.list_schedules(userid=owner, statuses=ACTIVE_STATUSES)
        if len(open_rows) >= self.policy.max_open:
            raise ScheduleError('schedule_limit', 'Too many active scheduled transfers.')
        fingerprint = (owner, source, dest, money, cadence)
        for row in open_rows:
            if row.fingerprint() == fingerprint:
                raise ScheduleError(
                    'schedule_duplicate',
                    'An active schedule already exists for that destination and amount.',
                    schedule=row,
                )

        schedule = Schedule(
            schedule_id=str(uuid.uuid4()),
            userid=owner,
            from_account=source,
            to_account=dest,
            amount=money,
            interval=cadence,
            status='active',
            start_at=when,
            next_run=when,
            created_at=now,
            actor=str(actor),
            actor_type=str(actor_type or 'customer'),
            note=str(note or '')[:200],
            end_at=until,
            max_occurrences=limit,
        )
        self.store.put_schedule(schedule)
        return schedule

    def cancel(
        self,
        *,
        schedule_id: str,
        actor: str,
        actor_type: str,
        owner_userid: Optional[str] = None,
    ) -> Schedule:
        schedule = self.store.get_schedule(schedule_id)
        if schedule is None:
            raise ScheduleError('schedule_not_found', 'Scheduled transfer not found.')
        if not self._can_manage(schedule, actor, actor_type, owner_userid):
            raise ScheduleError('schedule_forbidden', 'Not allowed to cancel this schedule.')
        if schedule.status not in ACTIVE_STATUSES:
            raise ScheduleError('schedule_not_active', 'Schedule is not active.')
        schedule.status = 'cancelled'
        schedule.cancelled_at = self._now()
        schedule.cancelled_by = str(actor)
        self.store.update_schedule(schedule)
        return schedule

    def pause(
        self,
        *,
        schedule_id: str,
        actor: str,
        actor_type: str,
        owner_userid: Optional[str] = None,
    ) -> Schedule:
        schedule = self.store.get_schedule(schedule_id)
        if schedule is None:
            raise ScheduleError('schedule_not_found', 'Scheduled transfer not found.')
        if not self._can_manage(schedule, actor, actor_type, owner_userid):
            raise ScheduleError('schedule_forbidden', 'Not allowed to pause this schedule.')
        if schedule.status != 'active':
            raise ScheduleError('schedule_not_active', 'Schedule is not active.')
        schedule.status = 'paused'
        self.store.update_schedule(schedule)
        return schedule

    def resume(
        self,
        *,
        schedule_id: str,
        actor: str,
        actor_type: str,
        owner_userid: Optional[str] = None,
    ) -> Schedule:
        schedule = self.store.get_schedule(schedule_id)
        if schedule is None:
            raise ScheduleError('schedule_not_found', 'Scheduled transfer not found.')
        if not self._can_manage(schedule, actor, actor_type, owner_userid):
            raise ScheduleError('schedule_forbidden', 'Not allowed to resume this schedule.')
        if schedule.status != 'paused':
            raise ScheduleError('schedule_not_paused', 'Schedule is not paused.')
        now = self._now()
        if schedule.interval != 'once' and schedule.next_run <= now:
            schedule.next_run = advance_past(schedule.next_run, schedule.interval, now)
        elif schedule.interval == 'once' and schedule.next_run < now + self.policy.min_lead_seconds:
            schedule.next_run = now + self.policy.min_lead_seconds
        schedule.status = 'active'
        self.store.update_schedule(schedule)
        return schedule

    def _can_manage(
        self,
        schedule: Schedule,
        actor: str,
        actor_type: str,
        owner_userid: Optional[str],
    ) -> bool:
        if self._actor_is_employee(actor_type):
            return True
        if str(actor) != str(schedule.userid):
            return False
        if owner_userid is not None and str(owner_userid) != str(schedule.userid):
            return False
        return True

    def snapshot(self, userid: str) -> Dict[str, Any]:
        rows = self.store.list_schedules(userid=userid)
        open_rows = [row for row in rows if row.status in ACTIVE_STATUSES]
        recent = self.store.list_occurrences(userid=userid, limit=20)
        return {
            'enabled': self.policy.enabled,
            'min_lead_seconds': self.policy.min_lead_seconds,
            'max_open': self.policy.max_open,
            'max_amount': canonical_amount(self.policy.max_amount),
            'catch_up': self.policy.catch_up,
            'open_count': len(open_rows),
            'schedules': [row.to_dict() for row in rows],
            'occurrences': [row.to_dict() for row in recent],
        }

    def _should_complete(self, schedule: Schedule, candidate_next: float) -> bool:
        if schedule.interval == 'once':
            return True
        if schedule.max_occurrences is not None and schedule.run_count >= schedule.max_occurrences:
            return True
        if schedule.end_at is not None and candidate_next > schedule.end_at:
            return True
        return False

    def _mark_complete(self, schedule: Schedule, now: float) -> None:
        schedule.status = 'completed'
        schedule.completed_at = now
        self.store.update_schedule(schedule)

    def _execute_one(
        self,
        schedule: Schedule,
        due_at: float,
        executor: Callable[[Dict[str, Any]], Any],
        now: float,
    ) -> Occurrence:
        occ_id = occurrence_id_for(schedule.schedule_id, due_at)
        existing = self.store.get_occurrence(occ_id)
        if existing is not None:
            return existing
        payload = {
            'userid': schedule.userid,
            'from_account': schedule.from_account,
            'to_account': schedule.to_account,
            'amount': schedule.amount,
            'schedule_id': schedule.schedule_id,
            'due_at': due_at,
        }
        try:
            result = executor(payload)
            status = 'settled'
            text = '' if result is None else str(result)
        except Exception as exc:  # noqa: BLE001 — fail-open skip vs fail is policy
            if not self.policy.skip_on_error:
                schedule.status = 'failed'
                schedule.last_run_at = now
                schedule.last_result = str(exc)
                self.store.update_schedule(schedule)
                occurrence = Occurrence(
                    occurrence_id=occ_id,
                    schedule_id=schedule.schedule_id,
                    userid=schedule.userid,
                    due_at=due_at,
                    ran_at=now,
                    status='failed',
                    result=str(exc),
                    from_account=schedule.from_account,
                    to_account=schedule.to_account,
                    amount=schedule.amount,
                )
                self.store.put_occurrence(occurrence)
                raise
            status = 'failed'
            text = str(exc)
        occurrence = Occurrence(
            occurrence_id=occ_id,
            schedule_id=schedule.schedule_id,
            userid=schedule.userid,
            due_at=due_at,
            ran_at=now,
            status=status,
            result=text[:500],
            from_account=schedule.from_account,
            to_account=schedule.to_account,
            amount=schedule.amount,
        )
        self.store.put_occurrence(occurrence)
        if status == 'settled':
            schedule.run_count += 1
        schedule.last_run_at = now
        schedule.last_result = occurrence.result
        return occurrence

    def run_due(
        self,
        executor: Optional[Callable[[Dict[str, Any]], Any]] = None,
        userid: Optional[str] = None,
    ) -> List[Occurrence]:
        if not self.policy.enabled:
            return []
        runner = executor or self.executor
        if runner is None:
            raise ScheduleError('schedule_no_executor', 'No money handler is configured.')
        now = self._now()
        produced: List[Occurrence] = []
        with self._run_lock:
            for schedule in self.store.due(now, userid=userid):
                produced.extend(self._run_schedule(schedule, runner, now))
        return produced

    def _run_schedule(
        self,
        schedule: Schedule,
        executor: Callable[[Dict[str, Any]], Any],
        now: float,
    ) -> List[Occurrence]:
        produced: List[Occurrence] = []
        due_at = schedule.next_run
        if due_at > now:
            return produced

        if self.policy.catch_up == CATCH_UP_SKIP:
            nxt = advance_past(due_at, schedule.interval, now) if schedule.interval != 'once' else due_at
            occ_id = occurrence_id_for(schedule.schedule_id, due_at)
            if self.store.get_occurrence(occ_id) is None:
                skipped = Occurrence(
                    occurrence_id=occ_id,
                    schedule_id=schedule.schedule_id,
                    userid=schedule.userid,
                    due_at=due_at,
                    ran_at=now,
                    status='skipped',
                    result='catch_up_skip',
                    from_account=schedule.from_account,
                    to_account=schedule.to_account,
                    amount=schedule.amount,
                )
                self.store.put_occurrence(skipped)
                produced.append(skipped)
            if schedule.interval == 'once' or self._should_complete(schedule, nxt):
                self._mark_complete(schedule, now)
            else:
                schedule.next_run = nxt
                self.store.update_schedule(schedule)
            return produced

        slots = [due_at]
        if self.policy.catch_up == CATCH_UP_ALL and schedule.interval != 'once':
            cursor = due_at
            for _ in range(366):
                nxt = next_occurrence(cursor, schedule.interval)
                if nxt <= cursor or nxt > now:
                    break
                slots.append(nxt)
                cursor = nxt

        for slot in slots:
            occ = self._execute_one(schedule, slot, executor, now)
            produced.append(occ)
            if schedule.status == 'failed':
                return produced
            if schedule.interval == 'once':
                self._mark_complete(schedule, now)
                return produced
            if schedule.max_occurrences is not None and schedule.run_count >= schedule.max_occurrences:
                self._mark_complete(schedule, now)
                return produced

        last_slot = slots[-1]
        nxt = next_occurrence(last_slot, schedule.interval)
        if schedule.interval != 'once' and self.policy.catch_up == CATCH_UP_ONE:
            nxt = advance_past(last_slot, schedule.interval, now)
        if self._should_complete(schedule, nxt):
            self._mark_complete(schedule, now)
        else:
            schedule.next_run = nxt
            self.store.update_schedule(schedule)
        return produced


def default_store() -> Any:
    kind = os.environ.get('SCHEDULE_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryScheduleStore()
    path = os.environ.get('SCHEDULE_DB', 'SystemLogs/schedule.sqlite')
    return SqliteScheduleStore(path)


def build_service(executor: Optional[Callable[[Dict[str, Any]], Any]] = None) -> ScheduleService:
    return ScheduleService(SchedulePolicy.from_env(), default_store(), executor=executor)


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
        'schedule_duplicate': 409,
        'schedule_limit': 409,
        'schedule_not_active': 409,
        'schedule_not_paused': 409,
        'schedule_forbidden': 403,
        'schedule_disabled': 403,
        'schedule_not_found': 404,
        'schedule_too_soon': 400,
        'schedule_same_account': 400,
        'per_txn_exceeded': 400,
        'invalid_interval': 400,
        'invalid_end_at': 400,
        'invalid_max_occurrences': 400,
        'missing_customer_id': 400,
        'schedule_no_executor': 500,
    }.get(code, 400)


def _error_body(exc: ScheduleError) -> Dict[str, Any]:
    body: Dict[str, Any] = {'message': exc.message, 'error': exc.code}
    body.update({key: value for key, value in exc.extra.items() if key not in {'schedule'}})
    if exc.extra.get('schedule') is not None:
        body['schedule'] = exc.extra['schedule'].to_dict()
    return body


def handle_schedule_transfer(service: ScheduleService, own_accounts_loader=None):
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
        schedule = service.create(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            from_account=values.get('fromAccount') or values.get('from_account'),
            to_account=values.get('toAccount') or values.get('to_account'),
            amount=values.get('amount'),
            interval=values.get('interval') or 'once',
            start_at=values.get('start_at') or values.get('startAt'),
            end_at=values.get('end_at') or values.get('endAt'),
            max_occurrences=values.get('max_occurrences') or values.get('maxOccurrences'),
            note=values.get('note') or '',
            own_accounts=own_accounts,
        )
    except AmountError:
        return jsonify({'message': 'Enter a valid amount', 'error': 'invalid_amount'}), 400
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except WhenError:
        return jsonify({'message': 'Invalid start or end time', 'error': 'invalid_when'}), 400
    except ScheduleError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Transfer scheduled',
        'schedule': schedule.to_dict(),
        'Schedules': service.snapshot(owner),
    }), 201


def handle_list_schedules(service: ScheduleService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Schedules': service.snapshot(owner)}), 200


def handle_cancel_schedule(service: ScheduleService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    schedule_id = str(values.get('schedule_id') or '').strip()
    if not schedule_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_schedule_id'}), 400
    try:
        schedule = service.cancel(
            schedule_id=schedule_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
            owner_userid=userid,
        )
    except ScheduleError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Schedule cancelled',
        'schedule': schedule.to_dict(),
        'Schedules': service.snapshot(schedule.userid),
    }), 200


def handle_pause_schedule(service: ScheduleService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    schedule_id = str(values.get('schedule_id') or '').strip()
    if not schedule_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_schedule_id'}), 400
    try:
        schedule = service.pause(
            schedule_id=schedule_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
            owner_userid=userid,
        )
    except ScheduleError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Schedule paused',
        'schedule': schedule.to_dict(),
        'Schedules': service.snapshot(schedule.userid),
    }), 200


def handle_resume_schedule(service: ScheduleService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    schedule_id = str(values.get('schedule_id') or '').strip()
    if not schedule_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_schedule_id'}), 400
    try:
        schedule = service.resume(
            schedule_id=schedule_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
            owner_userid=userid,
        )
    except ScheduleError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Schedule resumed',
        'schedule': schedule.to_dict(),
        'Schedules': service.snapshot(schedule.userid),
    }), 200


def handle_run_due(service: ScheduleService, executor=None):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    target = userid if actor_type not in EMPLOYEE_ROLES else None
    try:
        occurrences = service.run_due(executor=executor, userid=target)
    except ScheduleError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    snapshot_user = userid if actor_type not in EMPLOYEE_ROLES else userid
    return jsonify({
        'message': 'Due schedules processed',
        'ran': len(occurrences),
        'occurrences': [row.to_dict() for row in occurrences],
        'Schedules': service.snapshot(snapshot_user),
    }), 200


def attach_schedule_routes(
    app,
    service: ScheduleService,
    own_accounts_loader=None,
    executor=None,
) -> None:
    @app.route('/scheduleTransfer', methods=['POST', 'GET'])
    def schedule_transfer_route():
        return handle_schedule_transfer(service, own_accounts_loader=own_accounts_loader)

    @app.route('/listSchedules', methods=['POST', 'GET'])
    def list_schedules_route():
        return handle_list_schedules(service)

    @app.route('/cancelSchedule', methods=['POST', 'GET'])
    def cancel_schedule_route():
        return handle_cancel_schedule(service)

    @app.route('/pauseSchedule', methods=['POST', 'GET'])
    def pause_schedule_route():
        return handle_pause_schedule(service)

    @app.route('/resumeSchedule', methods=['POST', 'GET'])
    def resume_schedule_route():
        return handle_resume_schedule(service)

    @app.route('/runDueSchedules', methods=['POST', 'GET'])
    def run_due_route():
        return handle_run_due(service, executor=executor or service.executor)
