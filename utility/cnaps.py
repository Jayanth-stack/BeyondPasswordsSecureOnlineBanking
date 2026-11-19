"""China CNAPS IBPS / BEPS / CIPS origination with Chinese name inquiry.

Customers send CNY via a 12-digit CNAPS 联行号 (ISO 7064 MOD 11,10) +
10-19 digit account, optional +86 1XXXXXXXXXX mobile, 18-digit resident
ID, or email over IBPS (24/7), BEPS (09:00-16:00 CST banking days), or
CIPS RTGS (08:30-17:00 CST).
Independent of domestic Fedwire (PR #73), ACH linking (PR #68), and the
unmerged Japan Zengin / Korea KFTC / Thailand PromptPay / Malaysia
DuitNow / Philippines InstaPay / HK FPS / PayNow rails.
Existing `/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay
unchanged. `Customers.debit_request` / `credit_request` still write
`debited` / `direct deposited` unless a remark is supplied here.

Foundations (reusable beyond this screen):
- 12-digit CNAPS code (3 bank + 4 region + 4 branch + ISO 7064 MOD 11,10)
- Account type 1 ordinary / 2 current / 4 savings
- Chinese 户名 (NFC Han, 2-80 characters, mask 张伟 → 张***伟)
- ISO-style CN phone (+86 1XXXXXXXXXX), 18-digit resident ID, and email proxy
- Chinese name inquiry (`match` / `close_match` / `no_match` / `unavailable`)
- Resident ID (GB 11643 weights 7-9-10-5-8-4-2-1-6-3-7-9-10-5-8-4-2, X check)
- IBPS / BEPS / CIPS End-to-End ID (`IBPS`/`BEPS`/`CIPS` + YYYYMMDD + seq)
- CN1 pipe clearing (XML/DOCTYPE and non-CNAPS clearing rejected)
- CnClock in CST (UTC+8): IBPS 24/7; BEPS 09:00-16:00;
  CIPS 08:30-17:00; PBOC holidays + one weekday after a weekend cluster
  (Spring Festival 2024-02-10 Sat → BEPS value date 2024-02-19)
- CNYUSD quote book converting instructed amount to USD debit
- Dual-control at CNY 50,000; IBPS cap 50k / BEPS 1m / CIPS 100m

Reuses Fedwire OFAC screening, money parsing, fee helper, and account
directory from `utility.wire`. Stores are pluggable (memory for tests,
sqlite WAL for restart-safe default). Full account, Chinese name, fingerprint,
phone, email, resident ID, and E2E ID never appear unmasked in to_dict /
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
import unicodedata
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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
PN_HELD = 'held'
PN_QUEUED = 'queued'
PN_PENDING = 'pending_release'
PN_SENT = 'sent'
PN_COMPLETED = 'completed'
PN_REJECTED = 'rejected'
PN_CANCELLED = 'cancelled'
PN_RECALLED = 'recalled'
PN_NSF = 'nsf'
PN_FAILED = 'failed'
PN_STATUSES = frozenset({
    PN_HELD, PN_QUEUED, PN_PENDING, PN_SENT, PN_COMPLETED, PN_REJECTED,
    PN_CANCELLED, PN_RECALLED, PN_NSF, PN_FAILED,
})
OPEN_PN = frozenset({PN_HELD, PN_QUEUED, PN_PENDING, PN_SENT})
CANCELABLE = frozenset({PN_HELD, PN_QUEUED, PN_PENDING})
FEE_NONE = 'none'
FEE_COLLECTED = 'collected'
FEE_WAIVED = 'waived'
FEE_NSF = 'nsf'
PURPOSES = frozenset({'family', 'goods', 'payroll', 'tax', 'loan', 'rent', 'other'})
PURPOSE_ALIASES = {
    'personal': 'family', 'gift': 'family', 'support': 'family',
    'invoice': 'goods', 'purchase': 'goods', 'vendor': 'goods',
    'salary': 'payroll', 'wage': 'payroll',
    'rd': 'tax', 'taxes': 'tax', 'revenue': 'tax',
    'mortgage': 'loan', 'housing': 'rent',
}

RAILS = frozenset({'ibps', 'beps', 'cips'})
RAIL_ALIASES = {
    'ibps': 'ibps', 'instant': 'ibps', '24/7': 'ibps', 'cnaps': 'ibps',
    'internet': 'ibps', 'ibpsplus': 'ibps', 'fast': 'ibps',
    'beps': 'beps', 'bulk': 'beps', 'batch': 'beps', 'ach': 'beps', 'small': 'beps',
    'cips': 'cips', 'rtgs': 'cips', 'hvps': 'cips', 'cross_border': 'cips',
    'crossborder': 'cips', 'pboc': 'cips',
}
ACCOUNT_TYPES = frozenset({'1', '2', '4'})
ACCOUNT_TYPE_ALIASES = {
    'ordinary': '1', 'demand': '1', 'savings_ord': '1',
    'current': '2', 'checking': '2',
    'savings': '4',
}
PROXY_TYPES = frozenset({'cnaps', 'phone', 'nid', 'email'})
PROXY_ALIASES = {
    'account': 'cnaps', 'bank': 'cnaps', 'acct': 'cnaps', 'instapay': 'cnaps',
    'insta': 'cnaps', 'cnaps': 'cnaps', 'lianhang': 'cnaps',
    'mobile': 'phone', 'msisdn': 'phone', 'tel': 'phone', 'sms': 'phone',
    'mail': 'email', 'e_mail': 'email',
    'nid': 'nid', 'national_id': 'nid', 'id': 'nid', 'idcard': 'nid',
    'resident': 'nid', 'shenfenzheng': 'nid', 'citizen': 'nid', 'tin': 'nid',
}
CHINESE_MATCH = 'match'
CHINESE_CLOSE = 'close_match'
CHINESE_NONE = 'no_match'
CHINESE_UNAVAILABLE = 'unavailable'
CHINESE_OUTCOMES = frozenset({CHINESE_MATCH, CHINESE_CLOSE, CHINESE_NONE, CHINESE_UNAVAILABLE})
CLEARING_BY_RAIL = {'ibps': 'IBPS', 'beps': 'BEPS', 'cips': 'CIPS'}
REJECTED_CLEARING = frozenset({
    'AUNPP', 'USRTPP', 'FEDNOW', 'FEDWIRE', 'ACH', 'UPI', 'CHIPS', 'SWIFT',
    'PIX', 'SPEI', 'NPP', 'OSKO', 'FAST', 'PAYNOW', 'MEPS', 'GIRO', 'PNW',
    'FPS', 'FPS1', 'CHATS', 'ECG', 'HKFPS', 'HKID', 'CHAPS', 'BACS', 'SEPA',
    'ZGN', 'KFT1', 'MTS', 'BOJ', 'BOJNET', 'ZENGIN', 'MORETIME',
    'OPEN', 'EFTC', 'BOKW', 'KFTC', 'PPAY', 'SMRT', 'BAHT', 'PP1', 'SMART',
    'DUIT', 'IBGX', 'RNTS', 'DN1', 'BAHTNET', 'PROMPTPAY', 'IBG', 'RENTAS',
    'INST', 'PESO', 'PASS', 'IP1', 'INSTAPAY', 'PESONET', 'PHILPASS',
})
DEFAULT_BANKS = {
    '102': 'ICBC',
    '103': 'ABC',
    '104': 'BOC',
    '105': 'CCB',
    '301': 'BOCOM',
    '308': 'CMB',
    '309': 'CIB',
    '310': 'SPDB',
    '403': 'PSBC',
}
CNAPS_PASS = frozenset({
    ('104100000004', '6222021234567890'),
    ('102100099996', '9876543210987654'),
    ('105100000017', '1111222233334444'),
})
DEFAULT_CNAPS_DIRECTORY = {
    'cnaps:1041000000046222021234567890': '张伟',
    'phone:+8613812345678': '张伟',
    'nid:11010119900307002X': '张伟',
    'email:zhang@example.cn': '张伟',
    'cnaps:1021000999969876543210987654': '李娜',
}
CN1_VERSION = '1'
CN1_FIELDS = 14
DEFAULT_STORE_PATH = 'SystemLogs/cnaps.sqlite'
DEFAULT_SOURCE_BANK = '104100000004'
DEFAULT_SOURCE_BRANCH = '000'
STAFF_COMPLAINT_DAYS = 90
CST_OFFSET_MINUTES = 8 * 60
DEFAULT_CNYUSD = Decimal('0.137000')
BLOCKED_E2E_PREFIXES = (
    'IMAD', 'UETR', 'NPP', 'AUNPP', 'RRN', 'STAN', 'ARN', 'RFI', 'RFR', 'RTR',
    'PNW', 'FAST', 'MEPS', 'GIRO', 'PIX', 'SPEI', 'UPI', 'FPS1', 'CHATS', 'ECG',
    'HKFPS', 'HKID', 'PAYNOW', 'FPS', 'CHAPS', 'BACS', 'FEDWIRE', 'FEDNOW',
    'MTS', 'ZGN', 'BOJ', 'KFT1', 'MORETIME', 'ZENGIN', 'OPEN', 'EFTC', 'BOKW',
    'KFTC', 'PPAY', 'SMRT', 'BAHT', 'PP1', 'SMART', 'DUIT', 'IBGX', 'RNTS', 'DN1',
    'INST', 'PESO', 'PASS', 'IP1', 'INSTAPAY', 'PESONET', 'PHILPASS',
)
E2E_PREFIX = {'ibps': 'IBPS', 'beps': 'BEPS', 'cips': 'CIPS'}


class CnapsError(ValueError):
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
        raise CnapsError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise CnapsError('invalid_name', 'Legal name must be 2-80 characters.')
    return text.upper()


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or '').strip().lower()
    text = PURPOSE_ALIASES.get(text, text)
    if not text:
        return default
    if text not in PURPOSES:
        raise CnapsError('invalid_purpose', 'Unknown payment purpose.')
    return text


def normalize_rail(value: Any, *, default: str = 'ibps') -> str:
    text = str(value or '').strip().lower().replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if not text:
        text = default
    if text not in RAILS:
        raise CnapsError('invalid_rail', 'Rail must be ibps, beps, or cips.')
    return text


def normalize_currency(value: Any, *, default: str = 'CNY', rail: str = 'ibps') -> str:
    _ = rail
    text = str(value or default or 'CNY').strip().upper()
    if text in {'RMB', 'RENMINBI', 'YUAN', 'CNH', 'CN'}:
        text = 'CNY'
    if text != 'CNY':
        raise CnapsError('invalid_currency', 'IBPS / BEPS / CIPS is CNY only.')
    return text


def normalize_proxy_type(value: Any, *, default: str = '') -> str:
    text = str(value or '').strip().lower().replace('-', '_')
    text = PROXY_ALIASES.get(text, text)
    if not text:
        return default
    if text not in PROXY_TYPES:
        raise CnapsError('invalid_proxy', 'Proxy must be cnaps, phone, nid, or email.')
    return text


def normalize_account_type(value: Any, *, default: str = '1') -> str:
    text = str(value or '').strip().lower()
    text = ACCOUNT_TYPE_ALIASES.get(text, text)
    if not text:
        text = default
    if text not in ACCOUNT_TYPES:
        raise CnapsError('invalid_account_type', 'Account type must be 1, 2, or 4.')
    return text


def cnaps_check_digit(first11: str) -> str:
    """ISO 7064 MOD 11,10 check digit for a 12-digit CNAPS 联行号."""
    if len(first11) != 11 or not first11.isdigit():
        raise CnapsError('invalid_bank', 'CNAPS code must be twelve digits.')
    running = 10
    for ch in first11:
        remainder = (int(ch) + running) % 10
        if remainder == 0:
            remainder = 10
        running = (2 * remainder) % 11
    return str((11 - running) % 10)


def cnaps_check_digit_ok(digits: str) -> bool:
    if len(digits) != 12 or not digits.isdigit():
        return False
    if digits == '000000000000':
        return False
    return cnaps_check_digit(digits[:11]) == digits[11]


def normalize_bank_code(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise CnapsError('invalid_bank', 'CNAPS code is required.')
        return ''
    if len(digits) != 12:
        raise CnapsError('invalid_bank', 'CNAPS code must be 12 digits and not 000000000000.')
    if digits == '000000000000' or not cnaps_check_digit_ok(digits):
        raise CnapsError('invalid_bank', 'CNAPS code failed checksum.')
    return digits


def normalize_branch_code(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise CnapsError('invalid_branch', 'CNAPS branch must be 3-4 digits.')
        return ''
    if len(digits) > 4:
        raise CnapsError('invalid_branch', 'CNAPS branch must be 3-4 digits.')
    return digits.zfill(3)


def normalize_cnaps_account(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise CnapsError('invalid_cnaps_account', 'Chinese account number is required.')
        return ''
    if not (10 <= len(digits) <= 19):
        raise CnapsError('invalid_cnaps_account', 'Chinese account must be 10-19 digits.')
    return digits


def cnaps_ok(bank: str, account: str, branch: str = '') -> bool:
    _ = branch
    if (bank, account) in CNAPS_PASS:
        return True
    if not cnaps_check_digit_ok(bank):
        return False
    if not (account.isdigit() and 10 <= len(account) <= 19):
        return False
    return True


def bank_name(value: Any, *, directory: Optional[Dict[str, str]] = None) -> str:
    code = ''.join(ch for ch in str(value or '') if ch.isdigit())
    table = directory or DEFAULT_BANKS
    return table.get(code[:3], '')


def format_cnaps(bank: str, account: str, branch: str = '') -> str:
    _ = branch
    if not (bank and account):
        return ''
    return '%s-****%s' % (bank[:3], account[-4:])


def resident_id_ok(digits: str) -> bool:
    """GB 11643 18-digit resident ID: 17-digit body + 1-0-X check, valid YYYYMMDD."""
    text = str(digits or '').strip().upper().replace(' ', '')
    if len(text) != 18:
        return False
    body, check = text[:17], text[17]
    if not body.isdigit() or check not in '0123456789X':
        return False
    try:
        datetime.strptime(body[6:14], '%Y%m%d')
    except ValueError:
        return False
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    mapping = '10X98765432'
    total = sum(int(digit) * weight for digit, weight in zip(body, weights))
    return mapping[total % 11] == check


def normalize_resident_id(value: Any, *, required: bool = False) -> str:
    text = str(value or '').strip().upper().replace(' ', '').replace('-', '')
    digits = ''.join(ch for ch in text if ch.isdigit() or ch == 'X')
    if not digits:
        if required:
            raise CnapsError('invalid_resident_id', 'Chinese resident ID is required.')
        return ''
    if not resident_id_ok(digits):
        raise CnapsError('invalid_resident_id', 'Chinese resident ID failed checksum.')
    return digits


def _han_chars(text: str) -> List[str]:
    return [ch for ch in text if '一' <= ch <= '鿿']


def normalize_chinese(value: Any, *, required: bool = True) -> str:
    text = unicodedata.normalize('NFC', str(value or ''))
    text = ' '.join(text.replace('　', ' ').split())
    if not text:
        if required:
            raise CnapsError('invalid_chinese', 'Chinese beneficiary name is required.')
        return ''
    han = _han_chars(text)
    if required and len(han) < 2:
        raise CnapsError('invalid_chinese', 'Chinese name must contain at least two Han characters.')
    if len(text) > 80:
        raise CnapsError('invalid_chinese', 'Chinese name must be 2-80 characters.')
    return text


def mask_chinese(value: Any) -> str:
    """Mask using first and last Han so 张伟 → 张***伟, not a trailing Latin letter."""
    text = str(value or '')
    han = _han_chars(text)
    if han:
        if len(han) == 1:
            return han[0] + '***'
        return han[0] + '***' + han[-1]
    if len(text) <= 2:
        return text[:1] + '***' if text else ''
    return text[0] + '***' + text[-1]


def mask_account(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '*' * len(digits)
    return ('*' * (len(digits) - 4)) + digits[-4:]


def normalize_phone(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise CnapsError('invalid_phone', 'Mobile number is required.')
        return ''
    if digits.startswith('86') and len(digits) >= 13:
        national = digits[2:]
    elif digits.startswith('0') and len(digits) == 12 and digits[1] == '1':
        national = digits[1:]
    else:
        national = digits
    if not (len(national) == 11 and national[0] == '1' and national[1] in '3456789'):
        raise CnapsError('invalid_phone', 'China mobile must be +86 1XXXXXXXXXX.')
    return '+86' + national


def mask_phone(value: Any) -> str:
    text = str(value or '')
    if len(text) < 4:
        return text
    return '+86****' + text[-4:]


def normalize_email(value: Any, *, required: bool = False) -> str:
    text = str(value or '').strip().lower()
    if not text:
        if required:
            raise CnapsError('invalid_email', 'Email is required.')
        return ''
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', text):
        raise CnapsError('invalid_email', 'Email is not valid.')
    return text


def mask_email(value: Any) -> str:
    text = str(value or '')
    if '@' not in text:
        return text
    local, _, domain = text.partition('@')
    head = local[:1] if local else '*'
    return '%s***@%s' % (head, domain)


def normalize_proxy(proxy_type: Any, value: Any, *, required: bool = False) -> Tuple[str, str]:
    kind = normalize_proxy_type(proxy_type, default='')
    if not kind:
        if required:
            raise CnapsError('invalid_proxy', 'Proxy type is required.')
        return '', ''
    if kind == 'phone':
        return kind, normalize_phone(value, required=True)
    if kind == 'email':
        return kind, normalize_email(value, required=True)
    if kind == 'nid':
        return kind, normalize_resident_id(value, required=True)
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not (22 <= len(digits) <= 31):
        raise CnapsError('invalid_proxy', 'CNAPS proxy must be 12-digit code + account (22-31 digits).')
    bank, account = digits[:12], digits[12:]
    if not cnaps_ok(bank, account):
        raise CnapsError('invalid_cnaps', 'CNAPS bank/account is not valid.')
    return kind, digits


def mask_proxy(proxy_type: str, value: str) -> str:
    if proxy_type == 'phone':
        return mask_phone(value)
    if proxy_type == 'email':
        return mask_email(value)
    if proxy_type == 'nid':
        return mask_account(value)
    if proxy_type == 'cnaps' and len(value) >= 16:
        return '%s-****%s' % (value[:3], value[-4:])
    return mask_account(value)


def proxy_key(kind: str, value: str) -> str:
    return '%s:%s' % (kind, value)


def fingerprint_for(userid: str, bank: str, branch: str, account: str, proxy: str) -> str:
    material = '|'.join((userid, bank, branch, account, proxy))
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def mask_e2e(value: Any) -> str:
    text = str(value or '')
    if len(text) < 8:
        return text
    return text[:4] + '****' + text[-4:]


def compose_e2e(rail: str, cycle_date: str, sequence: int) -> str:
    kind = normalize_rail(rail)
    if not re.match(r'^\d{8}$', cycle_date):
        raise CnapsError('invalid_e2e', 'Cycle date must be YYYYMMDD.')
    prefix = E2E_PREFIX[kind]
    if any(cycle_date.startswith(blocked) for blocked in BLOCKED_E2E_PREFIXES):
        raise CnapsError('invalid_e2e', 'Reserved end-to-end prefix.')
    return '%s%s%06d' % (prefix, cycle_date, int(sequence))


def compose_cn1(
    *,
    rail: str,
    amount: str,
    bank_code: str,
    branch_code: str,
    account_last4: str,
    e2e_id: str,
    purpose: str,
    value_date: str,
    chinese_mask: str,
    ofac_hit: bool,
    sequence: int,
    source_bank: str = DEFAULT_SOURCE_BANK,
    currency: str = 'CNY',
) -> str:
    kind = normalize_rail(rail)
    clearing = CLEARING_BY_RAIL[kind]
    fields = [
        CN1_VERSION,
        clearing,
        currency,
        money_str(parse_money(amount, allow_zero=True)),
        bank_code,
        branch_code,
        account_last4,
        e2e_id,
        purpose,
        value_date,
        chinese_mask or '****',
        'Y' if ofac_hit else 'N',
        str(int(sequence)),
        source_bank,
    ]
    return '|'.join(fields)


def parse_cn1(payload: Any) -> Dict[str, str]:
    text = str(payload or '').strip()
    upper = text.upper()
    if '<!DOCTYPE' in upper or '<?XML' in upper or '<KFT' in upper or '<OPEN' in upper or '<BOK' in upper or '<PPAY' in upper or '<SMART' in upper or '<BAHT' in upper or '<ZGN' in upper or '<INST' in upper or '<PESONET' in upper or '<PHILPASS' in upper or '<DUIT' in upper or '<IBG' in upper or '<CNAPS' in upper or '<CIPS' in upper:
        raise CnapsError('invalid_cn1', 'XML/DOCTYPE CNAPS payloads are rejected.')
    parts = text.split('|')
    if len(parts) != CN1_FIELDS:
        raise CnapsError('invalid_cn1', 'CN1 must have 14 pipe fields.')
    clearing = parts[1].strip().upper()
    if clearing in REJECTED_CLEARING:
        raise CnapsError('invalid_clearing', 'Clearing %s is not a CNAPS rail.' % clearing)
    if clearing not in {'IBPS', 'BEPS', 'CIPS'}:
        raise CnapsError('invalid_clearing', 'Unknown CNAPS clearing.')
    currency = parts[2].strip().upper()
    if currency != 'CNY':
        raise CnapsError('invalid_currency', 'CN1 is CNY only.')
    return {
        'version': parts[0],
        'clearing': clearing,
        'currency': currency,
        'amount': parts[3],
        'bank_code': parts[4],
        'branch_code': parts[5],
        'account_last4': parts[6],
        'e2e_id': parts[7],
        'purpose': parts[8],
        'value_date': parts[9],
        'chinese_mask': parts[10],
        'ofac': parts[11],
        'sequence': parts[12],
        'source_bank': parts[13],
    }


def chinese_outcome(registered: str, presented: str) -> str:
    left = normalize_chinese(registered, required=False)
    right = normalize_chinese(presented, required=False)
    if not left or not right:
        return CHINESE_UNAVAILABLE
    if left == right:
        return CHINESE_MATCH
    if left.startswith(right) or right.startswith(left):
        return CHINESE_CLOSE
    left_han = ''.join(_han_chars(left))
    right_han = ''.join(_han_chars(right))
    if len(left_han) >= 1 and len(right_han) >= 1 and left_han[0] == right_han[0]:
        return CHINESE_CLOSE
    return CHINESE_NONE


@dataclass
class ChineseResolution:
    outcome: str
    registered_name: str
    presented_name: str
    proxy_type: str
    proxy_mask: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'outcome': self.outcome,
            'registered_mask': mask_chinese(self.registered_name),
            'presented_mask': mask_chinese(self.presented_name),
            'proxy_type': self.proxy_type,
            'proxy_mask': self.proxy_mask,
        }


def resolve_cnaps(
    proxy_type: Any,
    proxy: Any,
    presented_name: Any = '',
    *,
    bank_code: Any = '',
    branch_code: Any = '',
    account_number: Any = '',
    directory: Optional[Dict[str, str]] = None,
) -> ChineseResolution:
    table = directory or DEFAULT_CNAPS_DIRECTORY
    kind = normalize_proxy_type(proxy_type, default='')
    value = ''
    if kind:
        kind, value = normalize_proxy(kind, proxy, required=True)
        key = proxy_key(kind, value)
        mask = mask_proxy(kind, value)
    else:
        bank = normalize_bank_code(bank_code, required=False)
        branch = normalize_branch_code(branch_code, required=False)
        account = normalize_cnaps_account(account_number, required=False)
        if not (bank and account):
            return ChineseResolution(CHINESE_UNAVAILABLE, '', str(presented_name or ''), '', '')
        key = proxy_key('cnaps', bank + account)
        mask = format_cnaps(bank, account, branch)
        kind = 'cnaps'
        value = bank + account
    registered = table.get(key, '')
    presented = str(presented_name or '')
    if not registered:
        return ChineseResolution(CHINESE_UNAVAILABLE, '', presented, kind, mask)
    outcome = chinese_outcome(registered, presented)
    return ChineseResolution(outcome, registered, presented, kind, mask)


FX_QUANTUM = Decimal('0.000001')


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
    usd = (amount * rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    return usd


SPRING_FESTIVAL = {
    2024: (date(2024, 2, 10), date(2024, 2, 17)),
    2025: (date(2025, 1, 28), date(2025, 2, 4)),
    2026: (date(2026, 2, 15), date(2026, 2, 23)),
}


def cn_holidays(year: int) -> set:
    """PBOC-style public holidays. Spring Festival is a dated cluster."""
    raw = {
        date(year, 1, 1),
        date(year, 4, 4),
        date(year, 5, 1),
        date(year, 10, 1),
        date(year, 10, 2),
        date(year, 10, 3),
        date(year, 10, 4),
        date(year, 10, 5),
        date(year, 10, 6),
        date(year, 10, 7),
    }
    span = SPRING_FESTIVAL.get(year)
    if span:
        start, end = span
        cursor = start
        while cursor <= end:
            raw.add(cursor)
            cursor += timedelta(days=1)
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


class CnClock:
    def __init__(
        self,
        *,
        beps_open_hour: int = 9,
        beps_open_minute: int = 0,
        beps_cutoff_hour: int = 16,
        beps_cutoff_minute: int = 0,
        cips_open_hour: int = 8,
        cips_open_minute: int = 30,
        cips_cutoff_hour: int = 17,
        cips_cutoff_minute: int = 0,
        tz_offset_minutes: int = CST_OFFSET_MINUTES,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.beps_open_hour = beps_open_hour
        self.beps_open_minute = beps_open_minute
        self.beps_cutoff_hour = beps_cutoff_hour
        self.beps_cutoff_minute = beps_cutoff_minute
        self.cips_open_hour = cips_open_hour
        self.cips_open_minute = cips_open_minute
        self.cips_cutoff_hour = cips_cutoff_hour
        self.cips_cutoff_minute = cips_cutoff_minute
        self.tz_offset_minutes = tz_offset_minutes
        self.extra_holidays = tuple(extra_holidays)

    def local_dt(self, ts: float) -> datetime:
        tz = timezone(timedelta(minutes=self.tz_offset_minutes))
        return datetime.fromtimestamp(ts, tz=tz)

    def is_weekend(self, day: date) -> bool:
        return day.weekday() >= 5

    def is_holiday(self, day: date) -> bool:
        if day in cn_holidays(day.year):
            return True
        iso = day.isoformat()
        return iso in self.extra_holidays

    def is_business_day(self, day: date) -> bool:
        return not self.is_weekend(day) and not self.is_holiday(day)

    def next_business_day(self, day: date) -> date:
        cursor = day
        for _ in range(21):
            cursor += timedelta(days=1)
            if self.is_business_day(cursor):
                return cursor
        return day + timedelta(days=1)

    def _window(self, rail: str) -> Tuple[int, int, int, int]:
        if rail == 'cips':
            return (
                self.cips_open_hour, self.cips_open_minute,
                self.cips_cutoff_hour, self.cips_cutoff_minute,
            )
        return (
            self.beps_open_hour, self.beps_open_minute,
            self.beps_cutoff_hour, self.beps_cutoff_minute,
        )

    def is_after_cutoff(self, ts: float, *, rail: str = 'ibps') -> bool:
        kind = normalize_rail(rail)
        if kind == 'ibps':
            return False
        local = self.local_dt(ts)
        day = local.date()
        if not self.is_business_day(day):
            return True
        open_h, open_m, cut_h, cut_m = self._window(kind)
        minutes = local.hour * 60 + local.minute
        return minutes < open_h * 60 + open_m or minutes >= cut_h * 60 + cut_m

    def value_date(self, ts: float, *, rail: str = 'ibps') -> date:
        kind = normalize_rail(rail)
        local = self.local_dt(ts)
        day = local.date()
        if kind == 'ibps':
            return day
        if not self.is_business_day(day) or self.is_after_cutoff(ts, rail=kind):
            return self.next_business_day(day)
        return day

    def cycle_date(self, ts: float, *, rail: str = 'ibps') -> str:
        return self.value_date(ts, rail=rail).strftime('%Y%m%d')

    def snapshot(self, ts: float, *, rail: str = 'ibps') -> Dict[str, Any]:
        kind = normalize_rail(rail)
        local = self.local_dt(ts)
        after = self.is_after_cutoff(ts, rail=kind)
        value = self.value_date(ts, rail=kind)
        if kind == 'cips':
            cutoff = '%02d:%02d' % (self.cips_cutoff_hour, self.cips_cutoff_minute)
        elif kind == 'beps':
            cutoff = '%02d:%02d' % (self.beps_cutoff_hour, self.beps_cutoff_minute)
        else:
            cutoff = '24/7'
        return {
            'rail': kind,
            'local': local.strftime('%Y-%m-%d %H:%M'),
            'value_date': value.isoformat(),
            'after_cutoff': after,
            'cutoff': cutoff,
            'business_day': self.is_business_day(local.date()),
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
    raw = os.environ.get(name, default)
    try:
        return parse_money(raw, allow_zero=True)
    except AmountError:
        return parse_money(default, allow_zero=True)


def _env_rate(name: str, default: str) -> Decimal:
    raw = os.environ.get(name, default)
    try:
        return parse_fx_rate(raw)
    except AmountError:
        return parse_fx_rate(default)


def _env_list(name: str) -> Tuple[str, ...]:
    raw = os.environ.get(name, '')
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
class CnapsPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    allow_credit: bool = False
    require_chinese_match: bool = True
    max_beneficiaries: int = 12
    max_payments: int = 120
    min_amount: Decimal = Decimal('1.00')
    ibps_cap: Decimal = Decimal('50000.00')
    beps_cap: Decimal = Decimal('1000000.00')
    cips_cap: Decimal = Decimal('100000000.00')
    outbound_fee: Decimal = Decimal('0.00')
    cips_fee: Decimal = Decimal('8.00')
    dual_control_threshold: Decimal = Decimal('50000.00')
    cnyusd: Decimal = DEFAULT_CNYUSD
    beps_open_hour: int = 9
    beps_open_minute: int = 0
    beps_cutoff_hour: int = 16
    beps_cutoff_minute: int = 0
    cips_cutoff_hour: int = 17
    cips_cutoff_minute: int = 0
    tz_offset_minutes: int = CST_OFFSET_MINUTES
    source_bank: str = DEFAULT_SOURCE_BANK
    source_branch: str = DEFAULT_SOURCE_BRANCH
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'CnapsPolicy':
        extra = _env_list('CNAPS_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('CNAPS_ENABLED', True),
            customer_manage=_env_bool('CNAPS_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('CNAPS_CUSTOMER_SEND', True),
            allow_credit=_env_bool('CNAPS_ALLOW_CREDIT', False),
            require_chinese_match=_env_bool('CNAPS_REQUIRE_CHINESE', _env_bool('CNAPS_REQUIRE_HAN', True)),
            max_beneficiaries=max(1, _env_int('CNAPS_MAX_BENEFICIARIES', 12)),
            max_payments=max(1, _env_int('CNAPS_MAX_PAYMENTS', 120)),
            min_amount=_env_money('CNAPS_MIN_AMOUNT', '1.00'),
            ibps_cap=_env_money('CNAPS_IBPS_CAP', '50000.00'),
            beps_cap=_env_money('CNAPS_BEPS_CAP', '1000000.00'),
            cips_cap=_env_money('CNAPS_CIPS_CAP', '100000000.00'),
            outbound_fee=_env_money('CNAPS_FEE', '0.00'),
            cips_fee=_env_money('CNAPS_CIPS_FEE', '8.00'),
            dual_control_threshold=_env_money('CNAPS_DUAL_CONTROL', '50000.00'),
            cnyusd=_env_rate('CNAPS_FX_CNYUSD', '0.137000'),
            beps_open_hour=max(0, min(23, _env_int('CNAPS_BEPS_OPEN_HOUR', 9))),
            beps_open_minute=max(0, min(59, _env_int('CNAPS_BEPS_OPEN_MINUTE', 0))),
            beps_cutoff_hour=max(0, min(23, _env_int('CNAPS_BEPS_CUTOFF_HOUR', 16))),
            beps_cutoff_minute=max(0, min(59, _env_int('CNAPS_BEPS_CUTOFF_MINUTE', 0))),
            cips_cutoff_hour=max(0, min(23, _env_int('CNAPS_CIPS_CUTOFF_HOUR', 17))),
            cips_cutoff_minute=max(0, min(59, _env_int('CNAPS_CIPS_CUTOFF_MINUTE', 0))),
            tz_offset_minutes=_env_int('CNAPS_TZ_OFFSET', CST_OFFSET_MINUTES),
            source_bank=normalize_bank_code(os.environ.get('CNAPS_SOURCE_BANK', DEFAULT_SOURCE_BANK), required=False) or DEFAULT_SOURCE_BANK,
            source_branch=normalize_branch_code(os.environ.get('CNAPS_SOURCE_BRANCH', DEFAULT_SOURCE_BRANCH), required=False) or DEFAULT_SOURCE_BRANCH,
            watchlist=watch,
            extra_holidays=_env_list('CNAPS_HOLIDAYS'),
        )

    def cap_for(self, rail: str) -> Decimal:
        if rail == 'cips':
            return self.cips_cap
        if rail == 'beps':
            return self.beps_cap
        return self.ibps_cap

    def fee_for(self, rail: str) -> Decimal:
        if rail == 'cips':
            return self.cips_fee
        return self.outbound_fee

    def rate_for(self, currency: str) -> Decimal:
        _ = currency
        return self.cnyusd


@dataclass
class CnapsBeneficiary:
    beneficiary_id: str
    userid: str
    nickname: str
    legal_name: str
    chinese_name: str
    proxy_type: str
    proxy: str
    bank_code: str
    branch_code: str
    account_number: str
    account_type: str
    default_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float
    fingerprint: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'beneficiary_id': self.beneficiary_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'chinese_mask': mask_chinese(self.chinese_name),
            'proxy_type': self.proxy_type,
            'proxy_mask': mask_proxy(self.proxy_type, self.proxy) if self.proxy_type else '',
            'bank_code': self.bank_code,
            'bank_name': bank_name(self.bank_code),
            'branch_code': self.branch_code,
            'account_last4': last4(self.account_number) if self.account_number else '',
            'account_type': self.account_type,
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
class CnapsPayment:
    payment_id: str
    trace_id: str
    beneficiary_id: str
    userid: str
    internal_account: str
    rail: str
    currency: str
    instructed_amount: str
    usd_amount: str
    fx_rate: str
    fee: str
    fee_status: str
    nickname: str
    legal_name: str
    chinese_mask: str
    proxy_type: str
    proxy_mask: str
    bank_code: str
    branch_code: str
    account_last4: str
    purpose: str
    memo: str
    status: str
    e2e_id: str
    value_date: str
    chinese_outcome: str
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
            'rail': self.rail,
            'currency': self.currency,
            'amount': self.instructed_amount,
            'instructed_amount': self.instructed_amount,
            'usd_amount': self.usd_amount,
            'fx_rate': self.fx_rate,
            'fee': self.fee,
            'fee_status': self.fee_status,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'chinese_mask': self.chinese_mask,
            'proxy_type': self.proxy_type,
            'proxy_mask': self.proxy_mask,
            'bank_code': self.bank_code,
            'branch_code': self.branch_code,
            'account_last4': self.account_last4,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'e2e_id': mask_e2e(self.e2e_id),
            'value_date': self.value_date,
            'chinese_outcome': self.chinese_outcome,
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
            'held': self.status == PN_HELD,
            'queued': self.status == PN_QUEUED,
            'pending_release': self.status == PN_PENDING,
            'sent': self.status == PN_SENT,
            'completed': self.status == PN_COMPLETED,
            'cancelable': self.status in CANCELABLE,
            'instant': self.rail == 'ibps',
        }


def _clone_bene(row: CnapsBeneficiary) -> CnapsBeneficiary:
    return CnapsBeneficiary(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_pay(row: CnapsPayment) -> CnapsPayment:
    return CnapsPayment(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _bene_from_row(row: Any) -> CnapsBeneficiary:
    keys = row.keys() if hasattr(row, 'keys') else []
    return CnapsBeneficiary(
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        chinese_name=row['chinese_name'] if 'chinese_name' in keys else '',
        proxy_type=row['proxy_type'] or '',
        proxy=row['proxy'] or '',
        bank_code=row['bank_code'] or '',
        branch_code=row['branch_code'] or '',
        account_number=row['account_number'] or '',
        account_type=row['account_type'] if 'account_type' in keys else '1',
        default_account=row['default_account'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        fingerprint=row['fingerprint'] or '',
    )


def _pay_from_row(row: Any) -> CnapsPayment:
    keys = row.keys() if hasattr(row, 'keys') else []
    return CnapsPayment(
        payment_id=row['payment_id'],
        trace_id=row['trace_id'],
        beneficiary_id=row['beneficiary_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        rail=row['rail'],
        currency=row['currency'] if 'currency' in keys else 'CNY',
        instructed_amount=row['instructed_amount'],
        usd_amount=row['usd_amount'],
        fx_rate=row['fx_rate'],
        fee=row['fee'],
        fee_status=row['fee_status'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        chinese_mask=row['chinese_mask'] or '',
        proxy_type=row['proxy_type'] or '',
        proxy_mask=row['proxy_mask'] or '',
        bank_code=row['bank_code'] or '',
        branch_code=row['branch_code'] if 'branch_code' in keys else '',
        account_last4=row['account_last4'] or '',
        purpose=row['purpose'],
        memo=row['memo'] or '',
        status=row['status'],
        e2e_id=row['e2e_id'] or '',
        value_date=row['value_date'],
        chinese_outcome=row['chinese_outcome'] or '',
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


class MemoryCnapsStore:
    def __init__(self) -> None:
        self._benes: Dict[str, CnapsBeneficiary] = {}
        self._pays: Dict[str, CnapsPayment] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_beneficiary(self, row: CnapsBeneficiary) -> None:
        with self._lock:
            self._benes[row.beneficiary_id] = _clone_bene(row)

    def get_beneficiary(self, beneficiary_id: str) -> Optional[CnapsBeneficiary]:
        with self._lock:
            row = self._benes.get(beneficiary_id)
            return _clone_bene(row) if row else None

    def update_beneficiary(self, row: CnapsBeneficiary) -> None:
        self.put_beneficiary(row)

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[CnapsBeneficiary]:
        with self._lock:
            rows = list(self._benes.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != BENE_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return [_clone_bene(row) for row in rows]

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[CnapsBeneficiary]:
        wanted = nickname.strip().lower()
        for row in self.list_beneficiaries(userid, include_archived=False):
            if row.status in OPEN_BENE and row.nickname.lower() == wanted:
                return row
        return None

    def find_beneficiary_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[CnapsBeneficiary]:
        for row in self.list_beneficiaries(userid, include_archived=False):
            if row.status in OPEN_BENE and row.fingerprint == fingerprint:
                return row
        return None

    def put_payment(self, row: CnapsPayment) -> CnapsPayment:
        with self._lock:
            existing_id = self._by_trace.get(row.trace_id)
            if existing_id is not None:
                return _clone_pay(self._pays[existing_id])
            self._pays[row.payment_id] = _clone_pay(row)
            self._by_trace[row.trace_id] = row.payment_id
            return _clone_pay(row)

    def update_payment(self, row: CnapsPayment) -> None:
        with self._lock:
            self._pays[row.payment_id] = _clone_pay(row)
            self._by_trace[row.trace_id] = row.payment_id

    def get_payment(self, payment_id: str) -> Optional[CnapsPayment]:
        with self._lock:
            row = self._pays.get(payment_id)
            return _clone_pay(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[CnapsPayment]:
        with self._lock:
            payment_id = self._by_trace.get(trace_id)
            row = self._pays.get(payment_id) if payment_id else None
            return _clone_pay(row) if row else None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[CnapsPayment]:
        with self._lock:
            rows = list(self._pays.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if beneficiary_id is not None:
            rows = [row for row in rows if row.beneficiary_id == beneficiary_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return [_clone_pay(row) for row in rows]

    def next_e2e_sequence(self, cycle_date: str, rail: str) -> int:
        prefix = E2E_PREFIX[normalize_rail(rail)] + cycle_date
        with self._lock:
            n = sum(1 for row in self._pays.values() if (row.e2e_id or '').startswith(prefix))
        return n + 1


class SqliteCnapsStore:
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
                    chinese_name TEXT NOT NULL DEFAULT '',
                    proxy_type TEXT NOT NULL DEFAULT '',
                    proxy TEXT NOT NULL DEFAULT '',
                    bank_code TEXT NOT NULL DEFAULT '',
                    branch_code TEXT NOT NULL DEFAULT '',
                    account_number TEXT NOT NULL DEFAULT '',
                    account_type TEXT NOT NULL DEFAULT '1',
                    default_account TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    fingerprint TEXT NOT NULL DEFAULT ''
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
                    rail TEXT NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'CNY',
                    instructed_amount TEXT NOT NULL,
                    usd_amount TEXT NOT NULL,
                    fx_rate TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    fee_status TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    chinese_mask TEXT NOT NULL DEFAULT '',
                    proxy_type TEXT NOT NULL DEFAULT '',
                    proxy_mask TEXT NOT NULL DEFAULT '',
                    bank_code TEXT NOT NULL DEFAULT '',
                    branch_code TEXT NOT NULL DEFAULT '',
                    account_last4 TEXT NOT NULL DEFAULT '',
                    purpose TEXT NOT NULL,
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    e2e_id TEXT NOT NULL DEFAULT '',
                    value_date TEXT NOT NULL,
                    chinese_outcome TEXT NOT NULL DEFAULT '',
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

    def put_beneficiary(self, row: CnapsBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO beneficiaries (
                    beneficiary_id, userid, nickname, legal_name, chinese_name, proxy_type, proxy,
                    bank_code, branch_code, account_number, account_type, default_account, status, actor,
                    created_at, updated_at, fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.beneficiary_id, row.userid, row.nickname, row.legal_name, row.chinese_name,
                    row.proxy_type, row.proxy, row.bank_code, row.branch_code, row.account_number,
                    row.account_type, row.default_account, row.status, row.actor, row.created_at,
                    row.updated_at, row.fingerprint,
                ),
            )
            conn.commit()

    def get_beneficiary(self, beneficiary_id: str) -> Optional[CnapsBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM beneficiaries WHERE beneficiary_id = ?', (beneficiary_id,),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def update_beneficiary(self, row: CnapsBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE beneficiaries SET nickname=?, legal_name=?, chinese_name=?, proxy_type=?, proxy=?,
                    bank_code=?, branch_code=?, account_number=?, account_type=?, default_account=?, status=?,
                    actor=?, updated_at=?, fingerprint=?
                WHERE beneficiary_id=?
                """,
                (
                    row.nickname, row.legal_name, row.chinese_name, row.proxy_type, row.proxy,
                    row.bank_code, row.branch_code, row.account_number, row.account_type, row.default_account, row.status,
                    row.actor, row.updated_at, row.fingerprint, row.beneficiary_id,
                ),
            )
            conn.commit()

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[CnapsBeneficiary]:
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

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[CnapsBeneficiary]:
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

    def find_beneficiary_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[CnapsBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM beneficiaries
                WHERE userid = ? AND fingerprint = ?
                  AND status IN ('active', 'paused')
                """,
                (userid, fingerprint),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def put_payment(self, row: CnapsPayment) -> CnapsPayment:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM payments WHERE trace_id = ?', (row.trace_id,)
            ).fetchone()
            if existing is not None:
                return _pay_from_row(existing)
            conn.execute(
                """
                INSERT INTO payments (
                    payment_id, trace_id, beneficiary_id, userid, internal_account, rail,
                    currency, instructed_amount, usd_amount, fx_rate, fee, fee_status, nickname, legal_name,
                    chinese_mask, proxy_type, proxy_mask, bank_code, branch_code, account_last4, purpose, memo, status,
                    e2e_id, value_date, chinese_outcome, actor, releaser, ofac_hit, ofac_match,
                    created_at, updated_at, sent_at, completed_at, recalled_at, note, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.payment_id, row.trace_id, row.beneficiary_id, row.userid,
                    row.internal_account, row.rail, row.currency, row.instructed_amount, row.usd_amount,
                    row.fx_rate, row.fee, row.fee_status, row.nickname, row.legal_name,
                    row.chinese_mask, row.proxy_type, row.proxy_mask, row.bank_code, row.branch_code, row.account_last4,
                    row.purpose, row.memo, row.status, row.e2e_id, row.value_date,
                    row.chinese_outcome, row.actor, row.releaser, row.ofac_hit, row.ofac_match,
                    row.created_at, row.updated_at, row.sent_at, row.completed_at,
                    row.recalled_at, row.note, row.reason,
                ),
            )
            conn.commit()
            return row

    def update_payment(self, row: CnapsPayment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE payments SET fee=?, fee_status=?, status=?, e2e_id=?,
                    value_date=?, chinese_outcome=?, actor=?, releaser=?, ofac_hit=?,
                    ofac_match=?, updated_at=?, sent_at=?, completed_at=?,
                    recalled_at=?, note=?, reason=?
                WHERE payment_id=?
                """,
                (
                    row.fee, row.fee_status, row.status, row.e2e_id, row.value_date,
                    row.chinese_outcome, row.actor, row.releaser, row.ofac_hit, row.ofac_match,
                    row.updated_at, row.sent_at, row.completed_at, row.recalled_at,
                    row.note, row.reason, row.payment_id,
                ),
            )
            conn.commit()

    def get_payment(self, payment_id: str) -> Optional[CnapsPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE payment_id = ?', (payment_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[CnapsPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE trace_id = ?', (trace_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[CnapsPayment]:
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

    def next_e2e_sequence(self, cycle_date: str, rail: str) -> int:
        prefix = E2E_PREFIX[normalize_rail(rail)] + cycle_date
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE e2e_id LIKE ?",
                (prefix + '%',),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class CnapsService:
    def __init__(
        self,
        policy: CnapsPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[CnClock] = None,
        directory: Optional[Dict[str, str]] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or CnClock(
            beps_open_hour=policy.beps_open_hour,
            beps_open_minute=policy.beps_open_minute,
            beps_cutoff_hour=policy.beps_cutoff_hour,
            beps_cutoff_minute=policy.beps_cutoff_minute,
            cips_open_hour=8,
            cips_open_minute=30,
            cips_cutoff_hour=policy.cips_cutoff_hour,
            cips_cutoff_minute=policy.cips_cutoff_minute,
            tz_offset_minutes=policy.tz_offset_minutes,
            extra_holidays=policy.extra_holidays,
        )
        self.directory = dict(directory or DEFAULT_CNAPS_DIRECTORY)

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise CnapsError('cnaps_disabled', 'CNAPS / CIPS is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise CnapsError('cnaps_forbidden', 'Customers cannot manage CNAPS beneficiaries.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise CnapsError('cnaps_forbidden', 'Customers cannot originate CNAPS payments.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise CnapsError('cnaps_forbidden', 'Staff only.')

    def _owned_accounts(self, userid: str) -> List[str]:
        if self.accounts_fn is None:
            return []
        try:
            return own_accounts_from_customer_payload(self.accounts_fn(userid))
        except Exception:
            return []

    def _account_types(self, userid: str) -> Dict[str, str]:
        if self.accounts_fn is None:
            return {}
        try:
            return account_types_from_customer_payload(self.accounts_fn(userid))
        except Exception:
            return {}

    def _assert_internal_account(self, userid: str, account: str) -> None:
        owned = self._owned_accounts(userid)
        if owned and account not in owned:
            raise CnapsError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise CnapsError('credit_not_allowed', 'Credit accounts cannot originate CNAPS.')

    def _assert_amount(self, won: Decimal, rail: str, *, currency: str = 'CNY') -> None:
        _ = currency
        cap = self.policy.cap_for(rail)
        if won < self.policy.min_amount or won > cap:
            raise CnapsError('amount_out_of_range', 'Amount is outside the %s cap.' % rail)

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal, rail: str) -> bool:
        _ = rail
        return amount >= self.policy.dual_control_threshold

    def lookup(
        self,
        *,
        actor: str,
        actor_type: str,
        proxy_type: Any,
        proxy: Any,
        presented_name: Any = '',
        bank_code: Any = '',
        branch_code: Any = '',
        account_number: Any = '',
    ) -> ChineseResolution:
        self._require_send(actor_type)
        _ = actor
        return resolve_cnaps(
            proxy_type, proxy, presented_name,
            bank_code=bank_code, branch_code=branch_code, account_number=account_number,
            directory=self.directory,
        )

    def quote_fx(self, amount: Any, *, rail: Any = 'ibps', currency: Any = 'CNY') -> Dict[str, Any]:
        kind = normalize_rail(rail)
        currency = normalize_currency(currency, rail=kind)
        instructed = parse_money(amount)
        self._assert_amount(instructed, kind, currency=currency)
        rate = self.policy.rate_for(currency)
        usd = convert_instructed_to_usd(instructed, rate)
        fee = compute_fee(instructed, self.policy.fee_for(kind))
        return {
            'rail': kind,
            'currency': currency,
            'instructed_amount': money_str(instructed),
            'usd_amount': money_str(usd),
            'fx_rate': rate_str(rate),
            'fee': money_str(fee),
            'usd_total': money_str(usd + convert_instructed_to_usd(fee, rate) if fee else usd),
        }

    def add_beneficiary(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        default_account: Any,
        chinese_name: Any = '',
        proxy_type: Any = '',
        proxy: Any = '',
        bank_code: Any = '',
        branch_code: Any = '',
        account_number: Any = '',
        account_type: Any = '1',
    ) -> CnapsBeneficiary:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise CnapsError('cnaps_forbidden', 'Not allowed to add beneficiaries for this customer.')
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        kana = normalize_chinese(chinese_name or legal_name, required=True)
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        kind, value = normalize_proxy(proxy_type, proxy, required=False)
        bank = normalize_bank_code(bank_code, required=False)
        branch = normalize_branch_code(branch_code, required=False)
        external = normalize_cnaps_account(account_number, required=False)
        acct_type = normalize_account_type(account_type)
        if kind == 'cnaps' and 22 <= len(value) <= 31:
            bank, branch, external = value[:12], '', value[12:]
        if not (bank and external) and not kind:
            raise CnapsError('invalid_cnaps', 'CNAPS bank + account is required.')
        if bank and external and not cnaps_ok(bank, external, branch):
            raise CnapsError('invalid_cnaps', 'CNAPS bank/account is not valid.')
        if self.store.find_beneficiary_by_nickname(owner_userid, name) is not None:
            raise CnapsError('beneficiary_duplicate', 'A beneficiary with that nickname already exists.')
        fingerprint = fingerprint_for(owner_userid, bank, branch, external, value)
        if self.store.find_beneficiary_by_fingerprint(owner_userid, fingerprint) is not None:
            raise CnapsError('beneficiary_duplicate', 'That CNAPS destination is already on file.')
        open_rows = [row for row in self.store.list_beneficiaries(owner_userid) if row.status in OPEN_BENE]
        if len(open_rows) >= self.policy.max_beneficiaries:
            raise CnapsError('beneficiary_limit', 'CNAPS beneficiary limit reached.')
        now = float(self.clock())
        row = CnapsBeneficiary(
            beneficiary_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            chinese_name=kana,
            proxy_type=kind,
            proxy=value,
            bank_code=bank,
            branch_code=branch,
            account_number=external,
            account_type=acct_type,
            default_account=account,
            status=BENE_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
            fingerprint=fingerprint,
        )
        self.store.put_beneficiary(row)
        return row

    def get_beneficiary(self, *, beneficiary_id: str, actor: str, actor_type: str) -> CnapsBeneficiary:
        self._require_enabled()
        row = self.store.get_beneficiary(beneficiary_id)
        if row is None:
            raise CnapsError('beneficiary_not_found', 'CNAPS beneficiary not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise CnapsError('cnaps_forbidden', 'Not allowed to view this beneficiary.')
        return row

    def enforce_beneficiary(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> CnapsBeneficiary:
        row = self.get_beneficiary(beneficiary_id=beneficiary_id, actor=actor, actor_type=actor_type)
        if row.status == BENE_ARCHIVED:
            raise CnapsError('already_archived', 'Beneficiary is archived.')
        if row.status == BENE_PAUSED:
            raise CnapsError('beneficiary_paused', 'Beneficiary is paused.')
        if require_active and row.status != BENE_ACTIVE:
            raise CnapsError('invalid_status', 'Beneficiary is not active.')
        return row

    def set_beneficiary_status(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> CnapsBeneficiary:
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
            raise CnapsError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == BENE_ARCHIVED:
            raise CnapsError('already_archived', 'Beneficiary is already archived.')
        if wanted == BENE_PAUSED:
            if row.status == BENE_PAUSED:
                raise CnapsError('already_paused', 'Beneficiary is already paused.')
            if row.status != BENE_ACTIVE:
                raise CnapsError('invalid_status', 'Only an active beneficiary can be paused.')
        elif wanted == BENE_ACTIVE:
            if row.status == BENE_ACTIVE:
                raise CnapsError('already_active', 'Beneficiary is already active.')
            if row.status != BENE_PAUSED:
                raise CnapsError('invalid_status', 'Only a paused beneficiary can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_beneficiary(row)
        return row

    def _chinese_for(self, bene: CnapsBeneficiary, *, presented_name: Any = '', override: bool = False) -> ChineseResolution:
        presented = presented_name or bene.chinese_name
        resolution = resolve_cnaps(
            bene.proxy_type, bene.proxy, presented,
            bank_code=bene.bank_code, branch_code=bene.branch_code, account_number=bene.account_number,
            directory=self.directory,
        )
        if override:
            resolution.outcome = CHINESE_MATCH
            return resolution
        if self.policy.require_chinese_match and resolution.outcome in {CHINESE_NONE, CHINESE_UNAVAILABLE}:
            raise CnapsError(
                'chinese_mismatch',
                'Chinese name inquiry did not match.',
                chinese=resolution,
            )
        return resolution

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        beneficiary_id: Any,
        amount: Any,
        rail: Any = 'ibps',
        internal_account: Any = None,
        waive_fee: bool = False,
        presented_name: Any = '',
        currency: Any = 'CNY',
    ) -> Dict[str, Any]:
        self._require_send(actor_type)
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise CnapsError('cnaps_forbidden', 'Beneficiary does not belong to this customer.')
        kind = normalize_rail(rail)
        if not (bene.bank_code and bene.account_number) and not bene.proxy_type:
            raise CnapsError('invalid_cnaps', 'CNAPS bank + account is required.')
        ccy = normalize_currency(currency, rail=kind)
        instructed = parse_money(amount)
        self._assert_amount(instructed, kind, currency=ccy)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(instructed, self.policy.fee_for(kind), waived=staff_waive)
        rate = self.policy.rate_for(ccy)
        usd = convert_instructed_to_usd(instructed, rate)
        now = float(self.clock())
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        kana = self._chinese_for(bene, presented_name=presented_name)
        clock = self.calendar.snapshot(now, rail=kind)
        return {
            'amount': money_str(instructed),
            'instructed_amount': money_str(instructed),
            'currency': ccy,
            'usd_amount': money_str(usd),
            'fx_rate': rate_str(rate),
            'fee': money_str(fee),
            'total_usd': money_str(usd + convert_instructed_to_usd(fee, rate)),
            'internal_account': account,
            'rail': kind,
            'beneficiary': bene.to_dict(),
            'ofac': ofac.to_dict(),
            'chinese': kana.to_dict(),
            'dual_control': self._needs_dual_control(instructed, kind),
            'clock': clock,
        }

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        bene: CnapsBeneficiary,
        account: str,
        rail: str,
        currency: str,
        instructed: Decimal,
        usd: Decimal,
        fee: Decimal,
        purpose: str,
        memo: str,
        trace_id: str,
        ofac: ScreenResult,
        kana: ChineseResolution,
        waive_fee: bool,
    ) -> CnapsPayment:
        now = float(self.clock())
        value = self.calendar.cycle_date(now, rail=rail)
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        clock = self.calendar.snapshot(now, rail=rail)
        if ofac.hit:
            status = PN_HELD
        elif self._needs_dual_control(instructed, rail):
            status = PN_PENDING
        elif clock['after_cutoff']:
            status = PN_QUEUED
        elif rail == 'ibps':
            status = PN_COMPLETED
        else:
            status = PN_SENT
        payment = CnapsPayment(
            payment_id=uuid.uuid4().hex,
            trace_id=trace_id,
            beneficiary_id=bene.beneficiary_id,
            userid=owner_userid,
            internal_account=account,
            rail=rail,
            currency=currency,
            instructed_amount=money_str(instructed),
            usd_amount=money_str(usd),
            fx_rate=rate_str(self.policy.rate_for(currency)),
            fee=money_str(fee),
            fee_status=fee_status,
            nickname=bene.nickname,
            legal_name=bene.legal_name,
            chinese_mask=mask_chinese(bene.chinese_name),
            proxy_type=bene.proxy_type,
            proxy_mask=mask_proxy(bene.proxy_type, bene.proxy) if bene.proxy_type else '',
            bank_code=bene.bank_code,
            branch_code=bene.branch_code,
            account_last4=last4(bene.account_number) if bene.account_number else '',
            purpose=purpose,
            memo=memo,
            status=status if status in {PN_HELD, PN_PENDING, PN_QUEUED} else PN_SENT,
            e2e_id='',
            value_date=value,
            chinese_outcome=kana.outcome,
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
        )
        if status in {PN_SENT, PN_COMPLETED}:
            self._transmit(payment, actor=actor, complete=status == PN_COMPLETED)
        return payment

    def _transmit(self, payment: CnapsPayment, *, actor: str, complete: bool = False) -> CnapsPayment:
        now = float(self.clock())
        cycle = payment.value_date or self.calendar.cycle_date(now, rail=payment.rail)
        seq = self.store.next_e2e_sequence(cycle, payment.rail)
        payment.e2e_id = compose_e2e(payment.rail, cycle, seq)
        usd = parse_money(payment.usd_amount)
        fee_instructed = parse_money(payment.fee, allow_zero=True)
        fee_usd = convert_instructed_to_usd(fee_instructed, parse_fx_rate(payment.fx_rate)) if fee_instructed else Decimal('0.00')
        remark = 'cnaps to %s' % payment.nickname
        status = PN_SENT
        fail_note = ''
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(payment.internal_account, money_str(usd), remark)
            except Exception as exc:
                status = PN_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = PN_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = PN_FAILED
                    fail_note = str(result)[:240]
        if status == PN_SENT and fee_usd > 0 and payment.fee_status != FEE_WAIVED and self.debit_fn is not None:
            try:
                fee_result = self.debit_fn(payment.internal_account, money_str(fee_usd), 'cnaps fee %s' % mask_e2e(payment.e2e_id))
            except Exception:
                payment.fee_status = FEE_NSF
            else:
                kind = _classify_money_result(fee_result)
                payment.fee_status = FEE_COLLECTED if kind == 'ok' else FEE_NSF
        elif status == PN_SENT and (fee_usd == 0 or payment.fee_status == FEE_WAIVED):
            payment.fee_status = FEE_WAIVED if payment.fee_status == FEE_WAIVED or fee_usd == 0 else payment.fee_status
        payment.updated_at = now
        if status == PN_SENT:
            payment.sent_at = now
            payment.releaser = str(actor)
            if complete or payment.rail == 'ibps':
                payment.status = PN_COMPLETED
                payment.completed_at = now
            else:
                payment.status = PN_SENT
        else:
            payment.status = status
            payment.e2e_id = ''
            payment.note = fail_note
        return payment

    def originate(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        beneficiary_id: Any,
        amount: Any,
        rail: Any = 'ibps',
        internal_account: Any = None,
        purpose: Any = 'other',
        memo: Any = '',
        trace_id: Any = None,
        waive_fee: bool = False,
        presented_name: Any = '',
        chinese_override: bool = False,
        currency: Any = 'CNY',
    ) -> Tuple[CnapsPayment, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise CnapsError('cnaps_forbidden', 'Not allowed to originate CNAPS for this customer.')
        staff_override = bool(chinese_override) and actor_type in EMPLOYEE_ROLES
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise CnapsError('cnaps_forbidden', 'Beneficiary does not belong to this customer.')
        kind = normalize_rail(rail)
        ccy = normalize_currency(currency, rail=kind)
        instructed = parse_money(amount)
        self._assert_amount(instructed, kind, currency=ccy)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(instructed, self.policy.fee_for(kind), waived=staff_waive)
        usd = convert_instructed_to_usd(instructed, self.policy.rate_for(ccy))
        trace = normalize_id(trace_id)
        existing = self.store.get_payment_by_trace(trace)
        if existing is not None:
            return existing, False
        if len(self.store.list_payments(owner_userid)) >= self.policy.max_payments:
            raise CnapsError('cnaps_limit', 'CNAPS history limit reached.')
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        kana = self._chinese_for(bene, presented_name=presented_name, override=staff_override)
        if staff_override and kana.outcome in {CHINESE_NONE, CHINESE_UNAVAILABLE}:
            kana = ChineseResolution(
                outcome=CHINESE_MATCH, registered_name=bene.chinese_name,
                presented_name=str(presented_name or bene.chinese_name),
                proxy_type=bene.proxy_type or 'cnaps',
                proxy_mask=mask_proxy(bene.proxy_type, bene.proxy) if bene.proxy_type else format_cnaps(bene.bank_code, bene.account_number, bene.branch_code),
            )
        payment = self._place(
            owner_userid=owner_userid,
            actor=actor,
            bene=bene,
            account=account,
            rail=kind,
            currency=ccy,
            instructed=instructed,
            usd=usd,
            fee=fee,
            purpose=normalize_purpose(purpose),
            memo=normalize_note(memo, limit=140),
            trace_id=trace,
            ofac=ofac,
            kana=kana,
            waive_fee=staff_waive,
        )
        stored = self.store.put_payment(payment)
        if stored.payment_id != payment.payment_id:
            return stored, False
        if stored.status == PN_NSF:
            raise CnapsError('nsf', 'Insufficient funds for CNAPS.', payment=stored)
        if stored.status == PN_FAILED:
            raise CnapsError('failed', 'CNAPS debit did not complete.', payment=stored)
        return stored, True

    def get_payment(self, *, payment_id: str, actor: str, actor_type: str) -> CnapsPayment:
        self._require_enabled()
        row = self.store.get_payment(payment_id)
        if row is None:
            raise CnapsError('payment_not_found', 'CNAPS payment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise CnapsError('cnaps_forbidden', 'Not allowed to view this payment.')
        return row

    def cancel_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> CnapsPayment:
        self._require_send(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise CnapsError('cnaps_forbidden', 'Not allowed to cancel this payment.')
        if row.status not in CANCELABLE:
            raise CnapsError('not_cancelable', 'Payment can no longer be cancelled.')
        row.status = PN_CANCELLED
        row.actor = str(actor)
        row.note = normalize_note(note)
        row.updated_at = float(self.clock())
        self.store.update_payment(row)
        return row

    def release_payment(self, *, payment_id: str, actor: str, actor_type: str) -> CnapsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.ofac_hit:
            raise CnapsError('ofac_hold', 'OFAC hold must be overridden first.')
        if row.status not in {PN_PENDING, PN_QUEUED}:
            raise CnapsError('not_releasable', 'Payment is not waiting for release.')
        if row.status == PN_PENDING and row.actor == str(actor):
            raise CnapsError('same_approver', 'Maker cannot release their own payment.')
        complete = row.rail == 'ibps'
        self._transmit(row, actor=actor, complete=complete)
        if row.status == PN_NSF:
            self.store.update_payment(row)
            raise CnapsError('nsf', 'Insufficient funds for CNAPS.', payment=row)
        if row.status == PN_FAILED:
            self.store.update_payment(row)
            raise CnapsError('failed', 'CNAPS debit did not complete.', payment=row)
        self.store.update_payment(row)
        return row

    def reject_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> CnapsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status not in {PN_HELD, PN_QUEUED, PN_PENDING}:
            raise CnapsError('not_rejectable', 'Payment cannot be rejected.')
        row.status = PN_REJECTED
        row.reason = normalize_note(reason, limit=40) or 'other'
        row.note = normalize_note(note)
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_payment(row)
        return row

    def complete_payment(self, *, payment_id: str, actor: str, actor_type: str) -> CnapsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status == PN_COMPLETED:
            raise CnapsError('already_completed', 'Payment is already completed.')
        if row.status != PN_SENT:
            raise CnapsError('not_completable', 'Only a sent payment can be completed.')
        now = float(self.clock())
        row.status = PN_COMPLETED
        row.completed_at = now
        row.updated_at = now
        row.actor = str(actor)
        self.store.update_payment(row)
        return row

    def recall_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> CnapsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status == PN_RECALLED:
            raise CnapsError('already_recalled', 'Payment is already recalled.')
        if row.status not in {PN_SENT, PN_COMPLETED}:
            raise CnapsError('not_recallable', 'Payment cannot be recalled.')
        now = float(self.clock())
        window = STAFF_COMPLAINT_DAYS * 24 * 60 * 60
        sent_at = row.sent_at or row.created_at
        if row.rail == 'ibps' and (now - sent_at) > window:
            raise CnapsError('not_recallable', 'CNAPS recall window has closed.')
        usd = parse_money(row.usd_amount)
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(row.internal_account, money_str(usd), 'cnaps recall %s' % mask_e2e(row.e2e_id))
            except Exception as exc:
                raise CnapsError('recall_failed', 'Recall credit did not complete.', payment=row) from exc
            if _classify_money_result(result) != 'ok':
                raise CnapsError('recall_failed', 'Recall credit did not complete.', payment=row)
        row.status = PN_RECALLED
        row.recalled_at = now
        row.updated_at = now
        row.actor = str(actor)
        row.note = normalize_note(note)
        self.store.update_payment(row)
        return row

    def override_ofac(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> CnapsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status != PN_HELD:
            raise CnapsError('not_releasable', 'No OFAC hold to override.')
        row.ofac_hit = 0
        row.note = normalize_note(note)
        complete = row.rail == 'ibps'
        if self._needs_dual_control(parse_money(row.instructed_amount), row.rail):
            row.status = PN_PENDING
            row.updated_at = float(self.clock())
            self.store.update_payment(row)
            return row
        clock = self.calendar.snapshot(float(self.clock()), rail=row.rail)
        if clock['after_cutoff']:
            row.status = PN_QUEUED
            row.updated_at = float(self.clock())
            self.store.update_payment(row)
            return row
        self._transmit(row, actor=actor, complete=complete)
        self.store.update_payment(row)
        return row

    def override_chinese(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> CnapsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        row.chinese_outcome = CHINESE_MATCH
        row.note = normalize_note(note)
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_payment(row)
        return row

    def waive_fee(self, *, payment_id: str, actor: str, actor_type: str) -> CnapsPayment:
        self._require_staff(actor_type)
        row = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if row.status not in CANCELABLE | {PN_SENT}:
            raise CnapsError('not_releasable', 'Fee can no longer be waived.')
        row.fee_status = FEE_WAIVED
        row.fee = money_str(Decimal('0.00'))
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_payment(row)
        return row

    def run_due(self, userid: str) -> List[CnapsPayment]:
        now = float(self.clock())
        due = []
        for row in self.store.list_payments(userid):
            if row.status != PN_QUEUED:
                continue
            clock = self.calendar.snapshot(now, rail=row.rail)
            if clock['after_cutoff']:
                continue
            complete = row.rail == 'ibps'
            self._transmit(row, actor=row.actor or 'system', complete=complete)
            self.store.update_payment(row)
            due.append(row)
        return due

    def export_cn1(self, userid: str, *, actor: str, actor_type: str) -> str:
        self._require_staff(actor_type)
        _ = actor
        lines = []
        for index, row in enumerate(self.store.list_payments(userid), start=1):
            if row.status not in {PN_SENT, PN_COMPLETED}:
                continue
            lines.append(compose_cn1(
                rail=row.rail,
                amount=row.instructed_amount,
                bank_code=row.bank_code or self.policy.source_bank,
                branch_code=row.branch_code or self.policy.source_branch,
                account_last4=row.account_last4,
                e2e_id=row.e2e_id,
                purpose=row.purpose,
                value_date=row.value_date,
                chinese_mask=row.chinese_mask,
                ofac_hit=bool(row.ofac_hit),
                sequence=index,
                source_bank=self.policy.source_bank,
                currency=row.currency,
            ))
        return '\n'.join(lines)

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and actor and actor != userid:
            raise CnapsError('cnaps_forbidden', 'Not allowed to view this CNAPS book.')
        benes = self.store.list_beneficiaries(userid)
        pays = self.store.list_payments(userid)
        ytd = Decimal('0.00')
        ytd_fees = Decimal('0.00')
        recalled = Decimal('0.00')
        for row in pays:
            if row.status in {PN_SENT, PN_COMPLETED}:
                ytd += parse_money(row.instructed_amount, allow_zero=True)
                ytd_fees += parse_money(row.fee, allow_zero=True)
            if row.status == PN_RECALLED:
                recalled += parse_money(row.instructed_amount, allow_zero=True)
        now = float(self.clock())
        return {
            'enabled': True,
            'beneficiaries': [row.to_dict() for row in benes],
            'payments': [row.to_dict() for row in pays],
            'ytd_sent': money_str(ytd),
            'ytd_fees': money_str(ytd_fees),
            'recalled_ytd': money_str(recalled),
            'active_count': sum(1 for row in benes if row.status == BENE_ACTIVE),
            'open_count': sum(1 for row in pays if row.status in OPEN_PN),
            'fx_rate': rate_str(self.policy.cnyusd),
            'cny_fx_rate': rate_str(self.policy.cnyusd),
            'clock': self.calendar.snapshot(now, rail='ibps'),
            'beps_clock': self.calendar.snapshot(now, rail='beps'),
            'cips_clock': self.calendar.snapshot(now, rail='cips'),
        }


_SERVICE: Optional[CnapsService] = None


def set_service(service: Optional[CnapsService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[CnapsService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('CNAPS_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryCnapsStore()
    path = os.environ.get('CNAPS_DB', DEFAULT_STORE_PATH)
    return SqliteCnapsStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[CnClock] = None,
    directory: Optional[Dict[str, str]] = None,
) -> CnapsService:
    if store is None:
        store = default_store()
    return CnapsService(
        CnapsPolicy.from_env(),
        store,
        clock=clock,
        debit_fn=debit_fn,
        credit_fn=credit_fn,
        accounts_fn=accounts_fn,
        screen_fn=screen_fn,
        calendar=calendar,
        directory=directory,
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
        'cnaps_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_completed': 409,
        'already_recalled': 409,
        'nsf': 409,
        'failed': 409,
        'recall_failed': 409,
        'cnaps_forbidden': 403,
        'cnaps_disabled': 403,
        'beneficiary_paused': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'chinese_mismatch': 403,
        'not_cancelable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_completable': 403,
        'not_recallable': 403,
        'beneficiary_not_found': 404,
        'payment_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_bank': 400,
        'invalid_branch': 400,
        'invalid_cnaps_account': 400,
        'invalid_cnaps': 400,
        'invalid_chinese': 400,
        'invalid_phone': 400,
        'invalid_resident_id': 400,
        'invalid_email': 400,
        'invalid_proxy': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_e2e': 400,
        'invalid_cn1': 400,
        'invalid_clearing': 400,
        'invalid_currency': 400,
        'invalid_rail': 400,
        'invalid_account_type': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_beneficiary': 400,
        'missing_payment': 400,
    }.get(code, 400)


def _error_body(exc: CnapsError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('payment') is not None:
        body['payment'] = exc.extra['payment'].to_dict()
    if exc.extra.get('beneficiary') is not None:
        body['beneficiary'] = exc.extra['beneficiary'].to_dict()
    if exc.extra.get('chinese') is not None:
        body['chinese'] = exc.extra['chinese'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except CnapsError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: CnapsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Cnaps': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_lookup(service: CnapsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        resolution = service.lookup(
            actor=userid,
            actor_type=actor_type,
            proxy_type=values.get('proxy_type') or values.get('cnaps_type'),
            proxy=values.get('proxy') or values.get('phone') or values.get('email') or values.get('nid') or values.get('citizen'),
            presented_name=values.get('chinese_name') or values.get('legal_name') or values.get('name') or '',
            bank_code=values.get('bank_code') or values.get('bank'),
            branch_code=values.get('branch_code') or values.get('branch'),
            account_number=values.get('account_number') or values.get('external_account'),
        )
        return jsonify({'chinese': resolution.to_dict()}), 200

    return _handle_errors(_run)


def handle_quote(service: CnapsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}

    def _run():
        quote = service.quote_fx(
            values.get('amount'),
            rail=values.get('rail') or 'ibps',
            currency=values.get('currency') or 'CNY',
        )
        return jsonify({'quote': quote}), 200

    return _handle_errors(_run)


def handle_add_beneficiary(service: CnapsService):
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
            chinese_name=values.get('chinese_name') or values.get('huming') or values.get('huhao'),
            default_account=values.get('default_account') or values.get('account') or values.get('from_account'),
            proxy_type=values.get('proxy_type') or values.get('cnaps_type'),
            proxy=values.get('proxy') or values.get('phone') or values.get('email') or values.get('nid') or values.get('citizen'),
            bank_code=values.get('bank_code') or values.get('bank'),
            branch_code=values.get('branch_code') or values.get('branch'),
            account_number=values.get('account_number') or values.get('external_account'),
            account_type=values.get('account_type') or '1',
        )
        return jsonify({
            'message': 'CNAPS beneficiary added',
            'beneficiary': row.to_dict(),
            'Cnaps': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _bene_status_route(service: CnapsService, status: str, ok_message: str):
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
            'Cnaps': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: CnapsService):
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
            rail=values.get('rail') or 'ibps',
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            waive_fee=bool(values.get('waive_fee')),
            presented_name=values.get('chinese_name') or values.get('legal_name') or '',
            currency=values.get('currency') or 'CNY',
        )
        return jsonify({'preview': preview, 'Cnaps': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: CnapsService):
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
            rail=values.get('rail') or 'ibps',
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
            presented_name=values.get('chinese_name') or values.get('legal_name') or values.get('name') or '',
            chinese_override=bool(values.get('chinese_override') or values.get('cop_override')),
            currency=values.get('currency') or 'CNY',
        )
        return jsonify({
            'message': 'CNAPS originated' if created else 'CNAPS already posted',
            'payment': payment.to_dict(),
            'Cnaps': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: CnapsService):
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
            'message': 'CNAPS cancelled',
            'payment': payment.to_dict(),
            'Cnaps': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_route(service: CnapsService, action: str):
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
            message = 'CNAPS released'
        elif action == 'reject':
            payment = service.reject_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'CNAPS rejected'
        elif action == 'complete':
            payment = service.complete_payment(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'CNAPS completed'
        elif action == 'recall':
            payment = service.recall_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'CNAPS recalled'
        elif action == 'override':
            payment = service.override_ofac(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'chinese':
            payment = service.override_chinese(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Chinese name inquiry overridden'
        elif action == 'waive':
            payment = service.waive_fee(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'CNAPS fee waived'
        else:
            raise CnapsError('invalid_status', 'Unknown CNAPS action.')
        return jsonify({
            'message': message,
            'payment': payment.to_dict(),
            'Cnaps': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: CnapsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Cnaps': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_export(service: CnapsService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid

    def _run():
        payload = service.export_cn1(owner, actor=userid, actor_type=actor_type)
        return jsonify({'cn1': payload, 'Cnaps': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def attach_cnaps_routes(app, service: CnapsService) -> None:
    @app.route('/listCnaps', methods=['POST', 'GET'])
    def list_cnaps_route():
        return handle_list(service)

    @app.route('/lookupCnaps', methods=['POST', 'GET'])
    def lookup_cnaps_route():
        return handle_lookup(service)

    @app.route('/quoteCnapsFx', methods=['POST', 'GET'])
    def quote_cnaps_route():
        return handle_quote(service)

    @app.route('/addCnapsBeneficiary', methods=['POST', 'GET'])
    def add_cnaps_beneficiary_route():
        return handle_add_beneficiary(service)

    @app.route('/pauseCnapsBeneficiary', methods=['POST', 'GET'])
    def pause_cnaps_beneficiary_route():
        return _bene_status_route(service, BENE_PAUSED, 'CNAPS beneficiary paused')

    @app.route('/resumeCnapsBeneficiary', methods=['POST', 'GET'])
    def resume_cnaps_beneficiary_route():
        return _bene_status_route(service, BENE_ACTIVE, 'CNAPS beneficiary resumed')

    @app.route('/archiveCnapsBeneficiary', methods=['POST', 'GET'])
    def archive_cnaps_beneficiary_route():
        return _bene_status_route(service, BENE_ARCHIVED, 'CNAPS beneficiary archived')

    @app.route('/previewCnaps', methods=['POST', 'GET'])
    def preview_cnaps_route():
        return handle_preview(service)

    @app.route('/sendCnaps', methods=['POST', 'GET'])
    def send_cnaps_route():
        return handle_send(service)

    @app.route('/cancelCnaps', methods=['POST', 'GET'])
    def cancel_cnaps_route():
        return handle_cancel(service)

    @app.route('/releaseCnaps', methods=['POST', 'GET'])
    def release_cnaps_route():
        return _staff_route(service, 'release')

    @app.route('/rejectCnaps', methods=['POST', 'GET'])
    def reject_cnaps_route():
        return _staff_route(service, 'reject')

    @app.route('/completeCnaps', methods=['POST', 'GET'])
    def complete_cnaps_route():
        return _staff_route(service, 'complete')

    @app.route('/recallCnaps', methods=['POST', 'GET'])
    def recall_cnaps_route():
        return _staff_route(service, 'recall')

    @app.route('/overrideCnapsOfac', methods=['POST', 'GET'])
    def override_cnaps_ofac_route():
        return _staff_route(service, 'override')

    @app.route('/overrideCnapsChinese', methods=['POST', 'GET'])
    def override_cnaps_chinese_route():
        return _staff_route(service, 'chinese')

    @app.route('/waiveCnapsFee', methods=['POST', 'GET'])
    def waive_cnaps_fee_route():
        return _staff_route(service, 'waive')

    @app.route('/runDueCnaps', methods=['POST', 'GET'])
    def run_due_cnaps_route():
        return handle_run_due(service)

    @app.route('/exportCnaps', methods=['POST', 'GET'])
    def export_cnaps_route():
        return handle_export(service)
