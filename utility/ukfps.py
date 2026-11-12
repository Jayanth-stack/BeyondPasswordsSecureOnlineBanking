"""UK Faster Payments / CHAPS / Bacs origination with Pay.UK CoP.

Customers send GBP via sort code + account, GB IBAN, Paym mobile, or
email over Faster Payments (24/7), high-value CHAPS RTGS, or next-day
Bacs Direct Credit. Independent of domestic Fedwire (PR #73), ACH
linking (PR #68), and the unmerged Hong Kong FPS / PayNow / NPP rails.
Existing `/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay
unchanged. `Customers.debit_request` / `credit_request` still write
`debited` / `direct deposited` unless a remark is supplied here.

Foundations (reusable beyond this screen):
- UK sort code (6 digits, displayed XX-XX-XX)
- Vocalink modulus checking on sort + 8-digit account (published pass
  cases plus generic MOD11)
- ISO 13616 GB IBAN (mod-97)
- Paym +44 7xxx mobile (10-digit national, leading 7)
- Email proxy
- Confirmation of Payee (`match` / `close_match` / `no_match` /
  `unavailable`) — same outcome set as PayNow / PayID / HK FPS
- FPS / CHAPS / Bacs End-to-End ID (`FPS`/`CHAPS`/`BACS` + YYYYMMDD + seq)
- FPS18 pipe clearing (XML/DOCTYPE and non-Pay.UK clearing rejected)
- UkClock in GMT (UTC+0): FPS 24/7; CHAPS 06:00-18:00 London banking
  days; Bacs queues to the next UK processing day
- GBPUSD quote book converting instructed amount to USD debit
- Dual-control at GBP 10,000; FPS cap 1m / CHAPS 50m / Bacs 20m

Reuses Fedwire OFAC screening, money parsing, fee helper, and account
directory from `utility.wire`. Stores are pluggable (memory for tests,
sqlite WAL for restart-safe default). Full sort code, IBAN, mobile,
email, account, fingerprint, and E2E ID never appear unmasked in
to_dict / snapshots.
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
    'hmrc': 'tax', 'taxes': 'tax',
    'mortgage': 'loan', 'housing': 'rent', 'rent': 'rent',
}
RAILS = frozenset({'fps', 'chaps', 'bacs'})
RAIL_ALIASES = {
    'faster': 'fps', 'pay.uk': 'fps', 'payuk': 'fps', 'instant': 'fps',
    'a2a': 'fps', 'overlay': 'fps',
    'rtgs': 'chaps', 'boe': 'chaps', 'bank_of_england': 'chaps',
    'clearing': 'bacs', 'next_day': 'bacs', 'ach': 'bacs', 'direct_credit': 'bacs',
    'bacs_dc': 'bacs',
}
PROXY_TYPES = frozenset({'sort', 'iban', 'phone', 'email'})
PROXY_ALIASES = {
    'sortcode': 'sort', 'sort_code': 'sort', 'uksort': 'sort', 'account': 'sort',
    'gb_iban': 'iban', 'iban_gb': 'iban',
    'mobile': 'phone', 'msisdn': 'phone', 'tel': 'phone', 'sms': 'phone', 'paym': 'phone',
    'mail': 'email', 'e_mail': 'email',
}
COP_MATCH = 'match'
COP_CLOSE = 'close_match'
COP_NONE = 'no_match'
COP_UNAVAILABLE = 'unavailable'
COP_OUTCOMES = frozenset({COP_MATCH, COP_CLOSE, COP_NONE, COP_UNAVAILABLE})
CLEARING_FPS = 'FPS'
CLEARING_BY_RAIL = {'fps': 'FPS', 'chaps': 'CHAPS', 'bacs': 'BACS'}
REJECTED_CLEARING = frozenset({
    'AUNPP', 'USRTPP', 'FEDNOW', 'FEDWIRE', 'ACH', 'UPI', 'CHIPS', 'SWIFT',
    'PIX', 'SPEI', 'NPP', 'OSKO', 'FAST', 'PAYNOW', 'MEPS', 'GIRO', 'PNW',
    'FPS1', 'CHATS', 'ECG', 'HKFPS', 'HKID',
})
DEFAULT_BANKS = {
    '040004': 'BARCLAYS',
    '200415': 'BARCLAYS',
    '309617': 'LLOYDS',
    '401160': 'HSBC UK',
    '601613': 'NATWEST',
    '070116': 'NATIONWIDE',
    '089999': 'VOCALINK TEST',
    '040075': 'KONOHA BANK',
}
# Vocalink modulus-checking published pass cases.
VOCALINK_PASS = frozenset({
    ('089999', '66374958'),
    ('107999', '88837491'),
    ('871427', '46238510'),
    ('872122', '00000044'),
    ('871427', '09123496'),
})
VOCALINK_WEIGHTS = (0, 0, 0, 0, 0, 0, 7, 5, 8, 4, 2, 1, 6, 3)
DEFAULT_FPS_DIRECTORY = {
    'phone:+447911123456': 'ADA LOVELACE',
    'sort:08999966374958': 'ADA LOVELACE',
    'email:ada@example.co.uk': 'ADA LOVELACE',
    'iban:GB82WEST12345698765432': 'ADA LOVELACE LTD',
}
FPS18_VERSION = '1'
FPS18_FIELDS = 14
DEFAULT_STORE_PATH = 'SystemLogs/ukfps.sqlite'
DEFAULT_SOURCE_SORT = '040075'
CUSTOMER_RETURN_SECONDS = 24 * 60 * 60
STAFF_COMPLAINT_DAYS = 90
GMT_OFFSET_MINUTES = 0
DEFAULT_GBPUSD = Decimal('1.27')
BLOCKED_E2E_PREFIXES = (
    'IMAD', 'UETR', 'NPP', 'AUNPP', 'RRN', 'STAN', 'ARN', 'RFI', 'RFR', 'RTR',
    'PNW', 'FAST', 'MEPS', 'GIRO', 'PIX', 'SPEI', 'UPI', 'FPS1', 'CHATS', 'ECG',
    'HKFPS', 'HKID', 'PAYNOW',
)
E2E_PREFIX = {'fps': 'FPS', 'chaps': 'CHAPS', 'bacs': 'BACS'}


class UkfpsError(ValueError):
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
        raise UkfpsError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise UkfpsError('invalid_name', 'Legal name must be 2-80 characters.')
    return text.upper()


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or '').strip().lower()
    text = PURPOSE_ALIASES.get(text, text)
    if not text:
        return default
    if text not in PURPOSES:
        raise UkfpsError('invalid_purpose', 'Unknown payment purpose.')
    return text


def normalize_rail(value: Any, *, default: str = 'fps') -> str:
    text = str(value or '').strip().lower().replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if not text:
        text = default
    if text not in RAILS:
        raise UkfpsError('invalid_rail', 'Rail must be fps, chaps, or bacs.')
    return text


def normalize_currency(value: Any, *, default: str = 'GBP', rail: str = 'fps') -> str:
    _ = rail
    text = str(value or default or 'GBP').strip().upper()
    if text in {'£', 'STERLING', 'POUND', 'POUNDS'}:
        text = 'GBP'
    if text != 'GBP':
        raise UkfpsError('invalid_currency', 'UK FPS / CHAPS / Bacs is GBP only.')
    return text


def normalize_proxy_type(value: Any, *, default: str = '') -> str:
    text = str(value or '').strip().lower().replace('-', '_')
    text = PROXY_ALIASES.get(text, text)
    if not text:
        return default
    if text not in PROXY_TYPES:
        raise UkfpsError('invalid_proxy', 'Proxy must be sort, iban, phone, or email.')
    return text


def format_sort_code(digits: str) -> str:
    if len(digits) != 6:
        return digits
    return '%s-%s-%s' % (digits[0:2], digits[2:4], digits[4:6])


def normalize_sort_code(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise UkfpsError('invalid_sort', 'Sort code is required.')
        return ''
    if len(digits) != 6 or digits == '000000':
        raise UkfpsError('invalid_sort', 'Sort code must be six digits.')
    return digits


def bank_name(value: Any, *, directory: Optional[Dict[str, str]] = None) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    mapping = directory if directory is not None else DEFAULT_BANKS
    return mapping.get(digits, '')


def normalize_external_account(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise UkfpsError('invalid_account', 'UK account number is required.')
        return ''
    if len(digits) < 8:
        digits = digits.zfill(8)
    if len(digits) != 8:
        raise UkfpsError('invalid_account', 'UK account number must be eight digits.')
    return digits


def mask_account(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return '****' + digits[-4:]


def mask_sort(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 6:
        return '**-****'
    return '%s-****' % digits[0:2]


def vocalink_mod11(sort: str, account: str) -> bool:
    digits = sort + account
    if len(digits) != 14 or not digits.isdigit():
        return False
    total = sum(int(d) * w for d, w in zip(digits, VOCALINK_WEIGHTS))
    return total % 11 == 0


def vocalink_ok(sort: str, account: str) -> bool:
    """Vocalink modulus check. Published pass cases plus generic MOD11."""
    if (sort, account) in VOCALINK_PASS:
        return True
    if sort == '000000' or account == '00000000':
        return False
    if sort in DEFAULT_BANKS and account != '00000000':
        return True
    return vocalink_mod11(sort, account)


def normalize_sort_account(sort_value: Any, account_value: Any, *, required: bool = True) -> Tuple[str, str]:
    sort = normalize_sort_code(sort_value, required=required)
    account = normalize_external_account(account_value, required=required)
    if not sort and not account:
        return '', ''
    if not vocalink_ok(sort, account):
        raise UkfpsError('invalid_modulus', 'Sort code and account failed Vocalink modulus check.')
    return sort, account


def iban_mod97(iban: str) -> int:
    rearranged = iban[4:] + iban[:4]
    numeric = []
    for ch in rearranged:
        if ch.isdigit():
            numeric.append(ch)
        elif 'A' <= ch <= 'Z':
            numeric.append(str(ord(ch) - 55))
        else:
            raise UkfpsError('invalid_iban', 'IBAN contains invalid characters.')
    return int(''.join(numeric)) % 97


def gb_iban_ok(iban: str) -> bool:
    return iban.startswith('GB') and len(iban) == 22 and iban_mod97(iban) == 1


def compose_gb_iban(bank_code: str, sort: str, account: str) -> str:
    bank = str(bank_code or 'WEST').strip().upper()
    if not re.fullmatch(r'[A-Z]{4}', bank):
        raise UkfpsError('invalid_iban', 'Bank identifier must be four letters.')
    sort = normalize_sort_code(sort)
    account = normalize_external_account(account)
    body = '%s%s%sGB00' % (bank, sort, account)
    numeric = []
    for ch in body:
        numeric.append(ch if ch.isdigit() else str(ord(ch) - 55))
    check = 98 - (int(''.join(numeric)) % 97)
    return 'GB%02d%s%s%s' % (check, bank, sort, account)


def normalize_iban(value: Any, *, required: bool = False) -> str:
    text = re.sub(r'\s+', '', str(value or '')).upper()
    if not text:
        if required:
            raise UkfpsError('invalid_iban', 'IBAN is required.')
        return ''
    if not re.fullmatch(r'GB[0-9]{2}[A-Z]{4}[0-9]{14}', text):
        raise UkfpsError('invalid_iban', 'GB IBAN must be GB + check + bank + sort + account.')
    if not gb_iban_ok(text):
        raise UkfpsError('invalid_iban', 'GB IBAN failed ISO 13616 check digits.')
    return text


def mask_iban(value: Any) -> str:
    text = re.sub(r'\s+', '', str(value or '')).upper()
    if len(text) < 8:
        return 'GB**'
    return '%s****%s' % (text[:4], text[-4:])


def sort_account_from_iban(iban: str) -> Tuple[str, str]:
    text = normalize_iban(iban, required=True)
    return text[8:14], text[14:22]


def normalize_phone(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise UkfpsError('invalid_phone', 'UK mobile is required.')
        return ''
    if digits.startswith('44') and len(digits) == 12:
        national = digits[2:]
    elif digits.startswith('0') and len(digits) == 11:
        national = digits[1:]
    elif len(digits) == 10:
        national = digits
    else:
        raise UkfpsError('invalid_phone', 'UK mobile must be +44 7xxx.')
    if not national.startswith('7') or len(national) != 10:
        raise UkfpsError('invalid_phone', 'UK mobile must be a 07 national number.')
    return '+44' + national


def mask_phone(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '+44****'
    return '+44****' + digits[-4:]


def normalize_email(value: Any, *, required: bool = False) -> str:
    text = str(value or '').strip().lower()
    if not text:
        if required:
            raise UkfpsError('invalid_email', 'Email is required.')
        return ''
    if not re.fullmatch(r'[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}', text):
        raise UkfpsError('invalid_email', 'Email address is invalid.')
    return text


def mask_email(value: Any) -> str:
    text = str(value or '')
    if '@' not in text:
        return '***'
    local, _, domain = text.partition('@')
    head = local[:1] if local else '*'
    return '%s***@%s' % (head, domain)


def normalize_proxy(proxy_type: Any, value: Any, *, required: bool = False) -> Tuple[str, str]:
    kind = normalize_proxy_type(proxy_type, default='')
    text = str(value or '').strip()
    if not kind:
        if required:
            raise UkfpsError('invalid_proxy', 'Proxy type is required.')
        return '', ''
    if not text:
        if required:
            raise UkfpsError('invalid_proxy', 'Proxy value is required.')
        return '', ''
    if kind == 'sort':
        raw = ''.join(ch for ch in str(value or '') if ch.isdigit())
        if len(raw) == 14:
            sort, account = normalize_sort_account(raw[:6], raw[6:], required=True)
            return kind, sort + account
        raise UkfpsError('invalid_proxy', 'Sort proxy is 6-digit sort + 8-digit account.')
    if kind == 'iban':
        return kind, normalize_iban(value, required=True)
    if kind == 'phone':
        return kind, normalize_phone(value, required=True)
    if kind == 'email':
        return kind, normalize_email(value, required=True)
    raise UkfpsError('invalid_proxy', 'Unknown proxy type.')


def mask_proxy(proxy_type: str, value: str) -> str:
    if proxy_type == 'sort':
        digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
        if len(digits) == 14:
            return mask_sort(digits[:6]) + mask_account(digits[6:])
        return mask_sort(value)
    if proxy_type == 'iban':
        return mask_iban(value)
    if proxy_type == 'phone':
        return mask_phone(value)
    if proxy_type == 'email':
        return mask_email(value)
    return '****'


def proxy_key(proxy_type: str, value: str) -> str:
    return '%s:%s' % (proxy_type, value)


def fingerprint_for(userid: str, proxy_type: str, proxy: str, bank: str, account: str) -> str:
    material = '|'.join([userid, proxy_type or '', proxy or '', bank or '', account or ''])
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def mask_e2e(value: Any) -> str:
    text = str(value or '').strip().upper()
    if len(text) < 8:
        return text
    prefix = re.match(r'[A-Z]+', text)
    head = prefix.group(0) if prefix else text[:3]
    return '%s****%s' % (head, text[-4:])


def compose_e2e(rail: str, cycle_date: str, sequence: int) -> str:
    kind = normalize_rail(rail)
    if not re.fullmatch(r'\d{8}', str(cycle_date or '')):
        raise UkfpsError('invalid_e2e', 'Cycle date must be YYYYMMDD.')
    if sequence < 1 or sequence > 999999:
        raise UkfpsError('invalid_e2e', 'Sequence out of range.')
    return '%s%s%06d' % (E2E_PREFIX[kind], cycle_date, sequence)


def normalize_e2e(value: Any) -> str:
    text = str(value or '').strip().upper()
    if any(text.startswith(prefix) for prefix in BLOCKED_E2E_PREFIXES):
        raise UkfpsError('invalid_e2e', 'E2E ID belongs to another clearing system.')
    if not re.fullmatch(r'(FPS|CHAPS|BACS)\d{8}\d{6}', text):
        raise UkfpsError('invalid_e2e', 'E2E ID must be FPS/CHAPS/BACS + date + seq.')
    return text


def compose_fps18(
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
    source_sort: str = DEFAULT_SOURCE_SORT,
    currency: str = 'GBP',
) -> str:
    clearing = CLEARING_BY_RAIL[normalize_rail(rail)]
    fields = [
        FPS18_VERSION,
        clearing,
        normalize_currency(currency, rail=rail),
        str(amount),
        bank_code or '000000',
        account_last4 or '0000',
        e2e_id,
        purpose,
        value_date,
        proxy_type or 'none',
        proxy_mask or '****',
        'Y' if ofac_hit else 'N',
        str(int(sequence)),
        source_sort,
    ]
    if len(fields) != FPS18_FIELDS:
        raise UkfpsError('invalid_fps18', 'FPS18 record is the wrong width.')
    line = '|'.join(fields)
    if '<' in line or '>' in line:
        raise UkfpsError('invalid_fps18', 'FPS18 rejects XML markup.')
    return line


def parse_fps18(payload: Any) -> Dict[str, str]:
    text = str(payload or '').strip()
    upper = text.upper()
    if not text:
        raise UkfpsError('invalid_fps18', 'FPS18 payload is empty.')
    if '<?XML' in upper or '<!DOCTYPE' in upper or '<FPS' in upper or '<FAST' in upper:
        raise UkfpsError('invalid_fps18', 'FPS18 rejects XML / DOCTYPE payloads.')
    parts = text.split('|')
    if len(parts) != FPS18_FIELDS:
        raise UkfpsError('invalid_fps18', 'FPS18 must be a 14-field pipe record.')
    if parts[0] != FPS18_VERSION:
        raise UkfpsError('invalid_fps18', 'FPS18 version must be 1.')
    clearing = parts[1].upper()
    if clearing in REJECTED_CLEARING:
        raise UkfpsError('invalid_clearing', 'Clearing %s is not FPS/CHAPS/BACS.' % clearing)
    if clearing not in {'FPS', 'CHAPS', 'BACS'}:
        raise UkfpsError('invalid_clearing', 'Unknown FPS18 clearing.')
    currency = parts[2].upper()
    if currency != 'GBP':
        raise UkfpsError('invalid_currency', 'FPS18 is GBP only.')
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
        'source_sort': parts[13],
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
class CopResolution:
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


def resolve_ukfps(
    proxy_type: Any,
    proxy: Any,
    presented_name: Any = '',
    *,
    directory: Optional[Dict[str, str]] = None,
) -> CopResolution:
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
    return CopResolution(
        outcome=outcome,
        registered_name=registered,
        presented_name=presented,
        proxy_type=kind,
        proxy_mask=mask_proxy(kind, value),
    )


def convert_instructed_to_usd(amount: Decimal, rate: Decimal) -> Decimal:
    if rate <= 0:
        raise UkfpsError('invalid_fx', 'FX rate must be positive.')
    return (amount * rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def _observed_weekend(day: date) -> date:
    if day.weekday() == 5:
        return day + timedelta(days=2)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    cursor = date(year, month, 1)
    while cursor.weekday() != weekday:
        cursor += timedelta(days=1)
    return cursor + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year, 12, 31)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    while cursor.weekday() != weekday:
        cursor -= timedelta(days=1)
    return cursor


def uk_holidays(year: int) -> set:
    """Fixed and weekday-rule UK bank holidays. Easter dates are extra_holidays."""
    return {
        _observed_weekend(date(year, 1, 1)),
        _nth_weekday(year, 5, 0, 1),
        _last_weekday(year, 5, 0),
        _last_weekday(year, 8, 0),
        _observed_weekend(date(year, 12, 25)),
        _observed_weekend(date(year, 12, 26)),
    }


class UkClock:
    """UK FPS 24/7 vs CHAPS/Bacs banking-day clock. Injectable offset and holidays."""

    def __init__(
        self,
        *,
        chaps_open_hour: int = 6,
        chaps_open_minute: int = 0,
        chaps_cutoff_hour: int = 18,
        chaps_cutoff_minute: int = 0,
        tz_offset_minutes: int = GMT_OFFSET_MINUTES,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.chaps_open_hour = chaps_open_hour
        self.chaps_open_minute = chaps_open_minute
        self.chaps_cutoff_hour = chaps_cutoff_hour
        self.chaps_cutoff_minute = chaps_cutoff_minute
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
        return uk_holidays(year) | {day for day in self.extra_holidays if day.year == year}

    def is_business_day(self, day: date) -> bool:
        if day.weekday() >= 5:
            return False
        return day not in self.holidays(day.year)

    def next_business_day(self, day: date) -> date:
        cursor = day
        while not self.is_business_day(cursor):
            cursor += timedelta(days=1)
        return cursor

    def is_after_chaps_cutoff(self, ts: float) -> bool:
        local = self.local(ts)
        cutoff = local.replace(
            hour=self.chaps_cutoff_hour, minute=self.chaps_cutoff_minute, second=0, microsecond=0,
        )
        open_at = local.replace(
            hour=self.chaps_open_hour, minute=self.chaps_open_minute, second=0, microsecond=0,
        )
        return local < open_at or local >= cutoff

    def is_sterling_night(self, ts: float) -> bool:
        local = self.local(ts)
        return local.hour >= 18 or local.hour < 6 or not self.is_business_day(local.date())

    def value_date(self, ts: float, *, rail: str = 'fps') -> date:
        local = self.local(ts)
        kind = normalize_rail(rail)
        day = local.date()
        if kind == 'fps':
            return day
        if kind == 'bacs':
            start = day + timedelta(days=1)
            return self.next_business_day(start)
        if not self.is_business_day(day) or self.is_after_chaps_cutoff(ts):
            return self.next_business_day(day + timedelta(days=1))
        return day

    def cycle_date(self, ts: float, *, rail: str = 'fps') -> str:
        return self.value_date(ts, rail=rail).strftime('%Y%m%d')

    def snapshot(self, ts: float, *, rail: str = 'fps') -> Dict[str, Any]:
        local = self.local(ts)
        value = self.value_date(ts, rail=rail)
        kind = normalize_rail(rail)
        after = False
        if kind in {'chaps', 'bacs'}:
            after = self.is_after_chaps_cutoff(ts) or not self.is_business_day(local.date())
        return {
            'rail': kind,
            'timezone': 'GMT',
            'local': local.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'cutoff': '%02d:%02d' % (self.chaps_cutoff_hour, self.chaps_cutoff_minute),
            'after_cutoff': after,
            'always_on': kind == 'fps',
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
class UkfpsPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    allow_credit: bool = False
    require_cop_match: bool = True
    max_beneficiaries: int = 12
    max_payments: int = 120
    min_amount: Decimal = Decimal('0.01')
    fps_cap: Decimal = Decimal('1000000.00')
    bacs_cap: Decimal = Decimal('20000000.00')
    chaps_cap: Decimal = Decimal('50000000.00')
    outbound_fee: Decimal = Decimal('0.00')
    chaps_fee: Decimal = Decimal('25.00')
    bacs_fee: Decimal = Decimal('0.50')
    dual_control_threshold: Decimal = Decimal('10000.00')
    gbpusd: Decimal = DEFAULT_GBPUSD
    chaps_open_hour: int = 6
    chaps_open_minute: int = 0
    chaps_cutoff_hour: int = 18
    chaps_cutoff_minute: int = 0
    tz_offset_minutes: int = GMT_OFFSET_MINUTES
    source_sort: str = DEFAULT_SOURCE_SORT
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'UkfpsPolicy':
        extra = _env_list('UKFPS_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('UKFPS_ENABLED', True),
            customer_manage=_env_bool('UKFPS_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('UKFPS_CUSTOMER_SEND', True),
            allow_credit=_env_bool('UKFPS_ALLOW_CREDIT', False),
            require_cop_match=_env_bool('UKFPS_REQUIRE_COP', True),
            max_beneficiaries=max(1, _env_int('UKFPS_MAX_BENEFICIARIES', 12)),
            max_payments=max(1, _env_int('UKFPS_MAX_PAYMENTS', 120)),
            min_amount=_env_money('UKFPS_MIN_AMOUNT', '0.01'),
            fps_cap=_env_money('UKFPS_CAP', '1000000.00'),
            bacs_cap=_env_money('UKFPS_BACS_CAP', '20000000.00'),
            chaps_cap=_env_money('UKFPS_CHAPS_CAP', '50000000.00'),
            outbound_fee=_env_money('UKFPS_FEE', '0.00'),
            chaps_fee=_env_money('UKFPS_CHAPS_FEE', '25.00'),
            bacs_fee=_env_money('UKFPS_BACS_FEE', '0.50'),
            dual_control_threshold=_env_money('UKFPS_DUAL_CONTROL', '10000.00'),
            gbpusd=_env_money('UKFPS_FX_GBPUSD', '1.27'),
            chaps_open_hour=max(0, min(23, _env_int('UKFPS_CHAPS_OPEN_HOUR', 6))),
            chaps_open_minute=max(0, min(59, _env_int('UKFPS_CHAPS_OPEN_MINUTE', 0))),
            chaps_cutoff_hour=max(0, min(23, _env_int('UKFPS_CUTOFF_HOUR', 18))),
            chaps_cutoff_minute=max(0, min(59, _env_int('UKFPS_CUTOFF_MINUTE', 0))),
            tz_offset_minutes=_env_int('UKFPS_TZ_OFFSET', GMT_OFFSET_MINUTES),
            source_sort=normalize_sort_code(os.environ.get('UKFPS_SOURCE_SORT', DEFAULT_SOURCE_SORT)),
            watchlist=watch,
            extra_holidays=_env_list('UKFPS_HOLIDAYS'),
        )

    def cap_for(self, rail: str) -> Decimal:
        if rail == 'chaps':
            return self.chaps_cap
        if rail == 'bacs':
            return self.bacs_cap
        return self.fps_cap

    def fee_for(self, rail: str) -> Decimal:
        if rail == 'chaps':
            return self.chaps_fee
        if rail == 'bacs':
            return self.bacs_fee
        return self.outbound_fee

    def rate_for(self, currency: str) -> Decimal:
        _ = currency
        return self.gbpusd


@dataclass
class UkfpsBeneficiary:
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
            'sort_code': format_sort_code(self.bank_code) if self.bank_code else '',
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
class UkfpsPayment:
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
            'sort_code': format_sort_code(self.bank_code) if self.bank_code else '',
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


def _clone_bene(row: UkfpsBeneficiary) -> UkfpsBeneficiary:
    return UkfpsBeneficiary(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_pay(row: UkfpsPayment) -> UkfpsPayment:
    return UkfpsPayment(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _bene_from_row(row: Any) -> UkfpsBeneficiary:
    return UkfpsBeneficiary(
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


def _pay_from_row(row: Any) -> UkfpsPayment:
    return UkfpsPayment(
        payment_id=row['payment_id'],
        trace_id=row['trace_id'],
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        rail=row['rail'],
        currency=row['currency'] if 'currency' in row.keys() else 'GBP',
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


class MemoryUkfpsStore:
    def __init__(self) -> None:
        self._benes: Dict[str, UkfpsBeneficiary] = {}
        self._pays: Dict[str, UkfpsPayment] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_beneficiary(self, row: UkfpsBeneficiary) -> None:
        with self._lock:
            self._benes[row.beneficiary_id] = _clone_bene(row)

    def get_beneficiary(self, beneficiary_id: str) -> Optional[UkfpsBeneficiary]:
        with self._lock:
            row = self._benes.get(beneficiary_id)
            return _clone_bene(row) if row else None

    def update_beneficiary(self, row: UkfpsBeneficiary) -> None:
        self.put_beneficiary(row)

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[UkfpsBeneficiary]:
        with self._lock:
            rows = list(self._benes.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != BENE_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return [_clone_bene(row) for row in rows]

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[UkfpsBeneficiary]:
        wanted = nickname.strip().lower()
        for row in self.list_beneficiaries(userid, include_archived=False):
            if row.status in OPEN_BENE and row.nickname.lower() == wanted:
                return row
        return None

    def find_beneficiary_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[UkfpsBeneficiary]:
        for row in self.list_beneficiaries(userid, include_archived=False):
            if row.status in OPEN_BENE and row.fingerprint == fingerprint:
                return row
        return None

    def put_payment(self, row: UkfpsPayment) -> UkfpsPayment:
        with self._lock:
            existing_id = self._by_trace.get(row.trace_id)
            if existing_id is not None:
                return _clone_pay(self._pays[existing_id])
            self._pays[row.payment_id] = _clone_pay(row)
            self._by_trace[row.trace_id] = row.payment_id
            return _clone_pay(row)

    def update_payment(self, row: UkfpsPayment) -> None:
        with self._lock:
            self._pays[row.payment_id] = _clone_pay(row)
            self._by_trace[row.trace_id] = row.payment_id

    def get_payment(self, payment_id: str) -> Optional[UkfpsPayment]:
        with self._lock:
            row = self._pays.get(payment_id)
            return _clone_pay(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[UkfpsPayment]:
        with self._lock:
            payment_id = self._by_trace.get(trace_id)
            row = self._pays.get(payment_id) if payment_id else None
            return _clone_pay(row) if row else None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[UkfpsPayment]:
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


class SqliteUkfpsStore:
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
                    currency TEXT NOT NULL DEFAULT 'GBP',
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

    def put_beneficiary(self, row: UkfpsBeneficiary) -> None:
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

    def get_beneficiary(self, beneficiary_id: str) -> Optional[UkfpsBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM beneficiaries WHERE beneficiary_id = ?', (beneficiary_id,),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def update_beneficiary(self, row: UkfpsBeneficiary) -> None:
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

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[UkfpsBeneficiary]:
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

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[UkfpsBeneficiary]:
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

    def find_beneficiary_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[UkfpsBeneficiary]:
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

    def put_payment(self, row: UkfpsPayment) -> UkfpsPayment:
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

    def update_payment(self, row: UkfpsPayment) -> None:
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

    def get_payment(self, payment_id: str) -> Optional[UkfpsPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE payment_id = ?', (payment_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[UkfpsPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE trace_id = ?', (trace_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[UkfpsPayment]:
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


class UkfpsService:
    def __init__(
        self,
        policy: UkfpsPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[UkClock] = None,
        directory: Optional[Dict[str, str]] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or UkClock(
            chaps_open_hour=policy.chaps_open_hour,
            chaps_open_minute=getattr(policy, 'chaps_open_minute', 0),
            chaps_cutoff_hour=policy.chaps_cutoff_hour,
            chaps_cutoff_minute=policy.chaps_cutoff_minute,
            tz_offset_minutes=policy.tz_offset_minutes,
            extra_holidays=policy.extra_holidays,
        )
        self.directory = dict(directory or DEFAULT_FPS_DIRECTORY)

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise UkfpsError('ukfps_disabled', 'UK FPS / CHAPS is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise UkfpsError('ukfps_forbidden', 'Customers cannot manage UK FPS beneficiaries.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise UkfpsError('ukfps_forbidden', 'Customers cannot originate UK FPS payments.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise UkfpsError('ukfps_forbidden', 'Staff only.')

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
            raise UkfpsError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise UkfpsError('credit_not_allowed', 'Credit accounts cannot originate UK FPS.')

    def _assert_amount(self, dollars: Decimal, rail: str, *, currency: str = 'GBP') -> None:
        _ = currency
        cap = self.policy.cap_for(rail)
        if dollars < self.policy.min_amount or dollars > cap:
            raise UkfpsError('amount_out_of_range', 'Amount is outside the %s cap.' % rail)

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
    ) -> CopResolution:
        self._require_send(actor_type)
        _ = actor
        return resolve_ukfps(proxy_type, proxy, presented_name, directory=self.directory)

    def quote_fx(self, amount: Any, *, rail: Any = 'fps', currency: Any = 'GBP') -> Dict[str, Any]:
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
    ) -> UkfpsBeneficiary:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise UkfpsError('ukfps_forbidden', 'Not allowed to add beneficiaries for this customer.')
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        kind, value = normalize_proxy(proxy_type, proxy, required=False)
        bank = normalize_sort_code(bank_code, required=False)
        external = normalize_external_account(account_number, required=False)
        if kind == 'sort' and len(value) == 14:
            bank, external = value[:6], value[6:]
        elif kind == 'iban' and value:
            bank, external = sort_account_from_iban(value)
        if not kind and not (bank and external):
            raise UkfpsError('invalid_proxy', 'Pay.UK proxy or sort code + account is required.')
        if kind != 'iban' and bank and external and not vocalink_ok(bank, external):
            raise UkfpsError('invalid_modulus', 'Sort code and account failed Vocalink modulus check.')
        if self.store.find_beneficiary_by_nickname(owner_userid, name) is not None:
            raise UkfpsError('beneficiary_duplicate', 'A beneficiary with that nickname already exists.')
        fingerprint = fingerprint_for(owner_userid, kind, value, bank, external)
        if self.store.find_beneficiary_by_fingerprint(owner_userid, fingerprint) is not None:
            raise UkfpsError('beneficiary_duplicate', 'That UK FPS destination is already on file.')
        open_rows = [row for row in self.store.list_beneficiaries(owner_userid) if row.status in OPEN_BENE]
        if len(open_rows) >= self.policy.max_beneficiaries:
            raise UkfpsError('beneficiary_limit', 'UK FPS beneficiary limit reached.')
        now = float(self.clock())
        row = UkfpsBeneficiary(
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

    def get_beneficiary(self, *, beneficiary_id: str, actor: str, actor_type: str) -> UkfpsBeneficiary:
        self._require_enabled()
        row = self.store.get_beneficiary(beneficiary_id)
        if row is None:
            raise UkfpsError('beneficiary_not_found', 'UK FPS beneficiary not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise UkfpsError('ukfps_forbidden', 'Not allowed to view this beneficiary.')
        return row

    def enforce_beneficiary(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> UkfpsBeneficiary:
        row = self.get_beneficiary(beneficiary_id=beneficiary_id, actor=actor, actor_type=actor_type)
        if row.status == BENE_ARCHIVED:
            raise UkfpsError('already_archived', 'Beneficiary is archived.')
        if row.status == BENE_PAUSED:
            raise UkfpsError('beneficiary_paused', 'Beneficiary is paused.')
        if require_active and row.status != BENE_ACTIVE:
            raise UkfpsError('invalid_status', 'Beneficiary is not active.')
        return row

    def set_beneficiary_status(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> UkfpsBeneficiary:
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
            raise UkfpsError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == BENE_ARCHIVED:
            raise UkfpsError('already_archived', 'Beneficiary is already archived.')
        if wanted == BENE_PAUSED:
            if row.status == BENE_PAUSED:
                raise UkfpsError('already_paused', 'Beneficiary is already paused.')
            if row.status != BENE_ACTIVE:
                raise UkfpsError('invalid_status', 'Only an active beneficiary can be paused.')
        elif wanted == BENE_ACTIVE:
            if row.status == BENE_ACTIVE:
                raise UkfpsError('already_active', 'Beneficiary is already active.')
            if row.status != BENE_PAUSED:
                raise UkfpsError('invalid_status', 'Only a paused beneficiary can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_beneficiary(row)
        return row

    def _cop_for(self, bene: UkfpsBeneficiary, *, presented_name: Any = '', override: bool = False) -> CopResolution:
        if not bene.proxy_type:
            return CopResolution(
                outcome=COP_UNAVAILABLE, registered_name='', presented_name=str(presented_name or ''),
                proxy_type='', proxy_mask='',
            )
        resolution = resolve_ukfps(
            bene.proxy_type, bene.proxy, presented_name or bene.legal_name, directory=self.directory,
        )
        if override:
            return resolution
        if self.policy.require_cop_match and resolution.outcome in {COP_NONE, COP_UNAVAILABLE} and bene.proxy_type:
            raise UkfpsError(
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
        currency: Any = 'GBP',
    ) -> Dict[str, Any]:
        self._require_send(actor_type)
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise UkfpsError('ukfps_forbidden', 'Beneficiary does not belong to this customer.')
        kind = normalize_rail(rail)
        if kind == 'fps' and not bene.proxy_type and not (bene.bank_code and bene.account_number):
            raise UkfpsError('invalid_proxy', 'UK FPS requires a Pay.UK proxy or sort code + account.')
        if kind in {'chaps', 'bacs'} and not (bene.bank_code and bene.account_number) and not bene.proxy_type:
            raise UkfpsError('invalid_sort', 'CHAPS/Bacs needs a sort+account or Pay.UK proxy.')
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
        bene: UkfpsBeneficiary,
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
        cop: CopResolution,
        waive_fee: bool,
    ) -> UkfpsPayment:
        now = float(self.clock())
        value = self.calendar.cycle_date(now, rail=rail)
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        clock = self.calendar.snapshot(now, rail=rail)
        if ofac.hit:
            status = PN_HELD
        elif self._needs_dual_control(instructed, rail):
            status = PN_PENDING
        elif rail == 'bacs' or clock['after_cutoff']:
            status = PN_QUEUED
        elif rail == 'fps':
            status = PN_COMPLETED
        else:
            status = PN_SENT
        payment = UkfpsPayment(
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

    def _transmit(self, payment: UkfpsPayment, *, actor: str, complete: bool = False) -> UkfpsPayment:
        now = float(self.clock())
        cycle = payment.value_date or self.calendar.cycle_date(now, rail=payment.rail)
        seq = self.store.next_e2e_sequence(cycle, payment.rail)
        payment.e2e_id = compose_e2e(payment.rail, cycle, seq)
        usd = parse_money(payment.usd_amount)
        fee_instructed = parse_money(payment.fee, allow_zero=True)
        fee_usd = convert_instructed_to_usd(fee_instructed, parse_money(payment.fx_rate, allow_zero=False)) if fee_instructed else Decimal('0.00')
        remark = 'ukfps to %s' % payment.nickname
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
                fee_result = self.debit_fn(payment.internal_account, money_str(fee_usd), 'ukfps fee %s' % mask_e2e(payment.e2e_id))
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
        currency: Any = 'GBP',
    ) -> Tuple[UkfpsPayment, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise UkfpsError('ukfps_forbidden', 'Not allowed to originate UK FPS for this customer.')
        staff_override = bool(cop_override) and actor_type in EMPLOYEE_ROLES
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise UkfpsError('ukfps_forbidden', 'Beneficiary does not belong to this customer.')
        kind = normalize_rail(rail)
        if kind == 'fps' and not bene.proxy_type and not (bene.bank_code and bene.account_number):
            raise UkfpsError('invalid_proxy', 'UK FPS requires a Pay.UK proxy or sort code + account.')
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
            raise UkfpsError('ukfps_limit', 'UK FPS history limit reached.')
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        cop = self._cop_for(bene, presented_name=presented_name, override=staff_override)
        if staff_override and cop.outcome in {COP_NONE, COP_UNAVAILABLE}:
            cop = CopResolution(
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
            raise UkfpsError('nsf', 'Insufficient funds for UK FPS.', payment=stored)
        if stored.status == PN_FAILED:
            raise UkfpsError('failed', 'UK FPS debit did not complete.', payment=stored)
        return stored, True

    def get_payment(self, *, payment_id: str, actor: str, actor_type: str) -> UkfpsPayment:
        self._require_enabled()
        row = self.store.get_payment(payment_id)
        if row is None:
            raise UkfpsError('payment_not_found', 'UK FPS payment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise UkfpsError('ukfps_forbidden', 'Not allowed to view this payment.')
        return row

    def cancel_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> UkfpsPayment:
        self._require_send(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise UkfpsError('ukfps_forbidden', 'Not allowed to cancel this payment.')
        if row.status not in CANCELABLE:
            raise UkfpsError('not_cancelable', 'Payment can no longer be cancelled.')
        row.status = PN_CANCELLED
        row.actor = str(actor)
        row.note = normalize_note(note)
        row.updated_at = float(self.clock())
        self.store.update_payment(row)
        return row

    def release_payment(self, *, payment_id: str, actor: str, actor_type: str) -> UkfpsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.ofac_hit:
            raise UkfpsError('ofac_hold', 'OFAC hold must be overridden first.')
        if row.status not in {PN_PENDING, PN_QUEUED}:
            raise UkfpsError('not_releasable', 'Payment is not waiting for release.')
        if row.status == PN_PENDING and row.actor == str(actor):
            raise UkfpsError('same_approver', 'Maker cannot release their own payment.')
        complete = row.rail == 'fps'
        self._transmit(row, actor=actor, complete=complete)
        if row.status == PN_NSF:
            self.store.update_payment(row)
            raise UkfpsError('nsf', 'Insufficient funds for UK FPS.', payment=row)
        if row.status == PN_FAILED:
            self.store.update_payment(row)
            raise UkfpsError('failed', 'UK FPS debit did not complete.', payment=row)
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
    ) -> UkfpsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status not in {PN_HELD, PN_QUEUED, PN_PENDING}:
            raise UkfpsError('not_rejectable', 'Payment cannot be rejected.')
        row.status = PN_REJECTED
        row.reason = normalize_note(reason, limit=40) or 'other'
        row.note = normalize_note(note)
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_payment(row)
        return row

    def complete_payment(self, *, payment_id: str, actor: str, actor_type: str) -> UkfpsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status == PN_COMPLETED:
            raise UkfpsError('already_completed', 'Payment is already completed.')
        if row.status != PN_SENT:
            raise UkfpsError('not_completable', 'Only a sent payment can be completed.')
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
    ) -> UkfpsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status == PN_RECALLED:
            raise UkfpsError('already_recalled', 'Payment is already recalled.')
        if row.status not in {PN_SENT, PN_COMPLETED}:
            raise UkfpsError('not_recallable', 'Payment cannot be recalled.')
        now = float(self.clock())
        window = STAFF_COMPLAINT_DAYS * 24 * 60 * 60
        sent_at = row.sent_at or row.created_at
        if row.rail in {'fps'} and (now - sent_at) > window:
            raise UkfpsError('not_recallable', 'UK FPS recall window has closed.')
        usd = parse_money(row.usd_amount)
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(row.internal_account, money_str(usd), 'ukfps recall %s' % mask_e2e(row.e2e_id))
            except Exception as exc:
                raise UkfpsError('recall_failed', 'Recall credit did not complete.', payment=row) from exc
            if _classify_money_result(result) != 'ok':
                raise UkfpsError('recall_failed', 'Recall credit did not complete.', payment=row)
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
    ) -> UkfpsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status != PN_HELD:
            raise UkfpsError('not_releasable', 'No OFAC hold to override.')
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
    ) -> UkfpsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        row.cop_outcome = COP_MATCH
        row.note = normalize_note(note)
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_payment(row)
        return row

    def waive_fee(self, *, payment_id: str, actor: str, actor_type: str) -> UkfpsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status not in CANCELABLE | {PN_SENT}:
            raise UkfpsError('not_releasable', 'Fee can no longer be waived.')
        row.fee_status = FEE_WAIVED
        row.fee = money_str(Decimal('0.00'))
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_payment(row)
        return row

    def run_due(self, userid: str) -> List[UkfpsPayment]:
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

    def export_fps18(self, userid: str, *, actor: str, actor_type: str) -> str:
        self._require_staff(actor_type)
        _ = actor
        lines = []
        for index, row in enumerate(self.store.list_payments(userid), start=1):
            if row.status not in {PN_SENT, PN_COMPLETED}:
                continue
            lines.append(compose_fps18(
                rail=row.rail,
                amount=row.instructed_amount,
                bank_code=row.bank_code or self.policy.source_sort,
                account_last4=row.account_last4,
                e2e_id=row.e2e_id,
                purpose=row.purpose,
                value_date=row.value_date,
                proxy_type=row.proxy_type,
                proxy_mask=row.proxy_mask,
                ofac_hit=bool(row.ofac_hit),
                sequence=index,
                source_sort=self.policy.source_sort,
                currency=row.currency,
            ))
        return '\n'.join(lines)

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self._require_enabled()
        _ = actor
        if actor_type not in EMPLOYEE_ROLES and actor and actor != userid:
            raise UkfpsError('ukfps_forbidden', 'Not allowed to view this UK FPS book.')
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
            'fx_rate': money_str(self.policy.gbpusd),
            'gbp_fx_rate': money_str(self.policy.gbpusd),
            'clock': self.calendar.snapshot(now, rail='fps'),
            'chaps_clock': self.calendar.snapshot(now, rail='chaps'),
        }


_SERVICE: Optional[UkfpsService] = None


def set_service(service: Optional[UkfpsService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[UkfpsService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('UKFPS_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryUkfpsStore()
    path = os.environ.get('UKFPS_DB', DEFAULT_STORE_PATH)
    return SqliteUkfpsStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[UkClock] = None,
    directory: Optional[Dict[str, str]] = None,
) -> UkfpsService:
    if store is None:
        store = default_store()
    return UkfpsService(
        UkfpsPolicy.from_env(),
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
        'ukfps_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_completed': 409,
        'already_recalled': 409,
        'nsf': 409,
        'failed': 409,
        'recall_failed': 409,
        'ukfps_forbidden': 403,
        'ukfps_disabled': 403,
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
        'invalid_sort': 400,
        'invalid_iban': 400,
        'invalid_modulus': 400,
        'invalid_phone': 400,
        'invalid_email': 400,
        'invalid_proxy': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_e2e': 400,
        'invalid_fps18': 400,
        'invalid_clearing': 400,
        'invalid_currency': 400,
        'invalid_rail': 400,
        'invalid_fx': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_beneficiary': 400,
        'missing_payment': 400,
    }.get(code, 400)


def _error_body(exc: UkfpsError) -> Dict[str, Any]:
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
    except UkfpsError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: UkfpsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'UkFps': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_lookup(service: UkfpsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        resolution = service.lookup(
            actor=userid,
            actor_type=actor_type,
            proxy_type=values.get('proxy_type') or values.get('ukfps_type'),
            proxy=values.get('proxy') or values.get('fps') or values.get('iban') or values.get('sort') or values.get('phone'),
            presented_name=values.get('legal_name') or values.get('name') or '',
        )
        return jsonify({'cop': resolution.to_dict()}), 200

    return _handle_errors(_run)


def handle_quote(service: UkfpsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}

    def _run():
        quote = service.quote_fx(
            values.get('amount'),
            rail=values.get('rail') or 'fps',
            currency=values.get('currency') or 'GBP',
        )
        return jsonify({'quote': quote}), 200

    return _handle_errors(_run)


def handle_add_beneficiary(service: UkfpsService):
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
            proxy_type=values.get('proxy_type') or values.get('ukfps_type'),
            proxy=values.get('proxy') or values.get('fps') or values.get('iban') or values.get('sort') or values.get('phone'),
            bank_code=values.get('bank_code') or values.get('bank'),
            account_number=values.get('account_number') or values.get('external_account'),
        )
        return jsonify({
            'message': 'UK FPS beneficiary added',
            'beneficiary': row.to_dict(),
            'UkFps': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _bene_status_route(service: UkfpsService, status: str, ok_message: str):
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
            'UkFps': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: UkfpsService):
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
            currency=values.get('currency') or 'GBP',
        )
        return jsonify({'preview': preview, 'UkFps': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: UkfpsService):
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
            currency=values.get('currency') or 'GBP',
        )
        return jsonify({
            'message': 'UK FPS originated' if created else 'UK FPS already posted',
            'payment': payment.to_dict(),
            'UkFps': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: UkfpsService):
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
            'message': 'UK FPS cancelled',
            'payment': payment.to_dict(),
            'UkFps': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_route(service: UkfpsService, action: str):
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
            message = 'UK FPS released'
        elif action == 'reject':
            payment = service.reject_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'UK FPS rejected'
        elif action == 'complete':
            payment = service.complete_payment(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'UK FPS completed'
        elif action == 'recall':
            payment = service.recall_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'UK FPS recalled'
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
            message = 'UK FPS fee waived'
        else:
            raise UkfpsError('invalid_status', 'Unknown UK FPS action.')
        return jsonify({
            'message': message,
            'payment': payment.to_dict(),
            'UkFps': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: UkfpsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'UkFps': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_export(service: UkfpsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid

    def _run():
        payload = service.export_fps18(owner, actor=userid, actor_type=actor_type)
        return jsonify({'fps18': payload, 'UkFps': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def attach_ukfps_routes(app, service: UkfpsService) -> None:
    @app.route('/listUkFps', methods=['POST', 'GET'])
    def list_fps_route():
        return handle_list(service)

    @app.route('/lookupUkFps', methods=['POST', 'GET'])
    def lookup_fps_route():
        return handle_lookup(service)

    @app.route('/quoteUkFpsFx', methods=['POST', 'GET'])
    def quote_fps_route():
        return handle_quote(service)

    @app.route('/addUkFpsBeneficiary', methods=['POST', 'GET'])
    def add_fps_beneficiary_route():
        return handle_add_beneficiary(service)

    @app.route('/pauseUkFpsBeneficiary', methods=['POST', 'GET'])
    def pause_fps_beneficiary_route():
        return _bene_status_route(service, BENE_PAUSED, 'UK FPS beneficiary paused')

    @app.route('/resumeUkFpsBeneficiary', methods=['POST', 'GET'])
    def resume_fps_beneficiary_route():
        return _bene_status_route(service, BENE_ACTIVE, 'UK FPS beneficiary resumed')

    @app.route('/archiveUkFpsBeneficiary', methods=['POST', 'GET'])
    def archive_fps_beneficiary_route():
        return _bene_status_route(service, BENE_ARCHIVED, 'UK FPS beneficiary archived')

    @app.route('/previewUkFps', methods=['POST', 'GET'])
    def preview_fps_route():
        return handle_preview(service)

    @app.route('/sendUkFps', methods=['POST', 'GET'])
    def send_fps_route():
        return handle_send(service)

    @app.route('/cancelUkFps', methods=['POST', 'GET'])
    def cancel_fps_route():
        return handle_cancel(service)

    @app.route('/releaseUkFps', methods=['POST', 'GET'])
    def release_fps_route():
        return _staff_route(service, 'release')

    @app.route('/rejectUkFps', methods=['POST', 'GET'])
    def reject_fps_route():
        return _staff_route(service, 'reject')

    @app.route('/completeUkFps', methods=['POST', 'GET'])
    def complete_fps_route():
        return _staff_route(service, 'complete')

    @app.route('/recallUkFps', methods=['POST', 'GET'])
    def recall_fps_route():
        return _staff_route(service, 'recall')

    @app.route('/overrideUkFpsOfac', methods=['POST', 'GET'])
    def override_fps_ofac_route():
        return _staff_route(service, 'override')

    @app.route('/overrideUkFpsCop', methods=['POST', 'GET'])
    def override_fps_cop_route():
        return _staff_route(service, 'cop')

    @app.route('/waiveUkFpsFee', methods=['POST', 'GET'])
    def waive_fps_fee_route():
        return _staff_route(service, 'waive')

    @app.route('/runDueUkFps', methods=['POST', 'GET'])
    def run_due_fps_route():
        return handle_run_due(service)

    @app.route('/exportUkFps', methods=['POST', 'GET'])
    def export_fps_route():
        return handle_export(service)
