"""Fixed-window rate limiter, per client.

Deliberately in-process and deliberately small. The API runs at
``maxReplicas: 1``, so a process-local counter is the whole fleet; introducing
Redis to rate-limit a single container would cost more than the thing it
protects. If the service ever scales out, this becomes per-replica and the
docstring is the warning label.

This is abuse protection, not a quota system: it bounds how fast one caller can
force cache misses, and it is a second line behind the cache and the auth check.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

__all__ = ["RateLimiter"]


class RateLimiter:
    """Sliding-window limiter: at most ``limit`` events per ``window_seconds`` per key."""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] | None = None,
        max_keys: int = 4096,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._limit = limit
        self._window = window_seconds
        self._clock = clock or time.monotonic
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        bucket = self._hits.setdefault(key, deque())
        cutoff = now - self._window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        return bucket

    def _evict_if_needed(self) -> None:
        # Unbounded key growth is itself a memory-exhaustion vector. When the map
        # is full, drop the emptiest buckets first - they are the least active.
        if len(self._hits) <= self._max_keys:
            return
        for key in sorted(self._hits, key=lambda k: len(self._hits[k]))[: self._max_keys // 4]:
            self._hits.pop(key, None)

    def check(self, key: str) -> tuple[bool, float]:
        """Record an attempt for ``key``.

        Returns ``(allowed, retry_after_seconds)``. ``retry_after_seconds`` is 0
        when allowed, otherwise the seconds until the oldest hit leaves the window.
        """
        now = self._clock()
        bucket = self._prune(key, now)
        if len(bucket) >= self._limit:
            retry_after = max(0.0, (bucket[0] + self._window) - now)
            return False, retry_after
        bucket.append(now)
        self._evict_if_needed()
        return True, 0.0

    def reset(self) -> None:
        self._hits.clear()
