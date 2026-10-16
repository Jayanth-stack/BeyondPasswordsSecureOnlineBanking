"""Inbound CHIPS receive posting (USD UID directory / net settlement).

Staff ingest incoming CHIPS credits. Recipients are enrolled 6-digit
Universal Identifiers at this bank's 4-digit participant id. Matched
credits post as `released` during the CHIPS session (09:00–17:00 ET)
and net-settle at cutoff (`settled`). Independent of outbound CHIPS
origination (PR #131), ISO 20022 investigation (PR #129), inbound
Zelle (PR #125), ACH linking (PR #68), and domestic Fedwire (PR #73).
Existing `/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay
unchanged.

Foundations (reusable beyond this screen):
- CHIPS 4-digit participant id + optional ABA-to-participant directory
- CHIPS Universal Identifier compose / normalize / mask
- CHIPS sequence `{YYYYMMDD}{send_pid}{seq}` uniqueness
- Session clock with open hour AND cutoff (net settlement, not RTGS)
- CHIP1 pipe compose/parse (XML/DOCTYPE rejected)
- Dual-control at $10k, same-day net settlement of released credits

Reuses Fedwire OFAC screening, money parsing, and account directory
from `utility.wire`. Stores are pluggable (memory for tests, sqlite WAL
for restart-safe default). Full UID / sequence / destination account
never appear in to_dict / snapshots.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    DEFAULT_WATCHLIST,
    EMPLOYEE_ROLES,
    MONEY_QUANTUM,
    AccountError,
    AmountError,
    ScreenResult,
    account_types_from_customer_payload,
    last4,
    money_str,
    normalize_aba,
    normalize_account,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
    us_fed_holidays,
)

UID_ACTIVE = 'active'
UID_PAUSED = 'paused'
UID_CANCELLED = 'cancelled'
UID_STATUSES = frozenset({UID_ACTIVE, UID_PAUSED, UID_CANCELLED})
OPEN_UID = frozenset({UID_ACTIVE, UID_PAUSED})
IN_UNMATCHED = 'unmatched'
IN_HELD = 'held'
IN_PENDING = 'pending_release'
IN_QUEUED = 'queued'
IN_RELEASED = 'released'
IN_SETTLED = 'settled'
IN_REJECTED = 'rejected'
IN_RETURNED = 'returned'
IN_FAILED = 'failed'
IN_STATUSES = frozenset({
    IN_UNMATCHED, IN_HELD, IN_PENDING, IN_QUEUED, IN_RELEASED, IN_SETTLED,
    IN_REJECTED, IN_RETURNED, IN_FAILED,
})
OPEN_IN = frozenset({IN_UNMATCHED, IN_HELD, IN_PENDING, IN_QUEUED})
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
DEFAULT_RECEIVER = '0001'
DEFAULT_STORE_PATH = 'SystemLogs/crecv.sqlite'
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)


class CrecvError(ValueError):
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
    text = str(result or '').strip().lower()
    if not text:
        return 'failed'
    if any(token in text for token in CREDIT_NSF):
        return 'nsf'
    if text in CREDIT_OK or 'success' in text or 'credited' in text or 'debited' in text:
        return 'ok'
    return 'failed'


def normalize_nickname(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise CrecvError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise CrecvError('invalid_name', 'Sender legal name must be 2-80 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise CrecvError('invalid_purpose', 'Unknown CHIPS purpose.')
    return text


def normalize_participant(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise CrecvError('invalid_participant', 'CHIPS participant id is required.')
        return ''
    if len(digits) < 4:
        digits = digits.zfill(4)
    if len(digits) != 4 or digits == '0000':
        raise CrecvError('invalid_participant', 'CHIPS participant id must be four digits.')
    return digits


def mask_participant(value: Any) -> str:
    pid = str(value or '').strip()
    if len(pid) < 4:
        return pid or '****'
    return '**' + pid[-2:]


def participant_name(value: Any, *, directory: Optional[Dict[str, str]] = None) -> str:
    pid = normalize_participant(value)
    names = directory if directory is not None else DEFAULT_PARTICIPANTS
    return names.get(pid, '')


def participant_from_aba(value: Any, *, mapping: Optional[Dict[str, str]] = None) -> str:
    try:
        aba = normalize_aba(value)
    except Exception as exc:
        raise CrecvError('invalid_aba', 'ABA routing number is invalid.') from exc
    table = mapping if mapping is not None else ABA_TO_PARTICIPANT
    pid = table.get(aba)
    if not pid:
        raise CrecvError('unknown_participant', 'No CHIPS participant mapped for that ABA.')
    return pid


def compose_uid(account: Any) -> str:
    """Fold an account number into a 6-digit CHIPS Universal Identifier."""
    digits = ''.join(ch for ch in str(account or '') if ch.isdigit())
    if not digits:
        raise CrecvError('invalid_uid', 'Account or UID is required.')
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
            raise CrecvError('invalid_uid', 'CHIPS UID must be six digits.')
        return digits
    if account not in (None, ''):
        return compose_uid(account)
    raise CrecvError('invalid_uid', 'CHIPS UID or account is required.')


def mask_uid(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 3:
        return '***'
    return '***' + digits[-3:]


def compose_chips_seq(cycle_date: str, send_pid: str, sequence: int) -> str:
    """CHIPS sequence: {YYYYMMDD}{4-digit send PID}{6-digit seq}."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise CrecvError('invalid_sequence', 'Cycle date must be YYYYMMDD.')
    pid = normalize_participant(send_pid)
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise CrecvError('invalid_sequence', 'CHIPS sequence out of range.')
    return '%s%s%06d' % (day, pid, seq)


