from __future__ import annotations

from datetime import UTC, datetime

from conftest import load_json

from bikepulse.collector.schemas import normalize_information_payload
from bikepulse.collector.stations import refresh_station_dimension


def test_changed_station_metadata_closes_old_record_and_opens_new(fixture_dir, tmp_path) -> None:
    payload = load_json(fixture_dir / "gbfs_information_valid.json")
    first_batch = normalize_information_payload(payload)
    first_seen = datetime(2026, 8, 9, 3, tzinfo=UTC)

    refresh_station_dimension(
        data_dir=tmp_path, rows=list(first_batch.rows), observed_at_utc=first_seen
    )
    payload["data"]["stations"][0]["capacity"] = 25
    second_batch = normalize_information_payload(payload)
    second_seen = datetime(2026, 8, 10, 3, tzinfo=UTC)

    frame = refresh_station_dimension(
        data_dir=tmp_path, rows=list(second_batch.rows), observed_at_utc=second_seen
    )
    station_one = frame.filter(frame["station_id"] == "1").sort("valid_from_utc")

    assert station_one.height == 2
    assert station_one["is_current"].to_list() == [False, True]
    assert station_one["capacity"].to_list() == [20, 25]
