"""Inbound ACH Notification of Change (NACHA COR) as ODFI.

Staff ingest NACHA COR notices about a prior ACH origination. Customers
enroll external destination aliases; matched notices wait for apply
(mutate ABA / account / name / tran code) or refuse (C61-C69). This
maintains originator records — it does not post inbound ACH, send bill-pay
ACH, verify micro-deposits, return a FedNow credit, or answer camt.026.

Independent of ACH linking (PR #68), inbound ACH receive (PR #105),
bill-pay outgoing ACH (PR #66), inbound FedNow RFR (PR #167), inbound
FedNow RFI (PR #165), and domestic Fedwire (PR #73). Existing
`/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay unchanged.
The only ledger movement is an optional $0.20 apply fee.

Foundations (reusable beyond this screen):
- ABA routing checksum for destination and RDFI (reuse wire.normalize_aba)
- Destination fingerprint (ABA + account) uniqueness
- 15-digit NACHA trace uniqueness (not IMAD / E2E / RFR / TKN / VAU)
- NOC reference identifiers (NOC + YYYYMMDD + seq)
- NACHA COR SEC (PPD / CCD / WEB / pacs.008 / camt.056 rejected as ingest)
- Change codes C01-C09 / C13 / C14
- Refused-NOC codes C61-C69
- ACH banking-day clock with Same-Day 16:45 ET cutoff (not Fedwire 17:00,
  not FedNow 24/7) reusing the Fed holiday calendar
- NOC1 pipe compose/parse (XML/DOCTYPE rejected)
- Dual-control for routing-number changes (C02 / C03 / C07)

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Full destination accounts, fingerprints, traces, NOC refs, corrected data,
and ABAs never appear in to_dict / snapshots.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    AccountError,
    AmountError,
    DEFAULT_WATCHLIST,
    ScreenResult,
    WireCalendar,
    WireError,
    account_types_from_customer_payload,
    last4,
    money_str,
    normalize_aba,
    normalize_account,
    normalize_external_account,
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
EVT_QUEUED = 'queued'
EVT_PENDING = 'pending_release'
EVT_POSTED = 'posted'
EVT_REJECTED = 'rejected'
EVT_CANCELLED = 'cancelled'
EVT_REVERSED = 'reversed'
EVT_UNMATCHED = 'unmatched'
EVT_NSF = 'nsf'
EVT_FAILED = 'failed'
EVT_REFUSED = 'refused'
EVT_EXPIRED = 'expired'
EVT_STATUSES = frozenset({
    EVT_HELD, EVT_OPEN, EVT_QUEUED, EVT_PENDING, EVT_POSTED, EVT_REJECTED,
    EVT_CANCELLED, EVT_REVERSED, EVT_UNMATCHED, EVT_NSF, EVT_FAILED,
    EVT_EXPIRED, EVT_REFUSED,
})
OPEN_EVENTS = frozenset({EVT_HELD, EVT_OPEN, EVT_QUEUED, EVT_PENDING, EVT_UNMATCHED})
CANCELABLE = frozenset({EVT_HELD, EVT_OPEN, EVT_QUEUED, EVT_PENDING})
APPLYABLE = frozenset({EVT_OPEN})
REFUSABLE = frozenset({EVT_OPEN})
FEE_NONE = 'none'
FEE_COLLECTED = 'collected'
FEE_WAIVED = 'waived'
FEE_NSF = 'nsf'
MSG_COR = 'COR'
MSG_CORA = 'CORA'
MSG_CORR = 'CORR'
MESSAGES = frozenset({MSG_COR, MSG_CORA, MSG_CORR})
REJECTED_MESSAGES = frozenset({
    'ppd', 'ccd', 'web', 'tel', 'pos', 'pop', 'arc', 'boc', 'rck', 'iat',
    'pacs.008', 'pacs.002', 'camt.056', 'camt.026', 'camt.028', 'pain.013',
    'mt199', 'mt103',
})
CHANGE_C01 = 'C01'
CHANGE_C02 = 'C02'
CHANGE_C03 = 'C03'
CHANGE_C04 = 'C04'
CHANGE_C05 = 'C05'
CHANGE_C06 = 'C06'
CHANGE_C07 = 'C07'
CHANGE_C08 = 'C08'
CHANGE_C09 = 'C09'
CHANGE_C13 = 'C13'
CHANGE_C14 = 'C14'
CHANGE_CODES = frozenset({
    CHANGE_C01, CHANGE_C02, CHANGE_C03, CHANGE_C04, CHANGE_C05, CHANGE_C06,
    CHANGE_C07, CHANGE_C08, CHANGE_C09, CHANGE_C13, CHANGE_C14,
})
SENSITIVE_CHANGES = frozenset({CHANGE_C02, CHANGE_C03, CHANGE_C07})
CHANGE_ALIASES = {
    'account': CHANGE_C01, 'c01': CHANGE_C01, 'incorrect_account': CHANGE_C01,
    'routing': CHANGE_C02, 'c02': CHANGE_C02, 'incorrect_routing': CHANGE_C02,
    'routing_and_account': CHANGE_C03, 'c03': CHANGE_C03,
    'name': CHANGE_C04, 'c04': CHANGE_C04, 'incorrect_name': CHANGE_C04,
    'transaction_code': CHANGE_C05, 'c05': CHANGE_C05, 'tran_code': CHANGE_C05,
    'account_and_tran': CHANGE_C06, 'c06': CHANGE_C06,
    'routing_account_tran': CHANGE_C07, 'c07': CHANGE_C07,
    'receiving_dfi': CHANGE_C08, 'c08': CHANGE_C08, 'iat_dfi': CHANGE_C08,
    'individual_id': CHANGE_C09, 'c09': CHANGE_C09,
    'addenda': CHANGE_C13, 'c13': CHANGE_C13,
    'sec': CHANGE_C14, 'c14': CHANGE_C14, 'incorrect_sec': CHANGE_C14,
}
REFUSE_C61 = 'C61'
REFUSE_C62 = 'C62'
REFUSE_C63 = 'C63'
REFUSE_C64 = 'C64'
REFUSE_C65 = 'C65'
REFUSE_C66 = 'C66'
REFUSE_C67 = 'C67'
REFUSE_C68 = 'C68'
REFUSE_C69 = 'C69'
REFUSE_CODES = frozenset({
    REFUSE_C61, REFUSE_C62, REFUSE_C63, REFUSE_C64, REFUSE_C65,
    REFUSE_C66, REFUSE_C67, REFUSE_C68, REFUSE_C69,
})
REFUSE_ALIASES = {
    'misrouted': REFUSE_C61, 'c61': REFUSE_C61,
    'incorrect_trace': REFUSE_C62, 'c62': REFUSE_C62,
    'incorrect_company': REFUSE_C63, 'c63': REFUSE_C63,
    'incorrect_individual': REFUSE_C64, 'c64': REFUSE_C64,
    'bad_corrected': REFUSE_C65, 'c65': REFUSE_C65, 'formatted': REFUSE_C65,
    'discretionary': REFUSE_C66, 'c66': REFUSE_C66,
    'routing_mismatch': REFUSE_C67, 'c67': REFUSE_C67,
    'account_mismatch': REFUSE_C68, 'c68': REFUSE_C68,
    'incorrect_tran': REFUSE_C69, 'c69': REFUSE_C69,
}
ORIGINAL_SECS = frozenset({
    'PPD', 'CCD', 'WEB', 'TEL', 'POS', 'POP', 'ARC', 'BOC', 'RCK', 'CTX', 'CIE', 'IAT',
})
TRAN_CODES = frozenset({'22', '23', '27', '28', '32', '33', '37', '38'})
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/noc.sqlite'
CUSTOMER_REVERSE_SECONDS = 24 * 3600
STAFF_REVERSE_SECONDS = 90 * 24 * 3600
UNMATCHED_TTL_SECONDS = 24 * 3600
OPEN_TTL_SECONDS = 6 * 24 * 3600
ACH_CUTOFF_HOUR = 16
ACH_CUTOFF_MINUTE = 45


class NocError(ValueError):
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
        raise NocError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise NocError('invalid_name', 'Legal name must be 2-80 characters.')
    return text


def normalize_rdfi_aba(value: Any) -> str:
    """RDFI / destination ABA. Reuses the wire checksum; never a PAN Luhn."""
    try:
        return normalize_aba(value)
    except WireError as exc:
        raise NocError('invalid_aba', str(exc.message) if hasattr(exc, 'message') else str(exc)) from exc


def normalize_dest_account(value: Any) -> str:
    try:
        return normalize_external_account(value)
    except WireError as exc:
        raise NocError('invalid_account', str(exc.message) if hasattr(exc, 'message') else str(exc)) from exc


def mask_aba(aba: str) -> str:
    digits = ''.join(ch for ch in str(aba or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return '****' + digits[-4:]


def mask_account(account: str) -> str:
    digits = ''.join(ch for ch in str(account or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return '****' + digits[-4:]


def dest_fingerprint(aba: str, account: str) -> str:
    return hashlib.sha256(('%s|%s' % (aba, account)).encode('utf-8')).hexdigest()


def normalize_trace(value: Any) -> str:
    """15-digit NACHA trace. Not an IMAD, E2E, RFR, RFI, TKN, or VAU reference."""
    text = str(value or '').strip().upper()
    if text.startswith(('NOC', 'RFR', 'RFI', 'TKN', 'VAU', 'IMAD')):
        raise NocError('invalid_trace', 'Trace must not be a NOC, RFR, RFI, TKN, or VAU reference.')
    digits = ''.join(ch for ch in text if ch.isdigit())
    if len(text) == 22 and text[:8].isdigit() and text[8:16].isalnum() and text[16:].isdigit():
        raise NocError('invalid_trace', 'Trace must not be a Fedwire IMAD.')
    if len(digits) != 15:
        raise NocError('invalid_trace', 'NACHA trace must be 15 digits.')
    return digits


def mask_trace(trace: str) -> str:
    digits = ''.join(ch for ch in str(trace or '') if ch.isdigit())
    if len(digits) < 4:
        return 'TRC****'
    return 'TRC****' + digits[-4:]


def compose_noc_ref(cycle_date: str, sequence: int) -> str:
    """NOC + YYYYMMDD + 6-digit sequence. Not IMAD / ARN / RRN / RFR / TKN."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise NocError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise NocError('invalid_reference', 'NOC sequence out of range.')
    return 'NOC%s%06d' % (day, seq)


def normalize_noc_ref(value: Any) -> str:
    text = str(value or '').strip().upper()
    if not (text.startswith('NOC') and len(text) == 17 and text[3:].isdigit()):
        raise NocError('invalid_reference', 'NOC reference must be NOCYYYYMMDD######.')
    return text


