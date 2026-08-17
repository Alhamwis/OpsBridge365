"""FastAPI endpoints: /healthz, /metrics, and the no-secrets-in-responses rule."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.graph import GraphAuthError, GraphHTTPError
from app.main import app, get_sharepoint_client
from app.models import Ticket
from tests.conftest import ENV_VARS
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
        response = test_client.get("/metrics")

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

    response = client.get("/metrics")
    payload = response.json()

    assert response.status_code == 200
    assert payload["open_tickets"] == 2
    assert payload["due_within_30min"] == 1
    assert payload["sla_compliance_7d_pct"] == 100.0
    assert payload["resolved_last_7d"] == 1
    assert "generated_at" in payload


def test_metrics_with_no_tickets_reports_null_compliance(client: TestClient) -> None:
    _use_tickets([])

    payload = client.get("/metrics").json()

    assert payload["open_tickets"] == 0
    assert payload["sla_compliance_7d_pct"] is None


def test_metrics_never_echoes_configuration_or_secrets(client: TestClient) -> None:
    _use_tickets([Ticket(status="New")])

    body = client.get("/metrics").text

    for value in ENV_VARS.values():
        assert value not in body


def test_metrics_returns_502_when_graph_fails(client: TestClient) -> None:
    async def override() -> object:
        class Broken:
            async def list_tickets(self) -> list[Ticket]:
                raise GraphHTTPError("upstream exploded", status_code=500)

        return Broken()

    app.dependency_overrides[get_sharepoint_client] = override

    response = client.get("/metrics")

    assert response.status_code == 502
    assert "upstream exploded" not in response.text  # internals stay in the logs


def test_metrics_returns_502_when_authentication_fails(client: TestClient) -> None:
    async def override() -> object:
        class Broken:
            async def list_tickets(self) -> list[Ticket]:
                raise GraphAuthError("invalid_client: secret rejected")

        return Broken()

    app.dependency_overrides[get_sharepoint_client] = override

    response = client.get("/metrics")

    assert response.status_code == 502
    assert "secret" not in response.text.lower()
