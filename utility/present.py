"""Card settlement presentment (first / second presentment / clearing).

Staff ingest incoming card-network clearing presentments. Recipients are
enrolled PANs (Luhn + IIN brand) at this issuer. Matched first presentments
debit the ledger on the settlement cycle; second presentment re-clears an
ARN after reverse/reject; reversal credits a posted presentment.
Independent of card authorization holds (PR #149), chargeback representment
(PR #109), outbound Fedwire (PR #73), ACH linking (PR #68), and inbound UPI
(PR #147). Existing `/fundTransfer`, `/withdrawAmount`, and `/sendWire`
stay unchanged.

Foundations (reusable beyond this screen):
- PAN Luhn check digit + IIN brand
- PAN fingerprint (SHA-256; never in snapshots)
- ISO 8583 presentment MTI (1240 first, 1221 second, 1442 reversal)
- Function codes 200 / 205
- ARN uniqueness (23-digit acquirer reference)
- MCC interchange (percent + flat; issuer revenue, not a customer debit)
- Settlement-cycle clock (Fed holidays, 22:00 ET cutoff, T+0 / T+1)
- IPM1 pipe compose/parse (XML/DOCTYPE rejected)
- Dual-control at $10k, OFAC on merchant name
- 24h unmatched claim / 24h customer reverse / 90-day staff reverse

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Full PAN, fingerprint, and ARN never appear in to_dict / snapshots.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    AccountError,
    AmountError,
    DEFAULT_WATCHLIST,
    EMPLOYEE_ROLES,
    ScreenResult,
    account_types_from_customer_payload,
    last4,
    money_str,
    normalize_account,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
    us_fed_holidays,
)

KEY_ACTIVE = 'active'
KEY_PAUSED = 'paused'
KEY_CANCELLED = 'cancelled'
KEY_STATUSES = frozenset({KEY_ACTIVE, KEY_PAUSED, KEY_CANCELLED})
OPEN_KEYS = frozenset({KEY_ACTIVE, KEY_PAUSED})

PRES_UNMATCHED = 'unmatched'
PRES_HELD = 'held'
PRES_PENDING = 'pending_release'
PRES_QUEUED = 'queued'
PRES_POSTED = 'posted'
PRES_REVERSED = 'reversed'
PRES_REJECTED = 'rejected'
PRES_EXPIRED = 'expired'
PRES_NSF = 'nsf'
PRES_FAILED = 'failed'
PRES_STATUSES = frozenset({
    PRES_UNMATCHED, PRES_HELD, PRES_PENDING, PRES_QUEUED, PRES_POSTED,
    PRES_REVERSED, PRES_REJECTED, PRES_EXPIRED, PRES_NSF, PRES_FAILED,
})
OPEN_PRESENTMENTS = frozenset({PRES_UNMATCHED, PRES_HELD, PRES_PENDING, PRES_QUEUED, PRES_NSF})
RELEASABLE = frozenset({PRES_PENDING, PRES_HELD, PRES_QUEUED})
REJECTABLE = frozenset({PRES_UNMATCHED, PRES_HELD, PRES_PENDING, PRES_QUEUED})
REPRESENTABLE = frozenset({PRES_REVERSED, PRES_REJECTED})

FN_FIRST = '200'
FN_SECOND = '205'
FUNCTION_CODES = frozenset({FN_FIRST, FN_SECOND})
FUNCTION_ALIASES = {
    '200': FN_FIRST, 'first': FN_FIRST, 'presentment': FN_FIRST, '1st': FN_FIRST,
    '205': FN_SECOND, 'second': FN_SECOND, 'representment': FN_SECOND, '2nd': FN_SECOND,
}

MTI_FIRST = '1240'
MTI_SECOND = '1221'
MTI_REVERSAL = '1442'
MTI_ALIASES = {
    '0220': MTI_FIRST, '1220': MTI_FIRST, '0240': MTI_FIRST, '1240': MTI_FIRST,
    '0221': MTI_SECOND, '1221': MTI_SECOND,
    '0420': MTI_REVERSAL, '1420': MTI_REVERSAL, '0442': MTI_REVERSAL, '1442': MTI_REVERSAL,
}
FIRST_MTIS = frozenset({MTI_FIRST})
SECOND_MTIS = frozenset({MTI_SECOND})
REVERSAL_MTIS = frozenset({MTI_REVERSAL})
VALID_MTIS = FIRST_MTIS | SECOND_MTIS | REVERSAL_MTIS

BRANDS = frozenset({'visa', 'mastercard', 'amex', 'discover'})
BRAND_ALIASES = {
    'visa': 'visa', 'vi': 'visa',
    'mastercard': 'mastercard', 'mc': 'mastercard', 'master': 'mastercard',
    'amex': 'amex', 'americanexpress': 'amex', 'american_express': 'amex',
    'discover': 'discover', 'disc': 'discover',
}

MCC_FUEL = frozenset({'5541', '5542'})
MCC_GROCERY = frozenset({'5411'})
MCC_RESTAURANT = frozenset({'5812', '5814'})
MCC_LODGING = frozenset({'7011'})
MCC_AUTO = frozenset({'7512', '7513'})

PURPOSE_ALIASES = {
    'retail': 'retail', 'purchase': 'retail', 'pos': 'retail',
    'fuel': 'fuel', 'gas': 'fuel', 'petrol': 'fuel',
    'grocery': 'grocery', 'supermarket': 'grocery',
    'restaurant': 'restaurant', 'dining': 'restaurant',
    'hotel': 'lodging', 'lodging': 'lodging',
    'auto': 'auto', 'rental': 'auto', 'car': 'auto',
    'other': 'other',
}
PURPOSES = frozenset(PURPOSE_ALIASES.values())

DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/present.sqlite'
DEFAULT_ACQUIRER = '400000'
CUSTOMER_REVERSE_HOURS = 24
STAFF_REVERSE_DAYS = 90
UNMATCHED_HOURS = 24


class PresentError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def normalize_note(value: Any, *, limit: int = 500) -> str:
    return str(value or '').strip()[:limit]


def normalize_nickname(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise PresentError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_merchant(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise PresentError('invalid_name', 'Merchant name must be 2-80 characters.')
    return text


def normalize_currency(value: Any, *, default: str = 'USD') -> str:
    text = ''.join(ch for ch in str(value or default).upper() if ch.isalpha())
    if text != 'USD':
        raise PresentError('invalid_currency', 'Card presentments settle in USD.')
    return text


def normalize_purpose(value: Any, *, default: str = 'retail') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise PresentError('invalid_purpose', 'Unknown presentment purpose.')
    return text


def normalize_mcc(value: Any, *, default: str = '5999') -> str:
    digits = ''.join(ch for ch in str(value if value not in (None, '') else default) if ch.isdigit())
    if len(digits) == 3:
        digits = digits.zfill(4)
    if len(digits) != 4:
        raise PresentError('invalid_mcc', 'MCC must be four digits.')
    return digits


def purpose_from_mcc(mcc: str) -> str:
    if mcc in MCC_FUEL:
        return 'fuel'
    if mcc in MCC_GROCERY:
        return 'grocery'
    if mcc in MCC_RESTAURANT:
        return 'restaurant'
    if mcc in MCC_LODGING:
        return 'lodging'
    if mcc in MCC_AUTO:
        return 'auto'
    return 'retail'


def interchange_components(mcc: str) -> Tuple[Decimal, Decimal]:
    """Issuer interchange: (percent, flat). Distinct from auth-hold capture windows."""
    if mcc in MCC_FUEL:
        return Decimal('0.0115'), Decimal('0.25')
    if mcc in MCC_GROCERY:
        return Decimal('0.0115'), Decimal('0.05')
    if mcc in MCC_RESTAURANT:
        return Decimal('0.0164'), Decimal('0.10')
    if mcc in MCC_LODGING | MCC_AUTO:
        return Decimal('0.0180'), Decimal('0.10')
    return Decimal('0.0165'), Decimal('0.10')


def compute_interchange(amount: Decimal, mcc: str) -> Decimal:
    percent, flat = interchange_components(mcc)
    return (amount * percent + flat).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def luhn_check_digit_ok(digits: str) -> bool:
    if not digits or not digits.isdigit():
        return False
    total = 0
    reverse = digits[::-1]
    for index, ch in enumerate(reverse):
        n = int(ch)
        if index % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def brand_from_iin(digits: str) -> str:
    if not digits:
        raise PresentError('invalid_pan', 'PAN is required.')
    if digits.startswith('4'):
        return 'visa'
    two = int(digits[:2]) if len(digits) >= 2 else 0
    four = int(digits[:4]) if len(digits) >= 4 else 0
    three = int(digits[:3]) if len(digits) >= 3 else 0
    if two in {34, 37}:
        return 'amex'
    if 51 <= two <= 55 or 2221 <= four <= 2720:
        return 'mastercard'
    if digits.startswith('6011') or digits.startswith('65') or 644 <= three <= 649:
        return 'discover'
    raise PresentError('invalid_pan', 'Unsupported card brand.')


def normalize_brand(value: Any) -> str:
    text = ''.join(ch for ch in str(value or '').lower() if ch.isalnum() or ch == '_')
    brand = BRAND_ALIASES.get(text)
    if brand is None:
        raise PresentError('invalid_brand', 'Brand must be visa, mastercard, amex, or discover.')
    return brand


def normalize_pan(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not (13 <= len(digits) <= 19):
        raise PresentError('invalid_pan', 'PAN must be 13-19 digits.')
    if digits == '0' * len(digits) or not luhn_check_digit_ok(digits):
        raise PresentError('invalid_pan', 'PAN failed Luhn checksum.')
    brand = brand_from_iin(digits)
    if brand == 'amex' and len(digits) != 15:
        raise PresentError('invalid_pan', 'American Express PANs must be 15 digits.')
    if brand == 'mastercard' and len(digits) != 16:
        raise PresentError('invalid_pan', 'Mastercard PANs must be 16 digits.')
    if brand == 'discover' and len(digits) not in {16, 19}:
        raise PresentError('invalid_pan', 'Discover PANs must be 16 or 19 digits.')
    return digits


def mask_pan(pan: str) -> str:
    digits = ''.join(ch for ch in str(pan or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return ('*' * max(0, len(digits) - 4)) + digits[-4:]


def pan_fingerprint(pan: str) -> str:
    digits = ''.join(ch for ch in str(pan or '') if ch.isdigit())
    return hashlib.sha256(digits.encode('ascii')).hexdigest()


def normalize_acquirer(value: Any, *, default: str = DEFAULT_ACQUIRER) -> str:
    digits = ''.join(ch for ch in str(value or default) if ch.isdigit())
    if not digits:
        digits = default
    if len(digits) < 6:
        digits = digits.zfill(6)
    if len(digits) != 6:
        raise PresentError('invalid_arn', 'Acquirer BIN must be six digits.')
    if digits == '000000':
        raise PresentError('invalid_arn', 'Acquirer BIN cannot be zero.')
    return digits


def compose_arn(cycle_date: str, sequence: int, *, acquirer: str = DEFAULT_ACQUIRER) -> str:
    """23-digit ARN: {6-digit acquirer}{YYYYMMDD}{9-digit sequence}."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise PresentError('invalid_arn', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999999:
        raise PresentError('invalid_arn', 'ARN sequence out of range.')
    return '%s%s%09d' % (normalize_acquirer(acquirer), day, seq)


