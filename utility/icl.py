"""Inbound Check21 / X9.37 image cash letter presentment (paying bank).

Staff ingest incoming ANSI X9.37 ICL files of checks drawn on this bank.
Matched on-us items debit the drawer; unmatched, OFAC, dual-control, and
after-cutoff items sit in reusable queues. Independent of in-app cashier
cheque deposit, outbound Fedwire (PR #73), ACH linking (PR #68), inbound
Fedwire (PR #90), and Interac origination (PR #113). Existing
`/fundTransfer`, `/withdrawAmount`, `/sendWire`, and `/depositCheck`
stay unchanged.

Foundations (reusable beyond this screen):
- X9.37 pipe parse / compose / file split (XML/DOCTYPE rejected)
- 15-digit ECE item-sequence uniqueness
- 10-digit X9.37 cents amount field
- MICR on-us account / serial parse
- Image-view fingerprint (SHA-256; never in snapshots)
- Type-31 return compose with X9.100-187 reason codes
- Receiver-ABA / payor-routing acceptance (this bank)
- Account-directory lookup (drawer on-us → customer)
- Incoming debit posting + next-business-day customer return window
- Fed business-day / Check21 14:00 ET cutoff clock (reused)
- OFAC-style payee screening (reused)
- Dual-control release for high-value inbound presentments

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Drawer account numbers and image fingerprints never appear in to_dict / snapshots.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import date
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
    last4,
    money_str,
    normalize_aba,
    normalize_account,
    normalize_id,
    normalize_legal_name,
    normalize_note,
    normalize_party,
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
TYPE_FORWARD = '25'
TYPE_RETURN = '31'
TYPE_ALIASES = {
    '25': TYPE_FORWARD, 'check': TYPE_FORWARD, 'presentment': TYPE_FORWARD,
    'icl': TYPE_FORWARD, 'x937': TYPE_FORWARD, 'forward': TYPE_FORWARD,
    '31': TYPE_RETURN, 'return': TYPE_RETURN, 'ret': TYPE_RETURN,
}
RETURN_REASONS = frozenset({
    'nsf', 'uncollected', 'stop', 'closed', 'locate', 'frozen', 'stale',
    'postdated', 'endorse', 'signature', 'irregular', 'noncash', 'altered',
    'process', 'maker', 'limit', 'unauthorized', 'sold', 'image',
    'missing_image', 'warranty', 'duplicate', 'forged', 'other',
})
RETURN_ALIASES = {
    'a': 'nsf', 'insufficient': 'nsf',
    'b': 'uncollected',
    'c': 'stop', 'stop_payment': 'stop',
    'd': 'closed', 'acct': 'closed',
    'e': 'locate', 'utla': 'locate',
    'f': 'frozen', 'blocked': 'frozen',
    'g': 'stale', 'stale_date': 'stale',
    'h': 'postdated', 'post_dated': 'postdated',
    'i': 'endorse', 'endorsement': 'endorse',
    'j': 'signature',
    'k': 'irregular',
    'l': 'noncash',
    'm': 'altered', 'fictitious': 'altered',
    'n': 'process',
    'o': 'maker', 'cust': 'maker', 'customer': 'maker', 'refer': 'maker',
    'p': 'limit',
    'q': 'unauthorized',
    'r': 'sold',
    's': 'image',
    't': 'missing_image', 'image_missing': 'missing_image',
    'u': 'warranty',
    'y': 'duplicate', 'dup': 'duplicate',
    'z': 'forged', 'forgery': 'forged',
}
RETURN_X9 = {
    'nsf': 'A', 'uncollected': 'B', 'stop': 'C', 'closed': 'D', 'locate': 'E',
    'frozen': 'F', 'stale': 'G', 'postdated': 'H', 'endorse': 'I',
    'signature': 'J', 'irregular': 'K', 'noncash': 'L', 'altered': 'M',
    'process': 'N', 'maker': 'O', 'limit': 'P', 'unauthorized': 'Q',
    'sold': 'R', 'image': 'S', 'missing_image': 'T', 'warranty': 'U',
    'duplicate': 'Y', 'forged': 'Z', 'other': 'O',
}
HEADER_TYPES = frozenset({'01', '10', '20'})
CONTROL_TYPES = frozenset({'70', '90', '99'})
ADDENDA_TYPES = frozenset({'26', '28', '50', '52'})
DEFAULT_STORE_PATH = 'SystemLogs/icl.sqlite'
DEFAULT_RECEIVER_ABA = '021000021'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)


class IclError(ValueError):
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
        if any(token in low for token in DEBIT_NSF):
            return 'nsf'
        if low in DEBIT_OK:
            return 'ok'
        return 'failed'
    return 'failed'


def _reject_xml(text: str) -> None:
    low = text.lower()
    if '<!doctype' in low or '<?xml' in low or '<xml' in low:
        raise IclError('invalid_file', 'XML/DOCTYPE ICL files are rejected.')


def compose_amount_field(amount: Decimal) -> str:
    """X9.37 Type 25 amount: 10-digit cents."""
    cents = int((amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN) * 100).to_integral_value())
    if cents < 0 or cents > 9999999999:
        raise IclError('invalid_amount', 'Amount cannot be encoded in X9.37.')
    return '%010d' % cents


def parse_amount_field(value: Any) -> Decimal:
    text = str(value or '').strip().replace(',', '').replace('$', '')
    if not text:
        raise IclError('invalid_amount', 'Amount is required.')
    if text.isdigit() and len(text) == 10:
        return (Decimal(text) / Decimal('100')).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    try:
        return parse_money(text)
    except AmountError as exc:
        raise IclError('invalid_amount', 'Invalid Check21 amount.') from exc


def compose_ece(cycle_date: str, sequence: int) -> str:
    """ECE institution item sequence: YYYYMMDD + 7-digit sequence (15 digits)."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise IclError('invalid_ece', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 9999999:
        raise IclError('invalid_ece', 'ECE sequence out of range.')
    return '%s%07d' % (day, seq)


