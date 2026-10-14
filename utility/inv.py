"""ISO 20022 payment-status reports and FI-to-FI recalls.

Staff ingest pacs.002 status (ACCP/ACSC/ACSP/PDNG/RJCT/CANC) and camt.056
cancellation requests. Customers enroll a payment by UETR, request a recall,
and accept or decline inbound recalls. Resolutions compose camt.029 (ACCR/RJCR).
Independent of outbound RTP origination + customer-sent RfP (PR #83), inbound
FedNow/RTP credits (PR #93), inbound RfP fulfillment (PR #127), Zelle
send/receive (PR #123/#125), ACH linking (PR #68), and domestic Fedwire
(PR #73). Existing `/fundTransfer`, `/withdrawAmount`, and `/sendWire`
stay unchanged.

Foundations (reusable beyond this screen):
- ISO 20022 pacs.002 / camt.056 / camt.029 pipe compose/parse (XML/DOCTYPE rejected)
- UETR (RFC 4122 UUID) uniqueness and EndToEndId (INV + YYYYMMDD + seq)
- Instant 24/7 clock for FedNow/RTP; Fedwire uses the reusable WireCalendar
- Dual-control at $10k on money-moving recall resolution, not on status ingest
- 24h instant recall window; wire recalls close next Fed business day
- Reusable ABA checksum + OFAC screen via utility.wire

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Full UETR, EndToEndId, and destination accounts never appear in to_dict / snapshots.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    DEFAULT_WATCHLIST,
    AccountError,
    AmountError,
    ScreenResult,
    WireCalendar,
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
DIR_OUT = 'outbound'
DIR_IN = 'inbound'
DIRECTIONS = frozenset({DIR_OUT, DIR_IN})
DIR_ALIASES = {
    'outbound': DIR_OUT, 'out': DIR_OUT, 'sent': DIR_OUT, 'debit': DIR_OUT, 'originated': DIR_OUT,
    'inbound': DIR_IN, 'in': DIR_IN, 'received': DIR_IN, 'credit': DIR_IN, 'incoming': DIR_IN,
}
RAIL_FEDNOW = 'fednow'
RAIL_RTP = 'rtp'
RAIL_WIRE = 'wire'
RAILS = frozenset({RAIL_FEDNOW, RAIL_RTP, RAIL_WIRE})
RAIL_ALIASES = {
    'fednow': RAIL_FEDNOW, 'fed_now': RAIL_FEDNOW, 'fn': RAIL_FEDNOW, 'now': RAIL_FEDNOW,
    'rtp': RAIL_RTP, 'tch': RAIL_RTP, 'clearinghouse': RAIL_RTP, 'theclearinghouse': RAIL_RTP,
    'wire': RAIL_WIRE, 'fedwire': RAIL_WIRE, 'fw': RAIL_WIRE, 'domestic': RAIL_WIRE,
}
NET_ACCP = 'ACCP'
NET_ACSC = 'ACSC'
NET_ACSP = 'ACSP'
NET_PDNG = 'PDNG'
NET_RJCT = 'RJCT'
NET_CANC = 'CANC'
NETWORK_STATUSES = frozenset({NET_ACCP, NET_ACSC, NET_ACSP, NET_PDNG, NET_RJCT, NET_CANC})
NET_ALIASES = {
    'accp': NET_ACCP, 'accepted': NET_ACCP, 'accept': NET_ACCP,
    'acsc': NET_ACSC, 'settled': NET_ACSC, 'settlement': NET_ACSC, 'completed': NET_ACSC,
    'acsp': NET_ACSP, 'accepted_settlement_in_process': NET_ACSP,
    'pdng': NET_PDNG, 'pending': NET_PDNG,
    'rjct': NET_RJCT, 'rejected': NET_RJCT, 'reject': NET_RJCT,
    'canc': NET_CANC, 'cancelled': NET_CANC, 'canceled': NET_CANC,
}
KIND_PACS002 = 'pacs002'
KIND_CAMT056 = 'camt056'
KIND_CAMT029 = 'camt029'
MESSAGE_KINDS = frozenset({KIND_PACS002, KIND_CAMT056, KIND_CAMT029})
RES_ACCR = 'ACCR'
RES_RJCR = 'RJCR'
RESOLUTIONS = frozenset({RES_ACCR, RES_RJCR})
RES_ALIASES = {
    'accr': RES_ACCR, 'accept': RES_ACCR, 'accepted': RES_ACCR, 'agree': RES_ACCR,
    'rjcr': RES_RJCR, 'reject': RES_RJCR, 'rejected': RES_RJCR, 'decline': RES_RJCR, 'refuse': RES_RJCR,
}
RECALL_REASONS = frozenset({'DUPL', 'TECH', 'FRAD', 'CUST', 'AC03', 'AM09', 'FOCR'})
RECALL_ALIASES = {
    'dupl': 'DUPL', 'duplicate': 'DUPL', 'dup': 'DUPL',
    'tech': 'TECH', 'technical': 'TECH',
    'frad': 'FRAD', 'fraud': 'FRAD',
    'cust': 'CUST', 'customer': 'CUST', 'requested': 'CUST',
    'ac03': 'AC03', 'wrong_account': 'AC03',
    'am09': 'AM09', 'wrong_amount': 'AM09',
    'focr': 'FOCR', 'following_cancellation': 'FOCR',
}
INV_UNMATCHED = 'unmatched'
INV_HELD = 'held'
INV_OPEN = 'open'
INV_ACCEPTED = 'accepted'
INV_SETTLED = 'settled'
INV_PENDING = 'pending'
INV_REJECTED = 'rejected'
INV_CANCELLED = 'cancelled'
INV_PENDING_RELEASE = 'pending_release'
INV_RECALL_OUT = 'recall_requested'
INV_RECALL_IN = 'recall_inbound'
INV_RESOLVED_OK = 'resolved_accepted'
INV_RESOLVED_NO = 'resolved_rejected'
INV_EXPIRED = 'expired'
INV_FAILED = 'failed'
INV_STATUSES = frozenset({
    INV_UNMATCHED, INV_HELD, INV_OPEN, INV_ACCEPTED, INV_SETTLED, INV_PENDING,
    INV_REJECTED, INV_CANCELLED, INV_PENDING_RELEASE, INV_RECALL_OUT, INV_RECALL_IN,
    INV_RESOLVED_OK, INV_RESOLVED_NO, INV_EXPIRED, INV_FAILED,
})
OPEN_INV = frozenset({
    INV_UNMATCHED, INV_HELD, INV_OPEN, INV_ACCEPTED, INV_SETTLED, INV_PENDING,
    INV_PENDING_RELEASE, INV_RECALL_OUT, INV_RECALL_IN,
})
NET_TO_STATUS = {
    NET_ACCP: INV_ACCEPTED,
    NET_ACSC: INV_SETTLED,
    NET_ACSP: INV_PENDING,
    NET_PDNG: INV_PENDING,
    NET_RJCT: INV_REJECTED,
    NET_CANC: INV_CANCELLED,
}
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/inv.sqlite'
DEFAULT_RECEIVER = '021000021'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)
E2E_RE = re.compile(r'^INV\d{14}$')
UETR_RE = re.compile(r'^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$')
MSG_RE = re.compile(r'^(TXST|CXL|RSIN)\d{14}$')


class InvError(ValueError):
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
            raise InvError('invalid_routing', 'Participant ABA required.')
        return ''
    if len(digits) < 9:
        digits = digits.zfill(9)
    if len(digits) != 9 or digits == '000000000' or not aba_check_digit_ok(digits):
        raise InvError('invalid_routing', 'Participant ABA failed checksum.')
    return digits


def mask_participant(routing: str) -> str:
    digits = re.sub(r'\D', '', str(routing or ''))
    if len(digits) < 4:
        return '****'
    return digits[:4] + '****' + digits[-1:]


def normalize_rail(value: Any, *, default: str = RAIL_FEDNOW) -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text not in RAILS:
        raise InvError('invalid_rail', 'Rail must be fednow, rtp, or wire.')
    return text


def normalize_direction(value: Any, *, default: str = DIR_OUT) -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = DIR_ALIASES.get(text, text)
    if text not in DIRECTIONS:
        raise InvError('invalid_status', 'Direction must be outbound or inbound.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise InvError('invalid_name', 'Counterparty legal name must be 2-80 characters.')
    return text


def normalize_nickname(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise InvError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_currency(value: Any) -> str:
    text = str(value or 'USD').strip().upper()
    if text != 'USD':
        raise InvError('invalid_currency', 'Investigations must be USD.')
    return text


def normalize_network_status(value: Any) -> str:
    text = str(value or '').strip().upper().replace('-', '_').replace(' ', '_')
    text = NET_ALIASES.get(text.lower(), text)
    if text not in NETWORK_STATUSES:
        raise InvError('invalid_status', 'Unknown pacs.002 transaction status.')
    return text


def normalize_resolution(value: Any) -> str:
    text = str(value or '').strip().upper().replace('-', '_').replace(' ', '_')
    text = RES_ALIASES.get(text.lower(), text)
    if text not in RESOLUTIONS:
        raise InvError('invalid_status', 'Resolution must be ACCR or RJCR.')
    return text


def normalize_recall_reason(value: Any, *, default: str = 'CUST') -> str:
    text = str(value or default).strip().upper().replace('-', '_').replace(' ', '_')
    text = RECALL_ALIASES.get(text.lower(), text)
    if text not in RECALL_REASONS:
        raise InvError('invalid_reason', 'Unknown camt.056 cancellation reason.')
    return text


def compose_end_to_end_id(cycle_date: str, sequence: int) -> str:
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise InvError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise InvError('invalid_reference', 'Investigation sequence out of range.')
    return 'INV%s%06d' % (day, seq)


def normalize_end_to_end_id(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        if required:
            raise InvError('invalid_reference', 'EndToEndId required.')
        return ''
    if not E2E_RE.match(text):
        raise InvError('invalid_reference', 'EndToEndId must be INV + 14 digits.')
    return text


def mask_end_to_end_id(value: str) -> str:
    text = str(value or '')
    if len(text) < 8:
        return 'INV********'
    return text[:6] + '*****' + text[-4:]


def compose_uetr(value: Any = None) -> str:
    if value:
        return normalize_uetr(value)
    return str(uuid.uuid4()).upper()


def normalize_uetr(value: Any, *, required: bool = True) -> str:
    text = str(value or '').strip().upper()
    if not text:
        if required:
            raise InvError('invalid_reference', 'UETR required.')
        return ''
    if not UETR_RE.match(text):
        raise InvError('invalid_reference', 'UETR must be an RFC 4122 UUID.')
    return text


def mask_uetr(value: str) -> str:
    text = str(value or '')
    if len(text) < 12:
        return '********'
    return text[:8] + '***' + text[-4:]


def compose_message_id(kind: str, cycle_date: str, sequence: int) -> str:
    prefixes = {KIND_PACS002: 'TXST', KIND_CAMT056: 'CXL', KIND_CAMT029: 'RSIN'}
    prefix = prefixes.get(kind)
    if prefix is None:
        raise InvError('invalid_file', 'Unknown ISO 20022 message kind.')
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise InvError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise InvError('invalid_reference', 'Message sequence out of range.')
    return '%s%s%06d' % (prefix, day, seq)


def normalize_message_id(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        if required:
            raise InvError('invalid_reference', 'Message id required.')
        return ''
    if not MSG_RE.match(text):
        raise InvError('invalid_reference', 'Message id must be TXST/CXL/RSIN + 14 digits.')
    return text


def mask_message_id(value: str) -> str:
    text = str(value or '')
    if len(text) < 8:
        return '********'
    return text[:6] + '*****' + text[-4:]


def mask_account(value: str) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        return ''
    return last4(digits)


class InstantClock:
    """Fed holiday calendar. Instant rails are 24/7; snapshot still reports holidays."""

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
    if '<?xml' in low or '<!doctype' in low or '<!entity' in low or '<pacs' in low or '<camt' in low:
        raise InvError('invalid_file', 'XML/DOCTYPE investigation files are rejected.')


def compose_pacs002(
    *,
    message_id: str,
    uetr: str,
    end_to_end_id: str,
    status: str,
    amount: Decimal,
    rail: str,
    party_name: str,
    party_aba: str,
    receiver: str = DEFAULT_RECEIVER,
    cycle_date: str = '',
) -> str:
    fields = [
        'PACS002',
        normalize_message_id(message_id),
        normalize_uetr(uetr),
        normalize_end_to_end_id(end_to_end_id),
        normalize_network_status(status),
        money_str(amount),
        'USD',
        normalize_rail(rail),
        normalize_legal_name(party_name).replace('|', ' '),
        normalize_participant(party_aba),
        normalize_participant(receiver),
        str(cycle_date or ''),
    ]
    return '|'.join(fields)


def parse_pacs002(record: str) -> Dict[str, Any]:
    text = str(record or '').strip()
    _reject_xml(text)
    if not text or text.startswith('#'):
        raise InvError('invalid_file', 'Empty pacs.002 record.')
    parts = text.split('|')
    if len(parts) != 12 or parts[0] != 'PACS002':
        raise InvError('invalid_file', 'Inbound PACS002 records must have 12 pipe fields.')
    return {
        'message_id': normalize_message_id(parts[1]),
        'uetr': normalize_uetr(parts[2]),
        'end_to_end_id': normalize_end_to_end_id(parts[3]),
        'network_status': normalize_network_status(parts[4]),
        'amount': parse_money(parts[5]),
        'currency': normalize_currency(parts[6]),
        'rail': normalize_rail(parts[7]),
        'counterparty_name': normalize_legal_name(parts[8]),
        'counterparty_aba': normalize_participant(parts[9]),
        'receiver_routing': normalize_participant(parts[10]),
        'cycle_date': str(parts[11] or '').strip(),
        'kind': KIND_PACS002,
    }


def compose_camt056(
    *,
    message_id: str,
    uetr: str,
    end_to_end_id: str,
    amount: Decimal,
    rail: str,
    reason: str,
    party_name: str,
    party_aba: str,
    receiver: str = DEFAULT_RECEIVER,
) -> str:
    fields = [
        'CAMT056',
        normalize_message_id(message_id),
        normalize_uetr(uetr),
        normalize_end_to_end_id(end_to_end_id),
        money_str(amount),
        'USD',
        normalize_rail(rail),
        normalize_recall_reason(reason),
        normalize_legal_name(party_name).replace('|', ' '),
        normalize_participant(party_aba),
        normalize_participant(receiver),
    ]
    return '|'.join(fields)


def parse_camt056(record: str) -> Dict[str, Any]:
    text = str(record or '').strip()
    _reject_xml(text)
    if not text or text.startswith('#'):
        raise InvError('invalid_file', 'Empty camt.056 record.')
    parts = text.split('|')
    if len(parts) != 11 or parts[0] != 'CAMT056':
        raise InvError('invalid_file', 'Inbound CAMT056 records must have 11 pipe fields.')
    return {
        'message_id': normalize_message_id(parts[1]),
        'uetr': normalize_uetr(parts[2]),
        'end_to_end_id': normalize_end_to_end_id(parts[3]),
        'amount': parse_money(parts[4]),
        'currency': normalize_currency(parts[5]),
        'rail': normalize_rail(parts[6]),
        'reason': normalize_recall_reason(parts[7]),
        'counterparty_name': normalize_legal_name(parts[8]),
        'counterparty_aba': normalize_participant(parts[9]),
        'receiver_routing': normalize_participant(parts[10]),
        'kind': KIND_CAMT056,
    }


def compose_camt029(row: 'InvCase', *, resolution: str = RES_ACCR) -> str:
    return '|'.join([
        'CAMT029',
        row.resolution_id or mask_message_id(row.message_id),
        mask_uetr(row.uetr),
        mask_end_to_end_id(row.end_to_end_id),
        normalize_resolution(resolution),
        row.reason or '',
        row.rail,
        mask_participant(row.receiver_routing),
    ])


def split_inv_file(payload: str, *, header: str) -> List[str]:
    text = str(payload or '')
    _reject_xml(text)
    records = []
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        records.append(stripped)
    if not records:
        raise InvError('invalid_file', 'Investigation file had no %s records.' % header)
    return records


def message_from_pacs002_values(values: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'message_id': normalize_message_id(
            values.get('message_id') or values.get('msg_id') or values.get('scheme_id'),
        ),
        'uetr': normalize_uetr(values.get('uetr') or values.get('uetr_id')),
        'end_to_end_id': normalize_end_to_end_id(
            values.get('end_to_end_id') or values.get('e2e') or values.get('reference'),
        ),
        'network_status': normalize_network_status(
            values.get('network_status') or values.get('status') or values.get('txn_status'),
        ),
        'amount': parse_money(values.get('amount') or values.get('amount_usd')),
        'currency': normalize_currency(values.get('currency') or 'USD'),
        'rail': normalize_rail(values.get('rail') or RAIL_FEDNOW),
        'counterparty_name': normalize_legal_name(
            values.get('counterparty_name') or values.get('party_name') or values.get('name'),
        ),
        'counterparty_aba': normalize_participant(
            values.get('counterparty_aba') or values.get('aba') or values.get('routing'),
        ),
        'receiver_routing': normalize_participant(
            values.get('receiver_routing') or values.get('receiver') or DEFAULT_RECEIVER,
        ),
        'cycle_date': str(values.get('cycle_date') or values.get('value_date') or '').strip(),
        'kind': KIND_PACS002,
        'direction': normalize_direction(values.get('direction') or DIR_OUT, default=DIR_OUT),
    }


def message_from_camt056_values(values: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'message_id': normalize_message_id(
            values.get('message_id') or values.get('msg_id') or values.get('case_id'),
        ),
        'uetr': normalize_uetr(values.get('uetr') or values.get('uetr_id')),
        'end_to_end_id': normalize_end_to_end_id(
            values.get('end_to_end_id') or values.get('e2e') or values.get('reference'),
        ),
        'amount': parse_money(values.get('amount') or values.get('amount_usd')),
        'currency': normalize_currency(values.get('currency') or 'USD'),
        'rail': normalize_rail(values.get('rail') or RAIL_FEDNOW),
        'reason': normalize_recall_reason(values.get('reason') or values.get('cancel_reason') or 'CUST'),
        'counterparty_name': normalize_legal_name(
            values.get('counterparty_name') or values.get('party_name') or values.get('name'),
        ),
        'counterparty_aba': normalize_participant(
            values.get('counterparty_aba') or values.get('aba') or values.get('routing'),
        ),
        'receiver_routing': normalize_participant(
            values.get('receiver_routing') or values.get('receiver') or DEFAULT_RECEIVER,
        ),
        'kind': KIND_CAMT056,
        'direction': normalize_direction(values.get('direction') or DIR_IN, default=DIR_IN),
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
class InvPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_recall: bool = True
    allow_credit: bool = False
    max_enrollments: int = 12
    max_cases: int = 120
    min_amount: Decimal = Decimal('0.01')
    fednow_cap: Decimal = Decimal('100000.00')
    rtp_cap: Decimal = Decimal('1000000.00')
    wire_cap: Decimal = Decimal('1000000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    instant_recall_hours: int = 24
    tz_offset_hours: int = -4
    cutoff_hour: int = 17
    receiver_routing: str = DEFAULT_RECEIVER
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'InvPolicy':
        extra = _env_list('INV_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        routing = os.environ.get('INV_RECEIVER') or DEFAULT_RECEIVER
        return cls(
            enabled=_env_bool('INV_ENABLED', True),
            customer_manage=_env_bool('INV_CUSTOMER_MANAGE', True),
            customer_recall=_env_bool('INV_CUSTOMER_RECALL', True),
            allow_credit=_env_bool('INV_ALLOW_CREDIT', False),
            max_enrollments=max(1, _env_int('INV_MAX_ENROLLMENTS', 12)),
            max_cases=max(1, _env_int('INV_MAX_CASES', 120)),
            min_amount=_env_money('INV_MIN_AMOUNT', '0.01'),
            fednow_cap=_env_money('INV_FEDNOW_CAP', '100000.00'),
            rtp_cap=_env_money('INV_RTP_CAP', '1000000.00'),
            wire_cap=_env_money('INV_WIRE_CAP', '1000000.00'),
            dual_control_threshold=_env_money('INV_DUAL_CONTROL', '10000.00'),
            instant_recall_hours=max(1, _env_int('INV_RECALL_HOURS', 24)),
            tz_offset_hours=_env_int('INV_TZ_OFFSET', -4),
            cutoff_hour=_env_int('INV_CUTOFF_HOUR', 17),
            receiver_routing=normalize_participant(routing),
            watchlist=watch,
            extra_holidays=_env_list('INV_HOLIDAYS'),
        )

    def cap_for(self, rail: str) -> Decimal:
        if rail == RAIL_RTP:
            return self.rtp_cap
        if rail == RAIL_WIRE:
            return self.wire_cap
        return self.fednow_cap


@dataclass
class InvEnrollment:
    enrollment_id: str
    userid: str
    nickname: str
    direction: str
    rail: str
    amount_usd: str
    account: str
    counterparty_name: str
    counterparty_aba: str
    uetr: str
    end_to_end_id: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'enrollment_id': self.enrollment_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'direction': self.direction,
            'rail': self.rail,
            'amount_usd': self.amount_usd,
            'account_last4': mask_account(self.account),
            'counterparty_name': self.counterparty_name,
            'counterparty_aba_masked': mask_participant(self.counterparty_aba),
            'uetr_masked': mask_uetr(self.uetr),
            'end_to_end_id_masked': mask_end_to_end_id(self.end_to_end_id),
            'status': self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


@dataclass
class InvCase:
    case_id: str
    enrollment_id: str
    userid: str
    nickname: str
    direction: str
    rail: str
    amount_usd: str
    account: str
    counterparty_name: str
    counterparty_aba: str
    uetr: str
    end_to_end_id: str
    receiver_routing: str
    network_status: str
    status: str
    reason: str
    message_id: str
    resolution_id: str
    actor: str
    releaser: str
    ofac_hit: bool
    ofac_match: str
    ofac_overridden: bool
    created_at: float
    updated_at: float
    settled_at: float = 0.0
    resolved_at: float = 0.0
    expiry_at: float = 0.0
    note: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'case_id': self.case_id,
            'enrollment_id': self.enrollment_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'direction': self.direction,
            'rail': self.rail,
            'amount_usd': self.amount_usd,
            'account_last4': mask_account(self.account),
            'counterparty_name': self.counterparty_name,
            'counterparty_aba_masked': mask_participant(self.counterparty_aba),
            'uetr_masked': mask_uetr(self.uetr),
            'end_to_end_id_masked': mask_end_to_end_id(self.end_to_end_id),
            'message_id_masked': mask_message_id(self.message_id),
            'network_status': self.network_status,
            'status': self.status,
            'reason': self.reason,
            'ofac_hit': self.ofac_hit,
            'recallable': self.status == INV_SETTLED,
            'acceptable': self.status in {INV_RECALL_IN, INV_PENDING_RELEASE},
            'declinable': self.status == INV_RECALL_IN,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'settled_at': self.settled_at,
            'expiry_at': self.expiry_at,
        }


def _enrollment_from_row(row: Any) -> InvEnrollment:
    data = dict(row)
    return InvEnrollment(
        enrollment_id=data['enrollment_id'], userid=data['userid'], nickname=data['nickname'],
        direction=data['direction'], rail=data['rail'], amount_usd=data['amount_usd'],
        account=data['account'], counterparty_name=data['counterparty_name'],
        counterparty_aba=data['counterparty_aba'], uetr=data['uetr'],
        end_to_end_id=data['end_to_end_id'], status=data['status'], actor=data['actor'],
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
    )


def _case_from_row(row: Any) -> InvCase:
    data = dict(row)
    return InvCase(
        case_id=data['case_id'], enrollment_id=data.get('enrollment_id') or '',
        userid=data['userid'], nickname=data.get('nickname') or '',
        direction=data['direction'], rail=data['rail'], amount_usd=data['amount_usd'],
        account=data.get('account') or '', counterparty_name=data['counterparty_name'],
        counterparty_aba=data['counterparty_aba'], uetr=data['uetr'],
        end_to_end_id=data['end_to_end_id'], receiver_routing=data['receiver_routing'],
        network_status=data.get('network_status') or '', status=data['status'],
        reason=data.get('reason') or '', message_id=data.get('message_id') or '',
        resolution_id=data.get('resolution_id') or '', actor=data['actor'],
        releaser=data.get('releaser') or '', ofac_hit=bool(int(data.get('ofac_hit') or 0)),
        ofac_match=data.get('ofac_match') or '', ofac_overridden=bool(int(data.get('ofac_overridden') or 0)),
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
        settled_at=float(data.get('settled_at') or 0), resolved_at=float(data.get('resolved_at') or 0),
        expiry_at=float(data.get('expiry_at') or 0), note=data.get('note') or '',
    )


class MemoryInvStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.enrollments: Dict[str, InvEnrollment] = {}
        self.cases: Dict[str, InvCase] = {}
        self._by_uetr: Dict[str, str] = {}
        self._by_e2e: Dict[str, str] = {}
        self._by_message: Dict[str, str] = {}
        self._enroll_uetr: Dict[str, str] = {}
        self._enroll_e2e: Dict[str, str] = {}
        self._enroll_nick: Dict[Tuple[str, str], str] = {}

    def put_enrollment(self, row: InvEnrollment) -> None:
        with self._lock:
            self.enrollments[row.enrollment_id] = row
            if row.uetr:
                self._enroll_uetr[row.uetr] = row.enrollment_id
            if row.end_to_end_id:
                self._enroll_e2e[row.end_to_end_id] = row.enrollment_id
            self._enroll_nick[(row.userid, row.nickname.lower())] = row.enrollment_id

    def get_enrollment(self, enrollment_id: str) -> Optional[InvEnrollment]:
        return self.enrollments.get(enrollment_id)

    def get_enrollment_by_uetr(self, uetr: str) -> Optional[InvEnrollment]:
        enrollment_id = self._enroll_uetr.get(uetr)
        return self.enrollments.get(enrollment_id) if enrollment_id else None

    def get_enrollment_by_e2e(self, end_to_end_id: str) -> Optional[InvEnrollment]:
        enrollment_id = self._enroll_e2e.get(end_to_end_id)
        return self.enrollments.get(enrollment_id) if enrollment_id else None

    def get_enrollment_by_nickname(self, userid: str, nickname: str) -> Optional[InvEnrollment]:
        enrollment_id = self._enroll_nick.get((userid, nickname.lower()))
        return self.enrollments.get(enrollment_id) if enrollment_id else None

    def update_enrollment(self, row: InvEnrollment) -> None:
        self.put_enrollment(row)

    def list_enrollments(self, userid: Optional[str] = None) -> List[InvEnrollment]:
        rows = list(self.enrollments.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        return sorted(rows, key=lambda row: row.created_at)

    def put_case(self, row: InvCase) -> None:
        with self._lock:
            self.cases[row.case_id] = row
            if row.uetr:
                self._by_uetr[row.uetr] = row.case_id
            if row.end_to_end_id:
                self._by_e2e[row.end_to_end_id] = row.case_id
            if row.message_id:
                self._by_message[row.message_id] = row.case_id

    def get_case(self, case_id: str) -> Optional[InvCase]:
        return self.cases.get(case_id)

    def get_by_uetr(self, uetr: str) -> Optional[InvCase]:
        case_id = self._by_uetr.get(uetr)
        return self.cases.get(case_id) if case_id else None

    def get_by_end_to_end_id(self, end_to_end_id: str) -> Optional[InvCase]:
        case_id = self._by_e2e.get(end_to_end_id)
        return self.cases.get(case_id) if case_id else None

    def get_by_message_id(self, message_id: str) -> Optional[InvCase]:
        case_id = self._by_message.get(message_id)
        return self.cases.get(case_id) if case_id else None

    def update_case(self, row: InvCase) -> None:
        self.put_case(row)

    def list_cases(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[InvCase]:
        rows = list(self.cases.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)

    def next_sequence(self) -> int:
        return len(self.cases) + len(self.enrollments) + 1


class SqliteInvStore:
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
                    direction TEXT NOT NULL,
                    rail TEXT NOT NULL,
                    amount_usd TEXT NOT NULL,
                    account TEXT NOT NULL,
                    counterparty_name TEXT NOT NULL,
                    counterparty_aba TEXT NOT NULL,
                    uetr TEXT NOT NULL,
                    end_to_end_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY,
                    enrollment_id TEXT NOT NULL DEFAULT '',
                    userid TEXT NOT NULL DEFAULT '',
                    nickname TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL,
                    rail TEXT NOT NULL,
                    amount_usd TEXT NOT NULL,
                    account TEXT NOT NULL DEFAULT '',
                    counterparty_name TEXT NOT NULL,
                    counterparty_aba TEXT NOT NULL,
                    uetr TEXT NOT NULL,
                    end_to_end_id TEXT NOT NULL,
                    receiver_routing TEXT NOT NULL,
                    network_status TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    message_id TEXT NOT NULL DEFAULT '',
                    resolution_id TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    ofac_overridden INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    settled_at REAL NOT NULL DEFAULT 0,
                    resolved_at REAL NOT NULL DEFAULT 0,
                    expiry_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_enrollment(self, row: InvEnrollment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO enrollments (
                    enrollment_id, userid, nickname, direction, rail, amount_usd, account,
                    counterparty_name, counterparty_aba, uetr, end_to_end_id, status, actor,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.enrollment_id, row.userid, row.nickname, row.direction, row.rail,
                    row.amount_usd, row.account, row.counterparty_name, row.counterparty_aba,
                    row.uetr, row.end_to_end_id, row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_enrollment(self, enrollment_id: str) -> Optional[InvEnrollment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM enrollments WHERE enrollment_id = ?', (enrollment_id,)).fetchone()
        return _enrollment_from_row(row) if row else None

    def get_enrollment_by_uetr(self, uetr: str) -> Optional[InvEnrollment]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM enrollments WHERE uetr = ? ORDER BY created_at DESC LIMIT 1', (uetr,),
            ).fetchone()
        return _enrollment_from_row(row) if row else None

    def get_enrollment_by_e2e(self, end_to_end_id: str) -> Optional[InvEnrollment]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM enrollments WHERE end_to_end_id = ? ORDER BY created_at DESC LIMIT 1',
                (end_to_end_id,),
            ).fetchone()
        return _enrollment_from_row(row) if row else None

    def get_enrollment_by_nickname(self, userid: str, nickname: str) -> Optional[InvEnrollment]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM enrollments WHERE userid = ? AND lower(nickname) = lower(?)',
                (userid, nickname),
            ).fetchone()
        return _enrollment_from_row(row) if row else None

    def update_enrollment(self, row: InvEnrollment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE enrollments SET nickname=?, direction=?, rail=?, amount_usd=?, account=?,
                    counterparty_name=?, counterparty_aba=?, uetr=?, end_to_end_id=?, status=?,
                    actor=?, updated_at=?
                WHERE enrollment_id=?
                """,
                (
                    row.nickname, row.direction, row.rail, row.amount_usd, row.account,
                    row.counterparty_name, row.counterparty_aba, row.uetr, row.end_to_end_id,
                    row.status, row.actor, row.updated_at, row.enrollment_id,
                ),
            )
            conn.commit()

    def list_enrollments(self, userid: Optional[str] = None) -> List[InvEnrollment]:
        sql = 'SELECT * FROM enrollments'
        args: Tuple[Any, ...] = ()
        if userid is not None:
            sql += ' WHERE userid = ?'
            args = (userid,)
        sql += ' ORDER BY created_at'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_enrollment_from_row(row) for row in rows]

    def put_case(self, row: InvCase) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cases (
                    case_id, enrollment_id, userid, nickname, direction, rail, amount_usd, account,
                    counterparty_name, counterparty_aba, uetr, end_to_end_id, receiver_routing,
                    network_status, status, reason, message_id, resolution_id, actor, releaser,
                    ofac_hit, ofac_match, ofac_overridden, created_at, updated_at, settled_at,
                    resolved_at, expiry_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.case_id, row.enrollment_id, row.userid, row.nickname, row.direction, row.rail,
                    row.amount_usd, row.account, row.counterparty_name, row.counterparty_aba, row.uetr,
                    row.end_to_end_id, row.receiver_routing, row.network_status, row.status, row.reason,
                    row.message_id, row.resolution_id, row.actor, row.releaser, int(row.ofac_hit),
                    row.ofac_match, int(row.ofac_overridden), row.created_at, row.updated_at,
                    row.settled_at, row.resolved_at, row.expiry_at, row.note,
                ),
            )
            conn.commit()

    def get_case(self, case_id: str) -> Optional[InvCase]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM cases WHERE case_id = ?', (case_id,)).fetchone()
        return _case_from_row(row) if row else None

    def get_by_uetr(self, uetr: str) -> Optional[InvCase]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM cases WHERE uetr = ? ORDER BY created_at DESC LIMIT 1', (uetr,),
            ).fetchone()
        return _case_from_row(row) if row else None

    def get_by_end_to_end_id(self, end_to_end_id: str) -> Optional[InvCase]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM cases WHERE end_to_end_id = ? ORDER BY created_at DESC LIMIT 1',
                (end_to_end_id,),
            ).fetchone()
        return _case_from_row(row) if row else None

    def get_by_message_id(self, message_id: str) -> Optional[InvCase]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM cases WHERE message_id = ? OR resolution_id = ? ORDER BY created_at DESC LIMIT 1',
                (message_id, message_id),
            ).fetchone()
        return _case_from_row(row) if row else None

    def update_case(self, row: InvCase) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE cases SET enrollment_id=?, userid=?, nickname=?, direction=?, rail=?,
                    amount_usd=?, account=?, counterparty_name=?, counterparty_aba=?, uetr=?,
                    end_to_end_id=?, receiver_routing=?, network_status=?, status=?, reason=?,
                    message_id=?, resolution_id=?, actor=?, releaser=?, ofac_hit=?, ofac_match=?,
                    ofac_overridden=?, updated_at=?, settled_at=?, resolved_at=?, expiry_at=?, note=?
                WHERE case_id=?
                """,
                (
                    row.enrollment_id, row.userid, row.nickname, row.direction, row.rail, row.amount_usd,
                    row.account, row.counterparty_name, row.counterparty_aba, row.uetr, row.end_to_end_id,
                    row.receiver_routing, row.network_status, row.status, row.reason, row.message_id,
                    row.resolution_id, row.actor, row.releaser, int(row.ofac_hit), row.ofac_match,
                    int(row.ofac_overridden), row.updated_at, row.settled_at, row.resolved_at,
                    row.expiry_at, row.note, row.case_id,
                ),
            )
            conn.commit()

    def list_cases(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[InvCase]:
        clauses = []
        args: List[Any] = []
        if userid is not None:
            clauses.append('userid = ?')
            args.append(userid)
        if status is not None:
            clauses.append('status = ?')
            args.append(status)
        sql = 'SELECT * FROM cases'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        return [_case_from_row(row) for row in rows]

    def next_sequence(self) -> int:
        with self._lock, self._connect() as conn:
            enroll = conn.execute('SELECT COUNT(*) FROM enrollments').fetchone()[0]
            cases = conn.execute('SELECT COUNT(*) FROM cases').fetchone()[0]
        return int(enroll) + int(cases) + 1


class InvService:
    def __init__(
        self,
        policy: InvPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        instant: Optional[InstantClock] = None,
        calendar: Optional[WireCalendar] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.instant = instant or InstantClock(
            tz_offset_hours=policy.tz_offset_hours, extra_holidays=policy.extra_holidays,
        )
        self.calendar = calendar or WireCalendar(
            cutoff_hour=policy.cutoff_hour,
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise InvError('inv_disabled', 'Payment investigations are disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise InvError('inv_forbidden', 'Customers cannot manage investigation enrollments.')

    def _require_recall(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_recall:
            raise InvError('inv_forbidden', 'Customers cannot request recalls.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise InvError('inv_forbidden', 'Staff only.')

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
            raise InvError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise InvError('credit_not_allowed', 'Credit accounts cannot host investigations.')

    def _assert_amount(self, dollars: Decimal, rail: str) -> None:
        if dollars < self.policy.min_amount or dollars > self.policy.cap_for(rail):
            if dollars > self.policy.cap_for(rail):
                code = {
                    RAIL_FEDNOW: 'fednow_amount_exceeded',
                    RAIL_RTP: 'rtp_amount_exceeded',
                    RAIL_WIRE: 'wire_amount_exceeded',
                }.get(rail, 'amount_out_of_range')
                raise InvError(code, 'Amount exceeds the rail cap.')
            raise InvError('amount_out_of_range', 'Amount is outside the allowed range.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def _clock_for(self, rail: str):
        return self.calendar if rail == RAIL_WIRE else self.instant

    def _recall_open(self, case: InvCase, now: float) -> bool:
        if case.settled_at <= 0:
            return False
        if case.rail == RAIL_WIRE:
            deadline = self.calendar.value_date(case.settled_at + 1)
            local = self.calendar.local_dt(now)
            if local.date() < deadline:
                return True
            if local.date() > deadline:
                return False
            return not self.calendar.is_after_cutoff(now)
        return now < case.settled_at + (self.policy.instant_recall_hours * 3600)

    def _move_money(self, *, account: str, amount: str, remark: str, credit: bool) -> str:
        fn = self.credit_fn if credit else self.debit_fn
        if fn is None:
            return 'failed'
        try:
            return _classify_money_result(fn(account, amount, remark))
        except TypeError:
            return _classify_money_result(fn(account, amount))

    def add_enrollment(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        direction: Any,
        rail: Any,
        amount: Any,
        account: Any,
        counterparty_name: Any,
        counterparty_aba: Any,
        uetr: Any = None,
        end_to_end_id: Any = None,
    ) -> InvEnrollment:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise InvError('inv_forbidden', 'Not allowed to enroll investigations for this customer.')
        name = normalize_nickname(nickname)
        rail_n = normalize_rail(rail)
        direction_n = normalize_direction(direction)
        dollars = parse_money(amount)
        self._assert_amount(dollars, rail_n)
        internal = normalize_account(account)
        self._assert_internal_account(owner_userid, internal)
        party = normalize_legal_name(counterparty_name)
        aba = normalize_participant(counterparty_aba)
        now = float(self.clock())
        cycle = self.instant.cycle_date(now)
        e2e = normalize_end_to_end_id(end_to_end_id, required=False) or compose_end_to_end_id(
            cycle, self.store.next_sequence(),
        )
        uetr_n = compose_uetr(uetr) if uetr else compose_uetr()
        if self.store.get_enrollment_by_nickname(owner_userid, name) is not None:
            raise InvError('enrollment_duplicate', 'An enrollment with that nickname already exists.')
        if self.store.get_enrollment_by_uetr(uetr_n) is not None or self.store.get_by_uetr(uetr_n) is not None:
            raise InvError('enrollment_duplicate', 'That UETR is already on file.')
        if self.store.get_enrollment_by_e2e(e2e) is not None or self.store.get_by_end_to_end_id(e2e) is not None:
            raise InvError('enrollment_duplicate', 'That EndToEndId is already on file.')
        open_rows = [row for row in self.store.list_enrollments(owner_userid) if row.status in OPEN_ENROLL]
        if len(open_rows) >= self.policy.max_enrollments:
            raise InvError('enrollment_limit', 'Investigation enrollment limit reached.')
        row = InvEnrollment(
            enrollment_id=uuid.uuid4().hex, userid=owner_userid, nickname=name, direction=direction_n,
            rail=rail_n, amount_usd=money_str(dollars), account=internal, counterparty_name=party,
            counterparty_aba=aba, uetr=uetr_n, end_to_end_id=e2e, status=ENROLL_ACTIVE, actor=str(actor),
            created_at=now, updated_at=now,
        )
        self.store.put_enrollment(row)
        return row

    def get_enrollment(self, *, enrollment_id: str, actor: str, actor_type: str) -> InvEnrollment:
        self._require_enabled()
        row = self.store.get_enrollment(enrollment_id)
        if row is None:
            raise InvError('enrollment_not_found', 'Investigation enrollment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise InvError('inv_forbidden', 'Not allowed to view this enrollment.')
        return row

    def set_enrollment_status(
        self,
        *,
        enrollment_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> InvEnrollment:
        self._require_manage(actor_type)
        row = self.get_enrollment(enrollment_id=enrollment_id, actor=actor, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': ENROLL_PAUSED, 'hold': ENROLL_PAUSED,
            'resume': ENROLL_ACTIVE, 'activate': ENROLL_ACTIVE, 'unpause': ENROLL_ACTIVE,
            'cancel': ENROLL_CANCELLED, 'close': ENROLL_CANCELLED, 'archive': ENROLL_CANCELLED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {ENROLL_PAUSED, ENROLL_ACTIVE, ENROLL_CANCELLED}:
            raise InvError('invalid_status', 'Status must be pause, resume, or cancel.')
        if row.status == ENROLL_CANCELLED:
            raise InvError('already_cancelled', 'Enrollment is already cancelled.')
        if wanted == ENROLL_PAUSED:
            if row.status == ENROLL_PAUSED:
                raise InvError('already_paused', 'Enrollment is already paused.')
            if row.status != ENROLL_ACTIVE:
                raise InvError('invalid_status', 'Only an active enrollment can be paused.')
        elif wanted == ENROLL_ACTIVE:
            if row.status == ENROLL_ACTIVE:
                raise InvError('already_active', 'Enrollment is already active.')
            if row.status != ENROLL_PAUSED:
                raise InvError('invalid_status', 'Only a paused enrollment can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_enrollment(row)
        if wanted == ENROLL_ACTIVE:
            self._rematch(row)
        return row

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        enrollment_id: Any,
    ) -> Dict[str, Any]:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise InvError('inv_forbidden', 'Not allowed to preview this enrollment.')
        row = self.get_enrollment(enrollment_id=str(enrollment_id), actor=actor, actor_type=actor_type)
        now = float(self.clock())
        clock = self._clock_for(row.rail)
        return {
            'enrollment': row.to_dict(),
            'clock': clock.snapshot(now),
            'dual_control': parse_money(row.amount_usd) >= self.policy.dual_control_threshold,
            'instant_recall_hours': self.policy.instant_recall_hours,
        }

    def _match_enrollment(self, uetr: str, end_to_end_id: str) -> Optional[InvEnrollment]:
        row = self.store.get_enrollment_by_uetr(uetr)
        if row is not None:
            return row
        return self.store.get_enrollment_by_e2e(end_to_end_id)

    def _match_case(self, uetr: str, end_to_end_id: str) -> Optional[InvCase]:
        row = self.store.get_by_uetr(uetr)
        if row is not None:
            return row
        return self.store.get_by_end_to_end_id(end_to_end_id)

    def _apply_network(self, case: InvCase, network_status: str, now: float) -> None:
        case.network_status = network_status
        mapped = NET_TO_STATUS[network_status]
        if case.status in {INV_HELD, INV_PENDING_RELEASE, INV_RECALL_OUT, INV_RECALL_IN, INV_RESOLVED_OK, INV_RESOLVED_NO}:
            if network_status == NET_ACSC and case.settled_at <= 0:
                case.settled_at = now
            return
        case.status = mapped
        if mapped == INV_SETTLED:
            case.settled_at = now
            if case.rail == RAIL_WIRE:
                deadline = self.calendar.value_date(now + 1)
                local = self.calendar.local_dt(now)
                close = datetime(deadline.year, deadline.month, deadline.day, self.policy.cutoff_hour, tzinfo=local.tzinfo)
                case.expiry_at = close.timestamp()
            else:
                case.expiry_at = now + (self.policy.instant_recall_hours * 3600)

    def ingest_pacs002(
        self,
        *,
        actor: str,
        actor_type: str,
        message: Dict[str, Any],
    ) -> Tuple[InvCase, bool]:
        self._require_staff(actor_type)
        existing = self.store.get_by_message_id(message['message_id'])
        if existing is not None:
            return existing, False
        if message['receiver_routing'] != self.policy.receiver_routing:
            raise InvError('wrong_receiver', 'pacs.002 is not addressed to this bank.')
        self._assert_amount(message['amount'], message['rail'])
        if len(self.store.list_cases()) >= self.policy.max_cases:
            raise InvError('case_limit', 'Investigation case limit reached.')
        now = float(self.clock())
        enrollment = self._match_enrollment(message['uetr'], message['end_to_end_id'])
        case = self._match_case(message['uetr'], message['end_to_end_id'])
        screen = self._screen(message['counterparty_name'])
        if case is None:
            userid = enrollment.userid if enrollment and enrollment.status == ENROLL_ACTIVE else ''
            account = enrollment.account if enrollment and enrollment.status == ENROLL_ACTIVE else ''
            direction = enrollment.direction if enrollment else message.get('direction') or DIR_OUT
            nickname = enrollment.nickname if enrollment else ''
            credit_blocked = False
            if enrollment and enrollment.status == ENROLL_ACTIVE:
                types = self._account_types(enrollment.userid)
                if types.get(enrollment.account) == 'credit' and not self.policy.allow_credit:
                    credit_blocked = True
                    userid = ''
                    account = ''
            status = INV_UNMATCHED
            if enrollment is None or enrollment.status != ENROLL_ACTIVE or credit_blocked:
                status = INV_UNMATCHED
            elif screen.hit:
                status = INV_HELD
            else:
                status = NET_TO_STATUS[message['network_status']]
            case = InvCase(
                case_id=uuid.uuid4().hex,
                enrollment_id=enrollment.enrollment_id if enrollment else '',
                userid=userid, nickname=nickname, direction=direction, rail=message['rail'],
                amount_usd=money_str(message['amount']), account=account,
                counterparty_name=message['counterparty_name'], counterparty_aba=message['counterparty_aba'],
                uetr=message['uetr'], end_to_end_id=message['end_to_end_id'],
                receiver_routing=message['receiver_routing'], network_status=message['network_status'],
                status=status, reason='', message_id=message['message_id'], resolution_id='',
                actor=str(actor), releaser='', ofac_hit=screen.hit, ofac_match=screen.matched,
                ofac_overridden=False, created_at=now, updated_at=now,
            )
            if credit_blocked:
                case.reason = 'credit_not_allowed'
            if status == INV_SETTLED:
                case.settled_at = now
                case.expiry_at = now + (self.policy.instant_recall_hours * 3600) if case.rail != RAIL_WIRE else 0.0
                if case.rail == RAIL_WIRE:
                    deadline = self.calendar.value_date(now + 1)
                    local = self.calendar.local_dt(now)
                    close = datetime(deadline.year, deadline.month, deadline.day, self.policy.cutoff_hour, tzinfo=local.tzinfo)
                    case.expiry_at = close.timestamp()
            elif status == INV_HELD:
                case.ofac_hit = True
            self.store.put_case(case)
            return case, True
        case.message_id = message['message_id']
        case.actor = str(actor)
        case.updated_at = now
        case.counterparty_name = message['counterparty_name']
        case.counterparty_aba = message['counterparty_aba']
        if screen.hit and not case.ofac_overridden:
            case.ofac_hit = True
            case.ofac_match = screen.matched
            case.status = INV_HELD
        else:
            self._apply_network(case, message['network_status'], now)
        self.store.update_case(case)
        return case, True

    def ingest_camt056(
        self,
        *,
        actor: str,
        actor_type: str,
        message: Dict[str, Any],
    ) -> Tuple[InvCase, bool]:
        self._require_staff(actor_type)
        existing = self.store.get_by_message_id(message['message_id'])
        if existing is not None:
            return existing, False
        if message['receiver_routing'] != self.policy.receiver_routing:
            raise InvError('wrong_receiver', 'camt.056 is not addressed to this bank.')
        self._assert_amount(message['amount'], message['rail'])
        now = float(self.clock())
        case = self._match_case(message['uetr'], message['end_to_end_id'])
        enrollment = self._match_enrollment(message['uetr'], message['end_to_end_id'])
        screen = self._screen(message['counterparty_name'])
        if case is None:
            userid = enrollment.userid if enrollment and enrollment.status == ENROLL_ACTIVE else ''
            account = enrollment.account if enrollment and enrollment.status == ENROLL_ACTIVE else ''
            direction = enrollment.direction if enrollment else DIR_IN
            nickname = enrollment.nickname if enrollment else ''
            status = INV_UNMATCHED if not userid else INV_RECALL_IN
            if screen.hit:
                status = INV_HELD if userid else INV_UNMATCHED
            elif userid and self._needs_dual_control(message['amount']):
                status = INV_PENDING_RELEASE
            case = InvCase(
                case_id=uuid.uuid4().hex,
                enrollment_id=enrollment.enrollment_id if enrollment else '',
                userid=userid, nickname=nickname, direction=direction, rail=message['rail'],
                amount_usd=money_str(message['amount']), account=account,
                counterparty_name=message['counterparty_name'], counterparty_aba=message['counterparty_aba'],
                uetr=message['uetr'], end_to_end_id=message['end_to_end_id'],
                receiver_routing=message['receiver_routing'], network_status=NET_ACSC,
                status=status, reason=message['reason'], message_id=message['message_id'],
                resolution_id='', actor=str(actor), releaser='', ofac_hit=screen.hit,
                ofac_match=screen.matched, ofac_overridden=False, created_at=now, updated_at=now,
                settled_at=now if status != INV_UNMATCHED else 0.0,
            )
            if userid and enrollment:
                types = self._account_types(userid)
                if types.get(account) == 'credit' and not self.policy.allow_credit:
                    case.status = INV_UNMATCHED
                    case.reason = 'credit_not_allowed'
            self.store.put_case(case)
            return case, True
        if case.status in {INV_RESOLVED_OK, INV_RESOLVED_NO, INV_EXPIRED, INV_FAILED}:
            raise InvError('not_recallable', 'Case cannot accept a new recall.')
        case.message_id = message['message_id']
        case.reason = message['reason']
        case.actor = str(actor)
        case.updated_at = now
        if screen.hit and not case.ofac_overridden:
            case.ofac_hit = True
            case.ofac_match = screen.matched
            case.status = INV_HELD
        elif self._needs_dual_control(parse_money(case.amount_usd)) and case.status != INV_PENDING_RELEASE:
            case.status = INV_PENDING_RELEASE
        else:
            case.status = INV_RECALL_IN
        if case.settled_at <= 0:
            case.settled_at = now
        self.store.update_case(case)
        return case, True

    def _rematch(self, enrollment: InvEnrollment) -> None:
        for case in self.store.list_cases(status=INV_UNMATCHED):
            if case.uetr != enrollment.uetr and case.end_to_end_id != enrollment.end_to_end_id:
                continue
            types = self._account_types(enrollment.userid)
            if types.get(enrollment.account) == 'credit' and not self.policy.allow_credit:
                case.reason = 'credit_not_allowed'
                self.store.update_case(case)
                continue
            case.userid = enrollment.userid
            case.account = enrollment.account
            case.enrollment_id = enrollment.enrollment_id
            case.nickname = enrollment.nickname
            case.direction = enrollment.direction
            now = float(self.clock())
            case.updated_at = now
            if case.ofac_hit and not case.ofac_overridden:
                case.status = INV_HELD
            elif case.message_id.startswith('CXL'):
                if self._needs_dual_control(parse_money(case.amount_usd)):
                    case.status = INV_PENDING_RELEASE
                else:
                    case.status = INV_RECALL_OUT if case.direction == DIR_OUT else INV_RECALL_IN
            elif case.network_status:
                self._apply_network(case, case.network_status, now)
            else:
                case.status = INV_OPEN
            self.store.update_case(case)

    def request_recall(
        self,
        *,
        case_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'CUST',
        note: Any = '',
    ) -> InvCase:
        self._require_recall(actor_type)
        case = self._get_case(case_id, actor, actor_type)
        if case.status != INV_SETTLED:
            raise InvError('not_recallable', 'Only a settled payment can be recalled.')
        now = float(self.clock())
        if not self._recall_open(case, now):
            raise InvError('return_window_closed', 'Recall window has closed.')
        case.reason = normalize_recall_reason(reason)
        case.note = normalize_note(note)
        case.actor = str(actor)
        case.updated_at = now
        seq = self.store.next_sequence()
        case.message_id = compose_message_id(KIND_CAMT056, self.instant.cycle_date(now), seq)
        if self._needs_dual_control(parse_money(case.amount_usd)):
            case.status = INV_PENDING_RELEASE
        else:
            case.status = INV_RECALL_OUT
        self.store.update_case(case)
        return case

    def accept_recall(
        self,
        *,
        case_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> InvCase:
        self._require_enabled()
        case = self._get_case(case_id, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES and case.status != INV_RECALL_IN:
            raise InvError('not_acceptable', 'Customer can only accept an inbound recall.')
        if case.status == INV_HELD and not case.ofac_overridden:
            raise InvError('ofac_hold', 'OFAC hold must be overridden first.')
        if case.status == INV_PENDING_RELEASE:
            raise InvError('not_acceptable', 'Pending dual-control release.')
        if case.status not in {INV_RECALL_IN, INV_RECALL_OUT}:
            raise InvError('not_acceptable', 'Case is not awaiting recall resolution.')
        now = float(self.clock())
        if case.status == INV_RECALL_IN and not self._recall_open(case, now) and case.settled_at:
            # inbound still resolvable by staff after customer window; customers blocked
            if actor_type not in EMPLOYEE_ROLES:
                raise InvError('return_window_closed', 'Recall window has closed.')
        credit = case.direction == DIR_OUT
        result = self._move_money(
            account=case.account, amount=case.amount_usd,
            remark='recall %s' % (case.uetr[-12:] if case.uetr else case.case_id[-12:]),
            credit=credit,
        )
        if result == 'nsf':
            raise InvError('nsf', 'Insufficient funds to return the payment.', case=case)
        if result != 'ok':
            case.status = INV_FAILED
            case.updated_at = now
            self.store.update_case(case)
            raise InvError('failed', 'Recall posting failed.', case=case)
        case.status = INV_RESOLVED_OK
        case.resolved_at = now
        case.updated_at = now
        case.note = normalize_note(note)
        if actor_type in EMPLOYEE_ROLES:
            case.releaser = str(actor)
        seq = self.store.next_sequence()
        case.resolution_id = compose_message_id(KIND_CAMT029, self.instant.cycle_date(now), seq)
        self.store.update_case(case)
        return case

    def decline_recall(
        self,
        *,
        case_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> InvCase:
        self._require_enabled()
        case = self._get_case(case_id, actor, actor_type)
        if case.status not in {INV_RECALL_IN, INV_RECALL_OUT, INV_PENDING_RELEASE}:
            raise InvError('not_declinable', 'Case is not awaiting recall resolution.')
        if actor_type not in EMPLOYEE_ROLES and case.status != INV_RECALL_IN:
            raise InvError('not_declinable', 'Customer can only decline an inbound recall.')
        now = float(self.clock())
        case.status = INV_RESOLVED_NO
        case.resolved_at = now
        case.updated_at = now
        case.note = normalize_note(note)
        seq = self.store.next_sequence()
        case.resolution_id = compose_message_id(KIND_CAMT029, self.instant.cycle_date(now), seq)
        self.store.update_case(case)
        return case

    def assign_case(
        self,
        *,
        case_id: str,
        actor: str,
        actor_type: str,
        owner_userid: str,
        account: Any,
        nickname: Any = '',
        direction: Any = None,
    ) -> InvCase:
        self._require_staff(actor_type)
        case = self.store.get_case(case_id)
        if case is None:
            raise InvError('case_not_found', 'Investigation case not found.')
        if case.status != INV_UNMATCHED:
            raise InvError('not_assignable', 'Only unmatched cases can be assigned.')
        internal = normalize_account(account)
        self._assert_internal_account(owner_userid, internal)
        now = float(self.clock())
        enrollment = None
        if nickname:
            try:
                enrollment = self.add_enrollment(
                    owner_userid=owner_userid, actor=actor, actor_type=actor_type,
                    nickname=nickname, direction=direction or case.direction, rail=case.rail,
                    amount=case.amount_usd, account=internal, counterparty_name=case.counterparty_name,
                    counterparty_aba=case.counterparty_aba, uetr=case.uetr, end_to_end_id=case.end_to_end_id,
                )
            except InvError as exc:
                if exc.code != 'enrollment_duplicate':
                    raise
                enrollment = self.store.get_enrollment_by_uetr(case.uetr)
        case.userid = owner_userid
        case.account = internal
        case.nickname = enrollment.nickname if enrollment else (normalize_nickname(nickname) if nickname else case.nickname)
        if direction:
            case.direction = normalize_direction(direction)
        case.enrollment_id = enrollment.enrollment_id if enrollment else case.enrollment_id
        case.updated_at = now
        if case.ofac_hit and not case.ofac_overridden:
            case.status = INV_HELD
        elif case.message_id.startswith('CXL'):
            if self._needs_dual_control(parse_money(case.amount_usd)):
                case.status = INV_PENDING_RELEASE
            else:
                case.status = INV_RECALL_OUT if case.direction == DIR_OUT else INV_RECALL_IN
        elif case.network_status:
            self._apply_network(case, case.network_status, now)
        else:
            case.status = INV_OPEN
        self.store.update_case(case)
        return case

    def override_ofac(self, *, case_id: str, actor: str, actor_type: str, note: Any = '') -> InvCase:
        self._require_staff(actor_type)
        case = self.store.get_case(case_id)
        if case is None:
            raise InvError('case_not_found', 'Investigation case not found.')
        if case.status != INV_HELD:
            raise InvError('not_overridable', 'Case is not on OFAC hold.')
        now = float(self.clock())
        case.ofac_overridden = True
        case.note = normalize_note(note)
        case.updated_at = now
        if case.status == INV_HELD:
            case.status = INV_OPEN
        if case.message_id.startswith('CXL'):
            if self._needs_dual_control(parse_money(case.amount_usd)):
                case.status = INV_PENDING_RELEASE
            else:
                case.status = INV_RECALL_OUT if case.direction == DIR_OUT else INV_RECALL_IN
        elif case.network_status:
            self._apply_network(case, case.network_status, now)
        else:
            case.status = INV_OPEN
        self.store.update_case(case)
        return case

    def release_case(self, *, case_id: str, actor: str, actor_type: str) -> InvCase:
        self._require_staff(actor_type)
        case = self.store.get_case(case_id)
        if case is None:
            raise InvError('case_not_found', 'Investigation case not found.')
        if case.status != INV_PENDING_RELEASE:
            raise InvError('not_releasable', 'Case is not pending release.')
        if case.actor and case.actor == str(actor):
            raise InvError('same_approver', 'A different employee must release this case.')
        now = float(self.clock())
        case.releaser = str(actor)
        case.updated_at = now
        if case.message_id.startswith('CXL'):
            case.status = INV_RECALL_OUT if case.direction == DIR_OUT else INV_RECALL_IN
        elif case.network_status:
            self._apply_network(case, case.network_status, now)
        else:
            case.status = INV_OPEN
        self.store.update_case(case)
        return case

    def reject_case(self, *, case_id: str, actor: str, actor_type: str, note: Any = '') -> InvCase:
        self._require_staff(actor_type)
        case = self.store.get_case(case_id)
        if case is None:
            raise InvError('case_not_found', 'Investigation case not found.')
        if case.status not in {INV_HELD, INV_PENDING_RELEASE, INV_UNMATCHED, INV_RECALL_IN, INV_RECALL_OUT, INV_OPEN}:
            raise InvError('not_rejectable', 'Case cannot be rejected.')
        now = float(self.clock())
        case.status = INV_REJECTED
        case.note = normalize_note(note)
        case.updated_at = now
        self.store.update_case(case)
        return case

    def export_case(self, *, case_id: str, actor: str, actor_type: str) -> Dict[str, Any]:
        self._require_staff(actor_type)
        case = self.store.get_case(case_id)
        if case is None:
            raise InvError('case_not_found', 'Investigation case not found.')
        if case.status not in {INV_RESOLVED_OK, INV_RESOLVED_NO, INV_SETTLED, INV_RECALL_OUT, INV_RECALL_IN}:
            raise InvError('not_exportable', 'Case has no exportable ISO 20022 message.')
        payload = {
            'pacs002': compose_pacs002(
                message_id=case.message_id if case.message_id.startswith('TXST') else compose_message_id(
                    KIND_PACS002, self.instant.cycle_date(float(self.clock())), self.store.next_sequence(),
                ),
                uetr=case.uetr, end_to_end_id=case.end_to_end_id,
                status=case.network_status or NET_ACSC, amount=parse_money(case.amount_usd),
                rail=case.rail, party_name=case.counterparty_name, party_aba=case.counterparty_aba,
                receiver=case.receiver_routing, cycle_date=self.instant.cycle_date(float(self.clock())),
            ) if case.network_status else '',
            'camt029': compose_camt029(
                case, resolution=RES_ACCR if case.status == INV_RESOLVED_OK else RES_RJCR,
            ) if case.status in {INV_RESOLVED_OK, INV_RESOLVED_NO} else '',
            'case': case.to_dict(),
        }
        return payload

    def run_due(self, userid: Optional[str] = None) -> None:
        now = float(self.clock())
        rows = self.store.list_cases(userid=userid) if userid else self.store.list_cases()
        for case in rows:
            if case.status not in {INV_SETTLED, INV_RECALL_OUT, INV_RECALL_IN, INV_PENDING_RELEASE}:
                continue
            if case.settled_at <= 0:
                continue
            if self._recall_open(case, now):
                continue
            if case.status == INV_SETTLED:
                continue
            if case.status in {INV_RECALL_OUT, INV_RECALL_IN, INV_PENDING_RELEASE}:
                case.status = INV_EXPIRED
                case.updated_at = now
                self.store.update_case(case)

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.run_due(userid)
        enrollments = self.store.list_enrollments(userid)
        cases = self.store.list_cases(userid)
        recalled = Decimal('0.00')
        settled = Decimal('0.00')
        for row in cases:
            amount = parse_money(row.amount_usd, allow_zero=True)
            if row.status == INV_RESOLVED_OK:
                recalled += amount
            if row.status in {INV_SETTLED, INV_RECALL_OUT, INV_RECALL_IN, INV_RESOLVED_OK, INV_RESOLVED_NO}:
                settled += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clock': self.instant.snapshot(now),
            'wire_clock': self.calendar.snapshot(now),
            'enrollments': [row.to_dict() for row in enrollments[:40]],
            'cases': [row.to_dict() for row in cases[:40]],
            'ytd_settled': money_str(settled),
            'ytd_recalled': money_str(recalled),
            'active_count': sum(1 for row in enrollments if row.status == ENROLL_ACTIVE),
            'open_count': sum(1 for row in cases if row.status in OPEN_INV),
        }

    def _get_case(self, case_id: str, actor: str, actor_type: str) -> InvCase:
        self._require_enabled()
        row = self.store.get_case(case_id)
        if row is None:
            raise InvError('case_not_found', 'Investigation case not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise InvError('inv_forbidden', 'Not allowed to view this case.')
        return row


_SERVICE: Optional[InvService] = None


def set_service(service: Optional[InvService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[InvService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('INV_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryInvStore()
    path = os.environ.get('INV_DB', DEFAULT_STORE_PATH)
    return SqliteInvStore(path)


def build_service(
    *,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    store: Any = None,
) -> InvService:
    policy = InvPolicy.from_env()
    return InvService(
        policy,
        store if store is not None else default_store(),
        debit_fn=debit_fn,
        credit_fn=credit_fn,
        accounts_fn=accounts_fn,
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
        'case_limit': 409,
        'already_cancelled': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_recalled': 409,
        'nsf': 409,
        'failed': 409,
        'inv_forbidden': 403,
        'inv_disabled': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_recallable': 403,
        'not_acceptable': 403,
        'not_declinable': 403,
        'not_exportable': 403,
        'return_window_closed': 403,
        'enrollment_not_found': 404,
        'case_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_routing': 400,
        'invalid_rail': 400,
        'invalid_currency': 400,
        'invalid_status': 400,
        'invalid_reason': 400,
        'invalid_file': 400,
        'invalid_reference': 400,
        'wrong_receiver': 400,
        'fednow_amount_exceeded': 400,
        'rtp_amount_exceeded': 400,
        'wire_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_enrollment': 400,
        'missing_case': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: InvError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('case') is not None:
        body['case'] = exc.extra['case'].to_dict()
    if exc.extra.get('enrollment') is not None:
        body['enrollment'] = exc.extra['enrollment'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except InvError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snapshot_for(service: InvService, userid: str, actor: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(userid, actor=actor, actor_type=actor_type)


def handle_list(service: InvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Investigations': _snapshot_for(service, owner, userid, actor_type)}), 200


def handle_unmatched(service: InvService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'inv_forbidden'}), 403
    rows = [row.to_dict() for row in service.store.list_cases(status=INV_UNMATCHED)]
    return jsonify({'unmatched': rows, 'Investigations': _snapshot_for(service, userid, userid, actor_type)}), 200


def handle_add_enrollment(service: InvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_from(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.add_enrollment(
            owner_userid=owner, actor=userid, actor_type=actor_type,
            nickname=values.get('nickname'),
            direction=values.get('direction') or DIR_OUT,
            rail=values.get('rail') or RAIL_FEDNOW,
            amount=values.get('amount') or values.get('amount_usd'),
            account=values.get('account') or values.get('default_account') or values.get('from_account'),
            counterparty_name=values.get('counterparty_name') or values.get('name') or values.get('legal_name'),
            counterparty_aba=values.get('counterparty_aba') or values.get('aba') or values.get('routing'),
            uetr=values.get('uetr'),
            end_to_end_id=values.get('end_to_end_id') or values.get('e2e'),
        )
        return jsonify({
            'message': 'Investigation enrollment added',
            'enrollment': row.to_dict(),
            'Investigations': _snapshot_for(service, owner, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _enrollment_status_route(service: InvService, status: str, message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    enrollment_id = str(values.get('enrollment_id') or '').strip()
    if not enrollment_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_enrollment'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_enrollment_status(
            enrollment_id=enrollment_id, actor=userid, actor_type=actor_type, status=status,
        )
        return jsonify({
            'message': message,
            'enrollment': row.to_dict(),
            'Investigations': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: InvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_from(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    enrollment_id = str(values.get('enrollment_id') or '').strip()
    if not enrollment_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_enrollment'}), 400

    def _run():
        preview = service.preview(
            owner_userid=owner, actor=userid, actor_type=actor_type, enrollment_id=enrollment_id,
        )
        return jsonify({'preview': preview, 'Investigations': _snapshot_for(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest_pacs002(service: InvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        case, created = service.ingest_pacs002(
            actor=userid, actor_type=actor_type, message=message_from_pacs002_values(values),
        )
        owner = case.userid or userid
        return jsonify({
            'message': 'pacs.002 ingested' if created else 'pacs.002 already posted',
            'case': case.to_dict(),
            'Investigations': _snapshot_for(service, owner, userid, actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_pacs002_file(service: InvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    payload = values.get('file') or values.get('payload') or values.get('content')
    if not payload:
        return jsonify({'message': 'Some data missing', 'error': 'missing_file'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        created_any = False
        last = None
        for record in split_inv_file(str(payload), header='PACS002'):
            last, created = service.ingest_pacs002(
                actor=userid, actor_type=actor_type, message=parse_pacs002(record),
            )
            created_any = created_any or created
        owner = last.userid if last and last.userid else userid
        return jsonify({
            'message': 'pacs.002 file ingested',
            'case': last.to_dict() if last else {},
            'Investigations': _snapshot_for(service, owner, userid, actor_type),
        }), 201 if created_any else 200

    return _handle_errors(_run)


def handle_ingest_camt056(service: InvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        case, created = service.ingest_camt056(
            actor=userid, actor_type=actor_type, message=message_from_camt056_values(values),
        )
        owner = case.userid or userid
        return jsonify({
            'message': 'camt.056 ingested' if created else 'camt.056 already posted',
            'case': case.to_dict(),
            'Investigations': _snapshot_for(service, owner, userid, actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_camt056_file(service: InvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    payload = values.get('file') or values.get('payload') or values.get('content')
    if not payload:
        return jsonify({'message': 'Some data missing', 'error': 'missing_file'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        created_any = False
        last = None
        for record in split_inv_file(str(payload), header='CAMT056'):
            last, created = service.ingest_camt056(
                actor=userid, actor_type=actor_type, message=parse_camt056(record),
            )
            created_any = created_any or created
        owner = last.userid if last and last.userid else userid
        return jsonify({
            'message': 'camt.056 file ingested',
            'case': last.to_dict() if last else {},
            'Investigations': _snapshot_for(service, owner, userid, actor_type),
        }), 201 if created_any else 200

    return _handle_errors(_run)


def _case_action_route(service: InvService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    case_id = str(values.get('case_id') or values.get('investigation_id') or '').strip()
    if not case_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_case'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'request':
            case = service.request_recall(
                case_id=case_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'CUST', note=values.get('note') or '',
            )
            message = 'Recall requested'
        elif action == 'accept':
            case = service.accept_recall(
                case_id=case_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Recall accepted'
        elif action == 'decline':
            case = service.decline_recall(
                case_id=case_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Recall declined'
        elif action == 'assign':
            owner = str(values.get('customer_id') or values.get('owner') or '').strip()
            if not owner:
                raise InvError('missing_customer_id', 'Customer id required.')
            case = service.assign_case(
                case_id=case_id, actor=userid, actor_type=actor_type, owner_userid=owner,
                account=values.get('account') or values.get('default_account'),
                nickname=values.get('nickname') or '',
                direction=values.get('direction'),
            )
            message = 'Investigation assigned'
        elif action == 'override':
            case = service.override_ofac(
                case_id=case_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'release':
            case = service.release_case(case_id=case_id, actor=userid, actor_type=actor_type)
            message = 'Investigation released'
        elif action == 'reject':
            case = service.reject_case(
                case_id=case_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Investigation rejected'
        elif action == 'export':
            payload = service.export_case(case_id=case_id, actor=userid, actor_type=actor_type)
            owner = payload['case'].get('userid') or userid
            return jsonify({
                'message': 'Investigation exported',
                'export': payload,
                'Investigations': _snapshot_for(service, owner, userid, actor_type),
            }), 200
        else:
            raise InvError('invalid_status', 'Unknown investigation action.')
        return jsonify({
            'message': message,
            'case': case.to_dict(),
            'Investigations': _snapshot_for(service, case.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: InvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Investigations': _snapshot_for(service, owner, userid, actor_type)}), 200


def attach_inv_routes(app, service: InvService) -> None:
    @app.route('/listInvs', methods=['POST', 'GET'])
    def list_invs_route():
        return handle_list(service)

    @app.route('/listInvEnrollments', methods=['POST', 'GET'])
    def list_inv_enrollments_route():
        return handle_list(service)

    @app.route('/listUnmatchedInvs', methods=['POST', 'GET'])
    def list_unmatched_invs_route():
        return handle_unmatched(service)

    @app.route('/enrollInv', methods=['POST', 'GET'])
    def enroll_inv_route():
        return handle_add_enrollment(service)

    @app.route('/pauseInvEnrollment', methods=['POST', 'GET'])
    def pause_inv_enrollment_route():
        return _enrollment_status_route(service, ENROLL_PAUSED, 'Investigation enrollment paused')

    @app.route('/resumeInvEnrollment', methods=['POST', 'GET'])
    def resume_inv_enrollment_route():
        return _enrollment_status_route(service, ENROLL_ACTIVE, 'Investigation enrollment resumed')

    @app.route('/cancelInvEnrollment', methods=['POST', 'GET'])
    def cancel_inv_enrollment_route():
        return _enrollment_status_route(service, ENROLL_CANCELLED, 'Investigation enrollment cancelled')

    @app.route('/previewInv', methods=['POST', 'GET'])
    def preview_inv_route():
        return handle_preview(service)

    @app.route('/ingestPacs002', methods=['POST', 'GET'])
    def ingest_pacs002_route():
        return handle_ingest_pacs002(service)

    @app.route('/ingestPacs002File', methods=['POST', 'GET'])
    def ingest_pacs002_file_route():
        return handle_ingest_pacs002_file(service)

    @app.route('/ingestCamt056', methods=['POST', 'GET'])
    def ingest_camt056_route():
        return handle_ingest_camt056(service)

    @app.route('/ingestCamt056File', methods=['POST', 'GET'])
    def ingest_camt056_file_route():
        return handle_ingest_camt056_file(service)

    @app.route('/requestRecall', methods=['POST', 'GET'])
    def request_recall_route():
        return _case_action_route(service, 'request')

    @app.route('/acceptRecall', methods=['POST', 'GET'])
    def accept_recall_route():
        return _case_action_route(service, 'accept')

    @app.route('/declineRecall', methods=['POST', 'GET'])
    def decline_recall_route():
        return _case_action_route(service, 'decline')

    @app.route('/assignInv', methods=['POST', 'GET'])
    def assign_inv_route():
        return _case_action_route(service, 'assign')

    @app.route('/overrideInvOfac', methods=['POST', 'GET'])
    def override_inv_ofac_route():
        return _case_action_route(service, 'override')

    @app.route('/releaseInv', methods=['POST', 'GET'])
    def release_inv_route():
        return _case_action_route(service, 'release')

    @app.route('/rejectInv', methods=['POST', 'GET'])
    def reject_inv_route():
        return _case_action_route(service, 'reject')

    @app.route('/exportInv', methods=['POST', 'GET'])
    def export_inv_route():
        return _case_action_route(service, 'export')

    @app.route('/runDueInvs', methods=['POST', 'GET'])
    def run_due_invs_route():
        return handle_run_due(service)
