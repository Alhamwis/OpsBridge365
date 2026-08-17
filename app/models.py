"""Pydantic models for Graph payloads, SharePoint rows, and API responses.

``UNKNOWN`` is the project's honesty marker: when the sync cannot confidently
determine a value it writes this literal string rather than guessing. A wrong
value in the Assets list is worse than an admitted gap.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "UNKNOWN",
    "AssetItem",
    "GraphDevice",
    "GraphUser",
    "MetricsResponse",
    "SyncResult",
    "Ticket",
]

UNKNOWN: Final[str] = "Unknown"

_SERIAL_PHYSICAL_ID_PREFIX: Final[str] = "[SerialNumber]:"


class _GraphModel(BaseModel):
    """Base for models parsed straight from Graph JSON (camelCase in, snake_case out)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class GraphUser(_GraphModel):
    """A user from ``/users``."""

    id: str
    display_name: str | None = Field(default=None, alias="displayName")
    user_principal_name: str | None = Field(default=None, alias="userPrincipalName")
    mail: str | None = None
    account_enabled: bool | None = Field(default=None, alias="accountEnabled")

    @property
    def label(self) -> str:
        """Best human-readable identifier, or ``UNKNOWN`` when the user has neither."""
        return self.display_name or self.user_principal_name or UNKNOWN


class GraphDevice(_GraphModel):
    """A device from ``/devices``."""

    id: str
    device_id: str | None = Field(default=None, alias="deviceId")
    display_name: str | None = Field(default=None, alias="displayName")
    operating_system: str | None = Field(default=None, alias="operatingSystem")
    operating_system_version: str | None = Field(default=None, alias="operatingSystemVersion")
    is_compliant: bool | None = Field(default=None, alias="isCompliant")
    approximate_last_sign_in: datetime | None = Field(
        default=None, alias="approximateLastSignInDateTime"
    )
    serial_number: str | None = Field(default=None, alias="serialNumber")
    registered_owner_upn: str | None = None

    @classmethod
    def from_graph(cls, payload: dict[str, Any]) -> GraphDevice:
        """Build a device from a raw Graph object.

        Handles the two places a serial number can hide (a ``serialNumber``
        property, or a ``[SerialNumber]:`` entry inside ``physicalIds``) and
        pulls the first registered owner when the caller expanded them.
        """
        data = dict(payload)
        if not data.get("serialNumber"):
            serial = _serial_from_physical_ids(data.get("physicalIds"))
            if serial:
                data["serialNumber"] = serial
        owners = data.get("registeredOwners")
        if isinstance(owners, list) and owners:
            first = owners[0]
            if isinstance(first, dict):
                data["registered_owner_upn"] = first.get("userPrincipalName") or first.get("id")
        return cls.model_validate(data)


def _serial_from_physical_ids(physical_ids: Any) -> str | None:
    """Pull ``[SerialNumber]:ABC123`` out of Graph's ``physicalIds`` list."""
    if not isinstance(physical_ids, list):
        return None
    for entry in physical_ids:
        if isinstance(entry, str) and entry.startswith(_SERIAL_PHYSICAL_ID_PREFIX):
            value = entry[len(_SERIAL_PHYSICAL_ID_PREFIX) :].strip()
            if value:
                return value
    return None


class AssetItem(BaseModel):
    """One row of the SharePoint Assets list (``Title`` holds the asset tag)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    title: str | None = None
    serial_number: str | None = None
    device_name: str | None = None
    assigned_user: str | None = None
    compliance_status: str | None = None
    last_check_in: datetime | None = None

    @classmethod
    def from_list_item(cls, item: dict[str, Any]) -> AssetItem:
        """Build an asset from a Graph ``listItem`` with ``fields`` expanded."""
        fields = item.get("fields") or {}
        if not isinstance(fields, dict):
            fields = {}
        return cls.model_validate(
            {
                "id": str(item.get("id", "")),
                "title": fields.get("Title"),
                "serial_number": fields.get("SerialNumber"),
                "device_name": fields.get("DeviceName"),
                "assigned_user": fields.get("AssignedUser"),
                "compliance_status": fields.get("ComplianceStatus"),
                "last_check_in": fields.get("LastCheckIn") or None,
            }
        )


class SyncResult(BaseModel):
    """Summary printed as JSON when the sync job exits."""

    users_fetched: int = 0
    devices_fetched: int = 0
    assets_fetched: int = 0
    matched: int = 0
    patched: int = 0
    unmatched_devices: list[str] = Field(default_factory=list)
    unknown_assigned_user: int = 0
    unknown_compliance: int = 0
    unknown_last_check_in: int = 0
    errors: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing failed; drives the job's exit code."""
        return not self.errors


class Ticket(BaseModel):
    """One row of the SharePoint Tickets list, reduced to the SLA fields."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str | None = None
    ticket_id: str | None = None
    status: str | None = None
    sla_resolution_due: datetime | None = None
    resolved_date: datetime | None = None

    @classmethod
    def from_list_item(cls, item: dict[str, Any]) -> Ticket:
        """Build a ticket from a Graph ``listItem`` with ``fields`` expanded."""
        fields = item.get("fields") or {}
        if not isinstance(fields, dict):
            fields = {}
        return cls.model_validate(
            {
                "id": str(item.get("id", "")) or None,
                "ticket_id": fields.get("TicketID"),
                "status": fields.get("Status"),
                "sla_resolution_due": fields.get("SLAResolutionDue") or None,
                "resolved_date": fields.get("ResolvedDate") or None,
            }
        )


class MetricsResponse(BaseModel):
    """Live service-desk numbers returned by ``GET /metrics``.

    ``sla_compliance_7d_pct`` is ``null`` when nothing was resolved in the
    window - an honest "no data" instead of a misleading 0% or 100%.
    """

    open_tickets: int
    due_within_30min: int
    sla_compliance_7d_pct: float | None
    resolved_last_7d: int
    sla_measured_last_7d: int
    generated_at: datetime
