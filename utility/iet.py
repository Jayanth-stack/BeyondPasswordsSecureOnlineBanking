"""Outbound Interac e-Transfer origination.

Customers send CAD Interac e-Transfers to email or SMS aliases. Autodeposit
posts 24/7; question-and-answer sits until the recipient claims or the
transfer expires. Independent of inbound Interac receive (PR #111), domestic
Fedwire (PR #73), ACH linking (PR #68), and bill-pay ACH (PR #66). Existing
`/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- CPA 9-digit EFT routing (0 + institution 3 + transit 5) with check
- Canadian NANP email / SMS aliases
- Interac reference (IET + YYYYMMDD + seq)
- CADUSD book (USD debit equivalent)
- Payments Canada holiday clock (origination is 24/7; no cutoff queue)
- IET1 pipe file compose (XML/DOCTYPE rejected)
- SHA-256 security-question fingerprint (never in snapshots)

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Full alias values and security answers never appear in to_dict / snapshots.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    DEFAULT_WATCHLIST,
    AccountError,
    AmountError,
    ScreenResult,
    account_types_from_customer_payload,
    money_str,
    normalize_account,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
CONTACT_ACTIVE = 'active'
CONTACT_PAUSED = 'paused'
CONTACT_ARCHIVED = 'archived'
CONTACT_STATUSES = frozenset({CONTACT_ACTIVE, CONTACT_PAUSED, CONTACT_ARCHIVED})
OPEN_CONTACT = frozenset({CONTACT_ACTIVE, CONTACT_PAUSED})
KIND_EMAIL = 'email'
KIND_SMS = 'sms'
ALIAS_KINDS = frozenset({KIND_EMAIL, KIND_SMS})
KIND_ALIASES = {
    'email': KIND_EMAIL, 'mail': KIND_EMAIL, 'e-mail': KIND_EMAIL, 'autodeposit': KIND_EMAIL,
    'sms': KIND_SMS, 'phone': KIND_SMS, 'mobile': KIND_SMS, 'text': KIND_SMS, 'tel': KIND_SMS,
}
RAIL_AUTO = 'autodeposit'
RAIL_QUESTION = 'question'
RAILS = frozenset({RAIL_AUTO, RAIL_QUESTION})
RAIL_ALIASES = {
    'autodeposit': RAIL_AUTO, 'auto': RAIL_AUTO, 'deposit': RAIL_AUTO, 'ad': RAIL_AUTO,
    'question': RAIL_QUESTION, 'sqa': RAIL_QUESTION, 'security': RAIL_QUESTION,
    'qa': RAIL_QUESTION, 'claim': RAIL_QUESTION,
}
IET_HELD = 'held'
IET_PENDING = 'pending_release'
IET_CLAIM = 'pending_claim'
IET_COMPLETED = 'completed'
IET_REJECTED = 'rejected'
IET_CANCELLED = 'cancelled'
IET_RECALLED = 'recalled'
IET_EXPIRED = 'expired'
IET_NSF = 'nsf'
IET_FAILED = 'failed'
IET_STATUSES = frozenset({
    IET_HELD, IET_PENDING, IET_CLAIM, IET_COMPLETED, IET_REJECTED,
    IET_CANCELLED, IET_RECALLED, IET_EXPIRED, IET_NSF, IET_FAILED,
})
OPEN_IETS = frozenset({IET_HELD, IET_PENDING, IET_CLAIM})
CANCELABLE = frozenset({IET_HELD, IET_PENDING})
FEE_NONE = 'none'
FEE_COLLECTED = 'collected'
FEE_WAIVED = 'waived'
FEE_NSF = 'nsf'
PURPOSES = frozenset({'family', 'goods', 'rent', 'other'})
PURPOSE_ALIASES = {
    'personal': 'family', 'gift': 'family', 'support': 'family',
    'invoice': 'goods', 'purchase': 'goods', 'vendor': 'goods',
    'housing': 'rent', 'mortgage': 'rent',
}
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/iet.sqlite'
DEFAULT_SENDER = '000100016'
DEFAULT_CADUSD = Decimal('0.740000')
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)
CA_INSTITUTIONS = frozenset({
    '001', '002', '003', '004', '006', '010', '016', '039', '219', '809', '815', '828', '839',
})
CA_NPAS = frozenset({
    '204', '226', '236', '249', '250', '257', '263', '289', '306', '343', '354', '365', '367',
    '368', '382', '403', '416', '418', '428', '431', '437', '438', '450', '468', '474', '506',
    '514', '519', '548', '579', '581', '584', '587', '604', '613', '639', '647', '672', '683',
    '705', '709', '742', '753', '778', '780', '782', '807', '819', '825', '867', '873', '879',
    '902', '905',
})
CPA_WEIGHTS = (1, 2, 1, 2, 1, 2, 1, 2, 1)
EMAIL_RE = re.compile(r'^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,24}$')
REF_RE = re.compile(r'^IET\d{12}$')


class IetError(ValueError):
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


def cpa_checksum_ok(digits: str) -> bool:
    if len(digits) != 9 or not digits.isdigit():
        return False
    total = 0
    for digit, weight in zip(digits, CPA_WEIGHTS):
        product = int(digit) * weight
        total += product // 10 + product % 10
    return total % 10 == 0


def cpa_check_digit(stem8: str) -> str:
    if len(stem8) != 8 or not stem8.isdigit():
        raise IetError('invalid_routing', 'CPA routing stem must be 8 digits.')
    for digit in '0123456789':
        candidate = stem8 + digit
        if cpa_checksum_ok(candidate):
            return digit
    raise IetError('invalid_routing', 'CPA routing check digit could not be computed.')


def compose_cpa_routing(institution: Any, transit: Any) -> str:
    inst = re.sub(r'\D', '', str(institution or ''))
    trans = re.sub(r'\D', '', str(transit or ''))
    if len(inst) == 4 and inst.startswith('0'):
        inst = inst[1:]
    if len(inst) != 3 or inst not in CA_INSTITUTIONS:
        raise IetError('invalid_routing', 'Unknown Canadian institution number.')
    if len(trans) == 4:
        trans = trans + cpa_check_digit('0' + inst + trans)
    if len(trans) != 5:
        raise IetError('invalid_routing', 'Transit must be 5 digits.')
    routing = '0' + inst + trans
    if not cpa_checksum_ok(routing):
        raise IetError('invalid_routing', 'CPA routing number failed checksum.')
    return routing


def normalize_cpa_routing(value: Any, *, required: bool = True) -> str:
    digits = re.sub(r'\D', '', str(value or ''))
    if not digits:
        if required:
            raise IetError('invalid_routing', 'CPA routing number required.')
        return ''
    if len(digits) == 8:
        digits = '0' + digits
    if len(digits) != 9 or digits[0] != '0' or digits[1:4] not in CA_INSTITUTIONS:
        raise IetError('invalid_routing', 'CPA routing must be 0 + institution + transit.')
    if not cpa_checksum_ok(digits):
        raise IetError('invalid_routing', 'CPA routing number failed checksum.')
    return digits


def mask_routing(routing: str) -> str:
    digits = re.sub(r'\D', '', str(routing or ''))
    if len(digits) < 4:
        return '****'
    return digits[:4] + '****' + digits[-1:]


def normalize_email(value: Any) -> str:
    text = str(value or '').strip().lower()
    if not EMAIL_RE.match(text) or '..' in text or text.startswith('.') or text.endswith('.'):
        raise IetError('invalid_alias', 'Contact email is not valid.')
    local, domain = text.split('@', 1)
    if not (1 <= len(local) <= 64) or not (3 <= len(domain) <= 80):
        raise IetError('invalid_alias', 'Contact email is not valid.')
    return text


def mask_email(value: str) -> str:
    text = str(value or '')
    if '@' not in text:
        return '***'
    local, domain = text.split('@', 1)
    shown = local[:1] if local else '*'
    return '%s***@%s' % (shown, domain)


def normalize_phone(value: Any) -> str:
    digits = re.sub(r'\D', '', str(value or ''))
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in '01' or digits[3] in '01' or digits[:3] not in CA_NPAS:
        raise IetError('invalid_alias', 'Contact phone must be a Canadian NANP number.')
    return digits


def mask_phone(value: str) -> str:
    digits = re.sub(r'\D', '', str(value or ''))
    if len(digits) < 4:
        return '***-***-****'
    return '***-***-%s' % digits[-4:]


def normalize_kind(value: Any) -> str:
    text = str(value or '').strip().lower().replace('-', '_').replace(' ', '_')
    text = KIND_ALIASES.get(text, text)
    if text not in ALIAS_KINDS:
        raise IetError('invalid_alias', 'Alias must be email or sms.')
    return text


def normalize_alias_value(kind: str, value: Any) -> str:
    if kind == KIND_EMAIL:
        return normalize_email(value)
    return normalize_phone(value)


def mask_alias(kind: str, value: str) -> str:
    if kind == KIND_EMAIL:
        return mask_email(value)
    return mask_phone(value)


def alias_fingerprint(kind: str, value: str) -> str:
    material = ('%s\0%s' % (kind, value)).encode('utf-8')
    return hashlib.sha256(material).hexdigest()


def normalize_rail(value: Any, *, default: str = RAIL_AUTO) -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text not in RAILS:
        raise IetError('invalid_rail', 'Rail must be autodeposit or question.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise IetError('invalid_name', 'Contact legal name must be 2-80 characters.')
    return text


def normalize_nickname(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise IetError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_question(value: Any, *, required: bool = False) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not text:
        if required:
            raise IetError('invalid_question', 'Security question is required.')
        return ''
    if not (4 <= len(text) <= 80) or '|' in text:
        raise IetError('invalid_question', 'Security question must be 4-80 characters.')
    return text


def normalize_answer(value: Any) -> str:
    text = ' '.join(str(value or '').strip().lower().split())
    if not (3 <= len(text) <= 80):
        raise IetError('invalid_answer', 'Security answer must be 3-80 characters.')
    return text


def answer_fingerprint(value: Any) -> str:
    return hashlib.sha256(normalize_answer(value).encode('utf-8')).hexdigest()


def answers_match(digest: str, attempt: Any) -> bool:
    if not digest:
        return False
    try:
        got = answer_fingerprint(attempt)
    except IetError:
        return False
    return hmac.compare_digest(digest, got)


def compose_reference(cycle_date: str, sequence: int) -> str:
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise IetError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 9999:
        raise IetError('invalid_reference', 'Interac sequence out of range.')
    return 'IET%s%04d' % (day, seq)


def normalize_reference(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        if required:
            raise IetError('invalid_reference', 'Interac reference required.')
        return ''
    if not REF_RE.match(text):
        raise IetError('invalid_reference', 'Interac reference must be IET + 12 digits.')
    return text


def mask_reference(value: str) -> str:
    text = str(value or '')
    if len(text) < 8:
        return 'IET********'
    return text[:6] + '*****' + text[-4:]


def normalize_currency(value: Any) -> str:
    text = str(value or 'CAD').strip().upper()
    if text != 'CAD':
        raise IetError('invalid_currency', 'Interac origination must be CAD.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise IetError('invalid_purpose', 'Unknown Interac purpose.')
    return text


class CadUsdBook:
    """CAD → USD convertible amount for the USD ledger."""

    def __init__(self, cadusd: Decimal = DEFAULT_CADUSD) -> None:
        self.cadusd = cadusd.quantize(Decimal('0.000001'), rounding=ROUND_HALF_EVEN)

    def quote(self, amount_cad: Decimal) -> Dict[str, str]:
        usd = (amount_cad * self.cadusd).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
        return {
            'currency': 'CAD',
            'amount_cad': money_str(amount_cad),
            'rate': str(self.cadusd),
            'amount_usd': money_str(usd),
        }


def easter_gregorian(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month, day = divmod(h + el - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return date(year, month, 1 + offset + (n - 1) * 7)


def _monday_before(year: int, month: int, day: int) -> date:
    cursor = date(year, month, day)
    while cursor.weekday() != 0:
        cursor -= timedelta(days=1)
    return cursor


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def payments_canada_holidays(year: int) -> set:
    """ACSS / LVTS weekday-observed holidays (Ontario-style + federal)."""
    easter = easter_gregorian(year)
    days = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 2, 0, 3),
        easter - timedelta(days=2),
        _monday_before(year, 5, 24),
        _observed(date(year, 7, 1)),
        _nth_weekday(year, 8, 0, 1),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 10, 0, 2),
        _observed(date(year, 11, 11)),
        _observed(date(year, 12, 25)),
        _observed(date(year, 12, 26)),
    }
    return days


class InteracClock:
    """Payments Canada calendar. Origination posts 24/7; snapshot still reports holidays."""

    def __init__(
        self,
        *,
        tz_offset_hours: int = -4,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.tz_offset_hours = int(tz_offset_hours)
        self.tz = timezone(timedelta(hours=self.tz_offset_hours))
        extra = set()
        for item in extra_holidays:
            text = str(item).strip()
            if not text:
                continue
            extra.add(date.fromisoformat(text[:10]) if '-' in text else datetime.strptime(text, '%Y%m%d').date())
        self.extra_holidays = extra

    def local_dt(self, ts: float) -> datetime:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(self.tz)

    def is_weekend(self, day: date) -> bool:
        return day.weekday() >= 5

    def is_holiday(self, day: date) -> bool:
        return day in payments_canada_holidays(day.year) or day in self.extra_holidays

    def is_business_day(self, day: date) -> bool:
        return not self.is_weekend(day) and not self.is_holiday(day)

    def cycle_date(self, ts: float) -> str:
        return self.local_dt(ts).strftime('%Y%m%d')

    def snapshot(self, ts: float) -> Dict[str, Any]:
        local = self.local_dt(ts)
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'cutoff': '24/7',
            'after_cutoff': False,
            'business_day': self.is_business_day(local.date()),
            'value_date': local.date().isoformat(),
            'cycle_date': local.strftime('%Y%m%d'),
        }


def _reject_xml(text: str) -> None:
    low = text.lower()
    if '<?xml' in low or '<!doctype' in low or '<!entity' in low or '<iet' in low:
        raise IetError('invalid_file', 'XML/DOCTYPE Interac files are rejected.')


def compose_iet(
    *,
    reference: str,
    amount: Decimal,
    sender_name: str,
    sender_fi: str,
    kind: str,
    alias: str,
    rail: str,
    question: str = '',
    expiry: str = '',
    receiver: str = DEFAULT_SENDER,
) -> str:
    fields = [
        'IET1',
        normalize_reference(reference),
        money_str(amount),
        'CAD',
        normalize_legal_name(sender_name).replace('|', ' '),
        normalize_cpa_routing(sender_fi),
        kind,
        alias,
        rail,
        (question or '').replace('|', ' '),
        str(expiry or ''),
        normalize_cpa_routing(receiver),
    ]
    return '|'.join(fields)


def parse_iet(record: str) -> Dict[str, Any]:
    text = str(record or '').strip()
    _reject_xml(text)
    if not text or text.startswith('#'):
        raise IetError('invalid_file', 'Empty Interac record.')
    parts = text.split('|')
    if len(parts) != 12 or parts[0] != 'IET1':
        raise IetError('invalid_file', 'IET1 records must have 12 pipe fields.')
    kind = normalize_kind(parts[6])
    rail = normalize_rail(parts[8])
    return {
        'reference': normalize_reference(parts[1]),
        'amount': parse_money(parts[2]),
        'currency': normalize_currency(parts[3]),
        'sender_name': normalize_legal_name(parts[4]),
        'sender_fi': normalize_cpa_routing(parts[5]),
        'alias_type': kind,
        'alias': normalize_alias_value(kind, parts[7]),
        'rail': rail,
        'question': normalize_question(parts[9], required=(rail == RAIL_QUESTION)),
        'expiry': str(parts[10] or '').strip(),
        'receiver_routing': normalize_cpa_routing(parts[11]),
    }


def split_iet_file(payload: str) -> List[str]:
    text = str(payload or '')
    _reject_xml(text)
    records = []
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        records.append(stripped)
    if not records:
        raise IetError('invalid_file', 'Interac file had no IET1 records.')
    return records


def compute_fee(amount: Decimal, rail: str, auto_fee: Decimal, sqa_fee: Decimal, *, waived: bool = False) -> Decimal:
    """Flat per-rail outbound fee; waived transfers cost $0."""
    if waived:
        return Decimal('0.00')
    _ = amount
    fee = auto_fee if rail == RAIL_AUTO else sqa_fee
    return fee.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


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
class IetPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    allow_credit: bool = False
    max_contacts: int = 12
    max_transfers: int = 120
    min_amount: Decimal = Decimal('0.01')
    autodeposit_cap: Decimal = Decimal('10000.00')
    question_cap: Decimal = Decimal('25000.00')
    autodeposit_fee: Decimal = Decimal('1.50')
    question_fee: Decimal = Decimal('1.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    expiry_days: int = 30
    return_hours: int = 24
    tz_offset_hours: int = -4
    sender_routing: str = DEFAULT_SENDER
    cadusd: Decimal = DEFAULT_CADUSD
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'IetPolicy':
        extra = _env_list('IET_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        routing = os.environ.get('IET_SENDER') or DEFAULT_SENDER
        rate = os.environ.get('IET_CADUSD') or str(DEFAULT_CADUSD)
        return cls(
            enabled=_env_bool('IET_ENABLED', True),
            customer_manage=_env_bool('IET_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('IET_CUSTOMER_SEND', True),
            allow_credit=_env_bool('IET_ALLOW_CREDIT', False),
            max_contacts=max(1, _env_int('IET_MAX_CONTACTS', 12)),
            max_transfers=max(1, _env_int('IET_MAX_TRANSFERS', 120)),
            min_amount=_env_money('IET_MIN_AMOUNT', '0.01'),
            autodeposit_cap=_env_money('IET_AUTO_CAP', '10000.00'),
            question_cap=_env_money('IET_SQA_CAP', '25000.00'),
            autodeposit_fee=_env_money('IET_AUTO_FEE', '1.50'),
            question_fee=_env_money('IET_SQA_FEE', '1.00'),
            dual_control_threshold=_env_money('IET_DUAL_CONTROL', '10000.00'),
            expiry_days=max(1, _env_int('IET_EXPIRY_DAYS', 30)),
            return_hours=max(1, _env_int('IET_RETURN_HOURS', 24)),
            tz_offset_hours=_env_int('IET_TZ_OFFSET', -4),
            sender_routing=normalize_cpa_routing(routing),
            cadusd=Decimal(str(rate)),
            watchlist=watch,
            extra_holidays=_env_list('IET_HOLIDAYS'),
        )


@dataclass
class IetContact:
    contact_id: str
    userid: str
    nickname: str
    legal_name: str
    kind: str
    alias_value: str
    fingerprint: str
    rail: str
    question: str
    answer_digest: str
    default_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        last4 = self.default_account[-4:] if len(self.default_account) >= 4 else self.default_account
        return {
            'contact_id': self.contact_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'kind': self.kind,
            'alias_masked': mask_alias(self.kind, self.alias_value),
            'rail': self.rail,
            'question': self.question,
            'account_last4': last4,
            'default_account': self.default_account,
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == CONTACT_ACTIVE,
            'paused': self.status == CONTACT_PAUSED,
            'archived': self.status == CONTACT_ARCHIVED,
        }


@dataclass
class IetTransfer:
    transfer_id: str
    trace_id: str
    contact_id: str
    userid: str
    internal_account: str
    amount_cad: str
    debit_usd: str
    fx_rate: str
    fee: str
    fee_status: str
    nickname: str
    legal_name: str
    alias_kind: str
    alias_masked: str
    rail: str
    question: str
    purpose: str
    memo: str
    status: str
    reference: str
    sender_fi: str
    value_date: str
    expiry_date: str
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
            'contact_id': self.contact_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'amount_cad': self.amount_cad,
            'debit_usd': self.debit_usd,
            'fx_rate': self.fx_rate,
            'fee': self.fee,
            'fee_status': self.fee_status,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'kind': self.alias_kind,
            'alias_masked': self.alias_masked,
            'rail': self.rail,
            'question': self.question,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'reference_masked': mask_reference(self.reference) if self.reference else '',
            'sender_fi_masked': mask_routing(self.sender_fi),
            'value_date': self.value_date,
            'expiry_date': self.expiry_date,
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
            'held': self.status == IET_HELD,
            'pending_release': self.status == IET_PENDING,
            'pending_claim': self.status == IET_CLAIM,
            'completed': self.status == IET_COMPLETED,
            'cancelable': self.status in CANCELABLE or self.status == IET_CLAIM,
        }


def _clone_contact(row: IetContact) -> IetContact:
    return IetContact(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_transfer(row: IetTransfer) -> IetTransfer:
    return IetTransfer(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _contact_from_row(row: Any) -> IetContact:
    return IetContact(
        contact_id=row['contact_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        kind=row['kind'],
        alias_value=row['alias_value'],
        fingerprint=row['fingerprint'],
        rail=row['rail'],
        question=row['question'] or '',
        answer_digest=row['answer_digest'] or '',
        default_account=row['default_account'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
    )


def _transfer_from_row(row: Any) -> IetTransfer:
    return IetTransfer(
        transfer_id=row['transfer_id'],
        trace_id=row['trace_id'],
        contact_id=row['contact_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        amount_cad=row['amount_cad'],
        debit_usd=row['debit_usd'],
        fx_rate=row['fx_rate'],
        fee=row['fee'],
        fee_status=row['fee_status'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        alias_kind=row['alias_kind'],
        alias_masked=row['alias_masked'],
        rail=row['rail'],
        question=row['question'] or '',
        purpose=row['purpose'],
        memo=row['memo'] or '',
        status=row['status'],
        reference=row['reference'] or '',
        sender_fi=row['sender_fi'],
        value_date=row['value_date'],
        expiry_date=row['expiry_date'] or '',
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


class MemoryIetStore:
    def __init__(self) -> None:
        self._contacts: Dict[str, IetContact] = {}
        self._transfers: Dict[str, IetTransfer] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_contact(self, row: IetContact) -> None:
        with self._lock:
            self._contacts[row.contact_id] = row

    def get_contact(self, contact_id: str) -> Optional[IetContact]:
        with self._lock:
            row = self._contacts.get(contact_id)
            return _clone_contact(row) if row else None

    def update_contact(self, row: IetContact) -> None:
        with self._lock:
            self._contacts[row.contact_id] = row

    def list_contacts(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[IetContact]:
        with self._lock:
            rows = [_clone_contact(row) for row in self._contacts.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != CONTACT_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_contact_by_nickname(self, userid: str, nickname: str) -> Optional[IetContact]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._contacts.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_CONTACT:
                    return _clone_contact(row)
        return None

    def find_contact_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[IetContact]:
        with self._lock:
            for row in self._contacts.values():
                if row.userid == userid and row.fingerprint == fingerprint and row.status in OPEN_CONTACT:
                    return _clone_contact(row)
        return None

    def put_transfer(self, row: IetTransfer) -> IetTransfer:
        with self._lock:
            existing_id = self._by_trace.get(row.trace_id)
            if existing_id is not None:
                return self._transfers[existing_id]
            self._transfers[row.transfer_id] = row
            self._by_trace[row.trace_id] = row.transfer_id
            return row

    def update_transfer(self, row: IetTransfer) -> None:
        with self._lock:
            self._transfers[row.transfer_id] = row

    def get_transfer(self, transfer_id: str) -> Optional[IetTransfer]:
        with self._lock:
            row = self._transfers.get(transfer_id)
            return _clone_transfer(row) if row else None

    def get_transfer_by_trace(self, trace_id: str) -> Optional[IetTransfer]:
        with self._lock:
            transfer_id = self._by_trace.get(trace_id)
            return _clone_transfer(self._transfers[transfer_id]) if transfer_id else None

    def list_transfers(self, userid: Optional[str] = None, contact_id: Optional[str] = None) -> List[IetTransfer]:
        with self._lock:
            rows = [_clone_transfer(row) for row in self._transfers.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if contact_id is not None:
            rows = [row for row in rows if row.contact_id == contact_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def next_reference_sequence(self, cycle_date: str) -> int:
        prefix = 'IET' + cycle_date
        with self._lock:
            used = [row.reference for row in self._transfers.values() if row.reference.startswith(prefix)]
        return len(used) + 1


class SqliteIetStore:
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
                CREATE TABLE IF NOT EXISTS contacts (
                    contact_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    alias_value TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    rail TEXT NOT NULL,
                    question TEXT NOT NULL DEFAULT '',
                    answer_digest TEXT NOT NULL DEFAULT '',
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
                    contact_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    internal_account TEXT NOT NULL,
                    amount_cad TEXT NOT NULL,
                    debit_usd TEXT NOT NULL,
                    fx_rate TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    fee_status TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    alias_kind TEXT NOT NULL,
                    alias_masked TEXT NOT NULL,
                    rail TEXT NOT NULL,
                    question TEXT NOT NULL DEFAULT '',
                    purpose TEXT NOT NULL,
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    reference TEXT NOT NULL DEFAULT '',
                    sender_fi TEXT NOT NULL,
                    value_date TEXT NOT NULL,
                    expiry_date TEXT NOT NULL DEFAULT '',
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

    def put_contact(self, row: IetContact) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO contacts (
                    contact_id, userid, nickname, legal_name, kind, alias_value,
                    fingerprint, rail, question, answer_digest, default_account,
                    status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.contact_id, row.userid, row.nickname, row.legal_name, row.kind,
                    row.alias_value, row.fingerprint, row.rail, row.question,
                    row.answer_digest, row.default_account, row.status, row.actor,
                    row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_contact(self, contact_id: str) -> Optional[IetContact]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM contacts WHERE contact_id = ?', (contact_id,),
            ).fetchone()
        return _contact_from_row(row) if row else None

    def update_contact(self, row: IetContact) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE contacts SET nickname=?, legal_name=?, kind=?, alias_value=?,
                    fingerprint=?, rail=?, question=?, answer_digest=?, default_account=?,
                    status=?, actor=?, updated_at=?
                WHERE contact_id=?
                """,
                (
                    row.nickname, row.legal_name, row.kind, row.alias_value, row.fingerprint,
                    row.rail, row.question, row.answer_digest, row.default_account,
                    row.status, row.actor, row.updated_at, row.contact_id,
                ),
            )
            conn.commit()

    def list_contacts(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[IetContact]:
        sql = 'SELECT * FROM contacts'
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
        return [_contact_from_row(row) for row in rows]

    def find_contact_by_nickname(self, userid: str, nickname: str) -> Optional[IetContact]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM contacts
                WHERE userid = ? AND lower(nickname) = lower(?) AND status IN ('active', 'paused')
                """,
                (userid, nickname),
            ).fetchone()
        return _contact_from_row(row) if row else None

    def find_contact_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[IetContact]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM contacts
                WHERE userid = ? AND fingerprint = ? AND status IN ('active', 'paused')
                """,
                (userid, fingerprint),
            ).fetchone()
        return _contact_from_row(row) if row else None

    def put_transfer(self, row: IetTransfer) -> IetTransfer:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM transfers WHERE trace_id = ?', (row.trace_id,),
            ).fetchone()
            if existing is not None:
                return _transfer_from_row(existing)
            conn.execute(
                """
                INSERT INTO transfers (
                    transfer_id, trace_id, contact_id, userid, internal_account,
                    amount_cad, debit_usd, fx_rate, fee, fee_status, nickname,
                    legal_name, alias_kind, alias_masked, rail, question, purpose,
                    memo, status, reference, sender_fi, value_date, expiry_date,
                    actor, releaser, ofac_hit, ofac_match, created_at, updated_at,
                    sent_at, completed_at, recalled_at, note, reason
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                self._transfer_values(row),
            )
            conn.commit()
            return row

    def update_transfer(self, row: IetTransfer) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE transfers SET contact_id=?, userid=?, internal_account=?,
                    amount_cad=?, debit_usd=?, fx_rate=?, fee=?, fee_status=?,
                    nickname=?, legal_name=?, alias_kind=?, alias_masked=?, rail=?,
                    question=?, purpose=?, memo=?, status=?, reference=?, sender_fi=?,
                    value_date=?, expiry_date=?, actor=?, releaser=?, ofac_hit=?,
                    ofac_match=?, updated_at=?, sent_at=?, completed_at=?,
                    recalled_at=?, note=?, reason=?
                WHERE transfer_id=?
                """,
                (
                    row.contact_id, row.userid, row.internal_account, row.amount_cad,
                    row.debit_usd, row.fx_rate, row.fee, row.fee_status, row.nickname,
                    row.legal_name, row.alias_kind, row.alias_masked, row.rail,
                    row.question, row.purpose, row.memo, row.status, row.reference,
                    row.sender_fi, row.value_date, row.expiry_date, row.actor,
                    row.releaser, row.ofac_hit, row.ofac_match, row.updated_at,
                    row.sent_at, row.completed_at, row.recalled_at, row.note,
                    row.reason, row.transfer_id,
                ),
            )
            conn.commit()

    def get_transfer(self, transfer_id: str) -> Optional[IetTransfer]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM transfers WHERE transfer_id = ?', (transfer_id,),
            ).fetchone()
        return _transfer_from_row(row) if row else None

    def get_transfer_by_trace(self, trace_id: str) -> Optional[IetTransfer]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM transfers WHERE trace_id = ?', (trace_id,),
            ).fetchone()
        return _transfer_from_row(row) if row else None

    def list_transfers(self, userid: Optional[str] = None, contact_id: Optional[str] = None) -> List[IetTransfer]:
        sql = 'SELECT * FROM transfers'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if contact_id is not None:
            clauses.append('contact_id = ?')
            params.append(contact_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_transfer_from_row(row) for row in rows]

    def next_reference_sequence(self, cycle_date: str) -> int:
        prefix = 'IET' + cycle_date + '%'
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT COUNT(*) AS n FROM transfers WHERE reference LIKE ?', (prefix,),
            ).fetchone()
        return int(row['n'] if row else 0) + 1

    @staticmethod
    def _transfer_values(row: IetTransfer) -> Tuple[Any, ...]:
        return (
            row.transfer_id, row.trace_id, row.contact_id, row.userid, row.internal_account,
            row.amount_cad, row.debit_usd, row.fx_rate, row.fee, row.fee_status,
            row.nickname, row.legal_name, row.alias_kind, row.alias_masked, row.rail,
            row.question, row.purpose, row.memo, row.status, row.reference, row.sender_fi,
            row.value_date, row.expiry_date, row.actor, row.releaser, row.ofac_hit,
            row.ofac_match, row.created_at, row.updated_at, row.sent_at, row.completed_at,
            row.recalled_at, row.note, row.reason,
        )