def normalize_chips_seq(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 18:
        raise CrecvError('invalid_sequence', 'CHIPS sequence must be 18 digits.')
    compose_chips_seq(digits[:8], digits[8:12], int(digits[12:]))
    return digits


def mask_chips_seq(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 8:
        return digits or '***'
    return digits[:8] + '***' + digits[-4:]


def compose_settlement_id(cycle_date: str, recv_pid: str) -> str:
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise CrecvError('invalid_date', 'Settlement date must be YYYYMMDD.')
    return 'NET%s%s' % (day, normalize_participant(recv_pid))


def compose_amount_field(amount: Decimal) -> str:
    cents = int((amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN) * 100).to_integral_value())
    if cents < 0 or cents > 999999999999:
        raise CrecvError('invalid_amount', 'Amount cannot be encoded in CHIP1.')
    return '%012d' % cents


def parse_amount_field(value: Any) -> Decimal:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits or len(digits) > 12:
        raise CrecvError('invalid_amount', 'CHIP1 amount field is invalid.')
    return (Decimal(digits) / Decimal('100')).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def _parse_holiday_day(text: str) -> date:
    raw = str(text or '').strip()
    if not raw:
        raise ValueError('empty holiday')
    if '-' in raw:
        return date.fromisoformat(raw[:10])
    return datetime.strptime(raw[:8], '%Y%m%d').date()


def _reject_markup(text: str) -> None:
    lowered = text.lower()
    if '<?xml' in lowered or '<!doctype' in lowered or '<!entity' in lowered:
        raise CrecvError('invalid_file', 'XML/DOCTYPE CHIP1 files are rejected.')
    if '<chip' in lowered or '<pacs' in lowered:
        raise CrecvError('invalid_file', 'XML/DOCTYPE CHIP1 files are rejected.')


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
        raise CrecvError('invalid_date', 'Value date must be YYYYMMDD.')
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
        raise CrecvError('invalid_file', 'CHIP1 record is empty.')
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != CHIP1_FIELDS:
        raise CrecvError('invalid_file', 'CHIP1 record must have %d fields.' % CHIP1_FIELDS)
    if parts[0] != 'CHIP1' or parts[1] != CHIP1_VERSION:
        raise CrecvError('invalid_file', 'CHIP1 version marker is invalid.')
    if parts[10] != SETTLEMENT_CHIPS_NET:
        raise CrecvError('invalid_file', 'CHIP1 settlement method must be chips_net.')
    seq = parts[2]
    if '***' in seq or '***' in parts[6]:
        raise CrecvError('invalid_file', 'Masked CHIP1 sequence or UID cannot be re-ingested.')
    amount = parse_amount_field(parts[7])
    return {
        'sequence': normalize_chips_seq(seq),
        'value_date': parts[3],
        'send_pid': normalize_participant(parts[4]),
        'recv_pid': normalize_participant(parts[5]),
        'uid': normalize_uid(parts[6]),
        'amount': parse_money(amount),
        'name': normalize_legal_name(parts[8]),
        'purpose': normalize_purpose(parts[9]),
        'settlement_method': parts[10],
    }


def split_chip1_file(text: Any) -> List[str]:
    raw = str(text or '')
    _reject_markup(raw)
    rows = []
    for line in raw.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            rows.append(stripped)
    if not rows:
        raise CrecvError('invalid_file', 'CHIP1 file is empty.')
    return rows


def message_from_chip1(record: str) -> Dict[str, Any]:
    return parse_chip1(record)


def message_from_values(values: Dict[str, Any]) -> Dict[str, Any]:
    send_pid = values.get('send_pid') or values.get('sender') or values.get('participant')
    if not send_pid:
        aba = values.get('send_aba') or values.get('aba') or values.get('sender_aba')
        if aba:
            send_pid = participant_from_aba(aba)
    recv_pid = values.get('recv_pid') or values.get('receiver') or values.get('receiver_pid') or DEFAULT_RECEIVER
    sequence = values.get('sequence') or values.get('chips_seq')
    value_date = str(values.get('value_date') or values.get('cycle_date') or '').strip().replace('-', '')
    if not sequence:
        seq_n = values.get('seq') or values.get('sequence_n') or 1
        if not value_date:
            raise CrecvError('invalid_sequence', 'CHIPS sequence or cycle date is required.')
        sequence = compose_chips_seq(value_date, send_pid, int(seq_n))
    sequence = normalize_chips_seq(sequence)
    if not value_date:
        value_date = sequence[:8]
    uid = values.get('uid') or values.get('universal_id')
    account = values.get('account') or values.get('destination_account')
    return {
        'sequence': sequence,
        'value_date': value_date,
        'send_pid': normalize_participant(send_pid),
        'recv_pid': normalize_participant(recv_pid),
        'uid': normalize_uid(uid, account=account),
        'amount': parse_money(values.get('amount') or values.get('amount_usd')),
        'name': normalize_legal_name(values.get('name') or values.get('sender_name') or values.get('originator')),
        'purpose': normalize_purpose(values.get('purpose') or 'other'),
        'settlement_method': SETTLEMENT_CHIPS_NET,
    }


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
            extra.add(_parse_holiday_day(text))
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
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'open': '%02d:%02d' % (self.open_hour, self.open_minute),
            'cutoff': '%02d:%02d' % (self.cutoff_hour, self.cutoff_minute),
            'before_open': self.is_before_open(ts) and self.is_business_day(local.date()),
            'after_cutoff': self.is_after_cutoff(ts) or not self.is_business_day(local.date()),
            'session_open': self.is_session_open(ts),
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


@dataclass
class CrecvPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_return: bool = True
    allow_credit: bool = False
    max_uids: int = 8
    max_inbounds: int = 120
    min_amount: Decimal = Decimal('100.00')
    max_amount: Decimal = Decimal('100000000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    open_hour: int = 9
    cutoff_hour: int = 17
    tz_offset_hours: int = -4
    receiver_pid: str = DEFAULT_RECEIVER
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()
    participants: Dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.participants is None:
            self.participants = dict(DEFAULT_PARTICIPANTS)

    @classmethod
    def from_env(cls) -> 'CrecvPolicy':
        extra = _env_list('CRECV_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        extra_pids = _env_list('CRECV_PARTICIPANTS')
        directory = dict(DEFAULT_PARTICIPANTS)
        for item in extra_pids:
            if ':' in item:
                pid, name = item.split(':', 1)
                try:
                    directory[normalize_participant(pid)] = name.strip().upper()[:80]
                except CrecvError:
                    continue
        source = os.environ.get('CRECV_RECEIVER') or DEFAULT_RECEIVER
        try:
            receiver = normalize_participant(source)
        except CrecvError:
            receiver = DEFAULT_RECEIVER
        return cls(
            enabled=_env_bool('CRECV_ENABLED', True),
            customer_manage=_env_bool('CRECV_CUSTOMER_MANAGE', True),
            customer_return=_env_bool('CRECV_CUSTOMER_RETURN', True),
            allow_credit=_env_bool('CRECV_ALLOW_CREDIT', False),
            max_uids=max(1, _env_int('CRECV_MAX_UIDS', 8)),
            max_inbounds=max(1, _env_int('CRECV_MAX_INBOUNDS', 120)),
            min_amount=_env_money('CRECV_MIN_AMOUNT', '100.00'),
            max_amount=_env_money('CRECV_MAX_AMOUNT', '100000000.00'),
            dual_control_threshold=_env_money('CRECV_DUAL_CONTROL', '10000.00'),
            open_hour=_env_int('CRECV_OPEN_HOUR', 9),
            cutoff_hour=_env_int('CRECV_CUTOFF_HOUR', 17),
            tz_offset_hours=_env_int('CRECV_TZ_OFFSET', -4),
            receiver_pid=receiver,
            watchlist=watch,
            extra_holidays=_env_list('CRECV_HOLIDAYS'),
            participants=directory,
        )


@dataclass
class CrecvUid:
    uid_id: str
    userid: str
    nickname: str
    uid: str
    destination_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'uid_id': self.uid_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'uid_masked': mask_uid(self.uid),
            'account_last4': last4(self.destination_account),
            'status': self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == UID_ACTIVE,
            'paused': self.status == UID_PAUSED,
            'cancelled': self.status == UID_CANCELLED,
        }


@dataclass
class CrecvInbound:
    inbound_id: str
    sequence: str
    userid: str
    destination_account: str
    amount_usd: str
    sender_name: str
    send_pid: str
    recv_pid: str
    uid: str
    purpose: str
    value_date: str
    status: str
    reason: str
    actor: str
    releaser: str
    ofac_hit: bool
    ofac_match: str
    ofac_overridden: bool
    created_at: float
    updated_at: float
    released_at: float = 0.0
    settled_at: float = 0.0
    returned_at: float = 0.0
    settlement_id: str = ''
    note: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'inbound_id': self.inbound_id,
            'sequence_masked': mask_chips_seq(self.sequence),
            'userid': self.userid,
            'account_last4': last4(self.destination_account) if self.destination_account else '',
            'amount_usd': self.amount_usd,
            'sender_name': self.sender_name,
            'send_pid': self.send_pid,
            'send_name': participant_name(self.send_pid) if self.send_pid else '',
            'recv_pid': self.recv_pid,
            'uid_masked': mask_uid(self.uid) if self.uid else '***',
            'purpose': self.purpose,
            'value_date': self.value_date,
            'status': self.status,
            'reason': self.reason,
            'ofac_hit': self.ofac_hit,
            'returnable': self.status in OPEN_IN or self.status == IN_RELEASED,
            'settlement_id': self.settlement_id,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'released_at': self.released_at,
            'settled_at': self.settled_at,
        }


def _uid_from_row(row: Any) -> CrecvUid:
    data = dict(row)
    return CrecvUid(
        uid_id=data['uid_id'], userid=data['userid'], nickname=data['nickname'],
        uid=data['uid'], destination_account=data['destination_account'],
        status=data['status'], actor=data['actor'],
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
    )


def _inbound_from_row(row: Any) -> CrecvInbound:
    data = dict(row)
    return CrecvInbound(
        inbound_id=data['inbound_id'], sequence=data['sequence'], userid=data['userid'],
        destination_account=data['destination_account'], amount_usd=data['amount_usd'],
        sender_name=data['sender_name'], send_pid=data['send_pid'], recv_pid=data['recv_pid'],
        uid=data['uid'], purpose=data['purpose'], value_date=data['value_date'],
        status=data['status'], reason=data['reason'], actor=data['actor'],
        releaser=data['releaser'], ofac_hit=bool(int(data['ofac_hit'])),
        ofac_match=data['ofac_match'], ofac_overridden=bool(int(data['ofac_overridden'])),
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
        released_at=float(data['released_at'] or 0), settled_at=float(data['settled_at'] or 0),
        returned_at=float(data['returned_at'] or 0), settlement_id=data.get('settlement_id') or '',
        note=data.get('note') or '',
    )


class MemoryCrecvStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.uids: Dict[str, CrecvUid] = {}
        self.inbounds: Dict[str, CrecvInbound] = {}
        self._by_seq: Dict[str, str] = {}

    def put_uid(self, row: CrecvUid) -> None:
        with self._lock:
            self.uids[row.uid_id] = row

    def get_uid(self, uid_id: str) -> Optional[CrecvUid]:
        return self.uids.get(uid_id)

    def get_uid_by_value(self, uid: str) -> Optional[CrecvUid]:
        matches = [
            row for row in self.uids.values()
            if row.uid == uid and row.status in OPEN_UID
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda row: row.created_at, reverse=True)[0]

    def update_uid(self, row: CrecvUid) -> None:
        self.put_uid(row)

    def list_uids(self, userid: Optional[str] = None) -> List[CrecvUid]:
        rows = list(self.uids.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        return sorted(rows, key=lambda row: row.created_at)

    def put_inbound(self, row: CrecvInbound) -> None:
        with self._lock:
            self.inbounds[row.inbound_id] = row
            self._by_seq[row.sequence] = row.inbound_id

    def get_inbound(self, inbound_id: str) -> Optional[CrecvInbound]:
        return self.inbounds.get(inbound_id)

    def get_by_sequence(self, sequence: str) -> Optional[CrecvInbound]:
        inbound_id = self._by_seq.get(sequence)
        return self.inbounds.get(inbound_id) if inbound_id else None

    def update_inbound(self, row: CrecvInbound) -> None:
        self.put_inbound(row)

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[CrecvInbound]:
        rows = list(self.inbounds.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)


class SqliteCrecvStore:
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
                CREATE TABLE IF NOT EXISTS uids (
                    uid_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    uid TEXT NOT NULL,
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
                    sequence TEXT NOT NULL UNIQUE,
                    userid TEXT NOT NULL DEFAULT '',
                    destination_account TEXT NOT NULL DEFAULT '',
                    amount_usd TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    send_pid TEXT NOT NULL,
                    recv_pid TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    value_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    ofac_overridden INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    released_at REAL NOT NULL DEFAULT 0,
                    settled_at REAL NOT NULL DEFAULT 0,
                    returned_at REAL NOT NULL DEFAULT 0,
                    settlement_id TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_uid(self, row: CrecvUid) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO uids (
                    uid_id, userid, nickname, uid, destination_account,
                    status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.uid_id, row.userid, row.nickname, row.uid, row.destination_account,
                    row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_uid(self, uid_id: str) -> Optional[CrecvUid]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM uids WHERE uid_id = ?', (uid_id,)).fetchone()
        return _uid_from_row(row) if row else None

    def get_uid_by_value(self, uid: str) -> Optional[CrecvUid]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM uids WHERE uid = ? AND status IN ('active', 'paused')
                ORDER BY created_at DESC LIMIT 1
                """,
                (uid,),
            ).fetchone()
        return _uid_from_row(row) if row else None

    def update_uid(self, row: CrecvUid) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE uids SET nickname=?, uid=?, destination_account=?,
                    status=?, actor=?, updated_at=?
                WHERE uid_id=?
                """,
                (
                    row.nickname, row.uid, row.destination_account,
                    row.status, row.actor, row.updated_at, row.uid_id,
                ),
            )
            conn.commit()

    def list_uids(self, userid: Optional[str] = None) -> List[CrecvUid]:
        sql = 'SELECT * FROM uids'
        args: Tuple[Any, ...] = ()
        if userid is not None:
            sql += ' WHERE userid = ?'
            args = (userid,)
        sql += ' ORDER BY created_at'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_uid_from_row(row) for row in rows]

    def put_inbound(self, row: CrecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inbounds (
                    inbound_id, sequence, userid, destination_account, amount_usd,
                    sender_name, send_pid, recv_pid, uid, purpose, value_date, status,
                    reason, actor, releaser, ofac_hit, ofac_match, ofac_overridden,
                    created_at, updated_at, released_at, settled_at, returned_at,
                    settlement_id, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.inbound_id, row.sequence, row.userid, row.destination_account, row.amount_usd,
                    row.sender_name, row.send_pid, row.recv_pid, row.uid, row.purpose, row.value_date,
                    row.status, row.reason, row.actor, row.releaser, int(row.ofac_hit), row.ofac_match,
                    int(row.ofac_overridden), row.created_at, row.updated_at, row.released_at,
                    row.settled_at, row.returned_at, row.settlement_id, row.note,
                ),
            )
            conn.commit()

    def get_inbound(self, inbound_id: str) -> Optional[CrecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,)).fetchone()
        return _inbound_from_row(row) if row else None

    def get_by_sequence(self, sequence: str) -> Optional[CrecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE sequence = ?', (sequence,)).fetchone()
        return _inbound_from_row(row) if row else None

    def update_inbound(self, row: CrecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE inbounds SET userid=?, destination_account=?, amount_usd=?, sender_name=?,
                    send_pid=?, recv_pid=?, uid=?, purpose=?, value_date=?, status=?, reason=?,
                    actor=?, releaser=?, ofac_hit=?, ofac_match=?, ofac_overridden=?, updated_at=?,
                    released_at=?, settled_at=?, returned_at=?, settlement_id=?, note=?
                WHERE inbound_id=?
                """,
                (
                    row.userid, row.destination_account, row.amount_usd, row.sender_name,
                    row.send_pid, row.recv_pid, row.uid, row.purpose, row.value_date, row.status,
                    row.reason, row.actor, row.releaser, int(row.ofac_hit), row.ofac_match,
                    int(row.ofac_overridden), row.updated_at, row.released_at, row.settled_at,
                    row.returned_at, row.settlement_id, row.note, row.inbound_id,
                ),
            )
            conn.commit()

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[CrecvInbound]:
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


class CrecvService:
    def __init__(
        self,
        policy: CrecvPolicy,
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
        self.clock = clock or (lambda: datetime.now(tz=timezone.utc).timestamp())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn or screen_name
        self.calendar = calendar or ChipsCalendar(
            open_hour=policy.open_hour,
            cutoff_hour=policy.cutoff_hour,
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )

    def _now(self) -> float:
        return float(self.clock())

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise CrecvError('crecv_disabled', 'CHIPS receive is disabled.')

    def _require_staff(self, actor_type: str) -> None:
        if actor_type not in EMPLOYEE_ROLES:
            raise CrecvError('crecv_forbidden', 'Staff only.')

    def _require_customer_manage(self, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if not self.policy.customer_manage:
            raise CrecvError('crecv_forbidden', 'Customers cannot manage CHIPS UIDs.')

    def _accounts(self, userid: str) -> Any:
        if not self.accounts_fn:
            return {}
        return self.accounts_fn(userid) or {}

    def _owned(self, userid: str, account: str) -> None:
        owned = own_accounts_from_customer_payload(self._accounts(userid))
        if account not in owned:
            raise CrecvError('invalid_account', 'Destination is not an account of this customer.')

    def _account_type(self, userid: str, account: str) -> str:
        return account_types_from_customer_payload(self._accounts(userid)).get(account, '')

    def _credit_allowed(self, userid: str, account: str) -> bool:
        kind = self._account_type(userid, account)
        if kind == 'credit' and not self.policy.allow_credit:
            return False
        return True

    def add_uid(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: str,
        destination_account: Any,
        uid: Any = None,
    ) -> CrecvUid:
        self._require_enabled()
        self._require_customer_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise CrecvError('crecv_forbidden', 'Cannot enroll a UID for another customer.')
        account = normalize_account(destination_account)
        self._owned(owner_userid, account)
        if not self._credit_allowed(owner_userid, account):
            raise CrecvError('credit_not_allowed', 'Credit accounts cannot receive CHIPS.')
        uid_n = normalize_uid(uid, account=account)
        existing = self.store.get_uid_by_value(uid_n)
        if existing and existing.status in OPEN_UID:
            raise CrecvError('uid_duplicate', 'That CHIPS UID is already enrolled.')
        open_rows = [row for row in self.store.list_uids(owner_userid) if row.status in OPEN_UID]
        if any(row.nickname.lower() == normalize_nickname(nickname).lower() for row in open_rows):
            raise CrecvError('uid_duplicate', 'That nickname is already in use.')
        if len(open_rows) >= self.policy.max_uids:
            raise CrecvError('uid_limit', 'CHIPS UID limit reached.')
        now = self._now()
        row = CrecvUid(
            uid_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=normalize_nickname(nickname),
            uid=uid_n,
            destination_account=account,
            status=UID_ACTIVE,
            actor=actor,
            created_at=now,
            updated_at=now,
        )
        self.store.put_uid(row)
        return row

    def set_uid_status(
        self,
        *,
        uid_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> CrecvUid:
        self._require_enabled()
        row = self.store.get_uid(uid_id)
        if row is None:
            raise CrecvError('uid_not_found', 'CHIPS UID enrollment not found.')
        if actor_type not in EMPLOYEE_ROLES and actor != row.userid:
            raise CrecvError('crecv_forbidden', 'Cannot change another customer UID.')
        self._require_customer_manage(actor_type)
        if status not in UID_STATUSES:
            raise CrecvError('invalid_status', 'Unknown UID status.')
        if row.status == UID_CANCELLED:
            raise CrecvError('already_cancelled', 'Cancelled UIDs cannot change.')
        if status == UID_PAUSED and row.status != UID_ACTIVE:
            raise CrecvError('already_paused' if row.status == UID_PAUSED else 'invalid_status', 'UID is not active.')
        if status == UID_ACTIVE and row.status != UID_PAUSED:
            raise CrecvError('already_active', 'UID is already active.')
        if status == row.status:
            code = 'already_paused' if status == UID_PAUSED else 'already_active'
            raise CrecvError(code, 'UID already in that status.')
        updated = replace(row, status=status, actor=actor, updated_at=self._now())
        self.store.update_uid(updated)
        if status == UID_ACTIVE:
            self._rematch_unmatched(updated)
        return updated

    def _rematch_unmatched(self, enrollment: CrecvUid) -> None:
        for inbound in self.store.list_inbounds(status=IN_UNMATCHED):
            if inbound.uid != enrollment.uid:
                continue
            inbound.userid = enrollment.userid
            inbound.destination_account = enrollment.destination_account
            inbound.updated_at = self._now()
            self.store.update_inbound(inbound)
            self._evaluate(inbound, rescreen=True)

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        values: Dict[str, Any],
    ) -> Tuple[CrecvInbound, bool]:
        self._require_enabled()
        self._require_staff(actor_type)
        parsed = message_from_values(values)
        return self._ingest_parsed(actor=actor, parsed=parsed)

    def ingest_file(
        self,
        *,
        actor: str,
        actor_type: str,
        payload: str,
    ) -> Tuple[List[CrecvInbound], int]:
        self._require_enabled()
        self._require_staff(actor_type)
        created = 0
        rows = []
        for record in split_chip1_file(payload):
            parsed = parse_chip1(record)
            row, is_new = self._ingest_parsed(actor=actor, parsed=parsed)
            rows.append(row)
            if is_new:
                created += 1
        return rows, created

    def _ingest_parsed(self, *, actor: str, parsed: Dict[str, Any]) -> Tuple[CrecvInbound, bool]:
        if parsed['recv_pid'] != self.policy.receiver_pid:
            raise CrecvError('wrong_receiver', 'CHIPS credit is not for this participant.')
        existing = self.store.get_by_sequence(parsed['sequence'])
        if existing is not None:
            return existing, False
        total = len(self.store.list_inbounds())
        if total >= self.policy.max_inbounds:
            raise CrecvError('inbound_limit', 'Inbound CHIPS limit reached.')
        amount = parsed['amount'] if isinstance(parsed['amount'], Decimal) else parse_money(parsed['amount'])
        if amount < self.policy.min_amount or amount > self.policy.max_amount:
            code = 'chips_amount_exceeded' if amount > self.policy.max_amount else 'amount_out_of_range'
            raise CrecvError(code, 'Amount is outside the CHIPS range.')
        now = self._now()
        row = CrecvInbound(
            inbound_id=uuid.uuid4().hex,
            sequence=parsed['sequence'],
            userid='',
            destination_account='',
            amount_usd=money_str(amount),
            sender_name=parsed['name'],
            send_pid=parsed['send_pid'],
            recv_pid=parsed['recv_pid'],
            uid=parsed['uid'],
            purpose=parsed.get('purpose') or 'other',
            value_date=str(parsed.get('value_date') or parsed['sequence'][:8]),
            status=IN_UNMATCHED,
            reason='',
            actor=actor,
            releaser='',
            ofac_hit=False,
            ofac_match='',
            ofac_overridden=False,
            created_at=now,
            updated_at=now,
        )
        enrollment = self.store.get_uid_by_value(row.uid)
        if enrollment is not None and enrollment.status == UID_ACTIVE:
            row.userid = enrollment.userid
            row.destination_account = enrollment.destination_account
        self.store.put_inbound(row)
        self._evaluate(row, rescreen=True)
        return row, True

    def _evaluate(self, row: CrecvInbound, *, rescreen: bool) -> CrecvInbound:
        now = self._now()
        if row.status in {IN_SETTLED, IN_REJECTED, IN_RETURNED, IN_FAILED, IN_RELEASED}:
            if row.status == IN_RELEASED and self._should_settle(now, row):
                return self._settle_row(row, now)
            return row
        if not row.userid or not row.destination_account:
            row.status = IN_UNMATCHED
            row.reason = 'unmatched'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        try:
            self._owned(row.userid, row.destination_account)
        except CrecvError:
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
        usd = parse_money(row.amount_usd)
        if usd >= self.policy.dual_control_threshold and not row.releaser:
            row.status = IN_PENDING
            row.reason = 'dual_control'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if not self.calendar.is_session_open(now):
            row.status = IN_QUEUED
            row.reason = 'session_closed'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        return self._release_credit(row)

    def _release_credit(self, row: CrecvInbound) -> CrecvInbound:
        if self.credit_fn is None:
            raise CrecvError('failed', 'Credit function is not configured.')
        remark = 'chips from %s' % row.sender_name[:40]
        result = self.credit_fn(row.destination_account, row.amount_usd, remark)
        classified = _classify_money_result(result)
        now = self._now()
        if classified == 'nsf':
            row.status = IN_FAILED
            row.reason = 'nsf'
            row.updated_at = now
            self.store.update_inbound(row)
            raise CrecvError('nsf', 'Credit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            row.status = IN_FAILED
            row.reason = 'failed'
            row.updated_at = now
            self.store.update_inbound(row)
            raise CrecvError('failed', 'Credit failed.', inbound=row)
        row.status = IN_RELEASED
        row.reason = ''
        row.released_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def _should_settle(self, now: float, row: CrecvInbound) -> bool:
        if row.status != IN_RELEASED:
            return False
        local = self.calendar.local_dt(now).date()
        try:
            value = datetime.strptime(row.value_date[:8], '%Y%m%d').date()
        except (ValueError, TypeError):
            value = local
        if local > value:
            return True
        if local == value and self.calendar.is_after_cutoff(now):
            return True
        return False

    def _settle_row(self, row: CrecvInbound, now: float) -> CrecvInbound:
        row.status = IN_SETTLED
        row.reason = ''
        row.settled_at = now
        row.updated_at = now
        row.settlement_id = compose_settlement_id(row.value_date[:8], row.recv_pid)
        self.store.update_inbound(row)
        return row

    def preview(self, *, inbound_id: str = '', sequence: str = '', actor: str = '', actor_type: str = '') -> Dict[str, Any]:
        row = self._get(inbound_id=inbound_id, sequence=sequence)
        self._assert_visible(row, actor, actor_type)
        return {
            'inbound': row.to_dict(),
            'clock': self.calendar.snapshot(self._now()),
            'receiver': mask_participant(self.policy.receiver_pid),
        }

    def _get(self, *, inbound_id: str = '', sequence: str = '') -> CrecvInbound:
        row = None
        if inbound_id:
            row = self.store.get_inbound(str(inbound_id).strip())
        if row is None and sequence:
            row = self.store.get_by_sequence(normalize_chips_seq(sequence))
        if row is None:
            raise CrecvError('inbound_not_found', 'Inbound CHIPS not found.')
        return row

    def _assert_visible(self, row: CrecvInbound, actor: str, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if row.userid and row.userid != actor:
            raise CrecvError('crecv_forbidden', 'Cannot view another customer CHIPS.')

    def assign(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        customer_id: str,
        account: Any,
        nickname: str = '',
    ) -> CrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_UNMATCHED:
            raise CrecvError('not_assignable', 'Only unmatched CHIPS credits can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise CrecvError('missing_customer_id', 'Customer id is required to assign.')
        dest = normalize_account(account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise CrecvError('credit_not_allowed', 'Credit accounts cannot receive CHIPS.')
        existing = self.store.get_uid_by_value(row.uid)
        if existing is None or existing.status == UID_CANCELLED:
            self.add_uid(
                owner_userid=owner, actor=actor, actor_type=actor_type,
                nickname=nickname or ('CHIPS %s' % mask_uid(row.uid)),
                destination_account=dest, uid=row.uid,
            )
        elif existing.userid != owner:
            raise CrecvError('uid_duplicate', 'UID belongs to another customer.')
        row.userid = owner
        row.destination_account = dest
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=True)

    def override_ofac(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> CrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_HELD:
            raise CrecvError('not_overridable', 'Only OFAC-held CHIPS credits can be overridden.')
        row.ofac_overridden = True
        row.ofac_hit = False
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def release(self, *, inbound_id: str, actor: str, actor_type: str) -> CrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_PENDING:
            raise CrecvError('not_releasable', 'Only dual-control CHIPS credits can be released.')
        if actor == row.actor:
            raise CrecvError('same_approver', 'A different employee must release this credit.')
        row.releaser = actor
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def reject(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> CrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status not in OPEN_IN:
            raise CrecvError('not_rejectable', 'This CHIPS credit cannot be rejected.')
        row.status = IN_REJECTED
        row.reason = 'rejected'
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return row

    def settle(self, *, inbound_id: str, actor: str, actor_type: str) -> CrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        now = self._now()
        if row.status == IN_SETTLED:
            raise CrecvError('already_settled', 'CHIPS credit already settled.')
        if row.status != IN_RELEASED:
            raise CrecvError('not_settleable', 'Only released CHIPS credits can be settled.')
        if not self._should_settle(now, row):
            raise CrecvError('not_settleable', 'CHIPS session is still open.')
        _ = actor
        return self._settle_row(row, now)

    def export(self, *, inbound_id: str, actor: str, actor_type: str) -> Dict[str, Any]:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if not row.sequence:
            raise CrecvError('not_exportable', 'CHIPS credit has no sequence to export.')
        _ = actor
        record = compose_chip1(
            sequence=row.sequence,
            value_date=row.value_date,
            send_pid=row.send_pid,
            recv_pid=row.recv_pid,
            uid=row.uid,
            amount=parse_money(row.amount_usd, allow_zero=True),
            name=row.sender_name,
            purpose=row.purpose,
            mask=True,
        )
        return {'file': record, 'inbound': row.to_dict()}

    def return_inbound(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: str = 'cust',
    ) -> CrecvInbound:
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
    ) -> CrecvInbound:
        self._require_enabled()
        row = self._get(inbound_id=inbound_id)
        self._assert_visible(row, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_return:
                raise CrecvError('crecv_forbidden', 'Customers cannot return CHIPS credits.')
            if actor != row.userid:
                raise CrecvError('crecv_forbidden', 'Cannot return another customer CHIPS.')
        return self._return(row, actor=actor, reason=reason, honor_window=True)

    def _return(self, row: CrecvInbound, *, actor: str, reason: str, honor_window: bool) -> CrecvInbound:
        now = self._now()
        _ = actor
        if row.status == IN_SETTLED:
            raise CrecvError('scheme_irrevocable', 'Settled CHIPS credits cannot be returned.')
        if row.status == IN_RETURNED or row.returned_at:
            raise CrecvError('already_returned', 'CHIPS credit already returned.')
        if row.status in OPEN_IN:
            row.status = IN_RETURNED
            row.reason = reason or 'cust'
            row.returned_at = now
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if row.status != IN_RELEASED:
            raise CrecvError('not_returnable', 'This CHIPS credit cannot be returned.')
        if honor_window and self._should_settle(now, row):
            self._settle_row(row, now)
            raise CrecvError('scheme_irrevocable', 'Settled CHIPS credits cannot be returned.')
        if self.debit_fn is None:
            raise CrecvError('return_failed', 'Debit function is not configured.')
        remark = 'chips return %s' % row.sequence[-12:]
        result = self.debit_fn(row.destination_account, row.amount_usd, remark)
        classified = _classify_money_result(result)
        if classified == 'nsf':
            raise CrecvError('nsf', 'Return debit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            raise CrecvError('return_failed', 'Return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.reason = reason or 'cust'
        row.returned_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[CrecvInbound]:
        now = self._now()
        changed = []
        rows = self.store.list_inbounds(userid) if userid else self.store.list_inbounds()
        for row in rows:
            if row.status == IN_QUEUED:
                updated = self._evaluate(row, rescreen=False)
                if updated.status != IN_QUEUED:
                    changed.append(updated)
            elif row.status == IN_RELEASED and self._should_settle(now, row):
                changed.append(self._settle_row(row, now))
        return changed

    def snapshot(self, userid: str, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        _ = actor
        _ = actor_type
        now = self._now()
        if userid:
            self.run_due(userid)
        uids = self.store.list_uids(userid) if userid else []
        inbounds = self.store.list_inbounds(userid) if userid else []
        received = [row for row in inbounds if row.status in {IN_RELEASED, IN_SETTLED}]
        ytd = sum((parse_money(row.amount_usd, allow_zero=True) for row in received), Decimal('0.00'))
        return {
            'enabled': self.policy.enabled,
            'clock': self.calendar.snapshot(now),
            'receiver': mask_participant(self.policy.receiver_pid),
            'uids': [row.to_dict() for row in uids[:40]],
            'inbounds': [row.to_dict() for row in inbounds[:40]],
            'ytd_received': money_str(ytd),
            'open_count': sum(1 for row in inbounds if row.status in OPEN_IN or row.status == IN_RELEASED),
            'active_uids': sum(1 for row in uids if row.status == UID_ACTIVE),
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'settlement_method': SETTLEMENT_CHIPS_NET,
        }

    def unmatched_snapshot(self) -> Dict[str, Any]:
        rows = self.store.list_inbounds(status=IN_UNMATCHED)
        return {
            'enabled': self.policy.enabled,
            'unmatched': [row.to_dict() for row in rows[:40]],
            'count': len(rows),
        }


_SERVICE: Optional[CrecvService] = None


def set_service(service: Optional[CrecvService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[CrecvService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('CRECV_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryCrecvStore()
    path = os.environ.get('CRECV_DB', DEFAULT_STORE_PATH)
    return SqliteCrecvStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[ChipsCalendar] = None,
) -> CrecvService:
    if store is None:
        store = default_store()
    return CrecvService(
        CrecvPolicy.from_env(),
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


def _owner_from(actor: str, actor_type: str, values: Dict[str, Any]) -> str:
    if actor_type in EMPLOYEE_ROLES:
        return str(values.get('customer_id') or values.get('owner') or '').strip()
    return actor


def _error_status(code: str) -> int:
    return {
        'uid_duplicate': 409,
        'uid_limit': 409,
        'inbound_limit': 409,
        'already_cancelled': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_returned': 409,
        'already_settled': 409,
        'nsf': 409,
        'failed': 409,
        'return_failed': 409,
        'crecv_forbidden': 403,
        'crecv_disabled': 403,
        'credit_not_allowed': 403,
        'same_approver': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_returnable': 403,
        'not_settleable': 403,
        'not_exportable': 403,
        'scheme_irrevocable': 403,
        'uid_not_found': 404,
        'inbound_not_found': 404,
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
        'wrong_receiver': 400,
        'chips_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_uid': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: CrecvError) -> Dict[str, Any]:
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
    except CrecvError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snapshot_for(service: CrecvService, userid: str, actor: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(userid, actor=actor, actor_type=actor_type)


def handle_list(service: CrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = _owner_from(userid, actor_type, values)
        if not owner:
            return jsonify({'message': 'Customer id required', 'error': 'missing_customer_id'}), 400
    return jsonify({'ChipsIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def handle_unmatched(service: CrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'crecv_forbidden'}), 403
    snap = service.unmatched_snapshot()
    return jsonify({'UnmatchedChipsIns': snap, 'ChipsIn': snap}), 200


def handle_add_uid(service: CrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        owner = userid
        if actor_type in EMPLOYEE_ROLES:
            owner = _owner_from(userid, actor_type, values)
            if not owner:
                raise CrecvError('missing_customer_id', 'Customer id is required.')
        row = service.add_uid(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname') or '',
            destination_account=values.get('destination_account') or values.get('account') or values.get('default_account'),
            uid=values.get('uid') or values.get('universal_id'),
        )
        return jsonify({
            'message': 'CHIPS UID enrolled',
            'uid': row.to_dict(),
            'ChipsIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _uid_status_route(service: CrecvService, status: str, message: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        uid_id = str(values.get('uid_id') or '').strip()
        if not uid_id:
            raise CrecvError('missing_uid', 'uid_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.set_uid_status(uid_id=uid_id, actor=userid, actor_type=actor_type, status=status)
        return jsonify({
            'message': message,
            'uid': row.to_dict(),
            'ChipsIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: CrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        preview = service.preview(
            inbound_id=str(values.get('inbound_id') or ''),
            sequence=str(values.get('sequence') or values.get('chips_seq') or ''),
            actor=userid,
            actor_type=actor_type,
        )
        owner = preview['inbound'].get('userid') or userid
        return jsonify({'preview': preview, 'ChipsIn': _snapshot_for(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest(service: CrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'CHIPS ingested' if created else 'CHIPS already ingested',
            'inbound': row.to_dict(),
            'ChipsIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_ingest_file(service: CrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        payload = values.get('file') or values.get('body') or values.get('payload')
        if not payload:
            raise CrecvError('missing_file', 'CHIP1 file body is required.')
        actor_type = session.get('usertype') or 'customer'
        rows, created = service.ingest_file(actor=userid, actor_type=actor_type, payload=str(payload))
        owner = rows[-1].userid if rows and rows[-1].userid else userid
        return jsonify({
            'message': 'CHIP1 file ingested',
            'created': created,
            'inbounds': [row.to_dict() for row in rows],
            'ChipsIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_assign(service: CrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise CrecvError('missing_inbound', 'inbound_id is required.')
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
            'message': 'CHIPS assigned',
            'inbound': row.to_dict(),
            'ChipsIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_inbound_route(service: CrecvService, action: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise CrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        if action == 'override':
            row = service.override_ofac(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'release':
            row = service.release(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            message = 'CHIPS released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'CHIPS rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=str(values.get('reason') or 'cust'),
            )
            message = 'CHIPS returned'
        elif action == 'settle':
            row = service.settle(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            message = 'CHIPS settled'
        elif action == 'export':
            exported = service.export(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            owner = exported['inbound'].get('userid') or userid
            return jsonify({
                'message': 'CHIPS exported',
                'file': exported['file'],
                'inbound': exported['inbound'],
                'ChipsIn': _snapshot_for(service, owner, userid, actor_type),
            }), 200
        else:
            raise CrecvError('invalid_status', 'Unknown CHIPS action.')
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'ChipsIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: CrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise CrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.request_return(
            inbound_id=inbound_id, actor=userid, actor_type=actor_type,
            reason=str(values.get('reason') or 'cust'),
        )
        return jsonify({
            'message': 'CHIPS return requested',
            'inbound': row.to_dict(),
            'ChipsIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: CrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'ChipsIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def attach_crecv_routes(app, service: CrecvService) -> None:
    @app.route('/listChipsIns', methods=['POST', 'GET'])
    def list_chips_ins_route():
        return handle_list(service)

    @app.route('/listChipsUids', methods=['POST', 'GET'])
    def list_chips_uids_route():
        return handle_list(service)

    @app.route('/listUnmatchedChipsIns', methods=['POST', 'GET'])
    def list_unmatched_chips_ins_route():
        return handle_unmatched(service)

    @app.route('/addChipsUid', methods=['POST', 'GET'])
    def add_chips_uid_route():
        return handle_add_uid(service)

    @app.route('/pauseChipsUid', methods=['POST', 'GET'])
    def pause_chips_uid_route():
        return _uid_status_route(service, UID_PAUSED, 'CHIPS UID paused')

    @app.route('/resumeChipsUid', methods=['POST', 'GET'])
    def resume_chips_uid_route():
        return _uid_status_route(service, UID_ACTIVE, 'CHIPS UID resumed')

    @app.route('/cancelChipsUid', methods=['POST', 'GET'])
    def cancel_chips_uid_route():
        return _uid_status_route(service, UID_CANCELLED, 'CHIPS UID cancelled')

    @app.route('/previewChipsIn', methods=['POST', 'GET'])
    def preview_chips_in_route():
        return handle_preview(service)

    @app.route('/ingestChipsIn', methods=['POST', 'GET'])
    def ingest_chips_in_route():
        return handle_ingest(service)

    @app.route('/ingestChipsInFile', methods=['POST', 'GET'])
    def ingest_chips_in_file_route():
        return handle_ingest_file(service)

    @app.route('/assignChipsIn', methods=['POST', 'GET'])
    def assign_chips_in_route():
        return handle_assign(service)

    @app.route('/overrideChipsInOfac', methods=['POST', 'GET'])
    def override_chips_in_ofac_route():
        return _staff_inbound_route(service, 'override')

    @app.route('/releaseChipsIn', methods=['POST', 'GET'])
    def release_chips_in_route():
        return _staff_inbound_route(service, 'release')

    @app.route('/rejectChipsIn', methods=['POST', 'GET'])
    def reject_chips_in_route():
        return _staff_inbound_route(service, 'reject')

    @app.route('/returnChipsIn', methods=['POST', 'GET'])
    def return_chips_in_route():
        return _staff_inbound_route(service, 'return')

    @app.route('/settleChipsIn', methods=['POST', 'GET'])
    def settle_chips_in_route():
        return _staff_inbound_route(service, 'settle')

    @app.route('/exportChipsIn', methods=['POST', 'GET'])
    def export_chips_in_route():
        return _staff_inbound_route(service, 'export')

    @app.route('/requestChipsInReturn', methods=['POST', 'GET'])
    def request_chips_in_return_route():
        return handle_request_return(service)

    @app.route('/runDueChipsIns', methods=['POST', 'GET'])
    def run_due_chips_ins_route():
        return handle_run_due(service)
