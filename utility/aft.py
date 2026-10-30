"""Pull-from-card origination (Visa Direct AFT / Mastercard Send Funding).

Customers pull USD Account Funding Transactions from an external debit or
prepaid PAN into checking or savings. Independent of push-to-card OCT
(PR #155), inbound OCT/AFT receive (PR #159), card 3DS (PR #153),
authorization holds (PR #149), settlement presentment (PR #151),
outbound Fedwire (PR #73), and ACH linking (PR #68). Existing
`/fundTransfer`, `/withdrawAmount`, and `/sendWire` stay unchanged. AFT credits a DDA from a card; it does not push to a card.

Foundations (reusable beyond this screen):
- PAN Luhn + IIN brand (not ABA / ARN / STAN-as-auth / CAVV)
- PAN fingerprint uniqueness (SHA-256, never in snapshots)
- ISO 8583 AFT MTI 0200 / reversal 0420 with processing code 100000
  (not OCT 0260 / 260000, auth 1100, or presentment 1240)
- AFT RRN / STAN uniqueness
- 24/7 card-network clock (not Fedwire 17:00 ET cutoff)
- AFT1 pipe compose/parse
- Dual-control for high-value pulls
- 24h customer recall / 90-day staff recall of a completed AFT

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Full PAN, fingerprint, RRN, STAN, and processing code never appear unmasked in to_dict / snapshots.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

from utility.wire import (
    AccountError,
    AmountError,
    DEFAULT_WATCHLIST,
    EMPLOYEE_ROLES,
    ScreenResult,
    account_types_from_customer_payload,
    last4,
    money_str,
    normalize_account,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

SOURCE_ACTIVE = 'active'
SOURCE_PAUSED = 'paused'
SOURCE_ARCHIVED = 'archived'
SOURCE_STATUSES = frozenset({SOURCE_ACTIVE, SOURCE_PAUSED, SOURCE_ARCHIVED})
OPEN_SOURCE = frozenset({SOURCE_ACTIVE, SOURCE_PAUSED})
AFT_HELD = 'held'
AFT_PENDING = 'pending_release'
AFT_COMPLETED = 'completed'
AFT_REJECTED = 'rejected'
AFT_CANCELLED = 'cancelled'
AFT_RECALLED = 'recalled'
AFT_NSF = 'nsf'
AFT_FAILED = 'failed'
AFT_STATUSES = frozenset({
    AFT_HELD, AFT_PENDING, AFT_COMPLETED, AFT_REJECTED, AFT_CANCELLED,
    AFT_RECALLED, AFT_NSF, AFT_FAILED,
})
OPEN_AFTS = frozenset({AFT_HELD, AFT_PENDING})
CANCELABLE = frozenset({AFT_HELD, AFT_PENDING})
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
ALLOWED_BRANDS = frozenset({'visa', 'mastercard'})
NETWORK_FOR_BRAND = {
    'visa': 'visa_direct',
    'mastercard': 'mastercard_send',
}
ALLOWED_NETWORKS = frozenset(NETWORK_FOR_BRAND.values())
ALLOWED_MTI = frozenset({'0200', '0420'})
MTI_ALIASES = {
    '200': '0200', '0200': '0200', 'aft': '0200', 'funding': '0200', 'pull': '0200',
    '420': '0420', '0420': '0420', 'reversal': '0420', 'reverse': '0420',
}
ALLOWED_MCC = frozenset({'4829', '6012', '6536', '6537', '6538'})
DEFAULT_MCC = '6012'
ALLOWED_PROC = frozenset({'100000'})
PROC_ALIASES = {
    '10': '100000', '100000': '100000', 'aft': '100000', 'funding': '100000', 'pull': '100000',
}
DEFAULT_PROC = '100000'
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/aft.sqlite'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)
CUSTOMER_RECALL_SECONDS = 24 * 60 * 60
STAFF_RECALL_SECONDS = 90 * 24 * 60 * 60


class AftError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def normalize_nickname(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise AftError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise AftError('invalid_name', 'Cardholder legal name must be 2-80 characters.')
    return text


def normalize_note(value: Any, *, limit: int = 500) -> str:
    return str(value or '').strip()[:limit]


def normalize_id(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return uuid.uuid4().hex
    return text[:120]


def luhn_check_digit_ok(digits: str) -> bool:
    """ISO/IEC 7812 Luhn. Distinct from ABA routing checksum."""
    if not digits or not digits.isdigit() or len(digits) < 12:
        return False
    total = 0
    for index, ch in enumerate(reversed(digits)):
        n = int(ch)
        if index % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def brand_from_iin(pan: str) -> str:
    digits = ''.join(ch for ch in str(pan or '') if ch.isdigit())
    if not digits:
        return 'unknown'
    if digits.startswith('4'):
        return 'visa'
    if digits[:2] in {'51', '52', '53', '54', '55'}:
        return 'mastercard'
    if len(digits) >= 6:
        iin = int(digits[:6])
        if 222100 <= iin <= 272099:
            return 'mastercard'
    if digits[:2] in {'34', '37'}:
        return 'amex'
    if digits.startswith('6011') or digits.startswith('65') or digits[:3] in {'644', '645', '646', '647', '648', '649'}:
        return 'discover'
    return 'unknown'


def normalize_pan(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) not in {13, 15, 16, 19} or not luhn_check_digit_ok(digits):
        raise AftError('invalid_pan', 'PAN failed Luhn check or length.')
    return digits


def mask_pan(pan: str) -> str:
    digits = ''.join(ch for ch in str(pan or '') if ch.isdigit())
    if len(digits) < 10:
        return '****'
    return digits[:6] + ('*' * (len(digits) - 10)) + digits[-4:]


def pan_fingerprint(pan: str) -> str:
    digits = ''.join(ch for ch in str(pan or '') if ch.isdigit())
    return hashlib.sha256(digits.encode('ascii')).hexdigest()


def network_for_brand(brand: str) -> str:
    network = NETWORK_FOR_BRAND.get(brand)
    if network is None:
        raise AftError('invalid_brand', 'Only Visa Direct AFT and Mastercard Send Funding are supported.')
    return network


def normalize_brand(value: Any, *, pan: str = '') -> str:
    text = str(value or '').strip().lower().replace(' ', '_')
    aliases = {
        'visa_direct': 'visa', 'vs': 'visa',
        'mc': 'mastercard', 'master': 'mastercard', 'master_card': 'mastercard',
        'mastercard_send': 'mastercard', 'moneysend': 'mastercard',
    }
    text = aliases.get(text, text)
    if not text and pan:
        text = brand_from_iin(pan)
    if text not in ALLOWED_BRANDS:
        raise AftError('invalid_brand', 'Only Visa and Mastercard debit/prepaid funding sources are supported.')
    if pan:
        detected = brand_from_iin(pan)
        if detected != text:
            raise AftError('invalid_brand', 'Brand does not match PAN IIN.')
    return text


def normalize_expiry(value: Any, *, now: Optional[float] = None) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) == 6:
        digits = digits[:2] + digits[4:6]
    if len(digits) != 4:
        raise AftError('invalid_expiry', 'Expiry must be MMYY.')
    month = int(digits[:2])
    year = int(digits[2:])
    if month < 1 or month > 12:
        raise AftError('invalid_expiry', 'Expiry month is invalid.')
    if now is not None:
        local = datetime.fromtimestamp(float(now), tz=timezone.utc)
        exp_year = 2000 + year if year < 100 else year
        if (exp_year, month) < (local.year, local.month):
            raise AftError('invalid_expiry', 'Card is expired.')
    return digits


def normalize_mti(value: Any, *, default: str = '0200') -> str:
    text = str(value or default).strip().lower()
    text = MTI_ALIASES.get(text, text)
    if text not in ALLOWED_MTI:
        raise AftError('invalid_mti', 'MTI must be 0200 (AFT) or 0420 (reversal).')
    return text


def normalize_mcc(value: Any, *, default: str = DEFAULT_MCC) -> str:
    digits = ''.join(ch for ch in str(value or default) if ch.isdigit())
    if len(digits) != 4 or digits not in ALLOWED_MCC:
        raise AftError('invalid_mcc', 'MCC must be an AFT funding code (6012/6536/6537/6538/4829).')
    return digits


def normalize_processing_code(value: Any, *, default: str = DEFAULT_PROC) -> str:
    text = str(value or default).strip().lower().replace(' ', '_')
    text = PROC_ALIASES.get(text, text)
    digits = ''.join(ch for ch in text if ch.isdigit())
    if len(digits) == 2:
        digits = digits + '0000'
    if digits not in ALLOWED_PROC:
        raise AftError('invalid_processing_code', 'Processing code must be 100000 (AFT). OCT 260000 is rejected.')
    return digits


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise AftError('invalid_purpose', 'Unknown card-pull purpose.')
    return text


def compose_stan(sequence: int) -> str:
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise AftError('invalid_stan', 'STAN sequence out of range.')
    return '%06d' % seq


def normalize_stan(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 6:
        raise AftError('invalid_stan', 'STAN must be six digits.')
    return digits


def mask_stan(stan: str) -> str:
    digits = ''.join(ch for ch in str(stan or '') if ch.isdigit())
    if len(digits) < 2:
        return '**'
    return ('*' * (len(digits) - 2)) + digits[-2:]


def compose_rrn(cycle_date: str, sequence: int) -> str:
    """12-digit AFT RRN: YYMMDD + 6-digit sequence. Not a Fedwire IMAD, OCT RRN, or card ARN."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise AftError('invalid_rrn', 'Cycle date must be YYYYMMDD.')
    return day[2:] + compose_stan(sequence)


