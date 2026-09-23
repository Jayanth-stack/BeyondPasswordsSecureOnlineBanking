"""FedNow / TCH RTP instant origination and Request-for-Payment.

Customers send 24/7 USD instant payments to external counterparties identified
by a full ABA routing number, account, legal name, and address. Independent of
domestic Fedwire (PR #73), SWIFT MT103 (PR #78), SEPA SCT (PR #80), ACH
linking / micro-deposits (PR #68), bill-pay outgoing ACH (PR #66), inbound
payroll splits (PR #64), the in-bank payee allowlist (PR #26), and scheduled
internal transfers (PR #36). Existing `/fundTransfer`, `/withdrawAmount`,
and `/sendWire` stay unchanged. `Customers.debit_request` / `credit_request`
still write `debited` / `direct deposited` unless a remark is supplied here.

Foundations (reusable beyond this screen):
- Instant 24/7/365 clock (no Fedwire cutoff / weekend queue)
- Rail selection: FedNow vs The Clearing House RTP
- ISO 20022 identifiers (UETR, EndToEndId, MsgId, TxId)
- pacs.008 credit-transfer and pain.013 Request-for-Payment field maps
- Per-rail outbound fee (FedNow $1.00 / RTP $0.45)
- Dual-control release for high-value credits
- Request-for-Payment (RfP) inbound collection with expiry
- Reuses ABA checksum + OFAC screen from utility.wire

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Destination account numbers never appear in to_dict / snapshots.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    AccountError,
    AmountError,
    DEFAULT_WATCHLIST,
    US_STATES,
    WireError,
    aba_check_digit_ok,
    account_types_from_customer_payload,
    last4,
    money_str,
    normalize_aba as wire_normalize_aba,
    normalize_account,
    normalize_id,
    normalize_note,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
    ScreenResult,
)

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
PARTY_ACTIVE = 'active'
PARTY_PAUSED = 'paused'
PARTY_ARCHIVED = 'archived'
PARTY_STATUSES = frozenset({PARTY_ACTIVE, PARTY_PAUSED, PARTY_ARCHIVED})
OPEN_PARTY = frozenset({PARTY_ACTIVE, PARTY_PAUSED})
PAY_HELD = 'held'
PAY_PENDING = 'pending_release'
PAY_COMPLETED = 'completed'
PAY_REJECTED = 'rejected'
PAY_CANCELLED = 'cancelled'
PAY_NSF = 'nsf'
PAY_FAILED = 'failed'
PAY_STATUSES = frozenset({
    PAY_HELD, PAY_PENDING, PAY_COMPLETED, PAY_REJECTED, PAY_CANCELLED, PAY_NSF, PAY_FAILED,
})
OPEN_PAYMENTS = frozenset({PAY_HELD, PAY_PENDING})
CANCELABLE = frozenset({PAY_HELD, PAY_PENDING})
RFP_REQUESTED = 'requested'
RFP_HELD = 'held'
RFP_ACCEPTED = 'accepted'
RFP_REJECTED = 'rejected'
RFP_EXPIRED = 'expired'
RFP_CANCELLED = 'cancelled'
RFP_STATUSES = frozenset({
    RFP_REQUESTED, RFP_HELD, RFP_ACCEPTED, RFP_REJECTED, RFP_EXPIRED, RFP_CANCELLED,
})
OPEN_RFPS = frozenset({RFP_REQUESTED, RFP_HELD})
FEE_NONE = 'none'
FEE_COLLECTED = 'collected'
FEE_WAIVED = 'waived'
FEE_NSF = 'nsf'
RAIL_FEDNOW = 'fednow'
RAIL_RTP = 'rtp'
RAILS = frozenset({RAIL_FEDNOW, RAIL_RTP})
RAIL_ALIASES = {
    'fed': RAIL_FEDNOW, 'now': RAIL_FEDNOW, 'fn': RAIL_FEDNOW, 'fed_now': RAIL_FEDNOW,
    'federal': RAIL_FEDNOW, 'frb': RAIL_FEDNOW,
    'tch': RAIL_RTP, 'real_time': RAIL_RTP, 'realtime': RAIL_RTP, 'rtp_tch': RAIL_RTP,
}
RAIL_FEES = {RAIL_FEDNOW: Decimal('1.00'), RAIL_RTP: Decimal('0.45')}
RAIL_CAPS = {RAIL_FEDNOW: Decimal('1000000.00'), RAIL_RTP: Decimal('1000000.00')}
CLR_SYS = {RAIL_FEDNOW: 'FDN', RAIL_RTP: 'TCH'}
LCL_INSTRM = {RAIL_FEDNOW: 'FDN', RAIL_RTP: 'RTP'}
PURPOSES = frozenset({'family', 'goods', 'payroll', 'tax', 'loan', 'rent', 'other'})
PURPOSE_ALIASES = {
    'personal': 'family', 'gift': 'family', 'support': 'family',
    'invoice': 'goods', 'purchase': 'goods', 'vendor': 'goods',
    'salary': 'payroll', 'wage': 'payroll',
    'irs': 'tax', 'taxes': 'tax',
    'mortgage': 'loan', 'housing': 'rent',
}
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/rtp.sqlite'
DEFAULT_RFP_TTL = 7 * 24 * 3600
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)


class RtpError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def _from_wire(exc: WireError) -> RtpError:
    return RtpError(exc.code, exc.message, **exc.extra)


def normalize_aba(value: Any) -> str:
    try:
        return wire_normalize_aba(value)
    except WireError as exc:
        raise _from_wire(exc) from exc


def normalize_external_account(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not (4 <= len(digits) <= 17):
        raise RtpError('invalid_account', 'External account must be 4-17 digits.')
    return digits


def normalize_nickname(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise RtpError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise RtpError('invalid_name', 'Counterparty legal name must be 2-80 characters.')
    return text


def normalize_street(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (3 <= len(text) <= 80):
        raise RtpError('invalid_address', 'Street must be 3-80 characters.')
    return text


def normalize_city(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise RtpError('invalid_address', 'City must be 2-40 characters.')
    return text


def normalize_state(value: Any) -> str:
    text = str(value or '').strip().upper()
    if text not in US_STATES:
        raise RtpError('invalid_address', 'State must be a USPS two-letter code.')
    return text


def normalize_postal(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) not in {5, 9}:
        raise RtpError('invalid_address', 'ZIP must be 5 or 9 digits.')
    return digits


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise RtpError('invalid_purpose', 'Unknown instant-payment purpose.')
    return text


def normalize_rail(value: Any, *, default: str = RAIL_FEDNOW) -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text not in RAILS:
        raise RtpError('invalid_rail', 'Rail must be fednow or rtp.')
    return text


def normalize_source(value: Any, *, default: str = 'KONOHA01') -> str:
    text = ''.join(ch for ch in str(value or default).upper() if ch.isalnum())
    if not text:
        text = default
    return (text + 'XXXXXXXX')[:8]


def compose_uetr(value: Any = None) -> str:
    """ISO 20022 UETR is a canonical UUID v4."""
    text = str(value or '').strip()
    if text:
        try:
            return str(uuid.UUID(text))
        except ValueError as exc:
            raise RtpError('invalid_uetr', 'UETR must be a UUID.') from exc
    return str(uuid.uuid4())


def compose_end_to_end_id(rail: str, cycle_date: str, sequence: int) -> str:
    """ISO 20022 EndToEndId, max 35 chars: {FN|RTP}{YYYYMMDD}{seq}."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise RtpError('invalid_end_to_end', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise RtpError('invalid_end_to_end', 'EndToEndId sequence out of range.')
    prefix = 'FN' if normalize_rail(rail) == RAIL_FEDNOW else 'RTP'
    return '%s%s%06d' % (prefix, day, seq)


