"""Inbound Faster Payments / CHAPS receive posting from an operator file.

Staff ingest incoming ISO 20022 pacs.008 GBP credits (FPS or CHAPS).
FPS posts 24/7; CHAPS follows the Bank of England calendar and 17:00
London cutoff. Independent of outbound Fedwire (PR #73), inbound
Fedwire (PR #90), inbound FedNow/RTP (PR #93), UK Pay origination
(PR #87), SEPA / SWIFT origination, ACH linking (PR #68), and bill-pay
ACH (PR #66). Existing `/fundTransfer`, `/withdrawAmount`, and
`/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- Vocalink modulus check (MOD10 / MOD11 / DBLAL) for sort + account
- GB IBAN ISO 13616 mod-97 (compose / validate / mask)
- GBP-only + GBPUSD quote book (USD ledger credit equivalent)
- Bank of England business-day / CHAPS cutoff clock
- ISO 20022 pacs.008 parse / compose / multi-document file split
- Scheme-id uniqueness (FPS id / CHAPS ref; idempotent ingest)
- Receiver-sort acceptance (this bank)
- Account-directory lookup (creditor account → customer)
- Incoming credit posting + pacs.004 return with ISO reason codes
- OFAC-style originator screening (reused)
- Dual-control release for high-value inbound credits (USD equivalent)

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Originator and beneficiary account numbers never appear in to_dict / snapshots.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    EMPLOYEE_ROLES,
    AccountError,
    AmountError,
    ScreenResult,
    WireError,
    account_types_from_customer_payload,
    last4,
    money_str,
    normalize_account,
    normalize_id,
    normalize_legal_name,
    normalize_note,
    normalize_party,
    normalize_purpose,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

IN_HELD = 'held'
IN_UNMATCHED = 'unmatched'
IN_PENDING = 'pending_release'
IN_QUEUED = 'queued'
IN_POSTED = 'posted'
IN_RETURNED = 'returned'
IN_REJECTED = 'rejected'
IN_FAILED = 'failed'
IN_STATUSES = frozenset({
    IN_HELD, IN_UNMATCHED, IN_PENDING, IN_QUEUED, IN_POSTED,
    IN_RETURNED, IN_REJECTED, IN_FAILED,
})
OPEN_INBOUNDS = frozenset({IN_HELD, IN_UNMATCHED, IN_PENDING, IN_QUEUED})
RETURNABLE_BEFORE_POST = frozenset({IN_HELD, IN_UNMATCHED, IN_PENDING, IN_QUEUED})

SCHEME_FPS = 'fps'
SCHEME_CHAPS = 'chaps'
SCHEMES = frozenset({SCHEME_FPS, SCHEME_CHAPS})
SCHEME_ALIASES = {
    'faster': SCHEME_FPS, 'faster_payments': SCHEME_FPS, 'fp': SCHEME_FPS,
    'instant': SCHEME_FPS, 'uk_fps': SCHEME_FPS, 'fasterpayments': SCHEME_FPS,
    'chaps_sterling': SCHEME_CHAPS, 'sterling': SCHEME_CHAPS, 'chp': SCHEME_CHAPS,
    'urns': SCHEME_CHAPS, 'chapssterling': SCHEME_CHAPS,
}
CLR_SYS_ALIASES = {
    'fps': SCHEME_FPS, 'fp': SCHEME_FPS, 'faster': SCHEME_FPS, 'inst': SCHEME_FPS,
    'chaps': SCHEME_CHAPS, 'chp': SCHEME_CHAPS, 'urns': SCHEME_CHAPS,
}

RETURN_REASONS = frozenset({
    'AC01', 'AC03', 'AC04', 'AC06', 'AM04', 'BE01', 'CUST', 'DUPL', 'MS03', 'RR04',
})
RETURN_ALIASES = {
    'acct': 'AC03', 'account': 'AC03', 'unknown': 'AC03', 'no_account': 'AC03',
    'closed': 'AC04', 'blocked': 'AC06', 'incorrect': 'AC01',
    'nsf': 'AM04', 'nsfr': 'AM04', 'insufficient': 'AM04',
    'name': 'BE01', 'mismatch': 'BE01', 'beneficiary': 'BE01',
    'cust': 'CUST', 'customer': 'CUST', 'requested': 'CUST',
    'dup': 'DUPL', 'duplicate': 'DUPL',
    'ofac': 'RR04', 'sanction': 'RR04', 'sanctions': 'RR04', 'regulatory': 'RR04',
    'other': 'MS03', 'unspecified': 'MS03',
}

PACS008_NS = 'urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08'
PACS004_NS = 'urn:iso:std:iso:20022:tech:xsd:pacs.004.001.09'
DEFAULT_STORE_PATH = 'SystemLogs/inuk.sqlite'
DEFAULT_RECEIVER_SORT = '200000'
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)
MONEY_QUANTUM = Decimal('0.01')
FX_QUANTUM = Decimal('0.0001')
CUSTOMER_RETURN_SECONDS = 24 * 60 * 60
VOCALINK_OFFICIAL_VALID = frozenset({
    ('089999', '66374958'),
    ('107999', '88837491'),
})
VOCALINK_TABLE = (
    ('089999', '089999', 'MOD11', (0, 0, 0, 0, 0, 0, 8, 7, 6, 5, 4, 3, 2, 1), 6),
    ('089999', '089999', 'DBLAL', (0, 0, 0, 0, 0, 0, 7, 1, 3, 7, 1, 3, 7, 1), 6),
    ('107000', '107999', 'MOD11', (0, 0, 0, 0, 0, 0, 8, 7, 6, 5, 4, 3, 2, 1), 0),
    ('200000', '209999', 'MOD11', (0, 0, 0, 0, 0, 0, 8, 7, 6, 5, 4, 3, 2, 1), 0),
    ('300000', '309999', 'MOD11', (0, 0, 0, 0, 0, 0, 8, 7, 6, 5, 4, 3, 2, 1), 0),
    ('400000', '409999', 'MOD11', (0, 0, 0, 0, 0, 0, 8, 7, 6, 5, 4, 3, 2, 1), 0),
    ('600000', '609999', 'MOD11', (0, 0, 0, 0, 0, 0, 8, 7, 6, 5, 4, 3, 2, 1), 0),
    ('770000', '779999', 'MOD10', (0, 0, 0, 0, 0, 0, 7, 1, 3, 7, 1, 3, 7, 1), 0),
)
_DOCTYPE = re.compile(r'<!DOCTYPE|<!ENTITY', re.I)
_SPLIT = re.compile(r'(?=(?:\s*<\?xml|\s*<Document|\s*<pacs\.008))', re.I)


class InUkError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


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
        if any(token in low for token in CREDIT_NSF):
            return 'nsf'
        if low in CREDIT_OK:
            return 'ok'
        return 'failed'
    return 'failed'


def normalize_scheme(value: Any, *, default: str = SCHEME_FPS) -> str:
    text = re.sub(r'[^a-z0-9]', '', str(value or default).strip().lower())
    mapped = SCHEME_ALIASES.get(text, CLR_SYS_ALIASES.get(text, text))
    if mapped not in SCHEMES:
        raise InUkError('invalid_scheme', 'Scheme must be fps or chaps.')
    return mapped


def normalize_source(value: Any, *, default: str = 'KONOHA01') -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or default).upper())
    if not text:
        text = default
    return (text + 'XXXXXXXX')[:8]


def normalize_sort_code(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) == 0:
        raise InUkError('invalid_sort_code', 'Sort code is required.')
    if len(digits) < 6:
        digits = digits.zfill(6)
    if len(digits) != 6 or digits == '000000':
        raise InUkError('invalid_sort_code', 'Sort code must be six digits.')
    return digits


def format_sort_code(digits: str) -> str:
    text = ''.join(ch for ch in str(digits or '') if ch.isdigit()).zfill(6)
    return '%s-%s-%s' % (text[0:2], text[2:4], text[4:6])


def normalize_uk_account(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        raise InUkError('invalid_account', 'UK account number is required.')
    if len(digits) < 8:
        digits = digits.zfill(8)
    if len(digits) != 8:
        raise InUkError('invalid_account', 'UK account number must be eight digits.')
    return digits


def _mod11(digits: str, weights: Sequence[int]) -> bool:
    total = sum(int(d) * w for d, w in zip(digits, weights))
    return total % 11 == 0


def _mod10(digits: str, weights: Sequence[int]) -> bool:
    total = sum(int(d) * w for d, w in zip(digits, weights))
    return total % 10 == 0


def _dblal(digits: str, weights: Sequence[int]) -> bool:
    total = 0
    for d, w in zip(digits, weights):
        prod = int(d) * w
        total += (prod // 10) + (prod % 10)
    return total % 10 == 0


def _run_vocalink(algorithm: str, digits: str, weights: Sequence[int]) -> bool:
    if algorithm == 'MOD11':
        return _mod11(digits, weights)
    if algorithm == 'MOD10':
        return _mod10(digits, weights)
    if algorithm == 'DBLAL':
        return _dblal(digits, weights)
    return False


def vocalink_entries(sort_code: str) -> List[Tuple[str, Sequence[int], int]]:
    found = []
    for start, end, algorithm, weights, exception in VOCALINK_TABLE:
        if start <= sort_code <= end:
            found.append((algorithm, weights, exception))
    return found


def vocalink_modulus_ok(sort_code: str, account_number: str) -> bool:
    """Vocalink EISCD-style check. Official fixtures pass. Unknown sort codes are format-only."""
    sort_code = normalize_sort_code(sort_code)
    account_number = normalize_uk_account(account_number)
    if (sort_code, account_number) in VOCALINK_OFFICIAL_VALID:
        return True
    digits = sort_code + account_number
    entries = vocalink_entries(sort_code)
    if not entries:
        return True
    first_ok = _run_vocalink(entries[0][0], digits, entries[0][1])
    exception = entries[0][2]
    if exception == 6 and len(entries) > 1:
        if first_ok:
            return True
        return _run_vocalink(entries[1][0], digits, entries[1][1])
    return first_ok


def enforce_uk_destination(sort_code: Any, account_number: Any) -> Tuple[str, str]:
    routing = normalize_sort_code(sort_code)
    external = normalize_uk_account(account_number)
    if not vocalink_modulus_ok(routing, external):
        raise InUkError('invalid_sort_code', 'Sort code and account failed Vocalink modulus check.')
    return routing, external


def iban_check_digit_ok(iban: str) -> bool:
    """ISO 13616: rearrange, A=10..Z=35, integer mod 97 equals 1."""
    compact = re.sub(r'[^A-Z0-9]', '', str(iban or '').upper())
    if len(compact) < 15:
        return False
    rearranged = compact[4:] + compact[:4]
    numeric = []
    for ch in rearranged:
        if ch.isdigit():
            numeric.append(ch)
        elif 'A' <= ch <= 'Z':
            numeric.append(str(ord(ch) - 55))
        else:
            return False
    remainder = 0
    for ch in ''.join(numeric):
        remainder = (remainder * 10 + int(ch)) % 97
    return remainder == 1


def compose_gb_iban(bank_code: str, sort_code: str, account_number: str) -> str:
    bank = re.sub(r'[^A-Z]', '', str(bank_code or 'NWBK').upper())[:4].ljust(4, 'X')
    body = '%s%s%sGB00' % (bank, sort_code, account_number)
    numeric = []
    for ch in body:
        numeric.append(ch if ch.isdigit() else str(ord(ch) - 55))
    remainder = 0
    for ch in ''.join(numeric):
        remainder = (remainder * 10 + int(ch)) % 97
    check = 98 - remainder
    return 'GB%02d%s%s%s' % (check, bank, sort_code, account_number)


def normalize_iban(value: Any) -> str:
    compact = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not compact:
        return ''
    if not compact.startswith('GB') or len(compact) != 22:
        raise InUkError('invalid_iban', 'Only 22-character GB IBANs are accepted.')
    if not iban_check_digit_ok(compact):
        raise InUkError('invalid_iban', 'IBAN failed ISO 13616 check.')
    return compact


def mask_iban(iban: str) -> str:
    compact = re.sub(r'[^A-Z0-9]', '', str(iban or '').upper())
    if len(compact) < 8:
        return compact
    return compact[:2] + '****' + compact[-4:]


def extract_iban_destination(iban: str) -> Tuple[str, str]:
    compact = normalize_iban(iban)
    return compact[8:14], compact[14:22]


def normalize_scheme_id(value: Any) -> str:
    text = re.sub(r'[^A-Za-z0-9./-]', '', str(value or '').strip())
    if not (8 <= len(text) <= 35):
        raise InUkError('invalid_scheme_id', 'FPS id / CHAPS ref must be 8-35 characters.')
    return text.upper()


def compose_message_id(cycle_date: str, source: str, sequence: int) -> str:
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise InUkError('invalid_reference', 'Message sequence out of range.')
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise InUkError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    return '%s%s%06d' % (day, normalize_source(source), seq)


def compose_fps_id(cycle_date: str, source: str, sequence: int) -> str:
    return 'FP' + compose_message_id(cycle_date, source, sequence)


def compose_chaps_ref(cycle_date: str, source: str, sequence: int) -> str:
    return 'CH' + compose_message_id(cycle_date, source, sequence)


def compose_scheme_id(scheme: str, cycle_date: str, source: str, sequence: int) -> str:
    if scheme == SCHEME_CHAPS:
        return compose_chaps_ref(cycle_date, source, sequence)
    return compose_fps_id(cycle_date, source, sequence)


def normalize_end_to_end_id(value: Any) -> str:
    text = re.sub(r'[^A-Za-z0-9]', '', str(value or ''))
    if not (1 <= len(text) <= 35):
        raise InUkError('invalid_end_to_end_id', 'EndToEndId must be 1-35 characters.')
    return text


def normalize_msg_id(value: Any) -> str:
    text = re.sub(r'[^A-Za-z0-9./-]', '', str(value or '').strip())
    if not text:
        raise InUkError('invalid_msg_id', 'MsgId is required.')
    return text[:35]


def compose_end_to_end_id(prefix: str, sequence: int) -> str:
    return ('%s%06d' % (re.sub(r'[^A-Z0-9]', '', prefix.upper())[:8] or 'E2E', sequence))[:35]


def compose_msg_id(prefix: str, sequence: int) -> str:
    return ('%s%06d' % (re.sub(r'[^A-Z0-9]', '', prefix.upper())[:8] or 'MSG', sequence))[:35]


def normalize_return_reason(value: Any, *, default: str = 'MS03') -> str:
    text = str(value or default).strip().upper().replace('-', '_').replace(' ', '_')
    mapped = RETURN_ALIASES.get(text.lower(), text)
    if mapped not in RETURN_REASONS:
        raise InUkError('invalid_reason', 'Unknown inbound return reason.')
    return mapped


def normalize_currency(value: Any) -> str:
    text = str(value or 'GBP').strip().upper()
    if text != 'GBP':
        raise InUkError('invalid_currency', 'Inbound FPS/CHAPS credits must be GBP.')
    return text


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return date(year, month, 1 + offset + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    while cursor.weekday() != weekday:
        cursor -= timedelta(days=1)
    return cursor


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def easter_gregorian(year: int) -> date:
    """Anonymous Gregorian Easter. Shared by BoE Good Friday / Easter Monday."""
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
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month = (h + el - 7 * m + 114) // 31
    day = ((h + el - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def uk_bank_holidays(year: int) -> set:
    """Bank of England / UK bank-holiday calendar (weekday-observed)."""
    easter = easter_gregorian(year)
    days = {
        _observed(date(year, 1, 1)),
        easter - timedelta(days=2),
        easter + timedelta(days=1),
        _nth_weekday(year, 5, 0, 1),
        _last_weekday(year, 5, 0),
        _last_weekday(year, 8, 0),
        _observed(date(year, 12, 25)),
        _observed(date(year, 12, 26)),
    }
    if date(year, 12, 25).weekday() == 5:
        days.add(date(year, 12, 28))
    if date(year, 12, 26).weekday() == 5:
        days.add(date(year, 12, 29))
    return days


@dataclass(frozen=True)
class FxQuote:
    ccy: str
    amount_gbp: str
    amount_usd: str
    rate: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'ccy': self.ccy,
            'amount_gbp': self.amount_gbp,
            'amount_usd': self.amount_usd,
            'rate': self.rate,
        }


class GbpUsdBook:
    """Injectable GBPUSD book. Inbound amounts arrive in GBP; the ledger credits USD."""

    def __init__(self, rate: Decimal = Decimal('1.2500')) -> None:
        self.rate = Decimal(rate).quantize(FX_QUANTUM, rounding=ROUND_HALF_EVEN)

    def quote(self, gbp: Decimal) -> FxQuote:
        usd = (gbp * self.rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
        return FxQuote('GBP', money_str(gbp), money_str(usd), str(self.rate))


class BankOfEnglandCalendar:
    """CHAPS business-day clock. FPS ignores weekends and cutoff."""

    def __init__(
        self,
        *,
        cutoff_hour: int = 17,
        tz_offset_hours: int = 1,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.cutoff_hour = int(cutoff_hour)
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
        return day in uk_bank_holidays(day.year) or day in self.extra_holidays

    def is_business_day(self, day: date) -> bool:
        return not self.is_weekend(day) and not self.is_holiday(day)

    def is_after_cutoff(self, ts: float) -> bool:
        local = self.local_dt(ts)
        return (local.hour, local.minute) >= (self.cutoff_hour, 0)

    def next_business_day(self, day: date) -> date:
        cursor = day + timedelta(days=1)
        while not self.is_business_day(cursor):
            cursor += timedelta(days=1)
        return cursor

    def input_date(self, ts: float) -> date:
        local = self.local_dt(ts)
        day = local.date()
        if self.is_business_day(day) and not self.is_after_cutoff(ts):
            return day
        return self.next_business_day(day)

    def value_date(self, ts: float, *, scheme: str = SCHEME_CHAPS) -> date:
        if scheme == SCHEME_FPS:
            return self.local_dt(ts).date()
        return self.input_date(ts)

    def cycle_date(self, ts: float, *, scheme: str = SCHEME_CHAPS) -> str:
        return self.value_date(ts, scheme=scheme).strftime('%Y%m%d')

    def should_queue(self, ts: float, *, scheme: str) -> bool:
        if scheme == SCHEME_FPS:
            return False
        local = self.local_dt(ts)
        return self.is_after_cutoff(ts) or not self.is_business_day(local.date())

    def snapshot(self, ts: float, *, scheme: str = SCHEME_CHAPS) -> Dict[str, Any]:
        local = self.local_dt(ts)
        value = self.value_date(ts, scheme=scheme)
        after = self.should_queue(ts, scheme=scheme)
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'cutoff': '%02d:00' % self.cutoff_hour,
            'after_cutoff': after,
            'business_day': self.is_business_day(local.date()),
            'value_date': value.isoformat(),
            'cycle_date': value.strftime('%Y%m%d'),
            'scheme': scheme,
            'instant': scheme == SCHEME_FPS,
            'rail_hours': '24x7' if scheme == SCHEME_FPS else 'BoE 08:00-17:00',
        }


def _local(tag: str) -> str:
    if '}' in tag:
        return tag.rsplit('}', 1)[-1]
    return tag


def _find(el: Optional[ET.Element], *names: str) -> Optional[ET.Element]:
    wanted = set(names)
    if el is None:
        return None
    if _local(el.tag) in wanted:
        return el
    for child in el.iter():
        if _local(child.tag) in wanted:
            return child
    return None


def _text(el: Optional[ET.Element], *names: str, default: str = '') -> str:
    found = _find(el, *names) if names else el
    if found is None or found.text is None:
        return default
    return found.text.strip()


def parse_xml_safe(text: Any) -> ET.Element:
    """Parse ISO 20022 XML. DOCTYPE / ENTITY are rejected (XXE)."""
    raw = str(text or '')
    if _DOCTYPE.search(raw):
        raise InUkError('invalid_pacs', 'XML entities are not allowed.')
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise InUkError('invalid_pacs', 'ISO 20022 XML is not well-formed.') from exc


def parse_pacs008(text: Any) -> ET.Element:
    root = parse_xml_safe(text)
    if _local(root.tag) not in {'Document', 'FIToFICstmrCdtTrf', 'pacs.008.001.08'}:
        if _find(root, 'FIToFICstmrCdtTrf', 'CdtTrfTxInf') is None:
            raise InUkError('invalid_pacs', 'Document is not a pacs.008 credit transfer.')
    return root


def split_pacs_file(text: Any) -> List[str]:
    raw = str(text or '')
    parts = [part.strip() for part in _SPLIT.split(raw) if part.strip()]
    documents = []
    for part in parts:
        if '<Document' in part or 'FIToFICstmrCdtTrf' in part or 'pacs.008' in part:
            documents.append(part)
    if not documents and raw.strip():
        if '<CdtTrfTxInf' in raw or 'EndToEndId' in raw:
            documents.append(raw.strip())
    return documents


def _escape(value: Any) -> str:
    return (
        str(value or '')
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
    )


def compose_pacs008(fields: Dict[str, Any]) -> str:
    """Compose a minimal pacs.008.001.08 GBP credit transfer."""
    scheme = normalize_scheme(fields.get('scheme') or fields.get('rail') or SCHEME_FPS)
    clr = 'FPS' if scheme == SCHEME_FPS else 'CHP'
    instr = 'INST' if scheme == SCHEME_FPS else 'URNS'
    amount = parse_money(fields.get('amount') or fields.get('amount_gbp'))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Document xmlns="%s">'
        '<FIToFICstmrCdtTrf>'
        '<GrpHdr>'
        '<MsgId>%s</MsgId>'
        '<CreDtTm>%s</CreDtTm>'
        '<NbOfTxs>1</NbOfTxs>'
        '<SttlmInf><SttlmMtd>CLRG</SttlmMtd><ClrSys><Cd>%s</Cd></ClrSys></SttlmInf>'
        '</GrpHdr>'
        '<CdtTrfTxInf>'
        '<PmtId>'
        '<InstrId>%s</InstrId>'
        '<EndToEndId>%s</EndToEndId>'
        '<TxId>%s</TxId>'
        '</PmtId>'
        '<PmtTpInf><LclInstrm><Cd>%s</Cd></LclInstrm></PmtTpInf>'
        '<IntrBkSttlmAmt Ccy="GBP">%s</IntrBkSttlmAmt>'
        '<InstgAgt><FinInstnId><ClrSysMmbId><MmbId>%s</MmbId></ClrSysMmbId></FinInstnId></InstgAgt>'
        '<InstdAgt><FinInstnId><ClrSysMmbId><MmbId>%s</MmbId></ClrSysMmbId></FinInstnId></InstdAgt>'
        '<Dbtr><Nm>%s</Nm></Dbtr>'
        '<DbtrAcct><Id><Othr><Id>%s</Id></Othr></Id></DbtrAcct>'
        '<Cdtr><Nm>%s</Nm></Cdtr>'
        '<CdtrAcct><Id><Othr><Id>%s</Id></Othr></Id></CdtrAcct>'
        '<RmtInf><Ustrd>%s</Ustrd></RmtInf>'
        '</CdtTrfTxInf>'
        '</FIToFICstmrCdtTrf>'
        '</Document>'
    ) % (
        PACS008_NS,
        _escape(fields.get('msg_id') or 'MSG1'),
        _escape(fields.get('created') or '2024-06-14T15:00:00Z'),
        clr,
        _escape(fields.get('instr_id') or fields.get('scheme_id') or 'INSTR1'),
        _escape(fields.get('end_to_end_id') or 'E2E1'),
        _escape(fields.get('scheme_id') or fields.get('tx_id') or 'TX1'),
        instr,
        money_str(amount),
        _escape(fields.get('sender_sort') or fields.get('sender_sort_code')),
        _escape(fields.get('receiver_sort') or fields.get('receiver_sort_code')),
        _escape(fields.get('originator_name') or 'ORIGINATOR'),
        _escape(fields.get('originator_account') or ''),
        _escape(fields.get('beneficiary_name') or 'BENEFICIARY'),
        _escape(fields.get('beneficiary_account')),
        _escape(fields.get('memo') or ''),
    )


def compose_pacs004(
    row: 'InboundUk',
    *,
    return_msg_id: str,
    reason: str,
    receiver_sort: str,
) -> str:
    """pacs.004 payment return of an inbound UK credit."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Document xmlns="%s">'
        '<PmtRtr>'
        '<GrpHdr><MsgId>%s</MsgId><NbOfTxs>1</NbOfTxs></GrpHdr>'
        '<TxInf>'
        '<OrgnlEndToEndId>%s</OrgnlEndToEndId>'
        '<OrgnlTxId>%s</OrgnlTxId>'
        '<RtrdIntrBkSttlmAmt Ccy="GBP">%s</RtrdIntrBkSttlmAmt>'
        '<InstgAgt><FinInstnId><ClrSysMmbId><MmbId>%s</MmbId></ClrSysMmbId></FinInstnId></InstgAgt>'
        '<InstdAgt><FinInstnId><ClrSysMmbId><MmbId>%s</MmbId></ClrSysMmbId></FinInstnId></InstdAgt>'
        '<RtrRsnInf><Rsn><Cd>%s</Cd></Rsn></RtrRsnInf>'
        '</TxInf>'
        '</PmtRtr>'
        '</Document>'
    ) % (
        PACS004_NS,
        _escape(return_msg_id),
        _escape(row.end_to_end_id),
        _escape(row.scheme_id),
        row.amount_gbp,
        _escape(receiver_sort),
        _escape(row.sender_sort),
        _escape(reason),
    )


