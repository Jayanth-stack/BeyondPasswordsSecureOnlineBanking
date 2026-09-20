"""Domestic Fedwire origination.

Customers send same-day USD wires to external beneficiaries identified by a
full ABA routing number, account, legal name, and address. Independent of
ACH linking / micro-deposits (PR #68), bill-pay outgoing ACH (PR #66),
inbound payroll splits (PR #64), the in-bank payee allowlist (PR #26), and
scheduled internal transfers (PR #36). Existing `/fundTransfer` and
`/withdrawAmount` stay unchanged. `Customers.debit_request` /
`credit_request` still write `debited` / `direct deposited` unless a
remark is supplied here.

Foundations (reusable beyond this screen):
- ABA routing checksum
- Fedwire business-day / cutoff clock
- OFAC-style name screening against an injectable watchlist
- IMAD / OMAD identifiers
- Flat outbound wire-fee policy
- Dual-control release for high-value wires

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Destination account numbers never appear in to_dict / snapshots.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
BENE_ACTIVE = 'active'
BENE_PAUSED = 'paused'
BENE_ARCHIVED = 'archived'
BENE_STATUSES = frozenset({BENE_ACTIVE, BENE_PAUSED, BENE_ARCHIVED})
OPEN_BENE = frozenset({BENE_ACTIVE, BENE_PAUSED})
WIRE_HELD = 'held'
WIRE_QUEUED = 'queued'
WIRE_PENDING = 'pending_release'
WIRE_SENT = 'sent'
WIRE_COMPLETED = 'completed'
WIRE_REJECTED = 'rejected'
WIRE_CANCELLED = 'cancelled'
WIRE_RECALLED = 'recalled'
WIRE_NSF = 'nsf'
WIRE_FAILED = 'failed'
WIRE_STATUSES = frozenset({
    WIRE_HELD, WIRE_QUEUED, WIRE_PENDING, WIRE_SENT, WIRE_COMPLETED,
    WIRE_REJECTED, WIRE_CANCELLED, WIRE_RECALLED, WIRE_NSF, WIRE_FAILED,
})
OPEN_WIRES = frozenset({WIRE_HELD, WIRE_QUEUED, WIRE_PENDING, WIRE_SENT})
CANCELABLE = frozenset({WIRE_HELD, WIRE_QUEUED, WIRE_PENDING})
FEE_NONE = 'none'
FEE_COLLECTED = 'collected'
FEE_WAIVED = 'waived'
FEE_NSF = 'nsf'
PURPOSES = frozenset({'family', 'goods', 'payroll', 'tax', 'loan', 'rent', 'other'})
PURPOSE_ALIASES = {
    'personal': 'family', 'gift': 'family', 'support': 'family',
    'invoice': 'goods', 'purchase': 'goods', 'vendor': 'goods',
    'salary': 'payroll', 'wage': 'payroll',
    'irs': 'tax', 'taxes': 'tax',
    'mortgage': 'loan', 'housing': 'rent',
}
US_STATES = frozenset({
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID', 'IL',
    'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS', 'MO', 'MT',
    'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI',
    'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC',
})
DEFAULT_WATCHLIST = (
    'BLOCKED PERSON',
    'SANCTIONED ENTITY',
    'OFAC TESTNAME',
)
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/wire.sqlite'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)


class AccountError(ValueError):
    pass


class AmountError(ValueError):
    pass


class WireError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def normalize_account(value: Any, *, required: bool = True) -> str:
    if value is None:
        text = ''
    else:
        text = str(value).strip()
    if not text:
        if required:
            raise AccountError('invalid_account')
        return ''
    if text.endswith('.0') and text[:-2].isdigit():
        text = text[:-2]
    if not text.isdigit() or not (1 <= len(text) <= 17):
        raise AccountError('invalid_account')
    return str(int(text)) if len(text) < 16 else text.lstrip('0') or '0'


def parse_money(value: Any, *, allow_zero: bool = False) -> Decimal:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise AmountError('invalid_amount')
    try:
        amount = Decimal(str(value).strip().replace(',', '').replace('$', ''))
    except (InvalidOperation, ValueError):
        raise AmountError('invalid_amount') from None
    if not amount.is_finite():
        raise AmountError('invalid_amount')
    amount = amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    if amount < 0 or (amount == 0 and not allow_zero):
        raise AmountError('invalid_amount')
    return amount


def money_str(value: Decimal) -> str:
    return str(value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN))


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
        raise WireError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def last4(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return digits
    return digits[-4:]


def aba_check_digit_ok(digits: str) -> bool:
    """ABA routing checksum: 3(d1+d4+d7)+7(d2+d5+d8)+(d3+d6+d9) is divisible by 10."""
    if len(digits) != 9 or not digits.isdigit():
        return False
    weights = (3, 7, 1, 3, 7, 1, 3, 7, 1)
    total = sum(int(d) * w for d, w in zip(digits, weights))
    return total % 10 == 0


def normalize_aba(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        raise WireError('invalid_aba', 'ABA routing number is required.')
    if len(digits) < 9:
        digits = digits.zfill(9)
    if len(digits) != 9:
        raise WireError('invalid_aba', 'ABA routing number must be nine digits.')
    if digits == '000000000' or not aba_check_digit_ok(digits):
        raise WireError('invalid_aba', 'ABA routing number failed checksum.')
    return digits


def normalize_external_account(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not (4 <= len(digits) <= 17):
        raise WireError('invalid_account', 'External account must be 4-17 digits.')
    return digits


def normalize_party(value: Any) -> str:
    text = str(value or '').upper()
    cleaned = []
    for ch in text:
        if ch.isalnum() or ch.isspace():
            cleaned.append(ch)
        elif ch in "-'.,/&":
            cleaned.append(' ')
    return ' '.join(''.join(cleaned).split())


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise WireError('invalid_name', 'Beneficiary legal name must be 2-80 characters.')
    return text


def normalize_street(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (3 <= len(text) <= 80):
        raise WireError('invalid_address', 'Street must be 3-80 characters.')
    return text


def normalize_city(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise WireError('invalid_address', 'City must be 2-40 characters.')
    return text


def normalize_state(value: Any) -> str:
    text = str(value or '').strip().upper()
    if text not in US_STATES:
        raise WireError('invalid_address', 'State must be a USPS two-letter code.')
    return text


def normalize_postal(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) not in {5, 9}:
        raise WireError('invalid_address', 'ZIP must be 5 or 9 digits.')
    return digits


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise WireError('invalid_purpose', 'Unknown wire purpose.')
    return text


def normalize_source(value: Any, *, default: str = 'KONOHA01') -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or default).upper())
    if not text:
        text = default
    return (text + 'XXXXXXXX')[:8]


def compose_imad(cycle_date: str, source: str, sequence: int) -> str:
    """Fedwire IMAD: {YYYYMMDD}{8-char source}{6-digit sequence}."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise WireError('invalid_imad', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise WireError('invalid_imad', 'IMAD sequence out of range.')
    return '%s%s%06d' % (day, normalize_source(source), seq)


