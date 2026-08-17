"""FastAPI surface for the scale-to-zero API container.

Two endpoints, both read-only. No response ever echoes configuration or
secrets: failures are logged in full and returned as generic messages.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import __version__
from app.config import ConfigError, get_settings
from app.graph import GraphAuthError, GraphClient, GraphError
from app.metrics import get_metrics
from app.models import MetricsResponse
from app.sharepoint import SharePointClient

__all__ = ["HealthResponse", "app", "get_sharepoint_client"]

logger = logging.getLogger(__name__)

app = FastAPI(
    title="OpsBridge365 API",
    description="Live service-desk SLA metrics, backed by Microsoft Graph.",
    version=__version__,
)


class HealthResponse(BaseModel):
    """Payload for the container HEALTHCHECK."""

    status: str
    version: str


async def get_sharepoint_client() -> AsyncIterator[SharePointClient]:
    """Per-request SharePoint client; overridden in tests."""
    settings = get_settings()
    async with GraphClient(settings) as graph:
        yield SharePointClient(graph)


@app.exception_handler(ConfigError)
async def _config_error_handler(_request: Request, exc: ConfigError) -> JSONResponse:
    """Missing configuration is an operator problem - log it, never leak it."""
    logger.error("Configuration error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "Service configuration is incomplete."})


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Liveness probe. Reports the deployed version, nothing else."""
    return HealthResponse(status="ok", version=get_settings().app_version)


@app.get("/metrics", response_model=MetricsResponse)
async def metrics(
    sharepoint: SharePointClient = Depends(get_sharepoint_client),
) -> MetricsResponse:
    """Live open/at-risk/SLA numbers from the Tickets list."""
    try:
        return await get_metrics(sharepoint)
    except GraphAuthError as exc:
        logger.error("Graph authentication failed: %s", exc)
        raise HTTPException(status_code=502, detail="Upstream authentication failed.") from exc
    except GraphError as exc:
        logger.error("Graph request failed: %s", exc)
        raise HTTPException(
            status_code=502, detail="Upstream Microsoft Graph request failed."
        ) from exc