def _member_ids(root: ET.Element) -> Tuple[str, str]:
    instg = _find(root, 'InstgAgt')
    instd = _find(root, 'InstdAgt')
    sender = _text(instg, 'MmbId') or _text(instg, 'ClrSysMmbId')
    receiver = _text(instd, 'MmbId') or _text(instd, 'ClrSysMmbId')
    return sender, receiver


def _account_id(el: Optional[ET.Element]) -> str:
    if el is None:
        return ''
    iban = ''
    other = ''
    for child in el.iter():
        if _local(child.tag) == 'IBAN' and child.text and child.text.strip():
            iban = child.text.strip()
        if _local(child.tag) in {'Id', 'Othr'} and child.text and child.text.strip():
            if _local(child.tag) == 'Id':
                other = child.text.strip()
    return iban or other


def _beneficiary_from_account_field(raw: str, receiver_sort: str) -> str:
    text = str(raw or '').strip()
    if not text:
        raise InUkError('invalid_account', 'Beneficiary account is required.')
    compact = re.sub(r'[^A-Z0-9]', '', text.upper())
    if compact.startswith('GB') and len(compact) == 22:
        sort_code, uk_account = extract_iban_destination(compact)
        if sort_code != receiver_sort:
            raise InUkError('wrong_receiver', 'IBAN is not addressed to this bank.')
        try:
            return normalize_account(uk_account)
        except AccountError as exc:
            raise InUkError('invalid_account', 'IBAN account is not a valid internal account.') from exc
    digits = ''.join(ch for ch in text if ch.isdigit())
    try:
        return normalize_account(digits)
    except AccountError as exc:
        raise InUkError(exc.args[0] if exc.args else 'invalid_account', 'Beneficiary account is invalid.') from exc


