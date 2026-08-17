#!/usr/bin/env python
"""Idempotent SharePoint provisioning for OpsBridge365, via Microsoft Graph.

Creates (or completes) the two lists the cloud layer expects on the configured
site:

* ``Assets``  - written by the sync job (``python -m app.sync``)
* ``Tickets`` - read by ``GET /metrics``

Everything here is idempotent: it inspects what exists, creates only what is
missing, and never modifies or deletes anything that is already there. Running
it a second time is a no-op that prints "already exists" for every item.

Authentication is *not* reimplemented - :class:`app.graph.GraphClient` owns the
client-credentials flow, the retry policy, and the paging loop, and this script
drives it. The only thing it does differently is build :class:`app.config.Settings`
without ``ASSETS_LIST_ID`` / ``TICKETS_LIST_ID``, because those two ids are the
output of provisioning, not an input to it. The ids are printed at the end so
they can be pasted into ``.env`` or the deployment secrets.

Usage::

    python scripts/provision_sharepoint.py --dry-run     # plan only, no network
    python scripts/provision_sharepoint.py               # create what is missing
    python scripts/provision_sharepoint.py --seed        # ... and seed empty lists

``--dry-run`` is also the automatic behaviour when credentials are absent, so
the plan is inspectable on a machine with no tenant at all.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

# Run directly (`python scripts/provision_sharepoint.py`) and sys.path[0] is
# scripts/, not the repo root - so `app` would not import. Fix that before the
# project imports below, which is why they are not at the top of the file.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.config import Settings  # noqa: E402
from app.graph import GRAPH_BASE_URL, GraphClient, GraphError  # noqa: E402

__all__ = ["ColumnSpec", "ListSpec", "Report", "build_specs", "main"]

# --------------------------------------------------------------------- specs --

TEXT = "text"
CHOICE = "choice"
DATE_TIME = "dateTime"

COMPLIANCE_CHOICES = ("Compliant", "NonCompliant", "Unknown")
TICKET_STATUS_CHOICES = ("New", "InProgress", "Resolved")
TICKET_PRIORITY_CHOICES = ("Low", "Medium", "High")


@dataclass(frozen=True)
class ColumnSpec:
    """One SharePoint column, in the shape Graph's ``/columns`` endpoint wants."""

    name: str
    kind: str
    choices: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        """Request body for ``POST /lists/{id}/columns``."""
        body: dict[str, Any] = {"name": self.name, "enforceUniqueValues": False}
        if self.kind == TEXT:
            body[TEXT] = {"allowMultipleLines": False, "textType": "plain"}
        elif self.kind == CHOICE:
            body[CHOICE] = {
                "allowTextEntry": False,
                "choices": list(self.choices),
                "displayAs": "dropDownMenu",
            }
        elif self.kind == DATE_TIME:
            body[DATE_TIME] = {"displayAs": "default", "format": "dateTime"}
        else:  # pragma: no cover - guards a typo in the specs below
            raise ValueError(f"Unsupported column kind: {self.kind}")
        return body

    def describe(self) -> str:
        if self.kind == CHOICE:
            return f"{self.name} (choice: {' / '.join(self.choices)})"
        return f"{self.name} ({self.kind})"


@dataclass(frozen=True)
class ListSpec:
    """A list, its columns, and the sample rows used to seed it when empty."""

    display_name: str
    description: str
    columns: tuple[ColumnSpec, ...]
    seed_rows: tuple[dict[str, Any], ...]


