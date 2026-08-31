"""Outbound money velocity limits (rolling window, amount + count).

Distinct from request-count rate limits: this tracks *dollars moved*, not HTTP
attempts. Account-scoped per operation plus a customer-level outbound aggregate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import os
import sqlite3
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple


TWOPLACE = Decimal('0.01')
ZERO = Decimal('0.00')
EMPLOYEE_TYPES = frozenset({'admin', 'employee', 'tier1', 'tier2'})
OUTBOUND_OPS = frozenset({'transfer', 'withdraw', 'cheque'})
OP_LABELS = {
    'transfer': 'transfer',
    'withdraw': 'withdrawal',
    'cheque': 'cashier cheque',
    'outbound': 'outbound',
}


class AmountError(ValueError):
    """Rejected money input (not a velocity-limit miss)."""


def parse_money(value) -> Decimal:
    """Strict 2-dp money. No silent rounding, no scientific notation, no zeros."""
    if value is None or isinstance(value, bool):
        raise AmountError('invalid_amount')
    if isinstance(value, Decimal):
        raw = value
    elif isinstance(value, int):
        raw = Decimal(value)
    elif isinstance(value, float):
        raw = Decimal(str(value))
    elif isinstance(value, str):
        text = value.strip().replace(',', '')
        if not text or 'e' in text.lower() or text[0] == '+':
            raise AmountError('invalid_amount')
        try:
            raw = Decimal(text)
        except InvalidOperation as exc:
            raise AmountError('invalid_amount') from exc
    else:
        raise AmountError('invalid_amount')
    if raw.is_nan() or raw.is_infinite() or raw <= 0:
        raise AmountError('invalid_amount')
    exponent = raw.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise AmountError('invalid_amount')
    quantized = raw.quantize(TWOPLACE)
    if quantized > Decimal('1000000000.00'):
        raise AmountError('invalid_amount')
    return quantized


def _money(value: Decimal) -> str:
    return f"${value.quantize(TWOPLACE):.2f}"


def _env_decimal(name: str, default: str) -> Optional[Decimal]:
    raw = os.getenv(name, default)
    if raw in ('', 'none', 'None', 'unlimited'):
        return None
    return Decimal(raw).quantize(TWOPLACE)


def _env_int(name: str, default: str) -> Optional[int]:
    raw = os.getenv(name, default)
    if raw in ('', 'none', 'None', 'unlimited'):
        return None
    return int(raw)


def role_from_session(session) -> str:
    if (session or {}).get('usertype') in EMPLOYEE_TYPES:
        return 'employee'
    return 'customer'


@dataclass(frozen=True)
class LimitBand:
    daily_amount: Optional[Decimal]
    daily_count: Optional[int]
    per_txn: Optional[Decimal]
    window_seconds: int = 86400

    def unlimited(self) -> bool:
        return self.daily_amount is None and self.daily_count is None and self.per_txn is None


@dataclass(frozen=True)
class Movement:
    ts: float
    amount: Decimal
    operation: str
    ref: str = ''


@dataclass(frozen=True)
class VelocityDecision:
    allowed: bool
    code: str
    message: str
    used_amount: Decimal = ZERO
    remaining_amount: Optional[Decimal] = None
    used_count: int = 0
    remaining_count: Optional[int] = None
    retry_after: Optional[int] = None
    operation: str = ''

    def as_dict(self) -> dict:
        return {
            'error': None if self.allowed else self.code,
            'code': self.code,
            'message': self.message,
            'operation': self.operation,
            'used_amount': str(self.used_amount.quantize(TWOPLACE)),
            'remaining_amount': (
                None if self.remaining_amount is None
                else str(self.remaining_amount.quantize(TWOPLACE))
            ),
            'used_count': self.used_count,
            'remaining_count': self.remaining_count,
            'retry_after': self.retry_after,
        }


class MemoryVelocityStore:
    def __init__(self):
        self._rows: Dict[str, List[Movement]] = {}
        self._lock = threading.Lock()

    def list_since(self, scope: str, since: float) -> List[Movement]:
        with self._lock:
            return [m for m in self._rows.get(scope, []) if m.ts >= since]

    def append(self, scope: str, movement: Movement) -> None:
        with self._lock:
            self._rows.setdefault(scope, []).append(movement)

    def prune(self, before: float) -> int:
        removed = 0
        with self._lock:
            for scope, rows in list(self._rows.items()):
                keep = [m for m in rows if m.ts >= before]
                removed += len(rows) - len(keep)
                if keep:
                    self._rows[scope] = keep
                else:
                    del self._rows[scope]
        return removed

    def release(self, scope: str, ref: str) -> int:
        if not ref:
            return 0
        with self._lock:
            rows = self._rows.get(scope, [])
            keep = [m for m in rows if m.ref != ref]
            dropped = len(rows) - len(keep)
            if keep:
                self._rows[scope] = keep
            elif scope in self._rows:
                del self._rows[scope]
            return dropped


class SqliteVelocityStore:
    """Dedicated sqlite ledger. Not PR #12 DurableStore."""

    def __init__(self, path: str):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                ts REAL NOT NULL,
                amount TEXT NOT NULL,
                operation TEXT NOT NULL,
                ref TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_velocity_scope_ts ON movements(scope, ts)'
        )
        self._conn.commit()

    def list_since(self, scope: str, since: float) -> List[Movement]:
        with self._lock:
            cur = self._conn.execute(
                'SELECT ts, amount, operation, ref FROM movements '
                'WHERE scope = ? AND ts >= ? ORDER BY ts ASC',
                (scope, since),
            )
            return [
                Movement(float(ts), Decimal(amount), operation, ref or '')
                for ts, amount, operation, ref in cur.fetchall()
            ]

    def append(self, scope: str, movement: Movement) -> None:
        with self._lock:
            self._conn.execute(
                'INSERT INTO movements(scope, ts, amount, operation, ref) VALUES (?,?,?,?,?)',
                (scope, movement.ts, str(movement.amount), movement.operation, movement.ref),
            )
            self._conn.commit()

    def prune(self, before: float) -> int:
        with self._lock:
            cur = self._conn.execute('DELETE FROM movements WHERE ts < ?', (before,))
            self._conn.commit()
            return cur.rowcount or 0

    def release(self, scope: str, ref: str) -> int:
        if not ref:
            return 0
        with self._lock:
            cur = self._conn.execute(
                'DELETE FROM movements WHERE scope = ? AND ref = ?',
                (scope, ref),
            )
            self._conn.commit()
            return cur.rowcount or 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def default_bands(window_seconds: Optional[int] = None) -> Dict[Tuple[str, str], LimitBand]:
    window = window_seconds if window_seconds is not None else _env_int('VELOCITY_WINDOW_SECONDS', '86400') or 86400
    return {
        ('customer', 'transfer'): LimitBand(
            _env_decimal('VELOCITY_CUSTOMER_TRANSFER_DAILY', '5000'),
            _env_int('VELOCITY_CUSTOMER_TRANSFER_COUNT', '10'),
            _env_decimal('VELOCITY_CUSTOMER_TRANSFER_PER_TXN', '2000'),
            window,
        ),
        ('customer', 'withdraw'): LimitBand(
            _env_decimal('VELOCITY_CUSTOMER_WITHDRAW_DAILY', '1000'),
            _env_int('VELOCITY_CUSTOMER_WITHDRAW_COUNT', '5'),
            _env_decimal('VELOCITY_CUSTOMER_WITHDRAW_PER_TXN', '500'),
            window,
        ),
        ('customer', 'cheque'): LimitBand(
            _env_decimal('VELOCITY_CUSTOMER_CHEQUE_DAILY', '5000'),
            _env_int('VELOCITY_CUSTOMER_CHEQUE_COUNT', '3'),
            _env_decimal('VELOCITY_CUSTOMER_CHEQUE_PER_TXN', '5000'),
            window,
        ),
        ('customer', 'outbound'): LimitBand(
            _env_decimal('VELOCITY_CUSTOMER_OUTBOUND_DAILY', '8000'),
            _env_int('VELOCITY_CUSTOMER_OUTBOUND_COUNT', '15'),
            None,
            window,
        ),
        ('employee', 'transfer'): LimitBand(
            _env_decimal('VELOCITY_EMPLOYEE_TRANSFER_DAILY', '50000'),
            _env_int('VELOCITY_EMPLOYEE_TRANSFER_COUNT', '100'),
            _env_decimal('VELOCITY_EMPLOYEE_TRANSFER_PER_TXN', '25000'),
            window,
        ),
        ('employee', 'withdraw'): LimitBand(
            _env_decimal('VELOCITY_EMPLOYEE_WITHDRAW_DAILY', '20000'),
            _env_int('VELOCITY_EMPLOYEE_WITHDRAW_COUNT', '50'),
            _env_decimal('VELOCITY_EMPLOYEE_WITHDRAW_PER_TXN', '10000'),
            window,
        ),
        ('employee', 'cheque'): LimitBand(
            _env_decimal('VELOCITY_EMPLOYEE_CHEQUE_DAILY', '50000'),
            _env_int('VELOCITY_EMPLOYEE_CHEQUE_COUNT', '50'),
            _env_decimal('VELOCITY_EMPLOYEE_CHEQUE_PER_TXN', '25000'),
            window,
        ),
    }