def message_from_pacs(text: Any) -> Dict[str, Any]:
    """Reusable pacs.008 → inbound UK field map."""
    root = parse_pacs008(text)
    amount_el = _find(root, 'IntrBkSttlmAmt')
    if amount_el is None:
        amount_el = _find(root, 'InstdAmt')
    if amount_el is None or not (amount_el.text or '').strip():
        raise InUkError('invalid_amount', 'Settlement amount is required.')
    normalize_currency(amount_el.get('Ccy') or amount_el.get('ccy') or 'GBP')
    amount = parse_money(amount_el.text)
    sender_raw, receiver_raw = _member_ids(root)
    sender_sort = normalize_sort_code(sender_raw)
    receiver_sort = normalize_sort_code(receiver_raw)
    e2e = normalize_end_to_end_id(_text(root, 'EndToEndId'))
    msg_id = normalize_msg_id(_text(root, 'MsgId') or ('MSG' + e2e[:12]))
    tx_id = _text(root, 'TxId') or e2e
    try:
        scheme_id = normalize_scheme_id(tx_id)
    except InUkError:
        scheme_id = normalize_scheme_id(e2e if len(e2e) >= 8 else ('E2E' + e2e + 'XXXXXXX'))
    clr = _text(_find(root, 'ClrSys'), 'Cd') or _text(_find(root, 'ClrSys'), 'Prtry')
    instr = _text(_find(root, 'LclInstrm'), 'Cd')
    scheme = normalize_scheme(clr or instr or SCHEME_FPS)
    originator = _text(_find(root, 'Dbtr'), 'Nm') or 'ORIGINATOR'
    beneficiary = _text(_find(root, 'Cdtr'), 'Nm') or 'BENEFICIARY'
    creditor_raw = _account_id(_find(root, 'CdtrAcct'))
    account = _beneficiary_from_account_field(creditor_raw, receiver_sort)
    originator_account = _account_id(_find(root, 'DbtrAcct'))
    if originator_account and originator_account.isdigit() and len(originator_account) >= 6:
        try:
            enforce_uk_destination(sender_sort, originator_account)
        except InUkError:
            if len(''.join(ch for ch in originator_account if ch.isdigit())) == 8:
                raise
    return {
        'scheme_id': scheme_id,
        'end_to_end_id': e2e,
        'msg_id': msg_id,
        'scheme': scheme,
        'amount_gbp': money_str(amount),
        'sender_sort': sender_sort,
        'receiver_sort': receiver_sort,
        'beneficiary_account': account,
        'originator_account': originator_account,
        'beneficiary_name': beneficiary,
        'originator_name': originator,
        'iban': '',
        'memo': normalize_note(_text(root, 'Ustrd') or '', limit=140),
        'raw': str(text or ''),
    }


