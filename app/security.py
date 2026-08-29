"""Authentication and abuse protection for the live metrics endpoint.

Threat being closed
-------------------
``/metrics`` returns real tenant data and, before this module existed, every
anonymous request minted an Entra token and read a live SharePoint list. That is
three problems at once: operational disclosure (real ticket counts to anyone),
Graph amplification (one cheap HTTP request becomes several upstream calls), and
cost (a scale-to-zero container that never gets to sleep).

Why a bearer token and not Entra
--------------------------------
Container Apps' built-in Entra authentication would be stronger - no shared
secret at all - but it validates tokens in the platform, above this process, so
none of the required behaviours (401 on a bad token, 429 under load, cache-hit
accounting) could be proven by the offline test suite. It also needs a second
app registration in the college-managed Azure tenant, and it would make the
endpoint impossible to demonstrate without an Azure CLI session.

The trade-off taken here: a single high-entropy bearer token, stored in Key
Vault and injected exactly like the Graph secret, compared in constant time.
Rotation is a Key Vault secret update plus a revision restart. This is weaker
than short-lived Entra tokens and is documented as such in docs/SECURITY.md;
the upgrade path is recorded there too.

Fail closed
-----------
If no token is configured, the endpoint refuses rather than serving data. A
misconfiguration must never silently reopen the hole this module closes.
"""

from __future__ import annotations

import hmac
import logging
from typing import Final

from fastapi import HTTPException, Request, status

from app.config import ConfigError, get_settings
from app.ratelimit import RateLimiter

__all__ = [
    "MIN_TOKEN_LENGTH",
    "client_fingerprint",
    "metrics_rate_limiter",
    "require_metrics_auth",
]

logger = logging.getLogger(__name__)

# Short enough to type, long enough that guessing is not a strategy. 32 chars of
# base64url is ~192 bits; this floor rejects a placeholder like "changeme".
MIN_TOKEN_LENGTH: Final[int] = 32

# 30 requests/minute per caller. Comfortably above any human or dashboard poll,
# far below what it takes to make Graph throttling or cold-start billing matter.
RATE_LIMIT_REQUESTS: Final[int] = 30
RATE_LIMIT_WINDOW_SECONDS: Final[float] = 60.0

metrics_rate_limiter = RateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)

_UNAUTHORIZED_HEADERS: Final[dict[str, str]] = {"WWW-Authenticate": "Bearer"}


def client_fingerprint(request: Request) -> str:
    """Stable, non-identifying key for rate limiting and audit lines.

    Container Apps terminates TLS and forwards the caller in
    ``X-Forwarded-For``; ``request.client`` would otherwise be the ingress. Only
    the left-most entry is trusted enough to bucket on, and it is never returned
    to a caller - it exists to bound one client, and to make an audit line
    correlatable without storing an address in a response.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    client = request.client
    return client.host if client else "unknown"


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, credential = header.partition(" ")
    if scheme.strip().lower() != "bearer":
        return None
    credential = credential.strip()
    return credential or None


async def require_metrics_auth(request: Request) -> None:
    """FastAPI dependency: authenticate, then rate-limit. Order matters.

    Authentication runs first so an unauthenticated flood cannot consume the
    rate-limit budget of a legitimate caller sharing an egress address.
    """
    caller = client_fingerprint(request)

    try:
        expected = get_settings().metrics_api_token
    except ConfigError:
        # The service is not configured at all. Handled by the ConfigError
        # handler in app.main, which returns a generic 503.
        raise

    if not expected:
        logger.error(
            "metrics_auth_unconfigured caller=%s outcome=refused "
            "reason=METRICS_API_TOKEN is not set; refusing to serve live data",
            caller,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service configuration is incomplete.",
        )

    presented = _bearer_token(request)
    if presented is None:
        logger.warning("metrics_auth caller=%s outcome=denied reason=missing_bearer", caller)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers=_UNAUTHORIZED_HEADERS,
        )

    if not hmac.compare_digest(presented, expected):
        logger.warning("metrics_auth caller=%s outcome=denied reason=invalid_token", caller)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers=_UNAUTHORIZED_HEADERS,
        )

    allowed, retry_after = metrics_rate_limiter.check(caller)
    if not allowed:
        logger.warning(
            "metrics_auth caller=%s outcome=throttled retry_after=%.0f", caller, retry_after
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests.",
            headers={"Retry-After": str(max(1, int(retry_after)))},
        )

    logger.info("metrics_auth caller=%s outcome=allowed", caller)
