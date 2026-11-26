"""Inbound Canada Interac e-Transfer / EFT / Lynx receive posting.

Staff ingest incoming CAD credits. Recipients enroll Canada proxies
(CPA routing + DDA, +1 NANP mobile, 9-digit SIN, email) at this
bank's CPA routing. Matched Interac credits post instantly 24/7.
EFT waits for 09:00-16:00 ET. Lynx waits for 07:00-18:00 ET.
Unmatched proxies wait for a customer claim or staff assignment.
Independent of outbound Interac origination (PR #212), inbound UK FPS
(PR #210), domestic Fedwire (PR #73), ACH linking (PR #68), and the
older Interac-only PRs #111/#113. Existing `/fundTransfer`,
`/withdrawAmount`, and `/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- CPA 9-digit electronic routing (0 + FI + transit, 1-2 repeating checksum)
- 7-12 digit Canadian DDA
- Canadian NANP +1 mobile, 9-digit SIN Luhn, email proxy
- Name inquiry (`match` / `close_match` / `no_match` / `unavailable`)
- IETC / CPA5 / LYNX End-to-End ID (`IETC`/`CPA5`/`LYNX` + YYYYMMDD + seq)
- CA1 14-pipe file (XML/DOCTYPE and non-Canada clearing rejected)
- CaClock ET UTC-5: Interac 24/7; EFT 09:00–16:00;
  Lynx 07:00–18:00; Payments Canada holidays + one weekday after a weekend cluster
  (New Year 2022-01-01 Sat → EFT value date 2022-01-04)
- CADUSD quote book converting instructed amount to USD credit
- Interac claim (customer presents matching proxy; enroll rematches unmatched)
- Return windows (Interac 24h customer / EFT+Lynx next ET session / 90-day staff)
- Dual-control at CAD 50,000; Interac cap 50k / EFT 1m / Lynx 100m

Reuses Fedwire OFAC screening, money parsing, and account directory
from `utility.wire`. Stores are pluggable (memory for tests, sqlite WAL
for restart-safe default). Full routing, DDA, payee name, fingerprint, phone,
SIN, email, and E2E ID never appear in to_dict / snapshots.
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
    'cra': 'tax', 'taxes': 'tax', 'revenue': 'tax',
    'mortgage': 'loan', 'housing': 'rent',
}
RAILS = frozenset({'inst', 'eft', 'lynx'})
RAIL_ALIASES = {
    'inst': 'inst', 'instant': 'inst', 'interac': 'inst', 'etransfer': 'inst',
    'e_transfer': 'inst', 'autodeposit': 'inst', '24/7': 'inst', 'canpay': 'inst',
    'fast': 'inst', 'iet': 'inst',
    'eft': 'eft', 'credit': 'eft', 'transfer': 'eft', 'bulk': 'eft', 'batch': 'eft',
    'ach': 'eft', 'aft': 'eft', 'cpa': 'eft', 'pad': 'eft', 'standard005': 'eft',
    'lynx': 'lynx', 'lvts': 'lynx', 'rtgs': 'lynx', 'hvps': 'lynx',
    'boc': 'lynx', 'bankofcanada': 'lynx',
}
ACCOUNT_TYPES = frozenset({'1', '2', '4'})
ACCOUNT_TYPE_ALIASES = {
    'ordinary': '1', 'demand': '1', 'savings_ord': '1',
    'current': '2', 'checking': '2',
    'savings': '4',
}
PROXY_TYPES = frozenset({'cpa', 'phone', 'nid', 'email'})
PROXY_ALIASES = {
    'account': 'cpa', 'bank': 'cpa', 'acct': 'cpa', 'iban': 'cpa',
    'routing': 'cpa', 'transit': 'cpa', 'cpa': 'cpa', 'sort': 'cpa',
    'mobile': 'phone', 'msisdn': 'phone', 'tel': 'phone', 'sms': 'phone',
    'mail': 'email', 'e_mail': 'email',
    'nid': 'nid', 'national_id': 'nid', 'id': 'nid', 'sin': 'nid',
    'social': 'nid', 'tax': 'nid', 'citizen': 'nid',
}
PAYEE_MATCH = 'match'
PAYEE_CLOSE = 'close_match'
PAYEE_NONE = 'no_match'
PAYEE_UNAVAILABLE = 'unavailable'
PAYEE_OUTCOMES = frozenset({PAYEE_MATCH, PAYEE_CLOSE, PAYEE_NONE, PAYEE_UNAVAILABLE})
CLEARING_BY_RAIL = {'inst': 'IETC', 'eft': 'CPA5', 'lynx': 'LYNX'}
REJECTED_CLEARING = frozenset({
    'AUNPP', 'USRTPP', 'FEDNOW', 'FEDWIRE', 'ACH', 'UPI', 'CHIPS', 'SWIFT',
    'PIX', 'SPEI', 'NPP', 'OSKO', 'FAST', 'PAYNOW', 'MEPS', 'GIRO', 'PNW',
    'FPS', 'FPS1', 'CHATS', 'ECG', 'HKFPS', 'HKID', 'CHAPS', 'BACS',
    'ZGN', 'KFT1', 'MTS', 'BOJ', 'BOJNET', 'ZENGIN', 'MORETIME',
    'OPEN', 'EFTC', 'BOKW', 'KFTC', 'PPAY', 'SMRT', 'BAHT', 'PP1', 'SMART',
    'DUIT', 'IBGX', 'RNTS', 'DN1', 'BAHTNET', 'PROMPTPAY', 'IBG', 'RENTAS',
    'INST', 'INSTAPAY', 'PESO', 'PASS', 'IP1', 'PESONET', 'PHILPASS',
    'IBPS', 'BEPS', 'CIPS', 'CNAPS', 'CN1',
    'SCTI', 'SCT1', 'TARG', 'SE1', 'SEPA',
})
DEFAULT_BANKS = {
    '000': 'Bank of Canada',
    '001': 'Bank of Montreal',
    '002': 'Scotiabank',
    '003': 'RBC',
    '004': 'TD',
    '006': 'National Bank',
    '010': 'CIBC',
    '016': 'HSBC Bank Canada',
}
CPA_PASS = frozenset({
    ('000100016', '1234567'),
    ('000300004', '9876543210'),
    ('000200006', '1122334'),
    ('000400002', '1234567'),
})
DEFAULT_PAYEE_DIRECTORY = {
    'cpa:0004000021234567': 'Jean-Pierre Tremblay',
    'phone:+14165551234': 'Jean-Pierre Tremblay',
    'nid:046454286': 'Jean-Pierre Tremblay',
    'email:jean@example.ca': 'Jean-Pierre Tremblay',
}
CA1_VERSION = '1'
CA1_FIELDS = 14
DEFAULT_STORE_PATH = 'SystemLogs/carecv.sqlite'
DEFAULT_RECEIVER_BANK = '000400002'
DEFAULT_SENDER_BANK = '000100016'
CUSTOMER_RETURN_SECONDS = 24 * 60 * 60
STAFF_COMPLAINT_DAYS = 90
CLAIM_SECONDS = 24 * 60 * 60
ET_OFFSET_MINUTES = -300
DEFAULT_CADUSD = Decimal('0.730000')
BLOCKED_E2E_PREFIXES = (
    'IMAD', 'UETR', 'NPP', 'AUNPP', 'RRN', 'STAN', 'ARN', 'RFI', 'RFR', 'RTR',
    'PNW', 'FAST', 'MEPS', 'GIRO', 'PIX', 'SPEI', 'UPI', 'FPS1', 'CHATS', 'ECG',
    'HKFPS', 'HKID', 'PAYNOW', 'FPS', 'CHAPS', 'BACS', 'FEDWIRE', 'FEDNOW',
    'MTS', 'ZGN', 'BOJ', 'KFT1', 'MORETIME', 'ZENGIN', 'OPEN', 'EFTC', 'BOKW',
    'KFTC', 'PPAY', 'SMRT', 'BAHT', 'PP1', 'SMART', 'DUIT', 'IBGX', 'RNTS', 'DN1',
    'INST', 'PESO', 'PASS', 'IP1', 'INSTAPAY', 'PESONET', 'PHILPASS',
    'IBPS', 'BEPS', 'CIPS', 'CNAPS', 'SCTI', 'SCT1', 'TARG', 'SE1',
)
E2E_PREFIX = {'inst': 'IETC', 'eft': 'CPA5', 'lynx': 'LYNX'}
CA_AREA_CODES = frozenset({
    '204', '226', '236', '249', '250', '257', '263', '289',
    '306', '343', '354', '365', '367', '368', '382', '387',
    '403', '416', '418', '428', '431', '437', '438', '450', '460', '468', '474',
    '506', '514', '519', '548', '568', '579', '581', '584', '587',
    '604', '613', '639', '647', '672', '683',
    '705', '709', '742', '753', '778', '780', '782',
    '807', '819', '825', '851', '867', '873', '879',
    '902', '905', '942',
})
FX_QUANTUM = Decimal('0.000001')
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)


class CarecvError(ValueError):
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
        raise CarecvError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise CarecvError('invalid_name', 'Legal name must be 2-80 characters.')
    return text.upper()


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or '').strip().lower()
    text = PURPOSE_ALIASES.get(text, text)
    if not text:
        return default
    if text not in PURPOSES:
        raise CarecvError('invalid_purpose', 'Unknown payment purpose.')
    return text


def normalize_rail(value: Any, *, default: str = 'inst') -> str:
    text = str(value or '').strip().lower().replace(' ', '_')
    text = RAIL_ALIASES.get(text, text)
    if not text:
        text = default
    if text not in RAILS:
        raise CarecvError('invalid_rail', 'Rail must be inst, eft, or lynx.')
    return text


def normalize_currency(value: Any, *, default: str = 'CAD', rail: str = 'inst') -> str:
    _ = rail
    text = str(value or default or 'CAD').strip().upper()
    if text in {'CAD', 'CANADIAN', 'C$', 'CA$'}:
        text = 'CAD'
    if text != 'CAD':
        raise CarecvError('invalid_currency', 'Interac / EFT / Lynx is CAD only.')
    return text


def normalize_proxy_type(value: Any, *, default: str = '') -> str:
    text = str(value or '').strip().lower().replace('-', '_')
    text = PROXY_ALIASES.get(text, text)
    if not text:
        return default
    if text not in PROXY_TYPES:
        raise CarecvError('invalid_proxy', 'Proxy must be cpa, phone, nid, or email.')
    return text


def normalize_account_type(value: Any, *, default: str = '1') -> str:
    text = str(value or '').strip().lower()
    text = ACCOUNT_TYPE_ALIASES.get(text, text)
    if not text:
        text = default
    if text not in ACCOUNT_TYPES:
        raise CarecvError('invalid_account_type', 'Account type must be 1, 2, or 4.')
    return text


def cpa_compact(value: Any) -> str:
    return ''.join(ch for ch in str(value or '') if ch.isdigit())


def cpa_check_ok(digits: str) -> bool:
    """CPA 9-digit electronic routing: 0 + FI(3) + transit(5), weights 1-2 repeating."""
    text = ''.join(ch for ch in str(digits or '') if ch.isdigit())
    if len(text) != 9 or not text.startswith('0') or text == '000000000':
        return False
    weights = (1, 2, 1, 2, 1, 2, 1, 2, 1)
    total = sum(int(d) * w for d, w in zip(text, weights))
    return total % 10 == 0


def dda_digits_ok(account: str) -> bool:
    text = ''.join(ch for ch in str(account or '') if ch.isdigit())
    return 7 <= len(text) <= 12


def normalize_dda(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise CarecvError('invalid_account', 'Account is required.')
        return ''
    if not dda_digits_ok(digits):
        raise CarecvError('invalid_account', 'Account must be 7-12 digits.')
    return digits


def normalize_bank_code(value: Any, *, required: bool = True) -> str:
    raw = str(value or '').strip()
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if not digits:
        if required or raw:
            raise CarecvError('invalid_bank', 'CPA routing is required.')
        return ''
    if len(digits) < 9:
        digits = digits.zfill(9)
    if len(digits) != 9 or not cpa_check_ok(digits):
        raise CarecvError('invalid_bank', 'CPA routing failed checksum.')
    return digits


def normalize_branch_code(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise CarecvError('invalid_branch', 'Transit must be 5 digits.')
        return ''
    if len(digits) != 5:
        raise CarecvError('invalid_branch', 'Transit must be 5 digits.')
    return digits


def cpa_ok(bank: str, account: str, branch: str = '') -> bool:
    _ = branch
    routing = ''.join(ch for ch in str(bank or '') if ch.isdigit())
    if len(routing) < 9:
        routing = routing.zfill(9) if routing else ''
    dda = ''.join(ch for ch in str(account or '') if ch.isdigit())
    if (routing, dda) in CPA_PASS:
        return True
    if routing and not cpa_check_ok(routing):
        return False
    if dda and not dda_digits_ok(dda):
        return False
    return bool(routing and dda)


def bank_name(value: Any, *, directory: Optional[Dict[str, str]] = None) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit()).zfill(9) if str(value or '').strip() else ''
    table = directory or DEFAULT_BANKS
    if len(digits) >= 4:
        return table.get(digits[1:4], '')
    return table.get(str(value or '')[:3], '')


def mask_bank(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) >= 8:
        return digits[:4] + '****' + digits[-4:]
    if digits:
        return '*' * len(digits)
    return '****'


def format_cpa(bank: str, account: str, branch: str = '') -> str:
    _ = branch
    routing = ''.join(ch for ch in str(bank or '') if ch.isdigit())
    dda = ''.join(ch for ch in str(account or '') if ch.isdigit())
    if routing and dda:
        compact = routing + dda
        if len(compact) < 8:
            return '*' * len(compact)
        return compact[:4] + '****' + compact[-4:]
    if dda:
        if len(dda) < 4:
            return '*' * len(dda)
        return ('*' * (len(dda) - 4)) + dda[-4:]
    if routing:
        return mask_bank(routing)
    return ''


def normalize_external_account(value: Any, *, required: bool = True) -> str:
    return normalize_dda(value, required=required)


def mask_account(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) < 4:
        return '*' * len(digits) if digits else '****'
    return ('*' * (len(digits) - 4)) + digits[-4:]


def sin_ok(digits: str) -> bool:
    """Canadian SIN: 9-digit Luhn. 046454286 is the CRA test SIN."""
    text = ''.join(ch for ch in str(digits or '') if ch.isdigit())
    if len(text) != 9:
        return False
    total = 0
    for i, ch in enumerate(text):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def normalize_sin(value: Any, *, required: bool = False) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise CarecvError('invalid_sin', 'SIN is required.')
        return ''
    if not sin_ok(digits):
        raise CarecvError('invalid_sin', 'SIN failed checksum.')
    return digits


def _letter_chars(text: str) -> List[str]:
    return [ch for ch in text if ch.isalpha()]


def normalize_payee_name(value: Any, *, required: bool = True) -> str:
    text = unicodedata.normalize('NFC', str(value or ''))
    text = ' '.join(text.replace('\u00a0', ' ').split())
    if not text:
        if required:
            raise CarecvError('invalid_payee_name', 'Payee name is required.')
        return ''
    letters = _letter_chars(text)
    if required and len(letters) < 2:
        raise CarecvError('invalid_payee_name', 'Payee name must contain at least two letters.')
    if len(text) > 80:
        raise CarecvError('invalid_payee_name', 'Payee name must be 2-80 characters.')
    return text


def mask_payee_name(value: Any) -> str:
    """Mask using first and last letter so Jean-Pierre Tremblay → J***y."""
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
            raise CarecvError('invalid_phone', 'Mobile number is required.')
        return ''
    if digits.startswith('1') and len(digits) == 11:
        national = digits[1:]
    elif digits.startswith('01') and len(digits) == 12:
        national = digits[2:]
    else:
        national = digits
    if len(national) != 10 or national[0] == '0' or national[3] == '0' or national[:3] not in CA_AREA_CODES:
        raise CarecvError('invalid_phone', 'Canadian mobile must be a +1 NANP area code.')
    return '+1' + national


def mask_phone(value: Any) -> str:
    text = str(value or '')
    if len(text) < 4:
        return text
    return '+1****' + text[-4:]


def normalize_email(value: Any, *, required: bool = False) -> str:
    text = str(value or '').strip().lower()
    if not text:
        if required:
            raise CarecvError('invalid_email', 'Email is required.')
        return ''
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', text):
        raise CarecvError('invalid_email', 'Email is not valid.')
    return text


def mask_email(value: Any) -> str:
    text = str(value or '')
    if '@' not in text:
        return text
    local, _, domain = text.partition('@')
    head = local[:1] if local else '*'
    return '%s***@%s' % (head, domain)


def infer_proxy_type(value: Any) -> str:
    text = str(value or '').strip()
    if '@' in text:
        return 'email'
    digits = ''.join(ch for ch in text if ch.isdigit())
    if digits.startswith('1') and len(digits) == 11:
        return 'phone'
    if len(digits) == 10 and digits[:3] in CA_AREA_CODES:
        return 'phone'
    if len(digits) == 9 and sin_ok(digits):
        return 'nid'
    if 16 <= len(digits) <= 21:
        return 'cpa'
    return ''


def normalize_proxy(proxy_type: Any, value: Any, *, required: bool = False) -> Tuple[str, str]:
    kind = normalize_proxy_type(proxy_type, default='')
    if not kind:
        kind = infer_proxy_type(value)
    if not kind:
        if required:
            raise CarecvError('invalid_proxy', 'Proxy type is required.')
        return '', ''
    if kind == 'phone':
        return kind, normalize_phone(value, required=True)
    if kind == 'email':
        return kind, normalize_email(value, required=True)
    if kind == 'nid':
        return kind, normalize_sin(value, required=True)
    routing_account = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(routing_account) < 16:
        raise CarecvError('invalid_cpa', 'CPA proxy is routing plus account.')
    routing, dda = routing_account[:9], routing_account[9:]
    if not cpa_ok(routing, dda):
        raise CarecvError('invalid_cpa', 'CPA routing plus account is not valid.')
    return kind, routing + dda


def mask_proxy(proxy_type: str, value: str) -> str:
    if proxy_type == 'phone':
        return mask_phone(value)
    if proxy_type == 'email':
        return mask_email(value)
    if proxy_type == 'nid':
        return mask_account(value)
    if proxy_type == 'cpa':
        digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
        if len(digits) >= 16:
            return format_cpa(digits[:9], digits[9:])
        return format_cpa(digits, '')
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
        raise CarecvError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise CarecvError('invalid_reference', 'E2E sequence out of range.')
    return '%s%s%06d' % (E2E_PREFIX[kind], cycle_date, seq)


def normalize_e2e(value: Any) -> str:
    text = re.sub(r'[^A-Z0-9]', '', str(value or '').upper())
    if not text:
        raise CarecvError('invalid_reference', 'Canada End-to-End ID is required.')
    if any(text.startswith(prefix) for prefix in BLOCKED_E2E_PREFIXES):
        raise CarecvError('invalid_reference', 'End-to-End ID must not use another rail prefix.')
    if len(text) == 22 and text[:8].isdigit():
        raise CarecvError('invalid_reference', 'Fedwire IMAD is not a Canada End-to-End ID.')
    if not re.fullmatch(r'(IETC|CPA5|LYNX)\d{14}', text):
        raise CarecvError('invalid_reference', 'E2E ID must be IETC/CPA5/LYNX + date + seq.')
    return text


def compose_amount_field(amount: Decimal) -> str:
    quantized = amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    if quantized < 0 or quantized > Decimal('999999999.99'):
        raise CarecvError('invalid_amount', 'Amount cannot be encoded in CA1.')
    return money_str(quantized)


def parse_amount_field(value: Any) -> Decimal:
    try:
        return parse_money(value)
    except AmountError as exc:
        raise CarecvError('invalid_amount', 'CA1 amount field is invalid.') from exc


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
        raise CarecvError('invalid_fx', 'FX rate must be positive.')
    return (amount * rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def payee_name_outcome(registered: str, presented: str) -> str:
    left = normalize_payee_name(registered, required=False)
    right = normalize_payee_name(presented, required=False)
    if not left or not right:
        return PAYEE_UNAVAILABLE
    if left == right:
        return PAYEE_MATCH
    if left.startswith(right) or right.startswith(left):
        return PAYEE_CLOSE
    left_letters = ''.join(_letter_chars(left)).casefold()
    right_letters = ''.join(_letter_chars(right)).casefold()
    if len(left_letters) >= 1 and len(right_letters) >= 1 and left_letters[0] == right_letters[0]:
        return PAYEE_CLOSE
    return PAYEE_NONE


def _reject_markup(text: str) -> None:
    lowered = text.lower()
    if (
        '<xml' in lowered or '<!doctype' in lowered or '<?xml' in lowered
        or '<ca1' in lowered or '<ietc' in lowered or '<lynx' in lowered
        or '<interac' in lowered or '<cpa5' in lowered or '<canpay' in lowered
        or '<uk1' in lowered or '<se1' in lowered
    ):
        raise CarecvError('invalid_file', 'XML/DOCTYPE CA1 files are rejected.')


def compose_ca1(
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
    currency: str = 'CAD',
    mask: bool = True,
) -> str:
    kind = normalize_rail(rail)
    clearing = CLEARING_BY_RAIL[kind]
    money = normalize_currency(currency, rail=kind)
    name_field = extra or ''
    if mask and name_field:
        name_field = mask_payee_name(name_field)
    return '|'.join((
        'CA1',
        CA1_VERSION,
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


def parse_ca1(text: Any) -> Dict[str, Any]:
    raw = str(text or '').strip()
    if not raw:
        raise CarecvError('invalid_file', 'CA1 record is empty.')
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != CA1_FIELDS or parts[0] != 'CA1' or parts[1] != CA1_VERSION:
        raise CarecvError('invalid_file', 'CA1 record is malformed.')
    clearing = parts[13].strip().upper()
    if clearing in REJECTED_CLEARING or clearing not in {'IETC', 'CPA5', 'LYNX'}:
        raise CarecvError('invalid_file', 'CA1 clearing must be IETC/CPA5/LYNX.')
    rail = normalize_rail(parts[3])
    if CLEARING_BY_RAIL[rail] != clearing:
        raise CarecvError('invalid_file', 'CA1 rail and clearing do not match.')
    joined = '|'.join(parts[2:9])
    if '*' in joined or '…' in joined or '***' in joined:
        raise CarecvError('invalid_file', 'Masked CA1 record cannot be re-ingested.')
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


def split_ca1_file(text: Any) -> List[str]:
    raw = str(text or '')
    _reject_markup(raw)
    records = [line.strip() for line in raw.replace('\r\n', '\n').split('\n') if line.strip()]
    if not records:
        raise CarecvError('invalid_file', 'CA1 file is empty.')
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
        or values.get('phone') or values.get('email') or values.get('cpa')
        or values.get('sin') or values.get('nid') or values.get('sort')
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
            raise CarecvError('invalid_amount', 'Amount is invalid.') from exc
    e2e = str(values.get('end_to_end_id') or values.get('e2e') or '').strip()
    rail = normalize_rail(values.get('rail') or 'inst')
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
    currency = normalize_currency(values.get('currency') or 'CAD', rail=rail)
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
        'name': name or 'CA PAYER',
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


def payments_canada_holidays(year: int) -> set:
    """Payments Canada closing days plus the weekday after a weekend-touching cluster."""
    easter = easter_date(year)
    victoria = date(year, 5, 24)
    while victoria.weekday() != 0:
        victoria -= timedelta(days=1)
    labour = date(year, 9, 1)
    while labour.weekday() != 0:
        labour += timedelta(days=1)
    thanksgiving = date(year, 10, 1)
    seen = 0
    while True:
        if thanksgiving.weekday() == 0:
            seen += 1
            if seen == 2:
                break
        thanksgiving += timedelta(days=1)
    raw = {
        date(year, 1, 1),
        easter - timedelta(days=2),
        victoria,
        date(year, 7, 1),
        labour,
        date(year, 9, 30),
        thanksgiving,
        date(year, 11, 11),
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


class CaClock:
    """ET Interac 24/7 vs EFT / Lynx banking-day clock."""

    def __init__(
        self,
        *,
        lynx_open_hour: int = 7,
        lynx_open_minute: int = 0,
        lynx_cutoff_hour: int = 18,
        lynx_cutoff_minute: int = 0,
        eft_open_hour: int = 9,
        eft_open_minute: int = 0,
        eft_cutoff_hour: int = 16,
        eft_cutoff_minute: int = 0,
        tz_offset_minutes: int = ET_OFFSET_MINUTES,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.lynx_open_hour = int(lynx_open_hour)
        self.lynx_open_minute = int(lynx_open_minute)
        self.lynx_cutoff_hour = int(lynx_cutoff_hour)
        self.lynx_cutoff_minute = int(lynx_cutoff_minute)
        self.eft_open_hour = int(eft_open_hour)
        self.eft_open_minute = int(eft_open_minute)
        self.eft_cutoff_hour = int(eft_cutoff_hour)
        self.eft_cutoff_minute = int(eft_cutoff_minute)
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
        return payments_canada_holidays(year) | {day for day in self.extra_holidays if day.year == year}

    def is_weekend(self, day: date) -> bool:
        return day.weekday() >= 5

    def is_holiday(self, day: date) -> bool:
        return day in self.holidays(day.year)

    def is_business_day(self, day: date) -> bool:
        return not self.is_weekend(day) and not self.is_holiday(day)

    def _window(self, rail: str) -> Tuple[int, int, int, int]:
        if rail == 'eft':
            return (
                self.eft_open_hour, self.eft_open_minute,
                self.eft_cutoff_hour, self.eft_cutoff_minute,
            )
        return (
            self.lynx_open_hour, self.lynx_open_minute,
            self.lynx_cutoff_hour, self.lynx_cutoff_minute,
        )

    def is_after_cutoff(self, ts: float, *, rail: str = 'inst') -> bool:
        kind = normalize_rail(rail)
        if kind == 'inst':
            return False
        local = self.local_dt(ts)
        day = local.date()
        if not self.is_business_day(day):
            return True
        open_h, open_m, cut_h, cut_m = self._window(kind)
        minutes = local.hour * 60 + local.minute
        return minutes < open_h * 60 + open_m or minutes >= cut_h * 60 + cut_m

    def is_session(self, ts: float, *, rail: str = 'lynx') -> bool:
        kind = normalize_rail(rail)
        if kind == 'inst':
            return True
        return not self.is_after_cutoff(ts, rail=kind)

    def next_business_day(self, day: date) -> date:
        cursor = day + timedelta(days=1)
        while not self.is_business_day(cursor):
            cursor += timedelta(days=1)
        return cursor

    def value_date(self, ts: float, *, rail: str = 'inst') -> date:
        kind = normalize_rail(rail)
        local = self.local_dt(ts)
        day = local.date()
        if kind == 'inst':
            return day
        if not self.is_business_day(day) or self.is_after_cutoff(ts, rail=kind):
            return self.next_business_day(day)
        return day

    def cycle_date(self, ts: float, *, rail: str = 'inst') -> str:
        return self.value_date(ts, rail=rail).strftime('%Y%m%d')

    def session_close_ts(self, day: date, *, rail: str = 'lynx') -> float:
        kind = normalize_rail(rail)
        if kind == 'eft':
            hour, minute = self.eft_cutoff_hour, self.eft_cutoff_minute
        else:
            hour, minute = self.lynx_cutoff_hour, self.lynx_cutoff_minute
        local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.tz)
        return local.timestamp()

    def snapshot(self, ts: float, *, rail: str = 'inst') -> Dict[str, Any]:
        kind = normalize_rail(rail)
        local = self.local_dt(ts)
        day = local.date()
        after = self.is_after_cutoff(ts, rail=kind)
        if kind == 'eft':
            cutoff = '%02d:%02d' % (self.eft_cutoff_hour, self.eft_cutoff_minute)
            open_at = '%02d:%02d' % (self.eft_open_hour, self.eft_open_minute)
        elif kind == 'lynx':
            cutoff = '%02d:%02d' % (self.lynx_cutoff_hour, self.lynx_cutoff_minute)
            open_at = '%02d:%02d' % (self.lynx_open_hour, self.lynx_open_minute)
        else:
            cutoff = '24/7'
            open_at = '00:00'
        return {
            'rail': kind,
            'local_date': day.isoformat(),
            'local_time': local.strftime('%H:%M'),
            'timezone': 'ET',
            'open': open_at,
            'cutoff': cutoff,
            'session': self.is_session(ts, rail=kind),
            'after_cutoff': after,
            'always_on': kind == 'inst',
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
class CarecvPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_claim: bool = True
    customer_return: bool = True
    allow_credit: bool = False
    max_keys: int = 12
    max_inbounds: int = 120
    min_amount: Decimal = Decimal('0.01')
    inst_cap: Decimal = Decimal('50000.00')
    lynx_cap: Decimal = Decimal('1000000.00')
    eft_cap: Decimal = Decimal('100000000.00')
    dual_control_threshold: Decimal = Decimal('50000.00')
    cadusd: Decimal = DEFAULT_CADUSD
    require_payee_match: bool = True
    payee_directory: Dict[str, str] = None  # type: ignore[assignment]
    tz_offset_minutes: int = ET_OFFSET_MINUTES
    lynx_open_hour: int = 7
    lynx_open_minute: int = 0
    lynx_cutoff_hour: int = 18
    lynx_cutoff_minute: int = 0
    eft_open_hour: int = 9
    eft_open_minute: int = 0
    eft_cutoff_hour: int = 16
    eft_cutoff_minute: int = 0
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
        if self.payee_directory is None:
            self.payee_directory = dict(DEFAULT_PAYEE_DIRECTORY)

    def cap_for(self, rail: str) -> Decimal:
        if rail == 'lynx':
            return self.lynx_cap
        if rail == 'eft':
            return self.eft_cap
        return self.inst_cap

    def rate_for(self, currency: str) -> Decimal:
        _ = currency
        return self.cadusd

    @classmethod
    def from_env(cls) -> 'CarecvPolicy':
        extra = _env_list('CARECV_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        source = os.environ.get('CARECV_RECEIVER') or DEFAULT_RECEIVER_BANK
        try:
            receiver = normalize_bank_code(source)
        except CarecvError:
            receiver = DEFAULT_RECEIVER_BANK
        return cls(
            enabled=_env_bool('CARECV_ENABLED', True),
            customer_manage=_env_bool('CARECV_CUSTOMER_MANAGE', True),
            customer_claim=_env_bool('CARECV_CUSTOMER_CLAIM', True),
            customer_return=_env_bool('CARECV_CUSTOMER_RETURN', True),
            allow_credit=_env_bool('CARECV_ALLOW_CREDIT', False),
            max_keys=max(1, _env_int('CARECV_MAX_KEYS', 12)),
            max_inbounds=max(1, _env_int('CARECV_MAX_INBOUNDS', 120)),
            min_amount=_env_money('CARECV_MIN_AMOUNT', '0.01'),
            inst_cap=_env_money('CARECV_INST_CAP', '50000.00'),
            lynx_cap=_env_money('CARECV_LYNX_CAP', '1000000.00'),
            eft_cap=_env_money('CARECV_EFT_CAP', '100000000.00'),
            dual_control_threshold=_env_money('CARECV_DUAL_CONTROL', '50000.00'),
            cadusd=_env_rate('CARECV_FX_CADUSD', '0.730000'),
            require_payee_match=_env_bool('CARECV_REQUIRE_PAYEE', True),
            tz_offset_minutes=_env_int('CARECV_TZ_OFFSET', ET_OFFSET_MINUTES),
            lynx_open_hour=max(0, min(23, _env_int('CARECV_LYNX_OPEN_HOUR', 7))),
            lynx_open_minute=max(0, min(59, _env_int('CARECV_LYNX_OPEN_MINUTE', 0))),
            lynx_cutoff_hour=max(0, min(23, _env_int('CARECV_LYNX_CUTOFF_HOUR', 18))),
            lynx_cutoff_minute=max(0, min(59, _env_int('CARECV_LYNX_CUTOFF_MINUTE', 0))),
            eft_open_hour=max(0, min(23, _env_int('CARECV_EFT_OPEN_HOUR', 9))),
            eft_open_minute=max(0, min(59, _env_int('CARECV_EFT_OPEN_MINUTE', 0))),
            eft_cutoff_hour=max(0, min(23, _env_int('CARECV_EFT_CUTOFF_HOUR', 16))),
            eft_cutoff_minute=max(0, min(59, _env_int('CARECV_EFT_CUTOFF_MINUTE', 0))),
            receiver_bank=receiver,
            customer_return_seconds=max(60, _env_int('CARECV_RETURN_SECONDS', CUSTOMER_RETURN_SECONDS)),
            staff_complaint_days=max(1, _env_int('CARECV_COMPLAINT_DAYS', STAFF_COMPLAINT_DAYS)),
            claim_seconds=max(60, _env_int('CARECV_CLAIM_SECONDS', CLAIM_SECONDS)),
            watchlist=watch,
            extra_holidays=_env_list('CARECV_HOLIDAYS'),
        )


@dataclass
class CarecvKey:
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
class CarecvInbound:
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
    payee_outcome: str = ''
    payee_overridden: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'inbound_id': self.inbound_id,
            'e2e_masked': mask_e2e(self.e2e),
            'userid': self.userid,
            'account_last4': last4(self.destination_account) if self.destination_account else '',
            'amount_instructed': self.amount_instructed,
            'amount_usd': self.amount_usd,
            'amount_cad': self.amount_instructed if self.currency == 'CAD' else '',
            'currency': self.currency,
            'sender_name': self.sender_name,
            'send_bank_masked': mask_bank(self.send_bank) if self.send_bank else '',
            'send_name': bank_name(self.send_bank) if self.send_bank else '',
            'recv_bank_masked': mask_bank(self.recv_bank) if self.recv_bank else '',
            'rail': self.rail,
            'proxy_type': self.proxy_type,
            'proxy_masked': mask_proxy(self.proxy_type, self.proxy) if self.proxy else '****',
            'payee_masked': mask_payee_name(self.payee_name) if self.payee_name else '',
            'payee_outcome': self.payee_outcome,
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


def _key_from_row(row: Any) -> CarecvKey:
    data = dict(row)
    return CarecvKey(
        key_id=data['key_id'], userid=data['userid'], nickname=data['nickname'],
        proxy_type=data['proxy_type'], proxy=data['proxy'], fingerprint=data['fingerprint'],
        destination_account=data['destination_account'], status=data['status'], actor=data['actor'],
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
    )


def _inbound_from_row(row: Any) -> CarecvInbound:
    data = dict(row)
    return CarecvInbound(
        inbound_id=data['inbound_id'], e2e=data['e2e'], userid=data['userid'],
        destination_account=data['destination_account'], amount_instructed=data['amount_instructed'],
        amount_usd=data['amount_usd'], currency=data.get('currency') or 'CAD',
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
        payee_name=data.get('payee_name') or '', payee_outcome=data.get('payee_outcome') or '',
        payee_overridden=bool(int(data.get('payee_overridden') or 0)),
    )


class MemoryCarecvStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.keys: Dict[str, CarecvKey] = {}
        self.inbounds: Dict[str, CarecvInbound] = {}
        self._by_e2e: Dict[str, str] = {}

    def put_key(self, row: CarecvKey) -> None:
        with self._lock:
            self.keys[row.key_id] = row

    def get_key(self, key_id: str) -> Optional[CarecvKey]:
        return self.keys.get(key_id)

    def get_key_by_fingerprint(self, fingerprint: str) -> Optional[CarecvKey]:
        matches = [
            row for row in self.keys.values()
            if row.fingerprint == fingerprint and row.status in OPEN_KEY
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda row: row.created_at, reverse=True)[0]

    def update_key(self, row: CarecvKey) -> None:
        self.put_key(row)

    def list_keys(self, userid: Optional[str] = None) -> List[CarecvKey]:
        rows = list(self.keys.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        return sorted(rows, key=lambda row: row.created_at)

    def put_inbound(self, row: CarecvInbound) -> None:
        with self._lock:
            self.inbounds[row.inbound_id] = row
            self._by_e2e[row.e2e] = row.inbound_id

    def get_inbound(self, inbound_id: str) -> Optional[CarecvInbound]:
        return self.inbounds.get(inbound_id)

    def get_by_e2e(self, e2e: str) -> Optional[CarecvInbound]:
        inbound_id = self._by_e2e.get(e2e)
        return self.inbounds.get(inbound_id) if inbound_id else None

    def update_inbound(self, row: CarecvInbound) -> None:
        self.put_inbound(row)

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[CarecvInbound]:
        rows = list(self.inbounds.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)


class SqliteCarecvStore:
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
                    currency TEXT NOT NULL DEFAULT 'CAD',
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
                    payee_outcome TEXT NOT NULL DEFAULT '',
                    payee_overridden INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()

    def put_key(self, row: CarecvKey) -> None:
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

    def get_key(self, key_id: str) -> Optional[CarecvKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM keys WHERE key_id = ?', (key_id,)).fetchone()
        return _key_from_row(row) if row else None

    def get_key_by_fingerprint(self, fingerprint: str) -> Optional[CarecvKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM keys WHERE fingerprint = ? AND status IN ('active', 'paused')
                ORDER BY created_at DESC LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
        return _key_from_row(row) if row else None

    def update_key(self, row: CarecvKey) -> None:
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

    def list_keys(self, userid: Optional[str] = None) -> List[CarecvKey]:
        sql = 'SELECT * FROM keys'
        args: Tuple[Any, ...] = ()
        if userid is not None:
            sql += ' WHERE userid = ?'
            args = (userid,)
        sql += ' ORDER BY created_at'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_key_from_row(row) for row in rows]

    def put_inbound(self, row: CarecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inbounds (
                    inbound_id, e2e, userid, destination_account, amount_instructed, amount_usd,
                    currency, sender_name, send_bank, recv_bank, rail, proxy_type, proxy, fingerprint,
                    account_number, purpose, status, reason, actor, releaser, ofac_hit,
                    ofac_match, ofac_overridden, created_at, updated_at, posted_at,
                    returned_at, expiry_at, note, payee_name, payee_outcome, payee_overridden
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.inbound_id, row.e2e, row.userid, row.destination_account, row.amount_instructed,
                    row.amount_usd, row.currency, row.sender_name, row.send_bank, row.recv_bank, row.rail,
                    row.proxy_type, row.proxy, row.fingerprint, row.account_number, row.purpose,
                    row.status, row.reason, row.actor, row.releaser, int(row.ofac_hit),
                    row.ofac_match, int(row.ofac_overridden), row.created_at, row.updated_at,
                    row.posted_at, row.returned_at, row.expiry_at, row.note,
                    row.payee_name, row.payee_outcome, int(row.payee_overridden),
                ),
            )
            conn.commit()

    def get_inbound(self, inbound_id: str) -> Optional[CarecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,)).fetchone()
        return _inbound_from_row(row) if row else None

    def get_by_e2e(self, e2e: str) -> Optional[CarecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE e2e = ?', (e2e,)).fetchone()
        return _inbound_from_row(row) if row else None

    def update_inbound(self, row: CarecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE inbounds SET userid=?, destination_account=?, amount_instructed=?, amount_usd=?,
                    currency=?, sender_name=?, send_bank=?, recv_bank=?, rail=?, proxy_type=?, proxy=?,
                    fingerprint=?, account_number=?, purpose=?, status=?, reason=?, actor=?,
                    releaser=?, ofac_hit=?, ofac_match=?, ofac_overridden=?, updated_at=?,
                    posted_at=?, returned_at=?, expiry_at=?, note=?, payee_name=?, payee_outcome=?,
                    payee_overridden=?
                WHERE inbound_id=?
                """,
                (
                    row.userid, row.destination_account, row.amount_instructed, row.amount_usd,
                    row.currency, row.sender_name, row.send_bank, row.recv_bank, row.rail, row.proxy_type,
                    row.proxy, row.fingerprint, row.account_number, row.purpose, row.status,
                    row.reason, row.actor, row.releaser, int(row.ofac_hit), row.ofac_match,
                    int(row.ofac_overridden), row.updated_at, row.posted_at, row.returned_at,
                    row.expiry_at, row.note, row.payee_name, row.payee_outcome,
                    int(row.payee_overridden), row.inbound_id,
                ),
            )
            conn.commit()

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[CarecvInbound]:
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


