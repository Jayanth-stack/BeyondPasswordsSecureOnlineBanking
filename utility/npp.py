"""NPP / Osko / Direct Entry origination with PayID Confirmation of Payee.

Customers send AUD via the New Payments Platform (Osko overlay or full NPP)
to a PayID (email, +61 mobile, ABN) or BSB+account, or via BECS Direct
Entry which queues on Australian banking days. Independent of domestic
Fedwire (PR #73), TCH RTP RFI/RTR (PR #171/#173), FedNow RFI/RFR
(PR #165/#167), UPI (PR #145), PIX, SPEI, and ACH linking (PR #68).
Existing `/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay
unchanged. `Customers.debit_request` / `credit_request` still write
`debited` / `direct deposited` unless a remark is supplied here.

Foundations (reusable beyond this screen):
- BSB (6-digit Australian Bank State Branch, XXX-XXX display)
- PayID types: email, +61 mobile, ABN (mod-89), ACN (weighted check)
- Confirmation of Payee / PayID lookup (match, close_match, no_match,
  unavailable) — the name-check other rails (UK CoP, SEPA VoP, FedNow
  AIS) can reuse
- NPP End-to-End ID `NPP`+YYYYMMDD+seq (not IMAD / UETR / NPCI RRN)
- AUNPP clearing (USRTPP / FEDNOW / ACH / FEDWIRE / UPI rejected)
- AusClock in AEST (UTC+10): Osko/NPP 24/7; Direct Entry 09:00-17:00
  AEST, weekends + gazetted holidays queue
- NPP1 pipe compose/parse (XML/DOCTYPE rejected)
- Dual-control at AUD 10,000; Osko cap 10k / NPP cap 100k
- Complaint windows (Osko/NPP 24h customer / DE next banking day /
  90-day staff)

Reuses Fedwire OFAC screening, money parsing, fee helper, and account
directory from `utility.wire`. Stores are pluggable (memory for tests,
sqlite WAL for restart-safe default). Full PayID, BSB, account, ABN,
ACN, fingerprint, and E2E ID never appear unmasked in to_dict /
snapshots.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    DEBIT_NSF,
    DEBIT_OK,
    DEFAULT_WATCHLIST,
    EMPLOYEE_ROLES,
    MONEY_QUANTUM,
    AccountError,
    AmountError,
    ScreenResult,
    account_types_from_customer_payload,
    compute_fee,
    last4,
    money_str,
    normalize_account,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

BENE_ACTIVE = 'active'
BENE_PAUSED = 'paused'
BENE_ARCHIVED = 'archived'
BENE_STATUSES = frozenset({BENE_ACTIVE, BENE_PAUSED, BENE_ARCHIVED})
OPEN_BENE = frozenset({BENE_ACTIVE, BENE_PAUSED})
NPP_HELD = 'held'
NPP_QUEUED = 'queued'
NPP_PENDING = 'pending_release'
NPP_COMPLETED = 'completed'
NPP_REJECTED = 'rejected'
NPP_CANCELLED = 'cancelled'
NPP_RECALLED = 'recalled'
NPP_NSF = 'nsf'
NPP_FAILED = 'failed'
NPP_STATUSES = frozenset({
    NPP_HELD, NPP_QUEUED, NPP_PENDING, NPP_COMPLETED, NPP_REJECTED,
    NPP_CANCELLED, NPP_RECALLED, NPP_NSF, NPP_FAILED,
})
OPEN_NPP = frozenset({NPP_HELD, NPP_QUEUED, NPP_PENDING})
CANCELABLE = frozenset({NPP_HELD, NPP_QUEUED, NPP_PENDING})
FEE_NONE = 'none'
FEE_COLLECTED = 'collected'
FEE_WAIVED = 'waived'
FEE_NSF = 'nsf'
PURPOSES = frozenset({'family', 'goods', 'payroll', 'tax', 'loan', 'rent', 'other'})
PURPOSE_ALIASES = {
    'personal': 'family', 'gift': 'family', 'support': 'family',
    'invoice': 'goods', 'purchase': 'goods', 'vendor': 'goods',
    'salary': 'payroll', 'wage': 'payroll',
    'ato': 'tax', 'gst': 'tax', 'taxes': 'tax',
    'mortgage': 'loan', 'housing': 'rent',
}
RAILS = frozenset({'osko', 'npp', 'de'})
RAIL_ALIASES = {
    'osko_payment': 'osko', 'fast': 'osko', 'overlay': 'osko',
    'new_payments_platform': 'npp', 'aunpp': 'npp', 'instant': 'npp',
    'becs': 'de', 'direct_entry': 'de', 'bulk': 'de',
}
PAYID_TYPES = frozenset({'email', 'phone', 'abn', 'acn', 'orgid'})
PAYID_ALIASES = {
    'mail': 'email', 'e-mail': 'email',
    'mobile': 'phone', 'msisdn': 'phone', 'tel': 'phone',
    'australian_business_number': 'abn',
    'company': 'acn', 'australian_company_number': 'acn',
    'org': 'orgid', 'organisation': 'orgid', 'organization': 'orgid',
}
COP_MATCH = 'match'
COP_CLOSE = 'close_match'
COP_NONE = 'no_match'
COP_UNAVAILABLE = 'unavailable'
COP_OUTCOMES = frozenset({COP_MATCH, COP_CLOSE, COP_NONE, COP_UNAVAILABLE})
CLEARING_AUNPP = 'AUNPP'
REJECTED_CLEARING = frozenset({
    'USRTPP', 'FEDNOW', 'FEDWIRE', 'ACH', 'UPI', 'CHIPS', 'SWIFT', 'PIX', 'SPEI',
})
DEFAULT_BANKS = {
    '01': 'ANZ',
    '03': 'WESTPAC',
    '06': 'COMMONWEALTH BANK',
    '08': 'NAB',
    '12': 'BANK OF QUEENSLAND',
    '73': 'ST GEORGE',
    '80': 'CUSCAL',
    '90': 'KONOHA BANK',
}
DEFAULT_PAYID_DIRECTORY = {
    'email:ada@example.com': 'ADA LOVELACE',
    'phone:+61412345678': 'ADA LOVELACE',
    'abn:51824753556': 'ADA LOVELACE',
}
EMAIL_RE = re.compile(r'^[a-z0-9][a-z0-9._%+-]{0,63}@[a-z0-9.-]+\.[a-z]{2,24}$')
ORGID_RE = re.compile(r'^[A-Z0-9][A-Z0-9._-]{3,31}$')
ABN_WEIGHTS = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)
ACN_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 1)
NPP1_VERSION = '1'
NPP1_FIELDS = 14
DEFAULT_STORE_PATH = 'SystemLogs/npp.sqlite'
DEFAULT_SOURCE_BSB = '062000'
CUSTOMER_NPP_RETURN_SECONDS = 24 * 60 * 60
STAFF_COMPLAINT_DAYS = 90
AEST_OFFSET_MINUTES = 600
BLOCKED_E2E_PREFIXES = (
    'IMAD', 'UETR', 'RFI', 'RFR', 'RTR', 'TKN', 'VAU', 'RRN', 'STAN', 'ARN',
)


class NppError(ValueError):
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
        raise NppError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise NppError('invalid_name', 'Beneficiary legal name must be 2-80 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise NppError('invalid_purpose', 'Unknown NPP purpose.')
    return text


def normalize_currency(value: Any, *, default: str = 'AUD') -> str:
    text = str(value or default).strip().upper()
    if text not in {'AUD', 'A$', 'AUD.'}:
        raise NppError('invalid_currency', 'NPP / Osko / Direct Entry is AUD only.')
    return 'AUD'


def normalize_rail(value: Any, *, default: str = 'osko') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text not in RAILS:
        raise NppError('invalid_rail', 'Rail must be osko, npp, or de.')
    return text


def normalize_payid_type(value: Any, *, default: str = '') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PAYID_ALIASES.get(text, text)
    if not text:
        return ''
    if text not in PAYID_TYPES:
        raise NppError('invalid_payid', 'PayID type must be email, phone, abn, acn, or orgid.')
    return text


def normalize_bsb(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise NppError('invalid_bsb', 'BSB is required.')
        return ''
    if len(digits) != 6 or digits == '000000':
        raise NppError('invalid_bsb', 'BSB must be six digits.')
    return digits


def format_bsb(value: Any) -> str:
    digits = normalize_bsb(value, required=False)
    if len(digits) != 6:
        return ''
    return '%s-%s' % (digits[:3], digits[3:])


def mask_bsb(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 6:
        return '***-***'
    return digits[:3] + '-***'


def bank_code_from_bsb(value: Any) -> str:
    try:
        return normalize_bsb(value)[:2]
    except NppError:
        return ''


def bank_name(value: Any, *, directory: Optional[Dict[str, str]] = None) -> str:
    code = bank_code_from_bsb(value)
    mapping = directory if directory is not None else DEFAULT_BANKS
    return mapping.get(code, '')


def normalize_external_account(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise NppError('invalid_account', 'Beneficiary account is required.')
        return ''
    if not (4 <= len(digits) <= 9):
        raise NppError('invalid_account', 'Australian account must be 4-9 digits.')
    return digits


def mask_account(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return ('*' * max(0, len(digits) - 4)) + digits[-4:]


def abn_check_ok(digits: str) -> bool:
    if len(digits) != 11 or not digits.isdigit() or digits == '0' * 11:
        return False
    body = [int(digits[0]) - 1] + [int(ch) for ch in digits[1:]]
    total = sum(n * w for n, w in zip(body, ABN_WEIGHTS))
    return total % 89 == 0


def normalize_abn(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise NppError('invalid_abn', 'ABN is required.')
        return ''
    if not abn_check_ok(digits):
        raise NppError('invalid_abn', 'ABN failed the mod-89 checksum.')
    return digits


def mask_abn(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '***********'
    return digits[:2] + ('*' * 5) + digits[-4:]


def acn_check_digit(body: str) -> str:
    if len(body) != 8 or not body.isdigit():
        raise NppError('invalid_acn', 'ACN body must be 8 digits.')
    total = sum(int(ch) * w for ch, w in zip(body, ACN_WEIGHTS))
    return str((10 - (total % 10)) % 10)


def acn_check_ok(digits: str) -> bool:
    if len(digits) != 9 or not digits.isdigit():
        return False
    try:
        return acn_check_digit(digits[:8]) == digits[8]
    except NppError:
        return False


def compose_acn(body: Any) -> str:
    digits = ''.join(ch for ch in str(body or '') if ch.isdigit())
    if len(digits) != 8:
        raise NppError('invalid_acn', 'ACN body must be 8 digits.')
    return digits + acn_check_digit(digits)


def normalize_acn(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise NppError('invalid_acn', 'ACN is required.')
        return ''
    if not acn_check_ok(digits):
        raise NppError('invalid_acn', 'ACN failed checksum.')
    return digits


def mask_acn(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '*********'
    return digits[:3] + '***' + digits[-3:]


def normalize_phone(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise NppError('invalid_phone', 'Australian mobile is required.')
        return ''
    if digits.startswith('61') and len(digits) == 11:
        national = '0' + digits[2:]
    elif digits.startswith('0') and len(digits) == 10:
        national = digits
    elif len(digits) == 9 and digits[0] == '4':
        national = '0' + digits
    else:
        raise NppError('invalid_phone', 'Australian mobile must be 04xx xxx xxx.')
    if len(national) != 10 or not national.startswith('04'):
        raise NppError('invalid_phone', 'Australian mobile must be 04xx xxx xxx.')
    return '+61' + national[1:]


def mask_phone(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '+61-****-***'
    national = digits[-9:] if len(digits) >= 9 else digits
    return '+61-4**-***' + national[-3:]


def normalize_email(value: Any, *, required: bool = False) -> str:
    text = str(value or '').strip().lower()
    if not text:
        if required:
            raise NppError('invalid_payid', 'PayID email is required.')
        return ''
    if not EMAIL_RE.match(text):
        raise NppError('invalid_payid', 'PayID email is invalid.')
    return text


def mask_email(value: Any) -> str:
    text = str(value or '').strip().lower()
    if '@' not in text:
        return '***@***'
    local, _, domain = text.partition('@')
    if len(local) <= 2:
        hidden = '*' * max(1, len(local))
    else:
        hidden = local[0] + ('*' * (len(local) - 2)) + local[-1]
    return '%s@%s' % (hidden, domain)


def normalize_orgid(value: Any, *, required: bool = False) -> str:
    text = re.sub(r'\s+', '', str(value or '')).upper()
    if not text:
        if required:
            raise NppError('invalid_payid', 'Organisation ID is required.')
        return ''
    if not ORGID_RE.match(text):
        raise NppError('invalid_payid', 'Organisation ID is invalid.')
    return text


def mask_orgid(value: Any) -> str:
    text = str(value or '').upper()
    if len(text) < 4:
        return '****'
    return text[:2] + ('*' * (len(text) - 4)) + text[-2:]


def infer_payid_type(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    if '@' in text:
        return 'email'
    digits = ''.join(ch for ch in text if ch.isdigit())
    if digits.startswith('61') or text.startswith('+61') or text.startswith('04'):
        return 'phone'
    if len(digits) == 11:
        return 'abn'
    if len(digits) == 9 and digits.isdigit():
        return 'acn'
    return 'orgid'


def normalize_payid(value: Any, *, payid_type: Any = '', required: bool = True) -> Tuple[str, str]:
    raw = str(value or '').strip()
    kind = normalize_payid_type(payid_type) or infer_payid_type(raw)
    if not raw:
        if required:
            raise NppError('invalid_payid', 'PayID is required.')
        return '', kind
    if not kind:
        kind = infer_payid_type(raw)
    if kind == 'email':
        return normalize_email(raw, required=True), kind
    if kind == 'phone':
        return normalize_phone(raw, required=True), kind
    if kind == 'abn':
        return normalize_abn(raw, required=True), kind
    if kind == 'acn':
        return normalize_acn(raw, required=True), kind
    if kind == 'orgid':
        return normalize_orgid(raw, required=True), kind
    raise NppError('invalid_payid', 'PayID type is unknown.')


def mask_payid(value: Any, payid_type: str) -> str:
    if payid_type == 'email':
        return mask_email(value)
    if payid_type == 'phone':
        return mask_phone(value)
    if payid_type == 'abn':
        return mask_abn(value)
    if payid_type == 'acn':
        return mask_acn(value)
    if payid_type == 'orgid':
        return mask_orgid(value)
    return '****'


def payid_key(payid_type: str, payid: str) -> str:
    return '%s:%s' % (payid_type, payid)


def destination_fingerprint(
    rail: str, payid_type: str, payid: str, bsb: str, account_number: str,
) -> str:
    material = '%s|%s|%s|%s|%s' % (
        rail, payid_type or '', payid or '', bsb or '', account_number or '',
    )
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def compose_e2e(cycle_date: str, sequence: int) -> str:
    """NPP End-to-End ID: NPP + YYYYMMDD + 6-digit sequence."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise NppError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise NppError('invalid_reference', 'E2E sequence out of range.')
    return 'NPP%s%06d' % (day, seq)