def compose_omad(cycle_date: str, sequence: int, *, frb: str = 'FRBNY001') -> str:
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise WireError('invalid_imad', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise WireError('invalid_imad', 'OMAD sequence out of range.')
    return '%s%s%06d' % (day, normalize_source(frb), seq)


def compute_fee(amount: Decimal, fee: Decimal, *, waived: bool = False) -> Decimal:
    """Flat outbound fee; waived wires cost $0. Amount is accepted for future tiered fees."""
    if waived:
        return Decimal('0.00')
    _ = amount
    return fee.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class ScreenResult:
    hit: bool
    matched: str = ''
    score: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {'hit': self.hit, 'matched': self.matched, 'score': self.score}


def screen_name(
    name: Any,
    *,
    watchlist: Iterable[str] = DEFAULT_WATCHLIST,
    aliases: Iterable[str] = (),
) -> ScreenResult:
    """Local SDN-style screen. Exact / phrase / token-subset match. Injectable list."""
    haystacks = [normalize_party(name)]
    for alias in aliases:
        cleaned = normalize_party(alias)
        if cleaned:
            haystacks.append(cleaned)
    needles = [normalize_party(item) for item in watchlist]
    needles = [item for item in needles if item]
    for hay in haystacks:
        if not hay:
            continue
        hay_tokens = set(hay.split())
        for needle in needles:
            tokens = set(needle.split())
            if len(tokens) <= 1:
                if needle == hay:
                    return ScreenResult(True, needle, 100)
                continue
            if needle == hay or needle in hay or tokens <= hay_tokens:
                score = 100 if needle == hay else 90
                return ScreenResult(True, needle, score)
    return ScreenResult(False, '', 0)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return date(year, month, 1 + offset + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    while cursor.weekday() != weekday:
        cursor -= timedelta(days=1)
    return cursor


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def us_fed_holidays(year: int) -> set:
    """Federal Reserve holiday calendar for a year (weekday-observed)."""
    days = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _last_weekday(year, 5, 0),
        _observed(date(year, 6, 19)),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 10, 0, 2),
        _observed(date(year, 11, 11)),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    return days


class WireCalendar:
    """Fedwire business-day + customer cutoff clock. Injectable offset and holidays."""

    def __init__(
        self,
        *,
        cutoff_hour: int = 17,
        cutoff_minute: int = 0,
        tz_offset_hours: int = -4,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.cutoff_hour = int(cutoff_hour)
        self.cutoff_minute = int(cutoff_minute)
        self.tz_offset_hours = int(tz_offset_hours)
        self.tz = timezone(timedelta(hours=self.tz_offset_hours))
        extra = set()
        for item in extra_holidays:
            text = str(item).strip()
            if not text:
                continue
            extra.add(date.fromisoformat(text[:10]))
        self.extra_holidays = extra

    def local_dt(self, ts: float) -> datetime:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(self.tz)

    def is_weekend(self, day: date) -> bool:
        return day.weekday() >= 5

    def is_holiday(self, day: date) -> bool:
        return day in us_fed_holidays(day.year) or day in self.extra_holidays

    def is_business_day(self, day: date) -> bool:
        return not self.is_weekend(day) and not self.is_holiday(day)

    def is_after_cutoff(self, ts: float) -> bool:
        local = self.local_dt(ts)
        if (local.hour, local.minute) >= (self.cutoff_hour, self.cutoff_minute):
            return True
        return False

    def next_business_day(self, day: date) -> date:
        cursor = day + timedelta(days=1)
        while not self.is_business_day(cursor):
            cursor += timedelta(days=1)
        return cursor

    def value_date(self, ts: float) -> date:
        local = self.local_dt(ts)
        day = local.date()
        if self.is_business_day(day) and not self.is_after_cutoff(ts):
            return day
        return self.next_business_day(day)

    def cycle_date(self, ts: float) -> str:
        return self.value_date(ts).strftime('%Y%m%d')

    def snapshot(self, ts: float) -> Dict[str, Any]:
        local = self.local_dt(ts)
        value = self.value_date(ts)
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'cutoff': '%02d:%02d' % (self.cutoff_hour, self.cutoff_minute),
            'after_cutoff': self.is_after_cutoff(ts) or not self.is_business_day(local.date()),
            'business_day': self.is_business_day(local.date()),
            'value_date': value.isoformat(),
            'cycle_date': value.strftime('%Y%m%d'),
        }


def own_accounts_from_customer_payload(accounts: Any) -> List[str]:
    if not isinstance(accounts, dict):
        return []
    found = []
    for key in ('savings', 'checkin', 'credit'):
        item = accounts.get(key)
        if isinstance(item, dict) and item.get('Account') not in (None, 'None', ''):
            try:
                found.append(normalize_account(item['Account']))
            except AccountError:
                continue
    return found


def account_types_from_customer_payload(accounts: Any) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    if not isinstance(accounts, dict):
        return mapping
    for key in ('savings', 'checkin', 'credit'):
        item = accounts.get(key)
        if isinstance(item, dict) and item.get('Account') not in (None, 'None', ''):
            try:
                mapping[normalize_account(item['Account'])] = key
            except AccountError:
                continue
    return mapping


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
class WirePolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    allow_credit: bool = False
    max_beneficiaries: int = 12
    max_wires: int = 120
    min_amount: Decimal = Decimal('10.00')
    max_amount: Decimal = Decimal('1000000.00')
    outbound_fee: Decimal = Decimal('25.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    cutoff_hour: int = 17
    tz_offset_hours: int = -4
    source_id: str = 'KONOHA01'
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'WirePolicy':
        extra = _env_list('WIRE_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('WIRE_ENABLED', True),
            customer_manage=_env_bool('WIRE_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('WIRE_CUSTOMER_SEND', True),
            allow_credit=_env_bool('WIRE_ALLOW_CREDIT', False),
            max_beneficiaries=max(1, _env_int('WIRE_MAX_BENEFICIARIES', 12)),
            max_wires=max(1, _env_int('WIRE_MAX_WIRES', 120)),
            min_amount=_env_money('WIRE_MIN_AMOUNT', '10.00'),
            max_amount=_env_money('WIRE_MAX_AMOUNT', '1000000.00'),
            outbound_fee=_env_money('WIRE_FEE', '25.00'),
            dual_control_threshold=_env_money('WIRE_DUAL_CONTROL', '10000.00'),
            cutoff_hour=max(0, min(23, _env_int('WIRE_CUTOFF_HOUR', 17))),
            tz_offset_hours=_env_int('WIRE_TZ_OFFSET', -4),
            source_id=normalize_source(os.environ.get('WIRE_SOURCE', 'KONOHA01')),
            watchlist=watch,
            extra_holidays=_env_list('WIRE_HOLIDAYS'),
        )


@dataclass
class WireBeneficiary:
    beneficiary_id: str
    userid: str
    nickname: str
    legal_name: str
    aba: str
    account_number: str
    street: str
    city: str
    state: str
    postal: str
    default_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'beneficiary_id': self.beneficiary_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'aba': self.aba,
            'account_last4': last4(self.account_number),
            'street': self.street,
            'city': self.city,
            'state': self.state,
            'postal': self.postal,
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
class WireTransfer:
    wire_id: str
    trace_id: str
    beneficiary_id: str
    userid: str
    internal_account: str
    amount: str
    fee: str
    fee_status: str
    nickname: str
    legal_name: str
    aba: str
    account_last4: str
    purpose: str
    memo: str
    status: str
    imad: str
    omad: str
    value_date: str
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
            'wire_id': self.wire_id,
            'trace_id': self.trace_id,
            'beneficiary_id': self.beneficiary_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'amount': self.amount,
            'fee': self.fee,
            'fee_status': self.fee_status,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'aba': self.aba,
            'account_last4': self.account_last4,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'imad': self.imad,
            'omad': self.omad,
            'value_date': self.value_date,
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
            'held': self.status == WIRE_HELD,
            'queued': self.status == WIRE_QUEUED,
            'pending_release': self.status == WIRE_PENDING,
            'sent': self.status == WIRE_SENT,
            'completed': self.status == WIRE_COMPLETED,
            'cancelable': self.status in CANCELABLE,
        }


def _clone_bene(row: WireBeneficiary) -> WireBeneficiary:
    return WireBeneficiary(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_wire(row: WireTransfer) -> WireTransfer:
    return WireTransfer(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _bene_from_row(row: Any) -> WireBeneficiary:
    return WireBeneficiary(
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        aba=row['aba'],
        account_number=row['account_number'],
        street=row['street'],
        city=row['city'],
        state=row['state'],
        postal=row['postal'],
        default_account=row['default_account'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
    )


def _wire_from_row(row: Any) -> WireTransfer:
    return WireTransfer(
        wire_id=row['wire_id'],
        trace_id=row['trace_id'],
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        amount=row['amount'],
        fee=row['fee'],
        fee_status=row['fee_status'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        aba=row['aba'],
        account_last4=row['account_last4'],
        purpose=row['purpose'],
        memo=row['memo'] or '',
        status=row['status'],
        imad=row['imad'] or '',
        omad=row['omad'] or '',
        value_date=row['value_date'],
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


class MemoryWireStore:
    def __init__(self) -> None:
        self._benes: Dict[str, WireBeneficiary] = {}
        self._wires: Dict[str, WireTransfer] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_beneficiary(self, row: WireBeneficiary) -> None:
        with self._lock:
            self._benes[row.beneficiary_id] = row

    def get_beneficiary(self, beneficiary_id: str) -> Optional[WireBeneficiary]:
        with self._lock:
            row = self._benes.get(beneficiary_id)
            return _clone_bene(row) if row else None

    def update_beneficiary(self, row: WireBeneficiary) -> None:
        with self._lock:
            self._benes[row.beneficiary_id] = row

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[WireBeneficiary]:
        with self._lock:
            rows = [_clone_bene(row) for row in self._benes.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != BENE_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[WireBeneficiary]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._benes.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_BENE:
                    return _clone_bene(row)
        return None

    def find_beneficiary_by_fingerprint(self, userid: str, aba: str, account_number: str) -> Optional[WireBeneficiary]:
        with self._lock:
            for row in self._benes.values():
                if (
                    row.userid == userid
                    and row.aba == aba
                    and row.account_number == account_number
                    and row.status in OPEN_BENE
                ):
                    return _clone_bene(row)
        return None

    def put_wire(self, row: WireTransfer) -> WireTransfer:
        with self._lock:
            existing_id = self._by_trace.get(row.trace_id)
            if existing_id is not None:
                return self._wires[existing_id]
            self._wires[row.wire_id] = row
            self._by_trace[row.trace_id] = row.wire_id
            return row

    def update_wire(self, row: WireTransfer) -> None:
        with self._lock:
            self._wires[row.wire_id] = row

    def get_wire(self, wire_id: str) -> Optional[WireTransfer]:
        with self._lock:
            row = self._wires.get(wire_id)
            return _clone_wire(row) if row else None

    def get_wire_by_trace(self, trace_id: str) -> Optional[WireTransfer]:
        with self._lock:
            wire_id = self._by_trace.get(trace_id)
            return _clone_wire(self._wires[wire_id]) if wire_id else None

    def list_wires(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[WireTransfer]:
        with self._lock:
            rows = [_clone_wire(row) for row in self._wires.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if beneficiary_id is not None:
            rows = [row for row in rows if row.beneficiary_id == beneficiary_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def next_imad_sequence(self, cycle_date: str) -> int:
        with self._lock:
            used = [row.imad for row in self._wires.values() if row.imad.startswith(cycle_date)]
        return len(used) + 1


class SqliteWireStore:
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
                    aba TEXT NOT NULL,
                    account_number TEXT NOT NULL,
                    street TEXT NOT NULL,
                    city TEXT NOT NULL,
                    state TEXT NOT NULL,
                    postal TEXT NOT NULL,
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
                CREATE TABLE IF NOT EXISTS wires (
                    wire_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    beneficiary_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    internal_account TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    fee_status TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    aba TEXT NOT NULL,
                    account_last4 TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    imad TEXT NOT NULL DEFAULT '',
                    omad TEXT NOT NULL DEFAULT '',
                    value_date TEXT NOT NULL,
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

    def put_beneficiary(self, row: WireBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO beneficiaries (
                    beneficiary_id, userid, nickname, legal_name, aba, account_number,
                    street, city, state, postal, default_account, status, actor,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.beneficiary_id, row.userid, row.nickname, row.legal_name, row.aba,
                    row.account_number, row.street, row.city, row.state, row.postal,
                    row.default_account, row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_beneficiary(self, beneficiary_id: str) -> Optional[WireBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM beneficiaries WHERE beneficiary_id = ?', (beneficiary_id,),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def update_beneficiary(self, row: WireBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE beneficiaries SET nickname=?, legal_name=?, aba=?, account_number=?,
                    street=?, city=?, state=?, postal=?, default_account=?, status=?,
                    actor=?, updated_at=?
                WHERE beneficiary_id=?
                """,
                (
                    row.nickname, row.legal_name, row.aba, row.account_number, row.street,
                    row.city, row.state, row.postal, row.default_account, row.status,
                    row.actor, row.updated_at, row.beneficiary_id,
                ),
            )
            conn.commit()

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[WireBeneficiary]:
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

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[WireBeneficiary]:
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

    def find_beneficiary_by_fingerprint(self, userid: str, aba: str, account_number: str) -> Optional[WireBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM beneficiaries
                WHERE userid = ? AND aba = ? AND account_number = ?
                  AND status IN ('active', 'paused')
                """,
                (userid, aba, account_number),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def put_wire(self, row: WireTransfer) -> WireTransfer:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM wires WHERE trace_id = ?', (row.trace_id,)
            ).fetchone()
            if existing is not None:
                return _wire_from_row(existing)
            conn.execute(
                """
                INSERT INTO wires (
                    wire_id, trace_id, beneficiary_id, userid, internal_account,
                    amount, fee, fee_status, nickname, legal_name, aba, account_last4,
                    purpose, memo, status, imad, omad, value_date, actor, releaser,
                    ofac_hit, ofac_match, created_at, updated_at, sent_at, completed_at,
                    recalled_at, note, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.wire_id, row.trace_id, row.beneficiary_id, row.userid,
                    row.internal_account, row.amount, row.fee, row.fee_status,
                    row.nickname, row.legal_name, row.aba, row.account_last4,
                    row.purpose, row.memo, row.status, row.imad, row.omad, row.value_date,
                    row.actor, row.releaser, row.ofac_hit, row.ofac_match, row.created_at,
                    row.updated_at, row.sent_at, row.completed_at, row.recalled_at,
                    row.note, row.reason,
                ),
            )
            conn.commit()
            return row

    def update_wire(self, row: WireTransfer) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE wires SET fee=?, fee_status=?, status=?, imad=?, omad=?,
                    value_date=?, actor=?, releaser=?, ofac_hit=?, ofac_match=?,
                    updated_at=?, sent_at=?, completed_at=?, recalled_at=?,
                    note=?, reason=?
                WHERE wire_id=?
                """,
                (
                    row.fee, row.fee_status, row.status, row.imad, row.omad, row.value_date,
                    row.actor, row.releaser, row.ofac_hit, row.ofac_match, row.updated_at,
                    row.sent_at, row.completed_at, row.recalled_at, row.note, row.reason,
                    row.wire_id,
                ),
            )
            conn.commit()

    def get_wire(self, wire_id: str) -> Optional[WireTransfer]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM wires WHERE wire_id = ?', (wire_id,)).fetchone()
        return _wire_from_row(row) if row else None

    def get_wire_by_trace(self, trace_id: str) -> Optional[WireTransfer]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM wires WHERE trace_id = ?', (trace_id,)).fetchone()
        return _wire_from_row(row) if row else None

    def list_wires(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[WireTransfer]:
        sql = 'SELECT * FROM wires'
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
        return [_wire_from_row(row) for row in rows]

    def next_imad_sequence(self, cycle_date: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM wires WHERE imad LIKE ?",
                (cycle_date + '%',),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class WireService:
    def __init__(
        self,
        policy: WirePolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[WireCalendar] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or WireCalendar(
            cutoff_hour=policy.cutoff_hour,
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise WireError('wire_disabled', 'Domestic wires are disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise WireError('wire_forbidden', 'Customers cannot manage wire beneficiaries.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise WireError('wire_forbidden', 'Customers cannot originate wires.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise WireError('wire_forbidden', 'Staff only.')

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
            raise WireError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise WireError('credit_not_allowed', 'Credit accounts cannot originate wires.')

    def _assert_amount(self, dollars: Decimal) -> None:
        if dollars < self.policy.min_amount or dollars > self.policy.max_amount:
            raise WireError('amount_out_of_range', 'Amount is outside the allowed range.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def add_beneficiary(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        aba: Any,
        account_number: Any,
        street: Any,
        city: Any,
        state: Any,
        postal: Any,
        default_account: Any,
    ) -> WireBeneficiary:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise WireError('wire_forbidden', 'Not allowed to add beneficiaries for this customer.')
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        routing = normalize_aba(aba)
        external = normalize_external_account(account_number)
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        if self.store.find_beneficiary_by_nickname(owner_userid, name) is not None:
            raise WireError('beneficiary_duplicate', 'A beneficiary with that nickname already exists.')
        if self.store.find_beneficiary_by_fingerprint(owner_userid, routing, external) is not None:
            raise WireError('beneficiary_duplicate', 'That beneficiary account is already on file.')
        open_rows = [row for row in self.store.list_beneficiaries(owner_userid) if row.status in OPEN_BENE]
        if len(open_rows) >= self.policy.max_beneficiaries:
            raise WireError('beneficiary_limit', 'Wire beneficiary limit reached.')
        now = float(self.clock())
        row = WireBeneficiary(
            beneficiary_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            aba=routing,
            account_number=external,
            street=normalize_street(street),
            city=normalize_city(city),
            state=normalize_state(state),
            postal=normalize_postal(postal),
            default_account=account,
            status=BENE_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_beneficiary(row)
        return row

    def get_beneficiary(self, *, beneficiary_id: str, actor: str, actor_type: str) -> WireBeneficiary:
        self._require_enabled()
        row = self.store.get_beneficiary(beneficiary_id)
        if row is None:
            raise WireError('beneficiary_not_found', 'Wire beneficiary not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise WireError('wire_forbidden', 'Not allowed to view this beneficiary.')
        return row

    def enforce_beneficiary(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> WireBeneficiary:
        """Reusable gate: destination must be an active, unarchived wire beneficiary."""
        row = self.get_beneficiary(beneficiary_id=beneficiary_id, actor=actor, actor_type=actor_type)
        if row.status == BENE_ARCHIVED:
            raise WireError('already_archived', 'Beneficiary is archived.')
        if row.status == BENE_PAUSED:
            raise WireError('beneficiary_paused', 'Beneficiary is paused.')
        if require_active and row.status != BENE_ACTIVE:
            raise WireError('invalid_status', 'Beneficiary is not active.')
        return row

    def set_beneficiary_status(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> WireBeneficiary:
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
            raise WireError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == BENE_ARCHIVED:
            raise WireError('already_archived', 'Beneficiary is already archived.')
        if wanted == BENE_PAUSED:
            if row.status == BENE_PAUSED:
                raise WireError('already_paused', 'Beneficiary is already paused.')
            if row.status != BENE_ACTIVE:
                raise WireError('invalid_status', 'Only an active beneficiary can be paused.')
        elif wanted == BENE_ACTIVE:
            if row.status == BENE_ACTIVE:
                raise WireError('already_active', 'Beneficiary is already active.')
            if row.status != BENE_PAUSED:
                raise WireError('invalid_status', 'Only a paused beneficiary can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_beneficiary(row)
        return row

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        beneficiary_id: Any,
        amount: Any,
        internal_account: Any = None,
        waive_fee: bool = False,
    ) -> Dict[str, Any]:
        self._require_send(actor_type)
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise WireError('wire_forbidden', 'Beneficiary does not belong to this customer.')
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        fee = compute_fee(dollars, self.policy.outbound_fee, waived=bool(waive_fee) and actor_type in EMPLOYEE_ROLES)
        now = float(self.clock())
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        clock = self.calendar.snapshot(now)
        return {
            'amount': money_str(dollars),
            'fee': money_str(fee),
            'total': money_str(dollars + fee),
            'internal_account': account,
            'beneficiary': bene.to_dict(),
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(dollars),
            'clock': clock,
        }

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        bene: WireBeneficiary,
        account: str,
        dollars: Decimal,
        fee: Decimal,
        purpose: str,
        memo: str,
        trace_id: str,
        ofac: ScreenResult,
        waive_fee: bool,
    ) -> WireTransfer:
        now = float(self.clock())
        value = self.calendar.cycle_date(now)
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        if ofac.hit:
            status = WIRE_HELD
        elif self._needs_dual_control(dollars):
            status = WIRE_PENDING
        elif self.calendar.snapshot(now)['after_cutoff']:
            status = WIRE_QUEUED
        else:
            status = WIRE_SENT
        wire = WireTransfer(
            wire_id=uuid.uuid4().hex,
            trace_id=trace_id,
            beneficiary_id=bene.beneficiary_id,
            userid=owner_userid,
            internal_account=account,
            amount=money_str(dollars),
            fee=money_str(fee),
            fee_status=fee_status,
            nickname=bene.nickname,
            legal_name=bene.legal_name,
            aba=bene.aba,
            account_last4=last4(bene.account_number),
            purpose=purpose,
            memo=memo,
            status=status,
            imad='',
            omad='',
            value_date=value,
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
        )
        if status == WIRE_SENT:
            self._transmit(wire, actor=actor)
        return wire

    def _transmit(self, wire: WireTransfer, *, actor: str) -> WireTransfer:
        now = float(self.clock())
        cycle = wire.value_date or self.calendar.cycle_date(now)
        seq = self.store.next_imad_sequence(cycle)
        wire.imad = compose_imad(cycle, self.policy.source_id, seq)
        dollars = parse_money(wire.amount)
        fee = parse_money(wire.fee, allow_zero=True)
        remark = 'wire to %s' % wire.nickname
        status = WIRE_SENT
        fail_note = ''
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(wire.internal_account, money_str(dollars), remark)
            except Exception as exc:
                status = WIRE_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = WIRE_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = WIRE_FAILED
                    fail_note = str(result)[:240]
        if status == WIRE_SENT and fee > 0 and wire.fee_status != FEE_WAIVED and self.debit_fn is not None:
            try:
                fee_result = self.debit_fn(wire.internal_account, money_str(fee), 'wire fee %s' % wire.imad)
            except Exception:
                wire.fee_status = FEE_NSF
            else:
                kind = _classify_money_result(fee_result)
                wire.fee_status = FEE_COLLECTED if kind == 'ok' else FEE_NSF
        elif status == WIRE_SENT and (fee == 0 or wire.fee_status == FEE_WAIVED):
            wire.fee_status = FEE_WAIVED if wire.fee_status == FEE_WAIVED or fee == 0 else wire.fee_status
        wire.status = status
        wire.updated_at = now
        if status == WIRE_SENT:
            wire.sent_at = now
            wire.releaser = str(actor)
        else:
            wire.imad = ''
            wire.note = fail_note
        return wire

    def originate(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        beneficiary_id: Any,
        amount: Any,
        internal_account: Any = None,
        purpose: Any = 'other',
        memo: Any = '',
        trace_id: Any = None,
        waive_fee: bool = False,
    ) -> Tuple[WireTransfer, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise WireError('wire_forbidden', 'Not allowed to originate wires for this customer.')
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise WireError('wire_forbidden', 'Beneficiary does not belong to this customer.')
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(dollars, self.policy.outbound_fee, waived=staff_waive)
        trace = normalize_id(trace_id)
        existing = self.store.get_wire_by_trace(trace)
        if existing is not None:
            return existing, False
        if len(self.store.list_wires(owner_userid)) >= self.policy.max_wires:
            raise WireError('wire_limit', 'Wire history limit reached.')
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        wire = self._place(
            owner_userid=owner_userid,
            actor=actor,
            bene=bene,
            account=account,
            dollars=dollars,
            fee=fee,
            purpose=normalize_purpose(purpose),
            memo=normalize_note(memo, limit=140),
            trace_id=trace,
            ofac=ofac,
            waive_fee=staff_waive,
        )
        stored = self.store.put_wire(wire)
        if stored.wire_id != wire.wire_id:
            return stored, False
        if stored.status == WIRE_NSF:
            raise WireError('nsf', 'Insufficient funds for wire.', wire=stored)
        if stored.status == WIRE_FAILED:
            raise WireError('failed', 'Wire debit did not complete.', wire=stored)
        return stored, True

    def get_wire(self, *, wire_id: str, actor: str, actor_type: str) -> WireTransfer:
        self._require_enabled()
        row = self.store.get_wire(wire_id)
        if row is None:
            raise WireError('wire_not_found', 'Wire not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise WireError('wire_forbidden', 'Not allowed to view this wire.')
        return row

    def cancel_wire(
        self,
        *,
        wire_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> WireTransfer:
        self._require_send(actor_type)
        wire = self.get_wire(wire_id=wire_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and wire.userid != actor:
            raise WireError('wire_forbidden', 'Not allowed to cancel this wire.')
        if wire.status not in CANCELABLE:
            raise WireError('not_cancelable', 'Only held, queued, or pending wires can be cancelled.')
        wire.status = WIRE_CANCELLED
        wire.actor = str(actor)
        wire.updated_at = float(self.clock())
        wire.note = normalize_note(note)
        self.store.update_wire(wire)
        return wire

    def waive_fee(
        self,
        *,
        wire_id: str,
        actor: str,
        actor_type: str,
    ) -> WireTransfer:
        self._require_staff(actor_type)
        wire = self.get_wire(wire_id=wire_id, actor=actor, actor_type=actor_type)
        if wire.status not in CANCELABLE:
            raise WireError('invalid_status', 'Fee can only be waived before the wire is sent.')
        wire.fee = money_str(Decimal('0.00'))
        wire.fee_status = FEE_WAIVED
        wire.actor = str(actor)
        wire.updated_at = float(self.clock())
        self.store.update_wire(wire)
        return wire

    def override_ofac(
        self,
        *,
        wire_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> WireTransfer:
        self._require_staff(actor_type)
        wire = self.get_wire(wire_id=wire_id, actor=actor, actor_type=actor_type)
        if wire.status != WIRE_HELD:
            raise WireError('invalid_status', 'Only an OFAC hold can be overridden.')
        now = float(self.clock())
        wire.ofac_hit = 0
        wire.note = normalize_note(note) or 'ofac override'
        wire.actor = str(actor)
        wire.updated_at = now
        dollars = parse_money(wire.amount)
        if self._needs_dual_control(dollars):
            wire.status = WIRE_PENDING
        elif self.calendar.snapshot(now)['after_cutoff']:
            wire.status = WIRE_QUEUED
            wire.value_date = self.calendar.cycle_date(now)
        else:
            self._transmit(wire, actor=actor)
        self.store.update_wire(wire)
        if wire.status == WIRE_NSF:
            raise WireError('nsf', 'Insufficient funds for wire.', wire=wire)
        if wire.status == WIRE_FAILED:
            raise WireError('failed', 'Wire debit did not complete.', wire=wire)
        return wire

    def release_wire(
        self,
        *,
        wire_id: str,
        actor: str,
        actor_type: str,
    ) -> WireTransfer:
        self._require_staff(actor_type)
        wire = self.get_wire(wire_id=wire_id, actor=actor, actor_type=actor_type)
        if wire.status == WIRE_HELD:
            raise WireError('ofac_hold', 'OFAC hold must be overridden before release.')
        if wire.status not in {WIRE_PENDING, WIRE_QUEUED}:
            raise WireError('not_releasable', 'Only queued or pending wires can be released.')
        if (
            wire.status == WIRE_PENDING
            and wire.actor
            and str(actor) == str(wire.actor)
            and parse_money(wire.amount) >= self.policy.dual_control_threshold
        ):
            raise WireError('same_approver', 'A different employee must release this wire.')
        now = float(self.clock())
        if wire.status == WIRE_QUEUED or self.calendar.snapshot(now)['after_cutoff']:
            if self.calendar.snapshot(now)['after_cutoff'] and wire.status != WIRE_PENDING:
                wire.status = WIRE_QUEUED
                wire.value_date = self.calendar.cycle_date(now)
                wire.updated_at = now
                self.store.update_wire(wire)
                return wire
        self._transmit(wire, actor=actor)
        self.store.update_wire(wire)
        if wire.status == WIRE_NSF:
            raise WireError('nsf', 'Insufficient funds for wire.', wire=wire)
        if wire.status == WIRE_FAILED:
            raise WireError('failed', 'Wire debit did not complete.', wire=wire)
        return wire

    def reject_wire(
        self,
        *,
        wire_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> WireTransfer:
        self._require_staff(actor_type)
        wire = self.get_wire(wire_id=wire_id, actor=actor, actor_type=actor_type)
        if wire.status not in CANCELABLE:
            raise WireError('not_rejectable', 'Only held, queued, or pending wires can be rejected.')
        wire.status = WIRE_REJECTED
        wire.reason = normalize_note(reason, limit=40)
        wire.note = normalize_note(note)
        wire.actor = str(actor)
        wire.updated_at = float(self.clock())
        self.store.update_wire(wire)
        return wire

    def complete_wire(
        self,
        *,
        wire_id: str,
        actor: str,
        actor_type: str,
    ) -> WireTransfer:
        self._require_staff(actor_type)
        wire = self.get_wire(wire_id=wire_id, actor=actor, actor_type=actor_type)
        if wire.status == WIRE_COMPLETED:
            raise WireError('already_completed', 'Wire is already completed.')
        if wire.status != WIRE_SENT:
            raise WireError('not_completable', 'Only sent wires can be completed.')
        now = float(self.clock())
        seq = self.store.next_imad_sequence(wire.value_date)
        wire.omad = compose_omad(wire.value_date, seq)
        wire.status = WIRE_COMPLETED
        wire.completed_at = now
        wire.updated_at = now
        wire.actor = str(actor)
        self.store.update_wire(wire)
        return wire

    def recall_wire(
        self,
        *,
        wire_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> WireTransfer:
        self._require_staff(actor_type)
        wire = self.get_wire(wire_id=wire_id, actor=actor, actor_type=actor_type)
        if wire.status == WIRE_RECALLED:
            raise WireError('already_recalled', 'Wire is already recalled.')
        if wire.status == WIRE_COMPLETED:
            raise WireError('already_completed', 'Completed wires cannot be recalled.')
        if wire.status != WIRE_SENT:
            raise WireError('not_recallable', 'Only sent wires can be recalled.')
        remark = normalize_note(note) or ('wire recalled from %s' % wire.nickname)
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(wire.internal_account, wire.amount, remark)
            except Exception as exc:
                raise WireError('recall_failed', 'Recall credit failed.', wire=wire) from exc
            if _classify_money_result(result) != 'ok':
                raise WireError('recall_failed', 'Recall credit failed.', wire=wire)
            fee = parse_money(wire.fee, allow_zero=True)
            if fee > 0 and wire.fee_status == FEE_COLLECTED:
                self.credit_fn(wire.internal_account, money_str(fee), 'wire fee recalled %s' % wire.imad)
        now = float(self.clock())
        wire.status = WIRE_RECALLED
        wire.recalled_at = now
        wire.updated_at = now
        wire.actor = str(actor)
        wire.note = remark
        self.store.update_wire(wire)
        return wire

    def run_due(self, userid: Optional[str] = None) -> List[WireTransfer]:
        now = float(self.clock())
        today = self.calendar.local_dt(now).date().strftime('%Y%m%d')
        after = self.calendar.snapshot(now)['after_cutoff']
        changed: List[WireTransfer] = []
        for wire in self.store.list_wires(userid):
            if wire.status != WIRE_QUEUED:
                continue
            if wire.value_date > today:
                continue
            if after and wire.value_date == today:
                continue
            dollars = parse_money(wire.amount)
            if self._needs_dual_control(dollars):
                wire.status = WIRE_PENDING
                wire.updated_at = now
                self.store.update_wire(wire)
                changed.append(wire)
                continue
            self._transmit(wire, actor=wire.actor)
            self.store.update_wire(wire)
            changed.append(wire)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.run_due(userid)
        beneficiaries = self.store.list_beneficiaries(userid)
        wires = self.store.list_wires(userid)
        sent_ytd = Decimal('0.00')
        fee_ytd = Decimal('0.00')
        recalled = Decimal('0.00')
        for row in wires:
            amount = parse_money(row.amount, allow_zero=True)
            if row.status in {WIRE_SENT, WIRE_COMPLETED}:
                sent_ytd += amount
                if row.fee_status == FEE_COLLECTED:
                    fee_ytd += parse_money(row.fee, allow_zero=True)
            elif row.status == WIRE_RECALLED:
                recalled += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'fee': money_str(self.policy.outbound_fee),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clock': self.calendar.snapshot(now),
            'beneficiaries': [row.to_dict() for row in beneficiaries[:40]],
            'wires': [row.to_dict() for row in wires[:40]],
            'ytd_sent': money_str(sent_ytd),
            'ytd_fees': money_str(fee_ytd),
            'recalled_ytd': money_str(recalled),
            'active_count': sum(1 for row in beneficiaries if row.status == BENE_ACTIVE),
            'open_count': sum(1 for row in wires if row.status in OPEN_WIRES),
        }


_SERVICE: Optional[WireService] = None


def set_service(service: Optional[WireService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[WireService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('WIRE_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryWireStore()
    path = os.environ.get('WIRE_DB', DEFAULT_STORE_PATH)
    return SqliteWireStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[WireCalendar] = None,
) -> WireService:
    if store is None:
        store = default_store()
    return WireService(
        WirePolicy.from_env(),
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
        'beneficiary_duplicate': 409,
        'beneficiary_limit': 409,
        'wire_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_completed': 409,
        'already_recalled': 409,
        'nsf': 409,
        'failed': 409,
        'recall_failed': 409,
        'wire_forbidden': 403,
        'wire_disabled': 403,
        'beneficiary_paused': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_cancelable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_completable': 403,
        'not_recallable': 403,
        'beneficiary_not_found': 404,
        'wire_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_aba': 400,
        'invalid_address': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_imad': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_beneficiary': 400,
        'missing_wire': 400,
    }.get(code, 400)


def _error_body(exc: WireError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('wire') is not None:
        body['wire'] = exc.extra['wire'].to_dict()
    if exc.extra.get('beneficiary') is not None:
        body['beneficiary'] = exc.extra['beneficiary'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except WireError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list_wires(service: WireService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Wires': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_add_beneficiary(service: WireService):
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
            aba=values.get('aba') or values.get('routing'),
            account_number=values.get('account_number') or values.get('external_account'),
            street=values.get('street') or values.get('address'),
            city=values.get('city'),
            state=values.get('state'),
            postal=values.get('postal') or values.get('zip'),
            default_account=values.get('default_account') or values.get('account') or values.get('from_account'),
        )
        return jsonify({
            'message': 'Wire beneficiary added',
            'beneficiary': row.to_dict(),
            'Wires': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _bene_status_route(service: WireService, status: str, ok_message: str):
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
            'Wires': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: WireService):
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
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({'preview': preview, 'Wires': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: WireService):
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
        wire, created = service.originate(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            beneficiary_id=beneficiary_id,
            amount=values.get('amount'),
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({
            'message': 'Wire originated' if created else 'Wire already posted',
            'wire': wire.to_dict(),
            'Wires': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: WireService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    wire_id = str(values.get('wire_id') or '').strip()
    if not wire_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_wire'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        wire = service.cancel_wire(
            wire_id=wire_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Wire cancelled',
            'wire': wire.to_dict(),
            'Wires': service.snapshot(wire.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_wire_route(service: WireService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    wire_id = str(values.get('wire_id') or '').strip()
    if not wire_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_wire'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'release':
            wire = service.release_wire(wire_id=wire_id, actor=userid, actor_type=actor_type)
            message = 'Wire released'
        elif action == 'reject':
            wire = service.reject_wire(
                wire_id=wire_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Wire rejected'
        elif action == 'complete':
            wire = service.complete_wire(wire_id=wire_id, actor=userid, actor_type=actor_type)
            message = 'Wire completed'
        elif action == 'recall':
            wire = service.recall_wire(
                wire_id=wire_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Wire recalled'
        elif action == 'override':
            wire = service.override_ofac(
                wire_id=wire_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'waive':
            wire = service.waive_fee(wire_id=wire_id, actor=userid, actor_type=actor_type)
            message = 'Wire fee waived'
        else:
            raise WireError('invalid_status', 'Unknown wire action.')
        return jsonify({
            'message': message,
            'wire': wire.to_dict(),
            'Wires': service.snapshot(wire.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: WireService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Wires': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_wire_routes(app, service: WireService) -> None:
    @app.route('/listWires', methods=['POST', 'GET'])
    def list_wires_route():
        return handle_list_wires(service)

    @app.route('/listWireBeneficiaries', methods=['POST', 'GET'])
    def list_wire_beneficiaries_route():
        return handle_list_wires(service)

    @app.route('/addWireBeneficiary', methods=['POST', 'GET'])
    def add_wire_beneficiary_route():
        return handle_add_beneficiary(service)

    @app.route('/pauseWireBeneficiary', methods=['POST', 'GET'])
    def pause_wire_beneficiary_route():
        return _bene_status_route(service, BENE_PAUSED, 'Wire beneficiary paused')

    @app.route('/resumeWireBeneficiary', methods=['POST', 'GET'])
    def resume_wire_beneficiary_route():
        return _bene_status_route(service, BENE_ACTIVE, 'Wire beneficiary resumed')

    @app.route('/archiveWireBeneficiary', methods=['POST', 'GET'])
    def archive_wire_beneficiary_route():
        return _bene_status_route(service, BENE_ARCHIVED, 'Wire beneficiary archived')

    @app.route('/previewWire', methods=['POST', 'GET'])
    def preview_wire_route():
        return handle_preview(service)

    @app.route('/sendWire', methods=['POST', 'GET'])
    def send_wire_route():
        return handle_send(service)

    @app.route('/cancelWire', methods=['POST', 'GET'])
    def cancel_wire_route():
        return handle_cancel(service)

    @app.route('/releaseWire', methods=['POST', 'GET'])
    def release_wire_route():
        return _staff_wire_route(service, 'release')

    @app.route('/rejectWire', methods=['POST', 'GET'])
    def reject_wire_route():
        return _staff_wire_route(service, 'reject')

    @app.route('/completeWire', methods=['POST', 'GET'])
    def complete_wire_route():
        return _staff_wire_route(service, 'complete')

    @app.route('/recallWire', methods=['POST', 'GET'])
    def recall_wire_route():
        return _staff_wire_route(service, 'recall')

    @app.route('/overrideOfac', methods=['POST', 'GET'])
    def override_ofac_route():
        return _staff_wire_route(service, 'override')

    @app.route('/waiveWireFee', methods=['POST', 'GET'])
    def waive_wire_fee_route():
        return _staff_wire_route(service, 'waive')

    @app.route('/runDueWires', methods=['POST', 'GET'])
    def run_due_wires_route():
        return handle_run_due(service)