class VelocityPolicy:
    def __init__(self, bands: Optional[Dict[Tuple[str, str], LimitBand]] = None):
        self.bands = bands if bands is not None else default_bands()

    def band_for(self, role: str, operation: str) -> LimitBand:
        return self.bands.get((role, operation), LimitBand(None, None, None))

    def evaluate(
        self,
        band: LimitBand,
        used_amount: Decimal,
        used_count: int,
        amount: Decimal,
        operation: str,
        oldest_ts: Optional[float] = None,
        now: Optional[float] = None,
    ) -> VelocityDecision:
        remaining_amount = None if band.daily_amount is None else max(ZERO, band.daily_amount - used_amount)
        remaining_count = None if band.daily_count is None else max(0, band.daily_count - used_count)
        retry = _retry_after(oldest_ts, now, band.window_seconds)
        label = OP_LABELS.get(operation, operation)

        if band.per_txn is not None and amount > band.per_txn:
            return VelocityDecision(
                False, 'per_txn_exceeded',
                f'Amount exceeds the {_money(band.per_txn)} per-{label} limit.',
                used_amount, remaining_amount, used_count, remaining_count, retry, operation,
            )
        if band.daily_count is not None and used_count + 1 > band.daily_count:
            return VelocityDecision(
                False, 'daily_count_exceeded',
                f'Daily {label} count limit ({band.daily_count}) reached.',
                used_amount, remaining_amount, used_count, remaining_count, retry, operation,
            )
        if band.daily_amount is not None and used_amount + amount > band.daily_amount:
            left = max(ZERO, band.daily_amount - used_amount)
            return VelocityDecision(
                False, 'daily_amount_exceeded',
                f'Daily {label} limit exceeded. Remaining: {_money(left)}.',
                used_amount, left, used_count, remaining_count, retry, operation,
            )
        if remaining_amount is not None:
            remaining_amount = max(ZERO, remaining_amount - amount)
        if remaining_count is not None:
            remaining_count = max(0, remaining_count - 1)
        return VelocityDecision(
            True, 'ok', 'ok',
            used_amount + amount, remaining_amount, used_count + 1, remaining_count, None, operation,
        )