def compose_message_id(rail: str, cycle_date: str, sequence: int, *, source: str = 'KONOHA01') -> str:
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise RtpError('invalid_msgid', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 9999:
        raise RtpError('invalid_msgid', 'MessageId sequence out of range.')
    return '%s%s%s%04d' % (CLR_SYS[normalize_rail(rail)], normalize_source(source), day, seq)


def compose_tx_id(rail: str, cycle_date: str, sequence: int) -> str:
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise RtpError('invalid_txid', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise RtpError('invalid_txid', 'TxId sequence out of range.')
    clearing = 'FRB' if normalize_rail(rail) == RAIL_FEDNOW else 'TCH'
    return '%s%s%06d' % (clearing, day, seq)


def compose_pacs008(
    *,
    rail: str,
    amount: Decimal,
    aba: str,
    end_to_end_id: str,
    uetr: str,
    message_id: str,
    created: str,
    debtor_name: str = 'KONOHA BANK CUSTOMER',
    creditor_name: str = '',
    purpose: str = 'other',
) -> Dict[str, Any]:
    """ISO 20022 pacs.008 instant credit-transfer field map."""
    chosen = normalize_rail(rail)
    return {
        'MsgId': message_id,
        'CreDtTm': created,
        'NbOfTxs': '1',
        'SttlmMtd': 'CLRG',
        'ClrSys': CLR_SYS[chosen],
        'LclInstrm': LCL_INSTRM[chosen],
        'ChrgBr': 'SLEV',
        'EndToEndId': end_to_end_id,
        'UETR': uetr,
        'IntrBkSttlmAmt': {'Ccy': 'USD', 'Amt': money_str(amount)},
        'CdtrAgt': {'ClrSysMmbId': aba},
        'Dbtr': {'Nm': debtor_name},
        'Cdtr': {'Nm': creditor_name},
        'Purp': purpose,
    }


def compose_pain013(
    *,
    rail: str,
    amount: Decimal,
    aba: str,
    end_to_end_id: str,
    message_id: str,
    created: str,
    expiry: str,
    creditor_name: str = 'KONOHA BANK CUSTOMER',
    debtor_name: str = '',
    purpose: str = 'other',
) -> Dict[str, Any]:
    """ISO 20022 pain.013 Request-for-Payment field map."""
    chosen = normalize_rail(rail)
    return {
        'MsgId': message_id,
        'CreDtTm': created,
        'NbOfTxs': '1',
        'LclInstrm': 'RFP',
        'ClrSys': CLR_SYS[chosen],
        'EndToEndId': end_to_end_id,
        'Amt': {'Ccy': 'USD', 'Amt': money_str(amount)},
        'CdtrAgt': {'ClrSysMmbId': aba},
        'Cdtr': {'Nm': creditor_name},
        'Dbtr': {'Nm': debtor_name},
        'XpryDt': expiry,
        'Purp': purpose,
    }


def compute_fee(amount: Decimal, rail: str, fee: Optional[Decimal] = None, *, waived: bool = False) -> Decimal:
    """Per-rail flat outbound fee; waived payments cost $0."""
    if waived:
        return Decimal('0.00')
    _ = amount
    if fee is not None:
        return fee.quantize(MONEY_QUANTUM)
    return RAIL_FEES[normalize_rail(rail)].quantize(MONEY_QUANTUM)


def rfp_expiry(created_at: float, *, ttl_seconds: int = DEFAULT_RFP_TTL) -> float:
    return float(created_at) + int(ttl_seconds)


class InstantClock:
    """FedNow / TCH RTP operate 24/7/365. No cutoff, no weekend skip."""

    def __init__(self, *, tz_offset_hours: int = -4) -> None:
        self.tz_offset_hours = int(tz_offset_hours)
        self.tz = timezone(timedelta(hours=self.tz_offset_hours))

    def local_dt(self, ts: float) -> datetime:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(self.tz)

    def value_date(self, ts: float):
        return self.local_dt(ts).date()

    def cycle_date(self, ts: float) -> str:
        return self.value_date(ts).strftime('%Y%m%d')

    def iso_created(self, ts: float) -> str:
        return self.local_dt(ts).strftime('%Y-%m-%dT%H:%M:%S')

    def snapshot(self, ts: float) -> Dict[str, Any]:
        local = self.local_dt(ts)
        value = self.value_date(ts)
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'cutoff': '24/7',
            'after_cutoff': False,
            'business_day': True,
            'value_date': value.isoformat(),
            'cycle_date': value.strftime('%Y%m%d'),
            'hours': '24/7',
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
class RtpPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    customer_request: bool = True
    allow_credit: bool = False
    max_counterparties: int = 12
    max_payments: int = 120
    max_requests: int = 120
    min_amount: Decimal = Decimal('1.00')
    max_amount: Decimal = Decimal('1000000.00')
    fednow_fee: Decimal = Decimal('1.00')
    rtp_fee: Decimal = Decimal('0.45')
    dual_control_threshold: Decimal = Decimal('10000.00')
    tz_offset_hours: int = -4
    source_id: str = 'KONOHA01'
    rfp_ttl_seconds: int = DEFAULT_RFP_TTL
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST

    @classmethod
    def from_env(cls) -> 'RtpPolicy':
        extra = _env_list('RTP_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('RTP_ENABLED', True),
            customer_manage=_env_bool('RTP_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('RTP_CUSTOMER_SEND', True),
            customer_request=_env_bool('RTP_CUSTOMER_REQUEST', True),
            allow_credit=_env_bool('RTP_ALLOW_CREDIT', False),
            max_counterparties=max(1, _env_int('RTP_MAX_COUNTERPARTIES', 12)),
            max_payments=max(1, _env_int('RTP_MAX_PAYMENTS', 120)),
            max_requests=max(1, _env_int('RTP_MAX_REQUESTS', 120)),
            min_amount=_env_money('RTP_MIN_AMOUNT', '1.00'),
            max_amount=_env_money('RTP_MAX_AMOUNT', '1000000.00'),
            fednow_fee=_env_money('RTP_FEDNOW_FEE', '1.00'),
            rtp_fee=_env_money('RTP_TCH_FEE', '0.45'),
            dual_control_threshold=_env_money('RTP_DUAL_CONTROL', '10000.00'),
            tz_offset_hours=_env_int('RTP_TZ_OFFSET', -4),
            source_id=normalize_source(os.environ.get('RTP_SOURCE', 'KONOHA01')),
            rfp_ttl_seconds=max(60, _env_int('RTP_RFP_TTL', DEFAULT_RFP_TTL)),
            watchlist=watch,
        )

    def fee_for(self, rail: str) -> Decimal:
        return self.fednow_fee if rail == RAIL_FEDNOW else self.rtp_fee


@dataclass
class RtpCounterparty:
    counterparty_id: str
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
            'counterparty_id': self.counterparty_id,
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
            'active': self.status == PARTY_ACTIVE,
            'paused': self.status == PARTY_PAUSED,
            'archived': self.status == PARTY_ARCHIVED,
        }


@dataclass
class RtpPayment:
    payment_id: str
    trace_id: str
    counterparty_id: str
    userid: str
    internal_account: str
    rail: str
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
    uetr: str
    end_to_end_id: str
    message_id: str
    tx_id: str
    value_date: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    created_at: float
    updated_at: float
    sent_at: float = 0.0
    completed_at: float = 0.0
    note: str = ''
    reason: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'payment_id': self.payment_id,
            'trace_id': self.trace_id,
            'counterparty_id': self.counterparty_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'rail': self.rail,
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
            'uetr': self.uetr,
            'end_to_end_id': self.end_to_end_id,
            'message_id': self.message_id,
            'tx_id': self.tx_id,
            'value_date': self.value_date,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'sent_at': self.sent_at,
            'completed_at': self.completed_at,
            'note': self.note,
            'reason': self.reason,
            'held': self.status == PAY_HELD,
            'pending_release': self.status == PAY_PENDING,
            'completed': self.status == PAY_COMPLETED,
            'cancelable': self.status in CANCELABLE,
            'irrevocable': self.status == PAY_COMPLETED,
        }


@dataclass
class RtpRequest:
    request_id: str
    trace_id: str
    counterparty_id: str
    userid: str
    internal_account: str
    rail: str
    amount: str
    nickname: str
    legal_name: str
    aba: str
    account_last4: str
    purpose: str
    memo: str
    status: str
    end_to_end_id: str
    message_id: str
    expires_at: float
    actor: str
    ofac_hit: int
    ofac_match: str
    created_at: float
    updated_at: float
    accepted_at: float = 0.0
    note: str = ''
    reason: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'request_id': self.request_id,
            'trace_id': self.trace_id,
            'counterparty_id': self.counterparty_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'rail': self.rail,
            'amount': self.amount,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'aba': self.aba,
            'account_last4': self.account_last4,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'end_to_end_id': self.end_to_end_id,
            'message_id': self.message_id,
            'expires_at': self.expires_at,
            'actor': self.actor,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'accepted_at': self.accepted_at,
            'note': self.note,
            'reason': self.reason,
            'open': self.status in OPEN_RFPS,
            'cancelable': self.status in OPEN_RFPS,
        }