class IetService:
    def __init__(
        self,
        policy: IetPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[InteracClock] = None,
        fx: Optional[CadUsdBook] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: datetime.now(tz=timezone.utc).timestamp())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or InteracClock(
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )
        self.fx = fx or CadUsdBook(policy.cadusd)

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise IetError('iet_disabled', 'Interac origination is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise IetError('iet_forbidden', 'Customers cannot manage Interac contacts.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise IetError('iet_forbidden', 'Customers cannot send Interac transfers.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise IetError('iet_forbidden', 'Staff only.')

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
            raise IetError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise IetError('credit_not_allowed', 'Credit accounts cannot originate Interac transfers.')

    def _assert_amount(self, cad: Decimal, rail: str) -> None:
        cap = self.policy.autodeposit_cap if rail == RAIL_AUTO else self.policy.question_cap
        if cad < self.policy.min_amount:
            raise IetError('amount_out_of_range', 'Amount is outside the allowed range.')
        if cad > cap:
            code = 'autodeposit_amount_exceeded' if rail == RAIL_AUTO else 'question_amount_exceeded'
            raise IetError(code, 'Amount exceeds the %s cap.' % rail)

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, usd: Decimal) -> bool:
        return usd >= self.policy.dual_control_threshold

    def quote_fx(self, amount: Any) -> Dict[str, str]:
        cad = parse_money(amount)
        normalize_currency('CAD')
        return self.fx.quote(cad)

    def add_contact(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        kind: Any,
        alias: Any,
        rail: Any = RAIL_AUTO,
        question: Any = '',
        answer: Any = '',
        default_account: Any,
    ) -> IetContact:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise IetError('iet_forbidden', 'Not allowed to add contacts for this customer.')
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        alias_kind = normalize_kind(kind)
        alias_value = normalize_alias_value(alias_kind, alias)
        contact_rail = normalize_rail(rail)
        question_text = normalize_question(question, required=(contact_rail == RAIL_QUESTION))
        digest = ''
        if contact_rail == RAIL_QUESTION:
            digest = answer_fingerprint(answer)
        elif answer:
            raise IetError('invalid_answer', 'Autodeposit contacts do not take a security answer.')
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        if self.store.find_contact_by_nickname(owner_userid, name) is not None:
            raise IetError('contact_duplicate', 'A contact with that nickname already exists.')
        fingerprint = alias_fingerprint(alias_kind, alias_value)
        if self.store.find_contact_by_fingerprint(owner_userid, fingerprint) is not None:
            raise IetError('contact_duplicate', 'That Interac alias is already on file.')
        open_rows = [row for row in self.store.list_contacts(owner_userid) if row.status in OPEN_CONTACT]
        if len(open_rows) >= self.policy.max_contacts:
            raise IetError('contact_limit', 'Interac contact limit reached.')
        now = float(self.clock())
        row = IetContact(
            contact_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            kind=alias_kind,
            alias_value=alias_value,
            fingerprint=fingerprint,
            rail=contact_rail,
            question=question_text,
            answer_digest=digest,
            default_account=account,
            status=CONTACT_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_contact(row)
        return row

    def get_contact(self, *, contact_id: str, actor: str, actor_type: str) -> IetContact:
        self._require_enabled()
        row = self.store.get_contact(contact_id)
        if row is None:
            raise IetError('contact_not_found', 'Interac contact not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise IetError('iet_forbidden', 'Not allowed to view this contact.')
        return row

    def enforce_contact(
        self,
        *,
        contact_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> IetContact:
        row = self.get_contact(contact_id=contact_id, actor=actor, actor_type=actor_type)
        if row.status == CONTACT_ARCHIVED:
            raise IetError('already_archived', 'Contact is archived.')
        if row.status == CONTACT_PAUSED:
            raise IetError('contact_paused', 'Contact is paused.')
        if require_active and row.status != CONTACT_ACTIVE:
            raise IetError('invalid_status', 'Contact is not active.')
        return row

    def set_contact_status(
        self,
        *,
        contact_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> IetContact:
        self._require_manage(actor_type)
        row = self.get_contact(contact_id=contact_id, actor=actor, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': CONTACT_PAUSED, 'hold': CONTACT_PAUSED,
            'resume': CONTACT_ACTIVE, 'activate': CONTACT_ACTIVE, 'unpause': CONTACT_ACTIVE,
            'archive': CONTACT_ARCHIVED, 'close': CONTACT_ARCHIVED, 'cancel': CONTACT_ARCHIVED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {CONTACT_PAUSED, CONTACT_ACTIVE, CONTACT_ARCHIVED}:
            raise IetError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == CONTACT_ARCHIVED:
            raise IetError('already_archived', 'Contact is already archived.')
        if wanted == CONTACT_PAUSED:
            if row.status == CONTACT_PAUSED:
                raise IetError('already_paused', 'Contact is already paused.')
            if row.status != CONTACT_ACTIVE:
                raise IetError('invalid_status', 'Only an active contact can be paused.')
        elif wanted == CONTACT_ACTIVE:
            if row.status == CONTACT_ACTIVE:
                raise IetError('already_active', 'Contact is already active.')
            if row.status != CONTACT_PAUSED:
                raise IetError('invalid_status', 'Only a paused contact can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_contact(row)
        return row

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        contact_id: Any,
        amount: Any,
        internal_account: Any = None,
        waive_fee: bool = False,
    ) -> Dict[str, Any]:
        self._require_send(actor_type)
        contact = self.enforce_contact(
            contact_id=str(contact_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if contact.userid != owner_userid:
            raise IetError('iet_forbidden', 'Contact does not belong to this customer.')
        cad = parse_money(amount)
        self._assert_amount(cad, contact.rail)
        account = normalize_account(internal_account or contact.default_account)
        self._assert_internal_account(owner_userid, account)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(cad, contact.rail, self.policy.autodeposit_fee, self.policy.question_fee, waived=staff_waive)
        quote = self.fx.quote(cad)
        usd = parse_money(quote['amount_usd'])
        now = float(self.clock())
        ofac = self._screen(contact.legal_name, aliases=(contact.nickname,))
        return {
            'amount_cad': money_str(cad),
            'debit_usd': money_str(usd),
            'fx': quote,
            'fee': money_str(fee),
            'total_usd': money_str(usd + fee),
            'rail': contact.rail,
            'internal_account': account,
            'contact': contact.to_dict(),
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(usd),
            'clock': self.calendar.snapshot(now),
        }

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        contact: IetContact,
        account: str,
        cad: Decimal,
        usd: Decimal,
        fee: Decimal,
        purpose: str,
        memo: str,
        trace_id: str,
        ofac: ScreenResult,
        waive_fee: bool,
        quote: Dict[str, str],
    ) -> IetTransfer:
        now = float(self.clock())
        value = self.calendar.cycle_date(now)
        expiry = ''
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        if ofac.hit:
            status = IET_HELD
        elif self._needs_dual_control(usd):
            status = IET_PENDING
        elif contact.rail == RAIL_QUESTION:
            status = IET_CLAIM
        else:
            status = IET_COMPLETED
        if contact.rail == RAIL_QUESTION:
            expiry_day = (self.calendar.local_dt(now).date() + timedelta(days=self.policy.expiry_days)).isoformat()
            expiry = expiry_day
        transfer = IetTransfer(
            transfer_id=uuid.uuid4().hex,
            trace_id=trace_id,
            contact_id=contact.contact_id,
            userid=owner_userid,
            internal_account=account,
            amount_cad=money_str(cad),
            debit_usd=money_str(usd),
            fx_rate=quote['rate'],
            fee=money_str(fee),
            fee_status=fee_status,
            nickname=contact.nickname,
            legal_name=contact.legal_name,
            alias_kind=contact.kind,
            alias_masked=mask_alias(contact.kind, contact.alias_value),
            rail=contact.rail,
            question=contact.question,
            purpose=purpose,
            memo=memo,
            status=status if status in {IET_HELD, IET_PENDING} else IET_CLAIM,
            reference='',
            sender_fi=self.policy.sender_routing,
            value_date=value,
            expiry_date=expiry,
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
        )
        if status in {IET_CLAIM, IET_COMPLETED}:
            transfer.status = status
            self._transmit(transfer, contact=contact, actor=actor)
        return transfer

    def _transmit(self, transfer: IetTransfer, *, contact: IetContact, actor: str) -> IetTransfer:
        now = float(self.clock())
        cycle = transfer.value_date or self.calendar.cycle_date(now)
        seq = self.store.next_reference_sequence(cycle)
        transfer.reference = compose_reference(cycle, seq)
        usd = parse_money(transfer.debit_usd)
        fee = parse_money(transfer.fee, allow_zero=True)
        remark = 'interac to %s' % transfer.nickname
        landing = IET_COMPLETED if transfer.rail == RAIL_AUTO else IET_CLAIM
        status = landing
        fail_note = ''
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(transfer.internal_account, money_str(usd), remark)
            except Exception as exc:
                status = IET_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = IET_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = IET_FAILED
                    fail_note = str(result)[:240]
        if status == landing and fee > 0 and transfer.fee_status != FEE_WAIVED and self.debit_fn is not None:
            try:
                fee_result = self.debit_fn(
                    transfer.internal_account, money_str(fee), 'interac fee %s' % transfer.reference[-12:],
                )
            except Exception:
                transfer.fee_status = FEE_NSF
            else:
                kind = _classify_money_result(fee_result)
                transfer.fee_status = FEE_COLLECTED if kind == 'ok' else FEE_NSF
        elif status == landing and (fee == 0 or transfer.fee_status == FEE_WAIVED):
            transfer.fee_status = FEE_WAIVED if transfer.fee_status == FEE_WAIVED or fee == 0 else transfer.fee_status
        transfer.status = status
        transfer.updated_at = now
        if status == landing:
            transfer.sent_at = now
            transfer.releaser = str(actor)
            if status == IET_COMPLETED:
                transfer.completed_at = now
            _ = compose_iet(
                reference=transfer.reference,
                amount=parse_money(transfer.amount_cad),
                sender_name=transfer.legal_name,
                sender_fi=transfer.sender_fi,
                kind=contact.kind,
                alias=contact.alias_value,
                rail=transfer.rail,
                question=transfer.question,
                expiry=transfer.expiry_date.replace('-', '') if transfer.expiry_date else '',
                receiver=self.policy.sender_routing,
            )
        else:
            transfer.reference = ''
            transfer.note = fail_note
        return transfer

    def originate(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        contact_id: Any,
        amount: Any,
        internal_account: Any = None,
        purpose: Any = 'other',
        memo: Any = '',
        trace_id: Any = None,
        waive_fee: bool = False,
    ) -> Tuple[IetTransfer, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise IetError('iet_forbidden', 'Not allowed to originate Interac for this customer.')
        contact = self.enforce_contact(
            contact_id=str(contact_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if contact.userid != owner_userid:
            raise IetError('iet_forbidden', 'Contact does not belong to this customer.')
        cad = parse_money(amount)
        self._assert_amount(cad, contact.rail)
        account = normalize_account(internal_account or contact.default_account)
        self._assert_internal_account(owner_userid, account)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(cad, contact.rail, self.policy.autodeposit_fee, self.policy.question_fee, waived=staff_waive)
        quote = self.fx.quote(cad)
        usd = parse_money(quote['amount_usd'])
        trace = normalize_id(trace_id)
        existing = self.store.get_transfer_by_trace(trace)
        if existing is not None:
            return existing, False
        if len(self.store.list_transfers(owner_userid)) >= self.policy.max_transfers:
            raise IetError('transfer_limit', 'Interac history limit reached.')
        ofac = self._screen(contact.legal_name, aliases=(contact.nickname,))
        transfer = self._place(
            owner_userid=owner_userid,
            actor=actor,
            contact=contact,
            account=account,
            cad=cad,
            usd=usd,
            fee=fee,
            purpose=normalize_purpose(purpose),
            memo=normalize_note(memo, limit=140),
            trace_id=trace,
            ofac=ofac,
            waive_fee=staff_waive,
            quote=quote,
        )
        stored = self.store.put_transfer(transfer)
        if stored.transfer_id != transfer.transfer_id:
            return stored, False
        if stored.status == IET_NSF:
            raise IetError('nsf', 'Insufficient funds for Interac.', transfer=stored)
        if stored.status == IET_FAILED:
            raise IetError('failed', 'Interac debit did not complete.', transfer=stored)
        return stored, True

    def get_transfer(self, *, transfer_id: str, actor: str, actor_type: str) -> IetTransfer:
        self._require_enabled()
        row = self.store.get_transfer(transfer_id)
        if row is None:
            raise IetError('transfer_not_found', 'Interac transfer not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise IetError('iet_forbidden', 'Not allowed to view this transfer.')
        return row

    def _credit_back(self, transfer: IetTransfer, *, reason: str) -> None:
        usd = parse_money(transfer.debit_usd)
        fee = parse_money(transfer.fee, allow_zero=True)
        ref = (transfer.reference or transfer.transfer_id)[-12:]
        if self.credit_fn is None:
            return
        result = self.credit_fn(transfer.internal_account, money_str(usd), 'interac return %s' % ref)
        kind = _classify_money_result(result)
        if kind == 'nsf':
            raise IetError('nsf', 'Could not credit Interac return.', transfer=transfer)
        if kind != 'ok':
            raise IetError('recall_failed', 'Interac return credit failed.', transfer=transfer)
        if transfer.fee_status == FEE_COLLECTED and fee > 0:
            fee_result = self.credit_fn(transfer.internal_account, money_str(fee), 'interac fee return %s' % ref)
            if _classify_money_result(fee_result) != 'ok':
                raise IetError('recall_failed', 'Interac fee return failed.', transfer=transfer)
        transfer.reason = reason

    def cancel_transfer(
        self,
        *,
        transfer_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> IetTransfer:
        self._require_send(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and transfer.userid != actor:
            raise IetError('iet_forbidden', 'Not allowed to cancel this transfer.')
        if transfer.status == IET_CLAIM:
            self._credit_back(transfer, reason='unclaimed')
            transfer.status = IET_CANCELLED
            transfer.recalled_at = float(self.clock())
        elif transfer.status in CANCELABLE:
            transfer.status = IET_CANCELLED
        elif transfer.status == IET_COMPLETED and transfer.rail == RAIL_AUTO:
            raise IetError('scheme_irrevocable', 'Autodeposit Interac cannot be cancelled after send.')
        else:
            raise IetError('not_cancelable', 'Only held, pending, or unclaimed Interac can be cancelled.')
        transfer.actor = str(actor)
        transfer.updated_at = float(self.clock())
        transfer.note = normalize_note(note)
        self.store.update_transfer(transfer)
        return transfer

    def override_ofac(
        self,
        *,
        transfer_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> IetTransfer:
        self._require_staff(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if transfer.status != IET_HELD:
            raise IetError('not_overridable', 'Only held Interac can be OFAC-overridden.')
        contact = self.store.get_contact(transfer.contact_id)
        if contact is None:
            raise IetError('contact_not_found', 'Interac contact not found.')
        transfer.ofac_hit = 0
        transfer.note = normalize_note(note) or 'cleared'
        transfer.updated_at = float(self.clock())
        usd = parse_money(transfer.debit_usd)
        if self._needs_dual_control(usd):
            transfer.status = IET_PENDING
            self.store.update_transfer(transfer)
            return transfer
        landing = IET_COMPLETED if transfer.rail == RAIL_AUTO else IET_CLAIM
        transfer.status = landing
        self._transmit(transfer, contact=contact, actor=actor)
        self.store.update_transfer(transfer)
        if transfer.status == IET_NSF:
            raise IetError('nsf', 'Insufficient funds for Interac.', transfer=transfer)
        if transfer.status == IET_FAILED:
            raise IetError('failed', 'Interac debit did not complete.', transfer=transfer)
        return transfer

    def release_transfer(
        self,
        *,
        transfer_id: str,
        actor: str,
        actor_type: str,
    ) -> IetTransfer:
        self._require_staff(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if transfer.status == IET_HELD:
            raise IetError('ofac_hold', 'OFAC hold must be overridden before release.')
        if transfer.status != IET_PENDING:
            raise IetError('not_releasable', 'Only pending Interac can be released.')
        if transfer.actor == str(actor):
            raise IetError('same_approver', 'A different employee must release this Interac.')
        contact = self.store.get_contact(transfer.contact_id)
        if contact is None:
            raise IetError('contact_not_found', 'Interac contact not found.')
        landing = IET_COMPLETED if transfer.rail == RAIL_AUTO else IET_CLAIM
        transfer.status = landing
        self._transmit(transfer, contact=contact, actor=actor)
        self.store.update_transfer(transfer)
        if transfer.status == IET_NSF:
            raise IetError('nsf', 'Insufficient funds for Interac.', transfer=transfer)
        if transfer.status == IET_FAILED:
            raise IetError('failed', 'Interac debit did not complete.', transfer=transfer)
        return transfer

    def reject_transfer(
        self,
        *,
        transfer_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> IetTransfer:
        self._require_staff(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if transfer.status not in {IET_HELD, IET_PENDING, IET_CLAIM}:
            raise IetError('not_rejectable', 'Only open Interac can be rejected.')
        if transfer.status == IET_CLAIM:
            self._credit_back(transfer, reason='rejected')
            transfer.recalled_at = float(self.clock())
        transfer.status = IET_REJECTED
        transfer.actor = str(actor)
        transfer.updated_at = float(self.clock())
        transfer.note = normalize_note(note)
        self.store.update_transfer(transfer)
        return transfer

    def complete_transfer(
        self,
        *,
        transfer_id: str,
        actor: str,
        actor_type: str,
    ) -> IetTransfer:
        self._require_staff(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if transfer.status == IET_COMPLETED:
            raise IetError('already_completed', 'Interac is already completed.')
        if transfer.status != IET_CLAIM:
            raise IetError('not_completable', 'Only unclaimed Interac can be marked claimed.')
        now = float(self.clock())
        transfer.status = IET_COMPLETED
        transfer.completed_at = now
        transfer.updated_at = now
        transfer.releaser = str(actor)
        self.store.update_transfer(transfer)
        return transfer

    def recall_transfer(
        self,
        *,
        transfer_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> IetTransfer:
        self._require_staff(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        now = float(self.clock())
        if transfer.status == IET_CLAIM:
            self._credit_back(transfer, reason='recall')
        elif transfer.status == IET_COMPLETED:
            if transfer.rail == RAIL_QUESTION:
                raise IetError('scheme_irrevocable', 'Claimed Interac cannot be recalled.')
            window = self.policy.return_hours * 3600
            if transfer.completed_at and (now - transfer.completed_at) > window:
                raise IetError('return_window_closed', 'Autodeposit return window has closed.')
            self._credit_back(transfer, reason='recall')
        else:
            raise IetError('not_recallable', 'Only sent Interac can be recalled.')
        transfer.status = IET_RECALLED
        transfer.recalled_at = now
        transfer.updated_at = now
        transfer.actor = str(actor)
        transfer.note = normalize_note(note)
        self.store.update_transfer(transfer)
        return transfer

    def waive_fee(
        self,
        *,
        transfer_id: str,
        actor: str,
        actor_type: str,
    ) -> IetTransfer:
        self._require_staff(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if transfer.status not in {IET_HELD, IET_PENDING}:
            raise IetError('not_cancelable', 'Fee can only be waived before send.')
        transfer.fee = money_str(Decimal('0.00'))
        transfer.fee_status = FEE_WAIVED
        transfer.updated_at = float(self.clock())
        transfer.actor = str(actor)
        self.store.update_transfer(transfer)
        return transfer

    def run_due(self, userid: Optional[str] = None) -> List[IetTransfer]:
        now = float(self.clock())
        changed = []
        for transfer in self.store.list_transfers(userid):
            if transfer.status != IET_CLAIM:
                continue
            if not transfer.expiry_date:
                continue
            try:
                expiry = date.fromisoformat(transfer.expiry_date)
            except ValueError:
                continue
            if self.calendar.local_dt(now).date() < expiry:
                continue
            try:
                self._credit_back(transfer, reason='expired')
            except IetError:
                transfer.status = IET_FAILED
                transfer.note = 'expiry credit failed'
                transfer.updated_at = now
                self.store.update_transfer(transfer)
                changed.append(transfer)
                continue
            transfer.status = IET_EXPIRED
            transfer.recalled_at = now
            transfer.updated_at = now
            self.store.update_transfer(transfer)
            changed.append(transfer)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.run_due(userid)
        contacts = self.store.list_contacts(userid)
        transfers = self.store.list_transfers(userid)
        sent_ytd = Decimal('0.00')
        fee_ytd = Decimal('0.00')
        recalled = Decimal('0.00')
        for row in transfers:
            usd = parse_money(row.debit_usd, allow_zero=True)
            if row.status in {IET_CLAIM, IET_COMPLETED}:
                sent_ytd += usd
                if row.fee_status == FEE_COLLECTED:
                    fee_ytd += parse_money(row.fee, allow_zero=True)
            elif row.status in {IET_RECALLED, IET_EXPIRED, IET_CANCELLED} and row.recalled_at:
                recalled += usd
        now = float(self.clock())
        payload = {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'autodeposit_cap': money_str(self.policy.autodeposit_cap),
            'question_cap': money_str(self.policy.question_cap),
            'autodeposit_fee': money_str(self.policy.autodeposit_fee),
            'question_fee': money_str(self.policy.question_fee),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'cadusd': str(self.policy.cadusd),
            'clock': self.calendar.snapshot(now),
            'contacts': [row.to_dict() for row in contacts[:40]],
            'transfers': [row.to_dict() for row in transfers[:40]],
            'ytd_sent': money_str(sent_ytd),
            'ytd_fees': money_str(fee_ytd),
            'recalled_ytd': money_str(recalled),
            'active_count': sum(1 for row in contacts if row.status == CONTACT_ACTIVE),
            'open_count': sum(1 for row in transfers if row.status in OPEN_IETS),
        }
        _ = actor, actor_type
        return payload


_SERVICE: Optional[IetService] = None


def set_service(service: Optional[IetService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[IetService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('IET_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryIetStore()
    path = os.environ.get('IET_DB', DEFAULT_STORE_PATH)
    return SqliteIetStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[InteracClock] = None,
    fx: Optional[CadUsdBook] = None,
) -> IetService:
    if store is None:
        store = default_store()
    return IetService(
        IetPolicy.from_env(),
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
        'contact_duplicate': 409,
        'contact_limit': 409,
        'transfer_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_completed': 409,
        'already_recalled': 409,
        'nsf': 409,
        'failed': 409,
        'recall_failed': 409,
        'iet_forbidden': 403,
        'iet_disabled': 403,
        'contact_paused': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_cancelable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_completable': 403,
        'not_recallable': 403,
        'not_overridable': 403,
        'scheme_irrevocable': 403,
        'return_window_closed': 403,
        'contact_not_found': 404,
        'transfer_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_alias': 400,
        'invalid_rail': 400,
        'invalid_question': 400,
        'invalid_answer': 400,
        'invalid_routing': 400,
        'invalid_reference': 400,
        'invalid_currency': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_file': 400,
        'autodeposit_amount_exceeded': 400,
        'question_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_contact': 400,
        'missing_transfer': 400,
    }.get(code, 400)


def _error_body(exc: IetError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('transfer') is not None:
        body['transfer'] = exc.extra['transfer'].to_dict()
    if exc.extra.get('contact') is not None:
        body['contact'] = exc.extra['contact'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except IetError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list_iets(service: IetService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Iet': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_add_contact(service: IetService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.add_contact(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname'),
            legal_name=values.get('legal_name') or values.get('name'),
            kind=values.get('kind') or values.get('alias_type') or KIND_EMAIL,
            alias=values.get('alias') or values.get('email') or values.get('phone'),
            rail=values.get('rail') or RAIL_AUTO,
            question=values.get('question'),
            answer=values.get('answer'),
            default_account=values.get('default_account') or values.get('account') or values.get('from_account'),
        )
        return jsonify({
            'message': 'Interac contact added',
            'contact': row.to_dict(),
            'Iet': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _contact_status_route(service: IetService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    contact_id = str(values.get('contact_id') or '').strip()
    if not contact_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_contact'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_contact_status(
            contact_id=contact_id, actor=userid, actor_type=actor_type, status=status,
        )
        return jsonify({
            'message': ok_message,
            'contact': row.to_dict(),
            'Iet': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_quote(service: IetService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}

    def _run():
        quote = service.quote_fx(values.get('amount') or values.get('amount_cad'))
        return jsonify({'quote': quote}), 200

    return _handle_errors(_run)


def handle_preview(service: IetService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    contact_id = str(values.get('contact_id') or '').strip()
    if not contact_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_contact'}), 400

    def _run():
        preview = service.preview(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            contact_id=contact_id,
            amount=values.get('amount') or values.get('amount_cad'),
            internal_account=values.get('account') or values.get('default_account') or values.get('from_account'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({'preview': preview, 'Iet': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: IetService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    contact_id = str(values.get('contact_id') or '').strip()
    if not contact_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_contact'}), 400

    def _run():
        transfer, created = service.originate(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            contact_id=contact_id,
            amount=values.get('amount') or values.get('amount_cad'),
            internal_account=values.get('account') or values.get('default_account') or values.get('from_account'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({
            'message': 'Interac originated' if created else 'Interac already submitted',
            'transfer': transfer.to_dict(),
            'Iet': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_cancel(service: IetService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    transfer_id = str(values.get('transfer_id') or '').strip()
    if not transfer_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_transfer'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        transfer = service.cancel_transfer(
            transfer_id=transfer_id, actor=userid, actor_type=actor_type, note=values.get('note'),
        )
        return jsonify({
            'message': 'Interac cancelled',
            'transfer': transfer.to_dict(),
            'Iet': service.snapshot(transfer.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_iet_route(service: IetService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    transfer_id = str(values.get('transfer_id') or '').strip()
    if not transfer_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_transfer'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'release':
            transfer = service.release_transfer(transfer_id=transfer_id, actor=userid, actor_type=actor_type)
            message = 'Interac released'
        elif action == 'reject':
            transfer = service.reject_transfer(
                transfer_id=transfer_id, actor=userid, actor_type=actor_type, note=values.get('note'),
            )
            message = 'Interac rejected'
        elif action == 'complete':
            transfer = service.complete_transfer(transfer_id=transfer_id, actor=userid, actor_type=actor_type)
            message = 'Interac claimed'
        elif action == 'recall':
            transfer = service.recall_transfer(
                transfer_id=transfer_id, actor=userid, actor_type=actor_type, note=values.get('note'),
            )
            message = 'Interac recalled'
        elif action == 'override':
            transfer = service.override_ofac(
                transfer_id=transfer_id, actor=userid, actor_type=actor_type, note=values.get('note'),
            )
            message = 'OFAC overridden'
        elif action == 'waive':
            transfer = service.waive_fee(transfer_id=transfer_id, actor=userid, actor_type=actor_type)
            message = 'Interac fee waived'
        else:
            raise IetError('invalid_status', 'Unknown staff action.')
        return jsonify({
            'message': message,
            'transfer': transfer.to_dict(),
            'Iet': service.snapshot(transfer.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: IetService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Iet': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_iet_routes(app, service: IetService) -> None:
    @app.route('/listIets', methods=['POST', 'GET'])
    def list_iets_route():
        return handle_list_iets(service)

    @app.route('/listIetContacts', methods=['POST', 'GET'])
    def list_iet_contacts_route():
        return handle_list_iets(service)

    @app.route('/addIetContact', methods=['POST', 'GET'])
    def add_iet_contact_route():
        return handle_add_contact(service)

    @app.route('/pauseIetContact', methods=['POST', 'GET'])
    def pause_iet_contact_route():
        return _contact_status_route(service, CONTACT_PAUSED, 'Interac contact paused')

    @app.route('/resumeIetContact', methods=['POST', 'GET'])
    def resume_iet_contact_route():
        return _contact_status_route(service, CONTACT_ACTIVE, 'Interac contact resumed')

    @app.route('/archiveIetContact', methods=['POST', 'GET'])
    def archive_iet_contact_route():
        return _contact_status_route(service, CONTACT_ARCHIVED, 'Interac contact archived')

    @app.route('/quoteIetFx', methods=['POST', 'GET'])
    def quote_iet_fx_route():
        return handle_quote(service)

    @app.route('/previewIet', methods=['POST', 'GET'])
    def preview_iet_route():
        return handle_preview(service)

    @app.route('/sendIet', methods=['POST', 'GET'])
    def send_iet_route():
        return handle_send(service)

    @app.route('/cancelIet', methods=['POST', 'GET'])
    def cancel_iet_route():
        return handle_cancel(service)

    @app.route('/releaseIet', methods=['POST', 'GET'])
    def release_iet_route():
        return _staff_iet_route(service, 'release')

    @app.route('/rejectIet', methods=['POST', 'GET'])
    def reject_iet_route():
        return _staff_iet_route(service, 'reject')

    @app.route('/completeIet', methods=['POST', 'GET'])
    def complete_iet_route():
        return _staff_iet_route(service, 'complete')

    @app.route('/recallIet', methods=['POST', 'GET'])
    def recall_iet_route():
        return _staff_iet_route(service, 'recall')

    @app.route('/overrideIetOfac', methods=['POST', 'GET'])
    def override_iet_ofac_route():
        return _staff_iet_route(service, 'override')

    @app.route('/waiveIetFee', methods=['POST', 'GET'])
    def waive_iet_fee_route():
        return _staff_iet_route(service, 'waive')

    @app.route('/runDueIets', methods=['POST', 'GET'])
    def run_due_iets_route():
        return handle_run_due(service)
