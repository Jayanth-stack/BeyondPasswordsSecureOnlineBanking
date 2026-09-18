"""Bill pay / outgoing ACH.

Customers register billers and debit checking/savings to pay them. Recurring
instructions fire when due. Staff can settle or return a sent payment.
Independent of inbound ACH allocations (PR #64), scheduled internal transfers
(PR #36), and the internal payee allowlist (PR #26).

Existing `/fundTransfer` (in-bank) and `/withdrawAmount` (cash) stay
unchanged. `Customers.debit_request` still writes `debited` unless a remark
is supplied by this module.

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
"""

from __future__ import annotations

import calendar
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, List, Optional, Tuple

from flask import jsonify, request, session

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
BILLER_ACTIVE = 'active'
BILLER_PAUSED = 'paused'
BILLER_ARCHIVED = 'archived'
BILLER_STATUSES = frozenset({BILLER_ACTIVE, BILLER_PAUSED, BILLER_ARCHIVED})
INSTRUCTION_ACTIVE = 'active'
INSTRUCTION_PAUSED = 'paused'
INSTRUCTION_CANCELLED = 'cancelled'
INSTRUCTION_COMPLETED = 'completed'
INSTRUCTION_STATUSES = frozenset({
    INSTRUCTION_ACTIVE, INSTRUCTION_PAUSED, INSTRUCTION_CANCELLED, INSTRUCTION_COMPLETED,
})
PAY_SENT = 'sent'
PAY_SETTLED = 'settled'
PAY_RETURNED = 'returned'
PAY_NSF = 'nsf'
PAY_FAILED = 'failed'
PAY_CANCELLED = 'cancelled'
PAY_STATUSES = frozenset({PAY_SENT, PAY_SETTLED, PAY_RETURNED, PAY_NSF, PAY_FAILED, PAY_CANCELLED})
INTERVALS = frozenset({'once', 'weekly', 'monthly'})
INTERVAL_ALIASES = {
    'once': 'once', 'one': 'once', '1': 'once',
    'weekly': 'weekly', 'week': 'weekly', '7d': 'weekly',
    'monthly': 'monthly', 'month': 'monthly', '1m': 'monthly',
}
CATEGORIES = frozenset({'utility', 'rent', 'loan', 'card', 'insurance', 'other'})
CATEGORY_ALIASES = {
    'utilities': 'utility', 'electric': 'utility', 'gas': 'utility', 'water': 'utility',
    'phone': 'utility', 'internet': 'utility',
    'lease': 'rent', 'landlord': 'rent', 'housing': 'rent',
    'mortgage': 'loan', 'auto': 'loan', 'student': 'loan',
    'credit': 'card', 'credit_card': 'card', 'cc': 'card',
    'health': 'insurance', 'auto_insurance': 'insurance',
    'misc': 'other', 'general': 'other',
}
RETURN_REASONS = frozenset({'unauthorized', 'duplicate', 'wrong_amount', 'stop', 'other'})
CATCH_UP_ONE = 'one'
CATCH_UP_ALL = 'all'
CATCH_UP_SKIP = 'skip'
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/billpay.sqlite'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)


class AccountError(ValueError):
    pass


class AmountError(ValueError):
    pass


class WhenError(ValueError):
    pass


class BillPayError(ValueError):
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


def money_str(value: Decimal) -> str:
    return str(value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN))


def normalize_note(value: Any, *, limit: int = 500) -> str:
    return str(value or '').strip()[:limit]


