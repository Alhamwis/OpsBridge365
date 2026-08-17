"""In-memory stand-ins used by the sync and API tests."""

from __future__ import annotations

from typing import Any

from app.graph import GraphHTTPError
from app.models import AssetItem, GraphDevice, GraphUser, Ticket

__all__ = ["FakeGraph", "FakeSharePoint"]


class FakeGraph:
    """Minimal stand-in for :class:`app.graph.GraphClient`."""

    def __init__(self, users: list[GraphUser], devices: list[GraphDevice]) -> None:
        self._users = users
        self._devices = devices

    async def list_users(self) -> list[GraphUser]:
        return list(self._users)

    async def list_devices(self) -> list[GraphDevice]:
        return list(self._devices)


class FakeSharePoint:
    """Records PATCH calls instead of sending them."""

    def __init__(
        self,
        graph: FakeGraph | None = None,
        assets: list[AssetItem] | None = None,
        tickets: list[Ticket] | None = None,
    ) -> None:
        self.graph = graph or FakeGraph([], [])
        self._assets = assets or []
        self._tickets = tickets or []
        self.patches: list[tuple[str, dict[str, Any]]] = []
        self.fail_item_ids: set[str] = set()

    async def list_assets(self) -> list[AssetItem]:
        return list(self._assets)

    async def list_tickets(self) -> list[Ticket]:
        return list(self._tickets)

    async def patch_asset(self, item_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        if item_id in self.fail_item_ids:
            raise GraphHTTPError(f"PATCH rejected for item {item_id}", status_code=400)
        self.patches.append((item_id, dict(fields)))
        return {"id": item_id, **fields}
