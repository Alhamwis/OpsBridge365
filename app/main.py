"""FastAPI surface for the scale-to-zero API container.

Three endpoints with three different exposures, and the difference is the point:

    GET /healthz       public, no configuration required, no upstream call
    GET /demo/metrics  public, synthetic data, no upstream call
    GET /metrics       authenticated, rate limited, cached; live tenant data

No response ever echoes configuration or secrets: failures are logged in full
and returned as generic messages.

Why /metrics is not public any more
-----------------------------------
It used to be. Every anonymous request built a new MSAL application, acquired a
token, and read a live SharePoint list - so one HTTP request was several
upstream calls, real ticket counts were world-readable, and a loop could both
throttle Graph and hold a scale-to-zero container permanently awake. The fix is
layered: authenticate, then rate limit, then serve from a short-lived cache that
collapses concurrent misses into a single upstream fetch, all under one
wall-clock deadline.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import __version__
from app.cache import SingleFlightCache
from app.config import ConfigError, get_settings
from app.demo import DemoMetricsResponse, demo_metrics
from app.graph import GraphAuthError, GraphClient, GraphError
from app.metrics import get_metrics
from app.models import MetricsResponse
from app.security import require_metrics_auth
from app.sharepoint import SharePointClient

__all__ = [
    "HealthResponse",
    "app",
    "get_sharepoint_client",
    "metrics_cache",
]

logger = logging.getLogger(__name__)

# Long enough that a burst of callers costs one Graph read, short enough that a
# ticket resolved now shows up in under a minute. Also the knob that decides how
# often a scale-to-zero container is woken by polling.
METRICS_CACHE_TTL_SECONDS: Final[float] = 45.0

# Hard ceiling on one live refresh. app.graph bounds each individual request
# (30s, 3 attempts) but not the aggregate: a deeply paged list could otherwise
# run for minutes while the caller's connection is held open.
METRICS_DEADLINE_SECONDS: Final[float] = 25.0

metrics_cache: SingleFlightCache[MetricsResponse] = SingleFlightCache(METRICS_CACHE_TTL_SECONDS)

# One shared Graph client for the process. Rebuilt per request it meant a fresh
# MSAL ConfidentialClientApplication - and therefore tenant discovery and a
# token mint - on every single call, with an empty token cache each time.
_graph_client: GraphClient | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Build the shared Graph client on startup, close it on shutdown.

    Deliberately tolerant of missing configuration: /healthz and /demo/metrics
    must answer on an unconfigured container, and the orchestrator kills a
    container whose health probe fails.
    """
    global _graph_client
    try:
        _graph_client = GraphClient(get_settings())
    except ConfigError as exc:
        logger.warning("Starting without a Graph client: %s", exc)
        _graph_client = None
    try:
        yield
    finally:
        if _graph_client is not None:
            await _graph_client.aclose()
            _graph_client = None


app = FastAPI(
    title="OpsBridge365 API",
    description=(
        "Service-desk SLA metrics backed by Microsoft Graph. "
        "/healthz and /demo/metrics are public; /metrics requires a bearer token."
    ),
    version=__version__,
    lifespan=lifespan,
)


class HealthResponse(BaseModel):
    """Payload for the container HEALTHCHECK."""

    status: str
    version: str


async def get_sharepoint_client() -> AsyncIterator[SharePointClient]:
    """Yield a SharePoint client over the process-wide Graph client.

    Overridden in tests. When the app was started without configuration this
    raises ConfigError, which the handler below turns into a generic 503.
    """
    global _graph_client
    if _graph_client is None:
        _graph_client = GraphClient(get_settings())
    yield SharePointClient(_graph_client)


@app.exception_handler(ConfigError)
async def _config_error_handler(_request: Request, exc: ConfigError) -> JSONResponse:
    """Missing configuration is an operator problem - log it, never leak it."""
    logger.error("Configuration error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "Service configuration is incomplete."})


@app.get("/healthz", response_model=HealthResponse)
async def healthz(response: Response) -> HealthResponse:
    """Liveness probe. Reports the deployed version, nothing else.

    Deliberately independent of configuration: a container health probe must
    answer before any Azure credential is mounted, otherwise the orchestrator
    kills a container that is merely unconfigured. Missing credentials surface
    on /metrics, which is the endpoint that actually needs them.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        version = get_settings().app_version
    except ConfigError as exc:
        logger.warning("Serving /healthz without configuration: %s", exc)
        version = __version__
    return HealthResponse(status="ok", version=version)


@app.get("/demo/metrics", response_model=DemoMetricsResponse)
async def demo_metrics_endpoint(response: Response) -> DemoMetricsResponse:
    """Public, synthetic, and labelled as such in the body.

    Exists so the API can be demonstrated without handing out a token and
    without disclosing a real ticket count. It performs no upstream call, so it
    cannot be used to amplify anything.
    """
    response.headers["Cache-Control"] = "public, max-age=60"
    response.headers["X-Synthetic-Data"] = "true"
    return demo_metrics()


@app.get(
    "/metrics",
    response_model=MetricsResponse,
    dependencies=[Depends(require_metrics_auth)],
    responses={
        401: {"description": "Missing or invalid bearer token."},
        429: {"description": "Rate limit exceeded."},
        502: {"description": "Upstream Microsoft Graph or SharePoint failure."},
        503: {"description": "Service configuration is incomplete."},
        504: {"description": "Upstream did not answer within the deadline."},
    },
)
async def metrics(
    response: Response,
    sharepoint: SharePointClient = Depends(get_sharepoint_client),
) -> MetricsResponse:
    """Live open/at-risk/SLA numbers from the Tickets list.

    Served from a short-lived cache. Concurrent misses are collapsed into one
    upstream fetch, and the whole refresh is bounded by a wall-clock deadline.
    """

    async def refresh() -> MetricsResponse:
        async with asyncio.timeout(METRICS_DEADLINE_SECONDS):
            # get_token() is synchronous MSAL I/O. Off the event loop it cannot
            # stall every other request on this single-replica container.
            return await get_metrics(sharepoint)

    try:
        payload, from_cache = await metrics_cache.get(refresh)
    except TimeoutError as exc:
        logger.error("Metrics refresh exceeded %.0fs deadline", METRICS_DEADLINE_SECONDS)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Upstream request timed out.",
        ) from exc
    except GraphAuthError as exc:
        logger.error("Graph authentication failed: %s", exc)
        raise HTTPException(status_code=502, detail="Upstream authentication failed.") from exc
    except GraphError as exc:
        logger.error("Graph request failed: %s", exc)
        raise HTTPException(
            status_code=502, detail="Upstream Microsoft Graph request failed."
        ) from exc

    age = metrics_cache.age_seconds() or 0.0
    remaining = max(0, int(METRICS_CACHE_TTL_SECONDS - age))
    # private: this is tenant data, so no shared proxy may store it.
    response.headers["Cache-Control"] = f"private, max-age={remaining}"
    response.headers["X-Cache"] = "HIT" if from_cache else "MISS"
    return payload
