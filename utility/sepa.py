"""SEPA Credit Transfer (SCT) and SEPA Instant (SCT Inst) origination.

Customers send euro payments to SEPA-zone creditors identified by IBAN
(and optional BIC). Independent of domestic Fedwire (PR #73), SWIFT MT103
(PR #78), ACH linking (PR #68), bill-pay ACH (PR #66), inbound payroll
(PR #64), the in-bank payee allowlist (PR #26), and scheduled internal
transfers (PR #36). Existing `/fundTransfer`, `/withdrawAmount`, and
`/sendWire` stay unchanged. `Customers.debit_request` / `credit_request`
still write `debited` / `direct deposited` unless a remark is supplied here.

Foundations (reusable beyond this screen):
- IBAN ISO 13616 mod-97 checksum restricted to the EPC SEPA zone
- BIC / SWIFT ISO 9362 validation (optional; IBAN-only routing allowed)
- ISO 11649 RF creditor-reference check digits
- TARGET2 business-day / cutoff clock (SCT only; Instant is 24/7)
- EURUSD quote book converting EUR instructed amount to USD debit
- ISO 20022 pain.001 field map (EndToEndId, SLEV, SCT vs INST)
- Dual-control release on USD equivalent

Reuses the local SDN-style OFAC screen from `utility.wire`. Stores are
pluggable (memory for tests, sqlite WAL for restart-safe default). Full
IBAN never appears in to_dict / snapshots (`DE****3000`).
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
SEPA_HELD = 'held'
SEPA_QUEUED = 'queued'
SEPA_PENDING = 'pending_release'
SEPA_SENT = 'sent'
SEPA_COMPLETED = 'completed'
SEPA_REJECTED = 'rejected'
SEPA_CANCELLED = 'cancelled'
SEPA_RECALLED = 'recalled'
SEPA_NSF = 'nsf'
SEPA_FAILED = 'failed'
SEPA_STATUSES = frozenset({
    SEPA_HELD, SEPA_QUEUED, SEPA_PENDING, SEPA_SENT, SEPA_COMPLETED,
    SEPA_REJECTED, SEPA_CANCELLED, SEPA_RECALLED, SEPA_NSF, SEPA_FAILED,
})
OPEN_SEPAS = frozenset({SEPA_HELD, SEPA_QUEUED, SEPA_PENDING, SEPA_SENT})
CANCELABLE = frozenset({SEPA_HELD, SEPA_QUEUED, SEPA_PENDING})
FEE_NONE = 'none'
FEE_COLLECTED = 'collected'
FEE_WAIVED = 'waived'
FEE_NSF = 'nsf'
SCHEME_SCT = 'sct'
SCHEME_INSTANT = 'sct_inst'
SCHEMES = frozenset({SCHEME_SCT, SCHEME_INSTANT})
SCHEME_ALIASES = {
    'sct': SCHEME_SCT, 'sepa': SCHEME_SCT, 'credit': SCHEME_SCT,
    'standard': SCHEME_SCT, 'trf': SCHEME_SCT,
    'sct_inst': SCHEME_INSTANT, 'sctinst': SCHEME_INSTANT, 'inst': SCHEME_INSTANT,
    'instant': SCHEME_INSTANT, 'sct-inst': SCHEME_INSTANT, 'rtp': SCHEME_INSTANT,
}
PURPOSES = frozenset({'family', 'goods', 'payroll', 'tax', 'loan', 'rent', 'other'})
PURPOSE_ALIASES = {
    'personal': 'family', 'gift': 'family', 'support': 'family',
    'invoice': 'goods', 'purchase': 'goods', 'vendor': 'goods',
    'salary': 'payroll', 'wage': 'payroll',
    'irs': 'tax', 'taxes': 'tax',
    'mortgage': 'loan', 'housing': 'rent',
}
PURPOSE_ISO = {
    'family': 'FAMI', 'goods': 'GDDS', 'payroll': 'SALA',
    'tax': 'TAXS', 'loan': 'LOAN', 'rent': 'RENT', 'other': 'OTHR',
}
# EPC SEPA scheme countries (EU + EEA + CH/MC/SM/AD/VA/GB/GI) with ISO 13616 lengths.
SEPA_IBAN_LENGTHS = {
    'AD': 24, 'AT': 20, 'BE': 16, 'BG': 22, 'CH': 21, 'CY': 28, 'CZ': 24,
    'DE': 22, 'DK': 18, 'EE': 20, 'ES': 24, 'FI': 18, 'FR': 27, 'GB': 22,
    'GI': 23, 'GR': 27, 'HR': 21, 'HU': 28, 'IE': 22, 'IS': 26, 'IT': 27,
    'LI': 21, 'LT': 20, 'LU': 20, 'LV': 21, 'MC': 27, 'MT': 31, 'NL': 18,
    'NO': 15, 'PL': 28, 'PT': 25, 'RO': 24, 'SE': 24, 'SI': 19, 'SK': 24,
    'SM': 27, 'VA': 22,
}
SEPA_COUNTRIES = frozenset(SEPA_IBAN_LENGTHS)
SCHEME_FEES = {
    SCHEME_SCT: Decimal('15.00'),
    SCHEME_INSTANT: Decimal('25.00'),
}
DEFAULT_EURUSD = Decimal('1.080000')
RATE_QUANTUM = Decimal('0.000001')
MONEY_QUANTUM = Decimal('0.01')
BIC_RE = re.compile(r'^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$')
E2E_RE = re.compile(r'^[A-Za-z0-9/\-?:().,\'+ ]{1,35}$')
DEFAULT_STORE_PATH = 'SystemLogs/sepa.sqlite'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)


class SepaError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def normalize_note(value: Any, *, limit: int = 140) -> str:
    return str(value or '').strip()[:limit]


def normalize_id(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return uuid.uuid4().hex
    return text[:120]


def normalize_nickname(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise SepaError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 70):
        raise SepaError('invalid_name', 'Creditor legal name must be 2-70 characters.')
    return text


def normalize_city(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise SepaError('invalid_address', 'City must be 2-40 characters.')
    return text


def normalize_country(value: Any) -> str:
    text = str(value or '').strip().upper()
    if text not in SEPA_COUNTRIES:
        raise SepaError('invalid_country', 'Country must be a SEPA-zone ISO 3166-1 code.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise SepaError('invalid_purpose', 'Unknown SEPA purpose.')
    return text


def normalize_scheme(value: Any, *, default: str = SCHEME_SCT) -> str:
    text = str(value or default).strip().lower().replace(' ', '_')
    text = SCHEME_ALIASES.get(text, text)
    if text not in SCHEMES:
        raise SepaError('invalid_scheme', 'Scheme must be sct or sct_inst.')
    return text


def normalize_source(value: Any, *, default: str = 'KONOHA01') -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or default).upper())
    if not text:
        text = default
    return (text + 'XXXXXXXX')[:8]


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
            raise SepaError('invalid_iban', 'IBAN is required.')
        return ''
    country = text[:2]
    if country not in SEPA_COUNTRIES:
        raise SepaError('not_sepa_country', 'IBAN country is outside the SEPA zone.')
    expected = SEPA_IBAN_LENGTHS[country]
    if len(text) != expected:
        raise SepaError('invalid_iban', 'IBAN length does not match country %s.' % country)
    if not iban_check_digit_ok(text):
        raise SepaError('invalid_iban', 'IBAN failed mod-97 checksum.')
    return text


def mask_iban(iban: str) -> str:
    if not iban:
        return ''
    if len(iban) <= 6:
        return iban[:2] + '****'
    return iban[:2] + '****' + iban[-4:]


def iban_country(iban: str) -> str:
    return (iban or '')[:2]


def normalize_bic(value: Any, *, required: bool = False) -> str:
    text = re.sub(r'[^A-Za-z0-9]', '', str(value or '')).upper()
    if not text:
        if required:
            raise SepaError('invalid_bic', 'BIC is required.')
        return ''
    if len(text) == 8:
        text = text + 'XXX'
    if not BIC_RE.match(text):
        raise SepaError('invalid_bic', 'BIC must be 8 or 11 characters (ISO 9362).')
    country = text[4:6]
    if country not in SEPA_COUNTRIES:
        raise SepaError('invalid_bic', 'BIC country is outside the SEPA zone.')
    if text[6] == '0':
        raise SepaError('invalid_bic', 'BIC location code cannot start with 0.')
    return text


def bic8(value: str) -> str:
    return (value or '')[:8]


def bic_country(value: str) -> str:
    text = str(value or '')
    if len(text) >= 6:
        return text[4:6]
    return ''


def rf_check_digit_ok(reference: str) -> bool:
    """ISO 11649 RF creditor reference uses the same mod-97 rearrange as IBAN."""
    if len(reference) < 5 or len(reference) > 25 or not reference.startswith('RF'):
        return False
    if not reference[2:4].isdigit():
        return False
    rearranged = reference[4:] + reference[:4]
    numeric = []
    for ch in rearranged:
        if ch.isdigit():
            numeric.append(ch)
        elif ch.isalpha():
            numeric.append(str(ord(ch) - 55))
        else:
            return False
    return iban_mod97(''.join(numeric)) == 1


def normalize_creditor_ref(value: Any) -> str:
    text = re.sub(r'[^A-Za-z0-9]', '', str(value or '')).upper()
    if not text:
        return ''
    if not text.startswith('RF'):
        raise SepaError('invalid_reference', 'Creditor reference must be an ISO 11649 RF number.')
    if not rf_check_digit_ok(text):
        raise SepaError('invalid_reference', 'Creditor reference failed mod-97 checksum.')
    return text


def normalize_end_to_end(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    if not E2E_RE.match(text):
        raise SepaError('invalid_end_to_end', 'End-to-end id must be 1-35 ISO 20022 characters.')
    return text


def compose_end_to_end_id(scheme: str, cycle_date: str, sequence: int) -> str:
    """ISO 20022 EndToEndId: {SCT|INST}{YYYYMMDD}{6-digit sequence}."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise SepaError('invalid_end_to_end', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise SepaError('invalid_end_to_end', 'End-to-end sequence out of range.')
    prefix = 'INST' if scheme == SCHEME_INSTANT else 'SCT'
    return '%s%s%06d' % (prefix, day, seq)


