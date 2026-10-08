"""Outbound Check21 / X9.37 image cash letter origination (bank of first deposit).

Customers deposit checks drawn on OTHER banks. This bank (BOFD) credits the
depositor and originates an ANSI X9.37 cash letter to the payor. Independent
of in-app cashier cheque deposit (`/depositCheck`), domestic Fedwire
(PR #73), ACH linking (PR #68), and inbound paying-bank ICL presentment.
Existing `/fundTransfer`, `/withdrawAmount`, `/sendWire`, and
`/depositCheck` stay unchanged. `Customers.debit_request` /
`credit_request` still write `debited` / `direct deposited` unless a
remark is supplied here.

Foundations (reusable beyond this screen):
- X9.37 pipe parse / compose / file split (XML/DOCTYPE rejected)
- 15-digit ECE item-sequence uniqueness
- 10-digit X9.37 cents amount field
- MICR on-us account / serial parse
- Image-view fingerprint (SHA-256; never in snapshots)
- Type-31 return compose with X9.100-187 reason codes
- Payor ABA must not equal BOFD ABA (on-us items are not originated here)
- Fed business-day / Check21 14:00 ET cutoff clock (reused)
- OFAC-style payee screening (reused)
- Dual-control release for high-value deposits
- Trace-id idempotency

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Drawer account numbers, full ECE, image fingerprints, and raw X9.37 never
appear in to_dict / snapshots.
"""

from __future__ import annotations

import hashlib
import os
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
    compute_fee,
    last4,
    money_str,
    normalize_aba,
    normalize_account,
    normalize_id,
    normalize_legal_name,
    normalize_nickname,
    normalize_note,
    normalize_purpose,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

PROF_ACTIVE = 'active'
PROF_PAUSED = 'paused'
PROF_ARCHIVED = 'archived'
PROF_STATUSES = frozenset({PROF_ACTIVE, PROF_PAUSED, PROF_ARCHIVED})
OPEN_PROF = frozenset({PROF_ACTIVE, PROF_PAUSED})
OCL_HELD = 'held'
OCL_QUEUED = 'queued'
OCL_PENDING = 'pending_release'
OCL_SUBMITTED = 'submitted'
OCL_COMPLETED = 'completed'
OCL_RETURNED = 'returned'
OCL_REJECTED = 'rejected'
OCL_CANCELLED = 'cancelled'
OCL_RECALLED = 'recalled'
OCL_NSF = 'nsf'
OCL_FAILED = 'failed'
OCL_STATUSES = frozenset({
    OCL_HELD, OCL_QUEUED, OCL_PENDING, OCL_SUBMITTED, OCL_COMPLETED,
    OCL_RETURNED, OCL_REJECTED, OCL_CANCELLED, OCL_RECALLED, OCL_NSF, OCL_FAILED,
})
OPEN_OCLS = frozenset({OCL_HELD, OCL_QUEUED, OCL_PENDING, OCL_SUBMITTED})
CANCELABLE = frozenset({OCL_HELD, OCL_QUEUED, OCL_PENDING})
RETURNABLE = frozenset({OCL_SUBMITTED, OCL_COMPLETED})
PRESENTED = frozenset({OCL_HELD, OCL_QUEUED, OCL_PENDING, OCL_SUBMITTED, OCL_COMPLETED})
FEE_NONE = 'none'
FEE_COLLECTED = 'collected'
FEE_WAIVED = 'waived'
FEE_NSF = 'nsf'
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
DEFAULT_STORE_PATH = 'SystemLogs/ocl.sqlite'
DEFAULT_BOFD_ABA = '021000021'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)


class OclError(ValueError):
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
        raise OclError('invalid_file', 'XML/DOCTYPE ICL files are rejected.')


def compose_amount_field(amount: Decimal) -> str:
    """X9.37 Type 25 amount: 10-digit cents."""
    cents = int((amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN) * 100).to_integral_value())
    if cents < 0 or cents > 9999999999:
        raise OclError('invalid_amount', 'Amount cannot be encoded in X9.37.')
    return '%010d' % cents


def parse_amount_field(value: Any) -> Decimal:
    text = str(value or '').strip().replace(',', '').replace('$', '')
    if not text:
        raise OclError('invalid_amount', 'Amount is required.')
    if text.isdigit() and len(text) == 10:
        return (Decimal(text) / Decimal('100')).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    try:
        return parse_money(text)
    except AmountError as exc:
        raise OclError('invalid_amount', 'Invalid Check21 amount.') from exc


