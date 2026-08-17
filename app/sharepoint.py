"""SharePoint access through the Graph lists API.

Thin wrapper over :class:`app.graph.GraphClient` so auth, paging, and retry
behaviour are defined in exactly one place.
"""

from __future__ import annotations

import logging
from typing import Any

from app.graph import GRAPH_BASE_URL, GraphClient
from app.models import AssetItem, Ticket

__all__ = ["SharePointClient"]

logger = logging.getLogger(__name__)


class SharePointClient:
    """Read and update SharePoint list items for one site."""

    def __init__(self, graph: GraphClient) -> None:
        self._graph = graph
        self._settings = graph.settings

    @property
    def graph(self) -> GraphClient:
        return self._graph

    def _list_url(self, list_id: str) -> str:
        site_id = self._settings.sharepoint_site_id
        return f"{GRAPH_BASE_URL}/sites/{site_id}/lists/{list_id}"

    async def _list_items(self, list_id: str) -> list[dict[str, Any]]:
        """Every item of a list with its ``fields`` expanded (paged)."""
        return await self._graph.get_paged(
            f"{self._list_url(list_id)}/items",
            params={"expand": "fields", "$top": "999"},
        )

    async def list_assets(self) -> list[AssetItem]:
        """All rows of the Assets list."""
        items = await self._list_items(self._settings.assets_list_id)
        return [AssetItem.from_list_item(item) for item in items]

    async def list_tickets(self) -> list[Ticket]:
        """All rows of the Tickets list (source for ``/metrics``)."""
        items = await self._list_items(self._settings.tickets_list_id)
        return [Ticket.from_list_item(item) for item in items]

    async def patch_asset(self, item_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """PATCH one Assets row's field values.

        The payload is the field bag itself - Graph's ``/items/{id}/fields``
        endpoint takes ``{"LastCheckIn": ..., "AssignedUser": ...}`` directly.
        """
        url = f"{self._list_url(self._settings.assets_list_id)}/items/{item_id}/fields"
        logger.debug("PATCH asset %s fields=%s", item_id, sorted(fields))
        return await self._graph.request_json("PATCH", url, json=fields)
