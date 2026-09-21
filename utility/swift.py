"""International SWIFT MT103 origination.

Customers send cross-border wires to named foreign beneficiaries identified
by IBAN (or account + BIC), ISO 4217 currency, and charge bearer. Independent
of domestic Fedwire (PR #73), ACH linking (PR #68), bill-pay ACH (PR #66),
inbound payroll (PR #64), the in-bank payee allowlist (PR #26), and scheduled
internal transfers (PR #36). Existing `/fundTransfer`, `/withdrawAmount`,
and `/sendWire` stay unchanged. `Customers.debit_request` / `credit_request`
still write `debited` / `direct deposited` unless a remark is supplied here.

Foundations (reusable beyond this screen):
- IBAN ISO 13616 mod-97 checksum
- BIC / SWIFT ISO 9362 validation
- ISO 4217 currency + injectable FX book (USD debit equivalent)
- TARGET2 business-day / cutoff clock
- SWIFT gpi UETR (UUID v4)
- MT103 field composition
- Charge-bearer fee policy (OUR / SHA / BEN)
- Dual-control release on USD equivalent

Reuses the local SDN-style OFAC screen from `utility.wire`. Stores are
pluggable (memory for tests, sqlite WAL for restart-safe default). Full IBAN
and destination account numbers never appear in to_dict / snapshots.
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
    ScreenResult,
    account_types_from_customer_payload,
    last4,
    money_str,
    normalize_account,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
BENE_ACTIVE = 'active'
BENE_PAUSED = 'paused'
BENE_ARCHIVED = 'archived'
BENE_STATUSES = frozenset({BENE_ACTIVE, BENE_PAUSED, BENE_ARCHIVED})
OPEN_BENE = frozenset({BENE_ACTIVE, BENE_PAUSED})
SWIFT_HELD = 'held'
SWIFT_QUEUED = 'queued'
SWIFT_PENDING = 'pending_release'
SWIFT_SENT = 'sent'
SWIFT_COMPLETED = 'completed'
SWIFT_REJECTED = 'rejected'
SWIFT_CANCELLED = 'cancelled'
SWIFT_RECALLED = 'recalled'
SWIFT_NSF = 'nsf'
SWIFT_FAILED = 'failed'
SWIFT_STATUSES = frozenset({
    SWIFT_HELD, SWIFT_QUEUED, SWIFT_PENDING, SWIFT_SENT, SWIFT_COMPLETED,
    SWIFT_REJECTED, SWIFT_CANCELLED, SWIFT_RECALLED, SWIFT_NSF, SWIFT_FAILED,
})
OPEN_SWIFTS = frozenset({SWIFT_HELD, SWIFT_QUEUED, SWIFT_PENDING, SWIFT_SENT})
CANCELABLE = frozenset({SWIFT_HELD, SWIFT_QUEUED, SWIFT_PENDING})
FEE_NONE = 'none'
FEE_COLLECTED = 'collected'
FEE_WAIVED = 'waived'
FEE_NSF = 'nsf'
CHARGES = frozenset({'OUR', 'SHA', 'BEN'})
CHARGE_ALIASES = {
    'our': 'OUR', 'sender': 'OUR', 'ours': 'OUR',
    'sha': 'SHA', 'shared': 'SHA', 'share': 'SHA',
    'ben': 'BEN', 'beneficiary': 'BEN', 'theirs': 'BEN',
}
PURPOSES = frozenset({'family', 'goods', 'payroll', 'tax', 'loan', 'rent', 'other'})
PURPOSE_ALIASES = {
    'personal': 'family', 'gift': 'family', 'support': 'family',
    'invoice': 'goods', 'purchase': 'goods', 'vendor': 'goods',
    'salary': 'payroll', 'wage': 'payroll',
    'irs': 'tax', 'taxes': 'tax',
    'mortgage': 'loan', 'housing': 'rent',
}
# ISO 13616 IBAN lengths. Unknown countries still accept 15-34 if checksum holds.
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
# Countries that do not use IBAN; account + BIC is required instead.
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
# USD per 1 unit of foreign currency. Injectable via SWIFT_FX_<CCY>.
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
CHARGE_FEES = {
    'OUR': Decimal('45.00'),
    'SHA': Decimal('35.00'),
    'BEN': Decimal('15.00'),
}
MONEY_QUANTUM = Decimal('0.01')
RATE_QUANTUM = Decimal('0.000001')
UETR_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
)
BIC_RE = re.compile(r'^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$')
DEFAULT_STORE_PATH = 'SystemLogs/swift.sqlite'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)


class SwiftError(ValueError):
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
        raise SwiftError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise SwiftError('invalid_name', 'Beneficiary legal name must be 2-80 characters.')
    return text


def normalize_street(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (3 <= len(text) <= 80):
        raise SwiftError('invalid_address', 'Street must be 3-80 characters.')
    return text


def normalize_city(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise SwiftError('invalid_address', 'City must be 2-40 characters.')
    return text


def normalize_country(value: Any) -> str:
    text = str(value or '').strip().upper()
    if text not in ISO_COUNTRIES:
        raise SwiftError('invalid_country', 'Country must be an ISO 3166-1 alpha-2 code.')
    return text


def normalize_postal(value: Any, *, required: bool = False) -> str:
    text = str(value or '').strip().upper()
    text = re.sub(r'[^A-Z0-9 -]', '', text)
    if not text:
        if required:
            raise SwiftError('invalid_address', 'Postal code is required.')
        return ''
    if not (2 <= len(text) <= 12):
        raise SwiftError('invalid_address', 'Postal code must be 2-12 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise SwiftError('invalid_purpose', 'Unknown wire purpose.')
    return text


def normalize_charge(value: Any, *, default: str = 'SHA') -> str:
    text = str(value or default).strip().upper()
    text = CHARGE_ALIASES.get(text.lower(), text)
    if text not in CHARGES:
        raise SwiftError('invalid_charge', 'Charge bearer must be OUR, SHA, or BEN.')
    return text


def normalize_currency(value: Any, *, default: str = 'EUR') -> str:
    text = str(value or default).strip().upper()
    if text not in SUPPORTED_CURRENCIES:
        raise SwiftError('invalid_currency', 'Unsupported ISO 4217 currency.')
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
    if value is None or (isinstance(value, str) and not value.strip()):
        raise SwiftError('invalid_rate', 'FX rate is required.')
    try:
        rate = Decimal(str(value).strip().replace(',', ''))
    except (InvalidOperation, ValueError):
        raise SwiftError('invalid_rate', 'FX rate is invalid.') from None
    if not rate.is_finite() or rate <= 0:
        raise SwiftError('invalid_rate', 'FX rate must be positive.')
    return rate.quantize(RATE_QUANTUM, rounding=ROUND_HALF_EVEN)


def iban_mod97(digits: str) -> int:
    """Chunked mod-97 so we never build a 10^60 int from a 34-char IBAN."""
    remainder = 0
    for index in range(0, len(digits), 9):
        remainder = int(str(remainder) + digits[index:index + 9]) % 97
    return remainder


def iban_check_digit_ok(iban: str) -> bool:
    """ISO 13616: rearrange, A=10..Z=35, remaining value mod 97 equals 1."""
    if len(iban) < 15 or len(iban) > 34 or not iban[:2].isalpha() or not iban[2:4].isdigit():
        return False
    rearranged = iban[4:] + iban[:4]
    numeric = []
    for ch in rearranged:
        if ch.isdigit():
            numeric.append(ch)
        elif ch.isalpha():
            numeric.append(str(ord(ch) - 55))
        else:
            return False
    return iban_mod97(''.join(numeric)) == 1


def normalize_iban(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[^A-Za-z0-9]', '', str(value or '')).upper()
    if not text:
        if required:
            raise SwiftError('invalid_iban', 'IBAN is required.')
        return ''
    country = text[:2]
    expected = IBAN_LENGTHS.get(country)
    if expected is not None and len(text) != expected:
        raise SwiftError('invalid_iban', 'IBAN length does not match country %s.' % country)
    if expected is None and not (15 <= len(text) <= 34):
        raise SwiftError('invalid_iban', 'IBAN must be 15-34 characters.')
    if country not in ISO_COUNTRIES:
        raise SwiftError('invalid_iban', 'IBAN country code is unknown.')
    if not iban_check_digit_ok(text):
        raise SwiftError('invalid_iban', 'IBAN failed mod-97 checksum.')
    return text


def mask_iban(iban: str) -> str:
    if not iban:
        return ''
    if len(iban) <= 6:
        return iban[:2] + '****'
    return iban[:2] + '****' + iban[-4:]


def normalize_bic(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[^A-Za-z0-9]', '', str(value or '')).upper()
    if not text:
        if required:
            raise SwiftError('invalid_bic', 'BIC is required.')
        return ''
    if len(text) == 8:
        text = text + 'XXX'
    if not BIC_RE.match(text):
        raise SwiftError('invalid_bic', 'BIC must be 8 or 11 characters (ISO 9362).')
    country = text[4:6]
    if country not in ISO_COUNTRIES:
        raise SwiftError('invalid_bic', 'BIC country code is unknown.')
    if text[6] == '0':
        raise SwiftError('invalid_bic', 'BIC location code cannot start with 0.')
    return text


def bic8(value: str) -> str:
    return (value or '')[:8]


def bic_country(value: str) -> str:
    text = str(value or '')
    if len(text) >= 6:
        return text[4:6]
    return ''


def normalize_external_account(value: Any, *, required: bool = True) -> str:
    digits = re.sub(r'[^A-Za-z0-9]', '', str(value or '')).upper()
    if not digits:
        if required:
            raise SwiftError('invalid_account', 'External account is required.')
        return ''
    if not (4 <= len(digits) <= 34):
        raise SwiftError('invalid_account', 'External account must be 4-34 characters.')
    return digits


def compose_uetr(value: Any = None) -> str:
    """SWIFT gpi UETR is a lowercase UUID v4."""
    if value:
        text = str(value).strip().lower()
        if not UETR_RE.match(text):
            raise SwiftError('invalid_uetr', 'UETR must be a UUID v4.')
        return text
    return str(uuid.uuid4())


def compose_mur(cycle_date: str, source_bic: str, sequence: int) -> str:
    """Message user reference: {YYYYMMDD}{BIC8}{6-digit sequence}."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise SwiftError('invalid_mur', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise SwiftError('invalid_mur', 'MUR sequence out of range.')
    return '%s%s%06d' % (day, bic8(source_bic), seq)


