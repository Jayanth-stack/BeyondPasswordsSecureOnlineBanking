"""Issuer card tokenization (Visa VTS / Mastercard MDES).

Customers enroll issued PANs. Wallets and merchants receive network tokens
(device or COF) with a PAR, token PAN, and TAVV. Provision, suspend, resume,
deactivate, and reprovision are lifecycle events — not authorizations,
presentments, OCT/AFT, or Account Updater advice.

Independent of push-to-card OCT (PR #155), inbound OCT/AFT (PR #159),
AFT origination (PR #161), issuer Account Updater (PR #157), card 3DS
(PR #153), card settlement presentment (PR #151), card authorization
holds (PR #149), outbound Fedwire (PR #73), and ACH linking (PR #68).
Existing `/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- PAN Luhn + IIN brand + SHA-256 fingerprint (never ABA / IMAD / ARN)
- Payment Account Reference (PAR) uniqueness (not VAU ref / RRN / STAN / CAVV)
- Token PAN (Luhn, distinct from FPAN) + TAVV cryptogram (never in snapshots)
- Token Requestor ID (TRID) wallet directory
- Token reference identifiers (TKN + YYYYMMDD + seq, not IMAD / VAU / RRN)
- 24/7 token-network clock (not Fed 17:00, not VAU Friday 22:00)
- TOKEN1 pipe compose/parse (XML/DOCTYPE rejected)
- Dual-control for device provision, deactivate, and reprovision

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Full PAN, token PAN, fingerprint, PAR, TAVV, TRID, and token reference never
appear in to_dict / snapshots.
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
WALLET_ACTIVE = 'active'
WALLET_PAUSED = 'paused'
WALLET_ARCHIVED = 'archived'
OPEN_WALLETS = frozenset({WALLET_ACTIVE, WALLET_PAUSED})
EVT_HELD = 'held'
EVT_QUEUED = 'queued'
EVT_PENDING = 'pending_release'
EVT_POSTED = 'posted'
EVT_REJECTED = 'rejected'
EVT_CANCELLED = 'cancelled'
EVT_REVERSED = 'reversed'
EVT_UNMATCHED = 'unmatched'
EVT_NSF = 'nsf'
EVT_FAILED = 'failed'
EVT_EXPIRED = 'expired'
EVT_STATUSES = frozenset({
    EVT_HELD, EVT_QUEUED, EVT_PENDING, EVT_POSTED, EVT_REJECTED, EVT_CANCELLED,
    EVT_REVERSED, EVT_UNMATCHED, EVT_NSF, EVT_FAILED, EVT_EXPIRED,
})
OPEN_EVENTS = frozenset({EVT_HELD, EVT_QUEUED, EVT_PENDING, EVT_UNMATCHED})
CANCELABLE = frozenset({EVT_HELD, EVT_QUEUED, EVT_PENDING})
FEE_NONE = 'none'
FEE_COLLECTED = 'collected'
FEE_WAIVED = 'waived'
FEE_NSF = 'nsf'
ACTION_P = 'P'
ACTION_S = 'S'
ACTION_R = 'R'
ACTION_D = 'D'
ACTION_X = 'X'
ACTIONS = frozenset({ACTION_P, ACTION_S, ACTION_R, ACTION_D, ACTION_X})
SENSITIVE_ACTIONS = frozenset({ACTION_P, ACTION_D, ACTION_X})
ACTION_ALIASES = {
    'provision': ACTION_P, 'tokenize': ACTION_P, 'enroll': ACTION_P, 'p': ACTION_P,
    'suspend': ACTION_S, 'pause': ACTION_S, 's': ACTION_S,
    'resume': ACTION_R, 'unsuspend': ACTION_R, 'r': ACTION_R,
    'deactivate': ACTION_D, 'delete': ACTION_D, 'close': ACTION_D, 'd': ACTION_D,
    'reprovision': ACTION_X, 'replace': ACTION_X, 'rotate': ACTION_X, 'x': ACTION_X,
}
TOKEN_PROVISIONED = 'provisioned'
TOKEN_SUSPENDED = 'suspended'
TOKEN_DEACTIVATED = 'deactivated'
TOKEN_STATUSES = frozenset({TOKEN_PROVISIONED, TOKEN_SUSPENDED, TOKEN_DEACTIVATED})
TYPE_DEVICE = 'device'
TYPE_MERCHANT = 'merchant'
TOKEN_TYPES = frozenset({TYPE_DEVICE, TYPE_MERCHANT})
WALLET_APPLE = 'apple_pay'
WALLET_GOOGLE = 'google_pay'
WALLET_SAMSUNG = 'samsung_pay'
WALLET_MERCHANT = 'merchant'
WALLET_KINDS = frozenset({WALLET_APPLE, WALLET_GOOGLE, WALLET_SAMSUNG, WALLET_MERCHANT})
WALLET_ALIASES = {
    'apple': WALLET_APPLE, 'apple_pay': WALLET_APPLE, 'applepay': WALLET_APPLE,
    'google': WALLET_GOOGLE, 'google_pay': WALLET_GOOGLE, 'googlepay': WALLET_GOOGLE,
    'samsung': WALLET_SAMSUNG, 'samsung_pay': WALLET_SAMSUNG, 'samsungpay': WALLET_SAMSUNG,
    'merchant': WALLET_MERCHANT, 'cof': WALLET_MERCHANT, 'card_on_file': WALLET_MERCHANT,
}
NETWORK_VISA = 'visa_vts'
NETWORK_MC = 'mastercard_mdes'
BRAND_VISA = 'visa'
BRAND_MC = 'mastercard'
SOURCE_ISSUER = 'issuer'
SOURCE_WALLET = 'wallet'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/token.sqlite'
CUSTOMER_REVERSE_SECONDS = 24 * 3600
STAFF_REVERSE_SECONDS = 90 * 24 * 3600
UNMATCHED_TTL_SECONDS = 24 * 3600


class TokenError(ValueError):
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
        raise TokenError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise TokenError('invalid_name', 'Legal name must be 2-80 characters.')
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


def luhn_check_digit(body: str) -> str:
    if not body or not body.isdigit():
        raise TokenError('invalid_pan', 'Token body must be digits.')
    for digit in range(10):
        if luhn_check_digit_ok(body + str(digit)):
            return str(digit)
    raise TokenError('invalid_pan', 'Unable to compute Luhn check digit.')


def normalize_pan(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not (13 <= len(digits) <= 19):
        raise TokenError('invalid_pan', 'PAN must be 13-19 digits.')
    if not luhn_check_digit_ok(digits):
        raise TokenError('invalid_pan', 'PAN failed Luhn checksum.')
    return digits


def mask_pan(pan: str) -> str:
    digits = ''.join(ch for ch in str(pan or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    brand = ''
    try:
        brand = brand_from_iin(digits)
    except TokenError:
        brand = ''
    prefix = brand + ' ' if brand else ''
    return '%s****%s' % (prefix, digits[-4:])


def pan_fingerprint(pan: str) -> str:
    return hashlib.sha256(str(pan).encode('utf-8')).hexdigest()


def brand_from_iin(pan: str) -> str:
    digits = ''.join(ch for ch in str(pan or '') if ch.isdigit())
    if not digits:
        raise TokenError('invalid_brand', 'PAN is required to derive brand.')
    if digits.startswith('4'):
        return BRAND_VISA
    two = int(digits[:2]) if len(digits) >= 2 else 0
    four = int(digits[:4]) if len(digits) >= 4 else 0
    if 51 <= two <= 55 or 2221 <= four <= 2720:
        return BRAND_MC
    if digits.startswith('34') or digits.startswith('37'):
        raise TokenError('invalid_brand', 'American Express is not on VTS/MDES.')
    if digits.startswith('6011') or digits.startswith('65') or 644 <= int(digits[:3] or '0') <= 649:
        raise TokenError('invalid_brand', 'Discover is not on VTS/MDES.')
    raise TokenError('invalid_brand', 'Unsupported card brand.')


def network_for_brand(brand: str) -> str:
    if brand == BRAND_VISA:
        return NETWORK_VISA
    if brand == BRAND_MC:
        return NETWORK_MC
    raise TokenError('invalid_brand', 'Unsupported token network.')


def normalize_expiry(value: Any, *, now_ts: Optional[float] = None, require_future: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) == 6 and digits[:2].isdigit():
        digits = digits[:2] + digits[4:]
    if len(digits) != 4:
        raise TokenError('invalid_expiry', 'Expiry must be MMYY.')
    month = int(digits[:2])
    year = int(digits[2:])
    if month < 1 or month > 12:
        raise TokenError('invalid_expiry', 'Expiry month must be 01-12.')
    if require_future and now_ts is not None:
        local = datetime.fromtimestamp(float(now_ts), tz=timezone.utc)
        current = (local.year % 100) * 100 + local.month
        candidate = year * 100 + month
        if candidate < current:
            raise TokenError('invalid_expiry', 'Expiry is in the past.')
    return '%02d%02d' % (month, year)


def mask_expiry(expiry: str) -> str:
    digits = ''.join(ch for ch in str(expiry or '') if ch.isdigit())
    if len(digits) < 2:
        return '**'
    return '**' + digits[-2:]


def normalize_action(value: Any) -> str:
    text = str(value or '').strip().lower().replace('-', '_').replace(' ', '_')
    text = ACTION_ALIASES.get(text, text.upper() if len(text) == 1 else text)
    if text in ACTIONS:
        return text
    if text.upper() in ACTIONS:
        return text.upper()
    raise TokenError('invalid_action', 'Unknown token action.')


def normalize_wallet_kind(value: Any) -> str:
    text = str(value or '').strip().lower().replace('-', '_').replace(' ', '_')
    kind = WALLET_ALIASES.get(text, text)
    if kind not in WALLET_KINDS:
        raise TokenError('invalid_wallet', 'Wallet must be apple_pay, google_pay, samsung_pay, or merchant.')
    return kind


def token_type_for_wallet(kind: str) -> str:
    return TYPE_MERCHANT if kind == WALLET_MERCHANT else TYPE_DEVICE


def normalize_trid(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 11:
        raise TokenError('invalid_trid', 'Token requestor ID must be 11 digits.')
    return digits


def mask_trid(trid: str) -> str:
    digits = ''.join(ch for ch in str(trid or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return '****' + digits[-4:]


def compose_par(pan: str, brand: str) -> str:
    """29-char Payment Account Reference. Not a VAU ref, RRN, STAN, or CAVV."""
    digest = hashlib.sha256(str(pan).encode('utf-8')).hexdigest().upper()
    prefix = 'V' if brand == BRAND_VISA else 'M'
    return (prefix + digest)[:29]


def normalize_par(value: Any) -> str:
    text = str(value or '').strip().upper()
    if not (len(text) == 29 and text[0] in {'V', 'M'} and text[1:].isalnum()):
        raise TokenError('invalid_par', 'PAR must be 29 alphanumeric characters starting with V or M.')
    return text


def mask_par(par: str) -> str:
    text = str(par or '')
    if len(text) < 5:
        return 'PAR****'
    return text[0] + '****' + text[-4:]


def compose_token_pan(pan: str, wallet: str, sequence: int) -> str:
    """Luhn token PAN on the same brand IIN, never equal to the FPAN."""
    brand = brand_from_iin(pan)
    prefix = '4' if brand == BRAND_VISA else '5555'
    length = 16
    seed = hashlib.sha256(('%s|%s|%s' % (pan, wallet, sequence)).encode('utf-8')).hexdigest()
    body_len = length - len(prefix) - 1
    seq = int(sequence)
    for bump in range(50):
        material = seed if bump == 0 else hashlib.sha256(('%s|%s' % (seed, bump)).encode('utf-8')).hexdigest()
        body = ''.join(str(int(ch, 16) % 10) for ch in material)[:body_len]
        token = prefix + body + luhn_check_digit(prefix + body)
        if token != pan and luhn_check_digit_ok(token):
            return token
    raise TokenError('invalid_pan', 'Unable to allocate a distinct token PAN.')


def compose_tavv(token_pan: str, par: str, cycle: str) -> str:
    """28-char TAVV. Not a CAVV, approval code, or VAU reference."""
    digest = hashlib.sha256(('%s|%s|%s' % (token_pan, par, cycle)).encode('utf-8')).hexdigest().upper()
    return digest[:28]


def mask_tavv(tavv: str) -> str:
    text = str(tavv or '')
    if len(text) < 4:
        return 'TAVV****'
    return 'TAVV****' + text[-4:]


def compose_token_ref(cycle_date: str, sequence: int) -> str:
    """Token reference: TKN + YYYYMMDD + 6-digit sequence. Not IMAD/ARN/RRN/VAU."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise TokenError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise TokenError('invalid_reference', 'Token sequence out of range.')
    return 'TKN%s%06d' % (day, seq)