def _stamp(moment: datetime) -> str:
    """ISO 8601 in UTC with a trailing Z - the format SharePoint accepts."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_specs(now: datetime | None = None) -> tuple[ListSpec, ListSpec]:
    """Build the Assets and Tickets specs, with seed timestamps relative to now.

    Seed rows are deliberately, visibly fictional: ``CONTOSO-*`` asset tags,
    ``SN-SAMPLE-*`` serials, ``INC-100x`` ticket references, and summaries
    prefixed ``Sample -``. No real person, device, serial number, or ticket
    reference appears anywhere in this file.

    Seeded Assets rows carry ``AssignedUser`` and ``ComplianceStatus`` of
    ``Unknown`` on purpose. Those two columns are the sync job's output; seeding
    them with invented values would mean the first sync had nothing to prove.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)

    assets = ListSpec(
        display_name="Assets",
        description="OpsBridge365 device inventory. Updated by the opsbridge-sync job.",
        columns=(
            ColumnSpec("AssetTag", TEXT),
            ColumnSpec("SerialNumber", TEXT),
            ColumnSpec("DeviceName", TEXT),
            ColumnSpec("AssignedUser", TEXT),
            ColumnSpec("ComplianceStatus", CHOICE, COMPLIANCE_CHOICES),
            ColumnSpec("LastCheckIn", DATE_TIME),
        ),
        seed_rows=(
            {
                "Title": "CONTOSO-LT-001",
                "AssetTag": "CONTOSO-LT-001",
                "SerialNumber": "SN-SAMPLE-0001",
                "DeviceName": "CONTOSO-LT-001",
                "AssignedUser": "Unknown",
                "ComplianceStatus": "Unknown",
            },
            {
                "Title": "CONTOSO-LT-002",
                "AssetTag": "CONTOSO-LT-002",
                "SerialNumber": "SN-SAMPLE-0002",
                "DeviceName": "CONTOSO-LT-002",
                "AssignedUser": "Unknown",
                "ComplianceStatus": "Unknown",
            },
            {
                "Title": "CONTOSO-DT-003",
                "AssetTag": "CONTOSO-DT-003",
                "SerialNumber": "SN-SAMPLE-0003",
                "DeviceName": "CONTOSO-DT-003",
                "AssignedUser": "Unknown",
                "ComplianceStatus": "Unknown",
            },
            {
                "Title": "CONTOSO-TB-004",
                "AssetTag": "CONTOSO-TB-004",
                "SerialNumber": "SN-SAMPLE-0004",
                "DeviceName": "CONTOSO-TB-004",
                "AssignedUser": "Unknown",
                "ComplianceStatus": "Unknown",
            },
        ),
    )

    # Four rows that between them exercise every branch of app/metrics.py: one
    # open and inside the 30-minute window, one open and comfortably ahead of
    # it, one resolved inside SLA, one resolved late. A first /metrics call
    # therefore returns non-trivial numbers instead of all zeros.
    tickets = ListSpec(
        display_name="Tickets",
        description="OpsBridge365 service-desk queue. Read by GET /metrics.",
        columns=(
            # TicketID is the human-facing reference an agent reads out on a
            # call ("that is INC-1001"). SharePoint's own numeric ID is an
            # internal key, not that. app.models.Ticket already reads this
            # field, so a list without it parses to ticket_id=None on every row.
            ColumnSpec("TicketID", TEXT),
            ColumnSpec("Status", CHOICE, TICKET_STATUS_CHOICES),
            ColumnSpec("Priority", CHOICE, TICKET_PRIORITY_CHOICES),
            ColumnSpec("SLAResolutionDue", DATE_TIME),
            ColumnSpec("ResolvedDate", DATE_TIME),
        ),
        seed_rows=(
            {
                "Title": "Sample - VPN client will not connect",
                "TicketID": "INC-1001",
                "Status": "New",
                "Priority": "High",
                "SLAResolutionDue": _stamp(moment + timedelta(minutes=15)),
            },
            {
                "Title": "Sample - Printer offline in Lab B",
                "TicketID": "INC-1002",
                "Status": "InProgress",
                "Priority": "Medium",
                "SLAResolutionDue": _stamp(moment + timedelta(hours=6)),
            },
            {
                "Title": "Sample - Access request for shared mailbox",
                "TicketID": "INC-1003",
                "Status": "Resolved",
                "Priority": "Low",
                "SLAResolutionDue": _stamp(moment - timedelta(days=2)),
                "ResolvedDate": _stamp(moment - timedelta(days=2, hours=3)),
            },
            {
                "Title": "Sample - Laptop replacement request",
                "TicketID": "INC-1004",
                "Status": "Resolved",
                "Priority": "Medium",
                "SLAResolutionDue": _stamp(moment - timedelta(days=3)),
                "ResolvedDate": _stamp(moment - timedelta(days=2)),
            },
        ),
    )

    return assets, tickets


# Title exists on every generic list the moment it is created, so it is never
# posted as a new column - it is reported as already existing instead.
BUILT_IN_COLUMNS = frozenset({"title"})


# ------------------------------------------------------------------- report --

CREATED = "CREATED"
EXISTS = "EXISTS"
PLANNED = "WOULD CREATE"
SKIPPED = "SKIPPED"
FAILED = "FAILED"


