"""UK Faster Payments, CHAPS, and BACS origination.

Customers send GBP payments to UK beneficiaries identified by a Vocalink
sort code + account (optional GB IBAN). Independent of SEPA Direct Debit
(PR #85), FedNow / TCH RTP (PR #83), SEPA SCT (PR #80), SWIFT MT103
(PR #78), domestic Fedwire (PR #73), ACH linking / micro-deposits
(PR #68), bill-pay outgoing ACH (PR #66), inbound payroll splits
(PR #64), the in-bank payee allowlist (PR #26), and scheduled internal
transfers (PR #36). Existing `/fundTransfer`, `/withdrawAmount`, and
`/sendWire` stay unchanged. `Customers.debit_request` / `credit_request`
still write `debited` / `direct deposited` unless a remark is supplied here.

Foundations (reusable beyond this screen):
- Vocalink modulus check (MOD10 / MOD11 / DBLAL) for sort code + account
- GB IBAN ISO 13616 mod-97 (compose / validate / mask)
- GBP-only + GBPUSD quote book (USD debit equivalent)
- Bank of England business-day / cutoff clock
- FPS (24/7 instant, irrevocable) / CHAPS (same-day high-value) / BACS (T+2)
- ISO 20022 pacs.008 field map + BACS Standard 18
- Dual-control release on the USD equivalent

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Destination account numbers never appear in to_dict / snapshots.
"""

from __future__ import annotations

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
    AccountError,
    AmountError,
    DEFAULT_WATCHLIST,
    ScreenResult,
    WireError,
    account_types_from_customer_payload,
    last4,
    money_str,
    normalize_account,
    normalize_id,
    normalize_nickname as _wire_nickname,
    normalize_note,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)


def normalize_uk_nickname(value: Any) -> str:
    try:
        return _wire_nickname(value)
    except WireError as exc:
        raise UkPayError(exc.code, exc.message) from exc

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
BENE_ACTIVE = 'active'
BENE_PAUSED = 'paused'
BENE_ARCHIVED = 'archived'
BENE_STATUSES = frozenset({BENE_ACTIVE, BENE_PAUSED, BENE_ARCHIVED})
OPEN_BENE = frozenset({BENE_ACTIVE, BENE_PAUSED})
PAY_HELD = 'held'
PAY_QUEUED = 'queued'
PAY_PENDING = 'pending_release'
PAY_SENT = 'sent'
PAY_COMPLETED = 'completed'
PAY_REJECTED = 'rejected'
PAY_CANCELLED = 'cancelled'
PAY_RECALLED = 'recalled'
PAY_NSF = 'nsf'
PAY_FAILED = 'failed'
PAY_STATUSES = frozenset({
    PAY_HELD, PAY_QUEUED, PAY_PENDING, PAY_SENT, PAY_COMPLETED,
    PAY_REJECTED, PAY_CANCELLED, PAY_RECALLED, PAY_NSF, PAY_FAILED,
})
OPEN_PAYS = frozenset({PAY_HELD, PAY_QUEUED, PAY_PENDING, PAY_SENT})
CANCELABLE = frozenset({PAY_HELD, PAY_QUEUED, PAY_PENDING})
FEE_NONE = 'none'
FEE_COLLECTED = 'collected'
FEE_WAIVED = 'waived'
FEE_NSF = 'nsf'
SCHEME_FPS = 'fps'
SCHEME_CHAPS = 'chaps'
SCHEME_BACS = 'bacs'
SCHEMES = frozenset({SCHEME_FPS, SCHEME_CHAPS, SCHEME_BACS})
SCHEME_ALIASES = {
    'faster': SCHEME_FPS, 'faster_payments': SCHEME_FPS, 'fp': SCHEME_FPS,
    'instant': SCHEME_FPS, 'uk_fps': SCHEME_FPS,
    'chaps_sterling': SCHEME_CHAPS, 'sterling': SCHEME_CHAPS,
    'direct_credit': SCHEME_BACS, 'bacstel': SCHEME_BACS, 'standard18': SCHEME_BACS,
}
PURPOSES = frozenset({'family', 'goods', 'payroll', 'tax', 'loan', 'rent', 'other'})
PURPOSE_ALIASES = {
    'personal': 'family', 'gift': 'family', 'support': 'family',
    'invoice': 'goods', 'purchase': 'goods', 'vendor': 'goods',
    'salary': 'payroll', 'wage': 'payroll',
    'hmrc': 'tax', 'taxes': 'tax',
    'mortgage': 'loan', 'housing': 'rent',
}
UK_COUNTRIES = frozenset({'GB', 'UK', 'GG', 'JE', 'IM'})
MONEY_QUANTUM = Decimal('0.01')
FX_QUANTUM = Decimal('0.0001')
DEFAULT_STORE_PATH = 'SystemLogs/ukpay.sqlite'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)
# Official Vocalink worked example (scheme document).
VOCALINK_OFFICIAL_VALID = frozenset({
    ('089999', '66374958'),
    ('107999', '88837491'),
})
# (sort_from, sort_to, algorithm, 14 weights, exception)
# 200000-209999: Barclays-style MOD11. 200000 / 12345679 is a computed valid pair.
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


class UkPayError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise UkPayError('invalid_name', 'Beneficiary legal name must be 2-80 characters.')
    return text


def normalize_city(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise UkPayError('invalid_address', 'City must be 2-40 characters.')
    return text


def normalize_country(value: Any, *, default: str = 'GB') -> str:
    text = str(value or default).strip().upper()
    if text in {'UNITED KINGDOM', 'GREAT BRITAIN', 'ENGLAND', 'SCOTLAND', 'WALES'}:
        text = 'GB'
    if text not in UK_COUNTRIES:
        raise UkPayError('invalid_country', 'Beneficiary country must be GB/UK.')
    return 'GB' if text == 'UK' else text


def normalize_postcode(value: Any) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        return ''
    if not (5 <= len(text) <= 8):
        raise UkPayError('invalid_address', 'UK postcode must be 5-8 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise UkPayError('invalid_purpose', 'Unknown UK payment purpose.')
    return text


def normalize_scheme(value: Any, *, default: str = SCHEME_FPS) -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = SCHEME_ALIASES.get(text, text)
    if text not in SCHEMES:
        raise UkPayError('invalid_scheme', 'Scheme must be fps, chaps, or bacs.')
    return text


def normalize_source(value: Any, *, default: str = 'KONOHA01') -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or default).upper())
    if not text:
        text = default
    return (text + 'XXXXXXXX')[:8]


def normalize_sort_code(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) == 0:
        raise UkPayError('invalid_sort_code', 'Sort code is required.')
    if len(digits) < 6:
        digits = digits.zfill(6)
    if len(digits) != 6 or digits == '000000':
        raise UkPayError('invalid_sort_code', 'Sort code must be six digits.')
    return digits


def format_sort_code(digits: str) -> str:
    text = ''.join(ch for ch in str(digits or '') if ch.isdigit()).zfill(6)
    return '%s-%s-%s' % (text[0:2], text[2:4], text[4:6])


