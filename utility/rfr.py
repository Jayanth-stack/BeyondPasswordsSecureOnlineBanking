"""Inbound FedNow Request-for-Return (camt.056 / pacs.004 / camt.029).

Staff ingest ISO 20022 camt.056 Requests for Return of Funds about a prior
FedNow credit. Customers enroll DDA aliases; matched requests wait for an
accept (pacs.004 debit of the original amount) or a decline (camt.029 RJCR).
This returns funds — it does not originate a FedNow payment, receive a
FedNow credit, fulfill an RfP, answer a camt.026 RFI, or send SWIFT MT199.

Independent of inbound FedNow RFI (PR #165, camt.026/028 investigation),
ISO 20022 UETR investigation (pacs.002/camt.056 tracker), inbound
FedNow/RTP credits (PR #93), RfP fulfillment (PR #127), card tokenization
(PR #163), AFT origination (PR #161), outbound Fedwire (PR #73), and ACH
linking (PR #68). Existing `/fundTransfer`, `/withdrawAmount`, and
`/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- ABA routing checksum for the requesting FI (reuse wire.normalize_aba)
- FedNow end-to-end ID uniqueness (not IMAD / ARN / RRN / TKN / VAU / RFI)
- RFR reference identifiers (RFR + YYYYMMDD + seq)
- ISO 20022 camt.056 / pacs.004 / camt.029 (camt.026, pacs.008, pain.013 rejected)
- Return reason codes (DUPL / TECH / FRAD / CUST / AC03 / AM09 / FOCR)
- Decline codes (LEGL / AC04 / AM04 / CUST / NOAS / ARDT)
- 24/7 FedNow clock (not Fedwire 17:00) with a 10-day response window
- RFR1 pipe compose/parse (XML/DOCTYPE rejected)
- Dual-control for FRAD and high-value returns

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Full account numbers, fingerprints, E2E IDs, RFR refs, and requestor ABAs
never appear in to_dict / snapshots.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    AccountError,
    AmountError,
    DEFAULT_WATCHLIST,
    ScreenResult,
    WireError,
    account_types_from_customer_payload,
    last4,
    money_str,
    normalize_aba,
    normalize_account,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
KEY_ACTIVE = 'active'
KEY_PAUSED = 'paused'
KEY_CANCELLED = 'cancelled'
KEY_STATUSES = frozenset({KEY_ACTIVE, KEY_PAUSED, KEY_CANCELLED})
OPEN_KEYS = frozenset({KEY_ACTIVE, KEY_PAUSED})
CORR_ACTIVE = 'active'
CORR_PAUSED = 'paused'
CORR_ARCHIVED = 'archived'
OPEN_CORRS = frozenset({CORR_ACTIVE, CORR_PAUSED})
EVT_HELD = 'held'
EVT_OPEN = 'open'
EVT_PENDING = 'pending_release'
EVT_POSTED = 'posted'
EVT_REJECTED = 'rejected'
EVT_CANCELLED = 'cancelled'
EVT_REVERSED = 'reversed'
EVT_UNMATCHED = 'unmatched'
EVT_NSF = 'nsf'
EVT_FAILED = 'failed'
EVT_DECLINED = 'declined'
EVT_EXPIRED = 'expired'
EVT_STATUSES = frozenset({
    EVT_HELD, EVT_OPEN, EVT_PENDING, EVT_POSTED, EVT_REJECTED, EVT_CANCELLED,
    EVT_REVERSED, EVT_UNMATCHED, EVT_NSF, EVT_FAILED, EVT_EXPIRED, EVT_DECLINED,
})
OPEN_EVENTS = frozenset({EVT_HELD, EVT_OPEN, EVT_PENDING, EVT_UNMATCHED})
CANCELABLE = frozenset({EVT_HELD, EVT_OPEN, EVT_PENDING})
ACCEPTABLE = frozenset({EVT_OPEN})
DECLINABLE = frozenset({EVT_OPEN})
FEE_NONE = 'none'
FEE_COLLECTED = 'collected'
FEE_WAIVED = 'waived'
FEE_NSF = 'nsf'
MSG_056 = 'camt.056'
MSG_004 = 'pacs.004'
MSG_029 = 'camt.029'
MESSAGES = frozenset({MSG_056, MSG_004, MSG_029})
REJECTED_MESSAGES = frozenset({
    'pacs.008', 'pacs.002', 'pain.013', 'pain.014', 'camt.026', 'camt.028',
    'camt.087', 'mt199', 'mt103',
})
REASON_DUPL = 'DUPL'
REASON_TECH = 'TECH'
REASON_FRAD = 'FRAD'
REASON_CUST = 'CUST'
REASON_AC03 = 'AC03'
REASON_AM09 = 'AM09'
REASON_FOCR = 'FOCR'
REASONS = frozenset({
    REASON_DUPL, REASON_TECH, REASON_FRAD, REASON_CUST,
    REASON_AC03, REASON_AM09, REASON_FOCR,
})
SENSITIVE_REASONS = frozenset({REASON_FRAD})
REASON_ALIASES = {
    'duplicate': REASON_DUPL, 'dupl': REASON_DUPL,
    'technical': REASON_TECH, 'tech': REASON_TECH,
    'fraud': REASON_FRAD, 'frad': REASON_FRAD, 'unauthorized': REASON_FRAD,
    'customer': REASON_CUST, 'cust': REASON_CUST,
    'invalid_account': REASON_AC03, 'ac03': REASON_AC03, 'wrong_account': REASON_AC03,
    'wrong_amount': REASON_AM09, 'am09': REASON_AM09, 'incorrect_amount': REASON_AM09,
    'following_cancellation': REASON_FOCR, 'focr': REASON_FOCR, 'recall': REASON_FOCR,
}
DECLINE_LEGL = 'LEGL'
DECLINE_AC04 = 'AC04'
DECLINE_AM04 = 'AM04'
DECLINE_CUST = 'CUST'
DECLINE_NOAS = 'NOAS'
DECLINE_ARDT = 'ARDT'
DECLINE_CODES = frozenset({
    DECLINE_LEGL, DECLINE_AC04, DECLINE_AM04, DECLINE_CUST, DECLINE_NOAS, DECLINE_ARDT,
})
DECLINE_ALIASES = {
    'legal': DECLINE_LEGL, 'legl': DECLINE_LEGL,
    'closed': DECLINE_AC04, 'ac04': DECLINE_AC04, 'account_closed': DECLINE_AC04,
    'nsf': DECLINE_AM04, 'am04': DECLINE_AM04, 'insufficient': DECLINE_AM04,
    'customer': DECLINE_CUST, 'cust': DECLINE_CUST,
    'no_answer': DECLINE_NOAS, 'noas': DECLINE_NOAS,
    'already_returned': DECLINE_ARDT, 'ardt': DECLINE_ARDT,
}
RESOLUTION_ACCR = 'ACCR'
RESOLUTION_RJCR = 'RJCR'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/rfr.sqlite'
CUSTOMER_REVERSE_SECONDS = 24 * 3600
STAFF_REVERSE_SECONDS = 90 * 24 * 3600
UNMATCHED_TTL_SECONDS = 24 * 3600
OPEN_TTL_SECONDS = 10 * 24 * 3600
RESPONSE_LIMIT = 500


class RfrError(ValueError):
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
        raise RfrError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise RfrError('invalid_name', 'Legal name must be 2-80 characters.')
    return text


def normalize_requestor_aba(value: Any) -> str:
    """Requesting FI ABA. Reuses the wire checksum; never a PAN Luhn or IMAD."""
    try:
        return normalize_aba(value)
    except WireError as exc:
        raise RfrError('invalid_aba', str(exc.message) if hasattr(exc, 'message') else str(exc)) from exc


def mask_aba(aba: str) -> str:
    digits = ''.join(ch for ch in str(aba or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return '****' + digits[-4:]


def account_fingerprint(account: str) -> str:
    return hashlib.sha256(str(account).encode('utf-8')).hexdigest()


def mask_account(account: str) -> str:
    digits = ''.join(ch for ch in str(account or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return '****' + digits[-4:]


def normalize_e2e(value: Any) -> str:
    """FedNow end-to-end ID. Not an IMAD, ARN, RRN, TKN, VAU, or RFI/RFR reference."""
    text = ''.join(ch for ch in str(value or '').strip().upper() if ch.isalnum() or ch in '-_')
    if not (8 <= len(text) <= 35):
        raise RfrError('invalid_e2e', 'End-to-end ID must be 8-35 characters.')
    if text.startswith('TKN') or text.startswith('VAU') or text.startswith('RFI') or text.startswith('RFR'):
        raise RfrError('invalid_e2e', 'End-to-end ID must not be a TKN, VAU, RFI, or RFR reference.')
    if len(text) == 22 and text[:8].isdigit() and text[8:16].isalnum() and text[16:].isdigit():
        raise RfrError('invalid_e2e', 'End-to-end ID must not be a Fedwire IMAD.')
    return text


def mask_e2e(e2e: str) -> str:
    text = str(e2e or '')
    if len(text) < 4:
        return 'E2E****'
    return 'E2E****' + text[-4:]


def compose_rfr_ref(cycle_date: str, sequence: int) -> str:
    """RFR + YYYYMMDD + 6-digit sequence. Not IMAD / ARN / RRN / TKN / VAU."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise RfrError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise RfrError('invalid_reference', 'RFR sequence out of range.')
    return 'RFR%s%06d' % (day, seq)


def normalize_rfr_ref(value: Any) -> str:
    text = str(value or '').strip().upper()
    if not (text.startswith('RFR') and len(text) == 17 and text[3:].isdigit()):
        raise RfrError('invalid_reference', 'RFR reference must be RFRYYYYMMDD######.')
    return text