def mask_noc_ref(ref: str) -> str:
    text = str(ref or '')
    if len(text) < 4:
        return 'NOC****'
    return 'NOC****' + text[-4:]


def normalize_message_type(value: Any, *, default: str = MSG_COR) -> str:
    text = str(value or default).strip().upper().replace('_', '.')
    aliases = {
        'COR': MSG_COR, 'NOC': MSG_COR, 'NOTIFICATION': MSG_COR, 'CHANGE': MSG_COR,
        'CORA': MSG_CORA, 'APPLIED': MSG_CORA, 'APPLY': MSG_CORA,
        'CORR': MSG_CORR, 'REFUSED': MSG_CORR, 'REFUSE': MSG_CORR,
    }
    compact = text.replace('.', '').replace('-', '').replace(' ', '')
    lowered = compact.lower()
    if lowered in REJECTED_MESSAGES or text.lower() in REJECTED_MESSAGES:
        raise NocError('invalid_message', 'Message must be COR, CORA, or CORR.')
    mapped = aliases.get(text) or aliases.get(compact)
    if mapped not in MESSAGES:
        raise NocError('invalid_message', 'Message must be COR, CORA, or CORR.')
    return mapped


def normalize_change(value: Any, *, default: str = CHANGE_C01) -> str:
    text = str(value or default).strip().upper().replace('-', '_').replace(' ', '_')
    if text in CHANGE_CODES:
        return text
    mapped = CHANGE_ALIASES.get(text.lower()) or CHANGE_ALIASES.get(text)
    if mapped in CHANGE_CODES:
        return mapped
    raise NocError('invalid_reason', 'Unknown NACHA change code.')


def normalize_refuse(value: Any, *, default: str = REFUSE_C65) -> str:
    text = str(value or default).strip().upper().replace('-', '_').replace(' ', '_')
    if text in REFUSE_CODES:
        return text
    mapped = REFUSE_ALIASES.get(text.lower()) or REFUSE_ALIASES.get(text)
    if mapped in REFUSE_CODES:
        return mapped
    raise NocError('invalid_response', 'Unknown refused-NOC code.')


def normalize_original_sec(value: Any, *, default: str = 'PPD') -> str:
    text = str(value or default).strip().upper()
    if text in ORIGINAL_SECS:
        return text
    raise NocError('invalid_sec', 'Unknown original SEC code.')


def normalize_tran_code(value: Any, *, default: str = '22') -> str:
    text = ''.join(ch for ch in str(value or default) if ch.isdigit())
    if len(text) == 1:
        text = text.zfill(2)
    if text not in TRAN_CODES:
        raise NocError('invalid_tran', 'Transaction code must be a NACHA DDA code.')
    return text


def compute_fee(fee: Decimal, *, waived: bool = False) -> Decimal:
    if waived:
        return Decimal('0.00')
    return fee.quantize(MONEY_QUANTUM)


def _digits(value: Any) -> str:
    return ''.join(ch for ch in str(value or '') if ch.isdigit())


def parse_corrected(change_code: str, value: Any) -> Dict[str, str]:
    text = str(value or '').strip()
    if change_code == CHANGE_C13:
        return {'note': text[:80]}
    if change_code == CHANGE_C04:
        return {'name': normalize_legal_name(text)}
    if change_code == CHANGE_C09:
        cleaned = ''.join(ch for ch in text if ch.isalnum())
        if not (1 <= len(cleaned) <= 22):
            raise NocError('invalid_corrected', 'Individual ID must be 1-22 characters.')
        return {'individual_id': cleaned}
    if change_code == CHANGE_C14:
        return {'sec': normalize_original_sec(text)}
    if change_code == CHANGE_C05:
        return {'tran_code': normalize_tran_code(text)}
    if change_code == CHANGE_C01:
        return {'account': normalize_dest_account(text)}
    if change_code in {CHANGE_C02, CHANGE_C08}:
        return {'aba': normalize_rdfi_aba(text)}
    if change_code == CHANGE_C03:
        aba, account = _split_aba_account(text)
        return {'aba': aba, 'account': account}
    if change_code == CHANGE_C06:
        digits = _digits(text)
        if len(digits) < 6:
            raise NocError('invalid_corrected', 'C06 requires account plus transaction code.')
        return {'account': normalize_dest_account(digits[:-2]), 'tran_code': normalize_tran_code(digits[-2:])}
    if change_code == CHANGE_C07:
        parts = [item.strip() for item in text.replace(',', '|').split('|') if item.strip()]
        if len(parts) == 3:
            return {
                'aba': normalize_rdfi_aba(parts[0]),
                'account': normalize_dest_account(parts[1]),
                'tran_code': normalize_tran_code(parts[2]),
            }
        digits = _digits(text)
        if len(digits) < 15:
            raise NocError('invalid_corrected', 'C07 requires routing, account, and transaction code.')
        return {
            'aba': normalize_rdfi_aba(digits[:9]),
            'account': normalize_dest_account(digits[9:-2]),
            'tran_code': normalize_tran_code(digits[-2:]),
        }
    raise NocError('invalid_reason', 'Unknown NACHA change code.')


def _split_aba_account(text: str) -> Tuple[str, str]:
    if '|' in text:
        left, right = text.split('|', 1)
        return normalize_rdfi_aba(left), normalize_dest_account(right)
    pieces = text.split()
    if len(pieces) >= 2:
        return normalize_rdfi_aba(pieces[0]), normalize_dest_account(''.join(pieces[1:]))
    digits = _digits(text)
    if len(digits) < 13:
        raise NocError('invalid_corrected', 'C03 requires routing and account.')
    return normalize_rdfi_aba(digits[:9]), normalize_dest_account(digits[9:])


def compose_corrected(change_code: str, parsed: Dict[str, str]) -> str:
    if change_code in {CHANGE_C01}:
        return parsed['account']
    if change_code in {CHANGE_C02, CHANGE_C08}:
        return parsed['aba']
    if change_code == CHANGE_C03:
        return '%s|%s' % (parsed['aba'], parsed['account'])
    if change_code == CHANGE_C04:
        return parsed['name']
    if change_code == CHANGE_C05:
        return parsed['tran_code']
    if change_code == CHANGE_C06:
        return '%s%s' % (parsed['account'], parsed['tran_code'])
    if change_code == CHANGE_C07:
        return '%s|%s|%s' % (parsed['aba'], parsed['account'], parsed['tran_code'])
    if change_code == CHANGE_C09:
        return parsed['individual_id']
    if change_code == CHANGE_C13:
        return parsed.get('note', '')
    if change_code == CHANGE_C14:
        return parsed['sec']
    return ''


def mask_corrected(change_code: str, value: str) -> str:
    try:
        parsed = parse_corrected(change_code, value)
    except NocError:
        return '****'
    if 'account' in parsed and 'aba' in parsed:
        return '%s/%s' % (mask_aba(parsed['aba']), mask_account(parsed['account']))
    if 'account' in parsed:
        return mask_account(parsed['account'])
    if 'aba' in parsed:
        return mask_aba(parsed['aba'])
    if 'name' in parsed:
        return parsed['name'][:3] + '****'
    if 'tran_code' in parsed:
        return parsed['tran_code']
    if 'sec' in parsed:
        return parsed['sec']
    if 'individual_id' in parsed:
        ident = parsed['individual_id']
        return '****' + ident[-4:] if len(ident) >= 4 else '****'
    return '****'


def compose_noc1(
    *,
    message_type: str,
    trace: str,
    change_code: str,
    aba: str,
    account: str,
    corrected: str = '',
    name: str = '',
    original_sec: str = 'PPD',
    tran_code: str = '22',
    amount: str = '0.00',
    status: str = EVT_OPEN,
) -> str:
    return 'NOC1|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s' % (
        message_type, trace, change_code, aba, account,
        str(corrected or '').replace('|', '/'),
        str(name or '').replace('|', ' '),
        original_sec, tran_code, amount, status,
    )


def parse_noc1(line: str) -> Dict[str, str]:
    text = str(line or '').strip()
    lowered = text.lower()
    if '<?xml' in lowered or '<!doctype' in lowered or '<xml' in lowered:
        raise NocError('invalid_file', 'XML/DOCTYPE is not a valid NOC1 record.')
    if not text:
        raise NocError('invalid_file', 'Empty NOC1 record.')
    if not text.startswith('NOC1|'):
        raise NocError('invalid_file', 'Record must start with NOC1|.')
    parts = text.split('|')
    if len(parts) < 12:
        raise NocError('invalid_file', 'NOC1 record is missing fields.')
    account = parts[5]
    if '*' in account:
        raise NocError('invalid_account', 'Ingest requires a full account, not a mask.')
    return {
        'message_type': parts[1],
        'trace': parts[2],
        'change_code': parts[3],
        'aba': parts[4],
        'account': account,
        'corrected': parts[6].replace('/', '|'),
        'name': parts[7],
        'original_sec': parts[8],
        'tran_code': parts[9],
        'amount': parts[10],
        'status': parts[11],
    }


def split_noc1_file(payload: Any) -> List[str]:
    text = str(payload or '')
    lowered = text.lower()
    if '<?xml' in lowered or '<!doctype' in lowered or '<xml' in lowered:
        raise NocError('invalid_file', 'XML/DOCTYPE files are rejected.')
    lines = []
    for raw in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        item = raw.strip()
        if item:
            lines.append(item)
    if not lines:
        raise NocError('invalid_file', 'NOC1 file is empty.')
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


class AchClock:
    """ACH NOC uses the last Same-Day window (16:45 ET), not Fedwire 17:00 or FedNow 24/7."""

    def __init__(self, *, tz_offset_hours: int = -4) -> None:
        self.tz_offset_hours = int(tz_offset_hours)
        self.calendar = WireCalendar(
            cutoff_hour=ACH_CUTOFF_HOUR,
            cutoff_minute=ACH_CUTOFF_MINUTE,
            tz_offset_hours=self.tz_offset_hours,
        )

    def local_dt(self, ts: float):
        return self.calendar.local_dt(ts)

    def is_open(self, ts: float, network: str = '') -> bool:
        local = self.calendar.local_dt(ts)
        return self.calendar.is_business_day(local.date()) and not self.calendar.is_after_cutoff(ts)

    def cycle_date(self, ts: float, network: str = '') -> str:
        return self.calendar.cycle_date(ts)

    def snapshot(self, ts: float) -> Dict[str, Any]:
        local = self.calendar.local_dt(ts)
        cycle = self.cycle_date(ts)
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'cutoff': '%02d:%02d' % (ACH_CUTOFF_HOUR, ACH_CUTOFF_MINUTE),
            'after_cutoff': self.calendar.is_after_cutoff(ts) or not self.calendar.is_business_day(local.date()),
            'always_open': False,
            'ach_open': self.is_open(ts),
            'same_day_window': self.is_open(ts),
            'business_day': self.calendar.is_business_day(local.date()),
            'weekday': local.strftime('%A'),
            'value_date': '%s-%s-%s' % (cycle[:4], cycle[4:6], cycle[6:]),
            'cycle_date': cycle,
        }