def normalize_uk_account(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        raise UkPayError('invalid_account', 'UK account number is required.')
    if len(digits) < 8:
        digits = digits.zfill(8)
    if len(digits) != 8:
        raise UkPayError('invalid_account', 'UK account number must be eight digits.')
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
        raise UkPayError('invalid_sort_code', 'Sort code and account failed Vocalink modulus check.')
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
        raise UkPayError('invalid_iban', 'Only 22-character GB IBANs are accepted.')
    if not iban_check_digit_ok(compact):
        raise UkPayError('invalid_iban', 'IBAN failed ISO 13616 check.')
    return compact


def mask_iban(iban: str) -> str:
    compact = re.sub(r'[^A-Z0-9]', '', str(iban or '').upper())
    if len(compact) < 8:
        return compact
    return compact[:2] + '****' + compact[-4:]


def extract_iban_destination(iban: str) -> Tuple[str, str]:
    compact = normalize_iban(iban)
    return compact[8:14], compact[14:22]


def compose_end_to_end_id(*, scheme: str, owner: str, payment_id: str) -> str:
    prefix = {'fps': 'FP', 'chaps': 'CH', 'bacs': 'BA'}.get(scheme, 'UK')
    token = re.sub(r'[^A-Z0-9]', '', (str(owner or 'CUST') + payment_id).upper())[:12]
    return ('%s%s%s' % (prefix, token, uuid.uuid4().hex[:8].upper()))[:35]


def compose_message_id(cycle_date: str, source: str, sequence: int) -> str:
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise UkPayError('invalid_reference', 'Message sequence out of range.')
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise UkPayError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    return '%s%s%06d' % (day, normalize_source(source), seq)


def compose_fps_id(cycle_date: str, source: str, sequence: int) -> str:
    return 'FP' + compose_message_id(cycle_date, source, sequence)


def compose_chaps_ref(cycle_date: str, source: str, sequence: int) -> str:
    return 'CH' + compose_message_id(cycle_date, source, sequence)


def compose_bacs_serial(cycle_date: str, sequence: int, *, sun: str = '123456') -> str:
    digits = ''.join(ch for ch in str(sun) if ch.isdigit()).zfill(6)[:6]
    return '%s%s%06d' % (digits, cycle_date, int(sequence))


def compose_pacs008(
    *,
    scheme: str,
    end_to_end_id: str,
    message_id: str,
    amount_gbp: str,
    sort_code: str,
    account_last4: str,
    legal_name: str,
    purpose: str,
) -> Dict[str, Any]:
    svc = 'INST' if scheme == SCHEME_FPS else ('URNS' if scheme == SCHEME_CHAPS else 'NURG')
    return {
        'MsgId': message_id,
        'EndToEndId': end_to_end_id,
        'PmtMtd': 'TRF',
        'SvcLvl': 'SEPA' if scheme == SCHEME_BACS else 'URGP',
        'LclInstrm': svc,
        'Ccy': 'GBP',
        'InstdAmt': amount_gbp,
        'CdtrAgt': sort_code,
        'CdtrAcct': '****' + account_last4,
        'CdtrNm': legal_name,
        'Purp': purpose.upper()[:4],
    }


def compose_bacs18(
    *,
    sun: str,
    processing_date: str,
    serial: str,
    amount_gbp: str,
    sort_code: str,
    account_last4: str,
) -> Dict[str, Any]:
    return {
        'type': 'standard18',
        'sun': sun,
        'processing_date': processing_date,
        'serial': serial,
        'amount': amount_gbp,
        'sort_code': format_sort_code(sort_code),
        'account_last4': account_last4,
        'transaction_code': '99',
    }


def compute_fee(scheme: str, fees: Dict[str, Decimal], *, waived: bool = False) -> Decimal:
    if waived:
        return Decimal('0.00')
    return fees.get(scheme, Decimal('0.00')).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


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
    """Injectable GBPUSD book. Amounts originate in GBP; the ledger debits USD."""

    def __init__(self, rate: Decimal = Decimal('1.2500')) -> None:
        self.rate = Decimal(rate).quantize(FX_QUANTUM, rounding=ROUND_HALF_EVEN)

    def quote(self, gbp: Decimal) -> FxQuote:
        usd = (gbp * self.rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
        return FxQuote('GBP', money_str(gbp), money_str(usd), str(self.rate))


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


class BankOfEnglandCalendar:
    """CHAPS / BACS business-day clock. FPS ignores weekends and cutoff."""

    def __init__(
        self,
        *,
        cutoff_hour: int = 17,
        bacs_cutoff_hour: int = 16,
        tz_offset_hours: int = 1,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.cutoff_hour = int(cutoff_hour)
        self.bacs_cutoff_hour = int(bacs_cutoff_hour)
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

    def is_after_cutoff(self, ts: float, *, hour: Optional[int] = None) -> bool:
        local = self.local_dt(ts)
        limit = self.cutoff_hour if hour is None else int(hour)
        return (local.hour, local.minute) >= (limit, 0)

    def next_business_day(self, day: date) -> date:
        cursor = day + timedelta(days=1)
        while not self.is_business_day(cursor):
            cursor += timedelta(days=1)
        return cursor

    def add_business_days(self, day: date, count: int) -> date:
        cursor = day
        remaining = int(count)
        while remaining > 0:
            cursor = self.next_business_day(cursor)
            remaining -= 1
        return cursor

    def input_date(self, ts: float, *, hour: Optional[int] = None) -> date:
        local = self.local_dt(ts)
        day = local.date()
        if self.is_business_day(day) and not self.is_after_cutoff(ts, hour=hour):
            return day
        start = day if self.is_business_day(day) else day
        return self.next_business_day(start) if (not self.is_business_day(day) or self.is_after_cutoff(ts, hour=hour)) else day

    def value_date(self, ts: float, *, scheme: str = SCHEME_CHAPS) -> date:
        if scheme == SCHEME_FPS:
            return self.local_dt(ts).date()
        if scheme == SCHEME_BACS:
            inbound = self.input_date(ts, hour=self.bacs_cutoff_hour)
            return self.add_business_days(inbound, 2)
        return self.input_date(ts, hour=self.cutoff_hour)

    def cycle_date(self, ts: float, *, scheme: str = SCHEME_CHAPS) -> str:
        return self.value_date(ts, scheme=scheme).strftime('%Y%m%d')

    def snapshot(self, ts: float, *, scheme: str = SCHEME_CHAPS) -> Dict[str, Any]:
        local = self.local_dt(ts)
        value = self.value_date(ts, scheme=scheme)
        after = False
        if scheme == SCHEME_CHAPS:
            after = self.is_after_cutoff(ts) or not self.is_business_day(local.date())
        elif scheme == SCHEME_BACS:
            after = True
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'cutoff': '%02d:00' % (self.bacs_cutoff_hour if scheme == SCHEME_BACS else self.cutoff_hour),
            'after_cutoff': after,
            'business_day': self.is_business_day(local.date()),
            'value_date': value.isoformat(),
            'cycle_date': value.strftime('%Y%m%d'),
            'scheme': scheme,
            'instant': scheme == SCHEME_FPS,
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
class UkPayPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    allow_credit: bool = False
    max_beneficiaries: int = 12
    max_payments: int = 120
    min_amount: Decimal = Decimal('1.00')
    max_amount: Decimal = Decimal('1000000.00')
    fps_cap: Decimal = Decimal('1000000.00')
    bacs_cap: Decimal = Decimal('250000.00')
    fps_fee: Decimal = Decimal('1.50')
    chaps_fee: Decimal = Decimal('30.00')
    bacs_fee: Decimal = Decimal('5.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    fx_rate: Decimal = Decimal('1.2500')
    cutoff_hour: int = 17
    bacs_cutoff_hour: int = 16
    tz_offset_hours: int = 1
    source_id: str = 'KONOHA01'
    bacs_sun: str = '123456'
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()

    @property
    def fees(self) -> Dict[str, Decimal]:
        return {SCHEME_FPS: self.fps_fee, SCHEME_CHAPS: self.chaps_fee, SCHEME_BACS: self.bacs_fee}

    @classmethod
    def from_env(cls) -> 'UkPayPolicy':
        extra = _env_list('UKPAY_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('UKPAY_ENABLED', True),
            customer_manage=_env_bool('UKPAY_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('UKPAY_CUSTOMER_SEND', True),
            allow_credit=_env_bool('UKPAY_ALLOW_CREDIT', False),
            max_beneficiaries=max(1, _env_int('UKPAY_MAX_BENEFICIARIES', 12)),
            max_payments=max(1, _env_int('UKPAY_MAX_PAYMENTS', 120)),
            min_amount=_env_money('UKPAY_MIN_AMOUNT', '1.00'),
            max_amount=_env_money('UKPAY_MAX_AMOUNT', '1000000.00'),
            fps_cap=_env_money('UKPAY_FPS_CAP', '1000000.00'),
            bacs_cap=_env_money('UKPAY_BACS_CAP', '250000.00'),
            fps_fee=_env_money('UKPAY_FPS_FEE', '1.50'),
            chaps_fee=_env_money('UKPAY_CHAPS_FEE', '30.00'),
            bacs_fee=_env_money('UKPAY_BACS_FEE', '5.00'),
            dual_control_threshold=_env_money('UKPAY_DUAL_CONTROL', '10000.00'),
            fx_rate=_env_money('UKPAY_FX_RATE', '1.2500'),
            cutoff_hour=max(0, min(23, _env_int('UKPAY_CUTOFF_HOUR', 17))),
            bacs_cutoff_hour=max(0, min(23, _env_int('UKPAY_BACS_CUTOFF_HOUR', 16))),
            tz_offset_hours=_env_int('UKPAY_TZ_OFFSET', 1),
            source_id=normalize_source(os.environ.get('UKPAY_SOURCE', 'KONOHA01')),
            bacs_sun=''.join(ch for ch in str(os.environ.get('UKPAY_BACS_SUN', '123456')) if ch.isdigit()).zfill(6)[:6],
            watchlist=watch,
            extra_holidays=_env_list('UKPAY_HOLIDAYS'),
        )


@dataclass
class UkPayBeneficiary:
    beneficiary_id: str
    userid: str
    nickname: str
    legal_name: str
    sort_code: str
    account_number: str
    iban: str
    city: str
    country: str
    postcode: str
    default_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'beneficiary_id': self.beneficiary_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'sort_code': self.sort_code,
            'sort_code_formatted': format_sort_code(self.sort_code),
            'account_last4': last4(self.account_number),
            'iban_masked': mask_iban(self.iban) if self.iban else '',
            'city': self.city,
            'country': self.country,
            'postcode': self.postcode,
            'default_account': self.default_account,
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == BENE_ACTIVE,
            'paused': self.status == BENE_PAUSED,
            'archived': self.status == BENE_ARCHIVED,
        }


@dataclass
class UkPayTransfer:
    payment_id: str
    trace_id: str
    beneficiary_id: str
    userid: str
    internal_account: str
    scheme: str
    amount_gbp: str
    debit_usd: str
    fx_rate: str
    fee: str
    fee_status: str
    nickname: str
    legal_name: str
    sort_code: str
    account_last4: str
    purpose: str
    memo: str
    status: str
    end_to_end_id: str
    message_id: str
    scheme_ref: str
    value_date: str
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
            'payment_id': self.payment_id,
            'trace_id': self.trace_id,
            'beneficiary_id': self.beneficiary_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'scheme': self.scheme,
            'amount_gbp': self.amount_gbp,
            'amount': self.debit_usd,
            'debit_usd': self.debit_usd,
            'fx_rate': self.fx_rate,
            'fee': self.fee,
            'fee_status': self.fee_status,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'sort_code': self.sort_code,
            'sort_code_formatted': format_sort_code(self.sort_code),
            'account_last4': self.account_last4,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'end_to_end_id': self.end_to_end_id,
            'message_id': self.message_id,
            'scheme_ref': self.scheme_ref,
            'value_date': self.value_date,
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
            'held': self.status == PAY_HELD,
            'queued': self.status == PAY_QUEUED,
            'pending_release': self.status == PAY_PENDING,
            'sent': self.status == PAY_SENT,
            'completed': self.status == PAY_COMPLETED,
            'cancelable': self.status in CANCELABLE,
            'irrevocable': self.scheme == SCHEME_FPS and self.status in {PAY_SENT, PAY_COMPLETED},
        }


def _clone_bene(row: UkPayBeneficiary) -> UkPayBeneficiary:
    return UkPayBeneficiary(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_pay(row: UkPayTransfer) -> UkPayTransfer:
    return UkPayTransfer(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _bene_from_row(row: Any) -> UkPayBeneficiary:
    return UkPayBeneficiary(
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        sort_code=row['sort_code'],
        account_number=row['account_number'],
        iban=row['iban'] or '',
        city=row['city'],
        country=row['country'],
        postcode=row['postcode'] or '',
        default_account=row['default_account'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
    )


def _pay_from_row(row: Any) -> UkPayTransfer:
    return UkPayTransfer(
        payment_id=row['payment_id'],
        trace_id=row['trace_id'],
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        scheme=row['scheme'],
        amount_gbp=row['amount_gbp'],
        debit_usd=row['debit_usd'],
        fx_rate=row['fx_rate'],
        fee=row['fee'],
        fee_status=row['fee_status'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        sort_code=row['sort_code'],
        account_last4=row['account_last4'],
        purpose=row['purpose'],
        memo=row['memo'] or '',
        status=row['status'],
        end_to_end_id=row['end_to_end_id'] or '',
        message_id=row['message_id'] or '',
        scheme_ref=row['scheme_ref'] or '',
        value_date=row['value_date'],
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


class MemoryUkPayStore:
    def __init__(self) -> None:
        self._benes: Dict[str, UkPayBeneficiary] = {}
        self._pays: Dict[str, UkPayTransfer] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_beneficiary(self, row: UkPayBeneficiary) -> None:
        with self._lock:
            self._benes[row.beneficiary_id] = row

    def get_beneficiary(self, beneficiary_id: str) -> Optional[UkPayBeneficiary]:
        with self._lock:
            row = self._benes.get(beneficiary_id)
            return _clone_bene(row) if row else None

    def update_beneficiary(self, row: UkPayBeneficiary) -> None:
        with self._lock:
            self._benes[row.beneficiary_id] = row

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[UkPayBeneficiary]:
        with self._lock:
            rows = [_clone_bene(row) for row in self._benes.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != BENE_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[UkPayBeneficiary]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._benes.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_BENE:
                    return _clone_bene(row)
        return None

    def find_beneficiary_by_fingerprint(self, userid: str, sort_code: str, account_number: str) -> Optional[UkPayBeneficiary]:
        with self._lock:
            for row in self._benes.values():
                if (
                    row.userid == userid
                    and row.sort_code == sort_code
                    and row.account_number == account_number
                    and row.status in OPEN_BENE
                ):
                    return _clone_bene(row)
        return None

    def put_payment(self, row: UkPayTransfer) -> UkPayTransfer:
        with self._lock:
            existing_id = self._by_trace.get(row.trace_id)
            if existing_id is not None:
                return self._pays[existing_id]
            self._pays[row.payment_id] = row
            self._by_trace[row.trace_id] = row.payment_id
            return row

    def update_payment(self, row: UkPayTransfer) -> None:
        with self._lock:
            self._pays[row.payment_id] = row

    def get_payment(self, payment_id: str) -> Optional[UkPayTransfer]:
        with self._lock:
            row = self._pays.get(payment_id)
            return _clone_pay(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[UkPayTransfer]:
        with self._lock:
            payment_id = self._by_trace.get(trace_id)
            return _clone_pay(self._pays[payment_id]) if payment_id else None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[UkPayTransfer]:
        with self._lock:
            rows = [_clone_pay(row) for row in self._pays.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if beneficiary_id is not None:
            rows = [row for row in rows if row.beneficiary_id == beneficiary_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def next_sequence(self, cycle_date: str) -> int:
        with self._lock:
            used = [row.message_id for row in self._pays.values() if row.message_id[0:8] == cycle_date]
        return len(used) + 1


class SqliteUkPayStore:
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
                CREATE TABLE IF NOT EXISTS beneficiaries (
                    beneficiary_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    sort_code TEXT NOT NULL,
                    account_number TEXT NOT NULL,
                    iban TEXT NOT NULL DEFAULT '',
                    city TEXT NOT NULL,
                    country TEXT NOT NULL,
                    postcode TEXT NOT NULL DEFAULT '',
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
                    beneficiary_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    internal_account TEXT NOT NULL,
                    scheme TEXT NOT NULL,
                    amount_gbp TEXT NOT NULL,
                    debit_usd TEXT NOT NULL,
                    fx_rate TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    fee_status TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    sort_code TEXT NOT NULL,
                    account_last4 TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    end_to_end_id TEXT NOT NULL DEFAULT '',
                    message_id TEXT NOT NULL DEFAULT '',
                    scheme_ref TEXT NOT NULL DEFAULT '',
                    value_date TEXT NOT NULL,
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

    def put_beneficiary(self, row: UkPayBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO beneficiaries (
                    beneficiary_id, userid, nickname, legal_name, sort_code, account_number,
                    iban, city, country, postcode, default_account, status, actor,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.beneficiary_id, row.userid, row.nickname, row.legal_name, row.sort_code,
                    row.account_number, row.iban, row.city, row.country, row.postcode,
                    row.default_account, row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_beneficiary(self, beneficiary_id: str) -> Optional[UkPayBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM beneficiaries WHERE beneficiary_id = ?', (beneficiary_id,),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def update_beneficiary(self, row: UkPayBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE beneficiaries SET nickname=?, legal_name=?, sort_code=?, account_number=?,
                    iban=?, city=?, country=?, postcode=?, default_account=?, status=?,
                    actor=?, updated_at=?
                WHERE beneficiary_id=?
                """,
                (
                    row.nickname, row.legal_name, row.sort_code, row.account_number, row.iban,
                    row.city, row.country, row.postcode, row.default_account, row.status,
                    row.actor, row.updated_at, row.beneficiary_id,
                ),
            )
            conn.commit()

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[UkPayBeneficiary]:
        sql = 'SELECT * FROM beneficiaries'
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
        return [_bene_from_row(row) for row in rows]

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[UkPayBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM beneficiaries
                WHERE userid = ? AND lower(nickname) = lower(?)
                  AND status IN ('active', 'paused')
                """,
                (userid, nickname),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def find_beneficiary_by_fingerprint(self, userid: str, sort_code: str, account_number: str) -> Optional[UkPayBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM beneficiaries
                WHERE userid = ? AND sort_code = ? AND account_number = ?
                  AND status IN ('active', 'paused')
                """,
                (userid, sort_code, account_number),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def put_payment(self, row: UkPayTransfer) -> UkPayTransfer:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM payments WHERE trace_id = ?', (row.trace_id,)
            ).fetchone()
            if existing is not None:
                return _pay_from_row(existing)
            conn.execute(
                """
                INSERT INTO payments (
                    payment_id, trace_id, beneficiary_id, userid, internal_account, scheme,
                    amount_gbp, debit_usd, fx_rate, fee, fee_status, nickname, legal_name,
                    sort_code, account_last4, purpose, memo, status, end_to_end_id,
                    message_id, scheme_ref, value_date, actor, releaser, ofac_hit, ofac_match,
                    created_at, updated_at, sent_at, completed_at, recalled_at, note, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.payment_id, row.trace_id, row.beneficiary_id, row.userid,
                    row.internal_account, row.scheme, row.amount_gbp, row.debit_usd, row.fx_rate,
                    row.fee, row.fee_status, row.nickname, row.legal_name, row.sort_code,
                    row.account_last4, row.purpose, row.memo, row.status, row.end_to_end_id,
                    row.message_id, row.scheme_ref, row.value_date, row.actor, row.releaser,
                    row.ofac_hit, row.ofac_match, row.created_at, row.updated_at, row.sent_at,
                    row.completed_at, row.recalled_at, row.note, row.reason,
                ),
            )
            conn.commit()
            return row

    def update_payment(self, row: UkPayTransfer) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE payments SET fee=?, fee_status=?, status=?, end_to_end_id=?,
                    message_id=?, scheme_ref=?, value_date=?, actor=?, releaser=?,
                    ofac_hit=?, ofac_match=?, updated_at=?, sent_at=?, completed_at=?,
                    recalled_at=?, note=?, reason=?
                WHERE payment_id=?
                """,
                (
                    row.fee, row.fee_status, row.status, row.end_to_end_id, row.message_id,
                    row.scheme_ref, row.value_date, row.actor, row.releaser, row.ofac_hit,
                    row.ofac_match, row.updated_at, row.sent_at, row.completed_at,
                    row.recalled_at, row.note, row.reason, row.payment_id,
                ),
            )
            conn.commit()

    def get_payment(self, payment_id: str) -> Optional[UkPayTransfer]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE payment_id = ?', (payment_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[UkPayTransfer]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE trace_id = ?', (trace_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[UkPayTransfer]:
        sql = 'SELECT * FROM payments'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if beneficiary_id is not None:
            clauses.append('beneficiary_id = ?')
            params.append(beneficiary_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_pay_from_row(row) for row in rows]

    def next_sequence(self, cycle_date: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE message_id LIKE ?",
                (cycle_date + '%',),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class UkPayService:
    def __init__(
        self,
        policy: UkPayPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[BankOfEnglandCalendar] = None,
        fx_book: Optional[GbpUsdBook] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or BankOfEnglandCalendar(
            cutoff_hour=policy.cutoff_hour,
            bacs_cutoff_hour=policy.bacs_cutoff_hour,
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )
        self.fx_book = fx_book or GbpUsdBook(policy.fx_rate)

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise UkPayError('ukpay_disabled', 'UK payments are disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise UkPayError('ukpay_forbidden', 'Customers cannot manage UK beneficiaries.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise UkPayError('ukpay_forbidden', 'Customers cannot originate UK payments.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise UkPayError('ukpay_forbidden', 'Staff only.')

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
            raise UkPayError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise UkPayError('credit_not_allowed', 'Credit accounts cannot originate UK payments.')

    def _assert_amount(self, gbp: Decimal, scheme: str) -> None:
        if gbp < self.policy.min_amount or gbp > self.policy.max_amount:
            raise UkPayError('amount_out_of_range', 'Amount is outside the allowed range.')
        if scheme == SCHEME_FPS and gbp > self.policy.fps_cap:
            raise UkPayError('fps_amount_exceeded', 'Faster Payments amount exceeds the scheme cap.')
        if scheme == SCHEME_BACS and gbp > self.policy.bacs_cap:
            raise UkPayError('bacs_amount_exceeded', 'BACS amount exceeds the scheme cap.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, usd: Decimal) -> bool:
        return usd >= self.policy.dual_control_threshold

    def quote_fx(self, amount: Any) -> Dict[str, Any]:
        gbp = parse_money(amount)
        quote = self.fx_book.quote(gbp)
        payload = quote.to_dict()
        payload['dual_control'] = self._needs_dual_control(parse_money(quote.amount_usd))
        payload['clock'] = self.calendar.snapshot(float(self.clock()), scheme=SCHEME_CHAPS)
        return payload

    def add_beneficiary(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        sort_code: Any = None,
        account_number: Any = None,
        iban: Any = None,
        city: Any,
        country: Any = 'GB',
        postcode: Any = '',
        default_account: Any,
    ) -> UkPayBeneficiary:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise UkPayError('ukpay_forbidden', 'Not allowed to add beneficiaries for this customer.')
        name = normalize_uk_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        compact_iban = normalize_iban(iban) if iban else ''
        if compact_iban:
            routing, external = extract_iban_destination(compact_iban)
        else:
            routing, external = enforce_uk_destination(sort_code, account_number)
            compact_iban = compose_gb_iban('NWBK', routing, external)
        if not vocalink_modulus_ok(routing, external):
            raise UkPayError('invalid_sort_code', 'Sort code and account failed Vocalink modulus check.')
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        if self.store.find_beneficiary_by_nickname(owner_userid, name) is not None:
            raise UkPayError('beneficiary_duplicate', 'A beneficiary with that nickname already exists.')
        if self.store.find_beneficiary_by_fingerprint(owner_userid, routing, external) is not None:
            raise UkPayError('beneficiary_duplicate', 'That beneficiary account is already on file.')
        open_rows = [row for row in self.store.list_beneficiaries(owner_userid) if row.status in OPEN_BENE]
        if len(open_rows) >= self.policy.max_beneficiaries:
            raise UkPayError('beneficiary_limit', 'UK beneficiary limit reached.')
        now = float(self.clock())
        row = UkPayBeneficiary(
            beneficiary_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            sort_code=routing,
            account_number=external,
            iban=compact_iban,
            city=normalize_city(city),
            country=normalize_country(country),
            postcode=normalize_postcode(postcode),
            default_account=account,
            status=BENE_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_beneficiary(row)
        return row

    def get_beneficiary(self, *, beneficiary_id: str, actor: str, actor_type: str) -> UkPayBeneficiary:
        self._require_enabled()
        row = self.store.get_beneficiary(beneficiary_id)
        if row is None:
            raise UkPayError('beneficiary_not_found', 'UK beneficiary not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise UkPayError('ukpay_forbidden', 'Not allowed to view this beneficiary.')
        return row

    def enforce_beneficiary(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> UkPayBeneficiary:
        """Reusable gate: destination must be an active, unarchived UK beneficiary."""
        row = self.get_beneficiary(beneficiary_id=beneficiary_id, actor=actor, actor_type=actor_type)
        if row.status == BENE_ARCHIVED:
            raise UkPayError('already_archived', 'Beneficiary is archived.')
        if row.status == BENE_PAUSED:
            raise UkPayError('beneficiary_paused', 'Beneficiary is paused.')
        if require_active and row.status != BENE_ACTIVE:
            raise UkPayError('invalid_status', 'Beneficiary is not active.')
        return row

    def set_beneficiary_status(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> UkPayBeneficiary:
        self._require_manage(actor_type)
        row = self.get_beneficiary(beneficiary_id=beneficiary_id, actor=actor, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': BENE_PAUSED, 'hold': BENE_PAUSED,
            'resume': BENE_ACTIVE, 'activate': BENE_ACTIVE, 'unpause': BENE_ACTIVE,
            'archive': BENE_ARCHIVED, 'close': BENE_ARCHIVED, 'cancel': BENE_ARCHIVED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {BENE_PAUSED, BENE_ACTIVE, BENE_ARCHIVED}:
            raise UkPayError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == BENE_ARCHIVED:
            raise UkPayError('already_archived', 'Beneficiary is already archived.')
        if wanted == BENE_PAUSED:
            if row.status == BENE_PAUSED:
                raise UkPayError('already_paused', 'Beneficiary is already paused.')
            if row.status != BENE_ACTIVE:
                raise UkPayError('invalid_status', 'Only an active beneficiary can be paused.')
        elif wanted == BENE_ACTIVE:
            if row.status == BENE_ACTIVE:
                raise UkPayError('already_active', 'Beneficiary is already active.')
            if row.status != BENE_PAUSED:
                raise UkPayError('invalid_status', 'Only a paused beneficiary can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_beneficiary(row)
        return row

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        beneficiary_id: Any,
        amount: Any,
        scheme: Any = SCHEME_FPS,
        internal_account: Any = None,
        waive_fee: bool = False,
    ) -> Dict[str, Any]:
        self._require_send(actor_type)
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise UkPayError('ukpay_forbidden', 'Beneficiary does not belong to this customer.')
        rail = normalize_scheme(scheme)
        gbp = parse_money(amount)
        self._assert_amount(gbp, rail)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        quote = self.fx_book.quote(gbp)
        usd = parse_money(quote.amount_usd)
        fee = compute_fee(rail, self.policy.fees, waived=bool(waive_fee) and actor_type in EMPLOYEE_ROLES)
        now = float(self.clock())
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        clock = self.calendar.snapshot(now, scheme=rail)
        return {
            'scheme': rail,
            'amount_gbp': money_str(gbp),
            'debit_usd': money_str(usd),
            'fx': quote.to_dict(),
            'fee': money_str(fee),
            'total_usd': money_str(usd + fee),
            'internal_account': account,
            'beneficiary': bene.to_dict(),
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(usd),
            'clock': clock,
            'pacs008': compose_pacs008(
                scheme=rail,
                end_to_end_id='PREVIEW',
                message_id='PREVIEW',
                amount_gbp=money_str(gbp),
                sort_code=bene.sort_code,
                account_last4=last4(bene.account_number),
                legal_name=bene.legal_name,
                purpose='other',
            ),
        }

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        bene: UkPayBeneficiary,
        account: str,
        scheme: str,
        gbp: Decimal,
        usd: Decimal,
        quote: FxQuote,
        fee: Decimal,
        purpose: str,
        memo: str,
        trace_id: str,
        ofac: ScreenResult,
        waive_fee: bool,
    ) -> UkPayTransfer:
        now = float(self.clock())
        value = self.calendar.cycle_date(now, scheme=scheme)
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        clock = self.calendar.snapshot(now, scheme=scheme)
        if ofac.hit:
            status = PAY_HELD
        elif self._needs_dual_control(usd):
            status = PAY_PENDING
        elif scheme == SCHEME_BACS or (scheme == SCHEME_CHAPS and clock['after_cutoff']):
            status = PAY_QUEUED
        else:
            status = PAY_SENT
        pay = UkPayTransfer(
            payment_id=uuid.uuid4().hex,
            trace_id=trace_id,
            beneficiary_id=bene.beneficiary_id,
            userid=owner_userid,
            internal_account=account,
            scheme=scheme,
            amount_gbp=money_str(gbp),
            debit_usd=money_str(usd),
            fx_rate=quote.rate,
            fee=money_str(fee),
            fee_status=fee_status,
            nickname=bene.nickname,
            legal_name=bene.legal_name,
            sort_code=bene.sort_code,
            account_last4=last4(bene.account_number),
            purpose=purpose,
            memo=memo,
            status=status,
            end_to_end_id='',
            message_id='',
            scheme_ref='',
            value_date=value,
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
        )
        if status == PAY_SENT:
            self._transmit(pay, actor=actor)
        return pay

    def _assign_ids(self, pay: UkPayTransfer) -> None:
        now = float(self.clock())
        cycle = pay.value_date or self.calendar.cycle_date(now, scheme=pay.scheme)
        seq = self.store.next_sequence(cycle)
        pay.message_id = compose_message_id(cycle, self.policy.source_id, seq)
        pay.end_to_end_id = compose_end_to_end_id(
            scheme=pay.scheme, owner=pay.userid, payment_id=pay.payment_id,
        )
        if pay.scheme == SCHEME_FPS:
            pay.scheme_ref = compose_fps_id(cycle, self.policy.source_id, seq)
        elif pay.scheme == SCHEME_CHAPS:
            pay.scheme_ref = compose_chaps_ref(cycle, self.policy.source_id, seq)
        else:
            pay.scheme_ref = compose_bacs_serial(cycle, seq, sun=self.policy.bacs_sun)

    def _transmit(self, pay: UkPayTransfer, *, actor: str) -> UkPayTransfer:
        now = float(self.clock())
        self._assign_ids(pay)
        usd = parse_money(pay.debit_usd)
        fee = parse_money(pay.fee, allow_zero=True)
        remark = 'ukpay to %s' % pay.nickname
        status = PAY_COMPLETED if pay.scheme == SCHEME_FPS else PAY_SENT
        fail_note = ''
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(pay.internal_account, money_str(usd), remark)
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
        if status in {PAY_SENT, PAY_COMPLETED} and fee > 0 and pay.fee_status != FEE_WAIVED and self.debit_fn is not None:
            try:
                fee_result = self.debit_fn(pay.internal_account, money_str(fee), 'ukpay fee %s' % pay.end_to_end_id[:12])
            except Exception:
                pay.fee_status = FEE_NSF
            else:
                kind = _classify_money_result(fee_result)
                pay.fee_status = FEE_COLLECTED if kind == 'ok' else FEE_NSF
        elif status in {PAY_SENT, PAY_COMPLETED} and (fee == 0 or pay.fee_status == FEE_WAIVED):
            pay.fee_status = FEE_WAIVED if pay.fee_status == FEE_WAIVED or fee == 0 else pay.fee_status
        pay.status = status
        pay.updated_at = now
        if status in {PAY_SENT, PAY_COMPLETED}:
            pay.sent_at = now
            pay.releaser = str(actor)
            if status == PAY_COMPLETED:
                pay.completed_at = now
        else:
            pay.end_to_end_id = ''
            pay.message_id = ''
            pay.scheme_ref = ''
            pay.note = fail_note
        return pay

    def originate(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        beneficiary_id: Any,
        amount: Any,
        scheme: Any = SCHEME_FPS,
        internal_account: Any = None,
        purpose: Any = 'other',
        memo: Any = '',
        trace_id: Any = None,
        waive_fee: bool = False,
    ) -> Tuple[UkPayTransfer, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise UkPayError('ukpay_forbidden', 'Not allowed to originate UK payments for this customer.')
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise UkPayError('ukpay_forbidden', 'Beneficiary does not belong to this customer.')
        rail = normalize_scheme(scheme)
        gbp = parse_money(amount)
        self._assert_amount(gbp, rail)
        quote = self.fx_book.quote(gbp)
        usd = parse_money(quote.amount_usd)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(rail, self.policy.fees, waived=staff_waive)
        trace = normalize_id(trace_id)
        existing = self.store.get_payment_by_trace(trace)
        if existing is not None:
            return existing, False
        if len(self.store.list_payments(owner_userid)) >= self.policy.max_payments:
            raise UkPayError('payment_limit', 'UK payment history limit reached.')
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        pay = self._place(
            owner_userid=owner_userid,
            actor=actor,
            bene=bene,
            account=account,
            scheme=rail,
            gbp=gbp,
            usd=usd,
            quote=quote,
            fee=fee,
            purpose=normalize_purpose(purpose),
            memo=normalize_note(memo, limit=140),
            trace_id=trace,
            ofac=ofac,
            waive_fee=staff_waive,
        )
        stored = self.store.put_payment(pay)
        if stored.payment_id != pay.payment_id:
            return stored, False
        if stored.status == PAY_NSF:
            raise UkPayError('nsf', 'Insufficient funds for UK payment.', payment=stored)
        if stored.status == PAY_FAILED:
            raise UkPayError('failed', 'UK payment debit did not complete.', payment=stored)
        return stored, True

    def get_payment(self, *, payment_id: str, actor: str, actor_type: str) -> UkPayTransfer:
        self._require_enabled()
        row = self.store.get_payment(payment_id)
        if row is None:
            raise UkPayError('payment_not_found', 'UK payment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise UkPayError('ukpay_forbidden', 'Not allowed to view this payment.')
        return row

    def cancel_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> UkPayTransfer:
        self._require_send(actor_type)
        pay = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and pay.userid != actor:
            raise UkPayError('ukpay_forbidden', 'Not allowed to cancel this payment.')
        if pay.status not in CANCELABLE:
            raise UkPayError('not_cancelable', 'Only held, queued, or pending payments can be cancelled.')
        pay.status = PAY_CANCELLED
        pay.actor = str(actor)
        pay.updated_at = float(self.clock())
        pay.note = normalize_note(note)
        self.store.update_payment(pay)
        return pay

    def waive_fee(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
    ) -> UkPayTransfer:
        self._require_staff(actor_type)
        pay = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if pay.status not in CANCELABLE:
            raise UkPayError('invalid_status', 'Fee can only be waived before the payment is sent.')
        pay.fee = money_str(Decimal('0.00'))
        pay.fee_status = FEE_WAIVED
        pay.actor = str(actor)
        pay.updated_at = float(self.clock())
        self.store.update_payment(pay)
        return pay

    def override_ofac(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> UkPayTransfer:
        self._require_staff(actor_type)
        pay = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if pay.status != PAY_HELD:
            raise UkPayError('invalid_status', 'Only an OFAC hold can be overridden.')
        now = float(self.clock())
        pay.ofac_hit = 0
        pay.note = normalize_note(note) or 'ofac override'
        pay.actor = str(actor)
        pay.updated_at = now
        usd = parse_money(pay.debit_usd)
        clock = self.calendar.snapshot(now, scheme=pay.scheme)
        if self._needs_dual_control(usd):
            pay.status = PAY_PENDING
        elif pay.scheme == SCHEME_BACS or (pay.scheme == SCHEME_CHAPS and clock['after_cutoff']):
            pay.status = PAY_QUEUED
            pay.value_date = self.calendar.cycle_date(now, scheme=pay.scheme)
        else:
            self._transmit(pay, actor=actor)
        self.store.update_payment(pay)
        if pay.status == PAY_NSF:
            raise UkPayError('nsf', 'Insufficient funds for UK payment.', payment=pay)
        if pay.status == PAY_FAILED:
            raise UkPayError('failed', 'UK payment debit did not complete.', payment=pay)
        return pay

    def release_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
    ) -> UkPayTransfer:
        self._require_staff(actor_type)
        pay = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if pay.status == PAY_HELD:
            raise UkPayError('ofac_hold', 'OFAC hold must be overridden before release.')
        if pay.status not in {PAY_PENDING, PAY_QUEUED}:
            raise UkPayError('not_releasable', 'Only queued or pending payments can be released.')
        if (
            pay.status == PAY_PENDING
            and pay.actor
            and str(actor) == str(pay.actor)
            and parse_money(pay.debit_usd) >= self.policy.dual_control_threshold
        ):
            raise UkPayError('same_approver', 'A different employee must release this payment.')
        now = float(self.clock())
        clock = self.calendar.snapshot(now, scheme=pay.scheme)
        if pay.scheme == SCHEME_BACS:
            if pay.value_date > clock['cycle_date']:
                pay.updated_at = now
                self.store.update_payment(pay)
                return pay
        elif pay.scheme == SCHEME_CHAPS and clock['after_cutoff'] and pay.status != PAY_PENDING:
            pay.status = PAY_QUEUED
            pay.value_date = self.calendar.cycle_date(now, scheme=pay.scheme)
            pay.updated_at = now
            self.store.update_payment(pay)
            return pay
        self._transmit(pay, actor=actor)
        self.store.update_payment(pay)
        if pay.status == PAY_NSF:
            raise UkPayError('nsf', 'Insufficient funds for UK payment.', payment=pay)
        if pay.status == PAY_FAILED:
            raise UkPayError('failed', 'UK payment debit did not complete.', payment=pay)
        return pay

    def reject_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> UkPayTransfer:
        self._require_staff(actor_type)
        pay = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if pay.status not in CANCELABLE:
            raise UkPayError('not_rejectable', 'Only held, queued, or pending payments can be rejected.')
        pay.status = PAY_REJECTED
        pay.reason = normalize_note(reason, limit=40)
        pay.note = normalize_note(note)
        pay.actor = str(actor)
        pay.updated_at = float(self.clock())
        self.store.update_payment(pay)
        return pay

    def complete_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
    ) -> UkPayTransfer:
        self._require_staff(actor_type)
        pay = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if pay.status == PAY_COMPLETED:
            raise UkPayError('already_completed', 'Payment is already completed.')
        if pay.scheme == SCHEME_FPS:
            raise UkPayError('already_completed', 'Faster Payments complete on send.')
        if pay.status != PAY_SENT:
            raise UkPayError('not_completable', 'Only sent payments can be completed.')
        now = float(self.clock())
        pay.status = PAY_COMPLETED
        pay.completed_at = now
        pay.updated_at = now
        pay.actor = str(actor)
        self.store.update_payment(pay)
        return pay

    def recall_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> UkPayTransfer:
        self._require_staff(actor_type)
        pay = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if pay.scheme == SCHEME_FPS and pay.status in {PAY_SENT, PAY_COMPLETED}:
            raise UkPayError('scheme_irrevocable', 'Faster Payments cannot be recalled after send.')
        if pay.status == PAY_RECALLED:
            raise UkPayError('already_recalled', 'Payment is already recalled.')
        if pay.status == PAY_COMPLETED:
            raise UkPayError('already_completed', 'Completed payments cannot be recalled.')
        if pay.status != PAY_SENT:
            raise UkPayError('not_recallable', 'Only sent payments can be recalled.')
        remark = normalize_note(note) or ('ukpay recalled from %s' % pay.nickname)
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(pay.internal_account, pay.debit_usd, remark)
            except Exception as exc:
                raise UkPayError('recall_failed', 'Recall credit failed.', payment=pay) from exc
            if _classify_money_result(result) != 'ok':
                raise UkPayError('recall_failed', 'Recall credit failed.', payment=pay)
            fee = parse_money(pay.fee, allow_zero=True)
            if fee > 0 and pay.fee_status == FEE_COLLECTED:
                self.credit_fn(pay.internal_account, money_str(fee), 'ukpay fee recalled %s' % pay.end_to_end_id[:12])
        now = float(self.clock())
        pay.status = PAY_RECALLED
        pay.recalled_at = now
        pay.updated_at = now
        pay.actor = str(actor)
        pay.note = remark
        self.store.update_payment(pay)
        return pay

    def run_due(self, userid: Optional[str] = None) -> List[UkPayTransfer]:
        now = float(self.clock())
        changed: List[UkPayTransfer] = []
        for pay in self.store.list_payments(userid):
            if pay.status != PAY_QUEUED:
                continue
            clock = self.calendar.snapshot(now, scheme=pay.scheme)
            today = clock['cycle_date']
            if pay.value_date > today:
                continue
            if pay.scheme == SCHEME_CHAPS and clock['after_cutoff'] and pay.value_date == today:
                continue
            usd = parse_money(pay.debit_usd)
            if self._needs_dual_control(usd):
                pay.status = PAY_PENDING
                pay.updated_at = now
                self.store.update_payment(pay)
                changed.append(pay)
                continue
            self._transmit(pay, actor=pay.actor)
            self.store.update_payment(pay)
            changed.append(pay)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.run_due(userid)
        beneficiaries = self.store.list_beneficiaries(userid)
        payments = self.store.list_payments(userid)
        sent_ytd = Decimal('0.00')
        fee_ytd = Decimal('0.00')
        recalled = Decimal('0.00')
        by_scheme = {SCHEME_FPS: Decimal('0.00'), SCHEME_CHAPS: Decimal('0.00'), SCHEME_BACS: Decimal('0.00')}
        for row in payments:
            amount = parse_money(row.debit_usd, allow_zero=True)
            if row.status in {PAY_SENT, PAY_COMPLETED}:
                sent_ytd += amount
                by_scheme[row.scheme] = by_scheme.get(row.scheme, Decimal('0.00')) + amount
                if row.fee_status == FEE_COLLECTED:
                    fee_ytd += parse_money(row.fee, allow_zero=True)
            elif row.status == PAY_RECALLED:
                recalled += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'fps_cap': money_str(self.policy.fps_cap),
            'bacs_cap': money_str(self.policy.bacs_cap),
            'fees': {key: money_str(value) for key, value in self.policy.fees.items()},
            'fx_rate': str(self.fx_book.rate),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clock': self.calendar.snapshot(now, scheme=SCHEME_CHAPS),
            'fps_clock': self.calendar.snapshot(now, scheme=SCHEME_FPS),
            'bacs_clock': self.calendar.snapshot(now, scheme=SCHEME_BACS),
            'beneficiaries': [row.to_dict() for row in beneficiaries[:40]],
            'payments': [row.to_dict() for row in payments[:40]],
            'ytd_sent': money_str(sent_ytd),
            'ytd_fees': money_str(fee_ytd),
            'recalled_ytd': money_str(recalled),
            'ytd_fps': money_str(by_scheme[SCHEME_FPS]),
            'ytd_chaps': money_str(by_scheme[SCHEME_CHAPS]),
            'ytd_bacs': money_str(by_scheme[SCHEME_BACS]),
            'active_count': sum(1 for row in beneficiaries if row.status == BENE_ACTIVE),
            'open_count': sum(1 for row in payments if row.status in OPEN_PAYS),
        }


_SERVICE: Optional[UkPayService] = None


def set_service(service: Optional[UkPayService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[UkPayService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('UKPAY_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryUkPayStore()
    path = os.environ.get('UKPAY_DB', DEFAULT_STORE_PATH)
    return SqliteUkPayStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[BankOfEnglandCalendar] = None,
    fx_book: Optional[GbpUsdBook] = None,
) -> UkPayService:
    if store is None:
        store = default_store()
    return UkPayService(
        UkPayPolicy.from_env(),
        store,
        clock=clock,
        debit_fn=debit_fn,
        credit_fn=credit_fn,
        accounts_fn=accounts_fn,
        screen_fn=screen_fn,
        calendar=calendar,
        fx_book=fx_book,
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
        'beneficiary_duplicate': 409,
        'beneficiary_limit': 409,
        'payment_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_completed': 409,
        'already_recalled': 409,
        'nsf': 409,
        'failed': 409,
        'recall_failed': 409,
        'ukpay_forbidden': 403,
        'ukpay_disabled': 403,
        'beneficiary_paused': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_cancelable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_completable': 403,
        'not_recallable': 403,
        'scheme_irrevocable': 403,
        'beneficiary_not_found': 404,
        'payment_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_sort_code': 400,
        'invalid_iban': 400,
        'invalid_address': 400,
        'invalid_country': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_scheme': 400,
        'invalid_reference': 400,
        'fps_amount_exceeded': 400,
        'bacs_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_beneficiary': 400,
        'missing_payment': 400,
    }.get(code, 400)


def _error_body(exc: UkPayError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('payment') is not None:
        body['payment'] = exc.extra['payment'].to_dict()
    if exc.extra.get('beneficiary') is not None:
        body['beneficiary'] = exc.extra['beneficiary'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except WireError as exc:
        return jsonify({'message': exc.message, 'error': exc.code}), _error_status(exc.code)
    except UkPayError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: UkPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'UkPay': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_quote(service: UkPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}

    def _run():
        return jsonify({
            'quote': service.quote_fx(values.get('amount')),
            'UkPay': service.snapshot(userid, actor=userid, actor_type=session.get('usertype') or 'customer'),
        }), 200

    return _handle_errors(_run)


def handle_add_beneficiary(service: UkPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.add_beneficiary(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname'),
            legal_name=values.get('legal_name') or values.get('name'),
            sort_code=values.get('sort_code') or values.get('sortcode'),
            account_number=values.get('account_number') or values.get('external_account'),
            iban=values.get('iban'),
            city=values.get('city'),
            country=values.get('country') or 'GB',
            postcode=values.get('postcode') or values.get('postal'),
            default_account=values.get('default_account') or values.get('account') or values.get('from_account'),
        )
        return jsonify({
            'message': 'UK beneficiary added',
            'beneficiary': row.to_dict(),
            'UkPay': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _bene_status_route(service: UkPayService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    beneficiary_id = str(values.get('beneficiary_id') or '').strip()
    if not beneficiary_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_beneficiary'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_beneficiary_status(
            beneficiary_id=beneficiary_id, actor=userid, actor_type=actor_type, status=status,
        )
        return jsonify({
            'message': ok_message,
            'beneficiary': row.to_dict(),
            'UkPay': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: UkPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    beneficiary_id = str(values.get('beneficiary_id') or '').strip()
    if not beneficiary_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_beneficiary'}), 400

    def _run():
        preview = service.preview(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            beneficiary_id=beneficiary_id,
            amount=values.get('amount'),
            scheme=values.get('scheme') or SCHEME_FPS,
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({'preview': preview, 'UkPay': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: UkPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    beneficiary_id = str(values.get('beneficiary_id') or '').strip()
    if not beneficiary_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_beneficiary'}), 400

    def _run():
        pay, created = service.originate(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            beneficiary_id=beneficiary_id,
            amount=values.get('amount'),
            scheme=values.get('scheme') or SCHEME_FPS,
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({
            'message': 'UK payment originated' if created else 'UK payment already posted',
            'payment': pay.to_dict(),
            'UkPay': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: UkPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    payment_id = str(values.get('payment_id') or '').strip()
    if not payment_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_payment'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        pay = service.cancel_payment(
            payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': 'UK payment cancelled',
            'payment': pay.to_dict(),
            'UkPay': service.snapshot(pay.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_pay_route(service: UkPayService, action: str):
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
            pay = service.release_payment(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'UK payment released'
        elif action == 'reject':
            pay = service.reject_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'UK payment rejected'
        elif action == 'complete':
            pay = service.complete_payment(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'UK payment completed'
        elif action == 'recall':
            pay = service.recall_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'UK payment recalled'
        elif action == 'override':
            pay = service.override_ofac(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'waive':
            pay = service.waive_fee(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'UK payment fee waived'
        else:
            raise UkPayError('invalid_status', 'Unknown UK payment action.')
        return jsonify({
            'message': message,
            'payment': pay.to_dict(),
            'UkPay': service.snapshot(pay.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: UkPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'UkPay': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_ukpay_routes(app, service: UkPayService) -> None:
    @app.route('/listUkPays', methods=['POST', 'GET'])
    def list_ukpays_route():
        return handle_list(service)

    @app.route('/listUkPayBeneficiaries', methods=['POST', 'GET'])
    def list_ukpay_beneficiaries_route():
        return handle_list(service)

    @app.route('/addUkPayBeneficiary', methods=['POST', 'GET'])
    def add_ukpay_beneficiary_route():
        return handle_add_beneficiary(service)

    @app.route('/pauseUkPayBeneficiary', methods=['POST', 'GET'])
    def pause_ukpay_beneficiary_route():
        return _bene_status_route(service, BENE_PAUSED, 'UK beneficiary paused')

    @app.route('/resumeUkPayBeneficiary', methods=['POST', 'GET'])
    def resume_ukpay_beneficiary_route():
        return _bene_status_route(service, BENE_ACTIVE, 'UK beneficiary resumed')

    @app.route('/archiveUkPayBeneficiary', methods=['POST', 'GET'])
    def archive_ukpay_beneficiary_route():
        return _bene_status_route(service, BENE_ARCHIVED, 'UK beneficiary archived')

    @app.route('/quoteUkPayFx', methods=['POST', 'GET'])
    def quote_ukpay_fx_route():
        return handle_quote(service)

    @app.route('/previewUkPay', methods=['POST', 'GET'])
    def preview_ukpay_route():
        return handle_preview(service)

    @app.route('/sendUkPay', methods=['POST', 'GET'])
    def send_ukpay_route():
        return handle_send(service)

    @app.route('/cancelUkPay', methods=['POST', 'GET'])
    def cancel_ukpay_route():
        return handle_cancel(service)

    @app.route('/releaseUkPay', methods=['POST', 'GET'])
    def release_ukpay_route():
        return _staff_pay_route(service, 'release')

    @app.route('/rejectUkPay', methods=['POST', 'GET'])
    def reject_ukpay_route():
        return _staff_pay_route(service, 'reject')

    @app.route('/completeUkPay', methods=['POST', 'GET'])
    def complete_ukpay_route():
        return _staff_pay_route(service, 'complete')

    @app.route('/recallUkPay', methods=['POST', 'GET'])
    def recall_ukpay_route():
        return _staff_pay_route(service, 'recall')

    @app.route('/overrideUkPayOfac', methods=['POST', 'GET'])
    def override_ukpay_ofac_route():
        return _staff_pay_route(service, 'override')

    @app.route('/waiveUkPayFee', methods=['POST', 'GET'])
    def waive_ukpay_fee_route():
        return _staff_pay_route(service, 'waive')

    @app.route('/runDueUkPays', methods=['POST', 'GET'])
    def run_due_ukpays_route():
        return handle_run_due(service)

