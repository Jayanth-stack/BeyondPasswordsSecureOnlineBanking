"""Lockbox / BAI2 remittance capture.

Commercial customers enroll a lockbox. Staff ingest incoming BAI2 remittance
files of checks mailed to that box and credit the enrolled deposit account.
Independent of inbound Check21 / X9.37 (PR #115), inbound ACH / NACHA
(PR #105), Positive Pay exception decisioning (PR #119), in-app cashier
cheque deposit, ACH linking (PR #68), and domestic Fedwire (PR #73).
Existing `/fundTransfer`, `/withdrawAmount`, `/sendWire`, and
`/depositCheck` stay unchanged.

Foundations (reusable beyond this screen):
- BAI2 Type 01/02/03/16/88/49/98/99 parse and compose (XML/DOCTYPE rejected)
- 10-digit cents amount field
- 7-digit lockbox identifier
- 15-character bank reference (YYYYMMDD + 7-digit seq) with masked snapshots
- Invoice / OCR remittance fingerprint + optional require-invoice gate
- Check serial + remitter OFAC screen (reusable `utility.wire` SDN matcher)
- Fed lockbox availability clock (14:00 ET cutoff via `WireCalendar`)
- Dual-control release at $10k
- Injectable debit/credit + account-directory ownership

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Credit account numbers and raw BAI2 never appear in to_dict / snapshots.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    AccountError,
    AmountError,
    DEFAULT_WATCHLIST,
    DEBIT_NSF,
    DEBIT_OK,
    EMPLOYEE_ROLES,
    ScreenResult,
    WireCalendar,
    WireError,
    account_types_from_customer_payload,
    last4,
    money_str,
    normalize_aba,
    normalize_account,
    normalize_id,
    normalize_legal_name,
    normalize_nickname,
    normalize_note,
    normalize_party,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

BOX_ACTIVE = 'active'
BOX_PAUSED = 'paused'
BOX_ARCHIVED = 'archived'
BOX_STATUSES = frozenset({BOX_ACTIVE, BOX_PAUSED, BOX_ARCHIVED})
OPEN_BOX = frozenset({BOX_ACTIVE, BOX_PAUSED})

INV_OPEN = 'open'
INV_APPLIED = 'applied'
INV_CANCELLED = 'cancelled'
INV_STATUSES = frozenset({INV_OPEN, INV_APPLIED, INV_CANCELLED})

ITEM_UNMATCHED = 'unmatched'
ITEM_HELD = 'held'
ITEM_QUEUED = 'queued'
ITEM_PENDING = 'pending_release'
ITEM_POSTED = 'posted'
ITEM_REJECTED = 'rejected'
ITEM_RETURNED = 'returned'
ITEM_FAILED = 'failed'
ITEM_STATUSES = frozenset({
    ITEM_UNMATCHED, ITEM_HELD, ITEM_QUEUED, ITEM_PENDING,
    ITEM_POSTED, ITEM_REJECTED, ITEM_RETURNED, ITEM_FAILED,
})
OPEN_ITEMS = frozenset({ITEM_UNMATCHED, ITEM_HELD, ITEM_QUEUED, ITEM_PENDING, ITEM_POSTED})
PRE_POST = frozenset({ITEM_UNMATCHED, ITEM_HELD, ITEM_QUEUED, ITEM_PENDING})
TYPE_LOCKBOX_TOTAL = '072'
TYPE_LOCKBOX_CREDIT = '174'
TYPE_LOCKBOX_DEBIT = '175'
CREDIT_TYPES = frozenset({TYPE_LOCKBOX_CREDIT, '206'})
DEFAULT_RECEIVER = '021000021'
DEFAULT_SENDER = 'LOCKBOX1'
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/lockbox.sqlite'
XML_MARKERS = ('<?xml', '<!doctype', '<!DOCTYPE', '<bai', '<BAI')


class LockboxError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def compose_amount_field(amount: Decimal) -> str:
    """BAI2 amount: 10-digit cents, no decimal."""
    cents = int((amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN) * 100).to_integral_value())
    if cents < 0 or cents > 99_999_999_99:
        raise LockboxError('invalid_amount', 'Amount is outside the BAI2 field.')
    return '%010d' % cents


def parse_amount_field(value: Any) -> Decimal:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        raise AmountError('invalid_amount')
    return (Decimal(digits) / Decimal('100')).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def normalize_lockbox_id(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        raise LockboxError('invalid_lockbox', 'Lockbox number is required.')
    if len(digits) < 7:
        digits = digits.zfill(7)
    if len(digits) != 7 or digits == '0000000':
        raise LockboxError('invalid_lockbox', 'Lockbox number must be seven digits.')
    return digits


def compose_bank_ref(cycle_date: str, sequence: int) -> str:
    """Bank reference: YYYYMMDD + 7-digit sequence (15 characters)."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise LockboxError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 9_999_999:
        raise LockboxError('invalid_reference', 'Bank reference sequence out of range.')
    return '%s%07d' % (day, seq)


def normalize_bank_ref(value: Any, *, required: bool = False) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        if required:
            raise LockboxError('invalid_reference', 'Bank reference is required.')
        return ''
    if not (8 <= len(text) <= 15):
        raise LockboxError('invalid_reference', 'Bank reference must be 8-15 characters.')
    return text.ljust(15, '0')[:15] if len(text) < 15 and text[:8].isdigit() else text[:15]


def mask_bank_ref(value: Any) -> str:
    text = str(value or '')
    if len(text) < 12:
        return text
    return '%s***%s' % (text[:8], text[-4:])


