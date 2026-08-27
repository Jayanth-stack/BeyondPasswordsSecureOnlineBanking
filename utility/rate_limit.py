"""Sliding-window rate limiter on top of DurableStore.

`consume` is atomic enough for this app (process-local lock + SQLite).
Callers can `over_limit` (peek) for user buckets that should only count
failures, and `reset` after a successful login.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from utility.durable_store import DurableStore


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int, key: str):
        super().__init__('rate limit exceeded')
        self.retry_after = max(1, int(retry_after))
        self.key = key


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after: int
    count: int


class RateLimiter:
    def __init__(self, store: DurableStore, clock: Optional[Callable[[], float]] = None):
        self.store = store
        self.clock = clock or time.time

    @classmethod
    def from_path(cls, path: str, clock: Optional[Callable[[], float]] = None) -> 'RateLimiter':
        return cls(DurableStore(path), clock=clock)

    def over_limit(self, key: str, limit: int, window_seconds: int) -> bool:
        now = self.clock()
        cutoff = now - window_seconds
        return self.store.count_since(key, cutoff) >= limit

    def remaining(self, key: str, limit: int, window_seconds: int) -> int:
        now = self.clock()
        cutoff = now - window_seconds
        return max(0, limit - self.store.count_since(key, cutoff))

    def consume(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        now = self.clock()
        cutoff = now - window_seconds
        self.store.prune(key, cutoff)
        count = self.store.count_since(key, cutoff)
        if count >= limit:
            oldest = self.store.oldest_since(key, cutoff) or now
            retry_after = int(window_seconds - (now - oldest)) + 1
            raise RateLimitExceeded(retry_after, key)
        self.store.append(key, now)
        remaining = max(0, limit - count - 1)
        return RateLimitResult(True, remaining, 0, count + 1)

    def reset(self, key: str) -> None:
        self.store.clear(key)