def normalize_token_ref(value: Any) -> str:
    text = str(value or '').strip().upper()
    if not (text.startswith('TKN') and len(text) == 17 and text[3:].isdigit()):
        raise TokenError('invalid_reference', 'Token reference must be TKNYYYYMMDD######.')
    return text


def mask_token_ref(ref: str) -> str:
    text = str(ref or '')
    if len(text) < 4:
        return 'TKN****'
    return 'TKN****' + text[-4:]


def compute_fee(fee: Decimal, *, waived: bool = False) -> Decimal:
    if waived:
        return Decimal('0.00')
    return fee.quantize(MONEY_QUANTUM)


def compose_token1(
    *,
    action: str,
    pan: str,
    token_pan: str = '',
    expiry: str,
    brand: str,
    network: str,
    trid: str = '',
    wallet: str = '',
    reference: str = '',
    status: str = EVT_POSTED,
) -> str:
    return 'TOKEN1|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s' % (
        action, pan, token_pan, expiry, brand, network, trid,
        str(wallet or '').replace('|', ' '), reference, status,
    )


def parse_token1(line: str) -> Dict[str, str]:
    text = str(line or '').strip()
    lowered = text.lower()
    if '<?xml' in lowered or '<!doctype' in lowered or '<xml' in lowered:
        raise TokenError('invalid_file', 'XML/DOCTYPE is not a valid TOKEN1 record.')
    if not text:
        raise TokenError('invalid_file', 'Empty TOKEN1 record.')
    if not text.startswith('TOKEN1|'):
        raise TokenError('invalid_file', 'Record must start with TOKEN1|.')
    parts = text.split('|')
    if len(parts) < 11:
        raise TokenError('invalid_file', 'TOKEN1 record is missing fields.')
    pan = parts[2]
    if '*' in pan:
        raise TokenError('invalid_pan', 'Ingest requires a full PAN, not a mask.')
    return {
        'action': parts[1],
        'pan': pan,
        'token_pan': parts[3],
        'expiry': parts[4],
        'brand': parts[5],
        'network': parts[6],
        'trid': parts[7],
        'wallet': parts[8],
        'reference': parts[9],
        'status': parts[10],
    }


def split_token1_file(payload: Any) -> List[str]:
    text = str(payload or '')
    lowered = text.lower()
    if '<?xml' in lowered or '<!doctype' in lowered or '<xml' in lowered:
        raise TokenError('invalid_file', 'XML/DOCTYPE files are rejected.')
    lines = []
    for raw in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        item = raw.strip()
        if item:
            lines.append(item)
    if not lines:
        raise TokenError('invalid_file', 'TOKEN1 file is empty.')
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


class TokenClock:
    """Visa VTS and Mastercard MDES provision 24/7. Not Fed 17:00 or VAU Friday."""

    def __init__(self, *, tz_offset_hours: int = -4) -> None:
        self.tz_offset_hours = int(tz_offset_hours)
        self.tz = timezone(timedelta(hours=self.tz_offset_hours))

    def local_dt(self, ts: float) -> datetime:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(self.tz)

    def is_open(self, ts: float, network: str = '') -> bool:
        return True

    def cycle_date(self, ts: float, network: str = '') -> str:
        return self.local_dt(ts).date().strftime('%Y%m%d')

    def snapshot(self, ts: float) -> Dict[str, Any]:
        local = self.local_dt(ts)
        cycle = self.cycle_date(ts)
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'cutoff': 'none',
            'after_cutoff': False,
            'always_open': True,
            'visa_open': True,
            'mastercard_open': True,
            'weekday': local.strftime('%A'),
            'value_date': '%s-%s-%s' % (cycle[:4], cycle[4:6], cycle[6:]),
            'cycle_date': cycle,
        }


@dataclass
class TokenPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_provision: bool = True
    allow_credit: bool = False
    max_keys: int = 12
    max_events: int = 120
    max_wallets: int = 40
    provision_fee: Decimal = Decimal('0.15')
    tz_offset_hours: int = -4
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST

    @classmethod
    def from_env(cls) -> 'TokenPolicy':
        extra = _env_list('TOKEN_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('TOKEN_ENABLED', True),
            customer_manage=_env_bool('TOKEN_CUSTOMER_MANAGE', True),
            customer_provision=_env_bool('TOKEN_CUSTOMER_PROVISION', True),
            allow_credit=_env_bool('TOKEN_ALLOW_CREDIT', False),
            max_keys=max(1, _env_int('TOKEN_MAX_KEYS', 12)),
            max_events=max(1, _env_int('TOKEN_MAX_EVENTS', 120)),
            max_wallets=max(1, _env_int('TOKEN_MAX_WALLETS', 40)),
            provision_fee=_env_money('TOKEN_FEE', '0.15'),
            tz_offset_hours=_env_int('TOKEN_TZ_OFFSET', -4),
            watchlist=watch,
        )


@dataclass
class TokenKey:
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
class TokenWallet:
    wallet_id: str
    nickname: str
    legal_name: str
    trid: str
    network: str
    wallet_kind: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'wallet_id': self.wallet_id,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'trid_masked': mask_trid(self.trid),
            'network': self.network,
            'wallet_kind': self.wallet_kind,
            'token_type': token_type_for_wallet(self.wallet_kind),
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == WALLET_ACTIVE,
            'paused': self.status == WALLET_PAUSED,
            'archived': self.status == WALLET_ARCHIVED,
        }


@dataclass
class TokenEvent:
    event_id: str
    trace_id: str
    key_id: str
    wallet_id: str
    parent_id: str
    userid: str
    internal_account: str
    nickname: str
    legal_name: str
    pan: str
    expiry: str
    brand: str
    network: str
    fingerprint: str
    action: str
    source: str
    status: str
    token_ref: str
    token_pan: str
    par: str
    tavv: str
    token_status: str
    token_type: str
    prev_token_pan: str
    prev_tavv: str
    prev_token_ref: str
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
    trid: str = ''
    wallet_name: str = ''
    wallet_kind: str = ''
    value_date: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'event_id': self.event_id,
            'trace_id': self.trace_id,
            'key_id': self.key_id,
            'wallet_id': self.wallet_id,
            'parent_id': self.parent_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'pan_masked': mask_pan(self.pan),
            'last4': last4(self.pan),
            'expiry_masked': mask_expiry(self.expiry),
            'token_pan_masked': mask_pan(self.token_pan) if self.token_pan else '',
            'token_last4': last4(self.token_pan) if self.token_pan else '',
            'par_masked': mask_par(self.par) if self.par else '',
            'tavv_masked': mask_tavv(self.tavv) if self.tavv else '',
            'brand': self.brand,
            'network': self.network,
            'action': self.action,
            'source': self.source,
            'status': self.status,
            'token_ref_masked': mask_token_ref(self.token_ref) if self.token_ref else '',
            'token_status': self.token_status,
            'token_type': self.token_type,
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
            'trid_masked': mask_trid(self.trid) if self.trid else '',
            'wallet_name': self.wallet_name,
            'wallet_kind': self.wallet_kind,
            'value_date': self.value_date,
            'held': self.status == EVT_HELD,
            'queued': self.status == EVT_QUEUED,
            'pending_release': self.status == EVT_PENDING,
            'posted': self.status == EVT_POSTED,
            'unmatched': self.status == EVT_UNMATCHED,
            'cancelable': self.status in CANCELABLE,
        }


