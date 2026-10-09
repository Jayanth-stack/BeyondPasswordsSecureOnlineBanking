"""Positive Pay issued-check matching and exception decisioning.

Commercial customers upload the checks they issued. Staff ingest presentments.
Exact serial+amount(+payee) matches auto-pay; mismatches become exceptions the
customer or staff must pay or return before the Check21 14:00 ET cutoff.
Independent of inbound Check21 ICL (PR #115), outbound Check21 OCL (PR #117),
in-app cashier cheque deposit, and stop-payment (PR #34). Existing
`/fundTransfer`, `/withdrawAmount`, `/sendWire`, and `/depositCheck` stay
unchanged.

Foundations (reusable beyond this screen):
- 10-digit cents amount field
- Check serial / MICR on-us
- Payee-name fingerprint
- ISSUE1 / PPAY1 / DECN1 pipe compose and parse (XML/DOCTYPE rejected)
- Exception reason codes (not issued, amount/payee mismatch, void, stale,
  postdated, duplicate paid, reverse-pay review)
- Check21 decision cutoff via `utility.wire.WireCalendar`
- Default-return vs default-pay account policy
- Reverse positive pay (every presentment is an exception)
- OFAC screening and dual-control on high-value pays

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Drawer account numbers never appear in to_dict / snapshots.
"""

from __future__ import annotations

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
    DEFAULT_WATCHLIST,
    EMPLOYEE_ROLES,
    AccountError,
    AmountError,
    ScreenResult,
    WireCalendar,
    account_types_from_customer_payload,
    last4,
    money_str,
    normalize_account,
    normalize_legal_name,
    normalize_party,
    own_accounts_from_customer_payload,
    parse_money,
    screen_name,
)

ENROLL_ACTIVE = 'active'
ENROLL_PAUSED = 'paused'
ENROLL_ARCHIVED = 'archived'
ENROLL_STATUSES = frozenset({ENROLL_ACTIVE, ENROLL_PAUSED, ENROLL_ARCHIVED})
OPEN_ENROLL = frozenset({ENROLL_ACTIVE, ENROLL_PAUSED})

ISSUE_ISSUED = 'issued'
ISSUE_VOIDED = 'voided'
ISSUE_PAID = 'paid'
ISSUE_STATUSES = frozenset({ISSUE_ISSUED, ISSUE_VOIDED, ISSUE_PAID})

ITEM_UNMATCHED = 'unmatched'
ITEM_EXCEPTION = 'exception'
ITEM_HELD = 'held'
ITEM_PENDING = 'pending_release'
ITEM_PAID = 'paid'
ITEM_RETURNED = 'returned'
ITEM_REJECTED = 'rejected'
ITEM_NSF = 'nsf'
ITEM_FAILED = 'failed'
ITEM_STATUSES = frozenset({
    ITEM_UNMATCHED, ITEM_EXCEPTION, ITEM_HELD, ITEM_PENDING, ITEM_PAID,
    ITEM_RETURNED, ITEM_REJECTED, ITEM_NSF, ITEM_FAILED,
})
OPEN_ITEMS = frozenset({ITEM_UNMATCHED, ITEM_EXCEPTION, ITEM_HELD, ITEM_PENDING})
DECIDABLE = frozenset({ITEM_EXCEPTION})
DEFAULTABLE = frozenset({ITEM_EXCEPTION})

DEFAULT_RETURN = 'return'
DEFAULT_PAY = 'pay'
DEFAULT_ACTIONS = frozenset({DEFAULT_RETURN, DEFAULT_PAY})

REASON_NOT_ISSUED = 'not_issued'
REASON_AMOUNT = 'amount_mismatch'
REASON_PAYEE = 'payee_mismatch'
REASON_DUPLICATE = 'duplicate_paid'
REASON_VOIDED = 'voided'
REASON_STALE = 'stale'
REASON_POSTDATED = 'postdated'
REASON_REVIEW = 'review'
REASON_OFAC = 'ofac'
REASON_NSF = 'nsf'
REASON_OTHER = 'other'
REASONS = frozenset({
    REASON_NOT_ISSUED, REASON_AMOUNT, REASON_PAYEE, REASON_DUPLICATE, REASON_VOIDED,
    REASON_STALE, REASON_POSTDATED, REASON_REVIEW, REASON_OFAC, REASON_NSF, REASON_OTHER,
})
REASON_ALIASES = {
    'a': REASON_NOT_ISSUED, 'no_issue': REASON_NOT_ISSUED, 'missing': REASON_NOT_ISSUED,
    'b': REASON_AMOUNT, 'amt': REASON_AMOUNT, 'amount': REASON_AMOUNT,
    'c': REASON_PAYEE, 'payee': REASON_PAYEE, 'name': REASON_PAYEE,
    'd': REASON_DUPLICATE, 'dup': REASON_DUPLICATE, 'duplicate': REASON_DUPLICATE,
    'e': REASON_VOIDED, 'void': REASON_VOIDED, 'cancelled': REASON_VOIDED,
    'f': REASON_STALE, 'stale_dated': REASON_STALE,
    'g': REASON_POSTDATED, 'post_dated': REASON_POSTDATED, 'post-date': REASON_POSTDATED,
    'r': REASON_REVIEW, 'reverse': REASON_REVIEW,
    'o': REASON_OFAC, 'sanction': REASON_OFAC,
    'n': REASON_NSF, 'insufficient': REASON_NSF,
    'u': REASON_OTHER, 'other': REASON_OTHER,
}
REASON_CODES = {
    REASON_NOT_ISSUED: 'A',
    REASON_AMOUNT: 'B',
    REASON_PAYEE: 'C',
    REASON_DUPLICATE: 'D',
    REASON_VOIDED: 'E',
    REASON_STALE: 'F',
    REASON_POSTDATED: 'G',
    REASON_REVIEW: 'R',
    REASON_OFAC: 'O',
    REASON_NSF: 'N',
    REASON_OTHER: 'U',
}

MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/pospay.sqlite'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)
ISSUE_MARK = 'ISSUE1'
PRESENT_MARK = 'PPAY1'
DECISION_MARK = 'DECN1'


class PosPayError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


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


def _reject_xml(text: str) -> None:
    low = text.lstrip().lower()
    if low.startswith('<?xml') or low.startswith('<!doctype') or low.startswith('<'):
        raise PosPayError('invalid_file', 'XML/DOCTYPE files are rejected.')


def compose_amount_field(amount: Decimal) -> str:
    cents = int((amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN) * 100).to_integral_value())
    if cents < 0 or cents > 9999999999:
        raise PosPayError('invalid_amount', 'Amount is outside the 10-digit cents field.')
    return '%010d' % cents