@dataclass
class NocPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_respond: bool = True
    allow_credit: bool = False
    max_keys: int = 12
    max_notices: int = 120
    max_correspondents: int = 40
    fee_amount: Decimal = Decimal('0.20')
    tz_offset_hours: int = -4
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST

    @classmethod
    def from_env(cls) -> 'NocPolicy':
        extra = _env_list('NOC_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('NOC_ENABLED', True),
            customer_manage=_env_bool('NOC_CUSTOMER_MANAGE', True),
            customer_respond=_env_bool('NOC_CUSTOMER_RESPOND', True),
            allow_credit=_env_bool('NOC_ALLOW_CREDIT', False),
            max_keys=max(1, _env_int('NOC_MAX_KEYS', 12)),
            max_notices=max(1, _env_int('NOC_MAX_NOTICES', 120)),
            max_correspondents=max(1, _env_int('NOC_MAX_CORRESPONDENTS', 40)),
            fee_amount=_env_money('NOC_FEE', '0.20'),
            tz_offset_hours=_env_int('NOC_TZ_OFFSET', -4),
            watchlist=watch,
        )


@dataclass
class NocKey:
    key_id: str
    userid: str
    nickname: str
    legal_name: str
    dest_aba: str
    dest_account: str
    fingerprint: str
    funding_account: str
    tran_code: str
    original_sec: str
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
            'aba_masked': mask_aba(self.dest_aba),
            'account_masked': mask_account(self.dest_account),
            'last4': last4(self.dest_account),
            'funding_account': self.funding_account,
            'tran_code': self.tran_code,
            'original_sec': self.original_sec,
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == KEY_ACTIVE,
            'paused': self.status == KEY_PAUSED,
            'cancelled': self.status == KEY_CANCELLED,
        }


@dataclass
class NocCorrespondent:
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
class NocNotice:
    notice_id: str
    trace_id: str
    key_id: str
    correspondent_id: str
    userid: str
    funding_account: str
    nickname: str
    legal_name: str
    dest_account: str
    dest_aba: str
    fingerprint: str
    rdfi_aba: str
    rdfi_name: str
    message_type: str
    change_code: str
    original_sec: str
    tran_code: str
    corrected_data: str
    amount: str
    status: str
    noc_ref: str
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
    prior_dest_account: str = ''
    prior_dest_aba: str = ''
    prior_legal_name: str = ''
    prior_tran_code: str = ''
    prior_fingerprint: str = ''
    prior_original_sec: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'notice_id': self.notice_id,
            'inquiry_id': self.notice_id,
            'trace_masked': mask_trace(self.trace_id),
            'key_id': self.key_id,
            'correspondent_id': self.correspondent_id,
            'userid': self.userid,
            'funding_account': self.funding_account,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'account_masked': mask_account(self.dest_account),
            'aba_masked': mask_aba(self.dest_aba),
            'last4': last4(self.dest_account),
            'rdfi_aba_masked': mask_aba(self.rdfi_aba),
            'rdfi_name': self.rdfi_name,
            'message_type': self.message_type,
            'change_code': self.change_code,
            'original_sec': self.original_sec,
            'tran_code': self.tran_code,
            'corrected_masked': mask_corrected(self.change_code, self.corrected_data) if self.corrected_data else '',
            'amount': self.amount,
            'status': self.status,
            'noc_ref_masked': mask_noc_ref(self.noc_ref) if self.noc_ref else '',
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
            'queued': self.status == EVT_QUEUED,
            'pending_release': self.status == EVT_PENDING,
            'posted': self.status == EVT_POSTED,
            'refused': self.status == EVT_REFUSED,
            'unmatched': self.status == EVT_UNMATCHED,
            'cancelable': self.status in CANCELABLE,
            'applicable': self.status in APPLYABLE,
            'refusable': self.status in REFUSABLE,
        }


