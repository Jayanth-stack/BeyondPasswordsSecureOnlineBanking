"""Inbound Visa Direct / Mastercard Send receive (issuer OCT and AFT credit).

Staff ingest incoming Original Credit Transactions (MTI 0260) and Account
Funding Transaction credits (MTI 0200 / processing code 10) destined for
enrolled issued PANs. Matched credits post 24/7 onto checking/savings.
Unmatched PANs wait for a customer claim or staff assignment. Network
reversals (MTI 0420) return a posted inbound.

Independent of push-to-card OCT origination (PR #155), card 3DS (PR #153),
card settlement presentment (PR #151), card authorization holds (PR #149),
issuer Account Updater (PR #157), ACH linking (PR #68), and domestic
Fedwire (PR #73). Existing `/fundTransfer`, `/withdrawAmount`, and
`/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- PAN Luhn + IIN brand (Visa Direct / Mastercard Send; not ABA)
- PAN fingerprint uniqueness (SHA-256; never in snapshots)
- ISO 8583 inbound OCT/AFT MTI (not auth 1100 / presentment 1240)
- RRN / STAN uniqueness (not IMAD / ARN / CAVV / VAU ref)
- 24/7 card-network clock (not Fed 17:00 ET)
- OCT1 pipe compose/parse (XML/DOCTYPE rejected)
- Dual-control at $2,500
- 24h unmatched claim / 24h customer return / 90-day staff return

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Full PAN, fingerprint, RRN, and STAN never appear in to_dict / snapshots.
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
    DEFAULT_WATCHLIST,
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

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
KEY_ACTIVE = 'active'
KEY_PAUSED = 'paused'
KEY_CANCELLED = 'cancelled'
KEY_STATUSES = frozenset({KEY_ACTIVE, KEY_PAUSED, KEY_CANCELLED})
OPEN_KEYS = frozenset({KEY_ACTIVE, KEY_PAUSED})

IN_UNMATCHED = 'unmatched'
IN_HELD = 'held'
IN_PENDING = 'pending_release'
IN_POSTED = 'posted'
IN_REJECTED = 'rejected'
IN_RETURNED = 'returned'
IN_EXPIRED = 'expired'
IN_FAILED = 'failed'
IN_STATUSES = frozenset({
    IN_UNMATCHED, IN_HELD, IN_PENDING, IN_POSTED, IN_REJECTED,
    IN_RETURNED, IN_EXPIRED, IN_FAILED,
})
OPEN_IN = frozenset({IN_UNMATCHED, IN_HELD, IN_PENDING})
TERMINAL_IN = frozenset({IN_REJECTED, IN_RETURNED, IN_EXPIRED, IN_FAILED})
REJECTABLE = frozenset({IN_UNMATCHED, IN_HELD, IN_PENDING})
RETURNABLE = frozenset({IN_POSTED})

RAIL_OCT = 'oct'
RAIL_AFT = 'aft'
RAILS = frozenset({RAIL_OCT, RAIL_AFT})
RAIL_ALIASES = {
    'original_credit': RAIL_OCT, 'push': RAIL_OCT, 'visa_direct': RAIL_OCT,
    'mastercard_send': RAIL_OCT, 'oct': RAIL_OCT,
    'account_funding': RAIL_AFT, 'funding': RAIL_AFT, 'aft': RAIL_AFT, 'pull': RAIL_AFT,
}

MTI_OCT = '0260'
MTI_AFT = '0200'
MTI_REVERSAL = '0420'
MTI_ALIASES = {
    '260': MTI_OCT, '0260': MTI_OCT, 'oct': MTI_OCT, 'original_credit': MTI_OCT,
    '200': MTI_AFT, '0200': MTI_AFT, 'aft': MTI_AFT, 'account_funding': MTI_AFT,
    '420': MTI_REVERSAL, '0420': MTI_REVERSAL, 'reversal': MTI_REVERSAL, 'reverse': MTI_REVERSAL,
}
REJECTED_MTI = frozenset({
    '0100', '100', '1100', 'auth', 'authorization',
    '0120', '120', '1200', 'capture',
    '0220', '220', '1240', 'presentment', 'clearing',
    '1442', '1440',
})
PC_OCT = '260000'
PC_AFT = '100000'
PC_ALIASES = {
    '26': PC_OCT, '260000': PC_OCT, 'oct': PC_OCT,
    '10': PC_AFT, '100000': PC_AFT, 'aft': PC_AFT, 'funding': PC_AFT,
}
ALLOWED_MCC = frozenset({'6538', '6536', '6537', '4829', '6012'})
MCC_ALIASES = {
    'money_transfer': '6538', 'oct': '6538', 'push': '6538',
    'funding': '6012', 'aft': '6012', 'wallet': '6012',
    'p2p': '4829', 'wire': '4829',
}
BRAND_VISA = 'visa'
BRAND_MC = 'mastercard'
NETWORK_VISA = 'visa_direct'
NETWORK_MC = 'mastercard_send'
PURPOSES = frozenset({'family', 'goods', 'payroll', 'tax', 'loan', 'rent', 'other'})
PURPOSE_ALIASES = {
    'personal': 'family', 'gift': 'family', 'support': 'family',
    'invoice': 'goods', 'purchase': 'goods', 'vendor': 'goods',
    'salary': 'payroll', 'wage': 'payroll',
    'irs': 'tax', 'taxes': 'tax',
    'mortgage': 'loan', 'housing': 'rent',
}
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/octin.sqlite'


class OctinError(ValueError):
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
        raise OctinError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise OctinError('invalid_name', 'Sender legal name must be 2-80 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise OctinError('invalid_purpose', 'Unknown card-receive purpose.')
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
        raise OctinError('invalid_pan', 'PAN must be 13-19 digits.')
    if not luhn_check_digit_ok(digits):
        raise OctinError('invalid_pan', 'PAN failed Luhn checksum.')
    return digits


def mask_pan(pan: str) -> str:
    digits = ''.join(ch for ch in str(pan or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    brand = ''
    try:
        brand = brand_from_iin(digits)
    except OctinError:
        brand = ''
    prefix = brand + ' ' if brand else ''
    return '%s****%s' % (prefix, digits[-4:])


def pan_fingerprint(pan: str) -> str:
    return hashlib.sha256(str(pan).encode('utf-8')).hexdigest()


def brand_from_iin(pan: str) -> str:
    digits = ''.join(ch for ch in str(pan or '') if ch.isdigit())
    if not digits:
        raise OctinError('invalid_brand', 'PAN is required to derive brand.')
    if digits.startswith('4'):
        return BRAND_VISA
    two = int(digits[:2]) if len(digits) >= 2 else 0
    four = int(digits[:4]) if len(digits) >= 4 else 0
    if 51 <= two <= 55 or 2221 <= four <= 2720:
        return BRAND_MC
    if digits.startswith('34') or digits.startswith('37'):
        raise OctinError('invalid_brand', 'American Express is not on Visa Direct / Mastercard Send.')
    if digits.startswith('6011') or digits.startswith('65') or 644 <= int(digits[:3] or '0') <= 649:
        raise OctinError('invalid_brand', 'Discover is not on Visa Direct / Mastercard Send.')
    raise OctinError('invalid_brand', 'Unsupported card brand.')


def network_for_brand(brand: str) -> str:
    if brand == BRAND_VISA:
        return NETWORK_VISA
    if brand == BRAND_MC:
        return NETWORK_MC
    raise OctinError('invalid_brand', 'Unsupported card-receive network.')


def normalize_expiry(value: Any, *, now_ts: Optional[float] = None, require_future: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) == 6 and digits[:2].isdigit():
        digits = digits[:2] + digits[4:]
    if len(digits) != 4:
        raise OctinError('invalid_expiry', 'Expiry must be MMYY.')
    month = int(digits[:2])
    year = int(digits[2:])
    if month < 1 or month > 12:
        raise OctinError('invalid_expiry', 'Expiry month must be 01-12.')
    if require_future and now_ts is not None:
        local = datetime.fromtimestamp(float(now_ts), tz=timezone.utc)
        current = (local.year % 100) * 100 + local.month
        candidate = year * 100 + month
        if candidate < current:
            raise OctinError('invalid_expiry', 'Expiry is in the past.')
    return '%02d%02d' % (month, year)


def mask_expiry(expiry: str) -> str:
    digits = ''.join(ch for ch in str(expiry or '') if ch.isdigit())
    if len(digits) < 2:
        return '**'
    return '**' + digits[-2:]


def normalize_rail(value: Any, *, default: str = RAIL_OCT) -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text not in RAILS:
        raise OctinError('invalid_rail', 'Rail must be oct or aft.')
    return text


def normalize_mti(value: Any, *, default: Optional[str] = None) -> str:
    text = str(value or default or '').strip().lower().replace('-', '_').replace(' ', '_')
    if text in REJECTED_MTI:
        raise OctinError('invalid_mti', 'Auth/presentment MTI is not an inbound OCT/AFT.')
    text = MTI_ALIASES.get(text, text)
    if len(text) == 3 and text.isdigit():
        text = '0' + text
    if text not in {MTI_OCT, MTI_AFT, MTI_REVERSAL}:
        raise OctinError('invalid_mti', 'MTI must be 0260 (OCT), 0200 (AFT), or 0420 (reversal).')
    return text


def rail_for_mti(mti: str) -> str:
    if mti == MTI_AFT:
        return RAIL_AFT
    return RAIL_OCT


def mti_for_rail(rail: str) -> str:
    if rail == RAIL_AFT:
        return MTI_AFT
    return MTI_OCT


def normalize_processing_code(value: Any, *, rail: str) -> str:
    if value is None or str(value).strip() == '':
        return PC_AFT if rail == RAIL_AFT else PC_OCT
    text = str(value).strip().lower().replace('-', '_')
    digits = ''.join(ch for ch in text if ch.isdigit())
    if digits in PC_ALIASES:
        code = PC_ALIASES[digits]
    elif text in PC_ALIASES:
        code = PC_ALIASES[text]
    elif len(digits) == 6:
        code = digits
    elif len(digits) == 2:
        code = digits + '0000'
    else:
        raise OctinError('invalid_processing_code', 'Processing code must be 10 (AFT) or 26 (OCT).')
    expected = PC_AFT if rail == RAIL_AFT else PC_OCT
    if code != expected:
        raise OctinError('invalid_processing_code', 'Processing code does not match the rail.')
    return code


def normalize_mcc(value: Any, *, default: str = '6538') -> str:
    text = str(value or default).strip().lower().replace('-', '_')
    text = MCC_ALIASES.get(text, text)
    digits = ''.join(ch for ch in text if ch.isdigit())
    if digits not in ALLOWED_MCC:
        raise OctinError('invalid_mcc', 'MCC must be a Visa Direct / Mastercard Send money-transfer code.')
    return digits


def compose_rrn(cycle_date: str, sequence: int) -> str:
    """12-digit RRN: YYMMDD + 6-digit sequence. Not IMAD/ARN/STAN/CAVV."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise OctinError('invalid_rrn', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise OctinError('invalid_rrn', 'RRN sequence out of range.')
    return '%s%06d' % (day[2:], seq)


