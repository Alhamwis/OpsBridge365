"""Live tests against the real Microsoft 365 tenant.

These are NOT part of the default run. ``python -m pytest`` deselects them via
``addopts = "-ra -m 'not integration'"`` in pyproject.toml, so the offline suite
stays the thing that gates commits. Opt in explicitly::

    pytest -m integration            # just these
    pytest -m ""                     # offline + live, everything

Every value is read from the environment (see ``REQUIRED_ENV_VARS`` in
conftest.py); nothing tenant-specific is committed here. With the environment
unset each test SKIPS with a reason - it never fails.

Safety: exactly one test writes, and it restores the original value in a
``finally`` block, so the suite is safe to re-run and leaves the lists byte-for
-byte as it found them.

The last two tests are negative on purpose. Test 9 proves bad credentials
surface as a typed :class:`GraphAuthError` rather than a stack trace. Test 10 is
the least-privilege evidence: the app holds ``Sites.Selected`` with a ``write``
grant on ONE site, so a request for a site it was never granted must come back
403. A passing test 10 is what distinguishes ``Sites.Selected`` from the
tenant-wide ``Sites.ReadWrite.All`` it is often mistaken for.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.graph import GRAPH_BASE_URL, GraphAuthError, GraphClient, GraphHTTPError
from app.models import AssetItem, GraphDevice, GraphUser, Ticket
from app.sharepoint import SharePointClient
from tests.integration.conftest import requires_live_env

pytestmark = [pytest.mark.integration, requires_live_env]

#: The field the round-trip test mutates. Deliberately NOT one of the columns
#: the sync job owns (AssignedUser / ComplianceStatus / LastCheckIn) so a sync
#: running concurrently cannot be mistaken for this test's write.
ROUND_TRIP_FIELD = "AssetTag"

#: Marker written during the round-trip. Obvious in a list view if a failure
#: ever managed to leave it behind.
ROUND_TRIP_MARKER = "pytest-roundtrip-delete-me"


def _site_url(settings: Settings) -> str:
    return f"{GRAPH_BASE_URL}/sites/{settings.sharepoint_site_id}"


def _list_url(settings: Settings, list_id: str) -> str:
    return f"{_site_url(settings)}/lists/{list_id}"


async def _item_fields(graph: GraphClient, list_id: str, item_id: str) -> dict[str, Any]:
    """Raw field bag of one list item, straight from Graph."""
    url = f"{_list_url(graph.settings, list_id)}/items/{item_id}/fields"
    return await graph.request_json("GET", url)


# 1. authentication ------------------------------------------------------------


def test_client_credentials_token_acquisition_succeeds(live_graph: GraphClient) -> None:
    """The configured app registration can actually get an app-only token."""
    token = live_graph.get_token()

    assert isinstance(token, str)
    assert token, "Entra returned an empty access token"
    # A JWT is three dot-separated segments. Checking the shape - never the
    # contents - keeps the token out of any assertion message.
    assert token.count(".") == 2, "access token is not a JWT"


# 2. User.Read.All -------------------------------------------------------------


async def test_list_users_returns_parsed_graph_users(live_graph: GraphClient) -> None:
    """User.Read.All is consented and /users parses into GraphUser."""
    users = await live_graph.list_users()

    assert users, "tenant reported zero users, which cannot be right"
    assert all(isinstance(user, GraphUser) for user in users)
    assert all(user.id for user in users), "every user must carry an id"
    # .label is the sync job's fallback chain; exercise it on real data.
    assert all(user.label for user in users)


# 3. Device.Read.All -----------------------------------------------------------


async def test_list_devices_succeeds_and_parses(live_graph: GraphClient) -> None:
    """Device.Read.All works.

    A tenant with no Entra-registered devices is a legitimate state, so this
    asserts the call succeeds and returns a list - NOT that it is non-empty.
    Asserting non-empty here would be asserting a fact about the tenant, not
    about the code.
    """
    devices = await live_graph.list_devices()

    assert isinstance(devices, list)
    assert all(isinstance(device, GraphDevice) for device in devices)
    assert all(device.id for device in devices)


# 4. Sites.Selected: the granted site ------------------------------------------


async def test_granted_site_is_reachable_by_id(live_graph: GraphClient) -> None:
    """The one site the app was granted resolves by id."""
    site = await live_graph.request_json("GET", _site_url(live_graph.settings))

    assert site.get("id") == live_graph.settings.sharepoint_site_id
    assert site.get("webUrl", "").startswith("https://")


# 5. Assets list ---------------------------------------------------------------


async def test_assets_list_reads_back_seeded_rows(live_sharepoint: SharePointClient) -> None:
    """The provisioned Assets list is readable and parses into AssetItem."""
    assets = await live_sharepoint.list_assets()

    assert assets, "Assets list is empty; run scripts/provision_sharepoint.py"
    assert all(isinstance(asset, AssetItem) for asset in assets)
    assert all(asset.id for asset in assets)
    # Title carries the asset tag - the field the sync job matches devices on.
    assert any(asset.title for asset in assets), "no Assets row has a Title"


# 6. Tickets list --------------------------------------------------------------


async def test_tickets_list_reads_back_seeded_rows(live_sharepoint: SharePointClient) -> None:
    """The provisioned Tickets list is readable and parses into Ticket."""
    tickets = await live_sharepoint.list_tickets()

    assert tickets, "Tickets list is empty; run scripts/provision_sharepoint.py"
    assert all(isinstance(ticket, Ticket) for ticket in tickets)
    assert any(ticket.ticket_id for ticket in tickets), "no Tickets row has a TicketID"


# 7. PATCH round-trip ----------------------------------------------------------


async def test_patch_round_trip_restores_the_original_value(
    live_graph: GraphClient, live_sharepoint: SharePointClient
) -> None:
    """Prove the write path works, then put the row back exactly as it was.

    Read current value -> PATCH a marker -> read it back -> restore. The restore
    is in ``finally``, so an assertion failure in the middle still leaves the
    list unchanged.
    """
    list_id = live_graph.settings.assets_list_id
    assets = await live_sharepoint.list_assets()
    assert assets, "Assets list is empty; run scripts/provision_sharepoint.py"

    item_id = assets[0].id
    before = await _item_fields(live_graph, list_id, item_id)
    original = before.get(ROUND_TRIP_FIELD)
    assert original != ROUND_TRIP_MARKER, (
        f"item {item_id} already holds the test marker - a previous run did not "
        "restore it; fix the row by hand before re-running"
    )

    try:
        await live_sharepoint.patch_asset(item_id, {ROUND_TRIP_FIELD: ROUND_TRIP_MARKER})

        during = await _item_fields(live_graph, list_id, item_id)
        assert during.get(ROUND_TRIP_FIELD) == ROUND_TRIP_MARKER, "PATCH did not take effect"
    finally:
        # Send the original back even if it was absent: Graph clears the field
        # on null, which is the correct restore for a value that was not set.
        await live_sharepoint.patch_asset(item_id, {ROUND_TRIP_FIELD: original})

    after = await _item_fields(live_graph, list_id, item_id)
    assert after.get(ROUND_TRIP_FIELD) == original, (
        f"restore failed: item {item_id} field {ROUND_TRIP_FIELD} was not put back"
    )


# 8. paging --------------------------------------------------------------------


async def test_paging_helper_follows_a_real_next_link(live_graph: GraphClient) -> None:
    """``get_paged`` follows a genuine ``@odata.nextLink``, not a mocked one.

    ``$top=1`` forces Graph to hand back a nextLink for any list with more than
    one row, so the loop in ``GraphClient.get_paged`` is really exercised. The
    first assertion proves the server actually paginated; the second proves the
    helper collected every page rather than stopping at the first.
    """
    items_url = f"{_list_url(live_graph.settings, live_graph.settings.assets_list_id)}/items"

    first_page = await live_graph.request_json("GET", items_url, params={"$top": "1"})
    assert len(first_page.get("value", [])) == 1
    assert first_page.get("@odata.nextLink"), (
        "Graph did not paginate at $top=1; the Assets list needs at least two "
        "rows for this test to mean anything"
    )

    one_per_page = await live_graph.get_paged(items_url, params={"$top": "1"})
    default_paging = await live_graph.get_paged(items_url)

    assert len(one_per_page) > 1, "paging stopped after the first page"
    assert len(one_per_page) == len(default_paging), (
        "page size changed the result set; get_paged is dropping or repeating rows"
    )
    ids = [item.get("id") for item in one_per_page]
    assert len(ids) == len(set(ids)), "get_paged returned duplicate items across pages"


# 9. negative: bad credentials -------------------------------------------------


async def test_wrong_client_secret_raises_graph_auth_error(live_settings: Settings) -> None:
    """A wrong secret must surface as GraphAuthError, not an arbitrary crash.

    The bad value below is a literal, not a mangled copy of the real secret, so
    nothing derived from the credential exists in this process.
    """
    broken = live_settings.model_copy(
        update={"azure_client_secret": "deliberately-invalid-secret-not-a-credential"}
    )
    client = GraphClient(broken)
    try:
        with pytest.raises(GraphAuthError) as caught:
            client.get_token()
    finally:
        await client.aclose()

    # Entra's own code for a rejected client secret. Asserting on it proves the
    # failure came from the identity platform rather than from a local typo.
    assert "AADSTS7000215" in str(caught.value) or "invalid_client" in str(caught.value)


# 10. negative: Sites.Selected is genuinely scoped -----------------------------


async def test_ungranted_site_returns_403(live_graph: GraphClient) -> None:
    """The least-privilege proof.

    ``Sites.Selected`` carries no site access by itself - access comes from
    per-site grants, and this app has exactly one. Asking for the tenant ROOT
    site, which was never granted, must be refused with 403. If this ever
    returns 200 the app is holding tenant-wide SharePoint access and the
    security claim in docs/SECURITY.md is false.

    The hostname is derived from the configured site id (Graph site ids are
    ``hostname,siteGuid,webGuid``) so no tenant name is hardcoded.
    """
    hostname = live_graph.settings.sharepoint_site_id.split(",")[0].strip()
    if not hostname or "." not in hostname:
        pytest.skip(f"cannot derive a hostname from SHAREPOINT_SITE_ID: {hostname!r}")

    with pytest.raises(GraphHTTPError) as caught:
        await live_graph.request_json("GET", f"{GRAPH_BASE_URL}/sites/{hostname}")

    assert caught.value.status_code == 403, (
        f"expected 403 for the ungranted root site {hostname}, got "
        f"{caught.value.status_code}. A 200 means the app has broader SharePoint "
        "access than Sites.Selected plus one grant."
    )