def _retry_after(oldest_ts: Optional[float], now: Optional[float], window: int) -> Optional[int]:
    if oldest_ts is None or now is None:
        return None
    remain = int(oldest_ts + window - now)
    return max(1, remain)


def account_scope(account, operation: str) -> str:
    return f'acct:{int(account)}:{operation}'


def user_scope(userid: str) -> str:
    return f'user:{userid}:outbound'


class VelocityService:
    def __init__(
        self,
        store=None,
        policy: Optional[VelocityPolicy] = None,
        now: Optional[Callable[[], float]] = None,
        enabled: bool = True,
    ):
        self.store = store if store is not None else MemoryVelocityStore()
        self.policy = policy if policy is not None else VelocityPolicy()
        self._now = now or time.time
        self.enabled = enabled
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> 'VelocityService':
        enabled = os.getenv('VELOCITY_ENABLED', 'true').strip().lower() not in {'0', 'false', 'no', 'off'}
        path = os.getenv('VELOCITY_STORE', os.path.join('SystemLogs', 'velocity.sqlite'))
        if path in {':memory:', 'memory'}:
            store = MemoryVelocityStore()
        else:
            store = SqliteVelocityStore(path)
        return cls(store=store, policy=VelocityPolicy(), enabled=enabled)

    def snapshot(self, role: str, operation: str, account=None, userid: Optional[str] = None) -> dict:
        band = self.policy.band_for(role, operation)
        scope = user_scope(userid) if operation == 'outbound' else account_scope(account, operation)
        used_amount, used_count, oldest = self._usage(scope, band.window_seconds)
        remaining_amount = None if band.daily_amount is None else max(ZERO, band.daily_amount - used_amount)
        remaining_count = None if band.daily_count is None else max(0, band.daily_count - used_count)
        return {
            'operation': operation,
            'daily_amount': None if band.daily_amount is None else str(band.daily_amount),
            'per_txn': None if band.per_txn is None else str(band.per_txn),
            'used_amount': str(used_amount.quantize(TWOPLACE)),
            'remaining_amount': None if remaining_amount is None else str(remaining_amount.quantize(TWOPLACE)),
            'used_count': used_count,
            'remaining_count': remaining_count,
            'window_seconds': band.window_seconds,
        }

    def check(self, role: str, operation: str, amount, account=None, userid: Optional[str] = None) -> VelocityDecision:
        if not self.enabled:
            money = parse_money(amount)
            return VelocityDecision(True, 'ok', 'ok', money, None, 0, None, None, operation)
        money = parse_money(amount)
        with self._lock:
            return self._evaluate_locked(role, operation, money, account, userid)

    def consume(self, role: str, operation: str, amount, account=None, userid: Optional[str] = None, ref: str = '') -> VelocityDecision:
        if not self.enabled:
            money = parse_money(amount)
            return VelocityDecision(True, 'ok', 'ok', money, None, 0, None, None, operation)
        money = parse_money(amount)
        with self._lock:
            decision = self._evaluate_locked(role, operation, money, account, userid)
            if not decision.allowed:
                return decision
            now = self._now()
            movement = Movement(now, money, operation, ref)
            if operation != 'outbound':
                self.store.append(account_scope(account, operation), movement)
            if role == 'customer' and operation in OUTBOUND_OPS and userid:
                self.store.append(user_scope(userid), Movement(now, money, 'outbound', ref))
            return decision

    def release(self, operation: str, account=None, userid: Optional[str] = None, ref: str = '') -> int:
        dropped = 0
        if operation != 'outbound' and account is not None:
            dropped += self.store.release(account_scope(account, operation), ref)
        if userid:
            dropped += self.store.release(user_scope(userid), ref)
        return dropped

    def _evaluate_locked(self, role, operation, money, account, userid) -> VelocityDecision:
        band = self.policy.band_for(role, operation)
        scope = user_scope(userid) if operation == 'outbound' else account_scope(account, operation)
        used_amount, used_count, oldest = self._usage(scope, band.window_seconds)
        decision = self.policy.evaluate(band, used_amount, used_count, money, operation, oldest, self._now())
        if not decision.allowed:
            return decision
        if role == 'customer' and operation in OUTBOUND_OPS and userid:
            outbound = self._evaluate_locked(role, 'outbound', money, account=None, userid=userid)
            if not outbound.allowed:
                return outbound
        return decision

    def _usage(self, scope: str, window: int) -> Tuple[Decimal, int, Optional[float]]:
        since = self._now() - window
        rows = self.store.list_since(scope, since)
        if not rows:
            return ZERO, 0, None
        total = sum((m.amount for m in rows), ZERO)
        return total, len(rows), rows[0].ts


