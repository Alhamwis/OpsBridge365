"""Configuration validation - and the rule that importing never explodes."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import ConfigError, Settings, get_settings

REQUIRED = (
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "SHAREPOINT_SITE_ID",
    "ASSETS_LIST_ID",
    "TICKETS_LIST_ID",
)


def test_missing_config_names_every_missing_variable(clean_env: None) -> None:
    with pytest.raises(ConfigError) as excinfo:
        get_settings()

    message = str(excinfo.value)
    for name in REQUIRED:
        assert name in message, f"{name} not named in: {message}"
    assert set(excinfo.value.missing) == set(REQUIRED)


def test_partial_config_names_only_what_is_missing(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant")
    monkeypatch.setenv("AZURE_CLIENT_ID", "client")
    get_settings.cache_clear()

    with pytest.raises(ConfigError) as excinfo:
        get_settings()

    assert "AZURE_TENANT_ID" not in excinfo.value.missing
    assert "AZURE_CLIENT_SECRET" in excinfo.value.missing
    assert "TICKETS_LIST_ID" in excinfo.value.missing


def test_valid_config_is_cached_and_defaults_applied(configured_env: None) -> None:
    settings = get_settings()

    assert settings.graph_scope == "https://graph.microsoft.com/.default"
    assert settings.app_version == "9.9.9-test"
    assert settings.authority.endswith(settings.azure_tenant_id)
    assert get_settings() is settings  # lru_cache


def test_every_module_imports_without_env(clean_env: None) -> None:
    """Import-time must stay side-effect free; validation is call-time only."""
    for name in (
        "app.config",
        "app.models",
        "app.graph",
        "app.sharepoint",
        "app.sync",
        "app.metrics",
        "app.main",
    ):
        assert importlib.import_module(name) is not None


def test_import_in_a_pristine_interpreter_with_no_env() -> None:
    """The real guarantee: a fresh process with no OpsBridge variables at all."""
    completed = subprocess.run(
        [sys.executable, "-c", "import app.main, app.sync, app.metrics; print('ok')"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
        env={
            key: value
            for key, value in os.environ.items()
            if key not in {*REQUIRED, "GRAPH_SCOPE", "APP_VERSION"}
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert "ok" in completed.stdout


def test_settings_accept_explicit_values_without_environment(settings: Settings) -> None:
    assert settings.assets_list_id == "assets-list-guid"