def normalize_e2e(value: Any) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        raise NppError('invalid_reference', 'NPP End-to-End ID is required.')
    if any(text.startswith(prefix) for prefix in BLOCKED_E2E_PREFIXES):
        raise NppError('invalid_reference', 'End-to-End ID must not use another rail prefix.')
    if len(text) == 12 and text.isdigit():
        raise NppError('invalid_reference', 'NPCI RRN is not an NPP End-to-End ID.')
    if len(text) == 22 and text[:8].isdigit():
        raise NppError('invalid_reference', 'Fedwire IMAD is not an NPP End-to-End ID.')
    if not text.startswith('NPP') or len(text) != 17 or not text[3:].isdigit():
        raise NppError('invalid_reference', 'NPP End-to-End ID must be NPP+YYYYMMDD+seq.')
    return text


def mask_e2e(value: Any) -> str:
    text = str(value or '')
    if len(text) < 6:
        return '********'
    return ('*' * (len(text) - 4)) + text[-4:]


def compose_amount_field(amount: Decimal) -> str:
    quantized = amount.quantize(MONEY_QUANTUM)
    if quantized < 0 or quantized > Decimal('999999999.99'):
        raise NppError('invalid_amount', 'Amount cannot be encoded in NPP1.')
    return money_str(quantized)


def parse_amount_field(value: Any) -> Decimal:
    try:
        return parse_money(value)
    except AmountError as exc:
        raise NppError('invalid_amount', 'NPP1 amount field is invalid.') from exc


def _reject_markup(text: str) -> None:
    lowered = text.lower()
    if '<xml' in lowered or '<!doctype' in lowered or '<?xml' in lowered:
        raise NppError('invalid_file', 'XML/DOCTYPE NPP1 files are rejected.')


def compose_npp1(
    *,
    e2e: str,
    rail: str,
    payid_type: str,
    payid: str,
    bsb: str,
    account_number: str,
    abn: str,
    amount: Decimal,
    purpose: str,
    status: str,
    mask: bool = True,
) -> str:
    clearing = CLEARING_AUNPP
    return '|'.join((
        'NPP1',
        NPP1_VERSION,
        mask_e2e(e2e) if mask else normalize_e2e(e2e),
        normalize_rail(rail),
        payid_type or '',
        mask_payid(payid, payid_type) if mask and payid else (payid or ''),
        mask_bsb(bsb) if mask and bsb else (normalize_bsb(bsb, required=False) if bsb else ''),
        mask_account(account_number) if mask and account_number else (account_number or ''),
        mask_abn(abn) if mask and abn else (abn or ''),
        compose_amount_field(amount),
        normalize_purpose(purpose),
        status,
        'AUD',
        clearing,
    ))


