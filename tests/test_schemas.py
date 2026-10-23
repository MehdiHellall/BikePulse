from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import load_json

from bikepulse.collector.schemas import normalize_information_payload, normalize_status_payload


def test_valid_status_fixture_normalizes_to_contract(fixture_dir) -> None:
    payload = load_json(fixture_dir / "gbfs_status_valid.json")
    batch = normalize_status_payload(
        payload,
        snapshot_id="status-20260809T210000Z-abc",
        collected_at_utc=datetime(2026, 8, 9, 21, 0, tzinfo=UTC),
    )

    assert not batch.errors
    first = batch.rows[0]
    assert first.station_id == "1"
    assert first.available_bikes == 7
    assert first.mechanical_bikes == 5
    assert first.electric_bikes == 2
    assert first.feed_updated_at_utc == datetime(2026, 8, 9, 21, 40, tzinfo=UTC)
    assert first.data_age_seconds == 60


def test_invalid_status_record_produces_typed_error(fixture_dir) -> None:
    payload = load_json(fixture_dir / "gbfs_status_invalid_record.json")
    batch = normalize_status_payload(
        payload,
        snapshot_id="status-20260809T210000Z-abc",
        collected_at_utc=datetime(2026, 8, 9, 21, 0, tzinfo=UTC),
    )

    assert batch.rows == []
    assert batch.errors[0].station_id == "bad"
    assert batch.errors[0].code == "status_validation_error"


def test_information_fixture_normalizes_to_station_dimension_rows(fixture_dir) -> None:
    payload = load_json(fixture_dir / "gbfs_information_valid.json")
    batch = normalize_information_payload(payload)

    assert not batch.errors
    assert batch.rows[0].station_id == "1"
    assert batch.rows[0].capacity == 20


def test_current_vendor_aliases_and_numeric_station_ids_are_normalized() -> None:
    payload = {
        "lastUpdatedOther": 1791224671,
        "ttl": 60,
        "data": {
            "stations": [
                {
                    "station_id": 213688169,
                    "name": "Benjamin Godard - Victor Hugo",
                    "lat": 48.865983,
                    "lon": 2.275725,
                    "capacity": 35,
                }
            ]
        },
    }

    batch = normalize_information_payload(payload)

    assert not batch.errors
    assert batch.rows[0].station_id == "213688169"


@pytest.mark.parametrize("malformed", [None, 17, "station", ["not", "an", "object"]])
def test_non_object_status_member_is_isolated(fixture_dir, malformed: object) -> None:
    payload = load_json(fixture_dir / "gbfs_status_valid.json")
    payload["data"]["stations"].insert(1, malformed)

    batch = normalize_status_payload(
        payload,
        snapshot_id="status-20260809T210000Z-abc",
        collected_at_utc=datetime(2026, 8, 9, 21, 0, tzinfo=UTC),
    )

    assert [row.station_id for row in batch.rows] == ["1", "2"]
    assert len(batch.errors) == 1
    assert batch.errors[0].station_id == "unknown"
    assert batch.errors[0].code == "status_validation_error"


@pytest.mark.parametrize("malformed", [None, 17, "station", ["not", "an", "object"]])
def test_non_object_information_member_is_isolated(fixture_dir, malformed: object) -> None:
    payload = load_json(fixture_dir / "gbfs_information_valid.json")
    payload["data"]["stations"].insert(1, malformed)

    batch = normalize_information_payload(payload)

    assert [row.station_id for row in batch.rows] == ["1", "2"]
    assert len(batch.errors) == 1
    assert batch.errors[0].station_id == "unknown"
    assert batch.errors[0].code == "information_validation_error"
