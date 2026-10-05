from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime

import polars as pl
import pytest
from conftest import load_json

from bikepulse.collector.schemas import normalize_information_payload
from bikepulse.collector.stations import refresh_station_dimension
from bikepulse.collector.storage import store_snapshot

compaction = pytest.importorskip(
    "bikepulse.data.compaction",
    reason="M1 compaction is deferred while the station-map POC is active",
)
quality = pytest.importorskip(
    "bikepulse.data.quality",
    reason="M1 quality reporting is deferred while the station-map POC is active",
)
CompactionContractError = compaction.CompactionContractError
compact_status_hour = compaction.compact_status_hour
build_daily_quality_report = quality.build_daily_quality_report


def test_compaction_is_idempotent_and_joins_station_dimension(fixture_dir, tmp_path) -> None:
    info = normalize_information_payload(load_json(fixture_dir / "gbfs_information_valid.json"))
    refresh_station_dimension(
        data_dir=tmp_path,
        rows=list(info.rows),
        observed_at_utc=datetime(2026, 8, 9, 3, tzinfo=UTC),
    )
    payload = load_json(fixture_dir / "gbfs_status_valid.json")
    store_snapshot(
        data_dir=tmp_path,
        kind="status",
        collected_at_utc=datetime(2026, 8, 9, 21, 0, tzinfo=UTC),
        payload=payload,
        content_sha256="c" * 64,
    )

    first = compact_status_hour(tmp_path, datetime(2026, 8, 9, 21, tzinfo=UTC))
    second = compact_status_hour(tmp_path, datetime(2026, 8, 9, 21, tzinfo=UTC))

    assert first.sort("station_id").equals(second.sort("station_id"))
    key_counts = first.group_by(["snapshot_id", "station_id"]).len()
    assert key_counts["len"].max() == 1
    assert first.filter(pl.col("station_id") == "1")["capacity"].item() == 20


def test_silver_compaction_uses_metadata_valid_at_observation_time(fixture_dir, tmp_path) -> None:
    payload = load_json(fixture_dir / "gbfs_information_valid.json")
    first_batch = normalize_information_payload(payload)
    refresh_station_dimension(
        data_dir=tmp_path,
        rows=list(first_batch.rows),
        observed_at_utc=datetime(2026, 8, 9, 3, tzinfo=UTC),
    )
    payload["data"]["stations"][0]["capacity"] = 25
    second_batch = normalize_information_payload(payload)
    refresh_station_dimension(
        data_dir=tmp_path,
        rows=list(second_batch.rows),
        observed_at_utc=datetime(2026, 8, 10, 3, tzinfo=UTC),
    )
    store_snapshot(
        data_dir=tmp_path,
        kind="status",
        collected_at_utc=datetime(2026, 8, 9, 21, 0, tzinfo=UTC),
        payload=load_json(fixture_dir / "gbfs_status_valid.json"),
        content_sha256="9" * 64,
    )

    frame = compact_status_hour(tmp_path, datetime(2026, 8, 9, 21, tzinfo=UTC))

    assert frame.filter(pl.col("station_id") == "1")["capacity"].item() == 20


def test_quality_report_exposes_gaps_staleness_and_station_drift(fixture_dir, tmp_path) -> None:
    info = normalize_information_payload(load_json(fixture_dir / "gbfs_information_valid.json"))
    refresh_station_dimension(
        data_dir=tmp_path,
        rows=list(info.rows),
        observed_at_utc=datetime(2026, 8, 9, 3, tzinfo=UTC),
    )
    payload = load_json(fixture_dir / "gbfs_status_valid.json")
    late_payload = copy.deepcopy(payload)
    late_payload["data"]["stations"] = late_payload["data"]["stations"][:1]
    late_payload["data"]["stations"][0]["last_reported"] = 1786308600
    for collected_at, content_hash, body in [
        (datetime(2026, 8, 9, 21, 0, tzinfo=UTC), "d" * 64, payload),
        (datetime(2026, 8, 9, 21, 20, tzinfo=UTC), "e" * 64, late_payload),
    ]:
        store_snapshot(
            data_dir=tmp_path,
            kind="status",
            collected_at_utc=collected_at,
            payload=body,
            content_sha256=content_hash,
        )

    compact_status_hour(tmp_path, datetime(2026, 8, 9, 21, tzinfo=UTC))
    report = build_daily_quality_report(
        data_dir=tmp_path,
        day=date(2026, 8, 9),
        max_station_age_seconds=300,
        expected_interval_minutes=5,
    )

    assert report["snapshot_count"] == 2
    assert report["gap_count"] == 3
    assert "collection_gaps" in report["warnings"]
    assert "stale_station_data" in report["warnings"]
    assert "station_count_drift" in report["warnings"]
    assert report["storage_bytes"] > 0


def test_quality_report_exposes_sparse_day_boundary_gaps(fixture_dir, tmp_path) -> None:
    info = normalize_information_payload(load_json(fixture_dir / "gbfs_information_valid.json"))
    refresh_station_dimension(
        data_dir=tmp_path,
        rows=list(info.rows),
        observed_at_utc=datetime(2026, 8, 9, 3, tzinfo=UTC),
    )
    store_snapshot(
        data_dir=tmp_path,
        kind="status",
        collected_at_utc=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
        payload=load_json(fixture_dir / "gbfs_status_valid.json"),
        content_sha256="1" * 64,
    )
    compact_status_hour(tmp_path, datetime(2026, 8, 9, 12, tzinfo=UTC))

    report = build_daily_quality_report(
        data_dir=tmp_path,
        day=date(2026, 8, 9),
        max_station_age_seconds=300,
        expected_interval_minutes=5,
    )

    assert report["gap_count"] == 2
    assert report["gaps"][0]["start_utc"] == "2026-08-09T00:00:00+00:00"
    assert "collection_gaps" in report["warnings"]


def test_compaction_fails_on_duplicate_observation_keys(fixture_dir, tmp_path) -> None:
    payload = load_json(fixture_dir / "gbfs_status_valid.json")
    payload["data"]["stations"].append(copy.deepcopy(payload["data"]["stations"][0]))
    store_snapshot(
        data_dir=tmp_path,
        kind="status",
        collected_at_utc=datetime(2026, 8, 9, 21, 0, tzinfo=UTC),
        payload=payload,
        content_sha256="2" * 64,
    )

    with pytest.raises(CompactionContractError):
        compact_status_hour(tmp_path, datetime(2026, 8, 9, 21, tzinfo=UTC))


def test_compaction_metadata_feeds_schema_warnings(fixture_dir, tmp_path) -> None:
    store_snapshot(
        data_dir=tmp_path,
        kind="status",
        collected_at_utc=datetime(2026, 8, 9, 21, 0, tzinfo=UTC),
        payload=load_json(fixture_dir / "gbfs_status_invalid_record.json"),
        content_sha256="3" * 64,
    )
    compact_status_hour(tmp_path, datetime(2026, 8, 9, 21, tzinfo=UTC))

    report = build_daily_quality_report(
        data_dir=tmp_path,
        day=date(2026, 8, 9),
        max_station_age_seconds=300,
        expected_interval_minutes=5,
    )
    metadata = json.loads(
        (tmp_path / "quality" / "compaction" / "date=2026-08-09" / "hour=21.json").read_text(
            encoding="utf-8"
        )
    )

    assert metadata["schema_error_count"] == 1
    assert report["schema_error_count"] == 1
    assert "schema_validation_errors" in report["warnings"]
