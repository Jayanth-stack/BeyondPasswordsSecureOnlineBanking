"""Issuer ACS for EMV 3-D Secure 2.x (card-not-present step-up).

Staff ingest incoming Directory Server AReq messages. Recipients are
enrolled PANs (Luhn + IIN brand). The ACS decides frictionless (Y),
challenge (C), attempt (A), or reject (N/R/U), then issues CAVV + ECI
on success. Independent of card authorization holds (PR #149, ISO 8583
1100), card settlement presentment (PR #151, ISO 8583 1240 / IPM1),
chargeback representment (PR #109), outbound Fedwire (PR #73), and ACH
linking (PR #68). Existing `/fundTransfer`, `/withdrawAmount`, and
`/sendWire` stay unchanged. 3DS authenticates; it does not debit the
ledger (liability shift only).

Foundations (reusable beyond this screen):
- PAN Luhn check digit + IIN brand
- PAN fingerprint (SHA-256; never in snapshots)
- EMV 3DS protocol version 2.1.0 / 2.2.0
- Message types AReq / ARes / CReq / CRes / RReq / RRes
- Device channel APP / BRW / 3RI and message category PA / NPA
- transStatus Y/N/C/R/U/A and brand ECI (Visa 05/06/07, Mastercard 02/01/00)
- CAVV / AAV (28-char; never in snapshots)
- DS / ACS / 3DS Server trans IDs (UUID)
- 3DS Method completion gate for browser frictionless
- ACS clock (24/7; 10-minute challenge expiry — not a Fed settlement cutoff)
- 3DS1 pipe compose/parse (XML/DOCTYPE rejected)
- Dual-control at $10k, OFAC on merchant name
- 24h unmatched claim / 24h customer reverse / 90-day staff reverse

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Full PAN, fingerprint, CAVV, ACS code, and trans IDs never appear in to_dict.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
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
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

KEY_ACTIVE = 'active'
KEY_PAUSED = 'paused'
KEY_CANCELLED = 'cancelled'
KEY_STATUSES = frozenset({KEY_ACTIVE, KEY_PAUSED, KEY_CANCELLED})
OPEN_KEYS = frozenset({KEY_ACTIVE, KEY_PAUSED})

AUTH_UNMATCHED = 'unmatched'
AUTH_HELD = 'held'
AUTH_PENDING = 'pending_release'
AUTH_CHALLENGE = 'challenge'
AUTH_AUTHENTICATED = 'authenticated'
AUTH_ATTEMPTED = 'attempted'
AUTH_REJECTED = 'rejected'
AUTH_EXPIRED = 'expired'
AUTH_REVERSED = 'reversed'
AUTH_FAILED = 'failed'
AUTH_STATUSES = frozenset({
    AUTH_UNMATCHED, AUTH_HELD, AUTH_PENDING, AUTH_CHALLENGE, AUTH_AUTHENTICATED,
    AUTH_ATTEMPTED, AUTH_REJECTED, AUTH_EXPIRED, AUTH_REVERSED, AUTH_FAILED,
})
OPEN_AUTHS = frozenset({AUTH_UNMATCHED, AUTH_HELD, AUTH_PENDING, AUTH_CHALLENGE})
RELEASABLE = frozenset({AUTH_PENDING, AUTH_HELD})
REJECTABLE = frozenset({AUTH_UNMATCHED, AUTH_HELD, AUTH_PENDING, AUTH_CHALLENGE})
CHALLENGEABLE = frozenset({AUTH_CHALLENGE})
REVERSIBLE = frozenset({AUTH_AUTHENTICATED, AUTH_ATTEMPTED})

MSG_AREQ = 'AReq'
MSG_ARES = 'ARes'
MSG_CREQ = 'CReq'
MSG_CRES = 'CRes'
MSG_RREQ = 'RReq'
MSG_RRES = 'RRes'
MESSAGE_TYPES = frozenset({MSG_AREQ, MSG_ARES, MSG_CREQ, MSG_CRES, MSG_RREQ, MSG_RRES})
MESSAGE_ALIASES = {
    'areq': MSG_AREQ, 'a_req': MSG_AREQ, 'authenticationrequest': MSG_AREQ,
    'ares': MSG_ARES, 'a_res': MSG_ARES, 'authenticationresponse': MSG_ARES,
    'creq': MSG_CREQ, 'c_req': MSG_CREQ, 'challengerequest': MSG_CREQ,
    'cres': MSG_CRES, 'c_res': MSG_CRES, 'challengeresponse': MSG_CRES,
    'rreq': MSG_RREQ, 'r_req': MSG_RREQ, 'resultsrequest': MSG_RREQ,
    'rres': MSG_RRES, 'r_res': MSG_RRES, 'resultsresponse': MSG_RRES,
}

VERSION_210 = '2.1.0'
VERSION_220 = '2.2.0'
PROTOCOL_VERSIONS = frozenset({VERSION_210, VERSION_220})
VERSION_ALIASES = {
    '2.1': VERSION_210, '2.1.0': VERSION_210, '210': VERSION_210,
    '2.2': VERSION_220, '2.2.0': VERSION_220, '220': VERSION_220,
}

CH_APP = 'app'
CH_BRW = 'brw'
CH_3RI = '3ri'
DEVICE_CHANNELS = frozenset({CH_APP, CH_BRW, CH_3RI})
CHANNEL_ALIASES = {
    'app': CH_APP, '01': CH_APP, '1': CH_APP, 'application': CH_APP,
    'brw': CH_BRW, '02': CH_BRW, '2': CH_BRW, 'browser': CH_BRW, 'web': CH_BRW,
    '3ri': CH_3RI, '03': CH_3RI, '3': CH_3RI, 'ri': CH_3RI, 'merchant': CH_3RI,
}

CAT_PA = 'pa'
CAT_NPA = 'npa'
CATEGORIES = frozenset({CAT_PA, CAT_NPA})
CATEGORY_ALIASES = {
    'pa': CAT_PA, '01': CAT_PA, '1': CAT_PA, 'payment': CAT_PA,
    'npa': CAT_NPA, '02': CAT_NPA, '2': CAT_NPA, 'nonpayment': CAT_NPA, 'non_payment': CAT_NPA,
}

TS_Y = 'Y'
TS_N = 'N'
TS_C = 'C'
TS_R = 'R'
TS_U = 'U'
TS_A = 'A'
TRANS_STATUSES = frozenset({TS_Y, TS_N, TS_C, TS_R, TS_U, TS_A})

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
    'retail': 'retail', 'purchase': 'retail', 'pos': 'retail', 'ecommerce': 'retail',
    'fuel': 'fuel', 'gas': 'fuel', 'petrol': 'fuel',
    'grocery': 'grocery', 'supermarket': 'grocery',
    'restaurant': 'restaurant', 'dining': 'restaurant',
    'hotel': 'lodging', 'lodging': 'lodging',
    'auto': 'auto', 'rental': 'auto', 'car': 'auto',
    'other': 'other',
}
PURPOSES = frozenset(PURPOSE_ALIASES.values())

MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/acs.sqlite'
CUSTOMER_REVERSE_HOURS = 24
STAFF_REVERSE_DAYS = 90
UNMATCHED_HOURS = 24
CHALLENGE_SECONDS = 600
MAX_CHALLENGE_ATTEMPTS = 3
CAVV_LEN = 28


class AcsError(ValueError):
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
        raise AcsError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_merchant(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise AcsError('invalid_name', 'Merchant name must be 2-80 characters.')
    return text


def normalize_currency(value: Any, *, default: str = 'USD') -> str:
    text = ''.join(ch for ch in str(value or default).upper() if ch.isalpha())
    if text != 'USD':
        raise AcsError('invalid_currency', 'Card 3DS authentications are USD.')
    return text


def normalize_purpose(value: Any, *, default: str = 'retail') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise AcsError('invalid_purpose', 'Unknown 3DS purpose.')
    return text


def normalize_mcc(value: Any, *, default: str = '5999') -> str:
    digits = ''.join(ch for ch in str(value if value not in (None, '') else default) if ch.isdigit())
    if len(digits) == 3:
        digits = digits.zfill(4)
    if len(digits) != 4:
        raise AcsError('invalid_mcc', 'MCC must be four digits.')
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
        raise AcsError('invalid_pan', 'PAN is required.')
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
    raise AcsError('invalid_pan', 'Unsupported card brand.')


def normalize_brand(value: Any) -> str:
    text = ''.join(ch for ch in str(value or '').lower() if ch.isalnum() or ch == '_')
    brand = BRAND_ALIASES.get(text)
    if brand is None:
        raise AcsError('invalid_brand', 'Brand must be visa, mastercard, amex, or discover.')
    return brand


def normalize_pan(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not (13 <= len(digits) <= 19):
        raise AcsError('invalid_pan', 'PAN must be 13-19 digits.')
    if digits == '0' * len(digits) or not luhn_check_digit_ok(digits):
        raise AcsError('invalid_pan', 'PAN failed Luhn checksum.')
    brand = brand_from_iin(digits)
    if brand == 'amex' and len(digits) != 15:
        raise AcsError('invalid_pan', 'American Express PANs must be 15 digits.')
    if brand == 'mastercard' and len(digits) != 16:
        raise AcsError('invalid_pan', 'Mastercard PANs must be 16 digits.')
    if brand == 'discover' and len(digits) not in {16, 19}:
        raise AcsError('invalid_pan', 'Discover PANs must be 16 or 19 digits.')
    return digits


def mask_pan(pan: str) -> str:
    digits = ''.join(ch for ch in str(pan or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return ('*' * max(0, len(digits) - 4)) + digits[-4:]


def pan_fingerprint(pan: str) -> str:
    digits = ''.join(ch for ch in str(pan or '') if ch.isdigit())
    return hashlib.sha256(digits.encode('ascii')).hexdigest()


def normalize_protocol_version(value: Any, *, default: str = VERSION_220) -> str:
    text = str(value if value not in (None, '') else default).strip().lower()
    mapped = VERSION_ALIASES.get(text) or VERSION_ALIASES.get(text.replace('_', '.'))
    if mapped is None:
        digits = ''.join(ch for ch in text if ch.isdigit())
        mapped = VERSION_ALIASES.get(digits)
    if mapped is None:
        raise AcsError('invalid_version', 'Protocol version must be 2.1.0 or 2.2.0.')
    return mapped


def normalize_message_type(value: Any, *, default: str = MSG_AREQ) -> str:
    text = str(value if value not in (None, '') else default).strip()
    mapped = MESSAGE_ALIASES.get(text.lower().replace('-', '').replace('_', '').replace(' ', ''))
    if mapped is None:
        mapped = text if text in MESSAGE_TYPES else None
    if mapped is None:
        raise AcsError('invalid_message_type', 'Unknown EMV 3DS message type.')
    return mapped


def normalize_channel(value: Any, *, default: str = CH_BRW) -> str:
    text = str(value if value not in (None, '') else default).strip().lower().replace('-', '').replace(' ', '')
    mapped = CHANNEL_ALIASES.get(text)
    if mapped is None:
        raise AcsError('invalid_channel', 'Device channel must be APP, BRW, or 3RI.')
    return mapped


def normalize_category(value: Any, *, default: str = CAT_PA) -> str:
    text = str(value if value not in (None, '') else default).strip().lower().replace('-', '').replace(' ', '_')
    mapped = CATEGORY_ALIASES.get(text)
    if mapped is None:
        raise AcsError('invalid_category', 'Message category must be PA or NPA.')
    return mapped


def normalize_trans_status(value: Any) -> str:
    text = str(value or '').strip().upper()
    if text not in TRANS_STATUSES:
        raise AcsError('invalid_status', 'transStatus must be Y, N, C, R, U, or A.')
    return text


def normalize_uuid(value: Any, *, field: str = 'ds_trans_id') -> str:
    text = str(value or '').strip().lower().replace('urn:uuid:', '')
    hexed = ''.join(ch for ch in text if ch in '0123456789abcdef')
    if len(hexed) != 32:
        raise AcsError('invalid_reference', '%s must be a UUID.' % field)
    if hexed == '0' * 32:
        raise AcsError('invalid_reference', '%s cannot be zero.' % field)
    return '%s-%s-%s-%s-%s' % (hexed[0:8], hexed[8:12], hexed[12:16], hexed[16:20], hexed[20:32])


def compose_uuid() -> str:
    return str(uuid.uuid4())


def mask_uuid(value: str) -> str:
    hexed = ''.join(ch for ch in str(value or '') if ch in '0123456789abcdefABCDEF')
    if len(hexed) < 4:
        return '****'
    return ('*' * 32) + hexed[-4:].lower()


def eci_for(brand: str, trans_status: str) -> str:
    """Electronic Commerce Indicator. Visa-family 05/06/07; Mastercard 02/01/00."""
    if brand == 'mastercard':
        if trans_status == TS_Y:
            return '02'
        if trans_status == TS_A:
            return '01'
        return '00'
    if trans_status == TS_Y:
        return '05'
    if trans_status == TS_A:
        return '06'
    return '07'


def compose_cavv(*, brand: str, acs_trans_id: str, amount: str, at: float) -> str:
    """28-character CAVV/AAV. Distinct from ISO 8583 approval codes and ARNs."""
    material = ('%s|%s|%s|%s' % (brand, acs_trans_id, amount, int(at))).encode('ascii')
    digest = hashlib.sha256(material).digest()[:20]
    token = base64.b64encode(digest).decode('ascii').rstrip('=')
    token = ''.join(ch for ch in token if ch.isalnum())[:CAVV_LEN]
    return token.ljust(CAVV_LEN, 'A')


def normalize_cavv(value: Any) -> str:
    text = ''.join(ch for ch in str(value or '') if ch.isalnum())
    if len(text) != CAVV_LEN:
        raise AcsError('invalid_cavv', 'CAVV must be 28 characters.')
    return text


def mask_cavv(value: str) -> str:
    text = ''.join(ch for ch in str(value or '') if ch.isalnum())
    if len(text) < 4:
        return '****'
    return ('*' * (len(text) - 4)) + text[-4:]


def compose_acs_code(seed: str) -> str:
    digest = hashlib.sha256(str(seed).encode('utf-8')).hexdigest()
    number = int(digest[:8], 16) % 1000000
    if number == 0:
        number = 1
    return '%06d' % number


def hash_acs_code(code: str) -> str:
    digits = ''.join(ch for ch in str(code or '') if ch.isdigit())
    return hashlib.sha256(digits.encode('ascii')).hexdigest()


def normalize_acs_code(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 6 or digits == '000000':
        raise AcsError('invalid_code', 'ACS challenge code must be six digits.')
    return digits


def codes_match(presented: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_acs_code(presented), stored_hash or '')


def compose_3ds1(
    *,
    ds_trans_id: str,
    message_type: str,
    version: str,
    channel: str,
    category: str,
    pan_masked: str,
    amount: str,
    merchant: str,
    mcc: str,
    trans_status: str = '',
    eci: str = '',
    currency: str = 'USD',
) -> str:
    if '|' in merchant or '\n' in merchant:
        raise AcsError('invalid_file', 'Merchant cannot contain pipe or newline.')
    return '3DS1|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s' % (
        mask_uuid(ds_trans_id), message_type, version, channel.upper(), category.upper(),
        pan_masked, amount, merchant, mcc, trans_status, eci, currency,
    )


def parse_3ds1(line: Any) -> Dict[str, str]:
    text = str(line or '').strip()
    if not text:
        raise AcsError('invalid_file', 'Empty 3DS1 line.')
    upper = text.upper()
    if '<?XML' in upper or '<!DOCTYPE' in upper or '<' in text:
        raise AcsError('invalid_file', 'XML/DOCTYPE is rejected.')
    parts = text.split('|')
    if len(parts) < 10 or parts[0].upper() != '3DS1':
        raise AcsError(
            'invalid_file',
            '3DS1 line must be 3DS1|DS|MSG|VER|CH|CAT|PAN|AMOUNT|MERCHANT|MCC|STATUS|ECI|CURRENCY.',
        )
    while len(parts) < 13:
        parts.append('USD' if len(parts) == 12 else '')
    return {
        'ds_trans_id': parts[1],
        'message_type': parts[2],
        'version': parts[3],
        'channel': parts[4],
        'category': parts[5],
        'pan': parts[6],
        'amount': parts[7],
        'merchant': parts[8],
        'mcc': parts[9],
        'trans_status': parts[10],
        'eci': parts[11],
        'currency': parts[12] or 'USD',
    }


def split_3ds1_file(payload: Any) -> List[str]:
    text = str(payload or '')
    if '<?xml' in text.lower() or '<!doctype' in text.lower():
        raise AcsError('invalid_file', 'XML/DOCTYPE is rejected.')
    lines = [line.strip() for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    return [line for line in lines if line]


class AcsClock:
    """EMV 3DS ACS clock. Authentication is 24/7; challenges expire in 10 minutes.

    Distinct from the Fed settlement-cycle clock (22:00 ET cutoff) and from
    MCC authorization-hold expiry windows.
    """

    def __init__(
        self,
        *,
        challenge_seconds: int = CHALLENGE_SECONDS,
        tz_offset_hours: int = -4,
    ) -> None:
        self.challenge_seconds = int(challenge_seconds)
        self.tz_offset_hours = int(tz_offset_hours)
        self.tz = timezone(timedelta(hours=self.tz_offset_hours))

    def local_dt(self, ts: float) -> datetime:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(self.tz)

    def should_queue(self, ts: float) -> bool:
        """3DS never queues on weekend/holiday. Always False."""
        return False

    def challenge_expired(self, started_at: float, now: float) -> bool:
        if not started_at:
            return False
        return float(now) - float(started_at) >= self.challenge_seconds

    def remaining_seconds(self, started_at: float, now: float) -> int:
        if not started_at:
            return self.challenge_seconds
        left = int(self.challenge_seconds - (float(now) - float(started_at)))
        return max(0, left)

    def snapshot(self, ts: float) -> Dict[str, Any]:
        local = self.local_dt(ts)
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'always_open': True,
            'challenge_seconds': self.challenge_seconds,
            'after_cutoff': False,
            'settlement_day': True,
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


@dataclass
class AcsPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_reverse: bool = True
    allow_credit: bool = False
    max_keys: int = 8
    max_auths: int = 240
    min_amount: Decimal = Decimal('0.50')
    max_amount: Decimal = Decimal('25000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    frictionless_cap: Decimal = Decimal('250.00')
    challenge_seconds: int = CHALLENGE_SECONDS
    max_attempts: int = MAX_CHALLENGE_ATTEMPTS
    tz_offset_hours: int = -4
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST

    @classmethod
    def from_env(cls) -> 'AcsPolicy':
        extra = _env_list('ACS_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('ACS_ENABLED', True),
            customer_manage=_env_bool('ACS_CUSTOMER_MANAGE', True),
            customer_reverse=_env_bool('ACS_CUSTOMER_REVERSE', True),
            allow_credit=_env_bool('ACS_ALLOW_CREDIT', False),
            max_keys=max(1, _env_int('ACS_MAX_KEYS', 8)),
            max_auths=max(1, _env_int('ACS_MAX', 240)),
            min_amount=_env_money('ACS_MIN_AMOUNT', '0.50'),
            max_amount=_env_money('ACS_MAX_AMOUNT', '25000.00'),
            dual_control_threshold=_env_money('ACS_DUAL_CONTROL', '10000.00'),
            frictionless_cap=_env_money('ACS_FRICTIONLESS_CAP', '250.00'),
            challenge_seconds=max(30, _env_int('ACS_CHALLENGE_SECONDS', CHALLENGE_SECONDS)),
            max_attempts=max(1, _env_int('ACS_MAX_ATTEMPTS', MAX_CHALLENGE_ATTEMPTS)),
            tz_offset_hours=_env_int('ACS_TZ_OFFSET', -4),
            watchlist=watch,
        )


@dataclass
class AcsKey:
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
class AcsAuth:
    auth_id: str
    ds_trans_id: str
    acs_trans_id: str
    three_ds_server_trans_id: str
    message_type: str
    protocol_version: str
    device_channel: str
    message_category: str
    key_id: str
    userid: str
    internal_account: str
    amount: str
    nickname: str
    merchant: str
    mcc: str
    purpose: str
    brand: str
    last4: str
    status: str
    trans_status: str
    trans_status_reason: str
    eci: str
    cavv: str
    acs_code_hash: str
    method_completed: int
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    created_at: float
    updated_at: float
    authenticated_at: float = 0.0
    reversed_at: float = 0.0
    challenge_at: float = 0.0
    attempts: int = 0
    note: str = ''
    reason: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'auth_id': self.auth_id,
            'ds_trans_id_masked': mask_uuid(self.ds_trans_id),
            'acs_trans_id_masked': mask_uuid(self.acs_trans_id),
            'message_type': self.message_type,
            'protocol_version': self.protocol_version,
            'device_channel': self.device_channel,
            'message_category': self.message_category,
            'key_id': self.key_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'amount': self.amount,
            'nickname': self.nickname,
            'merchant': self.merchant,
            'mcc': self.mcc,
            'purpose': self.purpose,
            'brand': self.brand,
            'pan_masked': ('*' * 12) + self.last4 if self.last4 else '****',
            'last4': self.last4,
            'status': self.status,
            'trans_status': self.trans_status,
            'trans_status_reason': self.trans_status_reason,
            'eci': self.eci,
            'cavv_masked': mask_cavv(self.cavv) if self.cavv else '',
            'method_completed': bool(self.method_completed),
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'authenticated_at': self.authenticated_at,
            'reversed_at': self.reversed_at,
            'challenge_at': self.challenge_at,
            'attempts': self.attempts,
            'note': self.note,
            'reason': self.reason,
            'authenticated': self.status == AUTH_AUTHENTICATED,
            'challenge': self.status == AUTH_CHALLENGE,
            'pending_release': self.status == AUTH_PENDING,
            'open': self.status in OPEN_AUTHS,
        }


def _clone_key(row: AcsKey) -> AcsKey:
    return AcsKey(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_auth(row: AcsAuth) -> AcsAuth:
    return AcsAuth(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _key_from_row(row: Any) -> AcsKey:
    return AcsKey(
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


def _auth_from_row(row: Any) -> AcsAuth:
    return AcsAuth(
        auth_id=row['auth_id'],
        ds_trans_id=row['ds_trans_id'],
        acs_trans_id=row['acs_trans_id'],
        three_ds_server_trans_id=row['three_ds_server_trans_id'] or '',
        message_type=row['message_type'],
        protocol_version=row['protocol_version'],
        device_channel=row['device_channel'],
        message_category=row['message_category'],
        key_id=row['key_id'] or '',
        userid=row['userid'] or '',
        internal_account=row['internal_account'] or '',
        amount=row['amount'],
        nickname=row['nickname'] or '',
        merchant=row['merchant'],
        mcc=row['mcc'],
        purpose=row['purpose'],
        brand=row['brand'] or '',
        last4=row['last4'] or '',
        status=row['status'],
        trans_status=row['trans_status'] or '',
        trans_status_reason=row['trans_status_reason'] or '',
        eci=row['eci'] or '',
        cavv=row['cavv'] or '',
        acs_code_hash=row['acs_code_hash'] or '',
        method_completed=int(row['method_completed'] or 0),
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        authenticated_at=float(row['authenticated_at'] or 0),
        reversed_at=float(row['reversed_at'] or 0),
        challenge_at=float(row['challenge_at'] or 0),
        attempts=int(row['attempts'] or 0),
        note=row['note'] or '',
        reason=row['reason'] or '',
    )


class MemoryAcsStore:
    def __init__(self) -> None:
        self._keys: Dict[str, AcsKey] = {}
        self._rows: Dict[str, AcsAuth] = {}
        self._by_ds: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_key(self, row: AcsKey) -> None:
        with self._lock:
            self._keys[row.key_id] = row

    def get_key(self, key_id: str) -> Optional[AcsKey]:
        with self._lock:
            row = self._keys.get(key_id)
            return _clone_key(row) if row else None

    def update_key(self, row: AcsKey) -> None:
        with self._lock:
            self._keys[row.key_id] = row

    def list_keys(self, userid: Optional[str] = None, *, include_cancelled: bool = True) -> List[AcsKey]:
        with self._lock:
            rows = [_clone_key(row) for row in self._keys.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_cancelled:
            rows = [row for row in rows if row.status != KEY_CANCELLED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_key_by_nickname(self, userid: str, nickname: str) -> Optional[AcsKey]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._keys.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_KEYS:
                    return _clone_key(row)
        return None

    def find_key_by_fingerprint(self, fingerprint: str, *, userid: Optional[str] = None) -> Optional[AcsKey]:
        with self._lock:
            rows = [
                row for row in self._keys.values()
                if row.fingerprint == fingerprint and row.status == KEY_ACTIVE
            ]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        rows.sort(key=lambda item: item.created_at)
        return _clone_key(rows[0]) if rows else None

    def put_auth(self, row: AcsAuth) -> AcsAuth:
        with self._lock:
            existing_id = self._by_ds.get(row.ds_trans_id)
            if existing_id is not None:
                return self._rows[existing_id]
            self._rows[row.auth_id] = row
            self._by_ds[row.ds_trans_id] = row.auth_id
            return row

    def update_auth(self, row: AcsAuth) -> None:
        with self._lock:
            self._rows[row.auth_id] = row
            self._by_ds[row.ds_trans_id] = row.auth_id

    def get_auth(self, auth_id: str) -> Optional[AcsAuth]:
        with self._lock:
            row = self._rows.get(auth_id)
            return _clone_auth(row) if row else None

    def get_auth_by_ds(self, ds_trans_id: str) -> Optional[AcsAuth]:
        with self._lock:
            auth_id = self._by_ds.get(ds_trans_id)
            return _clone_auth(self._rows[auth_id]) if auth_id else None

    def list_auths(self, userid: Optional[str] = None, *, unmatched_only: bool = False) -> List[AcsAuth]:
        with self._lock:
            rows = [_clone_auth(row) for row in self._rows.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if unmatched_only:
            rows = [row for row in rows if row.status == AUTH_UNMATCHED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def list_unmatched(self) -> List[AcsAuth]:
        return self.list_auths(unmatched_only=True)


class SqliteAcsStore:
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
                CREATE TABLE IF NOT EXISTS auths (
                    auth_id TEXT PRIMARY KEY,
                    ds_trans_id TEXT NOT NULL UNIQUE,
                    acs_trans_id TEXT NOT NULL,
                    three_ds_server_trans_id TEXT NOT NULL DEFAULT '',
                    message_type TEXT NOT NULL,
                    protocol_version TEXT NOT NULL,
                    device_channel TEXT NOT NULL,
                    message_category TEXT NOT NULL,
                    key_id TEXT NOT NULL DEFAULT '',
                    userid TEXT NOT NULL DEFAULT '',
                    internal_account TEXT NOT NULL DEFAULT '',
                    amount TEXT NOT NULL,
                    nickname TEXT NOT NULL DEFAULT '',
                    merchant TEXT NOT NULL,
                    mcc TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    brand TEXT NOT NULL DEFAULT '',
                    last4 TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    trans_status TEXT NOT NULL DEFAULT '',
                    trans_status_reason TEXT NOT NULL DEFAULT '',
                    eci TEXT NOT NULL DEFAULT '',
                    cavv TEXT NOT NULL DEFAULT '',
                    acs_code_hash TEXT NOT NULL DEFAULT '',
                    method_completed INTEGER NOT NULL DEFAULT 0,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    authenticated_at REAL NOT NULL DEFAULT 0,
                    reversed_at REAL NOT NULL DEFAULT 0,
                    challenge_at REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_key(self, row: AcsKey) -> None:
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

    def get_key(self, key_id: str) -> Optional[AcsKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM keys WHERE key_id = ?', (key_id,)).fetchone()
        return _key_from_row(row) if row else None

    def update_key(self, row: AcsKey) -> None:
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

    def list_keys(self, userid: Optional[str] = None, *, include_cancelled: bool = True) -> List[AcsKey]:
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

    def find_key_by_nickname(self, userid: str, nickname: str) -> Optional[AcsKey]:
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

    def find_key_by_fingerprint(self, fingerprint: str, *, userid: Optional[str] = None) -> Optional[AcsKey]:
        sql = "SELECT * FROM keys WHERE fingerprint = ? AND status = 'active'"
        params: List[Any] = [fingerprint]
        if userid is not None:
            sql += ' AND userid = ?'
            params.append(userid)
        sql += ' ORDER BY created_at ASC'
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return _key_from_row(row) if row else None

    def put_auth(self, row: AcsAuth) -> AcsAuth:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM auths WHERE ds_trans_id = ?', (row.ds_trans_id,),
            ).fetchone()
            if existing is not None:
                return _auth_from_row(existing)
            conn.execute(
                """
                INSERT INTO auths (
                    auth_id, ds_trans_id, acs_trans_id, three_ds_server_trans_id, message_type,
                    protocol_version, device_channel, message_category, key_id, userid,
                    internal_account, amount, nickname, merchant, mcc, purpose, brand, last4,
                    status, trans_status, trans_status_reason, eci, cavv, acs_code_hash,
                    method_completed, actor, releaser, ofac_hit, ofac_match, created_at,
                    updated_at, authenticated_at, reversed_at, challenge_at, attempts, note, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.auth_id, row.ds_trans_id, row.acs_trans_id, row.three_ds_server_trans_id,
                    row.message_type, row.protocol_version, row.device_channel, row.message_category,
                    row.key_id, row.userid, row.internal_account, row.amount, row.nickname,
                    row.merchant, row.mcc, row.purpose, row.brand, row.last4, row.status,
                    row.trans_status, row.trans_status_reason, row.eci, row.cavv, row.acs_code_hash,
                    row.method_completed, row.actor, row.releaser, row.ofac_hit, row.ofac_match,
                    row.created_at, row.updated_at, row.authenticated_at, row.reversed_at,
                    row.challenge_at, row.attempts, row.note, row.reason,
                ),
            )
            conn.commit()
            return row

    def update_auth(self, row: AcsAuth) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE auths SET key_id=?, userid=?, internal_account=?, amount=?, nickname=?,
                    merchant=?, mcc=?, purpose=?, brand=?, last4=?, status=?, trans_status=?,
                    trans_status_reason=?, eci=?, cavv=?, acs_code_hash=?, method_completed=?,
                    actor=?, releaser=?, ofac_hit=?, ofac_match=?, updated_at=?, authenticated_at=?,
                    reversed_at=?, challenge_at=?, attempts=?, note=?, reason=?, message_type=?
                WHERE auth_id=?
                """,
                (
                    row.key_id, row.userid, row.internal_account, row.amount, row.nickname,
                    row.merchant, row.mcc, row.purpose, row.brand, row.last4, row.status,
                    row.trans_status, row.trans_status_reason, row.eci, row.cavv, row.acs_code_hash,
                    row.method_completed, row.actor, row.releaser, row.ofac_hit, row.ofac_match,
                    row.updated_at, row.authenticated_at, row.reversed_at, row.challenge_at,
                    row.attempts, row.note, row.reason, row.message_type, row.auth_id,
                ),
            )
            conn.commit()

    def get_auth(self, auth_id: str) -> Optional[AcsAuth]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM auths WHERE auth_id = ?', (auth_id,)).fetchone()
        return _auth_from_row(row) if row else None

    def get_auth_by_ds(self, ds_trans_id: str) -> Optional[AcsAuth]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM auths WHERE ds_trans_id = ?', (ds_trans_id,),
            ).fetchone()
        return _auth_from_row(row) if row else None

    def list_auths(self, userid: Optional[str] = None, *, unmatched_only: bool = False) -> List[AcsAuth]:
        sql = 'SELECT * FROM auths'
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
        return [_auth_from_row(row) for row in rows]

    def list_unmatched(self) -> List[AcsAuth]:
        return self.list_auths(unmatched_only=True)


class AcsService:
    def __init__(
        self,
        policy: AcsPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[AcsClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or AcsClock(
            challenge_seconds=policy.challenge_seconds,
            tz_offset_hours=policy.tz_offset_hours,
        )

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise AcsError('acs_disabled', 'Card 3DS ACS is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise AcsError('acs_forbidden', 'Customers cannot manage 3DS keys.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise AcsError('acs_forbidden', 'Staff only.')

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
            raise AcsError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise AcsError('credit_not_allowed', 'Credit accounts cannot be 3DS destinations.')

    def _assert_amount(self, dollars: Decimal) -> None:
        if dollars < self.policy.min_amount or dollars > self.policy.max_amount:
            raise AcsError('amount_out_of_range', 'Amount is outside the allowed range.')

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
    ) -> AcsKey:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise AcsError('acs_forbidden', 'Not allowed to add keys for this customer.')
        name = normalize_nickname(nickname)
        digits = normalize_pan(pan)
        derived = brand_from_iin(digits)
        if brand not in (None, ''):
            wanted = normalize_brand(brand)
            if wanted != derived:
                raise AcsError('invalid_brand', 'Brand does not match PAN IIN.')
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        fingerprint = pan_fingerprint(digits)
        if self.store.find_key_by_nickname(owner_userid, name) is not None:
            raise AcsError('key_duplicate', 'A 3DS key with that nickname already exists.')
        if self.store.find_key_by_fingerprint(fingerprint, userid=owner_userid) is not None:
            raise AcsError('key_duplicate', 'That card is already enrolled for 3DS.')
        open_rows = [row for row in self.store.list_keys(owner_userid) if row.status in OPEN_KEYS]
        if len(open_rows) >= self.policy.max_keys:
            raise AcsError('key_limit', '3DS key limit reached.')
        now = float(self.clock())
        row = AcsKey(
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

    def get_key(self, *, key_id: str, actor: str, actor_type: str) -> AcsKey:
        self._require_enabled()
        row = self.store.get_key(key_id)
        if row is None:
            raise AcsError('key_not_found', '3DS key not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise AcsError('acs_forbidden', 'Not allowed to view this 3DS key.')
        return row

    def set_key_status(
        self,
        *,
        key_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> AcsKey:
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
            raise AcsError('invalid_status', 'Status must be pause, resume, or cancel.')
        if row.status == KEY_CANCELLED:
            raise AcsError('already_cancelled', '3DS key is already cancelled.')
        if wanted == KEY_PAUSED:
            if row.status == KEY_PAUSED:
                raise AcsError('already_paused', '3DS key is already paused.')
            if row.status != KEY_ACTIVE:
                raise AcsError('invalid_status', 'Only an active 3DS key can be paused.')
        elif wanted == KEY_ACTIVE:
            if row.status == KEY_ACTIVE:
                raise AcsError('already_active', '3DS key is already active.')
            if row.status != KEY_PAUSED:
                raise AcsError('invalid_status', 'Only a paused 3DS key can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_key(row)
        if wanted == KEY_ACTIVE:
            self._rematch_unmatched(row)
        return row

    def _rematch_unmatched(self, key: AcsKey) -> None:
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
        channel: Any = CH_BRW,
        method_completed: Any = False,
        internal_account: Any = None,
    ) -> Dict[str, Any]:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise AcsError('acs_forbidden', 'Not allowed to preview 3DS for this customer.')
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        mcc_code = normalize_mcc(mcc)
        merchant_name = normalize_merchant(merchant)
        ofac = self._screen(merchant_name)
        device = normalize_channel(channel)
        method_ok = bool(method_completed) or str(method_completed).strip().lower() in {'1', 'true', 'yes', 'on'}
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
        path = self._decision_path(dollars, device, method_ok)
        return {
            'amount': money_str(dollars),
            'merchant': merchant_name,
            'mcc': mcc_code,
            'purpose': purpose_from_mcc(mcc_code),
            'device_channel': device,
            'method_completed': method_ok,
            'internal_account': account,
            'key': key.to_dict() if key is not None else None,
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(dollars),
            'path': path,
            'frictionless_cap': money_str(self.policy.frictionless_cap),
            'queued': False,
            'clock': self.calendar.snapshot(now),
        }

    def _decision_path(self, amount: Decimal, channel: str, method_ok: bool) -> str:
        if channel == CH_3RI:
            if amount < self.policy.frictionless_cap:
                return 'frictionless'
            return 'attempt'
        if amount >= self.policy.frictionless_cap:
            return 'challenge'
        if channel == CH_BRW and not method_ok:
            return 'challenge'
        return 'frictionless'

    def _apply_outcome(self, row: AcsAuth, *, path: str, actor: str, acs_code: Any = None) -> AcsAuth:
        now = float(self.clock())
        row.actor = str(actor)
        row.updated_at = now
        if path == 'frictionless':
            row.status = AUTH_AUTHENTICATED
            row.trans_status = TS_Y
            row.trans_status_reason = ''
            row.eci = eci_for(row.brand, TS_Y)
            row.cavv = compose_cavv(
                brand=row.brand or 'visa', acs_trans_id=row.acs_trans_id,
                amount=row.amount, at=now,
            )
            row.message_type = MSG_ARES
            row.authenticated_at = now
            row.acs_code_hash = ''
        elif path == 'attempt':
            row.status = AUTH_ATTEMPTED
            row.trans_status = TS_A
            row.trans_status_reason = '80'
            row.eci = eci_for(row.brand, TS_A)
            row.cavv = compose_cavv(
                brand=row.brand or 'visa', acs_trans_id=row.acs_trans_id,
                amount=row.amount, at=now,
            )
            row.message_type = MSG_ARES
            row.authenticated_at = now
            row.acs_code_hash = ''
        elif path == 'challenge':
            code = normalize_acs_code(acs_code) if acs_code not in (None, '') else compose_acs_code(row.acs_trans_id)
            row.status = AUTH_CHALLENGE
            row.trans_status = TS_C
            row.trans_status_reason = ''
            row.eci = ''
            row.cavv = ''
            row.message_type = MSG_ARES
            row.acs_code_hash = hash_acs_code(code)
            row.challenge_at = now
            row.attempts = 0
        else:
            row.status = AUTH_REJECTED
            row.trans_status = TS_N
            row.eci = eci_for(row.brand, TS_N)
            row.cavv = ''
            row.message_type = MSG_ARES
        self.store.update_auth(row)
        return row

    def _evaluate(self, row: AcsAuth, *, actor: str, acs_code: Any = None) -> AcsAuth:
        now = float(self.clock())
        if not row.key_id or not row.userid:
            row.status = AUTH_UNMATCHED
            row.updated_at = now
            self.store.update_auth(row)
            return row
        key = self.store.get_key(row.key_id)
        if key is None or key.status != KEY_ACTIVE:
            row.status = AUTH_UNMATCHED
            row.updated_at = now
            self.store.update_auth(row)
            return row
        if row.ofac_hit and row.status not in REVERSIBLE:
            row.status = AUTH_HELD
            row.updated_at = now
            self.store.update_auth(row)
            return row
        dollars = parse_money(row.amount)
        if self._needs_dual_control(dollars) and not row.releaser:
            row.status = AUTH_PENDING
            row.updated_at = now
            self.store.update_auth(row)
            return row
        path = self._decision_path(dollars, row.device_channel, bool(row.method_completed))
        return self._apply_outcome(row, path=path, actor=actor, acs_code=acs_code)

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        amount: Any,
        merchant: Any,
        pan: Any = None,
        mcc: Any = None,
        ds_trans_id: Any = None,
        acs_trans_id: Any = None,
        three_ds_server_trans_id: Any = None,
        message_type: Any = MSG_AREQ,
        protocol_version: Any = VERSION_220,
        channel: Any = CH_BRW,
        category: Any = CAT_PA,
        method_completed: Any = False,
        acs_code: Any = None,
        currency: Any = 'USD',
        owner_userid: Any = None,
        key_id: Any = None,
        internal_account: Any = None,
    ) -> Tuple[AcsAuth, bool]:
        self._require_staff(actor_type)
        normalize_currency(currency)
        msg = normalize_message_type(message_type)
        if ds_trans_id not in (None, '') and not str(ds_trans_id).startswith('*'):
            ds_value = normalize_uuid(ds_trans_id, field='ds_trans_id')
            existing = self.store.get_auth_by_ds(ds_value)
            if existing is not None:
                if msg == MSG_CREQ:
                    return self.respond(
                        auth_id=existing.auth_id, actor=actor, actor_type=actor_type,
                        acs_code=acs_code,
                    ), False
                if msg in {MSG_RREQ, MSG_RRES} and existing.status in REVERSIBLE:
                    return self.reverse(
                        auth_id=existing.auth_id, actor=actor, actor_type=actor_type,
                    ), False
                return existing, False
        now = float(self.clock())
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        merchant_name = normalize_merchant(merchant)
        mcc_code = normalize_mcc(mcc)
        purpose = purpose_from_mcc(mcc_code)
        ofac = self._screen(merchant_name)
        device = normalize_channel(channel)
        cat = normalize_category(category)
        version = normalize_protocol_version(protocol_version)
        method_ok = bool(method_completed) or str(method_completed).strip().lower() in {'1', 'true', 'yes', 'on'}
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
        if len(self.store.list_auths()) >= self.policy.max_auths:
            raise AcsError('auth_limit', '3DS authentication limit reached.')
        ds_value = (
            normalize_uuid(ds_trans_id, field='ds_trans_id')
            if ds_trans_id not in (None, '') and not str(ds_trans_id).startswith('*')
            else compose_uuid()
        )
        acs_value = (
            normalize_uuid(acs_trans_id, field='acs_trans_id')
            if acs_trans_id not in (None, '') and not str(acs_trans_id).startswith('*')
            else compose_uuid()
        )
        server_value = (
            normalize_uuid(three_ds_server_trans_id, field='three_ds_server_trans_id')
            if three_ds_server_trans_id not in (None, '') and not str(three_ds_server_trans_id).startswith('*')
            else compose_uuid()
        )
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
        row = AcsAuth(
            auth_id=uuid.uuid4().hex,
            ds_trans_id=ds_value,
            acs_trans_id=acs_value,
            three_ds_server_trans_id=server_value,
            message_type=MSG_AREQ,
            protocol_version=version,
            device_channel=device,
            message_category=cat,
            key_id=resolved_key_id,
            userid=userid,
            internal_account=account,
            amount=money_str(dollars),
            nickname=nickname,
            merchant=merchant_name,
            mcc=mcc_code,
            purpose=purpose,
            brand=brand,
            last4=pan_last4,
            status=AUTH_UNMATCHED,
            trans_status='',
            trans_status_reason='',
            eci='',
            cavv='',
            acs_code_hash='',
            method_completed=1 if method_ok else 0,
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
        )
        stored = self.store.put_auth(row)
        if stored.auth_id != row.auth_id:
            return stored, False
        return self._evaluate(row, actor=actor, acs_code=acs_code), True

    def ingest_file(self, *, actor: str, actor_type: str, payload: Any) -> List[AcsAuth]:
        self._require_staff(actor_type)
        rows = []
        for line in split_3ds1_file(payload):
            parsed = parse_3ds1(line)
            row, _created = self.ingest(
                actor=actor,
                actor_type=actor_type,
                amount=parsed['amount'],
                merchant=parsed['merchant'],
                pan=parsed['pan'] if not str(parsed['pan']).startswith('*') else None,
                mcc=parsed['mcc'],
                ds_trans_id=parsed['ds_trans_id'] if not str(parsed['ds_trans_id']).startswith('*') else None,
                message_type=parsed['message_type'] or MSG_AREQ,
                protocol_version=parsed['version'] or VERSION_220,
                channel=parsed['channel'] or CH_BRW,
                category=parsed['category'] or CAT_PA,
                currency=parsed['currency'],
            )
            rows.append(row)
        return rows

    def get_auth(self, *, auth_id: str, actor: str, actor_type: str) -> AcsAuth:
        self._require_enabled()
        row = self.store.get_auth(auth_id)
        if row is None:
            raise AcsError('auth_not_found', '3DS authentication not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise AcsError('acs_forbidden', 'Not allowed to view this 3DS authentication.')
        return row

    def claim(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        auth_id: str,
        pan: Any = None,
        last4_value: Any = None,
        brand: Any = None,
        default_account: Any = None,
        nickname: Any = None,
        acs_code: Any = None,
    ) -> AcsAuth:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise AcsError('acs_forbidden', 'Not allowed to claim 3DS authentications for this customer.')
        row = self.store.get_auth(auth_id)
        if row is None:
            raise AcsError('auth_not_found', '3DS authentication not found.')
        if row.status != AUTH_UNMATCHED:
            raise AcsError('not_claimable', 'Only unmatched 3DS authentications can be claimed.')
        presented_last4 = last4(last4_value or '')
        presented_brand = normalize_brand(brand) if brand not in (None, '') else row.brand
        fingerprint = ''
        if pan not in (None, ''):
            digits = normalize_pan(pan)
            fingerprint = pan_fingerprint(digits)
            presented_last4 = last4(digits)
            presented_brand = brand_from_iin(digits)
        if not presented_last4:
            raise AcsError('invalid_pan', 'Last4 or PAN is required to claim.')
        if row.last4 and row.last4 != presented_last4:
            raise AcsError('not_claimable', 'Claimed last4 does not match this authentication.')
        if row.brand and presented_brand and row.brand != presented_brand:
            raise AcsError('not_claimable', 'Claimed brand does not match this authentication.')
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
                raise AcsError('invalid_account', 'Destination account is required to enroll a claim.')
            if pan in (None, ''):
                raise AcsError('invalid_pan', 'PAN is required to enroll a claim.')
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
        return self._evaluate(row, actor=actor, acs_code=acs_code)

    def assign(
        self,
        *,
        auth_id: str,
        actor: str,
        actor_type: str,
        owner_userid: str,
        key_id: Any = None,
        pan: Any = None,
        default_account: Any = None,
        nickname: Any = None,
        acs_code: Any = None,
    ) -> AcsAuth:
        self._require_staff(actor_type)
        row = self.store.get_auth(auth_id)
        if row is None:
            raise AcsError('auth_not_found', '3DS authentication not found.')
        if row.status != AUTH_UNMATCHED:
            raise AcsError('not_assignable', 'Only unmatched 3DS authentications can be assigned.')
        key = None
        if key_id:
            key = self.get_key(key_id=str(key_id), actor=actor, actor_type=actor_type)
            if key.userid != owner_userid:
                raise AcsError('acs_forbidden', 'Key does not belong to this customer.')
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
            raise AcsError('missing_key', 'Assign requires a key or PAN.')
        row.key_id = key.key_id
        row.userid = key.userid
        row.internal_account = key.default_account
        row.nickname = key.nickname
        row.brand = key.brand
        row.last4 = key.last4
        return self._evaluate(row, actor=actor, acs_code=acs_code)

    def override_ofac(self, *, auth_id: str, actor: str, actor_type: str, note: Any = '') -> AcsAuth:
        self._require_staff(actor_type)
        row = self.get_auth(auth_id=auth_id, actor=actor, actor_type=actor_type)
        if row.status != AUTH_HELD:
            raise AcsError('not_overridable', 'Only an OFAC hold can be overridden.')
        row.ofac_hit = 0
        row.ofac_match = ''
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        return self._evaluate(row, actor=actor)

    def release(self, *, auth_id: str, actor: str, actor_type: str) -> AcsAuth:
        self._require_staff(actor_type)
        row = self.get_auth(auth_id=auth_id, actor=actor, actor_type=actor_type)
        if row.status not in RELEASABLE:
            raise AcsError('not_releasable', 'Only a pending or held 3DS authentication can be released.')
        if row.status == AUTH_HELD:
            raise AcsError('ofac_hold', 'OFAC hold must be overridden before release.')
        if row.actor == str(actor) and self._needs_dual_control(parse_money(row.amount)):
            raise AcsError('same_approver', 'A different employee must release this 3DS authentication.')
        row.releaser = str(actor)
        return self._evaluate(row, actor=actor)

    def reject(self, *, auth_id: str, actor: str, actor_type: str, reason: Any = 'other', note: Any = '') -> AcsAuth:
        self._require_staff(actor_type)
        row = self.get_auth(auth_id=auth_id, actor=actor, actor_type=actor_type)
        if row.status not in REJECTABLE:
            raise AcsError('not_rejectable', 'This 3DS authentication cannot be rejected.')
        now = float(self.clock())
        row.status = AUTH_REJECTED
        row.trans_status = TS_R
        row.eci = eci_for(row.brand, TS_R)
        row.cavv = ''
        row.acs_code_hash = ''
        row.message_type = MSG_ARES
        row.reason = normalize_note(reason, limit=40)
        row.note = normalize_note(note) or row.note
        row.updated_at = now
        row.actor = str(actor)
        self.store.update_auth(row)
        return row

    def respond(
        self,
        *,
        auth_id: str,
        actor: str,
        actor_type: str,
        acs_code: Any,
    ) -> AcsAuth:
        self._require_enabled()
        row = self.get_auth(auth_id=auth_id, actor=actor, actor_type=actor_type)
        if row.status != AUTH_CHALLENGE:
            raise AcsError('not_challengeable', 'Only a challenge 3DS authentication can be answered.')
        now = float(self.clock())
        if self.calendar.challenge_expired(row.challenge_at, now):
            row.status = AUTH_EXPIRED
            row.trans_status = TS_U
            row.eci = eci_for(row.brand, TS_U)
            row.cavv = ''
            row.message_type = MSG_ARES
            row.updated_at = now
            self.store.update_auth(row)
            raise AcsError('challenge_expired', 'ACS challenge window is 10 minutes.', auth=row)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise AcsError('acs_forbidden', 'Not allowed to answer this 3DS challenge.')
        presented = normalize_acs_code(acs_code)
        if not codes_match(presented, row.acs_code_hash):
            row.attempts += 1
            row.updated_at = now
            if row.attempts >= self.policy.max_attempts:
                row.status = AUTH_REJECTED
                row.trans_status = TS_N
                row.eci = eci_for(row.brand, TS_N)
                row.cavv = ''
                row.acs_code_hash = ''
                row.message_type = MSG_CRES
                row.reason = 'bad_code'
                self.store.update_auth(row)
                raise AcsError('invalid_code', 'ACS challenge failed.', auth=row)
            self.store.update_auth(row)
            raise AcsError('invalid_code', 'ACS challenge code is incorrect.', auth=row)
        row.status = AUTH_AUTHENTICATED
        row.trans_status = TS_Y
        row.eci = eci_for(row.brand, TS_Y)
        row.cavv = compose_cavv(
            brand=row.brand or 'visa', acs_trans_id=row.acs_trans_id,
            amount=row.amount, at=now,
        )
        row.acs_code_hash = ''
        row.message_type = MSG_CRES
        row.authenticated_at = now
        row.updated_at = now
        row.actor = str(actor)
        self.store.update_auth(row)
        return row

    def cancel(self, *, auth_id: str, actor: str, actor_type: str, note: Any = '') -> AcsAuth:
        self._require_enabled()
        row = self.get_auth(auth_id=auth_id, actor=actor, actor_type=actor_type)
        if row.status != AUTH_CHALLENGE:
            raise AcsError('not_cancelable', 'Only a challenge 3DS authentication can be cancelled.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise AcsError('acs_forbidden', 'Not allowed to cancel this 3DS challenge.')
        now = float(self.clock())
        row.status = AUTH_REJECTED
        row.trans_status = TS_N
        row.eci = eci_for(row.brand, TS_N)
        row.cavv = ''
        row.acs_code_hash = ''
        row.message_type = MSG_CRES
        row.note = normalize_note(note) or row.note
        row.updated_at = now
        row.actor = str(actor)
        self.store.update_auth(row)
        return row

    def reverse(self, *, auth_id: str, actor: str, actor_type: str, note: Any = '') -> AcsAuth:
        self._require_enabled()
        row = self.get_auth(auth_id=auth_id, actor=actor, actor_type=actor_type)
        if row.status not in REVERSIBLE:
            raise AcsError('not_reversible', 'Only an authenticated 3DS result can be reversed.')
        now = float(self.clock())
        stamped = row.authenticated_at or row.created_at
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_reverse:
                raise AcsError('acs_forbidden', 'Customers cannot reverse 3DS authentications.')
            if now - stamped > CUSTOMER_REVERSE_HOURS * 3600:
                raise AcsError('return_window_closed', 'Customer reversal window is 24 hours.')
        elif now - stamped > STAFF_REVERSE_DAYS * 86400:
            raise AcsError('return_window_closed', 'Staff reversal window is 90 days.')
        row.status = AUTH_REVERSED
        row.trans_status = TS_N
        row.eci = eci_for(row.brand, TS_N)
        row.cavv = ''
        row.message_type = MSG_RREQ
        row.reversed_at = now
        row.updated_at = now
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        self.store.update_auth(row)
        return row

    def export_auth(self, *, auth_id: str, actor: str, actor_type: str) -> str:
        self._require_staff(actor_type)
        row = self.get_auth(auth_id=auth_id, actor=actor, actor_type=actor_type)
        if row.status == AUTH_UNMATCHED:
            raise AcsError('not_exportable', 'Unmatched 3DS authentications cannot be exported.')
        return compose_3ds1(
            ds_trans_id=row.ds_trans_id,
            message_type=row.message_type,
            version=row.protocol_version,
            channel=row.device_channel,
            category=row.message_category,
            pan_masked=mask_pan(row.last4.rjust(16, '*')),
            amount=row.amount,
            merchant=row.merchant,
            mcc=row.mcc,
            trans_status=row.trans_status,
            eci=row.eci,
        )

    def run_due(self, userid: Optional[str] = None) -> List[AcsAuth]:
        self._require_enabled()
        now = float(self.clock())
        changed = []
        rows = self.store.list_auths(userid) if userid else self.store.list_auths()
        for row in rows:
            if row.status == AUTH_UNMATCHED and now - row.created_at >= UNMATCHED_HOURS * 3600:
                row.status = AUTH_EXPIRED
                row.trans_status = TS_U
                row.eci = eci_for(row.brand, TS_U)
                row.message_type = MSG_ARES
                row.updated_at = now
                self.store.update_auth(row)
                changed.append(row)
                continue
            if row.status == AUTH_CHALLENGE and self.calendar.challenge_expired(row.challenge_at, now):
                row.status = AUTH_EXPIRED
                row.trans_status = TS_U
                row.eci = eci_for(row.brand, TS_U)
                row.cavv = ''
                row.acs_code_hash = ''
                row.message_type = MSG_ARES
                row.updated_at = now
                self.store.update_auth(row)
                changed.append(row)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.run_due(userid)
        keys = self.store.list_keys(userid)
        rows = self.store.list_auths(userid)
        unmatched = [
            row for row in self.store.list_unmatched()
            if not userid or row.last4 in {key.last4 for key in keys}
        ]
        authed_ytd = Decimal('0.00')
        challenge_open = 0
        for row in rows:
            amount = parse_money(row.amount, allow_zero=True)
            if row.status == AUTH_AUTHENTICATED:
                authed_ytd += amount
            elif row.status == AUTH_CHALLENGE:
                challenge_open += 1
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'frictionless_cap': money_str(self.policy.frictionless_cap),
            'clock': self.calendar.snapshot(now),
            'keys': [row.to_dict() for row in keys[:40]],
            'auths': [row.to_dict() for row in rows[:40]],
            'unmatched': [row.to_dict() for row in unmatched[:40]] if actor_type in EMPLOYEE_ROLES else [],
            'ytd_authenticated': money_str(authed_ytd),
            'challenge_count': challenge_open,
            'active_count': sum(1 for row in keys if row.status == KEY_ACTIVE),
            'open_count': sum(1 for row in rows if row.status in OPEN_AUTHS),
        }


_SERVICE: Optional[AcsService] = None


def set_service(service: Optional[AcsService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[AcsService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('ACS_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryAcsStore()
    path = os.environ.get('ACS_DB', DEFAULT_STORE_PATH)
    return SqliteAcsStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[AcsClock] = None,
) -> AcsService:
    if store is None:
        store = default_store()
    return AcsService(
        AcsPolicy.from_env(),
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
        'auth_limit': 409,
        'already_cancelled': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_authenticated': 409,
        'already_reversed': 409,
        'failed': 409,
        'acs_forbidden': 403,
        'acs_disabled': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_claimable': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_reversible': 403,
        'not_challengeable': 403,
        'not_cancelable': 403,
        'not_exportable': 403,
        'challenge_expired': 403,
        'return_window_closed': 403,
        'key_not_found': 404,
        'auth_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_pan': 400,
        'invalid_brand': 400,
        'invalid_mcc': 400,
        'invalid_version': 400,
        'invalid_message_type': 400,
        'invalid_channel': 400,
        'invalid_category': 400,
        'invalid_code': 400,
        'invalid_cavv': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_currency': 400,
        'invalid_reference': 400,
        'invalid_file': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_key': 400,
        'missing_auth': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: AcsError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('auth') is not None:
        body['auth'] = exc.extra['auth'].to_dict()
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
    except AcsError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: AcsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Card3ds': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_add_key(service: AcsService):
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
            'message': '3DS key added',
            'key': row.to_dict(),
            'Card3ds': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _key_status_route(service: AcsService, status: str, ok_message: str):
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
            'Card3ds': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: AcsService):
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
            channel=values.get('channel') or values.get('device_channel') or CH_BRW,
            method_completed=values.get('method_completed'),
            internal_account=values.get('account') or values.get('internal_account'),
        )
        return jsonify({'preview': preview, 'Card3ds': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest(service: AcsService):
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
            mcc=values.get('mcc'),
            ds_trans_id=values.get('ds_trans_id'),
            acs_trans_id=values.get('acs_trans_id'),
            three_ds_server_trans_id=values.get('three_ds_server_trans_id'),
            message_type=values.get('message_type') or MSG_AREQ,
            protocol_version=values.get('protocol_version') or values.get('version') or VERSION_220,
            channel=values.get('channel') or values.get('device_channel') or CH_BRW,
            category=values.get('category') or values.get('message_category') or CAT_PA,
            method_completed=values.get('method_completed'),
            acs_code=values.get('acs_code'),
            currency=values.get('currency') or 'USD',
            owner_userid=values.get('customer_id'),
            key_id=values.get('key_id'),
            internal_account=values.get('account'),
        )
        owner = row.userid or str(values.get('customer_id') or userid)
        return jsonify({
            'message': '3DS authentication ingested' if created else '3DS authentication already recorded',
            'auth': row.to_dict(),
            'Card3ds': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_file(service: AcsService):
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
            'message': '3DS file ingested',
            'auths': [row.to_dict() for row in rows],
            'Card3ds': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def handle_claim(service: AcsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    auth_id = str(values.get('auth_id') or '').strip()
    if not auth_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_auth'}), 400
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values) or userid

    def _run():
        row = service.claim(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            auth_id=auth_id,
            pan=values.get('pan'),
            last4_value=values.get('last4'),
            brand=values.get('brand'),
            default_account=values.get('default_account') or values.get('account'),
            nickname=values.get('nickname'),
            acs_code=values.get('acs_code'),
        )
        return jsonify({
            'message': '3DS authentication claimed',
            'auth': row.to_dict(),
            'Card3ds': service.snapshot(row.userid or owner, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_assign(service: AcsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    auth_id = str(values.get('auth_id') or '').strip()
    if not auth_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_auth'}), 400
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.assign(
            auth_id=auth_id,
            actor=userid,
            actor_type=actor_type,
            owner_userid=owner,
            key_id=values.get('key_id'),
            pan=values.get('pan'),
            default_account=values.get('default_account') or values.get('account'),
            nickname=values.get('nickname'),
            acs_code=values.get('acs_code'),
        )
        return jsonify({
            'message': '3DS authentication assigned',
            'auth': row.to_dict(),
            'Card3ds': service.snapshot(row.userid or owner, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_respond(service: AcsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    auth_id = str(values.get('auth_id') or '').strip()
    if not auth_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_auth'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.respond(
            auth_id=auth_id, actor=userid, actor_type=actor_type,
            acs_code=values.get('acs_code') or values.get('code'),
        )
        return jsonify({
            'message': '3DS challenge completed',
            'auth': row.to_dict(),
            'Card3ds': service.snapshot(row.userid or userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_cancel(service: AcsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    auth_id = str(values.get('auth_id') or '').strip()
    if not auth_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_auth'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.cancel(
            auth_id=auth_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': '3DS challenge cancelled',
            'auth': row.to_dict(),
            'Card3ds': service.snapshot(row.userid or userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_acs_route(service: AcsService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    auth_id = str(values.get('auth_id') or '').strip()
    if not auth_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_auth'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'release':
            row = service.release(auth_id=auth_id, actor=userid, actor_type=actor_type)
            message = '3DS authentication released'
        elif action == 'reject':
            row = service.reject(
                auth_id=auth_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = '3DS authentication rejected'
        elif action == 'override':
            row = service.override_ofac(
                auth_id=auth_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'reverse':
            row = service.reverse(
                auth_id=auth_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = '3DS authentication reversed'
        elif action == 'export':
            body = service.export_auth(auth_id=auth_id, actor=userid, actor_type=actor_type)
            row = service.get_auth(auth_id=auth_id, actor=userid, actor_type=actor_type)
            return jsonify({
                'message': '3DS authentication exported',
                'file': body,
                'auth': row.to_dict(),
                'Card3ds': service.snapshot(row.userid or userid, actor=userid, actor_type=actor_type),
            }), 200
        else:
            raise AcsError('invalid_status', 'Unknown 3DS action.')
        return jsonify({
            'message': message,
            'auth': row.to_dict(),
            'Card3ds': service.snapshot(row.userid or userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: AcsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Card3ds': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_acs_routes(app, service: AcsService) -> None:
    @app.route('/listCard3ds', methods=['POST', 'GET'])
    def list_card_3ds_route():
        return handle_list(service)

    @app.route('/listCard3dsKeys', methods=['POST', 'GET'])
    def list_card_3ds_keys_route():
        return handle_list(service)

    @app.route('/listUnmatchedCard3ds', methods=['POST', 'GET'])
    def list_unmatched_card_3ds_route():
        return handle_list(service)

    @app.route('/addCard3dsKey', methods=['POST', 'GET'])
    def add_card_3ds_key_route():
        return handle_add_key(service)

    @app.route('/pauseCard3dsKey', methods=['POST', 'GET'])
    def pause_card_3ds_key_route():
        return _key_status_route(service, KEY_PAUSED, '3DS key paused')

    @app.route('/resumeCard3dsKey', methods=['POST', 'GET'])
    def resume_card_3ds_key_route():
        return _key_status_route(service, KEY_ACTIVE, '3DS key resumed')

    @app.route('/cancelCard3dsKey', methods=['POST', 'GET'])
    def cancel_card_3ds_key_route():
        return _key_status_route(service, KEY_CANCELLED, '3DS key cancelled')

    @app.route('/previewCard3ds', methods=['POST', 'GET'])
    def preview_card_3ds_route():
        return handle_preview(service)

    @app.route('/ingestCard3ds', methods=['POST', 'GET'])
    def ingest_card_3ds_route():
        return handle_ingest(service)

    @app.route('/ingestCard3dsFile', methods=['POST', 'GET'])
    def ingest_card_3ds_file_route():
        return handle_ingest_file(service)

    @app.route('/claimCard3ds', methods=['POST', 'GET'])
    def claim_card_3ds_route():
        return handle_claim(service)

    @app.route('/assignCard3ds', methods=['POST', 'GET'])
    def assign_card_3ds_route():
        return handle_assign(service)

    @app.route('/respondCard3ds', methods=['POST', 'GET'])
    def respond_card_3ds_route():
        return handle_respond(service)

    @app.route('/cancelCard3ds', methods=['POST', 'GET'])
    def cancel_card_3ds_route():
        return handle_cancel(service)

    @app.route('/overrideCard3dsOfac', methods=['POST', 'GET'])
    def override_card_3ds_ofac_route():
        return _staff_acs_route(service, 'override')

    @app.route('/releaseCard3ds', methods=['POST', 'GET'])
    def release_card_3ds_route():
        return _staff_acs_route(service, 'release')

    @app.route('/rejectCard3ds', methods=['POST', 'GET'])
    def reject_card_3ds_route():
        return _staff_acs_route(service, 'reject')

    @app.route('/reverseCard3ds', methods=['POST', 'GET'])
    def reverse_card_3ds_route():
        return _staff_acs_route(service, 'reverse')

    @app.route('/requestCard3dsReverse', methods=['POST', 'GET'])
    def request_card_3ds_reverse_route():
        return _staff_acs_route(service, 'reverse')

    @app.route('/exportCard3ds', methods=['POST', 'GET'])
    def export_card_3ds_route():
        return _staff_acs_route(service, 'export')

    @app.route('/runDueCard3ds', methods=['POST', 'GET'])
    def run_due_card_3ds_route():
        return handle_run_due(service)
