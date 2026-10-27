"""Push-to-card origination (Visa Direct / Mastercard Send OCT).

Customers send USD Original Credit Transactions to an external debit or
prepaid PAN. Independent of card 3DS / ACS (PR #153), card authorization
holds (PR #149), card settlement presentment (PR #151), chargeback
representment (PR #109), outbound Fedwire (PR #73), and ACH linking
(PR #68). Existing `/fundTransfer`, `/withdrawAmount`, and `/sendWire`
stay unchanged.

Foundations (reusable beyond this screen):
- PAN Luhn + IIN brand (not ABA / ARN / STAN-as-auth / CAVV)
- PAN fingerprint uniqueness (SHA-256, never in snapshots)
- ISO 8583 OCT MTI 0260 / reversal 0420 (not auth 1100 or presentment 1240)
- OCT RRN / STAN uniqueness
- 24/7 card-network clock (not Fedwire 17:00 ET cutoff)
- OCT1 pipe compose/parse
- Dual-control for high-value pushes
- 24h customer recall / 90-day staff recall of a completed OCT

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Full PAN, fingerprint, RRN, and STAN never appear in to_dict / snapshots.
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

BENE_ACTIVE = 'active'
BENE_PAUSED = 'paused'
BENE_ARCHIVED = 'archived'
BENE_STATUSES = frozenset({BENE_ACTIVE, BENE_PAUSED, BENE_ARCHIVED})
OPEN_BENE = frozenset({BENE_ACTIVE, BENE_PAUSED})
OCT_HELD = 'held'
OCT_PENDING = 'pending_release'
OCT_COMPLETED = 'completed'
OCT_REJECTED = 'rejected'
OCT_CANCELLED = 'cancelled'
OCT_RECALLED = 'recalled'
OCT_NSF = 'nsf'
OCT_FAILED = 'failed'
OCT_STATUSES = frozenset({
    OCT_HELD, OCT_PENDING, OCT_COMPLETED, OCT_REJECTED, OCT_CANCELLED,
    OCT_RECALLED, OCT_NSF, OCT_FAILED,
})
OPEN_OCTS = frozenset({OCT_HELD, OCT_PENDING})
CANCELABLE = frozenset({OCT_HELD, OCT_PENDING})
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
ALLOWED_MTI = frozenset({'0260', '0420'})
MTI_ALIASES = {
    '260': '0260', '0260': '0260', 'oct': '0260', 'credit': '0260',
    '420': '0420', '0420': '0420', 'reversal': '0420', 'reverse': '0420',
}
ALLOWED_MCC = frozenset({'4829', '6012', '6536', '6537', '6538'})
DEFAULT_MCC = '6538'
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/oct.sqlite'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)
CUSTOMER_RECALL_SECONDS = 24 * 60 * 60
STAFF_RECALL_SECONDS = 90 * 24 * 60 * 60


class OctError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def normalize_nickname(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise OctError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_legal_name(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 80):
        raise OctError('invalid_name', 'Beneficiary legal name must be 2-80 characters.')
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
        raise OctError('invalid_pan', 'PAN failed Luhn check or length.')
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
        raise OctError('invalid_brand', 'Only Visa Direct and Mastercard Send are supported.')
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
        raise OctError('invalid_brand', 'Only Visa and Mastercard debit/prepaid are supported.')
    if pan:
        detected = brand_from_iin(pan)
        if detected != text:
            raise OctError('invalid_brand', 'Brand does not match PAN IIN.')
    return text


def normalize_expiry(value: Any, *, now: Optional[float] = None) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) == 6:
        digits = digits[:2] + digits[4:6]
    if len(digits) != 4:
        raise OctError('invalid_expiry', 'Expiry must be MMYY.')
    month = int(digits[:2])
    year = int(digits[2:])
    if month < 1 or month > 12:
        raise OctError('invalid_expiry', 'Expiry month is invalid.')
    if now is not None:
        local = datetime.fromtimestamp(float(now), tz=timezone.utc)
        exp_year = 2000 + year if year < 100 else year
        if (exp_year, month) < (local.year, local.month):
            raise OctError('invalid_expiry', 'Card is expired.')
    return digits


def normalize_mti(value: Any, *, default: str = '0260') -> str:
    text = str(value or default).strip().lower()
    text = MTI_ALIASES.get(text, text)
    if text not in ALLOWED_MTI:
        raise OctError('invalid_mti', 'MTI must be 0260 (OCT) or 0420 (reversal).')
    return text


def normalize_mcc(value: Any, *, default: str = DEFAULT_MCC) -> str:
    digits = ''.join(ch for ch in str(value or default) if ch.isdigit())
    if len(digits) != 4 or digits not in ALLOWED_MCC:
        raise OctError('invalid_mcc', 'MCC must be a money-transfer code (6538/6536/6537/4829/6012).')
    return digits


def normalize_purpose(value: Any, *, default: str = 'other') -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = PURPOSE_ALIASES.get(text, text)
    if text not in PURPOSES:
        raise OctError('invalid_purpose', 'Unknown card-push purpose.')
    return text


def compose_stan(sequence: int) -> str:
    seq = int(sequence)
    if seq < 1 or seq > 999999:
        raise OctError('invalid_stan', 'STAN sequence out of range.')
    return '%06d' % seq


def normalize_stan(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 6:
        raise OctError('invalid_stan', 'STAN must be six digits.')
    return digits


def mask_stan(stan: str) -> str:
    digits = ''.join(ch for ch in str(stan or '') if ch.isdigit())
    if len(digits) < 2:
        return '**'
    return ('*' * (len(digits) - 2)) + digits[-2:]


def compose_rrn(cycle_date: str, sequence: int) -> str:
    """12-digit OCT RRN: YYMMDD + 6-digit sequence. Not a Fedwire IMAD or card ARN."""
    day = str(cycle_date or '').strip()
    if not (len(day) == 8 and day.isdigit()):
        raise OctError('invalid_rrn', 'Cycle date must be YYYYMMDD.')
    return day[2:] + compose_stan(sequence)


def normalize_rrn(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(digits) != 12:
        raise OctError('invalid_rrn', 'RRN must be twelve digits.')
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


def compose_oct1(
    *,
    mti: str,
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
        'OCT1',
        mti,
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


def parse_oct1(text: Any) -> Dict[str, str]:
    raw = str(text or '')
    if '<?xml' in raw.lower() or '<!doctype' in raw.lower() or '<' in raw:
        raise OctError('invalid_file', 'XML/DOCTYPE OCT payloads are rejected.')
    parts = [item.strip() for item in raw.strip().split('|')]
    if len(parts) < 11 or parts[0] != 'OCT1':
        raise OctError('invalid_file', 'OCT1 record is malformed.')
    return {
        'version': parts[0],
        'mti': normalize_mti(parts[1]),
        'network': parts[2],
        'pan_masked': parts[3],
        'brand': parts[4],
        'amount': parts[5],
        'currency': parts[6],
        'rrn_masked': parts[7],
        'stan_masked': parts[8],
        'mcc': normalize_mcc(parts[9]),
        'purpose': normalize_purpose(parts[10]),
    }


def split_oct1_file(text: Any) -> List[str]:
    raw = str(text or '')
    if '<?xml' in raw.lower() or '<!doctype' in raw.lower():
        raise OctError('invalid_file', 'XML/DOCTYPE OCT payloads are rejected.')
    lines = []
    for line in raw.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        stripped = line.strip()
        if stripped:
            lines.append(stripped)
    if not lines:
        raise OctError('invalid_file', 'OCT1 file is empty.')
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
class OctPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_send: bool = True
    allow_credit: bool = False
    max_beneficiaries: int = 12
    max_payments: int = 120
    min_amount: Decimal = Decimal('1.00')
    max_amount: Decimal = Decimal('10000.00')
    outbound_fee: Decimal = Decimal('1.50')
    dual_control_threshold: Decimal = Decimal('2500.00')
    customer_recall_seconds: int = CUSTOMER_RECALL_SECONDS
    staff_recall_seconds: int = STAFF_RECALL_SECONDS
    tz_offset_hours: int = 0
    default_mcc: str = DEFAULT_MCC
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST

    @classmethod
    def from_env(cls) -> 'OctPolicy':
        extra = _env_list('OCT_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('OCT_ENABLED', True),
            customer_manage=_env_bool('OCT_CUSTOMER_MANAGE', True),
            customer_send=_env_bool('OCT_CUSTOMER_SEND', True),
            allow_credit=_env_bool('OCT_ALLOW_CREDIT', False),
            max_beneficiaries=max(1, _env_int('OCT_MAX_BENEFICIARIES', 12)),
            max_payments=max(1, _env_int('OCT_MAX_PAYMENTS', 120)),
            min_amount=_env_money('OCT_MIN_AMOUNT', '1.00'),
            max_amount=_env_money('OCT_MAX_AMOUNT', '10000.00'),
            outbound_fee=_env_money('OCT_FEE', '1.50'),
            dual_control_threshold=_env_money('OCT_DUAL_CONTROL', '2500.00'),
            customer_recall_seconds=max(60, _env_int('OCT_CUSTOMER_RECALL_SECONDS', CUSTOMER_RECALL_SECONDS)),
            staff_recall_seconds=max(60, _env_int('OCT_STAFF_RECALL_SECONDS', STAFF_RECALL_SECONDS)),
            tz_offset_hours=_env_int('OCT_TZ_OFFSET', 0),
            default_mcc=normalize_mcc(os.environ.get('OCT_MCC', DEFAULT_MCC)),
            watchlist=watch,
        )


@dataclass
class OctBeneficiary:
    beneficiary_id: str
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
            'beneficiary_id': self.beneficiary_id,
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
            'active': self.status == BENE_ACTIVE,
            'paused': self.status == BENE_PAUSED,
            'archived': self.status == BENE_ARCHIVED,
        }


@dataclass
class OctPayment:
    payment_id: str
    trace_id: str
    beneficiary_id: str
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
            'beneficiary_id': self.beneficiary_id,
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
            'held': self.status == OCT_HELD,
            'pending_release': self.status == OCT_PENDING,
            'completed': self.status == OCT_COMPLETED,
            'cancelable': self.status in CANCELABLE,
            'recallable': self.status == OCT_COMPLETED,
        }


def _clone_bene(row: OctBeneficiary) -> OctBeneficiary:
    return OctBeneficiary(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_payment(row: OctPayment) -> OctPayment:
    return OctPayment(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _bene_from_row(row: Any) -> OctBeneficiary:
    return OctBeneficiary(
        beneficiary_id=row['beneficiary_id'],
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


def _payment_from_row(row: Any) -> OctPayment:
    return OctPayment(
        payment_id=row['payment_id'],
        trace_id=row['trace_id'],
        beneficiary_id=row['beneficiary_id'],
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


class MemoryOctStore:
    def __init__(self) -> None:
        self._benes: Dict[str, OctBeneficiary] = {}
        self._payments: Dict[str, OctPayment] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_beneficiary(self, row: OctBeneficiary) -> None:
        with self._lock:
            self._benes[row.beneficiary_id] = row

    def get_beneficiary(self, beneficiary_id: str) -> Optional[OctBeneficiary]:
        with self._lock:
            row = self._benes.get(beneficiary_id)
            return _clone_bene(row) if row else None

    def update_beneficiary(self, row: OctBeneficiary) -> None:
        with self._lock:
            self._benes[row.beneficiary_id] = row

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[OctBeneficiary]:
        with self._lock:
            rows = [_clone_bene(row) for row in self._benes.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != BENE_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[OctBeneficiary]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._benes.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_BENE:
                    return _clone_bene(row)
        return None

    def find_beneficiary_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[OctBeneficiary]:
        with self._lock:
            for row in self._benes.values():
                if row.userid == userid and row.fingerprint == fingerprint and row.status in OPEN_BENE:
                    return _clone_bene(row)
        return None

    def put_payment(self, row: OctPayment) -> OctPayment:
        with self._lock:
            existing_id = self._by_trace.get(row.trace_id)
            if existing_id is not None:
                return self._payments[existing_id]
            self._payments[row.payment_id] = row
            self._by_trace[row.trace_id] = row.payment_id
            return row

    def update_payment(self, row: OctPayment) -> None:
        with self._lock:
            self._payments[row.payment_id] = row

    def get_payment(self, payment_id: str) -> Optional[OctPayment]:
        with self._lock:
            row = self._payments.get(payment_id)
            return _clone_payment(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[OctPayment]:
        with self._lock:
            payment_id = self._by_trace.get(trace_id)
            return _clone_payment(self._payments[payment_id]) if payment_id else None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[OctPayment]:
        with self._lock:
            rows = [_clone_payment(row) for row in self._payments.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if beneficiary_id is not None:
            rows = [row for row in rows if row.beneficiary_id == beneficiary_id]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def next_stan_sequence(self, cycle_date: str) -> int:
        prefix = cycle_date[2:]
        with self._lock:
            used = [row.rrn for row in self._payments.values() if row.rrn.startswith(prefix)]
        return len(used) + 1


class SqliteOctStore:
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
                    beneficiary_id TEXT NOT NULL,
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

    def put_beneficiary(self, row: OctBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO beneficiaries (
                    beneficiary_id, userid, nickname, legal_name, pan, fingerprint,
                    brand, network, expiry, default_account, status, actor,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.beneficiary_id, row.userid, row.nickname, row.legal_name, row.pan,
                    row.fingerprint, row.brand, row.network, row.expiry, row.default_account,
                    row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_beneficiary(self, beneficiary_id: str) -> Optional[OctBeneficiary]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM beneficiaries WHERE beneficiary_id = ?', (beneficiary_id,),
            ).fetchone()
        return _bene_from_row(row) if row else None

    def update_beneficiary(self, row: OctBeneficiary) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE beneficiaries SET nickname=?, legal_name=?, pan=?, fingerprint=?,
                    brand=?, network=?, expiry=?, default_account=?, status=?,
                    actor=?, updated_at=?
                WHERE beneficiary_id=?
                """,
                (
                    row.nickname, row.legal_name, row.pan, row.fingerprint, row.brand,
                    row.network, row.expiry, row.default_account, row.status, row.actor,
                    row.updated_at, row.beneficiary_id,
                ),
            )
            conn.commit()

    def list_beneficiaries(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[OctBeneficiary]:
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

    def find_beneficiary_by_nickname(self, userid: str, nickname: str) -> Optional[OctBeneficiary]:
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

    def find_beneficiary_by_fingerprint(self, userid: str, fingerprint: str) -> Optional[OctBeneficiary]:
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

    def put_payment(self, row: OctPayment) -> OctPayment:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM payments WHERE trace_id = ?', (row.trace_id,)
            ).fetchone()
            if existing is not None:
                return _payment_from_row(existing)
            conn.execute(
                """
                INSERT INTO payments (
                    payment_id, trace_id, beneficiary_id, userid, internal_account,
                    amount, fee, fee_status, nickname, legal_name, pan_masked, brand,
                    network, last4, purpose, memo, status, mti, mcc, rrn, stan, actor,
                    releaser, ofac_hit, ofac_match, created_at, updated_at, completed_at,
                    recalled_at, note, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.payment_id, row.trace_id, row.beneficiary_id, row.userid,
                    row.internal_account, row.amount, row.fee, row.fee_status,
                    row.nickname, row.legal_name, row.pan_masked, row.brand, row.network,
                    row.last4, row.purpose, row.memo, row.status, row.mti, row.mcc,
                    row.rrn, row.stan, row.actor, row.releaser, row.ofac_hit,
                    row.ofac_match, row.created_at, row.updated_at, row.completed_at,
                    row.recalled_at, row.note, row.reason,
                ),
            )
            conn.commit()
            return row

    def update_payment(self, row: OctPayment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE payments SET fee=?, fee_status=?, status=?, mti=?, rrn=?, stan=?,
                    actor=?, releaser=?, ofac_hit=?, ofac_match=?, updated_at=?,
                    completed_at=?, recalled_at=?, note=?, reason=?
                WHERE payment_id=?
                """,
                (
                    row.fee, row.fee_status, row.status, row.mti, row.rrn, row.stan,
                    row.actor, row.releaser, row.ofac_hit, row.ofac_match, row.updated_at,
                    row.completed_at, row.recalled_at, row.note, row.reason, row.payment_id,
                ),
            )
            conn.commit()

    def get_payment(self, payment_id: str) -> Optional[OctPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE payment_id = ?', (payment_id,)).fetchone()
        return _payment_from_row(row) if row else None

    def get_payment_by_trace(self, trace_id: str) -> Optional[OctPayment]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM payments WHERE trace_id = ?', (trace_id,)).fetchone()
        return _payment_from_row(row) if row else None

    def list_payments(self, userid: Optional[str] = None, beneficiary_id: Optional[str] = None) -> List[OctPayment]:
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
        return [_payment_from_row(row) for row in rows]

    def next_stan_sequence(self, cycle_date: str) -> int:
        prefix = cycle_date[2:] + '%'
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM payments WHERE rrn LIKE ?",
                (prefix,),
            ).fetchone()
        return int(row['n'] if row else 0) + 1