def parse_npp1(text: Any) -> Dict[str, Any]:
    raw = str(text or '').strip()
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != NPP1_FIELDS or parts[0] != 'NPP1' or parts[1] != NPP1_VERSION:
        raise NppError('invalid_file', 'NPP1 record is malformed.')
    clearing = parts[13].strip().upper()
    if clearing in REJECTED_CLEARING:
        raise NppError('invalid_file', 'NPP1 clearing must be AUNPP.')
    if clearing != CLEARING_AUNPP:
        raise NppError('invalid_file', 'NPP1 clearing must be AUNPP.')
    return {
        'e2e': normalize_e2e(parts[2]),
        'rail': normalize_rail(parts[3]),
        'payid_type': normalize_payid_type(parts[4]) if parts[4] else '',
        'payid': parts[5],
        'bsb': parts[6],
        'account_number': parts[7],
        'abn': parts[8],
        'amount': money_str(parse_amount_field(parts[9])),
        'purpose': normalize_purpose(parts[10]),
        'status': parts[11],
        'currency': normalize_currency(parts[12]),
        'clearing': clearing,
        'settlement_method': normalize_rail(parts[3]),
    }


def split_npp1_file(text: Any) -> List[str]:
    raw = str(text or '')
    _reject_markup(raw)
    records = [line.strip() for line in raw.replace('\r\n', '\n').split('\n') if line.strip()]
    if not records:
        raise NppError('invalid_file', 'NPP1 file is empty.')
    return records


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
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month = (h + el - 7 * m + 114) // 31
    day = ((h + el - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return date(year, month, 1 + offset + (n - 1) * 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def aus_holidays(year: int) -> set:
    """National Australian banking holidays (weekday-observed where applicable)."""
    easter = easter_gregorian(year)
    return {
        _observed(date(year, 1, 1)),
        _observed(date(year, 1, 26)),
        easter - timedelta(days=2),
        easter + timedelta(days=1),
        _observed(date(year, 4, 25)),
        _nth_weekday(year, 6, 0, 2),
        _observed(date(year, 12, 25)),
        _observed(date(year, 12, 26)),
    }


def _name_tokens(value: str) -> set:
    return {token for token in value.split() if token}


@dataclass(frozen=True)
class PayIdResolution:
    outcome: str
    payid_type: str
    payid_masked: str
    registered_name: str = ''
    score: int = 0

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            'outcome': self.outcome,
            'payid_type': self.payid_type,
            'payid_masked': self.payid_masked,
            'name_match': self.outcome == COP_MATCH,
            'score': self.score,
        }
        if self.outcome in {COP_MATCH, COP_CLOSE} and self.registered_name:
            payload['registered_name'] = self.registered_name
        return payload


def resolve_payid(
    payid: Any,
    legal_name: Any = '',
    *,
    payid_type: Any = '',
    directory: Optional[Dict[str, str]] = None,
) -> PayIdResolution:
    """Confirmation of Payee against a PayID directory.

    MATCH / CLOSE_MATCH return the registered name so the sender can
    confirm. NO_MATCH does not, to avoid probing the directory.
    UNAVAILABLE means the PayID is not in this bank's participating set.
    """
    value, kind = normalize_payid(payid, payid_type=payid_type, required=True)
    mapping = directory if directory is not None else DEFAULT_PAYID_DIRECTORY
    registered = mapping.get(payid_key(kind, value), '')
    masked = mask_payid(value, kind)
    if not registered:
        return PayIdResolution(COP_UNAVAILABLE, kind, masked, '', 0)
    wanted = ' '.join(str(legal_name or '').upper().split())
    registered_clean = ' '.join(registered.upper().split())
    if wanted and wanted == registered_clean:
        return PayIdResolution(COP_MATCH, kind, masked, registered_clean, 100)
    wanted_tokens = _name_tokens(wanted)
    registered_tokens = _name_tokens(registered_clean)
    if wanted and wanted_tokens and wanted_tokens <= registered_tokens:
        return PayIdResolution(COP_CLOSE, kind, masked, registered_clean, 70)
    if wanted and registered_tokens and registered_tokens <= wanted_tokens:
        return PayIdResolution(COP_CLOSE, kind, masked, registered_clean, 70)
    if wanted:
        return PayIdResolution(COP_NONE, kind, masked, '', 0)
    return PayIdResolution(COP_MATCH, kind, masked, registered_clean, 100)


class AusClock:
    """Australian banking clock in AEST. Osko and NPP stay 24/7."""

    def __init__(
        self,
        *,
        open_hour: int = 9,
        cutoff_hour: int = 17,
        cutoff_minute: int = 0,
        tz_offset_minutes: int = AEST_OFFSET_MINUTES,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.open_hour = int(open_hour)
        self.cutoff_hour = int(cutoff_hour)
        self.cutoff_minute = int(cutoff_minute)
        self.tz_offset_minutes = int(tz_offset_minutes)
        self.tz = timezone(timedelta(minutes=self.tz_offset_minutes))
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
        return day in aus_holidays(day.year) or day in self.extra_holidays

    def is_business_day(self, day: date) -> bool:
        return not self.is_weekend(day) and not self.is_holiday(day)

    def is_after_cutoff(self, ts: float) -> bool:
        local = self.local_dt(ts)
        if (local.hour, local.minute) >= (self.cutoff_hour, self.cutoff_minute):
            return True
        if local.hour < self.open_hour:
            return True
        return False

    def is_session(self, ts: float) -> bool:
        local = self.local_dt(ts)
        return self.is_business_day(local.date()) and not self.is_after_cutoff(ts)

    def next_business_day(self, day: date) -> date:
        cursor = day + timedelta(days=1)
        while not self.is_business_day(cursor):
            cursor += timedelta(days=1)
        return cursor

    def value_date(self, ts: float) -> date:
        local = self.local_dt(ts)
        day = local.date()
        if self.is_session(ts):
            return day
        if self.is_business_day(day) and local.hour < self.open_hour:
            return day
        return self.next_business_day(day)

    def cycle_date(self, ts: float) -> str:
        return self.local_dt(ts).strftime('%Y%m%d')

    def session_close_ts(self, day: date) -> float:
        local = datetime(
            day.year, day.month, day.day,
            self.cutoff_hour, self.cutoff_minute, tzinfo=self.tz,
        )
        return local.timestamp()

    def snapshot(self, ts: float) -> Dict[str, Any]:
        local = self.local_dt(ts)
        day = local.date()
        session = self.is_session(ts)
        return {
            'local_date': day.isoformat(),
            'local_time': local.strftime('%H:%M'),
            'timezone': 'AEST',
            'open': '%02d:00' % self.open_hour,
            'cutoff': '%02d:%02d' % (self.cutoff_hour, self.cutoff_minute),
            'session': session,
            'after_cutoff': not session,
            'weekend': self.is_weekend(day),
            'holiday': self.is_holiday(day),
            'business_day': self.is_business_day(day),
            'value_date': self.value_date(ts).isoformat(),
            'cycle_date': self.cycle_date(ts),
            'settlement_method': 'npp',
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
class NppPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    allow_credit: bool = False
    max_beneficiaries: int = 12
    max_payments: int = 120
    min_amount: Decimal = Decimal('1.00')
    osko_max: Decimal = Decimal('10000.00')
    npp_max: Decimal = Decimal('100000.00')
    de_max: Decimal = Decimal('100000000.00')
    outbound_fee: Decimal = Decimal('0.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    tz_offset_minutes: int = AEST_OFFSET_MINUTES
    open_hour: int = 9
    cutoff_hour: int = 17
    source_bsb: str = DEFAULT_SOURCE_BSB
    customer_npp_return_seconds: int = CUSTOMER_NPP_RETURN_SECONDS
    staff_complaint_days: int = STAFF_COMPLAINT_DAYS
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()
    banks: Dict[str, str] = None  # type: ignore[assignment]
    payid_directory: Dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.banks is None:
            self.banks = dict(DEFAULT_BANKS)
        if self.payid_directory is None:
            self.payid_directory = dict(DEFAULT_PAYID_DIRECTORY)

    @classmethod
    def from_env(cls) -> 'NppPolicy':
        extra = _env_list('NPP_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        extra_banks = _env_list('NPP_BANKS')
        directory = dict(DEFAULT_BANKS)
        for item in extra_banks:
            if ':' in item:
                pid, _, label = item.partition(':')
                directory[pid.strip()[:2]] = label.strip() or directory.get(pid.strip()[:2], '')
        payids = dict(DEFAULT_PAYID_DIRECTORY)
        for item in _env_list('NPP_PAYIDS'):
            if '=' in item:
                key, _, label = item.partition('=')
                payids[key.strip().lower()] = label.strip().upper()
        source = os.environ.get('NPP_SOURCE_BSB', DEFAULT_SOURCE_BSB)
        try:
            source_bsb = normalize_bsb(source)
        except NppError:
            source_bsb = DEFAULT_SOURCE_BSB
        return cls(
            enabled=_env_bool('NPP_ENABLED', True),
            customer_manage=_env_bool('NPP_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('NPP_CUSTOMER_SEND', True),
            allow_credit=_env_bool('NPP_ALLOW_CREDIT', False),
            max_beneficiaries=max(1, _env_int('NPP_MAX_BENEFICIARIES', 12)),
            max_payments=max(1, _env_int('NPP_MAX_PAYMENTS', 120)),
            min_amount=_env_money('NPP_MIN_AMOUNT', '1.00'),
            osko_max=_env_money('NPP_OSKO_MAX', '10000.00'),
            npp_max=_env_money('NPP_MAX', '100000.00'),
            de_max=_env_money('NPP_DE_MAX', '100000000.00'),
            outbound_fee=_env_money('NPP_FEE', '0.00'),
            dual_control_threshold=_env_money('NPP_DUAL_CONTROL', '10000.00'),
            tz_offset_minutes=_env_int('NPP_TZ_OFFSET_MINUTES', AEST_OFFSET_MINUTES),
            open_hour=max(0, min(23, _env_int('NPP_OPEN_HOUR', 9))),
            cutoff_hour=max(0, min(23, _env_int('NPP_CUTOFF_HOUR', 17))),
            source_bsb=source_bsb,
            customer_npp_return_seconds=max(60, _env_int(
                'NPP_RETURN_SECONDS', CUSTOMER_NPP_RETURN_SECONDS,
            )),
            staff_complaint_days=max(1, _env_int('NPP_COMPLAINT_DAYS', STAFF_COMPLAINT_DAYS)),
            watchlist=watch,
            extra_holidays=_env_list('NPP_HOLIDAYS'),
            banks=directory,
            payid_directory=payids,
        )


@dataclass
class NppBeneficiary:
    beneficiary_id: str
    userid: str
    nickname: str
    legal_name: str
    rail: str
    payid_type: str
    payid: str
    bsb: str
    account_number: str
    abn: str
    acn: str
    fingerprint: str
    default_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float
    bank_label: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'beneficiary_id': self.beneficiary_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'rail': self.rail,
            'payid_type': self.payid_type,
            'payid_masked': mask_payid(self.payid, self.payid_type) if self.payid else '',
            'bsb_masked': mask_bsb(self.bsb) if self.bsb else '',
            'account_last4': last4(self.account_number) if self.account_number else '',
            'abn_masked': mask_abn(self.abn) if self.abn else '',
            'acn_masked': mask_acn(self.acn) if self.acn else '',
            'bank_code': bank_code_from_bsb(self.bsb) if self.bsb else '',
            'bank_label': self.bank_label,
            'default_account': self.default_account,
            'internal_last4': last4(self.default_account),
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == BENE_ACTIVE,
            'paused': self.status == BENE_PAUSED,
            'archived': self.status == BENE_ARCHIVED,
        }


@dataclass
class NppPayment:
    payment_id: str
    trace_id: str
    beneficiary_id: str
    userid: str
    internal_account: str
    amount: str
    fee: str
    fee_status: str
    nickname: str
    legal_name: str
    rail: str
    payid_type: str
    payid_masked: str
    bsb_masked: str
    account_last4: str
    abn_masked: str
    acn_masked: str
    bank_code: str
    purpose: str
    memo: str
    status: str
    e2e: str
    value_date: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    payid_hit: int
    payid_outcome: str
    created_at: float
    updated_at: float
    completed_at: float = 0.0
    recalled_at: float = 0.0
    note: str = ''
    reason: str = ''
    settlement_method: str = 'osko'

    def to_dict(self) -> Dict[str, Any]:
        return {
            'payment_id': self.payment_id,
            'trace_id': self.trace_id,
            'beneficiary_id': self.beneficiary_id,
            'userid': self.userid,
            'internal_account_last4': last4(self.internal_account),
            'amount': self.amount,
            'fee': self.fee,
            'fee_status': self.fee_status,
            'currency': 'AUD',
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'rail': self.rail,
            'payid_type': self.payid_type,
            'payid_masked': self.payid_masked,
            'bsb_masked': self.bsb_masked,
            'account_last4': self.account_last4,
            'abn_masked': self.abn_masked,
            'acn_masked': self.acn_masked,
            'bank_code': self.bank_code,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'e2e_masked': mask_e2e(self.e2e) if self.e2e else '',
            'value_date': self.value_date,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'payid_hit': bool(self.payid_hit),
            'payid_outcome': self.payid_outcome,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'completed_at': self.completed_at,
            'recalled_at': self.recalled_at,
            'note': self.note,
            'reason': self.reason,
            'settlement_method': self.settlement_method,
            'cancelable': self.status in CANCELABLE,
            'recallable': self.status == NPP_COMPLETED,
        }


def _clone_bene(row: NppBeneficiary) -> NppBeneficiary:
    return NppBeneficiary(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_pay(row: NppPayment) -> NppPayment:
    return NppPayment(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _row_get(row: Any, key: str, default: Any = '') -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _bene_from_row(row: Any) -> NppBeneficiary:
    return NppBeneficiary(
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        rail=row['rail'],
        payid_type=_row_get(row, 'payid_type', ''),
        payid=_row_get(row, 'payid', ''),
        bsb=_row_get(row, 'bsb', ''),
        account_number=_row_get(row, 'account_number', ''),
        abn=_row_get(row, 'abn', ''),
        acn=_row_get(row, 'acn', ''),
        fingerprint=row['fingerprint'],
        default_account=row['default_account'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        bank_label=_row_get(row, 'bank_label', '') or '',
    )


def _pay_from_row(row: Any) -> NppPayment:
    return NppPayment(
        payment_id=row['payment_id'],
        trace_id=row['trace_id'],
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        amount=row['amount'],
        fee=row['fee'],
        fee_status=row['fee_status'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        rail=row['rail'],
        payid_type=_row_get(row, 'payid_type', ''),
        payid_masked=_row_get(row, 'payid_masked', ''),
        bsb_masked=_row_get(row, 'bsb_masked', ''),
        account_last4=_row_get(row, 'account_last4', ''),
        abn_masked=_row_get(row, 'abn_masked', ''),
        acn_masked=_row_get(row, 'acn_masked', ''),
        bank_code=_row_get(row, 'bank_code', ''),
        purpose=row['purpose'],
        memo=row['memo'] or '',
        status=row['status'],
        e2e=_row_get(row, 'e2e', ''),
        value_date=row['value_date'],
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        payid_hit=int(_row_get(row, 'payid_hit', 0) or 0),
        payid_outcome=_row_get(row, 'payid_outcome', '') or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        completed_at=float(row['completed_at'] or 0),
        recalled_at=float(row['recalled_at'] or 0),
        note=row['note'] or '',
        reason=row['reason'] or '',
        settlement_method=_row_get(row, 'settlement_method', '') or row['rail'],
    )


class MemoryNppStore:
    def __init__(self) -> None:
        self._benes: Dict[str, NppBeneficiary] = {}
        self._pays: Dict[str, NppPayment] = {}
        self._lock = threading.Lock()

    def put_beneficiary(self, row: NppBeneficiary) -> None:
        self._benes[row.beneficiary_id] = _clone_bene(row)

    def get_beneficiary(self, beneficiary_id: str) -> Optional[NppBeneficiary]:
        row = self._benes.get(beneficiary_id)
        return _clone_bene(row) if row else None

    def update_beneficiary(self, row: NppBeneficiary) -> None:
        self._benes[row.beneficiary_id] = _clone_bene(row)

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[NppBeneficiary]:
        rows = list(self._benes.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != BENE_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return [_clone_bene(row) for row in rows]

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[NppBeneficiary]:
        wanted = nickname.strip().lower()
        for row in self._benes.values():
            if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_BENE:
                return _clone_bene(row)
        return None

    def find_beneficiary_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[NppBeneficiary]:
        for row in self._benes.values():
            if row.userid == userid and row.fingerprint == fingerprint and row.status in OPEN_BENE:
                return _clone_bene(row)
        return None

    def put_payment(self, row: NppPayment) -> NppPayment:
        with self._lock:
            existing = self.get_payment_by_trace(row.trace_id)
            if existing is not None:
                return existing
            self._pays[row.payment_id] = _clone_pay(row)
            return _clone_pay(row)

    def update_payment(self, row: NppPayment) -> None:
        self._pays[row.payment_id] = _clone_pay(row)

    def get_payment(self, payment_id: str) -> Optional[NppPayment]:
        row = self._pays.get(payment_id)
        return _clone_pay(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[NppPayment]:
        for row in self._pays.values():
            if row.trace_id == trace_id:
                return _clone_pay(row)
        return None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[NppPayment]:
        rows = list(self._pays.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if beneficiary_id is not None:
            rows = [row for row in rows if row.beneficiary_id == beneficiary_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return [_clone_pay(row) for row in rows]

    def next_sequence(self, cycle_date: str) -> int:
        used = [row.e2e for row in self._pays.values() if row.e2e and row.value_date == cycle_date]
        return len(used) + 1


class SqliteNppStore:
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
                    rail TEXT NOT NULL,
                    payid_type TEXT NOT NULL DEFAULT '',
                    payid TEXT NOT NULL DEFAULT '',
                    bsb TEXT NOT NULL DEFAULT '',
                    account_number TEXT NOT NULL DEFAULT '',
                    abn TEXT NOT NULL DEFAULT '',
                    acn TEXT NOT NULL DEFAULT '',
                    fingerprint TEXT NOT NULL,
                    default_account TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    bank_label TEXT NOT NULL DEFAULT ''
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
                    amount TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    fee_status TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    rail TEXT NOT NULL,
                    payid_type TEXT NOT NULL DEFAULT '',
                    payid_masked TEXT NOT NULL DEFAULT '',
                    bsb_masked TEXT NOT NULL DEFAULT '',
                    account_last4 TEXT NOT NULL DEFAULT '',
                    abn_masked TEXT NOT NULL DEFAULT '',
                    acn_masked TEXT NOT NULL DEFAULT '',
                    bank_code TEXT NOT NULL DEFAULT '',
                    purpose TEXT NOT NULL,
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    e2e TEXT NOT NULL DEFAULT '',
                    value_date TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    payid_hit INTEGER NOT NULL DEFAULT 0,
                    payid_outcome TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL NOT NULL DEFAULT 0,
                    recalled_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    settlement_method TEXT NOT NULL DEFAULT 'osko'
                )
                """
            )
            conn.commit()

    def put_beneficiary(self, row: NppBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO beneficiaries (
                    beneficiary_id, userid, nickname, legal_name, rail, payid_type,
                    payid, bsb, account_number, abn, acn, fingerprint, default_account,
                    status, actor, created_at, updated_at, bank_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.beneficiary_id, row.userid, row.nickname, row.legal_name,
                    row.rail, row.payid_type, row.payid, row.bsb, row.account_number,
                    row.abn, row.acn, row.fingerprint, row.default_account,
                    row.status, row.actor, row.created_at, row.updated_at, row.bank_label,
                ),
            )
            conn.commit()

    def get_beneficiary(self, beneficiary_id: str) -> Optional[NppBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM beneficiaries WHERE beneficiary_id = ?', (beneficiary_id,),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def update_beneficiary(self, row: NppBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE beneficiaries SET nickname=?, legal_name=?, rail=?, payid_type=?,
                    payid=?, bsb=?, account_number=?, abn=?, acn=?, fingerprint=?,
                    default_account=?, status=?, actor=?, updated_at=?, bank_label=?
                WHERE beneficiary_id=?
                """,
                (
                    row.nickname, row.legal_name, row.rail, row.payid_type, row.payid,
                    row.bsb, row.account_number, row.abn, row.acn, row.fingerprint,
                    row.default_account, row.status, row.actor, row.updated_at,
                    row.bank_label, row.beneficiary_id,
                ),
            )
            conn.commit()

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[NppBeneficiary]:
        sql = 'SELECT * FROM beneficiaries'
        args: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            args.append(userid)
        if not include_archived:
            clauses.append("status != 'archived'")
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_bene_from_row(row) for row in rows]

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[NppBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM beneficiaries
                WHERE userid = ? AND lower(nickname) = lower(?) AND status IN ('active', 'paused')
                """,
                (userid, nickname),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def find_beneficiary_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[NppBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM beneficiaries
                WHERE userid = ? AND fingerprint = ? AND status IN ('active', 'paused')
                """,
                (userid, fingerprint),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def put_payment(self, row: NppPayment) -> NppPayment:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM payments WHERE trace_id = ?', (row.trace_id,),
            ).fetchone()
            if existing is not None:
                return _pay_from_row(existing)
            conn.execute(
                """
                INSERT INTO payments (
                    payment_id, trace_id, beneficiary_id, userid, internal_account,
                    amount, fee, fee_status, nickname, legal_name, rail, payid_type,
                    payid_masked, bsb_masked, account_last4, abn_masked, acn_masked,
                    bank_code, purpose, memo, status, e2e, value_date, actor, releaser,
                    ofac_hit, ofac_match, payid_hit, payid_outcome, created_at,
                    updated_at, completed_at, recalled_at, note, reason, settlement_method
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.payment_id, row.trace_id, row.beneficiary_id, row.userid,
                    row.internal_account, row.amount, row.fee, row.fee_status,
                    row.nickname, row.legal_name, row.rail, row.payid_type,
                    row.payid_masked, row.bsb_masked, row.account_last4,
                    row.abn_masked, row.acn_masked, row.bank_code, row.purpose,
                    row.memo, row.status, row.e2e, row.value_date, row.actor,
                    row.releaser, row.ofac_hit, row.ofac_match, row.payid_hit,
                    row.payid_outcome, row.created_at, row.updated_at,
                    row.completed_at, row.recalled_at, row.note, row.reason,
                    row.settlement_method,
                ),
            )
            conn.commit()
        return row

    def update_payment(self, row: NppPayment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE payments SET amount=?, fee=?, fee_status=?, status=?,
                    e2e=?, value_date=?, actor=?, releaser=?, ofac_hit=?,
                    ofac_match=?, payid_hit=?, payid_outcome=?, updated_at=?,
                    completed_at=?, recalled_at=?, note=?, reason=?
                WHERE payment_id=?
                """,
                (
                    row.amount, row.fee, row.fee_status, row.status, row.e2e,
                    row.value_date, row.actor, row.releaser, row.ofac_hit,
                    row.ofac_match, row.payid_hit, row.payid_outcome,
                    row.updated_at, row.completed_at, row.recalled_at,
                    row.note, row.reason, row.payment_id,
                ),
            )
            conn.commit()

    def get_payment(self, payment_id: str) -> Optional[NppPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE payment_id = ?', (payment_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[NppPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE trace_id = ?', (trace_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[NppPayment]:
        sql = 'SELECT * FROM payments'
        args: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            args.append(userid)
        if beneficiary_id is not None:
            clauses.append('beneficiary_id = ?')
            args.append(beneficiary_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_pay_from_row(row) for row in rows]

    def next_sequence(self, cycle_date: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE value_date = ? AND e2e != ''",
                (cycle_date,),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class NppService:
    def __init__(
        self,
        policy: NppPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[AusClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn or screen_name
        self.calendar = calendar or AusClock(
            open_hour=policy.open_hour,
            cutoff_hour=policy.cutoff_hour,
            tz_offset_minutes=policy.tz_offset_minutes,
            extra_holidays=policy.extra_holidays,
        )

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise NppError('npp_disabled', 'NPP is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise NppError('npp_forbidden', 'Customers cannot manage NPP beneficiaries.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise NppError('npp_forbidden', 'Customers cannot originate NPP.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise NppError('npp_forbidden', 'Staff only.')

    def _accounts(self, userid: str) -> Tuple[List[str], Dict[str, str]]:
        payload = self.accounts_fn(userid) if self.accounts_fn is not None else {}
        return own_accounts_from_customer_payload(payload), account_types_from_customer_payload(payload)

    def _assert_internal_account(self, userid: str, account: str) -> str:
        owned, types = self._accounts(userid)
        if owned and account not in owned:
            raise NppError('invalid_account', 'Account is not owned by this customer.')
        kind = types.get(account, '')
        if kind == 'credit' and not self.policy.allow_credit:
            raise NppError('credit_not_allowed', 'Credit accounts cannot originate NPP.')
        return kind

    def _assert_amount(self, amount: Decimal, *, rail: str) -> None:
        if amount < self.policy.min_amount:
            raise NppError('amount_out_of_range', 'Amount is outside the allowed range.')
        if rail == 'osko' and amount > self.policy.osko_max:
            raise NppError('osko_amount_exceeded', 'Amount exceeds the Osko cap.')
        if rail == 'npp' and amount > self.policy.npp_max:
            raise NppError('npp_amount_exceeded', 'Amount exceeds the NPP cap.')
        if rail == 'de' and amount > self.policy.de_max:
            raise NppError('de_amount_exceeded', 'Amount exceeds the Direct Entry cap.')

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def _needs_queue(self, amount: Decimal, *, rail: str, clock: Dict[str, Any]) -> bool:
        _ = amount
        if rail in {'osko', 'npp'}:
            return False
        return not bool(clock.get('session'))

    def _screen(self, name: str, *, aliases: Iterable[str] = ()) -> ScreenResult:
        return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)

    def lookup_payid(self, payid: Any, legal_name: Any = '', *, payid_type: Any = '') -> PayIdResolution:
        self._require_enabled()
        return resolve_payid(
            payid, legal_name, payid_type=payid_type, directory=self.policy.payid_directory,
        )

    def add_beneficiary(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        rail: Any = 'osko',
        payid: Any = '',
        payid_type: Any = '',
        bsb: Any = '',
        account_number: Any = '',
        abn: Any = '',
        acn: Any = '',
        default_account: Any,
    ) -> NppBeneficiary:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise NppError('npp_forbidden', 'Not allowed to add NPP beneficiaries for this customer.')
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        kind = normalize_rail(rail)
        payid_value = ''
        payid_kind = ''
        if str(payid or '').strip():
            payid_value, payid_kind = normalize_payid(payid, payid_type=payid_type, required=True)
        elif kind in {'osko', 'npp'} and not str(bsb or '').strip():
            raise NppError('invalid_payid', 'Osko/NPP requires a PayID or BSB+account.')
        bsb_needed = kind == 'de' or (kind in {'osko', 'npp'} and not payid_value)
        bsb_value = normalize_bsb(bsb, required=bsb_needed)
        account_value = normalize_external_account(account_number, required=bool(bsb_value))
        abn_value = normalize_abn(abn, required=False)
        acn_value = normalize_acn(acn, required=False)
        if payid_kind == 'abn' and not abn_value:
            abn_value = payid_value
        if payid_kind == 'acn' and not acn_value:
            acn_value = payid_value
        if kind == 'de' and not (bsb_value and account_value):
            raise NppError('invalid_rail', 'Direct Entry requires BSB+account.')
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        fingerprint = destination_fingerprint(
            kind, payid_kind, payid_value, bsb_value, account_value,
        )
        if self.store.find_beneficiary_by_nickname(owner_userid, name) is not None:
            raise NppError('beneficiary_duplicate', 'An NPP beneficiary with that nickname already exists.')
        if self.store.find_beneficiary_by_fingerprint(owner_userid, fingerprint) is not None:
            raise NppError('beneficiary_duplicate', 'That PayID or BSB+account is already on file.')
        open_rows = [row for row in self.store.list_beneficiaries(owner_userid) if row.status in OPEN_BENE]
        if len(open_rows) >= self.policy.max_beneficiaries:
            raise NppError('beneficiary_limit', 'NPP beneficiary limit reached.')
        now = float(self.clock())
        row = NppBeneficiary(
            beneficiary_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            rail=kind,
            payid_type=payid_kind,
            payid=payid_value,
            bsb=bsb_value,
            account_number=account_value,
            abn=abn_value,
            acn=acn_value,
            fingerprint=fingerprint,
            default_account=account,
            status=BENE_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
            bank_label=self.policy.banks.get(bank_code_from_bsb(bsb_value), ''),
        )
        self.store.put_beneficiary(row)
        return row

    def get_beneficiary(self, *, beneficiary_id: str, actor: str, actor_type: str) -> NppBeneficiary:
        self._require_enabled()
        row = self.store.get_beneficiary(beneficiary_id)
        if row is None:
            raise NppError('beneficiary_not_found', 'NPP beneficiary not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise NppError('npp_forbidden', 'Not allowed to view this NPP beneficiary.')
        return row

    def enforce_beneficiary(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> NppBeneficiary:
        row = self.get_beneficiary(beneficiary_id=beneficiary_id, actor=actor, actor_type=actor_type)
        if row.status == BENE_ARCHIVED:
            raise NppError('already_archived', 'NPP beneficiary is archived.')
        if row.status == BENE_PAUSED:
            raise NppError('beneficiary_paused', 'NPP beneficiary is paused.')
        if require_active and row.status != BENE_ACTIVE:
            raise NppError('invalid_status', 'NPP beneficiary is not active.')
        return row

    def set_beneficiary_status(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> NppBeneficiary:
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
            raise NppError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == BENE_ARCHIVED:
            raise NppError('already_archived', 'NPP beneficiary is already archived.')
        if wanted == BENE_PAUSED:
            if row.status == BENE_PAUSED:
                raise NppError('already_paused', 'NPP beneficiary is already paused.')
            if row.status != BENE_ACTIVE:
                raise NppError('invalid_status', 'Only an active NPP beneficiary can be paused.')
        elif wanted == BENE_ACTIVE:
            if row.status == BENE_ACTIVE:
                raise NppError('already_active', 'NPP beneficiary is already active.')
            if row.status != BENE_PAUSED:
                raise NppError('invalid_status', 'Only a paused NPP beneficiary can be resumed.')
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
        internal_account: Any = None,
        waive_fee: bool = False,
    ) -> Dict[str, Any]:
        self._require_send(actor_type)
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise NppError('npp_forbidden', 'NPP beneficiary does not belong to this customer.')
        dollars = parse_money(amount)
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        self._assert_amount(dollars, rail=bene.rail)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        fee = compute_fee(dollars, self.policy.outbound_fee, waived=bool(waive_fee) and actor_type in EMPLOYEE_ROLES)
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        cop = PayIdResolution(COP_UNAVAILABLE, '', '', '', 0)
        if bene.payid:
            cop = self.lookup_payid(bene.payid, bene.legal_name, payid_type=bene.payid_type)
        queued = self._needs_queue(dollars, rail=bene.rail, clock=clock)
        return {
            'amount': money_str(dollars),
            'fee': money_str(fee),
            'total': money_str(dollars + fee),
            'currency': 'AUD',
            'internal_account': account,
            'beneficiary': bene.to_dict(),
            'ofac': ofac.to_dict(),
            'payid': cop.to_dict(),
            'dual_control': self._needs_dual_control(dollars),
            'queued': queued,
            'clock': clock,
            'osko_cap': money_str(self.policy.osko_max),
            'npp_cap': money_str(self.policy.npp_max),
            'de_cap': money_str(self.policy.de_max),
            'settlement_method': bene.rail,
        }

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        bene: NppBeneficiary,
        account: str,
        dollars: Decimal,
        fee: Decimal,
        purpose: str,
        memo: str,
        trace_id: str,
        ofac: ScreenResult,
        cop: PayIdResolution,
        waive_fee: bool,
        clock: Dict[str, Any],
    ) -> NppPayment:
        now = float(self.clock())
        value = self.calendar.cycle_date(now)
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        payid_hit = 1 if cop.outcome == COP_NONE else 0
        if ofac.hit:
            status = NPP_HELD
        elif payid_hit:
            status = NPP_HELD
        elif self._needs_dual_control(dollars):
            status = NPP_PENDING
        elif self._needs_queue(dollars, rail=bene.rail, clock=clock):
            status = NPP_QUEUED
            value = self.calendar.value_date(now).strftime('%Y%m%d')
        else:
            status = NPP_COMPLETED
        payment = NppPayment(
            payment_id=uuid.uuid4().hex,
            trace_id=trace_id,
            beneficiary_id=bene.beneficiary_id,
            userid=owner_userid,
            internal_account=account,
            amount=money_str(dollars),
            fee=money_str(fee),
            fee_status=fee_status,
            nickname=bene.nickname,
            legal_name=bene.legal_name,
            rail=bene.rail,
            payid_type=bene.payid_type,
            payid_masked=mask_payid(bene.payid, bene.payid_type) if bene.payid else '',
            bsb_masked=mask_bsb(bene.bsb) if bene.bsb else '',
            account_last4=last4(bene.account_number) if bene.account_number else '',
            abn_masked=mask_abn(bene.abn) if bene.abn else '',
            acn_masked=mask_acn(bene.acn) if bene.acn else '',
            bank_code=bank_code_from_bsb(bene.bsb) if bene.bsb else '',
            purpose=purpose,
            memo=memo,
            status=status,
            e2e='',
            value_date=value,
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            payid_hit=payid_hit,
            payid_outcome=cop.outcome,
            created_at=now,
            updated_at=now,
            settlement_method=bene.rail,
        )
        if status == NPP_COMPLETED:
            self._transmit(payment, actor=actor, bene=bene)
        return payment

    def _bene_secrets(self, payment: NppPayment) -> NppBeneficiary:
        bene = self.store.get_beneficiary(payment.beneficiary_id)
        if bene is None:
            return NppBeneficiary(
                beneficiary_id='', userid='', nickname='', legal_name='', rail=payment.rail,
                payid_type='', payid='', bsb='', account_number='', abn='', acn='',
                fingerprint='', default_account='', status=BENE_ACTIVE, actor='',
                created_at=0, updated_at=0,
            )
        return bene

    def _transmit(
        self,
        payment: NppPayment,
        *,
        actor: str,
        bene: Optional[NppBeneficiary] = None,
    ) -> NppPayment:
        now = float(self.clock())
        cycle = payment.value_date or self.calendar.cycle_date(now)
        seq = self.store.next_sequence(cycle)
        payment.e2e = compose_e2e(cycle, seq)
        dollars = parse_money(payment.amount)
        fee = parse_money(payment.fee, allow_zero=True)
        remark = '%s to %s' % (payment.rail, payment.nickname)
        status = NPP_COMPLETED
        fail_note = ''
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(payment.internal_account, money_str(dollars), remark)
            except Exception as exc:
                status = NPP_FAILED
                fail_note = str(exc)[:240]
            else:
                classified = _classify_money_result(result)
                if classified == 'nsf':
                    status = NPP_NSF
                    payment.e2e = ''
                elif classified != 'ok':
                    status = NPP_FAILED
                    fail_note = str(result)[:240]
                    payment.e2e = ''
        if status == NPP_COMPLETED and fee > 0 and payment.fee_status != FEE_WAIVED and self.debit_fn is not None:
            fee_remark = '%s fee %s' % (payment.rail, mask_e2e(payment.e2e))
            try:
                fee_result = self.debit_fn(payment.internal_account, money_str(fee), fee_remark)
            except Exception:
                payment.fee_status = FEE_NSF
            else:
                if _classify_money_result(fee_result) == 'ok':
                    payment.fee_status = FEE_COLLECTED
                else:
                    payment.fee_status = FEE_NSF
        payment.status = status
        payment.updated_at = now
        payment.completed_at = now if status == NPP_COMPLETED else 0.0
        payment.releaser = str(actor)
        if fail_note:
            payment.note = fail_note
        _ = bene
        return payment

    def originate(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        beneficiary_id: Any,
        amount: Any,
        internal_account: Any = None,
        purpose: Any = 'other',
        memo: Any = '',
        trace_id: Any = None,
        waive_fee: bool = False,
    ) -> Tuple[NppPayment, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise NppError('npp_forbidden', 'Not allowed to originate NPP for this customer.')
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise NppError('npp_forbidden', 'NPP beneficiary does not belong to this customer.')
        trace = normalize_id(trace_id)
        existing = self.store.get_payment_by_trace(trace)
        if existing is not None:
            return existing, False
        dollars = parse_money(amount)
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        self._assert_amount(dollars, rail=bene.rail)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        fee = compute_fee(
            dollars, self.policy.outbound_fee,
            waived=bool(waive_fee) and actor_type in EMPLOYEE_ROLES,
        )
        if len(self.store.list_payments(owner_userid)) >= self.policy.max_payments:
            raise NppError('payment_limit', 'NPP history limit reached.')
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        cop = PayIdResolution(COP_UNAVAILABLE, '', '', '', 0)
        if bene.payid:
            cop = self.lookup_payid(bene.payid, bene.legal_name, payid_type=bene.payid_type)
        payment = self._place(
            owner_userid=owner_userid,
            actor=actor,
            bene=bene,
            account=account,
            dollars=dollars,
            fee=fee,
            purpose=normalize_purpose(purpose),
            memo=normalize_note(memo, limit=140),
            trace_id=trace,
            ofac=ofac,
            cop=cop,
            waive_fee=bool(waive_fee) and actor_type in EMPLOYEE_ROLES,
            clock=clock,
        )
        stored = self.store.put_payment(payment)
        if stored.status == NPP_NSF:
            raise NppError('nsf', 'Insufficient funds for NPP.', payment=stored)
        if stored.status == NPP_FAILED:
            raise NppError('failed', 'NPP debit did not complete.', payment=stored)
        return stored, True

    def get_payment(self, *, payment_id: str, actor: str, actor_type: str) -> NppPayment:
        self._require_enabled()
        row = self.store.get_payment(payment_id)
        if row is None:
            raise NppError('payment_not_found', 'NPP payment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise NppError('npp_forbidden', 'Not allowed to view this NPP payment.')
        return row

    def cancel_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> NppPayment:
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and payment.userid != actor:
            raise NppError('npp_forbidden', 'Not allowed to cancel this NPP.')
        if payment.status not in CANCELABLE:
            raise NppError('not_cancelable', 'Only held, queued, or pending NPP can be cancelled.')
        payment.status = NPP_CANCELLED
        payment.note = normalize_note(note)
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        self.store.update_payment(payment)
        return payment

    def waive_fee(self, *, payment_id: str, actor: str, actor_type: str) -> NppPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status not in CANCELABLE:
            raise NppError('invalid_status', 'Fee can only be waived before the NPP is sent.')
        payment.fee = money_str(Decimal('0.00'))
        payment.fee_status = FEE_WAIVED
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        self.store.update_payment(payment)
        return payment

    def _requeue_or_transmit(self, payment: NppPayment, *, actor: str) -> NppPayment:
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        dollars = parse_money(payment.amount)
        if self._needs_queue(dollars, rail=payment.rail, clock=clock):
            payment.status = NPP_QUEUED
            payment.value_date = self.calendar.value_date(now).strftime('%Y%m%d')
            payment.updated_at = now
            payment.actor = str(actor)
            return payment
        return self._transmit(payment, actor=actor)

    def override_ofac(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> NppPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status != NPP_HELD or not payment.ofac_hit:
            raise NppError('not_overridable', 'Only an OFAC hold can be overridden.')
        now = float(self.clock())
        payment.ofac_hit = 0
        payment.note = normalize_note(note) or 'ofac override'
        payment.actor = str(actor)
        payment.updated_at = now
        dollars = parse_money(payment.amount)
        if payment.payid_hit:
            payment.status = NPP_HELD
        elif self._needs_dual_control(dollars):
            payment.status = NPP_PENDING
        else:
            self._requeue_or_transmit(payment, actor=actor)
        self.store.update_payment(payment)
        if payment.status == NPP_NSF:
            raise NppError('nsf', 'Insufficient funds for NPP.', payment=payment)
        if payment.status == NPP_FAILED:
            raise NppError('failed', 'NPP debit did not complete.', payment=payment)
        return payment

    def override_payid(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> NppPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status != NPP_HELD or not payment.payid_hit:
            raise NppError('not_overridable', 'Only a PayID mismatch hold can be overridden.')
        now = float(self.clock())
        payment.payid_hit = 0
        payment.payid_outcome = COP_MATCH
        payment.note = normalize_note(note) or 'payid override'
        payment.actor = str(actor)
        payment.updated_at = now
        dollars = parse_money(payment.amount)
        if payment.ofac_hit:
            payment.status = NPP_HELD
        elif self._needs_dual_control(dollars):
            payment.status = NPP_PENDING
        else:
            self._requeue_or_transmit(payment, actor=actor)
        self.store.update_payment(payment)
        if payment.status == NPP_NSF:
            raise NppError('nsf', 'Insufficient funds for NPP.', payment=payment)
        if payment.status == NPP_FAILED:
            raise NppError('failed', 'NPP debit did not complete.', payment=payment)
        return payment

    def release_payment(self, *, payment_id: str, actor: str, actor_type: str) -> NppPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == NPP_HELD:
            if payment.ofac_hit:
                raise NppError('ofac_hold', 'OFAC hold must be overridden before release.')
            raise NppError('payid_mismatch', 'PayID mismatch must be overridden before release.')
        if payment.status not in {NPP_PENDING, NPP_QUEUED}:
            raise NppError('not_releasable', 'Only queued or pending NPP can be released.')
        if (
            payment.status == NPP_PENDING
            and payment.actor
            and str(actor) == str(payment.actor)
            and parse_money(payment.amount) >= self.policy.dual_control_threshold
        ):
            raise NppError('same_approver', 'A different employee must release this NPP.')
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        dollars = parse_money(payment.amount)
        if payment.status == NPP_QUEUED and self._needs_queue(dollars, rail=payment.rail, clock=clock):
            payment.updated_at = now
            self.store.update_payment(payment)
            return payment
        self._requeue_or_transmit(payment, actor=actor)
        self.store.update_payment(payment)
        if payment.status == NPP_NSF:
            raise NppError('nsf', 'Insufficient funds for NPP.', payment=payment)
        if payment.status == NPP_FAILED:
            raise NppError('failed', 'NPP debit did not complete.', payment=payment)
        return payment

    def reject_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> NppPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status not in CANCELABLE:
            raise NppError('not_rejectable', 'Only held, queued, or pending NPP can be rejected.')
        payment.status = NPP_REJECTED
        payment.reason = normalize_note(reason, limit=40)
        payment.note = normalize_note(note)
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        self.store.update_payment(payment)
        return payment

    def _assert_complaint_window(self, payment: NppPayment, *, actor_type: str) -> None:
        now = float(self.clock())
        completed = float(payment.completed_at or payment.updated_at or payment.created_at)
        if actor_type in EMPLOYEE_ROLES:
            if now - completed > self.policy.staff_complaint_days * 86400:
                raise NppError('complaint_window_closed', 'Staff NPP complaint window has closed.')
            return
        if payment.rail in {'osko', 'npp'}:
            if now - completed > self.policy.customer_npp_return_seconds:
                raise NppError('return_window_closed', 'Customer Osko/NPP complaint window has closed.')
            return
        completed_day = self.calendar.local_dt(completed).date()
        deadline_day = self.calendar.next_business_day(completed_day)
        deadline = self.calendar.session_close_ts(deadline_day)
        if now > deadline:
            raise NppError('return_window_closed', 'Customer Direct Entry complaint window has closed.')

    def recall_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> NppPayment:
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == NPP_RECALLED:
            raise NppError('already_recalled', 'NPP payment is already recalled.')
        if payment.status != NPP_COMPLETED:
            raise NppError('not_recallable', 'Only completed NPP payments can be recalled.')
        self._assert_complaint_window(payment, actor_type=actor_type)
        remark = normalize_note(note) or (
            '%s complaint %s' % (payment.rail, mask_e2e(payment.e2e) or payment.nickname)
        )
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(payment.internal_account, payment.amount, remark)
            except Exception as exc:
                raise NppError('recall_failed', 'Complaint credit failed.', payment=payment) from exc
            if _classify_money_result(result) != 'ok':
                raise NppError('recall_failed', 'Complaint credit failed.', payment=payment)
            fee = parse_money(payment.fee, allow_zero=True)
            if fee > 0 and payment.fee_status == FEE_COLLECTED:
                self.credit_fn(
                    payment.internal_account, money_str(fee),
                    '%s fee recalled %s' % (payment.rail, mask_e2e(payment.e2e)),
                )
        now = float(self.clock())
        payment.status = NPP_RECALLED
        payment.recalled_at = now
        payment.updated_at = now
        payment.actor = str(actor)
        payment.note = remark
        self.store.update_payment(payment)
        return payment

    def export_npp1(self, *, payment_id: str, actor: str, actor_type: str) -> Dict[str, Any]:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status != NPP_COMPLETED:
            raise NppError('not_exportable', 'Only completed NPP can be exported.')
        if not payment.e2e:
            raise NppError('not_exportable', 'End-to-End ID has not been assigned.')
        bene = self._bene_secrets(payment)
        record = compose_npp1(
            e2e=payment.e2e,
            rail=payment.rail,
            payid_type=bene.payid_type,
            payid=bene.payid,
            bsb=bene.bsb,
            account_number=bene.account_number,
            abn=bene.abn,
            amount=parse_money(payment.amount),
            purpose=payment.purpose,
            status=payment.status,
            mask=True,
        )
        return {
            'payment_id': payment.payment_id,
            'npp1': record,
            'e2e_masked': mask_e2e(payment.e2e),
        }

    def run_due(self, userid: Optional[str] = None) -> List[NppPayment]:
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        changed: List[NppPayment] = []
        for payment in self.store.list_payments(userid):
            if payment.status != NPP_QUEUED:
                continue
            dollars = parse_money(payment.amount)
            if self._needs_queue(dollars, rail=payment.rail, clock=clock):
                continue
            self._transmit(payment, actor=payment.actor)
            self.store.update_payment(payment)
            changed.append(payment)
        return changed

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        _ = actor
        self.run_due(userid)
        beneficiaries = self.store.list_beneficiaries(userid)
        payments = self.store.list_payments(userid)
        sent_ytd = Decimal('0.00')
        fee_ytd = Decimal('0.00')
        recalled = Decimal('0.00')
        for row in payments:
            amount = parse_money(row.amount, allow_zero=True)
            if row.status == NPP_COMPLETED:
                sent_ytd += amount
                if row.fee_status == FEE_COLLECTED:
                    fee_ytd += parse_money(row.fee, allow_zero=True)
            elif row.status == NPP_RECALLED:
                recalled += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'osko_max': money_str(self.policy.osko_max),
            'npp_max': money_str(self.policy.npp_max),
            'de_max': money_str(self.policy.de_max),
            'fee': money_str(self.policy.outbound_fee),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'currency': 'AUD',
            'clock': self.calendar.snapshot(now),
            'beneficiaries': [row.to_dict() for row in beneficiaries[:40]],
            'payments': [row.to_dict() for row in payments[:40]],
            'ytd_sent': money_str(sent_ytd),
            'ytd_fees': money_str(fee_ytd),
            'recalled_ytd': money_str(recalled),
            'active_count': sum(1 for row in beneficiaries if row.status == BENE_ACTIVE),
            'open_count': sum(1 for row in payments if row.status in OPEN_NPP),
        }


_SERVICE: Optional[NppService] = None


def set_service(service: Optional[NppService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[NppService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('NPP_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryNppStore()
    path = os.environ.get('NPP_DB', DEFAULT_STORE_PATH)
    return SqliteNppStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[AusClock] = None,
) -> NppService:
    if store is None:
        store = default_store()
    return NppService(
        NppPolicy.from_env(),
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
        'beneficiary_duplicate': 409,
        'beneficiary_limit': 409,
        'payment_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_recalled': 409,
        'nsf': 409,
        'failed': 409,
        'recall_failed': 409,
        'npp_forbidden': 403,
        'npp_disabled': 403,
        'beneficiary_paused': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'payid_mismatch': 403,
        'same_approver': 403,
        'not_cancelable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_recallable': 403,
        'not_overridable': 403,
        'not_exportable': 403,
        'return_window_closed': 403,
        'complaint_window_closed': 403,
        'beneficiary_not_found': 404,
        'payment_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_bsb': 400,
        'invalid_payid': 400,
        'invalid_abn': 400,
        'invalid_acn': 400,
        'invalid_phone': 400,
        'invalid_rail': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_currency': 400,
        'invalid_reference': 400,
        'invalid_file': 400,
        'osko_amount_exceeded': 400,
        'npp_amount_exceeded': 400,
        'de_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_beneficiary': 400,
        'missing_payment': 400,
        'missing_payid': 400,
    }.get(code, 400)


def _error_body(exc: NppError) -> Dict[str, Any]:
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
    except NppError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list_npp(service: NppService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Npp': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_lookup_payid(service: NppService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    payid = values.get('payid') or values.get('email') or values.get('phone') or values.get('abn')
    if not str(payid or '').strip():
        return jsonify({'message': 'Some data missing', 'error': 'missing_payid'}), 400

    def _run():
        resolution = service.lookup_payid(
            payid, values.get('legal_name') or values.get('name') or '',
            payid_type=values.get('payid_type') or '',
        )
        return jsonify({
            'message': 'PayID lookup complete',
            'PayId': resolution.to_dict(),
        }), 200

    return _handle_errors(_run)


def handle_add_beneficiary(service: NppService):
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
            rail=values.get('rail') or 'osko',
            payid=values.get('payid') or values.get('email') or values.get('phone'),
            payid_type=values.get('payid_type'),
            bsb=values.get('bsb') or values.get('routing'),
            account_number=values.get('account_number') or values.get('beneficiary_account'),
            abn=values.get('abn'),
            acn=values.get('acn'),
            default_account=values.get('default_account') or values.get('account') or values.get('from_account'),
        )
        return jsonify({
            'message': 'NPP beneficiary added',
            'beneficiary': row.to_dict(),
            'Npp': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _bene_status_route(service: NppService, status: str, ok_message: str):
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
            'Npp': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: NppService):
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
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({'preview': preview, 'Npp': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: NppService):
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
        payment, created = service.originate(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            beneficiary_id=beneficiary_id,
            amount=values.get('amount'),
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({
            'message': 'NPP originated' if created else 'NPP already posted',
            'payment': payment.to_dict(),
            'Npp': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: NppService):
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
            'message': 'NPP cancelled',
            'payment': payment.to_dict(),
            'Npp': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_npp_route(service: NppService, action: str):
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
            message = 'NPP released'
        elif action == 'reject':
            payment = service.reject_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'NPP rejected'
        elif action == 'recall':
            payment = service.recall_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'NPP recalled'
        elif action == 'override':
            payment = service.override_ofac(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'override_payid':
            payment = service.override_payid(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'PayID mismatch overridden'
        elif action == 'waive':
            payment = service.waive_fee(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'NPP fee waived'
        elif action == 'export':
            exported = service.export_npp1(payment_id=payment_id, actor=userid, actor_type=actor_type)
            payment = service.get_payment(payment_id=payment_id, actor=userid, actor_type=actor_type)
            return jsonify({
                'message': 'NPP exported',
                'export': exported,
                'payment': payment.to_dict(),
                'Npp': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
            }), 200
        else:
            raise NppError('invalid_status', 'Unknown NPP action.')
        return jsonify({
            'message': message,
            'payment': payment.to_dict(),
            'Npp': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: NppService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Npp': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_npp_routes(app, service: NppService) -> None:
    @app.route('/listNpp', methods=['POST', 'GET'])
    def list_npp_route():
        return handle_list_npp(service)

    @app.route('/listNppBeneficiaries', methods=['POST', 'GET'])
    def list_npp_beneficiaries_route():
        return handle_list_npp(service)

    @app.route('/lookupPayId', methods=['POST', 'GET'])
    def lookup_payid_route():
        return handle_lookup_payid(service)

    @app.route('/addNppBeneficiary', methods=['POST', 'GET'])
    def add_npp_beneficiary_route():
        return handle_add_beneficiary(service)

    @app.route('/pauseNppBeneficiary', methods=['POST', 'GET'])
    def pause_npp_beneficiary_route():
        return _bene_status_route(service, BENE_PAUSED, 'NPP beneficiary paused')

    @app.route('/resumeNppBeneficiary', methods=['POST', 'GET'])
    def resume_npp_beneficiary_route():
        return _bene_status_route(service, BENE_ACTIVE, 'NPP beneficiary resumed')

    @app.route('/archiveNppBeneficiary', methods=['POST', 'GET'])
    def archive_npp_beneficiary_route():
        return _bene_status_route(service, BENE_ARCHIVED, 'NPP beneficiary archived')

    @app.route('/previewNpp', methods=['POST', 'GET'])
    def preview_npp_route():
        return handle_preview(service)

    @app.route('/sendNpp', methods=['POST', 'GET'])
    def send_npp_route():
        return handle_send(service)

    @app.route('/cancelNpp', methods=['POST', 'GET'])
    def cancel_npp_route():
        return handle_cancel(service)

    @app.route('/releaseNpp', methods=['POST', 'GET'])
    def release_npp_route():
        return _staff_npp_route(service, 'release')

    @app.route('/rejectNpp', methods=['POST', 'GET'])
    def reject_npp_route():
        return _staff_npp_route(service, 'reject')

    @app.route('/recallNpp', methods=['POST', 'GET'])
    def recall_npp_route():
        return _staff_npp_route(service, 'recall')

    @app.route('/overrideNppOfac', methods=['POST', 'GET'])
    def override_npp_ofac_route():
        return _staff_npp_route(service, 'override')

    @app.route('/overrideNppPayId', methods=['POST', 'GET'])
    def override_npp_payid_route():
        return _staff_npp_route(service, 'override_payid')

    @app.route('/waiveNppFee', methods=['POST', 'GET'])
    def waive_npp_fee_route():
        return _staff_npp_route(service, 'waive')

    @app.route('/exportNpp', methods=['POST', 'GET'])
    def export_npp_route():
        return _staff_npp_route(service, 'export')

    @app.route('/runDueNpp', methods=['POST', 'GET'])
    def run_due_npp_route():
        return handle_run_due(service)
