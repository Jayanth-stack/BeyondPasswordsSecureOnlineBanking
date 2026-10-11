"""Outbound Zelle P2P origination.

Customers send USD Zelle payments to email or SMS directory tokens.
Enrolled tokens post 24/7 and are irrevocable for the sender; unenrolled
request tokens sit until the recipient enrolls or the payment expires.
Independent of outbound Interac CAD (PR #113), inbound Interac (PR #111),
lockbox BAI2 (PR #121), Positive Pay (PR #119), Check21 ICL/OCL
(PR #115/#117), ACH linking (PR #68), and domestic Fedwire (PR #73).
Existing `/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- US NANP email / SMS token directory with SHA-256 fingerprints
- Zelle payment id (ZEL + YYYYMMDD + seq)
- Early Warning participant ABA
- USD-only 24/7 Zelle clock (Fed holidays reported, no cutoff queue)
- ZEL1 pipe file compose/parse (XML/DOCTYPE rejected)

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Full token values never appear in to_dict / snapshots.
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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    DEFAULT_WATCHLIST,
    AccountError,
    AmountError,
    ScreenResult,
    aba_check_digit_ok,
    account_types_from_customer_payload,
    money_str,
    normalize_account,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
    us_fed_holidays,
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
    'email': KIND_EMAIL, 'mail': KIND_EMAIL, 'e-mail': KIND_EMAIL, 'token': KIND_EMAIL,
    'sms': KIND_SMS, 'phone': KIND_SMS, 'mobile': KIND_SMS, 'text': KIND_SMS, 'tel': KIND_SMS,
}
RAIL_ENROLLED = 'enrolled'
RAIL_REQUEST = 'request'
RAILS = frozenset({RAIL_ENROLLED, RAIL_REQUEST})
RAIL_ALIASES = {
    'enrolled': RAIL_ENROLLED, 'enroll': RAIL_ENROLLED, 'instant': RAIL_ENROLLED,
    'directory': RAIL_ENROLLED, 'registered': RAIL_ENROLLED, 'zelle': RAIL_ENROLLED,
    'request': RAIL_REQUEST, 'unenrolled': RAIL_REQUEST, 'invite': RAIL_REQUEST,
    'pending': RAIL_REQUEST, 'claim': RAIL_REQUEST,
}
ZELLE_HELD = 'held'
ZELLE_PENDING = 'pending_release'
ZELLE_CLAIM = 'pending_claim'
ZELLE_COMPLETED = 'completed'
ZELLE_REJECTED = 'rejected'
ZELLE_CANCELLED = 'cancelled'
ZELLE_RECALLED = 'recalled'
ZELLE_EXPIRED = 'expired'
ZELLE_NSF = 'nsf'
ZELLE_FAILED = 'failed'
ZELLE_STATUSES = frozenset({
    ZELLE_HELD, ZELLE_PENDING, ZELLE_CLAIM, ZELLE_COMPLETED, ZELLE_REJECTED,
    ZELLE_CANCELLED, ZELLE_RECALLED, ZELLE_EXPIRED, ZELLE_NSF, ZELLE_FAILED,
})
OPEN_ZELLES = frozenset({ZELLE_HELD, ZELLE_PENDING, ZELLE_CLAIM})
CANCELABLE = frozenset({ZELLE_HELD, ZELLE_PENDING})
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
DEFAULT_STORE_PATH = 'SystemLogs/zelle.sqlite'
DEFAULT_SENDER = '021000021'
DEFAULT_NETWORK = 'ZELLE'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)
EMAIL_RE = re.compile(r'^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,24}$')
PAY_RE = re.compile(r'^ZEL\d{12}$')


class ZelleError(ValueError):
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


def normalize_participant(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise ZelleError('invalid_routing', 'Zelle participant ABA required.')
        return ''
    if len(digits) < 9:
        digits = digits.zfill(9)
    if len(digits) != 9 or digits == '000000000' or not aba_check_digit_ok(digits):
        raise ZelleError('invalid_routing', 'Zelle participant ABA failed checksum.')
    return digits


def mask_participant(routing: str) -> str:
    digits = re.sub(r'\D', '', str(routing or ''))
    if len(digits) < 4:
        return '****'
    return digits[:4] + '****' + digits[-1:]


def normalize_email(value: Any) -> str:
    text = str(value or '').strip().lower()
    if not EMAIL_RE.match(text) or '..' in text or text.startswith('.') or text.endswith('.'):
        raise ZelleError('invalid_alias', 'Contact email is not valid.')
    local, domain = text.split('@', 1)
    if not (1 <= len(local) <= 64) or not (3 <= len(domain) <= 80):
        raise ZelleError('invalid_alias', 'Contact email is not valid.')
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
    if len(digits) != 10 or digits[0] in '01' or digits[3] in '01':
        raise ZelleError('invalid_alias', 'Contact phone must be a US NANP number.')
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
        raise ZelleError('invalid_alias', 'Token must be email or sms.')
    return text


def normalize_alias_value(kind: str, value: Any) -> str:
    if kind == KIND_EMAIL:
        return normalize_email(value)
    return normalize_phone(value)


def mask_alias(kind: str, value: str) -> str:
    if kind == KIND_EMAIL:
        return mask_email(value)
    return mask_phone(value)


def token_fingerprint(kind: str, value: str) -> str:
    material = ('%s\0%s' % (kind, value)).encode('utf-8')
    return hashlib.sha256(material).hexdigest()


def normalize_rail(value: Any, *, default: str = RAIL_ENROLLED) -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text not in RAILS:
        raise ZelleError('invalid_rail', 'Rail must be enrolled or request.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise ZelleError('invalid_name', 'Contact legal name must be 2-80 characters.')
    return text


def normalize_nickname(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise ZelleError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def compose_payment_id(cycle_date: str, sequence: int) -> str:
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise ZelleError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 9999:
        raise ZelleError('invalid_reference', 'Zelle sequence out of range.')
    return 'ZEL%s%04d' % (day, seq)


def normalize_payment_id(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        if required:
            raise ZelleError('invalid_reference', 'Zelle payment id required.')
        return ''
    if not PAY_RE.match(text):
        raise ZelleError('invalid_reference', 'Zelle payment id must be ZEL + 12 digits.')
    return text


def mask_payment_id(value: str) -> str:
    text = str(value or '')
    if len(text) < 8:
        return 'ZEL********'
    return text[:6] + '*****' + text[-4:]


def normalize_currency(value: Any) -> str:
    text = str(value or 'USD').strip().upper()
    if text != 'USD':
        raise ZelleError('invalid_currency', 'Zelle origination must be USD.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise ZelleError('invalid_purpose', 'Unknown Zelle purpose.')
    return text


def normalize_network(value: Any, *, default: str = DEFAULT_NETWORK) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or default).upper())
    if text not in {'ZELLE', 'EWS', 'EARLYWARNING'}:
        raise ZelleError('invalid_file', 'Unknown Zelle network.')
    return 'ZELLE'


class ZelleClock:
    """Fed holiday calendar. Origination posts 24/7; snapshot still reports holidays."""

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
        return day in us_fed_holidays(day.year) or day in self.extra_holidays

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
    if '<?xml' in low or '<!doctype' in low or '<!entity' in low or '<zelle' in low:
        raise ZelleError('invalid_file', 'XML/DOCTYPE Zelle files are rejected.')


def compose_zel1(
    *,
    payment_id: str,
    amount: Decimal,
    recipient_name: str,
    sender_fi: str,
    kind: str,
    alias: str,
    rail: str,
    expiry: str = '',
    network: str = DEFAULT_NETWORK,
) -> str:
    fields = [
        'ZEL1',
        normalize_payment_id(payment_id),
        money_str(amount),
        'USD',
        normalize_legal_name(recipient_name).replace('|', ' '),
        normalize_participant(sender_fi),
        kind,
        alias,
        rail,
        str(expiry or ''),
        normalize_network(network),
    ]
    return '|'.join(fields)


def parse_zel1(record: str) -> Dict[str, Any]:
    text = str(record or '').strip()
    _reject_xml(text)
    if not text or text.startswith('#'):
        raise ZelleError('invalid_file', 'Empty Zelle record.')
    parts = text.split('|')
    if len(parts) != 11 or parts[0] != 'ZEL1':
        raise ZelleError('invalid_file', 'ZEL1 records must have 11 pipe fields.')
    kind = normalize_kind(parts[6])
    rail = normalize_rail(parts[8])
    return {
        'payment_id': normalize_payment_id(parts[1]),
        'amount': parse_money(parts[2]),
        'currency': normalize_currency(parts[3]),
        'recipient_name': normalize_legal_name(parts[4]),
        'sender_fi': normalize_participant(parts[5]),
        'alias_type': kind,
        'alias': normalize_alias_value(kind, parts[7]),
        'rail': rail,
        'expiry': str(parts[9] or '').strip(),
        'network': normalize_network(parts[10]),
    }


def split_zel1_file(payload: str) -> List[str]:
    text = str(payload or '')
    _reject_xml(text)
    records = []
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        records.append(stripped)
    if not records:
        raise ZelleError('invalid_file', 'Zelle file had no ZEL1 records.')
    return records


def compute_fee(amount: Decimal, rail: str, enrolled_fee: Decimal, request_fee: Decimal, *, waived: bool = False) -> Decimal:
    """Flat per-rail outbound fee; waived or default-zero transfers cost $0."""
    if waived:
        return Decimal('0.00')
    _ = amount
    fee = enrolled_fee if rail == RAIL_ENROLLED else request_fee
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
class ZellePolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    allow_credit: bool = False
    max_contacts: int = 12
    max_transfers: int = 120
    min_amount: Decimal = Decimal('0.01')
    enrolled_cap: Decimal = Decimal('15000.00')
    request_cap: Decimal = Decimal('2500.00')
    enrolled_fee: Decimal = Decimal('0.00')
    request_fee: Decimal = Decimal('0.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    expiry_days: int = 14
    return_hours: int = 24
    tz_offset_hours: int = -4
    sender_routing: str = DEFAULT_SENDER
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'ZellePolicy':
        extra = _env_list('ZELLE_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        routing = os.environ.get('ZELLE_SENDER') or DEFAULT_SENDER
        return cls(
            enabled=_env_bool('ZELLE_ENABLED', True),
            customer_manage=_env_bool('ZELLE_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('ZELLE_CUSTOMER_SEND', True),
            allow_credit=_env_bool('ZELLE_ALLOW_CREDIT', False),
            max_contacts=max(1, _env_int('ZELLE_MAX_CONTACTS', 12)),
            max_transfers=max(1, _env_int('ZELLE_MAX_TRANSFERS', 120)),
            min_amount=_env_money('ZELLE_MIN_AMOUNT', '0.01'),
            enrolled_cap=_env_money('ZELLE_ENROLLED_CAP', '15000.00'),
            request_cap=_env_money('ZELLE_REQUEST_CAP', '2500.00'),
            enrolled_fee=_env_money('ZELLE_ENROLLED_FEE', '0.00'),
            request_fee=_env_money('ZELLE_REQUEST_FEE', '0.00'),
            dual_control_threshold=_env_money('ZELLE_DUAL_CONTROL', '10000.00'),
            expiry_days=max(1, _env_int('ZELLE_EXPIRY_DAYS', 14)),
            return_hours=max(1, _env_int('ZELLE_RETURN_HOURS', 24)),
            tz_offset_hours=_env_int('ZELLE_TZ_OFFSET', -4),
            sender_routing=normalize_participant(routing),
            watchlist=watch,
            extra_holidays=_env_list('ZELLE_HOLIDAYS'),
        )


@dataclass
class ZelleContact:
    contact_id: str
    userid: str
    nickname: str
    legal_name: str
    kind: str
    alias_value: str
    fingerprint: str
    rail: str
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
class ZelleTransfer:
    transfer_id: str
    trace_id: str
    contact_id: str
    userid: str
    internal_account: str
    amount: str
    fee: str
    fee_status: str
    nickname: str
    legal_name: str
    alias_kind: str
    alias_masked: str
    rail: str
    purpose: str
    memo: str
    status: str
    payment_id: str
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
            'amount': self.amount,
            'fee': self.fee,
            'fee_status': self.fee_status,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'kind': self.alias_kind,
            'alias_masked': self.alias_masked,
            'rail': self.rail,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'payment_id_masked': mask_payment_id(self.payment_id) if self.payment_id else '',
            'sender_fi_masked': mask_participant(self.sender_fi),
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
            'held': self.status == ZELLE_HELD,
            'pending_release': self.status == ZELLE_PENDING,
            'pending_claim': self.status == ZELLE_CLAIM,
            'completed': self.status == ZELLE_COMPLETED,
            'cancelable': self.status in CANCELABLE or self.status == ZELLE_CLAIM,
        }


def _clone_contact(row: ZelleContact) -> ZelleContact:
    return ZelleContact(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_transfer(row: ZelleTransfer) -> ZelleTransfer:
    return ZelleTransfer(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _contact_from_row(row: Any) -> ZelleContact:
    return ZelleContact(
        contact_id=row['contact_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        kind=row['kind'],
        alias_value=row['alias_value'],
        fingerprint=row['fingerprint'],
        rail=row['rail'],
        default_account=row['default_account'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
    )


def _transfer_from_row(row: Any) -> ZelleTransfer:
    return ZelleTransfer(
        transfer_id=row['transfer_id'],
        trace_id=row['trace_id'],
        contact_id=row['contact_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        amount=row['amount'],
        fee=row['fee'],
        fee_status=row['fee_status'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        alias_kind=row['alias_kind'],
        alias_masked=row['alias_masked'],
        rail=row['rail'],
        purpose=row['purpose'],
        memo=row['memo'] or '',
        status=row['status'],
        payment_id=row['payment_id'] or '',
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


class MemoryZelleStore:
    def __init__(self) -> None:
        self._contacts: Dict[str, ZelleContact] = {}
        self._transfers: Dict[str, ZelleTransfer] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_contact(self, row: ZelleContact) -> None:
        with self._lock:
            self._contacts[row.contact_id] = row

    def get_contact(self, contact_id: str) -> Optional[ZelleContact]:
        with self._lock:
            row = self._contacts.get(contact_id)
            return _clone_contact(row) if row else None

    def update_contact(self, row: ZelleContact) -> None:
        with self._lock:
            self._contacts[row.contact_id] = row

    def list_contacts(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[ZelleContact]:
        with self._lock:
            rows = [_clone_contact(row) for row in self._contacts.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != CONTACT_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_contact_by_nickname(self, userid: str, nickname: str) -> Optional[ZelleContact]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._contacts.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_CONTACT:
                    return _clone_contact(row)
        return None

    def find_contact_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[ZelleContact]:
        with self._lock:
            for row in self._contacts.values():
                if row.userid == userid and row.fingerprint == fingerprint and row.status in OPEN_CONTACT:
                    return _clone_contact(row)
        return None

    def put_transfer(self, row: ZelleTransfer) -> ZelleTransfer:
        with self._lock:
            existing_id = self._by_trace.get(row.trace_id)
            if existing_id is not None:
                return self._transfers[existing_id]
            self._transfers[row.transfer_id] = row
            self._by_trace[row.trace_id] = row.transfer_id
            return row

    def update_transfer(self, row: ZelleTransfer) -> None:
        with self._lock:
            self._transfers[row.transfer_id] = row

    def get_transfer(self, transfer_id: str) -> Optional[ZelleTransfer]:
        with self._lock:
            row = self._transfers.get(transfer_id)
            return _clone_transfer(row) if row else None

    def get_transfer_by_trace(self, trace_id: str) -> Optional[ZelleTransfer]:
        with self._lock:
            transfer_id = self._by_trace.get(trace_id)
            return _clone_transfer(self._transfers[transfer_id]) if transfer_id else None

    def list_transfers(self, userid: Optional[str] = None, contact_id: Optional[str] = None) -> List[ZelleTransfer]:
        with self._lock:
            rows = [_clone_transfer(row) for row in self._transfers.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if contact_id is not None:
            rows = [row for row in rows if row.contact_id == contact_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def next_payment_sequence(self, cycle_date: str) -> int:
        prefix = 'ZEL' + cycle_date
        with self._lock:
            used = [row.payment_id for row in self._transfers.values() if row.payment_id.startswith(prefix)]
        return len(used) + 1


class SqliteZelleStore:
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
                    amount TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    fee_status TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    alias_kind TEXT NOT NULL,
                    alias_masked TEXT NOT NULL,
                    rail TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    payment_id TEXT NOT NULL DEFAULT '',
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

    def put_contact(self, row: ZelleContact) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO contacts (
                    contact_id, userid, nickname, legal_name, kind, alias_value,
                    fingerprint, rail, default_account, status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.contact_id, row.userid, row.nickname, row.legal_name, row.kind,
                    row.alias_value, row.fingerprint, row.rail, row.default_account,
                    row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_contact(self, contact_id: str) -> Optional[ZelleContact]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM contacts WHERE contact_id = ?', (contact_id,),
            ).fetchone()
        return _contact_from_row(row) if row else None

    def update_contact(self, row: ZelleContact) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE contacts SET nickname=?, legal_name=?, kind=?, alias_value=?,
                    fingerprint=?, rail=?, default_account=?, status=?, actor=?, updated_at=?
                WHERE contact_id=?
                """,
                (
                    row.nickname, row.legal_name, row.kind, row.alias_value, row.fingerprint,
                    row.rail, row.default_account, row.status, row.actor, row.updated_at,
                    row.contact_id,
                ),
            )
            conn.commit()

    def list_contacts(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[ZelleContact]:
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

    def find_contact_by_nickname(self, userid: str, nickname: str) -> Optional[ZelleContact]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM contacts
                WHERE userid = ? AND lower(nickname) = lower(?) AND status IN ('active', 'paused')
                """,
                (userid, nickname),
            ).fetchone()
        return _contact_from_row(row) if row else None

    def find_contact_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[ZelleContact]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM contacts
                WHERE userid = ? AND fingerprint = ? AND status IN ('active', 'paused')
                """,
                (userid, fingerprint),
            ).fetchone()
        return _contact_from_row(row) if row else None

    def put_transfer(self, row: ZelleTransfer) -> ZelleTransfer:
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
                    amount, fee, fee_status, nickname, legal_name, alias_kind,
                    alias_masked, rail, purpose, memo, status, payment_id, sender_fi,
                    value_date, expiry_date, actor, releaser, ofac_hit, ofac_match,
                    created_at, updated_at, sent_at, completed_at, recalled_at, note, reason
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                self._transfer_values(row),
            )
            conn.commit()
            return row

    def update_transfer(self, row: ZelleTransfer) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE transfers SET contact_id=?, userid=?, internal_account=?,
                    amount=?, fee=?, fee_status=?, nickname=?, legal_name=?,
                    alias_kind=?, alias_masked=?, rail=?, purpose=?, memo=?,
                    status=?, payment_id=?, sender_fi=?, value_date=?, expiry_date=?,
                    actor=?, releaser=?, ofac_hit=?, ofac_match=?, updated_at=?,
                    sent_at=?, completed_at=?, recalled_at=?, note=?, reason=?
                WHERE transfer_id=?
                """,
                (
                    row.contact_id, row.userid, row.internal_account, row.amount,
                    row.fee, row.fee_status, row.nickname, row.legal_name,
                    row.alias_kind, row.alias_masked, row.rail, row.purpose,
                    row.memo, row.status, row.payment_id, row.sender_fi,
                    row.value_date, row.expiry_date, row.actor, row.releaser,
                    row.ofac_hit, row.ofac_match, row.updated_at, row.sent_at,
                    row.completed_at, row.recalled_at, row.note, row.reason,
                    row.transfer_id,
                ),
            )
            conn.commit()

    def get_transfer(self, transfer_id: str) -> Optional[ZelleTransfer]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM transfers WHERE transfer_id = ?', (transfer_id,),
            ).fetchone()
        return _transfer_from_row(row) if row else None

    def get_transfer_by_trace(self, trace_id: str) -> Optional[ZelleTransfer]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM transfers WHERE trace_id = ?', (trace_id,),
            ).fetchone()
        return _transfer_from_row(row) if row else None

    def list_transfers(self, userid: Optional[str] = None, contact_id: Optional[str] = None) -> List[ZelleTransfer]:
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

    def next_payment_sequence(self, cycle_date: str) -> int:
        prefix = 'ZEL' + cycle_date + '%'
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT COUNT(*) AS n FROM transfers WHERE payment_id LIKE ?', (prefix,),
            ).fetchone()
        return int(row['n'] if row else 0) + 1

    @staticmethod
    def _transfer_values(row: ZelleTransfer) -> Tuple[Any, ...]:
        return (
            row.transfer_id, row.trace_id, row.contact_id, row.userid, row.internal_account,
            row.amount, row.fee, row.fee_status, row.nickname, row.legal_name,
            row.alias_kind, row.alias_masked, row.rail, row.purpose, row.memo,
            row.status, row.payment_id, row.sender_fi, row.value_date, row.expiry_date,
            row.actor, row.releaser, row.ofac_hit, row.ofac_match, row.created_at,
            row.updated_at, row.sent_at, row.completed_at, row.recalled_at, row.note,
            row.reason,
        )


class ZelleService:
    def __init__(
        self,
        policy: ZellePolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[ZelleClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: datetime.now(tz=timezone.utc).timestamp())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or ZelleClock(
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise ZelleError('zelle_disabled', 'Zelle origination is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise ZelleError('zelle_forbidden', 'Customers cannot manage Zelle contacts.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise ZelleError('zelle_forbidden', 'Customers cannot send Zelle payments.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise ZelleError('zelle_forbidden', 'Staff only.')

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
            raise ZelleError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise ZelleError('credit_not_allowed', 'Credit accounts cannot originate Zelle payments.')

    def _assert_amount(self, amount: Decimal, rail: str) -> None:
        cap = self.policy.enrolled_cap if rail == RAIL_ENROLLED else self.policy.request_cap
        if amount < self.policy.min_amount:
            raise ZelleError('amount_out_of_range', 'Amount is outside the allowed range.')
        if amount > cap:
            code = 'enrolled_amount_exceeded' if rail == RAIL_ENROLLED else 'request_amount_exceeded'
            raise ZelleError(code, 'Amount exceeds the %s cap.' % rail)

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, usd: Decimal) -> bool:
        return usd >= self.policy.dual_control_threshold

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
        rail: Any = RAIL_ENROLLED,
        default_account: Any,
    ) -> ZelleContact:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise ZelleError('zelle_forbidden', 'Not allowed to add contacts for this customer.')
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        alias_kind = normalize_kind(kind)
        alias_value = normalize_alias_value(alias_kind, alias)
        contact_rail = normalize_rail(rail)
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        if self.store.find_contact_by_nickname(owner_userid, name) is not None:
            raise ZelleError('contact_duplicate', 'A contact with that nickname already exists.')
        fingerprint = token_fingerprint(alias_kind, alias_value)
        if self.store.find_contact_by_fingerprint(owner_userid, fingerprint) is not None:
            raise ZelleError('contact_duplicate', 'That Zelle token is already on file.')
        open_rows = [row for row in self.store.list_contacts(owner_userid) if row.status in OPEN_CONTACT]
        if len(open_rows) >= self.policy.max_contacts:
            raise ZelleError('contact_limit', 'Zelle contact limit reached.')
        now = float(self.clock())
        row = ZelleContact(
            contact_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            kind=alias_kind,
            alias_value=alias_value,
            fingerprint=fingerprint,
            rail=contact_rail,
            default_account=account,
            status=CONTACT_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_contact(row)
        return row

    def get_contact(self, *, contact_id: str, actor: str, actor_type: str) -> ZelleContact:
        self._require_enabled()
        row = self.store.get_contact(contact_id)
        if row is None:
            raise ZelleError('contact_not_found', 'Zelle contact not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise ZelleError('zelle_forbidden', 'Not allowed to view this contact.')
        return row

    def enforce_contact(
        self,
        *,
        contact_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> ZelleContact:
        row = self.get_contact(contact_id=contact_id, actor=actor, actor_type=actor_type)
        if row.status == CONTACT_ARCHIVED:
            raise ZelleError('already_archived', 'Contact is archived.')
        if row.status == CONTACT_PAUSED:
            raise ZelleError('contact_paused', 'Contact is paused.')
        if require_active and row.status != CONTACT_ACTIVE:
            raise ZelleError('invalid_status', 'Contact is not active.')
        return row

    def set_contact_status(
        self,
        *,
        contact_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> ZelleContact:
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
            raise ZelleError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == CONTACT_ARCHIVED:
            raise ZelleError('already_archived', 'Contact is already archived.')
        if wanted == CONTACT_PAUSED:
            if row.status == CONTACT_PAUSED:
                raise ZelleError('already_paused', 'Contact is already paused.')
            if row.status != CONTACT_ACTIVE:
                raise ZelleError('invalid_status', 'Only an active contact can be paused.')
        elif wanted == CONTACT_ACTIVE:
            if row.status == CONTACT_ACTIVE:
                raise ZelleError('already_active', 'Contact is already active.')
            if row.status != CONTACT_PAUSED:
                raise ZelleError('invalid_status', 'Only a paused contact can be resumed.')
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
            raise ZelleError('zelle_forbidden', 'Contact does not belong to this customer.')
        usd = parse_money(amount)
        self._assert_amount(usd, contact.rail)
        account = normalize_account(internal_account or contact.default_account)
        self._assert_internal_account(owner_userid, account)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(
            usd, contact.rail, self.policy.enrolled_fee, self.policy.request_fee, waived=staff_waive,
        )
        now = float(self.clock())
        ofac = self._screen(contact.legal_name, aliases=(contact.nickname,))
        return {
            'amount': money_str(usd),
            'currency': 'USD',
            'fee': money_str(fee),
            'total': money_str(usd + fee),
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
        contact: ZelleContact,
        account: str,
        usd: Decimal,
        fee: Decimal,
        purpose: str,
        memo: str,
        trace_id: str,
        ofac: ScreenResult,
        waive_fee: bool,
    ) -> ZelleTransfer:
        now = float(self.clock())
        value = self.calendar.cycle_date(now)
        expiry = ''
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        if ofac.hit:
            status = ZELLE_HELD
        elif self._needs_dual_control(usd):
            status = ZELLE_PENDING
        elif contact.rail == RAIL_REQUEST:
            status = ZELLE_CLAIM
        else:
            status = ZELLE_COMPLETED
        if contact.rail == RAIL_REQUEST:
            expiry = (self.calendar.local_dt(now).date() + timedelta(days=self.policy.expiry_days)).isoformat()
        transfer = ZelleTransfer(
            transfer_id=uuid.uuid4().hex,
            trace_id=trace_id,
            contact_id=contact.contact_id,
            userid=owner_userid,
            internal_account=account,
            amount=money_str(usd),
            fee=money_str(fee),
            fee_status=fee_status,
            nickname=contact.nickname,
            legal_name=contact.legal_name,
            alias_kind=contact.kind,
            alias_masked=mask_alias(contact.kind, contact.alias_value),
            rail=contact.rail,
            purpose=purpose,
            memo=memo,
            status=status,
            payment_id='',
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
        if status in {ZELLE_CLAIM, ZELLE_COMPLETED}:
            self._transmit(transfer, contact=contact, actor=actor)
        return transfer

    def _transmit(self, transfer: ZelleTransfer, *, contact: ZelleContact, actor: str) -> ZelleTransfer:
        now = float(self.clock())
        cycle = transfer.value_date or self.calendar.cycle_date(now)
        seq = self.store.next_payment_sequence(cycle)
        transfer.payment_id = compose_payment_id(cycle, seq)
        usd = parse_money(transfer.amount)
        fee = parse_money(transfer.fee, allow_zero=True)
        remark = 'zelle to %s' % transfer.nickname
        landing = ZELLE_COMPLETED if transfer.rail == RAIL_ENROLLED else ZELLE_CLAIM
        status = landing
        fail_note = ''
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(transfer.internal_account, money_str(usd), remark)
            except Exception as exc:
                status = ZELLE_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = ZELLE_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = ZELLE_FAILED
                    fail_note = str(result)[:240]
        if status == landing and fee > 0 and transfer.fee_status != FEE_WAIVED and self.debit_fn is not None:
            try:
                fee_result = self.debit_fn(
                    transfer.internal_account, money_str(fee), 'zelle fee %s' % transfer.payment_id[-12:],
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
            if status == ZELLE_COMPLETED:
                transfer.completed_at = now
            _ = compose_zel1(
                payment_id=transfer.payment_id,
                amount=parse_money(transfer.amount),
                recipient_name=transfer.legal_name,
                sender_fi=transfer.sender_fi,
                kind=contact.kind,
                alias=contact.alias_value,
                rail=transfer.rail,
                expiry=transfer.expiry_date.replace('-', '') if transfer.expiry_date else '',
            )
        else:
            transfer.payment_id = ''
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
    ) -> Tuple[ZelleTransfer, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise ZelleError('zelle_forbidden', 'Not allowed to originate Zelle for this customer.')
        contact = self.enforce_contact(
            contact_id=str(contact_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if contact.userid != owner_userid:
            raise ZelleError('zelle_forbidden', 'Contact does not belong to this customer.')
        usd = parse_money(amount)
        self._assert_amount(usd, contact.rail)
        account = normalize_account(internal_account or contact.default_account)
        self._assert_internal_account(owner_userid, account)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(
            usd, contact.rail, self.policy.enrolled_fee, self.policy.request_fee, waived=staff_waive,
        )
        trace = normalize_id(trace_id)
        existing = self.store.get_transfer_by_trace(trace)
        if existing is not None:
            return existing, False
        if len(self.store.list_transfers(owner_userid)) >= self.policy.max_transfers:
            raise ZelleError('transfer_limit', 'Zelle history limit reached.')
        ofac = self._screen(contact.legal_name, aliases=(contact.nickname,))
        transfer = self._place(
            owner_userid=owner_userid,
            actor=actor,
            contact=contact,
            account=account,
            usd=usd,
            fee=fee,
            purpose=normalize_purpose(purpose),
            memo=normalize_note(memo, limit=140),
            trace_id=trace,
            ofac=ofac,
            waive_fee=staff_waive,
        )
        stored = self.store.put_transfer(transfer)
        if stored.transfer_id != transfer.transfer_id:
            return stored, False
        if stored.status == ZELLE_NSF:
            raise ZelleError('nsf', 'Insufficient funds for Zelle.', transfer=stored)
        if stored.status == ZELLE_FAILED:
            raise ZelleError('failed', 'Zelle debit did not complete.', transfer=stored)
        return stored, True

    def get_transfer(self, *, transfer_id: str, actor: str, actor_type: str) -> ZelleTransfer:
        self._require_enabled()
        row = self.store.get_transfer(transfer_id)
        if row is None:
            raise ZelleError('transfer_not_found', 'Zelle transfer not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise ZelleError('zelle_forbidden', 'Not allowed to view this transfer.')
        return row

    def _credit_back(self, transfer: ZelleTransfer, *, reason: str) -> None:
        usd = parse_money(transfer.amount)
        fee = parse_money(transfer.fee, allow_zero=True)
        ref = (transfer.payment_id or transfer.transfer_id)[-12:]
        if self.credit_fn is None:
            return
        result = self.credit_fn(transfer.internal_account, money_str(usd), 'zelle return %s' % ref)
        kind = _classify_money_result(result)
        if kind == 'nsf':
            raise ZelleError('nsf', 'Could not credit Zelle return.', transfer=transfer)
        if kind != 'ok':
            raise ZelleError('recall_failed', 'Zelle return credit failed.', transfer=transfer)
        if transfer.fee_status == FEE_COLLECTED and fee > 0:
            fee_result = self.credit_fn(transfer.internal_account, money_str(fee), 'zelle fee return %s' % ref)
            if _classify_money_result(fee_result) != 'ok':
                raise ZelleError('recall_failed', 'Zelle fee return failed.', transfer=transfer)
        transfer.reason = reason

    def cancel_transfer(
        self,
        *,
        transfer_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> ZelleTransfer:
        self._require_send(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and transfer.userid != actor:
            raise ZelleError('zelle_forbidden', 'Not allowed to cancel this transfer.')
        if transfer.status == ZELLE_CLAIM:
            self._credit_back(transfer, reason='unclaimed')
            transfer.status = ZELLE_CANCELLED
            transfer.recalled_at = float(self.clock())
        elif transfer.status in CANCELABLE:
            transfer.status = ZELLE_CANCELLED
        elif transfer.status == ZELLE_COMPLETED and transfer.rail == RAIL_ENROLLED:
            raise ZelleError('scheme_irrevocable', 'Enrolled Zelle cannot be cancelled after send.')
        else:
            raise ZelleError('not_cancelable', 'Only held, pending, or unclaimed Zelle can be cancelled.')
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
    ) -> ZelleTransfer:
        self._require_staff(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if transfer.status != ZELLE_HELD:
            raise ZelleError('not_overridable', 'Only held Zelle can be OFAC-overridden.')
        contact = self.store.get_contact(transfer.contact_id)
        if contact is None:
            raise ZelleError('contact_not_found', 'Zelle contact not found.')
        transfer.ofac_hit = 0
        transfer.note = normalize_note(note) or 'cleared'
        transfer.updated_at = float(self.clock())
        usd = parse_money(transfer.amount)
        if self._needs_dual_control(usd):
            transfer.status = ZELLE_PENDING
            self.store.update_transfer(transfer)
            return transfer
        landing = ZELLE_COMPLETED if transfer.rail == RAIL_ENROLLED else ZELLE_CLAIM
        transfer.status = landing
        self._transmit(transfer, contact=contact, actor=actor)
        self.store.update_transfer(transfer)
        if transfer.status == ZELLE_NSF:
            raise ZelleError('nsf', 'Insufficient funds for Zelle.', transfer=transfer)
        if transfer.status == ZELLE_FAILED:
            raise ZelleError('failed', 'Zelle debit did not complete.', transfer=transfer)
        return transfer

    def release_transfer(
        self,
        *,
        transfer_id: str,
        actor: str,
        actor_type: str,
    ) -> ZelleTransfer:
        self._require_staff(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if transfer.status == ZELLE_HELD:
            raise ZelleError('ofac_hold', 'OFAC hold must be overridden before release.')
        if transfer.status != ZELLE_PENDING:
            raise ZelleError('not_releasable', 'Only pending Zelle can be released.')
        if transfer.actor == str(actor):
            raise ZelleError('same_approver', 'A different employee must release this Zelle.')
        contact = self.store.get_contact(transfer.contact_id)
        if contact is None:
            raise ZelleError('contact_not_found', 'Zelle contact not found.')
        landing = ZELLE_COMPLETED if transfer.rail == RAIL_ENROLLED else ZELLE_CLAIM
        transfer.status = landing
        self._transmit(transfer, contact=contact, actor=actor)
        self.store.update_transfer(transfer)
        if transfer.status == ZELLE_NSF:
            raise ZelleError('nsf', 'Insufficient funds for Zelle.', transfer=transfer)
        if transfer.status == ZELLE_FAILED:
            raise ZelleError('failed', 'Zelle debit did not complete.', transfer=transfer)
        return transfer

    def reject_transfer(
        self,
        *,
        transfer_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> ZelleTransfer:
        self._require_staff(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if transfer.status not in {ZELLE_HELD, ZELLE_PENDING, ZELLE_CLAIM}:
            raise ZelleError('not_rejectable', 'Only open Zelle can be rejected.')
        if transfer.status == ZELLE_CLAIM:
            self._credit_back(transfer, reason='rejected')
            transfer.recalled_at = float(self.clock())
        transfer.status = ZELLE_REJECTED
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
    ) -> ZelleTransfer:
        self._require_staff(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if transfer.status == ZELLE_COMPLETED:
            raise ZelleError('already_completed', 'Zelle is already completed.')
        if transfer.status != ZELLE_CLAIM:
            raise ZelleError('not_completable', 'Only unclaimed Zelle can be marked enrolled.')
        now = float(self.clock())
        transfer.status = ZELLE_COMPLETED
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
    ) -> ZelleTransfer:
        self._require_staff(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        now = float(self.clock())
        if transfer.status == ZELLE_CLAIM:
            self._credit_back(transfer, reason='recall')
        elif transfer.status == ZELLE_COMPLETED:
            if transfer.rail == RAIL_REQUEST:
                raise ZelleError('scheme_irrevocable', 'Claimed Zelle cannot be recalled.')
            window = self.policy.return_hours * 3600
            if transfer.completed_at and (now - transfer.completed_at) > window:
                raise ZelleError('return_window_closed', 'Enrolled Zelle return window has closed.')
            self._credit_back(transfer, reason='recall')
        else:
            raise ZelleError('not_recallable', 'Only sent Zelle can be recalled.')
        transfer.status = ZELLE_RECALLED
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
    ) -> ZelleTransfer:
        self._require_staff(actor_type)
        transfer = self.get_transfer(transfer_id=transfer_id, actor=actor, actor_type=actor_type)
        if transfer.status not in {ZELLE_HELD, ZELLE_PENDING}:
            raise ZelleError('not_cancelable', 'Fee can only be waived before send.')
        transfer.fee = money_str(Decimal('0.00'))
        transfer.fee_status = FEE_WAIVED
        transfer.updated_at = float(self.clock())
        transfer.actor = str(actor)
        self.store.update_transfer(transfer)
        return transfer

    def run_due(self, userid: Optional[str] = None) -> List[ZelleTransfer]:
        now = float(self.clock())
        changed = []
        for transfer in self.store.list_transfers(userid):
            if transfer.status != ZELLE_CLAIM:
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
            except ZelleError:
                transfer.status = ZELLE_FAILED
                transfer.note = 'expiry credit failed'
                transfer.updated_at = now
                self.store.update_transfer(transfer)
                changed.append(transfer)
                continue
            transfer.status = ZELLE_EXPIRED
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
            usd = parse_money(row.amount, allow_zero=True)
            if row.status in {ZELLE_CLAIM, ZELLE_COMPLETED}:
                sent_ytd += usd
                if row.fee_status == FEE_COLLECTED:
                    fee_ytd += parse_money(row.fee, allow_zero=True)
            elif row.status in {ZELLE_RECALLED, ZELLE_EXPIRED, ZELLE_CANCELLED} and row.recalled_at:
                recalled += usd
        now = float(self.clock())
        payload = {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'enrolled_cap': money_str(self.policy.enrolled_cap),
            'request_cap': money_str(self.policy.request_cap),
            'enrolled_fee': money_str(self.policy.enrolled_fee),
            'request_fee': money_str(self.policy.request_fee),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clock': self.calendar.snapshot(now),
            'contacts': [row.to_dict() for row in contacts[:40]],
            'transfers': [row.to_dict() for row in transfers[:40]],
            'ytd_sent': money_str(sent_ytd),
            'ytd_fees': money_str(fee_ytd),
            'recalled_ytd': money_str(recalled),
            'active_count': sum(1 for row in contacts if row.status == CONTACT_ACTIVE),
            'open_count': sum(1 for row in transfers if row.status in OPEN_ZELLES),
        }
        _ = actor, actor_type
        return payload


_SERVICE: Optional[ZelleService] = None


def set_service(service: Optional[ZelleService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[ZelleService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('ZELLE_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryZelleStore()
    path = os.environ.get('ZELLE_DB', DEFAULT_STORE_PATH)
    return SqliteZelleStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[ZelleClock] = None,
) -> ZelleService:
    if store is None:
        store = default_store()
    return ZelleService(
        ZellePolicy.from_env(),
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
        'zelle_forbidden': 403,
        'zelle_disabled': 403,
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
        'invalid_routing': 400,
        'invalid_reference': 400,
        'invalid_currency': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_file': 400,
        'enrolled_amount_exceeded': 400,
        'request_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_contact': 400,
        'missing_transfer': 400,
    }.get(code, 400)


def _error_body(exc: ZelleError) -> Dict[str, Any]:
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
    except ZelleError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list_zelles(service: ZelleService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Zelle': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_add_contact(service: ZelleService):
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
            rail=values.get('rail') or RAIL_ENROLLED,
            default_account=values.get('default_account') or values.get('account') or values.get('from_account'),
        )
        return jsonify({
            'message': 'Zelle contact added',
            'contact': row.to_dict(),
            'Zelle': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _contact_status_route(service: ZelleService, status: str, ok_message: str):
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
            'Zelle': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: ZelleService):
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
            amount=values.get('amount'),
            internal_account=values.get('account') or values.get('default_account') or values.get('from_account'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({'preview': preview, 'Zelle': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: ZelleService):
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
            amount=values.get('amount'),
            internal_account=values.get('account') or values.get('default_account') or values.get('from_account'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({
            'message': 'Zelle originated' if created else 'Zelle already submitted',
            'transfer': transfer.to_dict(),
            'Zelle': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_cancel(service: ZelleService):
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
            'message': 'Zelle cancelled',
            'transfer': transfer.to_dict(),
            'Zelle': service.snapshot(transfer.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_zelle_route(service: ZelleService, action: str):
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
            message = 'Zelle released'
        elif action == 'reject':
            transfer = service.reject_transfer(
                transfer_id=transfer_id, actor=userid, actor_type=actor_type, note=values.get('note'),
            )
            message = 'Zelle rejected'
        elif action == 'complete':
            transfer = service.complete_transfer(transfer_id=transfer_id, actor=userid, actor_type=actor_type)
            message = 'Zelle claimed'
        elif action == 'recall':
            transfer = service.recall_transfer(
                transfer_id=transfer_id, actor=userid, actor_type=actor_type, note=values.get('note'),
            )
            message = 'Zelle recalled'
        elif action == 'override':
            transfer = service.override_ofac(
                transfer_id=transfer_id, actor=userid, actor_type=actor_type, note=values.get('note'),
            )
            message = 'OFAC overridden'
        elif action == 'waive':
            transfer = service.waive_fee(transfer_id=transfer_id, actor=userid, actor_type=actor_type)
            message = 'Zelle fee waived'
        else:
            raise ZelleError('invalid_status', 'Unknown staff action.')
        return jsonify({
            'message': message,
            'transfer': transfer.to_dict(),
            'Zelle': service.snapshot(transfer.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: ZelleService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Zelle': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_zelle_routes(app, service: ZelleService) -> None:
    @app.route('/listZelles', methods=['POST', 'GET'])
    def list_zelles_route():
        return handle_list_zelles(service)

    @app.route('/listZelleContacts', methods=['POST', 'GET'])
    def list_zelle_contacts_route():
        return handle_list_zelles(service)

    @app.route('/addZelleContact', methods=['POST', 'GET'])
    def add_zelle_contact_route():
        return handle_add_contact(service)

    @app.route('/pauseZelleContact', methods=['POST', 'GET'])
    def pause_zelle_contact_route():
        return _contact_status_route(service, CONTACT_PAUSED, 'Zelle contact paused')

    @app.route('/resumeZelleContact', methods=['POST', 'GET'])
    def resume_zelle_contact_route():
        return _contact_status_route(service, CONTACT_ACTIVE, 'Zelle contact resumed')

    @app.route('/archiveZelleContact', methods=['POST', 'GET'])
    def archive_zelle_contact_route():
        return _contact_status_route(service, CONTACT_ARCHIVED, 'Zelle contact archived')

    @app.route('/previewZelle', methods=['POST', 'GET'])
    def preview_zelle_route():
        return handle_preview(service)

    @app.route('/sendZelle', methods=['POST', 'GET'])
    def send_zelle_route():
        return handle_send(service)

    @app.route('/cancelZelle', methods=['POST', 'GET'])
    def cancel_zelle_route():
        return handle_cancel(service)

    @app.route('/releaseZelle', methods=['POST', 'GET'])
    def release_zelle_route():
        return _staff_zelle_route(service, 'release')

    @app.route('/rejectZelle', methods=['POST', 'GET'])
    def reject_zelle_route():
        return _staff_zelle_route(service, 'reject')

    @app.route('/completeZelle', methods=['POST', 'GET'])
    def complete_zelle_route():
        return _staff_zelle_route(service, 'complete')

    @app.route('/recallZelle', methods=['POST', 'GET'])
    def recall_zelle_route():
        return _staff_zelle_route(service, 'recall')

    @app.route('/overrideZelleOfac', methods=['POST', 'GET'])
    def override_zelle_ofac_route():
        return _staff_zelle_route(service, 'override')

    @app.route('/waiveZelleFee', methods=['POST', 'GET'])
    def waive_zelle_fee_route():
        return _staff_zelle_route(service, 'waive')

    @app.route('/runDueZelles', methods=['POST', 'GET'])
    def run_due_zelles_route():
        return handle_run_due(service)