def compose_tx_id(cycle_date: str, sequence: int, *, source: str = 'KONOHA01') -> str:
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise SepaError('invalid_end_to_end', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise SepaError('invalid_end_to_end', 'TxId sequence out of range.')
    return '%s%s%06d' % (normalize_source(source), day, seq)


def compose_pain001(
    *,
    msg_id: str,
    instr_id: str,
    end_to_end_id: str,
    scheme: str,
    amount_eur: str,
    creditor_name: str,
    iban: str,
    bic: str = '',
    remittance: str = '',
    purpose: str = 'other',
    creditor_ref: str = '',
) -> Dict[str, Any]:
    """ISO 20022 pain.001 Customer Credit Transfer field map (not XML)."""
    scheme = normalize_scheme(scheme)
    payload = {
        'MsgId': str(msg_id)[:35],
        'PmtInfId': str(instr_id)[:35],
        'InstrId': str(instr_id)[:35],
        'EndToEndId': str(end_to_end_id)[:35],
        'SvcLvl': 'SEPA',
        'LclInstrm': 'INST' if scheme == SCHEME_INSTANT else 'TRF',
        'InstdAmt': {'Ccy': 'EUR', 'Value': amount_eur},
        'ChrgBr': 'SLEV',
        'Cdtr': {'Nm': creditor_name[:70]},
        'CdtrAcct': {'IBAN': iban},
        'Purp': PURPOSE_ISO.get(purpose, 'OTHR'),
        'RmtInf': {},
    }
    if bic:
        payload['CdtrAgt'] = {'BIC': bic}
    if creditor_ref:
        payload['RmtInf']['Strd'] = {'CdtrRefInf': {'Ref': creditor_ref}}
    elif remittance:
        payload['RmtInf']['Ustrd'] = remittance[:140]
    if not payload['RmtInf']:
        del payload['RmtInf']
    return payload


def parse_rate(value: Any) -> Decimal:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise SepaError('invalid_rate', 'FX rate is required.')
    try:
        rate = Decimal(str(value).strip().replace(',', ''))
    except (InvalidOperation, ValueError):
        raise SepaError('invalid_rate', 'FX rate is invalid.') from None
    if not rate.is_finite() or rate <= 0:
        raise SepaError('invalid_rate', 'FX rate must be positive.')
    return rate.quantize(RATE_QUANTUM, rounding=ROUND_HALF_EVEN)


def compute_fee(scheme: str, *, waived: bool = False, table: Optional[Dict[str, Decimal]] = None) -> Decimal:
    if waived:
        return Decimal('0.00')
    fees = table or SCHEME_FEES
    return fees.get(scheme, SCHEME_FEES[SCHEME_SCT]).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


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
    """TARGET2 business-day + customer cutoff. SCT value date is T+1. Instant ignores this."""

    def __init__(
        self,
        *,
        cutoff_hour: int = 16,
        cutoff_minute: int = 0,
        tz_offset_hours: int = 2,
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

    def value_date(self, ts: float, *, scheme: str = SCHEME_SCT) -> date:
        local = self.local_dt(ts)
        day = local.date()
        if scheme == SCHEME_INSTANT:
            return day
        if not self.is_business_day(day) or self.is_after_cutoff(ts):
            day = self.next_business_day(day)
        return self.next_business_day(day)

    def cycle_date(self, ts: float, *, scheme: str = SCHEME_SCT) -> str:
        return self.value_date(ts, scheme=scheme).strftime('%Y%m%d')

    def snapshot(self, ts: float, *, scheme: str = SCHEME_SCT) -> Dict[str, Any]:
        local = self.local_dt(ts)
        value = self.value_date(ts, scheme=scheme)
        after = False if scheme == SCHEME_INSTANT else (
            self.is_after_cutoff(ts) or not self.is_business_day(local.date())
        )
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'cutoff': '%02d:%02d' % (self.cutoff_hour, self.cutoff_minute),
            'after_cutoff': after,
            'business_day': self.is_business_day(local.date()),
            'value_date': value.isoformat(),
            'cycle_date': value.strftime('%Y%m%d'),
            'calendar': '24x7' if scheme == SCHEME_INSTANT else 'TARGET2',
            'scheme': scheme,
        }


@dataclass
class EurQuote:
    currency: str
    amount_eur: str
    rate: str
    debit_usd: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'currency': self.currency,
            'amount_eur': self.amount_eur,
            'rate': self.rate,
            'debit_usd': self.debit_usd,
        }


