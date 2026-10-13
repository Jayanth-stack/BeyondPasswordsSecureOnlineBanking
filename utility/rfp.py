"""Inbound Request-for-Payment (RfP) fulfillment.

Staff ingest incoming ISO 20022 pain.013 payment requests on FedNow, TCH RTP,
or Zelle. Customers pay (debit) or decline. Independent of outbound RTP
origination + customer-sent RfP (PR #83), inbound FedNow/RTP credits
(PR #93), Zelle send/receive (PR #123/#125), ACH linking (PR #68), and
domestic Fedwire (PR #73). Existing `/fundTransfer`, `/withdrawAmount`,
and `/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- ISO 20022 pain.013 / pain.014 / pacs.008 pipe compose/parse (XML/DOCTYPE rejected)
- EndToEndId uniqueness (RFP + YYYYMMDD + seq) and fulfillment UETR
- Instant 24/7 clock (Fed holidays reported, no cutoff queue)
- Dual rail FedNow / RTP plus Zelle email/SMS alias matching
- Dual-control at $10k on the customer pay, not on ingest
- 7-day request expiry; instant rails are irrevocable after pay

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Creditor accounts, aliases, and fingerprints never appear in to_dict / snapshots.
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
    last4,
    money_str,
    normalize_account,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
    us_fed_holidays,
)

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
ENROLL_ACTIVE = 'active'
ENROLL_PAUSED = 'paused'
ENROLL_CANCELLED = 'cancelled'
ENROLL_STATUSES = frozenset({ENROLL_ACTIVE, ENROLL_PAUSED, ENROLL_CANCELLED})
OPEN_ENROLL = frozenset({ENROLL_ACTIVE, ENROLL_PAUSED})
KIND_NONE = 'none'
KIND_EMAIL = 'email'
KIND_SMS = 'sms'
ALIAS_KINDS = frozenset({KIND_NONE, KIND_EMAIL, KIND_SMS})
KIND_ALIASES = {
    'email': KIND_EMAIL, 'mail': KIND_EMAIL, 'e-mail': KIND_EMAIL, 'token': KIND_EMAIL,
    'sms': KIND_SMS, 'phone': KIND_SMS, 'mobile': KIND_SMS, 'text': KIND_SMS, 'tel': KIND_SMS,
    'none': KIND_NONE, 'account': KIND_NONE, 'dda': KIND_NONE, '': KIND_NONE, 'onus': KIND_NONE,
}
RAIL_FEDNOW = 'fednow'
RAIL_RTP = 'rtp'
RAIL_ZELLE = 'zelle'
RAILS = frozenset({RAIL_FEDNOW, RAIL_RTP, RAIL_ZELLE})
RAIL_ALIASES = {
    'fednow': RAIL_FEDNOW, 'fed_now': RAIL_FEDNOW, 'fn': RAIL_FEDNOW, 'now': RAIL_FEDNOW,
    'rtp': RAIL_RTP, 'tch': RAIL_RTP, 'clearinghouse': RAIL_RTP, 'theclearinghouse': RAIL_RTP,
    'zelle': RAIL_ZELLE, 'ews': RAIL_ZELLE, 'earlywarning': RAIL_ZELLE,
}
RFP_UNMATCHED = 'unmatched'
RFP_HELD = 'held'
RFP_REQUESTED = 'requested'
RFP_PENDING = 'pending_release'
RFP_PAID = 'paid'
RFP_DECLINED = 'declined'
RFP_REJECTED = 'rejected'
RFP_RETURNED = 'returned'
RFP_EXPIRED = 'expired'
RFP_FAILED = 'failed'
RFP_STATUSES = frozenset({
    RFP_UNMATCHED, RFP_HELD, RFP_REQUESTED, RFP_PENDING, RFP_PAID,
    RFP_DECLINED, RFP_REJECTED, RFP_RETURNED, RFP_EXPIRED, RFP_FAILED,
})
OPEN_RFP = frozenset({RFP_UNMATCHED, RFP_HELD, RFP_REQUESTED, RFP_PENDING})
PAYABLE = frozenset({RFP_REQUESTED})
DECLINE_REASONS = frozenset({'refuse', 'unable', 'duplicate', 'other'})
DECLINE_ALIASES = {
    'no': 'refuse', 'deny': 'refuse', 'declined': 'refuse', 'refuse': 'refuse',
    'nsf': 'unable', 'unable': 'unable', 'cannot': 'unable',
    'dup': 'duplicate', 'duplicate': 'duplicate',
    'other': 'other', 'cust': 'other',
}
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/rfp.sqlite'
DEFAULT_RECEIVER = '021000021'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)
EMAIL_RE = re.compile(r'^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,24}$')
E2E_RE = re.compile(r'^RFP\d{14}$')
UETR_RE = re.compile(r'^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$')


class RfpError(ValueError):
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
    if any(token in text for token in DEBIT_NSF):
        return 'nsf'
    if text in DEBIT_OK or 'success' in text or 'credited' in text or 'debited' in text:
        return 'ok'
    return 'failed'


def normalize_participant(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise RfpError('invalid_routing', 'Participant ABA required.')
        return ''
    if len(digits) < 9:
        digits = digits.zfill(9)
    if len(digits) != 9 or digits == '000000000' or not aba_check_digit_ok(digits):
        raise RfpError('invalid_routing', 'Participant ABA failed checksum.')
    return digits


def mask_participant(routing: str) -> str:
    digits = re.sub(r'\D', '', str(routing or ''))
    if len(digits) < 4:
        return '****'
    return digits[:4] + '****' + digits[-1:]


def normalize_email(value: Any) -> str:
    text = str(value or '').strip().lower()
    if not EMAIL_RE.match(text) or '..' in text or text.startswith('.') or text.endswith('.'):
        raise RfpError('invalid_alias', 'Alias email is not valid.')
    local, domain = text.split('@', 1)
    if not (1 <= len(local) <= 64) or not (3 <= len(domain) <= 80):
        raise RfpError('invalid_alias', 'Alias email is not valid.')
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
        raise RfpError('invalid_alias', 'Alias phone must be a US NANP number.')
    return digits


def mask_phone(value: str) -> str:
    digits = re.sub(r'\D', '', str(value or ''))
    if len(digits) < 4:
        return '***-***-****'
    return '***-***-%s' % digits[-4:]


def normalize_kind(value: Any, *, default: str = KIND_NONE) -> str:
    text = str(value if value is not None else default).strip().lower().replace('-', '_').replace(' ', '_')
    text = KIND_ALIASES.get(text, text)
    if text not in ALIAS_KINDS:
        raise RfpError('invalid_alias', 'Alias must be email, sms, or none.')
    return text


def normalize_alias_value(kind: str, value: Any, *, required: bool = False) -> str:
    if kind == KIND_NONE:
        return ''
    if not str(value or '').strip():
        if required:
            raise RfpError('invalid_alias', 'Alias value is required.')
        return ''
    if kind == KIND_EMAIL:
        return normalize_email(value)
    return normalize_phone(value)


def mask_alias(kind: str, value: str) -> str:
    if kind == KIND_EMAIL:
        return mask_email(value)
    if kind == KIND_SMS:
        return mask_phone(value)
    return ''


def alias_fingerprint(kind: str, value: str) -> str:
    if kind == KIND_NONE or not value:
        return ''
    material = ('%s\0%s' % (kind, value)).encode('utf-8')
    return hashlib.sha256(material).hexdigest()


def account_fingerprint(account: str) -> str:
    return hashlib.sha256(('dda\0%s' % account).encode('utf-8')).hexdigest()


def normalize_rail(value: Any, *, default: str = RAIL_FEDNOW) -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text not in RAILS:
        raise RfpError('invalid_rail', 'Rail must be fednow, rtp, or zelle.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise RfpError('invalid_name', 'Creditor legal name must be 2-80 characters.')
    return text


def normalize_nickname(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise RfpError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_currency(value: Any) -> str:
    text = str(value or 'USD').strip().upper()
    if text != 'USD':
        raise RfpError('invalid_currency', 'RfP must be USD.')
    return text


def normalize_decline_reason(value: Any, *, default: str = 'refuse') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = DECLINE_ALIASES.get(text, text)
    if text not in DECLINE_REASONS:
        raise RfpError('invalid_reason', 'Unknown decline reason.')
    return text


def compose_end_to_end_id(cycle_date: str, sequence: int) -> str:
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise RfpError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise RfpError('invalid_reference', 'RfP sequence out of range.')
    return 'RFP%s%06d' % (day, seq)


def normalize_end_to_end_id(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        if required:
            raise RfpError('invalid_reference', 'EndToEndId required.')
        return ''
    if not E2E_RE.match(text):
        raise RfpError('invalid_reference', 'EndToEndId must be RFP + 14 digits.')
    return text


def mask_end_to_end_id(value: str) -> str:
    text = str(value or '')
    if len(text) < 8:
        return 'RFP********'
    return text[:6] + '*****' + text[-4:]


def compose_uetr(value: Any = None) -> str:
    if value:
        return normalize_uetr(value)
    return str(uuid.uuid4()).upper()


def normalize_uetr(value: Any, *, required: bool = True) -> str:
    text = str(value or '').strip().upper()
    if not text:
        if required:
            raise RfpError('invalid_reference', 'UETR required.')
        return ''
    if not UETR_RE.match(text):
        raise RfpError('invalid_reference', 'UETR must be an RFC 4122 UUID.')
    return text


def mask_uetr(value: str) -> str:
    text = str(value or '')
    if len(text) < 12:
        return '********'
    return text[:8] + '***' + text[-4:]


def mask_account(value: str) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        return ''
    return last4(digits)


class InstantClock:
    """Fed holiday calendar. RfP fulfillment is 24/7; snapshot still reports holidays."""

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
    if '<?xml' in low or '<!doctype' in low or '<!entity' in low or '<pain' in low or '<pacs' in low:
        raise RfpError('invalid_file', 'XML/DOCTYPE RfP files are rejected.')


def compose_pain013(
    *,
    end_to_end_id: str,
    amount: Decimal,
    rail: str,
    creditor_name: str,
    creditor_aba: str,
    creditor_account: str,
    debtor_account: str = '',
    alias_kind: str = KIND_NONE,
    alias: str = '',
    expiry: str = '',
    receiver: str = DEFAULT_RECEIVER,
) -> str:
    kind = normalize_kind(alias_kind)
    fields = [
        'PAIN013',
        normalize_end_to_end_id(end_to_end_id),
        money_str(amount),
        'USD',
        normalize_rail(rail),
        normalize_legal_name(creditor_name).replace('|', ' '),
        normalize_participant(creditor_aba),
        ''.join(ch for ch in str(creditor_account or '') if ch.isdigit()),
        ''.join(ch for ch in str(debtor_account or '') if ch.isdigit()),
        kind,
        alias if kind != KIND_NONE else '',
        str(expiry or ''),
        normalize_participant(receiver),
    ]
    return '|'.join(fields)


def parse_pain013(record: str) -> Dict[str, Any]:
    text = str(record or '').strip()
    _reject_xml(text)
    if not text or text.startswith('#'):
        raise RfpError('invalid_file', 'Empty pain.013 record.')
    parts = text.split('|')
    if len(parts) != 13 or parts[0] != 'PAIN013':
        raise RfpError('invalid_file', 'Inbound PAIN013 records must have 13 pipe fields.')
    kind = normalize_kind(parts[9])
    rail = normalize_rail(parts[4])
    debtor = ''.join(ch for ch in str(parts[8] or '') if ch.isdigit())
    alias = normalize_alias_value(kind, parts[10], required=(rail == RAIL_ZELLE))
    if rail == RAIL_ZELLE and not alias:
        raise RfpError('invalid_alias', 'Zelle RfP requires an email or SMS alias.')
    if rail != RAIL_ZELLE and not debtor:
        raise RfpError('invalid_account', 'FedNow/RTP RfP requires a debtor account.')
    creditor_account = ''.join(ch for ch in str(parts[7] or '') if ch.isdigit())
    if not (4 <= len(creditor_account) <= 17):
        raise RfpError('invalid_account', 'Creditor account must be 4-17 digits.')
    return {
        'end_to_end_id': normalize_end_to_end_id(parts[1]),
        'amount': parse_money(parts[2]),
        'currency': normalize_currency(parts[3]),
        'rail': rail,
        'creditor_name': normalize_legal_name(parts[5]),
        'creditor_aba': normalize_participant(parts[6]),
        'creditor_account': creditor_account,
        'debtor_account': debtor,
        'alias_kind': kind,
        'alias': alias,
        'expiry': str(parts[11] or '').strip(),
        'receiver_routing': normalize_participant(parts[12]),
    }


def split_pain013_file(payload: str) -> List[str]:
    text = str(payload or '')
    _reject_xml(text)
    records = []
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        records.append(stripped)
    if not records:
        raise RfpError('invalid_file', 'RfP file had no PAIN013 records.')
    return records


def message_from_pain013(record: str) -> Dict[str, Any]:
    return parse_pain013(record)


def message_from_values(values: Dict[str, Any]) -> Dict[str, Any]:
    kind = normalize_kind(values.get('alias_type') or values.get('kind') or KIND_NONE)
    rail = normalize_rail(values.get('rail') or RAIL_FEDNOW)
    debtor = ''.join(ch for ch in str(values.get('debtor_account') or values.get('account') or '') if ch.isdigit())
    alias = normalize_alias_value(
        kind, values.get('alias') or values.get('alias_value') or values.get('token'),
        required=(rail == RAIL_ZELLE),
    )
    if rail == RAIL_ZELLE and not alias:
        raise RfpError('invalid_alias', 'Zelle RfP requires an email or SMS alias.')
    if rail != RAIL_ZELLE and not debtor:
        raise RfpError('invalid_account', 'FedNow/RTP RfP requires a debtor account.')
    creditor_account = ''.join(
        ch for ch in str(values.get('creditor_account') or values.get('external_account') or '') if ch.isdigit()
    )
    if not (4 <= len(creditor_account) <= 17):
        raise RfpError('invalid_account', 'Creditor account must be 4-17 digits.')
    return {
        'end_to_end_id': normalize_end_to_end_id(
            values.get('end_to_end_id') or values.get('e2e') or values.get('reference') or values.get('scheme_id'),
        ),
        'amount': parse_money(values.get('amount') or values.get('amount_usd')),
        'currency': normalize_currency(values.get('currency') or 'USD'),
        'rail': rail,
        'creditor_name': normalize_legal_name(values.get('creditor_name') or values.get('originator') or values.get('name')),
        'creditor_aba': normalize_participant(values.get('creditor_aba') or values.get('aba') or values.get('routing')),
        'creditor_account': creditor_account,
        'debtor_account': debtor,
        'alias_kind': kind,
        'alias': alias,
        'expiry': str(values.get('expiry') or values.get('expiry_date') or '').strip(),
        'receiver_routing': normalize_participant(
            values.get('receiver_routing') or values.get('receiver') or DEFAULT_RECEIVER,
        ),
    }


def compose_pain014(row: 'RfpRequest', *, reason: str = 'refuse') -> str:
    return '|'.join([
        'PAIN014',
        row.end_to_end_id,
        normalize_decline_reason(reason),
        row.rail,
        mask_end_to_end_id(row.end_to_end_id),
    ])


def compose_pacs008(row: 'RfpRequest') -> str:
    return '|'.join([
        'PACS008',
        row.end_to_end_id,
        row.uetr or '',
        row.amount_usd,
        'USD',
        row.rail,
        row.creditor_name.replace('|', ' '),
        mask_participant(row.creditor_aba),
        mask_account(row.funding_account or row.debtor_account),
        mask_participant(row.receiver_routing),
    ])


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
class RfpPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_pay: bool = True
    allow_credit: bool = False
    max_enrollments: int = 8
    max_requests: int = 120
    min_amount: Decimal = Decimal('0.01')
    fednow_cap: Decimal = Decimal('100000.00')
    rtp_cap: Decimal = Decimal('1000000.00')
    zelle_cap: Decimal = Decimal('2500.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    expiry_days: int = 7
    tz_offset_hours: int = -4
    receiver_routing: str = DEFAULT_RECEIVER
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'RfpPolicy':
        extra = _env_list('RFP_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        routing = os.environ.get('RFP_RECEIVER') or DEFAULT_RECEIVER
        return cls(
            enabled=_env_bool('RFP_ENABLED', True),
            customer_manage=_env_bool('RFP_CUSTOMER_MANAGE', True),
            customer_pay=_env_bool('RFP_CUSTOMER_PAY', True),
            allow_credit=_env_bool('RFP_ALLOW_CREDIT', False),
            max_enrollments=max(1, _env_int('RFP_MAX_ENROLLMENTS', 8)),
            max_requests=max(1, _env_int('RFP_MAX_REQUESTS', 120)),
            min_amount=_env_money('RFP_MIN_AMOUNT', '0.01'),
            fednow_cap=_env_money('RFP_FEDNOW_CAP', '100000.00'),
            rtp_cap=_env_money('RFP_RTP_CAP', '1000000.00'),
            zelle_cap=_env_money('RFP_ZELLE_CAP', '2500.00'),
            dual_control_threshold=_env_money('RFP_DUAL_CONTROL', '10000.00'),
            expiry_days=max(1, _env_int('RFP_EXPIRY_DAYS', 7)),
            tz_offset_hours=_env_int('RFP_TZ_OFFSET', -4),
            receiver_routing=normalize_participant(routing),
            watchlist=watch,
            extra_holidays=_env_list('RFP_HOLIDAYS'),
        )

    def cap_for(self, rail: str) -> Decimal:
        if rail == RAIL_RTP:
            return self.rtp_cap
        if rail == RAIL_ZELLE:
            return self.zelle_cap
        return self.fednow_cap


@dataclass
class RfpEnrollment:
    enrollment_id: str
    userid: str
    nickname: str
    destination_account: str
    alias_kind: str
    alias_value: str
    fingerprint: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'enrollment_id': self.enrollment_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'account_last4': mask_account(self.destination_account),
            'alias_kind': self.alias_kind,
            'alias_masked': mask_alias(self.alias_kind, self.alias_value),
            'status': self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


@dataclass
class RfpRequest:
    request_id: str
    end_to_end_id: str
    userid: str
    debtor_account: str
    funding_account: str
    amount_usd: str
    creditor_name: str
    creditor_aba: str
    creditor_account: str
    alias_kind: str
    alias_value: str
    rail: str
    receiver_routing: str
    status: str
    reason: str
    actor: str
    payer: str
    releaser: str
    ofac_hit: bool
    ofac_match: str
    ofac_overridden: bool
    uetr: str
    created_at: float
    updated_at: float
    paid_at: float = 0.0
    returned_at: float = 0.0
    expiry_at: float = 0.0
    note: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'request_id': self.request_id,
            'end_to_end_id': self.end_to_end_id,
            'end_to_end_id_masked': mask_end_to_end_id(self.end_to_end_id),
            'userid': self.userid,
            'account_last4': mask_account(self.funding_account or self.debtor_account),
            'amount_usd': self.amount_usd,
            'creditor_name': self.creditor_name,
            'creditor_aba_masked': mask_participant(self.creditor_aba),
            'creditor_last4': mask_account(self.creditor_account),
            'alias_kind': self.alias_kind,
            'alias_masked': mask_alias(self.alias_kind, self.alias_value),
            'rail': self.rail,
            'status': self.status,
            'reason': self.reason,
            'ofac_hit': self.ofac_hit,
            'uetr_masked': mask_uetr(self.uetr) if self.uetr else '',
            'payable': self.status in PAYABLE,
            'declinable': self.status == RFP_REQUESTED,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'paid_at': self.paid_at,
            'expiry_at': self.expiry_at,
        }


def _enrollment_from_row(row: Any) -> RfpEnrollment:
    data = dict(row)
    return RfpEnrollment(
        enrollment_id=data['enrollment_id'], userid=data['userid'], nickname=data['nickname'],
        destination_account=data['destination_account'], alias_kind=data['alias_kind'],
        alias_value=data['alias_value'], fingerprint=data['fingerprint'], status=data['status'],
        actor=data['actor'], created_at=float(data['created_at']), updated_at=float(data['updated_at']),
    )


def _request_from_row(row: Any) -> RfpRequest:
    data = dict(row)
    return RfpRequest(
        request_id=data['request_id'], end_to_end_id=data['end_to_end_id'], userid=data['userid'],
        debtor_account=data['debtor_account'], funding_account=data['funding_account'],
        amount_usd=data['amount_usd'], creditor_name=data['creditor_name'],
        creditor_aba=data['creditor_aba'], creditor_account=data['creditor_account'],
        alias_kind=data['alias_kind'], alias_value=data['alias_value'], rail=data['rail'],
        receiver_routing=data['receiver_routing'], status=data['status'], reason=data['reason'],
        actor=data['actor'], payer=data['payer'], releaser=data['releaser'],
        ofac_hit=bool(int(data['ofac_hit'])), ofac_match=data['ofac_match'],
        ofac_overridden=bool(int(data['ofac_overridden'])), uetr=data['uetr'] or '',
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
        paid_at=float(data['paid_at'] or 0), returned_at=float(data['returned_at'] or 0),
        expiry_at=float(data['expiry_at'] or 0), note=data.get('note') or '',
    )


class MemoryRfpStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.enrollments: Dict[str, RfpEnrollment] = {}
        self.requests: Dict[str, RfpRequest] = {}
        self._by_e2e: Dict[str, str] = {}
        self._by_fp: Dict[str, str] = {}
        self._by_account: Dict[str, str] = {}

    def put_enrollment(self, row: RfpEnrollment) -> None:
        with self._lock:
            self.enrollments[row.enrollment_id] = row
            if row.fingerprint:
                self._by_fp[row.fingerprint] = row.enrollment_id
            if row.destination_account:
                self._by_account[row.destination_account] = row.enrollment_id

    def get_enrollment(self, enrollment_id: str) -> Optional[RfpEnrollment]:
        return self.enrollments.get(enrollment_id)

    def get_enrollment_by_fingerprint(self, fingerprint: str) -> Optional[RfpEnrollment]:
        if not fingerprint:
            return None
        enrollment_id = self._by_fp.get(fingerprint)
        return self.enrollments.get(enrollment_id) if enrollment_id else None

    def get_enrollment_by_account(self, account: str) -> Optional[RfpEnrollment]:
        enrollment_id = self._by_account.get(account)
        return self.enrollments.get(enrollment_id) if enrollment_id else None

    def update_enrollment(self, row: RfpEnrollment) -> None:
        self.put_enrollment(row)

    def list_enrollments(self, userid: Optional[str] = None) -> List[RfpEnrollment]:
        rows = list(self.enrollments.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        return sorted(rows, key=lambda row: row.created_at)

    def put_request(self, row: RfpRequest) -> None:
        with self._lock:
            self.requests[row.request_id] = row
            self._by_e2e[row.end_to_end_id] = row.request_id

    def get_request(self, request_id: str) -> Optional[RfpRequest]:
        return self.requests.get(request_id)

    def get_by_end_to_end_id(self, end_to_end_id: str) -> Optional[RfpRequest]:
        request_id = self._by_e2e.get(end_to_end_id)
        return self.requests.get(request_id) if request_id else None

    def update_request(self, row: RfpRequest) -> None:
        self.put_request(row)

    def list_requests(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[RfpRequest]:
        rows = list(self.requests.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)


class SqliteRfpStore:
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
                CREATE TABLE IF NOT EXISTS enrollments (
                    enrollment_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    destination_account TEXT NOT NULL,
                    alias_kind TEXT NOT NULL,
                    alias_value TEXT NOT NULL DEFAULT '',
                    fingerprint TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    end_to_end_id TEXT NOT NULL UNIQUE,
                    userid TEXT NOT NULL DEFAULT '',
                    debtor_account TEXT NOT NULL DEFAULT '',
                    funding_account TEXT NOT NULL DEFAULT '',
                    amount_usd TEXT NOT NULL,
                    creditor_name TEXT NOT NULL,
                    creditor_aba TEXT NOT NULL,
                    creditor_account TEXT NOT NULL,
                    alias_kind TEXT NOT NULL,
                    alias_value TEXT NOT NULL DEFAULT '',
                    rail TEXT NOT NULL,
                    receiver_routing TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    payer TEXT NOT NULL DEFAULT '',
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    ofac_overridden INTEGER NOT NULL DEFAULT 0,
                    uetr TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    paid_at REAL NOT NULL DEFAULT 0,
                    returned_at REAL NOT NULL DEFAULT 0,
                    expiry_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_enrollment(self, row: RfpEnrollment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO enrollments (
                    enrollment_id, userid, nickname, destination_account, alias_kind, alias_value,
                    fingerprint, status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.enrollment_id, row.userid, row.nickname, row.destination_account, row.alias_kind,
                    row.alias_value, row.fingerprint, row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_enrollment(self, enrollment_id: str) -> Optional[RfpEnrollment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM enrollments WHERE enrollment_id = ?', (enrollment_id,)).fetchone()
        return _enrollment_from_row(row) if row else None

    def get_enrollment_by_fingerprint(self, fingerprint: str) -> Optional[RfpEnrollment]:
        if not fingerprint:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM enrollments WHERE fingerprint = ? ORDER BY created_at DESC LIMIT 1',
                (fingerprint,),
            ).fetchone()
        return _enrollment_from_row(row) if row else None

    def get_enrollment_by_account(self, account: str) -> Optional[RfpEnrollment]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM enrollments WHERE destination_account = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (account,),
            ).fetchone()
        return _enrollment_from_row(row) if row else None

    def update_enrollment(self, row: RfpEnrollment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE enrollments SET nickname=?, destination_account=?, alias_kind=?, alias_value=?,
                    fingerprint=?, status=?, actor=?, updated_at=?
                WHERE enrollment_id=?
                """,
                (
                    row.nickname, row.destination_account, row.alias_kind, row.alias_value,
                    row.fingerprint, row.status, row.actor, row.updated_at, row.enrollment_id,
                ),
            )
            conn.commit()

    def list_enrollments(self, userid: Optional[str] = None) -> List[RfpEnrollment]:
        sql = 'SELECT * FROM enrollments'
        args: Tuple[Any, ...] = ()
        if userid is not None:
            sql += ' WHERE userid = ?'
            args = (userid,)
        sql += ' ORDER BY created_at'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_enrollment_from_row(row) for row in rows]

    def put_request(self, row: RfpRequest) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO requests (
                    request_id, end_to_end_id, userid, debtor_account, funding_account, amount_usd,
                    creditor_name, creditor_aba, creditor_account, alias_kind, alias_value, rail,
                    receiver_routing, status, reason, actor, payer, releaser, ofac_hit, ofac_match,
                    ofac_overridden, uetr, created_at, updated_at, paid_at, returned_at, expiry_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.request_id, row.end_to_end_id, row.userid, row.debtor_account, row.funding_account,
                    row.amount_usd, row.creditor_name, row.creditor_aba, row.creditor_account,
                    row.alias_kind, row.alias_value, row.rail, row.receiver_routing, row.status,
                    row.reason, row.actor, row.payer, row.releaser, int(row.ofac_hit), row.ofac_match,
                    int(row.ofac_overridden), row.uetr, row.created_at, row.updated_at, row.paid_at,
                    row.returned_at, row.expiry_at, row.note,
                ),
            )
            conn.commit()

    def get_request(self, request_id: str) -> Optional[RfpRequest]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM requests WHERE request_id = ?', (request_id,)).fetchone()
        return _request_from_row(row) if row else None

    def get_by_end_to_end_id(self, end_to_end_id: str) -> Optional[RfpRequest]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM requests WHERE end_to_end_id = ?', (end_to_end_id,)).fetchone()
        return _request_from_row(row) if row else None

    def update_request(self, row: RfpRequest) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE requests SET userid=?, debtor_account=?, funding_account=?, amount_usd=?,
                    creditor_name=?, creditor_aba=?, creditor_account=?, alias_kind=?, alias_value=?,
                    rail=?, receiver_routing=?, status=?, reason=?, actor=?, payer=?, releaser=?,
                    ofac_hit=?, ofac_match=?, ofac_overridden=?, uetr=?, updated_at=?, paid_at=?,
                    returned_at=?, expiry_at=?, note=?
                WHERE request_id=?
                """,
                (
                    row.userid, row.debtor_account, row.funding_account, row.amount_usd, row.creditor_name,
                    row.creditor_aba, row.creditor_account, row.alias_kind, row.alias_value, row.rail,
                    row.receiver_routing, row.status, row.reason, row.actor, row.payer, row.releaser,
                    int(row.ofac_hit), row.ofac_match, int(row.ofac_overridden), row.uetr, row.updated_at,
                    row.paid_at, row.returned_at, row.expiry_at, row.note, row.request_id,
                ),
            )
            conn.commit()

    def list_requests(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[RfpRequest]:
        clauses = []
        args: List[Any] = []
        if userid is not None:
            clauses.append('userid = ?')
            args.append(userid)
        if status is not None:
            clauses.append('status = ?')
            args.append(status)
        sql = 'SELECT * FROM requests'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        return [_request_from_row(row) for row in rows]


class RfpService:
    def __init__(
        self,
        policy: RfpPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[InstantClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: datetime.now(tz=timezone.utc).timestamp())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn or screen_name
        self.calendar = calendar or InstantClock(
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )

    def _now(self) -> float:
        return float(self.clock())

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise RfpError('rfp_disabled', 'Request-for-Payment is disabled.')

    def _require_staff(self, actor_type: str) -> None:
        if actor_type not in EMPLOYEE_ROLES:
            raise RfpError('rfp_forbidden', 'Staff only.')

    def _require_customer_manage(self, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if not self.policy.customer_manage:
            raise RfpError('rfp_forbidden', 'Customers cannot manage RfP enrollments.')

    def _accounts(self, userid: str) -> Any:
        if not self.accounts_fn:
            return {}
        return self.accounts_fn(userid) or {}

    def _owned(self, userid: str, account: str) -> None:
        owned = own_accounts_from_customer_payload(self._accounts(userid))
        if account not in owned:
            raise RfpError('invalid_account', 'Account is not an account of this customer.')

    def _account_type(self, userid: str, account: str) -> str:
        return account_types_from_customer_payload(self._accounts(userid)).get(account, '')

    def _debit_allowed(self, userid: str, account: str) -> bool:
        kind = self._account_type(userid, account)
        if kind == 'credit' and not self.policy.allow_credit:
            return False
        return True

    def add_enrollment(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: str,
        destination_account: Any,
        kind: Any = KIND_NONE,
        value: Any = '',
    ) -> RfpEnrollment:
        self._require_enabled()
        self._require_customer_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise RfpError('rfp_forbidden', 'Cannot enroll RfP for another customer.')
        kind_n = normalize_kind(kind)
        alias_value = normalize_alias_value(kind_n, value, required=(kind_n != KIND_NONE))
        account = normalize_account(destination_account)
        self._owned(owner_userid, account)
        if not self._debit_allowed(owner_userid, account):
            raise RfpError('credit_not_allowed', 'Credit accounts cannot pay RfPs.')
        fingerprint = alias_fingerprint(kind_n, alias_value)
        if fingerprint:
            existing = self.store.get_enrollment_by_fingerprint(fingerprint)
            if existing and existing.status in OPEN_ENROLL:
                raise RfpError('enrollment_duplicate', 'That RfP alias is already enrolled.')
        by_account = self.store.get_enrollment_by_account(account)
        if by_account and by_account.status in OPEN_ENROLL:
            raise RfpError('enrollment_duplicate', 'That account is already enrolled for RfP.')
        open_count = sum(1 for row in self.store.list_enrollments(owner_userid) if row.status in OPEN_ENROLL)
        if open_count >= self.policy.max_enrollments:
            raise RfpError('enrollment_limit', 'RfP enrollment limit reached.')
        now = self._now()
        row = RfpEnrollment(
            enrollment_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=normalize_nickname(nickname),
            destination_account=account,
            alias_kind=kind_n,
            alias_value=alias_value,
            fingerprint=fingerprint,
            status=ENROLL_ACTIVE,
            actor=actor,
            created_at=now,
            updated_at=now,
        )
        self.store.put_enrollment(row)
        return row

    def set_enrollment_status(
        self,
        *,
        enrollment_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> RfpEnrollment:
        self._require_enabled()
        row = self.store.get_enrollment(enrollment_id)
        if row is None:
            raise RfpError('enrollment_not_found', 'RfP enrollment not found.')
        if actor_type not in EMPLOYEE_ROLES and actor != row.userid:
            raise RfpError('rfp_forbidden', 'Cannot change another customer enrollment.')
        self._require_customer_manage(actor_type)
        if status not in ENROLL_STATUSES:
            raise RfpError('invalid_status', 'Unknown enrollment status.')
        if row.status == ENROLL_CANCELLED:
            raise RfpError('already_cancelled', 'Cancelled enrollments cannot change.')
        if status == ENROLL_PAUSED and row.status != ENROLL_ACTIVE:
            raise RfpError('already_paused' if row.status == ENROLL_PAUSED else 'invalid_status', 'Enrollment is not active.')
        if status == ENROLL_ACTIVE and row.status != ENROLL_PAUSED:
            raise RfpError('already_active', 'Enrollment is already active.')
        if status == row.status:
            code = 'already_paused' if status == ENROLL_PAUSED else 'already_active'
            raise RfpError(code, 'Enrollment already in that status.')
        updated = replace(row, status=status, actor=actor, updated_at=self._now())
        self.store.update_enrollment(updated)
        if status == ENROLL_ACTIVE:
            self._rematch_unmatched(updated)
        return updated

    def _match_enrollment(self, *, rail: str, debtor_account: str, alias_kind: str, alias_value: str) -> Optional[RfpEnrollment]:
        if rail == RAIL_ZELLE:
            fingerprint = alias_fingerprint(alias_kind, alias_value)
            row = self.store.get_enrollment_by_fingerprint(fingerprint)
        else:
            account = normalize_account(debtor_account) if debtor_account else ''
            row = self.store.get_enrollment_by_account(account) if account else None
        if row is None or row.status != ENROLL_ACTIVE:
            return None
        return row

    def _rematch_unmatched(self, enrollment: RfpEnrollment) -> None:
        for request in self.store.list_requests(status=RFP_UNMATCHED):
            matched = self._match_enrollment(
                rail=request.rail, debtor_account=request.debtor_account or enrollment.destination_account,
                alias_kind=request.alias_kind, alias_value=request.alias_value,
            )
            if matched is None or matched.enrollment_id != enrollment.enrollment_id:
                continue
            request.userid = enrollment.userid
            request.debtor_account = enrollment.destination_account
            request.updated_at = self._now()
            self.store.update_request(request)
            self._evaluate(request, rescreen=True)

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        values: Dict[str, Any],
    ) -> Tuple[RfpRequest, bool]:
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
    ) -> Tuple[List[RfpRequest], int]:
        self._require_enabled()
        self._require_staff(actor_type)
        created = 0
        rows = []
        for record in split_pain013_file(payload):
            parsed = parse_pain013(record)
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
            raise RfpError('invalid_date', 'Expiry date is invalid.') from exc

    def _ingest_parsed(self, *, actor: str, parsed: Dict[str, Any]) -> Tuple[RfpRequest, bool]:
        if parsed['receiver_routing'] != self.policy.receiver_routing:
            raise RfpError('wrong_receiver', 'RfP is not for this receiver FI.')
        existing = self.store.get_by_end_to_end_id(parsed['end_to_end_id'])
        if existing is not None:
            return existing, False
        total = len(self.store.list_requests())
        if total >= self.policy.max_requests:
            raise RfpError('request_limit', 'Inbound RfP limit reached.')
        amount = parsed['amount']
        if amount < self.policy.min_amount:
            raise RfpError('amount_out_of_range', 'Amount is below the RfP minimum.')
        cap = self.policy.cap_for(parsed['rail'])
        if amount > cap:
            code = {
                RAIL_FEDNOW: 'fednow_amount_exceeded',
                RAIL_RTP: 'rtp_amount_exceeded',
                RAIL_ZELLE: 'zelle_amount_exceeded',
            }[parsed['rail']]
            raise RfpError(code, 'Amount exceeds the RfP rail cap.')
        now = self._now()
        debtor = parsed['debtor_account']
        row = RfpRequest(
            request_id=uuid.uuid4().hex,
            end_to_end_id=parsed['end_to_end_id'],
            userid='',
            debtor_account=normalize_account(debtor, required=False) if debtor else '',
            funding_account='',
            amount_usd=money_str(amount),
            creditor_name=parsed['creditor_name'],
            creditor_aba=parsed['creditor_aba'],
            creditor_account=parsed['creditor_account'],
            alias_kind=parsed['alias_kind'],
            alias_value=parsed['alias'],
            rail=parsed['rail'],
            receiver_routing=parsed['receiver_routing'],
            status=RFP_UNMATCHED,
            reason='',
            actor=actor,
            payer='',
            releaser='',
            ofac_hit=False,
            ofac_match='',
            ofac_overridden=False,
            uetr='',
            created_at=now,
            updated_at=now,
            expiry_at=self._expiry_at(now, parsed.get('expiry') or ''),
        )
        matched = self._match_enrollment(
            rail=row.rail, debtor_account=row.debtor_account,
            alias_kind=row.alias_kind, alias_value=row.alias_value,
        )
        if matched is not None:
            row.userid = matched.userid
            row.debtor_account = matched.destination_account
        self.store.put_request(row)
        self._evaluate(row, rescreen=True)
        return row, True

    def _evaluate(self, row: RfpRequest, *, rescreen: bool) -> RfpRequest:
        now = self._now()
        if row.status in {RFP_PAID, RFP_DECLINED, RFP_REJECTED, RFP_RETURNED, RFP_EXPIRED, RFP_FAILED}:
            return row
        if row.expiry_at and now > row.expiry_at and row.status in OPEN_RFP:
            row.status = RFP_EXPIRED
            row.reason = 'expired'
            row.updated_at = now
            self.store.update_request(row)
            return row
        if not row.userid or not row.debtor_account:
            row.status = RFP_UNMATCHED
            row.reason = 'unmatched'
            row.updated_at = now
            self.store.update_request(row)
            return row
        try:
            self._owned(row.userid, row.debtor_account)
        except RfpError:
            row.status = RFP_UNMATCHED
            row.reason = 'unmatched'
            row.userid = ''
            row.debtor_account = ''
            row.updated_at = now
            self.store.update_request(row)
            return row
        if not self._debit_allowed(row.userid, row.debtor_account):
            row.status = RFP_UNMATCHED
            row.reason = 'credit_not_allowed'
            row.updated_at = now
            self.store.update_request(row)
            return row
        if rescreen and not row.ofac_overridden:
            hit = self.screen_fn(row.creditor_name, watchlist=self.policy.watchlist)
            row.ofac_hit = bool(hit.hit)
            row.ofac_match = hit.matched
            if row.ofac_hit:
                row.status = RFP_HELD
                row.reason = 'ofac'
                row.updated_at = now
                self.store.update_request(row)
                return row
        elif row.ofac_hit and not row.ofac_overridden:
            row.status = RFP_HELD
            row.reason = 'ofac'
            row.updated_at = now
            self.store.update_request(row)
            return row
        if row.status == RFP_PENDING and row.releaser:
            return self._pay(row, actor=row.payer or row.releaser, funding_account=row.funding_account)
        row.status = RFP_REQUESTED
        row.reason = ''
        row.updated_at = now
        self.store.update_request(row)
        return row

    def _pay(self, row: RfpRequest, *, actor: str, funding_account: str) -> RfpRequest:
        if self.debit_fn is None:
            raise RfpError('failed', 'Debit function is not configured.')
        account = funding_account or row.debtor_account
        remark = 'rfp to %s' % row.creditor_name[:40]
        result = self.debit_fn(account, row.amount_usd, remark)
        classified = _classify_money_result(result)
        now = self._now()
        if classified == 'nsf':
            raise RfpError('nsf', 'Pay debit failed for insufficient funds.', request=row)
        if classified != 'ok':
            raise RfpError('failed', 'Pay debit failed.', request=row)
        row.status = RFP_PAID
        row.reason = ''
        row.payer = actor
        row.funding_account = account
        row.uetr = row.uetr or compose_uetr()
        row.paid_at = now
        row.updated_at = now
        self.store.update_request(row)
        return row

    def preview(self, *, request_id: str = '', end_to_end_id: str = '', actor: str = '', actor_type: str = '') -> Dict[str, Any]:
        row = self._get(request_id=request_id, end_to_end_id=end_to_end_id)
        self._assert_visible(row, actor, actor_type)
        return {
            'request': row.to_dict(),
            'clock': self.calendar.snapshot(self._now()),
            'receiver': mask_participant(self.policy.receiver_routing),
            'fulfillment': compose_pacs008(row) if row.status == RFP_PAID else '',
        }

    def _get(self, *, request_id: str = '', end_to_end_id: str = '') -> RfpRequest:
        row = None
        if request_id:
            row = self.store.get_request(str(request_id).strip())
        if row is None and end_to_end_id:
            row = self.store.get_by_end_to_end_id(normalize_end_to_end_id(end_to_end_id))
        if row is None:
            raise RfpError('request_not_found', 'Inbound RfP not found.')
        return row

    def _assert_visible(self, row: RfpRequest, actor: str, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if row.userid and row.userid != actor:
            raise RfpError('rfp_forbidden', 'Cannot view another customer RfP.')

    def assign(
        self,
        *,
        request_id: str,
        actor: str,
        actor_type: str,
        customer_id: str,
        account: Any,
        nickname: str = '',
    ) -> RfpRequest:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(request_id=request_id)
        if row.status != RFP_UNMATCHED:
            raise RfpError('not_assignable', 'Only unmatched RfPs can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise RfpError('missing_customer_id', 'Customer id is required to assign.')
        dest = normalize_account(account)
        self._owned(owner, dest)
        if not self._debit_allowed(owner, dest):
            raise RfpError('credit_not_allowed', 'Credit accounts cannot pay RfPs.')
        if row.rail == RAIL_ZELLE:
            existing = self.store.get_enrollment_by_fingerprint(alias_fingerprint(row.alias_kind, row.alias_value))
        else:
            existing = self.store.get_enrollment_by_account(dest)
        if existing is None or existing.status == ENROLL_CANCELLED:
            self.add_enrollment(
                owner_userid=owner, actor=actor, actor_type=actor_type,
                nickname=nickname or ('RfP %s' % row.rail),
                destination_account=dest, kind=row.alias_kind, value=row.alias_value,
            )
        elif existing.userid != owner:
            raise RfpError('enrollment_duplicate', 'Enrollment belongs to another customer.')
        row.userid = owner
        row.debtor_account = dest
        row.updated_at = self._now()
        self.store.update_request(row)
        return self._evaluate(row, rescreen=True)

    def override_ofac(self, *, request_id: str, actor: str, actor_type: str, note: str = '') -> RfpRequest:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(request_id=request_id)
        if row.status != RFP_HELD:
            raise RfpError('not_overridable', 'Only OFAC-held RfPs can be overridden.')
        row.ofac_overridden = True
        row.ofac_hit = False
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_request(row)
        return self._evaluate(row, rescreen=False)

    def pay(
        self,
        *,
        request_id: str,
        actor: str,
        actor_type: str,
        funding_account: Any = '',
    ) -> RfpRequest:
        self._require_enabled()
        row = self._get(request_id=request_id)
        self._assert_visible(row, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_pay:
                raise RfpError('rfp_forbidden', 'Customers cannot pay RfPs.')
            if actor != row.userid:
                raise RfpError('rfp_forbidden', 'Cannot pay another customer RfP.')
        if row.status == RFP_PAID:
            raise RfpError('already_paid', 'RfP is already paid.')
        if row.status == RFP_HELD:
            raise RfpError('ofac_hold', 'OFAC-held RfPs cannot be paid.')
        if row.status not in PAYABLE:
            raise RfpError('not_payable', 'This RfP cannot be paid.')
        account = normalize_account(funding_account, required=False) if funding_account else row.debtor_account
        if not account:
            account = row.debtor_account
        self._owned(row.userid, account)
        if not self._debit_allowed(row.userid, account):
            raise RfpError('credit_not_allowed', 'Credit accounts cannot pay RfPs.')
        usd = parse_money(row.amount_usd)
        if usd >= self.policy.dual_control_threshold and not row.releaser:
            row.status = RFP_PENDING
            row.reason = 'dual_control'
            row.payer = actor
            row.funding_account = account
            row.updated_at = self._now()
            self.store.update_request(row)
            return row
        return self._pay(row, actor=actor, funding_account=account)

    def release(self, *, request_id: str, actor: str, actor_type: str) -> RfpRequest:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(request_id=request_id)
        if row.status != RFP_PENDING:
            raise RfpError('not_releasable', 'Only dual-control RfPs can be released.')
        if actor == row.payer or actor == row.actor:
            raise RfpError('same_approver', 'A different employee must release this RfP.')
        row.releaser = actor
        row.updated_at = self._now()
        self.store.update_request(row)
        return self._pay(row, actor=row.payer or actor, funding_account=row.funding_account or row.debtor_account)

    def decline(
        self,
        *,
        request_id: str,
        actor: str,
        actor_type: str,
        reason: str = 'refuse',
    ) -> RfpRequest:
        self._require_enabled()
        row = self._get(request_id=request_id)
        self._assert_visible(row, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != row.userid:
            raise RfpError('rfp_forbidden', 'Cannot decline another customer RfP.')
        if row.status != RFP_REQUESTED:
            raise RfpError('not_declinable', 'Only open requested RfPs can be declined.')
        row.status = RFP_DECLINED
        row.reason = normalize_decline_reason(reason)
        row.updated_at = self._now()
        self.store.update_request(row)
        return row

    def reject(self, *, request_id: str, actor: str, actor_type: str, note: str = '') -> RfpRequest:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(request_id=request_id)
        if row.status not in OPEN_RFP:
            raise RfpError('not_rejectable', 'This RfP cannot be rejected.')
        row.status = RFP_REJECTED
        row.reason = 'rejected'
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_request(row)
        return row

    def return_request(
        self,
        *,
        request_id: str,
        actor: str,
        actor_type: str,
        reason: str = 'other',
    ) -> RfpRequest:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(request_id=request_id)
        now = self._now()
        if row.status == RFP_RETURNED or row.returned_at:
            raise RfpError('already_returned', 'RfP already returned.')
        if row.status in OPEN_RFP:
            row.status = RFP_RETURNED
            row.reason = reason or 'other'
            row.returned_at = now
            row.updated_at = now
            self.store.update_request(row)
            return row
        if row.status != RFP_PAID:
            raise RfpError('not_returnable', 'This RfP cannot be returned.')
        if self.credit_fn is None:
            raise RfpError('return_failed', 'Credit function is not configured.')
        account = row.funding_account or row.debtor_account
        remark = 'rfp return %s' % row.end_to_end_id[-12:]
        result = self.credit_fn(account, row.amount_usd, remark)
        classified = _classify_money_result(result)
        if classified == 'nsf':
            raise RfpError('nsf', 'Return credit failed for insufficient funds.', request=row)
        if classified != 'ok':
            raise RfpError('return_failed', 'Return credit failed.', request=row)
        row.status = RFP_RETURNED
        row.reason = reason or 'other'
        row.returned_at = now
        row.updated_at = now
        self.store.update_request(row)
        return row

    def export_fulfillment(self, *, request_id: str, actor: str, actor_type: str) -> Dict[str, Any]:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(request_id=request_id)
        _ = actor
        if row.status != RFP_PAID:
            raise RfpError('not_exportable', 'Only paid RfPs can be exported.')
        return {
            'request': row.to_dict(),
            'file': compose_pacs008(row),
            'reject': compose_pain014(row, reason=row.reason or 'other') if row.status != RFP_PAID else '',
        }

    def run_due(self, userid: Optional[str] = None) -> List[RfpRequest]:
        now = self._now()
        changed = []
        rows = self.store.list_requests(userid) if userid else self.store.list_requests()
        for row in rows:
            if row.status in OPEN_RFP and row.expiry_at and now > row.expiry_at:
                row.status = RFP_EXPIRED
                row.reason = 'expired'
                row.updated_at = now
                self.store.update_request(row)
                changed.append(row)
        return changed

    def snapshot(self, userid: str, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        _ = actor
        _ = actor_type
        now = self._now()
        if userid:
            self.run_due(userid)
        enrollments = self.store.list_enrollments(userid) if userid else []
        requests = self.store.list_requests(userid) if userid else []
        paid = [row for row in requests if row.status == RFP_PAID]
        ytd = sum((parse_money(row.amount_usd, allow_zero=True) for row in paid), Decimal('0.00'))
        return {
            'enabled': self.policy.enabled,
            'clock': self.calendar.snapshot(now),
            'receiver': mask_participant(self.policy.receiver_routing),
            'enrollments': [row.to_dict() for row in enrollments[:40]],
            'requests': [row.to_dict() for row in requests[:40]],
            'ytd_paid': money_str(ytd),
            'open_count': sum(1 for row in requests if row.status in OPEN_RFP),
            'active_enrollments': sum(1 for row in enrollments if row.status == ENROLL_ACTIVE),
            'fednow_cap': money_str(self.policy.fednow_cap),
            'rtp_cap': money_str(self.policy.rtp_cap),
            'zelle_cap': money_str(self.policy.zelle_cap),
            'dual_control': money_str(self.policy.dual_control_threshold),
        }

    def unmatched_snapshot(self) -> Dict[str, Any]:
        rows = self.store.list_requests(status=RFP_UNMATCHED)
        return {
            'enabled': self.policy.enabled,
            'unmatched': [row.to_dict() for row in rows[:40]],
            'count': len(rows),
        }


_SERVICE: Optional[RfpService] = None


def set_service(service: Optional[RfpService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[RfpService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('RFP_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryRfpStore()
    path = os.environ.get('RFP_DB', DEFAULT_STORE_PATH)
    return SqliteRfpStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[InstantClock] = None,
) -> RfpService:
    if store is None:
        store = default_store()
    return RfpService(
        RfpPolicy.from_env(),
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
        'enrollment_duplicate': 409,
        'enrollment_limit': 409,
        'request_limit': 409,
        'already_cancelled': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_paid': 409,
        'already_returned': 409,
        'nsf': 409,
        'failed': 409,
        'return_failed': 409,
        'rfp_forbidden': 403,
        'rfp_disabled': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_payable': 403,
        'not_declinable': 403,
        'not_returnable': 403,
        'not_exportable': 403,
        'enrollment_not_found': 404,
        'request_not_found': 404,
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
        'invalid_reason': 400,
        'wrong_receiver': 400,
        'fednow_amount_exceeded': 400,
        'rtp_amount_exceeded': 400,
        'zelle_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_request': 400,
        'missing_enrollment': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: RfpError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('request') is not None:
        body['request'] = exc.extra['request'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except RfpError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snapshot_for(service: RfpService, userid: str, actor: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(userid, actor=actor, actor_type=actor_type)


def handle_list(service: RfpService):
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
    return jsonify({'Rfp': _snapshot_for(service, owner, userid, actor_type)}), 200


def handle_unmatched(service: RfpService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'rfp_forbidden'}), 403
    snap = service.unmatched_snapshot()
    return jsonify({'UnmatchedRfps': snap, 'Rfp': snap}), 200


def handle_add_enrollment(service: RfpService):
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
                raise RfpError('missing_customer_id', 'Customer id is required.')
        enrollment = service.add_enrollment(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname') or '',
            destination_account=values.get('destination_account') or values.get('account') or values.get('default_account'),
            kind=values.get('kind') or values.get('alias_type') or KIND_NONE,
            value=values.get('alias') or values.get('value') or values.get('email') or values.get('phone') or '',
        )
        return jsonify({
            'message': 'RfP enrollment added',
            'enrollment': enrollment.to_dict(),
            'Rfp': _snapshot_for(service, enrollment.userid, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _enrollment_status_route(service: RfpService, status: str, message: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        enrollment_id = str(values.get('enrollment_id') or '').strip()
        if not enrollment_id:
            raise RfpError('missing_enrollment', 'enrollment_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.set_enrollment_status(
            enrollment_id=enrollment_id, actor=userid, actor_type=actor_type, status=status,
        )
        return jsonify({
            'message': message,
            'enrollment': row.to_dict(),
            'Rfp': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: RfpService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        preview = service.preview(
            request_id=str(values.get('request_id') or ''),
            end_to_end_id=str(values.get('end_to_end_id') or values.get('e2e') or values.get('reference') or ''),
            actor=userid,
            actor_type=actor_type,
        )
        owner = preview['request'].get('userid') or userid
        return jsonify({'preview': preview, 'Rfp': _snapshot_for(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest(service: RfpService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'RfP ingested' if created else 'RfP already ingested',
            'request': row.to_dict(),
            'Rfp': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_ingest_file(service: RfpService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        payload = values.get('file') or values.get('body') or values.get('payload')
        if not payload:
            raise RfpError('missing_file', 'RfP file body is required.')
        actor_type = session.get('usertype') or 'customer'
        rows, created = service.ingest_file(actor=userid, actor_type=actor_type, payload=str(payload))
        owner = rows[-1].userid if rows and rows[-1].userid else userid
        return jsonify({
            'message': 'RfP file ingested',
            'created': created,
            'requests': [row.to_dict() for row in rows],
            'Rfp': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_pay(service: RfpService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        request_id = str(values.get('request_id') or '').strip()
        if not request_id:
            raise RfpError('missing_request', 'request_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.pay(
            request_id=request_id, actor=userid, actor_type=actor_type,
            funding_account=values.get('account') or values.get('funding_account') or '',
        )
        return jsonify({
            'message': 'RfP paid' if row.status == RFP_PAID else 'RfP pending release',
            'request': row.to_dict(),
            'Rfp': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_decline(service: RfpService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        request_id = str(values.get('request_id') or '').strip()
        if not request_id:
            raise RfpError('missing_request', 'request_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.decline(
            request_id=request_id, actor=userid, actor_type=actor_type,
            reason=str(values.get('reason') or 'refuse'),
        )
        return jsonify({
            'message': 'RfP declined',
            'request': row.to_dict(),
            'Rfp': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_assign(service: RfpService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        request_id = str(values.get('request_id') or '').strip()
        if not request_id:
            raise RfpError('missing_request', 'request_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.assign(
            request_id=request_id,
            actor=userid,
            actor_type=actor_type,
            customer_id=str(values.get('customer_id') or ''),
            account=values.get('account') or values.get('destination_account'),
            nickname=str(values.get('nickname') or ''),
        )
        return jsonify({
            'message': 'RfP assigned',
            'request': row.to_dict(),
            'Rfp': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_request_route(service: RfpService, action: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        request_id = str(values.get('request_id') or '').strip()
        if not request_id:
            raise RfpError('missing_request', 'request_id is required.')
        actor_type = session.get('usertype') or 'customer'
        if action == 'override':
            row = service.override_ofac(
                request_id=request_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'release':
            row = service.release(request_id=request_id, actor=userid, actor_type=actor_type)
            message = 'RfP released'
        elif action == 'reject':
            row = service.reject(
                request_id=request_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'RfP rejected'
        elif action == 'return':
            row = service.return_request(
                request_id=request_id, actor=userid, actor_type=actor_type,
                reason=str(values.get('reason') or 'other'),
            )
            message = 'RfP returned'
        elif action == 'export':
            exported = service.export_fulfillment(
                request_id=request_id, actor=userid, actor_type=actor_type,
            )
            return jsonify({
                'message': 'RfP exported',
                'file': exported['file'],
                'request': exported['request'],
                'Rfp': _snapshot_for(service, exported['request'].get('userid') or userid, userid, actor_type),
            }), 200
        else:
            raise RfpError('invalid_status', 'Unknown RfP action.')
        return jsonify({
            'message': message,
            'request': row.to_dict(),
            'Rfp': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: RfpService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Rfp': _snapshot_for(service, owner, userid, actor_type)}), 200


def attach_rfp_routes(app, service: RfpService) -> None:
    @app.route('/listRfps', methods=['POST', 'GET'])
    def list_rfps_route():
        return handle_list(service)

    @app.route('/listRfpEnrollments', methods=['POST', 'GET'])
    def list_rfp_enrollments_route():
        return handle_list(service)

    @app.route('/listUnmatchedRfps', methods=['POST', 'GET'])
    def list_unmatched_rfps_route():
        return handle_unmatched(service)

    @app.route('/enrollRfp', methods=['POST', 'GET'])
    def enroll_rfp_route():
        return handle_add_enrollment(service)

    @app.route('/pauseRfpEnrollment', methods=['POST', 'GET'])
    def pause_rfp_enrollment_route():
        return _enrollment_status_route(service, ENROLL_PAUSED, 'RfP enrollment paused')

    @app.route('/resumeRfpEnrollment', methods=['POST', 'GET'])
    def resume_rfp_enrollment_route():
        return _enrollment_status_route(service, ENROLL_ACTIVE, 'RfP enrollment resumed')

    @app.route('/cancelRfpEnrollment', methods=['POST', 'GET'])
    def cancel_rfp_enrollment_route():
        return _enrollment_status_route(service, ENROLL_CANCELLED, 'RfP enrollment cancelled')

    @app.route('/previewRfp', methods=['POST', 'GET'])
    def preview_rfp_route():
        return handle_preview(service)

    @app.route('/ingestRfp', methods=['POST', 'GET'])
    def ingest_rfp_route():
        return handle_ingest(service)

    @app.route('/ingestRfpFile', methods=['POST', 'GET'])
    def ingest_rfp_file_route():
        return handle_ingest_file(service)

    @app.route('/payRfp', methods=['POST', 'GET'])
    def pay_rfp_route():
        return handle_pay(service)

    @app.route('/declineRfp', methods=['POST', 'GET'])
    def decline_rfp_route():
        return handle_decline(service)

    @app.route('/assignRfp', methods=['POST', 'GET'])
    def assign_rfp_route():
        return handle_assign(service)

    @app.route('/overrideRfpOfac', methods=['POST', 'GET'])
    def override_rfp_ofac_route():
        return _staff_request_route(service, 'override')

    @app.route('/releaseRfp', methods=['POST', 'GET'])
    def release_rfp_route():
        return _staff_request_route(service, 'release')

    @app.route('/rejectRfp', methods=['POST', 'GET'])
    def reject_rfp_route():
        return _staff_request_route(service, 'reject')

    @app.route('/returnRfp', methods=['POST', 'GET'])
    def return_rfp_route():
        return _staff_request_route(service, 'return')

    @app.route('/exportRfp', methods=['POST', 'GET'])
    def export_rfp_route():
        return _staff_request_route(service, 'export')

    @app.route('/runDueRfps', methods=['POST', 'GET'])
    def run_due_rfps_route():
        return handle_run_due(service)
