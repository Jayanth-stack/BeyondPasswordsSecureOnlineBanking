"""Inbound SEPA Credit Transfer / Instant receive posting from an operator file.

Staff ingest incoming ISO 20022 pacs.008 EUR credits (SCT or SCT Instant)
and credit the beneficiary customer in USD. SCT Instant posts 24/7; SCT
follows the TARGET2 calendar and 16:00 CEST cutoff. Independent of
outbound SEPA SCT (PR #80), inbound BACS (PR #99), inbound FPS/CHAPS
(PR #96), inbound FedNow/RTP (PR #93), inbound Fedwire (PR #90), UK Pay
origination (PR #87), outbound Fedwire (PR #73), ACH linking (PR #68),
and bill-pay ACH (PR #66). Existing `/fundTransfer`, `/withdrawAmount`,
and `/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- IBAN ISO 13616 mod-97 checksum restricted to the EPC SEPA zone
- BIC / SWIFT ISO 9362 (8→11 pad XXX; location cannot start with 0)
- EUR-only + EURUSD quote book (USD ledger credit equivalent)
- TARGET2 business-day / SCT cutoff clock (Instant is 24/7)
- ISO 20022 pacs.008 parse / compose / multi-document file split (XXE rejected)
- EndToEndId / TxId uniqueness (idempotent ingest)
- Receiver-BIC acceptance (this bank)
- Creditor IBAN → internal account (BBAN trailing digits) → customer directory
- Incoming credit posting + pacs.004 return with ISO reason codes
- OFAC-style originator screening (reused from utility.wire)
- Dual-control release for high-value inbound credits (USD equivalent)

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Full IBAN / originator account never appear in to_dict / snapshots.
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
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
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

SCHEME_SCT = 'sct'
SCHEME_INSTANT = 'sct_inst'
SCHEMES = frozenset({SCHEME_SCT, SCHEME_INSTANT})
SCHEME_ALIASES = {
    'sct': SCHEME_SCT, 'sepa': SCHEME_SCT, 'credit': SCHEME_SCT,
    'standard': SCHEME_SCT, 'trf': SCHEME_SCT, 'target': SCHEME_SCT,
    'target2': SCHEME_SCT, 'sctcredit': SCHEME_SCT,
    'sct_inst': SCHEME_INSTANT, 'sctinst': SCHEME_INSTANT, 'inst': SCHEME_INSTANT,
    'instant': SCHEME_INSTANT, 'sct-inst': SCHEME_INSTANT,
    'sepa_instant': SCHEME_INSTANT, 'rtp': SCHEME_INSTANT,
}
CLR_SYS_ALIASES = {
    'sct': SCHEME_SCT, 'sepa': SCHEME_SCT, 'trf': SCHEME_SCT, 'target': SCHEME_SCT,
    'inst': SCHEME_INSTANT, 'sctinst': SCHEME_INSTANT, 'instant': SCHEME_INSTANT,
}

SEPA_IBAN_LENGTHS = {
    'AD': 24, 'AT': 20, 'BE': 16, 'BG': 22, 'CH': 21, 'CY': 28, 'CZ': 24,
    'DE': 22, 'DK': 18, 'EE': 20, 'ES': 24, 'FI': 18, 'FR': 27, 'GB': 22,
    'GI': 23, 'GR': 27, 'HR': 21, 'HU': 28, 'IE': 22, 'IS': 26, 'IT': 27,
    'LI': 21, 'LT': 20, 'LU': 20, 'LV': 21, 'MC': 27, 'MT': 31, 'NL': 18,
    'NO': 15, 'PL': 28, 'PT': 25, 'RO': 24, 'SE': 24, 'SI': 19, 'SK': 24,
    'SM': 27, 'VA': 22,
}
SEPA_COUNTRIES = frozenset(SEPA_IBAN_LENGTHS)

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
DEFAULT_STORE_PATH = 'SystemLogs/insepa.sqlite'
DEFAULT_RECEIVER_BIC = 'KNHADEFFXXX'
DEFAULT_EURUSD = Decimal('1.080000')
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)
MONEY_QUANTUM = Decimal('0.01')
RATE_QUANTUM = Decimal('0.000001')
CUSTOMER_RETURN_SECONDS = 24 * 60 * 60
BIC_RE = re.compile(r'^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$')
_DOCTYPE = re.compile(r'<!DOCTYPE|<!ENTITY', re.I)
_SPLIT = re.compile(r'(?=(?:\s*<\?xml|\s*<Document|\s*<pacs\.008))', re.I)


class InSepaError(ValueError):
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


def normalize_scheme(value: Any, *, default: str = SCHEME_SCT) -> str:
    text = re.sub(r'[^a-z0-9]', '', str(value or default).strip().lower())
    mapped = SCHEME_ALIASES.get(text, CLR_SYS_ALIASES.get(text, text))
    if mapped not in SCHEMES:
        raise InSepaError('invalid_scheme', 'Scheme must be sct or sct_inst.')
    return mapped


def normalize_source(value: Any, *, default: str = 'KONOHA01') -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or default).upper())
    if not text:
        text = default
    return (text + 'XXXXXXXX')[:8]


def iban_mod97(digits: str) -> int:
    """Chunked mod-97 so we never build a 10^60 int from a 34-char IBAN."""
    remainder = 0
    for index in range(0, len(digits), 9):
        remainder = int(str(remainder) + digits[index:index + 9]) % 97
    return remainder


def iban_check_digit_ok(iban: str) -> bool:
    """ISO 13616: rearrange, A=10..Z=35, remaining value mod 97 equals 1."""
    compact = re.sub(r'[^A-Z0-9]', '', str(iban or '').upper())
    if len(compact) < 15 or len(compact) > 34 or not compact[:2].isalpha() or not compact[2:4].isdigit():
        return False
    rearranged = compact[4:] + compact[:4]
    numeric = []
    for ch in rearranged:
        if ch.isdigit():
            numeric.append(ch)
        elif ch.isalpha():
            numeric.append(str(ord(ch) - 55))
        else:
            return False
    return iban_mod97(''.join(numeric)) == 1


def compose_iban(country: str, bban: str) -> str:
    """Build a SEPA-zone IBAN with valid ISO 13616 check digits."""
    cc = re.sub(r'[^A-Z]', '', str(country or '').upper())
    body = re.sub(r'[^A-Z0-9]', '', str(bban or '').upper())
    if cc not in SEPA_COUNTRIES:
        raise InSepaError('not_sepa_country', 'IBAN country is outside the SEPA zone.')
    expected = SEPA_IBAN_LENGTHS[cc] - 4
    if len(body) != expected:
        raise InSepaError('invalid_iban', 'BBAN length does not match country %s.' % cc)
    rearranged = body + cc + '00'
    numeric = []
    for ch in rearranged:
        numeric.append(ch if ch.isdigit() else str(ord(ch) - 55))
    check = 98 - iban_mod97(''.join(numeric))
    iban = '%s%02d%s' % (cc, check, body)
    if not iban_check_digit_ok(iban):
        raise InSepaError('invalid_iban', 'Failed to compose a valid IBAN.')
    return iban


def normalize_iban(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[^A-Za-z0-9]', '', str(value or '')).upper()
    if not text:
        if required:
            raise InSepaError('invalid_iban', 'IBAN is required.')
        return ''
    country = text[:2]
    if country not in SEPA_COUNTRIES:
        raise InSepaError('not_sepa_country', 'IBAN country is outside the SEPA zone.')
    expected = SEPA_IBAN_LENGTHS[country]
    if len(text) != expected:
        raise InSepaError('invalid_iban', 'IBAN length does not match country %s.' % country)
    if not iban_check_digit_ok(text):
        raise InSepaError('invalid_iban', 'IBAN failed mod-97 checksum.')
    return text


def mask_iban(iban: str) -> str:
    compact = re.sub(r'[^A-Z0-9]', '', str(iban or '').upper())
    if not compact:
        return ''
    if len(compact) <= 6:
        return compact[:2] + '****'
    return compact[:2] + '****' + compact[-4:]


def extract_iban_account(iban: str) -> str:
    """Map SEPA IBAN BBAN trailing digits onto an internal account id."""
    compact = normalize_iban(iban)
    digits = ''.join(ch for ch in compact[4:] if ch.isdigit())
    if not digits:
        raise InSepaError('invalid_account', 'IBAN does not contain an account number.')
    for width in (10, 8, 12, 7, 6, 4):
        if len(digits) >= width:
            try:
                return normalize_account(digits[-width:])
            except AccountError:
                continue
    try:
        return normalize_account(digits[-4:] if len(digits) >= 4 else digits)
    except AccountError as exc:
        raise InSepaError('invalid_account', 'IBAN account is not a valid internal account.') from exc


def normalize_bic(value: Any, *, required: bool = True) -> str:
    text = re.sub(r'[^A-Za-z0-9]', '', str(value or '')).upper()
    if not text:
        if required:
            raise InSepaError('invalid_bic', 'BIC is required.')
        return ''
    if len(text) == 8:
        text = text + 'XXX'
    if not BIC_RE.match(text):
        raise InSepaError('invalid_bic', 'BIC must be 8 or 11 characters (ISO 9362).')
    country = text[4:6]
    if country not in SEPA_COUNTRIES:
        raise InSepaError('invalid_bic', 'BIC country is outside the SEPA zone.')
    if text[6] == '0':
        raise InSepaError('invalid_bic', 'BIC location code cannot start with 0.')
    return text


def format_bic(value: str) -> str:
    try:
        return normalize_bic(value, required=False) or str(value or '')
    except InSepaError:
        return str(value or '')


def bic8(value: str) -> str:
    text = format_bic(value)
    return text[:8]


def normalize_scheme_id(value: Any) -> str:
    text = re.sub(r'[^A-Za-z0-9./-]', '', str(value or '').strip())
    if not (8 <= len(text) <= 35):
        raise InSepaError('invalid_scheme_id', 'EndToEndId / TxId must be 8-35 characters.')
    return text.upper()


def compose_message_id(cycle_date: str, source: str, sequence: int) -> str:
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise InSepaError('invalid_reference', 'Message sequence out of range.')
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise InSepaError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    return '%s%s%06d' % (day, normalize_source(source), seq)


def compose_end_to_end_id(scheme: str, cycle_date: str, sequence: int) -> str:
    prefix = 'INST' if normalize_scheme(scheme) == SCHEME_INSTANT else 'SCT'
    return (prefix + compose_message_id(cycle_date, 'E2E', sequence)[8:])[:35]


def compose_scheme_id(scheme: str, cycle_date: str, source: str, sequence: int) -> str:
    prefix = 'INST' if normalize_scheme(scheme) == SCHEME_INSTANT else 'SCT'
    return prefix + compose_message_id(cycle_date, source, sequence)


def normalize_end_to_end_id(value: Any) -> str:
    text = re.sub(r'[^A-Za-z0-9/\-?:().,\'+ ]', '', str(value or '').strip())
    text = re.sub(r'\s+', '', text)
    if not (1 <= len(text) <= 35):
        raise InSepaError('invalid_end_to_end_id', 'EndToEndId must be 1-35 characters.')
    return text


def normalize_msg_id(value: Any) -> str:
    text = re.sub(r'[^A-Za-z0-9./-]', '', str(value or '').strip())
    if not text:
        raise InSepaError('invalid_msg_id', 'MsgId is required.')
    return text[:35]


def compose_msg_id(prefix: str, sequence: int) -> str:
    return ('%s%06d' % (re.sub(r'[^A-Z0-9]', '', prefix.upper())[:8] or 'MSG', sequence))[:35]


def normalize_return_reason(value: Any, *, default: str = 'MS03') -> str:
    text = str(value or default).strip().upper().replace('-', '_').replace(' ', '_')
    mapped = RETURN_ALIASES.get(text.lower(), text)
    if mapped not in RETURN_REASONS:
        raise InSepaError('invalid_reason', 'Unknown inbound return reason.')
    return mapped


def normalize_currency(value: Any) -> str:
    text = str(value or 'EUR').strip().upper()
    if text != 'EUR':
        raise InSepaError('invalid_currency', 'Inbound SEPA credits must be EUR.')
    return text


def parse_rate(value: Any) -> Decimal:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise InSepaError('invalid_rate', 'FX rate is required.')
    try:
        rate = Decimal(str(value).strip().replace(',', ''))
    except (InvalidOperation, ValueError):
        raise InSepaError('invalid_rate', 'FX rate is invalid.') from None
    if not rate.is_finite() or rate <= 0:
        raise InSepaError('invalid_rate', 'FX rate must be positive.')
    return rate.quantize(RATE_QUANTUM, rounding=ROUND_HALF_EVEN)


def easter_gregorian(year: int) -> date:
    """Anonymous Gregorian algorithm. TARGET2 Good Friday / Easter Monday depend on this."""
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
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def target2_holidays(year: int) -> set:
    """TARGET2 closing days. Weekend-falling holidays are NOT observed to a weekday."""
    easter = easter_gregorian(year)
    return {
        date(year, 1, 1),
        easter - timedelta(days=2),
        easter + timedelta(days=1),
        date(year, 5, 1),
        date(year, 12, 25),
        date(year, 12, 26),
    }


@dataclass(frozen=True)
class FxQuote:
    ccy: str
    amount_eur: str
    amount_usd: str
    rate: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'ccy': self.ccy,
            'amount_eur': self.amount_eur,
            'amount_usd': self.amount_usd,
            'rate': self.rate,
        }


class EurUsdBook:
    """Injectable EURUSD book. Inbound amounts arrive in EUR; the ledger credits USD."""

    def __init__(self, rate: Optional[Decimal] = None) -> None:
        self.rate = parse_rate(rate if rate is not None else DEFAULT_EURUSD)

    def quote(self, euros: Decimal) -> FxQuote:
        usd = (euros * self.rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
        return FxQuote('EUR', money_str(euros), money_str(usd), str(self.rate))


class Target2Calendar:
    """SCT business-day clock. SCT Instant ignores weekends and cutoff."""

    def __init__(
        self,
        *,
        cutoff_hour: int = 16,
        tz_offset_hours: int = 2,
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
        return day in target2_holidays(day.year) or day in self.extra_holidays

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

    def value_date(self, ts: float, *, scheme: str = SCHEME_SCT) -> date:
        if scheme == SCHEME_INSTANT:
            return self.local_dt(ts).date()
        return self.input_date(ts)

    def cycle_date(self, ts: float, *, scheme: str = SCHEME_SCT) -> str:
        return self.value_date(ts, scheme=scheme).strftime('%Y%m%d')

    def should_queue(self, ts: float, *, scheme: str) -> bool:
        if scheme == SCHEME_INSTANT:
            return False
        local = self.local_dt(ts)
        return self.is_after_cutoff(ts) or not self.is_business_day(local.date())

    def snapshot(self, ts: float, *, scheme: str = SCHEME_SCT) -> Dict[str, Any]:
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
            'instant': scheme == SCHEME_INSTANT,
            'rail_hours': '24x7' if scheme == SCHEME_INSTANT else 'TARGET2 07:00-16:00',
            'calendar': '24x7' if scheme == SCHEME_INSTANT else 'TARGET2',
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
        raise InSepaError('invalid_pacs', 'XML entities are not allowed.')
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise InSepaError('invalid_pacs', 'ISO 20022 XML is not well-formed.') from exc


def parse_pacs008(text: Any) -> ET.Element:
    root = parse_xml_safe(text)
    if _local(root.tag) not in {'Document', 'FIToFICstmrCdtTrf', 'pacs.008.001.08'}:
        if _find(root, 'FIToFICstmrCdtTrf', 'CdtTrfTxInf') is None:
            raise InSepaError('invalid_pacs', 'Document is not a pacs.008 credit transfer.')
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
    """Compose a minimal pacs.008.001.08 EUR SEPA credit transfer."""
    scheme = normalize_scheme(fields.get('scheme') or fields.get('rail') or SCHEME_SCT)
    clr = 'INST' if scheme == SCHEME_INSTANT else 'SCT'
    instr = 'INST' if scheme == SCHEME_INSTANT else 'TRF'
    amount = parse_money(fields.get('amount') or fields.get('amount_eur'))
    creditor_iban = fields.get('iban') or fields.get('beneficiary_iban') or fields.get('beneficiary_account')
    debtor_iban = fields.get('originator_iban') or fields.get('originator_account') or ''
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
        '<PmtTpInf><SvcLvl><Cd>SEPA</Cd></SvcLvl><LclInstrm><Cd>%s</Cd></LclInstrm></PmtTpInf>'
        '<IntrBkSttlmAmt Ccy="EUR">%s</IntrBkSttlmAmt>'
        '<ChrgBr>SLEV</ChrgBr>'
        '<InstgAgt><FinInstnId><BICFI>%s</BICFI></FinInstnId></InstgAgt>'
        '<InstdAgt><FinInstnId><BICFI>%s</BICFI></FinInstnId></InstdAgt>'
        '<Dbtr><Nm>%s</Nm></Dbtr>'
        '<DbtrAcct><Id><IBAN>%s</IBAN></Id></DbtrAcct>'
        '<Cdtr><Nm>%s</Nm></Cdtr>'
        '<CdtrAcct><Id><IBAN>%s</IBAN></Id></CdtrAcct>'
        '<RmtInf><Ustrd>%s</Ustrd></RmtInf>'
        '</CdtTrfTxInf>'
        '</FIToFICstmrCdtTrf>'
        '</Document>'
    ) % (
        PACS008_NS,
        _escape(fields.get('msg_id') or 'MSG1'),
        _escape(fields.get('created') or '2024-06-14T13:00:00Z'),
        clr,
        _escape(fields.get('instr_id') or fields.get('scheme_id') or 'INSTR1'),
        _escape(fields.get('end_to_end_id') or 'E2E1'),
        _escape(fields.get('scheme_id') or fields.get('tx_id') or 'TX1'),
        instr,
        money_str(amount),
        _escape(fields.get('sender_bic') or fields.get('sender')),
        _escape(fields.get('receiver_bic') or fields.get('receiver') or DEFAULT_RECEIVER_BIC),
        _escape(fields.get('originator_name') or 'ORIGINATOR'),
        _escape(debtor_iban),
        _escape(fields.get('beneficiary_name') or 'BENEFICIARY'),
        _escape(creditor_iban),
        _escape(fields.get('memo') or ''),
    )


def compose_pacs004(
    row: 'InboundSepa',
    *,
    return_msg_id: str,
    reason: str,
    receiver_bic: str,
) -> str:
    """pacs.004 payment return of an inbound SEPA credit."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Document xmlns="%s">'
        '<PmtRtr>'
        '<GrpHdr><MsgId>%s</MsgId><NbOfTxs>1</NbOfTxs></GrpHdr>'
        '<TxInf>'
        '<OrgnlEndToEndId>%s</OrgnlEndToEndId>'
        '<OrgnlTxId>%s</OrgnlTxId>'
        '<RtrdIntrBkSttlmAmt Ccy="EUR">%s</RtrdIntrBkSttlmAmt>'
        '<InstgAgt><FinInstnId><BICFI>%s</BICFI></FinInstnId></InstgAgt>'
        '<InstdAgt><FinInstnId><BICFI>%s</BICFI></FinInstnId></InstdAgt>'
        '<RtrRsnInf><Rsn><Cd>%s</Cd></Rsn></RtrRsnInf>'
        '</TxInf>'
        '</PmtRtr>'
        '</Document>'
    ) % (
        PACS004_NS,
        _escape(return_msg_id),
        _escape(row.end_to_end_id),
        _escape(row.scheme_id),
        row.amount_eur,
        _escape(receiver_bic),
        _escape(row.sender_bic),
        _escape(reason),
    )


