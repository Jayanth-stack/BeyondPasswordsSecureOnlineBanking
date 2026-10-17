"""SWIFT gpi tracker and MT199 investigation.

Staff ingest gpi tracker confirmations (ACCC/ACSC/ACSP/PDNG/RJCT/CANC) and
MT199 investigation queries. Customers enroll a payment by UETR, request an
investigation, and accept or decline inbound queries. Resolutions compose
MT199 replies (CNFM/UNAB). Independent of inbound SWIFT MT103 (PR #103),
outbound SWIFT MT103 (PR #78), ISO 20022 pacs.002 / camt.056 (PR #129),
CHIPS (PR #131/#133), ACH linking (PR #68), and domestic Fedwire (PR #73).
Existing `/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- SWIFT FIN MT199 / gpi tracker pipe compose/parse (XML/DOCTYPE rejected)
- UETR (RFC 4122 UUID) uniqueness and TRN (GPI + YYYYMMDD + seq)
- BIC ISO 9362 (8→11 pad XXX; location cannot start with 0)
- Instant 24/7 clock for gpi; correspondent MT uses TARGET2 (16:00 ET)
- Dual-control at $10k on money-moving investigation resolution, not on tracker ingest
- 24h gpi query window; MT queries close next TARGET2 business day
- Reusable OFAC screen via utility.wire

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Full UETR, TRN, BIC, and destination accounts never appear in to_dict / snapshots.
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
    account_types_from_customer_payload,
    last4,
    money_str,
    normalize_account,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
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
RAIL_GPI = 'gpi'
RAIL_MT = 'mt'
RAILS = frozenset({RAIL_GPI, RAIL_MT})
RAIL_ALIASES = {
    'gpi': RAIL_GPI, 'swiftgpi': RAIL_GPI, 'swift_gpi': RAIL_GPI, 'gcct': RAIL_GPI,
    'mt': RAIL_MT, 'fin': RAIL_MT, 'correspondent': RAIL_MT, 'swift': RAIL_MT, 'mt103': RAIL_MT,
}
NET_ACCC = 'ACCC'
NET_ACSC = 'ACSC'
NET_ACSP = 'ACSP'
NET_PDNG = 'PDNG'
NET_RJCT = 'RJCT'
NET_CANC = 'CANC'
NETWORK_STATUSES = frozenset({NET_ACCC, NET_ACSC, NET_ACSP, NET_PDNG, NET_RJCT, NET_CANC})
NET_ALIASES = {
    'accc': NET_ACCC, 'credited': NET_ACCC, 'credit_completed': NET_ACCC, 'accepted': NET_ACCC,
    'acsc': NET_ACSC, 'settled': NET_ACSC, 'settlement': NET_ACSC, 'completed': NET_ACSC,
    'acsp': NET_ACSP, 'in_process': NET_ACSP, 'forwarded': NET_ACSP,
    'pdng': NET_PDNG, 'pending': NET_PDNG,
    'rjct': NET_RJCT, 'rejected': NET_RJCT, 'reject': NET_RJCT,
    'canc': NET_CANC, 'cancelled': NET_CANC, 'canceled': NET_CANC,
}
KIND_TRACKER = 'gpi'
KIND_MT199 = 'mt199'
KIND_REPLY = 'mt199r'
MESSAGE_KINDS = frozenset({KIND_TRACKER, KIND_MT199, KIND_REPLY})
RES_CNFM = 'CNFM'
RES_UNAB = 'UNAB'
RESOLUTIONS = frozenset({RES_CNFM, RES_UNAB})
RES_ALIASES = {
    'cnfm': RES_CNFM, 'confirm': RES_CNFM, 'confirmed': RES_CNFM, 'accept': RES_CNFM, 'accepted': RES_CNFM, 'agree': RES_CNFM, 'yes': RES_CNFM,
    'unab': RES_UNAB, 'unable': RES_UNAB, 'reject': RES_UNAB, 'rejected': RES_UNAB, 'decline': RES_UNAB, 'refuse': RES_UNAB, 'no': RES_UNAB,
}
QUERY_REASONS = frozenset({'MSID', 'AMNT', 'DUPL', 'NARR', 'FROZ', 'DATE', 'BENF'})
QUERY_ALIASES = {
    'msid': 'MSID', 'missing': 'MSID', 'unknown': 'MSID',
    'amnt': 'AMNT', 'amount': 'AMNT', 'wrong_amount': 'AMNT',
    'dupl': 'DUPL', 'duplicate': 'DUPL', 'dup': 'DUPL',
    'narr': 'NARR', 'narrative': 'NARR', 'query': 'NARR', 'cust': 'NARR', 'customer': 'NARR',
    'froz': 'FROZ', 'frozen': 'FROZ', 'ofac': 'FROZ',
    'date': 'DATE', 'value_date': 'DATE',
    'benf': 'BENF', 'beneficiary': 'BENF', 'wrong_account': 'BENF',
}
GPI_UNMATCHED = 'unmatched'
GPI_HELD = 'held'
GPI_OPEN = 'open'
GPI_CREDITED = 'credited'
GPI_SETTLED = 'settled'
GPI_PENDING = 'pending'
GPI_REJECTED = 'rejected'
GPI_CANCELLED = 'cancelled'
GPI_PENDING_RELEASE = 'pending_release'
GPI_QUERY_OUT = 'query_requested'
GPI_QUERY_IN = 'query_inbound'
GPI_RESOLVED_OK = 'resolved_accepted'
GPI_RESOLVED_NO = 'resolved_rejected'
GPI_EXPIRED = 'expired'
GPI_FAILED = 'failed'
GPI_STATUSES = frozenset({
    GPI_UNMATCHED, GPI_HELD, GPI_OPEN, GPI_CREDITED, GPI_SETTLED, GPI_PENDING,
    GPI_REJECTED, GPI_CANCELLED, GPI_PENDING_RELEASE, GPI_QUERY_OUT, GPI_QUERY_IN,
    GPI_RESOLVED_OK, GPI_RESOLVED_NO, GPI_EXPIRED, GPI_FAILED,
})
OPEN_GPI = frozenset({
    GPI_UNMATCHED, GPI_HELD, GPI_OPEN, GPI_CREDITED, GPI_SETTLED, GPI_PENDING,
    GPI_PENDING_RELEASE, GPI_QUERY_OUT, GPI_QUERY_IN,
})
NET_TO_STATUS = {
    NET_ACCC: GPI_CREDITED,
    NET_ACSC: GPI_SETTLED,
    NET_ACSP: GPI_PENDING,
    NET_PDNG: GPI_PENDING,
    NET_RJCT: GPI_REJECTED,
    NET_CANC: GPI_CANCELLED,
}
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/gpi.sqlite'
DEFAULT_RECEIVER = 'KNHAUS33XXX'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)
TRN_RE = re.compile(r'^GPI\d{14}$')
UETR_RE = re.compile(r'^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$')
MSG_RE = re.compile(r'^(TRCK|INQY|N199)\d{14}$')
BIC_RE = re.compile(r'^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}[A-Z0-9]{3}$')


class GpiError(ValueError):
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


def easter_gregorian(year: int) -> date:
    """Anonymous Gregorian computus. TARGET2 holidays hang off this date."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def target2_holidays(year: int) -> set:
    """TARGET2 closing days (unobserved). Weekends are handled separately."""
    easter = easter_gregorian(year)
    return {
        date(year, 1, 1),
        easter - timedelta(days=2),
        easter + timedelta(days=1),
        date(year, 5, 1),
        date(year, 12, 25),
        date(year, 12, 26),
    }