def normalize_ece(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 15:
        raise IclError('invalid_ece', 'ECE item sequence must be 15 digits.')
    return digits


def mask_ece(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 12:
        return '*' * 15
    return digits[:8] + '***' + digits[-4:]


def normalize_serial(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise IclError('invalid_serial', 'Check serial is required.')
        return ''
    if len(digits) > 10:
        raise IclError('invalid_serial', 'Check serial must be 1-10 digits.')
    return digits


def parse_onus(value: Any) -> Tuple[str, str]:
    """MICR On-Us: account[/serial]. Trailing digits map to the customer directory."""
    text = str(value or '').strip()
    if not text:
        return '', ''
    if '/' in text:
        left, right = text.split('/', 1)
        account = ''.join(ch for ch in left if ch.isdigit())
        serial = ''.join(ch for ch in right if ch.isdigit())[:10]
        return account, serial
    digits = ''.join(ch for ch in text if ch.isdigit())
    return digits, ''


def image_fingerprint(ece: str, *, front: bool, rear: bool, payload: str = '') -> str:
    """SHA-256 of view flags + ECE + optional payload. Never store raw image bytes."""
    material = '%s|%s|%s|%s' % (ece, 'F' if front else '-', 'R' if rear else '-', payload)
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def _truthy_flag(value: Any) -> bool:
    text = str(value or '').strip().lower()
    return text in {'1', 'y', 'yes', 'true', 'front', 'rear', 'present'}


def normalize_type_code(value: Any, *, default: str = TYPE_FORWARD) -> str:
    text = str(value or default).strip().lower()
    mapped = TYPE_ALIASES.get(text, text)
    if mapped not in {TYPE_FORWARD, TYPE_RETURN}:
        raise IclError('invalid_type', 'Type must be 25 (presentment) or 31 (return).')
    return mapped


def normalize_return_reason(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RETURN_ALIASES.get(text, text)
    if text not in RETURN_REASONS:
        raise IclError('invalid_reason', 'Unknown Check21 return reason.')
    return text


def _split_record(line: str) -> List[str]:
    return [part.strip() for part in line.split('|')]


def parse_x937(text: Any) -> Dict[str, str]:
    """Parse one Check21 item (Type 25 plus optional 26/50 addenda) into fields."""
    raw = str(text or '')
    _reject_xml(raw)
    lines = [line.strip() for line in raw.replace('\r\n', '\n').replace('\r', '\n').split('\n') if line.strip()]
    if not lines:
        raise IclError('invalid_file', 'X9.37 message is empty.')
    fields: Dict[str, str] = {}
    found_detail = False
    for line in lines:
        parts = _split_record(line)
        rtype = parts[0] if parts else ''
        if rtype in HEADER_TYPES or rtype in CONTROL_TYPES:
            if rtype == '01' and len(parts) > 4:
                fields['file_destination'] = parts[4]
            if rtype == '10' and len(parts) > 3:
                fields['cash_letter_destination'] = parts[3]
            continue
        if rtype == TYPE_RETURN:
            raise IclError('invalid_type', 'Forward ICL ingest does not accept Type 31 returns.')
        if rtype == TYPE_FORWARD:
            found_detail = True
            # 25|Check|ece|aux|payor|onus|serial|amount|bofd|payee
            if len(parts) < 8:
                raise IclError('invalid_file', 'Type 25 check detail is truncated.')
            fields['record_type'] = TYPE_FORWARD
            fields['ece'] = parts[2] if len(parts) > 2 else ''
            fields['aux_on_us'] = parts[3] if len(parts) > 3 else ''
            fields['payor_aba'] = parts[4] if len(parts) > 4 else ''
            fields['on_us'] = parts[5] if len(parts) > 5 else ''
            fields['serial'] = parts[6] if len(parts) > 6 else ''
            fields['amount'] = parts[7] if len(parts) > 7 else ''
            fields['bofd_aba'] = parts[8] if len(parts) > 8 else ''
            fields['payee_name'] = parts[9] if len(parts) > 9 else ''
            continue
        if rtype == '26' and len(parts) > 1:
            fields['bofd_aba'] = fields.get('bofd_aba') or (parts[2] if len(parts) > 2 else parts[1])
            if len(parts) > 4 and not fields.get('payee_name'):
                fields['payee_name'] = parts[4]
            continue
        if rtype == '50':
            fields['front'] = parts[2] if len(parts) > 2 else ''
            fields['rear'] = parts[3] if len(parts) > 3 else ''
            fields['image_payload'] = parts[4] if len(parts) > 4 else ''
            continue
    if not found_detail:
        raise IclError('invalid_file', 'X9.37 message has no Type 25 check detail.')
    return fields


def split_x937_file(text: Any) -> List[str]:
    """Split an operator ICL into Type 25 items (addenda 26/50 ride with the item)."""
    raw = str(text or '')
    _reject_xml(raw)
    lines = [line.strip() for line in raw.replace('\r\n', '\n').replace('\r', '\n').split('\n') if line.strip()]
    if not lines:
        return []
    chunks: List[List[str]] = []
    current: List[str] = []
    for line in lines:
        rtype = line[:2]
        if rtype == TYPE_FORWARD:
            if current:
                chunks.append(current)
            current = [line]
        elif rtype in ADDENDA_TYPES and current:
            current.append(line)
        elif rtype == TYPE_RETURN:
            if current:
                chunks.append(current)
                current = []
            chunks.append([line])
        # headers / controls are file-level and skipped
    if current:
        chunks.append(current)
    return ['\n'.join(chunk) for chunk in chunks]


def compose_x937(fields: Dict[str, str], *, with_image: bool = True) -> str:
    payload = {str(key): str(value or '').strip() for key, value in fields.items()}
    ece = payload.get('ece') or ''
    line = '|'.join([
        TYPE_FORWARD,
        'Check',
        ece,
        payload.get('aux_on_us') or '',
        payload.get('payor_aba') or '',
        payload.get('on_us') or payload.get('drawer_account') or '',
        payload.get('serial') or '',
        payload.get('amount') or '',
        payload.get('bofd_aba') or '',
        payload.get('payee_name') or '',
    ])
    lines = [line]
    if with_image:
        front = payload.get('front') or 'Y'
        rear = payload.get('rear') or 'Y'
        lines.append('|'.join(['50', 'Image', front, rear, payload.get('image_payload') or '']))
    return '\n'.join(lines)


def compose_return_x937(row: 'InboundIcl', *, reason: str) -> str:
    amount = parse_money(row.amount)
    code = RETURN_X9.get(reason, 'O')
    return '|'.join([
        TYPE_RETURN,
        'Return',
        row.ece,
        code,
        row.payor_aba,
        last4(row.drawer_account),
        compose_amount_field(amount),
        row.bofd_aba,
        reason.upper(),
    ])


def message_from_x937(text: Any) -> Dict[str, Any]:
    fields = parse_x937(text)
    amount = parse_amount_field(fields.get('amount'))
    try:
        payor = normalize_aba(fields.get('payor_aba') or fields.get('file_destination') or '')
    except WireError as exc:
        raise IclError('invalid_aba', exc.message) from exc
    bofd_raw = fields.get('bofd_aba') or ''
    try:
        bofd = normalize_aba(bofd_raw) if bofd_raw else payor
    except WireError as exc:
        raise IclError('invalid_aba', exc.message) from exc
    on_us, parsed_serial = parse_onus(fields.get('on_us'))
    serial = normalize_serial(fields.get('serial') or parsed_serial)
    ece = normalize_ece(fields.get('ece'))
    front = _truthy_flag(fields.get('front') or 'Y')
    rear = _truthy_flag(fields.get('rear') or 'Y')
    fingerprint = image_fingerprint(
        ece, front=front, rear=rear, payload=fields.get('image_payload') or '',
    ) if (front or rear or fields.get('image_payload')) else ''
    return {
        'ece': ece,
        'type_code': TYPE_FORWARD,
        'amount': money_str(amount),
        'payor_aba': payor,
        'bofd_aba': bofd,
        'drawer_account': on_us,
        'serial': serial,
        'aux_on_us': str(fields.get('aux_on_us') or '')[:15],
        'payee_name': str(fields.get('payee_name') or 'PAYEE').strip() or 'PAYEE',
        'front': front,
        'rear': rear,
        'image_fingerprint': fingerprint,
        'raw': compose_x937({
            'ece': ece,
            'aux_on_us': fields.get('aux_on_us') or '',
            'payor_aba': payor,
            'on_us': on_us,
            'serial': serial,
            'amount': compose_amount_field(amount),
            'bofd_aba': bofd,
            'payee_name': str(fields.get('payee_name') or 'PAYEE').strip(),
            'front': 'Y' if front else 'N',
            'rear': 'Y' if rear else 'N',
        }),
    }


def message_from_values(values: Dict[str, Any]) -> Dict[str, Any]:
    if values.get('file') or values.get('x937') or values.get('icl') or values.get('raw'):
        return message_from_x937(values.get('file') or values.get('x937') or values.get('icl') or values.get('raw'))
    ece = values.get('ece')
    if not ece:
        raise IclError('invalid_ece', 'ECE item sequence is required.')
    amount = parse_amount_field(values.get('amount'))
    try:
        payor = normalize_aba(values.get('payor_aba') or values.get('receiver_aba') or values.get('payor'))
        bofd = normalize_aba(values.get('bofd_aba') or values.get('bofd') or values.get('sender_aba') or payor)
        drawer = str(values.get('drawer_account') or values.get('account') or values.get('on_us') or '').strip()
        if not drawer:
            raise IclError('invalid_account', 'Drawer on-us account is required.')
        on_us, parsed_serial = parse_onus(drawer)
        if not on_us:
            on_us = ''.join(ch for ch in drawer if ch.isdigit())
    except WireError as exc:
        raise IclError(exc.code, exc.message) from exc
    serial = normalize_serial(values.get('serial') or parsed_serial)
    ece_norm = normalize_ece(ece)
    front = _truthy_flag(values['front']) if 'front' in values else True
    rear = _truthy_flag(values['rear']) if 'rear' in values else True
    payload = str(values.get('image_payload') or '')
    fingerprint = image_fingerprint(ece_norm, front=front, rear=rear, payload=payload) if (front or rear or payload) else ''
    payee = str(values.get('payee_name') or values.get('payee') or 'PAYEE').strip() or 'PAYEE'
    return {
        'ece': ece_norm,
        'type_code': normalize_type_code(values.get('type_code') or values.get('type') or TYPE_FORWARD),
        'amount': money_str(amount),
        'payor_aba': payor,
        'bofd_aba': bofd,
        'drawer_account': on_us,
        'serial': serial,
        'aux_on_us': str(values.get('aux_on_us') or '')[:15],
        'payee_name': payee,
        'front': front,
        'rear': rear,
        'image_fingerprint': fingerprint,
        'raw': '',
    }


@dataclass
class IclPolicy:
    enabled: bool = True
    customer_view: bool = True
    customer_return: bool = True
    allow_credit: bool = False
    max_inbounds: int = 240
    min_amount: Decimal = Decimal('0.01')
    max_amount: Decimal = Decimal('1000000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    cutoff_hour: int = 14
    tz_offset_hours: int = -4
    receiver_aba: str = DEFAULT_RECEIVER_ABA
    watchlist: Tuple[str, ...] = (
        'BLOCKED PERSON',
        'SANCTIONED ENTITY',
        'OFAC TESTNAME',
    )
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'IclPolicy':
        extra = _env_list('ICL_OFAC_LIST')
        watch = tuple(dict.fromkeys(cls.watchlist + extra))
        receiver = os.environ.get('ICL_RECEIVER_ABA') or DEFAULT_RECEIVER_ABA
        try:
            receiver = normalize_aba(receiver)
        except WireError:
            receiver = DEFAULT_RECEIVER_ABA
        return cls(
            enabled=_env_bool('ICL_ENABLED', True),
            customer_view=_env_bool('ICL_CUSTOMER_VIEW', True),
            customer_return=_env_bool('ICL_CUSTOMER_RETURN', True),
            allow_credit=_env_bool('ICL_ALLOW_CREDIT', False),
            max_inbounds=max(1, _env_int('ICL_MAX', 240)),
            min_amount=_env_money('ICL_MIN_AMOUNT', '0.01'),
            max_amount=_env_money('ICL_MAX_AMOUNT', '1000000.00'),
            dual_control_threshold=_env_money('ICL_DUAL_CONTROL', '10000.00'),
            cutoff_hour=max(0, min(23, _env_int('ICL_CUTOFF_HOUR', 14))),
            tz_offset_hours=_env_int('ICL_TZ_OFFSET', -4),
            receiver_aba=receiver,
            watchlist=watch,
            extra_holidays=_env_list('ICL_HOLIDAYS'),
        )


@dataclass
class InboundIcl:
    inbound_id: str
    ece: str
    userid: str
    internal_account: str
    amount: str
    payor_aba: str
    bofd_aba: str
    payee_name: str
    drawer_account: str
    serial: str
    type_code: str
    status: str
    value_date: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    return_reason: str
    return_record: str
    image_fingerprint: str
    image_present: int
    created_at: float
    updated_at: float
    posted_at: float = 0.0
    returned_at: float = 0.0
    note: str = ''
    batch_id: str = ''
    aux_on_us: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'inbound_id': self.inbound_id,
            'ece': mask_ece(self.ece),
            'ece_masked': mask_ece(self.ece),
            'userid': self.userid,
            'internal_account': self.internal_account,
            'amount': self.amount,
            'payor_aba': self.payor_aba,
            'bofd_aba': self.bofd_aba,
            'payee_name': self.payee_name,
            'drawer_last4': last4(self.drawer_account),
            'serial': self.serial,
            'type_code': self.type_code,
            'status': self.status,
            'value_date': self.value_date,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'return_reason': self.return_reason,
            'image_present': bool(self.image_present),
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


def _clone(row: InboundIcl) -> InboundIcl:
    return InboundIcl(**{key: getattr(row, key) for key in row.__dataclass_fields__})


def _from_row(row: Any) -> InboundIcl:
    return InboundIcl(
        inbound_id=row['inbound_id'],
        ece=row['ece'],
        userid=row['userid'] or '',
        internal_account=row['internal_account'] or '',
        amount=row['amount'],
        payor_aba=row['payor_aba'],
        bofd_aba=row['bofd_aba'],
        payee_name=row['payee_name'],
        drawer_account=row['drawer_account'],
        serial=row['serial'] or '',
        type_code=row['type_code'],
        status=row['status'],
        value_date=row['value_date'],
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        return_reason=row['return_reason'] or '',
        return_record=row['return_record'] or '',
        image_fingerprint=row['image_fingerprint'] or '',
        image_present=int(row['image_present'] or 0),
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        posted_at=float(row['posted_at'] or 0),
        returned_at=float(row['returned_at'] or 0),
        note=row['note'] or '',
        batch_id=row['batch_id'] or '',
        aux_on_us=row['aux_on_us'] or '',
    )


class MemoryIclStore:
    def __init__(self) -> None:
        self._rows: Dict[str, InboundIcl] = {}
        self._by_ece: Dict[str, str] = {}
        self._seq = 0
        self._lock = threading.Lock()

    def put(self, row: InboundIcl) -> None:
        with self._lock:
            self._rows[row.inbound_id] = _clone(row)
            self._by_ece[row.ece] = row.inbound_id

    def update(self, row: InboundIcl) -> None:
        with self._lock:
            if row.inbound_id not in self._rows:
                raise IclError('inbound_not_found', 'Inbound ICL item not found.')
            self._rows[row.inbound_id] = _clone(row)
            self._by_ece[row.ece] = row.inbound_id

    def get(self, inbound_id: str) -> Optional[InboundIcl]:
        with self._lock:
            row = self._rows.get(inbound_id)
            return _clone(row) if row is not None else None

    def get_by_ece(self, ece: str) -> Optional[InboundIcl]:
        with self._lock:
            inbound_id = self._by_ece.get(ece)
            row = self._rows.get(inbound_id) if inbound_id else None
            return _clone(row) if row is not None else None

    def list_for(self, userid: str) -> List[InboundIcl]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.userid == userid]
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def list_unmatched(self) -> List[InboundIcl]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.status == IN_UNMATCHED]
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def list_open(self) -> List[InboundIcl]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.status in OPEN_INBOUNDS]
            rows.sort(key=lambda item: item.created_at)
            return [_clone(row) for row in rows]

    def list_all(self) -> List[InboundIcl]:
        with self._lock:
            rows = list(self._rows.values())
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def next_sequence(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq


class SqliteIclStore:
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
                    ece TEXT NOT NULL UNIQUE,
                    userid TEXT NOT NULL DEFAULT '',
                    internal_account TEXT NOT NULL DEFAULT '',
                    amount TEXT NOT NULL,
                    payor_aba TEXT NOT NULL,
                    bofd_aba TEXT NOT NULL,
                    payee_name TEXT NOT NULL,
                    drawer_account TEXT NOT NULL,
                    serial TEXT NOT NULL DEFAULT '',
                    type_code TEXT NOT NULL,
                    status TEXT NOT NULL,
                    value_date TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    return_reason TEXT NOT NULL DEFAULT '',
                    return_record TEXT NOT NULL DEFAULT '',
                    image_fingerprint TEXT NOT NULL DEFAULT '',
                    image_present INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    posted_at REAL NOT NULL DEFAULT 0,
                    returned_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    batch_id TEXT NOT NULL DEFAULT '',
                    aux_on_us TEXT NOT NULL DEFAULT ''
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

    def _write(self, conn: sqlite3.Connection, row: InboundIcl) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO inbounds (
                inbound_id, ece, userid, internal_account, amount, payor_aba,
                bofd_aba, payee_name, drawer_account, serial, type_code, status,
                value_date, actor, releaser, ofac_hit, ofac_match, return_reason,
                return_record, image_fingerprint, image_present, created_at,
                updated_at, posted_at, returned_at, note, batch_id, aux_on_us
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.inbound_id, row.ece, row.userid, row.internal_account, row.amount,
                row.payor_aba, row.bofd_aba, row.payee_name, row.drawer_account,
                row.serial, row.type_code, row.status, row.value_date, row.actor,
                row.releaser, int(row.ofac_hit), row.ofac_match, row.return_reason,
                row.return_record, row.image_fingerprint, int(row.image_present),
                row.created_at, row.updated_at, row.posted_at, row.returned_at,
                row.note, row.batch_id, row.aux_on_us,
            ),
        )

    def put(self, row: InboundIcl) -> None:
        with self._lock, self._connect() as conn:
            self._write(conn, row)
            conn.commit()

    def update(self, row: InboundIcl) -> None:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT inbound_id FROM inbounds WHERE inbound_id = ?', (row.inbound_id,),
            ).fetchone()
            if existing is None:
                raise IclError('inbound_not_found', 'Inbound ICL item not found.')
            self._write(conn, row)
            conn.commit()

    def get(self, inbound_id: str) -> Optional[InboundIcl]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def get_by_ece(self, ece: str) -> Optional[InboundIcl]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE ece = ?', (ece,)).fetchone()
        return _from_row(row) if row is not None else None

    def list_for(self, userid: str) -> List[InboundIcl]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE userid = ? ORDER BY created_at DESC',
                (userid,),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_unmatched(self) -> List[InboundIcl]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE status = ? ORDER BY created_at DESC',
                (IN_UNMATCHED,),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_open(self) -> List[InboundIcl]:
        with self._lock, self._connect() as conn:
            placeholders = ','.join('?' for _ in OPEN_INBOUNDS)
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE status IN (%s) ORDER BY created_at' % placeholders,
                tuple(OPEN_INBOUNDS),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_all(self) -> List[InboundIcl]:
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


class IclService:
    def __init__(
        self,
        policy: IclPolicy,
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
            raise IclError('icl_disabled', 'Inbound Check21 is disabled.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise IclError('icl_forbidden', 'Staff only.')

    def _require_view(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_view:
            raise IclError('icl_forbidden', 'Customers cannot view inbound Check21 items.')

    def _require_customer_return(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_return:
            raise IclError('icl_forbidden', 'Customers cannot request Check21 returns.')

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
            raise IclError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise IclError('credit_not_allowed', 'Credit accounts cannot pay Check21 items.')

    def _assert_amount(self, dollars: Decimal) -> None:
        if dollars < self.policy.min_amount or dollars > self.policy.max_amount:
            if dollars > self.policy.max_amount:
                raise IclError('icl_amount_exceeded', 'Check21 presentment exceeds the item cap.')
            raise IclError('amount_out_of_range', 'Amount is outside the allowed range.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def _assert_receiver(self, payor_aba: str) -> None:
        if payor_aba != self.policy.receiver_aba:
            raise IclError('wrong_receiver', 'Item is not drawn on this bank.')

    def _value_date_as_date(self, value_date: str) -> date:
        text = str(value_date or '')
        if len(text) == 8 and text.isdigit():
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        return date.fromisoformat(text[:10])

    def _return_window_open(self, row: InboundIcl, now: float) -> bool:
        posted = self._value_date_as_date(row.value_date)
        deadline = self.calendar.next_business_day(posted)
        today = self.calendar.local_dt(now).date()
        return today <= deadline

    def get_inbound(self, *, inbound_id: str, actor: str, actor_type: str) -> InboundIcl:
        self._require_view(actor_type)
        row = self.store.get(inbound_id)
        if row is None:
            raise IclError('inbound_not_found', 'Inbound ICL item not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise IclError('icl_forbidden', 'Not allowed to view this inbound item.')
        return row

    def preview_message(self, values: Dict[str, Any]) -> Dict[str, Any]:
        self._require_enabled()
        message = message_from_values(values)
        if message['type_code'] != TYPE_FORWARD:
            raise IclError('invalid_type', 'Only Type 25 presentment can be ingested.')
        dollars = parse_money(message['amount'])
        self._assert_amount(dollars)
        self._assert_receiver(message['payor_aba'])
        ofac = self._screen(message['payee_name'])
        try:
            account = normalize_account(message['drawer_account'])
        except AccountError:
            account = ''
        userid = self._lookup(account) if account else None
        now = float(self.clock())
        return {
            'message': {
                'ece': mask_ece(message['ece']),
                'amount': message['amount'],
                'payor_aba': message['payor_aba'],
                'bofd_aba': message['bofd_aba'],
                'payee_name': message['payee_name'],
                'drawer_last4': last4(message['drawer_account']),
                'serial': message['serial'],
                'type_code': message['type_code'],
                'image_present': bool(message['image_fingerprint']),
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

    def _debit(self, row: InboundIcl) -> str:
        if self.debit_fn is None:
            return 'ok'
        remark = 'icl %s' % (row.ece[-12:])
        result = self.debit_fn(row.internal_account, row.amount, remark)
        return _classify_money_result(result)

    def _credit(self, row: InboundIcl) -> str:
        if self.credit_fn is None:
            return 'ok'
        remark = 'icl return %s' % (row.ece[-12:])
        result = self.credit_fn(row.internal_account, row.amount, remark)
        return _classify_money_result(result)

    def _try_post(self, row: InboundIcl) -> InboundIcl:
        classified = self._debit(row)
        now = float(self.clock())
        if classified == 'ok':
            row.status = IN_POSTED
            row.posted_at = now
            row.updated_at = now
            self.store.update(row)
            return row
        if classified == 'nsf':
            row.status = IN_FAILED
            row.note = 'nsf'
            row.updated_at = now
            self.store.update(row)
            raise IclError('nsf', 'Insufficient funds to pay this Check21 item.', inbound=row)
        row.status = IN_FAILED
        row.note = 'debit_failed'
        row.updated_at = now
        self.store.update(row)
        raise IclError('failed', 'Inbound Check21 debit failed.', inbound=row)

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        values: Dict[str, Any],
        batch_id: str = '',
    ) -> Tuple[InboundIcl, bool]:
        self._require_staff(actor_type)
        message = message_from_values(values)
        if message['type_code'] != TYPE_FORWARD:
            raise IclError('invalid_type', 'Only Type 25 presentment can be ingested.')
        dollars = parse_money(message['amount'])
        self._assert_amount(dollars)
        self._assert_receiver(message['payor_aba'])
        existing = self.store.get_by_ece(message['ece'])
        if existing is not None:
            return existing, False
        if len(self.store.list_all()) >= self.policy.max_inbounds:
            raise IclError('inbound_limit', 'Inbound ICL limit reached.')
        try:
            account = normalize_account(message['drawer_account'])
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
        ofac = self._screen(message['payee_name'])
        now = float(self.clock())
        status = self._evaluate_status(
            userid=userid or '',
            account=account,
            dollars=dollars,
            ofac=ofac,
        )
        if credit_blocked:
            status = IN_UNMATCHED
        try:
            payee_name = normalize_legal_name(message['payee_name'])
        except WireError:
            payee_name = normalize_party(message['payee_name'])[:80] or 'PAYEE'
        row = InboundIcl(
            inbound_id=uuid.uuid4().hex,
            ece=message['ece'],
            userid=userid or '',
            internal_account=account,
            amount=money_str(dollars),
            payor_aba=message['payor_aba'],
            bofd_aba=message['bofd_aba'],
            payee_name=payee_name,
            drawer_account=message['drawer_account'],
            serial=message['serial'],
            type_code=message['type_code'],
            status=status if status != IN_POSTED else IN_QUEUED,
            value_date=self.calendar.cycle_date(now),
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            return_reason='',
            return_record='',
            image_fingerprint=message['image_fingerprint'],
            image_present=1 if message['image_fingerprint'] else 0,
            created_at=now,
            updated_at=now,
            note='credit_not_allowed' if credit_blocked else '',
            batch_id=batch_id or normalize_id(values.get('batch_id') if values.get('batch_id') else ''),
            aux_on_us=message.get('aux_on_us') or '',
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
    ) -> Dict[str, Any]:
        self._require_staff(actor_type)
        messages = split_x937_file(text)
        if not messages:
            raise IclError('invalid_file', 'Operator file has no X9.37 check details.')
        batch_id = uuid.uuid4().hex
        accepted = []
        duplicates = []
        errors = []
        for raw in messages:
            try:
                row, created = self.ingest(
                    actor=actor,
                    actor_type=actor_type,
                    values={'file': raw, 'batch_id': batch_id},
                    batch_id=batch_id,
                )
                payload = row.to_dict()
                if created:
                    accepted.append(payload)
                else:
                    duplicates.append(payload)
            except IclError as exc:
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
    ) -> InboundIcl:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_UNMATCHED:
            raise IclError('not_assignable', 'Only unmatched Check21 items can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise IclError('missing_customer_id', 'Customer id is required.')
        account = normalize_account(internal_account or row.drawer_account)
        self._assert_internal_account(owner, account)
        row.userid = owner
        row.internal_account = account
        row.drawer_account = account
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
    ) -> InboundIcl:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_HELD:
            raise IclError('not_overridable', 'Only OFAC-held Check21 items can be overridden.')
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
    ) -> InboundIcl:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_PENDING:
            raise IclError('not_releasable', 'Check21 item is not waiting for dual-control.')
        if row.actor and row.actor == str(actor):
            raise IclError('same_approver', 'A different employee must release this item.')
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
    ) -> InboundIcl:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status not in OPEN_INBOUNDS:
            raise IclError('not_rejectable', 'Check21 item cannot be rejected.')
        code = normalize_return_reason(reason)
        row.status = IN_REJECTED
        row.return_reason = code
        row.return_record = compose_return_x937(row, reason=code)
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
    ) -> InboundIcl:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        return self._return(row, actor=actor, reason=reason, note=note, force_window=True)

    def request_return(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'maker',
        note: Any = '',
    ) -> InboundIcl:
        self._require_customer_return(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise IclError('icl_forbidden', 'Not allowed to return this Check21 item.')
        return self._return(row, actor=actor, reason=reason or 'maker', note=note, force_window=False)

    def _return(
        self,
        row: InboundIcl,
        *,
        actor: str,
        reason: Any,
        note: Any,
        force_window: bool,
    ) -> InboundIcl:
        if row.status in {IN_RETURNED, IN_REJECTED}:
            raise IclError('already_returned', 'Check21 item is already returned or rejected.')
        code = normalize_return_reason(reason, default='maker')
        now = float(self.clock())
        if row.status in RETURNABLE_BEFORE_POST:
            row.status = IN_RETURNED
            row.return_reason = code
            row.return_record = compose_return_x937(row, reason=code)
            row.note = normalize_note(note) or row.note
            row.actor = str(actor)
            row.returned_at = now
            row.updated_at = now
            self.store.update(row)
            return row
        if row.status != IN_POSTED:
            raise IclError('not_returnable', 'Check21 item cannot be returned.')
        if not force_window and not self._return_window_open(row, now):
            raise IclError('return_window_closed', 'Next-day Check21 return window has closed.')
        classified = self._credit(row)
        if classified == 'nsf':
            raise IclError('nsf', 'Insufficient funds to return this Check21 item.', inbound=row)
        if classified != 'ok':
            raise IclError('return_failed', 'Check21 return credit failed.', inbound=row)
        row.status = IN_RETURNED
        row.return_reason = code
        row.return_record = compose_return_x937(row, reason=code)
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.returned_at = now
        row.updated_at = now
        self.store.update(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[InboundIcl]:
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
            except IclError:
                continue
        return posted

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self._require_view(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor and actor != userid:
            raise IclError('icl_forbidden', 'Not allowed to view this inbound book.')
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


_SERVICE: Optional[IclService] = None


def set_service(service: Optional[IclService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[IclService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('ICL_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryIclStore()
    path = os.environ.get('ICL_DB', DEFAULT_STORE_PATH)
    return SqliteIclStore(path)


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
) -> IclService:
    if store is None:
        store = default_store()
    return IclService(
        IclPolicy.from_env(),
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
        'icl_forbidden': 403,
        'icl_disabled': 403,
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
        'invalid_ece': 400,
        'invalid_file': 400,
        'invalid_type': 400,
        'invalid_reason': 400,
        'invalid_serial': 400,
        'wrong_receiver': 400,
        'icl_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: IclError) -> Dict[str, Any]:
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
    except IclError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: IclService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Icls': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_unmatched(service: IclService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'icl_forbidden'}), 403
    return jsonify({'Icls': service.unmatched_snapshot()}), 200


def handle_preview(service: IclService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'icl_forbidden'}), 403
    values = request.get_json(silent=True) or {}

    def _run():
        preview = service.preview_message(values)
        return jsonify({'preview': preview}), 200

    return _handle_errors(_run)


def handle_ingest(service: IclService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'Inbound Check21 ingested' if created else 'Inbound Check21 already posted',
            'inbound': row.to_dict(),
            'Icls': service.snapshot(owner, actor=userid, actor_type=actor_type) if row.userid else service.unmatched_snapshot(),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_file(service: IclService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    text = values.get('file') or values.get('x937') or values.get('icl') or values.get('text')
    if not text:
        return jsonify({'message': 'Operator file is required', 'error': 'missing_file'}), 400

    def _run():
        result = service.ingest_file(actor=userid, actor_type=actor_type, text=text)
        result['Unmatched'] = service.unmatched_snapshot()
        return jsonify(result), 201 if result['accepted_count'] else 200

    return _handle_errors(_run)


def handle_assign(service: IclService):
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
            'message': 'Inbound Check21 assigned',
            'inbound': row.to_dict(),
            'Icls': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_action(service: IclService, action: str):
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
            message = 'Inbound Check21 released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Inbound Check21 rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Inbound Check21 returned'
        else:
            raise IclError('invalid_reason', 'Unknown inbound action.')
        owner = row.userid or userid
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'Icls': service.snapshot(owner, actor=userid, actor_type=actor_type) if row.userid else service.unmatched_snapshot(),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: IclService):
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
            reason=values.get('reason') or 'maker',
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Check21 return requested',
            'inbound': row.to_dict(),
            'Icls': service.snapshot(row.userid or userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: IclService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner if actor_type not in EMPLOYEE_ROLES else None)
    return jsonify({'Icls': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_icl_routes(app, service: IclService) -> None:
    @app.route('/listIcls', methods=['POST', 'GET'])
    def list_icls_route():
        return handle_list(service)

    @app.route('/listUnmatchedIcls', methods=['POST', 'GET'])
    def list_unmatched_icls_route():
        return handle_unmatched(service)

    @app.route('/previewIcl', methods=['POST', 'GET'])
    def preview_icl_route():
        return handle_preview(service)

    @app.route('/ingestIcl', methods=['POST', 'GET'])
    def ingest_icl_route():
        return handle_ingest(service)

    @app.route('/ingestIclFile', methods=['POST', 'GET'])
    def ingest_icl_file_route():
        return handle_ingest_file(service)

    @app.route('/assignIcl', methods=['POST', 'GET'])
    def assign_icl_route():
        return handle_assign(service)

    @app.route('/overrideIclOfac', methods=['POST', 'GET'])
    def override_icl_ofac_route():
        return _staff_action(service, 'override')

    @app.route('/releaseIcl', methods=['POST', 'GET'])
    def release_icl_route():
        return _staff_action(service, 'release')

    @app.route('/rejectIcl', methods=['POST', 'GET'])
    def reject_icl_route():
        return _staff_action(service, 'reject')

    @app.route('/returnIcl', methods=['POST', 'GET'])
    def return_icl_route():
        return _staff_action(service, 'return')

    @app.route('/requestIclReturn', methods=['POST', 'GET'])
    def request_icl_return_route():
        return handle_request_return(service)

    @app.route('/runDueIcls', methods=['POST', 'GET'])
    def run_due_icls_route():
        return handle_run_due(service)