def parse_amount_field(value: Any) -> Decimal:
    text = str(value or '').strip()
    if not text.isdigit() or not (1 <= len(text) <= 10):
        raise PosPayError('invalid_amount', 'Amount field must be 1-10 digits of cents.')
    return (Decimal(int(text)) / Decimal('100')).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def normalize_serial(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise PosPayError('invalid_serial', 'Check serial is required.')
        return ''
    if len(digits) > 10:
        raise PosPayError('invalid_serial', 'Check serial must be 1-10 digits.')
    return str(int(digits))


def mask_serial(value: Any) -> str:
    serial = str(value or '')
    if len(serial) <= 4:
        return serial
    return ('*' * (len(serial) - 4)) + serial[-4:]


def parse_onus(value: Any) -> Tuple[str, str]:
    """Split a MICR on-us field into (account, serial). `account/serial` or `account serial`."""
    text = str(value or '').strip()
    if not text:
        raise PosPayError('invalid_account', 'On-us field is required.')
    for sep in ('/', ' ', '-', ':'):
        if sep in text:
            left, right = text.split(sep, 1)
            return normalize_account(left), normalize_serial(right)
    digits = ''.join(ch for ch in text if ch.isdigit())
    if len(digits) < 5:
        raise PosPayError('invalid_account', 'On-us field is too short.')
    return normalize_account(digits[:-4]), normalize_serial(digits[-4:])


def normalize_payee(value: Any) -> str:
    try:
        return normalize_legal_name(value)
    except Exception:
        raise PosPayError('invalid_name', 'Payee name must be 2-80 characters.') from None


def payee_fingerprint(value: Any) -> str:
    return normalize_party(value)


def payees_match(issued: Any, presented: Any) -> bool:
    left = payee_fingerprint(issued)
    right = payee_fingerprint(presented)
    return bool(left) and left == right


def normalize_issue_date(value: Any) -> str:
    text = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(text) != 8:
        raise PosPayError('invalid_date', 'Issue date must be YYYYMMDD.')
    try:
        datetime.strptime(text, '%Y%m%d')
    except ValueError:
        raise PosPayError('invalid_date', 'Issue date must be a real calendar day.') from None
    return text


def date_from_yyyymmdd(value: str):
    return datetime.strptime(value, '%Y%m%d').date()


def normalize_reason(value: Any, *, default: str = REASON_OTHER) -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    text = REASON_ALIASES.get(text, text)
    if text not in REASONS:
        raise PosPayError('invalid_reason', 'Unknown positive-pay reason.')
    return text


def normalize_default_action(value: Any, *, default: str = DEFAULT_RETURN) -> str:
    text = str(value or default).strip().lower()
    aliases = {'return': DEFAULT_RETURN, 'pay': DEFAULT_PAY, 'default_return': DEFAULT_RETURN, 'default_pay': DEFAULT_PAY}
    text = aliases.get(text, text)
    if text not in DEFAULT_ACTIONS:
        raise PosPayError('invalid_status', 'Default action must be pay or return.')
    return text


def normalize_decision(value: Any) -> str:
    text = str(value or '').strip().lower()
    if text in {'pay', 'paid', 'accept', 'honor'}:
        return DEFAULT_PAY
    if text in {'return', 'returned', 'reject', 'dishonor'}:
        return DEFAULT_RETURN
    raise PosPayError('invalid_status', 'Decision must be pay or return.')


def normalize_id(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return uuid.uuid4().hex
    return text[:120]


def _split_record(line: str) -> List[str]:
    return [part.strip() for part in line.split('|')]


def split_pospay_file(text: Any) -> List[str]:
    raw = str(text or '')
    _reject_xml(raw)
    lines = []
    for line in raw.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        stripped = line.strip()
        if stripped:
            lines.append(stripped)
    if not lines:
        raise PosPayError('invalid_file', 'File has no records.')
    return lines


def parse_issue_record(text: Any) -> Dict[str, str]:
    raw = str(text or '').strip()
    _reject_xml(raw)
    parts = _split_record(raw)
    if not parts or parts[0] != ISSUE_MARK:
        raise PosPayError('invalid_file', 'Issue records must start with ISSUE1.')
    if len(parts) < 6:
        raise PosPayError('invalid_file', 'Issue record is missing fields.')
    amount = parse_amount_field(parts[2])
    return {
        'kind': ISSUE_MARK,
        'serial': normalize_serial(parts[1]),
        'amount': money_str(amount),
        'amount_field': compose_amount_field(amount),
        'account': normalize_account(parts[3]),
        'payee': normalize_payee(parts[4]),
        'issue_date': normalize_issue_date(parts[5]),
        'void': '1' if len(parts) > 6 and str(parts[6]).strip().lower() in {'1', 'void', 'true', 'yes'} else '0',
    }


def parse_presentment_record(text: Any) -> Dict[str, str]:
    raw = str(text or '').strip()
    _reject_xml(raw)
    parts = _split_record(raw)
    if not parts or parts[0] != PRESENT_MARK:
        raise PosPayError('invalid_file', 'Presentment records must start with PPAY1.')
    if len(parts) < 6:
        raise PosPayError('invalid_file', 'Presentment record is missing fields.')
    amount = parse_amount_field(parts[2])
    present_date = normalize_issue_date(parts[6]) if len(parts) > 6 and parts[6].strip() else ''
    return {
        'kind': PRESENT_MARK,
        'serial': normalize_serial(parts[1]),
        'amount': money_str(amount),
        'amount_field': compose_amount_field(amount),
        'account': normalize_account(parts[3]),
        'payee': normalize_payee(parts[4]),
        'presentment_id': str(parts[5] or '').strip() or uuid.uuid4().hex,
        'present_date': present_date,
    }


def compose_issue_record(
    *,
    serial: str,
    amount: Decimal,
    account: str,
    payee: str,
    issue_date: str,
    voided: bool = False,
) -> str:
    parts = [
        ISSUE_MARK,
        normalize_serial(serial),
        compose_amount_field(amount),
        normalize_account(account),
        normalize_payee(payee),
        normalize_issue_date(issue_date),
    ]
    if voided:
        parts.append('void')
    return '|'.join(parts)


def compose_presentment_record(
    *,
    serial: str,
    amount: Decimal,
    account: str,
    payee: str,
    presentment_id: str,
    present_date: str = '',
) -> str:
    parts = [
        PRESENT_MARK,
        normalize_serial(serial),
        compose_amount_field(amount),
        normalize_account(account),
        normalize_payee(payee),
        str(presentment_id),
    ]
    if present_date:
        parts.append(normalize_issue_date(present_date))
    return '|'.join(parts)


def compose_decision_record(
    *,
    serial: str,
    amount: Decimal,
    account: str,
    payee: str,
    decision: str,
    reason: str,
    presentment_id: str,
) -> str:
    return '|'.join([
        DECISION_MARK,
        normalize_serial(serial),
        compose_amount_field(amount),
        last4(account) if len(str(account)) > 4 else str(account),
        normalize_payee(payee),
        normalize_decision(decision) if decision in {DEFAULT_PAY, DEFAULT_RETURN, 'pay', 'return', 'paid', 'returned'} else str(decision or 'return'),
        REASON_CODES.get(normalize_reason(reason, default=REASON_OTHER), 'U'),
        str(presentment_id),
    ])


def message_from_issue(text: Any) -> Dict[str, Any]:
    parsed = parse_issue_record(text)
    parsed['payee_fingerprint'] = payee_fingerprint(parsed['payee'])
    parsed['account_last4'] = last4(parsed['account'])
    return parsed


def message_from_presentment(text: Any) -> Dict[str, Any]:
    parsed = parse_presentment_record(text)
    parsed['payee_fingerprint'] = payee_fingerprint(parsed['payee'])
    parsed['account_last4'] = last4(parsed['account'])
    return parsed


@dataclass
class PosPayPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_decide: bool = True
    allow_credit: bool = False
    match_payee_default: bool = True
    reverse_default: bool = False
    default_action: str = DEFAULT_RETURN
    max_enrollments: int = 4
    max_issues: int = 500
    max_items: int = 500
    min_amount: Decimal = Decimal('0.01')
    max_amount: Decimal = Decimal('1000000.00')
    dual_control_threshold: Decimal = Decimal('10000.00')
    stale_days: int = 180
    cutoff_hour: int = 14
    tz_offset_hours: int = -4
    watchlist: Tuple[str, ...] = DEFAULT_WATCHLIST
    extra_holidays: Tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> 'PosPayPolicy':
        extra = _env_list('POSPAY_OFAC_LIST')
        watch = tuple(dict.fromkeys(DEFAULT_WATCHLIST + extra))
        return cls(
            enabled=_env_bool('POSPAY_ENABLED', True),
            customer_manage=_env_bool('POSPAY_CUSTOMER_MANAGE', True),
            customer_decide=_env_bool('POSPAY_CUSTOMER_DECIDE', True),
            allow_credit=_env_bool('POSPAY_ALLOW_CREDIT', False),
            match_payee_default=_env_bool('POSPAY_MATCH_PAYEE', True),
            reverse_default=_env_bool('POSPAY_REVERSE', False),
            default_action=normalize_default_action(os.environ.get('POSPAY_DEFAULT_ACTION', DEFAULT_RETURN)),
            max_enrollments=_env_int('POSPAY_MAX_ENROLLMENTS', 4),
            max_issues=_env_int('POSPAY_MAX_ISSUES', 500),
            max_items=_env_int('POSPAY_MAX_ITEMS', 500),
            min_amount=_env_money('POSPAY_MIN_AMOUNT', '0.01'),
            max_amount=_env_money('POSPAY_MAX_AMOUNT', '1000000.00'),
            dual_control_threshold=_env_money('POSPAY_DUAL_CONTROL', '10000.00'),
            stale_days=_env_int('POSPAY_STALE_DAYS', 180),
            cutoff_hour=_env_int('POSPAY_CUTOFF_HOUR', 14),
            tz_offset_hours=_env_int('POSPAY_TZ_OFFSET', -4),
            watchlist=watch,
            extra_holidays=_env_list('POSPAY_HOLIDAYS'),
        )


@dataclass
class PosPayEnrollment:
    enrollment_id: str
    userid: str
    account: str
    match_payee: int
    reverse_pay: int
    default_action: str
    status: str
    actor: str
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'enrollment_id': self.enrollment_id,
            'userid': self.userid,
            'account_last4': last4(self.account),
            'match_payee': bool(self.match_payee),
            'reverse_pay': bool(self.reverse_pay),
            'default_action': self.default_action,
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'active': self.status == ENROLL_ACTIVE,
            'paused': self.status == ENROLL_PAUSED,
            'archived': self.status == ENROLL_ARCHIVED,
        }


@dataclass
class PosPayIssue:
    issue_id: str
    userid: str
    account: str
    serial: str
    amount: str
    payee: str
    issue_date: str
    status: str
    actor: str
    created_at: float
    updated_at: float
    note: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'issue_id': self.issue_id,
            'userid': self.userid,
            'account_last4': last4(self.account),
            'serial': self.serial,
            'serial_masked': mask_serial(self.serial),
            'amount': self.amount,
            'payee': self.payee,
            'issue_date': self.issue_date,
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'note': self.note,
            'voidable': self.status == ISSUE_ISSUED,
        }


