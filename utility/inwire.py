"""Inbound Fedwire receive posting from an operator file.

Staff ingest incoming Fedwire Funds messages (FAIM tags or JSON). Matched
credits post to the beneficiary customer; unmatched, OFAC, dual-control, and
after-hours items sit in reusable queues. Independent of outbound Fedwire
(PR #73), ACH linking (PR #68), bill-pay ACH (PR #66), inbound payroll
splits (PR #64), FedNow/RTP origination (PR #83), and UK Pay (PR #87).
Existing `/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- FAIM tag parse / compose / multi-message file split
- Incoming IMAD uniqueness
- Receiver-ABA acceptance (this bank)
- Account-directory lookup (beneficiary account → customer)
- Incoming credit posting + same-day return (type 16) with reason codes
- Fedwire business-day / cutoff clock (reused)
- OFAC-style originator screening (reused)
- Dual-control release for high-value inbound credits

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Originator and beneficiary account numbers never appear in to_dict / snapshots.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    EMPLOYEE_ROLES,
    MONEY_QUANTUM,
    AccountError,
    AmountError,
    ScreenResult,
    WireCalendar,
    WireError,
    account_types_from_customer_payload,
    compose_imad,
    compose_omad,
    last4,
    money_str,
    normalize_aba,
    normalize_account,
    normalize_external_account,
    normalize_id,
    normalize_legal_name,
    normalize_note,
    normalize_party,
    normalize_purpose,
    normalize_source,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

IN_HELD = 'held'
IN_UNMATCHED = 'unmatched'
IN_QUEUED = 'queued'
IN_PENDING = 'pending_release'
IN_POSTED = 'posted'
IN_RETURNED = 'returned'
IN_REJECTED = 'rejected'
IN_FAILED = 'failed'
IN_STATUSES = frozenset({
    IN_HELD, IN_UNMATCHED, IN_QUEUED, IN_PENDING, IN_POSTED,
    IN_RETURNED, IN_REJECTED, IN_FAILED,
})
OPEN_INBOUNDS = frozenset({IN_HELD, IN_UNMATCHED, IN_QUEUED, IN_PENDING})
RETURNABLE_BEFORE_POST = frozenset({IN_HELD, IN_UNMATCHED, IN_QUEUED, IN_PENDING})
TYPE_CREDIT = '10'
TYPE_RETURN = '16'
TYPE_ALIASES = {
    '10': TYPE_CREDIT, 'ctr': TYPE_CREDIT, 'ctp': TYPE_CREDIT, 'btr': TYPE_CREDIT,
    'customer': TYPE_CREDIT, 'credit': TYPE_CREDIT,
    '16': TYPE_RETURN, 'rtc': TYPE_RETURN, 'return': TYPE_RETURN,
}
RETURN_REASONS = frozenset({
    'acct', 'name', 'ofac', 'nsfr', 'cust', 'dup', 'other',
})
RETURN_ALIASES = {
    'account': 'acct', 'closed': 'acct', 'unknown': 'acct', 'no_account': 'acct',
    'mismatch': 'name', 'beneficiary': 'name',
    'sanction': 'ofac', 'sanctions': 'ofac',
    'nsf': 'nsfr', 'insufficient': 'nsfr',
    'customer': 'cust', 'requested': 'cust',
    'duplicate': 'dup',
}
FAIM_ORDER = ('1100', '1110', '1500', '2000', '3100', '3400', '3600', '3700', '4200', '5000', '6000')
_TAG = re.compile(r'\{(\d{4})\}')
DEFAULT_STORE_PATH = 'SystemLogs/inwire.sqlite'
DEFAULT_RECEIVER_ABA = '021000021'
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)


class InWireError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


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


def _env_list(name: str) -> Tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(',') if item.strip())


def _classify_money_result(result: Any) -> str:
    if result in (None, True, 1):
        return 'ok'
    if isinstance(result, str):
        low = result.lower()
        if any(token in low for token in CREDIT_NSF):
            return 'nsf'
        if low in CREDIT_OK:
            return 'ok'
        return 'failed'
    return 'failed'


def normalize_imad(value: Any) -> str:
    """Accept FAIM IMAD / OMAD: YYYYMMDD + 8-char source + 6-digit sequence."""
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if len(text) != 22:
        raise InWireError('invalid_imad', 'IMAD must be 22 characters (YYYYMMDD + source + seq).')
    day, source, seq = text[:8], text[8:16], text[16:]
    if not day.isdigit() or not seq.isdigit():
        raise InWireError('invalid_imad', 'IMAD cycle or sequence is not numeric.')
    try:
        return compose_imad(day, source, int(seq))
    except WireError as exc:
        raise InWireError(exc.code, str(exc)) from exc


def parse_faim(text: Any) -> Dict[str, str]:
    """Parse one Fedwire FAIM message into tag → value."""
    raw = str(text or '')
    matches = list(_TAG.finditer(raw))
    if not matches:
        raise InWireError('invalid_faim', 'FAIM message has no tags.')
    fields: Dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        fields[match.group(1)] = raw[start:end].strip()
    return fields


def split_faim_file(text: Any) -> List[str]:
    """Split an operator file into messages that each start with {1100}."""
    raw = str(text or '')
    parts = re.split(r'(?=\{1100\})', raw)
    return [part.strip() for part in parts if part.strip() and '{1100}' in part]


def compose_faim(fields: Dict[str, str]) -> str:
    """Compose tags in canonical Fedwire order; unknown tags append sorted."""
    cleaned = {str(key).zfill(4)[-4:]: str(value or '').strip() for key, value in fields.items() if str(value or '').strip()}
    chunks = []
    seen = set()
    for tag in FAIM_ORDER:
        if tag in cleaned:
            chunks.append('{%s}%s' % (tag, cleaned[tag]))
            seen.add(tag)
    for tag in sorted(cleaned):
        if tag not in seen:
            chunks.append('{%s}%s' % (tag, cleaned[tag]))
    if not chunks:
        raise InWireError('invalid_faim', 'FAIM message has no tags.')
    return ''.join(chunks)


def compose_amount_tag(amount: Decimal) -> str:
    cents = int((amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN) * 100).to_integral_value())
    if cents < 0 or cents > 999999999999:
        raise InWireError('invalid_amount', 'Amount cannot be encoded in {2000}.')
    return '%012d' % cents


def parse_amount_tag(value: Any) -> Decimal:
    """{2000} is 12-digit cents. Dollar strings and shorter digit forms still parse."""
    text = str(value or '').strip().replace(',', '').replace('$', '')
    if not text:
        raise InWireError('invalid_amount', 'Amount is required.')
    if text.isdigit() and len(text) == 12:
        return (Decimal(text) / Decimal('100')).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    try:
        return parse_money(text)
    except AmountError as exc:
        raise InWireError('invalid_amount', 'Invalid wire amount.') from exc


def parse_di_tag(value: Any) -> Tuple[str, str]:
    """{3100}/{3400}: 9-digit ABA followed by a short name."""
    text = str(value or '').strip()
    if not text:
        raise InWireError('invalid_aba', 'Depository institution tag is required.')
    try:
        aba = normalize_aba(text[:9])
    except WireError as exc:
        raise InWireError('invalid_aba', exc.message) from exc
    name = text[9:].strip()
    return aba, name


def parse_party_tag(value: Any) -> Tuple[str, str]:
    """{4200}/{5000}: NAME*STREET*CITY*STATE*ZIP"""
    text = str(value or '').strip()
    parts = [part.strip() for part in text.split('*')]
    name = parts[0] if parts else ''
    address = ' '.join(part for part in parts[1:] if part)
    return name, address


def normalize_type_code(value: Any, *, default: str = TYPE_CREDIT) -> str:
    text = str(value or default).strip().lower()
    mapped = TYPE_ALIASES.get(text, text)
    if mapped not in {TYPE_CREDIT, TYPE_RETURN}:
        raise InWireError('invalid_type', 'Type must be 10 (credit) or 16 (return).')
    return mapped


def normalize_return_reason(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RETURN_ALIASES.get(text, text)
    if text not in RETURN_REASONS:
        raise InWireError('invalid_reason', 'Unknown inbound return reason.')
    return text


def message_from_faim(text: Any) -> Dict[str, Any]:
    """Reusable FAIM → inbound field map."""
    fields = parse_faim(text)
    if '1100' not in fields:
        raise InWireError('invalid_faim', 'FAIM message is missing {1100} IMAD.')
    amount = parse_amount_tag(fields.get('2000'))
    sender_aba, sender_name = parse_di_tag(fields.get('3100') or '')
    receiver_aba, receiver_name = parse_di_tag(fields.get('3400') or '')
    beneficiary_account = fields.get('3600') or ''
    originator_account = fields.get('3700') or ''
    beneficiary_name, _bene_addr = parse_party_tag(fields.get('4200') or '')
    originator_name, _orig_addr = parse_party_tag(fields.get('5000') or sender_name)
    return {
        'imad': normalize_imad(fields['1100']),
        'omad': normalize_imad(fields['1110']) if fields.get('1110') else '',
        'type_code': normalize_type_code(fields.get('1500') or TYPE_CREDIT),
        'amount': money_str(amount),
        'sender_aba': sender_aba,
        'sender_name': sender_name,
        'receiver_aba': receiver_aba,
        'receiver_name': receiver_name,
        'beneficiary_account': beneficiary_account,
        'originator_account': originator_account,
        'beneficiary_name': beneficiary_name or 'BENEFICIARY',
        'originator_name': originator_name or sender_name or 'ORIGINATOR',
        'memo': normalize_note(fields.get('6000') or '', limit=140),
        'raw': compose_faim(fields),
    }


def message_from_values(values: Dict[str, Any]) -> Dict[str, Any]:
    """JSON operator payload → inbound field map (same shape as FAIM)."""
    if values.get('file') or values.get('faim') or values.get('raw'):
        return message_from_faim(values.get('file') or values.get('faim') or values.get('raw'))
    imad = values.get('imad')
    if not imad:
        raise InWireError('invalid_imad', 'IMAD is required.')
    amount = parse_amount_tag(values.get('amount'))
    try:
        sender_aba = normalize_aba(values.get('sender_aba') or values.get('sender'))
        receiver_aba = normalize_aba(values.get('receiver_aba') or values.get('receiver'))
        account = normalize_external_account(values.get('beneficiary_account') or values.get('account'))
    except WireError as exc:
        raise InWireError(exc.code, exc.message) from exc
    originator = str(values.get('originator_name') or values.get('originator') or '').strip()
    beneficiary = str(values.get('beneficiary_name') or values.get('beneficiary') or '').strip()
    return {
        'imad': normalize_imad(imad),
        'omad': normalize_imad(values['omad']) if values.get('omad') else '',
        'type_code': normalize_type_code(values.get('type_code') or values.get('type') or TYPE_CREDIT),
        'amount': money_str(amount),
        'sender_aba': sender_aba,
        'sender_name': str(values.get('sender_name') or '').strip(),
        'receiver_aba': receiver_aba,
        'receiver_name': str(values.get('receiver_name') or '').strip(),
        'beneficiary_account': account,
        'originator_account': str(values.get('originator_account') or '').strip(),
        'beneficiary_name': beneficiary or 'BENEFICIARY',
        'originator_name': originator or 'ORIGINATOR',
        'memo': normalize_note(values.get('memo') or values.get('obi') or '', limit=140),
        'raw': '',
    }


def compose_return_faim(row: 'InboundWire', *, return_imad: str, reason: str, receiver_aba: str, source: str) -> str:
    """Type-16 return of an inbound credit. Reuses IMAD compose + FAIM map."""
    amount = parse_money(row.amount)
    return compose_faim({
        '1100': return_imad,
        '1110': compose_omad(return_imad[:8], int(return_imad[16:] or 1), frb='FRBNY001'),
        '1500': TYPE_RETURN,
        '2000': compose_amount_tag(amount),
        '3100': receiver_aba + source,
        '3400': row.sender_aba + (row.originator_name[:18] if row.originator_name else 'SENDER'),
        '3600': row.originator_account_last4,
        '4200': row.originator_name,
        '5000': row.beneficiary_name,
        '6000': 'RET %s %s' % (reason.upper(), row.imad[:16]),
    })


@dataclass
class InWirePolicy:
    enabled: bool = True
    customer_view: bool = True
    customer_return: bool = True
    allow_credit: bool = False
    max_inbounds: int = 240
    min_amount: Decimal = Decimal('0.01')
    max_amount: Decimal = Decimal('10000000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    cutoff_hour: int = 17
    tz_offset_hours: int = -4
    source_id: str = 'KONOHA01'
    receiver_aba: str = DEFAULT_RECEIVER_ABA
    watchlist: Tuple[str, ...] = (
        'BLOCKED PERSON',
        'SANCTIONED ENTITY',
        'OFAC TESTNAME',
    )
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'InWirePolicy':
        extra = _env_list('INWIRE_OFAC_LIST')
        watch = tuple(dict.fromkeys(cls.watchlist + extra))
        receiver = os.environ.get('INWIRE_RECEIVER_ABA') or DEFAULT_RECEIVER_ABA
        try:
            receiver = normalize_aba(receiver)
        except WireError:
            receiver = DEFAULT_RECEIVER_ABA
        return cls(
            enabled=_env_bool('INWIRE_ENABLED', True),
            customer_view=_env_bool('INWIRE_CUSTOMER_VIEW', True),
            customer_return=_env_bool('INWIRE_CUSTOMER_RETURN', True),
            allow_credit=_env_bool('INWIRE_ALLOW_CREDIT', False),
            max_inbounds=max(1, _env_int('INWIRE_MAX', 240)),
            min_amount=_env_money('INWIRE_MIN_AMOUNT', '0.01'),
            max_amount=_env_money('INWIRE_MAX_AMOUNT', '10000000.00'),
            dual_control_threshold=_env_money('INWIRE_DUAL_CONTROL', '10000.00'),
            cutoff_hour=max(0, min(23, _env_int('INWIRE_CUTOFF_HOUR', 17))),
            tz_offset_hours=_env_int('INWIRE_TZ_OFFSET', -4),
            source_id=normalize_source(os.environ.get('INWIRE_SOURCE', 'KONOHA01')),
            receiver_aba=receiver,
            watchlist=watch,
            extra_holidays=_env_list('INWIRE_HOLIDAYS'),
        )


@dataclass
class InboundWire:
    inbound_id: str
    imad: str
    omad: str
    userid: str
    internal_account: str
    amount: str
    sender_aba: str
    receiver_aba: str
    originator_name: str
    originator_account_last4: str
    beneficiary_name: str
    beneficiary_account: str
    type_code: str
    purpose: str
    memo: str
    status: str
    value_date: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    return_imad: str
    return_reason: str
    created_at: float
    updated_at: float
    posted_at: float = 0.0
    returned_at: float = 0.0
    note: str = ''
    batch_id: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'inbound_id': self.inbound_id,
            'imad': self.imad,
            'omad': self.omad,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'amount': self.amount,
            'sender_aba': self.sender_aba,
            'receiver_aba': self.receiver_aba,
            'originator_name': self.originator_name,
            'originator_last4': self.originator_account_last4,
            'beneficiary_name': self.beneficiary_name,
            'beneficiary_last4': last4(self.beneficiary_account),
            'type_code': self.type_code,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'value_date': self.value_date,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'return_imad': self.return_imad,
            'return_reason': self.return_reason,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'posted_at': self.posted_at,
            'returned_at': self.returned_at,
            'note': self.note,
            'batch_id': self.batch_id,
            'held': self.status == IN_HELD,
            'unmatched': self.status == IN_UNMATCHED,
            'queued': self.status == IN_QUEUED,
            'pending_release': self.status == IN_PENDING,
            'posted': self.status == IN_POSTED,
            'returned': self.status == IN_RETURNED,
            'returnable': self.status in RETURNABLE_BEFORE_POST or self.status == IN_POSTED,
        }


def _clone(row: InboundWire) -> InboundWire:
    return InboundWire(**{key: getattr(row, key) for key in row.__dataclass_fields__})


def _from_row(row: Any) -> InboundWire:
    return InboundWire(
        inbound_id=row['inbound_id'],
        imad=row['imad'],
        omad=row['omad'] or '',
        userid=row['userid'] or '',
        internal_account=row['internal_account'] or '',
        amount=row['amount'],
        sender_aba=row['sender_aba'],
        receiver_aba=row['receiver_aba'],
        originator_name=row['originator_name'],
        originator_account_last4=row['originator_account_last4'] or '',
        beneficiary_name=row['beneficiary_name'],
        beneficiary_account=row['beneficiary_account'],
        type_code=row['type_code'],
        purpose=row['purpose'] or 'other',
        memo=row['memo'] or '',
        status=row['status'],
        value_date=row['value_date'],
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        return_imad=row['return_imad'] or '',
        return_reason=row['return_reason'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        posted_at=float(row['posted_at'] or 0),
        returned_at=float(row['returned_at'] or 0),
        note=row['note'] or '',
        batch_id=row['batch_id'] or '',
    )


class MemoryInWireStore:
    def __init__(self) -> None:
        self._rows: Dict[str, InboundWire] = {}
        self._by_imad: Dict[str, str] = {}
        self._seq = 0
        self._lock = threading.Lock()

    def put(self, row: InboundWire) -> None:
        with self._lock:
            self._rows[row.inbound_id] = _clone(row)
            self._by_imad[row.imad] = row.inbound_id

    def update(self, row: InboundWire) -> None:
        with self._lock:
            if row.inbound_id not in self._rows:
                raise InWireError('inbound_not_found', 'Inbound wire not found.')
            self._rows[row.inbound_id] = _clone(row)
            self._by_imad[row.imad] = row.inbound_id

    def get(self, inbound_id: str) -> Optional[InboundWire]:
        with self._lock:
            row = self._rows.get(inbound_id)
            return _clone(row) if row is not None else None

    def get_by_imad(self, imad: str) -> Optional[InboundWire]:
        with self._lock:
            inbound_id = self._by_imad.get(imad)
            row = self._rows.get(inbound_id) if inbound_id else None
            return _clone(row) if row is not None else None

    def list_for(self, userid: str) -> List[InboundWire]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.userid == userid]
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def list_unmatched(self) -> List[InboundWire]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.status == IN_UNMATCHED]
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def list_open(self) -> List[InboundWire]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.status in OPEN_INBOUNDS]
            rows.sort(key=lambda item: item.created_at)
            return [_clone(row) for row in rows]

    def list_all(self) -> List[InboundWire]:
        with self._lock:
            rows = list(self._rows.values())
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def next_sequence(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq


class SqliteInWireStore:
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
                CREATE TABLE IF NOT EXISTS inbounds (
                    inbound_id TEXT PRIMARY KEY,
                    imad TEXT NOT NULL UNIQUE,
                    omad TEXT NOT NULL DEFAULT '',
                    userid TEXT NOT NULL DEFAULT '',
                    internal_account TEXT NOT NULL DEFAULT '',
                    amount TEXT NOT NULL,
                    sender_aba TEXT NOT NULL,
                    receiver_aba TEXT NOT NULL,
                    originator_name TEXT NOT NULL,
                    originator_account_last4 TEXT NOT NULL DEFAULT '',
                    beneficiary_name TEXT NOT NULL,
                    beneficiary_account TEXT NOT NULL,
                    type_code TEXT NOT NULL,
                    purpose TEXT NOT NULL DEFAULT 'other',
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    value_date TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    return_imad TEXT NOT NULL DEFAULT '',
                    return_reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    posted_at REAL NOT NULL DEFAULT 0,
                    returned_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    batch_id TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _write(self, conn: sqlite3.Connection, row: InboundWire) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO inbounds (
                inbound_id, imad, omad, userid, internal_account, amount,
                sender_aba, receiver_aba, originator_name, originator_account_last4,
                beneficiary_name, beneficiary_account, type_code, purpose, memo,
                status, value_date, actor, releaser, ofac_hit, ofac_match,
                return_imad, return_reason, created_at, updated_at, posted_at,
                returned_at, note, batch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.inbound_id, row.imad, row.omad, row.userid, row.internal_account,
                row.amount, row.sender_aba, row.receiver_aba, row.originator_name,
                row.originator_account_last4, row.beneficiary_name, row.beneficiary_account,
                row.type_code, row.purpose, row.memo, row.status, row.value_date,
                row.actor, row.releaser, int(row.ofac_hit), row.ofac_match,
                row.return_imad, row.return_reason, row.created_at, row.updated_at,
                row.posted_at, row.returned_at, row.note, row.batch_id,
            ),
        )

    def put(self, row: InboundWire) -> None:
        with self._lock, self._connect() as conn:
            self._write(conn, row)
            conn.commit()

    def update(self, row: InboundWire) -> None:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT inbound_id FROM inbounds WHERE inbound_id = ?', (row.inbound_id,),
            ).fetchone()
            if existing is None:
                raise InWireError('inbound_not_found', 'Inbound wire not found.')
            self._write(conn, row)
            conn.commit()

    def get(self, inbound_id: str) -> Optional[InboundWire]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def get_by_imad(self, imad: str) -> Optional[InboundWire]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE imad = ?', (imad,)).fetchone()
        return _from_row(row) if row is not None else None

    def list_for(self, userid: str) -> List[InboundWire]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE userid = ? ORDER BY created_at DESC',
                (userid,),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_unmatched(self) -> List[InboundWire]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM inbounds WHERE status = ? ORDER BY created_at DESC",
                (IN_UNMATCHED,),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_open(self) -> List[InboundWire]:
        with self._lock, self._connect() as conn:
            placeholders = ','.join('?' for _ in OPEN_INBOUNDS)
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE status IN (%s) ORDER BY created_at' % placeholders,
                tuple(OPEN_INBOUNDS),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_all(self) -> List[InboundWire]:
        with self._lock, self._connect() as conn:
            rows = conn.execute('SELECT * FROM inbounds ORDER BY created_at DESC').fetchall()
        return [_from_row(row) for row in rows]

    def next_sequence(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'seq'").fetchone()
            current = int(row['value']) if row is not None else 0
            current += 1
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('seq', ?)",
                (str(current),),
            )
            conn.commit()
        return current


class InWireService:
    def __init__(
        self,
        policy: InWirePolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        lookup_fn: Optional[Callable[[str], Optional[str]]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[WireCalendar] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.lookup_fn = lookup_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or WireCalendar(
            cutoff_hour=policy.cutoff_hour,
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise InWireError('inwire_disabled', 'Inbound wires are disabled.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise InWireError('inwire_forbidden', 'Staff only.')

    def _require_view(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_view:
            raise InWireError('inwire_forbidden', 'Customers cannot view inbound wires.')

    def _require_customer_return(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_return:
            raise InWireError('inwire_forbidden', 'Customers cannot request inbound returns.')

    def _owned_accounts(self, userid: str) -> List[str]:
        if self.accounts_fn is None:
            return []
        return own_accounts_from_customer_payload(self.accounts_fn(userid))

    def _account_types(self, userid: str) -> Dict[str, str]:
        if self.accounts_fn is None:
            return {}
        return account_types_from_customer_payload(self.accounts_fn(userid))

    def _lookup(self, account: str) -> Optional[str]:
        if self.lookup_fn is None:
            return None
        found = self.lookup_fn(account)
        if found in (None, '', -1, 0):
            return None
        return str(found)

    def _assert_internal_account(self, userid: str, account: str) -> None:
        owned = self._owned_accounts(userid)
        if owned and account not in owned:
            raise InWireError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise InWireError('credit_not_allowed', 'Credit accounts cannot receive inbound wires.')

    def _assert_amount(self, dollars: Decimal) -> None:
        if dollars < self.policy.min_amount or dollars > self.policy.max_amount:
            raise InWireError('amount_out_of_range', 'Amount is outside the allowed range.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def _assert_receiver(self, receiver_aba: str) -> None:
        if receiver_aba != self.policy.receiver_aba:
            raise InWireError('wrong_receiver', 'Message is not addressed to this bank.')

    def get_inbound(self, *, inbound_id: str, actor: str, actor_type: str) -> InboundWire:
        self._require_view(actor_type)
        row = self.store.get(inbound_id)
        if row is None:
            raise InWireError('inbound_not_found', 'Inbound wire not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise InWireError('inwire_forbidden', 'Not allowed to view this inbound wire.')
        return row

    def preview_message(self, values: Dict[str, Any]) -> Dict[str, Any]:
        self._require_enabled()
        message = message_from_values(values)
        dollars = parse_money(message['amount'])
        self._assert_amount(dollars)
        self._assert_receiver(message['receiver_aba'])
        ofac = self._screen(message['originator_name'])
        userid = self._lookup(message['beneficiary_account'])
        now = float(self.clock())
        return {
            'message': {
                'imad': message['imad'],
                'omad': message['omad'],
                'amount': message['amount'],
                'sender_aba': message['sender_aba'],
                'receiver_aba': message['receiver_aba'],
                'originator_name': message['originator_name'],
                'beneficiary_name': message['beneficiary_name'],
                'beneficiary_last4': last4(message['beneficiary_account']),
                'type_code': message['type_code'],
            },
            'matched_userid': userid or '',
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(dollars),
            'clock': self.calendar.snapshot(now),
        }

    def _evaluate_status(self, *, userid: str, account: str, dollars: Decimal, ofac: ScreenResult) -> str:
        if not userid or not account:
            return IN_UNMATCHED
        if ofac.hit:
            return IN_HELD
        if self._needs_dual_control(dollars):
            return IN_PENDING
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        if clock['after_cutoff'] or not clock['business_day']:
            return IN_QUEUED
        return IN_POSTED

    def _credit(self, row: InboundWire) -> str:
        if self.credit_fn is None:
            return 'ok'
        remark = 'inwire from %s' % (row.originator_name[:20] or 'originator')
        result = self.credit_fn(row.internal_account, row.amount, remark)
        return _classify_money_result(result)

    def _debit(self, row: InboundWire) -> str:
        if self.debit_fn is None:
            return 'ok'
        remark = 'inwire return %s' % (row.imad[:12])
        result = self.debit_fn(row.internal_account, row.amount, remark)
        return _classify_money_result(result)

    def _try_post(self, row: InboundWire) -> InboundWire:
        classified = self._credit(row)
        now = float(self.clock())
        if classified == 'ok':
            row.status = IN_POSTED
            row.posted_at = now
            row.updated_at = now
            if not row.omad:
                seq = self.store.next_sequence()
                row.omad = compose_omad(row.value_date, seq)
            self.store.update(row)
            return row
        row.status = IN_FAILED
        row.note = 'credit_failed'
        row.updated_at = now
        self.store.update(row)
        raise InWireError('failed', 'Inbound credit failed.', inbound=row)

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        values: Dict[str, Any],
        batch_id: str = '',
    ) -> Tuple[InboundWire, bool]:
        self._require_staff(actor_type)
        message = message_from_values(values)
        dollars = parse_money(message['amount'])
        self._assert_amount(dollars)
        self._assert_receiver(message['receiver_aba'])
        existing = self.store.get_by_imad(message['imad'])
        if existing is not None:
            return existing, False
        if len(self.store.list_all()) >= self.policy.max_inbounds:
            raise InWireError('inbound_limit', 'Inbound wire limit reached.')
        try:
            account = normalize_account(message['beneficiary_account'])
        except AccountError:
            account = ''
        userid = self._lookup(account) if account else None
        credit_blocked = False
        if userid and account:
            types = self._account_types(userid)
            if types.get(account) == 'credit' and not self.policy.allow_credit:
                credit_blocked = True
                userid = None
                account = ''
        ofac = self._screen(message['originator_name'], aliases=(message.get('sender_name') or '',))
        now = float(self.clock())
        status = self._evaluate_status(
            userid=userid or '',
            account=account,
            dollars=dollars,
            ofac=ofac,
        )
        if credit_blocked:
            status = IN_UNMATCHED
        purpose = normalize_purpose(values.get('purpose') or 'other')
        try:
            originator_name = normalize_legal_name(message['originator_name'])
        except WireError:
            originator_name = normalize_party(message['originator_name'])[:80] or 'ORIGINATOR'
        try:
            beneficiary_name = normalize_legal_name(message['beneficiary_name'])
        except WireError:
            beneficiary_name = normalize_party(message['beneficiary_name'])[:80] or 'BENEFICIARY'
        row = InboundWire(
            inbound_id=uuid.uuid4().hex,
            imad=message['imad'],
            omad=message['omad'],
            userid=userid or '',
            internal_account=account,
            amount=money_str(dollars),
            sender_aba=message['sender_aba'],
            receiver_aba=message['receiver_aba'],
            originator_name=originator_name,
            originator_account_last4=last4(message.get('originator_account')),
            beneficiary_name=beneficiary_name,
            beneficiary_account=message['beneficiary_account'],
            type_code=message['type_code'],
            purpose=purpose,
            memo=message['memo'],
            status=status if status != IN_POSTED else IN_QUEUED,
            value_date=self.calendar.cycle_date(now),
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            return_imad='',
            return_reason='',
            created_at=now,
            updated_at=now,
            note='credit_not_allowed' if credit_blocked else '',
            batch_id=batch_id or normalize_id(values.get('batch_id') if values.get('batch_id') else ''),
        )
        if status == IN_POSTED:
            row.status = IN_POSTED
        self.store.put(row)
        if row.status == IN_POSTED:
            return self._try_post(row), True
        return row, True

    def ingest_file(
        self,
        *,
        actor: str,
        actor_type: str,
        text: Any,
        purpose: str = 'other',
    ) -> Dict[str, Any]:
        self._require_staff(actor_type)
        messages = split_faim_file(text)
        if not messages:
            raise InWireError('invalid_faim', 'Operator file has no FAIM messages.')
        batch_id = uuid.uuid4().hex
        accepted = []
        duplicates = []
        errors = []
        for raw in messages:
            try:
                row, created = self.ingest(
                    actor=actor,
                    actor_type=actor_type,
                    values={'file': raw, 'purpose': purpose, 'batch_id': batch_id},
                    batch_id=batch_id,
                )
                payload = row.to_dict()
                if created:
                    accepted.append(payload)
                else:
                    duplicates.append(payload)
            except InWireError as exc:
                errors.append({'error': exc.code, 'message': exc.message})
        return {
            'batch_id': batch_id,
            'accepted': accepted,
            'duplicates': duplicates,
            'errors': errors,
            'accepted_count': len(accepted),
            'duplicate_count': len(duplicates),
            'error_count': len(errors),
        }

    def assign(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        customer_id: Any,
        internal_account: Any = None,
    ) -> InboundWire:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_UNMATCHED:
            raise InWireError('not_assignable', 'Only unmatched inbound wires can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise InWireError('missing_customer_id', 'Customer id is required.')
        account = normalize_account(internal_account or row.beneficiary_account)
        self._assert_internal_account(owner, account)
        row.userid = owner
        row.internal_account = account
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        ofac = ScreenResult(bool(row.ofac_hit), row.ofac_match, 100 if row.ofac_hit else 0)
        dollars = parse_money(row.amount)
        status = self._evaluate_status(userid=owner, account=account, dollars=dollars, ofac=ofac)
        row.status = status if status != IN_POSTED else IN_QUEUED
        if status == IN_POSTED:
            row.status = IN_POSTED
            self.store.update(row)
            return self._try_post(row)
        self.store.update(row)
        return row

    def override_ofac(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> InboundWire:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_HELD:
            raise InWireError('not_overridable', 'Only OFAC-held inbound wires can be overridden.')
        row.ofac_hit = 0
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        dollars = parse_money(row.amount)
        status = self._evaluate_status(
            userid=row.userid,
            account=row.internal_account,
            dollars=dollars,
            ofac=ScreenResult(False, '', 0),
        )
        row.status = status if status != IN_POSTED else IN_QUEUED
        if status == IN_POSTED:
            row.status = IN_POSTED
            self.store.update(row)
            return self._try_post(row)
        self.store.update(row)
        return row

    def release(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
    ) -> InboundWire:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_PENDING:
            raise InWireError('not_releasable', 'Inbound wire is not waiting for dual-control.')
        if row.actor and row.actor == str(actor):
            raise InWireError('same_approver', 'A different employee must release this inbound wire.')
        row.releaser = str(actor)
        row.updated_at = float(self.clock())
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        if clock['after_cutoff'] or not clock['business_day']:
            row.status = IN_QUEUED
            self.store.update(row)
            return row
        row.status = IN_POSTED
        self.store.update(row)
        return self._try_post(row)

    def reject(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> InboundWire:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status not in OPEN_INBOUNDS:
            raise InWireError('not_rejectable', 'Inbound wire cannot be rejected.')
        row.status = IN_REJECTED
        row.return_reason = normalize_return_reason(reason)
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update(row)
        return row

    def return_inbound(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> InboundWire:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        return self._return(row, actor=actor, reason=reason, note=note, force_window=True)

    def request_return(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'cust',
        note: Any = '',
    ) -> InboundWire:
        self._require_customer_return(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise InWireError('inwire_forbidden', 'Not allowed to return this inbound wire.')
        return self._return(row, actor=actor, reason=reason or 'cust', note=note, force_window=False)

    def _return(
        self,
        row: InboundWire,
        *,
        actor: str,
        reason: Any,
        note: Any,
        force_window: bool,
    ) -> InboundWire:
        if row.status in {IN_RETURNED, IN_REJECTED}:
            raise InWireError('already_returned', 'Inbound wire is already returned or rejected.')
        code = normalize_return_reason(reason, default='cust')
        now = float(self.clock())
        if row.status in RETURNABLE_BEFORE_POST:
            row.status = IN_RETURNED
            row.return_reason = code
            row.note = normalize_note(note) or row.note
            row.actor = str(actor)
            row.returned_at = now
            row.updated_at = now
            seq = self.store.next_sequence()
            row.return_imad = compose_imad(self.calendar.cycle_date(now), self.policy.source_id, seq)
            self.store.update(row)
            return row
        if row.status != IN_POSTED:
            raise InWireError('not_returnable', 'Inbound wire cannot be returned.')
        posted_cycle = str(row.value_date or '')
        current_cycle = self.calendar.cycle_date(now)
        if not force_window and posted_cycle != current_cycle:
            raise InWireError('return_window_closed', 'Same-day return window has closed.')
        classified = self._debit(row)
        if classified == 'nsf':
            raise InWireError('nsf', 'Insufficient funds to return this inbound wire.', inbound=row)
        if classified != 'ok':
            raise InWireError('return_failed', 'Inbound return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.return_reason = code
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.returned_at = now
        row.updated_at = now
        seq = self.store.next_sequence()
        row.return_imad = compose_imad(self.calendar.cycle_date(now), self.policy.source_id, seq)
        self.store.update(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[InboundWire]:
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        if clock['after_cutoff'] or not clock['business_day']:
            return []
        posted = []
        for row in self.store.list_open():
            if row.status != IN_QUEUED:
                continue
            if userid is not None and row.userid != userid:
                continue
            if not row.userid or not row.internal_account:
                continue
            try:
                posted.append(self._try_post(row))
            except InWireError:
                continue
        return posted

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self._require_view(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor and actor != userid:
            raise InWireError('inwire_forbidden', 'Not allowed to view this inbound book.')
        self.run_due(userid)
        rows = self.store.list_for(userid)
        posted_ytd = Decimal('0.00')
        returned_ytd = Decimal('0.00')
        for row in rows:
            amount = parse_money(row.amount, allow_zero=True)
            if row.status == IN_POSTED:
                posted_ytd += amount
            elif row.status == IN_RETURNED:
                returned_ytd += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'receiver_aba': self.policy.receiver_aba,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clock': self.calendar.snapshot(now),
            'inbounds': [row.to_dict() for row in rows[:40]],
            'ytd_posted': money_str(posted_ytd),
            'ytd_returned': money_str(returned_ytd),
            'open_count': sum(1 for row in rows if row.status in OPEN_INBOUNDS),
            'posted_count': sum(1 for row in rows if row.status == IN_POSTED),
        }

    def unmatched_snapshot(self) -> Dict[str, Any]:
        rows = self.store.list_unmatched()
        return {
            'enabled': self.policy.enabled,
            'receiver_aba': self.policy.receiver_aba,
            'unmatched': [row.to_dict() for row in rows[:40]],
            'unmatched_count': len(rows),
        }


_SERVICE: Optional[InWireService] = None


def set_service(service: Optional[InWireService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[InWireService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('INWIRE_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryInWireStore()
    path = os.environ.get('INWIRE_DB', DEFAULT_STORE_PATH)
    return SqliteInWireStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    lookup_fn: Optional[Callable[[str], Optional[str]]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[WireCalendar] = None,
) -> InWireService:
    if store is None:
        store = default_store()
    return InWireService(
        InWirePolicy.from_env(),
        store,
        clock=clock,
        debit_fn=debit_fn,
        credit_fn=credit_fn,
        accounts_fn=accounts_fn,
        lookup_fn=lookup_fn,
        screen_fn=screen_fn,
        calendar=calendar,
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


def _error_status(code: str) -> int:
    return {
        'already_returned': 409,
        'inbound_limit': 409,
        'nsf': 409,
        'failed': 409,
        'return_failed': 409,
        'inwire_forbidden': 403,
        'inwire_disabled': 403,
        'credit_not_allowed': 403,
        'same_approver': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_returnable': 403,
        'return_window_closed': 403,
        'inbound_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_aba': 400,
        'invalid_imad': 400,
        'invalid_faim': 400,
        'invalid_type': 400,
        'invalid_reason': 400,
        'invalid_purpose': 400,
        'wrong_receiver': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: InWireError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('inbound') is not None:
        body['inbound'] = exc.extra['inbound'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except WireError as exc:
        return jsonify({'message': str(exc), 'error': exc.code}), _error_status(exc.code)
    except InWireError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: InWireService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'InWires': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_unmatched(service: InWireService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'inwire_forbidden'}), 403
    return jsonify({'InWires': service.unmatched_snapshot()}), 200


def handle_preview(service: InWireService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'inwire_forbidden'}), 403
    values = request.get_json(silent=True) or {}

    def _run():
        preview = service.preview_message(values)
        return jsonify({'preview': preview}), 200

    return _handle_errors(_run)


def handle_ingest(service: InWireService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'Inbound wire ingested' if created else 'Inbound wire already posted',
            'inbound': row.to_dict(),
            'InWires': service.snapshot(owner, actor=userid, actor_type=actor_type) if row.userid else service.unmatched_snapshot(),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_file(service: InWireService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    text = values.get('file') or values.get('faim') or values.get('text')
    if not text:
        return jsonify({'message': 'Operator file is required', 'error': 'missing_file'}), 400

    def _run():
        result = service.ingest_file(
            actor=userid,
            actor_type=actor_type,
            text=text,
            purpose=values.get('purpose') or 'other',
        )
        result['Unmatched'] = service.unmatched_snapshot()
        return jsonify(result), 201 if result['accepted_count'] else 200

    return _handle_errors(_run)


def handle_assign(service: InWireService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    inbound_id = str(values.get('inbound_id') or '').strip()
    if not inbound_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inbound'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.assign(
            inbound_id=inbound_id,
            actor=userid,
            actor_type=actor_type,
            customer_id=values.get('customer_id') or values.get('owner'),
            internal_account=values.get('account') or values.get('internal_account'),
        )
        return jsonify({
            'message': 'Inbound wire assigned',
            'inbound': row.to_dict(),
            'InWires': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_action(service: InWireService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    inbound_id = str(values.get('inbound_id') or '').strip()
    if not inbound_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inbound'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'override':
            row = service.override_ofac(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'release':
            row = service.release(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            message = 'Inbound wire released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Inbound wire rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Inbound wire returned'
        else:
            raise InWireError('invalid_reason', 'Unknown inbound action.')
        owner = row.userid or userid
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'InWires': service.snapshot(owner, actor=userid, actor_type=actor_type) if row.userid else service.unmatched_snapshot(),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: InWireService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    inbound_id = str(values.get('inbound_id') or '').strip()
    if not inbound_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inbound'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.request_return(
            inbound_id=inbound_id,
            actor=userid,
            actor_type=actor_type,
            reason=values.get('reason') or 'cust',
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Inbound return requested',
            'inbound': row.to_dict(),
            'InWires': service.snapshot(row.userid or userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: InWireService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner if actor_type not in EMPLOYEE_ROLES else None)
    return jsonify({'InWires': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_inwire_routes(app, service: InWireService) -> None:
    @app.route('/listInWires', methods=['POST', 'GET'])
    def list_inwires_route():
        return handle_list(service)

    @app.route('/listUnmatchedInWires', methods=['POST', 'GET'])
    def list_unmatched_inwires_route():
        return handle_unmatched(service)

    @app.route('/previewInWire', methods=['POST', 'GET'])
    def preview_inwire_route():
        return handle_preview(service)

    @app.route('/ingestInWire', methods=['POST', 'GET'])
    def ingest_inwire_route():
        return handle_ingest(service)

    @app.route('/ingestInWireFile', methods=['POST', 'GET'])
    def ingest_inwire_file_route():
        return handle_ingest_file(service)

    @app.route('/assignInWire', methods=['POST', 'GET'])
    def assign_inwire_route():
        return handle_assign(service)

    @app.route('/overrideInWireOfac', methods=['POST', 'GET'])
    def override_inwire_ofac_route():
        return _staff_action(service, 'override')

    @app.route('/releaseInWire', methods=['POST', 'GET'])
    def release_inwire_route():
        return _staff_action(service, 'release')

    @app.route('/rejectInWire', methods=['POST', 'GET'])
    def reject_inwire_route():
        return _staff_action(service, 'reject')

    @app.route('/returnInWire', methods=['POST', 'GET'])
    def return_inwire_route():
        return _staff_action(service, 'return')

    @app.route('/requestInWireReturn', methods=['POST', 'GET'])
    def request_inwire_return_route():
        return handle_request_return(service)

    @app.route('/runDueInWires', methods=['POST', 'GET'])
    def run_due_inwires_route():
        return handle_run_due(service)
