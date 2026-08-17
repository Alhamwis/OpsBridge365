"""SLA metrics computed from the SharePoint Tickets list.

Definitions (they match the Power Apps queue and the SLA watchdog flow):

* ``open_tickets`` - anything whose Status is not ``Resolved``.
* ``due_within_30min`` - unresolved, with ``SLAResolutionDue`` falling inside
  the next 30 minutes.
* ``sla_compliance_7d_pct`` - of the tickets resolved in the last 7 days, the
  share where ``ResolvedDate <= SLAResolutionDue``. ``None`` when nothing was
  resolved in the window, or nothing carried a due date to measure against.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from app.models import MetricsResponse, Ticket
from app.sharepoint import SharePointClient

__all__ = ["DUE_WINDOW", "SLA_WINDOW", "compute_metrics", "get_metrics"]

RESOLVED_STATUS: Final[str] = "Resolved"
DUE_WINDOW: Final[timedelta] = timedelta(minutes=30)
SLA_WINDOW: Final[timedelta] = timedelta(days=7)


def _as_utc(value: datetime) -> datetime:
    """Treat naive timestamps as UTC; SharePoint stores UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _is_resolved(ticket: Ticket) -> bool:
    return (ticket.status or "").strip().casefold() == RESOLVED_STATUS.casefold()


def compute_metrics(tickets: list[Ticket], now: datetime | None = None) -> MetricsResponse:
    """Pure function over ticket rows - no I/O, so it is trivially testable."""
    moment = _as_utc(now) if now is not None else datetime.now(UTC)
    due_cutoff = moment + DUE_WINDOW
    sla_cutoff = moment - SLA_WINDOW

    open_tickets = 0
    due_within_30min = 0
    resolved_last_7d = 0
    sla_measured = 0
    sla_met = 0

    for ticket in tickets:
        resolved = _is_resolved(ticket)
        due = _as_utc(ticket.sla_resolution_due) if ticket.sla_resolution_due else None
        resolved_at = _as_utc(ticket.resolved_date) if ticket.resolved_date else None

        if not resolved:
            open_tickets += 1
            if due is not None and moment <= due <= due_cutoff:
                due_within_30min += 1
            continue

        if resolved_at is None or not (sla_cutoff <= resolved_at <= moment):
            continue
        resolved_last_7d += 1
        if due is None:
            continue  # no target to measure against - excluded, never assumed met
        sla_measured += 1
        if resolved_at <= due:
            sla_met += 1

    compliance = round(100.0 * sla_met / sla_measured, 1) if sla_measured else None

    return MetricsResponse(
        open_tickets=open_tickets,
        due_within_30min=due_within_30min,
        sla_compliance_7d_pct=compliance,
        resolved_last_7d=resolved_last_7d,
        sla_measured_last_7d=sla_measured,
        generated_at=moment,
    )


async def get_metrics(sharepoint: SharePointClient, now: datetime | None = None) -> MetricsResponse:
    """Fetch the Tickets list and reduce it to the metrics payload."""
    tickets = await sharepoint.list_tickets()
    return compute_metrics(tickets, now=now)
