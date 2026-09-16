"""Year-end tax reporting (1099-INT) from a reportable-income ledger.

Customers (and staff on lookup) can view issued 1099-INT copies. Staff
posts/voids reportable interest, generates year-end forms, files them,
and fulfills copy requests. Independent of APY posting (PR #49), period
statements (PR #52), spending categories (PR #55), and disputes (PR #58).

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
"""

from __future__ import annotations

import json
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
FORM_1099_INT = '1099-INT'
FORM_TYPES = frozenset({FORM_1099_INT})
BOX_INTEREST = 'interest'
BOX_EARLY_WITHDRAWAL = 'early_withdrawal'
BOX_WITHHOLDING = 'withholding'
BOXES = frozenset({BOX_INTEREST, BOX_EARLY_WITHDRAWAL, BOX_WITHHOLDING})
BOX_ALIASES = {
    'box1': BOX_INTEREST,
    'box_1': BOX_INTEREST,
    'int': BOX_INTEREST,
    'apy': BOX_INTEREST,
    'promo_interest': BOX_INTEREST,
    'interest_income': BOX_INTEREST,
    'box2': BOX_EARLY_WITHDRAWAL,
    'box_2': BOX_EARLY_WITHDRAWAL,
    'ewp': BOX_EARLY_WITHDRAWAL,
    'penalty': BOX_EARLY_WITHDRAWAL,
    'early': BOX_EARLY_WITHDRAWAL,
    'box4': BOX_WITHHOLDING,
    'box_4': BOX_WITHHOLDING,
    'backup': BOX_WITHHOLDING,
    'backup_withholding': BOX_WITHHOLDING,
    'fed_withholding': BOX_WITHHOLDING,
}
INTEREST_KINDS = frozenset({'interest', 'apy', 'promo_interest', 'int'})
PENALTY_KINDS = frozenset({'early_withdrawal', 'penalty', 'ewp'})
WITHHOLDING_KINDS = frozenset({'withholding', 'backup_withholding', 'backup'})
INTEREST_WORDS = ('interest', 'apy', 'accrued')
PENALTY_WORDS = ('early withdrawal', 'early_withdrawal', 'penalty')
WITHHOLDING_WORDS = ('withholding', 'backup withholding', 'backup_withholding')
ENTRY_ACTIVE = 'active'
ENTRY_VOIDED = 'voided'
FORM_STATUSES = frozenset({'interim', 'issued', 'filed', 'corrected', 'void'})
COPY_CHANNELS = frozenset({'mail', 'electronic', 'branch'})
COPY_PENDING = 'pending'
COPY_FULFILLED = 'fulfilled'
COPY_DENIED = 'denied'
COPY_DECISIONS = frozenset({'fulfill', 'deny'})
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/tax.sqlite'
DEFAULT_PAYER_NAME = 'Konoha Bank'


class AccountError(ValueError):
    pass


class AmountError(ValueError):
    pass


class TaxError(ValueError):
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


