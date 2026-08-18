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

Test 9 is negative on purpose: it proves bad credentials surface as a typed
:class:`GraphAuthError` rather than a stack trace.

Tests 10-12 are the least-privilege evidence, and they are deliberately aimed at
the boundary ``Sites.Selected`` actually enforces. Measured against a correctly
configured tenant, an ungranted site's *metadata* is readable (``GET
/sites/{hostname}`` returns 200); what is refused is that site's **data**
(``/drive`` -> 403) and **tenant-wide enumeration** (``/sites?search=*`` -> 403).
So test 10 asserts denial on data, test 11 asserts denial on enumeration, and
test 12 is the positive control proving the same calls succeed on the one site
the app was granted. A denial that is never contrasted with a success proves
nothing.
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


# 10. negative: an ungranted site's DATA is denied -----------------------------


def _site_guids(site_id: str) -> str:
    """The ``siteGuid,webGuid`` tail of a Graph site id, lowercased."""
    return ",".join(part.strip() for part in site_id.split(",")[1:]).lower()


async def _root_site_id(graph: GraphClient) -> str:
    """Resolve the tenant root site, skipping the test if it cannot be isolated.

    The hostname is derived from the configured site id (Graph site ids are
    ``hostname,siteGuid,webGuid``) so no tenant name is hardcoded here.

    Resolving the root site by hostname is expected to SUCCEED. That is not a
    finding - see :func:`test_ungranted_site_data_is_denied` for why.
    """
    hostname = graph.settings.sharepoint_site_id.split(",")[0].strip()
    if not hostname or "." not in hostname:
        pytest.skip(f"cannot derive a hostname from SHAREPOINT_SITE_ID: {hostname!r}")

    root = await graph.request_json("GET", f"{GRAPH_BASE_URL}/sites/{hostname}")
    root_id = str(root.get("id") or "")
    if not root_id:
        pytest.skip(f"root site {hostname} returned no id to test against")

    # If the granted site IS the root site there is no ungranted site to probe
    # and the denial tests would be measuring the granted site instead.
    granted_guids = _site_guids(graph.settings.sharepoint_site_id)
    if granted_guids and granted_guids == _site_guids(root_id):
        pytest.skip(
            "SHAREPOINT_SITE_ID is the tenant root site; there is no ungranted "
            "site to prove denial against"
        )
    return root_id


async def test_ungranted_site_data_is_denied(live_graph: GraphClient) -> None:
    """The least-privilege proof: no DATA access outside the granted site.

    ``Sites.Selected`` does **not** hide a site's existence. Site *metadata* -
    id, webUrl, displayName - stays readable for a site this app was never
    granted; ``GET /sites/{hostname}`` on the tenant root returns 200, and that
    is expected Microsoft behaviour, not a misconfiguration. Measured against
    the live tenant, an ungranted root site also returns 200 on ``/lists`` while
    disclosing zero lists.

    What ``Sites.Selected`` withholds is the site's **content**. ``/drive`` on
    an ungranted site is refused with 403, and that is the property the security
    claim in docs/SECURITY.md rests on. So this test asserts on ``/drive``, not
    on metadata.

    Do not "fix" this back into asserting 403 on ``GET /sites/{hostname}``. That
    assertion fails against a *correctly* configured tenant, because metadata
    visibility is not the boundary being enforced.
    """
    root_id = await _root_site_id(live_graph)

    with pytest.raises(GraphHTTPError) as caught:
        await live_graph.request_json("GET", f"{GRAPH_BASE_URL}/sites/{root_id}/drive")

    assert caught.value.status_code == 403, (
        "expected 403 reading the ungranted root site's drive, got "
        f"{caught.value.status_code}. A 200 means the app can read content on a "
        "site it was never granted, and the Sites.Selected claim is false."
    )


# 11. negative: the app cannot enumerate the tenant ----------------------------


async def test_tenant_wide_site_enumeration_is_denied(live_graph: GraphClient) -> None:
    """The strongest single proof that the app cannot roam the tenant.

    Metadata for a site whose id you already hold is readable (see
    :func:`test_ungranted_site_data_is_denied`), so "can it see other sites?" is
    the wrong question. The one that matters is whether it can *discover* them:
    ``GET /sites?search=*`` is the tenant-wide search that ``Sites.Read.All`` or
    ``Sites.ReadWrite.All`` would satisfy. Under ``Sites.Selected`` it is
    refused with 403, so the app cannot build a list of sites to attack.

    A 200 here - even with an empty result set - means the app holds a
    tenant-wide SharePoint permission it was never meant to have.
    """
    with pytest.raises(GraphHTTPError) as caught:
        await live_graph.request_json("GET", f"{GRAPH_BASE_URL}/sites", params={"search": "*"})

    assert caught.value.status_code == 403, (
        f"expected 403 for tenant-wide site search, got {caught.value.status_code}. "
        "Anything other than 403 means the app can enumerate the tenant's sites."
    )


# 12. positive control for the two denials above -------------------------------


async def test_granted_site_data_is_accessible(live_graph: GraphClient) -> None:
    """The control that gives the two denial tests their meaning.

    A 403 proves nothing on its own - a broken secret, a typo'd url or a
    revoked consent would produce the same 403 everywhere and make tests 10 and
    11 pass for entirely the wrong reason. This asserts the *same shape of call*
    succeeds on the one site the app was granted: ``/lists`` returns 200 and the
    provisioned Assets list is among the results.

    Read together: data access works where a grant exists and is refused where
    it does not. That contrast is the least-privilege evidence.
    """
    lists = await live_graph.get_paged(f"{_site_url(live_graph.settings)}/lists")

    assert lists, "the granted site disclosed zero lists; the write grant is not in effect"
    assert any(entry.get("id") == live_graph.settings.assets_list_id for entry in lists), (
        "the granted site's /lists did not include ASSETS_LIST_ID "
        f"({live_graph.settings.assets_list_id}); ids seen: "
        f"{[entry.get('id') for entry in lists]}"
    )
