"""CHIPS large-value clearing origination.

Customers send same-day USD payments over The Clearing House Interbank
Payments System to beneficiaries identified by a 4-digit CHIPS participant
id and a 6-digit Universal Identifier. Independent of domestic Fedwire
(PR #73), ACH linking (PR #68), ISO 20022 investigation (PR #129), and
SWIFT MT103. Existing `/fundTransfer`, `/withdrawAmount`, and `/sendWire`
stay unchanged. `Customers.debit_request` / `credit_request` still write
`debited` / `direct deposited` unless a remark is supplied here.

Foundations (reusable beyond this screen):
- CHIPS participant id (4-digit) + optional ABA-to-participant directory
- CHIPS Universal Identifier compose / normalize / mask
- CHIPS sequence `{YYYYMMDD}{send_pid}{seq}`
- Session clock with open hour AND cutoff (net settlement, not RTGS)
- CHIP1 pipe compose/parse (XML/DOCTYPE rejected)
- Per-customer net-debit cap
- Dual-control release for high-value CHIPS

Reuses Fedwire OFAC screening, money parsing, fee helper, and account
directory from `utility.wire`. Stores are pluggable (memory for tests,
sqlite WAL for restart-safe default). Destination UID/account never appear
unmasked in to_dict / snapshots.
"""

from __future__ import annotations

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
    normalize_aba,
    normalize_account,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
    us_fed_holidays,
)

BENE_ACTIVE = 'active'
BENE_PAUSED = 'paused'
BENE_ARCHIVED = 'archived'
BENE_STATUSES = frozenset({BENE_ACTIVE, BENE_PAUSED, BENE_ARCHIVED})
OPEN_BENE = frozenset({BENE_ACTIVE, BENE_PAUSED})
CHIP_HELD = 'held'
CHIP_QUEUED = 'queued'
CHIP_PENDING = 'pending_release'
CHIP_RELEASED = 'released'
CHIP_SETTLED = 'settled'
CHIP_REJECTED = 'rejected'
CHIP_CANCELLED = 'cancelled'
CHIP_RECALLED = 'recalled'
CHIP_NSF = 'nsf'
CHIP_FAILED = 'failed'
CHIP_STATUSES = frozenset({
    CHIP_HELD, CHIP_QUEUED, CHIP_PENDING, CHIP_RELEASED, CHIP_SETTLED,
    CHIP_REJECTED, CHIP_CANCELLED, CHIP_RECALLED, CHIP_NSF, CHIP_FAILED,
})
OPEN_CHIPS = frozenset({CHIP_HELD, CHIP_QUEUED, CHIP_PENDING, CHIP_RELEASED})
CANCELABLE = frozenset({CHIP_HELD, CHIP_QUEUED, CHIP_PENDING})
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
DEFAULT_PARTICIPANTS = {
    '0001': 'JPMORGAN CHASE',
    '0002': 'CITIBANK',
    '0008': 'BANK OF NEW YORK MELLON',
    '0103': 'BANK OF AMERICA',
    '0210': 'WELLS FARGO',
    '0430': 'HSBC BANK USA',
    '0509': 'GOLDMAN SACHS',
}
ABA_TO_PARTICIPANT = {
    '021000021': '0001',
    '021000089': '0002',
    '011000015': '0008',
    '026009593': '0103',
    '121000248': '0210',
    '021001033': '0430',
    '026002580': '0509',
}
SETTLEMENT_CHIPS_NET = 'chips_net'
CHIP1_VERSION = '1'
CHIP1_FIELDS = 11
DEFAULT_STORE_PATH = 'SystemLogs/chips.sqlite'


