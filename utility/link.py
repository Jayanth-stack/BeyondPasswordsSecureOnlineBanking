"""External account linking via micro-deposit / prenote verification.

Customers prove they own an external bank account before it can move ACH.
Two random cent deposits (or a zero-dollar prenote) are recorded; the
customer confirms the amounts. Staff can accept/reject prenotes, force
verify, settle, or return a later push/pull.

Independent of bill-pay outgoing ACH (PR #66), inbound payroll splits
(PR #64), the in-bank payee allowlist (PR #26), and scheduled internal
transfers (PR #36). Existing `/fundTransfer` and `/withdrawAmount` stay
unchanged. `Customers.debit_request` / `credit_request` still write
`debited` / `direct deposited` unless a remark is supplied here.

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
Challenge amounts are HMAC'd — never returned in snapshots or to_dict.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, List, Optional, Tuple

from flask import jsonify, request, session

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
METHOD_MICRO = 'micro'
METHOD_PRENOTE = 'prenote'
METHODS = frozenset({METHOD_MICRO, METHOD_PRENOTE})
METHOD_ALIASES = {
    'micro': METHOD_MICRO, 'microdeposit': METHOD_MICRO, 'micro_deposit': METHOD_MICRO,
    'deposits': METHOD_MICRO, 'challenge': METHOD_MICRO,
    'prenote': METHOD_PRENOTE, 'pre_note': METHOD_PRENOTE, 'zero': METHOD_PRENOTE,
}
LINK_PENDING = 'pending'
LINK_VERIFIED = 'verified'
LINK_PAUSED = 'paused'
LINK_LOCKED = 'locked'
LINK_EXPIRED = 'expired'
LINK_CLOSED = 'closed'
LINK_REJECTED = 'rejected'
LINK_STATUSES = frozenset({
    LINK_PENDING, LINK_VERIFIED, LINK_PAUSED, LINK_LOCKED,
    LINK_EXPIRED, LINK_CLOSED, LINK_REJECTED,
})
OPEN_STATUSES = frozenset({LINK_PENDING, LINK_VERIFIED, LINK_PAUSED, LINK_LOCKED, LINK_EXPIRED})
MOVE_PUSH = 'push'
MOVE_PULL = 'pull'
MOVE_DIRECTIONS = frozenset({MOVE_PUSH, MOVE_PULL})
PAY_SENT = 'sent'
PAY_SETTLED = 'settled'
PAY_RETURNED = 'returned'
PAY_NSF = 'nsf'
PAY_FAILED = 'failed'
PAY_STATUSES = frozenset({PAY_SENT, PAY_SETTLED, PAY_RETURNED, PAY_NSF, PAY_FAILED})
RETURN_REASONS = frozenset({'unauthorized', 'duplicate', 'wrong_amount', 'stop', 'other'})
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = 'SystemLogs/link.sqlite'
DEFAULT_SECRET_PATH = 'SystemLogs/.link_secret'
DEBIT_OK = frozenset({'success', 'done', 'ok', 'amount debited', 'amount credited'})
DEBIT_NSF = ('insufficient',)


class AccountError(ValueError):
    pass


class AmountError(ValueError):
    pass


class LinkError(ValueError):
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
        raise LinkError('invalid_nickname', 'Nickname must be 2-40 characters.')
    return text


def normalize_last4(value: Any, *, required: bool = True) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        if required:
            raise LinkError('invalid_last4', 'Routing/account last-4 must be four digits.')
        return ''
    if len(digits) < 4:
        raise LinkError('invalid_last4', 'Routing/account last-4 must be four digits.')
    return digits[-4:]


def normalize_method(value: Any, *, default: str = METHOD_MICRO) -> str:
    text = str(value or default).strip().lower().replace('-', '_').replace(' ', '_')
    mapped = METHOD_ALIASES.get(text, text)
    if mapped not in METHODS:
        raise LinkError('invalid_method', 'Method must be micro or prenote.')
    return mapped


def normalize_return_reason(value: Any) -> str:
    text = str(value or 'other').strip().lower().replace(' ', '_')
    aliases = {'nsf': 'other', 'r10': 'unauthorized', 'r07': 'unauthorized', 'stop_payment': 'stop'}
    text = aliases.get(text, text)
    if text not in RETURN_REASONS:
        raise LinkError('invalid_reason', 'Unknown return reason.')
    return text


def normalize_direction(value: Any) -> str:
    text = str(value or '').strip().lower()
    aliases = {
        'push': MOVE_PUSH, 'send': MOVE_PUSH, 'to': MOVE_PUSH, 'outbound': MOVE_PUSH,
        'pull': MOVE_PULL, 'receive': MOVE_PULL, 'from': MOVE_PULL, 'inbound': MOVE_PULL,
    }
    mapped = aliases.get(text, text)
    if mapped not in MOVE_DIRECTIONS:
        raise LinkError('invalid_direction', 'Direction must be push or pull.')
    return mapped


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


def compose_micro_deposits(
    *,
    min_cents: int = 1,
    max_cents: int = 99,
    rng: Optional[Callable[[int, int], int]] = None,
) -> Tuple[Decimal, Decimal]:
    """Two distinct cent amounts in [min_cents, max_cents]. Reusable challenge generator."""
    if min_cents < 1 or max_cents < min_cents or max_cents > 99:
        raise LinkError('invalid_amount', 'Micro-deposit cent range must be 1-99.')
    if min_cents == max_cents:
        raise LinkError('invalid_amount', 'Micro-deposit range must allow two distinct amounts.')
    picker = rng or (lambda low, high: secrets.randbelow(high - low + 1) + low)
    first = int(picker(min_cents, max_cents))
    second = int(picker(min_cents, max_cents))
    guard = 0
    while second == first and guard < 24:
        second = int(picker(min_cents, max_cents))
        guard += 1
    if second == first:
        second = first + 1 if first < max_cents else first - 1
    first = max(min_cents, min(max_cents, first))
    second = max(min_cents, min(max_cents, second))
    if first == second:
        second = first + 1 if first < max_cents else first - 1
    return (
        (Decimal(first) / Decimal(100)).quantize(MONEY_QUANTUM),
        (Decimal(second) / Decimal(100)).quantize(MONEY_QUANTUM),
    )


def canonical_challenge_pair(first: Decimal, second: Decimal) -> Tuple[Decimal, Decimal]:
    a = first.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    b = second.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    if a == b:
        raise LinkError('invalid_amount', 'Micro-deposits must be two different amounts.')
    return (a, b) if a < b else (b, a)


def challenge_digest(link_id: str, first: Decimal, second: Decimal, secret: str) -> str:
    low, high = canonical_challenge_pair(first, second)
    payload = '%s|%s|%s' % (link_id, money_str(low), money_str(high))
    return hmac.new(
        str(secret).encode('utf-8'),
        payload.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()


def amounts_match(digest: str, link_id: str, first: Decimal, second: Decimal, secret: str) -> bool:
    if not digest:
        return False
    try:
        expected = challenge_digest(link_id, first, second, secret)
    except LinkError:
        return False
    return hmac.compare_digest(str(digest), expected)


def fingerprint_for(userid: str, routing_last4: str, account_last4: str) -> str:
    return '%s:%s:%s' % (userid, routing_last4, account_last4)


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


def _load_secret() -> str:
    env = os.environ.get('LINK_CHALLENGE_SECRET')
    if env and env.strip():
        return env.strip()
    path = os.environ.get('LINK_SECRET_PATH', DEFAULT_SECRET_PATH)
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as handle:
            stored = handle.read().strip()
            if stored:
                return stored
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    secret = secrets.token_hex(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(secret)
    return secret


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
class LinkPolicy:
    enabled: bool = True
    customer_manage: bool = True
    customer_move: bool = True
    allow_credit: bool = False
    max_links: int = 8
    max_movements: int = 80
    max_attempts: int = 5
    max_resends: int = 3
    min_amount: Decimal = Decimal('1.00')
    max_amount: Decimal = Decimal('10000.00')
    min_micro_cents: int = 1
    max_micro_cents: int = 99
    pending_ttl_seconds: int = 7 * 86400
    prenote_wait_seconds: int = 2 * 86400
    prenote_auto_accept: bool = False
    challenge_secret: str = 'link-dev-secret'

    @classmethod
    def from_env(cls) -> 'LinkPolicy':
        return cls(
            enabled=_env_bool('LINK_ENABLED', True),
            customer_manage=_env_bool('LINK_CUSTOMER_MANAGE', True),
            customer_move=_env_bool('LINK_CUSTOMER_MOVE', True),
            allow_credit=_env_bool('LINK_ALLOW_CREDIT', False),
            max_links=max(1, _env_int('LINK_MAX_LINKS', 8)),
            max_movements=max(1, _env_int('LINK_MAX_MOVEMENTS', 80)),
            max_attempts=max(1, _env_int('LINK_MAX_ATTEMPTS', 5)),
            max_resends=max(0, _env_int('LINK_MAX_RESENDS', 3)),
            min_amount=_env_money('LINK_MIN_AMOUNT', '1.00'),
            max_amount=_env_money('LINK_MAX_AMOUNT', '10000.00'),
            min_micro_cents=max(1, min(98, _env_int('LINK_MIN_MICRO_CENTS', 1))),
            max_micro_cents=max(2, min(99, _env_int('LINK_MAX_MICRO_CENTS', 99))),
            pending_ttl_seconds=max(60, _env_int('LINK_PENDING_TTL', 7 * 86400)),
            prenote_wait_seconds=max(0, _env_int('LINK_PRENOTE_WAIT', 2 * 86400)),
            prenote_auto_accept=_env_bool('LINK_PRENOTE_AUTO_ACCEPT', False),
            challenge_secret=_load_secret(),
        )


@dataclass
class LinkedAccount:
    link_id: str
    userid: str
    nickname: str
    routing_last4: str
    account_last4: str
    default_account: str
    method: str
    status: str
    challenge_digest: str
    attempts: int
    resends: int
    actor: str
    created_at: float
    updated_at: float
    expires_at: float
    verified_at: float = 0.0
    locked_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'link_id': self.link_id,
            'userid': self.userid,
            'nickname': self.nickname,
            'routing_last4': self.routing_last4,
            'account_last4': self.account_last4,
            'default_account': self.default_account,
            'method': self.method,
            'status': self.status,
            'attempts': self.attempts,
            'resends': self.resends,
            'actor': self.actor,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'expires_at': self.expires_at,
            'verified_at': self.verified_at,
            'locked_at': self.locked_at,
            'pending': self.status == LINK_PENDING,
            'verified': self.status == LINK_VERIFIED,
            'paused': self.status == LINK_PAUSED,
            'locked': self.status == LINK_LOCKED,
            'expired': self.status == LINK_EXPIRED,
            'closed': self.status == LINK_CLOSED,
            'rejected': self.status == LINK_REJECTED,
            'confirmable': self.status == LINK_PENDING and self.method == METHOD_MICRO,
        }


@dataclass
class LinkedAch:
    movement_id: str
    trace_id: str
    link_id: str
    userid: str
    internal_account: str
    amount: str
    nickname: str
    direction: str
    status: str
    actor: str
    created_at: float
    returned_at: float = 0.0
    note: str = ''
    reason: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'movement_id': self.movement_id,
            'trace_id': self.trace_id,
            'link_id': self.link_id,
            'userid': self.userid,
            'internal_account': self.internal_account,
            'amount': self.amount,
            'nickname': self.nickname,
            'direction': self.direction,
            'status': self.status,
            'actor': self.actor,
            'created_at': self.created_at,
            'returned_at': self.returned_at,
            'note': self.note,
            'reason': self.reason,
            'sent': self.status == PAY_SENT,
            'settled': self.status == PAY_SETTLED,
            'returned': self.status == PAY_RETURNED,
        }


def _clone_link(row: LinkedAccount) -> LinkedAccount:
    return LinkedAccount(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _clone_movement(row: LinkedAch) -> LinkedAch:
    return LinkedAch(**{k: getattr(row, k) for k in row.__dataclass_fields__})


def _link_from_row(row: Any) -> LinkedAccount:
    return LinkedAccount(
        link_id=row['link_id'],
        userid=row['userid'],
        nickname=row['nickname'],
        routing_last4=row['routing_last4'],
        account_last4=row['account_last4'],
        default_account=row['default_account'],
        method=row['method'],
        status=row['status'],
        challenge_digest=row['challenge_digest'],
        attempts=int(row['attempts']),
        resends=int(row['resends']),
        actor=row['actor'],
        created_at=float(row['created_at']),
        updated_at=float(row['updated_at']),
        expires_at=float(row['expires_at']),
        verified_at=float(row['verified_at'] or 0),
        locked_at=float(row['locked_at'] or 0),
    )


def _movement_from_row(row: Any) -> LinkedAch:
    return LinkedAch(
        movement_id=row['movement_id'],
        trace_id=row['trace_id'],
        link_id=row['link_id'],
        userid=row['userid'],
        internal_account=row['internal_account'],
        amount=row['amount'],
        nickname=row['nickname'],
        direction=row['direction'],
        status=row['status'],
        actor=row['actor'],
        created_at=float(row['created_at']),
        returned_at=float(row['returned_at'] or 0),
        note=row['note'] or '',
        reason=row['reason'] or '',
    )


class MemoryLinkStore:
    def __init__(self) -> None:
        self._links: Dict[str, LinkedAccount] = {}
        self._movements: Dict[str, LinkedAch] = {}
        self._by_trace: Dict[str, str] = {}
        self._lock = threading.Lock()

    def put_link(self, link: LinkedAccount) -> None:
        with self._lock:
            self._links[link.link_id] = link

    def get_link(self, link_id: str) -> Optional[LinkedAccount]:
        with self._lock:
            row = self._links.get(link_id)
            return _clone_link(row) if row else None

    def update_link(self, link: LinkedAccount) -> None:
        with self._lock:
            self._links[link.link_id] = link

    def list_links(self, userid: Optional[str] = None, *, include_closed: bool = True) -> List[LinkedAccount]:
        with self._lock:
            rows = [_clone_link(row) for row in self._links.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if not include_closed:
            rows = [row for row in rows if row.status not in {LINK_CLOSED, LINK_REJECTED}]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows

    def find_link_by_nickname(self, userid: str, nickname: str) -> Optional[LinkedAccount]:
        wanted = nickname.strip().lower()
        with self._lock:
            for row in self._links.values():
                if row.userid == userid and row.nickname.lower() == wanted and row.status in OPEN_STATUSES:
                    return _clone_link(row)
        return None

    def find_link_by_fingerprint(self, userid: str, routing_last4: str, account_last4: str) -> Optional[LinkedAccount]:
        with self._lock:
            for row in self._links.values():
                if (
                    row.userid == userid
                    and row.routing_last4 == routing_last4
                    and row.account_last4 == account_last4
                    and row.status in OPEN_STATUSES
                ):
                    return _clone_link(row)
        return None

    def put_movement(self, movement: LinkedAch) -> LinkedAch:
        with self._lock:
            existing_id = self._by_trace.get(movement.trace_id)
            if existing_id is not None:
                return self._movements[existing_id]
            self._movements[movement.movement_id] = movement
            self._by_trace[movement.trace_id] = movement.movement_id
            return movement

    def update_movement(self, movement: LinkedAch) -> None:
        with self._lock:
            self._movements[movement.movement_id] = movement

    def get_movement(self, movement_id: str) -> Optional[LinkedAch]:
        with self._lock:
            row = self._movements.get(movement_id)
            return _clone_movement(row) if row else None

    def get_movement_by_trace(self, trace_id: str) -> Optional[LinkedAch]:
        with self._lock:
            movement_id = self._by_trace.get(trace_id)
            return _clone_movement(self._movements[movement_id]) if movement_id else None

    def list_movements(self, userid: Optional[str] = None, link_id: Optional[str] = None) -> List[LinkedAch]:
        with self._lock:
            rows = [_clone_movement(row) for row in self._movements.values()]
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        if link_id is not None:
            rows = [row for row in rows if row.link_id == link_id]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows


class SqliteLinkStore:
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
                CREATE TABLE IF NOT EXISTS links (
                    link_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    routing_last4 TEXT NOT NULL,
                    account_last4 TEXT NOT NULL,
                    default_account TEXT NOT NULL,
                    method TEXT NOT NULL,
                    status TEXT NOT NULL,
                    challenge_digest TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    resends INTEGER NOT NULL DEFAULT 0,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    verified_at REAL NOT NULL DEFAULT 0,
                    locked_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS movements (
                    movement_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE,
                    link_id TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    internal_account TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    returned_at REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()

    def put_link(self, link: LinkedAccount) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO links (
                    link_id, userid, nickname, routing_last4, account_last4,
                    default_account, method, status, challenge_digest, attempts,
                    resends, actor, created_at, updated_at, expires_at,
                    verified_at, locked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link.link_id, link.userid, link.nickname, link.routing_last4,
                    link.account_last4, link.default_account, link.method, link.status,
                    link.challenge_digest, link.attempts, link.resends, link.actor,
                    link.created_at, link.updated_at, link.expires_at,
                    link.verified_at, link.locked_at,
                ),
            )
            conn.commit()

    def get_link(self, link_id: str) -> Optional[LinkedAccount]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM links WHERE link_id = ?', (link_id,)).fetchone()
        return _link_from_row(row) if row else None

    def update_link(self, link: LinkedAccount) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE links SET nickname=?, routing_last4=?, account_last4=?,
                    default_account=?, method=?, status=?, challenge_digest=?,
                    attempts=?, resends=?, actor=?, updated_at=?, expires_at=?,
                    verified_at=?, locked_at=?
                WHERE link_id=?
                """,
                (
                    link.nickname, link.routing_last4, link.account_last4,
                    link.default_account, link.method, link.status, link.challenge_digest,
                    link.attempts, link.resends, link.actor, link.updated_at, link.expires_at,
                    link.verified_at, link.locked_at, link.link_id,
                ),
            )
            conn.commit()

    def list_links(self, userid: Optional[str] = None, *, include_closed: bool = True) -> List[LinkedAccount]:
        sql = 'SELECT * FROM links'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if not include_closed:
            clauses.append("status NOT IN ('closed', 'rejected')")
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_link_from_row(row) for row in rows]

    def find_link_by_nickname(self, userid: str, nickname: str) -> Optional[LinkedAccount]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM links
                WHERE userid = ? AND lower(nickname) = lower(?)
                  AND status IN ('pending', 'verified', 'paused', 'locked', 'expired')
                """,
                (userid, nickname),
            ).fetchone()
        return _link_from_row(row) if row else None

    def find_link_by_fingerprint(self, userid: str, routing_last4: str, account_last4: str) -> Optional[LinkedAccount]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM links
                WHERE userid = ? AND routing_last4 = ? AND account_last4 = ?
                  AND status IN ('pending', 'verified', 'paused', 'locked', 'expired')
                """,
                (userid, routing_last4, account_last4),
            ).fetchone()
        return _link_from_row(row) if row else None

    def put_movement(self, movement: LinkedAch) -> LinkedAch:
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                'SELECT * FROM movements WHERE trace_id = ?', (movement.trace_id,)
            ).fetchone()
            if existing is not None:
                return _movement_from_row(existing)
            conn.execute(
                """
                INSERT INTO movements (
                    movement_id, trace_id, link_id, userid, internal_account,
                    amount, nickname, direction, status, actor, created_at,
                    returned_at, note, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    movement.movement_id, movement.trace_id, movement.link_id, movement.userid,
                    movement.internal_account, movement.amount, movement.nickname,
                    movement.direction, movement.status, movement.actor, movement.created_at,
                    movement.returned_at, movement.note, movement.reason,
                ),
            )
            conn.commit()
            return movement

    def update_movement(self, movement: LinkedAch) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE movements SET status=?, actor=?, returned_at=?, note=?, reason=?
                WHERE movement_id=?
                """,
                (
                    movement.status, movement.actor, movement.returned_at,
                    movement.note, movement.reason, movement.movement_id,
                ),
            )
            conn.commit()

    def get_movement(self, movement_id: str) -> Optional[LinkedAch]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM movements WHERE movement_id = ?', (movement_id,)
            ).fetchone()
        return _movement_from_row(row) if row else None

    def get_movement_by_trace(self, trace_id: str) -> Optional[LinkedAch]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM movements WHERE trace_id = ?', (trace_id,)
            ).fetchone()
        return _movement_from_row(row) if row else None

    def list_movements(self, userid: Optional[str] = None, link_id: Optional[str] = None) -> List[LinkedAch]:
        sql = 'SELECT * FROM movements'
        params: List[Any] = []
        clauses = []
        if userid is not None:
            clauses.append('userid = ?')
            params.append(userid)
        if link_id is not None:
            clauses.append('link_id = ?')
            params.append(link_id)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_movement_from_row(row) for row in rows]


