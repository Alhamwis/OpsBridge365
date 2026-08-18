"""Fixtures for the live tests.

These talk to the real Microsoft 365 tenant, so this file deliberately undoes
two things the offline ``tests/conftest.py`` does for speed:

* backoff is real again - a live 429 must actually be waited out, not skipped;
* nothing is stubbed. There is no fake MSAL and no respx here.

Every value comes from the environment. No credential, tenant id, or site id is
written into this repository.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest

from app.config import Settings
from app.graph import GraphClient
from app.sharepoint import SharePointClient

# Exactly the variables app.config.Settings needs. If any one is missing the
# live tests skip with a reason rather than failing - a machine with no
# credentials must still get a green suite.
REQUIRED_ENV_VARS: tuple[str, ...] = (
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "SHAREPOINT_SITE_ID",
    "ASSETS_LIST_ID",
    "TICKETS_LIST_ID",
)


def missing_env_vars() -> tuple[str, ...]:
    """Names of the required variables that are absent or blank."""
    return tuple(name for name in REQUIRED_ENV_VARS if not (os.environ.get(name) or "").strip())


#: Applied at module level in the test file. Skips - never fails - when the
#: environment is not configured.
requires_live_env = pytest.mark.skipif(
    bool(missing_env_vars()),
    reason=(
        "live Microsoft 365 credentials not configured; set "
        + ", ".join(REQUIRED_ENV_VARS)
        + " to run these (see README)"
    ),
)


@pytest.fixture(autouse=True)
def sleep_calls(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Override the offline fixture of the same name: sleep for real.

    The offline suite replaces ``app.graph._sleep`` with a no-op so retry tests
    finish instantly. Against the live tenant that would turn Graph's
    ``Retry-After`` into a hot loop, so here the delay is honoured and merely
    recorded.
    """
    recorded: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(seconds: float) -> None:
        recorded.append(seconds)
        await real_sleep(seconds)

    monkeypatch.setattr("app.graph._sleep", recording_sleep)
    return recorded


@pytest.fixture
def live_settings() -> Settings:
    """Settings built from the process environment only.

    Constructed field-by-field rather than via ``get_settings()`` so a stray
    ``.env`` on the developer's disk cannot quietly supply a value the skip
    guard above believed was missing.
    """
    absent = missing_env_vars()
    if absent:
        pytest.skip(f"missing environment variables: {', '.join(absent)}")
    return Settings(
        azure_tenant_id=os.environ["AZURE_TENANT_ID"],
        azure_client_id=os.environ["AZURE_CLIENT_ID"],
        azure_client_secret=os.environ["AZURE_CLIENT_SECRET"],
        sharepoint_site_id=os.environ["SHAREPOINT_SITE_ID"],
        assets_list_id=os.environ["ASSETS_LIST_ID"],
        tickets_list_id=os.environ["TICKETS_LIST_ID"],
    )


@pytest.fixture
async def live_graph(live_settings: Settings) -> AsyncIterator[GraphClient]:
    """A real, authenticated GraphClient. Closed after every test."""
    client = GraphClient(live_settings)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def live_sharepoint(live_graph: GraphClient) -> SharePointClient:
    """The production SharePoint wrapper, pointed at the real site."""
    return SharePointClient(live_graph)