_service: Optional[VelocityService] = None


def get_service() -> VelocityService:
    global _service
    if _service is None:
        _service = VelocityService.from_env()
    return _service


def set_service(service: Optional[VelocityService]) -> None:
    global _service
    _service = service


def enforce_velocity(session, operation: str, account, amount, ref: str = '') -> Optional[Tuple[dict, int, dict]]:
    """Check + record. Returns (payload, status, headers) when denied, else None."""
    try:
        decision = get_service().consume(
            role_from_session(session),
            operation,
            amount,
            account=account,
            userid=(session or {}).get('userid'),
            ref=ref,
        )
    except AmountError:
        payload = {
            'error': 'invalid_amount',
            'code': 'invalid_amount',
            'message': 'Enter a valid amount',
            'operation': operation,
        }
        return payload, 400, {}
    if decision.allowed:
        return None
    headers = {}
    if decision.retry_after:
        headers['Retry-After'] = str(decision.retry_after)
    status = 400 if decision.code == 'invalid_amount' else 429
    return decision.as_dict(), status, headers


def attach_account_snapshots(session, accounts) -> dict:
    """Remaining daily limits keyed by account number, plus customer outbound."""
    if not isinstance(accounts, dict):
        return {}
    role = role_from_session(session)
    userid = (session or {}).get('userid')
    service = get_service()
    out = {'outbound': service.snapshot(role, 'outbound', userid=userid)}
    for _kind, info in accounts.items():
        if not isinstance(info, dict) or 'Account' not in info:
            continue
        acct = info['Account']
        out[str(acct)] = {
            'transfer': service.snapshot(role, 'transfer', account=acct),
            'withdraw': service.snapshot(role, 'withdraw', account=acct),
            'cheque': service.snapshot(role, 'cheque', account=acct),
        }
    return out