def normalize_serial(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise LockboxError('invalid_serial', 'Check serial is required.')
        return ''
    if not (1 <= len(digits) <= 10):
        raise LockboxError('invalid_serial', 'Check serial must be 1-10 digits.')
    return digits.lstrip('0') or '0'


def invoice_fingerprint(value: Any) -> str:
    return re.sub(r'[^A-Z0-9]', '', str(value or '').upper())


def normalize_invoice(value: Any, *, required: bool = False) -> str:
    text = ' '.join(str(value or '').strip().upper().split())
    text = re.sub(r'[^A-Z0-9\-]', '', text)
    if not text:
        if required:
            raise LockboxError('invalid_invoice', 'Invoice number is required.')
        return ''
    if not (2 <= len(text) <= 20):
        raise LockboxError('invalid_invoice', 'Invoice number must be 2-20 characters.')
    return text


def normalize_remitter(value: Any) -> str:
    try:
        return normalize_legal_name(value)
    except WireError:
        cleaned = normalize_party(value)
        if not (2 <= len(cleaned) <= 80):
            raise LockboxError('invalid_name', 'Remitter name must be 2-80 characters.')
        return cleaned


def normalize_receiver(value: Any, *, default: str = DEFAULT_RECEIVER) -> str:
    raw = str(value or default).strip()
    try:
        return normalize_aba(raw or default)
    except WireError as exc:
        raise LockboxError('invalid_aba', exc.message) from exc


def normalize_type_code(value: Any, *, default: str = TYPE_LOCKBOX_CREDIT) -> str:
    digits = ''.join(ch for ch in str(value or default) if ch.isdigit())
    if len(digits) == 2:
        digits = '0' + digits
    if len(digits) != 3:
        raise LockboxError('invalid_type', 'BAI2 type code must be three digits.')
    return digits


def _reject_xml(text: str) -> None:
    lowered = text.lstrip().lower()
    if any(marker.lower() in lowered[:400] for marker in XML_MARKERS):
        raise LockboxError('invalid_file', 'XML and DOCTYPE payloads are not accepted.')
    if '<!doctype' in lowered or '<?xml' in lowered:
        raise LockboxError('invalid_file', 'XML and DOCTYPE payloads are not accepted.')


def split_bai2_records(text: Any) -> List[str]:
    raw = str(text or '')
    _reject_xml(raw)
    if not raw.strip():
        raise LockboxError('invalid_file', 'BAI2 file is empty.')
    chunks = []
    buf = []
    for ch in raw.replace('\r\n', '\n').replace('\r', '\n'):
        if ch == '/':
            record = ''.join(buf).strip()
            buf = []
            if record:
                chunks.append(record)
            continue
        buf.append(ch)
    tail = ''.join(buf).strip()
    if tail:
        chunks.append(tail)
    if not chunks:
        raise LockboxError('invalid_file', 'BAI2 file has no records.')
    return chunks


def _fields(record: str) -> List[str]:
    return [part.strip() for part in record.split(',')]


def parse_bai2(text: Any) -> Dict[str, Any]:
    """Parse one BAI2 lockbox file into header + Type 16 credit items."""
    records = split_bai2_records(text)
    sender = DEFAULT_SENDER
    receiver = DEFAULT_RECEIVER
    as_of = ''
    file_id = ''
    items: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    account = ''
    for record in records:
        fields = _fields(record)
        if not fields:
            continue
        kind = fields[0]
        if kind == '01':
            sender = (fields[1] if len(fields) > 1 else sender) or sender
            receiver = (fields[2] if len(fields) > 2 else receiver) or receiver
            as_of = fields[3] if len(fields) > 3 else as_of
            file_id = fields[5] if len(fields) > 5 else file_id
        elif kind == '02':
            if len(fields) > 3 and fields[3]:
                as_of = fields[3]
        elif kind == '03':
            account = fields[1] if len(fields) > 1 else ''
        elif kind == '16':
            type_code = normalize_type_code(fields[1] if len(fields) > 1 else TYPE_LOCKBOX_CREDIT)
            if type_code not in CREDIT_TYPES:
                current = None
                continue
            amount = parse_amount_field(fields[2] if len(fields) > 2 else '0')
            bank_ref = fields[4] if len(fields) > 4 else ''
            customer_ref = fields[5] if len(fields) > 5 else ''
            remitter = ','.join(fields[6:]) if len(fields) > 6 else ''
            current = {
                'type_code': type_code,
                'amount': money_str(amount),
                'bank_ref': bank_ref,
                'customer_ref': customer_ref,
                'remitter_name': remitter,
                'serial': '',
                'invoice': customer_ref,
                'lockbox_id': account,
                'account': account,
            }
            items.append(current)
        elif kind == '88' and current is not None:
            extra = ','.join(fields[1:])
            _apply_continuation(current, extra)
    return {
        'sender': sender,
        'receiver': receiver,
        'as_of': as_of,
        'file_id': file_id,
        'items': items,
    }


def _apply_continuation(item: Dict[str, Any], extra: str) -> None:
    text = ' '.join(str(extra or '').split())
    if not text:
        return
    serial_match = re.search(r'\bSERIAL\s+([0-9]{1,10})\b', text, re.I)
    if serial_match:
        item['serial'] = serial_match.group(1)
    invoice_match = re.search(r'\bINVOICE\s+([A-Z0-9\-]{2,20})\b', text, re.I)
    if invoice_match:
        item['invoice'] = invoice_match.group(1).upper()
    if not item.get('remitter_name'):
        item['remitter_name'] = text


def compose_bai2(
    items: Sequence[Dict[str, Any]],
    *,
    sender: str = DEFAULT_SENDER,
    receiver: str = DEFAULT_RECEIVER,
    as_of: str = '',
    file_id: str = '000001',
    lockbox_id: str = '',
) -> str:
    """Compose a BAI2 lockbox file. Type 03 uses the lockbox number, never the DDA."""
    if not items:
        raise LockboxError('invalid_file', 'Nothing to export.')
    cycle = as_of or datetime.now(timezone.utc).strftime('%y%m%d')
    if len(cycle) == 8 and cycle.isdigit():
        cycle = cycle[2:]
    if not (len(cycle) == 6 and cycle.isdigit()):
        raise LockboxError('invalid_date', 'As-of date must be YYMMDD or YYYYMMDD.')
    box = lockbox_id or str(items[0].get('lockbox_id') or '')
    try:
        box = normalize_lockbox_id(box)
    except LockboxError:
        box = (box or '0000001')[:7].zfill(7)
    total = Decimal('0.00')
    details: List[str] = []
    for item in items:
        dollars = parse_money(item.get('amount'))
        total += dollars
        bank_ref = str(item.get('bank_ref') or item.get('bank_reference') or '')
        customer_ref = str(item.get('invoice') or item.get('customer_ref') or '')
        remitter = str(item.get('remitter_name') or item.get('remitter') or 'REMITTER')
        details.append('16,%s,%s,V,%s,%s,%s/' % (
            TYPE_LOCKBOX_CREDIT, compose_amount_field(dollars), bank_ref, customer_ref, remitter,
        ))
        serial = str(item.get('serial') or '')
        if serial:
            details.append('88,SERIAL %s/' % serial)
        elif customer_ref:
            details.append('88,INVOICE %s/' % customer_ref)
    amount_field = compose_amount_field(total)
    lines = [
        '01,%s,%s,%s,1000,%s,,,2/' % (sender, receiver, cycle, file_id),
        '02,%s,1,%s,1000,USD,2/' % (sender, cycle),
        '03,%s,USD,%s,%s,%d,V/' % (box, TYPE_LOCKBOX_TOTAL, amount_field, len(items)),
    ]
    lines.extend(details)
    recs_account = 2 + len(details)
    recs_group = recs_account + 2
    recs_file = recs_group + 2
    lines.append('49,%s,%d/' % (amount_field, recs_account))
    lines.append('98,%s,1,%d/' % (amount_field, recs_group))
    lines.append('99,%s,1,%d/' % (amount_field, recs_file))
    return '\n'.join(lines) + '\n'


def split_bai2_file(text: Any) -> List[Dict[str, Any]]:
    """Split a multi-file upload on Type 01 headers and flatten Type 16 credits."""
    records = split_bai2_records(text)
    groups: List[List[str]] = []
    current: List[str] = []
    for record in records:
        if _fields(record)[0] == '01' and current:
            groups.append(current)
            current = [record]
        else:
            current.append(record)
    if current:
        groups.append(current)
    entries: List[Dict[str, Any]] = []
    for group in groups:
        parsed = parse_bai2('/'.join(group) + '/')
        for item in parsed['items']:
            item = dict(item)
            item['sender'] = parsed['sender']
            item['receiver'] = parsed['receiver']
            item['as_of'] = parsed['as_of']
            item['file_id'] = parsed['file_id']
            entries.append(item)
    return entries


def message_from_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    amount = entry.get('amount')
    if amount is None or str(amount).strip() == '':
        raise LockboxError('invalid_amount', 'Amount is required.')
    lockbox = entry.get('lockbox_id') or entry.get('account') or entry.get('lockbox')
    return {
        'lockbox_id': str(lockbox or ''),
        'amount': str(amount),
        'remitter_name': str(entry.get('remitter_name') or entry.get('remitter') or ''),
        'serial': str(entry.get('serial') or ''),
        'invoice': str(entry.get('invoice') or entry.get('customer_ref') or ''),
        'customer_ref': str(entry.get('customer_ref') or entry.get('invoice') or ''),
        'bank_ref': str(entry.get('bank_ref') or entry.get('bank_reference') or entry.get('trace_id') or ''),
        'receiver_aba': str(entry.get('receiver') or entry.get('receiver_aba') or DEFAULT_RECEIVER),
        'type_code': str(entry.get('type_code') or TYPE_LOCKBOX_CREDIT),
        'account': str(entry.get('account') or ''),
    }


def message_from_bai2(text: Any) -> Dict[str, Any]:
    entries = split_bai2_file(text)
    if not entries:
        raise LockboxError('invalid_file', 'Operator file has no lockbox credits.')
    return message_from_entry(entries[0])


def message_from_values(values: Dict[str, Any]) -> Dict[str, Any]:
    if values.get('file') or values.get('bai2') or values.get('text'):
        return message_from_bai2(values.get('file') or values.get('bai2') or values.get('text'))
    return message_from_entry(values)


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
        if any(token in low for token in DEBIT_NSF):
            return 'nsf'
        if low in DEBIT_OK:
            return 'ok'
        return 'failed'
    return 'failed'


def _yy_to_cycle(as_of: str, fallback: str) -> str:
    text = str(as_of or '').strip()
    if len(text) == 8 and text.isdigit():
        return text
    if len(text) == 6 and text.isdigit():
        year = int(text[:2])
        return '%04d%s' % (2000 + year if year < 80 else 1900 + year, text[2:])
    return fallback


@dataclass
class LockboxPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_return: bool = True
    allow_credit: bool = False
    max_enrollments: int = 8
    max_invoices: int = 80
    max_items: int = 400
    min_amount: Decimal = Decimal('0.01')
    max_amount: Decimal = Decimal('1000000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    cutoff_hour: int = 14
    tz_offset_hours: int = -4
    receiver_aba: str = DEFAULT_RECEIVER
    sender_id: str = DEFAULT_SENDER
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'LockboxPolicy':
        extra = _env_list('LOCKBOX_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        receiver = os.environ.get('LOCKBOX_RECEIVER', DEFAULT_RECEIVER)
        try:
            receiver = normalize_aba(receiver)
        except WireError:
            receiver = DEFAULT_RECEIVER
        return cls(
            enabled=_env_bool('LOCKBOX_ENABLED', True),
            customer_manage=_env_bool('LOCKBOX_CUSTOMER_MANAGE', True),
            customer_return=_env_bool('LOCKBOX_CUSTOMER_RETURN', True),
            allow_credit=_env_bool('LOCKBOX_ALLOW_CREDIT', False),
            max_enrollments=max(1, _env_int('LOCKBOX_MAX_ENROLLMENTS', 8)),
            max_invoices=max(1, _env_int('LOCKBOX_MAX_INVOICES', 80)),
            max_items=max(1, _env_int('LOCKBOX_MAX_ITEMS', 400)),
            min_amount=_env_money('LOCKBOX_MIN_AMOUNT', '0.01'),
            max_amount=_env_money('LOCKBOX_MAX_AMOUNT', '1000000.00'),
            dual_control_threshold=_env_money('LOCKBOX_DUAL_CONTROL', '10000.00'),
            cutoff_hour=max(0, min(23, _env_int('LOCKBOX_CUTOFF_HOUR', 14))),
            tz_offset_hours=_env_int('LOCKBOX_TZ_OFFSET', -4),
            receiver_aba=receiver,
            sender_id=str(os.environ.get('LOCKBOX_SENDER', DEFAULT_SENDER) or DEFAULT_SENDER),
            watchlist=watch,
            extra_holidays=_env_list('LOCKBOX_HOLIDAYS'),
        )


@dataclass
class LockboxEnrollment:
    enrollment_id: str
    userid: str
    nickname: str
    lockbox_id: str
    credit_account: str
    require_invoice: int
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'enrollment_id': self.enrollment_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'lockbox_id': self.lockbox_id,
            'account_last4': last4(self.credit_account),
            'require_invoice': bool(self.require_invoice),
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == BOX_ACTIVE,
            'paused': self.status == BOX_PAUSED,
            'archived': self.status == BOX_ARCHIVED,
        }


@dataclass
class LockboxInvoice:
    invoice_id: str
    enrollment_id: str
    userid: str
    invoice_number: str
    amount: str
    remitter_name: str
    status: str
    item_id: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'invoice_id': self.invoice_id,
            'enrollment_id': self.enrollment_id,
            'userid': self.userid,
            'invoice_number': self.invoice_number,
            'amount': self.amount,
            'remitter_name': self.remitter_name,
            'status': self.status,
            'item_id': self.item_id,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'open': self.status == INV_OPEN,
        }


@dataclass
class LockboxItem:
    item_id: str
    bank_ref: str
    enrollment_id: str
    userid: str
    lockbox_id: str
    credit_account: str
    amount: str
    remitter_name: str
    serial: str
    invoice_number: str
    customer_ref: str
    receiver_aba: str
    status: str
    value_date: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    invoice_matched: int
    reason: str
    note: str
    created_at: float
    updated_at: float
    posted_at: float = 0.0
    returned_at: float = 0.0
    batch_id: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'item_id': self.item_id,
            'bank_ref_masked': mask_bank_ref(self.bank_ref),
            'enrollment_id': self.enrollment_id,
            'userid': self.userid,
            'lockbox_id': self.lockbox_id,
            'account_last4': last4(self.credit_account),
            'amount': self.amount,
            'remitter_name': self.remitter_name,
            'serial': self.serial,
            'invoice_number': self.invoice_number,
            'customer_ref': self.customer_ref,
            'receiver_aba': self.receiver_aba,
            'status': self.status,
            'value_date': self.value_date,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'invoice_matched': bool(self.invoice_matched),
            'reason': self.reason,
            'note': self.note,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'posted_at': self.posted_at,
            'returned_at': self.returned_at,
            'unmatched': self.status == ITEM_UNMATCHED,
            'held': self.status == ITEM_HELD,
            'queued': self.status == ITEM_QUEUED,
            'pending_release': self.status == ITEM_PENDING,
            'posted': self.status == ITEM_POSTED,
            'returnable': self.status in PRE_POST or self.status == ITEM_POSTED,
        }