def _clone_party(row: RtpCounterparty) -> RtpCounterparty:
    return RtpCounterparty(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_pay(row: RtpPayment) -> RtpPayment:
    return RtpPayment(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_rfp(row: RtpRequest) -> RtpRequest:
    return RtpRequest(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _party_from_row(row: Any) -> RtpCounterparty:
    return RtpCounterparty(
        counterparty_id=row['counterparty_id'],
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


def _pay_from_row(row: Any) -> RtpPayment:
    return RtpPayment(
        payment_id=row['payment_id'],
        trace_id=row['trace_id'],
        counterparty_id=row['counterparty_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        rail=row['rail'],
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
        uetr=row['uetr'] or '',
        end_to_end_id=row['end_to_end_id'] or '',
        message_id=row['message_id'] or '',
        tx_id=row['tx_id'] or '',
        value_date=row['value_date'],
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        sent_at=float(row['sent_at'] or 0),
        completed_at=float(row['completed_at'] or 0),
        note=row['note'] or '',
        reason=row['reason'] or '',
    )


def _rfp_from_row(row: Any) -> RtpRequest:
    return RtpRequest(
        request_id=row['request_id'],
        trace_id=row['trace_id'],
        counterparty_id=row['counterparty_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        rail=row['rail'],
        amount=row['amount'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        aba=row['aba'],
        account_last4=row['account_last4'],
        purpose=row['purpose'],
        memo=row['memo'] or '',
        status=row['status'],
        end_to_end_id=row['end_to_end_id'] or '',
        message_id=row['message_id'] or '',
        expires_at=float(row['expires_at']),
        actor=row['actor'],
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        accepted_at=float(row['accepted_at'] or 0),
        note=row['note'] or '',
        reason=row['reason'] or '',
    )


class MemoryRtpStore:
    def __init__(self) -> None:
        self._parties: Dict[str, RtpCounterparty] = {}
        self._pays: Dict[str, RtpPayment] = {}
        self._rfps: Dict[str, RtpRequest] = {}
        self._pay_trace: Dict[str, str] = {}
        self._rfp_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_counterparty(self, row: RtpCounterparty) -> None:
        with self._lock:
            self._parties[row.counterparty_id] = row

    def get_counterparty(self, counterparty_id: str) -> Optional[RtpCounterparty]:
        with self._lock:
            row = self._parties.get(counterparty_id)
            return _clone_party(row) if row else None

    def update_counterparty(self, row: RtpCounterparty) -> None:
        with self._lock:
            self._parties[row.counterparty_id] = row

    def list_counterparties(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[RtpCounterparty]:
        with self._lock:
            rows = [_clone_party(row) for row in self._parties.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != PARTY_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_counterparty_by_nickname(self, userid: str, nickname: str) -> Optional[RtpCounterparty]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._parties.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_PARTY:
                    return _clone_party(row)
        return None

    def find_counterparty_by_fingerprint(self, userid: str, aba: str, account_number: str) -> Optional[RtpCounterparty]:
        with self._lock:
            for row in self._parties.values():
                if (
                    row.userid == userid
                    and row.aba == aba
                    and row.account_number == account_number
                    and row.status in OPEN_PARTY
                ):
                    return _clone_party(row)
        return None

    def put_payment(self, row: RtpPayment) -> RtpPayment:
        with self._lock:
            existing_id = self._pay_trace.get(row.trace_id)
            if existing_id is not None:
                return self._pays[existing_id]
            self._pays[row.payment_id] = row
            self._pay_trace[row.trace_id] = row.payment_id
            return row

    def update_payment(self, row: RtpPayment) -> None:
        with self._lock:
            self._pays[row.payment_id] = row

    def get_payment(self, payment_id: str) -> Optional[RtpPayment]:
        with self._lock:
            row = self._pays.get(payment_id)
            return _clone_pay(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[RtpPayment]:
        with self._lock:
            payment_id = self._pay_trace.get(trace_id)
            return _clone_pay(self._pays[payment_id]) if payment_id else None

    def list_payments(self, userid: Optional[str] = None, counterparty_id: Optional[str] = None) -> List[RtpPayment]:
        with self._lock:
            rows = [_clone_pay(row) for row in self._pays.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if counterparty_id is not None:
            rows = [row for row in rows if row.counterparty_id == counterparty_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def put_request(self, row: RtpRequest) -> RtpRequest:
        with self._lock:
            existing_id = self._rfp_trace.get(row.trace_id)
            if existing_id is not None:
                return self._rfps[existing_id]
            self._rfps[row.request_id] = row
            self._rfp_trace[row.trace_id] = row.request_id
            return row

    def update_request(self, row: RtpRequest) -> None:
        with self._lock:
            self._rfps[row.request_id] = row

    def get_request(self, request_id: str) -> Optional[RtpRequest]:
        with self._lock:
            row = self._rfps.get(request_id)
            return _clone_rfp(row) if row else None

    def get_request_by_trace(self, trace_id: str) -> Optional[RtpRequest]:
        with self._lock:
            request_id = self._rfp_trace.get(trace_id)
            return _clone_rfp(self._rfps[request_id]) if request_id else None

    def list_requests(self, userid: Optional[str] = None, counterparty_id: Optional[str] = None) -> List[RtpRequest]:
        with self._lock:
            rows = [_clone_rfp(row) for row in self._rfps.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if counterparty_id is not None:
            rows = [row for row in rows if row.counterparty_id == counterparty_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def next_sequence(self, kind: str, cycle_date: str) -> int:
        with self._lock:
            if kind == 'rfp':
                used = [row.end_to_end_id for row in self._rfps.values() if row.end_to_end_id.endswith(cycle_date + row.end_to_end_id[-6:]) or cycle_date in row.end_to_end_id]
                return len(used) + 1
            used = [row.end_to_end_id for row in self._pays.values() if cycle_date in (row.end_to_end_id or '')]
        return len(used) + 1


class SqliteRtpStore:
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
                CREATE TABLE IF NOT EXISTS counterparties (
                    counterparty_id TEXT PRIMARY KEY,
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
                CREATE TABLE IF NOT EXISTS payments (
                    payment_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    counterparty_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    internal_account TEXT NOT NULL,
                    rail TEXT NOT NULL,
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
                    uetr TEXT NOT NULL DEFAULT '',
                    end_to_end_id TEXT NOT NULL DEFAULT '',
                    message_id TEXT NOT NULL DEFAULT '',
                    tx_id TEXT NOT NULL DEFAULT '',
                    value_date TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    sent_at REAL NOT NULL DEFAULT 0,
                    completed_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    counterparty_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    internal_account TEXT NOT NULL,
                    rail TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    aba TEXT NOT NULL,
                    account_last4 TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    end_to_end_id TEXT NOT NULL DEFAULT '',
                    message_id TEXT NOT NULL DEFAULT '',
                    expires_at REAL NOT NULL,
                    actor TEXT NOT NULL,
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    accepted_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_counterparty(self, row: RtpCounterparty) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO counterparties (
                    counterparty_id, userid, nickname, legal_name, aba, account_number,
                    street, city, state, postal, default_account, status, actor,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.counterparty_id, row.userid, row.nickname, row.legal_name, row.aba,
                    row.account_number, row.street, row.city, row.state, row.postal,
                    row.default_account, row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_counterparty(self, counterparty_id: str) -> Optional[RtpCounterparty]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM counterparties WHERE counterparty_id = ?', (counterparty_id,),
            ).fetchone()
        return _party_from_row(row) if row else None

    def update_counterparty(self, row: RtpCounterparty) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE counterparties SET nickname=?, legal_name=?, aba=?, account_number=?,
                    street=?, city=?, state=?, postal=?, default_account=?, status=?,
                    actor=?, updated_at=?
                WHERE counterparty_id=?
                """,
                (
                    row.nickname, row.legal_name, row.aba, row.account_number, row.street,
                    row.city, row.state, row.postal, row.default_account, row.status,
                    row.actor, row.updated_at, row.counterparty_id,
                ),
            )
            conn.commit()

    def list_counterparties(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[RtpCounterparty]:
        sql = 'SELECT * FROM counterparties'
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
        return [_party_from_row(row) for row in rows]

    def find_counterparty_by_nickname(self, userid: str, nickname: str) -> Optional[RtpCounterparty]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM counterparties
                WHERE userid = ? AND lower(nickname) = lower(?)
                  AND status IN ('active', 'paused')
                """,
                (userid, nickname),
            ).fetchone()
        return _party_from_row(row) if row else None

    def find_counterparty_by_fingerprint(self, userid: str, aba: str, account_number: str) -> Optional[RtpCounterparty]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM counterparties
                WHERE userid = ? AND aba = ? AND account_number = ?
                  AND status IN ('active', 'paused')
                """,
                (userid, aba, account_number),
            ).fetchone()
        return _party_from_row(row) if row else None

    def put_payment(self, row: RtpPayment) -> RtpPayment:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM payments WHERE trace_id = ?', (row.trace_id,)
            ).fetchone()
            if existing is not None:
                return _pay_from_row(existing)
            conn.execute(
                """
                INSERT INTO payments (
                    payment_id, trace_id, counterparty_id, userid, internal_account, rail,
                    amount, fee, fee_status, nickname, legal_name, aba, account_last4,
                    purpose, memo, status, uetr, end_to_end_id, message_id, tx_id, value_date,
                    actor, releaser, ofac_hit, ofac_match, created_at, updated_at, sent_at,
                    completed_at, note, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.payment_id, row.trace_id, row.counterparty_id, row.userid,
                    row.internal_account, row.rail, row.amount, row.fee, row.fee_status,
                    row.nickname, row.legal_name, row.aba, row.account_last4, row.purpose,
                    row.memo, row.status, row.uetr, row.end_to_end_id, row.message_id,
                    row.tx_id, row.value_date, row.actor, row.releaser, row.ofac_hit,
                    row.ofac_match, row.created_at, row.updated_at, row.sent_at,
                    row.completed_at, row.note, row.reason,
                ),
            )
            conn.commit()
            return row

    def update_payment(self, row: RtpPayment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE payments SET fee=?, fee_status=?, status=?, uetr=?, end_to_end_id=?,
                    message_id=?, tx_id=?, value_date=?, actor=?, releaser=?, ofac_hit=?,
                    ofac_match=?, updated_at=?, sent_at=?, completed_at=?, note=?, reason=?
                WHERE payment_id=?
                """,
                (
                    row.fee, row.fee_status, row.status, row.uetr, row.end_to_end_id,
                    row.message_id, row.tx_id, row.value_date, row.actor, row.releaser,
                    row.ofac_hit, row.ofac_match, row.updated_at, row.sent_at,
                    row.completed_at, row.note, row.reason, row.payment_id,
                ),
            )
            conn.commit()

    def get_payment(self, payment_id: str) -> Optional[RtpPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE payment_id = ?', (payment_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[RtpPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE trace_id = ?', (trace_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def list_payments(self, userid: Optional[str] = None, counterparty_id: Optional[str] = None) -> List[RtpPayment]:
        sql = 'SELECT * FROM payments'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if counterparty_id is not None:
            clauses.append('counterparty_id = ?')
            params.append(counterparty_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_pay_from_row(row) for row in rows]

    def put_request(self, row: RtpRequest) -> RtpRequest:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM requests WHERE trace_id = ?', (row.trace_id,)
            ).fetchone()
            if existing is not None:
                return _rfp_from_row(existing)
            conn.execute(
                """
                INSERT INTO requests (
                    request_id, trace_id, counterparty_id, userid, internal_account, rail,
                    amount, nickname, legal_name, aba, account_last4, purpose, memo, status,
                    end_to_end_id, message_id, expires_at, actor, ofac_hit, ofac_match,
                    created_at, updated_at, accepted_at, note, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.request_id, row.trace_id, row.counterparty_id, row.userid,
                    row.internal_account, row.rail, row.amount, row.nickname, row.legal_name,
                    row.aba, row.account_last4, row.purpose, row.memo, row.status,
                    row.end_to_end_id, row.message_id, row.expires_at, row.actor,
                    row.ofac_hit, row.ofac_match, row.created_at, row.updated_at,
                    row.accepted_at, row.note, row.reason,
                ),
            )
            conn.commit()
            return row

    def update_request(self, row: RtpRequest) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE requests SET status=?, end_to_end_id=?, message_id=?, expires_at=?,
                    actor=?, ofac_hit=?, ofac_match=?, updated_at=?, accepted_at=?,
                    note=?, reason=?
                WHERE request_id=?
                """,
                (
                    row.status, row.end_to_end_id, row.message_id, row.expires_at, row.actor,
                    row.ofac_hit, row.ofac_match, row.updated_at, row.accepted_at, row.note,
                    row.reason, row.request_id,
                ),
            )
            conn.commit()

    def get_request(self, request_id: str) -> Optional[RtpRequest]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM requests WHERE request_id = ?', (request_id,)).fetchone()
        return _rfp_from_row(row) if row else None

    def get_request_by_trace(self, trace_id: str) -> Optional[RtpRequest]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM requests WHERE trace_id = ?', (trace_id,)).fetchone()
        return _rfp_from_row(row) if row else None

    def list_requests(self, userid: Optional[str] = None, counterparty_id: Optional[str] = None) -> List[RtpRequest]:
        sql = 'SELECT * FROM requests'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if counterparty_id is not None:
            clauses.append('counterparty_id = ?')
            params.append(counterparty_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_rfp_from_row(row) for row in rows]

    def next_sequence(self, kind: str, cycle_date: str) -> int:
        table = 'requests' if kind == 'rfp' else 'payments'
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM %s WHERE end_to_end_id LIKE ?" % table,
                ('%' + cycle_date + '%',),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class RtpService:
    def __init__(
        self,
        policy: RtpPolicy,
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
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or InstantClock(tz_offset_hours=policy.tz_offset_hours)

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise RtpError('rtp_disabled', 'Instant payments are disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise RtpError('rtp_forbidden', 'Customers cannot manage instant counterparties.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise RtpError('rtp_forbidden', 'Customers cannot originate instant payments.')

    def _require_request(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_request:
            raise RtpError('rtp_forbidden', 'Customers cannot request instant payments.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise RtpError('rtp_forbidden', 'Staff only.')

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
            raise RtpError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise RtpError('credit_not_allowed', 'Credit accounts cannot originate instant payments.')

    def _assert_amount(self, dollars: Decimal, rail: str) -> None:
        cap = min(self.policy.max_amount, RAIL_CAPS[rail])
        if dollars < self.policy.min_amount or dollars > cap:
            raise RtpError('amount_out_of_range', 'Amount is outside the allowed range.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def add_counterparty(
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
    ) -> RtpCounterparty:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise RtpError('rtp_forbidden', 'Not allowed to add counterparties for this customer.')
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        routing = normalize_aba(aba)
        external = normalize_external_account(account_number)
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        if self.store.find_counterparty_by_nickname(owner_userid, name) is not None:
            raise RtpError('counterparty_duplicate', 'A counterparty with that nickname already exists.')
        if self.store.find_counterparty_by_fingerprint(owner_userid, routing, external) is not None:
            raise RtpError('counterparty_duplicate', 'That counterparty account is already on file.')
        open_rows = [row for row in self.store.list_counterparties(owner_userid) if row.status in OPEN_PARTY]
        if len(open_rows) >= self.policy.max_counterparties:
            raise RtpError('counterparty_limit', 'Instant counterparty limit reached.')
        now = float(self.clock())
        row = RtpCounterparty(
            counterparty_id=uuid.uuid4().hex,
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
            status=PARTY_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_counterparty(row)
        return row

    def get_counterparty(self, *, counterparty_id: str, actor: str, actor_type: str) -> RtpCounterparty:
        self._require_enabled()
        row = self.store.get_counterparty(counterparty_id)
        if row is None:
            raise RtpError('counterparty_not_found', 'Instant counterparty not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise RtpError('rtp_forbidden', 'Not allowed to view this counterparty.')
        return row

    def enforce_counterparty(
        self,
        *,
        counterparty_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> RtpCounterparty:
        """Reusable gate: destination must be an active, unarchived instant counterparty."""
        row = self.get_counterparty(counterparty_id=counterparty_id, actor=actor, actor_type=actor_type)
        if row.status == PARTY_ARCHIVED:
            raise RtpError('already_archived', 'Counterparty is archived.')
        if row.status == PARTY_PAUSED:
            raise RtpError('counterparty_paused', 'Counterparty is paused.')
        if require_active and row.status != PARTY_ACTIVE:
            raise RtpError('invalid_status', 'Counterparty is not active.')
        return row

    def set_counterparty_status(
        self,
        *,
        counterparty_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> RtpCounterparty:
        self._require_manage(actor_type)
        row = self.get_counterparty(counterparty_id=counterparty_id, actor=actor, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': PARTY_PAUSED, 'hold': PARTY_PAUSED,
            'resume': PARTY_ACTIVE, 'activate': PARTY_ACTIVE, 'unpause': PARTY_ACTIVE,
            'archive': PARTY_ARCHIVED, 'close': PARTY_ARCHIVED, 'cancel': PARTY_ARCHIVED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {PARTY_PAUSED, PARTY_ACTIVE, PARTY_ARCHIVED}:
            raise RtpError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == PARTY_ARCHIVED:
            raise RtpError('already_archived', 'Counterparty is already archived.')
        if wanted == PARTY_PAUSED:
            if row.status == PARTY_PAUSED:
                raise RtpError('already_paused', 'Counterparty is already paused.')
            if row.status != PARTY_ACTIVE:
                raise RtpError('invalid_status', 'Only an active counterparty can be paused.')
        elif wanted == PARTY_ACTIVE:
            if row.status == PARTY_ACTIVE:
                raise RtpError('already_active', 'Counterparty is already active.')
            if row.status != PARTY_PAUSED:
                raise RtpError('invalid_status', 'Only a paused counterparty can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_counterparty(row)
        return row

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        counterparty_id: Any,
        amount: Any,
        rail: Any = RAIL_FEDNOW,
        internal_account: Any = None,
        waive_fee: bool = False,
    ) -> Dict[str, Any]:
        self._require_send(actor_type)
        party = self.enforce_counterparty(
            counterparty_id=str(counterparty_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if party.userid != owner_userid:
            raise RtpError('rtp_forbidden', 'Counterparty does not belong to this customer.')
        chosen = normalize_rail(rail)
        dollars = parse_money(amount)
        self._assert_amount(dollars, chosen)
        account = normalize_account(internal_account or party.default_account)
        self._assert_internal_account(owner_userid, account)
        fee = compute_fee(
            dollars, chosen, self.policy.fee_for(chosen),
            waived=bool(waive_fee) and actor_type in EMPLOYEE_ROLES,
        )
        now = float(self.clock())
        ofac = self._screen(party.legal_name, aliases=(party.nickname,))
        return {
            'amount': money_str(dollars),
            'fee': money_str(fee),
            'total': money_str(dollars + fee),
            'rail': chosen,
            'internal_account': account,
            'counterparty': party.to_dict(),
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(dollars),
            'clock': self.calendar.snapshot(now),
            'irrevocable': True,
        }

    def _assign_ids(self, payment: RtpPayment, *, now: float) -> None:
        cycle = payment.value_date or self.calendar.cycle_date(now)
        seq = self.store.next_sequence('pay', cycle)
        payment.uetr = compose_uetr()
        payment.end_to_end_id = compose_end_to_end_id(payment.rail, cycle, seq)
        payment.message_id = compose_message_id(payment.rail, cycle, seq, source=self.policy.source_id)
        payment.tx_id = compose_tx_id(payment.rail, cycle, seq)

    def _clear_ids(self, payment: RtpPayment) -> None:
        payment.uetr = ''
        payment.end_to_end_id = ''
        payment.message_id = ''
        payment.tx_id = ''

    def _transmit(self, payment: RtpPayment, *, actor: str) -> RtpPayment:
        now = float(self.clock())
        self._assign_ids(payment, now=now)
        dollars = parse_money(payment.amount)
        fee = parse_money(payment.fee, allow_zero=True)
        remark = '%s to %s' % (payment.rail, payment.nickname)
        status = PAY_COMPLETED
        fail_note = ''
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(payment.internal_account, money_str(dollars), remark)
            except Exception as exc:
                status = PAY_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = PAY_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = PAY_FAILED
                    fail_note = str(result)[:240]
        if status == PAY_COMPLETED and fee > 0 and payment.fee_status != FEE_WAIVED and self.debit_fn is not None:
            try:
                fee_result = self.debit_fn(
                    payment.internal_account, money_str(fee),
                    '%s fee %s' % (payment.rail, payment.end_to_end_id[:12]),
                )
            except Exception:
                payment.fee_status = FEE_NSF
            else:
                kind = _classify_money_result(fee_result)
                payment.fee_status = FEE_COLLECTED if kind == 'ok' else FEE_NSF
        elif status == PAY_COMPLETED and (fee == 0 or payment.fee_status == FEE_WAIVED):
            payment.fee_status = FEE_WAIVED if payment.fee_status == FEE_WAIVED or fee == 0 else payment.fee_status
        payment.status = status
        payment.updated_at = now
        if status == PAY_COMPLETED:
            payment.sent_at = now
            payment.completed_at = now
            payment.releaser = str(actor)
        else:
            self._clear_ids(payment)
            payment.note = fail_note
        return payment

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        party: RtpCounterparty,
        account: str,
        rail: str,
        dollars: Decimal,
        fee: Decimal,
        purpose: str,
        memo: str,
        trace_id: str,
        ofac: ScreenResult,
        waive_fee: bool,
    ) -> RtpPayment:
        now = float(self.clock())
        value = self.calendar.cycle_date(now)
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        if ofac.hit:
            status = PAY_HELD
        elif self._needs_dual_control(dollars):
            status = PAY_PENDING
        else:
            status = PAY_COMPLETED
        payment = RtpPayment(
            payment_id=uuid.uuid4().hex,
            trace_id=trace_id,
            counterparty_id=party.counterparty_id,
            userid=owner_userid,
            internal_account=account,
            rail=rail,
            amount=money_str(dollars),
            fee=money_str(fee),
            fee_status=fee_status,
            nickname=party.nickname,
            legal_name=party.legal_name,
            aba=party.aba,
            account_last4=last4(party.account_number),
            purpose=purpose,
            memo=memo,
            status=status,
            uetr='',
            end_to_end_id='',
            message_id='',
            tx_id='',
            value_date=value,
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
        )
        if status == PAY_COMPLETED:
            self._transmit(payment, actor=actor)
        return payment

    def originate(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        counterparty_id: Any,
        amount: Any,
        rail: Any = RAIL_FEDNOW,
        internal_account: Any = None,
        purpose: Any = 'other',
        memo: Any = '',
        trace_id: Any = None,
        waive_fee: bool = False,
    ) -> Tuple[RtpPayment, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise RtpError('rtp_forbidden', 'Not allowed to originate instant payments for this customer.')
        party = self.enforce_counterparty(
            counterparty_id=str(counterparty_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if party.userid != owner_userid:
            raise RtpError('rtp_forbidden', 'Counterparty does not belong to this customer.')
        chosen = normalize_rail(rail)
        dollars = parse_money(amount)
        self._assert_amount(dollars, chosen)
        account = normalize_account(internal_account or party.default_account)
        self._assert_internal_account(owner_userid, account)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(dollars, chosen, self.policy.fee_for(chosen), waived=staff_waive)
        trace = normalize_id(trace_id)
        existing = self.store.get_payment_by_trace(trace)
        if existing is not None:
            return existing, False
        if len(self.store.list_payments(owner_userid)) >= self.policy.max_payments:
            raise RtpError('payment_limit', 'Instant payment history limit reached.')
        ofac = self._screen(party.legal_name, aliases=(party.nickname,))
        payment = self._place(
            owner_userid=owner_userid,
            actor=actor,
            party=party,
            account=account,
            rail=chosen,
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
        if stored.status == PAY_NSF:
            raise RtpError('nsf', 'Insufficient funds for instant payment.', payment=stored)
        if stored.status == PAY_FAILED:
            raise RtpError('failed', 'Instant debit did not complete.', payment=stored)
        return stored, True

    def get_payment(self, *, payment_id: str, actor: str, actor_type: str) -> RtpPayment:
        self._require_enabled()
        row = self.store.get_payment(payment_id)
        if row is None:
            raise RtpError('payment_not_found', 'Instant payment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise RtpError('rtp_forbidden', 'Not allowed to view this payment.')
        return row

    def cancel_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> RtpPayment:
        self._require_send(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and payment.userid != actor:
            raise RtpError('rtp_forbidden', 'Not allowed to cancel this payment.')
        if payment.status == PAY_COMPLETED:
            raise RtpError('scheme_irrevocable', 'Completed instant payments cannot be cancelled.')
        if payment.status not in CANCELABLE:
            raise RtpError('not_cancelable', 'Only held or pending instant payments can be cancelled.')
        payment.status = PAY_CANCELLED
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        payment.note = normalize_note(note)
        self.store.update_payment(payment)
        return payment

    def waive_fee(self, *, payment_id: str, actor: str, actor_type: str) -> RtpPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status not in CANCELABLE:
            raise RtpError('invalid_status', 'Fee can only be waived before the payment is sent.')
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
    ) -> RtpPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status != PAY_HELD:
            raise RtpError('invalid_status', 'Only an OFAC hold can be overridden.')
        now = float(self.clock())
        payment.ofac_hit = 0
        payment.note = normalize_note(note) or 'ofac override'
        payment.actor = str(actor)
        payment.updated_at = now
        dollars = parse_money(payment.amount)
        if self._needs_dual_control(dollars):
            payment.status = PAY_PENDING
        else:
            self._transmit(payment, actor=actor)
        self.store.update_payment(payment)
        if payment.status == PAY_NSF:
            raise RtpError('nsf', 'Insufficient funds for instant payment.', payment=payment)
        if payment.status == PAY_FAILED:
            raise RtpError('failed', 'Instant debit did not complete.', payment=payment)
        return payment

    def release_payment(self, *, payment_id: str, actor: str, actor_type: str) -> RtpPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == PAY_HELD:
            raise RtpError('ofac_hold', 'OFAC hold must be overridden before release.')
        if payment.status != PAY_PENDING:
            raise RtpError('not_releasable', 'Only pending instant payments can be released.')
        if (
            payment.actor
            and str(actor) == str(payment.actor)
            and parse_money(payment.amount) >= self.policy.dual_control_threshold
        ):
            raise RtpError('same_approver', 'A different employee must release this payment.')
        self._transmit(payment, actor=actor)
        self.store.update_payment(payment)
        if payment.status == PAY_NSF:
            raise RtpError('nsf', 'Insufficient funds for instant payment.', payment=payment)
        if payment.status == PAY_FAILED:
            raise RtpError('failed', 'Instant debit did not complete.', payment=payment)
        return payment

    def reject_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> RtpPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status not in CANCELABLE:
            raise RtpError('not_rejectable', 'Only held or pending instant payments can be rejected.')
        payment.status = PAY_REJECTED
        payment.reason = normalize_note(reason, limit=40)
        payment.note = normalize_note(note)
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        self.store.update_payment(payment)
        return payment

    def complete_payment(self, *, payment_id: str, actor: str, actor_type: str) -> RtpPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == PAY_COMPLETED:
            raise RtpError('already_completed', 'Instant payment is already completed.')
        raise RtpError('not_completable', 'Instant payments complete on send; nothing to complete.')

    def recall_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> RtpPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        _ = note
        if payment.status == PAY_COMPLETED:
            raise RtpError('scheme_irrevocable', 'Completed instant payments cannot be recalled.')
        raise RtpError('not_recallable', 'Only completed instant payments would be recallable, and they are irrevocable.')

    def request_payment(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        counterparty_id: Any,
        amount: Any,
        rail: Any = RAIL_FEDNOW,
        internal_account: Any = None,
        purpose: Any = 'other',
        memo: Any = '',
        trace_id: Any = None,
    ) -> Tuple[RtpRequest, bool]:
        self._require_request(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise RtpError('rtp_forbidden', 'Not allowed to request payment for this customer.')
        party = self.enforce_counterparty(
            counterparty_id=str(counterparty_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if party.userid != owner_userid:
            raise RtpError('rtp_forbidden', 'Counterparty does not belong to this customer.')
        chosen = normalize_rail(rail)
        dollars = parse_money(amount)
        self._assert_amount(dollars, chosen)
        account = normalize_account(internal_account or party.default_account)
        self._assert_internal_account(owner_userid, account)
        trace = normalize_id(trace_id)
        existing = self.store.get_request_by_trace(trace)
        if existing is not None:
            return existing, False
        if len(self.store.list_requests(owner_userid)) >= self.policy.max_requests:
            raise RtpError('request_limit', 'Request-for-payment history limit reached.')
        ofac = self._screen(party.legal_name, aliases=(party.nickname,))
        now = float(self.clock())
        cycle = self.calendar.cycle_date(now)
        seq = self.store.next_sequence('rfp', cycle)
        status = RFP_HELD if ofac.hit else RFP_REQUESTED
        end_to_end = compose_end_to_end_id(chosen, cycle, seq) if status == RFP_REQUESTED else ''
        message_id = compose_message_id(chosen, cycle, seq, source=self.policy.source_id) if status == RFP_REQUESTED else ''
        row = RtpRequest(
            request_id=uuid.uuid4().hex,
            trace_id=trace,
            counterparty_id=party.counterparty_id,
            userid=owner_userid,
            internal_account=account,
            rail=chosen,
            amount=money_str(dollars),
            nickname=party.nickname,
            legal_name=party.legal_name,
            aba=party.aba,
            account_last4=last4(party.account_number),
            purpose=normalize_purpose(purpose),
            memo=normalize_note(memo, limit=140),
            status=status,
            end_to_end_id=end_to_end,
            message_id=message_id,
            expires_at=rfp_expiry(now, ttl_seconds=self.policy.rfp_ttl_seconds),
            actor=str(actor),
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
        )
        stored = self.store.put_request(row)
        if stored.request_id != row.request_id:
            return stored, False
        return stored, True

    def get_request(self, *, request_id: str, actor: str, actor_type: str) -> RtpRequest:
        self._require_enabled()
        row = self.store.get_request(request_id)
        if row is None:
            raise RtpError('request_not_found', 'Request for payment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise RtpError('rtp_forbidden', 'Not allowed to view this request.')
        return row

    def cancel_request(
        self,
        *,
        request_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> RtpRequest:
        self._require_request(actor_type)
        row = self.get_request(request_id=request_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise RtpError('rtp_forbidden', 'Not allowed to cancel this request.')
        if row.status not in OPEN_RFPS:
            raise RtpError('not_cancelable', 'Only open requests for payment can be cancelled.')
        row.status = RFP_CANCELLED
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        row.note = normalize_note(note)
        self.store.update_request(row)
        return row

    def override_rfp_ofac(
        self,
        *,
        request_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> RtpRequest:
        self._require_staff(actor_type)
        row = self.get_request(request_id=request_id, actor=actor, actor_type=actor_type)
        if row.status != RFP_HELD:
            raise RtpError('invalid_status', 'Only an OFAC-held request can be overridden.')
        now = float(self.clock())
        cycle = self.calendar.cycle_date(now)
        seq = self.store.next_sequence('rfp', cycle)
        row.ofac_hit = 0
        row.status = RFP_REQUESTED
        row.end_to_end_id = compose_end_to_end_id(row.rail, cycle, seq)
        row.message_id = compose_message_id(row.rail, cycle, seq, source=self.policy.source_id)
        row.note = normalize_note(note) or 'ofac override'
        row.actor = str(actor)
        row.updated_at = now
        self.store.update_request(row)
        return row

    def accept_request(
        self,
        *,
        request_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> RtpRequest:
        self._require_staff(actor_type)
        row = self.get_request(request_id=request_id, actor=actor, actor_type=actor_type)
        if row.status == RFP_HELD:
            raise RtpError('ofac_hold', 'OFAC hold must be overridden before the request is accepted.')
        if row.status != RFP_REQUESTED:
            raise RtpError('not_acceptable', 'Only an open request for payment can be accepted.')
        now = float(self.clock())
        if now >= row.expires_at:
            row.status = RFP_EXPIRED
            row.updated_at = now
            self.store.update_request(row)
            raise RtpError('already_expired', 'Request for payment has expired.', request=row)
        remark = normalize_note(note) or ('rfp from %s' % row.nickname)
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(row.internal_account, row.amount, remark)
            except Exception as exc:
                raise RtpError('accept_failed', 'Request credit failed.', request=row) from exc
            if _classify_money_result(result) != 'ok':
                raise RtpError('accept_failed', 'Request credit failed.', request=row)
        row.status = RFP_ACCEPTED
        row.accepted_at = now
        row.updated_at = now
        row.actor = str(actor)
        row.note = remark
        self.store.update_request(row)
        return row

    def reject_request(
        self,
        *,
        request_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> RtpRequest:
        self._require_staff(actor_type)
        row = self.get_request(request_id=request_id, actor=actor, actor_type=actor_type)
        if row.status not in OPEN_RFPS:
            raise RtpError('not_rejectable', 'Only an open request for payment can be rejected.')
        row.status = RFP_REJECTED
        row.reason = normalize_note(reason, limit=40)
        row.note = normalize_note(note)
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_request(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[RtpRequest]:
        now = float(self.clock())
        changed: List[RtpRequest] = []
        for row in self.store.list_requests(userid):
            if row.status != RFP_REQUESTED:
                continue
            if now < row.expires_at:
                continue
            row.status = RFP_EXPIRED
            row.updated_at = now
            self.store.update_request(row)
            changed.append(row)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.run_due(userid)
        counterparties = self.store.list_counterparties(userid)
        payments = self.store.list_payments(userid)
        requests = self.store.list_requests(userid)
        sent_ytd = Decimal('0.00')
        fee_ytd = Decimal('0.00')
        collected_ytd = Decimal('0.00')
        for row in payments:
            amount = parse_money(row.amount, allow_zero=True)
            if row.status == PAY_COMPLETED:
                sent_ytd += amount
                if row.fee_status == FEE_COLLECTED:
                    fee_ytd += parse_money(row.fee, allow_zero=True)
        for row in requests:
            if row.status == RFP_ACCEPTED:
                collected_ytd += parse_money(row.amount, allow_zero=True)
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'fednow_fee': money_str(self.policy.fednow_fee),
            'rtp_fee': money_str(self.policy.rtp_fee),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clock': self.calendar.snapshot(now),
            'counterparties': [row.to_dict() for row in counterparties[:40]],
            'payments': [row.to_dict() for row in payments[:40]],
            'requests': [row.to_dict() for row in requests[:40]],
            'ytd_sent': money_str(sent_ytd),
            'ytd_fees': money_str(fee_ytd),
            'ytd_collected': money_str(collected_ytd),
            'active_count': sum(1 for row in counterparties if row.status == PARTY_ACTIVE),
            'open_count': sum(1 for row in payments if row.status in OPEN_PAYMENTS),
            'open_requests': sum(1 for row in requests if row.status in OPEN_RFPS),
        }


_SERVICE: Optional[RtpService] = None


def set_service(service: Optional[RtpService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[RtpService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('RTP_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryRtpStore()
    path = os.environ.get('RTP_DB', DEFAULT_STORE_PATH)
    return SqliteRtpStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[InstantClock] = None,
) -> RtpService:
    if store is None:
        store = default_store()
    return RtpService(
        RtpPolicy.from_env(),
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
        'counterparty_duplicate': 409,
        'counterparty_limit': 409,
        'payment_limit': 409,
        'request_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_completed': 409,
        'already_expired': 409,
        'nsf': 409,
        'failed': 409,
        'accept_failed': 409,
        'rtp_forbidden': 403,
        'rtp_disabled': 403,
        'counterparty_paused': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_cancelable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_completable': 403,
        'not_recallable': 403,
        'not_acceptable': 403,
        'scheme_irrevocable': 403,
        'counterparty_not_found': 404,
        'payment_not_found': 404,
        'request_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_aba': 400,
        'invalid_address': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_rail': 400,
        'invalid_uetr': 400,
        'invalid_end_to_end': 400,
        'invalid_msgid': 400,
        'invalid_txid': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_counterparty': 400,
        'missing_payment': 400,
        'missing_request': 400,
    }.get(code, 400)


def _error_body(exc: RtpError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('payment') is not None:
        body['payment'] = exc.extra['payment'].to_dict()
    if exc.extra.get('request') is not None:
        body['request'] = exc.extra['request'].to_dict()
    if exc.extra.get('counterparty') is not None:
        body['counterparty'] = exc.extra['counterparty'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except RtpError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: RtpService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Rtp': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_add_counterparty(service: RtpService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.add_counterparty(
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
            'message': 'Instant counterparty added',
            'counterparty': row.to_dict(),
            'Rtp': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _party_status_route(service: RtpService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    counterparty_id = str(values.get('counterparty_id') or '').strip()
    if not counterparty_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_counterparty'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_counterparty_status(
            counterparty_id=counterparty_id, actor=userid, actor_type=actor_type, status=status,
        )
        return jsonify({
            'message': ok_message,
            'counterparty': row.to_dict(),
            'Rtp': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: RtpService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    counterparty_id = str(values.get('counterparty_id') or '').strip()
    if not counterparty_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_counterparty'}), 400

    def _run():
        preview = service.preview(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            counterparty_id=counterparty_id,
            amount=values.get('amount'),
            rail=values.get('rail') or RAIL_FEDNOW,
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({'preview': preview, 'Rtp': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: RtpService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    counterparty_id = str(values.get('counterparty_id') or '').strip()
    if not counterparty_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_counterparty'}), 400

    def _run():
        payment, created = service.originate(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            counterparty_id=counterparty_id,
            amount=values.get('amount'),
            rail=values.get('rail') or RAIL_FEDNOW,
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({
            'message': 'Instant payment originated' if created else 'Instant payment already posted',
            'payment': payment.to_dict(),
            'Rtp': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: RtpService):
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
            'message': 'Instant payment cancelled',
            'payment': payment.to_dict(),
            'Rtp': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_request(service: RtpService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    counterparty_id = str(values.get('counterparty_id') or '').strip()
    if not counterparty_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_counterparty'}), 400

    def _run():
        row, created = service.request_payment(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            counterparty_id=counterparty_id,
            amount=values.get('amount'),
            rail=values.get('rail') or RAIL_FEDNOW,
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            trace_id=values.get('trace_id'),
        )
        return jsonify({
            'message': 'Request for payment created' if created else 'Request already posted',
            'request': row.to_dict(),
            'Rtp': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel_request(service: RtpService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    request_id = str(values.get('request_id') or '').strip()
    if not request_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_request'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.cancel_request(
            request_id=request_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Request for payment cancelled',
            'request': row.to_dict(),
            'Rtp': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_pay_route(service: RtpService, action: str):
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
            message = 'Instant payment released'
        elif action == 'reject':
            payment = service.reject_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Instant payment rejected'
        elif action == 'complete':
            payment = service.complete_payment(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'Instant payment completed'
        elif action == 'recall':
            payment = service.recall_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Instant payment recalled'
        elif action == 'override':
            payment = service.override_ofac(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'waive':
            payment = service.waive_fee(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'Instant fee waived'
        else:
            raise RtpError('invalid_status', 'Unknown instant-payment action.')
        return jsonify({
            'message': message,
            'payment': payment.to_dict(),
            'Rtp': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_rfp_route(service: RtpService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    request_id = str(values.get('request_id') or '').strip()
    if not request_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_request'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'accept':
            row = service.accept_request(
                request_id=request_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Request for payment accepted'
        elif action == 'reject':
            row = service.reject_request(
                request_id=request_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Request for payment rejected'
        elif action == 'override':
            row = service.override_rfp_ofac(
                request_id=request_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Request OFAC hold overridden'
        else:
            raise RtpError('invalid_status', 'Unknown request action.')
        return jsonify({
            'message': message,
            'request': row.to_dict(),
            'Rtp': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: RtpService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Rtp': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_rtp_routes(app, service: RtpService) -> None:
    @app.route('/listRtps', methods=['POST', 'GET'])
    def list_rtps_route():
        return handle_list(service)

    @app.route('/listRtpCounterparties', methods=['POST', 'GET'])
    def list_rtp_counterparties_route():
        return handle_list(service)

    @app.route('/addRtpCounterparty', methods=['POST', 'GET'])
    def add_rtp_counterparty_route():
        return handle_add_counterparty(service)

    @app.route('/pauseRtpCounterparty', methods=['POST', 'GET'])
    def pause_rtp_counterparty_route():
        return _party_status_route(service, PARTY_PAUSED, 'Instant counterparty paused')

    @app.route('/resumeRtpCounterparty', methods=['POST', 'GET'])
    def resume_rtp_counterparty_route():
        return _party_status_route(service, PARTY_ACTIVE, 'Instant counterparty resumed')

    @app.route('/archiveRtpCounterparty', methods=['POST', 'GET'])
    def archive_rtp_counterparty_route():
        return _party_status_route(service, PARTY_ARCHIVED, 'Instant counterparty archived')

    @app.route('/previewRtp', methods=['POST', 'GET'])
    def preview_rtp_route():
        return handle_preview(service)

    @app.route('/sendRtp', methods=['POST', 'GET'])
    def send_rtp_route():
        return handle_send(service)

    @app.route('/cancelRtp', methods=['POST', 'GET'])
    def cancel_rtp_route():
        return handle_cancel(service)

    @app.route('/releaseRtp', methods=['POST', 'GET'])
    def release_rtp_route():
        return _staff_pay_route(service, 'release')

    @app.route('/rejectRtp', methods=['POST', 'GET'])
    def reject_rtp_route():
        return _staff_pay_route(service, 'reject')

    @app.route('/completeRtp', methods=['POST', 'GET'])
    def complete_rtp_route():
        return _staff_pay_route(service, 'complete')

    @app.route('/recallRtp', methods=['POST', 'GET'])
    def recall_rtp_route():
        return _staff_pay_route(service, 'recall')

    @app.route('/overrideRtpOfac', methods=['POST', 'GET'])
    def override_rtp_ofac_route():
        return _staff_pay_route(service, 'override')

    @app.route('/waiveRtpFee', methods=['POST', 'GET'])
    def waive_rtp_fee_route():
        return _staff_pay_route(service, 'waive')

    @app.route('/requestRtp', methods=['POST', 'GET'])
    def request_rtp_route():
        return handle_request(service)

    @app.route('/cancelRfp', methods=['POST', 'GET'])
    def cancel_rfp_route():
        return handle_cancel_request(service)

    @app.route('/acceptRfp', methods=['POST', 'GET'])
    def accept_rfp_route():
        return _staff_rfp_route(service, 'accept')

    @app.route('/rejectRfp', methods=['POST', 'GET'])
    def reject_rfp_route():
        return _staff_rfp_route(service, 'reject')

    @app.route('/overrideRfpOfac', methods=['POST', 'GET'])
    def override_rfp_ofac_route():
        return _staff_rfp_route(service, 'override')

    @app.route('/runDueRtps', methods=['POST', 'GET'])
    def run_due_rtps_route():
        return handle_run_due(service)
