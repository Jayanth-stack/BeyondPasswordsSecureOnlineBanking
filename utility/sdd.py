"""SEPA Direct Debit (SDD Core / B2B) origination.

Customers collect euro payments from SEPA-zone debtors under an e-mandate.
Independent of SEPA SCT / Instant (PR #80), SWIFT MT103 (PR #78), domestic
Fedwire (PR #73), ACH linking / micro-deposits (PR #68), bill-pay outgoing
ACH (PR #66), inbound payroll splits (PR #64), the in-bank payee allowlist
(PR #26), and scheduled internal transfers (PR #36). Existing
`/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay unchanged.
`Customers.debit_request` / `credit_request` still write `debited` /
`direct deposited` unless a remark is supplied here.

Foundations (reusable beyond this screen):
- IBAN ISO 13616 mod-97 restricted to EPC SEPA countries
- BIC ISO 9362 (8→11 pad XXX)
- EPC Creditor Identifier (country + check + business code + national id)
- Unique Mandate Reference + Core/B2B scheme + FRST/RCUR/OOFF/FNAL
- TARGET2 clock (16:00 CEST cutoff, Easter/May 1/Christmas/Boxing Day)
- EUR-only + EURUSD quote (USD credit equivalent)
- ISO 20022 pain.008 field map
- Pre-notification lead times and Core refund / R-transaction windows
- Dual-control release on the USD equivalent
- Reusable OFAC via utility.wire.screen_name

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Debtor IBANs never appear in to_dict / snapshots.
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
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
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
DEBTOR_ACTIVE = 'active'
DEBTOR_PAUSED = 'paused'
DEBTOR_ARCHIVED = 'archived'
DEBTOR_STATUSES = frozenset({DEBTOR_ACTIVE, DEBTOR_PAUSED, DEBTOR_ARCHIVED})
OPEN_DEBTOR = frozenset({DEBTOR_ACTIVE, DEBTOR_PAUSED})
MANDATE_DRAFT = 'draft'
MANDATE_ACTIVE = 'active'
MANDATE_SUSPENDED = 'suspended'
MANDATE_CANCELLED = 'cancelled'
MANDATE_EXPIRED = 'expired'
MANDATE_STATUSES = frozenset({
    MANDATE_DRAFT, MANDATE_ACTIVE, MANDATE_SUSPENDED, MANDATE_CANCELLED, MANDATE_EXPIRED,
})
OPEN_MANDATE = frozenset({MANDATE_DRAFT, MANDATE_ACTIVE, MANDATE_SUSPENDED})
SDD_HELD = 'held'
SDD_QUEUED = 'queued'
SDD_PENDING = 'pending_release'
SDD_SENT = 'sent'
SDD_SETTLED = 'settled'
SDD_REJECTED = 'rejected'
SDD_CANCELLED = 'cancelled'
SDD_RETURNED = 'returned'
SDD_REFUNDED = 'refunded'
SDD_FAILED = 'failed'
SDD_STATUSES = frozenset({
    SDD_HELD, SDD_QUEUED, SDD_PENDING, SDD_SENT, SDD_SETTLED,
    SDD_REJECTED, SDD_CANCELLED, SDD_RETURNED, SDD_REFUNDED, SDD_FAILED,
})
OPEN_COLLECTIONS = frozenset({SDD_HELD, SDD_QUEUED, SDD_PENDING, SDD_SENT})
CANCELABLE = frozenset({SDD_HELD, SDD_QUEUED, SDD_PENDING})
FEE_NONE = 'none'
FEE_COLLECTED = 'collected'
FEE_WAIVED = 'waived'
FEE_NSF = 'nsf'
SCHEME_CORE = 'core'
SCHEME_B2B = 'b2b'
SCHEMES = frozenset({SCHEME_CORE, SCHEME_B2B})
SCHEME_ALIASES = {
    'sdd': SCHEME_CORE, 'sepa': SCHEME_CORE, 'consumer': SCHEME_CORE,
    'core': SCHEME_CORE, 'cor1': SCHEME_CORE,
    'b2b': SCHEME_B2B, 'business': SCHEME_B2B, 'b2b.': SCHEME_B2B,
}
SEQ_FRST = 'FRST'
SEQ_RCUR = 'RCUR'
SEQ_OOFF = 'OOFF'
SEQ_FNAL = 'FNAL'
SEQUENCES = frozenset({SEQ_FRST, SEQ_RCUR, SEQ_OOFF, SEQ_FNAL})
SEQ_ALIASES = {
    'first': SEQ_FRST, 'frst': SEQ_FRST, 'initial': SEQ_FRST,
    'recurring': SEQ_RCUR, 'rcur': SEQ_RCUR, 'repeat': SEQ_RCUR,
    'oneoff': SEQ_OOFF, 'ooff': SEQ_OOFF, 'once': SEQ_OOFF, 'one-off': SEQ_OOFF,
    'final': SEQ_FNAL, 'fnal': SEQ_FNAL, 'last': SEQ_FNAL,
}
PURPOSES = frozenset({'goods', 'payroll', 'tax', 'loan', 'rent', 'subscription', 'other'})
PURPOSE_ALIASES = {
    'invoice': 'goods', 'purchase': 'goods', 'vendor': 'goods',
    'salary': 'payroll', 'wage': 'payroll',
    'irs': 'tax', 'taxes': 'tax',
    'mortgage': 'loan', 'housing': 'rent',
    'utility': 'subscription', 'membership': 'subscription', 'bill': 'subscription',
}
RETURN_REASONS = frozenset({
    'AC01', 'AC04', 'AC06', 'AM04', 'MD01', 'MD06', 'MD07', 'MS02', 'MS03', 'RR01', 'RR02', 'RR03',
})
RETURN_ALIASES = {
    'invalid_account': 'AC01', 'closed': 'AC04', 'blocked': 'AC06',
    'nsf': 'AM04', 'insufficient': 'AM04',
    'no_mandate': 'MD01', 'unauthorized': 'MD01',
    'refund': 'MD06', 'customer_refund': 'MD06',
    'deceased': 'MD07',
    'refusal': 'MS02', 'reason_not_specified': 'MS03',
    'regulatory': 'RR01',
}
SEPA_IBAN_LENGTHS = {
    'AD': 24, 'AT': 20, 'BE': 16, 'BG': 22, 'CH': 21, 'CY': 28, 'CZ': 24, 'DE': 22,
    'DK': 18, 'EE': 20, 'ES': 24, 'FI': 18, 'FR': 27, 'GB': 22, 'GI': 23, 'GR': 27,
    'HR': 21, 'HU': 28, 'IE': 22, 'IS': 26, 'IT': 27, 'LI': 21, 'LT': 20, 'LU': 20,
    'LV': 21, 'MC': 27, 'MT': 31, 'NL': 18, 'NO': 15, 'PL': 28, 'PT': 25, 'RO': 24,
    'SE': 24, 'SI': 19, 'SK': 24, 'SM': 27, 'VA': 22,
}
SEPA_COUNTRIES = frozenset(SEPA_IBAN_LENGTHS)
DEFAULT_WATCHLIST = (
    'BLOCKED PERSON',
    'SANCTIONED ENTITY',
    'OFAC TESTNAME',
)
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/sdd.sqlite'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)
CORE_REFUND_DAYS = 56
UNAUTHORIZED_REFUND_DAYS = 396


class SddError(ValueError):
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
        raise SddError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_party(value: Any) -> str:
    text = str(value or '').upper()
    cleaned = []
    for ch in text:
        if ch.isalnum() or ch.isspace():
            cleaned.append(ch)
        elif ch in "-'.,/&":
            cleaned.append(' ')
    compact = ' '.join(''.join(cleaned).split())
    return compact


def normalize_legal_name(value: Any) -> str:
    text = normalize_party(value)
    if not (2 <= len(text) <= 70):
        raise SddError('invalid_name', 'Legal name must be 2-70 characters.')
    return text


def normalize_city(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise SddError('invalid_address', 'City must be 2-40 characters.')
    return text


def normalize_country(value: Any) -> str:
    text = ''.join(ch for ch in str(value or '').upper() if ch.isalpha())
    if text not in SEPA_COUNTRIES:
        raise SddError('not_sepa_country', 'Country is not in the SEPA zone.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or '').strip().lower()
    if not text:
        return default
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise SddError('invalid_purpose', 'Purpose is not allowed.')
    return text


def normalize_scheme(value: Any, *, default: str = SCHEME_CORE) -> str:
    text = str(value or '').strip().lower()
    if not text:
        return default
    text = SCHEME_ALIASES.get(text, text)
    if text not in SCHEMES:
        raise SddError('invalid_scheme', 'Scheme must be core or b2b.')
    return text


def normalize_sequence(value: Any, *, default: str = SEQ_FRST) -> str:
    text = str(value or '').strip().upper().replace('_', '').replace(' ', '')
    if not text:
        return default
    text = SEQ_ALIASES.get(text.lower(), text)
    if text not in SEQUENCES:
        raise SddError('invalid_sequence', 'Sequence must be FRST, RCUR, OOFF, or FNAL.')
    return text


def normalize_return_reason(value: Any, *, default: str = 'MS03') -> str:
    text = str(value or '').strip().upper()
    if not text:
        return default
    text = RETURN_ALIASES.get(text.lower(), text)
    if text not in RETURN_REASONS:
        raise SddError('invalid_reason', 'Return reason is not a recognized R-transaction code.')
    return text


def _alnum_to_digits(text: str) -> str:
    out = []
    for ch in text:
        if ch.isdigit():
            out.append(ch)
        else:
            out.append(str(ord(ch) - 55))
    return ''.join(out)


def _mod97(digits: str) -> int:
    remainder = 0
    for index in range(0, len(digits), 7):
        remainder = int(str(remainder) + digits[index:index + 7]) % 97
    return remainder


def iban_check_digit_ok(iban: str) -> bool:
    if len(iban) < 15 or not iban[:2].isalpha() or not iban[2:4].isdigit():
        return False
    return _mod97(_alnum_to_digits(iban[4:] + iban[:4])) == 1


def compose_iban_check_digits(country: str, bban: str) -> str:
    country = country.upper()
    remainder = _mod97(_alnum_to_digits(bban + country + '00'))
    return '%02d' % (98 - remainder)


def mask_iban(iban: str) -> str:
    text = str(iban or '')
    if len(text) < 8:
        return text
    return text[:2] + '****' + text[-4:]


def normalize_iban(value: Any) -> str:
    text = ''.join(ch for ch in str(value or '').upper() if ch.isalnum())
    if len(text) < 15:
        raise SddError('invalid_iban', 'IBAN is required.')
    country = text[:2]
    if country not in SEPA_COUNTRIES:
        raise SddError('not_sepa_country', 'IBAN country is not in the SEPA zone.')
    expected = SEPA_IBAN_LENGTHS[country]
    if len(text) != expected:
        raise SddError('invalid_iban', 'IBAN length does not match the country.')
    if not iban_check_digit_ok(text):
        raise SddError('invalid_iban', 'IBAN failed the ISO 13616 check digits.')
    return text


def normalize_bic(value: Any, *, required: bool = False) -> str:
    text = ''.join(ch for ch in str(value or '').upper() if ch.isalnum())
    if not text:
        if required:
            raise SddError('invalid_bic', 'BIC is required.')
        return ''
    if len(text) == 8:
        text = text + 'XXX'
    if len(text) != 11 or not text[:4].isalpha() or not text[4:6].isalpha() or not text[6:8].isalnum():
        raise SddError('invalid_bic', 'BIC must be an ISO 9362 8- or 11-character code.')
    return text


def bic_country(bic: str) -> str:
    if len(bic) < 6:
        return ''
    return bic[4:6]


def compose_creditor_identifier(country: str, national_id: Any, business_code: str = 'ZZZ') -> str:
    country = ''.join(ch for ch in str(country or '').upper() if ch.isalpha())
    if country not in SEPA_COUNTRIES:
        raise SddError('invalid_creditor_id', 'Creditor identifier country is not in the SEPA zone.')
    national = ''.join(ch for ch in str(national_id or '').upper() if ch.isalnum())
    if not (5 <= len(national) <= 28):
        raise SddError('invalid_creditor_id', 'National creditor id must be 5-28 characters.')
    code = ''.join(ch for ch in str(business_code or 'ZZZ').upper() if ch.isalnum())
    if len(code) != 3:
        raise SddError('invalid_creditor_id', 'Creditor business code must be 3 characters.')
    check = compose_iban_check_digits(country, national)
    return country + check + code + national


def creditor_identifier_ok(value: str) -> bool:
    text = ''.join(ch for ch in str(value or '').upper() if ch.isalnum())
    if len(text) < 12 or text[:2] not in SEPA_COUNTRIES or not text[2:4].isdigit():
        return False
    country, check, national = text[:2], text[2:4], text[7:]
    if not national:
        return False
    return compose_iban_check_digits(country, national) == check


def normalize_creditor_identifier(value: Any) -> str:
    text = ''.join(ch for ch in str(value or '').upper() if ch.isalnum())
    if not creditor_identifier_ok(text):
        raise SddError('invalid_creditor_id', 'Creditor identifier failed the EPC check digits.')
    return text


def normalize_umr(value: Any) -> str:
    text = ''.join(ch for ch in str(value or '').strip().upper() if ch.isalnum() or ch in '-._')
    if not (1 <= len(text) <= 35):
        raise SddError('invalid_mandate', 'Unique mandate reference must be 1-35 characters.')
    return text


def national_id_from_userid(userid: str) -> str:
    digest = hashlib.sha256(str(userid).encode('utf-8')).hexdigest()
    digits = ''.join(ch for ch in digest if ch.isdigit())
    return (digits + '00000000000')[:11]


def compose_end_to_end_id(*, userid: str, collection_id: str) -> str:
    raw = ('E2E' + str(userid)[:8] + collection_id).upper()
    return re.sub(r'[^A-Z0-9]', '', raw)[:35]


def compose_message_id(*, cycle: str, sequence: int) -> str:
    return 'SDD%s%06d' % (cycle, int(sequence))


def compose_pmtinf_id(*, cycle: str, sequence: int) -> str:
    return 'PINF%s%06d' % (cycle, int(sequence))


def compose_instr_id(collection_id: str) -> str:
    return ('INSTR' + collection_id)[:35]


def compose_tx_id(collection_id: str) -> str:
    return ('TX' + collection_id)[:35]


def compose_pain008(
    *,
    msg_id: str,
    created: datetime,
    collection_date: str,
    amount_eur: Decimal,
    sequence: str,
    scheme: str,
    creditor_name: str,
    creditor_identifier: str,
    debtor_name: str,
    debtor_iban: str,
    umr: str,
    signed_on: str,
    end_to_end_id: str,
    pmtinf_id: str,
    instr_id: str,
    tx_id: str,
    purpose: str = 'other',
) -> Dict[str, Any]:
    """ISO 20022 pain.008 field map. Full IBAN stays inside this structure only."""
    scheme_code = 'CORE' if scheme == SCHEME_CORE else 'B2B'
    return {
        'MsgId': msg_id,
        'CreDtTm': created.replace(microsecond=0).isoformat(),
        'NbOfTxs': '1',
        'CtrlSum': money_str(amount_eur),
        'PmtInf': {
            'PmtInfId': pmtinf_id,
            'PmtMtd': 'DD',
            'ReqdColltnDt': collection_date,
            'Cdtr': {'Nm': creditor_name},
            'CdtrSchmeId': creditor_identifier,
            'DrctDbtTxInf': {
                'PmtId': {
                    'InstrId': instr_id,
                    'EndToEndId': end_to_end_id,
                    'TxId': tx_id,
                },
                'InstdAmt': {'Ccy': 'EUR', 'Value': money_str(amount_eur)},
                'ChrgBr': 'SLEV',
                'LclInstrm': scheme_code,
                'SeqTp': sequence,
                'DrctDbtTx': {
                    'MndtRltdInf': {
                        'MndtId': umr,
                        'DtOfSgntr': signed_on,
                    }
                },
                'Dbtr': {'Nm': debtor_name},
                'DbtrAcct': {'IBAN': debtor_iban},
                'Purp': purpose.upper()[:4],
            },
        },
    }


def compute_fee(scheme: str, core_fee: Decimal, b2b_fee: Decimal, *, waived: bool = False) -> Decimal:
    if waived:
        return Decimal('0.00')
    return b2b_fee if scheme == SCHEME_B2B else core_fee


def lead_days_for(scheme: str, sequence: str, policy: 'SddPolicy') -> int:
    if scheme == SCHEME_B2B:
        return policy.b2b_lead_days
    if sequence in {SEQ_FRST, SEQ_OOFF}:
        return policy.core_first_lead_days
    return policy.core_recurring_lead_days


def easter_gregorian(year: int) -> date:
    """Anonymous Gregorian computus."""
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
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
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
    """TARGET2 business-day + collection cutoff clock. Injectable offset and holidays."""

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
        return (local.hour, local.minute) >= (self.cutoff_hour, self.cutoff_minute)

    def next_business_day(self, day: date) -> date:
        cursor = day + timedelta(days=1)
        while not self.is_business_day(cursor):
            cursor += timedelta(days=1)
        return cursor

    def add_business_days(self, day: date, count: int) -> date:
        cursor = day
        steps = max(0, int(count))
        while steps:
            cursor = self.next_business_day(cursor)
            steps -= 1
        if not self.is_business_day(cursor):
            cursor = self.next_business_day(cursor)
        return cursor

    def earliest_collection_date(self, ts: float, lead_days: int) -> date:
        local = self.local_dt(ts)
        start = local.date()
        if not self.is_business_day(start) or self.is_after_cutoff(ts):
            start = self.next_business_day(start)
        return self.add_business_days(start, lead_days)

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


@dataclass
class EurUsdBook:
    rate: Decimal = Decimal('1.08')

    def quote(self, euros: Decimal) -> Decimal:
        usd = (euros * self.rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
        return usd

    def snapshot(self) -> Dict[str, Any]:
        return {'pair': 'EURUSD', 'rate': money_str(self.rate), 'base': 'EUR', 'quote': 'USD'}


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


def _parse_iso_date(value: Any) -> Optional[date]:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise SddError('invalid_date', 'Date must be YYYY-MM-DD.') from exc


@dataclass
class SddPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_collect: bool = True
    allow_credit: bool = False
    max_debtors: int = 12
    max_mandates: int = 24
    max_collections: int = 120
    min_amount: Decimal = Decimal('1.00')
    max_amount: Decimal = Decimal('1000000.00')
    core_fee: Decimal = Decimal('8.00')
    b2b_fee: Decimal = Decimal('12.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    cutoff_hour: int = 16
    tz_offset_hours: int = 2
    eurusd: Decimal = Decimal('1.08')
    core_first_lead_days: int = 5
    core_recurring_lead_days: int = 2
    b2b_lead_days: int = 1
    core_refund_days: int = CORE_REFUND_DAYS
    unauthorized_refund_days: int = UNAUTHORIZED_REFUND_DAYS
    default_ci_country: str = 'DE'
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'SddPolicy':
        extra = _env_list('SDD_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('SDD_ENABLED', True),
            customer_manage=_env_bool('SDD_CUSTOMER_MANAGE', True),
            customer_collect=_env_bool('SDD_CUSTOMER_COLLECT', True),
            allow_credit=_env_bool('SDD_ALLOW_CREDIT', False),
            max_debtors=max(1, _env_int('SDD_MAX_DEBTORS', 12)),
            max_mandates=max(1, _env_int('SDD_MAX_MANDATES', 24)),
            max_collections=max(1, _env_int('SDD_MAX_COLLECTIONS', 120)),
            min_amount=_env_money('SDD_MIN_AMOUNT', '1.00'),
            max_amount=_env_money('SDD_MAX_AMOUNT', '1000000.00'),
            core_fee=_env_money('SDD_CORE_FEE', '8.00'),
            b2b_fee=_env_money('SDD_B2B_FEE', '12.00'),
            dual_control_threshold=_env_money('SDD_DUAL_CONTROL', '10000.00'),
            cutoff_hour=max(0, min(23, _env_int('SDD_CUTOFF_HOUR', 16))),
            tz_offset_hours=_env_int('SDD_TZ_OFFSET', 2),
            eurusd=_env_money('SDD_EURUSD', '1.08'),
            core_first_lead_days=max(0, _env_int('SDD_CORE_FIRST_LEAD', 5)),
            core_recurring_lead_days=max(0, _env_int('SDD_CORE_RCUR_LEAD', 2)),
            b2b_lead_days=max(0, _env_int('SDD_B2B_LEAD', 1)),
            core_refund_days=max(1, _env_int('SDD_CORE_REFUND_DAYS', CORE_REFUND_DAYS)),
            unauthorized_refund_days=max(1, _env_int('SDD_UNAUTH_REFUND_DAYS', UNAUTHORIZED_REFUND_DAYS)),
            default_ci_country=os.environ.get('SDD_CI_COUNTRY', 'DE').strip().upper() or 'DE',
            watchlist=watch,
            extra_holidays=_env_list('SDD_HOLIDAYS'),
        )


@dataclass
class SddCreditor:
    userid: str
    creditor_identifier: str
    legal_name: str
    country: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'userid': self.userid,
            'creditor_identifier': self.creditor_identifier,
            'legal_name': self.legal_name,
            'country': self.country,
            'created_at': self.created_at,
        }


@dataclass
class SddDebtor:
    debtor_id: str
    userid: str
    nickname: str
    legal_name: str
    iban: str
    bic: str
    city: str
    country: str
    default_account: str
    default_scheme: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'debtor_id': self.debtor_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'iban_masked': mask_iban(self.iban),
            'iban_last4': last4(self.iban),
            'bic': self.bic,
            'city': self.city,
            'country': self.country,
            'default_account': self.default_account,
            'default_scheme': self.default_scheme,
            'status': self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


@dataclass
class SddMandate:
    mandate_id: str
    userid: str
    debtor_id: str
    umr: str
    scheme: str
    sequence: str
    signed_on: str
    expires_on: str
    creditor_identifier: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'mandate_id': self.mandate_id,
            'userid': self.userid,
            'debtor_id': self.debtor_id,
            'umr': self.umr,
            'scheme': self.scheme,
            'sequence': self.sequence,
            'signed_on': self.signed_on,
            'expires_on': self.expires_on,
            'creditor_identifier': self.creditor_identifier,
            'status': self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


@dataclass
class SddCollection:
    collection_id: str
    trace_id: str
    mandate_id: str
    debtor_id: str
    userid: str
    internal_account: str
    amount_eur: str
    credit_usd: str
    fee: str
    fee_status: str
    nickname: str
    legal_name: str
    iban_masked: str
    iban_last4: str
    scheme: str
    sequence: str
    umr: str
    creditor_identifier: str
    purpose: str
    memo: str
    status: str
    collection_date: str
    msgid: str = ''
    pmtinf_id: str = ''
    instr_id: str = ''
    end_to_end_id: str = ''
    tx_id: str = ''
    actor: str = ''
    releaser: str = ''
    ofac_hit: int = 0
    ofac_match: str = ''
    created_at: float = 0.0
    updated_at: float = 0.0
    sent_at: float = 0.0
    settled_at: float = 0.0
    returned_at: float = 0.0
    refunded_at: float = 0.0
    note: str = ''
    reason: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'collection_id': self.collection_id,
            'trace_id': self.trace_id,
            'mandate_id': self.mandate_id,
            'debtor_id': self.debtor_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'amount_eur': self.amount_eur,
            'credit_usd': self.credit_usd,
            'fee': self.fee,
            'fee_status': self.fee_status,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'iban_masked': self.iban_masked,
            'iban_last4': self.iban_last4,
            'scheme': self.scheme,
            'sequence': self.sequence,
            'umr': self.umr,
            'creditor_identifier': self.creditor_identifier,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'collection_date': self.collection_date,
            'msgid': self.msgid,
            'pmtinf_id': self.pmtinf_id,
            'instr_id': self.instr_id,
            'end_to_end_id': self.end_to_end_id,
            'tx_id': self.tx_id,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'sent_at': self.sent_at,
            'settled_at': self.settled_at,
            'returned_at': self.returned_at,
            'refunded_at': self.refunded_at,
            'note': self.note,
            'reason': self.reason,
            'cancelable': self.status in CANCELABLE,
            'refundable': self.status == SDD_SETTLED and self.scheme == SCHEME_CORE,
        }


def _clone_creditor(row: SddCreditor) -> SddCreditor:
    return replace(row)


def _clone_debtor(row: SddDebtor) -> SddDebtor:
    return replace(row)


def _clone_mandate(row: SddMandate) -> SddMandate:
    return replace(row)


def _clone_collection(row: SddCollection) -> SddCollection:
    return replace(row)


def _creditor_from_row(row: Any) -> SddCreditor:
    return SddCreditor(
        userid=row['userid'],
        creditor_identifier=row['creditor_identifier'],
        legal_name=row['legal_name'],
        country=row['country'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
    )


def _debtor_from_row(row: Any) -> SddDebtor:
    return SddDebtor(
        debtor_id=row['debtor_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        iban=row['iban'],
        bic=row['bic'],
        city=row['city'],
        country=row['country'],
        default_account=row['default_account'],
        default_scheme=row['default_scheme'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
    )


def _mandate_from_row(row: Any) -> SddMandate:
    return SddMandate(
        mandate_id=row['mandate_id'],
        userid=row['userid'],
        debtor_id=row['debtor_id'],
        umr=row['umr'],
        scheme=row['scheme'],
        sequence=row['sequence'],
        signed_on=row['signed_on'],
        expires_on=row['expires_on'] or '',
        creditor_identifier=row['creditor_identifier'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
    )


def _collection_from_row(row: Any) -> SddCollection:
    return SddCollection(
        collection_id=row['collection_id'],
        trace_id=row['trace_id'],
        mandate_id=row['mandate_id'],
        debtor_id=row['debtor_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        amount_eur=row['amount_eur'],
        credit_usd=row['credit_usd'],
        fee=row['fee'],
        fee_status=row['fee_status'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        iban_masked=row['iban_masked'],
        iban_last4=row['iban_last4'],
        scheme=row['scheme'],
        sequence=row['sequence'],
        umr=row['umr'],
        creditor_identifier=row['creditor_identifier'],
        purpose=row['purpose'],
        memo=row['memo'],
        status=row['status'],
        collection_date=row['collection_date'],
        msgid=row['msgid'] or '',
        pmtinf_id=row['pmtinf_id'] or '',
        instr_id=row['instr_id'] or '',
        end_to_end_id=row['end_to_end_id'] or '',
        tx_id=row['tx_id'] or '',
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        sent_at=float(row['sent_at'] or 0),
        settled_at=float(row['settled_at'] or 0),
        returned_at=float(row['returned_at'] or 0),
        refunded_at=float(row['refunded_at'] or 0),
        note=row['note'] or '',
        reason=row['reason'] or '',
    )


class MemorySddStore:
    def __init__(self) -> None:
        self._creditors: Dict[str, SddCreditor] = {}
        self._debtors: Dict[str, SddDebtor] = {}
        self._mandates: Dict[str, SddMandate] = {}
        self._collections: Dict[str, SddCollection] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_creditor(self, row: SddCreditor) -> None:
        with self._lock:
            self._creditors[row.userid] = row

    def get_creditor(self, userid: str) -> Optional[SddCreditor]:
        with self._lock:
            row = self._creditors.get(userid)
            return _clone_creditor(row) if row else None

    def put_debtor(self, row: SddDebtor) -> None:
        with self._lock:
            self._debtors[row.debtor_id] = row

    def get_debtor(self, debtor_id: str) -> Optional[SddDebtor]:
        with self._lock:
            row = self._debtors.get(debtor_id)
            return _clone_debtor(row) if row else None

    def update_debtor(self, row: SddDebtor) -> None:
        with self._lock:
            self._debtors[row.debtor_id] = row

    def list_debtors(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[SddDebtor]:
        with self._lock:
            rows = [_clone_debtor(row) for row in self._debtors.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != DEBTOR_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_debtor_by_nickname(self, userid: str, nickname: str) -> Optional[SddDebtor]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._debtors.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_DEBTOR:
                    return _clone_debtor(row)
        return None

    def find_debtor_by_iban(self, userid: str, iban: str) -> Optional[SddDebtor]:
        with self._lock:
            for row in self._debtors.values():
                if row.userid == userid and row.iban == iban and row.status in OPEN_DEBTOR:
                    return _clone_debtor(row)
        return None

    def put_mandate(self, row: SddMandate) -> None:
        with self._lock:
            self._mandates[row.mandate_id] = row

    def get_mandate(self, mandate_id: str) -> Optional[SddMandate]:
        with self._lock:
            row = self._mandates.get(mandate_id)
            return _clone_mandate(row) if row else None

    def update_mandate(self, row: SddMandate) -> None:
        with self._lock:
            self._mandates[row.mandate_id] = row

    def list_mandates(self, userid: Optional[str] = None, debtor_id: Optional[str] = None) -> List[SddMandate]:
        with self._lock:
            rows = [_clone_mandate(row) for row in self._mandates.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if debtor_id is not None:
            rows = [row for row in rows if row.debtor_id == debtor_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_mandate_by_umr(self, userid: str, umr: str) -> Optional[SddMandate]:
        wanted = umr.strip().upper()
        with self._lock:
            for row in self._mandates.values():
                if row.userid == userid and row.umr == wanted and row.status in OPEN_MANDATE:
                    return _clone_mandate(row)
        return None

    def put_collection(self, row: SddCollection) -> SddCollection:
        with self._lock:
            existing_id = self._by_trace.get(row.trace_id)
            if existing_id is not None:
                return self._collections[existing_id]
            self._collections[row.collection_id] = row
            self._by_trace[row.trace_id] = row.collection_id
            return row

    def update_collection(self, row: SddCollection) -> None:
        with self._lock:
            self._collections[row.collection_id] = row

    def get_collection(self, collection_id: str) -> Optional[SddCollection]:
        with self._lock:
            row = self._collections.get(collection_id)
            return _clone_collection(row) if row else None

    def get_collection_by_trace(self, trace_id: str) -> Optional[SddCollection]:
        with self._lock:
            collection_id = self._by_trace.get(trace_id)
            return _clone_collection(self._collections[collection_id]) if collection_id else None

    def list_collections(self, userid: Optional[str] = None, mandate_id: Optional[str] = None) -> List[SddCollection]:
        with self._lock:
            rows = [_clone_collection(row) for row in self._collections.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if mandate_id is not None:
            rows = [row for row in rows if row.mandate_id == mandate_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def next_message_sequence(self, cycle_date: str) -> int:
        with self._lock:
            used = [row.msgid for row in self._collections.values() if row.msgid.startswith('SDD' + cycle_date)]
        return len(used) + 1


class SqliteSddStore:
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
                CREATE TABLE IF NOT EXISTS creditors (
                    userid TEXT PRIMARY KEY,
                    creditor_identifier TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    country TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS debtors (
                    debtor_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    iban TEXT NOT NULL,
                    bic TEXT NOT NULL DEFAULT '',
                    city TEXT NOT NULL,
                    country TEXT NOT NULL,
                    default_account TEXT NOT NULL,
                    default_scheme TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mandates (
                    mandate_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    debtor_id TEXT NOT NULL,
                    umr TEXT NOT NULL,
                    scheme TEXT NOT NULL,
                    sequence TEXT NOT NULL,
                    signed_on TEXT NOT NULL,
                    expires_on TEXT NOT NULL DEFAULT '',
                    creditor_identifier TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS collections (
                    collection_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    mandate_id TEXT NOT NULL,
                    debtor_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    internal_account TEXT NOT NULL,
                    amount_eur TEXT NOT NULL,
                    credit_usd TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    fee_status TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    iban_masked TEXT NOT NULL,
                    iban_last4 TEXT NOT NULL,
                    scheme TEXT NOT NULL,
                    sequence TEXT NOT NULL,
                    umr TEXT NOT NULL,
                    creditor_identifier TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    collection_date TEXT NOT NULL,
                    msgid TEXT NOT NULL DEFAULT '',
                    pmtinf_id TEXT NOT NULL DEFAULT '',
                    instr_id TEXT NOT NULL DEFAULT '',
                    end_to_end_id TEXT NOT NULL DEFAULT '',
                    tx_id TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    sent_at REAL NOT NULL DEFAULT 0,
                    settled_at REAL NOT NULL DEFAULT 0,
                    returned_at REAL NOT NULL DEFAULT 0,
                    refunded_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_creditor(self, row: SddCreditor) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO creditors (
                    userid, creditor_identifier, legal_name, country, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.userid, row.creditor_identifier, row.legal_name, row.country,
                    row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_creditor(self, userid: str) -> Optional[SddCreditor]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM creditors WHERE userid = ?', (userid,)).fetchone()
        return _creditor_from_row(row) if row else None

    def put_debtor(self, row: SddDebtor) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO debtors (
                    debtor_id, userid, nickname, legal_name, iban, bic, city, country,
                    default_account, default_scheme, status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.debtor_id, row.userid, row.nickname, row.legal_name, row.iban, row.bic,
                    row.city, row.country, row.default_account, row.default_scheme, row.status,
                    row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_debtor(self, debtor_id: str) -> Optional[SddDebtor]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM debtors WHERE debtor_id = ?', (debtor_id,)).fetchone()
        return _debtor_from_row(row) if row else None

    def update_debtor(self, row: SddDebtor) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE debtors SET nickname=?, legal_name=?, iban=?, bic=?, city=?, country=?,
                    default_account=?, default_scheme=?, status=?, actor=?, updated_at=?
                WHERE debtor_id=?
                """,
                (
                    row.nickname, row.legal_name, row.iban, row.bic, row.city, row.country,
                    row.default_account, row.default_scheme, row.status, row.actor,
                    row.updated_at, row.debtor_id,
                ),
            )
            conn.commit()

    def list_debtors(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[SddDebtor]:
        sql = 'SELECT * FROM debtors'
        args: List[Any] = []
        if userid is not None:
            sql += ' WHERE userid = ?'
            args.append(userid)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = [_debtor_from_row(row) for row in conn.execute(sql, args).fetchall()]
        if not include_archived:
            rows = [row for row in rows if row.status != DEBTOR_ARCHIVED]
        return rows

    def find_debtor_by_nickname(self, userid: str, nickname: str) -> Optional[SddDebtor]:
        wanted = nickname.strip().lower()
        for row in self.list_debtors(userid):
            if row.nickname.lower() == wanted and row.status in OPEN_DEBTOR:
                return row
        return None

    def find_debtor_by_iban(self, userid: str, iban: str) -> Optional[SddDebtor]:
        for row in self.list_debtors(userid):
            if row.iban == iban and row.status in OPEN_DEBTOR:
                return row
        return None

    def put_mandate(self, row: SddMandate) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mandates (
                    mandate_id, userid, debtor_id, umr, scheme, sequence, signed_on,
                    expires_on, creditor_identifier, status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.mandate_id, row.userid, row.debtor_id, row.umr, row.scheme, row.sequence,
                    row.signed_on, row.expires_on, row.creditor_identifier, row.status,
                    row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_mandate(self, mandate_id: str) -> Optional[SddMandate]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM mandates WHERE mandate_id = ?', (mandate_id,)).fetchone()
        return _mandate_from_row(row) if row else None

    def update_mandate(self, row: SddMandate) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE mandates SET umr=?, scheme=?, sequence=?, signed_on=?, expires_on=?,
                    creditor_identifier=?, status=?, actor=?, updated_at=?
                WHERE mandate_id=?
                """,
                (
                    row.umr, row.scheme, row.sequence, row.signed_on, row.expires_on,
                    row.creditor_identifier, row.status, row.actor, row.updated_at, row.mandate_id,
                ),
            )
            conn.commit()

    def list_mandates(self, userid: Optional[str] = None, debtor_id: Optional[str] = None) -> List[SddMandate]:
        clauses = []
        args: List[Any] = []
        if userid is not None:
            clauses.append('userid = ?')
            args.append(userid)
        if debtor_id is not None:
            clauses.append('debtor_id = ?')
            args.append(debtor_id)
        sql = 'SELECT * FROM mandates'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            return [_mandate_from_row(row) for row in conn.execute(sql, args).fetchall()]

    def find_mandate_by_umr(self, userid: str, umr: str) -> Optional[SddMandate]:
        wanted = umr.strip().upper()
        for row in self.list_mandates(userid):
            if row.umr == wanted and row.status in OPEN_MANDATE:
                return row
        return None

    def put_collection(self, row: SddCollection) -> SddCollection:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM collections WHERE trace_id = ?', (row.trace_id,),
            ).fetchone()
            if existing:
                return _collection_from_row(existing)
            conn.execute(
                """
                INSERT INTO collections (
                    collection_id, trace_id, mandate_id, debtor_id, userid, internal_account,
                    amount_eur, credit_usd, fee, fee_status, nickname, legal_name, iban_masked,
                    iban_last4, scheme, sequence, umr, creditor_identifier, purpose, memo,
                    status, collection_date, msgid, pmtinf_id, instr_id, end_to_end_id, tx_id,
                    actor, releaser, ofac_hit, ofac_match, created_at, updated_at, sent_at,
                    settled_at, returned_at, refunded_at, note, reason
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    row.collection_id, row.trace_id, row.mandate_id, row.debtor_id, row.userid,
                    row.internal_account, row.amount_eur, row.credit_usd, row.fee, row.fee_status,
                    row.nickname, row.legal_name, row.iban_masked, row.iban_last4, row.scheme,
                    row.sequence, row.umr, row.creditor_identifier, row.purpose, row.memo,
                    row.status, row.collection_date, row.msgid, row.pmtinf_id, row.instr_id,
                    row.end_to_end_id, row.tx_id, row.actor, row.releaser, row.ofac_hit,
                    row.ofac_match, row.created_at, row.updated_at, row.sent_at, row.settled_at,
                    row.returned_at, row.refunded_at, row.note, row.reason,
                ),
            )
            conn.commit()
            return row

    def update_collection(self, row: SddCollection) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE collections SET status=?, fee_status=?, msgid=?, pmtinf_id=?, instr_id=?,
                    end_to_end_id=?, tx_id=?, releaser=?, ofac_hit=?, ofac_match=?, updated_at=?,
                    sent_at=?, settled_at=?, returned_at=?, refunded_at=?, note=?, reason=?,
                    collection_date=?
                WHERE collection_id=?
                """,
                (
                    row.status, row.fee_status, row.msgid, row.pmtinf_id, row.instr_id,
                    row.end_to_end_id, row.tx_id, row.releaser, row.ofac_hit, row.ofac_match,
                    row.updated_at, row.sent_at, row.settled_at, row.returned_at, row.refunded_at,
                    row.note, row.reason, row.collection_date, row.collection_id,
                ),
            )
            conn.commit()

    def get_collection(self, collection_id: str) -> Optional[SddCollection]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM collections WHERE collection_id = ?', (collection_id,),
            ).fetchone()
        return _collection_from_row(row) if row else None

    def get_collection_by_trace(self, trace_id: str) -> Optional[SddCollection]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM collections WHERE trace_id = ?', (trace_id,)).fetchone()
        return _collection_from_row(row) if row else None

    def list_collections(self, userid: Optional[str] = None, mandate_id: Optional[str] = None) -> List[SddCollection]:
        clauses = []
        args: List[Any] = []
        if userid is not None:
            clauses.append('userid = ?')
            args.append(userid)
        if mandate_id is not None:
            clauses.append('mandate_id = ?')
            args.append(mandate_id)
        sql = 'SELECT * FROM collections'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            return [_collection_from_row(row) for row in conn.execute(sql, args).fetchall()]

    def next_message_sequence(self, cycle_date: str) -> int:
        prefix = 'SDD' + cycle_date
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT msgid FROM collections WHERE msgid LIKE ?', (prefix + '%',),
            ).fetchall()
        return len(rows) + 1


class SddService:
    def __init__(
        self,
        policy: SddPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[Target2Calendar] = None,
        fx_book: Optional[EurUsdBook] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or Target2Calendar(
            cutoff_hour=policy.cutoff_hour,
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )
        self.fx_book = fx_book or EurUsdBook(rate=policy.eurusd)

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise SddError('sdd_disabled', 'SEPA Direct Debit is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise SddError('sdd_forbidden', 'Customers cannot manage SEPA Direct Debit.')

    def _require_collect(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_collect:
            raise SddError('sdd_forbidden', 'Customers cannot originate SEPA Direct Debit.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise SddError('sdd_forbidden', 'Staff only.')

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
            raise SddError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise SddError('credit_not_allowed', 'Credit accounts cannot receive SEPA Direct Debit.')

    def _assert_amount(self, euros: Decimal) -> None:
        if euros < self.policy.min_amount or euros > self.policy.max_amount:
            raise SddError('amount_out_of_range', 'Amount is outside the allowed range.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, dollars: Decimal) -> bool:
        return dollars >= self.policy.dual_control_threshold

    def quote_fx(self, amount: Any) -> Dict[str, Any]:
        euros = parse_money(amount)
        self._assert_amount(euros)
        usd = self.fx_book.quote(euros)
        return {
            'amount_eur': money_str(euros),
            'credit_usd': money_str(usd),
            'fx': self.fx_book.snapshot(),
        }

    def register_creditor(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        legal_name: Any = None,
        country: Any = None,
        creditor_identifier: Any = None,
        national_id: Any = None,
    ) -> SddCreditor:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise SddError('sdd_forbidden', 'Not allowed to register a creditor for this customer.')
        existing = self.store.get_creditor(owner_userid)
        if existing is not None:
            raise SddError('creditor_duplicate', 'A creditor identifier is already on file.')
        now = float(self.clock())
        country_code = normalize_country(country or self.policy.default_ci_country)
        if creditor_identifier:
            ident = normalize_creditor_identifier(creditor_identifier)
            country_code = ident[:2]
        else:
            ident = compose_creditor_identifier(
                country_code, national_id or national_id_from_userid(owner_userid),
            )
        name = normalize_legal_name(legal_name or owner_userid)
        row = SddCreditor(
            userid=owner_userid,
            creditor_identifier=ident,
            legal_name=name,
            country=country_code,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_creditor(row)
        return row

    def ensure_creditor(self, *, owner_userid: str, actor: str, actor_type: str) -> SddCreditor:
        row = self.store.get_creditor(owner_userid)
        if row is not None:
            return row
        return self.register_creditor(owner_userid=owner_userid, actor=actor, actor_type=actor_type)

    def add_debtor(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        iban: Any,
        city: Any,
        country: Any = None,
        bic: Any = None,
        default_account: Any,
        default_scheme: Any = SCHEME_CORE,
    ) -> SddDebtor:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise SddError('sdd_forbidden', 'Not allowed to add debtors for this customer.')
        self.ensure_creditor(owner_userid=owner_userid, actor=actor, actor_type=actor_type)
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        account_iban = normalize_iban(iban)
        routing = normalize_bic(bic)
        if routing and bic_country(routing) != account_iban[:2]:
            raise SddError('bic_country_mismatch', 'BIC country must match the IBAN country.')
        country_code = normalize_country(country or account_iban[:2])
        if country_code != account_iban[:2]:
            raise SddError('invalid_country', 'Address country must match the IBAN country.')
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        if self.store.find_debtor_by_nickname(owner_userid, name) is not None:
            raise SddError('debtor_duplicate', 'A debtor with that nickname already exists.')
        if self.store.find_debtor_by_iban(owner_userid, account_iban) is not None:
            raise SddError('debtor_duplicate', 'That debtor IBAN is already on file.')
        open_rows = [row for row in self.store.list_debtors(owner_userid) if row.status in OPEN_DEBTOR]
        if len(open_rows) >= self.policy.max_debtors:
            raise SddError('debtor_limit', 'SEPA Direct Debit debtor limit reached.')
        now = float(self.clock())
        row = SddDebtor(
            debtor_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            iban=account_iban,
            bic=routing,
            city=normalize_city(city),
            country=country_code,
            default_account=account,
            default_scheme=normalize_scheme(default_scheme),
            status=DEBTOR_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_debtor(row)
        return row

    def get_debtor(self, *, debtor_id: str, actor: str, actor_type: str) -> SddDebtor:
        self._require_enabled()
        row = self.store.get_debtor(debtor_id)
        if row is None:
            raise SddError('debtor_not_found', 'SEPA Direct Debit debtor not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise SddError('sdd_forbidden', 'Not allowed to view this debtor.')
        return row

    def enforce_debtor(
        self,
        *,
        debtor_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> SddDebtor:
        row = self.get_debtor(debtor_id=debtor_id, actor=actor, actor_type=actor_type)
        if row.status == DEBTOR_ARCHIVED:
            raise SddError('already_archived', 'Debtor is archived.')
        if row.status == DEBTOR_PAUSED:
            raise SddError('debtor_paused', 'Debtor is paused.')
        if require_active and row.status != DEBTOR_ACTIVE:
            raise SddError('invalid_status', 'Debtor is not active.')
        return row

    def set_debtor_status(
        self,
        *,
        debtor_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> SddDebtor:
        self._require_manage(actor_type)
        row = self.get_debtor(debtor_id=debtor_id, actor=actor, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': DEBTOR_PAUSED, 'hold': DEBTOR_PAUSED,
            'resume': DEBTOR_ACTIVE, 'activate': DEBTOR_ACTIVE, 'unpause': DEBTOR_ACTIVE,
            'archive': DEBTOR_ARCHIVED, 'close': DEBTOR_ARCHIVED, 'cancel': DEBTOR_ARCHIVED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {DEBTOR_PAUSED, DEBTOR_ACTIVE, DEBTOR_ARCHIVED}:
            raise SddError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == DEBTOR_ARCHIVED:
            raise SddError('already_archived', 'Debtor is already archived.')
        if wanted == DEBTOR_PAUSED:
            if row.status == DEBTOR_PAUSED:
                raise SddError('already_paused', 'Debtor is already paused.')
            if row.status != DEBTOR_ACTIVE:
                raise SddError('invalid_status', 'Only an active debtor can be paused.')
        elif wanted == DEBTOR_ACTIVE:
            if row.status == DEBTOR_ACTIVE:
                raise SddError('already_active', 'Debtor is already active.')
            if row.status != DEBTOR_PAUSED:
                raise SddError('invalid_status', 'Only a paused debtor can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_debtor(row)
        return row

    def create_mandate(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        debtor_id: Any,
        umr: Any = None,
        scheme: Any = None,
        sequence: Any = None,
        signed_on: Any = None,
        expires_on: Any = None,
        activate: bool = False,
    ) -> SddMandate:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise SddError('sdd_forbidden', 'Not allowed to create mandates for this customer.')
        debtor = self.enforce_debtor(
            debtor_id=str(debtor_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if debtor.userid != owner_userid:
            raise SddError('sdd_forbidden', 'Debtor does not belong to this customer.')
        creditor = self.ensure_creditor(owner_userid=owner_userid, actor=actor, actor_type=actor_type)
        reference = normalize_umr(umr or ('UMR' + uuid.uuid4().hex[:16].upper()))
        if self.store.find_mandate_by_umr(owner_userid, reference) is not None:
            raise SddError('mandate_duplicate', 'That unique mandate reference is already on file.')
        open_rows = [row for row in self.store.list_mandates(owner_userid) if row.status in OPEN_MANDATE]
        if len(open_rows) >= self.policy.max_mandates:
            raise SddError('mandate_limit', 'SEPA Direct Debit mandate limit reached.')
        now = float(self.clock())
        today = self.calendar.local_dt(now).date()
        signed = _parse_iso_date(signed_on) or today
        expires = _parse_iso_date(expires_on)
        if expires is not None and expires < signed:
            raise SddError('invalid_date', 'Mandate expiry cannot precede the signature date.')
        row = SddMandate(
            mandate_id=uuid.uuid4().hex,
            userid=owner_userid,
            debtor_id=debtor.debtor_id,
            umr=reference,
            scheme=normalize_scheme(scheme or debtor.default_scheme),
            sequence=normalize_sequence(sequence or SEQ_FRST),
            signed_on=signed.isoformat(),
            expires_on=expires.isoformat() if expires else '',
            creditor_identifier=creditor.creditor_identifier,
            status=MANDATE_ACTIVE if activate else MANDATE_DRAFT,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_mandate(row)
        return row

    def get_mandate(self, *, mandate_id: str, actor: str, actor_type: str) -> SddMandate:
        self._require_enabled()
        row = self.store.get_mandate(mandate_id)
        if row is None:
            raise SddError('mandate_not_found', 'SEPA Direct Debit mandate not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise SddError('sdd_forbidden', 'Not allowed to view this mandate.')
        return row

    def enforce_mandate(
        self,
        *,
        mandate_id: str,
        actor: str,
        actor_type: str,
    ) -> SddMandate:
        row = self.get_mandate(mandate_id=mandate_id, actor=actor, actor_type=actor_type)
        now = self.calendar.local_dt(float(self.clock())).date()
        if row.expires_on:
            expires = date.fromisoformat(row.expires_on)
            if expires < now and row.status not in {MANDATE_CANCELLED, MANDATE_EXPIRED}:
                row.status = MANDATE_EXPIRED
                row.updated_at = float(self.clock())
                self.store.update_mandate(row)
        if row.status == MANDATE_EXPIRED:
            raise SddError('mandate_expired', 'Mandate has expired.')
        if row.status == MANDATE_CANCELLED:
            raise SddError('mandate_cancelled', 'Mandate is cancelled.')
        if row.status == MANDATE_SUSPENDED:
            raise SddError('mandate_suspended', 'Mandate is suspended.')
        if row.status != MANDATE_ACTIVE:
            raise SddError('mandate_inactive', 'Mandate must be active before collecting.')
        return row

    def set_mandate_status(
        self,
        *,
        mandate_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> SddMandate:
        self._require_manage(actor_type)
        row = self.get_mandate(mandate_id=mandate_id, actor=actor, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'activate': MANDATE_ACTIVE, 'active': MANDATE_ACTIVE, 'sign': MANDATE_ACTIVE,
            'suspend': MANDATE_SUSPENDED, 'pause': MANDATE_SUSPENDED,
            'cancel': MANDATE_CANCELLED, 'revoke': MANDATE_CANCELLED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {MANDATE_ACTIVE, MANDATE_SUSPENDED, MANDATE_CANCELLED}:
            raise SddError('invalid_status', 'Status must be activate, suspend, or cancel.')
        if row.status in {MANDATE_CANCELLED, MANDATE_EXPIRED}:
            raise SddError('already_cancelled' if row.status == MANDATE_CANCELLED else 'mandate_expired',
                           'Mandate is already closed.')
        if wanted == MANDATE_ACTIVE and row.status == MANDATE_ACTIVE:
            raise SddError('already_active', 'Mandate is already active.')
        if wanted == MANDATE_SUSPENDED and row.status == MANDATE_SUSPENDED:
            raise SddError('already_suspended', 'Mandate is already suspended.')
        if wanted == MANDATE_ACTIVE and row.status not in {MANDATE_DRAFT, MANDATE_SUSPENDED}:
            raise SddError('invalid_status', 'Only a draft or suspended mandate can be activated.')
        if wanted == MANDATE_SUSPENDED and row.status != MANDATE_ACTIVE:
            raise SddError('invalid_status', 'Only an active mandate can be suspended.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_mandate(row)
        return row

    def _assert_sequence(self, mandate: SddMandate, sequence: str) -> None:
        expected = mandate.sequence
        if expected == SEQ_FRST and sequence not in {SEQ_FRST, SEQ_OOFF}:
            raise SddError('invalid_sequence', 'First collection on this mandate must be FRST or OOFF.')
        if expected == SEQ_RCUR and sequence not in {SEQ_RCUR, SEQ_FNAL}:
            raise SddError('invalid_sequence', 'Recurring mandate collections must be RCUR or FNAL.')
        if expected == SEQ_OOFF and sequence != SEQ_OOFF:
            raise SddError('invalid_sequence', 'One-off mandate only accepts OOFF.')
        if expected == SEQ_FNAL:
            raise SddError('invalid_sequence', 'Mandate already used its final collection.')

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        mandate_id: Any,
        amount: Any,
        internal_account: Any = None,
        sequence: Any = None,
        collection_date: Any = None,
        waive_fee: bool = False,
    ) -> Dict[str, Any]:
        self._require_collect(actor_type)
        mandate = self.enforce_mandate(
            mandate_id=str(mandate_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if mandate.userid != owner_userid:
            raise SddError('sdd_forbidden', 'Mandate does not belong to this customer.')
        debtor = self.enforce_debtor(
            debtor_id=mandate.debtor_id, actor=actor, actor_type=actor_type,
        )
        euros = parse_money(amount)
        self._assert_amount(euros)
        usd = self.fx_book.quote(euros)
        account = normalize_account(internal_account or debtor.default_account)
        self._assert_internal_account(owner_userid, account)
        chosen = normalize_sequence(sequence or mandate.sequence)
        self._assert_sequence(mandate, chosen)
        fee = compute_fee(
            mandate.scheme, self.policy.core_fee, self.policy.b2b_fee,
            waived=bool(waive_fee) and actor_type in EMPLOYEE_ROLES,
        )
        now = float(self.clock())
        lead = lead_days_for(mandate.scheme, chosen, self.policy)
        earliest = self.calendar.earliest_collection_date(now, lead)
        requested = _parse_iso_date(collection_date) or earliest
        if requested < earliest:
            raise SddError('lead_time_not_met', 'Collection date does not meet the scheme lead time.')
        if not self.calendar.is_business_day(requested):
            requested = self.calendar.next_business_day(requested)
        ofac = self._screen(debtor.legal_name, aliases=(debtor.nickname,))
        clock = self.calendar.snapshot(now)
        return {
            'amount_eur': money_str(euros),
            'credit_usd': money_str(usd),
            'fee': money_str(fee),
            'net_usd': money_str(usd - fee),
            'internal_account': account,
            'debtor': debtor.to_dict(),
            'mandate': mandate.to_dict(),
            'sequence': chosen,
            'scheme': mandate.scheme,
            'collection_date': requested.isoformat(),
            'lead_days': lead,
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(usd),
            'fx': self.fx_book.snapshot(),
            'clock': clock,
        }

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        debtor: SddDebtor,
        mandate: SddMandate,
        account: str,
        euros: Decimal,
        usd: Decimal,
        fee: Decimal,
        sequence: str,
        purpose: str,
        memo: str,
        trace_id: str,
        ofac: ScreenResult,
        collection_date: date,
        waive_fee: bool,
    ) -> SddCollection:
        now = float(self.clock())
        today = self.calendar.local_dt(now).date()
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        if ofac.hit:
            status = SDD_HELD
        elif self._needs_dual_control(usd):
            status = SDD_PENDING
        elif collection_date > today or (
            collection_date == today and self.calendar.snapshot(now)['after_cutoff']
        ):
            status = SDD_QUEUED
        else:
            status = SDD_SENT
        row = SddCollection(
            collection_id=uuid.uuid4().hex,
            trace_id=trace_id,
            mandate_id=mandate.mandate_id,
            debtor_id=debtor.debtor_id,
            userid=owner_userid,
            internal_account=account,
            amount_eur=money_str(euros),
            credit_usd=money_str(usd),
            fee=money_str(fee),
            fee_status=fee_status,
            nickname=debtor.nickname,
            legal_name=debtor.legal_name,
            iban_masked=mask_iban(debtor.iban),
            iban_last4=last4(debtor.iban),
            scheme=mandate.scheme,
            sequence=sequence,
            umr=mandate.umr,
            creditor_identifier=mandate.creditor_identifier,
            purpose=purpose,
            memo=memo,
            status=status,
            collection_date=collection_date.isoformat(),
            actor=str(actor),
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
        )
        if status == SDD_SENT:
            self._transmit(row, actor=actor, debtor=debtor, mandate=mandate)
        return row

    def _transmit(self, row: SddCollection, *, actor: str, debtor: SddDebtor, mandate: SddMandate) -> SddCollection:
        now = float(self.clock())
        cycle = self.calendar.cycle_date(now)
        seq = self.store.next_message_sequence(cycle)
        row.msgid = compose_message_id(cycle=cycle, sequence=seq)
        row.pmtinf_id = compose_pmtinf_id(cycle=cycle, sequence=seq)
        row.instr_id = compose_instr_id(row.collection_id)
        row.end_to_end_id = compose_end_to_end_id(userid=row.userid, collection_id=row.collection_id)
        row.tx_id = compose_tx_id(row.collection_id)
        compose_pain008(
            msg_id=row.msgid,
            created=self.calendar.local_dt(now),
            collection_date=row.collection_date,
            amount_eur=parse_money(row.amount_eur),
            sequence=row.sequence,
            scheme=row.scheme,
            creditor_name=row.userid,
            creditor_identifier=row.creditor_identifier,
            debtor_name=debtor.legal_name,
            debtor_iban=debtor.iban,
            umr=row.umr,
            signed_on=mandate.signed_on,
            end_to_end_id=row.end_to_end_id,
            pmtinf_id=row.pmtinf_id,
            instr_id=row.instr_id,
            tx_id=row.tx_id,
            purpose=row.purpose,
        )
        row.status = SDD_SENT
        row.sent_at = now
        row.updated_at = now
        row.releaser = str(actor)
        return row

    def originate(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        mandate_id: Any,
        amount: Any,
        internal_account: Any = None,
        sequence: Any = None,
        purpose: Any = 'other',
        memo: Any = '',
        collection_date: Any = None,
        trace_id: Any = None,
        waive_fee: bool = False,
    ) -> Tuple[SddCollection, bool]:
        self._require_collect(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise SddError('sdd_forbidden', 'Not allowed to originate collections for this customer.')
        tid = normalize_id(trace_id)
        existing = self.store.get_collection_by_trace(tid)
        if existing is not None:
            return existing, False
        preview = self.preview(
            owner_userid=owner_userid,
            actor=actor,
            actor_type=actor_type,
            mandate_id=mandate_id,
            amount=amount,
            internal_account=internal_account,
            sequence=sequence,
            collection_date=collection_date,
            waive_fee=waive_fee,
        )
        mandate = self.enforce_mandate(
            mandate_id=str(mandate_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        debtor = self.enforce_debtor(
            debtor_id=mandate.debtor_id, actor=actor, actor_type=actor_type,
        )
        open_rows = [row for row in self.store.list_collections(owner_userid) if row.status in OPEN_COLLECTIONS]
        if len(open_rows) >= self.policy.max_collections:
            raise SddError('collection_limit', 'SEPA Direct Debit collection limit reached.')
        ofac = ScreenResult(
            hit=bool(preview['ofac'].get('hit')),
            matched=str(preview['ofac'].get('matched') or ''),
        ) if isinstance(preview['ofac'], dict) else preview['ofac']
        row = self._place(
            owner_userid=owner_userid,
            actor=actor,
            debtor=debtor,
            mandate=mandate,
            account=preview['internal_account'],
            euros=parse_money(preview['amount_eur']),
            usd=parse_money(preview['credit_usd']),
            fee=parse_money(preview['fee'], allow_zero=True),
            sequence=preview['sequence'],
            purpose=normalize_purpose(purpose),
            memo=normalize_note(memo),
            trace_id=tid,
            ofac=ofac,
            collection_date=date.fromisoformat(preview['collection_date']),
            waive_fee=bool(waive_fee) and actor_type in EMPLOYEE_ROLES,
        )
        stored = self.store.put_collection(row)
        if stored.collection_id != row.collection_id:
            return stored, False
        return row, True

    def get_collection(self, *, collection_id: str, actor: str, actor_type: str) -> SddCollection:
        self._require_enabled()
        row = self.store.get_collection(collection_id)
        if row is None:
            raise SddError('collection_not_found', 'SEPA Direct Debit collection not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise SddError('sdd_forbidden', 'Not allowed to view this collection.')
        return row

    def cancel_collection(
        self,
        *,
        collection_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> SddCollection:
        self._require_collect(actor_type)
        row = self.get_collection(collection_id=collection_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise SddError('sdd_forbidden', 'Not allowed to cancel this collection.')
        if row.status not in CANCELABLE:
            raise SddError('not_cancelable', 'Collection can no longer be cancelled.')
        row.status = SDD_CANCELLED
        row.note = normalize_note(note)
        row.updated_at = float(self.clock())
        self.store.update_collection(row)
        return row

    def override_ofac(
        self,
        *,
        collection_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> SddCollection:
        self._require_staff(actor_type)
        row = self.get_collection(collection_id=collection_id, actor=actor, actor_type=actor_type)
        if row.status != SDD_HELD:
            raise SddError('not_releasable', 'Only an OFAC-held collection can be overridden.')
        row.ofac_hit = 0
        row.note = normalize_note(note)
        row.updated_at = float(self.clock())
        usd = parse_money(row.credit_usd)
        now = float(self.clock())
        today = self.calendar.local_dt(now).date()
        due = date.fromisoformat(row.collection_date)
        if self._needs_dual_control(usd):
            row.status = SDD_PENDING
        elif due > today or (due == today and self.calendar.snapshot(now)['after_cutoff']):
            row.status = SDD_QUEUED
        else:
            mandate = self.store.get_mandate(row.mandate_id)
            debtor = self.store.get_debtor(row.debtor_id)
            if mandate is None or debtor is None:
                raise SddError('mandate_not_found', 'Mandate or debtor missing for this collection.')
            self._transmit(row, actor=actor, debtor=debtor, mandate=mandate)
        self.store.update_collection(row)
        return row

    def release_collection(
        self,
        *,
        collection_id: str,
        actor: str,
        actor_type: str,
    ) -> SddCollection:
        self._require_staff(actor_type)
        row = self.get_collection(collection_id=collection_id, actor=actor, actor_type=actor_type)
        if row.status == SDD_HELD:
            raise SddError('ofac_hold', 'OFAC hold must be overridden before release.')
        if row.status != SDD_PENDING:
            raise SddError('not_releasable', 'Collection is not pending dual-control release.')
        if row.actor == str(actor):
            raise SddError('same_approver', 'A different employee must release this collection.')
        mandate = self.store.get_mandate(row.mandate_id)
        debtor = self.store.get_debtor(row.debtor_id)
        if mandate is None or debtor is None:
            raise SddError('mandate_not_found', 'Mandate or debtor missing for this collection.')
        now = float(self.clock())
        today = self.calendar.local_dt(now).date()
        due = date.fromisoformat(row.collection_date)
        if due > today or (due == today and self.calendar.snapshot(now)['after_cutoff']):
            row.status = SDD_QUEUED
            row.releaser = str(actor)
            row.updated_at = now
        else:
            self._transmit(row, actor=actor, debtor=debtor, mandate=mandate)
        self.store.update_collection(row)
        return row

    def reject_collection(
        self,
        *,
        collection_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'MS03',
        note: Any = '',
    ) -> SddCollection:
        self._require_staff(actor_type)
        row = self.get_collection(collection_id=collection_id, actor=actor, actor_type=actor_type)
        if row.status not in {SDD_HELD, SDD_QUEUED, SDD_PENDING, SDD_SENT}:
            raise SddError('not_rejectable', 'Collection can no longer be rejected.')
        if row.status == SDD_SETTLED:
            raise SddError('already_settled', 'Settled collections must be returned or refunded.')
        row.status = SDD_REJECTED
        row.reason = normalize_return_reason(reason)
        row.note = normalize_note(note)
        row.updated_at = float(self.clock())
        self.store.update_collection(row)
        return row

    def waive_fee(
        self,
        *,
        collection_id: str,
        actor: str,
        actor_type: str,
    ) -> SddCollection:
        self._require_staff(actor_type)
        row = self.get_collection(collection_id=collection_id, actor=actor, actor_type=actor_type)
        if row.status not in CANCELABLE.union({SDD_SENT}):
            raise SddError('not_cancelable', 'Fee can only be waived before settlement.')
        if row.fee_status == FEE_COLLECTED:
            raise SddError('already_settled', 'Fee has already been collected.')
        row.fee_status = FEE_WAIVED
        row.updated_at = float(self.clock())
        self.store.update_collection(row)
        return row

    def _advance_mandate(self, row: SddCollection) -> None:
        mandate = self.store.get_mandate(row.mandate_id)
        if mandate is None:
            return
        now = float(self.clock())
        if row.sequence == SEQ_FRST:
            mandate.sequence = SEQ_RCUR
        elif row.sequence in {SEQ_OOFF, SEQ_FNAL}:
            mandate.status = MANDATE_CANCELLED
        mandate.updated_at = now
        self.store.update_mandate(mandate)

    def settle_collection(
        self,
        *,
        collection_id: str,
        actor: str,
        actor_type: str,
    ) -> SddCollection:
        if actor_type == 'system':
            self._require_enabled()
        elif actor_type not in EMPLOYEE_ROLES:
            self._require_collect(actor_type)
        else:
            self._require_staff(actor_type)
        lookup_actor = actor if actor_type != 'system' else actor
        lookup_type = 'admin' if actor_type == 'system' else actor_type
        row = self.get_collection(collection_id=collection_id, actor=lookup_actor, actor_type=lookup_type)
        if row.status == SDD_SETTLED:
            raise SddError('already_settled', 'Collection is already settled.')
        if row.status != SDD_SENT:
            raise SddError('not_settlable', 'Only a presented collection can be settled.')
        now = float(self.clock())
        today = self.calendar.local_dt(now).date()
        due = date.fromisoformat(row.collection_date)
        if due > today:
            raise SddError('not_settlable', 'Collection date has not been reached.')
        usd = parse_money(row.credit_usd)
        fee = parse_money(row.fee, allow_zero=True)
        remark = 'sdd from %s' % row.nickname
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(row.internal_account, money_str(usd), remark)
            except Exception as exc:
                row.status = SDD_FAILED
                row.note = str(exc)[:240]
                row.updated_at = now
                self.store.update_collection(row)
                raise SddError('failed', 'Settlement credit failed.', collection=row) from exc
            kind = _classify_money_result(result)
            if kind != 'ok':
                row.status = SDD_FAILED
                row.note = str(result)[:240]
                row.updated_at = now
                self.store.update_collection(row)
                raise SddError('failed', 'Settlement credit failed.', collection=row)
        if fee > 0 and row.fee_status != FEE_WAIVED and self.debit_fn is not None:
            try:
                fee_result = self.debit_fn(
                    row.internal_account, money_str(fee), 'sdd fee %s' % (row.end_to_end_id[:12] or row.collection_id[:12]),
                )
            except Exception:
                row.fee_status = FEE_NSF
            else:
                kind = _classify_money_result(fee_result)
                row.fee_status = FEE_COLLECTED if kind == 'ok' else FEE_NSF
        elif fee == 0 or row.fee_status == FEE_WAIVED:
            row.fee_status = FEE_WAIVED
        row.status = SDD_SETTLED
        row.settled_at = now
        row.updated_at = now
        if actor_type in EMPLOYEE_ROLES:
            row.releaser = row.releaser or str(actor)
        self.store.update_collection(row)
        self._advance_mandate(row)
        return row

    def _reverse_settlement(self, row: SddCollection, *, remark: str) -> None:
        usd = parse_money(row.credit_usd)
        if self.debit_fn is None:
            return
        try:
            result = self.debit_fn(row.internal_account, money_str(usd), remark)
        except Exception as exc:
            raise SddError('return_failed', 'Could not reverse the settled credit.', collection=row) from exc
        kind = _classify_money_result(result)
        if kind == 'nsf':
            raise SddError('nsf', 'Insufficient funds to reverse the settled credit.', collection=row)
        if kind != 'ok':
            raise SddError('return_failed', 'Could not reverse the settled credit.', collection=row)
        if row.fee_status == FEE_COLLECTED and self.credit_fn is not None:
            fee = parse_money(row.fee, allow_zero=True)
            if fee > 0:
                self.credit_fn(row.internal_account, money_str(fee), 'sdd fee return %s' % (row.end_to_end_id[:12] or row.collection_id[:12]))

    def return_collection(
        self,
        *,
        collection_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'MS03',
        note: Any = '',
    ) -> SddCollection:
        self._require_staff(actor_type)
        row = self.get_collection(collection_id=collection_id, actor=actor, actor_type=actor_type)
        if row.status in {SDD_RETURNED, SDD_REFUNDED}:
            raise SddError('already_returned', 'Collection has already been returned or refunded.')
        if row.status not in {SDD_SENT, SDD_SETTLED}:
            raise SddError('not_returnable', 'Only a presented or settled collection can be returned.')
        code = normalize_return_reason(reason)
        if row.status == SDD_SETTLED:
            self._reverse_settlement(row, remark='sdd return %s' % (row.end_to_end_id[:12] or row.collection_id[:12]))
        row.status = SDD_RETURNED
        row.reason = code
        row.note = normalize_note(note)
        row.returned_at = float(self.clock())
        row.updated_at = row.returned_at
        self.store.update_collection(row)
        return row

    def request_refund(
        self,
        *,
        collection_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'MD06',
        note: Any = '',
    ) -> SddCollection:
        self._require_collect(actor_type)
        row = self.get_collection(collection_id=collection_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise SddError('sdd_forbidden', 'Not allowed to refund this collection.')
        if row.status != SDD_SETTLED:
            raise SddError('not_refundable', 'Only a settled collection can be refunded.')
        if row.scheme == SCHEME_B2B:
            raise SddError('scheme_no_refund', 'B2B collections have no no-questions refund right.')
        code = normalize_return_reason(reason, default='MD06')
        now = float(self.clock())
        today = self.calendar.local_dt(now).date()
        settled_day = date.fromisoformat(row.collection_date)
        elapsed = (today - settled_day).days
        window = self.policy.unauthorized_refund_days if code == 'MD01' else self.policy.core_refund_days
        if elapsed > window:
            raise SddError('refund_window_closed', 'The SEPA Direct Debit refund window has closed.')
        self._reverse_settlement(row, remark='sdd refund %s' % (row.end_to_end_id[:12] or row.collection_id[:12]))
        row.status = SDD_REFUNDED
        row.reason = code
        row.note = normalize_note(note)
        row.refunded_at = now
        row.updated_at = now
        self.store.update_collection(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[SddCollection]:
        now = float(self.clock())
        today = self.calendar.local_dt(now).date()
        after = self.calendar.snapshot(now)['after_cutoff']
        changed: List[SddCollection] = []
        for row in self.store.list_collections(userid):
            due = date.fromisoformat(row.collection_date)
            if row.status == SDD_QUEUED and due <= today and not (due == today and after):
                mandate = self.store.get_mandate(row.mandate_id)
                debtor = self.store.get_debtor(row.debtor_id)
                if mandate is None or debtor is None:
                    continue
                self._transmit(row, actor=row.actor, debtor=debtor, mandate=mandate)
                self.store.update_collection(row)
                try:
                    settled = self.settle_collection(
                        collection_id=row.collection_id, actor=row.userid, actor_type='system',
                    )
                except SddError:
                    changed.append(row)
                    continue
                changed.append(settled)
                continue
            if row.status == SDD_SENT and due <= today and not (due == today and after):
                try:
                    settled = self.settle_collection(
                        collection_id=row.collection_id, actor=row.userid, actor_type='system',
                    )
                except SddError:
                    continue
                changed.append(settled)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        try:
            self.run_due(userid)
        except SddError:
            pass
        creditor = self.store.get_creditor(userid)
        debtors = self.store.list_debtors(userid)
        mandates = self.store.list_mandates(userid)
        collections = self.store.list_collections(userid)
        collected = Decimal('0.00')
        fees = Decimal('0.00')
        refunded = Decimal('0.00')
        for row in collections:
            amount = parse_money(row.credit_usd, allow_zero=True)
            if row.status == SDD_SETTLED:
                collected += amount
                if row.fee_status == FEE_COLLECTED:
                    fees += parse_money(row.fee, allow_zero=True)
            elif row.status in {SDD_REFUNDED, SDD_RETURNED} and row.settled_at:
                refunded += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'core_fee': money_str(self.policy.core_fee),
            'b2b_fee': money_str(self.policy.b2b_fee),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'fx': self.fx_book.snapshot(),
            'clock': self.calendar.snapshot(now),
            'creditor': creditor.to_dict() if creditor else None,
            'debtors': [row.to_dict() for row in debtors[:40]],
            'mandates': [row.to_dict() for row in mandates[:40]],
            'collections': [row.to_dict() for row in collections[:40]],
            'ytd_collected': money_str(collected),
            'ytd_fees': money_str(fees),
            'refunded_ytd': money_str(refunded),
            'active_count': sum(1 for row in debtors if row.status == DEBTOR_ACTIVE),
            'mandate_count': sum(1 for row in mandates if row.status == MANDATE_ACTIVE),
            'open_count': sum(1 for row in collections if row.status in OPEN_COLLECTIONS),
        }


_SERVICE: Optional[SddService] = None


def set_service(service: Optional[SddService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[SddService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('SDD_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemorySddStore()
    path = os.environ.get('SDD_DB', DEFAULT_STORE_PATH)
    return SqliteSddStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[Target2Calendar] = None,
    fx_book: Optional[EurUsdBook] = None,
) -> SddService:
    if store is None:
        store = default_store()
    return SddService(
        SddPolicy.from_env(),
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
        'debtor_duplicate': 409,
        'debtor_limit': 409,
        'mandate_duplicate': 409,
        'mandate_limit': 409,
        'collection_limit': 409,
        'creditor_duplicate': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_suspended': 409,
        'already_cancelled': 409,
        'already_settled': 409,
        'already_returned': 409,
        'nsf': 409,
        'failed': 409,
        'return_failed': 409,
        'sdd_forbidden': 403,
        'sdd_disabled': 403,
        'debtor_paused': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_cancelable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_settlable': 403,
        'not_returnable': 403,
        'not_refundable': 403,
        'scheme_no_refund': 403,
        'refund_window_closed': 403,
        'mandate_expired': 403,
        'mandate_cancelled': 403,
        'mandate_suspended': 403,
        'mandate_inactive': 403,
        'debtor_not_found': 404,
        'mandate_not_found': 404,
        'collection_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_iban': 400,
        'invalid_bic': 400,
        'invalid_country': 400,
        'invalid_address': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_scheme': 400,
        'invalid_sequence': 400,
        'invalid_mandate': 400,
        'invalid_creditor_id': 400,
        'invalid_date': 400,
        'invalid_reason': 400,
        'not_sepa_country': 400,
        'bic_country_mismatch': 400,
        'lead_time_not_met': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_debtor': 400,
        'missing_mandate': 400,
        'missing_collection': 400,
    }.get(code, 400)


def _error_body(exc: SddError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('collection') is not None:
        body['collection'] = exc.extra['collection'].to_dict()
    if exc.extra.get('debtor') is not None:
        body['debtor'] = exc.extra['debtor'].to_dict()
    if exc.extra.get('mandate') is not None:
        body['mandate'] = exc.extra['mandate'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except SddError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: SddService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Sdd': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_register_creditor(service: SddService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.register_creditor(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            legal_name=values.get('legal_name') or values.get('name'),
            country=values.get('country'),
            creditor_identifier=values.get('creditor_identifier') or values.get('ci'),
            national_id=values.get('national_id'),
        )
        return jsonify({
            'message': 'SEPA creditor registered',
            'creditor': row.to_dict(),
            'Sdd': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def handle_add_debtor(service: SddService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.add_debtor(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname'),
            legal_name=values.get('legal_name') or values.get('name'),
            iban=values.get('iban'),
            city=values.get('city'),
            country=values.get('country'),
            bic=values.get('bic'),
            default_account=values.get('default_account') or values.get('account') or values.get('from_account'),
            default_scheme=values.get('scheme') or values.get('default_scheme') or SCHEME_CORE,
        )
        return jsonify({
            'message': 'SEPA debtor added',
            'debtor': row.to_dict(),
            'Sdd': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _debtor_status_route(service: SddService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    debtor_id = str(values.get('debtor_id') or '').strip()
    if not debtor_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_debtor'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_debtor_status(
            debtor_id=debtor_id, actor=userid, actor_type=actor_type, status=status,
        )
        return jsonify({
            'message': ok_message,
            'debtor': row.to_dict(),
            'Sdd': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_create_mandate(service: SddService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    debtor_id = str(values.get('debtor_id') or '').strip()
    if not debtor_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_debtor'}), 400

    def _run():
        row = service.create_mandate(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            debtor_id=debtor_id,
            umr=values.get('umr') or values.get('mandate_ref'),
            scheme=values.get('scheme'),
            sequence=values.get('sequence'),
            signed_on=values.get('signed_on') or values.get('signed'),
            expires_on=values.get('expires_on') or values.get('expires'),
            activate=bool(values.get('activate', True)),
        )
        return jsonify({
            'message': 'SEPA mandate created',
            'mandate': row.to_dict(),
            'Sdd': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _mandate_status_route(service: SddService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    mandate_id = str(values.get('mandate_id') or '').strip()
    if not mandate_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_mandate'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_mandate_status(
            mandate_id=mandate_id, actor=userid, actor_type=actor_type, status=status,
        )
        return jsonify({
            'message': ok_message,
            'mandate': row.to_dict(),
            'Sdd': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_quote(service: SddService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        return jsonify({'quote': service.quote_fx(values.get('amount'))}), 200

    return _handle_errors(_run)


def handle_preview(service: SddService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    mandate_id = str(values.get('mandate_id') or '').strip()
    if not mandate_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_mandate'}), 400

    def _run():
        preview = service.preview(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            mandate_id=mandate_id,
            amount=values.get('amount'),
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            sequence=values.get('sequence'),
            collection_date=values.get('collection_date') or values.get('date'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({'preview': preview, 'Sdd': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_collect(service: SddService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    mandate_id = str(values.get('mandate_id') or '').strip()
    if not mandate_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_mandate'}), 400

    def _run():
        row, created = service.originate(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            mandate_id=mandate_id,
            amount=values.get('amount'),
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            sequence=values.get('sequence'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            collection_date=values.get('collection_date') or values.get('date'),
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({
            'message': 'SEPA Direct Debit originated' if created else 'Collection already posted',
            'collection': row.to_dict(),
            'Sdd': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: SddService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    collection_id = str(values.get('collection_id') or '').strip()
    if not collection_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_collection'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.cancel_collection(
            collection_id=collection_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Collection cancelled',
            'collection': row.to_dict(),
            'Sdd': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_refund(service: SddService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    collection_id = str(values.get('collection_id') or '').strip()
    if not collection_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_collection'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.request_refund(
            collection_id=collection_id,
            actor=userid,
            actor_type=actor_type,
            reason=values.get('reason') or 'MD06',
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'SEPA Direct Debit refunded',
            'collection': row.to_dict(),
            'Sdd': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_collection_route(service: SddService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    collection_id = str(values.get('collection_id') or '').strip()
    if not collection_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_collection'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'release':
            row = service.release_collection(collection_id=collection_id, actor=userid, actor_type=actor_type)
            message = 'Collection released'
        elif action == 'reject':
            row = service.reject_collection(
                collection_id=collection_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'MS03', note=values.get('note') or '',
            )
            message = 'Collection rejected'
        elif action == 'settle':
            row = service.settle_collection(collection_id=collection_id, actor=userid, actor_type=actor_type)
            message = 'Collection settled'
        elif action == 'return':
            row = service.return_collection(
                collection_id=collection_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'MS03', note=values.get('note') or '',
            )
            message = 'Collection returned'
        elif action == 'override':
            row = service.override_ofac(
                collection_id=collection_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'waive':
            row = service.waive_fee(collection_id=collection_id, actor=userid, actor_type=actor_type)
            message = 'Collection fee waived'
        else:
            raise SddError('invalid_status', 'Unknown SEPA Direct Debit action.')
        return jsonify({
            'message': message,
            'collection': row.to_dict(),
            'Sdd': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: SddService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Sdd': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_sdd_routes(app, service: SddService) -> None:
    @app.route('/listSdds', methods=['POST', 'GET'])
    def list_sdds_route():
        return handle_list(service)

    @app.route('/listSddDebtors', methods=['POST', 'GET'])
    def list_sdd_debtors_route():
        return handle_list(service)

    @app.route('/listSddMandates', methods=['POST', 'GET'])
    def list_sdd_mandates_route():
        return handle_list(service)

    @app.route('/registerSddCreditor', methods=['POST', 'GET'])
    def register_sdd_creditor_route():
        return handle_register_creditor(service)

    @app.route('/addSddDebtor', methods=['POST', 'GET'])
    def add_sdd_debtor_route():
        return handle_add_debtor(service)

    @app.route('/pauseSddDebtor', methods=['POST', 'GET'])
    def pause_sdd_debtor_route():
        return _debtor_status_route(service, DEBTOR_PAUSED, 'SEPA debtor paused')

    @app.route('/resumeSddDebtor', methods=['POST', 'GET'])
    def resume_sdd_debtor_route():
        return _debtor_status_route(service, DEBTOR_ACTIVE, 'SEPA debtor resumed')

    @app.route('/archiveSddDebtor', methods=['POST', 'GET'])
    def archive_sdd_debtor_route():
        return _debtor_status_route(service, DEBTOR_ARCHIVED, 'SEPA debtor archived')

    @app.route('/createSddMandate', methods=['POST', 'GET'])
    def create_sdd_mandate_route():
        return handle_create_mandate(service)

    @app.route('/activateSddMandate', methods=['POST', 'GET'])
    def activate_sdd_mandate_route():
        return _mandate_status_route(service, MANDATE_ACTIVE, 'SEPA mandate activated')

    @app.route('/suspendSddMandate', methods=['POST', 'GET'])
    def suspend_sdd_mandate_route():
        return _mandate_status_route(service, MANDATE_SUSPENDED, 'SEPA mandate suspended')

    @app.route('/cancelSddMandate', methods=['POST', 'GET'])
    def cancel_sdd_mandate_route():
        return _mandate_status_route(service, MANDATE_CANCELLED, 'SEPA mandate cancelled')

    @app.route('/quoteSddFx', methods=['POST', 'GET'])
    def quote_sdd_fx_route():
        return handle_quote(service)

    @app.route('/previewSdd', methods=['POST', 'GET'])
    def preview_sdd_route():
        return handle_preview(service)

    @app.route('/collectSdd', methods=['POST', 'GET'])
    def collect_sdd_route():
        return handle_collect(service)

    @app.route('/cancelSdd', methods=['POST', 'GET'])
    def cancel_sdd_route():
        return handle_cancel(service)

    @app.route('/requestSddRefund', methods=['POST', 'GET'])
    def request_sdd_refund_route():
        return handle_refund(service)

    @app.route('/releaseSdd', methods=['POST', 'GET'])
    def release_sdd_route():
        return _staff_collection_route(service, 'release')

    @app.route('/rejectSdd', methods=['POST', 'GET'])
    def reject_sdd_route():
        return _staff_collection_route(service, 'reject')

    @app.route('/settleSdd', methods=['POST', 'GET'])
    def settle_sdd_route():
        return _staff_collection_route(service, 'settle')

    @app.route('/returnSdd', methods=['POST', 'GET'])
    def return_sdd_route():
        return _staff_collection_route(service, 'return')

    @app.route('/overrideSddOfac', methods=['POST', 'GET'])
    def override_sdd_ofac_route():
        return _staff_collection_route(service, 'override')

    @app.route('/waiveSddFee', methods=['POST', 'GET'])
    def waive_sdd_fee_route():
        return _staff_collection_route(service, 'waive')

    @app.route('/runDueSdds', methods=['POST', 'GET'])
    def run_due_sdds_route():
        return handle_run_due(service)