def normalize_source_id(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return uuid.uuid4().hex
    return text[:120]


def normalize_form_type(value: Any, *, default: str = FORM_1099_INT) -> str:
    text = str(value or default).strip().upper().replace(' ', '-')
    aliases = {
        '1099': FORM_1099_INT,
        '1099INT': FORM_1099_INT,
        'INT': FORM_1099_INT,
        'INTEREST': FORM_1099_INT,
    }
    text = aliases.get(text.replace('-', ''), text)
    if text not in FORM_TYPES:
        raise TaxError('invalid_form', 'Unknown tax form type.')
    return text


def normalize_box(value: Any) -> str:
    text = str(value or '').strip().lower().replace(' ', '_').replace('-', '_')
    text = BOX_ALIASES.get(text, text)
    if text not in BOXES:
        raise TaxError('invalid_box', 'Box must be interest, early_withdrawal, or withholding.')
    return text


def normalize_channel(value: Any, *, default: str = 'electronic') -> str:
    text = str(value or default).strip().lower()
    aliases = {
        'email': 'electronic',
        'e': 'electronic',
        'pdf': 'electronic',
        'post': 'mail',
        'paper': 'mail',
        'office': 'branch',
        'pickup': 'branch',
    }
    text = aliases.get(text, text)
    if text not in COPY_CHANNELS:
        raise TaxError('invalid_channel', 'Copy channel must be mail, electronic, or branch.')
    return text


def normalize_copy_decision(value: Any) -> str:
    text = str(value or '').strip().lower()
    aliases = {
        'approve': 'fulfill',
        'complete': 'fulfill',
        'send': 'fulfill',
        'mailed': 'fulfill',
        'reject': 'deny',
        'decline': 'deny',
        'refuse': 'deny',
    }
    text = aliases.get(text, text)
    if text not in COPY_DECISIONS:
        raise TaxError('invalid_decision', 'Decision must be fulfill or deny.')
    return text


def normalize_tin_last4(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        return ''
    return digits[-4:].zfill(4)


def normalize_recipient(value: Any) -> Dict[str, str]:
    payload = value if isinstance(value, dict) else {}
    name = str(payload.get('name') or '').strip()[:120]
    address = str(payload.get('address') or '').strip()[:240]
    tin = normalize_tin_last4(payload.get('tin_last4') or payload.get('tin') or '')
    return {'name': name, 'address': address, 'tin_last4': tin}


def tax_year_of(ts: float) -> int:
    return time.gmtime(float(ts)).tm_year


def resolve_tax_year(value: Any, *, now: float, default: str = 'prior') -> int:
    current = tax_year_of(now)
    if value is None or (isinstance(value, str) and not str(value).strip()):
        return current if default == 'current' else current - 1
    text = str(value).strip().lower()
    if text in {'current', 'ytd', 'this'}:
        return current
    if text in {'prior', 'previous', 'last'}:
        return current - 1
    try:
        year = int(text)
    except (TypeError, ValueError):
        raise TaxError('invalid_year', 'Tax year must be a calendar year.') from None
    if year < 2000 or year > 2100:
        raise TaxError('invalid_year', 'Tax year is out of range.')
    return year


def classify_box(kind: Any, direction: Any, description: Any = '') -> Optional[str]:
    kind_text = str(kind or '').strip().lower().replace('-', '_').replace(' ', '_')
    aliases = {
        'direct_deposit': 'credit',
        'deposit': 'deposit',
        'bonus': 'open',
        'open': 'open',
        'withdrawal': 'withdraw',
        'debit': 'withdraw',
        'transfer_in': 'credit',
        'transfer_out': 'withdraw',
        'xfer_in': 'credit',
        'xfer_out': 'withdraw',
    }
    kind_text = aliases.get(kind_text, kind_text)
    dir_text = str(direction or '').strip().lower()
    if dir_text in {'out', 'debit'}:
        dir_text = 'debit'
    elif dir_text in {'in', 'credit'}:
        dir_text = 'credit'
    desc = str(description or '').strip().lower()
    if kind_text in INTEREST_KINDS or any(word in desc for word in INTEREST_WORDS):
        if dir_text and dir_text != 'credit':
            return None
        return BOX_INTEREST
    if kind_text in PENALTY_KINDS or any(word in desc for word in PENALTY_WORDS):
        if dir_text == 'credit':
            return None
        return BOX_EARLY_WITHDRAWAL
    if kind_text in WITHHOLDING_KINDS or any(word in desc for word in WITHHOLDING_WORDS):
        return BOX_WITHHOLDING
    return None


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


@dataclass
class ReportableEntry:
    entry_id: str
    source_id: str
    userid: str
    account: str
    amount: str
    box: str
    tax_year: int
    origin: str
    description: str
    status: str
    actor: str
    created_at: float
    voided_at: Optional[float] = None
    voided_by: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'entry_id': self.entry_id,
            'source_id': self.source_id,
            'userid': self.userid,
            'account': self.account,
            'amount': self.amount,
            'box': self.box,
            'tax_year': self.tax_year,
            'origin': self.origin,
            'description': self.description,
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'voided_at': self.voided_at,
            'voided_by': self.voided_by,
            'active': self.status == ENTRY_ACTIVE,
        }


@dataclass
class TaxForm:
    form_id: str
    userid: str
    form_type: str
    tax_year: int
    box1: str
    box2: str
    box4: str
    required: bool
    status: str
    accounts: List[Dict[str, str]]
    recipient_name: str
    recipient_address: str
    recipient_tin_last4: str
    payer_name: str
    payer_tin_last4: str
    payer_address: str
    actor: str
    actor_type: str
    created_at: float
    updated_at: float
    filed_at: Optional[float] = None
    filed_by: Optional[str] = None
    correction_of: Optional[str] = None
    corrected_by_form: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'form_id': self.form_id,
            'userid': self.userid,
            'form_type': self.form_type,
            'tax_year': self.tax_year,
            'box1': self.box1,
            'box2': self.box2,
            'box4': self.box4,
            'interest': self.box1,
            'early_withdrawal': self.box2,
            'withholding': self.box4,
            'required': self.required,
            'status': self.status,
            'accounts': list(self.accounts),
            'recipient': {
                'name': self.recipient_name,
                'address': self.recipient_address,
                'tin_last4': self.recipient_tin_last4,
            },
            'payer': {
                'name': self.payer_name,
                'tin_last4': self.payer_tin_last4,
                'address': self.payer_address,
            },
            'actor': self.actor,
            'actor_type': self.actor_type,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'filed_at': self.filed_at,
            'filed_by': self.filed_by,
            'correction_of': self.correction_of,
            'corrected_by_form': self.corrected_by_form,
            'official': self.status in {'issued', 'filed'},
        }


@dataclass
class CopyRequest:
    request_id: str
    form_id: str
    userid: str
    channel: str
    status: str
    actor: str
    created_at: float
    decided_at: Optional[float] = None
    decided_by: Optional[str] = None
    decision_note: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'request_id': self.request_id,
            'form_id': self.form_id,
            'userid': self.userid,
            'channel': self.channel,
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'decided_at': self.decided_at,
            'decided_by': self.decided_by,
            'decision_note': self.decision_note,
            'pending': self.status == COPY_PENDING,
        }


@dataclass(frozen=True)
class TaxPolicy:
    enabled: bool = True
    customer_generate: bool = True
    customer_request_copy: bool = True
    threshold: Decimal = Decimal('10.00')
    max_entries_year: int = 200
    max_forms_year: int = 8
    max_pending_copies: int = 3
    lookback_years: int = 7
    allow_file_interim: bool = False
    payer_name: str = DEFAULT_PAYER_NAME
    payer_tin_last4: str = ''
    payer_address: str = ''
    form_types: frozenset = field(default_factory=lambda: frozenset(FORM_TYPES))

    @classmethod
    def from_env(cls) -> 'TaxPolicy':
        return cls(
            enabled=_env_bool('TAX_ENABLED', True),
            customer_generate=_env_bool('TAX_CUSTOMER_GENERATE', True),
            customer_request_copy=_env_bool('TAX_CUSTOMER_COPY', True),
            threshold=_env_money('TAX_INT_THRESHOLD', '10.00'),
            max_entries_year=max(1, _env_int('TAX_MAX_ENTRIES_YEAR', 200)),
            max_forms_year=max(1, _env_int('TAX_MAX_FORMS_YEAR', 8)),
            max_pending_copies=max(1, _env_int('TAX_MAX_PENDING_COPIES', 3)),
            lookback_years=max(1, _env_int('TAX_LOOKBACK_YEARS', 7)),
            allow_file_interim=_env_bool('TAX_ALLOW_FILE_INTERIM', False),
            payer_name=(os.environ.get('TAX_PAYER_NAME') or DEFAULT_PAYER_NAME).strip()[:80],
            payer_tin_last4=normalize_tin_last4(os.environ.get('TAX_PAYER_TIN_LAST4') or ''),
            payer_address=(os.environ.get('TAX_PAYER_ADDRESS') or '').strip()[:240],
        )


