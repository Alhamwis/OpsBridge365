"""SingleFlightCache: TTL, expiry, concurrent coalescing, and failure handling.

The cache is the de-amplification control. Every property tested here is a
property the /metrics endpoint depends on to stop one HTTP request becoming
several Microsoft Graph calls.
"""

from __future__ import annotations

import asyncio

import pytest

from app.cache import SingleFlightCache


class FakeClock:
    """Manual monotonic clock, so TTL expiry is tested rather than slept through."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# 21. TTL ----------------------------------------------------------------------


async def test_a_second_call_inside_the_ttl_does_not_call_upstream() -> None:
    calls: list[int] = []
    cache: SingleFlightCache[str] = SingleFlightCache(60.0, clock=FakeClock())

    async def factory() -> str:
        calls.append(1)
        return "value"

    first, from_cache_first = await cache.get(factory)
    second, from_cache_second = await cache.get(factory)

    assert (first, second) == ("value", "value")
    assert (from_cache_first, from_cache_second) == (False, True)
    assert len(calls) == 1


async def test_the_value_is_refetched_after_the_ttl_expires() -> None:
    clock = FakeClock()
    calls: list[int] = []
    cache: SingleFlightCache[int] = SingleFlightCache(30.0, clock=clock)

    async def factory() -> int:
        calls.append(1)
        return len(calls)

    assert (await cache.get(factory))[0] == 1
    clock.advance(29.9)
    assert (await cache.get(factory))[0] == 1  # still inside the window
    clock.advance(0.2)
    assert (await cache.get(factory))[0] == 2  # window closed, refetched

    assert len(calls) == 2


async def test_a_zero_ttl_never_serves_from_cache() -> None:
    calls: list[int] = []
    cache: SingleFlightCache[int] = SingleFlightCache(0.0, clock=FakeClock())

    async def factory() -> int:
        calls.append(1)
        return len(calls)

    await cache.get(factory)
    await cache.get(factory)

    assert len(calls) == 2


def test_a_negative_ttl_is_rejected() -> None:
    with pytest.raises(ValueError):
        SingleFlightCache(-1.0)


# 22. Single flight ------------------------------------------------------------


async def test_concurrent_misses_collapse_into_one_upstream_call() -> None:
    """The stampede test.

    A plain TTL cache still lets N callers that all miss at the same instant
    make N upstream calls. Twenty concurrent callers here must produce exactly
    one Graph read.
    """
    calls: list[int] = []
    release = asyncio.Event()
    cache: SingleFlightCache[str] = SingleFlightCache(60.0, clock=FakeClock())

    async def slow_factory() -> str:
        calls.append(1)
        await release.wait()
        return "value"

    waiters = [asyncio.create_task(cache.get(slow_factory)) for _ in range(20)]
    await asyncio.sleep(0)  # let every task reach the cache
    release.set()
    results = await asyncio.gather(*waiters)

    assert len(calls) == 1
    assert {value for value, _ in results} == {"value"}
    assert cache.stats().upstream_calls == 1
    assert cache.stats().coalesced >= 1


async def test_a_second_wave_after_expiry_calls_upstream_again() -> None:
    clock = FakeClock()
    calls: list[int] = []
    cache: SingleFlightCache[int] = SingleFlightCache(10.0, clock=clock)

    async def factory() -> int:
        calls.append(1)
        return len(calls)

    await asyncio.gather(*(cache.get(factory) for _ in range(5)))
    clock.advance(11.0)
    await asyncio.gather(*(cache.get(factory) for _ in range(5)))

    assert len(calls) == 2


# 23. Failure handling ---------------------------------------------------------


async def test_a_failing_factory_is_not_cached() -> None:
    calls: list[int] = []
    cache: SingleFlightCache[str] = SingleFlightCache(60.0, clock=FakeClock())

    async def failing() -> str:
        calls.append(1)
        raise RuntimeError("upstream down")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cache.get(failing)

    assert len(calls) == 2  # each call retried; the error was never stored


async def test_every_concurrent_caller_sees_the_same_failure() -> None:
    cache: SingleFlightCache[str] = SingleFlightCache(60.0, clock=FakeClock())
    release = asyncio.Event()

    async def failing() -> str:
        await release.wait()
        raise RuntimeError("upstream down")

    waiters = [asyncio.create_task(cache.get(failing)) for _ in range(5)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*waiters, return_exceptions=True)

    assert all(isinstance(r, RuntimeError) for r in results)


async def test_a_stale_value_is_replaced_after_a_failure_recovers() -> None:
    clock = FakeClock()
    outcomes = iter([RuntimeError("down"), None])
    cache: SingleFlightCache[str] = SingleFlightCache(10.0, clock=clock)

    async def flaky() -> str:
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return "recovered"

    with pytest.raises(RuntimeError):
        await cache.get(flaky)
    value, from_cache = await cache.get(flaky)

    assert (value, from_cache) == ("recovered", False)


# 24. Observability ------------------------------------------------------------


async def test_age_is_none_before_anything_is_cached() -> None:
    cache: SingleFlightCache[str] = SingleFlightCache(10.0, clock=FakeClock())

    assert cache.age_seconds() is None


async def test_age_tracks_the_clock() -> None:
    clock = FakeClock()
    cache: SingleFlightCache[str] = SingleFlightCache(10.0, clock=clock)

    async def factory() -> str:
        return "v"

    await cache.get(factory)
    clock.advance(4.0)

    assert cache.age_seconds() == pytest.approx(4.0)


async def test_invalidate_forces_the_next_call_upstream() -> None:
    calls: list[int] = []
    cache: SingleFlightCache[int] = SingleFlightCache(60.0, clock=FakeClock())

    async def factory() -> int:
        calls.append(1)
        return len(calls)

    await cache.get(factory)
    cache.invalidate()
    await cache.get(factory)

    assert len(calls) == 2
