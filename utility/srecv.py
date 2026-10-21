"""Inbound SPEI / CoDi receive posting (MXN CLABE / Banxico / CoDi claim).

Staff ingest incoming SPEI and CoDi credits. Recipients are enrolled
CLABE (SPEI) or Mexican phone (CoDi) keys at this bank's 3-digit Banxico
code. Matched SPEI credits post during the Banxico session (06:00–18:00
CT); CoDi and SPEI en línea (≤ MXN 8,000) post 24/7. Unmatched keys wait
for a CoDi/CLABE claim or staff assignment. Independent of outbound SPEI
origination (PR #141), inbound PIX (PR #139), CHIPS, Zelle, ACH linking
(PR #68), and domestic Fedwire (PR #73). Existing `/fundTransfer`,
`/withdrawAmount`, and `/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- CLABE (18-digit) with 3-7-1 repeating check digit + 3-digit Banxico bank
- RFC (12 moral / 13 física) SAT check digit
- Mexican +52 10-digit phone overlay for CoDi
- Clave de rastreo `KNH{BBB}{YYYYMMDD}{7}` uniqueness
- Banxico session clock (06:00-18:00 CT) vs SPEI en línea / CoDi 24/7
- CEP1 / CODI1 pipe compose/parse (XML/DOCTYPE rejected)
- CoDi/CLABE claim (customer presents matching key; enroll rematches)
- Devolución windows (CoDi 24h customer / SPEI next business day / 90-day staff)
- Dual-control at MXN 200k

Reuses Fedwire OFAC screening, money parsing, and account directory
from `utility.wire`. Stores are pluggable (memory for tests, sqlite WAL
for restart-safe default). Full CLABE / RFC / phone / fingerprint /
tracking key / destination account never appear in to_dict / snapshots.
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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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
DEFAULT_RECEIVER = '646'
DEFAULT_STORE_PATH = 'SystemLogs/srecv.sqlite'
CUSTOMER_CODI_RETURN_SECONDS = 24 * 60 * 60
STAFF_DEVOLUCION_DAYS = 90
CLAIM_SECONDS = 24 * 60 * 60
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)
RFC_TAB = {
    '0': 0, '1': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7, '8': 8, '9': 9,
    'A': 10, 'B': 11, 'C': 12, 'D': 13, 'E': 14, 'F': 15, 'G': 16, 'H': 17, 'I': 18,
    'J': 19, 'K': 20, 'L': 21, 'M': 22, 'N': 23, '&': 24, 'O': 25, 'P': 26, 'Q': 27,
    'R': 28, 'S': 29, 'T': 30, 'U': 31, 'V': 32, 'W': 33, 'X': 34, 'Y': 35, 'Z': 36,
    ' ': 37, 'Ñ': 38,
}
CLABE_WEIGHTS = (3, 7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1, 3, 7, 1, 3, 7)


class SrecvError(ValueError):
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
        raise SrecvError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise SrecvError('invalid_name', 'Sender legal name must be 2-80 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise SrecvError('invalid_purpose', 'Unknown SPEI purpose.')
    return text


def normalize_currency(value: Any, *, default: str = 'MXN') -> str:
    text = str(value or default).strip().upper()
    if text not in {'MXN', 'MXP'}:
        raise SrecvError('invalid_currency', 'SPEI / CoDi is MXN only.')
    return 'MXN'


def normalize_rail(value: Any, *, default: str = 'spei') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text not in RAILS:
        raise SrecvError('invalid_rail', 'Rail must be spei or codi.')
    return text


def normalize_bank(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        raise SrecvError('invalid_bank', 'Banxico bank code is required.')
    if len(digits) < 3:
        digits = digits.zfill(3)
    if len(digits) != 3:
        raise SrecvError('invalid_bank', 'Banxico bank code must be three digits.')
    if digits == '000':
        raise SrecvError('invalid_bank', 'Banxico bank code is invalid.')
    return digits


def mask_bank(value: Any) -> str:
    bank = normalize_bank(value)
    return bank[0] + '*' + bank[2]


def bank_name(value: Any, *, directory: Optional[Dict[str, str]] = None) -> str:
    try:
        bank = normalize_bank(value)
    except SrecvError:
        return ''
    mapping = directory if directory is not None else DEFAULT_BANKS
    return mapping.get(bank, '')


def clabe_check_digit(body: str) -> str:
    if len(body) != 17 or not body.isdigit():
        raise SrecvError('invalid_clabe', 'CLABE body must be 17 digits.')
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
        raise SrecvError('invalid_clabe', 'Plaza must be three digits.')
    account_digits = ''.join(ch for ch in str(account or '') if ch.isdigit()).zfill(11)
    if len(account_digits) != 11:
        raise SrecvError('invalid_clabe', 'CLABE account must be 11 digits.')
    body = bank_code + plaza_digits + account_digits
    return body + clabe_check_digit(body)


def normalize_clabe(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise SrecvError('invalid_clabe', 'CLABE is required.')
        return ''
    if len(digits) != 18 or not clabe_check_digit_ok(digits):
        raise SrecvError('invalid_clabe', 'CLABE failed checksum.')
    return digits


def mask_clabe(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '***'
    return '**************' + digits[-4:]


def rfc_check_digit(body: str) -> str:
    text = str(body or '').upper()
    if len(text) not in {11, 12}:
        raise SrecvError('invalid_rfc', 'RFC body must be 11 or 12 characters.')
    total = 0
    factor = len(text) + 1
    for index, char in enumerate(text):
        if char not in RFC_TAB:
            raise SrecvError('invalid_rfc', 'RFC contains an invalid character.')
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
    except SrecvError:
        return False


def compose_rfc(body: Any) -> str:
    text = re.sub(r'[^A-Z0-9&Ñ ]', '', str(body or '').upper())
    if len(text) not in {11, 12}:
        raise SrecvError('invalid_rfc', 'RFC body must be 11 or 12 characters.')
    return text + rfc_check_digit(text)


def normalize_rfc(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[\s\-.]', '', str(value or '').upper())
    if not text:
        if required:
            raise SrecvError('invalid_rfc', 'RFC is required.')
        return ''
    if len(text) not in {12, 13} or not rfc_check_digit_ok(text):
        raise SrecvError('invalid_rfc', 'RFC failed checksum.')
    date_part = text[3:9] if len(text) == 12 else text[4:10]
    try:
        year = int(date_part[0:2])
        month = int(date_part[2:4])
        day = int(date_part[4:6])
        date(2000 + year if year < 30 else 1900 + year, month, day)
    except ValueError as exc:
        raise SrecvError('invalid_rfc', 'RFC date is invalid.') from exc
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
            raise SrecvError('invalid_phone', 'Mexican phone is required for CoDi.')
        return ''
    if digits.startswith('52') and len(digits) == 12:
        national = digits[2:]
    elif len(digits) == 10:
        national = digits
    elif digits.startswith('52') and len(digits) == 13 and digits[2] == '1':
        national = digits[3:]
    else:
        raise SrecvError('invalid_phone', 'Mexican phone must be 10 national digits.')
    if len(national) != 10 or national[0] == '0':
        raise SrecvError('invalid_phone', 'Mexican phone must be 10 national digits.')
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
        raise SrecvError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 9999999:
        raise SrecvError('invalid_reference', 'Tracking sequence out of range.')
    return 'KNH%s%s%07d' % (normalize_bank(bank), day, seq)


def normalize_tracking_key(value: Any) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not (15 <= len(text) <= 30) or not text.startswith('KNH'):
        raise SrecvError('invalid_reference', 'Clave de rastreo is invalid.')
    return text


def mask_tracking_key(value: Any) -> str:
    text = str(value or '')
    if len(text) < 7:
        return 'KNH***'
    return 'KNH************' + text[-4:]


def compose_amount_field(amount: Decimal) -> str:
    quantized = amount.quantize(MONEY_QUANTUM)
    if quantized < 0 or quantized > Decimal('999999999.99'):
        raise SrecvError('invalid_amount', 'Amount cannot be encoded in CEP1.')
    return money_str(quantized)


def parse_amount_field(value: Any) -> Decimal:
    try:
        return parse_money(value)
    except AmountError as exc:
        raise SrecvError('invalid_amount', 'CEP1 amount field is invalid.') from exc


def _reject_markup(text: str) -> None:
    lowered = text.lower()
    if '<xml' in lowered or '<!doctype' in lowered or '<?xml' in lowered:
        raise SrecvError('invalid_file', 'XML/DOCTYPE CEP1 files are rejected.')


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
        str(status or IN_POSTED),
        kind,
        SETTLEMENT_CODI if kind == 'codi' else SETTLEMENT_SPEI,
    ))


def parse_cep1(text: Any) -> Dict[str, Any]:
    raw = str(text or '').strip()
    if not raw:
        raise SrecvError('invalid_file', 'CEP1 record is empty.')
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != CEP1_FIELDS:
        raise SrecvError('invalid_file', 'CEP1 record must have %d fields.' % CEP1_FIELDS)
    if parts[0] != 'CEP1' or parts[1] != CEP1_VERSION:
        raise SrecvError('invalid_file', 'CEP1 version marker is invalid.')
    if parts[12] not in {SETTLEMENT_SPEI, SETTLEMENT_CODI}:
        raise SrecvError('invalid_file', 'CEP1 settlement method must be spei or codi.')
    if '*' in parts[2] or '***' in parts[5] or '*' in parts[6]:
        raise SrecvError('invalid_file', 'Masked CEP1 record cannot be re-ingested.')
    rail = normalize_rail(parts[11])
    clabe = normalize_clabe(parts[5], required=rail == 'spei')
    rfc = normalize_rfc(parts[6], required=False)
    phone = normalize_phone(parts[7], required=rail == 'codi')
    return {
        'tracking_key': normalize_tracking_key(parts[2]),
        'send_bank': normalize_bank(parts[3]),
        'recv_bank': normalize_bank(parts[4]),
        'clabe': clabe,
        'rfc': rfc,
        'phone': phone,
        'amount': parse_amount_field(parts[8]),
        'purpose': normalize_purpose(parts[9]),
        'status': parts[10],
        'rail': rail,
        'settlement_method': parts[12],
        'name': '',
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
        raise SrecvError('invalid_file', 'CODI1 record is empty.')
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != CODI1_FIELDS:
        raise SrecvError('invalid_file', 'CODI1 record must have %d fields.' % CODI1_FIELDS)
    if parts[0] != 'CODI1' or parts[1] != CEP1_VERSION or parts[6] != 'MXN':
        raise SrecvError('invalid_file', 'CODI1 version marker is invalid.')
    if '*' in parts[2] or '*' in parts[4]:
        raise SrecvError('invalid_file', 'Masked CODI1 record cannot be re-ingested.')
    return {
        'phone': normalize_phone(parts[2], required=True),
        'amount': parse_amount_field(parts[3]),
        'tracking_key': normalize_tracking_key(parts[4]),
        'reference': parts[5],
        'currency': 'MXN',
        'rail': 'codi',
        'clabe': '',
        'rfc': '',
        'send_bank': '',
        'recv_bank': DEFAULT_RECEIVER,
        'purpose': 'other',
        'name': '',
    }


def split_cep1_file(text: Any) -> List[str]:
    raw = str(text or '')
    _reject_markup(raw)
    records = [line.strip() for line in raw.replace('\r\n', '\n').split('\n') if line.strip()]
    if not records:
        raise SrecvError('invalid_file', 'CEP1 file is empty.')
    return records


def parse_record(text: Any) -> Dict[str, Any]:
    raw = str(text or '').strip()
    if raw.startswith('CODI1|'):
        return parse_codi1(raw)
    return parse_cep1(raw)


def message_from_values(values: Dict[str, Any]) -> Dict[str, Any]:
    rail = normalize_rail(values.get('rail') or values.get('kind') or 'spei')
    recv = normalize_bank(values.get('recv_bank') or values.get('receiver') or DEFAULT_RECEIVER)
    send_raw = values.get('send_bank') or values.get('sender_bank') or values.get('bank')
    send = normalize_bank(send_raw) if send_raw else '012'
    amount = values.get('amount')
    if not isinstance(amount, Decimal):
        try:
            amount = parse_money(amount)
        except AmountError as exc:
            raise SrecvError('invalid_amount', 'Amount is invalid.') from exc
    tracking = str(values.get('tracking_key') or values.get('clave') or values.get('reference') or '').strip()
    if tracking:
        tracking = normalize_tracking_key(tracking)
    else:
        cycle = str(values.get('value_date') or values.get('cycle_date') or '').strip()
        if '-' in cycle:
            cycle = cycle.replace('-', '')[:8]
        seq = values.get('sequence') or values.get('seq') or 1
        tracking = compose_tracking_key(send, cycle, int(seq))
    clabe = normalize_clabe(
        values.get('clabe') or values.get('clabe_number'),
        required=rail == 'spei',
    )
    rfc = normalize_rfc(values.get('rfc'), required=False)
    phone = normalize_phone(
        values.get('phone') or values.get('mobile') or values.get('cel'),
        required=rail == 'codi',
    )
    name = str(values.get('name') or values.get('sender_name') or values.get('payer') or '').strip()
    if name:
        name = normalize_legal_name(name)
    return {
        'tracking_key': tracking,
        'send_bank': send,
        'recv_bank': recv,
        'clabe': clabe,
        'rfc': rfc,
        'phone': phone,
        'amount': amount,
        'purpose': normalize_purpose(values.get('purpose')),
        'rail': rail,
        'name': name or ('CODI PAYER' if rail == 'codi' else 'SPEI PAYER'),
        'currency': normalize_currency(values.get('currency')),
    }


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
class SrecvPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_claim: bool = True
    customer_return: bool = True
    allow_credit: bool = False
    max_keys: int = 12
    max_inbounds: int = 120
    min_amount: Decimal = Decimal('0.01')
    session_max: Decimal = Decimal('1000000.00')
    online_max: Decimal = Decimal('8000.00')
    codi_max: Decimal = Decimal('8000.00')
    dual_control_threshold: Decimal = Decimal('200000.00')
    tz_offset_hours: int = -6
    open_hour: int = 6
    cutoff_hour: int = 18
    receiver_bank: str = DEFAULT_RECEIVER
    customer_codi_return_seconds: int = CUSTOMER_CODI_RETURN_SECONDS
    staff_devolucion_days: int = STAFF_DEVOLUCION_DAYS
    claim_seconds: int = CLAIM_SECONDS
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()
    banks: Dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.banks is None:
            self.banks = dict(DEFAULT_BANKS)

    @classmethod
    def from_env(cls) -> 'SrecvPolicy':
        extra = _env_list('SRECV_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        extra_banks = _env_list('SRECV_BANKS')
        directory = dict(DEFAULT_BANKS)
        for item in extra_banks:
            if ':' in item:
                pid, name = item.split(':', 1)
                try:
                    directory[normalize_bank(pid)] = name.strip().upper()[:80]
                except SrecvError:
                    continue
        source = os.environ.get('SRECV_RECEIVER') or DEFAULT_RECEIVER
        try:
            receiver = normalize_bank(source)
        except SrecvError:
            receiver = DEFAULT_RECEIVER
        return cls(
            enabled=_env_bool('SRECV_ENABLED', True),
            customer_manage=_env_bool('SRECV_CUSTOMER_MANAGE', True),
            customer_claim=_env_bool('SRECV_CUSTOMER_CLAIM', True),
            customer_return=_env_bool('SRECV_CUSTOMER_RETURN', True),
            allow_credit=_env_bool('SRECV_ALLOW_CREDIT', False),
            max_keys=max(1, _env_int('SRECV_MAX_KEYS', 12)),
            max_inbounds=max(1, _env_int('SRECV_MAX_INBOUNDS', 120)),
            min_amount=_env_money('SRECV_MIN_AMOUNT', '0.01'),
            session_max=_env_money('SRECV_SESSION_MAX', '1000000.00'),
            online_max=_env_money('SRECV_ONLINE_MAX', '8000.00'),
            codi_max=_env_money('SRECV_CODI_MAX', '8000.00'),
            dual_control_threshold=_env_money('SRECV_DUAL_CONTROL', '200000.00'),
            tz_offset_hours=_env_int('SRECV_TZ_OFFSET', -6),
            open_hour=max(0, min(23, _env_int('SRECV_OPEN_HOUR', 6))),
            cutoff_hour=max(0, min(23, _env_int('SRECV_CUTOFF_HOUR', 18))),
            receiver_bank=receiver,
            customer_codi_return_seconds=max(60, _env_int(
                'SRECV_CODI_RETURN_SECONDS', CUSTOMER_CODI_RETURN_SECONDS,
            )),
            staff_devolucion_days=max(1, _env_int('SRECV_DEVOLUCION_DAYS', STAFF_DEVOLUCION_DAYS)),
            claim_seconds=max(60, _env_int('SRECV_CLAIM_SECONDS', CLAIM_SECONDS)),
            watchlist=watch,
            extra_holidays=_env_list('SRECV_HOLIDAYS'),
            banks=directory,
        )


@dataclass
class SrecvKey:
    key_id: str
    userid: str
    nickname: str
    rail: str
    clabe: str
    rfc: str
    phone: str
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
            'clabe_masked': mask_clabe(self.clabe) if self.clabe else '',
            'rfc_masked': mask_rfc(self.rfc) if self.rfc else '',
            'phone_masked': mask_phone(self.phone) if self.phone else '',
            'bank': self.clabe[:3] if self.clabe else '',
            'account_last4': last4(self.destination_account),
            'status': self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == KEY_ACTIVE,
            'paused': self.status == KEY_PAUSED,
            'cancelled': self.status == KEY_CANCELLED,
        }


@dataclass
class SrecvInbound:
    inbound_id: str
    tracking_key: str
    userid: str
    destination_account: str
    amount_mxn: str
    sender_name: str
    send_bank: str
    recv_bank: str
    rail: str
    clabe: str
    rfc: str
    phone: str
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
            'tracking_key_masked': mask_tracking_key(self.tracking_key),
            'userid': self.userid,
            'account_last4': last4(self.destination_account) if self.destination_account else '',
            'amount_mxn': self.amount_mxn,
            'sender_name': self.sender_name,
            'send_bank_masked': mask_bank(self.send_bank) if self.send_bank else '',
            'send_name': bank_name(self.send_bank) if self.send_bank else '',
            'recv_bank_masked': mask_bank(self.recv_bank) if self.recv_bank else '',
            'rail': self.rail,
            'clabe_masked': mask_clabe(self.clabe) if self.clabe else '',
            'rfc_masked': mask_rfc(self.rfc) if self.rfc else '',
            'phone_masked': mask_phone(self.phone) if self.phone else '',
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


def _key_from_row(row: Any) -> SrecvKey:
    data = dict(row)
    return SrecvKey(
        key_id=data['key_id'], userid=data['userid'], nickname=data['nickname'],
        rail=data['rail'], clabe=data.get('clabe') or '', rfc=data.get('rfc') or '',
        phone=data.get('phone') or '', fingerprint=data['fingerprint'],
        destination_account=data['destination_account'], status=data['status'], actor=data['actor'],
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
    )


def _inbound_from_row(row: Any) -> SrecvInbound:
    data = dict(row)
    return SrecvInbound(
        inbound_id=data['inbound_id'], tracking_key=data['tracking_key'], userid=data['userid'],
        destination_account=data['destination_account'], amount_mxn=data['amount_mxn'],
        sender_name=data['sender_name'], send_bank=data['send_bank'], recv_bank=data['recv_bank'],
        rail=data['rail'], clabe=data.get('clabe') or '', rfc=data.get('rfc') or '',
        phone=data.get('phone') or '', fingerprint=data['fingerprint'],
        purpose=data['purpose'], status=data['status'],
        reason=data['reason'], actor=data['actor'], releaser=data['releaser'],
        ofac_hit=bool(int(data['ofac_hit'])), ofac_match=data['ofac_match'],
        ofac_overridden=bool(int(data['ofac_overridden'])),
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
        posted_at=float(data.get('posted_at') or 0), returned_at=float(data.get('returned_at') or 0),
        expiry_at=float(data.get('expiry_at') or 0), note=data.get('note') or '',
    )


class MemorySrecvStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.keys: Dict[str, SrecvKey] = {}
        self.inbounds: Dict[str, SrecvInbound] = {}
        self._by_tracking: Dict[str, str] = {}

    def put_key(self, row: SrecvKey) -> None:
        with self._lock:
            self.keys[row.key_id] = row

    def get_key(self, key_id: str) -> Optional[SrecvKey]:
        return self.keys.get(key_id)

    def get_key_by_fingerprint(self, fingerprint: str) -> Optional[SrecvKey]:
        matches = [
            row for row in self.keys.values()
            if row.fingerprint == fingerprint and row.status in OPEN_KEY
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda row: row.created_at, reverse=True)[0]

    def update_key(self, row: SrecvKey) -> None:
        self.put_key(row)

    def list_keys(self, userid: Optional[str] = None) -> List[SrecvKey]:
        rows = list(self.keys.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        return sorted(rows, key=lambda row: row.created_at)

    def put_inbound(self, row: SrecvInbound) -> None:
        with self._lock:
            self.inbounds[row.inbound_id] = row
            self._by_tracking[row.tracking_key] = row.inbound_id

    def get_inbound(self, inbound_id: str) -> Optional[SrecvInbound]:
        return self.inbounds.get(inbound_id)

    def get_by_tracking_key(self, tracking_key: str) -> Optional[SrecvInbound]:
        inbound_id = self._by_tracking.get(tracking_key)
        return self.inbounds.get(inbound_id) if inbound_id else None

    def update_inbound(self, row: SrecvInbound) -> None:
        self.put_inbound(row)

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[SrecvInbound]:
        rows = list(self.inbounds.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)


class SqliteSrecvStore:
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
                    clabe TEXT NOT NULL DEFAULT '',
                    rfc TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
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
                    tracking_key TEXT NOT NULL UNIQUE,
                    userid TEXT NOT NULL DEFAULT '',
                    destination_account TEXT NOT NULL DEFAULT '',
                    amount_mxn TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    send_bank TEXT NOT NULL,
                    recv_bank TEXT NOT NULL,
                    rail TEXT NOT NULL,
                    clabe TEXT NOT NULL DEFAULT '',
                    rfc TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
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

    def put_key(self, row: SrecvKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO keys (
                    key_id, userid, nickname, rail, clabe, rfc, phone, fingerprint,
                    destination_account, status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.key_id, row.userid, row.nickname, row.rail, row.clabe, row.rfc,
                    row.phone, row.fingerprint, row.destination_account, row.status, row.actor,
                    row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_key(self, key_id: str) -> Optional[SrecvKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM keys WHERE key_id = ?', (key_id,)).fetchone()
        return _key_from_row(row) if row else None

    def get_key_by_fingerprint(self, fingerprint: str) -> Optional[SrecvKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM keys WHERE fingerprint = ? AND status IN ('active', 'paused')
                ORDER BY created_at DESC LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
        return _key_from_row(row) if row else None

    def update_key(self, row: SrecvKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE keys SET nickname=?, rail=?, clabe=?, rfc=?, phone=?, fingerprint=?,
                    destination_account=?, status=?, actor=?, updated_at=?
                WHERE key_id=?
                """,
                (
                    row.nickname, row.rail, row.clabe, row.rfc, row.phone, row.fingerprint,
                    row.destination_account, row.status, row.actor, row.updated_at, row.key_id,
                ),
            )
            conn.commit()

    def list_keys(self, userid: Optional[str] = None) -> List[SrecvKey]:
        sql = 'SELECT * FROM keys'
        args: Tuple[Any, ...] = ()
        if userid is not None:
            sql += ' WHERE userid = ?'
            args = (userid,)
        sql += ' ORDER BY created_at'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_key_from_row(row) for row in rows]

    def put_inbound(self, row: SrecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inbounds (
                    inbound_id, tracking_key, userid, destination_account, amount_mxn,
                    sender_name, send_bank, recv_bank, rail, clabe, rfc, phone, fingerprint,
                    purpose, status, reason, actor, releaser, ofac_hit, ofac_match,
                    ofac_overridden, created_at, updated_at, posted_at, returned_at,
                    expiry_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.inbound_id, row.tracking_key, row.userid, row.destination_account,
                    row.amount_mxn, row.sender_name, row.send_bank, row.recv_bank, row.rail,
                    row.clabe, row.rfc, row.phone, row.fingerprint, row.purpose, row.status,
                    row.reason, row.actor, row.releaser, int(row.ofac_hit), row.ofac_match,
                    int(row.ofac_overridden), row.created_at, row.updated_at, row.posted_at,
                    row.returned_at, row.expiry_at, row.note,
                ),
            )
            conn.commit()

    def get_inbound(self, inbound_id: str) -> Optional[SrecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,)).fetchone()
        return _inbound_from_row(row) if row else None

    def get_by_tracking_key(self, tracking_key: str) -> Optional[SrecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM inbounds WHERE tracking_key = ?', (tracking_key,),
            ).fetchone()
        return _inbound_from_row(row) if row else None

    def update_inbound(self, row: SrecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE inbounds SET userid=?, destination_account=?, amount_mxn=?, sender_name=?,
                    send_bank=?, recv_bank=?, rail=?, clabe=?, rfc=?, phone=?, fingerprint=?,
                    purpose=?, status=?, reason=?, actor=?, releaser=?, ofac_hit=?, ofac_match=?,
                    ofac_overridden=?, updated_at=?, posted_at=?, returned_at=?, expiry_at=?, note=?
                WHERE inbound_id=?
                """,
                (
                    row.userid, row.destination_account, row.amount_mxn, row.sender_name,
                    row.send_bank, row.recv_bank, row.rail, row.clabe, row.rfc, row.phone,
                    row.fingerprint, row.purpose, row.status, row.reason, row.actor, row.releaser,
                    int(row.ofac_hit), row.ofac_match, int(row.ofac_overridden), row.updated_at,
                    row.posted_at, row.returned_at, row.expiry_at, row.note, row.inbound_id,
                ),
            )
            conn.commit()

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[SrecvInbound]:
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


class SrecvService:
    def __init__(
        self,
        policy: SrecvPolicy,
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
        self.clock = clock or (lambda: datetime.now(tz=timezone.utc).timestamp())
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

    def _now(self) -> float:
        return float(self.clock())

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise SrecvError('srecv_disabled', 'SPEI receive is disabled.')

    def _require_staff(self, actor_type: str) -> None:
        if actor_type not in EMPLOYEE_ROLES:
            raise SrecvError('srecv_forbidden', 'Staff only.')

    def _require_customer_manage(self, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if not self.policy.customer_manage:
            raise SrecvError('srecv_forbidden', 'Customers cannot manage SPEI receive keys.')

    def _accounts(self, userid: str) -> Any:
        if not self.accounts_fn:
            return {}
        return self.accounts_fn(userid) or {}

    def _owned(self, userid: str, account: str) -> None:
        owned = own_accounts_from_customer_payload(self._accounts(userid))
        if account not in owned:
            raise SrecvError('invalid_account', 'Destination is not an account of this customer.')

    def _account_type(self, userid: str, account: str) -> str:
        return account_types_from_customer_payload(self._accounts(userid)).get(account, '')

    def _credit_allowed(self, userid: str, account: str) -> bool:
        kind = self._account_type(userid, account)
        if kind == 'credit' and not self.policy.allow_credit:
            return False
        return True

    def _fingerprint_for(self, rail: str, clabe: str, phone: str) -> str:
        if rail == 'codi':
            return destination_fingerprint('codi', '', phone)
        return destination_fingerprint('spei', clabe, '')

    def add_key(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: str,
        rail: Any,
        destination_account: Any,
        clabe: Any = '',
        rfc: Any = '',
        phone: Any = '',
    ) -> SrecvKey:
        self._require_enabled()
        self._require_customer_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise SrecvError('srecv_forbidden', 'Cannot enroll a SPEI key for another customer.')
        kind = normalize_rail(rail)
        clabe_value = normalize_clabe(clabe, required=kind == 'spei')
        rfc_value = normalize_rfc(rfc, required=kind == 'spei')
        phone_value = normalize_phone(phone, required=kind == 'codi')
        if kind == 'spei' and clabe_value[:3] != self.policy.receiver_bank:
            raise SrecvError('wrong_receiver', 'CLABE is not issued by this Banxico participant.')
        account = normalize_account(destination_account)
        self._owned(owner_userid, account)
        if not self._credit_allowed(owner_userid, account):
            raise SrecvError('credit_not_allowed', 'Credit accounts cannot receive SPEI.')
        fingerprint = self._fingerprint_for(kind, clabe_value, phone_value)
        existing = self.store.get_key_by_fingerprint(fingerprint)
        if existing and existing.status in OPEN_KEY:
            raise SrecvError('key_duplicate', 'That SPEI / CoDi key is already enrolled.')
        open_rows = [row for row in self.store.list_keys(owner_userid) if row.status in OPEN_KEY]
        if any(row.nickname.lower() == normalize_nickname(nickname).lower() for row in open_rows):
            raise SrecvError('key_duplicate', 'That nickname is already in use.')
        if len(open_rows) >= self.policy.max_keys:
            raise SrecvError('key_limit', 'SPEI receive key limit reached.')
        now = self._now()
        row = SrecvKey(
            key_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=normalize_nickname(nickname),
            rail=kind,
            clabe=clabe_value,
            rfc=rfc_value,
            phone=phone_value,
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
    ) -> SrecvKey:
        self._require_enabled()
        row = self.store.get_key(key_id)
        if row is None:
            raise SrecvError('key_not_found', 'SPEI receive key enrollment not found.')
        if actor_type not in EMPLOYEE_ROLES and actor != row.userid:
            raise SrecvError('srecv_forbidden', 'Cannot change another customer SPEI key.')
        self._require_customer_manage(actor_type)
        if status not in KEY_STATUSES:
            raise SrecvError('invalid_status', 'Unknown SPEI key status.')
        if row.status == KEY_CANCELLED:
            raise SrecvError('already_cancelled', 'Cancelled SPEI keys cannot change.')
        if status == KEY_PAUSED and row.status != KEY_ACTIVE:
            raise SrecvError('already_paused' if row.status == KEY_PAUSED else 'invalid_status', 'Key is not active.')
        if status == KEY_ACTIVE and row.status != KEY_PAUSED:
            raise SrecvError('already_active', 'Key is already active.')
        if status == row.status:
            code = 'already_paused' if status == KEY_PAUSED else 'already_active'
            raise SrecvError(code, 'SPEI key already in that status.')
        updated = replace(row, status=status, actor=actor, updated_at=self._now())
        self.store.update_key(updated)
        if status == KEY_ACTIVE:
            self._rematch_unmatched(updated)
        return updated

    def _rematch_unmatched(self, enrollment: SrecvKey) -> None:
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
    ) -> Tuple[SrecvInbound, bool]:
        self._require_enabled()
        self._require_staff(actor_type)
        parsed = message_from_values(values)
        return self._ingest_parsed(actor=actor, parsed=parsed)

    def ingest_file(
        self,
        *,
        actor: str,
        actor_type: str,
        payload: str,
        sender_name: str = '',
    ) -> Tuple[List[SrecvInbound], int]:
        self._require_enabled()
        self._require_staff(actor_type)
        created = 0
        rows = []
        fallback = ''
        if sender_name:
            fallback = normalize_legal_name(sender_name)
        for record in split_cep1_file(payload):
            parsed = parse_record(record)
            if not parsed.get('recv_bank'):
                parsed['recv_bank'] = self.policy.receiver_bank
            if fallback:
                parsed['name'] = fallback
            elif not parsed.get('name'):
                parsed['name'] = 'CODI PAYER' if parsed.get('rail') == 'codi' else 'SPEI PAYER'
            row, is_new = self._ingest_parsed(actor=actor, parsed=parsed)
            rows.append(row)
            if is_new:
                created += 1
        return rows, created

    def _ingest_parsed(self, *, actor: str, parsed: Dict[str, Any]) -> Tuple[SrecvInbound, bool]:
        recv = parsed.get('recv_bank') or self.policy.receiver_bank
        if recv != self.policy.receiver_bank:
            raise SrecvError('wrong_receiver', 'SPEI credit is not for this Banxico participant.')
        existing = self.store.get_by_tracking_key(parsed['tracking_key'])
        if existing is not None:
            return existing, False
        total = len(self.store.list_inbounds())
        if total >= self.policy.max_inbounds:
            raise SrecvError('inbound_limit', 'Inbound SPEI limit reached.')
        amount = parsed['amount'] if isinstance(parsed['amount'], Decimal) else parse_money(parsed['amount'])
        if amount < self.policy.min_amount:
            raise SrecvError('amount_out_of_range', 'Amount is outside the SPEI range.')
        rail = normalize_rail(parsed.get('rail') or 'spei')
        if rail == 'codi' and amount > self.policy.codi_max:
            raise SrecvError('codi_amount_exceeded', 'Amount is outside the CoDi range.')
        if rail == 'spei' and amount > self.policy.session_max:
            raise SrecvError('spei_amount_exceeded', 'Amount is outside the SPEI range.')
        now = self._now()
        clabe = parsed.get('clabe') or ''
        phone = parsed.get('phone') or ''
        fingerprint = self._fingerprint_for(rail, clabe, phone)
        row = SrecvInbound(
            inbound_id=uuid.uuid4().hex,
            tracking_key=parsed['tracking_key'],
            userid='',
            destination_account='',
            amount_mxn=money_str(amount),
            sender_name=parsed.get('name') or 'SPEI PAYER',
            send_bank=parsed.get('send_bank') or '',
            recv_bank=recv,
            rail=rail,
            clabe=clabe,
            rfc=parsed.get('rfc') or '',
            phone=phone,
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

    def _should_queue(self, row: SrecvInbound, amount: Decimal) -> bool:
        if row.rail == 'codi':
            return False
        if amount <= self.policy.online_max:
            return False
        return not self.calendar.is_session(self._now())

    def _evaluate(self, row: SrecvInbound, *, rescreen: bool) -> SrecvInbound:
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
        except SrecvError:
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
        mxn = parse_money(row.amount_mxn)
        if mxn >= self.policy.dual_control_threshold and not row.releaser:
            row.status = IN_PENDING
            row.reason = 'dual_control'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if self._should_queue(row, mxn):
            row.status = IN_QUEUED
            row.reason = 'session_closed'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        return self._post(row)

    def _post(self, row: SrecvInbound) -> SrecvInbound:
        if self.credit_fn is None:
            raise SrecvError('failed', 'Credit function is not configured.')
        remark = '%s from %s' % (row.rail, row.sender_name[:40])
        result = self.credit_fn(row.destination_account, row.amount_mxn, remark)
        classified = _classify_money_result(result)
        now = self._now()
        if classified == 'nsf':
            row.status = IN_FAILED
            row.reason = 'nsf'
            row.updated_at = now
            self.store.update_inbound(row)
            raise SrecvError('nsf', 'Credit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            row.status = IN_FAILED
            row.reason = 'failed'
            row.updated_at = now
            self.store.update_inbound(row)
            raise SrecvError('failed', 'Credit failed.', inbound=row)
        row.status = IN_POSTED
        row.reason = ''
        row.posted_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def preview(self, *, inbound_id: str = '', tracking_key: str = '', actor: str = '', actor_type: str = '') -> Dict[str, Any]:
        row = self._get(inbound_id=inbound_id, tracking_key=tracking_key)
        self._assert_visible(row, actor, actor_type)
        return {
            'inbound': row.to_dict(),
            'clock': self.calendar.snapshot(self._now()),
            'receiver': mask_bank(self.policy.receiver_bank),
        }

    def _get(self, *, inbound_id: str = '', tracking_key: str = '') -> SrecvInbound:
        row = None
        if inbound_id:
            row = self.store.get_inbound(str(inbound_id).strip())
        if row is None and tracking_key:
            row = self.store.get_by_tracking_key(normalize_tracking_key(tracking_key))
        if row is None:
            raise SrecvError('inbound_not_found', 'Inbound SPEI not found.')
        return row

    def _assert_visible(self, row: SrecvInbound, actor: str, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if row.userid and row.userid != actor:
            raise SrecvError('srecv_forbidden', 'Cannot view another customer SPEI.')

    def assign(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        customer_id: str,
        account: Any,
        nickname: str = '',
    ) -> SrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_UNMATCHED:
            raise SrecvError('not_assignable', 'Only unmatched SPEI credits can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise SrecvError('missing_customer_id', 'Customer id is required to assign.')
        dest = normalize_account(account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise SrecvError('credit_not_allowed', 'Credit accounts cannot receive SPEI.')
        existing = self.store.get_key_by_fingerprint(row.fingerprint)
        if existing is None or existing.status == KEY_CANCELLED:
            self.add_key(
                owner_userid=owner, actor=actor, actor_type=actor_type,
                nickname=nickname or ('SPEI %s' % row.rail),
                rail=row.rail, clabe=row.clabe, rfc=row.rfc, phone=row.phone,
                destination_account=dest,
            )
            fresh = self.store.get_inbound(row.inbound_id)
            if fresh is not None and fresh.status != IN_UNMATCHED:
                return fresh
        elif existing.userid != owner:
            raise SrecvError('key_duplicate', 'SPEI key belongs to another customer.')
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
        clabe: Any = '',
        rfc: Any = '',
        phone: Any = '',
        destination_account: Any = '',
        nickname: str = '',
        owner_userid: str = '',
    ) -> SrecvInbound:
        """CoDi / CLABE claim: customer presents the matching key for an unmatched credit."""
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_claim:
                raise SrecvError('srecv_forbidden', 'Customers cannot claim SPEI credits.')
            owner = actor
        else:
            owner = str(owner_userid or '').strip()
            if not owner:
                raise SrecvError('missing_customer_id', 'Customer id is required to claim.')
        kind = normalize_rail(rail or ('codi' if phone and not clabe else 'spei'))
        clabe_value = normalize_clabe(clabe, required=kind == 'spei')
        rfc_value = normalize_rfc(rfc, required=False)
        phone_value = normalize_phone(phone, required=kind == 'codi')
        if kind == 'spei' and not clabe_value:
            raise SrecvError('missing_key', 'CLABE is required to claim SPEI.')
        if kind == 'codi' and not phone_value:
            raise SrecvError('missing_key', 'Phone is required to claim CoDi.')
        fingerprint = self._fingerprint_for(kind, clabe_value, phone_value)
        dest = normalize_account(destination_account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise SrecvError('credit_not_allowed', 'Credit accounts cannot receive SPEI.')
        row = None
        if inbound_id:
            row = self._get(inbound_id=inbound_id)
            if row.fingerprint != fingerprint:
                raise SrecvError('not_claimable', 'Presented SPEI key does not match this credit.')
        else:
            for candidate in self.store.list_inbounds(status=IN_UNMATCHED):
                if candidate.fingerprint == fingerprint:
                    row = candidate
                    break
        if row is None:
            raise SrecvError('inbound_not_found', 'No unmatched SPEI for that key.')
        if row.status != IN_UNMATCHED:
            raise SrecvError('not_claimable', 'This SPEI credit is not waiting for a claim.')
        if row.expiry_at and self._now() > row.expiry_at:
            row.status = IN_EXPIRED
            row.reason = 'expired'
            row.updated_at = self._now()
            self.store.update_inbound(row)
            raise SrecvError('return_window_closed', 'SPEI claim window has closed.')
        existing = self.store.get_key_by_fingerprint(fingerprint)
        if existing is None or existing.status == KEY_CANCELLED:
            self.add_key(
                owner_userid=owner, actor=actor, actor_type=actor_type,
                nickname=nickname or ('SPEI %s' % kind),
                rail=kind, clabe=clabe_value or row.clabe, rfc=rfc_value or row.rfc,
                phone=phone_value or row.phone, destination_account=dest,
            )
        elif existing.userid != owner:
            raise SrecvError('key_duplicate', 'SPEI key belongs to another customer.')
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

    def override_ofac(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> SrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_HELD:
            raise SrecvError('not_overridable', 'Only OFAC-held SPEI credits can be overridden.')
        row.ofac_overridden = True
        row.ofac_hit = False
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def release(self, *, inbound_id: str, actor: str, actor_type: str) -> SrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_PENDING:
            raise SrecvError('not_releasable', 'Only dual-control SPEI credits can be released.')
        if actor == row.actor:
            raise SrecvError('same_approver', 'A different employee must release this credit.')
        row.releaser = actor
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def reject(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> SrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status not in OPEN_IN:
            raise SrecvError('not_rejectable', 'This SPEI credit cannot be rejected.')
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
        if not row.tracking_key:
            raise SrecvError('not_exportable', 'SPEI credit has no clave de rastreo to export.')
        _ = actor
        record = compose_cep1(
            tracking_key=row.tracking_key,
            send_bank=row.send_bank or '012',
            recv_bank=row.recv_bank,
            clabe=row.clabe,
            rfc=row.rfc,
            phone=row.phone,
            amount=parse_money(row.amount_mxn, allow_zero=True),
            purpose=row.purpose,
            status=row.status,
            rail=row.rail,
            mask=True,
        )
        extra = ''
        if row.rail == 'codi' and row.phone:
            extra = compose_codi1(
                phone=row.phone,
                amount=parse_money(row.amount_mxn, allow_zero=True),
                tracking_key=row.tracking_key,
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
    ) -> SrecvInbound:
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
    ) -> SrecvInbound:
        self._require_enabled()
        row = self._get(inbound_id=inbound_id)
        self._assert_visible(row, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_return:
                raise SrecvError('srecv_forbidden', 'Customers cannot return SPEI credits.')
            if actor != row.userid:
                raise SrecvError('srecv_forbidden', 'Cannot return another customer SPEI.')
        return self._return(row, actor=actor, actor_type=actor_type, reason=reason, honor_window=True)

    def _assert_devolucion_window(self, row: SrecvInbound, *, actor_type: str, honor_window: bool) -> None:
        now = self._now()
        completed = float(row.posted_at or row.created_at)
        if actor_type in EMPLOYEE_ROLES:
            if now - completed > self.policy.staff_devolucion_days * 86400:
                raise SrecvError('devolucion_window_closed', 'Staff SPEI devolución window has closed.')
            return
        if not honor_window:
            return
        if row.rail == 'codi':
            if now - completed > self.policy.customer_codi_return_seconds:
                raise SrecvError('return_window_closed', 'Customer CoDi return window has closed.')
            return
        completed_day = self.calendar.local_dt(completed).date()
        deadline_day = self.calendar.next_business_day(completed_day)
        deadline = self.calendar.session_close_ts(deadline_day)
        if now > deadline:
            raise SrecvError('return_window_closed', 'Customer SPEI devolución window has closed.')

    def _return(
        self,
        row: SrecvInbound,
        *,
        actor: str,
        actor_type: str,
        reason: str,
        honor_window: bool,
    ) -> SrecvInbound:
        now = self._now()
        _ = actor
        if row.status == IN_RETURNED or row.returned_at:
            raise SrecvError('already_returned', 'SPEI credit already returned.')
        if row.status in OPEN_IN:
            row.status = IN_RETURNED
            row.reason = reason or 'cust'
            row.returned_at = now
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if row.status != IN_POSTED:
            raise SrecvError('not_returnable', 'This SPEI credit cannot be returned.')
        self._assert_devolucion_window(row, actor_type=actor_type, honor_window=honor_window)
        if self.debit_fn is None:
            raise SrecvError('return_failed', 'Debit function is not configured.')
        remark = '%s return %s' % (row.rail, row.tracking_key[-12:])
        result = self.debit_fn(row.destination_account, row.amount_mxn, remark)
        classified = _classify_money_result(result)
        if classified == 'nsf':
            raise SrecvError('nsf', 'Return debit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            raise SrecvError('return_failed', 'Return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.reason = reason or 'cust'
        row.returned_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[SrecvInbound]:
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
        ytd = sum((parse_money(row.amount_mxn, allow_zero=True) for row in received), Decimal('0.00'))
        return {
            'enabled': self.policy.enabled,
            'clock': self.calendar.snapshot(now),
            'receiver': mask_bank(self.policy.receiver_bank),
            'keys': [row.to_dict() for row in keys[:40]],
            'inbounds': [row.to_dict() for row in inbounds[:40]],
            'ytd_received': money_str(ytd),
            'open_count': sum(1 for row in inbounds if row.status in OPEN_IN),
            'active_keys': sum(1 for row in keys if row.status == KEY_ACTIVE),
            'min_amount': money_str(self.policy.min_amount),
            'session_max': money_str(self.policy.session_max),
            'online_max': money_str(self.policy.online_max),
            'codi_max': money_str(self.policy.codi_max),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'settlement_method': SETTLEMENT_SPEI,
            'currency': 'MXN',
        }

    def unmatched_snapshot(self) -> Dict[str, Any]:
        rows = self.store.list_inbounds(status=IN_UNMATCHED)
        return {
            'enabled': self.policy.enabled,
            'unmatched': [row.to_dict() for row in rows[:40]],
            'count': len(rows),
        }


_SERVICE: Optional[SrecvService] = None


def set_service(service: Optional[SrecvService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[SrecvService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('SRECV_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemorySrecvStore()
    path = os.environ.get('SRECV_DB', DEFAULT_STORE_PATH)
    return SqliteSrecvStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[SpeiClock] = None,
) -> SrecvService:
    if store is None:
        store = default_store()
    return SrecvService(
        SrecvPolicy.from_env(),
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
        'srecv_forbidden': 403,
        'srecv_disabled': 403,
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
        'devolucion_window_closed': 403,
        'key_not_found': 404,
        'inbound_not_found': 404,
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
        'invalid_reference': 400,
        'invalid_file': 400,
        'invalid_currency': 400,
        'wrong_receiver': 400,
        'spei_amount_exceeded': 400,
        'codi_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_key': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: SrecvError) -> Dict[str, Any]:
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
    except SrecvError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snapshot_for(service: SrecvService, userid: str, actor: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(userid, actor=actor, actor_type=actor_type)


def handle_list(service: SrecvService):
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
    return jsonify({'SpeiIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def handle_unmatched(service: SrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'srecv_forbidden'}), 403
    snap = service.unmatched_snapshot()
    return jsonify({'UnmatchedSpeiIns': snap, 'SpeiIn': snap}), 200


def handle_add_key(service: SrecvService):
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
                raise SrecvError('missing_customer_id', 'Customer id is required.')
        row = service.add_key(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname') or '',
            rail=values.get('rail') or values.get('kind') or 'spei',
            clabe=values.get('clabe') or values.get('clabe_number'),
            rfc=values.get('rfc'),
            phone=values.get('phone') or values.get('mobile'),
            destination_account=values.get('destination_account') or values.get('account') or values.get('default_account'),
        )
        return jsonify({
            'message': 'SPEI receive key enrolled',
            'key': row.to_dict(),
            'SpeiIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _key_status_route(service: SrecvService, status: str, message: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        key_id = str(values.get('key_id') or '').strip()
        if not key_id:
            raise SrecvError('missing_key', 'key_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.set_key_status(key_id=key_id, actor=userid, actor_type=actor_type, status=status)
        return jsonify({
            'message': message,
            'key': row.to_dict(),
            'SpeiIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: SrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        preview = service.preview(
            inbound_id=str(values.get('inbound_id') or ''),
            tracking_key=str(values.get('tracking_key') or values.get('clave') or ''),
            actor=userid,
            actor_type=actor_type,
        )
        owner = preview['inbound'].get('userid') or userid
        return jsonify({'preview': preview, 'SpeiIn': _snapshot_for(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest(service: SrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'SPEI ingested' if created else 'SPEI already ingested',
            'inbound': row.to_dict(),
            'SpeiIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_ingest_file(service: SrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        payload = values.get('file') or values.get('body') or values.get('payload')
        if not payload:
            raise SrecvError('missing_file', 'CEP1 file body is required.')
        actor_type = session.get('usertype') or 'customer'
        rows, created = service.ingest_file(
            actor=userid, actor_type=actor_type, payload=str(payload),
            sender_name=str(values.get('name') or values.get('sender_name') or ''),
        )
        owner = rows[-1].userid if rows and rows[-1].userid else userid
        return jsonify({
            'message': 'CEP1 file ingested',
            'created': created,
            'inbounds': [row.to_dict() for row in rows],
            'SpeiIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_assign(service: SrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise SrecvError('missing_inbound', 'inbound_id is required.')
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
            'message': 'SPEI assigned',
            'inbound': row.to_dict(),
            'SpeiIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_claim(service: SrecvService):
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
            clabe=values.get('clabe') or values.get('clabe_number'),
            rfc=values.get('rfc'),
            phone=values.get('phone') or values.get('mobile'),
            destination_account=values.get('destination_account') or values.get('account'),
            nickname=str(values.get('nickname') or ''),
            owner_userid=str(values.get('customer_id') or ''),
        )
        return jsonify({
            'message': 'SPEI claimed',
            'inbound': row.to_dict(),
            'SpeiIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_inbound_route(service: SrecvService, action: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise SrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        if action == 'override':
            row = service.override_ofac(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'release':
            row = service.release(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            message = 'SPEI released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'SPEI rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=str(values.get('reason') or 'cust'),
            )
            message = 'SPEI returned'
        elif action == 'export':
            exported = service.export(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            owner = exported['inbound'].get('userid') or userid
            return jsonify({
                'message': 'SPEI exported',
                'file': exported['file'],
                'inbound': exported['inbound'],
                'SpeiIn': _snapshot_for(service, owner, userid, actor_type),
            }), 200
        else:
            raise SrecvError('invalid_status', 'Unknown SPEI action.')
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'SpeiIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: SrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise SrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.request_return(
            inbound_id=inbound_id, actor=userid, actor_type=actor_type,
            reason=str(values.get('reason') or 'cust'),
        )
        return jsonify({
            'message': 'SPEI return requested',
            'inbound': row.to_dict(),
            'SpeiIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: SrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'SpeiIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def attach_srecv_routes(app, service: SrecvService) -> None:
    @app.route('/listSpeiIns', methods=['POST', 'GET'])
    def list_spei_ins_route():
        return handle_list(service)

    @app.route('/listSpeiRecvKeys', methods=['POST', 'GET'])
    def list_spei_recv_keys_route():
        return handle_list(service)

    @app.route('/listUnmatchedSpeiIns', methods=['POST', 'GET'])
    def list_unmatched_spei_ins_route():
        return handle_unmatched(service)

    @app.route('/addSpeiRecvKey', methods=['POST', 'GET'])
    def add_spei_recv_key_route():
        return handle_add_key(service)

    @app.route('/pauseSpeiRecvKey', methods=['POST', 'GET'])
    def pause_spei_recv_key_route():
        return _key_status_route(service, KEY_PAUSED, 'SPEI receive key paused')

    @app.route('/resumeSpeiRecvKey', methods=['POST', 'GET'])
    def resume_spei_recv_key_route():
        return _key_status_route(service, KEY_ACTIVE, 'SPEI receive key resumed')

    @app.route('/cancelSpeiRecvKey', methods=['POST', 'GET'])
    def cancel_spei_recv_key_route():
        return _key_status_route(service, KEY_CANCELLED, 'SPEI receive key cancelled')

    @app.route('/previewSpeiIn', methods=['POST', 'GET'])
    def preview_spei_in_route():
        return handle_preview(service)

    @app.route('/ingestSpeiIn', methods=['POST', 'GET'])
    def ingest_spei_in_route():
        return handle_ingest(service)

    @app.route('/ingestSpeiInFile', methods=['POST', 'GET'])
    def ingest_spei_in_file_route():
        return handle_ingest_file(service)

    @app.route('/claimSpeiIn', methods=['POST', 'GET'])
    def claim_spei_in_route():
        return handle_claim(service)

    @app.route('/assignSpeiIn', methods=['POST', 'GET'])
    def assign_spei_in_route():
        return handle_assign(service)

    @app.route('/overrideSpeiInOfac', methods=['POST', 'GET'])
    def override_spei_in_ofac_route():
        return _staff_inbound_route(service, 'override')

    @app.route('/releaseSpeiIn', methods=['POST', 'GET'])
    def release_spei_in_route():
        return _staff_inbound_route(service, 'release')

    @app.route('/rejectSpeiIn', methods=['POST', 'GET'])
    def reject_spei_in_route():
        return _staff_inbound_route(service, 'reject')

    @app.route('/returnSpeiIn', methods=['POST', 'GET'])
    def return_spei_in_route():
        return _staff_inbound_route(service, 'return')

    @app.route('/exportSpeiIn', methods=['POST', 'GET'])
    def export_spei_in_route():
        return _staff_inbound_route(service, 'export')

    @app.route('/requestSpeiInReturn', methods=['POST', 'GET'])
    def request_spei_in_return_route():
        return handle_request_return(service)

    @app.route('/runDueSpeiIns', methods=['POST', 'GET'])
    def run_due_spei_ins_route():
        return handle_run_due(service)