class EurUsdBook:
    """Single-pair EURUSD book. Instruction is always EUR; ledger debit is USD."""

    def __init__(self, rate: Optional[Decimal] = None) -> None:
        self.rate = parse_rate(rate if rate is not None else DEFAULT_EURUSD)

    def quote(self, amount_eur: Decimal) -> EurQuote:
        debit = (amount_eur * self.rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
        return EurQuote(
            currency='EUR',
            amount_eur=money_str(amount_eur),
            rate=str(self.rate),
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
class SepaPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    allow_credit: bool = False
    max_creditors: int = 12
    max_transfers: int = 120
    min_amount: Decimal = Decimal('1.00')
    max_amount: Decimal = Decimal('1000000.00')
    instant_max: Decimal = Decimal('100000.00')
    sct_fee: Decimal = Decimal('15.00')
    instant_fee: Decimal = Decimal('25.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    eurusd: Decimal = DEFAULT_EURUSD
    cutoff_hour: int = 16
    tz_offset_hours: int = 2
    source_id: str = 'KONOHA01'
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'SepaPolicy':
        extra = _env_list('SEPA_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        rate_raw = os.environ.get('SEPA_EURUSD')
        return cls(
            enabled=_env_bool('SEPA_ENABLED', True),
            customer_manage=_env_bool('SEPA_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('SEPA_CUSTOMER_SEND', True),
            allow_credit=_env_bool('SEPA_ALLOW_CREDIT', False),
            max_creditors=max(1, _env_int('SEPA_MAX_CREDITORS', 12)),
            max_transfers=max(1, _env_int('SEPA_MAX_TRANSFERS', 120)),
            min_amount=_env_money('SEPA_MIN_AMOUNT', '1.00'),
            max_amount=_env_money('SEPA_MAX_AMOUNT', '1000000.00'),
            instant_max=_env_money('SEPA_INSTANT_MAX', '100000.00'),
            sct_fee=_env_money('SEPA_SCT_FEE', '15.00'),
            instant_fee=_env_money('SEPA_INSTANT_FEE', '25.00'),
            dual_control_threshold=_env_money('SEPA_DUAL_CONTROL', '10000.00'),
            eurusd=parse_rate(rate_raw) if rate_raw else DEFAULT_EURUSD,
            cutoff_hour=max(0, min(23, _env_int('SEPA_CUTOFF_HOUR', 16))),
            tz_offset_hours=_env_int('SEPA_TZ_OFFSET', 2),
            source_id=normalize_source(os.environ.get('SEPA_SOURCE', 'KONOHA01')),
            watchlist=watch,
            extra_holidays=_env_list('SEPA_HOLIDAYS'),
        )

    def fee_table(self) -> Dict[str, Decimal]:
        return {SCHEME_SCT: self.sct_fee, SCHEME_INSTANT: self.instant_fee}


@dataclass
class SepaCreditor:
    creditor_id: str
    userid: str
    nickname: str
    legal_name: str
    iban: str
    bic: str
    country: str
    city: str
    creditor_ref: str
    default_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'creditor_id': self.creditor_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'iban_masked': mask_iban(self.iban),
            'iban_last4': last4(self.iban),
            'bic': self.bic,
            'country': self.country,
            'city': self.city,
            'creditor_ref': self.creditor_ref,
            'default_account': self.default_account,
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == BENE_ACTIVE,
            'paused': self.status == BENE_PAUSED,
            'archived': self.status == BENE_ARCHIVED,
        }


@dataclass
class SepaTransfer:
    transfer_id: str
    trace_id: str
    creditor_id: str
    userid: str
    internal_account: str
    amount: str
    debit_usd: str
    rate: str
    fee: str
    fee_status: str
    scheme: str
    nickname: str
    legal_name: str
    iban_masked: str
    bic: str
    purpose: str
    memo: str
    creditor_ref: str
    status: str
    end_to_end_id: str
    tx_id: str
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
            'transfer_id': self.transfer_id,
            'trace_id': self.trace_id,
            'creditor_id': self.creditor_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'amount': self.amount,
            'currency': 'EUR',
            'debit_usd': self.debit_usd,
            'rate': self.rate,
            'fee': self.fee,
            'fee_status': self.fee_status,
            'scheme': self.scheme,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'iban_masked': self.iban_masked,
            'bic': self.bic,
            'purpose': self.purpose,
            'memo': self.memo,
            'creditor_ref': self.creditor_ref,
            'status': self.status,
            'end_to_end_id': self.end_to_end_id,
            'tx_id': self.tx_id,
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
            'held': self.status == SEPA_HELD,
            'queued': self.status == SEPA_QUEUED,
            'pending_release': self.status == SEPA_PENDING,
            'sent': self.status == SEPA_SENT,
            'completed': self.status == SEPA_COMPLETED,
            'cancelable': self.status in CANCELABLE,
            'irrevocable': self.scheme == SCHEME_INSTANT and self.status in {SEPA_SENT, SEPA_COMPLETED},
        }


def _clone_creditor(row: SepaCreditor) -> SepaCreditor:
    return SepaCreditor(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_transfer(row: SepaTransfer) -> SepaTransfer:
    return SepaTransfer(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _creditor_from_row(row: Any) -> SepaCreditor:
    return SepaCreditor(
        creditor_id=row['creditor_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        iban=row['iban'],
        bic=row['bic'] or '',
        country=row['country'],
        city=row['city'],
        creditor_ref=row['creditor_ref'] or '',
        default_account=row['default_account'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
    )


def _transfer_from_row(row: Any) -> SepaTransfer:
    return SepaTransfer(
        transfer_id=row['transfer_id'],
        trace_id=row['trace_id'],
        creditor_id=row['creditor_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        amount=row['amount'],
        debit_usd=row['debit_usd'],
        rate=row['rate'],
        fee=row['fee'],
        fee_status=row['fee_status'],
        scheme=row['scheme'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        iban_masked=row['iban_masked'],
        bic=row['bic'] or '',
        purpose=row['purpose'],
        memo=row['memo'] or '',
        creditor_ref=row['creditor_ref'] or '',
        status=row['status'],
        end_to_end_id=row['end_to_end_id'] or '',
        tx_id=row['tx_id'] or '',
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


class MemorySepaStore:
    def __init__(self) -> None:
        self._creditors: Dict[str, SepaCreditor] = {}
        self._transfers: Dict[str, SepaTransfer] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_creditor(self, row: SepaCreditor) -> None:
        with self._lock:
            self._creditors[row.creditor_id] = row

    def get_creditor(self, creditor_id: str) -> Optional[SepaCreditor]:
        with self._lock:
            row = self._creditors.get(creditor_id)
            return _clone_creditor(row) if row else None

    def update_creditor(self, row: SepaCreditor) -> None:
        with self._lock:
            self._creditors[row.creditor_id] = row

    def list_creditors(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[SepaCreditor]:
        with self._lock:
            rows = [_clone_creditor(row) for row in self._creditors.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != BENE_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_creditor_by_nickname(self, userid: str, nickname: str) -> Optional[SepaCreditor]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._creditors.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_BENE:
                    return _clone_creditor(row)
        return None

    def find_creditor_by_fingerprint(self, userid: str, iban: str) -> Optional[SepaCreditor]:
        with self._lock:
            for row in self._creditors.values():
                if row.userid == userid and row.iban == iban and row.status in OPEN_BENE:
                    return _clone_creditor(row)
        return None

    def put_transfer(self, row: SepaTransfer) -> SepaTransfer:
        with self._lock:
            existing_id = self._by_trace.get(row.trace_id)
            if existing_id is not None:
                return self._transfers[existing_id]
            self._transfers[row.transfer_id] = row
            self._by_trace[row.trace_id] = row.transfer_id
            return row

    def update_transfer(self, row: SepaTransfer) -> None:
        with self._lock:
            self._transfers[row.transfer_id] = row

    def get_transfer(self, transfer_id: str) -> Optional[SepaTransfer]:
        with self._lock:
            row = self._transfers.get(transfer_id)
            return _clone_transfer(row) if row else None

    def get_transfer_by_trace(self, trace_id: str) -> Optional[SepaTransfer]:
        with self._lock:
            transfer_id = self._by_trace.get(trace_id)
            return _clone_transfer(self._transfers[transfer_id]) if transfer_id else None

    def list_transfers(self, userid: Optional[str] = None, creditor_id: Optional[str] = None) -> List[SepaTransfer]:
        with self._lock:
            rows = [_clone_transfer(row) for row in self._transfers.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if creditor_id is not None:
            rows = [row for row in rows if row.creditor_id == creditor_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def next_e2e_sequence(self, cycle_date: str) -> int:
        with self._lock:
            used = [
                row.end_to_end_id for row in self._transfers.values()
                if row.end_to_end_id and cycle_date in row.end_to_end_id
            ]
        return len(used) + 1


class SqliteSepaStore:
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
                CREATE TABLE IF NOT EXISTS creditors (
                    creditor_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    iban TEXT NOT NULL,
                    bic TEXT NOT NULL DEFAULT '',
                    country TEXT NOT NULL,
                    city TEXT NOT NULL,
                    creditor_ref TEXT NOT NULL DEFAULT '',
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
                CREATE TABLE IF NOT EXISTS transfers (
                    transfer_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    creditor_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    internal_account TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    debit_usd TEXT NOT NULL,
                    rate TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    fee_status TEXT NOT NULL,
                    scheme TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    iban_masked TEXT NOT NULL,
                    bic TEXT NOT NULL DEFAULT '',
                    purpose TEXT NOT NULL,
                    memo TEXT NOT NULL DEFAULT '',
                    creditor_ref TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    end_to_end_id TEXT NOT NULL DEFAULT '',
                    tx_id TEXT NOT NULL DEFAULT '',
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

    def put_creditor(self, row: SepaCreditor) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO creditors (
                    creditor_id, userid, nickname, legal_name, iban, bic, country,
                    city, creditor_ref, default_account, status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.creditor_id, row.userid, row.nickname, row.legal_name, row.iban,
                    row.bic, row.country, row.city, row.creditor_ref, row.default_account,
                    row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_creditor(self, creditor_id: str) -> Optional[SepaCreditor]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM creditors WHERE creditor_id = ?', (creditor_id,),
            ).fetchone()
        return _creditor_from_row(row) if row else None

    def update_creditor(self, row: SepaCreditor) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE creditors SET nickname=?, legal_name=?, iban=?, bic=?, country=?,
                    city=?, creditor_ref=?, default_account=?, status=?, actor=?, updated_at=?
                WHERE creditor_id=?
                """,
                (
                    row.nickname, row.legal_name, row.iban, row.bic, row.country, row.city,
                    row.creditor_ref, row.default_account, row.status, row.actor,
                    row.updated_at, row.creditor_id,
                ),
            )
            conn.commit()

    def list_creditors(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[SepaCreditor]:
        sql = 'SELECT * FROM creditors'
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
        return [_creditor_from_row(row) for row in rows]

    def find_creditor_by_nickname(self, userid: str, nickname: str) -> Optional[SepaCreditor]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM creditors
                WHERE userid = ? AND lower(nickname) = lower(?)
                  AND status IN ('active', 'paused')
                """,
                (userid, nickname),
            ).fetchone()
        return _creditor_from_row(row) if row else None

    def find_creditor_by_fingerprint(self, userid: str, iban: str) -> Optional[SepaCreditor]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM creditors
                WHERE userid = ? AND iban = ?
                  AND status IN ('active', 'paused')
                """,
                (userid, iban),
            ).fetchone()
        return _creditor_from_row(row) if row else None

    def put_transfer(self, row: SepaTransfer) -> SepaTransfer:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM transfers WHERE trace_id = ?', (row.trace_id,)
            ).fetchone()
            if existing is not None:
                return _transfer_from_row(existing)
            conn.execute(
                """
                INSERT INTO transfers (
                    transfer_id, trace_id, creditor_id, userid, internal_account,
                    amount, debit_usd, rate, fee, fee_status, scheme, nickname,
                    legal_name, iban_masked, bic, purpose, memo, creditor_ref, status,
                    end_to_end_id, tx_id, value_date, actor, releaser, ofac_hit,
                    ofac_match, created_at, updated_at, sent_at, completed_at,
                    recalled_at, note, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.transfer_id, row.trace_id, row.creditor_id, row.userid,
                    row.internal_account, row.amount, row.debit_usd, row.rate, row.fee,
                    row.fee_status, row.scheme, row.nickname, row.legal_name,
                    row.iban_masked, row.bic, row.purpose, row.memo, row.creditor_ref,
                    row.status, row.end_to_end_id, row.tx_id, row.value_date, row.actor,
                    row.releaser, row.ofac_hit, row.ofac_match, row.created_at,
                    row.updated_at, row.sent_at, row.completed_at, row.recalled_at,
                    row.note, row.reason,
                ),
            )
            conn.commit()
            return row

    def update_transfer(self, row: SepaTransfer) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE transfers SET fee=?, fee_status=?, status=?, end_to_end_id=?,
                    tx_id=?, value_date=?, actor=?, releaser=?, ofac_hit=?, ofac_match=?,
                    updated_at=?, sent_at=?, completed_at=?, recalled_at=?, note=?, reason=?
                WHERE transfer_id=?
                """,
                (
                    row.fee, row.fee_status, row.status, row.end_to_end_id, row.tx_id,
                    row.value_date, row.actor, row.releaser, row.ofac_hit, row.ofac_match,
                    row.updated_at, row.sent_at, row.completed_at, row.recalled_at,
                    row.note, row.reason, row.transfer_id,
                ),
            )
            conn.commit()

    def get_transfer(self, transfer_id: str) -> Optional[SepaTransfer]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM transfers WHERE transfer_id = ?', (transfer_id,),
            ).fetchone()
        return _transfer_from_row(row) if row else None

    def get_transfer_by_trace(self, trace_id: str) -> Optional[SepaTransfer]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM transfers WHERE trace_id = ?', (trace_id,),
            ).fetchone()
        return _transfer_from_row(row) if row else None

    def list_transfers(self, userid: Optional[str] = None, creditor_id: Optional[str] = None) -> List[SepaTransfer]:
        sql = 'SELECT * FROM transfers'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if creditor_id is not None:
            clauses.append('creditor_id = ?')
            params.append(creditor_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_transfer_from_row(row) for row in rows]

    def next_e2e_sequence(self, cycle_date: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM transfers WHERE end_to_end_id LIKE ?",
                ('%' + cycle_date + '%',),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class SepaService:
    def __init__(
        self,
        policy: SepaPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[Target2Calendar] = None,
        fx: Optional[EurUsdBook] = None,
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
        self.fx = fx or EurUsdBook(policy.eurusd)

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise SepaError('sepa_disabled', 'SEPA payments are disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise SepaError('sepa_forbidden', 'Customers cannot manage SEPA creditors.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise SepaError('sepa_forbidden', 'Customers cannot originate SEPA payments.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise SepaError('sepa_forbidden', 'Staff only.')

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
            raise SepaError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise SepaError('credit_not_allowed', 'Credit accounts cannot originate SEPA payments.')

    def _assert_amount(self, euros: Decimal, scheme: str) -> None:
        if euros < self.policy.min_amount or euros > self.policy.max_amount:
            raise SepaError('amount_out_of_range', 'Amount is outside the allowed range.')
        if scheme == SCHEME_INSTANT and euros > self.policy.instant_max:
            raise SepaError('instant_amount_exceeded', 'SCT Inst exceeds the instant amount cap.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, debit_usd: Decimal) -> bool:
        return debit_usd >= self.policy.dual_control_threshold

    def quote(self, amount: Any, *, scheme: Any = SCHEME_SCT, waive_fee: bool = False) -> Dict[str, Any]:
        euros = parse_money(amount)
        scheme_id = normalize_scheme(scheme)
        self._assert_amount(euros, scheme_id)
        fx = self.fx.quote(euros)
        fee = compute_fee(scheme_id, waived=waive_fee, table=self.policy.fee_table())
        debit = parse_money(fx.debit_usd)
        return {
            'quote': fx.to_dict(),
            'scheme': scheme_id,
            'fee': money_str(fee),
            'total_usd': money_str(debit + fee),
            'dual_control': self._needs_dual_control(debit),
        }

    def add_creditor(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        iban: Any,
        default_account: Any,
        bic: Any = '',
        city: Any = 'Berlin',
        country: Any = None,
        creditor_ref: Any = '',
    ) -> SepaCreditor:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise SepaError('sepa_forbidden', 'Not allowed to add creditors for this customer.')
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        account_iban = normalize_iban(iban)
        routing = normalize_bic(bic, required=False)
        if routing and bic_country(routing) != iban_country(account_iban):
            raise SepaError('bic_country_mismatch', 'BIC country must match the IBAN country.')
        country_code = normalize_country(country or iban_country(account_iban))
        if country_code != iban_country(account_iban):
            raise SepaError('invalid_country', 'Country must match the IBAN country.')
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        if self.store.find_creditor_by_nickname(owner_userid, name) is not None:
            raise SepaError('creditor_duplicate', 'A creditor with that nickname already exists.')
        if self.store.find_creditor_by_fingerprint(owner_userid, account_iban) is not None:
            raise SepaError('creditor_duplicate', 'That IBAN is already on file.')
        open_rows = [row for row in self.store.list_creditors(owner_userid) if row.status in OPEN_BENE]
        if len(open_rows) >= self.policy.max_creditors:
            raise SepaError('creditor_limit', 'SEPA creditor limit reached.')
        now = float(self.clock())
        row = SepaCreditor(
            creditor_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            iban=account_iban,
            bic=routing,
            country=country_code,
            city=normalize_city(city),
            creditor_ref=normalize_creditor_ref(creditor_ref),
            default_account=account,
            status=BENE_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_creditor(row)
        return row

    def get_creditor(self, *, creditor_id: str, actor: str, actor_type: str) -> SepaCreditor:
        self._require_enabled()
        row = self.store.get_creditor(creditor_id)
        if row is None:
            raise SepaError('creditor_not_found', 'SEPA creditor not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise SepaError('sepa_forbidden', 'Not allowed to view this creditor.')
        return row

    def enforce_creditor(
        self,
        *,
        creditor_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> SepaCreditor:
        """Reusable gate: destination must be an active, unarchived SEPA creditor."""
        row = self.get_creditor(creditor_id=creditor_id, actor=actor, actor_type=actor_type)
        if row.status == BENE_ARCHIVED:
            raise SepaError('already_archived', 'Creditor is archived.')
        if row.status == BENE_PAUSED:
            raise SepaError('creditor_paused', 'Creditor is paused.')
        if require_active and row.status != BENE_ACTIVE:
            raise SepaError('invalid_status', 'Creditor is not active.')
        return row

    def set_creditor_status(
        self,
        *,
        creditor_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> SepaCreditor:
        self._require_manage(actor_type)
        row = self.get_creditor(creditor_id=creditor_id, actor=actor, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': BENE_PAUSED, 'hold': BENE_PAUSED,
            'resume': BENE_ACTIVE, 'activate': BENE_ACTIVE, 'unpause': BENE_ACTIVE,
            'archive': BENE_ARCHIVED, 'close': BENE_ARCHIVED, 'cancel': BENE_ARCHIVED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {BENE_PAUSED, BENE_ACTIVE, BENE_ARCHIVED}:
            raise SepaError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == BENE_ARCHIVED:
            raise SepaError('already_archived', 'Creditor is already archived.')
        if wanted == BENE_PAUSED:
            if row.status == BENE_PAUSED:
                raise SepaError('already_paused', 'Creditor is already paused.')
            if row.status != BENE_ACTIVE:
                raise SepaError('invalid_status', 'Only an active creditor can be paused.')
        elif wanted == BENE_ACTIVE:
            if row.status == BENE_ACTIVE:
                raise SepaError('already_active', 'Creditor is already active.')
            if row.status != BENE_PAUSED:
                raise SepaError('invalid_status', 'Only a paused creditor can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_creditor(row)
        return row

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        creditor_id: Any,
        amount: Any,
        scheme: Any = SCHEME_SCT,
        internal_account: Any = None,
        waive_fee: bool = False,
    ) -> Dict[str, Any]:
        self._require_send(actor_type)
        creditor = self.enforce_creditor(
            creditor_id=str(creditor_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if creditor.userid != owner_userid:
            raise SepaError('sepa_forbidden', 'Creditor does not belong to this customer.')
        scheme_id = normalize_scheme(scheme)
        euros = parse_money(amount)
        self._assert_amount(euros, scheme_id)
        account = normalize_account(internal_account or creditor.default_account)
        self._assert_internal_account(owner_userid, account)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        quoted = self.quote(euros, scheme=scheme_id, waive_fee=staff_waive)
        now = float(self.clock())
        ofac = self._screen(creditor.legal_name, aliases=(creditor.nickname,))
        clock = self.calendar.snapshot(now, scheme=scheme_id)
        return {
            'amount': money_str(euros),
            'currency': 'EUR',
            'scheme': scheme_id,
            'fee': quoted['fee'],
            'total_usd': quoted['total_usd'],
            'quote': quoted['quote'],
            'internal_account': account,
            'creditor': creditor.to_dict(),
            'ofac': ofac.to_dict(),
            'dual_control': quoted['dual_control'],
            'clock': clock,
        }

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        creditor: SepaCreditor,
        account: str,
        euros: Decimal,
        scheme: str,
        fee: Decimal,
        purpose: str,
        memo: str,
        trace_id: str,
        ofac: ScreenResult,
        waive_fee: bool,
        end_to_end: str,
    ) -> SepaTransfer:
        now = float(self.clock())
        fx = self.fx.quote(euros)
        debit = parse_money(fx.debit_usd)
        value = self.calendar.cycle_date(now, scheme=scheme)
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        if ofac.hit:
            status = SEPA_HELD
        elif self._needs_dual_control(debit):
            status = SEPA_PENDING
        elif scheme == SCHEME_SCT and self.calendar.snapshot(now, scheme=scheme)['after_cutoff']:
            status = SEPA_QUEUED
        else:
            status = SEPA_SENT
        transfer = SepaTransfer(
            transfer_id=uuid.uuid4().hex,
            trace_id=trace_id,
            creditor_id=creditor.creditor_id,
            userid=owner_userid,
            internal_account=account,
            amount=money_str(euros),
            debit_usd=fx.debit_usd,
            rate=fx.rate,
            fee=money_str(fee),
            fee_status=fee_status,
            scheme=scheme,
            nickname=creditor.nickname,
            legal_name=creditor.legal_name,
            iban_masked=mask_iban(creditor.iban),
            bic=creditor.bic,
            purpose=purpose,
            memo=memo,
            creditor_ref=creditor.creditor_ref,
            status=status,
            end_to_end_id=end_to_end,
            tx_id='',
            value_date=value,
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
        )
        if status == SEPA_SENT:
            self._transmit(transfer, actor=actor, creditor=creditor)
        return transfer

    def _transmit(
        self,
        transfer: SepaTransfer,
        *,
        actor: str,
        creditor: Optional[SepaCreditor] = None,
    ) -> SepaTransfer:
        now = float(self.clock())
        cycle = transfer.value_date or self.calendar.cycle_date(now, scheme=transfer.scheme)
        seq = self.store.next_e2e_sequence(cycle)
        if not transfer.end_to_end_id:
            transfer.end_to_end_id = compose_end_to_end_id(transfer.scheme, cycle, seq)
        debit = parse_money(transfer.debit_usd)
        fee = parse_money(transfer.fee, allow_zero=True)
        remark = 'sepa to %s' % transfer.nickname
        status = SEPA_SENT
        fail_note = ''
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(transfer.internal_account, money_str(debit), remark)
            except Exception as exc:
                status = SEPA_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = SEPA_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = SEPA_FAILED
                    fail_note = str(result)[:240]
        if status == SEPA_SENT and fee > 0 and transfer.fee_status != FEE_WAIVED and self.debit_fn is not None:
            try:
                fee_result = self.debit_fn(
                    transfer.internal_account, money_str(fee), 'sepa fee %s' % transfer.end_to_end_id[:12],
                )
            except Exception:
                transfer.fee_status = FEE_NSF
            else:
                kind = _classify_money_result(fee_result)
                transfer.fee_status = FEE_COLLECTED if kind == 'ok' else FEE_NSF
        elif status == SEPA_SENT and (fee == 0 or transfer.fee_status == FEE_WAIVED):
            transfer.fee_status = FEE_WAIVED if transfer.fee_status == FEE_WAIVED or fee == 0 else transfer.fee_status
        transfer.updated_at = now
        if status == SEPA_SENT:
            transfer.sent_at = now
            transfer.releaser = str(actor)
            if transfer.scheme == SCHEME_INSTANT:
                transfer.tx_id = compose_tx_id(cycle, seq, source=self.policy.source_id)
                transfer.status = SEPA_COMPLETED
                transfer.completed_at = now
            else:
                transfer.status = SEPA_SENT
        else:
            transfer.status = status
            transfer.end_to_end_id = ''
            transfer.note = fail_note
        if creditor is not None and transfer.status in {SEPA_SENT, SEPA_COMPLETED}:
            transfer.note = ''
            _ = compose_pain001(
                msg_id=transfer.end_to_end_id,
                instr_id=transfer.transfer_id[:35],
                end_to_end_id=transfer.end_to_end_id,
                scheme=transfer.scheme,
                amount_eur=transfer.amount,
                creditor_name=creditor.legal_name,
                iban=creditor.iban,
                bic=creditor.bic,
                remittance=transfer.memo,
                purpose=transfer.purpose,
                creditor_ref=transfer.creditor_ref,
            )
        return transfer

    def originate(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        creditor_id: Any,
        amount: Any,
        scheme: Any = SCHEME_SCT,
        internal_account: Any = None,
        purpose: Any = 'other',
        memo: Any = '',
        trace_id: Any = None,
        waive_fee: bool = False,
        end_to_end_id: Any = None,
    ) -> Tuple[SepaTransfer, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise SepaError('sepa_forbidden', 'Not allowed to originate SEPA payments for this customer.')
        creditor = self.enforce_creditor(
            creditor_id=str(creditor_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if creditor.userid != owner_userid:
            raise SepaError('sepa_forbidden', 'Creditor does not belong to this customer.')
        scheme_id = normalize_scheme(scheme)
        euros = parse_money(amount)
        self._assert_amount(euros, scheme_id)
        account = normalize_account(internal_account or creditor.default_account)
        self._assert_internal_account(owner_userid, account)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(scheme_id, waived=staff_waive, table=self.policy.fee_table())
        trace = normalize_id(trace_id)
        existing = self.store.get_transfer_by_trace(trace)
        if existing is not None:
            return existing, False
        if len(self.store.list_transfers(owner_userid)) >= self.policy.max_transfers:
            raise SepaError('transfer_limit', 'SEPA history limit reached.')
        ofac = self._screen(creditor.legal_name, aliases=(creditor.nickname,))
        transfer = self._place(
            owner_userid=owner_userid,
            actor=actor,
            creditor=creditor,
            account=account,
            euros=euros,
            scheme=scheme_id,
            fee=fee,
            purpose=normalize_purpose(purpose),
            memo=normalize_note(memo, limit=140),
            trace_id=trace,
            ofac=ofac,
            waive_fee=staff_waive,
            end_to_end=normalize_end_to_end(end_to_end_id),
        )
        stored = self.store.put_transfer(transfer)
        if stored.transfer_id != transfer.transfer_id:
            return stored, False
        if stored.status == SEPA_NSF:
            raise SepaError('nsf', 'Insufficient funds for SEPA payment.', transfer=stored)
        if stored.status == SEPA_FAILED:
            raise SepaError('failed', 'SEPA debit did not complete.', transfer=stored)
        return stored, True

    def get_transfer(self, *, transfer_id: str, actor: str, actor_type: str) -> SepaTransfer:
        self._require_enabled()
        row = self.store.get_transfer(transfer_id)
        if row is None:
            raise SepaError('transfer_not_found', 'SEPA transfer not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise SepaError('sepa_forbidden', 'Not allowed to view this transfer.')
        return row

    def cancel_transfer(
        self,
        *,
        transfer_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> SepaTransfer:
        self._require_send(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and transfer.userid != actor:
            raise SepaError('sepa_forbidden', 'Not allowed to cancel this transfer.')
        if transfer.status not in CANCELABLE:
            raise SepaError('not_cancelable', 'Only held, queued, or pending SEPA payments can be cancelled.')
        transfer.status = SEPA_CANCELLED
        transfer.actor = str(actor)
        transfer.updated_at = float(self.clock())
        transfer.note = normalize_note(note)
        self.store.update_transfer(transfer)
        return transfer

    def waive_fee(
        self,
        *,
        transfer_id: str,
        actor: str,
        actor_type: str,
    ) -> SepaTransfer:
        self._require_staff(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if transfer.status not in CANCELABLE:
            raise SepaError('invalid_status', 'Fee can only be waived before the payment is sent.')
        transfer.fee = money_str(Decimal('0.00'))
        transfer.fee_status = FEE_WAIVED
        transfer.actor = str(actor)
        transfer.updated_at = float(self.clock())
        self.store.update_transfer(transfer)
        return transfer

    def override_ofac(
        self,
        *,
        transfer_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> SepaTransfer:
        self._require_staff(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if transfer.status != SEPA_HELD:
            raise SepaError('invalid_status', 'Only an OFAC hold can be overridden.')
        now = float(self.clock())
        transfer.ofac_hit = 0
        transfer.note = normalize_note(note) or 'ofac override'
        transfer.actor = str(actor)
        transfer.updated_at = now
        debit = parse_money(transfer.debit_usd)
        if self._needs_dual_control(debit):
            transfer.status = SEPA_PENDING
        elif transfer.scheme == SCHEME_SCT and self.calendar.snapshot(now, scheme=transfer.scheme)['after_cutoff']:
            transfer.status = SEPA_QUEUED
            transfer.value_date = self.calendar.cycle_date(now, scheme=transfer.scheme)
        else:
            creditor = self.store.get_creditor(transfer.creditor_id)
            self._transmit(transfer, actor=actor, creditor=creditor)
        self.store.update_transfer(transfer)
        if transfer.status == SEPA_NSF:
            raise SepaError('nsf', 'Insufficient funds for SEPA payment.', transfer=transfer)
        if transfer.status == SEPA_FAILED:
            raise SepaError('failed', 'SEPA debit did not complete.', transfer=transfer)
        return transfer

    def release_transfer(
        self,
        *,
        transfer_id: str,
        actor: str,
        actor_type: str,
    ) -> SepaTransfer:
        self._require_staff(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if transfer.status == SEPA_HELD:
            raise SepaError('ofac_hold', 'OFAC hold must be overridden before release.')
        if transfer.status not in {SEPA_PENDING, SEPA_QUEUED}:
            raise SepaError('not_releasable', 'Only queued or pending SEPA payments can be released.')
        if (
            transfer.status == SEPA_PENDING
            and transfer.actor
            and str(actor) == str(transfer.actor)
            and parse_money(transfer.debit_usd) >= self.policy.dual_control_threshold
        ):
            raise SepaError('same_approver', 'A different employee must release this payment.')
        now = float(self.clock())
        if transfer.scheme == SCHEME_SCT and (
            transfer.status == SEPA_QUEUED or self.calendar.snapshot(now, scheme=SCHEME_SCT)['after_cutoff']
        ):
            if self.calendar.snapshot(now, scheme=SCHEME_SCT)['after_cutoff'] and transfer.status != SEPA_PENDING:
                transfer.status = SEPA_QUEUED
                transfer.value_date = self.calendar.cycle_date(now, scheme=SCHEME_SCT)
                transfer.updated_at = now
                self.store.update_transfer(transfer)
                return transfer
        creditor = self.store.get_creditor(transfer.creditor_id)
        self._transmit(transfer, actor=actor, creditor=creditor)
        self.store.update_transfer(transfer)
        if transfer.status == SEPA_NSF:
            raise SepaError('nsf', 'Insufficient funds for SEPA payment.', transfer=transfer)
        if transfer.status == SEPA_FAILED:
            raise SepaError('failed', 'SEPA debit did not complete.', transfer=transfer)
        return transfer

    def reject_transfer(
        self,
        *,
        transfer_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> SepaTransfer:
        self._require_staff(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if transfer.status not in CANCELABLE:
            raise SepaError('not_rejectable', 'Only held, queued, or pending SEPA payments can be rejected.')
        transfer.status = SEPA_REJECTED
        transfer.reason = normalize_note(reason, limit=40)
        transfer.note = normalize_note(note)
        transfer.actor = str(actor)
        transfer.updated_at = float(self.clock())
        self.store.update_transfer(transfer)
        return transfer

    def complete_transfer(
        self,
        *,
        transfer_id: str,
        actor: str,
        actor_type: str,
    ) -> SepaTransfer:
        self._require_staff(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if transfer.status == SEPA_COMPLETED:
            raise SepaError('already_completed', 'SEPA payment is already completed.')
        if transfer.status != SEPA_SENT:
            raise SepaError('not_completable', 'Only sent SCT payments can be completed.')
        now = float(self.clock())
        seq = self.store.next_e2e_sequence(transfer.value_date)
        transfer.tx_id = compose_tx_id(transfer.value_date, seq, source=self.policy.source_id)
        transfer.status = SEPA_COMPLETED
        transfer.completed_at = now
        transfer.updated_at = now
        transfer.actor = str(actor)
        self.store.update_transfer(transfer)
        return transfer

    def recall_transfer(
        self,
        *,
        transfer_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> SepaTransfer:
        self._require_staff(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if transfer.scheme == SCHEME_INSTANT and transfer.status in {SEPA_SENT, SEPA_COMPLETED}:
            raise SepaError('scheme_irrevocable', 'SCT Inst payments cannot be recalled after settlement.')
        if transfer.status == SEPA_RECALLED:
            raise SepaError('already_recalled', 'SEPA payment is already recalled.')
        if transfer.status == SEPA_COMPLETED:
            raise SepaError('already_completed', 'Completed SCT payments cannot be recalled.')
        if transfer.status != SEPA_SENT:
            raise SepaError('not_recallable', 'Only sent SCT payments can be recalled.')
        remark = normalize_note(note) or ('sepa recalled from %s' % transfer.nickname)
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(transfer.internal_account, transfer.debit_usd, remark)
            except Exception as exc:
                raise SepaError('recall_failed', 'Recall credit failed.', transfer=transfer) from exc
            if _classify_money_result(result) != 'ok':
                raise SepaError('recall_failed', 'Recall credit failed.', transfer=transfer)
            fee = parse_money(transfer.fee, allow_zero=True)
            if fee > 0 and transfer.fee_status == FEE_COLLECTED:
                self.credit_fn(
                    transfer.internal_account, money_str(fee),
                    'sepa fee recalled %s' % (transfer.end_to_end_id[:12] or transfer.transfer_id[:8]),
                )
        now = float(self.clock())
        transfer.status = SEPA_RECALLED
        transfer.recalled_at = now
        transfer.updated_at = now
        transfer.actor = str(actor)
        transfer.note = remark
        self.store.update_transfer(transfer)
        return transfer

    def run_due(self, userid: Optional[str] = None) -> List[SepaTransfer]:
        now = float(self.clock())
        today = self.calendar.local_dt(now).date().strftime('%Y%m%d')
        after = self.calendar.snapshot(now, scheme=SCHEME_SCT)['after_cutoff']
        changed: List[SepaTransfer] = []
        for transfer in self.store.list_transfers(userid):
            if transfer.status != SEPA_QUEUED:
                continue
            if transfer.value_date > today:
                continue
            if after and transfer.value_date == today:
                continue
            debit = parse_money(transfer.debit_usd)
            if self._needs_dual_control(debit):
                transfer.status = SEPA_PENDING
                transfer.updated_at = now
                self.store.update_transfer(transfer)
                changed.append(transfer)
                continue
            creditor = self.store.get_creditor(transfer.creditor_id)
            self._transmit(transfer, actor=transfer.actor, creditor=creditor)
            self.store.update_transfer(transfer)
            changed.append(transfer)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.run_due(userid)
        creditors = self.store.list_creditors(userid)
        transfers = self.store.list_transfers(userid)
        sent_ytd = Decimal('0.00')
        debit_ytd = Decimal('0.00')
        fee_ytd = Decimal('0.00')
        recalled = Decimal('0.00')
        for row in transfers:
            amount = parse_money(row.amount, allow_zero=True)
            if row.status in {SEPA_SENT, SEPA_COMPLETED}:
                sent_ytd += amount
                debit_ytd += parse_money(row.debit_usd, allow_zero=True)
                if row.fee_status == FEE_COLLECTED:
                    fee_ytd += parse_money(row.fee, allow_zero=True)
            elif row.status == SEPA_RECALLED:
                recalled += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'instant_max': money_str(self.policy.instant_max),
            'sct_fee': money_str(self.policy.sct_fee),
            'instant_fee': money_str(self.policy.instant_fee),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'eurusd': str(self.fx.rate),
            'clock': self.calendar.snapshot(now, scheme=SCHEME_SCT),
            'instant_clock': self.calendar.snapshot(now, scheme=SCHEME_INSTANT),
            'creditors': [row.to_dict() for row in creditors[:40]],
            'transfers': [row.to_dict() for row in transfers[:40]],
            'ytd_sent': money_str(sent_ytd),
            'ytd_debit_usd': money_str(debit_ytd),
            'ytd_fees': money_str(fee_ytd),
            'recalled_ytd': money_str(recalled),
            'active_count': sum(1 for row in creditors if row.status == BENE_ACTIVE),
            'open_count': sum(1 for row in transfers if row.status in OPEN_SEPAS),
        }


_SERVICE: Optional[SepaService] = None


def set_service(service: Optional[SepaService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[SepaService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('SEPA_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemorySepaStore()
    path = os.environ.get('SEPA_DB', DEFAULT_STORE_PATH)
    return SqliteSepaStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[Target2Calendar] = None,
    fx: Optional[EurUsdBook] = None,
) -> SepaService:
    if store is None:
        store = default_store()
    return SepaService(
        SepaPolicy.from_env(),
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
        'creditor_duplicate': 409,
        'creditor_limit': 409,
        'transfer_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_completed': 409,
        'already_recalled': 409,
        'nsf': 409,
        'failed': 409,
        'recall_failed': 409,
        'sepa_forbidden': 403,
        'sepa_disabled': 403,
        'creditor_paused': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_cancelable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_completable': 403,
        'not_recallable': 403,
        'scheme_irrevocable': 403,
        'creditor_not_found': 404,
        'transfer_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_iban': 400,
        'invalid_bic': 400,
        'invalid_country': 400,
        'invalid_address': 400,
        'invalid_purpose': 400,
        'invalid_scheme': 400,
        'invalid_status': 400,
        'invalid_reference': 400,
        'invalid_end_to_end': 400,
        'invalid_rate': 400,
        'not_sepa_country': 400,
        'bic_country_mismatch': 400,
        'instant_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_creditor': 400,
        'missing_transfer': 400,
    }.get(code, 400)


def _error_body(exc: SepaError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('transfer') is not None:
        body['transfer'] = exc.extra['transfer'].to_dict()
    if exc.extra.get('creditor') is not None:
        body['creditor'] = exc.extra['creditor'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except SepaError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list_sepas(service: SepaService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Sepa': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_add_creditor(service: SepaService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.add_creditor(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname'),
            legal_name=values.get('legal_name') or values.get('name'),
            iban=values.get('iban'),
            bic=values.get('bic') or '',
            city=values.get('city') or 'Berlin',
            country=values.get('country'),
            creditor_ref=values.get('creditor_ref') or values.get('reference') or '',
            default_account=values.get('default_account') or values.get('account') or values.get('from_account'),
        )
        return jsonify({
            'message': 'SEPA creditor added',
            'creditor': row.to_dict(),
            'Sepa': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _creditor_status_route(service: SepaService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    creditor_id = str(values.get('creditor_id') or '').strip()
    if not creditor_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_creditor'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_creditor_status(
            creditor_id=creditor_id, actor=userid, actor_type=actor_type, status=status,
        )
        return jsonify({
            'message': ok_message,
            'creditor': row.to_dict(),
            'Sepa': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_quote(service: SepaService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        quoted = service.quote(
            values.get('amount'),
            scheme=values.get('scheme') or SCHEME_SCT,
            waive_fee=bool(values.get('waive_fee')) and actor_type in EMPLOYEE_ROLES,
        )
        return jsonify({'preview': quoted, 'Sepa': service.snapshot(
            _owner_userid(userid, actor_type, values) or userid,
            actor=userid, actor_type=actor_type,
        )}), 200

    return _handle_errors(_run)


def handle_preview(service: SepaService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    creditor_id = str(values.get('creditor_id') or values.get('beneficiary_id') or '').strip()
    if not creditor_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_creditor'}), 400

    def _run():
        preview = service.preview(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            creditor_id=creditor_id,
            amount=values.get('amount'),
            scheme=values.get('scheme') or SCHEME_SCT,
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({'preview': preview, 'Sepa': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: SepaService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    creditor_id = str(values.get('creditor_id') or values.get('beneficiary_id') or '').strip()
    if not creditor_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_creditor'}), 400

    def _run():
        transfer, created = service.originate(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            creditor_id=creditor_id,
            amount=values.get('amount'),
            scheme=values.get('scheme') or SCHEME_SCT,
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
            end_to_end_id=values.get('end_to_end_id'),
        )
        return jsonify({
            'message': 'SEPA originated' if created else 'SEPA already posted',
            'transfer': transfer.to_dict(),
            'Sepa': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: SepaService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    transfer_id = str(values.get('transfer_id') or values.get('wire_id') or '').strip()
    if not transfer_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_transfer'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        transfer = service.cancel_transfer(
            transfer_id=transfer_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': 'SEPA cancelled',
            'transfer': transfer.to_dict(),
            'Sepa': service.snapshot(transfer.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_sepa_route(service: SepaService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    transfer_id = str(values.get('transfer_id') or values.get('wire_id') or '').strip()
    if not transfer_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_transfer'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'release':
            transfer = service.release_transfer(transfer_id=transfer_id, actor=userid, actor_type=actor_type)
            message = 'SEPA released'
        elif action == 'reject':
            transfer = service.reject_transfer(
                transfer_id=transfer_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'SEPA rejected'
        elif action == 'complete':
            transfer = service.complete_transfer(transfer_id=transfer_id, actor=userid, actor_type=actor_type)
            message = 'SEPA completed'
        elif action == 'recall':
            transfer = service.recall_transfer(
                transfer_id=transfer_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'SEPA recalled'
        elif action == 'override':
            transfer = service.override_ofac(
                transfer_id=transfer_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'waive':
            transfer = service.waive_fee(transfer_id=transfer_id, actor=userid, actor_type=actor_type)
            message = 'SEPA fee waived'
        else:
            raise SepaError('invalid_status', 'Unknown SEPA action.')
        return jsonify({
            'message': message,
            'transfer': transfer.to_dict(),
            'Sepa': service.snapshot(transfer.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: SepaService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Sepa': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_sepa_routes(app, service: SepaService) -> None:
    @app.route('/listSepas', methods=['POST', 'GET'])
    def list_sepas_route():
        return handle_list_sepas(service)

    @app.route('/listSepaCreditors', methods=['POST', 'GET'])
    def list_sepa_creditors_route():
        return handle_list_sepas(service)

    @app.route('/addSepaCreditor', methods=['POST', 'GET'])
    def add_sepa_creditor_route():
        return handle_add_creditor(service)

    @app.route('/pauseSepaCreditor', methods=['POST', 'GET'])
    def pause_sepa_creditor_route():
        return _creditor_status_route(service, BENE_PAUSED, 'SEPA creditor paused')

    @app.route('/resumeSepaCreditor', methods=['POST', 'GET'])
    def resume_sepa_creditor_route():
        return _creditor_status_route(service, BENE_ACTIVE, 'SEPA creditor resumed')

    @app.route('/archiveSepaCreditor', methods=['POST', 'GET'])
    def archive_sepa_creditor_route():
        return _creditor_status_route(service, BENE_ARCHIVED, 'SEPA creditor archived')

    @app.route('/quoteSepaFx', methods=['POST', 'GET'])
    def quote_sepa_fx_route():
        return handle_quote(service)

    @app.route('/previewSepa', methods=['POST', 'GET'])
    def preview_sepa_route():
        return handle_preview(service)

    @app.route('/sendSepa', methods=['POST', 'GET'])
    def send_sepa_route():
        return handle_send(service)

    @app.route('/cancelSepa', methods=['POST', 'GET'])
    def cancel_sepa_route():
        return handle_cancel(service)

    @app.route('/releaseSepa', methods=['POST', 'GET'])
    def release_sepa_route():
        return _staff_sepa_route(service, 'release')

    @app.route('/rejectSepa', methods=['POST', 'GET'])
    def reject_sepa_route():
        return _staff_sepa_route(service, 'reject')

    @app.route('/completeSepa', methods=['POST', 'GET'])
    def complete_sepa_route():
        return _staff_sepa_route(service, 'complete')

    @app.route('/recallSepa', methods=['POST', 'GET'])
    def recall_sepa_route():
        return _staff_sepa_route(service, 'recall')

    @app.route('/overrideSepaOfac', methods=['POST', 'GET'])
    def override_sepa_ofac_route():
        return _staff_sepa_route(service, 'override')

    @app.route('/waiveSepaFee', methods=['POST', 'GET'])
    def waive_sepa_fee_route():
        return _staff_sepa_route(service, 'waive')

    @app.route('/runDueSepas', methods=['POST', 'GET'])
    def run_due_sepas_route():
        return handle_run_due(service)