class MemoryTaxStore:
    def __init__(self) -> None:
        self._entries: Dict[str, ReportableEntry] = {}
        self._by_source: Dict[str, str] = {}
        self._forms: Dict[str, TaxForm] = {}
        self._copies: Dict[str, CopyRequest] = {}
        self._lock = threading.Lock()

    def put_entry(self, entry: ReportableEntry) -> ReportableEntry:
        with self._lock:
            existing_id = self._by_source.get(entry.source_id)
            if existing_id is not None:
                return self._entries[existing_id]
            self._entries[entry.entry_id] = entry
            self._by_source[entry.source_id] = entry.entry_id
            return entry

    def get_entry(self, entry_id: str) -> Optional[ReportableEntry]:
        with self._lock:
            return self._entries.get(entry_id)

    def get_entry_by_source(self, source_id: str) -> Optional[ReportableEntry]:
        with self._lock:
            entry_id = self._by_source.get(source_id)
            if not entry_id:
                return None
            return self._entries.get(entry_id)

    def update_entry(self, entry: ReportableEntry) -> None:
        with self._lock:
            self._entries[entry.entry_id] = entry
            self._by_source[entry.source_id] = entry.entry_id

    def list_entries(
        self,
        userid: Optional[str] = None,
        tax_year: Optional[int] = None,
        *,
        include_voided: bool = False,
    ) -> List[ReportableEntry]:
        with self._lock:
            rows = list(self._entries.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if tax_year is not None:
            rows = [row for row in rows if row.tax_year == tax_year]
        if not include_voided:
            rows = [row for row in rows if row.status == ENTRY_ACTIVE]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows

    def put_form(self, form: TaxForm) -> None:
        with self._lock:
            self._forms[form.form_id] = form

    def get_form(self, form_id: str) -> Optional[TaxForm]:
        with self._lock:
            return self._forms.get(form_id)

    def update_form(self, form: TaxForm) -> None:
        with self._lock:
            self._forms[form.form_id] = form

    def find_original_form(self, userid: str, form_type: str, tax_year: int) -> Optional[TaxForm]:
        with self._lock:
            matches = [
                row for row in self._forms.values()
                if row.userid == userid
                and row.form_type == form_type
                and row.tax_year == tax_year
                and not row.correction_of
            ]
        if not matches:
            return None
        matches.sort(key=lambda row: row.created_at, reverse=True)
        return matches[0]

    def list_forms(self, userid: Optional[str] = None, tax_year: Optional[int] = None) -> List[TaxForm]:
        with self._lock:
            rows = list(self._forms.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if tax_year is not None:
            rows = [row for row in rows if row.tax_year == tax_year]
        rows.sort(key=lambda row: (row.tax_year, row.created_at), reverse=True)
        return rows

    def put_copy(self, copy: CopyRequest) -> None:
        with self._lock:
            self._copies[copy.request_id] = copy

    def get_copy(self, request_id: str) -> Optional[CopyRequest]:
        with self._lock:
            return self._copies.get(request_id)

    def update_copy(self, copy: CopyRequest) -> None:
        with self._lock:
            self._copies[copy.request_id] = copy

    def list_copies(
        self,
        userid: Optional[str] = None,
        form_id: Optional[str] = None,
        statuses: Optional[Iterable[str]] = None,
    ) -> List[CopyRequest]:
        wanted = set(statuses) if statuses is not None else None
        with self._lock:
            rows = list(self._copies.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if form_id is not None:
            rows = [row for row in rows if row.form_id == form_id]
        if wanted is not None:
            rows = [row for row in rows if row.status in wanted]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows


class SqliteTaxStore:
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
                CREATE TABLE IF NOT EXISTS entries (
                    entry_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL UNIQUE,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    box TEXT NOT NULL,
                    tax_year INTEGER NOT NULL,
                    origin TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    voided_at REAL,
                    voided_by TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS forms (
                    form_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    form_type TEXT NOT NULL,
                    tax_year INTEGER NOT NULL,
                    box1 TEXT NOT NULL,
                    box2 TEXT NOT NULL,
                    box4 TEXT NOT NULL,
                    required INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    accounts_json TEXT NOT NULL,
                    recipient_name TEXT NOT NULL,
                    recipient_address TEXT NOT NULL,
                    recipient_tin_last4 TEXT NOT NULL,
                    payer_name TEXT NOT NULL,
                    payer_tin_last4 TEXT NOT NULL,
                    payer_address TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    actor_type TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    filed_at REAL,
                    filed_by TEXT,
                    correction_of TEXT,
                    corrected_by_form TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS copies (
                    request_id TEXT PRIMARY KEY,
                    form_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    decided_at REAL,
                    decided_by TEXT,
                    decision_note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute('CREATE INDEX IF NOT EXISTS idx_entries_user_year ON entries(userid, tax_year, status)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_forms_user_year ON forms(userid, form_type, tax_year)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_copies_user ON copies(userid, status)')
            conn.commit()

    def put_entry(self, entry: ReportableEntry) -> ReportableEntry:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM entries WHERE source_id = ?', (entry.source_id,)
            ).fetchone()
            if existing is not None:
                return self._entry_from_row(existing)
            conn.execute(
                """
                INSERT INTO entries (
                    entry_id, source_id, userid, account, amount, box, tax_year,
                    origin, description, status, actor, created_at, voided_at, voided_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._entry_row(entry),
            )
            conn.commit()
            return entry

    def get_entry(self, entry_id: str) -> Optional[ReportableEntry]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM entries WHERE entry_id = ?', (entry_id,)).fetchone()
        return self._entry_from_row(row) if row else None

    def get_entry_by_source(self, source_id: str) -> Optional[ReportableEntry]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM entries WHERE source_id = ?', (source_id,)).fetchone()
        return self._entry_from_row(row) if row else None

    def update_entry(self, entry: ReportableEntry) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE entries SET
                    source_id=?, userid=?, account=?, amount=?, box=?, tax_year=?,
                    origin=?, description=?, status=?, actor=?, created_at=?,
                    voided_at=?, voided_by=?
                WHERE entry_id=?
                """,
                self._entry_row(entry)[1:] + (entry.entry_id,),
            )
            conn.commit()

    def list_entries(
        self,
        userid: Optional[str] = None,
        tax_year: Optional[int] = None,
        *,
        include_voided: bool = False,
    ) -> List[ReportableEntry]:
        clauses = []
        params: List[Any] = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if tax_year is not None:
            clauses.append('tax_year = ?')
            params.append(tax_year)
        if not include_voided:
            clauses.append("status = 'active'")
        sql = 'SELECT * FROM entries'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._entry_from_row(row) for row in rows]

    def put_form(self, form: TaxForm) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO forms (
                    form_id, userid, form_type, tax_year, box1, box2, box4, required,
                    status, accounts_json, recipient_name, recipient_address,
                    recipient_tin_last4, payer_name, payer_tin_last4, payer_address,
                    actor, actor_type, created_at, updated_at, filed_at, filed_by,
                    correction_of, corrected_by_form
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._form_row(form),
            )
            conn.commit()

    def get_form(self, form_id: str) -> Optional[TaxForm]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM forms WHERE form_id = ?', (form_id,)).fetchone()
        return self._form_from_row(row) if row else None

    def update_form(self, form: TaxForm) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE forms SET
                    userid=?, form_type=?, tax_year=?, box1=?, box2=?, box4=?, required=?,
                    status=?, accounts_json=?, recipient_name=?, recipient_address=?,
                    recipient_tin_last4=?, payer_name=?, payer_tin_last4=?, payer_address=?,
                    actor=?, actor_type=?, created_at=?, updated_at=?, filed_at=?, filed_by=?,
                    correction_of=?, corrected_by_form=?
                WHERE form_id=?
                """,
                self._form_row(form)[1:] + (form.form_id,),
            )
            conn.commit()

    def find_original_form(self, userid: str, form_type: str, tax_year: int) -> Optional[TaxForm]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM forms
                WHERE userid = ? AND form_type = ? AND tax_year = ? AND correction_of IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (userid, form_type, tax_year),
            ).fetchone()
        return self._form_from_row(row) if row else None

    def list_forms(self, userid: Optional[str] = None, tax_year: Optional[int] = None) -> List[TaxForm]:
        clauses = []
        params: List[Any] = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if tax_year is not None:
            clauses.append('tax_year = ?')
            params.append(tax_year)
        sql = 'SELECT * FROM forms'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY tax_year DESC, created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._form_from_row(row) for row in rows]

    def put_copy(self, copy: CopyRequest) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO copies (
                    request_id, form_id, userid, channel, status, actor,
                    created_at, decided_at, decided_by, decision_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._copy_row(copy),
            )
            conn.commit()

    def get_copy(self, request_id: str) -> Optional[CopyRequest]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM copies WHERE request_id = ?', (request_id,)).fetchone()
        return self._copy_from_row(row) if row else None

    def update_copy(self, copy: CopyRequest) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE copies SET
                    form_id=?, userid=?, channel=?, status=?, actor=?, created_at=?,
                    decided_at=?, decided_by=?, decision_note=?
                WHERE request_id=?
                """,
                self._copy_row(copy)[1:] + (copy.request_id,),
            )
            conn.commit()

    def list_copies(
        self,
        userid: Optional[str] = None,
        form_id: Optional[str] = None,
        statuses: Optional[Iterable[str]] = None,
    ) -> List[CopyRequest]:
        clauses = []
        params: List[Any] = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if form_id is not None:
            clauses.append('form_id = ?')
            params.append(form_id)
        sql = 'SELECT * FROM copies'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        copies = [self._copy_from_row(row) for row in rows]
        if statuses is not None:
            wanted = set(statuses)
            copies = [row for row in copies if row.status in wanted]
        return copies

    @staticmethod
    def _entry_row(entry: ReportableEntry) -> Tuple[Any, ...]:
        return (
            entry.entry_id, entry.source_id, entry.userid, entry.account, entry.amount,
            entry.box, entry.tax_year, entry.origin, entry.description, entry.status,
            entry.actor, entry.created_at, entry.voided_at, entry.voided_by,
        )

    @staticmethod
    def _entry_from_row(row: sqlite3.Row) -> ReportableEntry:
        return ReportableEntry(
            entry_id=row['entry_id'],
            source_id=row['source_id'],
            userid=row['userid'],
            account=row['account'],
            amount=row['amount'],
            box=row['box'],
            tax_year=int(row['tax_year']),
            origin=row['origin'],
            description=row['description'] or '',
            status=row['status'],
            actor=row['actor'],
            created_at=float(row['created_at']),
            voided_at=None if row['voided_at'] is None else float(row['voided_at']),
            voided_by=row['voided_by'],
        )

    @staticmethod
    def _accounts_dumps(accounts: List[Dict[str, str]]) -> str:
        return json.dumps(accounts)

    @staticmethod
    def _accounts_loads(raw: str) -> List[Dict[str, str]]:
        try:
            data = json.loads(raw or '[]')
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def _form_row(self, form: TaxForm) -> Tuple[Any, ...]:
        return (
            form.form_id, form.userid, form.form_type, form.tax_year, form.box1,
            form.box2, form.box4, 1 if form.required else 0, form.status,
            self._accounts_dumps(form.accounts), form.recipient_name, form.recipient_address,
            form.recipient_tin_last4, form.payer_name, form.payer_tin_last4, form.payer_address,
            form.actor, form.actor_type, form.created_at, form.updated_at, form.filed_at,
            form.filed_by, form.correction_of, form.corrected_by_form,
        )

    def _form_from_row(self, row: sqlite3.Row) -> TaxForm:
        return TaxForm(
            form_id=row['form_id'],
            userid=row['userid'],
            form_type=row['form_type'],
            tax_year=int(row['tax_year']),
            box1=row['box1'],
            box2=row['box2'],
            box4=row['box4'],
            required=bool(row['required']),
            status=row['status'],
            accounts=self._accounts_loads(row['accounts_json']),
            recipient_name=row['recipient_name'] or '',
            recipient_address=row['recipient_address'] or '',
            recipient_tin_last4=row['recipient_tin_last4'] or '',
            payer_name=row['payer_name'] or '',
            payer_tin_last4=row['payer_tin_last4'] or '',
            payer_address=row['payer_address'] or '',
            actor=row['actor'],
            actor_type=row['actor_type'],
            created_at=float(row['created_at']),
            updated_at=float(row['updated_at']),
            filed_at=None if row['filed_at'] is None else float(row['filed_at']),
            filed_by=row['filed_by'],
            correction_of=row['correction_of'],
            corrected_by_form=row['corrected_by_form'],
        )

    @staticmethod
    def _copy_row(copy: CopyRequest) -> Tuple[Any, ...]:
        return (
            copy.request_id, copy.form_id, copy.userid, copy.channel, copy.status,
            copy.actor, copy.created_at, copy.decided_at, copy.decided_by, copy.decision_note,
        )

    @staticmethod
    def _copy_from_row(row: sqlite3.Row) -> CopyRequest:
        return CopyRequest(
            request_id=row['request_id'],
            form_id=row['form_id'],
            userid=row['userid'],
            channel=row['channel'],
            status=row['status'],
            actor=row['actor'],
            created_at=float(row['created_at']),
            decided_at=None if row['decided_at'] is None else float(row['decided_at']),
            decided_by=row['decided_by'],
            decision_note=row['decision_note'] or '',
        )


class TaxService:
    def __init__(
        self,
        policy: TaxPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        recipient_fn: Optional[Callable[[str], Dict[str, str]]] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or time.time
        self.recipient_fn = recipient_fn

    @staticmethod
    def _actor_is_employee(actor_type: str) -> bool:
        return str(actor_type or '') in EMPLOYEE_ROLES

    def _ensure_enabled(self) -> None:
        if not self.policy.enabled:
            raise TaxError('tax_disabled', 'Tax reporting is disabled.')

    def _assert_year(self, year: int, *, now: float, allow_current: bool = True) -> None:
        current = tax_year_of(now)
        if year > current:
            raise TaxError('year_in_future', 'Cannot report a future tax year.')
        if year < current - self.policy.lookback_years:
            raise TaxError('year_too_old', 'Tax year is outside the retention window.')
        if not allow_current and year >= current:
            raise TaxError('year_not_closed', 'Tax year is still open.')

    def _recipient_for(self, userid: str, override: Any = None) -> Dict[str, str]:
        base: Dict[str, str] = {'name': '', 'address': '', 'tin_last4': ''}
        if callable(self.recipient_fn):
            try:
                loaded = self.recipient_fn(userid) or {}
            except Exception:
                loaded = {}
            if isinstance(loaded, dict):
                base.update(normalize_recipient(loaded))
        if override:
            incoming = normalize_recipient(override)
            for key, value in incoming.items():
                if value:
                    base[key] = value
        return base

    def _aggregate(self, userid: str, tax_year: int) -> Tuple[Dict[str, Decimal], List[Dict[str, str]]]:
        boxes = {
            BOX_INTEREST: Decimal('0.00'),
            BOX_EARLY_WITHDRAWAL: Decimal('0.00'),
            BOX_WITHHOLDING: Decimal('0.00'),
        }
        by_account: Dict[str, Dict[str, Decimal]] = {}
        for entry in self.store.list_entries(userid, tax_year):
            amount = parse_money(entry.amount, allow_zero=True)
            boxes[entry.box] = boxes.get(entry.box, Decimal('0.00')) + amount
            bucket = by_account.setdefault(entry.account, {
                BOX_INTEREST: Decimal('0.00'),
                BOX_EARLY_WITHDRAWAL: Decimal('0.00'),
                BOX_WITHHOLDING: Decimal('0.00'),
            })
            bucket[entry.box] = bucket.get(entry.box, Decimal('0.00')) + amount
        accounts = [
            {
                'account': account,
                'box1': money_str(values[BOX_INTEREST]),
                'box2': money_str(values[BOX_EARLY_WITHDRAWAL]),
                'box4': money_str(values[BOX_WITHHOLDING]),
            }
            for account, values in sorted(by_account.items())
        ]
        return boxes, accounts

    def _form_status(self, tax_year: int, box1: Decimal, *, now: float) -> str:
        current = tax_year_of(now)
        if tax_year >= current:
            return 'interim'
        return 'issued'

    def _load_owned_form(self, form_id: str, actor: str, actor_type: str) -> TaxForm:
        form = self.store.get_form(form_id)
        if form is None:
            raise TaxError('form_not_found', 'Tax form not found.')
        if not self._actor_is_employee(actor_type) and form.userid != str(actor):
            raise TaxError('tax_forbidden', 'Not allowed to access this tax form.')
        return form

    def observe(
        self,
        account: Any,
        amount: Any,
        kind: Any,
        *,
        direction: Any = None,
        userid: Optional[str] = None,
        description: Any = '',
        source_id: Any = None,
        created_at: Optional[float] = None,
    ) -> Optional[ReportableEntry]:
        if not self.policy.enabled:
            return None
        box = classify_box(kind, direction, description)
        if box is None:
            return None
        owner = str(userid or '').strip()
        if not owner:
            return None
        now = float(created_at if created_at is not None else self.clock())
        year = tax_year_of(now)
        try:
            self._assert_year(year, now=now)
            parsed = parse_money(amount)
            acct = normalize_account(account)
        except (AccountError, AmountError, TaxError):
            return None
        source = normalize_source_id(source_id)
        existing = self.store.get_entry_by_source(source)
        if existing is not None:
            return existing
        active = self.store.list_entries(owner, year)
        if len(active) >= self.policy.max_entries_year:
            return None
        entry = ReportableEntry(
            entry_id=uuid.uuid4().hex,
            source_id=source,
            userid=owner,
            account=acct,
            amount=money_str(parsed),
            box=box,
            tax_year=year,
            origin='observe',
            description=normalize_note(description),
            status=ENTRY_ACTIVE,
            actor='system',
            created_at=now,
        )
        return self.store.put_entry(entry)

    def post(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        amount: Any,
        box: Any,
        tax_year: Any = None,
        description: Any = '',
        source_id: Any = None,
    ) -> ReportableEntry:
        self._ensure_enabled()
        if not self._actor_is_employee(actor_type):
            raise TaxError('tax_forbidden', 'Only bank staff can post reportable income.')
        owner = str(owner_userid or '').strip()
        if not owner:
            raise TaxError('missing_customer_id', 'Customer id is required.')
        now = float(self.clock())
        year = resolve_tax_year(tax_year, now=now, default='current')
        self._assert_year(year, now=now)
        parsed = parse_money(amount)
        acct = normalize_account(account)
        box_name = normalize_box(box)
        source = normalize_source_id(source_id)
        existing = self.store.get_entry_by_source(source)
        if existing is not None:
            raise TaxError('entry_duplicate', 'A reportable entry already exists for this source.')
        active = self.store.list_entries(owner, year)
        if len(active) >= self.policy.max_entries_year:
            raise TaxError('entry_limit', 'Reportable entry limit reached for this tax year.')
        entry = ReportableEntry(
            entry_id=uuid.uuid4().hex,
            source_id=source,
            userid=owner,
            account=acct,
            amount=money_str(parsed),
            box=box_name,
            tax_year=year,
            origin='staff',
            description=normalize_note(description),
            status=ENTRY_ACTIVE,
            actor=str(actor),
            created_at=now,
        )
        return self.store.put_entry(entry)

    def void_entry(
        self,
        *,
        entry_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> ReportableEntry:
        self._ensure_enabled()
        if not self._actor_is_employee(actor_type):
            raise TaxError('tax_forbidden', 'Only bank staff can void reportable income.')
        entry = self.store.get_entry(str(entry_id).strip())
        if entry is None:
            raise TaxError('entry_not_found', 'Reportable entry not found.')
        if entry.status == ENTRY_VOIDED:
            raise TaxError('already_voided', 'Entry is already voided.')
        now = float(self.clock())
        entry.status = ENTRY_VOIDED
        entry.voided_at = now
        entry.voided_by = str(actor)
        if note:
            extra = normalize_note(note, limit=120)
            entry.description = (entry.description + ' | void: ' + extra).strip()[:500]
        self.store.update_entry(entry)
        return entry

    def _active_form(self, userid: str, form_type: str, tax_year: int) -> Optional[TaxForm]:
        rows = [
            row for row in self.store.list_forms(userid, tax_year)
            if row.form_type == form_type and row.status in {'interim', 'issued', 'filed'}
        ]
        if not rows:
            return None
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows[0]

    def generate(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        tax_year: Any = None,
        recipient: Any = None,
        force: bool = False,
        form_type: Any = FORM_1099_INT,
    ) -> Tuple[TaxForm, bool]:
        self._ensure_enabled()
        owner = str(owner_userid or '').strip()
        if not owner:
            raise TaxError('missing_customer_id', 'Customer id is required.')
        employee = self._actor_is_employee(actor_type)
        if not employee:
            if owner != str(actor):
                raise TaxError('tax_forbidden', 'Customers may only generate their own tax forms.')
            if not self.policy.customer_generate:
                raise TaxError('tax_forbidden', 'Customers cannot generate tax forms.')
        now = float(self.clock())
        year = resolve_tax_year(tax_year, now=now, default='prior')
        self._assert_year(year, now=now)
        kind = normalize_form_type(form_type)
        existing = self._active_form(owner, kind, year)
        boxes, accounts = self._aggregate(owner, year)
        box1 = boxes[BOX_INTEREST]
        recipient_info = self._recipient_for(owner, recipient)
        if existing is not None and not force:
            return existing, False
        if existing is not None and force:
            if not employee:
                raise TaxError('tax_forbidden', 'Only bank staff can rebuild an issued form.')
            if existing.status == 'filed':
                raise TaxError('already_filed', 'Filed forms must be corrected, not rebuilt.')
            if existing.status in {'void', 'corrected'}:
                raise TaxError('already_resolved', 'This form is no longer active.')
            existing.box1 = money_str(box1)
            existing.box2 = money_str(boxes[BOX_EARLY_WITHDRAWAL])
            existing.box4 = money_str(boxes[BOX_WITHHOLDING])
            existing.required = box1 >= self.policy.threshold
            existing.status = self._form_status(year, box1, now=now)
            existing.accounts = accounts
            if recipient_info.get('name'):
                existing.recipient_name = recipient_info['name']
            if recipient_info.get('address'):
                existing.recipient_address = recipient_info['address']
            if recipient_info.get('tin_last4'):
                existing.recipient_tin_last4 = recipient_info['tin_last4']
            existing.updated_at = now
            existing.actor = str(actor)
            existing.actor_type = str(actor_type)
            self.store.update_form(existing)
            return existing, False
        year_forms = [row for row in self.store.list_forms(owner, year) if row.form_type == kind]
        if len(year_forms) >= self.policy.max_forms_year:
            raise TaxError('form_limit', 'Tax form limit reached for this year.')
        form = TaxForm(
            form_id=uuid.uuid4().hex,
            userid=owner,
            form_type=kind,
            tax_year=year,
            box1=money_str(box1),
            box2=money_str(boxes[BOX_EARLY_WITHDRAWAL]),
            box4=money_str(boxes[BOX_WITHHOLDING]),
            required=box1 >= self.policy.threshold,
            status=self._form_status(year, box1, now=now),
            accounts=accounts,
            recipient_name=recipient_info.get('name') or '',
            recipient_address=recipient_info.get('address') or '',
            recipient_tin_last4=recipient_info.get('tin_last4') or '',
            payer_name=self.policy.payer_name,
            payer_tin_last4=self.policy.payer_tin_last4,
            payer_address=self.policy.payer_address,
            actor=str(actor),
            actor_type=str(actor_type),
            created_at=now,
            updated_at=now,
        )
        self.store.put_form(form)
        return form, True

    def get_form(self, *, form_id: str, actor: str, actor_type: str) -> TaxForm:
        self._ensure_enabled()
        return self._load_owned_form(str(form_id).strip(), actor, actor_type)

    def file(
        self,
        *,
        form_id: str,
        actor: str,
        actor_type: str,
    ) -> TaxForm:
        self._ensure_enabled()
        if not self._actor_is_employee(actor_type):
            raise TaxError('tax_forbidden', 'Only bank staff can file a tax form.')
        form = self._load_owned_form(str(form_id).strip(), actor, actor_type)
        now = float(self.clock())
        if form.status == 'filed':
            raise TaxError('already_filed', 'Form is already filed.')
        if form.status in {'void', 'corrected'}:
            raise TaxError('already_resolved', 'This form is no longer active.')
        if form.status == 'interim' and not self.policy.allow_file_interim:
            raise TaxError('year_not_closed', 'Cannot file an interim current-year form.')
        form.status = 'filed'
        form.filed_at = now
        form.filed_by = str(actor)
        form.updated_at = now
        self.store.update_form(form)
        return form

    def correct(
        self,
        *,
        form_id: str,
        actor: str,
        actor_type: str,
        recipient: Any = None,
    ) -> TaxForm:
        self._ensure_enabled()
        if not self._actor_is_employee(actor_type):
            raise TaxError('tax_forbidden', 'Only bank staff can issue a correction.')
        original = self._load_owned_form(str(form_id).strip(), actor, actor_type)
        if original.status in {'void', 'corrected'}:
            raise TaxError('already_resolved', 'This form is no longer active.')
        now = float(self.clock())
        year_forms = [
            row for row in self.store.list_forms(original.userid, original.tax_year)
            if row.form_type == original.form_type
        ]
        if len(year_forms) >= self.policy.max_forms_year:
            raise TaxError('form_limit', 'Tax form limit reached for this year.')
        boxes, accounts = self._aggregate(original.userid, original.tax_year)
        box1 = boxes[BOX_INTEREST]
        recipient_info = self._recipient_for(original.userid, recipient)
        if not recipient_info.get('name'):
            recipient_info['name'] = original.recipient_name
        if not recipient_info.get('address'):
            recipient_info['address'] = original.recipient_address
        if not recipient_info.get('tin_last4'):
            recipient_info['tin_last4'] = original.recipient_tin_last4
        correction = TaxForm(
            form_id=uuid.uuid4().hex,
            userid=original.userid,
            form_type=original.form_type,
            tax_year=original.tax_year,
            box1=money_str(box1),
            box2=money_str(boxes[BOX_EARLY_WITHDRAWAL]),
            box4=money_str(boxes[BOX_WITHHOLDING]),
            required=box1 >= self.policy.threshold,
            status='issued' if original.tax_year < tax_year_of(now) else 'interim',
            accounts=accounts,
            recipient_name=recipient_info.get('name') or '',
            recipient_address=recipient_info.get('address') or '',
            recipient_tin_last4=recipient_info.get('tin_last4') or '',
            payer_name=self.policy.payer_name,
            payer_tin_last4=self.policy.payer_tin_last4,
            payer_address=self.policy.payer_address,
            actor=str(actor),
            actor_type=str(actor_type),
            created_at=now,
            updated_at=now,
            correction_of=original.form_id,
        )
        original.status = 'corrected'
        original.corrected_by_form = correction.form_id
        original.updated_at = now
        self.store.put_form(correction)
        self.store.update_form(original)
        return correction

    def request_copy(
        self,
        *,
        form_id: str,
        actor: str,
        actor_type: str,
        channel: Any = 'electronic',
    ) -> CopyRequest:
        self._ensure_enabled()
        form = self._load_owned_form(str(form_id).strip(), actor, actor_type)
        employee = self._actor_is_employee(actor_type)
        if not employee:
            if not self.policy.customer_request_copy:
                raise TaxError('tax_forbidden', 'Customers cannot request official copies.')
            if form.userid != str(actor):
                raise TaxError('tax_forbidden', 'Not allowed to request this copy.')
        if form.status in {'void'}:
            raise TaxError('already_resolved', 'Cannot request a copy of a void form.')
        pending = self.store.list_copies(form.userid, form.form_id, statuses={COPY_PENDING})
        if len(pending) >= self.policy.max_pending_copies:
            raise TaxError('request_limit', 'Too many pending copy requests for this form.')
        if any(row.channel == normalize_channel(channel) for row in pending):
            raise TaxError('request_duplicate', 'A pending copy request already exists for this channel.')
        copy = CopyRequest(
            request_id=uuid.uuid4().hex,
            form_id=form.form_id,
            userid=form.userid,
            channel=normalize_channel(channel),
            status=COPY_PENDING,
            actor=str(actor),
            created_at=float(self.clock()),
        )
        self.store.put_copy(copy)
        return copy

    def decide_copy(
        self,
        *,
        request_id: str,
        actor: str,
        actor_type: str,
        decision: Any,
        note: Any = '',
    ) -> CopyRequest:
        self._ensure_enabled()
        if not self._actor_is_employee(actor_type):
            raise TaxError('tax_forbidden', 'Only bank staff can fulfill copy requests.')
        copy = self.store.get_copy(str(request_id).strip())
        if copy is None:
            raise TaxError('request_not_found', 'Copy request not found.')
        if copy.status != COPY_PENDING:
            raise TaxError('already_resolved', 'Copy request is already resolved.')
        verdict = normalize_copy_decision(decision)
        now = float(self.clock())
        copy.status = COPY_FULFILLED if verdict == 'fulfill' else COPY_DENIED
        copy.decided_at = now
        copy.decided_by = str(actor)
        copy.decision_note = normalize_note(note)
        self.store.update_copy(copy)
        return copy

    def snapshot(self, userid: str, tax_year: Any = None) -> Dict[str, Any]:
        now = float(self.clock())
        current = tax_year_of(now)
        year = resolve_tax_year(tax_year, now=now, default='current') if tax_year not in (None, '') else current
        boxes, accounts = self._aggregate(userid, year)
        box1 = boxes[BOX_INTEREST]
        forms = self.store.list_forms(userid)
        entries = self.store.list_entries(userid, year)
        copies = self.store.list_copies(userid)
        return {
            'enabled': self.policy.enabled,
            'threshold': money_str(self.policy.threshold),
            'current_year': current,
            'prior_year': current - 1,
            'tax_year': year,
            'box1': money_str(box1),
            'box2': money_str(boxes[BOX_EARLY_WITHDRAWAL]),
            'box4': money_str(boxes[BOX_WITHHOLDING]),
            'required': box1 >= self.policy.threshold and year < current,
            'ytd_required': box1 >= self.policy.threshold,
            'accounts': accounts,
            'forms': [row.to_dict() for row in forms],
            'entries': [row.to_dict() for row in entries],
            'copy_requests': [row.to_dict() for row in copies],
            'payer': {
                'name': self.policy.payer_name,
                'tin_last4': self.policy.payer_tin_last4,
                'address': self.policy.payer_address,
            },
        }


_SERVICE: Optional[TaxService] = None


def set_service(service: Optional[TaxService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[TaxService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('TAX_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryTaxStore()
    path = os.environ.get('TAX_DB', DEFAULT_STORE_PATH)
    return SqliteTaxStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    recipient_fn: Optional[Callable[[str], Dict[str, str]]] = None,
) -> TaxService:
    if store is None:
        store = default_store()
    return TaxService(
        TaxPolicy.from_env(),
        store,
        clock=clock,
        recipient_fn=recipient_fn,
    )


def observe_movement(
    account: Any,
    amount: Any,
    kind: Any,
    *,
    direction: Any = None,
    userid: Optional[str] = None,
    description: Any = '',
    source_id: Any = None,
) -> None:
    service = get_service()
    if service is None:
        return
    try:
        service.observe(
            account, amount, kind,
            direction=direction, userid=userid,
            description=description, source_id=source_id,
        )
    except Exception:
        return


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
        'entry_duplicate': 409,
        'entry_limit': 409,
        'form_limit': 409,
        'request_duplicate': 409,
        'request_limit': 409,
        'already_filed': 409,
        'already_resolved': 409,
        'already_voided': 409,
        'tax_forbidden': 403,
        'tax_disabled': 403,
        'form_not_found': 404,
        'entry_not_found': 404,
        'request_not_found': 404,
        'invalid_year': 400,
        'invalid_box': 400,
        'invalid_form': 400,
        'invalid_channel': 400,
        'invalid_decision': 400,
        'invalid_amount': 400,
        'invalid_account': 400,
        'year_in_future': 400,
        'year_too_old': 400,
        'year_not_closed': 400,
        'missing_customer_id': 400,
    }.get(code, 400)


def _error_body(exc: TaxError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('form') is not None:
        body['form'] = exc.extra['form'].to_dict()
    if exc.extra.get('entry') is not None:
        body['entry'] = exc.extra['entry'].to_dict()
    return body


def handle_list_tax_forms(service: TaxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'TaxForms': service.snapshot(owner, values.get('tax_year'))}), 200


def handle_list_reportable(service: TaxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    snapshot = service.snapshot(owner, values.get('tax_year'))
    return jsonify({'entries': snapshot['entries'], 'TaxForms': snapshot}), 200


def handle_get_tax_form(service: TaxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    form_id = str(values.get('form_id') or '').strip()
    if not form_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_form_id'}), 400
    try:
        form = service.get_form(
            form_id=form_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
        )
    except TaxError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({'form': form.to_dict(), 'TaxForms': service.snapshot(form.userid)}), 200


def handle_generate_tax_form(service: TaxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    try:
        form, created = service.generate(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            tax_year=values.get('tax_year') or values.get('year'),
            recipient=values.get('recipient'),
            force=bool(values.get('force')),
            form_type=values.get('form_type') or FORM_1099_INT,
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except TaxError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Tax form generated' if created else 'Tax form already exists',
        'form': form.to_dict(),
        'TaxForms': service.snapshot(owner),
    }), 201 if created else 200


def handle_post_reportable(service: TaxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    try:
        entry = service.post(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            amount=values.get('amount'),
            box=values.get('box') or values.get('kind') or BOX_INTEREST,
            tax_year=values.get('tax_year') or values.get('year'),
            description=values.get('description') or values.get('note') or '',
            source_id=values.get('source_id'),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except TaxError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Reportable income posted',
        'entry': entry.to_dict(),
        'TaxForms': service.snapshot(owner),
    }), 201


def handle_void_reportable(service: TaxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    entry_id = str(values.get('entry_id') or '').strip()
    if not entry_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_entry_id'}), 400
    try:
        entry = service.void_entry(
            entry_id=entry_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
            note=values.get('note') or '',
        )
    except TaxError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Reportable entry voided',
        'entry': entry.to_dict(),
        'TaxForms': service.snapshot(entry.userid),
    }), 200


def handle_file_tax_form(service: TaxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    form_id = str(values.get('form_id') or '').strip()
    if not form_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_form_id'}), 400
    try:
        form = service.file(
            form_id=form_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
        )
    except TaxError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Tax form filed',
        'form': form.to_dict(),
        'TaxForms': service.snapshot(form.userid),
    }), 200


def handle_correct_tax_form(service: TaxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    form_id = str(values.get('form_id') or '').strip()
    if not form_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_form_id'}), 400
    try:
        form = service.correct(
            form_id=form_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
            recipient=values.get('recipient'),
        )
    except TaxError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Corrected tax form issued',
        'form': form.to_dict(),
        'TaxForms': service.snapshot(form.userid),
    }), 201


def handle_request_tax_copy(service: TaxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    form_id = str(values.get('form_id') or '').strip()
    if not form_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_form_id'}), 400
    try:
        copy = service.request_copy(
            form_id=form_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
            channel=values.get('channel') or 'electronic',
        )
    except TaxError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Copy requested',
        'request': copy.to_dict(),
        'TaxForms': service.snapshot(copy.userid),
    }), 201


def handle_decide_tax_copy(service: TaxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    request_id = str(values.get('request_id') or '').strip()
    if not request_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_request_id'}), 400
    try:
        copy = service.decide_copy(
            request_id=request_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
            decision=values.get('decision'),
            note=values.get('note') or '',
        )
    except TaxError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Copy request %s' % copy.status,
        'request': copy.to_dict(),
        'TaxForms': service.snapshot(copy.userid),
    }), 200


def attach_tax_routes(app, service: TaxService) -> None:
    @app.route('/listTaxForms', methods=['POST', 'GET'])
    def list_tax_forms_route():
        return handle_list_tax_forms(service)

    @app.route('/listReportable', methods=['POST', 'GET'])
    def list_reportable_route():
        return handle_list_reportable(service)

    @app.route('/getTaxForm', methods=['POST', 'GET'])
    def get_tax_form_route():
        return handle_get_tax_form(service)

    @app.route('/generateTaxForm', methods=['POST', 'GET'])
    def generate_tax_form_route():
        return handle_generate_tax_form(service)

    @app.route('/postReportable', methods=['POST', 'GET'])
    def post_reportable_route():
        return handle_post_reportable(service)

    @app.route('/voidReportable', methods=['POST', 'GET'])
    def void_reportable_route():
        return handle_void_reportable(service)

    @app.route('/fileTaxForm', methods=['POST', 'GET'])
    def file_tax_form_route():
        return handle_file_tax_form(service)

    @app.route('/correctTaxForm', methods=['POST', 'GET'])
    def correct_tax_form_route():
        return handle_correct_tax_form(service)

    @app.route('/requestTaxCopy', methods=['POST', 'GET'])
    def request_tax_copy_route():
        return handle_request_tax_copy(service)

    @app.route('/decideTaxCopy', methods=['POST', 'GET'])
    def decide_tax_copy_route():
        return handle_decide_tax_copy(service)
