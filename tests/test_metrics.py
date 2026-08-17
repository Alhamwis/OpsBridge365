"""SLA metrics: open, at-risk, 7-day compliance, and the empty-window case."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.metrics import compute_metrics, get_metrics
from app.models import Ticket
from tests.fakes import FakeSharePoint

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def _ticket(
    ticket_id: str,
    status: str,
    due_offset: timedelta | None = None,
    resolved_offset: timedelta | None = None,
) -> Ticket:
    return Ticket(
        id=ticket_id,
        ticket_id=ticket_id,
        status=status,
        sla_resolution_due=NOW + due_offset if due_offset is not None else None,
        resolved_date=NOW + resolved_offset if resolved_offset is not None else None,
    )


# 12. open_tickets -------------------------------------------------------------


def test_open_tickets_counts_everything_not_resolved() -> None:
    tickets = [
        _ticket("T1", "New"),
        _ticket("T2", "Assigned"),
        _ticket("T3", "In Progress"),
        _ticket("T4", "Resolved", resolved_offset=-timedelta(hours=1)),
        _ticket("T5", ""),  # no status yet - not resolved, so it is open
    ]

    assert compute_metrics(tickets, now=NOW).open_tickets == 4


def test_resolved_status_comparison_ignores_case_and_padding() -> None:
    tickets = [_ticket("T1", " resolved ", resolved_offset=-timedelta(hours=2))]

    assert compute_metrics(tickets, now=NOW).open_tickets == 0


def test_no_tickets_at_all_is_all_zeroes() -> None:
    metrics = compute_metrics([], now=NOW)

    assert metrics.open_tickets == 0
    assert metrics.due_within_30min == 0
    assert metrics.generated_at == NOW


# 13. due_within_30min ---------------------------------------------------------


def test_due_within_30min_counts_only_unresolved_tickets_in_the_window() -> None:
    tickets = [
        _ticket("in-10-min", "Assigned", due_offset=timedelta(minutes=10)),
        _ticket("in-29-min", "In Progress", due_offset=timedelta(minutes=29)),
        _ticket("in-30-min", "New", due_offset=timedelta(minutes=30)),  # boundary: counted
        _ticket("in-31-min", "New", due_offset=timedelta(minutes=31)),  # outside
        _ticket("overdue", "New", due_offset=-timedelta(minutes=5)),  # already breached
        _ticket("no-due-date", "New"),
        _ticket(
            "resolved-soon-due",
            "Resolved",
            due_offset=timedelta(minutes=5),
            resolved_offset=-timedelta(minutes=1),
        ),
    ]

    assert compute_metrics(tickets, now=NOW).due_within_30min == 3


# 14. sla_compliance_7d_pct ----------------------------------------------------


def test_sla_compliance_over_the_last_seven_days() -> None:
    tickets = [
        # met: resolved before due, inside the window
        _ticket("T1", "Resolved", timedelta(days=-1), -timedelta(days=1, hours=2)),
        _ticket("T2", "Resolved", -timedelta(days=2), -timedelta(days=2, hours=1)),
        _ticket("T3", "Resolved", -timedelta(days=3), -timedelta(days=3, minutes=30)),
        # breached: resolved after due
        _ticket("T4", "Resolved", -timedelta(days=4, hours=2), -timedelta(days=4)),
        # outside the 7-day window: ignored entirely
        _ticket("T5", "Resolved", -timedelta(days=20), -timedelta(days=19)),
        # still open: not part of the compliance calculation
        _ticket("T6", "In Progress", timedelta(hours=4)),
    ]

    metrics = compute_metrics(tickets, now=NOW)

    assert metrics.resolved_last_7d == 4
    assert metrics.sla_measured_last_7d == 4
    assert metrics.sla_compliance_7d_pct == 75.0


def test_resolved_exactly_on_the_deadline_counts_as_met() -> None:
    tickets = [_ticket("T1", "Resolved", -timedelta(days=1), -timedelta(days=1))]

    assert compute_metrics(tickets, now=NOW).sla_compliance_7d_pct == 100.0


def test_resolved_ticket_without_a_due_date_is_excluded_not_assumed_met() -> None:
    tickets = [
        _ticket("T1", "Resolved", resolved_offset=-timedelta(days=1)),
        _ticket("T2", "Resolved", -timedelta(days=2), -timedelta(days=2, hours=1)),
    ]

    metrics = compute_metrics(tickets, now=NOW)

    assert metrics.resolved_last_7d == 2
    assert metrics.sla_measured_last_7d == 1
    assert metrics.sla_compliance_7d_pct == 100.0


def test_zero_denominator_returns_none_rather_than_a_made_up_percentage() -> None:
    """Nothing resolved in 7 days: report "no data", never 0% or 100%."""
    tickets = [
        _ticket("T1", "New", due_offset=timedelta(hours=3)),
        _ticket("T2", "Resolved", -timedelta(days=30), -timedelta(days=29)),
    ]

    metrics = compute_metrics(tickets, now=NOW)

    assert metrics.resolved_last_7d == 0
    assert metrics.sla_measured_last_7d == 0
    assert metrics.sla_compliance_7d_pct is None


def test_naive_ticket_timestamps_are_treated_as_utc() -> None:
    ticket = Ticket(
        status="Resolved",
        sla_resolution_due=datetime(2026, 8, 15, 12, 0),
        resolved_date=datetime(2026, 8, 15, 11, 0),
    )

    assert compute_metrics([ticket], now=NOW).sla_compliance_7d_pct == 100.0


async def test_get_metrics_reads_the_tickets_list() -> None:
    sharepoint = FakeSharePoint(
        tickets=[
            _ticket("T1", "New", due_offset=timedelta(minutes=5)),
            _ticket("T2", "Resolved", -timedelta(days=1), -timedelta(days=1, hours=1)),
        ]
    )

    metrics = await get_metrics(sharepoint, now=NOW)  # type: ignore[arg-type]

    assert metrics.open_tickets == 1
    assert metrics.due_within_30min == 1
    assert metrics.sla_compliance_7d_pct == 100.0
