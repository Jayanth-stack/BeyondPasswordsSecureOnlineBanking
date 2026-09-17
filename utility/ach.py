"""Inbound ACH / direct-deposit allocations.

Customers register payroll sources and split inbound credits across their
accounts. Staff posts an ACH credit; the allocation engine fans it out.
Independent of scheduled outbound transfers (PR #36), overdraft (PR #46),
interest (PR #49), statements (PR #52), spending categories (PR #55),
disputes (PR #58), and 1099-INT (PR #61).

Existing `/depositAmount` (teller cash, approval queue) and
`Customers.credit_request` (100% to one account) stay unchanged until a
source+plan is used via `/postInboundAch`.

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flask import jsonify, request, session

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
SOURCE_ACTIVE = 'active'
SOURCE_PAUSED = 'paused'
SOURCE_ARCHIVED = 'archived'
SOURCE_STATUSES = frozenset({SOURCE_ACTIVE, SOURCE_PAUSED, SOURCE_ARCHIVED})
LEG_PERCENT = 'percent'
LEG_FIXED = 'fixed'
LEG_REMAINDER = 'remainder'
LEG_KINDS = frozenset({LEG_PERCENT, LEG_FIXED, LEG_REMAINDER})
LEG_ALIASES = {
    'pct': LEG_PERCENT,
    '%': LEG_PERCENT,
    'percentage': LEG_PERCENT,
    'share': LEG_PERCENT,
    'dollar': LEG_FIXED,
    'dollars': LEG_FIXED,
    'amount': LEG_FIXED,
    'fixed_amount': LEG_FIXED,
    'rest': LEG_REMAINDER,
    'remaining': LEG_REMAINDER,
    'residual': LEG_REMAINDER,
    'leftover': LEG_REMAINDER,
    'default': LEG_REMAINDER,
}
INBOUND_POSTED = 'posted'
INBOUND_FAILED = 'failed'
INBOUND_PARTIAL = 'partial'
MONEY_QUANTUM = Decimal('0.01')
HUNDRED = Decimal('100')
DEFAULT_STORE_PATH = 'SystemLogs/ach.sqlite'


class AccountError(ValueError):
    pass


class AmountError(ValueError):
    pass


class AchError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def normalize_account(value: Any, *, required: bool = True) -> str:
    if value is None:
        text = ''
    else:
        text = str(value).strip()
    if not text:
        if required:
            raise AccountError('invalid_account')
        return ''
    if text.endswith('.0') and text[:-2].isdigit():
        text = text[:-2]
    if not text.isdigit() or not (1 <= len(text) <= 16):
        raise AccountError('invalid_account')
    return str(int(text))


def parse_money(value: Any, *, allow_zero: bool = False) -> Decimal:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise AmountError('invalid_amount')
    try:
        amount = Decimal(str(value).strip().replace(',', '').replace('$', ''))
    except (InvalidOperation, ValueError):
        raise AmountError('invalid_amount') from None
    if not amount.is_finite():
        raise AmountError('invalid_amount')
    amount = amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    if amount < 0 or (amount == 0 and not allow_zero):
        raise AmountError('invalid_amount')
    return amount


def money_str(value: Decimal) -> str:
    return str(value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN))


def parse_percent(value: Any) -> Decimal:
    if value is None or (isinstance(value, str) and not str(value).strip()):
        raise AchError('invalid_percent', 'Percent is required.')
    text = str(value).strip().replace('%', '').replace(',', '')
    try:
        pct = Decimal(text)
    except (InvalidOperation, ValueError):
        raise AchError('invalid_percent', 'Percent must be numeric.') from None
    if not pct.is_finite():
        raise AchError('invalid_percent', 'Percent must be numeric.')
    pct = pct.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)
    if pct <= 0 or pct > HUNDRED:
        raise AchError('invalid_percent', 'Percent must be between 0.01 and 100.')
    return pct


def normalize_note(value: Any, *, limit: int = 500) -> str:
    return str(value or '').strip()[:limit]


def normalize_source_id(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return uuid.uuid4().hex
    return text[:120]


def normalize_nickname(value: Any) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not (2 <= len(text) <= 40):
        raise AchError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_company_id(value: Any, *, required: bool = False) -> str:
    raw = str(value or '').strip().upper()
    text = ''.join(ch for ch in raw if ch.isalnum() or ch in {'-', '_'})
    if not text:
        if required:
            raise AchError('invalid_company', 'Company id is required.')
        return ''
    if not (2 <= len(text) <= 32):
        raise AchError('invalid_company', 'Company id must be 2-32 characters.')
    return text


def normalize_last4(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        return ''
    if len(digits) < 4:
        raise AchError('invalid_last4', 'Originator last-4 must be four digits.')
    return digits[-4:]


def normalize_leg_kind(value: Any) -> str:
    text = str(value or '').strip().lower().replace(' ', '_').replace('-', '_')
    text = LEG_ALIASES.get(text, text)
    if text not in LEG_KINDS:
        raise AchError('invalid_legs', 'Leg kind must be percent, fixed, or remainder.')
    return text


def own_accounts_from_customer_payload(accounts: Any) -> List[str]:
    if not isinstance(accounts, dict):
        return []
    found = []
    for key in ('savings', 'checkin', 'credit'):
        item = accounts.get(key)
        if isinstance(item, dict) and item.get('Account') not in (None, 'None', ''):
            try:
                found.append(normalize_account(item['Account']))
            except AccountError:
                continue
    return found


def account_types_from_customer_payload(accounts: Any) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    if not isinstance(accounts, dict):
        return mapping
    for key in ('savings', 'checkin', 'credit'):
        item = accounts.get(key)
        if isinstance(item, dict) and item.get('Account') not in (None, 'None', ''):
            try:
                mapping[normalize_account(item['Account'])] = key
            except AccountError:
                continue
    return mapping


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


def _normalize_leg_payload(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise AchError('invalid_legs', 'Each allocation leg must be an object.')
    account = normalize_account(raw.get('account') or raw.get('to') or raw.get('to_account'))
    kind = normalize_leg_kind(raw.get('kind') or raw.get('type') or LEG_PERCENT)
    value = raw.get('value')
    if value is None:
        value = raw.get('percent') if kind == LEG_PERCENT else raw.get('amount')
    return {'account': account, 'kind': kind, 'value': value}


def compute_splits(amount: Any, legs: Sequence[Any]) -> List[Dict[str, str]]:
    """Split an inbound credit across percent / fixed / remainder legs.

    Percents are of the *original* amount. Fixed dollars come off the top.
    Remainder absorbs leftover cents so the slices always sum to `amount`.
    Zero slices are omitted. Duplicate destination accounts are rejected.
    This is the reusable foundation used by preview and posting.
    """
    total = parse_money(amount)
    if not legs:
        raise AchError('invalid_legs', 'At least one allocation leg is required.')
    parsed = [_normalize_leg_payload(leg) for leg in legs]
    seen = set()
    remainder_count = 0
    for leg in parsed:
        if leg['account'] in seen:
            raise AchError('duplicate_account', 'An account can appear in only one leg.')
        seen.add(leg['account'])
        if leg['kind'] == LEG_REMAINDER:
            remainder_count += 1
    if remainder_count > 1:
        raise AchError('invalid_legs', 'Only one remainder leg is allowed.')

    fixed: List[Tuple[str, Decimal]] = []
    percents: List[Tuple[str, Decimal]] = []
    remainder_account: Optional[str] = None
    percent_sum = Decimal('0.00')
    for leg in parsed:
        if leg['kind'] == LEG_FIXED:
            fixed.append((leg['account'], parse_money(leg['value'])))
        elif leg['kind'] == LEG_PERCENT:
            pct = parse_percent(leg['value'])
            percent_sum += pct
            percents.append((leg['account'], pct))
        else:
            remainder_account = leg['account']
    if percent_sum > HUNDRED:
        raise AchError('percent_over_100', 'Percents must not exceed 100.')

    remaining = total
    slices: List[Dict[str, str]] = []

    for account, dollars in fixed:
        take = min(dollars, remaining)
        take = take.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
        if take > remaining:
            take = remaining
        if take > 0:
            slices.append({'account': account, 'amount': money_str(take), 'kind': LEG_FIXED})
            remaining -= take
        if remaining == 0:
            break

    for account, pct in percents:
        if remaining <= 0:
            break
        raw = (total * pct / HUNDRED).quantize(MONEY_QUANTUM, rounding=ROUND_DOWN)
        take = min(raw, remaining)
        if take > 0:
            slices.append({'account': account, 'amount': money_str(take), 'kind': LEG_PERCENT})
            remaining -= take

    remaining = remaining.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    if remaining > 0:
        if remainder_account:
            slices.append({
                'account': remainder_account,
                'amount': money_str(remaining),
                'kind': LEG_REMAINDER,
            })
            remaining = Decimal('0.00')
        elif percent_sum == HUNDRED and slices:
            last = slices[-1]
            bumped = parse_money(last['amount']) + remaining
            last['amount'] = money_str(bumped)
            remaining = Decimal('0.00')
        else:
            raise AchError(
                'allocation_incomplete',
                'Percents and fixed amounts leave a remainder with no remainder account.',
            )
    return [row for row in slices if parse_money(row['amount'], allow_zero=True) > 0]


@dataclass
class AllocationLeg:
    account: str
    kind: str
    value: str

    def to_dict(self) -> Dict[str, str]:
        return {'account': self.account, 'kind': self.kind, 'value': self.value}


@dataclass
class AchSource:
    source_id: str
    userid: str
    nickname: str
    company_id: str
    routing_last4: str
    account_last4: str
    default_account: str
    status: str
    actor: str
    created_at: float
    updated_at: float
    legs: List[AllocationLeg] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'source_id': self.source_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'company_id': self.company_id,
            'routing_last4': self.routing_last4,
            'account_last4': self.account_last4,
            'default_account': self.default_account,
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'legs': [leg.to_dict() for leg in self.legs],
            'active': self.status == SOURCE_ACTIVE,
            'paused': self.status == SOURCE_PAUSED,
            'archived': self.status == SOURCE_ARCHIVED,
        }


@dataclass
class InboundAch:
    inbound_id: str
    trace_id: str
    source_id: str
    userid: str
    amount: str
    company_id: str
    description: str
    status: str
    splits: List[Dict[str, str]]
    actor: str
    created_at: float
    note: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'inbound_id': self.inbound_id,
            'trace_id': self.trace_id,
            'source_id': self.source_id,
            'userid': self.userid,
            'amount': self.amount,
            'company_id': self.company_id,
            'description': self.description,
            'status': self.status,
            'splits': list(self.splits),
            'actor': self.actor,
            'created_at': self.created_at,
            'note': self.note,
            'posted': self.status == INBOUND_POSTED,
        }


@dataclass(frozen=True)
class AchPolicy:
    enabled: bool = True
    customer_manage: bool = True
    allow_credit: bool = True
    max_sources: int = 8
    max_legs: int = 6
    max_inbounds: int = 200
    min_amount: Decimal = Decimal('1.00')
    max_amount: Decimal = Decimal('100000.00')

    @classmethod
    def from_env(cls) -> 'AchPolicy':
        return cls(
            enabled=_env_bool('ACH_ENABLED', True),
            customer_manage=_env_bool('ACH_CUSTOMER_MANAGE', True),
            allow_credit=_env_bool('ACH_ALLOW_CREDIT', True),
            max_sources=max(1, _env_int('ACH_MAX_SOURCES', 8)),
            max_legs=max(1, _env_int('ACH_MAX_LEGS', 6)),
            max_inbounds=max(1, _env_int('ACH_MAX_INBOUNDS', 200)),
            min_amount=_env_money('ACH_MIN_AMOUNT', '1.00'),
            max_amount=_env_money('ACH_MAX_AMOUNT', '100000.00'),
        )


class MemoryAchStore:
    def __init__(self) -> None:
        self._sources: Dict[str, AchSource] = {}
        self._inbounds: Dict[str, InboundAch] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_source(self, source: AchSource) -> None:
        with self._lock:
            self._sources[source.source_id] = source

    def get_source(self, source_id: str) -> Optional[AchSource]:
        with self._lock:
            source = self._sources.get(source_id)
            if source is None:
                return None
            return _clone_source(source)

    def update_source(self, source: AchSource) -> None:
        with self._lock:
            self._sources[source.source_id] = source

    def list_sources(
        self,
        userid: Optional[str] = None,
        *,
        include_archived: bool = True,
    ) -> List[AchSource]:
        with self._lock:
            rows = [_clone_source(row) for row in self._sources.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_archived:
            rows = [row for row in rows if row.status != SOURCE_ARCHIVED]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows

    def find_by_nickname(self, userid: str, nickname: str) -> Optional[AchSource]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._sources.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status != SOURCE_ARCHIVED:
                    return _clone_source(row)
        return None

    def find_by_company(self, userid: str, company_id: str) -> Optional[AchSource]:
        if not company_id:
            return None
        wanted = company_id.strip().upper()
        with self._lock:
            for row in self._sources.values():
                if row.userid == userid and row.company_id == wanted and row.status != SOURCE_ARCHIVED:
                    return _clone_source(row)
        return None

    def put_inbound(self, inbound: InboundAch) -> InboundAch:
        with self._lock:
            existing_id = self._by_trace.get(inbound.trace_id)
            if existing_id is not None:
                return self._inbounds[existing_id]
            self._inbounds[inbound.inbound_id] = inbound
            self._by_trace[inbound.trace_id] = inbound.inbound_id
            return inbound

    def get_inbound(self, inbound_id: str) -> Optional[InboundAch]:
        with self._lock:
            return self._inbounds.get(inbound_id)

    def get_inbound_by_trace(self, trace_id: str) -> Optional[InboundAch]:
        with self._lock:
            inbound_id = self._by_trace.get(trace_id)
            if not inbound_id:
                return None
            return self._inbounds.get(inbound_id)

    def list_inbounds(
        self,
        userid: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> List[InboundAch]:
        with self._lock:
            rows = list(self._inbounds.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if source_id is not None:
            rows = [row for row in rows if row.source_id == source_id]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows


class SqliteAchStore:
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
                    company_id TEXT NOT NULL DEFAULT '',
                    routing_last4 TEXT NOT NULL DEFAULT '',
                    account_last4 TEXT NOT NULL DEFAULT '',
                    default_account TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    legs_json TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inbounds (
                    inbound_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    source_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    company_id TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    splits_json TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_source(self, source: AchSource) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sources (
                    source_id, userid, nickname, company_id, routing_last4,
                    account_last4, default_account, status, actor,
                    created_at, updated_at, legs_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _source_row(source),
            )
            conn.commit()

    def get_source(self, source_id: str) -> Optional[AchSource]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM sources WHERE source_id = ?', (source_id,)
            ).fetchone()
        return _source_from_row(row) if row else None

    def update_source(self, source: AchSource) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE sources SET
                    nickname=?, company_id=?, routing_last4=?, account_last4=?,
                    default_account=?, status=?, actor=?, updated_at=?, legs_json=?
                WHERE source_id=?
                """,
                (
                    source.nickname, source.company_id, source.routing_last4,
                    source.account_last4, source.default_account, source.status,
                    source.actor, source.updated_at, json.dumps([leg.to_dict() for leg in source.legs]),
                    source.source_id,
                ),
            )
            conn.commit()

    def list_sources(
        self,
        userid: Optional[str] = None,
        *,
        include_archived: bool = True,
    ) -> List[AchSource]:
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

    def find_by_nickname(self, userid: str, nickname: str) -> Optional[AchSource]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM sources
                WHERE userid = ? AND lower(nickname) = lower(?) AND status != 'archived'
                """,
                (userid, nickname),
            ).fetchone()
        return _source_from_row(row) if row else None

    def find_by_company(self, userid: str, company_id: str) -> Optional[AchSource]:
        if not company_id:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM sources
                WHERE userid = ? AND company_id = ? AND status != 'archived'
                """,
                (userid, company_id),
            ).fetchone()
        return _source_from_row(row) if row else None

    def put_inbound(self, inbound: InboundAch) -> InboundAch:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM inbounds WHERE trace_id = ?', (inbound.trace_id,)
            ).fetchone()
            if existing is not None:
                return _inbound_from_row(existing)
            conn.execute(
                """
                INSERT INTO inbounds (
                    inbound_id, trace_id, source_id, userid, amount, company_id,
                    description, status, splits_json, actor, created_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _inbound_row(inbound),
            )
            conn.commit()
            return inbound

    def get_inbound(self, inbound_id: str) -> Optional[InboundAch]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM inbounds WHERE inbound_id = ?', (inbound_id,)
            ).fetchone()
        return _inbound_from_row(row) if row else None

    def get_inbound_by_trace(self, trace_id: str) -> Optional[InboundAch]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM inbounds WHERE trace_id = ?', (trace_id,)
            ).fetchone()
        return _inbound_from_row(row) if row else None

    def list_inbounds(
        self,
        userid: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> List[InboundAch]:
        sql = 'SELECT * FROM inbounds'
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
        return [_inbound_from_row(row) for row in rows]


def _clone_source(source: AchSource) -> AchSource:
    return AchSource(
        source_id=source.source_id,
        userid=source.userid,
        nickname=source.nickname,
        company_id=source.company_id,
        routing_last4=source.routing_last4,
        account_last4=source.account_last4,
        default_account=source.default_account,
        status=source.status,
        actor=source.actor,
        created_at=source.created_at,
        updated_at=source.updated_at,
        legs=[AllocationLeg(leg.account, leg.kind, leg.value) for leg in source.legs],
    )


def _legs_from_json(raw: Any) -> List[AllocationLeg]:
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = []
    else:
        payload = raw or []
    legs = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        legs.append(AllocationLeg(
            account=str(item.get('account') or ''),
            kind=str(item.get('kind') or ''),
            value=str(item.get('value') or ''),
        ))
    return legs


def _source_row(source: AchSource) -> Tuple[Any, ...]:
    return (
        source.source_id, source.userid, source.nickname, source.company_id,
        source.routing_last4, source.account_last4, source.default_account,
        source.status, source.actor, source.created_at, source.updated_at,
        json.dumps([leg.to_dict() for leg in source.legs]),
    )


def _source_from_row(row: sqlite3.Row) -> AchSource:
    return AchSource(
        source_id=row['source_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        company_id=row['company_id'],
        routing_last4=row['routing_last4'],
        account_last4=row['account_last4'],
        default_account=row['default_account'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        legs=_legs_from_json(row['legs_json']),
    )


def _inbound_row(inbound: InboundAch) -> Tuple[Any, ...]:
    return (
        inbound.inbound_id, inbound.trace_id, inbound.source_id, inbound.userid,
        inbound.amount, inbound.company_id, inbound.description, inbound.status,
        json.dumps(inbound.splits), inbound.actor, inbound.created_at, inbound.note,
    )


def _inbound_from_row(row: sqlite3.Row) -> InboundAch:
    try:
        splits = json.loads(row['splits_json'])
    except (TypeError, json.JSONDecodeError):
        splits = []
    return InboundAch(
        inbound_id=row['inbound_id'],
        trace_id=row['trace_id'],
        source_id=row['source_id'],
        userid=row['userid'],
        amount=row['amount'],
        company_id=row['company_id'],
        description=row['description'],
        status=row['status'],
        splits=list(splits),
        actor=row['actor'],
        created_at=float(row['created_at']),
        note=row['note'] or '',
    )


class AchService:
    def __init__(
        self,
        policy: Optional[AchPolicy] = None,
        store: Any = None,
        *,
        clock: Optional[Callable[[], float]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.policy = policy or AchPolicy()
        self.store = store or MemoryAchStore()
        self.clock = clock or time.time
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise AchError('ach_disabled', 'Direct deposit allocations are disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise AchError('ach_forbidden', 'Customers cannot manage ACH sources.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise AchError('ach_forbidden', 'Only staff can post inbound ACH.')

    def _owned_accounts(self, userid: str) -> List[str]:
        if self.accounts_fn is None:
            return []
        try:
            payload = self.accounts_fn(userid)
        except Exception:
            return []
        return own_accounts_from_customer_payload(payload)

    def _account_types(self, userid: str) -> Dict[str, str]:
        if self.accounts_fn is None:
            return {}
        try:
            payload = self.accounts_fn(userid)
        except Exception:
            return {}
        return account_types_from_customer_payload(payload)

    def _assert_account_allowed(self, userid: str, account: str) -> None:
        owned = self._owned_accounts(userid)
        if owned and account not in owned:
            raise AchError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        if types.get(account) == 'credit' and not self.policy.allow_credit:
            raise AchError('credit_not_allowed', 'Credit accounts cannot receive ACH allocations.')

    def _effective_legs(self, source: AchSource) -> List[Dict[str, Any]]:
        if source.legs:
            return [leg.to_dict() for leg in source.legs]
        return [{
            'account': source.default_account,
            'kind': LEG_REMAINDER,
            'value': '',
        }]

    def preview(
        self,
        amount: Any,
        legs: Optional[Sequence[Any]] = None,
        *,
        source: Optional[AchSource] = None,
    ) -> List[Dict[str, str]]:
        self._require_enabled()
        if legs is None:
            if source is None:
                raise AchError('invalid_legs', 'Legs or a source are required to preview.')
            legs = self._effective_legs(source)
        return compute_splits(amount, legs)

    def add_source(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        default_account: Any,
        company_id: Any = '',
        routing_last4: Any = '',
        account_last4: Any = '',
        legs: Optional[Sequence[Any]] = None,
    ) -> AchSource:
        self._require_manage(actor_type)
        nick = normalize_nickname(nickname)
        company = normalize_company_id(company_id)
        default = normalize_account(default_account)
        self._assert_account_allowed(owner_userid, default)
        if self.store.find_by_nickname(owner_userid, nick):
            raise AchError('source_duplicate', 'A source with this nickname already exists.')
        if company and self.store.find_by_company(owner_userid, company):
            raise AchError('source_duplicate', 'A source with this company id already exists.')
        existing = [
            row for row in self.store.list_sources(owner_userid)
            if row.status != SOURCE_ARCHIVED
        ]
        if len(existing) >= self.policy.max_sources:
            raise AchError('source_limit', 'Source limit reached.')
        parsed_legs = self._parse_legs(owner_userid, legs, default) if legs else []
        now = float(self.clock())
        source = AchSource(
            source_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=nick,
            company_id=company,
            routing_last4=normalize_last4(routing_last4),
            account_last4=normalize_last4(account_last4),
            default_account=default,
            status=SOURCE_ACTIVE,
            actor=str(actor),
            created_at=now,
            updated_at=now,
            legs=parsed_legs,
        )
        self.store.put_source(source)
        return source

    def _parse_legs(
        self,
        userid: str,
        legs: Optional[Sequence[Any]],
        default_account: str,
    ) -> List[AllocationLeg]:
        payload = list(legs or [])
        if len(payload) > self.policy.max_legs:
            raise AchError('allocation_limit', 'Too many allocation legs.')
        parsed = [_normalize_leg_payload(leg) for leg in payload]
        has_remainder = any(leg['kind'] == LEG_REMAINDER for leg in parsed)
        used = {leg['account'] for leg in parsed}
        if not has_remainder and default_account not in used:
            parsed.append({'account': default_account, 'kind': LEG_REMAINDER, 'value': ''})
        if not parsed:
            parsed.append({'account': default_account, 'kind': LEG_REMAINDER, 'value': ''})
        # Validate by running a dummy $100 split (catches percent>100, dupes, incomplete).
        compute_splits('100.00', parsed)
        out = []
        for leg in parsed:
            self._assert_account_allowed(userid, leg['account'])
            if leg['kind'] == LEG_PERCENT:
                value = str(parse_percent(leg['value']))
            elif leg['kind'] == LEG_FIXED:
                value = money_str(parse_money(leg['value']))
            else:
                value = ''
            out.append(AllocationLeg(account=leg['account'], kind=leg['kind'], value=value))
        return out

    def get_source(
        self,
        *,
        source_id: str,
        actor: str,
        actor_type: str,
    ) -> AchSource:
        self._require_enabled()
        source = self.store.get_source(source_id)
        if source is None:
            raise AchError('source_not_found', 'ACH source not found.')
        if actor_type not in EMPLOYEE_ROLES and source.userid != actor:
            raise AchError('ach_forbidden', 'Not allowed to view this source.')
        return source

    def update_source(
        self,
        *,
        source_id: str,
        actor: str,
        actor_type: str,
        nickname: Any = None,
        company_id: Any = None,
        routing_last4: Any = None,
        account_last4: Any = None,
        default_account: Any = None,
    ) -> AchSource:
        source = self.get_source(source_id=source_id, actor=actor, actor_type=actor_type)
        self._require_manage(actor_type)
        if source.status == SOURCE_ARCHIVED:
            raise AchError('already_archived', 'Archived sources cannot be updated.')
        if nickname is not None and str(nickname).strip():
            nick = normalize_nickname(nickname)
            other = self.store.find_by_nickname(source.userid, nick)
            if other is not None and other.source_id != source.source_id:
                raise AchError('source_duplicate', 'A source with this nickname already exists.')
            source.nickname = nick
        if company_id is not None:
            company = normalize_company_id(company_id)
            if company:
                other = self.store.find_by_company(source.userid, company)
                if other is not None and other.source_id != source.source_id:
                    raise AchError('source_duplicate', 'A source with this company id already exists.')
            source.company_id = company
        if routing_last4 is not None:
            source.routing_last4 = normalize_last4(routing_last4)
        if account_last4 is not None:
            source.account_last4 = normalize_last4(account_last4)
        if default_account is not None and str(default_account).strip():
            default = normalize_account(default_account)
            self._assert_account_allowed(source.userid, default)
            source.default_account = default
        source.actor = str(actor)
        source.updated_at = float(self.clock())
        self.store.update_source(source)
        return source

    def set_allocation(
        self,
        *,
        source_id: str,
        actor: str,
        actor_type: str,
        legs: Sequence[Any],
        default_account: Any = None,
    ) -> AchSource:
        source = self.get_source(source_id=source_id, actor=actor, actor_type=actor_type)
        self._require_manage(actor_type)
        if source.status == SOURCE_ARCHIVED:
            raise AchError('already_archived', 'Archived sources cannot change allocations.')
        if default_account is not None and str(default_account).strip():
            source.default_account = normalize_account(default_account)
            self._assert_account_allowed(source.userid, source.default_account)
        source.legs = self._parse_legs(source.userid, legs, source.default_account)
        source.actor = str(actor)
        source.updated_at = float(self.clock())
        self.store.update_source(source)
        return source

    def set_status(
        self,
        *,
        source_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> AchSource:
        source = self.get_source(source_id=source_id, actor=actor, actor_type=actor_type)
        self._require_manage(actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': SOURCE_PAUSED,
            'hold': SOURCE_PAUSED,
            'resume': SOURCE_ACTIVE,
            'activate': SOURCE_ACTIVE,
            'unpause': SOURCE_ACTIVE,
            'archive': SOURCE_ARCHIVED,
            'remove': SOURCE_ARCHIVED,
            'delete': SOURCE_ARCHIVED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in SOURCE_STATUSES:
            raise AchError('invalid_status', 'Status must be active, paused, or archived.')
        if source.status == SOURCE_ARCHIVED and wanted != SOURCE_ARCHIVED:
            raise AchError('already_archived', 'Archived sources cannot be reopened.')
        if source.status == wanted:
            if wanted == SOURCE_PAUSED:
                raise AchError('already_paused', 'Source is already paused.')
            if wanted == SOURCE_ACTIVE:
                raise AchError('already_active', 'Source is already active.')
            raise AchError('already_archived', 'Source is already archived.')
        source.status = wanted
        source.actor = str(actor)
        source.updated_at = float(self.clock())
        self.store.update_source(source)
        return source

    def resolve_source(
        self,
        *,
        owner_userid: str,
        source_id: Any = None,
        company_id: Any = None,
    ) -> AchSource:
        sid = str(source_id or '').strip()
        if sid:
            source = self.store.get_source(sid)
            if source is None or source.userid != owner_userid:
                raise AchError('source_not_found', 'ACH source not found.')
            return source
        company = normalize_company_id(company_id)
        if company:
            source = self.store.find_by_company(owner_userid, company)
            if source is None:
                raise AchError('source_not_found', 'ACH source not found.')
            return source
        raise AchError('missing_source', 'source_id or company_id is required.')

    def post_inbound(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        amount: Any,
        source_id: Any = None,
        company_id: Any = None,
        trace_id: Any = None,
        description: Any = '',
        force: bool = False,
    ) -> Tuple[InboundAch, bool]:
        self._require_staff(actor_type)
        dollars = parse_money(amount)
        if dollars < self.policy.min_amount or dollars > self.policy.max_amount:
            raise AchError('amount_out_of_range', 'Inbound amount is outside policy range.')
        source = self.resolve_source(
            owner_userid=owner_userid, source_id=source_id, company_id=company_id,
        )
        if source.status == SOURCE_ARCHIVED:
            raise AchError('already_archived', 'Cannot post to an archived source.')
        if source.status == SOURCE_PAUSED and not force:
            raise AchError('source_paused', 'Source is paused.')
        existing_count = len(self.store.list_inbounds(owner_userid))
        trace = normalize_source_id(trace_id)
        existing = self.store.get_inbound_by_trace(trace)
        if existing is not None:
            return existing, False
        if existing_count >= self.policy.max_inbounds:
            raise AchError('inbound_limit', 'Inbound history limit reached.')
        splits = self.preview(dollars, source=source)
        for split in splits:
            self._assert_account_allowed(owner_userid, split['account'])
        remark = normalize_note(description) or ('payroll from %s' % source.nickname)
        status = INBOUND_POSTED
        note = ''
        credited = []
        for split in splits:
            if self.credit_fn is None:
                credited.append({**split, 'result': 'recorded'})
                continue
            try:
                result = self.credit_fn(split['account'], split['amount'], remark)
            except Exception as exc:
                status = INBOUND_PARTIAL if credited else INBOUND_FAILED
                note = str(exc)[:240]
                break
            ok = result in (None, True, 1, 'Success', 'success', 'Amount Credited')
            if isinstance(result, str) and result.lower() in {'success', 'done', 'ok'}:
                ok = True
            if not ok:
                status = INBOUND_PARTIAL if credited else INBOUND_FAILED
                note = str(result)[:240]
                break
            credited.append({**split, 'result': 'credited'})
        inbound = InboundAch(
            inbound_id=uuid.uuid4().hex,
            trace_id=trace,
            source_id=source.source_id,
            userid=owner_userid,
            amount=money_str(dollars),
            company_id=source.company_id,
            description=remark,
            status=status,
            splits=splits,
            actor=str(actor),
            created_at=float(self.clock()),
            note=note,
        )
        stored = self.store.put_inbound(inbound)
        if stored.inbound_id != inbound.inbound_id:
            return stored, False
        if status != INBOUND_POSTED:
            raise AchError(status, 'Inbound ACH credit did not complete.', inbound=stored)
        return stored, True

    def snapshot(self, userid: str) -> Dict[str, Any]:
        sources = self.store.list_sources(userid)
        inbounds = self.store.list_inbounds(userid)
        ytd = Decimal('0.00')
        for row in inbounds:
            if row.status == INBOUND_POSTED:
                try:
                    ytd += parse_money(row.amount, allow_zero=True)
                except AmountError:
                    continue
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'max_sources': self.policy.max_sources,
            'max_legs': self.policy.max_legs,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'sources': [row.to_dict() for row in sources],
            'inbounds': [row.to_dict() for row in inbounds[:40]],
            'ytd': money_str(ytd),
            'active_sources': sum(1 for row in sources if row.status == SOURCE_ACTIVE),
        }


_SERVICE: Optional[AchService] = None


def set_service(service: Optional[AchService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[AchService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('ACH_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryAchStore()
    path = os.environ.get('ACH_DB', DEFAULT_STORE_PATH)
    return SqliteAchStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
) -> AchService:
    if store is None:
        store = default_store()
    return AchService(
        AchPolicy.from_env(),
        store,
        clock=clock,
        credit_fn=credit_fn,
        accounts_fn=accounts_fn,
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
        'allocation_limit': 409,
        'inbound_limit': 409,
        'inbound_duplicate': 409,
        'already_archived': 409,
        'already_paused': 409,
        'already_active': 409,
        'posted': 409,
        'failed': 409,
        'partial': 409,
        'ach_forbidden': 403,
        'ach_disabled': 403,
        'source_paused': 403,
        'credit_not_allowed': 403,
        'source_not_found': 404,
        'inbound_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_percent': 400,
        'invalid_nickname': 400,
        'invalid_company': 400,
        'invalid_last4': 400,
        'invalid_legs': 400,
        'invalid_status': 400,
        'allocation_incomplete': 400,
        'percent_over_100': 400,
        'duplicate_account': 400,
        'amount_out_of_range': 400,
        'missing_customer_id': 400,
        'missing_source': 400,
    }.get(code, 400)


def _error_body(exc: AchError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('inbound') is not None:
        body['inbound'] = exc.extra['inbound'].to_dict()
    if exc.extra.get('source') is not None:
        body['source'] = exc.extra['source'].to_dict()
    if exc.extra.get('splits') is not None:
        body['splits'] = exc.extra['splits']
    return body


def handle_list_ach_sources(service: AchService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'DirectDeposit': service.snapshot(owner)}), 200


def handle_add_ach_source(service: AchService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    try:
        source = service.add_source(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname') or values.get('name'),
            default_account=values.get('default_account') or values.get('account'),
            company_id=values.get('company_id') or values.get('company'),
            routing_last4=values.get('routing_last4'),
            account_last4=values.get('account_last4'),
            legs=values.get('legs'),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except AchError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'ACH source added',
        'source': source.to_dict(),
        'DirectDeposit': service.snapshot(owner),
    }), 201


def handle_update_ach_source(service: AchService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    source_id = str(values.get('source_id') or '').strip()
    if not source_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_source'}), 400
    company = None
    if 'company_id' in values or 'company' in values:
        company = values.get('company_id') if 'company_id' in values else values.get('company')
    try:
        source = service.update_source(
            source_id=source_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
            nickname=values.get('nickname'),
            company_id=company,
            routing_last4=values.get('routing_last4'),
            account_last4=values.get('account_last4'),
            default_account=values.get('default_account') or values.get('account'),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AchError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'ACH source updated',
        'source': source.to_dict(),
        'DirectDeposit': service.snapshot(source.userid),
    }), 200


def handle_set_ach_allocation(service: AchService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    source_id = str(values.get('source_id') or '').strip()
    if not source_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_source'}), 400
    try:
        source = service.set_allocation(
            source_id=source_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
            legs=values.get('legs') or [],
            default_account=values.get('default_account'),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except AchError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Allocation saved',
        'source': source.to_dict(),
        'DirectDeposit': service.snapshot(source.userid),
    }), 200


def handle_pause_ach_source(service: AchService):
    return _status_route(service, SOURCE_PAUSED, 'ACH source paused')


def handle_resume_ach_source(service: AchService):
    return _status_route(service, SOURCE_ACTIVE, 'ACH source resumed')


def handle_archive_ach_source(service: AchService):
    return _status_route(service, SOURCE_ARCHIVED, 'ACH source archived')


def _status_route(service: AchService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    source_id = str(values.get('source_id') or '').strip()
    if not source_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_source'}), 400
    try:
        source = service.set_status(
            source_id=source_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
            status=status,
        )
    except AchError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': ok_message,
        'source': source.to_dict(),
        'DirectDeposit': service.snapshot(source.userid),
    }), 200


def handle_preview_ach_allocation(service: AchService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values) or userid
    try:
        source = None
        legs = values.get('legs')
        if values.get('source_id') or values.get('company_id'):
            source = service.resolve_source(
                owner_userid=owner,
                source_id=values.get('source_id'),
                company_id=values.get('company_id'),
            )
            if actor_type not in EMPLOYEE_ROLES and source.userid != userid:
                raise AchError('ach_forbidden', 'Not allowed to preview this source.')
        splits = service.preview(
            values.get('amount'),
            legs=legs,
            source=source,
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except AchError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'splits': splits,
        'amount': money_str(parse_money(values.get('amount'))),
        'DirectDeposit': service.snapshot(owner),
    }), 200


def handle_post_inbound_ach(service: AchService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    try:
        inbound, created = service.post_inbound(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            amount=values.get('amount'),
            source_id=values.get('source_id'),
            company_id=values.get('company_id') or values.get('company'),
            trace_id=values.get('trace_id') or values.get('source_trace'),
            description=values.get('description') or values.get('note') or '',
            force=bool(values.get('force')),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except AchError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Inbound ACH posted' if created else 'Inbound ACH already posted',
        'inbound': inbound.to_dict(),
        'DirectDeposit': service.snapshot(owner),
    }), 201 if created else 200


def handle_list_inbound_ach(service: AchService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    snapshot = service.snapshot(owner)
    return jsonify({'inbounds': snapshot['inbounds'], 'DirectDeposit': snapshot}), 200


def attach_ach_routes(app, service: AchService) -> None:
    @app.route('/listAchSources', methods=['POST', 'GET'])
    def list_ach_sources_route():
        return handle_list_ach_sources(service)

    @app.route('/addAchSource', methods=['POST', 'GET'])
    def add_ach_source_route():
        return handle_add_ach_source(service)

    @app.route('/updateAchSource', methods=['POST', 'GET'])
    def update_ach_source_route():
        return handle_update_ach_source(service)

    @app.route('/setAchAllocation', methods=['POST', 'GET'])
    def set_ach_allocation_route():
        return handle_set_ach_allocation(service)

    @app.route('/pauseAchSource', methods=['POST', 'GET'])
    def pause_ach_source_route():
        return handle_pause_ach_source(service)

    @app.route('/resumeAchSource', methods=['POST', 'GET'])
    def resume_ach_source_route():
        return handle_resume_ach_source(service)

    @app.route('/archiveAchSource', methods=['POST', 'GET'])
    def archive_ach_source_route():
        return handle_archive_ach_source(service)

    @app.route('/previewAchAllocation', methods=['POST', 'GET'])
    def preview_ach_allocation_route():
        return handle_preview_ach_allocation(service)

    @app.route('/postInboundAch', methods=['POST', 'GET'])
    def post_inbound_ach_route():
        return handle_post_inbound_ach(service)

    @app.route('/listInboundAch', methods=['POST', 'GET'])
    def list_inbound_ach_route():
        return handle_list_inbound_ach(service)