def normalize_arn(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 23:
        raise PresentError('invalid_arn', 'ARN must be twenty-three digits.')
    if digits == '0' * 23:
        raise PresentError('invalid_arn', 'ARN cannot be zero.')
    return digits


def mask_arn(arn: str) -> str:
    digits = ''.join(ch for ch in str(arn or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return ('*' * (len(digits) - 4)) + digits[-4:]


def normalize_function(value: Any, *, default: str = FN_FIRST) -> str:
    text = str(value if value not in (None, '') else default).strip().lower()
    mapped = FUNCTION_ALIASES.get(text) or FUNCTION_ALIASES.get(text.replace(' ', '_'))
    if mapped is None:
        digits = ''.join(ch for ch in text if ch.isdigit())
        mapped = FUNCTION_ALIASES.get(digits)
    if mapped is None:
        raise PresentError('invalid_function', 'Function code must be 200 or 205.')
    return mapped


def normalize_mti(value: Any, *, default: str = MTI_FIRST) -> str:
    text = ''.join(ch for ch in str(value if value not in (None, '') else default) if ch.isdigit())
    if len(text) == 3:
        text = text.zfill(4)
    mapped = MTI_ALIASES.get(text)
    if mapped is None:
        raise PresentError('invalid_mti', 'Unknown ISO 8583 presentment MTI.')
    return mapped


def mti_kind(mti: str) -> str:
    if mti in FIRST_MTIS:
        return 'first'
    if mti in SECOND_MTIS:
        return 'second'
    if mti in REVERSAL_MTIS:
        return 'reversal'
    raise PresentError('invalid_mti', 'Unknown ISO 8583 presentment MTI.')


def function_for_mti(mti: str, function_code: Any = None) -> str:
    if function_code not in (None, ''):
        return normalize_function(function_code)
    kind = mti_kind(mti)
    if kind == 'second':
        return FN_SECOND
    return FN_FIRST


def compose_ipm1(
    *,
    arn: str,
    mti: str,
    function_code: str,
    pan_masked: str,
    amount: str,
    merchant: str,
    mcc: str,
    interchange: str = '0.00',
    currency: str = 'USD',
) -> str:
    if '|' in merchant or '\n' in merchant:
        raise PresentError('invalid_file', 'Merchant cannot contain pipe or newline.')
    return 'IPM1|%s|%s|%s|%s|%s|%s|%s|%s|%s' % (
        mask_arn(arn), mti, function_code, pan_masked, amount, merchant, mcc, interchange, currency,
    )


def parse_ipm1(line: Any) -> Dict[str, str]:
    text = str(line or '').strip()
    if not text:
        raise PresentError('invalid_file', 'Empty IPM1 line.')
    upper = text.upper()
    if '<?XML' in upper or '<!DOCTYPE' in upper or '<' in text:
        raise PresentError('invalid_file', 'XML/DOCTYPE is rejected.')
    parts = text.split('|')
    if len(parts) < 8 or parts[0].upper() != 'IPM1':
        raise PresentError(
            'invalid_file',
            'IPM1 line must be IPM1|ARN|MTI|FUNCTION|PAN|AMOUNT|MERCHANT|MCC|INTERCHANGE|CURRENCY.',
        )
    while len(parts) < 10:
        parts.append('USD' if len(parts) == 9 else '0.00')
    return {
        'arn': parts[1],
        'mti': parts[2],
        'function_code': parts[3],
        'pan': parts[4],
        'amount': parts[5],
        'merchant': parts[6],
        'mcc': parts[7],
        'interchange': parts[8] or '0.00',
        'currency': parts[9] or 'USD',
    }


def split_ipm1_file(payload: Any) -> List[str]:
    text = str(payload or '')
    if '<?xml' in text.lower() or '<!doctype' in text.lower():
        raise PresentError('invalid_file', 'XML/DOCTYPE is rejected.')
    lines = [line.strip() for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    return [line for line in lines if line]


def _arn_fn_key(arn: str, function_code: str) -> str:
    return '%s:%s' % (arn, function_code)


class SettlementClock:
    """Card-network settlement cycle. Ingest is 24/7; posting follows Fed days + 22:00 ET."""

    def __init__(
        self,
        *,
        cutoff_hour: int = 22,
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

    def is_weekend(self, day) -> bool:
        return day.weekday() >= 5

    def is_holiday(self, day) -> bool:
        return day in us_fed_holidays(day.year) or day in self.extra_holidays

    def is_settlement_day(self, day) -> bool:
        return not self.is_weekend(day) and not self.is_holiday(day)

    def is_after_cutoff(self, ts: float) -> bool:
        local = self.local_dt(ts)
        return (local.hour, local.minute) >= (self.cutoff_hour, self.cutoff_minute)

    def next_settlement_day(self, day):
        cursor = day + timedelta(days=1)
        while not self.is_settlement_day(cursor):
            cursor += timedelta(days=1)
        return cursor

    def value_date(self, ts: float):
        local = self.local_dt(ts)
        day = local.date()
        if self.is_settlement_day(day) and not self.is_after_cutoff(ts):
            return day
        return self.next_settlement_day(day)

    def should_queue(self, ts: float) -> bool:
        local = self.local_dt(ts)
        return (not self.is_settlement_day(local.date())) or self.is_after_cutoff(ts)

    def cycle_date(self, ts: float) -> str:
        return self.value_date(ts).strftime('%Y%m%d')

    def snapshot(self, ts: float) -> Dict[str, Any]:
        local = self.local_dt(ts)
        value = self.value_date(ts)
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'cutoff': '%02d:%02d' % (self.cutoff_hour, self.cutoff_minute),
            'after_cutoff': self.should_queue(ts),
            'settlement_day': self.is_settlement_day(local.date()),
            'value_date': value.isoformat(),
            'cycle_date': value.strftime('%Y%m%d'),
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
class PresentPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_reverse: bool = True
    allow_credit: bool = False
    max_keys: int = 8
    max_presentments: int = 240
    min_amount: Decimal = Decimal('0.50')
    max_amount: Decimal = Decimal('25000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    cutoff_hour: int = 22
    tz_offset_hours: int = -4
    acquirer: str = DEFAULT_ACQUIRER
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'PresentPolicy':
        extra = _env_list('PRESENT_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('PRESENT_ENABLED', True),
            customer_manage=_env_bool('PRESENT_CUSTOMER_MANAGE', True),
            customer_reverse=_env_bool('PRESENT_CUSTOMER_REVERSE', True),
            allow_credit=_env_bool('PRESENT_ALLOW_CREDIT', False),
            max_keys=max(1, _env_int('PRESENT_MAX_KEYS', 8)),
            max_presentments=max(1, _env_int('PRESENT_MAX', 240)),
            min_amount=_env_money('PRESENT_MIN_AMOUNT', '0.50'),
            max_amount=_env_money('PRESENT_MAX_AMOUNT', '25000.00'),
            dual_control_threshold=_env_money('PRESENT_DUAL_CONTROL', '10000.00'),
            cutoff_hour=max(0, min(23, _env_int('PRESENT_CUTOFF_HOUR', 22))),
            tz_offset_hours=_env_int('PRESENT_TZ_OFFSET', -4),
            acquirer=normalize_acquirer(os.environ.get('PRESENT_ACQUIRER', DEFAULT_ACQUIRER)),
            watchlist=watch,
            extra_holidays=_env_list('PRESENT_HOLIDAYS'),
        )


@dataclass
class PresentKey:
    key_id: str
    userid: str
    nickname: str
    brand: str
    last4: str
    fingerprint: str
    default_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'key_id': self.key_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'brand': self.brand,
            'pan_masked': ('*' * 12) + self.last4,
            'last4': self.last4,
            'default_account': self.default_account,
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == KEY_ACTIVE,
            'paused': self.status == KEY_PAUSED,
            'cancelled': self.status == KEY_CANCELLED,
        }


@dataclass
class Presentment:
    presentment_id: str
    arn: str
    mti: str
    function_code: str
    key_id: str
    userid: str
    internal_account: str
    amount: str
    interchange: str
    nickname: str
    merchant: str
    mcc: str
    purpose: str
    brand: str
    last4: str
    status: str
    value_date: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    created_at: float
    updated_at: float
    posted_at: float = 0.0
    reversed_at: float = 0.0
    note: str = ''
    reason: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'presentment_id': self.presentment_id,
            'arn_masked': mask_arn(self.arn),
            'mti': self.mti,
            'function_code': self.function_code,
            'key_id': self.key_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'amount': self.amount,
            'interchange': self.interchange,
            'nickname': self.nickname,
            'merchant': self.merchant,
            'mcc': self.mcc,
            'purpose': self.purpose,
            'brand': self.brand,
            'pan_masked': ('*' * 12) + self.last4 if self.last4 else '****',
            'last4': self.last4,
            'status': self.status,
            'value_date': self.value_date,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'posted_at': self.posted_at,
            'reversed_at': self.reversed_at,
            'note': self.note,
            'reason': self.reason,
            'posted': self.status == PRES_POSTED,
            'queued': self.status == PRES_QUEUED,
            'pending_release': self.status == PRES_PENDING,
            'second': self.function_code == FN_SECOND,
            'open': self.status in OPEN_PRESENTMENTS,
        }


def _clone_key(row: PresentKey) -> PresentKey:
    return PresentKey(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_presentment(row: Presentment) -> Presentment:
    return Presentment(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _key_from_row(row: Any) -> PresentKey:
    return PresentKey(
        key_id=row['key_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        brand=row['brand'],
        last4=row['last4'],
        fingerprint=row['fingerprint'],
        default_account=row['default_account'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
    )


def _presentment_from_row(row: Any) -> Presentment:
    return Presentment(
        presentment_id=row['presentment_id'],
        arn=row['arn'],
        mti=row['mti'],
        function_code=row['function_code'],
        key_id=row['key_id'] or '',
        userid=row['userid'] or '',
        internal_account=row['internal_account'] or '',
        amount=row['amount'],
        interchange=row['interchange'] or '0.00',
        nickname=row['nickname'] or '',
        merchant=row['merchant'],
        mcc=row['mcc'],
        purpose=row['purpose'],
        brand=row['brand'] or '',
        last4=row['last4'] or '',
        status=row['status'],
        value_date=row['value_date'] or '',
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        posted_at=float(row['posted_at'] or 0),
        reversed_at=float(row['reversed_at'] or 0),
        note=row['note'] or '',
        reason=row['reason'] or '',
    )


class MemoryPresentStore:
    def __init__(self) -> None:
        self._keys: Dict[str, PresentKey] = {}
        self._rows: Dict[str, Presentment] = {}
        self._by_arn_fn: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_key(self, row: PresentKey) -> None:
        with self._lock:
            self._keys[row.key_id] = row

    def get_key(self, key_id: str) -> Optional[PresentKey]:
        with self._lock:
            row = self._keys.get(key_id)
            return _clone_key(row) if row else None

    def update_key(self, row: PresentKey) -> None:
        with self._lock:
            self._keys[row.key_id] = row

    def list_keys(self, userid: Optional[str] = None, *, include_cancelled: bool = True) -> List[PresentKey]:
        with self._lock:
            rows = [_clone_key(row) for row in self._keys.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_cancelled:
            rows = [row for row in rows if row.status != KEY_CANCELLED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_key_by_nickname(self, userid: str, nickname: str) -> Optional[PresentKey]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._keys.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_KEYS:
                    return _clone_key(row)
        return None

    def find_key_by_fingerprint(self, fingerprint: str, *, userid: Optional[str] = None) -> Optional[PresentKey]:
        with self._lock:
            rows = [
                row for row in self._keys.values()
                if row.fingerprint == fingerprint and row.status == KEY_ACTIVE
            ]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        rows.sort(key=lambda item: item.created_at)
        return _clone_key(rows[0]) if rows else None

    def put_presentment(self, row: Presentment) -> Presentment:
        with self._lock:
            existing_id = self._by_arn_fn.get(_arn_fn_key(row.arn, row.function_code))
            if existing_id is not None:
                return self._rows[existing_id]
            self._rows[row.presentment_id] = row
            self._by_arn_fn[_arn_fn_key(row.arn, row.function_code)] = row.presentment_id
            return row

    def update_presentment(self, row: Presentment) -> None:
        with self._lock:
            self._rows[row.presentment_id] = row

    def get_presentment(self, presentment_id: str) -> Optional[Presentment]:
        with self._lock:
            row = self._rows.get(presentment_id)
            return _clone_presentment(row) if row else None

    def get_presentment_by_arn(self, arn: str, *, function_code: Optional[str] = None) -> Optional[Presentment]:
        with self._lock:
            if function_code:
                presentment_id = self._by_arn_fn.get(_arn_fn_key(arn, function_code))
                return _clone_presentment(self._rows[presentment_id]) if presentment_id else None
            rows = [row for row in self._rows.values() if row.arn == arn]
        rows.sort(key=lambda item: item.created_at)
        return _clone_presentment(rows[0]) if rows else None

    def list_by_arn(self, arn: str) -> List[Presentment]:
        with self._lock:
            rows = [_clone_presentment(row) for row in self._rows.values() if row.arn == arn]
        rows.sort(key=lambda item: item.created_at)
        return rows

    def list_presentments(self, userid: Optional[str] = None, *, unmatched_only: bool = False) -> List[Presentment]:
        with self._lock:
            rows = [_clone_presentment(row) for row in self._rows.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if unmatched_only:
            rows = [row for row in rows if row.status == PRES_UNMATCHED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def list_unmatched(self) -> List[Presentment]:
        return self.list_presentments(unmatched_only=True)

    def next_arn_sequence(self, cycle_date: str) -> int:
        with self._lock:
            used = [row.arn for row in self._rows.values() if len(row.arn) >= 14 and row.arn[6:14] == cycle_date]
        return len(used) + 1


class SqlitePresentStore:
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
                CREATE TABLE IF NOT EXISTS keys (
                    key_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    brand TEXT NOT NULL,
                    last4 TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
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
                CREATE TABLE IF NOT EXISTS presentments (
                    presentment_id TEXT PRIMARY KEY,
                    arn TEXT NOT NULL,
                    mti TEXT NOT NULL,
                    function_code TEXT NOT NULL,
                    key_id TEXT NOT NULL DEFAULT '',
                    userid TEXT NOT NULL DEFAULT '',
                    internal_account TEXT NOT NULL DEFAULT '',
                    amount TEXT NOT NULL,
                    interchange TEXT NOT NULL DEFAULT '0.00',
                    nickname TEXT NOT NULL DEFAULT '',
                    merchant TEXT NOT NULL,
                    mcc TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    brand TEXT NOT NULL DEFAULT '',
                    last4 TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    value_date TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    posted_at REAL NOT NULL DEFAULT 0,
                    reversed_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    UNIQUE (arn, function_code)
                )
                """
            )
            conn.commit()

    def put_key(self, row: PresentKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO keys (
                    key_id, userid, nickname, brand, last4, fingerprint, default_account,
                    status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.key_id, row.userid, row.nickname, row.brand, row.last4, row.fingerprint,
                    row.default_account, row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_key(self, key_id: str) -> Optional[PresentKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM keys WHERE key_id = ?', (key_id,)).fetchone()
        return _key_from_row(row) if row else None

    def update_key(self, row: PresentKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE keys SET nickname=?, brand=?, last4=?, fingerprint=?, default_account=?,
                    status=?, actor=?, updated_at=?
                WHERE key_id=?
                """,
                (
                    row.nickname, row.brand, row.last4, row.fingerprint, row.default_account,
                    row.status, row.actor, row.updated_at, row.key_id,
                ),
            )
            conn.commit()

    def list_keys(self, userid: Optional[str] = None, *, include_cancelled: bool = True) -> List[PresentKey]:
        sql = 'SELECT * FROM keys'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if not include_cancelled:
            clauses.append("status != 'cancelled'")
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_key_from_row(row) for row in rows]

    def find_key_by_nickname(self, userid: str, nickname: str) -> Optional[PresentKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM keys
                WHERE userid = ? AND lower(nickname) = lower(?)
                  AND status IN ('active', 'paused')
                """,
                (userid, nickname),
            ).fetchone()
        return _key_from_row(row) if row else None

    def find_key_by_fingerprint(self, fingerprint: str, *, userid: Optional[str] = None) -> Optional[PresentKey]:
        sql = "SELECT * FROM keys WHERE fingerprint = ? AND status = 'active'"
        params: List[Any] = [fingerprint]
        if userid is not None:
            sql += ' AND userid = ?'
            params.append(userid)
        sql += ' ORDER BY created_at ASC'
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return _key_from_row(row) if row else None

    def put_presentment(self, row: Presentment) -> Presentment:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM presentments WHERE arn = ? AND function_code = ?',
                (row.arn, row.function_code),
            ).fetchone()
            if existing is not None:
                return _presentment_from_row(existing)
            conn.execute(
                """
                INSERT INTO presentments (
                    presentment_id, arn, mti, function_code, key_id, userid, internal_account,
                    amount, interchange, nickname, merchant, mcc, purpose, brand, last4,
                    status, value_date, actor, releaser, ofac_hit, ofac_match, created_at,
                    updated_at, posted_at, reversed_at, note, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.presentment_id, row.arn, row.mti, row.function_code, row.key_id, row.userid,
                    row.internal_account, row.amount, row.interchange, row.nickname, row.merchant,
                    row.mcc, row.purpose, row.brand, row.last4, row.status, row.value_date,
                    row.actor, row.releaser, row.ofac_hit, row.ofac_match, row.created_at,
                    row.updated_at, row.posted_at, row.reversed_at, row.note, row.reason,
                ),
            )
            conn.commit()
            return row

    def update_presentment(self, row: Presentment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE presentments SET key_id=?, userid=?, internal_account=?, amount=?,
                    interchange=?, nickname=?, merchant=?, mcc=?, purpose=?, brand=?, last4=?,
                    status=?, value_date=?, actor=?, releaser=?, ofac_hit=?, ofac_match=?,
                    updated_at=?, posted_at=?, reversed_at=?, note=?, reason=?, mti=?
                WHERE presentment_id=?
                """,
                (
                    row.key_id, row.userid, row.internal_account, row.amount, row.interchange,
                    row.nickname, row.merchant, row.mcc, row.purpose, row.brand, row.last4,
                    row.status, row.value_date, row.actor, row.releaser, row.ofac_hit,
                    row.ofac_match, row.updated_at, row.posted_at, row.reversed_at, row.note,
                    row.reason, row.mti, row.presentment_id,
                ),
            )
            conn.commit()

    def get_presentment(self, presentment_id: str) -> Optional[Presentment]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM presentments WHERE presentment_id = ?', (presentment_id,),
            ).fetchone()
        return _presentment_from_row(row) if row else None

    def get_presentment_by_arn(self, arn: str, *, function_code: Optional[str] = None) -> Optional[Presentment]:
        sql = 'SELECT * FROM presentments WHERE arn = ?'
        params: List[Any] = [arn]
        if function_code:
            sql += ' AND function_code = ?'
            params.append(function_code)
        sql += ' ORDER BY created_at ASC'
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return _presentment_from_row(row) if row else None

    def list_by_arn(self, arn: str) -> List[Presentment]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM presentments WHERE arn = ? ORDER BY created_at ASC', (arn,),
            ).fetchall()
        return [_presentment_from_row(row) for row in rows]

    def list_presentments(self, userid: Optional[str] = None, *, unmatched_only: bool = False) -> List[Presentment]:
        sql = 'SELECT * FROM presentments'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if unmatched_only:
            clauses.append("status = 'unmatched'")
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_presentment_from_row(row) for row in rows]

    def list_unmatched(self) -> List[Presentment]:
        return self.list_presentments(unmatched_only=True)

    def next_arn_sequence(self, cycle_date: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM presentments WHERE substr(arn, 7, 8) = ?",
                (cycle_date,),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class PresentService:
    def __init__(
        self,
        policy: PresentPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[SettlementClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or SettlementClock(
            cutoff_hour=policy.cutoff_hour,
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise PresentError('present_disabled', 'Card settlement presentment is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise PresentError('present_forbidden', 'Customers cannot manage presentment keys.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise PresentError('present_forbidden', 'Staff only.')

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
            raise PresentError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise PresentError('credit_not_allowed', 'Credit accounts cannot receive card presentments.')

    def _assert_amount(self, dollars: Decimal) -> None:
        if dollars < self.policy.min_amount or dollars > self.policy.max_amount:
            raise PresentError('amount_out_of_range', 'Amount is outside the allowed range.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def add_key(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        pan: Any,
        default_account: Any,
        brand: Any = None,
    ) -> PresentKey:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise PresentError('present_forbidden', 'Not allowed to add keys for this customer.')
        name = normalize_nickname(nickname)
        digits = normalize_pan(pan)
        derived = brand_from_iin(digits)
        if brand not in (None, ''):
            wanted = normalize_brand(brand)
            if wanted != derived:
                raise PresentError('invalid_brand', 'Brand does not match PAN IIN.')
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        fingerprint = pan_fingerprint(digits)
        if self.store.find_key_by_nickname(owner_userid, name) is not None:
            raise PresentError('key_duplicate', 'A card key with that nickname already exists.')
        if self.store.find_key_by_fingerprint(fingerprint, userid=owner_userid) is not None:
            raise PresentError('key_duplicate', 'That card is already enrolled.')
        open_rows = [row for row in self.store.list_keys(owner_userid) if row.status in OPEN_KEYS]
        if len(open_rows) >= self.policy.max_keys:
            raise PresentError('key_limit', 'Card key limit reached.')
        now = float(self.clock())
        row = PresentKey(
            key_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            brand=derived,
            last4=last4(digits),
            fingerprint=fingerprint,
            default_account=account,
            status=KEY_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_key(row)
        self._rematch_unmatched(row)
        return row

    def get_key(self, *, key_id: str, actor: str, actor_type: str) -> PresentKey:
        self._require_enabled()
        row = self.store.get_key(key_id)
        if row is None:
            raise PresentError('key_not_found', 'Card key not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise PresentError('present_forbidden', 'Not allowed to view this card key.')
        return row

    def set_key_status(
        self,
        *,
        key_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> PresentKey:
        self._require_manage(actor_type)
        row = self.get_key(key_id=key_id, actor=actor, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': KEY_PAUSED, 'hold': KEY_PAUSED,
            'resume': KEY_ACTIVE, 'activate': KEY_ACTIVE, 'unpause': KEY_ACTIVE,
            'cancel': KEY_CANCELLED, 'close': KEY_CANCELLED, 'archive': KEY_CANCELLED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {KEY_PAUSED, KEY_ACTIVE, KEY_CANCELLED}:
            raise PresentError('invalid_status', 'Status must be pause, resume, or cancel.')
        if row.status == KEY_CANCELLED:
            raise PresentError('already_cancelled', 'Card key is already cancelled.')
        if wanted == KEY_PAUSED:
            if row.status == KEY_PAUSED:
                raise PresentError('already_paused', 'Card key is already paused.')
            if row.status != KEY_ACTIVE:
                raise PresentError('invalid_status', 'Only an active card key can be paused.')
        elif wanted == KEY_ACTIVE:
            if row.status == KEY_ACTIVE:
                raise PresentError('already_active', 'Card key is already active.')
            if row.status != KEY_PAUSED:
                raise PresentError('invalid_status', 'Only a paused card key can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_key(row)
        if wanted == KEY_ACTIVE:
            self._rematch_unmatched(row)
        return row

    def _rematch_unmatched(self, key: PresentKey) -> None:
        if key.status != KEY_ACTIVE:
            return
        for row in self.store.list_unmatched():
            if row.last4 == key.last4 and (not row.brand or row.brand == key.brand):
                row.key_id = key.key_id
                row.userid = key.userid
                row.internal_account = row.internal_account or key.default_account
                row.nickname = key.nickname
                row.brand = key.brand
                row.last4 = key.last4
                self._evaluate(row, actor=key.actor)

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        amount: Any,
        merchant: Any,
        mcc: Any = None,
        pan: Any = None,
        key_id: Any = None,
        internal_account: Any = None,
        function_code: Any = FN_FIRST,
    ) -> Dict[str, Any]:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise PresentError('present_forbidden', 'Not allowed to preview presentments for this customer.')
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        mcc_code = normalize_mcc(mcc)
        merchant_name = normalize_merchant(merchant)
        ofac = self._screen(merchant_name)
        key = None
        if key_id:
            key = self.get_key(key_id=str(key_id), actor=actor, actor_type=actor_type)
        elif pan:
            digits = normalize_pan(pan)
            key = self.store.find_key_by_fingerprint(pan_fingerprint(digits), userid=owner_userid)
        account = ''
        if key is not None:
            account = normalize_account(internal_account or key.default_account)
            self._assert_internal_account(owner_userid, account)
        now = float(self.clock())
        return {
            'amount': money_str(dollars),
            'interchange': money_str(compute_interchange(dollars, mcc_code)),
            'merchant': merchant_name,
            'mcc': mcc_code,
            'purpose': purpose_from_mcc(mcc_code),
            'function_code': normalize_function(function_code),
            'internal_account': account,
            'key': key.to_dict() if key is not None else None,
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(dollars),
            'queued': self.calendar.should_queue(now),
            'clock': self.calendar.snapshot(now),
        }

    def _new_arn(self, *, arn: Any, now: float) -> str:
        if arn in (None, ''):
            cycle = self.calendar.cycle_date(now)
            value = compose_arn(cycle, self.store.next_arn_sequence(cycle), acquirer=self.policy.acquirer)
            while self.store.get_presentment_by_arn(value) is not None:
                value = compose_arn(cycle, int(value[-9:]) + 1, acquirer=self.policy.acquirer)
            return value
        return normalize_arn(arn)

    def _post(self, row: Presentment, *, actor: str) -> Presentment:
        now = float(self.clock())
        dollars = parse_money(row.amount)
        remark = 'card present %s' % row.merchant
        status = PRES_POSTED
        fail_note = ''
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(row.internal_account, money_str(dollars), remark)
            except Exception as exc:
                status = PRES_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = PRES_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = PRES_FAILED
                    fail_note = str(result)[:240]
        row.status = status
        row.updated_at = now
        row.actor = str(actor)
        if status == PRES_POSTED:
            row.posted_at = now
            row.value_date = self.calendar.value_date(now).isoformat()
        else:
            row.note = fail_note or row.note
        self.store.update_presentment(row)
        if status == PRES_NSF:
            raise PresentError('nsf', 'Insufficient funds to post presentment.', presentment=row)
        if status == PRES_FAILED:
            raise PresentError('failed', 'Presentment posting failed.', presentment=row)
        return row

    def _evaluate(self, row: Presentment, *, actor: str) -> Presentment:
        now = float(self.clock())
        if not row.key_id or not row.userid:
            row.status = PRES_UNMATCHED
            row.updated_at = now
            self.store.update_presentment(row)
            return row
        key = self.store.get_key(row.key_id)
        if key is None or key.status != KEY_ACTIVE:
            row.status = PRES_UNMATCHED
            row.updated_at = now
            self.store.update_presentment(row)
            return row
        if row.ofac_hit and row.status != PRES_POSTED:
            row.status = PRES_HELD
            row.updated_at = now
            self.store.update_presentment(row)
            return row
        dollars = parse_money(row.amount)
        if self._needs_dual_control(dollars) and not row.releaser:
            row.status = PRES_PENDING
            row.updated_at = now
            self.store.update_presentment(row)
            return row
        if self.calendar.should_queue(now):
            row.status = PRES_QUEUED
            row.value_date = self.calendar.value_date(now).isoformat()
            row.updated_at = now
            self.store.update_presentment(row)
            return row
        return self._post(row, actor=actor)

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        amount: Any,
        merchant: Any,
        pan: Any = None,
        mti: Any = MTI_FIRST,
        function_code: Any = None,
        mcc: Any = None,
        arn: Any = None,
        currency: Any = 'USD',
        owner_userid: Any = None,
        key_id: Any = None,
        internal_account: Any = None,
    ) -> Tuple[Presentment, bool]:
        self._require_staff(actor_type)
        normalize_currency(currency)
        mti_value = normalize_mti(mti)
        kind = mti_kind(mti_value)
        fn_code = function_for_mti(mti_value, function_code)
        if kind == 'second':
            fn_code = FN_SECOND
        if arn not in (None, '') and not str(arn).startswith('*'):
            arn_value = normalize_arn(arn)
            if kind == 'reversal':
                target = self._latest_reversible(arn_value)
                if target is not None:
                    return self.reverse(
                        presentment_id=target.presentment_id, actor=actor, actor_type=actor_type,
                    ), False
            existing = self.store.get_presentment_by_arn(arn_value, function_code=fn_code)
            if existing is not None:
                return existing, False
            if fn_code == FN_SECOND:
                first = self.store.get_presentment_by_arn(arn_value, function_code=FN_FIRST)
                if first is None:
                    raise PresentError('presentment_not_found', 'Second presentment requires a first presentment ARN.')
                if first.status not in REPRESENTABLE:
                    raise PresentError('not_representable', 'Second presentment requires a reversed or rejected first presentment.')
        now = float(self.clock())
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        merchant_name = normalize_merchant(merchant)
        mcc_code = normalize_mcc(mcc)
        purpose = purpose_from_mcc(mcc_code)
        ofac = self._screen(merchant_name)
        fingerprint = ''
        brand = ''
        pan_last4 = ''
        if pan not in (None, '') and str(pan).strip() and not str(pan).strip().startswith('*'):
            digits = normalize_pan(pan)
            fingerprint = pan_fingerprint(digits)
            brand = brand_from_iin(digits)
            pan_last4 = last4(digits)
        elif pan not in (None, '') and '*' in str(pan):
            pan_last4 = last4(pan)
        key = None
        if key_id:
            key = self.get_key(key_id=str(key_id), actor=actor, actor_type=actor_type)
        elif fingerprint:
            key = self.store.find_key_by_fingerprint(fingerprint)
        if owner_userid and key is None and fingerprint:
            key = self.store.find_key_by_fingerprint(fingerprint, userid=str(owner_userid))
        if key is not None and key.status != KEY_ACTIVE:
            key = None
        open_rows = self.store.list_presentments()
        if len(open_rows) >= self.policy.max_presentments:
            raise PresentError('presentment_limit', 'Card presentment limit reached.')
        arn_value = self._new_arn(arn=arn if arn not in (None, '') and not str(arn).startswith('*') else None, now=now)
        account = ''
        userid = str(owner_userid or '')
        nickname = ''
        resolved_key_id = ''
        if key is not None:
            resolved_key_id = key.key_id
            userid = key.userid
            nickname = key.nickname
            brand = key.brand
            pan_last4 = key.last4
            account = normalize_account(internal_account or key.default_account)
            self._assert_internal_account(userid, account)
        if kind == 'second':
            mti_value = MTI_SECOND
            fn_code = FN_SECOND
        row = Presentment(
            presentment_id=uuid.uuid4().hex,
            arn=arn_value,
            mti=mti_value if kind != 'reversal' else MTI_FIRST,
            function_code=fn_code,
            key_id=resolved_key_id,
            userid=userid,
            internal_account=account,
            amount=money_str(dollars),
            interchange=money_str(compute_interchange(dollars, mcc_code)),
            nickname=nickname,
            merchant=merchant_name,
            mcc=mcc_code,
            purpose=purpose,
            brand=brand,
            last4=pan_last4,
            status=PRES_UNMATCHED,
            value_date=self.calendar.value_date(now).isoformat(),
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
        )
        stored = self.store.put_presentment(row)
        if stored.presentment_id != row.presentment_id:
            return stored, False
        evaluated = self._evaluate(row, actor=actor)
        if kind == 'reversal' and evaluated.status == PRES_POSTED:
            return self.reverse(presentment_id=evaluated.presentment_id, actor=actor, actor_type=actor_type), True
        if kind == 'reversal' and evaluated.status in REJECTABLE:
            return self.reject(presentment_id=evaluated.presentment_id, actor=actor, actor_type=actor_type), True
        return evaluated, True

    def ingest_file(self, *, actor: str, actor_type: str, payload: Any) -> List[Presentment]:
        self._require_staff(actor_type)
        rows = []
        for line in split_ipm1_file(payload):
            parsed = parse_ipm1(line)
            row, _created = self.ingest(
                actor=actor,
                actor_type=actor_type,
                amount=parsed['amount'],
                merchant=parsed['merchant'],
                pan=parsed['pan'] if not str(parsed['pan']).startswith('*') else None,
                mti=parsed['mti'],
                function_code=parsed['function_code'],
                mcc=parsed['mcc'],
                arn=parsed['arn'] if not str(parsed['arn']).startswith('*') else None,
                currency=parsed['currency'],
            )
            rows.append(row)
        return rows

    def get_presentment(self, *, presentment_id: str, actor: str, actor_type: str) -> Presentment:
        self._require_enabled()
        row = self.store.get_presentment(presentment_id)
        if row is None:
            raise PresentError('presentment_not_found', 'Card presentment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise PresentError('present_forbidden', 'Not allowed to view this presentment.')
        return row

    def _latest_reversible(self, arn: str) -> Optional[Presentment]:
        rows = self.store.list_by_arn(arn)
        posted = [row for row in rows if row.status == PRES_POSTED]
        if posted:
            return posted[-1]
        open_rows = [row for row in rows if row.status in REJECTABLE]
        return open_rows[-1] if open_rows else None

    def claim(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        presentment_id: str,
        pan: Any = None,
        last4_value: Any = None,
        brand: Any = None,
        default_account: Any = None,
        nickname: Any = None,
    ) -> Presentment:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise PresentError('present_forbidden', 'Not allowed to claim presentments for this customer.')
        row = self.store.get_presentment(presentment_id)
        if row is None:
            raise PresentError('presentment_not_found', 'Card presentment not found.')
        if row.status != PRES_UNMATCHED:
            raise PresentError('not_claimable', 'Only unmatched presentments can be claimed.')
        presented_last4 = last4(last4_value or '')
        presented_brand = normalize_brand(brand) if brand not in (None, '') else row.brand
        fingerprint = ''
        if pan not in (None, ''):
            digits = normalize_pan(pan)
            fingerprint = pan_fingerprint(digits)
            presented_last4 = last4(digits)
            presented_brand = brand_from_iin(digits)
        if not presented_last4:
            raise PresentError('invalid_pan', 'Last4 or PAN is required to claim.')
        if row.last4 and row.last4 != presented_last4:
            raise PresentError('not_claimable', 'Claimed last4 does not match this presentment.')
        if row.brand and presented_brand and row.brand != presented_brand:
            raise PresentError('not_claimable', 'Claimed brand does not match this presentment.')
        key = None
        if fingerprint:
            key = self.store.find_key_by_fingerprint(fingerprint, userid=owner_userid)
        if key is None and presented_last4:
            for item in self.store.list_keys(owner_userid, include_cancelled=False):
                if item.status == KEY_ACTIVE and item.last4 == presented_last4:
                    if not presented_brand or item.brand == presented_brand:
                        key = item
                        break
        if key is None:
            account = normalize_account(default_account or row.internal_account or '0')
            if account == '0':
                raise PresentError('invalid_account', 'Destination account is required to enroll a claim.')
            if pan in (None, ''):
                raise PresentError('invalid_pan', 'PAN is required to enroll a claim.')
            key = self.add_key(
                owner_userid=owner_userid,
                actor=actor,
                actor_type=actor_type,
                nickname=nickname or row.merchant[:40],
                pan=pan,
                default_account=account,
                brand=presented_brand,
            )
        row.key_id = key.key_id
        row.userid = key.userid
        row.internal_account = key.default_account
        row.nickname = key.nickname
        row.brand = key.brand
        row.last4 = key.last4
        return self._evaluate(row, actor=actor)

    def assign(
        self,
        *,
        presentment_id: str,
        actor: str,
        actor_type: str,
        owner_userid: str,
        key_id: Any = None,
        pan: Any = None,
        default_account: Any = None,
        nickname: Any = None,
    ) -> Presentment:
        self._require_staff(actor_type)
        row = self.store.get_presentment(presentment_id)
        if row is None:
            raise PresentError('presentment_not_found', 'Card presentment not found.')
        if row.status != PRES_UNMATCHED:
            raise PresentError('not_assignable', 'Only unmatched presentments can be assigned.')
        key = None
        if key_id:
            key = self.get_key(key_id=str(key_id), actor=actor, actor_type=actor_type)
            if key.userid != owner_userid:
                raise PresentError('present_forbidden', 'Key does not belong to this customer.')
        elif pan not in (None, ''):
            digits = normalize_pan(pan)
            key = self.store.find_key_by_fingerprint(pan_fingerprint(digits), userid=owner_userid)
            if key is None:
                account = normalize_account(default_account or row.internal_account)
                key = self.add_key(
                    owner_userid=owner_userid,
                    actor=actor,
                    actor_type=actor_type,
                    nickname=nickname or row.merchant[:40],
                    pan=pan,
                    default_account=account,
                )
        if key is None:
            raise PresentError('missing_key', 'Assign requires a key or PAN.')
        row.key_id = key.key_id
        row.userid = key.userid
        row.internal_account = key.default_account
        row.nickname = key.nickname
        row.brand = key.brand
        row.last4 = key.last4
        return self._evaluate(row, actor=actor)

    def override_ofac(self, *, presentment_id: str, actor: str, actor_type: str, note: Any = '') -> Presentment:
        self._require_staff(actor_type)
        row = self.get_presentment(presentment_id=presentment_id, actor=actor, actor_type=actor_type)
        if row.status != PRES_HELD:
            raise PresentError('not_overridable', 'Only an OFAC hold can be overridden.')
        row.ofac_hit = 0
        row.ofac_match = ''
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        return self._evaluate(row, actor=actor)

    def release(self, *, presentment_id: str, actor: str, actor_type: str) -> Presentment:
        self._require_staff(actor_type)
        row = self.get_presentment(presentment_id=presentment_id, actor=actor, actor_type=actor_type)
        if row.status not in RELEASABLE:
            raise PresentError('not_releasable', 'Only a pending, held, or queued presentment can be released.')
        if row.status == PRES_HELD:
            raise PresentError('ofac_hold', 'OFAC hold must be overridden before release.')
        if row.actor == str(actor) and self._needs_dual_control(parse_money(row.amount)):
            raise PresentError('same_approver', 'A different employee must release this presentment.')
        row.releaser = str(actor)
        if row.status == PRES_QUEUED and self.calendar.should_queue(float(self.clock())):
            row.updated_at = float(self.clock())
            self.store.update_presentment(row)
            return row
        return self._evaluate(row, actor=actor)

    def reject(self, *, presentment_id: str, actor: str, actor_type: str, reason: Any = 'other', note: Any = '') -> Presentment:
        self._require_staff(actor_type)
        row = self.get_presentment(presentment_id=presentment_id, actor=actor, actor_type=actor_type)
        if row.status not in REJECTABLE:
            raise PresentError('not_rejectable', 'This presentment cannot be rejected.')
        now = float(self.clock())
        row.status = PRES_REJECTED
        row.reason = normalize_note(reason, limit=40)
        row.note = normalize_note(note) or row.note
        row.updated_at = now
        row.actor = str(actor)
        self.store.update_presentment(row)
        return row

    def reverse(self, *, presentment_id: str, actor: str, actor_type: str, note: Any = '') -> Presentment:
        self._require_enabled()
        row = self.get_presentment(presentment_id=presentment_id, actor=actor, actor_type=actor_type)
        if row.status != PRES_POSTED:
            raise PresentError('not_reversible', 'Only a posted presentment can be reversed.')
        now = float(self.clock())
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_reverse:
                raise PresentError('present_forbidden', 'Customers cannot reverse presentments.')
            if now - row.posted_at > CUSTOMER_REVERSE_HOURS * 3600:
                raise PresentError('return_window_closed', 'Customer reversal window is 24 hours.')
        elif now - row.posted_at > STAFF_REVERSE_DAYS * 86400:
            raise PresentError('return_window_closed', 'Staff reversal window is 90 days.')
        dollars = parse_money(row.amount)
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(row.internal_account, money_str(dollars), 'card reverse %s' % row.arn[-12:])
            except Exception as exc:
                raise PresentError('reverse_failed', str(exc)[:240], presentment=row) from exc
            kind = _classify_money_result(result)
            if kind != 'ok':
                raise PresentError('reverse_failed', str(result)[:240], presentment=row)
        row.status = PRES_REVERSED
        row.reversed_at = now
        row.updated_at = now
        row.mti = MTI_REVERSAL
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        self.store.update_presentment(row)
        return row

    def export_presentment(self, *, presentment_id: str, actor: str, actor_type: str) -> str:
        self._require_staff(actor_type)
        row = self.get_presentment(presentment_id=presentment_id, actor=actor, actor_type=actor_type)
        if row.status == PRES_UNMATCHED:
            raise PresentError('not_exportable', 'Unmatched presentments cannot be exported.')
        return compose_ipm1(
            arn=row.arn,
            mti=row.mti,
            function_code=row.function_code,
            pan_masked=mask_pan(row.last4.rjust(16, '*')),
            amount=row.amount,
            merchant=row.merchant,
            mcc=row.mcc,
            interchange=row.interchange,
        )

    def run_due(self, userid: Optional[str] = None) -> List[Presentment]:
        self._require_enabled()
        now = float(self.clock())
        changed = []
        rows = self.store.list_presentments(userid) if userid else self.store.list_presentments()
        for row in rows:
            if row.status == PRES_UNMATCHED and now - row.created_at >= UNMATCHED_HOURS * 3600:
                row.status = PRES_EXPIRED
                row.updated_at = now
                self.store.update_presentment(row)
                changed.append(row)
                continue
            if row.status == PRES_QUEUED and not self.calendar.should_queue(now):
                try:
                    changed.append(self._evaluate(row, actor=row.actor))
                except PresentError:
                    refreshed = self.store.get_presentment(row.presentment_id)
                    if refreshed is not None:
                        changed.append(refreshed)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.run_due(userid)
        keys = self.store.list_keys(userid)
        rows = self.store.list_presentments(userid)
        unmatched = [
            row for row in self.store.list_unmatched()
            if not userid or row.last4 in {key.last4 for key in keys}
        ]
        posted_ytd = Decimal('0.00')
        interchange_ytd = Decimal('0.00')
        queued_total = Decimal('0.00')
        for row in rows:
            amount = parse_money(row.amount, allow_zero=True)
            if row.status == PRES_POSTED:
                posted_ytd += amount
                interchange_ytd += parse_money(row.interchange, allow_zero=True)
            elif row.status == PRES_QUEUED:
                queued_total += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clock': self.calendar.snapshot(now),
            'keys': [row.to_dict() for row in keys[:40]],
            'presentments': [row.to_dict() for row in rows[:40]],
            'unmatched': [row.to_dict() for row in unmatched[:40]] if actor_type in EMPLOYEE_ROLES else [],
            'ytd_posted': money_str(posted_ytd),
            'ytd_interchange': money_str(interchange_ytd),
            'queued_total': money_str(queued_total),
            'active_count': sum(1 for row in keys if row.status == KEY_ACTIVE),
            'open_count': sum(1 for row in rows if row.status in OPEN_PRESENTMENTS),
        }


_SERVICE: Optional[PresentService] = None


def set_service(service: Optional[PresentService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[PresentService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('PRESENT_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryPresentStore()
    path = os.environ.get('PRESENT_DB', DEFAULT_STORE_PATH)
    return SqlitePresentStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[SettlementClock] = None,
) -> PresentService:
    if store is None:
        store = default_store()
    return PresentService(
        PresentPolicy.from_env(),
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
        'key_duplicate': 409,
        'key_limit': 409,
        'presentment_limit': 409,
        'already_cancelled': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_posted': 409,
        'already_reversed': 409,
        'nsf': 409,
        'failed': 409,
        'reverse_failed': 409,
        'present_forbidden': 403,
        'present_disabled': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_claimable': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_reversible': 403,
        'not_representable': 403,
        'not_exportable': 403,
        'void_window_closed': 403,
        'return_window_closed': 403,
        'key_not_found': 404,
        'presentment_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_pan': 400,
        'invalid_brand': 400,
        'invalid_mcc': 400,
        'invalid_mti': 400,
        'invalid_arn': 400,
        'invalid_function': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_currency': 400,
        'invalid_reference': 400,
        'invalid_file': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_key': 400,
        'missing_presentment': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: PresentError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('presentment') is not None:
        body['presentment'] = exc.extra['presentment'].to_dict()
    if exc.extra.get('key') is not None:
        body['key'] = exc.extra['key'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except PresentError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: PresentService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'CardPresents': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_add_key(service: PresentService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.add_key(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname'),
            pan=values.get('pan') or values.get('card_number'),
            default_account=values.get('default_account') or values.get('account'),
            brand=values.get('brand'),
        )
        return jsonify({
            'message': 'Card key added',
            'key': row.to_dict(),
            'CardPresents': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _key_status_route(service: PresentService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    key_id = str(values.get('key_id') or '').strip()
    if not key_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_key'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_key_status(
            key_id=key_id, actor=userid, actor_type=actor_type, status=status,
        )
        return jsonify({
            'message': ok_message,
            'key': row.to_dict(),
            'CardPresents': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: PresentService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        preview = service.preview(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            amount=values.get('amount'),
            merchant=values.get('merchant') or values.get('name'),
            mcc=values.get('mcc'),
            pan=values.get('pan'),
            key_id=values.get('key_id'),
            internal_account=values.get('account') or values.get('internal_account'),
            function_code=values.get('function_code') or FN_FIRST,
        )
        return jsonify({'preview': preview, 'CardPresents': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest(service: PresentService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row, created = service.ingest(
            actor=userid,
            actor_type=actor_type,
            amount=values.get('amount'),
            merchant=values.get('merchant') or values.get('name'),
            pan=values.get('pan') or values.get('card_number'),
            mti=values.get('mti') or MTI_FIRST,
            function_code=values.get('function_code'),
            mcc=values.get('mcc'),
            arn=values.get('arn'),
            currency=values.get('currency') or 'USD',
            owner_userid=values.get('customer_id'),
            key_id=values.get('key_id'),
            internal_account=values.get('account'),
        )
        owner = row.userid or str(values.get('customer_id') or userid)
        return jsonify({
            'message': 'Card presentment ingested' if created else 'Card presentment already posted',
            'presentment': row.to_dict(),
            'CardPresents': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_file(service: PresentService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    payload = values.get('file') or values.get('payload') or values.get('body')
    if not payload:
        return jsonify({'message': 'Some data missing', 'error': 'missing_file'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        rows = service.ingest_file(actor=userid, actor_type=actor_type, payload=payload)
        owner = str(values.get('customer_id') or userid)
        return jsonify({
            'message': 'Card presentment file ingested',
            'presentments': [row.to_dict() for row in rows],
            'CardPresents': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def handle_claim(service: PresentService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    presentment_id = str(values.get('presentment_id') or '').strip()
    if not presentment_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_presentment'}), 400
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values) or userid

    def _run():
        row = service.claim(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            presentment_id=presentment_id,
            pan=values.get('pan'),
            last4_value=values.get('last4'),
            brand=values.get('brand'),
            default_account=values.get('default_account') or values.get('account'),
            nickname=values.get('nickname'),
        )
        return jsonify({
            'message': 'Card presentment claimed',
            'presentment': row.to_dict(),
            'CardPresents': service.snapshot(row.userid or owner, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_assign(service: PresentService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    presentment_id = str(values.get('presentment_id') or '').strip()
    if not presentment_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_presentment'}), 400
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.assign(
            presentment_id=presentment_id,
            actor=userid,
            actor_type=actor_type,
            owner_userid=owner,
            key_id=values.get('key_id'),
            pan=values.get('pan'),
            default_account=values.get('default_account') or values.get('account'),
            nickname=values.get('nickname'),
        )
        return jsonify({
            'message': 'Card presentment assigned',
            'presentment': row.to_dict(),
            'CardPresents': service.snapshot(row.userid or owner, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_present_route(service: PresentService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    presentment_id = str(values.get('presentment_id') or '').strip()
    if not presentment_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_presentment'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'release':
            row = service.release(presentment_id=presentment_id, actor=userid, actor_type=actor_type)
            message = 'Card presentment released'
        elif action == 'reject':
            row = service.reject(
                presentment_id=presentment_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Card presentment rejected'
        elif action == 'override':
            row = service.override_ofac(
                presentment_id=presentment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'reverse':
            row = service.reverse(
                presentment_id=presentment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Card presentment reversed'
        elif action == 'export':
            body = service.export_presentment(presentment_id=presentment_id, actor=userid, actor_type=actor_type)
            row = service.get_presentment(presentment_id=presentment_id, actor=userid, actor_type=actor_type)
            return jsonify({
                'message': 'Card presentment exported',
                'file': body,
                'presentment': row.to_dict(),
                'CardPresents': service.snapshot(row.userid or userid, actor=userid, actor_type=actor_type),
            }), 200
        else:
            raise PresentError('invalid_status', 'Unknown presentment action.')
        return jsonify({
            'message': message,
            'presentment': row.to_dict(),
            'CardPresents': service.snapshot(row.userid or userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: PresentService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'CardPresents': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_present_routes(app, service: PresentService) -> None:
    @app.route('/listCardPresents', methods=['POST', 'GET'])
    def list_card_presents_route():
        return handle_list(service)

    @app.route('/listCardPresentKeys', methods=['POST', 'GET'])
    def list_card_present_keys_route():
        return handle_list(service)

    @app.route('/listUnmatchedCardPresents', methods=['POST', 'GET'])
    def list_unmatched_card_presents_route():
        return handle_list(service)

    @app.route('/addCardPresentKey', methods=['POST', 'GET'])
    def add_card_present_key_route():
        return handle_add_key(service)

    @app.route('/pauseCardPresentKey', methods=['POST', 'GET'])
    def pause_card_present_key_route():
        return _key_status_route(service, KEY_PAUSED, 'Card key paused')

    @app.route('/resumeCardPresentKey', methods=['POST', 'GET'])
    def resume_card_present_key_route():
        return _key_status_route(service, KEY_ACTIVE, 'Card key resumed')

    @app.route('/cancelCardPresentKey', methods=['POST', 'GET'])
    def cancel_card_present_key_route():
        return _key_status_route(service, KEY_CANCELLED, 'Card key cancelled')

    @app.route('/previewCardPresent', methods=['POST', 'GET'])
    def preview_card_present_route():
        return handle_preview(service)

    @app.route('/ingestCardPresent', methods=['POST', 'GET'])
    def ingest_card_present_route():
        return handle_ingest(service)

    @app.route('/ingestCardPresentFile', methods=['POST', 'GET'])
    def ingest_card_present_file_route():
        return handle_ingest_file(service)

    @app.route('/claimCardPresent', methods=['POST', 'GET'])
    def claim_card_present_route():
        return handle_claim(service)

    @app.route('/assignCardPresent', methods=['POST', 'GET'])
    def assign_card_present_route():
        return handle_assign(service)

    @app.route('/overrideCardPresentOfac', methods=['POST', 'GET'])
    def override_card_present_ofac_route():
        return _staff_present_route(service, 'override')

    @app.route('/releaseCardPresent', methods=['POST', 'GET'])
    def release_card_present_route():
        return _staff_present_route(service, 'release')

    @app.route('/rejectCardPresent', methods=['POST', 'GET'])
    def reject_card_present_route():
        return _staff_present_route(service, 'reject')

    @app.route('/reverseCardPresent', methods=['POST', 'GET'])
    def reverse_card_present_route():
        return _staff_present_route(service, 'reverse')

    @app.route('/requestCardPresentReverse', methods=['POST', 'GET'])
    def request_card_present_reverse_route():
        return _staff_present_route(service, 'reverse')

    @app.route('/exportCardPresent', methods=['POST', 'GET'])
    def export_card_present_route():
        return _staff_present_route(service, 'export')

    @app.route('/runDueCardPresents', methods=['POST', 'GET'])
    def run_due_card_presents_route():
        return handle_run_due(service)

