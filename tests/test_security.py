"""Authentication and rate limiting on /metrics.

These are the regression tests for the defect this work exists to close: the
endpoint served live tenant data to anyone, and every anonymous call reached
Microsoft Graph.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_sharepoint_client
from app.models import Ticket
from app.security import RATE_LIMIT_REQUESTS
from tests.conftest import AUTH_HEADER, ENV_VARS
from tests.fakes import FakeSharePoint

#: Counts real upstream reads, not dependency resolutions. FastAPI builds the
#: dependency on every request even when the endpoint answers from cache, so
#: counting in the override would measure the wrong thing entirely.
UPSTREAM_CALLS: list[int] = []


class CountingSharePoint(FakeSharePoint):
    """FakeSharePoint that records each actual list read."""

    async def list_tickets(self) -> list[Ticket]:
        UPSTREAM_CALLS.append(1)
        return await super().list_tickets()


@pytest.fixture
def client(configured_env: None) -> Iterator[TestClient]:
    UPSTREAM_CALLS.clear()

    async def override() -> CountingSharePoint:
        return CountingSharePoint(tickets=[Ticket(status="New")])

    app.dependency_overrides[get_sharepoint_client] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    UPSTREAM_CALLS.clear()


# 18. Unauthenticated access ---------------------------------------------------


def test_metrics_without_a_token_is_401(client: TestClient) -> None:
    response = client.get("/metrics")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_metrics_without_a_token_never_reaches_graph(client: TestClient) -> None:
    # The amplification half of the defect: rejecting late still costs a Graph
    # call. Authentication is a dependency, so it rejects before the endpoint
    # body - and therefore before any upstream request - runs.
    client.get("/metrics")

    assert UPSTREAM_CALLS == []


def test_metrics_with_a_wrong_token_is_401(client: TestClient) -> None:
    response = client.get("/metrics", headers={"Authorization": "Bearer not-the-right-token"})

    assert response.status_code == 401
    assert UPSTREAM_CALLS == []


def test_metrics_with_the_wrong_scheme_is_401(client: TestClient) -> None:
    response = client.get(
        "/metrics", headers={"Authorization": f"Basic {ENV_VARS['METRICS_API_TOKEN']}"}
    )

    assert response.status_code == 401


def test_metrics_with_an_empty_bearer_is_401(client: TestClient) -> None:
    assert client.get("/metrics", headers={"Authorization": "Bearer "}).status_code == 401


def test_a_token_prefix_is_not_accepted(client: TestClient) -> None:
    # compare_digest, not startswith: a prefix must not authenticate.
    truncated = ENV_VARS["METRICS_API_TOKEN"][:-1]

    response = client.get("/metrics", headers={"Authorization": f"Bearer {truncated}"})

    assert response.status_code == 401


def test_metrics_with_a_valid_token_is_200(client: TestClient) -> None:
    response = client.get("/metrics", headers=AUTH_HEADER)

    assert response.status_code == 200
    assert response.json()["open_tickets"] == 1


def test_a_401_body_does_not_disclose_the_expected_token(client: TestClient) -> None:
    response = client.get("/metrics", headers={"Authorization": "Bearer wrong"})

    assert ENV_VARS["METRICS_API_TOKEN"] not in response.text
    assert "wrong" not in response.text


# 19. Fail closed --------------------------------------------------------------


def test_metrics_refuses_when_no_token_is_configured(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset METRICS_API_TOKEN must close the endpoint, not reopen it.

    This is the single most important test in the file. The failure mode it
    guards against - "auth is optional, so an unconfigured deployment serves
    everything" - is exactly how the original defect would come back.
    """
    from app.config import get_settings

    for name, value in ENV_VARS.items():
        if name != "METRICS_API_TOKEN":
            monkeypatch.setenv(name, value)
    monkeypatch.delenv("METRICS_API_TOKEN", raising=False)
    get_settings.cache_clear()

    with TestClient(app) as test_client:
        unauthenticated = test_client.get("/metrics")
        with_a_token = test_client.get("/metrics", headers=AUTH_HEADER)

    get_settings.cache_clear()

    assert unauthenticated.status_code == 503
    assert with_a_token.status_code == 503
    assert "token" not in unauthenticated.text.lower()  # no hint about what is missing


# 20. Rate limiting ------------------------------------------------------------


def test_rate_limit_rejects_a_burst_with_429(client: TestClient) -> None:
    statuses = [
        client.get("/metrics", headers=AUTH_HEADER).status_code
        for _ in range(RATE_LIMIT_REQUESTS + 5)
    ]

    assert statuses[:RATE_LIMIT_REQUESTS] == [200] * RATE_LIMIT_REQUESTS
    assert set(statuses[RATE_LIMIT_REQUESTS:]) == {429}


def test_a_429_carries_retry_after(client: TestClient) -> None:
    for _ in range(RATE_LIMIT_REQUESTS):
        client.get("/metrics", headers=AUTH_HEADER)

    throttled = client.get("/metrics", headers=AUTH_HEADER)

    assert throttled.status_code == 429
    assert int(throttled.headers["retry-after"]) >= 1


def test_the_rate_limit_is_per_client(client: TestClient) -> None:
    # Container Apps terminates TLS, so every caller looks identical without
    # X-Forwarded-For. Bucketing on it is what keeps one noisy client from
    # throttling everybody else.
    noisy = {**AUTH_HEADER, "X-Forwarded-For": "198.51.100.7"}
    for _ in range(RATE_LIMIT_REQUESTS + 2):
        client.get("/metrics", headers=noisy)

    other = {**AUTH_HEADER, "X-Forwarded-For": "203.0.113.9"}

    assert client.get("/metrics", headers=noisy).status_code == 429
    assert client.get("/metrics", headers=other).status_code == 200


def test_unauthenticated_requests_do_not_consume_the_limit(client: TestClient) -> None:
    # Auth runs before the limiter, so an anonymous flood cannot exhaust a
    # legitimate caller's budget from behind the same egress address.
    for _ in range(RATE_LIMIT_REQUESTS + 10):
        client.get("/metrics")

    assert client.get("/metrics", headers=AUTH_HEADER).status_code == 200


# 21. De-amplification end to end ----------------------------------------------


def test_repeated_calls_inside_the_cache_window_hit_graph_once(client: TestClient) -> None:
    """The amplification regression test, measured at the HTTP boundary.

    Before this work, N requests to /metrics meant N SharePoint reads (and N
    token acquisitions). The dependency override counts how many times the
    endpoint actually reached for an upstream client.
    """
    for _ in range(RATE_LIMIT_REQUESTS):
        assert client.get("/metrics", headers=AUTH_HEADER).status_code == 200

    assert len(UPSTREAM_CALLS) == 1


def test_the_deadline_turns_a_hung_upstream_into_504(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung Graph call must not hold the caller's connection open forever.

    app.graph bounds each individual HTTP request, but not the aggregate: a
    deeply paged list could retry its way past any per-request timeout. The
    endpoint-level deadline is what bounds the whole refresh.
    """
    import app.main as main_module

    monkeypatch.setattr(main_module, "METRICS_DEADLINE_SECONDS", 0.05)

    async def override() -> object:
        class Hung:
            async def list_tickets(self) -> list[Ticket]:
                await asyncio.sleep(5)
                return []

        return Hung()

    app.dependency_overrides[get_sharepoint_client] = override

    response = client.get("/metrics", headers=AUTH_HEADER)

    assert response.status_code == 504
    assert "timed out" in response.json()["detail"].lower()
