"""Time-bounded cache with single-flight request coalescing.

Why this exists: ``/metrics`` is backed by a live Microsoft Graph read. Without
a cache, one HTTP request to this service is one Graph token acquisition plus at
least one SharePoint list read, so anybody who can reach the endpoint can turn a
cheap loop into Graph throttling and a permanently-awake container. The cache
turns N callers inside the TTL into at most one upstream call.

Single-flight matters separately from the TTL. A plain TTL cache still lets a
burst of concurrent callers that all miss at once issue N upstream calls - the
classic cache stampede. Here, the first caller to miss starts the fetch and
every concurrent caller awaits that same task.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

__all__ = ["CacheStats", "SingleFlightCache"]


@dataclass(frozen=True)
class CacheStats:
    """Observable counters. Exposed for tests and structured logging, never to callers."""

    hits: int = 0
    misses: int = 0
    coalesced: int = 0
    upstream_calls: int = 0


class SingleFlightCache[T]:
    """Single-slot cache: fresh value inside the TTL, one upstream call outside it.

    Not keyed - this service computes exactly one derived value, so a keyed cache
    would add a dictionary and an eviction policy to hold a single entry.
    """

    def __init__(self, ttl_seconds: float, *, clock: Callable[[], float] | None = None) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must not be negative")
        self._ttl = ttl_seconds
        self._clock = clock or time.monotonic
        self._value: T | None = None
        self._stored_at: float | None = None
        self._inflight: asyncio.Future[T] | None = None
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0
        self._coalesced = 0
        self._upstream = 0

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def stats(self) -> CacheStats:
        return CacheStats(
            hits=self._hits,
            misses=self._misses,
            coalesced=self._coalesced,
            upstream_calls=self._upstream,
        )

    def _fresh(self) -> bool:
        if self._stored_at is None:
            return False
        return (self._clock() - self._stored_at) < self._ttl

    def age_seconds(self) -> float | None:
        """Age of the cached value, or None when nothing is cached."""
        if self._stored_at is None:
            return None
        return self._clock() - self._stored_at

    def invalidate(self) -> None:
        self._value = None
        self._stored_at = None

    async def get(self, factory: Callable[[], Awaitable[T]]) -> tuple[T, bool]:
        """Return ``(value, served_from_cache)``.

        ``factory`` is awaited at most once per TTL window even under concurrency.
        A failing factory is not cached: the exception propagates to every caller
        waiting on that flight, and the next call retries.
        """
        # Fast path: no lock needed to read a fresh value.
        if self._fresh():
            self._hits += 1
            return self._value, True  # type: ignore[return-value]

        async with self._lock:
            # Re-check: another coroutine may have refreshed while we waited.
            if self._fresh():
                self._hits += 1
                return self._value, True  # type: ignore[return-value]

            if self._inflight is not None and not self._inflight.done():
                flight = self._inflight
                self._coalesced += 1
            else:
                self._misses += 1
                self._upstream += 1
                flight = asyncio.ensure_future(factory())
                self._inflight = flight

        try:
            value = await asyncio.shield(flight)
        except asyncio.CancelledError:
            # One caller going away must not cancel the shared flight for the others.
            raise
        finally:
            if flight.done() and self._inflight is flight:
                self._inflight = None

        # Only the coroutine that started the flight stores the result; the
        # others read the same object they just awaited.
        if not self._fresh():
            self._value = value
            self._stored_at = self._clock()
        return value, False
