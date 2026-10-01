"""Inbound SWIFT MT103 / gpi receive posting from an operator file.

Staff ingest incoming SWIFT FIN MT103 credits (correspondent MT or gpi)
and credit the beneficiary customer in USD. gpi posts 24/7; MT follows
the TARGET2 calendar and 16:00 ET cutoff. Independent of outbound SWIFT
MT103 (PR #78), inbound SEPA SCT (PR #101), inbound BACS (PR #99),
inbound FPS/CHAPS (PR #96), inbound FedNow/RTP (PR #93), inbound Fedwire
(PR #90), UK Pay origination (PR #87), outbound Fedwire (PR #73), ACH
linking (PR #68), and bill-pay ACH (PR #66). Existing `/fundTransfer`,
`/withdrawAmount`, and `/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- IBAN ISO 13616 mod-97 (worldwide length table)
- BIC / SWIFT ISO 9362 (8→11 pad XXX; location cannot start with 0)
- ISO 4217 + injectable FX book (USD ledger credit equivalent, JPY 0-decimal)
- TARGET2 business-day / MT cutoff clock (gpi is 24/7)
- SWIFT gpi UETR (UUID v4) uniqueness
- MT103 FIN parse / compose / multi-message file split
- Receiver-BIC acceptance (this bank)
- Creditor IBAN BBAN trailing digits or /account → customer directory
- Incoming credit posting + MT103 RETN exception return
- Charge-bearer recording (OUR / SHA / BEN)
- OFAC-style originator screening (reused from utility.wire)
- Dual-control release for high-value inbound credits (USD equivalent)

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Full IBAN / originator account never appear in to_dict / snapshots.
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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    EMPLOYEE_ROLES,
    AccountError,
    AmountError,
    ScreenResult,
    WireError,
    account_types_from_customer_payload,
    last4,
    money_str,
    normalize_account,
    normalize_id,
    normalize_legal_name,
    normalize_note,
    normalize_party,
    normalize_purpose,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

IN_HELD = 'held'
IN_UNMATCHED = 'unmatched'
IN_PENDING = 'pending_release'
IN_QUEUED = 'queued'
IN_POSTED = 'posted'
IN_RETURNED = 'returned'
IN_REJECTED = 'rejected'
IN_FAILED = 'failed'
IN_STATUSES = frozenset({
    IN_HELD, IN_UNMATCHED, IN_PENDING, IN_QUEUED, IN_POSTED,
    IN_RETURNED, IN_REJECTED, IN_FAILED,
})
OPEN_INBOUNDS = frozenset({IN_HELD, IN_UNMATCHED, IN_PENDING, IN_QUEUED})
RETURNABLE_BEFORE_POST = frozenset({IN_HELD, IN_UNMATCHED, IN_PENDING, IN_QUEUED})

RAIL_GPI = 'gpi'
RAIL_MT = 'mt'
RAILS = frozenset({RAIL_GPI, RAIL_MT})
RAIL_ALIASES = {
    'gpi': RAIL_GPI, 'swiftgpi': RAIL_GPI, 'instant': RAIL_GPI, 'track': RAIL_GPI,
    'mt': RAIL_MT, 'mt103': RAIL_MT, '103': RAIL_MT, 'correspondent': RAIL_MT,
    'swift': RAIL_MT, 'fin': RAIL_MT, 'wire': RAIL_MT,
}
CHARGES = frozenset({'OUR', 'SHA', 'BEN'})
CHARGE_ALIASES = {
    'our': 'OUR', 'sender': 'OUR', 'ours': 'OUR',
    'sha': 'SHA', 'shared': 'SHA', 'share': 'SHA',
    'ben': 'BEN', 'beneficiary': 'BEN', 'theirs': 'BEN',
}
IBAN_LENGTHS = {
    'AD': 24, 'AE': 23, 'AL': 28, 'AT': 20, 'AZ': 28, 'BA': 20, 'BE': 16, 'BG': 22,
    'BH': 22, 'BR': 29, 'BY': 28, 'CH': 21, 'CR': 22, 'CY': 28, 'CZ': 24, 'DE': 22,
    'DK': 18, 'DO': 28, 'EE': 20, 'EG': 29, 'ES': 24, 'FI': 18, 'FO': 18, 'FR': 27,
    'GB': 22, 'GE': 22, 'GI': 23, 'GL': 18, 'GR': 27, 'GT': 28, 'HR': 21, 'HU': 28,
    'IE': 22, 'IL': 23, 'IQ': 23, 'IS': 26, 'IT': 27, 'JO': 30, 'KW': 30, 'KZ': 20,
    'LB': 28, 'LC': 32, 'LI': 21, 'LT': 20, 'LU': 20, 'LV': 21, 'MC': 27, 'MD': 24,
    'ME': 22, 'MK': 19, 'MR': 27, 'MT': 31, 'MU': 30, 'NL': 18, 'NO': 15, 'PK': 24,
    'PL': 28, 'PS': 29, 'PT': 25, 'QA': 29, 'RO': 24, 'RS': 22, 'SA': 24, 'SE': 24,
    'SI': 19, 'SK': 24, 'SM': 27, 'TN': 24, 'TR': 26, 'UA': 29, 'VG': 24, 'XK': 20,
}
NON_IBAN_COUNTRIES = frozenset({
    'US', 'CA', 'AU', 'NZ', 'JP', 'CN', 'KR', 'SG', 'HK', 'IN', 'MX', 'ZA', 'TW',
    'TH', 'PH', 'MY', 'ID', 'VN', 'NG', 'AR', 'CL', 'CO', 'PE',
})
ISO_COUNTRIES = frozenset(set(IBAN_LENGTHS) | NON_IBAN_COUNTRIES | {
    'AF', 'AG', 'AI', 'AM', 'AO', 'AQ', 'AS', 'AW', 'AX', 'BB', 'BD', 'BF', 'BI',
    'BJ', 'BL', 'BM', 'BN', 'BO', 'BQ', 'BS', 'BT', 'BW', 'BZ', 'CC', 'CD', 'CF',
    'CG', 'CI', 'CK', 'CM', 'CU', 'CV', 'CW', 'CX', 'DJ', 'DM', 'DZ', 'EC', 'EH',
    'ER', 'ET', 'FJ', 'FK', 'FM', 'GA', 'GD', 'GF', 'GG', 'GH', 'GM', 'GN', 'GP',
    'GQ', 'GS', 'GU', 'GW', 'GY', 'HM', 'HN', 'HT', 'IM', 'IO', 'IR', 'JE', 'JM',
    'KE', 'KG', 'KH', 'KI', 'KM', 'KN', 'KP', 'KY', 'LA', 'LK', 'LR', 'LS', 'LY',
    'MA', 'MF', 'MG', 'MH', 'ML', 'MM', 'MN', 'MO', 'MP', 'MQ', 'MS', 'MV', 'MW',
    'MZ', 'NA', 'NC', 'NE', 'NF', 'NI', 'NP', 'NR', 'NU', 'OM', 'PA', 'PF', 'PG',
    'PM', 'PN', 'PR', 'PW', 'PY', 'RE', 'RU', 'RW', 'SB', 'SC', 'SD', 'SH', 'SJ',
    'SL', 'SN', 'SO', 'SR', 'SS', 'ST', 'SV', 'SX', 'SY', 'SZ', 'TC', 'TD', 'TF',
    'TG', 'TJ', 'TK', 'TL', 'TM', 'TO', 'TT', 'TV', 'TZ', 'UG', 'UM', 'UY', 'UZ',
    'VA', 'VC', 'VE', 'VI', 'VU', 'WF', 'WS', 'YE', 'YT', 'ZM', 'ZW',
})
CURRENCY_MINOR = {
    'JPY': 0, 'KRW': 0, 'VND': 0, 'CLP': 0,
    'BHD': 3, 'JOD': 3, 'KWD': 3, 'OMR': 3, 'TND': 3,
}
SUPPORTED_CURRENCIES = frozenset({
    'USD', 'EUR', 'GBP', 'CAD', 'CHF', 'AUD', 'JPY', 'MXN', 'INR', 'CNY',
    'HKD', 'SGD', 'NZD', 'SEK', 'NOK', 'DKK', 'ZAR', 'BRL', 'KRW', 'AED',
    'PLN', 'TRY', 'THB', 'PHP', 'CZK', 'HUF', 'ILS', 'SAR', 'QAR', 'KWD',
})
DEFAULT_RATES = {
    'USD': Decimal('1.000000'),
    'EUR': Decimal('1.080000'),
    'GBP': Decimal('1.270000'),
    'CAD': Decimal('0.740000'),
    'CHF': Decimal('1.120000'),
    'AUD': Decimal('0.660000'),
    'JPY': Decimal('0.006700'),
    'MXN': Decimal('0.058000'),
    'INR': Decimal('0.012000'),
    'CNY': Decimal('0.140000'),
    'HKD': Decimal('0.128000'),
    'SGD': Decimal('0.740000'),
    'NZD': Decimal('0.610000'),
    'SEK': Decimal('0.095000'),
    'NOK': Decimal('0.094000'),
    'DKK': Decimal('0.145000'),
    'ZAR': Decimal('0.055000'),
    'BRL': Decimal('0.200000'),
    'KRW': Decimal('0.000750'),
    'AED': Decimal('0.272000'),
    'PLN': Decimal('0.250000'),
    'TRY': Decimal('0.031000'),
    'THB': Decimal('0.029000'),
    'PHP': Decimal('0.018000'),
    'CZK': Decimal('0.043000'),
    'HUF': Decimal('0.002800'),
    'ILS': Decimal('0.270000'),
    'SAR': Decimal('0.266000'),
    'QAR': Decimal('0.274000'),
    'KWD': Decimal('3.250000'),
}
RETURN_REASONS = frozenset({
    'AC01', 'AC03', 'AC04', 'AC06', 'AM04', 'BE01', 'CUST', 'DUPL', 'FOCR', 'MS03', 'RR04',
})
RETURN_ALIASES = {
    'acct': 'AC03', 'account': 'AC03', 'unknown': 'AC03', 'no_account': 'AC03',
    'closed': 'AC04', 'blocked': 'AC06', 'incorrect': 'AC01',
    'nsf': 'AM04', 'nsfr': 'AM04', 'insufficient': 'AM04',
    'name': 'BE01', 'mismatch': 'BE01', 'beneficiary': 'BE01',
    'cust': 'CUST', 'customer': 'CUST', 'requested': 'CUST',
    'dup': 'DUPL', 'duplicate': 'DUPL',
    'ofac': 'RR04', 'sanction': 'RR04', 'sanctions': 'RR04', 'regulatory': 'RR04',
    'cancel': 'FOCR', 'recall': 'FOCR',
    'other': 'MS03', 'unspecified': 'MS03',
}

DEFAULT_STORE_PATH = 'SystemLogs/inswift.sqlite'
DEFAULT_RECEIVER_BIC = 'KNHAUS33XXX'
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)
MONEY_QUANTUM = Decimal('0.01')
RATE_QUANTUM = Decimal('0.000001')
CUSTOMER_RETURN_SECONDS = 24 * 60 * 60
BIC_RE = re.compile(r'^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$')
UETR_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
)
TRN_RE = re.compile(r'^[A-Z0-9/.-]{4,16}$')
FIELD_RE = re.compile(r':(\d{2,3}[A-Z]?):(.*?)(?=\n:\d{2,3}[A-Z]?:|\n-}|\Z)', re.S)
BLOCK1_RE = re.compile(r'\{1:F01([A-Z0-9]{12})')
BLOCK2_RE = re.compile(r'\{2:[IO]103(?:\d{4})?([A-Z0-9]{12})')
BLOCK3_UETR_RE = re.compile(r'\{121:([0-9a-fA-F-]{36})\}')
_SPLIT = re.compile(r'(?=\{1:)')


class InSwiftError(ValueError):
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


def _env_rates() -> Dict[str, Decimal]:
    rates = {}
    for key, value in os.environ.items():
        if not key.startswith('INSWIFT_FX_'):
            continue
        ccy = key[len('INSWIFT_FX_'):].upper()
        if ccy:
            rates[ccy] = parse_rate(value)
    return rates


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


def normalize_rail(value: Any, *, default: str = RAIL_GPI) -> str:
    text = re.sub(r'[^a-z0-9]', '', str(value or default).strip().lower())
    mapped = RAIL_ALIASES.get(text, text)
    if mapped not in RAILS:
        raise InSwiftError('invalid_rail', 'Rail must be gpi or mt.')
    return mapped


def normalize_source(value: Any, *, default: str = 'KONOHA01') -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or default).upper())
    if not text:
        text = default
    return (text + 'XXXXXXXX')[:8]


def normalize_charge(value: Any, *, default: str = 'SHA') -> str:
    text = str(value or default).strip().upper()
    text = CHARGE_ALIASES.get(text.lower(), text)
    if text not in CHARGES:
        raise InSwiftError('invalid_charge', 'Charge bearer must be OUR, SHA, or BEN.')
    return text


def normalize_currency(value: Any, *, default: str = 'EUR') -> str:
    text = str(value or default).strip().upper()
    if text not in SUPPORTED_CURRENCIES:
        raise InSwiftError('invalid_currency', 'Unsupported ISO 4217 currency.')
    return text


def currency_quantum(currency: str) -> Decimal:
    minor = CURRENCY_MINOR.get(currency, 2)
    if minor == 0:
        return Decimal('1')
    return Decimal('1').scaleb(-minor)


def quantize_ccy(amount: Decimal, currency: str) -> Decimal:
    return amount.quantize(currency_quantum(currency), rounding=ROUND_HALF_EVEN)


def money_str_ccy(amount: Decimal, currency: str) -> str:
    quantized = quantize_ccy(amount, currency)
    if CURRENCY_MINOR.get(currency, 2) == 0:
        return str(int(quantized))
    return format(quantized, 'f')


def parse_rate(value: Any) -> Decimal:
    if value is None or (isinstance(value, str) and not str(value).strip()):
        raise InSwiftError('invalid_rate', 'FX rate is required.')
    try:
        rate = Decimal(str(value).strip().replace(',', ''))
    except (InvalidOperation, ValueError):
        raise InSwiftError('invalid_rate', 'FX rate is invalid.') from None
    if not rate.is_finite() or rate <= 0:
        raise InSwiftError('invalid_rate', 'FX rate must be positive.')
    return rate.quantize(RATE_QUANTUM, rounding=ROUND_HALF_EVEN)


def iban_mod97(digits: str) -> int:
    """Chunked mod-97 so we never build a 10^60 int from a 34-char IBAN."""
    remainder = 0
    for index in range(0, len(digits), 9):
        remainder = int(str(remainder) + digits[index:index + 9]) % 97
    return remainder


def iban_check_digit_ok(iban: str) -> bool:
    """ISO 13616: rearrange, A=10..Z=35, remaining value mod 97 equals 1."""
    compact = re.sub(r'[^A-Z0-9]', '', str(iban or '').upper())
    if len(compact) < 15 or len(compact) > 34 or not compact[:2].isalpha() or not compact[2:4].isdigit():
        return False
    rearranged = compact[4:] + compact[:4]
    numeric = []
    for ch in rearranged:
        if ch.isdigit():
            numeric.append(ch)
        elif ch.isalpha():
            numeric.append(str(ord(ch) - 55))
        else:
            return False
    return iban_mod97(''.join(numeric)) == 1


def compose_iban(country: str, bban: str) -> str:
    """Build an IBAN with valid ISO 13616 check digits."""
    cc = re.sub(r'[^A-Z]', '', str(country or '').upper())
    body = re.sub(r'[^A-Z0-9]', '', str(bban or '').upper())
    expected_total = IBAN_LENGTHS.get(cc)
    if expected_total is None:
        raise InSwiftError('invalid_iban', 'IBAN country code is unknown.')
    expected = expected_total - 4
    if len(body) != expected:
        raise InSwiftError('invalid_iban', 'BBAN length does not match country %s.' % cc)
    rearranged = body + cc + '00'
    numeric = []
    for ch in rearranged:
        numeric.append(ch if ch.isdigit() else str(ord(ch) - 55))
    check = 98 - iban_mod97(''.join(numeric))
    iban = '%s%02d%s' % (cc, check, body)
    if not iban_check_digit_ok(iban):
        raise InSwiftError('invalid_iban', 'Failed to compose a valid IBAN.')
    return iban


def normalize_iban(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[^A-Za-z0-9]', '', str(value or '')).upper()
    if not text:
        if required:
            raise InSwiftError('invalid_iban', 'IBAN is required.')
        return ''
    country = text[:2]
    expected = IBAN_LENGTHS.get(country)
    if expected is not None and len(text) != expected:
        raise InSwiftError('invalid_iban', 'IBAN length does not match country %s.' % country)
    if expected is None and not (15 <= len(text) <= 34):
        raise InSwiftError('invalid_iban', 'IBAN must be 15-34 characters.')
    if country not in ISO_COUNTRIES:
        raise InSwiftError('invalid_iban', 'IBAN country code is unknown.')
    if not iban_check_digit_ok(text):
        raise InSwiftError('invalid_iban', 'IBAN failed mod-97 checksum.')
    return text


def mask_iban(iban: str) -> str:
    compact = re.sub(r'[^A-Z0-9]', '', str(iban or '').upper())
    if not compact:
        return ''
    if len(compact) <= 6:
        return compact[:2] + '****'
    return compact[:2] + '****' + compact[-4:]


def extract_iban_account(iban: str) -> str:
    """Map IBAN BBAN trailing digits onto an internal account id."""
    compact = normalize_iban(iban)
    digits = ''.join(ch for ch in compact[4:] if ch.isdigit())
    if not digits:
        raise InSwiftError('invalid_account', 'IBAN does not contain an account number.')
    for width in (10, 8, 12, 7, 6, 4):
        if len(digits) >= width:
            try:
                return normalize_account(digits[-width:])
            except AccountError:
                continue
    try:
        return normalize_account(digits[-4:] if len(digits) >= 4 else digits)
    except AccountError as exc:
        raise InSwiftError('invalid_account', 'IBAN account is not a valid internal account.') from exc


def normalize_bic(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[^A-Za-z0-9]', '', str(value or '')).upper()
    if not text:
        if required:
            raise InSwiftError('invalid_bic', 'BIC is required.')
        return ''
    if len(text) == 8:
        text = text + 'XXX'
    if not BIC_RE.match(text):
        raise InSwiftError('invalid_bic', 'BIC must be 8 or 11 characters (ISO 9362).')
    country = text[4:6]
    if country not in ISO_COUNTRIES:
        raise InSwiftError('invalid_bic', 'BIC country code is unknown.')
    if text[6] == '0':
        raise InSwiftError('invalid_bic', 'BIC location code cannot start with 0.')
    return text


def format_bic(value: str) -> str:
    try:
        return normalize_bic(value, required=False) or str(value or '')
    except InSwiftError:
        return str(value or '')


def bic8(value: str) -> str:
    text = format_bic(value)
    return text[:8]


def lt_address(bic: str) -> str:
    text = normalize_bic(bic)
    return text[:8] + 'X' + text[8:11]


def bic_from_lt(value: str) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '')).upper()
    if len(text) >= 12:
        return normalize_bic(text[:8] + text[9:12])
    if len(text) >= 11:
        return normalize_bic(text[:11])
    return normalize_bic(text[:8])


def compose_uetr(value: Any = None) -> str:
    """SWIFT gpi UETR is a lowercase UUID v4."""
    if value:
        text = str(value).strip().lower()
        if not UETR_RE.match(text):
            raise InSwiftError('invalid_uetr', 'UETR must be a UUID v4.')
        return text
    return str(uuid.uuid4())


def normalize_uetr(value: Any) -> str:
    return compose_uetr(value)


def normalize_trn(value: Any) -> str:
    text = re.sub(r'[^A-Za-z0-9/.-]', '', str(value or '').strip()).upper()
    if not TRN_RE.match(text):
        raise InSwiftError('invalid_trn', 'Field 20 TRN must be 4-16 characters.')
    return text


def normalize_return_reason(value: Any, *, default: str = 'MS03') -> str:
    text = str(value or default).strip().upper().replace('-', '_').replace(' ', '_')
    mapped = RETURN_ALIASES.get(text.lower(), text)
    if mapped not in RETURN_REASONS:
        raise InSwiftError('invalid_reason', 'Unknown inbound return reason.')
    return mapped


def format_swift_amount(amount: Decimal, currency: str) -> str:
    """SWIFT 32A uses a comma as the decimal separator."""
    quantized = quantize_ccy(amount, currency)
    if CURRENCY_MINOR.get(currency, 2) == 0:
        return '%s%s,' % (currency, int(quantized))
    text = format(quantized, 'f').replace('.', ',')
    return '%s%s' % (currency, text)


def parse_field_32a(text: Any) -> Tuple[str, str, Decimal]:
    raw = re.sub(r'\s+', '', str(text or ''))
    if len(raw) < 10:
        raise InSwiftError('invalid_amount', 'Field 32A is required.')
    yymmdd, ccy, amount_raw = raw[:6], raw[6:9], raw[9:]
    if not yymmdd.isdigit():
        raise InSwiftError('invalid_date', 'Field 32A value date is invalid.')
    year = int(yymmdd[:2])
    year += 2000 if year < 80 else 1900
    try:
        value = date(year, int(yymmdd[2:4]), int(yymmdd[4:6]))
    except ValueError as exc:
        raise InSwiftError('invalid_date', 'Field 32A value date is invalid.') from exc
    currency = normalize_currency(ccy)
    amount_text = amount_raw.replace(',', '.')
    if amount_text.endswith('.'):
        amount_text = amount_text[:-1]
    amount = quantize_ccy(parse_money(amount_text), currency)
    return value.isoformat(), currency, amount


def compose_msg_id(prefix: str, sequence: int) -> str:
    return ('%s%06d' % (re.sub(r'[^A-Z0-9]', '', prefix.upper())[:8] or 'MSG', sequence))[:16]


def easter_gregorian(year: int) -> date:
    """Anonymous Gregorian algorithm. TARGET2 Good Friday / Easter Monday depend on this."""
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
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def target2_holidays(year: int) -> set:
    """TARGET2 closing days. Weekend-falling holidays are NOT observed to a weekday."""
    easter = easter_gregorian(year)
    return {
        date(year, 1, 1),
        easter - timedelta(days=2),
        easter + timedelta(days=1),
        date(year, 5, 1),
        date(year, 12, 25),
        date(year, 12, 26),
    }


@dataclass
class FxQuote:
    currency: str
    amount: str
    amount_usd: str
    rate: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'currency': self.currency,
            'amount': self.amount,
            'amount_usd': self.amount_usd,
            'rate': self.rate,
        }


class FxBook:
    """Injectable USD-per-unit FX book. Inbound amounts arrive in foreign CCY; ledger credits USD."""

    def __init__(self, rates: Optional[Dict[str, Decimal]] = None) -> None:
        merged = dict(DEFAULT_RATES)
        if rates:
            for key, value in rates.items():
                merged[str(key).upper()] = parse_rate(value)
        self.rates = merged

    def rate(self, currency: str) -> Decimal:
        ccy = normalize_currency(currency)
        if ccy not in self.rates:
            raise InSwiftError('invalid_currency', 'No FX rate for %s.' % ccy)
        return self.rates[ccy]

    def quote(self, amount: Any, currency: str) -> FxQuote:
        ccy = normalize_currency(currency)
        foreign = quantize_ccy(parse_money(amount), ccy)
        if foreign <= 0:
            raise AmountError('invalid_amount')
        rate = self.rate(ccy)
        usd = (foreign * rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
        if usd <= 0:
            raise InSwiftError('invalid_amount', 'USD equivalent rounds to zero.')
        return FxQuote(
            currency=ccy,
            amount=money_str_ccy(foreign, ccy),
            amount_usd=money_str(usd),
            rate=format(rate, 'f'),
        )


class Target2Calendar:
    """MT correspondent clock. gpi ignores weekends and cutoff."""

    def __init__(
        self,
        *,
        cutoff_hour: int = 16,
        tz_offset_hours: int = -4,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.cutoff_hour = int(cutoff_hour)
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
        return day in target2_holidays(day.year) or day in self.extra_holidays

    def is_business_day(self, day: date) -> bool:
        return not self.is_weekend(day) and not self.is_holiday(day)

    def is_after_cutoff(self, ts: float) -> bool:
        local = self.local_dt(ts)
        return (local.hour, local.minute) >= (self.cutoff_hour, 0)

    def next_business_day(self, day: date) -> date:
        cursor = day + timedelta(days=1)
        while not self.is_business_day(cursor):
            cursor += timedelta(days=1)
        return cursor

    def input_date(self, ts: float) -> date:
        local = self.local_dt(ts)
        day = local.date()
        if self.is_business_day(day) and not self.is_after_cutoff(ts):
            return day
        return self.next_business_day(day)

    def value_date(self, ts: float, *, rail: str = RAIL_MT) -> date:
        if rail == RAIL_GPI:
            return self.local_dt(ts).date()
        return self.input_date(ts)

    def cycle_date(self, ts: float, *, rail: str = RAIL_MT) -> str:
        return self.value_date(ts, rail=rail).strftime('%Y%m%d')

    def should_queue(self, ts: float, *, rail: str) -> bool:
        if rail == RAIL_GPI:
            return False
        local = self.local_dt(ts)
        return self.is_after_cutoff(ts) or not self.is_business_day(local.date())

    def snapshot(self, ts: float, *, rail: str = RAIL_MT) -> Dict[str, Any]:
        local = self.local_dt(ts)
        value = self.value_date(ts, rail=rail)
        after = self.should_queue(ts, rail=rail)
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'cutoff': '%02d:00' % self.cutoff_hour,
            'after_cutoff': after,
            'business_day': self.is_business_day(local.date()),
            'value_date': value.isoformat(),
            'cycle_date': value.strftime('%Y%m%d'),
            'rail': rail,
            'gpi': rail == RAIL_GPI,
            'rail_hours': '24x7' if rail == RAIL_GPI else 'TARGET2 07:00-16:00',
            'calendar': '24x7' if rail == RAIL_GPI else 'TARGET2',
        }


def split_mt_file(text: Any) -> List[str]:
    raw = str(text or '')
    parts = [part.strip() for part in _SPLIT.split(raw) if part.strip()]
    messages = []
    for part in parts:
        if '{1:' in part or ':32A:' in part or ':20:' in part:
            messages.append(part)
    if not messages and raw.strip():
        if ':32A:' in raw or ':20:' in raw:
            messages.append(raw.strip())
    return messages


def parse_mt103(text: Any) -> Dict[str, str]:
    raw = str(text or '')
    if re.search(r'<!DOCTYPE|<!ENTITY', raw, re.I):
        raise InSwiftError('invalid_mt', 'XML entities are not allowed in a SWIFT FIN file.')
    if ':20:' not in raw and ':32A:' not in raw:
        raise InSwiftError('invalid_mt', 'Document is not an MT103 credit transfer.')
    fields = {}
    for match in FIELD_RE.finditer(raw.replace('\r\n', '\n')):
        fields[match.group(1)] = match.group(2).strip()
    if '32A' not in fields:
        raise InSwiftError('invalid_mt', 'MT103 is missing field 32A.')
    return fields


def _party_account_and_name(blob: str) -> Tuple[str, str]:
    lines = [line.strip() for line in str(blob or '').splitlines() if line.strip()]
    account = ''
    name = ''
    if lines:
        first = lines[0]
        if first.startswith('/'):
            account = first[1:].strip()
            name = ' '.join(lines[1:]).strip()
        else:
            name = ' '.join(lines).strip()
    return account, name


def _beneficiary_from_account_field(raw: str) -> Tuple[str, str]:
    """Return (internal_account, normalized_iban)."""
    text = str(raw or '').strip()
    if not text:
        raise InSwiftError('invalid_account', 'Beneficiary IBAN or account is required.')
    compact = re.sub(r'[^A-Z0-9]', '', text.upper())
    if len(compact) >= 15 and compact[:2].isalpha() and compact[2:4].isdigit():
        iban = normalize_iban(compact)
        return extract_iban_account(iban), iban
    digits = ''.join(ch for ch in text if ch.isdigit())
    try:
        return normalize_account(digits), ''
    except AccountError as exc:
        raise InSwiftError('invalid_account', 'Beneficiary account is invalid.') from exc


def compose_mt103(fields: Dict[str, Any]) -> str:
    """Compose a SWIFT FIN MT103. Reusable for any inbound or outbound credit."""
    rail = normalize_rail(fields.get('rail') or fields.get('scheme') or RAIL_GPI)
    uetr = compose_uetr(fields.get('uetr')) if fields.get('uetr') else compose_uetr()
    trn = normalize_trn(fields.get('trn') or fields.get('reference') or ('TRN' + uetr.replace('-', '')[:12]))
    currency = normalize_currency(fields.get('currency') or fields.get('ccy') or 'EUR')
    amount = quantize_ccy(parse_money(fields.get('amount')), currency)
    sender = normalize_bic(fields.get('sender_bic') or fields.get('sender') or 'COBADEFFXXX')
    receiver = normalize_bic(fields.get('receiver_bic') or fields.get('receiver') or DEFAULT_RECEIVER_BIC)
    dest = fields.get('iban') or fields.get('beneficiary_iban') or fields.get('beneficiary_account')
    originator_acct = fields.get('originator_account') or fields.get('originator_iban') or ''
    charge = normalize_charge(fields.get('charge') or 'SHA')
    value = str(fields.get('value_date') or '20240614').replace('-', '')
    if len(value) == 8:
        value = value[2:]
    if len(value) != 6 or not value.isdigit():
        raise InSwiftError('invalid_date', 'Value date must be YYMMDD or YYYY-MM-DD.')
    block4 = '\n'.join([
        ':20:%s' % trn,
        ':23B:CRED',
        ':32A:%s%s' % (value, format_swift_amount(amount, currency)),
        ':50K:/%s' % originator_acct,
        '%s' % (fields.get('originator_name') or 'ORIGINATOR'),
        ':52A:%s' % bic8(sender),
        ':57A:%s' % bic8(receiver),
        ':59:/%s' % dest,
        '%s' % (fields.get('beneficiary_name') or 'BENEFICIARY'),
        ':70:%s' % ((fields.get('memo') or 'OTHR')[:140]),
        ':71A:%s' % charge,
        ':121:%s' % uetr,
        '-',
    ])
    header = '{1:F01%s0000000000}{2:I103%sN}{3:{111:%s}{121:%s}}{4:\n%s}' % (
        lt_address(sender),
        lt_address(receiver),
        '001' if rail == RAIL_GPI else '000',
        uetr,
        block4,
    )
    return header


def compose_mt103_return(
    row: 'InboundSwift',
    *,
    return_trn: str,
    reason: str,
    receiver_bic: str,
) -> str:
    """MT103 RETN of an inbound SWIFT credit."""
    value = str(row.value_date or '').replace('-', '')
    yymmdd = value[2:] if len(value) == 8 else (value or '240101')
    currency = normalize_currency(row.currency)
    amount = parse_money(row.amount)
    return (
        '{1:F01%s0000000000}{2:I103%sN}{4:\n'
        ':20:%s\n'
        ':21:%s\n'
        ':32A:%s%s\n'
        ':72:/RETN/%s\n'
        '-}'
    ) % (
        lt_address(receiver_bic),
        lt_address(row.sender_bic),
        return_trn[:16],
        (row.trn or row.uetr[:16])[:16],
        yymmdd[:6],
        format_swift_amount(amount, currency),
        reason,
    )


def message_from_mt(text: Any) -> Dict[str, Any]:
    """Reusable MT103 FIN → inbound SWIFT field map."""
    raw = str(text or '')
    fields = parse_mt103(raw)
    value_date, currency, amount = parse_field_32a(fields['32A'])
    sender_match = BLOCK1_RE.search(raw)
    receiver_match = BLOCK2_RE.search(raw)
    sender_raw = fields.get('52A') or (bic_from_lt(sender_match.group(1)) if sender_match else '')
    receiver_raw = fields.get('57A') or (bic_from_lt(receiver_match.group(1)) if receiver_match else '')
    sender_bic = normalize_bic(sender_raw or 'COBADEFFXXX')
    receiver_bic = normalize_bic(receiver_raw or DEFAULT_RECEIVER_BIC)
    block3 = BLOCK3_UETR_RE.search(raw)
    uetr_raw = fields.get('121') or (block3.group(1) if block3 else '')
    if not uetr_raw:
        raise InSwiftError('invalid_uetr', 'UETR (field 121) is required.')
    uetr = normalize_uetr(uetr_raw)
    trn = normalize_trn(fields.get('20') or ('TRN' + uetr.replace('-', '')[:12]))
    originator_account, originator_name = _party_account_and_name(fields.get('50K') or fields.get('50A') or '')
    bene_account_raw, beneficiary_name = _party_account_and_name(fields.get('59') or fields.get('59A') or '')
    account, iban = _beneficiary_from_account_field(bene_account_raw)
    if originator_account:
        compact = re.sub(r'[^A-Z0-9]', '', originator_account.upper())
        if compact[:2].isalpha() and len(compact) >= 15:
            originator_account = normalize_iban(compact)
    service = re.search(r'\{111:(\d{3})\}', raw)
    rail = RAIL_GPI if (service and service.group(1) == '001') or ':121:' in raw else RAIL_MT
    if '{111:000}' in raw:
        rail = RAIL_MT
    charge = normalize_charge(fields.get('71A') or 'SHA')
    return {
        'uetr': uetr,
        'trn': trn,
        'rail': rail,
        'amount': money_str_ccy(amount, currency),
        'currency': currency,
        'value_date': value_date,
        'sender_bic': sender_bic,
        'receiver_bic': receiver_bic,
        'beneficiary_account': account,
        'originator_account': originator_account,
        'beneficiary_name': beneficiary_name or 'BENEFICIARY',
        'originator_name': originator_name or 'ORIGINATOR',
        'iban': iban,
        'charge': charge,
        'memo': normalize_note(fields.get('70') or '', limit=140),
        'raw': raw,
    }


def message_from_values(values: Dict[str, Any]) -> Dict[str, Any]:
    """JSON operator payload or FIN file → inbound field map."""
    blob = values.get('file') or values.get('mt') or values.get('raw') or values.get('text')
    if blob:
        parsed = message_from_mt(blob)
        override = values.get('rail') or values.get('scheme')
        if override:
            parsed['rail'] = normalize_rail(override)
        return parsed
    rail = normalize_rail(values.get('rail') or values.get('scheme') or RAIL_GPI)
    currency = normalize_currency(values.get('currency') or values.get('ccy') or 'EUR')
    amount = quantize_ccy(parse_money(values.get('amount')), currency)
    sender_bic = normalize_bic(values.get('sender_bic') or values.get('sender') or 'COBADEFFXXX')
    receiver_bic = normalize_bic(
        values.get('receiver_bic') or values.get('receiver') or DEFAULT_RECEIVER_BIC
    )
    iban_raw = values.get('iban') or values.get('beneficiary_iban') or ''
    account_raw = values.get('beneficiary_account') or values.get('account') or iban_raw
    account, iban = _beneficiary_from_account_field(account_raw)
    if iban_raw and not iban:
        iban = normalize_iban(iban_raw)
        account = extract_iban_account(iban)
    uetr_raw = values.get('uetr')
    if not uetr_raw:
        raise InSwiftError('invalid_uetr', 'UETR is required.')
    uetr = normalize_uetr(uetr_raw)
    trn_raw = values.get('trn') or values.get('reference') or ('TRN' + uetr.replace('-', '')[:12])
    originator = str(values.get('originator_name') or values.get('originator') or '').strip()
    beneficiary = str(values.get('beneficiary_name') or values.get('beneficiary') or '').strip()
    originator_account = str(values.get('originator_account') or values.get('originator_iban') or '').strip()
    if originator_account:
        compact = re.sub(r'[^A-Z0-9]', '', originator_account.upper())
        if compact[:2].isalpha() and len(compact) >= 15:
            originator_account = normalize_iban(compact)
    return {
        'uetr': uetr,
        'trn': normalize_trn(trn_raw),
        'rail': rail,
        'amount': money_str_ccy(amount, currency),
        'currency': currency,
        'value_date': '',
        'sender_bic': sender_bic,
        'receiver_bic': receiver_bic,
        'beneficiary_account': account,
        'originator_account': originator_account,
        'beneficiary_name': beneficiary or 'BENEFICIARY',
        'originator_name': originator or 'ORIGINATOR',
        'iban': iban,
        'charge': normalize_charge(values.get('charge') or 'SHA'),
        'memo': normalize_note(values.get('memo') or values.get('remittance') or '', limit=140),
        'raw': '',
    }


@dataclass
class InSwiftPolicy:
    enabled: bool = True
    customer_view: bool = True
    customer_return: bool = True
    allow_credit: bool = False
    max_inbounds: int = 240
    min_amount: Decimal = Decimal('0.01')
    max_amount_usd: Decimal = Decimal('50000000.00')
    gpi_max_usd: Decimal = Decimal('10000000.00')
    mt_max_usd: Decimal = Decimal('50000000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    cutoff_hour: int = 16
    tz_offset_hours: int = -4
    source_id: str = 'KONOHA01'
    receiver_bic: str = DEFAULT_RECEIVER_BIC
    customer_return_seconds: int = CUSTOMER_RETURN_SECONDS
    extra_holidays: Tuple[str, ...] = ()
    watchlist: Tuple[str, ...] = (
        'BLOCKED PERSON',
        'SANCTIONED ENTITY',
        'OFAC TESTNAME',
    )

    def rail_max(self, rail: str) -> Decimal:
        return self.gpi_max_usd if rail == RAIL_GPI else self.mt_max_usd

    @classmethod
    def from_env(cls) -> 'InSwiftPolicy':
        extra = _env_list('INSWIFT_OFAC_LIST')
        watch = tuple(dict.fromkeys(cls.watchlist + extra))
        receiver = os.environ.get('INSWIFT_RECEIVER_BIC') or DEFAULT_RECEIVER_BIC
        try:
            receiver = normalize_bic(receiver)
        except InSwiftError:
            receiver = DEFAULT_RECEIVER_BIC
        return cls(
            enabled=_env_bool('INSWIFT_ENABLED', True),
            customer_view=_env_bool('INSWIFT_CUSTOMER_VIEW', True),
            customer_return=_env_bool('INSWIFT_CUSTOMER_RETURN', True),
            allow_credit=_env_bool('INSWIFT_ALLOW_CREDIT', False),
            max_inbounds=max(1, _env_int('INSWIFT_MAX', 240)),
            min_amount=_env_money('INSWIFT_MIN_AMOUNT', '0.01'),
            max_amount_usd=_env_money('INSWIFT_MAX_AMOUNT', '50000000.00'),
            gpi_max_usd=_env_money('INSWIFT_GPI_MAX', '10000000.00'),
            mt_max_usd=_env_money('INSWIFT_MT_MAX', '50000000.00'),
            dual_control_threshold=_env_money('INSWIFT_DUAL_CONTROL', '10000.00'),
            cutoff_hour=max(0, min(23, _env_int('INSWIFT_CUTOFF_HOUR', 16))),
            tz_offset_hours=_env_int('INSWIFT_TZ_OFFSET', -4),
            source_id=normalize_source(os.environ.get('INSWIFT_SOURCE', 'KONOHA01')),
            receiver_bic=receiver,
            customer_return_seconds=max(60, _env_int('INSWIFT_RETURN_WINDOW', CUSTOMER_RETURN_SECONDS)),
            extra_holidays=_env_list('INSWIFT_HOLIDAYS'),
            watchlist=watch,
        )


@dataclass
class InboundSwift:
    inbound_id: str
    uetr: str
    trn: str
    rail: str
    userid: str
    internal_account: str
    currency: str
    amount: str
    amount_usd: str
    fx_rate: str
    sender_bic: str
    receiver_bic: str
    originator_name: str
    originator_account_last4: str
    beneficiary_name: str
    beneficiary_account: str
    iban_masked: str
    charge: str
    purpose: str
    memo: str
    status: str
    value_date: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    return_trn: str
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
            'uetr': self.uetr,
            'trn': self.trn,
            'rail': self.rail,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'currency': self.currency,
            'amount': self.amount,
            'amount_usd': self.amount_usd,
            'fx_rate': self.fx_rate,
            'sender_bic': format_bic(self.sender_bic),
            'receiver_bic': format_bic(self.receiver_bic),
            'originator_name': self.originator_name,
            'originator_last4': self.originator_account_last4,
            'beneficiary_name': self.beneficiary_name,
            'beneficiary_last4': last4(self.beneficiary_account),
            'iban_masked': self.iban_masked,
            'charge': self.charge,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'value_date': self.value_date,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'return_trn': self.return_trn,
            'return_reason': self.return_reason,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'posted_at': self.posted_at,
            'returned_at': self.returned_at,
            'note': self.note,
            'batch_id': self.batch_id,
            'held': self.status == IN_HELD,
            'unmatched': self.status == IN_UNMATCHED,
            'pending_release': self.status == IN_PENDING,
            'queued': self.status == IN_QUEUED,
            'posted': self.status == IN_POSTED,
            'returned': self.status == IN_RETURNED,
            'returnable': self.status in RETURNABLE_BEFORE_POST or self.status == IN_POSTED,
        }


def _clone(row: InboundSwift) -> InboundSwift:
    return InboundSwift(**{key: getattr(row, key) for key in row.__dataclass_fields__})


def _from_row(row: Any) -> InboundSwift:
    return InboundSwift(
        inbound_id=row['inbound_id'],
        uetr=row['uetr'],
        trn=row['trn'] or '',
        rail=row['rail'],
        userid=row['userid'] or '',
        internal_account=row['internal_account'] or '',
        currency=row['currency'],
        amount=row['amount'],
        amount_usd=row['amount_usd'],
        fx_rate=row['fx_rate'],
        sender_bic=row['sender_bic'],
        receiver_bic=row['receiver_bic'],
        originator_name=row['originator_name'],
        originator_account_last4=row['originator_account_last4'] or '',
        beneficiary_name=row['beneficiary_name'],
        beneficiary_account=row['beneficiary_account'],
        iban_masked=row['iban_masked'] or '',
        charge=row['charge'] or 'SHA',
        purpose=row['purpose'] or 'other',
        memo=row['memo'] or '',
        status=row['status'],
        value_date=row['value_date'],
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        return_trn=row['return_trn'] or '',
        return_reason=row['return_reason'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        posted_at=float(row['posted_at'] or 0),
        returned_at=float(row['returned_at'] or 0),
        note=row['note'] or '',
        batch_id=row['batch_id'] or '',
    )


class MemoryInSwiftStore:
    def __init__(self) -> None:
        self._rows: Dict[str, InboundSwift] = {}
        self._by_uetr: Dict[str, str] = {}
        self._seq = 0
        self._lock = threading.Lock()

    def put(self, row: InboundSwift) -> None:
        with self._lock:
            self._rows[row.inbound_id] = _clone(row)
            self._by_uetr[row.uetr] = row.inbound_id

    def update(self, row: InboundSwift) -> None:
        with self._lock:
            if row.inbound_id not in self._rows:
                raise InSwiftError('inbound_not_found', 'Inbound SWIFT payment not found.')
            self._rows[row.inbound_id] = _clone(row)
            self._by_uetr[row.uetr] = row.inbound_id

    def get(self, inbound_id: str) -> Optional[InboundSwift]:
        with self._lock:
            row = self._rows.get(inbound_id)
            return _clone(row) if row is not None else None

    def get_by_uetr(self, uetr: str) -> Optional[InboundSwift]:
        with self._lock:
            inbound_id = self._by_uetr.get(uetr)
            row = self._rows.get(inbound_id) if inbound_id else None
            return _clone(row) if row is not None else None

    def list_for(self, userid: str) -> List[InboundSwift]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.userid == userid]
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def list_unmatched(self) -> List[InboundSwift]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.status == IN_UNMATCHED]
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def list_open(self) -> List[InboundSwift]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.status in OPEN_INBOUNDS]
            rows.sort(key=lambda item: item.created_at)
            return [_clone(row) for row in rows]

    def list_all(self) -> List[InboundSwift]:
        with self._lock:
            rows = list(self._rows.values())
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def next_sequence(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq


class SqliteInSwiftStore:
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
                    uetr TEXT NOT NULL UNIQUE,
                    trn TEXT NOT NULL DEFAULT '',
                    rail TEXT NOT NULL,
                    userid TEXT NOT NULL DEFAULT '',
                    internal_account TEXT NOT NULL DEFAULT '',
                    currency TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    amount_usd TEXT NOT NULL,
                    fx_rate TEXT NOT NULL,
                    sender_bic TEXT NOT NULL,
                    receiver_bic TEXT NOT NULL,
                    originator_name TEXT NOT NULL,
                    originator_account_last4 TEXT NOT NULL DEFAULT '',
                    beneficiary_name TEXT NOT NULL,
                    beneficiary_account TEXT NOT NULL,
                    iban_masked TEXT NOT NULL DEFAULT '',
                    charge TEXT NOT NULL DEFAULT 'SHA',
                    purpose TEXT NOT NULL DEFAULT 'other',
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    value_date TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    return_trn TEXT NOT NULL DEFAULT '',
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

    def _write(self, conn: sqlite3.Connection, row: InboundSwift) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO inbounds (
                inbound_id, uetr, trn, rail, userid, internal_account, currency,
                amount, amount_usd, fx_rate, sender_bic, receiver_bic,
                originator_name, originator_account_last4, beneficiary_name,
                beneficiary_account, iban_masked, charge, purpose, memo, status,
                value_date, actor, releaser, ofac_hit, ofac_match, return_trn,
                return_reason, created_at, updated_at, posted_at, returned_at,
                note, batch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.inbound_id, row.uetr, row.trn, row.rail, row.userid,
                row.internal_account, row.currency, row.amount, row.amount_usd,
                row.fx_rate, row.sender_bic, row.receiver_bic, row.originator_name,
                row.originator_account_last4, row.beneficiary_name,
                row.beneficiary_account, row.iban_masked, row.charge, row.purpose,
                row.memo, row.status, row.value_date, row.actor, row.releaser,
                int(row.ofac_hit), row.ofac_match, row.return_trn, row.return_reason,
                row.created_at, row.updated_at, row.posted_at, row.returned_at,
                row.note, row.batch_id,
            ),
        )

    def put(self, row: InboundSwift) -> None:
        with self._lock, self._connect() as conn:
            self._write(conn, row)
            conn.commit()

    def update(self, row: InboundSwift) -> None:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT inbound_id FROM inbounds WHERE inbound_id = ?', (row.inbound_id,),
            ).fetchone()
            if existing is None:
                raise InSwiftError('inbound_not_found', 'Inbound SWIFT payment not found.')
            self._write(conn, row)
            conn.commit()

    def get(self, inbound_id: str) -> Optional[InboundSwift]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def get_by_uetr(self, uetr: str) -> Optional[InboundSwift]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM inbounds WHERE uetr = ?', (uetr,),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def list_for(self, userid: str) -> List[InboundSwift]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE userid = ? ORDER BY created_at DESC',
                (userid,),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_unmatched(self) -> List[InboundSwift]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE status = ? ORDER BY created_at DESC',
                (IN_UNMATCHED,),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_open(self) -> List[InboundSwift]:
        with self._lock, self._connect() as conn:
            placeholders = ','.join('?' for _ in OPEN_INBOUNDS)
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE status IN (%s) ORDER BY created_at' % placeholders,
                tuple(OPEN_INBOUNDS),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_all(self) -> List[InboundSwift]:
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


class InSwiftService:
    def __init__(
        self,
        policy: InSwiftPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        lookup_fn: Optional[Callable[[str], Optional[str]]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        fx_book: Optional[FxBook] = None,
        calendar: Optional[Target2Calendar] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.lookup_fn = lookup_fn
        self.screen_fn = screen_fn
        self.fx_book = fx_book or FxBook(_env_rates())
        self.calendar = calendar or Target2Calendar(
            cutoff_hour=policy.cutoff_hour,
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise InSwiftError('inswift_disabled', 'Inbound SWIFT payments are disabled.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise InSwiftError('inswift_forbidden', 'Staff only.')

    def _require_view(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_view:
            raise InSwiftError('inswift_forbidden', 'Customers cannot view inbound SWIFT payments.')

    def _require_customer_return(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_return:
            raise InSwiftError('inswift_forbidden', 'Customers cannot request inbound returns.')

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
            raise InSwiftError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise InSwiftError('credit_not_allowed', 'Credit accounts cannot receive inbound SWIFT payments.')

    def _assert_amount(self, usd: Decimal, rail: str) -> None:
        if usd < self.policy.min_amount:
            raise InSwiftError('amount_out_of_range', 'Amount is outside the allowed range.')
        cap = self.policy.rail_max(rail)
        if usd > cap:
            code = 'gpi_amount_exceeded' if rail == RAIL_GPI else 'mt_amount_exceeded'
            raise InSwiftError(code, 'Amount exceeds the %s cap.' % rail)
        if usd > self.policy.max_amount_usd:
            raise InSwiftError('amount_out_of_range', 'Amount is outside the allowed range.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, usd: Decimal) -> bool:
        return usd >= self.policy.dual_control_threshold

    def _assert_receiver(self, receiver_bic: str) -> None:
        if receiver_bic != self.policy.receiver_bic:
            raise InSwiftError('wrong_receiver', 'Message is not addressed to this bank.')

    def quote(self, amount: Any, currency: str = 'EUR') -> FxQuote:
        return self.fx_book.quote(amount, currency)

    def get_inbound(self, *, inbound_id: str, actor: str, actor_type: str) -> InboundSwift:
        self._require_view(actor_type)
        row = self.store.get(inbound_id)
        if row is None:
            raise InSwiftError('inbound_not_found', 'Inbound SWIFT payment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise InSwiftError('inswift_forbidden', 'Not allowed to view this inbound SWIFT payment.')
        return row

    def preview_message(self, values: Dict[str, Any]) -> Dict[str, Any]:
        self._require_enabled()
        message = message_from_values(values)
        fx = self.quote(message['amount'], message['currency'])
        usd = parse_money(fx.amount_usd)
        self._assert_amount(usd, message['rail'])
        self._assert_receiver(message['receiver_bic'])
        ofac = self._screen(message['originator_name'])
        userid = self._lookup(message['beneficiary_account'])
        now = float(self.clock())
        return {
            'message': {
                'uetr': message['uetr'],
                'trn': message['trn'],
                'rail': message['rail'],
                'currency': message['currency'],
                'amount': message['amount'],
                'sender_bic': format_bic(message['sender_bic']),
                'receiver_bic': format_bic(message['receiver_bic']),
                'originator_name': message['originator_name'],
                'beneficiary_name': message['beneficiary_name'],
                'beneficiary_last4': last4(message['beneficiary_account']),
                'charge': message['charge'],
            },
            'fx': fx.to_dict(),
            'matched_userid': userid or '',
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(usd),
            'clock': self.calendar.snapshot(now, rail=message['rail']),
        }

    def _evaluate_status(
        self,
        *,
        userid: str,
        account: str,
        usd: Decimal,
        ofac: ScreenResult,
        rail: str,
        now: float,
    ) -> str:
        if not userid or not account:
            return IN_UNMATCHED
        if ofac.hit:
            return IN_HELD
        if self._needs_dual_control(usd):
            return IN_PENDING
        if self.calendar.should_queue(now, rail=rail):
            return IN_QUEUED
        return IN_POSTED

    def _credit(self, row: InboundSwift) -> str:
        if self.credit_fn is None:
            return 'ok'
        remark = '%s from %s' % (row.rail, row.originator_name[:20] or 'originator')
        result = self.credit_fn(row.internal_account, row.amount_usd, remark)
        return _classify_money_result(result)

    def _debit(self, row: InboundSwift) -> str:
        if self.debit_fn is None:
            return 'ok'
        remark = '%s return %s' % (row.rail, (row.trn or row.uetr)[:12])
        result = self.debit_fn(row.internal_account, row.amount_usd, remark)
        return _classify_money_result(result)

    def _try_post(self, row: InboundSwift) -> InboundSwift:
        classified = self._credit(row)
        now = float(self.clock())
        if classified == 'ok':
            row.status = IN_POSTED
            row.posted_at = now
            row.updated_at = now
            self.store.update(row)
            return row
        row.status = IN_FAILED
        row.note = 'credit_failed'
        row.updated_at = now
        self.store.update(row)
        raise InSwiftError('failed', 'Inbound credit failed.', inbound=row)

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        values: Dict[str, Any],
        batch_id: str = '',
    ) -> Tuple[InboundSwift, bool]:
        self._require_staff(actor_type)
        message = message_from_values(values)
        fx = self.quote(message['amount'], message['currency'])
        usd = parse_money(fx.amount_usd)
        self._assert_amount(usd, message['rail'])
        self._assert_receiver(message['receiver_bic'])
        existing = self.store.get_by_uetr(message['uetr'])
        if existing is not None:
            return existing, False
        if len(self.store.list_all()) >= self.policy.max_inbounds:
            raise InSwiftError('inbound_limit', 'Inbound SWIFT payment limit reached.')
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
        ofac = self._screen(message['originator_name'])
        now = float(self.clock())
        status = self._evaluate_status(
            userid=userid or '',
            account=account,
            usd=usd,
            ofac=ofac,
            rail=message['rail'],
            now=now,
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
        row = InboundSwift(
            inbound_id=uuid.uuid4().hex,
            uetr=message['uetr'],
            trn=message['trn'],
            rail=message['rail'],
            userid=userid or '',
            internal_account=account,
            currency=message['currency'],
            amount=message['amount'],
            amount_usd=fx.amount_usd,
            fx_rate=fx.rate,
            sender_bic=message['sender_bic'],
            receiver_bic=message['receiver_bic'],
            originator_name=originator_name,
            originator_account_last4=last4(message.get('originator_account')),
            beneficiary_name=beneficiary_name,
            beneficiary_account=message['beneficiary_account'],
            iban_masked=mask_iban(message.get('iban') or ''),
            charge=message['charge'],
            purpose=purpose,
            memo=message['memo'],
            status=status,
            value_date=self.calendar.value_date(now, rail=message['rail']).isoformat(),
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            return_trn='',
            return_reason='',
            created_at=now,
            updated_at=now,
            note='credit_not_allowed' if credit_blocked else '',
            batch_id=batch_id or normalize_id(values.get('batch_id') if values.get('batch_id') else ''),
        )
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
        rail: Any = '',
    ) -> Dict[str, Any]:
        self._require_staff(actor_type)
        messages = split_mt_file(text)
        if not messages:
            raise InSwiftError('invalid_mt', 'Operator file has no MT103 messages.')
        batch_id = uuid.uuid4().hex
        accepted = []
        duplicates = []
        errors = []
        for raw in messages:
            try:
                values: Dict[str, Any] = {'file': raw, 'purpose': purpose, 'batch_id': batch_id}
                if rail:
                    values['rail'] = rail
                row, created = self.ingest(
                    actor=actor,
                    actor_type=actor_type,
                    values=values,
                    batch_id=batch_id,
                )
                payload = row.to_dict()
                if created:
                    accepted.append(payload)
                else:
                    duplicates.append(payload)
            except InSwiftError as exc:
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
    ) -> InboundSwift:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_UNMATCHED:
            raise InSwiftError('not_assignable', 'Only unmatched inbound payments can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise InSwiftError('missing_customer_id', 'Customer id is required.')
        account = normalize_account(internal_account or row.beneficiary_account)
        self._assert_internal_account(owner, account)
        row.userid = owner
        row.internal_account = account
        row.actor = str(actor)
        now = float(self.clock())
        row.updated_at = now
        ofac = ScreenResult(bool(row.ofac_hit), row.ofac_match, 100 if row.ofac_hit else 0)
        usd = parse_money(row.amount_usd)
        status = self._evaluate_status(
            userid=owner, account=account, usd=usd, ofac=ofac, rail=row.rail, now=now,
        )
        row.status = status
        if status == IN_POSTED:
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
    ) -> InboundSwift:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_HELD:
            raise InSwiftError('not_overridable', 'Only OFAC-held inbound payments can be overridden.')
        row.ofac_hit = 0
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        now = float(self.clock())
        row.updated_at = now
        usd = parse_money(row.amount_usd)
        status = self._evaluate_status(
            userid=row.userid,
            account=row.internal_account,
            usd=usd,
            ofac=ScreenResult(False, '', 0),
            rail=row.rail,
            now=now,
        )
        row.status = status
        if status == IN_POSTED:
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
    ) -> InboundSwift:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_PENDING:
            raise InSwiftError('not_releasable', 'Inbound payment is not waiting for dual-control.')
        if row.actor and row.actor == str(actor):
            raise InSwiftError('same_approver', 'A different employee must release this inbound payment.')
        row.releaser = str(actor)
        now = float(self.clock())
        row.updated_at = now
        if self.calendar.should_queue(now, rail=row.rail):
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
        reason: Any = 'MS03',
        note: Any = '',
    ) -> InboundSwift:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status not in OPEN_INBOUNDS:
            raise InSwiftError('not_rejectable', 'Inbound payment cannot be rejected.')
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
        reason: Any = 'MS03',
        note: Any = '',
    ) -> InboundSwift:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        return self._return(row, actor=actor, reason=reason, note=note, force_window=True)

    def request_return(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'CUST',
        note: Any = '',
    ) -> InboundSwift:
        self._require_customer_return(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise InSwiftError('inswift_forbidden', 'Not allowed to return this inbound payment.')
        return self._return(row, actor=actor, reason=reason or 'CUST', note=note, force_window=False)

    def _assign_return_id(self, row: InboundSwift) -> None:
        seq = self.store.next_sequence()
        row.return_trn = compose_msg_id(self.policy.source_id + 'R', seq)

    def _customer_window_open(self, row: InboundSwift, now: float) -> bool:
        posted_at = float(row.posted_at or row.created_at)
        if row.rail == RAIL_GPI:
            return (now - posted_at) <= self.policy.customer_return_seconds
        posted_day = self.calendar.local_dt(posted_at).date()
        now_day = self.calendar.local_dt(now).date()
        return posted_day == now_day and self.calendar.is_business_day(now_day)

    def _return(
        self,
        row: InboundSwift,
        *,
        actor: str,
        reason: Any,
        note: Any,
        force_window: bool,
    ) -> InboundSwift:
        if row.status in {IN_RETURNED, IN_REJECTED}:
            raise InSwiftError('already_returned', 'Inbound payment is already returned or rejected.')
        code = normalize_return_reason(reason, default='CUST')
        now = float(self.clock())
        if row.status in RETURNABLE_BEFORE_POST:
            row.status = IN_RETURNED
            row.return_reason = code
            row.note = normalize_note(note) or row.note
            row.actor = str(actor)
            row.returned_at = now
            row.updated_at = now
            self._assign_return_id(row)
            self.store.update(row)
            return row
        if row.status != IN_POSTED:
            raise InSwiftError('not_returnable', 'Inbound payment cannot be returned.')
        if not force_window and not self._customer_window_open(row, now):
            raise InSwiftError('return_window_closed', 'Exception-return window has closed.')
        classified = self._debit(row)
        if classified == 'nsf':
            raise InSwiftError('nsf', 'Insufficient funds to return this inbound payment.', inbound=row)
        if classified != 'ok':
            raise InSwiftError('return_failed', 'Inbound return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.return_reason = code
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.returned_at = now
        row.updated_at = now
        self._assign_return_id(row)
        self.store.update(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[InboundSwift]:
        now = float(self.clock())
        posted = []
        for row in self.store.list_open():
            if row.status != IN_QUEUED:
                continue
            if userid and row.userid != userid:
                continue
            if self.calendar.should_queue(now, rail=row.rail):
                continue
            row.status = IN_POSTED
            row.updated_at = now
            self.store.update(row)
            posted.append(self._try_post(row))
        return posted

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self._require_view(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor and actor != userid:
            raise InSwiftError('inswift_forbidden', 'Not allowed to view this inbound book.')
        self.run_due(userid)
        rows = self.store.list_for(userid)
        posted_ytd = Decimal('0.00')
        returned_ytd = Decimal('0.00')
        for row in rows:
            amount = parse_money(row.amount_usd, allow_zero=True)
            if row.status == IN_POSTED:
                posted_ytd += amount
            elif row.status == IN_RETURNED:
                returned_ytd += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'receiver_bic': format_bic(self.policy.receiver_bic),
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount_usd),
            'gpi_max': money_str(self.policy.gpi_max_usd),
            'mt_max': money_str(self.policy.mt_max_usd),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clock': self.calendar.snapshot(now, rail=RAIL_GPI),
            'mt_clock': self.calendar.snapshot(now, rail=RAIL_MT),
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
            'receiver_bic': format_bic(self.policy.receiver_bic),
            'unmatched': [row.to_dict() for row in rows[:40]],
            'unmatched_count': len(rows),
        }


_SERVICE: Optional[InSwiftService] = None


def set_service(service: Optional[InSwiftService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[InSwiftService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('INSWIFT_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryInSwiftStore()
    path = os.environ.get('INSWIFT_DB', DEFAULT_STORE_PATH)
    return SqliteInSwiftStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    lookup_fn: Optional[Callable[[str], Optional[str]]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    fx_book: Optional[FxBook] = None,
    calendar: Optional[Target2Calendar] = None,
) -> InSwiftService:
    if store is None:
        store = default_store()
    return InSwiftService(
        InSwiftPolicy.from_env(),
        store,
        clock=clock,
        debit_fn=debit_fn,
        credit_fn=credit_fn,
        accounts_fn=accounts_fn,
        lookup_fn=lookup_fn,
        screen_fn=screen_fn,
        fx_book=fx_book,
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
        'inswift_forbidden': 403,
        'inswift_disabled': 403,
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
        'invalid_bic': 400,
        'invalid_uetr': 400,
        'invalid_trn': 400,
        'invalid_mt': 400,
        'invalid_rail': 400,
        'invalid_currency': 400,
        'invalid_iban': 400,
        'invalid_rate': 400,
        'invalid_date': 400,
        'invalid_charge': 400,
        'invalid_reason': 400,
        'invalid_purpose': 400,
        'wrong_receiver': 400,
        'amount_out_of_range': 400,
        'gpi_amount_exceeded': 400,
        'mt_amount_exceeded': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: InSwiftError) -> Dict[str, Any]:
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
    except InSwiftError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: InSwiftService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'InSwifts': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_unmatched(service: InSwiftService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'inswift_forbidden'}), 403
    return jsonify({'InSwifts': service.unmatched_snapshot()}), 200


def handle_preview(service: InSwiftService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'inswift_forbidden'}), 403
    values = request.get_json(silent=True) or {}

    def _run():
        preview = service.preview_message(values)
        return jsonify({'preview': preview}), 200

    return _handle_errors(_run)


def handle_quote(service: InSwiftService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        amount = values.get('amount')
        currency = values.get('currency') or values.get('ccy') or 'EUR'
        return jsonify({'fx': service.quote(amount, currency).to_dict()}), 200

    return _handle_errors(_run)


def handle_ingest(service: InSwiftService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'Inbound SWIFT payment ingested' if created else 'Inbound SWIFT payment already posted',
            'inbound': row.to_dict(),
            'InSwifts': service.snapshot(owner, actor=userid, actor_type=actor_type) if row.userid else service.unmatched_snapshot(),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_file(service: InSwiftService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    text = values.get('file') or values.get('mt') or values.get('text') or values.get('raw')
    if not text:
        return jsonify({'message': 'Operator file is required', 'error': 'missing_file'}), 400

    def _run():
        result = service.ingest_file(
            actor=userid,
            actor_type=actor_type,
            text=text,
            purpose=values.get('purpose') or 'other',
            rail=values.get('rail') or values.get('scheme') or '',
        )
        result['Unmatched'] = service.unmatched_snapshot()
        return jsonify(result), 201 if result['accepted_count'] else 200

    return _handle_errors(_run)


def handle_assign(service: InSwiftService):
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
            'message': 'Inbound SWIFT payment assigned',
            'inbound': row.to_dict(),
            'InSwifts': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_action(service: InSwiftService, action: str):
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
            message = 'Inbound SWIFT payment released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'MS03', note=values.get('note') or '',
            )
            message = 'Inbound SWIFT payment rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'MS03', note=values.get('note') or '',
            )
            message = 'Inbound SWIFT payment returned'
        else:
            raise InSwiftError('invalid_reason', 'Unknown inbound action.')
        owner = row.userid or userid
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'InSwifts': service.snapshot(owner, actor=userid, actor_type=actor_type) if row.userid else service.unmatched_snapshot(),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: InSwiftService):
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
            reason=values.get('reason') or 'CUST',
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Inbound return requested',
            'inbound': row.to_dict(),
            'InSwifts': service.snapshot(row.userid or userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: InSwiftService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner if actor_type not in EMPLOYEE_ROLES else None)
    return jsonify({'InSwifts': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_inswift_routes(app, service: InSwiftService) -> None:
    @app.route('/listInSwifts', methods=['POST', 'GET'])
    def list_inswifts_route():
        return handle_list(service)

    @app.route('/listUnmatchedInSwifts', methods=['POST', 'GET'])
    def list_unmatched_inswifts_route():
        return handle_unmatched(service)

    @app.route('/previewInSwift', methods=['POST', 'GET'])
    def preview_inswift_route():
        return handle_preview(service)

    @app.route('/quoteInSwiftFx', methods=['POST', 'GET'])
    def quote_inswift_fx_route():
        return handle_quote(service)

    @app.route('/ingestInSwift', methods=['POST', 'GET'])
    def ingest_inswift_route():
        return handle_ingest(service)

    @app.route('/ingestInSwiftFile', methods=['POST', 'GET'])
    def ingest_inswift_file_route():
        return handle_ingest_file(service)

    @app.route('/assignInSwift', methods=['POST', 'GET'])
    def assign_inswift_route():
        return handle_assign(service)

    @app.route('/overrideInSwiftOfac', methods=['POST', 'GET'])
    def override_inswift_ofac_route():
        return _staff_action(service, 'override')

    @app.route('/releaseInSwift', methods=['POST', 'GET'])
    def release_inswift_route():
        return _staff_action(service, 'release')

    @app.route('/rejectInSwift', methods=['POST', 'GET'])
    def reject_inswift_route():
        return _staff_action(service, 'reject')

    @app.route('/returnInSwift', methods=['POST', 'GET'])
    def return_inswift_route():
        return _staff_action(service, 'return')

    @app.route('/requestInSwiftReturn', methods=['POST', 'GET'])
    def request_inswift_return_route():
        return handle_request_return(service)

    @app.route('/runDueInSwifts', methods=['POST', 'GET'])
    def run_due_inswifts_route():
        return handle_run_due(service)
