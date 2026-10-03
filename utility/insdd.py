"""Inbound SEPA Direct Debit (SDD Core / B2B) as debtor from an operator file.

Staff ingest incoming ISO 20022 pain.008 EUR collections and debit the
debtor customer in USD. Independent of outbound SDD origination (PR #85,
customer-as-creditor), inbound SEPA SCT (PR #101), inbound ACH (PR #105),
and domestic Fedwire (PR #73). Existing `/fundTransfer`, `/withdrawAmount`,
and `/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- IBAN ISO 13616 mod-97 restricted to EPC SEPA countries
- BIC ISO 9362 (8→11 pad XXX; location cannot start with 0)
- EPC Creditor Identifier + Unique Mandate Reference
- TARGET2 calendar (16:00 CEST cutoff, Easter / May 1 / Christmas)
- EUR-only + EURUSD quote (USD debit equivalent)
- ISO 20022 pain.008 parse/compose/file split (XXE/DOCTYPE rejected)
- pacs.002 reject / pacs.004 refund field maps
- Mandate registry (customer as debtor) + incoming debit posting
- Core 8-week MD06 / 13-month MD01 refund; B2B no no-questions refund
- OFAC-style creditor screening (reused) + dual-control on USD equivalent

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Creditor and debtor IBANs never appear in to_dict / snapshots.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    EMPLOYEE_ROLES,
    MONEY_QUANTUM,
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
IN_QUEUED = 'queued'
IN_PENDING = 'pending_release'
IN_POSTED = 'posted'
IN_RETURNED = 'returned'
IN_REJECTED = 'rejected'
IN_FAILED = 'failed'
IN_STATUSES = frozenset({
    IN_HELD, IN_UNMATCHED, IN_QUEUED, IN_PENDING, IN_POSTED,
    IN_RETURNED, IN_REJECTED, IN_FAILED,
})
OPEN_INBOUNDS = frozenset({IN_HELD, IN_UNMATCHED, IN_QUEUED, IN_PENDING})
RETURNABLE_BEFORE_POST = frozenset({IN_HELD, IN_UNMATCHED, IN_QUEUED, IN_PENDING})

SCHEME_CORE = 'core'
SCHEME_B2B = 'b2b'
SCHEMES = frozenset({SCHEME_CORE, SCHEME_B2B})
SCHEME_ALIASES = {
    'core': SCHEME_CORE, 'sdd': SCHEME_CORE, 'sepa': SCHEME_CORE,
    'coreb2c': SCHEME_CORE, 'consumer': SCHEME_CORE,
    'b2b': SCHEME_B2B, 'business': SCHEME_B2B, 'b2b-sdd': SCHEME_B2B,
}
SEQUENCES = frozenset({'FRST', 'RCUR', 'OOFF', 'FNAL'})
SEQ_ALIASES = {
    'frst': 'FRST', 'first': 'FRST',
    'rcur': 'RCUR', 'recurring': 'RCUR', 'recurrent': 'RCUR',
    'ooff': 'OOFF', 'oneoff': 'OOFF', 'one-off': 'OOFF', 'once': 'OOFF',
    'fnal': 'FNAL', 'final': 'FNAL', 'last': 'FNAL',
}
MANDATE_ACTIVE = 'active'
MANDATE_PAUSED = 'paused'
MANDATE_CANCELLED = 'cancelled'
MANDATE_EXPIRED = 'expired'
MANDATE_DRAFT = 'draft'
MANDATE_STATUSES = frozenset({
    MANDATE_ACTIVE, MANDATE_PAUSED, MANDATE_CANCELLED, MANDATE_EXPIRED, MANDATE_DRAFT,
})

RETURN_REASONS = frozenset({
    'MD01', 'MD06', 'MD07', 'AM04', 'AC01', 'AC04', 'AC06', 'RR01', 'MS03', 'FF01',
})
RETURN_ALIASES = {
    'md01': 'MD01', 'unauth': 'MD01', 'unauthorized': 'MD01', 'no_mandate': 'MD01',
    'md06': 'MD06', 'refund': 'MD06', 'cust': 'MD06', 'customer': 'MD06',
    'md07': 'MD07', 'end': 'MD07',
    'am04': 'AM04', 'nsf': 'AM04', 'insufficient': 'AM04',
    'ac01': 'AC01', 'acct': 'AC01', 'account': 'AC01', 'unknown': 'AC01',
    'ac04': 'AC04', 'closed': 'AC04',
    'ac06': 'AC06', 'blocked': 'AC06', 'freeze': 'AC06',
    'rr01': 'RR01', 'ofac': 'RR01',
    'ms03': 'MS03', 'other': 'MS03', 'refused': 'MS03',
    'ff01': 'FF01', 'invalid': 'FF01',
}

SEPA_COUNTRIES = frozenset({
    'AD', 'AT', 'BE', 'BG', 'CH', 'CY', 'CZ', 'DE', 'DK', 'EE', 'ES', 'FI',
    'FR', 'GI', 'GR', 'HR', 'HU', 'IE', 'IS', 'IT', 'LI', 'LT', 'LU', 'LV',
    'MC', 'MT', 'NL', 'NO', 'PL', 'PT', 'RO', 'SE', 'SI', 'SK', 'SM', 'VA',
})
DEFAULT_STORE_PATH = 'SystemLogs/insdd.sqlite'
DEFAULT_RECEIVER_BIC = 'KNHADEFFXXX'
DEFAULT_EURUSD = Decimal('1.080000')
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)
PAIN_NS = 'urn:iso:std:iso:20022:tech:xsd:pain.008.001.08'


class InSddError(ValueError):
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


def _alnum(value: Any) -> str:
    return ''.join(ch for ch in str(value or '') if ch.isalnum()).upper()


def iban_mod97(iban: str) -> int:
    rearr = iban[4:] + iban[:4]
    digits = []
    for ch in rearr:
        if ch.isdigit():
            digits.append(ch)
        elif 'A' <= ch <= 'Z':
            digits.append(str(ord(ch) - 55))
        else:
            raise InSddError('invalid_iban', 'IBAN contains an invalid character.')
    remainder = 0
    for chunk_start in range(0, len(''.join(digits)), 9):
        piece = str(remainder) + ''.join(digits)[chunk_start:chunk_start + 9]
        remainder = int(piece) % 97
    return remainder


def iban_check_digit_ok(iban: str) -> bool:
    try:
        return iban_mod97(_alnum(iban)) == 1
    except (InSddError, ValueError):
        return False


def compose_iban(country: str, bban: str) -> str:
    cc = str(country or '').strip().upper()
    body = _alnum(bban)
    if len(cc) != 2 or not cc.isalpha():
        raise InSddError('invalid_iban', 'IBAN country must be two letters.')
    if cc not in SEPA_COUNTRIES:
        raise InSddError('not_sepa_country', 'IBAN country is outside the SEPA zone.')
    if not body:
        raise InSddError('invalid_iban', 'BBAN is required.')
    check = 98 - iban_mod97('%s00%s' % (cc, body))
    return '%s%02d%s' % (cc, check, body)


def normalize_iban(value: Any) -> str:
    compact = _alnum(value)
    if len(compact) < 15 or len(compact) > 34:
        raise InSddError('invalid_iban', 'IBAN length is invalid.')
    if not compact[:2].isalpha() or not compact[2:4].isdigit():
        raise InSddError('invalid_iban', 'IBAN must start with a country code and check digits.')
    if compact[:2] not in SEPA_COUNTRIES:
        raise InSddError('not_sepa_country', 'IBAN country is outside the SEPA zone.')
    if iban_mod97(compact) != 1:
        raise InSddError('invalid_iban', 'IBAN check digits failed mod-97.')
    return compact


def mask_iban(value: Any) -> str:
    try:
        iban = normalize_iban(value)
    except InSddError:
        text = _alnum(value)
        return '%s****%s' % (text[:2] or 'XX', last4(text))
    return '%s****%s' % (iban[:2], last4(extract_iban_account(iban)))


def extract_iban_account(value: Any) -> str:
    iban = normalize_iban(value)
    bban = iban[4:]
    if iban[:2] == 'DE' and len(bban) >= 10:
        account = bban[-10:].lstrip('0') or '0'
        return account
    digits = ''.join(ch for ch in bban if ch.isdigit())
    return digits.lstrip('0') or '0'


def normalize_bic(value: Any) -> str:
    compact = _alnum(value)
    if len(compact) == 8:
        compact += 'XXX'
    if len(compact) != 11 or not compact[:4].isalpha() or not compact[4:6].isalpha():
        raise InSddError('invalid_bic', 'BIC must be 8 or 11 alphanumeric characters.')
    if compact[6] == '0':
        raise InSddError('invalid_bic', 'BIC location code cannot start with 0.')
    return compact


def format_bic(value: Any) -> str:
    return normalize_bic(value)


def bic8(value: Any) -> str:
    return normalize_bic(value)[:8]


def compose_creditor_identifier(country: str, national_id: str, business_code: str = 'ZZZ') -> str:
    cc = str(country or '').strip().upper()
    national = _alnum(national_id)
    code = _alnum(business_code)[:3] or 'ZZZ'
    if len(cc) != 2 or not cc.isalpha():
        raise InSddError('invalid_creditor_id', 'Creditor Identifier country is invalid.')
    if len(national) < 4:
        raise InSddError('invalid_creditor_id', 'National creditor id is too short.')
    check = 98 - iban_mod97('%s00%s' % (cc, national))
    return '%s%02d%s%s' % (cc, check, code, national)


def normalize_creditor_identifier(value: Any) -> str:
    compact = _alnum(value)
    if len(compact) < 8 or len(compact) > 35:
        raise InSddError('invalid_creditor_id', 'EPC Creditor Identifier length is invalid.')
    if not compact[:2].isalpha() or not compact[2:4].isdigit():
        raise InSddError('invalid_creditor_id', 'Creditor Identifier must start with country + check.')
    national = compact[7:]
    if not national:
        raise InSddError('invalid_creditor_id', 'Creditor Identifier is missing the national id.')
    expected = compose_creditor_identifier(compact[:2], national, compact[4:7])
    if expected[:4] != compact[:4]:
        raise InSddError('invalid_creditor_id', 'Creditor Identifier check digits failed.')
    return compact


def normalize_umr(value: Any) -> str:
    text = ''.join(ch for ch in str(value or '').strip() if ch.isalnum() or ch in {'-', '.', '/', '+'})
    if len(text) < 1 or len(text) > 35:
        raise InSddError('invalid_mandate', 'Unique Mandate Reference must be 1-35 characters.')
    return text


def normalize_scheme(value: Any, *, default: str = SCHEME_CORE) -> str:
    raw = str(value or default).strip().lower().replace(' ', '').replace('_', '')
    scheme = SCHEME_ALIASES.get(raw, raw)
    if scheme not in SCHEMES:
        raise InSddError('invalid_scheme', 'Scheme must be CORE or B2B.')
    return scheme


def normalize_sequence(value: Any, *, default: str = 'RCUR') -> str:
    raw = str(value or default).strip().lower().replace(' ', '').replace('_', '')
    seq = SEQ_ALIASES.get(raw, raw.upper())
    if seq not in SEQUENCES:
        raise InSddError('invalid_sequence', 'Sequence must be FRST, RCUR, OOFF, or FNAL.')
    return seq


def normalize_return_reason(value: Any, *, default: str = 'MS03') -> str:
    raw = str(value or default).strip().lower()
    code = RETURN_ALIASES.get(raw, raw.upper())
    if code not in RETURN_REASONS:
        raise InSddError('invalid_reason', 'Unknown SEPA Direct Debit return reason.')
    return code


def normalize_currency(value: Any) -> str:
    text = str(value or 'EUR').strip().upper()
    if text != 'EUR':
        raise InSddError('invalid_currency', 'Inbound SDD is EUR only.')
    return text


def compose_end_to_end_id(prefix: str = 'E2E') -> str:
    return '%s%s' % (prefix, uuid.uuid4().hex[:16].upper())


def compose_message_id() -> str:
    return 'MSG%s' % uuid.uuid4().hex[:16].upper()


def compose_pmtinf_id() -> str:
    return 'PMT%s' % uuid.uuid4().hex[:12].upper()


def compose_scheme_id(value: Any = None) -> str:
    if value:
        text = ''.join(ch for ch in str(value).strip() if ch.isalnum() or ch in {'-', '.'})
        if 6 <= len(text) <= 35:
            return text
        raise InSddError('invalid_scheme_id', 'EndToEndId must be 6-35 characters.')
    return compose_end_to_end_id()


def normalize_scheme_id(value: Any) -> str:
    text = ''.join(ch for ch in str(value or '').strip() if ch.isalnum() or ch in {'-', '.'})
    if len(text) < 6 or len(text) > 35:
        raise InSddError('invalid_scheme_id', 'EndToEndId must be 6-35 characters.')
    return text


def easter_gregorian(year: int) -> date:
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
    easter = easter_gregorian(year)
    days = {
        date(year, 1, 1),
        easter - timedelta(days=2),
        easter + timedelta(days=1),
        date(year, 5, 1),
        date(year, 12, 25),
        date(year, 12, 26),
    }
    return days


class Target2Calendar:
    """TARGET2 business-day + 16:00 CEST cutoff. Injectable offset and holidays."""

    def __init__(
        self,
        *,
        cutoff_hour: int = 16,
        cutoff_minute: int = 0,
        tz_offset_hours: int = 2,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.cutoff_hour = int(cutoff_hour)
        self.cutoff_minute = int(cutoff_minute)
        self.tz_offset_hours = int(tz_offset_hours)
        self.tz = timezone(timedelta(hours=self.tz_offset_hours))
        extra = set()
        for item in extra_holidays:
            text = str(item).strip()
            if text:
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
        return (local.hour, local.minute) >= (self.cutoff_hour, self.cutoff_minute)

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

    def value_date(self, ts: float) -> date:
        local = self.local_dt(ts)
        day = local.date()
        if self.is_business_day(day) and not self.is_after_cutoff(ts):
            return day
        return self.next_business_day(day)

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


class EurUsdBook:
    def __init__(self, rate: Decimal = DEFAULT_EURUSD) -> None:
        self.rate = Decimal(rate).quantize(Decimal('0.000001'), rounding=ROUND_HALF_EVEN)

    def quote(self, euros: Decimal) -> Dict[str, str]:
        usd = (euros * self.rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
        return {
            'currency': 'EUR',
            'amount_eur': money_str(euros),
            'rate': str(self.rate),
            'debit_usd': money_str(usd),
        }


def lead_days_for(scheme: str, sequence: str) -> int:
    if scheme == SCHEME_B2B:
        return 1
    if sequence in {'FRST', 'OOFF'}:
        return 5
    return 2


def _local_tag(tag: str) -> str:
    return tag.split('}', 1)[-1] if '}' in tag else tag


def _walk_find(node: Optional[ET.Element], name: str) -> Optional[ET.Element]:
    if node is None:
        return None
    if _local_tag(node.tag) == name:
        return node
    for child in list(node):
        found = _walk_find(child, name)
        if found is not None:
            return found
    return None


def _text_of(node: Optional[ET.Element], name: str, default: str = '') -> str:
    found = _walk_find(node, name)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def _attr_of(node: Optional[ET.Element], name: str, attr: str, default: str = '') -> str:
    found = _walk_find(node, name)
    if found is None:
        return default
    return str(found.attrib.get(attr) or default).strip()


def parse_xml_safe(text: Any) -> ET.Element:
    raw = str(text or '')
    upper = raw.upper()
    if '<!DOCTYPE' in upper or '<!ENTITY' in upper:
        raise InSddError('invalid_pain', 'XML DTD/entity payloads are rejected.')
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise InSddError('invalid_pain', 'Operator file is not valid pain.008 XML.') from exc


def parse_pain008(text: Any) -> Dict[str, Any]:
    root = parse_xml_safe(text)
    instd = _walk_find(root, 'InstdAmt')
    amount_text = (instd.text or '').strip() if instd is not None else ''
    if not amount_text:
        amount_text = _text_of(root, 'CtrlSum')
    currency = (instd.attrib.get('Ccy') if instd is not None else '') or _text_of(root, 'Ccy', 'EUR')
    debtor_iban = _text_of(_walk_find(root, 'DbtrAcct'), 'IBAN') or _text_of(root, 'IBAN')
    # Prefer the debtor IBAN: last IBAN under DrctDbtTxInf if both exist.
    tx = _walk_find(root, 'DrctDbtTxInf')
    if tx is not None:
        debtor_iban = _text_of(_walk_find(tx, 'DbtrAcct'), 'IBAN') or debtor_iban
        creditor_name = _text_of(_walk_find(root, 'Cdtr'), 'Nm') or _text_of(root, 'Nm')
        debtor_name = _text_of(_walk_find(tx, 'Dbtr'), 'Nm')
    else:
        creditor_name = _text_of(_walk_find(root, 'Cdtr'), 'Nm')
        debtor_name = _text_of(_walk_find(root, 'Dbtr'), 'Nm')
    creditor_iban = _text_of(_walk_find(root, 'CdtrAcct'), 'IBAN')
    receiver = _text_of(_walk_find(root, 'DbtrAgt'), 'BIC') or _text_of(root, 'BIC')
    scheme_raw = _text_of(_walk_find(root, 'LclInstrm'), 'Cd') or _text_of(root, 'LclInstrm') or 'CORE'
    sequence = _text_of(root, 'SeqTp') or 'RCUR'
    collection = _text_of(root, 'ReqdColltnDt')
    umr = _text_of(root, 'MndtId')
    creditor_id = _text_of(_walk_find(root, 'CdtrSchmeId'), 'Id') or _text_of(root, 'Id')
    # CdtrSchmeId often nests Id/PrvtId/Othr/Id — last non-IBAN Id near scheme.
    scheme_ids = [el for el in root.iter() if _local_tag(el.tag) == 'Id' and el.text]
    for el in scheme_ids:
        candidate = (el.text or '').strip()
        if candidate[:2].isalpha() and 'ZZZ' in candidate.upper():
            creditor_id = candidate
            break
    e2e = _text_of(root, 'EndToEndId') or _text_of(root, 'TxId')
    return {
        'scheme_id': normalize_scheme_id(e2e or compose_end_to_end_id()),
        'amount': money_str(parse_money(amount_text)),
        'currency': normalize_currency(currency or 'EUR'),
        'creditor_name': creditor_name or 'CREDITOR',
        'creditor_iban': creditor_iban,
        'creditor_id': creditor_id,
        'debtor_name': debtor_name or 'DEBTOR',
        'debtor_iban': debtor_iban,
        'receiver_bic': receiver,
        'scheme': normalize_scheme(scheme_raw),
        'sequence': normalize_sequence(sequence),
        'umr': umr,
        'collection_date': collection,
        'memo': _text_of(root, 'Ustrd'),
        'message_id': _text_of(root, 'MsgId'),
        'pmtinf_id': _text_of(root, 'PmtInfId'),
        'raw': str(text or ''),
    }


def compose_pain008(fields: Dict[str, Any]) -> str:
    amount = money_str(parse_money(fields.get('amount') or '0.01'))
    e2e = normalize_scheme_id(fields.get('scheme_id') or fields.get('end_to_end_id') or compose_end_to_end_id())
    scheme = normalize_scheme(fields.get('scheme') or SCHEME_CORE)
    sequence = normalize_sequence(fields.get('sequence') or 'FRST')
    receiver = normalize_bic(fields.get('receiver_bic') or DEFAULT_RECEIVER_BIC)
    debtor_iban = normalize_iban(fields.get('debtor_iban') or fields.get('iban'))
    creditor_iban = ''
    if fields.get('creditor_iban'):
        creditor_iban = normalize_iban(fields.get('creditor_iban'))
    creditor_id = normalize_creditor_identifier(fields.get('creditor_id'))
    umr = normalize_umr(fields.get('umr') or fields.get('mandate_id') or 'UMR-1')
    collection = str(fields.get('collection_date') or fields.get('value_date') or date.today().isoformat())
    if len(collection) == 8 and collection.isdigit():
        collection = '%s-%s-%s' % (collection[:4], collection[4:6], collection[6:8])
    creditor_name = str(fields.get('creditor_name') or 'CREDITOR').strip() or 'CREDITOR'
    debtor_name = str(fields.get('debtor_name') or 'DEBTOR').strip() or 'DEBTOR'
    memo = normalize_note(fields.get('memo') or '', limit=140)
    msg_id = str(fields.get('message_id') or compose_message_id())
    pmt_id = str(fields.get('pmtinf_id') or compose_pmtinf_id())
    creditor_iban_xml = (
        '<CdtrAcct><Id><IBAN>%s</IBAN></Id></CdtrAcct>' % creditor_iban if creditor_iban else ''
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Document xmlns="%s">'
        '<CstmrDrctDbtInitn>'
        '<GrpHdr><MsgId>%s</MsgId><NbOfTxs>1</NbOfTxs><CtrlSum>%s</CtrlSum>'
        '<InitgPty><Nm>%s</Nm></InitgPty></GrpHdr>'
        '<PmtInf><PmtInfId>%s</PmtInfId><PmtMtd>DD</PmtMtd>'
        '<PmtTpInf><SvcLvl><Cd>SEPA</Cd></SvcLvl><LclInstrm><Cd>%s</Cd></LclInstrm>'
        '<SeqTp>%s</SeqTp></PmtTpInf>'
        '<ReqdColltnDt>%s</ReqdColltnDt>'
        '<Cdtr><Nm>%s</Nm></Cdtr>%s'
        '<CdtrSchmeId><Id><PrvtId><Othr><Id>%s</Id></Othr></PrvtId></Id></CdtrSchmeId>'
        '<DrctDbtTxInf>'
        '<PmtId><EndToEndId>%s</EndToEndId></PmtId>'
        '<InstdAmt Ccy="EUR">%s</InstdAmt>'
        '<DrctDbtTx><MndtRltdInf><MndtId>%s</MndtId><DtOfSgntr>2023-01-15</DtOfSgntr>'
        '</MndtRltdInf></DrctDbtTx>'
        '<Dbtr><Nm>%s</Nm></Dbtr>'
        '<DbtrAcct><Id><IBAN>%s</IBAN></Id></DbtrAcct>'
        '<DbtrAgt><FinInstnId><BIC>%s</BIC></FinInstnId></DbtrAgt>'
        '<RmtInf><Ustrd>%s</Ustrd></RmtInf>'
        '</DrctDbtTxInf></PmtInf></CstmrDrctDbtInitn></Document>'
    ) % (
        PAIN_NS, msg_id, amount, creditor_name, pmt_id, scheme.upper(), sequence,
        collection, creditor_name, creditor_iban_xml, creditor_id, e2e, amount,
        umr, debtor_name, debtor_iban, receiver, memo,
    )


def split_pain_file(text: Any) -> List[Dict[str, Any]]:
    raw = str(text or '')
    upper = raw.upper()
    if '<!DOCTYPE' in upper or '<!ENTITY' in upper:
        raise InSddError('invalid_pain', 'XML DTD/entity payloads are rejected.')
    if not raw.strip():
        raise InSddError('invalid_pain', 'Operator file is empty.')
    chunks = []
    lower = raw
    start = 0
    token = '<Document'
    while True:
        idx = lower.find(token, start)
        if idx < 0:
            break
        end = lower.find('</Document>', idx)
        if end < 0:
            raise InSddError('invalid_pain', 'Operator file is not valid pain.008 XML.')
        chunks.append(raw[idx:end + len('</Document>')])
        start = end + len('</Document>')
    if not chunks:
        chunks = [raw]
    messages = []
    for chunk in chunks:
        parsed = parse_pain008(chunk)
        messages.append(parsed)
    if not messages:
        raise InSddError('invalid_pain', 'Operator file has no SDD collections.')
    return messages


def message_from_pain(text: Any) -> Dict[str, Any]:
    return split_pain_file(text)[0]


def message_from_values(values: Dict[str, Any]) -> Dict[str, Any]:
    if values.get('file') or values.get('pain') or values.get('raw') or values.get('xml'):
        return message_from_pain(values.get('file') or values.get('pain') or values.get('raw') or values.get('xml'))
    amount = parse_money(values.get('amount'))
    e2e = normalize_scheme_id(values.get('scheme_id') or values.get('end_to_end_id') or compose_end_to_end_id())
    debtor_iban = normalize_iban(values.get('debtor_iban') or values.get('iban') or values.get('beneficiary_iban'))
    receiver = normalize_bic(values.get('receiver_bic') or values.get('receiver') or DEFAULT_RECEIVER_BIC)
    creditor_id = normalize_creditor_identifier(values.get('creditor_id') or values.get('ci'))
    umr = normalize_umr(values.get('umr') or values.get('mandate') or values.get('mandate_id'))
    scheme = normalize_scheme(values.get('scheme') or SCHEME_CORE)
    sequence = normalize_sequence(values.get('sequence') or 'FRST')
    currency = normalize_currency(values.get('currency') or 'EUR')
    collection = str(values.get('collection_date') or values.get('value_date') or '').strip()
    creditor_iban = ''
    if values.get('creditor_iban'):
        creditor_iban = normalize_iban(values.get('creditor_iban'))
    return {
        'scheme_id': e2e,
        'amount': money_str(amount),
        'currency': currency,
        'creditor_name': str(values.get('creditor_name') or values.get('originator_name') or 'CREDITOR').strip() or 'CREDITOR',
        'creditor_iban': creditor_iban,
        'creditor_id': creditor_id,
        'debtor_name': str(values.get('debtor_name') or values.get('beneficiary_name') or 'DEBTOR').strip() or 'DEBTOR',
        'debtor_iban': debtor_iban,
        'receiver_bic': receiver,
        'scheme': scheme,
        'sequence': sequence,
        'umr': umr,
        'collection_date': collection,
        'memo': normalize_note(values.get('memo') or values.get('description') or '', limit=140),
        'message_id': str(values.get('message_id') or ''),
        'pmtinf_id': str(values.get('pmtinf_id') or ''),
        'raw': '',
    }


def compose_pacs002(row: Any, *, reason: str, receiver_bic: str) -> str:
    code = normalize_return_reason(reason, default='MS03')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.002.001.10">'
        '<FIToFIPmtStsRpt><GrpHdr><MsgId>%s</MsgId></GrpHdr>'
        '<TxInfAndSts><OrgnlEndToEndId>%s</OrgnlEndToEndId><TxSts>RJCT</TxSts>'
        '<StsRsnInf><Rsn><Cd>%s</Cd></Rsn></StsRsnInf>'
        '<InstgAgt><FinInstnId><BICFI>%s</BICFI></FinInstnId></InstgAgt>'
        '</TxInfAndSts></FIToFIPmtStsRpt></Document>'
    ) % (compose_message_id(), row.scheme_id, code, normalize_bic(receiver_bic))


def compose_pacs004(row: Any, *, reason: str, receiver_bic: str) -> str:
    code = normalize_return_reason(reason, default='MD06')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.004.001.09">'
        '<PmtRtr><GrpHdr><MsgId>%s</MsgId><NbOfTxs>1</NbOfTxs></GrpHdr>'
        '<TxInf><RtrId>%s</RtrId><OrgnlEndToEndId>%s</OrgnlEndToEndId>'
        '<RtrdInstdAmt Ccy="EUR">%s</RtrdInstdAmt>'
        '<RtrRsnInf><Rsn><Cd>%s</Cd></Rsn></RtrRsnInf>'
        '<InstgAgt><FinInstnId><BICFI>%s</BICFI></FinInstnId></InstgAgt>'
        '</TxInf></PmtRtr></Document>'
    ) % (
        compose_message_id(), row.return_id or compose_end_to_end_id('RTR'),
        row.scheme_id, row.amount_eur, code, normalize_bic(receiver_bic),
    )


@dataclass
class InSddPolicy:
    enabled: bool = True
    customer_view: bool = True
    customer_return: bool = True
    customer_mandate: bool = True
    allow_credit: bool = False
    max_inbounds: int = 240
    max_mandates: int = 40
    min_amount: Decimal = Decimal('0.01')
    core_max_amount: Decimal = Decimal('15000.00')
    b2b_max_amount: Decimal = Decimal('100000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    eurusd: Decimal = DEFAULT_EURUSD
    cutoff_hour: int = 16
    cutoff_minute: int = 0
    tz_offset_hours: int = 2
    receiver_bic: str = DEFAULT_RECEIVER_BIC
    core_refund_days: int = 56
    core_unauth_days: int = 396
    watchlist: Tuple[str, ...] = (
        'BLOCKED PERSON',
        'SANCTIONED ENTITY',
        'OFAC TESTNAME',
    )
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'InSddPolicy':
        extra = _env_list('INSDD_OFAC_LIST')
        watch = tuple(dict.fromkeys(cls.watchlist + extra))
        receiver = os.environ.get('INSDD_RECEIVER_BIC') or DEFAULT_RECEIVER_BIC
        try:
            receiver = normalize_bic(receiver)
        except InSddError:
            receiver = DEFAULT_RECEIVER_BIC
        rate_raw = os.environ.get('INSDD_EURUSD') or str(DEFAULT_EURUSD)
        try:
            rate = Decimal(str(rate_raw))
        except Exception:
            rate = DEFAULT_EURUSD
        return cls(
            enabled=_env_bool('INSDD_ENABLED', True),
            customer_view=_env_bool('INSDD_CUSTOMER_VIEW', True),
            customer_return=_env_bool('INSDD_CUSTOMER_RETURN', True),
            customer_mandate=_env_bool('INSDD_CUSTOMER_MANDATE', True),
            allow_credit=_env_bool('INSDD_ALLOW_CREDIT', False),
            max_inbounds=max(1, _env_int('INSDD_MAX', 240)),
            max_mandates=max(1, _env_int('INSDD_MAX_MANDATES', 40)),
            min_amount=_env_money('INSDD_MIN_AMOUNT', '0.01'),
            core_max_amount=_env_money('INSDD_CORE_MAX', '15000.00'),
            b2b_max_amount=_env_money('INSDD_B2B_MAX', '100000.00'),
            dual_control_threshold=_env_money('INSDD_DUAL_CONTROL', '10000.00'),
            eurusd=rate,
            cutoff_hour=max(0, min(23, _env_int('INSDD_CUTOFF_HOUR', 16))),
            cutoff_minute=max(0, min(59, _env_int('INSDD_CUTOFF_MINUTE', 0))),
            tz_offset_hours=_env_int('INSDD_TZ_OFFSET', 2),
            receiver_bic=receiver,
            core_refund_days=max(1, _env_int('INSDD_REFUND_DAYS', 56)),
            core_unauth_days=max(1, _env_int('INSDD_UNAUTH_DAYS', 396)),
            watchlist=watch,
            extra_holidays=_env_list('INSDD_HOLIDAYS'),
        )


@dataclass
class InSddMandate:
    mandate_id: str
    userid: str
    umr: str
    creditor_id: str
    creditor_name: str
    scheme: str
    last_sequence: str
    internal_account: str
    status: str
    signature_date: str
    actor: str
    created_at: float
    updated_at: float
    note: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'mandate_id': self.mandate_id,
            'userid': self.userid,
            'umr': self.umr,
            'creditor_id': self.creditor_id,
            'creditor_name': self.creditor_name,
            'scheme': self.scheme,
            'last_sequence': self.last_sequence,
            'internal_account': self.internal_account,
            'account_last4': last4(self.internal_account),
            'status': self.status,
            'signature_date': self.signature_date,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'note': self.note,
            'active': self.status == MANDATE_ACTIVE,
        }


@dataclass
class InboundSdd:
    inbound_id: str
    scheme_id: str
    userid: str
    internal_account: str
    amount_eur: str
    debit_usd: str
    fx_rate: str
    creditor_name: str
    creditor_id: str
    creditor_iban: str
    debtor_name: str
    debtor_iban: str
    receiver_bic: str
    scheme: str
    sequence: str
    umr: str
    mandate_id: str
    purpose: str
    memo: str
    status: str
    value_date: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    return_id: str
    return_reason: str
    created_at: float
    updated_at: float
    posted_at: float = 0.0
    returned_at: float = 0.0
    note: str = ''
    batch_id: str = ''
    currency: str = 'EUR'

    def to_dict(self) -> Dict[str, Any]:
        return {
            'inbound_id': self.inbound_id,
            'scheme_id': self.scheme_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'amount': self.amount_eur,
            'amount_eur': self.amount_eur,
            'debit_usd': self.debit_usd,
            'fx_rate': self.fx_rate,
            'currency': self.currency,
            'creditor_name': self.creditor_name,
            'creditor_id': self.creditor_id,
            'iban_masked': mask_iban(self.debtor_iban) if self.debtor_iban else '',
            'debtor_name': self.debtor_name,
            'debtor_last4': last4(extract_iban_account(self.debtor_iban) if self.debtor_iban else self.internal_account),
            'receiver_bic': self.receiver_bic,
            'scheme': self.scheme,
            'sequence': self.sequence,
            'umr': self.umr,
            'mandate_id': self.mandate_id,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'value_date': self.value_date,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'return_id': self.return_id,
            'return_reason': self.return_reason,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'posted_at': self.posted_at,
            'returned_at': self.returned_at,
            'note': self.note,
            'batch_id': self.batch_id,
            'held': self.status == IN_HELD,
            'unmatched': self.status == IN_UNMATCHED,
            'queued': self.status == IN_QUEUED,
            'pending_release': self.status == IN_PENDING,
            'posted': self.status == IN_POSTED,
            'returned': self.status == IN_RETURNED,
            'returnable': self.status in RETURNABLE_BEFORE_POST or self.status == IN_POSTED,
        }


def _clone_mandate(row: InSddMandate) -> InSddMandate:
    return InSddMandate(**{item.name: getattr(row, item.name) for item in fields(row)})


def _clone_inbound(row: InboundSdd) -> InboundSdd:
    return InboundSdd(**{item.name: getattr(row, item.name) for item in fields(row)})


def _mandate_from_row(row: Any) -> InSddMandate:
    return InSddMandate(
        mandate_id=row['mandate_id'],
        userid=row['userid'],
        umr=row['umr'],
        creditor_id=row['creditor_id'],
        creditor_name=row['creditor_name'],
        scheme=row['scheme'],
        last_sequence=row['last_sequence'] or '',
        internal_account=row['internal_account'],
        status=row['status'],
        signature_date=row['signature_date'] or '',
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        note=row['note'] or '',
    )


def _inbound_from_row(row: Any) -> InboundSdd:
    return InboundSdd(
        inbound_id=row['inbound_id'],
        scheme_id=row['scheme_id'],
        userid=row['userid'] or '',
        internal_account=row['internal_account'] or '',
        amount_eur=row['amount_eur'],
        debit_usd=row['debit_usd'],
        fx_rate=row['fx_rate'],
        creditor_name=row['creditor_name'],
        creditor_id=row['creditor_id'],
        creditor_iban=row['creditor_iban'] or '',
        debtor_name=row['debtor_name'],
        debtor_iban=row['debtor_iban'],
        receiver_bic=row['receiver_bic'],
        scheme=row['scheme'],
        sequence=row['sequence'],
        umr=row['umr'],
        mandate_id=row['mandate_id'] or '',
        purpose=row['purpose'] or 'other',
        memo=row['memo'] or '',
        status=row['status'],
        value_date=row['value_date'],
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        return_id=row['return_id'] or '',
        return_reason=row['return_reason'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        posted_at=float(row['posted_at'] or 0),
        returned_at=float(row['returned_at'] or 0),
        note=row['note'] or '',
        batch_id=row['batch_id'] or '',
        currency=row['currency'] or 'EUR',
    )


_MANDATE_FIELDS = (
    'mandate_id', 'userid', 'umr', 'creditor_id', 'creditor_name', 'scheme',
    'last_sequence', 'internal_account', 'status', 'signature_date', 'actor',
    'created_at', 'updated_at', 'note',
)
_INBOUND_FIELDS = (
    'inbound_id', 'scheme_id', 'userid', 'internal_account', 'amount_eur',
    'debit_usd', 'fx_rate', 'creditor_name', 'creditor_id', 'creditor_iban',
    'debtor_name', 'debtor_iban', 'receiver_bic', 'scheme', 'sequence', 'umr',
    'mandate_id', 'purpose', 'memo', 'status', 'value_date', 'actor', 'releaser',
    'ofac_hit', 'ofac_match', 'return_id', 'return_reason', 'created_at',
    'updated_at', 'posted_at', 'returned_at', 'note', 'batch_id', 'currency',
)


class MemoryInSddStore:
    def __init__(self) -> None:
        self._rows: Dict[str, InboundSdd] = {}
        self._by_e2e: Dict[str, str] = {}
        self._mandates: Dict[str, InSddMandate] = {}
        self._seq = 0
        self._lock = threading.Lock()

    def put(self, row: InboundSdd) -> None:
        with self._lock:
            self._rows[row.inbound_id] = _clone_inbound(row)
            self._by_e2e[row.scheme_id] = row.inbound_id

    def update(self, row: InboundSdd) -> None:
        with self._lock:
            if row.inbound_id not in self._rows:
                raise InSddError('inbound_not_found', 'Inbound SDD not found.')
            self._rows[row.inbound_id] = _clone_inbound(row)
            self._by_e2e[row.scheme_id] = row.inbound_id

    def get(self, inbound_id: str) -> Optional[InboundSdd]:
        with self._lock:
            row = self._rows.get(inbound_id)
            return _clone_inbound(row) if row is not None else None

    def get_by_scheme_id(self, scheme_id: str) -> Optional[InboundSdd]:
        with self._lock:
            inbound_id = self._by_e2e.get(scheme_id)
            row = self._rows.get(inbound_id) if inbound_id else None
            return _clone_inbound(row) if row is not None else None

    def list_for(self, userid: str) -> List[InboundSdd]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.userid == userid]
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone_inbound(row) for row in rows]

    def list_unmatched(self) -> List[InboundSdd]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.status == IN_UNMATCHED]
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone_inbound(row) for row in rows]

    def list_open(self) -> List[InboundSdd]:
        with self._lock:
            rows = [row for row in self._rows.values() if row.status in OPEN_INBOUNDS]
            rows.sort(key=lambda item: item.created_at)
            return [_clone_inbound(row) for row in rows]

    def list_all(self) -> List[InboundSdd]:
        with self._lock:
            rows = list(self._rows.values())
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone_inbound(row) for row in rows]

    def put_mandate(self, row: InSddMandate) -> None:
        with self._lock:
            self._mandates[row.mandate_id] = _clone_mandate(row)

    def update_mandate(self, row: InSddMandate) -> None:
        with self._lock:
            if row.mandate_id not in self._mandates:
                raise InSddError('mandate_not_found', 'Mandate not found.')
            self._mandates[row.mandate_id] = _clone_mandate(row)

    def get_mandate(self, mandate_id: str) -> Optional[InSddMandate]:
        with self._lock:
            row = self._mandates.get(mandate_id)
            return _clone_mandate(row) if row is not None else None

    def get_mandate_by_umr(self, umr: str, creditor_id: str) -> Optional[InSddMandate]:
        with self._lock:
            for row in self._mandates.values():
                if row.umr == umr and row.creditor_id == creditor_id:
                    return _clone_mandate(row)
            return None

    def list_mandates(self, userid: str) -> List[InSddMandate]:
        with self._lock:
            rows = [row for row in self._mandates.values() if row.userid == userid]
            rows.sort(key=lambda item: item.created_at, reverse=True)
            return [_clone_mandate(row) for row in rows]

    def next_sequence(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq


class SqliteInSddStore:
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
                CREATE TABLE IF NOT EXISTS mandates (
                    mandate_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    umr TEXT NOT NULL,
                    creditor_id TEXT NOT NULL,
                    creditor_name TEXT NOT NULL,
                    scheme TEXT NOT NULL,
                    last_sequence TEXT,
                    internal_account TEXT NOT NULL,
                    status TEXT NOT NULL,
                    signature_date TEXT,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    note TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inbounds (
                    inbound_id TEXT PRIMARY KEY,
                    scheme_id TEXT NOT NULL UNIQUE,
                    userid TEXT,
                    internal_account TEXT,
                    amount_eur TEXT NOT NULL,
                    debit_usd TEXT NOT NULL,
                    fx_rate TEXT NOT NULL,
                    creditor_name TEXT NOT NULL,
                    creditor_id TEXT NOT NULL,
                    creditor_iban TEXT,
                    debtor_name TEXT NOT NULL,
                    debtor_iban TEXT NOT NULL,
                    receiver_bic TEXT NOT NULL,
                    scheme TEXT NOT NULL,
                    sequence TEXT NOT NULL,
                    umr TEXT NOT NULL,
                    mandate_id TEXT,
                    purpose TEXT,
                    memo TEXT,
                    status TEXT NOT NULL,
                    value_date TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    releaser TEXT,
                    ofac_hit INTEGER,
                    ofac_match TEXT,
                    return_id TEXT,
                    return_reason TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    posted_at REAL,
                    returned_at REAL,
                    note TEXT,
                    batch_id TEXT,
                    currency TEXT
                )
                """
            )
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.commit()

    def _upsert(self, conn: sqlite3.Connection, table: str, columns: Sequence[str], row: Any) -> None:
        placeholders = ','.join('?' for _ in columns)
        assignments = ','.join('%s=excluded.%s' % (col, col) for col in columns if col != columns[0])
        conn.execute(
            'INSERT INTO %s (%s) VALUES (%s) ON CONFLICT(%s) DO UPDATE SET %s' % (
                table, ','.join(columns), placeholders, columns[0], assignments,
            ),
            [getattr(row, col) for col in columns],
        )

    def put(self, row: InboundSdd) -> None:
        with self._lock, self._connect() as conn:
            self._upsert(conn, 'inbounds', _INBOUND_FIELDS, row)
            conn.commit()

    def update(self, row: InboundSdd) -> None:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT inbound_id FROM inbounds WHERE inbound_id = ?', (row.inbound_id,),
            ).fetchone()
            if existing is None:
                raise InSddError('inbound_not_found', 'Inbound SDD not found.')
            self._upsert(conn, 'inbounds', _INBOUND_FIELDS, row)
            conn.commit()

    def get(self, inbound_id: str) -> Optional[InboundSdd]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,),
            ).fetchone()
        return _inbound_from_row(row) if row is not None else None

    def get_by_scheme_id(self, scheme_id: str) -> Optional[InboundSdd]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM inbounds WHERE scheme_id = ?', (scheme_id,),
            ).fetchone()
        return _inbound_from_row(row) if row is not None else None

    def list_for(self, userid: str) -> List[InboundSdd]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE userid = ? ORDER BY created_at DESC', (userid,),
            ).fetchall()
        return [_inbound_from_row(row) for row in rows]

    def list_unmatched(self) -> List[InboundSdd]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM inbounds WHERE status = ? ORDER BY created_at DESC",
                (IN_UNMATCHED,),
            ).fetchall()
        return [_inbound_from_row(row) for row in rows]

    def list_open(self) -> List[InboundSdd]:
        placeholders = ','.join('?' for _ in OPEN_INBOUNDS)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM inbounds WHERE status IN (%s) ORDER BY created_at' % placeholders,
                tuple(OPEN_INBOUNDS),
            ).fetchall()
        return [_inbound_from_row(row) for row in rows]

    def list_all(self) -> List[InboundSdd]:
        with self._lock, self._connect() as conn:
            rows = conn.execute('SELECT * FROM inbounds ORDER BY created_at DESC').fetchall()
        return [_inbound_from_row(row) for row in rows]

    def put_mandate(self, row: InSddMandate) -> None:
        with self._lock, self._connect() as conn:
            self._upsert(conn, 'mandates', _MANDATE_FIELDS, row)
            conn.commit()

    def update_mandate(self, row: InSddMandate) -> None:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT mandate_id FROM mandates WHERE mandate_id = ?', (row.mandate_id,),
            ).fetchone()
            if existing is None:
                raise InSddError('mandate_not_found', 'Mandate not found.')
            self._upsert(conn, 'mandates', _MANDATE_FIELDS, row)
            conn.commit()

    def get_mandate(self, mandate_id: str) -> Optional[InSddMandate]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM mandates WHERE mandate_id = ?', (mandate_id,),
            ).fetchone()
        return _mandate_from_row(row) if row is not None else None

    def get_mandate_by_umr(self, umr: str, creditor_id: str) -> Optional[InSddMandate]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM mandates WHERE umr = ? AND creditor_id = ?',
                (umr, creditor_id),
            ).fetchone()
        return _mandate_from_row(row) if row is not None else None

    def list_mandates(self, userid: str) -> List[InSddMandate]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM mandates WHERE userid = ? ORDER BY created_at DESC', (userid,),
            ).fetchall()
        return [_mandate_from_row(row) for row in rows]

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