def compose_ece(cycle_date: str, sequence: int) -> str:
    """ECE institution item sequence: YYYYMMDD + 7-digit sequence (15 digits)."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise OclError('invalid_ece', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 9999999:
        raise OclError('invalid_ece', 'ECE sequence out of range.')
    return '%s%07d' % (day, seq)


def normalize_ece(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 15:
        raise OclError('invalid_ece', 'ECE item sequence must be 15 digits.')
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
            raise OclError('invalid_serial', 'Check serial is required.')
        return ''
    if len(digits) > 10:
        raise OclError('invalid_serial', 'Check serial must be 1-10 digits.')
    return digits


def parse_onus(value: Any) -> Tuple[str, str]:
    """MICR On-Us: account[/serial]."""
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
        raise OclError('invalid_type', 'Type must be 25 (presentment) or 31 (return).')
    return mapped


def normalize_return_reason(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RETURN_ALIASES.get(text, text)
    if text not in RETURN_REASONS:
        raise OclError('invalid_reason', 'Unknown Check21 return reason.')
    return text


def _split_record(line: str) -> List[str]:
    return [part.strip() for part in str(line or '').split('|')]


def parse_x937(text: Any) -> Dict[str, str]:
    raw = str(text or '')
    _reject_xml(raw)
    lines = [line.strip() for line in raw.replace('\r\n', '\n').replace('\r', '\n').split('\n') if line.strip()]
    if not lines:
        raise OclError('invalid_file', 'X9.37 message is empty.')
    fields: Dict[str, str] = {}
    found_detail = False
    for line in lines:
        parts = _split_record(line)
        rtype = parts[0] if parts else ''
        if rtype in HEADER_TYPES or rtype in CONTROL_TYPES:
            continue
        if rtype == TYPE_RETURN:
            raise OclError('invalid_type', 'Forward ICL origination does not accept Type 31 as a presentment.')
        if rtype == TYPE_FORWARD:
            found_detail = True
            if len(parts) < 8:
                raise OclError('invalid_file', 'Type 25 check detail is truncated.')
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
        raise OclError('invalid_file', 'X9.37 message has no Type 25 check detail.')
    return fields


def parse_return_x937(text: Any) -> Dict[str, str]:
    raw = str(text or '')
    _reject_xml(raw)
    lines = [line.strip() for line in raw.replace('\r\n', '\n').replace('\r', '\n').split('\n') if line.strip()]
    if not lines:
        raise OclError('invalid_file', 'X9.37 return is empty.')
    for line in lines:
        parts = _split_record(line)
        if not parts or parts[0] != TYPE_RETURN:
            continue
        if len(parts) < 4:
            raise OclError('invalid_file', 'Type 31 return is truncated.')
        code = str(parts[3] or '').strip().upper()
        reason_map = {letter: name for name, letter in RETURN_X9.items()}
        reason = reason_map.get(code, str(parts[8] if len(parts) > 8 else 'other').strip().lower() or 'other')
        if reason not in RETURN_REASONS:
            reason = 'other'
        return {
            'ece': parts[2] if len(parts) > 2 else '',
            'reason_code': code,
            'reason': reason,
            'payor_aba': parts[4] if len(parts) > 4 else '',
            'amount': parts[6] if len(parts) > 6 else '',
            'bofd_aba': parts[7] if len(parts) > 7 else '',
        }
    raise OclError('invalid_type', 'X9.37 return has no Type 31 record.')


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


def compose_return_x937(
    *,
    ece: str,
    payor_aba: str,
    drawer_last4: str,
    amount: Decimal,
    bofd_aba: str,
    reason: str,
) -> str:
    code = RETURN_X9.get(reason, 'O')
    return '|'.join([
        TYPE_RETURN,
        'Return',
        ece,
        code,
        payor_aba,
        drawer_last4,
        compose_amount_field(amount),
        bofd_aba,
        reason.upper(),
    ])


def message_from_x937(text: Any) -> Dict[str, Any]:
    fields = parse_x937(text)
    amount = parse_amount_field(fields.get('amount'))
    try:
        payor = normalize_aba(fields.get('payor_aba') or '')
    except WireError as exc:
        raise OclError('invalid_aba', exc.message) from exc
    bofd_raw = fields.get('bofd_aba') or ''
    try:
        bofd = normalize_aba(bofd_raw) if bofd_raw else payor
    except WireError as exc:
        raise OclError('invalid_aba', exc.message) from exc
    on_us, parsed_serial = parse_onus(fields.get('on_us'))
    serial = normalize_serial(fields.get('serial') or parsed_serial)
    ece = normalize_ece(fields.get('ece'))
    front = _truthy_flag(fields.get('front') or 'Y')
    rear = _truthy_flag(fields.get('rear') or 'Y')
    fingerprint = image_fingerprint(
        ece, front=front, rear=rear, payload=fields.get('image_payload') or '',
    ) if (front or rear or fields.get('image_payload')) else ''
    payee = str(fields.get('payee_name') or 'PAYEE').strip() or 'PAYEE'
    return {
        'ece': ece,
        'type_code': TYPE_FORWARD,
        'amount': money_str(amount),
        'payor_aba': payor,
        'bofd_aba': bofd,
        'drawer_account': on_us,
        'serial': serial,
        'aux_on_us': str(fields.get('aux_on_us') or '')[:15],
        'payee_name': payee,
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
            'payee_name': payee,
            'front': 'Y' if front else 'N',
            'rear': 'Y' if rear else 'N',
        }),
    }


@dataclass
class OclPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    allow_credit: bool = False
    max_profiles: int = 12
    max_outbounds: int = 240
    min_amount: Decimal = Decimal('0.01')
    max_amount: Decimal = Decimal('1000000.00')
    outbound_fee: Decimal = Decimal('1.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    cutoff_hour: int = 14
    tz_offset_hours: int = -4
    bofd_aba: str = DEFAULT_BOFD_ABA
    watchlist: Tuple[str, ...] = (
        'BLOCKED PERSON',
        'SANCTIONED ENTITY',
        'OFAC TESTNAME',
    )
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'OclPolicy':
        extra = _env_list('OCL_OFAC_LIST')
        watch = tuple(dict.fromkeys(cls.watchlist + extra))
        bofd = os.environ.get('OCL_BOFD_ABA') or DEFAULT_BOFD_ABA
        try:
            bofd = normalize_aba(bofd)
        except WireError:
            bofd = DEFAULT_BOFD_ABA
        return cls(
            enabled=_env_bool('OCL_ENABLED', True),
            customer_manage=_env_bool('OCL_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('OCL_CUSTOMER_SEND', True),
            allow_credit=_env_bool('OCL_ALLOW_CREDIT', False),
            max_profiles=max(1, _env_int('OCL_MAX_PROFILES', 12)),
            max_outbounds=max(1, _env_int('OCL_MAX', 240)),
            min_amount=_env_money('OCL_MIN_AMOUNT', '0.01'),
            max_amount=_env_money('OCL_MAX_AMOUNT', '1000000.00'),
            outbound_fee=_env_money('OCL_FEE', '1.00'),
            dual_control_threshold=_env_money('OCL_DUAL_CONTROL', '10000.00'),
            cutoff_hour=max(0, min(23, _env_int('OCL_CUTOFF_HOUR', 14))),
            tz_offset_hours=_env_int('OCL_TZ_OFFSET', -4),
            bofd_aba=bofd,
            watchlist=watch,
            extra_holidays=_env_list('OCL_HOLIDAYS'),
        )


@dataclass
class OclProfile:
    profile_id: str
    userid: str
    nickname: str
    payee_name: str
    payor_aba: str
    drawer_account: str
    serial: str
    aux_on_us: str
    default_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'profile_id': self.profile_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'payee_name': self.payee_name,
            'payor_aba': self.payor_aba,
            'drawer_last4': last4(self.drawer_account),
            'serial': self.serial,
            'aux_on_us': self.aux_on_us,
            'default_account': self.default_account,
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == PROF_ACTIVE,
            'paused': self.status == PROF_PAUSED,
            'archived': self.status == PROF_ARCHIVED,
        }


@dataclass
class OclOutbound:
    outbound_id: str
    trace_id: str
    profile_id: str
    userid: str
    internal_account: str
    amount: str
    fee: str
    fee_status: str
    nickname: str
    payee_name: str
    payor_aba: str
    bofd_aba: str
    drawer_last4: str
    serial: str
    aux_on_us: str
    purpose: str
    memo: str
    status: str
    ece: str
    image_present: int
    image_fingerprint: str
    raw_x937: str
    return_record: str
    value_date: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    created_at: float
    updated_at: float
    submitted_at: float = 0.0
    completed_at: float = 0.0
    recalled_at: float = 0.0
    note: str = ''
    reason: str = ''
    front: int = 1
    rear: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            'outbound_id': self.outbound_id,
            'trace_id': self.trace_id,
            'profile_id': self.profile_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'amount': self.amount,
            'fee': self.fee,
            'fee_status': self.fee_status,
            'nickname': self.nickname,
            'payee_name': self.payee_name,
            'payor_aba': self.payor_aba,
            'bofd_aba': self.bofd_aba,
            'drawer_last4': self.drawer_last4,
            'serial': self.serial,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'ece_masked': mask_ece(self.ece) if self.ece else '',
            'image_present': bool(self.image_present),
            'value_date': self.value_date,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'submitted_at': self.submitted_at,
            'completed_at': self.completed_at,
            'recalled_at': self.recalled_at,
            'note': self.note,
            'reason': self.reason,
            'held': self.status == OCL_HELD,
            'queued': self.status == OCL_QUEUED,
            'pending_release': self.status == OCL_PENDING,
            'submitted': self.status == OCL_SUBMITTED,
            'completed': self.status == OCL_COMPLETED,
            'returned': self.status == OCL_RETURNED,
            'cancelable': self.status in CANCELABLE,
            'returnable': self.status in RETURNABLE,
        }


def _clone_profile(row: OclProfile) -> OclProfile:
    return OclProfile(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_outbound(row: OclOutbound) -> OclOutbound:
    return OclOutbound(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _profile_from_row(row: Any) -> OclProfile:
    return OclProfile(
        profile_id=row['profile_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        payee_name=row['payee_name'],
        payor_aba=row['payor_aba'],
        drawer_account=row['drawer_account'],
        serial=row['serial'] or '',
        aux_on_us=row['aux_on_us'] or '',
        default_account=row['default_account'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
    )


def _outbound_from_row(row: Any) -> OclOutbound:
    return OclOutbound(
        outbound_id=row['outbound_id'],
        trace_id=row['trace_id'],
        profile_id=row['profile_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        amount=row['amount'],
        fee=row['fee'],
        fee_status=row['fee_status'],
        nickname=row['nickname'],
        payee_name=row['payee_name'],
        payor_aba=row['payor_aba'],
        bofd_aba=row['bofd_aba'],
        drawer_last4=row['drawer_last4'],
        serial=row['serial'] or '',
        aux_on_us=row['aux_on_us'] or '',
        purpose=row['purpose'],
        memo=row['memo'] or '',
        status=row['status'],
        ece=row['ece'] or '',
        image_present=int(row['image_present'] or 0),
        image_fingerprint=row['image_fingerprint'] or '',
        raw_x937=row['raw_x937'] or '',
        return_record=row['return_record'] or '',
        value_date=row['value_date'],
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        submitted_at=float(row['submitted_at'] or 0),
        completed_at=float(row['completed_at'] or 0),
        recalled_at=float(row['recalled_at'] or 0),
        note=row['note'] or '',
        reason=row['reason'] or '',
        front=int(row['front'] if 'front' in row.keys() else 1),
        rear=int(row['rear'] if 'rear' in row.keys() else 1),
    )


class MemoryOclStore:
    def __init__(self) -> None:
        self._profiles: Dict[str, OclProfile] = {}
        self._outbounds: Dict[str, OclOutbound] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_profile(self, row: OclProfile) -> None:
        with self._lock:
            self._profiles[row.profile_id] = row

    def get_profile(self, profile_id: str) -> Optional[OclProfile]:
        with self._lock:
            row = self._profiles.get(profile_id)
            return _clone_profile(row) if row else None

    def update_profile(self, row: OclProfile) -> None:
        with self._lock:
            self._profiles[row.profile_id] = row

    def list_profiles(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[OclProfile]:
        with self._lock:
            rows = [_clone_profile(row) for row in self._profiles.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != PROF_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_profile_by_nickname(self, userid: str, nickname: str) -> Optional[OclProfile]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._profiles.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_PROF:
                    return _clone_profile(row)
        return None

    def find_profile_by_fingerprint(self, userid: str, payor_aba: str, drawer_account: str, serial: str) -> Optional[OclProfile]:
        with self._lock:
            for row in self._profiles.values():
                if (
                    row.userid == userid
                    and row.payor_aba == payor_aba
                    and row.drawer_account == drawer_account
                    and row.serial == serial
                    and row.status in OPEN_PROF
                ):
                    return _clone_profile(row)
        return None

    def put_outbound(self, row: OclOutbound) -> OclOutbound:
        with self._lock:
            existing_id = self._by_trace.get(row.trace_id)
            if existing_id is not None:
                return self._outbounds[existing_id]
            self._outbounds[row.outbound_id] = row
            self._by_trace[row.trace_id] = row.outbound_id
            return row

    def update_outbound(self, row: OclOutbound) -> None:
        with self._lock:
            self._outbounds[row.outbound_id] = row

    def get_outbound(self, outbound_id: str) -> Optional[OclOutbound]:
        with self._lock:
            row = self._outbounds.get(outbound_id)
            return _clone_outbound(row) if row else None

    def get_outbound_by_trace(self, trace_id: str) -> Optional[OclOutbound]:
        with self._lock:
            outbound_id = self._by_trace.get(trace_id)
            return _clone_outbound(self._outbounds[outbound_id]) if outbound_id else None

    def get_outbound_by_ece(self, ece: str) -> Optional[OclOutbound]:
        if not ece:
            return None
        with self._lock:
            for row in self._outbounds.values():
                if row.ece == ece:
                    return _clone_outbound(row)
        return None

    def find_presented_micr(self, payor_aba: str, drawer_last4: str, serial: str) -> Optional[OclOutbound]:
        with self._lock:
            for row in self._outbounds.values():
                if (
                    row.payor_aba == payor_aba
                    and row.drawer_last4 == drawer_last4
                    and row.serial == serial
                    and row.status in PRESENTED
                ):
                    return _clone_outbound(row)
        return None

    def list_outbounds(self, userid: Optional[str] = None, profile_id: Optional[str] = None) -> List[OclOutbound]:
        with self._lock:
            rows = [_clone_outbound(row) for row in self._outbounds.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if profile_id is not None:
            rows = [row for row in rows if row.profile_id == profile_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def next_ece_sequence(self, cycle_date: str) -> int:
        with self._lock:
            used = [row.ece for row in self._outbounds.values() if row.ece.startswith(cycle_date)]
        return len(used) + 1


class SqliteOclStore:
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
                CREATE TABLE IF NOT EXISTS profiles (
                    profile_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    payee_name TEXT NOT NULL,
                    payor_aba TEXT NOT NULL,
                    drawer_account TEXT NOT NULL,
                    serial TEXT NOT NULL DEFAULT '',
                    aux_on_us TEXT NOT NULL DEFAULT '',
                    default_account TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbounds (
                    outbound_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    profile_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    internal_account TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    fee_status TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    payee_name TEXT NOT NULL,
                    payor_aba TEXT NOT NULL,
                    bofd_aba TEXT NOT NULL,
                    drawer_last4 TEXT NOT NULL,
                    serial TEXT NOT NULL DEFAULT '',
                    aux_on_us TEXT NOT NULL DEFAULT '',
                    purpose TEXT NOT NULL,
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    ece TEXT NOT NULL DEFAULT '',
                    image_present INTEGER NOT NULL DEFAULT 0,
                    image_fingerprint TEXT NOT NULL DEFAULT '',
                    raw_x937 TEXT NOT NULL DEFAULT '',
                    return_record TEXT NOT NULL DEFAULT '',
                    value_date TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    submitted_at REAL NOT NULL DEFAULT 0,
                    completed_at REAL NOT NULL DEFAULT 0,
                    recalled_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    front INTEGER NOT NULL DEFAULT 1,
                    rear INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.commit()

    def put_profile(self, row: OclProfile) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO profiles (
                    profile_id, userid, nickname, payee_name, payor_aba, drawer_account,
                    serial, aux_on_us, default_account, status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.profile_id, row.userid, row.nickname, row.payee_name, row.payor_aba,
                    row.drawer_account, row.serial, row.aux_on_us, row.default_account,
                    row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_profile(self, profile_id: str) -> Optional[OclProfile]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM profiles WHERE profile_id = ?', (profile_id,),
            ).fetchone()
        return _profile_from_row(row) if row else None

    def update_profile(self, row: OclProfile) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE profiles SET nickname=?, payee_name=?, payor_aba=?, drawer_account=?,
                    serial=?, aux_on_us=?, default_account=?, status=?, actor=?, updated_at=?
                WHERE profile_id=?
                """,
                (
                    row.nickname, row.payee_name, row.payor_aba, row.drawer_account,
                    row.serial, row.aux_on_us, row.default_account, row.status,
                    row.actor, row.updated_at, row.profile_id,
                ),
            )
            conn.commit()

    def list_profiles(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[OclProfile]:
        sql = 'SELECT * FROM profiles'
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
        return [_profile_from_row(row) for row in rows]

    def find_profile_by_nickname(self, userid: str, nickname: str) -> Optional[OclProfile]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM profiles
                WHERE userid = ? AND lower(nickname) = lower(?)
                  AND status IN ('active', 'paused')
                """,
                (userid, nickname),
            ).fetchone()
        return _profile_from_row(row) if row else None

    def find_profile_by_fingerprint(self, userid: str, payor_aba: str, drawer_account: str, serial: str) -> Optional[OclProfile]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM profiles
                WHERE userid = ? AND payor_aba = ? AND drawer_account = ? AND serial = ?
                  AND status IN ('active', 'paused')
                """,
                (userid, payor_aba, drawer_account, serial),
            ).fetchone()
        return _profile_from_row(row) if row else None

    def put_outbound(self, row: OclOutbound) -> OclOutbound:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM outbounds WHERE trace_id = ?', (row.trace_id,)
            ).fetchone()
            if existing is not None:
                return _outbound_from_row(existing)
            conn.execute(
                """
                INSERT INTO outbounds (
                    outbound_id, trace_id, profile_id, userid, internal_account,
                    amount, fee, fee_status, nickname, payee_name, payor_aba, bofd_aba,
                    drawer_last4, serial, aux_on_us, purpose, memo, status, ece,
                    image_present, image_fingerprint, raw_x937, return_record, value_date,
                    actor, releaser, ofac_hit, ofac_match, created_at, updated_at,
                    submitted_at, completed_at, recalled_at, note, reason, front, rear
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.outbound_id, row.trace_id, row.profile_id, row.userid,
                    row.internal_account, row.amount, row.fee, row.fee_status,
                    row.nickname, row.payee_name, row.payor_aba, row.bofd_aba,
                    row.drawer_last4, row.serial, row.aux_on_us, row.purpose, row.memo,
                    row.status, row.ece, row.image_present, row.image_fingerprint,
                    row.raw_x937, row.return_record, row.value_date, row.actor,
                    row.releaser, row.ofac_hit, row.ofac_match, row.created_at,
                    row.updated_at, row.submitted_at, row.completed_at, row.recalled_at,
                    row.note, row.reason, row.front, row.rear,
                ),
            )
            conn.commit()
            return row

    def update_outbound(self, row: OclOutbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE outbounds SET fee=?, fee_status=?, status=?, ece=?,
                    image_present=?, image_fingerprint=?, raw_x937=?, return_record=?,
                    value_date=?, actor=?, releaser=?, ofac_hit=?, ofac_match=?,
                    updated_at=?, submitted_at=?, completed_at=?, recalled_at=?,
                    note=?, reason=?
                WHERE outbound_id=?
                """,
                (
                    row.fee, row.fee_status, row.status, row.ece, row.image_present,
                    row.image_fingerprint, row.raw_x937, row.return_record, row.value_date,
                    row.actor, row.releaser, row.ofac_hit, row.ofac_match, row.updated_at,
                    row.submitted_at, row.completed_at, row.recalled_at, row.note,
                    row.reason, row.outbound_id,
                ),
            )
            conn.commit()

    def get_outbound(self, outbound_id: str) -> Optional[OclOutbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM outbounds WHERE outbound_id = ?', (outbound_id,)).fetchone()
        return _outbound_from_row(row) if row else None

    def get_outbound_by_trace(self, trace_id: str) -> Optional[OclOutbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM outbounds WHERE trace_id = ?', (trace_id,)).fetchone()
        return _outbound_from_row(row) if row else None

    def get_outbound_by_ece(self, ece: str) -> Optional[OclOutbound]:
        if not ece:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM outbounds WHERE ece = ?', (ece,)).fetchone()
        return _outbound_from_row(row) if row else None

    def find_presented_micr(self, payor_aba: str, drawer_last4: str, serial: str) -> Optional[OclOutbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM outbounds
                WHERE payor_aba = ? AND drawer_last4 = ? AND serial = ?
                  AND status IN ('held', 'queued', 'pending_release', 'submitted', 'completed')
                """,
                (payor_aba, drawer_last4, serial),
            ).fetchone()
        return _outbound_from_row(row) if row else None

    def list_outbounds(self, userid: Optional[str] = None, profile_id: Optional[str] = None) -> List[OclOutbound]:
        sql = 'SELECT * FROM outbounds'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if profile_id is not None:
            clauses.append('profile_id = ?')
            params.append(profile_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_outbound_from_row(row) for row in rows]

    def next_ece_sequence(self, cycle_date: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM outbounds WHERE ece LIKE ?",
                (cycle_date + '%',),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class OclService:
    def __init__(
        self,
        policy: OclPolicy,
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
            raise OclError('ocl_disabled', 'Check21 origination is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise OclError('ocl_forbidden', 'Customers cannot manage Check21 profiles.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise OclError('ocl_forbidden', 'Customers cannot originate Check21 items.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise OclError('ocl_forbidden', 'Staff only.')

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
            raise OclError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise OclError('credit_not_allowed', 'Credit accounts cannot receive Check21 deposits.')

    def _assert_amount(self, dollars: Decimal) -> None:
        if dollars < self.policy.min_amount or dollars > self.policy.max_amount:
            raise OclError('amount_out_of_range', 'Amount is outside the allowed range.')

    def _assert_payor(self, payor: str) -> None:
        if payor == self.policy.bofd_aba:
            raise OclError('on_us_not_allowed', 'Checks drawn on this bank cannot be originated as outbound ICL.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def add_profile(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        payee_name: Any,
        payor_aba: Any,
        drawer_account: Any,
        serial: Any = '',
        aux_on_us: Any = '',
        default_account: Any = None,
    ) -> OclProfile:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise OclError('ocl_forbidden', 'Not allowed to add Check21 profiles for this customer.')
        try:
            name = normalize_nickname(nickname)
            payee = normalize_legal_name(payee_name)
            payor = normalize_aba(payor_aba)
        except WireError as exc:
            raise OclError(exc.code, exc.message) from exc
        self._assert_payor(payor)
        on_us, parsed_serial = parse_onus(drawer_account)
        if not on_us:
            raise OclError('invalid_account', 'Drawer on-us account is required.')
        if len(on_us) < 4 or len(on_us) > 17:
            raise OclError('invalid_account', 'Drawer on-us account must be 4-17 digits.')
        serial_norm = normalize_serial(serial or parsed_serial)
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        if self.store.find_profile_by_nickname(owner_userid, name) is not None:
            raise OclError('profile_duplicate', 'A Check21 profile with that nickname already exists.')
        if self.store.find_profile_by_fingerprint(owner_userid, payor, on_us, serial_norm) is not None:
            raise OclError('profile_duplicate', 'That MICR item is already on file.')
        open_rows = [row for row in self.store.list_profiles(owner_userid) if row.status in OPEN_PROF]
        if len(open_rows) >= self.policy.max_profiles:
            raise OclError('profile_limit', 'Check21 profile limit reached.')
        now = float(self.clock())
        row = OclProfile(
            profile_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            payee_name=payee,
            payor_aba=payor,
            drawer_account=on_us,
            serial=serial_norm,
            aux_on_us=str(aux_on_us or '')[:15],
            default_account=account,
            status=PROF_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_profile(row)
        return row

    def get_profile(self, *, profile_id: str, actor: str, actor_type: str) -> OclProfile:
        self._require_enabled()
        row = self.store.get_profile(profile_id)
        if row is None:
            raise OclError('profile_not_found', 'Check21 profile not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise OclError('ocl_forbidden', 'Not allowed to view this profile.')
        return row

    def enforce_profile(
        self,
        *,
        profile_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> OclProfile:
        row = self.get_profile(profile_id=profile_id, actor=actor, actor_type=actor_type)
        if row.status == PROF_ARCHIVED:
            raise OclError('already_archived', 'Profile is archived.')
        if row.status == PROF_PAUSED:
            raise OclError('profile_paused', 'Profile is paused.')
        if require_active and row.status != PROF_ACTIVE:
            raise OclError('invalid_status', 'Profile is not active.')
        return row

    def set_profile_status(
        self,
        *,
        profile_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> OclProfile:
        self._require_manage(actor_type)
        row = self.get_profile(profile_id=profile_id, actor=actor, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': PROF_PAUSED, 'hold': PROF_PAUSED,
            'resume': PROF_ACTIVE, 'activate': PROF_ACTIVE, 'unpause': PROF_ACTIVE,
            'archive': PROF_ARCHIVED, 'close': PROF_ARCHIVED, 'cancel': PROF_ARCHIVED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {PROF_PAUSED, PROF_ACTIVE, PROF_ARCHIVED}:
            raise OclError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == PROF_ARCHIVED:
            raise OclError('already_archived', 'Profile is already archived.')
        if wanted == PROF_PAUSED:
            if row.status == PROF_PAUSED:
                raise OclError('already_paused', 'Profile is already paused.')
            if row.status != PROF_ACTIVE:
                raise OclError('invalid_status', 'Only an active profile can be paused.')
        elif wanted == PROF_ACTIVE:
            if row.status == PROF_ACTIVE:
                raise OclError('already_active', 'Profile is already active.')
            if row.status != PROF_PAUSED:
                raise OclError('invalid_status', 'Only a paused profile can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_profile(row)
        return row

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        profile_id: Any,
        amount: Any,
        internal_account: Any = None,
        waive_fee: bool = False,
        front: Any = True,
        rear: Any = True,
    ) -> Dict[str, Any]:
        self._require_send(actor_type)
        profile = self.enforce_profile(
            profile_id=str(profile_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if profile.userid != owner_userid:
            raise OclError('ocl_forbidden', 'Profile does not belong to this customer.')
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        account = normalize_account(internal_account or profile.default_account)
        self._assert_internal_account(owner_userid, account)
        fee = compute_fee(dollars, self.policy.outbound_fee, waived=bool(waive_fee) and actor_type in EMPLOYEE_ROLES)
        now = float(self.clock())
        ofac = self._screen(profile.payee_name, aliases=(profile.nickname,))
        clock = self.calendar.snapshot(now)
        return {
            'amount': money_str(dollars),
            'amount_field': compose_amount_field(dollars),
            'fee': money_str(fee),
            'credit': money_str(dollars),
            'internal_account': account,
            'profile': profile.to_dict(),
            'bofd_aba': self.policy.bofd_aba,
            'front': _truthy_flag(front) if not isinstance(front, bool) else front,
            'rear': _truthy_flag(rear) if not isinstance(rear, bool) else rear,
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(dollars),
            'clock': clock,
        }

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        profile: OclProfile,
        account: str,
        dollars: Decimal,
        fee: Decimal,
        purpose: str,
        memo: str,
        trace_id: str,
        ofac: ScreenResult,
        waive_fee: bool,
        front: bool,
        rear: bool,
    ) -> OclOutbound:
        now = float(self.clock())
        value = self.calendar.cycle_date(now)
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        if ofac.hit:
            status = OCL_HELD
        elif self._needs_dual_control(dollars):
            status = OCL_PENDING
        elif self.calendar.snapshot(now)['after_cutoff']:
            status = OCL_QUEUED
        else:
            status = OCL_SUBMITTED
        item = OclOutbound(
            outbound_id=uuid.uuid4().hex,
            trace_id=trace_id,
            profile_id=profile.profile_id,
            userid=owner_userid,
            internal_account=account,
            amount=money_str(dollars),
            fee=money_str(fee),
            fee_status=fee_status,
            nickname=profile.nickname,
            payee_name=profile.payee_name,
            payor_aba=profile.payor_aba,
            bofd_aba=self.policy.bofd_aba,
            drawer_last4=last4(profile.drawer_account),
            serial=profile.serial,
            aux_on_us=profile.aux_on_us,
            purpose=purpose,
            memo=memo,
            status=status,
            ece='',
            image_present=1 if (front or rear) else 0,
            image_fingerprint='',
            raw_x937='',
            return_record='',
            value_date=value,
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
            front=1 if front else 0,
            rear=1 if rear else 0,
        )
        if status == OCL_SUBMITTED:
            self._transmit(item, actor=actor, drawer_account=profile.drawer_account)
        return item

    def _transmit(self, item: OclOutbound, *, actor: str, drawer_account: str = '') -> OclOutbound:
        now = float(self.clock())
        cycle = item.value_date or self.calendar.cycle_date(now)
        seq = self.store.next_ece_sequence(cycle)
        ece = compose_ece(cycle, seq)
        dollars = parse_money(item.amount)
        fee = parse_money(item.fee, allow_zero=True)
        remark = 'icl from %s' % item.nickname
        status = OCL_SUBMITTED
        fail_note = ''
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(item.internal_account, money_str(dollars), remark)
            except Exception as exc:
                status = OCL_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = OCL_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = OCL_FAILED
                    fail_note = str(result)[:240]
        if status == OCL_SUBMITTED and fee > 0 and item.fee_status != FEE_WAIVED and self.debit_fn is not None:
            try:
                fee_result = self.debit_fn(item.internal_account, money_str(fee), 'icl fee %s' % ece[-12:])
            except Exception:
                item.fee_status = FEE_NSF
            else:
                kind = _classify_money_result(fee_result)
                item.fee_status = FEE_COLLECTED if kind == 'ok' else FEE_NSF
        elif status == OCL_SUBMITTED and (fee == 0 or item.fee_status == FEE_WAIVED):
            item.fee_status = FEE_WAIVED if item.fee_status == FEE_WAIVED or fee == 0 else item.fee_status
        item.status = status
        item.updated_at = now
        if status == OCL_SUBMITTED:
            item.ece = ece
            item.submitted_at = now
            item.releaser = str(actor)
            front = bool(item.front)
            rear = bool(item.rear)
            item.image_fingerprint = image_fingerprint(ece, front=front, rear=rear) if item.image_present else ''
            on_us = drawer_account
            if not on_us:
                profile = self.store.get_profile(item.profile_id)
                on_us = profile.drawer_account if profile else ''
            item.raw_x937 = compose_x937({
                'ece': ece,
                'aux_on_us': item.aux_on_us,
                'payor_aba': item.payor_aba,
                'on_us': on_us,
                'serial': item.serial,
                'amount': compose_amount_field(dollars),
                'bofd_aba': item.bofd_aba,
                'payee_name': item.payee_name,
                'front': 'Y' if front else 'N',
                'rear': 'Y' if rear else 'N',
            }, with_image=bool(item.image_present))
        else:
            item.ece = ''
            item.note = fail_note
        return item

    def originate(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        profile_id: Any,
        amount: Any,
        internal_account: Any = None,
        purpose: Any = 'other',
        memo: Any = '',
        trace_id: Any = None,
        waive_fee: bool = False,
        front: Any = True,
        rear: Any = True,
    ) -> Tuple[OclOutbound, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise OclError('ocl_forbidden', 'Not allowed to originate Check21 items for this customer.')
        profile = self.enforce_profile(
            profile_id=str(profile_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if profile.userid != owner_userid:
            raise OclError('ocl_forbidden', 'Profile does not belong to this customer.')
        self._assert_payor(profile.payor_aba)
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        account = normalize_account(internal_account or profile.default_account)
        self._assert_internal_account(owner_userid, account)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(dollars, self.policy.outbound_fee, waived=staff_waive)
        trace = normalize_id(trace_id)
        existing = self.store.get_outbound_by_trace(trace)
        if existing is not None:
            return existing, False
        if len(self.store.list_outbounds(owner_userid)) >= self.policy.max_outbounds:
            raise OclError('outbound_limit', 'Check21 origination history limit reached.')
        presented = self.store.find_presented_micr(profile.payor_aba, last4(profile.drawer_account), profile.serial)
        if presented is not None and profile.serial:
            raise OclError('already_presented', 'That MICR item is already in presentment.')
        ofac = self._screen(profile.payee_name, aliases=(profile.nickname,))
        front_flag = _truthy_flag(front) if not isinstance(front, bool) else bool(front)
        rear_flag = _truthy_flag(rear) if not isinstance(rear, bool) else bool(rear)
        item = self._place(
            owner_userid=owner_userid,
            actor=actor,
            profile=profile,
            account=account,
            dollars=dollars,
            fee=fee,
            purpose=normalize_purpose(purpose),
            memo=normalize_note(memo, limit=140),
            trace_id=trace,
            ofac=ofac,
            waive_fee=staff_waive,
            front=front_flag,
            rear=rear_flag,
        )
        stored = self.store.put_outbound(item)
        if stored.outbound_id != item.outbound_id:
            return stored, False
        if stored.status == OCL_NSF:
            raise OclError('nsf', 'Deposit credit did not complete.', outbound=stored)
        if stored.status == OCL_FAILED:
            raise OclError('failed', 'Check21 credit did not complete.', outbound=stored)
        return stored, True

    def get_outbound(self, *, outbound_id: str, actor: str, actor_type: str) -> OclOutbound:
        self._require_enabled()
        row = self.store.get_outbound(outbound_id)
        if row is None:
            raise OclError('outbound_not_found', 'Check21 item not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise OclError('ocl_forbidden', 'Not allowed to view this item.')
        return row

    def cancel_outbound(
        self,
        *,
        outbound_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> OclOutbound:
        self._require_send(actor_type)
        item = self.get_outbound(outbound_id=outbound_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and item.userid != actor:
            raise OclError('ocl_forbidden', 'Not allowed to cancel this item.')
        if item.status not in CANCELABLE:
            raise OclError('not_cancelable', 'Only held, queued, or pending items can be cancelled.')
        item.status = OCL_CANCELLED
        item.actor = str(actor)
        item.updated_at = float(self.clock())
        item.note = normalize_note(note)
        self.store.update_outbound(item)
        return item

    def waive_fee(
        self,
        *,
        outbound_id: str,
        actor: str,
        actor_type: str,
    ) -> OclOutbound:
        self._require_staff(actor_type)
        item = self.get_outbound(outbound_id=outbound_id, actor=actor, actor_type=actor_type)
        if item.status not in CANCELABLE:
            raise OclError('invalid_status', 'Fee can only be waived before the item is submitted.')
        item.fee = money_str(Decimal('0.00'))
        item.fee_status = FEE_WAIVED
        item.actor = str(actor)
        item.updated_at = float(self.clock())
        self.store.update_outbound(item)
        return item

    def override_ofac(
        self,
        *,
        outbound_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> OclOutbound:
        self._require_staff(actor_type)
        item = self.get_outbound(outbound_id=outbound_id, actor=actor, actor_type=actor_type)
        if item.status != OCL_HELD:
            raise OclError('invalid_status', 'Only an OFAC hold can be overridden.')
        now = float(self.clock())
        item.ofac_hit = 0
        item.note = normalize_note(note) or 'ofac override'
        item.actor = str(actor)
        item.updated_at = now
        dollars = parse_money(item.amount)
        if self._needs_dual_control(dollars):
            item.status = OCL_PENDING
        elif self.calendar.snapshot(now)['after_cutoff']:
            item.status = OCL_QUEUED
            item.value_date = self.calendar.cycle_date(now)
        else:
            profile = self.store.get_profile(item.profile_id)
            self._transmit(item, actor=actor, drawer_account=profile.drawer_account if profile else '')
        self.store.update_outbound(item)
        if item.status == OCL_NSF:
            raise OclError('nsf', 'Deposit credit did not complete.', outbound=item)
        if item.status == OCL_FAILED:
            raise OclError('failed', 'Check21 credit did not complete.', outbound=item)
        return item

    def release_outbound(
        self,
        *,
        outbound_id: str,
        actor: str,
        actor_type: str,
    ) -> OclOutbound:
        self._require_staff(actor_type)
        item = self.get_outbound(outbound_id=outbound_id, actor=actor, actor_type=actor_type)
        if item.status == OCL_HELD:
            raise OclError('ofac_hold', 'OFAC hold must be overridden before release.')
        if item.status not in {OCL_PENDING, OCL_QUEUED}:
            raise OclError('not_releasable', 'Only queued or pending items can be released.')
        if (
            item.status == OCL_PENDING
            and item.actor
            and str(actor) == str(item.actor)
            and parse_money(item.amount) >= self.policy.dual_control_threshold
        ):
            raise OclError('same_approver', 'A different employee must release this item.')
        now = float(self.clock())
        if item.status == OCL_QUEUED or self.calendar.snapshot(now)['after_cutoff']:
            if self.calendar.snapshot(now)['after_cutoff'] and item.status != OCL_PENDING:
                item.status = OCL_QUEUED
                item.value_date = self.calendar.cycle_date(now)
                item.updated_at = now
                self.store.update_outbound(item)
                return item
        profile = self.store.get_profile(item.profile_id)
        self._transmit(item, actor=actor, drawer_account=profile.drawer_account if profile else '')
        self.store.update_outbound(item)
        if item.status == OCL_NSF:
            raise OclError('nsf', 'Deposit credit did not complete.', outbound=item)
        if item.status == OCL_FAILED:
            raise OclError('failed', 'Check21 credit did not complete.', outbound=item)
        return item

    def reject_outbound(
        self,
        *,
        outbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> OclOutbound:
        self._require_staff(actor_type)
        item = self.get_outbound(outbound_id=outbound_id, actor=actor, actor_type=actor_type)
        if item.status not in CANCELABLE:
            raise OclError('not_rejectable', 'Only held, queued, or pending items can be rejected.')
        item.status = OCL_REJECTED
        item.reason = normalize_note(reason, limit=40)
        item.note = normalize_note(note)
        item.actor = str(actor)
        item.updated_at = float(self.clock())
        self.store.update_outbound(item)
        return item

    def complete_outbound(
        self,
        *,
        outbound_id: str,
        actor: str,
        actor_type: str,
    ) -> OclOutbound:
        self._require_staff(actor_type)
        item = self.get_outbound(outbound_id=outbound_id, actor=actor, actor_type=actor_type)
        if item.status == OCL_COMPLETED:
            raise OclError('already_completed', 'Item is already completed.')
        if item.status != OCL_SUBMITTED:
            raise OclError('not_completable', 'Only submitted items can be completed.')
        now = float(self.clock())
        item.status = OCL_COMPLETED
        item.completed_at = now
        item.updated_at = now
        item.actor = str(actor)
        self.store.update_outbound(item)
        return item

    def recall_outbound(
        self,
        *,
        outbound_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> OclOutbound:
        self._require_staff(actor_type)
        item = self.get_outbound(outbound_id=outbound_id, actor=actor, actor_type=actor_type)
        if item.status == OCL_RECALLED:
            raise OclError('already_recalled', 'Item is already recalled.')
        if item.status == OCL_COMPLETED:
            raise OclError('already_completed', 'Completed items cannot be recalled; apply a Type 31 return.')
        if item.status != OCL_SUBMITTED:
            raise OclError('not_recallable', 'Only submitted items can be recalled.')
        remark = normalize_note(note) or ('icl recalled from %s' % item.nickname)
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(item.internal_account, item.amount, remark)
            except Exception as exc:
                raise OclError('recall_failed', 'Recall debit failed.', outbound=item) from exc
            if _classify_money_result(result) != 'ok':
                raise OclError('recall_failed', 'Recall debit failed.', outbound=item)
            fee = parse_money(item.fee, allow_zero=True)
            if fee > 0 and item.fee_status == FEE_COLLECTED and self.credit_fn is not None:
                self.credit_fn(item.internal_account, money_str(fee), 'icl fee recalled %s' % (item.ece[-12:] if item.ece else item.trace_id[:12]))
        now = float(self.clock())
        item.status = OCL_RECALLED
        item.recalled_at = now
        item.updated_at = now
        item.actor = str(actor)
        item.note = remark
        self.store.update_outbound(item)
        return item

    def return_outbound(
        self,
        *,
        outbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'nsf',
        file: Any = None,
        note: Any = '',
    ) -> OclOutbound:
        self._require_staff(actor_type)
        item = self.get_outbound(outbound_id=outbound_id, actor=actor, actor_type=actor_type)
        if item.status == OCL_RETURNED:
            raise OclError('already_returned', 'Item is already returned.')
        if item.status not in RETURNABLE:
            raise OclError('not_returnable', 'Only submitted or completed items can be returned.')
        if file:
            parsed = parse_return_x937(file)
            reason_norm = normalize_return_reason(parsed.get('reason') or reason)
            if parsed.get('ece') and item.ece and normalize_ece(parsed['ece']) != item.ece:
                raise OclError('invalid_ece', 'Return ECE does not match this item.')
        else:
            reason_norm = normalize_return_reason(reason)
        dollars = parse_money(item.amount)
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(
                    item.internal_account,
                    money_str(dollars),
                    'icl return %s' % (item.ece[-12:] if item.ece else item.trace_id[:12]),
                )
            except Exception as exc:
                raise OclError('return_failed', 'Return debit failed.', outbound=item) from exc
            kind = _classify_money_result(result)
            if kind == 'nsf':
                raise OclError('nsf', 'Insufficient funds to claw back the deposit.', outbound=item)
            if kind != 'ok':
                raise OclError('return_failed', 'Return debit failed.', outbound=item)
        now = float(self.clock())
        item.status = OCL_RETURNED
        item.reason = reason_norm
        item.note = normalize_note(note) or reason_norm
        item.return_record = compose_return_x937(
            ece=item.ece,
            payor_aba=item.payor_aba,
            drawer_last4=item.drawer_last4,
            amount=dollars,
            bofd_aba=item.bofd_aba,
            reason=reason_norm,
        )
        item.updated_at = now
        item.actor = str(actor)
        self.store.update_outbound(item)
        return item

    def export_file(self, userid: Optional[str] = None) -> str:
        chunks = []
        for row in self.store.list_outbounds(userid):
            if row.status in {OCL_SUBMITTED, OCL_COMPLETED} and row.raw_x937:
                chunks.append(row.raw_x937)
        return '\n'.join(chunks)

    def run_due(self, userid: Optional[str] = None) -> List[OclOutbound]:
        now = float(self.clock())
        today = self.calendar.local_dt(now).date().strftime('%Y%m%d')
        after = self.calendar.snapshot(now)['after_cutoff']
        changed: List[OclOutbound] = []
        for item in self.store.list_outbounds(userid):
            if item.status != OCL_QUEUED:
                continue
            if item.value_date > today:
                continue
            if after and item.value_date == today:
                continue
            dollars = parse_money(item.amount)
            if self._needs_dual_control(dollars):
                item.status = OCL_PENDING
                item.updated_at = now
                self.store.update_outbound(item)
                changed.append(item)
                continue
            profile = self.store.get_profile(item.profile_id)
            self._transmit(item, actor=item.actor, drawer_account=profile.drawer_account if profile else '')
            self.store.update_outbound(item)
            changed.append(item)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.run_due(userid)
        profiles = self.store.list_profiles(userid)
        outbounds = self.store.list_outbounds(userid)
        posted_ytd = Decimal('0.00')
        fee_ytd = Decimal('0.00')
        returned_ytd = Decimal('0.00')
        for row in outbounds:
            amount = parse_money(row.amount, allow_zero=True)
            if row.status in {OCL_SUBMITTED, OCL_COMPLETED}:
                posted_ytd += amount
                if row.fee_status == FEE_COLLECTED:
                    fee_ytd += parse_money(row.fee, allow_zero=True)
            elif row.status in {OCL_RETURNED, OCL_RECALLED}:
                returned_ytd += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'bofd_aba': self.policy.bofd_aba,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'fee': money_str(self.policy.outbound_fee),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clock': self.calendar.snapshot(now),
            'profiles': [row.to_dict() for row in profiles[:40]],
            'outbounds': [row.to_dict() for row in outbounds[:40]],
            'ytd_posted': money_str(posted_ytd),
            'ytd_fees': money_str(fee_ytd),
            'ytd_returned': money_str(returned_ytd),
            'active_count': sum(1 for row in profiles if row.status == PROF_ACTIVE),
            'open_count': sum(1 for row in outbounds if row.status in OPEN_OCLS),
            'posted_count': sum(1 for row in outbounds if row.status in {OCL_SUBMITTED, OCL_COMPLETED}),
        }


_SERVICE: Optional[OclService] = None


def set_service(service: Optional[OclService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[OclService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('OCL_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryOclStore()
    path = os.environ.get('OCL_DB', DEFAULT_STORE_PATH)
    return SqliteOclStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[WireCalendar] = None,
) -> OclService:
    if store is None:
        store = default_store()
    return OclService(
        OclPolicy.from_env(),
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
        'profile_duplicate': 409,
        'profile_limit': 409,
        'outbound_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_completed': 409,
        'already_recalled': 409,
        'already_returned': 409,
        'already_presented': 409,
        'nsf': 409,
        'failed': 409,
        'recall_failed': 409,
        'return_failed': 409,
        'ocl_forbidden': 403,
        'ocl_disabled': 403,
        'profile_paused': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_cancelable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_completable': 403,
        'not_recallable': 403,
        'not_returnable': 403,
        'on_us_not_allowed': 403,
        'profile_not_found': 404,
        'outbound_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_aba': 400,
        'invalid_serial': 400,
        'invalid_ece': 400,
        'invalid_file': 400,
        'invalid_type': 400,
        'invalid_reason': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_profile': 400,
        'missing_outbound': 400,
    }.get(code, 400)


def _error_body(exc: OclError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('outbound') is not None:
        body['outbound'] = exc.extra['outbound'].to_dict()
    if exc.extra.get('profile') is not None:
        body['profile'] = exc.extra['profile'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except WireError as exc:
        return jsonify({'message': exc.message, 'error': exc.code}), _error_status(exc.code)
    except OclError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list_ocls(service: OclService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Ocls': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_add_profile(service: OclService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.add_profile(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname'),
            payee_name=values.get('payee_name') or values.get('legal_name') or values.get('name'),
            payor_aba=values.get('payor_aba') or values.get('aba') or values.get('routing'),
            drawer_account=values.get('drawer_account') or values.get('account_number') or values.get('on_us'),
            serial=values.get('serial') or '',
            aux_on_us=values.get('aux_on_us') or '',
            default_account=values.get('default_account') or values.get('account') or values.get('from_account'),
        )
        return jsonify({
            'message': 'Check21 profile added',
            'profile': row.to_dict(),
            'Ocls': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _profile_status_route(service: OclService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    profile_id = str(values.get('profile_id') or '').strip()
    if not profile_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_profile'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_profile_status(
            profile_id=profile_id, actor=userid, actor_type=actor_type, status=status,
        )
        return jsonify({
            'message': ok_message,
            'profile': row.to_dict(),
            'Ocls': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: OclService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    profile_id = str(values.get('profile_id') or '').strip()
    if not profile_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_profile'}), 400

    def _run():
        preview = service.preview(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            profile_id=profile_id,
            amount=values.get('amount'),
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            waive_fee=bool(values.get('waive_fee')),
            front=values.get('front', True),
            rear=values.get('rear', True),
        )
        return jsonify({'preview': preview, 'Ocls': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: OclService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    profile_id = str(values.get('profile_id') or '').strip()
    if not profile_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_profile'}), 400

    def _run():
        item, created = service.originate(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            profile_id=profile_id,
            amount=values.get('amount'),
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
            front=values.get('front', True),
            rear=values.get('rear', True),
        )
        return jsonify({
            'message': 'Check21 item originated' if created else 'Check21 item already posted',
            'outbound': item.to_dict(),
            'Ocls': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: OclService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    outbound_id = str(values.get('outbound_id') or '').strip()
    if not outbound_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_outbound'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        item = service.cancel_outbound(
            outbound_id=outbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Check21 item cancelled',
            'outbound': item.to_dict(),
            'Ocls': service.snapshot(item.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_ocl_route(service: OclService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    outbound_id = str(values.get('outbound_id') or '').strip()
    if not outbound_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_outbound'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'release':
            item = service.release_outbound(outbound_id=outbound_id, actor=userid, actor_type=actor_type)
            message = 'Check21 item released'
        elif action == 'reject':
            item = service.reject_outbound(
                outbound_id=outbound_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Check21 item rejected'
        elif action == 'complete':
            item = service.complete_outbound(outbound_id=outbound_id, actor=userid, actor_type=actor_type)
            message = 'Check21 item completed'
        elif action == 'recall':
            item = service.recall_outbound(
                outbound_id=outbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Check21 item recalled'
        elif action == 'override':
            item = service.override_ofac(
                outbound_id=outbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'waive':
            item = service.waive_fee(outbound_id=outbound_id, actor=userid, actor_type=actor_type)
            message = 'Check21 fee waived'
        elif action == 'return':
            item = service.return_outbound(
                outbound_id=outbound_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'nsf', file=values.get('file') or values.get('x937'),
                note=values.get('note') or '',
            )
            message = 'Check21 item returned'
        else:
            raise OclError('invalid_status', 'Unknown Check21 action.')
        return jsonify({
            'message': message,
            'outbound': item.to_dict(),
            'Ocls': service.snapshot(item.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: OclService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Ocls': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_export(service: OclService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only.', 'error': 'ocl_forbidden'}), 403
    owner = str(values.get('customer_id') or userid).strip() or userid
    payload = service.export_file(owner)
    return jsonify({
        'file': payload,
        'Ocls': service.snapshot(owner, actor=userid, actor_type=actor_type),
    }), 200


def attach_ocl_routes(app, service: OclService) -> None:
    @app.route('/listOcls', methods=['POST', 'GET'])
    def list_ocls_route():
        return handle_list_ocls(service)

    @app.route('/listOclProfiles', methods=['POST', 'GET'])
    def list_ocl_profiles_route():
        return handle_list_ocls(service)

    @app.route('/addOclProfile', methods=['POST', 'GET'])
    def add_ocl_profile_route():
        return handle_add_profile(service)

    @app.route('/pauseOclProfile', methods=['POST', 'GET'])
    def pause_ocl_profile_route():
        return _profile_status_route(service, PROF_PAUSED, 'Check21 profile paused')

    @app.route('/resumeOclProfile', methods=['POST', 'GET'])
    def resume_ocl_profile_route():
        return _profile_status_route(service, PROF_ACTIVE, 'Check21 profile resumed')

    @app.route('/archiveOclProfile', methods=['POST', 'GET'])
    def archive_ocl_profile_route():
        return _profile_status_route(service, PROF_ARCHIVED, 'Check21 profile archived')

    @app.route('/previewOcl', methods=['POST', 'GET'])
    def preview_ocl_route():
        return handle_preview(service)

    @app.route('/sendOcl', methods=['POST', 'GET'])
    def send_ocl_route():
        return handle_send(service)

    @app.route('/cancelOcl', methods=['POST', 'GET'])
    def cancel_ocl_route():
        return handle_cancel(service)

    @app.route('/releaseOcl', methods=['POST', 'GET'])
    def release_ocl_route():
        return _staff_ocl_route(service, 'release')

    @app.route('/rejectOcl', methods=['POST', 'GET'])
    def reject_ocl_route():
        return _staff_ocl_route(service, 'reject')

    @app.route('/completeOcl', methods=['POST', 'GET'])
    def complete_ocl_route():
        return _staff_ocl_route(service, 'complete')

    @app.route('/recallOcl', methods=['POST', 'GET'])
    def recall_ocl_route():
        return _staff_ocl_route(service, 'recall')

    @app.route('/overrideOclOfac', methods=['POST', 'GET'])
    def override_ocl_ofac_route():
        return _staff_ocl_route(service, 'override')

    @app.route('/waiveOclFee', methods=['POST', 'GET'])
    def waive_ocl_fee_route():
        return _staff_ocl_route(service, 'waive')

    @app.route('/returnOcl', methods=['POST', 'GET'])
    def return_ocl_route():
        return _staff_ocl_route(service, 'return')

    @app.route('/exportOcl', methods=['POST', 'GET'])
    def export_ocl_route():
        return handle_export(service)

    @app.route('/runDueOcls', methods=['POST', 'GET'])
    def run_due_ocls_route():
        return handle_run_due(service)
