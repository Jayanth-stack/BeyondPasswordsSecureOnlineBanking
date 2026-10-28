"""Issuer Account Updater (Visa VAU / Mastercard ABU).

Customers enroll issued PANs. Reissue, expiry, close, and contact events
produce updater advice; participating merchants inquire against the
directory. Independent of push-to-card OCT (PR #155), card 3DS (PR #153),
card settlement presentment (PR #151), card authorization holds (PR #149),
chargeback representment (PR #109), outbound Fedwire (PR #73), and ACH
linking (PR #68). Existing `/fundTransfer`, `/withdrawAmount`, and
`/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- PAN Luhn + IIN brand + SHA-256 fingerprint (never ABA / IMAD / ARN)
- VAU/ABU reason codes (A/E/C/Q/P)
- Merchant BID directory
- VAU reference identifiers (not IMAD / RRN / STAN / CAVV)
- Visa weekly Friday 22:00 ET clock vs Mastercard 24/7 (not Fed 17:00)
- VAU1 pipe compose/parse (XML/DOCTYPE rejected)
- Dual-control for PAN-change (A) and closed (C), not an amount threshold

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Full PAN, fingerprint, BID, and VAU reference never appear in to_dict / snapshots.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
KEY_ACTIVE = 'active'
KEY_PAUSED = 'paused'
KEY_CANCELLED = 'cancelled'
KEY_STATUSES = frozenset({KEY_ACTIVE, KEY_PAUSED, KEY_CANCELLED})
OPEN_KEYS = frozenset({KEY_ACTIVE, KEY_PAUSED})
MERCH_ACTIVE = 'active'
MERCH_PAUSED = 'paused'
MERCH_ARCHIVED = 'archived'
OPEN_MERCH = frozenset({MERCH_ACTIVE, MERCH_PAUSED})
ADV_HELD = 'held'
ADV_QUEUED = 'queued'
ADV_PENDING = 'pending_release'
ADV_POSTED = 'posted'
ADV_REJECTED = 'rejected'
ADV_CANCELLED = 'cancelled'
ADV_REVERSED = 'reversed'
ADV_UNMATCHED = 'unmatched'
ADV_NSF = 'nsf'
ADV_FAILED = 'failed'
ADV_EXPIRED = 'expired'
ADV_STATUSES = frozenset({
    ADV_HELD, ADV_QUEUED, ADV_PENDING, ADV_POSTED, ADV_REJECTED, ADV_CANCELLED,
    ADV_REVERSED, ADV_UNMATCHED, ADV_NSF, ADV_FAILED, ADV_EXPIRED,
})
OPEN_ADVICES = frozenset({ADV_HELD, ADV_QUEUED, ADV_PENDING, ADV_UNMATCHED})
CANCELABLE = frozenset({ADV_HELD, ADV_QUEUED, ADV_PENDING})
FEE_NONE = 'none'
FEE_COLLECTED = 'collected'
FEE_WAIVED = 'waived'
FEE_NSF = 'nsf'
REASON_A = 'A'
REASON_E = 'E'
REASON_C = 'C'
REASON_Q = 'Q'
REASON_P = 'P'
REASONS = frozenset({REASON_A, REASON_E, REASON_C, REASON_Q, REASON_P})
ISSUER_REASONS = frozenset({REASON_A, REASON_E, REASON_C, REASON_Q})
SENSITIVE_REASONS = frozenset({REASON_A, REASON_C})
REASON_ALIASES = {
    'account': REASON_A, 'reissue': REASON_A, 'pan_change': REASON_A,
    'account_update': REASON_A, 'new_account': REASON_A, 'a': REASON_A,
    'expiry': REASON_E, 'expiration': REASON_E, 'date': REASON_E, 'e': REASON_E,
    'closed': REASON_C, 'close': REASON_C, 'cancelled': REASON_C, 'c': REASON_C,
    'contact': REASON_Q, 'query': REASON_Q, 'q': REASON_Q,
    'participating': REASON_P, 'no_change': REASON_P, 'inquiry': REASON_P, 'p': REASON_P,
}
NETWORK_VISA = 'visa_vau'
NETWORK_MC = 'mastercard_abu'
BRAND_VISA = 'visa'
BRAND_MC = 'mastercard'
SOURCE_ISSUER = 'issuer'
SOURCE_MERCHANT = 'merchant'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/vau.sqlite'
CUSTOMER_REVERSE_SECONDS = 24 * 3600
STAFF_REVERSE_SECONDS = 90 * 24 * 3600
UNMATCHED_TTL_SECONDS = 24 * 3600


class VauError(ValueError):
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
        raise VauError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise VauError('invalid_name', 'Legal name must be 2-80 characters.')
    return text


def luhn_check_digit_ok(digits: str) -> bool:
    """ISO/IEC 7812 Luhn checksum. Not an ABA routing check."""
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


def normalize_pan(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not (13 <= len(digits) <= 19):
        raise VauError('invalid_pan', 'PAN must be 13-19 digits.')
    if not luhn_check_digit_ok(digits):
        raise VauError('invalid_pan', 'PAN failed Luhn checksum.')
    return digits


def mask_pan(pan: str) -> str:
    digits = ''.join(ch for ch in str(pan or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    brand = ''
    try:
        brand = brand_from_iin(digits)
    except VauError:
        brand = ''
    prefix = brand + ' ' if brand else ''
    return '%s****%s' % (prefix, digits[-4:])


def pan_fingerprint(pan: str) -> str:
    return hashlib.sha256(str(pan).encode('utf-8')).hexdigest()


def brand_from_iin(pan: str) -> str:
    digits = ''.join(ch for ch in str(pan or '') if ch.isdigit())
    if not digits:
        raise VauError('invalid_brand', 'PAN is required to derive brand.')
    if digits.startswith('4'):
        return BRAND_VISA
    two = int(digits[:2]) if len(digits) >= 2 else 0
    four = int(digits[:4]) if len(digits) >= 4 else 0
    if 51 <= two <= 55 or 2221 <= four <= 2720:
        return BRAND_MC
    if digits.startswith('34') or digits.startswith('37'):
        raise VauError('invalid_brand', 'American Express is not on VAU/ABU.')
    if digits.startswith('6011') or digits.startswith('65') or 644 <= int(digits[:3] or '0') <= 649:
        raise VauError('invalid_brand', 'Discover is not on VAU/ABU.')
    raise VauError('invalid_brand', 'Unsupported card brand.')


def network_for_brand(brand: str) -> str:
    if brand == BRAND_VISA:
        return NETWORK_VISA
    if brand == BRAND_MC:
        return NETWORK_MC
    raise VauError('invalid_brand', 'Unsupported updater network.')


def normalize_expiry(value: Any, *, now_ts: Optional[float] = None, require_future: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) == 6 and digits[:2].isdigit():
        # MMYYYY → MMYY
        digits = digits[:2] + digits[4:]
    if len(digits) != 4:
        raise VauError('invalid_expiry', 'Expiry must be MMYY.')
    month = int(digits[:2])
    year = int(digits[2:])
    if month < 1 or month > 12:
        raise VauError('invalid_expiry', 'Expiry month must be 01-12.')
    if require_future and now_ts is not None:
        local = datetime.fromtimestamp(float(now_ts), tz=timezone.utc)
        current = (local.year % 100) * 100 + local.month
        candidate = year * 100 + month
        if candidate < current:
            raise VauError('invalid_expiry', 'Expiry is in the past.')
    return '%02d%02d' % (month, year)


def mask_expiry(expiry: str) -> str:
    digits = ''.join(ch for ch in str(expiry or '') if ch.isdigit())
    if len(digits) < 2:
        return '**'
    return '**' + digits[-2:]


def normalize_reason(value: Any, *, allow_participating: bool = False) -> str:
    text = str(value or '').strip().lower().replace('-', '_').replace(' ', '_')
    text = REASON_ALIASES.get(text, text.upper() if len(text) == 1 else text)
    if text in REASONS:
        reason = text
    elif text.upper() in REASONS:
        reason = text.upper()
    else:
        raise VauError('invalid_reason', 'Unknown updater reason code.')
    if reason == REASON_P and not allow_participating:
        raise VauError('invalid_reason', 'Participating (P) is merchant-inquiry only.')
    return reason


def normalize_bid(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not (6 <= len(digits) <= 11):
        raise VauError('invalid_bid', 'Merchant BID must be 6-11 digits.')
    return digits


def mask_bid(bid: str) -> str:
    digits = ''.join(ch for ch in str(bid or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return '****' + digits[-4:]


def compose_vau_ref(cycle_date: str, sequence: int) -> str:
    """VAU reference: VAU + YYYYMMDD + 6-digit sequence. Not IMAD/ARN/RRN/STAN."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise VauError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise VauError('invalid_reference', 'VAU sequence out of range.')
    return 'VAU%s%06d' % (day, seq)


def normalize_vau_ref(value: Any) -> str:
    text = str(value or '').strip().upper()
    if not (text.startswith('VAU') and len(text) == 17 and text[3:].isdigit()):
        raise VauError('invalid_reference', 'VAU reference must be VAUYYYYMMDD######.')
    return text


def mask_vau_ref(ref: str) -> str:
    text = str(ref or '')
    if len(text) < 4:
        return 'VAU****'
    return 'VAU****' + text[-4:]


def compute_fee(fee: Decimal, *, waived: bool = False) -> Decimal:
    if waived:
        return Decimal('0.00')
    return fee.quantize(MONEY_QUANTUM)


def compose_vau1(
    *,
    reason: str,
    pan: str,
    expiry: str,
    brand: str,
    network: str,
    bid: str = '',
    merchant: str = '',
    reference: str = '',
    status: str = ADV_POSTED,
) -> str:
    return 'VAU1|%s|%s|%s|%s|%s|%s|%s|%s|%s' % (
        reason, pan, expiry, brand, network, bid, merchant.replace('|', ' '),
        reference, status,
    )


