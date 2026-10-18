"""PIX origination over SPI / Dict keys.

Customers send 24/7 BRL PIX payments to Dict keys (CPF, CNPJ, email,
phone, EVP) at an 8-digit ISPB. Independent of domestic Fedwire (PR #73),
CHIPS (PR #131/#133), Zelle, SWIFT gpi/MT103, ACH linking (PR #68), and
ISO 20022 investigation. Existing `/fundTransfer`, `/withdrawAmount`, and
`/sendWire` stay unchanged. `Customers.debit_request` / `credit_request`
still write `debited` / `direct deposited` unless a remark is supplied here.

Foundations (reusable beyond this screen):
- ISPB (8-digit Brazilian payment participant) + directory
- Dict key compose / normalize / mask / fingerprint (CPF/CNPJ checksums)
- PIX EndToEndId `E{ISPB}{YYYYMMDD}{15}`
- SPI 24/7 clock with Brazil holidays reported and a night/day cap
- PIX1 pipe compose/parse (XML/DOCTYPE rejected)
- MED return windows (80-minute customer / 90-day staff)
- Dual-control release for high-value PIX

Reuses Fedwire OFAC screening, money parsing, fee helper, and account
directory from `utility.wire`. Stores are pluggable (memory for tests,
sqlite WAL for restart-safe default). Full Dict keys, fingerprints, and
EndToEndIds never appear unmasked in to_dict / snapshots.
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

KEY_ACTIVE = 'active'
KEY_PAUSED = 'paused'
KEY_ARCHIVED = 'archived'
KEY_STATUSES = frozenset({KEY_ACTIVE, KEY_PAUSED, KEY_ARCHIVED})
OPEN_KEYS = frozenset({KEY_ACTIVE, KEY_PAUSED})
PIX_HELD = 'held'
PIX_PENDING = 'pending_release'
PIX_COMPLETED = 'completed'
PIX_REJECTED = 'rejected'
PIX_CANCELLED = 'cancelled'
PIX_RECALLED = 'recalled'
PIX_NSF = 'nsf'
PIX_FAILED = 'failed'
PIX_STATUSES = frozenset({
    PIX_HELD, PIX_PENDING, PIX_COMPLETED, PIX_REJECTED, PIX_CANCELLED,
    PIX_RECALLED, PIX_NSF, PIX_FAILED,
})
OPEN_PIX = frozenset({PIX_HELD, PIX_PENDING})
CANCELABLE = frozenset({PIX_HELD, PIX_PENDING})
FEE_NONE = 'none'
FEE_COLLECTED = 'collected'
FEE_WAIVED = 'waived'
FEE_NSF = 'nsf'
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
DEFAULT_STORE_PATH = 'SystemLogs/pix.sqlite'
DEFAULT_SOURCE_ISPB = '00360305'
CUSTOMER_RETURN_SECONDS = 80 * 60
STAFF_MED_DAYS = 90


class PixError(ValueError):
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
        raise PixError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise PixError('invalid_name', 'Beneficiary legal name must be 2-80 characters.')
    return text


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise PixError('invalid_purpose', 'Unknown PIX purpose.')
    return text


def normalize_currency(value: Any, *, default: str = 'BRL') -> str:
    text = str(value or default).strip().upper()
    if text not in {'BRL', 'R$', 'REAL', 'REAIS'}:
        raise PixError('invalid_currency', 'PIX is BRL only.')
    return 'BRL'


def normalize_ispb(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        raise PixError('invalid_ispb', 'ISPB is required.')
    if len(digits) < 6 or len(digits) > 8:
        raise PixError('invalid_ispb', 'ISPB must be eight digits.')
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
        raise PixError('invalid_key', 'Dict key type must be cpf, cnpj, email, phone, or evp.')
    return text


def normalize_cpf(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not cpf_check_digit_ok(digits):
        raise PixError('invalid_key', 'CPF failed check digits.')
    return digits


def normalize_cnpj(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not cnpj_check_digit_ok(digits):
        raise PixError('invalid_key', 'CNPJ failed check digits.')
    return digits


def normalize_email(value: Any) -> str:
    text = str(value or '').strip().lower()
    if not re.fullmatch(r'[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,24}', text):
        raise PixError('invalid_key', 'Email Dict key is invalid.')
    if len(text) > 77:
        raise PixError('invalid_key', 'Email Dict key is too long.')
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
        raise PixError('invalid_key', 'Phone Dict key must be a Brazilian E.164 number.')
    national = digits[2:]
    if len(national) not in {10, 11}:
        raise PixError('invalid_key', 'Phone Dict key must be a Brazilian E.164 number.')
    return '+' + digits


def normalize_evp(value: Any) -> str:
    text = str(value or '').strip().lower()
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError, TypeError) as exc:
        raise PixError('invalid_key', 'EVP Dict key must be a UUID.') from exc
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
        raise PixError('invalid_reference', 'Cycle date must be YYYYMMDD.')
    seq = int(sequence)
    if seq < 1 or seq > 999999999999:
        raise PixError('invalid_reference', 'EndToEndId sequence out of range.')
    tail = ('PIX%012d' % seq)[-15:]
    return 'E%s%s%s' % (pid, day, tail)


def normalize_end_to_end_id(value: Any) -> str:
    text = str(value or '').strip().upper()
    if len(text) != 32 or not text.startswith('E'):
        raise PixError('invalid_reference', 'EndToEndId must be 32 characters starting with E.')
    if not text[1:17].isdigit():
        raise PixError('invalid_reference', 'EndToEndId ISPB/date is invalid.')
    if not re.fullmatch(r'[A-Z0-9]{15}', text[17:]):
        raise PixError('invalid_reference', 'EndToEndId tail must be 15 alphanumeric.')
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
        raise PixError('invalid_reference', 'TxId must be 1-35 alphanumeric.')
    return cleaned


def compose_amount_field(amount: Decimal) -> str:
    quantized = amount.quantize(MONEY_QUANTUM)
    if quantized < 0 or quantized > Decimal('999999999.99'):
        raise PixError('invalid_amount', 'Amount cannot be encoded in PIX1.')
    return money_str(quantized)


def parse_amount_field(value: Any) -> Decimal:
    try:
        return parse_money(value)
    except AmountError as exc:
        raise PixError('invalid_amount', 'PIX1 amount field is invalid.') from exc


def _reject_markup(text: str) -> None:
    lowered = text.lower()
    if '<xml' in lowered or '<!doctype' in lowered or '<?xml' in lowered:
        raise PixError('invalid_file', 'XML/DOCTYPE PIX1 files are rejected.')


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
        str(status or PIX_COMPLETED),
        SETTLEMENT_SPI,
    ))


def parse_pix1(text: Any) -> Dict[str, Any]:
    raw = str(text or '').strip()
    if not raw:
        raise PixError('invalid_file', 'PIX1 record is empty.')
    _reject_markup(raw)
    parts = raw.split('|')
    if len(parts) != PIX1_FIELDS:
        raise PixError('invalid_file', 'PIX1 record must have %d fields.' % PIX1_FIELDS)
    if parts[0] != 'PIX1' or parts[1] != PIX1_VERSION:
        raise PixError('invalid_file', 'PIX1 version marker is invalid.')
    if parts[11] != SETTLEMENT_SPI:
        raise PixError('invalid_file', 'PIX1 settlement method must be spi.')
    if '*' in parts[2] or '…' in parts[7] or '***' in parts[7]:
        raise PixError('invalid_file', 'Masked PIX1 record cannot be re-ingested.')
    return {
        'end_to_end_id': normalize_end_to_end_id(parts[2]),
        'txid': normalize_txid(parts[3]),
        'send_ispb': normalize_ispb(parts[4]),
        'recv_ispb': normalize_ispb(parts[5]),
        'key_type': normalize_key_type(parts[6]),
        'key_value': parts[7],
        'amount': money_str(parse_amount_field(parts[8])),
        'purpose': normalize_purpose(parts[9]),
        'status': parts[10],
        'settlement_method': SETTLEMENT_SPI,
    }


def split_pix1_file(text: Any) -> List[str]:
    raw = str(text or '')
    _reject_markup(raw)
    records = [line.strip() for line in raw.replace('\r\n', '\n').split('\n') if line.strip()]
    if not records:
        raise PixError('invalid_file', 'PIX1 file is empty.')
    return records


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
    days = {
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
    return days


class SpiClock:
    """PIX/SPI 24/7 clock. Holidays are reported; night cap uses BRT 20:00-06:00."""

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
            extra.add(date.fromisoformat(text[:10]))
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
class PixPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    allow_credit: bool = False
    max_keys: int = 12
    max_payments: int = 120
    min_amount: Decimal = Decimal('0.01')
    day_max: Decimal = Decimal('20000.00')
    night_max: Decimal = Decimal('1000.00')
    outbound_fee: Decimal = Decimal('0.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    tz_offset_hours: int = -3
    night_start_hour: int = 20
    night_end_hour: int = 6
    source_ispb: str = DEFAULT_SOURCE_ISPB
    customer_return_seconds: int = CUSTOMER_RETURN_SECONDS
    staff_med_days: int = STAFF_MED_DAYS
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()
    participants: Dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.participants is None:
            self.participants = dict(DEFAULT_PARTICIPANTS)

    @classmethod
    def from_env(cls) -> 'PixPolicy':
        extra = _env_list('PIX_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        extra_pids = _env_list('PIX_PARTICIPANTS')
        directory = dict(DEFAULT_PARTICIPANTS)
        for item in extra_pids:
            if ':' in item:
                pid, _, label = item.partition(':')
                try:
                    directory[normalize_ispb(pid)] = label.strip() or directory.get(normalize_ispb(pid), '')
                except PixError:
                    continue
        source = os.environ.get('PIX_SOURCE', DEFAULT_SOURCE_ISPB)
        try:
            source_ispb = normalize_ispb(source)
        except PixError:
            source_ispb = DEFAULT_SOURCE_ISPB
        return cls(
            enabled=_env_bool('PIX_ENABLED', True),
            customer_manage=_env_bool('PIX_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('PIX_CUSTOMER_SEND', True),
            allow_credit=_env_bool('PIX_ALLOW_CREDIT', False),
            max_keys=max(1, _env_int('PIX_MAX_KEYS', 12)),
            max_payments=max(1, _env_int('PIX_MAX_PAYMENTS', 120)),
            min_amount=_env_money('PIX_MIN_AMOUNT', '0.01'),
            day_max=_env_money('PIX_DAY_MAX', '20000.00'),
            night_max=_env_money('PIX_NIGHT_MAX', '1000.00'),
            outbound_fee=_env_money('PIX_FEE', '0.00'),
            dual_control_threshold=_env_money('PIX_DUAL_CONTROL', '10000.00'),
            tz_offset_hours=_env_int('PIX_TZ_OFFSET', -3),
            night_start_hour=max(0, min(23, _env_int('PIX_NIGHT_START', 20))),
            night_end_hour=max(0, min(23, _env_int('PIX_NIGHT_END', 6))),
            source_ispb=source_ispb,
            customer_return_seconds=max(60, _env_int('PIX_RETURN_SECONDS', CUSTOMER_RETURN_SECONDS)),
            staff_med_days=max(1, _env_int('PIX_MED_DAYS', STAFF_MED_DAYS)),
            watchlist=watch,
            extra_holidays=_env_list('PIX_HOLIDAYS'),
            participants=directory,
        )


@dataclass
class PixKey:
    key_id: str
    userid: str
    nickname: str
    legal_name: str
    key_type: str
    key_value: str
    key_fingerprint: str
    ispb: str
    default_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float
    participant_label: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'key_id': self.key_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'key_type': self.key_type,
            'key_masked': mask_dict_key(self.key_type, self.key_value),
            'ispb': self.ispb,
            'ispb_masked': mask_ispb(self.ispb),
            'participant_label': self.participant_label,
            'default_account': self.default_account,
            'account_last4': last4(self.default_account),
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == KEY_ACTIVE,
            'paused': self.status == KEY_PAUSED,
            'archived': self.status == KEY_ARCHIVED,
        }


@dataclass
class PixPayment:
    payment_id: str
    trace_id: str
    key_id: str
    userid: str
    internal_account: str
    amount: str
    fee: str
    fee_status: str
    nickname: str
    legal_name: str
    key_type: str
    key_masked: str
    ispb: str
    purpose: str
    memo: str
    status: str
    end_to_end_id: str
    txid: str
    value_date: str
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    created_at: float
    updated_at: float
    completed_at: float = 0.0
    recalled_at: float = 0.0
    note: str = ''
    reason: str = ''
    settlement_method: str = SETTLEMENT_SPI

    def to_dict(self) -> Dict[str, Any]:
        return {
            'payment_id': self.payment_id,
            'trace_id': self.trace_id,
            'key_id': self.key_id,
            'userid': self.userid,
            'internal_account_last4': last4(self.internal_account),
            'amount': self.amount,
            'fee': self.fee,
            'fee_status': self.fee_status,
            'currency': 'BRL',
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'key_type': self.key_type,
            'key_masked': self.key_masked,
            'ispb_masked': mask_ispb(self.ispb),
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'end_to_end_id_masked': mask_end_to_end_id(self.end_to_end_id) if self.end_to_end_id else '',
            'txid': self.txid,
            'value_date': self.value_date,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'completed_at': self.completed_at,
            'recalled_at': self.recalled_at,
            'note': self.note,
            'reason': self.reason,
            'settlement_method': self.settlement_method,
            'cancelable': self.status in CANCELABLE,
            'recallable': self.status == PIX_COMPLETED,
        }


def _clone_key(row: PixKey) -> PixKey:
    return PixKey(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_pay(row: PixPayment) -> PixPayment:
    return PixPayment(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _key_from_row(row: Any) -> PixKey:
    return PixKey(
        key_id=row['key_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        key_type=row['key_type'],
        key_value=row['key_value'],
        key_fingerprint=row['key_fingerprint'],
        ispb=row['ispb'],
        default_account=row['default_account'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        participant_label=row['participant_label'] if 'participant_label' in row.keys() else '',
    )


def _pay_from_row(row: Any) -> PixPayment:
    return PixPayment(
        payment_id=row['payment_id'],
        trace_id=row['trace_id'],
        key_id=row['key_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        amount=row['amount'],
        fee=row['fee'],
        fee_status=row['fee_status'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        key_type=row['key_type'],
        key_masked=row['key_masked'],
        ispb=row['ispb'],
        purpose=row['purpose'],
        memo=row['memo'],
        status=row['status'],
        end_to_end_id=row['end_to_end_id'],
        txid=row['txid'],
        value_date=row['value_date'],
        actor=row['actor'],
        releaser=row['releaser'],
        ofac_hit=int(row['ofac_hit']),
        ofac_match=row['ofac_match'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        completed_at=float(row['completed_at'] or 0),
        recalled_at=float(row['recalled_at'] or 0),
        note=row['note'] or '',
        reason=row['reason'] or '',
        settlement_method=row['settlement_method'] or SETTLEMENT_SPI,
    )


class MemoryPixStore:
    def __init__(self) -> None:
        self._keys: Dict[str, PixKey] = {}
        self._pays: Dict[str, PixPayment] = {}
        self._lock = threading.Lock()

    def put_key(self, row: PixKey) -> None:
        self._keys[row.key_id] = _clone_key(row)

    def get_key(self, key_id: str) -> Optional[PixKey]:
        row = self._keys.get(key_id)
        return _clone_key(row) if row else None

    def update_key(self, row: PixKey) -> None:
        self._keys[row.key_id] = _clone_key(row)

    def list_keys(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[PixKey]:
        rows = list(self._keys.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != KEY_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return [_clone_key(row) for row in rows]

    def find_key_by_nickname(self, userid: str, nickname: str) -> Optional[PixKey]:
        wanted = nickname.strip().lower()
        for row in self._keys.values():
            if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_KEYS:
                return _clone_key(row)
        return None

    def find_key_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[PixKey]:
        for row in self._keys.values():
            if row.userid == userid and row.key_fingerprint == fingerprint and row.status in OPEN_KEYS:
                return _clone_key(row)
        return None

    def put_payment(self, row: PixPayment) -> PixPayment:
        with self._lock:
            self._pays[row.payment_id] = _clone_pay(row)
            return _clone_pay(row)

    def update_payment(self, row: PixPayment) -> None:
        self._pays[row.payment_id] = _clone_pay(row)

    def get_payment(self, payment_id: str) -> Optional[PixPayment]:
        row = self._pays.get(payment_id)
        return _clone_pay(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[PixPayment]:
        for row in self._pays.values():
            if row.trace_id == trace_id:
                return _clone_pay(row)
        return None

    def list_payments(self, userid: Optional[str] = None, key_id: Optional[str] = None) -> List[PixPayment]:
        rows = list(self._pays.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if key_id is not None:
            rows = [row for row in rows if row.key_id == key_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return [_clone_pay(row) for row in rows]

    def next_sequence(self, cycle_date: str) -> int:
        with self._lock:
            used = [
                row.end_to_end_id for row in self._pays.values()
                if row.end_to_end_id and len(row.end_to_end_id) >= 17 and row.end_to_end_id[9:17] == cycle_date
            ]
        return len(used) + 1


class SqlitePixStore:
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
                CREATE TABLE IF NOT EXISTS pix_keys (
                    key_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    key_type TEXT NOT NULL,
                    key_value TEXT NOT NULL,
                    key_fingerprint TEXT NOT NULL,
                    ispb TEXT NOT NULL,
                    default_account TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    participant_label TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    payment_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    key_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    internal_account TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    fee_status TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    key_type TEXT NOT NULL,
                    key_masked TEXT NOT NULL,
                    ispb TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    end_to_end_id TEXT NOT NULL DEFAULT '',
                    txid TEXT NOT NULL DEFAULT '',
                    value_date TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL NOT NULL DEFAULT 0,
                    recalled_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    settlement_method TEXT NOT NULL DEFAULT 'spi'
                )
                """
            )
            conn.commit()

    def put_key(self, row: PixKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pix_keys (
                    key_id, userid, nickname, legal_name, key_type, key_value,
                    key_fingerprint, ispb, default_account, status, actor,
                    created_at, updated_at, participant_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.key_id, row.userid, row.nickname, row.legal_name,
                    row.key_type, row.key_value, row.key_fingerprint, row.ispb,
                    row.default_account, row.status, row.actor, row.created_at,
                    row.updated_at, row.participant_label,
                ),
            )
            conn.commit()

    def get_key(self, key_id: str) -> Optional[PixKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM pix_keys WHERE key_id = ?', (key_id,)).fetchone()
        return _key_from_row(row) if row else None

    def update_key(self, row: PixKey) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE pix_keys SET nickname=?, legal_name=?, key_type=?, key_value=?,
                    key_fingerprint=?, ispb=?, default_account=?, status=?, actor=?,
                    updated_at=?, participant_label=?
                WHERE key_id=?
                """,
                (
                    row.nickname, row.legal_name, row.key_type, row.key_value,
                    row.key_fingerprint, row.ispb, row.default_account, row.status,
                    row.actor, row.updated_at, row.participant_label, row.key_id,
                ),
            )
            conn.commit()

    def list_keys(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[PixKey]:
        sql = 'SELECT * FROM pix_keys'
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
        return [_key_from_row(row) for row in rows]

    def find_key_by_nickname(self, userid: str, nickname: str) -> Optional[PixKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM pix_keys
                WHERE userid = ? AND lower(nickname) = lower(?) AND status IN ('active', 'paused')
                """,
                (userid, nickname),
            ).fetchone()
        return _key_from_row(row) if row else None

    def find_key_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[PixKey]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM pix_keys
                WHERE userid = ? AND key_fingerprint = ? AND status IN ('active', 'paused')
                """,
                (userid, fingerprint),
            ).fetchone()
        return _key_from_row(row) if row else None

    def put_payment(self, row: PixPayment) -> PixPayment:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO payments (
                    payment_id, trace_id, key_id, userid, internal_account, amount,
                    fee, fee_status, nickname, legal_name, key_type, key_masked,
                    ispb, purpose, memo, status, end_to_end_id, txid, value_date,
                    actor, releaser, ofac_hit, ofac_match, created_at, updated_at,
                    completed_at, recalled_at, note, reason, settlement_method
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.payment_id, row.trace_id, row.key_id, row.userid,
                    row.internal_account, row.amount, row.fee, row.fee_status,
                    row.nickname, row.legal_name, row.key_type, row.key_masked,
                    row.ispb, row.purpose, row.memo, row.status, row.end_to_end_id,
                    row.txid, row.value_date, row.actor, row.releaser, row.ofac_hit,
                    row.ofac_match, row.created_at, row.updated_at, row.completed_at,
                    row.recalled_at, row.note, row.reason, row.settlement_method,
                ),
            )
            conn.commit()
        return row

    def update_payment(self, row: PixPayment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE payments SET amount=?, fee=?, fee_status=?, status=?,
                    end_to_end_id=?, txid=?, value_date=?, actor=?, releaser=?,
                    ofac_hit=?, ofac_match=?, updated_at=?, completed_at=?,
                    recalled_at=?, note=?, reason=?
                WHERE payment_id=?
                """,
                (
                    row.amount, row.fee, row.fee_status, row.status,
                    row.end_to_end_id, row.txid, row.value_date, row.actor,
                    row.releaser, row.ofac_hit, row.ofac_match, row.updated_at,
                    row.completed_at, row.recalled_at, row.note, row.reason,
                    row.payment_id,
                ),
            )
            conn.commit()

    def get_payment(self, payment_id: str) -> Optional[PixPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE payment_id = ?', (payment_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[PixPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE trace_id = ?', (trace_id,)).fetchone()
        return _pay_from_row(row) if row else None

    def list_payments(self, userid: Optional[str] = None, key_id: Optional[str] = None) -> List[PixPayment]:
        sql = 'SELECT * FROM payments'
        args: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            args.append(userid)
        if key_id is not None:
            clauses.append('key_id = ?')
            args.append(key_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_pay_from_row(row) for row in rows]

    def next_sequence(self, cycle_date: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE substr(end_to_end_id, 10, 8) = ?",
                (cycle_date,),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class PixService:
    def __init__(
        self,
        policy: PixPolicy,
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
        self.clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
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

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise PixError('pix_disabled', 'PIX is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise PixError('pix_forbidden', 'Customers cannot manage PIX keys.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise PixError('pix_forbidden', 'Customers cannot originate PIX.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise PixError('pix_forbidden', 'Staff only.')

    def _accounts(self, userid: str) -> Tuple[List[str], Dict[str, str]]:
        payload = self.accounts_fn(userid) if self.accounts_fn is not None else {}
        return own_accounts_from_customer_payload(payload), account_types_from_customer_payload(payload)

    def _assert_internal_account(self, userid: str, account: str) -> str:
        owned, types = self._accounts(userid)
        if owned and account not in owned:
            raise PixError('invalid_account', 'Account is not owned by this customer.')
        kind = types.get(account, '')
        if kind == 'credit' and not self.policy.allow_credit:
            raise PixError('credit_not_allowed', 'Credit accounts cannot originate PIX.')
        return kind

    def _assert_amount(self, amount: Decimal, *, night: bool) -> None:
        if amount < self.policy.min_amount:
            raise PixError('amount_out_of_range', 'Amount is outside the allowed range.')
        if amount > self.policy.day_max:
            raise PixError('pix_amount_exceeded', 'Amount exceeds the PIX day cap.')
        if night and amount > self.policy.night_max:
            raise PixError('night_amount_exceeded', 'Amount exceeds the PIX night cap.')

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def _screen(self, name: str, *, aliases: Iterable[str] = ()) -> ScreenResult:
        return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)

    def add_key(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        key_type: Any,
        key_value: Any,
        ispb: Any,
        default_account: Any,
    ) -> PixKey:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise PixError('pix_forbidden', 'Not allowed to add PIX keys for this customer.')
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        kind, normalized = normalize_dict_key(key_type, key_value)
        fingerprint = key_fingerprint(kind, normalized)
        pid = normalize_ispb(ispb)
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        if self.store.find_key_by_nickname(owner_userid, name) is not None:
            raise PixError('key_duplicate', 'A PIX key with that nickname already exists.')
        if self.store.find_key_by_fingerprint(owner_userid, fingerprint) is not None:
            raise PixError('key_duplicate', 'That Dict key is already on file.')
        open_rows = [row for row in self.store.list_keys(owner_userid) if row.status in OPEN_KEYS]
        if len(open_rows) >= self.policy.max_keys:
            raise PixError('key_limit', 'PIX key limit reached.')
        now = float(self.clock())
        row = PixKey(
            key_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            key_type=kind,
            key_value=normalized,
            key_fingerprint=fingerprint,
            ispb=pid,
            default_account=account,
            status=KEY_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
            participant_label=self.policy.participants.get(pid, ''),
        )
        self.store.put_key(row)
        return row

    def get_key(self, *, key_id: str, actor: str, actor_type: str) -> PixKey:
        self._require_enabled()
        row = self.store.get_key(key_id)
        if row is None:
            raise PixError('key_not_found', 'PIX key not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise PixError('pix_forbidden', 'Not allowed to view this PIX key.')
        return row

    def enforce_key(
        self,
        *,
        key_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> PixKey:
        row = self.get_key(key_id=key_id, actor=actor, actor_type=actor_type)
        if row.status == KEY_ARCHIVED:
            raise PixError('already_archived', 'PIX key is archived.')
        if row.status == KEY_PAUSED:
            raise PixError('key_paused', 'PIX key is paused.')
        if require_active and row.status != KEY_ACTIVE:
            raise PixError('invalid_status', 'PIX key is not active.')
        return row

    def set_key_status(
        self,
        *,
        key_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> PixKey:
        self._require_manage(actor_type)
        row = self.get_key(key_id=key_id, actor=actor, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': KEY_PAUSED, 'hold': KEY_PAUSED,
            'resume': KEY_ACTIVE, 'activate': KEY_ACTIVE, 'unpause': KEY_ACTIVE,
            'archive': KEY_ARCHIVED, 'close': KEY_ARCHIVED, 'cancel': KEY_ARCHIVED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {KEY_PAUSED, KEY_ACTIVE, KEY_ARCHIVED}:
            raise PixError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == KEY_ARCHIVED:
            raise PixError('already_archived', 'PIX key is already archived.')
        if wanted == KEY_PAUSED:
            if row.status == KEY_PAUSED:
                raise PixError('already_paused', 'PIX key is already paused.')
            if row.status != KEY_ACTIVE:
                raise PixError('invalid_status', 'Only an active PIX key can be paused.')
        elif wanted == KEY_ACTIVE:
            if row.status == KEY_ACTIVE:
                raise PixError('already_active', 'PIX key is already active.')
            if row.status != KEY_PAUSED:
                raise PixError('invalid_status', 'Only a paused PIX key can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_key(row)
        return row

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        key_id: Any,
        amount: Any,
        internal_account: Any = None,
        waive_fee: bool = False,
    ) -> Dict[str, Any]:
        self._require_send(actor_type)
        key = self.enforce_key(key_id=str(key_id or '').strip(), actor=actor, actor_type=actor_type)
        if key.userid != owner_userid:
            raise PixError('pix_forbidden', 'PIX key does not belong to this customer.')
        reais = parse_money(amount)
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        self._assert_amount(reais, night=bool(clock['night']))
        account = normalize_account(internal_account or key.default_account)
        self._assert_internal_account(owner_userid, account)
        fee = compute_fee(reais, self.policy.outbound_fee, waived=bool(waive_fee) and actor_type in EMPLOYEE_ROLES)
        ofac = self._screen(key.legal_name, aliases=(key.nickname,))
        return {
            'amount': money_str(reais),
            'fee': money_str(fee),
            'total': money_str(reais + fee),
            'currency': 'BRL',
            'internal_account': account,
            'key': key.to_dict(),
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(reais),
            'clock': clock,
            'night_cap': money_str(self.policy.night_max),
            'day_cap': money_str(self.policy.day_max),
            'settlement_method': SETTLEMENT_SPI,
        }

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        key: PixKey,
        account: str,
        reais: Decimal,
        fee: Decimal,
        purpose: str,
        memo: str,
        trace_id: str,
        txid: str,
        ofac: ScreenResult,
        waive_fee: bool,
    ) -> PixPayment:
        now = float(self.clock())
        value = self.calendar.cycle_date(now)
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        if ofac.hit:
            status = PIX_HELD
        elif self._needs_dual_control(reais):
            status = PIX_PENDING
        else:
            status = PIX_COMPLETED
        payment = PixPayment(
            payment_id=uuid.uuid4().hex,
            trace_id=trace_id,
            key_id=key.key_id,
            userid=owner_userid,
            internal_account=account,
            amount=money_str(reais),
            fee=money_str(fee),
            fee_status=fee_status,
            nickname=key.nickname,
            legal_name=key.legal_name,
            key_type=key.key_type,
            key_masked=mask_dict_key(key.key_type, key.key_value),
            ispb=key.ispb,
            purpose=purpose,
            memo=memo,
            status=status,
            end_to_end_id='',
            txid=txid,
            value_date=value,
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
        )
        if status == PIX_COMPLETED:
            self._transmit(payment, actor=actor, key=key)
        return payment

    def _key_value(self, payment: PixPayment) -> str:
        key = self.store.get_key(payment.key_id)
        return key.key_value if key is not None else ''

    def _transmit(self, payment: PixPayment, *, actor: str, key: Optional[PixKey] = None) -> PixPayment:
        now = float(self.clock())
        cycle = payment.value_date or self.calendar.cycle_date(now)
        seq = self.store.next_sequence(cycle)
        payment.end_to_end_id = compose_end_to_end_id(self.policy.source_ispb, cycle, seq)
        reais = parse_money(payment.amount)
        fee = parse_money(payment.fee, allow_zero=True)
        remark = 'pix to %s' % payment.nickname
        status = PIX_COMPLETED
        fail_note = ''
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(payment.internal_account, money_str(reais), remark)
            except Exception as exc:
                status = PIX_FAILED
                fail_note = str(exc)[:240]
            else:
                classified = _classify_money_result(result)
                if classified == 'nsf':
                    status = PIX_NSF
                    payment.end_to_end_id = ''
                elif classified != 'ok':
                    status = PIX_FAILED
                    fail_note = str(result)[:240]
                    payment.end_to_end_id = ''
        if status == PIX_COMPLETED and fee > 0 and payment.fee_status != FEE_WAIVED and self.debit_fn is not None:
            fee_remark = 'pix fee %s' % mask_end_to_end_id(payment.end_to_end_id)
            try:
                fee_result = self.debit_fn(payment.internal_account, money_str(fee), fee_remark)
            except Exception:
                payment.fee_status = FEE_NSF
            else:
                if _classify_money_result(fee_result) == 'ok':
                    payment.fee_status = FEE_COLLECTED
                elif _classify_money_result(fee_result) == 'nsf':
                    payment.fee_status = FEE_NSF
                else:
                    payment.fee_status = FEE_NSF
        payment.status = status
        payment.updated_at = now
        payment.completed_at = now if status == PIX_COMPLETED else 0.0
        payment.releaser = str(actor)
        if fail_note:
            payment.note = fail_note
        _ = key
        return payment

    def originate(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        key_id: Any,
        amount: Any,
        internal_account: Any = None,
        purpose: Any = 'other',
        memo: Any = '',
        trace_id: Any = None,
        txid: Any = '',
        waive_fee: bool = False,
    ) -> Tuple[PixPayment, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise PixError('pix_forbidden', 'Not allowed to originate PIX for this customer.')
        key = self.enforce_key(key_id=str(key_id or '').strip(), actor=actor, actor_type=actor_type)
        if key.userid != owner_userid:
            raise PixError('pix_forbidden', 'PIX key does not belong to this customer.')
        trace = normalize_id(trace_id)
        existing = self.store.get_payment_by_trace(trace)
        if existing is not None:
            return existing, False
        reais = parse_money(amount)
        now = float(self.clock())
        clock = self.calendar.snapshot(now)
        self._assert_amount(reais, night=bool(clock['night']))
        account = normalize_account(internal_account or key.default_account)
        self._assert_internal_account(owner_userid, account)
        fee = compute_fee(
            reais, self.policy.outbound_fee,
            waived=bool(waive_fee) and actor_type in EMPLOYEE_ROLES,
        )
        if len(self.store.list_payments(owner_userid)) >= self.policy.max_payments:
            raise PixError('payment_limit', 'PIX history limit reached.')
        ofac = self._screen(key.legal_name, aliases=(key.nickname,))
        payment = self._place(
            owner_userid=owner_userid,
            actor=actor,
            key=key,
            account=account,
            reais=reais,
            fee=fee,
            purpose=normalize_purpose(purpose),
            memo=normalize_note(memo, limit=140),
            trace_id=trace,
            txid=normalize_txid(txid),
            ofac=ofac,
            waive_fee=bool(waive_fee) and actor_type in EMPLOYEE_ROLES,
        )
        stored = self.store.put_payment(payment)
        if stored.status == PIX_NSF:
            raise PixError('nsf', 'Insufficient funds for PIX.', payment=stored)
        if stored.status == PIX_FAILED:
            raise PixError('failed', 'PIX debit did not complete.', payment=stored)
        return stored, True

    def get_payment(self, *, payment_id: str, actor: str, actor_type: str) -> PixPayment:
        self._require_enabled()
        row = self.store.get_payment(payment_id)
        if row is None:
            raise PixError('payment_not_found', 'PIX payment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise PixError('pix_forbidden', 'Not allowed to view this PIX payment.')
        return row

    def cancel_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> PixPayment:
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and payment.userid != actor:
            raise PixError('pix_forbidden', 'Not allowed to cancel this PIX.')
        if payment.status not in CANCELABLE:
            raise PixError('not_cancelable', 'Only held or pending PIX can be cancelled.')
        payment.status = PIX_CANCELLED
        payment.note = normalize_note(note)
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        self.store.update_payment(payment)
        return payment

    def waive_fee(self, *, payment_id: str, actor: str, actor_type: str) -> PixPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status not in CANCELABLE:
            raise PixError('invalid_status', 'Fee can only be waived before the PIX is sent.')
        payment.fee = money_str(Decimal('0.00'))
        payment.fee_status = FEE_WAIVED
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        self.store.update_payment(payment)
        return payment

    def override_ofac(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> PixPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status != PIX_HELD:
            raise PixError('not_overridable', 'Only an OFAC hold can be overridden.')
        now = float(self.clock())
        payment.ofac_hit = 0
        payment.note = normalize_note(note) or 'ofac override'
        payment.actor = str(actor)
        payment.updated_at = now
        reais = parse_money(payment.amount)
        if self._needs_dual_control(reais):
            payment.status = PIX_PENDING
        else:
            self._transmit(payment, actor=actor)
        self.store.update_payment(payment)
        if payment.status == PIX_NSF:
            raise PixError('nsf', 'Insufficient funds for PIX.', payment=payment)
        if payment.status == PIX_FAILED:
            raise PixError('failed', 'PIX debit did not complete.', payment=payment)
        return payment

    def release_payment(self, *, payment_id: str, actor: str, actor_type: str) -> PixPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == PIX_HELD:
            raise PixError('ofac_hold', 'OFAC hold must be overridden before release.')
        if payment.status != PIX_PENDING:
            raise PixError('not_releasable', 'Only pending PIX can be released.')
        if (
            payment.actor
            and str(actor) == str(payment.actor)
            and parse_money(payment.amount) >= self.policy.dual_control_threshold
        ):
            raise PixError('same_approver', 'A different employee must release this PIX.')
        self._transmit(payment, actor=actor)
        self.store.update_payment(payment)
        if payment.status == PIX_NSF:
            raise PixError('nsf', 'Insufficient funds for PIX.', payment=payment)
        if payment.status == PIX_FAILED:
            raise PixError('failed', 'PIX debit did not complete.', payment=payment)
        return payment

    def reject_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> PixPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status not in CANCELABLE:
            raise PixError('not_rejectable', 'Only held or pending PIX can be rejected.')
        payment.status = PIX_REJECTED
        payment.reason = normalize_note(reason, limit=40)
        payment.note = normalize_note(note)
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        self.store.update_payment(payment)
        return payment

    def _assert_med_window(self, payment: PixPayment, *, actor_type: str) -> None:
        now = float(self.clock())
        completed = float(payment.completed_at or payment.updated_at or payment.created_at)
        elapsed = now - completed
        if actor_type in EMPLOYEE_ROLES:
            if elapsed > self.policy.staff_med_days * 86400:
                raise PixError('med_window_closed', 'Staff MED return window has closed.')
            return
        if elapsed > self.policy.customer_return_seconds:
            raise PixError('return_window_closed', 'Customer PIX return window has closed.')

    def recall_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> PixPayment:
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == PIX_RECALLED:
            raise PixError('already_recalled', 'PIX payment is already recalled.')
        if payment.status != PIX_COMPLETED:
            raise PixError('not_recallable', 'Only completed PIX payments can be recalled.')
        self._assert_med_window(payment, actor_type=actor_type)
        remark = normalize_note(note) or ('pix med %s' % (mask_end_to_end_id(payment.end_to_end_id) or payment.nickname))
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(payment.internal_account, payment.amount, remark)
            except Exception as exc:
                raise PixError('recall_failed', 'MED credit failed.', payment=payment) from exc
            if _classify_money_result(result) != 'ok':
                raise PixError('recall_failed', 'MED credit failed.', payment=payment)
            fee = parse_money(payment.fee, allow_zero=True)
            if fee > 0 and payment.fee_status == FEE_COLLECTED:
                self.credit_fn(
                    payment.internal_account, money_str(fee),
                    'pix fee recalled %s' % mask_end_to_end_id(payment.end_to_end_id),
                )
        now = float(self.clock())
        payment.status = PIX_RECALLED
        payment.recalled_at = now
        payment.updated_at = now
        payment.actor = str(actor)
        payment.note = remark
        self.store.update_payment(payment)
        return payment

    def export_pix1(self, *, payment_id: str, actor: str, actor_type: str) -> Dict[str, Any]:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status != PIX_COMPLETED:
            raise PixError('not_exportable', 'Only completed PIX can be exported.')
        if not payment.end_to_end_id:
            raise PixError('not_exportable', 'EndToEndId has not been assigned.')
        record = compose_pix1(
            end_to_end_id=payment.end_to_end_id,
            txid=payment.txid,
            send_ispb=self.policy.source_ispb,
            recv_ispb=payment.ispb,
            key_type=payment.key_type,
            key_value=self._key_value(payment),
            amount=parse_money(payment.amount),
            purpose=payment.purpose,
            status=payment.status,
            mask=True,
        )
        return {
            'payment_id': payment.payment_id,
            'pix1': record,
            'end_to_end_id_masked': mask_end_to_end_id(payment.end_to_end_id),
        }

    def run_due(self, userid: Optional[str] = None) -> List[PixPayment]:
        _ = userid
        return []

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        _ = actor
        self.run_due(userid)
        keys = self.store.list_keys(userid)
        payments = self.store.list_payments(userid)
        sent_ytd = Decimal('0.00')
        fee_ytd = Decimal('0.00')
        recalled = Decimal('0.00')
        for row in payments:
            amount = parse_money(row.amount, allow_zero=True)
            if row.status == PIX_COMPLETED:
                sent_ytd += amount
                if row.fee_status == FEE_COLLECTED:
                    fee_ytd += parse_money(row.fee, allow_zero=True)
            elif row.status == PIX_RECALLED:
                recalled += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'day_max': money_str(self.policy.day_max),
            'night_max': money_str(self.policy.night_max),
            'fee': money_str(self.policy.outbound_fee),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'currency': 'BRL',
            'clock': self.calendar.snapshot(now),
            'keys': [row.to_dict() for row in keys[:40]],
            'payments': [row.to_dict() for row in payments[:40]],
            'ytd_sent': money_str(sent_ytd),
            'ytd_fees': money_str(fee_ytd),
            'recalled_ytd': money_str(recalled),
            'active_count': sum(1 for row in keys if row.status == KEY_ACTIVE),
            'open_count': sum(1 for row in payments if row.status in OPEN_PIX),
        }


_SERVICE: Optional[PixService] = None


def set_service(service: Optional[PixService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[PixService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('PIX_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryPixStore()
    path = os.environ.get('PIX_DB', DEFAULT_STORE_PATH)
    return SqlitePixStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[SpiClock] = None,
) -> PixService:
    if store is None:
        store = default_store()
    return PixService(
        PixPolicy.from_env(),
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
        'key_duplicate': 409,
        'key_limit': 409,
        'payment_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_recalled': 409,
        'nsf': 409,
        'failed': 409,
        'recall_failed': 409,
        'pix_forbidden': 403,
        'pix_disabled': 403,
        'key_paused': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_cancelable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'not_recallable': 403,
        'not_overridable': 403,
        'not_exportable': 403,
        'return_window_closed': 403,
        'med_window_closed': 403,
        'key_not_found': 404,
        'payment_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_key': 400,
        'invalid_ispb': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_currency': 400,
        'invalid_reference': 400,
        'invalid_file': 400,
        'pix_amount_exceeded': 400,
        'night_amount_exceeded': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_key': 400,
        'missing_payment': 400,
    }.get(code, 400)


def _error_body(exc: PixError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('payment') is not None:
        body['payment'] = exc.extra['payment'].to_dict()
    if exc.extra.get('key') is not None:
        body['key'] = exc.extra['key'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except PixError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list_pix(service: PixService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Pix': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_add_key(service: PixService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.add_key(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname'),
            legal_name=values.get('legal_name') or values.get('name'),
            key_type=values.get('key_type') or values.get('type'),
            key_value=values.get('key_value') or values.get('key') or values.get('pix_key'),
            ispb=values.get('ispb') or values.get('participant'),
            default_account=values.get('default_account') or values.get('account') or values.get('from_account'),
        )
        return jsonify({
            'message': 'PIX key added',
            'key': row.to_dict(),
            'Pix': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _key_status_route(service: PixService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    key_id = str(values.get('key_id') or '').strip()
    if not key_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_key'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_key_status(
            key_id=key_id, actor=userid, actor_type=actor_type, status=status,
        )
        return jsonify({
            'message': ok_message,
            'key': row.to_dict(),
            'Pix': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: PixService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    key_id = str(values.get('key_id') or '').strip()
    if not key_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_key'}), 400

    def _run():
        preview = service.preview(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            key_id=key_id,
            amount=values.get('amount'),
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({'preview': preview, 'Pix': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: PixService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    key_id = str(values.get('key_id') or '').strip()
    if not key_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_key'}), 400

    def _run():
        payment, created = service.originate(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            key_id=key_id,
            amount=values.get('amount'),
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            trace_id=values.get('trace_id'),
            txid=values.get('txid') or '',
            waive_fee=bool(values.get('waive_fee')),
        )
        return jsonify({
            'message': 'PIX originated' if created else 'PIX already posted',
            'payment': payment.to_dict(),
            'Pix': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: PixService):
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
            'message': 'PIX cancelled',
            'payment': payment.to_dict(),
            'Pix': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_pix_route(service: PixService, action: str):
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
            message = 'PIX released'
        elif action == 'reject':
            payment = service.reject_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'PIX rejected'
        elif action == 'recall':
            payment = service.recall_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'PIX recalled'
        elif action == 'override':
            payment = service.override_ofac(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'waive':
            payment = service.waive_fee(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'PIX fee waived'
        elif action == 'export':
            exported = service.export_pix1(payment_id=payment_id, actor=userid, actor_type=actor_type)
            payment = service.get_payment(payment_id=payment_id, actor=userid, actor_type=actor_type)
            return jsonify({
                'message': 'PIX exported',
                'export': exported,
                'payment': payment.to_dict(),
                'Pix': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
            }), 200
        else:
            raise PixError('invalid_status', 'Unknown PIX action.')
        return jsonify({
            'message': message,
            'payment': payment.to_dict(),
            'Pix': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: PixService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'Pix': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_pix_routes(app, service: PixService) -> None:
    @app.route('/listPix', methods=['POST', 'GET'])
    def list_pix_route():
        return handle_list_pix(service)

    @app.route('/listPixKeys', methods=['POST', 'GET'])
    def list_pix_keys_route():
        return handle_list_pix(service)

    @app.route('/addPixKey', methods=['POST', 'GET'])
    def add_pix_key_route():
        return handle_add_key(service)

    @app.route('/pausePixKey', methods=['POST', 'GET'])
    def pause_pix_key_route():
        return _key_status_route(service, KEY_PAUSED, 'PIX key paused')

    @app.route('/resumePixKey', methods=['POST', 'GET'])
    def resume_pix_key_route():
        return _key_status_route(service, KEY_ACTIVE, 'PIX key resumed')

    @app.route('/archivePixKey', methods=['POST', 'GET'])
    def archive_pix_key_route():
        return _key_status_route(service, KEY_ARCHIVED, 'PIX key archived')

    @app.route('/previewPix', methods=['POST', 'GET'])
    def preview_pix_route():
        return handle_preview(service)

    @app.route('/sendPix', methods=['POST', 'GET'])
    def send_pix_route():
        return handle_send(service)

    @app.route('/cancelPix', methods=['POST', 'GET'])
    def cancel_pix_route():
        return handle_cancel(service)

    @app.route('/releasePix', methods=['POST', 'GET'])
    def release_pix_route():
        return _staff_pix_route(service, 'release')

    @app.route('/rejectPix', methods=['POST', 'GET'])
    def reject_pix_route():
        return _staff_pix_route(service, 'reject')

    @app.route('/recallPix', methods=['POST', 'GET'])
    def recall_pix_route():
        return _staff_pix_route(service, 'recall')

    @app.route('/overridePixOfac', methods=['POST', 'GET'])
    def override_pix_ofac_route():
        return _staff_pix_route(service, 'override')

    @app.route('/waivePixFee', methods=['POST', 'GET'])
    def waive_pix_fee_route():
        return _staff_pix_route(service, 'waive')

    @app.route('/exportPix', methods=['POST', 'GET'])
    def export_pix_route():
        return _staff_pix_route(service, 'export')

    @app.route('/runDuePix', methods=['POST', 'GET'])
    def run_due_pix_route():
        return handle_run_due(service)
