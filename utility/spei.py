"""SPEI / CoDi origination over Banxico rails.

Customers send MXN SPEI payments to an 18-digit CLABE, or 24/7 CoDi
payments to a Mexican phone overlay. Independent of domestic Fedwire
(PR #73), PIX BRL Dict keys (PR #137/#139), CHIPS, Zelle, SWIFT, ACH
linking (PR #68), and ISO 20022 investigation. Existing `/fundTransfer`,
`/withdrawAmount`, and `/sendWire` stay unchanged. `Customers.debit_request`
/ `credit_request` still write `debited` / `direct deposited` unless a
remark is supplied here.

Foundations (reusable beyond this screen):
- CLABE (18-digit) with 3-7-1 repeating check digit + 3-digit Banxico bank
- RFC (12 moral / 13 física) SAT check digit
- Clave de rastreo `KNH{BBB}{YYYYMMDD}{7}` (not IMAD, not PIX EndToEndId)
- Banxico session clock (06:00-18:00 CT) vs SPEI en línea / CoDi 24/7
- CEP1 / CODI1 pipe compose/parse (XML/DOCTYPE rejected)
- Devolución windows (CoDi 24h customer / SPEI next business day / 90-day staff)
- Dual-control release for high-value SPEI

Reuses Fedwire OFAC screening, money parsing, fee helper, and account
directory from `utility.wire`. Stores are pluggable (memory for tests,
sqlite WAL for restart-safe default). Full CLABE, RFC, phone, fingerprint,
and tracking keys never appear unmasked in to_dict / snapshots.
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
SPEI_HELD = 'held'
SPEI_QUEUED = 'queued'
SPEI_PENDING = 'pending_release'
SPEI_COMPLETED = 'completed'
SPEI_REJECTED = 'rejected'
SPEI_CANCELLED = 'cancelled'
SPEI_RECALLED = 'recalled'
SPEI_NSF = 'nsf'
SPEI_FAILED = 'failed'
SPEI_STATUSES = frozenset({
    SPEI_HELD, SPEI_QUEUED, SPEI_PENDING, SPEI_COMPLETED, SPEI_REJECTED,
    SPEI_CANCELLED, SPEI_RECALLED, SPEI_NSF, SPEI_FAILED,
})
OPEN_SPEI = frozenset({SPEI_HELD, SPEI_QUEUED, SPEI_PENDING})
CANCELABLE = frozenset({SPEI_HELD, SPEI_QUEUED, SPEI_PENDING})
FEE_NONE = 'none'
FEE_COLLECTED = 'collected'
FEE_WAIVED = 'waived'
FEE_NSF = 'nsf'
PURPOSES = frozenset({'family', 'goods', 'payroll', 'tax', 'loan', 'rent', 'other'})
PURPOSE_ALIASES = {
    'personal': 'family', 'gift': 'family', 'support': 'family',
    'invoice': 'goods', 'purchase': 'goods', 'vendor': 'goods',
    'salary': 'payroll', 'wage': 'payroll',
    'sat': 'tax', 'impuesto': 'tax', 'taxes': 'tax',
    'mortgage': 'loan', 'housing': 'rent', 'renta': 'rent',
}
RAILS = frozenset({'spei', 'codi'})
RAIL_ALIASES = {
    'spei_en_linea': 'spei', 'linea': 'spei', 'rtgs': 'spei', 'clabe': 'spei',
    'qr': 'codi', 'cobro': 'codi', 'cobro_digital': 'codi', 'p2p': 'codi',
}
DEFAULT_BANKS = {
    '002': 'BANAMEX',
    '012': 'BBVA MEXICO',
    '014': 'SANTANDER',
    '019': 'BANJERCITO',
    '021': 'HSBC',
    '030': 'BAJIO',
    '036': 'INBURSA',
    '042': 'MIFEL',
    '044': 'SCOTIABANK',
    '058': 'BANREGIO',
    '062': 'AFIRME',
    '072': 'BANORTE',
    '127': 'AZTECA',
    '137': 'BANCOPPEL',
    '646': 'STP',
}
SETTLEMENT_SPEI = 'spei'
SETTLEMENT_CODI = 'codi'
CEP1_VERSION = '1'
CEP1_FIELDS = 13
CODI1_FIELDS = 7
DEFAULT_STORE_PATH = 'SystemLogs/spei.sqlite'
DEFAULT_SOURCE_BANK = '646'
CUSTOMER_CODI_RETURN_SECONDS = 24 * 60 * 60
STAFF_DEVOLUCION_DAYS = 90
RFC_TAB = {
    '0': 0, '1': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7, '8': 8, '9': 9,
    'A': 10, 'B': 11, 'C': 12, 'D': 13, 'E': 14, 'F': 15, 'G': 16, 'H': 17, 'I': 18,
    'J': 19, 'K': 20, 'L': 21, 'M': 22, 'N': 23, '&': 24, 'O': 25, 'P': 26, 'Q': 27,
    'R': 28, 'S': 29, 'T': 30, 'U': 31, 'V': 32, 'W': 33, 'X': 34, 'Y': 35, 'Z': 36,
    ' ': 37, 'Ñ': 38,
}
CLABE_WEIGHTS = (3, 7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1, 3, 7)


class SpeiError(ValueError):
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
        raise SpeiError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise SpeiError('invalid_name', 'Beneficiary legal name must be 2-80 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise SpeiError('invalid_purpose', 'Unknown SPEI purpose.')
    return text


def normalize_currency(value: Any, *, default: str = 'MXN') -> str:
    text = str(value or default).strip().upper()
    if text not in {'MXN', 'MXP'}:
        raise SpeiError('invalid_currency', 'SPEI / CoDi is MXN only.')
    return 'MXN'


def normalize_rail(value: Any, *, default: str = 'spei') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text not in RAILS:
        raise SpeiError('invalid_rail', 'Rail must be spei or codi.')
    return text


def normalize_bank(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        raise SpeiError('invalid_bank', 'Banxico bank code is required.')
    if len(digits) < 3:
        digits = digits.zfill(3)
    if len(digits) != 3:
        raise SpeiError('invalid_bank', 'Banxico bank code must be three digits.')
    if digits == '000':
        raise SpeiError('invalid_bank', 'Banxico bank code is invalid.')
    return digits


def mask_bank(value: Any) -> str:
    bank = normalize_bank(value)
    return bank[0] + '*' + bank[2]


def bank_name(value: Any, *, directory: Optional[Dict[str, str]] = None) -> str:
    try:
        bank = normalize_bank(value)
    except SpeiError:
        return ''
    mapping = directory if directory is not None else DEFAULT_BANKS
    return mapping.get(bank, '')


def clabe_check_digit(body: str) -> str:
    if len(body) != 17 or not body.isdigit():
        raise SpeiError('invalid_clabe', 'CLABE body must be 17 digits.')
    total = sum(int(digit) * weight for digit, weight in zip(body, CLABE_WEIGHTS))
    return str((10 - (total % 10)) % 10)


def clabe_check_digit_ok(digits: str) -> bool:
    if len(digits) != 18 or not digits.isdigit() or digits == '0' * 18:
        return False
    return clabe_check_digit(digits[:17]) == digits[17]


def compose_clabe(bank: Any, plaza: Any, account: Any) -> str:
    bank_code = normalize_bank(bank)
    plaza_digits = ''.join(ch for ch in str(plaza or '') if ch.isdigit()).zfill(3)
    if len(plaza_digits) != 3:
        raise SpeiError('invalid_clabe', 'Plaza must be three digits.')
    account_digits = ''.join(ch for ch in str(account or '') if ch.isdigit()).zfill(11)
    if len(account_digits) != 11:
        raise SpeiError('invalid_clabe', 'CLABE account must be 11 digits.')
    body = bank_code + plaza_digits + account_digits
    return body + clabe_check_digit(body)


def normalize_clabe(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise SpeiError('invalid_clabe', 'CLABE is required.')
        return ''
    if len(digits) != 18 or not clabe_check_digit_ok(digits):
        raise SpeiError('invalid_clabe', 'CLABE failed checksum.')
    return digits


def mask_clabe(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '***'
    return '**************' + digits[-4:]


def rfc_check_digit(body: str) -> str:
    text = str(body or '').upper()
    if len(text) not in {11, 12}:
        raise SpeiError('invalid_rfc', 'RFC body must be 11 or 12 characters.')
    total = 0
    factor = len(text) + 1
    for index, char in enumerate(text):
        if char not in RFC_TAB:
            raise SpeiError('invalid_rfc', 'RFC contains an invalid character.')
        total += RFC_TAB[char] * (factor - index)
    remainder = total % 11
    if remainder == 0:
        return '0'
    digit = 11 - remainder
    if digit == 10:
        return 'A'
    return str(digit)


def rfc_check_digit_ok(value: str) -> bool:
    text = str(value or '').upper()
    if len(text) not in {12, 13}:
        return False
    try:
        return rfc_check_digit(text[:-1]) == text[-1]
    except SpeiError:
        return False


def compose_rfc(body: Any) -> str:
    text = re.sub(r'[^A-Z0-9&Ñ ]', '', str(body or '').upper())
    if len(text) not in {11, 12}:
        raise SpeiError('invalid_rfc', 'RFC body must be 11 or 12 characters.')
    return text + rfc_check_digit(text)


def normalize_rfc(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[\s\-.]', '', str(value or '').upper())
    if not text:
        if required:
            raise SpeiError('invalid_rfc', 'RFC is required.')
        return ''
    if len(text) not in {12, 13} or not rfc_check_digit_ok(text):
        raise SpeiError('invalid_rfc', 'RFC failed checksum.')
    date_part = text[3:9] if len(text) == 12 else text[4:10]
    try:
        year = int(date_part[0:2])
        month = int(date_part[2:4])
        day = int(date_part[4:6])
        date(2000 + year if year < 30 else 1900 + year, month, day)
    except ValueError as exc:
        raise SpeiError('invalid_rfc', 'RFC date is invalid.') from exc
    return text


def mask_rfc(value: Any) -> str:
    text = str(value or '').upper()
    if len(text) < 4:
        return '***'
    return text[0] + ('*' * (len(text) - 3)) + text[-2:]


def normalize_phone(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise SpeiError('invalid_phone', 'Mexican phone is required for CoDi.')
        return ''
    if digits.startswith('52') and len(digits) == 12:
        national = digits[2:]
    elif len(digits) == 10:
        national = digits
    elif digits.startswith('52') and len(digits) == 13 and digits[2] == '1':
        national = digits[3:]
    else:
        raise SpeiError('invalid_phone', 'Mexican phone must be 10 national digits.')
    if len(national) != 10 or national[0] == '0':
        raise SpeiError('invalid_phone', 'Mexican phone must be 10 national digits.')
    return '+52' + national


def mask_phone(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '***-***-****'
    national = digits[-10:] if len(digits) >= 10 else digits
    return '***-***-' + national[-4:]


def destination_fingerprint(rail: str, clabe: str, phone: str) -> str:
    material = '%s|%s|%s' % (rail, clabe or '', phone or '')
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def compose_tracking_key(bank: str, cycle_date: str, sequence: int) -> str:
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise SpeiError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 9999999:
        raise SpeiError('invalid_reference', 'Tracking sequence out of range.')
    return 'KNH%s%s%07d' % (normalize_bank(bank), day, seq)


def normalize_tracking_key(value: Any) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not (15 <= len(text) <= 30) or not text.startswith('KNH'):
        raise SpeiError('invalid_reference', 'Clave de rastreo is invalid.')
    return text


def mask_tracking_key(value: Any) -> str:
    text = str(value or '')
    if len(text) < 7:
        return 'KNH***'
    return 'KNH************' + text[-4:]


def compose_amount_field(amount: Decimal) -> str:
    quantized = amount.quantize(MONEY_QUANTUM)
    if quantized < 0 or quantized > Decimal('999999999.99'):
        raise SpeiError('invalid_amount', 'Amount cannot be encoded in CEP1.')
    return money_str(quantized)


def parse_amount_field(value: Any) -> Decimal:
    try:
        return parse_money(value)
    except AmountError as exc:
        raise SpeiError('invalid_amount', 'CEP1 amount field is invalid.') from exc


def _reject_markup(text: str) -> None:
    lowered = text.lower()
    if '<xml' in lowered or '<!doctype' in lowered or '<?xml' in lowered:
        raise SpeiError('invalid_file', 'XML/DOCTYPE CEP1 files are rejected.')


def compose_cep1(
    *,
    tracking_key: str,
    send_bank: str,
    recv_bank: str,
    clabe: str,
    rfc: str,
    phone: str,
    amount: Decimal,
    purpose: str,
    status: str,
    rail: str,
    mask: bool = True,
) -> str:
    tracking = normalize_tracking_key(tracking_key)
    kind = normalize_rail(rail)
    return '|'.join((
        'CEP1',
        CEP1_VERSION,
        mask_tracking_key(tracking) if mask else tracking,
        mask_bank(send_bank) if mask else normalize_bank(send_bank),
        mask_bank(recv_bank) if mask else normalize_bank(recv_bank),
        mask_clabe(clabe) if mask else (normalize_clabe(clabe, required=False) or ''),
        mask_rfc(rfc) if mask and rfc else (normalize_rfc(rfc, required=False) if rfc else ''),
        mask_phone(phone) if mask and phone else (normalize_phone(phone) if phone else ''),
        compose_amount_field(amount),
        normalize_purpose(purpose),
        str(status or SPEI_COMPLETED),
        kind,
        SETTLEMENT_CODI if kind == 'codi' else SETTLEMENT_SPEI,
    ))


def parse_cep1(text: Any) -> Dict[str, Any]:
    raw = str(text or '').strip()
    if not raw:
        raise SpeiError('invalid_file', 'CEP1 record is empty.')
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != CEP1_FIELDS:
        raise SpeiError('invalid_file', 'CEP1 record must have %d fields.' % CEP1_FIELDS)
    if parts[0] != 'CEP1' or parts[1] != CEP1_VERSION:
        raise SpeiError('invalid_file', 'CEP1 version marker is invalid.')
    if parts[12] not in {SETTLEMENT_SPEI, SETTLEMENT_CODI}:
        raise SpeiError('invalid_file', 'CEP1 settlement method must be spei or codi.')
    if '*' in parts[2] or '***' in parts[5]:
        raise SpeiError('invalid_file', 'Masked CEP1 record cannot be re-ingested.')
    return {
        'tracking_key': normalize_tracking_key(parts[2]),
        'send_bank': normalize_bank(parts[3]),
        'recv_bank': normalize_bank(parts[4]),
        'clabe': parts[5],
        'rfc': parts[6],
        'phone': parts[7],
        'amount': money_str(parse_amount_field(parts[8])),
        'purpose': normalize_purpose(parts[9]),
        'status': parts[10],
        'rail': normalize_rail(parts[11]),
        'settlement_method': parts[12],
    }


def compose_codi1(
    *,
    phone: str,
    amount: Decimal,
    tracking_key: str,
    reference: str = '',
    mask: bool = True,
) -> str:
    tracking = normalize_tracking_key(tracking_key)
    return '|'.join((
        'CODI1',
        CEP1_VERSION,
        mask_phone(phone) if mask else normalize_phone(phone, required=True),
        compose_amount_field(amount),
        mask_tracking_key(tracking) if mask else tracking,
        normalize_note(reference, limit=40),
        'MXN',
    ))


def parse_codi1(text: Any) -> Dict[str, Any]:
    raw = str(text or '').strip()
    if not raw:
        raise SpeiError('invalid_file', 'CODI1 record is empty.')
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != CODI1_FIELDS:
        raise SpeiError('invalid_file', 'CODI1 record must have %d fields.' % CODI1_FIELDS)
    if parts[0] != 'CODI1' or parts[1] != CEP1_VERSION or parts[6] != 'MXN':
        raise SpeiError('invalid_file', 'CODI1 version marker is invalid.')
    if '*' in parts[2] or '*' in parts[4]:
        raise SpeiError('invalid_file', 'Masked CODI1 record cannot be re-ingested.')
    return {
        'phone': normalize_phone(parts[2], required=True),
        'amount': money_str(parse_amount_field(parts[3])),
        'tracking_key': normalize_tracking_key(parts[4]),
        'reference': parts[5],
        'currency': 'MXN',
    }


def split_cep1_file(text: Any) -> List[str]:
    raw = str(text or '')
    _reject_markup(raw)
    records = [line.strip() for line in raw.replace('\r\n', '\n').split('\n') if line.strip()]
    if not records:
        raise SpeiError('invalid_file', 'CEP1 file is empty.')
    return records


def easter_gregorian(year: int) -> date:
    """Anonymous Gregorian Easter (used for Jueves/Viernes Santo)."""
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


def mexico_holidays(year: int) -> set:
    """Banxico rest days (Ley Federal del Trabajo movable Mondays)."""
    easter = easter_gregorian(year)
    days = {
        date(year, 1, 1),
        _nth_weekday(year, 2, 0, 1),
        _nth_weekday(year, 3, 0, 3),
        easter - timedelta(days=3),
        easter - timedelta(days=2),
        date(year, 5, 1),
        date(year, 9, 16),
        _nth_weekday(year, 11, 0, 3),
        date(year, 12, 25),
    }
    if year >= 2024 and (year - 2024) % 6 == 0:
        days.add(date(year, 10, 1))
    if year < 2024 and year >= 2006 and (year - 2018) % 6 == 0:
        days.add(date(year, 12, 1))
    return days


class SpeiClock:
    """Banxico SPEI session clock. CoDi / SPEI en línea stay 24/7 under the cap."""

    def __init__(
        self,
        *,
        open_hour: int = 6,
        cutoff_hour: int = 18,
        cutoff_minute: int = 0,
        tz_offset_hours: int = -6,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.open_hour = int(open_hour)
        self.cutoff_hour = int(cutoff_hour)
        self.cutoff_minute = int(cutoff_minute)
        self.tz_offset_hours = int(tz_offset_hours)
        self.tz = timezone(timedelta(hours=self.tz_offset_hours))
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
        return day.weekday() >= 5

    def is_holiday(self, day: date) -> bool:
        return day in mexico_holidays(day.year) or day in self.extra_holidays

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
        if self.is_business_day(day) and not self.is_after_cutoff(ts):
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
            'timezone': 'CT',
            'open': '%02d:00' % self.open_hour,
            'cutoff': '%02d:%02d' % (self.cutoff_hour, self.cutoff_minute),
            'session': session,
            'after_cutoff': not session,
            'weekend': self.is_weekend(day),
            'holiday': self.is_holiday(day),
            'business_day': self.is_business_day(day),
            'value_date': self.value_date(ts).isoformat(),
            'cycle_date': self.cycle_date(ts),
            'settlement_method': SETTLEMENT_SPEI,
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
class SpeiPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    allow_credit: bool = False
    max_beneficiaries: int = 12
    max_payments: int = 120
    min_amount: Decimal = Decimal('0.01')
    session_max: Decimal = Decimal('1000000.00')
    online_max: Decimal = Decimal('8000.00')
    codi_max: Decimal = Decimal('8000.00')
    outbound_fee: Decimal = Decimal('0.00')
    dual_control_threshold: Decimal = Decimal('200000.00')
    tz_offset_hours: int = -6
    open_hour: int = 6
    cutoff_hour: int = 18
    source_bank: str = DEFAULT_SOURCE_BANK
    customer_codi_return_seconds: int = CUSTOMER_CODI_RETURN_SECONDS
    staff_devolucion_days: int = STAFF_DEVOLUCION_DAYS
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()
    banks: Dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.banks is None:
            self.banks = dict(DEFAULT_BANKS)

    @classmethod
    def from_env(cls) -> 'SpeiPolicy':
        extra = _env_list('SPEI_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        extra_banks = _env_list('SPEI_BANKS')
        directory = dict(DEFAULT_BANKS)
        for item in extra_banks:
            if ':' in item:
                pid, _, label = item.partition(':')
                try:
                    directory[normalize_bank(pid)] = label.strip() or directory.get(normalize_bank(pid), '')
                except SpeiError:
                    continue
        source = os.environ.get('SPEI_SOURCE', DEFAULT_SOURCE_BANK)
        try:
            source_bank = normalize_bank(source)
        except SpeiError:
            source_bank = DEFAULT_SOURCE_BANK
        return cls(
            enabled=_env_bool('SPEI_ENABLED', True),
            customer_manage=_env_bool('SPEI_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('SPEI_CUSTOMER_SEND', True),
            allow_credit=_env_bool('SPEI_ALLOW_CREDIT', False),
            max_beneficiaries=max(1, _env_int('SPEI_MAX_BENEFICIARIES', 12)),
            max_payments=max(1, _env_int('SPEI_MAX_PAYMENTS', 120)),
            min_amount=_env_money('SPEI_MIN_AMOUNT', '0.01'),
            session_max=_env_money('SPEI_SESSION_MAX', '1000000.00'),
            online_max=_env_money('SPEI_ONLINE_MAX', '8000.00'),
            codi_max=_env_money('SPEI_CODI_MAX', '8000.00'),
            outbound_fee=_env_money('SPEI_FEE', '0.00'),
            dual_control_threshold=_env_money('SPEI_DUAL_CONTROL', '200000.00'),
            tz_offset_hours=_env_int('SPEI_TZ_OFFSET', -6),
            open_hour=max(0, min(23, _env_int('SPEI_OPEN_HOUR', 6))),
            cutoff_hour=max(0, min(23, _env_int('SPEI_CUTOFF_HOUR', 18))),
            source_bank=source_bank,
            customer_codi_return_seconds=max(60, _env_int(
                'SPEI_CODI_RETURN_SECONDS', CUSTOMER_CODI_RETURN_SECONDS,
            )),
            staff_devolucion_days=max(1, _env_int('SPEI_DEVOLUCION_DAYS', STAFF_DEVOLUCION_DAYS)),
            watchlist=watch,
            extra_holidays=_env_list('SPEI_HOLIDAYS'),
            banks=directory,
        )


@dataclass
class SpeiBeneficiary:
    beneficiary_id: str
    userid: str
    nickname: str
    legal_name: str
    rail: str
    clabe: str
    rfc: str
    phone: str
    fingerprint: str
    default_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float
    bank_label: str = ''

    def to_dict(self) -> Dict[str, Any]:
        bank = self.clabe[:3] if len(self.clabe) >= 3 else ''
        return {
            'beneficiary_id': self.beneficiary_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'rail': self.rail,
            'clabe_masked': mask_clabe(self.clabe) if self.clabe else '',
            'rfc_masked': mask_rfc(self.rfc) if self.rfc else '',
            'phone_masked': mask_phone(self.phone) if self.phone else '',
            'bank_code': bank,
            'bank_label': self.bank_label,
            'default_account': self.default_account,
            'account_last4': last4(self.default_account),
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == BENE_ACTIVE,
            'paused': self.status == BENE_PAUSED,
            'archived': self.status == BENE_ARCHIVED,
        }


@dataclass
class SpeiPayment:
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
    clabe_masked: str
    rfc_masked: str
    phone_masked: str
    bank_code: str
    purpose: str
    memo: str
    status: str
    tracking_key: str
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
    settlement_method: str = SETTLEMENT_SPEI

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
            'currency': 'MXN',
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'rail': self.rail,
            'clabe_masked': self.clabe_masked,
            'rfc_masked': self.rfc_masked,
            'phone_masked': self.phone_masked,
            'bank_code': self.bank_code,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'tracking_key_masked': mask_tracking_key(self.tracking_key) if self.tracking_key else '',
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
            'recallable': self.status == SPEI_COMPLETED,
        }


def _clone_bene(row: SpeiBeneficiary) -> SpeiBeneficiary:
    return SpeiBeneficiary(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_pay(row: SpeiPayment) -> SpeiPayment:
    return SpeiPayment(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _bene_from_row(row: Any) -> SpeiBeneficiary:
    return SpeiBeneficiary(
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        rail=row['rail'],
        clabe=row['clabe'],
        rfc=row['rfc'],
        phone=row['phone'],
        fingerprint=row['fingerprint'],
        default_account=row['default_account'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        bank_label=row['bank_label'] if 'bank_label' in row.keys() else '',
    )


def _pay_from_row(row: Any) -> SpeiPayment:
    return SpeiPayment(
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
        clabe_masked=row['clabe_masked'],
        rfc_masked=row['rfc_masked'],
        phone_masked=row['phone_masked'],
        bank_code=row['bank_code'],
        purpose=row['purpose'],
        memo=row['memo'],
        status=row['status'],
        tracking_key=row['tracking_key'],
        value_date=row['value_date'],
        actor=row['actor'],
        releaser=row['releaser'],
        ofac_hit=int(row['ofac_hit']),
        ofac_match=row['ofac_match'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        completed_at=float(row['completed_at'] or 0),
        recalled_at=float(row['recalled_at'] or 0),
        note=row['note'] or '',
        reason=row['reason'] or '',
        settlement_method=row['settlement_method'] or SETTLEMENT_SPEI,
    )


class MemorySpeiStore:
    def __init__(self) -> None:
        self._benes: Dict[str, SpeiBeneficiary] = {}
        self._pays: Dict[str, SpeiPayment] = {}
        self._lock = threading.Lock()

    def put_beneficiary(self, row: SpeiBeneficiary) -> None:
        self._benes[row.beneficiary_id] = _clone_bene(row)

    def get_beneficiary(self, beneficiary_id: str) -> Optional[SpeiBeneficiary]:
        row = self._benes.get(beneficiary_id)
        return _clone_bene(row) if row else None

    def update_beneficiary(self, row: SpeiBeneficiary) -> None:
        self._benes[row.beneficiary_id] = _clone_bene(row)

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[SpeiBeneficiary]:
        rows = list(self._benes.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != BENE_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return [_clone_bene(row) for row in rows]

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[SpeiBeneficiary]:
        wanted = nickname.strip().lower()
        for row in self._benes.values():
            if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_BENE:
                return _clone_bene(row)
        return None

    def find_beneficiary_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[SpeiBeneficiary]:
        for row in self._benes.values():
            if row.userid == userid and row.fingerprint == fingerprint and row.status in OPEN_BENE:
                return _clone_bene(row)
        return None

    def put_payment(self, row: SpeiPayment) -> SpeiPayment:
        with self._lock:
            self._pays[row.payment_id] = _clone_pay(row)
            return _clone_pay(row)

    def update_payment(self, row: SpeiPayment) -> None:
        self._pays[row.payment_id] = _clone_pay(row)

    def get_payment(self, payment_id: str) -> Optional[SpeiPayment]:
        row = self._pays.get(payment_id)
        return _clone_pay(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[SpeiPayment]:
        for row in self._pays.values():
            if row.trace_id == trace_id:
                return _clone_pay(row)
        return None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[SpeiPayment]:
        rows = list(self._pays.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if beneficiary_id is not None:
            rows = [row for row in rows if row.beneficiary_id == beneficiary_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return [_clone_pay(row) for row in rows]

    def next_sequence(self, cycle_date: str) -> int:
        with self._lock:
            used = [
                row.tracking_key for row in self._pays.values()
                if row.tracking_key and len(row.tracking_key) >= 14 and row.tracking_key[6:14] == cycle_date
            ]
        return len(used) + 1


class SqliteSpeiStore:
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
                    clabe TEXT NOT NULL DEFAULT '',
                    rfc TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
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
                    clabe_masked TEXT NOT NULL DEFAULT '',
                    rfc_masked TEXT NOT NULL DEFAULT '',
                    phone_masked TEXT NOT NULL DEFAULT '',
                    bank_code TEXT NOT NULL DEFAULT '',
                    purpose TEXT NOT NULL,
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    tracking_key TEXT NOT NULL DEFAULT '',
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
                    settlement_method TEXT NOT NULL DEFAULT 'spei'
                )
                """
            )
            conn.commit()

    def put_beneficiary(self, row: SpeiBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO beneficiaries (
                    beneficiary_id, userid, nickname, legal_name, rail, clabe, rfc,
                    phone, fingerprint, default_account, status, actor, created_at,
                    updated_at, bank_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.beneficiary_id, row.userid, row.nickname, row.legal_name,
                    row.rail, row.clabe, row.rfc, row.phone, row.fingerprint,
                    row.default_account, row.status, row.actor, row.created_at,
                    row.updated_at, row.bank_label,
                ),
            )
            conn.commit()

    def get_beneficiary(self, beneficiary_id: str) -> Optional[SpeiBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM beneficiaries WHERE beneficiary_id = ?', (beneficiary_id,),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def update_beneficiary(self, row: SpeiBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE beneficiaries SET nickname=?, legal_name=?, rail=?, clabe=?,
                    rfc=?, phone=?, fingerprint=?, default_account=?, status=?,
                    actor=?, updated_at=?, bank_label=?
                WHERE beneficiary_id=?
                """,
                (
                    row.nickname, row.legal_name, row.rail, row.clabe, row.rfc,
                    row.phone, row.fingerprint, row.default_account, row.status,
                    row.actor, row.updated_at, row.bank_label, row.beneficiary_id,
                ),
            )
            conn.commit()

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[SpeiBeneficiary]:
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

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[SpeiBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM beneficiaries
                WHERE userid = ? AND lower(nickname) = lower(?) AND status IN ('active', 'paused')
                """,
                (userid, nickname),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def find_beneficiary_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[SpeiBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM beneficiaries
                WHERE userid = ? AND fingerprint = ? AND status IN ('active', 'paused')
                """,
                (userid, fingerprint),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def put_payment(self, row: SpeiPayment) -> SpeiPayment:
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
                    amount, fee, fee_status, nickname, legal_name, rail, clabe_masked,
                    rfc_masked, phone_masked, bank_code, purpose, memo, status,
                    tracking_key, value_date, actor, releaser, ofac_hit, ofac_match,
                    created_at, updated_at, completed_at, recalled_at, note, reason,
                    settlement_method
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.payment_id, row.trace_id, row.beneficiary_id, row.userid,
                    row.internal_account, row.amount, row.fee, row.fee_status,
                    row.nickname, row.legal_name, row.rail, row.clabe_masked,
                    row.rfc_masked, row.phone_masked, row.bank_code, row.purpose,
                    row.memo, row.status, row.tracking_key, row.value_date,
                    row.actor, row.releaser, row.ofac_hit, row.ofac_match,
                    row.created_at, row.updated_at, row.completed_at, row.recalled_at,
                    row.note, row.reason, row.settlement_method,
                ),
            )
            conn.commit()
        return row

    def update_payment(self, row: SpeiPayment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE payments SET amount=?, fee=?, fee_status=?, status=?,
                    tracking_key=?, value_date=?, actor=?, releaser=?, ofac_hit=?,
                    ofac_match=?, updated_at=?, completed_at=?, recalled_at=?,
                    note=?, reason=?
                WHERE payment_id=?
                """,
                (
                    row.amount, row.fee, row.fee_status, row.status, row.tracking_key,
                    row.value_date, row.actor, row.releaser, row.ofac_hit,
                    row.ofac_match, row.updated_at, row.completed_at, row.recalled_at,
                    row.note, row.reason, row.payment_id,
                ),
            )
            conn.commit()

    def get_payment(self, payment_id: str) -> Optional[SpeiPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE payment_id = ?', (payment_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[SpeiPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE trace_id = ?', (trace_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[SpeiPayment]:
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
                "SELECT COUNT(*) AS n FROM payments WHERE substr(tracking_key, 7, 8) = ?",
                (cycle_date,),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class SpeiService:
    def __init__(
        self,
        policy: SpeiPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[SpeiClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn or screen_name
        self.calendar = calendar or SpeiClock(
            open_hour=policy.open_hour,
            cutoff_hour=policy.cutoff_hour,
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise SpeiError('spei_disabled', 'SPEI is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise SpeiError('spei_forbidden', 'Customers cannot manage SPEI beneficiaries.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise SpeiError('spei_forbidden', 'Customers cannot originate SPEI.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise SpeiError('spei_forbidden', 'Staff only.')

    def _accounts(self, userid: str) -> Tuple[List[str], Dict[str, str]]:
        payload = self.accounts_fn(userid) if self.accounts_fn is not None else {}
        return own_accounts_from_customer_payload(payload), account_types_from_customer_payload(payload)

    def _assert_internal_account(self, userid: str, account: str) -> str:
        owned, types = self._accounts(userid)
        if owned and account not in owned:
            raise SpeiError('invalid_account', 'Account is not owned by this customer.')
        kind = types.get(account, '')
        if kind == 'credit' and not self.policy.allow_credit:
            raise SpeiError('credit_not_allowed', 'Credit accounts cannot originate SPEI.')
        return kind

    def _assert_amount(self, amount: Decimal, *, rail: str, session: bool) -> None:
        if amount < self.policy.min_amount:
            raise SpeiError('amount_out_of_range', 'Amount is outside the allowed range.')
        if rail == 'codi' and amount > self.policy.codi_max:
            raise SpeiError('codi_amount_exceeded', 'Amount exceeds the CoDi cap.')
        if rail == 'spei' and amount > self.policy.session_max:
            raise SpeiError('spei_amount_exceeded', 'Amount exceeds the SPEI session cap.')
        if rail == 'spei' and not session and amount > self.policy.online_max:
            return

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def _needs_queue(self, amount: Decimal, *, rail: str, session: bool) -> bool:
        if rail == 'codi':
            return False
        if session:
            return False
        return amount > self.policy.online_max

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
        rail: Any = 'spei',
        clabe: Any = '',
        rfc: Any = '',
        phone: Any = '',
        default_account: Any,
    ) -> SpeiBeneficiary:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise SpeiError('spei_forbidden', 'Not allowed to add SPEI beneficiaries for this customer.')
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        kind = normalize_rail(rail)
        clabe_value = normalize_clabe(clabe, required=kind == 'spei')
        rfc_value = normalize_rfc(rfc, required=kind == 'spei')
        phone_value = normalize_phone(phone, required=kind == 'codi')
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        fingerprint = destination_fingerprint(kind, clabe_value, phone_value)
        if self.store.find_beneficiary_by_nickname(owner_userid, name) is not None:
            raise SpeiError('beneficiary_duplicate', 'A SPEI beneficiary with that nickname already exists.')
        if self.store.find_beneficiary_by_fingerprint(owner_userid, fingerprint) is not None:
            raise SpeiError('beneficiary_duplicate', 'That CLABE or CoDi phone is already on file.')
        open_rows = [row for row in self.store.list_beneficiaries(owner_userid) if row.status in OPEN_BENE]
        if len(open_rows) >= self.policy.max_beneficiaries:
            raise SpeiError('beneficiary_limit', 'SPEI beneficiary limit reached.')
        now = float(self.clock())
        bank = clabe_value[:3] if clabe_value else ''
        row = SpeiBeneficiary(
            beneficiary_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            rail=kind,
            clabe=clabe_value,
            rfc=rfc_value,
            phone=phone_value,
            fingerprint=fingerprint,
            default_account=account,
            status=BENE_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
            bank_label=self.policy.banks.get(bank, ''),
        )
        self.store.put_beneficiary(row)
        return row

    def get_beneficiary(self, *, beneficiary_id: str, actor: str, actor_type: str) -> SpeiBeneficiary:
        self._require_enabled()
        row = self.store.get_beneficiary(beneficiary_id)
        if row is None:
            raise SpeiError('beneficiary_not_found', 'SPEI beneficiary not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise SpeiError('spei_forbidden', 'Not allowed to view this SPEI beneficiary.')
        return row

    def enforce_beneficiary(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> SpeiBeneficiary:
        row = self.get_beneficiary(beneficiary_id=beneficiary_id, actor=actor, actor_type=actor_type)
        if row.status == BENE_ARCHIVED:
            raise SpeiError('already_archived', 'SPEI beneficiary is archived.')
        if row.status == BENE_PAUSED:
            raise SpeiError('beneficiary_paused', 'SPEI beneficiary is paused.')
        if require_active and row.status != BENE_ACTIVE:
            raise SpeiError('invalid_status', 'SPEI beneficiary is not active.')
        return row

    def set_beneficiary_status(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> SpeiBeneficiary:
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
            raise SpeiError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == BENE_ARCHIVED:
            raise SpeiError('already_archived', 'SPEI beneficiary is already archived.')
        if wanted == BENE_PAUSED:
            if row.status == BENE_PAUSED:
                raise SpeiError('already_paused', 'SPEI beneficiary is already paused.')
            if row.status != BENE_ACTIVE:
                raise SpeiError('invalid_status', 'Only an active SPEI beneficiary can be paused.')
        elif wanted == BENE_ACTIVE:
            if row.status == BENE_ACTIVE:
                raise SpeiError('already_active', 'SPEI beneficiary is already active.')
            if row.status != BENE_PAUSED:
                raise SpeiError('invalid_status', 'Only a paused SPEI beneficiary can be resumed.')
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
            raise SpeiError('spei_forbidden', 'SPEI beneficiary does not belong to this customer.')
        pesos = parse_money(amount)
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        self._assert_amount(pesos, rail=bene.rail, session=bool(clock['session']))
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        fee = compute_fee(pesos, self.policy.outbound_fee, waived=bool(waive_fee) and actor_type in EMPLOYEE_ROLES)
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        queued = self._needs_queue(pesos, rail=bene.rail, session=bool(clock['session']))
        return {
            'amount': money_str(pesos),
            'fee': money_str(fee),
            'total': money_str(pesos + fee),
            'currency': 'MXN',
            'internal_account': account,
            'beneficiary': bene.to_dict(),
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(pesos),
            'queued': queued,
            'clock': clock,
            'online_cap': money_str(self.policy.online_max if bene.rail == 'spei' else self.policy.codi_max),
            'session_cap': money_str(self.policy.session_max),
            'settlement_method': SETTLEMENT_CODI if bene.rail == 'codi' else SETTLEMENT_SPEI,
        }

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        bene: SpeiBeneficiary,
        account: str,
        pesos: Decimal,
        fee: Decimal,
        purpose: str,
        memo: str,
        trace_id: str,
        ofac: ScreenResult,
        waive_fee: bool,
        session: bool,
    ) -> SpeiPayment:
        now = float(self.clock())
        value = self.calendar.cycle_date(now)
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        if ofac.hit:
            status = SPEI_HELD
        elif self._needs_dual_control(pesos):
            status = SPEI_PENDING
        elif self._needs_queue(pesos, rail=bene.rail, session=session):
            status = SPEI_QUEUED
            value = self.calendar.value_date(now).strftime('%Y%m%d')
        else:
            status = SPEI_COMPLETED
        payment = SpeiPayment(
            payment_id=uuid.uuid4().hex,
            trace_id=trace_id,
            beneficiary_id=bene.beneficiary_id,
            userid=owner_userid,
            internal_account=account,
            amount=money_str(pesos),
            fee=money_str(fee),
            fee_status=fee_status,
            nickname=bene.nickname,
            legal_name=bene.legal_name,
            rail=bene.rail,
            clabe_masked=mask_clabe(bene.clabe) if bene.clabe else '',
            rfc_masked=mask_rfc(bene.rfc) if bene.rfc else '',
            phone_masked=mask_phone(bene.phone) if bene.phone else '',
            bank_code=bene.clabe[:3] if bene.clabe else '',
            purpose=purpose,
            memo=memo,
            status=status,
            tracking_key='',
            value_date=value,
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
            settlement_method=SETTLEMENT_CODI if bene.rail == 'codi' else SETTLEMENT_SPEI,
        )
        if status == SPEI_COMPLETED:
            self._transmit(payment, actor=actor, bene=bene)
        return payment

    def _bene_secrets(self, payment: SpeiPayment) -> Tuple[str, str, str]:
        bene = self.store.get_beneficiary(payment.beneficiary_id)
        if bene is None:
            return '', '', ''
        return bene.clabe, bene.rfc, bene.phone

    def _transmit(
        self,
        payment: SpeiPayment,
        *,
        actor: str,
        bene: Optional[SpeiBeneficiary] = None,
    ) -> SpeiPayment:
        now = float(self.clock())
        cycle = payment.value_date or self.calendar.cycle_date(now)
        seq = self.store.next_sequence(cycle)
        payment.tracking_key = compose_tracking_key(self.policy.source_bank, cycle, seq)
        pesos = parse_money(payment.amount)
        fee = parse_money(payment.fee, allow_zero=True)
        remark = '%s to %s' % (payment.rail, payment.nickname)
        status = SPEI_COMPLETED
        fail_note = ''
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(payment.internal_account, money_str(pesos), remark)
            except Exception as exc:
                status = SPEI_FAILED
                fail_note = str(exc)[:240]
            else:
                classified = _classify_money_result(result)
                if classified == 'nsf':
                    status = SPEI_NSF
                    payment.tracking_key = ''
                elif classified != 'ok':
                    status = SPEI_FAILED
                    fail_note = str(result)[:240]
                    payment.tracking_key = ''
        if status == SPEI_COMPLETED and fee > 0 and payment.fee_status != FEE_WAIVED and self.debit_fn is not None:
            fee_remark = '%s fee %s' % (payment.rail, mask_tracking_key(payment.tracking_key))
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
        payment.completed_at = now if status == SPEI_COMPLETED else 0.0
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
    ) -> Tuple[SpeiPayment, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise SpeiError('spei_forbidden', 'Not allowed to originate SPEI for this customer.')
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise SpeiError('spei_forbidden', 'SPEI beneficiary does not belong to this customer.')
        trace = normalize_id(trace_id)
        existing = self.store.get_payment_by_trace(trace)
        if existing is not None:
            return existing, False
        pesos = parse_money(amount)
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        self._assert_amount(pesos, rail=bene.rail, session=bool(clock['session']))
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        fee = compute_fee(
            pesos, self.policy.outbound_fee,
            waived=bool(waive_fee) and actor_type in EMPLOYEE_ROLES,
        )
        if len(self.store.list_payments(owner_userid)) >= self.policy.max_payments:
            raise SpeiError('payment_limit', 'SPEI history limit reached.')
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        payment = self._place(
            owner_userid=owner_userid,
            actor=actor,
            bene=bene,
            account=account,
            pesos=pesos,
            fee=fee,
            purpose=normalize_purpose(purpose),
            memo=normalize_note(memo, limit=140),
            trace_id=trace,
            ofac=ofac,
            waive_fee=bool(waive_fee) and actor_type in EMPLOYEE_ROLES,
            session=bool(clock['session']),
        )
        stored = self.store.put_payment(payment)
        if stored.status == SPEI_NSF:
            raise SpeiError('nsf', 'Insufficient funds for SPEI.', payment=stored)
        if stored.status == SPEI_FAILED:
            raise SpeiError('failed', 'SPEI debit did not complete.', payment=stored)
        return stored, True

    def get_payment(self, *, payment_id: str, actor: str, actor_type: str) -> SpeiPayment:
        self._require_enabled()
        row = self.store.get_payment(payment_id)
        if row is None:
            raise SpeiError('payment_not_found', 'SPEI payment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise SpeiError('spei_forbidden', 'Not allowed to view this SPEI payment.')
        return row

    def cancel_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> SpeiPayment:
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and payment.userid != actor:
            raise SpeiError('spei_forbidden', 'Not allowed to cancel this SPEI.')
        if payment.status not in CANCELABLE:
            raise SpeiError('not_cancelable', 'Only held, queued, or pending SPEI can be cancelled.')
        payment.status = SPEI_CANCELLED
        payment.note = normalize_note(note)
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        self.store.update_payment(payment)
        return payment

    def waive_fee(self, *, payment_id: str, actor: str, actor_type: str) -> SpeiPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status not in CANCELABLE:
            raise SpeiError('invalid_status', 'Fee can only be waived before the SPEI is sent.')
        payment.fee = money_str(Decimal('0.00'))
        payment.fee_status = FEE_WAIVED
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        self.store.update_payment(payment)
        return payment

    def _requeue_or_transmit(self, payment: SpeiPayment, *, actor: str) -> SpeiPayment:
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        pesos = parse_money(payment.amount)
        if self._needs_queue(pesos, rail=payment.rail, session=bool(clock['session'])):
            payment.status = SPEI_QUEUED
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
    ) -> SpeiPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status != SPEI_HELD:
            raise SpeiError('not_overridable', 'Only an OFAC hold can be overridden.')
        now = float(self.clock())
        payment.ofac_hit = 0
        payment.note = normalize_note(note) or 'ofac override'
        payment.actor = str(actor)
        payment.updated_at = now
        pesos = parse_money(payment.amount)
        if self._needs_dual_control(pesos):
            payment.status = SPEI_PENDING
        else:
            self._requeue_or_transmit(payment, actor=actor)
        self.store.update_payment(payment)
        if payment.status == SPEI_NSF:
            raise SpeiError('nsf', 'Insufficient funds for SPEI.', payment=payment)
        if payment.status == SPEI_FAILED:
            raise SpeiError('failed', 'SPEI debit did not complete.', payment=payment)
        return payment

    def release_payment(self, *, payment_id: str, actor: str, actor_type: str) -> SpeiPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == SPEI_HELD:
            raise SpeiError('ofac_hold', 'OFAC hold must be overridden before release.')
        if payment.status not in {SPEI_PENDING, SPEI_QUEUED}:
            raise SpeiError('not_releasable', 'Only queued or pending SPEI can be released.')
        if (
            payment.status == SPEI_PENDING
            and payment.actor
            and str(actor) == str(payment.actor)
            and parse_money(payment.amount) >= self.policy.dual_control_threshold
        ):
            raise SpeiError('same_approver', 'A different employee must release this SPEI.')
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        pesos = parse_money(payment.amount)
        if payment.status == SPEI_QUEUED and self._needs_queue(pesos, rail=payment.rail, session=bool(clock['session'])):
            payment.updated_at = now
            self.store.update_payment(payment)
            return payment
        self._requeue_or_transmit(payment, actor=actor)
        self.store.update_payment(payment)
        if payment.status == SPEI_NSF:
            raise SpeiError('nsf', 'Insufficient funds for SPEI.', payment=payment)
        if payment.status == SPEI_FAILED:
            raise SpeiError('failed', 'SPEI debit did not complete.', payment=payment)
        return payment

    def reject_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> SpeiPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status not in CANCELABLE:
            raise SpeiError('not_rejectable', 'Only held, queued, or pending SPEI can be rejected.')
        payment.status = SPEI_REJECTED
        payment.reason = normalize_note(reason, limit=40)
        payment.note = normalize_note(note)
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        self.store.update_payment(payment)
        return payment

    def _assert_devolucion_window(self, payment: SpeiPayment, *, actor_type: str) -> None:
        now = float(self.clock())
        completed = float(payment.completed_at or payment.updated_at or payment.created_at)
        if actor_type in EMPLOYEE_ROLES:
            if now - completed > self.policy.staff_devolucion_days * 86400:
                raise SpeiError('devolucion_window_closed', 'Staff SPEI devolución window has closed.')
            return
        if payment.rail == 'codi':
            if now - completed > self.policy.customer_codi_return_seconds:
                raise SpeiError('return_window_closed', 'Customer CoDi return window has closed.')
            return
        completed_day = self.calendar.local_dt(completed).date()
        deadline_day = self.calendar.next_business_day(completed_day)
        deadline = self.calendar.session_close_ts(deadline_day)
        if now > deadline:
            raise SpeiError('return_window_closed', 'Customer SPEI devolución window has closed.')

    def recall_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> SpeiPayment:
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == SPEI_RECALLED:
            raise SpeiError('already_recalled', 'SPEI payment is already recalled.')
        if payment.status != SPEI_COMPLETED:
            raise SpeiError('not_recallable', 'Only completed SPEI payments can be recalled.')
        self._assert_devolucion_window(payment, actor_type=actor_type)
        remark = normalize_note(note) or (
            '%s devolucion %s' % (payment.rail, mask_tracking_key(payment.tracking_key) or payment.nickname)
        )
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(payment.internal_account, payment.amount, remark)
            except Exception as exc:
                raise SpeiError('recall_failed', 'Devolución credit failed.', payment=payment) from exc
            if _classify_money_result(result) != 'ok':
                raise SpeiError('recall_failed', 'Devolución credit failed.', payment=payment)
            fee = parse_money(payment.fee, allow_zero=True)
            if fee > 0 and payment.fee_status == FEE_COLLECTED:
                self.credit_fn(
                    payment.internal_account, money_str(fee),
                    '%s fee recalled %s' % (payment.rail, mask_tracking_key(payment.tracking_key)),
                )
        now = float(self.clock())
        payment.status = SPEI_RECALLED
        payment.recalled_at = now
        payment.updated_at = now
        payment.actor = str(actor)
        payment.note = remark
        self.store.update_payment(payment)
        return payment

    def export_cep1(self, *, payment_id: str, actor: str, actor_type: str) -> Dict[str, Any]:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status != SPEI_COMPLETED:
            raise SpeiError('not_exportable', 'Only completed SPEI can be exported.')
        if not payment.tracking_key:
            raise SpeiError('not_exportable', 'Clave de rastreo has not been assigned.')
        clabe, rfc, phone = self._bene_secrets(payment)
        record = compose_cep1(
            tracking_key=payment.tracking_key,
            send_bank=self.policy.source_bank,
            recv_bank=payment.bank_code or self.policy.source_bank,
            clabe=clabe,
            rfc=rfc,
            phone=phone,
            amount=parse_money(payment.amount),
            purpose=payment.purpose,
            status=payment.status,
            rail=payment.rail,
            mask=True,
        )
        payload = {
            'payment_id': payment.payment_id,
            'cep1': record,
            'tracking_key_masked': mask_tracking_key(payment.tracking_key),
        }
        if payment.rail == 'codi' and phone:
            payload['codi1'] = compose_codi1(
                phone=phone,
                amount=parse_money(payment.amount),
                tracking_key=payment.tracking_key,
                reference=payment.memo,
                mask=True,
            )
        return payload

    def run_due(self, userid: Optional[str] = None) -> List[SpeiPayment]:
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        changed: List[SpeiPayment] = []
        for payment in self.store.list_payments(userid):
            if payment.status != SPEI_QUEUED:
                continue
            pesos = parse_money(payment.amount)
            if self._needs_queue(pesos, rail=payment.rail, session=bool(clock['session'])):
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
            if row.status == SPEI_COMPLETED:
                sent_ytd += amount
                if row.fee_status == FEE_COLLECTED:
                    fee_ytd += parse_money(row.fee, allow_zero=True)
            elif row.status == SPEI_RECALLED:
                recalled += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'session_max': money_str(self.policy.session_max),
            'online_max': money_str(self.policy.online_max),
            'codi_max': money_str(self.policy.codi_max),
            'fee': money_str(self.policy.outbound_fee),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'currency': 'MXN',
            'clock': self.calendar.snapshot(now),
            'beneficiaries': [row.to_dict() for row in beneficiaries[:40]],
            'payments': [row.to_dict() for row in payments[:40]],
            'ytd_sent': money_str(sent_ytd),
            'ytd_fees': money_str(fee_ytd),
            'recalled_ytd': money_str(recalled),
            'active_count': sum(1 for row in beneficiaries if row.status == BENE_ACTIVE),
            'open_count': sum(1 for row in payments if row.status in OPEN_SPEI),
        }


_SERVICE: Optional[SpeiService] = None


def set_service(service: Optional[SpeiService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[SpeiService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('SPEI_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemorySpeiStore()
    path = os.environ.get('SPEI_DB', DEFAULT_STORE_PATH)
    return SqliteSpeiStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[SpeiClock] = None,
) -> SpeiService:
    if store is None:
        store = default_store()
    return SpeiService(
        SpeiPolicy.from_env(),
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
        'spei_forbidden': 403,
        'spei_disabled': 403,
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
        'devolucion_window_closed': 403,
        'beneficiary_not_found': 404,
        'payment_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_clabe': 400,
        'invalid_rfc': 400,
        'invalid_phone': 400,
        'invalid_bank': 400,
        'invalid_rail': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_currency': 400,
        'invalid_reference': 400,
        'invalid_file': 400,
        'spei_amount_exceeded': 400,
        'codi_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_beneficiary': 400,
        'missing_payment': 400,
    }.get(code, 400)


def _error_body(exc: SpeiError) -> Dict[str, Any]:
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
    except SpeiError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list_spei(service: SpeiService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Spei': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_add_beneficiary(service: SpeiService):
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
            rail=values.get('rail') or 'spei',
            clabe=values.get('clabe') or values.get('account_number'),
            rfc=values.get('rfc') or values.get('tax_id'),
            phone=values.get('phone') or values.get('codi_phone'),
            default_account=values.get('default_account') or values.get('account') or values.get('from_account'),
        )
        return jsonify({
            'message': 'SPEI beneficiary added',
            'beneficiary': row.to_dict(),
            'Spei': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _bene_status_route(service: SpeiService, status: str, ok_message: str):
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
            'Spei': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: SpeiService):
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
        return jsonify({'preview': preview, 'Spei': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: SpeiService):
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
            'message': 'SPEI originated' if created else 'SPEI already posted',
            'payment': payment.to_dict(),
            'Spei': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: SpeiService):
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
            'message': 'SPEI cancelled',
            'payment': payment.to_dict(),
            'Spei': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_spei_route(service: SpeiService, action: str):
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
            message = 'SPEI released'
        elif action == 'reject':
            payment = service.reject_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'SPEI rejected'
        elif action == 'recall':
            payment = service.recall_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'SPEI recalled'
        elif action == 'override':
            payment = service.override_ofac(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'waive':
            payment = service.waive_fee(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'SPEI fee waived'
        elif action == 'export':
            exported = service.export_cep1(payment_id=payment_id, actor=userid, actor_type=actor_type)
            payment = service.get_payment(payment_id=payment_id, actor=userid, actor_type=actor_type)
            return jsonify({
                'message': 'SPEI exported',
                'export': exported,
                'payment': payment.to_dict(),
                'Spei': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
            }), 200
        else:
            raise SpeiError('invalid_status', 'Unknown SPEI action.')
        return jsonify({
            'message': message,
            'payment': payment.to_dict(),
            'Spei': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: SpeiService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Spei': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_spei_routes(app, service: SpeiService) -> None:
    @app.route('/listSpei', methods=['POST', 'GET'])
    def list_spei_route():
        return handle_list_spei(service)

    @app.route('/listSpeiBeneficiaries', methods=['POST', 'GET'])
    def list_spei_beneficiaries_route():
        return handle_list_spei(service)

    @app.route('/addSpeiBeneficiary', methods=['POST', 'GET'])
    def add_spei_beneficiary_route():
        return handle_add_beneficiary(service)

    @app.route('/pauseSpeiBeneficiary', methods=['POST', 'GET'])
    def pause_spei_beneficiary_route():
        return _bene_status_route(service, BENE_PAUSED, 'SPEI beneficiary paused')

    @app.route('/resumeSpeiBeneficiary', methods=['POST', 'GET'])
    def resume_spei_beneficiary_route():
        return _bene_status_route(service, BENE_ACTIVE, 'SPEI beneficiary resumed')

    @app.route('/archiveSpeiBeneficiary', methods=['POST', 'GET'])
    def archive_spei_beneficiary_route():
        return _bene_status_route(service, BENE_ARCHIVED, 'SPEI beneficiary archived')

    @app.route('/previewSpei', methods=['POST', 'GET'])
    def preview_spei_route():
        return handle_preview(service)

    @app.route('/sendSpei', methods=['POST', 'GET'])
    def send_spei_route():
        return handle_send(service)

    @app.route('/cancelSpei', methods=['POST', 'GET'])
    def cancel_spei_route():
        return handle_cancel(service)

    @app.route('/releaseSpei', methods=['POST', 'GET'])
    def release_spei_route():
        return _staff_spei_route(service, 'release')

    @app.route('/rejectSpei', methods=['POST', 'GET'])
    def reject_spei_route():
        return _staff_spei_route(service, 'reject')

    @app.route('/recallSpei', methods=['POST', 'GET'])
    def recall_spei_route():
        return _staff_spei_route(service, 'recall')

    @app.route('/overrideSpeiOfac', methods=['POST', 'GET'])
    def override_spei_ofac_route():
        return _staff_spei_route(service, 'override')

    @app.route('/waiveSpeiFee', methods=['POST', 'GET'])
    def waive_spei_fee_route():
        return _staff_spei_route(service, 'waive')

    @app.route('/exportSpei', methods=['POST', 'GET'])
    def export_spei_route():
        return _staff_spei_route(service, 'export')

    @app.route('/runDueSpei', methods=['POST', 'GET'])
    def run_due_spei_route():
        return handle_run_due(service)