def format_swift_amount(amount: Decimal, currency: str) -> str:
    """SWIFT 32A uses a comma as the decimal separator."""
    quantized = quantize_ccy(amount, currency)
    if CURRENCY_MINOR.get(currency, 2) == 0:
        return '%s%s,' % (currency, int(quantized))
    text = format(quantized, 'f').replace('.', ',')
    return '%s%s' % (currency, text)


def compose_mt103(
    *,
    reference: str,
    value_date: str,
    currency: str,
    amount: Decimal,
    ordering_name: str,
    beneficiary_name: str,
    beneficiary_iban: str,
    beneficiary_account: str,
    beneficiary_bic: str,
    intermediary_bic: str = '',
    charge: str = 'SHA',
    purpose: str = 'other',
    memo: str = '',
    uetr: str = '',
) -> Dict[str, str]:
    """Compose MT103 field map. Reusable for any cross-border credit transfer."""
    dest = beneficiary_iban or beneficiary_account
    field59 = '/%s\n%s' % (dest, beneficiary_name)
    field70 = ' '.join(part for part in (purpose.upper(), memo) if part).strip()[:140]
    payload = {
        '20': (reference or 'NONREF')[:16],
        '23B': 'CRED',
        '32A': '%s%s' % (value_date, format_swift_amount(amount, currency)),
        '50K': ordering_name[:80],
        '57A': bic8(beneficiary_bic),
        '59': field59,
        '70': field70 or 'OTHR',
        '71A': charge,
        '121': uetr,
    }
    if intermediary_bic:
        payload['56A'] = bic8(intermediary_bic)
    return payload


def compute_fee(charge: str, *, waived: bool = False, table: Optional[Dict[str, Decimal]] = None) -> Decimal:
    if waived:
        return Decimal('0.00')
    fees = table or CHARGE_FEES
    fee = fees.get(charge, fees['SHA'])
    return fee.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


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


class Target2Calendar:
    """TARGET2 business-day + customer cutoff. International value date is T+1."""

    def __init__(
        self,
        *,
        cutoff_hour: int = 16,
        cutoff_minute: int = 0,
        tz_offset_hours: int = -4,
        extra_holidays: Sequence[str] = (),
    ) -> None:
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
        return day in target2_holidays(day.year) or day in self.extra_holidays

    def is_business_day(self, day: date) -> bool:
        return not self.is_weekend(day) and not self.is_holiday(day)

    def is_after_cutoff(self, ts: float) -> bool:
        local = self.local_dt(ts)
        return (local.hour, local.minute) >= (self.cutoff_hour, self.cutoff_minute)

    def next_business_day(self, day: date) -> date:
        cursor = day + timedelta(days=1)
        while not self.is_business_day(cursor):
            cursor += timedelta(days=1)
        return cursor

    def value_date(self, ts: float) -> date:
        """Spot T+1 on the TARGET2 calendar. After cutoff / holidays push one extra day."""
        local = self.local_dt(ts)
        day = local.date()
        if not self.is_business_day(day) or self.is_after_cutoff(ts):
            day = self.next_business_day(day)
        return self.next_business_day(day)

    def cycle_date(self, ts: float) -> str:
        return self.value_date(ts).strftime('%Y%m%d')

    def snapshot(self, ts: float) -> Dict[str, Any]:
        local = self.local_dt(ts)
        value = self.value_date(ts)
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'cutoff': '%02d:%02d' % (self.cutoff_hour, self.cutoff_minute),
            'after_cutoff': self.is_after_cutoff(ts) or not self.is_business_day(local.date()),
            'business_day': self.is_business_day(local.date()),
            'value_date': value.isoformat(),
            'cycle_date': value.strftime('%Y%m%d'),
            'calendar': 'TARGET2',
        }


@dataclass
class FxQuote:
    currency: str
    amount: str
    rate: str
    debit_usd: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'currency': self.currency,
            'amount': self.amount,
            'rate': self.rate,
            'debit_usd': self.debit_usd,
        }


