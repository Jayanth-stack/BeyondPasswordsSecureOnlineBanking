"""Hong Kong FPS / CHATS / ECG origination with Addressing Service CoP.

Customers send HKD (or FPS CNY) via FPS addressing (HKID, +852 mobile,
email, 7-digit FPS ID) or HKMA bank code + account. Overlay FPS is 24/7;
high-value CHATS RTGS follows the HKMA session; ECG is next-day clearing.
Independent of domestic Fedwire (PR #73), ACH linking (PR #68), and the
unmerged PayNow / NPP / UPI / PIX rails. Existing `/fundTransfer`,
`/withdrawAmount`, and `/sendWire` stay unchanged.
`Customers.debit_request` / `credit_request` still write `debited` /
`direct deposited` unless a remark is supplied here.

Foundations (reusable beyond this screen):
- Hong Kong Identity Card checksum (1-2 letters + 6 digits + mod-11)
- +852 8-digit mobile (4/5/6/7/9 national)
- FPS ID (7-digit HKICL addressing identifier)
- Email proxy (HKICL Addressing Service)
- 3-digit HKMA participating bank codes
- Confirmation of Payee (`match` / `close_match` / `no_match` /
  `unavailable`) — same outcome set as UK CoP / PayNow / PayID
- FPS End-to-End ID (`FPS`/`CHATS`/`ECG` + YYYYMMDD + seq)
- FPS1 pipe clearing (XML/DOCTYPE and non-HKICL clearing rejected)
- HkClock in HKT (UTC+8): FPS 24/7; CHATS 08:30-18:00 HKT banking
  days; ECG queues to the next HK clearing day
- HKDUSD / CNYUSD quote book converting instructed amount to USD debit
- Dual-control at HKD 100,000; FPS cap 1m / CHATS 50m; CNY night cap

Reuses Fedwire OFAC screening, money parsing, fee helper, and account
directory from `utility.wire`. Stores are pluggable (memory for tests,
sqlite WAL for restart-safe default). Full HKID, FPS ID, mobile, email,
account, fingerprint, and E2E ID never appear unmasked in to_dict /
snapshots.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    DEBIT_NSF,
    DEBIT_OK,
    DEFAULT_WATCHLIST,
    EMPLOYEE_ROLES,
    MONEY_QUANTUM,
    AccountError,
    AmountError,
    ScreenResult,
    account_types_from_customer_payload,
    compute_fee,
    last4,
    money_str,
    normalize_account,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

BENE_ACTIVE = 'active'
BENE_PAUSED = 'paused'
BENE_ARCHIVED = 'archived'
BENE_STATUSES = frozenset({BENE_ACTIVE, BENE_PAUSED, BENE_ARCHIVED})
OPEN_BENE = frozenset({BENE_ACTIVE, BENE_PAUSED})
PN_HELD = 'held'
PN_QUEUED = 'queued'
PN_PENDING = 'pending_release'
PN_SENT = 'sent'
PN_COMPLETED = 'completed'
PN_REJECTED = 'rejected'
PN_CANCELLED = 'cancelled'
PN_RECALLED = 'recalled'
PN_NSF = 'nsf'
PN_FAILED = 'failed'
PN_STATUSES = frozenset({
    PN_HELD, PN_QUEUED, PN_PENDING, PN_SENT, PN_COMPLETED, PN_REJECTED,
    PN_CANCELLED, PN_RECALLED, PN_NSF, PN_FAILED,
})
OPEN_PN = frozenset({PN_HELD, PN_QUEUED, PN_PENDING, PN_SENT})
CANCELABLE = frozenset({PN_HELD, PN_QUEUED, PN_PENDING})
FEE_NONE = 'none'
FEE_COLLECTED = 'collected'
FEE_WAIVED = 'waived'
FEE_NSF = 'nsf'
PURPOSES = frozenset({'family', 'goods', 'payroll', 'tax', 'loan', 'rent', 'other'})
PURPOSE_ALIASES = {
    'personal': 'family', 'gift': 'family', 'support': 'family',
    'invoice': 'goods', 'purchase': 'goods', 'vendor': 'goods',
    'salary': 'payroll', 'wage': 'payroll',
    'ird': 'tax', 'taxes': 'tax',
    'mortgage': 'loan', 'housing': 'rent', 'rent': 'rent',
}
RAILS = frozenset({'fps', 'chats', 'ecg'})
RAIL_ALIASES = {
    'faster': 'fps', 'hkicl': 'fps', 'overlay': 'fps', 'instant': 'fps',
    'a2a': 'fps', 'fps_a2a': 'fps',
    'rtgs': 'chats', 'chats_rtgs': 'chats', 'hkma': 'chats',
    'clearing': 'ecg', 'next_day': 'ecg', 'ach': 'ecg', 'paper': 'ecg',
}
PROXY_TYPES = frozenset({'hkid', 'phone', 'email', 'fpsid'})
PROXY_ALIASES = {
    'id': 'hkid', 'hk_id': 'hkid', 'identity': 'hkid', 'nric': 'hkid',
    'mobile': 'phone', 'msisdn': 'phone', 'tel': 'phone', 'sms': 'phone',
    'mail': 'email', 'e_mail': 'email',
    'fps_id': 'fpsid', 'proxy_id': 'fpsid', 'addressing': 'fpsid',
}
COP_MATCH = 'match'
COP_CLOSE = 'close_match'
COP_NONE = 'no_match'
COP_UNAVAILABLE = 'unavailable'
COP_OUTCOMES = frozenset({COP_MATCH, COP_CLOSE, COP_NONE, COP_UNAVAILABLE})
CLEARING_FPS = 'FPS'
CLEARING_BY_RAIL = {'fps': 'FPS', 'chats': 'CHATS', 'ecg': 'ECG'}
REJECTED_CLEARING = frozenset({
    'AUNPP', 'USRTPP', 'FEDNOW', 'FEDWIRE', 'ACH', 'UPI', 'CHIPS', 'SWIFT',
    'PIX', 'SPEI', 'NPP', 'OSKO', 'FAST', 'PAYNOW', 'MEPS', 'GIRO', 'PNW',
})
DEFAULT_BANKS = {
    '004': 'HSBC',
    '003': 'STANDARD CHARTERED',
    '012': 'BANK OF CHINA HONG KONG',
    '024': 'HANG SENG',
    '016': 'DBS',
    '018': 'CHINA CITIC',
    '027': 'BANK OF EAST ASIA',
    '039': 'CHIYU BANK',
    '043': 'NANYANG COMMERCIAL',
    '099': 'KONOHA BANK',
}
DEFAULT_FPS_DIRECTORY = {
    'phone:+85291234567': 'ADA LOVELACE',
    'hkid:A1234563': 'ADA LOVELACE',
    'email:ada@example.hk': 'ADA LOVELACE',
    'fpsid:1234567': 'ADA LOVELACE LTD',
}
FPS1_VERSION = '1'
FPS1_FIELDS = 14
DEFAULT_STORE_PATH = 'SystemLogs/hkfps.sqlite'
DEFAULT_SOURCE_BANK = '099'
CUSTOMER_RETURN_SECONDS = 24 * 60 * 60
STAFF_COMPLAINT_DAYS = 90
HKT_OFFSET_MINUTES = 480
DEFAULT_HKDUSD = Decimal('0.13')
DEFAULT_CNYUSD = Decimal('0.14')
BLOCKED_E2E_PREFIXES = (
    'IMAD', 'UETR', 'NPP', 'AUNPP', 'RRN', 'STAN', 'ARN', 'RFI', 'RFR', 'RTR',
    'PNW', 'FAST', 'MEPS', 'GIRO', 'PIX', 'SPEI', 'UPI',
)
E2E_PREFIX = {'fps': 'FPS', 'chats': 'CHATS', 'ecg': 'ECG'}
HKID_LETTER_BASE = 10
HKID_SPACE = 36


class FpsError(ValueError):
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
        raise FpsError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise FpsError('invalid_name', 'Beneficiary legal name must be 2-80 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise FpsError('invalid_purpose', 'Unknown FPS purpose.')
    return text


def normalize_currency(value: Any, *, default: str = 'HKD', rail: str = 'fps') -> str:
    text = str(value or default).strip().upper().replace('$', '')
    if text in {'HK', 'HK$', 'HKD.', 'H'}:
        text = 'HKD'
    if text in {'CN', 'CNY.', 'CNH', 'RMB', 'YUAN'}:
        text = 'CNY'
    if text not in {'HKD', 'CNY'}:
        raise FpsError('invalid_currency', 'FPS supports HKD and CNY only.')
    kind = normalize_rail(rail)
    if kind in {'chats', 'ecg'} and text != 'HKD':
        raise FpsError('invalid_currency', 'CHATS and ECG are HKD only.')
    return text


def normalize_rail(value: Any, *, default: str = 'fps') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text in {'paynow', 'fast'}:
        text = 'fps'
    if text == 'meps':
        text = 'chats'
    if text == 'giro':
        text = 'ecg'
    if text not in RAILS:
        raise FpsError('invalid_rail', 'Rail must be fps, chats, or ecg.')
    return text


def normalize_proxy_type(value: Any, *, default: str = '') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PROXY_ALIASES.get(text, text)
    if not text:
        return ''
    if text not in PROXY_TYPES:
        raise FpsError('invalid_proxy', 'FPS proxy must be hkid, phone, email, or fpsid.')
    return text


def normalize_bank_code(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise FpsError('invalid_bank', 'HKMA bank code is required.')
        return ''
    if len(digits) == 1:
        digits = digits.zfill(3)
    if len(digits) == 2:
        digits = digits.zfill(3)
    if len(digits) != 3 or digits == '000':
        raise FpsError('invalid_bank', 'HKMA bank code must be three digits.')
    return digits


def bank_name(value: Any, *, directory: Optional[Dict[str, str]] = None) -> str:
    try:
        code = normalize_bank_code(value)
    except FpsError:
        return ''
    mapping = directory if directory is not None else DEFAULT_BANKS
    return mapping.get(code, '')


def normalize_external_account(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise FpsError('invalid_account', 'Beneficiary account is required.')
        return ''
    if not (6 <= len(digits) <= 16):
        raise FpsError('invalid_account', 'Hong Kong account must be 6-16 digits.')
    return digits


def mask_account(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return ('*' * max(0, len(digits) - 4)) + digits[-4:]


def _hkid_value(ch: str) -> int:
    if ch == ' ':
        return HKID_SPACE
    if ch.isdigit():
        return int(ch)
    if ch.isalpha():
        return HKID_LETTER_BASE + (ord(ch.upper()) - ord('A'))
    raise FpsError('invalid_hkid', 'HKID contains an invalid character.')


def hkid_check_digit(prefix: str, body: str) -> str:
    prefix = prefix.upper()
    if not re.fullmatch(r'[A-Z]{1,2}', prefix) or len(body) != 6 or not body.isdigit():
        raise FpsError('invalid_hkid', 'HKID must be 1-2 letters plus 6 digits.')
    padded = prefix.rjust(2)
    total = 0
    weights = (9, 8, 7, 6, 5, 4, 3, 2)
    for ch, weight in zip(padded + body, weights):
        total += _hkid_value(ch) * weight
    remainder = total % 11
    if remainder == 0:
        return '0'
    if remainder == 1:
        return 'A'
    return str(11 - remainder)


def hkid_check_ok(value: str) -> bool:
    text = re.sub(r'[\s()/-]', '', str(value or '')).upper()
    match = re.fullmatch(r'([A-Z]{1,2})(\d{6})([0-9A])', text)
    if not match:
        return False
    try:
        return hkid_check_digit(match.group(1), match.group(2)) == match.group(3)
    except FpsError:
        return False


def compose_hkid(prefix: Any, body: Any) -> str:
    letter = str(prefix or '').strip().upper()
    digits = ''.join(ch for ch in str(body or '') if ch.isdigit())
    return letter + digits + hkid_check_digit(letter, digits)


def normalize_hkid(value: Any, *, required: bool = False) -> str:
    text = re.sub(r'[\s()/-]', '', str(value or '')).upper()
    if not text:
        if required:
            raise FpsError('invalid_hkid', 'HKID is required.')
        return ''
    if not hkid_check_ok(text):
        raise FpsError('invalid_hkid', 'HKID failed the checksum.')
    return text


def mask_hkid(value: Any) -> str:
    text = re.sub(r'[\s()/-]', '', str(value or '')).upper()
    if len(text) < 5:
        return 'A******X'
    prefix_len = 2 if len(text) == 9 else 1
    return text[:prefix_len] + '****' + text[-3:]


def fpsid_check_ok(value: str) -> bool:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return len(digits) == 7 and digits != '0000000'


def compose_fpsid(body: Any) -> str:
    digits = ''.join(ch for ch in str(body or '') if ch.isdigit())
    if len(digits) != 7 or digits == '0000000':
        raise FpsError('invalid_fpsid', 'FPS ID must be seven digits.')
    return digits


def normalize_fpsid(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise FpsError('invalid_fpsid', 'FPS ID is required.')
        return ''
    if not fpsid_check_ok(digits):
        raise FpsError('invalid_fpsid', 'FPS ID must be seven digits.')
    return digits


def mask_fpsid(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 7:
        return '*******'
    return digits[:2] + '***' + digits[-2:]


def normalize_phone(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise FpsError('invalid_phone', 'Hong Kong mobile is required.')
        return ''
    if digits.startswith('852') and len(digits) == 11:
        national = digits[3:]
    elif digits.startswith('0') and len(digits) == 9:
        national = digits[1:]
    else:
        national = digits
    if len(national) != 8 or national[0] not in {'4', '5', '6', '7', '9'}:
        raise FpsError('invalid_phone', 'FPS mobile must be an 8-digit +852 4/5/6/7/9 number.')
    return '+852' + national


def mask_phone(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if digits.startswith('852') and len(digits) == 11:
        national = digits[3:]
    else:
        national = digits
    if len(national) != 8:
        return '+852****'
    return '+852****' + national[-4:]


def normalize_email(value: Any, *, required: bool = False) -> str:
    text = str(value or '').strip().lower()
    if not text:
        if required:
            raise FpsError('invalid_email', 'Email proxy is required.')
        return ''
    if not re.fullmatch(r'[a-z0-9._%+-]{1,64}@[a-z0-9.-]+\.[a-z]{2,24}', text):
        raise FpsError('invalid_email', 'FPS email proxy is invalid.')
    return text


def mask_email(value: Any) -> str:
    text = str(value or '').strip().lower()
    if '@' not in text:
        return '***@***'
    local, domain = text.split('@', 1)
    head = local[:1] if local else '*'
    return head + '***@' + domain


def normalize_proxy(proxy_type: Any, value: Any, *, required: bool = False) -> Tuple[str, str]:
    kind = normalize_proxy_type(proxy_type)
    if not kind:
        if required:
            raise FpsError('invalid_proxy', 'FPS proxy type is required.')
        return '', ''
    if kind == 'hkid':
        return kind, normalize_hkid(value, required=True)
    if kind == 'fpsid':
        return kind, normalize_fpsid(value, required=True)
    if kind == 'email':
        return kind, normalize_email(value, required=True)
    return kind, normalize_phone(value, required=True)


def mask_proxy(proxy_type: str, value: str) -> str:
    if proxy_type == 'hkid':
        return mask_hkid(value)
    if proxy_type == 'fpsid':
        return mask_fpsid(value)
    if proxy_type == 'email':
        return mask_email(value)
    if proxy_type == 'phone':
        return mask_phone(value)
    return '****'


def proxy_key(proxy_type: str, value: str) -> str:
    return '%s:%s' % (proxy_type, value)


def fingerprint_for(userid: str, proxy_type: str, proxy: str, bank: str, account: str) -> str:
    material = '|'.join((userid, proxy_type, proxy, bank, account))
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def mask_e2e(value: Any) -> str:
    text = str(value or '').strip().upper()
    if len(text) < 8:
        return '****'
    prefix = re.match(r'[A-Z]+', text)
    head = prefix.group(0) if prefix else text[:4]
    return head + '****' + text[-4:]


def compose_e2e(rail: str, cycle_date: str, sequence: int) -> str:
    kind = normalize_rail(rail)
    if not re.fullmatch(r'\d{8}', str(cycle_date or '')):
        raise FpsError('invalid_e2e', 'Cycle date must be YYYYMMDD.')
    if sequence < 1 or sequence > 999999:
        raise FpsError('invalid_e2e', 'Sequence out of range.')
    return '%s%s%06d' % (E2E_PREFIX[kind], cycle_date, sequence)


def normalize_e2e(value: Any) -> str:
    text = str(value or '').strip().upper()
    if any(text.startswith(prefix) for prefix in BLOCKED_E2E_PREFIXES):
        raise FpsError('invalid_e2e', 'E2E ID belongs to another clearing system.')
    if not re.fullmatch(r'(FPS|CHATS|ECG)\d{8}\d{6}', text):
        raise FpsError('invalid_e2e', 'E2E ID must be FPS/CHATS/ECG + date + seq.')
    return text


def compose_fps1(
    *,
    rail: str,
    amount: str,
    bank_code: str,
    account_last4: str,
    e2e_id: str,
    purpose: str,
    value_date: str,
    proxy_type: str,
    proxy_mask: str,
    ofac_hit: bool,
    sequence: int,
    source_bank: str = DEFAULT_SOURCE_BANK,
    currency: str = 'HKD',
) -> str:
    clearing = CLEARING_BY_RAIL[normalize_rail(rail)]
    fields = [
        FPS1_VERSION,
        clearing,
        normalize_currency(currency, rail=rail),
        str(amount),
        bank_code or '000',
        account_last4 or '0000',
        e2e_id,
        purpose,
        value_date,
        proxy_type or 'none',
        proxy_mask or '****',
        'Y' if ofac_hit else 'N',
        str(int(sequence)),
        source_bank,
    ]
    if len(fields) != FPS1_FIELDS:
        raise FpsError('invalid_fps1', 'FPS1 record is the wrong width.')
    line = '|'.join(fields)
    if '<' in line or '>' in line:
        raise FpsError('invalid_fps1', 'FPS1 rejects XML markup.')
    return line


def parse_fps1(payload: Any) -> Dict[str, str]:
    text = str(payload or '').strip()
    upper = text.upper()
    if not text:
        raise FpsError('invalid_fps1', 'FPS1 payload is empty.')
    if '<?XML' in upper or '<!DOCTYPE' in upper or '<FPS' in upper or '<FAST' in upper:
        raise FpsError('invalid_fps1', 'FPS1 rejects XML / DOCTYPE payloads.')
    parts = text.split('|')
    if len(parts) != FPS1_FIELDS:
        raise FpsError('invalid_fps1', 'FPS1 must be a 14-field pipe record.')
    if parts[0] != FPS1_VERSION:
        raise FpsError('invalid_fps1', 'FPS1 version must be 1.')
    clearing = parts[1].upper()
    if clearing in REJECTED_CLEARING:
        raise FpsError('invalid_clearing', 'Clearing %s is not FPS/CHATS/ECG.' % clearing)
    if clearing not in {'FPS', 'CHATS', 'ECG'}:
        raise FpsError('invalid_clearing', 'Unknown FPS1 clearing.')
    currency = parts[2].upper()
    if currency not in {'HKD', 'CNY'}:
        raise FpsError('invalid_currency', 'FPS1 is HKD or CNY.')
    if clearing in {'CHATS', 'ECG'} and currency != 'HKD':
        raise FpsError('invalid_currency', 'CHATS and ECG FPS1 records are HKD only.')
    return {
        'version': parts[0],
        'clearing': clearing,
        'currency': currency,
        'amount': parts[3],
        'bank_code': parts[4],
        'account_last4': parts[5],
        'e2e_id': parts[6],
        'purpose': parts[7],
        'value_date': parts[8],
        'proxy_type': parts[9],
        'proxy_mask': parts[10],
        'ofac': parts[11],
        'sequence': parts[12],
        'source_bank': parts[13],
    }


def _tokens(name: str) -> set:
    return {part for part in re.split(r'[^A-Z0-9]+', str(name or '').upper()) if part}


def cop_outcome(registered: str, presented: str) -> str:
    left = _tokens(registered)
    right = _tokens(presented)
    if not left or not right:
        return COP_UNAVAILABLE
    if left == right:
        return COP_MATCH
    if left <= right or right <= left or len(left & right) >= 1:
        return COP_CLOSE
    return COP_NONE


@dataclass(frozen=True)
class FpsResolution:
    outcome: str
    registered_name: str
    presented_name: str
    proxy_type: str
    proxy_mask: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'outcome': self.outcome,
            'registered_name': self.registered_name if self.outcome in {COP_MATCH, COP_CLOSE} else '',
            'presented_name': self.presented_name,
            'proxy_type': self.proxy_type,
            'proxy_mask': self.proxy_mask,
            'match': self.outcome == COP_MATCH,
            'close_match': self.outcome == COP_CLOSE,
        }


def resolve_fps(
    proxy_type: Any,
    proxy: Any,
    presented_name: Any = '',
    *,
    directory: Optional[Dict[str, str]] = None,
) -> FpsResolution:
    kind, value = normalize_proxy(proxy_type, proxy, required=True)
    mapping = directory if directory is not None else DEFAULT_FPS_DIRECTORY
    registered = mapping.get(proxy_key(kind, value), '')
    presented = ' '.join(str(presented_name or '').strip().split())
    if not registered:
        outcome = COP_UNAVAILABLE
    elif not presented:
        outcome = COP_MATCH
        presented = registered
    else:
        outcome = cop_outcome(registered, presented)
    return FpsResolution(
        outcome=outcome,
        registered_name=registered,
        presented_name=presented,
        proxy_type=kind,
        proxy_mask=mask_proxy(kind, value),
    )


def convert_instructed_to_usd(amount: Decimal, rate: Decimal) -> Decimal:
    if rate <= 0:
        raise FpsError('invalid_fx', 'FX rate must be positive.')
    return (amount * rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def _observed_sunday(day: date) -> date:
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def hk_holidays(year: int) -> set:
    """Fixed HK-observed holidays. Moveable lunar dates are extra_holidays."""
    return {
        _observed_sunday(date(year, 1, 1)),
        _observed_sunday(date(year, 5, 1)),
        _observed_sunday(date(year, 7, 1)),
        _observed_sunday(date(year, 10, 1)),
        _observed_sunday(date(year, 12, 25)),
        _observed_sunday(date(year, 12, 26)),
    }


class HkClock:
    """Hong Kong FPS 24/7 vs CHATS/ECG banking-day clock. Injectable offset and holidays."""

    def __init__(
        self,
        *,
        chats_open_hour: int = 8,
        chats_open_minute: int = 30,
        chats_cutoff_hour: int = 18,
        chats_cutoff_minute: int = 0,
        tz_offset_minutes: int = HKT_OFFSET_MINUTES,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.chats_open_hour = chats_open_hour
        self.chats_open_minute = chats_open_minute
        self.chats_cutoff_hour = chats_cutoff_hour
        self.chats_cutoff_minute = chats_cutoff_minute
        self.tz = timezone(timedelta(minutes=tz_offset_minutes))
        extra = set()
        for item in extra_holidays:
            text = str(item).strip()
            try:
                extra.add(date.fromisoformat(text[:10]))
            except ValueError:
                continue
        self.extra_holidays = extra

    def local(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, tz=self.tz)

    def holidays(self, year: int) -> set:
        return hk_holidays(year) | {day for day in self.extra_holidays if day.year == year}

    def is_business_day(self, day: date) -> bool:
        if day.weekday() >= 5:
            return False
        return day not in self.holidays(day.year)

    def next_business_day(self, day: date) -> date:
        cursor = day
        while not self.is_business_day(cursor):
            cursor += timedelta(days=1)
        return cursor

    def is_after_chats_cutoff(self, ts: float) -> bool:
        local = self.local(ts)
        cutoff = local.replace(
            hour=self.chats_cutoff_hour, minute=self.chats_cutoff_minute, second=0, microsecond=0,
        )
        open_at = local.replace(
            hour=self.chats_open_hour, minute=self.chats_open_minute, second=0, microsecond=0,
        )
        return local < open_at or local >= cutoff

    def is_cny_night(self, ts: float) -> bool:
        local = self.local(ts)
        return local.hour >= 18 or local.hour < 8 or not self.is_business_day(local.date())

    def value_date(self, ts: float, *, rail: str = 'fps') -> date:
        local = self.local(ts)
        kind = normalize_rail(rail)
        day = local.date()
        if kind == 'fps':
            return day
        if kind == 'ecg':
            start = day + timedelta(days=1)
            return self.next_business_day(start)
        if not self.is_business_day(day) or self.is_after_chats_cutoff(ts):
            return self.next_business_day(day + timedelta(days=1))
        return day

    def cycle_date(self, ts: float, *, rail: str = 'fps') -> str:
        return self.value_date(ts, rail=rail).strftime('%Y%m%d')

    def snapshot(self, ts: float, *, rail: str = 'fps') -> Dict[str, Any]:
        local = self.local(ts)
        value = self.value_date(ts, rail=rail)
        kind = normalize_rail(rail)
        after = False
        if kind in {'chats', 'ecg'}:
            after = self.is_after_chats_cutoff(ts) or not self.is_business_day(local.date())
        return {
            'rail': kind,
            'timezone': 'HKT',
            'local': local.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'cutoff': '%02d:%02d' % (self.chats_cutoff_hour, self.chats_cutoff_minute),
            'after_cutoff': after,
            'always_on': kind == 'fps',
            'cny_night': self.is_cny_night(ts),
            'business_day': self.is_business_day(local.date()),
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
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def _env_money(name: str, default: str) -> Decimal:
    raw = os.environ.get(name, default)
    try:
        return parse_money(raw, allow_zero=True)
    except AmountError:
        return parse_money(default, allow_zero=True)


def _env_list(name: str) -> Tuple[str, ...]:
    raw = os.environ.get(name, '')
    parts = [item.strip() for item in str(raw).split(',') if item.strip()]
    return tuple(parts)


def _classify_money_result(result: Any) -> str:
    if result is None:
        return 'failed'
    if isinstance(result, Exception):
        return 'failed'
    if isinstance(result, str):
        low = result.strip().lower()
        if any(token in low for token in DEBIT_NSF):
            return 'nsf'
        if low in DEBIT_OK:
            return 'ok'
        return 'failed'
    return 'failed'


@dataclass
class FpsPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    allow_credit: bool = False
    require_cop_match: bool = True
    max_beneficiaries: int = 12
    max_payments: int = 120
    min_amount: Decimal = Decimal('0.01')
    fps_cap: Decimal = Decimal('1000000.00')
    ecg_cap: Decimal = Decimal('1000000.00')
    chats_cap: Decimal = Decimal('50000000.00')
    cny_night_cap: Decimal = Decimal('200000.00')
    outbound_fee: Decimal = Decimal('0.00')
    chats_fee: Decimal = Decimal('200.00')
    ecg_fee: Decimal = Decimal('5.00')
    dual_control_threshold: Decimal = Decimal('100000.00')
    hkdusd: Decimal = DEFAULT_HKDUSD
    cnyusd: Decimal = DEFAULT_CNYUSD
    chats_open_hour: int = 8
    chats_open_minute: int = 30
    chats_cutoff_hour: int = 18
    chats_cutoff_minute: int = 0
    tz_offset_minutes: int = HKT_OFFSET_MINUTES
    source_bank: str = DEFAULT_SOURCE_BANK
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'FpsPolicy':
        extra = _env_list('FPS_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('FPS_ENABLED', True),
            customer_manage=_env_bool('FPS_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('FPS_CUSTOMER_SEND', True),
            allow_credit=_env_bool('FPS_ALLOW_CREDIT', False),
            require_cop_match=_env_bool('FPS_REQUIRE_COP', True),
            max_beneficiaries=max(1, _env_int('FPS_MAX_BENEFICIARIES', 12)),
            max_payments=max(1, _env_int('FPS_MAX_PAYMENTS', 120)),
            min_amount=_env_money('FPS_MIN_AMOUNT', '0.01'),
            fps_cap=_env_money('FPS_CAP', '1000000.00'),
            ecg_cap=_env_money('FPS_ECG_CAP', '1000000.00'),
            chats_cap=_env_money('FPS_CHATS_CAP', '50000000.00'),
            cny_night_cap=_env_money('FPS_CNY_NIGHT_CAP', '200000.00'),
            outbound_fee=_env_money('FPS_FEE', '0.00'),
            chats_fee=_env_money('FPS_CHATS_FEE', '200.00'),
            ecg_fee=_env_money('FPS_ECG_FEE', '5.00'),
            dual_control_threshold=_env_money('FPS_DUAL_CONTROL', '100000.00'),
            hkdusd=_env_money('FPS_FX_HKDUSD', '0.13'),
            cnyusd=_env_money('FPS_FX_CNYUSD', '0.14'),
            chats_open_hour=max(0, min(23, _env_int('FPS_CHATS_OPEN_HOUR', 8))),
            chats_open_minute=max(0, min(59, _env_int('FPS_CHATS_OPEN_MINUTE', 30))),
            chats_cutoff_hour=max(0, min(23, _env_int('FPS_CUTOFF_HOUR', 18))),
            chats_cutoff_minute=max(0, min(59, _env_int('FPS_CUTOFF_MINUTE', 0))),
            tz_offset_minutes=_env_int('FPS_TZ_OFFSET', HKT_OFFSET_MINUTES),
            source_bank=normalize_bank_code(os.environ.get('FPS_SOURCE_BANK', DEFAULT_SOURCE_BANK)),
            watchlist=watch,
            extra_holidays=_env_list('FPS_HOLIDAYS'),
        )

    def cap_for(self, rail: str) -> Decimal:
        if rail == 'chats':
            return self.chats_cap
        if rail == 'ecg':
            return self.ecg_cap
        return self.fps_cap

    def fee_for(self, rail: str) -> Decimal:
        if rail == 'chats':
            return self.chats_fee
        if rail == 'ecg':
            return self.ecg_fee
        return self.outbound_fee

    def rate_for(self, currency: str) -> Decimal:
        if currency == 'CNY':
            return self.cnyusd
        return self.hkdusd


@dataclass
class FpsBeneficiary:
    beneficiary_id: str
    userid: str
    nickname: str
    legal_name: str
    proxy_type: str
    proxy: str
    bank_code: str
    account_number: str
    default_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float
    fingerprint: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'beneficiary_id': self.beneficiary_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'proxy_type': self.proxy_type,
            'proxy_mask': mask_proxy(self.proxy_type, self.proxy) if self.proxy_type else '',
            'bank_code': self.bank_code,
            'bank_name': bank_name(self.bank_code),
            'account_last4': last4(self.account_number) if self.account_number else '',
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
class FpsPayment:
    payment_id: str
    trace_id: str
    beneficiary_id: str
    userid: str
    internal_account: str
    rail: str
    currency: str
    instructed_amount: str
    usd_amount: str
    fx_rate: str
    fee: str
    fee_status: str
    nickname: str
    legal_name: str
    proxy_type: str
    proxy_mask: str
    bank_code: str
    account_last4: str
    purpose: str
    memo: str
    status: str
    e2e_id: str
    value_date: str
    cop_outcome: str
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
            'payment_id': self.payment_id,
            'trace_id': self.trace_id,
            'beneficiary_id': self.beneficiary_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'rail': self.rail,
            'currency': self.currency,
            'amount': self.instructed_amount,
            'instructed_amount': self.instructed_amount,
            'usd_amount': self.usd_amount,
            'fx_rate': self.fx_rate,
            'fee': self.fee,
            'fee_status': self.fee_status,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'proxy_type': self.proxy_type,
            'proxy_mask': self.proxy_mask,
            'bank_code': self.bank_code,
            'account_last4': self.account_last4,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'e2e_id': mask_e2e(self.e2e_id),
            'value_date': self.value_date,
            'cop_outcome': self.cop_outcome,
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
            'held': self.status == PN_HELD,
            'queued': self.status == PN_QUEUED,
            'pending_release': self.status == PN_PENDING,
            'sent': self.status == PN_SENT,
            'completed': self.status == PN_COMPLETED,
            'cancelable': self.status in CANCELABLE,
            'instant': self.rail == 'fps',
        }


def _clone_bene(row: FpsBeneficiary) -> FpsBeneficiary:
    return FpsBeneficiary(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_pay(row: FpsPayment) -> FpsPayment:
    return FpsPayment(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _bene_from_row(row: Any) -> FpsBeneficiary:
    return FpsBeneficiary(
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        proxy_type=row['proxy_type'] or '',
        proxy=row['proxy'] or '',
        bank_code=row['bank_code'] or '',
        account_number=row['account_number'] or '',
        default_account=row['default_account'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        fingerprint=row['fingerprint'] or '',
    )


def _pay_from_row(row: Any) -> FpsPayment:
    return FpsPayment(
        payment_id=row['payment_id'],
        trace_id=row['trace_id'],
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        rail=row['rail'],
        currency=row['currency'] if 'currency' in row.keys() else 'HKD',
        instructed_amount=row['instructed_amount'],
        usd_amount=row['usd_amount'],
        fx_rate=row['fx_rate'],
        fee=row['fee'],
        fee_status=row['fee_status'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        proxy_type=row['proxy_type'] or '',
        proxy_mask=row['proxy_mask'] or '',
        bank_code=row['bank_code'] or '',
        account_last4=row['account_last4'] or '',
        purpose=row['purpose'],
        memo=row['memo'] or '',
        status=row['status'],
        e2e_id=row['e2e_id'] or '',
        value_date=row['value_date'],
        cop_outcome=row['cop_outcome'] or '',
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


class MemoryFpsStore:
    def __init__(self) -> None:
        self._benes: Dict[str, FpsBeneficiary] = {}
        self._pays: Dict[str, FpsPayment] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_beneficiary(self, row: FpsBeneficiary) -> None:
        with self._lock:
            self._benes[row.beneficiary_id] = _clone_bene(row)

    def get_beneficiary(self, beneficiary_id: str) -> Optional[FpsBeneficiary]:
        with self._lock:
            row = self._benes.get(beneficiary_id)
            return _clone_bene(row) if row else None

    def update_beneficiary(self, row: FpsBeneficiary) -> None:
        self.put_beneficiary(row)

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[FpsBeneficiary]:
        with self._lock:
            rows = list(self._benes.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != BENE_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return [_clone_bene(row) for row in rows]

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[FpsBeneficiary]:
        wanted = nickname.strip().lower()
        for row in self.list_beneficiaries(userid, include_archived=False):
            if row.status in OPEN_BENE and row.nickname.lower() == wanted:
                return row
        return None

    def find_beneficiary_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[FpsBeneficiary]:
        for row in self.list_beneficiaries(userid, include_archived=False):
            if row.status in OPEN_BENE and row.fingerprint == fingerprint:
                return row
        return None

    def put_payment(self, row: FpsPayment) -> FpsPayment:
        with self._lock:
            existing_id = self._by_trace.get(row.trace_id)
            if existing_id is not None:
                return _clone_pay(self._pays[existing_id])
            self._pays[row.payment_id] = _clone_pay(row)
            self._by_trace[row.trace_id] = row.payment_id
            return _clone_pay(row)

    def update_payment(self, row: FpsPayment) -> None:
        with self._lock:
            self._pays[row.payment_id] = _clone_pay(row)
            self._by_trace[row.trace_id] = row.payment_id

    def get_payment(self, payment_id: str) -> Optional[FpsPayment]:
        with self._lock:
            row = self._pays.get(payment_id)
            return _clone_pay(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[FpsPayment]:
        with self._lock:
            payment_id = self._by_trace.get(trace_id)
            row = self._pays.get(payment_id) if payment_id else None
            return _clone_pay(row) if row else None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[FpsPayment]:
        with self._lock:
            rows = list(self._pays.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if beneficiary_id is not None:
            rows = [row for row in rows if row.beneficiary_id == beneficiary_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return [_clone_pay(row) for row in rows]

    def next_e2e_sequence(self, cycle_date: str, rail: str) -> int:
        prefix = E2E_PREFIX[normalize_rail(rail)] + cycle_date
        with self._lock:
            n = sum(1 for row in self._pays.values() if (row.e2e_id or '').startswith(prefix))
        return n + 1


class SqliteFpsStore:
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
                    proxy_type TEXT NOT NULL DEFAULT '',
                    proxy TEXT NOT NULL DEFAULT '',
                    bank_code TEXT NOT NULL DEFAULT '',
                    account_number TEXT NOT NULL DEFAULT '',
                    default_account TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    fingerprint TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    payment_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    beneficiary_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    internal_account TEXT NOT NULL,
                    rail TEXT NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'HKD',
                    instructed_amount TEXT NOT NULL,
                    usd_amount TEXT NOT NULL,
                    fx_rate TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    fee_status TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    proxy_type TEXT NOT NULL DEFAULT '',
                    proxy_mask TEXT NOT NULL DEFAULT '',
                    bank_code TEXT NOT NULL DEFAULT '',
                    account_last4 TEXT NOT NULL DEFAULT '',
                    purpose TEXT NOT NULL,
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    e2e_id TEXT NOT NULL DEFAULT '',
                    value_date TEXT NOT NULL,
                    cop_outcome TEXT NOT NULL DEFAULT '',
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

    def put_beneficiary(self, row: FpsBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO beneficiaries (
                    beneficiary_id, userid, nickname, legal_name, proxy_type, proxy,
                    bank_code, account_number, default_account, status, actor,
                    created_at, updated_at, fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.beneficiary_id, row.userid, row.nickname, row.legal_name,
                    row.proxy_type, row.proxy, row.bank_code, row.account_number,
                    row.default_account, row.status, row.actor, row.created_at,
                    row.updated_at, row.fingerprint,
                ),
            )
            conn.commit()

    def get_beneficiary(self, beneficiary_id: str) -> Optional[FpsBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM beneficiaries WHERE beneficiary_id = ?', (beneficiary_id,),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def update_beneficiary(self, row: FpsBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE beneficiaries SET nickname=?, legal_name=?, proxy_type=?, proxy=?,
                    bank_code=?, account_number=?, default_account=?, status=?,
                    actor=?, updated_at=?, fingerprint=?
                WHERE beneficiary_id=?
                """,
                (
                    row.nickname, row.legal_name, row.proxy_type, row.proxy,
                    row.bank_code, row.account_number, row.default_account, row.status,
                    row.actor, row.updated_at, row.fingerprint, row.beneficiary_id,
                ),
            )
            conn.commit()

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[FpsBeneficiary]:
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

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[FpsBeneficiary]:
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

    def find_beneficiary_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[FpsBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM beneficiaries
                WHERE userid = ? AND fingerprint = ?
                  AND status IN ('active', 'paused')
                """,
                (userid, fingerprint),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def put_payment(self, row: FpsPayment) -> FpsPayment:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM payments WHERE trace_id = ?', (row.trace_id,)
            ).fetchone()
            if existing is not None:
                return _pay_from_row(existing)
            conn.execute(
                """
                INSERT INTO payments (
                    payment_id, trace_id, beneficiary_id, userid, internal_account, rail,
                    currency, instructed_amount, usd_amount, fx_rate, fee, fee_status, nickname, legal_name,
                    proxy_type, proxy_mask, bank_code, account_last4, purpose, memo, status,
                    e2e_id, value_date, cop_outcome, actor, releaser, ofac_hit, ofac_match,
                    created_at, updated_at, sent_at, completed_at, recalled_at, note, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.payment_id, row.trace_id, row.beneficiary_id, row.userid,
                    row.internal_account, row.rail, row.currency, row.instructed_amount, row.usd_amount,
                    row.fx_rate, row.fee, row.fee_status, row.nickname, row.legal_name,
                    row.proxy_type, row.proxy_mask, row.bank_code, row.account_last4,
                    row.purpose, row.memo, row.status, row.e2e_id, row.value_date,
                    row.cop_outcome, row.actor, row.releaser, row.ofac_hit, row.ofac_match,
                    row.created_at, row.updated_at, row.sent_at, row.completed_at,
                    row.recalled_at, row.note, row.reason,
                ),
            )
            conn.commit()
            return row

    def update_payment(self, row: FpsPayment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE payments SET fee=?, fee_status=?, status=?, e2e_id=?,
                    value_date=?, cop_outcome=?, actor=?, releaser=?, ofac_hit=?,
                    ofac_match=?, updated_at=?, sent_at=?, completed_at=?,
                    recalled_at=?, note=?, reason=?
                WHERE payment_id=?
                """,
                (
                    row.fee, row.fee_status, row.status, row.e2e_id, row.value_date,
                    row.cop_outcome, row.actor, row.releaser, row.ofac_hit, row.ofac_match,
                    row.updated_at, row.sent_at, row.completed_at, row.recalled_at,
                    row.note, row.reason, row.payment_id,
                ),
            )
            conn.commit()

    def get_payment(self, payment_id: str) -> Optional[FpsPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE payment_id = ?', (payment_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[FpsPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE trace_id = ?', (trace_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[FpsPayment]:
        sql = 'SELECT * FROM payments'
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
        return [_pay_from_row(row) for row in rows]

    def next_e2e_sequence(self, cycle_date: str, rail: str) -> int:
        prefix = E2E_PREFIX[normalize_rail(rail)] + cycle_date
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE e2e_id LIKE ?",
                (prefix + '%',),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class FpsService:
    def __init__(
        self,
        policy: FpsPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[HkClock] = None,
        directory: Optional[Dict[str, str]] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or HkClock(
            chats_open_hour=policy.chats_open_hour,
            chats_open_minute=getattr(policy, 'chats_open_minute', 30),
            chats_cutoff_hour=policy.chats_cutoff_hour,
            chats_cutoff_minute=policy.chats_cutoff_minute,
            tz_offset_minutes=policy.tz_offset_minutes,
            extra_holidays=policy.extra_holidays,
        )
        self.directory = dict(directory or DEFAULT_FPS_DIRECTORY)

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise FpsError('fps_disabled', 'FPS / CHATS is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise FpsError('fps_forbidden', 'Customers cannot manage FPS beneficiaries.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise FpsError('fps_forbidden', 'Customers cannot originate FPS payments.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise FpsError('fps_forbidden', 'Staff only.')

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
            raise FpsError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise FpsError('credit_not_allowed', 'Credit accounts cannot originate FPS.')

    def _assert_amount(self, dollars: Decimal, rail: str, *, currency: str = 'HKD') -> None:
        cap = self.policy.cap_for(rail)
        if currency == 'CNY' and rail == 'fps' and self.calendar.is_cny_night(float(self.clock())):
            cap = min(cap, self.policy.cny_night_cap)
        if dollars < self.policy.min_amount or dollars > cap:
            raise FpsError('amount_out_of_range', 'Amount is outside the %s cap.' % rail)

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal, rail: str) -> bool:
        if rail in {'fps'}:
            return amount >= self.policy.dual_control_threshold
        return amount >= self.policy.dual_control_threshold

    def lookup(
        self,
        *,
        actor: str,
        actor_type: str,
        proxy_type: Any,
        proxy: Any,
        presented_name: Any = '',
    ) -> FpsResolution:
        self._require_send(actor_type)
        _ = actor
        return resolve_fps(proxy_type, proxy, presented_name, directory=self.directory)

    def quote_fx(self, amount: Any, *, rail: Any = 'fps', currency: Any = 'HKD') -> Dict[str, Any]:
        kind = normalize_rail(rail)
        currency = normalize_currency(currency, rail=kind)
        instructed = parse_money(amount)
        self._assert_amount(instructed, kind, currency=currency)
        rate = self.policy.rate_for(currency)
        usd = convert_instructed_to_usd(instructed, rate)
        fee = compute_fee(instructed, self.policy.fee_for(kind))
        return {
            'rail': kind,
            'currency': currency,
            'instructed_amount': money_str(instructed),
            'usd_amount': money_str(usd),
            'fx_rate': money_str(rate),
            'fee': money_str(fee),
            'usd_total': money_str(usd + convert_instructed_to_usd(fee, rate) if fee else usd),
        }

    def add_beneficiary(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        default_account: Any,
        proxy_type: Any = '',
        proxy: Any = '',
        bank_code: Any = '',
        account_number: Any = '',
    ) -> FpsBeneficiary:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise FpsError('fps_forbidden', 'Not allowed to add beneficiaries for this customer.')
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        kind, value = normalize_proxy(proxy_type, proxy, required=False)
        bank = normalize_bank_code(bank_code, required=False)
        external = normalize_external_account(account_number, required=False)
        if not kind and not (bank and external):
            raise FpsError('invalid_proxy', 'FPS proxy or HKMA bank + account is required.')
        if self.store.find_beneficiary_by_nickname(owner_userid, name) is not None:
            raise FpsError('beneficiary_duplicate', 'A beneficiary with that nickname already exists.')
        fingerprint = fingerprint_for(owner_userid, kind, value, bank, external)
        if self.store.find_beneficiary_by_fingerprint(owner_userid, fingerprint) is not None:
            raise FpsError('beneficiary_duplicate', 'That FPS destination is already on file.')
        open_rows = [row for row in self.store.list_beneficiaries(owner_userid) if row.status in OPEN_BENE]
        if len(open_rows) >= self.policy.max_beneficiaries:
            raise FpsError('beneficiary_limit', 'FPS beneficiary limit reached.')
        now = float(self.clock())
        row = FpsBeneficiary(
            beneficiary_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            proxy_type=kind,
            proxy=value,
            bank_code=bank,
            account_number=external,
            default_account=account,
            status=BENE_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
            fingerprint=fingerprint,
        )
        self.store.put_beneficiary(row)
        return row

    def get_beneficiary(self, *, beneficiary_id: str, actor: str, actor_type: str) -> FpsBeneficiary:
        self._require_enabled()
        row = self.store.get_beneficiary(beneficiary_id)
        if row is None:
            raise FpsError('beneficiary_not_found', 'FPS beneficiary not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise FpsError('fps_forbidden', 'Not allowed to view this beneficiary.')
        return row

    def enforce_beneficiary(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> FpsBeneficiary:
        row = self.get_beneficiary(beneficiary_id=beneficiary_id, actor=actor, actor_type=actor_type)
        if row.status == BENE_ARCHIVED:
            raise FpsError('already_archived', 'Beneficiary is archived.')
        if row.status == BENE_PAUSED:
            raise FpsError('beneficiary_paused', 'Beneficiary is paused.')
        if require_active and row.status != BENE_ACTIVE:
            raise FpsError('invalid_status', 'Beneficiary is not active.')
        return row

    def set_beneficiary_status(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> FpsBeneficiary:
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
            raise FpsError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == BENE_ARCHIVED:
            raise FpsError('already_archived', 'Beneficiary is already archived.')
        if wanted == BENE_PAUSED:
            if row.status == BENE_PAUSED:
                raise FpsError('already_paused', 'Beneficiary is already paused.')
            if row.status != BENE_ACTIVE:
                raise FpsError('invalid_status', 'Only an active beneficiary can be paused.')
        elif wanted == BENE_ACTIVE:
            if row.status == BENE_ACTIVE:
                raise FpsError('already_active', 'Beneficiary is already active.')
            if row.status != BENE_PAUSED:
                raise FpsError('invalid_status', 'Only a paused beneficiary can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_beneficiary(row)
        return row

    def _cop_for(self, bene: FpsBeneficiary, *, presented_name: Any = '', override: bool = False) -> FpsResolution:
        if not bene.proxy_type:
            return FpsResolution(
                outcome=COP_UNAVAILABLE, registered_name='', presented_name=str(presented_name or ''),
                proxy_type='', proxy_mask='',
            )
        resolution = resolve_fps(
            bene.proxy_type, bene.proxy, presented_name or bene.legal_name, directory=self.directory,
        )
        if override:
            return resolution
        if self.policy.require_cop_match and resolution.outcome in {COP_NONE, COP_UNAVAILABLE} and bene.proxy_type:
            raise FpsError(
                'cop_mismatch',
                'Confirmation of Payee did not match.',
                cop=resolution,
            )
        return resolution

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        beneficiary_id: Any,
        amount: Any,
        rail: Any = 'fps',
        internal_account: Any = None,
        waive_fee: bool = False,
        presented_name: Any = '',
        currency: Any = 'HKD',
    ) -> Dict[str, Any]:
        self._require_send(actor_type)
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise FpsError('fps_forbidden', 'Beneficiary does not belong to this customer.')
        kind = normalize_rail(rail)
        if kind == 'fps' and not bene.proxy_type and not (bene.bank_code and bene.account_number):
            raise FpsError('invalid_proxy', 'FPS overlay requires a registered proxy or HKMA bank + account.')
        if kind in {'chats', 'ecg'} and not (bene.bank_code and bene.account_number) and not bene.proxy_type:
            raise FpsError('invalid_bank', 'CHATS/ECG needs a bank+account or FPS proxy.')
        ccy = normalize_currency(currency, rail=kind)
        instructed = parse_money(amount)
        self._assert_amount(instructed, kind, currency=ccy)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(instructed, self.policy.fee_for(kind), waived=staff_waive)
        rate = self.policy.rate_for(ccy)
        usd = convert_instructed_to_usd(instructed, rate)
        now = float(self.clock())
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        cop = self._cop_for(bene, presented_name=presented_name)
        clock = self.calendar.snapshot(now, rail=kind)
        return {
            'amount': money_str(instructed),
            'instructed_amount': money_str(instructed),
            'currency': ccy,
            'usd_amount': money_str(usd),
            'fx_rate': money_str(rate),
            'fee': money_str(fee),
            'total_usd': money_str(usd + convert_instructed_to_usd(fee, rate)),
            'internal_account': account,
            'rail': kind,
            'beneficiary': bene.to_dict(),
            'ofac': ofac.to_dict(),
            'cop': cop.to_dict(),
            'dual_control': self._needs_dual_control(instructed, kind),
            'clock': clock,
        }

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        bene: FpsBeneficiary,
        account: str,
        rail: str,
        currency: str,
        instructed: Decimal,
        usd: Decimal,
        fee: Decimal,
        purpose: str,
        memo: str,
        trace_id: str,
        ofac: ScreenResult,
        cop: FpsResolution,
        waive_fee: bool,
    ) -> FpsPayment:
        now = float(self.clock())
        value = self.calendar.cycle_date(now, rail=rail)
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        clock = self.calendar.snapshot(now, rail=rail)
        if ofac.hit:
            status = PN_HELD
        elif self._needs_dual_control(instructed, rail):
            status = PN_PENDING
        elif rail == 'ecg' or clock['after_cutoff']:
            status = PN_QUEUED
        elif rail == 'fps':
            status = PN_COMPLETED
        else:
            status = PN_SENT
        payment = FpsPayment(
            payment_id=uuid.uuid4().hex,
            trace_id=trace_id,
            beneficiary_id=bene.beneficiary_id,
            userid=owner_userid,
            internal_account=account,
            rail=rail,
            currency=currency,
            instructed_amount=money_str(instructed),
            usd_amount=money_str(usd),
            fx_rate=money_str(self.policy.rate_for(currency)),
            fee=money_str(fee),
            fee_status=fee_status,
            nickname=bene.nickname,
            legal_name=bene.legal_name,
            proxy_type=bene.proxy_type,
            proxy_mask=mask_proxy(bene.proxy_type, bene.proxy) if bene.proxy_type else '',
            bank_code=bene.bank_code,
            account_last4=last4(bene.account_number) if bene.account_number else '',
            purpose=purpose,
            memo=memo,
            status=status if status in {PN_HELD, PN_PENDING, PN_QUEUED} else PN_SENT,
            e2e_id='',
            value_date=value,
            cop_outcome=cop.outcome,
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
        )
        if status in {PN_SENT, PN_COMPLETED}:
            self._transmit(payment, actor=actor, complete=status == PN_COMPLETED)
        return payment

    def _transmit(self, payment: FpsPayment, *, actor: str, complete: bool = False) -> FpsPayment:
        now = float(self.clock())
        cycle = payment.value_date or self.calendar.cycle_date(now, rail=payment.rail)
        seq = self.store.next_e2e_sequence(cycle, payment.rail)
        payment.e2e_id = compose_e2e(payment.rail, cycle, seq)
        usd = parse_money(payment.usd_amount)
        fee_instructed = parse_money(payment.fee, allow_zero=True)
        fee_usd = convert_instructed_to_usd(fee_instructed, parse_money(payment.fx_rate, allow_zero=False)) if fee_instructed else Decimal('0.00')
        remark = 'fps to %s' % payment.nickname
        status = PN_SENT
        fail_note = ''
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(payment.internal_account, money_str(usd), remark)
            except Exception as exc:
                status = PN_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = PN_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = PN_FAILED
                    fail_note = str(result)[:240]
        if status == PN_SENT and fee_usd > 0 and payment.fee_status != FEE_WAIVED and self.debit_fn is not None:
            try:
                fee_result = self.debit_fn(payment.internal_account, money_str(fee_usd), 'fps fee %s' % mask_e2e(payment.e2e_id))
            except Exception:
                payment.fee_status = FEE_NSF
            else:
                kind = _classify_money_result(fee_result)
                payment.fee_status = FEE_COLLECTED if kind == 'ok' else FEE_NSF
        elif status == PN_SENT and (fee_usd == 0 or payment.fee_status == FEE_WAIVED):
            payment.fee_status = FEE_WAIVED if payment.fee_status == FEE_WAIVED or fee_usd == 0 else payment.fee_status
        payment.updated_at = now
        if status == PN_SENT:
            payment.sent_at = now
            payment.releaser = str(actor)
            if complete or payment.rail == 'fps':
                payment.status = PN_COMPLETED
                payment.completed_at = now
            else:
                payment.status = PN_SENT
        else:
            payment.status = status
            payment.e2e_id = ''
            payment.note = fail_note
        return payment

    def originate(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        beneficiary_id: Any,
        amount: Any,
        rail: Any = 'fps',
        internal_account: Any = None,
        purpose: Any = 'other',
        memo: Any = '',
        trace_id: Any = None,
        waive_fee: bool = False,
        presented_name: Any = '',
        cop_override: bool = False,
        currency: Any = 'HKD',
    ) -> Tuple[FpsPayment, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise FpsError('fps_forbidden', 'Not allowed to originate FPS for this customer.')
        staff_override = bool(cop_override) and actor_type in EMPLOYEE_ROLES
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise FpsError('fps_forbidden', 'Beneficiary does not belong to this customer.')
        kind = normalize_rail(rail)
        if kind == 'fps' and not bene.proxy_type and not (bene.bank_code and bene.account_number):
            raise FpsError('invalid_proxy', 'FPS overlay requires a registered proxy or HKMA bank + account.')
        ccy = normalize_currency(currency, rail=kind)
        instructed = parse_money(amount)
        self._assert_amount(instructed, kind, currency=ccy)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(instructed, self.policy.fee_for(kind), waived=staff_waive)
        usd = convert_instructed_to_usd(instructed, self.policy.rate_for(ccy))
        trace = normalize_id(trace_id)
        existing = self.store.get_payment_by_trace(trace)
        if existing is not None:
            return existing, False
        if len(self.store.list_payments(owner_userid)) >= self.policy.max_payments:
            raise FpsError('fps_limit', 'FPS history limit reached.')
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        cop = self._cop_for(bene, presented_name=presented_name, override=staff_override)
        if staff_override and cop.outcome in {COP_NONE, COP_UNAVAILABLE}:
            cop = FpsResolution(
                outcome=COP_MATCH, registered_name=bene.legal_name,
                presented_name=str(presented_name or bene.legal_name),
                proxy_type=bene.proxy_type,
                proxy_mask=mask_proxy(bene.proxy_type, bene.proxy) if bene.proxy_type else '',
            )
        payment = self._place(
            owner_userid=owner_userid,
            actor=actor,
            bene=bene,
            account=account,
            rail=kind,
            currency=ccy,
            instructed=instructed,
            usd=usd,
            fee=fee,
            purpose=normalize_purpose(purpose),
            memo=normalize_note(memo, limit=140),
            trace_id=trace,
            ofac=ofac,
            cop=cop,
            waive_fee=staff_waive,
        )
        stored = self.store.put_payment(payment)
        if stored.payment_id != payment.payment_id:
            return stored, False
        if stored.status == PN_NSF:
            raise FpsError('nsf', 'Insufficient funds for FPS.', payment=stored)
        if stored.status == PN_FAILED:
            raise FpsError('failed', 'FPS debit did not complete.', payment=stored)
        return stored, True

    def get_payment(self, *, payment_id: str, actor: str, actor_type: str) -> FpsPayment:
        self._require_enabled()
        row = self.store.get_payment(payment_id)
        if row is None:
            raise FpsError('payment_not_found', 'FPS payment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise FpsError('fps_forbidden', 'Not allowed to view this payment.')
        return row

    def cancel_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> FpsPayment:
        self._require_send(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise FpsError('fps_forbidden', 'Not allowed to cancel this payment.')
        if row.status not in CANCELABLE:
            raise FpsError('not_cancelable', 'Payment can no longer be cancelled.')
        row.status = PN_CANCELLED
        row.actor = str(actor)
        row.note = normalize_note(note)
        row.updated_at = float(self.clock())
        self.store.update_payment(row)
        return row

    def release_payment(self, *, payment_id: str, actor: str, actor_type: str) -> FpsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.ofac_hit:
            raise FpsError('ofac_hold', 'OFAC hold must be overridden first.')
        if row.status not in {PN_PENDING, PN_QUEUED}:
            raise FpsError('not_releasable', 'Payment is not waiting for release.')
        if row.status == PN_PENDING and row.actor == str(actor):
            raise FpsError('same_approver', 'Maker cannot release their own payment.')
        complete = row.rail == 'fps'
        self._transmit(row, actor=actor, complete=complete)
        if row.status == PN_NSF:
            self.store.update_payment(row)
            raise FpsError('nsf', 'Insufficient funds for FPS.', payment=row)
        if row.status == PN_FAILED:
            self.store.update_payment(row)
            raise FpsError('failed', 'FPS debit did not complete.', payment=row)
        self.store.update_payment(row)
        return row

    def reject_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> FpsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status not in {PN_HELD, PN_QUEUED, PN_PENDING}:
            raise FpsError('not_rejectable', 'Payment cannot be rejected.')
        row.status = PN_REJECTED
        row.reason = normalize_note(reason, limit=40) or 'other'
        row.note = normalize_note(note)
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_payment(row)
        return row

    def complete_payment(self, *, payment_id: str, actor: str, actor_type: str) -> FpsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status == PN_COMPLETED:
            raise FpsError('already_completed', 'Payment is already completed.')
        if row.status != PN_SENT:
            raise FpsError('not_completable', 'Only a sent payment can be completed.')
        now = float(self.clock())
        row.status = PN_COMPLETED
        row.completed_at = now
        row.updated_at = now
        row.actor = str(actor)
        self.store.update_payment(row)
        return row

    def recall_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> FpsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status == PN_RECALLED:
            raise FpsError('already_recalled', 'Payment is already recalled.')
        if row.status not in {PN_SENT, PN_COMPLETED}:
            raise FpsError('not_recallable', 'Payment cannot be recalled.')
        now = float(self.clock())
        window = STAFF_COMPLAINT_DAYS * 24 * 60 * 60
        sent_at = row.sent_at or row.created_at
        if row.rail in {'fps'} and (now - sent_at) > window:
            raise FpsError('not_recallable', 'FPS recall window has closed.')
        usd = parse_money(row.usd_amount)
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(row.internal_account, money_str(usd), 'fps recall %s' % mask_e2e(row.e2e_id))
            except Exception as exc:
                raise FpsError('recall_failed', 'Recall credit did not complete.', payment=row) from exc
            if _classify_money_result(result) != 'ok':
                raise FpsError('recall_failed', 'Recall credit did not complete.', payment=row)
        row.status = PN_RECALLED
        row.recalled_at = now
        row.updated_at = now
        row.actor = str(actor)
        row.note = normalize_note(note)
        self.store.update_payment(row)
        return row

    def override_ofac(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> FpsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status != PN_HELD:
            raise FpsError('not_releasable', 'No OFAC hold to override.')
        row.ofac_hit = 0
        row.note = normalize_note(note)
        complete = row.rail == 'fps'
        if self._needs_dual_control(parse_money(row.instructed_amount), row.rail):
            row.status = PN_PENDING
            row.updated_at = float(self.clock())
            self.store.update_payment(row)
            return row
        clock = self.calendar.snapshot(float(self.clock()), rail=row.rail)
        if clock['after_cutoff']:
            row.status = PN_QUEUED
            row.updated_at = float(self.clock())
            self.store.update_payment(row)
            return row
        self._transmit(row, actor=actor, complete=complete)
        self.store.update_payment(row)
        return row

    def override_cop(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> FpsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        row.cop_outcome = COP_MATCH
        row.note = normalize_note(note)
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_payment(row)
        return row

    def waive_fee(self, *, payment_id: str, actor: str, actor_type: str) -> FpsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status not in CANCELABLE | {PN_SENT}:
            raise FpsError('not_releasable', 'Fee can no longer be waived.')
        row.fee_status = FEE_WAIVED
        row.fee = money_str(Decimal('0.00'))
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_payment(row)
        return row

    def run_due(self, userid: str) -> List[FpsPayment]:
        now = float(self.clock())
        due = []
        for row in self.store.list_payments(userid):
            if row.status != PN_QUEUED:
                continue
            clock = self.calendar.snapshot(now, rail=row.rail)
            if clock['after_cutoff']:
                continue
            complete = row.rail == 'fps'
            self._transmit(row, actor=row.actor or 'system', complete=complete)
            self.store.update_payment(row)
            due.append(row)
        return due

    def export_fps1(self, userid: str, *, actor: str, actor_type: str) -> str:
        self._require_staff(actor_type)
        _ = actor
        lines = []
        for index, row in enumerate(self.store.list_payments(userid), start=1):
            if row.status not in {PN_SENT, PN_COMPLETED}:
                continue
            lines.append(compose_fps1(
                rail=row.rail,
                amount=row.instructed_amount,
                bank_code=row.bank_code or self.policy.source_bank,
                account_last4=row.account_last4,
                e2e_id=row.e2e_id,
                purpose=row.purpose,
                value_date=row.value_date,
                proxy_type=row.proxy_type,
                proxy_mask=row.proxy_mask,
                ofac_hit=bool(row.ofac_hit),
                sequence=index,
                source_bank=self.policy.source_bank,
                currency=row.currency,
            ))
        return '\n'.join(lines)

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self._require_enabled()
        _ = actor
        if actor_type not in EMPLOYEE_ROLES and actor and actor != userid:
            raise FpsError('fps_forbidden', 'Not allowed to view this FPS book.')
        benes = self.store.list_beneficiaries(userid)
        pays = self.store.list_payments(userid)
        ytd = Decimal('0.00')
        ytd_fees = Decimal('0.00')
        recalled = Decimal('0.00')
        for row in pays:
            if row.status in {PN_SENT, PN_COMPLETED}:
                ytd += parse_money(row.instructed_amount, allow_zero=True)
                ytd_fees += parse_money(row.fee, allow_zero=True)
            if row.status == PN_RECALLED:
                recalled += parse_money(row.instructed_amount, allow_zero=True)
        now = float(self.clock())
        return {
            'enabled': True,
            'beneficiaries': [row.to_dict() for row in benes],
            'payments': [row.to_dict() for row in pays],
            'ytd_sent': money_str(ytd),
            'ytd_fees': money_str(ytd_fees),
            'recalled_ytd': money_str(recalled),
            'active_count': sum(1 for row in benes if row.status == BENE_ACTIVE),
            'open_count': sum(1 for row in pays if row.status in OPEN_PN),
            'fx_rate': money_str(self.policy.hkdusd),
            'cny_fx_rate': money_str(self.policy.cnyusd),
            'clock': self.calendar.snapshot(now, rail='fps'),
            'chats_clock': self.calendar.snapshot(now, rail='chats'),
        }


_SERVICE: Optional[FpsService] = None


def set_service(service: Optional[FpsService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[FpsService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('FPS_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryFpsStore()
    path = os.environ.get('FPS_DB', DEFAULT_STORE_PATH)
    return SqliteFpsStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[HkClock] = None,
    directory: Optional[Dict[str, str]] = None,
) -> FpsService:
    if store is None:
        store = default_store()
    return FpsService(
        FpsPolicy.from_env(),
        store,
        clock=clock,
        debit_fn=debit_fn,
        credit_fn=credit_fn,
        accounts_fn=accounts_fn,
        screen_fn=screen_fn,
        calendar=calendar,
        directory=directory,
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
        'fps_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_completed': 409,
        'already_recalled': 409,
        'nsf': 409,
        'failed': 409,
        'recall_failed': 409,
        'fps_forbidden': 403,
        'fps_disabled': 403,
        'beneficiary_paused': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'cop_mismatch': 403,
        'not_cancelable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_completable': 403,
        'not_recallable': 403,
        'beneficiary_not_found': 404,
        'payment_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_bank': 400,
        'invalid_hkid': 400,
        'invalid_fpsid': 400,
        'invalid_phone': 400,
        'invalid_hkid': 400,
        'invalid_fpsid': 400,
        'invalid_email': 400,
        'invalid_proxy': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_e2e': 400,
        'invalid_fps1': 400,
        'invalid_clearing': 400,
        'invalid_currency': 400,
        'invalid_rail': 400,
        'invalid_fx': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_beneficiary': 400,
        'missing_payment': 400,
    }.get(code, 400)


def _error_body(exc: FpsError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('payment') is not None:
        body['payment'] = exc.extra['payment'].to_dict()
    if exc.extra.get('beneficiary') is not None:
        body['beneficiary'] = exc.extra['beneficiary'].to_dict()
    if exc.extra.get('cop') is not None:
        body['cop'] = exc.extra['cop'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except FpsError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: FpsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Fps': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_lookup(service: FpsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        resolution = service.lookup(
            actor=userid,
            actor_type=actor_type,
            proxy_type=values.get('proxy_type') or values.get('fps_type'),
            proxy=values.get('proxy') or values.get('fps') or values.get('hkid') or values.get('fpsid') or values.get('phone'),
            presented_name=values.get('legal_name') or values.get('name') or '',
        )
        return jsonify({'cop': resolution.to_dict()}), 200

    return _handle_errors(_run)


def handle_quote(service: FpsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}

    def _run():
        quote = service.quote_fx(
            values.get('amount'),
            rail=values.get('rail') or 'fps',
            currency=values.get('currency') or 'HKD',
        )
        return jsonify({'quote': quote}), 200

    return _handle_errors(_run)


def handle_add_beneficiary(service: FpsService):
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
            default_account=values.get('default_account') or values.get('account') or values.get('from_account'),
            proxy_type=values.get('proxy_type') or values.get('fps_type'),
            proxy=values.get('proxy') or values.get('fps') or values.get('hkid') or values.get('fpsid') or values.get('phone'),
            bank_code=values.get('bank_code') or values.get('bank'),
            account_number=values.get('account_number') or values.get('external_account'),
        )
        return jsonify({
            'message': 'FPS beneficiary added',
            'beneficiary': row.to_dict(),
            'Fps': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _bene_status_route(service: FpsService, status: str, ok_message: str):
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
            'Fps': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: FpsService):
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
            rail=values.get('rail') or 'fps',
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            waive_fee=bool(values.get('waive_fee')),
            presented_name=values.get('legal_name') or values.get('name') or '',
            currency=values.get('currency') or 'HKD',
        )
        return jsonify({'preview': preview, 'Fps': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: FpsService):
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
        payment, created = service.originate(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            beneficiary_id=beneficiary_id,
            amount=values.get('amount'),
            rail=values.get('rail') or 'fps',
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
            presented_name=values.get('legal_name') or values.get('name') or '',
            cop_override=bool(values.get('cop_override')),
            currency=values.get('currency') or 'HKD',
        )
        return jsonify({
            'message': 'FPS originated' if created else 'FPS already posted',
            'payment': payment.to_dict(),
            'Fps': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: FpsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    payment_id = str(values.get('payment_id') or '').strip()
    if not payment_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_payment'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        payment = service.cancel_payment(
            payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': 'FPS cancelled',
            'payment': payment.to_dict(),
            'Fps': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_route(service: FpsService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    payment_id = str(values.get('payment_id') or '').strip()
    if not payment_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_payment'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'release':
            payment = service.release_payment(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'FPS released'
        elif action == 'reject':
            payment = service.reject_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'FPS rejected'
        elif action == 'complete':
            payment = service.complete_payment(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'FPS completed'
        elif action == 'recall':
            payment = service.recall_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'FPS recalled'
        elif action == 'override':
            payment = service.override_ofac(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'cop':
            payment = service.override_cop(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Confirmation of Payee overridden'
        elif action == 'waive':
            payment = service.waive_fee(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'FPS fee waived'
        else:
            raise FpsError('invalid_status', 'Unknown FPS action.')
        return jsonify({
            'message': message,
            'payment': payment.to_dict(),
            'Fps': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: FpsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Fps': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_export(service: FpsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid

    def _run():
        payload = service.export_fps1(owner, actor=userid, actor_type=actor_type)
        return jsonify({'fps1': payload, 'Fps': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def attach_fps_routes(app, service: FpsService) -> None:
    @app.route('/listFps', methods=['POST', 'GET'])
    def list_fps_route():
        return handle_list(service)

    @app.route('/lookupFps', methods=['POST', 'GET'])
    def lookup_fps_route():
        return handle_lookup(service)

    @app.route('/quoteFpsFx', methods=['POST', 'GET'])
    def quote_fps_route():
        return handle_quote(service)

    @app.route('/addFpsBeneficiary', methods=['POST', 'GET'])
    def add_fps_beneficiary_route():
        return handle_add_beneficiary(service)

    @app.route('/pauseFpsBeneficiary', methods=['POST', 'GET'])
    def pause_fps_beneficiary_route():
        return _bene_status_route(service, BENE_PAUSED, 'FPS beneficiary paused')

    @app.route('/resumeFpsBeneficiary', methods=['POST', 'GET'])
    def resume_fps_beneficiary_route():
        return _bene_status_route(service, BENE_ACTIVE, 'FPS beneficiary resumed')

    @app.route('/archiveFpsBeneficiary', methods=['POST', 'GET'])
    def archive_fps_beneficiary_route():
        return _bene_status_route(service, BENE_ARCHIVED, 'FPS beneficiary archived')

    @app.route('/previewFps', methods=['POST', 'GET'])
    def preview_fps_route():
        return handle_preview(service)

    @app.route('/sendFps', methods=['POST', 'GET'])
    def send_fps_route():
        return handle_send(service)

    @app.route('/cancelFps', methods=['POST', 'GET'])
    def cancel_fps_route():
        return handle_cancel(service)

    @app.route('/releaseFps', methods=['POST', 'GET'])
    def release_fps_route():
        return _staff_route(service, 'release')

    @app.route('/rejectFps', methods=['POST', 'GET'])
    def reject_fps_route():
        return _staff_route(service, 'reject')

    @app.route('/completeFps', methods=['POST', 'GET'])
    def complete_fps_route():
        return _staff_route(service, 'complete')

    @app.route('/recallFps', methods=['POST', 'GET'])
    def recall_fps_route():
        return _staff_route(service, 'recall')

    @app.route('/overrideFpsOfac', methods=['POST', 'GET'])
    def override_fps_ofac_route():
        return _staff_route(service, 'override')

    @app.route('/overrideFpsCop', methods=['POST', 'GET'])
    def override_fps_cop_route():
        return _staff_route(service, 'cop')

    @app.route('/waiveFpsFee', methods=['POST', 'GET'])
    def waive_fps_fee_route():
        return _staff_route(service, 'waive')

    @app.route('/runDueFps', methods=['POST', 'GET'])
    def run_due_fps_route():
        return handle_run_due(service)

    @app.route('/exportFps', methods=['POST', 'GET'])
    def export_fps_route():
        return handle_export(service)
