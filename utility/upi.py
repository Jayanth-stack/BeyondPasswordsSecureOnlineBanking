"""UPI / IMPS / NEFT / RTGS origination over NPCI and RBI rails.

Customers send INR payments to a Virtual Payment Address (UPI), an
IMPS MMID+mobile or IFSC+account destination, or a NEFT/RTGS credit
to an IFSC. Independent of domestic Fedwire (PR #73), SPEI/CoDi
(PR #141/#143), PIX Dict keys (PR #137/#139), CHIPS, Zelle, SWIFT,
and ACH linking (PR #68). Existing `/fundTransfer`, `/withdrawAmount`,
and `/sendWire` stay unchanged. `Customers.debit_request` /
`credit_request` still write `debited` / `direct deposited` unless a
remark is supplied here.

Foundations (reusable beyond this screen):
- IFSC (11-char `[A-Z]{4}0[A-Z0-9]{6}`, 5th character always 0)
- VPA `local@psp` against an NPCI PSP handle directory
- MMID (7-digit IMPS) + Indian +91 mobile (10-digit starting 6-9)
- PAN (AAAAA9999A) with weighted alphabetic check digit
- NPCI 12-digit RRN (UPI/IMPS) vs 16-char NEFT/RTGS UTR
- RBI clock in IST (UTC+5:30): 2nd/4th Saturdays + gazetted holidays;
  UPI/IMPS 24/7, NEFT queues on RBI holidays, RTGS 09:00-18:00 IST
- UPI1 / IMPS1 pipe compose/parse (XML/DOCTYPE rejected)
- Complaint windows (UPI 24h customer / other rails next RBI business
  day / 90-day staff)
- Dual-control release at INR 200,000

Reuses Fedwire OFAC screening, money parsing, fee helper, and account
directory from `utility.wire`. Stores are pluggable (memory for tests,
sqlite WAL for restart-safe default). Full VPA, IFSC, account, MMID,
PAN, phone, fingerprint, and RRN never appear unmasked in to_dict /
snapshots.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    DEBIT_NSF,
    DEBIT_OK,
    DEFAULT_WATCHLIST,
    EMPLOYEE_ROLES,
    MONEY_QUANTUM,
    AccountError,
    AmountError,
    ScreenResult,
    account_types_from_customer_payload,
    compute_fee,
    last4,
    money_str,
    normalize_account,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

BENE_ACTIVE = 'active'
BENE_PAUSED = 'paused'
BENE_ARCHIVED = 'archived'
BENE_STATUSES = frozenset({BENE_ACTIVE, BENE_PAUSED, BENE_ARCHIVED})
OPEN_BENE = frozenset({BENE_ACTIVE, BENE_PAUSED})
UPI_HELD = 'held'
UPI_QUEUED = 'queued'
UPI_PENDING = 'pending_release'
UPI_COMPLETED = 'completed'
UPI_REJECTED = 'rejected'
UPI_CANCELLED = 'cancelled'
UPI_RECALLED = 'recalled'
UPI_NSF = 'nsf'
UPI_FAILED = 'failed'
UPI_STATUSES = frozenset({
    UPI_HELD, UPI_QUEUED, UPI_PENDING, UPI_COMPLETED, UPI_REJECTED,
    UPI_CANCELLED, UPI_RECALLED, UPI_NSF, UPI_FAILED,
})
OPEN_UPI = frozenset({UPI_HELD, UPI_QUEUED, UPI_PENDING})
CANCELABLE = frozenset({UPI_HELD, UPI_QUEUED, UPI_PENDING})
FEE_NONE = 'none'
FEE_COLLECTED = 'collected'
FEE_WAIVED = 'waived'
FEE_NSF = 'nsf'
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
    'upi_lite': 'upi', 'vpa': 'upi', 'p2p': 'upi', 'intent': 'upi',
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
DEFAULT_STORE_PATH = 'SystemLogs/upi.sqlite'
DEFAULT_SOURCE_IFSC = 'KNHB0000001'
CUSTOMER_UPI_RETURN_SECONDS = 24 * 60 * 60
STAFF_COMPLAINT_DAYS = 90
IST_OFFSET_MINUTES = 330


class UpiError(ValueError):
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
        raise UpiError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise UpiError('invalid_name', 'Beneficiary legal name must be 2-80 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise UpiError('invalid_purpose', 'Unknown UPI purpose.')
    return text


def normalize_currency(value: Any, *, default: str = 'INR') -> str:
    text = str(value or default).strip().upper()
    if text not in {'INR', 'RS', 'INR.'}:
        raise UpiError('invalid_currency', 'UPI / IMPS / NEFT / RTGS is INR only.')
    return 'INR'


def normalize_rail(value: Any, *, default: str = 'upi') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text not in RAILS:
        raise UpiError('invalid_rail', 'Rail must be upi, imps, neft, or rtgs.')
    return text


def normalize_ifsc(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[\s-]', '', str(value or '')).upper()
    if not text:
        if required:
            raise UpiError('invalid_ifsc', 'IFSC is required.')
        return ''
    if not IFSC_RE.match(text):
        raise UpiError('invalid_ifsc', 'IFSC must be 11 characters with a 0 in position 5.')
    return text


def mask_ifsc(value: Any) -> str:
    text = re.sub(r'[\s-]', '', str(value or '')).upper()
    if len(text) < 5:
        return '****0******'
    return text[:4] + '0******'


def bank_code_from_ifsc(value: Any) -> str:
    try:
        return normalize_ifsc(value)[:4]
    except UpiError:
        return ''


def bank_name(value: Any, *, directory: Optional[Dict[str, str]] = None) -> str:
    code = bank_code_from_ifsc(value) or str(value or '').strip().upper()[:4]
    mapping = directory if directory is not None else DEFAULT_BANKS
    return mapping.get(code, '')


def normalize_external_account(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise UpiError('invalid_account', 'Beneficiary account is required.')
        return ''
    if not (6 <= len(digits) <= 18):
        raise UpiError('invalid_account', 'Beneficiary account must be 6-18 digits.')
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
            raise UpiError('invalid_vpa', 'UPI VPA is required.')
        return ''
    if text.count('@') != 1:
        raise UpiError('invalid_vpa', 'VPA must be local@psp.')
    local, _, handle = text.partition('@')
    if not VPA_LOCAL_RE.match(local):
        raise UpiError('invalid_vpa', 'VPA local part is invalid.')
    allowed = set(psps) if psps is not None else set(DEFAULT_PSPS)
    if handle not in allowed:
        raise UpiError('invalid_vpa', 'VPA PSP handle is not a registered NPCI handle.')
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
            raise UpiError('invalid_mmid', 'MMID is required for IMPS.')
        return ''
    if len(digits) != 7 or digits == '0000000':
        raise UpiError('invalid_mmid', 'MMID must be seven digits.')
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
            raise UpiError('invalid_phone', 'Indian mobile is required for IMPS MMID.')
        return ''
    if digits.startswith('91') and len(digits) == 12:
        national = digits[2:]
    elif len(digits) == 10:
        national = digits
    elif digits.startswith('0') and len(digits) == 11:
        national = digits[1:]
    else:
        raise UpiError('invalid_phone', 'Indian mobile must be 10 digits starting 6-9.')
    if len(national) != 10 or national[0] not in '6789':
        raise UpiError('invalid_phone', 'Indian mobile must be 10 digits starting 6-9.')
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
        raise UpiError('invalid_pan', 'PAN body must be 9 characters.')
    total = 0
    for index, char in enumerate(text):
        if char.isalpha():
            number = ord(char) - 55
        elif char.isdigit():
            number = int(char)
        else:
            raise UpiError('invalid_pan', 'PAN contains an invalid character.')
        total += number * (9 - index)
    return chr(ord('A') + (total % 26))


def pan_check_digit_ok(value: str) -> bool:
    text = str(value or '').upper()
    if len(text) != 10:
        return False
    try:
        return pan_check_digit(text[:9]) == text[9]
    except UpiError:
        return False


def compose_pan(body: Any) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(body or '').upper())
    if len(text) != 9:
        raise UpiError('invalid_pan', 'PAN body must be 9 characters.')
    return text + pan_check_digit(text)


def normalize_pan(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[\s-]', '', str(value or '')).upper()
    if not text:
        if required:
            raise UpiError('invalid_pan', 'PAN is required.')
        return ''
    if len(text) != 10 or not text[:5].isalpha() or not text[5:9].isdigit() or not text[9].isalpha():
        raise UpiError('invalid_pan', 'PAN must be AAAAA9999A.')
    if text[3] not in PAN_TYPES:
        raise UpiError('invalid_pan', 'PAN holder type is invalid.')
    if not pan_check_digit_ok(text):
        raise UpiError('invalid_pan', 'PAN failed checksum.')
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
        raise UpiError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise UpiError('invalid_reference', 'RRN sequence out of range.')
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
    raise UpiError('invalid_reference', 'NPCI RRN / UTR is invalid.')


def mask_rrn(value: Any) -> str:
    text = str(value or '')
    if len(text) < 6:
        return '********'
    return ('*' * (len(text) - 4)) + text[-4:]


def compose_amount_field(amount: Decimal) -> str:
    quantized = amount.quantize(MONEY_QUANTUM)
    if quantized < 0 or quantized > Decimal('999999999.99'):
        raise UpiError('invalid_amount', 'Amount cannot be encoded in UPI1.')
    return money_str(quantized)


def parse_amount_field(value: Any) -> Decimal:
    try:
        return parse_money(value)
    except AmountError as exc:
        raise UpiError('invalid_amount', 'UPI1 amount field is invalid.') from exc


def _reject_markup(text: str) -> None:
    lowered = text.lower()
    if '<xml' in lowered or '<!doctype' in lowered or '<?xml' in lowered:
        raise UpiError('invalid_file', 'XML/DOCTYPE UPI1 files are rejected.')


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
        str(status or UPI_COMPLETED),
        kind,
        kind,
    ))


def parse_upi1(text: Any) -> Dict[str, Any]:
    raw = str(text or '').strip()
    if not raw:
        raise UpiError('invalid_file', 'UPI1 record is empty.')
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != UPI1_FIELDS:
        raise UpiError('invalid_file', 'UPI1 record must have %d fields.' % UPI1_FIELDS)
    if parts[0] != 'UPI1' or parts[1] != UPI1_VERSION:
        raise UpiError('invalid_file', 'UPI1 version marker is invalid.')
    if parts[13] not in RAILS:
        raise UpiError('invalid_file', 'UPI1 settlement method is invalid.')
    if '*' in parts[2] or '*' in parts[3] or '*' in parts[4]:
        raise UpiError('invalid_file', 'Masked UPI1 record cannot be re-ingested.')
    return {
        'rrn': normalize_rrn(parts[2]),
        'vpa': parts[3],
        'ifsc': parts[4],
        'account_number': parts[5],
        'mmid': parts[6],
        'phone': parts[7],
        'pan': parts[8],
        'amount': money_str(parse_amount_field(parts[9])),
        'purpose': normalize_purpose(parts[10]),
        'status': parts[11],
        'rail': normalize_rail(parts[12]),
        'settlement_method': parts[13],
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
        raise UpiError('invalid_file', 'IMPS1 record is empty.')
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != IMPS1_FIELDS:
        raise UpiError('invalid_file', 'IMPS1 record must have %d fields.' % IMPS1_FIELDS)
    if parts[0] != 'IMPS1' or parts[1] != UPI1_VERSION or parts[6] != 'INR':
        raise UpiError('invalid_file', 'IMPS1 version marker is invalid.')
    if '*' in parts[2] or '*' in parts[3] or '*' in parts[5]:
        raise UpiError('invalid_file', 'Masked IMPS1 record cannot be re-ingested.')
    return {
        'mmid': normalize_mmid(parts[2], required=True),
        'phone': normalize_phone(parts[3], required=True),
        'amount': money_str(parse_amount_field(parts[4])),
        'rrn': normalize_rrn(parts[5]),
        'currency': 'INR',
    }


def split_upi1_file(text: Any) -> List[str]:
    raw = str(text or '')
    _reject_markup(raw)
    records = [line.strip() for line in raw.replace('\r\n', '\n').split('\n') if line.strip()]
    if not records:
        raise UpiError('invalid_file', 'UPI1 file is empty.')
    return records


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


@dataclass
class UpiPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    allow_credit: bool = False
    max_beneficiaries: int = 12
    max_payments: int = 120
    min_amount: Decimal = Decimal('1.00')
    upi_max: Decimal = Decimal('100000.00')
    imps_max: Decimal = Decimal('500000.00')
    neft_max: Decimal = Decimal('1000000.00')
    rtgs_min: Decimal = Decimal('200000.00')
    rtgs_max: Decimal = Decimal('5000000.00')
    outbound_fee: Decimal = Decimal('0.00')
    dual_control_threshold: Decimal = Decimal('200000.00')
    tz_offset_minutes: int = IST_OFFSET_MINUTES
    open_hour: int = 9
    cutoff_hour: int = 18
    source_ifsc: str = DEFAULT_SOURCE_IFSC
    customer_upi_return_seconds: int = CUSTOMER_UPI_RETURN_SECONDS
    staff_complaint_days: int = STAFF_COMPLAINT_DAYS
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()
    banks: Dict[str, str] = None  # type: ignore[assignment]
    psps: Tuple[str, ...] = tuple(sorted(DEFAULT_PSPS))

    def __post_init__(self) -> None:
        if self.banks is None:
            self.banks = dict(DEFAULT_BANKS)

    @classmethod
    def from_env(cls) -> 'UpiPolicy':
        extra = _env_list('UPI_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        extra_banks = _env_list('UPI_BANKS')
        directory = dict(DEFAULT_BANKS)
        for item in extra_banks:
            if ':' in item:
                pid, _, label = item.partition(':')
                directory[pid.strip().upper()[:4]] = label.strip() or directory.get(pid.strip().upper()[:4], '')
        extra_psps = _env_list('UPI_PSPS')
        psps = tuple(dict.fromkeys(tuple(sorted(DEFAULT_PSPS)) + extra_psps))
        source = os.environ.get('UPI_SOURCE', DEFAULT_SOURCE_IFSC)
        try:
            source_ifsc = normalize_ifsc(source)
        except UpiError:
            source_ifsc = DEFAULT_SOURCE_IFSC
        return cls(
            enabled=_env_bool('UPI_ENABLED', True),
            customer_manage=_env_bool('UPI_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('UPI_CUSTOMER_SEND', True),
            allow_credit=_env_bool('UPI_ALLOW_CREDIT', False),
            max_beneficiaries=max(1, _env_int('UPI_MAX_BENEFICIARIES', 12)),
            max_payments=max(1, _env_int('UPI_MAX_PAYMENTS', 120)),
            min_amount=_env_money('UPI_MIN_AMOUNT', '1.00'),
            upi_max=_env_money('UPI_MAX', '100000.00'),
            imps_max=_env_money('UPI_IMPS_MAX', '500000.00'),
            neft_max=_env_money('UPI_NEFT_MAX', '1000000.00'),
            rtgs_min=_env_money('UPI_RTGS_MIN', '200000.00'),
            rtgs_max=_env_money('UPI_RTGS_MAX', '5000000.00'),
            outbound_fee=_env_money('UPI_FEE', '0.00'),
            dual_control_threshold=_env_money('UPI_DUAL_CONTROL', '200000.00'),
            tz_offset_minutes=_env_int('UPI_TZ_OFFSET_MINUTES', IST_OFFSET_MINUTES),
            open_hour=max(0, min(23, _env_int('UPI_OPEN_HOUR', 9))),
            cutoff_hour=max(0, min(23, _env_int('UPI_CUTOFF_HOUR', 18))),
            source_ifsc=source_ifsc,
            customer_upi_return_seconds=max(60, _env_int(
                'UPI_RETURN_SECONDS', CUSTOMER_UPI_RETURN_SECONDS,
            )),
            staff_complaint_days=max(1, _env_int('UPI_COMPLAINT_DAYS', STAFF_COMPLAINT_DAYS)),
            watchlist=watch,
            extra_holidays=_env_list('UPI_HOLIDAYS'),
            banks=directory,
            psps=psps,
        )


@dataclass
class UpiBeneficiary:
    beneficiary_id: str
    userid: str
    nickname: str
    legal_name: str
    rail: str
    vpa: str
    ifsc: str
    account_number: str
    mmid: str
    phone: str
    pan: str
    fingerprint: str
    default_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float
    bank_label: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'beneficiary_id': self.beneficiary_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'rail': self.rail,
            'vpa_masked': mask_vpa(self.vpa) if self.vpa else '',
            'ifsc_masked': mask_ifsc(self.ifsc) if self.ifsc else '',
            'account_last4': last4(self.account_number) if self.account_number else '',
            'mmid_masked': mask_mmid(self.mmid) if self.mmid else '',
            'phone_masked': mask_phone(self.phone) if self.phone else '',
            'pan_masked': mask_pan(self.pan) if self.pan else '',
            'bank_code': bank_code_from_ifsc(self.ifsc) if self.ifsc else '',
            'bank_label': self.bank_label,
            'psp': self.vpa.split('@', 1)[1] if '@' in self.vpa else '',
            'default_account': self.default_account,
            'internal_last4': last4(self.default_account),
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == BENE_ACTIVE,
            'paused': self.status == BENE_PAUSED,
            'archived': self.status == BENE_ARCHIVED,
        }


@dataclass
class UpiPayment:
    payment_id: str
    trace_id: str
    beneficiary_id: str
    userid: str
    internal_account: str
    amount: str
    fee: str
    fee_status: str
    nickname: str
    legal_name: str
    rail: str
    vpa_masked: str
    ifsc_masked: str
    account_last4: str
    mmid_masked: str
    phone_masked: str
    pan_masked: str
    bank_code: str
    purpose: str
    memo: str
    status: str
    rrn: str
    value_date: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    created_at: float
    updated_at: float
    completed_at: float = 0.0
    recalled_at: float = 0.0
    note: str = ''
    reason: str = ''
    settlement_method: str = 'upi'

    def to_dict(self) -> Dict[str, Any]:
        return {
            'payment_id': self.payment_id,
            'trace_id': self.trace_id,
            'beneficiary_id': self.beneficiary_id,
            'userid': self.userid,
            'internal_account_last4': last4(self.internal_account),
            'amount': self.amount,
            'fee': self.fee,
            'fee_status': self.fee_status,
            'currency': 'INR',
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'rail': self.rail,
            'vpa_masked': self.vpa_masked,
            'ifsc_masked': self.ifsc_masked,
            'account_last4': self.account_last4,
            'mmid_masked': self.mmid_masked,
            'phone_masked': self.phone_masked,
            'pan_masked': self.pan_masked,
            'bank_code': self.bank_code,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'rrn_masked': mask_rrn(self.rrn) if self.rrn else '',
            'value_date': self.value_date,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'completed_at': self.completed_at,
            'recalled_at': self.recalled_at,
            'note': self.note,
            'reason': self.reason,
            'settlement_method': self.settlement_method,
            'cancelable': self.status in CANCELABLE,
            'recallable': self.status == UPI_COMPLETED,
        }


def _clone_bene(row: UpiBeneficiary) -> UpiBeneficiary:
    return UpiBeneficiary(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_pay(row: UpiPayment) -> UpiPayment:
    return UpiPayment(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _row_get(row: Any, key: str, default: Any = '') -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _bene_from_row(row: Any) -> UpiBeneficiary:
    return UpiBeneficiary(
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        rail=row['rail'],
        vpa=_row_get(row, 'vpa', ''),
        ifsc=_row_get(row, 'ifsc', ''),
        account_number=_row_get(row, 'account_number', ''),
        mmid=_row_get(row, 'mmid', ''),
        phone=_row_get(row, 'phone', ''),
        pan=_row_get(row, 'pan', ''),
        fingerprint=row['fingerprint'],
        default_account=row['default_account'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        bank_label=_row_get(row, 'bank_label', '') or '',
    )


def _pay_from_row(row: Any) -> UpiPayment:
    return UpiPayment(
        payment_id=row['payment_id'],
        trace_id=row['trace_id'],
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        amount=row['amount'],
        fee=row['fee'],
        fee_status=row['fee_status'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        rail=row['rail'],
        vpa_masked=_row_get(row, 'vpa_masked', ''),
        ifsc_masked=_row_get(row, 'ifsc_masked', ''),
        account_last4=_row_get(row, 'account_last4', ''),
        mmid_masked=_row_get(row, 'mmid_masked', ''),
        phone_masked=_row_get(row, 'phone_masked', ''),
        pan_masked=_row_get(row, 'pan_masked', ''),
        bank_code=_row_get(row, 'bank_code', ''),
        purpose=row['purpose'],
        memo=row['memo'] or '',
        status=row['status'],
        rrn=_row_get(row, 'rrn', ''),
        value_date=row['value_date'],
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        completed_at=float(row['completed_at'] or 0),
        recalled_at=float(row['recalled_at'] or 0),
        note=row['note'] or '',
        reason=row['reason'] or '',
        settlement_method=_row_get(row, 'settlement_method', '') or row['rail'],
    )


class MemoryUpiStore:
    def __init__(self) -> None:
        self._benes: Dict[str, UpiBeneficiary] = {}
        self._pays: Dict[str, UpiPayment] = {}
        self._lock = threading.Lock()

    def put_beneficiary(self, row: UpiBeneficiary) -> None:
        self._benes[row.beneficiary_id] = _clone_bene(row)

    def get_beneficiary(self, beneficiary_id: str) -> Optional[UpiBeneficiary]:
        row = self._benes.get(beneficiary_id)
        return _clone_bene(row) if row else None

    def update_beneficiary(self, row: UpiBeneficiary) -> None:
        self._benes[row.beneficiary_id] = _clone_bene(row)

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[UpiBeneficiary]:
        rows = list(self._benes.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != BENE_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return [_clone_bene(row) for row in rows]

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[UpiBeneficiary]:
        wanted = nickname.strip().lower()
        for row in self._benes.values():
            if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_BENE:
                return _clone_bene(row)
        return None

    def find_beneficiary_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[UpiBeneficiary]:
        for row in self._benes.values():
            if row.userid == userid and row.fingerprint == fingerprint and row.status in OPEN_BENE:
                return _clone_bene(row)
        return None

    def put_payment(self, row: UpiPayment) -> UpiPayment:
        with self._lock:
            existing = self.get_payment_by_trace(row.trace_id)
            if existing is not None:
                return existing
            self._pays[row.payment_id] = _clone_pay(row)
            return _clone_pay(row)

    def update_payment(self, row: UpiPayment) -> None:
        self._pays[row.payment_id] = _clone_pay(row)

    def get_payment(self, payment_id: str) -> Optional[UpiPayment]:
        row = self._pays.get(payment_id)
        return _clone_pay(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[UpiPayment]:
        for row in self._pays.values():
            if row.trace_id == trace_id:
                return _clone_pay(row)
        return None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[UpiPayment]:
        rows = list(self._pays.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if beneficiary_id is not None:
            rows = [row for row in rows if row.beneficiary_id == beneficiary_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return [_clone_pay(row) for row in rows]

    def next_sequence(self, cycle_date: str) -> int:
        used = [row.rrn for row in self._pays.values() if row.rrn and row.value_date == cycle_date]
        return len(used) + 1


class SqliteUpiStore:
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
                CREATE TABLE IF NOT EXISTS beneficiaries (
                    beneficiary_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    rail TEXT NOT NULL,
                    vpa TEXT NOT NULL DEFAULT '',
                    ifsc TEXT NOT NULL DEFAULT '',
                    account_number TEXT NOT NULL DEFAULT '',
                    mmid TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
                    pan TEXT NOT NULL DEFAULT '',
                    fingerprint TEXT NOT NULL,
                    default_account TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    bank_label TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    payment_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    beneficiary_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    internal_account TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    fee_status TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    rail TEXT NOT NULL,
                    vpa_masked TEXT NOT NULL DEFAULT '',
                    ifsc_masked TEXT NOT NULL DEFAULT '',
                    account_last4 TEXT NOT NULL DEFAULT '',
                    mmid_masked TEXT NOT NULL DEFAULT '',
                    phone_masked TEXT NOT NULL DEFAULT '',
                    pan_masked TEXT NOT NULL DEFAULT '',
                    bank_code TEXT NOT NULL DEFAULT '',
                    purpose TEXT NOT NULL,
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    rrn TEXT NOT NULL DEFAULT '',
                    value_date TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL NOT NULL DEFAULT 0,
                    recalled_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    settlement_method TEXT NOT NULL DEFAULT 'upi'
                )
                """
            )
            conn.commit()

    def put_beneficiary(self, row: UpiBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO beneficiaries (
                    beneficiary_id, userid, nickname, legal_name, rail, vpa, ifsc,
                    account_number, mmid, phone, pan, fingerprint, default_account,
                    status, actor, created_at, updated_at, bank_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.beneficiary_id, row.userid, row.nickname, row.legal_name,
                    row.rail, row.vpa, row.ifsc, row.account_number, row.mmid,
                    row.phone, row.pan, row.fingerprint, row.default_account,
                    row.status, row.actor, row.created_at, row.updated_at, row.bank_label,
                ),
            )
            conn.commit()

    def get_beneficiary(self, beneficiary_id: str) -> Optional[UpiBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM beneficiaries WHERE beneficiary_id = ?', (beneficiary_id,),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def update_beneficiary(self, row: UpiBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE beneficiaries SET nickname=?, legal_name=?, rail=?, vpa=?,
                    ifsc=?, account_number=?, mmid=?, phone=?, pan=?, fingerprint=?,
                    default_account=?, status=?, actor=?, updated_at=?, bank_label=?
                WHERE beneficiary_id=?
                """,
                (
                    row.nickname, row.legal_name, row.rail, row.vpa, row.ifsc,
                    row.account_number, row.mmid, row.phone, row.pan, row.fingerprint,
                    row.default_account, row.status, row.actor, row.updated_at,
                    row.bank_label, row.beneficiary_id,
                ),
            )
            conn.commit()

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[UpiBeneficiary]:
        sql = 'SELECT * FROM beneficiaries'
        args: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            args.append(userid)
        if not include_archived:
            clauses.append("status != 'archived'")
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_bene_from_row(row) for row in rows]

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[UpiBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM beneficiaries
                WHERE userid = ? AND lower(nickname) = lower(?) AND status IN ('active', 'paused')
                """,
                (userid, nickname),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def find_beneficiary_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[UpiBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM beneficiaries
                WHERE userid = ? AND fingerprint = ? AND status IN ('active', 'paused')
                """,
                (userid, fingerprint),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def put_payment(self, row: UpiPayment) -> UpiPayment:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM payments WHERE trace_id = ?', (row.trace_id,),
            ).fetchone()
            if existing is not None:
                return _pay_from_row(existing)
            conn.execute(
                """
                INSERT INTO payments (
                    payment_id, trace_id, beneficiary_id, userid, internal_account,
                    amount, fee, fee_status, nickname, legal_name, rail, vpa_masked,
                    ifsc_masked, account_last4, mmid_masked, phone_masked, pan_masked,
                    bank_code, purpose, memo, status, rrn, value_date, actor, releaser,
                    ofac_hit, ofac_match, created_at, updated_at, completed_at,
                    recalled_at, note, reason, settlement_method
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.payment_id, row.trace_id, row.beneficiary_id, row.userid,
                    row.internal_account, row.amount, row.fee, row.fee_status,
                    row.nickname, row.legal_name, row.rail, row.vpa_masked,
                    row.ifsc_masked, row.account_last4, row.mmid_masked,
                    row.phone_masked, row.pan_masked, row.bank_code, row.purpose,
                    row.memo, row.status, row.rrn, row.value_date, row.actor,
                    row.releaser, row.ofac_hit, row.ofac_match, row.created_at,
                    row.updated_at, row.completed_at, row.recalled_at, row.note,
                    row.reason, row.settlement_method,
                ),
            )
            conn.commit()
        return row

    def update_payment(self, row: UpiPayment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE payments SET amount=?, fee=?, fee_status=?, status=?,
                    rrn=?, value_date=?, actor=?, releaser=?, ofac_hit=?,
                    ofac_match=?, updated_at=?, completed_at=?, recalled_at=?,
                    note=?, reason=?
                WHERE payment_id=?
                """,
                (
                    row.amount, row.fee, row.fee_status, row.status, row.rrn,
                    row.value_date, row.actor, row.releaser, row.ofac_hit,
                    row.ofac_match, row.updated_at, row.completed_at, row.recalled_at,
                    row.note, row.reason, row.payment_id,
                ),
            )
            conn.commit()

    def get_payment(self, payment_id: str) -> Optional[UpiPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE payment_id = ?', (payment_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[UpiPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE trace_id = ?', (trace_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[UpiPayment]:
        sql = 'SELECT * FROM payments'
        args: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            args.append(userid)
        if beneficiary_id is not None:
            clauses.append('beneficiary_id = ?')
            args.append(beneficiary_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_pay_from_row(row) for row in rows]

    def next_sequence(self, cycle_date: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE value_date = ? AND rrn != ''",
                (cycle_date,),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class UpiService:
    def __init__(
        self,
        policy: UpiPolicy,
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
        self.clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
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

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise UpiError('upi_disabled', 'UPI is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise UpiError('upi_forbidden', 'Customers cannot manage UPI beneficiaries.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise UpiError('upi_forbidden', 'Customers cannot originate UPI.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise UpiError('upi_forbidden', 'Staff only.')

    def _accounts(self, userid: str) -> Tuple[List[str], Dict[str, str]]:
        payload = self.accounts_fn(userid) if self.accounts_fn is not None else {}
        return own_accounts_from_customer_payload(payload), account_types_from_customer_payload(payload)

    def _assert_internal_account(self, userid: str, account: str) -> str:
        owned, types = self._accounts(userid)
        if owned and account not in owned:
            raise UpiError('invalid_account', 'Account is not owned by this customer.')
        kind = types.get(account, '')
        if kind == 'credit' and not self.policy.allow_credit:
            raise UpiError('credit_not_allowed', 'Credit accounts cannot originate UPI.')
        return kind

    def _assert_amount(self, amount: Decimal, *, rail: str) -> None:
        if amount < self.policy.min_amount:
            raise UpiError('amount_out_of_range', 'Amount is outside the allowed range.')
        if rail == 'upi' and amount > self.policy.upi_max:
            raise UpiError('upi_amount_exceeded', 'Amount exceeds the UPI cap.')
        if rail == 'imps' and amount > self.policy.imps_max:
            raise UpiError('imps_amount_exceeded', 'Amount exceeds the IMPS cap.')
        if rail == 'neft' and amount > self.policy.neft_max:
            raise UpiError('neft_amount_exceeded', 'Amount exceeds the NEFT cap.')
        if rail == 'rtgs' and amount < self.policy.rtgs_min:
            raise UpiError('rtgs_amount_below_minimum', 'RTGS is below the RBI minimum.')
        if rail == 'rtgs' and amount > self.policy.rtgs_max:
            raise UpiError('rtgs_amount_exceeded', 'Amount exceeds the RTGS cap.')

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def _needs_queue(self, amount: Decimal, *, rail: str, clock: Dict[str, Any]) -> bool:
        _ = amount
        if rail in {'upi', 'imps'}:
            return False
        if rail == 'neft':
            return not bool(clock.get('business_day'))
        return not bool(clock.get('session'))

    def _screen(self, name: str, *, aliases: Iterable[str] = ()) -> ScreenResult:
        return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)

    def add_beneficiary(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        rail: Any = 'upi',
        vpa: Any = '',
        ifsc: Any = '',
        account_number: Any = '',
        mmid: Any = '',
        phone: Any = '',
        pan: Any = '',
        default_account: Any,
    ) -> UpiBeneficiary:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise UpiError('upi_forbidden', 'Not allowed to add UPI beneficiaries for this customer.')
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        kind = normalize_rail(rail)
        vpa_value = normalize_vpa(vpa, required=kind == 'upi', psps=self.policy.psps)
        ifsc_needed = kind in {'neft', 'rtgs'} or (kind == 'imps' and not str(mmid or '').strip())
        ifsc_value = normalize_ifsc(ifsc, required=ifsc_needed)
        account_value = normalize_external_account(account_number, required=bool(ifsc_value))
        mmid_value = normalize_mmid(mmid, required=kind == 'imps' and not ifsc_value)
        phone_value = normalize_phone(phone, required=kind == 'imps' and bool(mmid_value))
        pan_value = normalize_pan(pan, required=kind in {'neft', 'rtgs'})
        if kind == 'imps' and not ((ifsc_value and account_value) or (mmid_value and phone_value)):
            raise UpiError('invalid_rail', 'IMPS requires IFSC+account or MMID+mobile.')
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        fingerprint = destination_fingerprint(
            kind, vpa_value, ifsc_value, account_value, mmid_value, phone_value,
        )
        if self.store.find_beneficiary_by_nickname(owner_userid, name) is not None:
            raise UpiError('beneficiary_duplicate', 'A UPI beneficiary with that nickname already exists.')
        if self.store.find_beneficiary_by_fingerprint(owner_userid, fingerprint) is not None:
            raise UpiError('beneficiary_duplicate', 'That VPA, IFSC, or MMID is already on file.')
        open_rows = [row for row in self.store.list_beneficiaries(owner_userid) if row.status in OPEN_BENE]
        if len(open_rows) >= self.policy.max_beneficiaries:
            raise UpiError('beneficiary_limit', 'UPI beneficiary limit reached.')
        now = float(self.clock())
        row = UpiBeneficiary(
            beneficiary_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            rail=kind,
            vpa=vpa_value,
            ifsc=ifsc_value,
            account_number=account_value,
            mmid=mmid_value,
            phone=phone_value,
            pan=pan_value,
            fingerprint=fingerprint,
            default_account=account,
            status=BENE_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
            bank_label=self.policy.banks.get(bank_code_from_ifsc(ifsc_value), ''),
        )
        self.store.put_beneficiary(row)
        return row

    def get_beneficiary(self, *, beneficiary_id: str, actor: str, actor_type: str) -> UpiBeneficiary:
        self._require_enabled()
        row = self.store.get_beneficiary(beneficiary_id)
        if row is None:
            raise UpiError('beneficiary_not_found', 'UPI beneficiary not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise UpiError('upi_forbidden', 'Not allowed to view this UPI beneficiary.')
        return row

    def enforce_beneficiary(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> UpiBeneficiary:
        row = self.get_beneficiary(beneficiary_id=beneficiary_id, actor=actor, actor_type=actor_type)
        if row.status == BENE_ARCHIVED:
            raise UpiError('already_archived', 'UPI beneficiary is archived.')
        if row.status == BENE_PAUSED:
            raise UpiError('beneficiary_paused', 'UPI beneficiary is paused.')
        if require_active and row.status != BENE_ACTIVE:
            raise UpiError('invalid_status', 'UPI beneficiary is not active.')
        return row

    def set_beneficiary_status(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> UpiBeneficiary:
        self._require_manage(actor_type)
        row = self.get_beneficiary(beneficiary_id=beneficiary_id, actor=actor, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': BENE_PAUSED, 'hold': BENE_PAUSED,
            'resume': BENE_ACTIVE, 'activate': BENE_ACTIVE, 'unpause': BENE_ACTIVE,
            'archive': BENE_ARCHIVED, 'close': BENE_ARCHIVED, 'cancel': BENE_ARCHIVED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {BENE_PAUSED, BENE_ACTIVE, BENE_ARCHIVED}:
            raise UpiError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == BENE_ARCHIVED:
            raise UpiError('already_archived', 'UPI beneficiary is already archived.')
        if wanted == BENE_PAUSED:
            if row.status == BENE_PAUSED:
                raise UpiError('already_paused', 'UPI beneficiary is already paused.')
            if row.status != BENE_ACTIVE:
                raise UpiError('invalid_status', 'Only an active UPI beneficiary can be paused.')
        elif wanted == BENE_ACTIVE:
            if row.status == BENE_ACTIVE:
                raise UpiError('already_active', 'UPI beneficiary is already active.')
            if row.status != BENE_PAUSED:
                raise UpiError('invalid_status', 'Only a paused UPI beneficiary can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_beneficiary(row)
        return row

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        beneficiary_id: Any,
        amount: Any,
        internal_account: Any = None,
        waive_fee: bool = False,
    ) -> Dict[str, Any]:
        self._require_send(actor_type)
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise UpiError('upi_forbidden', 'UPI beneficiary does not belong to this customer.')
        rupees = parse_money(amount)
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        self._assert_amount(rupees, rail=bene.rail)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        fee = compute_fee(rupees, self.policy.outbound_fee, waived=bool(waive_fee) and actor_type in EMPLOYEE_ROLES)
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        queued = self._needs_queue(rupees, rail=bene.rail, clock=clock)
        return {
            'amount': money_str(rupees),
            'fee': money_str(fee),
            'total': money_str(rupees + fee),
            'currency': 'INR',
            'internal_account': account,
            'beneficiary': bene.to_dict(),
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(rupees),
            'queued': queued,
            'clock': clock,
            'upi_cap': money_str(self.policy.upi_max),
            'imps_cap': money_str(self.policy.imps_max),
            'neft_cap': money_str(self.policy.neft_max),
            'rtgs_min': money_str(self.policy.rtgs_min),
            'settlement_method': bene.rail,
        }

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        bene: UpiBeneficiary,
        account: str,
        rupees: Decimal,
        fee: Decimal,
        purpose: str,
        memo: str,
        trace_id: str,
        ofac: ScreenResult,
        waive_fee: bool,
        clock: Dict[str, Any],
    ) -> UpiPayment:
        now = float(self.clock())
        value = self.calendar.cycle_date(now)
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        if ofac.hit:
            status = UPI_HELD
        elif self._needs_dual_control(rupees):
            status = UPI_PENDING
        elif self._needs_queue(rupees, rail=bene.rail, clock=clock):
            status = UPI_QUEUED
            value = self.calendar.value_date(now).strftime('%Y%m%d')
        else:
            status = UPI_COMPLETED
        payment = UpiPayment(
            payment_id=uuid.uuid4().hex,
            trace_id=trace_id,
            beneficiary_id=bene.beneficiary_id,
            userid=owner_userid,
            internal_account=account,
            amount=money_str(rupees),
            fee=money_str(fee),
            fee_status=fee_status,
            nickname=bene.nickname,
            legal_name=bene.legal_name,
            rail=bene.rail,
            vpa_masked=mask_vpa(bene.vpa) if bene.vpa else '',
            ifsc_masked=mask_ifsc(bene.ifsc) if bene.ifsc else '',
            account_last4=last4(bene.account_number) if bene.account_number else '',
            mmid_masked=mask_mmid(bene.mmid) if bene.mmid else '',
            phone_masked=mask_phone(bene.phone) if bene.phone else '',
            pan_masked=mask_pan(bene.pan) if bene.pan else '',
            bank_code=bank_code_from_ifsc(bene.ifsc) if bene.ifsc else '',
            purpose=purpose,
            memo=memo,
            status=status,
            rrn='',
            value_date=value,
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
            settlement_method=bene.rail,
        )
        if status == UPI_COMPLETED:
            self._transmit(payment, actor=actor, bene=bene)
        return payment

    def _bene_secrets(self, payment: UpiPayment) -> UpiBeneficiary:
        bene = self.store.get_beneficiary(payment.beneficiary_id)
        if bene is None:
            return UpiBeneficiary(
                beneficiary_id='', userid='', nickname='', legal_name='', rail=payment.rail,
                vpa='', ifsc='', account_number='', mmid='', phone='', pan='',
                fingerprint='', default_account='', status=BENE_ACTIVE, actor='',
                created_at=0, updated_at=0,
            )
        return bene

    def _transmit(
        self,
        payment: UpiPayment,
        *,
        actor: str,
        bene: Optional[UpiBeneficiary] = None,
    ) -> UpiPayment:
        now = float(self.clock())
        cycle = payment.value_date or self.calendar.cycle_date(now)
        seq = self.store.next_sequence(cycle)
        payment.rrn = compose_rrn(cycle, seq, rail=payment.rail)
        rupees = parse_money(payment.amount)
        fee = parse_money(payment.fee, allow_zero=True)
        remark = '%s to %s' % (payment.rail, payment.nickname)
        status = UPI_COMPLETED
        fail_note = ''
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(payment.internal_account, money_str(rupees), remark)
            except Exception as exc:
                status = UPI_FAILED
                fail_note = str(exc)[:240]
            else:
                classified = _classify_money_result(result)
                if classified == 'nsf':
                    status = UPI_NSF
                    payment.rrn = ''
                elif classified != 'ok':
                    status = UPI_FAILED
                    fail_note = str(result)[:240]
                    payment.rrn = ''
        if status == UPI_COMPLETED and fee > 0 and payment.fee_status != FEE_WAIVED and self.debit_fn is not None:
            fee_remark = '%s fee %s' % (payment.rail, mask_rrn(payment.rrn))
            try:
                fee_result = self.debit_fn(payment.internal_account, money_str(fee), fee_remark)
            except Exception:
                payment.fee_status = FEE_NSF
            else:
                if _classify_money_result(fee_result) == 'ok':
                    payment.fee_status = FEE_COLLECTED
                else:
                    payment.fee_status = FEE_NSF
        payment.status = status
        payment.updated_at = now
        payment.completed_at = now if status == UPI_COMPLETED else 0.0
        payment.releaser = str(actor)
        if fail_note:
            payment.note = fail_note
        _ = bene
        return payment

    def originate(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        beneficiary_id: Any,
        amount: Any,
        internal_account: Any = None,
        purpose: Any = 'other',
        memo: Any = '',
        trace_id: Any = None,
        waive_fee: bool = False,
    ) -> Tuple[UpiPayment, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise UpiError('upi_forbidden', 'Not allowed to originate UPI for this customer.')
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise UpiError('upi_forbidden', 'UPI beneficiary does not belong to this customer.')
        trace = normalize_id(trace_id)
        existing = self.store.get_payment_by_trace(trace)
        if existing is not None:
            return existing, False
        rupees = parse_money(amount)
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        self._assert_amount(rupees, rail=bene.rail)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        fee = compute_fee(
            rupees, self.policy.outbound_fee,
            waived=bool(waive_fee) and actor_type in EMPLOYEE_ROLES,
        )
        if len(self.store.list_payments(owner_userid)) >= self.policy.max_payments:
            raise UpiError('payment_limit', 'UPI history limit reached.')
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        payment = self._place(
            owner_userid=owner_userid,
            actor=actor,
            bene=bene,
            account=account,
            rupees=rupees,
            fee=fee,
            purpose=normalize_purpose(purpose),
            memo=normalize_note(memo, limit=140),
            trace_id=trace,
            ofac=ofac,
            waive_fee=bool(waive_fee) and actor_type in EMPLOYEE_ROLES,
            clock=clock,
        )
        stored = self.store.put_payment(payment)
        if stored.status == UPI_NSF:
            raise UpiError('nsf', 'Insufficient funds for UPI.', payment=stored)
        if stored.status == UPI_FAILED:
            raise UpiError('failed', 'UPI debit did not complete.', payment=stored)
        return stored, True

    def get_payment(self, *, payment_id: str, actor: str, actor_type: str) -> UpiPayment:
        self._require_enabled()
        row = self.store.get_payment(payment_id)
        if row is None:
            raise UpiError('payment_not_found', 'UPI payment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise UpiError('upi_forbidden', 'Not allowed to view this UPI payment.')
        return row

    def cancel_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> UpiPayment:
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and payment.userid != actor:
            raise UpiError('upi_forbidden', 'Not allowed to cancel this UPI.')
        if payment.status not in CANCELABLE:
            raise UpiError('not_cancelable', 'Only held, queued, or pending UPI can be cancelled.')
        payment.status = UPI_CANCELLED
        payment.note = normalize_note(note)
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        self.store.update_payment(payment)
        return payment

    def waive_fee(self, *, payment_id: str, actor: str, actor_type: str) -> UpiPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status not in CANCELABLE:
            raise UpiError('invalid_status', 'Fee can only be waived before the UPI is sent.')
        payment.fee = money_str(Decimal('0.00'))
        payment.fee_status = FEE_WAIVED
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        self.store.update_payment(payment)
        return payment

    def _requeue_or_transmit(self, payment: UpiPayment, *, actor: str) -> UpiPayment:
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        rupees = parse_money(payment.amount)
        if self._needs_queue(rupees, rail=payment.rail, clock=clock):
            payment.status = UPI_QUEUED
            payment.value_date = self.calendar.value_date(now).strftime('%Y%m%d')
            payment.updated_at = now
            payment.actor = str(actor)
            return payment
        return self._transmit(payment, actor=actor)

    def override_ofac(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> UpiPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status != UPI_HELD:
            raise UpiError('not_overridable', 'Only an OFAC hold can be overridden.')
        now = float(self.clock())
        payment.ofac_hit = 0
        payment.note = normalize_note(note) or 'ofac override'
        payment.actor = str(actor)
        payment.updated_at = now
        rupees = parse_money(payment.amount)
        if self._needs_dual_control(rupees):
            payment.status = UPI_PENDING
        else:
            self._requeue_or_transmit(payment, actor=actor)
        self.store.update_payment(payment)
        if payment.status == UPI_NSF:
            raise UpiError('nsf', 'Insufficient funds for UPI.', payment=payment)
        if payment.status == UPI_FAILED:
            raise UpiError('failed', 'UPI debit did not complete.', payment=payment)
        return payment

    def release_payment(self, *, payment_id: str, actor: str, actor_type: str) -> UpiPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == UPI_HELD:
            raise UpiError('ofac_hold', 'OFAC hold must be overridden before release.')
        if payment.status not in {UPI_PENDING, UPI_QUEUED}:
            raise UpiError('not_releasable', 'Only queued or pending UPI can be released.')
        if (
            payment.status == UPI_PENDING
            and payment.actor
            and str(actor) == str(payment.actor)
            and parse_money(payment.amount) >= self.policy.dual_control_threshold
        ):
            raise UpiError('same_approver', 'A different employee must release this UPI.')
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        rupees = parse_money(payment.amount)
        if payment.status == UPI_QUEUED and self._needs_queue(rupees, rail=payment.rail, clock=clock):
            payment.updated_at = now
            self.store.update_payment(payment)
            return payment
        self._requeue_or_transmit(payment, actor=actor)
        self.store.update_payment(payment)
        if payment.status == UPI_NSF:
            raise UpiError('nsf', 'Insufficient funds for UPI.', payment=payment)
        if payment.status == UPI_FAILED:
            raise UpiError('failed', 'UPI debit did not complete.', payment=payment)
        return payment

    def reject_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> UpiPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status not in CANCELABLE:
            raise UpiError('not_rejectable', 'Only held, queued, or pending UPI can be rejected.')
        payment.status = UPI_REJECTED
        payment.reason = normalize_note(reason, limit=40)
        payment.note = normalize_note(note)
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        self.store.update_payment(payment)
        return payment

    def _assert_complaint_window(self, payment: UpiPayment, *, actor_type: str) -> None:
        now = float(self.clock())
        completed = float(payment.completed_at or payment.updated_at or payment.created_at)
        if actor_type in EMPLOYEE_ROLES:
            if now - completed > self.policy.staff_complaint_days * 86400:
                raise UpiError('complaint_window_closed', 'Staff UPI complaint window has closed.')
            return
        if payment.rail == 'upi':
            if now - completed > self.policy.customer_upi_return_seconds:
                raise UpiError('return_window_closed', 'Customer UPI complaint window has closed.')
            return
        completed_day = self.calendar.local_dt(completed).date()
        deadline_day = self.calendar.next_business_day(completed_day)
        deadline = self.calendar.session_close_ts(deadline_day)
        if now > deadline:
            raise UpiError('return_window_closed', 'Customer NPCI complaint window has closed.')

    def recall_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> UpiPayment:
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == UPI_RECALLED:
            raise UpiError('already_recalled', 'UPI payment is already recalled.')
        if payment.status != UPI_COMPLETED:
            raise UpiError('not_recallable', 'Only completed UPI payments can be recalled.')
        self._assert_complaint_window(payment, actor_type=actor_type)
        remark = normalize_note(note) or (
            '%s complaint %s' % (payment.rail, mask_rrn(payment.rrn) or payment.nickname)
        )
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(payment.internal_account, payment.amount, remark)
            except Exception as exc:
                raise UpiError('recall_failed', 'Complaint credit failed.', payment=payment) from exc
            if _classify_money_result(result) != 'ok':
                raise UpiError('recall_failed', 'Complaint credit failed.', payment=payment)
            fee = parse_money(payment.fee, allow_zero=True)
            if fee > 0 and payment.fee_status == FEE_COLLECTED:
                self.credit_fn(
                    payment.internal_account, money_str(fee),
                    '%s fee recalled %s' % (payment.rail, mask_rrn(payment.rrn)),
                )
        now = float(self.clock())
        payment.status = UPI_RECALLED
        payment.recalled_at = now
        payment.updated_at = now
        payment.actor = str(actor)
        payment.note = remark
        self.store.update_payment(payment)
        return payment

    def export_upi1(self, *, payment_id: str, actor: str, actor_type: str) -> Dict[str, Any]:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status != UPI_COMPLETED:
            raise UpiError('not_exportable', 'Only completed UPI can be exported.')
        if not payment.rrn:
            raise UpiError('not_exportable', 'RRN has not been assigned.')
        bene = self._bene_secrets(payment)
        record = compose_upi1(
            rrn=payment.rrn,
            vpa=bene.vpa,
            ifsc=bene.ifsc,
            account_number=bene.account_number,
            mmid=bene.mmid,
            phone=bene.phone,
            pan=bene.pan,
            amount=parse_money(payment.amount),
            purpose=payment.purpose,
            status=payment.status,
            rail=payment.rail,
            mask=True,
        )
        payload = {
            'payment_id': payment.payment_id,
            'upi1': record,
            'rrn_masked': mask_rrn(payment.rrn),
        }
        if payment.rail == 'imps' and bene.mmid and bene.phone:
            payload['imps1'] = compose_imps1(
                mmid=bene.mmid,
                phone=bene.phone,
                amount=parse_money(payment.amount),
                rrn=payment.rrn,
                reference=payment.memo,
                mask=True,
            )
        return payload

    def run_due(self, userid: Optional[str] = None) -> List[UpiPayment]:
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        changed: List[UpiPayment] = []
        for payment in self.store.list_payments(userid):
            if payment.status != UPI_QUEUED:
                continue
            rupees = parse_money(payment.amount)
            if self._needs_queue(rupees, rail=payment.rail, clock=clock):
                continue
            self._transmit(payment, actor=payment.actor)
            self.store.update_payment(payment)
            changed.append(payment)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        _ = actor
        self.run_due(userid)
        beneficiaries = self.store.list_beneficiaries(userid)
        payments = self.store.list_payments(userid)
        sent_ytd = Decimal('0.00')
        fee_ytd = Decimal('0.00')
        recalled = Decimal('0.00')
        for row in payments:
            amount = parse_money(row.amount, allow_zero=True)
            if row.status == UPI_COMPLETED:
                sent_ytd += amount
                if row.fee_status == FEE_COLLECTED:
                    fee_ytd += parse_money(row.fee, allow_zero=True)
            elif row.status == UPI_RECALLED:
                recalled += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'upi_max': money_str(self.policy.upi_max),
            'imps_max': money_str(self.policy.imps_max),
            'neft_max': money_str(self.policy.neft_max),
            'rtgs_min': money_str(self.policy.rtgs_min),
            'rtgs_max': money_str(self.policy.rtgs_max),
            'fee': money_str(self.policy.outbound_fee),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'currency': 'INR',
            'clock': self.calendar.snapshot(now),
            'beneficiaries': [row.to_dict() for row in beneficiaries[:40]],
            'payments': [row.to_dict() for row in payments[:40]],
            'ytd_sent': money_str(sent_ytd),
            'ytd_fees': money_str(fee_ytd),
            'recalled_ytd': money_str(recalled),
            'active_count': sum(1 for row in beneficiaries if row.status == BENE_ACTIVE),
            'open_count': sum(1 for row in payments if row.status in OPEN_UPI),
        }


_SERVICE: Optional[UpiService] = None


def set_service(service: Optional[UpiService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[UpiService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('UPI_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryUpiStore()
    path = os.environ.get('UPI_DB', DEFAULT_STORE_PATH)
    return SqliteUpiStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[NpciClock] = None,
) -> UpiService:
    if store is None:
        store = default_store()
    return UpiService(
        UpiPolicy.from_env(),
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
        'beneficiary_duplicate': 409,
        'beneficiary_limit': 409,
        'payment_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_recalled': 409,
        'nsf': 409,
        'failed': 409,
        'recall_failed': 409,
        'upi_forbidden': 403,
        'upi_disabled': 403,
        'beneficiary_paused': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_cancelable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_recallable': 403,
        'not_overridable': 403,
        'not_exportable': 403,
        'return_window_closed': 403,
        'complaint_window_closed': 403,
        'beneficiary_not_found': 404,
        'payment_not_found': 404,
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
        'invalid_currency': 400,
        'invalid_reference': 400,
        'invalid_file': 400,
        'upi_amount_exceeded': 400,
        'imps_amount_exceeded': 400,
        'neft_amount_exceeded': 400,
        'rtgs_amount_exceeded': 400,
        'rtgs_amount_below_minimum': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_beneficiary': 400,
        'missing_payment': 400,
    }.get(code, 400)


def _error_body(exc: UpiError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('payment') is not None:
        body['payment'] = exc.extra['payment'].to_dict()
    if exc.extra.get('beneficiary') is not None:
        body['beneficiary'] = exc.extra['beneficiary'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except UpiError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list_upi(service: UpiService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Upi': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_add_beneficiary(service: UpiService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.add_beneficiary(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname'),
            legal_name=values.get('legal_name') or values.get('name'),
            rail=values.get('rail') or 'upi',
            vpa=values.get('vpa') or values.get('upi_id'),
            ifsc=values.get('ifsc'),
            account_number=values.get('account_number') or values.get('beneficiary_account'),
            mmid=values.get('mmid'),
            phone=values.get('phone') or values.get('mobile'),
            pan=values.get('pan'),
            default_account=values.get('default_account') or values.get('account') or values.get('from_account'),
        )
        return jsonify({
            'message': 'UPI beneficiary added',
            'beneficiary': row.to_dict(),
            'Upi': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _bene_status_route(service: UpiService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    beneficiary_id = str(values.get('beneficiary_id') or '').strip()
    if not beneficiary_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_beneficiary'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_beneficiary_status(
            beneficiary_id=beneficiary_id, actor=userid, actor_type=actor_type, status=status,
        )
        return jsonify({
            'message': ok_message,
            'beneficiary': row.to_dict(),
            'Upi': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: UpiService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    beneficiary_id = str(values.get('beneficiary_id') or '').strip()
    if not beneficiary_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_beneficiary'}), 400

    def _run():
        preview = service.preview(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            beneficiary_id=beneficiary_id,
            amount=values.get('amount'),
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({'preview': preview, 'Upi': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: UpiService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    beneficiary_id = str(values.get('beneficiary_id') or '').strip()
    if not beneficiary_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_beneficiary'}), 400

    def _run():
        payment, created = service.originate(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            beneficiary_id=beneficiary_id,
            amount=values.get('amount'),
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({
            'message': 'UPI originated' if created else 'UPI already posted',
            'payment': payment.to_dict(),
            'Upi': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: UpiService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    payment_id = str(values.get('payment_id') or '').strip()
    if not payment_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_payment'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        payment = service.cancel_payment(
            payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': 'UPI cancelled',
            'payment': payment.to_dict(),
            'Upi': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_upi_route(service: UpiService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    payment_id = str(values.get('payment_id') or '').strip()
    if not payment_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_payment'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'release':
            payment = service.release_payment(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'UPI released'
        elif action == 'reject':
            payment = service.reject_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'UPI rejected'
        elif action == 'recall':
            payment = service.recall_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'UPI recalled'
        elif action == 'override':
            payment = service.override_ofac(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'waive':
            payment = service.waive_fee(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'UPI fee waived'
        elif action == 'export':
            exported = service.export_upi1(payment_id=payment_id, actor=userid, actor_type=actor_type)
            payment = service.get_payment(payment_id=payment_id, actor=userid, actor_type=actor_type)
            return jsonify({
                'message': 'UPI exported',
                'export': exported,
                'payment': payment.to_dict(),
                'Upi': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
            }), 200
        else:
            raise UpiError('invalid_status', 'Unknown UPI action.')
        return jsonify({
            'message': message,
            'payment': payment.to_dict(),
            'Upi': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: UpiService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Upi': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_upi_routes(app, service: UpiService) -> None:
    @app.route('/listUpi', methods=['POST', 'GET'])
    def list_upi_route():
        return handle_list_upi(service)

    @app.route('/listUpiBeneficiaries', methods=['POST', 'GET'])
    def list_upi_beneficiaries_route():
        return handle_list_upi(service)

    @app.route('/addUpiBeneficiary', methods=['POST', 'GET'])
    def add_upi_beneficiary_route():
        return handle_add_beneficiary(service)

    @app.route('/pauseUpiBeneficiary', methods=['POST', 'GET'])
    def pause_upi_beneficiary_route():
        return _bene_status_route(service, BENE_PAUSED, 'UPI beneficiary paused')

    @app.route('/resumeUpiBeneficiary', methods=['POST', 'GET'])
    def resume_upi_beneficiary_route():
        return _bene_status_route(service, BENE_ACTIVE, 'UPI beneficiary resumed')

    @app.route('/archiveUpiBeneficiary', methods=['POST', 'GET'])
    def archive_upi_beneficiary_route():
        return _bene_status_route(service, BENE_ARCHIVED, 'UPI beneficiary archived')

    @app.route('/previewUpi', methods=['POST', 'GET'])
    def preview_upi_route():
        return handle_preview(service)

    @app.route('/sendUpi', methods=['POST', 'GET'])
    def send_upi_route():
        return handle_send(service)

    @app.route('/cancelUpi', methods=['POST', 'GET'])
    def cancel_upi_route():
        return handle_cancel(service)

    @app.route('/releaseUpi', methods=['POST', 'GET'])
    def release_upi_route():
        return _staff_upi_route(service, 'release')

    @app.route('/rejectUpi', methods=['POST', 'GET'])
    def reject_upi_route():
        return _staff_upi_route(service, 'reject')

    @app.route('/recallUpi', methods=['POST', 'GET'])
    def recall_upi_route():
        return _staff_upi_route(service, 'recall')

    @app.route('/overrideUpiOfac', methods=['POST', 'GET'])
    def override_upi_ofac_route():
        return _staff_upi_route(service, 'override')

    @app.route('/waiveUpiFee', methods=['POST', 'GET'])
    def waive_upi_fee_route():
        return _staff_upi_route(service, 'waive')

    @app.route('/exportUpi', methods=['POST', 'GET'])
    def export_upi_route():
        return _staff_upi_route(service, 'export')

    @app.route('/runDueUpi', methods=['POST', 'GET'])
    def run_due_upi_route():
        return handle_run_due(service)
