"""SQLite-backed keyed event log that survives process restarts.

Used as the persistence layer for rate limiting (and later MFA cooldown /
idempotency) so counters are not lost when gunicorn workers recycle.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from typing import Optional


class DurableStore:
    def __init__(self, path: str):
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._ensure()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        return conn

    def _ensure(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    key TEXT NOT NULL,
                    ts REAL NOT NULL
                )
                """
            )
            conn.execute(
                'CREATE INDEX IF NOT EXISTS events_key_ts ON events(key, ts)'
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def append(self, key: str, ts: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute('INSERT INTO events(key, ts) VALUES(?, ?)', (key, ts))
            conn.commit()

    def count_since(self, key: str, since_ts: float) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT COUNT(*) FROM events WHERE key = ? AND ts >= ?',
                (key, since_ts),
            ).fetchone()
            return int(row[0] if row else 0)

    def oldest_since(self, key: str, since_ts: float) -> Optional[float]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT MIN(ts) FROM events WHERE key = ? AND ts >= ?',
                (key, since_ts),
            ).fetchone()
            if not row or row[0] is None:
                return None
            return float(row[0])

    def prune(self, key: str, before_ts: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                'DELETE FROM events WHERE key = ? AND ts < ?',
                (key, before_ts),
            )
            conn.commit()

    def clear(self, key: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute('DELETE FROM events WHERE key = ?', (key,))
            conn.execute('DELETE FROM kv WHERE key = ?', (key,))
            conn.commit()

    def get(self, key: str) -> Optional[str]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                'SELECT value FROM kv WHERE key = ?', (key,)
            ).fetchone()
            return None if not row else str(row[0])

    def set(self, key: str, value: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                'INSERT INTO kv(key, value) VALUES(?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                (key, value),
            )
            conn.commit()