def _member_ids(root: ET.Element) -> Tuple[str, str]:
    instg = _find(root, 'InstgAgt')
    instd = _find(root, 'InstdAgt')
    sender = _text(instg, 'BICFI') or _text(instg, 'BIC') or _text(instg, 'MmbId')
    receiver = _text(instd, 'BICFI') or _text(instd, 'BIC') or _text(instd, 'MmbId')
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


def _beneficiary_from_account_field(raw: str) -> Tuple[str, str]:
    """Return (internal_account, normalized_iban)."""
    text = str(raw or '').strip()
    if not text:
        raise InSepaError('invalid_account', 'Beneficiary IBAN or account is required.')
    compact = re.sub(r'[^A-Z0-9]', '', text.upper())
    if len(compact) >= 15 and compact[:2].isalpha() and compact[2:4].isdigit():
        iban = normalize_iban(compact)
        return extract_iban_account(iban), iban
    digits = ''.join(ch for ch in text if ch.isdigit())
    try:
        return normalize_account(digits), ''
    except AccountError as exc:
        raise InSepaError('invalid_account', 'Beneficiary account is invalid.') from exc


def message_from_pacs(text: Any) -> Dict[str, Any]:
    """Reusable pacs.008 → inbound SEPA field map."""
    root = parse_pacs008(text)
    amount_el = _find(root, 'IntrBkSttlmAmt')
    if amount_el is None:
        amount_el = _find(root, 'InstdAmt')
    if amount_el is None or not (amount_el.text or '').strip():
        raise InSepaError('invalid_amount', 'Settlement amount is required.')
    normalize_currency(amount_el.get('Ccy') or amount_el.get('ccy') or 'EUR')
    amount = parse_money(amount_el.text)
    sender_raw, receiver_raw = _member_ids(root)
    sender_bic = normalize_bic(sender_raw)
    receiver_bic = normalize_bic(receiver_raw)
    e2e = normalize_end_to_end_id(_text(root, 'EndToEndId'))
    msg_id = normalize_msg_id(_text(root, 'MsgId') or ('MSG' + e2e[:12]))
    tx_id = _text(root, 'TxId') or e2e
    try:
        scheme_id = normalize_scheme_id(tx_id)
    except InSepaError:
        scheme_id = normalize_scheme_id(e2e if len(e2e) >= 8 else ('E2E' + e2e + 'XXXXXXX'))
    clr = _text(_find(root, 'ClrSys'), 'Cd') or _text(_find(root, 'ClrSys'), 'Prtry')
    instr = _text(_find(root, 'LclInstrm'), 'Cd')
    scheme = normalize_scheme(clr or instr or SCHEME_SCT)
    originator = _text(_find(root, 'Dbtr'), 'Nm') or 'ORIGINATOR'
    beneficiary = _text(_find(root, 'Cdtr'), 'Nm') or 'BENEFICIARY'
    creditor_raw = _account_id(_find(root, 'CdtrAcct'))
    account, iban = _beneficiary_from_account_field(creditor_raw)
    originator_account = _account_id(_find(root, 'DbtrAcct'))
    return {
        'scheme_id': scheme_id,
        'end_to_end_id': e2e,
        'msg_id': msg_id,
        'scheme': scheme,
        'amount_eur': money_str(amount),
        'sender_bic': sender_bic,
        'receiver_bic': receiver_bic,
        'beneficiary_account': account,
        'originator_account': originator_account,
        'beneficiary_name': beneficiary,
        'originator_name': originator,
        'iban': iban,
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
    scheme = normalize_scheme(values.get('scheme') or values.get('rail') or SCHEME_SCT)
    amount = parse_money(values.get('amount') or values.get('amount_eur'))
    normalize_currency(values.get('currency') or values.get('ccy') or 'EUR')
    sender_bic = normalize_bic(values.get('sender_bic') or values.get('sender') or 'COBADEFFXXX')
    receiver_bic = normalize_bic(
        values.get('receiver_bic') or values.get('receiver') or DEFAULT_RECEIVER_BIC
    )
    iban_raw = values.get('iban') or values.get('beneficiary_iban') or ''
    account_raw = values.get('beneficiary_account') or values.get('account') or iban_raw
    account, iban = _beneficiary_from_account_field(account_raw)
    if iban_raw and not iban:
        iban = normalize_iban(iban_raw)
        account = extract_iban_account(iban)
    scheme_raw = (
        values.get('scheme_id') or values.get('tx_id') or values.get('end_to_end_id')
        or values.get('e2e')
    )
    if not scheme_raw:
        raise InSepaError('invalid_scheme_id', 'EndToEndId / TxId is required.')
    e2e_raw = values.get('end_to_end_id') or values.get('e2e') or scheme_raw
    msg_raw = values.get('msg_id') or values.get('msgid') or ('MSG' + re.sub(r'[^A-Za-z0-9]', '', str(scheme_raw))[:12])
    originator = str(values.get('originator_name') or values.get('originator') or '').strip()
    beneficiary = str(values.get('beneficiary_name') or values.get('beneficiary') or '').strip()
    originator_account = str(values.get('originator_account') or values.get('originator_iban') or '').strip()
    if originator_account:
        compact = re.sub(r'[^A-Z0-9]', '', originator_account.upper())
        if compact[:2].isalpha() and len(compact) >= 15:
            originator_account = normalize_iban(compact)
    return {
        'scheme_id': normalize_scheme_id(scheme_raw),
        'end_to_end_id': normalize_end_to_end_id(e2e_raw or ('E2E' + re.sub(r'[^A-Za-z0-9]', '', str(scheme_raw))[:16])),
        'msg_id': normalize_msg_id(msg_raw),
        'scheme': scheme,
        'amount_eur': money_str(amount),
        'sender_bic': sender_bic,
        'receiver_bic': receiver_bic,
        'beneficiary_account': account,
        'originator_account': originator_account,
        'beneficiary_name': beneficiary or 'BENEFICIARY',
        'originator_name': originator or 'ORIGINATOR',
        'iban': iban,
        'memo': normalize_note(values.get('memo') or values.get('remittance') or '', limit=140),
        'raw': '',
    }


@dataclass
class InSepaPolicy:
    enabled: bool = True
    customer_view: bool = True
    customer_return: bool = True
    allow_credit: bool = False
    max_inbounds: int = 240
    min_amount: Decimal = Decimal('0.01')
    max_amount: Decimal = Decimal('20000000.00')
    inst_max: Decimal = Decimal('100000.00')
    sct_max: Decimal = Decimal('20000000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    fx_rate: Decimal = Decimal('1.080000')
    cutoff_hour: int = 16
    tz_offset_hours: int = 2
    source_id: str = 'KONOHA01'
    receiver_bic: str = DEFAULT_RECEIVER_BIC
    customer_return_seconds: int = CUSTOMER_RETURN_SECONDS
    extra_holidays: Tuple[str, ...] = ()
    watchlist: Tuple[str, ...] = (
        'BLOCKED PERSON',
        'SANCTIONED ENTITY',
        'OFAC TESTNAME',
    )

    def scheme_max(self, scheme: str) -> Decimal:
        return self.sct_max if scheme == SCHEME_SCT else self.inst_max

    @classmethod
    def from_env(cls) -> 'InSepaPolicy':
        extra = _env_list('INSEPA_OFAC_LIST')
        watch = tuple(dict.fromkeys(cls.watchlist + extra))
        receiver = os.environ.get('INSEPA_RECEIVER_BIC') or DEFAULT_RECEIVER_BIC
        try:
            receiver = normalize_bic(receiver)
        except InSepaError:
            receiver = DEFAULT_RECEIVER_BIC
        return cls(
            enabled=_env_bool('INSEPA_ENABLED', True),
            customer_view=_env_bool('INSEPA_CUSTOMER_VIEW', True),
            customer_return=_env_bool('INSEPA_CUSTOMER_RETURN', True),
            allow_credit=_env_bool('INSEPA_ALLOW_CREDIT', False),
            max_inbounds=max(1, _env_int('INSEPA_MAX', 240)),
            min_amount=_env_money('INSEPA_MIN_AMOUNT', '0.01'),
            max_amount=_env_money('INSEPA_MAX_AMOUNT', '20000000.00'),
            inst_max=_env_money('INSEPA_INST_MAX', '100000.00'),
            sct_max=_env_money('INSEPA_SCT_MAX', '20000000.00'),
            dual_control_threshold=_env_money('INSEPA_DUAL_CONTROL', '10000.00'),
            fx_rate=_env_money('INSEPA_FX_RATE', '1.080000'),
            cutoff_hour=max(0, min(23, _env_int('INSEPA_CUTOFF_HOUR', 16))),
            tz_offset_hours=_env_int('INSEPA_TZ_OFFSET', 2),
            source_id=normalize_source(os.environ.get('INSEPA_SOURCE', 'KONOHA01')),
            receiver_bic=receiver,
            customer_return_seconds=max(60, _env_int('INSEPA_RETURN_WINDOW', CUSTOMER_RETURN_SECONDS)),
            extra_holidays=_env_list('INSEPA_HOLIDAYS'),
            watchlist=watch,
        )


@dataclass
class InboundSepa:
    inbound_id: str
    scheme_id: str
    end_to_end_id: str
    msg_id: str
    scheme: str
    userid: str
    internal_account: str
    amount_eur: str
    amount_usd: str
    fx_rate: str
    sender_bic: str
    receiver_bic: str
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
            'amount_eur': self.amount_eur,
            'amount_usd': self.amount_usd,
            'fx_rate': self.fx_rate,
            'sender_bic': format_bic(self.sender_bic),
            'receiver_bic': format_bic(self.receiver_bic),
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


def _clone(row: InboundSepa) -> InboundSepa:
    return InboundSepa(**{key: getattr(row, key) for key in row.__dataclass_fields__})


def _from_row(row: Any) -> InboundSepa:
    return InboundSepa(
        inbound_id=row['inbound_id'],
        scheme_id=row['scheme_id'],
        end_to_end_id=row['end_to_end_id'] or '',
        msg_id=row['msg_id'] or '',
        scheme=row['scheme'],
        userid=row['userid'] or '',
        internal_account=row['internal_account'] or '',
        amount_eur=row['amount_eur'],
        amount_usd=row['amount_usd'],
        fx_rate=row['fx_rate'],
        sender_bic=row['sender_bic'],
        receiver_bic=row['receiver_bic'],
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


class MemoryInSepaStore:
    def __init__(self) -> None:
        self._rows: Dict[str, InboundSepa] = {}
        self._by_scheme: Dict[str, str] = {}
        self._seq = 0
        self._lock = threading.Lock()

    def put(self, row: InboundSepa) -> None:
        with self._lock:
            self._rows[row.inbound_id] = _clone(row)
            self._by_scheme[row.scheme_id] = row.inbound_id

    def update(self, row: InboundSepa) -> None:
        with self._lock:
            if row.inbound_id not in self._rows:
                raise InSepaError('inbound_not_found', 'Inbound SEPA payment not found.')
            self._rows[row.inbound_id] = _clone(row)
            self._by_scheme[row.scheme_id] = row.inbound_id

    def get(self, inbound_id: str) -> Optional[InboundSepa]:
        with self._lock:
            row = self._rows.get(inbound_id)
            return _clone(row) if row is not None else None

    def get_by_scheme_id(self, scheme_id: str) -> Optional[InboundSepa]:
        with self._lock:
            inbound_id = self._by_scheme.get(scheme_id)
            row = self._rows.get(inbound_id) if inbound_id else None
            return _clone(row) if row is not None else None

    def list_for(self, userid: str) -> List[InboundSepa]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.userid == userid]
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def list_unmatched(self) -> List[InboundSepa]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.status == IN_UNMATCHED]
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def list_open(self) -> List[InboundSepa]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.status in OPEN_INBOUNDS]
            rows.sort(key=lambda item: item.created_at)
            return [_clone(row) for row in rows]

    def list_all(self) -> List[InboundSepa]:
        with self._lock:
            rows = list(self._rows.values())
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone(row) for row in rows]

    def next_sequence(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq


class SqliteInSepaStore:
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
                    amount_eur TEXT NOT NULL,
                    amount_usd TEXT NOT NULL,
                    fx_rate TEXT NOT NULL,
                    sender_bic TEXT NOT NULL,
                    receiver_bic TEXT NOT NULL,
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

    def _write(self, conn: sqlite3.Connection, row: InboundSepa) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO inbounds (
                inbound_id, scheme_id, end_to_end_id, msg_id, scheme, userid,
                internal_account, amount_eur, amount_usd, fx_rate, sender_bic,
                receiver_bic, originator_name, originator_account_last4,
                beneficiary_name, beneficiary_account, iban_masked, purpose, memo,
                status, value_date, actor, releaser, ofac_hit, ofac_match,
                return_msg_id, return_reason, created_at, updated_at, posted_at,
                returned_at, note, batch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.inbound_id, row.scheme_id, row.end_to_end_id, row.msg_id,
                row.scheme, row.userid, row.internal_account, row.amount_eur,
                row.amount_usd, row.fx_rate, row.sender_bic, row.receiver_bic,
                row.originator_name, row.originator_account_last4,
                row.beneficiary_name, row.beneficiary_account, row.iban_masked,
                row.purpose, row.memo, row.status, row.value_date, row.actor,
                row.releaser, int(row.ofac_hit), row.ofac_match, row.return_msg_id,
                row.return_reason, row.created_at, row.updated_at, row.posted_at,
                row.returned_at, row.note, row.batch_id,
            ),
        )

    def put(self, row: InboundSepa) -> None:
        with self._lock, self._connect() as conn:
            self._write(conn, row)
            conn.commit()

    def update(self, row: InboundSepa) -> None:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT inbound_id FROM inbounds WHERE inbound_id = ?', (row.inbound_id,),
            ).fetchone()
            if existing is None:
                raise InSepaError('inbound_not_found', 'Inbound SEPA payment not found.')
            self._write(conn, row)
            conn.commit()

    def get(self, inbound_id: str) -> Optional[InboundSepa]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def get_by_scheme_id(self, scheme_id: str) -> Optional[InboundSepa]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM inbounds WHERE scheme_id = ?', (scheme_id,),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def list_for(self, userid: str) -> List[InboundSepa]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE userid = ? ORDER BY created_at DESC',
                (userid,),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_unmatched(self) -> List[InboundSepa]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE status = ? ORDER BY created_at DESC',
                (IN_UNMATCHED,),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_open(self) -> List[InboundSepa]:
        with self._lock, self._connect() as conn:
            placeholders = ','.join('?' for _ in OPEN_INBOUNDS)
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE status IN (%s) ORDER BY created_at' % placeholders,
                tuple(OPEN_INBOUNDS),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def list_all(self) -> List[InboundSepa]:
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


