"""RateLimiter: window behaviour, per-key isolation, and bounded memory."""

from __future__ import annotations

import pytest

from app.ratelimit import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_requests_under_the_limit_are_allowed() -> None:
    limiter = RateLimiter(3, 60.0, clock=FakeClock())

    assert [limiter.check("a")[0] for _ in range(3)] == [True, True, True]


def test_the_request_over_the_limit_is_refused() -> None:
    limiter = RateLimiter(3, 60.0, clock=FakeClock())
    for _ in range(3):
        limiter.check("a")

    allowed, retry_after = limiter.check("a")

    assert allowed is False
    assert retry_after == pytest.approx(60.0)


def test_the_window_slides_rather_than_resetting() -> None:
    # A fixed window lets a caller send 2x the limit across a boundary. A
    # sliding window does not, and this is the test that tells them apart.
    clock = FakeClock()
    limiter = RateLimiter(2, 10.0, clock=clock)

    limiter.check("a")
    clock.advance(9.0)
    limiter.check("a")
    clock.advance(0.5)

    assert limiter.check("a")[0] is False  # first hit has not aged out yet

    clock.advance(1.0)  # now it has

    assert limiter.check("a")[0] is True


def test_retry_after_counts_down_as_the_window_ages() -> None:
    clock = FakeClock()
    limiter = RateLimiter(1, 10.0, clock=clock)
    limiter.check("a")
    clock.advance(6.0)

    _, retry_after = limiter.check("a")

    assert retry_after == pytest.approx(4.0)


def test_keys_are_isolated() -> None:
    limiter = RateLimiter(1, 60.0, clock=FakeClock())
    limiter.check("a")

    assert limiter.check("a")[0] is False
    assert limiter.check("b")[0] is True


def test_reset_clears_every_key() -> None:
    limiter = RateLimiter(1, 60.0, clock=FakeClock())
    limiter.check("a")
    limiter.reset()

    assert limiter.check("a")[0] is True


def test_key_growth_is_bounded() -> None:
    # An attacker rotating X-Forwarded-For must not be able to grow the map
    # until the container is OOM-killed.
    limiter = RateLimiter(5, 60.0, clock=FakeClock(), max_keys=64)

    for index in range(1_000):
        limiter.check(f"client-{index}")

    assert len(limiter._hits) <= 64 + 1


@pytest.mark.parametrize(("limit", "window"), [(0, 60.0), (-1, 60.0), (1, 0.0), (1, -5.0)])
def test_invalid_configuration_is_rejected(limit: int, window: float) -> None:
    with pytest.raises(ValueError):
        RateLimiter(limit, window)