def normalize_bic(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        if required:
            raise GpiError('invalid_bic', 'BIC required.')
        return ''
    if len(text) == 8:
        text += 'XXX'
    if len(text) != 11 or not BIC_RE.match(text) or text[6] == '0':
        raise GpiError('invalid_bic', 'BIC failed ISO 9362 checks.')
    return text


def mask_bic(value: str) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if len(text) < 8:
        return '****'
    return text[:4] + '****' + text[-3:]


def format_bic(value: Any) -> str:
    return normalize_bic(value)


def bic8(value: Any) -> str:
    return normalize_bic(value)[:8]


def normalize_rail(value: Any, *, default: str = RAIL_GPI) -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text not in RAILS:
        raise GpiError('invalid_rail', 'Rail must be gpi or mt.')
    return text


def normalize_direction(value: Any, *, default: str = DIR_OUT) -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = DIR_ALIASES.get(text, text)
    if text not in DIRECTIONS:
        raise GpiError('invalid_status', 'Direction must be outbound or inbound.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise GpiError('invalid_name', 'Counterparty legal name must be 2-80 characters.')
    return text


def normalize_nickname(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise GpiError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_currency(value: Any) -> str:
    text = str(value or 'USD').strip().upper()
    if text != 'USD':
        raise GpiError('invalid_currency', 'Gpi must be USD.')
    return text


def normalize_network_status(value: Any) -> str:
    text = str(value or '').strip().upper().replace('-', '_').replace(' ', '_')
    text = NET_ALIASES.get(text.lower(), text)
    if text not in NETWORK_STATUSES:
        raise GpiError('invalid_status', 'Unknown gpi tracker status.')
    return text


def normalize_resolution(value: Any) -> str:
    text = str(value or '').strip().upper().replace('-', '_').replace(' ', '_')
    text = RES_ALIASES.get(text.lower(), text)
    if text not in RESOLUTIONS:
        raise GpiError('invalid_status', 'Resolution must be CNFM or UNAB.')
    return text


def normalize_query_reason(value: Any, *, default: str = 'NARR') -> str:
    text = str(value or default).strip().upper().replace('-', '_').replace(' ', '_')
    text = QUERY_ALIASES.get(text.lower(), text)
    if text not in QUERY_REASONS:
        raise GpiError('invalid_reason', 'Unknown MT199 investigation reason.')
    return text


def compose_trn(cycle_date: str, sequence: int) -> str:
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise GpiError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise GpiError('invalid_reference', 'Gpi sequence out of range.')
    return 'GPI%s%06d' % (day, seq)


def normalize_trn(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        if required:
            raise GpiError('invalid_reference', 'TRN required.')
        return ''
    if not TRN_RE.match(text):
        raise GpiError('invalid_reference', 'TRN must be GPI + 14 digits.')
    return text


def mask_trn(value: str) -> str:
    text = str(value or '')
    if len(text) < 8:
        return 'GPI********'
    return text[:6] + '*****' + text[-4:]


def compose_uetr(value: Any = None) -> str:
    if value:
        return normalize_uetr(value)
    return str(uuid.uuid4()).upper()


def normalize_uetr(value: Any, *, required: bool = True) -> str:
    text = str(value or '').strip().upper()
    if not text:
        if required:
            raise GpiError('invalid_reference', 'UETR required.')
        return ''
    if not UETR_RE.match(text):
        raise GpiError('invalid_reference', 'UETR must be an RFC 4122 UUID.')
    return text


def mask_uetr(value: str) -> str:
    text = str(value or '')
    if len(text) < 12:
        return '********'
    return text[:8] + '***' + text[-4:]


def compose_message_id(kind: str, cycle_date: str, sequence: int) -> str:
    prefixes = {KIND_TRACKER: 'TRCK', KIND_MT199: 'INQY', KIND_REPLY: 'N199'}
    prefix = prefixes.get(kind)
    if prefix is None:
        raise GpiError('invalid_file', 'Unknown SWIFT gpi message kind.')
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise GpiError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise GpiError('invalid_reference', 'Message sequence out of range.')
    return '%s%s%06d' % (prefix, day, seq)


def normalize_message_id(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        if required:
            raise GpiError('invalid_reference', 'Message id required.')
        return ''
    if not MSG_RE.match(text):
        raise GpiError('invalid_reference', 'Message id must be TRCK/INQY/N199 + 14 digits.')
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
    """TARGET2 holiday calendar. gpi is 24/7; snapshot still reports holidays."""

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
        return day in target2_holidays(day.year) or day in self.extra_holidays

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


class Target2Calendar:
    """TARGET2 business-day + 16:00 ET cutoff. Injectable offset and holidays."""

    def __init__(
        self,
        *,
        cutoff_hour: int = 16,
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
            extra.add(date.fromisoformat(text[:10]) if '-' in text else datetime.strptime(text, '%Y%m%d').date())
        self.extra_holidays = extra

    def local_dt(self, ts: float) -> datetime:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(self.tz)

    def is_weekend(self, day: date) -> bool:
        return day.weekday() >= 5

    def is_holiday(self, day: date) -> bool:
        return day in target2_holidays(day.year) or day in self.extra_holidays

    def is_business_day(self, day: date) -> bool:
        return not self.is_weekend(day) and not self.is_holiday(day)

    def is_after_cutoff(self, ts: float) -> bool:
        local = self.local_dt(ts)
        return (local.hour, local.minute) >= (self.cutoff_hour, self.cutoff_minute)

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


def _reject_xml(text: str) -> None:
    low = text.lower()
    if (
        '<?xml' in low or '<!doctype' in low or '<!entity' in low
        or '<pacs' in low or '<camt' in low or '<mt' in low or '{1:' in low
    ):
        raise GpiError('invalid_file', 'XML/DOCTYPE SWIFT files are rejected.')


def compose_gpi(
    *,
    message_id: str,
    uetr: str,
    trn: str,
    status: str,
    amount: Decimal,
    rail: str,
    party_name: str,
    party_bic: str,
    receiver: str = DEFAULT_RECEIVER,
    cycle_date: str = '',
) -> str:
    fields = [
        'GPI1',
        normalize_message_id(message_id),
        normalize_uetr(uetr),
        normalize_trn(trn),
        normalize_network_status(status),
        money_str(amount),
        'USD',
        normalize_rail(rail),
        normalize_legal_name(party_name).replace('|', ' '),
        normalize_bic(party_bic),
        normalize_bic(receiver),
        str(cycle_date or ''),
    ]
    return '|'.join(fields)


def parse_gpi(record: str) -> Dict[str, Any]:
    text = str(record or '').strip()
    _reject_xml(text)
    if not text or text.startswith('#'):
        raise GpiError('invalid_file', 'Empty GPI1 record.')
    parts = text.split('|')
    if len(parts) != 12 or parts[0] != 'GPI1':
        raise GpiError('invalid_file', 'Inbound GPI1 records must have 12 pipe fields.')
    return {
        'message_id': normalize_message_id(parts[1]),
        'uetr': normalize_uetr(parts[2]),
        'trn': normalize_trn(parts[3]),
        'network_status': normalize_network_status(parts[4]),
        'amount': parse_money(parts[5]),
        'currency': normalize_currency(parts[6]),
        'rail': normalize_rail(parts[7]),
        'counterparty_name': normalize_legal_name(parts[8]),
        'counterparty_bic': normalize_bic(parts[9]),
        'receiver_bic': normalize_bic(parts[10]),
        'cycle_date': str(parts[11] or '').strip(),
        'kind': KIND_TRACKER,
    }


def compose_mt199(
    *,
    message_id: str,
    uetr: str,
    trn: str,
    amount: Decimal,
    rail: str,
    reason: str,
    party_name: str,
    party_bic: str,
    receiver: str = DEFAULT_RECEIVER,
) -> str:
    fields = [
        'MT199',
        normalize_message_id(message_id),
        normalize_uetr(uetr),
        normalize_trn(trn),
        money_str(amount),
        'USD',
        normalize_rail(rail),
        normalize_query_reason(reason),
        normalize_legal_name(party_name).replace('|', ' '),
        normalize_bic(party_bic),
        normalize_bic(receiver),
    ]
    return '|'.join(fields)


def parse_mt199(record: str) -> Dict[str, Any]:
    text = str(record or '').strip()
    _reject_xml(text)
    if not text or text.startswith('#'):
        raise GpiError('invalid_file', 'Empty MT199 record.')
    parts = text.split('|')
    if len(parts) != 11 or parts[0] != 'MT199':
        raise GpiError('invalid_file', 'Inbound MT199 records must have 11 pipe fields.')
    return {
        'message_id': normalize_message_id(parts[1]),
        'uetr': normalize_uetr(parts[2]),
        'trn': normalize_trn(parts[3]),
        'amount': parse_money(parts[4]),
        'currency': normalize_currency(parts[5]),
        'rail': normalize_rail(parts[6]),
        'reason': normalize_query_reason(parts[7]),
        'counterparty_name': normalize_legal_name(parts[8]),
        'counterparty_bic': normalize_bic(parts[9]),
        'receiver_bic': normalize_bic(parts[10]),
        'kind': KIND_MT199,
    }


def compose_mt199_reply(row: 'GpiCase', *, resolution: str = RES_CNFM) -> str:
    return '|'.join([
        'MT199R',
        row.resolution_id or mask_message_id(row.message_id),
        mask_uetr(row.uetr),
        mask_trn(row.trn),
        normalize_resolution(resolution),
        row.reason or '',
        row.rail,
        mask_bic(row.receiver_bic),
    ])


def split_gpi_file(payload: str, *, header: str) -> List[str]:
    text = str(payload or '')
    _reject_xml(text)
    records = []
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        records.append(stripped)
    if not records:
        raise GpiError('invalid_file', 'Gpi file had no %s records.' % header)
    return records


def message_from_gpi_values(values: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'message_id': normalize_message_id(
            values.get('message_id') or values.get('msg_id') or values.get('scheme_id'),
        ),
        'uetr': normalize_uetr(values.get('uetr') or values.get('uetr_id')),
        'trn': normalize_trn(
            values.get('trn') or values.get('e2e') or values.get('reference'),
        ),
        'network_status': normalize_network_status(
            values.get('network_status') or values.get('status') or values.get('txn_status'),
        ),
        'amount': parse_money(values.get('amount') or values.get('amount_usd')),
        'currency': normalize_currency(values.get('currency') or 'USD'),
        'rail': normalize_rail(values.get('rail') or RAIL_GPI),
        'counterparty_name': normalize_legal_name(
            values.get('counterparty_name') or values.get('party_name') or values.get('name'),
        ),
        'counterparty_bic': normalize_bic(
            values.get('counterparty_bic') or values.get('bic') or values.get('routing'),
        ),
        'receiver_bic': normalize_bic(
            values.get('receiver_bic') or values.get('receiver') or DEFAULT_RECEIVER,
        ),
        'cycle_date': str(values.get('cycle_date') or values.get('value_date') or '').strip(),
        'kind': KIND_TRACKER,
        'direction': normalize_direction(values.get('direction') or DIR_OUT, default=DIR_OUT),
    }


def message_from_mt199_values(values: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'message_id': normalize_message_id(
            values.get('message_id') or values.get('msg_id') or values.get('case_id'),
        ),
        'uetr': normalize_uetr(values.get('uetr') or values.get('uetr_id')),
        'trn': normalize_trn(
            values.get('trn') or values.get('e2e') or values.get('reference'),
        ),
        'amount': parse_money(values.get('amount') or values.get('amount_usd')),
        'currency': normalize_currency(values.get('currency') or 'USD'),
        'rail': normalize_rail(values.get('rail') or RAIL_GPI),
        'reason': normalize_query_reason(values.get('reason') or values.get('query_reason') or 'NARR'),
        'counterparty_name': normalize_legal_name(
            values.get('counterparty_name') or values.get('party_name') or values.get('name'),
        ),
        'counterparty_bic': normalize_bic(
            values.get('counterparty_bic') or values.get('bic') or values.get('routing'),
        ),
        'receiver_bic': normalize_bic(
            values.get('receiver_bic') or values.get('receiver') or DEFAULT_RECEIVER,
        ),
        'kind': KIND_MT199,
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
class GpiPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_query: bool = True
    allow_credit: bool = False
    max_enrollments: int = 12
    max_cases: int = 120
    min_amount: Decimal = Decimal('0.01')
    gpi_cap: Decimal = Decimal('10000000.00')
    mt_cap: Decimal = Decimal('50000000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    query_hours: int = 24
    tz_offset_hours: int = -4
    cutoff_hour: int = 16
    receiver_bic: str = DEFAULT_RECEIVER
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'GpiPolicy':
        extra = _env_list('GPI_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        routing = os.environ.get('GPI_RECEIVER') or DEFAULT_RECEIVER
        return cls(
            enabled=_env_bool('GPI_ENABLED', True),
            customer_manage=_env_bool('GPI_CUSTOMER_MANAGE', True),
            customer_query=_env_bool('GPI_CUSTOMER_QUERY', True),
            allow_credit=_env_bool('GPI_ALLOW_CREDIT', False),
            max_enrollments=max(1, _env_int('GPI_MAX_ENROLLMENTS', 12)),
            max_cases=max(1, _env_int('GPI_MAX_CASES', 120)),
            min_amount=_env_money('GPI_MIN_AMOUNT', '0.01'),
            gpi_cap=_env_money('GPI_GPI_CAP', '10000000.00'),
            mt_cap=_env_money('GPI_MT_CAP', '50000000.00'),
            dual_control_threshold=_env_money('GPI_DUAL_CONTROL', '10000.00'),
            query_hours=max(1, _env_int('GPI_QUERY_HOURS', 24)),
            tz_offset_hours=_env_int('GPI_TZ_OFFSET', -4),
            cutoff_hour=_env_int('GPI_CUTOFF_HOUR', 17),
            receiver_bic=normalize_bic(routing),
            watchlist=watch,
            extra_holidays=_env_list('GPI_HOLIDAYS'),
        )

    def cap_for(self, rail: str) -> Decimal:
        if rail == RAIL_GPI:
            return self.gpi_cap
        if rail == RAIL_MT:
            return self.mt_cap
        return self.gpi_cap


@dataclass
class GpiEnrollment:
    enrollment_id: str
    userid: str
    nickname: str
    direction: str
    rail: str
    amount_usd: str
    account: str
    counterparty_name: str
    counterparty_bic: str
    uetr: str
    trn: str
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
            'counterparty_bic_masked': mask_bic(self.counterparty_bic),
            'uetr_masked': mask_uetr(self.uetr),
            'trn_masked': mask_trn(self.trn),
            'status': self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


@dataclass
class GpiCase:
    case_id: str
    enrollment_id: str
    userid: str
    nickname: str
    direction: str
    rail: str
    amount_usd: str
    account: str
    counterparty_name: str
    counterparty_bic: str
    uetr: str
    trn: str
    receiver_bic: str
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
            'counterparty_bic_masked': mask_bic(self.counterparty_bic),
            'uetr_masked': mask_uetr(self.uetr),
            'trn_masked': mask_trn(self.trn),
            'message_id_masked': mask_message_id(self.message_id),
            'network_status': self.network_status,
            'status': self.status,
            'reason': self.reason,
            'ofac_hit': self.ofac_hit,
            'queryable': self.status in {GPI_SETTLED, GPI_CREDITED},
            'acceptable': self.status in {GPI_QUERY_IN, GPI_PENDING_RELEASE},
            'declinable': self.status == GPI_QUERY_IN,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'settled_at': self.settled_at,
            'expiry_at': self.expiry_at,
        }


def _enrollment_from_row(row: Any) -> GpiEnrollment:
    data = dict(row)
    return GpiEnrollment(
        enrollment_id=data['enrollment_id'], userid=data['userid'], nickname=data['nickname'],
        direction=data['direction'], rail=data['rail'], amount_usd=data['amount_usd'],
        account=data['account'], counterparty_name=data['counterparty_name'],
        counterparty_bic=data['counterparty_bic'], uetr=data['uetr'],
        trn=data['trn'], status=data['status'], actor=data['actor'],
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
    )


def _case_from_row(row: Any) -> GpiCase:
    data = dict(row)
    return GpiCase(
        case_id=data['case_id'], enrollment_id=data.get('enrollment_id') or '',
        userid=data['userid'], nickname=data.get('nickname') or '',
        direction=data['direction'], rail=data['rail'], amount_usd=data['amount_usd'],
        account=data.get('account') or '', counterparty_name=data['counterparty_name'],
        counterparty_bic=data['counterparty_bic'], uetr=data['uetr'],
        trn=data['trn'], receiver_bic=data['receiver_bic'],
        network_status=data.get('network_status') or '', status=data['status'],
        reason=data.get('reason') or '', message_id=data.get('message_id') or '',
        resolution_id=data.get('resolution_id') or '', actor=data['actor'],
        releaser=data.get('releaser') or '', ofac_hit=bool(int(data.get('ofac_hit') or 0)),
        ofac_match=data.get('ofac_match') or '', ofac_overridden=bool(int(data.get('ofac_overridden') or 0)),
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
        settled_at=float(data.get('settled_at') or 0), resolved_at=float(data.get('resolved_at') or 0),
        expiry_at=float(data.get('expiry_at') or 0), note=data.get('note') or '',
    )


class MemoryGpiStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.enrollments: Dict[str, GpiEnrollment] = {}
        self.cases: Dict[str, GpiCase] = {}
        self._by_uetr: Dict[str, str] = {}
        self._by_trn: Dict[str, str] = {}
        self._by_message: Dict[str, str] = {}
        self._enroll_uetr: Dict[str, str] = {}
        self._enroll_trn: Dict[str, str] = {}
        self._enroll_nick: Dict[Tuple[str, str], str] = {}

    def put_enrollment(self, row: GpiEnrollment) -> None:
        with self._lock:
            self.enrollments[row.enrollment_id] = row
            if row.uetr:
                self._enroll_uetr[row.uetr] = row.enrollment_id
            if row.trn:
                self._enroll_trn[row.trn] = row.enrollment_id
            self._enroll_nick[(row.userid, row.nickname.lower())] = row.enrollment_id

    def get_enrollment(self, enrollment_id: str) -> Optional[GpiEnrollment]:
        return self.enrollments.get(enrollment_id)

    def get_enrollment_by_uetr(self, uetr: str) -> Optional[GpiEnrollment]:
        enrollment_id = self._enroll_uetr.get(uetr)
        return self.enrollments.get(enrollment_id) if enrollment_id else None

    def get_enrollment_by_trn(self, trn: str) -> Optional[GpiEnrollment]:
        enrollment_id = self._enroll_trn.get(trn)
        return self.enrollments.get(enrollment_id) if enrollment_id else None

    def get_enrollment_by_nickname(self, userid: str, nickname: str) -> Optional[GpiEnrollment]:
        enrollment_id = self._enroll_nick.get((userid, nickname.lower()))
        return self.enrollments.get(enrollment_id) if enrollment_id else None

    def update_enrollment(self, row: GpiEnrollment) -> None:
        self.put_enrollment(row)

    def list_enrollments(self, userid: Optional[str] = None) -> List[GpiEnrollment]:
        rows = list(self.enrollments.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        return sorted(rows, key=lambda row: row.created_at)

    def put_case(self, row: GpiCase) -> None:
        with self._lock:
            self.cases[row.case_id] = row
            if row.uetr:
                self._by_uetr[row.uetr] = row.case_id
            if row.trn:
                self._by_trn[row.trn] = row.case_id
            if row.message_id:
                self._by_message[row.message_id] = row.case_id

    def get_case(self, case_id: str) -> Optional[GpiCase]:
        return self.cases.get(case_id)

    def get_by_uetr(self, uetr: str) -> Optional[GpiCase]:
        case_id = self._by_uetr.get(uetr)
        return self.cases.get(case_id) if case_id else None

    def get_by_trn(self, trn: str) -> Optional[GpiCase]:
        case_id = self._by_trn.get(trn)
        return self.cases.get(case_id) if case_id else None

    def get_by_message_id(self, message_id: str) -> Optional[GpiCase]:
        case_id = self._by_message.get(message_id)
        return self.cases.get(case_id) if case_id else None

    def update_case(self, row: GpiCase) -> None:
        self.put_case(row)

    def list_cases(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[GpiCase]:
        rows = list(self.cases.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)

    def next_sequence(self) -> int:
        return len(self.cases) + len(self.enrollments) + 1


class SqliteGpiStore:
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
                    counterparty_bic TEXT NOT NULL,
                    uetr TEXT NOT NULL,
                    trn TEXT NOT NULL,
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
                    counterparty_bic TEXT NOT NULL,
                    uetr TEXT NOT NULL,
                    trn TEXT NOT NULL,
                    receiver_bic TEXT NOT NULL,
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

    def put_enrollment(self, row: GpiEnrollment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO enrollments (
                    enrollment_id, userid, nickname, direction, rail, amount_usd, account,
                    counterparty_name, counterparty_bic, uetr, trn, status, actor,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.enrollment_id, row.userid, row.nickname, row.direction, row.rail,
                    row.amount_usd, row.account, row.counterparty_name, row.counterparty_bic,
                    row.uetr, row.trn, row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_enrollment(self, enrollment_id: str) -> Optional[GpiEnrollment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM enrollments WHERE enrollment_id = ?', (enrollment_id,)).fetchone()
        return _enrollment_from_row(row) if row else None

    def get_enrollment_by_uetr(self, uetr: str) -> Optional[GpiEnrollment]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM enrollments WHERE uetr = ? ORDER BY created_at DESC LIMIT 1', (uetr,),
            ).fetchone()
        return _enrollment_from_row(row) if row else None

    def get_enrollment_by_trn(self, trn: str) -> Optional[GpiEnrollment]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM enrollments WHERE trn = ? ORDER BY created_at DESC LIMIT 1',
                (trn,),
            ).fetchone()
        return _enrollment_from_row(row) if row else None

    def get_enrollment_by_nickname(self, userid: str, nickname: str) -> Optional[GpiEnrollment]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM enrollments WHERE userid = ? AND lower(nickname) = lower(?)',
                (userid, nickname),
            ).fetchone()
        return _enrollment_from_row(row) if row else None

    def update_enrollment(self, row: GpiEnrollment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE enrollments SET nickname=?, direction=?, rail=?, amount_usd=?, account=?,
                    counterparty_name=?, counterparty_bic=?, uetr=?, trn=?, status=?,
                    actor=?, updated_at=?
                WHERE enrollment_id=?
                """,
                (
                    row.nickname, row.direction, row.rail, row.amount_usd, row.account,
                    row.counterparty_name, row.counterparty_bic, row.uetr, row.trn,
                    row.status, row.actor, row.updated_at, row.enrollment_id,
                ),
            )
            conn.commit()

    def list_enrollments(self, userid: Optional[str] = None) -> List[GpiEnrollment]:
        sql = 'SELECT * FROM enrollments'
        args: Tuple[Any, ...] = ()
        if userid is not None:
            sql += ' WHERE userid = ?'
            args = (userid,)
        sql += ' ORDER BY created_at'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_enrollment_from_row(row) for row in rows]

    def put_case(self, row: GpiCase) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cases (
                    case_id, enrollment_id, userid, nickname, direction, rail, amount_usd, account,
                    counterparty_name, counterparty_bic, uetr, trn, receiver_bic,
                    network_status, status, reason, message_id, resolution_id, actor, releaser,
                    ofac_hit, ofac_match, ofac_overridden, created_at, updated_at, settled_at,
                    resolved_at, expiry_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.case_id, row.enrollment_id, row.userid, row.nickname, row.direction, row.rail,
                    row.amount_usd, row.account, row.counterparty_name, row.counterparty_bic, row.uetr,
                    row.trn, row.receiver_bic, row.network_status, row.status, row.reason,
                    row.message_id, row.resolution_id, row.actor, row.releaser, int(row.ofac_hit),
                    row.ofac_match, int(row.ofac_overridden), row.created_at, row.updated_at,
                    row.settled_at, row.resolved_at, row.expiry_at, row.note,
                ),
            )
            conn.commit()

    def get_case(self, case_id: str) -> Optional[GpiCase]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM cases WHERE case_id = ?', (case_id,)).fetchone()
        return _case_from_row(row) if row else None

    def get_by_uetr(self, uetr: str) -> Optional[GpiCase]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM cases WHERE uetr = ? ORDER BY created_at DESC LIMIT 1', (uetr,),
            ).fetchone()
        return _case_from_row(row) if row else None

    def get_by_trn(self, trn: str) -> Optional[GpiCase]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM cases WHERE trn = ? ORDER BY created_at DESC LIMIT 1',
                (trn,),
            ).fetchone()
        return _case_from_row(row) if row else None

    def get_by_message_id(self, message_id: str) -> Optional[GpiCase]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM cases WHERE message_id = ? OR resolution_id = ? ORDER BY created_at DESC LIMIT 1',
                (message_id, message_id),
            ).fetchone()
        return _case_from_row(row) if row else None

    def update_case(self, row: GpiCase) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE cases SET enrollment_id=?, userid=?, nickname=?, direction=?, rail=?,
                    amount_usd=?, account=?, counterparty_name=?, counterparty_bic=?, uetr=?,
                    trn=?, receiver_bic=?, network_status=?, status=?, reason=?,
                    message_id=?, resolution_id=?, actor=?, releaser=?, ofac_hit=?, ofac_match=?,
                    ofac_overridden=?, updated_at=?, settled_at=?, resolved_at=?, expiry_at=?, note=?
                WHERE case_id=?
                """,
                (
                    row.enrollment_id, row.userid, row.nickname, row.direction, row.rail, row.amount_usd,
                    row.account, row.counterparty_name, row.counterparty_bic, row.uetr, row.trn,
                    row.receiver_bic, row.network_status, row.status, row.reason, row.message_id,
                    row.resolution_id, row.actor, row.releaser, int(row.ofac_hit), row.ofac_match,
                    int(row.ofac_overridden), row.updated_at, row.settled_at, row.resolved_at,
                    row.expiry_at, row.note, row.case_id,
                ),
            )
            conn.commit()

    def list_cases(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[GpiCase]:
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


class GpiService:
    def __init__(
        self,
        policy: GpiPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        instant: Optional[InstantClock] = None,
        calendar: Optional[Target2Calendar] = None,
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
        self.calendar = calendar or Target2Calendar(
            cutoff_hour=policy.cutoff_hour,
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise GpiError('gpi_disabled', 'SWIFT gpi tracking is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise GpiError('gpi_forbidden', 'Customers cannot manage gpi enrollments.')

    def _require_query(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_query:
            raise GpiError('gpi_forbidden', 'Customers cannot request gpi investigations.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise GpiError('gpi_forbidden', 'Staff only.')

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
            raise GpiError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise GpiError('credit_not_allowed', 'Credit accounts cannot host gpi tracking.')

    def _assert_amount(self, dollars: Decimal, rail: str) -> None:
        if dollars < self.policy.min_amount or dollars > self.policy.cap_for(rail):
            if dollars > self.policy.cap_for(rail):
                code = {
                    RAIL_GPI: 'gpi_amount_exceeded',
                    RAIL_MT: 'mt_amount_exceeded',
                }.get(rail, 'amount_out_of_range')
                raise GpiError(code, 'Amount exceeds the rail cap.')
            raise GpiError('amount_out_of_range', 'Amount is outside the allowed range.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def _clock_for(self, rail: str):
        return self.calendar if rail == RAIL_MT else self.instant

    def _query_open(self, case: GpiCase, now: float) -> bool:
        if case.settled_at <= 0:
            return False
        if case.rail == RAIL_MT:
            deadline = self.calendar.value_date(case.settled_at + 1)
            local = self.calendar.local_dt(now)
            if local.date() < deadline:
                return True
            if local.date() > deadline:
                return False
            return not self.calendar.is_after_cutoff(now)
        return now < case.settled_at + (self.policy.query_hours * 3600)

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
        counterparty_bic: Any,
        uetr: Any = None,
        trn: Any = None,
    ) -> GpiEnrollment:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise GpiError('gpi_forbidden', 'Not allowed to enroll gpi tracking for this customer.')
        name = normalize_nickname(nickname)
        rail_n = normalize_rail(rail)
        direction_n = normalize_direction(direction)
        dollars = parse_money(amount)
        self._assert_amount(dollars, rail_n)
        internal = normalize_account(account)
        self._assert_internal_account(owner_userid, internal)
        party = normalize_legal_name(counterparty_name)
        aba = normalize_bic(counterparty_bic)
        now = float(self.clock())
        cycle = self.instant.cycle_date(now)
        e2e = normalize_trn(trn, required=False) or compose_trn(
            cycle, self.store.next_sequence(),
        )
        uetr_n = compose_uetr(uetr) if uetr else compose_uetr()
        if self.store.get_enrollment_by_nickname(owner_userid, name) is not None:
            raise GpiError('enrollment_duplicate', 'An enrollment with that nickname already exists.')
        if self.store.get_enrollment_by_uetr(uetr_n) is not None or self.store.get_by_uetr(uetr_n) is not None:
            raise GpiError('enrollment_duplicate', 'That UETR is already on file.')
        if self.store.get_enrollment_by_trn(e2e) is not None or self.store.get_by_trn(e2e) is not None:
            raise GpiError('enrollment_duplicate', 'That TRN is already on file.')
        open_rows = [row for row in self.store.list_enrollments(owner_userid) if row.status in OPEN_ENROLL]
        if len(open_rows) >= self.policy.max_enrollments:
            raise GpiError('enrollment_limit', 'Gpi enrollment limit reached.')
        row = GpiEnrollment(
            enrollment_id=uuid.uuid4().hex, userid=owner_userid, nickname=name, direction=direction_n,
            rail=rail_n, amount_usd=money_str(dollars), account=internal, counterparty_name=party,
            counterparty_bic=aba, uetr=uetr_n, trn=e2e, status=ENROLL_ACTIVE, actor=str(actor),
            created_at=now, updated_at=now,
        )
        self.store.put_enrollment(row)
        return row

    def get_enrollment(self, *, enrollment_id: str, actor: str, actor_type: str) -> GpiEnrollment:
        self._require_enabled()
        row = self.store.get_enrollment(enrollment_id)
        if row is None:
            raise GpiError('enrollment_not_found', 'Gpi enrollment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise GpiError('gpi_forbidden', 'Not allowed to view this gpi enrollment.')
        return row

    def set_enrollment_status(
        self,
        *,
        enrollment_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> GpiEnrollment:
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
            raise GpiError('invalid_status', 'Status must be pause, resume, or cancel.')
        if row.status == ENROLL_CANCELLED:
            raise GpiError('already_cancelled', 'Enrollment is already cancelled.')
        if wanted == ENROLL_PAUSED:
            if row.status == ENROLL_PAUSED:
                raise GpiError('already_paused', 'Enrollment is already paused.')
            if row.status != ENROLL_ACTIVE:
                raise GpiError('invalid_status', 'Only an active enrollment can be paused.')
        elif wanted == ENROLL_ACTIVE:
            if row.status == ENROLL_ACTIVE:
                raise GpiError('already_active', 'Enrollment is already active.')
            if row.status != ENROLL_PAUSED:
                raise GpiError('invalid_status', 'Only a paused enrollment can be resumed.')
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
            raise GpiError('gpi_forbidden', 'Not allowed to preview this gpi enrollment.')
        row = self.get_enrollment(enrollment_id=str(enrollment_id), actor=actor, actor_type=actor_type)
        now = float(self.clock())
        clock = self._clock_for(row.rail)
        return {
            'enrollment': row.to_dict(),
            'clock': clock.snapshot(now),
            'dual_control': parse_money(row.amount_usd) >= self.policy.dual_control_threshold,
            'query_hours': self.policy.query_hours,
        }

    def _match_enrollment(self, uetr: str, trn: str) -> Optional[GpiEnrollment]:
        row = self.store.get_enrollment_by_uetr(uetr)
        if row is not None:
            return row
        return self.store.get_enrollment_by_trn(trn)

    def _match_case(self, uetr: str, trn: str) -> Optional[GpiCase]:
        row = self.store.get_by_uetr(uetr)
        if row is not None:
            return row
        return self.store.get_by_trn(trn)

    def _apply_network(self, case: GpiCase, network_status: str, now: float) -> None:
        case.network_status = network_status
        mapped = NET_TO_STATUS[network_status]
        if case.status in {GPI_HELD, GPI_PENDING_RELEASE, GPI_QUERY_OUT, GPI_QUERY_IN, GPI_RESOLVED_OK, GPI_RESOLVED_NO}:
            if network_status == NET_ACSC and case.settled_at <= 0:
                case.settled_at = now
            return
        case.status = mapped
        if mapped in {GPI_SETTLED, GPI_CREDITED}:
            case.settled_at = now
            if case.rail == RAIL_MT:
                deadline = self.calendar.value_date(now + 1)
                local = self.calendar.local_dt(now)
                close = datetime(deadline.year, deadline.month, deadline.day, self.policy.cutoff_hour, tzinfo=local.tzinfo)
                case.expiry_at = close.timestamp()
            else:
                case.expiry_at = now + (self.policy.query_hours * 3600)

    def ingest_gpi(
        self,
        *,
        actor: str,
        actor_type: str,
        message: Dict[str, Any],
    ) -> Tuple[GpiCase, bool]:
        self._require_staff(actor_type)
        existing = self.store.get_by_message_id(message['message_id'])
        if existing is not None:
            return existing, False
        if message['receiver_bic'] != self.policy.receiver_bic:
            raise GpiError('wrong_receiver', 'gpi tracker is not addressed to this bank.')
        self._assert_amount(message['amount'], message['rail'])
        if len(self.store.list_cases()) >= self.policy.max_cases:
            raise GpiError('case_limit', 'Gpi case limit reached.')
        now = float(self.clock())
        enrollment = self._match_enrollment(message['uetr'], message['trn'])
        case = self._match_case(message['uetr'], message['trn'])
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
            status = GPI_UNMATCHED
            if enrollment is None or enrollment.status != ENROLL_ACTIVE or credit_blocked:
                status = GPI_UNMATCHED
            elif screen.hit:
                status = GPI_HELD
            else:
                status = NET_TO_STATUS[message['network_status']]
            case = GpiCase(
                case_id=uuid.uuid4().hex,
                enrollment_id=enrollment.enrollment_id if enrollment else '',
                userid=userid, nickname=nickname, direction=direction, rail=message['rail'],
                amount_usd=money_str(message['amount']), account=account,
                counterparty_name=message['counterparty_name'], counterparty_bic=message['counterparty_bic'],
                uetr=message['uetr'], trn=message['trn'],
                receiver_bic=message['receiver_bic'], network_status=message['network_status'],
                status=status, reason='', message_id=message['message_id'], resolution_id='',
                actor=str(actor), releaser='', ofac_hit=screen.hit, ofac_match=screen.matched,
                ofac_overridden=False, created_at=now, updated_at=now,
            )
            if credit_blocked:
                case.reason = 'credit_not_allowed'
            if status in {GPI_SETTLED, GPI_CREDITED}:
                case.settled_at = now
                case.expiry_at = now + (self.policy.query_hours * 3600) if case.rail != RAIL_MT else 0.0
                if case.rail == RAIL_MT:
                    deadline = self.calendar.value_date(now + 1)
                    local = self.calendar.local_dt(now)
                    close = datetime(deadline.year, deadline.month, deadline.day, self.policy.cutoff_hour, tzinfo=local.tzinfo)
                    case.expiry_at = close.timestamp()
            elif status == GPI_HELD:
                case.ofac_hit = True
            self.store.put_case(case)
            return case, True
        case.message_id = message['message_id']
        case.actor = str(actor)
        case.updated_at = now
        case.counterparty_name = message['counterparty_name']
        case.counterparty_bic = message['counterparty_bic']
        if screen.hit and not case.ofac_overridden:
            case.ofac_hit = True
            case.ofac_match = screen.matched
            case.status = GPI_HELD
        else:
            self._apply_network(case, message['network_status'], now)
        self.store.update_case(case)
        return case, True

    def ingest_mt199(
        self,
        *,
        actor: str,
        actor_type: str,
        message: Dict[str, Any],
    ) -> Tuple[GpiCase, bool]:
        self._require_staff(actor_type)
        existing = self.store.get_by_message_id(message['message_id'])
        if existing is not None:
            return existing, False
        if message['receiver_bic'] != self.policy.receiver_bic:
            raise GpiError('wrong_receiver', 'MT199 is not addressed to this bank.')
        self._assert_amount(message['amount'], message['rail'])
        now = float(self.clock())
        case = self._match_case(message['uetr'], message['trn'])
        enrollment = self._match_enrollment(message['uetr'], message['trn'])
        screen = self._screen(message['counterparty_name'])
        if case is None:
            userid = enrollment.userid if enrollment and enrollment.status == ENROLL_ACTIVE else ''
            account = enrollment.account if enrollment and enrollment.status == ENROLL_ACTIVE else ''
            direction = enrollment.direction if enrollment else DIR_IN
            nickname = enrollment.nickname if enrollment else ''
            status = GPI_UNMATCHED if not userid else GPI_QUERY_IN
            if screen.hit:
                status = GPI_HELD if userid else GPI_UNMATCHED
            elif userid and self._needs_dual_control(message['amount']):
                status = GPI_PENDING_RELEASE
            case = GpiCase(
                case_id=uuid.uuid4().hex,
                enrollment_id=enrollment.enrollment_id if enrollment else '',
                userid=userid, nickname=nickname, direction=direction, rail=message['rail'],
                amount_usd=money_str(message['amount']), account=account,
                counterparty_name=message['counterparty_name'], counterparty_bic=message['counterparty_bic'],
                uetr=message['uetr'], trn=message['trn'],
                receiver_bic=message['receiver_bic'], network_status=NET_ACSC,
                status=status, reason=message['reason'], message_id=message['message_id'],
                resolution_id='', actor=str(actor), releaser='', ofac_hit=screen.hit,
                ofac_match=screen.matched, ofac_overridden=False, created_at=now, updated_at=now,
                settled_at=now if status != GPI_UNMATCHED else 0.0,
            )
            if userid and enrollment:
                types = self._account_types(userid)
                if types.get(account) == 'credit' and not self.policy.allow_credit:
                    case.status = GPI_UNMATCHED
                    case.reason = 'credit_not_allowed'
            self.store.put_case(case)
            return case, True
        if case.status in {GPI_RESOLVED_OK, GPI_RESOLVED_NO, GPI_EXPIRED, GPI_FAILED}:
            raise GpiError('not_queryable', 'Case cannot accept a new MT199 query.')
        case.message_id = message['message_id']
        case.reason = message['reason']
        case.actor = str(actor)
        case.updated_at = now
        if screen.hit and not case.ofac_overridden:
            case.ofac_hit = True
            case.ofac_match = screen.matched
            case.status = GPI_HELD
        elif self._needs_dual_control(parse_money(case.amount_usd)) and case.status != GPI_PENDING_RELEASE:
            case.status = GPI_PENDING_RELEASE
        else:
            case.status = GPI_QUERY_IN
        if case.settled_at <= 0:
            case.settled_at = now
        self.store.update_case(case)
        return case, True

    def _rematch(self, enrollment: GpiEnrollment) -> None:
        for case in self.store.list_cases(status=GPI_UNMATCHED):
            if case.uetr != enrollment.uetr and case.trn != enrollment.trn:
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
                case.status = GPI_HELD
            elif case.message_id.startswith('INQY'):
                if self._needs_dual_control(parse_money(case.amount_usd)):
                    case.status = GPI_PENDING_RELEASE
                else:
                    case.status = GPI_QUERY_OUT if case.direction == DIR_OUT else GPI_QUERY_IN
            elif case.network_status:
                self._apply_network(case, case.network_status, now)
            else:
                case.status = GPI_OPEN
            self.store.update_case(case)

    def request_query(
        self,
        *,
        case_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'NARR',
        note: Any = '',
    ) -> GpiCase:
        self._require_query(actor_type)
        case = self._get_case(case_id, actor, actor_type)
        if case.status not in {GPI_SETTLED, GPI_CREDITED}:
            raise GpiError('not_queryable', 'Only a credited or settled payment can be queried.')
        now = float(self.clock())
        if not self._query_open(case, now):
            raise GpiError('return_window_closed', 'Query window has closed.')
        case.reason = normalize_query_reason(reason)
        case.note = normalize_note(note)
        case.actor = str(actor)
        case.updated_at = now
        seq = self.store.next_sequence()
        case.message_id = compose_message_id(KIND_MT199, self.instant.cycle_date(now), seq)
        if self._needs_dual_control(parse_money(case.amount_usd)):
            case.status = GPI_PENDING_RELEASE
        else:
            case.status = GPI_QUERY_OUT
        self.store.update_case(case)
        return case

    def accept_query(
        self,
        *,
        case_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> GpiCase:
        self._require_enabled()
        case = self._get_case(case_id, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES and case.status != GPI_QUERY_IN:
            raise GpiError('not_acceptable', 'Customer can only accept an inbound MT199.')
        if case.status == GPI_HELD and not case.ofac_overridden:
            raise GpiError('ofac_hold', 'OFAC hold must be overridden first.')
        if case.status == GPI_PENDING_RELEASE:
            raise GpiError('not_acceptable', 'Pending dual-control release.')
        if case.status not in {GPI_QUERY_IN, GPI_QUERY_OUT}:
            raise GpiError('not_acceptable', 'Case is not awaiting gpi query resolution.')
        now = float(self.clock())
        if case.status == GPI_QUERY_IN and not self._query_open(case, now) and case.settled_at:
            # inbound still resolvable by staff after customer window; customers blocked
            if actor_type not in EMPLOYEE_ROLES:
                raise GpiError('return_window_closed', 'Query window has closed.')
        credit = case.direction == DIR_OUT
        result = self._move_money(
            account=case.account, amount=case.amount_usd,
            remark='gpi query %s' % (case.uetr[-12:] if case.uetr else case.case_id[-12:]),
            credit=credit,
        )
        if result == 'nsf':
            raise GpiError('nsf', 'Insufficient funds to return the payment.', case=case)
        if result != 'ok':
            case.status = GPI_FAILED
            case.updated_at = now
            self.store.update_case(case)
            raise GpiError('failed', 'Query posting failed.', case=case)
        case.status = GPI_RESOLVED_OK
        case.resolved_at = now
        case.updated_at = now
        case.note = normalize_note(note)
        if actor_type in EMPLOYEE_ROLES:
            case.releaser = str(actor)
        seq = self.store.next_sequence()
        case.resolution_id = compose_message_id(KIND_REPLY, self.instant.cycle_date(now), seq)
        self.store.update_case(case)
        return case

    def decline_query(
        self,
        *,
        case_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> GpiCase:
        self._require_enabled()
        case = self._get_case(case_id, actor, actor_type)
        if case.status not in {GPI_QUERY_IN, GPI_QUERY_OUT, GPI_PENDING_RELEASE}:
            raise GpiError('not_declinable', 'Case is not awaiting gpi query resolution.')
        if actor_type not in EMPLOYEE_ROLES and case.status != GPI_QUERY_IN:
            raise GpiError('not_declinable', 'Customer can only decline an inbound MT199.')
        now = float(self.clock())
        case.status = GPI_RESOLVED_NO
        case.resolved_at = now
        case.updated_at = now
        case.note = normalize_note(note)
        seq = self.store.next_sequence()
        case.resolution_id = compose_message_id(KIND_REPLY, self.instant.cycle_date(now), seq)
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
    ) -> GpiCase:
        self._require_staff(actor_type)
        case = self.store.get_case(case_id)
        if case is None:
            raise GpiError('case_not_found', 'Gpi case not found.')
        if case.status != GPI_UNMATCHED:
            raise GpiError('not_assignable', 'Only unmatched cases can be assigned.')
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
                    counterparty_bic=case.counterparty_bic, uetr=case.uetr, trn=case.trn,
                )
            except GpiError as exc:
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
            case.status = GPI_HELD
        elif case.message_id.startswith('INQY'):
            if self._needs_dual_control(parse_money(case.amount_usd)):
                case.status = GPI_PENDING_RELEASE
            else:
                case.status = GPI_QUERY_OUT if case.direction == DIR_OUT else GPI_QUERY_IN
        elif case.network_status:
            self._apply_network(case, case.network_status, now)
        else:
            case.status = GPI_OPEN
        self.store.update_case(case)
        return case

    def override_ofac(self, *, case_id: str, actor: str, actor_type: str, note: Any = '') -> GpiCase:
        self._require_staff(actor_type)
        case = self.store.get_case(case_id)
        if case is None:
            raise GpiError('case_not_found', 'Gpi case not found.')
        if case.status != GPI_HELD:
            raise GpiError('not_overridable', 'Case is not on OFAC hold.')
        now = float(self.clock())
        case.ofac_overridden = True
        case.note = normalize_note(note)
        case.updated_at = now
        if case.status == GPI_HELD:
            case.status = GPI_OPEN
        if case.message_id.startswith('INQY'):
            if self._needs_dual_control(parse_money(case.amount_usd)):
                case.status = GPI_PENDING_RELEASE
            else:
                case.status = GPI_QUERY_OUT if case.direction == DIR_OUT else GPI_QUERY_IN
        elif case.network_status:
            self._apply_network(case, case.network_status, now)
        else:
            case.status = GPI_OPEN
        self.store.update_case(case)
        return case

    def release_case(self, *, case_id: str, actor: str, actor_type: str) -> GpiCase:
        self._require_staff(actor_type)
        case = self.store.get_case(case_id)
        if case is None:
            raise GpiError('case_not_found', 'Gpi case not found.')
        if case.status != GPI_PENDING_RELEASE:
            raise GpiError('not_releasable', 'Case is not pending release.')
        if case.actor and case.actor == str(actor):
            raise GpiError('same_approver', 'A different employee must release this case.')
        now = float(self.clock())
        case.releaser = str(actor)
        case.updated_at = now
        if case.message_id.startswith('INQY'):
            case.status = GPI_QUERY_OUT if case.direction == DIR_OUT else GPI_QUERY_IN
        elif case.network_status:
            self._apply_network(case, case.network_status, now)
        else:
            case.status = GPI_OPEN
        self.store.update_case(case)
        return case

    def reject_case(self, *, case_id: str, actor: str, actor_type: str, note: Any = '') -> GpiCase:
        self._require_staff(actor_type)
        case = self.store.get_case(case_id)
        if case is None:
            raise GpiError('case_not_found', 'Gpi case not found.')
        if case.status not in {GPI_HELD, GPI_PENDING_RELEASE, GPI_UNMATCHED, GPI_QUERY_IN, GPI_QUERY_OUT, GPI_OPEN}:
            raise GpiError('not_rejectable', 'Case cannot be rejected.')
        now = float(self.clock())
        case.status = GPI_REJECTED
        case.note = normalize_note(note)
        case.updated_at = now
        self.store.update_case(case)
        return case

    def export_case(self, *, case_id: str, actor: str, actor_type: str) -> Dict[str, Any]:
        self._require_staff(actor_type)
        case = self.store.get_case(case_id)
        if case is None:
            raise GpiError('case_not_found', 'Gpi case not found.')
        if case.status not in {GPI_RESOLVED_OK, GPI_RESOLVED_NO, GPI_SETTLED, GPI_CREDITED, GPI_QUERY_OUT, GPI_QUERY_IN}:
            raise GpiError('not_exportable', 'Case has no exportable SWIFT message.')
        payload = {
            'tracker': compose_gpi(
                message_id=case.message_id if case.message_id.startswith('TRCK') else compose_message_id(
                    KIND_TRACKER, self.instant.cycle_date(float(self.clock())), self.store.next_sequence(),
                ),
                uetr=case.uetr, trn=case.trn,
                status=case.network_status or NET_ACSC, amount=parse_money(case.amount_usd),
                rail=case.rail, party_name=case.counterparty_name, party_bic=case.counterparty_bic,
                receiver=case.receiver_bic, cycle_date=self.instant.cycle_date(float(self.clock())),
            ) if case.network_status else '',
            'mt199r': compose_mt199_reply(
                case, resolution=RES_CNFM if case.status == GPI_RESOLVED_OK else RES_UNAB,
            ) if case.status in {GPI_RESOLVED_OK, GPI_RESOLVED_NO} else '',
            'case': case.to_dict(),
        }
        return payload

    def run_due(self, userid: Optional[str] = None) -> None:
        now = float(self.clock())
        rows = self.store.list_cases(userid=userid) if userid else self.store.list_cases()
        for case in rows:
            if case.status not in {GPI_SETTLED, GPI_CREDITED, GPI_QUERY_OUT, GPI_QUERY_IN, GPI_PENDING_RELEASE}:
                continue
            if case.settled_at <= 0:
                continue
            if self._query_open(case, now):
                continue
            if case.status in {GPI_SETTLED, GPI_CREDITED}:
                continue
            if case.status in {GPI_QUERY_OUT, GPI_QUERY_IN, GPI_PENDING_RELEASE}:
                case.status = GPI_EXPIRED
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
            if row.status == GPI_RESOLVED_OK:
                recalled += amount
            if row.status in {GPI_SETTLED, GPI_CREDITED, GPI_QUERY_OUT, GPI_QUERY_IN, GPI_RESOLVED_OK, GPI_RESOLVED_NO}:
                settled += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clock': self.instant.snapshot(now),
            'mt_clock': self.calendar.snapshot(now),
            'enrollments': [row.to_dict() for row in enrollments[:40]],
            'cases': [row.to_dict() for row in cases[:40]],
            'ytd_tracked': money_str(settled),
            'ytd_queried': money_str(recalled),
            'active_count': sum(1 for row in enrollments if row.status == ENROLL_ACTIVE),
            'open_count': sum(1 for row in cases if row.status in OPEN_GPI),
        }

    def _get_case(self, case_id: str, actor: str, actor_type: str) -> GpiCase:
        self._require_enabled()
        row = self.store.get_case(case_id)
        if row is None:
            raise GpiError('case_not_found', 'Gpi case not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise GpiError('gpi_forbidden', 'Not allowed to view this gpi case.')
        return row


_SERVICE: Optional[GpiService] = None


def set_service(service: Optional[GpiService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[GpiService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('GPI_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryGpiStore()
    path = os.environ.get('GPI_DB', DEFAULT_STORE_PATH)
    return SqliteGpiStore(path)


def build_service(
    *,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    store: Any = None,
) -> GpiService:
    policy = GpiPolicy.from_env()
    return GpiService(
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
        'already_queried': 409,
        'nsf': 409,
        'failed': 409,
        'gpi_forbidden': 403,
        'gpi_disabled': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_queryable': 403,
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
        'invalid_bic': 400,
        'invalid_rail': 400,
        'invalid_currency': 400,
        'invalid_status': 400,
        'invalid_reason': 400,
        'invalid_file': 400,
        'invalid_reference': 400,
        'wrong_receiver': 400,
        'gpi_amount_exceeded': 400,
        'mt_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_enrollment': 400,
        'missing_case': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: GpiError) -> Dict[str, Any]:
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
    except GpiError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snapshot_for(service: GpiService, userid: str, actor: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(userid, actor=actor, actor_type=actor_type)


def handle_list(service: GpiService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Gpi': _snapshot_for(service, owner, userid, actor_type)}), 200


def handle_unmatched(service: GpiService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'gpi_forbidden'}), 403
    rows = [row.to_dict() for row in service.store.list_cases(status=GPI_UNMATCHED)]
    return jsonify({'unmatched': rows, 'Gpi': _snapshot_for(service, userid, userid, actor_type)}), 200


def handle_add_enrollment(service: GpiService):
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
            rail=values.get('rail') or RAIL_GPI,
            amount=values.get('amount') or values.get('amount_usd'),
            account=values.get('account') or values.get('default_account') or values.get('from_account'),
            counterparty_name=values.get('counterparty_name') or values.get('name') or values.get('legal_name'),
            counterparty_bic=values.get('counterparty_bic') or values.get('bic') or values.get('routing'),
            uetr=values.get('uetr'),
            trn=values.get('trn') or values.get('e2e') or values.get('reference'),
        )
        return jsonify({
            'message': 'Gpi enrollment added',
            'enrollment': row.to_dict(),
            'Gpi': _snapshot_for(service, owner, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _enrollment_status_route(service: GpiService, status: str, message: str):
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
            'Gpi': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: GpiService):
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
        return jsonify({'preview': preview, 'Gpi': _snapshot_for(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest_gpi(service: GpiService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        case, created = service.ingest_gpi(
            actor=userid, actor_type=actor_type, message=message_from_gpi_values(values),
        )
        owner = case.userid or userid
        return jsonify({
            'message': 'gpi tracker ingested' if created else 'gpi tracker already posted',
            'case': case.to_dict(),
            'Gpi': _snapshot_for(service, owner, userid, actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_gpi_file(service: GpiService):
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
        for record in split_gpi_file(str(payload), header='GPI1'):
            last, created = service.ingest_gpi(
                actor=userid, actor_type=actor_type, message=parse_gpi(record),
            )
            created_any = created_any or created
        owner = last.userid if last and last.userid else userid
        return jsonify({
            'message': 'gpi tracker file ingested',
            'case': last.to_dict() if last else {},
            'Gpi': _snapshot_for(service, owner, userid, actor_type),
        }), 201 if created_any else 200

    return _handle_errors(_run)


def handle_ingest_mt199(service: GpiService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        case, created = service.ingest_mt199(
            actor=userid, actor_type=actor_type, message=message_from_mt199_values(values),
        )
        owner = case.userid or userid
        return jsonify({
            'message': 'MT199 ingested' if created else 'MT199 already posted',
            'case': case.to_dict(),
            'Gpi': _snapshot_for(service, owner, userid, actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_mt199_file(service: GpiService):
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
        for record in split_gpi_file(str(payload), header='MT199'):
            last, created = service.ingest_mt199(
                actor=userid, actor_type=actor_type, message=parse_mt199(record),
            )
            created_any = created_any or created
        owner = last.userid if last and last.userid else userid
        return jsonify({
            'message': 'MT199 file ingested',
            'case': last.to_dict() if last else {},
            'Gpi': _snapshot_for(service, owner, userid, actor_type),
        }), 201 if created_any else 200

    return _handle_errors(_run)


def _case_action_route(service: GpiService, action: str):
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
            case = service.request_query(
                case_id=case_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'NARR', note=values.get('note') or '',
            )
            message = 'Query requested'
        elif action == 'accept':
            case = service.accept_query(
                case_id=case_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Query accepted'
        elif action == 'decline':
            case = service.decline_query(
                case_id=case_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Query declined'
        elif action == 'assign':
            owner = str(values.get('customer_id') or values.get('owner') or '').strip()
            if not owner:
                raise GpiError('missing_customer_id', 'Customer id required.')
            case = service.assign_case(
                case_id=case_id, actor=userid, actor_type=actor_type, owner_userid=owner,
                account=values.get('account') or values.get('default_account'),
                nickname=values.get('nickname') or '',
                direction=values.get('direction'),
            )
            message = 'Gpi case assigned'
        elif action == 'override':
            case = service.override_ofac(
                case_id=case_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'release':
            case = service.release_case(case_id=case_id, actor=userid, actor_type=actor_type)
            message = 'Gpi case released'
        elif action == 'reject':
            case = service.reject_case(
                case_id=case_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Gpi case rejected'
        elif action == 'export':
            payload = service.export_case(case_id=case_id, actor=userid, actor_type=actor_type)
            owner = payload['case'].get('userid') or userid
            return jsonify({
                'message': 'Gpi case exported',
                'export': payload,
                'Gpi': _snapshot_for(service, owner, userid, actor_type),
            }), 200
        else:
            raise GpiError('invalid_status', 'Unknown gpi action.')
        return jsonify({
            'message': message,
            'case': case.to_dict(),
            'Gpi': _snapshot_for(service, case.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: GpiService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Gpi': _snapshot_for(service, owner, userid, actor_type)}), 200


def attach_gpi_routes(app, service: GpiService) -> None:
    @app.route('/listGpis', methods=['POST', 'GET'])
    def list_gpis_route():
        return handle_list(service)

    @app.route('/listGpiEnrollments', methods=['POST', 'GET'])
    def list_gpi_enrollments_route():
        return handle_list(service)

    @app.route('/listUnmatchedGpis', methods=['POST', 'GET'])
    def list_unmatched_gpis_route():
        return handle_unmatched(service)

    @app.route('/enrollGpi', methods=['POST', 'GET'])
    def enroll_gpi_route():
        return handle_add_enrollment(service)

    @app.route('/pauseGpiEnrollment', methods=['POST', 'GET'])
    def pause_gpi_enrollment_route():
        return _enrollment_status_route(service, ENROLL_PAUSED, 'Gpi enrollment paused')

    @app.route('/resumeGpiEnrollment', methods=['POST', 'GET'])
    def resume_gpi_enrollment_route():
        return _enrollment_status_route(service, ENROLL_ACTIVE, 'Gpi enrollment resumed')

    @app.route('/cancelGpiEnrollment', methods=['POST', 'GET'])
    def cancel_gpi_enrollment_route():
        return _enrollment_status_route(service, ENROLL_CANCELLED, 'Gpi enrollment cancelled')

    @app.route('/previewGpi', methods=['POST', 'GET'])
    def preview_gpi_route():
        return handle_preview(service)

    @app.route('/ingestGpi', methods=['POST', 'GET'])
    def ingest_gpi_route():
        return handle_ingest_gpi(service)

    @app.route('/ingestGpiFile', methods=['POST', 'GET'])
    def ingest_gpi_file_route():
        return handle_ingest_gpi_file(service)

    @app.route('/ingestMt199', methods=['POST', 'GET'])
    def ingest_mt199_route():
        return handle_ingest_mt199(service)

    @app.route('/ingestMt199File', methods=['POST', 'GET'])
    def ingest_mt199_file_route():
        return handle_ingest_mt199_file(service)

    @app.route('/requestGpiQuery', methods=['POST', 'GET'])
    def request_query_route():
        return _case_action_route(service, 'request')

    @app.route('/acceptGpiQuery', methods=['POST', 'GET'])
    def accept_query_route():
        return _case_action_route(service, 'accept')

    @app.route('/declineGpiQuery', methods=['POST', 'GET'])
    def decline_query_route():
        return _case_action_route(service, 'decline')

    @app.route('/assignGpi', methods=['POST', 'GET'])
    def assign_gpi_route():
        return _case_action_route(service, 'assign')

    @app.route('/overrideGpiOfac', methods=['POST', 'GET'])
    def override_gpi_ofac_route():
        return _case_action_route(service, 'override')

    @app.route('/releaseGpi', methods=['POST', 'GET'])
    def release_gpi_route():
        return _case_action_route(service, 'release')

    @app.route('/rejectGpi', methods=['POST', 'GET'])
    def reject_gpi_route():
        return _case_action_route(service, 'reject')

    @app.route('/exportGpi', methods=['POST', 'GET'])
    def export_gpi_route():
        return _case_action_route(service, 'export')

    @app.route('/runDueGpis', methods=['POST', 'GET'])
    def run_due_gpis_route():
        return handle_run_due(service)