class InSepaService:
    def __init__(
        self,
        policy: InSepaPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        lookup_fn: Optional[Callable[[str], Optional[str]]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        fx_book: Optional[EurUsdBook] = None,
        calendar: Optional[Target2Calendar] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.lookup_fn = lookup_fn
        self.screen_fn = screen_fn
        self.fx_book = fx_book or EurUsdBook(policy.fx_rate)
        self.calendar = calendar or Target2Calendar(
            cutoff_hour=policy.cutoff_hour,
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise InSepaError('insepa_disabled', 'Inbound SEPA payments are disabled.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise InSepaError('insepa_forbidden', 'Staff only.')

    def _require_view(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_view:
            raise InSepaError('insepa_forbidden', 'Customers cannot view inbound SEPA payments.')

    def _require_customer_return(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_return:
            raise InSepaError('insepa_forbidden', 'Customers cannot request inbound returns.')

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
            raise InSepaError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise InSepaError('credit_not_allowed', 'Credit accounts cannot receive inbound SEPA payments.')

    def _assert_amount(self, euros: Decimal, scheme: str) -> None:
        if euros < self.policy.min_amount:
            raise InSepaError('amount_out_of_range', 'Amount is outside the allowed range.')
        cap = self.policy.scheme_max(scheme)
        if euros > cap:
            code = 'sct_amount_exceeded' if scheme == SCHEME_SCT else 'sct_inst_amount_exceeded'
            raise InSepaError(code, 'Amount exceeds the %s cap.' % scheme)
        if euros > self.policy.max_amount:
            raise InSepaError('amount_out_of_range', 'Amount is outside the allowed range.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, usd: Decimal) -> bool:
        return usd >= self.policy.dual_control_threshold

    def _assert_receiver(self, receiver_bic: str) -> None:
        if receiver_bic != self.policy.receiver_bic:
            raise InSepaError('wrong_receiver', 'Message is not addressed to this bank.')

    def quote(self, euros: Decimal) -> FxQuote:
        return self.fx_book.quote(euros)

    def get_inbound(self, *, inbound_id: str, actor: str, actor_type: str) -> InboundSepa:
        self._require_view(actor_type)
        row = self.store.get(inbound_id)
        if row is None:
            raise InSepaError('inbound_not_found', 'Inbound SEPA payment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise InSepaError('insepa_forbidden', 'Not allowed to view this inbound SEPA payment.')
        return row

    def preview_message(self, values: Dict[str, Any]) -> Dict[str, Any]:
        self._require_enabled()
        message = message_from_values(values)
        euros = parse_money(message['amount_eur'])
        self._assert_amount(euros, message['scheme'])
        self._assert_receiver(message['receiver_bic'])
        fx = self.quote(euros)
        ofac = self._screen(message['originator_name'])
        userid = self._lookup(message['beneficiary_account'])
        now = float(self.clock())
        return {
            'message': {
                'scheme_id': message['scheme_id'],
                'end_to_end_id': message['end_to_end_id'],
                'msg_id': message['msg_id'],
                'scheme': message['scheme'],
                'amount_eur': message['amount_eur'],
                'sender_bic': format_bic(message['sender_bic']),
                'receiver_bic': format_bic(message['receiver_bic']),
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

    def _credit(self, row: InboundSepa) -> str:
        if self.credit_fn is None:
            return 'ok'
        remark = '%s from %s' % (row.scheme, row.originator_name[:20] or 'originator')
        result = self.credit_fn(row.internal_account, row.amount_usd, remark)
        return _classify_money_result(result)

    def _debit(self, row: InboundSepa) -> str:
        if self.debit_fn is None:
            return 'ok'
        remark = '%s return %s' % (row.scheme, row.scheme_id[:12])
        result = self.debit_fn(row.internal_account, row.amount_usd, remark)
        return _classify_money_result(result)

    def _try_post(self, row: InboundSepa) -> InboundSepa:
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
        raise InSepaError('failed', 'Inbound credit failed.', inbound=row)

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        values: Dict[str, Any],
        batch_id: str = '',
    ) -> Tuple[InboundSepa, bool]:
        self._require_staff(actor_type)
        message = message_from_values(values)
        euros = parse_money(message['amount_eur'])
        self._assert_amount(euros, message['scheme'])
        self._assert_receiver(message['receiver_bic'])
        existing = self.store.get_by_scheme_id(message['scheme_id'])
        if existing is not None:
            return existing, False
        if len(self.store.list_all()) >= self.policy.max_inbounds:
            raise InSepaError('inbound_limit', 'Inbound SEPA payment limit reached.')
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
        fx = self.quote(euros)
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
        row = InboundSepa(
            inbound_id=uuid.uuid4().hex,
            scheme_id=message['scheme_id'],
            end_to_end_id=message['end_to_end_id'],
            msg_id=message['msg_id'],
            scheme=message['scheme'],
            userid=userid or '',
            internal_account=account,
            amount_eur=money_str(euros),
            amount_usd=fx.amount_usd,
            fx_rate=fx.rate,
            sender_bic=message['sender_bic'],
            receiver_bic=message['receiver_bic'],
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
            raise InSepaError('invalid_pacs', 'Operator file has no pacs.008 documents.')
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
            except InSepaError as exc:
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
    ) -> InboundSepa:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_UNMATCHED:
            raise InSepaError('not_assignable', 'Only unmatched inbound payments can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise InSepaError('missing_customer_id', 'Customer id is required.')
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
    ) -> InboundSepa:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_HELD:
            raise InSepaError('not_overridable', 'Only OFAC-held inbound payments can be overridden.')
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
    ) -> InboundSepa:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status != IN_PENDING:
            raise InSepaError('not_releasable', 'Inbound payment is not waiting for dual-control.')
        if row.actor and row.actor == str(actor):
            raise InSepaError('same_approver', 'A different employee must release this inbound payment.')
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
    ) -> InboundSepa:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if row.status not in OPEN_INBOUNDS:
            raise InSepaError('not_rejectable', 'Inbound payment cannot be rejected.')
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
    ) -> InboundSepa:
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
    ) -> InboundSepa:
        self._require_customer_return(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise InSepaError('insepa_forbidden', 'Not allowed to return this inbound payment.')
        return self._return(row, actor=actor, reason=reason or 'CUST', note=note, force_window=False)

    def _assign_return_id(self, row: InboundSepa) -> None:
        seq = self.store.next_sequence()
        row.return_msg_id = compose_msg_id(self.policy.source_id + 'R', seq)

    def _customer_window_open(self, row: InboundSepa, now: float) -> bool:
        posted_at = float(row.posted_at or row.created_at)
        if row.scheme == SCHEME_INSTANT:
            return (now - posted_at) <= self.policy.customer_return_seconds
        posted_day = self.calendar.local_dt(posted_at).date()
        now_day = self.calendar.local_dt(now).date()
        return posted_day == now_day and self.calendar.is_business_day(now_day)

    def _return(
        self,
        row: InboundSepa,
        *,
        actor: str,
        reason: Any,
        note: Any,
        force_window: bool,
    ) -> InboundSepa:
        if row.status in {IN_RETURNED, IN_REJECTED}:
            raise InSepaError('already_returned', 'Inbound payment is already returned or rejected.')
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
            raise InSepaError('not_returnable', 'Inbound payment cannot be returned.')
        if not force_window and not self._customer_window_open(row, now):
            raise InSepaError('return_window_closed', 'Exception-return window has closed.')
        classified = self._debit(row)
        if classified == 'nsf':
            raise InSepaError('nsf', 'Insufficient funds to return this inbound payment.', inbound=row)
        if classified != 'ok':
            raise InSepaError('return_failed', 'Inbound return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.return_reason = code
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.returned_at = now
        row.updated_at = now
        self._assign_return_id(row)
        self.store.update(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[InboundSepa]:
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
            raise InSepaError('insepa_forbidden', 'Not allowed to view this inbound book.')
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
            'receiver_bic': format_bic(self.policy.receiver_bic),
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'inst_max': money_str(self.policy.inst_max),
            'sct_max': money_str(self.policy.sct_max),
            'fx_rate': str(self.fx_book.rate),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clock': self.calendar.snapshot(now, scheme=SCHEME_INSTANT),
            'sct_clock': self.calendar.snapshot(now, scheme=SCHEME_SCT),
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
            'receiver_bic': format_bic(self.policy.receiver_bic),
            'unmatched': [row.to_dict() for row in rows[:40]],
            'unmatched_count': len(rows),
        }


_SERVICE: Optional[InSepaService] = None


def set_service(service: Optional[InSepaService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[InSepaService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('INSEPA_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryInSepaStore()
    path = os.environ.get('INSEPA_DB', DEFAULT_STORE_PATH)
    return SqliteInSepaStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    lookup_fn: Optional[Callable[[str], Optional[str]]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    fx_book: Optional[EurUsdBook] = None,
    calendar: Optional[Target2Calendar] = None,
) -> InSepaService:
    if store is None:
        store = default_store()
    return InSepaService(
        InSepaPolicy.from_env(),
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
        'insepa_forbidden': 403,
        'insepa_disabled': 403,
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
        'invalid_bic': 400,
        'invalid_scheme_id': 400,
        'invalid_pacs': 400,
        'invalid_scheme': 400,
        'invalid_currency': 400,
        'invalid_iban': 400,
        'not_sepa_country': 400,
        'invalid_rate': 400,
        'invalid_end_to_end_id': 400,
        'invalid_msg_id': 400,
        'invalid_reason': 400,
        'invalid_purpose': 400,
        'invalid_reference': 400,
        'wrong_receiver': 400,
        'amount_out_of_range': 400,
        'sct_inst_amount_exceeded': 400,
        'sct_amount_exceeded': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: InSepaError) -> Dict[str, Any]:
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
    except InSepaError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: InSepaService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'InSepas': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_unmatched(service: InSepaService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'insepa_forbidden'}), 403
    return jsonify({'InSepas': service.unmatched_snapshot()}), 200


def handle_preview(service: InSepaService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'insepa_forbidden'}), 403
    values = request.get_json(silent=True) or {}

    def _run():
        preview = service.preview_message(values)
        return jsonify({'preview': preview}), 200

    return _handle_errors(_run)


def handle_quote(service: InSepaService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        euros = parse_money(values.get('amount') or values.get('amount_eur'))
        return jsonify({'fx': service.quote(euros).to_dict()}), 200

    return _handle_errors(_run)


def handle_ingest(service: InSepaService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'Inbound SEPA payment ingested' if created else 'Inbound SEPA payment already posted',
            'inbound': row.to_dict(),
            'InSepas': service.snapshot(owner, actor=userid, actor_type=actor_type) if row.userid else service.unmatched_snapshot(),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_file(service: InSepaService):
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


def handle_assign(service: InSepaService):
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
            'message': 'Inbound SEPA payment assigned',
            'inbound': row.to_dict(),
            'InSepas': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_action(service: InSepaService, action: str):
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
            message = 'Inbound SEPA payment released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'MS03', note=values.get('note') or '',
            )
            message = 'Inbound SEPA payment rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'MS03', note=values.get('note') or '',
            )
            message = 'Inbound SEPA payment returned'
        else:
            raise InSepaError('invalid_reason', 'Unknown inbound action.')
        owner = row.userid or userid
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'InSepas': service.snapshot(owner, actor=userid, actor_type=actor_type) if row.userid else service.unmatched_snapshot(),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: InSepaService):
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
            'InSepas': service.snapshot(row.userid or userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: InSepaService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner if actor_type not in EMPLOYEE_ROLES else None)
    return jsonify({'InSepas': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_insepa_routes(app, service: InSepaService) -> None:
    @app.route('/listInSepas', methods=['POST', 'GET'])
    def list_insepas_route():
        return handle_list(service)

    @app.route('/listUnmatchedInSepas', methods=['POST', 'GET'])
    def list_unmatched_insepas_route():
        return handle_unmatched(service)

    @app.route('/previewInSepa', methods=['POST', 'GET'])
    def preview_insepa_route():
        return handle_preview(service)

    @app.route('/quoteInSepaFx', methods=['POST', 'GET'])
    def quote_insepa_fx_route():
        return handle_quote(service)

    @app.route('/ingestInSepa', methods=['POST', 'GET'])
    def ingest_insepa_route():
        return handle_ingest(service)

    @app.route('/ingestInSepaFile', methods=['POST', 'GET'])
    def ingest_insepa_file_route():
        return handle_ingest_file(service)

    @app.route('/assignInSepa', methods=['POST', 'GET'])
    def assign_insepa_route():
        return handle_assign(service)

    @app.route('/overrideInSepaOfac', methods=['POST', 'GET'])
    def override_insepa_ofac_route():
        return _staff_action(service, 'override')

    @app.route('/releaseInSepa', methods=['POST', 'GET'])
    def release_insepa_route():
        return _staff_action(service, 'release')

    @app.route('/rejectInSepa', methods=['POST', 'GET'])
    def reject_insepa_route():
        return _staff_action(service, 'reject')

    @app.route('/returnInSepa', methods=['POST', 'GET'])
    def return_insepa_route():
        return _staff_action(service, 'return')

    @app.route('/requestInSepaReturn', methods=['POST', 'GET'])
    def request_insepa_return_route():
        return handle_request_return(service)

    @app.route('/runDueInSepas', methods=['POST', 'GET'])
    def run_due_insepas_route():
        return handle_run_due(service)
