"""Inbound NPP / Osko / Direct Entry receive posting (PayID claim / AUNPP).

Staff ingest incoming Australian credits. Recipients enroll PayIDs
(email, +61 mobile, ABN, ACN, OrgID) at this bank's BSB. Matched Osko
and NPP credits post instantly 24/7. Direct Entry waits for the AEST
09:00–17:00 banking session. Unmatched PayIDs wait for a customer claim
or staff assignment. Independent of outbound NPP origination (PR #175),
PIX inbound (PR #139), domestic Fedwire (PR #73), and ACH linking
(PR #68). Existing `/fundTransfer`, `/withdrawAmount`, and `/sendWire`
stay unchanged.

Foundations (reusable beyond this screen):
- BSB (6-digit XXX-XXX) + Australian participant directory
- PayID normalize / mask / SHA-256 fingerprint (ABN mod-89, ACN check)
- NPP End-to-End ID `NPP`+YYYYMMDD+seq uniqueness (IMAD/UETR/RRN rejected)
- AusClock AEST (UTC+10): Osko/NPP 24/7; Direct Entry 09:00–17:00
- NPP1 pipe compose/parse (XML/DOCTYPE and non-AUNPP clearing rejected)
- PayID claim (customer presents matching PayID; enroll rematches unmatched)
- Return windows (Osko/NPP 24h customer / DE next banking day / 90-day staff)
- Dual-control at AUD 10k; Osko cap 10k / NPP+DE cap 100k

Reuses Fedwire OFAC screening, money parsing, and account directory
from `utility.wire`. Stores are pluggable (memory for tests, sqlite WAL
for restart-safe default). Full PayID, BSB, account, ABN, ACN,
fingerprint, and E2E ID never appear in to_dict / snapshots.
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
    DEFAULT_WATCHLIST,
    EMPLOYEE_ROLES,
    MONEY_QUANTUM,
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

KEY_ACTIVE = 'active'
KEY_PAUSED = 'paused'
KEY_CANCELLED = 'cancelled'
KEY_STATUSES = frozenset({KEY_ACTIVE, KEY_PAUSED, KEY_CANCELLED})
OPEN_KEY = frozenset({KEY_ACTIVE, KEY_PAUSED})
IN_UNMATCHED = 'unmatched'
IN_HELD = 'held'
IN_QUEUED = 'queued'
IN_PENDING = 'pending_release'
IN_POSTED = 'posted'
IN_REJECTED = 'rejected'
IN_RETURNED = 'returned'
IN_EXPIRED = 'expired'
IN_FAILED = 'failed'
IN_STATUSES = frozenset({
    IN_UNMATCHED, IN_HELD, IN_QUEUED, IN_PENDING, IN_POSTED, IN_REJECTED,
    IN_RETURNED, IN_EXPIRED, IN_FAILED,
})
OPEN_IN = frozenset({IN_UNMATCHED, IN_HELD, IN_QUEUED, IN_PENDING})
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
EMAIL_RE = re.compile(r'^[a-z0-9][a-z0-9._%+-]{0,63}@[a-z0-9.-]+\.[a-z]{2,24}$')
ORGID_RE = re.compile(r'^[A-Z0-9][A-Z0-9._-]{3,31}$')
ABN_WEIGHTS = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)
ACN_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 1)
NPP1_VERSION = '1'
NPP1_FIELDS = 14
DEFAULT_STORE_PATH = 'SystemLogs/nrecv.sqlite'
DEFAULT_RECEIVER_BSB = '062000'
CUSTOMER_NPP_RETURN_SECONDS = 24 * 60 * 60
STAFF_COMPLAINT_DAYS = 90
CLAIM_SECONDS = 24 * 60 * 60
AEST_OFFSET_MINUTES = 600
BLOCKED_E2E_PREFIXES = (
    'IMAD', 'UETR', 'RFI', 'RFR', 'RTR', 'TKN', 'VAU', 'RRN', 'STAN', 'ARN',
)
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)


class NrecvError(ValueError):
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
        raise NrecvError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise NrecvError('invalid_name', 'Sender legal name must be 2-80 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise NrecvError('invalid_purpose', 'Unknown NPP purpose.')
    return text


def normalize_currency(value: Any, *, default: str = 'AUD') -> str:
    text = str(value or default).strip().upper()
    if text not in {'AUD', 'A$', 'AUD.'}:
        raise NrecvError('invalid_currency', 'NPP / Osko / Direct Entry is AUD only.')
    return 'AUD'


def normalize_rail(value: Any, *, default: str = 'osko') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text not in RAILS:
        raise NrecvError('invalid_rail', 'Rail must be osko, npp, or de.')
    return text


def normalize_payid_type(value: Any, *, default: str = '') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PAYID_ALIASES.get(text, text)
    if not text:
        return ''
    if text not in PAYID_TYPES:
        raise NrecvError('invalid_payid', 'PayID type must be email, phone, abn, acn, or orgid.')
    return text


def normalize_bsb(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise NrecvError('invalid_bsb', 'BSB is required.')
        return ''
    if len(digits) != 6 or digits == '000000':
        raise NrecvError('invalid_bsb', 'BSB must be six digits.')
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
    except NrecvError:
        return ''


def bank_name(value: Any, *, directory: Optional[Dict[str, str]] = None) -> str:
    code = bank_code_from_bsb(value)
    mapping = directory if directory is not None else DEFAULT_BANKS
    return mapping.get(code, '')


def normalize_external_account(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise NrecvError('invalid_account', 'Account is required.')
        return ''
    if not (4 <= len(digits) <= 9):
        raise NrecvError('invalid_account', 'Australian account must be 4-9 digits.')
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
            raise NrecvError('invalid_abn', 'ABN is required.')
        return ''
    if not abn_check_ok(digits):
        raise NrecvError('invalid_abn', 'ABN failed the mod-89 checksum.')
    return digits


def mask_abn(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '***********'
    return digits[:2] + ('*' * 5) + digits[-4:]


def acn_check_digit(body: str) -> str:
    if len(body) != 8 or not body.isdigit():
        raise NrecvError('invalid_acn', 'ACN body must be 8 digits.')
    total = sum(int(ch) * w for ch, w in zip(body, ACN_WEIGHTS))
    return str((10 - (total % 10)) % 10)


def acn_check_ok(digits: str) -> bool:
    if len(digits) != 9 or not digits.isdigit():
        return False
    try:
        return acn_check_digit(digits[:8]) == digits[8]
    except NrecvError:
        return False


def compose_acn(body: Any) -> str:
    digits = ''.join(ch for ch in str(body or '') if ch.isdigit())
    if len(digits) != 8:
        raise NrecvError('invalid_acn', 'ACN body must be 8 digits.')
    return digits + acn_check_digit(digits)


def normalize_acn(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise NrecvError('invalid_acn', 'ACN is required.')
        return ''
    if not acn_check_ok(digits):
        raise NrecvError('invalid_acn', 'ACN failed checksum.')
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
            raise NrecvError('invalid_phone', 'Australian mobile is required.')
        return ''
    if digits.startswith('61') and len(digits) == 11:
        national = '0' + digits[2:]
    elif digits.startswith('0') and len(digits) == 10:
        national = digits
    elif len(digits) == 9 and digits[0] == '4':
        national = '0' + digits
    else:
        raise NrecvError('invalid_phone', 'Australian mobile must be 04xx xxx xxx.')
    if len(national) != 10 or not national.startswith('04'):
        raise NrecvError('invalid_phone', 'Australian mobile must be 04xx xxx xxx.')
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
            raise NrecvError('invalid_payid', 'PayID email is required.')
        return ''
    if not EMAIL_RE.match(text):
        raise NrecvError('invalid_payid', 'PayID email is invalid.')
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
            raise NrecvError('invalid_payid', 'Organisation ID is required.')
        return ''
    if not ORGID_RE.match(text):
        raise NrecvError('invalid_payid', 'Organisation ID is invalid.')
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
            raise NrecvError('invalid_payid', 'PayID is required.')
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
    raise NrecvError('invalid_payid', 'PayID type is unknown.')


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


def payid_fingerprint(payid_type: Any, value: Any) -> str:
    normalized, kind = normalize_payid(value, payid_type=payid_type, required=True)
    raw = '%s\0%s' % (kind, normalized)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def compose_e2e(cycle_date: str, sequence: int) -> str:
    """NPP End-to-End ID: NPP + YYYYMMDD + 6-digit sequence."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise NrecvError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise NrecvError('invalid_reference', 'E2E sequence out of range.')
    return 'NPP%s%06d' % (day, seq)