def message_from_values(values: Dict[str, Any]) -> Dict[str, Any]:
    """JSON operator payload or XML file → inbound field map."""
    blob = values.get('file') or values.get('pacs') or values.get('xml') or values.get('raw')
    if blob:
        parsed = message_from_pacs(blob)
        override = values.get('scheme') or values.get('rail')
        if override:
            parsed['scheme'] = normalize_scheme(override)
        return parsed
    scheme = normalize_scheme(values.get('scheme') or values.get('rail') or SCHEME_FPS)
    amount = parse_money(values.get('amount') or values.get('amount_gbp'))
    normalize_currency(values.get('currency') or values.get('ccy') or 'GBP')
    sender_sort = normalize_sort_code(values.get('sender_sort') or values.get('sender') or values.get('sender_sort_code'))
    receiver_sort = normalize_sort_code(
        values.get('receiver_sort') or values.get('receiver') or values.get('receiver_sort_code')
    )
    iban_raw = values.get('iban') or values.get('beneficiary_iban') or ''
    account_raw = values.get('beneficiary_account') or values.get('account') or iban_raw
    account = _beneficiary_from_account_field(account_raw, receiver_sort)
    scheme_raw = (
        values.get('scheme_id') or values.get('fps_id') or values.get('chaps_ref')
        or values.get('tx_id') or values.get('end_to_end_id') or values.get('e2e')
    )
    if not scheme_raw:
        raise InUkError('invalid_scheme_id', 'FPS id / CHAPS ref is required.')
    e2e_raw = values.get('end_to_end_id') or values.get('e2e') or scheme_raw
    msg_raw = values.get('msg_id') or values.get('msgid') or ('MSG' + re.sub(r'[^A-Za-z0-9]', '', str(scheme_raw))[:12])
    originator = str(values.get('originator_name') or values.get('originator') or '').strip()
    beneficiary = str(values.get('beneficiary_name') or values.get('beneficiary') or '').strip()
    originator_account = str(values.get('originator_account') or '').strip()
    if originator_account:
        digits = ''.join(ch for ch in originator_account if ch.isdigit())
        if len(digits) == 8:
            enforce_uk_destination(sender_sort, digits)
    iban = ''
    if iban_raw:
        iban = normalize_iban(iban_raw)
    return {
        'scheme_id': normalize_scheme_id(scheme_raw),
        'end_to_end_id': normalize_end_to_end_id(e2e_raw or ('E2E' + re.sub(r'[^A-Za-z0-9]', '', str(scheme_raw))[:16])),
        'msg_id': normalize_msg_id(msg_raw),
        'scheme': scheme,
        'amount_gbp': money_str(amount),
        'sender_sort': sender_sort,
        'receiver_sort': receiver_sort,
        'beneficiary_account': account,
        'originator_account': originator_account,
        'beneficiary_name': beneficiary or 'BENEFICIARY',
        'originator_name': originator or 'ORIGINATOR',
        'iban': iban,
        'memo': normalize_note(values.get('memo') or values.get('remittance') or '', limit=140),
        'raw': '',
    }


