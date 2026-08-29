"""Environment-driven configuration.

Importing this module - or any module in :mod:`app` - must never raise, even
with a completely empty environment. Validation happens the first time
:func:`get_settings` is called, so tests and ``--help`` style imports work on a
machine with no Azure credentials at all.

A note on the ``AZURE_`` prefix, because it reads as the wrong thing: every
``AZURE_*`` variable here describes the **Microsoft 365 tenant** the app calls
Graph in, not the Azure tenant that owns the subscription the container runs
on. Those are two different directories in this deployment. The names are kept
because they are the conventional ones for a client-credentials app; the Bicep
template feeds ``AZURE_TENANT_ID`` from its ``graphTenantId`` parameter for
exactly this reason.
"""

from __future__ import annotations

import functools

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["ConfigError", "Settings", "get_settings"]


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unusable."""

    def __init__(self, message: str, missing: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.missing: tuple[str, ...] = missing


class Settings(BaseSettings):
    """Runtime settings. Field names map to upper-case environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # AZURE_TENANT_ID: the Microsoft 365 (Graph) tenant the app registration
    # lives in - NOT the Azure tenant hosting the subscription. See the module
    # docstring.
    azure_tenant_id: str
    azure_client_id: str
    azure_client_secret: str
    sharepoint_site_id: str
    assets_list_id: str
    tickets_list_id: str

    # Bearer token guarding GET /metrics. Optional because the sync job shares
    # this Settings class and has no HTTP surface; the API refuses to serve live
    # data when it is unset, so "optional" never means "open".
    metrics_api_token: str | None = None

    graph_scope: str = "https://graph.microsoft.com/.default"
    app_version: str = "0.1.0"

    @property
    def authority(self) -> str:
        """MSAL authority URL for the client-credentials flow.

        Built from the Microsoft 365 tenant id; pointing it at the tenant that
        owns the Azure subscription makes every token request fail.
        """
        return f"https://login.microsoftonline.com/{self.azure_tenant_id}"


def _missing_variables(error: ValidationError) -> tuple[str, ...]:
    """Extract the environment variable names a ValidationError complains about."""
    names: list[str] = []
    for detail in error.errors():
        location = detail.get("loc") or ()
        if not location:
            continue
        name = str(location[0]).upper()
        if name not in names:
            names.append(name)
    return tuple(names)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build (and cache) :class:`Settings`.

    Raises:
        ConfigError: with every missing/invalid variable named in the message.
    """
    try:
        return Settings()  # type: ignore[call-arg]  # values come from the environment
    except ValidationError as exc:
        missing = _missing_variables(exc)
        listed = ", ".join(missing) if missing else "unknown"
        raise ConfigError(
            f"Missing or invalid configuration. Set these environment variables: {listed}. "
            "See .env.example for the full list.",
            missing=missing,
        ) from exc