def parse_vau1(line: str) -> Dict[str, str]:
    text = str(line or '').strip()
    lowered = text.lower()
    if '<?xml' in lowered or '<!doctype' in lowered or '<xml' in lowered:
        raise VauError('invalid_file', 'XML/DOCTYPE is not a valid VAU1 record.')
    if not text:
        raise VauError('invalid_file', 'Empty VAU1 record.')
    if not text.startswith('VAU1|'):
        raise VauError('invalid_file', 'Record must start with VAU1|.')
    parts = text.split('|')
    if len(parts) < 10:
        raise VauError('invalid_file', 'VAU1 record is missing fields.')
    pan = parts[2]
    if '*' in pan:
        raise VauError('invalid_pan', 'Ingest requires a full PAN, not a mask.')
    return {
        'reason': parts[1],
        'pan': pan,
        'expiry': parts[3],
        'brand': parts[4],
        'network': parts[5],
        'bid': parts[6],
        'merchant': parts[7],
        'reference': parts[8],
        'status': parts[9],
    }


def split_vau1_file(payload: Any) -> List[str]:
    text = str(payload or '')
    lowered = text.lower()
    if '<?xml' in lowered or '<!doctype' in lowered or '<xml' in lowered:
        raise VauError('invalid_file', 'XML/DOCTYPE files are rejected.')
    lines = []
    for raw in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        item = raw.strip()
        if item:
            lines.append(item)
    if not lines:
        raise VauError('invalid_file', 'VAU1 file is empty.')
    return lines


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


class UpdaterClock:
    """Visa weekly Friday 22:00 ET file vs Mastercard ABU 24/7. Not Fed 17:00."""

    def __init__(
        self,
        *,
        cutoff_hour: int = 22,
        cutoff_minute: int = 0,
        tz_offset_hours: int = -4,
        visa_weekday: int = 4,
    ) -> None:
        self.cutoff_hour = int(cutoff_hour)
        self.cutoff_minute = int(cutoff_minute)
        self.tz_offset_hours = int(tz_offset_hours)
        self.visa_weekday = int(visa_weekday)
        self.tz = timezone(timedelta(hours=self.tz_offset_hours))

    def local_dt(self, ts: float) -> datetime:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(self.tz)

    def is_visa_window(self, ts: float) -> bool:
        local = self.local_dt(ts)
        if local.weekday() != self.visa_weekday:
            return False
        return (local.hour, local.minute) < (self.cutoff_hour, self.cutoff_minute)

    def is_open(self, ts: float, network: str) -> bool:
        if network == NETWORK_MC:
            return True
        return self.is_visa_window(ts)

    def next_visa_friday(self, ts: float) -> datetime:
        local = self.local_dt(ts)
        day = local.date()
        if self.is_visa_window(ts):
            return datetime(day.year, day.month, day.day, tzinfo=self.tz)
        ahead = (self.visa_weekday - day.weekday()) % 7
        if ahead == 0:
            ahead = 7
        nxt = day + timedelta(days=ahead)
        return datetime(nxt.year, nxt.month, nxt.day, tzinfo=self.tz)

    def cycle_date(self, ts: float, network: str) -> str:
        if network == NETWORK_MC or self.is_visa_window(ts):
            return self.local_dt(ts).date().strftime('%Y%m%d')
        return self.next_visa_friday(ts).date().strftime('%Y%m%d')

    def snapshot(self, ts: float) -> Dict[str, Any]:
        local = self.local_dt(ts)
        visa_open = self.is_visa_window(ts)
        cycle = self.cycle_date(ts, NETWORK_VISA)
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'cutoff': '%02d:%02d' % (self.cutoff_hour, self.cutoff_minute),
            'after_cutoff': not visa_open,
            'visa_window': visa_open,
            'mastercard_open': True,
            'weekday': local.strftime('%A'),
            'value_date': '%s-%s-%s' % (cycle[:4], cycle[4:6], cycle[6:]),
            'cycle_date': cycle,
        }


@dataclass
class VauPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_report: bool = True
    allow_credit: bool = False
    max_keys: int = 12
    max_advices: int = 120
    max_merchants: int = 40
    advice_fee: Decimal = Decimal('0.10')
    cutoff_hour: int = 22
    tz_offset_hours: int = -4
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST

    @classmethod
    def from_env(cls) -> 'VauPolicy':
        extra = _env_list('VAU_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('VAU_ENABLED', True),
            customer_manage=_env_bool('VAU_CUSTOMER_MANAGE', True),
            customer_report=_env_bool('VAU_CUSTOMER_REPORT', True),
            allow_credit=_env_bool('VAU_ALLOW_CREDIT', False),
            max_keys=max(1, _env_int('VAU_MAX_KEYS', 12)),
            max_advices=max(1, _env_int('VAU_MAX_ADVICES', 120)),
            max_merchants=max(1, _env_int('VAU_MAX_MERCHANTS', 40)),
            advice_fee=_env_money('VAU_FEE', '0.10'),
            cutoff_hour=max(0, min(23, _env_int('VAU_CUTOFF_HOUR', 22))),
            tz_offset_hours=_env_int('VAU_TZ_OFFSET', -4),
            watchlist=watch,
        )


@dataclass
class VauKey:
    key_id: str
    userid: str
    nickname: str
    legal_name: str
    pan: str
    expiry: str
    brand: str
    network: str
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
            'legal_name': self.legal_name,
            'pan_masked': mask_pan(self.pan),
            'last4': last4(self.pan),
            'expiry_masked': mask_expiry(self.expiry),
            'brand': self.brand,
            'network': self.network,
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
class VauMerchant:
    merchant_id: str
    nickname: str
    legal_name: str
    bid: str
    network: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'merchant_id': self.merchant_id,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'bid_masked': mask_bid(self.bid),
            'network': self.network,
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == MERCH_ACTIVE,
            'paused': self.status == MERCH_PAUSED,
            'archived': self.status == MERCH_ARCHIVED,
        }


@dataclass
class VauAdvice:
    advice_id: str
    trace_id: str
    key_id: str
    merchant_id: str
    userid: str
    internal_account: str
    nickname: str
    legal_name: str
    pan: str
    expiry: str
    new_pan: str
    new_expiry: str
    brand: str
    network: str
    fingerprint: str
    new_fingerprint: str
    reason: str
    source: str
    status: str
    vau_ref: str
    fee: str
    fee_status: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    created_at: float
    updated_at: float
    posted_at: float = 0.0
    reversed_at: float = 0.0
    note: str = ''
    bid: str = ''
    merchant_name: str = ''
    value_date: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'advice_id': self.advice_id,
            'trace_id': self.trace_id,
            'key_id': self.key_id,
            'merchant_id': self.merchant_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'pan_masked': mask_pan(self.pan),
            'last4': last4(self.pan),
            'expiry_masked': mask_expiry(self.expiry),
            'new_pan_masked': mask_pan(self.new_pan) if self.new_pan else '',
            'new_last4': last4(self.new_pan) if self.new_pan else '',
            'new_expiry_masked': mask_expiry(self.new_expiry) if self.new_expiry else '',
            'brand': self.brand,
            'network': self.network,
            'reason': self.reason,
            'source': self.source,
            'status': self.status,
            'vau_ref_masked': mask_vau_ref(self.vau_ref) if self.vau_ref else '',
            'fee': self.fee,
            'fee_status': self.fee_status,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'posted_at': self.posted_at,
            'reversed_at': self.reversed_at,
            'note': self.note,
            'bid_masked': mask_bid(self.bid) if self.bid else '',
            'merchant_name': self.merchant_name,
            'value_date': self.value_date,
            'held': self.status == ADV_HELD,
            'queued': self.status == ADV_QUEUED,
            'pending_release': self.status == ADV_PENDING,
            'posted': self.status == ADV_POSTED,
            'unmatched': self.status == ADV_UNMATCHED,
            'cancelable': self.status in CANCELABLE,
        }