@dataclass
class PosPayItem:
    item_id: str
    trace_id: str
    userid: str
    account: str
    serial: str
    amount: str
    payee: str
    present_date: str
    status: str
    reason: str
    issue_id: str
    enrollment_id: str
    decision: str
    decided_by: str
    deadline_at: float
    actor: str
    releaser: str
    ofac_hit: int
    ofac_match: str
    created_at: float
    updated_at: float
    paid_at: float = 0.0
    note: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'item_id': self.item_id,
            'trace_id': self.trace_id,
            'userid': self.userid,
            'account_last4': last4(self.account),
            'serial': self.serial,
            'serial_masked': mask_serial(self.serial),
            'amount': self.amount,
            'payee': self.payee,
            'present_date': self.present_date,
            'status': self.status,
            'reason': self.reason,
            'reason_code': REASON_CODES.get(self.reason, ''),
            'issue_id': self.issue_id,
            'enrollment_id': self.enrollment_id,
            'decision': self.decision,
            'decided_by': self.decided_by,
            'deadline_at': self.deadline_at,
            'actor': self.actor,
            'releaser': self.releaser,
            'ofac_hit': bool(self.ofac_hit),
            'ofac_match': self.ofac_match,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'paid_at': self.paid_at,
            'note': self.note,
            'decidable': self.status in DECIDABLE,
            'exception': self.status == ITEM_EXCEPTION,
            'held': self.status == ITEM_HELD,
            'pending_release': self.status == ITEM_PENDING,
            'unmatched': self.status == ITEM_UNMATCHED,
        }


def _clone_enrollment(row: PosPayEnrollment) -> PosPayEnrollment:
    return PosPayEnrollment(**{**row.__dict__})


def _clone_issue(row: PosPayIssue) -> PosPayIssue:
    return PosPayIssue(**{**row.__dict__})


def _clone_item(row: PosPayItem) -> PosPayItem:
    return PosPayItem(**{**row.__dict__})


