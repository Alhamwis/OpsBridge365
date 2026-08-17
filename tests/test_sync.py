"""Sync job: matching, and the honesty rule that unknown means "Unknown"."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.models import UNKNOWN, AssetItem, GraphDevice, GraphUser
from app.sync import build_asset_index, main, resolve_fields, run_sync
from tests.fakes import FakeGraph, FakeSharePoint

ADA = GraphUser(id="user-1", displayName="Ada Lovelace", userPrincipalName="ada@contoso.example")


def _device(**overrides: object) -> GraphDevice:
    payload: dict = {
        "id": "device-1",
        "displayName": "LAP-0001",
        "serialNumber": "SN-12345",
        "isCompliant": True,
        "approximateLastSignInDateTime": "2026-08-16T09:30:00Z",
        "registeredOwners": [{"id": "user-1", "userPrincipalName": "ada@contoso.example"}],
    }
    payload.update(overrides)
    return GraphDevice.from_graph(payload)


def _asset(item_id: str, **fields: object) -> AssetItem:
    return AssetItem.from_list_item({"id": item_id, "fields": fields})


# 4. Device-to-asset matching, happy path --------------------------------------


async def test_sync_matches_on_serial_number_and_patches_all_three_columns() -> None:
    assets = [_asset("12", Title="LAP-0001", SerialNumber="sn-12345", DeviceName="OTHER-NAME")]
    sharepoint = FakeSharePoint(FakeGraph([ADA], [_device()]), assets)

    result = await run_sync(sharepoint)

    assert result.matched == 1
    assert result.patched == 1
    assert result.unmatched_devices == []
    item_id, fields = sharepoint.patches[0]
    assert item_id == "12"
    assert fields == {
        "AssignedUser": "Ada Lovelace",
        "ComplianceStatus": "Compliant",
        "LastCheckIn": "2026-08-16T09:30:00Z",
    }


async def test_sync_falls_back_to_device_name_when_serial_is_absent() -> None:
    assets = [_asset("20", Title="LAP-0002", DeviceName="lap-0002")]
    device = _device(id="device-2", displayName="LAP-0002", serialNumber=None, isCompliant=False)
    sharepoint = FakeSharePoint(FakeGraph([ADA], [device]), assets)

    result = await run_sync(sharepoint)

    assert result.matched == 1
    assert sharepoint.patches[0][0] == "20"
    assert sharepoint.patches[0][1]["ComplianceStatus"] == "NonCompliant"


async def test_serial_number_wins_over_device_name() -> None:
    by_serial = _asset("30", SerialNumber="SN-12345")
    by_name = _asset("31", DeviceName="LAP-0001")
    sharepoint = FakeSharePoint(FakeGraph([ADA], [_device()]), [by_serial, by_name])

    await run_sync(sharepoint)

    assert [item_id for item_id, _ in sharepoint.patches] == ["30"]


# 5. Unknown-device honesty ----------------------------------------------------


async def test_unmatched_device_is_reported_and_never_patched() -> None:
    assets = [_asset("12", Title="LAP-0001", SerialNumber="SN-OTHER", DeviceName="LAP-OTHER")]
    device = _device(id="device-9", displayName="LAP-9999", serialNumber="SN-99999")
    sharepoint = FakeSharePoint(FakeGraph([ADA], [device]), assets)

    result = await run_sync(sharepoint)

    assert result.matched == 0
    assert result.patched == 0
    assert sharepoint.patches == []
    assert result.unmatched_devices == ["LAP-9999"]


async def test_unknown_user_and_compliance_are_written_as_the_literal_string() -> None:
    """The load-bearing rule: never guess. Write "Unknown"."""
    assets = [_asset("12", SerialNumber="SN-12345")]
    device = _device(registeredOwners=[], isCompliant=None, approximateLastSignInDateTime=None)
    sharepoint = FakeSharePoint(FakeGraph([], [device]), assets)

    result = await run_sync(sharepoint)

    _, fields = sharepoint.patches[0]
    assert fields["AssignedUser"] == "Unknown"
    assert fields["ComplianceStatus"] == "Unknown"
    assert fields["AssignedUser"] == UNKNOWN and fields["ComplianceStatus"] == UNKNOWN
    # An unknown check-in time is left untouched, not stamped with a made-up date.
    assert "LastCheckIn" not in fields
    assert result.unknown_assigned_user == 1
    assert result.unknown_compliance == 1
    assert result.unknown_last_check_in == 1


async def test_owner_not_present_in_the_directory_stays_unknown() -> None:
    """A device owner Graph cannot resolve to a known user is not approximated."""
    device = _device(
        registeredOwners=[{"id": "user-ghost", "userPrincipalName": "ghost@contoso.example"}]
    )
    sharepoint = FakeSharePoint(FakeGraph([ADA], [device]), [_asset("12", SerialNumber="SN-12345")])

    await run_sync(sharepoint)

    assert sharepoint.patches[0][1]["AssignedUser"] == UNKNOWN


async def test_ambiguous_serial_matches_nothing() -> None:
    """Two rows with the same serial: a wrong match is worse than no match."""
    assets = [_asset("40", SerialNumber="SN-12345"), _asset("41", SerialNumber="sn-12345")]
    sharepoint = FakeSharePoint(FakeGraph([ADA], [_device(displayName=None)]), assets)

    result = await run_sync(sharepoint)

    assert sharepoint.patches == []
    assert result.unmatched_devices == ["SN-12345"]


def test_resolve_fields_is_pure_and_honest() -> None:
    fields = resolve_fields(_device(isCompliant=False), {"ada@contoso.example": ADA})

    assert fields == {
        "AssignedUser": "Ada Lovelace",
        "ComplianceStatus": "NonCompliant",
        "LastCheckIn": "2026-08-16T09:30:00Z",
    }


def test_naive_timestamps_are_treated_as_utc() -> None:
    device = GraphDevice(id="device-3", approximateLastSignInDateTime=datetime(2026, 8, 16, 9, 30))

    assert resolve_fields(device, {})["LastCheckIn"] == "2026-08-16T09:30:00Z"


def test_build_asset_index_drops_ambiguous_keys() -> None:
    index = build_asset_index(
        [
            _asset("1", SerialNumber="SN-1", DeviceName="LAP-1"),
            _asset("2", SerialNumber="SN-1", DeviceName="LAP-2"),
            _asset("3", SerialNumber="", DeviceName=None),
        ]
    )

    assert "sn-1" in index.ambiguous_serials
    assert "sn-1" not in index.by_serial
    assert set(index.by_name) == {"lap-1", "lap-2"}


async def test_patch_failure_is_recorded_without_aborting_the_run() -> None:
    assets = [_asset("12", SerialNumber="SN-12345"), _asset("13", SerialNumber="SN-67890")]
    devices = [_device(), _device(id="device-2", displayName="LAP-0002", serialNumber="SN-67890")]
    sharepoint = FakeSharePoint(FakeGraph([ADA], devices), assets)
    sharepoint.fail_item_ids = {"12"}

    result = await run_sync(sharepoint)

    assert result.matched == 2
    assert result.patched == 1
    assert len(result.errors) == 1
    assert result.ok is False


async def test_result_serialises_as_json_for_the_job_log() -> None:
    assets = [_asset("12", SerialNumber="SN-12345")]
    sharepoint = FakeSharePoint(FakeGraph([ADA], [_device()]), assets)

    result = await run_sync(sharepoint)
    payload = json.loads(result.model_dump_json())

    assert payload["patched"] == 1
    assert payload["devices_fetched"] == 1
    assert payload["errors"] == []


def test_main_exits_with_a_config_error_code_and_no_stdout_summary(
    clean_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """``python -m app.sync`` with no configuration fails loudly, not silently."""
    exit_code = main()
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out == ""
    assert "AZURE_TENANT_ID" in captured.err