class FxBook:
    """Injectable USD-per-unit FX book. Quote is the reusable conversion API."""

    def __init__(self, rates: Optional[Dict[str, Decimal]] = None) -> None:
        merged = dict(DEFAULT_RATES)
        if rates:
            for key, value in rates.items():
                merged[str(key).upper()] = parse_rate(value)
        self.rates = merged

    def rate(self, currency: str) -> Decimal:
        ccy = normalize_currency(currency)
        if ccy not in self.rates:
            raise SwiftError('invalid_currency', 'No FX rate for %s.' % ccy)
        return self.rates[ccy]

    def quote(self, amount: Any, currency: str) -> FxQuote:
        ccy = normalize_currency(currency)
        foreign = quantize_ccy(parse_money(amount), ccy)
        if foreign <= 0:
            raise AmountError('invalid_amount')
        rate = self.rate(ccy)
        debit = (foreign * rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
        if debit <= 0:
            raise SwiftError('invalid_amount', 'USD equivalent rounds to zero.')
        return FxQuote(
            currency=ccy,
            amount=money_str_ccy(foreign, ccy),
            rate=format(rate, 'f'),
            debit_usd=money_str(debit),
        )


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
    rates = dict(DEFAULT_RATES)
    prefix = 'SWIFT_FX_'
    for key, value in os.environ.items():
        if not key.startswith(prefix) or not str(value).strip():
            continue
        ccy = key[len(prefix):].upper()
        if ccy in SUPPORTED_CURRENCIES:
            rates[ccy] = parse_rate(value)
    return rates


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
class SwiftPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    allow_credit: bool = False
    max_beneficiaries: int = 12
    max_wires: int = 120
    min_amount: Decimal = Decimal('10.00')
    max_amount: Decimal = Decimal('1000000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    cutoff_hour: int = 16
    tz_offset_hours: int = -4
    source_bic: str = 'KONOUS33XXX'
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()
    charge_fees: Dict[str, Decimal] = None  # type: ignore[assignment]
    fx_rates: Dict[str, Decimal] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.charge_fees is None:
            self.charge_fees = dict(CHARGE_FEES)
        if self.fx_rates is None:
            self.fx_rates = dict(DEFAULT_RATES)

    @classmethod
    def from_env(cls) -> 'SwiftPolicy':
        extra = _env_list('SWIFT_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        fees = dict(CHARGE_FEES)
        for charge, env_name in (('OUR', 'SWIFT_FEE_OUR'), ('SHA', 'SWIFT_FEE_SHA'), ('BEN', 'SWIFT_FEE_BEN')):
            raw = os.environ.get(env_name)
            if raw and str(raw).strip():
                fees[charge] = parse_money(raw, allow_zero=True)
        source = os.environ.get('SWIFT_SOURCE_BIC', 'KONOUS33')
        return cls(
            enabled=_env_bool('SWIFT_ENABLED', True),
            customer_manage=_env_bool('SWIFT_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('SWIFT_CUSTOMER_SEND', True),
            allow_credit=_env_bool('SWIFT_ALLOW_CREDIT', False),
            max_beneficiaries=max(1, _env_int('SWIFT_MAX_BENEFICIARIES', 12)),
            max_wires=max(1, _env_int('SWIFT_MAX_WIRES', 120)),
            min_amount=_env_money('SWIFT_MIN_AMOUNT', '10.00'),
            max_amount=_env_money('SWIFT_MAX_AMOUNT', '1000000.00'),
            dual_control_threshold=_env_money('SWIFT_DUAL_CONTROL', '10000.00'),
            cutoff_hour=max(0, min(23, _env_int('SWIFT_CUTOFF_HOUR', 16))),
            tz_offset_hours=_env_int('SWIFT_TZ_OFFSET', -4),
            source_bic=normalize_bic(source),
            watchlist=watch,
            extra_holidays=_env_list('SWIFT_HOLIDAYS'),
            charge_fees=fees,
            fx_rates=_env_rates(),
        )


@dataclass
class SwiftBeneficiary:
    beneficiary_id: str
    userid: str
    nickname: str
    legal_name: str
    country: str
    bic: str
    iban: str
    account_number: str
    intermediary_bic: str
    street: str
    city: str
    postal: str
    default_account: str
    default_currency: str
    default_charge: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'beneficiary_id': self.beneficiary_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'country': self.country,
            'bic': self.bic,
            'iban_masked': mask_iban(self.iban),
            'account_last4': last4(self.iban or self.account_number),
            'intermediary_bic': self.intermediary_bic,
            'street': self.street,
            'city': self.city,
            'postal': self.postal,
            'default_account': self.default_account,
            'default_currency': self.default_currency,
            'default_charge': self.default_charge,
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == BENE_ACTIVE,
            'paused': self.status == BENE_PAUSED,
            'archived': self.status == BENE_ARCHIVED,
        }


@dataclass
class SwiftTransfer:
    wire_id: str
    trace_id: str
    beneficiary_id: str
    userid: str
    internal_account: str
    currency: str
    amount: str
    rate: str
    debit_usd: str
    fee: str
    fee_status: str
    charge: str
    nickname: str
    legal_name: str
    country: str
    bic: str
    iban_masked: str
    account_last4: str
    intermediary_bic: str
    purpose: str
    memo: str
    status: str
    uetr: str
    mur: str
    value_date: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    created_at: float
    updated_at: float
    sent_at: float = 0.0
    completed_at: float = 0.0
    recalled_at: float = 0.0
    note: str = ''
    reason: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'wire_id': self.wire_id,
            'trace_id': self.trace_id,
            'beneficiary_id': self.beneficiary_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'currency': self.currency,
            'amount': self.amount,
            'rate': self.rate,
            'debit_usd': self.debit_usd,
            'fee': self.fee,
            'fee_status': self.fee_status,
            'charge': self.charge,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'country': self.country,
            'bic': self.bic,
            'iban_masked': self.iban_masked,
            'account_last4': self.account_last4,
            'intermediary_bic': self.intermediary_bic,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'uetr': self.uetr,
            'mur': self.mur,
            'value_date': self.value_date,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'sent_at': self.sent_at,
            'completed_at': self.completed_at,
            'recalled_at': self.recalled_at,
            'note': self.note,
            'reason': self.reason,
            'held': self.status == SWIFT_HELD,
            'queued': self.status == SWIFT_QUEUED,
            'pending_release': self.status == SWIFT_PENDING,
            'sent': self.status == SWIFT_SENT,
            'completed': self.status == SWIFT_COMPLETED,
            'cancelable': self.status in CANCELABLE,
        }


def _clone_bene(row: SwiftBeneficiary) -> SwiftBeneficiary:
    return SwiftBeneficiary(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_wire(row: SwiftTransfer) -> SwiftTransfer:
    return SwiftTransfer(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _bene_from_row(row: Any) -> SwiftBeneficiary:
    return SwiftBeneficiary(
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        country=row['country'],
        bic=row['bic'],
        iban=row['iban'] or '',
        account_number=row['account_number'] or '',
        intermediary_bic=row['intermediary_bic'] or '',
        street=row['street'],
        city=row['city'],
        postal=row['postal'] or '',
        default_account=row['default_account'],
        default_currency=row['default_currency'],
        default_charge=row['default_charge'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
    )


def _wire_from_row(row: Any) -> SwiftTransfer:
    return SwiftTransfer(
        wire_id=row['wire_id'],
        trace_id=row['trace_id'],
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        currency=row['currency'],
        amount=row['amount'],
        rate=row['rate'],
        debit_usd=row['debit_usd'],
        fee=row['fee'],
        fee_status=row['fee_status'],
        charge=row['charge'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        country=row['country'],
        bic=row['bic'],
        iban_masked=row['iban_masked'],
        account_last4=row['account_last4'],
        intermediary_bic=row['intermediary_bic'] or '',
        purpose=row['purpose'],
        memo=row['memo'] or '',
        status=row['status'],
        uetr=row['uetr'] or '',
        mur=row['mur'] or '',
        value_date=row['value_date'],
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        sent_at=float(row['sent_at'] or 0),
        completed_at=float(row['completed_at'] or 0),
        recalled_at=float(row['recalled_at'] or 0),
        note=row['note'] or '',
        reason=row['reason'] or '',
    )


class MemorySwiftStore:
    def __init__(self) -> None:
        self._benes: Dict[str, SwiftBeneficiary] = {}
        self._wires: Dict[str, SwiftTransfer] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_beneficiary(self, row: SwiftBeneficiary) -> None:
        with self._lock:
            self._benes[row.beneficiary_id] = row

    def get_beneficiary(self, beneficiary_id: str) -> Optional[SwiftBeneficiary]:
        with self._lock:
            row = self._benes.get(beneficiary_id)
            return _clone_bene(row) if row else None

    def update_beneficiary(self, row: SwiftBeneficiary) -> None:
        with self._lock:
            self._benes[row.beneficiary_id] = row

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[SwiftBeneficiary]:
        with self._lock:
            rows = [_clone_bene(row) for row in self._benes.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != BENE_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[SwiftBeneficiary]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._benes.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_BENE:
                    return _clone_bene(row)
        return None

    def find_beneficiary_by_fingerprint(
        self, userid: str, iban: str, bic: str, account_number: str,
    ) -> Optional[SwiftBeneficiary]:
        with self._lock:
            for row in self._benes.values():
                if row.userid != userid or row.status not in OPEN_BENE:
                    continue
                if iban and row.iban == iban:
                    return _clone_bene(row)
                if not iban and row.bic == bic and row.account_number == account_number:
                    return _clone_bene(row)
        return None

    def put_wire(self, row: SwiftTransfer) -> SwiftTransfer:
        with self._lock:
            existing_id = self._by_trace.get(row.trace_id)
            if existing_id is not None:
                return self._wires[existing_id]
            self._wires[row.wire_id] = row
            self._by_trace[row.trace_id] = row.wire_id
            return row

    def update_wire(self, row: SwiftTransfer) -> None:
        with self._lock:
            self._wires[row.wire_id] = row

    def get_wire(self, wire_id: str) -> Optional[SwiftTransfer]:
        with self._lock:
            row = self._wires.get(wire_id)
            return _clone_wire(row) if row else None

    def get_wire_by_trace(self, trace_id: str) -> Optional[SwiftTransfer]:
        with self._lock:
            wire_id = self._by_trace.get(trace_id)
            return _clone_wire(self._wires[wire_id]) if wire_id else None

    def list_wires(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[SwiftTransfer]:
        with self._lock:
            rows = [_clone_wire(row) for row in self._wires.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if beneficiary_id is not None:
            rows = [row for row in rows if row.beneficiary_id == beneficiary_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def next_mur_sequence(self, cycle_date: str) -> int:
        with self._lock:
            used = [row.mur for row in self._wires.values() if row.mur.startswith(cycle_date)]
        return len(used) + 1


class SqliteSwiftStore:
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
                    country TEXT NOT NULL,
                    bic TEXT NOT NULL,
                    iban TEXT NOT NULL DEFAULT '',
                    account_number TEXT NOT NULL DEFAULT '',
                    intermediary_bic TEXT NOT NULL DEFAULT '',
                    street TEXT NOT NULL,
                    city TEXT NOT NULL,
                    postal TEXT NOT NULL DEFAULT '',
                    default_account TEXT NOT NULL,
                    default_currency TEXT NOT NULL,
                    default_charge TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wires (
                    wire_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    beneficiary_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    internal_account TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    rate TEXT NOT NULL,
                    debit_usd TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    fee_status TEXT NOT NULL,
                    charge TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    country TEXT NOT NULL,
                    bic TEXT NOT NULL,
                    iban_masked TEXT NOT NULL,
                    account_last4 TEXT NOT NULL,
                    intermediary_bic TEXT NOT NULL DEFAULT '',
                    purpose TEXT NOT NULL,
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    uetr TEXT NOT NULL DEFAULT '',
                    mur TEXT NOT NULL DEFAULT '',
                    value_date TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    sent_at REAL NOT NULL DEFAULT 0,
                    completed_at REAL NOT NULL DEFAULT 0,
                    recalled_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_beneficiary(self, row: SwiftBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO beneficiaries (
                    beneficiary_id, userid, nickname, legal_name, country, bic, iban,
                    account_number, intermediary_bic, street, city, postal,
                    default_account, default_currency, default_charge, status, actor,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.beneficiary_id, row.userid, row.nickname, row.legal_name,
                    row.country, row.bic, row.iban, row.account_number,
                    row.intermediary_bic, row.street, row.city, row.postal,
                    row.default_account, row.default_currency, row.default_charge,
                    row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_beneficiary(self, beneficiary_id: str) -> Optional[SwiftBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM beneficiaries WHERE beneficiary_id = ?', (beneficiary_id,),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def update_beneficiary(self, row: SwiftBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE beneficiaries SET nickname=?, legal_name=?, country=?, bic=?,
                    iban=?, account_number=?, intermediary_bic=?, street=?, city=?,
                    postal=?, default_account=?, default_currency=?, default_charge=?,
                    status=?, actor=?, updated_at=?
                WHERE beneficiary_id=?
                """,
                (
                    row.nickname, row.legal_name, row.country, row.bic, row.iban,
                    row.account_number, row.intermediary_bic, row.street, row.city,
                    row.postal, row.default_account, row.default_currency,
                    row.default_charge, row.status, row.actor, row.updated_at,
                    row.beneficiary_id,
                ),
            )
            conn.commit()

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[SwiftBeneficiary]:
        sql = 'SELECT * FROM beneficiaries'
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
        return [_bene_from_row(row) for row in rows]

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[SwiftBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM beneficiaries
                WHERE userid = ? AND lower(nickname) = lower(?)
                  AND status IN ('active', 'paused')
                """,
                (userid, nickname),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def find_beneficiary_by_fingerprint(
        self, userid: str, iban: str, bic: str, account_number: str,
    ) -> Optional[SwiftBeneficiary]:
        with self._lock, self._connect() as conn:
            if iban:
                row = conn.execute(
                    """
                    SELECT * FROM beneficiaries
                    WHERE userid = ? AND iban = ? AND status IN ('active', 'paused')
                    """,
                    (userid, iban),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM beneficiaries
                    WHERE userid = ? AND bic = ? AND account_number = ?
                      AND status IN ('active', 'paused')
                    """,
                    (userid, bic, account_number),
                ).fetchone()
        return _bene_from_row(row) if row else None

    def put_wire(self, row: SwiftTransfer) -> SwiftTransfer:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM wires WHERE trace_id = ?', (row.trace_id,)
            ).fetchone()
            if existing is not None:
                return _wire_from_row(existing)
            conn.execute(
                """
                INSERT INTO wires (
                    wire_id, trace_id, beneficiary_id, userid, internal_account,
                    currency, amount, rate, debit_usd, fee, fee_status, charge,
                    nickname, legal_name, country, bic, iban_masked, account_last4,
                    intermediary_bic, purpose, memo, status, uetr, mur, value_date,
                    actor, releaser, ofac_hit, ofac_match, created_at, updated_at,
                    sent_at, completed_at, recalled_at, note, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.wire_id, row.trace_id, row.beneficiary_id, row.userid,
                    row.internal_account, row.currency, row.amount, row.rate,
                    row.debit_usd, row.fee, row.fee_status, row.charge, row.nickname,
                    row.legal_name, row.country, row.bic, row.iban_masked,
                    row.account_last4, row.intermediary_bic, row.purpose, row.memo,
                    row.status, row.uetr, row.mur, row.value_date, row.actor,
                    row.releaser, row.ofac_hit, row.ofac_match, row.created_at,
                    row.updated_at, row.sent_at, row.completed_at, row.recalled_at,
                    row.note, row.reason,
                ),
            )
            conn.commit()
            return row

    def update_wire(self, row: SwiftTransfer) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE wires SET fee=?, fee_status=?, status=?, uetr=?, mur=?,
                    value_date=?, actor=?, releaser=?, ofac_hit=?, ofac_match=?,
                    updated_at=?, sent_at=?, completed_at=?, recalled_at=?,
                    note=?, reason=?
                WHERE wire_id=?
                """,
                (
                    row.fee, row.fee_status, row.status, row.uetr, row.mur,
                    row.value_date, row.actor, row.releaser, row.ofac_hit,
                    row.ofac_match, row.updated_at, row.sent_at, row.completed_at,
                    row.recalled_at, row.note, row.reason, row.wire_id,
                ),
            )
            conn.commit()

    def get_wire(self, wire_id: str) -> Optional[SwiftTransfer]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM wires WHERE wire_id = ?', (wire_id,)).fetchone()
        return _wire_from_row(row) if row else None

    def get_wire_by_trace(self, trace_id: str) -> Optional[SwiftTransfer]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM wires WHERE trace_id = ?', (trace_id,)).fetchone()
        return _wire_from_row(row) if row else None

    def list_wires(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[SwiftTransfer]:
        sql = 'SELECT * FROM wires'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if beneficiary_id is not None:
            clauses.append('beneficiary_id = ?')
            params.append(beneficiary_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_wire_from_row(row) for row in rows]

    def next_mur_sequence(self, cycle_date: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM wires WHERE mur LIKE ?",
                (cycle_date + '%',),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class SwiftService:
    def __init__(
        self,
        policy: SwiftPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[Target2Calendar] = None,
        fx: Optional[FxBook] = None,
        uetr_fn: Optional[Callable[[], str]] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or Target2Calendar(
            cutoff_hour=policy.cutoff_hour,
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )
        self.fx = fx or FxBook(policy.fx_rates)
        self.uetr_fn = uetr_fn or (lambda: compose_uetr())

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise SwiftError('swift_disabled', 'International wires are disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise SwiftError('swift_forbidden', 'Customers cannot manage SWIFT beneficiaries.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise SwiftError('swift_forbidden', 'Customers cannot originate SWIFT wires.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise SwiftError('swift_forbidden', 'Staff only.')

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
            raise SwiftError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise SwiftError('credit_not_allowed', 'Credit accounts cannot originate SWIFT wires.')

    def _assert_usd(self, dollars: Decimal) -> None:
        if dollars < self.policy.min_amount or dollars > self.policy.max_amount:
            raise SwiftError('amount_out_of_range', 'USD equivalent is outside the allowed range.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, debit_usd: Decimal) -> bool:
        return debit_usd >= self.policy.dual_control_threshold

    def quote_fx(self, amount: Any, currency: Any) -> FxQuote:
        return self.fx.quote(amount, normalize_currency(currency))

    def add_beneficiary(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        bic: Any,
        street: Any,
        city: Any,
        country: Any,
        default_account: Any,
        iban: Any = None,
        account_number: Any = None,
        intermediary_bic: Any = None,
        postal: Any = None,
        default_currency: Any = 'EUR',
        default_charge: Any = 'SHA',
    ) -> SwiftBeneficiary:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise SwiftError('swift_forbidden', 'Not allowed to add beneficiaries for this customer.')
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        routing = normalize_bic(bic)
        iso_country = normalize_country(country)
        bic_cc = bic_country(routing)
        if bic_cc != iso_country:
            raise SwiftError('invalid_country', 'BIC country does not match beneficiary country.')
        iban_value = normalize_iban(iban, required=False)
        external = normalize_external_account(account_number, required=False)
        if iso_country in IBAN_LENGTHS:
            if not iban_value:
                raise SwiftError('invalid_iban', 'IBAN is required for this country.')
            if iban_value[:2] != iso_country:
                raise SwiftError('invalid_country', 'IBAN country does not match beneficiary country.')
        else:
            if not external:
                raise SwiftError('invalid_account', 'Account number is required when IBAN is not used.')
        intermedia = normalize_bic(intermediary_bic, required=False)
        if intermedia and intermedia == routing:
            raise SwiftError('invalid_bic', 'Intermediary BIC must differ from the beneficiary BIC.')
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        if self.store.find_beneficiary_by_nickname(owner_userid, name) is not None:
            raise SwiftError('beneficiary_duplicate', 'A beneficiary with that nickname already exists.')
        if self.store.find_beneficiary_by_fingerprint(owner_userid, iban_value, routing, external) is not None:
            raise SwiftError('beneficiary_duplicate', 'That beneficiary account is already on file.')
        open_rows = [row for row in self.store.list_beneficiaries(owner_userid) if row.status in OPEN_BENE]
        if len(open_rows) >= self.policy.max_beneficiaries:
            raise SwiftError('beneficiary_limit', 'SWIFT beneficiary limit reached.')
        now = float(self.clock())
        row = SwiftBeneficiary(
            beneficiary_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            country=iso_country,
            bic=routing,
            iban=iban_value,
            account_number=external,
            intermediary_bic=intermedia,
            street=normalize_street(street),
            city=normalize_city(city),
            postal=normalize_postal(postal),
            default_account=account,
            default_currency=normalize_currency(default_currency),
            default_charge=normalize_charge(default_charge),
            status=BENE_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_beneficiary(row)
        return row

    def get_beneficiary(self, *, beneficiary_id: str, actor: str, actor_type: str) -> SwiftBeneficiary:
        self._require_enabled()
        row = self.store.get_beneficiary(beneficiary_id)
        if row is None:
            raise SwiftError('beneficiary_not_found', 'SWIFT beneficiary not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise SwiftError('swift_forbidden', 'Not allowed to view this beneficiary.')
        return row

    def enforce_beneficiary(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> SwiftBeneficiary:
        """Reusable gate: destination must be an active, unarchived SWIFT beneficiary."""
        row = self.get_beneficiary(beneficiary_id=beneficiary_id, actor=actor, actor_type=actor_type)
        if row.status == BENE_ARCHIVED:
            raise SwiftError('already_archived', 'Beneficiary is archived.')
        if row.status == BENE_PAUSED:
            raise SwiftError('beneficiary_paused', 'Beneficiary is paused.')
        if require_active and row.status != BENE_ACTIVE:
            raise SwiftError('invalid_status', 'Beneficiary is not active.')
        return row

    def set_beneficiary_status(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> SwiftBeneficiary:
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
            raise SwiftError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == BENE_ARCHIVED:
            raise SwiftError('already_archived', 'Beneficiary is already archived.')
        if wanted == BENE_PAUSED:
            if row.status == BENE_PAUSED:
                raise SwiftError('already_paused', 'Beneficiary is already paused.')
            if row.status != BENE_ACTIVE:
                raise SwiftError('invalid_status', 'Only an active beneficiary can be paused.')
        elif wanted == BENE_ACTIVE:
            if row.status == BENE_ACTIVE:
                raise SwiftError('already_active', 'Beneficiary is already active.')
            if row.status != BENE_PAUSED:
                raise SwiftError('invalid_status', 'Only a paused beneficiary can be resumed.')
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
        currency: Any = None,
        charge: Any = None,
        internal_account: Any = None,
        waive_fee: bool = False,
    ) -> Dict[str, Any]:
        self._require_send(actor_type)
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise SwiftError('swift_forbidden', 'Beneficiary does not belong to this customer.')
        ccy = normalize_currency(currency or bene.default_currency)
        fx = self.quote_fx(amount, ccy)
        debit = parse_money(fx.debit_usd)
        self._assert_usd(debit)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        charge_code = normalize_charge(charge or bene.default_charge)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(charge_code, waived=staff_waive, table=self.policy.charge_fees)
        now = float(self.clock())
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        clock = self.calendar.snapshot(now)
        mt103 = compose_mt103(
            reference='PREVIEW',
            value_date=clock['cycle_date'],
            currency=ccy,
            amount=parse_money(fx.amount, allow_zero=True),
            ordering_name=owner_userid,
            beneficiary_name=bene.legal_name,
            beneficiary_iban=mask_iban(bene.iban),
            beneficiary_account=last4(bene.account_number) if not bene.iban else '',
            beneficiary_bic=bene.bic,
            intermediary_bic=bene.intermediary_bic,
            charge=charge_code,
            purpose='other',
        )
        return {
            'currency': ccy,
            'amount': fx.amount,
            'rate': fx.rate,
            'debit_usd': fx.debit_usd,
            'fee': money_str(fee),
            'total_usd': money_str(debit + fee),
            'charge': charge_code,
            'internal_account': account,
            'beneficiary': bene.to_dict(),
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(debit),
            'clock': clock,
            'mt103': mt103,
        }

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        bene: SwiftBeneficiary,
        account: str,
        fx: FxQuote,
        fee: Decimal,
        charge: str,
        purpose: str,
        memo: str,
        trace_id: str,
        ofac: ScreenResult,
        waive_fee: bool,
    ) -> SwiftTransfer:
        now = float(self.clock())
        value = self.calendar.cycle_date(now)
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        debit = parse_money(fx.debit_usd)
        if ofac.hit:
            status = SWIFT_HELD
        elif self._needs_dual_control(debit):
            status = SWIFT_PENDING
        elif self.calendar.snapshot(now)['after_cutoff']:
            status = SWIFT_QUEUED
        else:
            status = SWIFT_SENT
        wire = SwiftTransfer(
            wire_id=uuid.uuid4().hex,
            trace_id=trace_id,
            beneficiary_id=bene.beneficiary_id,
            userid=owner_userid,
            internal_account=account,
            currency=fx.currency,
            amount=fx.amount,
            rate=fx.rate,
            debit_usd=fx.debit_usd,
            fee=money_str(fee),
            fee_status=fee_status,
            charge=charge,
            nickname=bene.nickname,
            legal_name=bene.legal_name,
            country=bene.country,
            bic=bene.bic,
            iban_masked=mask_iban(bene.iban),
            account_last4=last4(bene.iban or bene.account_number),
            intermediary_bic=bene.intermediary_bic,
            purpose=purpose,
            memo=memo,
            status=status,
            uetr='',
            mur='',
            value_date=value,
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
        )
        if status == SWIFT_SENT:
            self._transmit(wire, actor=actor)
        return wire

    def _transmit(self, wire: SwiftTransfer, *, actor: str) -> SwiftTransfer:
        now = float(self.clock())
        uetr = compose_uetr(self.uetr_fn())
        debit = parse_money(wire.debit_usd)
        fee = parse_money(wire.fee, allow_zero=True)
        remark = 'swift to %s' % wire.nickname
        status = SWIFT_SENT
        fail_note = ''
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(wire.internal_account, money_str(debit), remark)
            except Exception as exc:
                status = SWIFT_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = SWIFT_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = SWIFT_FAILED
                    fail_note = str(result)[:240]
        if status == SWIFT_SENT and fee > 0 and wire.fee_status != FEE_WAIVED and self.debit_fn is not None:
            try:
                fee_result = self.debit_fn(wire.internal_account, money_str(fee), 'swift fee %s' % uetr[:8])
            except Exception:
                wire.fee_status = FEE_NSF
            else:
                kind = _classify_money_result(fee_result)
                wire.fee_status = FEE_COLLECTED if kind == 'ok' else FEE_NSF
        elif status == SWIFT_SENT and (fee == 0 or wire.fee_status == FEE_WAIVED):
            wire.fee_status = FEE_WAIVED if wire.fee_status == FEE_WAIVED or fee == 0 else wire.fee_status
        wire.status = status
        wire.updated_at = now
        if status == SWIFT_SENT:
            wire.uetr = uetr
            wire.sent_at = now
            wire.releaser = str(actor)
        else:
            wire.uetr = ''
            wire.note = fail_note
        return wire

    def originate(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        beneficiary_id: Any,
        amount: Any,
        currency: Any = None,
        charge: Any = None,
        internal_account: Any = None,
        purpose: Any = 'other',
        memo: Any = '',
        trace_id: Any = None,
        waive_fee: bool = False,
    ) -> Tuple[SwiftTransfer, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise SwiftError('swift_forbidden', 'Not allowed to originate SWIFT wires for this customer.')
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise SwiftError('swift_forbidden', 'Beneficiary does not belong to this customer.')
        ccy = normalize_currency(currency or bene.default_currency)
        fx = self.quote_fx(amount, ccy)
        debit = parse_money(fx.debit_usd)
        self._assert_usd(debit)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        charge_code = normalize_charge(charge or bene.default_charge)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(charge_code, waived=staff_waive, table=self.policy.charge_fees)
        trace = normalize_id(trace_id)
        existing = self.store.get_wire_by_trace(trace)
        if existing is not None:
            return existing, False
        if len(self.store.list_wires(owner_userid)) >= self.policy.max_wires:
            raise SwiftError('wire_limit', 'SWIFT history limit reached.')
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        wire = self._place(
            owner_userid=owner_userid,
            actor=actor,
            bene=bene,
            account=account,
            fx=fx,
            fee=fee,
            charge=charge_code,
            purpose=normalize_purpose(purpose),
            memo=normalize_note(memo, limit=140),
            trace_id=trace,
            ofac=ofac,
            waive_fee=staff_waive,
        )
        stored = self.store.put_wire(wire)
        if stored.wire_id != wire.wire_id:
            return stored, False
        if stored.status == SWIFT_NSF:
            raise SwiftError('nsf', 'Insufficient funds for SWIFT wire.', wire=stored)
        if stored.status == SWIFT_FAILED:
            raise SwiftError('failed', 'SWIFT debit did not complete.', wire=stored)
        return stored, True

    def get_wire(self, *, wire_id: str, actor: str, actor_type: str) -> SwiftTransfer:
        self._require_enabled()
        row = self.store.get_wire(wire_id)
        if row is None:
            raise SwiftError('wire_not_found', 'SWIFT wire not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise SwiftError('swift_forbidden', 'Not allowed to view this wire.')
        return row

    def cancel_wire(
        self,
        *,
        wire_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> SwiftTransfer:
        self._require_send(actor_type)
        wire = self.get_wire(wire_id=wire_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and wire.userid != actor:
            raise SwiftError('swift_forbidden', 'Not allowed to cancel this wire.')
        if wire.status not in CANCELABLE:
            raise SwiftError('not_cancelable', 'Only held, queued, or pending wires can be cancelled.')
        wire.status = SWIFT_CANCELLED
        wire.actor = str(actor)
        wire.updated_at = float(self.clock())
        wire.note = normalize_note(note)
        self.store.update_wire(wire)
        return wire

    def waive_fee(
        self,
        *,
        wire_id: str,
        actor: str,
        actor_type: str,
    ) -> SwiftTransfer:
        self._require_staff(actor_type)
        wire = self.get_wire(wire_id=wire_id, actor=actor, actor_type=actor_type)
        if wire.status not in CANCELABLE:
            raise SwiftError('invalid_status', 'Fee can only be waived before the wire is sent.')
        wire.fee = money_str(Decimal('0.00'))
        wire.fee_status = FEE_WAIVED
        wire.actor = str(actor)
        wire.updated_at = float(self.clock())
        self.store.update_wire(wire)
        return wire

    def override_ofac(
        self,
        *,
        wire_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> SwiftTransfer:
        self._require_staff(actor_type)
        wire = self.get_wire(wire_id=wire_id, actor=actor, actor_type=actor_type)
        if wire.status != SWIFT_HELD:
            raise SwiftError('invalid_status', 'Only an OFAC hold can be overridden.')
        now = float(self.clock())
        wire.ofac_hit = 0
        wire.note = normalize_note(note) or 'ofac override'
        wire.actor = str(actor)
        wire.updated_at = now
        debit = parse_money(wire.debit_usd)
        if self._needs_dual_control(debit):
            wire.status = SWIFT_PENDING
        elif self.calendar.snapshot(now)['after_cutoff']:
            wire.status = SWIFT_QUEUED
            wire.value_date = self.calendar.cycle_date(now)
        else:
            self._transmit(wire, actor=actor)
        self.store.update_wire(wire)
        if wire.status == SWIFT_NSF:
            raise SwiftError('nsf', 'Insufficient funds for SWIFT wire.', wire=wire)
        if wire.status == SWIFT_FAILED:
            raise SwiftError('failed', 'SWIFT debit did not complete.', wire=wire)
        return wire

    def release_wire(
        self,
        *,
        wire_id: str,
        actor: str,
        actor_type: str,
    ) -> SwiftTransfer:
        self._require_staff(actor_type)
        wire = self.get_wire(wire_id=wire_id, actor=actor, actor_type=actor_type)
        if wire.status == SWIFT_HELD:
            raise SwiftError('ofac_hold', 'OFAC hold must be overridden before release.')
        if wire.status not in {SWIFT_PENDING, SWIFT_QUEUED}:
            raise SwiftError('not_releasable', 'Only queued or pending wires can be released.')
        debit = parse_money(wire.debit_usd)
        if (
            wire.status == SWIFT_PENDING
            and wire.actor
            and str(actor) == str(wire.actor)
            and debit >= self.policy.dual_control_threshold
        ):
            raise SwiftError('same_approver', 'A different employee must release this wire.')
        now = float(self.clock())
        if wire.status == SWIFT_QUEUED or self.calendar.snapshot(now)['after_cutoff']:
            if self.calendar.snapshot(now)['after_cutoff'] and wire.status != SWIFT_PENDING:
                wire.status = SWIFT_QUEUED
                wire.value_date = self.calendar.cycle_date(now)
                wire.updated_at = now
                self.store.update_wire(wire)
                return wire
        self._transmit(wire, actor=actor)
        self.store.update_wire(wire)
        if wire.status == SWIFT_NSF:
            raise SwiftError('nsf', 'Insufficient funds for SWIFT wire.', wire=wire)
        if wire.status == SWIFT_FAILED:
            raise SwiftError('failed', 'SWIFT debit did not complete.', wire=wire)
        return wire

    def reject_wire(
        self,
        *,
        wire_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> SwiftTransfer:
        self._require_staff(actor_type)
        wire = self.get_wire(wire_id=wire_id, actor=actor, actor_type=actor_type)
        if wire.status not in CANCELABLE:
            raise SwiftError('not_rejectable', 'Only held, queued, or pending wires can be rejected.')
        wire.status = SWIFT_REJECTED
        wire.reason = normalize_note(reason, limit=40)
        wire.note = normalize_note(note)
        wire.actor = str(actor)
        wire.updated_at = float(self.clock())
        self.store.update_wire(wire)
        return wire

    def complete_wire(
        self,
        *,
        wire_id: str,
        actor: str,
        actor_type: str,
    ) -> SwiftTransfer:
        self._require_staff(actor_type)
        wire = self.get_wire(wire_id=wire_id, actor=actor, actor_type=actor_type)
        if wire.status == SWIFT_COMPLETED:
            raise SwiftError('already_completed', 'Wire is already completed.')
        if wire.status != SWIFT_SENT:
            raise SwiftError('not_completable', 'Only sent wires can be completed.')
        now = float(self.clock())
        seq = self.store.next_mur_sequence(wire.value_date)
        wire.mur = compose_mur(wire.value_date, self.policy.source_bic, seq)
        wire.status = SWIFT_COMPLETED
        wire.completed_at = now
        wire.updated_at = now
        wire.actor = str(actor)
        self.store.update_wire(wire)
        return wire

    def recall_wire(
        self,
        *,
        wire_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> SwiftTransfer:
        self._require_staff(actor_type)
        wire = self.get_wire(wire_id=wire_id, actor=actor, actor_type=actor_type)
        if wire.status == SWIFT_RECALLED:
            raise SwiftError('already_recalled', 'Wire is already recalled.')
        if wire.status == SWIFT_COMPLETED:
            raise SwiftError('already_completed', 'Completed wires cannot be recalled.')
        if wire.status != SWIFT_SENT:
            raise SwiftError('not_recallable', 'Only sent wires can be recalled.')
        remark = normalize_note(note) or ('swift recalled from %s' % wire.nickname)
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(wire.internal_account, wire.debit_usd, remark)
            except Exception as exc:
                raise SwiftError('recall_failed', 'Recall credit failed.', wire=wire) from exc
            if _classify_money_result(result) != 'ok':
                raise SwiftError('recall_failed', 'Recall credit failed.', wire=wire)
            fee = parse_money(wire.fee, allow_zero=True)
            if fee > 0 and wire.fee_status == FEE_COLLECTED:
                self.credit_fn(wire.internal_account, money_str(fee), 'swift fee recalled %s' % (wire.uetr[:8] or wire.wire_id[:8]))
        now = float(self.clock())
        wire.status = SWIFT_RECALLED
        wire.recalled_at = now
        wire.updated_at = now
        wire.actor = str(actor)
        wire.note = remark
        self.store.update_wire(wire)
        return wire

    def run_due(self, userid: Optional[str] = None) -> List[SwiftTransfer]:
        now = float(self.clock())
        today = self.calendar.local_dt(now).date().strftime('%Y%m%d')
        after = self.calendar.snapshot(now)['after_cutoff']
        changed: List[SwiftTransfer] = []
        for wire in self.store.list_wires(userid):
            if wire.status != SWIFT_QUEUED:
                continue
            if wire.value_date > today:
                continue
            if after and wire.value_date == today:
                continue
            debit = parse_money(wire.debit_usd)
            if self._needs_dual_control(debit):
                wire.status = SWIFT_PENDING
                wire.updated_at = now
                self.store.update_wire(wire)
                changed.append(wire)
                continue
            self._transmit(wire, actor=wire.actor)
            self.store.update_wire(wire)
            changed.append(wire)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.run_due(userid)
        beneficiaries = self.store.list_beneficiaries(userid)
        wires = self.store.list_wires(userid)
        sent_ytd = Decimal('0.00')
        fee_ytd = Decimal('0.00')
        recalled = Decimal('0.00')
        for row in wires:
            amount = parse_money(row.debit_usd, allow_zero=True)
            if row.status in {SWIFT_SENT, SWIFT_COMPLETED}:
                sent_ytd += amount
                if row.fee_status == FEE_COLLECTED:
                    fee_ytd += parse_money(row.fee, allow_zero=True)
            elif row.status == SWIFT_RECALLED:
                recalled += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'fees': {key: money_str(value) for key, value in self.policy.charge_fees.items()},
            'dual_control': money_str(self.policy.dual_control_threshold),
            'currencies': sorted(SUPPORTED_CURRENCIES),
            'clock': self.calendar.snapshot(now),
            'beneficiaries': [row.to_dict() for row in beneficiaries[:40]],
            'wires': [row.to_dict() for row in wires[:40]],
            'ytd_sent': money_str(sent_ytd),
            'ytd_fees': money_str(fee_ytd),
            'recalled_ytd': money_str(recalled),
            'active_count': sum(1 for row in beneficiaries if row.status == BENE_ACTIVE),
            'open_count': sum(1 for row in wires if row.status in OPEN_SWIFTS),
        }


_SERVICE: Optional[SwiftService] = None


def set_service(service: Optional[SwiftService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[SwiftService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('SWIFT_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemorySwiftStore()
    path = os.environ.get('SWIFT_DB', DEFAULT_STORE_PATH)
    return SqliteSwiftStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[Target2Calendar] = None,
    fx: Optional[FxBook] = None,
) -> SwiftService:
    if store is None:
        store = default_store()
    return SwiftService(
        SwiftPolicy.from_env(),
        store,
        clock=clock,
        debit_fn=debit_fn,
        credit_fn=credit_fn,
        accounts_fn=accounts_fn,
        screen_fn=screen_fn,
        calendar=calendar,
        fx=fx,
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
        'wire_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_completed': 409,
        'already_recalled': 409,
        'nsf': 409,
        'failed': 409,
        'recall_failed': 409,
        'swift_forbidden': 403,
        'swift_disabled': 403,
        'beneficiary_paused': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_cancelable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_completable': 403,
        'not_recallable': 403,
        'beneficiary_not_found': 404,
        'wire_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_iban': 400,
        'invalid_bic': 400,
        'invalid_country': 400,
        'invalid_currency': 400,
        'invalid_charge': 400,
        'invalid_rate': 400,
        'invalid_address': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_uetr': 400,
        'invalid_mur': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_beneficiary': 400,
        'missing_wire': 400,
    }.get(code, 400)


def _error_body(exc: SwiftError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('wire') is not None:
        body['wire'] = exc.extra['wire'].to_dict()
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
    except SwiftError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list_swifts(service: SwiftService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Swift': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_add_beneficiary(service: SwiftService):
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
            bic=values.get('bic') or values.get('swift'),
            iban=values.get('iban'),
            account_number=values.get('account_number') or values.get('external_account'),
            intermediary_bic=values.get('intermediary_bic') or values.get('correspondent'),
            street=values.get('street') or values.get('address'),
            city=values.get('city'),
            country=values.get('country'),
            postal=values.get('postal') or values.get('zip'),
            default_account=values.get('default_account') or values.get('account') or values.get('from_account'),
            default_currency=values.get('default_currency') or values.get('currency') or 'EUR',
            default_charge=values.get('default_charge') or values.get('charge') or 'SHA',
        )
        return jsonify({
            'message': 'SWIFT beneficiary added',
            'beneficiary': row.to_dict(),
            'Swift': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _bene_status_route(service: SwiftService, status: str, ok_message: str):
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
            'Swift': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_quote(service: SwiftService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}

    def _run():
        quote = service.quote_fx(values.get('amount'), values.get('currency') or 'EUR')
        return jsonify({'quote': quote.to_dict()}), 200

    return _handle_errors(_run)


def handle_preview(service: SwiftService):
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
            currency=values.get('currency'),
            charge=values.get('charge'),
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({'preview': preview, 'Swift': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: SwiftService):
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
        wire, created = service.originate(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            beneficiary_id=beneficiary_id,
            amount=values.get('amount'),
            currency=values.get('currency'),
            charge=values.get('charge'),
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({
            'message': 'SWIFT originated' if created else 'SWIFT already posted',
            'wire': wire.to_dict(),
            'Swift': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: SwiftService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    wire_id = str(values.get('wire_id') or '').strip()
    if not wire_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_wire'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        wire = service.cancel_wire(
            wire_id=wire_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': 'SWIFT cancelled',
            'wire': wire.to_dict(),
            'Swift': service.snapshot(wire.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_swift_route(service: SwiftService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    wire_id = str(values.get('wire_id') or '').strip()
    if not wire_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_wire'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'release':
            wire = service.release_wire(wire_id=wire_id, actor=userid, actor_type=actor_type)
            message = 'SWIFT released'
        elif action == 'reject':
            wire = service.reject_wire(
                wire_id=wire_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'SWIFT rejected'
        elif action == 'complete':
            wire = service.complete_wire(wire_id=wire_id, actor=userid, actor_type=actor_type)
            message = 'SWIFT completed'
        elif action == 'recall':
            wire = service.recall_wire(
                wire_id=wire_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'SWIFT recalled'
        elif action == 'override':
            wire = service.override_ofac(
                wire_id=wire_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'waive':
            wire = service.waive_fee(wire_id=wire_id, actor=userid, actor_type=actor_type)
            message = 'SWIFT fee waived'
        else:
            raise SwiftError('invalid_status', 'Unknown SWIFT action.')
        return jsonify({
            'message': message,
            'wire': wire.to_dict(),
            'Swift': service.snapshot(wire.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: SwiftService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Swift': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_swift_routes(app, service: SwiftService) -> None:
    @app.route('/listSwifts', methods=['POST', 'GET'])
    def list_swifts_route():
        return handle_list_swifts(service)

    @app.route('/listSwiftBeneficiaries', methods=['POST', 'GET'])
    def list_swift_beneficiaries_route():
        return handle_list_swifts(service)

    @app.route('/addSwiftBeneficiary', methods=['POST', 'GET'])
    def add_swift_beneficiary_route():
        return handle_add_beneficiary(service)

    @app.route('/pauseSwiftBeneficiary', methods=['POST', 'GET'])
    def pause_swift_beneficiary_route():
        return _bene_status_route(service, BENE_PAUSED, 'SWIFT beneficiary paused')

    @app.route('/resumeSwiftBeneficiary', methods=['POST', 'GET'])
    def resume_swift_beneficiary_route():
        return _bene_status_route(service, BENE_ACTIVE, 'SWIFT beneficiary resumed')

    @app.route('/archiveSwiftBeneficiary', methods=['POST', 'GET'])
    def archive_swift_beneficiary_route():
        return _bene_status_route(service, BENE_ARCHIVED, 'SWIFT beneficiary archived')

    @app.route('/quoteSwiftFx', methods=['POST', 'GET'])
    def quote_swift_fx_route():
        return handle_quote(service)

    @app.route('/previewSwift', methods=['POST', 'GET'])
    def preview_swift_route():
        return handle_preview(service)

    @app.route('/sendSwift', methods=['POST', 'GET'])
    def send_swift_route():
        return handle_send(service)

    @app.route('/cancelSwift', methods=['POST', 'GET'])
    def cancel_swift_route():
        return handle_cancel(service)

    @app.route('/releaseSwift', methods=['POST', 'GET'])
    def release_swift_route():
        return _staff_swift_route(service, 'release')

    @app.route('/rejectSwift', methods=['POST', 'GET'])
    def reject_swift_route():
        return _staff_swift_route(service, 'reject')

    @app.route('/completeSwift', methods=['POST', 'GET'])
    def complete_swift_route():
        return _staff_swift_route(service, 'complete')

    @app.route('/recallSwift', methods=['POST', 'GET'])
    def recall_swift_route():
        return _staff_swift_route(service, 'recall')

    @app.route('/overrideSwiftOfac', methods=['POST', 'GET'])
    def override_swift_ofac_route():
        return _staff_swift_route(service, 'override')

    @app.route('/waiveSwiftFee', methods=['POST', 'GET'])
    def waive_swift_fee_route():
        return _staff_swift_route(service, 'waive')

    @app.route('/runDueSwifts', methods=['POST', 'GET'])
    def run_due_swifts_route():
        return handle_run_due(service)