def normalize_rrn(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 12:
        raise AftError('invalid_rrn', 'RRN must be twelve digits.')
    return digits


def mask_rrn(rrn: str) -> str:
    digits = ''.join(ch for ch in str(rrn or '') if ch.isdigit())
    if len(digits) < 4:
        return '****'
    return ('*' * (len(digits) - 4)) + digits[-4:]


def compute_fee(amount: Decimal, fee: Decimal, *, waived: bool = False) -> Decimal:
    if waived:
        return Decimal('0.00')
    _ = amount
    return fee.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def compose_aft1(
    *,
    mti: str,
    processing_code: str,
    network: str,
    pan_masked: str,
    brand: str,
    amount: str,
    rrn_masked: str,
    stan_masked: str,
    mcc: str,
    purpose: str,
    currency: str = 'USD',
) -> str:
    return '|'.join((
        'AFT1',
        mti,
        processing_code,
        network,
        pan_masked,
        brand,
        amount,
        currency,
        rrn_masked,
        stan_masked,
        mcc,
        purpose,
    ))


def parse_aft1(text: Any) -> Dict[str, str]:
    raw = str(text or '')
    if '<?xml' in raw.lower() or '<!doctype' in raw.lower() or '<' in raw:
        raise AftError('invalid_file', 'XML/DOCTYPE AFT payloads are rejected.')
    parts = [item.strip() for item in raw.strip().split('|')]
    if len(parts) < 12 or parts[0] != 'AFT1':
        raise AftError('invalid_file', 'AFT1 record is malformed.')
    return {
        'version': parts[0],
        'mti': normalize_mti(parts[1]),
        'processing_code': normalize_processing_code(parts[2]),
        'network': parts[3],
        'pan_masked': parts[4],
        'brand': parts[5],
        'amount': parts[6],
        'currency': parts[7],
        'rrn_masked': parts[8],
        'stan_masked': parts[9],
        'mcc': normalize_mcc(parts[10]),
        'purpose': normalize_purpose(parts[11]),
    }


def split_aft1_file(text: Any) -> List[str]:
    raw = str(text or '')
    if '<?xml' in raw.lower() or '<!doctype' in raw.lower():
        raise AftError('invalid_file', 'XML/DOCTYPE AFT payloads are rejected.')
    lines = []
    for line in raw.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        stripped = line.strip()
        if stripped:
            lines.append(stripped)
    if not lines:
        raise AftError('invalid_file', 'AFT1 file is empty.')
    return lines


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


class CardNetworkClock:
    """24/7 Visa Direct / Mastercard Send clock. Not a Fedwire cutoff calendar."""

    def __init__(self, *, tz_offset_hours: int = 0) -> None:
        self.tz_offset_hours = int(tz_offset_hours)
        self.tz = timezone(timedelta(hours=self.tz_offset_hours))

    def local_dt(self, ts: float) -> datetime:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(self.tz)

    def cycle_date(self, ts: float) -> str:
        return self.local_dt(ts).strftime('%Y%m%d')

    def snapshot(self, ts: float) -> Dict[str, Any]:
        local = self.local_dt(ts)
        return {
            'local_date': local.date().isoformat(),
            'local_time': local.strftime('%H:%M'),
            'cutoff': '24/7',
            'after_cutoff': False,
            'business_day': True,
            'always_open': True,
            'value_date': local.date().isoformat(),
            'cycle_date': local.strftime('%Y%m%d'),
        }


@dataclass
class AftPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    allow_credit: bool = False
    max_sources: int = 12
    max_payments: int = 120
    min_amount: Decimal = Decimal('1.00')
    max_amount: Decimal = Decimal('5000.00')
    outbound_fee: Decimal = Decimal('2.00')
    dual_control_threshold: Decimal = Decimal('2500.00')
    customer_recall_seconds: int = CUSTOMER_RECALL_SECONDS
    staff_recall_seconds: int = STAFF_RECALL_SECONDS
    tz_offset_hours: int = 0
    default_mcc: str = DEFAULT_MCC
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST

    @classmethod
    def from_env(cls) -> 'AftPolicy':
        extra = _env_list('AFT_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('AFT_ENABLED', True),
            customer_manage=_env_bool('AFT_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('AFT_CUSTOMER_SEND', True),
            allow_credit=_env_bool('AFT_ALLOW_CREDIT', False),
            max_sources=max(1, _env_int('AFT_MAX_SOURCES', 12)),
            max_payments=max(1, _env_int('AFT_MAX_PAYMENTS', 120)),
            min_amount=_env_money('AFT_MIN_AMOUNT', '1.00'),
            max_amount=_env_money('AFT_MAX_AMOUNT', '5000.00'),
            outbound_fee=_env_money('AFT_FEE', '2.00'),
            dual_control_threshold=_env_money('AFT_DUAL_CONTROL', '2500.00'),
            customer_recall_seconds=max(60, _env_int('AFT_CUSTOMER_RECALL_SECONDS', CUSTOMER_RECALL_SECONDS)),
            staff_recall_seconds=max(60, _env_int('AFT_STAFF_RECALL_SECONDS', STAFF_RECALL_SECONDS)),
            tz_offset_hours=_env_int('AFT_TZ_OFFSET', 0),
            default_mcc=normalize_mcc(os.environ.get('AFT_MCC', DEFAULT_MCC)),
            watchlist=watch,
        )


@dataclass
class AftSource:
    source_id: str
    userid: str
    nickname: str
    legal_name: str
    pan: str
    fingerprint: str
    brand: str
    network: str
    expiry: str
    default_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'source_id': self.source_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'pan_masked': mask_pan(self.pan),
            'brand': self.brand,
            'network': self.network,
            'last4': last4(self.pan),
            'expiry': self.expiry,
            'default_account': self.default_account,
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == SOURCE_ACTIVE,
            'paused': self.status == SOURCE_PAUSED,
            'archived': self.status == SOURCE_ARCHIVED,
        }


@dataclass
class AftPayment:
    payment_id: str
    trace_id: str
    source_id: str
    userid: str
    internal_account: str
    amount: str
    fee: str
    fee_status: str
    nickname: str
    legal_name: str
    pan_masked: str
    brand: str
    network: str
    last4: str
    purpose: str
    memo: str
    status: str
    mti: str
    processing_code: str
    mcc: str
    rrn: str
    stan: str
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            'payment_id': self.payment_id,
            'trace_id': self.trace_id,
            'source_id': self.source_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'amount': self.amount,
            'fee': self.fee,
            'fee_status': self.fee_status,
            'nickname': self.nickname,
            'legal_name': self.legal_name,
            'pan_masked': self.pan_masked,
            'brand': self.brand,
            'network': self.network,
            'last4': self.last4,
            'purpose': self.purpose,
            'memo': self.memo,
            'status': self.status,
            'mti': self.mti,
            'processing_code': self.processing_code,
            'mcc': self.mcc,
            'rrn_masked': mask_rrn(self.rrn) if self.rrn else '',
            'stan_masked': mask_stan(self.stan) if self.stan else '',
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
            'held': self.status == AFT_HELD,
            'pending_release': self.status == AFT_PENDING,
            'completed': self.status == AFT_COMPLETED,
            'cancelable': self.status in CANCELABLE,
            'recallable': self.status == AFT_COMPLETED,
        }