def _clone_enrollment(row: LockboxEnrollment) -> LockboxEnrollment:
    return LockboxEnrollment(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_invoice(row: LockboxInvoice) -> LockboxInvoice:
    return LockboxInvoice(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_item(row: LockboxItem) -> LockboxItem:
    return LockboxItem(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _enrollment_from_row(row: Any) -> LockboxEnrollment:
    return LockboxEnrollment(
        enrollment_id=row['enrollment_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        lockbox_id=row['lockbox_id'],
        credit_account=row['credit_account'],
        require_invoice=int(row['require_invoice'] or 0),
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
    )


def _invoice_from_row(row: Any) -> LockboxInvoice:
    return LockboxInvoice(
        invoice_id=row['invoice_id'],
        enrollment_id=row['enrollment_id'],
        userid=row['userid'],
        invoice_number=row['invoice_number'],
        amount=row['amount'] or '',
        remitter_name=row['remitter_name'] or '',
        status=row['status'],
        item_id=row['item_id'] or '',
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
    )


def _item_from_row(row: Any) -> LockboxItem:
    return LockboxItem(
        item_id=row['item_id'],
        bank_ref=row['bank_ref'],
        enrollment_id=row['enrollment_id'] or '',
        userid=row['userid'] or '',
        lockbox_id=row['lockbox_id'],
        credit_account=row['credit_account'] or '',
        amount=row['amount'],
        remitter_name=row['remitter_name'],
        serial=row['serial'] or '',
        invoice_number=row['invoice_number'] or '',
        customer_ref=row['customer_ref'] or '',
        receiver_aba=row['receiver_aba'],
        status=row['status'],
        value_date=row['value_date'],
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        invoice_matched=int(row['invoice_matched'] or 0),
        reason=row['reason'] or '',
        note=row['note'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        posted_at=float(row['posted_at'] or 0),
        returned_at=float(row['returned_at'] or 0),
        batch_id=row['batch_id'] or '',
    )


class MemoryLockboxStore:
    def __init__(self) -> None:
        self._boxes: Dict[str, LockboxEnrollment] = {}
        self._invoices: Dict[str, LockboxInvoice] = {}
        self._items: Dict[str, LockboxItem] = {}
        self._by_ref: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_enrollment(self, row: LockboxEnrollment) -> None:
        with self._lock:
            self._boxes[row.enrollment_id] = row

    def get_enrollment(self, enrollment_id: str) -> Optional[LockboxEnrollment]:
        with self._lock:
            row = self._boxes.get(enrollment_id)
            return _clone_enrollment(row) if row else None

    def update_enrollment(self, row: LockboxEnrollment) -> None:
        with self._lock:
            self._boxes[row.enrollment_id] = row

    def list_enrollments(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[LockboxEnrollment]:
        with self._lock:
            rows = [_clone_enrollment(row) for row in self._boxes.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != BOX_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_enrollment_by_nickname(self, userid: str, nickname: str) -> Optional[LockboxEnrollment]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._boxes.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_BOX:
                    return _clone_enrollment(row)
        return None

    def find_enrollment_by_lockbox(self, lockbox_id: str, userid: Optional[str] = None) -> Optional[LockboxEnrollment]:
        with self._lock:
            for row in self._boxes.values():
                if row.lockbox_id == lockbox_id and row.status in OPEN_BOX:
                    if userid is not None and row.userid != userid:
                        continue
                    return _clone_enrollment(row)
        return None

    def find_enrollment_by_account(self, account: str) -> Optional[LockboxEnrollment]:
        with self._lock:
            for row in self._boxes.values():
                if row.credit_account == account and row.status in OPEN_BOX:
                    return _clone_enrollment(row)
        return None

    def put_invoice(self, row: LockboxInvoice) -> None:
        with self._lock:
            self._invoices[row.invoice_id] = row

    def get_invoice(self, invoice_id: str) -> Optional[LockboxInvoice]:
        with self._lock:
            row = self._invoices.get(invoice_id)
            return _clone_invoice(row) if row else None

    def update_invoice(self, row: LockboxInvoice) -> None:
        with self._lock:
            self._invoices[row.invoice_id] = row

    def list_invoices(self, userid: Optional[str] = None, enrollment_id: Optional[str] = None) -> List[LockboxInvoice]:
        with self._lock:
            rows = [_clone_invoice(row) for row in self._invoices.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if enrollment_id is not None:
            rows = [row for row in rows if row.enrollment_id == enrollment_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_open_invoice(self, enrollment_id: str, fingerprint: str) -> Optional[LockboxInvoice]:
        wanted = invoice_fingerprint(fingerprint)
        if not wanted:
            return None
        with self._lock:
            for row in self._invoices.values():
                if (
                    row.enrollment_id == enrollment_id
                    and row.status == INV_OPEN
                    and invoice_fingerprint(row.invoice_number) == wanted
                ):
                    return _clone_invoice(row)
        return None

    def put_item(self, row: LockboxItem) -> LockboxItem:
        with self._lock:
            existing_id = self._by_ref.get(row.bank_ref)
            if existing_id is not None:
                return self._items[existing_id]
            self._items[row.item_id] = row
            self._by_ref[row.bank_ref] = row.item_id
            return row

    def update_item(self, row: LockboxItem) -> None:
        with self._lock:
            self._items[row.item_id] = row

    def get_item(self, item_id: str) -> Optional[LockboxItem]:
        with self._lock:
            row = self._items.get(item_id)
            return _clone_item(row) if row else None

    def get_item_by_ref(self, bank_ref: str) -> Optional[LockboxItem]:
        with self._lock:
            item_id = self._by_ref.get(bank_ref)
            return _clone_item(self._items[item_id]) if item_id else None

    def list_items(self, userid: Optional[str] = None, enrollment_id: Optional[str] = None) -> List[LockboxItem]:
        with self._lock:
            rows = [_clone_item(row) for row in self._items.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if enrollment_id is not None:
            rows = [row for row in rows if row.enrollment_id == enrollment_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def list_unmatched(self) -> List[LockboxItem]:
        with self._lock:
            rows = [_clone_item(row) for row in self._items.values() if row.status == ITEM_UNMATCHED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def list_all_items(self) -> List[LockboxItem]:
        with self._lock:
            rows = [_clone_item(row) for row in self._items.values()]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def next_bank_ref_sequence(self, cycle_date: str) -> int:
        with self._lock:
            used = [row.bank_ref for row in self._items.values() if row.bank_ref.startswith(cycle_date)]
        return len(used) + 1


class SqliteLockboxStore:
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
                CREATE TABLE IF NOT EXISTS enrollments (
                    enrollment_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    lockbox_id TEXT NOT NULL,
                    credit_account TEXT NOT NULL,
                    require_invoice INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS invoices (
                    invoice_id TEXT PRIMARY KEY,
                    enrollment_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    invoice_number TEXT NOT NULL,
                    amount TEXT NOT NULL DEFAULT '',
                    remitter_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    item_id TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS items (
                    item_id TEXT PRIMARY KEY,
                    bank_ref TEXT NOT NULL UNIQUE,
                    enrollment_id TEXT NOT NULL DEFAULT '',
                    userid TEXT NOT NULL DEFAULT '',
                    lockbox_id TEXT NOT NULL,
                    credit_account TEXT NOT NULL DEFAULT '',
                    amount TEXT NOT NULL,
                    remitter_name TEXT NOT NULL,
                    serial TEXT NOT NULL DEFAULT '',
                    invoice_number TEXT NOT NULL DEFAULT '',
                    customer_ref TEXT NOT NULL DEFAULT '',
                    receiver_aba TEXT NOT NULL,
                    status TEXT NOT NULL,
                    value_date TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    invoice_matched INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    posted_at REAL NOT NULL DEFAULT 0,
                    returned_at REAL NOT NULL DEFAULT 0,
                    batch_id TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_enrollment(self, row: LockboxEnrollment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO enrollments (
                    enrollment_id, userid, nickname, lockbox_id, credit_account,
                    require_invoice, status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.enrollment_id, row.userid, row.nickname, row.lockbox_id,
                    row.credit_account, row.require_invoice, row.status, row.actor,
                    row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_enrollment(self, enrollment_id: str) -> Optional[LockboxEnrollment]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM enrollments WHERE enrollment_id = ?', (enrollment_id,),
            ).fetchone()
        return _enrollment_from_row(row) if row else None

    def update_enrollment(self, row: LockboxEnrollment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE enrollments SET nickname=?, lockbox_id=?, credit_account=?,
                    require_invoice=?, status=?, actor=?, updated_at=?
                WHERE enrollment_id=?
                """,
                (
                    row.nickname, row.lockbox_id, row.credit_account, row.require_invoice,
                    row.status, row.actor, row.updated_at, row.enrollment_id,
                ),
            )
            conn.commit()

    def list_enrollments(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[LockboxEnrollment]:
        sql = 'SELECT * FROM enrollments'
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
        return [_enrollment_from_row(row) for row in rows]

    def find_enrollment_by_nickname(self, userid: str, nickname: str) -> Optional[LockboxEnrollment]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM enrollments
                WHERE userid = ? AND lower(nickname) = lower(?)
                  AND status IN ('active', 'paused')
                """,
                (userid, nickname),
            ).fetchone()
        return _enrollment_from_row(row) if row else None

    def find_enrollment_by_lockbox(self, lockbox_id: str, userid: Optional[str] = None) -> Optional[LockboxEnrollment]:
        sql = "SELECT * FROM enrollments WHERE lockbox_id = ? AND status IN ('active', 'paused')"
        params: List[Any] = [lockbox_id]
        if userid is not None:
            sql += ' AND userid = ?'
            params.append(userid)
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return _enrollment_from_row(row) if row else None

    def find_enrollment_by_account(self, account: str) -> Optional[LockboxEnrollment]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM enrollments
                WHERE credit_account = ? AND status IN ('active', 'paused')
                """,
                (account,),
            ).fetchone()
        return _enrollment_from_row(row) if row else None

    def put_invoice(self, row: LockboxInvoice) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO invoices (
                    invoice_id, enrollment_id, userid, invoice_number, amount,
                    remitter_name, status, item_id, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.invoice_id, row.enrollment_id, row.userid, row.invoice_number,
                    row.amount, row.remitter_name, row.status, row.item_id, row.actor,
                    row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_invoice(self, invoice_id: str) -> Optional[LockboxInvoice]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM invoices WHERE invoice_id = ?', (invoice_id,)).fetchone()
        return _invoice_from_row(row) if row else None

    def update_invoice(self, row: LockboxInvoice) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE invoices SET invoice_number=?, amount=?, remitter_name=?,
                    status=?, item_id=?, actor=?, updated_at=?
                WHERE invoice_id=?
                """,
                (
                    row.invoice_number, row.amount, row.remitter_name, row.status,
                    row.item_id, row.actor, row.updated_at, row.invoice_id,
                ),
            )
            conn.commit()

    def list_invoices(self, userid: Optional[str] = None, enrollment_id: Optional[str] = None) -> List[LockboxInvoice]:
        sql = 'SELECT * FROM invoices'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if enrollment_id is not None:
            clauses.append('enrollment_id = ?')
            params.append(enrollment_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_invoice_from_row(row) for row in rows]

    def find_open_invoice(self, enrollment_id: str, fingerprint: str) -> Optional[LockboxInvoice]:
        wanted = invoice_fingerprint(fingerprint)
        if not wanted:
            return None
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM invoices WHERE enrollment_id = ? AND status = 'open'",
                (enrollment_id,),
            ).fetchall()
        for row in rows:
            if invoice_fingerprint(row['invoice_number']) == wanted:
                return _invoice_from_row(row)
        return None

    def put_item(self, row: LockboxItem) -> LockboxItem:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM items WHERE bank_ref = ?', (row.bank_ref,),
            ).fetchone()
            if existing is not None:
                return _item_from_row(existing)
            conn.execute(
                """
                INSERT INTO items (
                    item_id, bank_ref, enrollment_id, userid, lockbox_id, credit_account,
                    amount, remitter_name, serial, invoice_number, customer_ref, receiver_aba,
                    status, value_date, actor, releaser, ofac_hit, ofac_match, invoice_matched,
                    reason, note, created_at, updated_at, posted_at, returned_at, batch_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.item_id, row.bank_ref, row.enrollment_id, row.userid, row.lockbox_id,
                    row.credit_account, row.amount, row.remitter_name, row.serial,
                    row.invoice_number, row.customer_ref, row.receiver_aba, row.status,
                    row.value_date, row.actor, row.releaser, row.ofac_hit, row.ofac_match,
                    row.invoice_matched, row.reason, row.note, row.created_at, row.updated_at,
                    row.posted_at, row.returned_at, row.batch_id,
                ),
            )
            conn.commit()
            return row

    def update_item(self, row: LockboxItem) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE items SET enrollment_id=?, userid=?, lockbox_id=?, credit_account=?,
                    remitter_name=?, serial=?, invoice_number=?, customer_ref=?, status=?,
                    value_date=?, actor=?, releaser=?, ofac_hit=?, ofac_match=?,
                    invoice_matched=?, reason=?, note=?, updated_at=?, posted_at=?,
                    returned_at=?, batch_id=?
                WHERE item_id=?
                """,
                (
                    row.enrollment_id, row.userid, row.lockbox_id, row.credit_account,
                    row.remitter_name, row.serial, row.invoice_number, row.customer_ref,
                    row.status, row.value_date, row.actor, row.releaser, row.ofac_hit,
                    row.ofac_match, row.invoice_matched, row.reason, row.note, row.updated_at,
                    row.posted_at, row.returned_at, row.batch_id, row.item_id,
                ),
            )
            conn.commit()

    def get_item(self, item_id: str) -> Optional[LockboxItem]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM items WHERE item_id = ?', (item_id,)).fetchone()
        return _item_from_row(row) if row else None

    def get_item_by_ref(self, bank_ref: str) -> Optional[LockboxItem]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM items WHERE bank_ref = ?', (bank_ref,)).fetchone()
        return _item_from_row(row) if row else None

    def list_items(self, userid: Optional[str] = None, enrollment_id: Optional[str] = None) -> List[LockboxItem]:
        sql = 'SELECT * FROM items'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if enrollment_id is not None:
            clauses.append('enrollment_id = ?')
            params.append(enrollment_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_item_from_row(row) for row in rows]

    def list_unmatched(self) -> List[LockboxItem]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM items WHERE status = 'unmatched' ORDER BY created_at DESC"
            ).fetchall()
        return [_item_from_row(row) for row in rows]

    def list_all_items(self) -> List[LockboxItem]:
        with self._lock, self._connect() as conn:
            rows = conn.execute('SELECT * FROM items ORDER BY created_at DESC').fetchall()
        return [_item_from_row(row) for row in rows]

    def next_bank_ref_sequence(self, cycle_date: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM items WHERE bank_ref LIKE ?",
                (cycle_date + '%',),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class LockboxService:
    def __init__(
        self,
        policy: LockboxPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[WireCalendar] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or WireCalendar(
            cutoff_hour=policy.cutoff_hour,
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise LockboxError('lockbox_disabled', 'Lockbox remittance capture is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise LockboxError('lockbox_forbidden', 'Customers cannot manage lockboxes.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise LockboxError('lockbox_forbidden', 'Staff only.')

    def _require_return(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_return:
            raise LockboxError('lockbox_forbidden', 'Customers cannot request lockbox returns.')

    def _owned_accounts(self, userid: str) -> List[str]:
        if self.accounts_fn is None:
            return []
        return own_accounts_from_customer_payload(self.accounts_fn(userid))

    def _account_types(self, userid: str) -> Dict[str, str]:
        if self.accounts_fn is None:
            return {}
        return account_types_from_customer_payload(self.accounts_fn(userid))

    def _assert_internal_account(self, userid: str, account: str) -> None:
        owned = self._owned_accounts(userid)
        if owned and account not in owned:
            raise LockboxError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise LockboxError('credit_not_allowed', 'Credit accounts cannot receive lockbox remittances.')

    def _assert_amount(self, dollars: Decimal) -> None:
        if dollars < self.policy.min_amount or dollars > self.policy.max_amount:
            raise LockboxError('amount_out_of_range', 'Amount is outside the allowed range.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def _assert_receiver(self, receiver_aba: str) -> None:
        if receiver_aba != self.policy.receiver_aba:
            raise LockboxError('wrong_receiver', 'File is not addressed to this bank.')

    def _today(self, ts: float) -> date:
        return self.calendar.local_dt(ts).date()

    def _cycle(self, ts: float) -> str:
        return self._today(ts).strftime('%Y%m%d')

    def enroll(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        lockbox_id: Any,
        credit_account: Any,
        require_invoice: Any = False,
    ) -> LockboxEnrollment:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise LockboxError('lockbox_forbidden', 'Not allowed to enroll a lockbox for this customer.')
        try:
            name = normalize_nickname(nickname)
        except WireError as exc:
            raise LockboxError('invalid_nickname', exc.message) from exc
        box = normalize_lockbox_id(lockbox_id)
        account = normalize_account(credit_account)
        self._assert_internal_account(owner_userid, account)
        if self.store.find_enrollment_by_nickname(owner_userid, name) is not None:
            raise LockboxError('enrollment_duplicate', 'A lockbox with that nickname already exists.')
        if self.store.find_enrollment_by_lockbox(box) is not None:
            raise LockboxError('enrollment_duplicate', 'That lockbox number is already enrolled.')
        open_rows = [row for row in self.store.list_enrollments(owner_userid) if row.status in OPEN_BOX]
        if len(open_rows) >= self.policy.max_enrollments:
            raise LockboxError('enrollment_limit', 'Lockbox enrollment limit reached.')
        now = float(self.clock())
        row = LockboxEnrollment(
            enrollment_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            lockbox_id=box,
            credit_account=account,
            require_invoice=1 if bool(require_invoice) else 0,
            status=BOX_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_enrollment(row)
        self._rematch_lockbox(box)
        return row

    def get_enrollment(self, *, enrollment_id: str, actor: str, actor_type: str) -> LockboxEnrollment:
        self._require_enabled()
        row = self.store.get_enrollment(enrollment_id)
        if row is None:
            raise LockboxError('enrollment_not_found', 'Lockbox enrollment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise LockboxError('lockbox_forbidden', 'Not allowed to view this lockbox.')
        return row

    def set_enrollment_status(
        self,
        *,
        enrollment_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> LockboxEnrollment:
        self._require_manage(actor_type)
        row = self.get_enrollment(enrollment_id=enrollment_id, actor=actor, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': BOX_PAUSED, 'hold': BOX_PAUSED,
            'resume': BOX_ACTIVE, 'activate': BOX_ACTIVE, 'unpause': BOX_ACTIVE,
            'archive': BOX_ARCHIVED, 'close': BOX_ARCHIVED, 'cancel': BOX_ARCHIVED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {BOX_PAUSED, BOX_ACTIVE, BOX_ARCHIVED}:
            raise LockboxError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == BOX_ARCHIVED:
            raise LockboxError('already_archived', 'Enrollment is already archived.')
        if wanted == BOX_PAUSED:
            if row.status == BOX_PAUSED:
                raise LockboxError('already_paused', 'Enrollment is already paused.')
            if row.status != BOX_ACTIVE:
                raise LockboxError('invalid_status', 'Only an active lockbox can be paused.')
        elif wanted == BOX_ACTIVE:
            if row.status == BOX_ACTIVE:
                raise LockboxError('already_active', 'Enrollment is already active.')
            if row.status != BOX_PAUSED:
                raise LockboxError('invalid_status', 'Only a paused lockbox can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_enrollment(row)
        if wanted == BOX_ACTIVE:
            self._rematch_lockbox(row.lockbox_id)
        return row

    def add_invoice(
        self,
        *,
        enrollment_id: str,
        actor: str,
        actor_type: str,
        invoice_number: Any,
        amount: Any = '',
        remitter_name: Any = '',
    ) -> LockboxInvoice:
        self._require_manage(actor_type)
        box = self.get_enrollment(enrollment_id=enrollment_id, actor=actor, actor_type=actor_type)
        if box.status == BOX_ARCHIVED:
            raise LockboxError('already_archived', 'Cannot add invoices to an archived lockbox.')
        number = normalize_invoice(invoice_number, required=True)
        if self.store.find_open_invoice(box.enrollment_id, number) is not None:
            raise LockboxError('invoice_duplicate', 'That invoice is already on file.')
        open_rows = [row for row in self.store.list_invoices(enrollment_id=box.enrollment_id) if row.status == INV_OPEN]
        if len(open_rows) >= self.policy.max_invoices:
            raise LockboxError('invoice_limit', 'Invoice limit reached.')
        dollars = ''
        if amount not in (None, ''):
            dollars = money_str(parse_money(amount))
        remitter = ''
        if remitter_name not in (None, ''):
            remitter = normalize_remitter(remitter_name)
        now = float(self.clock())
        row = LockboxInvoice(
            invoice_id=uuid.uuid4().hex,
            enrollment_id=box.enrollment_id,
            userid=box.userid,
            invoice_number=number,
            amount=dollars,
            remitter_name=remitter,
            status=INV_OPEN,
            item_id='',
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_invoice(row)
        self._rematch_lockbox(box.lockbox_id)
        return row

    def cancel_invoice(
        self,
        *,
        invoice_id: str,
        actor: str,
        actor_type: str,
    ) -> LockboxInvoice:
        self._require_manage(actor_type)
        row = self.store.get_invoice(invoice_id)
        if row is None:
            raise LockboxError('invoice_not_found', 'Invoice not found.')
        box = self.get_enrollment(enrollment_id=row.enrollment_id, actor=actor, actor_type=actor_type)
        _ = box
        if row.status == INV_CANCELLED:
            raise LockboxError('already_cancelled', 'Invoice is already cancelled.')
        if row.status == INV_APPLIED:
            raise LockboxError('already_applied', 'Applied invoices cannot be cancelled.')
        row.status = INV_CANCELLED
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_invoice(row)
        return row

    def _match_invoice(self, enrollment: LockboxEnrollment, item: LockboxItem) -> Optional[LockboxInvoice]:
        candidates = [item.invoice_number, item.customer_ref]
        for raw in candidates:
            found = self.store.find_open_invoice(enrollment.enrollment_id, raw)
            if found is None:
                continue
            if found.amount and found.amount != item.amount:
                continue
            return found
        return None

    def _evaluate_status(
        self,
        *,
        enrollment: Optional[LockboxEnrollment],
        dollars: Decimal,
        ofac: ScreenResult,
        invoice_matched: bool,
        skip_invoice: bool = False,
    ) -> Tuple[str, str]:
        if enrollment is None:
            return ITEM_UNMATCHED, 'not_enrolled'
        if enrollment.status == BOX_PAUSED:
            return ITEM_UNMATCHED, 'enrollment_paused'
        if enrollment.status != BOX_ACTIVE:
            return ITEM_UNMATCHED, 'not_enrolled'
        if enrollment.require_invoice and not invoice_matched and not skip_invoice:
            return ITEM_UNMATCHED, 'invoice_not_found'
        if ofac.hit:
            return ITEM_HELD, 'ofac'
        if self._needs_dual_control(dollars):
            return ITEM_PENDING, 'dual_control'
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        if clock['after_cutoff'] or not clock['business_day']:
            return ITEM_QUEUED, 'cutoff'
        return ITEM_POSTED, ''

    def _credit(self, row: LockboxItem) -> str:
        if self.credit_fn is None:
            return 'ok'
        remark = 'lockbox %s' % (row.bank_ref[-12:] if row.bank_ref else row.item_id[:12])
        result = self.credit_fn(row.credit_account, row.amount, remark)
        return _classify_money_result(result)

    def _debit(self, row: LockboxItem) -> str:
        if self.debit_fn is None:
            return 'ok'
        remark = 'lockbox return %s' % (row.bank_ref[-12:] if row.bank_ref else row.item_id[:12])
        result = self.debit_fn(row.credit_account, row.amount, remark)
        return _classify_money_result(result)

    def _try_post(self, row: LockboxItem) -> LockboxItem:
        classified = self._credit(row)
        now = float(self.clock())
        if classified == 'ok':
            row.status = ITEM_POSTED
            row.posted_at = now
            row.updated_at = now
            self.store.update_item(row)
            self._apply_invoice(row)
            return row
        row.status = ITEM_FAILED
        row.note = 'credit_failed'
        row.updated_at = now
        self.store.update_item(row)
        raise LockboxError('failed', 'Lockbox credit failed.', item=row)

    def _apply_invoice(self, row: LockboxItem) -> None:
        if not row.enrollment_id or not (row.invoice_number or row.customer_ref):
            return
        enrollment = self.store.get_enrollment(row.enrollment_id)
        if enrollment is None:
            return
        found = self._match_invoice(enrollment, row)
        if found is None:
            return
        found.status = INV_APPLIED
        found.item_id = row.item_id
        found.updated_at = float(self.clock())
        self.store.update_invoice(found)
        row.invoice_matched = 1
        if not row.invoice_number:
            row.invoice_number = found.invoice_number
        self.store.update_item(row)

    def _resolve_enrollment(self, message: Dict[str, Any], owner: Optional[str] = None) -> Optional[LockboxEnrollment]:
        lockbox_raw = str(message.get('lockbox_id') or '')
        account_raw = str(message.get('account') or '')
        box = None
        try:
            if lockbox_raw:
                box = normalize_lockbox_id(lockbox_raw)
        except LockboxError:
            box = None
        if box:
            found = self.store.find_enrollment_by_lockbox(box, owner)
            if found is not None:
                return found
        try:
            account = normalize_account(account_raw or lockbox_raw, required=False)
        except AccountError:
            account = ''
        if account:
            found = self.store.find_enrollment_by_account(account)
            if found is not None and (owner is None or found.userid == owner):
                return found
        return None

    def preview_message(self, values: Dict[str, Any]) -> Dict[str, Any]:
        self._require_enabled()
        message = message_from_values(values)
        dollars = parse_money(message['amount'])
        self._assert_amount(dollars)
        receiver = normalize_receiver(message.get('receiver_aba') or self.policy.receiver_aba)
        self._assert_receiver(receiver)
        remitter = normalize_remitter(message.get('remitter_name') or 'REMITTER')
        ofac = self._screen(remitter)
        enrollment = self._resolve_enrollment(message)
        now = float(self.clock())
        matched = False
        if enrollment is not None:
            probe = LockboxItem(
                item_id='preview', bank_ref='', enrollment_id=enrollment.enrollment_id,
                userid=enrollment.userid, lockbox_id=enrollment.lockbox_id,
                credit_account=enrollment.credit_account, amount=money_str(dollars),
                remitter_name=remitter, serial='', invoice_number=message.get('invoice') or '',
                customer_ref=message.get('customer_ref') or '', receiver_aba=receiver,
                status=ITEM_UNMATCHED, value_date=self.calendar.cycle_date(now),
                actor='', releaser='', ofac_hit=0, ofac_match='', invoice_matched=0,
                reason='', note='', created_at=now, updated_at=now,
            )
            matched = self._match_invoice(enrollment, probe) is not None
        status, reason = self._evaluate_status(
            enrollment=enrollment, dollars=dollars, ofac=ofac, invoice_matched=matched,
        )
        return {
            'lockbox_id': enrollment.lockbox_id if enrollment else (message.get('lockbox_id') or ''),
            'amount': money_str(dollars),
            'remitter_name': remitter,
            'matched_userid': enrollment.userid if enrollment else '',
            'account_last4': last4(enrollment.credit_account) if enrollment else '',
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(dollars),
            'invoice_matched': matched,
            'status': status,
            'reason': reason,
            'clock': self.calendar.snapshot(now),
        }

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        values: Dict[str, Any],
        batch_id: str = '',
        skip_invoice: bool = False,
    ) -> Tuple[LockboxItem, bool]:
        self._require_staff(actor_type)
        message = message_from_values(values)
        dollars = parse_money(message['amount'])
        self._assert_amount(dollars)
        receiver = normalize_receiver(message.get('receiver_aba') or self.policy.receiver_aba)
        self._assert_receiver(receiver)
        remitter = normalize_remitter(message.get('remitter_name') or 'REMITTER')
        serial = normalize_serial(message.get('serial'))
        invoice = ''
        if message.get('invoice') or message.get('customer_ref'):
            try:
                invoice = normalize_invoice(message.get('invoice') or message.get('customer_ref'))
            except LockboxError:
                invoice = str(message.get('invoice') or message.get('customer_ref') or '')[:20]
        now = float(self.clock())
        cycle = _yy_to_cycle(values.get('as_of') or message.get('as_of') or '', self._cycle(now))
        bank_ref = normalize_bank_ref(message.get('bank_ref'))
        if not bank_ref:
            bank_ref = compose_bank_ref(cycle, self.store.next_bank_ref_sequence(cycle))
        existing = self.store.get_item_by_ref(bank_ref)
        if existing is not None:
            return existing, False
        if len(self.store.list_all_items()) >= self.policy.max_items:
            raise LockboxError('item_limit', 'Lockbox item limit reached.')
        owner = str(values.get('customer_id') or values.get('owner') or '').strip() or None
        enrollment = self._resolve_enrollment(message, owner)
        credit_blocked = False
        userid = enrollment.userid if enrollment else ''
        account = enrollment.credit_account if enrollment else ''
        lockbox_id = enrollment.lockbox_id if enrollment else ''
        if not lockbox_id:
            try:
                lockbox_id = normalize_lockbox_id(message.get('lockbox_id'))
            except LockboxError:
                lockbox_id = ''.join(ch for ch in str(message.get('lockbox_id') or '') if ch.isdigit())[:7]
        if enrollment is not None:
            types = self._account_types(enrollment.userid)
            if types.get(enrollment.credit_account) == 'credit' and not self.policy.allow_credit:
                credit_blocked = True
                userid = ''
                account = ''
                enrollment = None
        ofac = self._screen(remitter)
        probe = LockboxItem(
            item_id='probe', bank_ref=bank_ref, enrollment_id=enrollment.enrollment_id if enrollment else '',
            userid=userid, lockbox_id=lockbox_id, credit_account=account, amount=money_str(dollars),
            remitter_name=remitter, serial=serial, invoice_number=invoice,
            customer_ref=str(message.get('customer_ref') or invoice), receiver_aba=receiver,
            status=ITEM_UNMATCHED, value_date=self.calendar.cycle_date(now),
            actor=str(actor), releaser='', ofac_hit=0, ofac_match='', invoice_matched=0,
            reason='', note='', created_at=now, updated_at=now,
        )
        matched_invoice = self._match_invoice(enrollment, probe) if enrollment else None
        status, reason = self._evaluate_status(
            enrollment=enrollment, dollars=dollars, ofac=ofac,
            invoice_matched=matched_invoice is not None, skip_invoice=skip_invoice,
        )
        if credit_blocked:
            status, reason = ITEM_UNMATCHED, 'credit_not_allowed'
        row = LockboxItem(
            item_id=uuid.uuid4().hex,
            bank_ref=bank_ref,
            enrollment_id=enrollment.enrollment_id if enrollment else '',
            userid=userid,
            lockbox_id=lockbox_id,
            credit_account=account,
            amount=money_str(dollars),
            remitter_name=remitter,
            serial=serial,
            invoice_number=invoice,
            customer_ref=str(message.get('customer_ref') or invoice),
            receiver_aba=receiver,
            status=status,
            value_date=self.calendar.cycle_date(now),
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            invoice_matched=1 if matched_invoice is not None else 0,
            reason=reason,
            note='credit_not_allowed' if credit_blocked else '',
            created_at=now,
            updated_at=now,
            batch_id=batch_id or normalize_id(values.get('batch_id') if values.get('batch_id') else ''),
        )
        stored = self.store.put_item(row)
        if stored.item_id != row.item_id:
            return stored, False
        if stored.status == ITEM_POSTED:
            return self._try_post(stored), True
        return stored, True

    def ingest_file(
        self,
        *,
        actor: str,
        actor_type: str,
        text: Any,
        customer_id: Any = '',
    ) -> Dict[str, Any]:
        self._require_staff(actor_type)
        entries = split_bai2_file(text)
        if not entries:
            raise LockboxError('invalid_file', 'Operator file has no lockbox credits.')
        batch_id = uuid.uuid4().hex
        accepted = []
        duplicates = []
        errors = []
        for entry in entries:
            try:
                payload = message_from_entry(entry)
                if customer_id:
                    payload['customer_id'] = customer_id
                payload['as_of'] = entry.get('as_of') or ''
                row, created = self.ingest(
                    actor=actor, actor_type=actor_type, values=payload, batch_id=batch_id,
                )
                snap = row.to_dict()
                if created:
                    accepted.append(snap)
                else:
                    duplicates.append(snap)
            except LockboxError as exc:
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

    def get_item(self, *, item_id: str, actor: str, actor_type: str) -> LockboxItem:
        self._require_enabled()
        row = self.store.get_item(item_id)
        if row is None:
            raise LockboxError('item_not_found', 'Lockbox item not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise LockboxError('lockbox_forbidden', 'Not allowed to view this remittance.')
        return row

    def assign(
        self,
        *,
        item_id: str,
        actor: str,
        actor_type: str,
        customer_id: Any,
        enrollment_id: Any = None,
        credit_account: Any = None,
        nickname: Any = None,
        lockbox_id: Any = None,
    ) -> LockboxItem:
        self._require_staff(actor_type)
        row = self.get_item(item_id=item_id, actor=actor, actor_type=actor_type)
        if row.status != ITEM_UNMATCHED:
            raise LockboxError('not_assignable', 'Only unmatched remittances can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise LockboxError('missing_customer_id', 'Customer id is required.')
        enrollment = None
        if enrollment_id:
            enrollment = self.get_enrollment(
                enrollment_id=str(enrollment_id).strip(), actor=actor, actor_type=actor_type,
            )
            if enrollment.userid != owner:
                raise LockboxError('lockbox_forbidden', 'Enrollment does not belong to this customer.')
        if enrollment is None:
            box = None
            try:
                box = normalize_lockbox_id(lockbox_id or row.lockbox_id)
            except LockboxError:
                box = row.lockbox_id
            if box:
                enrollment = self.store.find_enrollment_by_lockbox(box, owner)
        if enrollment is None:
            account = credit_account or row.credit_account
            if not account:
                raise LockboxError('missing_enrollment', 'Credit account is required to assign.')
            enrollment = self.enroll(
                owner_userid=owner,
                actor=actor,
                actor_type=actor_type,
                nickname=nickname or ('Lockbox %s' % (row.lockbox_id or last4(account) or 'box')),
                lockbox_id=lockbox_id or row.lockbox_id,
                credit_account=account,
            )
        self._assert_internal_account(owner, enrollment.credit_account)
        row.userid = owner
        row.enrollment_id = enrollment.enrollment_id
        row.lockbox_id = enrollment.lockbox_id
        row.credit_account = enrollment.credit_account
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        ofac = ScreenResult(bool(row.ofac_hit), row.ofac_match, 100 if row.ofac_hit else 0)
        matched = self._match_invoice(enrollment, row)
        row.invoice_matched = 1 if matched is not None else 0
        status, reason = self._evaluate_status(
            enrollment=enrollment,
            dollars=parse_money(row.amount),
            ofac=ofac,
            invoice_matched=bool(row.invoice_matched),
            skip_invoice=True,
        )
        row.status = status
        row.reason = reason
        self.store.update_item(row)
        if status == ITEM_POSTED:
            return self._try_post(row)
        return row

    def override_ofac(
        self,
        *,
        item_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> LockboxItem:
        self._require_staff(actor_type)
        row = self.get_item(item_id=item_id, actor=actor, actor_type=actor_type)
        if row.status != ITEM_HELD:
            raise LockboxError('not_overridable', 'Only an OFAC hold can be overridden.')
        enrollment = self.store.get_enrollment(row.enrollment_id) if row.enrollment_id else None
        row.ofac_hit = 0
        row.ofac_match = ''
        row.note = normalize_note(note) or 'ofac override'
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        status, reason = self._evaluate_status(
            enrollment=enrollment,
            dollars=parse_money(row.amount),
            ofac=ScreenResult(False, '', 0),
            invoice_matched=bool(row.invoice_matched),
            skip_invoice=True,
        )
        row.status = status
        row.reason = reason
        self.store.update_item(row)
        if status == ITEM_POSTED:
            return self._try_post(row)
        return row

    def release(
        self,
        *,
        item_id: str,
        actor: str,
        actor_type: str,
    ) -> LockboxItem:
        self._require_staff(actor_type)
        row = self.get_item(item_id=item_id, actor=actor, actor_type=actor_type)
        if row.status == ITEM_HELD:
            raise LockboxError('ofac_hold', 'OFAC hold must be overridden before release.')
        if row.status not in {ITEM_PENDING, ITEM_QUEUED}:
            raise LockboxError('not_releasable', 'Only queued or pending remittances can be released.')
        if row.status == ITEM_PENDING and row.actor and str(actor) == str(row.actor):
            if parse_money(row.amount) >= self.policy.dual_control_threshold:
                raise LockboxError('same_approver', 'A different employee must release this remittance.')
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        if clock['after_cutoff'] or not clock['business_day']:
            row.status = ITEM_QUEUED
            row.value_date = self.calendar.cycle_date(now)
            row.updated_at = now
            row.releaser = str(actor)
            self.store.update_item(row)
            return row
        row.releaser = str(actor)
        return self._try_post(row)

    def reject(
        self,
        *,
        item_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> LockboxItem:
        self._require_staff(actor_type)
        row = self.get_item(item_id=item_id, actor=actor, actor_type=actor_type)
        if row.status not in PRE_POST:
            raise LockboxError('not_rejectable', 'Only open remittances can be rejected.')
        row.status = ITEM_REJECTED
        row.reason = normalize_note(reason, limit=40)
        row.note = normalize_note(note)
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_item(row)
        return row

    def _return_window_open(self, row: LockboxItem) -> bool:
        if row.status in PRE_POST:
            return True
        if row.status != ITEM_POSTED or not row.posted_at:
            return False
        posted_day = self.calendar.local_dt(row.posted_at).date()
        deadline = self.calendar.next_business_day(posted_day)
        return self._today(float(self.clock())) <= deadline

    def return_item(
        self,
        *,
        item_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
        enforce_window: bool = False,
    ) -> LockboxItem:
        if actor_type not in EMPLOYEE_ROLES:
            self._require_return(actor_type)
        else:
            self._require_staff(actor_type)
        row = self.get_item(item_id=item_id, actor=actor, actor_type=actor_type)
        if row.status == ITEM_RETURNED:
            raise LockboxError('already_returned', 'Remittance is already returned.')
        if row.status not in PRE_POST and row.status != ITEM_POSTED:
            raise LockboxError('not_returnable', 'This remittance cannot be returned.')
        if enforce_window and not self._return_window_open(row):
            raise LockboxError('return_window_closed', 'The customer return window is closed.')
        now = float(self.clock())
        if row.status == ITEM_POSTED:
            classified = self._debit(row)
            if classified == 'nsf':
                raise LockboxError('nsf', 'Insufficient funds to return this remittance.', item=row)
            if classified != 'ok':
                raise LockboxError('return_failed', 'Lockbox return debit failed.', item=row)
        row.status = ITEM_RETURNED
        row.reason = normalize_note(reason, limit=40)
        row.note = normalize_note(note)
        row.returned_at = now
        row.updated_at = now
        row.actor = str(actor)
        self.store.update_item(row)
        return row

    def request_return(
        self,
        *,
        item_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'cust',
        note: Any = '',
    ) -> LockboxItem:
        return self.return_item(
            item_id=item_id, actor=actor, actor_type=actor_type,
            reason=reason, note=note, enforce_window=True,
        )

    def export_file(
        self,
        *,
        actor: str,
        actor_type: str,
        customer_id: Any = None,
        enrollment_id: Any = None,
    ) -> str:
        self._require_staff(actor_type)
        owner = str(customer_id or '').strip() or None
        enr = str(enrollment_id or '').strip() or None
        items = self.store.list_items(userid=owner, enrollment_id=enr)
        if owner is not None and not items:
            items = [row for row in self.store.list_all_items() if row.userid == owner]
        posted = [row for row in items if row.status == ITEM_POSTED]
        if not posted:
            raise LockboxError('invalid_file', 'No posted remittances to export.')
        payload = []
        for row in posted:
            payload.append({
                'amount': row.amount,
                'bank_ref': row.bank_ref,
                'invoice': row.invoice_number,
                'remitter_name': row.remitter_name,
                'serial': row.serial,
                'lockbox_id': row.lockbox_id,
            })
        return compose_bai2(
            payload,
            sender=self.policy.sender_id,
            receiver=self.policy.receiver_aba,
            as_of=self._cycle(float(self.clock())),
            lockbox_id=posted[0].lockbox_id,
        )

    def _rematch_lockbox(self, lockbox_id: str) -> List[LockboxItem]:
        changed: List[LockboxItem] = []
        enrollment = self.store.find_enrollment_by_lockbox(lockbox_id)
        if enrollment is None or enrollment.status != BOX_ACTIVE:
            return changed
        for row in self.store.list_unmatched():
            if row.lockbox_id != lockbox_id and row.lockbox_id:
                continue
            if row.lockbox_id and row.lockbox_id != enrollment.lockbox_id:
                continue
            if row.lockbox_id != enrollment.lockbox_id and row.credit_account != enrollment.credit_account:
                if row.lockbox_id:
                    continue
            row.userid = enrollment.userid
            row.enrollment_id = enrollment.enrollment_id
            row.lockbox_id = enrollment.lockbox_id
            row.credit_account = enrollment.credit_account
            matched = self._match_invoice(enrollment, row)
            row.invoice_matched = 1 if matched is not None else 0
            ofac = ScreenResult(bool(row.ofac_hit), row.ofac_match, 100 if row.ofac_hit else 0)
            status, reason = self._evaluate_status(
                enrollment=enrollment,
                dollars=parse_money(row.amount),
                ofac=ofac,
                invoice_matched=bool(row.invoice_matched),
            )
            row.status = status
            row.reason = reason
            row.updated_at = float(self.clock())
            self.store.update_item(row)
            if status == ITEM_POSTED:
                try:
                    row = self._try_post(row)
                except LockboxError:
                    pass
            changed.append(row)
        return changed

    def run_due(self, userid: Optional[str] = None) -> List[LockboxItem]:
        now = float(self.clock())
        today = self._today(now).strftime('%Y%m%d')
        after = self.calendar.snapshot(now)['after_cutoff']
        changed: List[LockboxItem] = []
        rows = self.store.list_items(userid) if userid is not None else self.store.list_all_items()
        for row in rows:
            if row.status != ITEM_QUEUED:
                continue
            if row.value_date > today:
                continue
            if after and row.value_date == today:
                continue
            dollars = parse_money(row.amount)
            if self._needs_dual_control(dollars):
                row.status = ITEM_PENDING
                row.reason = 'dual_control'
                row.updated_at = now
                self.store.update_item(row)
                changed.append(row)
                continue
            try:
                changed.append(self._try_post(row))
            except LockboxError:
                changed.append(self.store.get_item(row.item_id) or row)
        return changed

    def unmatched_snapshot(self) -> Dict[str, Any]:
        rows = self.store.list_unmatched()
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'clock': self.calendar.snapshot(now),
            'items': [row.to_dict() for row in rows[:40]],
            'unmatched_count': len(rows),
        }

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        _ = actor
        self.run_due(userid)
        enrollments = self.store.list_enrollments(userid)
        invoices = self.store.list_invoices(userid)
        items = self.store.list_items(userid)
        posted_ytd = Decimal('0.00')
        returned_ytd = Decimal('0.00')
        for row in items:
            amount = parse_money(row.amount, allow_zero=True)
            if row.status == ITEM_POSTED:
                posted_ytd += amount
            elif row.status == ITEM_RETURNED:
                returned_ytd += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'receiver_aba': self.policy.receiver_aba,
            'clock': self.calendar.snapshot(now),
            'enrollments': [row.to_dict() for row in enrollments[:40]],
            'invoices': [row.to_dict() for row in invoices[:40]],
            'items': [row.to_dict() for row in items[:40]],
            'ytd_posted': money_str(posted_ytd),
            'ytd_returned': money_str(returned_ytd),
            'active_count': sum(1 for row in enrollments if row.status == BOX_ACTIVE),
            'open_count': sum(1 for row in items if row.status in OPEN_ITEMS),
        }


_SERVICE: Optional[LockboxService] = None


def set_service(service: Optional[LockboxService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[LockboxService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('LOCKBOX_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryLockboxStore()
    path = os.environ.get('LOCKBOX_DB', DEFAULT_STORE_PATH)
    return SqliteLockboxStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[WireCalendar] = None,
) -> LockboxService:
    if store is None:
        store = default_store()
    return LockboxService(
        LockboxPolicy.from_env(),
        store,
        clock=clock,
        debit_fn=debit_fn,
        credit_fn=credit_fn,
        accounts_fn=accounts_fn,
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


def _owner_userid(actor: str, actor_type: str, values: Dict[str, Any]) -> str:
    if actor_type in EMPLOYEE_ROLES:
        return str(values.get('customer_id') or values.get('owner') or '').strip()
    return actor


def _error_status(code: str) -> int:
    return {
        'enrollment_duplicate': 409,
        'enrollment_limit': 409,
        'invoice_duplicate': 409,
        'invoice_limit': 409,
        'item_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_returned': 409,
        'already_cancelled': 409,
        'already_applied': 409,
        'nsf': 409,
        'failed': 409,
        'return_failed': 409,
        'lockbox_forbidden': 403,
        'lockbox_disabled': 403,
        'enrollment_paused': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_returnable': 403,
        'return_window_closed': 403,
        'enrollment_not_found': 404,
        'invoice_not_found': 404,
        'item_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_lockbox': 400,
        'invalid_invoice': 400,
        'invalid_serial': 400,
        'invalid_aba': 400,
        'invalid_file': 400,
        'invalid_type': 400,
        'invalid_reason': 400,
        'invalid_status': 400,
        'invalid_date': 400,
        'invalid_reference': 400,
        'wrong_receiver': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_enrollment': 400,
        'missing_item': 400,
        'missing_file': 400,
        'missing_invoice': 400,
    }.get(code, 400)


def _error_body(exc: LockboxError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('item') is not None:
        body['item'] = exc.extra['item'].to_dict()
    if exc.extra.get('enrollment') is not None:
        body['enrollment'] = exc.extra['enrollment'].to_dict()
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
    except LockboxError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: LockboxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Lockboxes': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_unmatched(service: LockboxService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'lockbox_forbidden'}), 403
    return jsonify({'Lockboxes': service.unmatched_snapshot()}), 200


def handle_enroll(service: LockboxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.enroll(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname'),
            lockbox_id=values.get('lockbox_id') or values.get('lockbox'),
            credit_account=values.get('credit_account') or values.get('account') or values.get('default_account'),
            require_invoice=values.get('require_invoice'),
        )
        return jsonify({
            'message': 'Lockbox enrolled',
            'enrollment': row.to_dict(),
            'Lockboxes': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _box_status_route(service: LockboxService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    enrollment_id = str(values.get('enrollment_id') or '').strip()
    if not enrollment_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_enrollment'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_enrollment_status(
            enrollment_id=enrollment_id, actor=userid, actor_type=actor_type, status=status,
        )
        return jsonify({
            'message': ok_message,
            'enrollment': row.to_dict(),
            'Lockboxes': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_add_invoice(service: LockboxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    enrollment_id = str(values.get('enrollment_id') or '').strip()
    if not enrollment_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_enrollment'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.add_invoice(
            enrollment_id=enrollment_id,
            actor=userid,
            actor_type=actor_type,
            invoice_number=values.get('invoice_number') or values.get('invoice'),
            amount=values.get('amount') or '',
            remitter_name=values.get('remitter_name') or values.get('remitter') or '',
        )
        return jsonify({
            'message': 'Invoice added',
            'invoice': row.to_dict(),
            'Lockboxes': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def handle_cancel_invoice(service: LockboxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    invoice_id = str(values.get('invoice_id') or '').strip()
    if not invoice_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_invoice'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.cancel_invoice(invoice_id=invoice_id, actor=userid, actor_type=actor_type)
        return jsonify({
            'message': 'Invoice cancelled',
            'invoice': row.to_dict(),
            'Lockboxes': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: LockboxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}

    def _run():
        preview = service.preview_message(values)
        return jsonify({'preview': preview}), 200

    return _handle_errors(_run)


def handle_ingest(service: LockboxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'Lockbox remittance ingested' if created else 'Lockbox remittance already posted',
            'item': row.to_dict(),
            'Lockboxes': service.snapshot(owner, actor=userid, actor_type=actor_type) if row.userid else service.unmatched_snapshot(),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_file(service: LockboxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    text = values.get('file') or values.get('bai2') or values.get('text')
    if not text:
        return jsonify({'message': 'Operator file is required', 'error': 'missing_file'}), 400

    def _run():
        result = service.ingest_file(
            actor=userid,
            actor_type=actor_type,
            text=text,
            customer_id=values.get('customer_id') or '',
        )
        result['Unmatched'] = service.unmatched_snapshot()
        return jsonify(result), 201 if result['accepted_count'] else 200

    return _handle_errors(_run)


def handle_assign(service: LockboxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    item_id = str(values.get('item_id') or '').strip()
    if not item_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_item'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.assign(
            item_id=item_id,
            actor=userid,
            actor_type=actor_type,
            customer_id=values.get('customer_id') or values.get('owner'),
            enrollment_id=values.get('enrollment_id'),
            credit_account=values.get('account') or values.get('credit_account'),
            nickname=values.get('nickname'),
            lockbox_id=values.get('lockbox_id'),
        )
        return jsonify({
            'message': 'Lockbox remittance assigned',
            'item': row.to_dict(),
            'Lockboxes': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_action(service: LockboxService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    item_id = str(values.get('item_id') or '').strip()
    if not item_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_item'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'override':
            row = service.override_ofac(
                item_id=item_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'release':
            row = service.release(item_id=item_id, actor=userid, actor_type=actor_type)
            message = 'Lockbox remittance released'
        elif action == 'reject':
            row = service.reject(
                item_id=item_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Lockbox remittance rejected'
        elif action == 'return':
            row = service.return_item(
                item_id=item_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Lockbox remittance returned'
        else:
            raise LockboxError('invalid_status', 'Unknown lockbox action.')
        owner = row.userid or userid
        return jsonify({
            'message': message,
            'item': row.to_dict(),
            'Lockboxes': service.snapshot(owner, actor=userid, actor_type=actor_type) if row.userid else service.unmatched_snapshot(),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: LockboxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    item_id = str(values.get('item_id') or '').strip()
    if not item_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_item'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.request_return(
            item_id=item_id,
            actor=userid,
            actor_type=actor_type,
            reason=values.get('reason') or 'cust',
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Lockbox return requested',
            'item': row.to_dict(),
            'Lockboxes': service.snapshot(row.userid or userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_export(service: LockboxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        text = service.export_file(
            actor=userid,
            actor_type=actor_type,
            customer_id=values.get('customer_id'),
            enrollment_id=values.get('enrollment_id'),
        )
        return jsonify({'file': text, 'message': 'Lockbox BAI2 exported'}), 200

    return _handle_errors(_run)


def handle_run_due(service: LockboxService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner if actor_type not in EMPLOYEE_ROLES else None)
    return jsonify({'Lockboxes': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_lockbox_routes(app, service: LockboxService) -> None:
    @app.route('/listLockboxes', methods=['POST', 'GET'])
    def list_lockboxes_route():
        return handle_list(service)

    @app.route('/listLockboxItems', methods=['POST', 'GET'])
    def list_lockbox_items_route():
        return handle_list(service)

    @app.route('/listUnmatchedLockboxes', methods=['POST', 'GET'])
    def list_unmatched_lockboxes_route():
        return handle_unmatched(service)

    @app.route('/enrollLockbox', methods=['POST', 'GET'])
    def enroll_lockbox_route():
        return handle_enroll(service)

    @app.route('/pauseLockbox', methods=['POST', 'GET'])
    def pause_lockbox_route():
        return _box_status_route(service, BOX_PAUSED, 'Lockbox paused')

    @app.route('/resumeLockbox', methods=['POST', 'GET'])
    def resume_lockbox_route():
        return _box_status_route(service, BOX_ACTIVE, 'Lockbox resumed')

    @app.route('/archiveLockbox', methods=['POST', 'GET'])
    def archive_lockbox_route():
        return _box_status_route(service, BOX_ARCHIVED, 'Lockbox archived')

    @app.route('/addLockboxInvoice', methods=['POST', 'GET'])
    def add_lockbox_invoice_route():
        return handle_add_invoice(service)

    @app.route('/cancelLockboxInvoice', methods=['POST', 'GET'])
    def cancel_lockbox_invoice_route():
        return handle_cancel_invoice(service)

    @app.route('/previewLockbox', methods=['POST', 'GET'])
    def preview_lockbox_route():
        return handle_preview(service)

    @app.route('/ingestLockbox', methods=['POST', 'GET'])
    def ingest_lockbox_route():
        return handle_ingest(service)

    @app.route('/ingestLockboxFile', methods=['POST', 'GET'])
    def ingest_lockbox_file_route():
        return handle_ingest_file(service)

    @app.route('/assignLockbox', methods=['POST', 'GET'])
    def assign_lockbox_route():
        return handle_assign(service)

    @app.route('/overrideLockboxOfac', methods=['POST', 'GET'])
    def override_lockbox_ofac_route():
        return _staff_action(service, 'override')

    @app.route('/releaseLockbox', methods=['POST', 'GET'])
    def release_lockbox_route():
        return _staff_action(service, 'release')

    @app.route('/rejectLockbox', methods=['POST', 'GET'])
    def reject_lockbox_route():
        return _staff_action(service, 'reject')

    @app.route('/returnLockbox', methods=['POST', 'GET'])
    def return_lockbox_route():
        return _staff_action(service, 'return')

    @app.route('/requestLockboxReturn', methods=['POST', 'GET'])
    def request_lockbox_return_route():
        return handle_request_return(service)

    @app.route('/exportLockbox', methods=['POST', 'GET'])
    def export_lockbox_route():
        return handle_export(service)

    @app.route('/runDueLockboxes', methods=['POST', 'GET'])
    def run_due_lockboxes_route():
        return handle_run_due(service)