class OctService:
    def __init__(
        self,
        policy: OctPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[CardNetworkClock] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or CardNetworkClock(tz_offset_hours=policy.tz_offset_hours)

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise OctError('oct_disabled', 'Card push is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise OctError('oct_forbidden', 'Customers cannot manage card-push beneficiaries.')

    def _require_send(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_send:
            raise OctError('oct_forbidden', 'Customers cannot originate card pushes.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise OctError('oct_forbidden', 'Staff only.')

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
            raise OctError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise OctError('credit_not_allowed', 'Credit accounts cannot originate card pushes.')

    def _assert_amount(self, dollars: Decimal) -> None:
        if dollars < self.policy.min_amount or dollars > self.policy.max_amount:
            raise OctError('amount_out_of_range', 'Amount is outside the allowed range.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def add_beneficiary(
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
    ) -> OctBeneficiary:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise OctError('oct_forbidden', 'Not allowed to add beneficiaries for this customer.')
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
        if self.store.find_beneficiary_by_nickname(owner_userid, name) is not None:
            raise OctError('beneficiary_duplicate', 'A beneficiary with that nickname already exists.')
        if self.store.find_beneficiary_by_fingerprint(owner_userid, fingerprint) is not None:
            raise OctError('beneficiary_duplicate', 'That card is already on file.')
        open_rows = [row for row in self.store.list_beneficiaries(owner_userid) if row.status in OPEN_BENE]
        if len(open_rows) >= self.policy.max_beneficiaries:
            raise OctError('beneficiary_limit', 'Card-push beneficiary limit reached.')
        row = OctBeneficiary(
            beneficiary_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            legal_name=legal,
            pan=card,
            fingerprint=fingerprint,
            brand=card_brand,
            network=network,
            expiry=exp,
            default_account=account,
            status=BENE_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_beneficiary(row)
        return row

    def get_beneficiary(self, *, beneficiary_id: str, actor: str, actor_type: str) -> OctBeneficiary:
        self._require_enabled()
        row = self.store.get_beneficiary(beneficiary_id)
        if row is None:
            raise OctError('beneficiary_not_found', 'Card-push beneficiary not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise OctError('oct_forbidden', 'Not allowed to view this beneficiary.')
        return row

    def enforce_beneficiary(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        require_active: bool = True,
    ) -> OctBeneficiary:
        row = self.get_beneficiary(beneficiary_id=beneficiary_id, actor=actor, actor_type=actor_type)
        if row.status == BENE_ARCHIVED:
            raise OctError('already_archived', 'Beneficiary is archived.')
        if row.status == BENE_PAUSED:
            raise OctError('beneficiary_paused', 'Beneficiary is paused.')
        if require_active and row.status != BENE_ACTIVE:
            raise OctError('invalid_status', 'Beneficiary is not active.')
        normalize_expiry(row.expiry, now=float(self.clock()))
        return row

    def set_beneficiary_status(
        self,
        *,
        beneficiary_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> OctBeneficiary:
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
            raise OctError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == BENE_ARCHIVED:
            raise OctError('already_archived', 'Beneficiary is already archived.')
        if wanted == BENE_PAUSED:
            if row.status == BENE_PAUSED:
                raise OctError('already_paused', 'Beneficiary is already paused.')
            if row.status != BENE_ACTIVE:
                raise OctError('invalid_status', 'Only an active beneficiary can be paused.')
        elif wanted == BENE_ACTIVE:
            if row.status == BENE_ACTIVE:
                raise OctError('already_active', 'Beneficiary is already active.')
            if row.status != BENE_PAUSED:
                raise OctError('invalid_status', 'Only a paused beneficiary can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_beneficiary(row)
        return row

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        beneficiary_id: Any,
        amount: Any,
        internal_account: Any = None,
        waive_fee: bool = False,
        mcc: Any = None,
    ) -> Dict[str, Any]:
        self._require_send(actor_type)
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise OctError('oct_forbidden', 'Beneficiary does not belong to this customer.')
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
            'total': money_str(dollars + fee),
            'internal_account': account,
            'mcc': normalize_mcc(mcc or self.policy.default_mcc),
            'mti': '0260',
            'network': bene.network,
            'brand': bene.brand,
            'beneficiary': bene.to_dict(),
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(dollars),
            'clock': self.calendar.snapshot(now),
        }

    def _place(
        self,
        *,
        owner_userid: str,
        actor: str,
        bene: OctBeneficiary,
        account: str,
        dollars: Decimal,
        fee: Decimal,
        purpose: str,
        memo: str,
        trace_id: str,
        ofac: ScreenResult,
        waive_fee: bool,
        mcc: str,
    ) -> OctPayment:
        now = float(self.clock())
        fee_status = FEE_WAIVED if waive_fee or fee == 0 else FEE_NONE
        if ofac.hit:
            status = OCT_HELD
        elif self._needs_dual_control(dollars):
            status = OCT_PENDING
        else:
            status = OCT_COMPLETED
        payment = OctPayment(
            payment_id=uuid.uuid4().hex,
            trace_id=trace_id,
            beneficiary_id=bene.beneficiary_id,
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
            mti='0260',
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
        if status == OCT_COMPLETED:
            self._transmit(payment, actor=actor)
        return payment

    def _transmit(self, payment: OctPayment, *, actor: str) -> OctPayment:
        now = float(self.clock())
        cycle = self.calendar.cycle_date(now)
        seq = self.store.next_stan_sequence(cycle)
        payment.stan = compose_stan(seq)
        payment.rrn = compose_rrn(cycle, seq)
        payment.mti = '0260'
        dollars = parse_money(payment.amount)
        fee = parse_money(payment.fee, allow_zero=True)
        remark = 'card push %s' % payment.nickname
        status = OCT_COMPLETED
        fail_note = ''
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(payment.internal_account, money_str(dollars), remark)
            except Exception as exc:
                status = OCT_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = OCT_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = OCT_FAILED
                    fail_note = str(result)[:240]
        if status == OCT_COMPLETED and fee > 0 and payment.fee_status != FEE_WAIVED and self.debit_fn is not None:
            try:
                fee_result = self.debit_fn(
                    payment.internal_account, money_str(fee), 'card push fee %s' % mask_rrn(payment.rrn),
                )
            except Exception:
                payment.fee_status = FEE_NSF
            else:
                kind = _classify_money_result(fee_result)
                payment.fee_status = FEE_COLLECTED if kind == 'ok' else FEE_NSF
        elif status == OCT_COMPLETED and (fee == 0 or payment.fee_status == FEE_WAIVED):
            payment.fee_status = FEE_WAIVED if payment.fee_status == FEE_WAIVED or fee == 0 else payment.fee_status
        payment.status = status
        payment.updated_at = now
        if status == OCT_COMPLETED:
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
        beneficiary_id: Any,
        amount: Any,
        internal_account: Any = None,
        purpose: Any = 'other',
        memo: Any = '',
        trace_id: Any = None,
        waive_fee: bool = False,
        mcc: Any = None,
    ) -> Tuple[OctPayment, bool]:
        self._require_send(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise OctError('oct_forbidden', 'Not allowed to originate card pushes for this customer.')
        bene = self.enforce_beneficiary(
            beneficiary_id=str(beneficiary_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if bene.userid != owner_userid:
            raise OctError('oct_forbidden', 'Beneficiary does not belong to this customer.')
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
            raise OctError('payment_limit', 'Card-push history limit reached.')
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
        if stored.status == OCT_NSF:
            raise OctError('nsf', 'Insufficient funds for card push.', payment=stored)
        if stored.status == OCT_FAILED:
            raise OctError('failed', 'Card-push debit did not complete.', payment=stored)
        return stored, True

    def get_payment(self, *, payment_id: str, actor: str, actor_type: str) -> OctPayment:
        self._require_enabled()
        row = self.store.get_payment(payment_id)
        if row is None:
            raise OctError('payment_not_found', 'Card-push payment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise OctError('oct_forbidden', 'Not allowed to view this payment.')
        return row

    def cancel_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> OctPayment:
        self._require_send(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and payment.userid != actor:
            raise OctError('oct_forbidden', 'Not allowed to cancel this payment.')
        if payment.status not in CANCELABLE:
            raise OctError('not_cancelable', 'Only held or pending card pushes can be cancelled.')
        payment.status = OCT_CANCELLED
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
    ) -> OctPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status not in CANCELABLE:
            raise OctError('invalid_status', 'Fee can only be waived before the card push is sent.')
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
    ) -> OctPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status != OCT_HELD:
            raise OctError('not_overridable', 'Only an OFAC hold can be overridden.')
        now = float(self.clock())
        payment.ofac_hit = 0
        payment.note = normalize_note(note) or 'ofac override'
        payment.actor = str(actor)
        payment.updated_at = now
        dollars = parse_money(payment.amount)
        if self._needs_dual_control(dollars):
            payment.status = OCT_PENDING
        else:
            self._transmit(payment, actor=actor)
        self.store.update_payment(payment)
        if payment.status == OCT_NSF:
            raise OctError('nsf', 'Insufficient funds for card push.', payment=payment)
        if payment.status == OCT_FAILED:
            raise OctError('failed', 'Card-push debit did not complete.', payment=payment)
        return payment

    def release_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
    ) -> OctPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == OCT_HELD:
            raise OctError('ofac_hold', 'OFAC hold must be overridden before release.')
        if payment.status != OCT_PENDING:
            raise OctError('not_releasable', 'Only pending card pushes can be released.')
        if (
            payment.actor
            and str(actor) == str(payment.actor)
            and parse_money(payment.amount) >= self.policy.dual_control_threshold
        ):
            raise OctError('same_approver', 'A different employee must release this card push.')
        self._transmit(payment, actor=actor)
        self.store.update_payment(payment)
        if payment.status == OCT_NSF:
            raise OctError('nsf', 'Insufficient funds for card push.', payment=payment)
        if payment.status == OCT_FAILED:
            raise OctError('failed', 'Card-push debit did not complete.', payment=payment)
        return payment

    def reject_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> OctPayment:
        self._require_staff(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status not in CANCELABLE:
            raise OctError('not_rejectable', 'Only held or pending card pushes can be rejected.')
        payment.status = OCT_REJECTED
        payment.reason = normalize_note(reason, limit=40)
        payment.note = normalize_note(note)
        payment.actor = str(actor)
        payment.updated_at = float(self.clock())
        self.store.update_payment(payment)
        return payment

    def _assert_recall_window(self, payment: OctPayment, *, actor_type: str) -> None:
        if payment.status != OCT_COMPLETED:
            raise OctError('not_recallable', 'Only completed card pushes can be recalled.')
        now = float(self.clock())
        started = payment.completed_at or payment.created_at
        limit = self.policy.staff_recall_seconds if actor_type in EMPLOYEE_ROLES else self.policy.customer_recall_seconds
        if now - started > limit:
            raise OctError('return_window_closed', 'Card-push recall window has closed.')

    def recall_payment(
        self,
        *,
        payment_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> OctPayment:
        if actor_type in EMPLOYEE_ROLES:
            self._require_staff(actor_type)
        else:
            self._require_send(actor_type)
        payment = self.get_payment(payment_id=payment_id, actor=actor, actor_type=actor_type)
        if payment.status == OCT_RECALLED:
            raise OctError('already_recalled', 'Card push is already recalled.')
        if actor_type not in EMPLOYEE_ROLES and payment.userid != actor:
            raise OctError('oct_forbidden', 'Not allowed to recall this payment.')
        self._assert_recall_window(payment, actor_type=actor_type)
        remark = normalize_note(note) or ('card push recalled from %s' % payment.nickname)
        if self.credit_fn is not None:
            try:
                result = self.credit_fn(payment.internal_account, payment.amount, remark)
            except Exception as exc:
                raise OctError('recall_failed', 'Recall credit failed.', payment=payment) from exc
            if _classify_money_result(result) != 'ok':
                raise OctError('recall_failed', 'Recall credit failed.', payment=payment)
            fee = parse_money(payment.fee, allow_zero=True)
            if fee > 0 and payment.fee_status == FEE_COLLECTED:
                self.credit_fn(
                    payment.internal_account, money_str(fee),
                    'card push fee recalled %s' % mask_rrn(payment.rrn),
                )
        now = float(self.clock())
        payment.status = OCT_RECALLED
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
        if payment.status not in {OCT_COMPLETED, OCT_RECALLED}:
            raise OctError('not_exportable', 'Only completed or recalled card pushes can be exported.')
        record = compose_oct1(
            mti=payment.mti,
            network=payment.network,
            pan_masked=payment.pan_masked,
            brand=payment.brand,
            amount=payment.amount,
            rrn_masked=mask_rrn(payment.rrn),
            stan_masked=mask_stan(payment.stan),
            mcc=payment.mcc,
            purpose=payment.purpose,
        )
        parsed = parse_oct1(record)
        if 'fingerprint' in parsed or payment.pan_masked.find('*') < 0:
            raise OctError('not_exportable', 'Export must not include a full PAN.')
        return {'file': record, 'payment': payment.to_dict()}

    def run_due(self, userid: Optional[str] = None) -> List[OctPayment]:
        """24/7 rail: nothing queues on a Fed cutoff. Kept for API symmetry."""
        _ = userid
        return []

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.run_due(userid)
        beneficiaries = self.store.list_beneficiaries(userid)
        payments = self.store.list_payments(userid)
        sent_ytd = Decimal('0.00')
        fee_ytd = Decimal('0.00')
        recalled = Decimal('0.00')
        for row in payments:
            amount = parse_money(row.amount, allow_zero=True)
            if row.status == OCT_COMPLETED:
                sent_ytd += amount
                if row.fee_status == FEE_COLLECTED:
                    fee_ytd += parse_money(row.fee, allow_zero=True)
            elif row.status == OCT_RECALLED:
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
            'beneficiaries': [row.to_dict() for row in beneficiaries[:40]],
            'payments': [row.to_dict() for row in payments[:40]],
            'ytd_sent': money_str(sent_ytd),
            'ytd_fees': money_str(fee_ytd),
            'recalled_ytd': money_str(recalled),
            'active_count': sum(1 for row in beneficiaries if row.status == BENE_ACTIVE),
            'open_count': sum(1 for row in payments if row.status in OPEN_OCTS),
        }


_SERVICE: Optional[OctService] = None


def set_service(service: Optional[OctService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[OctService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('OCT_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryOctStore()
    path = os.environ.get('OCT_DB', DEFAULT_STORE_PATH)
    return SqliteOctStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[CardNetworkClock] = None,
) -> OctService:
    if store is None:
        store = default_store()
    return OctService(
        OctPolicy.from_env(),
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
        'beneficiary_duplicate': 409,
        'beneficiary_limit': 409,
        'payment_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_recalled': 409,
        'nsf': 409,
        'failed': 409,
        'recall_failed': 409,
        'oct_forbidden': 403,
        'oct_disabled': 403,
        'beneficiary_paused': 403,
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
        'beneficiary_not_found': 404,
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
        'missing_customer_id': 400,
        'missing_beneficiary': 400,
        'missing_payment': 400,
    }.get(code, 400)


def _error_body(exc: OctError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('payment') is not None:
        body['payment'] = exc.extra['payment'].to_dict()
    if exc.extra.get('beneficiary') is not None:
        body['beneficiary'] = exc.extra['beneficiary'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except OctError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: OctService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'CardPush': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_add_beneficiary(service: OctService):
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
            pan=values.get('pan') or values.get('card_number'),
            expiry=values.get('expiry') or values.get('exp'),
            default_account=values.get('default_account') or values.get('account'),
            brand=values.get('brand'),
        )
        return jsonify({
            'message': 'Card-push beneficiary added',
            'beneficiary': row.to_dict(),
            'CardPush': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _bene_status_route(service: OctService, status: str, message: str):
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
            'message': message,
            'beneficiary': row.to_dict(),
            'CardPush': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: OctService):
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
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            waive_fee=bool(values.get('waive_fee')),
            mcc=values.get('mcc'),
        )
        return jsonify({'preview': preview, 'CardPush': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_send(service: OctService):
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
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            purpose=values.get('purpose') or 'other',
            memo=values.get('memo') or values.get('note') or '',
            trace_id=values.get('trace_id'),
            waive_fee=bool(values.get('waive_fee')),
            mcc=values.get('mcc'),
        )
        return jsonify({
            'message': 'Card push originated' if created else 'Card push already posted',
            'payment': payment.to_dict(),
            'CardPush': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_cancel(service: OctService):
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
            'message': 'Card push cancelled',
            'payment': payment.to_dict(),
            'CardPush': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_recall(service: OctService):
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
            'message': 'Card push recalled',
            'payment': payment.to_dict(),
            'CardPush': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_payment_route(service: OctService, action: str):
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
            message = 'Card push released'
        elif action == 'reject':
            payment = service.reject_payment(
                payment_id=payment_id, actor=userid, actor_type=actor_type,
                reason=values.get('reason') or 'other', note=values.get('note') or '',
            )
            message = 'Card push rejected'
        elif action == 'override':
            payment = service.override_ofac(
                payment_id=payment_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'waive':
            payment = service.waive_fee(payment_id=payment_id, actor=userid, actor_type=actor_type)
            message = 'Card-push fee waived'
        elif action == 'export':
            exported = service.export_payment(payment_id=payment_id, actor=userid, actor_type=actor_type)
            return jsonify({
                'message': 'Card push exported',
                'file': exported['file'],
                'payment': exported['payment'],
                'CardPush': service.snapshot(exported['payment']['userid'], actor=userid, actor_type=actor_type),
            }), 200
        else:
            raise OctError('invalid_status', 'Unknown card-push action.')
        return jsonify({
            'message': message,
            'payment': payment.to_dict(),
            'CardPush': service.snapshot(payment.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: OctService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'CardPush': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_oct_routes(app, service: OctService) -> None:
    @app.route('/listCardPush', methods=['POST', 'GET'])
    def list_card_push_route():
        return handle_list(service)

    @app.route('/listCardPushBeneficiaries', methods=['POST', 'GET'])
    def list_card_push_beneficiaries_route():
        return handle_list(service)

    @app.route('/addCardPushBeneficiary', methods=['POST', 'GET'])
    def add_card_push_beneficiary_route():
        return handle_add_beneficiary(service)

    @app.route('/pauseCardPushBeneficiary', methods=['POST', 'GET'])
    def pause_card_push_beneficiary_route():
        return _bene_status_route(service, BENE_PAUSED, 'Card-push beneficiary paused')

    @app.route('/resumeCardPushBeneficiary', methods=['POST', 'GET'])
    def resume_card_push_beneficiary_route():
        return _bene_status_route(service, BENE_ACTIVE, 'Card-push beneficiary resumed')

    @app.route('/archiveCardPushBeneficiary', methods=['POST', 'GET'])
    def archive_card_push_beneficiary_route():
        return _bene_status_route(service, BENE_ARCHIVED, 'Card-push beneficiary archived')

    @app.route('/previewCardPush', methods=['POST', 'GET'])
    def preview_card_push_route():
        return handle_preview(service)

    @app.route('/sendCardPush', methods=['POST', 'GET'])
    def send_card_push_route():
        return handle_send(service)

    @app.route('/cancelCardPush', methods=['POST', 'GET'])
    def cancel_card_push_route():
        return handle_cancel(service)

    @app.route('/requestCardPushRecall', methods=['POST', 'GET'])
    def request_card_push_recall_route():
        return handle_recall(service)

    @app.route('/releaseCardPush', methods=['POST', 'GET'])
    def release_card_push_route():
        return _staff_payment_route(service, 'release')

    @app.route('/rejectCardPush', methods=['POST', 'GET'])
    def reject_card_push_route():
        return _staff_payment_route(service, 'reject')

    @app.route('/recallCardPush', methods=['POST', 'GET'])
    def recall_card_push_route():
        return handle_recall(service)

    @app.route('/overrideCardPushOfac', methods=['POST', 'GET'])
    def override_card_push_ofac_route():
        return _staff_payment_route(service, 'override')

    @app.route('/waiveCardPushFee', methods=['POST', 'GET'])
    def waive_card_push_fee_route():
        return _staff_payment_route(service, 'waive')

    @app.route('/exportCardPush', methods=['POST', 'GET'])
    def export_card_push_route():
        return _staff_payment_route(service, 'export')

    @app.route('/runDueCardPush', methods=['POST', 'GET'])
    def run_due_card_push_route():
        return handle_run_due(service)
