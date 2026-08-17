"""GraphClient: parsing, paging, auth failures, retries, timeouts, junk JSON.

respx intercepts every request, so nothing here touches the network.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.graph import GRAPH_BASE_URL, GraphAuthError, GraphClient, GraphHTTPError
from app.models import GraphDevice, GraphUser

USERS_URL = f"{GRAPH_BASE_URL}/users"
DEVICES_URL = f"{GRAPH_BASE_URL}/devices"


def _users_page(users: list[dict], next_link: str | None = None) -> dict:
    payload: dict = {"value": users}
    if next_link:
        payload["@odata.nextLink"] = next_link
    return payload


# 1. Graph user parsing --------------------------------------------------------


async def test_list_users_parses_graph_payload(graph_client: GraphClient) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(USERS_URL).mock(
            return_value=httpx.Response(
                200,
                json=_users_page(
                    [
                        {
                            "id": "user-1",
                            "displayName": "Ada Lovelace",
                            "userPrincipalName": "ada@contoso.example",
                            "mail": "ada@contoso.example",
                            "accountEnabled": True,
                            "unexpectedProperty": "ignored",
                        },
                        {"id": "user-2"},
                    ]
                ),
            )
        )
        users = await graph_client.list_users()

    assert [user.id for user in users] == ["user-1", "user-2"]
    assert users[0].display_name == "Ada Lovelace"
    assert users[0].user_principal_name == "ada@contoso.example"
    assert users[0].account_enabled is True
    assert users[0].label == "Ada Lovelace"
    # A user with no name at all is labelled honestly, not blank-guessed.
    assert users[1].label == "Unknown"


def test_graph_user_model_tolerates_missing_optional_fields() -> None:
    user = GraphUser.model_validate({"id": "user-9", "userPrincipalName": "bob@contoso.example"})

    assert user.display_name is None
    assert user.label == "bob@contoso.example"


# 2. Graph device parsing ------------------------------------------------------


async def test_list_devices_parses_graph_payload(graph_client: GraphClient) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(DEVICES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "device-1",
                            "deviceId": "dev-guid-1",
                            "displayName": "LAP-0001",
                            "operatingSystem": "Windows",
                            "operatingSystemVersion": "10.0.26200",
                            "isCompliant": True,
                            "approximateLastSignInDateTime": "2026-08-16T09:30:00Z",
                            "physicalIds": ["[ZTDID]:abc", "[SerialNumber]:SN-12345"],
                            "registeredOwners": [
                                {"id": "user-1", "userPrincipalName": "ada@contoso.example"}
                            ],
                        }
                    ]
                },
            )
        )
        devices = await graph_client.list_devices()

    device = devices[0]
    assert device.id == "device-1"
    assert device.display_name == "LAP-0001"
    assert device.is_compliant is True
    assert device.serial_number == "SN-12345"  # recovered from physicalIds
    assert device.registered_owner_upn == "ada@contoso.example"
    assert device.approximate_last_sign_in == datetime(2026, 8, 16, 9, 30, tzinfo=UTC)


def test_device_parsing_prefers_explicit_serial_number_property() -> None:
    device = GraphDevice.from_graph(
        {
            "id": "device-2",
            "serialNumber": "SN-EXPLICIT",
            "physicalIds": ["[SerialNumber]:SN-FROM-PHYSICAL"],
        }
    )

    assert device.serial_number == "SN-EXPLICIT"
    assert device.is_compliant is None
    assert device.registered_owner_upn is None


# 3. @odata.nextLink paging ----------------------------------------------------


async def test_paging_follows_every_next_link(graph_client: GraphClient) -> None:
    page_two = f"{USERS_URL}?$skiptoken=page2"
    page_three = f"{USERS_URL}?$skiptoken=page3"

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(USERS_URL).mock(
            side_effect=[
                httpx.Response(200, json=_users_page([{"id": "user-1"}], page_two)),
                httpx.Response(200, json=_users_page([{"id": "user-2"}], page_three)),
                httpx.Response(200, json=_users_page([{"id": "user-3"}])),
            ]
        )
        users = await graph_client.list_users()

    assert [user.id for user in users] == ["user-1", "user-2", "user-3"]
    assert route.call_count == 3
    assert str(route.calls[1].request.url) == page_two
    assert str(route.calls[2].request.url) == page_three


# 7. Auth error path -----------------------------------------------------------


async def test_token_failure_raises_graph_auth_error(
    settings, msal_factory: Callable[..., type]
) -> None:
    msal_factory(
        result={
            "error": "invalid_client",
            "error_description": "AADSTS7000215: Invalid client secret provided.",
        }
    )

    async with httpx.AsyncClient() as http_client:
        client = GraphClient(settings, client=http_client)
        with respx.mock(assert_all_called=False) as mock:
            route = mock.get(USERS_URL).mock(return_value=httpx.Response(200, json={"value": []}))
            with pytest.raises(GraphAuthError) as excinfo:
                await client.list_users()

    assert "invalid_client" in str(excinfo.value)
    assert route.call_count == 0  # never reached Graph without a token


async def test_token_transport_failure_is_wrapped(
    settings, msal_factory: Callable[..., type]
) -> None:
    msal_factory(raises=OSError("identity endpoint unreachable"))

    async with httpx.AsyncClient() as http_client:
        client = GraphClient(settings, client=http_client)
        with pytest.raises(GraphAuthError):
            client.get_token()


async def test_successful_token_is_sent_as_bearer(graph_client: GraphClient) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(USERS_URL).mock(return_value=httpx.Response(200, json={"value": []}))
        await graph_client.list_users()

    assert route.calls[0].request.headers["Authorization"].startswith("Bearer ")


# 8. HTTP 500 ------------------------------------------------------------------


async def test_http_500_raises_and_is_not_retried(
    graph_client: GraphClient, sleep_calls: list[float]
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(USERS_URL).mock(
            return_value=httpx.Response(
                500, json={"error": {"code": "internalServerError", "message": "boom"}}
            )
        )
        with pytest.raises(GraphHTTPError) as excinfo:
            await graph_client.list_users()

    assert excinfo.value.status_code == 500
    assert "internalServerError" in str(excinfo.value)
    assert route.call_count == 1
    assert sleep_calls == []


# 9. 429 retry honouring Retry-After -------------------------------------------


async def test_429_is_retried_honouring_retry_after(
    graph_client: GraphClient, sleep_calls: list[float]
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(USERS_URL).mock(
            side_effect=[
                httpx.Response(
                    429, headers={"Retry-After": "7"}, json={"error": {"code": "throttled"}}
                ),
                httpx.Response(200, json=_users_page([{"id": "user-1"}])),
            ]
        )
        users = await graph_client.list_users()

    assert [user.id for user in users] == ["user-1"]
    assert route.call_count == 2
    assert sleep_calls == [7.0]  # server's Retry-After, not our backoff


async def test_503_retries_then_gives_up_after_three_attempts(
    graph_client: GraphClient, sleep_calls: list[float]
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(USERS_URL).mock(
            return_value=httpx.Response(503, json={"error": {"code": "serviceUnavailable"}})
        )
        with pytest.raises(GraphHTTPError) as excinfo:
            await graph_client.list_users()

    assert excinfo.value.status_code == 503
    assert route.call_count == 3
    assert sleep_calls == [1.0, 2.0]  # exponential backoff when no Retry-After


async def test_unparseable_retry_after_falls_back_to_backoff(
    graph_client: GraphClient, sleep_calls: list[float]
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(USERS_URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
                httpx.Response(200, json={"value": []}),
            ]
        )
        await graph_client.list_users()

    assert sleep_calls == [1.0]


# 10. Timeouts -----------------------------------------------------------------


async def test_timeout_is_retried_then_raises_graph_http_error(
    graph_client: GraphClient, sleep_calls: list[float]
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(USERS_URL).mock(side_effect=httpx.ReadTimeout("timed out"))
        with pytest.raises(GraphHTTPError) as excinfo:
            await graph_client.list_users()

    assert "timed out" in str(excinfo.value)
    assert excinfo.value.status_code is None
    assert route.call_count == 3
    assert sleep_calls == [1.0, 2.0]


async def test_transient_timeout_recovers_on_retry(graph_client: GraphClient) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(USERS_URL).mock(
            side_effect=[
                httpx.ConnectTimeout("first attempt"),
                httpx.Response(200, json=_users_page([{"id": "user-1"}])),
            ]
        )
        users = await graph_client.list_users()

    assert len(users) == 1
    assert route.call_count == 2


# 11. Malformed JSON -----------------------------------------------------------


async def test_malformed_json_raises_graph_http_error(graph_client: GraphClient) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(USERS_URL).mock(
            return_value=httpx.Response(
                200,
                content=b"<html>not json at all</html>",
                headers={"Content-Type": "application/json"},
            )
        )
        with pytest.raises(GraphHTTPError) as excinfo:
            await graph_client.list_users()

    assert "Malformed JSON" in str(excinfo.value)


async def test_json_array_instead_of_object_raises(graph_client: GraphClient) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(USERS_URL).mock(return_value=httpx.Response(200, json=[{"id": "user-1"}]))
        with pytest.raises(GraphHTTPError) as excinfo:
            await graph_client.list_users()

    assert "Expected a JSON object" in str(excinfo.value)
