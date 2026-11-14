"""Inbound Zengin / More Time / BOJ-NET receive posting.

Staff ingest incoming JPY credits. Recipients enroll Zengin proxies
(4+3+7 account, +81 mobile, email) at this bank's Zengin code.
Matched More Time credits post instantly 24/7. Core Zengin waits for
08:30–15:30 JST. BOJ-NET waits for 08:30–19:00 JST. Unmatched proxies
wait for a customer claim or staff assignment.
Independent of outbound Zengin origination (PR #190), inbound HK FPS
(PR #186), domestic Fedwire (PR #73), and ACH linking (PR #68).
Existing `/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay
unchanged.

Foundations (reusable beyond this screen):
- Zengin bank code (4 digits) + branch (3 digits) + 7-digit account
- Account type 1 ordinary / 2 current / 4 savings
- Katakana beneficiary name (max 15 kana, hiragana folded)
- ISO-style JP phone (+81 70/80/90) and email proxy
- Kana name inquiry (`match` / `close_match` / `no_match` / `unavailable`)
- MTS / ZGN / BOJ End-to-End ID (`MTS`/`ZGN`/`BOJ` + YYYYMMDD + seq)
- ZGN1 14-pipe file (XML/DOCTYPE and non-Zengin clearing rejected)
- JpClock JST UTC+9: More Time 24/7; core Zengin 08:30–15:30;
  BOJ-NET 08:30–19:00; Japan bank holidays + 振替休日
- JPYUSD quote book converting instructed amount to USD credit
- Zengin claim (customer presents matching proxy; enroll rematches unmatched)
- Return windows (More Time 24h customer / Zengin+BOJ next JP day / 90-day staff)
- Dual-control at JPY 1,000,000; MTS cap 10m / Zengin 100m / BOJ-NET 1bn

Reuses Fedwire OFAC screening, money parsing, and account directory
from `utility.wire`. Stores are pluggable (memory for tests, sqlite WAL
for restart-safe default). Full account, kana, fingerprint, phone, email,
and E2E ID never appear in to_dict / snapshots.
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
    'nta': 'tax', 'taxes': 'tax', 'zeikin': 'tax',
    'mortgage': 'loan', 'housing': 'rent',
}
RAILS = frozenset({'moretime', 'zengin', 'bojnet'})
RAIL_ALIASES = {
    'mts': 'moretime', 'more_time': 'moretime', 'more-time': 'moretime',
    '24/7': 'moretime', 'instant': 'moretime', 'overlay': 'moretime',
    'core': 'zengin', 'zenshin': 'zengin', 'zgn': 'zengin', 'clearing': 'zengin',
    'boj': 'bojnet', 'boj_net': 'bojnet', 'boj-net': 'bojnet', 'rtgs': 'bojnet',
    'bank_of_japan': 'bojnet',
}
ACCOUNT_TYPES = frozenset({'1', '2', '4'})
ACCOUNT_TYPE_ALIASES = {
    'ordinary': '1', 'futsu': '1', 'savings_ord': '1', '普通': '1',
    'current': '2', 'toza': '2', 'checking': '2', '当座': '2',
    'savings': '4', 'chochiku': '4', '貯蓄': '4',
}
PROXY_TYPES = frozenset({'zengin', 'phone', 'email'})
PROXY_ALIASES = {
    'account': 'zengin', 'bank': 'zengin', 'branch': 'zengin',
    'mobile': 'phone', 'msisdn': 'phone', 'tel': 'phone', 'sms': 'phone',
    'mail': 'email', 'e_mail': 'email',
}
KANA_MATCH = 'match'
KANA_CLOSE = 'close_match'
KANA_NONE = 'no_match'
KANA_UNAVAILABLE = 'unavailable'
KANA_OUTCOMES = frozenset({KANA_MATCH, KANA_CLOSE, KANA_NONE, KANA_UNAVAILABLE})
CLEARING_BY_RAIL = {'moretime': 'MTS', 'zengin': 'ZGN', 'bojnet': 'BOJ'}
REJECTED_CLEARING = frozenset({
    'AUNPP', 'USRTPP', 'FEDNOW', 'FEDWIRE', 'ACH', 'UPI', 'CHIPS', 'SWIFT',
    'PIX', 'SPEI', 'NPP', 'OSKO', 'FAST', 'PAYNOW', 'MEPS', 'GIRO', 'PNW',
    'FPS', 'FPS1', 'CHATS', 'ECG', 'HKFPS', 'HKID', 'CHAPS', 'BACS', 'SEPA',
})
DEFAULT_BANKS = {
    '0001': 'MIZUHO',
    '0005': 'MUFG',
    '0009': 'SMBC',
    '0010': 'RESONA',
    '0033': 'PAYPAY',
    '0036': 'RAKUTEN',
    '0038': 'SBI SUMISHIN',
    '9900': 'JAPAN POST',
    '0398': 'KONOHA BANK',
}
ZENGIN_PASS = frozenset({
    ('0001', '001', '1234567'),
    ('0005', '101', '0000001'),
    ('0009', '201', '7654321'),
    ('0398', '001', '1000001'),
})
DEFAULT_ZENGIN_DIRECTORY = {
    'zengin:00010011234567': 'ヤマダタロウ',
    'phone:+819011122222': 'ヤマダタロウ',
    'email:taro@example.jp': 'ヤマダタロウ',
    'zengin:00051010000001': 'サトウハナコ',
    'phone:+819012345678': 'アダラブレス',
}
ZGN1_VERSION = '1'
ZGN1_FIELDS = 14
DEFAULT_STORE_PATH = 'SystemLogs/jprecv.sqlite'
DEFAULT_RECEIVER_BANK = '0398'
DEFAULT_SENDER_BANK = '0001'
CUSTOMER_RETURN_SECONDS = 24 * 60 * 60
STAFF_COMPLAINT_DAYS = 90
CLAIM_SECONDS = 24 * 60 * 60
JST_OFFSET_MINUTES = 9 * 60
DEFAULT_JPYUSD = Decimal('0.0067')
BLOCKED_E2E_PREFIXES = (
    'IMAD', 'UETR', 'NPP', 'AUNPP', 'RRN', 'STAN', 'ARN', 'RFI', 'RFR', 'RTR',
    'PNW', 'FAST', 'MEPS', 'GIRO', 'PIX', 'SPEI', 'UPI', 'FPS1', 'CHATS', 'ECG',
    'HKFPS', 'HKID', 'PAYNOW', 'FPS', 'CHAPS', 'BACS', 'FEDWIRE', 'FEDNOW',
)
E2E_PREFIX = {'moretime': 'MTS', 'zengin': 'ZGN', 'bojnet': 'BOJ'}
EQUINOX = {
    2023: (date(2023, 3, 21), date(2023, 9, 23)),
    2024: (date(2024, 3, 20), date(2024, 9, 22)),
    2025: (date(2025, 3, 20), date(2025, 9, 23)),
    2026: (date(2026, 3, 20), date(2026, 9, 23)),
    2027: (date(2027, 3, 21), date(2027, 9, 23)),
}
FX_QUANTUM = Decimal('0.000001')
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)


class JprecvError(ValueError):
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
        raise JprecvError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise JprecvError('invalid_name', 'Sender legal name must be 2-80 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise JprecvError('invalid_purpose', 'Unknown Zengin purpose.')
    return text


def normalize_rail(value: Any, *, default: str = 'moretime') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if text not in RAILS:
        raise JprecvError('invalid_rail', 'Rail must be moretime, zengin, or bojnet.')
    return text


def normalize_currency(value: Any, *, default: str = 'JPY', rail: str = 'moretime') -> str:
    _ = rail
    text = str(value or default or 'JPY').strip().upper()
    if text in {'¥', 'YEN', '円'}:
        text = 'JPY'
    if text != 'JPY':
        raise JprecvError('invalid_currency', 'Zengin / More Time / BOJ-NET is JPY only.')
    return text


def normalize_proxy_type(value: Any, *, default: str = '') -> str:
    text = str(value or default).strip().lower().replace('-', '_')
    text = PROXY_ALIASES.get(text, text)
    if not text:
        return ''
    if text not in PROXY_TYPES:
        raise JprecvError('invalid_proxy', 'Proxy must be zengin, phone, or email.')
    return text


def normalize_account_type(value: Any, *, default: str = '1') -> str:
    text = str(value or '').strip().lower()
    text = ACCOUNT_TYPE_ALIASES.get(text, text)
    if not text:
        text = default
    if text not in ACCOUNT_TYPES:
        raise JprecvError('invalid_account_type', 'Account type must be 1, 2, or 4.')
    return text


def normalize_bank_code(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise JprecvError('invalid_bank', 'Zengin bank code is required.')
        return ''
    if len(digits) > 4:
        raise JprecvError('invalid_bank', 'Zengin bank code must be 4 digits and not 0000.')
    padded = digits.zfill(4)
    if padded == '0000':
        raise JprecvError('invalid_bank', 'Zengin bank code must be 4 digits and not 0000.')
    return padded


def normalize_branch_code(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise JprecvError('invalid_branch', 'Zengin branch code is required.')
        return ''
    if len(digits) > 3:
        raise JprecvError('invalid_branch', 'Zengin branch code must be 3 digits.')
    return digits.zfill(3)


def normalize_zengin_account(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise JprecvError('invalid_zengin_account', 'Zengin account number is required.')
        return ''
    if len(digits) > 7:
        raise JprecvError('invalid_zengin_account', 'Zengin account number must be 7 digits.')
    return digits.zfill(7)


def zengin_ok(bank: str, branch: str, account: str) -> bool:
    if (bank, branch, account) in ZENGIN_PASS:
        return True
    if not (len(bank) == 4 and bank.isdigit() and bank != '0000'):
        return False
    if not (len(branch) == 3 and branch.isdigit()):
        return False
    if not (len(account) == 7 and account.isdigit()):
        return False
    return True


def mask_bank(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit()).zfill(4)[-4:]
    if len(digits) != 4:
        return '****'
    return digits[:1] + '***'


def bank_name(value: Any, *, directory: Optional[Dict[str, str]] = None) -> str:
    try:
        code = normalize_bank_code(value)
    except JprecvError:
        return ''
    mapping = directory if directory is not None else DEFAULT_BANKS
    return mapping.get(code, '')


def normalize_external_account(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise JprecvError('invalid_account', 'Account is required.')
        return ''
    if not (4 <= len(digits) <= 16):
        raise JprecvError('invalid_account', 'Account must be 4-16 digits.')
    return digits


def mask_account(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return ('*' * max(0, len(digits) - 4)) + digits[-4:]


def _hiragana_to_katakana(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if 0x3041 <= code <= 0x3096:
            out.append(chr(code + 0x60))
        else:
            out.append(ch)
    return ''.join(out)


def normalize_kana(value: Any, *, required: bool = True) -> str:
    raw = str(value or '')
    text = _hiragana_to_katakana(raw)
    text = text.replace(' ', '').replace('\u3000', '').replace('・', '')
    text = text.strip()
    if not text:
        if required:
            raise JprecvError('invalid_kana', 'Katakana beneficiary name is required.')
        return ''
    if len(text) > 15:
        raise JprecvError('invalid_kana', 'Katakana name must be at most 15 characters.')
    return text


def mask_kana(value: Any) -> str:
    text = str(value or '')
    if len(text) <= 2:
        return text[:1] + '***' if text else ''
    return text[0] + '***' + text[-1]


def normalize_phone(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise JprecvError('invalid_phone', 'Mobile number is required.')
        return ''
    if digits.startswith('81') and len(digits) >= 12:
        national = digits[2:]
    elif digits.startswith('0') and len(digits) == 11:
        national = digits[1:]
    else:
        national = digits
    if not (len(national) == 10 and national[0] in '789' and national[1] == '0'):
        raise JprecvError('invalid_phone', 'Japan mobile must be +81 70/80/90.')
    return '+81' + national


def mask_phone(value: Any) -> str:
    text = str(value or '')
    digits = ''.join(ch for ch in text if ch.isdigit())
    if digits.startswith('81') and len(digits) >= 12:
        national = digits[2:]
    elif text.startswith('+81'):
        national = digits[2:] if digits.startswith('81') else digits
    else:
        national = digits
    if len(national) < 4:
        return '+81****'
    return '+81****' + national[-4:]


def normalize_email(value: Any, *, required: bool = False) -> str:
    text = str(value or '').strip().lower()
    if not text:
        if required:
            raise JprecvError('invalid_email', 'Email is required.')
        return ''
    if not re.fullmatch(r'[a-z0-9._%+-]{1,64}@[a-z0-9.-]+\.[a-z]{2,24}', text):
        raise JprecvError('invalid_email', 'Email proxy is invalid.')
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
    digits = ''.join(ch for ch in text if ch.isdigit())
    if digits.startswith('81') and len(digits) >= 12:
        return 'phone'
    if digits.startswith('0') and len(digits) == 11 and digits[1] in '789':
        return 'phone'
    if len(digits) == 10 and digits[0] in '789' and digits[1] == '0':
        return 'phone'
    if len(digits) == 14:
        return 'zengin'
    return ''


def normalize_proxy(proxy_type: Any, value: Any, *, required: bool = False) -> Tuple[str, str]:
    kind = normalize_proxy_type(proxy_type, default='')
    if not kind:
        kind = infer_proxy_type(value)
    if not kind:
        if required:
            raise JprecvError('invalid_proxy', 'Proxy type is required.')
        return '', ''
    if kind == 'phone':
        return kind, normalize_phone(value, required=True)
    if kind == 'email':
        return kind, normalize_email(value, required=True)
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 14:
        raise JprecvError('invalid_proxy', 'Zengin proxy must be bank+branch+account (14 digits).')
    bank, branch, account = digits[:4], digits[4:7], digits[7:]
    if not zengin_ok(bank, branch, account):
        raise JprecvError('invalid_zengin', 'Zengin bank/branch/account is not valid.')
    return kind, digits


def mask_proxy(proxy_type: str, value: str) -> str:
    if proxy_type == 'phone':
        return mask_phone(value)
    if proxy_type == 'email':
        return mask_email(value)
    if proxy_type == 'zengin' and len(value) == 14:
        return '%s-%s-****%s' % (value[:4], value[4:7], value[-4:])
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
    head = prefix.group(0) if prefix else text[:3]
    return head + '****' + text[-4:]


def compose_e2e(rail: str, cycle_date: str, sequence: int) -> str:
    kind = normalize_rail(rail)
    if not re.fullmatch(r'\d{8}', str(cycle_date or '')):
        raise JprecvError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise JprecvError('invalid_reference', 'E2E sequence out of range.')
    return '%s%s%06d' % (E2E_PREFIX[kind], cycle_date, seq)


def normalize_e2e(value: Any) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        raise JprecvError('invalid_reference', 'Zengin End-to-End ID is required.')
    if any(text.startswith(prefix) for prefix in BLOCKED_E2E_PREFIXES):
        raise JprecvError('invalid_reference', 'End-to-End ID must not use another rail prefix.')
    if len(text) == 22 and text[:8].isdigit():
        raise JprecvError('invalid_reference', 'Fedwire IMAD is not a Zengin End-to-End ID.')
    if not re.fullmatch(r'(MTS|ZGN|BOJ)\d{14}', text):
        raise JprecvError('invalid_reference', 'E2E ID must be MTS/ZGN/BOJ + date + seq.')
    return text


def compose_amount_field(amount: Decimal) -> str:
    quantized = amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    if quantized < 0 or quantized > Decimal('999999999.99'):
        raise JprecvError('invalid_amount', 'Amount cannot be encoded in ZGN1.')
    return money_str(quantized)


def parse_amount_field(value: Any) -> Decimal:
    try:
        return parse_money(value)
    except AmountError as exc:
        raise JprecvError('invalid_amount', 'ZGN1 amount field is invalid.') from exc


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
    text = format(quantized.normalize(), 'f')
    if '.' not in text:
        text += '.0'
    return text


def convert_instructed_to_usd(amount: Decimal, rate: Decimal) -> Decimal:
    if rate <= 0:
        raise JprecvError('invalid_fx', 'FX rate must be positive.')
    return (amount * rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def kana_outcome(registered: str, presented: str) -> str:
    left = normalize_kana(registered, required=False)
    right = normalize_kana(presented, required=False)
    if not left or not right:
        return KANA_UNAVAILABLE
    if left == right:
        return KANA_MATCH
    if left.startswith(right) or right.startswith(left):
        return KANA_CLOSE
    if len(left) >= 2 and len(right) >= 2 and left[:2] == right[:2]:
        return KANA_CLOSE
    return KANA_NONE


def _reject_markup(text: str) -> None:
    lowered = text.lower()
    if '<xml' in lowered or '<!doctype' in lowered or '<?xml' in lowered or '<zgn' in lowered or '<mts' in lowered:
        raise JprecvError('invalid_file', 'XML/DOCTYPE ZGN1 files are rejected.')


def compose_zgn1(
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
    currency: str = 'JPY',
    mask: bool = True,
) -> str:
    kind = normalize_rail(rail)
    clearing = CLEARING_BY_RAIL[kind]
    money = normalize_currency(currency, rail=kind)
    kana_field = extra or ''
    if mask and kana_field:
        kana_field = mask_kana(kana_field)
    return '|'.join((
        'ZGN1',
        ZGN1_VERSION,
        mask_e2e(e2e) if mask else normalize_e2e(e2e),
        kind,
        proxy_type or '',
        mask_proxy(proxy_type, proxy) if mask and proxy else (proxy or ''),
        mask_bank(bank) if mask and bank else (normalize_bank_code(bank, required=False) if bank else ''),
        mask_account(account_number) if mask and account_number else (account_number or ''),
        kana_field,
        compose_amount_field(amount),
        normalize_purpose(purpose),
        str(status or IN_POSTED),
        money,
        clearing,
    ))


def parse_zgn1(text: Any) -> Dict[str, Any]:
    raw = str(text or '').strip()
    if not raw:
        raise JprecvError('invalid_file', 'ZGN1 record is empty.')
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != ZGN1_FIELDS or parts[0] != 'ZGN1' or parts[1] != ZGN1_VERSION:
        raise JprecvError('invalid_file', 'ZGN1 record is malformed.')
    clearing = parts[13].strip().upper()
    if clearing in REJECTED_CLEARING or clearing not in {'MTS', 'ZGN', 'BOJ'}:
        raise JprecvError('invalid_file', 'ZGN1 clearing must be MTS/ZGN/BOJ.')
    joined = '|'.join(parts[2:9])
    if '*' in joined or '…' in joined:
        raise JprecvError('invalid_file', 'Masked ZGN1 record cannot be re-ingested.')
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
        'recv_bank': normalize_bank_code(parts[6], required=bool(parts[6])),
        'account_number': normalize_external_account(parts[7], required=bool(parts[7])),
        'kana': normalize_kana(parts[8], required=False) if parts[8] else '',
        'amount': parse_amount_field(parts[9]),
        'purpose': normalize_purpose(parts[10]),
        'status': parts[11],
        'currency': normalize_currency(parts[12], rail=rail),
        'clearing': clearing,
        'name': '',
        'send_bank': DEFAULT_SENDER_BANK,
    }


def split_zgn1_file(text: Any) -> List[str]:
    raw = str(text or '')
    _reject_markup(raw)
    records = [line.strip() for line in raw.replace('\r\n', '\n').split('\n') if line.strip()]
    if not records:
        raise JprecvError('invalid_file', 'ZGN1 file is empty.')
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
        values.get('proxy') or values.get('zengin') or values.get('key')
        or values.get('phone') or values.get('email')
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
            raise JprecvError('invalid_amount', 'Amount is invalid.') from exc
    e2e = str(values.get('end_to_end_id') or values.get('e2e') or '').strip()
    rail = normalize_rail(values.get('rail') or 'moretime')
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
    currency = normalize_currency(values.get('currency') or 'JPY', rail=rail)
    kana = normalize_kana(values.get('kana') or values.get('beneficiary_kana') or '', required=False)
    return {
        'e2e': e2e,
        'rail': rail,
        'proxy_type': kind,
        'proxy': proxy,
        'bank': recv,
        'account_number': dest_acct,
        'kana': kana,
        'amount': amount,
        'purpose': normalize_purpose(values.get('purpose')),
        'name': name or 'ZENGIN PAYER',
        'currency': currency,
        'send_bank': send,
        'recv_bank': recv,
    }


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return date(year, month, 1 + offset + (n - 1) * 7)


def _substitute(day: date) -> date:
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def jp_holidays(year: int) -> set:
    vernal, autumn = EQUINOX.get(year, (date(year, 3, 20), date(year, 9, 23)))
    raw = {
        date(year, 1, 1),
        date(year, 1, 2),
        date(year, 1, 3),
        _nth_weekday(year, 1, 0, 2),
        date(year, 2, 11),
        date(year, 2, 23),
        vernal,
        date(year, 4, 29),
        date(year, 5, 3),
        date(year, 5, 5),
        _nth_weekday(year, 7, 0, 3),
        date(year, 8, 11),
        _nth_weekday(year, 9, 0, 3),
        autumn,
        _nth_weekday(year, 10, 0, 2),
        date(year, 11, 3),
        date(year, 11, 23),
    }
    # Golden Week 5/4 is Greenery Day
    raw.add(date(year, 5, 4))
    observed = set(raw)
    for day in list(raw):
        if day.weekday() == 6:
            observed.add(_substitute(day))
    return observed


class JpClock:
    """JST More Time 24/7 vs core Zengin / BOJ-NET banking-day clock."""

    def __init__(
        self,
        *,
        zengin_open_hour: int = 8,
        zengin_open_minute: int = 30,
        zengin_cutoff_hour: int = 15,
        zengin_cutoff_minute: int = 30,
        boj_open_hour: int = 8,
        boj_open_minute: int = 30,
        boj_cutoff_hour: int = 19,
        boj_cutoff_minute: int = 0,
        tz_offset_minutes: int = JST_OFFSET_MINUTES,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.zengin_open_hour = int(zengin_open_hour)
        self.zengin_open_minute = int(zengin_open_minute)
        self.zengin_cutoff_hour = int(zengin_cutoff_hour)
        self.zengin_cutoff_minute = int(zengin_cutoff_minute)
        self.boj_open_hour = int(boj_open_hour)
        self.boj_open_minute = int(boj_open_minute)
        self.boj_cutoff_hour = int(boj_cutoff_hour)
        self.boj_cutoff_minute = int(boj_cutoff_minute)
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
        return jp_holidays(year) | {day for day in self.extra_holidays if day.year == year}

    def is_weekend(self, day: date) -> bool:
        return day.weekday() >= 5

    def is_holiday(self, day: date) -> bool:
        return day in self.holidays(day.year)

    def is_business_day(self, day: date) -> bool:
        return not self.is_weekend(day) and not self.is_holiday(day)

    def _window(self, rail: str) -> Tuple[int, int, int, int]:
        if rail == 'bojnet':
            return (
                self.boj_open_hour, self.boj_open_minute,
                self.boj_cutoff_hour, self.boj_cutoff_minute,
            )
        return (
            self.zengin_open_hour, self.zengin_open_minute,
            self.zengin_cutoff_hour, self.zengin_cutoff_minute,
        )

    def is_after_cutoff(self, ts: float, *, rail: str = 'moretime') -> bool:
        kind = normalize_rail(rail)
        if kind == 'moretime':
            return False
        local = self.local_dt(ts)
        day = local.date()
        if not self.is_business_day(day):
            return True
        open_h, open_m, cut_h, cut_m = self._window(kind)
        minutes = local.hour * 60 + local.minute
        return minutes < open_h * 60 + open_m or minutes >= cut_h * 60 + cut_m

    def is_session(self, ts: float, *, rail: str = 'zengin') -> bool:
        kind = normalize_rail(rail)
        if kind == 'moretime':
            return True
        return not self.is_after_cutoff(ts, rail=kind)

    def next_business_day(self, day: date) -> date:
        cursor = day + timedelta(days=1)
        while not self.is_business_day(cursor):
            cursor += timedelta(days=1)
        return cursor

    def value_date(self, ts: float, *, rail: str = 'moretime') -> date:
        kind = normalize_rail(rail)
        local = self.local_dt(ts)
        day = local.date()
        if kind == 'moretime':
            return day
        if not self.is_business_day(day) or self.is_after_cutoff(ts, rail=kind):
            return self.next_business_day(day)
        return day

    def cycle_date(self, ts: float, *, rail: str = 'moretime') -> str:
        return self.value_date(ts, rail=rail).strftime('%Y%m%d')

    def session_close_ts(self, day: date, *, rail: str = 'zengin') -> float:
        kind = normalize_rail(rail)
        if kind == 'bojnet':
            hour, minute = self.boj_cutoff_hour, self.boj_cutoff_minute
        else:
            hour, minute = self.zengin_cutoff_hour, self.zengin_cutoff_minute
        local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.tz)
        return local.timestamp()

    def snapshot(self, ts: float, *, rail: str = 'moretime') -> Dict[str, Any]:
        kind = normalize_rail(rail)
        local = self.local_dt(ts)
        day = local.date()
        after = self.is_after_cutoff(ts, rail=kind)
        if kind == 'bojnet':
            cutoff = '%02d:%02d' % (self.boj_cutoff_hour, self.boj_cutoff_minute)
            open_at = '%02d:%02d' % (self.boj_open_hour, self.boj_open_minute)
        elif kind == 'zengin':
            cutoff = '%02d:%02d' % (self.zengin_cutoff_hour, self.zengin_cutoff_minute)
            open_at = '%02d:%02d' % (self.zengin_open_hour, self.zengin_open_minute)
        else:
            cutoff = '24/7'
            open_at = '00:00'
        return {
            'rail': kind,
            'local_date': day.isoformat(),
            'local_time': local.strftime('%H:%M'),
            'timezone': 'JST',
            'open': open_at,
            'cutoff': cutoff,
            'session': self.is_session(ts, rail=kind),
            'after_cutoff': after,
            'always_on': kind == 'moretime',
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
class JprecvPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_claim: bool = True
    customer_return: bool = True
    allow_credit: bool = False
    max_keys: int = 12
    max_inbounds: int = 120
    min_amount: Decimal = Decimal('0.01')
    moretime_cap: Decimal = Decimal('10000000.00')
    zengin_cap: Decimal = Decimal('100000000.00')
    bojnet_cap: Decimal = Decimal('1000000000.00')
    dual_control_threshold: Decimal = Decimal('1000000.00')
    jpyusd: Decimal = DEFAULT_JPYUSD
    require_kana_match: bool = True
    kana_directory: Dict[str, str] = None  # type: ignore[assignment]
    tz_offset_minutes: int = JST_OFFSET_MINUTES
    zengin_open_hour: int = 8
    zengin_open_minute: int = 30
    zengin_cutoff_hour: int = 15
    zengin_cutoff_minute: int = 30
    boj_open_hour: int = 8
    boj_open_minute: int = 30
    boj_cutoff_hour: int = 19
    boj_cutoff_minute: int = 0
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
        if self.kana_directory is None:
            self.kana_directory = dict(DEFAULT_ZENGIN_DIRECTORY)

    def cap_for(self, rail: str) -> Decimal:
        if rail == 'zengin':
            return self.zengin_cap
        if rail == 'bojnet':
            return self.bojnet_cap
        return self.moretime_cap

    def rate_for(self, currency: str) -> Decimal:
        _ = currency
        return self.jpyusd

    @classmethod
    def from_env(cls) -> 'JprecvPolicy':
        extra = _env_list('JPRECV_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        source = os.environ.get('JPRECV_RECEIVER') or DEFAULT_RECEIVER_BANK
        try:
            receiver = normalize_bank_code(source)
        except JprecvError:
            receiver = DEFAULT_RECEIVER_BANK
        return cls(
            enabled=_env_bool('JPRECV_ENABLED', True),
            customer_manage=_env_bool('JPRECV_CUSTOMER_MANAGE', True),
            customer_claim=_env_bool('JPRECV_CUSTOMER_CLAIM', True),
            customer_return=_env_bool('JPRECV_CUSTOMER_RETURN', True),
            allow_credit=_env_bool('JPRECV_ALLOW_CREDIT', False),
            max_keys=max(1, _env_int('JPRECV_MAX_KEYS', 12)),
            max_inbounds=max(1, _env_int('JPRECV_MAX_INBOUNDS', 120)),
            min_amount=_env_money('JPRECV_MIN_AMOUNT', '0.01'),
            moretime_cap=_env_money('JPRECV_MORETIME_CAP', '10000000.00'),
            zengin_cap=_env_money('JPRECV_ZENGIN_CAP', '100000000.00'),
            bojnet_cap=_env_money('JPRECV_BOJNET_CAP', '1000000000.00'),
            dual_control_threshold=_env_money('JPRECV_DUAL_CONTROL', '1000000.00'),
            jpyusd=_env_rate('JPRECV_FX_JPYUSD', '0.0067'),
            require_kana_match=_env_bool('JPRECV_REQUIRE_KANA', True),
            tz_offset_minutes=_env_int('JPRECV_TZ_OFFSET', JST_OFFSET_MINUTES),
            zengin_open_hour=max(0, min(23, _env_int('JPRECV_OPEN_HOUR', 8))),
            zengin_open_minute=max(0, min(59, _env_int('JPRECV_OPEN_MINUTE', 30))),
            zengin_cutoff_hour=max(0, min(23, _env_int('JPRECV_CUTOFF_HOUR', 15))),
            zengin_cutoff_minute=max(0, min(59, _env_int('JPRECV_CUTOFF_MINUTE', 30))),
            boj_open_hour=max(0, min(23, _env_int('JPRECV_BOJ_OPEN_HOUR', 8))),
            boj_open_minute=max(0, min(59, _env_int('JPRECV_BOJ_OPEN_MINUTE', 30))),
            boj_cutoff_hour=max(0, min(23, _env_int('JPRECV_BOJ_CUTOFF_HOUR', 19))),
            boj_cutoff_minute=max(0, min(59, _env_int('JPRECV_BOJ_CUTOFF_MINUTE', 0))),
            receiver_bank=receiver,
            customer_return_seconds=max(60, _env_int('JPRECV_RETURN_SECONDS', CUSTOMER_RETURN_SECONDS)),
            staff_complaint_days=max(1, _env_int('JPRECV_COMPLAINT_DAYS', STAFF_COMPLAINT_DAYS)),
            claim_seconds=max(60, _env_int('JPRECV_CLAIM_SECONDS', CLAIM_SECONDS)),
            watchlist=watch,
            extra_holidays=_env_list('JPRECV_HOLIDAYS'),
        )


@dataclass
class JprecvKey:
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
class JprecvInbound:
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
    kana: str = ''
    kana_outcome: str = ''
    kana_overridden: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'inbound_id': self.inbound_id,
            'e2e_masked': mask_e2e(self.e2e),
            'userid': self.userid,
            'account_last4': last4(self.destination_account) if self.destination_account else '',
            'amount_instructed': self.amount_instructed,
            'amount_usd': self.amount_usd,
            'amount_jpy': self.amount_instructed if self.currency == 'JPY' else '',
            'currency': self.currency,
            'sender_name': self.sender_name,
            'send_bank_masked': mask_bank(self.send_bank) if self.send_bank else '',
            'send_name': bank_name(self.send_bank) if self.send_bank else '',
            'recv_bank_masked': mask_bank(self.recv_bank) if self.recv_bank else '',
            'rail': self.rail,
            'proxy_type': self.proxy_type,
            'proxy_masked': mask_proxy(self.proxy_type, self.proxy) if self.proxy else '****',
            'kana_masked': mask_kana(self.kana) if self.kana else '',
            'kana_outcome': self.kana_outcome,
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


def _key_from_row(row: Any) -> JprecvKey:
    data = dict(row)
    return JprecvKey(
        key_id=data['key_id'], userid=data['userid'], nickname=data['nickname'],
        proxy_type=data['proxy_type'], proxy=data['proxy'], fingerprint=data['fingerprint'],
        destination_account=data['destination_account'], status=data['status'], actor=data['actor'],
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
    )


def _inbound_from_row(row: Any) -> JprecvInbound:
    data = dict(row)
    return JprecvInbound(
        inbound_id=data['inbound_id'], e2e=data['e2e'], userid=data['userid'],
        destination_account=data['destination_account'], amount_instructed=data['amount_instructed'],
        amount_usd=data['amount_usd'], currency=data.get('currency') or 'JPY',
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
        kana=data.get('kana') or '', kana_outcome=data.get('kana_outcome') or '',
        kana_overridden=bool(int(data.get('kana_overridden') or 0)),
    )


class MemoryJprecvStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.keys: Dict[str, JprecvKey] = {}
        self.inbounds: Dict[str, JprecvInbound] = {}
        self._by_e2e: Dict[str, str] = {}

    def put_key(self, row: JprecvKey) -> None:
        with self._lock:
            self.keys[row.key_id] = row

    def get_key(self, key_id: str) -> Optional[JprecvKey]:
        return self.keys.get(key_id)

    def get_key_by_fingerprint(self, fingerprint: str) -> Optional[JprecvKey]:
        matches = [
            row for row in self.keys.values()
            if row.fingerprint == fingerprint and row.status in OPEN_KEY
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda row: row.created_at, reverse=True)[0]

    def update_key(self, row: JprecvKey) -> None:
        self.put_key(row)

    def list_keys(self, userid: Optional[str] = None) -> List[JprecvKey]:
        rows = list(self.keys.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        return sorted(rows, key=lambda row: row.created_at)

    def put_inbound(self, row: JprecvInbound) -> None:
        with self._lock:
            self.inbounds[row.inbound_id] = row
            self._by_e2e[row.e2e] = row.inbound_id

    def get_inbound(self, inbound_id: str) -> Optional[JprecvInbound]:
        return self.inbounds.get(inbound_id)

    def get_by_e2e(self, e2e: str) -> Optional[JprecvInbound]:
        inbound_id = self._by_e2e.get(e2e)
        return self.inbounds.get(inbound_id) if inbound_id else None

    def update_inbound(self, row: JprecvInbound) -> None:
        self.put_inbound(row)

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[JprecvInbound]:
        rows = list(self.inbounds.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)


class SqliteJprecvStore:
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
                    currency TEXT NOT NULL DEFAULT 'JPY',
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

    def put_key(self, row: JprecvKey) -> None:
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

    def get_key(self, key_id: str) -> Optional[JprecvKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM keys WHERE key_id = ?', (key_id,)).fetchone()
        return _key_from_row(row) if row else None

    def get_key_by_fingerprint(self, fingerprint: str) -> Optional[JprecvKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM keys WHERE fingerprint = ? AND status IN ('active', 'paused')
                ORDER BY created_at DESC LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
        return _key_from_row(row) if row else None

    def update_key(self, row: JprecvKey) -> None:
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

    def list_keys(self, userid: Optional[str] = None) -> List[JprecvKey]:
        sql = 'SELECT * FROM keys'
        args: Tuple[Any, ...] = ()
        if userid is not None:
            sql += ' WHERE userid = ?'
            args = (userid,)
        sql += ' ORDER BY created_at'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_key_from_row(row) for row in rows]

    def put_inbound(self, row: JprecvInbound) -> None:
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

    def get_inbound(self, inbound_id: str) -> Optional[JprecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,)).fetchone()
        return _inbound_from_row(row) if row else None

    def get_by_e2e(self, e2e: str) -> Optional[JprecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE e2e = ?', (e2e,)).fetchone()
        return _inbound_from_row(row) if row else None

    def update_inbound(self, row: JprecvInbound) -> None:
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

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[JprecvInbound]:
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


class JprecvService:
    def __init__(
        self,
        policy: JprecvPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[JpClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: datetime.now(tz=timezone.utc).timestamp())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn or screen_name
        self.calendar = calendar or JpClock(
            zengin_open_hour=policy.zengin_open_hour,
            zengin_open_minute=policy.zengin_open_minute,
            zengin_cutoff_hour=policy.zengin_cutoff_hour,
            zengin_cutoff_minute=policy.zengin_cutoff_minute,
            boj_open_hour=policy.boj_open_hour,
            boj_open_minute=policy.boj_open_minute,
            boj_cutoff_hour=policy.boj_cutoff_hour,
            boj_cutoff_minute=policy.boj_cutoff_minute,
            tz_offset_minutes=policy.tz_offset_minutes,
            extra_holidays=policy.extra_holidays,
        )

    def _now(self) -> float:
        return float(self.clock())

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise JprecvError('jprecv_disabled', 'Zengin receive is disabled.')

    def _require_staff(self, actor_type: str) -> None:
        if actor_type not in EMPLOYEE_ROLES:
            raise JprecvError('jprecv_forbidden', 'Staff only.')

    def _require_customer_manage(self, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if not self.policy.customer_manage:
            raise JprecvError('jprecv_forbidden', 'Customers cannot manage Zengin proxies.')

    def _accounts(self, userid: str) -> Any:
        if not self.accounts_fn:
            return {}
        return self.accounts_fn(userid) or {}

    def _owned(self, userid: str, account: str) -> None:
        owned = own_accounts_from_customer_payload(self._accounts(userid))
        if account not in owned:
            raise JprecvError('invalid_account', 'Destination is not an account of this customer.')

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
    ) -> JprecvKey:
        self._require_enabled()
        self._require_customer_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise JprecvError('jprecv_forbidden', 'Cannot enroll a Zengin proxy for another customer.')
        kind, value = normalize_proxy(proxy_type, proxy, required=True)
        account = normalize_account(destination_account)
        self._owned(owner_userid, account)
        if not self._credit_allowed(owner_userid, account):
            raise JprecvError('credit_not_allowed', 'Credit accounts cannot receive Zengin credits.')
        fingerprint = proxy_fingerprint(kind, value)
        existing = self.store.get_key_by_fingerprint(fingerprint)
        if existing and existing.status in OPEN_KEY:
            raise JprecvError('key_duplicate', 'That Zengin proxy is already enrolled.')
        open_rows = [row for row in self.store.list_keys(owner_userid) if row.status in OPEN_KEY]
        if any(row.nickname.lower() == normalize_nickname(nickname).lower() for row in open_rows):
            raise JprecvError('key_duplicate', 'That nickname is already in use.')
        if len(open_rows) >= self.policy.max_keys:
            raise JprecvError('key_limit', 'Zengin enrollment limit reached.')
        now = self._now()
        row = JprecvKey(
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
    ) -> JprecvKey:
        self._require_enabled()
        row = self.store.get_key(key_id)
        if row is None:
            raise JprecvError('key_not_found', 'Zengin enrollment not found.')
        if actor_type not in EMPLOYEE_ROLES and actor != row.userid:
            raise JprecvError('jprecv_forbidden', 'Cannot change another customer Zengin proxy.')
        self._require_customer_manage(actor_type)
        if status not in KEY_STATUSES:
            raise JprecvError('invalid_status', 'Unknown Zengin status.')
        if row.status == KEY_CANCELLED:
            raise JprecvError('already_cancelled', 'Cancelled Zengin proxies cannot change.')
        if status == KEY_PAUSED and row.status != KEY_ACTIVE:
            raise JprecvError('already_paused' if row.status == KEY_PAUSED else 'invalid_status', 'Zengin proxy is not active.')
        if status == KEY_ACTIVE and row.status != KEY_PAUSED:
            raise JprecvError('already_active', 'Zengin proxy is already active.')
        if status == row.status:
            code = 'already_paused' if status == KEY_PAUSED else 'already_active'
            raise JprecvError(code, 'Zengin proxy already in that status.')
        row.status = status
        row.actor = actor
        row.updated_at = self._now()
        self.store.update_key(row)
        if status == KEY_ACTIVE:
            self._rematch_unmatched(row)
        return row

    def _rematch_unmatched(self, enrollment: JprecvKey) -> None:
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
    ) -> Tuple[JprecvInbound, bool]:
        self._require_enabled()
        self._require_staff(actor_type)
        payload = dict(values or {})
        if not payload.get('end_to_end_id') and not payload.get('e2e') and not payload.get('cycle_date') and not payload.get('value_date'):
            payload['cycle_date'] = self.calendar.cycle_date(
                self._now(), rail=normalize_rail(payload.get('rail') or 'moretime'),
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
    ) -> Tuple[List[JprecvInbound], int]:
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
        for record in split_zgn1_file(payload):
            parsed = parse_zgn1(record)
            parsed['recv_bank'] = parsed.get('recv_bank') or parsed.get('bank') or self.policy.receiver_bank
            parsed['send_bank'] = sender or parsed.get('send_bank') or DEFAULT_SENDER_BANK
            if fallback:
                parsed['name'] = fallback
            elif not parsed.get('name'):
                parsed['name'] = 'ZENGIN PAYER'
            row, is_new = self._ingest_parsed(actor=actor, parsed=parsed)
            rows.append(row)
            if is_new:
                created += 1
        return rows, created

    def _ingest_parsed(self, *, actor: str, parsed: Dict[str, Any]) -> Tuple[JprecvInbound, bool]:
        recv = parsed.get('recv_bank') or parsed.get('bank') or self.policy.receiver_bank
        if recv != self.policy.receiver_bank:
            raise JprecvError('wrong_receiver', 'Credit is not for this Zengin bank code.')
        existing = self.store.get_by_e2e(parsed['e2e'])
        if existing is not None:
            return existing, False
        total = len(self.store.list_inbounds())
        if total >= self.policy.max_inbounds:
            raise JprecvError('inbound_limit', 'Inbound Zengin limit reached.')
        amount = parsed['amount'] if isinstance(parsed['amount'], Decimal) else parse_money(parsed['amount'])
        rail = normalize_rail(parsed.get('rail') or 'moretime')
        cap = self.policy.cap_for(rail)
        if amount < self.policy.min_amount or amount > cap:
            code = '%s_amount_exceeded' % rail if amount > cap else 'amount_out_of_range'
            raise JprecvError(code, 'Amount is outside the %s range.' % rail)
        now = self._now()
        currency = normalize_currency(parsed.get('currency') or 'JPY', rail=rail)
        proxy = parsed.get('proxy') or ''
        kind = parsed.get('proxy_type') or ''
        fingerprint = proxy_fingerprint(kind, proxy) if proxy else ''
        usd = convert_instructed_to_usd(amount, self.policy.rate_for(currency))
        row = JprecvInbound(
            inbound_id=uuid.uuid4().hex,
            e2e=parsed['e2e'],
            userid='',
            destination_account='',
            amount_instructed=money_str(amount),
            amount_usd=money_str(usd),
            currency=currency,
            sender_name=parsed.get('name') or 'ZENGIN PAYER',
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
            kana=parsed.get('kana') or '',
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

    def _evaluate(self, row: JprecvInbound, *, rescreen: bool) -> JprecvInbound:
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
        except JprecvError:
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
        if self.policy.require_kana_match and not row.kana_overridden:
            registered = ''
            if row.proxy:
                registered = (self.policy.kana_directory or {}).get(proxy_key(row.proxy_type, row.proxy), '')
            outcome = kana_outcome(registered, row.kana) if (registered or row.kana) else KANA_UNAVAILABLE
            row.kana_outcome = outcome
            if outcome == KANA_NONE:
                row.status = IN_HELD
                row.reason = 'kana'
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
        if row.rail in {'zengin', 'bojnet'} and self.calendar.is_after_cutoff(now, rail=row.rail):
            row.status = IN_QUEUED
            row.reason = 'zengin_session' if row.rail == 'zengin' else 'bojnet_session'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        return self._post(row)

    def _post(self, row: JprecvInbound) -> JprecvInbound:
        if self.credit_fn is None:
            raise JprecvError('failed', 'Credit function is not configured.')
        remark = '%s from %s' % (row.rail, row.sender_name[:40])
        result = self.credit_fn(row.destination_account, row.amount_usd, remark)
        classified = _classify_money_result(result)
        now = self._now()
        if classified == 'nsf':
            row.status = IN_FAILED
            row.reason = 'nsf'
            row.updated_at = now
            self.store.update_inbound(row)
            raise JprecvError('nsf', 'Credit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            row.status = IN_FAILED
            row.reason = 'failed'
            row.updated_at = now
            self.store.update_inbound(row)
            raise JprecvError('failed', 'Credit failed.', inbound=row)
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

    def _get(self, *, inbound_id: str = '', e2e: str = '') -> JprecvInbound:
        row = None
        if inbound_id:
            row = self.store.get_inbound(str(inbound_id).strip())
        if row is None and e2e:
            row = self.store.get_by_e2e(normalize_e2e(e2e))
        if row is None:
            raise JprecvError('inbound_not_found', 'Inbound Zengin credit not found.')
        return row

    def _assert_visible(self, row: JprecvInbound, actor: str, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if row.userid and row.userid != actor:
            raise JprecvError('jprecv_forbidden', 'Cannot view another customer Zengin credit.')

    def assign(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        customer_id: str,
        account: Any,
        nickname: str = '',
    ) -> JprecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_UNMATCHED:
            raise JprecvError('not_assignable', 'Only unmatched Zengin credits can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise JprecvError('missing_customer_id', 'Customer id is required to assign.')
        dest = normalize_account(account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise JprecvError('credit_not_allowed', 'Credit accounts cannot receive Zengin credits.')
        if row.proxy:
            existing = self.store.get_key_by_fingerprint(row.fingerprint)
            if existing is None or existing.status == KEY_CANCELLED:
                self.add_key(
                    owner_userid=owner, actor=actor, actor_type=actor_type,
                    nickname=nickname or ('Zengin %s' % (row.proxy_type or 'proxy')),
                    proxy_type=row.proxy_type, proxy=row.proxy, destination_account=dest,
                )
                fresh = self.store.get_inbound(row.inbound_id)
                if fresh is not None and fresh.status != IN_UNMATCHED:
                    return fresh
            elif existing.userid != owner:
                raise JprecvError('key_duplicate', 'Zengin proxy belongs to another customer.')
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
    ) -> JprecvInbound:
        """Zengin claim: customer presents the matching proxy for an unmatched credit."""
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_claim:
                raise JprecvError('jprecv_forbidden', 'Customers cannot claim Zengin credits.')
            owner = actor
        else:
            owner = str(owner_userid or '').strip()
            if not owner:
                raise JprecvError('missing_customer_id', 'Customer id is required to claim.')
        if not proxy:
            raise JprecvError('missing_key', 'Zengin proxy is required to claim.')
        kind, value = normalize_proxy(proxy_type, proxy, required=True)
        fingerprint = proxy_fingerprint(kind, value)
        dest = normalize_account(destination_account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise JprecvError('credit_not_allowed', 'Credit accounts cannot receive Zengin credits.')
        row = None
        if inbound_id:
            row = self._get(inbound_id=inbound_id)
            if row.fingerprint != fingerprint:
                raise JprecvError('not_claimable', 'Presented proxy does not match this credit.')
        else:
            for candidate in self.store.list_inbounds(status=IN_UNMATCHED):
                if candidate.fingerprint == fingerprint:
                    row = candidate
                    break
        if row is None:
            raise JprecvError('inbound_not_found', 'No unmatched Zengin credit for that proxy.')
        if row.status != IN_UNMATCHED:
            raise JprecvError('not_claimable', 'This Zengin credit is not waiting for a claim.')
        if row.expiry_at and self._now() > row.expiry_at:
            row.status = IN_EXPIRED
            row.reason = 'expired'
            row.updated_at = self._now()
            self.store.update_inbound(row)
            raise JprecvError('return_window_closed', 'Zengin claim window has closed.')
        existing = self.store.get_key_by_fingerprint(fingerprint)
        if existing is None or existing.status == KEY_CANCELLED:
            self.add_key(
                owner_userid=owner, actor=actor, actor_type=actor_type,
                nickname=nickname or ('Zengin %s' % kind),
                proxy_type=kind, proxy=value, destination_account=dest,
            )
        elif existing.userid != owner:
            raise JprecvError('key_duplicate', 'Zengin proxy belongs to another customer.')
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

    def override_ofac(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> JprecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_HELD or row.reason != 'ofac':
            raise JprecvError('not_overridable', 'Only OFAC-held Zengin credits can be overridden.')
        row.ofac_overridden = True
        row.ofac_hit = False
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def override_kana(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> JprecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_HELD or row.reason != 'kana':
            raise JprecvError('not_overridable', 'Only kana-held Zengin credits can be overridden.')
        row.kana_overridden = True
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def release(self, *, inbound_id: str, actor: str, actor_type: str) -> JprecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_PENDING:
            raise JprecvError('not_releasable', 'Only dual-control Zengin credits can be released.')
        if actor == row.actor:
            raise JprecvError('same_approver', 'A different employee must release this credit.')
        row.releaser = actor
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def reject(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> JprecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status not in OPEN_IN:
            raise JprecvError('not_rejectable', 'This Zengin credit cannot be rejected.')
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
            raise JprecvError('not_exportable', 'Zengin credit has no End-to-End ID to export.')
        _ = actor
        record = compose_zgn1(
            e2e=row.e2e,
            rail=row.rail,
            proxy_type=row.proxy_type,
            proxy=row.proxy,
            bank=row.recv_bank,
            account_number=row.account_number,
            extra=row.kana if getattr(row, 'kana', '') else '',
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
    ) -> JprecvInbound:
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
    ) -> JprecvInbound:
        self._require_enabled()
        row = self._get(inbound_id=inbound_id)
        self._assert_visible(row, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_return:
                raise JprecvError('jprecv_forbidden', 'Customers cannot return Zengin credits.')
            if actor != row.userid:
                raise JprecvError('jprecv_forbidden', 'Cannot return another customer Zengin credit.')
        return self._return(row, actor=actor, actor_type=actor_type, reason=reason, honor_window=True)

    def _customer_deadline(self, row: JprecvInbound) -> float:
        posted = row.posted_at or row.created_at
        if row.rail in {'zengin', 'bojnet'}:
            day = self.calendar.local_dt(posted).date()
            return self.calendar.session_close_ts(self.calendar.next_business_day(day), rail=row.rail)
        return posted + self.policy.customer_return_seconds

    def _return(
        self,
        row: JprecvInbound,
        *,
        actor: str,
        actor_type: str,
        reason: str,
        honor_window: bool,
    ) -> JprecvInbound:
        now = self._now()
        _ = actor
        if row.status == IN_RETURNED or row.returned_at:
            raise JprecvError('already_returned', 'Zengin credit already returned.')
        if row.status in OPEN_IN:
            row.status = IN_RETURNED
            row.reason = reason or 'cust'
            row.returned_at = now
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if row.status != IN_POSTED:
            raise JprecvError('not_returnable', 'This Zengin credit cannot be returned.')
        if honor_window and actor_type not in EMPLOYEE_ROLES:
            if now > self._customer_deadline(row):
                raise JprecvError('return_window_closed', 'Customer Zengin return window has closed.')
        if actor_type in EMPLOYEE_ROLES:
            elapsed = now - (row.posted_at or row.created_at)
            if elapsed > self.policy.staff_complaint_days * 86400:
                raise JprecvError('med_window_closed', 'Staff complaint window is 90 days.')
        if self.debit_fn is None:
            raise JprecvError('return_failed', 'Debit function is not configured.')
        remark = '%s return %s' % (row.rail, row.e2e[-12:])
        result = self.debit_fn(row.destination_account, row.amount_usd, remark)
        classified = _classify_money_result(result)
        if classified == 'nsf':
            raise JprecvError('nsf', 'Return debit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            raise JprecvError('return_failed', 'Return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.reason = reason or 'cust'
        row.returned_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[JprecvInbound]:
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
            'moretime_cap': money_str(self.policy.moretime_cap),
            'zengin_cap': money_str(self.policy.zengin_cap),
            'bojnet_cap': money_str(self.policy.bojnet_cap),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clearing': 'zgn',
            'currency': 'JPY',
            'fx_jpyusd': rate_str(self.policy.jpyusd),
        }

    def unmatched_snapshot(self) -> Dict[str, Any]:
        rows = self.store.list_inbounds(status=IN_UNMATCHED)
        return {
            'enabled': self.policy.enabled,
            'unmatched': [row.to_dict() for row in rows[:40]],
            'count': len(rows),
        }


_SERVICE: Optional[JprecvService] = None


def set_service(service: Optional[JprecvService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[JprecvService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('JPRECV_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryJprecvStore()
    path = os.environ.get('JPRECV_DB', DEFAULT_STORE_PATH)
    return SqliteJprecvStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[JpClock] = None,
) -> JprecvService:
    if store is None:
        store = default_store()
    return JprecvService(
        JprecvPolicy.from_env(),
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
        'jprecv_forbidden': 403,
        'jprecv_disabled': 403,
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
        'invalid_kana': 400,
        'invalid_zengin': 400,
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
        'moretime_amount_exceeded': 400,
        'zengin_amount_exceeded': 400,
        'bojnet_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_key': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: JprecvError) -> Dict[str, Any]:
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
    except JprecvError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snapshot_for(service: JprecvService, userid: str, actor: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(userid, actor=actor, actor_type=actor_type)


def handle_list(service: JprecvService):
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
    return jsonify({'ZenginIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def handle_unmatched(service: JprecvService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'jprecv_forbidden'}), 403
    snap = service.unmatched_snapshot()
    return jsonify({'UnmatchedZenginIns': snap, 'ZenginIn': snap}), 200


def handle_add_key(service: JprecvService):
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
                raise JprecvError('missing_customer_id', 'Customer id is required.')
        row = service.add_key(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname') or '',
            proxy_type=values.get('proxy_type') or values.get('key_type') or values.get('kind'),
            proxy=values.get('proxy') or values.get('zengin') or values.get('key') or values.get('phone') or values.get('email'),
            destination_account=values.get('destination_account') or values.get('account') or values.get('default_account'),
        )
        return jsonify({
            'message': 'Zengin proxy enrolled',
            'key': row.to_dict(),
            'ZenginIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _key_status_route(service: JprecvService, status: str, message: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        key_id = str(values.get('key_id') or '').strip()
        if not key_id:
            raise JprecvError('missing_key', 'key_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.set_key_status(key_id=key_id, actor=userid, actor_type=actor_type, status=status)
        return jsonify({
            'message': message,
            'key': row.to_dict(),
            'ZenginIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: JprecvService):
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
        return jsonify({'preview': preview, 'ZenginIn': _snapshot_for(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest(service: JprecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'Zengin ingested' if created else 'Zengin already ingested',
            'inbound': row.to_dict(),
            'ZenginIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_ingest_file(service: JprecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        payload = values.get('file') or values.get('body') or values.get('payload')
        if not payload:
            raise JprecvError('missing_file', 'ZGN1 file body is required.')
        actor_type = session.get('usertype') or 'customer'
        rows, created = service.ingest_file(
            actor=userid, actor_type=actor_type, payload=str(payload),
            sender_name=str(values.get('name') or values.get('sender_name') or ''),
            send_bank=str(values.get('send_bank') or values.get('sender_bank') or ''),
        )
        owner = rows[-1].userid if rows and rows[-1].userid else userid
        return jsonify({
            'message': 'ZGN1 file ingested',
            'created': created,
            'inbounds': [row.to_dict() for row in rows],
            'ZenginIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_assign(service: JprecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise JprecvError('missing_inbound', 'inbound_id is required.')
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
            'message': 'Zengin assigned',
            'inbound': row.to_dict(),
            'ZenginIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_claim(service: JprecvService):
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
            proxy=values.get('proxy') or values.get('zengin') or values.get('key') or values.get('phone') or values.get('email'),
            destination_account=values.get('destination_account') or values.get('account'),
            nickname=str(values.get('nickname') or ''),
            owner_userid=str(values.get('customer_id') or ''),
        )
        return jsonify({
            'message': 'Zengin claimed',
            'inbound': row.to_dict(),
            'ZenginIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_inbound_route(service: JprecvService, action: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise JprecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        if action == 'override':
            row = service.override_ofac(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'kana':
            row = service.override_kana(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Kana hold overridden'
        elif action == 'release':
            row = service.release(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            message = 'Zengin released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Zengin rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=str(values.get('reason') or 'cust'),
            )
            message = 'Zengin returned'
        elif action == 'export':
            exported = service.export(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            owner = exported['inbound'].get('userid') or userid
            return jsonify({
                'message': 'ZGN1 exported',
                'file': exported['file'],
                'inbound': exported['inbound'],
                'ZenginIn': _snapshot_for(service, owner, userid, actor_type),
            }), 200
        else:
            raise JprecvError('invalid_status', 'Unknown Zengin action.')
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'ZenginIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: JprecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise JprecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.request_return(
            inbound_id=inbound_id, actor=userid, actor_type=actor_type,
            reason=str(values.get('reason') or 'cust'),
        )
        return jsonify({
            'message': 'Zengin return requested',
            'inbound': row.to_dict(),
            'ZenginIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: JprecvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'ZenginIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def attach_jprecv_routes(app, service: JprecvService) -> None:
    @app.route('/listZenginIns', methods=['POST', 'GET'])
    def list_zengin_ins_route():
        return handle_list(service)

    @app.route('/listZenginRecvKeys', methods=['POST', 'GET'])
    def list_zengin_recv_keys_route():
        return handle_list(service)

    @app.route('/listUnmatchedZenginIns', methods=['POST', 'GET'])
    def list_unmatched_zengin_ins_route():
        return handle_unmatched(service)

    @app.route('/addZenginRecvKey', methods=['POST', 'GET'])
    def add_zengin_recv_key_route():
        return handle_add_key(service)

    @app.route('/pauseZenginRecvKey', methods=['POST', 'GET'])
    def pause_zengin_recv_key_route():
        return _key_status_route(service, KEY_PAUSED, 'Zengin proxy paused')

    @app.route('/resumeZenginRecvKey', methods=['POST', 'GET'])
    def resume_zengin_recv_key_route():
        return _key_status_route(service, KEY_ACTIVE, 'Zengin proxy resumed')

    @app.route('/cancelZenginRecvKey', methods=['POST', 'GET'])
    def cancel_zengin_recv_key_route():
        return _key_status_route(service, KEY_CANCELLED, 'Zengin proxy cancelled')

    @app.route('/previewZenginIn', methods=['POST', 'GET'])
    def preview_zengin_in_route():
        return handle_preview(service)

    @app.route('/ingestZenginIn', methods=['POST', 'GET'])
    def ingest_zengin_in_route():
        return handle_ingest(service)

    @app.route('/ingestZenginInFile', methods=['POST', 'GET'])
    def ingest_zengin_in_file_route():
        return handle_ingest_file(service)

    @app.route('/claimZenginIn', methods=['POST', 'GET'])
    def claim_zengin_in_route():
        return handle_claim(service)

    @app.route('/assignZenginIn', methods=['POST', 'GET'])
    def assign_zengin_in_route():
        return handle_assign(service)

    @app.route('/overrideZenginInOfac', methods=['POST', 'GET'])
    def override_zengin_in_ofac_route():
        return _staff_inbound_route(service, 'override')

    @app.route('/overrideZenginInKana', methods=['POST', 'GET'])
    def override_zengin_in_kana_route():
        return _staff_inbound_route(service, 'kana')

    @app.route('/releaseZenginIn', methods=['POST', 'GET'])
    def release_zengin_in_route():
        return _staff_inbound_route(service, 'release')

    @app.route('/rejectZenginIn', methods=['POST', 'GET'])
    def reject_zengin_in_route():
        return _staff_inbound_route(service, 'reject')

    @app.route('/returnZenginIn', methods=['POST', 'GET'])
    def return_zengin_in_route():
        return _staff_inbound_route(service, 'return')

    @app.route('/exportZenginIn', methods=['POST', 'GET'])
    def export_zengin_in_route():
        return _staff_inbound_route(service, 'export')

    @app.route('/requestZenginInReturn', methods=['POST', 'GET'])
    def request_zengin_in_return_route():
        return handle_request_return(service)

    @app.route('/runDueZenginIns', methods=['POST', 'GET'])
    def run_due_zengin_ins_route():
        return handle_run_due(service)