def _clone_source(row: AftSource) -> AftSource:
    return AftSource(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_payment(row: AftPayment) -> AftPayment:
    return AftPayment(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _source_from_row(row: Any) -> AftSource:
    return AftSource(
        source_id=row['source_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        pan=row['pan'],
        fingerprint=row['fingerprint'],
        brand=row['brand'],
        network=row['network'],
        expiry=row['expiry'],
        default_account=row['default_account'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
    )


def _payment_from_row(row: Any) -> AftPayment:
    return AftPayment(
        payment_id=row['payment_id'],
        trace_id=row['trace_id'],
        source_id=row['source_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        amount=row['amount'],
        fee=row['fee'],
        fee_status=row['fee_status'],
        nickname=row['nickname'],
        legal_name=row['legal_name'],
        pan_masked=row['pan_masked'],
        brand=row['brand'],
        network=row['network'],
        last4=row['last4'],
        purpose=row['purpose'],
        memo=row['memo'] or '',
        status=row['status'],
        mti=row['mti'],
        processing_code=row['processing_code'] if 'processing_code' in row.keys() else DEFAULT_PROC,
        mcc=row['mcc'],
        rrn=row['rrn'] or '',
        stan=row['stan'] or '',
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        completed_at=float(row['completed_at'] or 0),
        recalled_at=float(row['recalled_at'] or 0),
        note=row['note'] or '',
        reason=row['reason'] or '',
    )


class MemoryAftStore:
    def __init__(self) -> None:
        self._sources: Dict[str, AftSource] = {}
        self._payments: Dict[str, AftPayment] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_source(self, row: AftSource) -> None:
        with self._lock:
            self._sources[row.source_id] = row

    def get_source(self, source_id: str) -> Optional[AftSource]:
        with self._lock:
            row = self._sources.get(source_id)
            return _clone_source(row) if row else None

    def update_source(self, row: AftSource) -> None:
        with self._lock:
            self._sources[row.source_id] = row

    def list_sources(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[AftSource]:
        with self._lock:
            rows = [_clone_source(row) for row in self._sources.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != SOURCE_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_source_by_nickname(self, userid: str, nickname: str) -> Optional[AftSource]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._sources.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_SOURCE:
                    return _clone_source(row)
        return None

    def find_source_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[AftSource]:
        with self._lock:
            for row in self._sources.values():
                if row.userid == userid and row.fingerprint == fingerprint and row.status in OPEN_SOURCE:
                    return _clone_source(row)
        return None

    def put_payment(self, row: AftPayment) -> AftPayment:
        with self._lock:
            existing_id = self._by_trace.get(row.trace_id)
            if existing_id is not None:
                return self._payments[existing_id]
            self._payments[row.payment_id] = row
            self._by_trace[row.trace_id] = row.payment_id
            return row

    def update_payment(self, row: AftPayment) -> None:
        with self._lock:
            self._payments[row.payment_id] = row

    def get_payment(self, payment_id: str) -> Optional[AftPayment]:
        with self._lock:
            row = self._payments.get(payment_id)
            return _clone_payment(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[AftPayment]:
        with self._lock:
            payment_id = self._by_trace.get(trace_id)
            return _clone_payment(self._payments[payment_id]) if payment_id else None

    def list_payments(self, userid: Optional[str] = None, source_id: Optional[str] = None) -> List[AftPayment]:
        with self._lock:
            rows = [_clone_payment(row) for row in self._payments.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if source_id is not None:
            rows = [row for row in rows if row.source_id == source_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def next_stan_sequence(self, cycle_date: str) -> int:
        prefix = cycle_date[2:]
        with self._lock:
            used = [row.rrn for row in self._payments.values() if row.rrn.startswith(prefix)]
        return len(used) + 1


class SqliteAftStore:
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
                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    pan TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    brand TEXT NOT NULL,
                    network TEXT NOT NULL,
                    expiry TEXT NOT NULL,
                    default_account TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    payment_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    source_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    internal_account TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    fee_status TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    legal_name TEXT NOT NULL,
                    pan_masked TEXT NOT NULL,
                    brand TEXT NOT NULL,
                    network TEXT NOT NULL,
                    last4 TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    mti TEXT NOT NULL,
                    processing_code TEXT NOT NULL DEFAULT '100000',
                    mcc TEXT NOT NULL,
                    rrn TEXT NOT NULL DEFAULT '',
                    stan TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL NOT NULL DEFAULT 0,
                    recalled_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_source(self, row: AftSource) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sources (
                    source_id, userid, nickname, legal_name, pan, fingerprint,
                    brand, network, expiry, default_account, status, actor,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.source_id, row.userid, row.nickname, row.legal_name, row.pan,
                    row.fingerprint, row.brand, row.network, row.expiry, row.default_account,
                    row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_source(self, source_id: str) -> Optional[AftSource]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM sources WHERE source_id = ?', (source_id,),
            ).fetchone()
        return _source_from_row(row) if row else None

    def update_source(self, row: AftSource) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE sources SET nickname=?, legal_name=?, pan=?, fingerprint=?,
                    brand=?, network=?, expiry=?, default_account=?, status=?,
                    actor=?, updated_at=?
                WHERE source_id=?
                """,
                (
                    row.nickname, row.legal_name, row.pan, row.fingerprint, row.brand,
                    row.network, row.expiry, row.default_account, row.status, row.actor,
                    row.updated_at, row.source_id,
                ),
            )
            conn.commit()

    def list_sources(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[AftSource]:
        sql = 'SELECT * FROM sources'
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
        return [_source_from_row(row) for row in rows]

    def find_source_by_nickname(self, userid: str, nickname: str) -> Optional[AftSource]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM sources
                WHERE userid = ? AND lower(nickname) = lower(?)
                  AND status IN ('active', 'paused')
                """,
                (userid, nickname),
            ).fetchone()
        return _source_from_row(row) if row else None

    def find_source_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[AftSource]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM sources
                WHERE userid = ? AND fingerprint = ?
                  AND status IN ('active', 'paused')
                """,
                (userid, fingerprint),
            ).fetchone()
        return _source_from_row(row) if row else None

    def put_payment(self, row: AftPayment) -> AftPayment:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM payments WHERE trace_id = ?', (row.trace_id,)
            ).fetchone()
            if existing is not None:
                return _payment_from_row(existing)
            conn.execute(
                """
                INSERT INTO payments (
                    payment_id, trace_id, source_id, userid, internal_account,
                    amount, fee, fee_status, nickname, legal_name, pan_masked, brand,
                    network, last4, purpose, memo, status, mti, processing_code, mcc, rrn, stan, actor,
                    releaser, ofac_hit, ofac_match, created_at, updated_at, completed_at,
                    recalled_at, note, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.payment_id, row.trace_id, row.source_id, row.userid,
                    row.internal_account, row.amount, row.fee, row.fee_status,
                    row.nickname, row.legal_name, row.pan_masked, row.brand, row.network,
                    row.last4, row.purpose, row.memo, row.status, row.mti, row.processing_code,
                    row.mcc, row.rrn, row.stan, row.actor, row.releaser, row.ofac_hit,
                    row.ofac_match, row.created_at, row.updated_at, row.completed_at,
                    row.recalled_at, row.note, row.reason,
                ),
            )
            conn.commit()
            return row

    def update_payment(self, row: AftPayment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE payments SET fee=?, fee_status=?, status=?, mti=?, processing_code=?, rrn=?, stan=?,
                    actor=?, releaser=?, ofac_hit=?, ofac_match=?, updated_at=?,
                    completed_at=?, recalled_at=?, note=?, reason=?
                WHERE payment_id=?
                """,
                (
                    row.fee, row.fee_status, row.status, row.mti, row.processing_code, row.rrn, row.stan,
                    row.actor, row.releaser, row.ofac_hit, row.ofac_match, row.updated_at,
                    row.completed_at, row.recalled_at, row.note, row.reason, row.payment_id,
                ),
            )
            conn.commit()

    def get_payment(self, payment_id: str) -> Optional[AftPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE payment_id = ?', (payment_id,)).fetchone()
        return _payment_from_row(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[AftPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE trace_id = ?', (trace_id,)).fetchone()
        return _payment_from_row(row) if row else None

    def list_payments(self, userid: Optional[str] = None, source_id: Optional[str] = None) -> List[AftPayment]:
        sql = 'SELECT * FROM payments'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if source_id is not None:
            clauses.append('source_id = ?')
            params.append(source_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_payment_from_row(row) for row in rows]

    def next_stan_sequence(self, cycle_date: str) -> int:
        prefix = cycle_date[2:] + '%'
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE rrn LIKE ?",
                (prefix,),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class AftService:
    def __init__(
        self,
        policy: AftPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        issuer_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[CardNetworkClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.issuer_fn = issuer_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or CardNetworkClock(tz_offset_hours=policy.tz_offset_hours)

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise AftError('aft_disabled', 'Card pull is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise AftError('aft_forbidden', 'Customers cannot manage card-pull sources.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise AftError('aft_forbidden', 'Customers cannot originate card pulls.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise AftError('aft_forbidden', 'Staff only.')

    def _owned_accounts(self, userid: str) -> List[str]:
        if self.accounts_fn is None:
            return []
        return own_accounts_from_customer_payload(self.accounts_fn(userid))

    def _account_types(self, userid: str) -> Dict[str, str]:
        if self.accounts_fn is None:
            return {}
        return account_types_from_customer_payload(self.accounts_fn(userid))

    def _assert_internal_account(self, userid: str, account: str) -> None:
        owned = self._owned_accounts(userid)
        if owned and account not in owned:
            raise AftError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise AftError('credit_not_allowed', 'Credit accounts cannot receive card pulls.')

    def _assert_amount(self, dollars: Decimal) -> None:
        if dollars < self.policy.min_amount:
            raise AftError('amount_out_of_range', 'Amount is outside the allowed range.')
        if dollars > self.policy.max_amount:
            raise AftError('aft_amount_exceeded', 'AFT amount exceeds the $5,000 cap.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def add_source(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        legal_name: Any,
        pan: Any,
        expiry: Any,
        default_account: Any,
        brand: Any = None,
    ) -> AftSource:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise AftError('aft_forbidden', 'Not allowed to add funding sources for this customer.')
        name = normalize_nickname(nickname)
        legal = normalize_legal_name(legal_name)
        card = normalize_pan(pan)
        detected = brand_from_iin(card)
        card_brand = normalize_brand(brand or detected, pan=card)
        network = network_for_brand(card_brand)
        now = float(self.clock())
        exp = normalize_expiry(expiry, now=now)
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        fingerprint = pan_fingerprint(card)
        if self.store.find_source_by_nickname(owner_userid, name) is not None:
            raise AftError('source_duplicate', 'A funding source with that nickname already exists.')
        if self.store.find_source_by_fingerprint(owner_userid, fingerprint) is not None:
            raise AftError('source_duplicate', 'That card is already enrolled as a funding source.')
        open_rows = [row for row in self.store.list_sources(owner_userid) if row.status in OPEN_SOURCE]
        if len(open_rows) >= self.policy.max_sources:
            raise AftError('source_limit', 'Card-pull source limit reached.')
        row = AftSource(
            source_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            pan=card,
            fingerprint=fingerprint,
            brand=card_brand,
            network=network,
            expiry=exp,
            default_account=account,
            status=SOURCE_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_source(row)
        return row

    def get_source(self, *, source_id: str, actor: str, actor_type: str) -> AftSource:
        self._require_enabled()
        row = self.store.get_source(source_id)
        if row is None:
            raise AftError('source_not_found', 'Card-pull source not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise AftError('aft_forbidden', 'Not allowed to view this funding source.')
        return row

    def enforce_source(
        self,
        *,
        source_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> AftSource:
        row = self.get_source(source_id=source_id, actor=actor, actor_type=actor_type)
        if row.status == SOURCE_ARCHIVED:
            raise AftError('already_archived', 'Beneficiary is archived.')
        if row.status == SOURCE_PAUSED:
            raise AftError('source_paused', 'Beneficiary is paused.')
        if require_active and row.status != SOURCE_ACTIVE:
            raise AftError('invalid_status', 'Beneficiary is not active.')
        normalize_expiry(row.expiry, now=float(self.clock()))
        return row

    def set_source_status(
        self,
        *,
        source_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> AftSource:
        self._require_manage(actor_type)
        row = self.get_source(source_id=source_id, actor=actor, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': SOURCE_PAUSED, 'hold': SOURCE_PAUSED,
            'resume': SOURCE_ACTIVE, 'activate': SOURCE_ACTIVE, 'unpause': SOURCE_ACTIVE,
            'archive': SOURCE_ARCHIVED, 'close': SOURCE_ARCHIVED, 'cancel': SOURCE_ARCHIVED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {SOURCE_PAUSED, SOURCE_ACTIVE, SOURCE_ARCHIVED}:
            raise AftError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == SOURCE_ARCHIVED:
            raise AftError('already_archived', 'Beneficiary is already archived.')
        if wanted == SOURCE_PAUSED:
            if row.status == SOURCE_PAUSED:
                raise AftError('already_paused', 'Beneficiary is already paused.')
            if row.status != SOURCE_ACTIVE:
                raise AftError('invalid_status', 'Only an active funding source can be paused.')
        elif wanted == SOURCE_ACTIVE:
            if row.status == SOURCE_ACTIVE:
                raise AftError('already_active', 'Beneficiary is already active.')
            if row.status != SOURCE_PAUSED:
                raise AftError('invalid_status', 'Only a paused funding source can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_source(row)
        return row

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        source_id: Any,
        amount: Any,
        internal_account: Any = None,
        waive_fee: bool = False,
        mcc: Any = None,
    ) -> Dict[str, Any]:
        self._require_send(actor_type)
        bene = self.enforce_source(
            source_id=str(source_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise AftError('aft_forbidden', 'Funding source does not belong to this customer.')
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        fee = compute_fee(dollars, self.policy.outbound_fee, waived=bool(waive_fee) and actor_type in EMPLOYEE_ROLES)
        now = float(self.clock())
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
        return {
            'amount': money_str(dollars),
            'fee': money_str(fee),
            'net': money_str(dollars - fee),
            'total': money_str(dollars),
            'internal_account': account,
            'mcc': normalize_mcc(mcc or self.policy.default_mcc),
            'mti': '0200',
            'processing_code': DEFAULT_PROC,
            'network': bene.network,
            'brand': bene.brand,
            'source': bene.to_dict(),
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(dollars),
            'clock': self.calendar.snapshot(now),
        }

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        bene: AftSource,
        account: str,
        dollars: Decimal,
        fee: Decimal,
        purpose: str,
        memo: str,
        trace_id: str,
        ofac: ScreenResult,
        waive_fee: bool,
        mcc: str,
    ) -> AftPayment:
        now = float(self.clock())
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        if ofac.hit:
            status = AFT_HELD
        elif self._needs_dual_control(dollars):
            status = AFT_PENDING
        else:
            status = AFT_COMPLETED
        payment = AftPayment(
            payment_id=uuid.uuid4().hex,
            trace_id=trace_id,
            source_id=bene.source_id,
            userid=owner_userid,
            internal_account=account,
            amount=money_str(dollars),
            fee=money_str(fee),
            fee_status=fee_status,
            nickname=bene.nickname,
            legal_name=bene.legal_name,
            pan_masked=mask_pan(bene.pan),
            brand=bene.brand,
            network=bene.network,
            last4=last4(bene.pan),
            purpose=purpose,
            memo=memo,
            status=status,
            mti='0200',
            processing_code=DEFAULT_PROC,
            mcc=mcc,
            rrn='',
            stan='',
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
        )
        if status == AFT_COMPLETED:
            self._transmit(payment, actor=actor)
        return payment

    def _transmit(self, payment: AftPayment, *, actor: str) -> AftPayment:
        now = float(self.clock())
        cycle = self.calendar.cycle_date(now)
        seq = self.store.next_stan_sequence(cycle)
        payment.stan = compose_stan(seq)
        payment.rrn = compose_rrn(cycle, seq)
        payment.mti = '0200'
        payment.processing_code = DEFAULT_PROC
        dollars = parse_money(payment.amount)
        fee = parse_money(payment.fee, allow_zero=True)
        remark = 'card pull %s' % payment.nickname
        status = AFT_COMPLETED
        fail_note = ''
        if self.issuer_fn is not None:
            try:
                result = self.issuer_fn(payment.pan_masked, money_str(dollars), payment.rrn)
            except Exception as exc:
                status = AFT_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = AFT_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = AFT_FAILED
                    fail_note = str(result)[:240]
        if status == AFT_COMPLETED and self.credit_fn is not None:
            try:
                result = self.credit_fn(payment.internal_account, money_str(dollars), remark)
            except Exception as exc:
                status = AFT_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind != 'ok':
                    status = AFT_FAILED
                    fail_note = str(result)[:240]
        if status == AFT_COMPLETED and fee > 0 and payment.fee_status != FEE_WAIVED and self.debit_fn is not None:
            try:
                fee_result = self.debit_fn(
                    payment.internal_account, money_str(fee), 'aft fee %s' % mask_rrn(payment.rrn),
                )
            except Exception:
                payment.fee_status = FEE_NSF
            else:
                kind = _classify_money_result(fee_result)
                payment.fee_status = FEE_COLLECTED if kind == 'ok' else FEE_NSF
        elif status == AFT_COMPLETED and (fee == 0 or payment.fee_status == FEE_WAIVED):
            payment.fee_status = FEE_WAIVED if payment.fee_status == FEE_WAIVED or fee == 0 else payment.fee_status
        payment.status = status
        payment.updated_at = now
        if status == AFT_COMPLETED:
            payment.completed_at = now
            payment.releaser = str(actor)
        else:
            payment.rrn = ''
            payment.stan = ''
            payment.note = fail_note
        return payment

    def originate(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        source_id: Any,
        amount: Any,
        internal_account: Any = None,
        purpose: Any = 'other',
        memo: Any = '',
        trace_id: Any = None,
        waive_fee: bool = False,
        mcc: Any = None,
    ) -> Tuple[AftPayment, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise AftError('aft_forbidden', 'Not allowed to originate card pulls for this customer.')
        bene = self.enforce_source(
            source_id=str(source_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise AftError('aft_forbidden', 'Funding source does not belong to this customer.')
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        account = normalize_account(internal_account or bene.default_account)
        self._assert_internal_account(owner_userid, account)
        staff_waive = bool(waive_fee) and actor_type in EMPLOYEE_ROLES
        fee = compute_fee(dollars, self.policy.outbound_fee, waived=staff_waive)
        trace = normalize_id(trace_id)
        existing = self.store.get_payment_by_trace(trace)
        if existing is not None:
            return existing, False
        if len(self.store.list_payments(owner_userid)) >= self.policy.max_payments:
            raise AftError('payment_limit', 'Card-pull history limit reached.')
        ofac = self._screen(bene.legal_name, aliases=(bene.nickname,))
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
            waive_fee=staff_waive,
            mcc=normalize_mcc(mcc or self.policy.default_mcc),
        )
        stored = self.store.put_payment(payment)
        if stored.payment_id != payment.payment_id:
            return stored, False
        if stored.status == AFT_NSF:
            raise AftError('nsf', 'Card issuer declined the account-funding pull.', payment=stored)
        if stored.status == AFT_FAILED:
            raise AftError('failed', 'Card-pull credit did not complete.', payment=stored)
        return stored, True

    def get_payment(self, *, payment_id: str, actor: str, actor_type: str) -> AftPayment:
        self._require_enabled()
        row = self.store.get_payment(payment_id)
        if row is None:
            raise AftError('payment_not_found', 'Card-pull payment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise AftError('aft_forbidden', 'Not allowed to view this payment.')
        return row

    def cancel_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> AftPayment:
        self._require_send(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and payment.userid != actor:
            raise AftError('aft_forbidden', 'Not allowed to cancel this payment.')
        if payment.status not in CANCELABLE:
            raise AftError('not_cancelable', 'Only held or pending card pulls can be cancelled.')
        payment.status = AFT_CANCELLED
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        payment.note = normalize_note(note)
        self.store.update_payment(payment)
        return payment

    def waive_fee(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
    ) -> AftPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status not in CANCELABLE:
            raise AftError('invalid_status', 'Fee can only be waived before the card pull is sent.')
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
    ) -> AftPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status != AFT_HELD:
            raise AftError('not_overridable', 'Only an OFAC hold can be overridden.')
        now = float(self.clock())
        payment.ofac_hit = 0
        payment.note = normalize_note(note) or 'ofac override'
        payment.actor = str(actor)
        payment.updated_at = now
        dollars = parse_money(payment.amount)
        if self._needs_dual_control(dollars):
            payment.status = AFT_PENDING
        else:
            self._transmit(payment, actor=actor)
        self.store.update_payment(payment)
        if payment.status == AFT_NSF:
            raise AftError('nsf', 'Card issuer declined the account-funding pull.', payment=payment)
        if payment.status == AFT_FAILED:
            raise AftError('failed', 'Card-pull credit did not complete.', payment=payment)
        return payment

    def release_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
    ) -> AftPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == AFT_HELD:
            raise AftError('ofac_hold', 'OFAC hold must be overridden before release.')
        if payment.status != AFT_PENDING:
            raise AftError('not_releasable', 'Only pending card pulls can be released.')
        if (
            payment.actor
            and str(actor) == str(payment.actor)
            and parse_money(payment.amount) >= self.policy.dual_control_threshold
        ):
            raise AftError('same_approver', 'A different employee must release this card pull.')
        self._transmit(payment, actor=actor)
        self.store.update_payment(payment)
        if payment.status == AFT_NSF:
            raise AftError('nsf', 'Card issuer declined the account-funding pull.', payment=payment)
        if payment.status == AFT_FAILED:
            raise AftError('failed', 'Card-pull credit did not complete.', payment=payment)
        return payment

    def reject_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> AftPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status not in CANCELABLE:
            raise AftError('not_rejectable', 'Only held or pending card pulls can be rejected.')
        payment.status = AFT_REJECTED
        payment.reason = normalize_note(reason, limit=40)
        payment.note = normalize_note(note)
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        self.store.update_payment(payment)
        return payment

    def _assert_recall_window(self, payment: AftPayment, *, actor_type: str) -> None:
        if payment.status != AFT_COMPLETED:
            raise AftError('not_recallable', 'Only completed card pulls can be recalled.')
        now = float(self.clock())
        started = payment.completed_at or payment.created_at
        limit = self.policy.staff_recall_seconds if actor_type in EMPLOYEE_ROLES else self.policy.customer_recall_seconds
        if now - started > limit:
            raise AftError('return_window_closed', 'Card-pull recall window has closed.')

    def recall_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> AftPayment:
        if actor_type in EMPLOYEE_ROLES:
            self._require_staff(actor_type)
        else:
            self._require_send(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == AFT_RECALLED:
            raise AftError('already_recalled', 'Card pull is already recalled.')
        if actor_type not in EMPLOYEE_ROLES and payment.userid != actor:
            raise AftError('aft_forbidden', 'Not allowed to recall this payment.')
        self._assert_recall_window(payment, actor_type=actor_type)
        remark = normalize_note(note) or ('card pull return %s' % (payment.rrn[-12:] if payment.rrn else payment.nickname))
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(payment.internal_account, payment.amount, remark)
            except Exception as exc:
                raise AftError('recall_failed', 'Recall debit failed.', payment=payment) from exc
            if _classify_money_result(result) != 'ok':
                raise AftError('recall_failed', 'Recall debit failed.', payment=payment)
            fee = parse_money(payment.fee, allow_zero=True)
            if fee > 0 and payment.fee_status == FEE_COLLECTED and self.credit_fn is not None:
                self.credit_fn(
                    payment.internal_account, money_str(fee),
                    'aft fee recalled %s' % mask_rrn(payment.rrn),
                )
        now = float(self.clock())
        payment.status = AFT_RECALLED
        payment.mti = '0420'
        payment.recalled_at = now
        payment.updated_at = now
        payment.actor = str(actor)
        payment.note = remark
        self.store.update_payment(payment)
        return payment

    def export_payment(self, *, payment_id: str, actor: str, actor_type: str) -> Dict[str, Any]:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status not in {AFT_COMPLETED, AFT_RECALLED}:
            raise AftError('not_exportable', 'Only completed or recalled card pulls can be exported.')
        record = compose_aft1(
            mti=payment.mti,
            processing_code=payment.processing_code or DEFAULT_PROC,
            network=payment.network,
            pan_masked=payment.pan_masked,
            brand=payment.brand,
            amount=payment.amount,
            rrn_masked=mask_rrn(payment.rrn),
            stan_masked=mask_stan(payment.stan),
            mcc=payment.mcc,
            purpose=payment.purpose,
        )
        parsed = parse_aft1(record)
        if 'fingerprint' in parsed or payment.pan_masked.find('*') < 0:
            raise AftError('not_exportable', 'Export must not include a full PAN.')
        return {'file': record, 'payment': payment.to_dict()}

    def run_due(self, userid: Optional[str] = None) -> List[AftPayment]:
        """24/7 rail: nothing queues on a Fed cutoff. Kept for API symmetry."""
        _ = userid
        return []

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.run_due(userid)
        beneficiaries = self.store.list_sources(userid)
        payments = self.store.list_payments(userid)
        sent_ytd = Decimal('0.00')
        fee_ytd = Decimal('0.00')
        recalled = Decimal('0.00')
        for row in payments:
            amount = parse_money(row.amount, allow_zero=True)
            if row.status == AFT_COMPLETED:
                sent_ytd += amount
                if row.fee_status == FEE_COLLECTED:
                    fee_ytd += parse_money(row.fee, allow_zero=True)
            elif row.status == AFT_RECALLED:
                recalled += amount
        now = float(self.clock())
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'fee': money_str(self.policy.outbound_fee),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'clock': self.calendar.snapshot(now),
            'sources': [row.to_dict() for row in beneficiaries[:40]],
            'payments': [row.to_dict() for row in payments[:40]],
            'ytd_pulled': money_str(sent_ytd),
            'ytd_fees': money_str(fee_ytd),
            'recalled_ytd': money_str(recalled),
            'active_count': sum(1 for row in beneficiaries if row.status == SOURCE_ACTIVE),
            'open_count': sum(1 for row in payments if row.status in OPEN_AFTS),
        }


_SERVICE: Optional[AftService] = None


def set_service(service: Optional[AftService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[AftService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('AFT_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryAftStore()
    path = os.environ.get('AFT_DB', DEFAULT_STORE_PATH)
    return SqliteAftStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    issuer_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[CardNetworkClock] = None,
) -> AftService:
    if store is None:
        store = default_store()
    return AftService(
        AftPolicy.from_env(),
        store,
        clock=clock,
        debit_fn=debit_fn,
        credit_fn=credit_fn,
        issuer_fn=issuer_fn,
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
        'source_duplicate': 409,
        'source_limit': 409,
        'payment_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_recalled': 409,
        'nsf': 409,
        'failed': 409,
        'recall_failed': 409,
        'aft_forbidden': 403,
        'aft_disabled': 403,
        'source_paused': 403,
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
        'source_not_found': 404,
        'payment_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_name': 400,
        'invalid_pan': 400,
        'invalid_brand': 400,
        'invalid_expiry': 400,
        'invalid_mti': 400,
        'invalid_mcc': 400,
        'invalid_stan': 400,
        'invalid_rrn': 400,
        'invalid_purpose': 400,
        'invalid_status': 400,
        'invalid_file': 400,
        'amount_out_of_range': 400,
        'aft_amount_exceeded': 400,
        'invalid_processing_code': 400,
        'missing_customer_id': 400,
        'missing_source': 400,
        'missing_payment': 400,
    }.get(code, 400)


def _error_body(exc: AftError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('payment') is not None:
        body['payment'] = exc.extra['payment'].to_dict()
    if exc.extra.get('source') is not None:
        body['source'] = exc.extra['source'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except AftError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: AftService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'CardPull': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_add_source(service: AftService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.add_source(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname'),
            legal_name=values.get('legal_name') or values.get('name'),
            pan=values.get('pan') or values.get('card_number'),
            expiry=values.get('expiry') or values.get('exp'),
            default_account=values.get('default_account') or values.get('account'),
            brand=values.get('brand'),
        )
        return jsonify({
            'message': 'Card-pull source added',
            'source': row.to_dict(),
            'CardPull': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _source_status_route(service: AftService, status: str, message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    source_id = str(values.get('source_id') or values.get('beneficiary_id') or '').strip()
    if not source_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_source'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_source_status(
            source_id=source_id, actor=userid, actor_type=actor_type, status=status,
        )
        return jsonify({
            'message': message,
            'source': row.to_dict(),
            'CardPull': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: AftService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    source_id = str(values.get('source_id') or values.get('beneficiary_id') or '').strip()
    if not source_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_source'}), 400

    def _run():
        preview = service.preview(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            source_id=source_id,
            amount=values.get('amount'),
            internal_account=values.get('account') or values.get('internal_account') or values.get('to_account') or values.get('from_account'),
            waive_fee=bool(values.get('waive_fee')),
            mcc=values.get('mcc'),
        )
        return jsonify({'preview': preview, 'CardPull': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: AftService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    source_id = str(values.get('source_id') or values.get('beneficiary_id') or '').strip()
    if not source_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_source'}), 400

    def _run():
        payment, created = service.originate(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            source_id=source_id,
            amount=values.get('amount'),
            internal_account=values.get('account') or values.get('internal_account') or values.get('to_account') or values.get('from_account'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
            mcc=values.get('mcc'),
        )
        return jsonify({
            'message': 'Card pull originated' if created else 'Card pull already posted',
            'payment': payment.to_dict(),
            'CardPull': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: AftService):
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
            'message': 'Card pull cancelled',
            'payment': payment.to_dict(),
            'CardPull': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_recall(service: AftService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    payment_id = str(values.get('payment_id') or '').strip()
    if not payment_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_payment'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        payment = service.recall_payment(
            payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Card pull recalled',
            'payment': payment.to_dict(),
            'CardPull': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_payment_route(service: AftService, action: str):
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
            message = 'Card pull released'
        elif action == 'reject':
            payment = service.reject_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Card pull rejected'
        elif action == 'override':
            payment = service.override_ofac(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'waive':
            payment = service.waive_fee(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'Card-pull fee waived'
        elif action == 'export':
            exported = service.export_payment(payment_id=payment_id, actor=userid, actor_type=actor_type)
            return jsonify({
                'message': 'Card pull exported',
                'file': exported['file'],
                'payment': exported['payment'],
                'CardPull': service.snapshot(exported['payment']['userid'], actor=userid, actor_type=actor_type),
            }), 200
        else:
            raise AftError('invalid_status', 'Unknown card-pull action.')
        return jsonify({
            'message': message,
            'payment': payment.to_dict(),
            'CardPull': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: AftService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'CardPull': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_aft_routes(app, service: AftService) -> None:
    @app.route('/listCardPull', methods=['POST', 'GET'])
    def list_card_pull_route():
        return handle_list(service)

    @app.route('/listCardPullSources', methods=['POST', 'GET'])
    def list_card_pull_sources_route():
        return handle_list(service)

    @app.route('/addCardPullSource', methods=['POST', 'GET'])
    def add_card_pull_source_route():
        return handle_add_source(service)

    @app.route('/pauseCardPullSource', methods=['POST', 'GET'])
    def pause_card_pull_source_route():
        return _source_status_route(service, SOURCE_PAUSED, 'Card-pull source paused')

    @app.route('/resumeCardPullSource', methods=['POST', 'GET'])
    def resume_card_pull_source_route():
        return _source_status_route(service, SOURCE_ACTIVE, 'Card-pull source resumed')

    @app.route('/archiveCardPullSource', methods=['POST', 'GET'])
    def archive_card_pull_source_route():
        return _source_status_route(service, SOURCE_ARCHIVED, 'Card-pull source archived')

    @app.route('/previewCardPull', methods=['POST', 'GET'])
    def preview_card_pull_route():
        return handle_preview(service)

    @app.route('/sendCardPull', methods=['POST', 'GET'])
    def send_card_pull_route():
        return handle_send(service)

    @app.route('/cancelCardPull', methods=['POST', 'GET'])
    def cancel_card_pull_route():
        return handle_cancel(service)

    @app.route('/requestCardPullRecall', methods=['POST', 'GET'])
    def request_card_pull_recall_route():
        return handle_recall(service)

    @app.route('/releaseCardPull', methods=['POST', 'GET'])
    def release_card_pull_route():
        return _staff_payment_route(service, 'release')

    @app.route('/rejectCardPull', methods=['POST', 'GET'])
    def reject_card_pull_route():
        return _staff_payment_route(service, 'reject')

    @app.route('/recallCardPull', methods=['POST', 'GET'])
    def recall_card_pull_route():
        return handle_recall(service)

    @app.route('/overrideCardPullOfac', methods=['POST', 'GET'])
    def override_card_pull_ofac_route():
        return _staff_payment_route(service, 'override')

    @app.route('/waiveCardPullFee', methods=['POST', 'GET'])
    def waive_card_pull_fee_route():
        return _staff_payment_route(service, 'waive')

    @app.route('/exportCardPull', methods=['POST', 'GET'])
    def export_card_pull_route():
        return _staff_payment_route(service, 'export')

    @app.route('/runDueCardPull', methods=['POST', 'GET'])
    def run_due_card_pull_route():
        return handle_run_due(service)
