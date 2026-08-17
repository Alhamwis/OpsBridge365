"""Run-and-exit sync job: Microsoft Graph devices -> SharePoint Assets list.

Run with ``python -m app.sync``. Azure Container Apps Jobs owns the schedule;
this process starts, syncs, prints a JSON :class:`SyncResult`, and exits.

Honesty rules (deliberate, and stated in the README):

* A device is matched to an Assets row by serial number first, then by device
  name. A key that maps to more than one row is ambiguous, so it matches
  nothing - a wrong match is worse than no match.
* When the assigned user or compliance state cannot be determined, the literal
  string ``"Unknown"`` is written. Nothing is guessed or approximated.
* ``LastCheckIn`` is a date/time column, so an unknown check-in is left
  untouched rather than stamped with a made-up time; the count of those shows
  up in the summary as ``unknown_last_check_in``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import ConfigError, get_settings
from app.graph import GraphClient, GraphError
from app.models import UNKNOWN, AssetItem, GraphDevice, GraphUser, SyncResult
from app.sharepoint import SharePointClient

__all__ = ["AssetIndex", "build_asset_index", "main", "resolve_fields", "run_sync", "sync_assets"]

logger = logging.getLogger(__name__)

COMPLIANT = "Compliant"
NON_COMPLIANT = "NonCompliant"


def _normalise(value: str | None) -> str | None:
    """Casefold and trim a match key; empty/blank values match nothing."""
    if not value:
        return None
    cleaned = value.strip()
    return cleaned.casefold() if cleaned else None


@dataclass(slots=True)
class AssetIndex:
    """Lookup tables from Assets rows, with ambiguous keys removed."""

    by_serial: dict[str, AssetItem] = field(default_factory=dict)
    by_name: dict[str, AssetItem] = field(default_factory=dict)
    ambiguous_serials: set[str] = field(default_factory=set)
    ambiguous_names: set[str] = field(default_factory=set)

    def lookup(self, device: GraphDevice) -> AssetItem | None:
        """Serial number first, then device name. Ambiguity yields no match."""
        serial = _normalise(device.serial_number)
        if serial and serial not in self.ambiguous_serials:
            asset = self.by_serial.get(serial)
            if asset is not None:
                return asset
        name = _normalise(device.display_name)
        if name and name not in self.ambiguous_names:
            return self.by_name.get(name)
        return None


def _add_key(
    table: dict[str, AssetItem], ambiguous: set[str], key: str | None, asset: AssetItem
) -> None:
    if key is None:
        return
    existing = table.get(key)
    if existing is not None and existing.id != asset.id:
        ambiguous.add(key)
        table.pop(key, None)
        return
    if key in ambiguous:
        return
    table[key] = asset


def build_asset_index(assets: list[AssetItem]) -> AssetIndex:
    """Index Assets rows by serial number and device name."""
    index = AssetIndex()
    for asset in assets:
        _add_key(index.by_serial, index.ambiguous_serials, _normalise(asset.serial_number), asset)
        _add_key(index.by_name, index.ambiguous_names, _normalise(asset.device_name), asset)
    if index.ambiguous_serials or index.ambiguous_names:
        logger.warning(
            "Ignoring ambiguous match keys: %s serial(s), %s name(s)",
            len(index.ambiguous_serials),
            len(index.ambiguous_names),
        )
    return index


def _format_timestamp(value: datetime) -> str:
    """ISO 8601 in UTC with a trailing Z, which is what SharePoint expects."""
    stamped = value if value.tzinfo else value.replace(tzinfo=UTC)
    return stamped.astimezone(UTC).isoformat().replace("+00:00", "Z")


def device_label(device: GraphDevice) -> str:
    """Human-readable device identity for logs and the summary."""
    return device.display_name or device.serial_number or device.device_id or device.id or UNKNOWN


def resolve_fields(device: GraphDevice, users_by_key: dict[str, GraphUser]) -> dict[str, Any]:
    """Build the SharePoint field bag for a matched device.

    Unresolvable text values become the literal ``"Unknown"``; an unresolvable
    ``LastCheckIn`` is omitted rather than invented.
    """
    fields: dict[str, Any] = {}

    owner_key = _normalise(device.registered_owner_upn)
    user = users_by_key.get(owner_key) if owner_key else None
    fields["AssignedUser"] = user.label if user is not None else UNKNOWN

    if device.is_compliant is True:
        fields["ComplianceStatus"] = COMPLIANT
    elif device.is_compliant is False:
        fields["ComplianceStatus"] = NON_COMPLIANT
    else:
        fields["ComplianceStatus"] = UNKNOWN

    if device.approximate_last_sign_in is not None:
        fields["LastCheckIn"] = _format_timestamp(device.approximate_last_sign_in)

    return fields


def index_users(users: list[GraphUser]) -> dict[str, GraphUser]:
    """Users keyed by UPN and by object id, both normalised."""
    table: dict[str, GraphUser] = {}
    for user in users:
        for key in (_normalise(user.user_principal_name), _normalise(user.id)):
            if key:
                table.setdefault(key, user)
    return table


async def sync_assets(sharepoint: SharePointClient) -> SyncResult:
    """Fetch users, devices, and assets, then PATCH every confident match."""
    graph = sharepoint.graph
    result = SyncResult()

    users = await graph.list_users()
    devices = await graph.list_devices()
    assets = await sharepoint.list_assets()

    result.users_fetched = len(users)
    result.devices_fetched = len(devices)
    result.assets_fetched = len(assets)

    users_by_key = index_users(users)
    index = build_asset_index(assets)

    for device in devices:
        asset = index.lookup(device)
        if asset is None:
            result.unmatched_devices.append(device_label(device))
            logger.info("No confident Assets match for device %s", device_label(device))
            continue

        result.matched += 1
        fields = resolve_fields(device, users_by_key)
        if fields.get("AssignedUser") == UNKNOWN:
            result.unknown_assigned_user += 1
        if fields.get("ComplianceStatus") == UNKNOWN:
            result.unknown_compliance += 1
        if "LastCheckIn" not in fields:
            result.unknown_last_check_in += 1

        try:
            await sharepoint.patch_asset(asset.id, fields)
        except GraphError as exc:
            message = f"PATCH failed for asset {asset.id} ({device_label(device)}): {exc}"
            logger.error(message)
            result.errors.append(message)
            continue
        result.patched += 1

    return result


async def run_sync(sharepoint: SharePointClient | None = None) -> SyncResult:
    """Entry point used by :func:`main` and by tests (inject a client to mock)."""
    if sharepoint is not None:
        return await sync_assets(sharepoint)

    settings = get_settings()
    async with GraphClient(settings) as graph:
        return await sync_assets(SharePointClient(graph))


def main() -> int:
    """Run once, print the summary as JSON, and return a process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        result = asyncio.run(run_sync())
    except ConfigError as exc:
        print(json.dumps({"status": "config_error", "detail": str(exc)}), file=sys.stderr)
        return 2
    except GraphError as exc:
        print(json.dumps({"status": "graph_error", "detail": str(exc)}), file=sys.stderr)
        return 1

    print(result.model_dump_json(indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