def normalize_e2e(value: Any) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        raise NrecvError('invalid_reference', 'NPP End-to-End ID is required.')
    if any(text.startswith(prefix) for prefix in BLOCKED_E2E_PREFIXES):
        raise NrecvError('invalid_reference', 'End-to-End ID must not use another rail prefix.')
    if len(text) == 12 and text.isdigit():
        raise NrecvError('invalid_reference', 'NPCI RRN is not an NPP End-to-End ID.')
    if len(text) == 22 and text[:8].isdigit():
        raise NrecvError('invalid_reference', 'Fedwire IMAD is not an NPP End-to-End ID.')
    if not text.startswith('NPP') or len(text) != 17 or not text[3:].isdigit():
        raise NrecvError('invalid_reference', 'NPP End-to-End ID must be NPP+YYYYMMDD+seq.')
    return text


def mask_e2e(value: Any) -> str:
    text = str(value or '')
    if len(text) < 6:
        return '********'
    return ('*' * (len(text) - 4)) + text[-4:]


def compose_amount_field(amount: Decimal) -> str:
    quantized = amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    if quantized < 0 or quantized > Decimal('999999999.99'):
        raise NrecvError('invalid_amount', 'Amount cannot be encoded in NPP1.')
    return money_str(quantized)


def parse_amount_field(value: Any) -> Decimal:
    try:
        return parse_money(value)
    except AmountError as exc:
        raise NrecvError('invalid_amount', 'NPP1 amount field is invalid.') from exc


def _reject_markup(text: str) -> None:
    lowered = text.lower()
    if '<xml' in lowered or '<!doctype' in lowered or '<?xml' in lowered:
        raise NrecvError('invalid_file', 'XML/DOCTYPE NPP1 files are rejected.')


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
        str(status or IN_POSTED),
        'AUD',
        CLEARING_AUNPP,
    ))


def parse_npp1(text: Any) -> Dict[str, Any]:
    raw = str(text or '').strip()
    if not raw:
        raise NrecvError('invalid_file', 'NPP1 record is empty.')
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != NPP1_FIELDS or parts[0] != 'NPP1' or parts[1] != NPP1_VERSION:
        raise NrecvError('invalid_file', 'NPP1 record is malformed.')
    clearing = parts[13].strip().upper()
    if clearing in REJECTED_CLEARING or clearing != CLEARING_AUNPP:
        raise NrecvError('invalid_file', 'NPP1 clearing must be AUNPP.')
    joined = '|'.join(parts[2:9])
    if '*' in joined or '…' in joined:
        raise NrecvError('invalid_file', 'Masked NPP1 record cannot be re-ingested.')
    payid_type = normalize_payid_type(parts[4]) if parts[4] else ''
    payid = ''
    if parts[5]:
        payid, payid_type = normalize_payid(parts[5], payid_type=payid_type, required=True)
    return {
        'e2e': normalize_e2e(parts[2]),
        'rail': normalize_rail(parts[3]),
        'payid_type': payid_type,
        'payid': payid,
        'bsb': normalize_bsb(parts[6], required=bool(parts[6])),
        'account_number': normalize_external_account(parts[7], required=bool(parts[7])),
        'abn': normalize_abn(parts[8], required=False) if parts[8] else '',
        'amount': parse_amount_field(parts[9]),
        'purpose': normalize_purpose(parts[10]),
        'status': parts[11],
        'currency': normalize_currency(parts[12]),
        'clearing': clearing,
        'name': '',
    }


def split_npp1_file(text: Any) -> List[str]:
    raw = str(text or '')
    _reject_markup(raw)
    records = [line.strip() for line in raw.replace('\r\n', '\n').split('\n') if line.strip()]
    if not records:
        raise NrecvError('invalid_file', 'NPP1 file is empty.')
    return records


def message_from_values(values: Dict[str, Any]) -> Dict[str, Any]:
    recv = normalize_bsb(values.get('recv_bsb') or values.get('receiver') or DEFAULT_RECEIVER_BSB)
    send = normalize_bsb(values.get('send_bsb') or values.get('sender_bsb') or values.get('bsb') or '013000', required=False)
    if not send:
        send = '013000'
    payid_raw = values.get('payid') or values.get('key') or values.get('key_value') or values.get('pay_id')
    payid_type = values.get('payid_type') or values.get('key_type') or values.get('kind') or ''
    payid = ''
    kind = ''
    if payid_raw:
        payid, kind = normalize_payid(payid_raw, payid_type=payid_type, required=True)
    amount = values.get('amount')
    if not isinstance(amount, Decimal):
        try:
            amount = parse_money(amount)
        except AmountError as exc:
            raise NrecvError('invalid_amount', 'Amount is invalid.') from exc
    e2e = str(values.get('end_to_end_id') or values.get('e2e') or '').strip()
    if e2e:
        e2e = normalize_e2e(e2e)
    else:
        cycle = str(values.get('value_date') or values.get('cycle_date') or '').strip()
        if '-' in cycle:
            cycle = cycle.replace('-', '')[:8]
        seq = values.get('sequence') or values.get('seq') or 1
        e2e = compose_e2e(cycle, int(seq))
    name = str(values.get('name') or values.get('sender_name') or values.get('payer') or '').strip()
    if name:
        name = normalize_legal_name(name)
    dest_bsb = normalize_bsb(values.get('dest_bsb') or values.get('account_bsb') or recv, required=False) or recv
    dest_acct = normalize_external_account(
        values.get('account_number') or values.get('external_account') or '',
        required=False,
    )
    return {
        'e2e': e2e,
        'rail': normalize_rail(values.get('rail') or 'osko'),
        'payid_type': kind,
        'payid': payid,
        'bsb': dest_bsb,
        'account_number': dest_acct,
        'abn': normalize_abn(values.get('abn'), required=False) if values.get('abn') else '',
        'amount': amount,
        'purpose': normalize_purpose(values.get('purpose')),
        'name': name or 'NPP PAYER',
        'currency': normalize_currency(values.get('currency')),
        'send_bsb': send,
        'recv_bsb': recv,
    }


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
            extra.add(date.fromisoformat(text[:10]) if '-' in text else datetime.strptime(text[:8], '%Y%m%d').date())
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
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def _env_money(name: str, default: str) -> Decimal:
    raw = os.environ.get(name)
    try:
        return parse_money(raw if raw not in (None, '') else default, allow_zero=True)
    except AmountError:
        return parse_money(default, allow_zero=True)


