"""Synthetic metrics for the public demo endpoint.

Everything here is fabricated on purpose and touches no tenant, no Graph call
and no credential. It exists so the shape of the API can be shown to somebody
who has no token, without that person ever seeing a real ticket count.

The payload is deterministic apart from ``generated_at``: a demo that changes
its numbers between two runs of the same curl is a demo that invites the
question "is this live?", and the answer must be an unambiguous no. The
``synthetic`` flag is part of the response body, not a header, so it survives
being pasted into a screenshot.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from app.models import MetricsResponse

__all__ = ["DEMO_NOTICE", "DemoMetricsResponse", "demo_metrics"]

DEMO_NOTICE: Final[str] = (
    "Synthetic sample data. Not from any Microsoft 365 tenant. "
    "The authenticated /metrics endpoint returns live values."
)

# Chosen to exercise the interesting cases: a non-trivial open count, a ticket
# inside the 30-minute window, and a compliance percentage with a visible
# denominator so the number is not mistaken for a rounded marketing figure.
_OPEN_TICKETS: Final[int] = 7
_DUE_WITHIN_30MIN: Final[int] = 2
_RESOLVED_LAST_7D: Final[int] = 12
_SLA_MEASURED_LAST_7D: Final[int] = 11
_SLA_COMPLIANCE_PCT: Final[float] = 90.9  # 10 of 11


class DemoMetricsResponse(MetricsResponse):
    """A :class:`MetricsResponse` that says what it is."""

    synthetic: bool = True
    notice: str = DEMO_NOTICE


def demo_metrics(now: datetime | None = None) -> DemoMetricsResponse:
    """Build the synthetic payload. No I/O, so it cannot fail or leak."""
    return DemoMetricsResponse(
        open_tickets=_OPEN_TICKETS,
        due_within_30min=_DUE_WITHIN_30MIN,
        sla_compliance_7d_pct=_SLA_COMPLIANCE_PCT,
        resolved_last_7d=_RESOLVED_LAST_7D,
        sla_measured_last_7d=_SLA_MEASURED_LAST_7D,
        generated_at=now or datetime.now(UTC),
    )