def _enrollment_from_row(row: Any) -> PosPayEnrollment:
    return PosPayEnrollment(
        enrollment_id=row['enrollment_id'],
        userid=row['userid'],
        account=row['account'],
        match_payee=int(row['match_payee']),
        reverse_pay=int(row['reverse_pay']),
        default_action=row['default_action'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
    )


def _issue_from_row(row: Any) -> PosPayIssue:
    return PosPayIssue(
        issue_id=row['issue_id'],
        userid=row['userid'],
        account=row['account'],
        serial=row['serial'],
        amount=row['amount'],
        payee=row['payee'],
        issue_date=row['issue_date'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        note=row['note'] or '',
    )


def _item_from_row(row: Any) -> PosPayItem:
    return PosPayItem(
        item_id=row['item_id'],
        trace_id=row['trace_id'],
        userid=row['userid'],
        account=row['account'],
        serial=row['serial'],
        amount=row['amount'],
        payee=row['payee'],
        present_date=row['present_date'],
        status=row['status'],
        reason=row['reason'],
        issue_id=row['issue_id'] or '',
        enrollment_id=row['enrollment_id'] or '',
        decision=row['decision'] or '',
        decided_by=row['decided_by'] or '',
        deadline_at=float(row['deadline_at'] or 0),
        actor=row['actor'],
        releaser=row['releaser'] or '',
        ofac_hit=int(row['ofac_hit'] or 0),
        ofac_match=row['ofac_match'] or '',
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        paid_at=float(row['paid_at'] or 0),
        note=row['note'] or '',
    )


class MemoryPosPayStore:
    def __init__(self) -> None:
        self._enrollments: Dict[str, PosPayEnrollment] = {}
        self._issues: Dict[str, PosPayIssue] = {}
        self._items: Dict[str, PosPayItem] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_enrollment(self, row: PosPayEnrollment) -> None:
        with self._lock:
            self._enrollments[row.enrollment_id] = row

    def get_enrollment(self, enrollment_id: str) -> Optional[PosPayEnrollment]:
        with self._lock:
            row = self._enrollments.get(enrollment_id)
            return _clone_enrollment(row) if row else None

    def update_enrollment(self, row: PosPayEnrollment) -> None:
        with self._lock:
            self._enrollments[row.enrollment_id] = row

    def list_enrollments(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[PosPayEnrollment]:
        with self._lock:
            rows = [_clone_enrollment(row) for row in self._enrollments.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != ENROLL_ARCHIVED]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_enrollment(self, userid: str, account: str) -> Optional[PosPayEnrollment]:
        with self._lock:
            for row in self._enrollments.values():
                if row.userid == userid and row.account == account and row.status in OPEN_ENROLL:
                    return _clone_enrollment(row)
        return None

    def put_issue(self, row: PosPayIssue) -> None:
        with self._lock:
            self._issues[row.issue_id] = row

    def get_issue(self, issue_id: str) -> Optional[PosPayIssue]:
        with self._lock:
            row = self._issues.get(issue_id)
            return _clone_issue(row) if row else None

    def update_issue(self, row: PosPayIssue) -> None:
        with self._lock:
            self._issues[row.issue_id] = row

    def list_issues(self, userid: Optional[str] = None) -> List[PosPayIssue]:
        with self._lock:
            rows = [_clone_issue(row) for row in self._issues.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def find_issue(self, userid: str, account: str, serial: str) -> Optional[PosPayIssue]:
        with self._lock:
            for row in self._issues.values():
                if row.userid == userid and row.account == account and row.serial == serial:
                    return _clone_issue(row)
        return None

    def put_item(self, row: PosPayItem) -> PosPayItem:
        with self._lock:
            existing_id = self._by_trace.get(row.trace_id)
            if existing_id is not None:
                return self._items[existing_id]
            self._items[row.item_id] = row
            self._by_trace[row.trace_id] = row.item_id
            return row

    def update_item(self, row: PosPayItem) -> None:
        with self._lock:
            self._items[row.item_id] = row

    def get_item(self, item_id: str) -> Optional[PosPayItem]:
        with self._lock:
            row = self._items.get(item_id)
            return _clone_item(row) if row else None

    def get_item_by_trace(self, trace_id: str) -> Optional[PosPayItem]:
        with self._lock:
            item_id = self._by_trace.get(trace_id)
            return _clone_item(self._items[item_id]) if item_id else None

    def list_items(self, userid: Optional[str] = None) -> List[PosPayItem]:
        with self._lock:
            rows = [_clone_item(row) for row in self._items.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows

    def paid_serial(self, userid: str, account: str, serial: str, *, exclude_item_id: str = '') -> bool:
        with self._lock:
            for row in self._items.values():
                if (
                    row.userid == userid
                    and row.account == account
                    and row.serial == serial
                    and row.status == ITEM_PAID
                    and row.item_id != exclude_item_id
                ):
                    return True
        return False


class SqlitePosPayStore:
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
                CREATE TABLE IF NOT EXISTS enrollments (
                    enrollment_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL,
                    match_payee INTEGER NOT NULL DEFAULT 1,
                    reverse_pay INTEGER NOT NULL DEFAULT 0,
                    default_action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS issues (
                    issue_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL,
                    serial TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    payee TEXT NOT NULL,
                    issue_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS items (
                    item_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL,
                    serial TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    payee TEXT NOT NULL,
                    present_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    issue_id TEXT NOT NULL DEFAULT '',
                    enrollment_id TEXT NOT NULL DEFAULT '',
                    decision TEXT NOT NULL DEFAULT '',
                    decided_by TEXT NOT NULL DEFAULT '',
                    deadline_at REAL NOT NULL DEFAULT 0,
                    actor TEXT NOT NULL,
                    releaser TEXT NOT NULL DEFAULT '',
                    ofac_hit INTEGER NOT NULL DEFAULT 0,
                    ofac_match TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    paid_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_enrollment(self, row: PosPayEnrollment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO enrollments (
                    enrollment_id, userid, account, match_payee, reverse_pay,
                    default_action, status, actor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.enrollment_id, row.userid, row.account, row.match_payee, row.reverse_pay,
                    row.default_action, row.status, row.actor, row.created_at, row.updated_at,
                ),
            )
            conn.commit()

    def get_enrollment(self, enrollment_id: str) -> Optional[PosPayEnrollment]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM enrollments WHERE enrollment_id = ?', (enrollment_id,),
            ).fetchone()
        return _enrollment_from_row(row) if row else None

    def update_enrollment(self, row: PosPayEnrollment) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE enrollments SET match_payee=?, reverse_pay=?, default_action=?,
                    status=?, actor=?, updated_at=?
                WHERE enrollment_id=?
                """,
                (
                    row.match_payee, row.reverse_pay, row.default_action, row.status,
                    row.actor, row.updated_at, row.enrollment_id,
                ),
            )
            conn.commit()

    def list_enrollments(self, userid: Optional[str] = None, *, include_archived: bool = True) -> List[PosPayEnrollment]:
        sql = 'SELECT * FROM enrollments'
        params: List[Any] = []
        if userid is not None:
            sql += ' WHERE userid = ?'
            params.append(userid)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        items = [_enrollment_from_row(row) for row in rows]
        if not include_archived:
            items = [row for row in items if row.status != ENROLL_ARCHIVED]
        return items

    def find_enrollment(self, userid: str, account: str) -> Optional[PosPayEnrollment]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM enrollments
                WHERE userid = ? AND account = ? AND status IN ('active', 'paused')
                ORDER BY created_at DESC LIMIT 1
                """,
                (userid, account),
            ).fetchone()
        return _enrollment_from_row(row) if row else None

    def put_issue(self, row: PosPayIssue) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO issues (
                    issue_id, userid, account, serial, amount, payee, issue_date,
                    status, actor, created_at, updated_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.issue_id, row.userid, row.account, row.serial, row.amount, row.payee,
                    row.issue_date, row.status, row.actor, row.created_at, row.updated_at, row.note,
                ),
            )
            conn.commit()

    def get_issue(self, issue_id: str) -> Optional[PosPayIssue]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM issues WHERE issue_id = ?', (issue_id,)).fetchone()
        return _issue_from_row(row) if row else None

    def update_issue(self, row: PosPayIssue) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE issues SET status=?, actor=?, updated_at=?, note=?
                WHERE issue_id=?
                """,
                (row.status, row.actor, row.updated_at, row.note, row.issue_id),
            )
            conn.commit()

    def list_issues(self, userid: Optional[str] = None) -> List[PosPayIssue]:
        sql = 'SELECT * FROM issues'
        params: List[Any] = []
        if userid is not None:
            sql += ' WHERE userid = ?'
            params.append(userid)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_issue_from_row(row) for row in rows]

    def find_issue(self, userid: str, account: str, serial: str) -> Optional[PosPayIssue]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM issues WHERE userid = ? AND account = ? AND serial = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (userid, account, serial),
            ).fetchone()
        return _issue_from_row(row) if row else None

    def put_item(self, row: PosPayItem) -> PosPayItem:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM items WHERE trace_id = ?', (row.trace_id,),
            ).fetchone()
            if existing is not None:
                return _item_from_row(existing)
            conn.execute(
                """
                INSERT INTO items (
                    item_id, trace_id, userid, account, serial, amount, payee, present_date,
                    status, reason, issue_id, enrollment_id, decision, decided_by, deadline_at,
                    actor, releaser, ofac_hit, ofac_match, created_at, updated_at, paid_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.item_id, row.trace_id, row.userid, row.account, row.serial, row.amount,
                    row.payee, row.present_date, row.status, row.reason, row.issue_id,
                    row.enrollment_id, row.decision, row.decided_by, row.deadline_at, row.actor,
                    row.releaser, row.ofac_hit, row.ofac_match, row.created_at, row.updated_at,
                    row.paid_at, row.note,
                ),
            )
            conn.commit()
            return row

    def update_item(self, row: PosPayItem) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE items SET userid=?, account=?, status=?, reason=?, issue_id=?,
                    enrollment_id=?, decision=?, decided_by=?, deadline_at=?, actor=?,
                    releaser=?, ofac_hit=?, ofac_match=?, updated_at=?, paid_at=?, note=?
                WHERE item_id=?
                """,
                (
                    row.userid, row.account, row.status, row.reason, row.issue_id,
                    row.enrollment_id, row.decision, row.decided_by, row.deadline_at, row.actor,
                    row.releaser, row.ofac_hit, row.ofac_match, row.updated_at, row.paid_at,
                    row.note, row.item_id,
                ),
            )
            conn.commit()

    def get_item(self, item_id: str) -> Optional[PosPayItem]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM items WHERE item_id = ?', (item_id,)).fetchone()
        return _item_from_row(row) if row else None

    def get_item_by_trace(self, trace_id: str) -> Optional[PosPayItem]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM items WHERE trace_id = ?', (trace_id,)).fetchone()
        return _item_from_row(row) if row else None

    def list_items(self, userid: Optional[str] = None) -> List[PosPayItem]:
        sql = 'SELECT * FROM items'
        params: List[Any] = []
        if userid is not None:
            sql += ' WHERE userid = ?'
            params.append(userid)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_item_from_row(row) for row in rows]

    def paid_serial(self, userid: str, account: str, serial: str, *, exclude_item_id: str = '') -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT item_id FROM items
                WHERE userid = ? AND account = ? AND serial = ? AND status = ?
                  AND item_id != ?
                LIMIT 1
                """,
                (userid, account, serial, ITEM_PAID, exclude_item_id),
            ).fetchone()
        return row is not None


class PosPayService:
    def __init__(
        self,
        policy: PosPayPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        lookup_fn: Optional[Callable[[str], Optional[str]]] = None,
        screen_fn: Optional[Callable[..., ScreenResult]] = None,
        calendar: Optional[WireCalendar] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.lookup_fn = lookup_fn
        self.screen_fn = screen_fn
        self.calendar = calendar or WireCalendar(
            cutoff_hour=policy.cutoff_hour,
            tz_offset_hours=policy.tz_offset_hours,
            extra_holidays=policy.extra_holidays,
        )

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise PosPayError('pospay_disabled', 'Positive Pay is disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise PosPayError('pospay_forbidden', 'Customers cannot manage Positive Pay.')

    def _require_decide(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_decide:
            raise PosPayError('pospay_forbidden', 'Customers cannot decide Positive Pay exceptions.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise PosPayError('pospay_forbidden', 'Staff only.')

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
            raise PosPayError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise PosPayError('credit_not_allowed', 'Credit accounts cannot enroll in Positive Pay.')

    def _assert_amount(self, dollars: Decimal) -> None:
        if dollars < self.policy.min_amount or dollars > self.policy.max_amount:
            raise PosPayError('amount_out_of_range', 'Amount is outside the allowed range.')

    def _screen(self, name: str, aliases: Sequence[str] = ()) -> ScreenResult:
        if self.screen_fn is not None:
            return self.screen_fn(name, watchlist=self.policy.watchlist, aliases=aliases)
        return screen_name(name, watchlist=self.policy.watchlist, aliases=aliases)

    def _needs_dual_control(self, amount: Decimal) -> bool:
        return amount >= self.policy.dual_control_threshold

    def _rematch_unmatched(self, userid: str, account: str) -> None:
        for item in self.store.list_items(userid):
            if item.account != account:
                continue
            reopen = item.status == ITEM_UNMATCHED or (
                item.status == ITEM_EXCEPTION and item.reason == REASON_NOT_ISSUED
            )
            if not reopen:
                continue
            enrollment = self.store.find_enrollment(userid, account)
            item.enrollment_id = enrollment.enrollment_id if enrollment is not None else ''
            ofac = ScreenResult(True, item.ofac_match, 100) if item.ofac_hit else ScreenResult(False, '', 0)
            self._evaluate(item, ofac=ofac)

    def _deadline_at(self, ts: float) -> float:
        value = self.calendar.value_date(ts)
        local = datetime(
            value.year, value.month, value.day,
            self.calendar.cutoff_hour, self.calendar.cutoff_minute,
            tzinfo=self.calendar.tz,
        )
        return local.timestamp()

    def _present_date(self, ts: float, explicit: str = '') -> str:
        if explicit:
            return normalize_issue_date(explicit)
        return self.calendar.local_dt(ts).date().strftime('%Y%m%d')

    def enroll(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        match_payee: Optional[bool] = None,
        reverse_pay: Optional[bool] = None,
        default_action: Any = None,
    ) -> PosPayEnrollment:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise PosPayError('pospay_forbidden', 'Not allowed to enroll this customer.')
        internal = normalize_account(account)
        self._assert_internal_account(owner_userid, internal)
        existing = self.store.find_enrollment(owner_userid, internal)
        if existing is not None:
            raise PosPayError('enrollment_duplicate', 'That account is already enrolled.')
        open_rows = [row for row in self.store.list_enrollments(owner_userid) if row.status in OPEN_ENROLL]
        if len(open_rows) >= self.policy.max_enrollments:
            raise PosPayError('enrollment_limit', 'Positive Pay enrollment limit reached.')
        now = float(self.clock())
        row = PosPayEnrollment(
            enrollment_id=uuid.uuid4().hex,
            userid=owner_userid,
            account=internal,
            match_payee=1 if (self.policy.match_payee_default if match_payee is None else bool(match_payee)) else 0,
            reverse_pay=1 if (self.policy.reverse_default if reverse_pay is None else bool(reverse_pay)) else 0,
            default_action=normalize_default_action(
                default_action, default=self.policy.default_action,
            ),
            status=ENROLL_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        self.store.put_enrollment(row)
        self._rematch_unmatched(owner_userid, internal)
        return row

    def _ensure_enrollment(self, owner_userid: str, account: str, actor: str, actor_type: str) -> PosPayEnrollment:
        row = self.store.find_enrollment(owner_userid, account)
        if row is not None:
            if row.status == ENROLL_PAUSED:
                raise PosPayError('enrollment_paused', 'Positive Pay enrollment is paused.')
            return row
        return self.enroll(
            owner_userid=owner_userid, actor=actor, actor_type=actor_type, account=account,
        )

    def get_enrollment(self, *, enrollment_id: str, actor: str, actor_type: str) -> PosPayEnrollment:
        self._require_enabled()
        row = self.store.get_enrollment(enrollment_id)
        if row is None:
            raise PosPayError('enrollment_not_found', 'Positive Pay enrollment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise PosPayError('pospay_forbidden', 'Not allowed to view this enrollment.')
        return row

    def set_enrollment_status(
        self,
        *,
        enrollment_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> PosPayEnrollment:
        self._require_manage(actor_type)
        row = self.get_enrollment(enrollment_id=enrollment_id, actor=actor, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': ENROLL_PAUSED, 'hold': ENROLL_PAUSED,
            'resume': ENROLL_ACTIVE, 'activate': ENROLL_ACTIVE, 'unpause': ENROLL_ACTIVE,
            'archive': ENROLL_ARCHIVED, 'close': ENROLL_ARCHIVED, 'cancel': ENROLL_ARCHIVED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {ENROLL_PAUSED, ENROLL_ACTIVE, ENROLL_ARCHIVED}:
            raise PosPayError('invalid_status', 'Status must be pause, resume, or archive.')
        if row.status == ENROLL_ARCHIVED:
            raise PosPayError('already_archived', 'Enrollment is already archived.')
        if wanted == ENROLL_PAUSED:
            if row.status == ENROLL_PAUSED:
                raise PosPayError('already_paused', 'Enrollment is already paused.')
            if row.status != ENROLL_ACTIVE:
                raise PosPayError('invalid_status', 'Only an active enrollment can be paused.')
        elif wanted == ENROLL_ACTIVE:
            if row.status == ENROLL_ACTIVE:
                raise PosPayError('already_active', 'Enrollment is already active.')
            if row.status != ENROLL_PAUSED:
                raise PosPayError('invalid_status', 'Only a paused enrollment can be resumed.')
        row.status = wanted
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        self.store.update_enrollment(row)
        if wanted == ENROLL_ACTIVE:
            self._rematch_unmatched(row.userid, row.account)
        return row

    def add_issue(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        serial: Any,
        amount: Any,
        payee: Any,
        issue_date: Any,
        note: Any = '',
    ) -> PosPayIssue:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise PosPayError('pospay_forbidden', 'Not allowed to add issues for this customer.')
        internal = normalize_account(account)
        self._assert_internal_account(owner_userid, internal)
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        check = normalize_serial(serial)
        name = normalize_payee(payee)
        day = normalize_issue_date(issue_date)
        self._ensure_enrollment(owner_userid, internal, actor, actor_type)
        existing = self.store.find_issue(owner_userid, internal, check)
        if existing is not None:
            raise PosPayError('issue_duplicate', 'That serial is already on the issue file.')
        open_rows = self.store.list_issues(owner_userid)
        if len(open_rows) >= self.policy.max_issues:
            raise PosPayError('issue_limit', 'Issued-check limit reached.')
        now = float(self.clock())
        row = PosPayIssue(
            issue_id=uuid.uuid4().hex,
            userid=owner_userid,
            account=internal,
            serial=check,
            amount=money_str(dollars),
            payee=name,
            issue_date=day,
            status=ISSUE_ISSUED,
            actor=str(actor),
            created_at=now,
            updated_at=now,
            note=str(note or '')[:240],
        )
        self.store.put_issue(row)
        self._rematch_unmatched(owner_userid, internal)
        return row

    def ingest_issue_file(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        text: Any,
    ) -> List[PosPayIssue]:
        rows = []
        for line in split_pospay_file(text):
            parsed = parse_issue_record(line)
            if parsed['void'] == '1':
                existing = self.store.find_issue(owner_userid, parsed['account'], parsed['serial'])
                if existing is None:
                    raise PosPayError('issue_not_found', 'Cannot void a serial that was never issued.')
                rows.append(self.void_issue(issue_id=existing.issue_id, actor=actor, actor_type=actor_type))
                continue
            rows.append(self.add_issue(
                owner_userid=owner_userid,
                actor=actor,
                actor_type=actor_type,
                account=parsed['account'],
                serial=parsed['serial'],
                amount=parsed['amount'],
                payee=parsed['payee'],
                issue_date=parsed['issue_date'],
            ))
        return rows

    def void_issue(self, *, issue_id: str, actor: str, actor_type: str, note: Any = '') -> PosPayIssue:
        self._require_manage(actor_type)
        row = self.store.get_issue(issue_id)
        if row is None:
            raise PosPayError('issue_not_found', 'Issued check not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise PosPayError('pospay_forbidden', 'Not allowed to void this issue.')
        if row.status == ISSUE_VOIDED:
            raise PosPayError('already_voided', 'Issued check is already voided.')
        if row.status == ISSUE_PAID:
            raise PosPayError('already_paid', 'Issued check was already paid.')
        row.status = ISSUE_VOIDED
        row.actor = str(actor)
        row.updated_at = float(self.clock())
        row.note = str(note or row.note)[:240]
        self.store.update_issue(row)
        return row

    def preview(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        serial: Any,
        amount: Any,
        payee: Any,
        account: Any,
        present_date: Any = '',
    ) -> Dict[str, Any]:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise PosPayError('pospay_forbidden', 'Not allowed to preview this customer.')
        internal = normalize_account(account)
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        check = normalize_serial(serial)
        name = normalize_payee(payee)
        now = float(self.clock())
        day = self._present_date(now, str(present_date or ''))
        enrollment = self.store.find_enrollment(owner_userid, internal)
        match = self._match(
            owner_userid=owner_userid,
            account=internal,
            serial=check,
            amount=dollars,
            payee=name,
            present_date=day,
            enrollment=enrollment,
            exclude_item_id='',
        )
        ofac = self._screen(name)
        return {
            'serial': check,
            'amount': money_str(dollars),
            'payee': name,
            'account_last4': last4(internal),
            'present_date': day,
            'enrolled': enrollment is not None and enrollment.status == ENROLL_ACTIVE,
            'match': match,
            'ofac': ofac.to_dict(),
            'dual_control': self._needs_dual_control(dollars),
            'clock': self.calendar.snapshot(now),
            'deadline_at': self._deadline_at(now),
        }

    def _match(
        self,
        *,
        owner_userid: str,
        account: str,
        serial: str,
        amount: Decimal,
        payee: str,
        present_date: str,
        enrollment: Optional[PosPayEnrollment],
        exclude_item_id: str,
    ) -> Dict[str, Any]:
        if enrollment is None or enrollment.status != ENROLL_ACTIVE:
            return {'matched': False, 'reason': '', 'issue_id': '', 'unmatched': True, 'code': 'not_enrolled'}
        if bool(enrollment.reverse_pay):
            return {'matched': False, 'reason': REASON_REVIEW, 'issue_id': '', 'unmatched': False, 'code': REASON_REVIEW}
        issue = self.store.find_issue(owner_userid, account, serial)
        if issue is None:
            return {'matched': False, 'reason': REASON_NOT_ISSUED, 'issue_id': '', 'unmatched': False, 'code': REASON_NOT_ISSUED}
        if issue.status == ISSUE_VOIDED:
            return {'matched': False, 'reason': REASON_VOIDED, 'issue_id': issue.issue_id, 'unmatched': False, 'code': REASON_VOIDED}
        if issue.status == ISSUE_PAID or self.store.paid_serial(owner_userid, account, serial, exclude_item_id=exclude_item_id):
            return {'matched': False, 'reason': REASON_DUPLICATE, 'issue_id': issue.issue_id, 'unmatched': False, 'code': REASON_DUPLICATE}
        issued_amount = parse_money(issue.amount)
        if issued_amount != amount:
            return {'matched': False, 'reason': REASON_AMOUNT, 'issue_id': issue.issue_id, 'unmatched': False, 'code': REASON_AMOUNT}
        if bool(enrollment.match_payee) and not payees_match(issue.payee, payee):
            return {'matched': False, 'reason': REASON_PAYEE, 'issue_id': issue.issue_id, 'unmatched': False, 'code': REASON_PAYEE}
        present = date_from_yyyymmdd(present_date)
        issued_day = date_from_yyyymmdd(issue.issue_date)
        if issued_day > present:
            return {'matched': False, 'reason': REASON_POSTDATED, 'issue_id': issue.issue_id, 'unmatched': False, 'code': REASON_POSTDATED}
        if present > issued_day + timedelta(days=int(self.policy.stale_days)):
            return {'matched': False, 'reason': REASON_STALE, 'issue_id': issue.issue_id, 'unmatched': False, 'code': REASON_STALE}
        return {'matched': True, 'reason': '', 'issue_id': issue.issue_id, 'unmatched': False, 'code': ''}

    def ingest(
        self,
        *,
        actor: str,
        actor_type: str,
        serial: Any,
        amount: Any,
        payee: Any,
        account: Any,
        customer_id: Any = '',
        trace_id: Any = None,
        present_date: Any = '',
        note: Any = '',
    ) -> Tuple[PosPayItem, bool]:
        self._require_staff(actor_type)
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        check = normalize_serial(serial)
        name = normalize_payee(payee)
        internal = normalize_account(account)
        tid = normalize_id(trace_id)
        existing = self.store.get_item_by_trace(tid)
        if existing is not None:
            return existing, False
        if len(self.store.list_items()) >= self.policy.max_items:
            raise PosPayError('item_limit', 'Presentment limit reached.')
        now = float(self.clock())
        day = self._present_date(now, str(present_date or ''))
        owner = str(customer_id or '').strip()
        if not owner and self.lookup_fn is not None:
            owner = str(self.lookup_fn(internal) or '').strip()
        enrollment = None
        unmatched_reason = ''
        if not owner:
            unmatched_reason = 'not_enrolled'
        else:
            owned = self._owned_accounts(owner)
            types = self._account_types(owner)
            if owned and internal not in owned:
                unmatched_reason = 'not_enrolled'
                owner = owner
            elif types.get(internal) == 'credit' and not self.policy.allow_credit:
                unmatched_reason = 'credit_not_allowed'
            else:
                enrollment = self.store.find_enrollment(owner, internal)
                if enrollment is None or enrollment.status != ENROLL_ACTIVE:
                    unmatched_reason = 'not_enrolled'
        ofac = self._screen(name)
        item = PosPayItem(
            item_id=uuid.uuid4().hex,
            trace_id=tid,
            userid=owner,
            account=internal,
            serial=check,
            amount=money_str(dollars),
            payee=name,
            present_date=day,
            status=ITEM_UNMATCHED,
            reason=unmatched_reason,
            issue_id='',
            enrollment_id=enrollment.enrollment_id if enrollment is not None else '',
            decision='',
            decided_by='',
            deadline_at=self._deadline_at(now),
            actor=str(actor),
            releaser='',
            ofac_hit=1 if ofac.hit else 0,
            ofac_match=ofac.matched,
            created_at=now,
            updated_at=now,
            note=str(note or '')[:240],
        )
        stored = self.store.put_item(item)
        if stored.item_id != item.item_id:
            return stored, False
        self._evaluate(item, ofac=ofac)
        return item, True

    def ingest_file(
        self,
        *,
        actor: str,
        actor_type: str,
        text: Any,
        customer_id: Any = '',
    ) -> List[PosPayItem]:
        items = []
        for line in split_pospay_file(text):
            parsed = parse_presentment_record(line)
            item, _created = self.ingest(
                actor=actor,
                actor_type=actor_type,
                serial=parsed['serial'],
                amount=parsed['amount'],
                payee=parsed['payee'],
                account=parsed['account'],
                customer_id=customer_id,
                trace_id=parsed['presentment_id'],
                present_date=parsed.get('present_date') or '',
            )
            items.append(item)
        return items

    def _evaluate(self, item: PosPayItem, *, ofac: Optional[ScreenResult] = None) -> PosPayItem:
        if item.status in {ITEM_PAID, ITEM_RETURNED, ITEM_REJECTED, ITEM_NSF, ITEM_FAILED}:
            return item
        if not item.userid:
            item.status = ITEM_UNMATCHED
            item.reason = item.reason or 'not_enrolled'
            item.updated_at = float(self.clock())
            self.store.update_item(item)
            return item
        enrollment = None
        if item.enrollment_id:
            enrollment = self.store.get_enrollment(item.enrollment_id)
        if enrollment is None:
            enrollment = self.store.find_enrollment(item.userid, item.account)
        if enrollment is None or enrollment.status != ENROLL_ACTIVE:
            item.status = ITEM_UNMATCHED
            item.reason = 'not_enrolled'
            item.updated_at = float(self.clock())
            self.store.update_item(item)
            return item
        types = self._account_types(item.userid)
        if types.get(item.account) == 'credit' and not self.policy.allow_credit:
            item.status = ITEM_UNMATCHED
            item.reason = 'credit_not_allowed'
            item.updated_at = float(self.clock())
            self.store.update_item(item)
            return item
        dollars = parse_money(item.amount)
        match = self._match(
            owner_userid=item.userid,
            account=item.account,
            serial=item.serial,
            amount=dollars,
            payee=item.payee,
            present_date=item.present_date or self._present_date(float(self.clock())),
            enrollment=enrollment,
            exclude_item_id=item.item_id,
        )
        item.enrollment_id = enrollment.enrollment_id
        item.issue_id = str(match.get('issue_id') or '')
        if ofac is None:
            ofac = ScreenResult(bool(item.ofac_hit), item.ofac_match, 100 if item.ofac_hit else 0)
        if match.get('unmatched'):
            item.status = ITEM_UNMATCHED
            item.reason = str(match.get('code') or 'not_enrolled')
        elif ofac.hit:
            item.status = ITEM_HELD
            item.reason = REASON_OFAC
            item.ofac_hit = 1
            item.ofac_match = ofac.matched
        elif not match.get('matched'):
            item.status = ITEM_EXCEPTION
            item.reason = str(match.get('reason') or REASON_OTHER)
        elif self._needs_dual_control(dollars):
            item.status = ITEM_PENDING
            item.reason = ''
        else:
            self._pay(item, actor=item.actor, mark_issue=True)
            return item
        item.updated_at = float(self.clock())
        self.store.update_item(item)
        return item

    def _pay(self, item: PosPayItem, *, actor: str, mark_issue: bool) -> PosPayItem:
        dollars = parse_money(item.amount)
        remark = 'pospay %s' % item.serial
        if self.debit_fn is not None:
            try:
                result = self.debit_fn(item.account, money_str(dollars), remark)
            except Exception as exc:
                item.status = ITEM_FAILED
                item.note = str(exc)[:240]
                item.updated_at = float(self.clock())
                self.store.update_item(item)
                raise PosPayError('failed', 'Debit failed.', item=item) from exc
            kind = _classify_money_result(result)
            if kind == 'nsf':
                item.status = ITEM_NSF
                item.reason = REASON_NSF
                item.note = str(result)[:240]
                item.updated_at = float(self.clock())
                self.store.update_item(item)
                raise PosPayError('nsf', 'Insufficient funds.', item=item)
            if kind != 'ok':
                item.status = ITEM_FAILED
                item.note = str(result)[:240]
                item.updated_at = float(self.clock())
                self.store.update_item(item)
                raise PosPayError('failed', 'Debit failed.', item=item)
        now = float(self.clock())
        item.status = ITEM_PAID
        item.decision = DEFAULT_PAY
        item.decided_by = str(actor)
        item.paid_at = now
        item.updated_at = now
        self.store.update_item(item)
        if mark_issue and item.issue_id:
            issue = self.store.get_issue(item.issue_id)
            if issue is not None and issue.status == ISSUE_ISSUED:
                issue.status = ISSUE_PAID
                issue.actor = str(actor)
                issue.updated_at = now
                self.store.update_issue(issue)
        return item

    def _return(self, item: PosPayItem, *, actor: str, reason: str) -> PosPayItem:
        now = float(self.clock())
        item.status = ITEM_RETURNED
        item.decision = DEFAULT_RETURN
        item.decided_by = str(actor)
        item.reason = reason or item.reason or REASON_OTHER
        item.updated_at = now
        self.store.update_item(item)
        return item

    def get_item(self, *, item_id: str, actor: str, actor_type: str) -> PosPayItem:
        self._require_enabled()
        row = self.store.get_item(item_id)
        if row is None:
            raise PosPayError('item_not_found', 'Presentment not found.')
        if actor_type not in EMPLOYEE_ROLES and row.userid != actor:
            raise PosPayError('pospay_forbidden', 'Not allowed to view this presentment.')
        return row

    def decide(
        self,
        *,
        item_id: str,
        actor: str,
        actor_type: str,
        decision: Any,
        reason: Any = '',
        note: Any = '',
        force: bool = False,
    ) -> PosPayItem:
        self._require_decide(actor_type)
        item = self.get_item(item_id=item_id, actor=actor, actor_type=actor_type)
        wanted = normalize_decision(decision)
        now = float(self.clock())
        if item.status in {ITEM_PAID}:
            raise PosPayError('already_paid', 'Presentment is already paid.')
        if item.status in {ITEM_RETURNED}:
            raise PosPayError('already_returned', 'Presentment is already returned.')
        if item.status == ITEM_REJECTED:
            raise PosPayError('not_decidable', 'Rejected presentments cannot be decided.')
        if item.status == ITEM_HELD:
            raise PosPayError('ofac_hold', 'OFAC hold must be overridden first.')
        if item.status == ITEM_PENDING:
            raise PosPayError('not_decidable', 'High-value match is pending dual-control release.')
        if item.status == ITEM_UNMATCHED:
            raise PosPayError('not_decidable', 'Unmatched presentment cannot be decided.')
        if item.status != ITEM_EXCEPTION:
            raise PosPayError('not_decidable', 'Only exceptions can be paid or returned.')
        if actor_type not in EMPLOYEE_ROLES and not force and item.deadline_at and now >= float(item.deadline_at):
            raise PosPayError('decision_window_closed', 'Decision window has closed.')
        item.note = str(note or item.note)[:240]
        if wanted == DEFAULT_PAY:
            return self._pay(item, actor=actor, mark_issue=bool(item.issue_id))
        return self._return(item, actor=actor, reason=normalize_reason(reason, default=item.reason or REASON_OTHER))

    def assign(
        self,
        *,
        item_id: str,
        actor: str,
        actor_type: str,
        customer_id: Any,
        account: Any = None,
    ) -> PosPayItem:
        self._require_staff(actor_type)
        item = self.get_item(item_id=item_id, actor=actor, actor_type=actor_type)
        if item.status != ITEM_UNMATCHED:
            raise PosPayError('not_assignable', 'Only unmatched presentments can be assigned.')
        owner = str(customer_id or '').strip()
        if not owner:
            raise PosPayError('missing_customer_id', 'Customer id is required.')
        internal = normalize_account(account or item.account)
        self._assert_internal_account(owner, internal)
        item.userid = owner
        item.account = internal
        item.actor = str(actor)
        item.updated_at = float(self.clock())
        enrollment = self.store.find_enrollment(owner, internal)
        item.enrollment_id = enrollment.enrollment_id if enrollment is not None else ''
        self.store.update_item(item)
        ofac = ScreenResult(False, '', 0) if item.ofac_hit else ScreenResult(bool(item.ofac_hit), item.ofac_match, 0)
        if item.ofac_hit:
            ofac = ScreenResult(True, item.ofac_match, 100)
        return self._evaluate(item, ofac=ofac)

    def override_ofac(self, *, item_id: str, actor: str, actor_type: str, note: Any = '') -> PosPayItem:
        self._require_staff(actor_type)
        item = self.get_item(item_id=item_id, actor=actor, actor_type=actor_type)
        if item.status != ITEM_HELD:
            raise PosPayError('not_overridable', 'Only held presentments can be OFAC-overridden.')
        item.ofac_hit = 0
        item.ofac_match = ''
        item.note = str(note or item.note)[:240]
        item.actor = str(actor)
        item.updated_at = float(self.clock())
        self.store.update_item(item)
        return self._evaluate(item, ofac=ScreenResult(False, '', 0))

    def release(self, *, item_id: str, actor: str, actor_type: str) -> PosPayItem:
        self._require_staff(actor_type)
        item = self.get_item(item_id=item_id, actor=actor, actor_type=actor_type)
        if item.status != ITEM_PENDING:
            raise PosPayError('not_releasable', 'Only pending-release presentments can be released.')
        if item.actor == str(actor):
            raise PosPayError('same_approver', 'A different employee must release the pay.')
        item.releaser = str(actor)
        return self._pay(item, actor=actor, mark_issue=bool(item.issue_id))

    def reject(self, *, item_id: str, actor: str, actor_type: str, note: Any = '') -> PosPayItem:
        self._require_staff(actor_type)
        item = self.get_item(item_id=item_id, actor=actor, actor_type=actor_type)
        if item.status not in OPEN_ITEMS:
            raise PosPayError('not_rejectable', 'Presentment cannot be rejected.')
        item.status = ITEM_REJECTED
        item.actor = str(actor)
        item.note = str(note or item.note)[:240]
        item.updated_at = float(self.clock())
        self.store.update_item(item)
        return item

    def export_decisions(self, *, owner_userid: str, actor: str, actor_type: str) -> str:
        self._require_staff(actor_type)
        items = [row for row in self.store.list_items(owner_userid) if row.status in {ITEM_PAID, ITEM_RETURNED}]
        lines = []
        for row in items:
            lines.append(compose_decision_record(
                serial=row.serial,
                amount=parse_money(row.amount),
                account=row.account,
                payee=row.payee,
                decision=row.decision or (DEFAULT_PAY if row.status == ITEM_PAID else DEFAULT_RETURN),
                reason=row.reason or REASON_OTHER,
                presentment_id=row.trace_id,
            ))
        return '\n'.join(lines)

    def run_due(self, userid: Optional[str] = None) -> List[PosPayItem]:
        self._require_enabled()
        now = float(self.clock())
        due = []
        for item in self.store.list_items(userid):
            if item.status not in DEFAULTABLE:
                continue
            if item.deadline_at and now < float(item.deadline_at):
                continue
            enrollment = None
            if item.enrollment_id:
                enrollment = self.store.get_enrollment(item.enrollment_id)
            action = enrollment.default_action if enrollment is not None else self.policy.default_action
            try:
                if action == DEFAULT_PAY:
                    self._pay(item, actor='system', mark_issue=bool(item.issue_id))
                else:
                    self._return(item, actor='system', reason=item.reason or REASON_OTHER)
            except PosPayError:
                due.append(item)
                continue
            due.append(item)
        return due

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        _ = actor
        now = float(self.clock())
        if actor_type not in EMPLOYEE_ROLES:
            self.run_due(userid)
        enrollments = self.store.list_enrollments(userid)
        issues = self.store.list_issues(userid)
        items = self.store.list_items(userid)
        paid_ytd = Decimal('0.00')
        returned_ytd = Decimal('0.00')
        for row in items:
            if row.status == ITEM_PAID:
                paid_ytd += parse_money(row.amount, allow_zero=True)
            if row.status == ITEM_RETURNED:
                returned_ytd += parse_money(row.amount, allow_zero=True)
        return {
            'enabled': self.policy.enabled,
            'max_amount': money_str(self.policy.max_amount),
            'dual_control': money_str(self.policy.dual_control_threshold),
            'stale_days': self.policy.stale_days,
            'default_action': self.policy.default_action,
            'clock': self.calendar.snapshot(now),
            'enrollments': [row.to_dict() for row in enrollments[:40]],
            'issues': [row.to_dict() for row in issues[:40]],
            'items': [row.to_dict() for row in items[:40]],
            'exceptions': [row.to_dict() for row in items if row.status == ITEM_EXCEPTION][:40],
            'ytd_paid': money_str(paid_ytd),
            'ytd_returned': money_str(returned_ytd),
            'active_count': sum(1 for row in enrollments if row.status == ENROLL_ACTIVE),
            'open_count': sum(1 for row in items if row.status in OPEN_ITEMS),
            'exception_count': sum(1 for row in items if row.status == ITEM_EXCEPTION),
        }


_SERVICE: Optional[PosPayService] = None


def set_service(service: Optional[PosPayService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[PosPayService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('POSPAY_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryPosPayStore()
    path = os.environ.get('POSPAY_DB', DEFAULT_STORE_PATH)
    return SqlitePosPayStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    lookup_fn: Optional[Callable[[str], Optional[str]]] = None,
    screen_fn: Optional[Callable[..., ScreenResult]] = None,
    calendar: Optional[WireCalendar] = None,
) -> PosPayService:
    if store is None:
        store = default_store()
    return PosPayService(
        PosPayPolicy.from_env(),
        store,
        clock=clock,
        debit_fn=debit_fn,
        credit_fn=credit_fn,
        accounts_fn=accounts_fn,
        lookup_fn=lookup_fn,
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
        'enrollment_duplicate': 409,
        'enrollment_limit': 409,
        'issue_duplicate': 409,
        'issue_limit': 409,
        'item_limit': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_voided': 409,
        'already_paid': 409,
        'already_returned': 409,
        'nsf': 409,
        'failed': 409,
        'pospay_forbidden': 403,
        'pospay_disabled': 403,
        'enrollment_paused': 403,
        'credit_not_allowed': 403,
        'ofac_hold': 403,
        'same_approver': 403,
        'not_decidable': 403,
        'not_assignable': 403,
        'not_overridable': 403,
        'not_releasable': 403,
        'not_rejectable': 403,
        'decision_window_closed': 403,
        'enrollment_not_found': 404,
        'issue_not_found': 404,
        'item_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_serial': 400,
        'invalid_name': 400,
        'invalid_date': 400,
        'invalid_file': 400,
        'invalid_reason': 400,
        'invalid_status': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_enrollment': 400,
        'missing_issue': 400,
        'missing_item': 400,
        'missing_file': 400,
    }.get(code, 400)


def _error_body(exc: PosPayError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('item') is not None:
        body['item'] = exc.extra['item'].to_dict()
    if exc.extra.get('issue') is not None:
        body['issue'] = exc.extra['issue'].to_dict()
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except PosPayError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list(service: PosPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'PosPay': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_enroll(service: PosPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.enroll(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account') or values.get('default_account') or values.get('from_account'),
            match_payee=values.get('match_payee') if 'match_payee' in values else None,
            reverse_pay=values.get('reverse_pay') if 'reverse_pay' in values else None,
            default_action=values.get('default_action'),
        )
        return jsonify({
            'message': 'Positive Pay enrolled',
            'enrollment': row.to_dict(),
            'PosPay': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def _enrollment_status_route(service: PosPayService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    enrollment_id = str(values.get('enrollment_id') or '').strip()
    if not enrollment_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_enrollment'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.set_enrollment_status(
            enrollment_id=enrollment_id, actor=userid, actor_type=actor_type, status=status,
        )
        return jsonify({
            'message': ok_message,
            'enrollment': row.to_dict(),
            'PosPay': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_add_issue(service: PosPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        row = service.add_issue(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account') or values.get('from_account'),
            serial=values.get('serial'),
            amount=values.get('amount'),
            payee=values.get('payee') or values.get('name'),
            issue_date=values.get('issue_date') or values.get('date'),
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Issued check added',
            'issue': row.to_dict(),
            'PosPay': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def handle_issue_file(service: PosPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    text = values.get('file') or values.get('text') or values.get('body')
    if not text:
        return jsonify({'message': 'Some data missing', 'error': 'missing_file'}), 400

    def _run():
        rows = service.ingest_issue_file(
            owner_userid=owner, actor=userid, actor_type=actor_type, text=text,
        )
        return jsonify({
            'message': 'Issue file loaded',
            'issues': [row.to_dict() for row in rows],
            'PosPay': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def handle_void_issue(service: PosPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    issue_id = str(values.get('issue_id') or '').strip()
    if not issue_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_issue'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        row = service.void_issue(
            issue_id=issue_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Issued check voided',
            'issue': row.to_dict(),
            'PosPay': service.snapshot(row.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_preview(service: PosPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        preview = service.preview(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            serial=values.get('serial'),
            amount=values.get('amount'),
            payee=values.get('payee') or values.get('name'),
            account=values.get('account') or values.get('from_account'),
            present_date=values.get('present_date') or values.get('date') or '',
        )
        return jsonify({'preview': preview, 'PosPay': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200

    return _handle_errors(_run)


def handle_ingest(service: PosPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'

    def _run():
        item, created = service.ingest(
            actor=userid,
            actor_type=actor_type,
            serial=values.get('serial'),
            amount=values.get('amount'),
            payee=values.get('payee') or values.get('name'),
            account=values.get('account') or values.get('from_account'),
            customer_id=values.get('customer_id') or '',
            trace_id=values.get('trace_id') or values.get('presentment_id'),
            present_date=values.get('present_date') or values.get('date') or '',
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Presentment ingested' if created else 'Presentment already posted',
            'item': item.to_dict(),
            'PosPay': service.snapshot(item.userid or str(values.get('customer_id') or userid), actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_ingest_file(service: PosPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    text = values.get('file') or values.get('text') or values.get('body')
    if not text:
        return jsonify({'message': 'Some data missing', 'error': 'missing_file'}), 400

    def _run():
        items = service.ingest_file(
            actor=userid,
            actor_type=actor_type,
            text=text,
            customer_id=values.get('customer_id') or '',
        )
        owner = str(values.get('customer_id') or (items[0].userid if items else userid))
        return jsonify({
            'message': 'Presentment file ingested',
            'items': [row.to_dict() for row in items],
            'PosPay': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def handle_decide(service: PosPayService, decision: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    item_id = str(values.get('item_id') or '').strip()
    if not item_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_item'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        item = service.decide(
            item_id=item_id,
            actor=userid,
            actor_type=actor_type,
            decision=decision,
            reason=values.get('reason') or '',
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Presentment paid' if decision == DEFAULT_PAY else 'Presentment returned',
            'item': item.to_dict(),
            'PosPay': service.snapshot(item.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _staff_item_route(service: PosPayService, action: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    item_id = str(values.get('item_id') or '').strip()
    if not item_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_item'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        if action == 'assign':
            item = service.assign(
                item_id=item_id,
                actor=userid,
                actor_type=actor_type,
                customer_id=values.get('customer_id') or values.get('owner'),
                account=values.get('account'),
            )
            message = 'Presentment assigned'
        elif action == 'override':
            item = service.override_ofac(
                item_id=item_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'OFAC hold overridden'
        elif action == 'release':
            item = service.release(item_id=item_id, actor=userid, actor_type=actor_type)
            message = 'Presentment released'
        elif action == 'reject':
            item = service.reject(
                item_id=item_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
            )
            message = 'Presentment rejected'
        else:
            raise PosPayError('invalid_status', 'Unknown positive-pay action.')
        return jsonify({
            'message': message,
            'item': item.to_dict(),
            'PosPay': service.snapshot(item.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_export(service: PosPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        body = service.export_decisions(owner_userid=owner, actor=userid, actor_type=actor_type)
        return jsonify({
            'file': body,
            'PosPay': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_run_due(service: PosPayService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    service.run_due(owner)
    return jsonify({'PosPay': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def attach_pospay_routes(app, service: PosPayService) -> None:
    @app.route('/listPosPays', methods=['POST', 'GET'])
    def list_pospays_route():
        return handle_list(service)

    @app.route('/listPosPayIssues', methods=['POST', 'GET'])
    def list_pospay_issues_route():
        return handle_list(service)

    @app.route('/listPosPayExceptions', methods=['POST', 'GET'])
    def list_pospay_exceptions_route():
        return handle_list(service)

    @app.route('/enrollPosPay', methods=['POST', 'GET'])
    def enroll_pospay_route():
        return handle_enroll(service)

    @app.route('/pausePosPay', methods=['POST', 'GET'])
    def pause_pospay_route():
        return _enrollment_status_route(service, ENROLL_PAUSED, 'Positive Pay paused')

    @app.route('/resumePosPay', methods=['POST', 'GET'])
    def resume_pospay_route():
        return _enrollment_status_route(service, ENROLL_ACTIVE, 'Positive Pay resumed')

    @app.route('/archivePosPay', methods=['POST', 'GET'])
    def archive_pospay_route():
        return _enrollment_status_route(service, ENROLL_ARCHIVED, 'Positive Pay archived')

    @app.route('/addPosPayIssue', methods=['POST', 'GET'])
    def add_pospay_issue_route():
        return handle_add_issue(service)

    @app.route('/ingestPosPayIssueFile', methods=['POST', 'GET'])
    def ingest_pospay_issue_file_route():
        return handle_issue_file(service)

    @app.route('/voidPosPayIssue', methods=['POST', 'GET'])
    def void_pospay_issue_route():
        return handle_void_issue(service)

    @app.route('/previewPosPay', methods=['POST', 'GET'])
    def preview_pospay_route():
        return handle_preview(service)

    @app.route('/ingestPosPay', methods=['POST', 'GET'])
    def ingest_pospay_route():
        return handle_ingest(service)

    @app.route('/ingestPosPayFile', methods=['POST', 'GET'])
    def ingest_pospay_file_route():
        return handle_ingest_file(service)

    @app.route('/payPosPay', methods=['POST', 'GET'])
    def pay_pospay_route():
        return handle_decide(service, DEFAULT_PAY)

    @app.route('/returnPosPay', methods=['POST', 'GET'])
    def return_pospay_route():
        return handle_decide(service, DEFAULT_RETURN)

    @app.route('/assignPosPay', methods=['POST', 'GET'])
    def assign_pospay_route():
        return _staff_item_route(service, 'assign')

    @app.route('/overridePosPayOfac', methods=['POST', 'GET'])
    def override_pospay_ofac_route():
        return _staff_item_route(service, 'override')

    @app.route('/releasePosPay', methods=['POST', 'GET'])
    def release_pospay_route():
        return _staff_item_route(service, 'release')

    @app.route('/rejectPosPay', methods=['POST', 'GET'])
    def reject_pospay_route():
        return _staff_item_route(service, 'reject')

    @app.route('/exportPosPayDecision', methods=['POST', 'GET'])
    def export_pospay_route():
        return handle_export(service)

    @app.route('/runDuePosPays', methods=['POST', 'GET'])
    def run_due_pospays_route():
        return handle_run_due(service)
