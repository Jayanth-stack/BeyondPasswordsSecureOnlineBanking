"""Inbound UPI / IMPS / NEFT / RTGS receive posting (INR VPA / NPCI / IFSC).

Staff ingest incoming UPI, IMPS, NEFT, and RTGS credits. Recipients are
enrolled Virtual Payment Addresses (UPI), MMID+mobile (IMPS), or
IFSC+account keys (NEFT/RTGS and IFSC IMPS) at this bank's IFSC.
Matched UPI and IMPS credits post 24/7; NEFT queues on RBI holidays
and Sundays; RTGS posts 09:00–18:00 IST. Unmatched keys wait for a
VPA / MMID / IFSC claim or staff assignment. Independent of outbound
UPI origination (PR #145), inbound SPEI (PR #143), PIX, CHIPS, Zelle,
ACH linking (PR #68), and domestic Fedwire (PR #73). Existing
`/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- IFSC (11-char `[A-Z]{4}0[A-Z0-9]{6}`, 5th character always 0)
- VPA `local@psp` against an NPCI PSP handle directory
- MMID (7-digit IMPS) + Indian +91 mobile (10-digit starting 6-9)
- PAN (AAAAA9999A) with weighted alphabetic check digit
- NPCI 12-digit RRN (UPI/IMPS) vs 16-char NEFT/RTGS UTR
- RBI clock in IST (UTC+5:30): 2nd/4th Saturdays + gazetted holidays;
  UPI/IMPS 24/7, NEFT queues on RBI holidays, RTGS 09:00-18:00 IST
- UPI1 / IMPS1 pipe compose/parse (XML/DOCTYPE rejected)
- VPA / MMID / IFSC claim (customer presents matching key; enroll rematches)
- Complaint windows (UPI 24h customer / other rails next RBI business
  day / 90-day staff)
- Dual-control at INR 200,000

Reuses Fedwire OFAC screening, money parsing, and account directory
from `utility.wire`. Stores are pluggable (memory for tests, sqlite WAL
for restart-safe default). Full VPA / IFSC / account / MMID / PAN /
phone / fingerprint / RRN / destination never appear in to_dict /
snapshots.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    DEFAULT_WATCHLIST,
    EMPLOYEE_ROLES,
    MONEY_QUANTUM,
    AccountError,
    AmountError,
    ScreenResult,
    account_types_from_customer_payload,
    last4,
    money_str,
    normalize_account,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

KEY_ACTIVE = 'active'
KEY_PAUSED = 'paused'
KEY_CANCELLED = 'cancelled'
KEY_STATUSES = frozenset({KEY_ACTIVE, KEY_PAUSED, KEY_CANCELLED})
OPEN_KEY = frozenset({KEY_ACTIVE, KEY_PAUSED})
IN_UNMATCHED = 'unmatched'
IN_HELD = 'held'
IN_PENDING = 'pending_release'
IN_QUEUED = 'queued'
IN_POSTED = 'posted'
IN_REJECTED = 'rejected'
IN_RETURNED = 'returned'
IN_EXPIRED = 'expired'
IN_FAILED = 'failed'
IN_STATUSES = frozenset({
    IN_UNMATCHED, IN_HELD, IN_PENDING, IN_QUEUED, IN_POSTED, IN_REJECTED,
    IN_RETURNED, IN_EXPIRED, IN_FAILED,
})
OPEN_IN = frozenset({IN_UNMATCHED, IN_HELD, IN_PENDING, IN_QUEUED})
TERMINAL_IN = frozenset({IN_POSTED, IN_REJECTED, IN_RETURNED, IN_EXPIRED, IN_FAILED})
PURPOSES = frozenset({'family', 'goods', 'payroll', 'tax', 'loan', 'rent', 'other'})
PURPOSE_ALIASES = {
    'personal': 'family', 'gift': 'family', 'support': 'family',
    'invoice': 'goods', 'purchase': 'goods', 'vendor': 'goods',
    'salary': 'payroll', 'wage': 'payroll',
    'gst': 'tax', 'tds': 'tax', 'taxes': 'tax', 'income_tax': 'tax',
    'mortgage': 'loan', 'housing': 'rent', 'emi': 'loan',
}
RAILS = frozenset({'upi', 'imps', 'neft', 'rtgs'})
RAIL_ALIASES = {
    'upi_lite': 'upi', 'vpa': 'upi', 'p2p': 'upi', 'intent': 'upi', 'collect': 'upi',
    'immediate': 'imps', 'mmid': 'imps',
    'electronic': 'neft', 'batch': 'neft',
    'rtgs_plus': 'rtgs', 'large': 'rtgs',
}
DEFAULT_BANKS = {
    'HDFC': 'HDFC BANK',
    'ICIC': 'ICICI BANK',
    'SBIN': 'STATE BANK OF INDIA',
    'KKBK': 'KOTAK MAHINDRA BANK',
    'UTIB': 'AXIS BANK',
    'YESB': 'YES BANK',
    'PUNB': 'PUNJAB NATIONAL BANK',
    'CNRB': 'CANARA BANK',
    'BARB': 'BANK OF BARODA',
    'IDIB': 'INDIAN BANK',
    'UBIN': 'UNION BANK OF INDIA',
    'IOBA': 'INDIAN OVERSEAS BANK',
    'KNHB': 'KONOHA BANK',
}
DEFAULT_PSPS = frozenset({
    'oksbi', 'okhdfcbank', 'okicici', 'okaxis', 'okkotak',
    'ybl', 'axl', 'ibl', 'apl', 'paytm', 'upi', 'kbl', 'federal', 'slc',
})
PAN_TYPES = frozenset('PCHABGJLFT')
IFSC_RE = re.compile(r'^[A-Z]{4}0[A-Z0-9]{6}$')
VPA_LOCAL_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{1,44}$')
UPI1_VERSION = '1'
UPI1_FIELDS = 14
IMPS1_FIELDS = 7
DEFAULT_STORE_PATH = 'SystemLogs/urecv.sqlite'
DEFAULT_RECEIVER_IFSC = 'KNHB0000001'
DEFAULT_SENDER_IFSC = 'HDFC0000001'
CUSTOMER_UPI_RETURN_SECONDS = 24 * 60 * 60
STAFF_COMPLAINT_DAYS = 90
CLAIM_SECONDS = 24 * 60 * 60
IST_OFFSET_MINUTES = 330
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)


class UrecvError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


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
        raise UrecvError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise UrecvError('invalid_name', 'Sender legal name must be 2-80 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise UrecvError('invalid_purpose', 'Unknown UPI purpose.')
    return text


def normalize_currency(value: Any, *, default: str = 'INR') -> str:
    text = str(value or default).strip().upper()
    if text not in {'INR', 'RS', 'INR.'}:
        raise UrecvError('invalid_currency', 'UPI / IMPS / NEFT / RTGS is INR only.')
    return 'INR'


def normalize_rail(value: Any, *, default: str = 'upi') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text not in RAILS:
        raise UrecvError('invalid_rail', 'Rail must be upi, imps, neft, or rtgs.')
    return text


def normalize_ifsc(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[\s-]', '', str(value or '')).upper()
    if not text:
        if required:
            raise UrecvError('invalid_ifsc', 'IFSC is required.')
        return ''
    if not IFSC_RE.match(text):
        raise UrecvError('invalid_ifsc', 'IFSC must be 11 characters with a 0 in position 5.')
    return text


def mask_ifsc(value: Any) -> str:
    text = re.sub(r'[\s-]', '', str(value or '')).upper()
    if len(text) < 5:
        return '****0******'
    return text[:4] + '0******'


def bank_code_from_ifsc(value: Any) -> str:
    try:
        return normalize_ifsc(value)[:4]
    except UrecvError:
        return ''


def bank_name(value: Any, *, directory: Optional[Dict[str, str]] = None) -> str:
    code = bank_code_from_ifsc(value) or str(value or '').strip().upper()[:4]
    mapping = directory if directory is not None else DEFAULT_BANKS
    return mapping.get(code, '')


def normalize_external_account(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise UrecvError('invalid_account', 'Beneficiary account is required.')
        return ''
    if not (6 <= len(digits) <= 18):
        raise UrecvError('invalid_account', 'Beneficiary account must be 6-18 digits.')
    return digits


def mask_account(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return ('*' * max(0, len(digits) - 4)) + digits[-4:]


def normalize_vpa(value: Any, *, required: bool = True, psps: Optional[Iterable[str]] = None) -> str:
    text = str(value or '').strip().lower()
    if not text:
        if required:
            raise UrecvError('invalid_vpa', 'UPI VPA is required.')
        return ''
    if text.count('@') != 1:
        raise UrecvError('invalid_vpa', 'VPA must be local@psp.')
    local, _, handle = text.partition('@')
    if not VPA_LOCAL_RE.match(local):
        raise UrecvError('invalid_vpa', 'VPA local part is invalid.')
    allowed = set(psps) if psps is not None else set(DEFAULT_PSPS)
    if handle not in allowed:
        raise UrecvError('invalid_vpa', 'VPA PSP handle is not a registered NPCI handle.')
    return '%s@%s' % (local, handle)


def mask_vpa(value: Any) -> str:
    text = str(value or '').strip().lower()
    if '@' not in text:
        return '***@***'
    local, _, handle = text.partition('@')
    if len(local) <= 2:
        hidden = '*' * len(local)
    else:
        hidden = local[0] + ('*' * (len(local) - 2)) + local[-1]
    return '%s@%s' % (hidden, handle)


def normalize_mmid(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise UrecvError('invalid_mmid', 'MMID is required for IMPS.')
        return ''
    if len(digits) != 7 or digits == '0000000':
        raise UrecvError('invalid_mmid', 'MMID must be seven digits.')
    return digits


def mask_mmid(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '***'
    return '***' + digits[-4:]


def normalize_phone(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise UrecvError('invalid_phone', 'Indian mobile is required for IMPS MMID.')
        return ''
    if digits.startswith('91') and len(digits) == 12:
        national = digits[2:]
    elif len(digits) == 10:
        national = digits
    elif digits.startswith('0') and len(digits) == 11:
        national = digits[1:]
    else:
        raise UrecvError('invalid_phone', 'Indian mobile must be 10 digits starting 6-9.')
    if len(national) != 10 or national[0] not in '6789':
        raise UrecvError('invalid_phone', 'Indian mobile must be 10 digits starting 6-9.')
    return '+91' + national


def mask_phone(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '+91-****-****'
    national = digits[-10:] if len(digits) >= 10 else digits
    return '+91-****-' + national[-4:]


def pan_check_digit(body: str) -> str:
    text = str(body or '').upper()
    if len(text) != 9:
        raise UrecvError('invalid_pan', 'PAN body must be 9 characters.')
    total = 0
    for index, char in enumerate(text):
        if char.isalpha():
            number = ord(char) - 55
        elif char.isdigit():
            number = int(char)
        else:
            raise UrecvError('invalid_pan', 'PAN contains an invalid character.')
        total += number * (9 - index)
    return chr(ord('A') + (total % 26))


def pan_check_digit_ok(value: str) -> bool:
    text = str(value or '').upper()
    if len(text) != 10:
        return False
    try:
        return pan_check_digit(text[:9]) == text[9]
    except UrecvError:
        return False


def compose_pan(body: Any) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(body or '').upper())
    if len(text) != 9:
        raise UrecvError('invalid_pan', 'PAN body must be 9 characters.')
    return text + pan_check_digit(text)


def normalize_pan(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[\s-]', '', str(value or '')).upper()
    if not text:
        if required:
            raise UrecvError('invalid_pan', 'PAN is required.')
        return ''
    if len(text) != 10 or not text[:5].isalpha() or not text[5:9].isdigit() or not text[9].isalpha():
        raise UrecvError('invalid_pan', 'PAN must be AAAAA9999A.')
    if text[3] not in PAN_TYPES:
        raise UrecvError('invalid_pan', 'PAN holder type is invalid.')
    if not pan_check_digit_ok(text):
        raise UrecvError('invalid_pan', 'PAN failed checksum.')
    return text


def mask_pan(value: Any) -> str:
    text = str(value or '').upper()
    if len(text) < 4:
        return '**********'
    return text[:3] + '******' + text[-1]


def destination_fingerprint(
    rail: str, vpa: str, ifsc: str, account_number: str, mmid: str, phone: str,
) -> str:
    material = '%s|%s|%s|%s|%s|%s' % (
        rail, vpa or '', ifsc or '', account_number or '', mmid or '', phone or '',
    )
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def compose_rrn(cycle_date: str, sequence: int, *, rail: str = 'upi') -> str:
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise UrecvError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise UrecvError('invalid_reference', 'RRN sequence out of range.')
    kind = normalize_rail(rail)
    if kind in {'neft', 'rtgs'}:
        return 'KNH%s%05d' % (day, seq)
    return '%s%06d' % (day[2:], seq)


def normalize_rrn(value: Any) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if len(text) == 12 and text.isdigit():
        return text
    if len(text) == 16 and text.startswith('KNH'):
        return text
    raise UrecvError('invalid_reference', 'NPCI RRN / UTR is invalid.')


def mask_rrn(value: Any) -> str:
    text = str(value or '')
    if len(text) < 6:
        return '********'
    return ('*' * (len(text) - 4)) + text[-4:]


def compose_amount_field(amount: Decimal) -> str:
    quantized = amount.quantize(MONEY_QUANTUM)
    if quantized < 0 or quantized > Decimal('999999999.99'):
        raise UrecvError('invalid_amount', 'Amount cannot be encoded in UPI1.')
    return money_str(quantized)


def parse_amount_field(value: Any) -> Decimal:
    try:
        return parse_money(value)
    except AmountError as exc:
        raise UrecvError('invalid_amount', 'UPI1 amount field is invalid.') from exc


def _reject_markup(text: str) -> None:
    lowered = text.lower()
    if '<xml' in lowered or '<!doctype' in lowered or '<?xml' in lowered:
        raise UrecvError('invalid_file', 'XML/DOCTYPE UPI1 files are rejected.')


def compose_upi1(
    *,
    rrn: str,
    vpa: str,
    ifsc: str,
    account_number: str,
    mmid: str,
    phone: str,
    pan: str,
    amount: Decimal,
    purpose: str,
    status: str,
    rail: str,
    mask: bool = True,
) -> str:
    reference = normalize_rrn(rrn)
    kind = normalize_rail(rail)
    return '|'.join((
        'UPI1',
        UPI1_VERSION,
        mask_rrn(reference) if mask else reference,
        mask_vpa(vpa) if mask and vpa else (normalize_vpa(vpa, required=False) if vpa else ''),
        mask_ifsc(ifsc) if mask and ifsc else (normalize_ifsc(ifsc, required=False) if ifsc else ''),
        mask_account(account_number) if mask and account_number else (
            normalize_external_account(account_number, required=False) if account_number else ''
        ),
        mask_mmid(mmid) if mask and mmid else (normalize_mmid(mmid) if mmid else ''),
        mask_phone(phone) if mask and phone else (normalize_phone(phone) if phone else ''),
        mask_pan(pan) if mask and pan else (normalize_pan(pan, required=False) if pan else ''),
        compose_amount_field(amount),
        normalize_purpose(purpose),
        str(status or IN_POSTED),
        kind,
        kind,
    ))


def parse_upi1(text: Any) -> Dict[str, Any]:
    raw = str(text or '').strip()
    if not raw:
        raise UrecvError('invalid_file', 'UPI1 record is empty.')
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != UPI1_FIELDS:
        raise UrecvError('invalid_file', 'UPI1 record must have %d fields.' % UPI1_FIELDS)
    if parts[0] != 'UPI1' or parts[1] != UPI1_VERSION:
        raise UrecvError('invalid_file', 'UPI1 version marker is invalid.')
    if parts[13] not in RAILS:
        raise UrecvError('invalid_file', 'UPI1 settlement method is invalid.')
    if '*' in parts[2] or '*' in parts[3] or '*' in parts[4]:
        raise UrecvError('invalid_file', 'Masked UPI1 record cannot be re-ingested.')
    rail = normalize_rail(parts[12])
    return {
        'rrn': normalize_rrn(parts[2]),
        'vpa': normalize_vpa(parts[3], required=rail == 'upi') if parts[3] else '',
        'ifsc': normalize_ifsc(parts[4], required=False) if parts[4] else '',
        'account_number': normalize_external_account(parts[5], required=False) if parts[5] else '',
        'mmid': normalize_mmid(parts[6]) if parts[6] else '',
        'phone': normalize_phone(parts[7]) if parts[7] else '',
        'pan': normalize_pan(parts[8], required=False) if parts[8] else '',
        'amount': parse_amount_field(parts[9]),
        'purpose': normalize_purpose(parts[10]),
        'status': parts[11],
        'rail': rail,
        'settlement_method': parts[13],
        'send_ifsc': '',
        'recv_ifsc': DEFAULT_RECEIVER_IFSC,
        'name': '',
    }


def compose_imps1(
    *,
    mmid: str,
    phone: str,
    amount: Decimal,
    rrn: str,
    reference: str = '',
    mask: bool = True,
) -> str:
    tracking = normalize_rrn(rrn)
    _ = reference
    return '|'.join((
        'IMPS1',
        UPI1_VERSION,
        mask_mmid(mmid) if mask else normalize_mmid(mmid, required=True),
        mask_phone(phone) if mask else normalize_phone(phone, required=True),
        compose_amount_field(amount),
        mask_rrn(tracking) if mask else tracking,
        'INR',
    ))


def parse_imps1(text: Any) -> Dict[str, Any]:
    raw = str(text or '').strip()
    if not raw:
        raise UrecvError('invalid_file', 'IMPS1 record is empty.')
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != IMPS1_FIELDS:
        raise UrecvError('invalid_file', 'IMPS1 record must have %d fields.' % IMPS1_FIELDS)
    if parts[0] != 'IMPS1' or parts[1] != UPI1_VERSION or parts[6] != 'INR':
        raise UrecvError('invalid_file', 'IMPS1 version marker is invalid.')
    if '*' in parts[2] or '*' in parts[3] or '*' in parts[5]:
        raise UrecvError('invalid_file', 'Masked IMPS1 record cannot be re-ingested.')
    return {
        'mmid': normalize_mmid(parts[2], required=True),
        'phone': normalize_phone(parts[3], required=True),
        'amount': parse_amount_field(parts[4]),
        'rrn': normalize_rrn(parts[5]),
        'currency': 'INR',
        'rail': 'imps',
        'vpa': '',
        'ifsc': '',
        'account_number': '',
        'pan': '',
        'send_ifsc': '',
        'recv_ifsc': DEFAULT_RECEIVER_IFSC,
        'purpose': 'other',
        'name': '',
    }


def split_upi1_file(text: Any) -> List[str]:
    raw = str(text or '')
    _reject_markup(raw)
    records = [line.strip() for line in raw.replace('\r\n', '\n').split('\n') if line.strip()]
    if not records:
        raise UrecvError('invalid_file', 'UPI1 file is empty.')
    return records


def parse_record(text: Any) -> Dict[str, Any]:
    raw = str(text or '').strip()
    if raw.startswith('IMPS1|'):
        return parse_imps1(raw)
    return parse_upi1(raw)


def message_from_values(values: Dict[str, Any], *, psps: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    rail = normalize_rail(values.get('rail') or values.get('kind') or 'upi')
    recv_raw = values.get('recv_ifsc') or values.get('receiver') or values.get('recv_bank') or DEFAULT_RECEIVER_IFSC
    recv = normalize_ifsc(recv_raw)
    send_raw = values.get('send_ifsc') or values.get('sender_ifsc') or values.get('send_bank') or values.get('ifsc_from')
    send = normalize_ifsc(send_raw) if send_raw else DEFAULT_SENDER_IFSC
    amount = values.get('amount')
    if not isinstance(amount, Decimal):
        try:
            amount = parse_money(amount)
        except AmountError as exc:
            raise UrecvError('invalid_amount', 'Amount is invalid.') from exc
    tracking = str(values.get('rrn') or values.get('tracking_key') or values.get('utr') or values.get('reference') or '').strip()
    if tracking:
        tracking = normalize_rrn(tracking)
    else:
        cycle = str(values.get('value_date') or values.get('cycle_date') or '').strip()
        if '-' in cycle:
            cycle = cycle.replace('-', '')[:8]
        seq = values.get('sequence') or values.get('seq') or 1
        tracking = compose_rrn(cycle, int(seq), rail=rail)
    vpa = normalize_vpa(values.get('vpa') or values.get('virtual_address'), required=rail == 'upi', psps=psps)
    ifsc = normalize_ifsc(
        values.get('ifsc') or values.get('destination_ifsc'),
        required=rail in {'neft', 'rtgs'} or (rail == 'imps' and not (values.get('mmid') or values.get('mobile_mmid'))),
    )
    account_number = normalize_external_account(
        values.get('account_number') or values.get('account') or values.get('destination_account_ext'),
        required=bool(ifsc) and rail != 'upi',
    )
    mmid = normalize_mmid(values.get('mmid') or values.get('mobile_mmid'), required=rail == 'imps' and not ifsc)
    phone = normalize_phone(
        values.get('phone') or values.get('mobile') or values.get('cel'),
        required=rail == 'imps' and bool(mmid),
    )
    pan = normalize_pan(values.get('pan'), required=rail in {'neft', 'rtgs'})
    name = str(values.get('name') or values.get('sender_name') or values.get('payer') or '').strip()
    if name:
        name = normalize_legal_name(name)
    labels = {'upi': 'UPI PAYER', 'imps': 'IMPS PAYER', 'neft': 'NEFT PAYER', 'rtgs': 'RTGS PAYER'}
    return {
        'rrn': tracking,
        'send_ifsc': send,
        'recv_ifsc': recv,
        'vpa': vpa,
        'ifsc': ifsc,
        'account_number': account_number,
        'mmid': mmid,
        'phone': phone,
        'pan': pan,
        'amount': amount,
        'purpose': normalize_purpose(values.get('purpose')),
        'rail': rail,
        'name': name or labels.get(rail, 'UPI PAYER'),
        'currency': normalize_currency(values.get('currency')),
    }


def easter_gregorian(year: int) -> date:
    """Anonymous Gregorian Easter (used for Good Friday)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month = (h + el - 7 * m + 114) // 31
    day = ((h + el - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return date(year, month, 1 + offset + (n - 1) * 7)


def rbi_holidays(year: int) -> set:
    """RBI working-day calendar: gazetted holidays plus 2nd/4th Saturdays."""
    easter = easter_gregorian(year)
    days = {
        date(year, 1, 26),
        date(year, 4, 14),
        date(year, 5, 1),
        date(year, 8, 15),
        date(year, 10, 2),
        date(year, 12, 25),
        easter - timedelta(days=2),
    }
    for month in range(1, 13):
        days.add(_nth_weekday(year, month, 5, 2))
        days.add(_nth_weekday(year, month, 5, 4))
    return days


class NpciClock:
    """RBI / NPCI clock in IST. UPI and IMPS stay 24/7 under their caps."""

    def __init__(
        self,
        *,
        open_hour: int = 9,
        cutoff_hour: int = 18,
        cutoff_minute: int = 0,
        tz_offset_minutes: int = IST_OFFSET_MINUTES,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.open_hour = int(open_hour)
        self.cutoff_hour = int(cutoff_hour)
        self.cutoff_minute = int(cutoff_minute)
        self.tz_offset_minutes = int(tz_offset_minutes)
        self.tz = timezone(timedelta(minutes=self.tz_offset_minutes))
        extra = set()
        for item in extra_holidays:
            text = str(item).strip()
            if not text:
                continue
            extra.add(date.fromisoformat(text[:10]))
        self.extra_holidays = extra

    def local_dt(self, ts: float) -> datetime:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(self.tz)

    def is_weekend(self, day: date) -> bool:
        return day.weekday() == 6

    def is_holiday(self, day: date) -> bool:
        return day in rbi_holidays(day.year) or day in self.extra_holidays

    def is_business_day(self, day: date) -> bool:
        return not self.is_weekend(day) and not self.is_holiday(day)

    def is_after_cutoff(self, ts: float) -> bool:
        local = self.local_dt(ts)
        if (local.hour, local.minute) >= (self.cutoff_hour, self.cutoff_minute):
            return True
        if local.hour < self.open_hour:
            return True
        return False

    def is_session(self, ts: float) -> bool:
        local = self.local_dt(ts)
        return self.is_business_day(local.date()) and not self.is_after_cutoff(ts)

    def next_business_day(self, day: date) -> date:
        cursor = day + timedelta(days=1)
        while not self.is_business_day(cursor):
            cursor += timedelta(days=1)
        return cursor

    def value_date(self, ts: float) -> date:
        local = self.local_dt(ts)
        day = local.date()
        if self.is_session(ts):
            return day
        if self.is_business_day(day) and local.hour < self.open_hour:
            return day
        return self.next_business_day(day)

    def cycle_date(self, ts: float) -> str:
        return self.local_dt(ts).strftime('%Y%m%d')

    def session_close_ts(self, day: date) -> float:
        local = datetime(
            day.year, day.month, day.day,
            self.cutoff_hour, self.cutoff_minute, tzinfo=self.tz,
        )
        return local.timestamp()

    def snapshot(self, ts: float) -> Dict[str, Any]:
        local = self.local_dt(ts)
        day = local.date()
        session = self.is_session(ts)
        return {
            'local_date': day.isoformat(),
            'local_time': local.strftime('%H:%M'),
            'timezone': 'IST',
            'open': '%02d:00' % self.open_hour,
            'cutoff': '%02d:%02d' % (self.cutoff_hour, self.cutoff_minute),
            'session': session,
            'after_cutoff': not session,
            'weekend': self.is_weekend(day),
            'holiday': self.is_holiday(day),
            'business_day': self.is_business_day(day),
            'value_date': self.value_date(ts).isoformat(),
            'cycle_date': self.cycle_date(ts),
            'settlement_method': 'upi',
        }


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def _env_money(name: str, default: str) -> Decimal:
    raw = os.environ.get(name)
    try:
        return parse_money(raw if raw not in (None, '') else default, allow_zero=True)
    except AmountError:
        return parse_money(default, allow_zero=True)


def _env_list(name: str) -> Tuple[str, ...]:
    raw = os.environ.get(name) or ''
    return tuple(item.strip() for item in raw.split(',') if item.strip())


def _classify_money_result(result: Any) -> str:
    text = str(result or '').strip().lower()
    if any(token in text for token in CREDIT_NSF):
        return 'nsf'
    if text in CREDIT_OK or 'debit' in text or 'credit' in text or 'success' in text:
        return 'ok'
    return 'failed'


@dataclass
class UrecvPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_claim: bool = True
    customer_return: bool = True
    allow_credit: bool = False
    max_keys: int = 12
    max_inbounds: int = 120
    min_amount: Decimal = Decimal('0.01')
    upi_max: Decimal = Decimal('100000.00')
    imps_max: Decimal = Decimal('500000.00')
    neft_max: Decimal = Decimal('1000000.00')
    rtgs_min: Decimal = Decimal('200000.00')
    rtgs_max: Decimal = Decimal('5000000.00')
    dual_control_threshold: Decimal = Decimal('200000.00')
    tz_offset_minutes: int = IST_OFFSET_MINUTES
    open_hour: int = 9
    cutoff_hour: int = 18
    receiver_ifsc: str = DEFAULT_RECEIVER_IFSC
    customer_upi_return_seconds: int = CUSTOMER_UPI_RETURN_SECONDS
    staff_complaint_days: int = STAFF_COMPLAINT_DAYS
    claim_seconds: int = CLAIM_SECONDS
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()
    banks: Dict[str, str] = None  # type: ignore[assignment]
    psps: Tuple[str, ...] = tuple(sorted(DEFAULT_PSPS))

    def __post_init__(self) -> None:
        if self.banks is None:
            self.banks = dict(DEFAULT_BANKS)

    @classmethod
    def from_env(cls) -> 'UrecvPolicy':
        extra = _env_list('URECV_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        extra_banks = _env_list('URECV_BANKS')
        directory = dict(DEFAULT_BANKS)
        for item in extra_banks:
            if ':' in item:
                pid, name = item.split(':', 1)
                directory[pid.strip().upper()[:4]] = name.strip().upper()[:80]
        extra_psps = _env_list('URECV_PSPS')
        psps = tuple(dict.fromkeys(tuple(sorted(DEFAULT_PSPS)) + extra_psps))
        source = os.environ.get('URECV_RECEIVER') or DEFAULT_RECEIVER_IFSC
        try:
            receiver = normalize_ifsc(source)
        except UrecvError:
            receiver = DEFAULT_RECEIVER_IFSC
        return cls(
            enabled=_env_bool('URECV_ENABLED', True),
            customer_manage=_env_bool('URECV_CUSTOMER_MANAGE', True),
            customer_claim=_env_bool('URECV_CUSTOMER_CLAIM', True),
            customer_return=_env_bool('URECV_CUSTOMER_RETURN', True),
            allow_credit=_env_bool('URECV_ALLOW_CREDIT', False),
            max_keys=max(1, _env_int('URECV_MAX_KEYS', 12)),
            max_inbounds=max(1, _env_int('URECV_MAX_INBOUNDS', 120)),
            min_amount=_env_money('URECV_MIN_AMOUNT', '0.01'),
            upi_max=_env_money('URECV_UPI_MAX', '100000.00'),
            imps_max=_env_money('URECV_IMPS_MAX', '500000.00'),
            neft_max=_env_money('URECV_NEFT_MAX', '1000000.00'),
            rtgs_min=_env_money('URECV_RTGS_MIN', '200000.00'),
            rtgs_max=_env_money('URECV_RTGS_MAX', '5000000.00'),
            dual_control_threshold=_env_money('URECV_DUAL_CONTROL', '200000.00'),
            tz_offset_minutes=_env_int('URECV_TZ_OFFSET_MINUTES', IST_OFFSET_MINUTES),
            open_hour=max(0, min(23, _env_int('URECV_OPEN_HOUR', 9))),
            cutoff_hour=max(0, min(23, _env_int('URECV_CUTOFF_HOUR', 18))),
            receiver_ifsc=receiver,
            customer_upi_return_seconds=max(60, _env_int(
                'URECV_UPI_RETURN_SECONDS', CUSTOMER_UPI_RETURN_SECONDS,
            )),
            staff_complaint_days=max(1, _env_int('URECV_COMPLAINT_DAYS', STAFF_COMPLAINT_DAYS)),
            claim_seconds=max(60, _env_int('URECV_CLAIM_SECONDS', CLAIM_SECONDS)),
            watchlist=watch,
            extra_holidays=_env_list('URECV_HOLIDAYS'),
            banks=directory,
            psps=psps,
        )


@dataclass
class UrecvKey:
    key_id: str
    userid: str
    nickname: str
    rail: str
    vpa: str
    ifsc: str
    account_number: str
    mmid: str
    phone: str
    pan: str
    fingerprint: str
    destination_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'key_id': self.key_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'rail': self.rail,
            'vpa_masked': mask_vpa(self.vpa) if self.vpa else '',
            'ifsc_masked': mask_ifsc(self.ifsc) if self.ifsc else '',
            'account_masked': mask_account(self.account_number) if self.account_number else '',
            'mmid_masked': mask_mmid(self.mmid) if self.mmid else '',
            'phone_masked': mask_phone(self.phone) if self.phone else '',
            'pan_masked': mask_pan(self.pan) if self.pan else '',
            'bank': bank_code_from_ifsc(self.ifsc) if self.ifsc else '',
            'account_last4': last4(self.destination_account),
            'status': self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == KEY_ACTIVE,
            'paused': self.status == KEY_PAUSED,
            'cancelled': self.status == KEY_CANCELLED,
        }


@dataclass
class UrecvInbound:
    inbound_id: str
    rrn: str
    userid: str
    destination_account: str
    amount_inr: str
    sender_name: str
    send_ifsc: str
    recv_ifsc: str
    rail: str
    vpa: str
    ifsc: str
    account_number: str
    mmid: str
    phone: str
    pan: str
    fingerprint: str
    purpose: str
    status: str
    reason: str
    actor: str
    releaser: str
    ofac_hit: bool
    ofac_match: str
    ofac_overridden: bool
    created_at: float
    updated_at: float
    posted_at: float = 0.0
    returned_at: float = 0.0
    expiry_at: float = 0.0
    note: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'inbound_id': self.inbound_id,
            'rrn_masked': mask_rrn(self.rrn),
            'userid': self.userid,
            'account_last4': last4(self.destination_account) if self.destination_account else '',
            'amount_inr': self.amount_inr,
            'sender_name': self.sender_name,
            'send_ifsc_masked': mask_ifsc(self.send_ifsc) if self.send_ifsc else '',
            'send_name': bank_name(self.send_ifsc) if self.send_ifsc else '',
            'recv_ifsc_masked': mask_ifsc(self.recv_ifsc) if self.recv_ifsc else '',
            'rail': self.rail,
            'vpa_masked': mask_vpa(self.vpa) if self.vpa else '',
            'ifsc_masked': mask_ifsc(self.ifsc) if self.ifsc else '',
            'account_masked': mask_account(self.account_number) if self.account_number else '',
            'mmid_masked': mask_mmid(self.mmid) if self.mmid else '',
            'phone_masked': mask_phone(self.phone) if self.phone else '',
            'pan_masked': mask_pan(self.pan) if self.pan else '',
            'purpose': self.purpose,
            'status': self.status,
            'reason': self.reason,
            'ofac_hit': self.ofac_hit,
            'returnable': self.status in OPEN_IN or self.status == IN_POSTED,
            'claimable': self.status == IN_UNMATCHED,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'posted_at': self.posted_at,
            'expiry_at': self.expiry_at,
        }


def _key_from_row(row: Any) -> UrecvKey:
    data = dict(row)
    return UrecvKey(
        key_id=data['key_id'], userid=data['userid'], nickname=data['nickname'],
        rail=data['rail'], vpa=data.get('vpa') or '', ifsc=data.get('ifsc') or '',
        account_number=data.get('account_number') or '', mmid=data.get('mmid') or '',
        phone=data.get('phone') or '', pan=data.get('pan') or '', fingerprint=data['fingerprint'],
        destination_account=data['destination_account'], status=data['status'], actor=data['actor'],
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
    )


def _inbound_from_row(row: Any) -> UrecvInbound:
    data = dict(row)
    return UrecvInbound(
        inbound_id=data['inbound_id'], rrn=data['rrn'], userid=data['userid'],
        destination_account=data['destination_account'], amount_inr=data['amount_inr'],
        sender_name=data['sender_name'], send_ifsc=data['send_ifsc'], recv_ifsc=data['recv_ifsc'],
        rail=data['rail'], vpa=data.get('vpa') or '', ifsc=data.get('ifsc') or '',
        account_number=data.get('account_number') or '', mmid=data.get('mmid') or '',
        phone=data.get('phone') or '', pan=data.get('pan') or '', fingerprint=data['fingerprint'],
        purpose=data['purpose'], status=data['status'],
        reason=data['reason'], actor=data['actor'], releaser=data['releaser'],
        ofac_hit=bool(int(data['ofac_hit'])), ofac_match=data['ofac_match'],
        ofac_overridden=bool(int(data['ofac_overridden'])),
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
        posted_at=float(data.get('posted_at') or 0), returned_at=float(data.get('returned_at') or 0),
        expiry_at=float(data.get('expiry_at') or 0), note=data.get('note') or '',
    )


class MemoryUrecvStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.keys: Dict[str, UrecvKey] = {}
        self.inbounds: Dict[str, UrecvInbound] = {}
        self._by_rrn: Dict[str, str] = {}

    def put_key(self, row: UrecvKey) -> None:
        with self._lock:
            self.keys[row.key_id] = row

    def get_key(self, key_id: str) -> Optional[UrecvKey]:
        return self.keys.get(key_id)

    def get_key_by_fingerprint(self, fingerprint: str) -> Optional[UrecvKey]:
        matches = [
            row for row in self.keys.values()
            if row.fingerprint == fingerprint and row.status in OPEN_KEY
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda row: row.created_at, reverse=True)[0]

    def update_key(self, row: UrecvKey) -> None:
        self.put_key(row)

    def list_keys(self, userid: Optional[str] = None) -> List[UrecvKey]:
        rows = list(self.keys.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        return sorted(rows, key=lambda row: row.created_at)

    def put_inbound(self, row: UrecvInbound) -> None:
        with self._lock:
            self.inbounds[row.inbound_id] = row
            self._by_rrn[row.rrn] = row.inbound_id

    def get_inbound(self, inbound_id: str) -> Optional[UrecvInbound]:
        return self.inbounds.get(inbound_id)

    def get_by_rrn(self, rrn: str) -> Optional[UrecvInbound]:
        inbound_id = self._by_rrn.get(rrn)
        return self.inbounds.get(inbound_id) if inbound_id else None

    def update_inbound(self, row: UrecvInbound) -> None:
        self.put_inbound(row)

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[UrecvInbound]:
        rows = list(self.inbounds.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)


class SqliteUrecvStore:
    def __init__(self, path: str = DEFAULT_STORE_PATH) -> None:
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        return conn

    def _init(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS keys (
                    key_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    rail TEXT NOT NULL,
                    vpa TEXT NOT NULL DEFAULT '',
                    ifsc TEXT NOT NULL DEFAULT '',
                    account_number TEXT NOT NULL DEFAULT '',
                    mmid TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
                    pan TEXT NOT NULL DEFAULT '',
                    fingerprint TEXT NOT NULL,
                    destination_account TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inbounds (
                    inbound_id TEXT PRIMARY KEY,
                    rrn TEXT NOT NULL UNIQUE,
                    userid TEXT NOT NULL DEFAULT '',
                    destination_account TEXT NOT NULL DEFAULT '',
                    amount_inr TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    send_ifsc TEXT NOT NULL,
                    recv_ifsc TEXT NOT NULL,
                    rail TEXT NOT NULL,
                    vpa TEXT NOT NULL DEFAULT '',
                    ifsc TEXT NOT NULL DEFAULT '',
                    account_number TEXT NOT NULL DEFAULT '',
                    mmid TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
                    pan TEXT NOT NULL DEFAULT '',
                    fingerprint TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    ofac_overridden INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    posted_at REAL NOT NULL DEFAULT 0,
                    returned_at REAL NOT NULL DEFAULT 0,
                    expiry_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_key(self, row: UrecvKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO keys (
                    key_id, userid, nickname, rail, vpa, ifsc, account_number, mmid, phone, pan,
                    fingerprint, destination_account, status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.key_id, row.userid, row.nickname, row.rail, row.vpa, row.ifsc,
                    row.account_number, row.mmid, row.phone, row.pan, row.fingerprint,
                    row.destination_account, row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_key(self, key_id: str) -> Optional[UrecvKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM keys WHERE key_id = ?', (key_id,)).fetchone()
        return _key_from_row(row) if row else None

    def get_key_by_fingerprint(self, fingerprint: str) -> Optional[UrecvKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM keys WHERE fingerprint = ? AND status IN ('active', 'paused')
                ORDER BY created_at DESC LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
        return _key_from_row(row) if row else None

    def update_key(self, row: UrecvKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE keys SET nickname=?, rail=?, vpa=?, ifsc=?, account_number=?, mmid=?,
                    phone=?, pan=?, fingerprint=?, destination_account=?, status=?, actor=?, updated_at=?
                WHERE key_id=?
                """,
                (
                    row.nickname, row.rail, row.vpa, row.ifsc, row.account_number, row.mmid,
                    row.phone, row.pan, row.fingerprint, row.destination_account, row.status,
                    row.actor, row.updated_at, row.key_id,
                ),
            )
            conn.commit()

    def list_keys(self, userid: Optional[str] = None) -> List[UrecvKey]:
        sql = 'SELECT * FROM keys'
        args: Tuple[Any, ...] = ()
        if userid is not None:
            sql += ' WHERE userid = ?'
            args = (userid,)
        sql += ' ORDER BY created_at'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_key_from_row(row) for row in rows]

    def put_inbound(self, row: UrecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inbounds (
                    inbound_id, rrn, userid, destination_account, amount_inr,
                    sender_name, send_ifsc, recv_ifsc, rail, vpa, ifsc, account_number,
                    mmid, phone, pan, fingerprint, purpose, status, reason, actor, releaser,
                    ofac_hit, ofac_match, ofac_overridden, created_at, updated_at, posted_at,
                    returned_at, expiry_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.inbound_id, row.rrn, row.userid, row.destination_account,
                    row.amount_inr, row.sender_name, row.send_ifsc, row.recv_ifsc, row.rail,
                    row.vpa, row.ifsc, row.account_number, row.mmid, row.phone, row.pan,
                    row.fingerprint, row.purpose, row.status, row.reason, row.actor, row.releaser,
                    int(row.ofac_hit), row.ofac_match, int(row.ofac_overridden), row.created_at,
                    row.updated_at, row.posted_at, row.returned_at, row.expiry_at, row.note,
                ),
            )
            conn.commit()

    def get_inbound(self, inbound_id: str) -> Optional[UrecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,)).fetchone()
        return _inbound_from_row(row) if row else None

    def get_by_rrn(self, rrn: str) -> Optional[UrecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE rrn = ?', (rrn,)).fetchone()
        return _inbound_from_row(row) if row else None

    def update_inbound(self, row: UrecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE inbounds SET userid=?, destination_account=?, amount_inr=?, sender_name=?,
                    send_ifsc=?, recv_ifsc=?, rail=?, vpa=?, ifsc=?, account_number=?, mmid=?,
                    phone=?, pan=?, fingerprint=?, purpose=?, status=?, reason=?, actor=?, releaser=?,
                    ofac_hit=?, ofac_match=?, ofac_overridden=?, updated_at=?, posted_at=?,
                    returned_at=?, expiry_at=?, note=?
                WHERE inbound_id=?
                """,
                (
                    row.userid, row.destination_account, row.amount_inr, row.sender_name,
                    row.send_ifsc, row.recv_ifsc, row.rail, row.vpa, row.ifsc, row.account_number,
                    row.mmid, row.phone, row.pan, row.fingerprint, row.purpose, row.status,
                    row.reason, row.actor, row.releaser, int(row.ofac_hit), row.ofac_match,
                    int(row.ofac_overridden), row.updated_at, row.posted_at, row.returned_at,
                    row.expiry_at, row.note, row.inbound_id,
                ),
            )
            conn.commit()

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[UrecvInbound]:
        clauses = []
        args: List[Any] = []
        if userid is not None:
            clauses.append('userid = ?')
            args.append(userid)
        if status is not None:
            clauses.append('status = ?')
            args.append(status)
        sql = 'SELECT * FROM inbounds'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        return [_inbound_from_row(row) for row in rows]


class UrecvService:
    def __init__(
        self,
        policy: UrecvPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[NpciClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: datetime.now(tz=timezone.utc).timestamp())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn or screen_name
        self.calendar = calendar or NpciClock(
            open_hour=policy.open_hour,
            cutoff_hour=policy.cutoff_hour,
            tz_offset_minutes=policy.tz_offset_minutes,
            extra_holidays=policy.extra_holidays,
        )

    def _now(self) -> float:
        return float(self.clock())

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise UrecvError('urecv_disabled', 'UPI receive is disabled.')

    def _require_staff(self, actor_type: str) -> None:
        if actor_type not in EMPLOYEE_ROLES:
            raise UrecvError('urecv_forbidden', 'Staff only.')

    def _require_customer_manage(self, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if not self.policy.customer_manage:
            raise UrecvError('urecv_forbidden', 'Customers cannot manage UPI receive keys.')

    def _accounts(self, userid: str) -> Any:
        if not self.accounts_fn:
            return {}
        return self.accounts_fn(userid) or {}

    def _owned(self, userid: str, account: str) -> None:
        owned = own_accounts_from_customer_payload(self._accounts(userid))
        if account not in owned:
            raise UrecvError('invalid_account', 'Destination is not an account of this customer.')

    def _account_type(self, userid: str, account: str) -> str:
        return account_types_from_customer_payload(self._accounts(userid)).get(account, '')

    def _credit_allowed(self, userid: str, account: str) -> bool:
        kind = self._account_type(userid, account)
        if kind == 'credit' and not self.policy.allow_credit:
            return False
        return True

    def _fingerprint_for(
        self, rail: str, vpa: str, ifsc: str, account_number: str, mmid: str, phone: str,
    ) -> str:
        if rail == 'upi':
            return destination_fingerprint('upi', vpa, '', '', '', '')
        if rail == 'imps' and mmid:
            return destination_fingerprint('imps', '', '', '', mmid, phone)
        return destination_fingerprint(rail, '', ifsc, account_number, '', '')

    def _normalize_key_fields(
        self, *, rail: Any, vpa: Any = '', ifsc: Any = '', account_number: Any = '',
        mmid: Any = '', phone: Any = '', pan: Any = '',
    ) -> Tuple[str, str, str, str, str, str, str]:
        kind = normalize_rail(rail)
        mmid_value = normalize_mmid(mmid, required=False)
        ifsc_needed = kind in {'neft', 'rtgs'} or (kind == 'imps' and not mmid_value)
        vpa_value = normalize_vpa(vpa, required=kind == 'upi', psps=self.policy.psps)
        ifsc_value = normalize_ifsc(ifsc, required=ifsc_needed)
        account_value = normalize_external_account(
            account_number, required=bool(ifsc_value) and kind != 'upi',
        )
        if kind == 'imps' and not mmid_value:
            mmid_value = normalize_mmid(mmid, required=False)
        elif kind == 'imps' and not ifsc_value:
            mmid_value = normalize_mmid(mmid, required=True)
        phone_value = normalize_phone(phone, required=kind == 'imps' and bool(mmid_value))
        pan_value = normalize_pan(pan, required=kind in {'neft', 'rtgs'})
        if kind == 'upi' and not vpa_value:
            raise UrecvError('missing_key', 'VPA is required for UPI collect.')
        if ifsc_needed and ifsc_value != self.policy.receiver_ifsc:
            raise UrecvError('wrong_receiver', 'IFSC is not issued by this NPCI participant.')
        return kind, vpa_value, ifsc_value, account_value, mmid_value, phone_value, pan_value

    def add_key(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: str,
        rail: Any,
        destination_account: Any,
        vpa: Any = '',
        ifsc: Any = '',
        account_number: Any = '',
        mmid: Any = '',
        phone: Any = '',
        pan: Any = '',
    ) -> UrecvKey:
        self._require_enabled()
        self._require_customer_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise UrecvError('urecv_forbidden', 'Cannot enroll a UPI key for another customer.')
        kind, vpa_value, ifsc_value, account_value, mmid_value, phone_value, pan_value = self._normalize_key_fields(
            rail=rail, vpa=vpa, ifsc=ifsc, account_number=account_number,
            mmid=mmid, phone=phone, pan=pan,
        )
        account = normalize_account(destination_account)
        self._owned(owner_userid, account)
        if not self._credit_allowed(owner_userid, account):
            raise UrecvError('credit_not_allowed', 'Credit accounts cannot receive UPI.')
        fingerprint = self._fingerprint_for(
            kind, vpa_value, ifsc_value, account_value, mmid_value, phone_value,
        )
        existing = self.store.get_key_by_fingerprint(fingerprint)
        if existing and existing.status in OPEN_KEY:
            raise UrecvError('key_duplicate', 'That UPI / IMPS / IFSC key is already enrolled.')
        open_rows = [row for row in self.store.list_keys(owner_userid) if row.status in OPEN_KEY]
        if any(row.nickname.lower() == normalize_nickname(nickname).lower() for row in open_rows):
            raise UrecvError('key_duplicate', 'That nickname is already in use.')
        if len(open_rows) >= self.policy.max_keys:
            raise UrecvError('key_limit', 'UPI receive key limit reached.')
        now = self._now()
        row = UrecvKey(
            key_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=normalize_nickname(nickname),
            rail=kind,
            vpa=vpa_value,
            ifsc=ifsc_value,
            account_number=account_value,
            mmid=mmid_value,
            phone=phone_value,
            pan=pan_value,
            fingerprint=fingerprint,
            destination_account=account,
            status=KEY_ACTIVE,
            actor=actor,
            created_at=now,
            updated_at=now,
        )
        self.store.put_key(row)
        self._rematch_unmatched(row)
        return row

    def set_key_status(
        self,
        *,
        key_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> UrecvKey:
        self._require_enabled()
        row = self.store.get_key(key_id)
        if row is None:
            raise UrecvError('key_not_found', 'UPI receive key enrollment not found.')
        if actor_type not in EMPLOYEE_ROLES and actor != row.userid:
            raise UrecvError('urecv_forbidden', 'Cannot change another customer UPI key.')
        self._require_customer_manage(actor_type)
        if status not in KEY_STATUSES:
            raise UrecvError('invalid_status', 'Unknown UPI key status.')
        if row.status == KEY_CANCELLED:
            raise UrecvError('already_cancelled', 'Cancelled UPI keys cannot change.')
        if status == KEY_PAUSED and row.status != KEY_ACTIVE:
            raise UrecvError('already_paused' if row.status == KEY_PAUSED else 'invalid_status', 'Key is not active.')
        if status == KEY_ACTIVE and row.status != KEY_PAUSED:
            raise UrecvError('already_active', 'Key is already active.')
        if status == row.status:
            code = 'already_paused' if status == KEY_PAUSED else 'already_active'
            raise UrecvError(code, 'UPI key already in that status.')
        updated = replace(row, status=status, actor=actor, updated_at=self._now())
        self.store.update_key(updated)
        if status == KEY_ACTIVE:
            self._rematch_unmatched(updated)
        return updated

    def _rematch_unmatched(self, enrollment: UrecvKey) -> None:
        if enrollment.status != KEY_ACTIVE:
            return
        for inbound in self.store.list_inbounds(status=IN_UNMATCHED):
            if inbound.fingerprint != enrollment.fingerprint:
                continue
            inbound.userid = enrollment.userid
            inbound.destination_account = enrollment.destination_account
            inbound.updated_at = self._now()
            self.store.update_inbound(inbound)
            self._evaluate(inbound, rescreen=True)

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        values: Dict[str, Any],
    ) -> Tuple[UrecvInbound, bool]:
        self._require_enabled()
        self._require_staff(actor_type)
        parsed = message_from_values(values, psps=self.policy.psps)
        return self._ingest_parsed(actor=actor, parsed=parsed)

    def ingest_file(
        self,
        *,
        actor: str,
        actor_type: str,
        payload: str,
        sender_name: str = '',
    ) -> Tuple[List[UrecvInbound], int]:
        self._require_enabled()
        self._require_staff(actor_type)
        created = 0
        rows = []
        fallback = ''
        if sender_name:
            fallback = normalize_legal_name(sender_name)
        for record in split_upi1_file(payload):
            parsed = parse_record(record)
            if not parsed.get('recv_ifsc'):
                parsed['recv_ifsc'] = self.policy.receiver_ifsc
            if fallback:
                parsed['name'] = fallback
            elif not parsed.get('name'):
                labels = {'upi': 'UPI PAYER', 'imps': 'IMPS PAYER', 'neft': 'NEFT PAYER', 'rtgs': 'RTGS PAYER'}
                parsed['name'] = labels.get(parsed.get('rail'), 'UPI PAYER')
            row, is_new = self._ingest_parsed(actor=actor, parsed=parsed)
            rows.append(row)
            if is_new:
                created += 1
        return rows, created

    def _assert_amount(self, rail: str, amount: Decimal) -> None:
        if amount < self.policy.min_amount:
            raise UrecvError('amount_out_of_range', 'Amount is outside the UPI range.')
        if rail == 'upi' and amount > self.policy.upi_max:
            raise UrecvError('upi_amount_exceeded', 'Amount is outside the UPI range.')
        if rail == 'imps' and amount > self.policy.imps_max:
            raise UrecvError('imps_amount_exceeded', 'Amount is outside the IMPS range.')
        if rail == 'neft' and amount > self.policy.neft_max:
            raise UrecvError('neft_amount_exceeded', 'Amount is outside the NEFT range.')
        if rail == 'rtgs':
            if amount < self.policy.rtgs_min:
                raise UrecvError('rtgs_amount_below_minimum', 'RTGS amount is below the minimum.')
            if amount > self.policy.rtgs_max:
                raise UrecvError('rtgs_amount_exceeded', 'Amount is outside the RTGS range.')

    def _ingest_parsed(self, *, actor: str, parsed: Dict[str, Any]) -> Tuple[UrecvInbound, bool]:
        recv = parsed.get('recv_ifsc') or self.policy.receiver_ifsc
        if recv != self.policy.receiver_ifsc:
            raise UrecvError('wrong_receiver', 'UPI credit is not for this NPCI participant.')
        existing = self.store.get_by_rrn(parsed['rrn'])
        if existing is not None:
            return existing, False
        total = len(self.store.list_inbounds())
        if total >= self.policy.max_inbounds:
            raise UrecvError('inbound_limit', 'Inbound UPI limit reached.')
        amount = parsed['amount'] if isinstance(parsed['amount'], Decimal) else parse_money(parsed['amount'])
        rail = normalize_rail(parsed.get('rail') or 'upi')
        self._assert_amount(rail, amount)
        now = self._now()
        vpa = parsed.get('vpa') or ''
        ifsc = parsed.get('ifsc') or ''
        account_number = parsed.get('account_number') or ''
        mmid = parsed.get('mmid') or ''
        phone = parsed.get('phone') or ''
        fingerprint = self._fingerprint_for(rail, vpa, ifsc, account_number, mmid, phone)
        row = UrecvInbound(
            inbound_id=uuid.uuid4().hex,
            rrn=parsed['rrn'],
            userid='',
            destination_account='',
            amount_inr=money_str(amount),
            sender_name=parsed.get('name') or 'UPI PAYER',
            send_ifsc=parsed.get('send_ifsc') or '',
            recv_ifsc=recv,
            rail=rail,
            vpa=vpa,
            ifsc=ifsc,
            account_number=account_number,
            mmid=mmid,
            phone=phone,
            pan=parsed.get('pan') or '',
            fingerprint=fingerprint,
            purpose=parsed.get('purpose') or 'other',
            status=IN_UNMATCHED,
            reason='',
            actor=actor,
            releaser='',
            ofac_hit=False,
            ofac_match='',
            ofac_overridden=False,
            created_at=now,
            updated_at=now,
            expiry_at=now + self.policy.claim_seconds,
        )
        enrollment = self.store.get_key_by_fingerprint(fingerprint)
        if enrollment is not None and enrollment.status == KEY_ACTIVE:
            row.userid = enrollment.userid
            row.destination_account = enrollment.destination_account
        self.store.put_inbound(row)
        self._evaluate(row, rescreen=True)
        return row, True

    def _should_queue(self, row: UrecvInbound, amount: Decimal) -> bool:
        _ = amount
        if row.rail in {'upi', 'imps'}:
            return False
        now = self._now()
        if row.rail == 'neft':
            return not self.calendar.is_business_day(self.calendar.local_dt(now).date())
        if row.rail == 'rtgs':
            return not self.calendar.is_session(now)
        return False

    def _evaluate(self, row: UrecvInbound, *, rescreen: bool) -> UrecvInbound:
        now = self._now()
        if row.status in TERMINAL_IN:
            return row
        if row.status == IN_UNMATCHED and row.expiry_at and now > row.expiry_at:
            row.status = IN_EXPIRED
            row.reason = 'expired'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if not row.userid or not row.destination_account:
            row.status = IN_UNMATCHED
            row.reason = 'unmatched'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        try:
            self._owned(row.userid, row.destination_account)
        except UrecvError:
            row.status = IN_UNMATCHED
            row.reason = 'unmatched'
            row.userid = ''
            row.destination_account = ''
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if not self._credit_allowed(row.userid, row.destination_account):
            row.status = IN_UNMATCHED
            row.reason = 'credit_not_allowed'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if rescreen and not row.ofac_overridden:
            hit = self.screen_fn(row.sender_name, watchlist=self.policy.watchlist)
            row.ofac_hit = bool(hit.hit)
            row.ofac_match = hit.matched
            if row.ofac_hit:
                row.status = IN_HELD
                row.reason = 'ofac'
                row.updated_at = now
                self.store.update_inbound(row)
                return row
        elif row.ofac_hit and not row.ofac_overridden:
            row.status = IN_HELD
            row.reason = 'ofac'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        inr = parse_money(row.amount_inr)
        if inr >= self.policy.dual_control_threshold and not row.releaser:
            row.status = IN_PENDING
            row.reason = 'dual_control'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if self._should_queue(row, inr):
            row.status = IN_QUEUED
            row.reason = 'session_closed'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        return self._post(row)

    def _post(self, row: UrecvInbound) -> UrecvInbound:
        if self.credit_fn is None:
            raise UrecvError('failed', 'Credit function is not configured.')
        remark = '%s from %s' % (row.rail, row.sender_name[:40])
        result = self.credit_fn(row.destination_account, row.amount_inr, remark)
        classified = _classify_money_result(result)
        now = self._now()
        if classified == 'nsf':
            row.status = IN_FAILED
            row.reason = 'nsf'
            row.updated_at = now
            self.store.update_inbound(row)
            raise UrecvError('nsf', 'Credit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            row.status = IN_FAILED
            row.reason = 'failed'
            row.updated_at = now
            self.store.update_inbound(row)
            raise UrecvError('failed', 'Credit failed.', inbound=row)
        row.status = IN_POSTED
        row.reason = ''
        row.posted_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def preview(self, *, inbound_id: str = '', rrn: str = '', actor: str = '', actor_type: str = '') -> Dict[str, Any]:
        row = self._get(inbound_id=inbound_id, rrn=rrn)
        self._assert_visible(row, actor, actor_type)
        return {
            'inbound': row.to_dict(),
            'clock': self.calendar.snapshot(self._now()),
            'receiver': mask_ifsc(self.policy.receiver_ifsc),
        }

    def _get(self, *, inbound_id: str = '', rrn: str = '') -> UrecvInbound:
        row = None
        if inbound_id:
            row = self.store.get_inbound(str(inbound_id).strip())
        if row is None and rrn:
            row = self.store.get_by_rrn(normalize_rrn(rrn))
        if row is None:
            raise UrecvError('inbound_not_found', 'Inbound UPI not found.')
        return row

    def _assert_visible(self, row: UrecvInbound, actor: str, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if row.userid and row.userid != actor:
            raise UrecvError('urecv_forbidden', 'Cannot view another customer UPI.')

    def assign(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        customer_id: str,
        account: Any,
        nickname: str = '',
    ) -> UrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_UNMATCHED:
            raise UrecvError('not_assignable', 'Only unmatched UPI credits can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise UrecvError('missing_customer_id', 'Customer id is required to assign.')
        dest = normalize_account(account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise UrecvError('credit_not_allowed', 'Credit accounts cannot receive UPI.')
        existing = self.store.get_key_by_fingerprint(row.fingerprint)
        if existing is None or existing.status == KEY_CANCELLED:
            ifsc_value = row.ifsc
            if row.rail in {'neft', 'rtgs'} or (row.rail == 'imps' and not row.mmid):
                ifsc_value = row.ifsc or self.policy.receiver_ifsc
            self.add_key(
                owner_userid=owner, actor=actor, actor_type=actor_type,
                nickname=nickname or ('UPI %s' % row.rail),
                rail=row.rail, vpa=row.vpa, ifsc=ifsc_value,
                account_number=row.account_number, mmid=row.mmid, phone=row.phone,
                pan=row.pan, destination_account=dest,
            )
            fresh = self.store.get_inbound(row.inbound_id)
            if fresh is not None and fresh.status != IN_UNMATCHED:
                return fresh
        elif existing.userid != owner:
            raise UrecvError('key_duplicate', 'UPI key belongs to another customer.')
        row.userid = owner
        row.destination_account = dest
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=True)

    def claim(
        self,
        *,
        actor: str,
        actor_type: str,
        inbound_id: str = '',
        rail: Any = '',
        vpa: Any = '',
        ifsc: Any = '',
        account_number: Any = '',
        mmid: Any = '',
        phone: Any = '',
        pan: Any = '',
        destination_account: Any = '',
        nickname: str = '',
        owner_userid: str = '',
    ) -> UrecvInbound:
        """VPA / MMID / IFSC claim: customer presents the matching key for an unmatched credit."""
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_claim:
                raise UrecvError('urecv_forbidden', 'Customers cannot claim UPI credits.')
            owner = actor
        else:
            owner = str(owner_userid or '').strip()
            if not owner:
                raise UrecvError('missing_customer_id', 'Customer id is required to claim.')
        guessed = 'upi'
        if mmid and not vpa:
            guessed = 'imps'
        elif ifsc and not vpa:
            guessed = 'neft'
        kind, vpa_value, ifsc_value, account_value, mmid_value, phone_value, pan_value = self._normalize_key_fields(
            rail=rail or guessed, vpa=vpa, ifsc=ifsc, account_number=account_number,
            mmid=mmid, phone=phone, pan=pan,
        )
        fingerprint = self._fingerprint_for(
            kind, vpa_value, ifsc_value, account_value, mmid_value, phone_value,
        )
        dest = normalize_account(destination_account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise UrecvError('credit_not_allowed', 'Credit accounts cannot receive UPI.')
        row = None
        if inbound_id:
            row = self._get(inbound_id=inbound_id)
            if row.fingerprint != fingerprint:
                raise UrecvError('not_claimable', 'Presented UPI key does not match this credit.')
        else:
            for candidate in self.store.list_inbounds(status=IN_UNMATCHED):
                if candidate.fingerprint == fingerprint:
                    row = candidate
                    break
        if row is None:
            raise UrecvError('inbound_not_found', 'No unmatched UPI for that key.')
        if row.status != IN_UNMATCHED:
            raise UrecvError('not_claimable', 'This UPI credit is not waiting for a claim.')
        if row.expiry_at and self._now() > row.expiry_at:
            row.status = IN_EXPIRED
            row.reason = 'expired'
            row.updated_at = self._now()
            self.store.update_inbound(row)
            raise UrecvError('return_window_closed', 'UPI claim window has closed.')
        existing = self.store.get_key_by_fingerprint(fingerprint)
        if existing is None or existing.status == KEY_CANCELLED:
            self.add_key(
                owner_userid=owner, actor=actor, actor_type=actor_type,
                nickname=nickname or ('UPI %s' % kind),
                rail=kind, vpa=vpa_value or row.vpa, ifsc=ifsc_value or row.ifsc,
                account_number=account_value or row.account_number,
                mmid=mmid_value or row.mmid, phone=phone_value or row.phone,
                pan=pan_value or row.pan, destination_account=dest,
            )
        elif existing.userid != owner:
            raise UrecvError('key_duplicate', 'UPI key belongs to another customer.')
        elif existing.status == KEY_PAUSED:
            self.set_key_status(key_id=existing.key_id, actor=actor, actor_type=actor_type, status=KEY_ACTIVE)
        fresh = self.store.get_inbound(row.inbound_id)
        if fresh is not None and fresh.status != IN_UNMATCHED:
            return fresh
        row.userid = owner
        row.destination_account = dest
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=True)

    def override_ofac(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> UrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_HELD:
            raise UrecvError('not_overridable', 'Only OFAC-held UPI credits can be overridden.')
        row.ofac_overridden = True
        row.ofac_hit = False
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def release(self, *, inbound_id: str, actor: str, actor_type: str) -> UrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_PENDING:
            raise UrecvError('not_releasable', 'Only dual-control UPI credits can be released.')
        if actor == row.actor:
            raise UrecvError('same_approver', 'A different employee must release this credit.')
        row.releaser = actor
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def reject(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> UrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status not in OPEN_IN:
            raise UrecvError('not_rejectable', 'This UPI credit cannot be rejected.')
        row.status = IN_REJECTED
        row.reason = 'rejected'
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return row

    def export(self, *, inbound_id: str, actor: str, actor_type: str) -> Dict[str, Any]:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if not row.rrn:
            raise UrecvError('not_exportable', 'UPI credit has no RRN to export.')
        _ = actor
        record = compose_upi1(
            rrn=row.rrn,
            vpa=row.vpa,
            ifsc=row.ifsc,
            account_number=row.account_number,
            mmid=row.mmid,
            phone=row.phone,
            pan=row.pan,
            amount=parse_money(row.amount_inr, allow_zero=True),
            purpose=row.purpose,
            status=row.status,
            rail=row.rail,
            mask=True,
        )
        extra = ''
        if row.rail == 'imps' and row.mmid and row.phone:
            extra = compose_imps1(
                mmid=row.mmid,
                phone=row.phone,
                amount=parse_money(row.amount_inr, allow_zero=True),
                rrn=row.rrn,
                mask=True,
            )
        return {'file': record if not extra else record + '\n' + extra, 'inbound': row.to_dict()}

    def return_inbound(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: str = 'cust',
    ) -> UrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        return self._return(row, actor=actor, actor_type=actor_type, reason=reason, honor_window=False)

    def request_return(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: str = 'cust',
    ) -> UrecvInbound:
        self._require_enabled()
        row = self._get(inbound_id=inbound_id)
        self._assert_visible(row, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_return:
                raise UrecvError('urecv_forbidden', 'Customers cannot return UPI credits.')
            if actor != row.userid:
                raise UrecvError('urecv_forbidden', 'Cannot return another customer UPI.')
        return self._return(row, actor=actor, actor_type=actor_type, reason=reason, honor_window=True)

    def _assert_complaint_window(self, row: UrecvInbound, *, actor_type: str, honor_window: bool) -> None:
        now = self._now()
        completed = float(row.posted_at or row.created_at)
        if actor_type in EMPLOYEE_ROLES:
            if now - completed > self.policy.staff_complaint_days * 86400:
                raise UrecvError('complaint_window_closed', 'Staff UPI complaint window has closed.')
            return
        if not honor_window:
            return
        if row.rail == 'upi':
            if now - completed > self.policy.customer_upi_return_seconds:
                raise UrecvError('return_window_closed', 'Customer UPI return window has closed.')
            return
        completed_day = self.calendar.local_dt(completed).date()
        deadline_day = self.calendar.next_business_day(completed_day)
        deadline = self.calendar.session_close_ts(deadline_day)
        if now > deadline:
            raise UrecvError('return_window_closed', 'Customer NPCI complaint window has closed.')

    def _return(
        self,
        row: UrecvInbound,
        *,
        actor: str,
        actor_type: str,
        reason: str,
        honor_window: bool,
    ) -> UrecvInbound:
        now = self._now()
        _ = actor
        if row.status == IN_RETURNED or row.returned_at:
            raise UrecvError('already_returned', 'UPI credit already returned.')
        if row.status in OPEN_IN:
            row.status = IN_RETURNED
            row.reason = reason or 'cust'
            row.returned_at = now
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if row.status != IN_POSTED:
            raise UrecvError('not_returnable', 'This UPI credit cannot be returned.')
        self._assert_complaint_window(row, actor_type=actor_type, honor_window=honor_window)
        if self.debit_fn is None:
            raise UrecvError('return_failed', 'Debit function is not configured.')
        remark = '%s return %s' % (row.rail, row.rrn[-12:])
        result = self.debit_fn(row.destination_account, row.amount_inr, remark)
        classified = _classify_money_result(result)
        if classified == 'nsf':
            raise UrecvError('nsf', 'Return debit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            raise UrecvError('return_failed', 'Return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.reason = reason or 'cust'
        row.returned_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[UrecvInbound]:
        now = self._now()
        changed = []
        rows = self.store.list_inbounds(userid) if userid else self.store.list_inbounds()
        for row in rows:
            if row.status == IN_UNMATCHED and row.expiry_at and now > row.expiry_at:
                row.status = IN_EXPIRED
                row.reason = 'expired'
                row.updated_at = now
                self.store.update_inbound(row)
                changed.append(row)
                continue
            if row.status == IN_QUEUED:
                posted = self._evaluate(row, rescreen=False)
                if posted.status != IN_QUEUED:
                    changed.append(posted)
        return changed

    def snapshot(self, userid: str, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        _ = actor
        _ = actor_type
        now = self._now()
        if userid:
            self.run_due(userid)
        keys = self.store.list_keys(userid) if userid else []
        inbounds = self.store.list_inbounds(userid) if userid else []
        received = [row for row in inbounds if row.status == IN_POSTED]
        ytd = sum((parse_money(row.amount_inr, allow_zero=True) for row in received), Decimal('0.00'))
        return {
            'enabled': self.policy.enabled,
            'clock': self.calendar.snapshot(now),
            'receiver': mask_ifsc(self.policy.receiver_ifsc),
            'keys': [row.to_dict() for row in keys[:40]],
            'inbounds': [row.to_dict() for row in inbounds[:40]],
            'ytd_received': money_str(ytd),
            'open_count': sum(1 for row in inbounds if row.status in OPEN_IN),
            'active_keys': sum(1 for row in keys if row.status == KEY_ACTIVE),
            'min_amount': money_str(self.policy.min_amount),
            'upi_max': money_str(self.policy.upi_max),
            'imps_max': money_str(self.policy.imps_max),
            'neft_max': money_str(self.policy.neft_max),
            'rtgs_min': money_str(self.policy.rtgs_min),
            'rtgs_max': money_str(self.policy.rtgs_max),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'settlement_method': 'upi',
            'currency': 'INR',
        }

    def unmatched_snapshot(self) -> Dict[str, Any]:
        rows = self.store.list_inbounds(status=IN_UNMATCHED)
        return {
            'enabled': self.policy.enabled,
            'unmatched': [row.to_dict() for row in rows[:40]],
            'count': len(rows),
        }


_SERVICE: Optional[UrecvService] = None


def set_service(service: Optional[UrecvService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[UrecvService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('URECV_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryUrecvStore()
    path = os.environ.get('URECV_DB', DEFAULT_STORE_PATH)
    return SqliteUrecvStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[NpciClock] = None,
) -> UrecvService:
    if store is None:
        store = default_store()
    return UrecvService(
        UrecvPolicy.from_env(),
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


def _owner_from(actor: str, actor_type: str, values: Dict[str, Any]) -> str:
    if actor_type in EMPLOYEE_ROLES:
        return str(values.get('customer_id') or values.get('owner') or '').strip()
    return actor


def _error_status(code: str) -> int:
    return {
        'key_duplicate': 409,
        'key_limit': 409,
        'inbound_limit': 409,
        'already_cancelled': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_returned': 409,
        'nsf': 409,
        'failed': 409,
        'return_failed': 409,
        'urecv_forbidden': 403,
        'urecv_disabled': 403,
        'credit_not_allowed': 403,
        'same_approver': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_returnable': 403,
        'not_claimable': 403,
        'not_exportable': 403,
        'return_window_closed': 403,
        'complaint_window_closed': 403,
        'key_not_found': 404,
        'inbound_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_ifsc': 400,
        'invalid_vpa': 400,
        'invalid_mmid': 400,
        'invalid_pan': 400,
        'invalid_phone': 400,
        'invalid_rail': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_reference': 400,
        'invalid_file': 400,
        'invalid_currency': 400,
        'wrong_receiver': 400,
        'upi_amount_exceeded': 400,
        'imps_amount_exceeded': 400,
        'neft_amount_exceeded': 400,
        'rtgs_amount_exceeded': 400,
        'rtgs_amount_below_minimum': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_key': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: UrecvError) -> Dict[str, Any]:
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
    except UrecvError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snapshot_for(service: UrecvService, userid: str, actor: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(userid, actor=actor, actor_type=actor_type)


def handle_list(service: UrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = _owner_from(userid, actor_type, values)
        if not owner:
            return jsonify({'message': 'Customer id required', 'error': 'missing_customer_id'}), 400
    return jsonify({'UpiIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def handle_unmatched(service: UrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'urecv_forbidden'}), 403
    snap = service.unmatched_snapshot()
    return jsonify({'UnmatchedUpiIns': snap, 'UpiIn': snap}), 200


def handle_add_key(service: UrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        owner = userid
        if actor_type in EMPLOYEE_ROLES:
            owner = _owner_from(userid, actor_type, values)
            if not owner:
                raise UrecvError('missing_customer_id', 'Customer id is required.')
        row = service.add_key(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname') or '',
            rail=values.get('rail') or values.get('kind') or 'upi',
            vpa=values.get('vpa') or values.get('virtual_address'),
            ifsc=values.get('ifsc') or values.get('destination_ifsc'),
            account_number=values.get('account_number') or values.get('external_account'),
            mmid=values.get('mmid'),
            phone=values.get('phone') or values.get('mobile'),
            pan=values.get('pan'),
            destination_account=values.get('destination_account') or values.get('account') or values.get('default_account'),
        )
        return jsonify({
            'message': 'UPI receive key enrolled',
            'key': row.to_dict(),
            'UpiIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _key_status_route(service: UrecvService, status: str, message: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        key_id = str(values.get('key_id') or '').strip()
        if not key_id:
            raise UrecvError('missing_key', 'key_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.set_key_status(key_id=key_id, actor=userid, actor_type=actor_type, status=status)
        return jsonify({
            'message': message,
            'key': row.to_dict(),
            'UpiIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: UrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        preview = service.preview(
            inbound_id=str(values.get('inbound_id') or ''),
            rrn=str(values.get('rrn') or values.get('tracking_key') or values.get('utr') or ''),
            actor=userid,
            actor_type=actor_type,
        )
        owner = preview['inbound'].get('userid') or userid
        return jsonify({'preview': preview, 'UpiIn': _snapshot_for(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest(service: UrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'UPI ingested' if created else 'UPI already ingested',
            'inbound': row.to_dict(),
            'UpiIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_ingest_file(service: UrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        payload = values.get('file') or values.get('body') or values.get('payload')
        if not payload:
            raise UrecvError('missing_file', 'UPI1 file body is required.')
        actor_type = session.get('usertype') or 'customer'
        rows, created = service.ingest_file(
            actor=userid, actor_type=actor_type, payload=str(payload),
            sender_name=str(values.get('name') or values.get('sender_name') or ''),
        )
        owner = rows[-1].userid if rows and rows[-1].userid else userid
        return jsonify({
            'message': 'UPI1 file ingested',
            'created': created,
            'inbounds': [row.to_dict() for row in rows],
            'UpiIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_assign(service: UrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise UrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.assign(
            inbound_id=inbound_id,
            actor=userid,
            actor_type=actor_type,
            customer_id=str(values.get('customer_id') or ''),
            account=values.get('account') or values.get('destination_account'),
            nickname=str(values.get('nickname') or ''),
        )
        return jsonify({
            'message': 'UPI assigned',
            'inbound': row.to_dict(),
            'UpiIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_claim(service: UrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        row = service.claim(
            actor=userid,
            actor_type=actor_type,
            inbound_id=str(values.get('inbound_id') or ''),
            rail=values.get('rail') or values.get('kind'),
            vpa=values.get('vpa') or values.get('virtual_address'),
            ifsc=values.get('ifsc'),
            account_number=values.get('account_number'),
            mmid=values.get('mmid'),
            phone=values.get('phone') or values.get('mobile'),
            pan=values.get('pan'),
            destination_account=values.get('destination_account') or values.get('account'),
            nickname=str(values.get('nickname') or ''),
            owner_userid=str(values.get('customer_id') or ''),
        )
        return jsonify({
            'message': 'UPI claimed',
            'inbound': row.to_dict(),
            'UpiIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_inbound_route(service: UrecvService, action: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise UrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        if action == 'override':
            row = service.override_ofac(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'release':
            row = service.release(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            message = 'UPI released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'UPI rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=str(values.get('reason') or 'cust'),
            )
            message = 'UPI returned'
        elif action == 'export':
            exported = service.export(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            owner = exported['inbound'].get('userid') or userid
            return jsonify({
                'message': 'UPI exported',
                'file': exported['file'],
                'inbound': exported['inbound'],
                'UpiIn': _snapshot_for(service, owner, userid, actor_type),
            }), 200
        else:
            raise UrecvError('invalid_status', 'Unknown UPI action.')
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'UpiIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: UrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise UrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.request_return(
            inbound_id=inbound_id, actor=userid, actor_type=actor_type,
            reason=str(values.get('reason') or 'cust'),
        )
        return jsonify({
            'message': 'UPI return requested',
            'inbound': row.to_dict(),
            'UpiIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: UrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'UpiIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def attach_urecv_routes(app, service: UrecvService) -> None:
    @app.route('/listUpiIns', methods=['POST', 'GET'])
    def list_upi_ins_route():
        return handle_list(service)

    @app.route('/listUpiRecvKeys', methods=['POST', 'GET'])
    def list_upi_recv_keys_route():
        return handle_list(service)

    @app.route('/listUnmatchedUpiIns', methods=['POST', 'GET'])
    def list_unmatched_upi_ins_route():
        return handle_unmatched(service)

    @app.route('/addUpiRecvKey', methods=['POST', 'GET'])
    def add_upi_recv_key_route():
        return handle_add_key(service)

    @app.route('/pauseUpiRecvKey', methods=['POST', 'GET'])
    def pause_upi_recv_key_route():
        return _key_status_route(service, KEY_PAUSED, 'UPI receive key paused')

    @app.route('/resumeUpiRecvKey', methods=['POST', 'GET'])
    def resume_upi_recv_key_route():
        return _key_status_route(service, KEY_ACTIVE, 'UPI receive key resumed')

    @app.route('/cancelUpiRecvKey', methods=['POST', 'GET'])
    def cancel_upi_recv_key_route():
        return _key_status_route(service, KEY_CANCELLED, 'UPI receive key cancelled')

    @app.route('/previewUpiIn', methods=['POST', 'GET'])
    def preview_upi_in_route():
        return handle_preview(service)

    @app.route('/ingestUpiIn', methods=['POST', 'GET'])
    def ingest_upi_in_route():
        return handle_ingest(service)

    @app.route('/ingestUpiInFile', methods=['POST', 'GET'])
    def ingest_upi_in_file_route():
        return handle_ingest_file(service)

    @app.route('/claimUpiIn', methods=['POST', 'GET'])
    def claim_upi_in_route():
        return handle_claim(service)

    @app.route('/assignUpiIn', methods=['POST', 'GET'])
    def assign_upi_in_route():
        return handle_assign(service)

    @app.route('/overrideUpiInOfac', methods=['POST', 'GET'])
    def override_upi_in_ofac_route():
        return _staff_inbound_route(service, 'override')

    @app.route('/releaseUpiIn', methods=['POST', 'GET'])
    def release_upi_in_route():
        return _staff_inbound_route(service, 'release')

    @app.route('/rejectUpiIn', methods=['POST', 'GET'])
    def reject_upi_in_route():
        return _staff_inbound_route(service, 'reject')

    @app.route('/returnUpiIn', methods=['POST', 'GET'])
    def return_upi_in_route():
        return _staff_inbound_route(service, 'return')

    @app.route('/exportUpiIn', methods=['POST', 'GET'])
    def export_upi_in_route():
        return _staff_inbound_route(service, 'export')

    @app.route('/requestUpiInReturn', methods=['POST', 'GET'])
    def request_upi_in_return_route():
        return handle_request_return(service)

    @app.route('/runDueUpiIns', methods=['POST', 'GET'])
    def run_due_upi_ins_route():
        return handle_run_due(service)