def _sequence_allowed(last: str, incoming: str) -> bool:
    if incoming == 'OOFF':
        return last in {'', 'OOFF'}
    if incoming == 'FRST':
        return last == ''
    if incoming == 'RCUR':
        return last in {'FRST', 'RCUR'}
    if incoming == 'FNAL':
        return last in {'FRST', 'RCUR'}
    return False


class InSddService:
    def __init__(
        self,
        policy: InSddPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        lookup_fn: Optional[Callable[[str], Optional[str]]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[Target2Calendar] = None,
        fx: Optional[EurUsdBook] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.lookup_fn = lookup_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or Target2Calendar(
            cutoff_hour=policy.cutoff_hour,
            cutoff_minute=policy.cutoff_minute,
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )
        self.fx = fx or EurUsdBook(policy.eurusd)

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise InSddError('insdd_disabled', 'Inbound SEPA Direct Debit is disabled.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise InSddError('insdd_forbidden', 'Staff only.')

    def _require_view(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_view:
            raise InSddError('insdd_forbidden', 'Customers cannot view inbound SDD.')

    def _require_customer_return(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_return:
            raise InSddError('insdd_forbidden', 'Customers cannot request inbound SDD returns.')

    def _require_mandate_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_mandate:
            raise InSddError('insdd_forbidden', 'Customers cannot manage SDD mandates.')

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
            raise InSddError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise InSddError('credit_not_allowed', 'Credit accounts cannot be debited by SDD.')

    def _assert_amount(self, euros: Decimal, scheme: str) -> None:
        if euros < self.policy.min_amount:
            raise InSddError('amount_out_of_range', 'Amount is outside the allowed range.')
        if scheme == SCHEME_CORE and euros > self.policy.core_max_amount:
            raise InSddError('core_amount_exceeded', 'CORE Direct Debit exceeds the per-item cap.')
        if scheme == SCHEME_B2B and euros > self.policy.b2b_max_amount:
            raise InSddError('b2b_amount_exceeded', 'B2B Direct Debit exceeds the per-item cap.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, usd: Decimal) -> bool:
        return usd >= self.policy.dual_control_threshold

    def _assert_receiver(self, receiver_bic: str) -> None:
        if normalize_bic(receiver_bic) != normalize_bic(self.policy.receiver_bic):
            raise InSddError('wrong_receiver', 'Collection is not addressed to this bank.')

    def _parse_collection_date(self, raw: str, ts: float) -> date:
        text = str(raw or '').strip()
        if len(text) == 8 and text.isdigit():
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        if len(text) >= 10 and text[4] == '-':
            return date.fromisoformat(text[:10])
        return self.calendar.value_date(ts)

    def _today(self, ts: float) -> date:
        return self.calendar.local_dt(ts).date()

    def quote(self, amount: Any) -> Dict[str, str]:
        euros = parse_money(amount)
        return self.fx.quote(euros)

    def get_inbound(self, *, inbound_id: str, actor: str, actor_type: str) -> InboundSdd:
        self._require_view(actor_type)
        row = self.store.get(inbound_id)
        if row is None:
            raise InSddError('inbound_not_found', 'Inbound SDD not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise InSddError('insdd_forbidden', 'Not allowed to view this inbound SDD.')
        return row

    def preview_message(self, values: Dict[str, Any]) -> Dict[str, Any]:
        self._require_enabled()
        message = message_from_values(values)
        euros = parse_money(message['amount'])
        self._assert_amount(euros, message['scheme'])
        self._assert_receiver(message['receiver_bic'])
        quoted = self.fx.quote(euros)
        ofac = self._screen(message['creditor_name'])
        account = extract_iban_account(message['debtor_iban'])
        userid = self._lookup(account)
        mandate = self.store.get_mandate_by_umr(message['umr'], message['creditor_id'])
        now = float(self.clock())
        value_date = self._parse_collection_date(message.get('collection_date') or '', now)
        return {
            'message': {
                'scheme_id': message['scheme_id'],
                'amount_eur': message['amount'],
                'debit_usd': quoted['debit_usd'],
                'rate': quoted['rate'],
                'receiver_bic': message['receiver_bic'],
                'creditor_name': message['creditor_name'],
                'creditor_id': message['creditor_id'],
                'debtor_name': message['debtor_name'],
                'iban_masked': mask_iban(message['debtor_iban']),
                'scheme': message['scheme'],
                'sequence': message['sequence'],
                'umr': message['umr'],
            },
            'matched_userid': userid or (mandate.userid if mandate else ''),
            'mandate_id': mandate.mandate_id if mandate else '',
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(parse_money(quoted['debit_usd'])),
            'value_date': value_date.isoformat(),
            'lead_days': lead_days_for(message['scheme'], message['sequence']),
            'clock': self.calendar.snapshot(now),
        }

    def _evaluate_status(
        self,
        *,
        userid: str,
        account: str,
        usd: Decimal,
        ofac: ScreenResult,
        value_date: date,
        mandate_ok: bool,
    ) -> str:
        if not userid or not account or not mandate_ok:
            return IN_UNMATCHED
        if ofac.hit:
            return IN_HELD
        if self._needs_dual_control(usd):
            return IN_PENDING
        now = float(self.clock())
        today = self._today(now)
        if value_date > today:
            return IN_QUEUED
        clock = self.calendar.snapshot(now)
        if clock['after_cutoff'] or not clock['business_day']:
            return IN_QUEUED
        return IN_POSTED

    def _debit(self, row: InboundSdd) -> str:
        if self.debit_fn is None:
            return 'ok'
        remark = 'sdd to %s' % (row.creditor_name[:20] or 'creditor')
        result = self.debit_fn(row.internal_account, row.debit_usd, remark)
        return _classify_money_result(result)

    def _credit(self, row: InboundSdd) -> str:
        if self.credit_fn is None:
            return 'ok'
        remark = 'sdd refund %s' % (row.scheme_id[:16])
        result = self.credit_fn(row.internal_account, row.debit_usd, remark)
        return _classify_money_result(result)

    def _advance_mandate(self, mandate: InSddMandate, sequence: str, now: float) -> None:
        mandate.last_sequence = sequence
        if sequence in {'OOFF', 'FNAL'}:
            mandate.status = MANDATE_CANCELLED
            mandate.note = 'completed_%s' % sequence.lower()
        mandate.updated_at = now
        self.store.update_mandate(mandate)

    def _try_post(self, row: InboundSdd) -> InboundSdd:
        classified = self._debit(row)
        now = float(self.clock())
        if classified == 'ok':
            row.status = IN_POSTED
            row.posted_at = now
            row.updated_at = now
            self.store.update(row)
            if row.mandate_id:
                mandate = self.store.get_mandate(row.mandate_id)
                if mandate is not None:
                    self._advance_mandate(mandate, row.sequence, now)
            return row
        if classified == 'nsf':
            row.status = IN_FAILED
            row.note = 'AM04'
            row.return_reason = 'AM04'
            row.updated_at = now
            self.store.update(row)
            raise InSddError('nsf', 'Insufficient funds to post this inbound SDD.', inbound=row)
        row.status = IN_FAILED
        row.note = 'debit_failed'
        row.updated_at = now
        self.store.update(row)
        raise InSddError('failed', 'Inbound SDD debit failed.', inbound=row)

    def add_mandate(
        self,
        *,
        actor: str,
        actor_type: str,
        userid: str,
        values: Dict[str, Any],
    ) -> InSddMandate:
        self._require_mandate_manage(actor_type)
        owner = str(userid)
        if actor_type not in EMPLOYEE_ROLES and actor != owner:
            raise InSddError('insdd_forbidden', 'Not allowed to manage this mandate book.')
        umr = normalize_umr(values.get('umr') or values.get('mandate') or values.get('mandate_id'))
        creditor_id = normalize_creditor_identifier(values.get('creditor_id') or values.get('ci'))
        existing = self.store.get_mandate_by_umr(umr, creditor_id)
        if existing is not None:
            raise InSddError('mandate_duplicate', 'A mandate with this UMR and creditor already exists.')
        if len(self.store.list_mandates(owner)) >= self.policy.max_mandates:
            raise InSddError('mandate_limit', 'Mandate limit reached.')
        account = normalize_account(values.get('account') or values.get('internal_account'))
        self._assert_internal_account(owner, account)
        try:
            creditor_name = normalize_legal_name(values.get('creditor_name') or values.get('name') or 'CREDITOR')
        except WireError:
            creditor_name = normalize_party(values.get('creditor_name') or 'CREDITOR')[:80] or 'CREDITOR'
        now = float(self.clock())
        row = InSddMandate(
            mandate_id=uuid.uuid4().hex,
            userid=owner,
            umr=umr,
            creditor_id=creditor_id,
            creditor_name=creditor_name,
            scheme=normalize_scheme(values.get('scheme') or SCHEME_CORE),
            last_sequence='',
            internal_account=account,
            status=MANDATE_ACTIVE,
            signature_date=str(values.get('signature_date') or self._today(now).isoformat()),
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_mandate(row)
        self._rematch_unmatched(row)
        return row

    def _rematch_unmatched(self, mandate: InSddMandate) -> None:
        for inbound in self.store.list_unmatched():
            if inbound.umr != mandate.umr or inbound.creditor_id != mandate.creditor_id:
                continue
            if inbound.scheme != mandate.scheme:
                continue
            if not _sequence_allowed(mandate.last_sequence, inbound.sequence):
                continue
            inbound.userid = mandate.userid
            inbound.internal_account = mandate.internal_account
            inbound.mandate_id = mandate.mandate_id
            inbound.note = ''
            ofac = ScreenResult(bool(inbound.ofac_hit), inbound.ofac_match)
            value_date = date(
                int(inbound.value_date[:4]), int(inbound.value_date[4:6]), int(inbound.value_date[6:8]),
            )
            inbound.status = self._evaluate_status(
                userid=mandate.userid,
                account=mandate.internal_account,
                usd=parse_money(inbound.debit_usd),
                ofac=ofac,
                value_date=value_date,
                mandate_ok=True,
            )
            inbound.updated_at = float(self.clock())
            self.store.update(inbound)
            if inbound.status == IN_POSTED:
                try:
                    self._try_post(inbound)
                except InSddError:
                    continue

    def _set_mandate_status(
        self,
        *,
        mandate_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> InSddMandate:
        self._require_mandate_manage(actor_type)
        row = self.store.get_mandate(mandate_id)
        if row is None:
            raise InSddError('mandate_not_found', 'Mandate not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise InSddError('insdd_forbidden', 'Not allowed to manage this mandate.')
        if row.status == MANDATE_CANCELLED and status != MANDATE_CANCELLED:
            raise InSddError('already_cancelled', 'Cancelled mandates cannot be reused.')
        if status == MANDATE_PAUSED and row.status == MANDATE_PAUSED:
            raise InSddError('already_paused', 'Mandate is already paused.')
        if status == MANDATE_ACTIVE and row.status == MANDATE_ACTIVE:
            raise InSddError('already_active', 'Mandate is already active.')
        if status == MANDATE_CANCELLED and row.status == MANDATE_CANCELLED:
            raise InSddError('already_cancelled', 'Mandate is already cancelled.')
        row.status = status
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_mandate(row)
        return row

    def pause_mandate(self, **kwargs: Any) -> InSddMandate:
        return self._set_mandate_status(status=MANDATE_PAUSED, **kwargs)

    def resume_mandate(self, **kwargs: Any) -> InSddMandate:
        return self._set_mandate_status(status=MANDATE_ACTIVE, **kwargs)

    def cancel_mandate(self, **kwargs: Any) -> InSddMandate:
        return self._set_mandate_status(status=MANDATE_CANCELLED, **kwargs)

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        values: Dict[str, Any],
        batch_id: str = '',
    ) -> Tuple[InboundSdd, bool]:
        self._require_staff(actor_type)
        message = message_from_values(values)
        euros = parse_money(message['amount'])
        self._assert_amount(euros, message['scheme'])
        self._assert_receiver(message['receiver_bic'])
        existing = self.store.get_by_scheme_id(message['scheme_id'])
        if existing is not None:
            return existing, False
        if len(self.store.list_all()) >= self.policy.max_inbounds:
            raise InSddError('inbound_limit', 'Inbound SDD limit reached.')
        quoted = self.fx.quote(euros)
        usd = parse_money(quoted['debit_usd'])
        try:
            account = extract_iban_account(message['debtor_iban'])
            account = normalize_account(account)
        except (InSddError, AccountError):
            account = ''
        userid = self._lookup(account) if account else None
        mandate = self.store.get_mandate_by_umr(message['umr'], message['creditor_id'])
        mandate_ok = False
        note = ''
        if mandate is None:
            note = 'mandate_missing'
            userid = userid or ''
        elif mandate.status != MANDATE_ACTIVE:
            note = 'mandate_inactive'
            userid = mandate.userid
            account = mandate.internal_account
        elif mandate.scheme != message['scheme']:
            note = 'scheme_mismatch'
            userid = mandate.userid
            account = mandate.internal_account
        elif not _sequence_allowed(mandate.last_sequence, message['sequence']):
            note = 'invalid_sequence'
            userid = mandate.userid
            account = mandate.internal_account
        else:
            mandate_ok = True
            userid = mandate.userid
            account = mandate.internal_account
        credit_blocked = False
        if userid and account:
            types = self._account_types(userid)
            if types.get(account) == 'credit' and not self.policy.allow_credit:
                credit_blocked = True
                userid = None
                account = ''
                mandate_ok = False
                note = 'credit_not_allowed'
        ofac = self._screen(message['creditor_name'])
        now = float(self.clock())
        value_date = self._parse_collection_date(message.get('collection_date') or '', now)
        if not self.calendar.is_business_day(value_date):
            value_date = self.calendar.next_business_day(value_date)
        status = self._evaluate_status(
            userid=userid or '',
            account=account,
            usd=usd,
            ofac=ofac,
            value_date=value_date,
            mandate_ok=mandate_ok,
        )
        if credit_blocked:
            status = IN_UNMATCHED
        try:
            purpose = normalize_purpose(values.get('purpose') or 'other')
        except WireError:
            purpose = 'other'
        try:
            creditor_name = normalize_legal_name(message['creditor_name'])
        except WireError:
            creditor_name = normalize_party(message['creditor_name'])[:80] or 'CREDITOR'
        try:
            debtor_name = normalize_legal_name(message['debtor_name'])
        except WireError:
            debtor_name = normalize_party(message['debtor_name'])[:80] or 'DEBTOR'
        row = InboundSdd(
            inbound_id=uuid.uuid4().hex,
            scheme_id=message['scheme_id'],
            userid=userid or '',
            internal_account=account,
            amount_eur=money_str(euros),
            debit_usd=quoted['debit_usd'],
            fx_rate=quoted['rate'],
            creditor_name=creditor_name,
            creditor_id=message['creditor_id'],
            creditor_iban=message.get('creditor_iban') or '',
            debtor_name=debtor_name,
            debtor_iban=message['debtor_iban'],
            receiver_bic=message['receiver_bic'],
            scheme=message['scheme'],
            sequence=message['sequence'],
            umr=message['umr'],
            mandate_id=mandate.mandate_id if mandate and mandate_ok else (mandate.mandate_id if mandate else ''),
            purpose=purpose,
            memo=message.get('memo') or '',
            status=status,
            value_date=value_date.strftime('%Y%m%d'),
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            return_id='',
            return_reason='',
            created_at=now,
            updated_at=now,
            note=note,
            batch_id=batch_id or normalize_id(values.get('batch_id') if values.get('batch_id') else ''),
            currency='EUR',
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
        messages = split_pain_file(text)
        batch_id = uuid.uuid4().hex
        accepted = []
        duplicates = []
        errors = []
        forced = normalize_scheme(scheme) if scheme else ''
        for payload in messages:
            try:
                values = dict(payload)
                if forced:
                    values['scheme'] = forced
                values['purpose'] = purpose
                row, created = self.ingest(
                    actor=actor, actor_type=actor_type, values=values, batch_id=batch_id,
                )
                if created:
                    accepted.append(row.to_dict())
                else:
                    duplicates.append(row.to_dict())
            except InSddError as exc:
                errors.append({'error': exc.code, 'message': exc.message})
        return {
            'message': 'Inbound SDD file ingested',
            'accepted_count': len(accepted),
            'duplicate_count': len(duplicates),
            'error_count': len(errors),
            'accepted': accepted,
            'duplicates': duplicates,
            'errors': errors,
            'batch_id': batch_id,
        }

    def assign(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        customer_id: Any,
        internal_account: Any = None,
        umr: Any = None,
        creditor_id: Any = None,
        scheme: Any = None,
    ) -> InboundSdd:
        self._require_staff(actor_type)
        row = self.store.get(inbound_id)
        if row is None:
            raise InSddError('inbound_not_found', 'Inbound SDD not found.')
        if row.status != IN_UNMATCHED:
            raise InSddError('not_assignable', 'Only unmatched inbound SDD can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise InSddError('missing_customer_id', 'Customer id is required.')
        account = normalize_account(internal_account or row.internal_account)
        self._assert_internal_account(owner, account)
        mandate = self.store.get_mandate_by_umr(umr or row.umr, creditor_id or row.creditor_id)
        if mandate is None:
            mandate = self.add_mandate(
                actor=actor,
                actor_type=actor_type,
                userid=owner,
                values={
                    'umr': umr or row.umr,
                    'creditor_id': creditor_id or row.creditor_id,
                    'creditor_name': row.creditor_name,
                    'scheme': scheme or row.scheme,
                    'account': account,
                },
            )
        elif mandate.userid != owner:
            raise InSddError('insdd_forbidden', 'Mandate belongs to another customer.')
        elif mandate.status != MANDATE_ACTIVE:
            raise InSddError('mandate_inactive', 'Assigned mandate is not active.')
        row.userid = owner
        row.internal_account = account
        row.mandate_id = mandate.mandate_id
        row.note = ''
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        ofac = ScreenResult(bool(row.ofac_hit), row.ofac_match)
        value_date = date(int(row.value_date[:4]), int(row.value_date[4:6]), int(row.value_date[6:8]))
        row.status = self._evaluate_status(
            userid=owner,
            account=account,
            usd=parse_money(row.debit_usd),
            ofac=ofac,
            value_date=value_date,
            mandate_ok=True,
        )
        self.store.update(row)
        if row.status == IN_POSTED:
            return self._try_post(row)
        return row

    def override_ofac(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> InboundSdd:
        self._require_staff(actor_type)
        row = self.store.get(inbound_id)
        if row is None:
            raise InSddError('inbound_not_found', 'Inbound SDD not found.')
        if row.status != IN_HELD and not row.ofac_hit:
            raise InSddError('not_overridable', 'Inbound SDD is not held for OFAC.')
        row.ofac_hit = 0
        row.ofac_match = ''
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        value_date = date(int(row.value_date[:4]), int(row.value_date[4:6]), int(row.value_date[6:8]))
        row.status = self._evaluate_status(
            userid=row.userid,
            account=row.internal_account,
            usd=parse_money(row.debit_usd),
            ofac=ScreenResult(False, ''),
            value_date=value_date,
            mandate_ok=bool(row.userid and row.internal_account and row.mandate_id),
        )
        self.store.update(row)
        if row.status == IN_POSTED:
            return self._try_post(row)
        return row

    def release(self, *, inbound_id: str, actor: str, actor_type: str) -> InboundSdd:
        self._require_staff(actor_type)
        row = self.store.get(inbound_id)
        if row is None:
            raise InSddError('inbound_not_found', 'Inbound SDD not found.')
        if row.status != IN_PENDING:
            raise InSddError('not_releasable', 'Inbound SDD is not pending release.')
        if row.actor == str(actor):
            raise InSddError('same_approver', 'A different employee must release this collection.')
        row.releaser = str(actor)
        now = float(self.clock())
        today = self._today(now)
        value_date = date(int(row.value_date[:4]), int(row.value_date[4:6]), int(row.value_date[6:8]))
        if value_date > today or not self.calendar.is_business_day(today) or self.calendar.is_after_cutoff(now):
            row.status = IN_QUEUED
            row.updated_at = now
            self.store.update(row)
            return row
        row.status = IN_POSTED
        row.updated_at = now
        self.store.update(row)
        return self._try_post(row)

    def reject(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> InboundSdd:
        self._require_staff(actor_type)
        row = self.store.get(inbound_id)
        if row is None:
            raise InSddError('inbound_not_found', 'Inbound SDD not found.')
        if row.status not in RETURNABLE_BEFORE_POST:
            raise InSddError('not_rejectable', 'Inbound SDD cannot be rejected.')
        now = float(self.clock())
        row.status = IN_REJECTED
        row.return_reason = normalize_return_reason(reason, default='MS03')
        row.return_id = compose_end_to_end_id('RJT')
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.returned_at = now
        row.updated_at = now
        self.store.update(row)
        return row

    def return_inbound(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> InboundSdd:
        self._require_staff(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        return self._return(row, actor=actor, reason=reason, note=note, force_window=True)

    def request_return(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'cust',
        note: Any = '',
    ) -> InboundSdd:
        self._require_customer_return(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise InSddError('insdd_forbidden', 'Not allowed to return this inbound SDD.')
        return self._return(row, actor=actor, reason=reason or 'cust', note=note, force_window=False)

    def request_refund(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'md06',
        note: Any = '',
    ) -> InboundSdd:
        self._require_customer_return(actor_type)
        row = self.get_inbound(inbound_id=inbound_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise InSddError('insdd_forbidden', 'Not allowed to refund this inbound SDD.')
        if row.status != IN_POSTED:
            raise InSddError('not_refundable', 'Only posted inbound SDD can be refunded.')
        if row.scheme == SCHEME_B2B:
            raise InSddError('scheme_no_refund', 'B2B Direct Debit has no no-questions refund.')
        return self._return(row, actor=actor, reason=reason or 'md06', note=note, force_window=False)

    def _refund_deadline(self, posted_at: float, reason: str) -> date:
        start = self.calendar.local_dt(posted_at).date()
        days = self.policy.core_unauth_days if reason == 'MD01' else self.policy.core_refund_days
        return start + timedelta(days=days)

    def _return(
        self,
        row: InboundSdd,
        *,
        actor: str,
        reason: Any,
        note: Any,
        force_window: bool,
    ) -> InboundSdd:
        if row.status in {IN_RETURNED, IN_REJECTED}:
            raise InSddError('already_returned', 'Inbound SDD is already returned or rejected.')
        code = normalize_return_reason(reason, default='MS03')
        now = float(self.clock())
        if row.status in RETURNABLE_BEFORE_POST:
            row.status = IN_RETURNED
            row.return_reason = code
            row.return_id = compose_end_to_end_id('RTR')
            row.note = normalize_note(note) or row.note
            row.actor = str(actor)
            row.returned_at = now
            row.updated_at = now
            self.store.update(row)
            return row
        if row.status != IN_POSTED:
            raise InSddError('not_returnable', 'Inbound SDD cannot be returned.')
        if row.scheme == SCHEME_B2B and not force_window:
            raise InSddError('scheme_no_refund', 'B2B Direct Debit has no no-questions refund.')
        if not force_window:
            deadline = self._refund_deadline(row.posted_at or row.created_at, code)
            if self._today(now) > deadline:
                raise InSddError('return_window_closed', 'SDD refund window has closed.')
        classified = self._credit(row)
        if classified == 'nsf':
            raise InSddError('nsf', 'Insufficient funds to refund this inbound SDD.', inbound=row)
        if classified != 'ok':
            raise InSddError('return_failed', 'Inbound SDD refund credit failed.', inbound=row)
        row.status = IN_RETURNED
        row.return_reason = code
        row.return_id = compose_end_to_end_id('RTR')
        row.note = normalize_note(note) or row.note
        row.actor = str(actor)
        row.returned_at = now
        row.updated_at = now
        self.store.update(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[InboundSdd]:
        now = float(self.clock())
        if not self.calendar.is_business_day(self._today(now)):
            return []
        if self.calendar.is_after_cutoff(now):
            return []
        today = self._today(now).strftime('%Y%m%d')
        posted = []
        for row in self.store.list_open():
            if row.status != IN_QUEUED:
                continue
            if userid is not None and row.userid != userid:
                continue
            if not row.userid or not row.internal_account:
                continue
            if row.value_date > today:
                continue
            try:
                posted.append(self._try_post(row))
            except InSddError:
                continue
        return posted

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self._require_view(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor and actor != userid:
            raise InSddError('insdd_forbidden', 'Not allowed to view this inbound book.')
        self.run_due(userid)
        rows = self.store.list_for(userid)
        mandates = self.store.list_mandates(userid)
        posted_ytd = Decimal('0.00')
        returned_ytd = Decimal('0.00')
        for row in rows:
            amount = parse_money(row.debit_usd, allow_zero=True)
            if row.status == IN_POSTED:
                posted_ytd += amount
            elif row.status == IN_RETURNED:
                returned_ytd += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'receiver_bic': self.policy.receiver_bic,
            'min_amount': money_str(self.policy.min_amount),
            'core_max': money_str(self.policy.core_max_amount),
            'b2b_max': money_str(self.policy.b2b_max_amount),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'eurusd': str(self.fx.rate),
            'clock': self.calendar.snapshot(now),
            'inbounds': [row.to_dict() for row in rows[:40]],
            'mandates': [row.to_dict() for row in mandates[:40]],
            'ytd_posted': money_str(posted_ytd),
            'ytd_returned': money_str(returned_ytd),
            'open_count': sum(1 for row in rows if row.status in OPEN_INBOUNDS),
            'posted_count': sum(1 for row in rows if row.status == IN_POSTED),
            'mandate_count': sum(1 for row in mandates if row.status == MANDATE_ACTIVE),
        }

    def unmatched_snapshot(self) -> Dict[str, Any]:
        rows = self.store.list_unmatched()
        return {
            'enabled': self.policy.enabled,
            'receiver_bic': self.policy.receiver_bic,
            'unmatched': [row.to_dict() for row in rows[:40]],
            'unmatched_count': len(rows),
        }


_SERVICE: Optional[InSddService] = None


def set_service(service: Optional[InSddService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[InSddService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('INSDD_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryInSddStore()
    path = os.environ.get('INSDD_DB', DEFAULT_STORE_PATH)
    return SqliteInSddStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    lookup_fn: Optional[Callable[[str], Optional[str]]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[Target2Calendar] = None,
    fx: Optional[EurUsdBook] = None,
) -> InSddService:
    if store is None:
        store = default_store()
    return InSddService(
        InSddPolicy.from_env(),
        store,
        clock=clock,
        debit_fn=debit_fn,
        credit_fn=credit_fn,
        accounts_fn=accounts_fn,
        lookup_fn=lookup_fn,
        screen_fn=screen_fn,
        calendar=calendar,
        fx=fx,
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
        'already_paused': 409,
        'already_active': 409,
        'already_cancelled': 409,
        'inbound_limit': 409,
        'mandate_duplicate': 409,
        'mandate_limit': 409,
        'nsf': 409,
        'failed': 409,
        'return_failed': 409,
        'insdd_forbidden': 403,
        'insdd_disabled': 403,
        'credit_not_allowed': 403,
        'same_approver': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_returnable': 403,
        'not_refundable': 403,
        'scheme_no_refund': 403,
        'return_window_closed': 403,
        'mandate_inactive': 403,
        'inbound_not_found': 404,
        'mandate_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_iban': 400,
        'invalid_bic': 400,
        'invalid_scheme_id': 400,
        'invalid_pain': 400,
        'invalid_scheme': 400,
        'invalid_sequence': 400,
        'invalid_mandate': 400,
        'invalid_creditor_id': 400,
        'invalid_currency': 400,
        'invalid_reason': 400,
        'invalid_purpose': 400,
        'not_sepa_country': 400,
        'wrong_receiver': 400,
        'core_amount_exceeded': 400,
        'b2b_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_mandate': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: InSddError) -> Dict[str, Any]:
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
    except InSddError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: InSddService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'InSdds': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_unmatched(service: InSddService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'insdd_forbidden'}), 403
    return jsonify({'InSdds': service.unmatched_snapshot()}), 200


def handle_preview(service: InSddService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'insdd_forbidden'}), 403
    values = request.get_json(silent=True) or {}

    def _run():
        preview = service.preview_message(values)
        return jsonify({'preview': preview}), 200

    return _handle_errors(_run)


def handle_quote(service: InSddService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}

    def _run():
        return jsonify({'quote': service.quote(values.get('amount'))}), 200

    return _handle_errors(_run)


def handle_ingest(service: InSddService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'Inbound SDD ingested' if created else 'Inbound SDD already posted',
            'inbound': row.to_dict(),
            'InSdds': service.snapshot(owner, actor=userid, actor_type=actor_type) if row.userid else service.unmatched_snapshot(),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_file(service: InSddService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    text = values.get('file') or values.get('pain') or values.get('text') or values.get('xml')
    if not text:
        return jsonify({'message': 'Operator file is required', 'error': 'missing_file'}), 400

    def _run():
        result = service.ingest_file(
            actor=userid,
            actor_type=actor_type,
            text=text,
            purpose=values.get('purpose') or 'other',
            scheme=values.get('scheme') or '',
        )
        result['Unmatched'] = service.unmatched_snapshot()
        return jsonify(result), 201 if result['accepted_count'] else 200

    return _handle_errors(_run)


def handle_assign(service: InSddService):
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
            umr=values.get('umr'),
            creditor_id=values.get('creditor_id'),
            scheme=values.get('scheme'),
        )
        return jsonify({
            'message': 'Inbound SDD assigned',
            'inbound': row.to_dict(),
            'InSdds': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_add_mandate(service: InSddService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid

    def _run():
        row = service.add_mandate(actor=userid, actor_type=actor_type, userid=owner, values=values)
        return jsonify({
            'message': 'SDD mandate registered',
            'mandate': row.to_dict(),
            'InSdds': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _mandate_action(service: InSddService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    mandate_id = str(values.get('mandate_id') or '').strip()
    if not mandate_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_mandate'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'pause':
            row = service.pause_mandate(mandate_id=mandate_id, actor=userid, actor_type=actor_type)
            message = 'Mandate paused'
        elif action == 'resume':
            row = service.resume_mandate(mandate_id=mandate_id, actor=userid, actor_type=actor_type)
            message = 'Mandate resumed'
        else:
            row = service.cancel_mandate(mandate_id=mandate_id, actor=userid, actor_type=actor_type)
            message = 'Mandate cancelled'
        return jsonify({
            'message': message,
            'mandate': row.to_dict(),
            'InSdds': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_action(service: InSddService, action: str):
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
            message = 'Inbound SDD released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Inbound SDD rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Inbound SDD returned'
        else:
            raise InSddError('invalid_reason', 'Unknown inbound action.')
        owner = row.userid or userid
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'InSdds': service.snapshot(owner, actor=userid, actor_type=actor_type) if row.userid else service.unmatched_snapshot(),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: InSddService):
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
            reason=values.get('reason') or 'cust',
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Inbound SDD return requested',
            'inbound': row.to_dict(),
            'InSdds': service.snapshot(row.userid or userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_request_refund(service: InSddService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    inbound_id = str(values.get('inbound_id') or '').strip()
    if not inbound_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_inbound'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.request_refund(
            inbound_id=inbound_id,
            actor=userid,
            actor_type=actor_type,
            reason=values.get('reason') or 'md06',
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Inbound SDD refund requested',
            'inbound': row.to_dict(),
            'InSdds': service.snapshot(row.userid or userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: InSddService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner if actor_type not in EMPLOYEE_ROLES else None)
    return jsonify({'InSdds': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_insdd_routes(app, service: InSddService) -> None:
    @app.route('/listInSdds', methods=['POST', 'GET'])
    def list_insdds_route():
        return handle_list(service)

    @app.route('/listUnmatchedInSdds', methods=['POST', 'GET'])
    def list_unmatched_insdds_route():
        return handle_unmatched(service)

    @app.route('/previewInSdd', methods=['POST', 'GET'])
    def preview_insdd_route():
        return handle_preview(service)

    @app.route('/quoteInSddFx', methods=['POST', 'GET'])
    def quote_insdd_route():
        return handle_quote(service)

    @app.route('/ingestInSdd', methods=['POST', 'GET'])
    def ingest_insdd_route():
        return handle_ingest(service)

    @app.route('/ingestInSddFile', methods=['POST', 'GET'])
    def ingest_insdd_file_route():
        return handle_ingest_file(service)

    @app.route('/assignInSdd', methods=['POST', 'GET'])
    def assign_insdd_route():
        return handle_assign(service)

    @app.route('/addInSddMandate', methods=['POST', 'GET'])
    def add_insdd_mandate_route():
        return handle_add_mandate(service)

    @app.route('/pauseInSddMandate', methods=['POST', 'GET'])
    def pause_insdd_mandate_route():
        return _mandate_action(service, 'pause')

    @app.route('/resumeInSddMandate', methods=['POST', 'GET'])
    def resume_insdd_mandate_route():
        return _mandate_action(service, 'resume')

    @app.route('/cancelInSddMandate', methods=['POST', 'GET'])
    def cancel_insdd_mandate_route():
        return _mandate_action(service, 'cancel')

    @app.route('/overrideInSddOfac', methods=['POST', 'GET'])
    def override_insdd_ofac_route():
        return _staff_action(service, 'override')

    @app.route('/releaseInSdd', methods=['POST', 'GET'])
    def release_insdd_route():
        return _staff_action(service, 'release')

    @app.route('/rejectInSdd', methods=['POST', 'GET'])
    def reject_insdd_route():
        return _staff_action(service, 'reject')

    @app.route('/returnInSdd', methods=['POST', 'GET'])
    def return_insdd_route():
        return _staff_action(service, 'return')

    @app.route('/requestInSddReturn', methods=['POST', 'GET'])
    def request_insdd_return_route():
        return handle_request_return(service)

    @app.route('/requestInSddRefund', methods=['POST', 'GET'])
    def request_insdd_refund_route():
        return handle_request_refund(service)

    @app.route('/runDueInSdds', methods=['POST', 'GET'])
    def run_due_insdds_route():
        return handle_run_due(service)
