"""Inbound PayNow / FAST / MEPS+ / GIRO receive posting.

Staff ingest incoming Singapore credits. Recipients enroll PayNow
proxies (NRIC/FIN, +65 mobile, UEN) at this bank's MAS code. Matched
PayNow and FAST credits post instantly 24/7. MEPS+ waits for the
09:00–17:30 SGT MAS session. GIRO waits for the next MAS banking day.
Unmatched proxies wait for a customer claim or staff assignment.
Independent of outbound PayNow origination (PR #180), inbound NPP
(PR #178), domestic Fedwire (PR #73), and ACH linking (PR #68).
Existing `/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay
unchanged.

Foundations (reusable beyond this screen):
- Singapore NRIC/FIN checksum (S/T/F/G/M prefix + weighted remainder)
- UEN n8X check letter (ACRA-style weighted modulus)
- +65 PayNow mobile (8-digit 8/9 national)
- MAS 4-digit participating bank codes
- FAST End-to-End ID (`FAST`/`PNW`/`MEPS`/`GIRO` + YYYYMMDD + seq)
- FAST1 14-pipe file (XML/DOCTYPE and non-FAST clearing rejected)
- SgClock SGT UTC+8: PayNow/FAST 24/7; MEPS+ 09:00–17:30; GIRO next MAS day
- SGDUSD quote book converting instructed SGD to USD ledger credit
- PayNow claim (customer presents matching proxy; enroll rematches unmatched)
- Return windows (PayNow/FAST 24h customer / GIRO+MEPS next MAS day / 90-day staff)
- Dual-control at SGD 10k; FAST/PayNow cap 200k / MEPS 10m

Reuses Fedwire OFAC screening, money parsing, and account directory
from `utility.wire`. Stores are pluggable (memory for tests, sqlite WAL
for restart-safe default). Full NRIC, UEN, mobile, account,
fingerprint, and E2E ID never appear in to_dict / snapshots.
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
    'iras': 'tax', 'gst': 'tax', 'taxes': 'tax',
    'mortgage': 'loan', 'housing': 'rent', 'hdb': 'rent',
}
RAILS = frozenset({'paynow', 'fast', 'meps', 'giro'})
RAIL_ALIASES = {
    'pay_now': 'paynow', 'overlay': 'paynow', 'pnw': 'paynow',
    'fast_and_secure': 'fast', 'instant': 'fast', 'a2a': 'fast',
    'mepsplus': 'meps', 'meps_plus': 'meps', 'rtgs': 'meps',
    'ibg': 'giro', 'interbank_giro': 'giro', 'bulk': 'giro',
}
PROXY_TYPES = frozenset({'nric', 'phone', 'uen'})
PROXY_ALIASES = {
    'fin': 'nric', 'nricfin': 'nric', 'nric_fin': 'nric', 'id': 'nric',
    'mobile': 'phone', 'msisdn': 'phone', 'tel': 'phone', 'sms': 'phone',
    'entity': 'uen', 'company': 'uen', 'business': 'uen',
}
CLEARING_BY_RAIL = {'paynow': 'PAYNOW', 'fast': 'FAST', 'meps': 'MEPS', 'giro': 'GIRO'}
REJECTED_CLEARING = frozenset({
    'AUNPP', 'USRTPP', 'FEDNOW', 'FEDWIRE', 'ACH', 'UPI', 'CHIPS', 'SWIFT',
    'PIX', 'SPEI', 'NPP', 'OSKO',
})
DEFAULT_BANKS = {
    '7171': 'DBS',
    '7375': 'OCBC',
    '7232': 'UOB',
    '7302': 'STANDARD CHARTERED',
    '7339': 'CITIBANK',
    '7047': 'MAYBANK',
    '7083': 'HSBC',
    '7214': 'BANK OF CHINA',
    '9092': 'KONOHA BANK',
}
NRIC_WEIGHTS = (2, 7, 6, 5, 4, 3, 2)
NRIC_ST = 'JZIHGFEDCBA'
NRIC_FG = 'XWUTRQPNMLK'
NRIC_M = 'KCLGJTUSQPB'
UEN_WEIGHTS = (10, 8, 6, 4, 2, 9, 7, 5)
UEN_LETTERS = 'ABCDEFGHJKZ'
FAST1_VERSION = '1'
FAST1_FIELDS = 14
DEFAULT_STORE_PATH = 'SystemLogs/sgrecv.sqlite'
DEFAULT_RECEIVER_BANK = '9092'
DEFAULT_SENDER_BANK = '7171'
CUSTOMER_RETURN_SECONDS = 24 * 60 * 60
STAFF_COMPLAINT_DAYS = 90
CLAIM_SECONDS = 24 * 60 * 60
SGT_OFFSET_MINUTES = 480
DEFAULT_SGDUSD = Decimal('0.74')
BLOCKED_E2E_PREFIXES = (
    'IMAD', 'UETR', 'NPP', 'AUNPP', 'RRN', 'STAN', 'ARN', 'RFI', 'RFR', 'RTR',
)
E2E_PREFIX = {'paynow': 'PNW', 'fast': 'FAST', 'meps': 'MEPS', 'giro': 'GIRO'}
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)


class SgrecvError(ValueError):
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
        raise SgrecvError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise SgrecvError('invalid_name', 'Sender legal name must be 2-80 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise SgrecvError('invalid_purpose', 'Unknown PayNow purpose.')
    return text


def normalize_currency(value: Any, *, default: str = 'SGD') -> str:
    text = str(value or default).strip().upper().replace('$', '')
    if text in {'S', 'S$', 'SGD.', 'SG'}:
        text = 'SGD'
    if text != 'SGD':
        raise SgrecvError('invalid_currency', 'PayNow / FAST / MEPS+ / GIRO is SGD only.')
    return 'SGD'


def normalize_rail(value: Any, *, default: str = 'paynow') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text not in RAILS:
        raise SgrecvError('invalid_rail', 'Rail must be paynow, fast, meps, or giro.')
    return text


def normalize_proxy_type(value: Any, *, default: str = '') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PROXY_ALIASES.get(text, text)
    if not text:
        return ''
    if text not in PROXY_TYPES:
        raise SgrecvError('invalid_proxy', 'PayNow proxy must be nric, phone, or uen.')
    return text


def normalize_bank_code(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise SgrecvError('invalid_bank', 'MAS bank code is required.')
        return ''
    if len(digits) != 4 or digits == '0000':
        raise SgrecvError('invalid_bank', 'MAS bank code must be four digits.')
    return digits


def mask_bank(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 4:
        return '****'
    return digits[:2] + '**'


def bank_name(value: Any, *, directory: Optional[Dict[str, str]] = None) -> str:
    try:
        code = normalize_bank_code(value)
    except SgrecvError:
        return ''
    mapping = directory if directory is not None else DEFAULT_BANKS
    return mapping.get(code, '')


def normalize_external_account(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise SgrecvError('invalid_account', 'Account is required.')
        return ''
    if not (6 <= len(digits) <= 16):
        raise SgrecvError('invalid_account', 'Singapore account must be 6-16 digits.')
    return digits


def mask_account(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return ('*' * max(0, len(digits) - 4)) + digits[-4:]


def nric_check_letter(prefix: str, body: str) -> str:
    prefix = prefix.upper()
    if prefix not in {'S', 'T', 'F', 'G', 'M'} or len(body) != 7 or not body.isdigit():
        raise SgrecvError('invalid_nric', 'NRIC/FIN must be prefix + 7 digits.')
    total = sum(int(ch) * w for ch, w in zip(body, NRIC_WEIGHTS))
    if prefix == 'T':
        total += 4
    elif prefix == 'G':
        total += 4
    elif prefix == 'M':
        total += 3
    remainder = total % 11
    if prefix in {'S', 'T'}:
        return NRIC_ST[remainder]
    if prefix in {'F', 'G'}:
        return NRIC_FG[remainder]
    return NRIC_M[remainder]


def nric_check_ok(value: str) -> bool:
    text = str(value or '').strip().upper()
    if not re.fullmatch(r'[STFGM]\d{7}[A-Z]', text):
        return False
    try:
        return nric_check_letter(text[0], text[1:8]) == text[8]
    except SgrecvError:
        return False


def compose_nric(prefix: Any, body: Any) -> str:
    letter = str(prefix or '').strip().upper()
    digits = ''.join(ch for ch in str(body or '') if ch.isdigit())
    return letter + digits + nric_check_letter(letter, digits)


def normalize_nric(value: Any, *, required: bool = False) -> str:
    text = re.sub(r'[\s-]', '', str(value or '')).upper()
    if not text:
        if required:
            raise SgrecvError('invalid_nric', 'NRIC/FIN is required.')
        return ''
    if not nric_check_ok(text):
        raise SgrecvError('invalid_nric', 'NRIC/FIN failed the checksum.')
    return text


def mask_nric(value: Any) -> str:
    text = re.sub(r'[\s-]', '', str(value or '')).upper()
    if len(text) != 9:
        return 'S*******X'
    return text[0] + '****' + text[-4:]


def uen_check_letter(body: str) -> str:
    digits = ''.join(ch for ch in str(body or '') if ch.isdigit())
    if len(digits) != 8:
        raise SgrecvError('invalid_uen', 'UEN body must be 8 digits.')
    total = sum(int(ch) * w for ch, w in zip(digits, UEN_WEIGHTS))
    return UEN_LETTERS[total % 11]


def uen_check_ok(value: str) -> bool:
    text = str(value or '').strip().upper()
    if not re.fullmatch(r'\d{8}[A-Z]', text):
        return False
    try:
        return uen_check_letter(text[:8]) == text[8]
    except SgrecvError:
        return False


def compose_uen(body: Any) -> str:
    digits = ''.join(ch for ch in str(body or '') if ch.isdigit())
    return digits + uen_check_letter(digits)


def normalize_uen(value: Any, *, required: bool = False) -> str:
    text = re.sub(r'[\s-]', '', str(value or '')).upper()
    if not text:
        if required:
            raise SgrecvError('invalid_uen', 'UEN is required.')
        return ''
    if not uen_check_ok(text):
        raise SgrecvError('invalid_uen', 'UEN failed the check letter.')
    return text


def mask_uen(value: Any) -> str:
    text = re.sub(r'[\s-]', '', str(value or '')).upper()
    if len(text) < 5:
        return '********X'
    return text[:2] + '****' + text[-3:]


def normalize_phone(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise SgrecvError('invalid_phone', 'Singapore mobile is required.')
        return ''
    if digits.startswith('65') and len(digits) == 10:
        national = digits[2:]
    elif digits.startswith('0') and len(digits) == 9:
        national = digits[1:]
    else:
        national = digits
    if len(national) != 8 or national[0] not in {'8', '9'}:
        raise SgrecvError('invalid_phone', 'PayNow mobile must be an 8-digit +65 8/9 number.')
    return '+65' + national


def mask_phone(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if digits.startswith('65') and len(digits) == 10:
        national = digits[2:]
    else:
        national = digits
    if len(national) != 8:
        return '+65****'
    return '+65****' + national[-4:]


def infer_proxy_type(value: Any) -> str:
    text = re.sub(r'[\s-]', '', str(value or '')).upper()
    if nric_check_ok(text):
        return 'nric'
    if uen_check_ok(text):
        return 'uen'
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if digits.startswith('65') and len(digits) == 10:
        return 'phone'
    if len(digits) == 8 and digits[:1] in {'8', '9'}:
        return 'phone'
    return ''


def normalize_proxy(proxy_type: Any, value: Any, *, required: bool = False) -> Tuple[str, str]:
    kind = normalize_proxy_type(proxy_type)
    if not kind:
        kind = infer_proxy_type(value)
    if not kind:
        if required:
            raise SgrecvError('invalid_proxy', 'PayNow proxy type is required.')
        return '', ''
    if kind == 'nric':
        return kind, normalize_nric(value, required=True)
    if kind == 'uen':
        return kind, normalize_uen(value, required=True)
    return kind, normalize_phone(value, required=True)


def mask_proxy(proxy_type: str, value: str) -> str:
    if proxy_type == 'nric':
        return mask_nric(value)
    if proxy_type == 'uen':
        return mask_uen(value)
    if proxy_type == 'phone':
        return mask_phone(value)
    return '****'


def proxy_fingerprint(proxy_type: Any, value: Any) -> str:
    kind, proxy = normalize_proxy(proxy_type, value, required=True)
    material = '%s:%s' % (kind, proxy)
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def mask_e2e(value: Any) -> str:
    text = str(value or '').strip().upper()
    if len(text) < 8:
        return '****'
    prefix = re.match(r'[A-Z]+', text)
    head = prefix.group(0) if prefix else text[:4]
    return head + '****' + text[-4:]


def compose_e2e(rail: str, cycle_date: str, sequence: int) -> str:
    kind = normalize_rail(rail)
    if not re.fullmatch(r'\d{8}', str(cycle_date or '')):
        raise SgrecvError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise SgrecvError('invalid_reference', 'E2E sequence out of range.')
    return '%s%s%06d' % (E2E_PREFIX[kind], cycle_date, seq)


def normalize_e2e(value: Any) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        raise SgrecvError('invalid_reference', 'FAST End-to-End ID is required.')
    if any(text.startswith(prefix) for prefix in BLOCKED_E2E_PREFIXES):
        raise SgrecvError('invalid_reference', 'End-to-End ID must not use another rail prefix.')
    if len(text) == 22 and text[:8].isdigit():
        raise SgrecvError('invalid_reference', 'Fedwire IMAD is not a FAST End-to-End ID.')
    if not re.fullmatch(r'(PNW|FAST|MEPS|GIRO)\d{14}', text):
        raise SgrecvError('invalid_reference', 'E2E ID must be FAST/PNW/MEPS/GIRO + date + seq.')
    return text


def compose_amount_field(amount: Decimal) -> str:
    quantized = amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    if quantized < 0 or quantized > Decimal('999999999.99'):
        raise SgrecvError('invalid_amount', 'Amount cannot be encoded in FAST1.')
    return money_str(quantized)


def parse_amount_field(value: Any) -> Decimal:
    try:
        return parse_money(value)
    except AmountError as exc:
        raise SgrecvError('invalid_amount', 'FAST1 amount field is invalid.') from exc


def _reject_markup(text: str) -> None:
    lowered = text.lower()
    if '<xml' in lowered or '<!doctype' in lowered or '<?xml' in lowered or '<fast' in lowered:
        raise SgrecvError('invalid_file', 'XML/DOCTYPE FAST1 files are rejected.')


def compose_fast1(
    *,
    e2e: str,
    rail: str,
    proxy_type: str,
    proxy: str,
    bank: str,
    account_number: str,
    uen: str,
    amount: Decimal,
    purpose: str,
    status: str,
    mask: bool = True,
) -> str:
    kind = normalize_rail(rail)
    clearing = CLEARING_BY_RAIL[kind]
    return '|'.join((
        'FAST1',
        FAST1_VERSION,
        mask_e2e(e2e) if mask else normalize_e2e(e2e),
        kind,
        proxy_type or '',
        mask_proxy(proxy_type, proxy) if mask and proxy else (proxy or ''),
        mask_bank(bank) if mask and bank else (normalize_bank_code(bank, required=False) if bank else ''),
        mask_account(account_number) if mask and account_number else (account_number or ''),
        mask_uen(uen) if mask and uen else (uen or ''),
        compose_amount_field(amount),
        normalize_purpose(purpose),
        str(status or IN_POSTED),
        'SGD',
        clearing,
    ))


def parse_fast1(text: Any) -> Dict[str, Any]:
    raw = str(text or '').strip()
    if not raw:
        raise SgrecvError('invalid_file', 'FAST1 record is empty.')
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != FAST1_FIELDS or parts[0] != 'FAST1' or parts[1] != FAST1_VERSION:
        raise SgrecvError('invalid_file', 'FAST1 record is malformed.')
    clearing = parts[13].strip().upper()
    if clearing in REJECTED_CLEARING or clearing not in {'FAST', 'MEPS', 'GIRO', 'PAYNOW'}:
        raise SgrecvError('invalid_file', 'FAST1 clearing must be FAST/MEPS/GIRO/PAYNOW.')
    joined = '|'.join(parts[2:9])
    if '*' in joined or '…' in joined:
        raise SgrecvError('invalid_file', 'Masked FAST1 record cannot be re-ingested.')
    proxy_type = normalize_proxy_type(parts[4]) if parts[4] else ''
    proxy = ''
    if parts[5]:
        proxy_type, proxy = normalize_proxy(proxy_type, parts[5], required=True)
    return {
        'e2e': normalize_e2e(parts[2]),
        'rail': normalize_rail(parts[3]),
        'proxy_type': proxy_type,
        'proxy': proxy,
        'bank': normalize_bank_code(parts[6], required=bool(parts[6])),
        'account_number': normalize_external_account(parts[7], required=bool(parts[7])),
        'uen': normalize_uen(parts[8], required=False) if parts[8] else '',
        'amount': parse_amount_field(parts[9]),
        'purpose': normalize_purpose(parts[10]),
        'status': parts[11],
        'currency': normalize_currency(parts[12]),
        'clearing': clearing,
        'name': '',
    }


def split_fast1_file(text: Any) -> List[str]:
    raw = str(text or '')
    _reject_markup(raw)
    records = [line.strip() for line in raw.replace('\r\n', '\n').split('\n') if line.strip()]
    if not records:
        raise SgrecvError('invalid_file', 'FAST1 file is empty.')
    return records


def convert_sgd_to_usd(amount: Decimal, rate: Decimal) -> Decimal:
    if rate <= 0:
        raise SgrecvError('invalid_fx', 'SGDUSD rate must be positive.')
    return (amount * rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def message_from_values(values: Dict[str, Any]) -> Dict[str, Any]:
    recv = normalize_bank_code(
        values.get('recv_bank') or values.get('receiver') or values.get('bank') or DEFAULT_RECEIVER_BANK,
    )
    send = normalize_bank_code(
        values.get('send_bank') or values.get('sender_bank') or DEFAULT_SENDER_BANK,
        required=False,
    ) or DEFAULT_SENDER_BANK
    proxy_raw = values.get('proxy') or values.get('paynow') or values.get('key') or values.get('payid')
    proxy_type = values.get('proxy_type') or values.get('key_type') or values.get('kind') or ''
    proxy = ''
    kind = ''
    if proxy_raw:
        kind, proxy = normalize_proxy(proxy_type, proxy_raw, required=True)
    amount = values.get('amount')
    if not isinstance(amount, Decimal):
        try:
            amount = parse_money(amount)
        except AmountError as exc:
            raise SgrecvError('invalid_amount', 'Amount is invalid.') from exc
    e2e = str(values.get('end_to_end_id') or values.get('e2e') or '').strip()
    rail = normalize_rail(values.get('rail') or 'paynow')
    if e2e:
        e2e = normalize_e2e(e2e)
    else:
        cycle = str(values.get('value_date') or values.get('cycle_date') or '').strip()
        if '-' in cycle:
            cycle = cycle.replace('-', '')[:8]
        seq = values.get('sequence') or values.get('seq') or 1
        e2e = compose_e2e(rail, cycle, int(seq))
    name = str(values.get('name') or values.get('sender_name') or values.get('payer') or '').strip()
    if name:
        name = normalize_legal_name(name)
    dest_acct = normalize_external_account(
        values.get('account_number') or values.get('external_account') or '',
        required=False,
    )
    return {
        'e2e': e2e,
        'rail': rail,
        'proxy_type': kind,
        'proxy': proxy,
        'bank': recv,
        'account_number': dest_acct,
        'uen': normalize_uen(values.get('uen'), required=False) if values.get('uen') else '',
        'amount': amount,
        'purpose': normalize_purpose(values.get('purpose')),
        'name': name or 'PAYNOW PAYER',
        'currency': normalize_currency(values.get('currency')),
        'send_bank': send,
        'recv_bank': recv,
    }


def _observed_sunday(day: date) -> date:
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def sg_mas_holidays(year: int) -> set:
    """Fixed MAS-observed holidays. Moveable feasts are injected via extra_holidays."""
    return {
        _observed_sunday(date(year, 1, 1)),
        _observed_sunday(date(year, 5, 1)),
        _observed_sunday(date(year, 8, 9)),
        _observed_sunday(date(year, 12, 25)),
    }


class SgClock:
    """Singapore FAST 24/7 vs MEPS+/GIRO banking-day clock. Injectable offset and holidays."""

    def __init__(
        self,
        *,
        meps_open_hour: int = 9,
        meps_cutoff_hour: int = 17,
        meps_cutoff_minute: int = 30,
        tz_offset_minutes: int = SGT_OFFSET_MINUTES,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.meps_open_hour = int(meps_open_hour)
        self.meps_cutoff_hour = int(meps_cutoff_hour)
        self.meps_cutoff_minute = int(meps_cutoff_minute)
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

    def holidays(self, year: int) -> set:
        return sg_mas_holidays(year) | {day for day in self.extra_holidays if day.year == year}

    def is_weekend(self, day: date) -> bool:
        return day.weekday() >= 5

    def is_holiday(self, day: date) -> bool:
        return day in self.holidays(day.year)

    def is_business_day(self, day: date) -> bool:
        return not self.is_weekend(day) and not self.is_holiday(day)

    def is_after_meps_cutoff(self, ts: float) -> bool:
        local = self.local_dt(ts)
        if (local.hour, local.minute) >= (self.meps_cutoff_hour, self.meps_cutoff_minute):
            return True
        if local.hour < self.meps_open_hour:
            return True
        return False

    def is_session(self, ts: float) -> bool:
        local = self.local_dt(ts)
        return self.is_business_day(local.date()) and not self.is_after_meps_cutoff(ts)

    def next_business_day(self, day: date) -> date:
        cursor = day + timedelta(days=1)
        while not self.is_business_day(cursor):
            cursor += timedelta(days=1)
        return cursor

    def value_date(self, ts: float, *, rail: str = 'paynow') -> date:
        local = self.local_dt(ts)
        kind = normalize_rail(rail)
        day = local.date()
        if kind in {'paynow', 'fast'}:
            return day
        if kind == 'giro':
            start = day + timedelta(days=1)
            return self.next_business_day(start)
        if not self.is_business_day(day) or self.is_after_meps_cutoff(ts):
            return self.next_business_day(day)
        return day

    def cycle_date(self, ts: float, *, rail: str = 'paynow') -> str:
        return self.value_date(ts, rail=rail).strftime('%Y%m%d')

    def session_close_ts(self, day: date) -> float:
        local = datetime(
            day.year, day.month, day.day,
            self.meps_cutoff_hour, self.meps_cutoff_minute, tzinfo=self.tz,
        )
        return local.timestamp()

    def snapshot(self, ts: float, *, rail: str = 'paynow') -> Dict[str, Any]:
        local = self.local_dt(ts)
        day = local.date()
        kind = normalize_rail(rail)
        session = self.is_session(ts)
        after = False
        if kind in {'meps', 'giro'}:
            after = not session
        return {
            'rail': kind,
            'local_date': day.isoformat(),
            'local_time': local.strftime('%H:%M'),
            'timezone': 'SGT',
            'open': '%02d:00' % self.meps_open_hour,
            'cutoff': '%02d:%02d' % (self.meps_cutoff_hour, self.meps_cutoff_minute),
            'session': session,
            'after_cutoff': after,
            'always_on': kind in {'paynow', 'fast'},
            'weekend': self.is_weekend(day),
            'holiday': self.is_holiday(day),
            'business_day': self.is_business_day(day),
            'value_date': self.value_date(ts, rail=kind).isoformat(),
            'cycle_date': self.cycle_date(ts, rail=kind),
            'settlement_method': 'fast',
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
class SgrecvPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_claim: bool = True
    customer_return: bool = True
    allow_credit: bool = False
    max_keys: int = 12
    max_inbounds: int = 120
    min_amount: Decimal = Decimal('0.01')
    fast_cap: Decimal = Decimal('200000.00')
    giro_cap: Decimal = Decimal('200000.00')
    meps_cap: Decimal = Decimal('10000000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    sgdusd: Decimal = DEFAULT_SGDUSD
    tz_offset_minutes: int = SGT_OFFSET_MINUTES
    meps_open_hour: int = 9
    meps_cutoff_hour: int = 17
    meps_cutoff_minute: int = 30
    receiver_bank: str = DEFAULT_RECEIVER_BANK
    customer_return_seconds: int = CUSTOMER_RETURN_SECONDS
    staff_complaint_days: int = STAFF_COMPLAINT_DAYS
    claim_seconds: int = CLAIM_SECONDS
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()
    banks: Dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.banks is None:
            self.banks = dict(DEFAULT_BANKS)

    def cap_for(self, rail: str) -> Decimal:
        if rail == 'meps':
            return self.meps_cap
        if rail == 'giro':
            return self.giro_cap
        return self.fast_cap

    @classmethod
    def from_env(cls) -> 'SgrecvPolicy':
        extra = _env_list('SGRECV_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        source = os.environ.get('SGRECV_RECEIVER') or DEFAULT_RECEIVER_BANK
        try:
            receiver = normalize_bank_code(source)
        except SgrecvError:
            receiver = DEFAULT_RECEIVER_BANK
        return cls(
            enabled=_env_bool('SGRECV_ENABLED', True),
            customer_manage=_env_bool('SGRECV_CUSTOMER_MANAGE', True),
            customer_claim=_env_bool('SGRECV_CUSTOMER_CLAIM', True),
            customer_return=_env_bool('SGRECV_CUSTOMER_RETURN', True),
            allow_credit=_env_bool('SGRECV_ALLOW_CREDIT', False),
            max_keys=max(1, _env_int('SGRECV_MAX_KEYS', 12)),
            max_inbounds=max(1, _env_int('SGRECV_MAX_INBOUNDS', 120)),
            min_amount=_env_money('SGRECV_MIN_AMOUNT', '0.01'),
            fast_cap=_env_money('SGRECV_FAST_CAP', '200000.00'),
            giro_cap=_env_money('SGRECV_GIRO_CAP', '200000.00'),
            meps_cap=_env_money('SGRECV_MEPS_CAP', '10000000.00'),
            dual_control_threshold=_env_money('SGRECV_DUAL_CONTROL', '10000.00'),
            sgdusd=_env_money('SGRECV_FX_SGDUSD', '0.74'),
            tz_offset_minutes=_env_int('SGRECV_TZ_OFFSET', SGT_OFFSET_MINUTES),
            meps_open_hour=max(0, min(23, _env_int('SGRECV_OPEN_HOUR', 9))),
            meps_cutoff_hour=max(0, min(23, _env_int('SGRECV_CUTOFF_HOUR', 17))),
            meps_cutoff_minute=max(0, min(59, _env_int('SGRECV_CUTOFF_MINUTE', 30))),
            receiver_bank=receiver,
            customer_return_seconds=max(60, _env_int('SGRECV_RETURN_SECONDS', CUSTOMER_RETURN_SECONDS)),
            staff_complaint_days=max(1, _env_int('SGRECV_COMPLAINT_DAYS', STAFF_COMPLAINT_DAYS)),
            claim_seconds=max(60, _env_int('SGRECV_CLAIM_SECONDS', CLAIM_SECONDS)),
            watchlist=watch,
            extra_holidays=_env_list('SGRECV_HOLIDAYS'),
        )


@dataclass
class SgrecvKey:
    key_id: str
    userid: str
    nickname: str
    proxy_type: str
    proxy: str
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
            'proxy_type': self.proxy_type,
            'proxy_masked': mask_proxy(self.proxy_type, self.proxy),
            'account_last4': last4(self.destination_account),
            'status': self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == KEY_ACTIVE,
            'paused': self.status == KEY_PAUSED,
            'cancelled': self.status == KEY_CANCELLED,
        }


@dataclass
class SgrecvInbound:
    inbound_id: str
    e2e: str
    userid: str
    destination_account: str
    amount_sgd: str
    amount_usd: str
    sender_name: str
    send_bank: str
    recv_bank: str
    rail: str
    proxy_type: str
    proxy: str
    fingerprint: str
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
            'amount_sgd': self.amount_sgd,
            'amount_usd': self.amount_usd,
            'sender_name': self.sender_name,
            'send_bank_masked': mask_bank(self.send_bank) if self.send_bank else '',
            'send_name': bank_name(self.send_bank) if self.send_bank else '',
            'recv_bank_masked': mask_bank(self.recv_bank) if self.recv_bank else '',
            'rail': self.rail,
            'proxy_type': self.proxy_type,
            'proxy_masked': mask_proxy(self.proxy_type, self.proxy) if self.proxy else '****',
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


def _key_from_row(row: Any) -> SgrecvKey:
    data = dict(row)
    return SgrecvKey(
        key_id=data['key_id'], userid=data['userid'], nickname=data['nickname'],
        proxy_type=data['proxy_type'], proxy=data['proxy'], fingerprint=data['fingerprint'],
        destination_account=data['destination_account'], status=data['status'], actor=data['actor'],
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
    )


def _inbound_from_row(row: Any) -> SgrecvInbound:
    data = dict(row)
    return SgrecvInbound(
        inbound_id=data['inbound_id'], e2e=data['e2e'], userid=data['userid'],
        destination_account=data['destination_account'], amount_sgd=data['amount_sgd'],
        amount_usd=data['amount_usd'], sender_name=data['sender_name'],
        send_bank=data['send_bank'], recv_bank=data['recv_bank'], rail=data['rail'],
        proxy_type=data['proxy_type'], proxy=data['proxy'], fingerprint=data['fingerprint'],
        account_number=data.get('account_number') or '', purpose=data['purpose'],
        status=data['status'], reason=data['reason'], actor=data['actor'], releaser=data['releaser'],
        ofac_hit=bool(int(data['ofac_hit'])), ofac_match=data['ofac_match'],
        ofac_overridden=bool(int(data['ofac_overridden'])),
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
        posted_at=float(data.get('posted_at') or 0), returned_at=float(data.get('returned_at') or 0),
        expiry_at=float(data.get('expiry_at') or 0), note=data.get('note') or '',
    )


class MemorySgrecvStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.keys: Dict[str, SgrecvKey] = {}
        self.inbounds: Dict[str, SgrecvInbound] = {}
        self._by_e2e: Dict[str, str] = {}

    def put_key(self, row: SgrecvKey) -> None:
        with self._lock:
            self.keys[row.key_id] = row

    def get_key(self, key_id: str) -> Optional[SgrecvKey]:
        return self.keys.get(key_id)

    def get_key_by_fingerprint(self, fingerprint: str) -> Optional[SgrecvKey]:
        matches = [
            row for row in self.keys.values()
            if row.fingerprint == fingerprint and row.status in OPEN_KEY
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda row: row.created_at, reverse=True)[0]

    def update_key(self, row: SgrecvKey) -> None:
        self.put_key(row)

    def list_keys(self, userid: Optional[str] = None) -> List[SgrecvKey]:
        rows = list(self.keys.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        return sorted(rows, key=lambda row: row.created_at)

    def put_inbound(self, row: SgrecvInbound) -> None:
        with self._lock:
            self.inbounds[row.inbound_id] = row
            self._by_e2e[row.e2e] = row.inbound_id

    def get_inbound(self, inbound_id: str) -> Optional[SgrecvInbound]:
        return self.inbounds.get(inbound_id)

    def get_by_e2e(self, e2e: str) -> Optional[SgrecvInbound]:
        inbound_id = self._by_e2e.get(e2e)
        return self.inbounds.get(inbound_id) if inbound_id else None

    def update_inbound(self, row: SgrecvInbound) -> None:
        self.put_inbound(row)

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[SgrecvInbound]:
        rows = list(self.inbounds.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)


class SqliteSgrecvStore:
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
                    proxy_type TEXT NOT NULL,
                    proxy TEXT NOT NULL,
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
                    amount_sgd TEXT NOT NULL,
                    amount_usd TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    send_bank TEXT NOT NULL,
                    recv_bank TEXT NOT NULL,
                    rail TEXT NOT NULL,
                    proxy_type TEXT NOT NULL DEFAULT '',
                    proxy TEXT NOT NULL DEFAULT '',
                    fingerprint TEXT NOT NULL DEFAULT '',
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

    def put_key(self, row: SgrecvKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO keys (
                    key_id, userid, nickname, proxy_type, proxy, fingerprint,
                    destination_account, status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.key_id, row.userid, row.nickname, row.proxy_type, row.proxy,
                    row.fingerprint, row.destination_account, row.status, row.actor,
                    row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_key(self, key_id: str) -> Optional[SgrecvKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM keys WHERE key_id = ?', (key_id,)).fetchone()
        return _key_from_row(row) if row else None

    def get_key_by_fingerprint(self, fingerprint: str) -> Optional[SgrecvKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM keys WHERE fingerprint = ? AND status IN ('active', 'paused')
                ORDER BY created_at DESC LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
        return _key_from_row(row) if row else None

    def update_key(self, row: SgrecvKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE keys SET nickname=?, proxy_type=?, proxy=?, fingerprint=?,
                    destination_account=?, status=?, actor=?, updated_at=?
                WHERE key_id=?
                """,
                (
                    row.nickname, row.proxy_type, row.proxy, row.fingerprint,
                    row.destination_account, row.status, row.actor, row.updated_at, row.key_id,
                ),
            )
            conn.commit()

    def list_keys(self, userid: Optional[str] = None) -> List[SgrecvKey]:
        sql = 'SELECT * FROM keys'
        args: Tuple[Any, ...] = ()
        if userid is not None:
            sql += ' WHERE userid = ?'
            args = (userid,)
        sql += ' ORDER BY created_at'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_key_from_row(row) for row in rows]

    def put_inbound(self, row: SgrecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inbounds (
                    inbound_id, e2e, userid, destination_account, amount_sgd, amount_usd,
                    sender_name, send_bank, recv_bank, rail, proxy_type, proxy, fingerprint,
                    account_number, purpose, status, reason, actor, releaser, ofac_hit,
                    ofac_match, ofac_overridden, created_at, updated_at, posted_at,
                    returned_at, expiry_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.inbound_id, row.e2e, row.userid, row.destination_account, row.amount_sgd,
                    row.amount_usd, row.sender_name, row.send_bank, row.recv_bank, row.rail,
                    row.proxy_type, row.proxy, row.fingerprint, row.account_number, row.purpose,
                    row.status, row.reason, row.actor, row.releaser, int(row.ofac_hit),
                    row.ofac_match, int(row.ofac_overridden), row.created_at, row.updated_at,
                    row.posted_at, row.returned_at, row.expiry_at, row.note,
                ),
            )
            conn.commit()

    def get_inbound(self, inbound_id: str) -> Optional[SgrecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,)).fetchone()
        return _inbound_from_row(row) if row else None

    def get_by_e2e(self, e2e: str) -> Optional[SgrecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE e2e = ?', (e2e,)).fetchone()
        return _inbound_from_row(row) if row else None

    def update_inbound(self, row: SgrecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE inbounds SET userid=?, destination_account=?, amount_sgd=?, amount_usd=?,
                    sender_name=?, send_bank=?, recv_bank=?, rail=?, proxy_type=?, proxy=?,
                    fingerprint=?, account_number=?, purpose=?, status=?, reason=?, actor=?,
                    releaser=?, ofac_hit=?, ofac_match=?, ofac_overridden=?, updated_at=?,
                    posted_at=?, returned_at=?, expiry_at=?, note=?
                WHERE inbound_id=?
                """,
                (
                    row.userid, row.destination_account, row.amount_sgd, row.amount_usd,
                    row.sender_name, row.send_bank, row.recv_bank, row.rail, row.proxy_type,
                    row.proxy, row.fingerprint, row.account_number, row.purpose, row.status,
                    row.reason, row.actor, row.releaser, int(row.ofac_hit), row.ofac_match,
                    int(row.ofac_overridden), row.updated_at, row.posted_at, row.returned_at,
                    row.expiry_at, row.note, row.inbound_id,
                ),
            )
            conn.commit()

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[SgrecvInbound]:
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


class SgrecvService:
    def __init__(
        self,
        policy: SgrecvPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[SgClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: datetime.now(tz=timezone.utc).timestamp())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn or screen_name
        self.calendar = calendar or SgClock(
            meps_open_hour=policy.meps_open_hour,
            meps_cutoff_hour=policy.meps_cutoff_hour,
            meps_cutoff_minute=policy.meps_cutoff_minute,
            tz_offset_minutes=policy.tz_offset_minutes,
            extra_holidays=policy.extra_holidays,
        )

    def _now(self) -> float:
        return float(self.clock())

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise SgrecvError('sgrecv_disabled', 'PayNow receive is disabled.')

    def _require_staff(self, actor_type: str) -> None:
        if actor_type not in EMPLOYEE_ROLES:
            raise SgrecvError('sgrecv_forbidden', 'Staff only.')

    def _require_customer_manage(self, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if not self.policy.customer_manage:
            raise SgrecvError('sgrecv_forbidden', 'Customers cannot manage PayNow proxies.')

    def _accounts(self, userid: str) -> Any:
        if not self.accounts_fn:
            return {}
        return self.accounts_fn(userid) or {}

    def _owned(self, userid: str, account: str) -> None:
        owned = own_accounts_from_customer_payload(self._accounts(userid))
        if account not in owned:
            raise SgrecvError('invalid_account', 'Destination is not an account of this customer.')

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
        proxy_type: Any,
        proxy: Any,
        destination_account: Any,
    ) -> SgrecvKey:
        self._require_enabled()
        self._require_customer_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise SgrecvError('sgrecv_forbidden', 'Cannot enroll a PayNow proxy for another customer.')
        kind, value = normalize_proxy(proxy_type, proxy, required=True)
        account = normalize_account(destination_account)
        self._owned(owner_userid, account)
        if not self._credit_allowed(owner_userid, account):
            raise SgrecvError('credit_not_allowed', 'Credit accounts cannot receive PayNow.')
        fingerprint = proxy_fingerprint(kind, value)
        existing = self.store.get_key_by_fingerprint(fingerprint)
        if existing and existing.status in OPEN_KEY:
            raise SgrecvError('key_duplicate', 'That PayNow proxy is already enrolled.')
        open_rows = [row for row in self.store.list_keys(owner_userid) if row.status in OPEN_KEY]
        if any(row.nickname.lower() == normalize_nickname(nickname).lower() for row in open_rows):
            raise SgrecvError('key_duplicate', 'That nickname is already in use.')
        if len(open_rows) >= self.policy.max_keys:
            raise SgrecvError('key_limit', 'PayNow enrollment limit reached.')
        now = self._now()
        row = SgrecvKey(
            key_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=normalize_nickname(nickname),
            proxy_type=kind,
            proxy=value,
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
    ) -> SgrecvKey:
        self._require_enabled()
        row = self.store.get_key(key_id)
        if row is None:
            raise SgrecvError('key_not_found', 'PayNow enrollment not found.')
        if actor_type not in EMPLOYEE_ROLES and actor != row.userid:
            raise SgrecvError('sgrecv_forbidden', 'Cannot change another customer PayNow proxy.')
        self._require_customer_manage(actor_type)
        if status not in KEY_STATUSES:
            raise SgrecvError('invalid_status', 'Unknown PayNow status.')
        if row.status == KEY_CANCELLED:
            raise SgrecvError('already_cancelled', 'Cancelled PayNow proxies cannot change.')
        if status == KEY_PAUSED and row.status != KEY_ACTIVE:
            raise SgrecvError('already_paused' if row.status == KEY_PAUSED else 'invalid_status', 'PayNow proxy is not active.')
        if status == KEY_ACTIVE and row.status != KEY_PAUSED:
            raise SgrecvError('already_active', 'PayNow proxy is already active.')
        if status == row.status:
            code = 'already_paused' if status == KEY_PAUSED else 'already_active'
            raise SgrecvError(code, 'PayNow proxy already in that status.')
        row.status = status
        row.actor = actor
        row.updated_at = self._now()
        self.store.update_key(row)
        if status == KEY_ACTIVE:
            self._rematch_unmatched(row)
        return row

    def _rematch_unmatched(self, enrollment: SgrecvKey) -> None:
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
    ) -> Tuple[SgrecvInbound, bool]:
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
        send_bank: str = '',
    ) -> Tuple[List[SgrecvInbound], int]:
        self._require_enabled()
        self._require_staff(actor_type)
        created = 0
        rows = []
        fallback = ''
        if sender_name:
            fallback = normalize_legal_name(sender_name)
        sender = ''
        if send_bank:
            sender = normalize_bank_code(send_bank, required=False)
        for record in split_fast1_file(payload):
            parsed = parse_fast1(record)
            parsed['recv_bank'] = parsed.get('bank') or self.policy.receiver_bank
            parsed['send_bank'] = sender or DEFAULT_SENDER_BANK
            if fallback:
                parsed['name'] = fallback
            elif not parsed.get('name'):
                parsed['name'] = 'PAYNOW PAYER'
            row, is_new = self._ingest_parsed(actor=actor, parsed=parsed)
            rows.append(row)
            if is_new:
                created += 1
        return rows, created

    def _ingest_parsed(self, *, actor: str, parsed: Dict[str, Any]) -> Tuple[SgrecvInbound, bool]:
        recv = parsed.get('recv_bank') or parsed.get('bank') or self.policy.receiver_bank
        if recv != self.policy.receiver_bank:
            raise SgrecvError('wrong_receiver', 'PayNow credit is not for this MAS bank code.')
        existing = self.store.get_by_e2e(parsed['e2e'])
        if existing is not None:
            return existing, False
        total = len(self.store.list_inbounds())
        if total >= self.policy.max_inbounds:
            raise SgrecvError('inbound_limit', 'Inbound PayNow limit reached.')
        amount = parsed['amount'] if isinstance(parsed['amount'], Decimal) else parse_money(parsed['amount'])
        rail = normalize_rail(parsed.get('rail') or 'paynow')
        cap = self.policy.cap_for(rail)
        if amount < self.policy.min_amount or amount > cap:
            code = 'paynow_amount_exceeded' if amount > cap else 'amount_out_of_range'
            raise SgrecvError(code, 'Amount is outside the %s range.' % rail)
        now = self._now()
        proxy = parsed.get('proxy') or ''
        kind = parsed.get('proxy_type') or ''
        fingerprint = proxy_fingerprint(kind, proxy) if proxy else ''
        usd = convert_sgd_to_usd(amount, self.policy.sgdusd)
        row = SgrecvInbound(
            inbound_id=uuid.uuid4().hex,
            e2e=parsed['e2e'],
            userid='',
            destination_account='',
            amount_sgd=money_str(amount),
            amount_usd=money_str(usd),
            sender_name=parsed.get('name') or 'PAYNOW PAYER',
            send_bank=parsed.get('send_bank') or '',
            recv_bank=recv,
            rail=rail,
            proxy_type=kind,
            proxy=proxy,
            fingerprint=fingerprint,
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

    def _evaluate(self, row: SgrecvInbound, *, rescreen: bool) -> SgrecvInbound:
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
        except SgrecvError:
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
        sgd = parse_money(row.amount_sgd)
        if sgd >= self.policy.dual_control_threshold and not row.releaser:
            row.status = IN_PENDING
            row.reason = 'dual_control'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if row.rail in {'meps', 'giro'} and not self.calendar.is_session(now):
            row.status = IN_QUEUED
            row.reason = 'meps_session' if row.rail == 'meps' else 'giro_session'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if row.rail == 'giro':
            local = self.calendar.local_dt(now).date()
            created = self.calendar.local_dt(row.created_at).date()
            if local <= created and self.calendar.is_session(now):
                # GIRO posts on the next MAS day, not same-session.
                row.status = IN_QUEUED
                row.reason = 'giro_session'
                row.updated_at = now
                self.store.update_inbound(row)
                return row
        return self._post(row)

    def _post(self, row: SgrecvInbound) -> SgrecvInbound:
        if self.credit_fn is None:
            raise SgrecvError('failed', 'Credit function is not configured.')
        remark = 'paynow from %s' % row.sender_name[:40]
        result = self.credit_fn(row.destination_account, row.amount_usd, remark)
        classified = _classify_money_result(result)
        now = self._now()
        if classified == 'nsf':
            row.status = IN_FAILED
            row.reason = 'nsf'
            row.updated_at = now
            self.store.update_inbound(row)
            raise SgrecvError('nsf', 'Credit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            row.status = IN_FAILED
            row.reason = 'failed'
            row.updated_at = now
            self.store.update_inbound(row)
            raise SgrecvError('failed', 'Credit failed.', inbound=row)
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
            'clock': self.calendar.snapshot(self._now(), rail=row.rail),
            'receiver': mask_bank(self.policy.receiver_bank),
        }

    def _get(self, *, inbound_id: str = '', e2e: str = '') -> SgrecvInbound:
        row = None
        if inbound_id:
            row = self.store.get_inbound(str(inbound_id).strip())
        if row is None and e2e:
            row = self.store.get_by_e2e(normalize_e2e(e2e))
        if row is None:
            raise SgrecvError('inbound_not_found', 'Inbound PayNow not found.')
        return row

    def _assert_visible(self, row: SgrecvInbound, actor: str, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if row.userid and row.userid != actor:
            raise SgrecvError('sgrecv_forbidden', 'Cannot view another customer PayNow.')

    def assign(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        customer_id: str,
        account: Any,
        nickname: str = '',
    ) -> SgrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_UNMATCHED:
            raise SgrecvError('not_assignable', 'Only unmatched PayNow credits can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise SgrecvError('missing_customer_id', 'Customer id is required to assign.')
        dest = normalize_account(account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise SgrecvError('credit_not_allowed', 'Credit accounts cannot receive PayNow.')
        if row.proxy:
            existing = self.store.get_key_by_fingerprint(row.fingerprint)
            if existing is None or existing.status == KEY_CANCELLED:
                self.add_key(
                    owner_userid=owner, actor=actor, actor_type=actor_type,
                    nickname=nickname or ('PayNow %s' % (row.proxy_type or 'proxy')),
                    proxy_type=row.proxy_type, proxy=row.proxy, destination_account=dest,
                )
                fresh = self.store.get_inbound(row.inbound_id)
                if fresh is not None and fresh.status != IN_UNMATCHED:
                    return fresh
            elif existing.userid != owner:
                raise SgrecvError('key_duplicate', 'PayNow proxy belongs to another customer.')
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
        proxy_type: Any = '',
        proxy: Any = '',
        destination_account: Any = '',
        nickname: str = '',
        owner_userid: str = '',
    ) -> SgrecvInbound:
        """PayNow claim: customer presents the matching proxy for an unmatched credit."""
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_claim:
                raise SgrecvError('sgrecv_forbidden', 'Customers cannot claim PayNow credits.')
            owner = actor
        else:
            owner = str(owner_userid or '').strip()
            if not owner:
                raise SgrecvError('missing_customer_id', 'Customer id is required to claim.')
        if not proxy:
            raise SgrecvError('missing_key', 'PayNow proxy is required to claim.')
        kind, value = normalize_proxy(proxy_type, proxy, required=True)
        fingerprint = proxy_fingerprint(kind, value)
        dest = normalize_account(destination_account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise SgrecvError('credit_not_allowed', 'Credit accounts cannot receive PayNow.')
        row = None
        if inbound_id:
            row = self._get(inbound_id=inbound_id)
            if row.fingerprint != fingerprint:
                raise SgrecvError('not_claimable', 'Presented proxy does not match this PayNow.')
        else:
            for candidate in self.store.list_inbounds(status=IN_UNMATCHED):
                if candidate.fingerprint == fingerprint:
                    row = candidate
                    break
        if row is None:
            raise SgrecvError('inbound_not_found', 'No unmatched PayNow for that proxy.')
        if row.status != IN_UNMATCHED:
            raise SgrecvError('not_claimable', 'This PayNow credit is not waiting for a claim.')
        if row.expiry_at and self._now() > row.expiry_at:
            row.status = IN_EXPIRED
            row.reason = 'expired'
            row.updated_at = self._now()
            self.store.update_inbound(row)
            raise SgrecvError('return_window_closed', 'PayNow claim window has closed.')
        existing = self.store.get_key_by_fingerprint(fingerprint)
        if existing is None or existing.status == KEY_CANCELLED:
            self.add_key(
                owner_userid=owner, actor=actor, actor_type=actor_type,
                nickname=nickname or ('PayNow %s' % kind),
                proxy_type=kind, proxy=value, destination_account=dest,
            )
        elif existing.userid != owner:
            raise SgrecvError('key_duplicate', 'PayNow proxy belongs to another customer.')
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

    def override_ofac(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> SgrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_HELD:
            raise SgrecvError('not_overridable', 'Only OFAC-held PayNow credits can be overridden.')
        row.ofac_overridden = True
        row.ofac_hit = False
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def release(self, *, inbound_id: str, actor: str, actor_type: str) -> SgrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_PENDING:
            raise SgrecvError('not_releasable', 'Only dual-control PayNow credits can be released.')
        if actor == row.actor:
            raise SgrecvError('same_approver', 'A different employee must release this credit.')
        row.releaser = actor
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def reject(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> SgrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status not in OPEN_IN:
            raise SgrecvError('not_rejectable', 'This PayNow credit cannot be rejected.')
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
            raise SgrecvError('not_exportable', 'PayNow credit has no End-to-End ID to export.')
        _ = actor
        record = compose_fast1(
            e2e=row.e2e,
            rail=row.rail,
            proxy_type=row.proxy_type,
            proxy=row.proxy,
            bank=row.recv_bank,
            account_number=row.account_number,
            uen=row.proxy if row.proxy_type == 'uen' else '',
            amount=parse_money(row.amount_sgd, allow_zero=True),
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
    ) -> SgrecvInbound:
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
    ) -> SgrecvInbound:
        self._require_enabled()
        row = self._get(inbound_id=inbound_id)
        self._assert_visible(row, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_return:
                raise SgrecvError('sgrecv_forbidden', 'Customers cannot return PayNow credits.')
            if actor != row.userid:
                raise SgrecvError('sgrecv_forbidden', 'Cannot return another customer PayNow.')
        return self._return(row, actor=actor, actor_type=actor_type, reason=reason, honor_window=True)

    def _customer_deadline(self, row: SgrecvInbound) -> float:
        posted = row.posted_at or row.created_at
        if row.rail in {'giro', 'meps'}:
            day = self.calendar.local_dt(posted).date()
            return self.calendar.session_close_ts(self.calendar.next_business_day(day))
        return posted + self.policy.customer_return_seconds

    def _return(
        self,
        row: SgrecvInbound,
        *,
        actor: str,
        actor_type: str,
        reason: str,
        honor_window: bool,
    ) -> SgrecvInbound:
        now = self._now()
        _ = actor
        if row.status == IN_RETURNED or row.returned_at:
            raise SgrecvError('already_returned', 'PayNow credit already returned.')
        if row.status in OPEN_IN:
            row.status = IN_RETURNED
            row.reason = reason or 'cust'
            row.returned_at = now
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if row.status != IN_POSTED:
            raise SgrecvError('not_returnable', 'This PayNow credit cannot be returned.')
        if honor_window and actor_type not in EMPLOYEE_ROLES:
            if now > self._customer_deadline(row):
                raise SgrecvError('return_window_closed', 'Customer PayNow return window has closed.')
        if actor_type in EMPLOYEE_ROLES:
            elapsed = now - (row.posted_at or row.created_at)
            if elapsed > self.policy.staff_complaint_days * 86400:
                raise SgrecvError('med_window_closed', 'Staff complaint window is 90 days.')
        if self.debit_fn is None:
            raise SgrecvError('return_failed', 'Debit function is not configured.')
        remark = 'paynow return %s' % row.e2e[-12:]
        result = self.debit_fn(row.destination_account, row.amount_usd, remark)
        classified = _classify_money_result(result)
        if classified == 'nsf':
            raise SgrecvError('nsf', 'Return debit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            raise SgrecvError('return_failed', 'Return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.reason = reason or 'cust'
        row.returned_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[SgrecvInbound]:
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
        ytd = sum((parse_money(row.amount_sgd, allow_zero=True) for row in received), Decimal('0.00'))
        return {
            'enabled': self.policy.enabled,
            'clock': self.calendar.snapshot(now),
            'receiver': mask_bank(self.policy.receiver_bank),
            'keys': [row.to_dict() for row in keys[:40]],
            'inbounds': [row.to_dict() for row in inbounds[:40]],
            'ytd_received': money_str(ytd),
            'open_count': sum(1 for row in inbounds if row.status in OPEN_IN),
            'active_keys': sum(1 for row in keys if row.status == KEY_ACTIVE),
            'min_amount': money_str(self.policy.min_amount),
            'fast_cap': money_str(self.policy.fast_cap),
            'meps_cap': money_str(self.policy.meps_cap),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'settlement_method': 'fast',
            'currency': 'SGD',
            'fx_sgdusd': money_str(self.policy.sgdusd),
        }

    def unmatched_snapshot(self) -> Dict[str, Any]:
        rows = self.store.list_inbounds(status=IN_UNMATCHED)
        return {
            'enabled': self.policy.enabled,
            'unmatched': [row.to_dict() for row in rows[:40]],
            'count': len(rows),
        }


_SERVICE: Optional[SgrecvService] = None


def set_service(service: Optional[SgrecvService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[SgrecvService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('SGRECV_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemorySgrecvStore()
    path = os.environ.get('SGRECV_DB', DEFAULT_STORE_PATH)
    return SqliteSgrecvStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[SgClock] = None,
) -> SgrecvService:
    if store is None:
        store = default_store()
    return SgrecvService(
        SgrecvPolicy.from_env(),
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
        'sgrecv_forbidden': 403,
        'sgrecv_disabled': 403,
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
        'invalid_bank': 400,
        'invalid_proxy': 400,
        'invalid_phone': 400,
        'invalid_nric': 400,
        'invalid_uen': 400,
        'invalid_rail': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_reference': 400,
        'invalid_file': 400,
        'invalid_currency': 400,
        'invalid_fx': 400,
        'wrong_receiver': 400,
        'paynow_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_key': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: SgrecvError) -> Dict[str, Any]:
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
    except SgrecvError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snapshot_for(service: SgrecvService, userid: str, actor: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(userid, actor=actor, actor_type=actor_type)


def handle_list(service: SgrecvService):
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
    return jsonify({'PayNowIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def handle_unmatched(service: SgrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'sgrecv_forbidden'}), 403
    snap = service.unmatched_snapshot()
    return jsonify({'UnmatchedPayNowIns': snap, 'PayNowIn': snap}), 200


def handle_add_key(service: SgrecvService):
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
                raise SgrecvError('missing_customer_id', 'Customer id is required.')
        row = service.add_key(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname') or '',
            proxy_type=values.get('proxy_type') or values.get('key_type') or values.get('kind'),
            proxy=values.get('proxy') or values.get('paynow') or values.get('key') or values.get('payid'),
            destination_account=values.get('destination_account') or values.get('account') or values.get('default_account'),
        )
        return jsonify({
            'message': 'PayNow proxy enrolled',
            'key': row.to_dict(),
            'PayNowIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _key_status_route(service: SgrecvService, status: str, message: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        key_id = str(values.get('key_id') or '').strip()
        if not key_id:
            raise SgrecvError('missing_key', 'key_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.set_key_status(key_id=key_id, actor=userid, actor_type=actor_type, status=status)
        return jsonify({
            'message': message,
            'key': row.to_dict(),
            'PayNowIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: SgrecvService):
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
        return jsonify({'preview': preview, 'PayNowIn': _snapshot_for(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest(service: SgrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'PayNow ingested' if created else 'PayNow already ingested',
            'inbound': row.to_dict(),
            'PayNowIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_ingest_file(service: SgrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        payload = values.get('file') or values.get('body') or values.get('payload')
        if not payload:
            raise SgrecvError('missing_file', 'FAST1 file body is required.')
        actor_type = session.get('usertype') or 'customer'
        rows, created = service.ingest_file(
            actor=userid, actor_type=actor_type, payload=str(payload),
            sender_name=str(values.get('name') or values.get('sender_name') or ''),
            send_bank=str(values.get('send_bank') or values.get('sender_bank') or ''),
        )
        owner = rows[-1].userid if rows and rows[-1].userid else userid
        return jsonify({
            'message': 'FAST1 file ingested',
            'created': created,
            'inbounds': [row.to_dict() for row in rows],
            'PayNowIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_assign(service: SgrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise SgrecvError('missing_inbound', 'inbound_id is required.')
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
            'message': 'PayNow assigned',
            'inbound': row.to_dict(),
            'PayNowIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_claim(service: SgrecvService):
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
            proxy_type=values.get('proxy_type') or values.get('key_type') or values.get('kind'),
            proxy=values.get('proxy') or values.get('paynow') or values.get('key') or values.get('payid'),
            destination_account=values.get('destination_account') or values.get('account'),
            nickname=str(values.get('nickname') or ''),
            owner_userid=str(values.get('customer_id') or ''),
        )
        return jsonify({
            'message': 'PayNow claimed',
            'inbound': row.to_dict(),
            'PayNowIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_inbound_route(service: SgrecvService, action: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise SgrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        if action == 'override':
            row = service.override_ofac(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'release':
            row = service.release(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            message = 'PayNow released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'PayNow rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=str(values.get('reason') or 'cust'),
            )
            message = 'PayNow returned'
        elif action == 'export':
            exported = service.export(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            owner = exported['inbound'].get('userid') or userid
            return jsonify({
                'message': 'PayNow exported',
                'file': exported['file'],
                'inbound': exported['inbound'],
                'PayNowIn': _snapshot_for(service, owner, userid, actor_type),
            }), 200
        else:
            raise SgrecvError('invalid_status', 'Unknown PayNow action.')
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'PayNowIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: SgrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise SgrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.request_return(
            inbound_id=inbound_id, actor=userid, actor_type=actor_type,
            reason=str(values.get('reason') or 'cust'),
        )
        return jsonify({
            'message': 'PayNow return requested',
            'inbound': row.to_dict(),
            'PayNowIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: SgrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'PayNowIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def attach_sgrecv_routes(app, service: SgrecvService) -> None:
    @app.route('/listPayNowIns', methods=['POST', 'GET'])
    def list_paynow_ins_route():
        return handle_list(service)

    @app.route('/listPayNowRecvKeys', methods=['POST', 'GET'])
    def list_paynow_recv_keys_route():
        return handle_list(service)

    @app.route('/listUnmatchedPayNowIns', methods=['POST', 'GET'])
    def list_unmatched_paynow_ins_route():
        return handle_unmatched(service)

    @app.route('/addPayNowRecvKey', methods=['POST', 'GET'])
    def add_paynow_recv_key_route():
        return handle_add_key(service)

    @app.route('/pausePayNowRecvKey', methods=['POST', 'GET'])
    def pause_paynow_recv_key_route():
        return _key_status_route(service, KEY_PAUSED, 'PayNow proxy paused')

    @app.route('/resumePayNowRecvKey', methods=['POST', 'GET'])
    def resume_paynow_recv_key_route():
        return _key_status_route(service, KEY_ACTIVE, 'PayNow proxy resumed')

    @app.route('/cancelPayNowRecvKey', methods=['POST', 'GET'])
    def cancel_paynow_recv_key_route():
        return _key_status_route(service, KEY_CANCELLED, 'PayNow proxy cancelled')

    @app.route('/previewPayNowIn', methods=['POST', 'GET'])
    def preview_paynow_in_route():
        return handle_preview(service)

    @app.route('/ingestPayNowIn', methods=['POST', 'GET'])
    def ingest_paynow_in_route():
        return handle_ingest(service)

    @app.route('/ingestPayNowInFile', methods=['POST', 'GET'])
    def ingest_paynow_in_file_route():
        return handle_ingest_file(service)

    @app.route('/claimPayNowIn', methods=['POST', 'GET'])
    def claim_paynow_in_route():
        return handle_claim(service)

    @app.route('/assignPayNowIn', methods=['POST', 'GET'])
    def assign_paynow_in_route():
        return handle_assign(service)

    @app.route('/overridePayNowInOfac', methods=['POST', 'GET'])
    def override_paynow_in_ofac_route():
        return _staff_inbound_route(service, 'override')

    @app.route('/releasePayNowIn', methods=['POST', 'GET'])
    def release_paynow_in_route():
        return _staff_inbound_route(service, 'release')

    @app.route('/rejectPayNowIn', methods=['POST', 'GET'])
    def reject_paynow_in_route():
        return _staff_inbound_route(service, 'reject')

    @app.route('/returnPayNowIn', methods=['POST', 'GET'])
    def return_paynow_in_route():
        return _staff_inbound_route(service, 'return')

    @app.route('/exportPayNowIn', methods=['POST', 'GET'])
    def export_paynow_in_route():
        return _staff_inbound_route(service, 'export')

    @app.route('/requestPayNowInReturn', methods=['POST', 'GET'])
    def request_paynow_in_return_route():
        return handle_request_return(service)

    @app.route('/runDuePayNowIns', methods=['POST', 'GET'])
    def run_due_paynow_ins_route():
        return handle_run_due(service)
