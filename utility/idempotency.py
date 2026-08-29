"""Request idempotency for mutating banking operations.

Client supplies `Idempotency-Key` (header or JSON). The same actor + operation
+ key replays the original response; a different payload with that key is a
conflict. In-flight duplicates are rejected so a double-click cannot run the
handler twice. Stores are swappable (memory for tests, sqlite for restarts).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, Iterator, Optional, Tuple
from functools import wraps

from utility.money import try_canonical_amount

KEY_HEADER = 'Idempotency-Key'
KEY_BODY_FIELD = 'idempotency_key'
REPLAY_HEADER = 'Idempotent-Replayed'
KEY_PATTERN = re.compile(r'^[A-Za-z0-9._-]{8,128}$')

DEFAULT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_PENDING_TIMEOUT = 60

_AMOUNT_KEYS = frozenset({'amount'})
_SKIP_FINGERPRINT_KEYS = frozenset({KEY_BODY_FIELD, 'Idempotency-Key'})
_IDENTITY_KEYS = frozenset({
    'fromAccount', 'toAccount', 'account', 'from_account', 'to_account',
    'cheque_no', 'transaction_no', 'account_no', 'userid', 'customer_id',
})


class IdempotencyKeyError(ValueError):
    def __init__(self, message: str, code: str = 'invalid_idempotency_key'):
        super().__init__(message)
        self.code = code


def validate_key(key: Any) -> str:
    if not isinstance(key, str) or not KEY_PATTERN.match(key):
        raise IdempotencyKeyError(
            'Idempotency-Key must be 8-128 characters of A-Z, a-z, 0-9, ".", "_" or "-"'
        )
    return key


def _canonical_value(key: str, value: Any) -> Any:
    if key in _SKIP_FINGERPRINT_KEYS:
        return None
    if key in _AMOUNT_KEYS:
        canonical = try_canonical_amount(value)
        return canonical if canonical is not None else value
    if key in _IDENTITY_KEYS:
        if value is None or isinstance(value, bool):
            return value
        return str(value).strip()
    if isinstance(value, dict):
        return _canonical_payload(value)
    if isinstance(value, list):
        return [_canonical_value('', item) for item in value]
    return value


def _canonical_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    out = {}
    for key in sorted(payload.keys()):
        if key in _SKIP_FINGERPRINT_KEYS:
            continue
        out[key] = _canonical_value(key, payload[key])
    return out


def fingerprint_payload(payload: Any) -> str:
    canonical = _canonical_payload(payload if isinstance(payload, dict) else {})
    blob = json.dumps(canonical, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


@dataclass
class IdempotencyRecord:
    scope: str
    key: str
    fingerprint: str
    state: str  # pending | completed
    created_at: float
    status: Optional[int] = None
    body: Optional[str] = None
    content_type: Optional[str] = None
    completed_at: Optional[float] = None


@dataclass
class BeginResult:
    kind: str  # miss | replay | conflict | in_progress
    record: Optional[IdempotencyRecord] = None


class IdempotencyStore:
    def get(self, scope: str, key: str) -> Optional[IdempotencyRecord]:
        raise NotImplementedError

    def insert_pending(self, record: IdempotencyRecord) -> bool:
        """Insert pending row. Return False if (scope, key) already exists."""
        raise NotImplementedError

    def complete(self, scope: str, key: str, status: int, body: str,
                 content_type: str, completed_at: float) -> None:
        raise NotImplementedError

    def release(self, scope: str, key: str) -> None:
        raise NotImplementedError

    def take_over_stale(self, scope: str, key: str, fingerprint: str,
                        now: float, stale_before: float) -> bool:
        """Reset a stale pending row so this caller owns it. False if not stale/missing."""
        raise NotImplementedError

    def prune(self, cutoff: float) -> None:
        raise NotImplementedError

    @contextmanager
    def locked(self) -> Iterator[None]:
        yield


class MemoryIdempotencyStore(IdempotencyStore):
    def __init__(self):
        self._lock = threading.RLock()
        self._rows: Dict[Tuple[str, str], IdempotencyRecord] = {}

    @contextmanager
    def locked(self) -> Iterator[None]:
        with self._lock:
            yield

    def get(self, scope: str, key: str) -> Optional[IdempotencyRecord]:
        rec = self._rows.get((scope, key))
        if rec is None:
            return None
        return IdempotencyRecord(**rec.__dict__)

    def insert_pending(self, record: IdempotencyRecord) -> bool:
        idx = (record.scope, record.key)
        if idx in self._rows:
            return False
        self._rows[idx] = IdempotencyRecord(**record.__dict__)
        return True

    def complete(self, scope: str, key: str, status: int, body: str,
                 content_type: str, completed_at: float) -> None:
        rec = self._rows.get((scope, key))
        if rec is None:
            return
        rec.state = 'completed'
        rec.status = status
        rec.body = body
        rec.content_type = content_type
        rec.completed_at = completed_at

    def release(self, scope: str, key: str) -> None:
        self._rows.pop((scope, key), None)

    def take_over_stale(self, scope: str, key: str, fingerprint: str,
                        now: float, stale_before: float) -> bool:
        rec = self._rows.get((scope, key))
        if rec is None or rec.state != 'pending' or rec.created_at > stale_before:
            return False
        rec.fingerprint = fingerprint
        rec.created_at = now
        rec.status = None
        rec.body = None
        rec.content_type = None
        rec.completed_at = None
        return True

    def prune(self, cutoff: float) -> None:
        expired = [idx for idx, rec in self._rows.items() if rec.created_at < cutoff]
        for idx in expired:
            self._rows.pop(idx, None)


class SqliteIdempotencyStore(IdempotencyStore):
    """Restart-safe store. Independent of PR #12 DurableStore (event log + kv)."""

    def __init__(self, path: str):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.execute('PRAGMA busy_timeout=5000')
        self._conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS idempotency (
                scope TEXT NOT NULL,
                key TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                status INTEGER,
                body TEXT,
                content_type TEXT,
                created_at REAL NOT NULL,
                completed_at REAL,
                PRIMARY KEY (scope, key)
            )
            '''
        )

    @contextmanager
    def locked(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute('BEGIN IMMEDIATE')
            try:
                yield
                self._conn.execute('COMMIT')
            except Exception:
                self._conn.execute('ROLLBACK')
                raise

    def _row(self, raw) -> Optional[IdempotencyRecord]:
        if raw is None:
            return None
        return IdempotencyRecord(
            scope=raw[0], key=raw[1], fingerprint=raw[2], state=raw[3],
            status=raw[4], body=raw[5], content_type=raw[6],
            created_at=raw[7], completed_at=raw[8],
        )

    def get(self, scope: str, key: str) -> Optional[IdempotencyRecord]:
        cur = self._conn.execute(
            'SELECT scope, key, fingerprint, state, status, body, content_type, '
            'created_at, completed_at FROM idempotency WHERE scope=? AND key=?',
            (scope, key),
        )
        return self._row(cur.fetchone())

    def insert_pending(self, record: IdempotencyRecord) -> bool:
        cur = self._conn.execute(
            '''
            INSERT OR IGNORE INTO idempotency
                (scope, key, fingerprint, state, status, body, content_type, created_at, completed_at)
            VALUES (?, ?, ?, 'pending', NULL, NULL, NULL, ?, NULL)
            ''',
            (record.scope, record.key, record.fingerprint, record.created_at),
        )
        return cur.rowcount == 1

    def complete(self, scope: str, key: str, status: int, body: str,
                 content_type: str, completed_at: float) -> None:
        self._conn.execute(
            '''
            UPDATE idempotency
               SET state='completed', status=?, body=?, content_type=?, completed_at=?
             WHERE scope=? AND key=?
            ''',
            (status, body, content_type, completed_at, scope, key),
        )

    def release(self, scope: str, key: str) -> None:
        self._conn.execute('DELETE FROM idempotency WHERE scope=? AND key=?', (scope, key))

    def take_over_stale(self, scope: str, key: str, fingerprint: str,
                        now: float, stale_before: float) -> bool:
        cur = self._conn.execute(
            '''
            UPDATE idempotency
               SET fingerprint=?, created_at=?, status=NULL, body=NULL,
                   content_type=NULL, completed_at=NULL, state='pending'
             WHERE scope=? AND key=? AND state='pending' AND created_at<=?
            ''',
            (fingerprint, now, scope, key, stale_before),
        )
        return cur.rowcount == 1

    def prune(self, cutoff: float) -> None:
        self._conn.execute('DELETE FROM idempotency WHERE created_at < ?', (cutoff,))

    def close(self) -> None:
        self._conn.close()


class IdempotencyService:
    def __init__(
        self,
        store: IdempotencyStore,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        pending_timeout: int = DEFAULT_PENDING_TIMEOUT,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.ttl_seconds = ttl_seconds
        self.pending_timeout = pending_timeout
        self.clock = clock

    def begin(self, key: str, scope: str, fingerprint: str) -> BeginResult:
        key = validate_key(key)
        now = self.clock()
        with self.store.locked():
            self.store.prune(now - self.ttl_seconds)
            existing = self.store.get(scope, key)
            if existing is None:
                created = self.store.insert_pending(IdempotencyRecord(
                    scope=scope, key=key, fingerprint=fingerprint,
                    state='pending', created_at=now,
                ))
                if created:
                    return BeginResult('miss')
                existing = self.store.get(scope, key)
                if existing is None:
                    return BeginResult('miss')

            if existing.state == 'completed':
                if existing.fingerprint != fingerprint:
                    return BeginResult('conflict', existing)
                return BeginResult('replay', existing)

            stale_before = now - self.pending_timeout
            if existing.created_at <= stale_before:
                if existing.fingerprint != fingerprint:
                    return BeginResult('conflict', existing)
                if self.store.take_over_stale(scope, key, fingerprint, now, stale_before):
                    return BeginResult('miss', existing)
            return BeginResult('in_progress', existing)

    def complete(self, key: str, scope: str, status: int, body: str,
                 content_type: str = 'application/json') -> None:
        with self.store.locked():
            self.store.complete(scope, key, status, body, content_type, self.clock())

    def release(self, key: str, scope: str) -> None:
        with self.store.locked():
            self.store.release(scope, key)


_service: Optional[IdempotencyService] = None


def set_idempotency(service: Optional[IdempotencyService]) -> None:
    global _service
    _service = service


def get_idempotency() -> Optional[IdempotencyService]:
    return _service


def extract_key(request) -> Optional[str]:
    header = request.headers.get(KEY_HEADER)
    if header:
        return header.strip()
    payload = request.get_json(silent=True) or {}
    body_key = payload.get(KEY_BODY_FIELD)
    if isinstance(body_key, str):
        return body_key.strip()
    return None


def _scope(session, operation: str) -> str:
    userid = session.get('userid') or 'anonymous'
    return f'{userid}:{operation}'


def _json_error(code: str, message: str, status: int):
    from flask import jsonify
    return jsonify({'error': code, 'message': message}), status


def _capture(result):
    from flask import make_response
    response = make_response(result)
    return response, response.status_code, response.get_data(as_text=True), response.content_type


def _replay_response(record: IdempotencyRecord):
    from flask import make_response
    response = make_response(record.body or '', record.status or 200)
    if record.content_type:
        response.headers['Content-Type'] = record.content_type
    response.headers[REPLAY_HEADER] = 'true'
    return response


def flask_idempotent(operation: str):
    """Replay stored responses for money-moving POSTs when a key is present.

    Missing keys fall through unchanged (backward compatible with old clients).
    GET is never idempotent-cached. 5xx / uncaught errors release the key so
    the client can retry the same operation.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            from flask import request, session
            if request.method == 'GET':
                return fn(*args, **kwargs)
            service = get_idempotency()
            if service is None:
                return fn(*args, **kwargs)
            raw_key = extract_key(request)
            if not raw_key:
                return fn(*args, **kwargs)
            try:
                key = validate_key(raw_key)
            except IdempotencyKeyError as exc:
                return _json_error(exc.code, str(exc), 400)

            payload = request.get_json(silent=True) or {}
            fingerprint = fingerprint_payload(payload)
            scope = _scope(session, operation)
            outcome = service.begin(key, scope, fingerprint)
            if outcome.kind == 'replay':
                return _replay_response(outcome.record)
            if outcome.kind == 'conflict':
                return _json_error(
                    'idempotency_key_reused',
                    'Idempotency-Key was already used with a different request',
                    409,
                )
            if outcome.kind == 'in_progress':
                return _json_error(
                    'idempotency_in_progress',
                    'A request with this Idempotency-Key is already in progress',
                    409,
                )
            try:
                result = fn(*args, **kwargs)
                response, status, body, content_type = _capture(result)
                if status >= 500:
                    service.release(key, scope)
                    return response
                service.complete(key, scope, status, body, content_type)
                return response
            except Exception:
                service.release(key, scope)
                raise
        return wrapper
    return decorator
