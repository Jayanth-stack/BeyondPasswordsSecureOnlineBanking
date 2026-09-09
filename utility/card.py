"""Credit-card PIN and lock.

Card-level control, distinct from account freeze (outbound freeze of any
account type) and from closing an account (`Accounts.active`). Checking and
savings are never gated. Credit charges (transfer / withdraw / cheque /
customer approve) require a PIN; a lock blocks charges even for employees.

Stores are pluggable (memory for tests, sqlite WAL for restart-safe default).
PIN hashes reuse the existing bcrypt helpers in `utility.encrypt`.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from flask import jsonify, request, session

from utility.encrypt import check_encrypted_password, encrypt as hash_secret

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
CHARGE_OPERATIONS = frozenset({'transfer', 'withdraw', 'cheque', 'approve'})
INBOUND_OPERATIONS = frozenset({'deposit', 'request'})
LOCK_REASONS = frozenset({'customer', 'lost', 'stolen', 'employee', 'fraud', 'pin_lockout'})
CUSTOMER_REASONS = frozenset({'customer', 'lost', 'stolen'})
EMPLOYEE_ONLY_REASONS = frozenset({'employee', 'fraud'})
SELF_UNLOCK_REASONS = frozenset({'customer', 'lost', 'stolen', 'pin_lockout'})
WILDCARD_OPERATION = '*'


class AccountError(ValueError):
    pass


class PinError(ValueError):
    pass


class CardError(ValueError):
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


def normalize_pin(value: Any, *, min_length: int = 4, max_length: int = 6) -> str:
    if value is None:
        text = ''
    else:
        text = str(value).strip()
    if not text.isdigit() or not (min_length <= len(text) <= max_length):
        raise PinError('invalid_pin')
    return text


def normalize_reason(value: Any, *, default: str = 'customer') -> str:
    text = str(value or default).strip().lower() or default
    if text not in LOCK_REASONS:
        raise CardError('invalid_reason', 'Unknown card lock reason.')
    return text


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


def _own_account_set(own_accounts: Optional[Iterable[Any]]) -> set:
    found = set()
    for item in own_accounts or ():
        try:
            found.add(normalize_account(item))
        except AccountError:
            continue
    return found


def credit_accounts_from_customer_payload(accounts: Any) -> List[str]:
    if not isinstance(accounts, dict):
        return []
    item = accounts.get('credit')
    if isinstance(item, dict) and item.get('Account') not in (None, 'None', ''):
        try:
            return [normalize_account(item['Account'])]
        except AccountError:
            return []
    return []


def hash_pin(pin: str) -> str:
    return hash_secret(pin)


def verify_pin_hash(pin: str, hashed: str) -> bool:
    try:
        return bool(hashed) and check_encrypted_password(pin, hashed)
    except Exception:
        return False


@dataclass(frozen=True)
class CardDecision:
    action: str
    reason: str = 'ok'
    card: Optional['Card'] = None
    remaining: Optional[int] = None

    @property
    def blocked(self) -> bool:
        return self.action == 'block'


@dataclass
class Card:
    card_id: str
    userid: str
    account: str
    pin_hash: Optional[str]
    failed_attempts: int
    locked: bool
    lock_reason: Optional[str]
    lock_actor: Optional[str]
    lock_actor_type: Optional[str]
    locked_at: Optional[float]
    unlocked_at: Optional[float]
    unlocked_by: Optional[str]
    pin_set_at: Optional[float]
    pin_updated_at: Optional[float]
    created_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            'card_id': self.card_id,
            'userid': self.userid,
            'account': self.account,
            'pin_set': bool(self.pin_hash),
            'failed_attempts': self.failed_attempts,
            'locked': self.locked,
            'lock_reason': self.lock_reason,
            'lock_actor': self.lock_actor,
            'lock_actor_type': self.lock_actor_type,
            'locked_at': self.locked_at,
            'unlocked_at': self.unlocked_at,
            'unlocked_by': self.unlocked_by,
            'pin_set_at': self.pin_set_at,
            'pin_updated_at': self.pin_updated_at,
            'created_at': self.created_at,
        }


@dataclass
class PinToken:
    token: str
    userid: str
    account: str
    operation: str
    created_at: float
    expires_at: float
    used_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'token': self.token,
            'userid': self.userid,
            'account': self.account,
            'operation': self.operation,
            'created_at': self.created_at,
            'expires_at': self.expires_at,
            'used': self.used_at is not None,
        }


@dataclass(frozen=True)
class CardPolicy:
    enabled: bool = True
    pin_required: bool = True
    pin_not_set_blocks: bool = True
    pin_min_length: int = 4
    pin_max_length: int = 6
    max_failed_attempts: int = 3
    lockout_on_fail: bool = True
    customer_self_lock: bool = True
    customer_self_unlock: bool = True
    employee_skip_pin: bool = True
    pin_token_ttl: int = 300
    operations: frozenset = field(default_factory=lambda: frozenset(CHARGE_OPERATIONS))

    @classmethod
    def from_env(cls) -> 'CardPolicy':
        operations = os.environ.get('CARD_OPERATIONS', 'transfer,withdraw,cheque,approve')
        parsed = frozenset(part.strip() for part in operations.split(',') if part.strip())
        return cls(
            enabled=_env_bool('CARD_ENABLED', True),
            pin_required=_env_bool('CARD_PIN_REQUIRED', True),
            pin_not_set_blocks=_env_bool('CARD_PIN_NOT_SET_BLOCKS', True),
            pin_min_length=max(4, _env_int('CARD_PIN_MIN', 4)),
            pin_max_length=max(4, _env_int('CARD_PIN_MAX', 6)),
            max_failed_attempts=max(1, _env_int('CARD_MAX_FAILED', 3)),
            lockout_on_fail=_env_bool('CARD_LOCKOUT_ON_FAIL', True),
            customer_self_lock=_env_bool('CARD_CUSTOMER_LOCK', True),
            customer_self_unlock=_env_bool('CARD_CUSTOMER_UNLOCK', True),
            employee_skip_pin=_env_bool('CARD_EMPLOYEE_SKIP_PIN', True),
            pin_token_ttl=max(30, _env_int('CARD_PIN_TOKEN_TTL', 300)),
            operations=parsed or frozenset(CHARGE_OPERATIONS),
        )

    def evaluate_lock(self, operation: str, card: Optional[Card]) -> CardDecision:
        if not self.enabled or card is None or not card.locked:
            return CardDecision('proceed', 'ok', card)
        if operation in INBOUND_OPERATIONS:
            return CardDecision('proceed', 'inbound_allowed', card)
        if operation not in self.operations:
            return CardDecision('proceed', 'ungated_operation', card)
        return CardDecision('block', 'card_locked', card)


class MemoryCardStore:
    def __init__(self) -> None:
        self._cards: Dict[str, Card] = {}
        self._by_account: Dict[str, str] = {}
        self._tokens: Dict[str, PinToken] = {}
        self._lock = threading.Lock()

    def put_card(self, card: Card) -> None:
        with self._lock:
            self._cards[card.card_id] = card
            self._by_account[card.account] = card.card_id

    def get_card(self, card_id: str) -> Optional[Card]:
        with self._lock:
            return self._cards.get(card_id)

    def get_by_account(self, account: str) -> Optional[Card]:
        with self._lock:
            card_id = self._by_account.get(account)
            return self._cards.get(card_id) if card_id else None

    def update_card(self, card: Card) -> None:
        with self._lock:
            self._cards[card.card_id] = card
            self._by_account[card.account] = card.card_id

    def list_cards(self, userid: Optional[str] = None) -> List[Card]:
        with self._lock:
            rows = list(self._cards.values())
        if userid is not None:
            rows = [row for row in rows if row.userid == userid]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows

    def put_token(self, token: PinToken) -> None:
        with self._lock:
            self._tokens[token.token] = token

    def get_token(self, token: str) -> Optional[PinToken]:
        with self._lock:
            return self._tokens.get(token)

    def update_token(self, token: PinToken) -> None:
        with self._lock:
            self._tokens[token.token] = token


class SqliteCardStore:
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
        return conn

    def _init(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cards (
                    card_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL UNIQUE,
                    pin_hash TEXT,
                    failed_attempts INTEGER NOT NULL,
                    locked INTEGER NOT NULL,
                    lock_reason TEXT,
                    lock_actor TEXT,
                    lock_actor_type TEXT,
                    locked_at REAL,
                    unlocked_at REAL,
                    unlocked_by TEXT,
                    pin_set_at REAL,
                    pin_updated_at REAL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pin_tokens (
                    token TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used_at REAL
                )
                """
            )
            conn.execute('CREATE INDEX IF NOT EXISTS idx_cards_user ON cards(userid)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_cards_account ON cards(account)')
            conn.commit()

    def put_card(self, card: Card) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cards (
                    card_id, userid, account, pin_hash, failed_attempts, locked,
                    lock_reason, lock_actor, lock_actor_type, locked_at,
                    unlocked_at, unlocked_by, pin_set_at, pin_updated_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._card_row(card),
            )
            conn.commit()

    def get_card(self, card_id: str) -> Optional[Card]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM cards WHERE card_id = ?', (card_id,)).fetchone()
        return self._card_from_row(row) if row else None

    def get_by_account(self, account: str) -> Optional[Card]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM cards WHERE account = ?', (account,)).fetchone()
        return self._card_from_row(row) if row else None

    def update_card(self, card: Card) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE cards SET
                    userid=?, account=?, pin_hash=?, failed_attempts=?, locked=?,
                    lock_reason=?, lock_actor=?, lock_actor_type=?, locked_at=?,
                    unlocked_at=?, unlocked_by=?, pin_set_at=?, pin_updated_at=?
                WHERE card_id=?
                """,
                (
                    card.userid, card.account, card.pin_hash, card.failed_attempts,
                    1 if card.locked else 0, card.lock_reason, card.lock_actor,
                    card.lock_actor_type, card.locked_at, card.unlocked_at,
                    card.unlocked_by, card.pin_set_at, card.pin_updated_at,
                    card.card_id,
                ),
            )
            conn.commit()

    def list_cards(self, userid: Optional[str] = None) -> List[Card]:
        sql = 'SELECT * FROM cards'
        params: Tuple[Any, ...] = ()
        if userid is not None:
            sql += ' WHERE userid = ?'
            params = (userid,)
        sql += ' ORDER BY created_at DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._card_from_row(row) for row in rows]

    def put_token(self, token: PinToken) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pin_tokens (
                    token, userid, account, operation, created_at, expires_at, used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token.token, token.userid, token.account, token.operation,
                    token.created_at, token.expires_at, token.used_at,
                ),
            )
            conn.commit()

    def get_token(self, token: str) -> Optional[PinToken]:
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT * FROM pin_tokens WHERE token = ?', (token,)).fetchone()
        return self._token_from_row(row) if row else None

    def update_token(self, token: PinToken) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                'UPDATE pin_tokens SET used_at=? WHERE token=?',
                (token.used_at, token.token),
            )
            conn.commit()

    @staticmethod
    def _card_row(card: Card) -> Tuple[Any, ...]:
        return (
            card.card_id, card.userid, card.account, card.pin_hash,
            card.failed_attempts, 1 if card.locked else 0, card.lock_reason,
            card.lock_actor, card.lock_actor_type, card.locked_at,
            card.unlocked_at, card.unlocked_by, card.pin_set_at,
            card.pin_updated_at, card.created_at,
        )

    @staticmethod
    def _card_from_row(row: sqlite3.Row) -> Card:
        return Card(
            card_id=row['card_id'],
            userid=row['userid'],
            account=row['account'],
            pin_hash=row['pin_hash'],
            failed_attempts=int(row['failed_attempts'] or 0),
            locked=bool(row['locked']),
            lock_reason=row['lock_reason'],
            lock_actor=row['lock_actor'],
            lock_actor_type=row['lock_actor_type'],
            locked_at=row['locked_at'],
            unlocked_at=row['unlocked_at'],
            unlocked_by=row['unlocked_by'],
            pin_set_at=row['pin_set_at'],
            pin_updated_at=row['pin_updated_at'],
            created_at=float(row['created_at']),
        )

    @staticmethod
    def _token_from_row(row: sqlite3.Row) -> PinToken:
        return PinToken(
            token=row['token'],
            userid=row['userid'],
            account=row['account'],
            operation=row['operation'],
            created_at=float(row['created_at']),
            expires_at=float(row['expires_at']),
            used_at=row['used_at'],
        )


class CardService:
    def __init__(
        self,
        policy: Optional[CardPolicy] = None,
        store: Optional[Any] = None,
        clock: Any = time.time,
        hash_pin: Callable[[str], str] = hash_pin,
        verify_pin: Callable[[str, str], bool] = verify_pin_hash,
        is_credit_loader: Optional[Callable[[str, Optional[str]], bool]] = None,
    ) -> None:
        self.policy = policy or CardPolicy()
        self.store = store or MemoryCardStore()
        self.clock = clock
        self.hash_pin = hash_pin
        self.verify_pin = verify_pin
        self.is_credit_loader = is_credit_loader

    def _actor_is_employee(self, actor_type: str) -> bool:
        return (actor_type or '') in EMPLOYEE_ROLES

    def _assert_ownership(
        self,
        *,
        account: str,
        actor_type: str,
        own_accounts: Optional[Iterable[Any]],
    ) -> None:
        if self._actor_is_employee(actor_type) or own_accounts is None:
            return
        if account not in _own_account_set(own_accounts):
            raise CardError('card_forbidden', 'Card does not belong to this customer.')

    def is_credit_account(self, account: str, userid: Optional[str] = None) -> bool:
        if self.store.get_by_account(account) is not None:
            return True
        if callable(self.is_credit_loader):
            try:
                return bool(self.is_credit_loader(account, userid))
            except Exception:
                return False
        return False

    def ensure_card(self, *, userid: str, account: Any) -> Card:
        account_text = normalize_account(account)
        existing = self.store.get_by_account(account_text)
        if existing is not None:
            return existing
        card = Card(
            card_id=uuid.uuid4().hex,
            userid=str(userid),
            account=account_text,
            pin_hash=None,
            failed_attempts=0,
            locked=False,
            lock_reason=None,
            lock_actor=None,
            lock_actor_type=None,
            locked_at=None,
            unlocked_at=None,
            unlocked_by=None,
            pin_set_at=None,
            pin_updated_at=None,
            created_at=float(self.clock()),
        )
        self.store.put_card(card)
        return card

    def set_pin(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        pin: Any,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> Card:
        if not self.policy.enabled:
            raise CardError('card_disabled', 'Card PIN/lock is disabled.')
        account_text = normalize_account(account)
        self._assert_ownership(account=account_text, actor_type=actor_type, own_accounts=own_accounts)
        pin_text = normalize_pin(
            pin,
            min_length=self.policy.pin_min_length,
            max_length=self.policy.pin_max_length,
        )
        card = self.ensure_card(userid=owner_userid, account=account_text)
        if card.userid != str(owner_userid) and not self._actor_is_employee(actor_type):
            raise CardError('card_forbidden', 'Card does not belong to this customer.')
        if card.pin_hash:
            raise CardError('pin_already_set', 'PIN is already set. Use change PIN.')
        now = float(self.clock())
        card.pin_hash = self.hash_pin(pin_text)
        card.pin_set_at = now
        card.pin_updated_at = now
        card.failed_attempts = 0
        self.store.update_card(card)
        return card

    def change_pin(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        current_pin: Any,
        new_pin: Any,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> Card:
        if not self.policy.enabled:
            raise CardError('card_disabled', 'Card PIN/lock is disabled.')
        account_text = normalize_account(account)
        self._assert_ownership(account=account_text, actor_type=actor_type, own_accounts=own_accounts)
        card = self.store.get_by_account(account_text)
        if card is None or not card.pin_hash:
            raise CardError('pin_not_set', 'No PIN is set for this card.')
        if card.userid != str(owner_userid) and not self._actor_is_employee(actor_type):
            raise CardError('card_forbidden', 'Card does not belong to this customer.')
        if card.locked and not self._actor_is_employee(actor_type):
            raise CardError('card_locked', 'Unlock the card before changing PIN.')
        current = normalize_pin(
            current_pin,
            min_length=self.policy.pin_min_length,
            max_length=self.policy.pin_max_length,
        )
        new_text = normalize_pin(
            new_pin,
            min_length=self.policy.pin_min_length,
            max_length=self.policy.pin_max_length,
        )
        if current == new_text:
            raise CardError('pin_reuse', 'New PIN must be different.')
        if not self.verify_pin(current, card.pin_hash):
            self._record_failure(card)
            remaining = max(0, self.policy.max_failed_attempts - card.failed_attempts)
            raise CardError(
                'pin_incorrect',
                'Current PIN is incorrect.',
                remaining=remaining,
                card=card,
            )
        card.pin_hash = self.hash_pin(new_text)
        card.pin_updated_at = float(self.clock())
        card.failed_attempts = 0
        self.store.update_card(card)
        return card

    def reset_pin(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
    ) -> Card:
        if not self._actor_is_employee(actor_type):
            raise CardError('card_forbidden', 'Only bank staff can reset a PIN.')
        account_text = normalize_account(account)
        card = self.ensure_card(userid=owner_userid, account=account_text)
        card.pin_hash = None
        card.pin_set_at = None
        card.pin_updated_at = float(self.clock())
        card.failed_attempts = 0
        self.store.update_card(card)
        return card

    def verify_and_issue_token(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        pin: Any,
        operation: str = WILDCARD_OPERATION,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> Tuple[Card, PinToken]:
        account_text = normalize_account(account)
        self._assert_ownership(account=account_text, actor_type=actor_type, own_accounts=own_accounts)
        card = self._require_unlocked_with_pin(account_text, owner_userid, actor_type)
        pin_text = normalize_pin(
            pin,
            min_length=self.policy.pin_min_length,
            max_length=self.policy.pin_max_length,
        )
        if not self.verify_pin(pin_text, card.pin_hash or ''):
            self._record_failure(card)
            remaining = max(0, self.policy.max_failed_attempts - card.failed_attempts)
            code = 'card_locked' if card.locked else 'pin_incorrect'
            message = (
                'Card locked after too many incorrect PIN attempts.'
                if card.locked else
                'PIN is incorrect.'
            )
            raise CardError(code, message, remaining=remaining, card=card)
        card.failed_attempts = 0
        self.store.update_card(card)
        now = float(self.clock())
        token = PinToken(
            token=uuid.uuid4().hex,
            userid=str(owner_userid),
            account=account_text,
            operation=str(operation or WILDCARD_OPERATION),
            created_at=now,
            expires_at=now + self.policy.pin_token_ttl,
        )
        self.store.put_token(token)
        return card, token

    def consume_token(
        self,
        *,
        token: str,
        userid: str,
        account: str,
        operation: str,
    ) -> PinToken:
        found = self.store.get_token(str(token or '').strip())
        if found is None:
            raise CardError('pin_token_invalid', 'PIN confirmation is missing or invalid.')
        now = float(self.clock())
        if found.used_at is not None:
            raise CardError('pin_token_invalid', 'PIN confirmation has already been used.')
        if found.expires_at < now:
            raise CardError('pin_token_expired', 'PIN confirmation has expired.')
        if found.userid != str(userid) or found.account != account:
            raise CardError('pin_token_invalid', 'PIN confirmation does not match this charge.')
        if found.operation not in {WILDCARD_OPERATION, operation}:
            raise CardError('pin_token_invalid', 'PIN confirmation does not match this operation.')
        found.used_at = now
        self.store.update_token(found)
        return found

    def lock_card(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        reason: Any = None,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> Card:
        if not self.policy.enabled:
            raise CardError('card_disabled', 'Card PIN/lock is disabled.')
        employee = self._actor_is_employee(actor_type)
        if not employee and not self.policy.customer_self_lock:
            raise CardError('card_forbidden', 'Customers cannot lock cards.')
        reason_text = normalize_reason(reason, default='employee' if employee else 'lost')
        if not employee and reason_text in EMPLOYEE_ONLY_REASONS:
            raise CardError('card_forbidden', 'Customers cannot apply that lock reason.')
        if reason_text == 'pin_lockout' and not employee:
            raise CardError('card_forbidden', 'PIN lockout is applied automatically.')
        account_text = normalize_account(account)
        self._assert_ownership(account=account_text, actor_type=actor_type, own_accounts=own_accounts)
        card = self.ensure_card(userid=owner_userid, account=account_text)
        if card.userid != str(owner_userid) and not employee:
            raise CardError('card_forbidden', 'Card does not belong to this customer.')
        if card.locked:
            raise CardError('lock_duplicate', 'Card is already locked.', card=card)
        now = float(self.clock())
        card.locked = True
        card.lock_reason = reason_text
        card.lock_actor = str(actor)
        card.lock_actor_type = str(actor_type or 'customer')
        card.locked_at = now
        self.store.update_card(card)
        return card

    def unlock_card(
        self,
        *,
        owner_userid: str,
        actor: str,
        actor_type: str,
        account: Any,
        pin: Any = None,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> Card:
        account_text = normalize_account(account)
        self._assert_ownership(account=account_text, actor_type=actor_type, own_accounts=own_accounts)
        card = self.store.get_by_account(account_text)
        if card is None:
            raise CardError('card_not_found', 'Card not found.')
        employee = self._actor_is_employee(actor_type)
        if not employee:
            if card.userid != str(owner_userid or actor):
                raise CardError('card_forbidden', 'Card does not belong to this customer.')
            if not self.policy.customer_self_unlock:
                raise CardError('card_forbidden', 'Customers cannot unlock cards.')
            if (card.lock_reason or 'customer') not in SELF_UNLOCK_REASONS:
                raise CardError('card_lock_locked', 'Only bank staff can release this card lock.')
            if card.pin_hash:
                pin_text = normalize_pin(
                    pin,
                    min_length=self.policy.pin_min_length,
                    max_length=self.policy.pin_max_length,
                )
                if not self.verify_pin(pin_text, card.pin_hash):
                    self._record_failure(card)
                    remaining = max(0, self.policy.max_failed_attempts - card.failed_attempts)
                    raise CardError(
                        'pin_incorrect',
                        'PIN is incorrect.',
                        remaining=remaining,
                        card=card,
                    )
        if not card.locked:
            raise CardError('card_not_locked', 'Card is not locked.', card=card)
        card.locked = False
        card.unlocked_at = float(self.clock())
        card.unlocked_by = str(actor)
        card.failed_attempts = 0
        self.store.update_card(card)
        return card

    def evaluate(
        self,
        *,
        operation: str,
        account: Any = None,
        userid: Optional[str] = None,
        actor_type: str = 'customer',
        pin: Any = None,
        pin_token: Any = None,
    ) -> CardDecision:
        if not self.policy.enabled:
            return CardDecision('proceed', 'disabled')
        if operation in INBOUND_OPERATIONS:
            return CardDecision('proceed', 'inbound_allowed')
        if operation not in self.policy.operations:
            return CardDecision('proceed', 'ungated_operation')
        try:
            account_text = normalize_account(account, required=True)
        except AccountError:
            return CardDecision('proceed', 'not_credit')
        if not self.is_credit_account(account_text, userid):
            return CardDecision('proceed', 'not_credit')

        card = None
        if userid:
            card = self.ensure_card(userid=userid, account=account_text)
        else:
            card = self.store.get_by_account(account_text)
        if card is None:
            return CardDecision('proceed', 'not_credit')

        lock_decision = self.policy.evaluate_lock(operation, card)
        if lock_decision.blocked:
            return lock_decision

        employee = self._actor_is_employee(actor_type)
        if employee and self.policy.employee_skip_pin:
            return CardDecision('proceed', 'employee_skip', card)
        if not self.policy.pin_required:
            return CardDecision('proceed', 'pin_not_required', card)
        if not card.pin_hash:
            if self.policy.pin_not_set_blocks:
                return CardDecision('block', 'pin_not_set', card)
            return CardDecision('proceed', 'pin_optional', card)

        if pin_token:
            try:
                self.consume_token(
                    token=str(pin_token),
                    userid=str(userid or card.userid),
                    account=account_text,
                    operation=operation,
                )
            except CardError as exc:
                return CardDecision('block', exc.code, card)
            return CardDecision('proceed', 'pin_token', card)

        if pin in (None, ''):
            return CardDecision('block', 'pin_required', card)
        try:
            pin_text = normalize_pin(
                pin,
                min_length=self.policy.pin_min_length,
                max_length=self.policy.pin_max_length,
            )
        except PinError:
            return CardDecision('block', 'invalid_pin', card)
        if not self.verify_pin(pin_text, card.pin_hash):
            self._record_failure(card)
            remaining = max(0, self.policy.max_failed_attempts - card.failed_attempts)
            reason = 'card_locked' if card.locked else 'pin_incorrect'
            return CardDecision('block', reason, card, remaining=remaining)
        card.failed_attempts = 0
        self.store.update_card(card)
        return CardDecision('proceed', 'pin_ok', card)

    def snapshot(self, userid: str) -> Dict[str, Any]:
        cards = [row.to_dict() for row in self.store.list_cards(userid)]
        locked = sorted({row['account'] for row in cards if row['locked']})
        pin_set = sorted({row['account'] for row in cards if row['pin_set']})
        return {
            'enabled': self.policy.enabled,
            'pin_required': self.policy.pin_required,
            'pin_not_set_blocks': self.policy.pin_not_set_blocks,
            'pin_min_length': self.policy.pin_min_length,
            'pin_max_length': self.policy.pin_max_length,
            'max_failed_attempts': self.policy.max_failed_attempts,
            'operations': sorted(self.policy.operations),
            'locked_accounts': locked,
            'pin_set_accounts': pin_set,
            'cards': cards,
        }

    def _require_unlocked_with_pin(
        self,
        account: str,
        owner_userid: str,
        actor_type: str,
    ) -> Card:
        card = self.store.get_by_account(account)
        if card is None or not card.pin_hash:
            raise CardError('pin_not_set', 'No PIN is set for this card.')
        if card.userid != str(owner_userid) and not self._actor_is_employee(actor_type):
            raise CardError('card_forbidden', 'Card does not belong to this customer.')
        if card.locked:
            raise CardError('card_locked', 'This card is locked.')
        return card

    def _record_failure(self, card: Card) -> None:
        card.failed_attempts = int(card.failed_attempts or 0) + 1
        if self.policy.lockout_on_fail and card.failed_attempts >= self.policy.max_failed_attempts:
            card.locked = True
            card.lock_reason = 'pin_lockout'
            card.lock_actor = 'system'
            card.lock_actor_type = 'system'
            card.locked_at = float(self.clock())
        self.store.update_card(card)


def default_store() -> Any:
    kind = os.environ.get('CARD_STORE', 'sqlite').strip().lower()
    if kind in {'memory', 'mem'}:
        return MemoryCardStore()
    path = os.environ.get('CARD_DB', 'SystemLogs/card.sqlite')
    return SqliteCardStore(path)


def build_service(is_credit_loader=None) -> CardService:
    return CardService(
        CardPolicy.from_env(),
        default_store(),
        is_credit_loader=is_credit_loader,
    )


def _blocked_payload(decision: CardDecision, operation: str) -> Dict[str, Any]:
    reason = decision.reason
    messages = {
        'card_locked': 'This credit card is locked. Charges are blocked.',
        'pin_not_set': 'Set a PIN before charging this credit card.',
        'pin_required': 'PIN is required for this credit-card charge.',
        'pin_incorrect': 'PIN is incorrect.',
        'invalid_pin': 'PIN must be 4-6 digits.',
        'pin_token_invalid': 'PIN confirmation is missing or invalid.',
        'pin_token_expired': 'PIN confirmation has expired. Verify PIN again.',
    }
    payload = {
        'message': messages.get(reason, 'Credit-card charge was blocked.'),
        'error': reason,
        'operation': operation,
    }
    if decision.remaining is not None:
        payload['remaining'] = decision.remaining
    if decision.card is not None:
        payload['card'] = decision.card.to_dict()
    return payload


def enforce_card(
    service: CardService,
    *,
    operation: str,
    account: Any = None,
    userid: Optional[str] = None,
    actor_type: str = 'customer',
    pin: Any = None,
    pin_token: Any = None,
) -> Optional[Tuple[Dict[str, Any], int]]:
    """Return (body, status) to short-circuit, or None to run the existing handler."""
    if account not in (None, ''):
        try:
            normalize_account(account, required=True)
        except AccountError:
            return {'message': 'Invalid account', 'error': 'invalid_account'}, 400

    decision = service.evaluate(
        operation=operation,
        account=account,
        userid=userid,
        actor_type=actor_type,
        pin=pin,
        pin_token=pin_token,
    )
    if not decision.blocked:
        return None
    status = 403
    return _blocked_payload(decision, operation), status


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
        'pin_already_set': 409,
        'lock_duplicate': 409,
        'card_not_locked': 409,
        'card_forbidden': 403,
        'card_lock_locked': 403,
        'card_locked': 403,
        'card_disabled': 403,
        'pin_incorrect': 403,
        'pin_not_set': 403,
        'pin_required': 403,
        'pin_token_invalid': 403,
        'pin_token_expired': 403,
        'card_not_found': 404,
        'invalid_pin': 400,
        'invalid_account': 400,
        'invalid_reason': 400,
        'pin_reuse': 400,
        'missing_customer_id': 400,
    }.get(code, 400)


def _error_body(exc: CardError) -> Dict[str, Any]:
    body = {'message': exc.message, 'error': exc.code}
    if 'remaining' in exc.extra:
        body['remaining'] = exc.extra['remaining']
    card = exc.extra.get('card')
    if card is not None:
        body['card'] = card.to_dict()
    return body


def handle_set_pin(service: CardService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    own_accounts = None
    if actor_type == 'customer' and callable(own_accounts_loader):
        own_accounts = own_accounts_loader(userid)
    try:
        card = service.set_pin(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            pin=values.get('pin'),
            own_accounts=own_accounts,
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except PinError:
        return jsonify({'message': 'PIN must be 4-6 digits', 'error': 'invalid_pin'}), 400
    except CardError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({'message': 'PIN set', 'card': card.to_dict(), 'Cards': service.snapshot(owner)}), 200


def handle_change_pin(service: CardService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values) or userid
    own_accounts = None
    if actor_type == 'customer' and callable(own_accounts_loader):
        own_accounts = own_accounts_loader(userid)
    try:
        card = service.change_pin(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            current_pin=values.get('current_pin') or values.get('pin'),
            new_pin=values.get('new_pin'),
            own_accounts=own_accounts,
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except PinError:
        return jsonify({'message': 'PIN must be 4-6 digits', 'error': 'invalid_pin'}), 400
    except CardError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({'message': 'PIN changed', 'card': card.to_dict(), 'Cards': service.snapshot(owner)}), 200


def handle_reset_pin(service: CardService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    try:
        card = service.reset_pin(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except CardError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({'message': 'PIN reset', 'card': card.to_dict(), 'Cards': service.snapshot(owner)}), 200


def handle_verify_pin(service: CardService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values) or userid
    own_accounts = None
    if actor_type == 'customer' and callable(own_accounts_loader):
        own_accounts = own_accounts_loader(userid)
    try:
        card, token = service.verify_and_issue_token(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            pin=values.get('pin'),
            operation=str(values.get('operation') or WILDCARD_OPERATION),
            own_accounts=own_accounts,
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except PinError:
        return jsonify({'message': 'PIN must be 4-6 digits', 'error': 'invalid_pin'}), 400
    except CardError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({
        'message': 'PIN verified',
        'pin_token': token.token,
        'expires_at': token.expires_at,
        'card': card.to_dict(),
        'Cards': service.snapshot(owner),
    }), 200


def handle_lock_card(service: CardService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values)
    if not owner:
        return jsonify({'message': 'Some data missing', 'error': 'missing_customer_id'}), 400
    own_accounts = None
    if actor_type == 'customer' and callable(own_accounts_loader):
        own_accounts = own_accounts_loader(userid)
    try:
        card = service.lock_card(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            reason=values.get('reason'),
            own_accounts=own_accounts,
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except CardError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({'message': 'Card locked', 'card': card.to_dict(), 'Cards': service.snapshot(owner)}), 200


def handle_unlock_card(service: CardService, own_accounts_loader=None):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = _owner_userid(userid, actor_type, values) or userid
    own_accounts = None
    if actor_type == 'customer' and callable(own_accounts_loader):
        own_accounts = own_accounts_loader(userid)
    try:
        card = service.unlock_card(
            owner_userid=owner,
            actor=userid,
            actor_type=actor_type,
            account=values.get('account'),
            pin=values.get('pin'),
            own_accounts=own_accounts,
        )
    except AccountError:
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    except PinError:
        return jsonify({'message': 'PIN must be 4-6 digits', 'error': 'invalid_pin'}), 400
    except CardError as exc:
        return jsonify(_error_body(exc)), _error_status(exc.code)
    return jsonify({'message': 'Card unlocked', 'card': card.to_dict(), 'Cards': service.snapshot(owner)}), 200


def handle_list_cards(service: CardService):
    userid, error = _require_session_user()
    if error:
        return error
    values = request.get_json(silent=True) or {}
    actor_type = session.get('usertype') or 'customer'
    owner = userid
    if actor_type in EMPLOYEE_ROLES:
        owner = str(values.get('customer_id') or userid).strip() or userid
    return jsonify({'Cards': service.snapshot(owner)}), 200


def attach_card_routes(app, service: CardService, own_accounts_loader=None) -> None:
    @app.route('/setPin', methods=['POST', 'GET'])
    def set_pin_route():
        return handle_set_pin(service, own_accounts_loader=own_accounts_loader)

    @app.route('/changePin', methods=['POST', 'GET'])
    def change_pin_route():
        return handle_change_pin(service, own_accounts_loader=own_accounts_loader)

    @app.route('/resetPin', methods=['POST', 'GET'])
    def reset_pin_route():
        return handle_reset_pin(service)

    @app.route('/verifyPin', methods=['POST', 'GET'])
    def verify_pin_route():
        return handle_verify_pin(service, own_accounts_loader=own_accounts_loader)

    @app.route('/lockCard', methods=['POST', 'GET'])
    def lock_card_route():
        return handle_lock_card(service, own_accounts_loader=own_accounts_loader)

    @app.route('/unlockCard', methods=['POST', 'GET'])
    def unlock_card_route():
        return handle_unlock_card(service, own_accounts_loader=own_accounts_loader)

    @app.route('/listCards', methods=['POST', 'GET'])
    def list_cards_route():
        return handle_list_cards(service)
