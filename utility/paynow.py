"""PayNow / FAST / MEPS+ / GIRO origination with Confirmation of Payee.

Customers send SGD via PayNow (NRIC/FIN, +65 mobile, UEN) over FAST,
account-to-account FAST, high-value MEPS+ RTGS, or next-day GIRO.
Independent of domestic Fedwire (PR #73), ACH linking (PR #68), and the
unmerged NPP / UPI / PIX / SPEI rails. Existing `/fundTransfer`,
`/withdrawAmount`, and `/sendWire` stay unchanged.
`Customers.debit_request` / `credit_request` still write `debited` /
`direct deposited` unless a remark is supplied here.

Foundations (reusable beyond this screen):
- Singapore NRIC/FIN checksum (S/T/F/G/M prefix + weighted remainder)
- UEN n8X check letter (ACRA-style weighted modulus)
- +65 PayNow mobile (8-digit 8/9 national)
- MAS 4-digit participating bank codes
- Confirmation of Payee (`match` / `close_match` / `no_match` /
  `unavailable`) — same outcome set as UK CoP / PayID
- FAST End-to-End ID (`FAST`/`PNW`/`MEPS`/`GIRO` + YYYYMMDD + seq)
- FAST1 pipe clearing (XML/DOCTYPE and non-FAST clearing rejected)
- SgClock in SGT (UTC+8): PayNow/FAST 24/7; MEPS+ 09:00-17:30 SGT
  banking days; GIRO queues to the next MAS day
- SGDUSD quote book converting instructed SGD to USD ledger debit
- Dual-control at SGD 10,000; FAST/PayNow cap 200k / MEPS 10m

Reuses Fedwire OFAC screening, money parsing, fee helper, and account
directory from `utility.wire`. Stores are pluggable (memory for tests,
sqlite WAL for restart-safe default). Full NRIC, UEN, mobile, account,
fingerprint, and E2E ID never appear unmasked in to_dict / snapshots.
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
    'iras': 'tax', 'gst': 'tax', 'taxes': 'tax',
    'mortgage': 'loan', 'housing': 'rent', 'hdb': 'rent',
}
RAILS = frozenset({'paynow', 'fast', 'meps', 'giro'})
RAIL_ALIASES = {
    'pay_now': 'paynow', 'overlay': 'paynow', 'pnw': 'paynow',
    'fast_and_secure': 'fast', 'instant': 'fast', 'a2a': 'fast',
    'mepsplus': 'meps', 'meps_plus': 'meps', 'rtgs': 'meps',
    'ibg': 'giro', 'interbank_giro': 'giro', 'bulk': 'giro',
}
PROXY_TYPES = frozenset({'nric', 'phone', 'uen'})
PROXY_ALIASES = {
    'fin': 'nric', 'nricfin': 'nric', 'nric_fin': 'nric', 'id': 'nric',
    'mobile': 'phone', 'msisdn': 'phone', 'tel': 'phone', 'sms': 'phone',
    'entity': 'uen', 'company': 'uen', 'business': 'uen',
}
COP_MATCH = 'match'
COP_CLOSE = 'close_match'
COP_NONE = 'no_match'
COP_UNAVAILABLE = 'unavailable'
COP_OUTCOMES = frozenset({COP_MATCH, COP_CLOSE, COP_NONE, COP_UNAVAILABLE})
CLEARING_FAST = 'FAST'
CLEARING_BY_RAIL = {'paynow': 'PAYNOW', 'fast': 'FAST', 'meps': 'MEPS', 'giro': 'GIRO'}
REJECTED_CLEARING = frozenset({
    'AUNPP', 'USRTPP', 'FEDNOW', 'FEDWIRE', 'ACH', 'UPI', 'CHIPS', 'SWIFT',
    'PIX', 'SPEI', 'NPP', 'OSKO',
})
DEFAULT_BANKS = {
    '7171': 'DBS',
    '7375': 'OCBC',
    '7232': 'UOB',
    '7302': 'STANDARD CHARTERED',
    '7339': 'CITIBANK',
    '7047': 'MAYBANK',
    '7083': 'HSBC',
    '7214': 'BANK OF CHINA',
    '9092': 'KONOHA BANK',
}
DEFAULT_PAYNOW_DIRECTORY = {
    'phone:+6591234567': 'ADA LOVELACE',
    'nric:S0000001I': 'ADA LOVELACE',
    'uen:12345678E': 'ADA LOVELACE PTE LTD',
}
NRIC_WEIGHTS = (2, 7, 6, 5, 4, 3, 2)
NRIC_ST = 'JZIHGFEDCBA'
NRIC_FG = 'XWUTRQPNMLK'
NRIC_M = 'KCLGJTUSQPB'
UEN_WEIGHTS = (10, 8, 6, 4, 2, 9, 7, 5)
UEN_LETTERS = 'ABCDEFGHJKZ'
FAST1_VERSION = '1'
FAST1_FIELDS = 14
DEFAULT_STORE_PATH = 'SystemLogs/paynow.sqlite'
DEFAULT_SOURCE_BANK = '9092'
CUSTOMER_RETURN_SECONDS = 24 * 60 * 60
STAFF_COMPLAINT_DAYS = 90
SGT_OFFSET_MINUTES = 480
DEFAULT_SGDUSD = Decimal('0.74')
BLOCKED_E2E_PREFIXES = (
    'IMAD', 'UETR', 'NPP', 'AUNPP', 'RRN', 'STAN', 'ARN', 'RFI', 'RFR', 'RTR',
)
E2E_PREFIX = {'paynow': 'PNW', 'fast': 'FAST', 'meps': 'MEPS', 'giro': 'GIRO'}


class PayNowError(ValueError):
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
        raise PayNowError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise PayNowError('invalid_name', 'Beneficiary legal name must be 2-80 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise PayNowError('invalid_purpose', 'Unknown PayNow purpose.')
    return text


def normalize_currency(value: Any, *, default: str = 'SGD') -> str:
    text = str(value or default).strip().upper().replace('$', '')
    if text in {'S', 'S$', 'SGD.', 'SG'}:
        text = 'SGD'
    if text != 'SGD':
        raise PayNowError('invalid_currency', 'PayNow / FAST / MEPS+ / GIRO is SGD only.')
    return 'SGD'


def normalize_rail(value: Any, *, default: str = 'paynow') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text not in RAILS:
        raise PayNowError('invalid_rail', 'Rail must be paynow, fast, meps, or giro.')
    return text


def normalize_proxy_type(value: Any, *, default: str = '') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PROXY_ALIASES.get(text, text)
    if not text:
        return ''
    if text not in PROXY_TYPES:
        raise PayNowError('invalid_proxy', 'PayNow proxy must be nric, phone, or uen.')
    return text


def normalize_bank_code(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise PayNowError('invalid_bank', 'MAS bank code is required.')
        return ''
    if len(digits) != 4 or digits == '0000':
        raise PayNowError('invalid_bank', 'MAS bank code must be four digits.')
    return digits


def bank_name(value: Any, *, directory: Optional[Dict[str, str]] = None) -> str:
    try:
        code = normalize_bank_code(value)
    except PayNowError:
        return ''
    mapping = directory if directory is not None else DEFAULT_BANKS
    return mapping.get(code, '')


def normalize_external_account(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise PayNowError('invalid_account', 'Beneficiary account is required.')
        return ''
    if not (6 <= len(digits) <= 16):
        raise PayNowError('invalid_account', 'Singapore account must be 6-16 digits.')
    return digits


def mask_account(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return ('*' * max(0, len(digits) - 4)) + digits[-4:]


def nric_check_letter(prefix: str, body: str) -> str:
    prefix = prefix.upper()
    if prefix not in {'S', 'T', 'F', 'G', 'M'} or len(body) != 7 or not body.isdigit():
        raise PayNowError('invalid_nric', 'NRIC/FIN must be prefix + 7 digits.')
    total = sum(int(ch) * w for ch, w in zip(body, NRIC_WEIGHTS))
    if prefix == 'T':
        total += 4
    elif prefix == 'G':
        total += 4
    elif prefix == 'M':
        total += 3
    remainder = total % 11
    if prefix in {'S', 'T'}:
        return NRIC_ST[remainder]
    if prefix in {'F', 'G'}:
        return NRIC_FG[remainder]
    return NRIC_M[remainder]


def nric_check_ok(value: str) -> bool:
    text = str(value or '').strip().upper()
    if not re.fullmatch(r'[STFGM]\d{7}[A-Z]', text):
        return False
    try:
        return nric_check_letter(text[0], text[1:8]) == text[8]
    except PayNowError:
        return False


def compose_nric(prefix: Any, body: Any) -> str:
    letter = str(prefix or '').strip().upper()
    digits = ''.join(ch for ch in str(body or '') if ch.isdigit())
    return letter + digits + nric_check_letter(letter, digits)


def normalize_nric(value: Any, *, required: bool = False) -> str:
    text = re.sub(r'[\s-]', '', str(value or '')).upper()
    if not text:
        if required:
            raise PayNowError('invalid_nric', 'NRIC/FIN is required.')
        return ''
    if not nric_check_ok(text):
        raise PayNowError('invalid_nric', 'NRIC/FIN failed the checksum.')
    return text


def mask_nric(value: Any) -> str:
    text = re.sub(r'[\s-]', '', str(value or '')).upper()
    if len(text) != 9:
        return 'S*******X'
    return text[0] + '****' + text[-4:]


def uen_check_letter(body: str) -> str:
    digits = ''.join(ch for ch in str(body or '') if ch.isdigit())
    if len(digits) != 8:
        raise PayNowError('invalid_uen', 'UEN body must be 8 digits.')
    total = sum(int(ch) * w for ch, w in zip(digits, UEN_WEIGHTS))
    return UEN_LETTERS[total % 11]


def uen_check_ok(value: str) -> bool:
    text = str(value or '').strip().upper()
    if not re.fullmatch(r'\d{8}[A-Z]', text):
        return False
    try:
        return uen_check_letter(text[:8]) == text[8]
    except PayNowError:
        return False


def compose_uen(body: Any) -> str:
    digits = ''.join(ch for ch in str(body or '') if ch.isdigit())
    return digits + uen_check_letter(digits)


def normalize_uen(value: Any, *, required: bool = False) -> str:
    text = re.sub(r'[\s-]', '', str(value or '')).upper()
    if not text:
        if required:
            raise PayNowError('invalid_uen', 'UEN is required.')
        return ''
    if not uen_check_ok(text):
        raise PayNowError('invalid_uen', 'UEN failed the check letter.')
    return text


def mask_uen(value: Any) -> str:
    text = re.sub(r'[\s-]', '', str(value or '')).upper()
    if len(text) < 5:
        return '********X'
    return text[:2] + '****' + text[-3:]


def normalize_phone(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise PayNowError('invalid_phone', 'Singapore mobile is required.')
        return ''
    if digits.startswith('65') and len(digits) == 10:
        national = digits[2:]
    elif digits.startswith('0') and len(digits) == 9:
        national = digits[1:]
    else:
        national = digits
    if len(national) != 8 or national[0] not in {'8', '9'}:
        raise PayNowError('invalid_phone', 'PayNow mobile must be an 8-digit +65 8/9 number.')
    return '+65' + national


def mask_phone(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if digits.startswith('65') and len(digits) == 10:
        national = digits[2:]
    else:
        national = digits
    if len(national) != 8:
        return '+65****'
    return '+65****' + national[-4:]


def normalize_proxy(proxy_type: Any, value: Any, *, required: bool = False) -> Tuple[str, str]:
    kind = normalize_proxy_type(proxy_type)
    if not kind:
        if required:
            raise PayNowError('invalid_proxy', 'PayNow proxy type is required.')
        return '', ''
    if kind == 'nric':
        return kind, normalize_nric(value, required=True)
    if kind == 'uen':
        return kind, normalize_uen(value, required=True)
    return kind, normalize_phone(value, required=True)


def mask_proxy(proxy_type: str, value: str) -> str:
    if proxy_type == 'nric':
        return mask_nric(value)
    if proxy_type == 'uen':
        return mask_uen(value)
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
        raise PayNowError('invalid_e2e', 'Cycle date must be YYYYMMDD.')
    if sequence < 1 or sequence > 999999:
        raise PayNowError('invalid_e2e', 'Sequence out of range.')
    return '%s%s%06d' % (E2E_PREFIX[kind], cycle_date, sequence)


def normalize_e2e(value: Any) -> str:
    text = str(value or '').strip().upper()
    if any(text.startswith(prefix) for prefix in BLOCKED_E2E_PREFIXES):
        raise PayNowError('invalid_e2e', 'E2E ID belongs to another clearing system.')
    if not re.fullmatch(r'(PNW|FAST|MEPS|GIRO)\d{8}\d{6}', text):
        raise PayNowError('invalid_e2e', 'E2E ID must be FAST/PNW/MEPS/GIRO + date + seq.')
    return text


def compose_fast1(
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
) -> str:
    clearing = CLEARING_BY_RAIL[normalize_rail(rail)]
    fields = [
        FAST1_VERSION,
        clearing,
        'SGD',
        str(amount),
        bank_code or '0000',
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
    if len(fields) != FAST1_FIELDS:
        raise PayNowError('invalid_fast1', 'FAST1 record is the wrong width.')
    line = '|'.join(fields)
    if '<' in line or '>' in line:
        raise PayNowError('invalid_fast1', 'FAST1 rejects XML markup.')
    return line


def parse_fast1(payload: Any) -> Dict[str, str]:
    text = str(payload or '').strip()
    upper = text.upper()
    if not text:
        raise PayNowError('invalid_fast1', 'FAST1 payload is empty.')
    if '<?XML' in upper or '<!DOCTYPE' in upper or '<FAST' in upper:
        raise PayNowError('invalid_fast1', 'FAST1 rejects XML / DOCTYPE payloads.')
    parts = text.split('|')
    if len(parts) != FAST1_FIELDS:
        raise PayNowError('invalid_fast1', 'FAST1 must be a 14-field pipe record.')
    if parts[0] != FAST1_VERSION:
        raise PayNowError('invalid_fast1', 'FAST1 version must be 1.')
    clearing = parts[1].upper()
    if clearing in REJECTED_CLEARING:
        raise PayNowError('invalid_clearing', 'Clearing %s is not FAST/MEPS/GIRO/PAYNOW.' % clearing)
    if clearing not in {'FAST', 'MEPS', 'GIRO', 'PAYNOW'}:
        raise PayNowError('invalid_clearing', 'Unknown FAST1 clearing.')
    if parts[2].upper() != 'SGD':
        raise PayNowError('invalid_currency', 'FAST1 is SGD only.')
    return {
        'version': parts[0],
        'clearing': clearing,
        'currency': 'SGD',
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
class PayIdResolution:
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


def resolve_paynow(
    proxy_type: Any,
    proxy: Any,
    presented_name: Any = '',
    *,
    directory: Optional[Dict[str, str]] = None,
) -> PayIdResolution:
    kind, value = normalize_proxy(proxy_type, proxy, required=True)
    mapping = directory if directory is not None else DEFAULT_PAYNOW_DIRECTORY
    registered = mapping.get(proxy_key(kind, value), '')
    presented = ' '.join(str(presented_name or '').strip().split())
    if not registered:
        outcome = COP_UNAVAILABLE
    elif not presented:
        outcome = COP_MATCH
        presented = registered
    else:
        outcome = cop_outcome(registered, presented)
    return PayIdResolution(
        outcome=outcome,
        registered_name=registered,
        presented_name=presented,
        proxy_type=kind,
        proxy_mask=mask_proxy(kind, value),
    )


def convert_sgd_to_usd(amount: Decimal, rate: Decimal) -> Decimal:
    if rate <= 0:
        raise PayNowError('invalid_fx', 'SGDUSD rate must be positive.')
    return (amount * rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def _observed_sunday(day: date) -> date:
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def sg_mas_holidays(year: int) -> set:
    """Fixed MAS-observed holidays. Moveable feasts are injected via extra_holidays."""
    return {
        _observed_sunday(date(year, 1, 1)),
        _observed_sunday(date(year, 5, 1)),
        _observed_sunday(date(year, 8, 9)),
        _observed_sunday(date(year, 12, 25)),
    }


class SgClock:
    """Singapore FAST 24/7 vs MEPS+/GIRO banking-day clock. Injectable offset and holidays."""

    def __init__(
        self,
        *,
        meps_open_hour: int = 9,
        meps_cutoff_hour: int = 17,
        meps_cutoff_minute: int = 30,
        tz_offset_minutes: int = SGT_OFFSET_MINUTES,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.meps_open_hour = meps_open_hour
        self.meps_cutoff_hour = meps_cutoff_hour
        self.meps_cutoff_minute = meps_cutoff_minute
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
        return sg_mas_holidays(year) | {day for day in self.extra_holidays if day.year == year}

    def is_business_day(self, day: date) -> bool:
        if day.weekday() >= 5:
            return False
        return day not in self.holidays(day.year)

    def next_business_day(self, day: date) -> date:
        cursor = day
        while not self.is_business_day(cursor):
            cursor += timedelta(days=1)
        return cursor

    def value_date(self, ts: float, *, rail: str = 'paynow') -> date:
        local = self.local(ts)
        kind = normalize_rail(rail)
        day = local.date()
        if kind in {'paynow', 'fast'}:
            return day
        if kind == 'giro':
            start = day + timedelta(days=1)
            if self.is_after_meps_cutoff(ts) and self.is_business_day(day):
                start = day + timedelta(days=1)
            return self.next_business_day(start)
        if not self.is_business_day(day) or self.is_after_meps_cutoff(ts):
            return self.next_business_day(day + timedelta(days=1))
        return day

    def is_after_meps_cutoff(self, ts: float) -> bool:
        local = self.local(ts)
        cutoff = local.replace(
            hour=self.meps_cutoff_hour, minute=self.meps_cutoff_minute, second=0, microsecond=0,
        )
        open_at = local.replace(hour=self.meps_open_hour, minute=0, second=0, microsecond=0)
        return local < open_at or local >= cutoff

    def cycle_date(self, ts: float, *, rail: str = 'paynow') -> str:
        return self.value_date(ts, rail=rail).strftime('%Y%m%d')

    def snapshot(self, ts: float, *, rail: str = 'paynow') -> Dict[str, Any]:
        local = self.local(ts)
        value = self.value_date(ts, rail=rail)
        kind = normalize_rail(rail)
        after = False
        if kind in {'meps', 'giro'}:
            after = self.is_after_meps_cutoff(ts) or not self.is_business_day(local.date())
        return {
            'rail': kind,
            'timezone': 'SGT',
            'local': local.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'cutoff': '%02d:%02d' % (self.meps_cutoff_hour, self.meps_cutoff_minute),
            'after_cutoff': after,
            'always_on': kind in {'paynow', 'fast'},
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
class PayNowPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    allow_credit: bool = False
    require_cop_match: bool = True
    max_beneficiaries: int = 12
    max_payments: int = 120
    min_amount: Decimal = Decimal('0.01')
    fast_cap: Decimal = Decimal('200000.00')
    giro_cap: Decimal = Decimal('200000.00')
    meps_cap: Decimal = Decimal('10000000.00')
    outbound_fee: Decimal = Decimal('0.00')
    meps_fee: Decimal = Decimal('20.00')
    giro_fee: Decimal = Decimal('0.50')
    dual_control_threshold: Decimal = Decimal('10000.00')
    sgdusd: Decimal = DEFAULT_SGDUSD
    meps_cutoff_hour: int = 17
    meps_cutoff_minute: int = 30
    tz_offset_minutes: int = SGT_OFFSET_MINUTES
    source_bank: str = DEFAULT_SOURCE_BANK
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'PayNowPolicy':
        extra = _env_list('PAYNOW_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('PAYNOW_ENABLED', True),
            customer_manage=_env_bool('PAYNOW_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('PAYNOW_CUSTOMER_SEND', True),
            allow_credit=_env_bool('PAYNOW_ALLOW_CREDIT', False),
            require_cop_match=_env_bool('PAYNOW_REQUIRE_COP', True),
            max_beneficiaries=max(1, _env_int('PAYNOW_MAX_BENEFICIARIES', 12)),
            max_payments=max(1, _env_int('PAYNOW_MAX_PAYMENTS', 120)),
            min_amount=_env_money('PAYNOW_MIN_AMOUNT', '0.01'),
            fast_cap=_env_money('PAYNOW_FAST_CAP', '200000.00'),
            giro_cap=_env_money('PAYNOW_GIRO_CAP', '200000.00'),
            meps_cap=_env_money('PAYNOW_MEPS_CAP', '10000000.00'),
            outbound_fee=_env_money('PAYNOW_FEE', '0.00'),
            meps_fee=_env_money('PAYNOW_MEPS_FEE', '20.00'),
            giro_fee=_env_money('PAYNOW_GIRO_FEE', '0.50'),
            dual_control_threshold=_env_money('PAYNOW_DUAL_CONTROL', '10000.00'),
            sgdusd=_env_money('PAYNOW_FX_SGDUSD', '0.74'),
            meps_cutoff_hour=max(0, min(23, _env_int('PAYNOW_CUTOFF_HOUR', 17))),
            meps_cutoff_minute=max(0, min(59, _env_int('PAYNOW_CUTOFF_MINUTE', 30))),
            tz_offset_minutes=_env_int('PAYNOW_TZ_OFFSET', SGT_OFFSET_MINUTES),
            source_bank=normalize_bank_code(os.environ.get('PAYNOW_SOURCE_BANK', DEFAULT_SOURCE_BANK)),
            watchlist=watch,
            extra_holidays=_env_list('PAYNOW_HOLIDAYS'),
        )

    def cap_for(self, rail: str) -> Decimal:
        if rail == 'meps':
            return self.meps_cap
        if rail == 'giro':
            return self.giro_cap
        return self.fast_cap

    def fee_for(self, rail: str) -> Decimal:
        if rail == 'meps':
            return self.meps_fee
        if rail == 'giro':
            return self.giro_fee
        return self.outbound_fee


@dataclass
class PayNowBeneficiary:
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
class PayNowPayment:
    payment_id: str
    trace_id: str
    beneficiary_id: str
    userid: str
    internal_account: str
    rail: str
    sgd_amount: str
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
            'amount': self.sgd_amount,
            'sgd_amount': self.sgd_amount,
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
            'instant': self.rail in {'paynow', 'fast'},
        }


def _clone_bene(row: PayNowBeneficiary) -> PayNowBeneficiary:
    return PayNowBeneficiary(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_pay(row: PayNowPayment) -> PayNowPayment:
    return PayNowPayment(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _bene_from_row(row: Any) -> PayNowBeneficiary:
    return PayNowBeneficiary(
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


def _pay_from_row(row: Any) -> PayNowPayment:
    return PayNowPayment(
        payment_id=row['payment_id'],
        trace_id=row['trace_id'],
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        rail=row['rail'],
        sgd_amount=row['sgd_amount'],
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


class MemoryPayNowStore:
    def __init__(self) -> None:
        self._benes: Dict[str, PayNowBeneficiary] = {}
        self._pays: Dict[str, PayNowPayment] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_beneficiary(self, row: PayNowBeneficiary) -> None:
        with self._lock:
            self._benes[row.beneficiary_id] = _clone_bene(row)

    def get_beneficiary(self, beneficiary_id: str) -> Optional[PayNowBeneficiary]:
        with self._lock:
            row = self._benes.get(beneficiary_id)
            return _clone_bene(row) if row else None

    def update_beneficiary(self, row: PayNowBeneficiary) -> None:
        self.put_beneficiary(row)

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[PayNowBeneficiary]:
        with self._lock:
            rows = list(self._benes.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != BENE_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return [_clone_bene(row) for row in rows]

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[PayNowBeneficiary]:
        wanted = nickname.strip().lower()
        for row in self.list_beneficiaries(userid, include_archived=False):
            if row.status in OPEN_BENE and row.nickname.lower() == wanted:
                return row
        return None

    def find_beneficiary_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[PayNowBeneficiary]:
        for row in self.list_beneficiaries(userid, include_archived=False):
            if row.status in OPEN_BENE and row.fingerprint == fingerprint:
                return row
        return None

    def put_payment(self, row: PayNowPayment) -> PayNowPayment:
        with self._lock:
            existing_id = self._by_trace.get(row.trace_id)
            if existing_id is not None:
                return _clone_pay(self._pays[existing_id])
            self._pays[row.payment_id] = _clone_pay(row)
            self._by_trace[row.trace_id] = row.payment_id
            return _clone_pay(row)

    def update_payment(self, row: PayNowPayment) -> None:
        with self._lock:
            self._pays[row.payment_id] = _clone_pay(row)
            self._by_trace[row.trace_id] = row.payment_id

    def get_payment(self, payment_id: str) -> Optional[PayNowPayment]:
        with self._lock:
            row = self._pays.get(payment_id)
            return _clone_pay(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[PayNowPayment]:
        with self._lock:
            payment_id = self._by_trace.get(trace_id)
            row = self._pays.get(payment_id) if payment_id else None
            return _clone_pay(row) if row else None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[PayNowPayment]:
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


class SqlitePayNowStore:
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
                    sgd_amount TEXT NOT NULL,
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

    def put_beneficiary(self, row: PayNowBeneficiary) -> None:
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

    def get_beneficiary(self, beneficiary_id: str) -> Optional[PayNowBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM beneficiaries WHERE beneficiary_id = ?', (beneficiary_id,),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def update_beneficiary(self, row: PayNowBeneficiary) -> None:
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

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[PayNowBeneficiary]:
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

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[PayNowBeneficiary]:
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

    def find_beneficiary_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[PayNowBeneficiary]:
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

    def put_payment(self, row: PayNowPayment) -> PayNowPayment:
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
                    sgd_amount, usd_amount, fx_rate, fee, fee_status, nickname, legal_name,
                    proxy_type, proxy_mask, bank_code, account_last4, purpose, memo, status,
                    e2e_id, value_date, cop_outcome, actor, releaser, ofac_hit, ofac_match,
                    created_at, updated_at, sent_at, completed_at, recalled_at, note, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.payment_id, row.trace_id, row.beneficiary_id, row.userid,
                    row.internal_account, row.rail, row.sgd_amount, row.usd_amount,
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

    def update_payment(self, row: PayNowPayment) -> None:
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

    def get_payment(self, payment_id: str) -> Optional[PayNowPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE payment_id = ?', (payment_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[PayNowPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE trace_id = ?', (trace_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[PayNowPayment]:
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


class PayNowService:
    def __init__(
        self,
        policy: PayNowPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[SgClock] = None,
        directory: Optional[Dict[str, str]] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or SgClock(
            meps_cutoff_hour=policy.meps_cutoff_hour,
            meps_cutoff_minute=policy.meps_cutoff_minute,
            tz_offset_minutes=policy.tz_offset_minutes,
            extra_holidays=policy.extra_holidays,
        )
        self.directory = dict(directory or DEFAULT_PAYNOW_DIRECTORY)

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise PayNowError('paynow_disabled', 'PayNow / FAST is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise PayNowError('paynow_forbidden', 'Customers cannot manage PayNow beneficiaries.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise PayNowError('paynow_forbidden', 'Customers cannot originate PayNow payments.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise PayNowError('paynow_forbidden', 'Staff only.')

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
            raise PayNowError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise PayNowError('credit_not_allowed', 'Credit accounts cannot originate PayNow.')

    def _assert_amount(self, dollars: Decimal, rail: str) -> None:
        cap = self.policy.cap_for(rail)
        if dollars < self.policy.min_amount or dollars > cap:
            raise PayNowError('amount_out_of_range', 'Amount is outside the %s cap.' % rail)

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal, rail: str) -> bool:
        if rail in {'paynow', 'fast'}:
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
    ) -> PayIdResolution:
        self._require_send(actor_type)
        _ = actor
        return resolve_paynow(proxy_type, proxy, presented_name, directory=self.directory)

    def quote_fx(self, amount: Any, *, rail: Any = 'paynow') -> Dict[str, Any]:
        kind = normalize_rail(rail)
        sgd = parse_money(amount)
        self._assert_amount(sgd, kind)
        usd = convert_sgd_to_usd(sgd, self.policy.sgdusd)
        fee = compute_fee(sgd, self.policy.fee_for(kind))
        return {
            'rail': kind,
            'currency': 'SGD',
            'sgd_amount': money_str(sgd),
            'usd_amount': money_str(usd),
            'fx_rate': money_str(self.policy.sgdusd),
            'fee': money_str(fee),
            'usd_total': money_str(usd + convert_sgd_to_usd(fee, self.policy.sgdusd) if fee else usd),
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
    ) -> PayNowBeneficiary:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise PayNowError('paynow_forbidden', 'Not allowed to add beneficiaries for this customer.')
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        kind, value = normalize_proxy(proxy_type, proxy, required=False)
        bank = normalize_bank_code(bank_code, required=False)
        external = normalize_external_account(account_number, required=False)
        if not kind and not (bank and external):
            raise PayNowError('invalid_proxy', 'PayNow proxy or MAS bank + account is required.')
        if self.store.find_beneficiary_by_nickname(owner_userid, name) is not None:
            raise PayNowError('beneficiary_duplicate', 'A beneficiary with that nickname already exists.')
        fingerprint = fingerprint_for(owner_userid, kind, value, bank, external)
        if self.store.find_beneficiary_by_fingerprint(owner_userid, fingerprint) is not None:
            raise PayNowError('beneficiary_duplicate', 'That PayNow destination is already on file.')
        open_rows = [row for row in self.store.list_beneficiaries(owner_userid) if row.status in OPEN_BENE]
        if len(open_rows) >= self.policy.max_beneficiaries:
            raise PayNowError('beneficiary_limit', 'PayNow beneficiary limit reached.')
        now = float(self.clock())
        row = PayNowBeneficiary(
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

    def get_beneficiary(self, *, beneficiary_id: str, actor: str, actor_type: str) -> PayNowBeneficiary:
        self._require_enabled()
        row = self.store.get_beneficiary(beneficiary_id)
        if row is None:
            raise PayNowError('beneficiary_not_found', 'PayNow beneficiary not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise PayNowError('paynow_forbidden', 'Not allowed to view this beneficiary.')
        return row

    def enforce_beneficiary(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> PayNowBeneficiary:
        row = self.get_beneficiary(beneficiary_id=beneficiary_id, actor=actor, actor_type=actor_type)
        if row.status == BENE_ARCHIVED:
            raise PayNowError('already_archived', 'Beneficiary is archived.')
        if row.status == BENE_PAUSED:
            raise PayNowError('beneficiary_paused', 'Beneficiary is paused.')
        if require_active and row.status != BENE_ACTIVE:
            raise PayNowError('invalid_status', 'Beneficiary is not active.')
        return row

    def set_beneficiary_status(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> PayNowBeneficiary:
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
            raise PayNowError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == BENE_ARCHIVED:
            raise PayNowError('already_archived', 'Beneficiary is already archived.')
        if wanted == BENE_PAUSED:
            if row.status == BENE_PAUSED:
                raise PayNowError('already_paused', 'Beneficiary is already paused.')
            if row.status != BENE_ACTIVE:
                raise PayNowError('invalid_status', 'Only an active beneficiary can be paused.')
        elif wanted == BENE_ACTIVE:
            if row.status == BENE_ACTIVE:
                raise PayNowError('already_active', 'Beneficiary is already active.')
            if row.status != BENE_PAUSED:
                raise PayNowError('invalid_status', 'Only a paused beneficiary can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_beneficiary(row)
        return row

    def _cop_for(self, bene: PayNowBeneficiary, *, presented_name: Any = '', override: bool = False) -> PayIdResolution:
        if not bene.proxy_type:
            return PayIdResolution(
                outcome=COP_UNAVAILABLE, registered_name='', presented_name=str(presented_name or ''),
                proxy_type='', proxy_mask='',
            )
        resolution = resolve_paynow(
            bene.proxy_type, bene.proxy, presented_name or bene.legal_name, directory=self.directory,
        )
        if override:
            return resolution
        if self.policy.require_cop_match and resolution.outcome in {COP_NONE, COP_UNAVAILABLE} and bene.proxy_type:
            raise PayNowError(
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
        rail: Any = 'paynow',
        internal_account: Any = None,
        waive_fee: bool = False,
        presented_name: Any = '',
    ) -> Dict[str, Any]:
        self._require_send(actor_type)
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise PayNowError('paynow_forbidden', 'Beneficiary does not belong to this customer.')
        kind = normalize_rail(rail)
        if kind == 'paynow' and not bene.proxy_type:
            raise PayNowError('invalid_proxy', 'PayNow overlay requires a registered proxy.')
        if kind in {'fast', 'meps', 'giro'} and not (bene.bank_code and bene.account_number) and not bene.proxy_type:
            raise PayNowError('invalid_bank', 'FAST/MEPS/GIRO needs a bank+account or PayNow proxy.')
        sgd = parse_money(amount)
        self._assert_amount(sgd, kind)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(sgd, self.policy.fee_for(kind), waived=staff_waive)
        usd = convert_sgd_to_usd(sgd, self.policy.sgdusd)
        now = float(self.clock())
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        cop = self._cop_for(bene, presented_name=presented_name)
        clock = self.calendar.snapshot(now, rail=kind)
        return {
            'amount': money_str(sgd),
            'sgd_amount': money_str(sgd),
            'usd_amount': money_str(usd),
            'fx_rate': money_str(self.policy.sgdusd),
            'fee': money_str(fee),
            'total_usd': money_str(usd + convert_sgd_to_usd(fee, self.policy.sgdusd)),
            'internal_account': account,
            'rail': kind,
            'beneficiary': bene.to_dict(),
            'ofac': ofac.to_dict(),
            'cop': cop.to_dict(),
            'dual_control': self._needs_dual_control(sgd, kind),
            'clock': clock,
        }

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        bene: PayNowBeneficiary,
        account: str,
        rail: str,
        sgd: Decimal,
        usd: Decimal,
        fee: Decimal,
        purpose: str,
        memo: str,
        trace_id: str,
        ofac: ScreenResult,
        cop: PayIdResolution,
        waive_fee: bool,
    ) -> PayNowPayment:
        now = float(self.clock())
        value = self.calendar.cycle_date(now, rail=rail)
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        clock = self.calendar.snapshot(now, rail=rail)
        if ofac.hit:
            status = PN_HELD
        elif self._needs_dual_control(sgd, rail):
            status = PN_PENDING
        elif rail == 'giro' or clock['after_cutoff']:
            status = PN_QUEUED
        elif rail in {'paynow', 'fast'}:
            status = PN_COMPLETED
        else:
            status = PN_SENT
        payment = PayNowPayment(
            payment_id=uuid.uuid4().hex,
            trace_id=trace_id,
            beneficiary_id=bene.beneficiary_id,
            userid=owner_userid,
            internal_account=account,
            rail=rail,
            sgd_amount=money_str(sgd),
            usd_amount=money_str(usd),
            fx_rate=money_str(self.policy.sgdusd),
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

    def _transmit(self, payment: PayNowPayment, *, actor: str, complete: bool = False) -> PayNowPayment:
        now = float(self.clock())
        cycle = payment.value_date or self.calendar.cycle_date(now, rail=payment.rail)
        seq = self.store.next_e2e_sequence(cycle, payment.rail)
        payment.e2e_id = compose_e2e(payment.rail, cycle, seq)
        usd = parse_money(payment.usd_amount)
        fee_sgd = parse_money(payment.fee, allow_zero=True)
        fee_usd = convert_sgd_to_usd(fee_sgd, parse_money(payment.fx_rate, allow_zero=False)) if fee_sgd else Decimal('0.00')
        remark = 'paynow to %s' % payment.nickname
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
                fee_result = self.debit_fn(payment.internal_account, money_str(fee_usd), 'paynow fee %s' % mask_e2e(payment.e2e_id))
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
            if complete or payment.rail in {'paynow', 'fast'}:
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
        rail: Any = 'paynow',
        internal_account: Any = None,
        purpose: Any = 'other',
        memo: Any = '',
        trace_id: Any = None,
        waive_fee: bool = False,
        presented_name: Any = '',
        cop_override: bool = False,
    ) -> Tuple[PayNowPayment, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise PayNowError('paynow_forbidden', 'Not allowed to originate PayNow for this customer.')
        staff_override = bool(cop_override) and actor_type in EMPLOYEE_ROLES
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise PayNowError('paynow_forbidden', 'Beneficiary does not belong to this customer.')
        kind = normalize_rail(rail)
        if kind == 'paynow' and not bene.proxy_type:
            raise PayNowError('invalid_proxy', 'PayNow overlay requires a registered proxy.')
        sgd = parse_money(amount)
        self._assert_amount(sgd, kind)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(sgd, self.policy.fee_for(kind), waived=staff_waive)
        usd = convert_sgd_to_usd(sgd, self.policy.sgdusd)
        trace = normalize_id(trace_id)
        existing = self.store.get_payment_by_trace(trace)
        if existing is not None:
            return existing, False
        if len(self.store.list_payments(owner_userid)) >= self.policy.max_payments:
            raise PayNowError('paynow_limit', 'PayNow history limit reached.')
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        cop = self._cop_for(bene, presented_name=presented_name, override=staff_override)
        if staff_override and cop.outcome in {COP_NONE, COP_UNAVAILABLE}:
            cop = PayIdResolution(
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
            sgd=sgd,
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
            raise PayNowError('nsf', 'Insufficient funds for PayNow.', payment=stored)
        if stored.status == PN_FAILED:
            raise PayNowError('failed', 'PayNow debit did not complete.', payment=stored)
        return stored, True

    def get_payment(self, *, payment_id: str, actor: str, actor_type: str) -> PayNowPayment:
        self._require_enabled()
        row = self.store.get_payment(payment_id)
        if row is None:
            raise PayNowError('payment_not_found', 'PayNow payment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise PayNowError('paynow_forbidden', 'Not allowed to view this payment.')
        return row

    def cancel_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> PayNowPayment:
        self._require_send(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise PayNowError('paynow_forbidden', 'Not allowed to cancel this payment.')
        if row.status not in CANCELABLE:
            raise PayNowError('not_cancelable', 'Payment can no longer be cancelled.')
        row.status = PN_CANCELLED
        row.actor = str(actor)
        row.note = normalize_note(note)
        row.updated_at = float(self.clock())
        self.store.update_payment(row)
        return row

    def release_payment(self, *, payment_id: str, actor: str, actor_type: str) -> PayNowPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.ofac_hit:
            raise PayNowError('ofac_hold', 'OFAC hold must be overridden first.')
        if row.status not in {PN_PENDING, PN_QUEUED}:
            raise PayNowError('not_releasable', 'Payment is not waiting for release.')
        if row.status == PN_PENDING and row.actor == str(actor):
            raise PayNowError('same_approver', 'Maker cannot release their own payment.')
        complete = row.rail in {'paynow', 'fast'}
        self._transmit(row, actor=actor, complete=complete)
        if row.status == PN_NSF:
            self.store.update_payment(row)
            raise PayNowError('nsf', 'Insufficient funds for PayNow.', payment=row)
        if row.status == PN_FAILED:
            self.store.update_payment(row)
            raise PayNowError('failed', 'PayNow debit did not complete.', payment=row)
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
    ) -> PayNowPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status not in {PN_HELD, PN_QUEUED, PN_PENDING}:
            raise PayNowError('not_rejectable', 'Payment cannot be rejected.')
        row.status = PN_REJECTED
        row.reason = normalize_note(reason, limit=40) or 'other'
        row.note = normalize_note(note)
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_payment(row)
        return row

    def complete_payment(self, *, payment_id: str, actor: str, actor_type: str) -> PayNowPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status == PN_COMPLETED:
            raise PayNowError('already_completed', 'Payment is already completed.')
        if row.status != PN_SENT:
            raise PayNowError('not_completable', 'Only a sent payment can be completed.')
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
    ) -> PayNowPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status == PN_RECALLED:
            raise PayNowError('already_recalled', 'Payment is already recalled.')
        if row.status not in {PN_SENT, PN_COMPLETED}:
            raise PayNowError('not_recallable', 'Payment cannot be recalled.')
        now = float(self.clock())
        window = STAFF_COMPLAINT_DAYS * 24 * 60 * 60
        sent_at = row.sent_at or row.created_at
        if row.rail in {'paynow', 'fast'} and (now - sent_at) > window:
            raise PayNowError('not_recallable', 'FAST recall window has closed.')
        usd = parse_money(row.usd_amount)
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(row.internal_account, money_str(usd), 'paynow recall %s' % mask_e2e(row.e2e_id))
            except Exception as exc:
                raise PayNowError('recall_failed', 'Recall credit did not complete.', payment=row) from exc
            if _classify_money_result(result) != 'ok':
                raise PayNowError('recall_failed', 'Recall credit did not complete.', payment=row)
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
    ) -> PayNowPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status != PN_HELD:
            raise PayNowError('not_releasable', 'No OFAC hold to override.')
        row.ofac_hit = 0
        row.note = normalize_note(note)
        complete = row.rail in {'paynow', 'fast'}
        if self._needs_dual_control(parse_money(row.sgd_amount), row.rail):
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
    ) -> PayNowPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        row.cop_outcome = COP_MATCH
        row.note = normalize_note(note)
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_payment(row)
        return row

    def waive_fee(self, *, payment_id: str, actor: str, actor_type: str) -> PayNowPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status not in CANCELABLE | {PN_SENT}:
            raise PayNowError('not_releasable', 'Fee can no longer be waived.')
        row.fee_status = FEE_WAIVED
        row.fee = money_str(Decimal('0.00'))
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_payment(row)
        return row

    def run_due(self, userid: str) -> List[PayNowPayment]:
        now = float(self.clock())
        due = []
        for row in self.store.list_payments(userid):
            if row.status != PN_QUEUED:
                continue
            clock = self.calendar.snapshot(now, rail=row.rail)
            if clock['after_cutoff']:
                continue
            complete = row.rail in {'paynow', 'fast'}
            self._transmit(row, actor=row.actor or 'system', complete=complete)
            self.store.update_payment(row)
            due.append(row)
        return due

    def export_fast1(self, userid: str, *, actor: str, actor_type: str) -> str:
        self._require_staff(actor_type)
        _ = actor
        lines = []
        for index, row in enumerate(self.store.list_payments(userid), start=1):
            if row.status not in {PN_SENT, PN_COMPLETED}:
                continue
            lines.append(compose_fast1(
                rail=row.rail,
                amount=row.sgd_amount,
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
            ))
        return '\n'.join(lines)

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self._require_enabled()
        _ = actor
        if actor_type not in EMPLOYEE_ROLES and actor and actor != userid:
            raise PayNowError('paynow_forbidden', 'Not allowed to view this PayNow book.')
        benes = self.store.list_beneficiaries(userid)
        pays = self.store.list_payments(userid)
        ytd = Decimal('0.00')
        ytd_fees = Decimal('0.00')
        recalled = Decimal('0.00')
        for row in pays:
            if row.status in {PN_SENT, PN_COMPLETED}:
                ytd += parse_money(row.sgd_amount, allow_zero=True)
                ytd_fees += parse_money(row.fee, allow_zero=True)
            if row.status == PN_RECALLED:
                recalled += parse_money(row.sgd_amount, allow_zero=True)
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
            'fx_rate': money_str(self.policy.sgdusd),
            'clock': self.calendar.snapshot(now, rail='paynow'),
            'meps_clock': self.calendar.snapshot(now, rail='meps'),
        }


_SERVICE: Optional[PayNowService] = None


def set_service(service: Optional[PayNowService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[PayNowService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('PAYNOW_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryPayNowStore()
    path = os.environ.get('PAYNOW_DB', DEFAULT_STORE_PATH)
    return SqlitePayNowStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[SgClock] = None,
    directory: Optional[Dict[str, str]] = None,
) -> PayNowService:
    if store is None:
        store = default_store()
    return PayNowService(
        PayNowPolicy.from_env(),
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
        'paynow_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_completed': 409,
        'already_recalled': 409,
        'nsf': 409,
        'failed': 409,
        'recall_failed': 409,
        'paynow_forbidden': 403,
        'paynow_disabled': 403,
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
        'invalid_nric': 400,
        'invalid_uen': 400,
        'invalid_phone': 400,
        'invalid_proxy': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_e2e': 400,
        'invalid_fast1': 400,
        'invalid_clearing': 400,
        'invalid_currency': 400,
        'invalid_rail': 400,
        'invalid_fx': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_beneficiary': 400,
        'missing_payment': 400,
    }.get(code, 400)


def _error_body(exc: PayNowError) -> Dict[str, Any]:
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
    except PayNowError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: PayNowService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'PayNow': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_lookup(service: PayNowService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        resolution = service.lookup(
            actor=userid,
            actor_type=actor_type,
            proxy_type=values.get('proxy_type') or values.get('paynow_type'),
            proxy=values.get('proxy') or values.get('paynow') or values.get('nric') or values.get('uen') or values.get('phone'),
            presented_name=values.get('legal_name') or values.get('name') or '',
        )
        return jsonify({'cop': resolution.to_dict()}), 200

    return _handle_errors(_run)


def handle_quote(service: PayNowService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}

    def _run():
        quote = service.quote_fx(values.get('amount'), rail=values.get('rail') or 'paynow')
        return jsonify({'quote': quote}), 200

    return _handle_errors(_run)


def handle_add_beneficiary(service: PayNowService):
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
            proxy_type=values.get('proxy_type') or values.get('paynow_type'),
            proxy=values.get('proxy') or values.get('paynow') or values.get('nric') or values.get('uen') or values.get('phone'),
            bank_code=values.get('bank_code') or values.get('bank'),
            account_number=values.get('account_number') or values.get('external_account'),
        )
        return jsonify({
            'message': 'PayNow beneficiary added',
            'beneficiary': row.to_dict(),
            'PayNow': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _bene_status_route(service: PayNowService, status: str, ok_message: str):
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
            'PayNow': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: PayNowService):
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
            rail=values.get('rail') or 'paynow',
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            waive_fee=bool(values.get('waive_fee')),
            presented_name=values.get('legal_name') or values.get('name') or '',
        )
        return jsonify({'preview': preview, 'PayNow': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: PayNowService):
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
            rail=values.get('rail') or 'paynow',
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
            presented_name=values.get('legal_name') or values.get('name') or '',
            cop_override=bool(values.get('cop_override')),
        )
        return jsonify({
            'message': 'PayNow originated' if created else 'PayNow already posted',
            'payment': payment.to_dict(),
            'PayNow': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: PayNowService):
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
            'message': 'PayNow cancelled',
            'payment': payment.to_dict(),
            'PayNow': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_route(service: PayNowService, action: str):
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
            message = 'PayNow released'
        elif action == 'reject':
            payment = service.reject_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'PayNow rejected'
        elif action == 'complete':
            payment = service.complete_payment(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'PayNow completed'
        elif action == 'recall':
            payment = service.recall_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'PayNow recalled'
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
            message = 'PayNow fee waived'
        else:
            raise PayNowError('invalid_status', 'Unknown PayNow action.')
        return jsonify({
            'message': message,
            'payment': payment.to_dict(),
            'PayNow': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: PayNowService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'PayNow': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_export(service: PayNowService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid

    def _run():
        payload = service.export_fast1(owner, actor=userid, actor_type=actor_type)
        return jsonify({'fast1': payload, 'PayNow': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def attach_paynow_routes(app, service: PayNowService) -> None:
    @app.route('/listPayNow', methods=['POST', 'GET'])
    def list_paynow_route():
        return handle_list(service)

    @app.route('/lookupPayNow', methods=['POST', 'GET'])
    def lookup_paynow_route():
        return handle_lookup(service)

    @app.route('/quotePayNowFx', methods=['POST', 'GET'])
    def quote_paynow_route():
        return handle_quote(service)

    @app.route('/addPayNowBeneficiary', methods=['POST', 'GET'])
    def add_paynow_beneficiary_route():
        return handle_add_beneficiary(service)

    @app.route('/pausePayNowBeneficiary', methods=['POST', 'GET'])
    def pause_paynow_beneficiary_route():
        return _bene_status_route(service, BENE_PAUSED, 'PayNow beneficiary paused')

    @app.route('/resumePayNowBeneficiary', methods=['POST', 'GET'])
    def resume_paynow_beneficiary_route():
        return _bene_status_route(service, BENE_ACTIVE, 'PayNow beneficiary resumed')

    @app.route('/archivePayNowBeneficiary', methods=['POST', 'GET'])
    def archive_paynow_beneficiary_route():
        return _bene_status_route(service, BENE_ARCHIVED, 'PayNow beneficiary archived')

    @app.route('/previewPayNow', methods=['POST', 'GET'])
    def preview_paynow_route():
        return handle_preview(service)

    @app.route('/sendPayNow', methods=['POST', 'GET'])
    def send_paynow_route():
        return handle_send(service)

    @app.route('/cancelPayNow', methods=['POST', 'GET'])
    def cancel_paynow_route():
        return handle_cancel(service)

    @app.route('/releasePayNow', methods=['POST', 'GET'])
    def release_paynow_route():
        return _staff_route(service, 'release')

    @app.route('/rejectPayNow', methods=['POST', 'GET'])
    def reject_paynow_route():
        return _staff_route(service, 'reject')

    @app.route('/completePayNow', methods=['POST', 'GET'])
    def complete_paynow_route():
        return _staff_route(service, 'complete')

    @app.route('/recallPayNow', methods=['POST', 'GET'])
    def recall_paynow_route():
        return _staff_route(service, 'recall')

    @app.route('/overridePayNowOfac', methods=['POST', 'GET'])
    def override_paynow_ofac_route():
        return _staff_route(service, 'override')

    @app.route('/overridePayNowCop', methods=['POST', 'GET'])
    def override_paynow_cop_route():
        return _staff_route(service, 'cop')

    @app.route('/waivePayNowFee', methods=['POST', 'GET'])
    def waive_paynow_fee_route():
        return _staff_route(service, 'waive')

    @app.route('/runDuePayNow', methods=['POST', 'GET'])
    def run_due_paynow_route():
        return handle_run_due(service)

    @app.route('/exportPayNow', methods=['POST', 'GET'])
    def export_paynow_route():
        return handle_export(service)