def normalize_id(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return uuid.uuid4().hex
    return text[:120]


def normalize_nickname(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise BillPayError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_last4(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        return ''
    if len(digits) < 4:
        raise BillPayError('invalid_last4', 'Routing/account last-4 must be four digits.')
    return digits[-4:]


def normalize_category(value: Any) -> str:
    text = str(value or 'other').strip().lower().replace(' ', '_').replace('-', '_')
    text = CATEGORY_ALIASES.get(text, text)
    if text not in CATEGORIES:
        raise BillPayError('invalid_category', 'Unknown biller category.')
    return text


def normalize_interval(value: Any, *, default: str = 'once') -> str:
    text = str(value or default).strip().lower() or default
    mapped = INTERVAL_ALIASES.get(text, text)
    if mapped not in INTERVALS:
        raise BillPayError('invalid_interval', 'Interval must be once, weekly, or monthly.')
    return mapped


def normalize_return_reason(value: Any) -> str:
    text = str(value or 'other').strip().lower().replace(' ', '_')
    aliases = {'nsf': 'other', 'r10': 'unauthorized', 'r07': 'unauthorized', 'stop_payment': 'stop'}
    text = aliases.get(text, text)
    if text not in RETURN_REASONS:
        raise BillPayError('invalid_reason', 'Unknown return reason.')
    return text


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


def account_types_from_customer_payload(accounts: Any) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    if not isinstance(accounts, dict):
        return mapping
    for key in ('savings', 'checkin', 'credit'):
        item = accounts.get(key)
        if isinstance(item, dict) and item.get('Account') not in (None, 'None', ''):
            try:
                mapping[normalize_account(item['Account'])] = key
            except AccountError:
                continue
    return mapping


def add_calendar_months(stamp: float, months: int) -> float:
    """Advance `stamp` by `months`, clamping to the last day of the landing month."""
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
    if interval == 'weekly':
        return stamp + 7 * 86400.0
    if interval == 'monthly':
        return add_calendar_months(stamp, 1)
    raise BillPayError('invalid_interval', 'Unknown recurrence interval.')


def advance_past(stamp: float, interval: str, now: float) -> float:
    """First occurrence strictly after `now`, starting from `stamp`."""
    if interval == 'once':
        return stamp
    cursor = stamp
    for _ in range(10000):
        if cursor > now:
            return cursor
        nxt = next_occurrence(cursor, interval)
        if nxt <= cursor:
            return cursor + 86400.0
        cursor = nxt
    return cursor


def occurrence_id_for(instruction_id: str, due_at: float) -> str:
    return '%s:%d' % (instruction_id, int(due_at))


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
        return parse_money(default, allow_zero=True)
    return parse_money(raw, allow_zero=True)


def _classify_money_result(result: Any) -> str:
    if result in (None, True, 1):
        return 'ok'
    if isinstance(result, str):
        low = result.lower()
        if any(token in low for token in DEBIT_NSF):
            return 'nsf'
        if low in DEBIT_OK:
            return 'ok'
        return 'failed'
    return 'failed'


@dataclass(frozen=True)
class BillPayPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_pay: bool = True
    allow_credit: bool = False
    max_billers: int = 20
    max_instructions: int = 20
    max_payments: int = 200
    min_amount: Decimal = Decimal('1.00')
    max_amount: Decimal = Decimal('25000.00')
    min_lead_seconds: int = 60
    catch_up: str = CATCH_UP_ONE
    skip_on_error: bool = True

    @classmethod
    def from_env(cls) -> 'BillPayPolicy':
        catch_up = os.environ.get('BILLPAY_CATCH_UP', CATCH_UP_ONE).strip().lower() or CATCH_UP_ONE
        if catch_up not in {CATCH_UP_ONE, CATCH_UP_ALL, CATCH_UP_SKIP}:
            catch_up = CATCH_UP_ONE
        return cls(
            enabled=_env_bool('BILLPAY_ENABLED', True),
            customer_manage=_env_bool('BILLPAY_CUSTOMER_MANAGE', True),
            customer_pay=_env_bool('BILLPAY_CUSTOMER_PAY', True),
            allow_credit=_env_bool('BILLPAY_ALLOW_CREDIT', False),
            max_billers=max(1, _env_int('BILLPAY_MAX_BILLERS', 20)),
            max_instructions=max(1, _env_int('BILLPAY_MAX_INSTRUCTIONS', 20)),
            max_payments=max(1, _env_int('BILLPAY_MAX_PAYMENTS', 200)),
            min_amount=_env_money('BILLPAY_MIN_AMOUNT', '1.00'),
            max_amount=_env_money('BILLPAY_MAX_AMOUNT', '25000.00'),
            min_lead_seconds=max(0, _env_int('BILLPAY_MIN_LEAD_SECONDS', 60)),
            catch_up=catch_up,
            skip_on_error=_env_bool('BILLPAY_SKIP_ON_ERROR', True),
        )


@dataclass
class Biller:
    biller_id: str
    userid: str
    nickname: str
    category: str
    routing_last4: str
    account_last4: str
    default_from_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'biller_id': self.biller_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'category': self.category,
            'routing_last4': self.routing_last4,
            'account_last4': self.account_last4,
            'default_from_account': self.default_from_account,
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == BILLER_ACTIVE,
            'paused': self.status == BILLER_PAUSED,
            'archived': self.status == BILLER_ARCHIVED,
        }


@dataclass
class Instruction:
    instruction_id: str
    biller_id: str
    userid: str
    from_account: str
    amount: str
    interval: str
    status: str
    start_at: float
    next_run: float
    created_at: float
    actor: str
    note: str = ''
    last_occurrence_id: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'instruction_id': self.instruction_id,
            'biller_id': self.biller_id,
            'userid': self.userid,
            'from_account': self.from_account,
            'amount': self.amount,
            'interval': self.interval,
            'status': self.status,
            'start_at': self.start_at,
            'next_run': self.next_run,
            'created_at': self.created_at,
            'actor': self.actor,
            'note': self.note,
            'last_occurrence_id': self.last_occurrence_id,
            'active': self.status == INSTRUCTION_ACTIVE,
        }


@dataclass
class OutboundAch:
    payment_id: str
    trace_id: str
    occurrence_id: str
    instruction_id: str
    biller_id: str
    userid: str
    from_account: str
    amount: str
    nickname: str
    status: str
    actor: str
    created_at: float
    returned_at: float = 0.0
    note: str = ''
    reason: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'payment_id': self.payment_id,
            'trace_id': self.trace_id,
            'occurrence_id': self.occurrence_id,
            'instruction_id': self.instruction_id,
            'biller_id': self.biller_id,
            'userid': self.userid,
            'from_account': self.from_account,
            'amount': self.amount,
            'nickname': self.nickname,
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'returned_at': self.returned_at,
            'note': self.note,
            'reason': self.reason,
            'sent': self.status == PAY_SENT,
            'settled': self.status == PAY_SETTLED,
            'returned': self.status == PAY_RETURNED,
        }