@dataclass
class Report:
    """Accumulates one line per decision, for the summary table at the end."""

    rows: list[tuple[str, str, str]] = field(default_factory=list)
    list_ids: dict[str, str] = field(default_factory=dict)

    def add(self, status: str, target: str, detail: str = "") -> None:
        self.rows.append((status, target, detail))

    @property
    def failed(self) -> bool:
        return any(status == FAILED for status, _, _ in self.rows)

    def print_summary(self, *, dry_run: bool) -> None:
        print()
        print("=" * 78)
        print("SUMMARY" + ("  (dry run - nothing was changed)" if dry_run else ""))
        print("=" * 78)

        width = max((len(status) for status, _, _ in self.rows), default=6)
        for status, target, detail in self.rows:
            suffix = f"  {detail}" if detail else ""
            print(f"  {status.ljust(width)}  {target}{suffix}")

        counts: dict[str, int] = {}
        for status, _, _ in self.rows:
            counts[status] = counts.get(status, 0) + 1
        print()
        tally = ", ".join(f"{name.lower()}: {count}" for name, count in sorted(counts.items()))
        print(f"  {tally}")

        if self.list_ids:
            print()
            print("  List ids (these are what ASSETS_LIST_ID / TICKETS_LIST_ID want):")
            for name, list_id in self.list_ids.items():
                print(f"    {name.upper()}_LIST_ID={list_id}")


# ------------------------------------------------------------------ settings --


def _missing_variables(error: ValidationError) -> tuple[str, ...]:
    """Environment variable names a ValidationError is complaining about."""
    names: list[str] = []
    for detail in error.errors():
        location = detail.get("loc") or ()
        if location:
            name = str(location[0]).upper()
            if name not in names:
                names.append(name)
    return tuple(names)


def load_settings() -> tuple[Settings | None, tuple[str, ...]]:
    """Load credentials, or report what is missing.

    ``ASSETS_LIST_ID`` and ``TICKETS_LIST_ID`` are supplied as empty strings:
    :class:`Settings` requires them for the sync and the API, but this script
    is what *produces* them, so demanding them here would be circular.
    """
    try:
        settings = Settings(assets_list_id="", tickets_list_id="")  # type: ignore[call-arg]
    except ValidationError as exc:
        return None, _missing_variables(exc)
    return settings, ()


# --------------------------------------------------------------- graph calls --


class Provisioner:
    """Ensures lists, columns, and seed rows exist. Creates, never modifies."""

    def __init__(self, graph: GraphClient, report: Report, *, seed: bool) -> None:
        self._graph = graph
        self._report = report
        self._seed = seed
        self._site_id = graph.settings.sharepoint_site_id

    @property
    def _site_url(self) -> str:
        return f"{GRAPH_BASE_URL}/sites/{self._site_id}"

    async def ensure_list(self, spec: ListSpec) -> None:
        existing = await self._find_list(spec.display_name)
        if existing is None:
            list_id = await self._create_list(spec)
            self._report.add(CREATED, f"list {spec.display_name}")
        else:
            list_id = str(existing.get("id", ""))
            self._report.add(EXISTS, f"list {spec.display_name}")
        self._report.list_ids[spec.display_name] = list_id

        await self._ensure_columns(spec, list_id)
        await self._maybe_seed(spec, list_id)

    async def _find_list(self, display_name: str) -> dict[str, Any] | None:
        """Look the list up by display name (Graph has no reliable filter here)."""
        wanted = display_name.casefold()
        lists = await self._graph.get_paged(f"{self._site_url}/lists", params={"$top": "999"})
        for entry in lists:
            names = {
                str(entry.get("displayName") or "").casefold(),
                str(entry.get("name") or "").casefold(),
            }
            if wanted in names:
                return entry
        return None

    async def _create_list(self, spec: ListSpec) -> str:
        """Create an empty generic list; columns are added by the step after."""
        payload = {
            "displayName": spec.display_name,
            "description": spec.description,
            "list": {"template": "genericList"},
        }
        created = await self._graph.request_json("POST", f"{self._site_url}/lists", json=payload)
        return str(created.get("id", ""))

    async def _ensure_columns(self, spec: ListSpec, list_id: str) -> None:
        """Add any spec column the list does not already have.

        Deliberately per-column, not per-list: this runs for a list that was
        just created *and* for one that already existed, so a list provisioned
        before a column was added to the spec gains it on the next run instead
        of being skipped wholesale. That is what makes adding ``TicketID`` to
        an already-provisioned Tickets list work.

        Existing rows are left alone. A new column on a populated list starts
        empty on every row, and backfilling it would mean writing invented
        values over real data - which this script never does.
        """
        present = await self._existing_column_names(list_id)
        for column in spec.columns:
            label = f"  column {spec.display_name}.{column.describe()}"
            if column.name.casefold() in present:
                self._report.add(EXISTS, label)
                continue
            try:
                await self._graph.request_json(
                    "POST",
                    f"{self._site_url}/lists/{list_id}/columns",
                    json=column.payload(),
                )
            except GraphError as exc:
                self._report.add(FAILED, label, str(exc))
                continue
            self._report.add(CREATED, label)

    async def _existing_column_names(self, list_id: str) -> set[str]:
        columns = await self._graph.get_paged(
            f"{self._site_url}/lists/{list_id}/columns", params={"$top": "999"}
        )
        names = set(BUILT_IN_COLUMNS)
        for column in columns:
            for key in ("name", "displayName"):
                value = column.get(key)
                if value:
                    names.add(str(value).casefold())
        return names

    async def _maybe_seed(self, spec: ListSpec, list_id: str) -> None:
        label = f"  seed {spec.display_name} ({len(spec.seed_rows)} sample rows)"
        if not self._seed:
            self._report.add(SKIPPED, label, "pass --seed to seed an empty list")
            return

        # Only ever seed an empty list. Anything else would append duplicates on
        # every run, which is the opposite of idempotent.
        probe = await self._graph.request_json(
            "GET", f"{self._site_url}/lists/{list_id}/items", params={"$top": "1"}
        )
        if probe.get("value"):
            self._report.add(SKIPPED, label, "list is not empty")
            return

        for row in spec.seed_rows:
            try:
                await self._graph.request_json(
                    "POST",
                    f"{self._site_url}/lists/{list_id}/items",
                    json={"fields": dict(row)},
                )
            except GraphError as exc:
                self._report.add(FAILED, f"  seed row {row.get('Title')}", str(exc))
                return
        self._report.add(CREATED, label)