class LinkService:
    def __init__(
        self,
        policy: LinkPolicy,
        store: Any,
        *,
        clock: Optional[Callable[[], float]] = None,
        debit_fn: Optional[Callable[..., Any]] = None,
        credit_fn: Optional[Callable[..., Any]] = None,
        accounts_fn: Optional[Callable[[str], Any]] = None,
        amount_fn: Optional[Callable[[], Tuple[Decimal, Decimal]]] = None,
    ) -> None:
        self.policy = policy
        self.store = store
        self.clock = clock or (lambda: __import__('time').time())
        self.debit_fn = debit_fn
        self.credit_fn = credit_fn
        self.accounts_fn = accounts_fn
        self.amount_fn = amount_fn

    def _require_enabled(self) -> None:
        if not self.policy.enabled:
            raise LinkError('link_disabled', 'Linked accounts are disabled.')

    def _require_manage(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_manage:
            raise LinkError('link_forbidden', 'Customers cannot manage linked accounts.')

    def _require_move(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES and not self.policy.customer_move:
            raise LinkError('link_forbidden', 'Customers cannot move funds on linked accounts.')

    def _require_staff(self, actor_type: str) -> None:
        self._require_enabled()
        if actor_type not in EMPLOYEE_ROLES:
            raise LinkError('link_forbidden', 'Staff only.')

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
            raise LinkError('invalid_account', 'Account is not owned by this customer.')
        types = self._account_types(userid)
        kind = types.get(account)
        if kind == 'credit' and not self.policy.allow_credit:
            raise LinkError('credit_not_allowed', 'Credit accounts cannot originate or receive linked ACH.')

    def _assert_amount(self, dollars: Decimal) -> None:
        if dollars < self.policy.min_amount or dollars > self.policy.max_amount:
            raise LinkError('amount_out_of_range', 'Amount is outside the allowed range.')

    def _challenge_amounts(self) -> Tuple[Decimal, Decimal]:
        if self.amount_fn is not None:
            pair = self.amount_fn()
            return canonical_challenge_pair(pair[0], pair[1])
        return compose_micro_deposits(
            min_cents=self.policy.min_micro_cents,
            max_cents=self.policy.max_micro_cents,
        )

    def _issue_challenge(self, link: LinkedAccount, *, increment_resend: bool = False) -> LinkedAccount:
        now = float(self.clock())
        if link.method == METHOD_PRENOTE:
            link.challenge_digest = ''
            link.attempts = 0
            link.expires_at = now + float(self.policy.prenote_wait_seconds)
        else:
            first, second = self._challenge_amounts()
            link.challenge_digest = challenge_digest(
                link.link_id, first, second, self.policy.challenge_secret,
            )
            link.attempts = 0
            link.expires_at = now + float(self.policy.pending_ttl_seconds)
        if increment_resend:
            link.resends += 1
        link.status = LINK_PENDING
        link.locked_at = 0.0
        link.updated_at = now
        return link

    def expire_due(self, userid: Optional[str] = None) -> List[LinkedAccount]:
        now = float(self.clock())
        changed: List[LinkedAccount] = []
        for link in self.store.list_links(userid):
            if link.status != LINK_PENDING:
                continue
            if now < link.expires_at:
                continue
            if link.method == METHOD_PRENOTE and self.policy.prenote_auto_accept:
                link.status = LINK_VERIFIED
                link.verified_at = now
                link.updated_at = now
            else:
                link.status = LINK_EXPIRED if link.method == METHOD_MICRO else LINK_PENDING
                if link.method == METHOD_MICRO:
                    link.updated_at = now
                else:
                    # Prenote stays pending until staff accept/reject; do not auto-expire.
                    continue
            self.store.update_link(link)
            changed.append(link)
        return changed

    def add_link(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        nickname: Any,
        default_account: Any,
        routing_last4: Any = None,
        account_last4: Any = None,
        method: Any = METHOD_MICRO,
    ) -> LinkedAccount:
        self._require_manage(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise LinkError('link_forbidden', 'Not allowed to link accounts for this customer.')
        name = normalize_nickname(nickname)
        routing = normalize_last4(routing_last4, required=True)
        acct = normalize_last4(account_last4, required=True)
        account = normalize_account(default_account)
        self._assert_internal_account(owner_userid, account)
        kind = normalize_method(method)
        if self.store.find_link_by_nickname(owner_userid, name) is not None:
            raise LinkError('link_duplicate', 'A linked account with that nickname already exists.')
        if self.store.find_link_by_fingerprint(owner_userid, routing, acct) is not None:
            raise LinkError('link_duplicate', 'That external account is already linked.')
        open_rows = [row for row in self.store.list_links(owner_userid) if row.status in OPEN_STATUSES]
        if len(open_rows) >= self.policy.max_links:
            raise LinkError('link_limit', 'Linked-account limit reached.')
        now = float(self.clock())
        link = LinkedAccount(
            link_id=uuid.uuid4().hex,
            userid=owner_userid,
            nickname=name,
            routing_last4=routing,
            account_last4=acct,
            default_account=account,
            method=kind,
            status=LINK_PENDING,
            challenge_digest='',
            attempts=0,
            resends=0,
            actor=str(actor),
            created_at=now,
            updated_at=now,
            expires_at=now,
        )
        self._issue_challenge(link)
        self.store.put_link(link)
        return link

    def get_link(self, *, link_id: str, actor: str, actor_type: str) -> LinkedAccount:
        self._require_enabled()
        self.expire_due()
        link = self.store.get_link(link_id)
        if link is None:
            raise LinkError('link_not_found', 'Linked account not found.')
        if actor_type not in EMPLOYEE_ROLES and link.userid != actor:
            raise LinkError('link_forbidden', 'Not allowed to view this linked account.')
        return self.store.get_link(link_id) or link

    def enforce_link(
        self,
        *,
        link_id: str,
        actor: str,
        actor_type: str,
        require_verified: bool = True,
    ) -> LinkedAccount:
        """Reusable gate: a destination/source must be a verified, unpaused link."""
        link = self.get_link(link_id=link_id, actor=actor, actor_type=actor_type)
        if link.status == LINK_CLOSED:
            raise LinkError('already_closed', 'Linked account is closed.')
        if link.status == LINK_REJECTED:
            raise LinkError('already_rejected', 'Prenote was rejected.')
        if link.status == LINK_LOCKED:
            raise LinkError('link_locked', 'Linked account is locked after too many attempts.')
        if link.status == LINK_EXPIRED:
            raise LinkError('link_expired', 'Verification challenge expired.')
        if link.status == LINK_PAUSED:
            raise LinkError('link_paused', 'Linked account is paused.')
        if require_verified and link.status != LINK_VERIFIED:
            raise LinkError('link_not_verified', 'External account is not verified.')
        return link

    def confirm_micro(
        self,
        *,
        link_id: str,
        actor: str,
        actor_type: str,
        amount1: Any,
        amount2: Any,
    ) -> LinkedAccount:
        self._require_manage(actor_type)
        link = self.get_link(link_id=link_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and link.userid != actor:
            raise LinkError('link_forbidden', 'Not allowed to confirm this linked account.')
        if link.method == METHOD_PRENOTE:
            raise LinkError('prenote_pending', 'Prenotes are confirmed by the bank, not by amounts.')
        if link.status == LINK_VERIFIED:
            raise LinkError('already_verified', 'Linked account is already verified.')
        if link.status == LINK_LOCKED:
            raise LinkError('link_locked', 'Too many incorrect attempts.')
        if link.status == LINK_EXPIRED:
            raise LinkError('link_expired', 'Challenge expired; resend to try again.')
        if link.status != LINK_PENDING:
            raise LinkError('invalid_status', 'Linked account cannot be confirmed in this state.')
        first = parse_money(amount1)
        second = parse_money(amount2)
        now = float(self.clock())
        if not amounts_match(link.challenge_digest, link.link_id, first, second, self.policy.challenge_secret):
            link.attempts += 1
            link.updated_at = now
            if link.attempts >= self.policy.max_attempts:
                link.status = LINK_LOCKED
                link.locked_at = now
                self.store.update_link(link)
                raise LinkError('link_locked', 'Too many incorrect attempts.', link=link)
            self.store.update_link(link)
            remaining = self.policy.max_attempts - link.attempts
            raise LinkError(
                'amounts_incorrect',
                'Those amounts do not match the micro-deposits.',
                remaining=remaining,
                link=link,
            )
        link.status = LINK_VERIFIED
        link.verified_at = now
        link.updated_at = now
        link.attempts = 0
        self.store.update_link(link)
        return link

    def resend_challenge(
        self,
        *,
        link_id: str,
        actor: str,
        actor_type: str,
    ) -> LinkedAccount:
        self._require_manage(actor_type)
        link = self.get_link(link_id=link_id, actor=actor, actor_type=actor_type)
        if actor_type not in EMPLOYEE_ROLES and link.userid != actor:
            raise LinkError('link_forbidden', 'Not allowed to resend this challenge.')
        if link.method == METHOD_PRENOTE:
            raise LinkError('prenote_pending', 'Prenotes cannot be resent as micro-deposits.')
        if link.status == LINK_VERIFIED:
            raise LinkError('already_verified', 'Linked account is already verified.')
        if link.status in {LINK_CLOSED, LINK_REJECTED}:
            raise LinkError('already_closed', 'Linked account is closed.')
        if link.status == LINK_LOCKED and actor_type not in EMPLOYEE_ROLES:
            raise LinkError('link_locked', 'Locked links can only be reset by staff.')
        if link.resends >= self.policy.max_resends and actor_type not in EMPLOYEE_ROLES:
            raise LinkError('resend_limit', 'Resend limit reached.')
        self._issue_challenge(link, increment_resend=True)
        link.actor = str(actor)
        self.store.update_link(link)
        return link

    def set_link_status(
        self,
        *,
        link_id: str,
        actor: str,
        actor_type: str,
        status: str,
    ) -> LinkedAccount:
        self._require_manage(actor_type)
        link = self.get_link(link_id=link_id, actor=actor, actor_type=actor_type)
        wanted = str(status or '').strip().lower()
        aliases = {
            'pause': LINK_PAUSED, 'hold': LINK_PAUSED,
            'resume': LINK_VERIFIED, 'activate': LINK_VERIFIED, 'unpause': LINK_VERIFIED,
            'close': LINK_CLOSED, 'archive': LINK_CLOSED, 'cancel': LINK_CLOSED,
        }
        wanted = aliases.get(wanted, wanted)
        if wanted not in {LINK_PAUSED, LINK_VERIFIED, LINK_CLOSED}:
            raise LinkError('invalid_status', 'Status must be pause, resume, or close.')
        if link.status in {LINK_CLOSED, LINK_REJECTED}:
            raise LinkError('already_closed', 'Linked account is already closed.')
        now = float(self.clock())
        if wanted == LINK_CLOSED:
            link.status = LINK_CLOSED
        elif wanted == LINK_PAUSED:
            if link.status == LINK_PAUSED:
                raise LinkError('already_paused', 'Linked account is already paused.')
            if link.status != LINK_VERIFIED:
                raise LinkError('link_not_verified', 'Only verified links can be paused.')
            link.status = LINK_PAUSED
        else:
            if link.status == LINK_VERIFIED:
                raise LinkError('already_active', 'Linked account is already active.')
            if link.status != LINK_PAUSED:
                raise LinkError('invalid_status', 'Only a paused link can be resumed.')
            link.status = LINK_VERIFIED
        link.actor = str(actor)
        link.updated_at = now
        self.store.update_link(link)
        return link

    def force_verify(
        self,
        *,
        link_id: str,
        actor: str,
        actor_type: str,
    ) -> LinkedAccount:
        self._require_staff(actor_type)
        link = self.get_link(link_id=link_id, actor=actor, actor_type=actor_type)
        if link.status in {LINK_CLOSED, LINK_REJECTED}:
            raise LinkError('already_closed', 'Linked account is closed.')
        if link.status == LINK_VERIFIED:
            raise LinkError('already_verified', 'Linked account is already verified.')
        now = float(self.clock())
        link.status = LINK_VERIFIED
        link.verified_at = now
        link.updated_at = now
        link.actor = str(actor)
        link.attempts = 0
        self.store.update_link(link)
        return link

    def accept_prenote(
        self,
        *,
        link_id: str,
        actor: str,
        actor_type: str,
    ) -> LinkedAccount:
        self._require_staff(actor_type)
        link = self.get_link(link_id=link_id, actor=actor, actor_type=actor_type)
        if link.method != METHOD_PRENOTE:
            raise LinkError('invalid_method', 'This link is not a prenote.')
        if link.status != LINK_PENDING:
            raise LinkError('invalid_status', 'Only a pending prenote can be accepted.')
        now = float(self.clock())
        if now < link.created_at + float(self.policy.prenote_wait_seconds):
            raise LinkError('too_soon', 'Prenote wait has not elapsed.')
        return self.force_verify(link_id=link_id, actor=actor, actor_type=actor_type)

    def reject_prenote(
        self,
        *,
        link_id: str,
        actor: str,
        actor_type: str,
        note: Any = '',
    ) -> LinkedAccount:
        self._require_staff(actor_type)
        link = self.get_link(link_id=link_id, actor=actor, actor_type=actor_type)
        if link.method != METHOD_PRENOTE:
            raise LinkError('invalid_method', 'This link is not a prenote.')
        if link.status != LINK_PENDING:
            raise LinkError('invalid_status', 'Only a pending prenote can be rejected.')
        now = float(self.clock())
        link.status = LINK_REJECTED
        link.actor = str(actor)
        link.updated_at = now
        self.store.update_link(link)
        return link

    def _post_movement(
        self,
        *,
        owner_userid: str,
        actor: str,
        link: LinkedAccount,
        account: str,
        dollars: Decimal,
        direction: str,
        trace_id: str,
        note: str,
    ) -> LinkedAch:
        if direction == MOVE_PUSH:
            remark = note or ('ach to %s' % link.nickname)
            executor = self.debit_fn
        else:
            remark = note or ('ach from %s' % link.nickname)
            executor = self.credit_fn
        status = PAY_SENT
        fail_note = ''
        if executor is not None:
            try:
                result = executor(account, money_str(dollars), remark)
            except Exception as exc:
                status = PAY_FAILED
                fail_note = str(exc)[:240]
            else:
                kind = _classify_money_result(result)
                if kind == 'nsf':
                    status = PAY_NSF
                    fail_note = str(result)[:240]
                elif kind != 'ok':
                    status = PAY_FAILED
                    fail_note = str(result)[:240]
        return LinkedAch(
            movement_id=uuid.uuid4().hex,
            trace_id=trace_id,
            link_id=link.link_id,
            userid=owner_userid,
            internal_account=account,
            amount=money_str(dollars),
            nickname=link.nickname,
            direction=direction,
            status=status,
            actor=str(actor),
            created_at=float(self.clock()),
            note=fail_note or remark,
        )

    def move(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        link_id: Any,
        amount: Any,
        direction: Any,
        internal_account: Any = None,
        trace_id: Any = None,
        note: Any = '',
    ) -> Tuple[LinkedAch, bool]:
        self._require_move(actor_type)
        if actor_type not in EMPLOYEE_ROLES and actor != owner_userid:
            raise LinkError('link_forbidden', 'Not allowed to move funds for this customer.')
        link = self.enforce_link(
            link_id=str(link_id or '').strip(), actor=actor, actor_type=actor_type,
        )
        if link.userid != owner_userid:
            raise LinkError('link_forbidden', 'Linked account does not belong to this customer.')
        dollars = parse_money(amount)
        self._assert_amount(dollars)
        account = normalize_account(internal_account or link.default_account)
        self._assert_internal_account(owner_userid, account)
        way = normalize_direction(direction)
        trace = normalize_id(trace_id)
        existing = self.store.get_movement_by_trace(trace)
        if existing is not None:
            return existing, False
        if len(self.store.list_movements(owner_userid)) >= self.policy.max_movements:
            raise LinkError('movement_limit', 'Linked ACH history limit reached.')
        movement = self._post_movement(
            owner_userid=owner_userid,
            actor=actor,
            link=link,
            account=account,
            dollars=dollars,
            direction=way,
            trace_id=trace,
            note=normalize_note(note),
        )
        stored = self.store.put_movement(movement)
        if stored.movement_id != movement.movement_id:
            return stored, False
        if stored.status == PAY_NSF:
            raise LinkError('nsf', 'Insufficient funds for linked ACH.', movement=stored)
        if stored.status != PAY_SENT:
            raise LinkError('failed', 'Linked ACH did not complete.', movement=stored)
        return stored, True

    def get_movement(self, *, movement_id: str, actor: str, actor_type: str) -> LinkedAch:
        self._require_enabled()
        movement = self.store.get_movement(movement_id)
        if movement is None:
            raise LinkError('movement_not_found', 'Linked ACH movement not found.')
        if actor_type not in EMPLOYEE_ROLES and movement.userid != actor:
            raise LinkError('link_forbidden', 'Not allowed to view this movement.')
        return movement

    def settle_movement(
        self,
        *,
        movement_id: str,
        actor: str,
        actor_type: str,
    ) -> LinkedAch:
        self._require_staff(actor_type)
        movement = self.get_movement(movement_id=movement_id, actor=actor, actor_type=actor_type)
        if movement.status == PAY_SETTLED:
            raise LinkError('already_settled', 'Movement is already settled.')
        if movement.status != PAY_SENT:
            raise LinkError('not_returnable', 'Only sent movements can be settled.')
        movement.status = PAY_SETTLED
        movement.actor = str(actor)
        self.store.update_movement(movement)
        return movement

    def return_movement(
        self,
        *,
        movement_id: str,
        actor: str,
        actor_type: str,
        reason: Any = 'other',
        note: Any = '',
    ) -> LinkedAch:
        self._require_staff(actor_type)
        movement = self.get_movement(movement_id=movement_id, actor=actor, actor_type=actor_type)
        if movement.status == PAY_RETURNED:
            raise LinkError('already_returned', 'Movement is already returned.')
        if movement.status == PAY_SETTLED:
            raise LinkError('already_settled', 'Settled movements cannot be returned.')
        if movement.status != PAY_SENT:
            raise LinkError('not_returnable', 'Only sent movements can be returned.')
        why = normalize_return_reason(reason)
        if movement.direction == MOVE_PUSH:
            remark = normalize_note(note) or ('ach returned from %s' % movement.nickname)
            executor = self.credit_fn
        else:
            remark = normalize_note(note) or ('ach pull returned to %s' % movement.nickname)
            executor = self.debit_fn
        if executor is not None:
            try:
                result = executor(movement.internal_account, movement.amount, remark)
            except Exception as exc:
                raise LinkError('return_failed', 'Return did not complete.', movement=movement) from exc
            if _classify_money_result(result) != 'ok':
                raise LinkError('return_failed', 'Return did not complete.', movement=movement)
        movement.status = PAY_RETURNED
        movement.reason = why
        movement.note = remark
        movement.returned_at = float(self.clock())
        movement.actor = str(actor)
        self.store.update_movement(movement)
        return movement

    def snapshot(self, userid: str, *, actor: Optional[str] = None, actor_type: str = 'customer') -> Dict[str, Any]:
        self.expire_due(userid)
        links = self.store.list_links(userid)
        movements = self.store.list_movements(userid)
        push_ytd = Decimal('0.00')
        pull_ytd = Decimal('0.00')
        returned = Decimal('0.00')
        for row in movements:
            amount = parse_money(row.amount, allow_zero=True)
            if row.status in {PAY_SENT, PAY_SETTLED}:
                if row.direction == MOVE_PUSH:
                    push_ytd += amount
                else:
                    pull_ytd += amount
            elif row.status == PAY_RETURNED:
                returned += amount
        return {
            'enabled': self.policy.enabled,
            'allow_credit': self.policy.allow_credit,
            'min_amount': money_str(self.policy.min_amount),
            'max_amount': money_str(self.policy.max_amount),
            'max_attempts': self.policy.max_attempts,
            'links': [row.to_dict() for row in links[:40]],
            'movements': [row.to_dict() for row in movements[:40]],
            'ytd_push': money_str(push_ytd),
            'ytd_pull': money_str(pull_ytd),
            'returned_ytd': money_str(returned),
            'pending_count': sum(1 for row in links if row.status == LINK_PENDING),
            'verified_count': sum(1 for row in links if row.status == LINK_VERIFIED),
        }


_SERVICE: Optional[LinkService] = None


def set_service(service: Optional[LinkService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[LinkService]:
    return _SERVICE


def default_store() -> Any:
    kind = os.environ.get('LINK_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryLinkStore()
    path = os.environ.get('LINK_DB', DEFAULT_STORE_PATH)
    return SqliteLinkStore(path)


def build_service(
    *,
    clock: Optional[Callable[[], float]] = None,
    store: Any = None,
    debit_fn: Optional[Callable[..., Any]] = None,
    credit_fn: Optional[Callable[..., Any]] = None,
    accounts_fn: Optional[Callable[[str], Any]] = None,
    amount_fn: Optional[Callable[[], Tuple[Decimal, Decimal]]] = None,
) -> LinkService:
    if store is None:
        store = default_store()
    return LinkService(
        LinkPolicy.from_env(),
        store,
        clock=clock,
        debit_fn=debit_fn,
        credit_fn=credit_fn,
        accounts_fn=accounts_fn,
        amount_fn=amount_fn,
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
        'link_duplicate': 409,
        'link_limit': 409,
        'movement_limit': 409,
        'already_closed': 409,
        'already_paused': 409,
        'already_active': 409,
        'already_verified': 409,
        'already_rejected': 409,
        'already_resolved': 409,
        'already_settled': 409,
        'already_returned': 409,
        'nsf': 409,
        'failed': 409,
        'return_failed': 409,
        'resend_limit': 409,
        'link_forbidden': 403,
        'link_disabled': 403,
        'link_paused': 403,
        'link_locked': 403,
        'link_expired': 403,
        'link_not_verified': 403,
        'credit_not_allowed': 403,
        'prenote_pending': 403,
        'not_returnable': 403,
        'link_not_found': 404,
        'movement_not_found': 404,
        'invalid_amount': 400,
        'invalid_account': 400,
        'invalid_nickname': 400,
        'invalid_last4': 400,
        'invalid_method': 400,
        'invalid_status': 400,
        'invalid_reason': 400,
        'invalid_direction': 400,
        'amount_out_of_range': 400,
        'amounts_incorrect': 400,
        'too_soon': 400,
        'missing_customer_id': 400,
        'missing_link': 400,
        'missing_movement': 400,
    }.get(code, 400)


def _error_body(exc: LinkError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if exc.extra.get('movement') is not None:
        body['movement'] = exc.extra['movement'].to_dict()
    if exc.extra.get('link') is not None:
        body['link'] = exc.extra['link'].to_dict()
    if exc.extra.get('remaining') is not None:
        body['remaining'] = exc.extra['remaining']
    return body


def _handle_errors(fn):
    try:
        return fn()
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except AmountError:
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    except LinkError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)


def handle_list_links(service: LinkService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'LinkedAccounts': service.snapshot(owner, actor=userid, actor_type=actor_type)}), 200


def handle_add_link(service: LinkService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400

    def _run():
        link = service.add_link(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            nickname=values.get('nickname'),
            default_account=values.get('default_account') or values.get('account') or values.get('from_account'),
            routing_last4=values.get('routing_last4'),
            account_last4=values.get('account_last4'),
            method=values.get('method') or METHOD_MICRO,
        )
        return jsonify({
            'message': 'Linked account added',
            'link': link.to_dict(),
            'LinkedAccounts': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201

    return _handle_errors(_run)


def handle_confirm_link(service: LinkService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    link_id = str(values.get('link_id') or '').strip()
    if not link_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_link'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        link = service.confirm_micro(
            link_id=link_id,
            actor=userid,
            actor_type=actor_type,
            amount1=values.get('amount1') or values.get('deposit1'),
            amount2=values.get('amount2') or values.get('deposit2'),
        )
        return jsonify({
            'message': 'Linked account verified',
            'link': link.to_dict(),
            'LinkedAccounts': service.snapshot(link.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_resend_link(service: LinkService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    link_id = str(values.get('link_id') or '').strip()
    if not link_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_link'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        link = service.resend_challenge(link_id=link_id, actor=userid, actor_type=actor_type)
        return jsonify({
            'message': 'Challenge resent',
            'link': link.to_dict(),
            'LinkedAccounts': service.snapshot(link.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def _link_status_route(service: LinkService, status: str, ok_message: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    link_id = str(values.get('link_id') or '').strip()
    if not link_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_link'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        link = service.set_link_status(
            link_id=link_id, actor=userid, actor_type=actor_type, status=status,
        )
        return jsonify({
            'message': ok_message,
            'link': link.to_dict(),
            'LinkedAccounts': service.snapshot(link.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_force_verify(service: LinkService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    link_id = str(values.get('link_id') or '').strip()
    if not link_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_link'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        link = service.force_verify(link_id=link_id, actor=userid, actor_type=actor_type)
        return jsonify({
            'message': 'Linked account verified',
            'link': link.to_dict(),
            'LinkedAccounts': service.snapshot(link.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_accept_prenote(service: LinkService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    link_id = str(values.get('link_id') or '').strip()
    if not link_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_link'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        link = service.accept_prenote(link_id=link_id, actor=userid, actor_type=actor_type)
        return jsonify({
            'message': 'Prenote accepted',
            'link': link.to_dict(),
            'LinkedAccounts': service.snapshot(link.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_reject_prenote(service: LinkService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    link_id = str(values.get('link_id') or '').strip()
    if not link_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_link'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        link = service.reject_prenote(
            link_id=link_id, actor=userid, actor_type=actor_type, note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Prenote rejected',
            'link': link.to_dict(),
            'LinkedAccounts': service.snapshot(link.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_move(service: LinkService, direction: str):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    link_id = str(values.get('link_id') or '').strip()
    if not link_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_link'}), 400

    def _run():
        movement, created = service.move(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            link_id=link_id,
            amount=values.get('amount'),
            direction=direction,
            internal_account=values.get('account') or values.get('internal_account') or values.get('from_account'),
            trace_id=values.get('trace_id'),
            note=values.get('note') or '',
        )
        verb = 'Pushed to linked account' if direction == MOVE_PUSH else 'Pulled from linked account'
        return jsonify({
            'message': verb if created else 'Linked ACH already posted',
            'movement': movement.to_dict(),
            'LinkedAccounts': service.snapshot(owner, actor=userid, actor_type=actor_type),
        }), 201 if created else 200

    return _handle_errors(_run)


def handle_list_movements(service: LinkService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    snapshot = service.snapshot(owner, actor=userid, actor_type=actor_type)
    return jsonify({'movements': snapshot['movements'], 'LinkedAccounts': snapshot}), 200


def handle_return_movement(service: LinkService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    movement_id = str(values.get('movement_id') or values.get('payment_id') or '').strip()
    if not movement_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_movement'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        movement = service.return_movement(
            movement_id=movement_id,
            actor=userid,
            actor_type=actor_type,
            reason=values.get('reason') or 'other',
            note=values.get('note') or '',
        )
        return jsonify({
            'message': 'Linked ACH returned',
            'movement': movement.to_dict(),
            'LinkedAccounts': service.snapshot(movement.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def handle_settle_movement(service: LinkService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    movement_id = str(values.get('movement_id') or values.get('payment_id') or '').strip()
    if not movement_id:
        return jsonify({'message': 'Some data missing', 'error': 'missing_movement'}), 400
    actor_type = session.get('usertype') or 'customer'

    def _run():
        movement = service.settle_movement(
            movement_id=movement_id, actor=userid, actor_type=actor_type,
        )
        return jsonify({
            'message': 'Linked ACH settled',
            'movement': movement.to_dict(),
            'LinkedAccounts': service.snapshot(movement.userid, actor=userid, actor_type=actor_type),
        }), 200

    return _handle_errors(_run)


def attach_link_routes(app, service: LinkService) -> None:
    @app.route('/listLinkedAccounts', methods=['POST', 'GET'])
    def list_linked_accounts_route():
        return handle_list_links(service)

    @app.route('/addLinkedAccount', methods=['POST', 'GET'])
    def add_linked_account_route():
        return handle_add_link(service)

    @app.route('/confirmLinkedAccount', methods=['POST', 'GET'])
    def confirm_linked_account_route():
        return handle_confirm_link(service)

    @app.route('/resendLinkedChallenge', methods=['POST', 'GET'])
    def resend_linked_challenge_route():
        return handle_resend_link(service)

    @app.route('/pauseLinkedAccount', methods=['POST', 'GET'])
    def pause_linked_account_route():
        return _link_status_route(service, LINK_PAUSED, 'Linked account paused')

    @app.route('/resumeLinkedAccount', methods=['POST', 'GET'])
    def resume_linked_account_route():
        return _link_status_route(service, LINK_VERIFIED, 'Linked account resumed')

    @app.route('/closeLinkedAccount', methods=['POST', 'GET'])
    def close_linked_account_route():
        return _link_status_route(service, LINK_CLOSED, 'Linked account closed')

    @app.route('/forceVerifyLinkedAccount', methods=['POST', 'GET'])
    def force_verify_linked_account_route():
        return handle_force_verify(service)

    @app.route('/acceptPrenote', methods=['POST', 'GET'])
    def accept_prenote_route():
        return handle_accept_prenote(service)

    @app.route('/rejectPrenote', methods=['POST', 'GET'])
    def reject_prenote_route():
        return handle_reject_prenote(service)

    @app.route('/pushToLinked', methods=['POST', 'GET'])
    def push_to_linked_route():
        return handle_move(service, MOVE_PUSH)

    @app.route('/pullFromLinked', methods=['POST', 'GET'])
    def pull_from_linked_route():
        return handle_move(service, MOVE_PULL)

    @app.route('/listLinkedAch', methods=['POST', 'GET'])
    def list_linked_ach_route():
        return handle_list_movements(service)

    @app.route('/returnLinkedAch', methods=['POST', 'GET'])
    def return_linked_ach_route():
        return handle_return_movement(service)

    @app.route('/settleLinkedAch', methods=['POST', 'GET'])
    def settle_linked_ach_route():
        return handle_settle_movement(service)
