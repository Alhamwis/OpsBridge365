"""FastAPI endpoints: /healthz, /demo/metrics, /metrics, and the no-secrets rule."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.graph import GraphAuthError, GraphHTTPError
from app.main import app, get_sharepoint_client
from app.models import Ticket
from tests.conftest import AUTH_HEADER, ENV_VARS
from tests.fakes import FakeSharePoint

NOW = datetime.now(UTC)


@pytest.fixture
def client(configured_env: None) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _use_tickets(tickets: list[Ticket]) -> None:
    async def override() -> FakeSharePoint:
        return FakeSharePoint(tickets=tickets)

    app.dependency_overrides[get_sharepoint_client] = override


# 15. GET /healthz -------------------------------------------------------------


def test_healthz_reports_status_and_version(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "9.9.9-test"}


def test_healthz_needs_no_authentication(client: TestClient) -> None:
    # The synthetic-health workflow and the container HEALTHCHECK both call this
    # with no credential. Putting auth in front of it would break both.
    assert client.get("/healthz").status_code == 200


def test_healthz_is_not_cached(client: TestClient) -> None:
    # A cached liveness probe is a liveness probe that lies.
    assert client.get("/healthz").headers["cache-control"] == "no-store"


def test_healthz_is_still_200_when_configuration_is_missing(clean_env: None) -> None:
    # A container HEALTHCHECK runs before credentials are mounted, and an
    # unconfigured container is unhealthy for the operator, not for the
    # orchestrator. /healthz answers regardless; /metrics is what 503s.
    with TestClient(app) as test_client:
        response = test_client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}
    assert "AZURE_CLIENT_SECRET" not in response.text  # no variable names leaked to callers


def test_metrics_is_503_when_configuration_is_missing(clean_env: None) -> None:
    with TestClient(app) as test_client:
        response = test_client.get("/metrics", headers=AUTH_HEADER)

    assert response.status_code == 503
    body = response.text
    assert "AZURE_CLIENT_SECRET" not in body  # no variable names leaked to callers


# 16. GET /metrics -------------------------------------------------------------


def test_metrics_returns_the_computed_payload(client: TestClient) -> None:
    _use_tickets(
        [
            Ticket(status="New", sla_resolution_due=NOW + timedelta(minutes=10)),
            Ticket(status="In Progress", sla_resolution_due=NOW + timedelta(days=2)),
            Ticket(
                status="Resolved",
                sla_resolution_due=NOW - timedelta(days=1),
                resolved_date=NOW - timedelta(days=1, hours=3),
            ),
        ]
    )

    response = client.get("/metrics", headers=AUTH_HEADER)
    payload = response.json()

    assert response.status_code == 200
    assert payload["open_tickets"] == 2
    assert payload["due_within_30min"] == 1
    assert payload["sla_compliance_7d_pct"] == 100.0
    assert payload["resolved_last_7d"] == 1
    assert "generated_at" in payload


def test_metrics_with_no_tickets_reports_null_compliance(client: TestClient) -> None:
    _use_tickets([])

    payload = client.get("/metrics", headers=AUTH_HEADER).json()

    assert payload["open_tickets"] == 0
    assert payload["sla_compliance_7d_pct"] is None


def test_metrics_never_echoes_configuration_or_secrets(client: TestClient) -> None:
    _use_tickets([Ticket(status="New")])

    response = client.get("/metrics", headers=AUTH_HEADER)
    body = response.text

    for value in ENV_VARS.values():
        assert value not in body
    # The bearer token is presented on every call; it must never come back out,
    # in the body or in a response header.
    assert ENV_VARS["METRICS_API_TOKEN"] not in str(dict(response.headers))


def test_metrics_returns_502_when_graph_fails(client: TestClient) -> None:
    async def override() -> object:
        class Broken:
            async def list_tickets(self) -> list[Ticket]:
                raise GraphHTTPError("upstream exploded", status_code=500)

        return Broken()

    app.dependency_overrides[get_sharepoint_client] = override

    response = client.get("/metrics", headers=AUTH_HEADER)

    assert response.status_code == 502
    assert "upstream exploded" not in response.text  # internals stay in the logs


def test_metrics_returns_502_when_authentication_fails(client: TestClient) -> None:
    async def override() -> object:
        class Broken:
            async def list_tickets(self) -> list[Ticket]:
                raise GraphAuthError("invalid_client: secret rejected")

        return Broken()

    app.dependency_overrides[get_sharepoint_client] = override

    response = client.get("/metrics", headers=AUTH_HEADER)

    assert response.status_code == 502
    assert "secret" not in response.text.lower()


def test_a_failed_refresh_is_not_cached(client: TestClient) -> None:
    # A cached exception would turn one transient Graph blip into a TTL-long
    # outage, and would hide recovery from the caller.
    calls: list[int] = []

    async def override() -> object:
        class Flaky:
            async def list_tickets(self) -> list[Ticket]:
                calls.append(1)
                raise GraphHTTPError("transient", status_code=503)

        return Flaky()

    app.dependency_overrides[get_sharepoint_client] = override

    assert client.get("/metrics", headers=AUTH_HEADER).status_code == 502
    assert client.get("/metrics", headers=AUTH_HEADER).status_code == 502
    assert len(calls) == 2  # the second call really did retry upstream


# 16b. /metrics response headers -----------------------------------------------


def test_metrics_marks_cache_hits_and_misses(client: TestClient) -> None:
    _use_tickets([Ticket(status="New")])

    first = client.get("/metrics", headers=AUTH_HEADER)
    second = client.get("/metrics", headers=AUTH_HEADER)

    assert first.headers["x-cache"] == "MISS"
    assert second.headers["x-cache"] == "HIT"


def test_metrics_is_never_cached_by_a_shared_proxy(client: TestClient) -> None:
    # Tenant data. `public` here would let a CDN serve one org's ticket counts
    # to the next caller.
    _use_tickets([Ticket(status="New")])

    cache_control = client.get("/metrics", headers=AUTH_HEADER).headers["cache-control"]

    assert cache_control.startswith("private")
    assert "public" not in cache_control


# 17. GET /demo/metrics --------------------------------------------------------


def test_demo_metrics_is_public(client: TestClient) -> None:
    assert client.get("/demo/metrics").status_code == 200


def test_demo_metrics_is_labelled_synthetic_in_the_body(client: TestClient) -> None:
    # In the body, not only a header: a screenshot keeps the body and drops the
    # headers, and a screenshot is exactly how a demo payload travels.
    payload = client.get("/demo/metrics").json()

    assert payload["synthetic"] is True
    assert "synthetic" in payload["notice"].lower()
    assert "not from any microsoft 365 tenant" in payload["notice"].lower()


def test_demo_metrics_makes_no_upstream_call(client: TestClient) -> None:
    # If it could reach Graph it would be an unauthenticated amplifier again.
    called: list[int] = []

    async def override() -> object:
        class Tripwire:
            async def list_tickets(self) -> list[Ticket]:
                called.append(1)
                return []

        return Tripwire()

    app.dependency_overrides[get_sharepoint_client] = override

    assert client.get("/demo/metrics").status_code == 200
    assert called == []


def test_demo_metrics_is_deterministic_apart_from_the_timestamp(client: TestClient) -> None:
    first = client.get("/demo/metrics").json()
    second = client.get("/demo/metrics").json()

    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second


def test_demo_metrics_works_without_configuration(clean_env: None) -> None:
    with TestClient(app) as test_client:
        response = test_client.get("/demo/metrics")

    assert response.status_code == 200
    assert response.json()["synthetic"] is True
