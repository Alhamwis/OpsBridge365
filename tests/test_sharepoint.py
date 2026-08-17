"""SharePointClient: list reads and the exact shape of a PATCH."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.config import Settings
from app.graph import GRAPH_BASE_URL, GraphClient, GraphHTTPError
from app.sharepoint import SharePointClient


def _items_url(settings: Settings, list_id: str) -> str:
    return f"{GRAPH_BASE_URL}/sites/{settings.sharepoint_site_id}/lists/{list_id}/items"


@pytest.fixture
def sharepoint(graph_client: GraphClient) -> SharePointClient:
    return SharePointClient(graph_client)


async def test_list_assets_parses_expanded_fields(
    sharepoint: SharePointClient, settings: Settings
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(_items_url(settings, settings.assets_list_id)).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "12",
                            "fields": {
                                "Title": "LAP-0001",
                                "SerialNumber": "SN-12345",
                                "DeviceName": "LAP-0001",
                                "AssignedUser": "Unknown",
                                "ComplianceStatus": "Unknown",
                                "LastCheckIn": "2026-08-01T00:00:00Z",
                            },
                        },
                        {"id": "13", "fields": {"Title": "PRN-0002"}},
                    ]
                },
            )
        )
        assets = await sharepoint.list_assets()

    assert [asset.id for asset in assets] == ["12", "13"]
    assert assets[0].serial_number == "SN-12345"
    assert assets[0].compliance_status == "Unknown"
    assert assets[1].serial_number is None
    assert assets[1].last_check_in is None


# 6. PATCH payload shape -------------------------------------------------------


async def test_patch_asset_sends_field_bag_to_the_fields_endpoint(
    sharepoint: SharePointClient, settings: Settings
) -> None:
    fields = {
        "LastCheckIn": "2026-08-16T09:30:00Z",
        "AssignedUser": "Ada Lovelace",
        "ComplianceStatus": "Compliant",
    }
    expected_url = f"{_items_url(settings, settings.assets_list_id)}/12/fields"

    with respx.mock(assert_all_called=True) as mock:
        route = mock.patch(expected_url).mock(
            return_value=httpx.Response(200, json={"id": "12", **fields})
        )
        result = await sharepoint.patch_asset("12", fields)

    request = route.calls[0].request
    assert request.method == "PATCH"
    assert str(request.url) == expected_url
    assert json.loads(request.content) == fields  # the field bag itself, not wrapped
    assert request.headers["Authorization"].startswith("Bearer ")
    assert result["ComplianceStatus"] == "Compliant"


async def test_patch_asset_surfaces_http_errors(
    sharepoint: SharePointClient, settings: Settings
) -> None:
    url = f"{_items_url(settings, settings.assets_list_id)}/99/fields"

    with respx.mock(assert_all_called=True) as mock:
        mock.patch(url).mock(
            return_value=httpx.Response(
                400, json={"error": {"code": "invalidRequest", "message": "bad column"}}
            )
        )
        with pytest.raises(GraphHTTPError) as excinfo:
            await sharepoint.patch_asset("99", {"ComplianceStatus": "Compliant"})

    assert excinfo.value.status_code == 400


async def test_list_tickets_reads_the_tickets_list(
    sharepoint: SharePointClient, settings: Settings
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(_items_url(settings, settings.tickets_list_id)).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "1",
                            "fields": {
                                "TicketID": "TCK-0001",
                                "Status": "Resolved",
                                "SLAResolutionDue": "2026-08-15T12:00:00Z",
                                "ResolvedDate": "2026-08-15T11:00:00Z",
                            },
                        }
                    ]
                },
            )
        )
        tickets = await sharepoint.list_tickets()

    assert tickets[0].ticket_id == "TCK-0001"
    assert tickets[0].status == "Resolved"
    assert settings.tickets_list_id in str(route.calls[0].request.url)
