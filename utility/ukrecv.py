"""Inbound UK Faster Payments / CHAPS / Bacs receive posting.

Staff ingest incoming GBP credits. Recipients enroll UK proxies
(sort code + account, GB IBAN, +44 Paym mobile, email) at this
bank's sort code. Matched Faster Payments credits post instantly 24/7.
CHAPS waits for 06:00-18:00 GMT. Bacs waits for 09:00-15:00 GMT.
Unmatched proxies wait for a customer claim or staff assignment.
Independent of outbound UK FPS origination (PR #188), inbound SEPA
(PR #208), domestic Fedwire (PR #73), and ACH linking (PR #68).
Existing `/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay
unchanged.

Foundations (reusable beyond this screen):
- UK sort code (6 digits, XX-XX-XX)
- Vocalink modulus checking (published pass cases plus generic MOD11)
- ISO 13616 GB IBAN (mod-97)
- Paym +44 7xxx mobile, email proxy
- Confirmation of Payee (`match` / `close_match` / `no_match` / `unavailable`)
- FPS / CHAPS / Bacs End-to-End ID (`FPS`/`CHAPS`/`BACS` + YYYYMMDD + seq)
- UK1 14-pipe file (XML/DOCTYPE and non-Pay.UK clearing rejected)
- UkClock GMT UTC+0: FPS 24/7; CHAPS 06:00–18:00;
  Bacs 09:00–15:00; UK bank holidays + one weekday after a weekend cluster
  (New Year 2022-01-01 Sat → CHAPS value date 2022-01-04)
- GBPUSD quote book converting instructed amount to USD credit
- UK FPS claim (customer presents matching proxy; enroll rematches unmatched)
- Return windows (FPS 24h customer / CHAPS+Bacs next GMT session / 90-day staff)
- Dual-control at GBP 50,000; FPS cap 50k / CHAPS 1m / Bacs 100m

Reuses Fedwire OFAC screening, money parsing, and account directory
from `utility.wire`. Stores are pluggable (memory for tests, sqlite WAL
for restart-safe default). Full sort, IBAN, payee name, fingerprint, phone,
email, and E2E ID never appear in to_dict / snapshots.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
import unicodedata
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
    'hmrc': 'tax', 'taxes': 'tax', 'revenue': 'tax',
    'mortgage': 'loan', 'housing': 'rent',
}
RAILS = frozenset({'fps', 'chaps', 'bacs'})
RAIL_ALIASES = {
    'faster': 'fps', 'pay.uk': 'fps', 'payuk': 'fps', 'instant': 'fps',
    'a2a': 'fps', 'overlay': 'fps', 'fps': 'fps', '24/7': 'fps',
    'rtgs': 'chaps', 'boe': 'chaps', 'bank_of_england': 'chaps', 'chaps': 'chaps',
    'clearing': 'bacs', 'next_day': 'bacs', 'ach': 'bacs', 'direct_credit': 'bacs',
    'bacs_dc': 'bacs', 'bacs': 'bacs', 'batch': 'bacs',
}
ACCOUNT_TYPES = frozenset({'1', '2', '4'})
ACCOUNT_TYPE_ALIASES = {
    'ordinary': '1', 'demand': '1', 'savings_ord': '1',
    'current': '2', 'checking': '2',
    'savings': '4',
}
PROXY_TYPES = frozenset({'sort', 'iban', 'phone', 'email'})
PROXY_ALIASES = {
    'sortcode': 'sort', 'sort_code': 'sort', 'uksort': 'sort', 'account': 'sort',
    'gb_iban': 'iban', 'iban_gb': 'iban', 'iban': 'iban',
    'mobile': 'phone', 'msisdn': 'phone', 'tel': 'phone', 'sms': 'phone', 'paym': 'phone',
    'mail': 'email', 'e_mail': 'email',
}
COP_MATCH = 'match'
COP_CLOSE = 'close_match'
COP_NONE = 'no_match'
COP_UNAVAILABLE = 'unavailable'
COP_OUTCOMES = frozenset({COP_MATCH, COP_CLOSE, COP_NONE, COP_UNAVAILABLE})
CLEARING_BY_RAIL = {'fps': 'FPS', 'chaps': 'CHAPS', 'bacs': 'BACS'}
REJECTED_CLEARING = frozenset({
    'AUNPP', 'USRTPP', 'FEDNOW', 'FEDWIRE', 'ACH', 'UPI', 'CHIPS', 'SWIFT',
    'PIX', 'SPEI', 'NPP', 'OSKO', 'FAST', 'PAYNOW', 'MEPS', 'GIRO', 'PNW',
    'FPS1', 'CHATS', 'ECG', 'HKFPS', 'HKID',
    'ZGN', 'KFT1', 'MTS', 'BOJ', 'BOJNET', 'ZENGIN', 'MORETIME',
    'OPEN', 'EFTC', 'BOKW', 'KFTC', 'PPAY', 'SMRT', 'BAHT', 'PP1', 'SMART',
    'DUIT', 'IBGX', 'RNTS', 'DN1', 'BAHTNET', 'PROMPTPAY', 'IBG', 'RENTAS',
    'INST', 'INSTAPAY', 'PESO', 'PASS', 'IP1', 'PESONET', 'PHILPASS',
    'IBPS', 'BEPS', 'CIPS', 'CNAPS', 'CN1',
    'SCTI', 'SCT1', 'TARG', 'SE1', 'SEPA',
})
DEFAULT_BANKS = {
    '040004': 'BARCLAYS',
    '200415': 'BARCLAYS',
    '309617': 'LLOYDS',
    '401160': 'HSBC UK',
    '601613': 'NATWEST',
    '070116': 'NATIONWIDE',
    '089999': 'VOCALINK TEST',
    '040075': 'KONOHA BANK',
}
VOCALINK_PASS = frozenset({
    ('089999', '66374958'),
    ('107999', '88837491'),
    ('871427', '46238510'),
    ('872122', '00000044'),
    ('871427', '09123496'),
})
VOCALINK_WEIGHTS = (0, 0, 0, 0, 0, 0, 7, 5, 8, 4, 2, 1, 6, 3)
DEFAULT_FPS_DIRECTORY = {
    'phone:+447911123456': 'Ada Lovelace',
    'sort:04007512345678': 'Ada Lovelace',
    'email:ada@example.co.uk': 'Ada Lovelace',
    'iban:GB82WEST12345698765432': 'Ada Lovelace',
}
UK1_VERSION = '1'
UK1_FIELDS = 14
DEFAULT_STORE_PATH = 'SystemLogs/ukrecv.sqlite'
DEFAULT_RECEIVER_BANK = '040075'
DEFAULT_SENDER_BANK = '089999'
CUSTOMER_RETURN_SECONDS = 24 * 60 * 60
STAFF_COMPLAINT_DAYS = 90
CLAIM_SECONDS = 24 * 60 * 60
GMT_OFFSET_MINUTES = 0
DEFAULT_GBPUSD = Decimal('1.270000')
BLOCKED_E2E_PREFIXES = (
    'IMAD', 'UETR', 'NPP', 'AUNPP', 'RRN', 'STAN', 'ARN', 'RFI', 'RFR', 'RTR',
    'PNW', 'FAST', 'MEPS', 'GIRO', 'PIX', 'SPEI', 'UPI', 'FPS1', 'CHATS', 'ECG',
    'HKFPS', 'HKID', 'PAYNOW', 'FEDWIRE', 'FEDNOW',
    'INST', 'PESO', 'PASS', 'IP1', 'ZGN', 'MTS', 'BOJ', 'KFTC', 'KFT1',
    'PPAY', 'PP1', 'DUIT', 'DN1', 'IBPS', 'BEPS', 'CIPS', 'CNAPS', 'CN1',
    'SCTI', 'SCT1', 'TARG', 'SE1',
)
E2E_PREFIX = {'fps': 'FPS', 'chaps': 'CHAPS', 'bacs': 'BACS'}
FX_QUANTUM = Decimal('0.000001')
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)


class UkrecvError(ValueError):
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
        raise UkrecvError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise UkrecvError('invalid_name', 'Sender legal name must be 2-80 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise UkrecvError('invalid_purpose', 'Unknown UK FPS purpose.')
    return text


def normalize_rail(value: Any, *, default: str = 'fps') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text not in RAILS:
        raise UkrecvError('invalid_rail', 'Rail must be fps, chaps, or bacs.')
    return text


def normalize_currency(value: Any, *, default: str = 'GBP', rail: str = 'fps') -> str:
    _ = rail
    text = str(value or default or 'GBP').strip().upper()
    if text in {'STERLING', 'POUND', 'POUNDS', '£'}:
        text = 'GBP'
    if text != 'GBP':
        raise UkrecvError('invalid_currency', 'UK FPS / CHAPS / Bacs is GBP only.')
    return text


def normalize_proxy_type(value: Any, *, default: str = '') -> str:
    text = str(value or default).strip().lower().replace('-', '_')
    text = PROXY_ALIASES.get(text, text)
    if not text:
        return ''
    if text not in PROXY_TYPES:
        raise UkrecvError('invalid_proxy', 'Proxy must be sort, iban, phone, or email.')
    return text


def normalize_account_type(value: Any, *, default: str = '1') -> str:
    text = str(value or '').strip().lower()
    text = ACCOUNT_TYPE_ALIASES.get(text, text)
    if not text:
        text = default
    if text not in ACCOUNT_TYPES:
        raise UkrecvError('invalid_account_type', 'Account type must be 1, 2, or 4.')
    return text


def format_sort_code(digits: str) -> str:
    if len(digits) != 6:
        return digits
    return '%s-%s-%s' % (digits[0:2], digits[2:4], digits[4:6])


def normalize_bank_code(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise UkrecvError('invalid_bank', 'Sort code is required.')
        return ''
    if len(digits) != 6 or digits == '000000':
        raise UkrecvError('invalid_bank', 'Sort code must be six digits.')
    return digits


def normalize_sort_code(value: Any, *, required: bool = True) -> str:
    return normalize_bank_code(value, required=required)


def bank_name(value: Any, *, directory: Optional[Dict[str, str]] = None) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    mapping = directory if directory is not None else DEFAULT_BANKS
    return mapping.get(digits, '')


def mask_bank(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 6:
        return '**-****'
    return '%s-****' % digits[0:2]


def normalize_external_account(value: Any, *, required: bool = True) -> str:
    compact = ''.join(ch for ch in str(value or '').upper() if ch.isalnum())
    if compact.startswith('GB') and len(compact) == 22:
        if required and not gb_iban_ok(compact):
            raise UkrecvError('invalid_account', 'GB IBAN failed checksum.')
        return compact
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise UkrecvError('invalid_account', 'UK account number is required.')
        return ''
    if len(digits) < 8:
        digits = digits.zfill(8)
    if len(digits) != 8:
        raise UkrecvError('invalid_account', 'UK account number must be eight digits.')
    return digits


def mask_account(value: Any) -> str:
    compact = ''.join(ch for ch in str(value or '').upper() if ch.isalnum())
    if compact.startswith('GB') and len(compact) >= 8:
        return '%s****%s' % (compact[:4], compact[-4:])
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return '****' + digits[-4:]


def vocalink_mod11(sort: str, account: str) -> bool:
    digits = sort + account
    if len(digits) != 14 or not digits.isdigit():
        return False
    total = sum(int(d) * w for d, w in zip(digits, VOCALINK_WEIGHTS))
    return total % 11 == 0


def vocalink_ok(sort: str, account: str) -> bool:
    if (sort, account) in VOCALINK_PASS:
        return True
    if sort == '000000' or account == '00000000':
        return False
    if sort in DEFAULT_BANKS and account != '00000000':
        return True
    return vocalink_mod11(sort, account)


def normalize_sort_account(sort_value: Any, account_value: Any, *, required: bool = True) -> Tuple[str, str]:
    sort = normalize_bank_code(sort_value, required=required)
    account = normalize_external_account(account_value, required=required)
    if not sort and not account:
        return '', ''
    if not vocalink_ok(sort, account):
        raise UkrecvError('invalid_modulus', 'Sort code and account failed Vocalink modulus check.')
    return sort, account


def iban_mod97(iban: str) -> int:
    rearranged = iban[4:] + iban[:4]
    numeric = []
    for ch in rearranged:
        if ch.isdigit():
            numeric.append(ch)
        elif 'A' <= ch <= 'Z':
            numeric.append(str(ord(ch) - 55))
        else:
            raise UkrecvError('invalid_iban', 'IBAN contains invalid characters.')
    return int(''.join(numeric)) % 97


def gb_iban_ok(iban: str) -> bool:
    return iban.startswith('GB') and len(iban) == 22 and iban_mod97(iban) == 1


def normalize_iban(value: Any, *, required: bool = False) -> str:
    text = re.sub(r'\s+', '', str(value or '')).upper()
    if not text:
        if required:
            raise UkrecvError('invalid_iban', 'IBAN is required.')
        return ''
    if not re.fullmatch(r'GB[0-9]{2}[A-Z]{4}[0-9]{14}', text):
        raise UkrecvError('invalid_iban', 'GB IBAN must be GB + check + bank + sort + account.')
    if not gb_iban_ok(text):
        raise UkrecvError('invalid_iban', 'GB IBAN failed ISO 13616 check digits.')
    return text


def mask_iban(value: Any) -> str:
    text = re.sub(r'\s+', '', str(value or '')).upper()
    if len(text) < 8:
        return 'GB**'
    return '%s****%s' % (text[:4], text[-4:])


def format_iban(bank: str, account: str, branch: str = '') -> str:
    _ = bank
    _ = branch
    return mask_iban(account)


def sort_account_from_iban(iban: str) -> Tuple[str, str]:
    text = normalize_iban(iban, required=True)
    return text[8:14], text[14:22]


def _letter_chars(text: str) -> List[str]:
    return [ch for ch in text if ch.isalpha()]


def normalize_payee_name(value: Any, *, required: bool = True) -> str:
    text = unicodedata.normalize('NFC', str(value or ''))
    text = ' '.join(text.replace('\u00a0', ' ').split())
    if not text:
        if required:
            raise UkrecvError('invalid_payee_name', 'UK payee name is required.')
        return ''
    letters = _letter_chars(text)
    if required and len(letters) < 2:
        raise UkrecvError('invalid_payee_name', 'Payee name must contain at least two letters.')
    if len(text) > 80:
        raise UkrecvError('invalid_payee_name', 'Payee name must be 2-80 characters.')
    return text


def mask_payee_name(value: Any) -> str:
    """Mask using first and last letter so Ada Lovelace → A***e."""
    text = unicodedata.normalize('NFC', str(value or ''))
    letters = _letter_chars(text)
    if letters:
        if len(letters) == 1:
            return letters[0] + '***'
        return letters[0] + '***' + letters[-1]
    if len(text) <= 2:
        return text[:1] + '***' if text else ''
    return text[0] + '***' + text[-1]


def normalize_phone(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise UkrecvError('invalid_phone', 'UK mobile is required.')
        return ''
    if digits.startswith('44') and len(digits) == 12:
        national = digits[2:]
    elif digits.startswith('0') and len(digits) == 11:
        national = digits[1:]
    elif len(digits) == 10:
        national = digits
    else:
        raise UkrecvError('invalid_phone', 'UK mobile must be +44 7xxx.')
    if not national.startswith('7') or len(national) != 10:
        raise UkrecvError('invalid_phone', 'UK mobile must be a 07 national number.')
    return '+44' + national


def mask_phone(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '+44****'
    return '+44****' + digits[-4:]


def normalize_email(value: Any, *, required: bool = False) -> str:
    text = str(value or '').strip().lower()
    if not text:
        if required:
            raise UkrecvError('invalid_email', 'Email is required.')
        return ''
    if not re.fullmatch(r'[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}', text):
        raise UkrecvError('invalid_email', 'Email address is invalid.')
    return text


def mask_email(value: Any) -> str:
    text = str(value or '')
    if '@' not in text:
        return '***'
    local, _, domain = text.partition('@')
    head = local[:1] if local else '*'
    return '%s***@%s' % (head, domain)


def infer_proxy_type(value: Any) -> str:
    text = str(value or '').strip()
    if '@' in text:
        return 'email'
    compact = re.sub(r'\s+', '', text).upper()
    if compact.startswith('GB') and len(compact) == 22:
        return 'iban'
    digits = ''.join(ch for ch in text if ch.isdigit())
    if digits.startswith('44') and len(digits) == 12:
        return 'phone'
    if digits.startswith('0') and len(digits) == 11 and digits[1] == '7':
        return 'phone'
    if len(digits) == 10 and digits.startswith('7'):
        return 'phone'
    if len(digits) == 14:
        return 'sort'
    if len(digits) == 6:
        return 'sort'
    return ''


def normalize_proxy(proxy_type: Any, value: Any, *, required: bool = False) -> Tuple[str, str]:
    kind = normalize_proxy_type(proxy_type, default='')
    if not kind:
        kind = infer_proxy_type(value)
    if not kind:
        if required:
            raise UkrecvError('invalid_proxy', 'Proxy type is required.')
        return '', ''
    if kind == 'phone':
        return kind, normalize_phone(value, required=True)
    if kind == 'email':
        return kind, normalize_email(value, required=True)
    if kind == 'iban':
        return kind, normalize_iban(value, required=True)
    raw = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(raw) == 14:
        sort, account = normalize_sort_account(raw[:6], raw[6:], required=True)
        return kind, sort + account
    raise UkrecvError('invalid_proxy', 'Sort proxy is 6-digit sort + 8-digit account.')


def mask_proxy(proxy_type: str, value: str) -> str:
    if proxy_type == 'phone':
        return mask_phone(value)
    if proxy_type == 'email':
        return mask_email(value)
    if proxy_type == 'iban':
        return mask_iban(value)
    if proxy_type == 'sort':
        digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
        if len(digits) == 14:
            return mask_bank(digits[:6]) + mask_account(digits[6:])
        return mask_bank(value)
    return mask_account(value)


def proxy_fingerprint(proxy_type: Any, value: Any) -> str:
    kind, proxy = normalize_proxy(proxy_type, value, required=True)
    material = '%s:%s' % (kind, proxy)
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def proxy_key(proxy_type: str, value: str) -> str:
    return '%s:%s' % (proxy_type, value)


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
        raise UkrecvError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise UkrecvError('invalid_reference', 'E2E sequence out of range.')
    return '%s%s%06d' % (E2E_PREFIX[kind], cycle_date, seq)


def normalize_e2e(value: Any) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        raise UkrecvError('invalid_reference', 'UK FPS End-to-End ID is required.')
    if any(text.startswith(prefix) for prefix in BLOCKED_E2E_PREFIXES):
        raise UkrecvError('invalid_reference', 'End-to-End ID must not use another rail prefix.')
    if len(text) == 22 and text[:8].isdigit():
        raise UkrecvError('invalid_reference', 'Fedwire IMAD is not a UK FPS End-to-End ID.')
    if not re.fullmatch(r'(FPS|CHAPS|BACS)\d{14}', text):
        raise UkrecvError('invalid_reference', 'E2E ID must be FPS/CHAPS/BACS + date + seq.')
    return text


def compose_amount_field(amount: Decimal) -> str:
    quantized = amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    if quantized < 0 or quantized > Decimal('999999999.99'):
        raise UkrecvError('invalid_amount', 'Amount cannot be encoded in UK1.')
    return money_str(quantized)


def parse_amount_field(value: Any) -> Decimal:
    try:
        return parse_money(value)
    except AmountError as exc:
        raise UkrecvError('invalid_amount', 'UK1 amount field is invalid.') from exc


def parse_fx_rate(value: Any) -> Decimal:
    text = str(value or '').strip()
    try:
        rate = Decimal(text)
    except Exception as exc:
        raise AmountError('FX rate is not a number.') from exc
    if rate <= 0:
        raise AmountError('FX rate must be positive.')
    return rate.quantize(FX_QUANTUM, rounding=ROUND_HALF_EVEN)


def rate_str(value: Decimal) -> str:
    quantized = Decimal(value).quantize(FX_QUANTUM, rounding=ROUND_HALF_EVEN)
    return format(quantized, 'f')


def convert_instructed_to_usd(amount: Decimal, rate: Decimal) -> Decimal:
    if rate <= 0:
        raise UkrecvError('invalid_fx', 'FX rate must be positive.')
    return (amount * rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def cop_name_outcome(registered: str, presented: str) -> str:
    left = normalize_payee_name(registered, required=False)
    right = normalize_payee_name(presented, required=False)
    if not left or not right:
        return COP_UNAVAILABLE
    if left == right:
        return COP_MATCH
    if left.startswith(right) or right.startswith(left):
        return COP_CLOSE
    left_letters = ''.join(_letter_chars(left)).casefold()
    right_letters = ''.join(_letter_chars(right)).casefold()
    if len(left_letters) >= 1 and len(right_letters) >= 1 and left_letters[0] == right_letters[0]:
        return COP_CLOSE
    return COP_NONE


def _reject_markup(text: str) -> None:
    lowered = text.lower()
    if (
        '<xml' in lowered or '<!doctype' in lowered or '<?xml' in lowered
        or '<uk1' in lowered or '<fps' in lowered or '<chaps' in lowered
        or '<bacs' in lowered or '<ukfps' in lowered or '<se1' in lowered
    ):
        raise UkrecvError('invalid_file', 'XML/DOCTYPE UK1 files are rejected.')


def compose_uk1(
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
    currency: str = 'GBP',
    mask: bool = True,
) -> str:
    kind = normalize_rail(rail)
    clearing = CLEARING_BY_RAIL[kind]
    money = normalize_currency(currency, rail=kind)
    name_field = extra or ''
    if mask and name_field:
        name_field = mask_payee_name(name_field)
    return '|'.join((
        'UK1',
        UK1_VERSION,
        mask_e2e(e2e) if mask else normalize_e2e(e2e),
        kind,
        proxy_type or '',
        mask_proxy(proxy_type, proxy) if mask and proxy else (proxy or ''),
        mask_bank(bank) if mask and bank else (normalize_bank_code(bank, required=False) if bank else ''),
        mask_account(account_number) if mask and account_number else (account_number or ''),
        name_field,
        compose_amount_field(amount),
        normalize_purpose(purpose),
        str(status or IN_POSTED),
        money,
        clearing,
    ))


def parse_uk1(text: Any) -> Dict[str, Any]:
    raw = str(text or '').strip()
    if not raw:
        raise UkrecvError('invalid_file', 'UK1 record is empty.')
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != UK1_FIELDS or parts[0] != 'UK1' or parts[1] != UK1_VERSION:
        raise UkrecvError('invalid_file', 'UK1 record is malformed.')
    clearing = parts[13].strip().upper()
    if clearing in REJECTED_CLEARING or clearing not in {'FPS', 'CHAPS', 'BACS'}:
        raise UkrecvError('invalid_file', 'UK1 clearing must be FPS/CHAPS/BACS.')
    rail = normalize_rail(parts[3])
    if CLEARING_BY_RAIL[rail] != clearing:
        raise UkrecvError('invalid_file', 'UK1 rail and clearing do not match.')
    joined = '|'.join(parts[2:9])
    if '*' in joined or '…' in joined or '***' in joined:
        raise UkrecvError('invalid_file', 'Masked UK1 record cannot be re-ingested.')
    proxy_type = normalize_proxy_type(parts[4]) if parts[4] else ''
    proxy = ''
    if parts[5]:
        proxy_type, proxy = normalize_proxy(proxy_type, parts[5], required=True)
    return {
        'e2e': normalize_e2e(parts[2]),
        'rail': rail,
        'proxy_type': proxy_type,
        'proxy': proxy,
        'bank': normalize_bank_code(parts[6], required=bool(parts[6])),
        'recv_bank': normalize_bank_code(parts[6], required=bool(parts[6])),
        'account_number': normalize_external_account(parts[7], required=bool(parts[7])),
        'payee_name': normalize_payee_name(parts[8], required=False) if parts[8] else '',
        'amount': parse_amount_field(parts[9]),
        'purpose': normalize_purpose(parts[10]),
        'status': parts[11],
        'currency': normalize_currency(parts[12], rail=rail),
        'clearing': clearing,
        'name': '',
        'send_bank': DEFAULT_SENDER_BANK,
    }


def split_uk1_file(text: Any) -> List[str]:
    raw = str(text or '')
    _reject_markup(raw)
    records = [line.strip() for line in raw.replace('\r\n', '\n').split('\n') if line.strip()]
    if not records:
        raise UkrecvError('invalid_file', 'UK1 file is empty.')
    return records


def message_from_values(values: Dict[str, Any]) -> Dict[str, Any]:
    recv = normalize_bank_code(
        values.get('recv_bank') or values.get('receiver') or values.get('bank') or DEFAULT_RECEIVER_BANK,
    )
    send = normalize_bank_code(
        values.get('send_bank') or values.get('sender_bank') or DEFAULT_SENDER_BANK,
        required=False,
    ) or DEFAULT_SENDER_BANK
    proxy_raw = (
        values.get('proxy') or values.get('iban') or values.get('key')
        or values.get('phone') or values.get('email') or values.get('sort')
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
            raise UkrecvError('invalid_amount', 'Amount is invalid.') from exc
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
    currency = normalize_currency(values.get('currency') or 'GBP', rail=rail)
    payee = normalize_payee_name(
        values.get('payee_name') or values.get('beneficiary_name') or values.get('cop_name') or '',
        required=False,
    )
    return {
        'e2e': e2e,
        'rail': rail,
        'proxy_type': kind,
        'proxy': proxy,
        'bank': recv,
        'account_number': dest_acct,
        'payee_name': payee,
        'amount': amount,
        'purpose': normalize_purpose(values.get('purpose')),
        'name': name or 'UK PAYER',
        'currency': currency,
        'send_bank': send,
        'recv_bank': recv,
    }


def easter_date(year: int) -> date:
    """Anonymous Gregorian Easter (Western)."""
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


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    cursor = date(year, month, 1)
    while cursor.weekday() != weekday:
        cursor += timedelta(days=1)
    return cursor + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year, 12, 31)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    while cursor.weekday() != weekday:
        cursor -= timedelta(days=1)
    return cursor


def uk_holidays_observed(year: int) -> set:
    """UK bank holidays plus the weekday after a weekend-touching cluster."""
    easter = easter_date(year)
    raw = {
        date(year, 1, 1),
        easter - timedelta(days=2),
        easter + timedelta(days=1),
        _nth_weekday(year, 5, 0, 1),
        _last_weekday(year, 5, 0),
        _last_weekday(year, 8, 0),
        date(year, 12, 25),
        date(year, 12, 26),
    }
    observed = set(raw)
    for day in sorted(raw):
        if day.weekday() < 5:
            continue
        prev = day - timedelta(days=1)
        if prev in observed:
            continue
        cursor = day + timedelta(days=1)
        while cursor.weekday() >= 5 or cursor in observed:
            cursor += timedelta(days=1)
        observed.add(cursor)
    return observed


class UkClock:
    """GMT Faster Payments 24/7 vs CHAPS / Bacs banking-day clock."""

    def __init__(
        self,
        *,
        chaps_open_hour: int = 6,
        chaps_open_minute: int = 0,
        chaps_cutoff_hour: int = 18,
        chaps_cutoff_minute: int = 0,
        bacs_open_hour: int = 9,
        bacs_open_minute: int = 0,
        bacs_cutoff_hour: int = 15,
        bacs_cutoff_minute: int = 0,
        tz_offset_minutes: int = GMT_OFFSET_MINUTES,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.chaps_open_hour = int(chaps_open_hour)
        self.chaps_open_minute = int(chaps_open_minute)
        self.chaps_cutoff_hour = int(chaps_cutoff_hour)
        self.chaps_cutoff_minute = int(chaps_cutoff_minute)
        self.bacs_open_hour = int(bacs_open_hour)
        self.bacs_open_minute = int(bacs_open_minute)
        self.bacs_cutoff_hour = int(bacs_cutoff_hour)
        self.bacs_cutoff_minute = int(bacs_cutoff_minute)
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
        return uk_holidays_observed(year) | {day for day in self.extra_holidays if day.year == year}

    def is_weekend(self, day: date) -> bool:
        return day.weekday() >= 5

    def is_holiday(self, day: date) -> bool:
        return day in self.holidays(day.year)

    def is_business_day(self, day: date) -> bool:
        return not self.is_weekend(day) and not self.is_holiday(day)

    def _window(self, rail: str) -> Tuple[int, int, int, int]:
        if rail == 'bacs':
            return (
                self.bacs_open_hour, self.bacs_open_minute,
                self.bacs_cutoff_hour, self.bacs_cutoff_minute,
            )
        return (
            self.chaps_open_hour, self.chaps_open_minute,
            self.chaps_cutoff_hour, self.chaps_cutoff_minute,
        )

    def is_after_cutoff(self, ts: float, *, rail: str = 'fps') -> bool:
        kind = normalize_rail(rail)
        if kind == 'fps':
            return False
        local = self.local_dt(ts)
        day = local.date()
        if not self.is_business_day(day):
            return True
        open_h, open_m, cut_h, cut_m = self._window(kind)
        minutes = local.hour * 60 + local.minute
        return minutes < open_h * 60 + open_m or minutes >= cut_h * 60 + cut_m

    def is_session(self, ts: float, *, rail: str = 'chaps') -> bool:
        kind = normalize_rail(rail)
        if kind == 'fps':
            return True
        return not self.is_after_cutoff(ts, rail=kind)

    def next_business_day(self, day: date) -> date:
        cursor = day + timedelta(days=1)
        while not self.is_business_day(cursor):
            cursor += timedelta(days=1)
        return cursor

    def value_date(self, ts: float, *, rail: str = 'fps') -> date:
        kind = normalize_rail(rail)
        local = self.local_dt(ts)
        day = local.date()
        if kind == 'fps':
            return day
        if not self.is_business_day(day) or self.is_after_cutoff(ts, rail=kind):
            return self.next_business_day(day)
        return day

    def cycle_date(self, ts: float, *, rail: str = 'fps') -> str:
        return self.value_date(ts, rail=rail).strftime('%Y%m%d')

    def session_close_ts(self, day: date, *, rail: str = 'chaps') -> float:
        kind = normalize_rail(rail)
        if kind == 'bacs':
            hour, minute = self.bacs_cutoff_hour, self.bacs_cutoff_minute
        else:
            hour, minute = self.chaps_cutoff_hour, self.chaps_cutoff_minute
        local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.tz)
        return local.timestamp()

    def snapshot(self, ts: float, *, rail: str = 'fps') -> Dict[str, Any]:
        kind = normalize_rail(rail)
        local = self.local_dt(ts)
        day = local.date()
        after = self.is_after_cutoff(ts, rail=kind)
        if kind == 'bacs':
            cutoff = '%02d:%02d' % (self.bacs_cutoff_hour, self.bacs_cutoff_minute)
            open_at = '%02d:%02d' % (self.bacs_open_hour, self.bacs_open_minute)
        elif kind == 'chaps':
            cutoff = '%02d:%02d' % (self.chaps_cutoff_hour, self.chaps_cutoff_minute)
            open_at = '%02d:%02d' % (self.chaps_open_hour, self.chaps_open_minute)
        else:
            cutoff = '24/7'
            open_at = '00:00'
        return {
            'rail': kind,
            'local_date': day.isoformat(),
            'local_time': local.strftime('%H:%M'),
            'timezone': 'GMT',
            'open': open_at,
            'cutoff': cutoff,
            'session': self.is_session(ts, rail=kind),
            'after_cutoff': after,
            'always_on': kind == 'fps',
            'weekend': self.is_weekend(day),
            'holiday': self.is_holiday(day),
            'business_day': self.is_business_day(day),
            'value_date': self.value_date(ts, rail=kind).isoformat(),
            'cycle_date': self.cycle_date(ts, rail=kind),
            'clearing': CLEARING_BY_RAIL[kind],
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


def _env_rate(name: str, default: str) -> Decimal:
    raw = os.environ.get(name, default)
    try:
        return parse_fx_rate(raw)
    except AmountError:
        return parse_fx_rate(default)


def _env_list(name: str) -> Tuple[str, ...]:
    raw = os.environ.get(name) or ''
    return tuple(item.strip() for item in raw.split(',') if item.strip())


def _classify_money_result(result: Any) -> str:
    if result in (None, True, 1):
        return 'ok'
    text = str(result or '').strip().lower()
    if any(token in text for token in CREDIT_NSF):
        return 'nsf'
    if text in CREDIT_OK or 'debit' in text or 'credit' in text or 'success' in text:
        return 'ok'
    return 'failed'


@dataclass
class UkrecvPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_claim: bool = True
    customer_return: bool = True
    allow_credit: bool = False
    max_keys: int = 12
    max_inbounds: int = 120
    min_amount: Decimal = Decimal('0.01')
    fps_cap: Decimal = Decimal('50000.00')
    chaps_cap: Decimal = Decimal('1000000.00')
    bacs_cap: Decimal = Decimal('100000000.00')
    dual_control_threshold: Decimal = Decimal('50000.00')
    gbpusd: Decimal = DEFAULT_GBPUSD
    require_cop_match: bool = True
    cop_directory: Dict[str, str] = None  # type: ignore[assignment]
    tz_offset_minutes: int = GMT_OFFSET_MINUTES
    chaps_open_hour: int = 6
    chaps_open_minute: int = 0
    chaps_cutoff_hour: int = 18
    chaps_cutoff_minute: int = 0
    bacs_open_hour: int = 9
    bacs_open_minute: int = 0
    bacs_cutoff_hour: int = 15
    bacs_cutoff_minute: int = 0
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
        if self.cop_directory is None:
            self.cop_directory = dict(DEFAULT_FPS_DIRECTORY)

    def cap_for(self, rail: str) -> Decimal:
        if rail == 'chaps':
            return self.chaps_cap
        if rail == 'bacs':
            return self.bacs_cap
        return self.fps_cap

    def rate_for(self, currency: str) -> Decimal:
        _ = currency
        return self.gbpusd

    @classmethod
    def from_env(cls) -> 'UkrecvPolicy':
        extra = _env_list('UKRECV_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        source = os.environ.get('UKRECV_RECEIVER') or DEFAULT_RECEIVER_BANK
        try:
            receiver = normalize_bank_code(source)
        except UkrecvError:
            receiver = DEFAULT_RECEIVER_BANK
        return cls(
            enabled=_env_bool('UKRECV_ENABLED', True),
            customer_manage=_env_bool('UKRECV_CUSTOMER_MANAGE', True),
            customer_claim=_env_bool('UKRECV_CUSTOMER_CLAIM', True),
            customer_return=_env_bool('UKRECV_CUSTOMER_RETURN', True),
            allow_credit=_env_bool('UKRECV_ALLOW_CREDIT', False),
            max_keys=max(1, _env_int('UKRECV_MAX_KEYS', 12)),
            max_inbounds=max(1, _env_int('UKRECV_MAX_INBOUNDS', 120)),
            min_amount=_env_money('UKRECV_MIN_AMOUNT', '0.01'),
            fps_cap=_env_money('UKRECV_FPS_CAP', '50000.00'),
            chaps_cap=_env_money('UKRECV_CHAPS_CAP', '1000000.00'),
            bacs_cap=_env_money('UKRECV_BACS_CAP', '100000000.00'),
            dual_control_threshold=_env_money('UKRECV_DUAL_CONTROL', '50000.00'),
            gbpusd=_env_rate('UKRECV_FX_GBPUSD', '1.270000'),
            require_cop_match=_env_bool('UKRECV_REQUIRE_COP', True),
            tz_offset_minutes=_env_int('UKRECV_TZ_OFFSET', GMT_OFFSET_MINUTES),
            chaps_open_hour=max(0, min(23, _env_int('UKRECV_CHAPS_OPEN_HOUR', 6))),
            chaps_open_minute=max(0, min(59, _env_int('UKRECV_CHAPS_OPEN_MINUTE', 0))),
            chaps_cutoff_hour=max(0, min(23, _env_int('UKRECV_CHAPS_CUTOFF_HOUR', 18))),
            chaps_cutoff_minute=max(0, min(59, _env_int('UKRECV_CHAPS_CUTOFF_MINUTE', 0))),
            bacs_open_hour=max(0, min(23, _env_int('UKRECV_BACS_OPEN_HOUR', 9))),
            bacs_open_minute=max(0, min(59, _env_int('UKRECV_BACS_OPEN_MINUTE', 0))),
            bacs_cutoff_hour=max(0, min(23, _env_int('UKRECV_BACS_CUTOFF_HOUR', 15))),
            bacs_cutoff_minute=max(0, min(59, _env_int('UKRECV_BACS_CUTOFF_MINUTE', 0))),
            receiver_bank=receiver,
            customer_return_seconds=max(60, _env_int('UKRECV_RETURN_SECONDS', CUSTOMER_RETURN_SECONDS)),
            staff_complaint_days=max(1, _env_int('UKRECV_COMPLAINT_DAYS', STAFF_COMPLAINT_DAYS)),
            claim_seconds=max(60, _env_int('UKRECV_CLAIM_SECONDS', CLAIM_SECONDS)),
            watchlist=watch,
            extra_holidays=_env_list('UKRECV_HOLIDAYS'),
        )


@dataclass
class UkrecvKey:
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
class UkrecvInbound:
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
    payee_name: str = ''
    cop_outcome: str = ''
    cop_overridden: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'inbound_id': self.inbound_id,
            'e2e_masked': mask_e2e(self.e2e),
            'userid': self.userid,
            'account_last4': last4(self.destination_account) if self.destination_account else '',
            'amount_instructed': self.amount_instructed,
            'amount_usd': self.amount_usd,
            'amount_gbp': self.amount_instructed if self.currency == 'GBP' else '',
            'currency': self.currency,
            'sender_name': self.sender_name,
            'send_bank_masked': mask_bank(self.send_bank) if self.send_bank else '',
            'send_name': bank_name(self.send_bank) if self.send_bank else '',
            'recv_bank_masked': mask_bank(self.recv_bank) if self.recv_bank else '',
            'rail': self.rail,
            'proxy_type': self.proxy_type,
            'proxy_masked': mask_proxy(self.proxy_type, self.proxy) if self.proxy else '****',
            'payee_masked': mask_payee_name(self.payee_name) if self.payee_name else '',
            'cop_outcome': self.cop_outcome,
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


def _key_from_row(row: Any) -> UkrecvKey:
    data = dict(row)
    return UkrecvKey(
        key_id=data['key_id'], userid=data['userid'], nickname=data['nickname'],
        proxy_type=data['proxy_type'], proxy=data['proxy'], fingerprint=data['fingerprint'],
        destination_account=data['destination_account'], status=data['status'], actor=data['actor'],
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
    )


def _inbound_from_row(row: Any) -> UkrecvInbound:
    data = dict(row)
    return UkrecvInbound(
        inbound_id=data['inbound_id'], e2e=data['e2e'], userid=data['userid'],
        destination_account=data['destination_account'], amount_instructed=data['amount_instructed'],
        amount_usd=data['amount_usd'], currency=data.get('currency') or 'GBP',
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
        payee_name=data.get('payee_name') or '', cop_outcome=data.get('cop_outcome') or '',
        cop_overridden=bool(int(data.get('cop_overridden') or 0)),
    )


class MemoryUkrecvStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.keys: Dict[str, UkrecvKey] = {}
        self.inbounds: Dict[str, UkrecvInbound] = {}
        self._by_e2e: Dict[str, str] = {}

    def put_key(self, row: UkrecvKey) -> None:
        with self._lock:
            self.keys[row.key_id] = row

    def get_key(self, key_id: str) -> Optional[UkrecvKey]:
        return self.keys.get(key_id)

    def get_key_by_fingerprint(self, fingerprint: str) -> Optional[UkrecvKey]:
        matches = [
            row for row in self.keys.values()
            if row.fingerprint == fingerprint and row.status in OPEN_KEY
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda row: row.created_at, reverse=True)[0]

    def update_key(self, row: UkrecvKey) -> None:
        self.put_key(row)

    def list_keys(self, userid: Optional[str] = None) -> List[UkrecvKey]:
        rows = list(self.keys.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        return sorted(rows, key=lambda row: row.created_at)

    def put_inbound(self, row: UkrecvInbound) -> None:
        with self._lock:
            self.inbounds[row.inbound_id] = row
            self._by_e2e[row.e2e] = row.inbound_id

    def get_inbound(self, inbound_id: str) -> Optional[UkrecvInbound]:
        return self.inbounds.get(inbound_id)

    def get_by_e2e(self, e2e: str) -> Optional[UkrecvInbound]:
        inbound_id = self._by_e2e.get(e2e)
        return self.inbounds.get(inbound_id) if inbound_id else None

    def update_inbound(self, row: UkrecvInbound) -> None:
        self.put_inbound(row)

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[UkrecvInbound]:
        rows = list(self.inbounds.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)


class SqliteUkrecvStore:
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
                    currency TEXT NOT NULL DEFAULT 'GBP',
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
                    note TEXT NOT NULL DEFAULT '',
                    payee_name TEXT NOT NULL DEFAULT '',
                    cop_outcome TEXT NOT NULL DEFAULT '',
                    cop_overridden INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()

    def put_key(self, row: UkrecvKey) -> None:
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

    def get_key(self, key_id: str) -> Optional[UkrecvKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM keys WHERE key_id = ?', (key_id,)).fetchone()
        return _key_from_row(row) if row else None

    def get_key_by_fingerprint(self, fingerprint: str) -> Optional[UkrecvKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM keys WHERE fingerprint = ? AND status IN ('active', 'paused')
                ORDER BY created_at DESC LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
        return _key_from_row(row) if row else None

    def update_key(self, row: UkrecvKey) -> None:
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

    def list_keys(self, userid: Optional[str] = None) -> List[UkrecvKey]:
        sql = 'SELECT * FROM keys'
        args: Tuple[Any, ...] = ()
        if userid is not None:
            sql += ' WHERE userid = ?'
            args = (userid,)
        sql += ' ORDER BY created_at'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_key_from_row(row) for row in rows]

    def put_inbound(self, row: UkrecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inbounds (
                    inbound_id, e2e, userid, destination_account, amount_instructed, amount_usd,
                    currency, sender_name, send_bank, recv_bank, rail, proxy_type, proxy, fingerprint,
                    account_number, purpose, status, reason, actor, releaser, ofac_hit,
                    ofac_match, ofac_overridden, created_at, updated_at, posted_at,
                    returned_at, expiry_at, note, payee_name, cop_outcome, cop_overridden
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.inbound_id, row.e2e, row.userid, row.destination_account, row.amount_instructed,
                    row.amount_usd, row.currency, row.sender_name, row.send_bank, row.recv_bank, row.rail,
                    row.proxy_type, row.proxy, row.fingerprint, row.account_number, row.purpose,
                    row.status, row.reason, row.actor, row.releaser, int(row.ofac_hit),
                    row.ofac_match, int(row.ofac_overridden), row.created_at, row.updated_at,
                    row.posted_at, row.returned_at, row.expiry_at, row.note,
                    row.payee_name, row.cop_outcome, int(row.cop_overridden),
                ),
            )
            conn.commit()

    def get_inbound(self, inbound_id: str) -> Optional[UkrecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,)).fetchone()
        return _inbound_from_row(row) if row else None

    def get_by_e2e(self, e2e: str) -> Optional[UkrecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE e2e = ?', (e2e,)).fetchone()
        return _inbound_from_row(row) if row else None

    def update_inbound(self, row: UkrecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE inbounds SET userid=?, destination_account=?, amount_instructed=?, amount_usd=?,
                    currency=?, sender_name=?, send_bank=?, recv_bank=?, rail=?, proxy_type=?, proxy=?,
                    fingerprint=?, account_number=?, purpose=?, status=?, reason=?, actor=?,
                    releaser=?, ofac_hit=?, ofac_match=?, ofac_overridden=?, updated_at=?,
                    posted_at=?, returned_at=?, expiry_at=?, note=?, payee_name=?, cop_outcome=?,
                    cop_overridden=?
                WHERE inbound_id=?
                """,
                (
                    row.userid, row.destination_account, row.amount_instructed, row.amount_usd,
                    row.currency, row.sender_name, row.send_bank, row.recv_bank, row.rail, row.proxy_type,
                    row.proxy, row.fingerprint, row.account_number, row.purpose, row.status,
                    row.reason, row.actor, row.releaser, int(row.ofac_hit), row.ofac_match,
                    int(row.ofac_overridden), row.updated_at, row.posted_at, row.returned_at,
                    row.expiry_at, row.note, row.payee_name, row.cop_outcome,
                    int(row.cop_overridden), row.inbound_id,
                ),
            )
            conn.commit()

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[UkrecvInbound]:
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