def mask_rfr_ref(ref: str) -> str:
    text = str(ref or '')
    if len(text) < 4:
        return 'RFR****'
    return 'RFR****' + text[-4:]


def normalize_message_type(value: Any, *, default: str = MSG_056) -> str:
    text = str(value or default).strip().lower().replace('_', '.')
    aliases = {
        'camt.056': MSG_056, 'camt056': MSG_056, 'rfr': MSG_056,
        'request': MSG_056, 'requestforreturn': MSG_056, 'returnrequest': MSG_056,
        'pacs.004': MSG_004, 'pacs004': MSG_004, 'return': MSG_004, 'paymentreturn': MSG_004,
        'camt.029': MSG_029, 'camt029': MSG_029, 'resolution': MSG_029, 'decline': MSG_029,
    }
    compact = text.replace('.', '').replace('-', '').replace(' ', '')
    if text in REJECTED_MESSAGES or compact in {
        'pacs008', 'pacs002', 'pain013', 'pain014', 'camt026', 'camt028',
        'camt087', 'mt199', 'mt103',
    }:
        raise RfrError('invalid_message', 'Message must be camt.056, pacs.004, or camt.029.')
    mapped = aliases.get(text) or aliases.get(compact)
    if mapped not in MESSAGES:
        raise RfrError('invalid_message', 'Message must be camt.056, pacs.004, or camt.029.')
    return mapped


def normalize_reason(value: Any, *, default: str = REASON_DUPL) -> str:
    text = str(value or default).strip().upper().replace('-', '_').replace(' ', '_')
    if text in REASONS:
        return text
    mapped = REASON_ALIASES.get(text.lower()) or REASON_ALIASES.get(text)
    if mapped in REASONS:
        return mapped
    raise RfrError('invalid_reason', 'Unknown FedNow return reason code.')


def normalize_decline(value: Any, *, default: str = DECLINE_LEGL) -> str:
    text = str(value or default).strip().upper().replace('-', '_').replace(' ', '_')
    if text in DECLINE_CODES:
        return text
    mapped = DECLINE_ALIASES.get(text.lower()) or DECLINE_ALIASES.get(text)
    if mapped in DECLINE_CODES:
        return mapped
    raise RfrError('invalid_response', 'Unknown camt.029 decline code.')


def compute_fee(fee: Decimal, *, waived: bool = False) -> Decimal:
    if waived:
        return Decimal('0.00')
    return fee.quantize(MONEY_QUANTUM)


def compose_rfr1(
    *,
    message_type: str,
    e2e: str,
    reason: str,
    aba: str,
    account: str,
    amount: str = '0.00',
    name: str = '',
    reference: str = '',
    status: str = EVT_OPEN,
) -> str:
    return 'RFR1|%s|%s|%s|%s|%s|%s|%s|%s|%s' % (
        message_type, e2e, reason, aba, account, amount,
        str(name or '').replace('|', ' '), reference, status,
    )


def parse_rfr1(line: str) -> Dict[str, str]:
    text = str(line or '').strip()
    lowered = text.lower()
    if '<?xml' in lowered or '<!doctype' in lowered or '<xml' in lowered:
        raise RfrError('invalid_file', 'XML/DOCTYPE is not a valid RFR1 record.')
    if not text:
        raise RfrError('invalid_file', 'Empty RFR1 record.')
    if not text.startswith('RFR1|'):
        raise RfrError('invalid_file', 'Record must start with RFR1|.')
    parts = text.split('|')
    if len(parts) < 10:
        raise RfrError('invalid_file', 'RFR1 record is missing fields.')
    account = parts[5]
    if '*' in account:
        raise RfrError('invalid_account', 'Ingest requires a full account, not a mask.')
    return {
        'message_type': parts[1],
        'e2e': parts[2],
        'reason': parts[3],
        'aba': parts[4],
        'account': account,
        'amount': parts[6],
        'name': parts[7],
        'reference': parts[8],
        'status': parts[9],
    }


def split_rfr1_file(payload: Any) -> List[str]:
    text = str(payload or '')
    lowered = text.lower()
    if '<?xml' in lowered or '<!doctype' in lowered or '<xml' in lowered:
        raise RfrError('invalid_file', 'XML/DOCTYPE files are rejected.')
    lines = []
    for raw in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        item = raw.strip()
        if item:
            lines.append(item)
    if not lines:
        raise RfrError('invalid_file', 'RFR1 file is empty.')
    return lines


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


class FedNowClock:
    """FedNow RFR is 24/7/365. Not the Fedwire 17:00 ET cutoff."""

    def __init__(self, *, tz_offset_hours: int = -4) -> None:
        self.tz_offset_hours = int(tz_offset_hours)
        self.tz = timezone(timedelta(hours=self.tz_offset_hours))

    def local_dt(self, ts: float) -> datetime:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(self.tz)

    def is_open(self, ts: float, network: str = '') -> bool:
        return True

    def cycle_date(self, ts: float, network: str = '') -> str:
        return self.local_dt(ts).date().strftime('%Y%m%d')

    def snapshot(self, ts: float) -> Dict[str, Any]:
        local = self.local_dt(ts)
        cycle = self.cycle_date(ts)
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'cutoff': 'none',
            'after_cutoff': False,
            'always_open': True,
            'fednow_open': True,
            'weekday': local.strftime('%A'),
            'value_date': '%s-%s-%s' % (cycle[:4], cycle[4:6], cycle[6:]),
            'cycle_date': cycle,
        }


@dataclass
class RfrPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_respond: bool = True
    allow_credit: bool = False
    max_keys: int = 12
    max_inquiries: int = 120
    max_correspondents: int = 40
    dual_control_threshold: Decimal = Decimal('2500.00')
    tz_offset_hours: int = -4
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST

    @classmethod
    def from_env(cls) -> 'RfrPolicy':
        extra = _env_list('RFR_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('RFR_ENABLED', True),
            customer_manage=_env_bool('RFR_CUSTOMER_MANAGE', True),
            customer_respond=_env_bool('RFR_CUSTOMER_RESPOND', True),
            allow_credit=_env_bool('RFR_ALLOW_CREDIT', False),
            max_keys=max(1, _env_int('RFR_MAX_KEYS', 12)),
            max_inquiries=max(1, _env_int('RFR_MAX_INQUIRIES', 120)),
            max_correspondents=max(1, _env_int('RFR_MAX_CORRESPONDENTS', 40)),
            dual_control_threshold=_env_money('RFR_DUAL_CONTROL', '2500.00'),
            tz_offset_hours=_env_int('RFR_TZ_OFFSET', -4),
            watchlist=watch,
        )


@dataclass
class RfrKey:
    key_id: str
    userid: str
    nickname: str
    legal_name: str
    default_account: str
    fingerprint: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'key_id': self.key_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'account_masked': mask_account(self.default_account),
            'last4': last4(self.default_account),
            'default_account': self.default_account,
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == KEY_ACTIVE,
            'paused': self.status == KEY_PAUSED,
            'cancelled': self.status == KEY_CANCELLED,
        }


@dataclass
class RfrCorrespondent:
    correspondent_id: str
    nickname: str
    legal_name: str
    aba: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'correspondent_id': self.correspondent_id,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'aba_masked': mask_aba(self.aba),
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == CORR_ACTIVE,
            'paused': self.status == CORR_PAUSED,
            'archived': self.status == CORR_ARCHIVED,
        }


@dataclass
class RfrInquiry:
    inquiry_id: str
    e2e_id: str
    key_id: str
    correspondent_id: str
    userid: str
    internal_account: str
    nickname: str
    legal_name: str
    account: str
    fingerprint: str
    requestor_aba: str
    requestor_name: str
    message_type: str
    reason: str
    amount: str
    status: str
    rfr_ref: str
    response_text: str
    fee: str
    fee_status: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    created_at: float
    updated_at: float
    posted_at: float = 0.0
    reversed_at: float = 0.0
    note: str = ''
    value_date: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'inquiry_id': self.inquiry_id,
            'e2e_masked': mask_e2e(self.e2e_id),
            'key_id': self.key_id,
            'correspondent_id': self.correspondent_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'account_masked': mask_account(self.account),
            'last4': last4(self.account),
            'aba_masked': mask_aba(self.requestor_aba),
            'requestor_name': self.requestor_name,
            'message_type': self.message_type,
            'reason': self.reason,
            'amount': self.amount,
            'status': self.status,
            'rfr_ref_masked': mask_rfr_ref(self.rfr_ref) if self.rfr_ref else '',
            'response_text': self.response_text,
            'fee': self.fee,
            'fee_status': self.fee_status,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'posted_at': self.posted_at,
            'reversed_at': self.reversed_at,
            'note': self.note,
            'value_date': self.value_date,
            'held': self.status == EVT_HELD,
            'open': self.status == EVT_OPEN,
            'pending_release': self.status == EVT_PENDING,
            'posted': self.status == EVT_POSTED,
            'declined': self.status == EVT_DECLINED,
            'unmatched': self.status == EVT_UNMATCHED,
            'cancelable': self.status in CANCELABLE,
            'acceptable': self.status in ACCEPTABLE,
            'declinable': self.status in DECLINABLE,
        }


