"""Card authorization holds (ISO 8583 auth / capture / void / reversal).

Staff ingest incoming card-network authorizations. Recipients are enrolled
PANs (Luhn + IIN brand) at this issuer. Matched auths place a hold on
available balance without debiting the ledger; capture posts the charge;
void/expiry releases the hold; reversal credits a posted capture.
Independent of outbound Fedwire (PR #73), ACH linking (PR #68), inbound UPI
(PR #147), chargeback representment (PR #105), and cheque deposit. Existing
`/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- PAN Luhn check digit + IIN brand
- PAN fingerprint (SHA-256; never in snapshots)
- ISO 8583 MTI (1100 auth, 1200 financial/capture, 1400 reversal)
- STAN / RRN uniqueness
- MCC-driven hold expiry (retail 7d, fuel 2d, lodging/auto/cruise 30d)
- MCC capture window (retail <= auth, fuel <= $200, lodging/auto +15%)
- AUTH1 pipe compose/parse (XML/DOCTYPE rejected)
- Dual-control at $10k, OFAC on merchant name
- 24/7 card-network clock (holds expire; they do not queue on Fed holidays)

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Full PAN, fingerprint, STAN, and RRN never appear in to_dict / snapshots.
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
    normalize_id,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

KEY_ACTIVE = 'active'
KEY_PAUSED = 'paused'
KEY_CANCELLED = 'cancelled'
KEY_STATUSES = frozenset({KEY_ACTIVE, KEY_PAUSED, KEY_CANCELLED})
OPEN_KEYS = frozenset({KEY_ACTIVE, KEY_PAUSED})

HOLD_UNMATCHED = 'unmatched'
HOLD_HELD = 'held'
HOLD_PENDING = 'pending_release'
HOLD_AUTHORIZED = 'authorized'
HOLD_CAPTURED = 'captured'
HOLD_VOIDED = 'voided'
HOLD_EXPIRED = 'expired'
HOLD_REVERSED = 'reversed'
HOLD_REJECTED = 'rejected'
HOLD_NSF = 'nsf'
HOLD_FAILED = 'failed'
HOLD_STATUSES = frozenset({
    HOLD_UNMATCHED, HOLD_HELD, HOLD_PENDING, HOLD_AUTHORIZED, HOLD_CAPTURED,
    HOLD_VOIDED, HOLD_EXPIRED, HOLD_REVERSED, HOLD_REJECTED, HOLD_NSF, HOLD_FAILED,
})
OPEN_HOLDS = frozenset({HOLD_UNMATCHED, HOLD_HELD, HOLD_PENDING, HOLD_AUTHORIZED, HOLD_NSF})
VOIDABLE = frozenset({HOLD_UNMATCHED, HOLD_HELD, HOLD_PENDING, HOLD_AUTHORIZED})
RELEASABLE = frozenset({HOLD_PENDING, HOLD_HELD})

MTI_AUTH = '1100'
MTI_FINANCIAL = '1200'
MTI_ADVICE = '1220'
MTI_REVERSAL = '1400'
MTI_REVERSAL_ADVICE = '1420'
MTI_ALIASES = {
    '0100': MTI_AUTH, '1100': MTI_AUTH, '1110': MTI_AUTH,
    '0200': MTI_FINANCIAL, '1200': MTI_FINANCIAL, '0210': MTI_FINANCIAL,
    '0220': MTI_ADVICE, '1220': MTI_ADVICE,
    '0400': MTI_REVERSAL, '1400': MTI_REVERSAL, '0410': MTI_REVERSAL,
    '0420': MTI_REVERSAL_ADVICE, '1420': MTI_REVERSAL_ADVICE,
}
AUTH_MTIS = frozenset({MTI_AUTH})
CAPTURE_MTIS = frozenset({MTI_FINANCIAL, MTI_ADVICE})
REVERSAL_MTIS = frozenset({MTI_REVERSAL, MTI_REVERSAL_ADVICE})
VALID_MTIS = AUTH_MTIS | CAPTURE_MTIS | REVERSAL_MTIS

BRANDS = frozenset({'visa', 'mastercard', 'amex', 'discover'})
BRAND_ALIASES = {
    'visa': 'visa', 'vi': 'visa',
    'mastercard': 'mastercard', 'mc': 'mastercard', 'master': 'mastercard',
    'amex': 'amex', 'americanexpress': 'amex', 'american_express': 'amex',
    'discover': 'discover', 'disc': 'discover',
}

MCC_FUEL = frozenset({'5541', '5542'})
MCC_LODGING = frozenset({'7011'})
MCC_AUTO = frozenset({'7512', '7513'})
MCC_CRUISE = frozenset({'4411'})
MCC_EXTENDED = MCC_LODGING | MCC_AUTO | MCC_CRUISE

PURPOSE_ALIASES = {
    'retail': 'retail', 'purchase': 'retail', 'pos': 'retail',
    'fuel': 'fuel', 'gas': 'fuel', 'petrol': 'fuel',
    'hotel': 'lodging', 'lodging': 'lodging',
    'auto': 'auto', 'rental': 'auto', 'car': 'auto',
    'cruise': 'cruise', 'other': 'other',
}
PURPOSES = frozenset(PURPOSE_ALIASES.values())

DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/cardhold.sqlite'
FUEL_CAPTURE_CAP = Decimal('200.00')
LODGING_OVERCAPTURE = Decimal('1.15')
CUSTOMER_VOID_HOURS = 24
STAFF_REVERSE_DAYS = 90


class CardHoldError(ValueError):
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
        raise CardHoldError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_merchant(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise CardHoldError('invalid_name', 'Merchant name must be 2-80 characters.')
    return text


def normalize_currency(value: Any, *, default: str = 'USD') -> str:
    text = ''.join(ch for ch in str(value or default).upper() if ch.isalpha())
    if text != 'USD':
        raise CardHoldError('invalid_currency', 'Card holds settle in USD.')
    return text


def normalize_purpose(value: Any, *, default: str = 'retail') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise CardHoldError('invalid_purpose', 'Unknown card-hold purpose.')
    return text


def normalize_mcc(value: Any, *, default: str = '5999') -> str:
    digits = ''.join(ch for ch in str(value if value not in (None, '') else default) if ch.isdigit())
    if len(digits) == 3:
        digits = digits.zfill(4)
    if len(digits) != 4:
        raise CardHoldError('invalid_mcc', 'MCC must be four digits.')
    return digits


def purpose_from_mcc(mcc: str) -> str:
    if mcc in MCC_FUEL:
        return 'fuel'
    if mcc in MCC_LODGING:
        return 'lodging'
    if mcc in MCC_AUTO:
        return 'auto'
    if mcc in MCC_CRUISE:
        return 'cruise'
    return 'retail'


def expiry_days_for_mcc(mcc: str) -> int:
    if mcc in MCC_FUEL:
        return 2
    if mcc in MCC_EXTENDED:
        return 30
    return 7


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
        raise CardHoldError('invalid_pan', 'PAN is required.')
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
    raise CardHoldError('invalid_pan', 'Unsupported card brand.')


def normalize_brand(value: Any) -> str:
    text = ''.join(ch for ch in str(value or '').lower() if ch.isalnum() or ch == '_')
    brand = BRAND_ALIASES.get(text)
    if brand is None:
        raise CardHoldError('invalid_brand', 'Brand must be visa, mastercard, amex, or discover.')
    return brand


def normalize_pan(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not (13 <= len(digits) <= 19):
        raise CardHoldError('invalid_pan', 'PAN must be 13-19 digits.')
    if digits == '0' * len(digits) or not luhn_check_digit_ok(digits):
        raise CardHoldError('invalid_pan', 'PAN failed Luhn checksum.')
    brand = brand_from_iin(digits)
    if brand == 'amex' and len(digits) != 15:
        raise CardHoldError('invalid_pan', 'American Express PANs must be 15 digits.')
    if brand == 'mastercard' and len(digits) != 16:
        raise CardHoldError('invalid_pan', 'Mastercard PANs must be 16 digits.')
    if brand == 'discover' and len(digits) not in {16, 19}:
        raise CardHoldError('invalid_pan', 'Discover PANs must be 16 or 19 digits.')
    return digits


def mask_pan(pan: str) -> str:
    digits = ''.join(ch for ch in str(pan or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return ('*' * max(0, len(digits) - 4)) + digits[-4:]


def pan_fingerprint(pan: str) -> str:
    digits = ''.join(ch for ch in str(pan or '') if ch.isdigit())
    return hashlib.sha256(digits.encode('ascii')).hexdigest()


def normalize_mti(value: Any, *, default: str = MTI_AUTH) -> str:
    text = ''.join(ch for ch in str(value if value not in (None, '') else default) if ch.isdigit())
    if len(text) == 3:
        text = text.zfill(4)
    mapped = MTI_ALIASES.get(text)
    if mapped is None:
        raise CardHoldError('invalid_mti', 'Unknown ISO 8583 MTI.')
    return mapped


def mti_kind(mti: str) -> str:
    if mti in AUTH_MTIS:
        return 'auth'
    if mti in CAPTURE_MTIS:
        return 'capture'
    if mti in REVERSAL_MTIS:
        return 'reversal'
    raise CardHoldError('invalid_mti', 'Unknown ISO 8583 MTI.')


def compose_stan(sequence: int) -> str:
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise CardHoldError('invalid_stan', 'STAN sequence out of range.')
    return '%06d' % seq


def normalize_stan(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        raise CardHoldError('invalid_stan', 'STAN is required.')
    if len(digits) > 6:
        raise CardHoldError('invalid_stan', 'STAN must be six digits.')
    digits = digits.zfill(6)
    if digits == '000000':
        raise CardHoldError('invalid_stan', 'STAN cannot be zero.')
    return digits


def mask_stan(stan: str) -> str:
    digits = ''.join(ch for ch in str(stan or '') if ch.isdigit()).zfill(6)
    return '***' + digits[-3:]


def compose_rrn(cycle_date: str, sequence: int) -> str:
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise CardHoldError('invalid_rrn', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 9999:
        raise CardHoldError('invalid_rrn', 'RRN sequence out of range.')
    return '%s%04d' % (day, seq)


def normalize_rrn(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 12:
        raise CardHoldError('invalid_rrn', 'RRN must be twelve digits.')
    if digits == '0' * 12:
        raise CardHoldError('invalid_rrn', 'RRN cannot be zero.')
    return digits


def mask_rrn(rrn: str) -> str:
    digits = ''.join(ch for ch in str(rrn or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return ('*' * (len(digits) - 4)) + digits[-4:]


def compose_approval(sequence: int, *, prefix: str = 'A') -> str:
    seq = int(sequence)
    if seq < 1 or seq > 99999:
        raise CardHoldError('invalid_reference', 'Approval sequence out of range.')
    return '%s%05d' % (prefix[:1].upper() or 'A', seq)


def normalize_approval(value: Any) -> str:
    text = ''.join(ch for ch in str(value or '').upper() if ch.isalnum())
    if len(text) != 6:
        raise CardHoldError('invalid_reference', 'Approval code must be six characters.')
    return text


def capture_limit(mcc: str, authorized: Decimal) -> Decimal:
    if mcc in MCC_FUEL:
        return max(authorized, FUEL_CAPTURE_CAP)
    if mcc in MCC_LODGING | MCC_AUTO:
        return (authorized * LODGING_OVERCAPTURE).quantize(MONEY_QUANTUM)
    return authorized


def compose_auth1(
    *,
    stan: str,
    rrn: str,
    mti: str,
    pan_masked: str,
    amount: str,
    merchant: str,
    mcc: str,
    currency: str = 'USD',
) -> str:
    if '|' in merchant or '\n' in merchant:
        raise CardHoldError('invalid_file', 'Merchant cannot contain pipe or newline.')
    return 'AUTH1|%s|%s|%s|%s|%s|%s|%s|%s' % (
        mask_stan(stan), mask_rrn(rrn), mti, pan_masked, amount, merchant, mcc, currency,
    )


def parse_auth1(line: Any) -> Dict[str, str]:
    text = str(line or '').strip()
    if not text:
        raise CardHoldError('invalid_file', 'Empty AUTH1 line.')
    upper = text.upper()
    if '<?XML' in upper or '<!DOCTYPE' in upper or '<' in text:
        raise CardHoldError('invalid_file', 'XML/DOCTYPE is rejected.')
    parts = text.split('|')
    if len(parts) < 8 or parts[0].upper() != 'AUTH1':
        raise CardHoldError('invalid_file', 'AUTH1 line must be AUTH1|STAN|RRN|MTI|PAN|AMOUNT|MERCHANT|MCC|CURRENCY.')
    while len(parts) < 9:
        parts.append('USD')
    return {
        'stan': parts[1],
        'rrn': parts[2],
        'mti': parts[3],
        'pan': parts[4],
        'amount': parts[5],
        'merchant': parts[6],
        'mcc': parts[7],
        'currency': parts[8] or 'USD',
    }


def split_auth1_file(payload: Any) -> List[str]:
    text = str(payload or '')
    if '<?xml' in text.lower() or '<!doctype' in text.lower():
        raise CardHoldError('invalid_file', 'XML/DOCTYPE is rejected.')
    lines = [line.strip() for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    return [line for line in lines if line]


class HoldClock:
    """24/7 card-network clock. Holds expire by MCC; they do not queue on Fed holidays."""

    def __init__(self, *, tz_offset_hours: int = -4) -> None:
        self.tz_offset_hours = int(tz_offset_hours)
        self.tz = timezone(timedelta(hours=self.tz_offset_hours))

    def local_dt(self, ts: float) -> datetime:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(self.tz)

    def cycle_date(self, ts: float) -> str:
        return self.local_dt(ts).strftime('%Y%m%d')

    def expires_at(self, ts: float, mcc: str) -> float:
        days = expiry_days_for_mcc(mcc)
        return float(ts) + (days * 86400)

    def snapshot(self, ts: float) -> Dict[str, Any]:
        local = self.local_dt(ts)
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'network_open': True,
            'cycle_date': local.strftime('%Y%m%d'),
            'retail_days': 7,
            'fuel_days': 2,
            'extended_days': 30,
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
class CardHoldPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_void: bool = True
    allow_credit: bool = False
    max_keys: int = 8
    max_holds: int = 240
    min_amount: Decimal = Decimal('0.50')
    max_amount: Decimal = Decimal('25000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    tz_offset_hours: int = -4
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST

    @classmethod
    def from_env(cls) -> 'CardHoldPolicy':
        extra = _env_list('CARDHOLD_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('CARDHOLD_ENABLED', True),
            customer_manage=_env_bool('CARDHOLD_CUSTOMER_MANAGE', True),
            customer_void=_env_bool('CARDHOLD_CUSTOMER_VOID', True),
            allow_credit=_env_bool('CARDHOLD_ALLOW_CREDIT', False),
            max_keys=max(1, _env_int('CARDHOLD_MAX_KEYS', 8)),
            max_holds=max(1, _env_int('CARDHOLD_MAX_HOLDS', 240)),
            min_amount=_env_money('CARDHOLD_MIN_AMOUNT', '0.50'),
            max_amount=_env_money('CARDHOLD_MAX_AMOUNT', '25000.00'),
            dual_control_threshold=_env_money('CARDHOLD_DUAL_CONTROL', '10000.00'),
            tz_offset_hours=_env_int('CARDHOLD_TZ_OFFSET', -4),
            watchlist=watch,
        )


@dataclass
class CardHoldKey:
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
class CardHold:
    hold_id: str
    stan: str
    rrn: str
    mti: str
    key_id: str
    userid: str
    internal_account: str
    amount: str
    captured_amount: str
    nickname: str
    merchant: str
    mcc: str
    purpose: str
    brand: str
    last4: str
    status: str
    approval_code: str
    expires_at: float
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    created_at: float
    updated_at: float
    authorized_at: float = 0.0
    captured_at: float = 0.0
    voided_at: float = 0.0
    reversed_at: float = 0.0
    note: str = ''
    reason: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'hold_id': self.hold_id,
            'stan_masked': mask_stan(self.stan),
            'rrn_masked': mask_rrn(self.rrn),
            'mti': self.mti,
            'key_id': self.key_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'amount': self.amount,
            'captured_amount': self.captured_amount,
            'nickname': self.nickname,
            'merchant': self.merchant,
            'mcc': self.mcc,
            'purpose': self.purpose,
            'brand': self.brand,
            'pan_masked': ('*' * 12) + self.last4 if self.last4 else '****',
            'last4': self.last4,
            'status': self.status,
            'approval_code': self.approval_code,
            'expires_at': self.expires_at,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'authorized_at': self.authorized_at,
            'captured_at': self.captured_at,
            'voided_at': self.voided_at,
            'reversed_at': self.reversed_at,
            'note': self.note,
            'reason': self.reason,
            'authorized': self.status == HOLD_AUTHORIZED,
            'captured': self.status == HOLD_CAPTURED,
            'pending_release': self.status == HOLD_PENDING,
            'voidable': self.status in VOIDABLE,
            'open': self.status in OPEN_HOLDS,
        }


def _clone_key(row: CardHoldKey) -> CardHoldKey:
    return CardHoldKey(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_hold(row: CardHold) -> CardHold:
    return CardHold(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _key_from_row(row: Any) -> CardHoldKey:
    return CardHoldKey(
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


def _hold_from_row(row: Any) -> CardHold:
    return CardHold(
        hold_id=row['hold_id'],
        stan=row['stan'],
        rrn=row['rrn'],
        mti=row['mti'],
        key_id=row['key_id'] or '',
        userid=row['userid'] or '',
        internal_account=row['internal_account'] or '',
        amount=row['amount'],
        captured_amount=row['captured_amount'] or '0.00',
        nickname=row['nickname'] or '',
        merchant=row['merchant'],
        mcc=row['mcc'],
        purpose=row['purpose'],
        brand=row['brand'] or '',
        last4=row['last4'] or '',
        status=row['status'],
        approval_code=row['approval_code'] or '',
        expires_at=float(row['expires_at'] or 0),
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        authorized_at=float(row['authorized_at'] or 0),
        captured_at=float(row['captured_at'] or 0),
        voided_at=float(row['voided_at'] or 0),
        reversed_at=float(row['reversed_at'] or 0),
        note=row['note'] or '',
        reason=row['reason'] or '',
    )


class MemoryCardHoldStore:
    def __init__(self) -> None:
        self._keys: Dict[str, CardHoldKey] = {}
        self._holds: Dict[str, CardHold] = {}
        self._by_stan: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_key(self, row: CardHoldKey) -> None:
        with self._lock:
            self._keys[row.key_id] = row

    def get_key(self, key_id: str) -> Optional[CardHoldKey]:
        with self._lock:
            row = self._keys.get(key_id)
            return _clone_key(row) if row else None

    def update_key(self, row: CardHoldKey) -> None:
        with self._lock:
            self._keys[row.key_id] = row

    def list_keys(self, userid: Optional[str] = None, *, include_cancelled: bool = True) -> List[CardHoldKey]:
        with self._lock:
            rows = [_clone_key(row) for row in self._keys.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_cancelled:
            rows = [row for row in rows if row.status != KEY_CANCELLED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_key_by_nickname(self, userid: str, nickname: str) -> Optional[CardHoldKey]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._keys.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_KEYS:
                    return _clone_key(row)
        return None

    def find_key_by_fingerprint(self, fingerprint: str, *, userid: Optional[str] = None) -> Optional[CardHoldKey]:
        with self._lock:
            rows = [
                row for row in self._keys.values()
                if row.fingerprint == fingerprint and row.status == KEY_ACTIVE
            ]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        rows.sort(key=lambda item: item.created_at)
        return _clone_key(rows[0]) if rows else None

    def put_hold(self, row: CardHold) -> CardHold:
        with self._lock:
            existing_id = self._by_stan.get(row.stan)
            if existing_id is not None:
                return self._holds[existing_id]
            self._holds[row.hold_id] = row
            self._by_stan[row.stan] = row.hold_id
            return row

    def update_hold(self, row: CardHold) -> None:
        with self._lock:
            self._holds[row.hold_id] = row

    def get_hold(self, hold_id: str) -> Optional[CardHold]:
        with self._lock:
            row = self._holds.get(hold_id)
            return _clone_hold(row) if row else None

    def get_hold_by_stan(self, stan: str) -> Optional[CardHold]:
        with self._lock:
            hold_id = self._by_stan.get(stan)
            return _clone_hold(self._holds[hold_id]) if hold_id else None

    def get_hold_by_rrn(self, rrn: str) -> Optional[CardHold]:
        with self._lock:
            rows = [row for row in self._holds.values() if row.rrn == rrn]
        rows.sort(key=lambda item: item.created_at)
        return _clone_hold(rows[0]) if rows else None

    def list_holds(self, userid: Optional[str] = None, *, unmatched_only: bool = False) -> List[CardHold]:
        with self._lock:
            rows = [_clone_hold(row) for row in self._holds.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if unmatched_only:
            rows = [row for row in rows if row.status == HOLD_UNMATCHED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def list_unmatched(self) -> List[CardHold]:
        return self.list_holds(unmatched_only=True)

    def next_stan_sequence(self) -> int:
        with self._lock:
            return len(self._holds) + 1

    def next_rrn_sequence(self, cycle_date: str) -> int:
        with self._lock:
            used = [row.rrn for row in self._holds.values() if row.rrn.startswith(cycle_date)]
        return len(used) + 1


class SqliteCardHoldStore:
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
                CREATE TABLE IF NOT EXISTS holds (
                    hold_id TEXT PRIMARY KEY,
                    stan TEXT NOT NULL UNIQUE,
                    rrn TEXT NOT NULL,
                    mti TEXT NOT NULL,
                    key_id TEXT NOT NULL DEFAULT '',
                    userid TEXT NOT NULL DEFAULT '',
                    internal_account TEXT NOT NULL DEFAULT '',
                    amount TEXT NOT NULL,
                    captured_amount TEXT NOT NULL DEFAULT '0.00',
                    nickname TEXT NOT NULL DEFAULT '',
                    merchant TEXT NOT NULL,
                    mcc TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    brand TEXT NOT NULL DEFAULT '',
                    last4 TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    approval_code TEXT NOT NULL DEFAULT '',
                    expires_at REAL NOT NULL DEFAULT 0,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    authorized_at REAL NOT NULL DEFAULT 0,
                    captured_at REAL NOT NULL DEFAULT 0,
                    voided_at REAL NOT NULL DEFAULT 0,
                    reversed_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_key(self, row: CardHoldKey) -> None:
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

    def get_key(self, key_id: str) -> Optional[CardHoldKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM keys WHERE key_id = ?', (key_id,)).fetchone()
        return _key_from_row(row) if row else None

    def update_key(self, row: CardHoldKey) -> None:
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

    def list_keys(self, userid: Optional[str] = None, *, include_cancelled: bool = True) -> List[CardHoldKey]:
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

    def find_key_by_nickname(self, userid: str, nickname: str) -> Optional[CardHoldKey]:
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

    def find_key_by_fingerprint(self, fingerprint: str, *, userid: Optional[str] = None) -> Optional[CardHoldKey]:
        sql = "SELECT * FROM keys WHERE fingerprint = ? AND status = 'active'"
        params: List[Any] = [fingerprint]
        if userid is not None:
            sql += ' AND userid = ?'
            params.append(userid)
        sql += ' ORDER BY created_at ASC'
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return _key_from_row(row) if row else None

    def put_hold(self, row: CardHold) -> CardHold:
        with self._lock, self._connect() as conn:
            existing = conn.execute('SELECT * FROM holds WHERE stan = ?', (row.stan,)).fetchone()
            if existing is not None:
                return _hold_from_row(existing)
            conn.execute(
                """
                INSERT INTO holds (
                    hold_id, stan, rrn, mti, key_id, userid, internal_account, amount,
                    captured_amount, nickname, merchant, mcc, purpose, brand, last4,
                    status, approval_code, expires_at, actor, releaser, ofac_hit, ofac_match,
                    created_at, updated_at, authorized_at, captured_at, voided_at, reversed_at,
                    note, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.hold_id, row.stan, row.rrn, row.mti, row.key_id, row.userid,
                    row.internal_account, row.amount, row.captured_amount, row.nickname,
                    row.merchant, row.mcc, row.purpose, row.brand, row.last4, row.status,
                    row.approval_code, row.expires_at, row.actor, row.releaser, row.ofac_hit,
                    row.ofac_match, row.created_at, row.updated_at, row.authorized_at,
                    row.captured_at, row.voided_at, row.reversed_at, row.note, row.reason,
                ),
            )
            conn.commit()
            return row

    def update_hold(self, row: CardHold) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE holds SET key_id=?, userid=?, internal_account=?, amount=?,
                    captured_amount=?, nickname=?, merchant=?, mcc=?, purpose=?, brand=?,
                    last4=?, status=?, approval_code=?, expires_at=?, actor=?, releaser=?,
                    ofac_hit=?, ofac_match=?, updated_at=?, authorized_at=?, captured_at=?,
                    voided_at=?, reversed_at=?, note=?, reason=?, mti=?
                WHERE hold_id=?
                """,
                (
                    row.key_id, row.userid, row.internal_account, row.amount, row.captured_amount,
                    row.nickname, row.merchant, row.mcc, row.purpose, row.brand, row.last4,
                    row.status, row.approval_code, row.expires_at, row.actor, row.releaser,
                    row.ofac_hit, row.ofac_match, row.updated_at, row.authorized_at,
                    row.captured_at, row.voided_at, row.reversed_at, row.note, row.reason,
                    row.mti, row.hold_id,
                ),
            )
            conn.commit()

    def get_hold(self, hold_id: str) -> Optional[CardHold]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM holds WHERE hold_id = ?', (hold_id,)).fetchone()
        return _hold_from_row(row) if row else None

    def get_hold_by_stan(self, stan: str) -> Optional[CardHold]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM holds WHERE stan = ?', (stan,)).fetchone()
        return _hold_from_row(row) if row else None

    def get_hold_by_rrn(self, rrn: str) -> Optional[CardHold]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM holds WHERE rrn = ? ORDER BY created_at ASC', (rrn,),
            ).fetchone()
        return _hold_from_row(row) if row else None

    def list_holds(self, userid: Optional[str] = None, *, unmatched_only: bool = False) -> List[CardHold]:
        sql = 'SELECT * FROM holds'
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
        return [_hold_from_row(row) for row in rows]

    def list_unmatched(self) -> List[CardHold]:
        return self.list_holds(unmatched_only=True)

    def next_stan_sequence(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT COUNT(*) AS n FROM holds').fetchone()
        return int(row['n'] if row else 0) + 1

    def next_rrn_sequence(self, cycle_date: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT COUNT(*) AS n FROM holds WHERE rrn LIKE ?', (cycle_date + '%',),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class CardHoldService:
    def __init__(
        self,
        policy: CardHoldPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[HoldClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or HoldClock(tz_offset_hours=policy.tz_offset_hours)

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise CardHoldError('cardhold_disabled', 'Card authorization holds are disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise CardHoldError('cardhold_forbidden', 'Customers cannot manage card keys.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise CardHoldError('cardhold_forbidden', 'Staff only.')

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
            raise CardHoldError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise CardHoldError('credit_not_allowed', 'Credit accounts cannot receive card holds.')

    def _assert_amount(self, dollars: Decimal) -> None:
        if dollars < self.policy.min_amount or dollars > self.policy.max_amount:
            raise CardHoldError('amount_out_of_range', 'Amount is outside the allowed range.')

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
    ) -> CardHoldKey:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise CardHoldError('cardhold_forbidden', 'Not allowed to add keys for this customer.')
        name = normalize_nickname(nickname)
        digits = normalize_pan(pan)
        derived = brand_from_iin(digits)
        if brand not in (None, ''):
            wanted = normalize_brand(brand)
            if wanted != derived:
                raise CardHoldError('invalid_brand', 'Brand does not match PAN IIN.')
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        fingerprint = pan_fingerprint(digits)
        if self.store.find_key_by_nickname(owner_userid, name) is not None:
            raise CardHoldError('key_duplicate', 'A card key with that nickname already exists.')
        if self.store.find_key_by_fingerprint(fingerprint, userid=owner_userid) is not None:
            raise CardHoldError('key_duplicate', 'That card is already enrolled.')
        open_rows = [row for row in self.store.list_keys(owner_userid) if row.status in OPEN_KEYS]
        if len(open_rows) >= self.policy.max_keys:
            raise CardHoldError('key_limit', 'Card key limit reached.')
        now = float(self.clock())
        row = CardHoldKey(
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

    def get_key(self, *, key_id: str, actor: str, actor_type: str) -> CardHoldKey:
        self._require_enabled()
        row = self.store.get_key(key_id)
        if row is None:
            raise CardHoldError('key_not_found', 'Card key not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise CardHoldError('cardhold_forbidden', 'Not allowed to view this card key.')
        return row

    def set_key_status(
        self,
        *,
        key_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> CardHoldKey:
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
            raise CardHoldError('invalid_status', 'Status must be pause, resume, or cancel.')
        if row.status == KEY_CANCELLED:
            raise CardHoldError('already_cancelled', 'Card key is already cancelled.')
        if wanted == KEY_PAUSED:
            if row.status == KEY_PAUSED:
                raise CardHoldError('already_paused', 'Card key is already paused.')
            if row.status != KEY_ACTIVE:
                raise CardHoldError('invalid_status', 'Only an active card key can be paused.')
        elif wanted == KEY_ACTIVE:
            if row.status == KEY_ACTIVE:
                raise CardHoldError('already_active', 'Card key is already active.')
            if row.status != KEY_PAUSED:
                raise CardHoldError('invalid_status', 'Only a paused card key can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_key(row)
        if wanted == KEY_ACTIVE:
            self._rematch_unmatched(row)
        return row

    def _rematch_unmatched(self, key: CardHoldKey) -> None:
        if key.status != KEY_ACTIVE:
            return
        for hold in self.store.list_unmatched():
            if hold.last4 == key.last4 and (not hold.brand or hold.brand == key.brand):
                hold.key_id = key.key_id
                hold.userid = key.userid
                hold.internal_account = hold.internal_account or key.default_account
                hold.nickname = key.nickname
                hold.brand = key.brand
                hold.last4 = key.last4
                self._evaluate(hold, actor=key.actor)

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
    ) -> Dict[str, Any]:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise CardHoldError('cardhold_forbidden', 'Not allowed to preview holds for this customer.')
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
            'merchant': merchant_name,
            'mcc': mcc_code,
            'purpose': purpose_from_mcc(mcc_code),
            'expiry_days': expiry_days_for_mcc(mcc_code),
            'capture_limit': money_str(capture_limit(mcc_code, dollars)),
            'internal_account': account,
            'key': key.to_dict() if key is not None else None,
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(dollars),
            'clock': self.calendar.snapshot(now),
        }

    def _new_ids(self, *, stan: Any, rrn: Any, now: float) -> Tuple[str, str]:
        if stan in (None, ''):
            stan_value = compose_stan(self.store.next_stan_sequence())
            while self.store.get_hold_by_stan(stan_value) is not None:
                stan_value = compose_stan(int(stan_value) + 1)
        else:
            stan_value = normalize_stan(stan)
        if rrn in (None, ''):
            cycle = self.calendar.cycle_date(now)
            rrn_value = compose_rrn(cycle, self.store.next_rrn_sequence(cycle))
            while self.store.get_hold_by_rrn(rrn_value) is not None:
                rrn_value = compose_rrn(cycle, int(rrn_value[-4:]) + 1)
        else:
            rrn_value = normalize_rrn(rrn)
        return stan_value, rrn_value

    def _authorize_hold(self, hold: CardHold, *, actor: str) -> CardHold:
        now = float(self.clock())
        if not hold.approval_code:
            hold.approval_code = compose_approval(max(1, self.store.next_stan_sequence()))
        hold.status = HOLD_AUTHORIZED
        hold.authorized_at = now
        hold.updated_at = now
        hold.actor = str(actor)
        self.store.update_hold(hold)
        return hold

    def _evaluate(self, hold: CardHold, *, actor: str) -> CardHold:
        now = float(self.clock())
        if not hold.key_id or not hold.userid:
            hold.status = HOLD_UNMATCHED
            hold.updated_at = now
            self.store.update_hold(hold)
            return hold
        key = self.store.get_key(hold.key_id)
        if key is None or key.status != KEY_ACTIVE:
            hold.status = HOLD_UNMATCHED
            hold.updated_at = now
            self.store.update_hold(hold)
            return hold
        if hold.ofac_hit and hold.status != HOLD_AUTHORIZED:
            hold.status = HOLD_HELD
            hold.updated_at = now
            self.store.update_hold(hold)
            return hold
        dollars = parse_money(hold.amount)
        if self._needs_dual_control(dollars) and not hold.releaser:
            hold.status = HOLD_PENDING
            hold.updated_at = now
            self.store.update_hold(hold)
            return hold
        return self._authorize_hold(hold, actor=actor)

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        amount: Any,
        merchant: Any,
        pan: Any = None,
        mti: Any = MTI_AUTH,
        mcc: Any = None,
        stan: Any = None,
        rrn: Any = None,
        currency: Any = 'USD',
        owner_userid: Any = None,
        key_id: Any = None,
        internal_account: Any = None,
    ) -> Tuple[CardHold, bool]:
        self._require_staff(actor_type)
        normalize_currency(currency)
        mti_value = normalize_mti(mti)
        kind = mti_kind(mti_value)
        if stan not in (None, ''):
            existing = self.store.get_hold_by_stan(normalize_stan(stan))
            if existing is not None:
                return existing, False
        if rrn not in (None, '') and kind != 'auth':
            linked = self.store.get_hold_by_rrn(normalize_rrn(rrn))
            if linked is not None:
                if kind == 'capture':
                    return self.capture(hold_id=linked.hold_id, actor=actor, actor_type=actor_type, amount=amount), False
                return self._void_or_reverse(linked, actor=actor, actor_type=actor_type), False
        if rrn not in (None, '') and kind == 'auth':
            linked = self.store.get_hold_by_rrn(normalize_rrn(rrn))
            if linked is not None and linked.status == HOLD_AUTHORIZED:
                return self.increment(
                    hold_id=linked.hold_id, actor=actor, actor_type=actor_type, amount=amount,
                ), False
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
        open_holds = self.store.list_holds()
        if len(open_holds) >= self.policy.max_holds:
            raise CardHoldError('hold_limit', 'Card hold limit reached.')
        stan_value, rrn_value = self._new_ids(stan=stan, rrn=rrn, now=now)
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
        hold = CardHold(
            hold_id=uuid.uuid4().hex,
            stan=stan_value,
            rrn=rrn_value,
            mti=mti_value,
            key_id=resolved_key_id,
            userid=userid,
            internal_account=account,
            amount=money_str(dollars),
            captured_amount='0.00',
            nickname=nickname,
            merchant=merchant_name,
            mcc=mcc_code,
            purpose=purpose,
            brand=brand,
            last4=pan_last4,
            status=HOLD_UNMATCHED,
            approval_code='',
            expires_at=self.calendar.expires_at(now, mcc_code),
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
        )
        stored = self.store.put_hold(hold)
        if stored.hold_id != hold.hold_id:
            return stored, False
        evaluated = self._evaluate(hold, actor=actor)
        if kind == 'capture' and evaluated.status == HOLD_AUTHORIZED:
            return self.capture(hold_id=evaluated.hold_id, actor=actor, actor_type=actor_type, amount=amount), True
        if kind == 'reversal' and evaluated.status in VOIDABLE:
            return self.void_hold(hold_id=evaluated.hold_id, actor=actor, actor_type=actor_type), True
        return evaluated, True

    def ingest_file(self, *, actor: str, actor_type: str, payload: Any) -> List[CardHold]:
        self._require_staff(actor_type)
        rows = []
        for line in split_auth1_file(payload):
            parsed = parse_auth1(line)
            hold, _created = self.ingest(
                actor=actor,
                actor_type=actor_type,
                amount=parsed['amount'],
                merchant=parsed['merchant'],
                pan=parsed['pan'] if not str(parsed['pan']).startswith('*') else None,
                mti=parsed['mti'],
                mcc=parsed['mcc'],
                stan=parsed['stan'] if not str(parsed['stan']).startswith('*') else None,
                rrn=parsed['rrn'] if not str(parsed['rrn']).startswith('*') else None,
                currency=parsed['currency'],
            )
            rows.append(hold)
        return rows

    def get_hold(self, *, hold_id: str, actor: str, actor_type: str) -> CardHold:
        self._require_enabled()
        row = self.store.get_hold(hold_id)
        if row is None:
            raise CardHoldError('hold_not_found', 'Card hold not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise CardHoldError('cardhold_forbidden', 'Not allowed to view this hold.')
        return row

    def claim(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        hold_id: str,
        pan: Any = None,
        last4_value: Any = None,
        brand: Any = None,
        default_account: Any = None,
        nickname: Any = None,
    ) -> CardHold:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise CardHoldError('cardhold_forbidden', 'Not allowed to claim holds for this customer.')
        hold = self.store.get_hold(hold_id)
        if hold is None:
            raise CardHoldError('hold_not_found', 'Card hold not found.')
        if hold.status != HOLD_UNMATCHED:
            raise CardHoldError('not_claimable', 'Only unmatched holds can be claimed.')
        presented_last4 = last4(last4_value or '')
        presented_brand = normalize_brand(brand) if brand not in (None, '') else hold.brand
        fingerprint = ''
        if pan not in (None, ''):
            digits = normalize_pan(pan)
            fingerprint = pan_fingerprint(digits)
            presented_last4 = last4(digits)
            presented_brand = brand_from_iin(digits)
        if not presented_last4:
            raise CardHoldError('invalid_pan', 'Last4 or PAN is required to claim.')
        if hold.last4 and hold.last4 != presented_last4:
            raise CardHoldError('not_claimable', 'Claimed last4 does not match this hold.')
        if hold.brand and presented_brand and hold.brand != presented_brand:
            raise CardHoldError('not_claimable', 'Claimed brand does not match this hold.')
        key = None
        if fingerprint:
            key = self.store.find_key_by_fingerprint(fingerprint, userid=owner_userid)
        if key is None and presented_last4:
            for row in self.store.list_keys(owner_userid, include_cancelled=False):
                if row.status == KEY_ACTIVE and row.last4 == presented_last4:
                    if not presented_brand or row.brand == presented_brand:
                        key = row
                        break
        if key is None:
            account = normalize_account(default_account or hold.internal_account or '0')
            if account == '0':
                raise CardHoldError('invalid_account', 'Destination account is required to enroll a claim.')
            key = self.add_key(
                owner_userid=owner_userid,
                actor=actor,
                actor_type=actor_type,
                nickname=nickname or hold.merchant[:40],
                pan=pan,
                default_account=account,
                brand=presented_brand,
            )
        hold.key_id = key.key_id
        hold.userid = key.userid
        hold.internal_account = key.default_account
        hold.nickname = key.nickname
        hold.brand = key.brand
        hold.last4 = key.last4
        return self._evaluate(hold, actor=actor)

    def assign(
        self,
        *,
        hold_id: str,
        actor: str,
        actor_type: str,
        owner_userid: str,
        key_id: Any = None,
        pan: Any = None,
        default_account: Any = None,
        nickname: Any = None,
    ) -> CardHold:
        self._require_staff(actor_type)
        hold = self.store.get_hold(hold_id)
        if hold is None:
            raise CardHoldError('hold_not_found', 'Card hold not found.')
        if hold.status != HOLD_UNMATCHED:
            raise CardHoldError('not_assignable', 'Only unmatched holds can be assigned.')
        key = None
        if key_id:
            key = self.get_key(key_id=str(key_id), actor=actor, actor_type=actor_type)
            if key.userid != owner_userid:
                raise CardHoldError('cardhold_forbidden', 'Key does not belong to this customer.')
        elif pan not in (None, ''):
            digits = normalize_pan(pan)
            key = self.store.find_key_by_fingerprint(pan_fingerprint(digits), userid=owner_userid)
            if key is None:
                account = normalize_account(default_account or hold.internal_account)
                key = self.add_key(
                    owner_userid=owner_userid,
                    actor=actor,
                    actor_type=actor_type,
                    nickname=nickname or hold.merchant[:40],
                    pan=pan,
                    default_account=account,
                )
        if key is None:
            raise CardHoldError('missing_key', 'Assign requires a key or PAN.')
        hold.key_id = key.key_id
        hold.userid = key.userid
        hold.internal_account = key.default_account
        hold.nickname = key.nickname
        hold.brand = key.brand
        hold.last4 = key.last4
        return self._evaluate(hold, actor=actor)

    def override_ofac(self, *, hold_id: str, actor: str, actor_type: str, note: Any = '') -> CardHold:
        self._require_staff(actor_type)
        hold = self.get_hold(hold_id=hold_id, actor=actor, actor_type=actor_type)
        if hold.status != HOLD_HELD:
            raise CardHoldError('not_overridable', 'Only an OFAC hold can be overridden.')
        hold.ofac_hit = 0
        hold.ofac_match = ''
        hold.note = normalize_note(note) or hold.note
        hold.actor = str(actor)
        return self._evaluate(hold, actor=actor)

    def release_hold(self, *, hold_id: str, actor: str, actor_type: str) -> CardHold:
        self._require_staff(actor_type)
        hold = self.get_hold(hold_id=hold_id, actor=actor, actor_type=actor_type)
        if hold.status not in RELEASABLE:
            raise CardHoldError('not_releasable', 'Only a pending or held authorization can be released.')
        if hold.status == HOLD_HELD:
            raise CardHoldError('ofac_hold', 'OFAC hold must be overridden before release.')
        if hold.actor == str(actor) and self._needs_dual_control(parse_money(hold.amount)):
            raise CardHoldError('same_approver', 'A different employee must release this authorization.')
        hold.releaser = str(actor)
        return self._evaluate(hold, actor=actor)

    def reject(self, *, hold_id: str, actor: str, actor_type: str, reason: Any = 'other', note: Any = '') -> CardHold:
        self._require_staff(actor_type)
        hold = self.get_hold(hold_id=hold_id, actor=actor, actor_type=actor_type)
        if hold.status not in VOIDABLE:
            raise CardHoldError('not_rejectable', 'This authorization cannot be rejected.')
        now = float(self.clock())
        hold.status = HOLD_REJECTED
        hold.reason = normalize_note(reason, limit=40)
        hold.note = normalize_note(note) or hold.note
        hold.updated_at = now
        hold.actor = str(actor)
        self.store.update_hold(hold)
        return hold

    def void_hold(self, *, hold_id: str, actor: str, actor_type: str, note: Any = '') -> CardHold:
        self._require_enabled()
        hold = self.get_hold(hold_id=hold_id, actor=actor, actor_type=actor_type)
        if hold.status not in VOIDABLE:
            raise CardHoldError('not_voidable', 'Only an open authorization can be voided.')
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_void:
                raise CardHoldError('cardhold_forbidden', 'Customers cannot void authorizations.')
            age = float(self.clock()) - hold.created_at
            if age > CUSTOMER_VOID_HOURS * 3600:
                raise CardHoldError('void_window_closed', 'Customer void window is 24 hours.')
        now = float(self.clock())
        hold.status = HOLD_VOIDED
        hold.voided_at = now
        hold.updated_at = now
        hold.note = normalize_note(note) or hold.note
        hold.actor = str(actor)
        self.store.update_hold(hold)
        return hold

    def increment(self, *, hold_id: str, actor: str, actor_type: str, amount: Any) -> CardHold:
        self._require_staff(actor_type)
        hold = self.get_hold(hold_id=hold_id, actor=actor, actor_type=actor_type)
        if hold.status != HOLD_AUTHORIZED:
            raise CardHoldError('not_incrementable', 'Only an authorized hold can be incremented.')
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        current = parse_money(hold.amount)
        if dollars <= current:
            raise CardHoldError('invalid_amount', 'Incremental authorization must exceed the current hold.')
        if self._needs_dual_control(dollars) and hold.releaser != str(actor) and hold.actor == str(actor):
            hold.amount = money_str(dollars)
            hold.status = HOLD_PENDING
            hold.updated_at = float(self.clock())
            self.store.update_hold(hold)
            return hold
        hold.amount = money_str(dollars)
        hold.updated_at = float(self.clock())
        hold.expires_at = self.calendar.expires_at(hold.updated_at, hold.mcc)
        self.store.update_hold(hold)
        return hold

    def capture(
        self,
        *,
        hold_id: str,
        actor: str,
        actor_type: str,
        amount: Any = None,
    ) -> CardHold:
        self._require_staff(actor_type)
        hold = self.get_hold(hold_id=hold_id, actor=actor, actor_type=actor_type)
        if hold.status == HOLD_CAPTURED:
            raise CardHoldError('already_captured', 'Authorization is already captured.')
        if hold.status != HOLD_AUTHORIZED:
            raise CardHoldError('not_capturable', 'Only an authorized hold can be captured.')
        authorized = parse_money(hold.amount)
        dollars = parse_money(amount) if amount not in (None, '') else authorized
        limit = capture_limit(hold.mcc, authorized)
        if dollars > limit:
            raise CardHoldError('amount_out_of_range', 'Capture exceeds the MCC window.')
        if dollars < self.policy.min_amount:
            raise CardHoldError('invalid_amount', 'Capture amount is invalid.')
        now = float(self.clock())
        remark = 'card %s' % hold.merchant
        status = HOLD_CAPTURED
        fail_note = ''
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(hold.internal_account, money_str(dollars), remark)
            except Exception as exc:
                status = HOLD_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = HOLD_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = HOLD_FAILED
                    fail_note = str(result)[:240]
        hold.status = status
        hold.updated_at = now
        hold.actor = str(actor)
        if status == HOLD_CAPTURED:
            hold.captured_amount = money_str(dollars)
            hold.captured_at = now
            hold.mti = MTI_FINANCIAL
        else:
            hold.note = fail_note or hold.note
        self.store.update_hold(hold)
        if status == HOLD_NSF:
            raise CardHoldError('nsf', 'Insufficient funds to capture.', hold=hold)
        if status == HOLD_FAILED:
            raise CardHoldError('failed', 'Capture failed.', hold=hold)
        return hold

    def reverse(self, *, hold_id: str, actor: str, actor_type: str, note: Any = '') -> CardHold:
        self._require_enabled()
        hold = self.get_hold(hold_id=hold_id, actor=actor, actor_type=actor_type)
        if hold.status != HOLD_CAPTURED:
            raise CardHoldError('not_reversible', 'Only a captured authorization can be reversed.')
        now = float(self.clock())
        if actor_type not in EMPLOYEE_ROLES:
            if now - hold.captured_at > CUSTOMER_VOID_HOURS * 3600:
                raise CardHoldError('return_window_closed', 'Customer reversal window is 24 hours.')
        elif now - hold.captured_at > STAFF_REVERSE_DAYS * 86400:
            raise CardHoldError('return_window_closed', 'Staff reversal window is 90 days.')
        dollars = parse_money(hold.captured_amount)
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(hold.internal_account, money_str(dollars), 'card reverse %s' % hold.rrn[-12:])
            except Exception as exc:
                raise CardHoldError('reverse_failed', str(exc)[:240], hold=hold) from exc
            kind = _classify_money_result(result)
            if kind != 'ok':
                raise CardHoldError('reverse_failed', str(result)[:240], hold=hold)
        hold.status = HOLD_REVERSED
        hold.reversed_at = now
        hold.updated_at = now
        hold.mti = MTI_REVERSAL
        hold.note = normalize_note(note) or hold.note
        hold.actor = str(actor)
        self.store.update_hold(hold)
        return hold

    def _void_or_reverse(self, hold: CardHold, *, actor: str, actor_type: str) -> CardHold:
        if hold.status == HOLD_CAPTURED:
            return self.reverse(hold_id=hold.hold_id, actor=actor, actor_type=actor_type)
        return self.void_hold(hold_id=hold.hold_id, actor=actor, actor_type=actor_type)

    def export_hold(self, *, hold_id: str, actor: str, actor_type: str) -> str:
        self._require_staff(actor_type)
        hold = self.get_hold(hold_id=hold_id, actor=actor, actor_type=actor_type)
        if hold.status == HOLD_UNMATCHED:
            raise CardHoldError('not_exportable', 'Unmatched holds cannot be exported.')
        return compose_auth1(
            stan=hold.stan,
            rrn=hold.rrn,
            mti=hold.mti,
            pan_masked=mask_pan(hold.last4.rjust(16, '*')),
            amount=hold.captured_amount if hold.status == HOLD_CAPTURED else hold.amount,
            merchant=hold.merchant,
            mcc=hold.mcc,
        )

    def run_due(self, userid: Optional[str] = None) -> List[CardHold]:
        self._require_enabled()
        now = float(self.clock())
        changed = []
        rows = self.store.list_holds(userid) if userid else self.store.list_holds()
        for hold in rows:
            if hold.status == HOLD_AUTHORIZED and hold.expires_at and now >= hold.expires_at:
                hold.status = HOLD_EXPIRED
                hold.updated_at = now
                hold.voided_at = now
                self.store.update_hold(hold)
                changed.append(hold)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.run_due(userid)
        keys = self.store.list_keys(userid)
        holds = self.store.list_holds(userid)
        unmatched = [row for row in self.store.list_unmatched() if not userid or row.last4 in {key.last4 for key in keys}]
        authorized_ytd = Decimal('0.00')
        captured_ytd = Decimal('0.00')
        open_holds = Decimal('0.00')
        for row in holds:
            amount = parse_money(row.amount, allow_zero=True)
            if row.status == HOLD_AUTHORIZED:
                authorized_ytd += amount
                open_holds += amount
            elif row.status == HOLD_CAPTURED:
                captured_ytd += parse_money(row.captured_amount, allow_zero=True)
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clock': self.calendar.snapshot(now),
            'keys': [row.to_dict() for row in keys[:40]],
            'holds': [row.to_dict() for row in holds[:40]],
            'unmatched': [row.to_dict() for row in unmatched[:40]] if actor_type in EMPLOYEE_ROLES else [],
            'ytd_authorized': money_str(authorized_ytd),
            'ytd_captured': money_str(captured_ytd),
            'open_holds': money_str(open_holds),
            'active_count': sum(1 for row in keys if row.status == KEY_ACTIVE),
            'open_count': sum(1 for row in holds if row.status in OPEN_HOLDS),
        }


_SERVICE: Optional[CardHoldService] = None


def set_service(service: Optional[CardHoldService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[CardHoldService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('CARDHOLD_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryCardHoldStore()
    path = os.environ.get('CARDHOLD_DB', DEFAULT_STORE_PATH)
    return SqliteCardHoldStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[HoldClock] = None,
) -> CardHoldService:
    if store is None:
        store = default_store()
    return CardHoldService(
        CardHoldPolicy.from_env(),
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
        'hold_limit': 409,
        'already_cancelled': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_captured': 409,
        'already_reversed': 409,
        'nsf': 409,
        'failed': 409,
        'reverse_failed': 409,
        'cardhold_forbidden': 403,
        'cardhold_disabled': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_claimable': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_voidable': 403,
        'not_capturable': 403,
        'not_incrementable': 403,
        'not_reversible': 403,
        'not_exportable': 403,
        'void_window_closed': 403,
        'return_window_closed': 403,
        'key_not_found': 404,
        'hold_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_pan': 400,
        'invalid_brand': 400,
        'invalid_mcc': 400,
        'invalid_mti': 400,
        'invalid_stan': 400,
        'invalid_rrn': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_currency': 400,
        'invalid_reference': 400,
        'invalid_file': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_key': 400,
        'missing_hold': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: CardHoldError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('hold') is not None:
        body['hold'] = exc.extra['hold'].to_dict()
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
    except CardHoldError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: CardHoldService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'CardHolds': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_add_key(service: CardHoldService):
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
            'CardHolds': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _key_status_route(service: CardHoldService, status: str, ok_message: str):
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
            'CardHolds': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: CardHoldService):
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
        )
        return jsonify({'preview': preview, 'CardHolds': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest(service: CardHoldService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        hold, created = service.ingest(
            actor=userid,
            actor_type=actor_type,
            amount=values.get('amount'),
            merchant=values.get('merchant') or values.get('name'),
            pan=values.get('pan') or values.get('card_number'),
            mti=values.get('mti') or MTI_AUTH,
            mcc=values.get('mcc'),
            stan=values.get('stan'),
            rrn=values.get('rrn'),
            currency=values.get('currency') or 'USD',
            owner_userid=values.get('customer_id'),
            key_id=values.get('key_id'),
            internal_account=values.get('account'),
        )
        owner = hold.userid or str(values.get('customer_id') or userid)
        return jsonify({
            'message': 'Card hold ingested' if created else 'Card hold already posted',
            'hold': hold.to_dict(),
            'CardHolds': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_file(service: CardHoldService):
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
            'message': 'Card hold file ingested',
            'holds': [row.to_dict() for row in rows],
            'CardHolds': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def handle_claim(service: CardHoldService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    hold_id = str(values.get('hold_id') or '').strip()
    if not hold_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_hold'}), 400
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values) or userid

    def _run():
        hold = service.claim(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            hold_id=hold_id,
            pan=values.get('pan'),
            last4_value=values.get('last4'),
            brand=values.get('brand'),
            default_account=values.get('default_account') or values.get('account'),
            nickname=values.get('nickname'),
        )
        return jsonify({
            'message': 'Card hold claimed',
            'hold': hold.to_dict(),
            'CardHolds': service.snapshot(hold.userid or owner, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_assign(service: CardHoldService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    hold_id = str(values.get('hold_id') or '').strip()
    if not hold_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_hold'}), 400
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        hold = service.assign(
            hold_id=hold_id,
            actor=userid,
            actor_type=actor_type,
            owner_userid=owner,
            key_id=values.get('key_id'),
            pan=values.get('pan'),
            default_account=values.get('default_account') or values.get('account'),
            nickname=values.get('nickname'),
        )
        return jsonify({
            'message': 'Card hold assigned',
            'hold': hold.to_dict(),
            'CardHolds': service.snapshot(hold.userid or owner, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_hold_route(service: CardHoldService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    hold_id = str(values.get('hold_id') or '').strip()
    if not hold_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_hold'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'release':
            hold = service.release_hold(hold_id=hold_id, actor=userid, actor_type=actor_type)
            message = 'Card hold released'
        elif action == 'reject':
            hold = service.reject(
                hold_id=hold_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Card hold rejected'
        elif action == 'override':
            hold = service.override_ofac(
                hold_id=hold_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'void':
            hold = service.void_hold(
                hold_id=hold_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Card hold voided'
        elif action == 'capture':
            hold = service.capture(
                hold_id=hold_id, actor=userid, actor_type=actor_type, amount=values.get('amount'),
            )
            message = 'Card hold captured'
        elif action == 'increment':
            hold = service.increment(
                hold_id=hold_id, actor=userid, actor_type=actor_type, amount=values.get('amount'),
            )
            message = 'Card hold incremented'
        elif action == 'reverse':
            hold = service.reverse(
                hold_id=hold_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Card hold reversed'
        elif action == 'export':
            body = service.export_hold(hold_id=hold_id, actor=userid, actor_type=actor_type)
            hold = service.get_hold(hold_id=hold_id, actor=userid, actor_type=actor_type)
            return jsonify({
                'message': 'Card hold exported',
                'file': body,
                'hold': hold.to_dict(),
                'CardHolds': service.snapshot(hold.userid or userid, actor=userid, actor_type=actor_type),
            }), 200
        else:
            raise CardHoldError('invalid_status', 'Unknown card-hold action.')
        return jsonify({
            'message': message,
            'hold': hold.to_dict(),
            'CardHolds': service.snapshot(hold.userid or userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: CardHoldService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'CardHolds': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_cardhold_routes(app, service: CardHoldService) -> None:
    @app.route('/listCardHolds', methods=['POST', 'GET'])
    def list_card_holds_route():
        return handle_list(service)

    @app.route('/listCardHoldKeys', methods=['POST', 'GET'])
    def list_card_hold_keys_route():
        return handle_list(service)

    @app.route('/listUnmatchedCardHolds', methods=['POST', 'GET'])
    def list_unmatched_card_holds_route():
        return handle_list(service)

    @app.route('/addCardHoldKey', methods=['POST', 'GET'])
    def add_card_hold_key_route():
        return handle_add_key(service)

    @app.route('/pauseCardHoldKey', methods=['POST', 'GET'])
    def pause_card_hold_key_route():
        return _key_status_route(service, KEY_PAUSED, 'Card key paused')

    @app.route('/resumeCardHoldKey', methods=['POST', 'GET'])
    def resume_card_hold_key_route():
        return _key_status_route(service, KEY_ACTIVE, 'Card key resumed')

    @app.route('/cancelCardHoldKey', methods=['POST', 'GET'])
    def cancel_card_hold_key_route():
        return _key_status_route(service, KEY_CANCELLED, 'Card key cancelled')

    @app.route('/previewCardHold', methods=['POST', 'GET'])
    def preview_card_hold_route():
        return handle_preview(service)

    @app.route('/ingestCardHold', methods=['POST', 'GET'])
    def ingest_card_hold_route():
        return handle_ingest(service)

    @app.route('/ingestCardHoldFile', methods=['POST', 'GET'])
    def ingest_card_hold_file_route():
        return handle_ingest_file(service)

    @app.route('/claimCardHold', methods=['POST', 'GET'])
    def claim_card_hold_route():
        return handle_claim(service)

    @app.route('/assignCardHold', methods=['POST', 'GET'])
    def assign_card_hold_route():
        return handle_assign(service)

    @app.route('/overrideCardHoldOfac', methods=['POST', 'GET'])
    def override_card_hold_ofac_route():
        return _staff_hold_route(service, 'override')

    @app.route('/releaseCardHold', methods=['POST', 'GET'])
    def release_card_hold_route():
        return _staff_hold_route(service, 'release')

    @app.route('/rejectCardHold', methods=['POST', 'GET'])
    def reject_card_hold_route():
        return _staff_hold_route(service, 'reject')

    @app.route('/voidCardHold', methods=['POST', 'GET'])
    def void_card_hold_route():
        return _staff_hold_route(service, 'void')

    @app.route('/requestCardHoldVoid', methods=['POST', 'GET'])
    def request_card_hold_void_route():
        return _staff_hold_route(service, 'void')

    @app.route('/captureCardHold', methods=['POST', 'GET'])
    def capture_card_hold_route():
        return _staff_hold_route(service, 'capture')

    @app.route('/incrementCardHold', methods=['POST', 'GET'])
    def increment_card_hold_route():
        return _staff_hold_route(service, 'increment')

    @app.route('/reverseCardHold', methods=['POST', 'GET'])
    def reverse_card_hold_route():
        return _staff_hold_route(service, 'reverse')

    @app.route('/exportCardHold', methods=['POST', 'GET'])
    def export_card_hold_route():
        return _staff_hold_route(service, 'export')

    @app.route('/runDueCardHolds', methods=['POST', 'GET'])
    def run_due_card_holds_route():
        return handle_run_due(service)

   