# ------------------------------------------------------------------ dry run --


def print_plan(specs: tuple[ListSpec, ...], *, seed: bool, site_id: str | None) -> Report:
    """Print exactly what a real run would attempt. Sends nothing anywhere."""
    report = Report()
    target = site_id or "<SHAREPOINT_SITE_ID not set>"
    print(f"Site: {target}")
    print()

    for spec in specs:
        print(f"List '{spec.display_name}' - {spec.description}")
        report.add(PLANNED, f"list {spec.display_name}", "if absent")
        print("  ensure list exists (create if absent)")
        for column in spec.columns:
            print(f"  ensure column {column.describe()}")
            report.add(PLANNED, f"  column {spec.display_name}.{column.describe()}", "if absent")
        print("  ensure column Title (text) - built in, never created")
        report.add(EXISTS, f"  column {spec.display_name}.Title (text)", "built in")

        label = f"  seed {spec.display_name} ({len(spec.seed_rows)} sample rows)"
        if seed:
            print(f"  seed {len(spec.seed_rows)} sample rows, ONLY if the list is empty:")
            for row in spec.seed_rows:
                print(f"    {row.get('Title')}")
            report.add(PLANNED, label, "only if the list is empty")
        else:
            print("  seeding not requested (--seed)")
            report.add(SKIPPED, label, "pass --seed to seed an empty list")
        print()

    return report


# --------------------------------------------------------------------- main --


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="provision_sharepoint.py",
        description=(
            "Idempotently provision the OpsBridge365 Assets and Tickets lists on "
            "the configured SharePoint site. Safe to run repeatedly."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and change nothing. Implied when credentials are absent.",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Also insert a few clearly fictional sample rows, but only into an empty list.",
    )
    return parser.parse_args(argv)


async def provision(settings: Settings, specs: tuple[ListSpec, ...], *, seed: bool) -> Report:
    report = Report()
    async with GraphClient(settings) as graph:
        provisioner = Provisioner(graph, report, seed=seed)
        for spec in specs:
            await provisioner.ensure_list(spec)
    return report


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    specs = build_specs()

    print("OpsBridge365 - SharePoint provisioning")
    print("=" * 78)

    settings, missing = load_settings()
    dry_run = options.dry_run or settings is None

    if settings is None:
        print("No usable credentials in the environment or .env:")
        print(f"  missing: {', '.join(missing) or 'unknown'}")
        print("Falling back to a dry run - the plan below is what a real run would do.")
        print()
    elif options.dry_run:
        print("--dry-run: no request will be sent, nothing will be changed.")
        print()

    if dry_run:
        report = print_plan(
            specs,
            seed=options.seed,
            site_id=settings.sharepoint_site_id if settings else None,
        )
        report.print_summary(dry_run=True)
        return 0

    assert settings is not None  # narrowed by the dry_run branch above
    print(f"Site: {settings.sharepoint_site_id}")
    print("Creating only what is missing; nothing existing is modified or deleted.")
    print()

    try:
        report = asyncio.run(provision(settings, specs, seed=options.seed))
    except GraphError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    report.print_summary(dry_run=False)
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