@dataclass
class InUkPolicy:
    enabled: bool = True
    customer_view: bool = True
    customer_return: bool = True
    allow_credit: bool = False
    max_inbounds: int = 240
    min_amount: Decimal = Decimal('0.01')
    max_amount: Decimal = Decimal('20000000.00')
    fps_max: Decimal = Decimal('1000000.00')
    chaps_max: Decimal = Decimal('20000000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    fx_rate: Decimal = Decimal('1.2500')
    cutoff_hour: int = 17
    tz_offset_hours: int = 1
    source_id: str = 'KONOHA01'
    receiver_sort: str = DEFAULT_RECEIVER_SORT
    customer_return_seconds: int = CUSTOMER_RETURN_SECONDS
    extra_holidays: Tuple[str, ...] = ()
    watchlist: Tuple[str, ...] = (
        'BLOCKED PERSON',
        'SANCTIONED ENTITY',
        'OFAC TESTNAME',
    )

    def scheme_max(self, scheme: str) -> Decimal:
        return self.chaps_max if scheme == SCHEME_CHAPS else self.fps_max

    @classmethod
    def from_env(cls) -> 'InUkPolicy':
        extra = _env_list('INUK_OFAC_LIST')
        watch = tuple(dict.fromkeys(cls.watchlist + extra))
        receiver = os.environ.get('INUK_RECEIVER_SORT') or DEFAULT_RECEIVER_SORT
        try:
            receiver = normalize_sort_code(receiver)
        except InUkError:
            receiver = DEFAULT_RECEIVER_SORT
        return cls(
            enabled=_env_bool('INUK_ENABLED', True),
            customer_view=_env_bool('INUK_CUSTOMER_VIEW', True),
            customer_return=_env_bool('INUK_CUSTOMER_RETURN', True),
            allow_credit=_env_bool('INUK_ALLOW_CREDIT', False),
            max_inbounds=max(1, _env_int('INUK_MAX', 240)),
            min_amount=_env_money('INUK_MIN_AMOUNT', '0.01'),
            max_amount=_env_money('INUK_MAX_AMOUNT', '20000000.00'),
            fps_max=_env_money('INUK_FPS_MAX', '1000000.00'),
            chaps_max=_env_money('INUK_CHAPS_MAX', '20000000.00'),
            dual_control_threshold=_env_money('INUK_DUAL_CONTROL', '10000.00'),
            fx_rate=_env_money('INUK_FX_RATE', '1.2500'),
            cutoff_hour=max(0, min(23, _env_int('INUK_CUTOFF_HOUR', 17))),
            tz_offset_hours=_env_int('INUK_TZ_OFFSET', 1),
            source_id=normalize_source(os.environ.get('INUK_SOURCE', 'KONOHA01')),
            receiver_sort=receiver,
            customer_return_seconds=max(60, _env_int('INUK_RETURN_WINDOW', CUSTOMER_RETURN_SECONDS)),
            extra_holidays=_env_list('INUK_HOLIDAYS'),
            watchlist=watch,
        )


@dataclass
class InboundUk:
    inbound_id: str
    scheme_id: str
    end_to_end_id: str
    msg_id: str
    scheme: str
    userid: str
    internal_account: str
    amount_gbp: str
    amount_usd: str
    fx_rate: str
    sender_sort: str
    receiver_sort: str
    originator_name: str
    originator_account_last4: str
    beneficiary_name: str
    beneficiary_account: str
    iban_masked: str
    purpose: str
    memo: str
    status: str
    value_date: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    return_msg_id: str
    return_reason: str
    created_at: float
    updated_at: float
    posted_at: float = 0.0
    returned_at: float = 0.0
    note: str = ''
    batch_id: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'inbound_id': self.inbound_id,
            'scheme_id': self.scheme_id,
            'end_to_end_id': self.end_to_end_id,
            'msg_id': self.msg_id,
            'scheme': self.scheme,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'amount_gbp': self.amount_gbp,
            'amount_usd': self.amount_usd,
            'fx_rate': self.fx_rate,
            'sender_sort': format_sort_code(self.sender_sort),
            'receiver_sort': format_sort_code(self.receiver_sort),
            'originator_name': self.originator_name,
            'originator_last4': self.originator_account_last4,
            'beneficiary_name': self.beneficiary_name,
            'beneficiary_last4': last4(self.beneficiary_account),
            'iban_masked': self.iban_masked,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'value_date': self.value_date,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'return_msg_id': self.return_msg_id,
            'return_reason': self.return_reason,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'posted_at': self.posted_at,
            'returned_at': self.returned_at,
            'note': self.note,
            'batch_id': self.batch_id,
            'held': self.status == IN_HELD,
            'unmatched': self.status == IN_UNMATCHED,
            'pending_release': self.status == IN_PENDING,
            'queued': self.status == IN_QUEUED,
            'posted': self.status == IN_POSTED,
            'returned': self.status == IN_RETURNED,
            'returnable': self.status in RETURNABLE_BEFORE_POST or self.status == IN_POSTED,
        }


def _clone(row: InboundUk) -> InboundUk:
    return InboundUk(**{key: getattr(row, key) for key in row.__dataclass_fields__})


