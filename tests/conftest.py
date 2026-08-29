"""Shared fixtures.

Every test here runs offline: httpx is intercepted by respx and MSAL is
replaced by a stub, so no token request and no Graph call ever leaves the
machine. Nothing in this file is a real credential.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx
import pytest

from app import graph as graph_module
from app.config import Settings
from app.graph import GraphClient

ENV_VARS: dict[str, str] = {
    "AZURE_TENANT_ID": "00000000-0000-0000-0000-000000000001",
    "AZURE_CLIENT_ID": "00000000-0000-0000-0000-000000000002",
    "AZURE_CLIENT_SECRET": "unit-test-value-not-a-credential",
    "SHAREPOINT_SITE_ID": "contoso.sharepoint.com,site-guid,web-guid",
    "ASSETS_LIST_ID": "assets-list-guid",
    "TICKETS_LIST_ID": "tickets-list-guid",
    "METRICS_API_TOKEN": "unit-test-metrics-token-not-a-credential-000",
    "APP_VERSION": "9.9.9-test",
}

TEST_TOKEN = "test-token-not-a-real-jwt"

#: Authorization header that satisfies ``require_metrics_auth`` under ``configured_env``.
AUTH_HEADER: dict[str, str] = {"Authorization": f"Bearer {ENV_VARS['METRICS_API_TOKEN']}"}


class FakeMsalApp:
    """Stand-in for ``msal.ConfidentialClientApplication``."""

    result: dict[str, Any] = {"access_token": TEST_TOKEN, "expires_in": 3600}
    raises: BaseException | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls: list[list[str]] = []

    def acquire_token_for_client(self, scopes: list[str]) -> dict[str, Any]:
        self.calls.append(scopes)
        if self.raises is not None:
            raise self.raises
        return dict(self.result)


@pytest.fixture
def settings() -> Settings:
    """Settings built directly - never touches the environment or a .env file."""
    return Settings(
        azure_tenant_id=ENV_VARS["AZURE_TENANT_ID"],
        azure_client_id=ENV_VARS["AZURE_CLIENT_ID"],
        azure_client_secret=ENV_VARS["AZURE_CLIENT_SECRET"],
        sharepoint_site_id=ENV_VARS["SHAREPOINT_SITE_ID"],
        assets_list_id=ENV_VARS["ASSETS_LIST_ID"],
        tickets_list_id=ENV_VARS["TICKETS_LIST_ID"],
    )


@pytest.fixture(autouse=True)
def reset_api_state() -> Iterator[None]:
    """Clear the process-wide metrics cache and rate limiter around every test.

    Both are module-level singletons because the container runs one replica and
    computes one value. That is right in production and poisonous in a test
    suite: without this fixture a cached payload or a spent rate-limit budget
    leaks from one test into the next, and the failure looks like a bug in the
    endpoint rather than in the harness.
    """
    from app.main import metrics_cache
    from app.security import metrics_rate_limiter

    metrics_cache.invalidate()
    metrics_rate_limiter.reset()
    yield
    metrics_cache.invalidate()
    metrics_rate_limiter.reset()


@pytest.fixture(autouse=True)
def sleep_calls(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Neutralise backoff and record the delays that were requested."""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(graph_module, "_sleep", fake_sleep)
    return recorded


@pytest.fixture
def msal_factory(monkeypatch: pytest.MonkeyPatch) -> Callable[..., type[FakeMsalApp]]:
    """Install a stub MSAL app; the factory lets a test choose the token result."""

    def install(
        result: dict[str, Any] | None = None, raises: BaseException | None = None
    ) -> type[FakeMsalApp]:
        stub = type(
            "InstalledFakeMsalApp",
            (FakeMsalApp,),
            {
                "result": result if result is not None else dict(FakeMsalApp.result),
                "raises": raises,
            },
        )
        monkeypatch.setattr(graph_module.msal, "ConfidentialClientApplication", stub)
        return stub

    return install


@pytest.fixture
def stub_msal(msal_factory: Callable[..., type[FakeMsalApp]]) -> type[FakeMsalApp]:
    """A stub MSAL that always hands back a token."""
    return msal_factory()


@pytest.fixture
async def graph_client(
    settings: Settings, stub_msal: type[FakeMsalApp]
) -> AsyncIterator[GraphClient]:
    """GraphClient wired to an httpx client that respx will intercept."""
    async with httpx.AsyncClient() as http_client:
        yield GraphClient(settings, client=http_client)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[None]:
    """No OpsBridge env vars and no .env on disk."""
    from app.config import get_settings

    monkeypatch.chdir(tmp_path)
    for name in (*ENV_VARS, "GRAPH_SCOPE"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def configured_env(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Full, valid configuration in the environment."""
    from app.config import get_settings

    for name, value in ENV_VARS.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
