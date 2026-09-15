"""Transaction disputes and provisional credit.

Customers (and staff on lookup) can challenge a posted debit. Staff
investigates, may grant a temporary credit, then uphold (credit stays)
or deny (credit is clawed back). Independent of spending categories
(PR #55, uncategorized tags), period statements (PR #52), and the
HTML `transaction_history` blob.

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from flask import jsonify, request, session

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
DISPUTABLE_KINDS = frozenset({'transfer_out', 'withdraw', 'cheque'})
CREDIT_KINDS = frozenset({'transfer_in', 'deposit', 'credit', 'open', 'provisional', 'provisional_clawback'})
KNOWN_KINDS = DISPUTABLE_KINDS | CREDIT_KINDS | frozenset({'transfer'})
DEBIT_DIRECTIONS = frozenset({'debit', 'out'})
CREDIT_DIRECTIONS = frozenset({'credit', 'in'})
REASONS = frozenset({'unauthorized', 'duplicate', 'incorrect_amount', 'goods_not_received', 'other'})
OPEN_STATUSES = frozenset({'open', 'investigating', 'provisionally_credited'})
TERMINAL_STATUSES = frozenset({'upheld', 'denied', 'withdrawn'})
ALL_STATUSES = OPEN_STATUSES | TERMINAL_STATUSES
DECISIONS = frozenset({'uphold', 'deny'})
CREDIT_STATUSES = frozenset({'none', 'provisional', 'final', 'clawed', 'clawback_failed'})
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/dispute.sqlite'

CreditFn = Callable[[str, str, str], bool]
DebitFn = Callable[[str, str, str], bool]


class AccountError(ValueError):
    pass


class AmountError(ValueError):
    pass


class DisputeError(ValueError):
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


def normalize_reason(value: Any, *, default: str = 'unauthorized') -> str:
    text = str(value or default).strip().lower().replace(' ', '_') or default
    aliases = {
        'fraud': 'unauthorized',
        'unauth': 'unauthorized',
        'dup': 'duplicate',
        'wrong_amount': 'incorrect_amount',
        'amount': 'incorrect_amount',
        'not_received': 'goods_not_received',
        'merchandise': 'goods_not_received',
    }
    text = aliases.get(text, text)
    if text not in REASONS:
        raise DisputeError('invalid_reason', 'Unknown dispute reason.')
    return text


def normalize_kind(value: Any, *, direction: Optional[str] = None) -> str:
    text = str(value or '').strip().lower().replace('-', '_').replace(' ', '_')
    aliases = {
        'transferout': 'transfer_out',
        'xfer_out': 'transfer_out',
        'debit': 'withdraw',
        'withdrawal': 'withdraw',
        'cashier_cheque': 'cheque',
        'cashier_check': 'cheque',
        'check': 'cheque',
        'transferin': 'transfer_in',
        'xfer_in': 'transfer_in',
        'direct_deposit': 'credit',
        'bonus': 'open',
    }
    text = aliases.get(text, text)
    if text == 'transfer':
        if direction in DEBIT_DIRECTIONS:
            text = 'transfer_out'
        elif direction in CREDIT_DIRECTIONS:
            text = 'transfer_in'
    if text not in KNOWN_KINDS:
        raise DisputeError('invalid_kind', 'Unknown movement kind.')
    return text


def normalize_direction(value: Any, *, kind: Optional[str] = None) -> str:
    text = str(value or '').strip().lower()
    if text in DEBIT_DIRECTIONS:
        return 'debit'
    if text in CREDIT_DIRECTIONS:
        return 'credit'
    if kind in DISPUTABLE_KINDS:
        return 'debit'
    if kind in CREDIT_KINDS:
        return 'credit'
    raise DisputeError('invalid_kind', 'Movement direction is required.')


def normalize_decision(value: Any) -> str:
    text = str(value or '').strip().lower()
    aliases = {
        'approve': 'uphold',
        'grant': 'uphold',
        'accept': 'uphold',
        'reject': 'deny',
        'decline': 'deny',
        'refuse': 'deny',
    }
    text = aliases.get(text, text)
    if text not in DECISIONS:
        raise DisputeError('invalid_decision', 'Decision must be uphold or deny.')
    return text


def normalize_note(value: Any, *, limit: int = 500) -> str:
    return str(value or '').strip()[:limit]


def normalize_source_id(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return uuid.uuid4().hex
    return text[:120]


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


def _own_account_set(own_accounts: Optional[Iterable[Any]]) -> set:
    found = set()
    for item in own_accounts or ():
        try:
            found.add(normalize_account(item))
        except AccountError:
            continue
    return found


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


@dataclass
class Movement:
    source_id: str
    userid: str
    account: str
    amount: str
    kind: str
    direction: str
    counterparty: str
    description: str
    created_at: float
    disputed: bool = False
    dispute_id: Optional[str] = None

    @property
    def disputable(self) -> bool:
        return self.direction == 'debit' and self.kind in DISPUTABLE_KINDS

    def to_dict(self) -> Dict[str, Any]:
        return {
            'source_id': self.source_id,
            'userid': self.userid,
            'account': self.account,
            'amount': self.amount,
            'kind': self.kind,
            'direction': self.direction,
            'counterparty': self.counterparty,
            'description': self.description,
            'created_at': self.created_at,
            'disputable': self.disputable,
            'disputed': self.disputed,
            'dispute_id': self.dispute_id,
        }


@dataclass
class Dispute:
    dispute_id: str
    userid: str
    account: str
    source_id: str
    amount: str
    claimed_amount: str
    kind: str
    reason: str
    evidence: str
    status: str
    actor: str
    actor_type: str
    created_at: float
    updated_at: float
    investigator: Optional[str] = None
    investigated_at: Optional[float] = None
    decision: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[float] = None
    decision_note: str = ''
    credit_status: str = 'none'
    credited_amount: str = '0.00'
    credited_at: Optional[float] = None
    credited_by: Optional[str] = None
    clawback_at: Optional[float] = None
    clawback_by: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'dispute_id': self.dispute_id,
            'userid': self.userid,
            'account': self.account,
            'source_id': self.source_id,
            'amount': self.amount,
            'claimed_amount': self.claimed_amount,
            'kind': self.kind,
            'reason': self.reason,
            'evidence': self.evidence,
            'status': self.status,
            'actor': self.actor,
            'actor_type': self.actor_type,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'investigator': self.investigator,
            'investigated_at': self.investigated_at,
            'decision': self.decision,
            'decided_by': self.decided_by,
            'decided_at': self.decided_at,
            'decision_note': self.decision_note,
            'credit_status': self.credit_status,
            'credited_amount': self.credited_amount,
            'credited_at': self.credited_at,
            'credited_by': self.credited_by,
            'clawback_at': self.clawback_at,
            'clawback_by': self.clawback_by,
            'open': self.status in OPEN_STATUSES,
        }


@dataclass(frozen=True)
class DisputePolicy:
    enabled: bool = True
    customer_file: bool = True
    customer_withdraw: bool = True
    window_seconds: int = 90 * 24 * 3600
    max_open: int = 8
    max_evidence: int = 500
    min_amount: Decimal = Decimal('0.01')
    max_amount: Decimal = Decimal('25000.00')
    credit_kinds_disputable: bool = False
    auto_investigate: bool = True
    operations: frozenset = field(default_factory=lambda: frozenset(DISPUTABLE_KINDS))

    @classmethod
    def from_env(cls) -> 'DisputePolicy':
        operations = os.environ.get('DISPUTE_OPERATIONS', 'transfer_out,withdraw,cheque')
        parsed = frozenset(part.strip() for part in operations.split(',') if part.strip())
        return cls(
            enabled=_env_bool('DISPUTE_ENABLED', True),
            customer_file=_env_bool('DISPUTE_CUSTOMER_FILE', True),
            customer_withdraw=_env_bool('DISPUTE_CUSTOMER_WITHDRAW', True),
            window_seconds=max(3600, _env_int('DISPUTE_WINDOW_SECONDS', 90 * 24 * 3600)),
            max_open=max(1, _env_int('DISPUTE_MAX_OPEN', 8)),
            max_evidence=max(40, _env_int('DISPUTE_MAX_EVIDENCE', 500)),
            min_amount=_env_money('DISPUTE_MIN_AMOUNT', '0.01'),
            max_amount=_env_money('DISPUTE_MAX_AMOUNT', '25000'),
            credit_kinds_disputable=_env_bool('DISPUTE_CREDIT_KINDS', False),
            auto_investigate=_env_bool('DISPUTE_AUTO_INVESTIGATE', True),
            operations=parsed or frozenset(DISPUTABLE_KINDS),
        )


def _movement_from_parts(
    *,
    source_id: str,
    userid: str,
    account: str,
    amount: str,
    kind: str,
    direction: str,
    counterparty: str,
    description: str,
    created_at: float,
    disputed: bool = False,
    dispute_id: Optional[str] = None,
) -> Movement:
    return Movement(
        source_id=source_id,
        userid=userid,
        account=account,
        amount=amount,
        kind=kind,
        direction=direction,
        counterparty=counterparty,
        description=description,
        created_at=created_at,
        disputed=disputed,
        dispute_id=dispute_id,
    )


class MemoryDisputeStore:
    def __init__(self) -> None:
        self._movements: Dict[str, Movement] = {}
        self._disputes: Dict[str, Dispute] = {}
        self._lock = threading.Lock()

    def put_movement(self, movement: Movement) -> Movement:
        with self._lock:
            existing = self._movements.get(movement.source_id)
            if existing is not None:
                return existing
            self._movements[movement.source_id] = movement
            return movement

    def get_movement(self, source_id: str) -> Optional[Movement]:
        with self._lock:
            return self._movements.get(source_id)

    def mark_movement_disputed(self, source_id: str, dispute_id: str, disputed: bool = True) -> None:
        with self._lock:
            movement = self._movements.get(source_id)
            if movement is None:
                return
            movement.disputed = disputed
            movement.dispute_id = dispute_id if disputed else None

    def list_movements(
        self,
        userid: Optional[str] = None,
        account: Optional[str] = None,
        *,
        disputable_only: bool = False,
        since: Optional[float] = None,
    ) -> List[Movement]:
        with self._lock:
            rows = list(self._movements.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if account is not None:
            rows = [row for row in rows if row.account == account]
        if since is not None:
            rows = [row for row in rows if row.created_at >= since]
        if disputable_only:
            rows = [row for row in rows if row.disputable]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows

    def put_dispute(self, dispute: Dispute) -> None:
        with self._lock:
            self._disputes[dispute.dispute_id] = dispute

    def get_dispute(self, dispute_id: str) -> Optional[Dispute]:
        with self._lock:
            return self._disputes.get(dispute_id)

    def update_dispute(self, dispute: Dispute) -> None:
        with self._lock:
            self._disputes[dispute.dispute_id] = dispute

    def find_by_source(self, source_id: str) -> Optional[Dispute]:
        with self._lock:
            matches = [
                row for row in self._disputes.values()
                if row.source_id == source_id and row.status != 'withdrawn'
            ]
        if not matches:
            return None
        matches.sort(key=lambda row: row.created_at, reverse=True)
        return matches[0]

    def list_disputes(
        self,
        userid: Optional[str] = None,
        statuses: Optional[Iterable[str]] = None,
        account: Optional[str] = None,
    ) -> List[Dispute]:
        wanted = set(statuses) if statuses is not None else None
        with self._lock:
            rows = list(self._disputes.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if account is not None:
            rows = [row for row in rows if row.account == account]
        if wanted is not None:
            rows = [row for row in rows if row.status in wanted]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows


class SqliteDisputeStore:
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
                CREATE TABLE IF NOT EXISTS movements (
                    source_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    counterparty TEXT NOT NULL,
                    description TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    disputed INTEGER NOT NULL DEFAULT 0,
                    dispute_id TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS disputes (
                    dispute_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    claimed_amount TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    actor_type TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    investigator TEXT,
                    investigated_at REAL,
                    decision TEXT,
                    decided_by TEXT,
                    decided_at REAL,
                    decision_note TEXT NOT NULL DEFAULT '',
                    credit_status TEXT NOT NULL DEFAULT 'none',
                    credited_amount TEXT NOT NULL DEFAULT '0.00',
                    credited_at REAL,
                    credited_by TEXT,
                    clawback_at REAL,
                    clawback_by TEXT
                )
                """
            )
            conn.execute('CREATE INDEX IF NOT EXISTS idx_movements_user ON movements(userid, created_at)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_movements_account ON movements(account, created_at)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_disputes_user ON disputes(userid, status)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_disputes_source ON disputes(source_id, status)')
            conn.commit()

    def put_movement(self, movement: Movement) -> Movement:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM movements WHERE source_id = ?', (movement.source_id,)
            ).fetchone()
            if existing is not None:
                return self._movement_from_row(existing)
            conn.execute(
                """
                INSERT INTO movements (
                    source_id, userid, account, amount, kind, direction,
                    counterparty, description, created_at, disputed, dispute_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    movement.source_id, movement.userid, movement.account, movement.amount,
                    movement.kind, movement.direction, movement.counterparty,
                    movement.description, movement.created_at,
                    1 if movement.disputed else 0, movement.dispute_id,
                ),
            )
            conn.commit()
            return movement

    def get_movement(self, source_id: str) -> Optional[Movement]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM movements WHERE source_id = ?', (source_id,)
            ).fetchone()
        return self._movement_from_row(row) if row else None

    def mark_movement_disputed(self, source_id: str, dispute_id: str, disputed: bool = True) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                'UPDATE movements SET disputed=?, dispute_id=? WHERE source_id=?',
                (1 if disputed else 0, dispute_id if disputed else None, source_id),
            )
            conn.commit()

    def list_movements(
        self,
        userid: Optional[str] = None,
        account: Optional[str] = None,
        *,
        disputable_only: bool = False,
        since: Optional[float] = None,
    ) -> List[Movement]:
        clauses = []
        params: List[Any] = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if account is not None:
            clauses.append('account = ?')
            params.append(account)
        if since is not None:
            clauses.append('created_at >= ?')
            params.append(since)
        sql = 'SELECT * FROM movements'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        movements = [self._movement_from_row(row) for row in rows]
        if disputable_only:
            movements = [row for row in movements if row.disputable]
        return movements

    def put_dispute(self, dispute: Dispute) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO disputes (
                    dispute_id, userid, account, source_id, amount, claimed_amount,
                    kind, reason, evidence, status, actor, actor_type, created_at,
                    updated_at, investigator, investigated_at, decision, decided_by,
                    decided_at, decision_note, credit_status, credited_amount,
                    credited_at, credited_by, clawback_at, clawback_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._dispute_row(dispute),
            )
            conn.commit()

    def get_dispute(self, dispute_id: str) -> Optional[Dispute]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM disputes WHERE dispute_id = ?', (dispute_id,)
            ).fetchone()
        return self._dispute_from_row(row) if row else None

    def update_dispute(self, dispute: Dispute) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE disputes SET
                    userid=?, account=?, source_id=?, amount=?, claimed_amount=?,
                    kind=?, reason=?, evidence=?, status=?, actor=?, actor_type=?,
                    created_at=?, updated_at=?, investigator=?, investigated_at=?,
                    decision=?, decided_by=?, decided_at=?, decision_note=?,
                    credit_status=?, credited_amount=?, credited_at=?, credited_by=?,
                    clawback_at=?, clawback_by=?
                WHERE dispute_id=?
                """,
                self._dispute_row(dispute)[1:] + (dispute.dispute_id,),
            )
            conn.commit()

    def find_by_source(self, source_id: str) -> Optional[Dispute]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM disputes
                WHERE source_id = ? AND status != 'withdrawn'
                ORDER BY created_at DESC LIMIT 1
                """,
                (source_id,),
            ).fetchone()
        return self._dispute_from_row(row) if row else None

    def list_disputes(
        self,
        userid: Optional[str] = None,
        statuses: Optional[Iterable[str]] = None,
        account: Optional[str] = None,
    ) -> List[Dispute]:
        clauses = []
        params: List[Any] = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if account is not None:
            clauses.append('account = ?')
            params.append(account)
        sql = 'SELECT * FROM disputes'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        disputes = [self._dispute_from_row(row) for row in rows]
        if statuses is not None:
            wanted = set(statuses)
            disputes = [row for row in disputes if row.status in wanted]
        return disputes

    @staticmethod
    def _movement_from_row(row: sqlite3.Row) -> Movement:
        return Movement(
            source_id=row['source_id'],
            userid=row['userid'],
            account=row['account'],
            amount=row['amount'],
            kind=row['kind'],
            direction=row['direction'],
            counterparty=row['counterparty'] or '',
            description=row['description'] or '',
            created_at=float(row['created_at']),
            disputed=bool(row['disputed']),
            dispute_id=row['dispute_id'],
        )

    @staticmethod
    def _dispute_row(dispute: Dispute) -> Tuple[Any, ...]:
        return (
            dispute.dispute_id, dispute.userid, dispute.account, dispute.source_id,
            dispute.amount, dispute.claimed_amount, dispute.kind, dispute.reason,
            dispute.evidence, dispute.status, dispute.actor, dispute.actor_type,
            dispute.created_at, dispute.updated_at, dispute.investigator,
            dispute.investigated_at, dispute.decision, dispute.decided_by,
            dispute.decided_at, dispute.decision_note, dispute.credit_status,
            dispute.credited_amount, dispute.credited_at, dispute.credited_by,
            dispute.clawback_at, dispute.clawback_by,
        )

    @staticmethod
    def _dispute_from_row(row: sqlite3.Row) -> Dispute:
        return Dispute(
            dispute_id=row['dispute_id'],
            userid=row['userid'],
            account=row['account'],
            source_id=row['source_id'],
            amount=row['amount'],
            claimed_amount=row['claimed_amount'],
            kind=row['kind'],
            reason=row['reason'],
            evidence=row['evidence'],
            status=row['status'],
            actor=row['actor'],
            actor_type=row['actor_type'],
            created_at=float(row['created_at']),
            updated_at=float(row['updated_at']),
            investigator=row['investigator'],
            investigated_at=None if row['investigated_at'] is None else float(row['investigated_at']),
            decision=row['decision'],
            decided_by=row['decided_by'],
            decided_at=None if row['decided_at'] is None else float(row['decided_at']),
            decision_note=row['decision_note'] or '',
            credit_status=row['credit_status'] or 'none',
            credited_amount=row['credited_amount'] or '0.00',
            credited_at=None if row['credited_at'] is None else float(row['credited_at']),
            credited_by=row['credited_by'],
            clawback_at=None if row['clawback_at'] is None else float(row['clawback_at']),
            clawback_by=row['clawback_by'],
        )


class DisputeService:
    def __init__(
        self,
        policy: DisputePolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        credit_fn: Optional[CreditFn] = None,
        debit_fn: Optional[DebitFn] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or time.time
        self.credit_fn = credit_fn
        self.debit_fn = debit_fn

    @staticmethod
    def _actor_is_employee(actor_type: str) -> bool:
        return str(actor_type or '') in EMPLOYEE_ROLES

    def observe(
        self,
        account: Any,
        amount: Any,
        kind: Any,
        *,
        direction: Any = None,
        userid: Optional[str] = None,
        counterparty: Any = '',
        description: Any = '',
        source_id: Any = None,
    ) -> Optional[Movement]:
        if not self.policy.enabled:
            return None
        account_text = normalize_account(account)
        kind_text = normalize_kind(kind, direction=str(direction or ''))
        direction_text = normalize_direction(direction, kind=kind_text)
        money = parse_money(amount)
        counterparty_text = ''
        if counterparty not in (None, ''):
            try:
                counterparty_text = normalize_account(counterparty)
            except AccountError:
                counterparty_text = str(counterparty).strip()[:32]
        movement = _movement_from_parts(
            source_id=normalize_source_id(source_id),
            userid=str(userid or ''),
            account=account_text,
            amount=money_str(money),
            kind=kind_text,
            direction=direction_text,
            counterparty=counterparty_text,
            description=normalize_note(description, limit=240),
            created_at=float(self.clock()),
        )
        return self.store.put_movement(movement)

    def observe_transfer(
        self,
        from_account: Any,
        to_account: Any,
        amount: Any,
        *,
        from_userid: Optional[str] = None,
        to_userid: Optional[str] = None,
        source_id: Any = None,
        deposit: bool = False,
        description: Any = '',
    ) -> List[Movement]:
        posted: List[Movement] = []
        base = str(source_id or uuid.uuid4())
        if deposit:
            row = self.observe(
                to_account, amount, 'deposit', direction='credit',
                userid=to_userid, counterparty=from_account,
                description=description or 'deposit',
                source_id=base + ':deposit',
            )
            if row is not None:
                posted.append(row)
            return posted
        outgoing = self.observe(
            from_account, amount, 'transfer_out', direction='debit',
            userid=from_userid, counterparty=to_account,
            description=description or 'transfer out',
            source_id=base + ':out',
        )
        incoming = self.observe(
            to_account, amount, 'transfer_in', direction='credit',
            userid=to_userid, counterparty=from_account,
            description=description or 'transfer in',
            source_id=base + ':in',
        )
        if outgoing is not None:
            posted.append(outgoing)
        if incoming is not None:
            posted.append(incoming)
        return posted

    def _kind_disputable(self, kind: str, direction: str) -> bool:
        if direction != 'debit':
            return False
        if kind in CREDIT_KINDS and not self.policy.credit_kinds_disputable:
            return False
        return kind in self.policy.operations

    def open_dispute(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        source_id: Any,
        reason: Any = 'unauthorized',
        evidence: Any = '',
        claimed_amount: Any = None,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> Dispute:
        if not self.policy.enabled:
            raise DisputeError('dispute_disabled', 'Disputes are disabled.')
        employee = self._actor_is_employee(actor_type)
        if not employee and not self.policy.customer_file:
            raise DisputeError('dispute_forbidden', 'Customers cannot file disputes.')
        movement = self.store.get_movement(str(source_id or '').strip())
        if movement is None:
            raise DisputeError('movement_not_found', 'No posted movement matches that id.')
        if movement.userid and movement.userid != str(owner_userid):
            raise DisputeError('dispute_forbidden', 'Movement does not belong to this customer.')
        if not employee:
            allowed = _own_account_set(own_accounts)
            if allowed and movement.account not in allowed:
                raise DisputeError('dispute_forbidden', 'Account is not owned by this customer.')
            if str(owner_userid) != str(actor):
                raise DisputeError('dispute_forbidden', 'Customers can only dispute their own movements.')
        if not self._kind_disputable(movement.kind, movement.direction):
            raise DisputeError('not_disputable', 'Credits and deposits cannot be disputed.')
        now = float(self.clock())
        if now - float(movement.created_at) > self.policy.window_seconds:
            raise DisputeError('window_expired', 'The dispute window for this movement has closed.')
        existing = self.store.find_by_source(movement.source_id)
        if existing is not None:
            raise DisputeError('dispute_duplicate', 'This movement already has an open or decided dispute.', dispute=existing)
        open_count = len(self.store.list_disputes(owner_userid, statuses=OPEN_STATUSES))
        if open_count >= self.policy.max_open:
            raise DisputeError('dispute_limit', 'Too many open disputes.')
        posted = parse_money(movement.amount)
        if posted < self.policy.min_amount or posted > self.policy.max_amount:
            raise DisputeError('amount_out_of_range', 'Movement amount is outside the disputable range.')
        if claimed_amount in (None, ''):
            claimed = posted
        else:
            claimed = parse_money(claimed_amount)
            if claimed > posted:
                raise DisputeError('credit_exceeds_dispute', 'Claimed amount cannot exceed the posted debit.')
            if claimed < self.policy.min_amount:
                raise AmountError('invalid_amount')
        reason_text = normalize_reason(reason)
        evidence_text = normalize_note(evidence, limit=self.policy.max_evidence)
        dispute = Dispute(
            dispute_id=uuid.uuid4().hex,
            userid=str(owner_userid),
            account=movement.account,
            source_id=movement.source_id,
            amount=movement.amount,
            claimed_amount=money_str(claimed),
            kind=movement.kind,
            reason=reason_text,
            evidence=evidence_text,
            status='open',
            actor=str(actor),
            actor_type=str(actor_type or 'customer'),
            created_at=now,
            updated_at=now,
        )
        self.store.put_dispute(dispute)
        self.store.mark_movement_disputed(movement.source_id, dispute.dispute_id, True)
        return dispute

    def _load_owned(self, dispute_id: str, actor: str, actor_type: str, owner_userid: Optional[str]) -> Dispute:
        dispute = self.store.get_dispute(str(dispute_id or '').strip())
        if dispute is None:
            raise DisputeError('dispute_not_found', 'Dispute not found.')
        employee = self._actor_is_employee(actor_type)
        if not employee and dispute.userid != str(owner_userid or actor):
            raise DisputeError('dispute_forbidden', 'Dispute does not belong to this user.')
        return dispute

    def withdraw(
        self,
        *,
        dispute_id: str,
        actor: str,
        actor_type: str,
        owner_userid: Optional[str] = None,
    ) -> Dispute:
        dispute = self._load_owned(dispute_id, actor, actor_type, owner_userid)
        employee = self._actor_is_employee(actor_type)
        if not employee and not self.policy.customer_withdraw:
            raise DisputeError('dispute_forbidden', 'Customers cannot withdraw disputes.')
        if dispute.status in TERMINAL_STATUSES:
            raise DisputeError('already_resolved', 'Dispute is already resolved.')
        if dispute.credit_status in {'provisional', 'final'}:
            raise DisputeError('credit_already_granted', 'Provisional credit was already granted; staff must decide.')
        now = float(self.clock())
        dispute.status = 'withdrawn'
        dispute.updated_at = now
        dispute.decision = 'withdraw'
        dispute.decided_by = str(actor)
        dispute.decided_at = now
        self.store.update_dispute(dispute)
        self.store.mark_movement_disputed(dispute.source_id, dispute.dispute_id, False)
        return dispute

    def investigate(
        self,
        *,
        dispute_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> Dispute:
        if not self._actor_is_employee(actor_type):
            raise DisputeError('dispute_forbidden', 'Only bank staff can investigate disputes.')
        dispute = self._load_owned(dispute_id, actor, actor_type, None)
        if dispute.status in TERMINAL_STATUSES:
            raise DisputeError('already_resolved', 'Dispute is already resolved.')
        if dispute.status == 'provisionally_credited':
            return dispute
        now = float(self.clock())
        dispute.status = 'investigating'
        dispute.investigator = str(actor)
        dispute.investigated_at = now
        dispute.updated_at = now
        if note:
            dispute.decision_note = normalize_note(note, limit=self.policy.max_evidence)
        self.store.update_dispute(dispute)
        return dispute

    def _apply_credit(self, dispute: Dispute, amount: Decimal, actor: str, *, final: bool) -> None:
        remark = (
            'provisional credit for dispute %s' % dispute.dispute_id[:8]
            if not final else
            'dispute %s credited' % dispute.dispute_id[:8]
        )
        if self.credit_fn is not None:
            ok = self.credit_fn(dispute.account, money_str(amount), remark)
            if not ok:
                raise DisputeError('credit_failed', 'Could not post provisional credit to the account.')
        now = float(self.clock())
        dispute.credit_status = 'final' if final else 'provisional'
        dispute.credited_amount = money_str(amount)
        dispute.credited_at = now
        dispute.credited_by = str(actor)
        dispute.updated_at = now
        if not final:
            dispute.status = 'provisionally_credited'
        self.store.update_dispute(dispute)
        if self.policy.enabled:
            self.observe(
                dispute.account, money_str(amount),
                'provisional' if not final else 'credit',
                direction='credit',
                userid=dispute.userid,
                description=remark,
                source_id='disp:%s:%s' % (dispute.dispute_id, 'final' if final else 'prov'),
            )

    def _apply_clawback(self, dispute: Dispute, actor: str) -> None:
        amount = parse_money(dispute.credited_amount, allow_zero=True)
        if amount <= 0:
            dispute.credit_status = 'clawed'
            return
        remark = 'provisional credit reversed for dispute %s' % dispute.dispute_id[:8]
        if self.debit_fn is not None:
            ok = self.debit_fn(dispute.account, money_str(amount), remark)
            if not ok:
                dispute.credit_status = 'clawback_failed'
                dispute.updated_at = float(self.clock())
                self.store.update_dispute(dispute)
                raise DisputeError(
                    'clawback_failed',
                    'Could not reverse provisional credit; account could not cover the debit.',
                    dispute=dispute,
                )
        now = float(self.clock())
        dispute.credit_status = 'clawed'
        dispute.clawback_at = now
        dispute.clawback_by = str(actor)
        dispute.updated_at = now
        self.store.update_dispute(dispute)
        if self.policy.enabled:
            self.observe(
                dispute.account, money_str(amount),
                'provisional_clawback', direction='debit',
                userid=dispute.userid,
                description=remark,
                source_id='disp:%s:claw' % dispute.dispute_id,
            )

    def grant_provisional(
        self,
        *,
        dispute_id: str,
        actor: str,
        actor_type: str,
        amount: Any = None,
    ) -> Dispute:
        if not self._actor_is_employee(actor_type):
            raise DisputeError('dispute_forbidden', 'Only bank staff can grant provisional credit.')
        dispute = self._load_owned(dispute_id, actor, actor_type, None)
        if dispute.status in TERMINAL_STATUSES:
            raise DisputeError('already_resolved', 'Dispute is already resolved.')
        if dispute.credit_status in {'provisional', 'final'}:
            raise DisputeError('credit_already_granted', 'Provisional credit was already granted.', dispute=dispute)
        claimed = parse_money(dispute.claimed_amount)
        if amount in (None, ''):
            credit_amount = claimed
        else:
            credit_amount = parse_money(amount)
            if credit_amount > claimed:
                raise DisputeError('credit_exceeds_dispute', 'Provisional credit cannot exceed the claimed amount.')
        if dispute.status == 'open' and self.policy.auto_investigate:
            dispute.status = 'investigating'
            dispute.investigator = str(actor)
            dispute.investigated_at = float(self.clock())
        self._apply_credit(dispute, credit_amount, actor, final=False)
        return self.store.get_dispute(dispute.dispute_id) or dispute

    def decide(
        self,
        *,
        dispute_id: str,
        actor: str,
        actor_type: str,
        decision: Any,
        note: Any = '',
    ) -> Dispute:
        if not self._actor_is_employee(actor_type):
            raise DisputeError('dispute_forbidden', 'Only bank staff can decide disputes.')
        dispute = self._load_owned(dispute_id, actor, actor_type, None)
        if dispute.status in TERMINAL_STATUSES:
            raise DisputeError('already_resolved', 'Dispute is already resolved.')
        verdict = normalize_decision(decision)
        now = float(self.clock())
        if verdict == 'uphold':
            if dispute.credit_status == 'none':
                self._apply_credit(dispute, parse_money(dispute.claimed_amount), actor, final=True)
                dispute = self.store.get_dispute(dispute.dispute_id) or dispute
            elif dispute.credit_status == 'provisional':
                dispute.credit_status = 'final'
            elif dispute.credit_status == 'clawback_failed':
                raise DisputeError('clawback_failed', 'Outstanding clawback must be resolved first.', dispute=dispute)
            dispute.status = 'upheld'
        else:
            if dispute.credit_status in {'provisional', 'final'}:
                self._apply_clawback(dispute, actor)
                dispute = self.store.get_dispute(dispute.dispute_id) or dispute
            elif dispute.credit_status == 'clawback_failed':
                self._apply_clawback(dispute, actor)
                dispute = self.store.get_dispute(dispute.dispute_id) or dispute
            dispute.status = 'denied'
        dispute.decision = verdict
        dispute.decided_by = str(actor)
        dispute.decided_at = now
        dispute.updated_at = now
        if note:
            dispute.decision_note = normalize_note(note, limit=self.policy.max_evidence)
        self.store.update_dispute(dispute)
        return dispute

    def retry_clawback(
        self,
        *,
        dispute_id: str,
        actor: str,
        actor_type: str,
    ) -> Dispute:
        if not self._actor_is_employee(actor_type):
            raise DisputeError('dispute_forbidden', 'Only bank staff can retry a clawback.')
        dispute = self._load_owned(dispute_id, actor, actor_type, None)
        if dispute.credit_status != 'clawback_failed':
            raise DisputeError('not_disputable', 'No failed clawback to retry.')
        self._apply_clawback(dispute, actor)
        dispute = self.store.get_dispute(dispute.dispute_id) or dispute
        if dispute.status not in TERMINAL_STATUSES:
            dispute.status = 'denied'
            dispute.decision = 'deny'
            dispute.decided_by = str(actor)
            dispute.decided_at = float(self.clock())
            dispute.updated_at = float(self.clock())
            self.store.update_dispute(dispute)
        return dispute

    def snapshot(self, userid: str) -> Dict[str, Any]:
        now = float(self.clock())
        window_start = now - self.policy.window_seconds
        disputes = self.store.list_disputes(userid)
        movements = self.store.list_movements(userid, since=window_start)
        challengeable = [
            row.to_dict() for row in movements
            if row.disputable and not row.disputed
        ]
        open_rows = [row for row in disputes if row.status in OPEN_STATUSES]
        provisional_total = Decimal('0.00')
        for row in open_rows:
            if row.credit_status == 'provisional':
                provisional_total += parse_money(row.credited_amount, allow_zero=True)
        return {
            'enabled': self.policy.enabled,
            'window_seconds': self.policy.window_seconds,
            'max_open': self.policy.max_open,
            'reasons': sorted(REASONS),
            'open_count': len(open_rows),
            'provisional_total': money_str(provisional_total),
            'disputes': [row.to_dict() for row in disputes],
            'challengeable': challengeable,
        }


_SERVICE: Optional[DisputeService] = None


def set_service(service: Optional[DisputeService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[DisputeService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('DISPUTE_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryDisputeStore()
    path = os.environ.get('DISPUTE_DB', DEFAULT_STORE_PATH)
    return SqliteDisputeStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    credit_fn: Optional[CreditFn] = None,
    debit_fn: Optional[DebitFn] = None,
) -> DisputeService:
    if store is None:
        store = default_store()
    return DisputeService(
        DisputePolicy.from_env(),
        store,
        clock=clock,
        credit_fn=credit_fn,
        debit_fn=debit_fn,
    )


def observe_movement(
    account: Any,
    amount: Any,
    kind: Any,
    *,
    direction: Any = None,
    userid: Optional[str] = None,
    counterparty: Any = '',
    description: Any = '',
    source_id: Any = None,
) -> None:
    service = get_service()
    if service is None:
        return
    try:
        service.observe(
            account, amount, kind,
            direction=direction, userid=userid, counterparty=counterparty,
            description=description, source_id=source_id,
        )
    except Exception:
        return


def observe_transfer(
    from_account: Any,
    to_account: Any,
    amount: Any,
    *,
    from_userid: Optional[str] = None,
    to_userid: Optional[str] = None,
    source_id: Any = None,
    deposit: bool = False,
    description: Any = '',
) -> None:
    service = get_service()
    if service is None:
        return
    try:
        service.observe_transfer(
            from_account, to_account, amount,
            from_userid=from_userid, to_userid=to_userid,
            source_id=source_id, deposit=deposit, description=description,
        )
    except Exception:
        return


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
        'dispute_duplicate': 409,
        'dispute_limit': 409,
        'already_resolved': 409,
        'credit_already_granted': 409,
        'clawback_failed': 409,
        'credit_failed': 409,
        'dispute_forbidden': 403,
        'dispute_disabled': 403,
        'not_disputable': 403,
        'window_expired': 400,
        'invalid_reason': 400,
        'invalid_decision': 400,
        'invalid_kind': 400,
        'invalid_amount': 400,
        'invalid_account': 400,
        'amount_out_of_range': 400,
        'credit_exceeds_dispute': 400,
        'dispute_not_found': 404,
        'movement_not_found': 404,
        'missing_customer_id': 400,
    }.get(code, 400)


def _error_body(exc: DisputeError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('dispute') is not None:
        body['dispute'] = exc.extra['dispute'].to_dict()
    return body


def handle_open_dispute(service: DisputeService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    source_id = str(values.get('source_id') or '').strip()
    if not source_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_source_id'}), 400
    own_accounts = None
    if actor_type == 'customer' and callable(own_accounts_loader):
        own_accounts = own_accounts_loader(userid)
    try:
        dispute = service.open_dispute(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            source_id=source_id,
            reason=values.get('reason') or 'unauthorized',
            evidence=values.get('evidence') or values.get('note') or '',
            claimed_amount=values.get('claimed_amount') or values.get('amount'),
            own_accounts=own_accounts,
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except DisputeError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Dispute opened',
        'dispute': dispute.to_dict(),
        'Disputes': service.snapshot(owner),
    }), 201


def handle_list_disputes(service: DisputeService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Disputes': service.snapshot(owner)}), 200


def handle_list_challengeable(service: DisputeService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    snapshot = service.snapshot(owner)
    return jsonify({'challengeable': snapshot['challengeable'], 'Disputes': snapshot}), 200


def handle_get_dispute(service: DisputeService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    dispute_id = str(values.get('dispute_id') or '').strip()
    if not dispute_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_dispute_id'}), 400
    try:
        dispute = service._load_owned(
            dispute_id, userid, session.get('usertype') or 'customer', userid,
        )
    except DisputeError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({'dispute': dispute.to_dict(), 'Disputes': service.snapshot(dispute.userid)}), 200


def handle_withdraw_dispute(service: DisputeService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    dispute_id = str(values.get('dispute_id') or '').strip()
    if not dispute_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_dispute_id'}), 400
    try:
        dispute = service.withdraw(
            dispute_id=dispute_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
            owner_userid=userid,
        )
    except DisputeError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Dispute withdrawn',
        'dispute': dispute.to_dict(),
        'Disputes': service.snapshot(dispute.userid),
    }), 200


def handle_investigate_dispute(service: DisputeService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    dispute_id = str(values.get('dispute_id') or '').strip()
    if not dispute_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_dispute_id'}), 400
    try:
        dispute = service.investigate(
            dispute_id=dispute_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
            note=values.get('note') or '',
        )
    except DisputeError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Dispute under investigation',
        'dispute': dispute.to_dict(),
        'Disputes': service.snapshot(dispute.userid),
    }), 200


def handle_grant_provisional(service: DisputeService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    dispute_id = str(values.get('dispute_id') or '').strip()
    if not dispute_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_dispute_id'}), 400
    try:
        dispute = service.grant_provisional(
            dispute_id=dispute_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
            amount=values.get('amount') or values.get('claimed_amount'),
        )
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except DisputeError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Provisional credit granted',
        'dispute': dispute.to_dict(),
        'Disputes': service.snapshot(dispute.userid),
    }), 200


def handle_decide_dispute(service: DisputeService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    dispute_id = str(values.get('dispute_id') or '').strip()
    if not dispute_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_dispute_id'}), 400
    try:
        dispute = service.decide(
            dispute_id=dispute_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
            decision=values.get('decision'),
            note=values.get('note') or '',
        )
    except DisputeError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Dispute %s' % ('upheld' if dispute.decision == 'uphold' else 'denied'),
        'dispute': dispute.to_dict(),
        'Disputes': service.snapshot(dispute.userid),
    }), 200


def handle_retry_clawback(service: DisputeService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    dispute_id = str(values.get('dispute_id') or '').strip()
    if not dispute_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_dispute_id'}), 400
    try:
        dispute = service.retry_clawback(
            dispute_id=dispute_id,
            actor=userid,
            actor_type=session.get('usertype') or 'customer',
        )
    except DisputeError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'Provisional credit reversed',
        'dispute': dispute.to_dict(),
        'Disputes': service.snapshot(dispute.userid),
    }), 200


def attach_dispute_routes(app, service: DisputeService, own_accounts_loader=None) -> None:
    @app.route('/openDispute', methods=['POST', 'GET'])
    def open_dispute_route():
        return handle_open_dispute(service, own_accounts_loader=own_accounts_loader)

    @app.route('/listDisputes', methods=['POST', 'GET'])
    def list_disputes_route():
        return handle_list_disputes(service)

    @app.route('/listChallengeable', methods=['POST', 'GET'])
    def list_challengeable_route():
        return handle_list_challengeable(service)

    @app.route('/getDispute', methods=['POST', 'GET'])
    def get_dispute_route():
        return handle_get_dispute(service)

    @app.route('/withdrawDispute', methods=['POST', 'GET'])
    def withdraw_dispute_route():
        return handle_withdraw_dispute(service)

    @app.route('/investigateDispute', methods=['POST', 'GET'])
    def investigate_dispute_route():
        return handle_investigate_dispute(service)

    @app.route('/grantProvisionalCredit', methods=['POST', 'GET'])
    def grant_provisional_route():
        return handle_grant_provisional(service)

    @app.route('/decideDispute', methods=['POST', 'GET'])
    def decide_dispute_route():
        return handle_decide_dispute(service)

    @app.route('/retryClawback', methods=['POST', 'GET'])
    def retry_clawback_route():
        return handle_retry_clawback(service)