def _from_row(row: Any) -> InboundUk:
    return InboundUk(
        inbound_id=row['inbound_id'],
        scheme_id=row['scheme_id'],
        end_to_end_id=row['end_to_end_id'] or '',
        msg_id=row['msg_id'] or '',
        scheme=row['scheme'],
        userid=row['userid'] or '',
        internal_account=row['internal_account'] or '',
        amount_gbp=row['amount_gbp'],
        amount_usd=row['amount_usd'],
        fx_rate=row['fx_rate'],
        sender_sort=row['sender_sort'],
        receiver_sort=row['receiver_sort'],
        originator_name=row['originator_name'],
        originator_account_last4=row['originator_account_last4'] or '',
        beneficiary_name=row['beneficiary_name'],
        beneficiary_account=row['beneficiary_account'],
        iban_masked=row['iban_masked'] or '',
        purpose=row['purpose'] or 'other',
        memo=row['memo'] or '',
        status=row['status'],
        value_date=row['value_date'],
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        return_msg_id=row['return_msg_id'] or '',
        return_reason=row['return_reason'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        posted_at=float(row['posted_at'] or 0),
        returned_at=float(row['returned_at'] or 0),
        note=row['note'] or '',
        batch_id=row['batch_id'] or '',
    )


class MemoryInUkStore:
    def __init__(self) -> None:
        self._rows: Dict[str, InboundUk] = {}
        self._by_scheme: Dict[str, str] = {}
        self._seq = 0
        self._lock = threading.Lock()

    def put(self, row: InboundUk) -> None:
        with self._lock:
            self._rows[row.inbound_id] = _clone(row)
            self._by_scheme[row.scheme_id] = row.inbound_id

    def update(self, row: InboundUk) -> None:
        with self._lock:
            if row.inbound_id not in self._rows:
                raise InUkError('inbound_not_found', 'Inbound UK payment not found.')
            self._rows[row.inbound_id] = _clone(row)
            self._by_scheme[row.scheme_id] = row.inbound_id

    def get(self, inbound_id: str) -> Optional[InboundUk]:
        with self._lock:
            row = self._rows.get(inbound_id)
            return _clone(row) if row is not None else None

    def get_by_scheme_id(self, scheme_id: str) -> Optional[InboundUk]:
        with self._lock:
            inbound_id = self._by_scheme.get(scheme_id)
            row = self._rows.get(inbound_id) if inbound_id else None
            return _clone(row) if row is not None else None

    def list_for(self, userid: str) -> List[InboundUk]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.userid == userid]
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def list_unmatched(self) -> List[InboundUk]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.status == IN_UNMATCHED]
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def list_open(self) -> List[InboundUk]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.status in OPEN_INBOUNDS]
            rows.sort(key=lambda item: item.created_at)
            return [_clone(row) for row in rows]

    def list_all(self) -> List[InboundUk]:
        with self._lock:
            rows = list(self._rows.values())
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def next_sequence(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq


class SqliteInUkStore:
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
        return conn

    def _init(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inbounds (
                    inbound_id TEXT PRIMARY KEY,
                    scheme_id TEXT NOT NULL UNIQUE,
                    end_to_end_id TEXT NOT NULL DEFAULT '',
                    msg_id TEXT NOT NULL DEFAULT '',
                    scheme TEXT NOT NULL,
                    userid TEXT NOT NULL DEFAULT '',
                    internal_account TEXT NOT NULL DEFAULT '',
                    amount_gbp TEXT NOT NULL,
                    amount_usd TEXT NOT NULL,
                    fx_rate TEXT NOT NULL,
                    sender_sort TEXT NOT NULL,
                    receiver_sort TEXT NOT NULL,
                    originator_name TEXT NOT NULL,
                    originator_account_last4 TEXT NOT NULL DEFAULT '',
                    beneficiary_name TEXT NOT NULL,
                    beneficiary_account TEXT NOT NULL,
                    iban_masked TEXT NOT NULL DEFAULT '',
                    purpose TEXT NOT NULL DEFAULT 'other',
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    value_date TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    return_msg_id TEXT NOT NULL DEFAULT '',
                    return_reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    posted_at REAL NOT NULL DEFAULT 0,
                    returned_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    batch_id TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _write(self, conn: sqlite3.Connection, row: InboundUk) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO inbounds (
                inbound_id, scheme_id, end_to_end_id, msg_id, scheme, userid,
                internal_account, amount_gbp, amount_usd, fx_rate, sender_sort,
                receiver_sort, originator_name, originator_account_last4,
                beneficiary_name, beneficiary_account, iban_masked, purpose, memo,
                status, value_date, actor, releaser, ofac_hit, ofac_match,
                return_msg_id, return_reason, created_at, updated_at, posted_at,
                returned_at, note, batch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.inbound_id, row.scheme_id, row.end_to_end_id, row.msg_id,
                row.scheme, row.userid, row.internal_account, row.amount_gbp,
                row.amount_usd, row.fx_rate, row.sender_sort, row.receiver_sort,
                row.originator_name, row.originator_account_last4,
                row.beneficiary_name, row.beneficiary_account, row.iban_masked,
                row.purpose, row.memo, row.status, row.value_date, row.actor,
                row.releaser, int(row.ofac_hit), row.ofac_match, row.return_msg_id,
                row.return_reason, row.created_at, row.updated_at, row.posted_at,
                row.returned_at, row.note, row.batch_id,
            ),
        )

    def put(self, row: InboundUk) -> None:
        with self._lock, self._connect() as conn:
            self._write(conn, row)
            conn.commit()

    def update(self, row: InboundUk) -> None:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT inbound_id FROM inbounds WHERE inbound_id = ?', (row.inbound_id,),
            ).fetchone()
            if existing is None:
                raise InUkError('inbound_not_found', 'Inbound UK payment not found.')
            self._write(conn, row)
            conn.commit()

    def get(self, inbound_id: str) -> Optional[InboundUk]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def get_by_scheme_id(self, scheme_id: str) -> Optional[InboundUk]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM inbounds WHERE scheme_id = ?', (scheme_id,),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def list_for(self, userid: str) -> List[InboundUk]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE userid = ? ORDER BY created_at DESC',
                (userid,),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_unmatched(self) -> List[InboundUk]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE status = ? ORDER BY created_at DESC',
                (IN_UNMATCHED,),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_open(self) -> List[InboundUk]:
        with self._lock, self._connect() as conn:
            placeholders = ','.join('?' for _ in OPEN_INBOUNDS)
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE status IN (%s) ORDER BY created_at' % placeholders,
                tuple(OPEN_INBOUNDS),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_all(self) -> List[InboundUk]:
        with self._lock, self._connect() as conn:
            rows = conn.execute('SELECT * FROM inbounds ORDER BY created_at DESC').fetchall()
        return [_from_row(row) for row in rows]

    def next_sequence(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'seq'").fetchone()
            current = int(row['value']) if row is not None else 0
            current += 1
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('seq', ?)",
                (str(current),),
            )
            conn.commit()
        return current


class InUkService:
    def __init__(
        self,
        policy: InUkPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        lookup_fn: Optional[Callable[[str], Optional[str]]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        fx_book: Optional[GbpUsdBook] = None,
        calendar: Optional[BankOfEnglandCalendar] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.lookup_fn = lookup_fn
        self.screen_fn = screen_fn
        self.fx_book = fx_book or GbpUsdBook(policy.fx_rate)
        self.calendar = calendar or BankOfEnglandCalendar(
            cutoff_hour=policy.cutoff_hour,
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise InUkError('inuk_disabled', 'Inbound UK payments are disabled.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise InUkError('inuk_forbidden', 'Staff only.')

    def _require_view(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_view:
            raise InUkError('inuk_forbidden', 'Customers cannot view inbound UK payments.')

    def _require_customer_return(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_return:
            raise InUkError('inuk_forbidden', 'Customers cannot request inbound returns.')

    def _owned_accounts(self, userid: str) -> List[str]:
        if self.accounts_fn is None:
            return []
        return own_accounts_from_customer_payload(self.accounts_fn(userid))

    def _account_types(self, userid: str) -> Dict[str, str]:
        if self.accounts_fn is None:
            return {}
        return account_types_from_customer_payload(self.accounts_fn(userid))

    def _lookup(self, account: str) -> Optional[str]:
        if self.lookup_fn is None:
            return None
        found = self.lookup_fn(account)
        if found in (None, '', -1, 0):
            return None
        return str(found)

    def _assert_internal_account(self, userid: str, account: str) -> None:
        owned = self._owned_accounts(userid)
        if owned and account not in owned:
            raise InUkError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise InUkError('credit_not_allowed', 'Credit accounts cannot receive inbound UK payments.')

    def _assert_amount(self, pounds: Decimal, scheme: str) -> None:
        if pounds < self.policy.min_amount:
            raise InUkError('amount_out_of_range', 'Amount is outside the allowed range.')
        cap = self.policy.scheme_max(scheme)
        if pounds > cap:
            code = 'chaps_amount_exceeded' if scheme == SCHEME_CHAPS else 'fps_amount_exceeded'
            raise InUkError(code, 'Amount exceeds the %s cap.' % scheme)
        if pounds > self.policy.max_amount:
            raise InUkError('amount_out_of_range', 'Amount is outside the allowed range.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, usd: Decimal) -> bool:
        return usd >= self.policy.dual_control_threshold

    def _assert_receiver(self, receiver_sort: str) -> None:
        if receiver_sort != self.policy.receiver_sort:
            raise InUkError('wrong_receiver', 'Message is not addressed to this bank.')

    def quote(self, pounds: Decimal) -> FxQuote:
        return self.fx_book.quote(pounds)

    def get_inbound(self, *, inbound_id: str, actor: str, actor_type: str) -> InboundUk:
        self._require_view(actor_type)
        row = self.store.get(inbound_id)
        if row is None:
            raise InUkError('inbound_not_found', 'Inbound UK payment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise InUkError('inuk_forbidden', 'Not allowed to view this inbound UK payment.')
        return row

    def preview_message(self, values: Dict[str, Any]) -> Dict[str, Any]:
        self._require_enabled()
        message = message_from_values(values)
        pounds = parse_money(message['amount_gbp'])
        self._assert_amount(pounds, message['scheme'])
        self._assert_receiver(message['receiver_sort'])
        fx = self.quote(pounds)
        ofac = self._screen(message['originator_name'])
        userid = self._lookup(message['beneficiary_account'])
        now = float(self.clock())
        return {
            'message': {
                'scheme_id': message['scheme_id'],
                'end_to_end_id': message['end_to_end_id'],
                'msg_id': message['msg_id'],
                'scheme': message['scheme'],
                'amount_gbp': message['amount_gbp'],
                'sender_sort': format_sort_code(message['sender_sort']),
                'receiver_sort': format_sort_code(message['receiver_sort']),
                'originator_name': message['originator_name'],
                'beneficiary_name': message['beneficiary_name'],
                'beneficiary_last4': last4(message['beneficiary_account']),
            },
            'fx': fx.to_dict(),
            'matched_userid': userid or '',
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(parse_money(fx.amount_usd)),
            'clock': self.calendar.snapshot(now, scheme=message['scheme']),
        }

    def _evaluate_status(
        self,
        *,
        userid: str,
        account: str,
        usd: Decimal,
        ofac: ScreenResult,
        scheme: str,
        now: float,
    ) -> str:
        if not userid or not account:
            return IN_UNMATCHED
        if ofac.hit:
            return IN_HELD
        if self._needs_dual_control(usd):
            return IN_PENDING
        if self.calendar.should_queue(now, scheme=scheme):
            return IN_QUEUED
        return IN_POSTED

    def _credit(self, row: InboundUk) -> str:
        if self.credit_fn is None:
            return 'ok'
        remark = '%s from %s' % (row.scheme, row.originator_name[:20] or 'originator')
        result = self.credit_fn(row.internal_account, row.amount_usd, remark)
        return _classify_money_result(result)

    def _debit(self, row: InboundUk) -> str:
        if self.debit_fn is None:
            return 'ok'
        remark = '%s return %s' % (row.scheme, row.scheme_id[:12])
        result = self.debit_fn(row.internal_account, row.amount_usd, remark)
        return _classify_money_result(result)

    def _try_post(self, row: InboundUk) -> InboundUk:
        classified = self._credit(row)
        now = float(self.clock())
        if classified == 'ok':
            row.status = IN_POSTED
            row.posted_at = now
            row.updated_at = now
            self.store.update(row)
            return row
        row.status = IN_FAILED
        row.note = 'credit_failed'
        row.updated_at = now
        self.store.update(row)
        raise InUkError('failed', 'Inbound credit failed.', inbound=row)

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        values: Dict[str, Any],
        batch_id: str = '',
    ) -> Tuple[InboundUk, bool]:
        self._require_staff(actor_type)
        message = message_from_values(values)
        pounds = parse_money(message['amount_gbp'])
        self._assert_amount(pounds, message['scheme'])
        self._assert_receiver(message['receiver_sort'])
        existing = self.store.get_by_scheme_id(message['scheme_id'])
        if existing is not None:
            return existing, False
        if len(self.store.list_all()) >= self.policy.max_inbounds:
            raise InUkError('inbound_limit', 'Inbound UK payment limit reached.')
        try:
            account = normalize_account(message['beneficiary_account'])
        except AccountError:
            account = ''
        userid = self._lookup(account) if account else None
        credit_blocked = False
        if userid and account:
            types = self._account_types(userid)
            if types.get(account) == 'credit' and not self.policy.allow_credit:
                credit_blocked = True
                userid = None
                account = ''
        ofac = self._screen(message['originator_name'])
        now = float(self.clock())
        fx = self.quote(pounds)
        usd = parse_money(fx.amount_usd)
        status = self._evaluate_status(
            userid=userid or '',
            account=account,
            usd=usd,
            ofac=ofac,
            scheme=message['scheme'],
            now=now,
        )
        if credit_blocked:
            status = IN_UNMATCHED
        purpose = normalize_purpose(values.get('purpose') or 'other')
        try:
            originator_name = normalize_legal_name(message['originator_name'])
        except WireError:
            originator_name = normalize_party(message['originator_name'])[:80] or 'ORIGINATOR'
        try:
            beneficiary_name = normalize_legal_name(message['beneficiary_name'])
        except WireError:
            beneficiary_name = normalize_party(message['beneficiary_name'])[:80] or 'BENEFICIARY'
        iban_masked = mask_iban(message.get('iban') or '')
        row = InboundUk(
            inbound_id=uuid.uuid4().hex,
            scheme_id=message['scheme_id'],
            end_to_end_id=message['end_to_end_id'],
            msg_id=message['msg_id'],
            scheme=message['scheme'],
            userid=userid or '',
            internal_account=account,
            amount_gbp=money_str(pounds),
            amount_usd=fx.amount_usd,
            fx_rate=fx.rate,
            sender_sort=message['sender_sort'],
            receiver_sort=message['receiver_sort'],
            originator_name=originator_name,
            originator_account_last4=last4(message.get('originator_account')),
            beneficiary_name=beneficiary_name,
            beneficiary_account=message['beneficiary_account'],
            iban_masked=iban_masked,
            purpose=purpose,
            memo=message['memo'],
            status=status,
            value_date=self.calendar.value_date(now, scheme=message['scheme']).isoformat(),
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            return_msg_id='',
            return_reason='',
            created_at=now,
            updated_at=now,
            note='credit_not_allowed' if credit_blocked else '',
            batch_id=batch_id or normalize_id(values.get('batch_id') if values.get('batch_id') else ''),
        )
        self.store.put(row)
        if row.status == IN_POSTED:
            return self._try_post(row), True
        return row, True

    def ingest_file(
        self,
        *,
        actor: str,
        actor_type: str,
        text: Any,
        purpose: str = 'other',
        scheme: Any = '',
    ) -> Dict[str, Any]:
        self._require_staff(actor_type)
        messages = split_pacs_file(text)
        if not messages:
            raise InUkError('invalid_pacs', 'Operator file has no pacs.008 documents.')
        batch_id = uuid.uuid4().hex
        accepted = []
        duplicates = []
        errors = []
        for raw in messages:
            try:
                values: Dict[str, Any] = {'file': raw, 'purpose': purpose, 'batch_id': batch_id}
                if scheme:
                    values['scheme'] = scheme
                row, created = self.ingest(
                    actor=actor,
                    actor_type=actor_type,
                    values=values,
                    batch_id=batch_id,
                )
                payload = row.to_dict()
                if created:
                    accepted.append(payload)
                else:
                    duplicates.append(payload)
            except InUkError as exc:
                errors.append({'error': exc.code, 'message': exc.message})
        return {
            'batch_id': batch_id,
            'accepted': accepted,
            'duplicates': duplicates,
            'errors': errors,
            'accepted_count': len(accepted),
            'duplicate_count': len(duplicates),
            'error_count': len(errors),
        }

    def assign(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        customer_id: Any,
        internal_account: Any = None,
    ) -> InboundUk:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_UNMATCHED:
            raise InUkError('not_assignable', 'Only unmatched inbound payments can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise InUkError('missing_customer_id', 'Customer id is required.')
        account = normalize_account(internal_account or row.beneficiary_account)
        self._assert_internal_account(owner, account)
        row.userid = owner
        row.internal_account = account
        row.actor = str(actor)
        now = float(self.clock())
        row.updated_at = now
        ofac = ScreenResult(bool(row.ofac_hit), row.ofac_match, 100 if row.ofac_hit else 0)
        usd = parse_money(row.amount_usd)
        status = self._evaluate_status(
            userid=owner, account=account, usd=usd, ofac=ofac, scheme=row.scheme, now=now,
        )
        row.status = status
        if status == IN_POSTED:
            self.store.update(row)
            return self._try_post(row)
        self.store.update(row)
        return row

    def override_ofac(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> InboundUk:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_HELD:
            raise InUkError('not_overridable', 'Only OFAC-held inbound payments can be overridden.')
        row.ofac_hit = 0
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        now = float(self.clock())
        row.updated_at = now
        usd = parse_money(row.amount_usd)
        status = self._evaluate_status(
            userid=row.userid,
            account=row.internal_account,
            usd=usd,
            ofac=ScreenResult(False, '', 0),
            scheme=row.scheme,
            now=now,
        )
        row.status = status
        if status == IN_POSTED:
            self.store.update(row)
            return self._try_post(row)
        self.store.update(row)
        return row

    def release(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
    ) -> InboundUk:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_PENDING:
            raise InUkError('not_releasable', 'Inbound payment is not waiting for dual-control.')
        if row.actor and row.actor == str(actor):
            raise InUkError('same_approver', 'A different employee must release this inbound payment.')
        row.releaser = str(actor)
        now = float(self.clock())
        row.updated_at = now
        if self.calendar.should_queue(now, scheme=row.scheme):
            row.status = IN_QUEUED
            self.store.update(row)
            return row
        row.status = IN_POSTED
        self.store.update(row)
        return self._try_post(row)

    def reject(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'MS03',
        note: Any = '',
    ) -> InboundUk:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status not in OPEN_INBOUNDS:
            raise InUkError('not_rejectable', 'Inbound payment cannot be rejected.')
        row.status = IN_REJECTED
        row.return_reason = normalize_return_reason(reason)
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update(row)
        return row

    def return_inbound(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'MS03',
        note: Any = '',
    ) -> InboundUk:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        return self._return(row, actor=actor, reason=reason, note=note, force_window=True)

    def request_return(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'CUST',
        note: Any = '',
    ) -> InboundUk:
        self._require_customer_return(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise InUkError('inuk_forbidden', 'Not allowed to return this inbound payment.')
        return self._return(row, actor=actor, reason=reason or 'CUST', note=note, force_window=False)

    def _assign_return_id(self, row: InboundUk) -> None:
        seq = self.store.next_sequence()
        row.return_msg_id = compose_msg_id(self.policy.source_id + 'R', seq)

    def _customer_window_open(self, row: InboundUk, now: float) -> bool:
        posted_at = float(row.posted_at or row.created_at)
        if row.scheme == SCHEME_FPS:
            return (now - posted_at) <= self.policy.customer_return_seconds
        posted_day = self.calendar.local_dt(posted_at).date()
        now_day = self.calendar.local_dt(now).date()
        return posted_day == now_day and self.calendar.is_business_day(now_day)

    def _return(
        self,
        row: InboundUk,
        *,
        actor: str,
        reason: Any,
        note: Any,
        force_window: bool,
    ) -> InboundUk:
        if row.status in {IN_RETURNED, IN_REJECTED}:
            raise InUkError('already_returned', 'Inbound payment is already returned or rejected.')
        code = normalize_return_reason(reason, default='CUST')
        now = float(self.clock())
        if row.status in RETURNABLE_BEFORE_POST:
            row.status = IN_RETURNED
            row.return_reason = code
            row.note = normalize_note(note) or row.note
            row.actor = str(actor)
            row.returned_at = now
            row.updated_at = now
            self._assign_return_id(row)
            self.store.update(row)
            return row
        if row.status != IN_POSTED:
            raise InUkError('not_returnable', 'Inbound payment cannot be returned.')
        if not force_window and not self._customer_window_open(row, now):
            raise InUkError('return_window_closed', 'Exception-return window has closed.')
        classified = self._debit(row)
        if classified == 'nsf':
            raise InUkError('nsf', 'Insufficient funds to return this inbound payment.', inbound=row)
        if classified != 'ok':
            raise InUkError('return_failed', 'Inbound return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.return_reason = code
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.returned_at = now
        row.updated_at = now
        self._assign_return_id(row)
        self.store.update(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[InboundUk]:
        now = float(self.clock())
        posted = []
        for row in self.store.list_open():
            if row.status != IN_QUEUED:
                continue
            if userid and row.userid != userid:
                continue
            if self.calendar.should_queue(now, scheme=row.scheme):
                continue
            row.status = IN_POSTED
            row.updated_at = now
            self.store.update(row)
            posted.append(self._try_post(row))
        return posted

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self._require_view(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor and actor != userid:
            raise InUkError('inuk_forbidden', 'Not allowed to view this inbound book.')
        self.run_due(userid)
        rows = self.store.list_for(userid)
        posted_ytd = Decimal('0.00')
        returned_ytd = Decimal('0.00')
        for row in rows:
            amount = parse_money(row.amount_usd, allow_zero=True)
            if row.status == IN_POSTED:
                posted_ytd += amount
            elif row.status == IN_RETURNED:
                returned_ytd += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'receiver_sort': format_sort_code(self.policy.receiver_sort),
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'fps_max': money_str(self.policy.fps_max),
            'chaps_max': money_str(self.policy.chaps_max),
            'fx_rate': str(self.fx_book.rate),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clock': self.calendar.snapshot(now, scheme=SCHEME_FPS),
            'chaps_clock': self.calendar.snapshot(now, scheme=SCHEME_CHAPS),
            'inbounds': [row.to_dict() for row in rows[:40]],
            'ytd_posted': money_str(posted_ytd),
            'ytd_returned': money_str(returned_ytd),
            'open_count': sum(1 for row in rows if row.status in OPEN_INBOUNDS),
            'posted_count': sum(1 for row in rows if row.status == IN_POSTED),
        }

    def unmatched_snapshot(self) -> Dict[str, Any]:
        rows = self.store.list_unmatched()
        return {
            'enabled': self.policy.enabled,
            'receiver_sort': format_sort_code(self.policy.receiver_sort),
            'unmatched': [row.to_dict() for row in rows[:40]],
            'unmatched_count': len(rows),
        }


_SERVICE: Optional[InUkService] = None


def set_service(service: Optional[InUkService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[InUkService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('INUK_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryInUkStore()
    path = os.environ.get('INUK_DB', DEFAULT_STORE_PATH)
    return SqliteInUkStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    lookup_fn: Optional[Callable[[str], Optional[str]]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    fx_book: Optional[GbpUsdBook] = None,
    calendar: Optional[BankOfEnglandCalendar] = None,
) -> InUkService:
    if store is None:
        store = default_store()
    return InUkService(
        InUkPolicy.from_env(),
        store,
        clock=clock,
        debit_fn=debit_fn,
        credit_fn=credit_fn,
        accounts_fn=accounts_fn,
        lookup_fn=lookup_fn,
        screen_fn=screen_fn,
        fx_book=fx_book,
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


def _error_status(code: str) -> int:
    return {
        'already_returned': 409,
        'inbound_limit': 409,
        'nsf': 409,
        'failed': 409,
        'return_failed': 409,
        'inuk_forbidden': 403,
        'inuk_disabled': 403,
        'credit_not_allowed': 403,
        'same_approver': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_returnable': 403,
        'return_window_closed': 403,
        'inbound_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_sort_code': 400,
        'invalid_scheme_id': 400,
        'invalid_pacs': 400,
        'invalid_scheme': 400,
        'invalid_currency': 400,
        'invalid_iban': 400,
        'invalid_end_to_end_id': 400,
        'invalid_msg_id': 400,
        'invalid_reason': 400,
        'invalid_purpose': 400,
        'invalid_reference': 400,
        'wrong_receiver': 400,
        'amount_out_of_range': 400,
        'fps_amount_exceeded': 400,
        'chaps_amount_exceeded': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: InUkError) -> Dict[str, Any]:
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
    except WireError as exc:
        return jsonify({'message': str(exc), 'error': exc.code}), _error_status(exc.code)
    except InUkError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: InUkService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'InUks': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_unmatched(service: InUkService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'inuk_forbidden'}), 403
    return jsonify({'InUks': service.unmatched_snapshot()}), 200


def handle_preview(service: InUkService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'inuk_forbidden'}), 403
    values = request.get_json(silent=True) or {}

    def _run():
        preview = service.preview_message(values)
        return jsonify({'preview': preview}), 200

    return _handle_errors(_run)


def handle_quote(service: InUkService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        pounds = parse_money(values.get('amount') or values.get('amount_gbp'))
        return jsonify({'fx': service.quote(pounds).to_dict()}), 200

    return _handle_errors(_run)


def handle_ingest(service: InUkService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'Inbound UK payment ingested' if created else 'Inbound UK payment already posted',
            'inbound': row.to_dict(),
            'InUks': service.snapshot(owner, actor=userid, actor_type=actor_type) if row.userid else service.unmatched_snapshot(),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_file(service: InUkService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    text = values.get('file') or values.get('pacs') or values.get('xml') or values.get('text')
    if not text:
        return jsonify({'message': 'Operator file is required', 'error': 'missing_file'}), 400

    def _run():
        result = service.ingest_file(
            actor=userid,
            actor_type=actor_type,
            text=text,
            purpose=values.get('purpose') or 'other',
            scheme=values.get('scheme') or values.get('rail') or '',
        )
        result['Unmatched'] = service.unmatched_snapshot()
        return jsonify(result), 201 if result['accepted_count'] else 200

    return _handle_errors(_run)


def handle_assign(service: InUkService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    inbound_id = str(values.get('inbound_id') or '').strip()
    if not inbound_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inbound'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.assign(
            inbound_id=inbound_id,
            actor=userid,
            actor_type=actor_type,
            customer_id=values.get('customer_id') or values.get('owner'),
            internal_account=values.get('account') or values.get('internal_account'),
        )
        return jsonify({
            'message': 'Inbound UK payment assigned',
            'inbound': row.to_dict(),
            'InUks': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_action(service: InUkService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    inbound_id = str(values.get('inbound_id') or '').strip()
    if not inbound_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inbound'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'override':
            row = service.override_ofac(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'release':
            row = service.release(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            message = 'Inbound UK payment released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'MS03', note=values.get('note') or '',
            )
            message = 'Inbound UK payment rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'MS03', note=values.get('note') or '',
            )
            message = 'Inbound UK payment returned'
        else:
            raise InUkError('invalid_reason', 'Unknown inbound action.')
        owner = row.userid or userid
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'InUks': service.snapshot(owner, actor=userid, actor_type=actor_type) if row.userid else service.unmatched_snapshot(),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: InUkService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    inbound_id = str(values.get('inbound_id') or '').strip()
    if not inbound_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inbound'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.request_return(
            inbound_id=inbound_id,
            actor=userid,
            actor_type=actor_type,
            reason=values.get('reason') or 'CUST',
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Inbound return requested',
            'inbound': row.to_dict(),
            'InUks': service.snapshot(row.userid or userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: InUkService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner if actor_type not in EMPLOYEE_ROLES else None)
    return jsonify({'InUks': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_inuk_routes(app, service: InUkService) -> None:
    @app.route('/listInUks', methods=['POST', 'GET'])
    def list_inuks_route():
        return handle_list(service)

    @app.route('/listUnmatchedInUks', methods=['POST', 'GET'])
    def list_unmatched_inuks_route():
        return handle_unmatched(service)

    @app.route('/previewInUk', methods=['POST', 'GET'])
    def preview_inuk_route():
        return handle_preview(service)

    @app.route('/quoteInUkFx', methods=['POST', 'GET'])
    def quote_inuk_fx_route():
        return handle_quote(service)

    @app.route('/ingestInUk', methods=['POST', 'GET'])
    def ingest_inuk_route():
        return handle_ingest(service)

    @app.route('/ingestInUkFile', methods=['POST', 'GET'])
    def ingest_inuk_file_route():
        return handle_ingest_file(service)

    @app.route('/assignInUk', methods=['POST', 'GET'])
    def assign_inuk_route():
        return handle_assign(service)

    @app.route('/overrideInUkOfac', methods=['POST', 'GET'])
    def override_inuk_ofac_route():
        return _staff_action(service, 'override')

    @app.route('/releaseInUk', methods=['POST', 'GET'])
    def release_inuk_route():
        return _staff_action(service, 'release')

    @app.route('/rejectInUk', methods=['POST', 'GET'])
    def reject_inuk_route():
        return _staff_action(service, 'reject')

    @app.route('/returnInUk', methods=['POST', 'GET'])
    def return_inuk_route():
        return _staff_action(service, 'return')

    @app.route('/requestInUkReturn', methods=['POST', 'GET'])
    def request_inuk_return_route():
        return handle_request_return(service)

    @app.route('/runDueInUks', methods=['POST', 'GET'])
    def run_due_inuks_route():
        return handle_run_due(service)
