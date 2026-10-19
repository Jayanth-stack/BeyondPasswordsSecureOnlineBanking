"""Inbound PIX receive posting (BRL Dict keys / SPI / DICT claim / MED).

Staff ingest incoming PIX credits. Recipients are enrolled Dict keys
(CPF, CNPJ, email, phone, EVP) at this bank's 8-digit ISPB. Matched
credits post instantly on SPI (24/7). Unmatched keys wait for a DICT
claim or staff assignment. Independent of outbound PIX origination
(PR #137), SWIFT gpi (PR #135), inbound CHIPS (PR #133), Zelle, ACH
linking (PR #68), and domestic Fedwire (PR #73). Existing
`/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay unchanged.

Foundations (reusable beyond this screen):
- ISPB (8-digit) + Brazilian participant directory
- Dict key normalize / mask / SHA-256 fingerprint (CPF/CNPJ checksums)
- Bacen EndToEndId `E{ISPB}{YYYYMMDD}{15}` uniqueness
- SPI 24/7 clock with Brazil holidays and 20:00–06:00 BRT night window
- PIX1 pipe compose/parse (XML/DOCTYPE rejected)
- DICT claim (customer presents matching key; enroll rematches unmatched)
- MED windows (80-minute customer / 90-day staff)
- Dual-control at R$10k

Reuses Fedwire OFAC screening, money parsing, and account directory
from `utility.wire`. Stores are pluggable (memory for tests, sqlite WAL
for restart-safe default). Full Dict key / fingerprint / EndToEndId /
destination account never appear in to_dict / snapshots.
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
IN_PENDING = 'pending_release'
IN_POSTED = 'posted'
IN_REJECTED = 'rejected'
IN_RETURNED = 'returned'
IN_EXPIRED = 'expired'
IN_FAILED = 'failed'
IN_STATUSES = frozenset({
    IN_UNMATCHED, IN_HELD, IN_PENDING, IN_POSTED, IN_REJECTED,
    IN_RETURNED, IN_EXPIRED, IN_FAILED,
})
OPEN_IN = frozenset({IN_UNMATCHED, IN_HELD, IN_PENDING})
PURPOSES = frozenset({'family', 'goods', 'payroll', 'tax', 'loan', 'rent', 'other'})
PURPOSE_ALIASES = {
    'personal': 'family', 'gift': 'family', 'support': 'family',
    'invoice': 'goods', 'purchase': 'goods', 'vendor': 'goods',
    'salary': 'payroll', 'wage': 'payroll',
    'irs': 'tax', 'taxes': 'tax',
    'mortgage': 'loan', 'housing': 'rent',
}
KEY_TYPES = frozenset({'cpf', 'cnpj', 'email', 'phone', 'evp'})
KEY_TYPE_ALIASES = {
    'tax_id': 'cpf', 'cpf_cnpj': 'cpf', 'tin': 'cpf',
    'company': 'cnpj', 'cnpj_cpf': 'cnpj',
    'e-mail': 'email', 'mail': 'email',
    'sms': 'phone', 'mobile': 'phone', 'cel': 'phone', 'telefone': 'phone',
    'random': 'evp', 'uuid': 'evp', 'aleatoria': 'evp',
}
DEFAULT_PARTICIPANTS = {
    '00360305': 'CAIXA ECONOMICA FEDERAL',
    '60701190': 'ITAU UNIBANCO',
    '60746948': 'BRADESCO',
    '90400888': 'SANTANDER BRASIL',
    '00000000': 'BACEN',
    '36030586': 'BANCO INTER',
    '58160789': 'BANCO SAFRA',
    '00000208': 'BRB',
}
SETTLEMENT_SPI = 'spi'
PIX1_VERSION = '1'
PIX1_FIELDS = 12
DEFAULT_RECEIVER = '00360305'
DEFAULT_STORE_PATH = 'SystemLogs/precv.sqlite'
CUSTOMER_RETURN_SECONDS = 80 * 60
STAFF_MED_DAYS = 90
CLAIM_SECONDS = 24 * 60 * 60
CREDIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
CREDIT_NSF = ('insufficient',)


class PrecvError(ValueError):
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
        raise PrecvError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise PrecvError('invalid_name', 'Sender legal name must be 2-80 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise PrecvError('invalid_purpose', 'Unknown PIX purpose.')
    return text


def normalize_currency(value: Any, *, default: str = 'BRL') -> str:
    text = str(value or default).strip().upper()
    if text not in {'BRL', 'R$', 'REAL', 'REAIS'}:
        raise PrecvError('invalid_currency', 'PIX is BRL only.')
    return 'BRL'


def normalize_ispb(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        raise PrecvError('invalid_ispb', 'ISPB is required.')
    if len(digits) < 6 or len(digits) > 8:
        raise PrecvError('invalid_ispb', 'ISPB must be eight digits.')
    if len(digits) < 8:
        digits = digits.zfill(8)
    return digits


def mask_ispb(value: Any) -> str:
    pid = str(value or '').strip()
    if len(pid) < 8:
        return pid
    return pid[:2] + '****' + pid[-2:]


def participant_name(value: Any, *, directory: Optional[Dict[str, str]] = None) -> str:
    pid = normalize_ispb(value)
    names = directory if directory is not None else DEFAULT_PARTICIPANTS
    return names.get(pid, '')


def cpf_check_digit_ok(digits: str) -> bool:
    if len(digits) != 11 or not digits.isdigit() or digits == digits[0] * 11:
        return False

    def digit(base: str, factor: int) -> int:
        total = sum(int(ch) * (factor - index) for index, ch in enumerate(base))
        rem = (total * 10) % 11
        return 0 if rem == 10 else rem

    first = digit(digits[:9], 10)
    second = digit(digits[:9] + str(first), 11)
    return digits[9] == str(first) and digits[10] == str(second)


def cnpj_check_digit_ok(digits: str) -> bool:
    if len(digits) != 14 or not digits.isdigit() or digits == digits[0] * 14:
        return False
    weights_one = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
    weights_two = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)

    def digit(base: str, weights: Sequence[int]) -> int:
        total = sum(int(ch) * weight for ch, weight in zip(base, weights))
        rem = total % 11
        return 0 if rem < 2 else 11 - rem

    first = digit(digits[:12], weights_one)
    second = digit(digits[:12] + str(first), weights_two)
    return digits[12] == str(first) and digits[13] == str(second)


def normalize_key_type(value: Any) -> str:
    text = str(value or '').strip().lower().replace('-', '_').replace(' ', '_')
    text = KEY_TYPE_ALIASES.get(text, text)
    if text not in KEY_TYPES:
        raise PrecvError('invalid_key', 'Dict key type must be cpf, cnpj, email, phone, or evp.')
    return text


def normalize_cpf(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not cpf_check_digit_ok(digits):
        raise PrecvError('invalid_key', 'CPF failed check digits.')
    return digits


def normalize_cnpj(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not cnpj_check_digit_ok(digits):
        raise PrecvError('invalid_key', 'CNPJ failed check digits.')
    return digits


def normalize_email(value: Any) -> str:
    text = str(value or '').strip().lower()
    if not re.fullmatch(r'[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,24}', text):
        raise PrecvError('invalid_key', 'Email Dict key is invalid.')
    if len(text) > 77:
        raise PrecvError('invalid_key', 'Email Dict key is too long.')
    return text


def normalize_phone(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if digits.startswith('00'):
        digits = digits[2:]
    if digits.startswith('55') and len(digits) >= 12:
        digits = digits
    elif len(digits) in {10, 11}:
        digits = '55' + digits
    if not (12 <= len(digits) <= 13) or not digits.startswith('55'):
        raise PrecvError('invalid_key', 'Phone Dict key must be a Brazilian E.164 number.')
    national = digits[2:]
    if len(national) not in {10, 11}:
        raise PrecvError('invalid_key', 'Phone Dict key must be a Brazilian E.164 number.')
    return '+' + digits


def normalize_evp(value: Any) -> str:
    text = str(value or '').strip().lower()
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError, TypeError) as exc:
        raise PrecvError('invalid_key', 'EVP Dict key must be a UUID.') from exc
    return str(parsed)


def normalize_dict_key(key_type: Any, value: Any) -> Tuple[str, str]:
    kind = normalize_key_type(key_type)
    if kind == 'cpf':
        return kind, normalize_cpf(value)
    if kind == 'cnpj':
        return kind, normalize_cnpj(value)
    if kind == 'email':
        return kind, normalize_email(value)
    if kind == 'phone':
        return kind, normalize_phone(value)
    return kind, normalize_evp(value)


def mask_dict_key(key_type: Any, value: Any) -> str:
    kind = str(key_type or '').strip().lower()
    text = str(value or '')
    if kind == 'cpf':
        digits = ''.join(ch for ch in text if ch.isdigit())
        return '***.***.***-%s' % (digits[-2:] if len(digits) >= 2 else digits)
    if kind == 'cnpj':
        digits = ''.join(ch for ch in text if ch.isdigit())
        return '**.***.***/****-%s' % (digits[-2:] if len(digits) >= 2 else digits)
    if kind == 'email':
        if '@' not in text:
            return '***'
        local, _, domain = text.partition('@')
        head = local[:1] if local else '*'
        return '%s***@%s' % (head, domain)
    if kind == 'phone':
        digits = ''.join(ch for ch in text if ch.isdigit())
        return '+55*******%s' % (digits[-4:] if len(digits) >= 4 else digits)
    if len(text) >= 8:
        return text[:8] + '…'
    return '***'


def key_fingerprint(key_type: Any, value: Any) -> str:
    kind, normalized = normalize_dict_key(key_type, value)
    raw = '%s\0%s' % (kind, normalized)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def compose_end_to_end_id(ispb: str, cycle_date: str, sequence: int) -> str:
    """Bacen EndToEndId: E + ISPB(8) + YYYYMMDD(8) + 15 alphanumeric."""
    pid = normalize_ispb(ispb)
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise PrecvError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999999999:
        raise PrecvError('invalid_reference', 'EndToEndId sequence out of range.')
    tail = ('PIX%012d' % seq)[-15:]
    return 'E%s%s%s' % (pid, day, tail)


def normalize_end_to_end_id(value: Any) -> str:
    text = str(value or '').strip().upper()
    if len(text) != 32 or not text.startswith('E'):
        raise PrecvError('invalid_reference', 'EndToEndId must be 32 characters starting with E.')
    if not text[1:17].isdigit():
        raise PrecvError('invalid_reference', 'EndToEndId ISPB/date is invalid.')
    if not re.fullmatch(r'[A-Z0-9]{15}', text[17:]):
        raise PrecvError('invalid_reference', 'EndToEndId tail must be 15 alphanumeric.')
    return text


def mask_end_to_end_id(value: Any) -> str:
    text = str(value or '').strip()
    if len(text) < 4:
        return text
    return 'E************%s' % text[-4:]


def normalize_txid(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    cleaned = re.sub(r'[^A-Za-z0-9]', '', text)
    if not (1 <= len(cleaned) <= 35):
        raise PrecvError('invalid_reference', 'TxId must be 1-35 alphanumeric.')
    return cleaned


def compose_amount_field(amount: Decimal) -> str:
    quantized = amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    if quantized < 0 or quantized > Decimal('999999999.99'):
        raise PrecvError('invalid_amount', 'Amount cannot be encoded in PIX1.')
    return money_str(quantized)


def parse_amount_field(value: Any) -> Decimal:
    try:
        return parse_money(value)
    except AmountError as exc:
        raise PrecvError('invalid_amount', 'PIX1 amount field is invalid.') from exc


def _reject_markup(text: str) -> None:
    lowered = text.lower()
    if '<xml' in lowered or '<!doctype' in lowered or '<?xml' in lowered:
        raise PrecvError('invalid_file', 'XML/DOCTYPE PIX1 files are rejected.')


def compose_pix1(
    *,
    end_to_end_id: str,
    txid: str,
    send_ispb: str,
    recv_ispb: str,
    key_type: str,
    key_value: str,
    amount: Decimal,
    purpose: str,
    status: str,
    mask: bool = True,
) -> str:
    e2e = normalize_end_to_end_id(end_to_end_id)
    return '|'.join((
        'PIX1',
        PIX1_VERSION,
        mask_end_to_end_id(e2e) if mask else e2e,
        normalize_txid(txid),
        mask_ispb(send_ispb) if mask else normalize_ispb(send_ispb),
        mask_ispb(recv_ispb) if mask else normalize_ispb(recv_ispb),
        normalize_key_type(key_type),
        mask_dict_key(key_type, key_value) if mask else normalize_dict_key(key_type, key_value)[1],
        compose_amount_field(amount),
        normalize_purpose(purpose),
        str(status or IN_POSTED),
        SETTLEMENT_SPI,
    ))


def parse_pix1(text: Any) -> Dict[str, Any]:
    raw = str(text or '').strip()
    if not raw:
        raise PrecvError('invalid_file', 'PIX1 record is empty.')
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != PIX1_FIELDS:
        raise PrecvError('invalid_file', 'PIX1 record must have %d fields.' % PIX1_FIELDS)
    if parts[0] != 'PIX1' or parts[1] != PIX1_VERSION:
        raise PrecvError('invalid_file', 'PIX1 version marker is invalid.')
    if parts[11] != SETTLEMENT_SPI:
        raise PrecvError('invalid_file', 'PIX1 settlement method must be spi.')
    if '*' in parts[2] or '…' in parts[7] or '***' in parts[7]:
        raise PrecvError('invalid_file', 'Masked PIX1 record cannot be re-ingested.')
    kind, key_value = normalize_dict_key(parts[6], parts[7])
    return {
        'end_to_end_id': normalize_end_to_end_id(parts[2]),
        'txid': normalize_txid(parts[3]),
        'send_ispb': normalize_ispb(parts[4]),
        'recv_ispb': normalize_ispb(parts[5]),
        'key_type': kind,
        'key_value': key_value,
        'amount': parse_amount_field(parts[8]),
        'purpose': normalize_purpose(parts[9]),
        'status': parts[10],
        'settlement_method': SETTLEMENT_SPI,
        'name': '',
    }


def split_pix1_file(text: Any) -> List[str]:
    raw = str(text or '')
    _reject_markup(raw)
    records = [line.strip() for line in raw.replace('\r\n', '\n').split('\n') if line.strip()]
    if not records:
        raise PrecvError('invalid_file', 'PIX1 file is empty.')
    return records


def message_from_pix1(record: str) -> Dict[str, Any]:
    return parse_pix1(record)


def message_from_values(values: Dict[str, Any]) -> Dict[str, Any]:
    recv = normalize_ispb(values.get('recv_ispb') or values.get('receiver') or DEFAULT_RECEIVER)
    send = normalize_ispb(values.get('send_ispb') or values.get('sender_ispb') or values.get('ispb'))
    kind, key_value = normalize_dict_key(
        values.get('key_type') or values.get('kind') or 'email',
        values.get('key') or values.get('key_value') or values.get('dict_key'),
    )
    amount = values.get('amount')
    if not isinstance(amount, Decimal):
        try:
            amount = parse_money(amount)
        except AmountError as exc:
            raise PrecvError('invalid_amount', 'Amount is invalid.') from exc
    e2e = str(values.get('end_to_end_id') or values.get('e2e') or '').strip()
    if e2e:
        e2e = normalize_end_to_end_id(e2e)
    else:
        cycle = str(values.get('value_date') or values.get('cycle_date') or '').strip()
        if '-' in cycle:
            cycle = cycle.replace('-', '')[:8]
        seq = values.get('sequence') or values.get('seq') or 1
        e2e = compose_end_to_end_id(send, cycle, int(seq))
    name = str(values.get('name') or values.get('sender_name') or values.get('payer') or '').strip()
    if name:
        name = normalize_legal_name(name)
    return {
        'end_to_end_id': e2e,
        'txid': normalize_txid(values.get('txid') or values.get('tx_id') or ''),
        'send_ispb': send,
        'recv_ispb': recv,
        'key_type': kind,
        'key_value': key_value,
        'amount': amount,
        'purpose': normalize_purpose(values.get('purpose')),
        'name': name or 'PIX PAYER',
        'currency': normalize_currency(values.get('currency')),
    }


def easter_gregorian(year: int) -> date:
    """Anonymous Gregorian Easter (used for Carnival / Good Friday)."""
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


def brazil_holidays(year: int) -> set:
    """National bank holidays in Brazil (weekday calendar, not PIX cutoff)."""
    easter = easter_gregorian(year)
    return {
        date(year, 1, 1),
        easter - timedelta(days=48),
        easter - timedelta(days=47),
        easter - timedelta(days=2),
        date(year, 4, 21),
        date(year, 5, 1),
        date(year, 9, 7),
        date(year, 10, 12),
        date(year, 11, 2),
        date(year, 11, 15),
        date(year, 11, 20),
        date(year, 12, 25),
    }


class SpiClock:
    """PIX/SPI 24/7 clock. Holidays are reported; night window is BRT 20:00-06:00."""

    def __init__(
        self,
        *,
        tz_offset_hours: int = -3,
        night_start_hour: int = 20,
        night_end_hour: int = 6,
        extra_holidays: Sequence[str] = (),
    ) -> None:
        self.tz_offset_hours = int(tz_offset_hours)
        self.night_start_hour = int(night_start_hour)
        self.night_end_hour = int(night_end_hour)
        self.tz = timezone(timedelta(hours=self.tz_offset_hours))
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
        return day in brazil_holidays(day.year) or day in self.extra_holidays

    def is_night(self, ts: float) -> bool:
        local = self.local_dt(ts)
        hour = local.hour
        start = self.night_start_hour
        end = self.night_end_hour
        if start == end:
            return False
        if start > end:
            return hour >= start or hour < end
        return start <= hour < end

    def cycle_date(self, ts: float) -> str:
        return self.local_dt(ts).strftime('%Y%m%d')

    def snapshot(self, ts: float) -> Dict[str, Any]:
        local = self.local_dt(ts)
        day = local.date()
        return {
            'local_date': day.isoformat(),
            'local_time': local.strftime('%H:%M'),
            'timezone': 'BRT',
            'night': self.is_night(ts),
            'weekend': self.is_weekend(day),
            'holiday': self.is_holiday(day),
            'instant': True,
            'night_window': '%02d:00-%02d:00' % (self.night_start_hour, self.night_end_hour),
            'cycle_date': self.cycle_date(ts),
            'settlement_method': SETTLEMENT_SPI,
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
class PrecvPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_claim: bool = True
    customer_return: bool = True
    allow_credit: bool = False
    max_keys: int = 12
    max_inbounds: int = 120
    min_amount: Decimal = Decimal('0.01')
    max_amount: Decimal = Decimal('100000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    tz_offset_hours: int = -3
    night_start_hour: int = 20
    night_end_hour: int = 6
    receiver_ispb: str = DEFAULT_RECEIVER
    customer_return_seconds: int = CUSTOMER_RETURN_SECONDS
    staff_med_days: int = STAFF_MED_DAYS
    claim_seconds: int = CLAIM_SECONDS
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()
    participants: Dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.participants is None:
            self.participants = dict(DEFAULT_PARTICIPANTS)

    @classmethod
    def from_env(cls) -> 'PrecvPolicy':
        extra = _env_list('PRECV_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        extra_pids = _env_list('PRECV_PARTICIPANTS')
        directory = dict(DEFAULT_PARTICIPANTS)
        for item in extra_pids:
            if ':' in item:
                pid, name = item.split(':', 1)
                try:
                    directory[normalize_ispb(pid)] = name.strip().upper()[:80]
                except PrecvError:
                    continue
        source = os.environ.get('PRECV_RECEIVER') or DEFAULT_RECEIVER
        try:
            receiver = normalize_ispb(source)
        except PrecvError:
            receiver = DEFAULT_RECEIVER
        return cls(
            enabled=_env_bool('PRECV_ENABLED', True),
            customer_manage=_env_bool('PRECV_CUSTOMER_MANAGE', True),
            customer_claim=_env_bool('PRECV_CUSTOMER_CLAIM', True),
            customer_return=_env_bool('PRECV_CUSTOMER_RETURN', True),
            allow_credit=_env_bool('PRECV_ALLOW_CREDIT', False),
            max_keys=max(1, _env_int('PRECV_MAX_KEYS', 12)),
            max_inbounds=max(1, _env_int('PRECV_MAX_INBOUNDS', 120)),
            min_amount=_env_money('PRECV_MIN_AMOUNT', '0.01'),
            max_amount=_env_money('PRECV_MAX_AMOUNT', '100000.00'),
            dual_control_threshold=_env_money('PRECV_DUAL_CONTROL', '10000.00'),
            tz_offset_hours=_env_int('PRECV_TZ_OFFSET', -3),
            night_start_hour=max(0, min(23, _env_int('PRECV_NIGHT_START', 20))),
            night_end_hour=max(0, min(23, _env_int('PRECV_NIGHT_END', 6))),
            receiver_ispb=receiver,
            customer_return_seconds=max(60, _env_int('PRECV_RETURN_SECONDS', CUSTOMER_RETURN_SECONDS)),
            staff_med_days=max(1, _env_int('PRECV_MED_DAYS', STAFF_MED_DAYS)),
            claim_seconds=max(60, _env_int('PRECV_CLAIM_SECONDS', CLAIM_SECONDS)),
            watchlist=watch,
            extra_holidays=_env_list('PRECV_HOLIDAYS'),
            participants=directory,
        )


@dataclass
class PrecvKey:
    key_id: str
    userid: str
    nickname: str
    key_type: str
    key_value: str
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
            'key_type': self.key_type,
            'key_masked': mask_dict_key(self.key_type, self.key_value),
            'account_last4': last4(self.destination_account),
            'status': self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == KEY_ACTIVE,
            'paused': self.status == KEY_PAUSED,
            'cancelled': self.status == KEY_CANCELLED,
        }


@dataclass
class PrecvInbound:
    inbound_id: str
    end_to_end_id: str
    userid: str
    destination_account: str
    amount_brl: str
    sender_name: str
    send_ispb: str
    recv_ispb: str
    key_type: str
    key_value: str
    fingerprint: str
    txid: str
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
            'end_to_end_id_masked': mask_end_to_end_id(self.end_to_end_id),
            'userid': self.userid,
            'account_last4': last4(self.destination_account) if self.destination_account else '',
            'amount_brl': self.amount_brl,
            'sender_name': self.sender_name,
            'send_ispb_masked': mask_ispb(self.send_ispb) if self.send_ispb else '',
            'send_name': participant_name(self.send_ispb) if self.send_ispb else '',
            'recv_ispb_masked': mask_ispb(self.recv_ispb) if self.recv_ispb else '',
            'key_type': self.key_type,
            'key_masked': mask_dict_key(self.key_type, self.key_value) if self.key_value else '***',
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


def _key_from_row(row: Any) -> PrecvKey:
    data = dict(row)
    return PrecvKey(
        key_id=data['key_id'], userid=data['userid'], nickname=data['nickname'],
        key_type=data['key_type'], key_value=data['key_value'], fingerprint=data['fingerprint'],
        destination_account=data['destination_account'], status=data['status'], actor=data['actor'],
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
    )


def _inbound_from_row(row: Any) -> PrecvInbound:
    data = dict(row)
    return PrecvInbound(
        inbound_id=data['inbound_id'], end_to_end_id=data['end_to_end_id'], userid=data['userid'],
        destination_account=data['destination_account'], amount_brl=data['amount_brl'],
        sender_name=data['sender_name'], send_ispb=data['send_ispb'], recv_ispb=data['recv_ispb'],
        key_type=data['key_type'], key_value=data['key_value'], fingerprint=data['fingerprint'],
        txid=data.get('txid') or '', purpose=data['purpose'], status=data['status'],
        reason=data['reason'], actor=data['actor'], releaser=data['releaser'],
        ofac_hit=bool(int(data['ofac_hit'])), ofac_match=data['ofac_match'],
        ofac_overridden=bool(int(data['ofac_overridden'])),
        created_at=float(data['created_at']), updated_at=float(data['updated_at']),
        posted_at=float(data.get('posted_at') or 0), returned_at=float(data.get('returned_at') or 0),
        expiry_at=float(data.get('expiry_at') or 0), note=data.get('note') or '',
    )


class MemoryPrecvStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.keys: Dict[str, PrecvKey] = {}
        self.inbounds: Dict[str, PrecvInbound] = {}
        self._by_e2e: Dict[str, str] = {}

    def put_key(self, row: PrecvKey) -> None:
        with self._lock:
            self.keys[row.key_id] = row

    def get_key(self, key_id: str) -> Optional[PrecvKey]:
        return self.keys.get(key_id)

    def get_key_by_fingerprint(self, fingerprint: str) -> Optional[PrecvKey]:
        matches = [
            row for row in self.keys.values()
            if row.fingerprint == fingerprint and row.status in OPEN_KEY
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda row: row.created_at, reverse=True)[0]

    def update_key(self, row: PrecvKey) -> None:
        self.put_key(row)

    def list_keys(self, userid: Optional[str] = None) -> List[PrecvKey]:
        rows = list(self.keys.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        return sorted(rows, key=lambda row: row.created_at)

    def put_inbound(self, row: PrecvInbound) -> None:
        with self._lock:
            self.inbounds[row.inbound_id] = row
            self._by_e2e[row.end_to_end_id] = row.inbound_id

    def get_inbound(self, inbound_id: str) -> Optional[PrecvInbound]:
        return self.inbounds.get(inbound_id)

    def get_by_end_to_end_id(self, end_to_end_id: str) -> Optional[PrecvInbound]:
        inbound_id = self._by_e2e.get(end_to_end_id)
        return self.inbounds.get(inbound_id) if inbound_id else None

    def update_inbound(self, row: PrecvInbound) -> None:
        self.put_inbound(row)

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[PrecvInbound]:
        rows = list(self.inbounds.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)


class SqlitePrecvStore:
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
                    key_type TEXT NOT NULL,
                    key_value TEXT NOT NULL,
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
                    end_to_end_id TEXT NOT NULL UNIQUE,
                    userid TEXT NOT NULL DEFAULT '',
                    destination_account TEXT NOT NULL DEFAULT '',
                    amount_brl TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    send_ispb TEXT NOT NULL,
                    recv_ispb TEXT NOT NULL,
                    key_type TEXT NOT NULL,
                    key_value TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    txid TEXT NOT NULL DEFAULT '',
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

    def put_key(self, row: PrecvKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO keys (
                    key_id, userid, nickname, key_type, key_value, fingerprint,
                    destination_account, status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.key_id, row.userid, row.nickname, row.key_type, row.key_value,
                    row.fingerprint, row.destination_account, row.status, row.actor,
                    row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_key(self, key_id: str) -> Optional[PrecvKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM keys WHERE key_id = ?', (key_id,)).fetchone()
        return _key_from_row(row) if row else None

    def get_key_by_fingerprint(self, fingerprint: str) -> Optional[PrecvKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM keys WHERE fingerprint = ? AND status IN ('active', 'paused')
                ORDER BY created_at DESC LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
        return _key_from_row(row) if row else None

    def update_key(self, row: PrecvKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE keys SET nickname=?, key_type=?, key_value=?, fingerprint=?,
                    destination_account=?, status=?, actor=?, updated_at=?
                WHERE key_id=?
                """,
                (
                    row.nickname, row.key_type, row.key_value, row.fingerprint,
                    row.destination_account, row.status, row.actor, row.updated_at, row.key_id,
                ),
            )
            conn.commit()

    def list_keys(self, userid: Optional[str] = None) -> List[PrecvKey]:
        sql = 'SELECT * FROM keys'
        args: Tuple[Any, ...] = ()
        if userid is not None:
            sql += ' WHERE userid = ?'
            args = (userid,)
        sql += ' ORDER BY created_at'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_key_from_row(row) for row in rows]

    def put_inbound(self, row: PrecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inbounds (
                    inbound_id, end_to_end_id, userid, destination_account, amount_brl,
                    sender_name, send_ispb, recv_ispb, key_type, key_value, fingerprint,
                    txid, purpose, status, reason, actor, releaser, ofac_hit, ofac_match,
                    ofac_overridden, created_at, updated_at, posted_at, returned_at,
                    expiry_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.inbound_id, row.end_to_end_id, row.userid, row.destination_account,
                    row.amount_brl, row.sender_name, row.send_ispb, row.recv_ispb, row.key_type,
                    row.key_value, row.fingerprint, row.txid, row.purpose, row.status, row.reason,
                    row.actor, row.releaser, int(row.ofac_hit), row.ofac_match,
                    int(row.ofac_overridden), row.created_at, row.updated_at, row.posted_at,
                    row.returned_at, row.expiry_at, row.note,
                ),
            )
            conn.commit()

    def get_inbound(self, inbound_id: str) -> Optional[PrecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,)).fetchone()
        return _inbound_from_row(row) if row else None

    def get_by_end_to_end_id(self, end_to_end_id: str) -> Optional[PrecvInbound]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM inbounds WHERE end_to_end_id = ?', (end_to_end_id,),
            ).fetchone()
        return _inbound_from_row(row) if row else None

    def update_inbound(self, row: PrecvInbound) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE inbounds SET userid=?, destination_account=?, amount_brl=?, sender_name=?,
                    send_ispb=?, recv_ispb=?, key_type=?, key_value=?, fingerprint=?, txid=?,
                    purpose=?, status=?, reason=?, actor=?, releaser=?, ofac_hit=?, ofac_match=?,
                    ofac_overridden=?, updated_at=?, posted_at=?, returned_at=?, expiry_at=?, note=?
                WHERE inbound_id=?
                """,
                (
                    row.userid, row.destination_account, row.amount_brl, row.sender_name,
                    row.send_ispb, row.recv_ispb, row.key_type, row.key_value, row.fingerprint,
                    row.txid, row.purpose, row.status, row.reason, row.actor, row.releaser,
                    int(row.ofac_hit), row.ofac_match, int(row.ofac_overridden), row.updated_at,
                    row.posted_at, row.returned_at, row.expiry_at, row.note, row.inbound_id,
                ),
            )
            conn.commit()

    def list_inbounds(self, userid: Optional[str] = None, status: Optional[str] = None) -> List[PrecvInbound]:
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


class PrecvService:
    def __init__(
        self,
        policy: PrecvPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[SpiClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: datetime.now(tz=timezone.utc).timestamp())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn or screen_name
        self.calendar = calendar or SpiClock(
            tz_offset_hours=policy.tz_offset_hours,
            night_start_hour=policy.night_start_hour,
            night_end_hour=policy.night_end_hour,
            extra_holidays=policy.extra_holidays,
        )

    def _now(self) -> float:
        return float(self.clock())

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise PrecvError('precv_disabled', 'PIX receive is disabled.')

    def _require_staff(self, actor_type: str) -> None:
        if actor_type not in EMPLOYEE_ROLES:
            raise PrecvError('precv_forbidden', 'Staff only.')

    def _require_customer_manage(self, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if not self.policy.customer_manage:
            raise PrecvError('precv_forbidden', 'Customers cannot manage PIX Dict keys.')

    def _accounts(self, userid: str) -> Any:
        if not self.accounts_fn:
            return {}
        return self.accounts_fn(userid) or {}

    def _owned(self, userid: str, account: str) -> None:
        owned = own_accounts_from_customer_payload(self._accounts(userid))
        if account not in owned:
            raise PrecvError('invalid_account', 'Destination is not an account of this customer.')

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
        key_type: Any,
        key_value: Any,
        destination_account: Any,
    ) -> PrecvKey:
        self._require_enabled()
        self._require_customer_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise PrecvError('precv_forbidden', 'Cannot enroll a Dict key for another customer.')
        kind, value = normalize_dict_key(key_type, key_value)
        account = normalize_account(destination_account)
        self._owned(owner_userid, account)
        if not self._credit_allowed(owner_userid, account):
            raise PrecvError('credit_not_allowed', 'Credit accounts cannot receive PIX.')
        fingerprint = key_fingerprint(kind, value)
        existing = self.store.get_key_by_fingerprint(fingerprint)
        if existing and existing.status in OPEN_KEY:
            raise PrecvError('key_duplicate', 'That Dict key is already enrolled.')
        open_rows = [row for row in self.store.list_keys(owner_userid) if row.status in OPEN_KEY]
        if any(row.nickname.lower() == normalize_nickname(nickname).lower() for row in open_rows):
            raise PrecvError('key_duplicate', 'That nickname is already in use.')
        if len(open_rows) >= self.policy.max_keys:
            raise PrecvError('key_limit', 'PIX Dict key limit reached.')
        now = self._now()
        row = PrecvKey(
            key_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=normalize_nickname(nickname),
            key_type=kind,
            key_value=value,
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
    ) -> PrecvKey:
        self._require_enabled()
        row = self.store.get_key(key_id)
        if row is None:
            raise PrecvError('key_not_found', 'PIX Dict key enrollment not found.')
        if actor_type not in EMPLOYEE_ROLES and actor != row.userid:
            raise PrecvError('precv_forbidden', 'Cannot change another customer Dict key.')
        self._require_customer_manage(actor_type)
        if status not in KEY_STATUSES:
            raise PrecvError('invalid_status', 'Unknown Dict key status.')
        if row.status == KEY_CANCELLED:
            raise PrecvError('already_cancelled', 'Cancelled Dict keys cannot change.')
        if status == KEY_PAUSED and row.status != KEY_ACTIVE:
            raise PrecvError('already_paused' if row.status == KEY_PAUSED else 'invalid_status', 'Key is not active.')
        if status == KEY_ACTIVE and row.status != KEY_PAUSED:
            raise PrecvError('already_active', 'Key is already active.')
        if status == row.status:
            code = 'already_paused' if status == KEY_PAUSED else 'already_active'
            raise PrecvError(code, 'Dict key already in that status.')
        updated = replace(row, status=status, actor=actor, updated_at=self._now())
        self.store.update_key(updated)
        if status == KEY_ACTIVE:
            self._rematch_unmatched(updated)
        return updated

    def _rematch_unmatched(self, enrollment: PrecvKey) -> None:
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
    ) -> Tuple[PrecvInbound, bool]:
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
    ) -> Tuple[List[PrecvInbound], int]:
        self._require_enabled()
        self._require_staff(actor_type)
        created = 0
        rows = []
        fallback = ''
        if sender_name:
            fallback = normalize_legal_name(sender_name)
        for record in split_pix1_file(payload):
            parsed = parse_pix1(record)
            if fallback:
                parsed['name'] = fallback
            elif not parsed.get('name'):
                parsed['name'] = 'PIX PAYER'
            row, is_new = self._ingest_parsed(actor=actor, parsed=parsed)
            rows.append(row)
            if is_new:
                created += 1
        return rows, created

    def _ingest_parsed(self, *, actor: str, parsed: Dict[str, Any]) -> Tuple[PrecvInbound, bool]:
        if parsed['recv_ispb'] != self.policy.receiver_ispb:
            raise PrecvError('wrong_receiver', 'PIX credit is not for this ISPB.')
        existing = self.store.get_by_end_to_end_id(parsed['end_to_end_id'])
        if existing is not None:
            return existing, False
        total = len(self.store.list_inbounds())
        if total >= self.policy.max_inbounds:
            raise PrecvError('inbound_limit', 'Inbound PIX limit reached.')
        amount = parsed['amount'] if isinstance(parsed['amount'], Decimal) else parse_money(parsed['amount'])
        if amount < self.policy.min_amount or amount > self.policy.max_amount:
            code = 'pix_amount_exceeded' if amount > self.policy.max_amount else 'amount_out_of_range'
            raise PrecvError(code, 'Amount is outside the PIX range.')
        now = self._now()
        fingerprint = key_fingerprint(parsed['key_type'], parsed['key_value'])
        row = PrecvInbound(
            inbound_id=uuid.uuid4().hex,
            end_to_end_id=parsed['end_to_end_id'],
            userid='',
            destination_account='',
            amount_brl=money_str(amount),
            sender_name=parsed.get('name') or 'PIX PAYER',
            send_ispb=parsed['send_ispb'],
            recv_ispb=parsed['recv_ispb'],
            key_type=parsed['key_type'],
            key_value=parsed['key_value'],
            fingerprint=fingerprint,
            txid=parsed.get('txid') or '',
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
        enrollment = self.store.get_key_by_fingerprint(fingerprint)
        if enrollment is not None and enrollment.status == KEY_ACTIVE:
            row.userid = enrollment.userid
            row.destination_account = enrollment.destination_account
        self.store.put_inbound(row)
        self._evaluate(row, rescreen=True)
        return row, True

    def _evaluate(self, row: PrecvInbound, *, rescreen: bool) -> PrecvInbound:
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
        except PrecvError:
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
        brl = parse_money(row.amount_brl)
        if brl >= self.policy.dual_control_threshold and not row.releaser:
            row.status = IN_PENDING
            row.reason = 'dual_control'
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        return self._post(row)

    def _post(self, row: PrecvInbound) -> PrecvInbound:
        if self.credit_fn is None:
            raise PrecvError('failed', 'Credit function is not configured.')
        remark = 'pix from %s' % row.sender_name[:40]
        result = self.credit_fn(row.destination_account, row.amount_brl, remark)
        classified = _classify_money_result(result)
        now = self._now()
        if classified == 'nsf':
            row.status = IN_FAILED
            row.reason = 'nsf'
            row.updated_at = now
            self.store.update_inbound(row)
            raise PrecvError('nsf', 'Credit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            row.status = IN_FAILED
            row.reason = 'failed'
            row.updated_at = now
            self.store.update_inbound(row)
            raise PrecvError('failed', 'Credit failed.', inbound=row)
        row.status = IN_POSTED
        row.reason = ''
        row.posted_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def preview(self, *, inbound_id: str = '', end_to_end_id: str = '', actor: str = '', actor_type: str = '') -> Dict[str, Any]:
        row = self._get(inbound_id=inbound_id, end_to_end_id=end_to_end_id)
        self._assert_visible(row, actor, actor_type)
        return {
            'inbound': row.to_dict(),
            'clock': self.calendar.snapshot(self._now()),
            'receiver': mask_ispb(self.policy.receiver_ispb),
        }

    def _get(self, *, inbound_id: str = '', end_to_end_id: str = '') -> PrecvInbound:
        row = None
        if inbound_id:
            row = self.store.get_inbound(str(inbound_id).strip())
        if row is None and end_to_end_id:
            row = self.store.get_by_end_to_end_id(normalize_end_to_end_id(end_to_end_id))
        if row is None:
            raise PrecvError('inbound_not_found', 'Inbound PIX not found.')
        return row

    def _assert_visible(self, row: PrecvInbound, actor: str, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if row.userid and row.userid != actor:
            raise PrecvError('precv_forbidden', 'Cannot view another customer PIX.')

    def assign(
        self,
        *,
        inbound_id: str,
        actor: str,
        actor_type: str,
        customer_id: str,
        account: Any,
        nickname: str = '',
    ) -> PrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_UNMATCHED:
            raise PrecvError('not_assignable', 'Only unmatched PIX credits can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise PrecvError('missing_customer_id', 'Customer id is required to assign.')
        dest = normalize_account(account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise PrecvError('credit_not_allowed', 'Credit accounts cannot receive PIX.')
        existing = self.store.get_key_by_fingerprint(row.fingerprint)
        if existing is None or existing.status == KEY_CANCELLED:
            self.add_key(
                owner_userid=owner, actor=actor, actor_type=actor_type,
                nickname=nickname or ('PIX %s' % row.key_type),
                key_type=row.key_type, key_value=row.key_value, destination_account=dest,
            )
            fresh = self.store.get_inbound(row.inbound_id)
            if fresh is not None and fresh.status != IN_UNMATCHED:
                return fresh
        elif existing.userid != owner:
            raise PrecvError('key_duplicate', 'Dict key belongs to another customer.')
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
        key_type: Any = '',
        key_value: Any = '',
        destination_account: Any = '',
        nickname: str = '',
        owner_userid: str = '',
    ) -> PrecvInbound:
        """DICT claim: customer presents the matching Dict key for an unmatched PIX."""
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_claim:
                raise PrecvError('precv_forbidden', 'Customers cannot claim PIX credits.')
            owner = actor
        else:
            owner = str(owner_userid or '').strip()
            if not owner:
                raise PrecvError('missing_customer_id', 'Customer id is required to claim.')
        if not key_type or not key_value:
            raise PrecvError('missing_key', 'Dict key is required to claim.')
        kind, value = normalize_dict_key(key_type, key_value)
        fingerprint = key_fingerprint(kind, value)
        dest = normalize_account(destination_account)
        self._owned(owner, dest)
        if not self._credit_allowed(owner, dest):
            raise PrecvError('credit_not_allowed', 'Credit accounts cannot receive PIX.')
        row = None
        if inbound_id:
            row = self._get(inbound_id=inbound_id)
            if row.fingerprint != fingerprint:
                raise PrecvError('not_claimable', 'Presented Dict key does not match this PIX.')
        else:
            for candidate in self.store.list_inbounds(status=IN_UNMATCHED):
                if candidate.fingerprint == fingerprint:
                    row = candidate
                    break
        if row is None:
            raise PrecvError('inbound_not_found', 'No unmatched PIX for that Dict key.')
        if row.status != IN_UNMATCHED:
            raise PrecvError('not_claimable', 'This PIX credit is not waiting for a DICT claim.')
        if row.expiry_at and self._now() > row.expiry_at:
            row.status = IN_EXPIRED
            row.reason = 'expired'
            row.updated_at = self._now()
            self.store.update_inbound(row)
            raise PrecvError('return_window_closed', 'DICT claim window has closed.')
        existing = self.store.get_key_by_fingerprint(fingerprint)
        if existing is None or existing.status == KEY_CANCELLED:
            self.add_key(
                owner_userid=owner, actor=actor, actor_type=actor_type,
                nickname=nickname or ('PIX %s' % kind),
                key_type=kind, key_value=value, destination_account=dest,
            )
        elif existing.userid != owner:
            raise PrecvError('key_duplicate', 'Dict key belongs to another customer.')
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

    def override_ofac(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> PrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_HELD:
            raise PrecvError('not_overridable', 'Only OFAC-held PIX credits can be overridden.')
        row.ofac_overridden = True
        row.ofac_hit = False
        row.note = normalize_note(note)
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def release(self, *, inbound_id: str, actor: str, actor_type: str) -> PrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status != IN_PENDING:
            raise PrecvError('not_releasable', 'Only dual-control PIX credits can be released.')
        if actor == row.actor:
            raise PrecvError('same_approver', 'A different employee must release this credit.')
        row.releaser = actor
        row.updated_at = self._now()
        self.store.update_inbound(row)
        return self._evaluate(row, rescreen=False)

    def reject(self, *, inbound_id: str, actor: str, actor_type: str, note: str = '') -> PrecvInbound:
        self._require_enabled()
        self._require_staff(actor_type)
        row = self._get(inbound_id=inbound_id)
        if row.status not in OPEN_IN:
            raise PrecvError('not_rejectable', 'This PIX credit cannot be rejected.')
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
        if not row.end_to_end_id:
            raise PrecvError('not_exportable', 'PIX credit has no EndToEndId to export.')
        _ = actor
        record = compose_pix1(
            end_to_end_id=row.end_to_end_id,
            txid=row.txid,
            send_ispb=row.send_ispb,
            recv_ispb=row.recv_ispb,
            key_type=row.key_type,
            key_value=row.key_value,
            amount=parse_money(row.amount_brl, allow_zero=True),
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
    ) -> PrecvInbound:
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
    ) -> PrecvInbound:
        self._require_enabled()
        row = self._get(inbound_id=inbound_id)
        self._assert_visible(row, actor, actor_type)
        if actor_type not in EMPLOYEE_ROLES:
            if not self.policy.customer_return:
                raise PrecvError('precv_forbidden', 'Customers cannot return PIX credits.')
            if actor != row.userid:
                raise PrecvError('precv_forbidden', 'Cannot return another customer PIX.')
        return self._return(row, actor=actor, actor_type=actor_type, reason=reason, honor_window=True)

    def _return(
        self,
        row: PrecvInbound,
        *,
        actor: str,
        actor_type: str,
        reason: str,
        honor_window: bool,
    ) -> PrecvInbound:
        now = self._now()
        _ = actor
        if row.status == IN_RETURNED or row.returned_at:
            raise PrecvError('already_returned', 'PIX credit already returned.')
        if row.status in OPEN_IN:
            row.status = IN_RETURNED
            row.reason = reason or 'cust'
            row.returned_at = now
            row.updated_at = now
            self.store.update_inbound(row)
            return row
        if row.status != IN_POSTED:
            raise PrecvError('not_returnable', 'This PIX credit cannot be returned.')
        elapsed = now - (row.posted_at or row.created_at)
        if honor_window and actor_type not in EMPLOYEE_ROLES:
            if elapsed > self.policy.customer_return_seconds:
                raise PrecvError('return_window_closed', 'Customer MED window is 80 minutes.')
        if actor_type in EMPLOYEE_ROLES:
            if elapsed > self.policy.staff_med_days * 86400:
                raise PrecvError('med_window_closed', 'Staff MED window is 90 days.')
        if self.debit_fn is None:
            raise PrecvError('return_failed', 'Debit function is not configured.')
        remark = 'pix return %s' % row.end_to_end_id[-12:]
        result = self.debit_fn(row.destination_account, row.amount_brl, remark)
        classified = _classify_money_result(result)
        if classified == 'nsf':
            raise PrecvError('nsf', 'Return debit failed for insufficient funds.', inbound=row)
        if classified != 'ok':
            raise PrecvError('return_failed', 'Return debit failed.', inbound=row)
        row.status = IN_RETURNED
        row.reason = reason or 'cust'
        row.returned_at = now
        row.updated_at = now
        self.store.update_inbound(row)
        return row

    def run_due(self, userid: Optional[str] = None) -> List[PrecvInbound]:
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
        ytd = sum((parse_money(row.amount_brl, allow_zero=True) for row in received), Decimal('0.00'))
        return {
            'enabled': self.policy.enabled,
            'clock': self.calendar.snapshot(now),
            'receiver': mask_ispb(self.policy.receiver_ispb),
            'keys': [row.to_dict() for row in keys[:40]],
            'inbounds': [row.to_dict() for row in inbounds[:40]],
            'ytd_received': money_str(ytd),
            'open_count': sum(1 for row in inbounds if row.status in OPEN_IN),
            'active_keys': sum(1 for row in keys if row.status == KEY_ACTIVE),
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'settlement_method': SETTLEMENT_SPI,
            'currency': 'BRL',
        }

    def unmatched_snapshot(self) -> Dict[str, Any]:
        rows = self.store.list_inbounds(status=IN_UNMATCHED)
        return {
            'enabled': self.policy.enabled,
            'unmatched': [row.to_dict() for row in rows[:40]],
            'count': len(rows),
        }


_SERVICE: Optional[PrecvService] = None


def set_service(service: Optional[PrecvService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[PrecvService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('PRECV_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryPrecvStore()
    path = os.environ.get('PRECV_DB', DEFAULT_STORE_PATH)
    return SqlitePrecvStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[SpiClock] = None,
) -> PrecvService:
    if store is None:
        store = default_store()
    return PrecvService(
        PrecvPolicy.from_env(),
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
        'precv_forbidden': 403,
        'precv_disabled': 403,
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
        'invalid_ispb': 400,
        'invalid_key': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_reference': 400,
        'invalid_file': 400,
        'invalid_currency': 400,
        'wrong_receiver': 400,
        'pix_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_inbound': 400,
        'missing_key': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: PrecvError) -> Dict[str, Any]:
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
    except PrecvError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def _snapshot_for(service: PrecvService, userid: str, actor: str, actor_type: str) -> Dict[str, Any]:
    return service.snapshot(userid, actor=actor, actor_type=actor_type)


def handle_list(service: PrecvService):
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
    return jsonify({'PixIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def handle_unmatched(service: PrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    actor_type = session.get('usertype') or 'customer'
    if actor_type not in EMPLOYEE_ROLES:
        return jsonify({'message': 'Staff only', 'error': 'precv_forbidden'}), 403
    snap = service.unmatched_snapshot()
    return jsonify({'UnmatchedPixIns': snap, 'PixIn': snap}), 200


def handle_add_key(service: PrecvService):
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
                raise PrecvError('missing_customer_id', 'Customer id is required.')
        row = service.add_key(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname') or '',
            key_type=values.get('key_type') or values.get('kind'),
            key_value=values.get('key') or values.get('key_value') or values.get('dict_key'),
            destination_account=values.get('destination_account') or values.get('account') or values.get('default_account'),
        )
        return jsonify({
            'message': 'PIX Dict key enrolled',
            'key': row.to_dict(),
            'PixIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 201

    return _handle_errors(_run)


def _key_status_route(service: PrecvService, status: str, message: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        key_id = str(values.get('key_id') or '').strip()
        if not key_id:
            raise PrecvError('missing_key', 'key_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.set_key_status(key_id=key_id, actor=userid, actor_type=actor_type, status=status)
        return jsonify({
            'message': message,
            'key': row.to_dict(),
            'PixIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: PrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        preview = service.preview(
            inbound_id=str(values.get('inbound_id') or ''),
            end_to_end_id=str(values.get('end_to_end_id') or values.get('e2e') or ''),
            actor=userid,
            actor_type=actor_type,
        )
        owner = preview['inbound'].get('userid') or userid
        return jsonify({'preview': preview, 'PixIn': _snapshot_for(service, owner, userid, actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest(service: PrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        actor_type = session.get('usertype') or 'customer'
        row, created = service.ingest(actor=userid, actor_type=actor_type, values=values)
        owner = row.userid or userid
        return jsonify({
            'message': 'PIX ingested' if created else 'PIX already ingested',
            'inbound': row.to_dict(),
            'PixIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_ingest_file(service: PrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        payload = values.get('file') or values.get('body') or values.get('payload')
        if not payload:
            raise PrecvError('missing_file', 'PIX1 file body is required.')
        actor_type = session.get('usertype') or 'customer'
        rows, created = service.ingest_file(
            actor=userid, actor_type=actor_type, payload=str(payload),
            sender_name=str(values.get('name') or values.get('sender_name') or ''),
        )
        owner = rows[-1].userid if rows and rows[-1].userid else userid
        return jsonify({
            'message': 'PIX1 file ingested',
            'created': created,
            'inbounds': [row.to_dict() for row in rows],
            'PixIn': _snapshot_for(service, owner, userid, actor_type),
        }), (201 if created else 200)

    return _handle_errors(_run)


def handle_assign(service: PrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise PrecvError('missing_inbound', 'inbound_id is required.')
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
            'message': 'PIX assigned',
            'inbound': row.to_dict(),
            'PixIn': _snapshot_for(service, row.userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_claim(service: PrecvService):
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
            key_type=values.get('key_type') or values.get('kind'),
            key_value=values.get('key') or values.get('key_value') or values.get('dict_key'),
            destination_account=values.get('destination_account') or values.get('account'),
            nickname=str(values.get('nickname') or ''),
            owner_userid=str(values.get('customer_id') or ''),
        )
        return jsonify({
            'message': 'PIX claimed',
            'inbound': row.to_dict(),
            'PixIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_inbound_route(service: PrecvService, action: str):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise PrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        if action == 'override':
            row = service.override_ofac(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'release':
            row = service.release(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            message = 'PIX released'
        elif action == 'reject':
            row = service.reject(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'PIX rejected'
        elif action == 'return':
            row = service.return_inbound(
                inbound_id=inbound_id, actor=userid, actor_type=actor_type,
                reason=str(values.get('reason') or 'cust'),
            )
            message = 'PIX returned'
        elif action == 'export':
            exported = service.export(inbound_id=inbound_id, actor=userid, actor_type=actor_type)
            owner = exported['inbound'].get('userid') or userid
            return jsonify({
                'message': 'PIX exported',
                'file': exported['file'],
                'inbound': exported['inbound'],
                'PixIn': _snapshot_for(service, owner, userid, actor_type),
            }), 200
        else:
            raise PrecvError('invalid_status', 'Unknown PIX action.')
        return jsonify({
            'message': message,
            'inbound': row.to_dict(),
            'PixIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_request_return(service: PrecvService):
    userid, error = _require_session_user()
    if error:
        return error

    def _run():
        values = request.get_json(silent=True) or {}
        inbound_id = str(values.get('inbound_id') or '').strip()
        if not inbound_id:
            raise PrecvError('missing_inbound', 'inbound_id is required.')
        actor_type = session.get('usertype') or 'customer'
        row = service.request_return(
            inbound_id=inbound_id, actor=userid, actor_type=actor_type,
            reason=str(values.get('reason') or 'cust'),
        )
        return jsonify({
            'message': 'PIX return requested',
            'inbound': row.to_dict(),
            'PixIn': _snapshot_for(service, row.userid or userid, userid, actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: PrecvService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'PixIn': _snapshot_for(service, owner, userid, actor_type)}), 200


def attach_precv_routes(app, service: PrecvService) -> None:
    @app.route('/listPixIns', methods=['POST', 'GET'])
    def list_pix_ins_route():
        return handle_list(service)

    @app.route('/listPixRecvKeys', methods=['POST', 'GET'])
    def list_pix_recv_keys_route():
        return handle_list(service)

    @app.route('/listUnmatchedPixIns', methods=['POST', 'GET'])
    def list_unmatched_pix_ins_route():
        return handle_unmatched(service)

    @app.route('/addPixRecvKey', methods=['POST', 'GET'])
    def add_pix_recv_key_route():
        return handle_add_key(service)

    @app.route('/pausePixRecvKey', methods=['POST', 'GET'])
    def pause_pix_recv_key_route():
        return _key_status_route(service, KEY_PAUSED, 'PIX Dict key paused')

    @app.route('/resumePixRecvKey', methods=['POST', 'GET'])
    def resume_pix_recv_key_route():
        return _key_status_route(service, KEY_ACTIVE, 'PIX Dict key resumed')

    @app.route('/cancelPixRecvKey', methods=['POST', 'GET'])
    def cancel_pix_recv_key_route():
        return _key_status_route(service, KEY_CANCELLED, 'PIX Dict key cancelled')

    @app.route('/previewPixIn', methods=['POST', 'GET'])
    def preview_pix_in_route():
        return handle_preview(service)

    @app.route('/ingestPixIn', methods=['POST', 'GET'])
    def ingest_pix_in_route():
        return handle_ingest(service)

    @app.route('/ingestPixInFile', methods=['POST', 'GET'])
    def ingest_pix_in_file_route():
        return handle_ingest_file(service)

    @app.route('/claimPixIn', methods=['POST', 'GET'])
    def claim_pix_in_route():
        return handle_claim(service)

    @app.route('/assignPixIn', methods=['POST', 'GET'])
    def assign_pix_in_route():
        return handle_assign(service)

    @app.route('/overridePixInOfac', methods=['POST', 'GET'])
    def override_pix_in_ofac_route():
        return _staff_inbound_route(service, 'override')

    @app.route('/releasePixIn', methods=['POST', 'GET'])
    def release_pix_in_route():
        return _staff_inbound_route(service, 'release')

    @app.route('/rejectPixIn', methods=['POST', 'GET'])
    def reject_pix_in_route():
        return _staff_inbound_route(service, 'reject')

    @app.route('/returnPixIn', methods=['POST', 'GET'])
    def return_pix_in_route():
        return _staff_inbound_route(service, 'return')

    @app.route('/exportPixIn', methods=['POST', 'GET'])
    def export_pix_in_route():
        return _staff_inbound_route(service, 'export')

    @app.route('/requestPixInReturn', methods=['POST', 'GET'])
    def request_pix_in_return_route():
        return handle_request_return(service)

    @app.route('/runDuePixIns', methods=['POST', 'GET'])
    def run_due_pix_ins_route():
        return handle_run_due(service)