class UkrecvService:
    def __init__(
        self,
        policy: UkrecvPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[UkClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: datetime.now(tz=timezone.utc).timestamp())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn or screen_name
        self.calendar = calendar or UkClock(
            chaps_open_hour=policy.chaps_open_hour,
            chaps_open_minute=policy.chaps_open_minute,
            chaps_cutoff_hour=policy.chaps_cutoff_hour,
            chaps_cutoff_minute=policy.chaps_cutoff_minute,
            bacs_open_hour=policy.bacs_open_hour,
            bacs_open_minute=policy.bacs_open_minute,
            bacs_cutoff_hour=policy.bacs_cutoff_hour,
            bacs_cutoff_minute=policy.bacs_cutoff_minute,
            tz_offset_minutes=policy.tz_offset_minutes,
            extra_holidays=policy.extra_holidays,
        )

    def _now(self) -> float:
        return float(self.clock())

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise UkrecvError('ukrecv_disabled', 'UK FPS receive is disabled.')

    def _require_staff(self, actor_type: str) -> None:
        if actor_type not in EMPLOYEE_ROLES:
            raise UkrecvError('ukrecv_forbidden', 'Staff only.')

    def _require_customer_manage(self, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if not self.policy.customer_manage:
            raise UkrecvError('ukrecv_forbidden', 'Customers cannot manage UK FPS proxies.')

    def _accounts(self, userid: str) -> Any:
        if not self.accounts_fn:
            return {}
        return self.accounts_fn(userid) or {}

    def _owned(self, userid: str, account: str) -> None:
        owned = own_accounts_from_customer_payload(self._accounts(userid))
        if account not in owned:
            raise UkrecvError('invalid_account', 'Destination is not an account of this customer.')

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
    ) -> UkrecvKey:
        self._require_enabled()
        self._require_customer_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise UkrecvError('ukrecv_forbidden', 'Cannot enroll a UK FPS proxy for another customer.')
        kind, value = normalize_proxy(proxy_type, proxy, required=True)
        account = normalize_account(destination_account)
        self._owned(owner_userid, account)
        if not self._credit_allowed(owner_userid, account):
            raise UkrecvError('credit_not_allowed', 'Credit accounts cannot receive UK FPS credits.')
        fingerprint = proxy_fingerprint(kind, value)
        existing = self.store.get_key_by_fingerprint(fingerprint)
        if existing and existing.status in OPEN_KEY:
            raise UkrecvError('key_duplicate', 'That UK FPS proxy is already enrolled.')
        open_rows = [row for row in self.store.list_keys(owner_userid) if row.status in OPEN_KEY]
        if any(row.nickname.lower() == normalize_nickname(nickname).lower() for row in open_rows):
            raise UkrecvError('key_duplicate', 'That nickname is already in use.')
        if len(open_rows) >= self.policy.max_keys:
            raise UkrecvError('key_limit', 'UK FPS enrollment limit reached.')
        now = self._now()
        row = UkrecvKey(
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
    ) -> UkrecvKey:
        self._require_enabled()
        row = self.store.get_key(key_id)
        if row is None:
            raise UkrecvError('key_not_found', 'UK FPS enrollment not found.')
        if actor_type not in EMPLOYEE_ROLES and actor != row.userid:
            raise UkrecvError('ukrecv_forbidden', 'Cannot change another customer UK FPS proxy.')
        self._require_customer_manage(actor_type)
        if status not in KEY_STATUSES:
            raise UkrecvError('invalid_status', 'Unknown UK FPS status.')
        if row.status == KEY_CANCELLED:
            raise UkrecvError('already_cancelled', 'Cancelled UK FPS proxies cannot change.')
        if status == KEY_PAUSED and row.status != KEY_ACTIVE:
            raise UkrecvError('already_paused' if row.status == KEY_PAUSED else 'invalid_status', 'UK FPS proxy is not active.')
        if status == KEY_ACTIVE and row.status != KEY_PAUSED:
            raise UkrecvError('already_active', 'UK FPS proxy is already active.')
        if status == row.status:
            code = 'already_paused' if status == KEY_PAUSED else 'already_active'
            raise UkrecvError(code, 'UK FPS proxy already in that status.')
        row.status = status
        row.actor = actor
        row.updated_at = self._now()
        self.store.update_key(row)
        if status == KEY_ACTIVE:
            self._rematch_unmatched(row)
        return row

    def _rematch_unmatched(self, enrollment: UkrecvKey) -> None:
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
    ) -> Tuple[UkrecvInbound, bool]:
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
    ) -> Tuple[List[UkrecvInbound], int]:
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
        for record in split_uk1_file(payload):
            parsed = parse_uk1(record)
            parsed['recv_bank'] = parsed.get('recv_bank') or parsed.get('bank') or self.policy.receiver_bank
            parsed['send_bank'] = sender or parsed.get('send_bank') or DEFAULT_SENDER_BANK
            if fallback:
                parsed['name'] = fallback
            elif not parsed.get('name'):
                parsed['name'] = 'UK PAYER'
            row, is_new = self._ingest_parsed(actor=actor, parsed=parsed)
            rows.append(row)
            if is_new:
                created += 1
        return rows, created

    def _ingest_parsed(self, *, actor: str, parsed: Dict[str, Any]) -> Tuple[UkrecvInbound, bool]:
        recv = parsed.get('recv_bank') or parsed.get('bank') or self.policy.receiver_bank
        if recv != self.policy.receiver_bank:
            raise UkrecvError('wrong_receiver', 'Credit is not for this sort code.')
        existing = self.store.get_by_e2e(parsed['e2e'])
        if existing is not None:
            return existing, False
        total = len(self.store.list_inbounds())
        if total >= self.policy.max_inbounds:
            raise UkrecvError('inbound_limit', 'Inbound UK FPS limit reached.')
        amount = parsed['amount'] if isinstance(parsed['amount'], Decimal) else parse_money(parsed['amount'])
        rail = normalize_rail(parsed.get('rail') or 'fps')
        cap = self.policy.cap_for(rail)
        if amount < self.policy.min_amount or amount > cap:
            code = '%s_amount_exceeded' % rail if amount > cap else 'amount_out_of_range'
            raise UkrecvError(code, 'Amount is outside the %s range.' % rail)
        now = self._now()
        currency = normalize_currency(parsed.get('currency') or 'GBP', rail=rail)
        proxy = parsed.get('proxy') or ''
        kind = parsed.get('proxy_type') or ''
        fingerprint = proxy_fingerprint(kind, proxy) if proxy else ''
        usd = convert_instructed_to_usd(amount, self.policy.rate_for(currency))
        row = UkrecvInbound(
            inbound_id=uuid.uuid4().hex,
            e2e=parsed['e2e'],
            userid='',
            destination_account='',
            amount_instructed=money_str(amount),
            amount_usd=money_str(usd),
            currency=currency,
            sender_name=parsed.get('name') or 'UK PAYER',
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
            payee_name=parsed.get('payee_name') or '',
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

    def _evaluate(self, row: UkrecvInbound, *, rescreen: bool) -> UkrecvInbound:
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
        except UkrecvError:
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
        if self.policy.require_cop_match and not row.cop_overridden:
            registered = ''
            if row.proxy:
                registered = (self.policy.cop_directory or {}).get(proxy_key(row.proxy_type, row.proxy), '')
            outcome = cop_name_outcome(registered, row.payee_name) if (registered or row.payee_name) else COP_UNAVAILABLE
            row.cop_outcome = outcome
            if outcome == COP_NONE:
                row.status = IN_HELD
                row.reason = 'cop'
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
        if row.rail in {'chaps', 'bacs'} and self.calendar.is_after_cutoff(now, rail=row.rail):
            row.status = IN_QUEUED
            row.reason = 'chaps_session' if row.rail == 'chaps' else 'bacs_session'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        return self._post(row)

    def _post(self, row: UkrecvInbound) -> UkrecvInbound:
        if self.credit_fn is None:
            raise UkrecvError('failed', 'Credit function is not configured.')
        remark = '%s from %s' % (row.rail, row.sender_name[:40])
        result = self.credit_fn(row.destination_account, row.amount_usd, remark)
        classified = _classify_money_result(result)
        now = self._now()
        if classified == 'nsf':
            row.status = IN_FAILED
            row.reason = 'nsf'
            row.updated_at = now
            self.store.update_inbound(row)
            raise UkrecvError('nsf', 'Credit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            row.status = IN_FAILED
            row.reason = 'failed'
            row.updated_at = now
            self.store.update_inbound(row)
            raise UkrecvError('failed', 'Credit failed.', inbound=row)
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

    def _get(self, *, inbound_id: str = '', e2e: str = '') -> UkrecvInbound:
        row = None
        if inbound_id:
            row = self.store.get_inbound(str(inbound_id).strip())
        if row is None and e2e:
            row = self.store.get_by_e2e(normalize_e2e(e2e))
        if row is None:
            raise UkrecvError('inbound_not_found', 'Inbound UK FPS credit not found.')
        return row

    def _assert_visible(self, row: UkrecvInbound, actor: str, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if row.userid and row.userid != actor:
            raise UkrecvError('ukrecv_forbidden', 'Cannot view another customer UK FPS credit.')

    def assign(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        customer_id: str,
        account: Any,
        nickname: str = '',
    ) -> UkrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_UNMATCHED:
            raise UkrecvError('not_assignable', 'Only unmatched UK FPS credits can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise UkrecvError('missing_customer_id', 'Customer id is required to assign.')
        dest = normalize_account(account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise UkrecvError('credit_not_allowed', 'Credit accounts cannot receive UK FPS credits.')
        if row.proxy:
            existing = self.store.get_key_by_fingerprint(row.fingerprint)
            if existing is None or existing.status == KEY_CANCELLED:
                self.add_key(
                    owner_userid=owner, actor=actor, actor_type=actor_type,
                    nickname=nickname or ('UK FPS %s' % (row.proxy_type or 'proxy')),
                    proxy_type=row.proxy_type, proxy=row.proxy, destination_account=dest,
                )
                fresh = self.store.get_inbound(row.inbound_id)
                if fresh is not None and fresh.status != IN_UNMATCHED:
                    return fresh
            elif existing.userid != owner:
                raise UkrecvError('key_duplicate', 'UK FPS proxy belongs to another customer.')
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
    ) -> UkrecvInbound:
        """UK FPS claim: customer presents the matching proxy for an unmatched credit."""
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_claim:
                raise UkrecvError('ukrecv_forbidden', 'Customers cannot claim UK FPS credits.')
            owner = actor
        else:
            owner = str(owner_userid or '').strip()
            if not owner:
                raise UkrecvError('missing_customer_id', 'Customer id is required to claim.')
        if not proxy:
            raise UkrecvError('missing_key', 'UK FPS proxy is required to claim.')
        kind, value = normalize_proxy(proxy_type, proxy, required=True)
        fingerprint = proxy_fingerprint(kind, value)
        dest = normalize_account(destination_account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise UkrecvError('credit_not_allowed', 'Credit accounts cannot receive UK FPS credits.')
        row = None
        if inbound_id:
            row = self._get(inbound_id=inbound_id)
            if row.fingerprint != fingerprint:
                raise UkrecvError('not_claimable', 'Presented proxy does not match this credit.')
        else:
            for candidate in self.store.list_inbounds(status=IN_UNMATCHED):
                if candidate.fingerprint == fingerprint:
                    row = candidate
                    break
        if row is None:
            raise UkrecvError('inbound_not_found', 'No unmatched UK FPS credit for that proxy.')
        if row.status != IN_UNMATCHED:
            raise UkrecvError('not_claimable', 'This UK FPS credit is not waiting for a claim.')
        if row.expiry_at and self._now() > row.expiry_at:
            row.status = IN_EXPIRED
            row.reason = 'expired'
            row.updated_at = self._now()
            self.store.update_inbound(row)
            raise UkrecvError('return_window_closed', 'UK FPS claim window has closed.')
        existing = self.store.get_key_by_fingerprint(fingerprint)
        if existing is None or existing.status == KEY_CANCELLED:
            self.add_key(
                owner_userid=owner, actor=actor, actor_type=actor_type,
                nickname=nickname or ('UK FPS %s' % kind),
                proxy_type=kind, proxy=value, destination_account=dest,
            )
        elif existing.userid != owner:
            raise UkrecvError('key_duplicate', 'UK FPS proxy belongs to another customer.')
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

    def override_ofac(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> UkrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_HELD or row.reason != 'ofac':
            raise UkrecvError('not_overridable', 'Only OFAC-held UK FPS credits can be overridden.')
        row.ofac_overridden = True
        row.ofac_hit = False
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def override_cop(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> UkrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_HELD or row.reason != 'cop':
            raise UkrecvError('not_overridable', 'Only Confirmation-of-Payee-held credits can be overridden.')
        row.cop_overridden = True
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def release(self, *, inbound_id: str, actor: str, actor_type: str) -> UkrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_PENDING:
            raise UkrecvError('not_releasable', 'Only dual-control UK FPS credits can be released.')
        if actor == row.actor:
            raise UkrecvError('same_approver', 'A different employee must release this credit.')
        row.releaser = actor
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def reject(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> UkrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status not in OPEN_IN:
            raise UkrecvError('not_rejectable', 'This UK FPS credit cannot be rejected.')
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
            raise UkrecvError('not_exportable', 'UK FPS credit has no End-to-End ID to export.')
        _ = actor
        record = compose_uk1(
            e2e=row.e2e,
            rail=row.rail,
            proxy_type=row.proxy_type,
            proxy=row.proxy,
            bank=row.recv_bank,
            account_number=row.account_number,
            extra=row.payee_name,
            amount=parse_money(row.amount_instructed, allow_zero=True),
            purpose=row.purpose,
            status=row.status,
            currency=row.currency,
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
    ) -> UkrecvInbound:
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
    ) -> UkrecvInbound:
        self._require_enabled()
        row = self._get(inbound_id=inbound_id)
        self._assert_visible(row, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_return:
                raise UkrecvError('ukrecv_forbidden', 'Customers cannot return UK FPS credits.')
            if actor != row.userid:
                raise UkrecvError('ukrecv_forbidden', 'Cannot return another customer UK FPS credit.')
        return self._return(row, actor=actor, actor_type=actor_type, reason=reason, honor_window=True)

    def _customer_deadline(self, row: UkrecvInbound) -> float:
        posted = row.posted_at or row.created_at
        if row.rail in {'chaps', 'bacs'}:
            day = self.calendar.local_dt(posted).date()
            return self.calendar.session_close_ts(self.calendar.next_business_day(day), rail=row.rail)
        return posted + self.policy.customer_return_seconds

    def _return(
        self,
        row: UkrecvInbound,
        *,
        actor: str,
        actor_type: str,
        reason: str,
        honor_window: bool,
    ) -> UkrecvInbound:
        now = self._now()
        _ = actor
        if row.status == IN_RETURNED or row.returned_at:
            raise UkrecvError('already_returned', 'UK FPS credit already returned.')
        if row.status in OPEN_IN:
            row.status = IN_RETURNED
            row.reason = reason or 'cust'
            row.returned_at = now
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if row.status != IN_POSTED:
            raise UkrecvError('not_returnable', 'This UK FPS credit cannot be returned.')
        if honor_window and actor_type not in EMPLOYEE_ROLES:
            if now > self._customer_deadline(row):
                raise UkrecvError('return_window_closed', 'Customer UK FPS return window has closed.')
        if actor_type in EMPLOYEE_ROLES:
            elapsed = now - (row.posted_at or row.created_at)
            if elapsed > self.policy.staff_complaint_days * 86400:
                raise UkrecvError('med_window_closed', 'Staff complaint window is 90 days.')
        if self.debit_fn is None:
            raise UkrecvError('return_failed', 'Debit function is not configured.')
        remark = '%s return %s' % (row.rail, row.e2e[-12:])
        result = self.debit_fn(row.destination_account, row.amount_usd, remark)
        classified = _classify_money_result(result)
        if classified == 'nsf':
            raise UkrecvError('nsf', 'Return debit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            raise UkrecvError('return_failed', 'Return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.reason = reason or 'cust'
        row.returned_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[UkrecvInbound]:
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
        ytd = sum((parse_money(row.amount_usd, allow_zero=True) for row in received), Decimal('0.00'))
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
            'chaps_cap': money_str(self.policy.chaps_cap),
            'bacs_cap': money_str(self.policy.bacs_cap),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clearing': 'uk1',
            'currency': 'GBP',
            'fx_gbpusd': rate_str(self.policy.gbpusd),
        }

    def unmatched_snapshot(self) -> Dict[str, Any]:
        rows = self.store.list_inbounds(status=IN_UNMATCHED)
        return {
            'enabled': self.policy.enabled,
            'unmatched': [row.to_dict() for row in rows[:40]],
            'count': len(rows),
        }


_SERVICE: Optional[UkrecvService] = None


def set_service(service: Optional[UkrecvService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[UkrecvService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('UKRECV_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryUkrecvStore()
    path = os.environ.get('UKRECV_DB', DEFAULT_STORE_PATH)
    return SqliteUkrecvStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[UkClock] = None,
) -> UkrecvService:
    if store is None:
        store = default_store()
    return UkrecvService(
        UkrecvPolicy.from_env(),
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
        'ukrecv_forbidden': 403,
        'ukrecv_disabled': 403,
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
        'invalid_payee_name': 400,
        'invalid_iban': 400,
        'invalid_modulus': 400,
        'invalid_account_type': 400,
        'invalid_branch': 400,
        'invalid_email': 400,
        'amount_out_of_range': 400,
        'invalid_rail': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_reference': 400,
        'invalid_file': 400,
        'invalid_currency': 400,
        'invalid_fx': 400,
        'wrong_receiver': 400,
        'fps_amount_exceeded': 400,
        'chaps_amount_exceeded': 400,
        'bacs_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_key': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: UkrecvError) -> Dict[str, Any]:
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
    except UkrecvError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snapshot_for(service: UkrecvService, userid: str, actor: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(userid, actor=actor, actor_type=actor_type)


def handle_list(service: UkrecvService):
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
    return jsonify({'UkFpsIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def handle_unmatched(service: UkrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'ukrecv_forbidden'}), 403
    snap = service.unmatched_snapshot()
    return jsonify({'UnmatchedUkFpsIns': snap, 'UkFpsIn': snap}), 200


def handle_add_key(service: UkrecvService):
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
                raise UkrecvError('missing_customer_id', 'Customer id is required.')
        row = service.add_key(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname') or '',
            proxy_type=values.get('proxy_type') or values.get('key_type') or values.get('kind'),
            proxy=values.get('proxy') or values.get('iban') or values.get('key') or values.get('phone') or values.get('email') or values.get('sort'),
            destination_account=values.get('destination_account') or values.get('account') or values.get('default_account'),
        )
        return jsonify({
            'message': 'UK FPS proxy enrolled',
            'key': row.to_dict(),
            'UkFpsIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _key_status_route(service: UkrecvService, status: str, message: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        key_id = str(values.get('key_id') or '').strip()
        if not key_id:
            raise UkrecvError('missing_key', 'key_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.set_key_status(key_id=key_id, actor=userid, actor_type=actor_type, status=status)
        return jsonify({
            'message': message,
            'key': row.to_dict(),
            'UkFpsIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: UkrecvService):
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
        return jsonify({'preview': preview, 'UkFpsIn': _snapshot_for(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest(service: UkrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'UK FPS ingested' if created else 'UK FPS already ingested',
            'inbound': row.to_dict(),
            'UkFpsIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_ingest_file(service: UkrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        payload = values.get('file') or values.get('body') or values.get('payload')
        if not payload:
            raise UkrecvError('missing_file', 'UK1 file body is required.')
        actor_type = session.get('usertype') or 'customer'
        rows, created = service.ingest_file(
            actor=userid, actor_type=actor_type, payload=str(payload),
            sender_name=str(values.get('name') or values.get('sender_name') or ''),
            send_bank=str(values.get('send_bank') or values.get('sender_bank') or ''),
        )
        owner = rows[-1].userid if rows and rows[-1].userid else userid
        return jsonify({
            'message': 'UK1 file ingested',
            'created': created,
            'inbounds': [row.to_dict() for row in rows],
            'UkFpsIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_assign(service: UkrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise UkrecvError('missing_inbound', 'inbound_id is required.')
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
            'message': 'UK FPS assigned',
            'inbound': row.to_dict(),
            'UkFpsIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_claim(service: UkrecvService):
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
            proxy=values.get('proxy') or values.get('iban') or values.get('key') or values.get('phone') or values.get('email') or values.get('sort'),
            destination_account=values.get('destination_account') or values.get('account'),
            nickname=str(values.get('nickname') or ''),
            owner_userid=str(values.get('customer_id') or ''),
        )
        return jsonify({
            'message': 'UK FPS claimed',
            'inbound': row.to_dict(),
            'UkFpsIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_inbound_route(service: UkrecvService, action: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise UkrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        if action == 'override':
            row = service.override_ofac(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'cop':
            row = service.override_cop(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Confirmation of Payee hold overridden'
        elif action == 'release':
            row = service.release(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            message = 'UK FPS released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'UK FPS rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=str(values.get('reason') or 'cust'),
            )
            message = 'UK FPS returned'
        elif action == 'export':
            exported = service.export(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            owner = exported['inbound'].get('userid') or userid
            return jsonify({
                'message': 'UK1 exported',
                'file': exported['file'],
                'inbound': exported['inbound'],
                'UkFpsIn': _snapshot_for(service, owner, userid, actor_type),
            }), 200
        else:
            raise UkrecvError('invalid_status', 'Unknown UK FPS action.')
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'UkFpsIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: UkrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise UkrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.request_return(
            inbound_id=inbound_id, actor=userid, actor_type=actor_type,
            reason=str(values.get('reason') or 'cust'),
        )
        return jsonify({
            'message': 'UK FPS return requested',
            'inbound': row.to_dict(),
            'UkFpsIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: UkrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'UkFpsIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def attach_ukrecv_routes(app, service: UkrecvService) -> None:
    @app.route('/listUkFpsIns', methods=['POST', 'GET'])
    def list_ukfps_ins_route():
        return handle_list(service)

    @app.route('/listUkFpsRecvKeys', methods=['POST', 'GET'])
    def list_ukfps_recv_keys_route():
        return handle_list(service)

    @app.route('/listUnmatchedUkFpsIns', methods=['POST', 'GET'])
    def list_unmatched_ukfps_ins_route():
        return handle_unmatched(service)

    @app.route('/addUkFpsRecvKey', methods=['POST', 'GET'])
    def add_ukfps_recv_key_route():
        return handle_add_key(service)

    @app.route('/pauseUkFpsRecvKey', methods=['POST', 'GET'])
    def pause_ukfps_recv_key_route():
        return _key_status_route(service, KEY_PAUSED, 'UK FPS proxy paused')

    @app.route('/resumeUkFpsRecvKey', methods=['POST', 'GET'])
    def resume_ukfps_recv_key_route():
        return _key_status_route(service, KEY_ACTIVE, 'UK FPS proxy resumed')

    @app.route('/cancelUkFpsRecvKey', methods=['POST', 'GET'])
    def cancel_ukfps_recv_key_route():
        return _key_status_route(service, KEY_CANCELLED, 'UK FPS proxy cancelled')

    @app.route('/previewUkFpsIn', methods=['POST', 'GET'])
    def preview_ukfps_in_route():
        return handle_preview(service)

    @app.route('/ingestUkFpsIn', methods=['POST', 'GET'])
    def ingest_ukfps_in_route():
        return handle_ingest(service)

    @app.route('/ingestUkFpsInFile', methods=['POST', 'GET'])
    def ingest_ukfps_in_file_route():
        return handle_ingest_file(service)

    @app.route('/claimUkFpsIn', methods=['POST', 'GET'])
    def claim_ukfps_in_route():
        return handle_claim(service)

    @app.route('/assignUkFpsIn', methods=['POST', 'GET'])
    def assign_ukfps_in_route():
        return handle_assign(service)

    @app.route('/overrideUkFpsInOfac', methods=['POST', 'GET'])
    def override_cop_in_ofac_route():
        return _staff_inbound_route(service, 'override')

    @app.route('/overrideUkFpsInCop', methods=['POST', 'GET'])
    def override_cop_in_ukfps_route():
        return _staff_inbound_route(service, 'cop')

    @app.route('/releaseUkFpsIn', methods=['POST', 'GET'])
    def release_ukfps_in_route():
        return _staff_inbound_route(service, 'release')

    @app.route('/rejectUkFpsIn', methods=['POST', 'GET'])
    def reject_ukfps_in_route():
        return _staff_inbound_route(service, 'reject')

    @app.route('/returnUkFpsIn', methods=['POST', 'GET'])
    def return_ukfps_in_route():
        return _staff_inbound_route(service, 'return')

    @app.route('/exportUkFpsIn', methods=['POST', 'GET'])
    def export_ukfps_in_route():
        return _staff_inbound_route(service, 'export')

    @app.route('/requestUkFpsInReturn', methods=['POST', 'GET'])
    def request_ukfps_in_return_route():
        return handle_request_return(service)

    @app.route('/runDueUkFpsIns', methods=['POST', 'GET'])
    def run_due_ukfps_ins_route():
        return handle_run_due(service)