class CarecvService:
    def __init__(
        self,
        policy: CarecvPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[CaClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: datetime.now(tz=timezone.utc).timestamp())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn or screen_name
        self.calendar = calendar or CaClock(
            lynx_open_hour=policy.lynx_open_hour,
            lynx_open_minute=policy.lynx_open_minute,
            lynx_cutoff_hour=policy.lynx_cutoff_hour,
            lynx_cutoff_minute=policy.lynx_cutoff_minute,
            eft_open_hour=policy.eft_open_hour,
            eft_open_minute=policy.eft_open_minute,
            eft_cutoff_hour=policy.eft_cutoff_hour,
            eft_cutoff_minute=policy.eft_cutoff_minute,
            tz_offset_minutes=policy.tz_offset_minutes,
            extra_holidays=policy.extra_holidays,
        )

    def _now(self) -> float:
        return float(self.clock())

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise CarecvError('carecv_disabled', 'Interac receive is disabled.')

    def _require_staff(self, actor_type: str) -> None:
        if actor_type not in EMPLOYEE_ROLES:
            raise CarecvError('carecv_forbidden', 'Staff only.')

    def _require_customer_manage(self, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if not self.policy.customer_manage:
            raise CarecvError('carecv_forbidden', 'Customers cannot manage Interac proxies.')

    def _accounts(self, userid: str) -> Any:
        if not self.accounts_fn:
            return {}
        return self.accounts_fn(userid) or {}

    def _owned(self, userid: str, account: str) -> None:
        owned = own_accounts_from_customer_payload(self._accounts(userid))
        if account not in owned:
            raise CarecvError('invalid_account', 'Destination is not an account of this customer.')

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
    ) -> CarecvKey:
        self._require_enabled()
        self._require_customer_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise CarecvError('carecv_forbidden', 'Cannot enroll a Interac proxy for another customer.')
        kind, value = normalize_proxy(proxy_type, proxy, required=True)
        account = normalize_account(destination_account)
        self._owned(owner_userid, account)
        if not self._credit_allowed(owner_userid, account):
            raise CarecvError('credit_not_allowed', 'Credit accounts cannot receive Interac credits.')
        fingerprint = proxy_fingerprint(kind, value)
        existing = self.store.get_key_by_fingerprint(fingerprint)
        if existing and existing.status in OPEN_KEY:
            raise CarecvError('key_duplicate', 'That Interac proxy is already enrolled.')
        open_rows = [row for row in self.store.list_keys(owner_userid) if row.status in OPEN_KEY]
        if any(row.nickname.lower() == normalize_nickname(nickname).lower() for row in open_rows):
            raise CarecvError('key_duplicate', 'That nickname is already in use.')
        if len(open_rows) >= self.policy.max_keys:
            raise CarecvError('key_limit', 'Interac enrollment limit reached.')
        now = self._now()
        row = CarecvKey(
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
    ) -> CarecvKey:
        self._require_enabled()
        row = self.store.get_key(key_id)
        if row is None:
            raise CarecvError('key_not_found', 'Interac enrollment not found.')
        if actor_type not in EMPLOYEE_ROLES and actor != row.userid:
            raise CarecvError('carecv_forbidden', 'Cannot change another customer Interac proxy.')
        self._require_customer_manage(actor_type)
        if status not in KEY_STATUSES:
            raise CarecvError('invalid_status', 'Unknown Interac status.')
        if row.status == KEY_CANCELLED:
            raise CarecvError('already_cancelled', 'Cancelled Interac proxies cannot change.')
        if status == KEY_PAUSED and row.status != KEY_ACTIVE:
            raise CarecvError('already_paused' if row.status == KEY_PAUSED else 'invalid_status', 'Interac proxy is not active.')
        if status == KEY_ACTIVE and row.status != KEY_PAUSED:
            raise CarecvError('already_active', 'Interac proxy is already active.')
        if status == row.status:
            code = 'already_paused' if status == KEY_PAUSED else 'already_active'
            raise CarecvError(code, 'Interac proxy already in that status.')
        row.status = status
        row.actor = actor
        row.updated_at = self._now()
        self.store.update_key(row)
        if status == KEY_ACTIVE:
            self._rematch_unmatched(row)
        return row

    def _rematch_unmatched(self, enrollment: CarecvKey) -> None:
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
    ) -> Tuple[CarecvInbound, bool]:
        self._require_enabled()
        self._require_staff(actor_type)
        payload = dict(values or {})
        if not payload.get('end_to_end_id') and not payload.get('e2e') and not payload.get('cycle_date') and not payload.get('value_date'):
            payload['cycle_date'] = self.calendar.cycle_date(
                self._now(), rail=normalize_rail(payload.get('rail') or 'inst'),
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
    ) -> Tuple[List[CarecvInbound], int]:
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
        for record in split_ca1_file(payload):
            parsed = parse_ca1(record)
            parsed['recv_bank'] = parsed.get('recv_bank') or parsed.get('bank') or self.policy.receiver_bank
            parsed['send_bank'] = sender or parsed.get('send_bank') or DEFAULT_SENDER_BANK
            if fallback:
                parsed['name'] = fallback
            elif not parsed.get('name'):
                parsed['name'] = 'CA PAYER'
            row, is_new = self._ingest_parsed(actor=actor, parsed=parsed)
            rows.append(row)
            if is_new:
                created += 1
        return rows, created

    def _ingest_parsed(self, *, actor: str, parsed: Dict[str, Any]) -> Tuple[CarecvInbound, bool]:
        recv = parsed.get('recv_bank') or parsed.get('bank') or self.policy.receiver_bank
        if recv != self.policy.receiver_bank:
            raise CarecvError('wrong_receiver', 'Credit is not for this CPA routing.')
        existing = self.store.get_by_e2e(parsed['e2e'])
        if existing is not None:
            return existing, False
        total = len(self.store.list_inbounds())
        if total >= self.policy.max_inbounds:
            raise CarecvError('inbound_limit', 'Inbound Interac limit reached.')
        amount = parsed['amount'] if isinstance(parsed['amount'], Decimal) else parse_money(parsed['amount'])
        rail = normalize_rail(parsed.get('rail') or 'inst')
        cap = self.policy.cap_for(rail)
        if amount < self.policy.min_amount or amount > cap:
            code = '%s_amount_exceeded' % rail if amount > cap else 'amount_out_of_range'
            raise CarecvError(code, 'Amount is outside the %s range.' % rail)
        now = self._now()
        currency = normalize_currency(parsed.get('currency') or 'CAD', rail=rail)
        proxy = parsed.get('proxy') or ''
        kind = parsed.get('proxy_type') or ''
        fingerprint = proxy_fingerprint(kind, proxy) if proxy else ''
        usd = convert_instructed_to_usd(amount, self.policy.rate_for(currency))
        row = CarecvInbound(
            inbound_id=uuid.uuid4().hex,
            e2e=parsed['e2e'],
            userid='',
            destination_account='',
            amount_instructed=money_str(amount),
            amount_usd=money_str(usd),
            currency=currency,
            sender_name=parsed.get('name') or 'CA PAYER',
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

    def _evaluate(self, row: CarecvInbound, *, rescreen: bool) -> CarecvInbound:
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
        except CarecvError:
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
        if self.policy.require_payee_match and not row.payee_overridden:
            registered = ''
            if row.proxy:
                registered = (self.policy.payee_directory or {}).get(proxy_key(row.proxy_type, row.proxy), '')
            outcome = payee_name_outcome(registered, row.payee_name) if (registered or row.payee_name) else PAYEE_UNAVAILABLE
            row.payee_outcome = outcome
            if outcome == PAYEE_NONE:
                row.status = IN_HELD
                row.reason = 'payee'
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
        if row.rail in {'lynx', 'eft'} and self.calendar.is_after_cutoff(now, rail=row.rail):
            row.status = IN_QUEUED
            row.reason = 'lynx_session' if row.rail == 'lynx' else 'eft_session'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        return self._post(row)

    def _post(self, row: CarecvInbound) -> CarecvInbound:
        if self.credit_fn is None:
            raise CarecvError('failed', 'Credit function is not configured.')
        remark = '%s from %s' % (row.rail, row.sender_name[:40])
        result = self.credit_fn(row.destination_account, row.amount_usd, remark)
        classified = _classify_money_result(result)
        now = self._now()
        if classified == 'nsf':
            row.status = IN_FAILED
            row.reason = 'nsf'
            row.updated_at = now
            self.store.update_inbound(row)
            raise CarecvError('nsf', 'Credit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            row.status = IN_FAILED
            row.reason = 'failed'
            row.updated_at = now
            self.store.update_inbound(row)
            raise CarecvError('failed', 'Credit failed.', inbound=row)
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

    def _get(self, *, inbound_id: str = '', e2e: str = '') -> CarecvInbound:
        row = None
        if inbound_id:
            row = self.store.get_inbound(str(inbound_id).strip())
        if row is None and e2e:
            row = self.store.get_by_e2e(normalize_e2e(e2e))
        if row is None:
            raise CarecvError('inbound_not_found', 'Inbound Interac credit not found.')
        return row

    def _assert_visible(self, row: CarecvInbound, actor: str, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if row.userid and row.userid != actor:
            raise CarecvError('carecv_forbidden', 'Cannot view another customer Interac credit.')

    def assign(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        customer_id: str,
        account: Any,
        nickname: str = '',
    ) -> CarecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_UNMATCHED:
            raise CarecvError('not_assignable', 'Only unmatched Interac credits can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise CarecvError('missing_customer_id', 'Customer id is required to assign.')
        dest = normalize_account(account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise CarecvError('credit_not_allowed', 'Credit accounts cannot receive Interac credits.')
        if row.proxy:
            existing = self.store.get_key_by_fingerprint(row.fingerprint)
            if existing is None or existing.status == KEY_CANCELLED:
                self.add_key(
                    owner_userid=owner, actor=actor, actor_type=actor_type,
                    nickname=nickname or ('Interac %s' % (row.proxy_type or 'proxy')),
                    proxy_type=row.proxy_type, proxy=row.proxy, destination_account=dest,
                )
                fresh = self.store.get_inbound(row.inbound_id)
                if fresh is not None and fresh.status != IN_UNMATCHED:
                    return fresh
            elif existing.userid != owner:
                raise CarecvError('key_duplicate', 'Interac proxy belongs to another customer.')
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
    ) -> CarecvInbound:
        """Interac claim: customer presents the matching proxy for an unmatched credit."""
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_claim:
                raise CarecvError('carecv_forbidden', 'Customers cannot claim Interac credits.')
            owner = actor
        else:
            owner = str(owner_userid or '').strip()
            if not owner:
                raise CarecvError('missing_customer_id', 'Customer id is required to claim.')
        if not proxy:
            raise CarecvError('missing_key', 'Interac proxy is required to claim.')
        kind, value = normalize_proxy(proxy_type, proxy, required=True)
        fingerprint = proxy_fingerprint(kind, value)
        dest = normalize_account(destination_account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise CarecvError('credit_not_allowed', 'Credit accounts cannot receive Interac credits.')
        row = None
        if inbound_id:
            row = self._get(inbound_id=inbound_id)
            if row.fingerprint != fingerprint:
                raise CarecvError('not_claimable', 'Presented proxy does not match this credit.')
        else:
            for candidate in self.store.list_inbounds(status=IN_UNMATCHED):
                if candidate.fingerprint == fingerprint:
                    row = candidate
                    break
        if row is None:
            raise CarecvError('inbound_not_found', 'No unmatched Interac credit for that proxy.')
        if row.status != IN_UNMATCHED:
            raise CarecvError('not_claimable', 'This Interac credit is not waiting for a claim.')
        if row.expiry_at and self._now() > row.expiry_at:
            row.status = IN_EXPIRED
            row.reason = 'expired'
            row.updated_at = self._now()
            self.store.update_inbound(row)
            raise CarecvError('return_window_closed', 'Interac claim window has closed.')
        existing = self.store.get_key_by_fingerprint(fingerprint)
        if existing is None or existing.status == KEY_CANCELLED:
            self.add_key(
                owner_userid=owner, actor=actor, actor_type=actor_type,
                nickname=nickname or ('Interac %s' % kind),
                proxy_type=kind, proxy=value, destination_account=dest,
            )
        elif existing.userid != owner:
            raise CarecvError('key_duplicate', 'Interac proxy belongs to another customer.')
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

    def override_ofac(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> CarecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_HELD or row.reason != 'ofac':
            raise CarecvError('not_overridable', 'Only OFAC-held Interac credits can be overridden.')
        row.ofac_overridden = True
        row.ofac_hit = False
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def override_payee(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> CarecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_HELD or row.reason != 'payee':
            raise CarecvError('not_overridable', 'Only Name-inquiry-held credits can be overridden.')
        row.payee_overridden = True
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def release(self, *, inbound_id: str, actor: str, actor_type: str) -> CarecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_PENDING:
            raise CarecvError('not_releasable', 'Only dual-control Interac credits can be released.')
        if actor == row.actor:
            raise CarecvError('same_approver', 'A different employee must release this credit.')
        row.releaser = actor
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def reject(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> CarecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status not in OPEN_IN:
            raise CarecvError('not_rejectable', 'This Interac credit cannot be rejected.')
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
            raise CarecvError('not_exportable', 'Interac credit has no End-to-End ID to export.')
        _ = actor
        record = compose_ca1(
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
    ) -> CarecvInbound:
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
    ) -> CarecvInbound:
        self._require_enabled()
        row = self._get(inbound_id=inbound_id)
        self._assert_visible(row, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_return:
                raise CarecvError('carecv_forbidden', 'Customers cannot return Interac credits.')
            if actor != row.userid:
                raise CarecvError('carecv_forbidden', 'Cannot return another customer Interac credit.')
        return self._return(row, actor=actor, actor_type=actor_type, reason=reason, honor_window=True)

    def _customer_deadline(self, row: CarecvInbound) -> float:
        posted = row.posted_at or row.created_at
        if row.rail in {'lynx', 'eft'}:
            day = self.calendar.local_dt(posted).date()
            return self.calendar.session_close_ts(self.calendar.next_business_day(day), rail=row.rail)
        return posted + self.policy.customer_return_seconds

    def _return(
        self,
        row: CarecvInbound,
        *,
        actor: str,
        actor_type: str,
        reason: str,
        honor_window: bool,
    ) -> CarecvInbound:
        now = self._now()
        _ = actor
        if row.status == IN_RETURNED or row.returned_at:
            raise CarecvError('already_returned', 'Interac credit already returned.')
        if row.status in OPEN_IN:
            row.status = IN_RETURNED
            row.reason = reason or 'cust'
            row.returned_at = now
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if row.status != IN_POSTED:
            raise CarecvError('not_returnable', 'This Interac credit cannot be returned.')
        if honor_window and actor_type not in EMPLOYEE_ROLES:
            if now > self._customer_deadline(row):
                raise CarecvError('return_window_closed', 'Customer Interac return window has closed.')
        if actor_type in EMPLOYEE_ROLES:
            elapsed = now - (row.posted_at or row.created_at)
            if elapsed > self.policy.staff_complaint_days * 86400:
                raise CarecvError('med_window_closed', 'Staff complaint window is 90 days.')
        if self.debit_fn is None:
            raise CarecvError('return_failed', 'Debit function is not configured.')
        remark = '%s return %s' % (row.rail, row.e2e[-12:])
        result = self.debit_fn(row.destination_account, row.amount_usd, remark)
        classified = _classify_money_result(result)
        if classified == 'nsf':
            raise CarecvError('nsf', 'Return debit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            raise CarecvError('return_failed', 'Return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.reason = reason or 'cust'
        row.returned_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[CarecvInbound]:
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
            'clock': self.calendar.snapshot(now, rail='inst'),
            'receiver': mask_bank(self.policy.receiver_bank),
            'keys': [row.to_dict() for row in keys[:40]],
            'inbounds': [row.to_dict() for row in inbounds[:40]],
            'ytd_received': money_str(ytd),
            'open_count': sum(1 for row in inbounds if row.status in OPEN_IN),
            'active_keys': sum(1 for row in keys if row.status == KEY_ACTIVE),
            'min_amount': money_str(self.policy.min_amount),
            'inst_cap': money_str(self.policy.inst_cap),
            'lynx_cap': money_str(self.policy.lynx_cap),
            'eft_cap': money_str(self.policy.eft_cap),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clearing': 'ca1',
            'currency': 'CAD',
            'fx_cadusd': rate_str(self.policy.cadusd),
        }

    def unmatched_snapshot(self) -> Dict[str, Any]:
        rows = self.store.list_inbounds(status=IN_UNMATCHED)
        return {
            'enabled': self.policy.enabled,
            'unmatched': [row.to_dict() for row in rows[:40]],
            'count': len(rows),
        }


_SERVICE: Optional[CarecvService] = None


def set_service(service: Optional[CarecvService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[CarecvService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('CARECV_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryCarecvStore()
    path = os.environ.get('CARECV_DB', DEFAULT_STORE_PATH)
    return SqliteCarecvStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[CaClock] = None,
) -> CarecvService:
    if store is None:
        store = default_store()
    return CarecvService(
        CarecvPolicy.from_env(),
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
        'carecv_forbidden': 403,
        'carecv_disabled': 403,
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
        'invalid_cpa': 400,
        'invalid_sin': 400,
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
        'inst_amount_exceeded': 400,
        'lynx_amount_exceeded': 400,
        'eft_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_key': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: CarecvError) -> Dict[str, Any]:
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
    except CarecvError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snapshot_for(service: CarecvService, userid: str, actor: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(userid, actor=actor, actor_type=actor_type)


def handle_list(service: CarecvService):
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
    return jsonify({'CanPayIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def handle_unmatched(service: CarecvService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'carecv_forbidden'}), 403
    snap = service.unmatched_snapshot()
    return jsonify({'UnmatchedCanPayIns': snap, 'CanPayIn': snap}), 200


def handle_add_key(service: CarecvService):
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
                raise CarecvError('missing_customer_id', 'Customer id is required.')
        row = service.add_key(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname') or '',
            proxy_type=values.get('proxy_type') or values.get('key_type') or values.get('kind'),
            proxy=values.get('proxy') or values.get('iban') or values.get('key') or values.get('phone') or values.get('email') or values.get('cpa') or values.get('sin') or values.get('nid'),
            destination_account=values.get('destination_account') or values.get('account') or values.get('default_account'),
        )
        return jsonify({
            'message': 'Interac proxy enrolled',
            'key': row.to_dict(),
            'CanPayIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _key_status_route(service: CarecvService, status: str, message: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        key_id = str(values.get('key_id') or '').strip()
        if not key_id:
            raise CarecvError('missing_key', 'key_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.set_key_status(key_id=key_id, actor=userid, actor_type=actor_type, status=status)
        return jsonify({
            'message': message,
            'key': row.to_dict(),
            'CanPayIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: CarecvService):
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
        return jsonify({'preview': preview, 'CanPayIn': _snapshot_for(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest(service: CarecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'Interac ingested' if created else 'Interac already ingested',
            'inbound': row.to_dict(),
            'CanPayIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_ingest_file(service: CarecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        payload = values.get('file') or values.get('body') or values.get('payload')
        if not payload:
            raise CarecvError('missing_file', 'CA1 file body is required.')
        actor_type = session.get('usertype') or 'customer'
        rows, created = service.ingest_file(
            actor=userid, actor_type=actor_type, payload=str(payload),
            sender_name=str(values.get('name') or values.get('sender_name') or ''),
            send_bank=str(values.get('send_bank') or values.get('sender_bank') or ''),
        )
        owner = rows[-1].userid if rows and rows[-1].userid else userid
        return jsonify({
            'message': 'CA1 file ingested',
            'created': created,
            'inbounds': [row.to_dict() for row in rows],
            'CanPayIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_assign(service: CarecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise CarecvError('missing_inbound', 'inbound_id is required.')
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
            'message': 'Interac assigned',
            'inbound': row.to_dict(),
            'CanPayIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_claim(service: CarecvService):
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
            proxy=values.get('proxy') or values.get('iban') or values.get('key') or values.get('phone') or values.get('email') or values.get('cpa') or values.get('sin') or values.get('nid'),
            destination_account=values.get('destination_account') or values.get('account'),
            nickname=str(values.get('nickname') or ''),
            owner_userid=str(values.get('customer_id') or ''),
        )
        return jsonify({
            'message': 'Interac claimed',
            'inbound': row.to_dict(),
            'CanPayIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_inbound_route(service: CarecvService, action: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise CarecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        if action == 'override':
            row = service.override_ofac(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'payee':
            row = service.override_payee(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Name inquiry hold overridden'
        elif action == 'release':
            row = service.release(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            message = 'Interac released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Interac rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=str(values.get('reason') or 'cust'),
            )
            message = 'Interac returned'
        elif action == 'export':
            exported = service.export(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            owner = exported['inbound'].get('userid') or userid
            return jsonify({
                'message': 'CA1 exported',
                'file': exported['file'],
                'inbound': exported['inbound'],
                'CanPayIn': _snapshot_for(service, owner, userid, actor_type),
            }), 200
        else:
            raise CarecvError('invalid_status', 'Unknown Interac action.')
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'CanPayIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: CarecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise CarecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.request_return(
            inbound_id=inbound_id, actor=userid, actor_type=actor_type,
            reason=str(values.get('reason') or 'cust'),
        )
        return jsonify({
            'message': 'Interac return requested',
            'inbound': row.to_dict(),
            'CanPayIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: CarecvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'CanPayIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def attach_carecv_routes(app, service: CarecvService) -> None:
    @app.route('/listCanPayIns', methods=['POST', 'GET'])
    def list_canpay_ins_route():
        return handle_list(service)

    @app.route('/listCanPayRecvKeys', methods=['POST', 'GET'])
    def list_canpay_recv_keys_route():
        return handle_list(service)

    @app.route('/listUnmatchedCanPayIns', methods=['POST', 'GET'])
    def list_unmatched_canpay_ins_route():
        return handle_unmatched(service)

    @app.route('/addCanPayRecvKey', methods=['POST', 'GET'])
    def add_canpay_recv_key_route():
        return handle_add_key(service)

    @app.route('/pauseCanPayRecvKey', methods=['POST', 'GET'])
    def pause_canpay_recv_key_route():
        return _key_status_route(service, KEY_PAUSED, 'Interac proxy paused')

    @app.route('/resumeCanPayRecvKey', methods=['POST', 'GET'])
    def resume_canpay_recv_key_route():
        return _key_status_route(service, KEY_ACTIVE, 'Interac proxy resumed')

    @app.route('/cancelCanPayRecvKey', methods=['POST', 'GET'])
    def cancel_canpay_recv_key_route():
        return _key_status_route(service, KEY_CANCELLED, 'Interac proxy cancelled')

    @app.route('/previewCanPayIn', methods=['POST', 'GET'])
    def preview_canpay_in_route():
        return handle_preview(service)

    @app.route('/ingestCanPayIn', methods=['POST', 'GET'])
    def ingest_canpay_in_route():
        return handle_ingest(service)

    @app.route('/ingestCanPayInFile', methods=['POST', 'GET'])
    def ingest_canpay_in_file_route():
        return handle_ingest_file(service)

    @app.route('/claimCanPayIn', methods=['POST', 'GET'])
    def claim_canpay_in_route():
        return handle_claim(service)

    @app.route('/assignCanPayIn', methods=['POST', 'GET'])
    def assign_canpay_in_route():
        return handle_assign(service)

    @app.route('/overrideCanPayInOfac', methods=['POST', 'GET'])
    def override_payee_in_ofac_route():
        return _staff_inbound_route(service, 'override')

    @app.route('/overrideCanPayInPayee', methods=['POST', 'GET'])
    def override_payee_in_canpay_route():
        return _staff_inbound_route(service, 'payee')

    @app.route('/releaseCanPayIn', methods=['POST', 'GET'])
    def release_canpay_in_route():
        return _staff_inbound_route(service, 'release')

    @app.route('/rejectCanPayIn', methods=['POST', 'GET'])
    def reject_canpay_in_route():
        return _staff_inbound_route(service, 'reject')

    @app.route('/returnCanPayIn', methods=['POST', 'GET'])
    def return_canpay_in_route():
        return _staff_inbound_route(service, 'return')

    @app.route('/exportCanPayIn', methods=['POST', 'GET'])
    def export_canpay_in_route():
        return _staff_inbound_route(service, 'export')

    @app.route('/requestCanPayInReturn', methods=['POST', 'GET'])
    def request_canpay_in_return_route():
        return handle_request_return(service)

    @app.route('/runDueCanPayIns', methods=['POST', 'GET'])
    def run_due_canpay_ins_route():
        return handle_run_due(service)