def _clone_biller(row: Biller) -> Biller:
    return Biller(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_instruction(row: Instruction) -> Instruction:
    return Instruction(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_payment(row: OutboundAch) -> OutboundAch:
    return OutboundAch(**{k: getattr(row, k) for k in row.__dataclass_fields__})


class MemoryBillPayStore:
    def __init__(self) -> None:
        self._billers: Dict[str, Biller] = {}
        self._instructions: Dict[str, Instruction] = {}
        self._payments: Dict[str, OutboundAch] = {}
        self._by_trace: Dict[str, str] = {}
        self._by_occurrence: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_biller(self, biller: Biller) -> None:
        with self._lock:
            self._billers[biller.biller_id] = biller

    def get_biller(self, biller_id: str) -> Optional[Biller]:
        with self._lock:
            row = self._billers.get(biller_id)
            return _clone_biller(row) if row else None

    def update_biller(self, biller: Biller) -> None:
        with self._lock:
            self._billers[biller.biller_id] = biller

    def list_billers(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[Biller]:
        with self._lock:
            rows = [_clone_biller(row) for row in self._billers.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != BILLER_ARCHIVED]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows

    def find_biller_by_nickname(self, userid: str, nickname: str) -> Optional[Biller]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._billers.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status != BILLER_ARCHIVED:
                    return _clone_biller(row)
        return None

    def put_instruction(self, instruction: Instruction) -> None:
        with self._lock:
            self._instructions[instruction.instruction_id] = instruction

    def get_instruction(self, instruction_id: str) -> Optional[Instruction]:
        with self._lock:
            row = self._instructions.get(instruction_id)
            return _clone_instruction(row) if row else None

    def update_instruction(self, instruction: Instruction) -> None:
        with self._lock:
            self._instructions[instruction.instruction_id] = instruction

    def list_instructions(
        self,
        userid: Optional[str] = None,
        biller_id: Optional[str] = None,
    ) -> List[Instruction]:
        with self._lock:
            rows = [_clone_instruction(row) for row in self._instructions.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if biller_id is not None:
            rows = [row for row in rows if row.biller_id == biller_id]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows

    def put_payment(self, payment: OutboundAch) -> OutboundAch:
        with self._lock:
            existing_id = self._by_trace.get(payment.trace_id)
            if existing_id is not None:
                return self._payments[existing_id]
            if payment.occurrence_id:
                existing_occ = self._by_occurrence.get(payment.occurrence_id)
                if existing_occ is not None:
                    return self._payments[existing_occ]
            self._payments[payment.payment_id] = payment
            self._by_trace[payment.trace_id] = payment.payment_id
            if payment.occurrence_id:
                self._by_occurrence[payment.occurrence_id] = payment.payment_id
            return payment

    def update_payment(self, payment: OutboundAch) -> None:
        with self._lock:
            self._payments[payment.payment_id] = payment

    def get_payment(self, payment_id: str) -> Optional[OutboundAch]:
        with self._lock:
            row = self._payments.get(payment_id)
            return _clone_payment(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[OutboundAch]:
        with self._lock:
            payment_id = self._by_trace.get(trace_id)
            return _clone_payment(self._payments[payment_id]) if payment_id else None

    def get_payment_by_occurrence(self, occurrence_id: str) -> Optional[OutboundAch]:
        if not occurrence_id:
            return None
        with self._lock:
            payment_id = self._by_occurrence.get(occurrence_id)
            return _clone_payment(self._payments[payment_id]) if payment_id else None

    def list_payments(
        self,
        userid: Optional[str] = None,
        biller_id: Optional[str] = None,
    ) -> List[OutboundAch]:
        with self._lock:
            rows = [_clone_payment(row) for row in self._payments.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if biller_id is not None:
            rows = [row for row in rows if row.biller_id == biller_id]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows


class SqliteBillPayStore:
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
                CREATE TABLE IF NOT EXISTS billers (
                    biller_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    category TEXT NOT NULL,
                    routing_last4 TEXT NOT NULL DEFAULT '',
                    account_last4 TEXT NOT NULL DEFAULT '',
                    default_from_account TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS instructions (
                    instruction_id TEXT PRIMARY KEY,
                    biller_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    from_account TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    status TEXT NOT NULL,
                    start_at REAL NOT NULL,
                    next_run REAL NOT NULL,
                    created_at REAL NOT NULL,
                    actor TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    last_occurrence_id TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    payment_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    occurrence_id TEXT NOT NULL DEFAULT '',
                    instruction_id TEXT NOT NULL DEFAULT '',
                    biller_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    from_account TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    returned_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS payments_occurrence_uq '
                'ON payments(occurrence_id) WHERE occurrence_id != \'\''
            )
            conn.commit()

    def put_biller(self, biller: Biller) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO billers (
                    biller_id, userid, nickname, category, routing_last4,
                    account_last4, default_from_account, status, actor,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    biller.biller_id, biller.userid, biller.nickname, biller.category,
                    biller.routing_last4, biller.account_last4, biller.default_from_account,
                    biller.status, biller.actor, biller.created_at, biller.updated_at,
                ),
            )
            conn.commit()

    def get_biller(self, biller_id: str) -> Optional[Biller]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM billers WHERE biller_id = ?', (biller_id,)).fetchone()
        return _biller_from_row(row) if row else None

    def update_biller(self, biller: Biller) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE billers SET nickname=?, category=?, routing_last4=?, account_last4=?,
                    default_from_account=?, status=?, actor=?, updated_at=?
                WHERE biller_id=?
                """,
                (
                    biller.nickname, biller.category, biller.routing_last4, biller.account_last4,
                    biller.default_from_account, biller.status, biller.actor, biller.updated_at,
                    biller.biller_id,
                ),
            )
            conn.commit()

    def list_billers(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[Biller]:
        sql = 'SELECT * FROM billers'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if not include_archived:
            clauses.append("status != 'archived'")
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_biller_from_row(row) for row in rows]

    def find_biller_by_nickname(self, userid: str, nickname: str) -> Optional[Biller]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM billers
                WHERE userid = ? AND lower(nickname) = lower(?) AND status != 'archived'
                """,
                (userid, nickname),
            ).fetchone()
        return _biller_from_row(row) if row else None

    def put_instruction(self, instruction: Instruction) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO instructions (
                    instruction_id, biller_id, userid, from_account, amount, interval,
                    status, start_at, next_run, created_at, actor, note, last_occurrence_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _instruction_row(instruction),
            )
            conn.commit()

    def get_instruction(self, instruction_id: str) -> Optional[Instruction]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM instructions WHERE instruction_id = ?', (instruction_id,)
            ).fetchone()
        return _instruction_from_row(row) if row else None

    def update_instruction(self, instruction: Instruction) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE instructions SET from_account=?, amount=?, interval=?, status=?,
                    next_run=?, actor=?, note=?, last_occurrence_id=?
                WHERE instruction_id=?
                """,
                (
                    instruction.from_account, instruction.amount, instruction.interval,
                    instruction.status, instruction.next_run, instruction.actor,
                    instruction.note, instruction.last_occurrence_id, instruction.instruction_id,
                ),
            )
            conn.commit()

    def list_instructions(
        self,
        userid: Optional[str] = None,
        biller_id: Optional[str] = None,
    ) -> List[Instruction]:
        sql = 'SELECT * FROM instructions'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if biller_id is not None:
            clauses.append('biller_id = ?')
            params.append(biller_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_instruction_from_row(row) for row in rows]

    def put_payment(self, payment: OutboundAch) -> OutboundAch:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM payments WHERE trace_id = ?', (payment.trace_id,)
            ).fetchone()
            if existing is not None:
                return _payment_from_row(existing)
            if payment.occurrence_id:
                existing_occ = conn.execute(
                    'SELECT * FROM payments WHERE occurrence_id = ?', (payment.occurrence_id,)
                ).fetchone()
                if existing_occ is not None:
                    return _payment_from_row(existing_occ)
            conn.execute(
                """
                INSERT INTO payments (
                    payment_id, trace_id, occurrence_id, instruction_id, biller_id,
                    userid, from_account, amount, nickname, status, actor,
                    created_at, returned_at, note, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _payment_row(payment),
            )
            conn.commit()
            return payment

    def update_payment(self, payment: OutboundAch) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE payments SET status=?, actor=?, returned_at=?, note=?, reason=?
                WHERE payment_id=?
                """,
                (
                    payment.status, payment.actor, payment.returned_at,
                    payment.note, payment.reason, payment.payment_id,
                ),
            )
            conn.commit()

    def get_payment(self, payment_id: str) -> Optional[OutboundAch]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE payment_id = ?', (payment_id,)).fetchone()
        return _payment_from_row(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[OutboundAch]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE trace_id = ?', (trace_id,)).fetchone()
        return _payment_from_row(row) if row else None

    def get_payment_by_occurrence(self, occurrence_id: str) -> Optional[OutboundAch]:
        if not occurrence_id:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM payments WHERE occurrence_id = ?', (occurrence_id,)
            ).fetchone()
        return _payment_from_row(row) if row else None

    def list_payments(
        self,
        userid: Optional[str] = None,
        biller_id: Optional[str] = None,
    ) -> List[OutboundAch]:
        sql = 'SELECT * FROM payments'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if biller_id is not None:
            clauses.append('biller_id = ?')
            params.append(biller_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_payment_from_row(row) for row in rows]


def _biller_from_row(row: sqlite3.Row) -> Biller:
    return Biller(
        biller_id=row['biller_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        category=row['category'],
        routing_last4=row['routing_last4'],
        account_last4=row['account_last4'],
        default_from_account=row['default_from_account'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
    )


def _instruction_row(instruction: Instruction) -> Tuple[Any, ...]:
    return (
        instruction.instruction_id, instruction.biller_id, instruction.userid,
        instruction.from_account, instruction.amount, instruction.interval,
        instruction.status, instruction.start_at, instruction.next_run,
        instruction.created_at, instruction.actor, instruction.note,
        instruction.last_occurrence_id,
    )


def _instruction_from_row(row: sqlite3.Row) -> Instruction:
    return Instruction(
        instruction_id=row['instruction_id'],
        biller_id=row['biller_id'],
        userid=row['userid'],
        from_account=row['from_account'],
        amount=row['amount'],
        interval=row['interval'],
        status=row['status'],
        start_at=float(row['start_at']),
        next_run=float(row['next_run']),
        created_at=float(row['created_at']),
        actor=row['actor'],
        note=row['note'] or '',
        last_occurrence_id=row['last_occurrence_id'] or '',
    )


def _payment_row(payment: OutboundAch) -> Tuple[Any, ...]:
    return (
        payment.payment_id, payment.trace_id, payment.occurrence_id, payment.instruction_id,
        payment.biller_id, payment.userid, payment.from_account, payment.amount,
        payment.nickname, payment.status, payment.actor, payment.created_at,
        payment.returned_at, payment.note, payment.reason,
    )


def _payment_from_row(row: sqlite3.Row) -> OutboundAch:
    return OutboundAch(
        payment_id=row['payment_id'],
        trace_id=row['trace_id'],
        occurrence_id=row['occurrence_id'] or '',
        instruction_id=row['instruction_id'] or '',
        biller_id=row['biller_id'],
        userid=row['userid'],
        from_account=row['from_account'],
        amount=row['amount'],
        nickname=row['nickname'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        returned_at=float(row['returned_at'] or 0),
        note=row['note'] or '',
        reason=row['reason'] or '',
    )


class BillPayService:
    def __init__(
        self,
        policy: Optional[BillPayPolicy] = None,
        store: Any = None,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.policy = policy or BillPayPolicy()
        self.store = store or MemoryBillPayStore()
        self.clock = clock or time.time
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise BillPayError('billpay_disabled', 'Bill pay is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise BillPayError('billpay_forbidden', 'Customers cannot manage billers.')

    def _require_pay(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_pay:
            raise BillPayError('billpay_forbidden', 'Customers cannot originate bill pay.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise BillPayError('billpay_forbidden', 'Only staff can settle or return ACH.')

    def _owned_accounts(self, userid: str) -> List[str]:
        if self.accounts_fn is None:
            return []
        try:
            payload = self.accounts_fn(userid)
        except Exception:
            return []
        return own_accounts_from_customer_payload(payload)

    def _account_types(self, userid: str) -> Dict[str, str]:
        if self.accounts_fn is None:
            return {}
        try:
            payload = self.accounts_fn(userid)
        except Exception:
            return {}
        return account_types_from_customer_payload(payload)

    def _assert_from_account(self, userid: str, account: str) -> None:
        owned = self._owned_accounts(userid)
        if owned and account not in owned:
            raise BillPayError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        if types.get(account) == 'credit' and not self.policy.allow_credit:
            raise BillPayError('credit_not_allowed', 'Credit accounts cannot originate ACH bill pay.')

    def _assert_amount(self, dollars: Decimal) -> None:
        if dollars < self.policy.min_amount or dollars > self.policy.max_amount:
            raise BillPayError('amount_out_of_range', 'Amount is outside bill-pay policy range.')

    def get_biller(self, *, biller_id: str, actor: str, actor_type: str) -> Biller:
        self._require_enabled()
        biller = self.store.get_biller(biller_id)
        if biller is None:
            raise BillPayError('biller_not_found', 'Biller not found.')
        if actor_type not in EMPLOYEE_ROLES and biller.userid != actor:
            raise BillPayError('billpay_forbidden', 'Not allowed to view this biller.')
        return biller

    def add_biller(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        default_from_account: Any,
        category: Any = 'other',
        routing_last4: Any = '',
        account_last4: Any = '',
    ) -> Biller:
        self._require_manage(actor_type)
        nick = normalize_nickname(nickname)
        default = normalize_account(default_from_account)
        self._assert_from_account(owner_userid, default)
        if self.store.find_biller_by_nickname(owner_userid, nick):
            raise BillPayError('biller_duplicate', 'A biller with this nickname already exists.')
        existing = [row for row in self.store.list_billers(owner_userid) if row.status != BILLER_ARCHIVED]
        if len(existing) >= self.policy.max_billers:
            raise BillPayError('biller_limit', 'Biller limit reached.')
        now = float(self.clock())
        biller = Biller(
            biller_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=nick,
            category=normalize_category(category),
            routing_last4=normalize_last4(routing_last4),
            account_last4=normalize_last4(account_last4),
            default_from_account=default,
            status=BILLER_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_biller(biller)
        return biller

    def update_biller(
        self,
        *,
        biller_id: str,
        actor: str,
        actor_type: str,
        nickname: Any = None,
        category: Any = None,
        routing_last4: Any = None,
        account_last4: Any = None,
        default_from_account: Any = None,
    ) -> Biller:
        biller = self.get_biller(biller_id=biller_id, actor=actor, actor_type=actor_type)
        self._require_manage(actor_type)
        if biller.status == BILLER_ARCHIVED:
            raise BillPayError('already_archived', 'Archived billers cannot be updated.')
        if nickname is not None and str(nickname).strip():
            nick = normalize_nickname(nickname)
            other = self.store.find_biller_by_nickname(biller.userid, nick)
            if other is not None and other.biller_id != biller.biller_id:
                raise BillPayError('biller_duplicate', 'A biller with this nickname already exists.')
            biller.nickname = nick
        if category is not None and str(category).strip():
            biller.category = normalize_category(category)
        if routing_last4 is not None:
            biller.routing_last4 = normalize_last4(routing_last4)
        if account_last4 is not None:
            biller.account_last4 = normalize_last4(account_last4)
        if default_from_account is not None and str(default_from_account).strip():
            default = normalize_account(default_from_account)
            self._assert_from_account(biller.userid, default)
            biller.default_from_account = default
        biller.actor = str(actor)
        biller.updated_at = float(self.clock())
        self.store.update_biller(biller)
        return biller

    def set_biller_status(
        self,
        *,
        biller_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> Biller:
        biller = self.get_biller(biller_id=biller_id, actor=actor, actor_type=actor_type)
        self._require_manage(actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': BILLER_PAUSED, 'hold': BILLER_PAUSED,
            'resume': BILLER_ACTIVE, 'activate': BILLER_ACTIVE, 'unpause': BILLER_ACTIVE,
            'archive': BILLER_ARCHIVED, 'remove': BILLER_ARCHIVED, 'delete': BILLER_ARCHIVED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in BILLER_STATUSES:
            raise BillPayError('invalid_status', 'Status must be active, paused, or archived.')
        if biller.status == BILLER_ARCHIVED and wanted != BILLER_ARCHIVED:
            raise BillPayError('already_archived', 'Archived billers cannot be reopened.')
        if biller.status == wanted:
            if wanted == BILLER_PAUSED:
                raise BillPayError('already_paused', 'Biller is already paused.')
            if wanted == BILLER_ACTIVE:
                raise BillPayError('already_active', 'Biller is already active.')
            raise BillPayError('already_archived', 'Biller is already archived.')
        biller.status = wanted
        biller.actor = str(actor)
        biller.updated_at = float(self.clock())
        self.store.update_biller(biller)
        if wanted == BILLER_ARCHIVED:
            for instruction in self.store.list_instructions(biller.userid, biller.biller_id):
                if instruction.status == INSTRUCTION_ACTIVE:
                    instruction.status = INSTRUCTION_CANCELLED
                    instruction.actor = str(actor)
                    self.store.update_instruction(instruction)
        return biller

    def _require_active_biller(self, biller: Biller, *, force: bool = False) -> None:
        if biller.status == BILLER_ARCHIVED:
            raise BillPayError('already_archived', 'Cannot pay an archived biller.')
        if biller.status == BILLER_PAUSED and not force:
            raise BillPayError('biller_paused', 'Biller is paused.')

    def pay_bill(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        biller_id: Any,
        amount: Any,
        from_account: Any = None,
        trace_id: Any = None,
        note: Any = '',
        force: bool = False,
    ) -> Tuple[OutboundAch, bool]:
        self._require_pay(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise BillPayError('billpay_forbidden', 'Not allowed to pay for this customer.')
        biller = self.get_biller(biller_id=str(biller_id or '').strip(), actor=actor, actor_type=actor_type)
        if biller.userid != owner_userid:
            raise BillPayError('billpay_forbidden', 'Biller does not belong to this customer.')
        self._require_active_biller(biller, force=force)
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        account = normalize_account(from_account or biller.default_from_account)
        self._assert_from_account(owner_userid, account)
        trace = normalize_id(trace_id)
        existing = self.store.get_payment_by_trace(trace)
        if existing is not None:
            return existing, False
        if len(self.store.list_payments(owner_userid)) >= self.policy.max_payments:
            raise BillPayError('payment_limit', 'Outbound ACH history limit reached.')
        payment = self._post_debit(
            owner_userid=owner_userid,
            actor=actor,
            biller=biller,
            account=account,
            dollars=dollars,
            trace_id=trace,
            occurrence_id='',
            instruction_id='',
            note=normalize_note(note),
        )
        stored = self.store.put_payment(payment)
        if stored.payment_id != payment.payment_id:
            return stored, False
        if stored.status == PAY_NSF:
            raise BillPayError('nsf', 'Insufficient funds for bill pay.', payment=stored)
        if stored.status != PAY_SENT:
            raise BillPayError('failed', 'Bill pay debit did not complete.', payment=stored)
        return stored, True

    def schedule_bill_pay(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        biller_id: Any,
        amount: Any,
        interval: Any = 'monthly',
        start_at: Any = None,
        from_account: Any = None,
        note: Any = '',
    ) -> Instruction:
        self._require_pay(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise BillPayError('billpay_forbidden', 'Not allowed to schedule for this customer.')
        biller = self.get_biller(biller_id=str(biller_id or '').strip(), actor=actor, actor_type=actor_type)
        if biller.userid != owner_userid:
            raise BillPayError('billpay_forbidden', 'Biller does not belong to this customer.')
        self._require_active_biller(biller)
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        account = normalize_account(from_account or biller.default_from_account)
        self._assert_from_account(owner_userid, account)
        kind = normalize_interval(interval, default='monthly')
        now = float(self.clock())
        when = parse_when(start_at)
        if when is None:
            when = now + float(self.policy.min_lead_seconds)
        if when < now + float(self.policy.min_lead_seconds):
            raise BillPayError('too_soon', 'First payment is inside the minimum lead time.')
        open_rows = [
            row for row in self.store.list_instructions(owner_userid)
            if row.status in {INSTRUCTION_ACTIVE, INSTRUCTION_PAUSED}
        ]
        if len(open_rows) >= self.policy.max_instructions:
            raise BillPayError('instruction_limit', 'Recurring bill-pay limit reached.')
        for row in open_rows:
            if (
                row.biller_id == biller.biller_id
                and row.from_account == account
                and row.amount == money_str(dollars)
                and row.interval == kind
            ):
                raise BillPayError('instruction_duplicate', 'An identical bill-pay instruction already exists.')
        instruction = Instruction(
            instruction_id=uuid.uuid4().hex,
            biller_id=biller.biller_id,
            userid=owner_userid,
            from_account=account,
            amount=money_str(dollars),
            interval=kind,
            status=INSTRUCTION_ACTIVE,
            start_at=when,
            next_run=when,
            created_at=now,
            actor=str(actor),
            note=normalize_note(note),
        )
        self.store.put_instruction(instruction)
        return instruction

    def get_instruction(self, *, instruction_id: str, actor: str, actor_type: str) -> Instruction:
        self._require_enabled()
        instruction = self.store.get_instruction(instruction_id)
        if instruction is None:
            raise BillPayError('instruction_not_found', 'Bill-pay instruction not found.')
        if actor_type not in EMPLOYEE_ROLES and instruction.userid != actor:
            raise BillPayError('billpay_forbidden', 'Not allowed to view this instruction.')
        return instruction

    def set_instruction_status(
        self,
        *,
        instruction_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> Instruction:
        instruction = self.get_instruction(
            instruction_id=instruction_id, actor=actor, actor_type=actor_type,
        )
        self._require_pay(actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': INSTRUCTION_PAUSED, 'hold': INSTRUCTION_PAUSED,
            'resume': INSTRUCTION_ACTIVE, 'activate': INSTRUCTION_ACTIVE, 'unpause': INSTRUCTION_ACTIVE,
            'cancel': INSTRUCTION_CANCELLED, 'stop': INSTRUCTION_CANCELLED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {INSTRUCTION_ACTIVE, INSTRUCTION_PAUSED, INSTRUCTION_CANCELLED}:
            raise BillPayError('invalid_status', 'Status must be active, paused, or cancelled.')
        if instruction.status in {INSTRUCTION_CANCELLED, INSTRUCTION_COMPLETED}:
            raise BillPayError('already_resolved', 'Instruction is no longer changeable.')
        if instruction.status == wanted:
            if wanted == INSTRUCTION_PAUSED:
                raise BillPayError('already_paused', 'Instruction is already paused.')
            if wanted == INSTRUCTION_ACTIVE:
                raise BillPayError('already_active', 'Instruction is already active.')
            raise BillPayError('already_resolved', 'Instruction is already cancelled.')
        if wanted == INSTRUCTION_ACTIVE:
            biller = self.store.get_biller(instruction.biller_id)
            if biller is None or biller.status != BILLER_ACTIVE:
                raise BillPayError('biller_paused', 'Biller is not active.')
        instruction.status = wanted
        instruction.actor = str(actor)
        self.store.update_instruction(instruction)
        return instruction

    def run_due(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        limit: int = 20,
    ) -> List[OutboundAch]:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise BillPayError('billpay_forbidden', 'Not allowed to run due payments for this customer.')
        now = float(self.clock())
        posted: List[OutboundAch] = []
        rows = [
            row for row in self.store.list_instructions(owner_userid)
            if row.status == INSTRUCTION_ACTIVE and row.next_run <= now
        ]
        rows.sort(key=lambda row: row.next_run)
        for instruction in rows:
            if len(posted) >= limit:
                break
            biller = self.store.get_biller(instruction.biller_id)
            if biller is None or biller.status != BILLER_ACTIVE:
                continue
            due_at = instruction.next_run
            occurrence_id = occurrence_id_for(instruction.instruction_id, due_at)
            existing = self.store.get_payment_by_occurrence(occurrence_id)
            if existing is not None:
                self._advance_instruction(instruction, now)
                continue
            if self.policy.catch_up == CATCH_UP_SKIP:
                self._advance_instruction(instruction, now)
                continue
            dues = [due_at]
            if self.policy.catch_up == CATCH_UP_ALL and instruction.interval != 'once':
                cursor = due_at
                while True:
                    nxt = next_occurrence(cursor, instruction.interval)
                    if nxt <= cursor or nxt > now:
                        break
                    dues.append(nxt)
                    cursor = nxt
                    if len(dues) >= limit:
                        break
            for stamp in dues:
                if len(posted) >= limit:
                    break
                occ = occurrence_id_for(instruction.instruction_id, stamp)
                if self.store.get_payment_by_occurrence(occ) is not None:
                    continue
                payment = self._post_debit(
                    owner_userid=owner_userid,
                    actor=actor,
                    biller=biller,
                    account=instruction.from_account,
                    dollars=parse_money(instruction.amount),
                    trace_id=normalize_id(None),
                    occurrence_id=occ,
                    instruction_id=instruction.instruction_id,
                    note=instruction.note,
                )
                stored = self.store.put_payment(payment)
                posted.append(stored)
                instruction.last_occurrence_id = occ
                if stored.status != PAY_SENT and not self.policy.skip_on_error:
                    self.store.update_instruction(instruction)
                    return posted
            self._advance_instruction(instruction, now)
        return posted

    def _advance_instruction(self, instruction: Instruction, now: float) -> None:
        if instruction.interval == 'once':
            instruction.status = INSTRUCTION_COMPLETED
        else:
            instruction.next_run = advance_past(instruction.next_run, instruction.interval, now)
        self.store.update_instruction(instruction)

    def _post_debit(
        self,
        *,
        owner_userid: str,
        actor: str,
        biller: Biller,
        account: str,
        dollars: Decimal,
        trace_id: str,
        occurrence_id: str,
        instruction_id: str,
        note: str,
    ) -> OutboundAch:
        remark = note or ('bill pay to %s' % biller.nickname)
        status = PAY_SENT
        fail_note = ''
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(account, money_str(dollars), remark)
            except Exception as exc:
                status = PAY_FAILED
                fail_note = str(exc)[:240]
                result = None
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = PAY_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = PAY_FAILED
                    fail_note = str(result)[:240]
        return OutboundAch(
            payment_id=uuid.uuid4().hex,
            trace_id=trace_id,
            occurrence_id=occurrence_id,
            instruction_id=instruction_id,
            biller_id=biller.biller_id,
            userid=owner_userid,
            from_account=account,
            amount=money_str(dollars),
            nickname=biller.nickname,
            status=status,
            actor=str(actor),
            created_at=float(self.clock()),
            note=fail_note or remark,
        )

    def get_payment(self, *, payment_id: str, actor: str, actor_type: str) -> OutboundAch:
        self._require_enabled()
        payment = self.store.get_payment(payment_id)
        if payment is None:
            raise BillPayError('payment_not_found', 'Outbound ACH payment not found.')
        if actor_type not in EMPLOYEE_ROLES and payment.userid != actor:
            raise BillPayError('billpay_forbidden', 'Not allowed to view this payment.')
        return payment

    def settle_outbound(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
    ) -> OutboundAch:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == PAY_SETTLED:
            raise BillPayError('already_settled', 'Payment is already settled.')
        if payment.status != PAY_SENT:
            raise BillPayError('not_returnable', 'Only sent payments can be settled.')
        payment.status = PAY_SETTLED
        payment.actor = str(actor)
        self.store.update_payment(payment)
        return payment

    def return_outbound(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> OutboundAch:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == PAY_RETURNED:
            raise BillPayError('already_returned', 'Payment is already returned.')
        if payment.status == PAY_SETTLED:
            raise BillPayError('already_settled', 'Settled payments cannot be returned.')
        if payment.status != PAY_SENT:
            raise BillPayError('not_returnable', 'Only sent payments can be returned.')
        why = normalize_return_reason(reason)
        remark = normalize_note(note) or ('bill pay returned from %s' % payment.nickname)
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(payment.from_account, payment.amount, remark)
            except Exception as exc:
                raise BillPayError('return_failed', 'Return credit did not complete.', payment=payment) from exc
            if _classify_money_result(result) != 'ok':
                raise BillPayError('return_failed', 'Return credit did not complete.', payment=payment)
        payment.status = PAY_RETURNED
        payment.reason = why
        payment.note = remark
        payment.actor = str(actor)
        payment.returned_at = float(self.clock())
        self.store.update_payment(payment)
        return payment

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        try:
            self.run_due(
                owner_userid=userid,
                actor=actor or userid,
                actor_type=actor_type if actor_type in EMPLOYEE_ROLES else 'customer',
            )
        except Exception:
            pass
        billers = self.store.list_billers(userid)
        instructions = self.store.list_instructions(userid)
        payments = self.store.list_payments(userid)
        ytd = Decimal('0.00')
        returned = Decimal('0.00')
        for row in payments:
            try:
                dollars = parse_money(row.amount, allow_zero=True)
            except AmountError:
                continue
            if row.status in {PAY_SENT, PAY_SETTLED}:
                ytd += dollars
            elif row.status == PAY_RETURNED:
                returned += dollars
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'max_billers': self.policy.max_billers,
            'max_instructions': self.policy.max_instructions,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'billers': [row.to_dict() for row in billers],
            'instructions': [row.to_dict() for row in instructions],
            'payments': [row.to_dict() for row in payments[:40]],
            'ytd': money_str(ytd),
            'returned_ytd': money_str(returned),
            'active_billers': sum(1 for row in billers if row.status == BILLER_ACTIVE),
            'open_instructions': sum(1 for row in instructions if row.status == INSTRUCTION_ACTIVE),
        }


_SERVICE: Optional[BillPayService] = None


def set_service(service: Optional[BillPayService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[BillPayService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('BILLPAY_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryBillPayStore()
    path = os.environ.get('BILLPAY_DB', DEFAULT_STORE_PATH)
    return SqliteBillPayStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
) -> BillPayService:
    if store is None:
        store = default_store()
    return BillPayService(
        BillPayPolicy.from_env(),
        store,
        clock=clock,
        debit_fn=debit_fn,
        credit_fn=credit_fn,
        accounts_fn=accounts_fn,
    )


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
        'biller_duplicate': 409,
        'biller_limit': 409,
        'instruction_duplicate': 409,
        'instruction_limit': 409,
        'payment_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_resolved': 409,
        'already_settled': 409,
        'already_returned': 409,
        'nsf': 409,
        'failed': 409,
        'return_failed': 409,
        'billpay_forbidden': 403,
        'billpay_disabled': 403,
        'biller_paused': 403,
        'credit_not_allowed': 403,
        'not_returnable': 403,
        'biller_not_found': 404,
        'instruction_not_found': 404,
        'payment_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_last4': 400,
        'invalid_category': 400,
        'invalid_interval': 400,
        'invalid_status': 400,
        'invalid_reason': 400,
        'invalid_when': 400,
        'amount_out_of_range': 400,
        'too_soon': 400,
        'missing_customer_id': 400,
        'missing_biller': 400,
        'missing_instruction': 400,
        'missing_payment': 400,
    }.get(code, 400)


def _error_body(exc: BillPayError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('payment') is not None:
        body['payment'] = exc.extra['payment'].to_dict()
    if exc.extra.get('biller') is not None:
        body['biller'] = exc.extra['biller'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except WhenError:
        return jsonify({'message': 'Invalid date', 'error': 'invalid_when'}), 400
    except BillPayError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list_billers(service: BillPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'BillPay': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_add_biller(service: BillPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        biller = service.add_biller(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname') or values.get('name'),
            default_from_account=values.get('default_from_account') or values.get('account') or values.get('from_account'),
            category=values.get('category') or 'other',
            routing_last4=values.get('routing_last4'),
            account_last4=values.get('account_last4'),
        )
        return jsonify({
            'message': 'Biller added',
            'biller': biller.to_dict(),
            'BillPay': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def handle_update_biller(service: BillPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    biller_id = str(values.get('biller_id') or '').strip()
    if not biller_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_biller'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        biller = service.update_biller(
            biller_id=biller_id,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname'),
            category=values.get('category'),
            routing_last4=values.get('routing_last4'),
            account_last4=values.get('account_last4'),
            default_from_account=values.get('default_from_account') or values.get('account') or values.get('from_account'),
        )
        return jsonify({
            'message': 'Biller updated',
            'biller': biller.to_dict(),
            'BillPay': service.snapshot(biller.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _biller_status_route(service: BillPayService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    biller_id = str(values.get('biller_id') or '').strip()
    if not biller_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_biller'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        biller = service.set_biller_status(
            biller_id=biller_id, actor=userid, actor_type=actor_type, status=status,
        )
        return jsonify({
            'message': ok_message,
            'biller': biller.to_dict(),
            'BillPay': service.snapshot(biller.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_pay_bill(service: BillPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    biller_id = str(values.get('biller_id') or '').strip()
    if not biller_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_biller'}), 400

    def _run():
        payment, created = service.pay_bill(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            biller_id=biller_id,
            amount=values.get('amount'),
            from_account=values.get('from_account') or values.get('account'),
            trace_id=values.get('trace_id'),
            note=values.get('note') or values.get('description') or '',
            force=bool(values.get('force')),
        )
        return jsonify({
            'message': 'Bill paid' if created else 'Bill pay already posted',
            'payment': payment.to_dict(),
            'BillPay': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_schedule_bill_pay(service: BillPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    biller_id = str(values.get('biller_id') or '').strip()
    if not biller_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_biller'}), 400

    def _run():
        instruction = service.schedule_bill_pay(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            biller_id=biller_id,
            amount=values.get('amount'),
            interval=values.get('interval') or 'monthly',
            start_at=values.get('start_at') or values.get('when'),
            from_account=values.get('from_account') or values.get('account'),
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Bill pay scheduled',
            'instruction': instruction.to_dict(),
            'BillPay': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def handle_list_bill_pay(service: BillPayService):
    return handle_list_billers(service)


def _instruction_status_route(service: BillPayService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    instruction_id = str(values.get('instruction_id') or '').strip()
    if not instruction_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_instruction'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        instruction = service.set_instruction_status(
            instruction_id=instruction_id, actor=userid, actor_type=actor_type, status=status,
        )
        return jsonify({
            'message': ok_message,
            'instruction': instruction.to_dict(),
            'BillPay': service.snapshot(instruction.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due_bill_pay(service: BillPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values) or userid

    def _run():
        posted = service.run_due(owner_userid=owner, actor=userid, actor_type=actor_type)
        return jsonify({
            'message': 'Due bill payments posted',
            'posted': [row.to_dict() for row in posted],
            'BillPay': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_list_outbound_ach(service: BillPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    snapshot = service.snapshot(owner, actor=userid, actor_type=actor_type)
    return jsonify({'payments': snapshot['payments'], 'BillPay': snapshot}), 200


def handle_return_outbound_ach(service: BillPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    payment_id = str(values.get('payment_id') or '').strip()
    if not payment_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_payment'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        payment = service.return_outbound(
            payment_id=payment_id,
            actor=userid,
            actor_type=actor_type,
            reason=values.get('reason') or 'other',
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Outbound ACH returned',
            'payment': payment.to_dict(),
            'BillPay': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_settle_outbound_ach(service: BillPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    payment_id = str(values.get('payment_id') or '').strip()
    if not payment_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_payment'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        payment = service.settle_outbound(
            payment_id=payment_id, actor=userid, actor_type=actor_type,
        )
        return jsonify({
            'message': 'Outbound ACH settled',
            'payment': payment.to_dict(),
            'BillPay': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def attach_billpay_routes(app, service: BillPayService) -> None:
    @app.route('/listBillers', methods=['POST', 'GET'])
    def list_billers_route():
        return handle_list_billers(service)

    @app.route('/addBiller', methods=['POST', 'GET'])
    def add_biller_route():
        return handle_add_biller(service)

    @app.route('/updateBiller', methods=['POST', 'GET'])
    def update_biller_route():
        return handle_update_biller(service)

    @app.route('/pauseBiller', methods=['POST', 'GET'])
    def pause_biller_route():
        return _biller_status_route(service, BILLER_PAUSED, 'Biller paused')

    @app.route('/resumeBiller', methods=['POST', 'GET'])
    def resume_biller_route():
        return _biller_status_route(service, BILLER_ACTIVE, 'Biller resumed')

    @app.route('/archiveBiller', methods=['POST', 'GET'])
    def archive_biller_route():
        return _biller_status_route(service, BILLER_ARCHIVED, 'Biller archived')

    @app.route('/payBill', methods=['POST', 'GET'])
    def pay_bill_route():
        return handle_pay_bill(service)

    @app.route('/scheduleBillPay', methods=['POST', 'GET'])
    def schedule_bill_pay_route():
        return handle_schedule_bill_pay(service)

    @app.route('/listBillPay', methods=['POST', 'GET'])
    def list_bill_pay_route():
        return handle_list_bill_pay(service)

    @app.route('/pauseBillPay', methods=['POST', 'GET'])
    def pause_bill_pay_route():
        return _instruction_status_route(service, INSTRUCTION_PAUSED, 'Bill pay paused')

    @app.route('/resumeBillPay', methods=['POST', 'GET'])
    def resume_bill_pay_route():
        return _instruction_status_route(service, INSTRUCTION_ACTIVE, 'Bill pay resumed')

    @app.route('/cancelBillPay', methods=['POST', 'GET'])
    def cancel_bill_pay_route():
        return _instruction_status_route(service, INSTRUCTION_CANCELLED, 'Bill pay cancelled')

    @app.route('/runDueBillPay', methods=['POST', 'GET'])
    def run_due_bill_pay_route():
        return handle_run_due_bill_pay(service)

    @app.route('/listOutboundAch', methods=['POST', 'GET'])
    def list_outbound_ach_route():
        return handle_list_outbound_ach(service)

    @app.route('/returnOutboundAch', methods=['POST', 'GET'])
    def return_outbound_ach_route():
        return handle_return_outbound_ach(service)

    @app.route('/settleOutboundAch', methods=['POST', 'GET'])
    def settle_outbound_ach_route():
        return handle_settle_outbound_ach(service)
