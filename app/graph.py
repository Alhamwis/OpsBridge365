"""Microsoft Graph client: client-credentials auth, paging, and retries.

Everything that talks to Graph goes through :func:`request_with_retry` - the
SharePoint client reuses it rather than growing its own copy.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import msal

from app.config import Settings, get_settings
from app.models import GraphDevice, GraphUser

__all__ = [
    "GRAPH_BASE_URL",
    "GraphAuthError",
    "GraphClient",
    "GraphError",
    "GraphHTTPError",
    "request_with_retry",
]

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
MAX_RETRY_AFTER_SECONDS = 60.0
MAX_PAGES = 1000
DEFAULT_TIMEOUT_SECONDS = 30.0
_ERROR_BODY_LIMIT = 500

RETRYABLE_STATUS = frozenset({429, 503})


class GraphError(Exception):
    """Base class for every Graph failure this package raises."""


class GraphAuthError(GraphError):
    """Token acquisition failed (bad credentials, missing consent, unreachable IdP)."""


class GraphHTTPError(GraphError):
    """A Graph request failed, timed out, or returned something unparseable."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


async def _sleep(seconds: float) -> None:
    """Indirection so tests can neutralise backoff without patching asyncio."""
    await asyncio.sleep(seconds)


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff: 1s, 2s, 4s ..."""
    return BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    """Honour ``Retry-After`` when the server sends a usable one."""
    raw = response.headers.get("Retry-After")
    if raw:
        try:
            seconds = float(raw.strip())
        except ValueError:
            seconds = -1.0
        if seconds >= 0:
            return min(seconds, MAX_RETRY_AFTER_SECONDS)
    return _backoff_seconds(attempt)


def _error_detail(response: httpx.Response) -> str:
    """Short, secret-free description of a failed response."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code") or ""
            message = error.get("message") or ""
            detail = f"{code}: {message}".strip(": ").strip()
            if detail:
                return detail[:_ERROR_BODY_LIMIT]
    return response.text[:_ERROR_BODY_LIMIT]


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json: Any | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> httpx.Response:
    """Perform one Graph request, retrying throttles and transport errors.

    Retries on 429/503 (honouring ``Retry-After``) and on transport/timeout
    errors, up to ``max_attempts`` total attempts with exponential backoff.

    Raises:
        GraphHTTPError: on a non-retryable 4xx/5xx, or once retries run out.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.request(method, url, headers=headers, params=params, json=json)
        except httpx.TimeoutException as exc:
            if attempt >= max_attempts:
                raise GraphHTTPError(
                    f"{method} {url} timed out after {attempt} attempt(s): {exc!r}"
                ) from exc
            logger.warning("Graph request timed out (attempt %s/%s)", attempt, max_attempts)
            await _sleep(_backoff_seconds(attempt))
            continue
        except httpx.TransportError as exc:
            if attempt >= max_attempts:
                raise GraphHTTPError(
                    f"{method} {url} failed after {attempt} attempt(s): {exc!r}"
                ) from exc
            logger.warning("Graph transport error (attempt %s/%s)", attempt, max_attempts)
            await _sleep(_backoff_seconds(attempt))
            continue

        if response.status_code in RETRYABLE_STATUS and attempt < max_attempts:
            delay = _retry_after_seconds(response, attempt)
            logger.warning(
                "Graph returned %s, retrying in %.1fs (attempt %s/%s)",
                response.status_code,
                delay,
                attempt,
                max_attempts,
            )
            await _sleep(delay)
            continue

        if response.status_code >= 400:
            raise GraphHTTPError(
                f"{method} {url} failed with HTTP {response.status_code}: "
                f"{_error_detail(response)}",
                status_code=response.status_code,
            )
        return response

    raise GraphHTTPError(f"{method} {url} exhausted {max_attempts} attempt(s)")


def parse_json_object(response: httpx.Response) -> dict[str, Any]:
    """Decode a JSON object body, turning junk into a typed error."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise GraphHTTPError(f"Malformed JSON response from {response.url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GraphHTTPError(
            f"Expected a JSON object from {response.url}, got {type(payload).__name__}"
        )
    return payload


class GraphClient:
    """Authenticated Microsoft Graph client (application permissions)."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
        self._msal_app: msal.ConfidentialClientApplication | None = None

    async def __aenter__(self) -> GraphClient:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this instance created it."""
        if self._owns_client:
            await self._client.aclose()

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def http(self) -> httpx.AsyncClient:
        return self._client

    def _app(self) -> msal.ConfidentialClientApplication:
        if self._msal_app is None:
            self._msal_app = msal.ConfidentialClientApplication(
                client_id=self._settings.azure_client_id,
                client_credential=self._settings.azure_client_secret,
                authority=self._settings.authority,
            )
        return self._msal_app

    def get_token(self) -> str:
        """Acquire an app-only access token. MSAL caches it in memory.

        Raises:
            GraphAuthError: when Entra refuses, or the response has no token.
        """
        try:
            result = self._app().acquire_token_for_client(scopes=[self._settings.graph_scope])
        except Exception as exc:  # msal raises assorted transport errors
            raise GraphAuthError(f"Token acquisition failed: {exc!r}") from exc

        if not isinstance(result, dict) or "access_token" not in result:
            payload = result if isinstance(result, dict) else {}
            error = payload.get("error", "unknown_error")
            description = payload.get("error_description", "no error_description")
            raise GraphAuthError(f"Token acquisition failed ({error}): {description}")
        return str(result["access_token"])

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_token()}", "Accept": "application/json"}

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> dict[str, Any]:
        """Single authenticated request returning a decoded JSON object."""
        response = await request_with_retry(
            self._client,
            method,
            url,
            headers=self.auth_headers(),
            params=params,
            json=json,
        )
        if response.status_code == 204 or not response.content:
            return {}
        return parse_json_object(response)

    async def get_paged(
        self, url: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """GET a Graph collection, following ``@odata.nextLink`` until exhausted."""
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        next_params = params

        for _ in range(MAX_PAGES):
            if next_url is None:
                return items
            payload = await self.request_json("GET", next_url, params=next_params)
            value = payload.get("value", [])
            if not isinstance(value, list):
                raise GraphHTTPError(f"Expected a list in 'value' from {next_url}")
            items.extend(entry for entry in value if isinstance(entry, dict))

            link = payload.get("@odata.nextLink")
            next_url = str(link) if link else None
            next_params = None  # a nextLink already carries its own query string

        raise GraphHTTPError(f"Paging {url} exceeded {MAX_PAGES} pages; refusing to loop forever")

    async def list_users(self) -> list[GraphUser]:
        """All users, paged."""
        raw = await self.get_paged(
            f"{GRAPH_BASE_URL}/users",
            params={
                "$select": "id,displayName,userPrincipalName,mail,accountEnabled",
                "$top": "999",
            },
        )
        return [GraphUser.model_validate(entry) for entry in raw]

    async def list_devices(self) -> list[GraphDevice]:
        """All registered devices, paged, with registered owners expanded."""
        raw = await self.get_paged(
            f"{GRAPH_BASE_URL}/devices",
            params={"$expand": "registeredOwners", "$top": "999"},
        )
        return [GraphDevice.from_graph(entry) for entry in raw]