def _clone_key(row: RfrKey) -> RfrKey:
    return RfrKey(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_corr(row: RfrCorrespondent) -> RfrCorrespondent:
    return RfrCorrespondent(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_inquiry(row: RfrInquiry) -> RfrInquiry:
    return RfrInquiry(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _key_from_row(row: Any) -> RfrKey:
    return RfrKey(
        key_id=row['key_id'], userid=row['userid'], nickname=row['nickname'],
        legal_name=row['legal_name'], default_account=row['default_account'],
        fingerprint=row['fingerprint'], status=row['status'], actor=row['actor'],
        created_at=float(row['created_at']), updated_at=float(row['updated_at']),
    )


def _corr_from_row(row: Any) -> RfrCorrespondent:
    return RfrCorrespondent(
        correspondent_id=row['correspondent_id'], nickname=row['nickname'],
        legal_name=row['legal_name'], aba=row['aba'], status=row['status'],
        actor=row['actor'], created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
    )


def _inquiry_from_row(row: Any) -> RfrInquiry:
    return RfrInquiry(
        inquiry_id=row['inquiry_id'], e2e_id=row['e2e_id'], key_id=row['key_id'] or '',
        correspondent_id=row['correspondent_id'] or '', userid=row['userid'] or '',
        internal_account=row['internal_account'] or '', nickname=row['nickname'] or '',
        legal_name=row['legal_name'] or '', account=row['account'],
        fingerprint=row['fingerprint'], requestor_aba=row['requestor_aba'],
        requestor_name=row['requestor_name'] or '', message_type=row['message_type'],
        reason=row['reason'], amount=row['amount'], status=row['status'],
        rfr_ref=row['rfr_ref'] or '', response_text=row['response_text'] or '',
        fee=row['fee'], fee_status=row['fee_status'], actor=row['actor'],
        releaser=row['releaser'] or '', ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '', created_at=float(row['created_at']),
        updated_at=float(row['updated_at']), posted_at=float(row['posted_at'] or 0),
        reversed_at=float(row['reversed_at'] or 0), note=row['note'] or '',
        value_date=row['value_date'] or '',
    )


class MemoryRfrStore:
    def __init__(self) -> None:
        self._keys: Dict[str, RfrKey] = {}
        self._corrs: Dict[str, RfrCorrespondent] = {}
        self._inquiries: Dict[str, RfrInquiry] = {}
        self._by_e2e: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_key(self, row: RfrKey) -> None:
        with self._lock:
            self._keys[row.key_id] = row

    def get_key(self, key_id: str) -> Optional[RfrKey]:
        with self._lock:
            row = self._keys.get(key_id)
            return _clone_key(row) if row else None

    def update_key(self, row: RfrKey) -> None:
        with self._lock:
            self._keys[row.key_id] = row

    def list_keys(self, userid: Optional[str] = None, *, include_cancelled: bool = True) -> List[RfrKey]:
        with self._lock:
            rows = [_clone_key(row) for row in self._keys.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_cancelled:
            rows = [row for row in rows if row.status != KEY_CANCELLED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_key_by_nickname(self, userid: str, nickname: str) -> Optional[RfrKey]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._keys.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_KEYS:
                    return _clone_key(row)
        return None

    def find_key_by_fingerprint(self, fingerprint: str, *, userid: Optional[str] = None) -> Optional[RfrKey]:
        with self._lock:
            for row in self._keys.values():
                if row.fingerprint == fingerprint and row.status in OPEN_KEYS:
                    if userid is None or row.userid == userid:
                        return _clone_key(row)
        return None

    def put_correspondent(self, row: RfrCorrespondent) -> None:
        with self._lock:
            self._corrs[row.correspondent_id] = row

    def get_correspondent(self, correspondent_id: str) -> Optional[RfrCorrespondent]:
        with self._lock:
            row = self._corrs.get(correspondent_id)
            return _clone_corr(row) if row else None

    def update_correspondent(self, row: RfrCorrespondent) -> None:
        with self._lock:
            self._corrs[row.correspondent_id] = row

    def list_correspondents(self, *, include_archived: bool = True) -> List[RfrCorrespondent]:
        with self._lock:
            rows = [_clone_corr(row) for row in self._corrs.values()]
        if not include_archived:
            rows = [row for row in rows if row.status != CORR_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_correspondent_by_aba(self, aba: str) -> Optional[RfrCorrespondent]:
        with self._lock:
            for row in self._corrs.values():
                if row.aba == aba and row.status in OPEN_CORRS:
                    return _clone_corr(row)
        return None

    def find_correspondent_by_nickname(self, nickname: str) -> Optional[RfrCorrespondent]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._corrs.values():
                if row.nickname.lower() == wanted and row.status in OPEN_CORRS:
                    return _clone_corr(row)
        return None

    def put_inquiry(self, row: RfrInquiry) -> RfrInquiry:
        with self._lock:
            existing_id = self._by_e2e.get(row.e2e_id)
            if existing_id is not None:
                return self._inquiries[existing_id]
            self._inquiries[row.inquiry_id] = row
            self._by_e2e[row.e2e_id] = row.inquiry_id
            return row

    def update_inquiry(self, row: RfrInquiry) -> None:
        with self._lock:
            self._inquiries[row.inquiry_id] = row

    def get_inquiry(self, inquiry_id: str) -> Optional[RfrInquiry]:
        with self._lock:
            row = self._inquiries.get(inquiry_id)
            return _clone_inquiry(row) if row else None

    def get_inquiry_by_e2e(self, e2e_id: str) -> Optional[RfrInquiry]:
        with self._lock:
            inquiry_id = self._by_e2e.get(e2e_id)
            return _clone_inquiry(self._inquiries[inquiry_id]) if inquiry_id else None

    def list_inquiries(self, userid: Optional[str] = None, key_id: Optional[str] = None) -> List[RfrInquiry]:
        with self._lock:
            rows = [_clone_inquiry(row) for row in self._inquiries.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if key_id is not None:
            rows = [row for row in rows if row.key_id == key_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def list_unmatched(self) -> List[RfrInquiry]:
        with self._lock:
            rows = [_clone_inquiry(row) for row in self._inquiries.values() if row.status == EVT_UNMATCHED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def next_rfr_sequence(self, cycle_date: str) -> int:
        prefix = 'RFI' + cycle_date
        with self._lock:
            used = [row.rfr_ref for row in self._inquiries.values() if row.rfr_ref.startswith(prefix)]
        return len(used) + 1


class SqliteRfrStore:
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
                CREATE TABLE IF NOT EXISTS keys (
                    key_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    default_account TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS correspondents (
                    correspondent_id TEXT PRIMARY KEY,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    aba TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inquiries (
                    inquiry_id TEXT PRIMARY KEY,
                    e2e_id TEXT NOT NULL UNIQUE,
                    key_id TEXT NOT NULL DEFAULT '',
                    correspondent_id TEXT NOT NULL DEFAULT '',
                    userid TEXT NOT NULL DEFAULT '',
                    internal_account TEXT NOT NULL DEFAULT '',
                    nickname TEXT NOT NULL DEFAULT '',
                    legal_name TEXT NOT NULL DEFAULT '',
                    account TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    requestor_aba TEXT NOT NULL,
                    requestor_name TEXT NOT NULL DEFAULT '',
                    message_type TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    status TEXT NOT NULL,
                    rfr_ref TEXT NOT NULL DEFAULT '',
                    response_text TEXT NOT NULL DEFAULT '',
                    fee TEXT NOT NULL,
                    fee_status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    posted_at REAL NOT NULL DEFAULT 0,
                    reversed_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    value_date TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_key(self, row: RfrKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO keys (
                    key_id, userid, nickname, legal_name, default_account, fingerprint,
                    status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.key_id, row.userid, row.nickname, row.legal_name, row.default_account,
                    row.fingerprint, row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_key(self, key_id: str) -> Optional[RfrKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM keys WHERE key_id = ?', (key_id,)).fetchone()
        return _key_from_row(row) if row else None

    def update_key(self, row: RfrKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE keys SET nickname=?, legal_name=?, default_account=?, fingerprint=?,
                    status=?, actor=?, updated_at=? WHERE key_id=?
                """,
                (
                    row.nickname, row.legal_name, row.default_account, row.fingerprint,
                    row.status, row.actor, row.updated_at, row.key_id,
                ),
            )
            conn.commit()

    def list_keys(self, userid: Optional[str] = None, *, include_cancelled: bool = True) -> List[RfrKey]:
        sql = 'SELECT * FROM keys'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if not include_cancelled:
            clauses.append("status != 'cancelled'")
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_key_from_row(row) for row in rows]

    def find_key_by_nickname(self, userid: str, nickname: str) -> Optional[RfrKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM keys
                WHERE userid = ? AND lower(nickname) = lower(?)
                  AND status IN ('active', 'paused')
                """,
                (userid, nickname),
            ).fetchone()
        return _key_from_row(row) if row else None

    def find_key_by_fingerprint(self, fingerprint: str, *, userid: Optional[str] = None) -> Optional[RfrKey]:
        sql = "SELECT * FROM keys WHERE fingerprint = ? AND status IN ('active', 'paused')"
        params: List[Any] = [fingerprint]
        if userid is not None:
            sql += ' AND userid = ?'
            params.append(userid)
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return _key_from_row(row) if row else None

    def put_correspondent(self, row: RfrCorrespondent) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO correspondents (
                    correspondent_id, nickname, legal_name, aba, status, actor,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.correspondent_id, row.nickname, row.legal_name, row.aba,
                    row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_correspondent(self, correspondent_id: str) -> Optional[RfrCorrespondent]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM correspondents WHERE correspondent_id = ?', (correspondent_id,),
            ).fetchone()
        return _corr_from_row(row) if row else None

    def update_correspondent(self, row: RfrCorrespondent) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE correspondents SET nickname=?, legal_name=?, aba=?, status=?,
                    actor=?, updated_at=? WHERE correspondent_id=?
                """,
                (
                    row.nickname, row.legal_name, row.aba, row.status, row.actor,
                    row.updated_at, row.correspondent_id,
                ),
            )
            conn.commit()

    def list_correspondents(self, *, include_archived: bool = True) -> List[RfrCorrespondent]:
        sql = 'SELECT * FROM correspondents'
        if not include_archived:
            sql += " WHERE status != 'archived'"
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [_corr_from_row(row) for row in rows]

    def find_correspondent_by_aba(self, aba: str) -> Optional[RfrCorrespondent]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM correspondents WHERE aba = ? AND status IN ('active', 'paused')",
                (aba,),
            ).fetchone()
        return _corr_from_row(row) if row else None

    def find_correspondent_by_nickname(self, nickname: str) -> Optional[RfrCorrespondent]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM correspondents
                WHERE lower(nickname) = lower(?) AND status IN ('active', 'paused')
                """,
                (nickname,),
            ).fetchone()
        return _corr_from_row(row) if row else None

    def put_inquiry(self, row: RfrInquiry) -> RfrInquiry:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM inquiries WHERE e2e_id = ?', (row.e2e_id,)
            ).fetchone()
            if existing is not None:
                return _inquiry_from_row(existing)
            conn.execute(
                """
                INSERT INTO inquiries (
                    inquiry_id, e2e_id, key_id, correspondent_id, userid, internal_account,
                    nickname, legal_name, account, fingerprint, requestor_aba, requestor_name,
                    message_type, reason, amount, status, rfr_ref, response_text, fee,
                    fee_status, actor, releaser, ofac_hit, ofac_match, created_at, updated_at,
                    posted_at, reversed_at, note, value_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.inquiry_id, row.e2e_id, row.key_id, row.correspondent_id, row.userid,
                    row.internal_account, row.nickname, row.legal_name, row.account,
                    row.fingerprint, row.requestor_aba, row.requestor_name, row.message_type,
                    row.reason, row.amount, row.status, row.rfr_ref, row.response_text,
                    row.fee, row.fee_status, row.actor, row.releaser, row.ofac_hit,
                    row.ofac_match, row.created_at, row.updated_at, row.posted_at,
                    row.reversed_at, row.note, row.value_date,
                ),
            )
            conn.commit()
            return row

    def update_inquiry(self, row: RfrInquiry) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE inquiries SET key_id=?, correspondent_id=?, userid=?, internal_account=?,
                    nickname=?, legal_name=?, account=?, fingerprint=?, requestor_aba=?,
                    requestor_name=?, message_type=?, reason=?, amount=?, status=?, rfr_ref=?,
                    response_text=?, fee=?, fee_status=?, actor=?, releaser=?, ofac_hit=?,
                    ofac_match=?, updated_at=?, posted_at=?, reversed_at=?, note=?, value_date=?
                WHERE inquiry_id=?
                """,
                (
                    row.key_id, row.correspondent_id, row.userid, row.internal_account,
                    row.nickname, row.legal_name, row.account, row.fingerprint,
                    row.requestor_aba, row.requestor_name, row.message_type, row.reason,
                    row.amount, row.status, row.rfr_ref, row.response_text, row.fee,
                    row.fee_status, row.actor, row.releaser, row.ofac_hit, row.ofac_match,
                    row.updated_at, row.posted_at, row.reversed_at, row.note, row.value_date,
                    row.inquiry_id,
                ),
            )
            conn.commit()

    def get_inquiry(self, inquiry_id: str) -> Optional[RfrInquiry]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inquiries WHERE inquiry_id = ?', (inquiry_id,)).fetchone()
        return _inquiry_from_row(row) if row else None

    def get_inquiry_by_e2e(self, e2e_id: str) -> Optional[RfrInquiry]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inquiries WHERE e2e_id = ?', (e2e_id,)).fetchone()
        return _inquiry_from_row(row) if row else None

    def list_inquiries(self, userid: Optional[str] = None, key_id: Optional[str] = None) -> List[RfrInquiry]:
        sql = 'SELECT * FROM inquiries'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if key_id is not None:
            clauses.append('key_id = ?')
            params.append(key_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_inquiry_from_row(row) for row in rows]

    def list_unmatched(self) -> List[RfrInquiry]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM inquiries WHERE status = 'unmatched' ORDER BY created_at DESC"
            ).fetchall()
        return [_inquiry_from_row(row) for row in rows]

    def next_rfr_sequence(self, cycle_date: str) -> int:
        prefix = 'RFI' + cycle_date + '%'
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT COUNT(*) AS n FROM inquiries WHERE rfr_ref LIKE ?', (prefix,),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class RfrService:
    def __init__(
        self,
        policy: RfrPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[FedNowClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or FedNowClock(tz_offset_hours=policy.tz_offset_hours)

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise RfrError('rfr_disabled', 'FedNow RFR is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise RfrError('rfr_forbidden', 'Customers cannot manage RFR keys.')

    def _require_respond(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_respond:
            raise RfrError('rfr_forbidden', 'Customers cannot accept or decline RFRs.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise RfrError('rfr_forbidden', 'Staff only.')

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
            raise RfrError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise RfrError('credit_not_allowed', 'Credit accounts cannot enroll FedNow RFR keys.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, reason: str, amount: Any = None) -> bool:
        if reason in SENSITIVE_REASONS:
            return True
        if amount is None:
            return False
        try:
            money = parse_money(amount, allow_zero=True)
        except AmountError:
            return False
        return money >= self.policy.dual_control_threshold

    def add_key(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        default_account: Any,
    ) -> RfrKey:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise RfrError('rfr_forbidden', 'Not allowed to enroll RFR keys for this customer.')
        now = float(self.clock())
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        fingerprint = account_fingerprint(account)
        if self.store.find_key_by_nickname(owner_userid, name) is not None:
            raise RfrError('key_duplicate', 'An alias with that nickname already exists.')
        if self.store.find_key_by_fingerprint(fingerprint) is not None:
            raise RfrError('key_duplicate', 'That account is already enrolled.')
        open_rows = [row for row in self.store.list_keys(owner_userid) if row.status in OPEN_KEYS]
        if len(open_rows) >= self.policy.max_keys:
            raise RfrError('key_limit', 'RFR key limit reached.')
        row = RfrKey(
            key_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            default_account=account,
            fingerprint=fingerprint,
            status=KEY_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_key(row)
        self._rematch_unmatched(row)
        return row

    def get_key(self, *, key_id: str, actor: str, actor_type: str) -> RfrKey:
        self._require_enabled()
        row = self.store.get_key(key_id)
        if row is None:
            raise RfrError('key_not_found', 'RFR key not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise RfrError('rfr_forbidden', 'Not allowed to view this key.')
        return row

    def enforce_key(
        self,
        *,
        key_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> RfrKey:
        row = self.get_key(key_id=key_id, actor=actor, actor_type=actor_type)
        if row.status == KEY_CANCELLED:
            raise RfrError('already_cancelled', 'Key is cancelled.')
        if row.status == KEY_PAUSED:
            raise RfrError('key_paused', 'Key is paused.')
        if require_active and row.status != KEY_ACTIVE:
            raise RfrError('invalid_status', 'Key is not active.')
        return row

    def set_key_status(
        self,
        *,
        key_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> RfrKey:
        self._require_manage(actor_type)
        row = self.get_key(key_id=key_id, actor=actor, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': KEY_PAUSED, 'hold': KEY_PAUSED,
            'resume': KEY_ACTIVE, 'activate': KEY_ACTIVE, 'unpause': KEY_ACTIVE,
            'cancel': KEY_CANCELLED, 'close': KEY_CANCELLED, 'archive': KEY_CANCELLED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {KEY_PAUSED, KEY_ACTIVE, KEY_CANCELLED}:
            raise RfrError('invalid_status', 'Status must be pause, resume, or cancel.')
        if row.status == KEY_CANCELLED:
            raise RfrError('already_cancelled', 'Key is already cancelled.')
        if wanted == KEY_PAUSED:
            if row.status == KEY_PAUSED:
                raise RfrError('already_paused', 'Key is already paused.')
            if row.status != KEY_ACTIVE:
                raise RfrError('invalid_status', 'Only an active key can be paused.')
        elif wanted == KEY_ACTIVE:
            if row.status == KEY_ACTIVE:
                raise RfrError('already_active', 'Key is already active.')
            if row.status != KEY_PAUSED:
                raise RfrError('invalid_status', 'Only a paused key can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_key(row)
        if wanted == KEY_ACTIVE:
            self._rematch_unmatched(row)
        return row

    def add_correspondent(
        self,
        *,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        aba: Any,
    ) -> RfrCorrespondent:
        self._require_staff(actor_type)
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        routing = normalize_requestor_aba(aba)
        if self.store.find_correspondent_by_nickname(name) is not None:
            raise RfrError('correspondent_duplicate', 'A correspondent with that nickname already exists.')
        if self.store.find_correspondent_by_aba(routing) is not None:
            raise RfrError('correspondent_duplicate', 'That ABA is already on file.')
        open_rows = [row for row in self.store.list_correspondents() if row.status in OPEN_CORRS]
        if len(open_rows) >= self.policy.max_correspondents:
            raise RfrError('correspondent_limit', 'Correspondent directory limit reached.')
        now = float(self.clock())
        row = RfrCorrespondent(
            correspondent_id=uuid.uuid4().hex,
            nickname=name,
            legal_name=legal,
            aba=routing,
            status=CORR_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_correspondent(row)
        return row

    def get_correspondent(self, *, correspondent_id: str, actor_type: str) -> RfrCorrespondent:
        self._require_staff(actor_type)
        row = self.store.get_correspondent(correspondent_id)
        if row is None:
            raise RfrError('correspondent_not_found', 'Correspondent not found.')
        return row

    def set_correspondent_status(
        self,
        *,
        correspondent_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> RfrCorrespondent:
        self._require_staff(actor_type)
        row = self.get_correspondent(correspondent_id=correspondent_id, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': CORR_PAUSED, 'resume': CORR_ACTIVE, 'activate': CORR_ACTIVE,
            'archive': CORR_ARCHIVED, 'close': CORR_ARCHIVED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {CORR_PAUSED, CORR_ACTIVE, CORR_ARCHIVED}:
            raise RfrError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == CORR_ARCHIVED:
            raise RfrError('already_archived', 'Correspondent is already archived.')
        if wanted == CORR_PAUSED and row.status == CORR_PAUSED:
            raise RfrError('already_paused', 'Correspondent is already paused.')
        if wanted == CORR_ACTIVE and row.status == CORR_ACTIVE:
            raise RfrError('already_active', 'Correspondent is already active.')
        if wanted == CORR_ACTIVE and row.status != CORR_PAUSED:
            raise RfrError('invalid_status', 'Only a paused correspondent can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_correspondent(row)
        return row

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        inquiry_id: Any = None,
        key_id: Any = None,
        reason: Any = None,
        waive_fee: bool = False,
    ) -> Dict[str, Any]:
        self._require_respond(actor_type)
        inquiry = None
        key = None
        if inquiry_id:
            inquiry = self.get_inquiry(inquiry_id=str(inquiry_id).strip(), actor=actor, actor_type=actor_type)
            if inquiry.userid and inquiry.userid != owner_userid and actor_type not in EMPLOYEE_ROLES:
                raise RfrError('rfr_forbidden', 'Inquiry does not belong to this customer.')
            if inquiry.key_id:
                key = self.store.get_key(inquiry.key_id)
        elif key_id:
            key = self.enforce_key(key_id=str(key_id).strip(), actor=actor, actor_type=actor_type)
            if key.userid != owner_userid:
                raise RfrError('rfr_forbidden', 'Key does not belong to this customer.')
        code = normalize_reason(reason or (inquiry.reason if inquiry else REASON_DUPL))
        now = float(self.clock())
        screen_target = inquiry.requestor_name if inquiry else (key.legal_name if key else '')
        ofac = self._screen(screen_target, aliases=(inquiry.nickname,) if inquiry else ())
        amount = inquiry.amount if inquiry else '0.00'
        return {
            'reason': code,
            'amount': amount,
            'dual_control': self._needs_dual_control(code, amount),
            'ofac': ofac.to_dict(),
            'key': key.to_dict() if key else None,
            'inquiry': inquiry.to_dict() if inquiry else None,
            'clock': self.calendar.snapshot(now),
            'network_open': self.calendar.is_open(now),
            'message_type': MSG_004,
            'resolution': RESOLUTION_ACCR,
        }

    def get_inquiry(self, *, inquiry_id: str, actor: str, actor_type: str) -> RfrInquiry:
        self._require_enabled()
        row = self.store.get_inquiry(inquiry_id)
        if row is None:
            raise RfrError('inquiry_not_found', 'RFR request not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid and row.userid != actor:
            raise RfrError('rfr_forbidden', 'Not allowed to view this inquiry.')
        return row

    def _evaluate_status(self, *, key: Optional[RfrKey], ofac: ScreenResult) -> str:
        if key is None:
            return EVT_UNMATCHED
        if ofac.hit:
            return EVT_HELD
        return EVT_OPEN

    def _attach_key(self, inquiry: RfrInquiry, key: RfrKey) -> None:
        inquiry.key_id = key.key_id
        inquiry.userid = key.userid
        inquiry.internal_account = key.default_account
        inquiry.nickname = key.nickname
        inquiry.legal_name = key.legal_name
        inquiry.account = key.default_account
        inquiry.fingerprint = key.fingerprint

    def ingest_inquiry(
        self,
        *,
        actor: str,
        actor_type: str,
        e2e: Any,
        account: Any,
        aba: Any,
        reason: Any = REASON_DUPL,
        amount: Any = None,
        requestor_name: Any = '',
        correspondent_id: Any = None,
        message_type: Any = MSG_056,
        waive_fee: bool = False,
        note: Any = '',
    ) -> Tuple[RfrInquiry, bool]:
        self._require_staff(actor_type)
        e2e_id = normalize_e2e(e2e)
        existing = self.store.get_inquiry_by_e2e(e2e_id)
        if existing is not None:
            return existing, False
        msg = normalize_message_type(message_type)
        if msg != MSG_056:
            raise RfrError('invalid_message', 'Inbound ingest must be camt.056.')
        dest = normalize_account(account)
        fingerprint = account_fingerprint(dest)
        routing = normalize_requestor_aba(aba)
        code = normalize_reason(reason)
        money = parse_money(amount)
        corr = None
        if correspondent_id:
            corr = self.get_correspondent(correspondent_id=str(correspondent_id).strip(), actor_type=actor_type)
        else:
            corr = self.store.find_correspondent_by_aba(routing)
        if corr is not None and corr.status == CORR_PAUSED:
            raise RfrError('correspondent_paused', 'Correspondent is paused.')
        if corr is not None:
            routing = corr.aba
        requestor = normalize_legal_name(requestor_name or (corr.legal_name if corr else 'Requesting FI'))
        key = self.store.find_key_by_fingerprint(fingerprint)
        if key is not None and key.status == KEY_PAUSED:
            key = None
        ofac = self._screen(requestor, aliases=(corr.nickname,) if corr else ())
        now = float(self.clock())
        open_count = len(self.store.list_inquiries())
        if open_count >= self.policy.max_inquiries:
            raise RfrError('inquiry_limit', 'RFR request limit reached.')
        status = self._evaluate_status(key=key, ofac=ofac)
        inquiry = RfrInquiry(
            inquiry_id=uuid.uuid4().hex,
            e2e_id=e2e_id,
            key_id=key.key_id if key else '',
            correspondent_id=corr.correspondent_id if corr else '',
            userid=key.userid if key else '',
            internal_account=key.default_account if key else '',
            nickname=key.nickname if key else '',
            legal_name=key.legal_name if key else '',
            account=dest,
            fingerprint=fingerprint,
            requestor_aba=routing,
            requestor_name=requestor,
            message_type=MSG_056,
            reason=code,
            amount=money_str(money),
            status=status,
            rfr_ref='',
            response_text='',
            fee=money_str(Decimal('0.00')),
            fee_status=FEE_WAIVED if waive_fee else FEE_NONE,
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
            note=normalize_note(note),
            value_date=self.calendar.cycle_date(now),
        )
        stored = self.store.put_inquiry(inquiry)
        return stored, stored.inquiry_id == inquiry.inquiry_id

    def ingest_file(self, *, actor: str, actor_type: str, payload: Any) -> List[RfrInquiry]:
        self._require_staff(actor_type)
        rows = []
        for line in split_rfr1_file(payload):
            parsed = parse_rfr1(line)
            row, _created = self.ingest_inquiry(
                actor=actor,
                actor_type=actor_type,
                e2e=parsed['e2e'],
                account=parsed['account'],
                aba=parsed['aba'],
                reason=parsed['reason'],
                amount=parsed['amount'],
                requestor_name=parsed['name'],
                message_type=parsed['message_type'] or MSG_056,
            )
            rows.append(row)
        return rows

    def _rematch_unmatched(self, key: RfrKey) -> None:
        if key.status != KEY_ACTIVE:
            return
        for row in self.store.list_unmatched():
            if row.fingerprint != key.fingerprint:
                continue
            ofac = ScreenResult(bool(row.ofac_hit), row.ofac_match, 90 if row.ofac_hit else 0)
            self._attach_key(row, key)
            row.status = self._evaluate_status(key=key, ofac=ofac)
            row.updated_at = float(self.clock())
            self.store.update_inquiry(row)

    def claim(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        inquiry_id: Any = None,
        account: Any = None,
        last4_digits: Any = None,
        default_account: Any = None,
        nickname: Any = None,
        legal_name: Any = None,
    ) -> RfrInquiry:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise RfrError('rfr_forbidden', 'Not allowed to claim for this customer.')
        dest = normalize_account(default_account or account) if (default_account or account) else ''
        if dest:
            self._assert_internal_account(owner_userid, dest)
        target = None
        if inquiry_id:
            target = self.store.get_inquiry(str(inquiry_id).strip())
            if target is None:
                raise RfrError('inquiry_not_found', 'RFR request not found.')
            if target.status != EVT_UNMATCHED:
                raise RfrError('not_claimable', 'Inquiry is not unmatched.')
        else:
            needle = dest or ''
            last = ''.join(ch for ch in str(last4_digits or '') if ch.isdigit())
            name = ' '.join(str(legal_name or '').strip().split()).lower()
            for row in self.store.list_unmatched():
                if needle and row.account == needle:
                    target = row
                    break
                if last and last4(row.account) == last[-4:] and (
                    not name or name == row.requestor_name.lower() or True
                ):
                    if last and last4(row.account) == last[-4:]:
                        target = row
                        break
            if target is None:
                raise RfrError('inquiry_not_found', 'No unmatched RFI matches that account.')
        if dest and not inquiry_id and target.account != dest and last4(target.account) != last4(dest):
            raise RfrError('not_claimable', 'Account does not match this inquiry.')
        if not dest:
            dest = normalize_account(default_account or target.account)
            self._assert_internal_account(owner_userid, dest)
        key = self.store.find_key_by_fingerprint(account_fingerprint(dest), userid=owner_userid)
        if key is None:
            key = self.add_key(
                owner_userid=owner_userid,
                actor=actor,
                actor_type=actor_type,
                nickname=nickname or ('FedNow ' + last4(dest)),
                legal_name=legal_name or owner_userid,
                default_account=dest,
            )
        if key.status != KEY_ACTIVE:
            raise RfrError('key_paused', 'Key is paused.')
        ofac = ScreenResult(bool(target.ofac_hit), target.ofac_match, 90 if target.ofac_hit else 0)
        if not target.ofac_hit:
            ofac = self._screen(target.requestor_name)
        self._attach_key(target, key)
        target.ofac_hit = 1 if ofac.hit else 0
        target.ofac_match = ofac.matched
        target.status = self._evaluate_status(key=key, ofac=ofac)
        target.updated_at = float(self.clock())
        self.store.update_inquiry(target)
        return target

    def assign(
        self,
        *,
        inquiry_id: str,
        actor: str,
        actor_type: str,
        owner_userid: str,
        key_id: Any = None,
        nickname: Any = None,
        legal_name: Any = None,
        default_account: Any = None,
    ) -> RfrInquiry:
        self._require_staff(actor_type)
        row = self.store.get_inquiry(inquiry_id)
        if row is None:
            raise RfrError('inquiry_not_found', 'RFR request not found.')
        if row.status != EVT_UNMATCHED:
            raise RfrError('not_assignable', 'Inquiry is not unmatched.')
        key = None
        if key_id:
            key = self.get_key(key_id=str(key_id).strip(), actor=actor, actor_type=actor_type)
            if key.userid != owner_userid:
                raise RfrError('rfr_forbidden', 'Key does not belong to this customer.')
        dest = normalize_account(default_account or row.account)
        if key is None:
            key = self.store.find_key_by_fingerprint(account_fingerprint(dest), userid=owner_userid)
        if key is None:
            key = self.add_key(
                owner_userid=owner_userid,
                actor=actor,
                actor_type=actor_type,
                nickname=nickname or ('FedNow ' + last4(dest)),
                legal_name=legal_name or owner_userid,
                default_account=dest,
            )
        return self.claim(
            owner_userid=owner_userid,
            actor=actor,
            actor_type=actor_type,
            inquiry_id=row.inquiry_id,
            default_account=key.default_account,
            nickname=key.nickname,
            legal_name=key.legal_name,
        )

    def accept(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        inquiry_id: Any,
        note: Any = '',
    ) -> Tuple[RfrInquiry, bool]:
        self._require_respond(actor_type)
        row = self.get_inquiry(inquiry_id=str(inquiry_id or '').strip(), actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != owner_userid:
            raise RfrError('rfr_forbidden', 'Request does not belong to this customer.')
        if row.status == EVT_POSTED and row.response_text == RESOLUTION_ACCR:
            return row, False
        if row.status != EVT_OPEN:
            if row.status == EVT_HELD:
                raise RfrError('ofac_hold', 'OFAC hold must be overridden before returning funds.')
            if row.status == EVT_PENDING:
                raise RfrError('not_respondable', 'Return is awaiting release.')
            raise RfrError('not_respondable', 'Request is not open for a return.')
        if not row.key_id:
            raise RfrError('not_respondable', 'Request is not matched to a key.')
        self.enforce_key(key_id=row.key_id, actor=actor, actor_type=actor_type)
        now = float(self.clock())
        row.response_text = RESOLUTION_ACCR
        row.note = normalize_note(note) or row.note
        row.fee = row.amount
        row.actor = str(actor)
        row.updated_at = now
        if self._needs_dual_control(row.reason, row.amount):
            row.status = EVT_PENDING
            self.store.update_inquiry(row)
            return row, True
        self._transmit(row, actor=actor)
        return row, True

    def decline(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        inquiry_id: Any,
        decline_code: Any = DECLINE_LEGL,
        note: Any = '',
    ) -> Tuple[RfrInquiry, bool]:
        self._require_respond(actor_type)
        row = self.get_inquiry(inquiry_id=str(inquiry_id or '').strip(), actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != owner_userid:
            raise RfrError('rfr_forbidden', 'Request does not belong to this customer.')
        if row.status == EVT_DECLINED:
            return row, False
        if row.status != EVT_OPEN:
            if row.status == EVT_HELD:
                raise RfrError('ofac_hold', 'OFAC hold must be overridden before declining.')
            raise RfrError('not_respondable', 'Request is not open to decline.')
        if not row.key_id:
            raise RfrError('not_respondable', 'Request is not matched to a key.')
        self.enforce_key(key_id=row.key_id, actor=actor, actor_type=actor_type)
        code = normalize_decline(decline_code)
        now = float(self.clock())
        cycle = row.value_date or self.calendar.cycle_date(now)
        seq = self.store.next_rfr_sequence(cycle)
        row.rfr_ref = compose_rfr_ref(cycle, seq)
        row.response_text = RESOLUTION_RJCR + ' ' + code
        row.message_type = MSG_029
        row.status = EVT_DECLINED
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.releaser = str(actor)
        row.posted_at = now
        row.updated_at = now
        self.store.update_inquiry(row)
        return row, True

    def _transmit(self, inquiry: RfrInquiry, *, actor: str) -> RfrInquiry:
        now = float(self.clock())
        cycle = inquiry.value_date or self.calendar.cycle_date(now)
        seq = self.store.next_rfr_sequence(cycle)
        inquiry.rfr_ref = compose_rfr_ref(cycle, seq)
        amount = parse_money(inquiry.amount)
        status = EVT_POSTED
        fail_note = ''
        if self.debit_fn is not None and inquiry.internal_account:
            try:
                result = self.debit_fn(
                    inquiry.internal_account, money_str(amount), 'fednow return %s' % inquiry.rfr_ref[:12],
                )
            except Exception as exc:
                status = EVT_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = EVT_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = EVT_FAILED
                    fail_note = str(result)[:240]
                else:
                    inquiry.fee = money_str(amount)
                    inquiry.fee_status = FEE_COLLECTED
        inquiry.status = status
        inquiry.updated_at = now
        if status == EVT_POSTED:
            inquiry.posted_at = now
            inquiry.releaser = str(actor)
            inquiry.message_type = MSG_004
            inquiry.response_text = RESOLUTION_ACCR
        else:
            inquiry.rfr_ref = ''
            inquiry.note = fail_note or inquiry.note
            self.store.update_inquiry(inquiry)
            if status == EVT_NSF:
                raise RfrError('nsf', 'Insufficient funds to return the FedNow credit.', inquiry=inquiry)
            raise RfrError('failed', 'Return debit failed.', inquiry=inquiry)
        self.store.update_inquiry(inquiry)
        return inquiry

    def cancel_inquiry(
        self,
        *,
        inquiry_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> RfrInquiry:
        self._require_manage(actor_type)
        row = self.get_inquiry(inquiry_id=inquiry_id, actor=actor, actor_type=actor_type)
        if row.status not in CANCELABLE:
            raise RfrError('not_cancelable', 'Inquiry cannot be cancelled.')
        row.status = EVT_CANCELLED
        row.actor = str(actor)
        row.note = normalize_note(note) or row.note
        row.updated_at = float(self.clock())
        self.store.update_inquiry(row)
        return row

    def release_inquiry(self, *, inquiry_id: str, actor: str, actor_type: str) -> RfrInquiry:
        self._require_staff(actor_type)
        row = self.get_inquiry(inquiry_id=inquiry_id, actor=actor, actor_type=actor_type)
        if row.status == EVT_HELD:
            raise RfrError('ofac_hold', 'OFAC hold must be overridden before release.')
        if row.status != EVT_PENDING:
            raise RfrError('not_releasable', 'Inquiry is not pending release.')
        if row.actor and row.actor == actor:
            raise RfrError('same_approver', 'A different employee must release this return.')
        if row.response_text != RESOLUTION_ACCR:
            raise RfrError('not_releasable', 'No accepted return to release.')
        return self._transmit(row, actor=actor)

    def reject_inquiry(
        self,
        *,
        inquiry_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> RfrInquiry:
        self._require_staff(actor_type)
        row = self.get_inquiry(inquiry_id=inquiry_id, actor=actor, actor_type=actor_type)
        if row.status not in OPEN_EVENTS:
            raise RfrError('not_rejectable', 'Inquiry cannot be rejected.')
        row.status = EVT_REJECTED
        row.actor = str(actor)
        row.note = normalize_note(note) or normalize_note(reason) or row.note
        row.updated_at = float(self.clock())
        self.store.update_inquiry(row)
        return row

    def override_ofac(
        self,
        *,
        inquiry_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> RfrInquiry:
        self._require_staff(actor_type)
        row = self.get_inquiry(inquiry_id=inquiry_id, actor=actor, actor_type=actor_type)
        if row.status != EVT_HELD:
            raise RfrError('not_overridable', 'Inquiry is not on an OFAC hold.')
        row.ofac_hit = 0
        row.ofac_match = ''
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        key = self.store.get_key(row.key_id) if row.key_id else None
        row.status = self._evaluate_status(key=key, ofac=ScreenResult(False, '', 0))
        self.store.update_inquiry(row)
        return row

    def waive_fee(self, *, inquiry_id: str, actor: str, actor_type: str) -> RfrInquiry:
        self._require_staff(actor_type)
        row = self.get_inquiry(inquiry_id=inquiry_id, actor=actor, actor_type=actor_type)
        if row.status == EVT_POSTED:
            raise RfrError('not_exportable', 'Fee already posted.')
        row.fee = money_str(Decimal('0.00'))
        row.fee_status = FEE_WAIVED
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_inquiry(row)
        return row

    def reverse_inquiry(
        self,
        *,
        inquiry_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
        customer_request: bool = False,
    ) -> RfrInquiry:
        if customer_request:
            self._require_respond(actor_type)
        else:
            self._require_staff(actor_type)
        row = self.get_inquiry(inquiry_id=inquiry_id, actor=actor, actor_type=actor_type)
        if row.status == EVT_REVERSED:
            raise RfrError('already_reversed', 'Return is already reversed.')
        if row.status != EVT_POSTED:
            raise RfrError('not_reversible', 'Only a posted pacs.004 return can be reversed.')
        now = float(self.clock())
        posted_at = row.posted_at or row.updated_at
        window = CUSTOMER_REVERSE_SECONDS if customer_request else STAFF_REVERSE_SECONDS
        if now - posted_at > window:
            raise RfrError('return_window_closed', 'Reverse window has closed.')
        if row.fee_status == FEE_COLLECTED and self.credit_fn is not None and row.internal_account:
            amount = parse_money(row.amount)
            if amount > 0:
                try:
                    result = self.credit_fn(
                        row.internal_account, money_str(amount), 'fednow return reverse %s' % (row.rfr_ref[:12] or row.inquiry_id[:12]),
                    )
                except Exception as exc:
                    raise RfrError('reverse_failed', str(exc)[:240], inquiry=row) from exc
                if _classify_money_result(result) != 'ok':
                    raise RfrError('reverse_failed', 'Unable to credit returned funds.', inquiry=row)
        row.status = EVT_REVERSED
        row.reversed_at = now
        row.updated_at = now
        row.actor = str(actor)
        row.note = normalize_note(note) or row.note
        self.store.update_inquiry(row)
        return row

    def export_file(self, *, actor: str, actor_type: str, userid: Optional[str] = None) -> str:
        self._require_staff(actor_type)
        rows = self.store.list_inquiries(userid)
        lines = []
        for row in rows:
            if row.status not in {EVT_POSTED, EVT_OPEN, EVT_PENDING, EVT_HELD, EVT_DECLINED}:
                continue
            lines.append(compose_rfr1(
                message_type=row.message_type,
                e2e=mask_e2e(row.e2e_id),
                reason=row.reason,
                aba=mask_aba(row.requestor_aba),
                account=mask_account(row.account),
                amount=row.amount,
                name=row.requestor_name,
                reference=mask_rfr_ref(row.rfr_ref) if row.rfr_ref else '',
                status=row.status,
            ))
        if not lines:
            raise RfrError('not_exportable', 'No RFR requests to export.')
        return '\n'.join(lines)

    def run_due(self, userid: Optional[str] = None) -> List[RfrInquiry]:
        now = float(self.clock())
        changed: List[RfrInquiry] = []
        rows = self.store.list_unmatched() if userid is None else [
            row for row in self.store.list_inquiries(userid) if row.status == EVT_UNMATCHED
        ]
        if userid is None:
            rows = self.store.list_unmatched()
        else:
            rows = [row for row in self.store.list_inquiries(userid) if row.status == EVT_UNMATCHED]
            rows.extend([row for row in self.store.list_unmatched() if row.fingerprint])
            seen = set()
            unique = []
            for row in rows:
                if row.inquiry_id in seen:
                    continue
                seen.add(row.inquiry_id)
                unique.append(row)
            rows = unique
        for row in self.store.list_inquiries(userid):
            age = now - row.created_at
            if row.status == EVT_UNMATCHED and age >= UNMATCHED_TTL_SECONDS:
                row.status = EVT_EXPIRED
                row.updated_at = now
                self.store.update_inquiry(row)
                changed.append(row)
            elif row.status == EVT_OPEN and age >= OPEN_TTL_SECONDS:
                row.status = EVT_EXPIRED
                row.updated_at = now
                self.store.update_inquiry(row)
                changed.append(row)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.run_due(userid)
        keys = self.store.list_keys(userid)
        inquiries = self.store.list_inquiries(userid)
        returned_ytd = Decimal('0.00')
        posted = 0
        declined = 0
        open_count = 0
        for row in inquiries:
            if row.status == EVT_POSTED:
                posted += 1
                if row.fee_status == FEE_COLLECTED:
                    returned_ytd += parse_money(row.amount, allow_zero=True)
            if row.status == EVT_DECLINED:
                declined += 1
            if row.status in OPEN_EVENTS:
                open_count += 1
        now = float(self.clock())
        payload = {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clock': self.calendar.snapshot(now),
            'keys': [row.to_dict() for row in keys[:40]],
            'inquiries': [row.to_dict() for row in inquiries[:40]],
            'correspondents': [],
            'unmatched': [],
            'ytd_returned': money_str(returned_ytd),
            'posted_count': posted,
            'declined_count': declined,
            'active_count': sum(1 for row in keys if row.status == KEY_ACTIVE),
            'open_count': open_count,
        }
        if actor_type in EMPLOYEE_ROLES:
            payload['correspondents'] = [row.to_dict() for row in self.store.list_correspondents()[:40]]
            payload['unmatched'] = [row.to_dict() for row in self.store.list_unmatched()[:40]]
        return payload


_SERVICE: Optional[RfrService] = None


def set_service(service: Optional[RfrService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[RfrService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('RFR_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryRfrStore()
    path = os.environ.get('RFR_DB', DEFAULT_STORE_PATH)
    return SqliteRfrStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[FedNowClock] = None,
) -> RfrService:
    if store is None:
        store = default_store()
    return RfrService(
        RfrPolicy.from_env(),
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
        'key_duplicate': 409,
        'key_limit': 409,
        'inquiry_limit': 409,
        'correspondent_duplicate': 409,
        'correspondent_limit': 409,
        'already_cancelled': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_archived': 409,
        'already_reversed': 409,
        'nsf': 409,
        'failed': 409,
        'reverse_failed': 409,
        'rfr_forbidden': 403,
        'rfr_disabled': 403,
        'key_paused': 403,
        'correspondent_paused': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_cancelable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_reversible': 403,
        'not_overridable': 403,
        'not_claimable': 403,
        'not_assignable': 403,
        'not_exportable': 403,
        'not_respondable': 403,
        'not_declinable': 403,
        'return_window_closed': 403,
        'key_not_found': 404,
        'inquiry_not_found': 404,
        'correspondent_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_aba': 400,
        'invalid_e2e': 400,
        'invalid_reason': 400,
        'invalid_message': 400,
        'invalid_response': 400,
        'invalid_status': 400,
        'invalid_reference': 400,
        'invalid_file': 400,
        'missing_customer_id': 400,
        'missing_key': 400,
        'missing_inquiry': 400,
        'missing_correspondent': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: RfrError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('inquiry') is not None:
        body['inquiry'] = exc.extra['inquiry'].to_dict()
    if exc.extra.get('key') is not None:
        body['key'] = exc.extra['key'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except RfrError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snap(service: RfrService, owner: str, userid: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(owner, actor=userid, actor_type=actor_type)


def handle_list(service: RfrService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'FedNowRfr': _snap(service, owner, userid, actor_type)}), 200


def handle_add_key(service: RfrService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.add_key(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname'),
            legal_name=values.get('legal_name') or values.get('name'),
            default_account=values.get('default_account') or values.get('account') or values.get('from_account'),
        )
        return jsonify({
            'message': 'FedNow alias enrolled',
            'key': row.to_dict(),
            'FedNowRfr': _snap(service, owner, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _key_status_route(service: RfrService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    key_id = str(values.get('key_id') or '').strip()
    if not key_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_key'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_key_status(key_id=key_id, actor=userid, actor_type=actor_type, status=status)
        return jsonify({
            'message': ok_message,
            'key': row.to_dict(),
            'FedNowRfr': _snap(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: RfrService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        preview = service.preview(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            inquiry_id=values.get('inquiry_id') or values.get('rfr_id'),
            key_id=values.get('key_id'),
            reason=values.get('reason'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({'preview': preview, 'FedNowRfr': _snap(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_accept(service: RfrService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    inquiry_id = str(values.get('inquiry_id') or values.get('rfr_id') or '').strip()
    if not inquiry_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inquiry'}), 400

    def _run():
        row, created = service.accept(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            inquiry_id=inquiry_id,
            note=values.get('note') or values.get('response') or '',
        )
        return jsonify({
            'message': 'Return accepted' if created else 'Return already posted',
            'inquiry': row.to_dict(),
            'FedNowRfr': _snap(service, owner, userid, actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_decline(service: RfrService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    inquiry_id = str(values.get('inquiry_id') or values.get('rfr_id') or '').strip()
    if not inquiry_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inquiry'}), 400

    def _run():
        row, created = service.decline(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            inquiry_id=inquiry_id,
            decline_code=values.get('decline') or values.get('reason') or values.get('response') or DECLINE_LEGL,
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Return declined' if created else 'Decline already posted',
            'inquiry': row.to_dict(),
            'FedNowRfr': _snap(service, owner, userid, actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: RfrService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    inquiry_id = str(values.get('inquiry_id') or values.get('rfr_id') or '').strip()
    if not inquiry_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inquiry'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.cancel_inquiry(
            inquiry_id=inquiry_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': 'RFR cancelled',
            'inquiry': row.to_dict(),
            'FedNowRfr': _snap(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_claim(service: RfrService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.claim(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            inquiry_id=values.get('inquiry_id') or values.get('rfr_id'),
            account=values.get('account') or values.get('account_number'),
            last4_digits=values.get('last4') or values.get('account_last4'),
            default_account=values.get('default_account') or values.get('account'),
            nickname=values.get('nickname'),
            legal_name=values.get('legal_name'),
        )
        return jsonify({
            'message': 'Inquiry claimed',
            'inquiry': row.to_dict(),
            'FedNowRfr': _snap(service, owner, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_add_correspondent(service: RfrService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.add_correspondent(
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname'),
            legal_name=values.get('legal_name') or values.get('name'),
            aba=values.get('aba') or values.get('routing'),
        )
        owner = str(values.get('customer_id') or userid)
        return jsonify({
            'message': 'Correspondent enrolled',
            'correspondent': row.to_dict(),
            'FedNowRfr': _snap(service, owner, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _corr_status_route(service: RfrService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    correspondent_id = str(values.get('correspondent_id') or '').strip()
    if not correspondent_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_correspondent'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_correspondent_status(
            correspondent_id=correspondent_id, actor=userid, actor_type=actor_type, status=status,
        )
        owner = str(values.get('customer_id') or userid)
        return jsonify({
            'message': ok_message,
            'correspondent': row.to_dict(),
            'FedNowRfr': _snap(service, owner, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_ingest(service: RfrService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row, created = service.ingest_inquiry(
            actor=userid,
            actor_type=actor_type,
            e2e=values.get('e2e') or values.get('e2e_id') or values.get('end_to_end'),
            account=values.get('account') or values.get('account_number'),
            aba=values.get('aba') or values.get('routing'),
            reason=values.get('reason'),
            amount=values.get('amount'),
            requestor_name=values.get('requestor_name') or values.get('name'),
            correspondent_id=values.get('correspondent_id'),
            message_type=values.get('message_type') or MSG_056,
            waive_fee=bool(values.get('waive_fee')),
            note=values.get('note') or '',
        )
        owner = row.userid or str(values.get('customer_id') or userid)
        return jsonify({
            'message': 'Inquiry ingested' if created else 'Inquiry already posted',
            'inquiry': row.to_dict(),
            'FedNowRfr': _snap(service, owner, userid, actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_file(service: RfrService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    payload = values.get('file') or values.get('payload') or values.get('rfr1')
    if not payload:
        return jsonify({'message': 'Some data missing', 'error': 'missing_file'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        rows = service.ingest_file(actor=userid, actor_type=actor_type, payload=payload)
        owner = str(values.get('customer_id') or userid)
        return jsonify({
            'message': 'RFR1 file ingested',
            'count': len(rows),
            'inquiries': [row.to_dict() for row in rows],
            'FedNowRfr': _snap(service, owner, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def handle_assign(service: RfrService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    inquiry_id = str(values.get('inquiry_id') or values.get('rfr_id') or '').strip()
    if not inquiry_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inquiry'}), 400
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.assign(
            inquiry_id=inquiry_id,
            actor=userid,
            actor_type=actor_type,
            owner_userid=owner,
            key_id=values.get('key_id'),
            nickname=values.get('nickname'),
            legal_name=values.get('legal_name'),
            default_account=values.get('default_account') or values.get('account'),
        )
        return jsonify({
            'message': 'Inquiry assigned',
            'inquiry': row.to_dict(),
            'FedNowRfr': _snap(service, owner, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_inquiry_route(service: RfrService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    inquiry_id = str(values.get('inquiry_id') or values.get('rfr_id') or '').strip()
    if not inquiry_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inquiry'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'release':
            row = service.release_inquiry(inquiry_id=inquiry_id, actor=userid, actor_type=actor_type)
            message = 'Return released'
        elif action == 'reject':
            row = service.reject_inquiry(
                inquiry_id=inquiry_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'RFR rejected'
        elif action == 'reverse':
            row = service.reverse_inquiry(
                inquiry_id=inquiry_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Return reversed'
        elif action == 'customer_reverse':
            row = service.reverse_inquiry(
                inquiry_id=inquiry_id, actor=userid, actor_type=actor_type,
                note=values.get('note') or '', customer_request=True,
            )
            message = 'Reverse requested'
        elif action == 'override':
            row = service.override_ofac(
                inquiry_id=inquiry_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'waive':
            row = service.waive_fee(inquiry_id=inquiry_id, actor=userid, actor_type=actor_type)
            message = 'return debit waived'
        else:
            raise RfrError('invalid_status', 'Unknown RFR action.')
        return jsonify({
            'message': message,
            'inquiry': row.to_dict(),
            'FedNowRfr': _snap(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_export(service: RfrService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = None
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or '').strip() or None

    def _run():
        payload = service.export_file(actor=userid, actor_type=actor_type, userid=owner)
        return jsonify({
            'message': 'RFR1 exported',
            'file': payload,
            'FedNowRfr': _snap(service, owner or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_unmatched(service: RfrService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'

    def _run():
        service._require_staff(actor_type)
        rows = service.store.list_unmatched()
        return jsonify({
            'unmatched': [row.to_dict() for row in rows[:40]],
            'FedNowRfr': _snap(service, userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: RfrService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'FedNowRfr': _snap(service, owner, userid, actor_type)}), 200


def attach_rfr_routes(app, service: RfrService) -> None:
    @app.route('/listFedNowRfr', methods=['POST', 'GET'])
    def list_fednow_rfr_route():
        return handle_list(service)

    @app.route('/listFedNowRfrKeys', methods=['POST', 'GET'])
    def list_fednow_rfr_keys_route():
        return handle_list(service)

    @app.route('/listUnmatchedFedNowRfr', methods=['POST', 'GET'])
    def list_unmatched_fednow_rfr_route():
        return handle_unmatched(service)

    @app.route('/addFedNowRfrKey', methods=['POST', 'GET'])
    def add_fednow_rfr_key_route():
        return handle_add_key(service)

    @app.route('/pauseFedNowRfrKey', methods=['POST', 'GET'])
    def pause_fednow_rfr_key_route():
        return _key_status_route(service, KEY_PAUSED, 'Alias paused')

    @app.route('/resumeFedNowRfrKey', methods=['POST', 'GET'])
    def resume_fednow_rfr_key_route():
        return _key_status_route(service, KEY_ACTIVE, 'Alias resumed')

    @app.route('/cancelFedNowRfrKey', methods=['POST', 'GET'])
    def cancel_fednow_rfr_key_route():
        return _key_status_route(service, KEY_CANCELLED, 'Alias cancelled')

    @app.route('/previewFedNowRfr', methods=['POST', 'GET'])
    def preview_fednow_rfr_route():
        return handle_preview(service)

    @app.route('/acceptFedNowRfr', methods=['POST', 'GET'])
    def accept_fednow_rfr_route():
        return handle_accept(service)

    @app.route('/declineFedNowRfr', methods=['POST', 'GET'])
    def decline_fednow_rfr_route():
        return handle_decline(service)

    @app.route('/cancelFedNowRfr', methods=['POST', 'GET'])
    def cancel_fednow_rfr_route():
        return handle_cancel(service)

    @app.route('/claimFedNowRfr', methods=['POST', 'GET'])
    def claim_fednow_rfr_route():
        return handle_claim(service)

    @app.route('/requestFedNowRfrReverse', methods=['POST', 'GET'])
    def request_fednow_rfr_reverse_route():
        return _staff_inquiry_route(service, 'customer_reverse')

    @app.route('/addFedNowRfrCorrespondent', methods=['POST', 'GET'])
    def add_fednow_rfr_correspondent_route():
        return handle_add_correspondent(service)

    @app.route('/listFedNowRfrCorrespondents', methods=['POST', 'GET'])
    def list_fednow_rfr_correspondents_route():
        return handle_list(service)

    @app.route('/pauseFedNowRfrCorrespondent', methods=['POST', 'GET'])
    def pause_fednow_rfr_correspondent_route():
        return _corr_status_route(service, CORR_PAUSED, 'Correspondent paused')

    @app.route('/resumeFedNowRfrCorrespondent', methods=['POST', 'GET'])
    def resume_fednow_rfr_correspondent_route():
        return _corr_status_route(service, CORR_ACTIVE, 'Correspondent resumed')

    @app.route('/archiveFedNowRfrCorrespondent', methods=['POST', 'GET'])
    def archive_fednow_rfr_correspondent_route():
        return _corr_status_route(service, CORR_ARCHIVED, 'Correspondent archived')

    @app.route('/ingestFedNowRfr', methods=['POST', 'GET'])
    def ingest_fednow_rfr_route():
        return handle_ingest(service)

    @app.route('/ingestFedNowRfrFile', methods=['POST', 'GET'])
    def ingest_fednow_rfr_file_route():
        return handle_ingest_file(service)

    @app.route('/assignFedNowRfr', methods=['POST', 'GET'])
    def assign_fednow_rfr_route():
        return handle_assign(service)

    @app.route('/overrideFedNowRfrOfac', methods=['POST', 'GET'])
    def override_fednow_rfr_ofac_route():
        return _staff_inquiry_route(service, 'override')

    @app.route('/releaseFedNowRfr', methods=['POST', 'GET'])
    def release_fednow_rfr_route():
        return _staff_inquiry_route(service, 'release')

    @app.route('/rejectFedNowRfr', methods=['POST', 'GET'])
    def reject_fednow_rfr_route():
        return _staff_inquiry_route(service, 'reject')

    @app.route('/reverseFedNowRfr', methods=['POST', 'GET'])
    def reverse_fednow_rfr_route():
        return _staff_inquiry_route(service, 'reverse')

    @app.route('/waiveFedNowRfrFee', methods=['POST', 'GET'])
    def waive_fednow_rfr_fee_route():
        return _staff_inquiry_route(service, 'waive')

    @app.route('/exportFedNowRfr', methods=['POST', 'GET'])
    def export_fednow_rfr_route():
        return handle_export(service)

    @app.route('/runDueFedNowRfr', methods=['POST', 'GET'])
    def run_due_fednow_rfr_route():
        return handle_run_due(service)
