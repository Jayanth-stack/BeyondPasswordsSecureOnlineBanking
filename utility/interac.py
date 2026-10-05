"""Inbound Interac e-Transfer receive posting (CAD Autodeposit / SQA).

Staff ingest incoming Interac credits. Recipients are email or SMS aliases.
Autodeposit posts when the alias is registered; question-and-answer waits
for the customer to claim. Independent of domestic Fedwire (PR #73), ACH
linking (PR #68), bill-pay ACH (PR #66), inbound payroll splits (PR #64),
and in-app cheque deposit. Existing `/fundTransfer`, `/withdrawAmount`,
and `/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- CPA 9-digit EFT routing (0 + institution 3 + transit 5) with check
- Canadian NANP email / SMS Autodeposit aliases
- Interac reference (IET + 12 digits)
- CADUSD book
- Payments Canada holiday clock (Autodeposit is 24/7; no cutoff queue)
- IET1 pipe file parse/compose (XML/DOCTYPE rejected)
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
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

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
ALIAS_ACTIVE = 'active'
ALIAS_PAUSED = 'paused'
ALIAS_CANCELLED = 'cancelled'
ALIAS_STATUSES = frozenset({ALIAS_ACTIVE, ALIAS_PAUSED, ALIAS_CANCELLED})
OPEN_ALIAS = frozenset({ALIAS_ACTIVE, ALIAS_PAUSED})
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
IN_UNMATCHED = 'unmatched'
IN_HELD = 'held'
IN_PENDING = 'pending_release'
IN_CLAIM = 'pending_claim'
IN_POSTED = 'posted'
IN_REJECTED = 'rejected'
IN_RETURNED = 'returned'
IN_EXPIRED = 'expired'
IN_FAILED = 'failed'
IN_STATUSES = frozenset({
    IN_UNMATCHED, IN_HELD, IN_PENDING, IN_CLAIM, IN_POSTED, IN_REJECTED,
    IN_RETURNED, IN_EXPIRED, IN_FAILED,
})
OPEN_IN = frozenset({IN_UNMATCHED, IN_HELD, IN_PENDING, IN_CLAIM})
RETURNABLE_AFTER = frozenset({IN_POSTED})
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/interac.sqlite'
DEFAULT_RECEIVER = '000100016'
DEFAULT_CADUSD = Decimal('0.740000')
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)
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


class InteracError(ValueError):
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
        if any(token in low for token in CREDIT_NSF):
            return 'nsf'
        if low in CREDIT_OK:
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
        raise InteracError('invalid_routing', 'CPA routing stem must be 8 digits.')
    for digit in '0123456789':
        candidate = stem8 + digit
        if cpa_checksum_ok(candidate):
            return digit
    raise InteracError('invalid_routing', 'CPA routing check digit could not be computed.')


def compose_cpa_routing(institution: Any, transit: Any) -> str:
    inst = re.sub(r'\D', '', str(institution or ''))
    trans = re.sub(r'\D', '', str(transit or ''))
    if len(inst) == 4 and inst.startswith('0'):
        inst = inst[1:]
    if len(inst) != 3 or inst not in CA_INSTITUTIONS:
        raise InteracError('invalid_routing', 'Unknown Canadian institution number.')
    if len(trans) == 4:
        trans = trans + cpa_check_digit('0' + inst + trans)
    if len(trans) != 5:
        raise InteracError('invalid_routing', 'Transit must be 5 digits.')
    routing = '0' + inst + trans
    if not cpa_checksum_ok(routing):
        raise InteracError('invalid_routing', 'CPA routing number failed checksum.')
    return routing


def normalize_cpa_routing(value: Any, *, required: bool = True) -> str:
    digits = re.sub(r'\D', '', str(value or ''))
    if not digits:
        if required:
            raise InteracError('invalid_routing', 'CPA routing number required.')
        return ''
    if len(digits) == 8:
        digits = '0' + digits
    if len(digits) != 9 or digits[0] != '0' or digits[1:4] not in CA_INSTITUTIONS:
        raise InteracError('invalid_routing', 'CPA routing must be 0 + institution + transit.')
    if not cpa_checksum_ok(digits):
        raise InteracError('invalid_routing', 'CPA routing number failed checksum.')
    return digits


def mask_routing(routing: str) -> str:
    digits = re.sub(r'\D', '', str(routing or ''))
    if len(digits) < 4:
        return '****'
    return digits[:4] + '****' + digits[-1:]


def normalize_email(value: Any) -> str:
    text = str(value or '').strip().lower()
    if not EMAIL_RE.match(text) or '..' in text or text.startswith('.') or text.endswith('.'):
        raise InteracError('invalid_alias', 'Autodeposit email is not valid.')
    local, domain = text.split('@', 1)
    if not (1 <= len(local) <= 64) or not (3 <= len(domain) <= 80):
        raise InteracError('invalid_alias', 'Autodeposit email is not valid.')
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
        raise InteracError('invalid_alias', 'Autodeposit phone must be a Canadian NANP number.')
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
        raise InteracError('invalid_alias', 'Alias must be email or sms.')
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
        raise InteracError('invalid_rail', 'Rail must be autodeposit or question.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise InteracError('invalid_name', 'Sender name must be 2-80 characters.')
    return text


def normalize_nickname(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise InteracError('invalid_nickname', 'Alias nickname must be 2-40 characters.')
    return text


def normalize_question(value: Any, *, required: bool = False) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not text:
        if required:
            raise InteracError('invalid_question', 'Security question is required.')
        return ''
    if not (4 <= len(text) <= 80) or '|' in text:
        raise InteracError('invalid_question', 'Security question must be 4-80 characters.')
    return text


def normalize_answer(value: Any) -> str:
    text = ' '.join(str(value or '').strip().lower().split())
    if not (3 <= len(text) <= 80):
        raise InteracError('invalid_answer', 'Security answer must be 3-80 characters.')
    return text


def answer_fingerprint(value: Any) -> str:
    return hashlib.sha256(normalize_answer(value).encode('utf-8')).hexdigest()


def answers_match(digest: str, attempt: Any) -> bool:
    if not digest:
        return False
    try:
        got = answer_fingerprint(attempt)
    except InteracError:
        return False
    return hmac.compare_digest(digest, got)


def compose_reference(cycle_date: str, sequence: int) -> str:
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise InteracError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 9999:
        raise InteracError('invalid_reference', 'Interac sequence out of range.')
    return 'IET%s%04d' % (day, seq)


def normalize_reference(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        if required:
            raise InteracError('invalid_reference', 'Interac reference required.')
        return ''
    if not REF_RE.match(text):
        raise InteracError('invalid_reference', 'Interac reference must be IET + 12 digits.')
    return text


def mask_reference(value: str) -> str:
    text = str(value or '')
    if len(text) < 8:
        return 'IET********'
    return text[:6] + '*****' + text[-4:]


def normalize_currency(value: Any) -> str:
    text = str(value or 'CAD').strip().upper()
    if text != 'CAD':
        raise InteracError('invalid_currency', 'Interac credits must be CAD.')
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
    """Payments Canada calendar. Autodeposit posts 24/7; snapshot still reports holidays."""

    def __init__(
        self,
        *,
        tz_offset_hours: int = -4,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.tz_offset_hours = int(tz_offset_hours)
        self.extra = set()
        for item in extra_holidays:
            text = str(item).strip()
            if text:
                self.extra.add(date.fromisoformat(text) if '-' in text else datetime.strptime(text, '%Y%m%d').date())

    def local_dt(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=self.tz_offset_hours)))

    def is_business_day(self, day: date) -> bool:
        if day.weekday() >= 5:
            return False
        if day in payments_canada_holidays(day.year) or day in self.extra:
            return False
        return True

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
        raise InteracError('invalid_file', 'XML/DOCTYPE Interac files are rejected.')


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
    receiver: str = DEFAULT_RECEIVER,
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
        raise InteracError('invalid_file', 'Empty Interac record.')
    parts = text.split('|')
    if len(parts) != 12 or parts[0] != 'IET1':
        raise InteracError('invalid_file', 'IET1 records must have 12 pipe fields.')
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
        raise InteracError('invalid_file', 'Interac file had no IET1 records.')
    return records


def message_from_iet(record: str) -> Dict[str, Any]:
    return parse_iet(record)


def message_from_values(values: Dict[str, Any]) -> Dict[str, Any]:
    kind = normalize_kind(values.get('alias_type') or values.get('kind') or KIND_EMAIL)
    rail = normalize_rail(values.get('rail') or RAIL_AUTO)
    payload = {
        'reference': normalize_reference(values.get('reference') or values.get('scheme_id')),
        'amount': parse_money(values.get('amount') or values.get('amount_cad')),
        'currency': normalize_currency(values.get('currency') or 'CAD'),
        'sender_name': normalize_legal_name(values.get('sender_name') or values.get('originator')),
        'sender_fi': normalize_cpa_routing(values.get('sender_fi') or values.get('originator_fi') or DEFAULT_RECEIVER),
        'alias_type': kind,
        'alias': normalize_alias_value(kind, values.get('alias') or values.get('alias_value')),
        'rail': rail,
        'question': normalize_question(values.get('question'), required=(rail == RAIL_QUESTION)),
        'expiry': str(values.get('expiry') or values.get('expiry_date') or '').strip(),
        'receiver_routing': normalize_cpa_routing(
            values.get('receiver_routing') or values.get('receiver') or DEFAULT_RECEIVER,
        ),
    }
    if values.get('answer'):
        payload['answer'] = values.get('answer')
    return payload


def compose_return_iet(row: 'InteracInbound', *, reason: str = 'cust') -> str:
    return compose_iet(
        reference=row.reference,
        amount=parse_money(row.amount_cad),
        sender_name=row.sender_name,
        sender_fi=row.sender_fi,
        kind=row.alias_kind,
        alias=row.alias_value,
        rail=row.rail,
        question='RETURN %s' % reason,
        expiry='',
        receiver=row.receiver_routing,
    )


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
class InteracPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_claim: bool = True
    allow_credit: bool = False
    max_aliases: int = 8
    max_inbounds: int = 120
    min_amount: Decimal = Decimal('0.01')
    autodeposit_cap: Decimal = Decimal('10000.00')
    question_cap: Decimal = Decimal('25000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    claim_attempts: int = 3
    expiry_days: int = 30
    return_hours: int = 24
    tz_offset_hours: int = -4
    receiver_routing: str = DEFAULT_RECEIVER
    cadusd: Decimal = DEFAULT_CADUSD
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'InteracPolicy':
        extra = _env_list('INTERAC_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        routing = os.environ.get('INTERAC_RECEIVER') or DEFAULT_RECEIVER
        rate = os.environ.get('INTERAC_CADUSD') or str(DEFAULT_CADUSD)
        return cls(
            enabled=_env_bool('INTERAC_ENABLED', True),
            customer_manage=_env_bool('INTERAC_CUSTOMER_MANAGE', True),
            customer_claim=_env_bool('INTERAC_CUSTOMER_CLAIM', True),
            allow_credit=_env_bool('INTERAC_ALLOW_CREDIT', False),
            max_aliases=max(1, _env_int('INTERAC_MAX_ALIASES', 8)),
            max_inbounds=max(1, _env_int('INTERAC_MAX_INBOUNDS', 120)),
            min_amount=_env_money('INTERAC_MIN_AMOUNT', '0.01'),
            autodeposit_cap=_env_money('INTERAC_AUTO_CAP', '10000.00'),
            question_cap=_env_money('INTERAC_SQA_CAP', '25000.00'),
            dual_control_threshold=_env_money('INTERAC_DUAL_CONTROL', '10000.00'),
            claim_attempts=max(1, _env_int('INTERAC_CLAIM_ATTEMPTS', 3)),
            expiry_days=max(1, _env_int('INTERAC_EXPIRY_DAYS', 30)),
            return_hours=max(1, _env_int('INTERAC_RETURN_HOURS', 24)),
            tz_offset_hours=_env_int('INTERAC_TZ_OFFSET', -4),
            receiver_routing=normalize_cpa_routing(routing),
            cadusd=Decimal(str(rate)),
            watchlist=watch,
            extra_holidays=_env_list('INTERAC_HOLIDAYS'),
        )


@dataclass
class InteracAlias:
    alias_id: str
    userid: str
    nickname: str
    kind: str
    value: str
    fingerprint: str
    destination_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        last4 = self.destination_account[-4:] if len(self.destination_account) >= 4 else self.destination_account
        return {
            'alias_id': self.alias_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'kind': self.kind,
            'alias_masked': mask_alias(self.kind, self.value),
            'account_last4': last4,
            'status': self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


@dataclass
class InteracInbound:
    inbound_id: str
    reference: str
    userid: str
    destination_account: str
    amount_cad: str
    credit_usd: str
    fx_rate: str
    sender_name: str
    sender_fi: str
    alias_kind: str
    alias_value: str
    rail: str
    question: str
    answer_digest: str
    receiver_routing: str
    status: str
    reason: str
    actor: str
    releaser: str
    ofac_hit: bool
    ofac_match: str
    ofac_overridden: bool
    claimed: bool
    attempts: int
    created_at: float
    updated_at: float
    posted_at: float = 0.0
    returned_at: float = 0.0
    expiry_at: float = 0.0
    note: str = ''

    def to_dict(self) -> Dict[str, Any]:
        last4 = self.destination_account[-4:] if len(self.destination_account) >= 4 else ''
        return {
            'inbound_id': self.inbound_id,
            'reference': self.reference,
            'reference_masked': mask_reference(self.reference),
            'userid': self.userid,
            'account_last4': last4,
            'amount_cad': self.amount_cad,
            'credit_usd': self.credit_usd,
            'fx_rate': self.fx_rate,
            'sender_name': self.sender_name,
            'sender_fi_masked': mask_routing(self.sender_fi),
            'alias_kind': self.alias_kind,
            'alias_masked': mask_alias(self.alias_kind, self.alias_value),
            'rail': self.rail,
            'question': self.question if self.rail == RAIL_QUESTION else '',
            'status': self.status,
            'reason': self.reason,
            'ofac_hit': self.ofac_hit,
            'claimable': self.status == IN_CLAIM,
            'returnable': self.status in OPEN_IN or self.status == IN_POSTED,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'posted_at': self.posted_at,
            'expiry_at': self.expiry_at,
        }


def _alias_from_row(row: Any) -> InteracAlias:
    data = dict(row)
    return InteracAlias(
        alias_id=data['alias_id'], userid=data['userid'], nickname=data['nickname'],
        kind=data['kind'], value=data['value'], fingerprint=data['fingerprint'],
        destination_account=data['destination_account'], status=data['status'],
        actor=data['actor'], created_at=float(data['created_at']), updated_at=float(data['updated_at']),
    )


def _inbound_from_row(row: Any) -> InteracInbound:
    data = dict(row)
    return InteracInbound(
        inbound_id=data['inbound_id'], reference=data['reference'], userid=data['userid'],
        destination_account=data['destination_account'], amount_cad=data['amount_cad'],
        credit_usd=data['credit_usd'], fx_rate=data['fx_rate'], sender_name=data['sender_name'],
        sender_fi=data['sender_fi'], alias_kind=data['alias_kind'], alias_value=data['alias_value'],
        rail=data['rail'], question=data['question'], answer_digest=data['answer_digest'],
        receiver_routing=data['receiver_routing'], status=data['status'], reason=data['reason'],
        actor=data['actor'], releaser=data['releaser'], ofac_hit=bool(int(data['ofac_hit'])),
        ofac_match=data['ofac_match'], ofac_overridden=bool(int(data['ofac_overridden'])),
        claimed=bool(int(data['claimed'])), attempts=int(data['attempts']),
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
        posted_at=float(data['posted_at'] or 0), returned_at=float(data['returned_at'] or 0),
        expiry_at=float(data['expiry_at'] or 0), note=data.get('note') or '',
    )


class MemoryInteracStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.aliases: Dict[str, InteracAlias] = {}
        self.inbounds: Dict[str, InteracInbound] = {}
        self._by_ref: Dict[str, str] = {}
        self._by_fp: Dict[str, str] = {}

    def put_alias(self, row: InteracAlias) -> None:
        with self._lock:
            self.aliases[row.alias_id] = row
            self._by_fp[row.fingerprint] = row.alias_id

    def get_alias(self, alias_id: str) -> Optional[InteracAlias]:
        return self.aliases.get(alias_id)

    def get_alias_by_fingerprint(self, fingerprint: str) -> Optional[InteracAlias]:
        alias_id = self._by_fp.get(fingerprint)
        return self.aliases.get(alias_id) if alias_id else None

    def update_alias(self, row: InteracAlias) -> None:
        self.put_alias(row)

    def list_aliases(self, userid: Optional[str] = None) -> List[InteracAlias]:
        rows = list(self.aliases.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        return sorted(rows, key=lambda row: row.created_at)

    def put_inbound(self, row: InteracInbound) -> None:
        with self._lock:
            self.inbounds[row.inbound_id] = row
            self._by_ref[row.reference] = row.inbound_id

    def get_inbound(self, inbound_id: str) -> Optional[InteracInbound]:
        return self.inbounds.get(inbound_id)

    def get_by_reference(self, reference: str) -> Optional[InteracInbound]:
        inbound_id = self._by_ref.get(reference)
        return self.inbounds.get(inbound_id) if inbound_id else None

    def update_inbound(self, row: InteracInbound) -> None:
        self.put_inbound(row)

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[InteracInbound]:
        rows = list(self.inbounds.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)


class SqliteInteracStore:
    def __init__(self, path: str = DEFAULT_STORE_PATH) -> None:
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        return conn

    def _init(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS aliases (
                    alias_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    value TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    destination_account TEXT NOT NULL,
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
                    reference TEXT NOT NULL UNIQUE,
                    userid TEXT NOT NULL DEFAULT '',
                    destination_account TEXT NOT NULL DEFAULT '',
                    amount_cad TEXT NOT NULL,
                    credit_usd TEXT NOT NULL,
                    fx_rate TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    sender_fi TEXT NOT NULL,
                    alias_kind TEXT NOT NULL,
                    alias_value TEXT NOT NULL,
                    rail TEXT NOT NULL,
                    question TEXT NOT NULL DEFAULT '',
                    answer_digest TEXT NOT NULL DEFAULT '',
                    receiver_routing TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    ofac_overridden INTEGER NOT NULL DEFAULT 0,
                    claimed INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    posted_at REAL NOT NULL DEFAULT 0,
                    returned_at REAL NOT NULL DEFAULT 0,
                    expiry_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_alias(self, row: InteracAlias) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO aliases (
                    alias_id, userid, nickname, kind, value, fingerprint,
                    destination_account, status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.alias_id, row.userid, row.nickname, row.kind, row.value, row.fingerprint,
                    row.destination_account, row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_alias(self, alias_id: str) -> Optional[InteracAlias]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM aliases WHERE alias_id = ?', (alias_id,)).fetchone()
        return _alias_from_row(row) if row else None

    def get_alias_by_fingerprint(self, fingerprint: str) -> Optional[InteracAlias]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM aliases WHERE fingerprint = ? ORDER BY created_at DESC LIMIT 1',
                (fingerprint,),
            ).fetchone()
        return _alias_from_row(row) if row else None

    def update_alias(self, row: InteracAlias) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE aliases SET nickname=?, kind=?, value=?, fingerprint=?,
                    destination_account=?, status=?, actor=?, updated_at=?
                WHERE alias_id=?
                """,
                (
                    row.nickname, row.kind, row.value, row.fingerprint, row.destination_account,
                    row.status, row.actor, row.updated_at, row.alias_id,
                ),
            )
            conn.commit()

    def list_aliases(self, userid: Optional[str] = None) -> List[InteracAlias]:
        sql = 'SELECT * FROM aliases'
        args: Tuple[Any, ...] = ()
        if userid is not None:
            sql += ' WHERE userid = ?'
            args = (userid,)
        sql += ' ORDER BY created_at'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_alias_from_row(row) for row in rows]

    def put_inbound(self, row: InteracInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inbounds (
                    inbound_id, reference, userid, destination_account, amount_cad, credit_usd,
                    fx_rate, sender_name, sender_fi, alias_kind, alias_value, rail, question,
                    answer_digest, receiver_routing, status, reason, actor, releaser, ofac_hit,
                    ofac_match, ofac_overridden, claimed, attempts, created_at, updated_at,
                    posted_at, returned_at, expiry_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.inbound_id, row.reference, row.userid, row.destination_account, row.amount_cad,
                    row.credit_usd, row.fx_rate, row.sender_name, row.sender_fi, row.alias_kind,
                    row.alias_value, row.rail, row.question, row.answer_digest, row.receiver_routing,
                    row.status, row.reason, row.actor, row.releaser, int(row.ofac_hit), row.ofac_match,
                    int(row.ofac_overridden), int(row.claimed), row.attempts, row.created_at, row.updated_at,
                    row.posted_at, row.returned_at, row.expiry_at, row.note,
                ),
            )
            conn.commit()

    def get_inbound(self, inbound_id: str) -> Optional[InteracInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,)).fetchone()
        return _inbound_from_row(row) if row else None

    def get_by_reference(self, reference: str) -> Optional[InteracInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE reference = ?', (reference,)).fetchone()
        return _inbound_from_row(row) if row else None

    def update_inbound(self, row: InteracInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE inbounds SET userid=?, destination_account=?, amount_cad=?, credit_usd=?,
                    fx_rate=?, sender_name=?, sender_fi=?, alias_kind=?, alias_value=?, rail=?,
                    question=?, answer_digest=?, receiver_routing=?, status=?, reason=?, actor=?,
                    releaser=?, ofac_hit=?, ofac_match=?, ofac_overridden=?, claimed=?, attempts=?,
                    updated_at=?, posted_at=?, returned_at=?, expiry_at=?, note=?
                WHERE inbound_id=?
                """,
                (
                    row.userid, row.destination_account, row.amount_cad, row.credit_usd, row.fx_rate,
                    row.sender_name, row.sender_fi, row.alias_kind, row.alias_value, row.rail,
                    row.question, row.answer_digest, row.receiver_routing, row.status, row.reason,
                    row.actor, row.releaser, int(row.ofac_hit), row.ofac_match, int(row.ofac_overridden),
                    int(row.claimed), row.attempts, row.updated_at, row.posted_at, row.returned_at,
                    row.expiry_at, row.note, row.inbound_id,
                ),
            )
            conn.commit()

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[InteracInbound]:
        clauses = []
        args: List[Any] = []
        if userid is not None:
            clauses.append('userid = ?')
            args.append(userid)
        if status is not None:
            clauses.append('status = ?')
            args.append(status)
        sql = 'SELECT * FROM inbounds'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        return [_inbound_from_row(row) for row in rows]


class InteracService:
    def __init__(
        self,
        policy: InteracPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        book: Optional[CadUsdBook] = None,
        calendar: Optional[InteracClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: datetime.now(tz=timezone.utc).timestamp())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn or screen_name
        self.book = book or CadUsdBook(policy.cadusd)
        self.calendar = calendar or InteracClock(
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )

    def _now(self) -> float:
        return float(self.clock())

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise InteracError('interac_disabled', 'Interac receive is disabled.')

    def _require_staff(self, actor_type: str) -> None:
        if actor_type not in EMPLOYEE_ROLES:
            raise InteracError('interac_forbidden', 'Staff only.')

    def _require_customer_manage(self, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if not self.policy.customer_manage:
            raise InteracError('interac_forbidden', 'Customers cannot manage Autodeposit aliases.')

    def _accounts(self, userid: str) -> Any:
        if not self.accounts_fn:
            return {}
        return self.accounts_fn(userid) or {}

    def _owned(self, userid: str, account: str) -> None:
        owned = own_accounts_from_customer_payload(self._accounts(userid))
        if account not in owned:
            raise InteracError('invalid_account', 'Destination is not an account of this customer.')

    def _account_type(self, userid: str, account: str) -> str:
        return account_types_from_customer_payload(self._accounts(userid)).get(account, '')

    def _credit_allowed(self, userid: str, account: str) -> bool:
        kind = self._account_type(userid, account)
        if kind == 'credit' and not self.policy.allow_credit:
            return False
        return True

    def quote(self, amount: Any) -> Dict[str, str]:
        cad = parse_money(amount)
        if cad < self.policy.min_amount:
            raise InteracError('amount_out_of_range', 'Amount is below the Interac minimum.')
        return self.book.quote(cad)

    def add_alias(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: str,
        kind: Any,
        value: Any,
        destination_account: Any,
    ) -> InteracAlias:
        self._require_enabled()
        self._require_customer_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise InteracError('interac_forbidden', 'Cannot register an alias for another customer.')
        kind_n = normalize_kind(kind)
        alias_value = normalize_alias_value(kind_n, value)
        account = normalize_account(destination_account)
        self._owned(owner_userid, account)
        if not self._credit_allowed(owner_userid, account):
            raise InteracError('credit_not_allowed', 'Credit accounts cannot receive Interac.')
        fingerprint = alias_fingerprint(kind_n, alias_value)
        existing = self.store.get_alias_by_fingerprint(fingerprint)
        if existing and existing.status in OPEN_ALIAS:
            raise InteracError('alias_duplicate', 'That Autodeposit alias is already registered.')
        open_count = sum(1 for row in self.store.list_aliases(owner_userid) if row.status in OPEN_ALIAS)
        if open_count >= self.policy.max_aliases:
            raise InteracError('alias_limit', 'Autodeposit alias limit reached.')
        now = self._now()
        row = InteracAlias(
            alias_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=normalize_nickname(nickname),
            kind=kind_n,
            value=alias_value,
            fingerprint=fingerprint,
            destination_account=account,
            status=ALIAS_ACTIVE,
            actor=actor,
            created_at=now,
            updated_at=now,
        )
        self.store.put_alias(row)
        return row

    def set_alias_status(
        self,
        *,
        alias_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> InteracAlias:
        self._require_enabled()
        row = self.store.get_alias(alias_id)
        if row is None:
            raise InteracError('alias_not_found', 'Autodeposit alias not found.')
        if actor_type not in EMPLOYEE_ROLES and actor != row.userid:
            raise InteracError('interac_forbidden', 'Cannot change another customer alias.')
        self._require_customer_manage(actor_type)
        if status not in ALIAS_STATUSES:
            raise InteracError('invalid_status', 'Unknown alias status.')
        if row.status == ALIAS_CANCELLED:
            raise InteracError('already_cancelled', 'Cancelled aliases cannot change.')
        if status == ALIAS_PAUSED and row.status != ALIAS_ACTIVE:
            raise InteracError('already_paused' if row.status == ALIAS_PAUSED else 'invalid_status', 'Alias is not active.')
        if status == ALIAS_ACTIVE and row.status != ALIAS_PAUSED:
            raise InteracError('already_active', 'Alias is already active.')
        if status == row.status:
            code = 'already_paused' if status == ALIAS_PAUSED else 'already_active'
            raise InteracError(code, 'Alias already in that status.')
        updated = replace(row, status=status, actor=actor, updated_at=self._now())
        self.store.update_alias(updated)
        if status == ALIAS_ACTIVE:
            self._rematch_unmatched(updated)
        return updated

    def _rematch_unmatched(self, alias: InteracAlias) -> None:
        fingerprint = alias.fingerprint
        for inbound in self.store.list_inbounds(status=IN_UNMATCHED):
            if alias_fingerprint(inbound.alias_kind, inbound.alias_value) != fingerprint:
                continue
            inbound.userid = alias.userid
            inbound.destination_account = alias.destination_account
            inbound.updated_at = self._now()
            self.store.update_inbound(inbound)
            self._evaluate(inbound, rescreen=True)

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        values: Dict[str, Any],
    ) -> Tuple[InteracInbound, bool]:
        self._require_enabled()
        self._require_staff(actor_type)
        parsed = message_from_values(values)
        return self._ingest_parsed(actor=actor, parsed=parsed, answer=values.get('answer'))

    def ingest_file(
        self,
        *,
        actor: str,
        actor_type: str,
        payload: str,
    ) -> Tuple[List[InteracInbound], int]:
        self._require_enabled()
        self._require_staff(actor_type)
        created = 0
        rows = []
        for record in split_iet_file(payload):
            parsed = parse_iet(record)
            row, is_new = self._ingest_parsed(actor=actor, parsed=parsed, answer=None)
            rows.append(row)
            if is_new:
                created += 1
        return rows, created

    def _ingest_parsed(
        self,
        *,
        actor: str,
        parsed: Dict[str, Any],
        answer: Any,
    ) -> Tuple[InteracInbound, bool]:
        if parsed['receiver_routing'] != self.policy.receiver_routing:
            raise InteracError('wrong_receiver', 'Interac credit is not for this receiver FI.')
        existing = self.store.get_by_reference(parsed['reference'])
        if existing is not None:
            return existing, False
        total = len(self.store.list_inbounds())
        if total >= self.policy.max_inbounds:
            raise InteracError('inbound_limit', 'Inbound Interac limit reached.')
        amount = parsed['amount']
        if amount < self.policy.min_amount:
            raise InteracError('amount_out_of_range', 'Amount is below the Interac minimum.')
        cap = self.policy.autodeposit_cap if parsed['rail'] == RAIL_AUTO else self.policy.question_cap
        if amount > cap:
            code = 'autodeposit_amount_exceeded' if parsed['rail'] == RAIL_AUTO else 'question_amount_exceeded'
            raise InteracError(code, 'Amount exceeds the Interac rail cap.')
        fx = self.book.quote(amount)
        now = self._now()
        expiry_at = now + (self.policy.expiry_days * 86400)
        if parsed.get('expiry'):
            text = str(parsed['expiry'])
            try:
                if '-' in text:
                    day = date.fromisoformat(text[:10])
                else:
                    day = datetime.strptime(text[:8], '%Y%m%d').date()
                local = self.calendar.local_dt(now)
                expiry_at = datetime(day.year, day.month, day.day, 23, 59, tzinfo=local.tzinfo).timestamp()
            except (ValueError, TypeError) as exc:
                raise InteracError('invalid_date', 'Expiry date is invalid.') from exc
        digest = ''
        if parsed['rail'] == RAIL_QUESTION:
            if answer:
                digest = answer_fingerprint(answer)
            elif not parsed['question']:
                raise InteracError('invalid_question', 'Security question is required.')
        row = InteracInbound(
            inbound_id=uuid.uuid4().hex,
            reference=parsed['reference'],
            userid='',
            destination_account='',
            amount_cad=fx['amount_cad'],
            credit_usd=fx['amount_usd'],
            fx_rate=fx['rate'],
            sender_name=parsed['sender_name'],
            sender_fi=parsed['sender_fi'],
            alias_kind=parsed['alias_type'],
            alias_value=parsed['alias'],
            rail=parsed['rail'],
            question=parsed['question'],
            answer_digest=digest,
            receiver_routing=parsed['receiver_routing'],
            status=IN_UNMATCHED,
            reason='',
            actor=actor,
            releaser='',
            ofac_hit=False,
            ofac_match='',
            ofac_overridden=False,
            claimed=False,
            attempts=0,
            created_at=now,
            updated_at=now,
            expiry_at=expiry_at,
        )
        alias = self.store.get_alias_by_fingerprint(alias_fingerprint(row.alias_kind, row.alias_value))
        if alias is not None and alias.status == ALIAS_ACTIVE:
            row.userid = alias.userid
            row.destination_account = alias.destination_account
        self.store.put_inbound(row)
        self._evaluate(row, rescreen=True)
        return row, True

    def _evaluate(self, row: InteracInbound, *, rescreen: bool) -> InteracInbound:
        now = self._now()
        if row.status in {IN_POSTED, IN_REJECTED, IN_RETURNED, IN_EXPIRED, IN_FAILED}:
            return row
        if row.expiry_at and now > row.expiry_at and row.status in OPEN_IN:
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
            self._owned(row.userid, row.destination_account)
        except InteracError:
            row.status = IN_UNMATCHED
            row.reason = 'unmatched'
            row.userid = ''
            row.destination_account = ''
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if not self._credit_allowed(row.userid, row.destination_account):
            row.status = IN_UNMATCHED
            row.reason = 'credit_not_allowed'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if rescreen and not row.ofac_overridden:
            hit = self.screen_fn(row.sender_name, watchlist=self.policy.watchlist)
            row.ofac_hit = bool(hit.hit)
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
        usd = parse_money(row.credit_usd)
        if usd >= self.policy.dual_control_threshold and not row.releaser:
            row.status = IN_PENDING
            row.reason = 'dual_control'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if row.rail == RAIL_QUESTION and not row.claimed:
            row.status = IN_CLAIM
            row.reason = ''
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        return self._post(row)

    def _post(self, row: InteracInbound) -> InteracInbound:
        if self.credit_fn is None:
            raise InteracError('failed', 'Credit function is not configured.')
        remark = 'interac from %s' % row.sender_name[:40]
        result = self.credit_fn(row.destination_account, row.credit_usd, remark)
        classified = _classify_money_result(result)
        now = self._now()
        if classified == 'nsf':
            row.status = IN_FAILED
            row.reason = 'nsf'
            row.updated_at = now
            self.store.update_inbound(row)
            raise InteracError('nsf', 'Credit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            row.status = IN_FAILED
            row.reason = 'failed'
            row.updated_at = now
            self.store.update_inbound(row)
            raise InteracError('failed', 'Credit failed.', inbound=row)
        row.status = IN_POSTED
        row.reason = ''
        row.posted_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def preview(self, *, inbound_id: str = '', reference: str = '', actor: str = '', actor_type: str = '') -> Dict[str, Any]:
        row = self._get(inbound_id=inbound_id, reference=reference)
        self._assert_visible(row, actor, actor_type)
        fx = self.book.quote(parse_money(row.amount_cad))
        return {
            'inbound': row.to_dict(),
            'fx': fx,
            'clock': self.calendar.snapshot(self._now()),
            'receiver': mask_routing(self.policy.receiver_routing),
        }

    def _get(self, *, inbound_id: str = '', reference: str = '') -> InteracInbound:
        row = None
        if inbound_id:
            row = self.store.get_inbound(str(inbound_id).strip())
        if row is None and reference:
            row = self.store.get_by_reference(normalize_reference(reference))
        if row is None:
            raise InteracError('inbound_not_found', 'Inbound Interac not found.')
        return row

    def _assert_visible(self, row: InteracInbound, actor: str, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if row.userid and row.userid != actor:
            raise InteracError('interac_forbidden', 'Cannot view another customer Interac.')

    def assign(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        customer_id: str,
        account: Any,
        nickname: str = '',
    ) -> InteracInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_UNMATCHED:
            raise InteracError('not_assignable', 'Only unmatched Interac credits can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise InteracError('missing_customer_id', 'Customer id is required to assign.')
        dest = normalize_account(account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise InteracError('credit_not_allowed', 'Credit accounts cannot receive Interac.')
        fingerprint = alias_fingerprint(row.alias_kind, row.alias_value)
        existing = self.store.get_alias_by_fingerprint(fingerprint)
        if existing is None or existing.status == ALIAS_CANCELLED:
            self.add_alias(
                owner_userid=owner, actor=actor, actor_type=actor_type,
                nickname=nickname or ('Interac %s' % row.alias_kind),
                kind=row.alias_kind, value=row.alias_value, destination_account=dest,
            )
        elif existing.userid != owner:
            raise InteracError('alias_duplicate', 'Alias belongs to another customer.')
        row.userid = owner
        row.destination_account = dest
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=True)

    def override_ofac(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> InteracInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_HELD:
            raise InteracError('not_overridable', 'Only OFAC-held Interac credits can be overridden.')
        row.ofac_overridden = True
        row.ofac_hit = False
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def release(self, *, inbound_id: str, actor: str, actor_type: str) -> InteracInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_PENDING:
            raise InteracError('not_releasable', 'Only dual-control Interac credits can be released.')
        if actor == row.actor:
            raise InteracError('same_approver', 'A different employee must release this credit.')
        row.releaser = actor
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def reject(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> InteracInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status not in OPEN_IN:
            raise InteracError('not_rejectable', 'This Interac credit cannot be rejected.')
        row.status = IN_REJECTED
        row.reason = 'rejected'
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return row

    def claim(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        answer: Any,
    ) -> InteracInbound:
        self._require_enabled()
        row = self._get(inbound_id=inbound_id)
        self._assert_visible(row, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_claim:
                raise InteracError('interac_forbidden', 'Customers cannot claim Interac credits.')
            if actor != row.userid:
                raise InteracError('interac_forbidden', 'Cannot claim another customer Interac.')
        if row.status != IN_CLAIM:
            raise InteracError('not_claimable', 'This Interac credit is not waiting for a claim.')
        if row.attempts >= self.policy.claim_attempts:
            raise InteracError('claim_locked', 'Security-answer attempts exceeded.')
        if not row.answer_digest or not answers_match(row.answer_digest, answer):
            row.attempts += 1
            row.updated_at = self._now()
            self.store.update_inbound(row)
            if row.attempts >= self.policy.claim_attempts:
                raise InteracError('claim_locked', 'Security-answer attempts exceeded.', inbound=row)
            raise InteracError('invalid_answer', 'Security answer does not match.', inbound=row)
        row.claimed = True
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def decline(self, *, inbound_id: str, actor: str, actor_type: str) -> InteracInbound:
        self._require_enabled()
        row = self._get(inbound_id=inbound_id)
        self._assert_visible(row, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != row.userid:
            raise InteracError('interac_forbidden', 'Cannot decline another customer Interac.')
        if row.status != IN_CLAIM:
            raise InteracError('not_declinable', 'Only unclaimed question transfers can be declined.')
        row.status = IN_REJECTED
        row.reason = 'declined'
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return row

    def return_inbound(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: str = 'cust',
    ) -> InteracInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        return self._return(row, actor=actor, reason=reason, honor_window=False)

    def request_return(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: str = 'cust',
    ) -> InteracInbound:
        self._require_enabled()
        row = self._get(inbound_id=inbound_id)
        self._assert_visible(row, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != row.userid:
            raise InteracError('interac_forbidden', 'Cannot return another customer Interac.')
        return self._return(row, actor=actor, reason=reason, honor_window=True)

    def _return(self, row: InteracInbound, *, actor: str, reason: str, honor_window: bool) -> InteracInbound:
        now = self._now()
        if row.status == IN_RETURNED or row.returned_at:
            raise InteracError('already_returned', 'Interac credit already returned.')
        if row.status in OPEN_IN:
            row.status = IN_RETURNED
            row.reason = reason or 'cust'
            row.returned_at = now
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if row.status != IN_POSTED:
            raise InteracError('not_returnable', 'This Interac credit cannot be returned.')
        if row.returned_at:
            raise InteracError('already_returned', 'Interac credit already returned.')
        if honor_window and row.posted_at and (now - row.posted_at) > (self.policy.return_hours * 3600):
            raise InteracError('return_window_closed', 'Customer Interac return window has closed.')
        if self.debit_fn is None:
            raise InteracError('return_failed', 'Debit function is not configured.')
        remark = 'interac return %s' % row.reference[-12:]
        result = self.debit_fn(row.destination_account, row.credit_usd, remark)
        classified = _classify_money_result(result)
        if classified == 'nsf':
            raise InteracError('nsf', 'Return debit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            raise InteracError('return_failed', 'Return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.reason = reason or 'cust'
        row.returned_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[InteracInbound]:
        now = self._now()
        changed = []
        rows = self.store.list_inbounds(userid) if userid else self.store.list_inbounds()
        for row in rows:
            if row.status in OPEN_IN and row.expiry_at and now > row.expiry_at:
                row.status = IN_EXPIRED
                row.reason = 'expired'
                row.updated_at = now
                self.store.update_inbound(row)
                changed.append(row)
        return changed

    def snapshot(self, userid: str, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        _ = actor
        now = self._now()
        if userid:
            self.run_due(userid)
        aliases = self.store.list_aliases(userid) if userid else []
        inbounds = self.store.list_inbounds(userid) if userid else []
        posted = [row for row in inbounds if row.status == IN_POSTED]
        ytd = sum((parse_money(row.credit_usd, allow_zero=True) for row in posted), Decimal('0.00'))
        return {
            'enabled': self.policy.enabled,
            'clock': self.calendar.snapshot(now),
            'fx': {'cadusd': str(self.book.cadusd)},
            'receiver': mask_routing(self.policy.receiver_routing),
            'aliases': [row.to_dict() for row in aliases[:40]],
            'inbounds': [row.to_dict() for row in inbounds[:40]],
            'ytd_received': money_str(ytd),
            'open_count': sum(1 for row in inbounds if row.status in OPEN_IN),
            'active_aliases': sum(1 for row in aliases if row.status == ALIAS_ACTIVE),
            'autodeposit_cap': money_str(self.policy.autodeposit_cap),
            'question_cap': money_str(self.policy.question_cap),
            'dual_control': money_str(self.policy.dual_control_threshold),
        }

    def unmatched_snapshot(self) -> Dict[str, Any]:
        rows = self.store.list_inbounds(status=IN_UNMATCHED)
        return {
            'enabled': self.policy.enabled,
            'unmatched': [row.to_dict() for row in rows[:40]],
            'count': len(rows),
        }


_SERVICE: Optional[InteracService] = None


def set_service(service: Optional[InteracService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[InteracService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('INTERAC_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryInteracStore()
    path = os.environ.get('INTERAC_DB', DEFAULT_STORE_PATH)
    return SqliteInteracStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    book: Optional[CadUsdBook] = None,
    calendar: Optional[InteracClock] = None,
) -> InteracService:
    if store is None:
        store = default_store()
    return InteracService(
        InteracPolicy.from_env(),
        store,
        clock=clock,
        debit_fn=debit_fn,
        credit_fn=credit_fn,
        accounts_fn=accounts_fn,
        screen_fn=screen_fn,
        book=book,
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
        'alias_duplicate': 409,
        'alias_limit': 409,
        'inbound_limit': 409,
        'already_cancelled': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_returned': 409,
        'nsf': 409,
        'failed': 409,
        'return_failed': 409,
        'interac_forbidden': 403,
        'interac_disabled': 403,
        'credit_not_allowed': 403,
        'same_approver': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_claimable': 403,
        'not_declinable': 403,
        'not_returnable': 403,
        'claim_locked': 403,
        'return_window_closed': 403,
        'alias_not_found': 404,
        'inbound_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_alias': 400,
        'invalid_routing': 400,
        'invalid_reference': 400,
        'invalid_rail': 400,
        'invalid_question': 400,
        'invalid_answer': 400,
        'invalid_currency': 400,
        'invalid_file': 400,
        'invalid_date': 400,
        'invalid_status': 400,
        'wrong_receiver': 400,
        'autodeposit_amount_exceeded': 400,
        'question_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_alias': 400,
        'missing_file': 400,
        'missing_answer': 400,
    }.get(code, 400)


def _error_body(exc: InteracError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('inbound') is not None:
        body['inbound'] = exc.extra['inbound'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except InteracError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snapshot_for(service: InteracService, userid: str, actor: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(userid, actor=actor, actor_type=actor_type)


def handle_list(service: InteracService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = _owner_userid(userid, actor_type, values)
        if not owner:
            return jsonify({'message': 'Customer id required', 'error': 'missing_customer_id'}), 400
    return jsonify({'Interacs': _snapshot_for(service, owner, userid, actor_type)}), 200


def handle_unmatched(service: InteracService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'interac_forbidden'}), 403
    snap = service.unmatched_snapshot()
    return jsonify({'UnmatchedInteracs': snap, 'Interacs': snap}), 200


def handle_add_alias(service: InteracService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        owner = userid
        if actor_type in EMPLOYEE_ROLES:
            owner = _owner_userid(userid, actor_type, values)
            if not owner:
                raise InteracError('missing_customer_id', 'Customer id is required.')
        alias = service.add_alias(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname') or '',
            kind=values.get('kind') or values.get('alias_type') or KIND_EMAIL,
            value=values.get('alias') or values.get('value') or values.get('email') or values.get('phone'),
            destination_account=values.get('destination_account') or values.get('account') or values.get('default_account'),
        )
        return jsonify({
            'message': 'Autodeposit alias registered',
            'alias': alias.to_dict(),
            'Interacs': _snapshot_for(service, alias.userid, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _alias_status_route(service: InteracService, status: str, message: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        alias_id = str(values.get('alias_id') or '').strip()
        if not alias_id:
            raise InteracError('missing_alias', 'alias_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.set_alias_status(alias_id=alias_id, actor=userid, actor_type=actor_type, status=status)
        return jsonify({
            'message': message,
            'alias': row.to_dict(),
            'Interacs': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_quote(service: InteracService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        return jsonify({'fx': service.quote(values.get('amount')), 'Interacs': _snapshot_for(
            service, userid, userid, session.get('usertype') or 'customer',
        )}), 200

    return _handle_errors(_run)


def handle_preview(service: InteracService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        preview = service.preview(
            inbound_id=str(values.get('inbound_id') or ''),
            reference=str(values.get('reference') or ''),
            actor=userid,
            actor_type=actor_type,
        )
        owner = preview['inbound'].get('userid') or userid
        return jsonify({'preview': preview, 'Interacs': _snapshot_for(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest(service: InteracService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'Interac ingested' if created else 'Interac already ingested',
            'inbound': row.to_dict(),
            'Interacs': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_ingest_file(service: InteracService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        payload = values.get('file') or values.get('body') or values.get('payload')
        if not payload:
            raise InteracError('missing_file', 'Interac file body is required.')
        actor_type = session.get('usertype') or 'customer'
        rows, created = service.ingest_file(actor=userid, actor_type=actor_type, payload=str(payload))
        owner = rows[-1].userid if rows and rows[-1].userid else userid
        return jsonify({
            'message': 'Interac file ingested',
            'created': created,
            'inbounds': [row.to_dict() for row in rows],
            'Interacs': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_claim(service: InteracService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise InteracError('missing_inbound', 'inbound_id is required.')
        if not values.get('answer'):
            raise InteracError('missing_answer', 'Security answer is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.claim(inbound_id=inbound_id, actor=userid, actor_type=actor_type, answer=values.get('answer'))
        return jsonify({
            'message': 'Interac claimed',
            'inbound': row.to_dict(),
            'Interacs': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_decline(service: InteracService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise InteracError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.decline(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
        return jsonify({
            'message': 'Interac declined',
            'inbound': row.to_dict(),
            'Interacs': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_assign(service: InteracService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise InteracError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.assign(
            inbound_id=inbound_id,
            actor=userid,
            actor_type=actor_type,
            customer_id=str(values.get('customer_id') or ''),
            account=values.get('account') or values.get('destination_account'),
            nickname=str(values.get('nickname') or ''),
        )
        return jsonify({
            'message': 'Interac assigned',
            'inbound': row.to_dict(),
            'Interacs': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_inbound_route(service: InteracService, action: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise InteracError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        if action == 'override':
            row = service.override_ofac(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'release':
            row = service.release(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            message = 'Interac released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Interac rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=str(values.get('reason') or 'cust'),
            )
            message = 'Interac returned'
        else:
            raise InteracError('invalid_status', 'Unknown Interac action.')
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'Interacs': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: InteracService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise InteracError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.request_return(
            inbound_id=inbound_id, actor=userid, actor_type=actor_type,
            reason=str(values.get('reason') or 'cust'),
        )
        return jsonify({
            'message': 'Interac return requested',
            'inbound': row.to_dict(),
            'Interacs': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: InteracService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Interacs': _snapshot_for(service, owner, userid, actor_type)}), 200


def attach_interac_routes(app, service: InteracService) -> None:
    @app.route('/listInteracs', methods=['POST', 'GET'])
    def list_interacs_route():
        return handle_list(service)

    @app.route('/listInteracAliases', methods=['POST', 'GET'])
    def list_interac_aliases_route():
        return handle_list(service)

    @app.route('/listUnmatchedInteracs', methods=['POST', 'GET'])
    def list_unmatched_interacs_route():
        return handle_unmatched(service)

    @app.route('/addInteracAlias', methods=['POST', 'GET'])
    def add_interac_alias_route():
        return handle_add_alias(service)

    @app.route('/pauseInteracAlias', methods=['POST', 'GET'])
    def pause_interac_alias_route():
        return _alias_status_route(service, ALIAS_PAUSED, 'Autodeposit alias paused')

    @app.route('/resumeInteracAlias', methods=['POST', 'GET'])
    def resume_interac_alias_route():
        return _alias_status_route(service, ALIAS_ACTIVE, 'Autodeposit alias resumed')

    @app.route('/cancelInteracAlias', methods=['POST', 'GET'])
    def cancel_interac_alias_route():
        return _alias_status_route(service, ALIAS_CANCELLED, 'Autodeposit alias cancelled')

    @app.route('/quoteInteracFx', methods=['POST', 'GET'])
    def quote_interac_fx_route():
        return handle_quote(service)

    @app.route('/previewInterac', methods=['POST', 'GET'])
    def preview_interac_route():
        return handle_preview(service)

    @app.route('/ingestInterac', methods=['POST', 'GET'])
    def ingest_interac_route():
        return handle_ingest(service)

    @app.route('/ingestInteracFile', methods=['POST', 'GET'])
    def ingest_interac_file_route():
        return handle_ingest_file(service)

    @app.route('/claimInterac', methods=['POST', 'GET'])
    def claim_interac_route():
        return handle_claim(service)

    @app.route('/declineInterac', methods=['POST', 'GET'])
    def decline_interac_route():
        return handle_decline(service)

    @app.route('/assignInterac', methods=['POST', 'GET'])
    def assign_interac_route():
        return handle_assign(service)

    @app.route('/overrideInteracOfac', methods=['POST', 'GET'])
    def override_interac_ofac_route():
        return _staff_inbound_route(service, 'override')

    @app.route('/releaseInterac', methods=['POST', 'GET'])
    def release_interac_route():
        return _staff_inbound_route(service, 'release')

    @app.route('/rejectInterac', methods=['POST', 'GET'])
    def reject_interac_route():
        return _staff_inbound_route(service, 'reject')

    @app.route('/returnInterac', methods=['POST', 'GET'])
    def return_interac_route():
        return _staff_inbound_route(service, 'return')

    @app.route('/requestInteracReturn', methods=['POST', 'GET'])
    def request_interac_return_route():
        return handle_request_return(service)

    @app.route('/runDueInteracs', methods=['POST', 'GET'])
    def run_due_interacs_route():
        return handle_run_due(service)