def _clone_key(row: VauKey) -> VauKey:
    return VauKey(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_merchant(row: VauMerchant) -> VauMerchant:
    return VauMerchant(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_advice(row: VauAdvice) -> VauAdvice:
    return VauAdvice(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _key_from_row(row: Any) -> VauKey:
    return VauKey(
        key_id=row['key_id'], userid=row['userid'], nickname=row['nickname'],
        legal_name=row['legal_name'], pan=row['pan'], expiry=row['expiry'],
        brand=row['brand'], network=row['network'], fingerprint=row['fingerprint'],
        default_account=row['default_account'], status=row['status'], actor=row['actor'],
        created_at=float(row['created_at']), updated_at=float(row['updated_at']),
    )


def _merchant_from_row(row: Any) -> VauMerchant:
    return VauMerchant(
        merchant_id=row['merchant_id'], nickname=row['nickname'], legal_name=row['legal_name'],
        bid=row['bid'], network=row['network'], status=row['status'], actor=row['actor'],
        created_at=float(row['created_at']), updated_at=float(row['updated_at']),
    )


def _advice_from_row(row: Any) -> VauAdvice:
    return VauAdvice(
        advice_id=row['advice_id'], trace_id=row['trace_id'], key_id=row['key_id'] or '',
        merchant_id=row['merchant_id'] or '', userid=row['userid'] or '',
        internal_account=row['internal_account'] or '', nickname=row['nickname'] or '',
        legal_name=row['legal_name'] or '', pan=row['pan'], expiry=row['expiry'],
        new_pan=row['new_pan'] or '', new_expiry=row['new_expiry'] or '',
        brand=row['brand'], network=row['network'], fingerprint=row['fingerprint'],
        new_fingerprint=row['new_fingerprint'] or '', reason=row['reason'],
        source=row['source'], status=row['status'], vau_ref=row['vau_ref'] or '',
        fee=row['fee'], fee_status=row['fee_status'], actor=row['actor'],
        releaser=row['releaser'] or '', ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '', created_at=float(row['created_at']),
        updated_at=float(row['updated_at']), posted_at=float(row['posted_at'] or 0),
        reversed_at=float(row['reversed_at'] or 0), note=row['note'] or '',
        bid=row['bid'] or '', merchant_name=row['merchant_name'] or '',
        value_date=row['value_date'] or '',
    )


class MemoryVauStore:
    def __init__(self) -> None:
        self._keys: Dict[str, VauKey] = {}
        self._merchants: Dict[str, VauMerchant] = {}
        self._advices: Dict[str, VauAdvice] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_key(self, row: VauKey) -> None:
        with self._lock:
            self._keys[row.key_id] = row

    def get_key(self, key_id: str) -> Optional[VauKey]:
        with self._lock:
            row = self._keys.get(key_id)
            return _clone_key(row) if row else None

    def update_key(self, row: VauKey) -> None:
        with self._lock:
            self._keys[row.key_id] = row

    def list_keys(self, userid: Optional[str] = None, *, include_cancelled: bool = True) -> List[VauKey]:
        with self._lock:
            rows = [_clone_key(row) for row in self._keys.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_cancelled:
            rows = [row for row in rows if row.status != KEY_CANCELLED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_key_by_nickname(self, userid: str, nickname: str) -> Optional[VauKey]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._keys.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_KEYS:
                    return _clone_key(row)
        return None

    def find_key_by_fingerprint(self, fingerprint: str, *, userid: Optional[str] = None) -> Optional[VauKey]:
        with self._lock:
            for row in self._keys.values():
                if row.fingerprint == fingerprint and row.status in OPEN_KEYS:
                    if userid is None or row.userid == userid:
                        return _clone_key(row)
        return None

    def put_merchant(self, row: VauMerchant) -> None:
        with self._lock:
            self._merchants[row.merchant_id] = row

    def get_merchant(self, merchant_id: str) -> Optional[VauMerchant]:
        with self._lock:
            row = self._merchants.get(merchant_id)
            return _clone_merchant(row) if row else None

    def update_merchant(self, row: VauMerchant) -> None:
        with self._lock:
            self._merchants[row.merchant_id] = row

    def list_merchants(self, *, include_archived: bool = True) -> List[VauMerchant]:
        with self._lock:
            rows = [_clone_merchant(row) for row in self._merchants.values()]
        if not include_archived:
            rows = [row for row in rows if row.status != MERCH_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_merchant_by_bid(self, bid: str) -> Optional[VauMerchant]:
        with self._lock:
            for row in self._merchants.values():
                if row.bid == bid and row.status in OPEN_MERCH:
                    return _clone_merchant(row)
        return None

    def find_merchant_by_nickname(self, nickname: str) -> Optional[VauMerchant]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._merchants.values():
                if row.nickname.lower() == wanted and row.status in OPEN_MERCH:
                    return _clone_merchant(row)
        return None

    def put_advice(self, row: VauAdvice) -> VauAdvice:
        with self._lock:
            existing_id = self._by_trace.get(row.trace_id)
            if existing_id is not None:
                return self._advices[existing_id]
            self._advices[row.advice_id] = row
            self._by_trace[row.trace_id] = row.advice_id
            return row

    def update_advice(self, row: VauAdvice) -> None:
        with self._lock:
            self._advices[row.advice_id] = row

    def get_advice(self, advice_id: str) -> Optional[VauAdvice]:
        with self._lock:
            row = self._advices.get(advice_id)
            return _clone_advice(row) if row else None

    def get_advice_by_trace(self, trace_id: str) -> Optional[VauAdvice]:
        with self._lock:
            advice_id = self._by_trace.get(trace_id)
            return _clone_advice(self._advices[advice_id]) if advice_id else None

    def list_advices(self, userid: Optional[str] = None, key_id: Optional[str] = None) -> List[VauAdvice]:
        with self._lock:
            rows = [_clone_advice(row) for row in self._advices.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if key_id is not None:
            rows = [row for row in rows if row.key_id == key_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def list_unmatched(self) -> List[VauAdvice]:
        with self._lock:
            rows = [_clone_advice(row) for row in self._advices.values() if row.status == ADV_UNMATCHED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def next_vau_sequence(self, cycle_date: str) -> int:
        prefix = 'VAU' + cycle_date
        with self._lock:
            used = [row.vau_ref for row in self._advices.values() if row.vau_ref.startswith(prefix)]
        return len(used) + 1


class SqliteVauStore:
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
                    legal_name TEXT NOT NULL,
                    pan TEXT NOT NULL,
                    expiry TEXT NOT NULL,
                    brand TEXT NOT NULL,
                    network TEXT NOT NULL,
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
                CREATE TABLE IF NOT EXISTS merchants (
                    merchant_id TEXT PRIMARY KEY,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    bid TEXT NOT NULL,
                    network TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS advices (
                    advice_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    key_id TEXT NOT NULL DEFAULT '',
                    merchant_id TEXT NOT NULL DEFAULT '',
                    userid TEXT NOT NULL DEFAULT '',
                    internal_account TEXT NOT NULL DEFAULT '',
                    nickname TEXT NOT NULL DEFAULT '',
                    legal_name TEXT NOT NULL DEFAULT '',
                    pan TEXT NOT NULL,
                    expiry TEXT NOT NULL,
                    new_pan TEXT NOT NULL DEFAULT '',
                    new_expiry TEXT NOT NULL DEFAULT '',
                    brand TEXT NOT NULL,
                    network TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    new_fingerprint TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    vau_ref TEXT NOT NULL DEFAULT '',
                    fee TEXT NOT NULL,
                    fee_status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    posted_at REAL NOT NULL DEFAULT 0,
                    reversed_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    bid TEXT NOT NULL DEFAULT '',
                    merchant_name TEXT NOT NULL DEFAULT '',
                    value_date TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_key(self, row: VauKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO keys (
                    key_id, userid, nickname, legal_name, pan, expiry, brand, network,
                    fingerprint, default_account, status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.key_id, row.userid, row.nickname, row.legal_name, row.pan, row.expiry,
                    row.brand, row.network, row.fingerprint, row.default_account, row.status,
                    row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_key(self, key_id: str) -> Optional[VauKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM keys WHERE key_id = ?', (key_id,)).fetchone()
        return _key_from_row(row) if row else None

    def update_key(self, row: VauKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE keys SET nickname=?, legal_name=?, pan=?, expiry=?, brand=?, network=?,
                    fingerprint=?, default_account=?, status=?, actor=?, updated_at=?
                WHERE key_id=?
                """,
                (
                    row.nickname, row.legal_name, row.pan, row.expiry, row.brand, row.network,
                    row.fingerprint, row.default_account, row.status, row.actor, row.updated_at,
                    row.key_id,
                ),
            )
            conn.commit()

    def list_keys(self, userid: Optional[str] = None, *, include_cancelled: bool = True) -> List[VauKey]:
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

    def find_key_by_nickname(self, userid: str, nickname: str) -> Optional[VauKey]:
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

    def find_key_by_fingerprint(self, fingerprint: str, *, userid: Optional[str] = None) -> Optional[VauKey]:
        sql = "SELECT * FROM keys WHERE fingerprint = ? AND status IN ('active', 'paused')"
        params: List[Any] = [fingerprint]
        if userid is not None:
            sql += ' AND userid = ?'
            params.append(userid)
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return _key_from_row(row) if row else None

    def put_merchant(self, row: VauMerchant) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO merchants (
                    merchant_id, nickname, legal_name, bid, network, status, actor,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.merchant_id, row.nickname, row.legal_name, row.bid, row.network,
                    row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_merchant(self, merchant_id: str) -> Optional[VauMerchant]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM merchants WHERE merchant_id = ?', (merchant_id,)).fetchone()
        return _merchant_from_row(row) if row else None

    def update_merchant(self, row: VauMerchant) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE merchants SET nickname=?, legal_name=?, bid=?, network=?, status=?,
                    actor=?, updated_at=? WHERE merchant_id=?
                """,
                (
                    row.nickname, row.legal_name, row.bid, row.network, row.status,
                    row.actor, row.updated_at, row.merchant_id,
                ),
            )
            conn.commit()

    def list_merchants(self, *, include_archived: bool = True) -> List[VauMerchant]:
        sql = 'SELECT * FROM merchants'
        if not include_archived:
            sql += " WHERE status != 'archived'"
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [_merchant_from_row(row) for row in rows]

    def find_merchant_by_bid(self, bid: str) -> Optional[VauMerchant]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM merchants WHERE bid = ? AND status IN ('active', 'paused')",
                (bid,),
            ).fetchone()
        return _merchant_from_row(row) if row else None

    def find_merchant_by_nickname(self, nickname: str) -> Optional[VauMerchant]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM merchants
                WHERE lower(nickname) = lower(?) AND status IN ('active', 'paused')
                """,
                (nickname,),
            ).fetchone()
        return _merchant_from_row(row) if row else None

    def put_advice(self, row: VauAdvice) -> VauAdvice:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM advices WHERE trace_id = ?', (row.trace_id,)
            ).fetchone()
            if existing is not None:
                return _advice_from_row(existing)
            conn.execute(
                """
                INSERT INTO advices (
                    advice_id, trace_id, key_id, merchant_id, userid, internal_account,
                    nickname, legal_name, pan, expiry, new_pan, new_expiry, brand, network,
                    fingerprint, new_fingerprint, reason, source, status, vau_ref, fee,
                    fee_status, actor, releaser, ofac_hit, ofac_match, created_at, updated_at,
                    posted_at, reversed_at, note, bid, merchant_name, value_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.advice_id, row.trace_id, row.key_id, row.merchant_id, row.userid,
                    row.internal_account, row.nickname, row.legal_name, row.pan, row.expiry,
                    row.new_pan, row.new_expiry, row.brand, row.network, row.fingerprint,
                    row.new_fingerprint, row.reason, row.source, row.status, row.vau_ref,
                    row.fee, row.fee_status, row.actor, row.releaser, row.ofac_hit,
                    row.ofac_match, row.created_at, row.updated_at, row.posted_at,
                    row.reversed_at, row.note, row.bid, row.merchant_name, row.value_date,
                ),
            )
            conn.commit()
            return row

    def update_advice(self, row: VauAdvice) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE advices SET key_id=?, merchant_id=?, userid=?, internal_account=?,
                    nickname=?, legal_name=?, pan=?, expiry=?, new_pan=?, new_expiry=?,
                    brand=?, network=?, fingerprint=?, new_fingerprint=?, reason=?, source=?,
                    status=?, vau_ref=?, fee=?, fee_status=?, actor=?, releaser=?, ofac_hit=?,
                    ofac_match=?, updated_at=?, posted_at=?, reversed_at=?, note=?, bid=?,
                    merchant_name=?, value_date=?
                WHERE advice_id=?
                """,
                (
                    row.key_id, row.merchant_id, row.userid, row.internal_account, row.nickname,
                    row.legal_name, row.pan, row.expiry, row.new_pan, row.new_expiry, row.brand,
                    row.network, row.fingerprint, row.new_fingerprint, row.reason, row.source,
                    row.status, row.vau_ref, row.fee, row.fee_status, row.actor, row.releaser,
                    row.ofac_hit, row.ofac_match, row.updated_at, row.posted_at, row.reversed_at,
                    row.note, row.bid, row.merchant_name, row.value_date, row.advice_id,
                ),
            )
            conn.commit()

    def get_advice(self, advice_id: str) -> Optional[VauAdvice]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM advices WHERE advice_id = ?', (advice_id,)).fetchone()
        return _advice_from_row(row) if row else None

    def get_advice_by_trace(self, trace_id: str) -> Optional[VauAdvice]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM advices WHERE trace_id = ?', (trace_id,)).fetchone()
        return _advice_from_row(row) if row else None

    def list_advices(self, userid: Optional[str] = None, key_id: Optional[str] = None) -> List[VauAdvice]:
        sql = 'SELECT * FROM advices'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if key_id is not None:
            clauses.append('key_id = ?')
            params.append(key_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_advice_from_row(row) for row in rows]

    def list_unmatched(self) -> List[VauAdvice]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM advices WHERE status = 'unmatched' ORDER BY created_at DESC"
            ).fetchall()
        return [_advice_from_row(row) for row in rows]

    def next_vau_sequence(self, cycle_date: str) -> int:
        prefix = 'VAU' + cycle_date + '%'
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT COUNT(*) AS n FROM advices WHERE vau_ref LIKE ?', (prefix,),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class VauService:
    def __init__(
        self,
        policy: VauPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[UpdaterClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or UpdaterClock(
            cutoff_hour=policy.cutoff_hour,
            tz_offset_hours=policy.tz_offset_hours,
        )

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise VauError('vau_disabled', 'Account updater is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise VauError('vau_forbidden', 'Customers cannot manage updater keys.')

    def _require_report(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_report:
            raise VauError('vau_forbidden', 'Customers cannot report card updates.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise VauError('vau_forbidden', 'Staff only.')

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
            raise VauError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise VauError('credit_not_allowed', 'Credit accounts cannot enroll updater keys.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, reason: str) -> bool:
        return reason in SENSITIVE_REASONS

    def add_key(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        pan: Any,
        expiry: Any,
        default_account: Any,
    ) -> VauKey:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise VauError('vau_forbidden', 'Not allowed to enroll cards for this customer.')
        now = float(self.clock())
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        card = normalize_pan(pan)
        exp = normalize_expiry(expiry, now_ts=now, require_future=True)
        brand = brand_from_iin(card)
        network = network_for_brand(brand)
        fingerprint = pan_fingerprint(card)
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        if self.store.find_key_by_nickname(owner_userid, name) is not None:
            raise VauError('key_duplicate', 'A card with that nickname already exists.')
        if self.store.find_key_by_fingerprint(fingerprint) is not None:
            raise VauError('key_duplicate', 'That PAN is already enrolled.')
        open_rows = [row for row in self.store.list_keys(owner_userid) if row.status in OPEN_KEYS]
        if len(open_rows) >= self.policy.max_keys:
            raise VauError('key_limit', 'Updater key limit reached.')
        row = VauKey(
            key_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            pan=card,
            expiry=exp,
            brand=brand,
            network=network,
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

    def get_key(self, *, key_id: str, actor: str, actor_type: str) -> VauKey:
        self._require_enabled()
        row = self.store.get_key(key_id)
        if row is None:
            raise VauError('key_not_found', 'Updater key not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise VauError('vau_forbidden', 'Not allowed to view this key.')
        return row

    def enforce_key(
        self,
        *,
        key_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> VauKey:
        row = self.get_key(key_id=key_id, actor=actor, actor_type=actor_type)
        if row.status == KEY_CANCELLED:
            raise VauError('already_cancelled', 'Key is cancelled.')
        if row.status == KEY_PAUSED:
            raise VauError('key_paused', 'Key is paused.')
        if require_active and row.status != KEY_ACTIVE:
            raise VauError('invalid_status', 'Key is not active.')
        return row

    def set_key_status(
        self,
        *,
        key_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> VauKey:
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
            raise VauError('invalid_status', 'Status must be pause, resume, or cancel.')
        if row.status == KEY_CANCELLED:
            raise VauError('already_cancelled', 'Key is already cancelled.')
        if wanted == KEY_PAUSED:
            if row.status == KEY_PAUSED:
                raise VauError('already_paused', 'Key is already paused.')
            if row.status != KEY_ACTIVE:
                raise VauError('invalid_status', 'Only an active key can be paused.')
        elif wanted == KEY_ACTIVE:
            if row.status == KEY_ACTIVE:
                raise VauError('already_active', 'Key is already active.')
            if row.status != KEY_PAUSED:
                raise VauError('invalid_status', 'Only a paused key can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_key(row)
        if wanted == KEY_ACTIVE:
            self._rematch_unmatched(row)
        return row

    def add_merchant(
        self,
        *,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        bid: Any,
        network: Any = None,
    ) -> VauMerchant:
        self._require_staff(actor_type)
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        merchant_bid = normalize_bid(bid)
        net = str(network or NETWORK_VISA).strip().lower().replace('-', '_')
        if net in {'visa', 'vau', 'visa_vau'}:
            net = NETWORK_VISA
        elif net in {'mastercard', 'mc', 'abu', 'mastercard_abu'}:
            net = NETWORK_MC
        else:
            raise VauError('invalid_brand', 'Merchant network must be visa_vau or mastercard_abu.')
        if self.store.find_merchant_by_nickname(name) is not None:
            raise VauError('merchant_duplicate', 'A merchant with that nickname already exists.')
        if self.store.find_merchant_by_bid(merchant_bid) is not None:
            raise VauError('merchant_duplicate', 'That BID is already on file.')
        open_rows = [row for row in self.store.list_merchants() if row.status in OPEN_MERCH]
        if len(open_rows) >= self.policy.max_merchants:
            raise VauError('merchant_limit', 'Merchant directory limit reached.')
        now = float(self.clock())
        row = VauMerchant(
            merchant_id=uuid.uuid4().hex,
            nickname=name,
            legal_name=legal,
            bid=merchant_bid,
            network=net,
            status=MERCH_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_merchant(row)
        return row

    def get_merchant(self, *, merchant_id: str, actor_type: str) -> VauMerchant:
        self._require_staff(actor_type)
        row = self.store.get_merchant(merchant_id)
        if row is None:
            raise VauError('merchant_not_found', 'Merchant not found.')
        return row

    def set_merchant_status(
        self,
        *,
        merchant_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> VauMerchant:
        self._require_staff(actor_type)
        row = self.get_merchant(merchant_id=merchant_id, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': MERCH_PAUSED, 'resume': MERCH_ACTIVE, 'activate': MERCH_ACTIVE,
            'archive': MERCH_ARCHIVED, 'close': MERCH_ARCHIVED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {MERCH_PAUSED, MERCH_ACTIVE, MERCH_ARCHIVED}:
            raise VauError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == MERCH_ARCHIVED:
            raise VauError('already_archived', 'Merchant is already archived.')
        if wanted == MERCH_PAUSED and row.status == MERCH_PAUSED:
            raise VauError('already_paused', 'Merchant is already paused.')
        if wanted == MERCH_ACTIVE and row.status == MERCH_ACTIVE:
            raise VauError('already_active', 'Merchant is already active.')
        if wanted == MERCH_ACTIVE and row.status != MERCH_PAUSED:
            raise VauError('invalid_status', 'Only a paused merchant can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_merchant(row)
        return row

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        key_id: Any,
        reason: Any,
        new_pan: Any = None,
        new_expiry: Any = None,
        waive_fee: bool = False,
    ) -> Dict[str, Any]:
        self._require_report(actor_type)
        key = self.enforce_key(key_id=str(key_id or '').strip(), actor=actor, actor_type=actor_type)
        if key.userid != owner_userid:
            raise VauError('vau_forbidden', 'Key does not belong to this customer.')
        code = normalize_reason(reason)
        now = float(self.clock())
        new_card, new_exp = self._replacement(code, key, new_pan, new_expiry, now)
        ofac = self._screen(key.legal_name, aliases=(key.nickname,))
        fee = compute_fee(self.policy.advice_fee, waived=bool(waive_fee) and actor_type in EMPLOYEE_ROLES)
        return {
            'reason': code,
            'fee': money_str(fee),
            'dual_control': self._needs_dual_control(code),
            'ofac': ofac.to_dict(),
            'key': key.to_dict(),
            'new_pan_masked': mask_pan(new_card) if new_card else '',
            'new_expiry_masked': mask_expiry(new_exp) if new_exp else '',
            'clock': self.calendar.snapshot(now),
            'network_open': self.calendar.is_open(now, key.network),
        }

    def _replacement(
        self,
        reason: str,
        key: VauKey,
        new_pan: Any,
        new_expiry: Any,
        now: float,
    ) -> Tuple[str, str]:
        if reason == REASON_A:
            card = normalize_pan(new_pan)
            if brand_from_iin(card) != key.brand:
                raise VauError('invalid_brand', 'Replacement PAN must stay on the same network.')
            if pan_fingerprint(card) == key.fingerprint:
                raise VauError('invalid_pan', 'Replacement PAN must differ from the enrolled PAN.')
            exp = normalize_expiry(new_expiry, now_ts=now, require_future=True)
            return card, exp
        if reason == REASON_E:
            exp = normalize_expiry(new_expiry, now_ts=now, require_future=True)
            if exp == key.expiry:
                raise VauError('invalid_expiry', 'New expiry must differ from the enrolled expiry.')
            return '', exp
        if reason in {REASON_C, REASON_Q, REASON_P}:
            return '', ''
        raise VauError('invalid_reason', 'Unknown updater reason code.')

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        key: Optional[VauKey],
        merchant: Optional[VauMerchant],
        account: str,
        reason: str,
        source: str,
        pan: str,
        expiry: str,
        new_pan: str,
        new_expiry: str,
        brand: str,
        network: str,
        fingerprint: str,
        ofac: ScreenResult,
        trace_id: str,
        waive_fee: bool,
        nickname: str,
        legal_name: str,
    ) -> VauAdvice:
        now = float(self.clock())
        fee = compute_fee(self.policy.advice_fee, waived=waive_fee)
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        if key is None:
            status = ADV_UNMATCHED
        elif ofac.hit:
            status = ADV_HELD
        elif self._needs_dual_control(reason):
            status = ADV_PENDING
        elif not self.calendar.is_open(now, network):
            status = ADV_QUEUED
        else:
            status = ADV_POSTED
        advice = VauAdvice(
            advice_id=uuid.uuid4().hex,
            trace_id=trace_id,
            key_id=key.key_id if key else '',
            merchant_id=merchant.merchant_id if merchant else '',
            userid=owner_userid,
            internal_account=account,
            nickname=nickname,
            legal_name=legal_name,
            pan=pan,
            expiry=expiry,
            new_pan=new_pan,
            new_expiry=new_expiry,
            brand=brand,
            network=network,
            fingerprint=fingerprint,
            new_fingerprint=pan_fingerprint(new_pan) if new_pan else '',
            reason=reason,
            source=source,
            status=status,
            vau_ref='',
            fee=money_str(fee),
            fee_status=fee_status,
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
            bid=merchant.bid if merchant else '',
            merchant_name=merchant.legal_name if merchant else '',
            value_date=self.calendar.cycle_date(now, network),
        )
        if status == ADV_POSTED:
            self._transmit(advice, key=key, actor=actor)
        return advice

    def _transmit(self, advice: VauAdvice, *, key: Optional[VauKey], actor: str) -> VauAdvice:
        now = float(self.clock())
        cycle = advice.value_date or self.calendar.cycle_date(now, advice.network)
        seq = self.store.next_vau_sequence(cycle)
        advice.vau_ref = compose_vau_ref(cycle, seq)
        fee = parse_money(advice.fee, allow_zero=True)
        status = ADV_POSTED
        fail_note = ''
        if fee > 0 and advice.fee_status != FEE_WAIVED and self.debit_fn is not None and advice.internal_account:
            try:
                result = self.debit_fn(
                    advice.internal_account, money_str(fee), 'vau fee %s' % advice.vau_ref[:12],
                )
            except Exception as exc:
                status = ADV_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = ADV_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = ADV_FAILED
                    fail_note = str(result)[:240]
                else:
                    advice.fee_status = FEE_COLLECTED
        elif fee == 0 or advice.fee_status == FEE_WAIVED:
            advice.fee_status = FEE_WAIVED
        advice.status = status
        advice.updated_at = now
        if status == ADV_POSTED:
            advice.posted_at = now
            advice.releaser = str(actor)
            if key is not None:
                self._apply_key_mutation(key, advice, now)
        else:
            advice.vau_ref = ''
            advice.note = fail_note
            advice.fee_status = FEE_NSF if status == ADV_NSF else advice.fee_status
        return advice

    def _apply_key_mutation(self, key: VauKey, advice: VauAdvice, now: float) -> None:
        changed = False
        if advice.reason == REASON_A and advice.new_pan:
            key.pan = advice.new_pan
            key.expiry = advice.new_expiry or key.expiry
            key.fingerprint = advice.new_fingerprint or pan_fingerprint(advice.new_pan)
            changed = True
        elif advice.reason == REASON_E and advice.new_expiry:
            key.expiry = advice.new_expiry
            changed = True
        elif advice.reason == REASON_C:
            key.status = KEY_CANCELLED
            changed = True
        if changed:
            key.updated_at = now
            self.store.update_key(key)

    def _restore_key(self, advice: VauAdvice, now: float) -> None:
        if not advice.key_id:
            return
        key = self.store.get_key(advice.key_id)
        if key is None:
            return
        if advice.reason == REASON_A:
            key.pan = advice.pan
            key.expiry = advice.expiry
            key.fingerprint = advice.fingerprint
            key.status = KEY_ACTIVE if key.status == KEY_CANCELLED else key.status
        elif advice.reason == REASON_E:
            key.expiry = advice.expiry
        elif advice.reason == REASON_C and key.status == KEY_CANCELLED:
            key.status = KEY_ACTIVE
        key.updated_at = now
        self.store.update_key(key)

    def report(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        key_id: Any,
        reason: Any,
        new_pan: Any = None,
        new_expiry: Any = None,
        trace_id: Any = None,
        waive_fee: bool = False,
    ) -> Tuple[VauAdvice, bool]:
        self._require_report(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise VauError('vau_forbidden', 'Not allowed to report updates for this customer.')
        key = self.enforce_key(key_id=str(key_id or '').strip(), actor=actor, actor_type=actor_type)
        if key.userid != owner_userid:
            raise VauError('vau_forbidden', 'Key does not belong to this customer.')
        trace = normalize_id(trace_id)
        existing = self.store.get_advice_by_trace(trace)
        if existing is not None:
            return existing, False
        code = normalize_reason(reason)
        now = float(self.clock())
        new_card, new_exp = self._replacement(code, key, new_pan, new_expiry, now)
        if len(self.store.list_advices(owner_userid)) >= self.policy.max_advices:
            raise VauError('advice_limit', 'Updater history limit reached.')
        ofac = self._screen(key.legal_name, aliases=(key.nickname,))
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        advice = self._place(
            owner_userid=owner_userid,
            actor=actor,
            key=key,
            merchant=None,
            account=key.default_account,
            reason=code,
            source=SOURCE_ISSUER,
            pan=key.pan,
            expiry=key.expiry,
            new_pan=new_card,
            new_expiry=new_exp,
            brand=key.brand,
            network=key.network,
            fingerprint=key.fingerprint,
            ofac=ofac,
            trace_id=trace,
            waive_fee=staff_waive,
            nickname=key.nickname,
            legal_name=key.legal_name,
        )
        stored = self.store.put_advice(advice)
        if stored.advice_id != advice.advice_id:
            return stored, False
        if stored.status == ADV_NSF:
            raise VauError('nsf', 'Insufficient funds for updater fee.', advice=stored)
        if stored.status == ADV_FAILED:
            raise VauError('failed', 'Updater fee debit did not complete.', advice=stored)
        return stored, True

    def ingest_inquiry(
        self,
        *,
        actor: str,
        actor_type: str,
        pan: Any,
        expiry: Any,
        bid: Any = None,
        merchant_id: Any = None,
        merchant_name: Any = None,
        trace_id: Any = None,
        waive_fee: bool = False,
    ) -> Tuple[VauAdvice, bool]:
        self._require_staff(actor_type)
        now = float(self.clock())
        card = normalize_pan(pan)
        exp = normalize_expiry(expiry)
        brand = brand_from_iin(card)
        network = network_for_brand(brand)
        fingerprint = pan_fingerprint(card)
        trace = normalize_id(trace_id)
        existing = self.store.get_advice_by_trace(trace)
        if existing is not None:
            return existing, False
        merchant = None
        if merchant_id:
            merchant = self.store.get_merchant(str(merchant_id).strip())
            if merchant is None:
                raise VauError('merchant_not_found', 'Merchant not found.')
        elif bid:
            merchant = self.store.find_merchant_by_bid(normalize_bid(bid))
            if merchant is None:
                raise VauError('merchant_not_found', 'Merchant BID is not enrolled.')
        if merchant is not None and merchant.status != MERCH_ACTIVE:
            raise VauError('merchant_paused', 'Merchant is not active.')
        screen_target = (merchant.legal_name if merchant else '') or str(merchant_name or '')
        ofac = self._screen(screen_target, aliases=(merchant.nickname,) if merchant else ())
        key = self.store.find_key_by_fingerprint(fingerprint)
        owner = key.userid if key else ''
        account = key.default_account if key else ''
        nickname = key.nickname if key else ''
        legal = key.legal_name if key else (merchant.legal_name if merchant else '')
        advice = self._place(
            owner_userid=owner,
            actor=actor,
            key=key,
            merchant=merchant,
            account=account,
            reason=REASON_P,
            source=SOURCE_MERCHANT,
            pan=card,
            expiry=exp,
            new_pan='',
            new_expiry='',
            brand=brand,
            network=network,
            fingerprint=fingerprint,
            ofac=ofac,
            trace_id=trace,
            waive_fee=bool(waive_fee),
            nickname=nickname,
            legal_name=legal,
        )
        if merchant:
            advice.bid = merchant.bid
            advice.merchant_name = merchant.legal_name
        stored = self.store.put_advice(advice)
        if stored.advice_id != advice.advice_id:
            return stored, False
        if stored.status == ADV_NSF:
            raise VauError('nsf', 'Insufficient funds for updater fee.', advice=stored)
        if stored.status == ADV_FAILED:
            raise VauError('failed', 'Updater fee debit did not complete.', advice=stored)
        return stored, True

    def ingest_file(self, *, actor: str, actor_type: str, payload: Any) -> List[VauAdvice]:
        self._require_staff(actor_type)
        rows = []
        for line in split_vau1_file(payload):
            parsed = parse_vau1(line)
            advice, _created = self.ingest_inquiry(
                actor=actor,
                actor_type=actor_type,
                pan=parsed['pan'],
                expiry=parsed['expiry'],
                bid=parsed.get('bid') or None,
                merchant_name=parsed.get('merchant') or None,
                trace_id=parsed.get('reference') or None,
            )
            rows.append(advice)
        return rows

    def _rematch_unmatched(self, key: VauKey) -> None:
        for row in self.store.list_unmatched():
            if row.fingerprint != key.fingerprint:
                continue
            row.key_id = key.key_id
            row.userid = key.userid
            row.internal_account = key.default_account
            row.nickname = key.nickname
            row.legal_name = key.legal_name
            ofac = self._screen(key.legal_name, aliases=(key.nickname, row.merchant_name))
            row.ofac_hit = 1 if ofac.hit else 0
            row.ofac_match = ofac.matched
            now = float(self.clock())
            if ofac.hit:
                row.status = ADV_HELD
            elif not self.calendar.is_open(now, key.network):
                row.status = ADV_QUEUED
                row.value_date = self.calendar.cycle_date(now, key.network)
            else:
                self._transmit(row, key=key, actor=key.actor)
            row.updated_at = now
            self.store.update_advice(row)

    def claim(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        advice_id: Any = None,
        pan: Any = None,
        last4_digits: Any = None,
        brand: Any = None,
        expiry: Any = None,
        default_account: Any = None,
        nickname: Any = None,
        legal_name: Any = None,
    ) -> VauAdvice:
        self._require_report(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise VauError('vau_forbidden', 'Not allowed to claim this inquiry.')
        target = None
        if advice_id:
            target = self.store.get_advice(str(advice_id).strip())
        elif pan:
            fingerprint = pan_fingerprint(normalize_pan(pan))
            for row in self.store.list_unmatched():
                if row.fingerprint == fingerprint:
                    target = row
                    break
        elif last4_digits and brand:
            wanted_last4 = last4(last4_digits)
            wanted_brand = str(brand).strip().lower()
            wanted_exp = normalize_expiry(expiry) if expiry else ''
            for row in self.store.list_unmatched():
                if last4(row.pan) == wanted_last4 and row.brand == wanted_brand:
                    if wanted_exp and row.expiry != wanted_exp:
                        continue
                    target = row
                    break
        if target is None or target.status != ADV_UNMATCHED:
            raise VauError('not_claimable', 'No unmatched inquiry matches that card.')
        now = float(self.clock())
        key = self.store.find_key_by_fingerprint(target.fingerprint, userid=owner_userid)
        if key is None:
            if not default_account:
                raise VauError('invalid_account', 'An internal account is required to enroll the claimed card.')
            key = self.add_key(
                owner_userid=owner_userid,
                actor=actor,
                actor_type=actor_type,
                nickname=nickname or ('card ' + last4(target.pan)),
                legal_name=legal_name or target.legal_name or 'Cardholder',
                pan=target.pan,
                expiry=target.expiry,
                default_account=default_account,
            )
            rematched = self.store.get_advice(target.advice_id)
            if rematched is None:
                raise VauError('advice_not_found', 'Inquiry disappeared during claim.')
            return rematched
        if key.status != KEY_ACTIVE:
            raise VauError('key_paused', 'Matching key is not active.')
        target.key_id = key.key_id
        target.userid = key.userid
        target.internal_account = key.default_account
        target.nickname = key.nickname
        target.legal_name = key.legal_name
        ofac = self._screen(key.legal_name, aliases=(key.nickname, target.merchant_name))
        target.ofac_hit = 1 if ofac.hit else 0
        target.ofac_match = ofac.matched
        if ofac.hit:
            target.status = ADV_HELD
        elif not self.calendar.is_open(now, key.network):
            target.status = ADV_QUEUED
            target.value_date = self.calendar.cycle_date(now, key.network)
        else:
            self._transmit(target, key=key, actor=actor)
        target.updated_at = now
        self.store.update_advice(target)
        if target.status == ADV_NSF:
            raise VauError('nsf', 'Insufficient funds for updater fee.', advice=target)
        if target.status == ADV_FAILED:
            raise VauError('failed', 'Updater fee debit did not complete.', advice=target)
        return target

    def assign(
        self,
        *,
        advice_id: str,
        actor: str,
        actor_type: str,
        owner_userid: str,
        key_id: Any = None,
        nickname: Any = None,
        legal_name: Any = None,
        default_account: Any = None,
    ) -> VauAdvice:
        self._require_staff(actor_type)
        row = self.store.get_advice(advice_id)
        if row is None:
            raise VauError('advice_not_found', 'Inquiry not found.')
        if row.status != ADV_UNMATCHED:
            raise VauError('not_assignable', 'Only unmatched inquiries can be assigned.')
        if not owner_userid:
            raise VauError('missing_customer_id', 'Customer id is required.')
        if key_id:
            key = self.get_key(key_id=str(key_id).strip(), actor=actor, actor_type=actor_type)
            if key.userid != owner_userid:
                raise VauError('vau_forbidden', 'Key does not belong to this customer.')
            if key.fingerprint != row.fingerprint:
                raise VauError('invalid_pan', 'Key PAN does not match the inquiry.')
        else:
            key = self.add_key(
                owner_userid=owner_userid,
                actor=actor,
                actor_type=actor_type,
                nickname=nickname or ('card ' + last4(row.pan)),
                legal_name=legal_name or row.legal_name or 'Cardholder',
                pan=row.pan,
                expiry=row.expiry,
                default_account=default_account,
            )
        rematched = self.store.get_advice(row.advice_id)
        if rematched is None:
            raise VauError('advice_not_found', 'Inquiry disappeared during assign.')
        if rematched.status == ADV_UNMATCHED:
            return self.claim(
                owner_userid=owner_userid, actor=actor, actor_type=actor_type,
                advice_id=row.advice_id, default_account=key.default_account,
            )
        return rematched

    def get_advice(self, *, advice_id: str, actor: str, actor_type: str) -> VauAdvice:
        self._require_enabled()
        row = self.store.get_advice(advice_id)
        if row is None:
            raise VauError('advice_not_found', 'Advice not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise VauError('vau_forbidden', 'Not allowed to view this advice.')
        return row

    def cancel_advice(
        self,
        *,
        advice_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> VauAdvice:
        self._require_report(actor_type)
        row = self.get_advice(advice_id=advice_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise VauError('vau_forbidden', 'Not allowed to cancel this advice.')
        if row.status not in CANCELABLE:
            raise VauError('not_cancelable', 'Only held, queued, or pending advice can be cancelled.')
        row.status = ADV_CANCELLED
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        row.note = normalize_note(note)
        self.store.update_advice(row)
        return row

    def waive_fee(self, *, advice_id: str, actor: str, actor_type: str) -> VauAdvice:
        self._require_staff(actor_type)
        row = self.get_advice(advice_id=advice_id, actor=actor, actor_type=actor_type)
        if row.status not in CANCELABLE:
            raise VauError('invalid_status', 'Fee can only be waived before the advice is posted.')
        row.fee = money_str(Decimal('0.00'))
        row.fee_status = FEE_WAIVED
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_advice(row)
        return row

    def override_ofac(self, *, advice_id: str, actor: str, actor_type: str, note: Any = '') -> VauAdvice:
        self._require_staff(actor_type)
        row = self.get_advice(advice_id=advice_id, actor=actor, actor_type=actor_type)
        if row.status != ADV_HELD:
            raise VauError('not_overridable', 'Only an OFAC hold can be overridden.')
        now = float(self.clock())
        row.ofac_hit = 0
        row.note = normalize_note(note) or 'ofac override'
        row.actor = str(actor)
        row.updated_at = now
        key = self.store.get_key(row.key_id) if row.key_id else None
        if self._needs_dual_control(row.reason):
            row.status = ADV_PENDING
        elif key is None:
            row.status = ADV_UNMATCHED
        elif not self.calendar.is_open(now, row.network):
            row.status = ADV_QUEUED
            row.value_date = self.calendar.cycle_date(now, row.network)
        else:
            self._transmit(row, key=key, actor=actor)
        self.store.update_advice(row)
        if row.status == ADV_NSF:
            raise VauError('nsf', 'Insufficient funds for updater fee.', advice=row)
        if row.status == ADV_FAILED:
            raise VauError('failed', 'Updater fee debit did not complete.', advice=row)
        return row

    def release_advice(self, *, advice_id: str, actor: str, actor_type: str) -> VauAdvice:
        self._require_staff(actor_type)
        row = self.get_advice(advice_id=advice_id, actor=actor, actor_type=actor_type)
        if row.status == ADV_HELD:
            raise VauError('ofac_hold', 'OFAC hold must be overridden before release.')
        if row.status not in {ADV_PENDING, ADV_QUEUED}:
            raise VauError('not_releasable', 'Only queued or pending advice can be released.')
        if (
            row.status == ADV_PENDING
            and row.actor
            and str(actor) == str(row.actor)
            and self._needs_dual_control(row.reason)
        ):
            raise VauError('same_approver', 'A different employee must release this update.')
        now = float(self.clock())
        key = self.store.get_key(row.key_id) if row.key_id else None
        if row.status == ADV_QUEUED or not self.calendar.is_open(now, row.network):
            if not self.calendar.is_open(now, row.network) and row.status != ADV_PENDING:
                row.status = ADV_QUEUED
                row.value_date = self.calendar.cycle_date(now, row.network)
                row.updated_at = now
                self.store.update_advice(row)
                return row
        self._transmit(row, key=key, actor=actor)
        self.store.update_advice(row)
        if row.status == ADV_NSF:
            raise VauError('nsf', 'Insufficient funds for updater fee.', advice=row)
        if row.status == ADV_FAILED:
            raise VauError('failed', 'Updater fee debit did not complete.', advice=row)
        return row

    def reject_advice(
        self,
        *,
        advice_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> VauAdvice:
        self._require_staff(actor_type)
        row = self.get_advice(advice_id=advice_id, actor=actor, actor_type=actor_type)
        if row.status not in CANCELABLE and row.status != ADV_UNMATCHED:
            raise VauError('not_rejectable', 'Only held, queued, pending, or unmatched advice can be rejected.')
        row.status = ADV_REJECTED
        row.note = normalize_note(note) or normalize_note(reason, limit=40)
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_advice(row)
        return row

    def reverse_advice(
        self,
        *,
        advice_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
        customer_request: bool = False,
    ) -> VauAdvice:
        if customer_request:
            self._require_report(actor_type)
        else:
            self._require_staff(actor_type)
        row = self.get_advice(advice_id=advice_id, actor=actor, actor_type=actor_type)
        if row.status == ADV_REVERSED:
            raise VauError('already_reversed', 'Advice is already reversed.')
        if row.status != ADV_POSTED:
            raise VauError('not_reversible', 'Only posted advice can be reversed.')
        now = float(self.clock())
        age = now - (row.posted_at or row.created_at)
        if customer_request:
            if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
                raise VauError('vau_forbidden', 'Not allowed to reverse this advice.')
            if age > CUSTOMER_REVERSE_SECONDS:
                raise VauError('return_window_closed', 'Customer reverse window is 24 hours.')
        elif age > STAFF_REVERSE_SECONDS:
            raise VauError('return_window_closed', 'Staff reverse window is 90 days.')
        fee = parse_money(row.fee, allow_zero=True)
        if fee > 0 and row.fee_status == FEE_COLLECTED and self.credit_fn is not None and row.internal_account:
            try:
                result = self.credit_fn(
                    row.internal_account, money_str(fee), 'vau reverse %s' % (row.vau_ref[-12:] or row.advice_id[:12]),
                )
            except Exception as exc:
                raise VauError('reverse_failed', 'Reverse credit failed.', advice=row) from exc
            if _classify_money_result(result) != 'ok':
                raise VauError('reverse_failed', 'Reverse credit failed.', advice=row)
        self._restore_key(row, now)
        row.status = ADV_REVERSED
        row.reversed_at = now
        row.updated_at = now
        row.actor = str(actor)
        row.note = normalize_note(note) or 'reversed'
        self.store.update_advice(row)
        return row

    def export_file(self, *, actor: str, actor_type: str, userid: Optional[str] = None) -> str:
        self._require_staff(actor_type)
        rows = self.store.list_advices(userid)
        lines = []
        for row in rows:
            if row.status != ADV_POSTED:
                continue
            lines.append(compose_vau1(
                reason=row.reason,
                pan=mask_pan(row.pan),
                expiry=mask_expiry(row.expiry),
                brand=row.brand,
                network=row.network,
                bid=mask_bid(row.bid) if row.bid else '',
                merchant=row.merchant_name,
                reference=mask_vau_ref(row.vau_ref),
                status=row.status,
            ))
        if not lines:
            raise VauError('not_exportable', 'No posted advice to export.')
        return '\n'.join(lines)

    def run_due(self, userid: Optional[str] = None) -> List[VauAdvice]:
        now = float(self.clock())
        changed: List[VauAdvice] = []
        for row in self.store.list_advices(userid):
            if row.status == ADV_UNMATCHED and (now - row.created_at) >= UNMATCHED_TTL_SECONDS:
                row.status = ADV_EXPIRED
                row.updated_at = now
                self.store.update_advice(row)
                changed.append(row)
                continue
            if row.status != ADV_QUEUED:
                continue
            if not self.calendar.is_open(now, row.network):
                continue
            if self._needs_dual_control(row.reason):
                row.status = ADV_PENDING
                row.updated_at = now
                self.store.update_advice(row)
                changed.append(row)
                continue
            key = self.store.get_key(row.key_id) if row.key_id else None
            self._transmit(row, key=key, actor=row.actor)
            self.store.update_advice(row)
            changed.append(row)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.run_due(userid)
        keys = self.store.list_keys(userid)
        advices = self.store.list_advices(userid)
        fee_ytd = Decimal('0.00')
        posted = 0
        for row in advices:
            if row.status == ADV_POSTED:
                posted += 1
                if row.fee_status == FEE_COLLECTED:
                    fee_ytd += parse_money(row.fee, allow_zero=True)
        now = float(self.clock())
        payload = {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'fee': money_str(self.policy.advice_fee),
            'clock': self.calendar.snapshot(now),
            'keys': [row.to_dict() for row in keys[:40]],
            'advices': [row.to_dict() for row in advices[:40]],
            'merchants': [],
            'unmatched': [],
            'ytd_fees': money_str(fee_ytd),
            'posted_count': posted,
            'active_count': sum(1 for row in keys if row.status == KEY_ACTIVE),
            'open_count': sum(1 for row in advices if row.status in OPEN_ADVICES),
        }
        if actor_type in EMPLOYEE_ROLES:
            payload['merchants'] = [row.to_dict() for row in self.store.list_merchants()[:40]]
            payload['unmatched'] = [row.to_dict() for row in self.store.list_unmatched()[:40]]
        return payload


_SERVICE: Optional[VauService] = None


def set_service(service: Optional[VauService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[VauService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('VAU_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryVauStore()
    path = os.environ.get('VAU_DB', DEFAULT_STORE_PATH)
    return SqliteVauStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[UpdaterClock] = None,
) -> VauService:
    if store is None:
        store = default_store()
    return VauService(
        VauPolicy.from_env(),
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
        'advice_limit': 409,
        'merchant_duplicate': 409,
        'merchant_limit': 409,
        'already_cancelled': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_archived': 409,
        'already_reversed': 409,
        'nsf': 409,
        'failed': 409,
        'reverse_failed': 409,
        'vau_forbidden': 403,
        'vau_disabled': 403,
        'key_paused': 403,
        'merchant_paused': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_cancelable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_reversible': 403,
        'not_overridable': 403,
        'not_claimable': 403,
        'not_assignable': 403,
        'not_exportable': 403,
        'return_window_closed': 403,
        'key_not_found': 404,
        'advice_not_found': 404,
        'merchant_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_pan': 400,
        'invalid_brand': 400,
        'invalid_expiry': 400,
        'invalid_reason': 400,
        'invalid_bid': 400,
        'invalid_status': 400,
        'invalid_reference': 400,
        'invalid_file': 400,
        'missing_customer_id': 400,
        'missing_key': 400,
        'missing_advice': 400,
        'missing_merchant': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: VauError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('advice') is not None:
        body['advice'] = exc.extra['advice'].to_dict()
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
    except VauError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snap(service: VauService, owner: str, userid: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(owner, actor=userid, actor_type=actor_type)


def handle_list(service: VauService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'CardUpdater': _snap(service, owner, userid, actor_type)}), 200


def handle_add_key(service: VauService):
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
            legal_name=values.get('legal_name') or values.get('name'),
            pan=values.get('pan') or values.get('card') or values.get('account_number'),
            expiry=values.get('expiry') or values.get('exp'),
            default_account=values.get('default_account') or values.get('account') or values.get('from_account'),
        )
        return jsonify({
            'message': 'Card enrolled',
            'key': row.to_dict(),
            'CardUpdater': _snap(service, owner, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _key_status_route(service: VauService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    key_id = str(values.get('key_id') or '').strip()
    if not key_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_key'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_key_status(key_id=key_id, actor=userid, actor_type=actor_type, status=status)
        return jsonify({
            'message': ok_message,
            'key': row.to_dict(),
            'CardUpdater': _snap(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: VauService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    key_id = str(values.get('key_id') or '').strip()
    if not key_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_key'}), 400

    def _run():
        preview = service.preview(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            key_id=key_id,
            reason=values.get('reason'),
            new_pan=values.get('new_pan'),
            new_expiry=values.get('new_expiry'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({'preview': preview, 'CardUpdater': _snap(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_report(service: VauService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    key_id = str(values.get('key_id') or '').strip()
    if not key_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_key'}), 400

    def _run():
        advice, created = service.report(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            key_id=key_id,
            reason=values.get('reason'),
            new_pan=values.get('new_pan'),
            new_expiry=values.get('new_expiry'),
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({
            'message': 'Card update reported' if created else 'Update already posted',
            'advice': advice.to_dict(),
            'CardUpdater': _snap(service, owner, userid, actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: VauService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    advice_id = str(values.get('advice_id') or '').strip()
    if not advice_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_advice'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.cancel_advice(
            advice_id=advice_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Update cancelled',
            'advice': row.to_dict(),
            'CardUpdater': _snap(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_claim(service: VauService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.claim(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            advice_id=values.get('advice_id'),
            pan=values.get('pan'),
            last4_digits=values.get('last4') or values.get('pan_last4'),
            brand=values.get('brand'),
            expiry=values.get('expiry'),
            default_account=values.get('default_account') or values.get('account'),
            nickname=values.get('nickname'),
            legal_name=values.get('legal_name'),
        )
        return jsonify({
            'message': 'Inquiry claimed',
            'advice': row.to_dict(),
            'CardUpdater': _snap(service, owner, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_add_merchant(service: VauService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.add_merchant(
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname'),
            legal_name=values.get('legal_name') or values.get('name'),
            bid=values.get('bid'),
            network=values.get('network'),
        )
        owner = str(values.get('customer_id') or userid)
        return jsonify({
            'message': 'Merchant enrolled',
            'merchant': row.to_dict(),
            'CardUpdater': _snap(service, owner, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _merchant_status_route(service: VauService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    merchant_id = str(values.get('merchant_id') or '').strip()
    if not merchant_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_merchant'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_merchant_status(
            merchant_id=merchant_id, actor=userid, actor_type=actor_type, status=status,
        )
        owner = str(values.get('customer_id') or userid)
        return jsonify({
            'message': ok_message,
            'merchant': row.to_dict(),
            'CardUpdater': _snap(service, owner, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_ingest(service: VauService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row, created = service.ingest_inquiry(
            actor=userid,
            actor_type=actor_type,
            pan=values.get('pan'),
            expiry=values.get('expiry'),
            bid=values.get('bid'),
            merchant_id=values.get('merchant_id'),
            merchant_name=values.get('merchant_name') or values.get('name'),
            trace_id=values.get('trace_id') or values.get('inquiry_id'),
            waive_fee=bool(values.get('waive_fee')),
        )
        owner = row.userid or str(values.get('customer_id') or userid)
        return jsonify({
            'message': 'Inquiry ingested' if created else 'Inquiry already posted',
            'advice': row.to_dict(),
            'CardUpdater': _snap(service, owner, userid, actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_file(service: VauService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    payload = values.get('file') or values.get('payload') or values.get('vau1')
    if not payload:
        return jsonify({'message': 'Some data missing', 'error': 'missing_file'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        rows = service.ingest_file(actor=userid, actor_type=actor_type, payload=payload)
        owner = str(values.get('customer_id') or userid)
        return jsonify({
            'message': 'VAU1 file ingested',
            'count': len(rows),
            'advices': [row.to_dict() for row in rows],
            'CardUpdater': _snap(service, owner, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def handle_assign(service: VauService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    advice_id = str(values.get('advice_id') or '').strip()
    if not advice_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_advice'}), 400
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.assign(
            advice_id=advice_id,
            actor=userid,
            actor_type=actor_type,
            owner_userid=owner,
            key_id=values.get('key_id'),
            nickname=values.get('nickname'),
            legal_name=values.get('legal_name'),
            default_account=values.get('default_account') or values.get('account'),
        )
        return jsonify({
            'message': 'Inquiry assigned',
            'advice': row.to_dict(),
            'CardUpdater': _snap(service, owner, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_advice_route(service: VauService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    advice_id = str(values.get('advice_id') or '').strip()
    if not advice_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_advice'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'release':
            row = service.release_advice(advice_id=advice_id, actor=userid, actor_type=actor_type)
            message = 'Update released'
        elif action == 'reject':
            row = service.reject_advice(
                advice_id=advice_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Update rejected'
        elif action == 'reverse':
            row = service.reverse_advice(
                advice_id=advice_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Update reversed'
        elif action == 'customer_reverse':
            row = service.reverse_advice(
                advice_id=advice_id, actor=userid, actor_type=actor_type,
                note=values.get('note') or '', customer_request=True,
            )
            message = 'Reverse requested'
        elif action == 'override':
            row = service.override_ofac(
                advice_id=advice_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'waive':
            row = service.waive_fee(advice_id=advice_id, actor=userid, actor_type=actor_type)
            message = 'Updater fee waived'
        else:
            raise VauError('invalid_status', 'Unknown updater action.')
        return jsonify({
            'message': message,
            'advice': row.to_dict(),
            'CardUpdater': _snap(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_export(service: VauService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = None
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or '').strip() or None

    def _run():
        payload = service.export_file(actor=userid, actor_type=actor_type, userid=owner)
        return jsonify({
            'message': 'VAU1 exported',
            'file': payload,
            'CardUpdater': _snap(service, owner or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_unmatched(service: VauService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'

    def _run():
        service._require_staff(actor_type)
        rows = service.store.list_unmatched()
        return jsonify({
            'unmatched': [row.to_dict() for row in rows[:40]],
            'CardUpdater': _snap(service, userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: VauService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'CardUpdater': _snap(service, owner, userid, actor_type)}), 200


def attach_vau_routes(app, service: VauService) -> None:
    @app.route('/listCardUpdater', methods=['POST', 'GET'])
    def list_card_updater_route():
        return handle_list(service)

    @app.route('/listCardUpdaterKeys', methods=['POST', 'GET'])
    def list_card_updater_keys_route():
        return handle_list(service)

    @app.route('/listUnmatchedCardUpdates', methods=['POST', 'GET'])
    def list_unmatched_card_updates_route():
        return handle_unmatched(service)

    @app.route('/addCardUpdaterKey', methods=['POST', 'GET'])
    def add_card_updater_key_route():
        return handle_add_key(service)

    @app.route('/pauseCardUpdaterKey', methods=['POST', 'GET'])
    def pause_card_updater_key_route():
        return _key_status_route(service, KEY_PAUSED, 'Card paused')

    @app.route('/resumeCardUpdaterKey', methods=['POST', 'GET'])
    def resume_card_updater_key_route():
        return _key_status_route(service, KEY_ACTIVE, 'Card resumed')

    @app.route('/cancelCardUpdaterKey', methods=['POST', 'GET'])
    def cancel_card_updater_key_route():
        return _key_status_route(service, KEY_CANCELLED, 'Card cancelled')

    @app.route('/previewCardUpdate', methods=['POST', 'GET'])
    def preview_card_update_route():
        return handle_preview(service)

    @app.route('/reportCardUpdate', methods=['POST', 'GET'])
    def report_card_update_route():
        return handle_report(service)

    @app.route('/cancelCardUpdate', methods=['POST', 'GET'])
    def cancel_card_update_route():
        return handle_cancel(service)

    @app.route('/claimCardUpdate', methods=['POST', 'GET'])
    def claim_card_update_route():
        return handle_claim(service)

    @app.route('/requestCardUpdateReverse', methods=['POST', 'GET'])
    def request_card_update_reverse_route():
        return _staff_advice_route(service, 'customer_reverse')

    @app.route('/addCardUpdaterMerchant', methods=['POST', 'GET'])
    def add_card_updater_merchant_route():
        return handle_add_merchant(service)

    @app.route('/listCardUpdaterMerchants', methods=['POST', 'GET'])
    def list_card_updater_merchants_route():
        return handle_list(service)

    @app.route('/pauseCardUpdaterMerchant', methods=['POST', 'GET'])
    def pause_card_updater_merchant_route():
        return _merchant_status_route(service, MERCH_PAUSED, 'Merchant paused')

    @app.route('/resumeCardUpdaterMerchant', methods=['POST', 'GET'])
    def resume_card_updater_merchant_route():
        return _merchant_status_route(service, MERCH_ACTIVE, 'Merchant resumed')

    @app.route('/archiveCardUpdaterMerchant', methods=['POST', 'GET'])
    def archive_card_updater_merchant_route():
        return _merchant_status_route(service, MERCH_ARCHIVED, 'Merchant archived')

    @app.route('/ingestCardUpdateInquiry', methods=['POST', 'GET'])
    def ingest_card_update_inquiry_route():
        return handle_ingest(service)

    @app.route('/ingestCardUpdateFile', methods=['POST', 'GET'])
    def ingest_card_update_file_route():
        return handle_ingest_file(service)

    @app.route('/assignCardUpdate', methods=['POST', 'GET'])
    def assign_card_update_route():
        return handle_assign(service)

    @app.route('/overrideCardUpdateOfac', methods=['POST', 'GET'])
    def override_card_update_ofac_route():
        return _staff_advice_route(service, 'override')

    @app.route('/releaseCardUpdate', methods=['POST', 'GET'])
    def release_card_update_route():
        return _staff_advice_route(service, 'release')

    @app.route('/rejectCardUpdate', methods=['POST', 'GET'])
    def reject_card_update_route():
        return _staff_advice_route(service, 'reject')

    @app.route('/reverseCardUpdate', methods=['POST', 'GET'])
    def reverse_card_update_route():
        return _staff_advice_route(service, 'reverse')

    @app.route('/waiveCardUpdateFee', methods=['POST', 'GET'])
    def waive_card_update_fee_route():
        return _staff_advice_route(service, 'waive')

    @app.route('/exportCardUpdate', methods=['POST', 'GET'])
    def export_card_update_route():
        return handle_export(service)

    @app.route('/runDueCardUpdates', methods=['POST', 'GET'])
    def run_due_card_updates_route():
        return handle_run_due(service)