def normalize_rrn(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 12:
        raise OctinError('invalid_rrn', 'RRN must be 12 digits.')
    return digits


def mask_rrn(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return '****' + digits[-4:]


def compose_stan(sequence: int) -> str:
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise OctinError('invalid_stan', 'STAN sequence out of range.')
    return '%06d' % seq


def normalize_stan(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        raise OctinError('invalid_stan', 'STAN is required.')
    if len(digits) > 6:
        raise OctinError('invalid_stan', 'STAN must be 1-6 digits.')
    return digits.zfill(6)


def mask_stan(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 2:
        return '****'
    return '****' + digits[-2:]


def _reject_markup(text: str) -> None:
    lowered = str(text or '').lower()
    if '<?xml' in lowered or '<!doctype' in lowered or '<xml' in lowered:
        raise OctinError('invalid_file', 'XML/DOCTYPE is not a valid OCT1 record.')


def compose_oct1(
    *,
    mti: str,
    pan: str,
    expiry: str,
    amount: str,
    mcc: str,
    processing_code: str,
    rrn: str,
    stan: str,
    sender: str,
    rail: str,
    status: str = IN_POSTED,
) -> str:
    return 'OCT1|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s' % (
        mti, pan, expiry, amount, mcc, processing_code, rrn, stan,
        str(sender or '').replace('|', ' '), rail, status,
    )


def parse_oct1(line: str) -> Dict[str, str]:
    text = str(line or '').strip()
    _reject_markup(text)
    if not text:
        raise OctinError('invalid_file', 'Empty OCT1 record.')
    if not text.startswith('OCT1|'):
        raise OctinError('invalid_file', 'Record must start with OCT1|.')
    parts = text.split('|')
    if len(parts) < 12:
        raise OctinError('invalid_file', 'OCT1 record is missing fields.')
    pan = parts[2]
    if '*' in pan:
        raise OctinError('invalid_pan', 'Ingest requires a full PAN, not a mask.')
    return {
        'mti': parts[1],
        'pan': pan,
        'expiry': parts[3],
        'amount': parts[4],
        'mcc': parts[5],
        'processing_code': parts[6],
        'rrn': parts[7],
        'stan': parts[8],
        'sender_name': parts[9],
        'rail': parts[10],
        'status': parts[11],
    }


def split_oct1_file(payload: Any) -> List[str]:
    text = str(payload or '')
    _reject_markup(text)
    lines = []
    for raw in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        item = raw.strip()
        if item:
            lines.append(item)
    if not lines:
        raise OctinError('invalid_file', 'OCT1 file is empty.')
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


class CardNetworkClock:
    """Visa Direct / Mastercard Send 24/7 clock. Not Fed 17:00 ET."""

    def __init__(self, *, tz_offset_hours: int = -4) -> None:
        self.tz_offset_hours = int(tz_offset_hours)
        self.tz = timezone(timedelta(hours=self.tz_offset_hours))

    def local_dt(self, ts: float) -> datetime:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(self.tz)

    def is_open(self, ts: float) -> bool:
        _ = ts
        return True

    def cycle_date(self, ts: float) -> str:
        return self.local_dt(ts).date().strftime('%Y%m%d')

    def snapshot(self, ts: float) -> Dict[str, Any]:
        local = self.local_dt(ts)
        cycle = self.cycle_date(ts)
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'cutoff': '24/7',
            'after_cutoff': False,
            'network_open': True,
            'weekday': local.strftime('%A'),
            'value_date': local.date().isoformat(),
            'cycle_date': cycle,
        }


@dataclass
class OctinPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_claim: bool = True
    allow_credit: bool = False
    max_keys: int = 12
    max_inbounds: int = 120
    min_amount: Decimal = Decimal('1.00')
    oct_max: Decimal = Decimal('10000.00')
    aft_max: Decimal = Decimal('5000.00')
    dual_control_threshold: Decimal = Decimal('2500.00')
    claim_seconds: int = 24 * 3600
    customer_return_seconds: int = 24 * 3600
    staff_return_seconds: int = 90 * 24 * 3600
    tz_offset_hours: int = -4
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST

    @classmethod
    def from_env(cls) -> 'OctinPolicy':
        extra = _env_list('OCTIN_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('OCTIN_ENABLED', True),
            customer_manage=_env_bool('OCTIN_CUSTOMER_MANAGE', True),
            customer_claim=_env_bool('OCTIN_CUSTOMER_CLAIM', True),
            allow_credit=_env_bool('OCTIN_ALLOW_CREDIT', False),
            max_keys=max(1, _env_int('OCTIN_MAX_KEYS', 12)),
            max_inbounds=max(1, _env_int('OCTIN_MAX_INBOUNDS', 120)),
            min_amount=_env_money('OCTIN_MIN', '1.00'),
            oct_max=_env_money('OCTIN_OCT_MAX', '10000.00'),
            aft_max=_env_money('OCTIN_AFT_MAX', '5000.00'),
            dual_control_threshold=_env_money('OCTIN_DUAL_CONTROL', '2500.00'),
            claim_seconds=max(60, _env_int('OCTIN_CLAIM_SECONDS', 24 * 3600)),
            customer_return_seconds=max(60, _env_int('OCTIN_CUSTOMER_RETURN_SECONDS', 24 * 3600)),
            staff_return_seconds=max(60, _env_int('OCTIN_STAFF_RETURN_SECONDS', 90 * 24 * 3600)),
            tz_offset_hours=_env_int('OCTIN_TZ_OFFSET', -4),
            watchlist=watch,
        )


@dataclass
class OctinKey:
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
class OctinInbound:
    inbound_id: str
    rrn: str
    stan: str
    userid: str
    destination_account: str
    amount: str
    sender_name: str
    rail: str
    mti: str
    pan: str
    expiry: str
    brand: str
    network: str
    fingerprint: str
    mcc: str
    processing_code: str
    purpose: str
    status: str
    reason: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    ofac_overridden: int
    created_at: float
    updated_at: float
    posted_at: float = 0.0
    returned_at: float = 0.0
    expiry_at: float = 0.0
    nickname: str = ''
    key_id: str = ''
    merchant: str = ''
    note: str = ''
    value_date: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'inbound_id': self.inbound_id,
            'rrn_masked': mask_rrn(self.rrn) if self.rrn else '',
            'stan_masked': mask_stan(self.stan) if self.stan else '',
            'userid': self.userid,
            'destination_account': self.destination_account,
            'amount': self.amount,
            'sender_name': self.sender_name,
            'rail': self.rail,
            'mti': self.mti,
            'pan_masked': mask_pan(self.pan),
            'last4': last4(self.pan),
            'expiry_masked': mask_expiry(self.expiry) if self.expiry else '',
            'brand': self.brand,
            'network': self.network,
            'mcc': self.mcc,
            'processing_code': self.processing_code,
            'purpose': self.purpose,
            'status': self.status,
            'reason': self.reason,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'posted_at': self.posted_at,
            'returned_at': self.returned_at,
            'expiry_at': self.expiry_at,
            'nickname': self.nickname,
            'key_id': self.key_id,
            'merchant': self.merchant,
            'note': self.note,
            'value_date': self.value_date,
            'unmatched': self.status == IN_UNMATCHED,
            'held': self.status == IN_HELD,
            'pending_release': self.status == IN_PENDING,
            'posted': self.status == IN_POSTED,
            'claimable': self.status == IN_UNMATCHED,
            'returnable': self.status == IN_POSTED,
        }


def _clone_key(row: OctinKey) -> OctinKey:
    return OctinKey(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_inbound(row: OctinInbound) -> OctinInbound:
    return OctinInbound(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _key_from_row(row: Any) -> OctinKey:
    return OctinKey(
        key_id=row['key_id'], userid=row['userid'], nickname=row['nickname'],
        legal_name=row['legal_name'], pan=row['pan'], expiry=row['expiry'],
        brand=row['brand'], network=row['network'], fingerprint=row['fingerprint'],
        default_account=row['default_account'], status=row['status'], actor=row['actor'],
        created_at=float(row['created_at']), updated_at=float(row['updated_at']),
    )


def _inbound_from_row(row: Any) -> OctinInbound:
    return OctinInbound(
        inbound_id=row['inbound_id'], rrn=row['rrn'] or '', stan=row['stan'] or '',
        userid=row['userid'] or '', destination_account=row['destination_account'] or '',
        amount=row['amount'], sender_name=row['sender_name'], rail=row['rail'],
        mti=row['mti'], pan=row['pan'], expiry=row['expiry'] or '', brand=row['brand'],
        network=row['network'], fingerprint=row['fingerprint'], mcc=row['mcc'],
        processing_code=row['processing_code'], purpose=row['purpose'] or 'other',
        status=row['status'], reason=row['reason'] or '', actor=row['actor'],
        releaser=row['releaser'] or '', ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '', ofac_overridden=int(row['ofac_overridden'] or 0),
        created_at=float(row['created_at']), updated_at=float(row['updated_at']),
        posted_at=float(row['posted_at'] or 0), returned_at=float(row['returned_at'] or 0),
        expiry_at=float(row['expiry_at'] or 0), nickname=row['nickname'] or '',
        key_id=row['key_id'] or '', merchant=row['merchant'] or '', note=row['note'] or '',
        value_date=row['value_date'] or '',
    )


class MemoryOctinStore:
    def __init__(self) -> None:
        self._keys: Dict[str, OctinKey] = {}
        self._inbounds: Dict[str, OctinInbound] = {}
        self._by_rrn: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_key(self, row: OctinKey) -> None:
        with self._lock:
            self._keys[row.key_id] = row

    def get_key(self, key_id: str) -> Optional[OctinKey]:
        with self._lock:
            row = self._keys.get(key_id)
            return _clone_key(row) if row else None

    def update_key(self, row: OctinKey) -> None:
        with self._lock:
            self._keys[row.key_id] = row

    def list_keys(self, userid: Optional[str] = None, *, include_cancelled: bool = True) -> List[OctinKey]:
        with self._lock:
            rows = [_clone_key(row) for row in self._keys.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_cancelled:
            rows = [row for row in rows if row.status != KEY_CANCELLED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_key_by_nickname(self, userid: str, nickname: str) -> Optional[OctinKey]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._keys.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_KEYS:
                    return _clone_key(row)
        return None

    def find_key_by_fingerprint(self, fingerprint: str) -> Optional[OctinKey]:
        with self._lock:
            for row in self._keys.values():
                if row.fingerprint == fingerprint and row.status in OPEN_KEYS:
                    return _clone_key(row)
        return None

    def put_inbound(self, row: OctinInbound) -> OctinInbound:
        with self._lock:
            existing_id = self._by_rrn.get(row.rrn)
            if existing_id is not None:
                return self._inbounds[existing_id]
            self._inbounds[row.inbound_id] = row
            if row.rrn:
                self._by_rrn[row.rrn] = row.inbound_id
            return row

    def update_inbound(self, row: OctinInbound) -> None:
        with self._lock:
            self._inbounds[row.inbound_id] = row
            if row.rrn:
                self._by_rrn[row.rrn] = row.inbound_id

    def get_inbound(self, inbound_id: str) -> Optional[OctinInbound]:
        with self._lock:
            row = self._inbounds.get(inbound_id)
            return _clone_inbound(row) if row else None

    def get_by_rrn(self, rrn: str) -> Optional[OctinInbound]:
        with self._lock:
            inbound_id = self._by_rrn.get(rrn)
            return _clone_inbound(self._inbounds[inbound_id]) if inbound_id else None

    def list_inbounds(
        self,
        userid: Optional[str] = None,
        *,
        status: Optional[str] = None,
        fingerprint: Optional[str] = None,
    ) -> List[OctinInbound]:
        with self._lock:
            rows = [_clone_inbound(row) for row in self._inbounds.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        if fingerprint is not None:
            rows = [row for row in rows if row.fingerprint == fingerprint]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def next_rrn_sequence(self, cycle_date: str) -> int:
        prefix = cycle_date[2:]
        with self._lock:
            used = [row.rrn for row in self._inbounds.values() if row.rrn.startswith(prefix)]
        return len(used) + 1

    def next_stan_sequence(self) -> int:
        with self._lock:
            return len(self._inbounds) + 1


class SqliteOctinStore:
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
                CREATE TABLE IF NOT EXISTS inbounds (
                    inbound_id TEXT PRIMARY KEY,
                    rrn TEXT,
                    stan TEXT,
                    userid TEXT,
                    destination_account TEXT,
                    amount TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    rail TEXT NOT NULL,
                    mti TEXT NOT NULL,
                    pan TEXT NOT NULL,
                    expiry TEXT,
                    brand TEXT NOT NULL,
                    network TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    mcc TEXT NOT NULL,
                    processing_code TEXT NOT NULL,
                    purpose TEXT,
                    status TEXT NOT NULL,
                    reason TEXT,
                    actor TEXT NOT NULL,
                    releaser TEXT,
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT,
                    ofac_overridden INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    posted_at REAL,
                    returned_at REAL,
                    expiry_at REAL,
                    nickname TEXT,
                    key_id TEXT,
                    merchant TEXT,
                    note TEXT,
                    value_date TEXT
                )
                """
            )
            conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_octin_rrn ON inbounds(rrn) WHERE rrn IS NOT NULL AND rrn != ""')
            conn.commit()

    def put_key(self, row: OctinKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO keys (
                    key_id, userid, nickname, legal_name, pan, expiry, brand, network,
                    fingerprint, default_account, status, actor, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row.key_id, row.userid, row.nickname, row.legal_name, row.pan, row.expiry,
                    row.brand, row.network, row.fingerprint, row.default_account, row.status,
                    row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_key(self, key_id: str) -> Optional[OctinKey]:
        with self._lock, self._connect() as conn:
            cur = conn.execute('SELECT * FROM keys WHERE key_id = ?', (key_id,))
            row = cur.fetchone()
        return _key_from_row(row) if row else None

    def update_key(self, row: OctinKey) -> None:
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

    def list_keys(self, userid: Optional[str] = None, *, include_cancelled: bool = True) -> List[OctinKey]:
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

    def find_key_by_nickname(self, userid: str, nickname: str) -> Optional[OctinKey]:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                SELECT * FROM keys WHERE userid = ? AND lower(nickname) = lower(?)
                  AND status IN ('active','paused') LIMIT 1
                """,
                (userid, nickname),
            )
            row = cur.fetchone()
        return _key_from_row(row) if row else None

    def find_key_by_fingerprint(self, fingerprint: str) -> Optional[OctinKey]:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                SELECT * FROM keys WHERE fingerprint = ? AND status IN ('active','paused') LIMIT 1
                """,
                (fingerprint,),
            )
            row = cur.fetchone()
        return _key_from_row(row) if row else None

    def put_inbound(self, row: OctinInbound) -> OctinInbound:
        with self._lock, self._connect() as conn:
            if row.rrn:
                cur = conn.execute('SELECT * FROM inbounds WHERE rrn = ?', (row.rrn,))
                existing = cur.fetchone()
                if existing is not None:
                    return _inbound_from_row(existing)
            conn.execute(
                """
                INSERT INTO inbounds (
                    inbound_id, rrn, stan, userid, destination_account, amount, sender_name,
                    rail, mti, pan, expiry, brand, network, fingerprint, mcc, processing_code,
                    purpose, status, reason, actor, releaser, ofac_hit, ofac_match, ofac_overridden,
                    created_at, updated_at, posted_at, returned_at, expiry_at, nickname, key_id,
                    merchant, note, value_date
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row.inbound_id, row.rrn, row.stan, row.userid, row.destination_account,
                    row.amount, row.sender_name, row.rail, row.mti, row.pan, row.expiry,
                    row.brand, row.network, row.fingerprint, row.mcc, row.processing_code,
                    row.purpose, row.status, row.reason, row.actor, row.releaser, row.ofac_hit,
                    row.ofac_match, row.ofac_overridden, row.created_at, row.updated_at,
                    row.posted_at, row.returned_at, row.expiry_at, row.nickname, row.key_id,
                    row.merchant, row.note, row.value_date,
                ),
            )
            conn.commit()
            return row

    def update_inbound(self, row: OctinInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE inbounds SET rrn=?, stan=?, userid=?, destination_account=?, amount=?,
                    sender_name=?, rail=?, mti=?, pan=?, expiry=?, brand=?, network=?, fingerprint=?,
                    mcc=?, processing_code=?, purpose=?, status=?, reason=?, actor=?, releaser=?,
                    ofac_hit=?, ofac_match=?, ofac_overridden=?, updated_at=?, posted_at=?,
                    returned_at=?, expiry_at=?, nickname=?, key_id=?, merchant=?, note=?, value_date=?
                WHERE inbound_id=?
                """,
                (
                    row.rrn, row.stan, row.userid, row.destination_account, row.amount,
                    row.sender_name, row.rail, row.mti, row.pan, row.expiry, row.brand,
                    row.network, row.fingerprint, row.mcc, row.processing_code, row.purpose,
                    row.status, row.reason, row.actor, row.releaser, row.ofac_hit, row.ofac_match,
                    row.ofac_overridden, row.updated_at, row.posted_at, row.returned_at,
                    row.expiry_at, row.nickname, row.key_id, row.merchant, row.note,
                    row.value_date, row.inbound_id,
                ),
            )
            conn.commit()

    def get_inbound(self, inbound_id: str) -> Optional[OctinInbound]:
        with self._lock, self._connect() as conn:
            cur = conn.execute('SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,))
            row = cur.fetchone()
        return _inbound_from_row(row) if row else None

    def get_by_rrn(self, rrn: str) -> Optional[OctinInbound]:
        with self._lock, self._connect() as conn:
            cur = conn.execute('SELECT * FROM inbounds WHERE rrn = ?', (rrn,))
            row = cur.fetchone()
        return _inbound_from_row(row) if row else None

    def list_inbounds(
        self,
        userid: Optional[str] = None,
        *,
        status: Optional[str] = None,
        fingerprint: Optional[str] = None,
    ) -> List[OctinInbound]:
        sql = 'SELECT * FROM inbounds'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if status is not None:
            clauses.append('status = ?')
            params.append(status)
        if fingerprint is not None:
            clauses.append('fingerprint = ?')
            params.append(fingerprint)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_inbound_from_row(row) for row in rows]

    def next_rrn_sequence(self, cycle_date: str) -> int:
        prefix = cycle_date[2:] + '%'
        with self._lock, self._connect() as conn:
            cur = conn.execute('SELECT COUNT(*) FROM inbounds WHERE rrn LIKE ?', (prefix,))
            count = int(cur.fetchone()[0])
        return count + 1

    def next_stan_sequence(self) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute('SELECT COUNT(*) FROM inbounds')
            count = int(cur.fetchone()[0])
        return count + 1


class OctinService:
    def __init__(
        self,
        policy: OctinPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[CardNetworkClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or CardNetworkClock(tz_offset_hours=policy.tz_offset_hours)

    def _now(self) -> float:
        return float(self.clock())

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise OctinError('octin_disabled', 'Card receive is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise OctinError('octin_forbidden', 'Customers cannot manage card-receive keys.')

    def _require_claim(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_claim:
            raise OctinError('octin_forbidden', 'Customers cannot claim inbound card credits.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise OctinError('octin_forbidden', 'Staff only.')

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
            raise OctinError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise OctinError('credit_not_allowed', 'Credit accounts cannot receive OCT/AFT.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _assert_amount(self, rail: str, amount: Decimal) -> None:
        if amount < self.policy.min_amount:
            raise OctinError('amount_out_of_range', 'Amount is outside the card-receive range.')
        if rail == RAIL_OCT and amount > self.policy.oct_max:
            raise OctinError('oct_amount_exceeded', 'Amount is outside the OCT range.')
        if rail == RAIL_AFT and amount > self.policy.aft_max:
            raise OctinError('aft_amount_exceeded', 'Amount is outside the AFT range.')

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
    ) -> OctinKey:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise OctinError('octin_forbidden', 'Not allowed to enroll cards for this customer.')
        now = self._now()
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
            raise OctinError('key_duplicate', 'A card with that nickname already exists.')
        existing = self.store.find_key_by_fingerprint(fingerprint)
        if existing is not None:
            raise OctinError('key_duplicate', 'That PAN is already enrolled.')
        open_rows = [row for row in self.store.list_keys(owner_userid) if row.status in OPEN_KEYS]
        if len(open_rows) >= self.policy.max_keys:
            raise OctinError('key_limit', 'Card-receive key limit reached.')
        row = OctinKey(
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

    def get_key(self, *, key_id: str, actor: str, actor_type: str) -> OctinKey:
        self._require_enabled()
        row = self.store.get_key(key_id)
        if row is None:
            raise OctinError('key_not_found', 'Card-receive key not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise OctinError('octin_forbidden', 'Not allowed to view this key.')
        return row

    def set_key_status(
        self,
        *,
        key_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> OctinKey:
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
            raise OctinError('invalid_status', 'Status must be pause, resume, or cancel.')
        if row.status == KEY_CANCELLED:
            raise OctinError('already_cancelled', 'Key is already cancelled.')
        if wanted == KEY_PAUSED:
            if row.status == KEY_PAUSED:
                raise OctinError('already_paused', 'Key is already paused.')
            if row.status != KEY_ACTIVE:
                raise OctinError('invalid_status', 'Only an active key can be paused.')
        elif wanted == KEY_ACTIVE:
            if row.status == KEY_ACTIVE:
                raise OctinError('already_active', 'Key is already active.')
            if row.status != KEY_PAUSED:
                raise OctinError('invalid_status', 'Only a paused key can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = self._now()
        self.store.update_key(row)
        if wanted == KEY_ACTIVE:
            self._rematch_unmatched(row)
        return row

    def _rematch_unmatched(self, enrollment: OctinKey) -> None:
        if enrollment.status != KEY_ACTIVE:
            return
        for inbound in self.store.list_inbounds(status=IN_UNMATCHED):
            if inbound.fingerprint != enrollment.fingerprint:
                continue
            inbound.userid = enrollment.userid
            inbound.destination_account = enrollment.default_account
            inbound.key_id = enrollment.key_id
            inbound.nickname = enrollment.nickname
            inbound.updated_at = self._now()
            self.store.update_inbound(inbound)
            self._evaluate(inbound, rescreen=True)

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        values: Dict[str, Any],
    ) -> Tuple[OctinInbound, bool]:
        self._require_staff(actor_type)
        return self._ingest_values(actor=actor, actor_type=actor_type, values=values)

    def ingest_file(
        self,
        *,
        actor: str,
        actor_type: str,
        payload: str,
        sender_name: str = '',
    ) -> Tuple[List[OctinInbound], int]:
        self._require_staff(actor_type)
        created = 0
        rows = []
        fallback = normalize_legal_name(sender_name) if sender_name else ''
        for record in split_oct1_file(payload):
            parsed = parse_oct1(record)
            if fallback and not parsed.get('sender_name'):
                parsed['sender_name'] = fallback
            row, is_new = self._ingest_values(actor=actor, actor_type=actor_type, values=parsed)
            rows.append(row)
            if is_new:
                created += 1
        return rows, created

    def _ingest_values(self, *, actor: str, actor_type: str, values: Dict[str, Any]) -> Tuple[OctinInbound, bool]:
        mti = normalize_mti(values.get('mti') or values.get('rail') or MTI_OCT)
        if mti == MTI_REVERSAL:
            return self._ingest_reversal(actor=actor, actor_type=actor_type, values=values), False
        rail = normalize_rail(values.get('rail') or rail_for_mti(mti))
        if mti_for_rail(rail) != mti and values.get('mti'):
            # Explicit MTI wins; align rail to MTI.
            rail = rail_for_mti(mti)
        amount = parse_money(values.get('amount'))
        self._assert_amount(rail, amount)
        pan = normalize_pan(values.get('pan'))
        brand = brand_from_iin(pan)
        network = network_for_brand(brand)
        now = self._now()
        expiry = ''
        if values.get('expiry') not in (None, ''):
            expiry = normalize_expiry(values.get('expiry'), now_ts=now, require_future=False)
        mcc = normalize_mcc(values.get('mcc'), default='6012' if rail == RAIL_AFT else '6538')
        pc = normalize_processing_code(values.get('processing_code') or values.get('pc'), rail=rail)
        sender = normalize_legal_name(values.get('sender_name') or values.get('name') or 'CARD PAYER')
        purpose = normalize_purpose(values.get('purpose'), default='other')
        cycle = self.calendar.cycle_date(now)
        if values.get('rrn'):
            rrn = normalize_rrn(values.get('rrn'))
        else:
            rrn = compose_rrn(cycle, self.store.next_rrn_sequence(cycle))
        existing = self.store.get_by_rrn(rrn)
        if existing is not None:
            return existing, False
        if len(self.store.list_inbounds()) >= self.policy.max_inbounds:
            raise OctinError('inbound_limit', 'Inbound card-credit limit reached.')
        if values.get('stan'):
            stan = normalize_stan(values.get('stan'))
        else:
            stan = compose_stan(self.store.next_stan_sequence())
        fingerprint = pan_fingerprint(pan)
        row = OctinInbound(
            inbound_id=uuid.uuid4().hex,
            rrn=rrn,
            stan=stan,
            userid='',
            destination_account='',
            amount=money_str(amount),
            sender_name=sender,
            rail=rail,
            mti=mti,
            pan=pan,
            expiry=expiry,
            brand=brand,
            network=network,
            fingerprint=fingerprint,
            mcc=mcc,
            processing_code=pc,
            purpose=purpose,
            status=IN_UNMATCHED,
            reason='',
            actor=str(actor),
            releaser='',
            ofac_hit=0,
            ofac_match='',
            ofac_overridden=0,
            created_at=now,
            updated_at=now,
            expiry_at=now + self.policy.claim_seconds,
            merchant=normalize_note(values.get('merchant'), limit=80),
            note=normalize_note(values.get('note')),
            value_date=self.calendar.local_dt(now).date().isoformat(),
        )
        enrollment = self.store.find_key_by_fingerprint(fingerprint)
        if enrollment is not None and enrollment.status == KEY_ACTIVE:
            row.userid = enrollment.userid
            row.destination_account = enrollment.default_account
            row.key_id = enrollment.key_id
            row.nickname = enrollment.nickname
        stored = self.store.put_inbound(row)
        if stored.inbound_id != row.inbound_id:
            return stored, False
        self._evaluate(row, rescreen=True)
        return row, True

    def _ingest_reversal(self, *, actor: str, actor_type: str, values: Dict[str, Any]) -> OctinInbound:
        rrn = normalize_rrn(values.get('rrn') or values.get('original_rrn'))
        row = self.store.get_by_rrn(rrn)
        if row is None:
            raise OctinError('inbound_not_found', 'Original OCT/AFT RRN was not found.')
        return self.return_inbound(
            inbound_id=row.inbound_id,
            actor=actor,
            actor_type=actor_type,
            staff=True,
            note=normalize_note(values.get('note') or 'network reversal'),
        )

    def _evaluate(self, row: OctinInbound, *, rescreen: bool) -> OctinInbound:
        now = self._now()
        if row.status in TERMINAL_IN or row.status == IN_POSTED:
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
            self._assert_internal_account(row.userid, row.destination_account)
        except OctinError as exc:
            if exc.code == 'credit_not_allowed':
                row.status = IN_UNMATCHED
                row.reason = 'credit_not_allowed'
                row.updated_at = now
                self.store.update_inbound(row)
                return row
            row.status = IN_UNMATCHED
            row.reason = 'unmatched'
            row.userid = ''
            row.destination_account = ''
            row.key_id = ''
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if rescreen and not row.ofac_overridden:
            hit = self._screen(row.sender_name)
            row.ofac_hit = 1 if hit.hit else 0
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
        amount = parse_money(row.amount)
        if amount >= self.policy.dual_control_threshold and not row.releaser:
            row.status = IN_PENDING
            row.reason = 'dual_control'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        return self._post(row)

    def _post(self, row: OctinInbound) -> OctinInbound:
        if self.credit_fn is None:
            raise OctinError('failed', 'Credit function is not configured.')
        remark = '%s from %s' % (row.rail, row.sender_name[:40])
        result = self.credit_fn(row.destination_account, row.amount, remark)
        classified = _classify_money_result(result)
        now = self._now()
        if classified == 'nsf':
            row.status = IN_FAILED
            row.reason = 'nsf'
            row.updated_at = now
            self.store.update_inbound(row)
            raise OctinError('nsf', 'Credit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            row.status = IN_FAILED
            row.reason = 'failed'
            row.updated_at = now
            self.store.update_inbound(row)
            raise OctinError('failed', 'Credit failed.', inbound=row)
        row.status = IN_POSTED
        row.reason = ''
        row.posted_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def preview(self, *, inbound_id: str = '', rrn: str = '', actor: str = '', actor_type: str = '') -> Dict[str, Any]:
        row = self._get(inbound_id=inbound_id, rrn=rrn)
        self._assert_visible(row, actor, actor_type)
        amount = parse_money(row.amount, allow_zero=True)
        return {
            'inbound': row.to_dict(),
            'clock': self.calendar.snapshot(self._now()),
            'dual_control': amount >= self.policy.dual_control_threshold,
            'network_open': True,
            'oct_cap': money_str(self.policy.oct_max),
            'aft_cap': money_str(self.policy.aft_max),
        }

    def _get(self, *, inbound_id: str = '', rrn: str = '') -> OctinInbound:
        row = None
        if inbound_id:
            row = self.store.get_inbound(str(inbound_id).strip())
        if row is None and rrn:
            row = self.store.get_by_rrn(normalize_rrn(rrn))
        if row is None:
            raise OctinError('inbound_not_found', 'Inbound OCT/AFT not found.')
        return row

    def _assert_visible(self, row: OctinInbound, actor: str, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if row.userid and row.userid != actor:
            raise OctinError('octin_forbidden', 'Cannot view another customer card credit.')
        if not row.userid and row.status != IN_UNMATCHED:
            raise OctinError('octin_forbidden', 'Cannot view this card credit.')

    def assign(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        customer_id: str,
        account: Any,
        nickname: str = '',
    ) -> OctinInbound:
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_UNMATCHED:
            raise OctinError('not_assignable', 'Only unmatched card credits can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise OctinError('missing_customer_id', 'Customer id is required to assign.')
        dest = normalize_account(account)
        self._assert_internal_account(owner, dest)
        enrollment = self.store.find_key_by_fingerprint(row.fingerprint)
        if enrollment is None or enrollment.status == KEY_CANCELLED:
            enrollment = self.add_key(
                owner_userid=owner,
                actor=actor,
                actor_type=actor_type,
                nickname=nickname or ('Card ' + last4(row.pan)),
                legal_name=row.sender_name if len(row.sender_name) >= 2 else 'Cardholder',
                pan=row.pan,
                expiry=row.expiry or '1299',
                default_account=dest,
            )
            refreshed = self.store.get_inbound(row.inbound_id)
            if refreshed is not None and refreshed.status != IN_UNMATCHED:
                return refreshed
            row = refreshed or row
        elif enrollment.userid != owner:
            raise OctinError('octin_forbidden', 'PAN is enrolled to a different customer.')
        row.userid = owner
        row.destination_account = dest
        row.key_id = enrollment.key_id
        row.nickname = enrollment.nickname
        row.actor = str(actor)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=True)

    def claim(
        self,
        *,
        actor: str,
        actor_type: str,
        pan: Any = '',
        last4_value: Any = '',
        brand: Any = '',
        destination_account: Any = '',
        inbound_id: str = '',
        nickname: str = '',
    ) -> OctinInbound:
        self._require_claim(actor_type)
        if actor_type in EMPLOYEE_ROLES:
            raise OctinError('octin_forbidden', 'Staff must assign unmatched credits.')
        candidates: List[OctinInbound]
        if inbound_id:
            row = self._get(inbound_id=inbound_id)
            if row.status != IN_UNMATCHED:
                raise OctinError('not_claimable', 'Only unmatched card credits can be claimed.')
            candidates = [row]
        elif pan:
            card = normalize_pan(pan)
            fingerprint = pan_fingerprint(card)
            candidates = [
                item for item in self.store.list_inbounds(status=IN_UNMATCHED)
                if item.fingerprint == fingerprint
            ]
        else:
            digits = ''.join(ch for ch in str(last4_value or '') if ch.isdigit())
            if len(digits) != 4:
                raise OctinError('invalid_pan', 'Claim requires a PAN or last4 + brand.')
            wanted_brand = str(brand or '').strip().lower()
            if wanted_brand in {'mc', 'mastercard_send'}:
                wanted_brand = BRAND_MC
            if wanted_brand in {'visa_direct', 'visa'}:
                wanted_brand = BRAND_VISA
            if wanted_brand not in {BRAND_VISA, BRAND_MC}:
                raise OctinError('invalid_brand', 'Brand must be visa or mastercard.')
            candidates = [
                item for item in self.store.list_inbounds(status=IN_UNMATCHED)
                if last4(item.pan) == digits and item.brand == wanted_brand
            ]
        if not candidates:
            raise OctinError('inbound_not_found', 'No unmatched card credit matches that PAN.')
        if len(candidates) > 1 and not inbound_id:
            raise OctinError('not_claimable', 'Multiple unmatched credits match; provide inbound_id.')
        row = candidates[0]
        enrollment = self.store.find_key_by_fingerprint(row.fingerprint)
        if enrollment is not None and enrollment.status != KEY_CANCELLED and enrollment.userid != actor:
            raise OctinError('octin_forbidden', 'PAN is enrolled to a different customer.')
        dest = ''
        if destination_account not in (None, ''):
            dest = normalize_account(destination_account)
            self._assert_internal_account(actor, dest)
        if enrollment is not None and enrollment.status == KEY_ACTIVE:
            dest = dest or enrollment.default_account
        else:
            if not dest:
                raise OctinError('invalid_account', 'Destination account is required to claim.')
            if enrollment is None or enrollment.status == KEY_CANCELLED:
                card = row.pan
                enrollment = self.add_key(
                    owner_userid=actor,
                    actor=actor,
                    actor_type=actor_type,
                    nickname=nickname or ('Card ' + last4(card)),
                    legal_name='Cardholder',
                    pan=card,
                    expiry=row.expiry or '1299',
                    default_account=dest,
                )
                refreshed = self.store.get_inbound(row.inbound_id)
                if refreshed is not None and refreshed.status != IN_UNMATCHED:
                    return refreshed
                row = refreshed or row
        row.userid = actor
        row.destination_account = dest
        row.key_id = enrollment.key_id
        row.nickname = enrollment.nickname
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=True)

    def override_ofac(self, *, inbound_id: str, actor: str, actor_type: str) -> OctinInbound:
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_HELD:
            raise OctinError('not_overridable', 'Only OFAC-held credits can be overridden.')
        row.ofac_overridden = 1
        row.ofac_hit = 0
        row.actor = str(actor)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def release(self, *, inbound_id: str, actor: str, actor_type: str) -> OctinInbound:
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_PENDING:
            raise OctinError('not_releasable', 'Only pending dual-control credits can be released.')
        if row.actor and row.actor == str(actor):
            raise OctinError('same_approver', 'A different employee must release dual-control.')
        row.releaser = str(actor)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def reject(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> OctinInbound:
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status not in REJECTABLE:
            raise OctinError('not_rejectable', 'This card credit cannot be rejected.')
        row.status = IN_REJECTED
        row.reason = 'rejected'
        row.actor = str(actor)
        row.note = normalize_note(note) or row.note
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return row

    def return_inbound(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        staff: bool = False,
        note: str = '',
    ) -> OctinInbound:
        self._require_enabled()
        row = self._get(inbound_id=inbound_id)
        self._assert_visible(row, actor, actor_type)
        if row.status not in RETURNABLE:
            raise OctinError('not_returnable', 'Only posted card credits can be returned.')
        now = self._now()
        if not staff:
            if actor_type in EMPLOYEE_ROLES:
                raise OctinError('octin_forbidden', 'Staff must use the staff return path.')
            if actor != row.userid:
                raise OctinError('octin_forbidden', 'Cannot return another customer card credit.')
            if not row.posted_at or (now - row.posted_at) > self.policy.customer_return_seconds:
                raise OctinError('return_window_closed', 'Customer return window is 24 hours.')
        else:
            self._require_staff(actor_type)
            if not row.posted_at or (now - row.posted_at) > self.policy.staff_return_seconds:
                raise OctinError('return_window_closed', 'Staff return window is 90 days.')
        if self.debit_fn is None:
            raise OctinError('return_failed', 'Debit function is not configured.')
        remark = '%s return %s' % (row.rail, row.rrn[-12:] if row.rrn else row.inbound_id[-12:])
        result = self.debit_fn(row.destination_account, row.amount, remark)
        classified = _classify_money_result(result)
        if classified == 'nsf':
            raise OctinError('nsf', 'Return debit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            raise OctinError('return_failed', 'Return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.reason = 'returned'
        row.returned_at = now
        row.updated_at = now
        row.actor = str(actor)
        row.note = normalize_note(note) or row.note
        self.store.update_inbound(row)
        return row

    def export_oct1(self, *, inbound_id: str, actor_type: str) -> str:
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status == IN_UNMATCHED and not row.userid:
            raise OctinError('not_exportable', 'Unmatched credits cannot be exported until assigned.')
        return compose_oct1(
            mti=row.mti,
            pan=mask_pan(row.pan),
            expiry=mask_expiry(row.expiry) if row.expiry else '',
            amount=row.amount,
            mcc=row.mcc,
            processing_code=row.processing_code,
            rrn=mask_rrn(row.rrn),
            stan=mask_stan(row.stan),
            sender=row.sender_name,
            rail=row.rail,
            status=row.status,
        )

    def run_due(self, *, actor: str, actor_type: str) -> Dict[str, int]:
        self._require_staff(actor_type)
        _ = actor
        expired = 0
        posted = 0
        now = self._now()
        for row in self.store.list_inbounds():
            if row.status == IN_UNMATCHED and row.expiry_at and now > row.expiry_at:
                self._evaluate(row, rescreen=False)
                expired += 1
            elif row.status == IN_PENDING and row.releaser:
                self._evaluate(row, rescreen=False)
                posted += 1
        return {'expired': expired, 'posted': posted}

    def snapshot(self, userid: str, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self._require_enabled()
        _ = actor
        keys = [row.to_dict() for row in self.store.list_keys(userid)]
        inbounds = [row.to_dict() for row in self.store.list_inbounds(userid)]
        if actor_type in EMPLOYEE_ROLES:
            unmatched = [row.to_dict() for row in self.store.list_inbounds(status=IN_UNMATCHED)]
        else:
            unmatched = []
        posted = [row for row in self.store.list_inbounds(userid) if row.status == IN_POSTED]
        ytd = sum((parse_money(row.amount) for row in posted), Decimal('0.00'))
        returned = [row for row in self.store.list_inbounds(userid) if row.status == IN_RETURNED]
        returned_ytd = sum((parse_money(row.amount) for row in returned), Decimal('0.00'))
        return {
            'enabled': True,
            'keys': keys,
            'inbounds': inbounds,
            'unmatched': unmatched,
            'clock': self.calendar.snapshot(self._now()),
            'ytd_posted': money_str(ytd),
            'returned_ytd': money_str(returned_ytd),
            'active_count': sum(1 for row in keys if row['active']),
            'open_count': sum(1 for row in inbounds if row['status'] in OPEN_IN),
            'posted_count': len(posted),
        }


_SERVICE: Optional[OctinService] = None


def set_service(service: Optional[OctinService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[OctinService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('OCTIN_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryOctinStore()
    path = os.environ.get('OCTIN_DB', DEFAULT_STORE_PATH)
    return SqliteOctinStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[CardNetworkClock] = None,
) -> OctinService:
    if store is None:
        store = default_store()
    return OctinService(
        OctinPolicy.from_env(),
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
        'inbound_limit': 409,
        'already_cancelled': 409,
        'already_paused': 409,
        'already_active': 409,
        'nsf': 409,
        'failed': 409,
        'return_failed': 409,
        'octin_forbidden': 403,
        'octin_disabled': 403,
        'credit_not_allowed': 403,
        'same_approver': 403,
        'not_claimable': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_returnable': 403,
        'not_exportable': 403,
        'return_window_closed': 403,
        'key_not_found': 404,
        'inbound_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_pan': 400,
        'invalid_brand': 400,
        'invalid_expiry': 400,
        'invalid_mti': 400,
        'invalid_mcc': 400,
        'invalid_rrn': 400,
        'invalid_stan': 400,
        'invalid_rail': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_file': 400,
        'invalid_processing_code': 400,
        'oct_amount_exceeded': 400,
        'aft_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_key': 400,
        'missing_inbound': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: OctinError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('inbound') is not None:
        body['inbound'] = exc.extra['inbound'].to_dict()
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
    except OctinError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snap(service: OctinService, owner: str, userid: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(owner, actor=userid, actor_type=actor_type)


def handle_list(service: OctinService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'CardRecv': _snap(service, owner, userid, actor_type)}), 200


def handle_unmatched(service: OctinService):
    userid, error = _require_session_user()
    if error:
        return error

    def inner():
        actor_type = session.get('usertype') or 'customer'
        service._require_staff(actor_type)
        rows = [row.to_dict() for row in service.store.list_inbounds(status=IN_UNMATCHED)]
        return jsonify({'unmatched': rows, 'CardRecv': _snap(service, userid, userid, actor_type)}), 200

    return _handle_errors(inner)


def handle_add_key(service: OctinService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if actor_type in EMPLOYEE_ROLES and not owner:
        return jsonify({'message': 'Customer id is required', 'error': 'missing_customer_id'}), 400

    def inner():
        row = service.add_key(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname'),
            legal_name=values.get('legal_name') or values.get('name') or 'Cardholder',
            pan=values.get('pan'),
            expiry=values.get('expiry'),
            default_account=values.get('default_account') or values.get('destination_account'),
        )
        return jsonify({
            'message': 'Card enrolled',
            'key': row.to_dict(),
            'CardRecv': _snap(service, owner, userid, actor_type),
        }), 201

    return _handle_errors(inner)


def _key_status_route(service: OctinService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    key_id = str(values.get('key_id') or '').strip()
    if not key_id:
        return jsonify({'message': 'Key is required', 'error': 'missing_key'}), 400

    def inner():
        row = service.set_key_status(key_id=key_id, actor=userid, actor_type=actor_type, status=status)
        return jsonify({
            'message': ok_message,
            'key': row.to_dict(),
            'CardRecv': _snap(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(inner)


def handle_preview(service: OctinService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def inner():
        preview = service.preview(
            inbound_id=str(values.get('inbound_id') or ''),
            rrn=str(values.get('rrn') or ''),
            actor=userid,
            actor_type=actor_type,
        )
        owner = preview['inbound'].get('userid') or userid
        return jsonify({'preview': preview, 'CardRecv': _snap(service, owner, userid, actor_type)}), 200

    return _handle_errors(inner)


def handle_ingest(service: OctinService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def inner():
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'Card credit ingested' if created else 'Card credit already posted',
            'inbound': row.to_dict(),
            'CardRecv': _snap(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(inner)


def handle_ingest_file(service: OctinService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    payload = values.get('file') or values.get('payload') or values.get('oct1')
    if not payload:
        return jsonify({'message': 'OCT1 file is required', 'error': 'missing_file'}), 400

    def inner():
        rows, created = service.ingest_file(
            actor=userid,
            actor_type=actor_type,
            payload=str(payload),
            sender_name=str(values.get('sender_name') or ''),
        )
        return jsonify({
            'message': 'OCT1 ingested',
            'created': created,
            'count': len(rows),
            'inbounds': [row.to_dict() for row in rows],
            'CardRecv': _snap(service, userid, userid, actor_type),
        }), 201

    return _handle_errors(inner)


def handle_assign(service: OctinService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    inbound_id = str(values.get('inbound_id') or '').strip()
    if not inbound_id:
        return jsonify({'message': 'Inbound id is required', 'error': 'missing_inbound'}), 400

    def inner():
        row = service.assign(
            inbound_id=inbound_id,
            actor=userid,
            actor_type=actor_type,
            customer_id=str(values.get('customer_id') or ''),
            account=values.get('destination_account') or values.get('account') or values.get('default_account'),
            nickname=str(values.get('nickname') or ''),
        )
        return jsonify({
            'message': 'Card credit assigned',
            'inbound': row.to_dict(),
            'CardRecv': _snap(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(inner)


def handle_claim(service: OctinService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def inner():
        row = service.claim(
            actor=userid,
            actor_type=actor_type,
            pan=values.get('pan'),
            last4_value=values.get('last4'),
            brand=values.get('brand'),
            destination_account=values.get('destination_account') or values.get('default_account'),
            inbound_id=str(values.get('inbound_id') or ''),
            nickname=str(values.get('nickname') or ''),
        )
        return jsonify({
            'message': 'Card credit claimed',
            'inbound': row.to_dict(),
            'CardRecv': _snap(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(inner)


def _staff_inbound_route(service: OctinService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    inbound_id = str(values.get('inbound_id') or '').strip()
    if not inbound_id:
        return jsonify({'message': 'Inbound id is required', 'error': 'missing_inbound'}), 400

    def inner():
        if action == 'override':
            row = service.override_ofac(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            message = 'OFAC overridden'
        elif action == 'release':
            row = service.release(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            message = 'Card credit released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                note=str(values.get('note') or ''),
            )
            message = 'Card credit rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, staff=True,
                note=str(values.get('note') or ''),
            )
            message = 'Card credit returned'
        elif action == 'customer_return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, staff=False,
                note=str(values.get('note') or ''),
            )
            message = 'Card credit return requested'
        else:
            raise OctinError('invalid_status', 'Unknown staff action.')
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'CardRecv': _snap(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(inner)


def handle_export(service: OctinService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    inbound_id = str(values.get('inbound_id') or '').strip()
    if not inbound_id:
        return jsonify({'message': 'Inbound id is required', 'error': 'missing_inbound'}), 400

    def inner():
        line = service.export_oct1(inbound_id=inbound_id, actor_type=actor_type)
        row = service._get(inbound_id=inbound_id)
        return jsonify({
            'message': 'OCT1 exported',
            'oct1': line,
            'inbound': row.to_dict(),
            'CardRecv': _snap(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(inner)


def handle_run_due(service: OctinService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'

    def inner():
        counts = service.run_due(actor=userid, actor_type=actor_type)
        return jsonify({'message': 'Due card credits processed', 'counts': counts,
                        'CardRecv': _snap(service, userid, userid, actor_type)}), 200

    return _handle_errors(inner)


def attach_octin_routes(app, service: OctinService) -> None:
    @app.route('/listCardRecv', methods=['POST', 'GET'])
    def list_card_recv_route():
        return handle_list(service)

    @app.route('/listCardRecvKeys', methods=['POST', 'GET'])
    def list_card_recv_keys_route():
        return handle_list(service)

    @app.route('/listUnmatchedCardRecv', methods=['POST', 'GET'])
    def list_unmatched_card_recv_route():
        return handle_unmatched(service)

    @app.route('/addCardRecvKey', methods=['POST', 'GET'])
    def add_card_recv_key_route():
        return handle_add_key(service)

    @app.route('/pauseCardRecvKey', methods=['POST', 'GET'])
    def pause_card_recv_key_route():
        return _key_status_route(service, KEY_PAUSED, 'Card paused')

    @app.route('/resumeCardRecvKey', methods=['POST', 'GET'])
    def resume_card_recv_key_route():
        return _key_status_route(service, KEY_ACTIVE, 'Card resumed')

    @app.route('/cancelCardRecvKey', methods=['POST', 'GET'])
    def cancel_card_recv_key_route():
        return _key_status_route(service, KEY_CANCELLED, 'Card cancelled')

    @app.route('/previewCardRecv', methods=['POST', 'GET'])
    def preview_card_recv_route():
        return handle_preview(service)

    @app.route('/ingestCardRecv', methods=['POST', 'GET'])
    def ingest_card_recv_route():
        return handle_ingest(service)

    @app.route('/ingestCardRecvFile', methods=['POST', 'GET'])
    def ingest_card_recv_file_route():
        return handle_ingest_file(service)

    @app.route('/claimCardRecv', methods=['POST', 'GET'])
    def claim_card_recv_route():
        return handle_claim(service)

    @app.route('/assignCardRecv', methods=['POST', 'GET'])
    def assign_card_recv_route():
        return handle_assign(service)

    @app.route('/overrideCardRecvOfac', methods=['POST', 'GET'])
    def override_card_recv_ofac_route():
        return _staff_inbound_route(service, 'override')

    @app.route('/releaseCardRecv', methods=['POST', 'GET'])
    def release_card_recv_route():
        return _staff_inbound_route(service, 'release')

    @app.route('/rejectCardRecv', methods=['POST', 'GET'])
    def reject_card_recv_route():
        return _staff_inbound_route(service, 'reject')

    @app.route('/returnCardRecv', methods=['POST', 'GET'])
    def return_card_recv_route():
        return _staff_inbound_route(service, 'return')

    @app.route('/requestCardRecvReturn', methods=['POST', 'GET'])
    def request_card_recv_return_route():
        return _staff_inbound_route(service, 'customer_return')

    @app.route('/exportCardRecv', methods=['POST', 'GET'])
    def export_card_recv_route():
        return handle_export(service)

    @app.route('/runDueCardRecv', methods=['POST', 'GET'])
    def run_due_card_recv_route():
        return handle_run_due(service)
