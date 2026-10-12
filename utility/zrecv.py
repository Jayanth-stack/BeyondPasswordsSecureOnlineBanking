"""Inbound Zelle receive posting (USD enrolled / request).

Staff ingest incoming Zelle credits. Recipients are email or SMS directory
tokens. Enrolled tokens post 24/7 when registered; request payments wait
for the customer to claim or expire after 14 days. Independent of outbound
Zelle origination (PR #123), Interac CAD (PR #111/#113), lockbox BAI2
(PR #121), Positive Pay (PR #119), Check21 (PR #115/#117), ACH linking
(PR #68), and domestic Fedwire (PR #73). Existing `/fundTransfer`,
`/withdrawAmount`, and `/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- US NANP email / SMS token directory with SHA-256 fingerprints
- Zelle payment-id uniqueness (ZEL + YYYYMMDD + seq)
- Early Warning participant ABA (checksum via utility.wire)
- USD-only 24/7 clock (Fed holidays reported, no cutoff queue)
- ZEL1 pipe parse/compose (XML/DOCTYPE rejected; 12 inbound fields)
- Dual-control at $10k, enrolled cap $15k / request cap $2.5k
- 14-day unenrolled claim window

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Full token values and fingerprints never appear in to_dict / snapshots.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
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
TOKEN_ACTIVE = 'active'
TOKEN_PAUSED = 'paused'
TOKEN_CANCELLED = 'cancelled'
TOKEN_STATUSES = frozenset({TOKEN_ACTIVE, TOKEN_PAUSED, TOKEN_CANCELLED})
OPEN_TOKEN = frozenset({TOKEN_ACTIVE, TOKEN_PAUSED})
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
    'enrolled': RAIL_ENROLLED, 'enroll': RAIL_ENROLLED, 'auto': RAIL_ENROLLED,
    'autodeposit': RAIL_ENROLLED, 'directory': RAIL_ENROLLED, 'registered': RAIL_ENROLLED,
    'request': RAIL_REQUEST, 'payrequest': RAIL_REQUEST, 'claim': RAIL_REQUEST,
    'unenrolled': RAIL_REQUEST, 'unregistered': RAIL_REQUEST,
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
    IN_UNMATCHED, IN_HELD, IN_PENDING, IN_CLAIM, IN_POSTED,
    IN_REJECTED, IN_RETURNED, IN_EXPIRED, IN_FAILED,
})
OPEN_IN = frozenset({IN_UNMATCHED, IN_HELD, IN_PENDING, IN_CLAIM})
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/zrecv.sqlite'
DEFAULT_RECEIVER = '021000021'
DEFAULT_NETWORK = 'ZELLE'
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)
EMAIL_RE = re.compile(r'^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,24}$')
PAY_RE = re.compile(r'^ZEL\d{12}$')


class ZrecvError(ValueError):
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


def normalize_participant(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise ZrecvError('invalid_routing', 'Zelle participant ABA required.')
        return ''
    if len(digits) < 9:
        digits = digits.zfill(9)
    if len(digits) != 9 or digits == '000000000' or not aba_check_digit_ok(digits):
        raise ZrecvError('invalid_routing', 'Zelle participant ABA failed checksum.')
    return digits


def mask_participant(routing: str) -> str:
    digits = re.sub(r'\D', '', str(routing or ''))
    if len(digits) < 4:
        return '****'
    return digits[:4] + '****' + digits[-1:]


def normalize_email(value: Any) -> str:
    text = str(value or '').strip().lower()
    if not EMAIL_RE.match(text) or '..' in text or text.startswith('.') or text.endswith('.'):
        raise ZrecvError('invalid_alias', 'Token email is not valid.')
    local, domain = text.split('@', 1)
    if not (1 <= len(local) <= 64) or not (3 <= len(domain) <= 80):
        raise ZrecvError('invalid_alias', 'Token email is not valid.')
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
        raise ZrecvError('invalid_alias', 'Token phone must be a US NANP number.')
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
        raise ZrecvError('invalid_alias', 'Token must be email or sms.')
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
        raise ZrecvError('invalid_rail', 'Rail must be enrolled or request.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise ZrecvError('invalid_name', 'Sender legal name must be 2-80 characters.')
    return text


def normalize_nickname(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise ZrecvError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def compose_payment_id(cycle_date: str, sequence: int) -> str:
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise ZrecvError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 9999:
        raise ZrecvError('invalid_reference', 'Zelle sequence out of range.')
    return 'ZEL%s%04d' % (day, seq)


def normalize_payment_id(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        if required:
            raise ZrecvError('invalid_reference', 'Zelle payment id required.')
        return ''
    if not PAY_RE.match(text):
        raise ZrecvError('invalid_reference', 'Zelle payment id must be ZEL + 12 digits.')
    return text


def mask_payment_id(value: str) -> str:
    text = str(value or '')
    if len(text) < 8:
        return 'ZEL********'
    return text[:6] + '*****' + text[-4:]


def normalize_currency(value: Any) -> str:
    text = str(value or 'USD').strip().upper()
    if text != 'USD':
        raise ZrecvError('invalid_currency', 'Zelle receive must be USD.')
    return text


def normalize_network(value: Any, *, default: str = DEFAULT_NETWORK) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or default).upper())
    if text not in {'ZELLE', 'EWS', 'EARLYWARNING'}:
        raise ZrecvError('invalid_file', 'Unknown Zelle network.')
    return 'ZELLE'


class ZelleClock:
    """Fed holiday calendar. Receive posts 24/7; snapshot still reports holidays."""

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
        raise ZrecvError('invalid_file', 'XML/DOCTYPE Zelle files are rejected.')


def compose_zel1(
    *,
    payment_id: str,
    amount: Decimal,
    sender_name: str,
    sender_fi: str,
    kind: str,
    alias: str,
    rail: str,
    expiry: str = '',
    network: str = DEFAULT_NETWORK,
    receiver: str = DEFAULT_RECEIVER,
) -> str:
    fields = [
        'ZEL1',
        normalize_payment_id(payment_id),
        money_str(amount),
        'USD',
        normalize_legal_name(sender_name).replace('|', ' '),
        normalize_participant(sender_fi),
        kind,
        alias,
        rail,
        str(expiry or ''),
        normalize_network(network),
        normalize_participant(receiver),
    ]
    return '|'.join(fields)


def parse_zel1(record: str) -> Dict[str, Any]:
    text = str(record or '').strip()
    _reject_xml(text)
    if not text or text.startswith('#'):
        raise ZrecvError('invalid_file', 'Empty Zelle record.')
    parts = text.split('|')
    if len(parts) != 12 or parts[0] != 'ZEL1':
        raise ZrecvError('invalid_file', 'Inbound ZEL1 records must have 12 pipe fields.')
    kind = normalize_kind(parts[6])
    rail = normalize_rail(parts[8])
    return {
        'payment_id': normalize_payment_id(parts[1]),
        'amount': parse_money(parts[2]),
        'currency': normalize_currency(parts[3]),
        'sender_name': normalize_legal_name(parts[4]),
        'sender_fi': normalize_participant(parts[5]),
        'alias_type': kind,
        'alias': normalize_alias_value(kind, parts[7]),
        'rail': rail,
        'expiry': str(parts[9] or '').strip(),
        'network': normalize_network(parts[10]),
        'receiver_routing': normalize_participant(parts[11]),
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
        raise ZrecvError('invalid_file', 'Zelle file had no ZEL1 records.')
    return records


def message_from_zel1(record: str) -> Dict[str, Any]:
    return parse_zel1(record)


def message_from_values(values: Dict[str, Any]) -> Dict[str, Any]:
    kind = normalize_kind(values.get('alias_type') or values.get('kind') or KIND_EMAIL)
    rail = normalize_rail(values.get('rail') or RAIL_ENROLLED)
    return {
        'payment_id': normalize_payment_id(values.get('payment_id') or values.get('reference') or values.get('scheme_id')),
        'amount': parse_money(values.get('amount') or values.get('amount_usd')),
        'currency': normalize_currency(values.get('currency') or 'USD'),
        'sender_name': normalize_legal_name(values.get('sender_name') or values.get('originator')),
        'sender_fi': normalize_participant(values.get('sender_fi') or values.get('originator_fi') or DEFAULT_RECEIVER),
        'alias_type': kind,
        'alias': normalize_alias_value(kind, values.get('alias') or values.get('alias_value') or values.get('token')),
        'rail': rail,
        'expiry': str(values.get('expiry') or values.get('expiry_date') or '').strip(),
        'network': normalize_network(values.get('network') or DEFAULT_NETWORK),
        'receiver_routing': normalize_participant(
            values.get('receiver_routing') or values.get('receiver') or DEFAULT_RECEIVER,
        ),
    }


def compose_return_zel1(row: 'ZrecvInbound', *, reason: str = 'cust') -> str:
    return compose_zel1(
        payment_id=row.payment_id,
        amount=parse_money(row.amount_usd),
        sender_name=row.sender_name,
        sender_fi=row.sender_fi,
        kind=row.alias_kind,
        alias=row.alias_value,
        rail=row.rail,
        expiry='RETURN %s' % reason,
        network=row.network,
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
class ZrecvPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_claim: bool = True
    allow_credit: bool = False
    max_tokens: int = 8
    max_inbounds: int = 120
    min_amount: Decimal = Decimal('0.01')
    enrolled_cap: Decimal = Decimal('15000.00')
    request_cap: Decimal = Decimal('2500.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    expiry_days: int = 14
    return_hours: int = 24
    tz_offset_hours: int = -4
    receiver_routing: str = DEFAULT_RECEIVER
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'ZrecvPolicy':
        extra = _env_list('ZRECV_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        routing = os.environ.get('ZRECV_RECEIVER') or DEFAULT_RECEIVER
        return cls(
            enabled=_env_bool('ZRECV_ENABLED', True),
            customer_manage=_env_bool('ZRECV_CUSTOMER_MANAGE', True),
            customer_claim=_env_bool('ZRECV_CUSTOMER_CLAIM', True),
            allow_credit=_env_bool('ZRECV_ALLOW_CREDIT', False),
            max_tokens=max(1, _env_int('ZRECV_MAX_TOKENS', 8)),
            max_inbounds=max(1, _env_int('ZRECV_MAX_INBOUNDS', 120)),
            min_amount=_env_money('ZRECV_MIN_AMOUNT', '0.01'),
            enrolled_cap=_env_money('ZRECV_ENROLLED_CAP', '15000.00'),
            request_cap=_env_money('ZRECV_REQUEST_CAP', '2500.00'),
            dual_control_threshold=_env_money('ZRECV_DUAL_CONTROL', '10000.00'),
            expiry_days=max(1, _env_int('ZRECV_EXPIRY_DAYS', 14)),
            return_hours=max(1, _env_int('ZRECV_RETURN_HOURS', 24)),
            tz_offset_hours=_env_int('ZRECV_TZ_OFFSET', -4),
            receiver_routing=normalize_participant(routing),
            watchlist=watch,
            extra_holidays=_env_list('ZRECV_HOLIDAYS'),
        )


@dataclass
class ZrecvToken:
    token_id: str
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
            'token_id': self.token_id,
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
class ZrecvInbound:
    inbound_id: str
    payment_id: str
    userid: str
    destination_account: str
    amount_usd: str
    sender_name: str
    sender_fi: str
    alias_kind: str
    alias_value: str
    rail: str
    receiver_routing: str
    network: str
    status: str
    reason: str
    actor: str
    releaser: str
    ofac_hit: bool
    ofac_match: str
    ofac_overridden: bool
    claimed: bool
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
            'payment_id': self.payment_id,
            'payment_id_masked': mask_payment_id(self.payment_id),
            'userid': self.userid,
            'account_last4': last4,
            'amount_usd': self.amount_usd,
            'sender_name': self.sender_name,
            'sender_fi_masked': mask_participant(self.sender_fi),
            'alias_kind': self.alias_kind,
            'alias_masked': mask_alias(self.alias_kind, self.alias_value),
            'rail': self.rail,
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


def _token_from_row(row: Any) -> ZrecvToken:
    data = dict(row)
    return ZrecvToken(
        token_id=data['token_id'], userid=data['userid'], nickname=data['nickname'],
        kind=data['kind'], value=data['value'], fingerprint=data['fingerprint'],
        destination_account=data['destination_account'], status=data['status'],
        actor=data['actor'], created_at=float(data['created_at']), updated_at=float(data['updated_at']),
    )


def _inbound_from_row(row: Any) -> ZrecvInbound:
    data = dict(row)
    return ZrecvInbound(
        inbound_id=data['inbound_id'], payment_id=data['payment_id'], userid=data['userid'],
        destination_account=data['destination_account'], amount_usd=data['amount_usd'],
        sender_name=data['sender_name'], sender_fi=data['sender_fi'], alias_kind=data['alias_kind'],
        alias_value=data['alias_value'], rail=data['rail'], receiver_routing=data['receiver_routing'],
        network=data['network'], status=data['status'], reason=data['reason'],
        actor=data['actor'], releaser=data['releaser'], ofac_hit=bool(int(data['ofac_hit'])),
        ofac_match=data['ofac_match'], ofac_overridden=bool(int(data['ofac_overridden'])),
        claimed=bool(int(data['claimed'])), created_at=float(data['created_at']),
        updated_at=float(data['updated_at']), posted_at=float(data['posted_at'] or 0),
        returned_at=float(data['returned_at'] or 0), expiry_at=float(data['expiry_at'] or 0),
        note=data.get('note') or '',
    )


class MemoryZrecvStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.tokens: Dict[str, ZrecvToken] = {}
        self.inbounds: Dict[str, ZrecvInbound] = {}
        self._by_pay: Dict[str, str] = {}
        self._by_fp: Dict[str, str] = {}

    def put_token(self, row: ZrecvToken) -> None:
        with self._lock:
            self.tokens[row.token_id] = row
            self._by_fp[row.fingerprint] = row.token_id

    def get_token(self, token_id: str) -> Optional[ZrecvToken]:
        return self.tokens.get(token_id)

    def get_token_by_fingerprint(self, fingerprint: str) -> Optional[ZrecvToken]:
        token_id = self._by_fp.get(fingerprint)
        return self.tokens.get(token_id) if token_id else None

    def update_token(self, row: ZrecvToken) -> None:
        self.put_token(row)

    def list_tokens(self, userid: Optional[str] = None) -> List[ZrecvToken]:
        rows = list(self.tokens.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        return sorted(rows, key=lambda row: row.created_at)

    def put_inbound(self, row: ZrecvInbound) -> None:
        with self._lock:
            self.inbounds[row.inbound_id] = row
            self._by_pay[row.payment_id] = row.inbound_id

    def get_inbound(self, inbound_id: str) -> Optional[ZrecvInbound]:
        return self.inbounds.get(inbound_id)

    def get_by_payment_id(self, payment_id: str) -> Optional[ZrecvInbound]:
        inbound_id = self._by_pay.get(payment_id)
        return self.inbounds.get(inbound_id) if inbound_id else None

    def update_inbound(self, row: ZrecvInbound) -> None:
        self.put_inbound(row)

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[ZrecvInbound]:
        rows = list(self.inbounds.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)


class SqliteZrecvStore:
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
                CREATE TABLE IF NOT EXISTS tokens (
                    token_id TEXT PRIMARY KEY,
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
                    payment_id TEXT NOT NULL UNIQUE,
                    userid TEXT NOT NULL DEFAULT '',
                    destination_account TEXT NOT NULL DEFAULT '',
                    amount_usd TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    sender_fi TEXT NOT NULL,
                    alias_kind TEXT NOT NULL,
                    alias_value TEXT NOT NULL,
                    rail TEXT NOT NULL,
                    receiver_routing TEXT NOT NULL,
                    network TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    ofac_overridden INTEGER NOT NULL DEFAULT 0,
                    claimed INTEGER NOT NULL DEFAULT 0,
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

    def put_token(self, row: ZrecvToken) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tokens (
                    token_id, userid, nickname, kind, value, fingerprint,
                    destination_account, status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.token_id, row.userid, row.nickname, row.kind, row.value, row.fingerprint,
                    row.destination_account, row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_token(self, token_id: str) -> Optional[ZrecvToken]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM tokens WHERE token_id = ?', (token_id,)).fetchone()
        return _token_from_row(row) if row else None

    def get_token_by_fingerprint(self, fingerprint: str) -> Optional[ZrecvToken]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM tokens WHERE fingerprint = ? ORDER BY created_at DESC LIMIT 1',
                (fingerprint,),
            ).fetchone()
        return _token_from_row(row) if row else None

    def update_token(self, row: ZrecvToken) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE tokens SET nickname=?, kind=?, value=?, fingerprint=?,
                    destination_account=?, status=?, actor=?, updated_at=?
                WHERE token_id=?
                """,
                (
                    row.nickname, row.kind, row.value, row.fingerprint, row.destination_account,
                    row.status, row.actor, row.updated_at, row.token_id,
                ),
            )
            conn.commit()

    def list_tokens(self, userid: Optional[str] = None) -> List[ZrecvToken]:
        sql = 'SELECT * FROM tokens'
        args: Tuple[Any, ...] = ()
        if userid is not None:
            sql += ' WHERE userid = ?'
            args = (userid,)
        sql += ' ORDER BY created_at'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_token_from_row(row) for row in rows]

    def put_inbound(self, row: ZrecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inbounds (
                    inbound_id, payment_id, userid, destination_account, amount_usd, sender_name,
                    sender_fi, alias_kind, alias_value, rail, receiver_routing, network, status,
                    reason, actor, releaser, ofac_hit, ofac_match, ofac_overridden, claimed,
                    created_at, updated_at, posted_at, returned_at, expiry_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.inbound_id, row.payment_id, row.userid, row.destination_account, row.amount_usd,
                    row.sender_name, row.sender_fi, row.alias_kind, row.alias_value, row.rail,
                    row.receiver_routing, row.network, row.status, row.reason, row.actor, row.releaser,
                    int(row.ofac_hit), row.ofac_match, int(row.ofac_overridden), int(row.claimed),
                    row.created_at, row.updated_at, row.posted_at, row.returned_at, row.expiry_at, row.note,
                ),
            )
            conn.commit()

    def get_inbound(self, inbound_id: str) -> Optional[ZrecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,)).fetchone()
        return _inbound_from_row(row) if row else None

    def get_by_payment_id(self, payment_id: str) -> Optional[ZrecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE payment_id = ?', (payment_id,)).fetchone()
        return _inbound_from_row(row) if row else None

    def update_inbound(self, row: ZrecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE inbounds SET userid=?, destination_account=?, amount_usd=?, sender_name=?,
                    sender_fi=?, alias_kind=?, alias_value=?, rail=?, receiver_routing=?, network=?,
                    status=?, reason=?, actor=?, releaser=?, ofac_hit=?, ofac_match=?,
                    ofac_overridden=?, claimed=?, updated_at=?, posted_at=?, returned_at=?,
                    expiry_at=?, note=?
                WHERE inbound_id=?
                """,
                (
                    row.userid, row.destination_account, row.amount_usd, row.sender_name, row.sender_fi,
                    row.alias_kind, row.alias_value, row.rail, row.receiver_routing, row.network,
                    row.status, row.reason, row.actor, row.releaser, int(row.ofac_hit), row.ofac_match,
                    int(row.ofac_overridden), int(row.claimed), row.updated_at, row.posted_at,
                    row.returned_at, row.expiry_at, row.note, row.inbound_id,
                ),
            )
            conn.commit()

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[ZrecvInbound]:
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


class ZrecvService:
    def __init__(
        self,
        policy: ZrecvPolicy,
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
        self.screen_fn = screen_fn or screen_name
        self.calendar = calendar or ZelleClock(
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )

    def _now(self) -> float:
        return float(self.clock())

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise ZrecvError('zrecv_disabled', 'Zelle receive is disabled.')

    def _require_staff(self, actor_type: str) -> None:
        if actor_type not in EMPLOYEE_ROLES:
            raise ZrecvError('zrecv_forbidden', 'Staff only.')

    def _require_customer_manage(self, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if not self.policy.customer_manage:
            raise ZrecvError('zrecv_forbidden', 'Customers cannot manage Zelle tokens.')

    def _accounts(self, userid: str) -> Any:
        if not self.accounts_fn:
            return {}
        return self.accounts_fn(userid) or {}

    def _owned(self, userid: str, account: str) -> None:
        owned = own_accounts_from_customer_payload(self._accounts(userid))
        if account not in owned:
            raise ZrecvError('invalid_account', 'Destination is not an account of this customer.')

    def _account_type(self, userid: str, account: str) -> str:
        return account_types_from_customer_payload(self._accounts(userid)).get(account, '')

    def _credit_allowed(self, userid: str, account: str) -> bool:
        kind = self._account_type(userid, account)
        if kind == 'credit' and not self.policy.allow_credit:
            return False
        return True

    def add_token(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: str,
        kind: Any,
        value: Any,
        destination_account: Any,
    ) -> ZrecvToken:
        self._require_enabled()
        self._require_customer_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise ZrecvError('zrecv_forbidden', 'Cannot register a token for another customer.')
        kind_n = normalize_kind(kind)
        alias_value = normalize_alias_value(kind_n, value)
        account = normalize_account(destination_account)
        self._owned(owner_userid, account)
        if not self._credit_allowed(owner_userid, account):
            raise ZrecvError('credit_not_allowed', 'Credit accounts cannot receive Zelle.')
        fingerprint = token_fingerprint(kind_n, alias_value)
        existing = self.store.get_token_by_fingerprint(fingerprint)
        if existing and existing.status in OPEN_TOKEN:
            raise ZrecvError('token_duplicate', 'That Zelle token is already registered.')
        open_count = sum(1 for row in self.store.list_tokens(owner_userid) if row.status in OPEN_TOKEN)
        if open_count >= self.policy.max_tokens:
            raise ZrecvError('token_limit', 'Zelle token limit reached.')
        now = self._now()
        row = ZrecvToken(
            token_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=normalize_nickname(nickname),
            kind=kind_n,
            value=alias_value,
            fingerprint=fingerprint,
            destination_account=account,
            status=TOKEN_ACTIVE,
            actor=actor,
            created_at=now,
            updated_at=now,
        )
        self.store.put_token(row)
        return row

    def set_token_status(
        self,
        *,
        token_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> ZrecvToken:
        self._require_enabled()
        row = self.store.get_token(token_id)
        if row is None:
            raise ZrecvError('token_not_found', 'Zelle token not found.')
        if actor_type not in EMPLOYEE_ROLES and actor != row.userid:
            raise ZrecvError('zrecv_forbidden', 'Cannot change another customer token.')
        self._require_customer_manage(actor_type)
        if status not in TOKEN_STATUSES:
            raise ZrecvError('invalid_status', 'Unknown token status.')
        if row.status == TOKEN_CANCELLED:
            raise ZrecvError('already_cancelled', 'Cancelled tokens cannot change.')
        if status == TOKEN_PAUSED and row.status != TOKEN_ACTIVE:
            raise ZrecvError('already_paused' if row.status == TOKEN_PAUSED else 'invalid_status', 'Token is not active.')
        if status == TOKEN_ACTIVE and row.status != TOKEN_PAUSED:
            raise ZrecvError('already_active', 'Token is already active.')
        if status == row.status:
            code = 'already_paused' if status == TOKEN_PAUSED else 'already_active'
            raise ZrecvError(code, 'Token already in that status.')
        updated = replace(row, status=status, actor=actor, updated_at=self._now())
        self.store.update_token(updated)
        if status == TOKEN_ACTIVE:
            self._rematch_unmatched(updated)
        return updated

    def _rematch_unmatched(self, token: ZrecvToken) -> None:
        fingerprint = token.fingerprint
        for inbound in self.store.list_inbounds(status=IN_UNMATCHED):
            if token_fingerprint(inbound.alias_kind, inbound.alias_value) != fingerprint:
                continue
            inbound.userid = token.userid
            inbound.destination_account = token.destination_account
            inbound.updated_at = self._now()
            self.store.update_inbound(inbound)
            self._evaluate(inbound, rescreen=True)

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        values: Dict[str, Any],
    ) -> Tuple[ZrecvInbound, bool]:
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
    ) -> Tuple[List[ZrecvInbound], int]:
        self._require_enabled()
        self._require_staff(actor_type)
        created = 0
        rows = []
        for record in split_zel1_file(payload):
            parsed = parse_zel1(record)
            row, is_new = self._ingest_parsed(actor=actor, parsed=parsed)
            rows.append(row)
            if is_new:
                created += 1
        return rows, created

    def _expiry_at(self, now: float, expiry: str) -> float:
        expiry_at = now + (self.policy.expiry_days * 86400)
        if not expiry:
            return expiry_at
        text = str(expiry)
        try:
            if '-' in text:
                day = date.fromisoformat(text[:10])
            else:
                day = datetime.strptime(text[:8], '%Y%m%d').date()
            local = self.calendar.local_dt(now)
            return datetime(day.year, day.month, day.day, 23, 59, tzinfo=local.tzinfo).timestamp()
        except (ValueError, TypeError) as exc:
            raise ZrecvError('invalid_date', 'Expiry date is invalid.') from exc

    def _ingest_parsed(self, *, actor: str, parsed: Dict[str, Any]) -> Tuple[ZrecvInbound, bool]:
        if parsed['receiver_routing'] != self.policy.receiver_routing:
            raise ZrecvError('wrong_receiver', 'Zelle credit is not for this receiver FI.')
        existing = self.store.get_by_payment_id(parsed['payment_id'])
        if existing is not None:
            return existing, False
        total = len(self.store.list_inbounds())
        if total >= self.policy.max_inbounds:
            raise ZrecvError('inbound_limit', 'Inbound Zelle limit reached.')
        amount = parsed['amount']
        if amount < self.policy.min_amount:
            raise ZrecvError('amount_out_of_range', 'Amount is below the Zelle minimum.')
        cap = self.policy.enrolled_cap if parsed['rail'] == RAIL_ENROLLED else self.policy.request_cap
        if amount > cap:
            code = 'enrolled_amount_exceeded' if parsed['rail'] == RAIL_ENROLLED else 'request_amount_exceeded'
            raise ZrecvError(code, 'Amount exceeds the Zelle rail cap.')
        now = self._now()
        row = ZrecvInbound(
            inbound_id=uuid.uuid4().hex,
            payment_id=parsed['payment_id'],
            userid='',
            destination_account='',
            amount_usd=money_str(amount),
            sender_name=parsed['sender_name'],
            sender_fi=parsed['sender_fi'],
            alias_kind=parsed['alias_type'],
            alias_value=parsed['alias'],
            rail=parsed['rail'],
            receiver_routing=parsed['receiver_routing'],
            network=parsed.get('network') or DEFAULT_NETWORK,
            status=IN_UNMATCHED,
            reason='',
            actor=actor,
            releaser='',
            ofac_hit=False,
            ofac_match='',
            ofac_overridden=False,
            claimed=False,
            created_at=now,
            updated_at=now,
            expiry_at=self._expiry_at(now, parsed.get('expiry') or ''),
        )
        token = self.store.get_token_by_fingerprint(token_fingerprint(row.alias_kind, row.alias_value))
        if token is not None and token.status == TOKEN_ACTIVE:
            row.userid = token.userid
            row.destination_account = token.destination_account
        self.store.put_inbound(row)
        self._evaluate(row, rescreen=True)
        return row, True

    def _evaluate(self, row: ZrecvInbound, *, rescreen: bool) -> ZrecvInbound:
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
        except ZrecvError:
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
        if row.rail == RAIL_REQUEST and not row.claimed:
            row.status = IN_CLAIM
            row.reason = ''
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        return self._post(row)

    def _post(self, row: ZrecvInbound) -> ZrecvInbound:
        if self.credit_fn is None:
            raise ZrecvError('failed', 'Credit function is not configured.')
        remark = 'zelle from %s' % row.sender_name[:40]
        result = self.credit_fn(row.destination_account, row.amount_usd, remark)
        classified = _classify_money_result(result)
        now = self._now()
        if classified == 'nsf':
            row.status = IN_FAILED
            row.reason = 'nsf'
            row.updated_at = now
            self.store.update_inbound(row)
            raise ZrecvError('nsf', 'Credit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            row.status = IN_FAILED
            row.reason = 'failed'
            row.updated_at = now
            self.store.update_inbound(row)
            raise ZrecvError('failed', 'Credit failed.', inbound=row)
        row.status = IN_POSTED
        row.reason = ''
        row.posted_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def preview(self, *, inbound_id: str = '', payment_id: str = '', actor: str = '', actor_type: str = '') -> Dict[str, Any]:
        row = self._get(inbound_id=inbound_id, payment_id=payment_id)
        self._assert_visible(row, actor, actor_type)
        return {
            'inbound': row.to_dict(),
            'clock': self.calendar.snapshot(self._now()),
            'receiver': mask_participant(self.policy.receiver_routing),
        }

    def _get(self, *, inbound_id: str = '', payment_id: str = '') -> ZrecvInbound:
        row = None
        if inbound_id:
            row = self.store.get_inbound(str(inbound_id).strip())
        if row is None and payment_id:
            row = self.store.get_by_payment_id(normalize_payment_id(payment_id))
        if row is None:
            raise ZrecvError('inbound_not_found', 'Inbound Zelle not found.')
        return row

    def _assert_visible(self, row: ZrecvInbound, actor: str, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if row.userid and row.userid != actor:
            raise ZrecvError('zrecv_forbidden', 'Cannot view another customer Zelle.')

    def assign(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        customer_id: str,
        account: Any,
        nickname: str = '',
    ) -> ZrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_UNMATCHED:
            raise ZrecvError('not_assignable', 'Only unmatched Zelle credits can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise ZrecvError('missing_customer_id', 'Customer id is required to assign.')
        dest = normalize_account(account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise ZrecvError('credit_not_allowed', 'Credit accounts cannot receive Zelle.')
        fingerprint = token_fingerprint(row.alias_kind, row.alias_value)
        existing = self.store.get_token_by_fingerprint(fingerprint)
        if existing is None or existing.status == TOKEN_CANCELLED:
            self.add_token(
                owner_userid=owner, actor=actor, actor_type=actor_type,
                nickname=nickname or ('Zelle %s' % row.alias_kind),
                kind=row.alias_kind, value=row.alias_value, destination_account=dest,
            )
        elif existing.userid != owner:
            raise ZrecvError('token_duplicate', 'Token belongs to another customer.')
        row.userid = owner
        row.destination_account = dest
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=True)

    def override_ofac(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> ZrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_HELD:
            raise ZrecvError('not_overridable', 'Only OFAC-held Zelle credits can be overridden.')
        row.ofac_overridden = True
        row.ofac_hit = False
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def release(self, *, inbound_id: str, actor: str, actor_type: str) -> ZrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_PENDING:
            raise ZrecvError('not_releasable', 'Only dual-control Zelle credits can be released.')
        if actor == row.actor:
            raise ZrecvError('same_approver', 'A different employee must release this credit.')
        row.releaser = actor
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def reject(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> ZrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status not in OPEN_IN:
            raise ZrecvError('not_rejectable', 'This Zelle credit cannot be rejected.')
        row.status = IN_REJECTED
        row.reason = 'rejected'
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return row

    def claim(self, *, inbound_id: str, actor: str, actor_type: str) -> ZrecvInbound:
        self._require_enabled()
        row = self._get(inbound_id=inbound_id)
        self._assert_visible(row, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_claim:
                raise ZrecvError('zrecv_forbidden', 'Customers cannot claim Zelle credits.')
            if actor != row.userid:
                raise ZrecvError('zrecv_forbidden', 'Cannot claim another customer Zelle.')
        if row.status != IN_CLAIM:
            raise ZrecvError('not_claimable', 'This Zelle credit is not waiting for a claim.')
        row.claimed = True
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def decline(self, *, inbound_id: str, actor: str, actor_type: str) -> ZrecvInbound:
        self._require_enabled()
        row = self._get(inbound_id=inbound_id)
        self._assert_visible(row, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != row.userid:
            raise ZrecvError('zrecv_forbidden', 'Cannot decline another customer Zelle.')
        if row.status != IN_CLAIM:
            raise ZrecvError('not_declinable', 'Only unclaimed request transfers can be declined.')
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
    ) -> ZrecvInbound:
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
    ) -> ZrecvInbound:
        self._require_enabled()
        row = self._get(inbound_id=inbound_id)
        self._assert_visible(row, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != row.userid:
            raise ZrecvError('zrecv_forbidden', 'Cannot return another customer Zelle.')
        return self._return(row, actor=actor, reason=reason, honor_window=True)

    def _return(self, row: ZrecvInbound, *, actor: str, reason: str, honor_window: bool) -> ZrecvInbound:
        now = self._now()
        _ = actor
        if row.status == IN_RETURNED or row.returned_at:
            raise ZrecvError('already_returned', 'Zelle credit already returned.')
        if row.status in OPEN_IN:
            row.status = IN_RETURNED
            row.reason = reason or 'cust'
            row.returned_at = now
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if row.status != IN_POSTED:
            raise ZrecvError('not_returnable', 'This Zelle credit cannot be returned.')
        if honor_window and row.posted_at and (now - row.posted_at) > (self.policy.return_hours * 3600):
            raise ZrecvError('return_window_closed', 'Customer Zelle return window has closed.')
        if self.debit_fn is None:
            raise ZrecvError('return_failed', 'Debit function is not configured.')
        remark = 'zelle return %s' % row.payment_id[-12:]
        result = self.debit_fn(row.destination_account, row.amount_usd, remark)
        classified = _classify_money_result(result)
        if classified == 'nsf':
            raise ZrecvError('nsf', 'Return debit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            raise ZrecvError('return_failed', 'Return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.reason = reason or 'cust'
        row.returned_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[ZrecvInbound]:
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
        _ = actor_type
        now = self._now()
        if userid:
            self.run_due(userid)
        tokens = self.store.list_tokens(userid) if userid else []
        inbounds = self.store.list_inbounds(userid) if userid else []
        posted = [row for row in inbounds if row.status == IN_POSTED]
        ytd = sum((parse_money(row.amount_usd, allow_zero=True) for row in posted), Decimal('0.00'))
        return {
            'enabled': self.policy.enabled,
            'clock': self.calendar.snapshot(now),
            'receiver': mask_participant(self.policy.receiver_routing),
            'tokens': [row.to_dict() for row in tokens[:40]],
            'inbounds': [row.to_dict() for row in inbounds[:40]],
            'ytd_received': money_str(ytd),
            'open_count': sum(1 for row in inbounds if row.status in OPEN_IN),
            'active_tokens': sum(1 for row in tokens if row.status == TOKEN_ACTIVE),
            'enrolled_cap': money_str(self.policy.enrolled_cap),
            'request_cap': money_str(self.policy.request_cap),
            'dual_control': money_str(self.policy.dual_control_threshold),
        }

    def unmatched_snapshot(self) -> Dict[str, Any]:
        rows = self.store.list_inbounds(status=IN_UNMATCHED)
        return {
            'enabled': self.policy.enabled,
            'unmatched': [row.to_dict() for row in rows[:40]],
            'count': len(rows),
        }


_SERVICE: Optional[ZrecvService] = None


def set_service(service: Optional[ZrecvService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[ZrecvService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('ZRECV_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryZrecvStore()
    path = os.environ.get('ZRECV_DB', DEFAULT_STORE_PATH)
    return SqliteZrecvStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[ZelleClock] = None,
) -> ZrecvService:
    if store is None:
        store = default_store()
    return ZrecvService(
        ZrecvPolicy.from_env(),
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
        'token_duplicate': 409,
        'token_limit': 409,
        'inbound_limit': 409,
        'already_cancelled': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_returned': 409,
        'nsf': 409,
        'failed': 409,
        'return_failed': 409,
        'zrecv_forbidden': 403,
        'zrecv_disabled': 403,
        'credit_not_allowed': 403,
        'same_approver': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_claimable': 403,
        'not_declinable': 403,
        'not_returnable': 403,
        'return_window_closed': 403,
        'token_not_found': 404,
        'inbound_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_alias': 400,
        'invalid_routing': 400,
        'invalid_reference': 400,
        'invalid_rail': 400,
        'invalid_currency': 400,
        'invalid_file': 400,
        'invalid_date': 400,
        'invalid_status': 400,
        'wrong_receiver': 400,
        'enrolled_amount_exceeded': 400,
        'request_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_token': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: ZrecvError) -> Dict[str, Any]:
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
    except ZrecvError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snapshot_for(service: ZrecvService, userid: str, actor: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(userid, actor=actor, actor_type=actor_type)


def handle_list(service: ZrecvService):
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
    return jsonify({'ZelleIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def handle_unmatched(service: ZrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'zrecv_forbidden'}), 403
    snap = service.unmatched_snapshot()
    return jsonify({'UnmatchedZelleIns': snap, 'ZelleIn': snap}), 200


def handle_add_token(service: ZrecvService):
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
                raise ZrecvError('missing_customer_id', 'Customer id is required.')
        token = service.add_token(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname') or '',
            kind=values.get('kind') or values.get('alias_type') or KIND_EMAIL,
            value=values.get('alias') or values.get('value') or values.get('email') or values.get('phone'),
            destination_account=values.get('destination_account') or values.get('account') or values.get('default_account'),
        )
        return jsonify({
            'message': 'Zelle token registered',
            'token': token.to_dict(),
            'ZelleIn': _snapshot_for(service, token.userid, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _token_status_route(service: ZrecvService, status: str, message: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        token_id = str(values.get('token_id') or '').strip()
        if not token_id:
            raise ZrecvError('missing_token', 'token_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.set_token_status(token_id=token_id, actor=userid, actor_type=actor_type, status=status)
        return jsonify({
            'message': message,
            'token': row.to_dict(),
            'ZelleIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: ZrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        preview = service.preview(
            inbound_id=str(values.get('inbound_id') or ''),
            payment_id=str(values.get('payment_id') or values.get('reference') or ''),
            actor=userid,
            actor_type=actor_type,
        )
        owner = preview['inbound'].get('userid') or userid
        return jsonify({'preview': preview, 'ZelleIn': _snapshot_for(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest(service: ZrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'Zelle ingested' if created else 'Zelle already ingested',
            'inbound': row.to_dict(),
            'ZelleIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_ingest_file(service: ZrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        payload = values.get('file') or values.get('body') or values.get('payload')
        if not payload:
            raise ZrecvError('missing_file', 'Zelle file body is required.')
        actor_type = session.get('usertype') or 'customer'
        rows, created = service.ingest_file(actor=userid, actor_type=actor_type, payload=str(payload))
        owner = rows[-1].userid if rows and rows[-1].userid else userid
        return jsonify({
            'message': 'Zelle file ingested',
            'created': created,
            'inbounds': [row.to_dict() for row in rows],
            'ZelleIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_claim(service: ZrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise ZrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.claim(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
        return jsonify({
            'message': 'Zelle claimed',
            'inbound': row.to_dict(),
            'ZelleIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_decline(service: ZrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise ZrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.decline(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
        return jsonify({
            'message': 'Zelle declined',
            'inbound': row.to_dict(),
            'ZelleIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_assign(service: ZrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise ZrecvError('missing_inbound', 'inbound_id is required.')
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
            'message': 'Zelle assigned',
            'inbound': row.to_dict(),
            'ZelleIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_inbound_route(service: ZrecvService, action: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise ZrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        if action == 'override':
            row = service.override_ofac(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'release':
            row = service.release(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            message = 'Zelle released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Zelle rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=str(values.get('reason') or 'cust'),
            )
            message = 'Zelle returned'
        else:
            raise ZrecvError('invalid_status', 'Unknown Zelle action.')
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'ZelleIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: ZrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise ZrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.request_return(
            inbound_id=inbound_id, actor=userid, actor_type=actor_type,
            reason=str(values.get('reason') or 'cust'),
        )
        return jsonify({
            'message': 'Zelle return requested',
            'inbound': row.to_dict(),
            'ZelleIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: ZrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'ZelleIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def attach_zrecv_routes(app, service: ZrecvService) -> None:
    @app.route('/listZelleIns', methods=['POST', 'GET'])
    def list_zelle_ins_route():
        return handle_list(service)

    @app.route('/listZelleTokens', methods=['POST', 'GET'])
    def list_zelle_tokens_route():
        return handle_list(service)

    @app.route('/listUnmatchedZelleIns', methods=['POST', 'GET'])
    def list_unmatched_zelle_ins_route():
        return handle_unmatched(service)

    @app.route('/addZelleToken', methods=['POST', 'GET'])
    def add_zelle_token_route():
        return handle_add_token(service)

    @app.route('/pauseZelleToken', methods=['POST', 'GET'])
    def pause_zelle_token_route():
        return _token_status_route(service, TOKEN_PAUSED, 'Zelle token paused')

    @app.route('/resumeZelleToken', methods=['POST', 'GET'])
    def resume_zelle_token_route():
        return _token_status_route(service, TOKEN_ACTIVE, 'Zelle token resumed')

    @app.route('/cancelZelleToken', methods=['POST', 'GET'])
    def cancel_zelle_token_route():
        return _token_status_route(service, TOKEN_CANCELLED, 'Zelle token cancelled')

    @app.route('/previewZelleIn', methods=['POST', 'GET'])
    def preview_zelle_in_route():
        return handle_preview(service)

    @app.route('/ingestZelleIn', methods=['POST', 'GET'])
    def ingest_zelle_in_route():
        return handle_ingest(service)

    @app.route('/ingestZelleInFile', methods=['POST', 'GET'])
    def ingest_zelle_in_file_route():
        return handle_ingest_file(service)

    @app.route('/claimZelleIn', methods=['POST', 'GET'])
    def claim_zelle_in_route():
        return handle_claim(service)

    @app.route('/declineZelleIn', methods=['POST', 'GET'])
    def decline_zelle_in_route():
        return handle_decline(service)

    @app.route('/assignZelleIn', methods=['POST', 'GET'])
    def assign_zelle_in_route():
        return handle_assign(service)

    @app.route('/overrideZelleInOfac', methods=['POST', 'GET'])
    def override_zelle_in_ofac_route():
        return _staff_inbound_route(service, 'override')

    @app.route('/releaseZelleIn', methods=['POST', 'GET'])
    def release_zelle_in_route():
        return _staff_inbound_route(service, 'release')

    @app.route('/rejectZelleIn', methods=['POST', 'GET'])
    def reject_zelle_in_route():
        return _staff_inbound_route(service, 'reject')

    @app.route('/returnZelleIn', methods=['POST', 'GET'])
    def return_zelle_in_route():
        return _staff_inbound_route(service, 'return')

    @app.route('/requestZelleInReturn', methods=['POST', 'GET'])
    def request_zelle_in_return_route():
        return handle_request_return(service)

    @app.route('/runDueZelleIns', methods=['POST', 'GET'])
    def run_due_zelle_ins_route():
        return handle_run_due(service)