class ChipsError(ValueError):
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
        raise ChipsError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise ChipsError('invalid_name', 'Beneficiary legal name must be 2-80 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise ChipsError('invalid_purpose', 'Unknown CHIPS purpose.')
    return text


def normalize_participant(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        raise ChipsError('invalid_participant', 'CHIPS participant id is required.')
    if len(digits) < 4:
        digits = digits.zfill(4)
    if len(digits) != 4 or digits == '0000':
        raise ChipsError('invalid_participant', 'CHIPS participant id must be four digits.')
    return digits


def mask_participant(value: Any) -> str:
    pid = str(value or '').strip()
    if len(pid) < 4:
        return pid
    return '**' + pid[-2:]


def participant_name(value: Any, *, directory: Optional[Dict[str, str]] = None) -> str:
    pid = normalize_participant(value)
    names = directory if directory is not None else DEFAULT_PARTICIPANTS
    return names.get(pid, '')


def participant_from_aba(value: Any, *, mapping: Optional[Dict[str, str]] = None) -> str:
    try:
        aba = normalize_aba(value)
    except Exception as exc:
        raise ChipsError('invalid_aba', 'ABA routing number is invalid.') from exc
    table = mapping if mapping is not None else ABA_TO_PARTICIPANT
    pid = table.get(aba)
    if not pid:
        raise ChipsError('unknown_participant', 'No CHIPS participant mapped for that ABA.')
    return pid


def compose_uid(account: Any) -> str:
    """Fold an account number into a 6-digit CHIPS Universal Identifier."""
    digits = ''.join(ch for ch in str(account or '') if ch.isdigit())
    if not digits:
        raise ChipsError('invalid_uid', 'Account or UID is required.')
    if len(digits) == 6 and digits != '000000':
        return digits
    total = 0
    padded = digits.zfill(12)[-12:]
    for index, ch in enumerate(padded):
        total += int(ch) * (index + 3)
    tail = int(digits[-4:]) if len(digits) >= 4 else int(digits or '0')
    uid = (total * 97 + tail) % 1000000
    if uid == 0:
        uid = 1
    return '%06d' % uid


def normalize_uid(value: Any, *, account: Any = None) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if digits:
        if len(digits) < 6:
            digits = digits.zfill(6)
        if len(digits) != 6 or digits == '000000':
            raise ChipsError('invalid_uid', 'CHIPS UID must be six digits.')
        return digits
    if account not in (None, ''):
        return compose_uid(account)
    raise ChipsError('invalid_uid', 'CHIPS UID or external account is required.')


def mask_uid(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 3:
        return '***'
    return '***' + digits[-3:]


def compose_chips_seq(cycle_date: str, send_pid: str, sequence: int) -> str:
    """CHIPS sequence: {YYYYMMDD}{4-digit send PID}{6-digit seq}."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise ChipsError('invalid_sequence', 'Cycle date must be YYYYMMDD.')
    pid = normalize_participant(send_pid)
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise ChipsError('invalid_sequence', 'CHIPS sequence out of range.')
    return '%s%s%06d' % (day, pid, seq)


def normalize_chips_seq(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 18:
        raise ChipsError('invalid_sequence', 'CHIPS sequence must be 18 digits.')
    compose_chips_seq(digits[:8], digits[8:12], int(digits[12:]))
    return digits


def mask_chips_seq(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 8:
        return digits
    return digits[:8] + '***' + digits[-4:]


def compose_amount_field(amount: Decimal) -> str:
    cents = int((amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN) * 100).to_integral_value())
    if cents < 0 or cents > 999999999999:
        raise ChipsError('invalid_amount', 'Amount cannot be encoded in CHIP1.')
    return '%012d' % cents


def parse_amount_field(value: Any) -> Decimal:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits or len(digits) > 12:
        raise ChipsError('invalid_amount', 'CHIP1 amount field is invalid.')
    return (Decimal(digits) / Decimal('100')).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def _reject_markup(text: str) -> None:
    lowered = text.lower()
    if '<?xml' in lowered or '<!doctype' in lowered or '<chip' in lowered or '<pacs' in lowered:
        raise ChipsError('invalid_file', 'XML/DOCTYPE CHIP1 files are rejected.')


def compose_chip1(
    *,
    sequence: str,
    value_date: str,
    send_pid: str,
    recv_pid: str,
    uid: str,
    amount: Decimal,
    name: str,
    purpose: str = 'other',
    mask: bool = True,
) -> str:
    seq = normalize_chips_seq(sequence)
    day = str(value_date or seq[:8]).strip()
    if len(day) == 10 and day[4] == '-':
        day = day.replace('-', '')
    if not (len(day) == 8 and day.isdigit()):
        raise ChipsError('invalid_date', 'Value date must be YYYYMMDD.')
    uid_out = mask_uid(uid) if mask else normalize_uid(uid)
    name_out = re.sub(r'\|', ' ', str(name or '').strip())[:80] or 'UNKNOWN'
    return '|'.join((
        'CHIP1',
        CHIP1_VERSION,
        mask_chips_seq(seq) if mask else seq,
        day,
        normalize_participant(send_pid),
        normalize_participant(recv_pid),
        uid_out,
        compose_amount_field(amount),
        name_out,
        normalize_purpose(purpose),
        SETTLEMENT_CHIPS_NET,
    ))


def parse_chip1(text: Any) -> Dict[str, Any]:
    raw = str(text or '').strip()
    if not raw:
        raise ChipsError('invalid_file', 'CHIP1 record is empty.')
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != CHIP1_FIELDS:
        raise ChipsError('invalid_file', 'CHIP1 record must have %d fields.' % CHIP1_FIELDS)
    if parts[0] != 'CHIP1' or parts[1] != CHIP1_VERSION:
        raise ChipsError('invalid_file', 'CHIP1 version marker is invalid.')
    if parts[10] != SETTLEMENT_CHIPS_NET:
        raise ChipsError('invalid_file', 'CHIP1 settlement method must be chips_net.')
    seq = parts[2]
    if '***' in seq:
        raise ChipsError('invalid_file', 'Masked CHIP1 sequence cannot be re-ingested.')
    amount = parse_amount_field(parts[7])
    return {
        'sequence': normalize_chips_seq(seq),
        'value_date': parts[3],
        'send_pid': normalize_participant(parts[4]),
        'recv_pid': normalize_participant(parts[5]),
        'uid': normalize_uid(parts[6]),
        'amount': money_str(amount),
        'name': parts[8].strip(),
        'purpose': normalize_purpose(parts[9]),
        'settlement_method': parts[10],
    }


def split_chip1_file(text: Any) -> List[str]:
    raw = str(text or '')
    _reject_markup(raw)
    rows = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            rows.append(stripped)
    if not rows:
        raise ChipsError('invalid_file', 'CHIP1 file is empty.')
    return rows


class ChipsCalendar:
    """CHIPS business-day clock with open hour and cutoff. Net session, not RTGS."""

    def __init__(
        self,
        *,
        open_hour: int = 9,
        open_minute: int = 0,
        cutoff_hour: int = 17,
        cutoff_minute: int = 0,
        tz_offset_hours: int = -4,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.open_hour = int(open_hour)
        self.open_minute = int(open_minute)
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

    def is_before_open(self, ts: float) -> bool:
        local = self.local_dt(ts)
        return (local.hour, local.minute) < (self.open_hour, self.open_minute)

    def is_after_cutoff(self, ts: float) -> bool:
        local = self.local_dt(ts)
        return (local.hour, local.minute) >= (self.cutoff_hour, self.cutoff_minute)

    def is_session_open(self, ts: float) -> bool:
        local = self.local_dt(ts)
        if not self.is_business_day(local.date()):
            return False
        return not self.is_before_open(ts) and not self.is_after_cutoff(ts)

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
        session_open = self.is_session_open(ts)
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'open': '%02d:%02d' % (self.open_hour, self.open_minute),
            'cutoff': '%02d:%02d' % (self.cutoff_hour, self.cutoff_minute),
            'before_open': self.is_before_open(ts) and self.is_business_day(local.date()),
            'after_cutoff': self.is_after_cutoff(ts) or not self.is_business_day(local.date()),
            'session_open': session_open,
            'business_day': self.is_business_day(local.date()),
            'value_date': value.isoformat(),
            'cycle_date': value.strftime('%Y%m%d'),
            'settlement_method': SETTLEMENT_CHIPS_NET,
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
class ChipsPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    allow_credit: bool = False
    max_beneficiaries: int = 12
    max_payments: int = 120
    min_amount: Decimal = Decimal('100.00')
    max_amount: Decimal = Decimal('100000000.00')
    outbound_fee: Decimal = Decimal('8.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    net_debit_cap: Decimal = Decimal('5000000.00')
    open_hour: int = 9
    cutoff_hour: int = 17
    tz_offset_hours: int = -4
    source_id: str = '0001'
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()
    participants: Dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.participants is None:
            self.participants = dict(DEFAULT_PARTICIPANTS)

    @classmethod
    def from_env(cls) -> 'ChipsPolicy':
        watch = tuple(DEFAULT_WATCHLIST) + _env_list('CHIPS_OFAC_LIST')
        extra_pids = _env_list('CHIPS_PARTICIPANTS')
        directory = dict(DEFAULT_PARTICIPANTS)
        for item in extra_pids:
            if ':' in item:
                pid, name = item.split(':', 1)
                try:
                    directory[normalize_participant(pid)] = name.strip().upper()[:80]
                except ChipsError:
                    continue
        source = os.environ.get('CHIPS_SOURCE', '0001')
        try:
            source_id = normalize_participant(source)
        except ChipsError:
            source_id = '0001'
        return cls(
            enabled=_env_bool('CHIPS_ENABLED', True),
            customer_manage=_env_bool('CHIPS_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('CHIPS_CUSTOMER_SEND', True),
            allow_credit=_env_bool('CHIPS_ALLOW_CREDIT', False),
            max_beneficiaries=_env_int('CHIPS_MAX_BENEFICIARIES', 12),
            max_payments=_env_int('CHIPS_MAX_PAYMENTS', 120),
            min_amount=_env_money('CHIPS_MIN_AMOUNT', '100.00'),
            max_amount=_env_money('CHIPS_MAX_AMOUNT', '100000000.00'),
            outbound_fee=_env_money('CHIPS_FEE', '8.00'),
            dual_control_threshold=_env_money('CHIPS_DUAL_CONTROL', '10000.00'),
            net_debit_cap=_env_money('CHIPS_NET_DEBIT_CAP', '5000000.00'),
            open_hour=_env_int('CHIPS_OPEN_HOUR', 9),
            cutoff_hour=_env_int('CHIPS_CUTOFF_HOUR', 17),
            tz_offset_hours=_env_int('CHIPS_TZ_OFFSET', -4),
            source_id=source_id,
            watchlist=watch,
            extra_holidays=_env_list('CHIPS_HOLIDAYS'),
            participants=directory,
        )


@dataclass
class ChipsBeneficiary:
    beneficiary_id: str
    userid: str
    nickname: str
    legal_name: str
    participant: str
    uid: str
    account_number: str
    default_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float
    participant_label: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'beneficiary_id': self.beneficiary_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'participant': self.participant,
            'participant_name': self.participant_label,
            'uid_masked': mask_uid(self.uid),
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
class ChipsPayment:
    payment_id: str
    trace_id: str
    beneficiary_id: str
    userid: str
    internal_account: str
    amount: str
    fee: str
    fee_status: str
    nickname: str
    legal_name: str
    participant: str
    uid_masked: str
    purpose: str
    memo: str
    status: str
    chips_seq: str
    settlement_id: str
    value_date: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    created_at: float
    updated_at: float
    released_at: float = 0.0
    settled_at: float = 0.0
    recalled_at: float = 0.0
    note: str = ''
    reason: str = ''
    settlement_method: str = SETTLEMENT_CHIPS_NET

    def to_dict(self) -> Dict[str, Any]:
        return {
            'payment_id': self.payment_id,
            'trace_id': self.trace_id,
            'beneficiary_id': self.beneficiary_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'amount': self.amount,
            'fee': self.fee,
            'fee_status': self.fee_status,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'participant': self.participant,
            'uid_masked': self.uid_masked,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'chips_seq_masked': mask_chips_seq(self.chips_seq) if self.chips_seq else '',
            'settlement_id_masked': mask_chips_seq(self.settlement_id) if self.settlement_id else '',
            'value_date': self.value_date,
            'settlement_method': self.settlement_method,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'released_at': self.released_at,
            'settled_at': self.settled_at,
            'recalled_at': self.recalled_at,
            'note': self.note,
            'reason': self.reason,
            'held': self.status == CHIP_HELD,
            'queued': self.status == CHIP_QUEUED,
            'pending_release': self.status == CHIP_PENDING,
            'released': self.status == CHIP_RELEASED,
            'settled': self.status == CHIP_SETTLED,
            'cancelable': self.status in CANCELABLE,
        }


def _clone_bene(row: ChipsBeneficiary) -> ChipsBeneficiary:
    return ChipsBeneficiary(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_pay(row: ChipsPayment) -> ChipsPayment:
    return ChipsPayment(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _bene_from_row(row: Any) -> ChipsBeneficiary:
    return ChipsBeneficiary(
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        participant=row['participant'],
        uid=row['uid'],
        account_number=row['account_number'] or '',
        default_account=row['default_account'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        participant_label=row['participant_label'] or '',
    )


def _pay_from_row(row: Any) -> ChipsPayment:
    return ChipsPayment(
        payment_id=row['payment_id'],
        trace_id=row['trace_id'],
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        amount=row['amount'],
        fee=row['fee'],
        fee_status=row['fee_status'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        participant=row['participant'],
        uid_masked=row['uid_masked'],
        purpose=row['purpose'],
        memo=row['memo'] or '',
        status=row['status'],
        chips_seq=row['chips_seq'] or '',
        settlement_id=row['settlement_id'] or '',
        value_date=row['value_date'],
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        released_at=float(row['released_at'] or 0),
        settled_at=float(row['settled_at'] or 0),
        recalled_at=float(row['recalled_at'] or 0),
        note=row['note'] or '',
        reason=row['reason'] or '',
        settlement_method=row['settlement_method'] or SETTLEMENT_CHIPS_NET,
    )


class MemoryChipsStore:
    def __init__(self) -> None:
        self._benes: Dict[str, ChipsBeneficiary] = {}
        self._pays: Dict[str, ChipsPayment] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_beneficiary(self, row: ChipsBeneficiary) -> None:
        with self._lock:
            self._benes[row.beneficiary_id] = row

    def get_beneficiary(self, beneficiary_id: str) -> Optional[ChipsBeneficiary]:
        with self._lock:
            row = self._benes.get(beneficiary_id)
            return _clone_bene(row) if row else None

    def update_beneficiary(self, row: ChipsBeneficiary) -> None:
        with self._lock:
            self._benes[row.beneficiary_id] = row

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[ChipsBeneficiary]:
        with self._lock:
            rows = [_clone_bene(row) for row in self._benes.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != BENE_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[ChipsBeneficiary]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._benes.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_BENE:
                    return _clone_bene(row)
        return None

    def find_beneficiary_by_fingerprint(self, userid: str, participant: str, uid: str) -> Optional[ChipsBeneficiary]:
        with self._lock:
            for row in self._benes.values():
                if (
                    row.userid == userid
                    and row.participant == participant
                    and row.uid == uid
                    and row.status in OPEN_BENE
                ):
                    return _clone_bene(row)
        return None

    def put_payment(self, row: ChipsPayment) -> ChipsPayment:
        with self._lock:
            existing_id = self._by_trace.get(row.trace_id)
            if existing_id is not None:
                return self._pays[existing_id]
            self._pays[row.payment_id] = row
            self._by_trace[row.trace_id] = row.payment_id
            return row

    def update_payment(self, row: ChipsPayment) -> None:
        with self._lock:
            self._pays[row.payment_id] = row

    def get_payment(self, payment_id: str) -> Optional[ChipsPayment]:
        with self._lock:
            row = self._pays.get(payment_id)
            return _clone_pay(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[ChipsPayment]:
        with self._lock:
            payment_id = self._by_trace.get(trace_id)
            return _clone_pay(self._pays[payment_id]) if payment_id else None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[ChipsPayment]:
        with self._lock:
            rows = [_clone_pay(row) for row in self._pays.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if beneficiary_id is not None:
            rows = [row for row in rows if row.beneficiary_id == beneficiary_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def next_sequence(self, cycle_date: str) -> int:
        with self._lock:
            used = [row.chips_seq for row in self._pays.values() if row.chips_seq.startswith(cycle_date)]
        return len(used) + 1


class SqliteChipsStore:
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
                    participant TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    account_number TEXT NOT NULL DEFAULT '',
                    default_account TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    participant_label TEXT NOT NULL DEFAULT ''
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
                    amount TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    fee_status TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    participant TEXT NOT NULL,
                    uid_masked TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    chips_seq TEXT NOT NULL DEFAULT '',
                    settlement_id TEXT NOT NULL DEFAULT '',
                    value_date TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    released_at REAL NOT NULL DEFAULT 0,
                    settled_at REAL NOT NULL DEFAULT 0,
                    recalled_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    settlement_method TEXT NOT NULL DEFAULT 'chips_net'
                )
                """
            )
            conn.commit()

    def put_beneficiary(self, row: ChipsBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO beneficiaries (
                    beneficiary_id, userid, nickname, legal_name, participant, uid,
                    account_number, default_account, status, actor, created_at,
                    updated_at, participant_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.beneficiary_id, row.userid, row.nickname, row.legal_name,
                    row.participant, row.uid, row.account_number, row.default_account,
                    row.status, row.actor, row.created_at, row.updated_at,
                    row.participant_label,
                ),
            )
            conn.commit()

    def get_beneficiary(self, beneficiary_id: str) -> Optional[ChipsBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM beneficiaries WHERE beneficiary_id = ?', (beneficiary_id,),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def update_beneficiary(self, row: ChipsBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE beneficiaries SET nickname=?, legal_name=?, participant=?, uid=?,
                    account_number=?, default_account=?, status=?, actor=?, updated_at=?,
                    participant_label=?
                WHERE beneficiary_id=?
                """,
                (
                    row.nickname, row.legal_name, row.participant, row.uid, row.account_number,
                    row.default_account, row.status, row.actor, row.updated_at,
                    row.participant_label, row.beneficiary_id,
                ),
            )
            conn.commit()

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[ChipsBeneficiary]:
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

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[ChipsBeneficiary]:
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

    def find_beneficiary_by_fingerprint(self, userid: str, participant: str, uid: str) -> Optional[ChipsBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM beneficiaries
                WHERE userid = ? AND participant = ? AND uid = ?
                  AND status IN ('active', 'paused')
                """,
                (userid, participant, uid),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def put_payment(self, row: ChipsPayment) -> ChipsPayment:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM payments WHERE trace_id = ?', (row.trace_id,)
            ).fetchone()
            if existing is not None:
                return _pay_from_row(existing)
            conn.execute(
                """
                INSERT INTO payments (
                    payment_id, trace_id, beneficiary_id, userid, internal_account,
                    amount, fee, fee_status, nickname, legal_name, participant, uid_masked,
                    purpose, memo, status, chips_seq, settlement_id, value_date, actor,
                    releaser, ofac_hit, ofac_match, created_at, updated_at, released_at,
                    settled_at, recalled_at, note, reason, settlement_method
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.payment_id, row.trace_id, row.beneficiary_id, row.userid,
                    row.internal_account, row.amount, row.fee, row.fee_status,
                    row.nickname, row.legal_name, row.participant, row.uid_masked,
                    row.purpose, row.memo, row.status, row.chips_seq, row.settlement_id,
                    row.value_date, row.actor, row.releaser, row.ofac_hit, row.ofac_match,
                    row.created_at, row.updated_at, row.released_at, row.settled_at,
                    row.recalled_at, row.note, row.reason, row.settlement_method,
                ),
            )
            conn.commit()
            return row

    def update_payment(self, row: ChipsPayment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE payments SET fee=?, fee_status=?, status=?, chips_seq=?,
                    settlement_id=?, value_date=?, actor=?, releaser=?, ofac_hit=?,
                    ofac_match=?, updated_at=?, released_at=?, settled_at=?,
                    recalled_at=?, note=?, reason=?
                WHERE payment_id=?
                """,
                (
                    row.fee, row.fee_status, row.status, row.chips_seq, row.settlement_id,
                    row.value_date, row.actor, row.releaser, row.ofac_hit, row.ofac_match,
                    row.updated_at, row.released_at, row.settled_at, row.recalled_at,
                    row.note, row.reason, row.payment_id,
                ),
            )
            conn.commit()

    def get_payment(self, payment_id: str) -> Optional[ChipsPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE payment_id = ?', (payment_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[ChipsPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE trace_id = ?', (trace_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[ChipsPayment]:
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

    def next_sequence(self, cycle_date: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE chips_seq LIKE ?",
                (cycle_date + '%',),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class ChipsService:
    def __init__(
        self,
        policy: ChipsPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[ChipsCalendar] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or ChipsCalendar(
            open_hour=policy.open_hour,
            cutoff_hour=policy.cutoff_hour,
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise ChipsError('chips_disabled', 'CHIPS origination is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise ChipsError('chips_forbidden', 'Customers cannot manage CHIPS beneficiaries.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise ChipsError('chips_forbidden', 'Customers cannot originate CHIPS payments.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise ChipsError('chips_forbidden', 'Staff only.')

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
            raise ChipsError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise ChipsError('credit_not_allowed', 'Credit accounts cannot originate CHIPS payments.')

    def _assert_amount(self, dollars: Decimal) -> None:
        if dollars < self.policy.min_amount or dollars > self.policy.max_amount:
            raise ChipsError('amount_out_of_range', 'Amount is outside the allowed CHIPS range.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def _net_debit(self, userid: str, cycle_date: str) -> Decimal:
        total = Decimal('0.00')
        for row in self.store.list_payments(userid):
            if row.value_date == cycle_date and row.status == CHIP_RELEASED:
                total += parse_money(row.amount, allow_zero=True)
        return total

    def _assert_net_debit(self, userid: str, cycle_date: str, amount: Decimal) -> None:
        outstanding = self._net_debit(userid, cycle_date)
        if outstanding + amount > self.policy.net_debit_cap:
            raise ChipsError('net_debit_cap', 'CHIPS net-debit cap would be exceeded.')

    def add_beneficiary(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        participant: Any,
        uid: Any = None,
        account_number: Any = None,
        default_account: Any,
        aba: Any = None,
    ) -> ChipsBeneficiary:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise ChipsError('chips_forbidden', 'Not allowed to add beneficiaries for this customer.')
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        if participant in (None, '') and aba not in (None, ''):
            pid = participant_from_aba(aba)
        else:
            pid = normalize_participant(participant)
        chips_uid = normalize_uid(uid, account=account_number)
        external = ''
        if account_number not in (None, ''):
            digits = ''.join(ch for ch in str(account_number) if ch.isdigit())
            if not (4 <= len(digits) <= 17):
                raise ChipsError('invalid_account', 'External account must be 4-17 digits.')
            external = digits
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        if self.store.find_beneficiary_by_nickname(owner_userid, name) is not None:
            raise ChipsError('beneficiary_duplicate', 'A beneficiary with that nickname already exists.')
        if self.store.find_beneficiary_by_fingerprint(owner_userid, pid, chips_uid) is not None:
            raise ChipsError('beneficiary_duplicate', 'That CHIPS UID is already on file.')
        open_rows = [row for row in self.store.list_beneficiaries(owner_userid) if row.status in OPEN_BENE]
        if len(open_rows) >= self.policy.max_beneficiaries:
            raise ChipsError('beneficiary_limit', 'CHIPS beneficiary limit reached.')
        now = float(self.clock())
        row = ChipsBeneficiary(
            beneficiary_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            participant=pid,
            uid=chips_uid,
            account_number=external,
            default_account=account,
            status=BENE_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
            participant_label=self.policy.participants.get(pid, ''),
        )
        self.store.put_beneficiary(row)
        return row

    def get_beneficiary(self, *, beneficiary_id: str, actor: str, actor_type: str) -> ChipsBeneficiary:
        self._require_enabled()
        row = self.store.get_beneficiary(beneficiary_id)
        if row is None:
            raise ChipsError('beneficiary_not_found', 'CHIPS beneficiary not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise ChipsError('chips_forbidden', 'Not allowed to view this beneficiary.')
        return row

    def enforce_beneficiary(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> ChipsBeneficiary:
        row = self.get_beneficiary(beneficiary_id=beneficiary_id, actor=actor, actor_type=actor_type)
        if row.status == BENE_ARCHIVED:
            raise ChipsError('already_archived', 'Beneficiary is archived.')
        if row.status == BENE_PAUSED:
            raise ChipsError('beneficiary_paused', 'Beneficiary is paused.')
        if require_active and row.status != BENE_ACTIVE:
            raise ChipsError('invalid_status', 'Beneficiary is not active.')
        return row

    def set_beneficiary_status(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> ChipsBeneficiary:
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
            raise ChipsError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == BENE_ARCHIVED:
            raise ChipsError('already_archived', 'Beneficiary is already archived.')
        if wanted == BENE_PAUSED:
            if row.status == BENE_PAUSED:
                raise ChipsError('already_paused', 'Beneficiary is already paused.')
            if row.status != BENE_ACTIVE:
                raise ChipsError('invalid_status', 'Only an active beneficiary can be paused.')
        elif wanted == BENE_ACTIVE:
            if row.status == BENE_ACTIVE:
                raise ChipsError('already_active', 'Beneficiary is already active.')
            if row.status != BENE_PAUSED:
                raise ChipsError('invalid_status', 'Only a paused beneficiary can be resumed.')
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
            raise ChipsError('chips_forbidden', 'Beneficiary does not belong to this customer.')
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        fee = compute_fee(dollars, self.policy.outbound_fee, waived=bool(waive_fee) and actor_type in EMPLOYEE_ROLES)
        now = float(self.clock())
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        clock = self.calendar.snapshot(now)
        net = self._net_debit(owner_userid, clock['cycle_date'])
        return {
            'amount': money_str(dollars),
            'fee': money_str(fee),
            'total': money_str(dollars + fee),
            'internal_account': account,
            'beneficiary': bene.to_dict(),
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(dollars),
            'clock': clock,
            'net_debit': money_str(net),
            'net_debit_cap': money_str(self.policy.net_debit_cap),
            'net_headroom': money_str(self.policy.net_debit_cap - net),
            'settlement_method': SETTLEMENT_CHIPS_NET,
        }

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        bene: ChipsBeneficiary,
        account: str,
        dollars: Decimal,
        fee: Decimal,
        purpose: str,
        memo: str,
        trace_id: str,
        ofac: ScreenResult,
        waive_fee: bool,
    ) -> ChipsPayment:
        now = float(self.clock())
        value = self.calendar.cycle_date(now)
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        clock = self.calendar.snapshot(now)
        if ofac.hit:
            status = CHIP_HELD
        elif self._needs_dual_control(dollars):
            status = CHIP_PENDING
        elif not clock['session_open']:
            status = CHIP_QUEUED
        else:
            status = CHIP_RELEASED
        payment = ChipsPayment(
            payment_id=uuid.uuid4().hex,
            trace_id=trace_id,
            beneficiary_id=bene.beneficiary_id,
            userid=owner_userid,
            internal_account=account,
            amount=money_str(dollars),
            fee=money_str(fee),
            fee_status=fee_status,
            nickname=bene.nickname,
            legal_name=bene.legal_name,
            participant=bene.participant,
            uid_masked=mask_uid(bene.uid),
            purpose=purpose,
            memo=memo,
            status=status,
            chips_seq='',
            settlement_id='',
            value_date=value,
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
        )
        if status == CHIP_RELEASED:
            self._transmit(payment, actor=actor, bene=bene)
        return payment

    def _bene_uid(self, payment: ChipsPayment) -> str:
        bene = self.store.get_beneficiary(payment.beneficiary_id)
        return bene.uid if bene is not None else '000001'

    def _transmit(self, payment: ChipsPayment, *, actor: str, bene: Optional[ChipsBeneficiary] = None) -> ChipsPayment:
        now = float(self.clock())
        cycle = payment.value_date or self.calendar.cycle_date(now)
        dollars = parse_money(payment.amount)
        self._assert_net_debit(payment.userid, cycle, dollars)
        seq = self.store.next_sequence(cycle)
        payment.chips_seq = compose_chips_seq(cycle, self.policy.source_id, seq)
        fee = parse_money(payment.fee, allow_zero=True)
        remark = 'chips to %s' % payment.nickname
        status = CHIP_RELEASED
        fail_note = ''
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(payment.internal_account, money_str(dollars), remark)
            except Exception as exc:
                status = CHIP_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = CHIP_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = CHIP_FAILED
                    fail_note = str(result)[:240]
        if status == CHIP_RELEASED and fee > 0 and payment.fee_status != FEE_WAIVED and self.debit_fn is not None:
            try:
                fee_result = self.debit_fn(
                    payment.internal_account, money_str(fee), 'chips fee %s' % mask_chips_seq(payment.chips_seq),
                )
            except Exception:
                payment.fee_status = FEE_NSF
            else:
                kind = _classify_money_result(fee_result)
                payment.fee_status = FEE_COLLECTED if kind == 'ok' else FEE_NSF
        elif status == CHIP_RELEASED and (fee == 0 or payment.fee_status == FEE_WAIVED):
            payment.fee_status = FEE_WAIVED if payment.fee_status == FEE_WAIVED or fee == 0 else payment.fee_status
        payment.status = status
        payment.updated_at = now
        if status == CHIP_RELEASED:
            payment.released_at = now
            payment.releaser = str(actor)
        else:
            payment.chips_seq = ''
            payment.note = fail_note
        _ = bene
        return payment

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
    ) -> Tuple[ChipsPayment, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise ChipsError('chips_forbidden', 'Not allowed to originate CHIPS for this customer.')
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise ChipsError('chips_forbidden', 'Beneficiary does not belong to this customer.')
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(dollars, self.policy.outbound_fee, waived=staff_waive)
        trace = normalize_id(trace_id)
        existing = self.store.get_payment_by_trace(trace)
        if existing is not None:
            return existing, False
        if len(self.store.list_payments(owner_userid)) >= self.policy.max_payments:
            raise ChipsError('payment_limit', 'CHIPS history limit reached.')
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        payment = self._place(
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
        stored = self.store.put_payment(payment)
        if stored.payment_id != payment.payment_id:
            return stored, False
        if stored.status == CHIP_NSF:
            raise ChipsError('nsf', 'Insufficient funds for CHIPS.', payment=stored)
        if stored.status == CHIP_FAILED:
            raise ChipsError('failed', 'CHIPS debit did not complete.', payment=stored)
        return stored, True

    def get_payment(self, *, payment_id: str, actor: str, actor_type: str) -> ChipsPayment:
        self._require_enabled()
        row = self.store.get_payment(payment_id)
        if row is None:
            raise ChipsError('payment_not_found', 'CHIPS payment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise ChipsError('chips_forbidden', 'Not allowed to view this payment.')
        return row

    def cancel_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> ChipsPayment:
        self._require_send(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and payment.userid != actor:
            raise ChipsError('chips_forbidden', 'Not allowed to cancel this payment.')
        if payment.status not in CANCELABLE:
            raise ChipsError('not_cancelable', 'Only held, queued, or pending CHIPS can be cancelled.')
        payment.status = CHIP_CANCELLED
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        payment.note = normalize_note(note)
        self.store.update_payment(payment)
        return payment

    def waive_fee(self, *, payment_id: str, actor: str, actor_type: str) -> ChipsPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status not in CANCELABLE:
            raise ChipsError('invalid_status', 'Fee can only be waived before the payment is released.')
        payment.fee = money_str(Decimal('0.00'))
        payment.fee_status = FEE_WAIVED
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        self.store.update_payment(payment)
        return payment

    def override_ofac(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> ChipsPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status != CHIP_HELD:
            raise ChipsError('not_overridable', 'Only an OFAC hold can be overridden.')
        now = float(self.clock())
        payment.ofac_hit = 0
        payment.note = normalize_note(note) or 'ofac override'
        payment.actor = str(actor)
        payment.updated_at = now
        dollars = parse_money(payment.amount)
        clock = self.calendar.snapshot(now)
        if self._needs_dual_control(dollars):
            payment.status = CHIP_PENDING
        elif not clock['session_open']:
            payment.status = CHIP_QUEUED
            payment.value_date = self.calendar.cycle_date(now)
        else:
            self._transmit(payment, actor=actor)
        self.store.update_payment(payment)
        if payment.status == CHIP_NSF:
            raise ChipsError('nsf', 'Insufficient funds for CHIPS.', payment=payment)
        if payment.status == CHIP_FAILED:
            raise ChipsError('failed', 'CHIPS debit did not complete.', payment=payment)
        return payment

    def release_payment(self, *, payment_id: str, actor: str, actor_type: str) -> ChipsPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == CHIP_HELD:
            raise ChipsError('ofac_hold', 'OFAC hold must be overridden before release.')
        if payment.status not in {CHIP_PENDING, CHIP_QUEUED}:
            raise ChipsError('not_releasable', 'Only queued or pending CHIPS can be released.')
        if (
            payment.status == CHIP_PENDING
            and payment.actor
            and str(actor) == str(payment.actor)
            and parse_money(payment.amount) >= self.policy.dual_control_threshold
        ):
            raise ChipsError('same_approver', 'A different employee must release this CHIPS payment.')
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        if not clock['session_open']:
            payment.status = CHIP_QUEUED
            payment.value_date = self.calendar.cycle_date(now)
            payment.updated_at = now
            self.store.update_payment(payment)
            return payment
        self._transmit(payment, actor=actor)
        self.store.update_payment(payment)
        if payment.status == CHIP_NSF:
            raise ChipsError('nsf', 'Insufficient funds for CHIPS.', payment=payment)
        if payment.status == CHIP_FAILED:
            raise ChipsError('failed', 'CHIPS debit did not complete.', payment=payment)
        return payment

    def reject_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> ChipsPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status not in CANCELABLE:
            raise ChipsError('not_rejectable', 'Only held, queued, or pending CHIPS can be rejected.')
        payment.status = CHIP_REJECTED
        payment.reason = normalize_note(reason, limit=40)
        payment.note = normalize_note(note)
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        self.store.update_payment(payment)
        return payment

    def settle_payment(self, *, payment_id: str, actor: str, actor_type: str) -> ChipsPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == CHIP_SETTLED:
            raise ChipsError('already_settled', 'CHIPS payment is already settled.')
        if payment.status != CHIP_RELEASED:
            raise ChipsError('not_settleable', 'Only released CHIPS payments can be net-settled.')
        now = float(self.clock())
        seq = self.store.next_sequence(payment.value_date)
        payment.settlement_id = compose_chips_seq(payment.value_date, self.policy.source_id, seq)
        payment.status = CHIP_SETTLED
        payment.settled_at = now
        payment.updated_at = now
        payment.actor = str(actor)
        self.store.update_payment(payment)
        return payment

    def recall_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> ChipsPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == CHIP_RECALLED:
            raise ChipsError('already_recalled', 'CHIPS payment is already recalled.')
        if payment.status == CHIP_SETTLED:
            raise ChipsError('scheme_irrevocable', 'Settled CHIPS payments cannot be recalled.')
        if payment.status != CHIP_RELEASED:
            raise ChipsError('not_recallable', 'Only released CHIPS payments can be recalled.')
        remark = normalize_note(note) or ('chips recalled from %s' % payment.nickname)
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(payment.internal_account, payment.amount, remark)
            except Exception as exc:
                raise ChipsError('recall_failed', 'Recall credit failed.', payment=payment) from exc
            if _classify_money_result(result) != 'ok':
                raise ChipsError('recall_failed', 'Recall credit failed.', payment=payment)
            fee = parse_money(payment.fee, allow_zero=True)
            if fee > 0 and payment.fee_status == FEE_COLLECTED:
                self.credit_fn(
                    payment.internal_account, money_str(fee),
                    'chips fee recalled %s' % mask_chips_seq(payment.chips_seq),
                )
        now = float(self.clock())
        payment.status = CHIP_RECALLED
        payment.recalled_at = now
        payment.updated_at = now
        payment.actor = str(actor)
        payment.note = remark
        self.store.update_payment(payment)
        return payment

    def export_chip1(self, *, payment_id: str, actor: str, actor_type: str) -> Dict[str, Any]:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status not in {CHIP_RELEASED, CHIP_SETTLED}:
            raise ChipsError('not_exportable', 'Only released or settled CHIPS can be exported.')
        if not payment.chips_seq:
            raise ChipsError('not_exportable', 'CHIPS sequence has not been assigned.')
        record = compose_chip1(
            sequence=payment.chips_seq,
            value_date=payment.value_date,
            send_pid=self.policy.source_id,
            recv_pid=payment.participant,
            uid=self._bene_uid(payment),
            amount=parse_money(payment.amount),
            name=payment.legal_name,
            purpose=payment.purpose,
            mask=True,
        )
        return {
            'payment_id': payment.payment_id,
            'chip1': record,
            'chips_seq_masked': mask_chips_seq(payment.chips_seq),
        }

    def run_due(self, userid: Optional[str] = None) -> List[ChipsPayment]:
        now = float(self.clock())
        today = self.calendar.local_dt(now).date().strftime('%Y%m%d')
        clock = self.calendar.snapshot(now)
        changed: List[ChipsPayment] = []
        for payment in self.store.list_payments(userid):
            if payment.status == CHIP_QUEUED:
                if payment.value_date > today:
                    continue
                if not clock['session_open']:
                    continue
                dollars = parse_money(payment.amount)
                if self._needs_dual_control(dollars):
                    payment.status = CHIP_PENDING
                    payment.updated_at = now
                    self.store.update_payment(payment)
                    changed.append(payment)
                    continue
                self._transmit(payment, actor=payment.actor)
                self.store.update_payment(payment)
                changed.append(payment)
            elif payment.status == CHIP_RELEASED and (clock['after_cutoff'] or not clock['business_day']):
                if payment.value_date > today:
                    continue
                seq = self.store.next_sequence(payment.value_date)
                payment.settlement_id = compose_chips_seq(payment.value_date, self.policy.source_id, seq)
                payment.status = CHIP_SETTLED
                payment.settled_at = now
                payment.updated_at = now
                self.store.update_payment(payment)
                changed.append(payment)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.run_due(userid)
        beneficiaries = self.store.list_beneficiaries(userid)
        payments = self.store.list_payments(userid)
        sent_ytd = Decimal('0.00')
        fee_ytd = Decimal('0.00')
        recalled = Decimal('0.00')
        for row in payments:
            amount = parse_money(row.amount, allow_zero=True)
            if row.status in {CHIP_RELEASED, CHIP_SETTLED}:
                sent_ytd += amount
                if row.fee_status == FEE_COLLECTED:
                    fee_ytd += parse_money(row.fee, allow_zero=True)
            elif row.status == CHIP_RECALLED:
                recalled += amount
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        net = self._net_debit(userid, clock['cycle_date'])
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'fee': money_str(self.policy.outbound_fee),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'net_debit': money_str(net),
            'net_debit_cap': money_str(self.policy.net_debit_cap),
            'net_headroom': money_str(self.policy.net_debit_cap - net),
            'settlement_method': SETTLEMENT_CHIPS_NET,
            'clock': clock,
            'beneficiaries': [row.to_dict() for row in beneficiaries[:40]],
            'payments': [row.to_dict() for row in payments[:40]],
            'ytd_sent': money_str(sent_ytd),
            'ytd_fees': money_str(fee_ytd),
            'recalled_ytd': money_str(recalled),
            'active_count': sum(1 for row in beneficiaries if row.status == BENE_ACTIVE),
            'open_count': sum(1 for row in payments if row.status in OPEN_CHIPS),
        }


_SERVICE: Optional[ChipsService] = None


def set_service(service: Optional[ChipsService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[ChipsService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('CHIPS_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryChipsStore()
    path = os.environ.get('CHIPS_DB', DEFAULT_STORE_PATH)
    return SqliteChipsStore(path)


def build_service(
    *,
    clock=None,
    store=None,
    debit_fn=None,
    credit_fn=None,
    accounts_fn=None,
    screen_fn=None,
    calendar=None,
) -> ChipsService:
    policy = ChipsPolicy.from_env()
    return ChipsService(
        policy,
        store or default_store(),
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
        'payment_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_settled': 409,
        'already_recalled': 409,
        'nsf': 409,
        'failed': 409,
        'recall_failed': 409,
        'net_debit_cap': 409,
        'chips_forbidden': 403,
        'chips_disabled': 403,
        'beneficiary_paused': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_cancelable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_settleable': 403,
        'not_recallable': 403,
        'not_overridable': 403,
        'not_exportable': 403,
        'scheme_irrevocable': 403,
        'beneficiary_not_found': 404,
        'payment_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_participant': 400,
        'invalid_uid': 400,
        'invalid_aba': 400,
        'unknown_participant': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_sequence': 400,
        'invalid_file': 400,
        'invalid_date': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_beneficiary': 400,
        'missing_payment': 400,
    }.get(code, 400)


def _error_body(exc: ChipsError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('payment') is not None:
        body['payment'] = exc.extra['payment'].to_dict()
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
    except ChipsError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list_chips(service: ChipsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Chips': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_add_beneficiary(service: ChipsService):
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
            participant=values.get('participant') or values.get('chips_id'),
            uid=values.get('uid') or values.get('chips_uid'),
            account_number=values.get('account_number') or values.get('external_account'),
            default_account=values.get('default_account') or values.get('account') or values.get('from_account'),
            aba=values.get('aba') or values.get('routing'),
        )
        return jsonify({
            'message': 'CHIPS beneficiary added',
            'beneficiary': row.to_dict(),
            'Chips': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _bene_status_route(service: ChipsService, status: str, ok_message: str):
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
            'Chips': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: ChipsService):
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
        return jsonify({'preview': preview, 'Chips': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: ChipsService):
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
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({
            'message': 'CHIPS originated' if created else 'CHIPS already posted',
            'payment': payment.to_dict(),
            'Chips': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: ChipsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    payment_id = str(values.get('payment_id') or values.get('chips_id') or '').strip()
    if not payment_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_payment'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        payment = service.cancel_payment(
            payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': 'CHIPS cancelled',
            'payment': payment.to_dict(),
            'Chips': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_chips_route(service: ChipsService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    payment_id = str(values.get('payment_id') or values.get('chips_id') or '').strip()
    if not payment_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_payment'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        extra: Dict[str, Any] = {}
        if action == 'release':
            payment = service.release_payment(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'CHIPS released'
        elif action == 'reject':
            payment = service.reject_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'CHIPS rejected'
        elif action == 'settle':
            payment = service.settle_payment(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'CHIPS settled'
        elif action == 'recall':
            payment = service.recall_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'CHIPS recalled'
        elif action == 'override':
            payment = service.override_ofac(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'waive':
            payment = service.waive_fee(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'CHIPS fee waived'
        elif action == 'export':
            exported = service.export_chip1(payment_id=payment_id, actor=userid, actor_type=actor_type)
            payment = service.get_payment(payment_id=payment_id, actor=userid, actor_type=actor_type)
            extra['export'] = exported
            message = 'CHIPS exported'
        else:
            raise ChipsError('invalid_status', 'Unknown CHIPS action.')
        body = {
            'message': message,
            'payment': payment.to_dict(),
            'Chips': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }
        body.update(extra)
        return jsonify(body), 200

    return _handle_errors(_run)


def handle_run_due(service: ChipsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Chips': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_chips_routes(app, service: ChipsService) -> None:
    @app.route('/listChips', methods=['POST', 'GET'])
    def list_chips_route():
        return handle_list_chips(service)

    @app.route('/listChipsBeneficiaries', methods=['POST', 'GET'])
    def list_chips_beneficiaries_route():
        return handle_list_chips(service)

    @app.route('/addChipsBeneficiary', methods=['POST', 'GET'])
    def add_chips_beneficiary_route():
        return handle_add_beneficiary(service)

    @app.route('/pauseChipsBeneficiary', methods=['POST', 'GET'])
    def pause_chips_beneficiary_route():
        return _bene_status_route(service, BENE_PAUSED, 'CHIPS beneficiary paused')

    @app.route('/resumeChipsBeneficiary', methods=['POST', 'GET'])
    def resume_chips_beneficiary_route():
        return _bene_status_route(service, BENE_ACTIVE, 'CHIPS beneficiary resumed')

    @app.route('/archiveChipsBeneficiary', methods=['POST', 'GET'])
    def archive_chips_beneficiary_route():
        return _bene_status_route(service, BENE_ARCHIVED, 'CHIPS beneficiary archived')

    @app.route('/previewChips', methods=['POST', 'GET'])
    def preview_chips_route():
        return handle_preview(service)

    @app.route('/sendChips', methods=['POST', 'GET'])
    def send_chips_route():
        return handle_send(service)

    @app.route('/cancelChips', methods=['POST', 'GET'])
    def cancel_chips_route():
        return handle_cancel(service)

    @app.route('/releaseChips', methods=['POST', 'GET'])
    def release_chips_route():
        return _staff_chips_route(service, 'release')

    @app.route('/rejectChips', methods=['POST', 'GET'])
    def reject_chips_route():
        return _staff_chips_route(service, 'reject')

    @app.route('/settleChips', methods=['POST', 'GET'])
    def settle_chips_route():
        return _staff_chips_route(service, 'settle')

    @app.route('/recallChips', methods=['POST', 'GET'])
    def recall_chips_route():
        return _staff_chips_route(service, 'recall')

    @app.route('/overrideChipsOfac', methods=['POST', 'GET'])
    def override_chips_ofac_route():
        return _staff_chips_route(service, 'override')

    @app.route('/waiveChipsFee', methods=['POST', 'GET'])
    def waive_chips_fee_route():
        return _staff_chips_route(service, 'waive')

    @app.route('/exportChips', methods=['POST', 'GET'])
    def export_chips_route():
        return _staff_chips_route(service, 'export')

    @app.route('/runDueChips', methods=['POST', 'GET'])
    def run_due_chips_route():
        return handle_run_due(service)