def _clone_key(row: NocKey) -> NocKey:
    return NocKey(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_corr(row: NocCorrespondent) -> NocCorrespondent:
    return NocCorrespondent(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_notice(row: NocNotice) -> NocNotice:
    return NocNotice(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _key_from_row(row: Any) -> NocKey:
    return NocKey(
        key_id=row['key_id'], userid=row['userid'], nickname=row['nickname'],
        legal_name=row['legal_name'], dest_aba=row['dest_aba'],
        dest_account=row['dest_account'], fingerprint=row['fingerprint'],
        funding_account=row['funding_account'], tran_code=row['tran_code'],
        original_sec=row['original_sec'], status=row['status'], actor=row['actor'],
        created_at=float(row['created_at']), updated_at=float(row['updated_at']),
    )


def _corr_from_row(row: Any) -> NocCorrespondent:
    return NocCorrespondent(
        correspondent_id=row['correspondent_id'], nickname=row['nickname'],
        legal_name=row['legal_name'], aba=row['aba'], status=row['status'],
        actor=row['actor'], created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
    )


def _notice_from_row(row: Any) -> NocNotice:
    return NocNotice(
        notice_id=row['notice_id'], trace_id=row['trace_id'], key_id=row['key_id'] or '',
        correspondent_id=row['correspondent_id'] or '', userid=row['userid'] or '',
        funding_account=row['funding_account'] or '', nickname=row['nickname'] or '',
        legal_name=row['legal_name'] or '', dest_account=row['dest_account'],
        dest_aba=row['dest_aba'], fingerprint=row['fingerprint'],
        rdfi_aba=row['rdfi_aba'], rdfi_name=row['rdfi_name'] or '',
        message_type=row['message_type'], change_code=row['change_code'],
        original_sec=row['original_sec'] or 'PPD', tran_code=row['tran_code'] or '22',
        corrected_data=row['corrected_data'] or '', amount=row['amount'],
        status=row['status'], noc_ref=row['noc_ref'] or '',
        response_text=row['response_text'] or '', fee=row['fee'],
        fee_status=row['fee_status'], actor=row['actor'],
        releaser=row['releaser'] or '', ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '', created_at=float(row['created_at']),
        updated_at=float(row['updated_at']), posted_at=float(row['posted_at'] or 0),
        reversed_at=float(row['reversed_at'] or 0), note=row['note'] or '',
        value_date=row['value_date'] or '',
        prior_dest_account=row['prior_dest_account'] or '',
        prior_dest_aba=row['prior_dest_aba'] or '',
        prior_legal_name=row['prior_legal_name'] or '',
        prior_tran_code=row['prior_tran_code'] or '',
        prior_fingerprint=row['prior_fingerprint'] or '',
        prior_original_sec=row['prior_original_sec'] or '',
    )


class MemoryNocStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: Dict[str, NocKey] = {}
        self._corrs: Dict[str, NocCorrespondent] = {}
        self._notices: Dict[str, NocNotice] = {}

    def put_key(self, row: NocKey) -> None:
        with self._lock:
            self._keys[row.key_id] = _clone_key(row)

    def get_key(self, key_id: str) -> Optional[NocKey]:
        with self._lock:
            row = self._keys.get(key_id)
            return _clone_key(row) if row else None

    def update_key(self, row: NocKey) -> None:
        self.put_key(row)

    def list_keys(self, userid: Optional[str] = None, *, include_cancelled: bool = True) -> List[NocKey]:
        with self._lock:
            rows = list(self._keys.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_cancelled:
            rows = [row for row in rows if row.status != KEY_CANCELLED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return [_clone_key(row) for row in rows]

    def find_key_by_nickname(self, userid: str, nickname: str) -> Optional[NocKey]:
        with self._lock:
            for row in self._keys.values():
                if row.userid == userid and row.nickname.lower() == nickname.lower() and row.status != KEY_CANCELLED:
                    return _clone_key(row)
        return None

    def find_key_by_fingerprint(self, fingerprint: str, *, userid: Optional[str] = None) -> Optional[NocKey]:
        with self._lock:
            for row in self._keys.values():
                if row.fingerprint == fingerprint and row.status != KEY_CANCELLED:
                    if userid is None or row.userid == userid:
                        return _clone_key(row)
        return None

    def put_correspondent(self, row: NocCorrespondent) -> None:
        with self._lock:
            self._corrs[row.correspondent_id] = _clone_corr(row)

    def get_correspondent(self, correspondent_id: str) -> Optional[NocCorrespondent]:
        with self._lock:
            row = self._corrs.get(correspondent_id)
            return _clone_corr(row) if row else None

    def update_correspondent(self, row: NocCorrespondent) -> None:
        self.put_correspondent(row)

    def list_correspondents(self, *, include_archived: bool = True) -> List[NocCorrespondent]:
        with self._lock:
            rows = list(self._corrs.values())
        if not include_archived:
            rows = [row for row in rows if row.status != CORR_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return [_clone_corr(row) for row in rows]

    def find_correspondent_by_aba(self, aba: str) -> Optional[NocCorrespondent]:
        with self._lock:
            for row in self._corrs.values():
                if row.aba == aba and row.status != CORR_ARCHIVED:
                    return _clone_corr(row)
        return None

    def find_correspondent_by_nickname(self, nickname: str) -> Optional[NocCorrespondent]:
        with self._lock:
            for row in self._corrs.values():
                if row.nickname.lower() == nickname.lower() and row.status != CORR_ARCHIVED:
                    return _clone_corr(row)
        return None

    def put_notice(self, row: NocNotice) -> NocNotice:
        with self._lock:
            existing = next((item for item in self._notices.values() if item.trace_id == row.trace_id), None)
            if existing is not None:
                return _clone_notice(existing)
            self._notices[row.notice_id] = _clone_notice(row)
            return _clone_notice(row)

    def update_notice(self, row: NocNotice) -> None:
        with self._lock:
            self._notices[row.notice_id] = _clone_notice(row)

    def get_notice(self, notice_id: str) -> Optional[NocNotice]:
        with self._lock:
            row = self._notices.get(notice_id)
            return _clone_notice(row) if row else None

    def get_notice_by_trace(self, trace_id: str) -> Optional[NocNotice]:
        with self._lock:
            for row in self._notices.values():
                if row.trace_id == trace_id:
                    return _clone_notice(row)
        return None

    def list_notices(self, userid: Optional[str] = None, key_id: Optional[str] = None) -> List[NocNotice]:
        with self._lock:
            rows = list(self._notices.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if key_id is not None:
            rows = [row for row in rows if row.key_id == key_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return [_clone_notice(row) for row in rows]

    def list_unmatched(self) -> List[NocNotice]:
        with self._lock:
            rows = [_clone_notice(row) for row in self._notices.values() if row.status == EVT_UNMATCHED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def next_noc_sequence(self, cycle_date: str) -> int:
        prefix = 'NOC' + cycle_date
        with self._lock:
            used = [row.noc_ref for row in self._notices.values() if row.noc_ref.startswith(prefix)]
        return len(used) + 1


class SqliteNocStore:
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
                    dest_aba TEXT NOT NULL,
                    dest_account TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    funding_account TEXT NOT NULL,
                    tran_code TEXT NOT NULL,
                    original_sec TEXT NOT NULL,
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
                CREATE TABLE IF NOT EXISTS notices (
                    notice_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    key_id TEXT NOT NULL DEFAULT '',
                    correspondent_id TEXT NOT NULL DEFAULT '',
                    userid TEXT NOT NULL DEFAULT '',
                    funding_account TEXT NOT NULL DEFAULT '',
                    nickname TEXT NOT NULL DEFAULT '',
                    legal_name TEXT NOT NULL DEFAULT '',
                    dest_account TEXT NOT NULL,
                    dest_aba TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    rdfi_aba TEXT NOT NULL,
                    rdfi_name TEXT NOT NULL DEFAULT '',
                    message_type TEXT NOT NULL,
                    change_code TEXT NOT NULL,
                    original_sec TEXT NOT NULL DEFAULT 'PPD',
                    tran_code TEXT NOT NULL DEFAULT '22',
                    corrected_data TEXT NOT NULL DEFAULT '',
                    amount TEXT NOT NULL,
                    status TEXT NOT NULL,
                    noc_ref TEXT NOT NULL DEFAULT '',
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
                    value_date TEXT NOT NULL DEFAULT '',
                    prior_dest_account TEXT NOT NULL DEFAULT '',
                    prior_dest_aba TEXT NOT NULL DEFAULT '',
                    prior_legal_name TEXT NOT NULL DEFAULT '',
                    prior_tran_code TEXT NOT NULL DEFAULT '',
                    prior_fingerprint TEXT NOT NULL DEFAULT '',
                    prior_original_sec TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_key(self, row: NocKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO keys (
                    key_id, userid, nickname, legal_name, dest_aba, dest_account,
                    fingerprint, funding_account, tran_code, original_sec, status,
                    actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.key_id, row.userid, row.nickname, row.legal_name, row.dest_aba,
                    row.dest_account, row.fingerprint, row.funding_account, row.tran_code,
                    row.original_sec, row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_key(self, key_id: str) -> Optional[NocKey]:
        with self._lock, self._connect() as conn:
            cur = conn.execute('SELECT * FROM keys WHERE key_id = ?', (key_id,))
            row = cur.fetchone()
            return _key_from_row(row) if row else None

    def update_key(self, row: NocKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE keys SET nickname=?, legal_name=?, dest_aba=?, dest_account=?,
                    fingerprint=?, funding_account=?, tran_code=?, original_sec=?,
                    status=?, actor=?, updated_at=?
                WHERE key_id=?
                """,
                (
                    row.nickname, row.legal_name, row.dest_aba, row.dest_account,
                    row.fingerprint, row.funding_account, row.tran_code, row.original_sec,
                    row.status, row.actor, row.updated_at, row.key_id,
                ),
            )
            conn.commit()

    def list_keys(self, userid: Optional[str] = None, *, include_cancelled: bool = True) -> List[NocKey]:
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

    def find_key_by_nickname(self, userid: str, nickname: str) -> Optional[NocKey]:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM keys WHERE userid = ? AND lower(nickname) = lower(?) AND status != 'cancelled'",
                (userid, nickname),
            )
            row = cur.fetchone()
            return _key_from_row(row) if row else None

    def find_key_by_fingerprint(self, fingerprint: str, *, userid: Optional[str] = None) -> Optional[NocKey]:
        sql = "SELECT * FROM keys WHERE fingerprint = ? AND status != 'cancelled'"
        params: List[Any] = [fingerprint]
        if userid is not None:
            sql += ' AND userid = ?'
            params.append(userid)
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return _key_from_row(row) if row else None

    def put_correspondent(self, row: NocCorrespondent) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO correspondents (
                    correspondent_id, nickname, legal_name, aba, status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.correspondent_id, row.nickname, row.legal_name, row.aba,
                    row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_correspondent(self, correspondent_id: str) -> Optional[NocCorrespondent]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM correspondents WHERE correspondent_id = ?', (correspondent_id,),
            ).fetchone()
            return _corr_from_row(row) if row else None

    def update_correspondent(self, row: NocCorrespondent) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE correspondents SET nickname=?, legal_name=?, aba=?, status=?, actor=?, updated_at=?
                WHERE correspondent_id=?
                """,
                (row.nickname, row.legal_name, row.aba, row.status, row.actor, row.updated_at, row.correspondent_id),
            )
            conn.commit()

    def list_correspondents(self, *, include_archived: bool = True) -> List[NocCorrespondent]:
        sql = 'SELECT * FROM correspondents'
        if not include_archived:
            sql += " WHERE status != 'archived'"
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [_corr_from_row(row) for row in rows]

    def find_correspondent_by_aba(self, aba: str) -> Optional[NocCorrespondent]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM correspondents WHERE aba = ? AND status != 'archived'", (aba,),
            ).fetchone()
            return _corr_from_row(row) if row else None

    def find_correspondent_by_nickname(self, nickname: str) -> Optional[NocCorrespondent]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM correspondents WHERE lower(nickname) = lower(?) AND status != 'archived'",
                (nickname,),
            ).fetchone()
            return _corr_from_row(row) if row else None

    def put_notice(self, row: NocNotice) -> NocNotice:
        with self._lock, self._connect() as conn:
            existing = conn.execute('SELECT * FROM notices WHERE trace_id = ?', (row.trace_id,)).fetchone()
            if existing is not None:
                return _notice_from_row(existing)
            conn.execute(
                """
                INSERT INTO notices (
                    notice_id, trace_id, key_id, correspondent_id, userid, funding_account,
                    nickname, legal_name, dest_account, dest_aba, fingerprint, rdfi_aba,
                    rdfi_name, message_type, change_code, original_sec, tran_code,
                    corrected_data, amount, status, noc_ref, response_text, fee, fee_status,
                    actor, releaser, ofac_hit, ofac_match, created_at, updated_at, posted_at,
                    reversed_at, note, value_date, prior_dest_account, prior_dest_aba,
                    prior_legal_name, prior_tran_code, prior_fingerprint, prior_original_sec
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    row.notice_id, row.trace_id, row.key_id, row.correspondent_id, row.userid,
                    row.funding_account, row.nickname, row.legal_name, row.dest_account,
                    row.dest_aba, row.fingerprint, row.rdfi_aba, row.rdfi_name,
                    row.message_type, row.change_code, row.original_sec, row.tran_code,
                    row.corrected_data, row.amount, row.status, row.noc_ref, row.response_text,
                    row.fee, row.fee_status, row.actor, row.releaser, row.ofac_hit,
                    row.ofac_match, row.created_at, row.updated_at, row.posted_at,
                    row.reversed_at, row.note, row.value_date, row.prior_dest_account,
                    row.prior_dest_aba, row.prior_legal_name, row.prior_tran_code,
                    row.prior_fingerprint, row.prior_original_sec,
                ),
            )
            conn.commit()
            return _clone_notice(row)

    def update_notice(self, row: NocNotice) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE notices SET key_id=?, correspondent_id=?, userid=?, funding_account=?,
                    nickname=?, legal_name=?, dest_account=?, dest_aba=?, fingerprint=?,
                    rdfi_aba=?, rdfi_name=?, message_type=?, change_code=?, original_sec=?,
                    tran_code=?, corrected_data=?, amount=?, status=?, noc_ref=?,
                    response_text=?, fee=?, fee_status=?, actor=?, releaser=?, ofac_hit=?,
                    ofac_match=?, updated_at=?, posted_at=?, reversed_at=?, note=?,
                    value_date=?, prior_dest_account=?, prior_dest_aba=?, prior_legal_name=?,
                    prior_tran_code=?, prior_fingerprint=?, prior_original_sec=?
                WHERE notice_id=?
                """,
                (
                    row.key_id, row.correspondent_id, row.userid, row.funding_account,
                    row.nickname, row.legal_name, row.dest_account, row.dest_aba,
                    row.fingerprint, row.rdfi_aba, row.rdfi_name, row.message_type,
                    row.change_code, row.original_sec, row.tran_code, row.corrected_data,
                    row.amount, row.status, row.noc_ref, row.response_text, row.fee,
                    row.fee_status, row.actor, row.releaser, row.ofac_hit, row.ofac_match,
                    row.updated_at, row.posted_at, row.reversed_at, row.note, row.value_date,
                    row.prior_dest_account, row.prior_dest_aba, row.prior_legal_name,
                    row.prior_tran_code, row.prior_fingerprint, row.prior_original_sec,
                    row.notice_id,
                ),
            )
            conn.commit()

    def get_notice(self, notice_id: str) -> Optional[NocNotice]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM notices WHERE notice_id = ?', (notice_id,)).fetchone()
            return _notice_from_row(row) if row else None

    def get_notice_by_trace(self, trace_id: str) -> Optional[NocNotice]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM notices WHERE trace_id = ?', (trace_id,)).fetchone()
            return _notice_from_row(row) if row else None

    def list_notices(self, userid: Optional[str] = None, key_id: Optional[str] = None) -> List[NocNotice]:
        sql = 'SELECT * FROM notices'
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
        return [_notice_from_row(row) for row in rows]

    def list_unmatched(self) -> List[NocNotice]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notices WHERE status = 'unmatched' ORDER BY created_at DESC"
            ).fetchall()
        return [_notice_from_row(row) for row in rows]

    def next_noc_sequence(self, cycle_date: str) -> int:
        prefix = 'NOC' + cycle_date + '%'
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM notices WHERE noc_ref LIKE ?", (prefix,),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class NocService:
    def __init__(
        self,
        policy: NocPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[AchClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or AchClock(tz_offset_hours=policy.tz_offset_hours)

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise NocError('noc_disabled', 'ACH NOC is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise NocError('noc_forbidden', 'Customers cannot manage NOC keys.')

    def _require_respond(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_respond:
            raise NocError('noc_forbidden', 'Customers cannot apply or refuse NOCs.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise NocError('noc_forbidden', 'Staff only.')

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
            raise NocError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise NocError('credit_not_allowed', 'Credit accounts cannot fund ACH NOC keys.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, change_code: str) -> bool:
        return change_code in SENSITIVE_CHANGES

    def add_key(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        dest_aba: Any,
        dest_account: Any,
        funding_account: Any,
        tran_code: Any = '22',
        original_sec: Any = 'PPD',
    ) -> NocKey:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise NocError('noc_forbidden', 'Not allowed to enroll NOC keys for this customer.')
        now = float(self.clock())
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        aba = normalize_rdfi_aba(dest_aba)
        dest = normalize_dest_account(dest_account)
        funding = normalize_account(funding_account)
        self._assert_internal_account(owner_userid, funding)
        fingerprint = dest_fingerprint(aba, dest)
        if self.store.find_key_by_nickname(owner_userid, name) is not None:
            raise NocError('key_duplicate', 'An alias with that nickname already exists.')
        if self.store.find_key_by_fingerprint(fingerprint) is not None:
            raise NocError('key_duplicate', 'That destination is already enrolled.')
        open_rows = [row for row in self.store.list_keys(owner_userid) if row.status in OPEN_KEYS]
        if len(open_rows) >= self.policy.max_keys:
            raise NocError('key_limit', 'NOC key limit reached.')
        row = NocKey(
            key_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            dest_aba=aba,
            dest_account=dest,
            fingerprint=fingerprint,
            funding_account=funding,
            tran_code=normalize_tran_code(tran_code),
            original_sec=normalize_original_sec(original_sec),
            status=KEY_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_key(row)
        self._rematch_unmatched(row)
        return row

    def get_key(self, *, key_id: str, actor: str, actor_type: str) -> NocKey:
        self._require_enabled()
        row = self.store.get_key(key_id)
        if row is None:
            raise NocError('key_not_found', 'NOC key not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise NocError('noc_forbidden', 'Not allowed to view this key.')
        return row

    def enforce_key(
        self,
        *,
        key_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> NocKey:
        row = self.get_key(key_id=key_id, actor=actor, actor_type=actor_type)
        if row.status == KEY_CANCELLED:
            raise NocError('already_cancelled', 'Key is cancelled.')
        if row.status == KEY_PAUSED:
            raise NocError('key_paused', 'Key is paused.')
        if require_active and row.status != KEY_ACTIVE:
            raise NocError('invalid_status', 'Key is not active.')
        return row

    def set_key_status(
        self,
        *,
        key_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> NocKey:
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
            raise NocError('invalid_status', 'Status must be pause, resume, or cancel.')
        if row.status == KEY_CANCELLED:
            raise NocError('already_cancelled', 'Key is already cancelled.')
        if wanted == KEY_PAUSED:
            if row.status == KEY_PAUSED:
                raise NocError('already_paused', 'Key is already paused.')
            if row.status != KEY_ACTIVE:
                raise NocError('invalid_status', 'Only an active key can be paused.')
        elif wanted == KEY_ACTIVE:
            if row.status == KEY_ACTIVE:
                raise NocError('already_active', 'Key is already active.')
            if row.status != KEY_PAUSED:
                raise NocError('invalid_status', 'Only a paused key can be resumed.')
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
    ) -> NocCorrespondent:
        self._require_staff(actor_type)
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        routing = normalize_rdfi_aba(aba)
        if self.store.find_correspondent_by_nickname(name) is not None:
            raise NocError('correspondent_duplicate', 'A correspondent with that nickname already exists.')
        if self.store.find_correspondent_by_aba(routing) is not None:
            raise NocError('correspondent_duplicate', 'That ABA is already on file.')
        open_rows = [row for row in self.store.list_correspondents() if row.status in OPEN_CORRS]
        if len(open_rows) >= self.policy.max_correspondents:
            raise NocError('correspondent_limit', 'Correspondent directory limit reached.')
        now = float(self.clock())
        row = NocCorrespondent(
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

    def get_correspondent(self, *, correspondent_id: str, actor_type: str) -> NocCorrespondent:
        self._require_staff(actor_type)
        row = self.store.get_correspondent(correspondent_id)
        if row is None:
            raise NocError('correspondent_not_found', 'Correspondent not found.')
        return row

    def set_correspondent_status(
        self,
        *,
        correspondent_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> NocCorrespondent:
        self._require_staff(actor_type)
        row = self.get_correspondent(correspondent_id=correspondent_id, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': CORR_PAUSED, 'resume': CORR_ACTIVE, 'activate': CORR_ACTIVE,
            'archive': CORR_ARCHIVED, 'close': CORR_ARCHIVED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {CORR_PAUSED, CORR_ACTIVE, CORR_ARCHIVED}:
            raise NocError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == CORR_ARCHIVED:
            raise NocError('already_archived', 'Correspondent is already archived.')
        if wanted == CORR_PAUSED and row.status == CORR_PAUSED:
            raise NocError('already_paused', 'Correspondent is already paused.')
        if wanted == CORR_ACTIVE and row.status == CORR_ACTIVE:
            raise NocError('already_active', 'Correspondent is already active.')
        if wanted == CORR_ACTIVE and row.status != CORR_PAUSED:
            raise NocError('invalid_status', 'Only a paused correspondent can be resumed.')
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
        notice_id: Any = None,
        key_id: Any = None,
        change_code: Any = None,
        waive_fee: bool = False,
    ) -> Dict[str, Any]:
        self._require_respond(actor_type)
        notice = None
        key = None
        if notice_id:
            notice = self.get_notice(notice_id=str(notice_id).strip(), actor=actor, actor_type=actor_type)
            if notice.userid and notice.userid != owner_userid and actor_type not in EMPLOYEE_ROLES:
                raise NocError('noc_forbidden', 'Notice does not belong to this customer.')
            if notice.key_id:
                key = self.store.get_key(notice.key_id)
        elif key_id:
            key = self.enforce_key(key_id=str(key_id).strip(), actor=actor, actor_type=actor_type)
            if key.userid != owner_userid:
                raise NocError('noc_forbidden', 'Key does not belong to this customer.')
        code = normalize_change(change_code or (notice.change_code if notice else CHANGE_C01))
        now = float(self.clock())
        screen_target = notice.rdfi_name if notice else (key.legal_name if key else '')
        ofac = self._screen(screen_target, aliases=(notice.nickname,) if notice else ())
        fee = compute_fee(self.policy.fee_amount, waived=waive_fee or (notice.fee_status == FEE_WAIVED if notice else False))
        return {
            'change_code': code,
            'fee': money_str(fee),
            'dual_control': self._needs_dual_control(code),
            'ofac': ofac.to_dict(),
            'key': key.to_dict() if key else None,
            'notice': notice.to_dict() if notice else None,
            'clock': self.calendar.snapshot(now),
            'network_open': self.calendar.is_open(now),
            'message_type': MSG_CORA,
        }

    def get_notice(self, *, notice_id: str, actor: str, actor_type: str) -> NocNotice:
        self._require_enabled()
        row = self.store.get_notice(notice_id)
        if row is None:
            raise NocError('inquiry_not_found', 'NOC notice not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid and row.userid != actor:
            raise NocError('noc_forbidden', 'Not allowed to view this notice.')
        return row

    def _evaluate_status(self, *, key: Optional[NocKey], ofac: ScreenResult) -> str:
        if key is None:
            return EVT_UNMATCHED
        if ofac.hit:
            return EVT_HELD
        return EVT_OPEN

    def _attach_key(self, notice: NocNotice, key: NocKey) -> None:
        notice.key_id = key.key_id
        notice.userid = key.userid
        notice.funding_account = key.funding_account
        notice.nickname = key.nickname
        notice.legal_name = key.legal_name

    def ingest_notice(
        self,
        *,
        actor: str,
        actor_type: str,
        trace: Any,
        account: Any,
        aba: Any,
        change_code: Any = CHANGE_C01,
        corrected: Any = '',
        amount: Any = '0.00',
        rdfi_name: Any = '',
        correspondent_id: Any = None,
        message_type: Any = MSG_COR,
        original_sec: Any = 'PPD',
        tran_code: Any = '22',
        waive_fee: bool = False,
        note: Any = '',
    ) -> Tuple[NocNotice, bool]:
        self._require_staff(actor_type)
        trace_id = normalize_trace(trace)
        existing = self.store.get_notice_by_trace(trace_id)
        if existing is not None:
            return existing, False
        msg = normalize_message_type(message_type)
        if msg != MSG_COR:
            raise NocError('invalid_message', 'Inbound ingest must be COR.')
        dest = normalize_dest_account(account)
        dest_aba = normalize_rdfi_aba(aba)
        fingerprint = dest_fingerprint(dest_aba, dest)
        code = normalize_change(change_code)
        parsed = parse_corrected(code, corrected)
        money = parse_money(amount, allow_zero=True)
        corr = None
        if correspondent_id:
            corr = self.get_correspondent(correspondent_id=str(correspondent_id).strip(), actor_type=actor_type)
        else:
            corr = self.store.find_correspondent_by_aba(dest_aba)
        if corr is not None and corr.status == CORR_PAUSED:
            raise NocError('correspondent_paused', 'Correspondent is paused.')
        rdfi_routing = corr.aba if corr is not None else dest_aba
        requestor = normalize_legal_name(rdfi_name or (corr.legal_name if corr else 'Receiving DFI'))
        key = self.store.find_key_by_fingerprint(fingerprint)
        if key is not None and key.status == KEY_PAUSED:
            key = None
        ofac = self._screen(requestor, aliases=(corr.nickname,) if corr else ())
        now = float(self.clock())
        if len(self.store.list_notices()) >= self.policy.max_notices:
            raise NocError('inquiry_limit', 'NOC notice limit reached.')
        status = self._evaluate_status(key=key, ofac=ofac)
        notice = NocNotice(
            notice_id=uuid.uuid4().hex,
            trace_id=trace_id,
            key_id=key.key_id if key else '',
            correspondent_id=corr.correspondent_id if corr else '',
            userid=key.userid if key else '',
            funding_account=key.funding_account if key else '',
            nickname=key.nickname if key else '',
            legal_name=key.legal_name if key else '',
            dest_account=dest,
            dest_aba=dest_aba,
            fingerprint=fingerprint,
            rdfi_aba=rdfi_routing,
            rdfi_name=requestor,
            message_type=MSG_COR,
            change_code=code,
            original_sec=normalize_original_sec(original_sec),
            tran_code=normalize_tran_code(tran_code),
            corrected_data=compose_corrected(code, parsed),
            amount=money_str(money),
            status=status,
            noc_ref='',
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
        stored = self.store.put_notice(notice)
        return stored, stored.notice_id == notice.notice_id

    def ingest_file(self, *, actor: str, actor_type: str, payload: Any) -> List[NocNotice]:
        self._require_staff(actor_type)
        rows = []
        for line in split_noc1_file(payload):
            parsed = parse_noc1(line)
            row, _created = self.ingest_notice(
                actor=actor,
                actor_type=actor_type,
                trace=parsed['trace'],
                account=parsed['account'],
                aba=parsed['aba'],
                change_code=parsed['change_code'],
                corrected=parsed['corrected'],
                amount=parsed['amount'] or '0.00',
                rdfi_name=parsed['name'],
                message_type=parsed['message_type'] or MSG_COR,
                original_sec=parsed['original_sec'] or 'PPD',
                tran_code=parsed['tran_code'] or '22',
            )
            rows.append(row)
        return rows

    def _rematch_unmatched(self, key: NocKey) -> None:
        if key.status != KEY_ACTIVE:
            return
        for row in self.store.list_unmatched():
            if row.fingerprint != key.fingerprint:
                continue
            ofac = ScreenResult(bool(row.ofac_hit), row.ofac_match, 90 if row.ofac_hit else 0)
            self._attach_key(row, key)
            row.status = self._evaluate_status(key=key, ofac=ofac)
            row.updated_at = float(self.clock())
            self.store.update_notice(row)

    def claim(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        notice_id: Any = None,
        account: Any = None,
        last4_digits: Any = None,
        dest_aba: Any = None,
        funding_account: Any = None,
        nickname: Any = None,
        legal_name: Any = None,
    ) -> NocNotice:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise NocError('noc_forbidden', 'Not allowed to claim for this customer.')
        target = None
        if notice_id:
            target = self.store.get_notice(str(notice_id).strip())
            if target is None:
                raise NocError('inquiry_not_found', 'NOC notice not found.')
            if target.status != EVT_UNMATCHED:
                raise NocError('not_claimable', 'Notice is not unmatched.')
        else:
            needle = normalize_dest_account(account) if account else ''
            last = ''.join(ch for ch in str(last4_digits or '') if ch.isdigit())
            for row in self.store.list_unmatched():
                if needle and row.dest_account == needle:
                    target = row
                    break
                if last and last4(row.dest_account) == last[-4:]:
                    target = row
                    break
            if target is None:
                raise NocError('inquiry_not_found', 'No unmatched NOC matches that destination.')
        funding = normalize_account(funding_account or target.funding_account or '')
        if not funding:
            raise NocError('invalid_account', 'Funding account is required to claim.')
        self._assert_internal_account(owner_userid, funding)
        key = self.store.find_key_by_fingerprint(target.fingerprint, userid=owner_userid)
        if key is None:
            key = self.add_key(
                owner_userid=owner_userid,
                actor=actor,
                actor_type=actor_type,
                nickname=nickname or ('ACH ' + last4(target.dest_account)),
                legal_name=legal_name or owner_userid,
                dest_aba=dest_aba or target.dest_aba,
                dest_account=target.dest_account,
                funding_account=funding,
                tran_code=target.tran_code,
                original_sec=target.original_sec,
            )
        if key.status != KEY_ACTIVE:
            raise NocError('key_paused', 'Key is paused.')
        ofac = ScreenResult(bool(target.ofac_hit), target.ofac_match, 90 if target.ofac_hit else 0)
        if not target.ofac_hit:
            ofac = self._screen(target.rdfi_name)
        self._attach_key(target, key)
        target.ofac_hit = 1 if ofac.hit else 0
        target.ofac_match = ofac.matched
        target.status = self._evaluate_status(key=key, ofac=ofac)
        target.updated_at = float(self.clock())
        self.store.update_notice(target)
        return target

    def assign(
        self,
        *,
        notice_id: str,
        actor: str,
        actor_type: str,
        owner_userid: str,
        key_id: Any = None,
        nickname: Any = None,
        legal_name: Any = None,
        funding_account: Any = None,
    ) -> NocNotice:
        self._require_staff(actor_type)
        row = self.store.get_notice(notice_id)
        if row is None:
            raise NocError('inquiry_not_found', 'NOC notice not found.')
        if row.status != EVT_UNMATCHED:
            raise NocError('not_assignable', 'Notice is not unmatched.')
        key = None
        if key_id:
            key = self.get_key(key_id=str(key_id).strip(), actor=actor, actor_type=actor_type)
            if key.userid != owner_userid:
                raise NocError('noc_forbidden', 'Key does not belong to this customer.')
        if key is None:
            key = self.store.find_key_by_fingerprint(row.fingerprint, userid=owner_userid)
        return self.claim(
            owner_userid=owner_userid,
            actor=actor,
            actor_type=actor_type,
            notice_id=row.notice_id,
            funding_account=funding_account or (key.funding_account if key else None),
            nickname=(key.nickname if key else nickname),
            legal_name=(key.legal_name if key else legal_name),
        )

    def apply(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        notice_id: Any,
        note: Any = '',
    ) -> Tuple[NocNotice, bool]:
        self._require_respond(actor_type)
        row = self.get_notice(notice_id=str(notice_id or '').strip(), actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != owner_userid:
            raise NocError('noc_forbidden', 'Notice does not belong to this customer.')
        if row.status == EVT_POSTED and row.response_text == 'APPLIED':
            return row, False
        if row.status != EVT_OPEN:
            if row.status == EVT_HELD:
                raise NocError('ofac_hold', 'OFAC hold must be overridden before applying the NOC.')
            if row.status == EVT_PENDING:
                raise NocError('not_respondable', 'NOC is awaiting release.')
            if row.status == EVT_QUEUED:
                raise NocError('not_respondable', 'NOC is queued for the ACH window.')
            raise NocError('not_respondable', 'Notice is not open to apply.')
        if not row.key_id:
            raise NocError('not_respondable', 'Notice is not matched to a key.')
        self.enforce_key(key_id=row.key_id, actor=actor, actor_type=actor_type)
        now = float(self.clock())
        row.response_text = 'APPLIED'
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.updated_at = now
        if self._needs_dual_control(row.change_code):
            row.status = EVT_PENDING
            self.store.update_notice(row)
            return row, True
        if not self.calendar.is_open(now):
            row.status = EVT_QUEUED
            self.store.update_notice(row)
            return row, True
        self._transmit(row, actor=actor)
        return row, True

    def refuse(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        notice_id: Any,
        refuse_code: Any = REFUSE_C65,
        note: Any = '',
    ) -> Tuple[NocNotice, bool]:
        self._require_respond(actor_type)
        row = self.get_notice(notice_id=str(notice_id or '').strip(), actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != owner_userid:
            raise NocError('noc_forbidden', 'Notice does not belong to this customer.')
        if row.status == EVT_REFUSED:
            return row, False
        if row.status != EVT_OPEN:
            if row.status == EVT_HELD:
                raise NocError('ofac_hold', 'OFAC hold must be overridden before refusing.')
            raise NocError('not_respondable', 'Notice is not open to refuse.')
        if not row.key_id:
            raise NocError('not_respondable', 'Notice is not matched to a key.')
        self.enforce_key(key_id=row.key_id, actor=actor, actor_type=actor_type)
        code = normalize_refuse(refuse_code)
        now = float(self.clock())
        cycle = row.value_date or self.calendar.cycle_date(now)
        seq = self.store.next_noc_sequence(cycle)
        row.noc_ref = compose_noc_ref(cycle, seq)
        row.response_text = 'REFUSED ' + code
        row.message_type = MSG_CORR
        row.status = EVT_REFUSED
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.releaser = str(actor)
        row.posted_at = now
        row.updated_at = now
        self.store.update_notice(row)
        return row, True

    def _apply_correction(self, key: NocKey, notice: NocNotice) -> None:
        parsed = parse_corrected(notice.change_code, notice.corrected_data)
        notice.prior_dest_account = key.dest_account
        notice.prior_dest_aba = key.dest_aba
        notice.prior_legal_name = key.legal_name
        notice.prior_tran_code = key.tran_code
        notice.prior_fingerprint = key.fingerprint
        notice.prior_original_sec = key.original_sec
        if 'account' in parsed:
            key.dest_account = parsed['account']
        if 'aba' in parsed:
            key.dest_aba = parsed['aba']
        if 'name' in parsed:
            key.legal_name = parsed['name']
        if 'tran_code' in parsed:
            key.tran_code = parsed['tran_code']
        if 'sec' in parsed:
            key.original_sec = parsed['sec']
        key.fingerprint = dest_fingerprint(key.dest_aba, key.dest_account)
        collision = self.store.find_key_by_fingerprint(key.fingerprint)
        if collision is not None and collision.key_id != key.key_id:
            raise NocError('key_duplicate', 'Corrected destination collides with another alias.')
        key.updated_at = float(self.clock())
        key.actor = notice.actor
        self.store.update_key(key)

    def _restore_key(self, notice: NocNotice) -> None:
        if not notice.key_id or not notice.prior_fingerprint:
            return
        key = self.store.get_key(notice.key_id)
        if key is None:
            return
        key.dest_account = notice.prior_dest_account or key.dest_account
        key.dest_aba = notice.prior_dest_aba or key.dest_aba
        key.legal_name = notice.prior_legal_name or key.legal_name
        key.tran_code = notice.prior_tran_code or key.tran_code
        key.original_sec = notice.prior_original_sec or key.original_sec
        key.fingerprint = notice.prior_fingerprint
        key.updated_at = float(self.clock())
        self.store.update_key(key)

    def _transmit(self, notice: NocNotice, *, actor: str) -> NocNotice:
        now = float(self.clock())
        cycle = notice.value_date or self.calendar.cycle_date(now)
        seq = self.store.next_noc_sequence(cycle)
        notice.noc_ref = compose_noc_ref(cycle, seq)
        waived = notice.fee_status == FEE_WAIVED
        fee = compute_fee(self.policy.fee_amount, waived=waived)
        status = EVT_POSTED
        fail_note = ''
        if fee > 0 and self.debit_fn is not None and notice.funding_account:
            try:
                result = self.debit_fn(
                    notice.funding_account, money_str(fee), 'noc fee %s' % notice.noc_ref[:12],
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
                    notice.fee = money_str(fee)
                    notice.fee_status = FEE_COLLECTED
        elif waived:
            notice.fee = money_str(Decimal('0.00'))
            notice.fee_status = FEE_WAIVED
        notice.status = status
        notice.updated_at = now
        if status == EVT_POSTED:
            key = self.store.get_key(notice.key_id) if notice.key_id else None
            if key is not None:
                self._apply_correction(key, notice)
            notice.posted_at = now
            notice.releaser = str(actor)
            notice.message_type = MSG_CORA
            notice.response_text = 'APPLIED'
        else:
            notice.noc_ref = ''
            notice.note = fail_note or notice.note
            self.store.update_notice(notice)
            if status == EVT_NSF:
                raise NocError('nsf', 'Insufficient funds to collect the NOC fee.', inquiry=notice)
            raise NocError('failed', 'NOC apply debit failed.', inquiry=notice)
        self.store.update_notice(notice)
        return notice

    def cancel_notice(
        self,
        *,
        notice_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> NocNotice:
        self._require_manage(actor_type)
        row = self.get_notice(notice_id=notice_id, actor=actor, actor_type=actor_type)
        if row.status not in CANCELABLE:
            raise NocError('not_cancelable', 'Notice cannot be cancelled.')
        row.status = EVT_CANCELLED
        row.actor = str(actor)
        row.note = normalize_note(note) or row.note
        row.updated_at = float(self.clock())
        self.store.update_notice(row)
        return row

    def release_notice(self, *, notice_id: str, actor: str, actor_type: str) -> NocNotice:
        self._require_staff(actor_type)
        row = self.get_notice(notice_id=notice_id, actor=actor, actor_type=actor_type)
        if row.status == EVT_HELD:
            raise NocError('ofac_hold', 'OFAC hold must be overridden before release.')
        if row.status != EVT_PENDING:
            raise NocError('not_releasable', 'Notice is not pending release.')
        if row.actor and row.actor == actor:
            raise NocError('same_approver', 'A different employee must release this NOC.')
        if row.response_text != 'APPLIED':
            raise NocError('not_releasable', 'No applied NOC to release.')
        now = float(self.clock())
        if not self.calendar.is_open(now):
            row.status = EVT_QUEUED
            row.updated_at = now
            self.store.update_notice(row)
            return row
        return self._transmit(row, actor=actor)

    def reject_notice(
        self,
        *,
        notice_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> NocNotice:
        self._require_staff(actor_type)
        row = self.get_notice(notice_id=notice_id, actor=actor, actor_type=actor_type)
        if row.status not in OPEN_EVENTS:
            raise NocError('not_rejectable', 'Notice cannot be rejected.')
        row.status = EVT_REJECTED
        row.actor = str(actor)
        row.note = normalize_note(note) or normalize_note(reason) or row.note
        row.updated_at = float(self.clock())
        self.store.update_notice(row)
        return row

    def override_ofac(
        self,
        *,
        notice_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> NocNotice:
        self._require_staff(actor_type)
        row = self.get_notice(notice_id=notice_id, actor=actor, actor_type=actor_type)
        if row.status != EVT_HELD:
            raise NocError('not_overridable', 'Notice is not on an OFAC hold.')
        row.ofac_hit = 0
        row.ofac_match = ''
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        key = self.store.get_key(row.key_id) if row.key_id else None
        row.status = self._evaluate_status(key=key, ofac=ScreenResult(False, '', 0))
        self.store.update_notice(row)
        return row

    def waive_fee(self, *, notice_id: str, actor: str, actor_type: str) -> NocNotice:
        self._require_staff(actor_type)
        row = self.get_notice(notice_id=notice_id, actor=actor, actor_type=actor_type)
        if row.status == EVT_POSTED:
            raise NocError('not_exportable', 'Fee already posted.')
        row.fee = money_str(Decimal('0.00'))
        row.fee_status = FEE_WAIVED
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_notice(row)
        return row

    def reverse_notice(
        self,
        *,
        notice_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
        customer_request: bool = False,
    ) -> NocNotice:
        if customer_request:
            self._require_respond(actor_type)
        else:
            self._require_staff(actor_type)
        row = self.get_notice(notice_id=notice_id, actor=actor, actor_type=actor_type)
        if row.status == EVT_REVERSED:
            raise NocError('already_reversed', 'NOC is already reversed.')
        if row.status != EVT_POSTED:
            raise NocError('not_reversible', 'Only a posted CORA apply can be reversed.')
        now = float(self.clock())
        posted_at = row.posted_at or row.updated_at
        window = CUSTOMER_REVERSE_SECONDS if customer_request else STAFF_REVERSE_SECONDS
        if now - posted_at > window:
            raise NocError('return_window_closed', 'Reverse window has closed.')
        if row.fee_status == FEE_COLLECTED and self.credit_fn is not None and row.funding_account:
            amount = parse_money(row.fee, allow_zero=True)
            if amount > 0:
                try:
                    result = self.credit_fn(
                        row.funding_account, money_str(amount),
                        'noc fee reverse %s' % (row.noc_ref[:12] or row.notice_id[:12]),
                    )
                except Exception as exc:
                    raise NocError('reverse_failed', str(exc)[:240], inquiry=row) from exc
                if _classify_money_result(result) != 'ok':
                    raise NocError('reverse_failed', 'Unable to credit NOC fee.', inquiry=row)
        self._restore_key(row)
        row.status = EVT_REVERSED
        row.reversed_at = now
        row.updated_at = now
        row.actor = str(actor)
        row.note = normalize_note(note) or row.note
        self.store.update_notice(row)
        return row

    def export_file(self, *, actor: str, actor_type: str, userid: Optional[str] = None) -> str:
        self._require_staff(actor_type)
        rows = self.store.list_notices(userid)
        lines = []
        for row in rows:
            if row.status not in {EVT_POSTED, EVT_OPEN, EVT_PENDING, EVT_HELD, EVT_REFUSED, EVT_QUEUED}:
                continue
            lines.append(compose_noc1(
                message_type=row.message_type,
                trace=mask_trace(row.trace_id),
                change_code=row.change_code,
                aba=mask_aba(row.dest_aba),
                account=mask_account(row.dest_account),
                corrected=mask_corrected(row.change_code, row.corrected_data) if row.corrected_data else '',
                name=row.rdfi_name,
                original_sec=row.original_sec,
                tran_code=row.tran_code,
                amount=row.amount,
                status=row.status,
            ))
        if not lines:
            raise NocError('not_exportable', 'No NOC notices to export.')
        return '\n'.join(lines)

    def run_due(self, userid: Optional[str] = None) -> List[NocNotice]:
        now = float(self.clock())
        changed: List[NocNotice] = []
        for row in self.store.list_notices(userid):
            age = now - row.created_at
            if row.status == EVT_UNMATCHED and age >= UNMATCHED_TTL_SECONDS:
                row.status = EVT_EXPIRED
                row.updated_at = now
                self.store.update_notice(row)
                changed.append(row)
            elif row.status == EVT_OPEN and age >= OPEN_TTL_SECONDS:
                row.status = EVT_EXPIRED
                row.updated_at = now
                self.store.update_notice(row)
                changed.append(row)
            elif row.status == EVT_QUEUED and row.response_text == 'APPLIED' and self.calendar.is_open(now):
                try:
                    transmitted = self._transmit(row, actor=row.actor or 'system')
                except NocError:
                    changed.append(row)
                else:
                    changed.append(transmitted)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.run_due(userid)
        keys = self.store.list_keys(userid)
        notices = self.store.list_notices(userid)
        fees_ytd = Decimal('0.00')
        posted = 0
        refused = 0
        open_count = 0
        for row in notices:
            if row.status == EVT_POSTED:
                posted += 1
                if row.fee_status == FEE_COLLECTED:
                    fees_ytd += parse_money(row.fee, allow_zero=True)
            if row.status == EVT_REFUSED:
                refused += 1
            if row.status in OPEN_EVENTS:
                open_count += 1
        now = float(self.clock())
        payload = {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'fee': money_str(self.policy.fee_amount),
            'clock': self.calendar.snapshot(now),
            'keys': [row.to_dict() for row in keys[:40]],
            'notices': [row.to_dict() for row in notices[:40]],
            'inquiries': [row.to_dict() for row in notices[:40]],
            'correspondents': [],
            'unmatched': [],
            'ytd_fees': money_str(fees_ytd),
            'applied_count': posted,
            'refused_count': refused,
            'active_count': sum(1 for row in keys if row.status == KEY_ACTIVE),
            'open_count': open_count,
        }
        if actor_type in EMPLOYEE_ROLES:
            payload['correspondents'] = [row.to_dict() for row in self.store.list_correspondents()[:40]]
            payload['unmatched'] = [row.to_dict() for row in self.store.list_unmatched()[:40]]
        return payload


_SERVICE: Optional[NocService] = None


def set_service(service: Optional[NocService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[NocService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('NOC_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryNocStore()
    path = os.environ.get('NOC_DB', DEFAULT_STORE_PATH)
    return SqliteNocStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[AchClock] = None,
) -> NocService:
    if store is None:
        store = default_store()
    return NocService(
        NocPolicy.from_env(),
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
        'noc_forbidden': 403,
        'noc_disabled': 403,
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
        'invalid_trace': 400,
        'invalid_reason': 400,
        'invalid_message': 400,
        'invalid_response': 400,
        'invalid_status': 400,
        'invalid_reference': 400,
        'invalid_file': 400,
        'invalid_corrected': 400,
        'invalid_sec': 400,
        'invalid_tran': 400,
        'missing_customer_id': 400,
        'missing_key': 400,
        'missing_inquiry': 400,
        'missing_correspondent': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: NocError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('inquiry') is not None:
        body['notice'] = exc.extra['inquiry'].to_dict()
        body['inquiry'] = body['notice']
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
    except NocError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snap(service: NocService, owner: str, userid: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(owner, actor=userid, actor_type=actor_type)


def _notice_id(values: Dict[str, Any]) -> str:
    return str(values.get('notice_id') or values.get('inquiry_id') or values.get('noc_id') or '').strip()


def handle_list(service: NocService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'AchNoc': _snap(service, owner, userid, actor_type)}), 200


def handle_add_key(service: NocService):
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
            dest_aba=values.get('dest_aba') or values.get('aba') or values.get('routing'),
            dest_account=values.get('dest_account') or values.get('account') or values.get('account_number'),
            funding_account=values.get('funding_account') or values.get('default_account') or values.get('from_account'),
            tran_code=values.get('tran_code') or '22',
            original_sec=values.get('original_sec') or values.get('sec') or 'PPD',
        )
        return jsonify({
            'message': 'ACH destination enrolled',
            'key': row.to_dict(),
            'AchNoc': _snap(service, owner, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _key_status_route(service: NocService, status: str, ok_message: str):
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
            'AchNoc': _snap(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: NocService):
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
            notice_id=_notice_id(values),
            key_id=values.get('key_id'),
            change_code=values.get('change_code') or values.get('reason'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({'preview': preview, 'AchNoc': _snap(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_apply(service: NocService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    notice_id = _notice_id(values)
    if not notice_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inquiry'}), 400

    def _run():
        row, created = service.apply(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            notice_id=notice_id,
            note=values.get('note') or values.get('response') or '',
        )
        return jsonify({
            'message': 'NOC applied' if created else 'NOC already posted',
            'notice': row.to_dict(),
            'inquiry': row.to_dict(),
            'AchNoc': _snap(service, owner, userid, actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_refuse(service: NocService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    notice_id = _notice_id(values)
    if not notice_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inquiry'}), 400

    def _run():
        row, created = service.refuse(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            notice_id=notice_id,
            refuse_code=values.get('refuse') or values.get('decline') or values.get('reason') or REFUSE_C65,
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'NOC refused' if created else 'Refusal already posted',
            'notice': row.to_dict(),
            'inquiry': row.to_dict(),
            'AchNoc': _snap(service, owner, userid, actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: NocService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    notice_id = _notice_id(values)
    if not notice_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inquiry'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.cancel_notice(
            notice_id=notice_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': 'NOC cancelled',
            'notice': row.to_dict(),
            'AchNoc': _snap(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_claim(service: NocService):
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
            notice_id=_notice_id(values),
            account=values.get('account') or values.get('account_number') or values.get('dest_account'),
            last4_digits=values.get('last4') or values.get('account_last4'),
            dest_aba=values.get('dest_aba') or values.get('aba'),
            funding_account=values.get('funding_account') or values.get('default_account'),
            nickname=values.get('nickname'),
            legal_name=values.get('legal_name'),
        )
        return jsonify({
            'message': 'Notice claimed',
            'notice': row.to_dict(),
            'AchNoc': _snap(service, owner, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_add_correspondent(service: NocService):
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
            'AchNoc': _snap(service, owner, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _corr_status_route(service: NocService, status: str, ok_message: str):
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
            'AchNoc': _snap(service, owner, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_ingest(service: NocService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row, created = service.ingest_notice(
            actor=userid,
            actor_type=actor_type,
            trace=values.get('trace') or values.get('trace_id') or values.get('e2e'),
            account=values.get('account') or values.get('account_number') or values.get('dest_account'),
            aba=values.get('aba') or values.get('routing') or values.get('dest_aba'),
            change_code=values.get('change_code') or values.get('reason'),
            corrected=values.get('corrected') or values.get('corrected_data'),
            amount=values.get('amount') or '0.00',
            rdfi_name=values.get('rdfi_name') or values.get('requestor_name') or values.get('name'),
            correspondent_id=values.get('correspondent_id'),
            message_type=values.get('message_type') or MSG_COR,
            original_sec=values.get('original_sec') or values.get('sec') or 'PPD',
            tran_code=values.get('tran_code') or '22',
            waive_fee=bool(values.get('waive_fee')),
            note=values.get('note') or '',
        )
        owner = row.userid or str(values.get('customer_id') or userid)
        return jsonify({
            'message': 'Notice ingested' if created else 'Notice already posted',
            'notice': row.to_dict(),
            'inquiry': row.to_dict(),
            'AchNoc': _snap(service, owner, userid, actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_file(service: NocService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    payload = values.get('file') or values.get('payload') or values.get('noc1')
    if not payload:
        return jsonify({'message': 'Some data missing', 'error': 'missing_file'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        rows = service.ingest_file(actor=userid, actor_type=actor_type, payload=payload)
        owner = str(values.get('customer_id') or userid)
        return jsonify({
            'message': 'NOC1 file ingested',
            'count': len(rows),
            'notices': [row.to_dict() for row in rows],
            'inquiries': [row.to_dict() for row in rows],
            'AchNoc': _snap(service, owner, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def handle_assign(service: NocService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    notice_id = _notice_id(values)
    if not notice_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inquiry'}), 400
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.assign(
            notice_id=notice_id,
            actor=userid,
            actor_type=actor_type,
            owner_userid=owner,
            key_id=values.get('key_id'),
            nickname=values.get('nickname'),
            legal_name=values.get('legal_name'),
            funding_account=values.get('funding_account') or values.get('default_account') or values.get('account'),
        )
        return jsonify({
            'message': 'Notice assigned',
            'notice': row.to_dict(),
            'AchNoc': _snap(service, owner, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_notice_route(service: NocService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    notice_id = _notice_id(values)
    if not notice_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inquiry'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'release':
            row = service.release_notice(notice_id=notice_id, actor=userid, actor_type=actor_type)
            message = 'NOC released'
        elif action == 'reject':
            row = service.reject_notice(
                notice_id=notice_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'NOC rejected'
        elif action == 'reverse':
            row = service.reverse_notice(
                notice_id=notice_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'NOC reversed'
        elif action == 'customer_reverse':
            row = service.reverse_notice(
                notice_id=notice_id, actor=userid, actor_type=actor_type,
                note=values.get('note') or '', customer_request=True,
            )
            message = 'Reverse requested'
        elif action == 'override':
            row = service.override_ofac(
                notice_id=notice_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'waive':
            row = service.waive_fee(notice_id=notice_id, actor=userid, actor_type=actor_type)
            message = 'NOC fee waived'
        else:
            raise NocError('invalid_status', 'Unknown NOC action.')
        return jsonify({
            'message': message,
            'notice': row.to_dict(),
            'inquiry': row.to_dict(),
            'AchNoc': _snap(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_export(service: NocService):
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
            'message': 'NOC1 exported',
            'file': payload,
            'AchNoc': _snap(service, owner or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_unmatched(service: NocService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'

    def _run():
        service._require_staff(actor_type)
        rows = service.store.list_unmatched()
        return jsonify({
            'unmatched': [row.to_dict() for row in rows[:40]],
            'AchNoc': _snap(service, userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: NocService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'AchNoc': _snap(service, owner, userid, actor_type)}), 200


def attach_noc_routes(app, service: NocService) -> None:
    @app.route('/listAchNoc', methods=['POST', 'GET'])
    def list_ach_noc_route():
        return handle_list(service)

    @app.route('/listAchNocKeys', methods=['POST', 'GET'])
    def list_ach_noc_keys_route():
        return handle_list(service)

    @app.route('/listUnmatchedAchNoc', methods=['POST', 'GET'])
    def list_unmatched_ach_noc_route():
        return handle_unmatched(service)

    @app.route('/addAchNocKey', methods=['POST', 'GET'])
    def add_ach_noc_key_route():
        return handle_add_key(service)

    @app.route('/pauseAchNocKey', methods=['POST', 'GET'])
    def pause_ach_noc_key_route():
        return _key_status_route(service, KEY_PAUSED, 'Alias paused')

    @app.route('/resumeAchNocKey', methods=['POST', 'GET'])
    def resume_ach_noc_key_route():
        return _key_status_route(service, KEY_ACTIVE, 'Alias resumed')

    @app.route('/cancelAchNocKey', methods=['POST', 'GET'])
    def cancel_ach_noc_key_route():
        return _key_status_route(service, KEY_CANCELLED, 'Alias cancelled')

    @app.route('/previewAchNoc', methods=['POST', 'GET'])
    def preview_ach_noc_route():
        return handle_preview(service)

    @app.route('/applyAchNoc', methods=['POST', 'GET'])
    def apply_ach_noc_route():
        return handle_apply(service)

    @app.route('/refuseAchNoc', methods=['POST', 'GET'])
    def refuse_ach_noc_route():
        return handle_refuse(service)

    @app.route('/cancelAchNoc', methods=['POST', 'GET'])
    def cancel_ach_noc_route():
        return handle_cancel(service)

    @app.route('/claimAchNoc', methods=['POST', 'GET'])
    def claim_ach_noc_route():
        return handle_claim(service)

    @app.route('/requestAchNocReverse', methods=['POST', 'GET'])
    def request_ach_noc_reverse_route():
        return _staff_notice_route(service, 'customer_reverse')

    @app.route('/addAchNocCorrespondent', methods=['POST', 'GET'])
    def add_ach_noc_correspondent_route():
        return handle_add_correspondent(service)

    @app.route('/listAchNocCorrespondents', methods=['POST', 'GET'])
    def list_ach_noc_correspondents_route():
        return handle_list(service)

    @app.route('/pauseAchNocCorrespondent', methods=['POST', 'GET'])
    def pause_ach_noc_correspondent_route():
        return _corr_status_route(service, CORR_PAUSED, 'Correspondent paused')

    @app.route('/resumeAchNocCorrespondent', methods=['POST', 'GET'])
    def resume_ach_noc_correspondent_route():
        return _corr_status_route(service, CORR_ACTIVE, 'Correspondent resumed')

    @app.route('/archiveAchNocCorrespondent', methods=['POST', 'GET'])
    def archive_ach_noc_correspondent_route():
        return _corr_status_route(service, CORR_ARCHIVED, 'Correspondent archived')

    @app.route('/ingestAchNoc', methods=['POST', 'GET'])
    def ingest_ach_noc_route():
        return handle_ingest(service)

    @app.route('/ingestAchNocFile', methods=['POST', 'GET'])
    def ingest_ach_noc_file_route():
        return handle_ingest_file(service)

    @app.route('/assignAchNoc', methods=['POST', 'GET'])
    def assign_ach_noc_route():
        return handle_assign(service)

    @app.route('/overrideAchNocOfac', methods=['POST', 'GET'])
    def override_ach_noc_ofac_route():
        return _staff_notice_route(service, 'override')

    @app.route('/releaseAchNoc', methods=['POST', 'GET'])
    def release_ach_noc_route():
        return _staff_notice_route(service, 'release')

    @app.route('/rejectAchNoc', methods=['POST', 'GET'])
    def reject_ach_noc_route():
        return _staff_notice_route(service, 'reject')

    @app.route('/reverseAchNoc', methods=['POST', 'GET'])
    def reverse_ach_noc_route():
        return _staff_notice_route(service, 'reverse')

    @app.route('/waiveAchNocFee', methods=['POST', 'GET'])
    def waive_ach_noc_fee_route():
        return _staff_notice_route(service, 'waive')

    @app.route('/exportAchNoc', methods=['POST', 'GET'])
    def export_ach_noc_route():
        return handle_export(service)

    @app.route('/runDueAchNoc', methods=['POST', 'GET'])
    def run_due_ach_noc_route():
        return handle_run_due(service)