def _clone_key(row: TokenKey) -> TokenKey:
    return TokenKey(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_wallet(row: TokenWallet) -> TokenWallet:
    return TokenWallet(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_event(row: TokenEvent) -> TokenEvent:
    return TokenEvent(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _key_from_row(row: Any) -> TokenKey:
    return TokenKey(
        key_id=row['key_id'], userid=row['userid'], nickname=row['nickname'],
        legal_name=row['legal_name'], pan=row['pan'], expiry=row['expiry'],
        brand=row['brand'], network=row['network'], fingerprint=row['fingerprint'],
        default_account=row['default_account'], status=row['status'], actor=row['actor'],
        created_at=float(row['created_at']), updated_at=float(row['updated_at']),
    )


def _wallet_from_row(row: Any) -> TokenWallet:
    return TokenWallet(
        wallet_id=row['wallet_id'], nickname=row['nickname'], legal_name=row['legal_name'],
        trid=row['trid'], network=row['network'], wallet_kind=row['wallet_kind'],
        status=row['status'], actor=row['actor'],
        created_at=float(row['created_at']), updated_at=float(row['updated_at']),
    )


def _event_from_row(row: Any) -> TokenEvent:
    return TokenEvent(
        event_id=row['event_id'], trace_id=row['trace_id'], key_id=row['key_id'] or '',
        wallet_id=row['wallet_id'] or '', parent_id=row['parent_id'] or '',
        userid=row['userid'] or '', internal_account=row['internal_account'] or '',
        nickname=row['nickname'] or '', legal_name=row['legal_name'] or '',
        pan=row['pan'], expiry=row['expiry'], brand=row['brand'], network=row['network'],
        fingerprint=row['fingerprint'], action=row['action'], source=row['source'],
        status=row['status'], token_ref=row['token_ref'] or '', token_pan=row['token_pan'] or '',
        par=row['par'] or '', tavv=row['tavv'] or '', token_status=row['token_status'] or '',
        token_type=row['token_type'] or '', prev_token_pan=row['prev_token_pan'] or '',
        prev_tavv=row['prev_tavv'] or '', prev_token_ref=row['prev_token_ref'] or '',
        fee=row['fee'], fee_status=row['fee_status'], actor=row['actor'],
        releaser=row['releaser'] or '', ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '', created_at=float(row['created_at']),
        updated_at=float(row['updated_at']), posted_at=float(row['posted_at'] or 0),
        reversed_at=float(row['reversed_at'] or 0), note=row['note'] or '',
        trid=row['trid'] or '', wallet_name=row['wallet_name'] or '',
        wallet_kind=row['wallet_kind'] or '', value_date=row['value_date'] or '',
    )


class MemoryTokenStore:
    def __init__(self) -> None:
        self._keys: Dict[str, TokenKey] = {}
        self._wallets: Dict[str, TokenWallet] = {}
        self._events: Dict[str, TokenEvent] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_key(self, row: TokenKey) -> None:
        with self._lock:
            self._keys[row.key_id] = row

    def get_key(self, key_id: str) -> Optional[TokenKey]:
        with self._lock:
            row = self._keys.get(key_id)
            return _clone_key(row) if row else None

    def update_key(self, row: TokenKey) -> None:
        with self._lock:
            self._keys[row.key_id] = row

    def list_keys(self, userid: Optional[str] = None, *, include_cancelled: bool = True) -> List[TokenKey]:
        with self._lock:
            rows = [_clone_key(row) for row in self._keys.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_cancelled:
            rows = [row for row in rows if row.status != KEY_CANCELLED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_key_by_nickname(self, userid: str, nickname: str) -> Optional[TokenKey]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._keys.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_KEYS:
                    return _clone_key(row)
        return None

    def find_key_by_fingerprint(self, fingerprint: str, *, userid: Optional[str] = None) -> Optional[TokenKey]:
        with self._lock:
            for row in self._keys.values():
                if row.fingerprint == fingerprint and row.status in OPEN_KEYS:
                    if userid is None or row.userid == userid:
                        return _clone_key(row)
        return None

    def put_wallet(self, row: TokenWallet) -> None:
        with self._lock:
            self._wallets[row.wallet_id] = row

    def get_wallet(self, wallet_id: str) -> Optional[TokenWallet]:
        with self._lock:
            row = self._wallets.get(wallet_id)
            return _clone_wallet(row) if row else None

    def update_wallet(self, row: TokenWallet) -> None:
        with self._lock:
            self._wallets[row.wallet_id] = row

    def list_wallets(self, *, include_archived: bool = True) -> List[TokenWallet]:
        with self._lock:
            rows = [_clone_wallet(row) for row in self._wallets.values()]
        if not include_archived:
            rows = [row for row in rows if row.status != WALLET_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_wallet_by_trid(self, trid: str) -> Optional[TokenWallet]:
        with self._lock:
            for row in self._wallets.values():
                if row.trid == trid and row.status in OPEN_WALLETS:
                    return _clone_wallet(row)
        return None

    def find_wallet_by_nickname(self, nickname: str) -> Optional[TokenWallet]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._wallets.values():
                if row.nickname.lower() == wanted and row.status in OPEN_WALLETS:
                    return _clone_wallet(row)
        return None

    def put_event(self, row: TokenEvent) -> TokenEvent:
        with self._lock:
            existing_id = self._by_trace.get(row.trace_id)
            if existing_id is not None:
                return self._events[existing_id]
            self._events[row.event_id] = row
            self._by_trace[row.trace_id] = row.event_id
            return row

    def update_event(self, row: TokenEvent) -> None:
        with self._lock:
            self._events[row.event_id] = row

    def get_event(self, event_id: str) -> Optional[TokenEvent]:
        with self._lock:
            row = self._events.get(event_id)
            return _clone_event(row) if row else None

    def get_event_by_trace(self, trace_id: str) -> Optional[TokenEvent]:
        with self._lock:
            event_id = self._by_trace.get(trace_id)
            return _clone_event(self._events[event_id]) if event_id else None

    def list_events(self, userid: Optional[str] = None, key_id: Optional[str] = None) -> List[TokenEvent]:
        with self._lock:
            rows = [_clone_event(row) for row in self._events.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if key_id is not None:
            rows = [row for row in rows if row.key_id == key_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def list_unmatched(self) -> List[TokenEvent]:
        with self._lock:
            rows = [_clone_event(row) for row in self._events.values() if row.status == EVT_UNMATCHED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def next_token_sequence(self, cycle_date: str) -> int:
        prefix = 'TKN' + cycle_date
        with self._lock:
            used = [row.token_ref for row in self._events.values() if row.token_ref.startswith(prefix)]
        return len(used) + 1


class SqliteTokenStore:
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
                CREATE TABLE IF NOT EXISTS wallets (
                    wallet_id TEXT PRIMARY KEY,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    trid TEXT NOT NULL,
                    network TEXT NOT NULL,
                    wallet_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    key_id TEXT NOT NULL DEFAULT '',
                    wallet_id TEXT NOT NULL DEFAULT '',
                    parent_id TEXT NOT NULL DEFAULT '',
                    userid TEXT NOT NULL DEFAULT '',
                    internal_account TEXT NOT NULL DEFAULT '',
                    nickname TEXT NOT NULL DEFAULT '',
                    legal_name TEXT NOT NULL DEFAULT '',
                    pan TEXT NOT NULL,
                    expiry TEXT NOT NULL,
                    brand TEXT NOT NULL,
                    network TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    action TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    token_ref TEXT NOT NULL DEFAULT '',
                    token_pan TEXT NOT NULL DEFAULT '',
                    par TEXT NOT NULL DEFAULT '',
                    tavv TEXT NOT NULL DEFAULT '',
                    token_status TEXT NOT NULL DEFAULT '',
                    token_type TEXT NOT NULL DEFAULT '',
                    prev_token_pan TEXT NOT NULL DEFAULT '',
                    prev_tavv TEXT NOT NULL DEFAULT '',
                    prev_token_ref TEXT NOT NULL DEFAULT '',
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
                    trid TEXT NOT NULL DEFAULT '',
                    wallet_name TEXT NOT NULL DEFAULT '',
                    wallet_kind TEXT NOT NULL DEFAULT '',
                    value_date TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_key(self, row: TokenKey) -> None:
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

    def get_key(self, key_id: str) -> Optional[TokenKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM keys WHERE key_id = ?', (key_id,)).fetchone()
        return _key_from_row(row) if row else None

    def update_key(self, row: TokenKey) -> None:
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

    def list_keys(self, userid: Optional[str] = None, *, include_cancelled: bool = True) -> List[TokenKey]:
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

    def find_key_by_nickname(self, userid: str, nickname: str) -> Optional[TokenKey]:
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

    def find_key_by_fingerprint(self, fingerprint: str, *, userid: Optional[str] = None) -> Optional[TokenKey]:
        sql = "SELECT * FROM keys WHERE fingerprint = ? AND status IN ('active', 'paused')"
        params: List[Any] = [fingerprint]
        if userid is not None:
            sql += ' AND userid = ?'
            params.append(userid)
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return _key_from_row(row) if row else None

    def put_wallet(self, row: TokenWallet) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO wallets (
                    wallet_id, nickname, legal_name, trid, network, wallet_kind, status,
                    actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.wallet_id, row.nickname, row.legal_name, row.trid, row.network,
                    row.wallet_kind, row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_wallet(self, wallet_id: str) -> Optional[TokenWallet]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM wallets WHERE wallet_id = ?', (wallet_id,)).fetchone()
        return _wallet_from_row(row) if row else None

    def update_wallet(self, row: TokenWallet) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE wallets SET nickname=?, legal_name=?, trid=?, network=?, wallet_kind=?,
                    status=?, actor=?, updated_at=? WHERE wallet_id=?
                """,
                (
                    row.nickname, row.legal_name, row.trid, row.network, row.wallet_kind,
                    row.status, row.actor, row.updated_at, row.wallet_id,
                ),
            )
            conn.commit()

    def list_wallets(self, *, include_archived: bool = True) -> List[TokenWallet]:
        sql = 'SELECT * FROM wallets'
        if not include_archived:
            sql += " WHERE status != 'archived'"
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [_wallet_from_row(row) for row in rows]

    def find_wallet_by_trid(self, trid: str) -> Optional[TokenWallet]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM wallets WHERE trid = ? AND status IN ('active', 'paused')",
                (trid,),
            ).fetchone()
        return _wallet_from_row(row) if row else None

    def find_wallet_by_nickname(self, nickname: str) -> Optional[TokenWallet]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM wallets
                WHERE lower(nickname) = lower(?) AND status IN ('active', 'paused')
                """,
                (nickname,),
            ).fetchone()
        return _wallet_from_row(row) if row else None

    def put_event(self, row: TokenEvent) -> TokenEvent:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM events WHERE trace_id = ?', (row.trace_id,)
            ).fetchone()
            if existing is not None:
                return _event_from_row(existing)
            conn.execute(
                """
                INSERT INTO events (
                    event_id, trace_id, key_id, wallet_id, parent_id, userid, internal_account,
                    nickname, legal_name, pan, expiry, brand, network, fingerprint, action,
                    source, status, token_ref, token_pan, par, tavv, token_status, token_type,
                    prev_token_pan, prev_tavv, prev_token_ref, fee, fee_status, actor, releaser,
                    ofac_hit, ofac_match, created_at, updated_at, posted_at, reversed_at, note,
                    trid, wallet_name, wallet_kind, value_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.event_id, row.trace_id, row.key_id, row.wallet_id, row.parent_id,
                    row.userid, row.internal_account, row.nickname, row.legal_name, row.pan,
                    row.expiry, row.brand, row.network, row.fingerprint, row.action, row.source,
                    row.status, row.token_ref, row.token_pan, row.par, row.tavv, row.token_status,
                    row.token_type, row.prev_token_pan, row.prev_tavv, row.prev_token_ref,
                    row.fee, row.fee_status, row.actor, row.releaser, row.ofac_hit,
                    row.ofac_match, row.created_at, row.updated_at, row.posted_at,
                    row.reversed_at, row.note, row.trid, row.wallet_name, row.wallet_kind,
                    row.value_date,
                ),
            )
            conn.commit()
            return row

    def update_event(self, row: TokenEvent) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE events SET key_id=?, wallet_id=?, parent_id=?, userid=?, internal_account=?,
                    nickname=?, legal_name=?, pan=?, expiry=?, brand=?, network=?, fingerprint=?,
                    action=?, source=?, status=?, token_ref=?, token_pan=?, par=?, tavv=?,
                    token_status=?, token_type=?, prev_token_pan=?, prev_tavv=?, prev_token_ref=?,
                    fee=?, fee_status=?, actor=?, releaser=?, ofac_hit=?, ofac_match=?,
                    updated_at=?, posted_at=?, reversed_at=?, note=?, trid=?, wallet_name=?,
                    wallet_kind=?, value_date=?
                WHERE event_id=?
                """,
                (
                    row.key_id, row.wallet_id, row.parent_id, row.userid, row.internal_account,
                    row.nickname, row.legal_name, row.pan, row.expiry, row.brand, row.network,
                    row.fingerprint, row.action, row.source, row.status, row.token_ref,
                    row.token_pan, row.par, row.tavv, row.token_status, row.token_type,
                    row.prev_token_pan, row.prev_tavv, row.prev_token_ref, row.fee, row.fee_status,
                    row.actor, row.releaser, row.ofac_hit, row.ofac_match, row.updated_at,
                    row.posted_at, row.reversed_at, row.note, row.trid, row.wallet_name,
                    row.wallet_kind, row.value_date, row.event_id,
                ),
            )
            conn.commit()

    def get_event(self, event_id: str) -> Optional[TokenEvent]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM events WHERE event_id = ?', (event_id,)).fetchone()
        return _event_from_row(row) if row else None

    def get_event_by_trace(self, trace_id: str) -> Optional[TokenEvent]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM events WHERE trace_id = ?', (trace_id,)).fetchone()
        return _event_from_row(row) if row else None

    def list_events(self, userid: Optional[str] = None, key_id: Optional[str] = None) -> List[TokenEvent]:
        sql = 'SELECT * FROM events'
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
        return [_event_from_row(row) for row in rows]

    def list_unmatched(self) -> List[TokenEvent]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE status = 'unmatched' ORDER BY created_at DESC"
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def next_token_sequence(self, cycle_date: str) -> int:
        prefix = 'TKN' + cycle_date + '%'
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT COUNT(*) AS n FROM events WHERE token_ref LIKE ?', (prefix,),
            ).fetchone()
        return int(row['n'] if row else 0) + 1

class TokenService:
    def __init__(
        self,
        policy: TokenPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[TokenClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or TokenClock(tz_offset_hours=policy.tz_offset_hours)

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise TokenError('token_disabled', 'Card tokenization is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise TokenError('token_forbidden', 'Customers cannot manage token keys.')

    def _require_provision(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_provision:
            raise TokenError('token_forbidden', 'Customers cannot provision tokens.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise TokenError('token_forbidden', 'Staff only.')

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
            raise TokenError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise TokenError('credit_not_allowed', 'Credit accounts cannot enroll token keys.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, action: str, token_type: str) -> bool:
        if action == ACTION_P and token_type == TYPE_MERCHANT:
            return False
        if action in {ACTION_S, ACTION_R}:
            return False
        return action in SENSITIVE_ACTIONS

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
    ) -> TokenKey:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise TokenError('token_forbidden', 'Not allowed to enroll cards for this customer.')
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
            raise TokenError('key_duplicate', 'A card with that nickname already exists.')
        if self.store.find_key_by_fingerprint(fingerprint) is not None:
            raise TokenError('key_duplicate', 'That PAN is already enrolled.')
        open_rows = [row for row in self.store.list_keys(owner_userid) if row.status in OPEN_KEYS]
        if len(open_rows) >= self.policy.max_keys:
            raise TokenError('key_limit', 'Token key limit reached.')
        row = TokenKey(
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

    def get_key(self, *, key_id: str, actor: str, actor_type: str) -> TokenKey:
        self._require_enabled()
        row = self.store.get_key(key_id)
        if row is None:
            raise TokenError('key_not_found', 'Token key not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise TokenError('token_forbidden', 'Not allowed to view this key.')
        return row

    def enforce_key(
        self,
        *,
        key_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> TokenKey:
        row = self.get_key(key_id=key_id, actor=actor, actor_type=actor_type)
        if row.status == KEY_CANCELLED:
            raise TokenError('already_cancelled', 'Key is cancelled.')
        if row.status == KEY_PAUSED:
            raise TokenError('key_paused', 'Key is paused.')
        if require_active and row.status != KEY_ACTIVE:
            raise TokenError('invalid_status', 'Key is not active.')
        return row

    def set_key_status(
        self,
        *,
        key_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> TokenKey:
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
            raise TokenError('invalid_status', 'Status must be pause, resume, or cancel.')
        if row.status == KEY_CANCELLED:
            raise TokenError('already_cancelled', 'Key is already cancelled.')
        if wanted == KEY_PAUSED:
            if row.status == KEY_PAUSED:
                raise TokenError('already_paused', 'Key is already paused.')
            if row.status != KEY_ACTIVE:
                raise TokenError('invalid_status', 'Only an active key can be paused.')
        elif wanted == KEY_ACTIVE:
            if row.status == KEY_ACTIVE:
                raise TokenError('already_active', 'Key is already active.')
            if row.status != KEY_PAUSED:
                raise TokenError('invalid_status', 'Only a paused key can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_key(row)
        if wanted == KEY_ACTIVE:
            self._rematch_unmatched(row)
        return row

    def add_wallet(
        self,
        *,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        trid: Any,
        network: Any = None,
        wallet_kind: Any = None,
    ) -> TokenWallet:
        self._require_staff(actor_type)
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        requestor = normalize_trid(trid)
        kind = normalize_wallet_kind(wallet_kind or WALLET_APPLE)
        net = str(network or NETWORK_VISA).strip().lower().replace('-', '_')
        if net in {'visa', 'vts', 'visa_vts'}:
            net = NETWORK_VISA
        elif net in {'mastercard', 'mc', 'mdes', 'mastercard_mdes'}:
            net = NETWORK_MC
        else:
            raise TokenError('invalid_brand', 'Wallet network must be visa_vts or mastercard_mdes.')
        if self.store.find_wallet_by_nickname(name) is not None:
            raise TokenError('wallet_duplicate', 'A wallet with that nickname already exists.')
        if self.store.find_wallet_by_trid(requestor) is not None:
            raise TokenError('wallet_duplicate', 'That TRID is already on file.')
        open_rows = [row for row in self.store.list_wallets() if row.status in OPEN_WALLETS]
        if len(open_rows) >= self.policy.max_wallets:
            raise TokenError('wallet_limit', 'Wallet directory limit reached.')
        now = float(self.clock())
        row = TokenWallet(
            wallet_id=uuid.uuid4().hex,
            nickname=name,
            legal_name=legal,
            trid=requestor,
            network=net,
            wallet_kind=kind,
            status=WALLET_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_wallet(row)
        return row

    def get_wallet(self, *, wallet_id: str, actor_type: str) -> TokenWallet:
        self._require_staff(actor_type)
        row = self.store.get_wallet(wallet_id)
        if row is None:
            raise TokenError('wallet_not_found', 'Wallet not found.')
        return row

    def set_wallet_status(
        self,
        *,
        wallet_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> TokenWallet:
        self._require_staff(actor_type)
        row = self.get_wallet(wallet_id=wallet_id, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': WALLET_PAUSED, 'resume': WALLET_ACTIVE, 'activate': WALLET_ACTIVE,
            'archive': WALLET_ARCHIVED, 'close': WALLET_ARCHIVED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {WALLET_PAUSED, WALLET_ACTIVE, WALLET_ARCHIVED}:
            raise TokenError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == WALLET_ARCHIVED:
            raise TokenError('already_archived', 'Wallet is already archived.')
        if wanted == WALLET_PAUSED and row.status == WALLET_PAUSED:
            raise TokenError('already_paused', 'Wallet is already paused.')
        if wanted == WALLET_ACTIVE and row.status == WALLET_ACTIVE:
            raise TokenError('already_active', 'Wallet is already active.')
        if wanted == WALLET_ACTIVE and row.status != WALLET_PAUSED:
            raise TokenError('invalid_status', 'Only a paused wallet can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_wallet(row)
        return row

    def _live_token(self, key_id: str, wallet_id: str = '', wallet_kind: str = '') -> Optional[TokenEvent]:
        for row in self.store.list_events(key_id=key_id):
            if row.action != ACTION_P or row.status != EVT_POSTED:
                continue
            if wallet_id and row.wallet_id and row.wallet_id != wallet_id:
                continue
            if wallet_kind and row.wallet_kind and row.wallet_kind != wallet_kind:
                continue
            if row.token_status in {TOKEN_PROVISIONED, TOKEN_SUSPENDED}:
                return row
        return None

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        key_id: Any,
        action: Any = ACTION_P,
        wallet_id: Any = None,
        wallet_kind: Any = None,
        waive_fee: bool = False,
    ) -> Dict[str, Any]:
        self._require_provision(actor_type)
        key = self.enforce_key(key_id=str(key_id or '').strip(), actor=actor, actor_type=actor_type)
        if key.userid != owner_userid:
            raise TokenError('token_forbidden', 'Key does not belong to this customer.')
        code = normalize_action(action)
        kind = normalize_wallet_kind(wallet_kind or WALLET_APPLE)
        wallet = None
        if wallet_id:
            wallet = self.store.get_wallet(str(wallet_id).strip())
            if wallet is None:
                raise TokenError('wallet_not_found', 'Wallet not found.')
            kind = wallet.wallet_kind
        token_type = token_type_for_wallet(kind)
        now = float(self.clock())
        ofac = self._screen(key.legal_name, aliases=(key.nickname,))
        fee = compute_fee(self.policy.provision_fee, waived=bool(waive_fee) and actor_type in EMPLOYEE_ROLES)
        if code != ACTION_P:
            fee = Decimal('0.00')
        return {
            'action': code,
            'fee': money_str(fee),
            'dual_control': self._needs_dual_control(code, token_type),
            'ofac': ofac.to_dict(),
            'key': key.to_dict(),
            'wallet_kind': kind,
            'token_type': token_type,
            'clock': self.calendar.snapshot(now),
            'network_open': self.calendar.is_open(now, key.network),
        }

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        key: Optional[TokenKey],
        wallet: Optional[TokenWallet],
        parent: Optional[TokenEvent],
        account: str,
        action: str,
        source: str,
        pan: str,
        expiry: str,
        brand: str,
        network: str,
        fingerprint: str,
        ofac: ScreenResult,
        trace_id: str,
        waive_fee: bool,
        nickname: str,
        legal_name: str,
        wallet_kind: str,
        token_type: str,
    ) -> TokenEvent:
        now = float(self.clock())
        charge = action == ACTION_P
        fee = compute_fee(self.policy.provision_fee, waived=waive_fee) if charge else Decimal('0.00')
        fee_status = FEE_WAIVED if (waive_fee or fee == 0) else FEE_NONE
        if key is None:
            status = EVT_UNMATCHED
        elif ofac.hit:
            status = EVT_HELD
        elif self._needs_dual_control(action, token_type):
            status = EVT_PENDING
        else:
            status = EVT_POSTED
        event = TokenEvent(
            event_id=uuid.uuid4().hex,
            trace_id=trace_id,
            key_id=key.key_id if key else '',
            wallet_id=wallet.wallet_id if wallet else '',
            parent_id=parent.event_id if parent else '',
            userid=owner_userid,
            internal_account=account,
            nickname=nickname,
            legal_name=legal_name,
            pan=pan,
            expiry=expiry,
            brand=brand,
            network=network,
            fingerprint=fingerprint,
            action=action,
            source=source,
            status=status,
            token_ref='',
            token_pan=parent.token_pan if parent else '',
            par=parent.par if parent else (compose_par(pan, brand) if key else ''),
            tavv=parent.tavv if parent else '',
            token_status=parent.token_status if parent else '',
            token_type=token_type,
            prev_token_pan=parent.token_pan if parent else '',
            prev_tavv=parent.tavv if parent else '',
            prev_token_ref=parent.token_ref if parent else '',
            fee=money_str(fee),
            fee_status=fee_status,
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
            trid=wallet.trid if wallet else '',
            wallet_name=wallet.legal_name if wallet else wallet_kind,
            wallet_kind=wallet_kind,
            value_date=self.calendar.cycle_date(now, network),
        )
        if status == EVT_POSTED:
            self._transmit(event, key=key, parent=parent, actor=actor)
        return event

    def _transmit(
        self,
        event: TokenEvent,
        *,
        key: Optional[TokenKey],
        parent: Optional[TokenEvent],
        actor: str,
    ) -> TokenEvent:
        now = float(self.clock())
        cycle = event.value_date or self.calendar.cycle_date(now, event.network)
        seq = self.store.next_token_sequence(cycle)
        event.token_ref = compose_token_ref(cycle, seq)
        fee = parse_money(event.fee, allow_zero=True)
        status = EVT_POSTED
        fail_note = ''
        if fee > 0 and event.fee_status != FEE_WAIVED and self.debit_fn is not None and event.internal_account:
            try:
                result = self.debit_fn(
                    event.internal_account, money_str(fee), 'token fee %s' % event.token_ref[:12],
                )
            except Exception as exc:
                status = EVT_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = EVT_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = EVT_FAILED
                    fail_note = str(result)[:240]
                else:
                    event.fee_status = FEE_COLLECTED
        elif fee == 0 or event.fee_status == FEE_WAIVED:
            event.fee_status = FEE_WAIVED
        event.status = status
        event.updated_at = now
        if status == EVT_POSTED:
            event.posted_at = now
            event.releaser = str(actor)
            self._apply_token_mutation(event, parent=parent, now=now, sequence=seq)
        else:
            event.token_ref = ''
            event.note = fail_note
            event.fee_status = FEE_NSF if status == EVT_NSF else event.fee_status
        return event

    def _apply_token_mutation(
        self,
        event: TokenEvent,
        *,
        parent: Optional[TokenEvent],
        now: float,
        sequence: int,
    ) -> None:
        if event.action == ACTION_P:
            event.token_pan = compose_token_pan(event.pan, event.wallet_kind or event.trid or 'wallet', sequence)
            event.par = event.par or compose_par(event.pan, event.brand)
            event.tavv = compose_tavv(event.token_pan, event.par, event.value_date)
            event.token_status = TOKEN_PROVISIONED
            return
        live = parent or (self.store.get_event(event.parent_id) if event.parent_id else None)
        if live is None:
            raise TokenError('token_not_found', 'No live token for this action.')
        if event.action == ACTION_S:
            if live.token_status != TOKEN_PROVISIONED:
                raise TokenError('not_suspendable', 'Only a provisioned token can be suspended.')
            live.token_status = TOKEN_SUSPENDED
            event.token_status = TOKEN_SUSPENDED
        elif event.action == ACTION_R:
            if live.token_status != TOKEN_SUSPENDED:
                raise TokenError('not_resumable', 'Only a suspended token can be resumed.')
            live.token_status = TOKEN_PROVISIONED
            event.token_status = TOKEN_PROVISIONED
        elif event.action == ACTION_D:
            if live.token_status == TOKEN_DEACTIVATED:
                raise TokenError('already_deactivated', 'Token is already deactivated.')
            live.token_status = TOKEN_DEACTIVATED
            event.token_status = TOKEN_DEACTIVATED
        elif event.action == ACTION_X:
            if live.token_status == TOKEN_DEACTIVATED:
                raise TokenError('not_reprovisionable', 'Deactivated tokens cannot be reprovisioned.')
            event.prev_token_pan = live.token_pan
            event.prev_tavv = live.tavv
            event.prev_token_ref = live.token_ref
            event.token_pan = compose_token_pan(event.pan, event.wallet_kind or 'wallet', sequence + 17)
            event.par = live.par
            event.tavv = compose_tavv(event.token_pan, event.par, event.value_date)
            event.token_status = TOKEN_PROVISIONED
            live.token_pan = event.token_pan
            live.tavv = event.tavv
            live.token_ref = event.token_ref
            live.token_status = TOKEN_PROVISIONED
        else:
            raise TokenError('invalid_action', 'Unknown token action.')
        event.token_pan = event.token_pan or live.token_pan
        event.par = event.par or live.par
        event.tavv = event.tavv or live.tavv
        live.updated_at = now
        self.store.update_event(live)

    def _restore_token(self, event: TokenEvent, now: float) -> None:
        if event.action == ACTION_P:
            event.token_status = TOKEN_DEACTIVATED
            return
        live = self.store.get_event(event.parent_id) if event.parent_id else None
        if live is None:
            return
        if event.action == ACTION_S:
            live.token_status = TOKEN_PROVISIONED
        elif event.action == ACTION_R:
            live.token_status = TOKEN_SUSPENDED
        elif event.action == ACTION_D:
            live.token_status = TOKEN_SUSPENDED if event.prev_tavv and event.token_status == TOKEN_DEACTIVATED and event.note == 'was_suspended' else TOKEN_PROVISIONED
        elif event.action == ACTION_X:
            live.token_pan = event.prev_token_pan or live.token_pan
            live.tavv = event.prev_tavv or live.tavv
            live.token_ref = event.prev_token_ref or live.token_ref
            live.token_status = TOKEN_PROVISIONED
        live.updated_at = now
        self.store.update_event(live)

    def provision(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        key_id: Any,
        wallet_id: Any = None,
        wallet_kind: Any = None,
        trace_id: Any = None,
        waive_fee: bool = False,
        action: Any = ACTION_P,
        event_id: Any = None,
    ) -> Tuple[TokenEvent, bool]:
        self._require_provision(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise TokenError('token_forbidden', 'Not allowed to provision tokens for this customer.')
        key = self.enforce_key(key_id=str(key_id or '').strip(), actor=actor, actor_type=actor_type)
        if key.userid != owner_userid:
            raise TokenError('token_forbidden', 'Key does not belong to this customer.')
        code = normalize_action(action)
        trace = normalize_id(trace_id)
        existing = self.store.get_event_by_trace(trace)
        if existing is not None:
            return existing, False
        wallet = None
        kind = normalize_wallet_kind(wallet_kind or WALLET_APPLE)
        if wallet_id:
            wallet = self.store.get_wallet(str(wallet_id).strip())
            if wallet is None:
                raise TokenError('wallet_not_found', 'Wallet not found.')
            if wallet.status != WALLET_ACTIVE:
                raise TokenError('wallet_paused', 'Wallet is not active.')
            kind = wallet.wallet_kind
        token_type = token_type_for_wallet(kind)
        parent = None
        if code != ACTION_P:
            parent = self.store.get_event(str(event_id).strip()) if event_id else self._live_token(
                key.key_id, wallet.wallet_id if wallet else '', kind,
            )
            if parent is None or parent.status != EVT_POSTED:
                raise TokenError('token_not_found', 'No live token for this action.')
        else:
            live = self._live_token(key.key_id, wallet.wallet_id if wallet else '', kind)
            if live is not None:
                raise TokenError('already_provisioned', 'That wallet already has a live token.')
        if len(self.store.list_events(owner_userid)) >= self.policy.max_events:
            raise TokenError('event_limit', 'Token history limit reached.')
        ofac = self._screen(key.legal_name, aliases=(key.nickname,))
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        event = self._place(
            owner_userid=owner_userid,
            actor=actor,
            key=key,
            wallet=wallet,
            parent=parent,
            account=key.default_account,
            action=code,
            source=SOURCE_ISSUER,
            pan=key.pan,
            expiry=key.expiry,
            brand=key.brand,
            network=key.network,
            fingerprint=key.fingerprint,
            ofac=ofac,
            trace_id=trace,
            waive_fee=staff_waive,
            nickname=key.nickname,
            legal_name=key.legal_name,
            wallet_kind=kind,
            token_type=token_type,
        )
        stored = self.store.put_event(event)
        if stored.event_id != event.event_id:
            return stored, False
        if stored.status == EVT_NSF:
            raise TokenError('nsf', 'Insufficient funds for token fee.', event=stored)
        if stored.status == EVT_FAILED:
            raise TokenError('failed', 'Token fee debit did not complete.', event=stored)
        return stored, True

    def ingest_inquiry(
        self,
        *,
        actor: str,
        actor_type: str,
        pan: Any,
        expiry: Any,
        trid: Any = None,
        wallet_id: Any = None,
        wallet_name: Any = None,
        wallet_kind: Any = None,
        trace_id: Any = None,
        waive_fee: bool = False,
    ) -> Tuple[TokenEvent, bool]:
        self._require_staff(actor_type)
        now = float(self.clock())
        card = normalize_pan(pan)
        exp = normalize_expiry(expiry)
        brand = brand_from_iin(card)
        network = network_for_brand(brand)
        fingerprint = pan_fingerprint(card)
        trace = normalize_id(trace_id)
        existing = self.store.get_event_by_trace(trace)
        if existing is not None:
            return existing, False
        wallet = None
        if wallet_id:
            wallet = self.store.get_wallet(str(wallet_id).strip())
            if wallet is None:
                raise TokenError('wallet_not_found', 'Wallet not found.')
        elif trid:
            wallet = self.store.find_wallet_by_trid(normalize_trid(trid))
            if wallet is None:
                raise TokenError('wallet_not_found', 'Wallet TRID is not enrolled.')
        if wallet is not None and wallet.status != WALLET_ACTIVE:
            raise TokenError('wallet_paused', 'Wallet is not active.')
        kind = wallet.wallet_kind if wallet else normalize_wallet_kind(wallet_kind or WALLET_MERCHANT)
        token_type = token_type_for_wallet(kind)
        screen_target = (wallet.legal_name if wallet else '') or str(wallet_name or '')
        ofac = self._screen(screen_target, aliases=(wallet.nickname,) if wallet else ())
        key = self.store.find_key_by_fingerprint(fingerprint)
        owner = key.userid if key else ''
        account = key.default_account if key else ''
        nickname = key.nickname if key else ''
        legal = key.legal_name if key else (wallet.legal_name if wallet else '')
        event = self._place(
            owner_userid=owner,
            actor=actor,
            key=key,
            wallet=wallet,
            parent=None,
            account=account,
            action=ACTION_P,
            source=SOURCE_WALLET,
            pan=card,
            expiry=exp,
            brand=brand,
            network=network,
            fingerprint=fingerprint,
            ofac=ofac,
            trace_id=trace,
            waive_fee=bool(waive_fee),
            nickname=nickname,
            legal_name=legal,
            wallet_kind=kind,
            token_type=token_type,
        )
        if wallet:
            event.trid = wallet.trid
            event.wallet_name = wallet.legal_name
        stored = self.store.put_event(event)
        if stored.event_id != event.event_id:
            return stored, False
        if stored.status == EVT_NSF:
            raise TokenError('nsf', 'Insufficient funds for token fee.', event=stored)
        if stored.status == EVT_FAILED:
            raise TokenError('failed', 'Token fee debit did not complete.', event=stored)
        return stored, True

    def ingest_file(self, *, actor: str, actor_type: str, payload: Any) -> List[TokenEvent]:
        self._require_staff(actor_type)
        rows = []
        for line in split_token1_file(payload):
            parsed = parse_token1(line)
            raw_wallet = str(parsed.get('wallet') or '').strip()
            kind = None
            try:
                if raw_wallet:
                    kind = normalize_wallet_kind(raw_wallet)
            except TokenError:
                kind = None
            event, _created = self.ingest_inquiry(
                actor=actor,
                actor_type=actor_type,
                pan=parsed['pan'],
                expiry=parsed['expiry'],
                trid=parsed.get('trid') or None,
                wallet_name=raw_wallet or None,
                wallet_kind=kind,
                trace_id=parsed.get('reference') or None,
            )
            rows.append(event)
        return rows

    def _rematch_unmatched(self, key: TokenKey) -> None:
        for row in self.store.list_unmatched():
            if row.fingerprint != key.fingerprint:
                continue
            row.key_id = key.key_id
            row.userid = key.userid
            row.internal_account = key.default_account
            row.nickname = key.nickname
            row.legal_name = key.legal_name
            ofac = self._screen(key.legal_name, aliases=(key.nickname, row.wallet_name))
            row.ofac_hit = 1 if ofac.hit else 0
            row.ofac_match = ofac.matched
            now = float(self.clock())
            if ofac.hit:
                row.status = EVT_HELD
            elif self._needs_dual_control(row.action, row.token_type):
                row.status = EVT_PENDING
            else:
                parent = self.store.get_event(row.parent_id) if row.parent_id else None
                self._transmit(row, key=key, parent=parent, actor=key.actor)
            row.updated_at = now
            self.store.update_event(row)

    def claim(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        event_id: Any = None,
        pan: Any = None,
        last4_digits: Any = None,
        brand: Any = None,
        expiry: Any = None,
        default_account: Any = None,
        nickname: Any = None,
        legal_name: Any = None,
    ) -> TokenEvent:
        self._require_provision(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise TokenError('token_forbidden', 'Not allowed to claim this inquiry.')
        target = None
        if event_id:
            target = self.store.get_event(str(event_id).strip())
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
        if target is None or target.status != EVT_UNMATCHED:
            raise TokenError('not_claimable', 'No unmatched inquiry matches that card.')
        now = float(self.clock())
        key = self.store.find_key_by_fingerprint(target.fingerprint, userid=owner_userid)
        if key is None:
            if not default_account:
                raise TokenError('invalid_account', 'An internal account is required to enroll the claimed card.')
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
            rematched = self.store.get_event(target.event_id)
            if rematched is None:
                raise TokenError('event_not_found', 'Inquiry disappeared during claim.')
            return rematched
        if key.status != KEY_ACTIVE:
            raise TokenError('key_paused', 'Matching key is not active.')
        target.key_id = key.key_id
        target.userid = key.userid
        target.internal_account = key.default_account
        target.nickname = key.nickname
        target.legal_name = key.legal_name
        ofac = self._screen(key.legal_name, aliases=(key.nickname, target.wallet_name))
        target.ofac_hit = 1 if ofac.hit else 0
        target.ofac_match = ofac.matched
        if ofac.hit:
            target.status = EVT_HELD
        elif self._needs_dual_control(target.action, target.token_type):
            target.status = EVT_PENDING
        else:
            parent = self.store.get_event(target.parent_id) if target.parent_id else None
            self._transmit(target, key=key, parent=parent, actor=actor)
        target.updated_at = now
        self.store.update_event(target)
        if target.status == EVT_NSF:
            raise TokenError('nsf', 'Insufficient funds for token fee.', event=target)
        if target.status == EVT_FAILED:
            raise TokenError('failed', 'Token fee debit did not complete.', event=target)
        return target

    def assign(
        self,
        *,
        event_id: str,
        actor: str,
        actor_type: str,
        owner_userid: str,
        key_id: Any = None,
        nickname: Any = None,
        legal_name: Any = None,
        default_account: Any = None,
    ) -> TokenEvent:
        self._require_staff(actor_type)
        row = self.store.get_event(event_id)
        if row is None:
            raise TokenError('event_not_found', 'Inquiry not found.')
        if row.status != EVT_UNMATCHED:
            raise TokenError('not_assignable', 'Only unmatched inquiries can be assigned.')
        if not owner_userid:
            raise TokenError('missing_customer_id', 'Customer id is required.')
        if key_id:
            key = self.get_key(key_id=str(key_id).strip(), actor=actor, actor_type=actor_type)
            if key.userid != owner_userid:
                raise TokenError('token_forbidden', 'Key does not belong to this customer.')
            if key.fingerprint != row.fingerprint:
                raise TokenError('invalid_pan', 'Key PAN does not match the inquiry.')
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
        rematched = self.store.get_event(row.event_id)
        if rematched is None:
            raise TokenError('event_not_found', 'Inquiry disappeared during assign.')
        if rematched.status == EVT_UNMATCHED:
            return self.claim(
                owner_userid=owner_userid, actor=actor, actor_type=actor_type,
                event_id=row.event_id, default_account=key.default_account,
            )
        return rematched

    def get_event(self, *, event_id: str, actor: str, actor_type: str) -> TokenEvent:
        self._require_enabled()
        row = self.store.get_event(event_id)
        if row is None:
            raise TokenError('event_not_found', 'Token event not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise TokenError('token_forbidden', 'Not allowed to view this event.')
        return row

    def cancel_event(
        self,
        *,
        event_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> TokenEvent:
        self._require_provision(actor_type)
        row = self.get_event(event_id=event_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise TokenError('token_forbidden', 'Not allowed to cancel this event.')
        if row.status not in CANCELABLE:
            raise TokenError('not_cancelable', 'Only held, queued, or pending events can be cancelled.')
        row.status = EVT_CANCELLED
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        row.note = normalize_note(note)
        self.store.update_event(row)
        return row

    def waive_fee(self, *, event_id: str, actor: str, actor_type: str) -> TokenEvent:
        self._require_staff(actor_type)
        row = self.get_event(event_id=event_id, actor=actor, actor_type=actor_type)
        if row.status not in CANCELABLE:
            raise TokenError('invalid_status', 'Fee can only be waived before the event is posted.')
        row.fee = money_str(Decimal('0.00'))
        row.fee_status = FEE_WAIVED
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_event(row)
        return row

    def override_ofac(self, *, event_id: str, actor: str, actor_type: str, note: Any = '') -> TokenEvent:
        self._require_staff(actor_type)
        row = self.get_event(event_id=event_id, actor=actor, actor_type=actor_type)
        if row.status != EVT_HELD:
            raise TokenError('not_overridable', 'Only an OFAC hold can be overridden.')
        now = float(self.clock())
        row.ofac_hit = 0
        row.note = normalize_note(note) or 'ofac override'
        row.actor = str(actor)
        row.updated_at = now
        key = self.store.get_key(row.key_id) if row.key_id else None
        if self._needs_dual_control(row.action, row.token_type):
            row.status = EVT_PENDING
        elif key is None:
            row.status = EVT_UNMATCHED
        else:
            parent = self.store.get_event(row.parent_id) if row.parent_id else None
            self._transmit(row, key=key, parent=parent, actor=actor)
        self.store.update_event(row)
        if row.status == EVT_NSF:
            raise TokenError('nsf', 'Insufficient funds for token fee.', event=row)
        if row.status == EVT_FAILED:
            raise TokenError('failed', 'Token fee debit did not complete.', event=row)
        return row

    def release_event(self, *, event_id: str, actor: str, actor_type: str) -> TokenEvent:
        self._require_staff(actor_type)
        row = self.get_event(event_id=event_id, actor=actor, actor_type=actor_type)
        if row.status == EVT_HELD:
            raise TokenError('ofac_hold', 'OFAC hold must be overridden before release.')
        if row.status not in {EVT_PENDING, EVT_QUEUED}:
            raise TokenError('not_releasable', 'Only queued or pending events can be released.')
        if (
            row.status == EVT_PENDING
            and row.actor
            and str(actor) == str(row.actor)
            and self._needs_dual_control(row.action, row.token_type)
        ):
            raise TokenError('same_approver', 'A different employee must release this token.')
        key = self.store.get_key(row.key_id) if row.key_id else None
        parent = self.store.get_event(row.parent_id) if row.parent_id else None
        self._transmit(row, key=key, parent=parent, actor=actor)
        self.store.update_event(row)
        if row.status == EVT_NSF:
            raise TokenError('nsf', 'Insufficient funds for token fee.', event=row)
        if row.status == EVT_FAILED:
            raise TokenError('failed', 'Token fee debit did not complete.', event=row)
        return row

    def reject_event(
        self,
        *,
        event_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> TokenEvent:
        self._require_staff(actor_type)
        row = self.get_event(event_id=event_id, actor=actor, actor_type=actor_type)
        if row.status not in CANCELABLE and row.status != EVT_UNMATCHED:
            raise TokenError('not_rejectable', 'Only held, queued, pending, or unmatched events can be rejected.')
        row.status = EVT_REJECTED
        row.note = normalize_note(note) or normalize_note(reason, limit=40)
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_event(row)
        return row

    def reverse_event(
        self,
        *,
        event_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
        customer_request: bool = False,
    ) -> TokenEvent:
        if customer_request:
            self._require_provision(actor_type)
        else:
            self._require_staff(actor_type)
        row = self.get_event(event_id=event_id, actor=actor, actor_type=actor_type)
        if row.status == EVT_REVERSED:
            raise TokenError('already_reversed', 'Event is already reversed.')
        if row.status != EVT_POSTED:
            raise TokenError('not_reversible', 'Only posted events can be reversed.')
        now = float(self.clock())
        age = now - (row.posted_at or row.created_at)
        if customer_request:
            if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
                raise TokenError('token_forbidden', 'Not allowed to reverse this event.')
            if age > CUSTOMER_REVERSE_SECONDS:
                raise TokenError('return_window_closed', 'Customer reverse window is 24 hours.')
        elif age > STAFF_REVERSE_SECONDS:
            raise TokenError('return_window_closed', 'Staff reverse window is 90 days.')
        fee = parse_money(row.fee, allow_zero=True)
        if fee > 0 and row.fee_status == FEE_COLLECTED and self.credit_fn is not None and row.internal_account:
            try:
                result = self.credit_fn(
                    row.internal_account, money_str(fee), 'token reverse %s' % (row.token_ref[-12:] or row.event_id[:12]),
                )
            except Exception as exc:
                raise TokenError('reverse_failed', 'Reverse credit failed.', event=row) from exc
            if _classify_money_result(result) != 'ok':
                raise TokenError('reverse_failed', 'Reverse credit failed.', event=row)
        self._restore_token(row, now)
        row.status = EVT_REVERSED
        row.reversed_at = now
        row.updated_at = now
        row.actor = str(actor)
        row.note = normalize_note(note) or 'reversed'
        if row.action == ACTION_P:
            row.token_status = TOKEN_DEACTIVATED
        self.store.update_event(row)
        return row

    def export_file(self, *, actor: str, actor_type: str, userid: Optional[str] = None) -> str:
        self._require_staff(actor_type)
        rows = self.store.list_events(userid)
        lines = []
        for row in rows:
            if row.status != EVT_POSTED:
                continue
            lines.append(compose_token1(
                action=row.action,
                pan=mask_pan(row.pan),
                token_pan=mask_pan(row.token_pan) if row.token_pan else '',
                expiry=mask_expiry(row.expiry),
                brand=row.brand,
                network=row.network,
                trid=mask_trid(row.trid) if row.trid else '',
                wallet=row.wallet_name,
                reference=mask_token_ref(row.token_ref),
                status=row.status,
            ))
        if not lines:
            raise TokenError('not_exportable', 'No posted token events to export.')
        return '\n'.join(lines)

    def run_due(self, userid: Optional[str] = None) -> List[TokenEvent]:
        now = float(self.clock())
        changed: List[TokenEvent] = []
        for row in self.store.list_events(userid):
            if row.status == EVT_UNMATCHED and (now - row.created_at) >= UNMATCHED_TTL_SECONDS:
                row.status = EVT_EXPIRED
                row.updated_at = now
                self.store.update_event(row)
                changed.append(row)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.run_due(userid)
        keys = self.store.list_keys(userid)
        events = self.store.list_events(userid)
        fee_ytd = Decimal('0.00')
        posted = 0
        live = 0
        for row in events:
            if row.status == EVT_POSTED:
                posted += 1
                if row.fee_status == FEE_COLLECTED:
                    fee_ytd += parse_money(row.fee, allow_zero=True)
                if row.action == ACTION_P and row.token_status == TOKEN_PROVISIONED:
                    live += 1
        now = float(self.clock())
        payload = {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'fee': money_str(self.policy.provision_fee),
            'clock': self.calendar.snapshot(now),
            'keys': [row.to_dict() for row in keys[:40]],
            'events': [row.to_dict() for row in events[:40]],
            'wallets': [],
            'unmatched': [],
            'ytd_fees': money_str(fee_ytd),
            'posted_count': posted,
            'live_count': live,
            'active_count': sum(1 for row in keys if row.status == KEY_ACTIVE),
            'open_count': sum(1 for row in events if row.status in OPEN_EVENTS),
        }
        if actor_type in EMPLOYEE_ROLES:
            payload['wallets'] = [row.to_dict() for row in self.store.list_wallets()[:40]]
            payload['unmatched'] = [row.to_dict() for row in self.store.list_unmatched()[:40]]
        return payload


_SERVICE: Optional[TokenService] = None


def set_service(service: Optional[TokenService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[TokenService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('TOKEN_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryTokenStore()
    path = os.environ.get('TOKEN_DB', DEFAULT_STORE_PATH)
    return SqliteTokenStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[TokenClock] = None,
) -> TokenService:
    if store is None:
        store = default_store()
    return TokenService(
        TokenPolicy.from_env(),
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
        'event_limit': 409,
        'wallet_duplicate': 409,
        'wallet_limit': 409,
        'already_cancelled': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_archived': 409,
        'already_reversed': 409,
        'already_provisioned': 409,
        'already_deactivated': 409,
        'nsf': 409,
        'failed': 409,
        'reverse_failed': 409,
        'token_forbidden': 403,
        'token_disabled': 403,
        'key_paused': 403,
        'wallet_paused': 403,
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
        'not_suspendable': 403,
        'not_resumable': 403,
        'not_reprovisionable': 403,
        'return_window_closed': 403,
        'key_not_found': 404,
        'event_not_found': 404,
        'wallet_not_found': 404,
        'token_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_pan': 400,
        'invalid_brand': 400,
        'invalid_expiry': 400,
        'invalid_action': 400,
        'invalid_trid': 400,
        'invalid_wallet': 400,
        'invalid_par': 400,
        'invalid_status': 400,
        'invalid_reference': 400,
        'invalid_file': 400,
        'missing_customer_id': 400,
        'missing_key': 400,
        'missing_event': 400,
        'missing_wallet': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: TokenError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('event') is not None:
        body['event'] = exc.extra['event'].to_dict()
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
    except TokenError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snap(service: TokenService, owner: str, userid: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(owner, actor=userid, actor_type=actor_type)


def handle_list(service: TokenService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'CardTokens': _snap(service, owner, userid, actor_type)}), 200


def handle_add_key(service: TokenService):
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
            'CardTokens': _snap(service, owner, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _key_status_route(service: TokenService, status: str, ok_message: str):
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
            'CardTokens': _snap(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: TokenService):
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
            action=values.get('action') or ACTION_P,
            wallet_id=values.get('wallet_id'),
            wallet_kind=values.get('wallet_kind') or values.get('wallet'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({'preview': preview, 'CardTokens': _snap(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_provision(service: TokenService, action: str = ACTION_P):
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
        event, created = service.provision(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            key_id=key_id,
            wallet_id=values.get('wallet_id'),
            wallet_kind=values.get('wallet_kind') or values.get('wallet'),
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
            action=values.get('action') or action,
            event_id=values.get('event_id') or values.get('token_id'),
        )
        labels = {
            ACTION_P: ('Token provisioned', 'Token already posted'),
            ACTION_S: ('Token suspended', 'Suspend already posted'),
            ACTION_R: ('Token resumed', 'Resume already posted'),
            ACTION_D: ('Token deactivated', 'Deactivate already posted'),
            ACTION_X: ('Token reprovisioned', 'Reprovision already posted'),
        }
        ok, again = labels.get(event.action, ('Token updated', 'Token already posted'))
        return jsonify({
            'message': ok if created else again,
            'event': event.to_dict(),
            'CardTokens': _snap(service, owner, userid, actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: TokenService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    event_id = str(values.get('event_id') or values.get('token_id') or '').strip()
    if not event_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_event'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.cancel_event(
            event_id=event_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Token event cancelled',
            'event': row.to_dict(),
            'CardTokens': _snap(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_claim(service: TokenService):
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
            event_id=values.get('event_id') or values.get('token_id'),
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
            'event': row.to_dict(),
            'CardTokens': _snap(service, owner, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_add_wallet(service: TokenService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.add_wallet(
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname'),
            legal_name=values.get('legal_name') or values.get('name'),
            trid=values.get('trid') or values.get('bid'),
            network=values.get('network'),
            wallet_kind=values.get('wallet_kind') or values.get('wallet'),
        )
        owner = str(values.get('customer_id') or userid)
        return jsonify({
            'message': 'Wallet enrolled',
            'wallet': row.to_dict(),
            'CardTokens': _snap(service, owner, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _wallet_status_route(service: TokenService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    wallet_id = str(values.get('wallet_id') or '').strip()
    if not wallet_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_wallet'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_wallet_status(
            wallet_id=wallet_id, actor=userid, actor_type=actor_type, status=status,
        )
        owner = str(values.get('customer_id') or userid)
        return jsonify({
            'message': ok_message,
            'wallet': row.to_dict(),
            'CardTokens': _snap(service, owner, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_ingest(service: TokenService):
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
            trid=values.get('trid'),
            wallet_id=values.get('wallet_id'),
            wallet_name=values.get('wallet_name') or values.get('name'),
            wallet_kind=values.get('wallet_kind') or values.get('wallet'),
            trace_id=values.get('trace_id') or values.get('inquiry_id'),
            waive_fee=bool(values.get('waive_fee')),
        )
        owner = row.userid or str(values.get('customer_id') or userid)
        return jsonify({
            'message': 'Inquiry ingested' if created else 'Inquiry already posted',
            'event': row.to_dict(),
            'CardTokens': _snap(service, owner, userid, actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_file(service: TokenService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    payload = values.get('file') or values.get('payload') or values.get('token1')
    if not payload:
        return jsonify({'message': 'Some data missing', 'error': 'missing_file'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        rows = service.ingest_file(actor=userid, actor_type=actor_type, payload=payload)
        owner = str(values.get('customer_id') or userid)
        return jsonify({
            'message': 'TOKEN1 file ingested',
            'count': len(rows),
            'events': [row.to_dict() for row in rows],
            'CardTokens': _snap(service, owner, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def handle_assign(service: TokenService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    event_id = str(values.get('event_id') or values.get('token_id') or '').strip()
    if not event_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_event'}), 400
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.assign(
            event_id=event_id,
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
            'event': row.to_dict(),
            'CardTokens': _snap(service, owner, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_event_route(service: TokenService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    event_id = str(values.get('event_id') or values.get('token_id') or '').strip()
    if not event_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_event'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'release':
            row = service.release_event(event_id=event_id, actor=userid, actor_type=actor_type)
            message = 'Token released'
        elif action == 'reject':
            row = service.reject_event(
                event_id=event_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Token rejected'
        elif action == 'reverse':
            row = service.reverse_event(
                event_id=event_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Token reversed'
        elif action == 'customer_reverse':
            row = service.reverse_event(
                event_id=event_id, actor=userid, actor_type=actor_type,
                note=values.get('note') or '', customer_request=True,
            )
            message = 'Reverse requested'
        elif action == 'override':
            row = service.override_ofac(
                event_id=event_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'waive':
            row = service.waive_fee(event_id=event_id, actor=userid, actor_type=actor_type)
            message = 'Token fee waived'
        else:
            raise TokenError('invalid_status', 'Unknown token action.')
        return jsonify({
            'message': message,
            'event': row.to_dict(),
            'CardTokens': _snap(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_export(service: TokenService):
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
            'message': 'TOKEN1 exported',
            'file': payload,
            'CardTokens': _snap(service, owner or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_unmatched(service: TokenService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'

    def _run():
        service._require_staff(actor_type)
        rows = service.store.list_unmatched()
        return jsonify({
            'unmatched': [row.to_dict() for row in rows[:40]],
            'CardTokens': _snap(service, userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: TokenService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'CardTokens': _snap(service, owner, userid, actor_type)}), 200


def attach_token_routes(app, service: TokenService) -> None:
    @app.route('/listCardTokens', methods=['POST', 'GET'])
    def list_card_tokens_route():
        return handle_list(service)

    @app.route('/listCardTokenKeys', methods=['POST', 'GET'])
    def list_card_token_keys_route():
        return handle_list(service)

    @app.route('/listUnmatchedCardTokens', methods=['POST', 'GET'])
    def list_unmatched_card_tokens_route():
        return handle_unmatched(service)

    @app.route('/addCardTokenKey', methods=['POST', 'GET'])
    def add_card_token_key_route():
        return handle_add_key(service)

    @app.route('/pauseCardTokenKey', methods=['POST', 'GET'])
    def pause_card_token_key_route():
        return _key_status_route(service, KEY_PAUSED, 'Card paused')

    @app.route('/resumeCardTokenKey', methods=['POST', 'GET'])
    def resume_card_token_key_route():
        return _key_status_route(service, KEY_ACTIVE, 'Card resumed')

    @app.route('/cancelCardTokenKey', methods=['POST', 'GET'])
    def cancel_card_token_key_route():
        return _key_status_route(service, KEY_CANCELLED, 'Card cancelled')

    @app.route('/previewCardToken', methods=['POST', 'GET'])
    def preview_card_token_route():
        return handle_preview(service)

    @app.route('/provisionCardToken', methods=['POST', 'GET'])
    def provision_card_token_route():
        return handle_provision(service, ACTION_P)

    @app.route('/suspendCardToken', methods=['POST', 'GET'])
    def suspend_card_token_route():
        return handle_provision(service, ACTION_S)

    @app.route('/resumeCardToken', methods=['POST', 'GET'])
    def resume_card_token_route():
        return handle_provision(service, ACTION_R)

    @app.route('/deactivateCardToken', methods=['POST', 'GET'])
    def deactivate_card_token_route():
        return handle_provision(service, ACTION_D)

    @app.route('/reprovisionCardToken', methods=['POST', 'GET'])
    def reprovision_card_token_route():
        return handle_provision(service, ACTION_X)

    @app.route('/cancelCardToken', methods=['POST', 'GET'])
    def cancel_card_token_route():
        return handle_cancel(service)

    @app.route('/claimCardToken', methods=['POST', 'GET'])
    def claim_card_token_route():
        return handle_claim(service)

    @app.route('/requestCardTokenReverse', methods=['POST', 'GET'])
    def request_card_token_reverse_route():
        return _staff_event_route(service, 'customer_reverse')

    @app.route('/addCardTokenWallet', methods=['POST', 'GET'])
    def add_card_token_wallet_route():
        return handle_add_wallet(service)

    @app.route('/listCardTokenWallets', methods=['POST', 'GET'])
    def list_card_token_wallets_route():
        return handle_list(service)

    @app.route('/pauseCardTokenWallet', methods=['POST', 'GET'])
    def pause_card_token_wallet_route():
        return _wallet_status_route(service, WALLET_PAUSED, 'Wallet paused')

    @app.route('/resumeCardTokenWallet', methods=['POST', 'GET'])
    def resume_card_token_wallet_route():
        return _wallet_status_route(service, WALLET_ACTIVE, 'Wallet resumed')

    @app.route('/archiveCardTokenWallet', methods=['POST', 'GET'])
    def archive_card_token_wallet_route():
        return _wallet_status_route(service, WALLET_ARCHIVED, 'Wallet archived')

    @app.route('/ingestCardTokenInquiry', methods=['POST', 'GET'])
    def ingest_card_token_inquiry_route():
        return handle_ingest(service)

    @app.route('/ingestCardTokenFile', methods=['POST', 'GET'])
    def ingest_card_token_file_route():
        return handle_ingest_file(service)

    @app.route('/assignCardToken', methods=['POST', 'GET'])
    def assign_card_token_route():
        return handle_assign(service)

    @app.route('/overrideCardTokenOfac', methods=['POST', 'GET'])
    def override_card_token_ofac_route():
        return _staff_event_route(service, 'override')

    @app.route('/releaseCardToken', methods=['POST', 'GET'])
    def release_card_token_route():
        return _staff_event_route(service, 'release')

    @app.route('/rejectCardToken', methods=['POST', 'GET'])
    def reject_card_token_route():
        return _staff_event_route(service, 'reject')

    @app.route('/reverseCardToken', methods=['POST', 'GET'])
    def reverse_card_token_route():
        return _staff_event_route(service, 'reverse')

    @app.route('/waiveCardTokenFee', methods=['POST', 'GET'])
    def waive_card_token_fee_route():
        return _staff_event_route(service, 'waive')

    @app.route('/exportCardToken', methods=['POST', 'GET'])
    def export_card_token_route():
        return handle_export(service)

    @app.route('/runDueCardTokens', methods=['POST', 'GET'])
    def run_due_card_tokens_route():
        return handle_run_due(service)
