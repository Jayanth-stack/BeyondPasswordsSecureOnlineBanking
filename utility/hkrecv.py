"""Inbound Hong Kong FPS / CHATS / ECG receive posting.

Staff ingest incoming Hong Kong credits. Recipients enroll FPS proxies
(HKID, +852 mobile, email, 7-digit FPS ID) at this bank's HKMA code.
Matched FPS credits post instantly 24/7. CHATS waits for the
08:30–18:00 HKT HKMA session. ECG waits for the next HK clearing day.
Unmatched proxies wait for a customer claim or staff assignment.
Independent of outbound FPS origination (PR #184), inbound PayNow
(PR #182), domestic Fedwire (PR #73), and ACH linking (PR #68).
Existing `/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay
unchanged.

Foundations (reusable beyond this screen):
- Hong Kong Identity Card checksum (1-2 letters + 6 digits + mod-11)
- +852 8-digit mobile (4/5/6/7/9 national)
- FPS ID (7-digit HKICL addressing identifier)
- Email proxy (HKICL Addressing Service)
- 3-digit HKMA participating bank codes
- FPS End-to-End ID (`FPS`/`CHATS`/`ECG` + YYYYMMDD + seq)
- FPS1 14-pipe file (XML/DOCTYPE and non-HKICL clearing rejected)
- HkClock HKT UTC+8: FPS 24/7; CHATS 08:30–18:00; ECG next HK day
- HKDUSD / CNYUSD quote book converting instructed amount to USD credit
- FPS claim (customer presents matching proxy; enroll rematches unmatched)
- Return windows (FPS 24h customer / CHATS+ECG next HK day / 90-day staff)
- Dual-control at HKD 100k; FPS cap 1m / CHATS 50m; CNY night cap 200k

Reuses Fedwire OFAC screening, money parsing, and account directory
from `utility.wire`. Stores are pluggable (memory for tests, sqlite WAL
for restart-safe default). Full HKID, FPS ID, mobile, email, account,
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
    'ird': 'tax', 'taxes': 'tax',
    'mortgage': 'loan', 'housing': 'rent',
}
RAILS = frozenset({'fps', 'chats', 'ecg'})
RAIL_ALIASES = {
    'faster': 'fps', 'hkicl': 'fps', 'overlay': 'fps', 'instant': 'fps',
    'a2a': 'fps', 'fps_a2a': 'fps', 'paynow': 'fps', 'fast': 'fps',
    'rtgs': 'chats', 'chats_rtgs': 'chats', 'hkma': 'chats', 'meps': 'chats',
    'clearing': 'ecg', 'next_day': 'ecg', 'ach': 'ecg', 'paper': 'ecg', 'giro': 'ecg',
}
PROXY_TYPES = frozenset({'hkid', 'phone', 'email', 'fpsid'})
PROXY_ALIASES = {
    'id': 'hkid', 'hk_id': 'hkid', 'identity': 'hkid', 'nric': 'hkid',
    'mobile': 'phone', 'msisdn': 'phone', 'tel': 'phone', 'sms': 'phone',
    'mail': 'email', 'e_mail': 'email',
    'fps_id': 'fpsid', 'proxy_id': 'fpsid', 'addressing': 'fpsid', 'uen': 'fpsid',
}
CLEARING_BY_RAIL = {'fps': 'FPS', 'chats': 'CHATS', 'ecg': 'ECG'}
REJECTED_CLEARING = frozenset({
    'AUNPP', 'USRTPP', 'FEDNOW', 'FEDWIRE', 'ACH', 'UPI', 'CHIPS', 'SWIFT',
    'PIX', 'SPEI', 'NPP', 'OSKO', 'FAST', 'PAYNOW', 'MEPS', 'GIRO', 'PNW',
})
DEFAULT_BANKS = {
    '004': 'HSBC',
    '003': 'STANDARD CHARTERED',
    '012': 'BANK OF CHINA HONG KONG',
    '024': 'HANG SENG',
    '016': 'DBS',
    '018': 'CHINA CITIC',
    '027': 'BANK OF EAST ASIA',
    '039': 'CHIYU BANK',
    '043': 'NANYANG COMMERCIAL',
    '099': 'KONOHA BANK',
}
FPS1_VERSION = '1'
FPS1_FIELDS = 14
DEFAULT_STORE_PATH = 'SystemLogs/hkrecv.sqlite'
DEFAULT_RECEIVER_BANK = '099'
DEFAULT_SENDER_BANK = '004'
CUSTOMER_RETURN_SECONDS = 24 * 60 * 60
STAFF_COMPLAINT_DAYS = 90
CLAIM_SECONDS = 24 * 60 * 60
HKT_OFFSET_MINUTES = 480
DEFAULT_HKDUSD = Decimal('0.13')
DEFAULT_CNYUSD = Decimal('0.14')
BLOCKED_E2E_PREFIXES = (
    'IMAD', 'UETR', 'NPP', 'AUNPP', 'RRN', 'STAN', 'ARN', 'RFI', 'RFR', 'RTR',
    'PNW', 'FAST', 'MEPS', 'GIRO', 'PIX', 'SPEI', 'UPI', 'PAYNOW',
)
E2E_PREFIX = {'fps': 'FPS', 'chats': 'CHATS', 'ecg': 'ECG'}
HKID_LETTER_BASE = 10
HKID_SPACE = 36
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)


class HkrecvError(ValueError):
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
        raise HkrecvError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise HkrecvError('invalid_name', 'Sender legal name must be 2-80 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise HkrecvError('invalid_purpose', 'Unknown FPS purpose.')
    return text


def normalize_rail(value: Any, *, default: str = 'fps') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text not in RAILS:
        raise HkrecvError('invalid_rail', 'Rail must be fps, chats, or ecg.')
    return text


def normalize_currency(value: Any, *, default: str = 'HKD', rail: str = 'fps') -> str:
    text = str(value or default).strip().upper().replace('$', '')
    if text in {'HK', 'HK$', 'HKD.', 'H'}:
        text = 'HKD'
    if text in {'CN', 'CNY.', 'CNH', 'RMB', 'YUAN'}:
        text = 'CNY'
    if text not in {'HKD', 'CNY'}:
        raise HkrecvError('invalid_currency', 'FPS supports HKD and CNY only.')
    kind = normalize_rail(rail)
    if kind in {'chats', 'ecg'} and text != 'HKD':
        raise HkrecvError('invalid_currency', 'CHATS and ECG are HKD only.')
    return text


def normalize_proxy_type(value: Any, *, default: str = '') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PROXY_ALIASES.get(text, text)
    if not text:
        return ''
    if text not in PROXY_TYPES:
        raise HkrecvError('invalid_proxy', 'FPS proxy must be hkid, phone, email, or fpsid.')
    return text


def normalize_bank_code(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise HkrecvError('invalid_bank', 'HKMA bank code is required.')
        return ''
    if len(digits) < 3:
        digits = digits.zfill(3)
    if len(digits) != 3 or digits == '000':
        raise HkrecvError('invalid_bank', 'HKMA bank code must be three digits.')
    return digits


def mask_bank(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 3:
        return '***'
    return digits[:1] + '**'


def bank_name(value: Any, *, directory: Optional[Dict[str, str]] = None) -> str:
    try:
        code = normalize_bank_code(value)
    except HkrecvError:
        return ''
    mapping = directory if directory is not None else DEFAULT_BANKS
    return mapping.get(code, '')


def normalize_external_account(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise HkrecvError('invalid_account', 'Account is required.')
        return ''
    if not (6 <= len(digits) <= 16):
        raise HkrecvError('invalid_account', 'Hong Kong account must be 6-16 digits.')
    return digits


def mask_account(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return ('*' * max(0, len(digits) - 4)) + digits[-4:]


def _hkid_value(ch: str) -> int:
    if ch == ' ':
        return HKID_SPACE
    if ch.isdigit():
        return int(ch)
    if ch.isalpha():
        return HKID_LETTER_BASE + (ord(ch.upper()) - ord('A'))
    raise HkrecvError('invalid_hkid', 'HKID contains an invalid character.')


def hkid_check_digit(prefix: str, body: str) -> str:
    prefix = prefix.upper()
    if not re.fullmatch(r'[A-Z]{1,2}', prefix) or len(body) != 6 or not body.isdigit():
        raise HkrecvError('invalid_hkid', 'HKID must be 1-2 letters plus 6 digits.')
    padded = prefix.rjust(2)
    total = 0
    weights = (9, 8, 7, 6, 5, 4, 3, 2)
    for ch, weight in zip(padded + body, weights):
        total += _hkid_value(ch) * weight
    remainder = total % 11
    if remainder == 0:
        return '0'
    if remainder == 1:
        return 'A'
    return str(11 - remainder)


def hkid_check_ok(value: str) -> bool:
    text = re.sub(r'[\s()/-]', '', str(value or '')).upper()
    match = re.fullmatch(r'([A-Z]{1,2})(\d{6})([0-9A])', text)
    if not match:
        return False
    try:
        return hkid_check_digit(match.group(1), match.group(2)) == match.group(3)
    except HkrecvError:
        return False


def compose_hkid(prefix: Any, body: Any) -> str:
    letter = str(prefix or '').strip().upper()
    digits = ''.join(ch for ch in str(body or '') if ch.isdigit())
    return letter + digits + hkid_check_digit(letter, digits)


def normalize_hkid(value: Any, *, required: bool = False) -> str:
    text = re.sub(r'[\s()/-]', '', str(value or '')).upper()
    if not text:
        if required:
            raise HkrecvError('invalid_hkid', 'HKID is required.')
        return ''
    if not hkid_check_ok(text):
        raise HkrecvError('invalid_hkid', 'HKID failed the checksum.')
    return text


def mask_hkid(value: Any) -> str:
    text = re.sub(r'[\s()/-]', '', str(value or '')).upper()
    if len(text) < 5:
        return 'A******X'
    prefix_len = 2 if len(text) == 9 else 1
    return text[:prefix_len] + '****' + text[-3:]


def fpsid_check_ok(value: str) -> bool:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return len(digits) == 7 and digits != '0000000'


def compose_fpsid(body: Any) -> str:
    digits = ''.join(ch for ch in str(body or '') if ch.isdigit())
    if len(digits) != 7 or digits == '0000000':
        raise HkrecvError('invalid_fpsid', 'FPS ID must be seven digits.')
    return digits


def normalize_fpsid(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise HkrecvError('invalid_fpsid', 'FPS ID is required.')
        return ''
    if not fpsid_check_ok(digits):
        raise HkrecvError('invalid_fpsid', 'FPS ID must be seven digits.')
    return digits


def mask_fpsid(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 7:
        return '*******'
    return digits[:2] + '***' + digits[-2:]


def normalize_phone(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise HkrecvError('invalid_phone', 'Hong Kong mobile is required.')
        return ''
    if digits.startswith('852') and len(digits) == 11:
        national = digits[3:]
    elif digits.startswith('0') and len(digits) == 9:
        national = digits[1:]
    else:
        national = digits
    if len(national) != 8 or national[0] not in {'4', '5', '6', '7', '9'}:
        raise HkrecvError('invalid_phone', 'FPS mobile must be an 8-digit +852 4/5/6/7/9 number.')
    return '+852' + national


def mask_phone(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if digits.startswith('852') and len(digits) == 11:
        national = digits[3:]
    else:
        national = digits
    if len(national) != 8:
        return '+852****'
    return '+852****' + national[-4:]


def normalize_email(value: Any, *, required: bool = False) -> str:
    text = str(value or '').strip().lower()
    if not text:
        if required:
            raise HkrecvError('invalid_email', 'Email proxy is required.')
        return ''
    if not re.fullmatch(r'[a-z0-9._%+-]{1,64}@[a-z0-9.-]+\.[a-z]{2,24}', text):
        raise HkrecvError('invalid_email', 'FPS email proxy is invalid.')
    return text


def mask_email(value: Any) -> str:
    text = str(value or '').strip().lower()
    if '@' not in text:
        return '***@***'
    local, domain = text.split('@', 1)
    head = local[:1] if local else '*'
    return head + '***@' + domain


def infer_proxy_type(value: Any) -> str:
    text = str(value or '').strip()
    if '@' in text:
        return 'email'
    compact = re.sub(r'[\s()/-]', '', text).upper()
    if hkid_check_ok(compact):
        return 'hkid'
    digits = ''.join(ch for ch in text if ch.isdigit())
    if fpsid_check_ok(digits) and not digits.startswith('852'):
        return 'fpsid'
    if digits.startswith('852') and len(digits) == 11:
        return 'phone'
    if len(digits) == 8 and digits[:1] in {'4', '5', '6', '7', '9'}:
        return 'phone'
    return ''


def normalize_proxy(proxy_type: Any, value: Any, *, required: bool = False) -> Tuple[str, str]:
    kind = normalize_proxy_type(proxy_type)
    if not kind:
        kind = infer_proxy_type(value)
    if not kind:
        if required:
            raise HkrecvError('invalid_proxy', 'FPS proxy type is required.')
        return '', ''
    if kind == 'hkid':
        return kind, normalize_hkid(value, required=True)
    if kind == 'fpsid':
        return kind, normalize_fpsid(value, required=True)
    if kind == 'email':
        return kind, normalize_email(value, required=True)
    return kind, normalize_phone(value, required=True)


def mask_proxy(proxy_type: str, value: str) -> str:
    if proxy_type == 'hkid':
        return mask_hkid(value)
    if proxy_type == 'fpsid':
        return mask_fpsid(value)
    if proxy_type == 'email':
        return mask_email(value)
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
        raise HkrecvError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise HkrecvError('invalid_reference', 'E2E sequence out of range.')
    return '%s%s%06d' % (E2E_PREFIX[kind], cycle_date, seq)


def normalize_e2e(value: Any) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        raise HkrecvError('invalid_reference', 'FPS End-to-End ID is required.')
    if any(text.startswith(prefix) for prefix in BLOCKED_E2E_PREFIXES):
        raise HkrecvError('invalid_reference', 'End-to-End ID must not use another rail prefix.')
    if len(text) == 22 and text[:8].isdigit():
        raise HkrecvError('invalid_reference', 'Fedwire IMAD is not an FPS End-to-End ID.')
    if not re.fullmatch(r'(FPS|CHATS|ECG)\d{14}', text):
        raise HkrecvError('invalid_reference', 'E2E ID must be FPS/CHATS/ECG + date + seq.')
    return text


def compose_amount_field(amount: Decimal) -> str:
    quantized = amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    if quantized < 0 or quantized > Decimal('999999999.99'):
        raise HkrecvError('invalid_amount', 'Amount cannot be encoded in FPS1.')
    return money_str(quantized)


def parse_amount_field(value: Any) -> Decimal:
    try:
        return parse_money(value)
    except AmountError as exc:
        raise HkrecvError('invalid_amount', 'FPS1 amount field is invalid.') from exc


def _reject_markup(text: str) -> None:
    lowered = text.lower()
    if '<xml' in lowered or '<!doctype' in lowered or '<?xml' in lowered or '<fps' in lowered or '<fast' in lowered:
        raise HkrecvError('invalid_file', 'XML/DOCTYPE FPS1 files are rejected.')


def compose_fps1(
    *,
    e2e: str,
    rail: str,
    proxy_type: str,
    proxy: str,
    bank: str,
    account_number: str,
    extra: str,
    amount: Decimal,
    purpose: str,
    status: str,
    currency: str = 'HKD',
    mask: bool = True,
) -> str:
    kind = normalize_rail(rail)
    clearing = CLEARING_BY_RAIL[kind]
    money = currency if isinstance(currency, str) else 'HKD'
    money = normalize_currency(money, rail=kind)
    return '|'.join((
        'FPS1',
        FPS1_VERSION,
        mask_e2e(e2e) if mask else normalize_e2e(e2e),
        kind,
        proxy_type or '',
        mask_proxy(proxy_type, proxy) if mask and proxy else (proxy or ''),
        mask_bank(bank) if mask and bank else (normalize_bank_code(bank, required=False) if bank else ''),
        mask_account(account_number) if mask and account_number else (account_number or ''),
        mask_fpsid(extra) if mask and extra else (extra or ''),
        compose_amount_field(amount),
        normalize_purpose(purpose),
        str(status or IN_POSTED),
        money,
        clearing,
    ))


def parse_fps1(text: Any) -> Dict[str, Any]:
    raw = str(text or '').strip()
    if not raw:
        raise HkrecvError('invalid_file', 'FPS1 record is empty.')
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != FPS1_FIELDS or parts[0] != 'FPS1' or parts[1] != FPS1_VERSION:
        raise HkrecvError('invalid_file', 'FPS1 record is malformed.')
    clearing = parts[13].strip().upper()
    if clearing in REJECTED_CLEARING or clearing not in {'FPS', 'CHATS', 'ECG'}:
        raise HkrecvError('invalid_file', 'FPS1 clearing must be FPS/CHATS/ECG.')
    joined = '|'.join(parts[2:9])
    if '*' in joined or '…' in joined:
        raise HkrecvError('invalid_file', 'Masked FPS1 record cannot be re-ingested.')
    proxy_type = normalize_proxy_type(parts[4]) if parts[4] else ''
    proxy = ''
    if parts[5]:
        proxy_type, proxy = normalize_proxy(proxy_type, parts[5], required=True)
    rail = normalize_rail(parts[3])
    return {
        'e2e': normalize_e2e(parts[2]),
        'rail': rail,
        'proxy_type': proxy_type,
        'proxy': proxy,
        'bank': normalize_bank_code(parts[6], required=bool(parts[6])),
        'account_number': normalize_external_account(parts[7], required=bool(parts[7])),
        'extra': normalize_fpsid(parts[8], required=False) if parts[8] else '',
        'amount': parse_amount_field(parts[9]),
        'purpose': normalize_purpose(parts[10]),
        'status': parts[11],
        'currency': normalize_currency(parts[12], rail=rail),
        'clearing': clearing,
        'name': '',
    }


def split_fps1_file(text: Any) -> List[str]:
    raw = str(text or '')
    _reject_markup(raw)
    records = [line.strip() for line in raw.replace('\r\n', '\n').split('\n') if line.strip()]
    if not records:
        raise HkrecvError('invalid_file', 'FPS1 file is empty.')
    return records


def convert_instructed_to_usd(amount: Decimal, rate: Decimal) -> Decimal:
    if rate <= 0:
        raise HkrecvError('invalid_fx', 'FX rate must be positive.')
    return (amount * rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def message_from_values(values: Dict[str, Any]) -> Dict[str, Any]:
    recv = normalize_bank_code(
        values.get('recv_bank') or values.get('receiver') or values.get('bank') or DEFAULT_RECEIVER_BANK,
    )
    send = normalize_bank_code(
        values.get('send_bank') or values.get('sender_bank') or DEFAULT_SENDER_BANK,
        required=False,
    ) or DEFAULT_SENDER_BANK
    proxy_raw = (
        values.get('proxy') or values.get('fps') or values.get('key')
        or values.get('hkid') or values.get('paynow') or values.get('payid')
    )
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
            raise HkrecvError('invalid_amount', 'Amount is invalid.') from exc
    e2e = str(values.get('end_to_end_id') or values.get('e2e') or '').strip()
    rail = normalize_rail(values.get('rail') or 'fps')
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
    currency = normalize_currency(values.get('currency') or 'HKD', rail=rail)
    return {
        'e2e': e2e,
        'rail': rail,
        'proxy_type': kind,
        'proxy': proxy,
        'bank': recv,
        'account_number': dest_acct,
        'extra': normalize_fpsid(values.get('fpsid'), required=False) if values.get('fpsid') else '',
        'amount': amount,
        'purpose': normalize_purpose(values.get('purpose')),
        'name': name or 'FPS PAYER',
        'currency': currency,
        'send_bank': send,
        'recv_bank': recv,
    }


def _observed_sunday(day: date) -> date:
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def hk_holidays(year: int) -> set:
    """Fixed HK-observed holidays. Moveable lunar dates are extra_holidays."""
    return {
        _observed_sunday(date(year, 1, 1)),
        _observed_sunday(date(year, 5, 1)),
        _observed_sunday(date(year, 7, 1)),
        _observed_sunday(date(year, 10, 1)),
        _observed_sunday(date(year, 12, 25)),
        _observed_sunday(date(year, 12, 26)),
    }


class HkClock:
    """Hong Kong FPS 24/7 vs CHATS/ECG banking-day clock. Injectable offset and holidays."""

    def __init__(
        self,
        *,
        chats_open_hour: int = 8,
        chats_open_minute: int = 30,
        chats_cutoff_hour: int = 18,
        chats_cutoff_minute: int = 0,
        tz_offset_minutes: int = HKT_OFFSET_MINUTES,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.chats_open_hour = int(chats_open_hour)
        self.chats_open_minute = int(chats_open_minute)
        self.chats_cutoff_hour = int(chats_cutoff_hour)
        self.chats_cutoff_minute = int(chats_cutoff_minute)
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
        return hk_holidays(year) | {day for day in self.extra_holidays if day.year == year}

    def is_weekend(self, day: date) -> bool:
        return day.weekday() >= 5

    def is_holiday(self, day: date) -> bool:
        return day in self.holidays(day.year)

    def is_business_day(self, day: date) -> bool:
        return not self.is_weekend(day) and not self.is_holiday(day)

    def is_after_chats_cutoff(self, ts: float) -> bool:
        local = self.local_dt(ts)
        if (local.hour, local.minute) >= (self.chats_cutoff_hour, self.chats_cutoff_minute):
            return True
        if (local.hour, local.minute) < (self.chats_open_hour, self.chats_open_minute):
            return True
        return False

    def is_session(self, ts: float) -> bool:
        local = self.local_dt(ts)
        return self.is_business_day(local.date()) and not self.is_after_chats_cutoff(ts)

    def is_cny_night(self, ts: float) -> bool:
        local = self.local_dt(ts)
        return local.hour >= 18 or local.hour < 8 or not self.is_business_day(local.date())

    def next_business_day(self, day: date) -> date:
        cursor = day + timedelta(days=1)
        while not self.is_business_day(cursor):
            cursor += timedelta(days=1)
        return cursor

    def value_date(self, ts: float, *, rail: str = 'fps') -> date:
        local = self.local_dt(ts)
        kind = normalize_rail(rail)
        day = local.date()
        if kind == 'fps':
            return day
        if kind == 'ecg':
            return self.next_business_day(day)
        if not self.is_business_day(day) or self.is_after_chats_cutoff(ts):
            return self.next_business_day(day)
        return day

    def cycle_date(self, ts: float, *, rail: str = 'fps') -> str:
        return self.value_date(ts, rail=rail).strftime('%Y%m%d')

    def session_close_ts(self, day: date) -> float:
        local = datetime(
            day.year, day.month, day.day,
            self.chats_cutoff_hour, self.chats_cutoff_minute, tzinfo=self.tz,
        )
        return local.timestamp()

    def snapshot(self, ts: float, *, rail: str = 'fps') -> Dict[str, Any]:
        local = self.local_dt(ts)
        day = local.date()
        kind = normalize_rail(rail)
        session = self.is_session(ts)
        after = False
        if kind in {'chats', 'ecg'}:
            after = not session
        return {
            'rail': kind,
            'local_date': day.isoformat(),
            'local_time': local.strftime('%H:%M'),
            'timezone': 'HKT',
            'open': '%02d:%02d' % (self.chats_open_hour, self.chats_open_minute),
            'cutoff': '%02d:%02d' % (self.chats_cutoff_hour, self.chats_cutoff_minute),
            'session': session,
            'after_cutoff': after,
            'always_on': kind == 'fps',
            'cny_night': self.is_cny_night(ts),
            'weekend': self.is_weekend(day),
            'holiday': self.is_holiday(day),
            'business_day': self.is_business_day(day),
            'value_date': self.value_date(ts, rail=kind).isoformat(),
            'cycle_date': self.cycle_date(ts, rail=kind),
            'settlement_method': 'fps',
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
class HkrecvPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_claim: bool = True
    customer_return: bool = True
    allow_credit: bool = False
    max_keys: int = 12
    max_inbounds: int = 120
    min_amount: Decimal = Decimal('0.01')
    fps_cap: Decimal = Decimal('1000000.00')
    ecg_cap: Decimal = Decimal('1000000.00')
    chats_cap: Decimal = Decimal('50000000.00')
    dual_control_threshold: Decimal = Decimal('100000.00')
    hkdusd: Decimal = DEFAULT_HKDUSD
    cnyusd: Decimal = DEFAULT_CNYUSD
    cny_night_cap: Decimal = Decimal('200000.00')
    tz_offset_minutes: int = HKT_OFFSET_MINUTES
    chats_open_hour: int = 8
    chats_open_minute: int = 30
    chats_cutoff_hour: int = 18
    chats_cutoff_minute: int = 0
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
        if rail == 'chats':
            return self.chats_cap
        if rail == 'ecg':
            return self.ecg_cap
        return self.fps_cap

    def rate_for(self, currency: str) -> Decimal:
        if currency == 'CNY':
            return self.cnyusd
        return self.hkdusd

    @classmethod
    def from_env(cls) -> 'HkrecvPolicy':
        extra = _env_list('HKRECV_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        source = os.environ.get('HKRECV_RECEIVER') or DEFAULT_RECEIVER_BANK
        try:
            receiver = normalize_bank_code(source)
        except HkrecvError:
            receiver = DEFAULT_RECEIVER_BANK
        return cls(
            enabled=_env_bool('HKRECV_ENABLED', True),
            customer_manage=_env_bool('HKRECV_CUSTOMER_MANAGE', True),
            customer_claim=_env_bool('HKRECV_CUSTOMER_CLAIM', True),
            customer_return=_env_bool('HKRECV_CUSTOMER_RETURN', True),
            allow_credit=_env_bool('HKRECV_ALLOW_CREDIT', False),
            max_keys=max(1, _env_int('HKRECV_MAX_KEYS', 12)),
            max_inbounds=max(1, _env_int('HKRECV_MAX_INBOUNDS', 120)),
            min_amount=_env_money('HKRECV_MIN_AMOUNT', '0.01'),
            fps_cap=_env_money('HKRECV_FPS_CAP', '1000000.00'),
            ecg_cap=_env_money('HKRECV_ECG_CAP', '1000000.00'),
            chats_cap=_env_money('HKRECV_CHATS_CAP', '50000000.00'),
            dual_control_threshold=_env_money('HKRECV_DUAL_CONTROL', '100000.00'),
            hkdusd=_env_money('HKRECV_FX_HKDUSD', '0.13'),
            cnyusd=_env_money('HKRECV_FX_CNYUSD', '0.14'),
            cny_night_cap=_env_money('HKRECV_CNY_NIGHT_CAP', '200000.00'),
            tz_offset_minutes=_env_int('HKRECV_TZ_OFFSET', HKT_OFFSET_MINUTES),
            chats_open_hour=max(0, min(23, _env_int('HKRECV_OPEN_HOUR', 8))),
            chats_open_minute=max(0, min(59, _env_int('HKRECV_OPEN_MINUTE', 30))),
            chats_cutoff_hour=max(0, min(23, _env_int('HKRECV_CUTOFF_HOUR', 18))),
            chats_cutoff_minute=max(0, min(59, _env_int('HKRECV_CUTOFF_MINUTE', 0))),
            receiver_bank=receiver,
            customer_return_seconds=max(60, _env_int('HKRECV_RETURN_SECONDS', CUSTOMER_RETURN_SECONDS)),
            staff_complaint_days=max(1, _env_int('HKRECV_COMPLAINT_DAYS', STAFF_COMPLAINT_DAYS)),
            claim_seconds=max(60, _env_int('HKRECV_CLAIM_SECONDS', CLAIM_SECONDS)),
            watchlist=watch,
            extra_holidays=_env_list('HKRECV_HOLIDAYS'),
        )


@dataclass
class HkrecvKey:
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
class HkrecvInbound:
    inbound_id: str
    e2e: str
    userid: str
    destination_account: str
    amount_instructed: str
    amount_usd: str
    currency: str
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
            'amount_instructed': self.amount_instructed,
            'amount_usd': self.amount_usd,
            'amount_hkd': self.amount_instructed if self.currency == 'HKD' else '',
            'currency': self.currency,
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


def _key_from_row(row: Any) -> HkrecvKey:
    data = dict(row)
    return HkrecvKey(
        key_id=data['key_id'], userid=data['userid'], nickname=data['nickname'],
        proxy_type=data['proxy_type'], proxy=data['proxy'], fingerprint=data['fingerprint'],
        destination_account=data['destination_account'], status=data['status'], actor=data['actor'],
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
    )


def _inbound_from_row(row: Any) -> HkrecvInbound:
    data = dict(row)
    return HkrecvInbound(
        inbound_id=data['inbound_id'], e2e=data['e2e'], userid=data['userid'],
        destination_account=data['destination_account'], amount_instructed=data['amount_instructed'],
        amount_usd=data['amount_usd'], currency=data.get('currency') or 'HKD',
        sender_name=data['sender_name'],
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


class MemoryHkrecvStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.keys: Dict[str, HkrecvKey] = {}
        self.inbounds: Dict[str, HkrecvInbound] = {}
        self._by_e2e: Dict[str, str] = {}

    def put_key(self, row: HkrecvKey) -> None:
        with self._lock:
            self.keys[row.key_id] = row

    def get_key(self, key_id: str) -> Optional[HkrecvKey]:
        return self.keys.get(key_id)

    def get_key_by_fingerprint(self, fingerprint: str) -> Optional[HkrecvKey]:
        matches = [
            row for row in self.keys.values()
            if row.fingerprint == fingerprint and row.status in OPEN_KEY
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda row: row.created_at, reverse=True)[0]

    def update_key(self, row: HkrecvKey) -> None:
        self.put_key(row)

    def list_keys(self, userid: Optional[str] = None) -> List[HkrecvKey]:
        rows = list(self.keys.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        return sorted(rows, key=lambda row: row.created_at)

    def put_inbound(self, row: HkrecvInbound) -> None:
        with self._lock:
            self.inbounds[row.inbound_id] = row
            self._by_e2e[row.e2e] = row.inbound_id

    def get_inbound(self, inbound_id: str) -> Optional[HkrecvInbound]:
        return self.inbounds.get(inbound_id)

    def get_by_e2e(self, e2e: str) -> Optional[HkrecvInbound]:
        inbound_id = self._by_e2e.get(e2e)
        return self.inbounds.get(inbound_id) if inbound_id else None

    def update_inbound(self, row: HkrecvInbound) -> None:
        self.put_inbound(row)

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[HkrecvInbound]:
        rows = list(self.inbounds.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)


class SqliteHkrecvStore:
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
                    amount_instructed TEXT NOT NULL,
                    amount_usd TEXT NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'HKD',
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

    def put_key(self, row: HkrecvKey) -> None:
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

    def get_key(self, key_id: str) -> Optional[HkrecvKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM keys WHERE key_id = ?', (key_id,)).fetchone()
        return _key_from_row(row) if row else None

    def get_key_by_fingerprint(self, fingerprint: str) -> Optional[HkrecvKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM keys WHERE fingerprint = ? AND status IN ('active', 'paused')
                ORDER BY created_at DESC LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
        return _key_from_row(row) if row else None

    def update_key(self, row: HkrecvKey) -> None:
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

    def list_keys(self, userid: Optional[str] = None) -> List[HkrecvKey]:
        sql = 'SELECT * FROM keys'
        args: Tuple[Any, ...] = ()
        if userid is not None:
            sql += ' WHERE userid = ?'
            args = (userid,)
        sql += ' ORDER BY created_at'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_key_from_row(row) for row in rows]

    def put_inbound(self, row: HkrecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inbounds (
                    inbound_id, e2e, userid, destination_account, amount_instructed, amount_usd,
                    currency, sender_name, send_bank, recv_bank, rail, proxy_type, proxy, fingerprint,
                    account_number, purpose, status, reason, actor, releaser, ofac_hit,
                    ofac_match, ofac_overridden, created_at, updated_at, posted_at,
                    returned_at, expiry_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.inbound_id, row.e2e, row.userid, row.destination_account, row.amount_instructed,
                    row.amount_usd, row.currency, row.sender_name, row.send_bank, row.recv_bank, row.rail,
                    row.proxy_type, row.proxy, row.fingerprint, row.account_number, row.purpose,
                    row.status, row.reason, row.actor, row.releaser, int(row.ofac_hit),
                    row.ofac_match, int(row.ofac_overridden), row.created_at, row.updated_at,
                    row.posted_at, row.returned_at, row.expiry_at, row.note,
                ),
            )
            conn.commit()

    def get_inbound(self, inbound_id: str) -> Optional[HkrecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,)).fetchone()
        return _inbound_from_row(row) if row else None

    def get_by_e2e(self, e2e: str) -> Optional[HkrecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE e2e = ?', (e2e,)).fetchone()
        return _inbound_from_row(row) if row else None

    def update_inbound(self, row: HkrecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE inbounds SET userid=?, destination_account=?, amount_instructed=?, amount_usd=?,
                    currency=?, sender_name=?, send_bank=?, recv_bank=?, rail=?, proxy_type=?, proxy=?,
                    fingerprint=?, account_number=?, purpose=?, status=?, reason=?, actor=?,
                    releaser=?, ofac_hit=?, ofac_match=?, ofac_overridden=?, updated_at=?,
                    posted_at=?, returned_at=?, expiry_at=?, note=?
                WHERE inbound_id=?
                """,
                (
                    row.userid, row.destination_account, row.amount_instructed, row.amount_usd,
                    row.currency, row.sender_name, row.send_bank, row.recv_bank, row.rail, row.proxy_type,
                    row.proxy, row.fingerprint, row.account_number, row.purpose, row.status,
                    row.reason, row.actor, row.releaser, int(row.ofac_hit), row.ofac_match,
                    int(row.ofac_overridden), row.updated_at, row.posted_at, row.returned_at,
                    row.expiry_at, row.note, row.inbound_id,
                ),
            )
            conn.commit()

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[HkrecvInbound]:
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