def _env_list(name: str) -> Tuple[str, ...]:
    raw = os.environ.get(name) or ''
    return tuple(item.strip() for item in raw.split(',') if item.strip())


def _classify_money_result(result: Any) -> str:
    text = str(result or '').strip().lower()
    if any(token in text for token in CREDIT_NSF):
        return 'nsf'
    if text in CREDIT_OK or 'debit' in text or 'credit' in text or 'success' in text:
        return 'ok'
    return 'failed'


@dataclass
class NrecvPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_claim: bool = True
    customer_return: bool = True
    allow_credit: bool = False
    max_keys: int = 12
    max_inbounds: int = 120
    min_amount: Decimal = Decimal('0.01')
    osko_cap: Decimal = Decimal('10000.00')
    npp_cap: Decimal = Decimal('100000.00')
    de_cap: Decimal = Decimal('100000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    tz_offset_minutes: int = AEST_OFFSET_MINUTES
    open_hour: int = 9
    cutoff_hour: int = 17
    receiver_bsb: str = DEFAULT_RECEIVER_BSB
    customer_return_seconds: int = CUSTOMER_NPP_RETURN_SECONDS
    staff_complaint_days: int = STAFF_COMPLAINT_DAYS
    claim_seconds: int = CLAIM_SECONDS
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()
    banks: Dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.banks is None:
            self.banks = dict(DEFAULT_BANKS)

    def cap_for(self, rail: str) -> Decimal:
        if rail == 'osko':
            return self.osko_cap
        if rail == 'de':
            return self.de_cap
        return self.npp_cap

    @classmethod
    def from_env(cls) -> 'NrecvPolicy':
        extra = _env_list('NRECV_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        source = os.environ.get('NRECV_RECEIVER') or DEFAULT_RECEIVER_BSB
        try:
            receiver = normalize_bsb(source)
        except NrecvError:
            receiver = DEFAULT_RECEIVER_BSB
        return cls(
            enabled=_env_bool('NRECV_ENABLED', True),
            customer_manage=_env_bool('NRECV_CUSTOMER_MANAGE', True),
            customer_claim=_env_bool('NRECV_CUSTOMER_CLAIM', True),
            customer_return=_env_bool('NRECV_CUSTOMER_RETURN', True),
            allow_credit=_env_bool('NRECV_ALLOW_CREDIT', False),
            max_keys=max(1, _env_int('NRECV_MAX_KEYS', 12)),
            max_inbounds=max(1, _env_int('NRECV_MAX_INBOUNDS', 120)),
            min_amount=_env_money('NRECV_MIN_AMOUNT', '0.01'),
            osko_cap=_env_money('NRECV_OSKO_CAP', '10000.00'),
            npp_cap=_env_money('NRECV_NPP_CAP', '100000.00'),
            de_cap=_env_money('NRECV_DE_CAP', '100000.00'),
            dual_control_threshold=_env_money('NRECV_DUAL_CONTROL', '10000.00'),
            tz_offset_minutes=_env_int('NRECV_TZ_OFFSET', AEST_OFFSET_MINUTES),
            open_hour=max(0, min(23, _env_int('NRECV_OPEN_HOUR', 9))),
            cutoff_hour=max(0, min(23, _env_int('NRECV_CUTOFF_HOUR', 17))),
            receiver_bsb=receiver,
            customer_return_seconds=max(60, _env_int('NRECV_RETURN_SECONDS', CUSTOMER_NPP_RETURN_SECONDS)),
            staff_complaint_days=max(1, _env_int('NRECV_COMPLAINT_DAYS', STAFF_COMPLAINT_DAYS)),
            claim_seconds=max(60, _env_int('NRECV_CLAIM_SECONDS', CLAIM_SECONDS)),
            watchlist=watch,
            extra_holidays=_env_list('NRECV_HOLIDAYS'),
        )


@dataclass
class NrecvKey:
    key_id: str
    userid: str
    nickname: str
    payid_type: str
    payid: str
    fingerprint: str
    destination_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'key_id': self.key_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'payid_type': self.payid_type,
            'payid_masked': mask_payid(self.payid, self.payid_type),
            'account_last4': last4(self.destination_account),
            'status': self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == KEY_ACTIVE,
            'paused': self.status == KEY_PAUSED,
            'cancelled': self.status == KEY_CANCELLED,
        }


@dataclass
class NrecvInbound:
    inbound_id: str
    e2e: str
    userid: str
    destination_account: str
    amount_aud: str
    sender_name: str
    send_bsb: str
    recv_bsb: str
    rail: str
    payid_type: str
    payid: str
    fingerprint: str
    bsb: str
    account_number: str
    purpose: str
    status: str
    reason: str
    actor: str
    releaser: str
    ofac_hit: bool
    ofac_match: str
    ofac_overridden: bool
    created_at: float
    updated_at: float
    posted_at: float = 0.0
    returned_at: float = 0.0
    expiry_at: float = 0.0
    note: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'inbound_id': self.inbound_id,
            'e2e_masked': mask_e2e(self.e2e),
            'userid': self.userid,
            'account_last4': last4(self.destination_account) if self.destination_account else '',
            'amount_aud': self.amount_aud,
            'sender_name': self.sender_name,
            'send_bsb_masked': mask_bsb(self.send_bsb) if self.send_bsb else '',
            'send_name': bank_name(self.send_bsb) if self.send_bsb else '',
            'recv_bsb_masked': mask_bsb(self.recv_bsb) if self.recv_bsb else '',
            'rail': self.rail,
            'payid_type': self.payid_type,
            'payid_masked': mask_payid(self.payid, self.payid_type) if self.payid else '****',
            'purpose': self.purpose,
            'status': self.status,
            'reason': self.reason,
            'ofac_hit': self.ofac_hit,
            'returnable': self.status in OPEN_IN or self.status == IN_POSTED,
            'claimable': self.status == IN_UNMATCHED,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'posted_at': self.posted_at,
            'expiry_at': self.expiry_at,
        }


def _key_from_row(row: Any) -> NrecvKey:
    data = dict(row)
    return NrecvKey(
        key_id=data['key_id'], userid=data['userid'], nickname=data['nickname'],
        payid_type=data['payid_type'], payid=data['payid'], fingerprint=data['fingerprint'],
        destination_account=data['destination_account'], status=data['status'], actor=data['actor'],
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
    )


def _inbound_from_row(row: Any) -> NrecvInbound:
    data = dict(row)
    return NrecvInbound(
        inbound_id=data['inbound_id'], e2e=data['e2e'], userid=data['userid'],
        destination_account=data['destination_account'], amount_aud=data['amount_aud'],
        sender_name=data['sender_name'], send_bsb=data['send_bsb'], recv_bsb=data['recv_bsb'],
        rail=data['rail'], payid_type=data['payid_type'], payid=data['payid'],
        fingerprint=data['fingerprint'], bsb=data.get('bsb') or '',
        account_number=data.get('account_number') or '', purpose=data['purpose'],
        status=data['status'], reason=data['reason'], actor=data['actor'], releaser=data['releaser'],
        ofac_hit=bool(int(data['ofac_hit'])), ofac_match=data['ofac_match'],
        ofac_overridden=bool(int(data['ofac_overridden'])),
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
        posted_at=float(data.get('posted_at') or 0), returned_at=float(data.get('returned_at') or 0),
        expiry_at=float(data.get('expiry_at') or 0), note=data.get('note') or '',
    )


class MemoryNrecvStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.keys: Dict[str, NrecvKey] = {}
        self.inbounds: Dict[str, NrecvInbound] = {}
        self._by_e2e: Dict[str, str] = {}

    def put_key(self, row: NrecvKey) -> None:
        with self._lock:
            self.keys[row.key_id] = row

    def get_key(self, key_id: str) -> Optional[NrecvKey]:
        return self.keys.get(key_id)

    def get_key_by_fingerprint(self, fingerprint: str) -> Optional[NrecvKey]:
        matches = [
            row for row in self.keys.values()
            if row.fingerprint == fingerprint and row.status in OPEN_KEY
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda row: row.created_at, reverse=True)[0]

    def update_key(self, row: NrecvKey) -> None:
        self.put_key(row)

    def list_keys(self, userid: Optional[str] = None) -> List[NrecvKey]:
        rows = list(self.keys.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        return sorted(rows, key=lambda row: row.created_at)

    def put_inbound(self, row: NrecvInbound) -> None:
        with self._lock:
            self.inbounds[row.inbound_id] = row
            self._by_e2e[row.e2e] = row.inbound_id

    def get_inbound(self, inbound_id: str) -> Optional[NrecvInbound]:
        return self.inbounds.get(inbound_id)

    def get_by_e2e(self, e2e: str) -> Optional[NrecvInbound]:
        inbound_id = self._by_e2e.get(e2e)
        return self.inbounds.get(inbound_id) if inbound_id else None

    def update_inbound(self, row: NrecvInbound) -> None:
        self.put_inbound(row)

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[NrecvInbound]:
        rows = list(self.inbounds.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)


class SqliteNrecvStore:
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
                CREATE TABLE IF NOT EXISTS keys (
                    key_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    payid_type TEXT NOT NULL,
                    payid TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    destination_account TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inbounds (
                    inbound_id TEXT PRIMARY KEY,
                    e2e TEXT NOT NULL UNIQUE,
                    userid TEXT NOT NULL DEFAULT '',
                    destination_account TEXT NOT NULL DEFAULT '',
                    amount_aud TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    send_bsb TEXT NOT NULL,
                    recv_bsb TEXT NOT NULL,
                    rail TEXT NOT NULL,
                    payid_type TEXT NOT NULL DEFAULT '',
                    payid TEXT NOT NULL DEFAULT '',
                    fingerprint TEXT NOT NULL DEFAULT '',
                    bsb TEXT NOT NULL DEFAULT '',
                    account_number TEXT NOT NULL DEFAULT '',
                    purpose TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    ofac_overridden INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    posted_at REAL NOT NULL DEFAULT 0,
                    returned_at REAL NOT NULL DEFAULT 0,
                    expiry_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_key(self, row: NrecvKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO keys (
                    key_id, userid, nickname, payid_type, payid, fingerprint,
                    destination_account, status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.key_id, row.userid, row.nickname, row.payid_type, row.payid,
                    row.fingerprint, row.destination_account, row.status, row.actor,
                    row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_key(self, key_id: str) -> Optional[NrecvKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM keys WHERE key_id = ?', (key_id,)).fetchone()
        return _key_from_row(row) if row else None

    def get_key_by_fingerprint(self, fingerprint: str) -> Optional[NrecvKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM keys WHERE fingerprint = ? AND status IN ('active', 'paused')
                ORDER BY created_at DESC LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
        return _key_from_row(row) if row else None

    def update_key(self, row: NrecvKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE keys SET nickname=?, payid_type=?, payid=?, fingerprint=?,
                    destination_account=?, status=?, actor=?, updated_at=?
                WHERE key_id=?
                """,
                (
                    row.nickname, row.payid_type, row.payid, row.fingerprint,
                    row.destination_account, row.status, row.actor, row.updated_at, row.key_id,
                ),
            )
            conn.commit()

    def list_keys(self, userid: Optional[str] = None) -> List[NrecvKey]:
        sql = 'SELECT * FROM keys'
        args: Tuple[Any, ...] = ()
        if userid is not None:
            sql += ' WHERE userid = ?'
            args = (userid,)
        sql += ' ORDER BY created_at'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_key_from_row(row) for row in rows]

    def put_inbound(self, row: NrecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inbounds (
                    inbound_id, e2e, userid, destination_account, amount_aud, sender_name,
                    send_bsb, recv_bsb, rail, payid_type, payid, fingerprint, bsb,
                    account_number, purpose, status, reason, actor, releaser, ofac_hit,
                    ofac_match, ofac_overridden, created_at, updated_at, posted_at,
                    returned_at, expiry_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.inbound_id, row.e2e, row.userid, row.destination_account, row.amount_aud,
                    row.sender_name, row.send_bsb, row.recv_bsb, row.rail, row.payid_type,
                    row.payid, row.fingerprint, row.bsb, row.account_number, row.purpose,
                    row.status, row.reason, row.actor, row.releaser, int(row.ofac_hit),
                    row.ofac_match, int(row.ofac_overridden), row.created_at, row.updated_at,
                    row.posted_at, row.returned_at, row.expiry_at, row.note,
                ),
            )
            conn.commit()

    def get_inbound(self, inbound_id: str) -> Optional[NrecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,)).fetchone()
        return _inbound_from_row(row) if row else None

    def get_by_e2e(self, e2e: str) -> Optional[NrecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE e2e = ?', (e2e,)).fetchone()
        return _inbound_from_row(row) if row else None

    def update_inbound(self, row: NrecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE inbounds SET userid=?, destination_account=?, amount_aud=?, sender_name=?,
                    send_bsb=?, recv_bsb=?, rail=?, payid_type=?, payid=?, fingerprint=?, bsb=?,
                    account_number=?, purpose=?, status=?, reason=?, actor=?, releaser=?, ofac_hit=?,
                    ofac_match=?, ofac_overridden=?, updated_at=?, posted_at=?, returned_at=?,
                    expiry_at=?, note=?
                WHERE inbound_id=?
                """,
                (
                    row.userid, row.destination_account, row.amount_aud, row.sender_name,
                    row.send_bsb, row.recv_bsb, row.rail, row.payid_type, row.payid, row.fingerprint,
                    row.bsb, row.account_number, row.purpose, row.status, row.reason, row.actor,
                    row.releaser, int(row.ofac_hit), row.ofac_match, int(row.ofac_overridden),
                    row.updated_at, row.posted_at, row.returned_at, row.expiry_at, row.note,
                    row.inbound_id,
                ),
            )
            conn.commit()

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[NrecvInbound]:
        clauses = []
        args: List[Any] = []
        if userid is not None:
            clauses.append('userid = ?')
            args.append(userid)
        if status is not None:
            clauses.append('status = ?')
            args.append(status)
        sql = 'SELECT * FROM inbounds'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        return [_inbound_from_row(row) for row in rows]


class NrecvService:
    def __init__(
        self,
        policy: NrecvPolicy,
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
        self.clock = clock or (lambda: datetime.now(tz=timezone.utc).timestamp())
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

    def _now(self) -> float:
        return float(self.clock())

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise NrecvError('nrecv_disabled', 'NPP receive is disabled.')

    def _require_staff(self, actor_type: str) -> None:
        if actor_type not in EMPLOYEE_ROLES:
            raise NrecvError('nrecv_forbidden', 'Staff only.')

    def _require_customer_manage(self, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if not self.policy.customer_manage:
            raise NrecvError('nrecv_forbidden', 'Customers cannot manage PayIDs.')

    def _accounts(self, userid: str) -> Any:
        if not self.accounts_fn:
            return {}
        return self.accounts_fn(userid) or {}

    def _owned(self, userid: str, account: str) -> None:
        owned = own_accounts_from_customer_payload(self._accounts(userid))
        if account not in owned:
            raise NrecvError('invalid_account', 'Destination is not an account of this customer.')

    def _account_type(self, userid: str, account: str) -> str:
        return account_types_from_customer_payload(self._accounts(userid)).get(account, '')

    def _credit_allowed(self, userid: str, account: str) -> bool:
        kind = self._account_type(userid, account)
        if kind == 'credit' and not self.policy.allow_credit:
            return False
        return True

    def add_key(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: str,
        payid_type: Any,
        payid: Any,
        destination_account: Any,
    ) -> NrecvKey:
        self._require_enabled()
        self._require_customer_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise NrecvError('nrecv_forbidden', 'Cannot enroll a PayID for another customer.')
        value, kind = normalize_payid(payid, payid_type=payid_type, required=True)
        account = normalize_account(destination_account)
        self._owned(owner_userid, account)
        if not self._credit_allowed(owner_userid, account):
            raise NrecvError('credit_not_allowed', 'Credit accounts cannot receive NPP.')
        fingerprint = payid_fingerprint(kind, value)
        existing = self.store.get_key_by_fingerprint(fingerprint)
        if existing and existing.status in OPEN_KEY:
            raise NrecvError('key_duplicate', 'That PayID is already enrolled.')
        open_rows = [row for row in self.store.list_keys(owner_userid) if row.status in OPEN_KEY]
        if any(row.nickname.lower() == normalize_nickname(nickname).lower() for row in open_rows):
            raise NrecvError('key_duplicate', 'That nickname is already in use.')
        if len(open_rows) >= self.policy.max_keys:
            raise NrecvError('key_limit', 'PayID enrollment limit reached.')
        now = self._now()
        row = NrecvKey(
            key_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=normalize_nickname(nickname),
            payid_type=kind,
            payid=value,
            fingerprint=fingerprint,
            destination_account=account,
            status=KEY_ACTIVE,
            actor=actor,
            created_at=now,
            updated_at=now,
        )
        self.store.put_key(row)
        self._rematch_unmatched(row)
        return row

    def set_key_status(
        self,
        *,
        key_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> NrecvKey:
        self._require_enabled()
        row = self.store.get_key(key_id)
        if row is None:
            raise NrecvError('key_not_found', 'PayID enrollment not found.')
        if actor_type not in EMPLOYEE_ROLES and actor != row.userid:
            raise NrecvError('nrecv_forbidden', 'Cannot change another customer PayID.')
        self._require_customer_manage(actor_type)
        if status not in KEY_STATUSES:
            raise NrecvError('invalid_status', 'Unknown PayID status.')
        if row.status == KEY_CANCELLED:
            raise NrecvError('already_cancelled', 'Cancelled PayIDs cannot change.')
        if status == KEY_PAUSED and row.status != KEY_ACTIVE:
            raise NrecvError('already_paused' if row.status == KEY_PAUSED else 'invalid_status', 'PayID is not active.')
        if status == KEY_ACTIVE and row.status != KEY_PAUSED:
            raise NrecvError('already_active', 'PayID is already active.')
        if status == row.status:
            code = 'already_paused' if status == KEY_PAUSED else 'already_active'
            raise NrecvError(code, 'PayID already in that status.')
        updated = replace(row, status=status, actor=actor, updated_at=self._now())
        self.store.update_key(updated)
        if status == KEY_ACTIVE:
            self._rematch_unmatched(updated)
        return updated

    def _rematch_unmatched(self, enrollment: NrecvKey) -> None:
        if enrollment.status != KEY_ACTIVE:
            return
        for inbound in self.store.list_inbounds(status=IN_UNMATCHED):
            if inbound.fingerprint != enrollment.fingerprint:
                continue
            inbound.userid = enrollment.userid
            inbound.destination_account = enrollment.destination_account
            inbound.updated_at = self._now()
            self.store.update_inbound(inbound)
            self._evaluate(inbound, rescreen=True)

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        values: Dict[str, Any],
    ) -> Tuple[NrecvInbound, bool]:
        self._require_enabled()
        self._require_staff(actor_type)
        parsed = message_from_values(values)
        return self._ingest_parsed(actor=actor, parsed=parsed)

    def ingest_file(
        self,
        *,
        actor: str,
        actor_type: str,
        payload: str,
        sender_name: str = '',
        send_bsb: str = '',
    ) -> Tuple[List[NrecvInbound], int]:
        self._require_enabled()
        self._require_staff(actor_type)
        created = 0
        rows = []
        fallback = ''
        if sender_name:
            fallback = normalize_legal_name(sender_name)
        sender = ''
        if send_bsb:
            sender = normalize_bsb(send_bsb, required=False)
        for record in split_npp1_file(payload):
            parsed = parse_npp1(record)
            parsed['recv_bsb'] = parsed.get('bsb') or self.policy.receiver_bsb
            parsed['send_bsb'] = sender or '013000'
            if fallback:
                parsed['name'] = fallback
            elif not parsed.get('name'):
                parsed['name'] = 'NPP PAYER'
            row, is_new = self._ingest_parsed(actor=actor, parsed=parsed)
            rows.append(row)
            if is_new:
                created += 1
        return rows, created

    def _ingest_parsed(self, *, actor: str, parsed: Dict[str, Any]) -> Tuple[NrecvInbound, bool]:
        recv = parsed.get('recv_bsb') or parsed.get('bsb') or self.policy.receiver_bsb
        if recv != self.policy.receiver_bsb:
            raise NrecvError('wrong_receiver', 'NPP credit is not for this BSB.')
        existing = self.store.get_by_e2e(parsed['e2e'])
        if existing is not None:
            return existing, False
        total = len(self.store.list_inbounds())
        if total >= self.policy.max_inbounds:
            raise NrecvError('inbound_limit', 'Inbound NPP limit reached.')
        amount = parsed['amount'] if isinstance(parsed['amount'], Decimal) else parse_money(parsed['amount'])
        rail = normalize_rail(parsed.get('rail') or 'osko')
        cap = self.policy.cap_for(rail)
        if amount < self.policy.min_amount or amount > cap:
            code = 'npp_amount_exceeded' if amount > cap else 'amount_out_of_range'
            raise NrecvError(code, 'Amount is outside the %s range.' % rail)
        now = self._now()
        payid = parsed.get('payid') or ''
        kind = parsed.get('payid_type') or ''
        fingerprint = payid_fingerprint(kind, payid) if payid else ''
        row = NrecvInbound(
            inbound_id=uuid.uuid4().hex,
            e2e=parsed['e2e'],
            userid='',
            destination_account='',
            amount_aud=money_str(amount),
            sender_name=parsed.get('name') or 'NPP PAYER',
            send_bsb=parsed.get('send_bsb') or '',
            recv_bsb=recv,
            rail=rail,
            payid_type=kind,
            payid=payid,
            fingerprint=fingerprint,
            bsb=parsed.get('bsb') or recv,
            account_number=parsed.get('account_number') or '',
            purpose=parsed.get('purpose') or 'other',
            status=IN_UNMATCHED,
            reason='',
            actor=actor,
            releaser='',
            ofac_hit=False,
            ofac_match='',
            ofac_overridden=False,
            created_at=now,
            updated_at=now,
            expiry_at=now + self.policy.claim_seconds,
        )
        enrollment = None
        if fingerprint:
            enrollment = self.store.get_key_by_fingerprint(fingerprint)
        if enrollment is not None and enrollment.status == KEY_ACTIVE:
            row.userid = enrollment.userid
            row.destination_account = enrollment.destination_account
        self.store.put_inbound(row)
        self._evaluate(row, rescreen=True)
        return row, True

    def _evaluate(self, row: NrecvInbound, *, rescreen: bool) -> NrecvInbound:
        now = self._now()
        if row.status in {IN_POSTED, IN_REJECTED, IN_RETURNED, IN_EXPIRED, IN_FAILED}:
            return row
        if row.status == IN_UNMATCHED and row.expiry_at and now > row.expiry_at:
            row.status = IN_EXPIRED
            row.reason = 'expired'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if not row.userid or not row.destination_account:
            row.status = IN_UNMATCHED
            row.reason = 'unmatched'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        try:
            self._owned(row.userid, row.destination_account)
        except NrecvError:
            row.status = IN_UNMATCHED
            row.reason = 'unmatched'
            row.userid = ''
            row.destination_account = ''
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if not self._credit_allowed(row.userid, row.destination_account):
            row.status = IN_UNMATCHED
            row.reason = 'credit_not_allowed'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if rescreen and not row.ofac_overridden:
            hit = self.screen_fn(row.sender_name, watchlist=self.policy.watchlist)
            row.ofac_hit = bool(hit.hit)
            row.ofac_match = hit.matched
            if row.ofac_hit:
                row.status = IN_HELD
                row.reason = 'ofac'
                row.updated_at = now
                self.store.update_inbound(row)
                return row
        elif row.ofac_hit and not row.ofac_overridden:
            row.status = IN_HELD
            row.reason = 'ofac'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        aud = parse_money(row.amount_aud)
        if aud >= self.policy.dual_control_threshold and not row.releaser:
            row.status = IN_PENDING
            row.reason = 'dual_control'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if row.rail == 'de' and not self.calendar.is_session(now):
            row.status = IN_QUEUED
            row.reason = 'de_session'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        return self._post(row)

    def _post(self, row: NrecvInbound) -> NrecvInbound:
        if self.credit_fn is None:
            raise NrecvError('failed', 'Credit function is not configured.')
        remark = 'npp from %s' % row.sender_name[:40]
        result = self.credit_fn(row.destination_account, row.amount_aud, remark)
        classified = _classify_money_result(result)
        now = self._now()
        if classified == 'nsf':
            row.status = IN_FAILED
            row.reason = 'nsf'
            row.updated_at = now
            self.store.update_inbound(row)
            raise NrecvError('nsf', 'Credit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            row.status = IN_FAILED
            row.reason = 'failed'
            row.updated_at = now
            self.store.update_inbound(row)
            raise NrecvError('failed', 'Credit failed.', inbound=row)
        row.status = IN_POSTED
        row.reason = ''
        row.posted_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def preview(self, *, inbound_id: str = '', e2e: str = '', actor: str = '', actor_type: str = '') -> Dict[str, Any]:
        row = self._get(inbound_id=inbound_id, e2e=e2e)
        self._assert_visible(row, actor, actor_type)
        return {
            'inbound': row.to_dict(),
            'clock': self.calendar.snapshot(self._now()),
            'receiver': mask_bsb(self.policy.receiver_bsb),
        }

    def _get(self, *, inbound_id: str = '', e2e: str = '') -> NrecvInbound:
        row = None
        if inbound_id:
            row = self.store.get_inbound(str(inbound_id).strip())
        if row is None and e2e:
            row = self.store.get_by_e2e(normalize_e2e(e2e))
        if row is None:
            raise NrecvError('inbound_not_found', 'Inbound NPP not found.')
        return row

    def _assert_visible(self, row: NrecvInbound, actor: str, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if row.userid and row.userid != actor:
            raise NrecvError('nrecv_forbidden', 'Cannot view another customer NPP.')

    def assign(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        customer_id: str,
        account: Any,
        nickname: str = '',
    ) -> NrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_UNMATCHED:
            raise NrecvError('not_assignable', 'Only unmatched NPP credits can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise NrecvError('missing_customer_id', 'Customer id is required to assign.')
        dest = normalize_account(account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise NrecvError('credit_not_allowed', 'Credit accounts cannot receive NPP.')
        if row.payid:
            existing = self.store.get_key_by_fingerprint(row.fingerprint)
            if existing is None or existing.status == KEY_CANCELLED:
                self.add_key(
                    owner_userid=owner, actor=actor, actor_type=actor_type,
                    nickname=nickname or ('NPP %s' % (row.payid_type or 'payid')),
                    payid_type=row.payid_type, payid=row.payid, destination_account=dest,
                )
                fresh = self.store.get_inbound(row.inbound_id)
                if fresh is not None and fresh.status != IN_UNMATCHED:
                    return fresh
            elif existing.userid != owner:
                raise NrecvError('key_duplicate', 'PayID belongs to another customer.')
        row.userid = owner
        row.destination_account = dest
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=True)

    def claim(
        self,
        *,
        actor: str,
        actor_type: str,
        inbound_id: str = '',
        payid_type: Any = '',
        payid: Any = '',
        destination_account: Any = '',
        nickname: str = '',
        owner_userid: str = '',
    ) -> NrecvInbound:
        """PayID claim: customer presents the matching PayID for an unmatched NPP."""
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_claim:
                raise NrecvError('nrecv_forbidden', 'Customers cannot claim NPP credits.')
            owner = actor
        else:
            owner = str(owner_userid or '').strip()
            if not owner:
                raise NrecvError('missing_customer_id', 'Customer id is required to claim.')
        if not payid:
            raise NrecvError('missing_key', 'PayID is required to claim.')
        value, kind = normalize_payid(payid, payid_type=payid_type, required=True)
        fingerprint = payid_fingerprint(kind, value)
        dest = normalize_account(destination_account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise NrecvError('credit_not_allowed', 'Credit accounts cannot receive NPP.')
        row = None
        if inbound_id:
            row = self._get(inbound_id=inbound_id)
            if row.fingerprint != fingerprint:
                raise NrecvError('not_claimable', 'Presented PayID does not match this NPP.')
        else:
            for candidate in self.store.list_inbounds(status=IN_UNMATCHED):
                if candidate.fingerprint == fingerprint:
                    row = candidate
                    break
        if row is None:
            raise NrecvError('inbound_not_found', 'No unmatched NPP for that PayID.')
        if row.status != IN_UNMATCHED:
            raise NrecvError('not_claimable', 'This NPP credit is not waiting for a PayID claim.')
        if row.expiry_at and self._now() > row.expiry_at:
            row.status = IN_EXPIRED
            row.reason = 'expired'
            row.updated_at = self._now()
            self.store.update_inbound(row)
            raise NrecvError('return_window_closed', 'PayID claim window has closed.')
        existing = self.store.get_key_by_fingerprint(fingerprint)
        if existing is None or existing.status == KEY_CANCELLED:
            self.add_key(
                owner_userid=owner, actor=actor, actor_type=actor_type,
                nickname=nickname or ('NPP %s' % kind),
                payid_type=kind, payid=value, destination_account=dest,
            )
        elif existing.userid != owner:
            raise NrecvError('key_duplicate', 'PayID belongs to another customer.')
        elif existing.status == KEY_PAUSED:
            self.set_key_status(key_id=existing.key_id, actor=actor, actor_type=actor_type, status=KEY_ACTIVE)
        fresh = self.store.get_inbound(row.inbound_id)
        if fresh is not None and fresh.status != IN_UNMATCHED:
            return fresh
        row.userid = owner
        row.destination_account = dest
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=True)

    def override_ofac(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> NrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_HELD:
            raise NrecvError('not_overridable', 'Only OFAC-held NPP credits can be overridden.')
        row.ofac_overridden = True
        row.ofac_hit = False
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def release(self, *, inbound_id: str, actor: str, actor_type: str) -> NrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_PENDING:
            raise NrecvError('not_releasable', 'Only dual-control NPP credits can be released.')
        if actor == row.actor:
            raise NrecvError('same_approver', 'A different employee must release this credit.')
        row.releaser = actor
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def reject(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> NrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status not in OPEN_IN:
            raise NrecvError('not_rejectable', 'This NPP credit cannot be rejected.')
        row.status = IN_REJECTED
        row.reason = 'rejected'
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return row

    def export(self, *, inbound_id: str, actor: str, actor_type: str) -> Dict[str, Any]:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if not row.e2e:
            raise NrecvError('not_exportable', 'NPP credit has no End-to-End ID to export.')
        _ = actor
        record = compose_npp1(
            e2e=row.e2e,
            rail=row.rail,
            payid_type=row.payid_type,
            payid=row.payid,
            bsb=row.bsb or row.recv_bsb,
            account_number=row.account_number,
            abn=row.payid if row.payid_type == 'abn' else '',
            amount=parse_money(row.amount_aud, allow_zero=True),
            purpose=row.purpose,
            status=row.status,
            mask=True,
        )
        return {'file': record, 'inbound': row.to_dict()}

    def return_inbound(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: str = 'cust',
    ) -> NrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        return self._return(row, actor=actor, actor_type=actor_type, reason=reason, honor_window=False)

    def request_return(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        reason: str = 'cust',
    ) -> NrecvInbound:
        self._require_enabled()
        row = self._get(inbound_id=inbound_id)
        self._assert_visible(row, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_return:
                raise NrecvError('nrecv_forbidden', 'Customers cannot return NPP credits.')
            if actor != row.userid:
                raise NrecvError('nrecv_forbidden', 'Cannot return another customer NPP.')
        return self._return(row, actor=actor, actor_type=actor_type, reason=reason, honor_window=True)

    def _customer_deadline(self, row: NrecvInbound) -> float:
        posted = row.posted_at or row.created_at
        if row.rail == 'de':
            day = self.calendar.local_dt(posted).date()
            return self.calendar.session_close_ts(self.calendar.next_business_day(day))
        return posted + self.policy.customer_return_seconds

    def _return(
        self,
        row: NrecvInbound,
        *,
        actor: str,
        actor_type: str,
        reason: str,
        honor_window: bool,
    ) -> NrecvInbound:
        now = self._now()
        _ = actor
        if row.status == IN_RETURNED or row.returned_at:
            raise NrecvError('already_returned', 'NPP credit already returned.')
        if row.status in OPEN_IN:
            row.status = IN_RETURNED
            row.reason = reason or 'cust'
            row.returned_at = now
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if row.status != IN_POSTED:
            raise NrecvError('not_returnable', 'This NPP credit cannot be returned.')
        if honor_window and actor_type not in EMPLOYEE_ROLES:
            if now > self._customer_deadline(row):
                raise NrecvError('return_window_closed', 'Customer NPP return window has closed.')
        if actor_type in EMPLOYEE_ROLES:
            elapsed = now - (row.posted_at or row.created_at)
            if elapsed > self.policy.staff_complaint_days * 86400:
                raise NrecvError('med_window_closed', 'Staff complaint window is 90 days.')
        if self.debit_fn is None:
            raise NrecvError('return_failed', 'Debit function is not configured.')
        remark = 'npp return %s' % row.e2e[-12:]
        result = self.debit_fn(row.destination_account, row.amount_aud, remark)
        classified = _classify_money_result(result)
        if classified == 'nsf':
            raise NrecvError('nsf', 'Return debit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            raise NrecvError('return_failed', 'Return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.reason = reason or 'cust'
        row.returned_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[NrecvInbound]:
        now = self._now()
        changed = []
        rows = self.store.list_inbounds(userid) if userid else self.store.list_inbounds()
        for row in rows:
            if row.status == IN_UNMATCHED and row.expiry_at and now > row.expiry_at:
                row.status = IN_EXPIRED
                row.reason = 'expired'
                row.updated_at = now
                self.store.update_inbound(row)
                changed.append(row)
            elif row.status == IN_QUEUED:
                updated = self._evaluate(row, rescreen=False)
                if updated.status != IN_QUEUED:
                    changed.append(updated)
        return changed

    def snapshot(self, userid: str, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        _ = actor
        _ = actor_type
        now = self._now()
        if userid:
            self.run_due(userid)
        keys = self.store.list_keys(userid) if userid else []
        inbounds = self.store.list_inbounds(userid) if userid else []
        received = [row for row in inbounds if row.status == IN_POSTED]
        ytd = sum((parse_money(row.amount_aud, allow_zero=True) for row in received), Decimal('0.00'))
        return {
            'enabled': self.policy.enabled,
            'clock': self.calendar.snapshot(now),
            'receiver': mask_bsb(self.policy.receiver_bsb),
            'keys': [row.to_dict() for row in keys[:40]],
            'inbounds': [row.to_dict() for row in inbounds[:40]],
            'ytd_received': money_str(ytd),
            'open_count': sum(1 for row in inbounds if row.status in OPEN_IN),
            'active_keys': sum(1 for row in keys if row.status == KEY_ACTIVE),
            'min_amount': money_str(self.policy.min_amount),
            'osko_cap': money_str(self.policy.osko_cap),
            'npp_cap': money_str(self.policy.npp_cap),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'settlement_method': 'npp',
            'currency': 'AUD',
        }

    def unmatched_snapshot(self) -> Dict[str, Any]:
        rows = self.store.list_inbounds(status=IN_UNMATCHED)
        return {
            'enabled': self.policy.enabled,
            'unmatched': [row.to_dict() for row in rows[:40]],
            'count': len(rows),
        }


_SERVICE: Optional[NrecvService] = None


def set_service(service: Optional[NrecvService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[NrecvService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('NRECV_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryNrecvStore()
    path = os.environ.get('NRECV_DB', DEFAULT_STORE_PATH)
    return SqliteNrecvStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[AusClock] = None,
) -> NrecvService:
    if store is None:
        store = default_store()
    return NrecvService(
        NrecvPolicy.from_env(),
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


def _owner_from(actor: str, actor_type: str, values: Dict[str, Any]) -> str:
    if actor_type in EMPLOYEE_ROLES:
        return str(values.get('customer_id') or values.get('owner') or '').strip()
    return actor


def _error_status(code: str) -> int:
    return {
        'key_duplicate': 409,
        'key_limit': 409,
        'inbound_limit': 409,
        'already_cancelled': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_returned': 409,
        'nsf': 409,
        'failed': 409,
        'return_failed': 409,
        'nrecv_forbidden': 403,
        'nrecv_disabled': 403,
        'credit_not_allowed': 403,
        'same_approver': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_returnable': 403,
        'not_claimable': 403,
        'not_exportable': 403,
        'return_window_closed': 403,
        'med_window_closed': 403,
        'key_not_found': 404,
        'inbound_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_bsb': 400,
        'invalid_payid': 400,
        'invalid_phone': 400,
        'invalid_abn': 400,
        'invalid_acn': 400,
        'invalid_rail': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_reference': 400,
        'invalid_file': 400,
        'invalid_currency': 400,
        'wrong_receiver': 400,
        'npp_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_key': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: NrecvError) -> Dict[str, Any]:
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
    except NrecvError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snapshot_for(service: NrecvService, userid: str, actor: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(userid, actor=actor, actor_type=actor_type)


def handle_list(service: NrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = _owner_from(userid, actor_type, values)
        if not owner:
            return jsonify({'message': 'Customer id required', 'error': 'missing_customer_id'}), 400
    return jsonify({'NppIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def handle_unmatched(service: NrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'nrecv_forbidden'}), 403
    snap = service.unmatched_snapshot()
    return jsonify({'UnmatchedNppIns': snap, 'NppIn': snap}), 200


def handle_add_key(service: NrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        owner = userid
        if actor_type in EMPLOYEE_ROLES:
            owner = _owner_from(userid, actor_type, values)
            if not owner:
                raise NrecvError('missing_customer_id', 'Customer id is required.')
        row = service.add_key(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname') or '',
            payid_type=values.get('payid_type') or values.get('key_type') or values.get('kind'),
            payid=values.get('payid') or values.get('key') or values.get('key_value'),
            destination_account=values.get('destination_account') or values.get('account') or values.get('default_account'),
        )
        return jsonify({
            'message': 'PayID enrolled',
            'key': row.to_dict(),
            'NppIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _key_status_route(service: NrecvService, status: str, message: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        key_id = str(values.get('key_id') or '').strip()
        if not key_id:
            raise NrecvError('missing_key', 'key_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.set_key_status(key_id=key_id, actor=userid, actor_type=actor_type, status=status)
        return jsonify({
            'message': message,
            'key': row.to_dict(),
            'NppIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: NrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        preview = service.preview(
            inbound_id=str(values.get('inbound_id') or ''),
            e2e=str(values.get('end_to_end_id') or values.get('e2e') or ''),
            actor=userid,
            actor_type=actor_type,
        )
        owner = preview['inbound'].get('userid') or userid
        return jsonify({'preview': preview, 'NppIn': _snapshot_for(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest(service: NrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'NPP ingested' if created else 'NPP already ingested',
            'inbound': row.to_dict(),
            'NppIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_ingest_file(service: NrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        payload = values.get('file') or values.get('body') or values.get('payload')
        if not payload:
            raise NrecvError('missing_file', 'NPP1 file body is required.')
        actor_type = session.get('usertype') or 'customer'
        rows, created = service.ingest_file(
            actor=userid, actor_type=actor_type, payload=str(payload),
            sender_name=str(values.get('name') or values.get('sender_name') or ''),
            send_bsb=str(values.get('send_bsb') or values.get('sender_bsb') or ''),
        )
        owner = rows[-1].userid if rows and rows[-1].userid else userid
        return jsonify({
            'message': 'NPP1 file ingested',
            'created': created,
            'inbounds': [row.to_dict() for row in rows],
            'NppIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_assign(service: NrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise NrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.assign(
            inbound_id=inbound_id,
            actor=userid,
            actor_type=actor_type,
            customer_id=str(values.get('customer_id') or ''),
            account=values.get('account') or values.get('destination_account'),
            nickname=str(values.get('nickname') or ''),
        )
        return jsonify({
            'message': 'NPP assigned',
            'inbound': row.to_dict(),
            'NppIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_claim(service: NrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        row = service.claim(
            actor=userid,
            actor_type=actor_type,
            inbound_id=str(values.get('inbound_id') or ''),
            payid_type=values.get('payid_type') or values.get('key_type') or values.get('kind'),
            payid=values.get('payid') or values.get('key') or values.get('key_value'),
            destination_account=values.get('destination_account') or values.get('account'),
            nickname=str(values.get('nickname') or ''),
            owner_userid=str(values.get('customer_id') or ''),
        )
        return jsonify({
            'message': 'NPP claimed',
            'inbound': row.to_dict(),
            'NppIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_inbound_route(service: NrecvService, action: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise NrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        if action == 'override':
            row = service.override_ofac(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'release':
            row = service.release(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            message = 'NPP released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'NPP rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=str(values.get('reason') or 'cust'),
            )
            message = 'NPP returned'
        elif action == 'export':
            exported = service.export(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            owner = exported['inbound'].get('userid') or userid
            return jsonify({
                'message': 'NPP exported',
                'file': exported['file'],
                'inbound': exported['inbound'],
                'NppIn': _snapshot_for(service, owner, userid, actor_type),
            }), 200
        else:
            raise NrecvError('invalid_status', 'Unknown NPP action.')
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'NppIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: NrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise NrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.request_return(
            inbound_id=inbound_id, actor=userid, actor_type=actor_type,
            reason=str(values.get('reason') or 'cust'),
        )
        return jsonify({
            'message': 'NPP return requested',
            'inbound': row.to_dict(),
            'NppIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: NrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'NppIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def attach_nrecv_routes(app, service: NrecvService) -> None:
    @app.route('/listNppIns', methods=['POST', 'GET'])
    def list_npp_ins_route():
        return handle_list(service)

    @app.route('/listNppRecvKeys', methods=['POST', 'GET'])
    def list_npp_recv_keys_route():
        return handle_list(service)

    @app.route('/listUnmatchedNppIns', methods=['POST', 'GET'])
    def list_unmatched_npp_ins_route():
        return handle_unmatched(service)

    @app.route('/addNppRecvKey', methods=['POST', 'GET'])
    def add_npp_recv_key_route():
        return handle_add_key(service)

    @app.route('/pauseNppRecvKey', methods=['POST', 'GET'])
    def pause_npp_recv_key_route():
        return _key_status_route(service, KEY_PAUSED, 'PayID paused')

    @app.route('/resumeNppRecvKey', methods=['POST', 'GET'])
    def resume_npp_recv_key_route():
        return _key_status_route(service, KEY_ACTIVE, 'PayID resumed')

    @app.route('/cancelNppRecvKey', methods=['POST', 'GET'])
    def cancel_npp_recv_key_route():
        return _key_status_route(service, KEY_CANCELLED, 'PayID cancelled')

    @app.route('/previewNppIn', methods=['POST', 'GET'])
    def preview_npp_in_route():
        return handle_preview(service)

    @app.route('/ingestNppIn', methods=['POST', 'GET'])
    def ingest_npp_in_route():
        return handle_ingest(service)

    @app.route('/ingestNppInFile', methods=['POST', 'GET'])
    def ingest_npp_in_file_route():
        return handle_ingest_file(service)

    @app.route('/claimNppIn', methods=['POST', 'GET'])
    def claim_npp_in_route():
        return handle_claim(service)

    @app.route('/assignNppIn', methods=['POST', 'GET'])
    def assign_npp_in_route():
        return handle_assign(service)

    @app.route('/overrideNppInOfac', methods=['POST', 'GET'])
    def override_npp_in_ofac_route():
        return _staff_inbound_route(service, 'override')

    @app.route('/releaseNppIn', methods=['POST', 'GET'])
    def release_npp_in_route():
        return _staff_inbound_route(service, 'release')

    @app.route('/rejectNppIn', methods=['POST', 'GET'])
    def reject_npp_in_route():
        return _staff_inbound_route(service, 'reject')

    @app.route('/returnNppIn', methods=['POST', 'GET'])
    def return_npp_in_route():
        return _staff_inbound_route(service, 'return')

    @app.route('/exportNppIn', methods=['POST', 'GET'])
    def export_npp_in_route():
        return _staff_inbound_route(service, 'export')

    @app.route('/requestNppInReturn', methods=['POST', 'GET'])
    def request_npp_in_return_route():
        return handle_request_return(service)

    @app.route('/runDueNppIns', methods=['POST', 'GET'])
    def run_due_npp_ins_route():
        return handle_run_due(service)
