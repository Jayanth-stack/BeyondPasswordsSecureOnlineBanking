"""Spending categories, merchant tags, and monthly budget envelopes.

`Accounts.transaction_history` is still a concatenated HTML blob, and
period statements (PR #52) snapshot uncategorized journal lines. This
module is the reusable tagging layer those surfaces can share later:

- a seeded category taxonomy (system + customer custom)
- merchant tags and keyword rules that auto-assign a category
- an append-only movement log with customer/staff recategorize
- monthly budget envelopes with warning/exceeded status

Distinct from:

- Period statements (PR #52) — documents, not merchant MCC / budgets
- Interest posting (PR #49) — yield credits, not spend envelopes
- Velocity (PR #20) — bank-imposed outbound caps, not customer budgets
- Payee allowlist (PR #26) — destination registration, not spend tags
- Audit trail (PR #24) — admin security events

Existing HTML history and `/getTransactionHistory` stay unchanged.
Observe hooks are fail-open so a tagging miss never blocks a debit.

Stores are pluggable (memory for tests, sqlite WAL by default).
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

EMPLOYEE_ROLES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
ENTRY_KINDS = frozenset({
    'credit', 'debit', 'transfer_in', 'transfer_out',
    'deposit', 'withdraw', 'cheque', 'open', 'interest',
})
CREDIT_KINDS = frozenset({'credit', 'transfer_in', 'deposit', 'open', 'interest'})
DEBIT_KINDS = frozenset({'debit', 'transfer_out', 'withdraw', 'cheque'})
CATEGORY_KINDS = frozenset({'system', 'custom'})
BUDGET_STATUSES = ('ok', 'warning', 'exceeded')
MONEY_QUANTUM = Decimal('0.01')
DEFAULT_STORE_PATH = os.path.join('SystemLogs', 'category.sqlite')
UNCATEGORIZED = 'uncategorized'
WARNING_RATIO = Decimal('0.80')

SYSTEM_CATEGORIES: Tuple[Tuple[str, str], ...] = (
    ('groceries', 'Groceries'),
    ('dining', 'Dining'),
    ('transport', 'Transport'),
    ('utilities', 'Utilities'),
    ('housing', 'Housing'),
    ('shopping', 'Shopping'),
    ('healthcare', 'Healthcare'),
    ('entertainment', 'Entertainment'),
    ('travel', 'Travel'),
    ('income', 'Income'),
    ('transfer', 'Transfers'),
    ('cash', 'Cash'),
    (UNCATEGORIZED, 'Uncategorized'),
)

KIND_DEFAULTS = {
    'deposit': 'income',
    'open': 'income',
    'interest': 'income',
    'credit': 'income',
    'transfer_in': 'transfer',
    'transfer_out': 'transfer',
    'withdraw': 'cash',
    'cheque': 'shopping',
}

SYSTEM_KEYWORDS: Tuple[Tuple[str, str], ...] = (
    ('grocery', 'groceries'),
    ('supermarket', 'groceries'),
    ('walmart', 'groceries'),
    ('costco', 'groceries'),
    ('restaurant', 'dining'),
    ('cafe', 'dining'),
    ('doordash', 'dining'),
    ('ubereats', 'dining'),
    ('uber', 'transport'),
    ('lyft', 'transport'),
    ('fuel', 'transport'),
    ('parking', 'transport'),
    ('electric', 'utilities'),
    ('internet', 'utilities'),
    ('verizon', 'utilities'),
    ('rent', 'housing'),
    ('mortgage', 'housing'),
    ('amazon', 'shopping'),
    ('pharmacy', 'healthcare'),
    ('hospital', 'healthcare'),
    ('netflix', 'entertainment'),
    ('spotify', 'entertainment'),
    ('airline', 'travel'),
    ('hotel', 'travel'),
    ('payroll', 'income'),
    ('salary', 'income'),
)

_SERVICE: Optional['CategoryService'] = None
_SLUG_RE = re.compile(r'[^a-z0-9]+')


class AccountError(ValueError):
    pass


class AmountError(ValueError):
    pass


class PeriodError(ValueError):
    pass


class CategoryError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


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
    if raw is None or not str(raw).strip():
        raw = default
    try:
        return parse_money(raw, allow_zero=True)
    except AmountError:
        return Decimal(default)


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


def utc_datetime(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def month_id(ts: float) -> str:
    dt = utc_datetime(ts)
    return f'{dt.year:04d}-{dt.month:02d}'


def parse_month(value: Any) -> Tuple[int, int]:
    text = str(value or '').strip()
    try:
        year_s, month_s = text.split('-', 1)
        year, month = int(year_s), int(month_s)
    except (TypeError, ValueError):
        raise PeriodError('invalid_period') from None
    if month < 1 or month > 12 or year < 1970 or year > 2100:
        raise PeriodError('invalid_period')
    return year, month


def month_start_ts(year: int, month: int) -> float:
    return datetime(year, month, 1, tzinfo=timezone.utc).timestamp()


def month_end_ts(year: int, month: int) -> float:
    if month == 12:
        return datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp()
    return datetime(year, month + 1, 1, tzinfo=timezone.utc).timestamp()


def resolve_month(period: Any = None, *, now: Optional[float] = None) -> Tuple[str, float, float]:
    now = float(now if now is not None else datetime.now(tz=timezone.utc).timestamp())
    if period in (None, ''):
        label = month_id(now)
        year, month = parse_month(label)
    else:
        year, month = parse_month(period)
        label = f'{year:04d}-{month:02d}'
    start = month_start_ts(year, month)
    end = month_end_ts(year, month)
    if start >= now:
        raise CategoryError('period_in_future', 'Cannot inspect a future spending period')
    if end > now:
        end = now
    return label, start, end


def normalize_kind(value: Any) -> str:
    kind = str(value or '').strip().lower()
    aliases = {
        'transfer-in': 'transfer_in',
        'transfer-out': 'transfer_out',
        'xfer_in': 'transfer_in',
        'xfer_out': 'transfer_out',
        'withdrawal': 'withdraw',
        'bonus': 'open',
        'cashier_cheque': 'cheque',
        'check': 'cheque',
        'direct_deposit': 'credit',
    }
    kind = aliases.get(kind, kind)
    if kind not in ENTRY_KINDS:
        raise CategoryError('invalid_kind', 'Unknown movement kind')
    return kind


def normalize_direction(value: Any, kind: Optional[str] = None) -> str:
    text = str(value or '').strip().lower()
    if text in {'credit', 'cr', 'in', '+'}:
        return 'credit'
    if text in {'debit', 'dr', 'out', '-'}:
        return 'debit'
    if kind in CREDIT_KINDS:
        return 'credit'
    if kind in DEBIT_KINDS:
        return 'debit'
    raise CategoryError('invalid_kind', 'Unknown movement direction')


def normalize_label(value: Any, *, max_len: int = 40) -> str:
    text = ' '.join(str(value or '').strip().split())
    if not text:
        raise CategoryError('invalid_category', 'Category label is required')
    if len(text) > max_len:
        raise CategoryError('invalid_category', 'Category label is too long')
    return text


def slugify(value: Any) -> str:
    text = _SLUG_RE.sub('-', str(value or '').strip().lower()).strip('-')
    return text[:40]


def normalize_category_id(value: Any, *, required: bool = True) -> str:
    text = slugify(value)
    if not text:
        if required:
            raise CategoryError('invalid_category', 'Category is required')
        return ''
    return text


def normalize_keyword(value: Any) -> str:
    text = ' '.join(str(value or '').strip().lower().split())
    text = re.sub(r'[^a-z0-9 ]+', '', text).strip()
    if len(text) < 3 or len(text) > 40:
        raise CategoryError('invalid_keyword', 'Keyword must be 3-40 characters')
    return text


def normalize_merchant(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        raise CategoryError('invalid_merchant', 'Merchant is required')
    if text.endswith('.0') and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit() and 1 <= len(text) <= 16:
        return str(int(text))
    key = slugify(text)
    if len(key) < 2:
        raise CategoryError('invalid_merchant', 'Merchant tag is too short')
    return key


def accounts_from_customer_payload(accounts: Any) -> List[str]:
    found: List[str] = []
    if not isinstance(accounts, dict):
        return found
    for key in ('savings', 'checkin', 'checking', 'credit'):
        item = accounts.get(key)
        if isinstance(item, dict) and item.get('Account') not in (None, 'None', ''):
            try:
                found.append(normalize_account(item['Account']))
            except AccountError:
                continue
    return found


def _own_account_set(own_accounts: Optional[Iterable[Any]]) -> set:
    owned = set()
    for item in own_accounts or ():
        try:
            owned.add(normalize_account(item))
        except AccountError:
            continue
    return owned


def _copy_category(item: 'Category') -> 'Category':
    return Category(
        category_id=item.category_id,
        label=item.label,
        kind=item.kind,
        userid=item.userid,
        archived=item.archived,
        created_at=item.created_at,
    )


@dataclass
class CategoryPolicy:
    max_custom_categories: int = 24
    max_merchant_tags: int = 50
    max_rules: int = 40
    max_budgets: int = 24
    min_budget: Decimal = Decimal('1.00')
    max_budget: Decimal = Decimal('100000.00')
    max_label: int = 40
    max_description: int = 240
    warning_ratio: Decimal = WARNING_RATIO

    @classmethod
    def from_env(cls) -> 'CategoryPolicy':
        return cls(
            max_custom_categories=_env_int('CATEGORY_MAX_CUSTOM', 24),
            max_merchant_tags=_env_int('CATEGORY_MAX_MERCHANTS', 50),
            max_rules=_env_int('CATEGORY_MAX_RULES', 40),
            max_budgets=_env_int('CATEGORY_MAX_BUDGETS', 24),
            min_budget=_env_money('CATEGORY_MIN_BUDGET', '1.00'),
            max_budget=_env_money('CATEGORY_MAX_BUDGET', '100000.00'),
        )


@dataclass
class Category:
    category_id: str
    label: str
    kind: str
    userid: str = ''
    archived: bool = False
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'category_id': self.category_id,
            'label': self.label,
            'kind': self.kind,
            'userid': self.userid,
            'archived': self.archived,
            'created_at': self.created_at,
        }


@dataclass
class MerchantTag:
    merchant_id: str
    userid: str
    merchant: str
    category_id: str
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'merchant_id': self.merchant_id,
            'userid': self.userid,
            'merchant': self.merchant,
            'category_id': self.category_id,
            'created_at': self.created_at,
            'scope': 'system' if not self.userid else 'customer',
        }


@dataclass
class CategoryRule:
    rule_id: str
    userid: str
    keyword: str
    category_id: str
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'rule_id': self.rule_id,
            'userid': self.userid,
            'keyword': self.keyword,
            'category_id': self.category_id,
            'created_at': self.created_at,
            'scope': 'system' if not self.userid else 'customer',
        }


@dataclass
class Movement:
    movement_id: str
    userid: str
    account: str
    kind: str
    direction: str
    amount: Decimal
    category_id: str
    counterparty: str = ''
    merchant: str = ''
    description: str = ''
    source_id: str = ''
    posted_at: float = 0.0
    manual: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'movement_id': self.movement_id,
            'userid': self.userid,
            'account': self.account,
            'kind': self.kind,
            'direction': self.direction,
            'amount': money_str(self.amount),
            'category_id': self.category_id,
            'counterparty': self.counterparty,
            'merchant': self.merchant,
            'description': self.description,
            'source_id': self.source_id,
            'posted_at': self.posted_at,
            'manual': self.manual,
        }


@dataclass
class Budget:
    budget_id: str
    userid: str
    category_id: str
    amount: Decimal
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'budget_id': self.budget_id,
            'userid': self.userid,
            'category_id': self.category_id,
            'amount': money_str(self.amount),
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


class MemoryCategoryStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.categories: Dict[str, Category] = {}
        self.merchants: Dict[str, MerchantTag] = {}
        self.rules: Dict[str, CategoryRule] = {}
        self.movements: Dict[str, Movement] = {}
        self.budgets: Dict[str, Budget] = {}
        self._source: Dict[str, str] = {}

    def put_category(self, item: Category) -> None:
        with self._lock:
            self.categories[item.category_id] = item

    def get_category(self, category_id: str) -> Optional[Category]:
        with self._lock:
            item = self.categories.get(category_id)
            return None if item is None else _copy_category(item)

    def list_categories(self, userid: Optional[str] = None, include_archived: bool = False) -> List[Category]:
        with self._lock:
            items = list(self.categories.values())
        if userid is not None:
            items = [item for item in items if item.kind == 'system' or item.userid == userid]
        if not include_archived:
            items = [item for item in items if not item.archived]
        items.sort(key=lambda item: (0 if item.kind == 'system' else 1, item.label.lower()))
        return [_copy_category(item) for item in items]

    def put_merchant(self, item: MerchantTag) -> None:
        with self._lock:
            self.merchants[item.merchant_id] = item

    def get_merchant(self, merchant_id: str) -> Optional[MerchantTag]:
        with self._lock:
            return self.merchants.get(merchant_id)

    def get_merchant_by_key(self, userid: str, merchant: str) -> Optional[MerchantTag]:
        with self._lock:
            for item in self.merchants.values():
                if item.userid == userid and item.merchant == merchant:
                    return item
        return None

    def list_merchants(self, userid: Optional[str] = None) -> List[MerchantTag]:
        with self._lock:
            items = list(self.merchants.values())
        if userid is not None:
            items = [item for item in items if item.userid in {userid, ''}]
        items.sort(key=lambda item: (0 if not item.userid else 1, item.merchant))
        return items

    def delete_merchant(self, merchant_id: str) -> None:
        with self._lock:
            self.merchants.pop(merchant_id, None)

    def put_rule(self, item: CategoryRule) -> None:
        with self._lock:
            self.rules[item.rule_id] = item

    def get_rule(self, rule_id: str) -> Optional[CategoryRule]:
        with self._lock:
            return self.rules.get(rule_id)

    def get_rule_by_keyword(self, userid: str, keyword: str) -> Optional[CategoryRule]:
        with self._lock:
            for item in self.rules.values():
                if item.userid == userid and item.keyword == keyword:
                    return item
        return None

    def list_rules(self, userid: Optional[str] = None) -> List[CategoryRule]:
        with self._lock:
            items = list(self.rules.values())
        if userid is not None:
            items = [item for item in items if item.userid in {userid, ''}]
        items.sort(key=lambda item: (0 if item.userid else 1, -len(item.keyword), item.keyword))
        return items

    def delete_rule(self, rule_id: str) -> None:
        with self._lock:
            self.rules.pop(rule_id, None)

    def put_movement(self, item: Movement) -> Movement:
        with self._lock:
            if item.source_id and item.source_id in self._source:
                existing = self.movements.get(self._source[item.source_id])
                if existing is not None:
                    return existing
            self.movements[item.movement_id] = item
            if item.source_id:
                self._source[item.source_id] = item.movement_id
            return item

    def get_movement(self, movement_id: str) -> Optional[Movement]:
        with self._lock:
            return self.movements.get(movement_id)

    def get_by_source(self, source_id: str) -> Optional[Movement]:
        with self._lock:
            movement_id = self._source.get(source_id)
            return None if movement_id is None else self.movements.get(movement_id)

    def update_movement(self, item: Movement) -> None:
        with self._lock:
            self.movements[item.movement_id] = item
            if item.source_id:
                self._source[item.source_id] = item.movement_id

    def list_movements(
        self,
        *,
        userid: Optional[str] = None,
        account: Optional[str] = None,
        start: Optional[float] = None,
        end: Optional[float] = None,
        category_id: Optional[str] = None,
    ) -> List[Movement]:
        with self._lock:
            items = list(self.movements.values())
        if userid is not None:
            items = [item for item in items if item.userid == userid]
        if account is not None:
            items = [item for item in items if item.account == account]
        if category_id is not None:
            items = [item for item in items if item.category_id == category_id]
        if start is not None:
            items = [item for item in items if item.posted_at >= start]
        if end is not None:
            items = [item for item in items if item.posted_at < end]
        items.sort(key=lambda item: (item.posted_at, item.movement_id), reverse=True)
        return items

    def put_budget(self, item: Budget) -> None:
        with self._lock:
            self.budgets[item.budget_id] = item

    def get_budget(self, budget_id: str) -> Optional[Budget]:
        with self._lock:
            return self.budgets.get(budget_id)

    def get_budget_for(self, userid: str, category_id: str) -> Optional[Budget]:
        with self._lock:
            for item in self.budgets.values():
                if item.userid == userid and item.category_id == category_id:
                    return item
        return None

    def list_budgets(self, userid: str) -> List[Budget]:
        with self._lock:
            items = [item for item in self.budgets.values() if item.userid == userid]
        items.sort(key=lambda item: item.category_id)
        return items

    def delete_budget(self, budget_id: str) -> None:
        with self._lock:
            self.budgets.pop(budget_id, None)


class SqliteCategoryStore:
    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        return conn

    def _init(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS categories (
                    category_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    userid TEXT NOT NULL,
                    archived INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS merchants (
                    merchant_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    merchant TEXT NOT NULL,
                    category_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS merchants_owner_key
                    ON merchants(userid, merchant);
                CREATE TABLE IF NOT EXISTS category_rules (
                    rule_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    category_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS rules_owner_keyword
                    ON category_rules(userid, keyword);
                CREATE TABLE IF NOT EXISTS movements (
                    movement_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    account TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    category_id TEXT NOT NULL,
                    counterparty TEXT NOT NULL,
                    merchant TEXT NOT NULL,
                    description TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    posted_at REAL NOT NULL,
                    manual INTEGER NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS movements_source
                    ON movements(source_id) WHERE source_id != '';
                CREATE INDEX IF NOT EXISTS movements_user_time
                    ON movements(userid, posted_at);
                CREATE TABLE IF NOT EXISTS budgets (
                    budget_id TEXT PRIMARY KEY,
                    userid TEXT NOT NULL,
                    category_id TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS budgets_owner_category
                    ON budgets(userid, category_id);
                """
            )

    def put_category(self, item: Category) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO categories (
                    category_id, label, kind, userid, archived, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    item.category_id, item.label, item.kind, item.userid,
                    1 if item.archived else 0, item.created_at,
                ),
            )

    def get_category(self, category_id: str) -> Optional[Category]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM categories WHERE category_id=?', (category_id,)
            ).fetchone()
        return None if row is None else self._category_from_row(row)

    def list_categories(self, userid: Optional[str] = None, include_archived: bool = False) -> List[Category]:
        sql = 'SELECT * FROM categories'
        clauses: List[str] = []
        params: List[Any] = []
        if userid is not None:
            clauses.append("(kind='system' OR userid=?)")
            params.append(userid)
        if not include_archived:
            clauses.append('archived=0')
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += " ORDER BY CASE kind WHEN 'system' THEN 0 ELSE 1 END, label COLLATE NOCASE"
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._category_from_row(row) for row in rows]

    def put_merchant(self, item: MerchantTag) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO merchants (
                    merchant_id, userid, merchant, category_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (item.merchant_id, item.userid, item.merchant, item.category_id, item.created_at),
            )

    def get_merchant(self, merchant_id: str) -> Optional[MerchantTag]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM merchants WHERE merchant_id=?', (merchant_id,)
            ).fetchone()
        return None if row is None else self._merchant_from_row(row)

    def get_merchant_by_key(self, userid: str, merchant: str) -> Optional[MerchantTag]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM merchants WHERE userid=? AND merchant=?',
                (userid, merchant),
            ).fetchone()
        return None if row is None else self._merchant_from_row(row)

    def list_merchants(self, userid: Optional[str] = None) -> List[MerchantTag]:
        sql = 'SELECT * FROM merchants'
        params: List[Any] = []
        if userid is not None:
            sql += ' WHERE userid IN (?, ?)'
            params.extend([userid, ''])
        sql += ' ORDER BY CASE userid WHEN \'\' THEN 0 ELSE 1 END, merchant'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._merchant_from_row(row) for row in rows]

    def delete_merchant(self, merchant_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute('DELETE FROM merchants WHERE merchant_id=?', (merchant_id,))

    def put_rule(self, item: CategoryRule) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO category_rules (
                    rule_id, userid, keyword, category_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (item.rule_id, item.userid, item.keyword, item.category_id, item.created_at),
            )

    def get_rule(self, rule_id: str) -> Optional[CategoryRule]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM category_rules WHERE rule_id=?', (rule_id,)
            ).fetchone()
        return None if row is None else self._rule_from_row(row)

    def get_rule_by_keyword(self, userid: str, keyword: str) -> Optional[CategoryRule]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM category_rules WHERE userid=? AND keyword=?',
                (userid, keyword),
            ).fetchone()
        return None if row is None else self._rule_from_row(row)

    def list_rules(self, userid: Optional[str] = None) -> List[CategoryRule]:
        sql = 'SELECT * FROM category_rules'
        params: List[Any] = []
        if userid is not None:
            sql += ' WHERE userid IN (?, ?)'
            params.extend([userid, ''])
        sql += ' ORDER BY CASE userid WHEN \'\' THEN 1 ELSE 0 END, LENGTH(keyword) DESC, keyword'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._rule_from_row(row) for row in rows]

    def delete_rule(self, rule_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute('DELETE FROM category_rules WHERE rule_id=?', (rule_id,))

    def put_movement(self, item: Movement) -> Movement:
        with self._lock, self._connect() as conn:
            if item.source_id:
                row = conn.execute(
                    'SELECT * FROM movements WHERE source_id=?', (item.source_id,)
                ).fetchone()
                if row is not None:
                    return self._movement_from_row(row)
            conn.execute(
                """
                INSERT INTO movements (
                    movement_id, userid, account, kind, direction, amount, category_id,
                    counterparty, merchant, description, source_id, posted_at, manual
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.movement_id, item.userid, item.account, item.kind, item.direction,
                    money_str(item.amount), item.category_id, item.counterparty, item.merchant,
                    item.description, item.source_id, item.posted_at, 1 if item.manual else 0,
                ),
            )
        return item

    def get_movement(self, movement_id: str) -> Optional[Movement]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM movements WHERE movement_id=?', (movement_id,)
            ).fetchone()
        return None if row is None else self._movement_from_row(row)

    def get_by_source(self, source_id: str) -> Optional[Movement]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM movements WHERE source_id=?', (source_id,)
            ).fetchone()
        return None if row is None else self._movement_from_row(row)

    def update_movement(self, item: Movement) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE movements SET category_id=?, merchant=?, description=?, manual=?
                WHERE movement_id=?
                """,
                (
                    item.category_id, item.merchant, item.description,
                    1 if item.manual else 0, item.movement_id,
                ),
            )

    def list_movements(
        self,
        *,
        userid: Optional[str] = None,
        account: Optional[str] = None,
        start: Optional[float] = None,
        end: Optional[float] = None,
        category_id: Optional[str] = None,
    ) -> List[Movement]:
        clauses: List[str] = []
        params: List[Any] = []
        if userid is not None:
            clauses.append('userid=?')
            params.append(userid)
        if account is not None:
            clauses.append('account=?')
            params.append(account)
        if category_id is not None:
            clauses.append('category_id=?')
            params.append(category_id)
        if start is not None:
            clauses.append('posted_at>=?')
            params.append(start)
        if end is not None:
            clauses.append('posted_at<?')
            params.append(end)
        sql = 'SELECT * FROM movements'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY posted_at DESC, movement_id DESC'
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._movement_from_row(row) for row in rows]

    def put_budget(self, item: Budget) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO budgets (
                    budget_id, userid, category_id, amount, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    item.budget_id, item.userid, item.category_id,
                    money_str(item.amount), item.created_at, item.updated_at,
                ),
            )

    def get_budget(self, budget_id: str) -> Optional[Budget]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM budgets WHERE budget_id=?', (budget_id,)
            ).fetchone()
        return None if row is None else self._budget_from_row(row)

    def get_budget_for(self, userid: str, category_id: str) -> Optional[Budget]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM budgets WHERE userid=? AND category_id=?',
                (userid, category_id),
            ).fetchone()
        return None if row is None else self._budget_from_row(row)

    def list_budgets(self, userid: str) -> List[Budget]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM budgets WHERE userid=? ORDER BY category_id',
                (userid,),
            ).fetchall()
        return [self._budget_from_row(row) for row in rows]

    def delete_budget(self, budget_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute('DELETE FROM budgets WHERE budget_id=?', (budget_id,))

    def _category_from_row(self, row: sqlite3.Row) -> Category:
        return Category(
            category_id=row['category_id'],
            label=row['label'],
            kind=row['kind'],
            userid=row['userid'] or '',
            archived=bool(row['archived']),
            created_at=float(row['created_at']),
        )

    def _merchant_from_row(self, row: sqlite3.Row) -> MerchantTag:
        return MerchantTag(
            merchant_id=row['merchant_id'],
            userid=row['userid'] or '',
            merchant=row['merchant'],
            category_id=row['category_id'],
            created_at=float(row['created_at']),
        )

    def _rule_from_row(self, row: sqlite3.Row) -> CategoryRule:
        return CategoryRule(
            rule_id=row['rule_id'],
            userid=row['userid'] or '',
            keyword=row['keyword'],
            category_id=row['category_id'],
            created_at=float(row['created_at']),
        )

    def _movement_from_row(self, row: sqlite3.Row) -> Movement:
        return Movement(
            movement_id=row['movement_id'],
            userid=row['userid'] or '',
            account=row['account'],
            kind=row['kind'],
            direction=row['direction'],
            amount=Decimal(row['amount']),
            category_id=row['category_id'],
            counterparty=row['counterparty'] or '',
            merchant=row['merchant'] or '',
            description=row['description'] or '',
            source_id=row['source_id'] or '',
            posted_at=float(row['posted_at']),
            manual=bool(row['manual']),
        )

    def _budget_from_row(self, row: sqlite3.Row) -> Budget:
        return Budget(
            budget_id=row['budget_id'],
            userid=row['userid'],
            category_id=row['category_id'],
            amount=Decimal(row['amount']),
            created_at=float(row['created_at']),
            updated_at=float(row['updated_at']),
        )


class CategoryService:
    def __init__(
        self,
        policy: Optional[CategoryPolicy] = None,
        store: Any = None,
        *,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.policy = policy or CategoryPolicy()
        self.store = store or MemoryCategoryStore()
        self.clock = clock or (lambda: datetime.now(tz=timezone.utc).timestamp())
        self.ensure_taxonomy()

    def ensure_taxonomy(self) -> None:
        now = self.clock()
        for category_id, label in SYSTEM_CATEGORIES:
            existing = self.store.get_category(category_id)
            if existing is None:
                self.store.put_category(Category(
                    category_id=category_id, label=label, kind='system',
                    userid='', archived=False, created_at=now,
                ))
        for keyword, category_id in SYSTEM_KEYWORDS:
            if self.store.get_rule_by_keyword('', keyword) is None:
                self.store.put_rule(CategoryRule(
                    rule_id='sys:%s' % keyword, userid='', keyword=keyword,
                    category_id=category_id, created_at=now,
                ))

    def _assert_owner(self, *, owner_userid: str, actor: str, actor_type: str) -> None:
        if actor_type in EMPLOYEE_ROLES:
            return
        if actor != owner_userid:
            raise CategoryError('userid_mismatch', 'User ID mismatch')

    def _require_owner(self, *, actor: str, actor_type: str, customer_id: Any) -> str:
        if actor_type in EMPLOYEE_ROLES:
            owner = str(customer_id or '').strip()
            if not owner:
                raise CategoryError('missing_customer_id', 'customer_id is required')
            return owner
        return actor

    def _resolve_category(self, category_id: str, owner: str, *, allow_archived: bool = False) -> Category:
        item = self.store.get_category(category_id)
        if item is None:
            raise CategoryError('category_not_found', 'Category not found')
        if item.kind == 'custom' and item.userid != owner:
            raise CategoryError('category_forbidden', 'Category is not owned by this customer')
        if item.archived and not allow_archived:
            raise CategoryError('category_not_found', 'Category is archived')
        return item

    def classify(
        self,
        *,
        userid: str,
        kind: str,
        direction: str,
        counterparty: str = '',
        merchant: str = '',
        description: str = '',
        category_id: Any = None,
    ) -> str:
        explicit = normalize_category_id(category_id, required=False)
        if explicit:
            try:
                return self._resolve_category(explicit, userid).category_id
            except CategoryError:
                pass
        merchant_key = merchant
        if not merchant_key and counterparty:
            try:
                merchant_key = normalize_merchant(counterparty)
            except CategoryError:
                merchant_key = ''
        if merchant_key:
            tagged = (
                self.store.get_merchant_by_key(userid, merchant_key)
                or self.store.get_merchant_by_key('', merchant_key)
            )
            if tagged is not None:
                try:
                    return self._resolve_category(tagged.category_id, userid).category_id
                except CategoryError:
                    pass
        haystack = ' '.join((description or '', merchant_key or '', counterparty or '')).lower()
        for rule in self.store.list_rules(userid=userid):
            if rule.keyword and rule.keyword in haystack:
                try:
                    return self._resolve_category(rule.category_id, userid).category_id
                except CategoryError:
                    continue
        fallback = KIND_DEFAULTS.get(kind)
        if fallback and self.store.get_category(fallback) is not None:
            return fallback
        return UNCATEGORIZED

    def observe(
        self,
        account: Any,
        amount: Any,
        kind: Any,
        *,
        direction: Any = None,
        userid: Optional[str] = None,
        counterparty: Any = '',
        merchant: Any = '',
        description: Any = '',
        category_id: Any = None,
        source_id: Any = None,
        posted_at: Optional[float] = None,
    ) -> Optional[Movement]:
        try:
            account_no = normalize_account(account)
            parsed_amount = parse_money(amount)
            entry_kind = normalize_kind(kind)
            entry_direction = normalize_direction(direction, entry_kind)
        except (AccountError, AmountError, CategoryError):
            return None
        source = str(source_id or '').strip()
        if source:
            existing = self.store.get_by_source(source)
            if existing is not None:
                return existing
        note = str(description or '').strip()
        if len(note) > self.policy.max_description:
            note = note[: self.policy.max_description]
        merchant_key = ''
        if merchant not in (None, ''):
            try:
                merchant_key = normalize_merchant(merchant)
            except CategoryError:
                merchant_key = slugify(merchant)
        elif counterparty not in (None, ''):
            try:
                merchant_key = normalize_merchant(counterparty)
            except CategoryError:
                merchant_key = ''
        owner = str(userid or '').strip()
        assigned = self.classify(
            userid=owner, kind=entry_kind, direction=entry_direction,
            counterparty=str(counterparty or ''), merchant=merchant_key,
            description=note, category_id=category_id,
        )
        explicit = bool(normalize_category_id(category_id, required=False))
        item = Movement(
            movement_id=str(uuid.uuid4()),
            userid=owner,
            account=account_no,
            kind=entry_kind,
            direction=entry_direction,
            amount=parsed_amount,
            category_id=assigned,
            counterparty=str(counterparty or '').strip(),
            merchant=merchant_key,
            description=note,
            source_id=source,
            posted_at=float(posted_at if posted_at is not None else self.clock()),
            manual=explicit,
        )
        return self.store.put_movement(item)

    def create_category(
        self,
        *,
        actor: str,
        actor_type: str,
        label: Any,
        customer_id: Any = None,
        keyword: Any = None,
    ) -> Category:
        now = self.clock()
        name = normalize_label(label, max_len=self.policy.max_label)
        slug = slugify(name)
        if not slug:
            raise CategoryError('invalid_category', 'Category label is required')
        if actor_type in EMPLOYEE_ROLES and not str(customer_id or '').strip():
            category_id = slug
            if self.store.get_category(category_id) is not None:
                raise CategoryError('category_duplicate', 'Category already exists')
            item = Category(
                category_id=category_id, label=name, kind='system',
                userid='', archived=False, created_at=now,
            )
            self.store.put_category(item)
            return item
        owner = self._require_owner(actor=actor, actor_type=actor_type, customer_id=customer_id)
        self._assert_owner(owner_userid=owner, actor=actor, actor_type=actor_type)
        custom = [item for item in self.store.list_categories(userid=owner, include_archived=True)
                  if item.kind == 'custom' and item.userid == owner and not item.archived]
        if len(custom) >= self.policy.max_custom_categories:
            raise CategoryError('category_limit', 'Too many custom categories')
        category_id = 'c-%s-%s' % (slugify(owner), slug)
        existing = self.store.get_category(category_id)
        if existing is not None and not existing.archived:
            raise CategoryError('category_duplicate', 'Category already exists')
        item = Category(
            category_id=category_id, label=name, kind='custom',
            userid=owner, archived=False, created_at=now,
        )
        self.store.put_category(item)
        if keyword not in (None, ''):
            self.add_rule(
                actor=actor, actor_type=actor_type, keyword=keyword,
                category_id=item.category_id, customer_id=owner,
            )
        return item

    def archive_category(
        self,
        *,
        actor: str,
        actor_type: str,
        category_id: Any,
        customer_id: Any = None,
    ) -> Category:
        owner = self._require_owner(actor=actor, actor_type=actor_type, customer_id=customer_id)
        self._assert_owner(owner_userid=owner, actor=actor, actor_type=actor_type)
        item = self._resolve_category(normalize_category_id(category_id), owner)
        if item.kind == 'system' and actor_type not in EMPLOYEE_ROLES:
            raise CategoryError('category_system', 'System categories cannot be archived by customers')
        if item.kind == 'system':
            raise CategoryError('category_system', 'System categories cannot be archived')
        item.archived = True
        self.store.put_category(item)
        return item

    def add_rule(
        self,
        *,
        actor: str,
        actor_type: str,
        keyword: Any,
        category_id: Any,
        customer_id: Any = None,
    ) -> CategoryRule:
        owner = '' if (actor_type in EMPLOYEE_ROLES and not str(customer_id or '').strip()) else self._require_owner(
            actor=actor, actor_type=actor_type, customer_id=customer_id,
        )
        if owner:
            self._assert_owner(owner_userid=owner, actor=actor, actor_type=actor_type)
        word = normalize_keyword(keyword)
        category = self._resolve_category(normalize_category_id(category_id), owner or actor)
        if self.store.get_rule_by_keyword(owner, word) is not None:
            raise CategoryError('rule_duplicate', 'Keyword rule already exists')
        owned_rules = [item for item in self.store.list_rules(userid=owner or None) if item.userid == owner]
        if owner and len(owned_rules) >= self.policy.max_rules:
            raise CategoryError('rule_limit', 'Too many keyword rules')
        item = CategoryRule(
            rule_id=str(uuid.uuid4()), userid=owner, keyword=word,
            category_id=category.category_id, created_at=self.clock(),
        )
        self.store.put_rule(item)
        return item

    def remove_rule(
        self,
        *,
        actor: str,
        actor_type: str,
        rule_id: Any,
        customer_id: Any = None,
    ) -> CategoryRule:
        item = self.store.get_rule(str(rule_id or '').strip())
        if item is None:
            raise CategoryError('rule_not_found', 'Keyword rule not found')
        if item.userid:
            owner = self._require_owner(actor=actor, actor_type=actor_type, customer_id=customer_id or item.userid)
            self._assert_owner(owner_userid=owner, actor=actor, actor_type=actor_type)
            if item.userid != owner and actor_type not in EMPLOYEE_ROLES:
                raise CategoryError('category_forbidden', 'Keyword rule is not owned by this customer')
        elif actor_type not in EMPLOYEE_ROLES:
            raise CategoryError('category_forbidden', 'System rules cannot be removed by customers')
        self.store.delete_rule(item.rule_id)
        return item

    def set_merchant_tag(
        self,
        *,
        actor: str,
        actor_type: str,
        merchant: Any,
        category_id: Any,
        customer_id: Any = None,
    ) -> MerchantTag:
        owner = '' if (actor_type in EMPLOYEE_ROLES and not str(customer_id or '').strip()) else self._require_owner(
            actor=actor, actor_type=actor_type, customer_id=customer_id,
        )
        if owner:
            self._assert_owner(owner_userid=owner, actor=actor, actor_type=actor_type)
        key = normalize_merchant(merchant)
        category = self._resolve_category(normalize_category_id(category_id), owner or actor)
        existing = self.store.get_merchant_by_key(owner, key)
        if existing is not None:
            existing.category_id = category.category_id
            self.store.put_merchant(existing)
            return existing
        owned = [item for item in self.store.list_merchants(userid=owner or None) if item.userid == owner]
        if owner and len(owned) >= self.policy.max_merchant_tags:
            raise CategoryError('merchant_limit', 'Too many merchant tags')
        item = MerchantTag(
            merchant_id=str(uuid.uuid4()), userid=owner, merchant=key,
            category_id=category.category_id, created_at=self.clock(),
        )
        self.store.put_merchant(item)
        return item

    def remove_merchant_tag(
        self,
        *,
        actor: str,
        actor_type: str,
        merchant_id: Any = None,
        merchant: Any = None,
        customer_id: Any = None,
    ) -> MerchantTag:
        item = None
        if merchant_id:
            item = self.store.get_merchant(str(merchant_id).strip())
        elif merchant not in (None, ''):
            owner = self._require_owner(actor=actor, actor_type=actor_type, customer_id=customer_id) if actor_type not in EMPLOYEE_ROLES or str(customer_id or '').strip() else ''
            item = self.store.get_merchant_by_key(owner, normalize_merchant(merchant))
        if item is None:
            raise CategoryError('merchant_not_found', 'Merchant tag not found')
        if item.userid:
            owner = self._require_owner(actor=actor, actor_type=actor_type, customer_id=customer_id or item.userid)
            self._assert_owner(owner_userid=owner, actor=actor, actor_type=actor_type)
            if item.userid != owner and actor_type not in EMPLOYEE_ROLES:
                raise CategoryError('category_forbidden', 'Merchant tag is not owned by this customer')
        elif actor_type not in EMPLOYEE_ROLES:
            raise CategoryError('category_forbidden', 'System merchant tags cannot be removed by customers')
        self.store.delete_merchant(item.merchant_id)
        return item

    def recategorize(
        self,
        *,
        actor: str,
        actor_type: str,
        movement_id: Any,
        category_id: Any,
        customer_id: Any = None,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> Movement:
        item = self.store.get_movement(str(movement_id or '').strip())
        if item is None:
            raise CategoryError('movement_not_found', 'Movement not found')
        owner = item.userid
        if actor_type not in EMPLOYEE_ROLES:
            self._assert_owner(owner_userid=owner, actor=actor, actor_type=actor_type)
            owned = _own_account_set(own_accounts)
            if owned and item.account not in owned:
                raise CategoryError('category_forbidden', 'Account is not owned by this customer')
        else:
            claimed = str(customer_id or owner).strip()
            if claimed and claimed != owner:
                raise CategoryError('category_forbidden', 'Movement does not belong to this customer')
        category = self._resolve_category(normalize_category_id(category_id), owner)
        item.category_id = category.category_id
        item.manual = True
        self.store.update_movement(item)
        return item

    def set_budget(
        self,
        *,
        actor: str,
        actor_type: str,
        category_id: Any,
        amount: Any,
        customer_id: Any = None,
    ) -> Budget:
        owner = self._require_owner(actor=actor, actor_type=actor_type, customer_id=customer_id)
        self._assert_owner(owner_userid=owner, actor=actor, actor_type=actor_type)
        category = self._resolve_category(normalize_category_id(category_id), owner)
        parsed = parse_money(amount)
        if parsed < self.policy.min_budget or parsed > self.policy.max_budget:
            raise CategoryError('budget_out_of_range', 'Budget is outside the allowed range')
        existing = self.store.get_budget_for(owner, category.category_id)
        now = self.clock()
        if existing is None:
            budgets = self.store.list_budgets(owner)
            if len(budgets) >= self.policy.max_budgets:
                raise CategoryError('budget_limit', 'Too many category budgets')
            item = Budget(
                budget_id=str(uuid.uuid4()), userid=owner,
                category_id=category.category_id, amount=parsed,
                created_at=now, updated_at=now,
            )
        else:
            item = existing
            item.amount = parsed
            item.updated_at = now
        self.store.put_budget(item)
        return item

    def clear_budget(
        self,
        *,
        actor: str,
        actor_type: str,
        category_id: Any,
        customer_id: Any = None,
    ) -> Budget:
        owner = self._require_owner(actor=actor, actor_type=actor_type, customer_id=customer_id)
        self._assert_owner(owner_userid=owner, actor=actor, actor_type=actor_type)
        item = self.store.get_budget_for(owner, normalize_category_id(category_id))
        if item is None:
            raise CategoryError('budget_not_found', 'Budget not found')
        self.store.delete_budget(item.budget_id)
        return item

    def list_categories_for(self, userid: str) -> List[Category]:
        return self.store.list_categories(userid=userid)

    def list_movements_for(
        self,
        *,
        actor: str,
        actor_type: str,
        customer_id: Any = None,
        account: Any = None,
        period: Any = None,
        own_accounts: Optional[Iterable[Any]] = None,
    ) -> List[Movement]:
        owner = self._require_owner(actor=actor, actor_type=actor_type, customer_id=customer_id)
        self._assert_owner(owner_userid=owner, actor=actor, actor_type=actor_type)
        _label, start, end = resolve_month(period, now=self.clock())
        account_no = normalize_account(account, required=False) if account not in (None, '') else None
        if actor_type not in EMPLOYEE_ROLES and account_no:
            owned = _own_account_set(own_accounts)
            if owned and account_no not in owned:
                raise CategoryError('category_forbidden', 'Account is not owned by this customer')
        return self.store.list_movements(userid=owner, account=account_no, start=start, end=end)

    def _budget_row(self, budget: Budget, spent: Decimal, labels: Dict[str, str]) -> Dict[str, Any]:
        remaining = budget.amount - spent
        if remaining < 0:
            remaining = Decimal('0.00')
        percent = Decimal('0.00')
        if budget.amount > 0:
            percent = (spent / budget.amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)
        status = 'ok'
        if spent >= budget.amount:
            status = 'exceeded'
        elif spent >= (budget.amount * self.policy.warning_ratio):
            status = 'warning'
        return {
            'budget_id': budget.budget_id,
            'category_id': budget.category_id,
            'label': labels.get(budget.category_id, budget.category_id),
            'limit': money_str(budget.amount),
            'spent': money_str(spent),
            'remaining': money_str(remaining),
            'percent': str(percent),
            'status': status,
        }

    def snapshot(
        self,
        userid: str,
        *,
        period: Any = None,
        account: Any = None,
    ) -> Dict[str, Any]:
        owner = str(userid or '').strip()
        label, start, end = resolve_month(period, now=self.clock())
        account_no = normalize_account(account, required=False) if account not in (None, '') else None
        categories = self.store.list_categories(userid=owner)
        labels = {item.category_id: item.label for item in categories}
        movements = self.store.list_movements(userid=owner, account=account_no, start=start, end=end)
        spent: Dict[str, Decimal] = {}
        credited: Dict[str, Decimal] = {}
        debit_total = Decimal('0.00')
        credit_total = Decimal('0.00')
        uncategorized = Decimal('0.00')
        for item in movements:
            if item.direction == 'debit':
                spent[item.category_id] = spent.get(item.category_id, Decimal('0.00')) + item.amount
                debit_total += item.amount
                if item.category_id == UNCATEGORIZED:
                    uncategorized += item.amount
            else:
                credited[item.category_id] = credited.get(item.category_id, Decimal('0.00')) + item.amount
                credit_total += item.amount
        breakdown = []
        seen = set(spent) | set(credited)
        for category in categories:
            if category.category_id not in seen and not self.store.get_budget_for(owner, category.category_id):
                continue
            debit_amt = spent.get(category.category_id, Decimal('0.00'))
            credit_amt = credited.get(category.category_id, Decimal('0.00'))
            if debit_amt == 0 and credit_amt == 0 and category.category_id not in {
                item.category_id for item in self.store.list_budgets(owner)
            }:
                continue
            breakdown.append({
                'category_id': category.category_id,
                'label': category.label,
                'kind': category.kind,
                'debit': money_str(debit_amt),
                'credit': money_str(credit_amt),
            })
        budgets = [
            self._budget_row(item, spent.get(item.category_id, Decimal('0.00')), labels)
            for item in self.store.list_budgets(owner)
        ]
        return {
            'period': label,
            'period_start': start,
            'period_end': end,
            'categories': [item.to_dict() for item in categories],
            'merchants': [item.to_dict() for item in self.store.list_merchants(userid=owner) if item.userid in {owner, ''}],
            'rules': [item.to_dict() for item in self.store.list_rules(userid=owner) if item.userid in {owner, ''}],
            'movements': [item.to_dict() for item in movements[:40]],
            'breakdown': breakdown,
            'budgets': budgets,
            'totals': {
                'debit': money_str(debit_total),
                'credit': money_str(credit_total),
                'uncategorized_debit': money_str(uncategorized),
            },
        }


def set_service(service: Optional[CategoryService]) -> None:
    global _SERVICE
    _SERVICE = service


def get_service() -> Optional[CategoryService]:
    return _SERVICE


def build_service(*, clock: Optional[Callable[[], float]] = None, store: Any = None) -> CategoryService:
    if store is None:
        path = os.environ.get('CATEGORY_STORE') or DEFAULT_STORE_PATH
        if (os.environ.get('CATEGORY_STORE_BACKEND') or 'sqlite').strip().lower() == 'memory':
            store = MemoryCategoryStore()
        else:
            store = SqliteCategoryStore(path)
    return CategoryService(CategoryPolicy.from_env(), store, clock=clock)


def observe_movement(
    account: Any,
    amount: Any,
    kind: Any,
    *,
    direction: Any = None,
    userid: Optional[str] = None,
    counterparty: Any = '',
    merchant: Any = '',
    description: Any = '',
    category_id: Any = None,
    source_id: Any = None,
) -> None:
    service = get_service()
    if service is None:
        return
    try:
        service.observe(
            account, amount, kind,
            direction=direction, userid=userid, counterparty=counterparty,
            merchant=merchant, description=description, category_id=category_id,
            source_id=source_id,
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
        base = str(source_id or uuid.uuid4())
        if deposit:
            service.observe(
                to_account, amount, 'deposit', direction='credit',
                userid=to_userid, counterparty=from_account,
                description=description or 'deposit',
                source_id=base + ':deposit',
            )
            return
        service.observe(
            from_account, amount, 'transfer_out', direction='debit',
            userid=from_userid, counterparty=to_account,
            description=description or 'transfer out',
            source_id=base + ':out',
        )
        service.observe(
            to_account, amount, 'transfer_in', direction='credit',
            userid=to_userid, counterparty=from_account,
            description=description or 'transfer in',
            source_id=base + ':in',
        )
    except Exception:
        return


def _flask():
    from flask import jsonify, request, session
    return jsonify, request, session


def _require_session_user():
    jsonify, request, session = _flask()
    userid = session.get('userid')
    if not userid:
        return None, (jsonify({'message': 'Unauthorized access or session expired'}), 401)
    values = request.get_json(silent=True) or {}
    claimed = values.get('userid')
    if claimed is not None and str(claimed) != str(userid):
        return None, (jsonify({'message': 'User ID mismatch', 'error': 'userid_mismatch'}), 403)
    return str(userid), None


def _error_status(code: str) -> int:
    return {
        'category_duplicate': 409,
        'category_limit': 409,
        'merchant_duplicate': 409,
        'merchant_limit': 409,
        'rule_duplicate': 409,
        'rule_limit': 409,
        'budget_limit': 409,
        'category_forbidden': 403,
        'category_system': 403,
        'userid_mismatch': 403,
        'category_not_found': 404,
        'movement_not_found': 404,
        'merchant_not_found': 404,
        'rule_not_found': 404,
        'budget_not_found': 404,
        'invalid_account': 400,
        'invalid_amount': 400,
        'invalid_category': 400,
        'invalid_period': 400,
        'invalid_keyword': 400,
        'invalid_merchant': 400,
        'invalid_kind': 400,
        'budget_out_of_range': 400,
        'period_in_future': 400,
        'missing_customer_id': 400,
    }.get(code, 400)


def _error_response(exc: Exception):
    jsonify, _request, _session = _flask()
    if isinstance(exc, CategoryError):
        payload = {'message': exc.message, 'error': exc.code}
        payload.update(exc.extra)
        return jsonify(payload), _error_status(exc.code)
    if isinstance(exc, AccountError):
        return jsonify({'message': 'Invalid account', 'error': 'invalid_account'}), 400
    if isinstance(exc, AmountError):
        return jsonify({'message': 'Invalid amount', 'error': 'invalid_amount'}), 400
    if isinstance(exc, PeriodError):
        return jsonify({'message': 'Invalid period', 'error': 'invalid_period'}), 400
    return jsonify({'message': 'Something Went wrong, Please try again later'}), 500


def _values():
    _jsonify, request, _session = _flask()
    return request.get_json(silent=True) or {}


def _snapshot_payload(service: CategoryService, owner: str, values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    values = values or {}
    return service.snapshot(owner, period=values.get('period'), account=values.get('account'))


def handle_list_categories(service: CategoryService):
    jsonify, _request, session = _flask()
    userid, error = _require_session_user()
    if error:
        return error
    values = _values()
    actor_type = session.get('usertype') or 'customer'
    try:
        owner = service._require_owner(
            actor=userid, actor_type=actor_type, customer_id=values.get('customer_id'),
        ) if actor_type in EMPLOYEE_ROLES else userid
        if actor_type not in EMPLOYEE_ROLES:
            owner = userid
        else:
            owner = str(values.get('customer_id') or values.get('owner') or '').strip() or userid
        return jsonify({
            'categories': [item.to_dict() for item in service.list_categories_for(owner)],
        }), 200
    except Exception as exc:
        return _error_response(exc)


def handle_create_category(service: CategoryService):
    jsonify, _request, session = _flask()
    userid, error = _require_session_user()
    if error:
        return error
    values = _values()
    actor_type = session.get('usertype') or 'customer'
    try:
        item = service.create_category(
            actor=userid, actor_type=actor_type, label=values.get('label') or values.get('category'),
            customer_id=values.get('customer_id'), keyword=values.get('keyword'),
        )
        owner = item.userid or str(values.get('customer_id') or userid)
        return jsonify({
            'category': item.to_dict(),
            'Spending': _snapshot_payload(service, owner, values),
        }), 201
    except Exception as exc:
        return _error_response(exc)


def handle_archive_category(service: CategoryService):
    jsonify, _request, session = _flask()
    userid, error = _require_session_user()
    if error:
        return error
    values = _values()
    actor_type = session.get('usertype') or 'customer'
    try:
        item = service.archive_category(
            actor=userid, actor_type=actor_type,
            category_id=values.get('category_id'),
            customer_id=values.get('customer_id'),
        )
        owner = item.userid or str(values.get('customer_id') or userid)
        return jsonify({
            'category': item.to_dict(),
            'Spending': _snapshot_payload(service, owner, values),
        }), 200
    except Exception as exc:
        return _error_response(exc)


def handle_set_merchant(service: CategoryService):
    jsonify, _request, session = _flask()
    userid, error = _require_session_user()
    if error:
        return error
    values = _values()
    actor_type = session.get('usertype') or 'customer'
    try:
        item = service.set_merchant_tag(
            actor=userid, actor_type=actor_type,
            merchant=values.get('merchant') or values.get('counterparty'),
            category_id=values.get('category_id'),
            customer_id=values.get('customer_id'),
        )
        owner = item.userid or str(values.get('customer_id') or userid)
        return jsonify({
            'merchant': item.to_dict(),
            'Spending': _snapshot_payload(service, owner, values),
        }), 200
    except Exception as exc:
        return _error_response(exc)


def handle_remove_merchant(service: CategoryService):
    jsonify, _request, session = _flask()
    userid, error = _require_session_user()
    if error:
        return error
    values = _values()
    actor_type = session.get('usertype') or 'customer'
    try:
        item = service.remove_merchant_tag(
            actor=userid, actor_type=actor_type,
            merchant_id=values.get('merchant_id'),
            merchant=values.get('merchant'),
            customer_id=values.get('customer_id'),
        )
        owner = item.userid or str(values.get('customer_id') or userid)
        return jsonify({
            'merchant': item.to_dict(),
            'Spending': _snapshot_payload(service, owner, values),
        }), 200
    except Exception as exc:
        return _error_response(exc)


def handle_add_rule(service: CategoryService):
    jsonify, _request, session = _flask()
    userid, error = _require_session_user()
    if error:
        return error
    values = _values()
    actor_type = session.get('usertype') or 'customer'
    try:
        item = service.add_rule(
            actor=userid, actor_type=actor_type,
            keyword=values.get('keyword'),
            category_id=values.get('category_id'),
            customer_id=values.get('customer_id'),
        )
        owner = item.userid or str(values.get('customer_id') or userid)
        return jsonify({
            'rule': item.to_dict(),
            'Spending': _snapshot_payload(service, owner, values),
        }), 201
    except Exception as exc:
        return _error_response(exc)


def handle_remove_rule(service: CategoryService):
    jsonify, _request, session = _flask()
    userid, error = _require_session_user()
    if error:
        return error
    values = _values()
    actor_type = session.get('usertype') or 'customer'
    try:
        item = service.remove_rule(
            actor=userid, actor_type=actor_type,
            rule_id=values.get('rule_id'),
            customer_id=values.get('customer_id'),
        )
        owner = item.userid or str(values.get('customer_id') or userid)
        return jsonify({
            'rule': item.to_dict(),
            'Spending': _snapshot_payload(service, owner, values),
        }), 200
    except Exception as exc:
        return _error_response(exc)


def handle_categorize(service: CategoryService, own_accounts_loader=None):
    jsonify, _request, session = _flask()
    userid, error = _require_session_user()
    if error:
        return error
    values = _values()
    actor_type = session.get('usertype') or 'customer'
    try:
        owned = None
        if callable(own_accounts_loader) and actor_type not in EMPLOYEE_ROLES:
            owned = own_accounts_loader(userid)
        item = service.recategorize(
            actor=userid, actor_type=actor_type,
            movement_id=values.get('movement_id'),
            category_id=values.get('category_id'),
            customer_id=values.get('customer_id'),
            own_accounts=owned,
        )
        return jsonify({
            'movement': item.to_dict(),
            'Spending': _snapshot_payload(service, item.userid, values),
        }), 200
    except Exception as exc:
        return _error_response(exc)


def handle_list_movements(service: CategoryService, own_accounts_loader=None):
    jsonify, _request, session = _flask()
    userid, error = _require_session_user()
    if error:
        return error
    values = _values()
    actor_type = session.get('usertype') or 'customer'
    try:
        owned = None
        if callable(own_accounts_loader) and actor_type not in EMPLOYEE_ROLES:
            owned = own_accounts_loader(userid)
        items = service.list_movements_for(
            actor=userid, actor_type=actor_type,
            customer_id=values.get('customer_id'),
            account=values.get('account'),
            period=values.get('period'),
            own_accounts=owned,
        )
        return jsonify({'movements': [item.to_dict() for item in items]}), 200
    except Exception as exc:
        return _error_response(exc)


def handle_set_budget(service: CategoryService):
    jsonify, _request, session = _flask()
    userid, error = _require_session_user()
    if error:
        return error
    values = _values()
    actor_type = session.get('usertype') or 'customer'
    try:
        item = service.set_budget(
            actor=userid, actor_type=actor_type,
            category_id=values.get('category_id'),
            amount=values.get('amount'),
            customer_id=values.get('customer_id'),
        )
        return jsonify({
            'budget': item.to_dict(),
            'Spending': _snapshot_payload(service, item.userid, values),
        }), 200
    except Exception as exc:
        return _error_response(exc)


def handle_clear_budget(service: CategoryService):
    jsonify, _request, session = _flask()
    userid, error = _require_session_user()
    if error:
        return error
    values = _values()
    actor_type = session.get('usertype') or 'customer'
    try:
        item = service.clear_budget(
            actor=userid, actor_type=actor_type,
            category_id=values.get('category_id'),
            customer_id=values.get('customer_id'),
        )
        return jsonify({
            'budget': item.to_dict(),
            'Spending': _snapshot_payload(service, item.userid, values),
        }), 200
    except Exception as exc:
        return _error_response(exc)


def handle_list_spending(service: CategoryService):
    jsonify, _request, session = _flask()
    userid, error = _require_session_user()
    if error:
        return error
    values = _values()
    actor_type = session.get('usertype') or 'customer'
    try:
        if actor_type in EMPLOYEE_ROLES:
            owner = str(values.get('customer_id') or '').strip()
            if not owner:
                raise CategoryError('missing_customer_id', 'customer_id is required')
        else:
            owner = userid
        return jsonify({'Spending': _snapshot_payload(service, owner, values)}), 200
    except Exception as exc:
        return _error_response(exc)


def attach_category_routes(app, service: CategoryService, own_accounts_loader=None) -> None:
    @app.route('/listCategories', methods=['POST', 'GET'])
    def list_categories_route():
        return handle_list_categories(service)

    @app.route('/createCategory', methods=['POST', 'GET'])
    def create_category_route():
        return handle_create_category(service)

    @app.route('/archiveCategory', methods=['POST', 'GET'])
    def archive_category_route():
        return handle_archive_category(service)

    @app.route('/setMerchantTag', methods=['POST', 'GET'])
    def set_merchant_route():
        return handle_set_merchant(service)

    @app.route('/removeMerchantTag', methods=['POST', 'GET'])
    def remove_merchant_route():
        return handle_remove_merchant(service)

    @app.route('/addCategoryRule', methods=['POST', 'GET'])
    def add_rule_route():
        return handle_add_rule(service)

    @app.route('/removeCategoryRule', methods=['POST', 'GET'])
    def remove_rule_route():
        return handle_remove_rule(service)

    @app.route('/categorizeMovement', methods=['POST', 'GET'])
    def categorize_route():
        return handle_categorize(service, own_accounts_loader=own_accounts_loader)

    @app.route('/listMovements', methods=['POST', 'GET'])
    def list_movements_route():
        return handle_list_movements(service, own_accounts_loader=own_accounts_loader)

    @app.route('/setBudget', methods=['POST', 'GET'])
    def set_budget_route():
        return handle_set_budget(service)

    @app.route('/clearBudget', methods=['POST', 'GET'])
    def clear_budget_route():
        return handle_clear_budget(service)

    @app.route('/listSpending', methods=['POST', 'GET'])
    def list_spending_route():
        return handle_list_spending(service)