class HkrecvService:
    def __init__(
        self,
        policy: HkrecvPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[HkClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: datetime.now(tz=timezone.utc).timestamp())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn or screen_name
        self.calendar = calendar or HkClock(
            chats_open_hour=policy.chats_open_hour,
            chats_open_minute=policy.chats_open_minute,
            chats_cutoff_hour=policy.chats_cutoff_hour,
            chats_cutoff_minute=policy.chats_cutoff_minute,
            tz_offset_minutes=policy.tz_offset_minutes,
            extra_holidays=policy.extra_holidays,
        )

    def _now(self) -> float:
        return float(self.clock())

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise HkrecvError('hkrecv_disabled', 'FPS receive is disabled.')

    def _require_staff(self, actor_type: str) -> None:
        if actor_type not in EMPLOYEE_ROLES:
            raise HkrecvError('hkrecv_forbidden', 'Staff only.')

    def _require_customer_manage(self, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if not self.policy.customer_manage:
            raise HkrecvError('hkrecv_forbidden', 'Customers cannot manage FPS proxies.')

    def _accounts(self, userid: str) -> Any:
        if not self.accounts_fn:
            return {}
        return self.accounts_fn(userid) or {}

    def _owned(self, userid: str, account: str) -> None:
        owned = own_accounts_from_customer_payload(self._accounts(userid))
        if account not in owned:
            raise HkrecvError('invalid_account', 'Destination is not an account of this customer.')

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
    ) -> HkrecvKey:
        self._require_enabled()
        self._require_customer_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise HkrecvError('hkrecv_forbidden', 'Cannot enroll a FPS proxy for another customer.')
        kind, value = normalize_proxy(proxy_type, proxy, required=True)
        account = normalize_account(destination_account)
        self._owned(owner_userid, account)
        if not self._credit_allowed(owner_userid, account):
            raise HkrecvError('credit_not_allowed', 'Credit accounts cannot receive FPS.')
        fingerprint = proxy_fingerprint(kind, value)
        existing = self.store.get_key_by_fingerprint(fingerprint)
        if existing and existing.status in OPEN_KEY:
            raise HkrecvError('key_duplicate', 'That FPS proxy is already enrolled.')
        open_rows = [row for row in self.store.list_keys(owner_userid) if row.status in OPEN_KEY]
        if any(row.nickname.lower() == normalize_nickname(nickname).lower() for row in open_rows):
            raise HkrecvError('key_duplicate', 'That nickname is already in use.')
        if len(open_rows) >= self.policy.max_keys:
            raise HkrecvError('key_limit', 'FPS enrollment limit reached.')
        now = self._now()
        row = HkrecvKey(
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
    ) -> HkrecvKey:
        self._require_enabled()
        row = self.store.get_key(key_id)
        if row is None:
            raise HkrecvError('key_not_found', 'FPS enrollment not found.')
        if actor_type not in EMPLOYEE_ROLES and actor != row.userid:
            raise HkrecvError('hkrecv_forbidden', 'Cannot change another customer FPS proxy.')
        self._require_customer_manage(actor_type)
        if status not in KEY_STATUSES:
            raise HkrecvError('invalid_status', 'Unknown FPS status.')
        if row.status == KEY_CANCELLED:
            raise HkrecvError('already_cancelled', 'Cancelled FPS proxies cannot change.')
        if status == KEY_PAUSED and row.status != KEY_ACTIVE:
            raise HkrecvError('already_paused' if row.status == KEY_PAUSED else 'invalid_status', 'FPS proxy is not active.')
        if status == KEY_ACTIVE and row.status != KEY_PAUSED:
            raise HkrecvError('already_active', 'FPS proxy is already active.')
        if status == row.status:
            code = 'already_paused' if status == KEY_PAUSED else 'already_active'
            raise HkrecvError(code, 'FPS proxy already in that status.')
        row.status = status
        row.actor = actor
        row.updated_at = self._now()
        self.store.update_key(row)
        if status == KEY_ACTIVE:
            self._rematch_unmatched(row)
        return row

    def _rematch_unmatched(self, enrollment: HkrecvKey) -> None:
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
    ) -> Tuple[HkrecvInbound, bool]:
        self._require_enabled()
        self._require_staff(actor_type)
        payload = dict(values or {})
        if not payload.get('end_to_end_id') and not payload.get('e2e') and not payload.get('cycle_date') and not payload.get('value_date'):
            payload['cycle_date'] = self.calendar.cycle_date(
                self._now(), rail=normalize_rail(payload.get('rail') or 'fps'),
            )
        parsed = message_from_values(payload)
        return self._ingest_parsed(actor=actor, parsed=parsed)

    def ingest_file(
        self,
        *,
        actor: str,
        actor_type: str,
        payload: str,
        sender_name: str = '',
        send_bank: str = '',
    ) -> Tuple[List[HkrecvInbound], int]:
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
        for record in split_fps1_file(payload):
            parsed = parse_fps1(record)
            parsed['recv_bank'] = parsed.get('bank') or self.policy.receiver_bank
            parsed['send_bank'] = sender or DEFAULT_SENDER_BANK
            if fallback:
                parsed['name'] = fallback
            elif not parsed.get('name'):
                parsed['name'] = 'FPS PAYER'
            row, is_new = self._ingest_parsed(actor=actor, parsed=parsed)
            rows.append(row)
            if is_new:
                created += 1
        return rows, created

    def _ingest_parsed(self, *, actor: str, parsed: Dict[str, Any]) -> Tuple[HkrecvInbound, bool]:
        recv = parsed.get('recv_bank') or parsed.get('bank') or self.policy.receiver_bank
        if recv != self.policy.receiver_bank:
            raise HkrecvError('wrong_receiver', 'FPS credit is not for this HKMA bank code.')
        existing = self.store.get_by_e2e(parsed['e2e'])
        if existing is not None:
            return existing, False
        total = len(self.store.list_inbounds())
        if total >= self.policy.max_inbounds:
            raise HkrecvError('inbound_limit', 'Inbound FPS limit reached.')
        amount = parsed['amount'] if isinstance(parsed['amount'], Decimal) else parse_money(parsed['amount'])
        rail = normalize_rail(parsed.get('rail') or 'fps')
        cap = self.policy.cap_for(rail)
        if amount < self.policy.min_amount or amount > cap:
            code = 'fps_amount_exceeded' if amount > cap else 'amount_out_of_range'
            raise HkrecvError(code, 'Amount is outside the %s range.' % rail)
        now = self._now()
        currency = normalize_currency(parsed.get('currency') or 'HKD', rail=rail)
        if currency == 'CNY' and self.calendar.is_cny_night(now) and amount > self.policy.cny_night_cap:
            raise HkrecvError('cny_night_cap', 'CNY FPS night cap exceeded.')
        proxy = parsed.get('proxy') or ''
        kind = parsed.get('proxy_type') or ''
        fingerprint = proxy_fingerprint(kind, proxy) if proxy else ''
        usd = convert_instructed_to_usd(amount, self.policy.rate_for(currency))
        row = HkrecvInbound(
            inbound_id=uuid.uuid4().hex,
            e2e=parsed['e2e'],
            userid='',
            destination_account='',
            amount_instructed=money_str(amount),
            amount_usd=money_str(usd),
            currency=currency,
            sender_name=parsed.get('name') or 'FPS PAYER',
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

    def _evaluate(self, row: HkrecvInbound, *, rescreen: bool) -> HkrecvInbound:
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
        except HkrecvError:
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
        instructed = parse_money(row.amount_instructed)
        if instructed >= self.policy.dual_control_threshold and not row.releaser:
            row.status = IN_PENDING
            row.reason = 'dual_control'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if row.rail in {'chats', 'ecg'} and not self.calendar.is_session(now):
            row.status = IN_QUEUED
            row.reason = 'chats_session' if row.rail == 'chats' else 'ecg_session'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if row.rail == 'ecg':
            local = self.calendar.local_dt(now).date()
            created = self.calendar.local_dt(row.created_at).date()
            if local <= created and self.calendar.is_session(now):
                # ECG posts on the next HK clearing day, not same-session.
                row.status = IN_QUEUED
                row.reason = 'ecg_session'
                row.updated_at = now
                self.store.update_inbound(row)
                return row
        return self._post(row)

    def _post(self, row: HkrecvInbound) -> HkrecvInbound:
        if self.credit_fn is None:
            raise HkrecvError('failed', 'Credit function is not configured.')
        remark = 'fps from %s' % row.sender_name[:40]
        result = self.credit_fn(row.destination_account, row.amount_usd, remark)
        classified = _classify_money_result(result)
        now = self._now()
        if classified == 'nsf':
            row.status = IN_FAILED
            row.reason = 'nsf'
            row.updated_at = now
            self.store.update_inbound(row)
            raise HkrecvError('nsf', 'Credit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            row.status = IN_FAILED
            row.reason = 'failed'
            row.updated_at = now
            self.store.update_inbound(row)
            raise HkrecvError('failed', 'Credit failed.', inbound=row)
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

    def _get(self, *, inbound_id: str = '', e2e: str = '') -> HkrecvInbound:
        row = None
        if inbound_id:
            row = self.store.get_inbound(str(inbound_id).strip())
        if row is None and e2e:
            row = self.store.get_by_e2e(normalize_e2e(e2e))
        if row is None:
            raise HkrecvError('inbound_not_found', 'Inbound FPS not found.')
        return row

    def _assert_visible(self, row: HkrecvInbound, actor: str, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if row.userid and row.userid != actor:
            raise HkrecvError('hkrecv_forbidden', 'Cannot view another customer FPS.')

    def assign(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        customer_id: str,
        account: Any,
        nickname: str = '',
    ) -> HkrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_UNMATCHED:
            raise HkrecvError('not_assignable', 'Only unmatched FPS credits can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise HkrecvError('missing_customer_id', 'Customer id is required to assign.')
        dest = normalize_account(account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise HkrecvError('credit_not_allowed', 'Credit accounts cannot receive FPS.')
        if row.proxy:
            existing = self.store.get_key_by_fingerprint(row.fingerprint)
            if existing is None or existing.status == KEY_CANCELLED:
                self.add_key(
                    owner_userid=owner, actor=actor, actor_type=actor_type,
                    nickname=nickname or ('FPS %s' % (row.proxy_type or 'proxy')),
                    proxy_type=row.proxy_type, proxy=row.proxy, destination_account=dest,
                )
                fresh = self.store.get_inbound(row.inbound_id)
                if fresh is not None and fresh.status != IN_UNMATCHED:
                    return fresh
            elif existing.userid != owner:
                raise HkrecvError('key_duplicate', 'FPS proxy belongs to another customer.')
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
    ) -> HkrecvInbound:
        """FPS claim: customer presents the matching proxy for an unmatched credit."""
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_claim:
                raise HkrecvError('hkrecv_forbidden', 'Customers cannot claim FPS credits.')
            owner = actor
        else:
            owner = str(owner_userid or '').strip()
            if not owner:
                raise HkrecvError('missing_customer_id', 'Customer id is required to claim.')
        if not proxy:
            raise HkrecvError('missing_key', 'FPS proxy is required to claim.')
        kind, value = normalize_proxy(proxy_type, proxy, required=True)
        fingerprint = proxy_fingerprint(kind, value)
        dest = normalize_account(destination_account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise HkrecvError('credit_not_allowed', 'Credit accounts cannot receive FPS.')
        row = None
        if inbound_id:
            row = self._get(inbound_id=inbound_id)
            if row.fingerprint != fingerprint:
                raise HkrecvError('not_claimable', 'Presented proxy does not match this FPS.')
        else:
            for candidate in self.store.list_inbounds(status=IN_UNMATCHED):
                if candidate.fingerprint == fingerprint:
                    row = candidate
                    break
        if row is None:
            raise HkrecvError('inbound_not_found', 'No unmatched FPS for that proxy.')
        if row.status != IN_UNMATCHED:
            raise HkrecvError('not_claimable', 'This FPS credit is not waiting for a claim.')
        if row.expiry_at and self._now() > row.expiry_at:
            row.status = IN_EXPIRED
            row.reason = 'expired'
            row.updated_at = self._now()
            self.store.update_inbound(row)
            raise HkrecvError('return_window_closed', 'FPS claim window has closed.')
        existing = self.store.get_key_by_fingerprint(fingerprint)
        if existing is None or existing.status == KEY_CANCELLED:
            self.add_key(
                owner_userid=owner, actor=actor, actor_type=actor_type,
                nickname=nickname or ('FPS %s' % kind),
                proxy_type=kind, proxy=value, destination_account=dest,
            )
        elif existing.userid != owner:
            raise HkrecvError('key_duplicate', 'FPS proxy belongs to another customer.')
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

    def override_ofac(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> HkrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_HELD:
            raise HkrecvError('not_overridable', 'Only OFAC-held FPS credits can be overridden.')
        row.ofac_overridden = True
        row.ofac_hit = False
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def release(self, *, inbound_id: str, actor: str, actor_type: str) -> HkrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_PENDING:
            raise HkrecvError('not_releasable', 'Only dual-control FPS credits can be released.')
        if actor == row.actor:
            raise HkrecvError('same_approver', 'A different employee must release this credit.')
        row.releaser = actor
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def reject(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> HkrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status not in OPEN_IN:
            raise HkrecvError('not_rejectable', 'This FPS credit cannot be rejected.')
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
            raise HkrecvError('not_exportable', 'FPS credit has no End-to-End ID to export.')
        _ = actor
        record = compose_fps1(
            e2e=row.e2e,
            rail=row.rail,
            proxy_type=row.proxy_type,
            proxy=row.proxy,
            bank=row.recv_bank,
            account_number=row.account_number,
            extra=row.proxy if row.proxy_type == 'fpsid' else '',
            amount=parse_money(row.amount_instructed, allow_zero=True),
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
    ) -> HkrecvInbound:
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
    ) -> HkrecvInbound:
        self._require_enabled()
        row = self._get(inbound_id=inbound_id)
        self._assert_visible(row, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_return:
                raise HkrecvError('hkrecv_forbidden', 'Customers cannot return FPS credits.')
            if actor != row.userid:
                raise HkrecvError('hkrecv_forbidden', 'Cannot return another customer FPS.')
        return self._return(row, actor=actor, actor_type=actor_type, reason=reason, honor_window=True)

    def _customer_deadline(self, row: HkrecvInbound) -> float:
        posted = row.posted_at or row.created_at
        if row.rail in {'ecg', 'chats'}:
            day = self.calendar.local_dt(posted).date()
            return self.calendar.session_close_ts(self.calendar.next_business_day(day))
        return posted + self.policy.customer_return_seconds

    def _return(
        self,
        row: HkrecvInbound,
        *,
        actor: str,
        actor_type: str,
        reason: str,
        honor_window: bool,
    ) -> HkrecvInbound:
        now = self._now()
        _ = actor
        if row.status == IN_RETURNED or row.returned_at:
            raise HkrecvError('already_returned', 'FPS credit already returned.')
        if row.status in OPEN_IN:
            row.status = IN_RETURNED
            row.reason = reason or 'cust'
            row.returned_at = now
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if row.status != IN_POSTED:
            raise HkrecvError('not_returnable', 'This FPS credit cannot be returned.')
        if honor_window and actor_type not in EMPLOYEE_ROLES:
            if now > self._customer_deadline(row):
                raise HkrecvError('return_window_closed', 'Customer FPS return window has closed.')
        if actor_type in EMPLOYEE_ROLES:
            elapsed = now - (row.posted_at or row.created_at)
            if elapsed > self.policy.staff_complaint_days * 86400:
                raise HkrecvError('med_window_closed', 'Staff complaint window is 90 days.')
        if self.debit_fn is None:
            raise HkrecvError('return_failed', 'Debit function is not configured.')
        remark = 'fps return %s' % row.e2e[-12:]
        result = self.debit_fn(row.destination_account, row.amount_usd, remark)
        classified = _classify_money_result(result)
        if classified == 'nsf':
            raise HkrecvError('nsf', 'Return debit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            raise HkrecvError('return_failed', 'Return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.reason = reason or 'cust'
        row.returned_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[HkrecvInbound]:
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
        ytd = sum((parse_money(row.amount_instructed, allow_zero=True) for row in received), Decimal('0.00'))
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
            'fps_cap': money_str(self.policy.fps_cap),
            'chats_cap': money_str(self.policy.chats_cap),
            'ecg_cap': money_str(self.policy.ecg_cap),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'settlement_method': 'fps',
            'currency': 'HKD',
            'fx_hkdusd': money_str(self.policy.hkdusd),
            'fx_cnyusd': money_str(self.policy.cnyusd),
        }

    def unmatched_snapshot(self) -> Dict[str, Any]:
        rows = self.store.list_inbounds(status=IN_UNMATCHED)
        return {
            'enabled': self.policy.enabled,
            'unmatched': [row.to_dict() for row in rows[:40]],
            'count': len(rows),
        }


_SERVICE: Optional[HkrecvService] = None


def set_service(service: Optional[HkrecvService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[HkrecvService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('HKRECV_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryHkrecvStore()
    path = os.environ.get('HKRECV_DB', DEFAULT_STORE_PATH)
    return SqliteHkrecvStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[HkClock] = None,
) -> HkrecvService:
    if store is None:
        store = default_store()
    return HkrecvService(
        HkrecvPolicy.from_env(),
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
        'hkrecv_forbidden': 403,
        'hkrecv_disabled': 403,
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
        'invalid_hkid': 400,
        'invalid_fpsid': 400,
        'invalid_email': 400,
        'cny_night_cap': 400,
        'invalid_rail': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_reference': 400,
        'invalid_file': 400,
        'invalid_currency': 400,
        'invalid_fx': 400,
        'wrong_receiver': 400,
        'fps_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_key': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: HkrecvError) -> Dict[str, Any]:
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
    except HkrecvError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snapshot_for(service: HkrecvService, userid: str, actor: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(userid, actor=actor, actor_type=actor_type)


def handle_list(service: HkrecvService):
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
    return jsonify({'FpsIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def handle_unmatched(service: HkrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'hkrecv_forbidden'}), 403
    snap = service.unmatched_snapshot()
    return jsonify({'UnmatchedFpsIns': snap, 'FpsIn': snap}), 200


def handle_add_key(service: HkrecvService):
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
                raise HkrecvError('missing_customer_id', 'Customer id is required.')
        row = service.add_key(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname') or '',
            proxy_type=values.get('proxy_type') or values.get('key_type') or values.get('kind'),
            proxy=values.get('proxy') or values.get('fps') or values.get('key') or values.get('payid') or values.get('hkid'),
            destination_account=values.get('destination_account') or values.get('account') or values.get('default_account'),
        )
        return jsonify({
            'message': 'FPS proxy enrolled',
            'key': row.to_dict(),
            'FpsIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _key_status_route(service: HkrecvService, status: str, message: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        key_id = str(values.get('key_id') or '').strip()
        if not key_id:
            raise HkrecvError('missing_key', 'key_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.set_key_status(key_id=key_id, actor=userid, actor_type=actor_type, status=status)
        return jsonify({
            'message': message,
            'key': row.to_dict(),
            'FpsIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: HkrecvService):
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
        return jsonify({'preview': preview, 'FpsIn': _snapshot_for(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest(service: HkrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'FPS ingested' if created else 'FPS already ingested',
            'inbound': row.to_dict(),
            'FpsIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_ingest_file(service: HkrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        payload = values.get('file') or values.get('body') or values.get('payload')
        if not payload:
            raise HkrecvError('missing_file', 'FPS1 file body is required.')
        actor_type = session.get('usertype') or 'customer'
        rows, created = service.ingest_file(
            actor=userid, actor_type=actor_type, payload=str(payload),
            sender_name=str(values.get('name') or values.get('sender_name') or ''),
            send_bank=str(values.get('send_bank') or values.get('sender_bank') or ''),
        )
        owner = rows[-1].userid if rows and rows[-1].userid else userid
        return jsonify({
            'message': 'FPS1 file ingested',
            'created': created,
            'inbounds': [row.to_dict() for row in rows],
            'FpsIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_assign(service: HkrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise HkrecvError('missing_inbound', 'inbound_id is required.')
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
            'message': 'FPS assigned',
            'inbound': row.to_dict(),
            'FpsIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_claim(service: HkrecvService):
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
            proxy=values.get('proxy') or values.get('fps') or values.get('key') or values.get('payid') or values.get('hkid'),
            destination_account=values.get('destination_account') or values.get('account'),
            nickname=str(values.get('nickname') or ''),
            owner_userid=str(values.get('customer_id') or ''),
        )
        return jsonify({
            'message': 'FPS claimed',
            'inbound': row.to_dict(),
            'FpsIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_inbound_route(service: HkrecvService, action: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise HkrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        if action == 'override':
            row = service.override_ofac(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'release':
            row = service.release(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            message = 'FPS released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'FPS rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=str(values.get('reason') or 'cust'),
            )
            message = 'FPS returned'
        elif action == 'export':
            exported = service.export(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            owner = exported['inbound'].get('userid') or userid
            return jsonify({
                'message': 'FPS exported',
                'file': exported['file'],
                'inbound': exported['inbound'],
                'FpsIn': _snapshot_for(service, owner, userid, actor_type),
            }), 200
        else:
            raise HkrecvError('invalid_status', 'Unknown FPS action.')
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'FpsIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: HkrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise HkrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.request_return(
            inbound_id=inbound_id, actor=userid, actor_type=actor_type,
            reason=str(values.get('reason') or 'cust'),
        )
        return jsonify({
            'message': 'FPS return requested',
            'inbound': row.to_dict(),
            'FpsIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: HkrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'FpsIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def attach_hkrecv_routes(app, service: HkrecvService) -> None:
    @app.route('/listFpsIns', methods=['POST', 'GET'])
    def list_fps_ins_route():
        return handle_list(service)

    @app.route('/listFpsRecvKeys', methods=['POST', 'GET'])
    def list_fps_recv_keys_route():
        return handle_list(service)

    @app.route('/listUnmatchedFpsIns', methods=['POST', 'GET'])
    def list_unmatched_fps_ins_route():
        return handle_unmatched(service)

    @app.route('/addFpsRecvKey', methods=['POST', 'GET'])
    def add_fps_recv_key_route():
        return handle_add_key(service)

    @app.route('/pauseFpsRecvKey', methods=['POST', 'GET'])
    def pause_fps_recv_key_route():
        return _key_status_route(service, KEY_PAUSED, 'FPS proxy paused')

    @app.route('/resumeFpsRecvKey', methods=['POST', 'GET'])
    def resume_fps_recv_key_route():
        return _key_status_route(service, KEY_ACTIVE, 'FPS proxy resumed')

    @app.route('/cancelFpsRecvKey', methods=['POST', 'GET'])
    def cancel_fps_recv_key_route():
        return _key_status_route(service, KEY_CANCELLED, 'FPS proxy cancelled')

    @app.route('/previewFpsIn', methods=['POST', 'GET'])
    def preview_fps_in_route():
        return handle_preview(service)

    @app.route('/ingestFpsIn', methods=['POST', 'GET'])
    def ingest_fps_in_route():
        return handle_ingest(service)

    @app.route('/ingestFpsInFile', methods=['POST', 'GET'])
    def ingest_fps_in_file_route():
        return handle_ingest_file(service)

    @app.route('/claimFpsIn', methods=['POST', 'GET'])
    def claim_fps_in_route():
        return handle_claim(service)

    @app.route('/assignFpsIn', methods=['POST', 'GET'])
    def assign_fps_in_route():
        return handle_assign(service)

    @app.route('/overrideFpsInOfac', methods=['POST', 'GET'])
    def override_fps_in_ofac_route():
        return _staff_inbound_route(service, 'override')

    @app.route('/releaseFpsIn', methods=['POST', 'GET'])
    def release_fps_in_route():
        return _staff_inbound_route(service, 'release')

    @app.route('/rejectFpsIn', methods=['POST', 'GET'])
    def reject_fps_in_route():
        return _staff_inbound_route(service, 'reject')

    @app.route('/returnFpsIn', methods=['POST', 'GET'])
    def return_fps_in_route():
        return _staff_inbound_route(service, 'return')

    @app.route('/exportFpsIn', methods=['POST', 'GET'])
    def export_fps_in_route():
        return _staff_inbound_route(service, 'export')

    @app.route('/requestFpsInReturn', methods=['POST', 'GET'])
    def request_fps_in_return_route():
        return handle_request_return(service)

    @app.route('/runDueFpsIns', methods=['POST', 'GET'])
    def run_due_fps_ins_route():
        return handle_run_due(service)
