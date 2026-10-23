from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from bikepulse.collector.schemas import NormalizedInformation
from bikepulse.collector.stations import refresh_station_dimension
from bikepulse.collector.storage import store_snapshot, write_poll_record
from bikepulse.data.compaction import OBSERVATION_SCHEMA
from bikepulse.ml.dataset import canonical_grid, feature_rows
from bikepulse.ml.export import (
    EARTH_RADIUS_METERS,
    assign_frozen_splits,
    enriched_training_view,
    export_frozen_dataset,
    geographic_features,
)

START = datetime(2026, 8, 1, 10, tzinfo=UTC)


def _history(
    times: list[datetime],
    *,
    move_neighbor_at: datetime | None = None,
    future_neighbor_count: int = 2,
) -> pl.DataFrame:
    near_delta = math.degrees(999 / EARTH_RADIUS_METERS)
    far_delta = math.degrees(1001 / EARTH_RADIUS_METERS)
    counts = {
        "target": [10, 8, 7],
        "near": [7, 4, future_neighbor_count],
        "far": [6, 1, 0],
        "unknown": [5, 3, 2],
    }
    rows = []
    for index, stamp in enumerate(times):
        for station_id, latitude in [
            ("target", 48.85),
            ("near", 48.85 + near_delta),
            ("far", 48.85 + far_delta),
            ("unknown", None),
        ]:
            if station_id == "near" and stamp == move_neighbor_at:
                latitude = 48.88
            count = counts[station_id][index]
            rows.append(
                {
                    "snapshot_id": f"poll-{stamp.isoformat()}",
                    "station_id": station_id,
                    "collected_at_utc": stamp - timedelta(seconds=10),
                    "received_at_utc": stamp - timedelta(seconds=5),
                    "feed_updated_at_utc": stamp - timedelta(seconds=10),
                    "station_last_reported_utc": stamp - timedelta(seconds=10),
                    "data_age_seconds": 5,
                    "is_installed": True,
                    "is_renting": True,
                    "is_returning": True,
                    "available_bikes": count,
                    "available_docks": 20 - count,
                    "capacity": 20,
                    "mechanical_bikes": count,
                    "electric_bikes": 0,
                    "latitude": latitude,
                    "longitude": 2.35 if latitude is not None else None,
                }
            )
    return pl.DataFrame(rows, schema=OBSERVATION_SCHEMA)


def _geographic_row(history: pl.DataFrame, stamp: datetime, station_id: str) -> dict:
    grid = canonical_grid(history, stamp - timedelta(minutes=15), stamp + timedelta(minutes=10))
    return (
        geographic_features(feature_rows(grid))
        .filter((pl.col("as_of_utc") == stamp) & (pl.col("station_id") == station_id))
        .row(0, named=True)
    )


def test_geographic_features_respect_radius_versions_and_future_isolation() -> None:
    stamp = START + timedelta(minutes=15)
    times = [stamp - timedelta(minutes=15), stamp, stamp + timedelta(minutes=5)]
    row = _geographic_row(_history(times), stamp, "target")
    assert row["neighbor_station_count_1km"] == 1
    assert row["neighbor_capacity_1km"] == 20
    assert row["nearby_observed_count_15m"] == 1
    assert row["nearby_abs_net_bike_change_15m"] == 3

    moved = _geographic_row(_history(times, move_neighbor_at=stamp), stamp, "target")
    assert moved["neighbor_station_count_1km"] == 0
    assert moved["nearby_observed_count_15m"] == 0

    future_changed = _geographic_row(_history(times, future_neighbor_count=19), stamp, "target")
    assert future_changed == row
    unknown = _geographic_row(_history(times), stamp, "unknown")
    assert unknown["neighbor_station_count_1km"] is None
    assert unknown["nearby_abs_net_bike_change_15m"] is None


def test_frozen_splits_purge_boundaries_and_enrichment_is_deterministic() -> None:
    validation = START + timedelta(hours=2)
    test = START + timedelta(hours=4)
    times = [
        validation - timedelta(minutes=35),
        validation - timedelta(minutes=30),
        validation,
        test - timedelta(minutes=30),
        test,
    ]
    split_input = pl.DataFrame(
        {
            "as_of_utc": times,
            "label_end_utc": [stamp + timedelta(minutes=30) for stamp in times],
        }
    )
    assert assign_frozen_splits(split_input, validation_start=validation, test_start=test)[
        "split"
    ].to_list() == ["train", "purged", "validation", "purged", "test"]

    training = pl.DataFrame(
        {
            "station_id": [str(index) for index in range(53)],
            "as_of_utc": [START + timedelta(minutes=index) for index in range(53)],
            "label": [1] * 3 + [0] * 50,
            "split": ["train"] * 53,
        }
    )
    first = enriched_training_view(training)
    second = enriched_training_view(training)
    assert first.equals(second)
    assert first.filter(pl.col("label") == 1).height == 3
    assert first.filter(pl.col("label") == 0).height == 27


def _write_export_fixture(data_dir: Path) -> datetime:
    station_ids = [str(index) for index in range(11)]
    refresh_station_dimension(
        data_dir=data_dir,
        observed_at_utc=START - timedelta(hours=1),
        rows=[
            NormalizedInformation(
                station_id=station_id,
                name=f"Station {station_id}",
                capacity=20,
                latitude=48.85 + int(station_id) * 0.0001,
                longitude=2.35,
            )
            for station_id in station_ids
        ],
    )
    last_receipt = START
    pattern = [5, 5, 5, 4, 3, 2, 0, 2, 3, 4, 5]
    for block_start in (START, START + timedelta(hours=2), START + timedelta(hours=4)):
        for tick, positive_count in enumerate(pattern):
            boundary = block_start + timedelta(minutes=tick * 5)
            collected = boundary - timedelta(seconds=10)
            received = boundary - timedelta(seconds=5)
            payload = {
                "last_updated": int(collected.timestamp()),
                "ttl": 60,
                "data": {
                    "stations": [
                        {
                            "station_id": station_id,
                            "is_installed": 1,
                            "is_renting": 1,
                            "is_returning": 1,
                            "num_bikes_available": positive_count if station_id == "0" else 10,
                            "num_docks_available": 20
                            - (positive_count if station_id == "0" else 10),
                            "last_reported": int(collected.timestamp()),
                        }
                        for station_id in station_ids
                    ]
                },
            }
            content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            stored = store_snapshot(
                data_dir=data_dir,
                kind="status",
                collected_at_utc=collected,
                received_at_utc=received,
                payload=payload,
                content_sha256=hashlib.sha256(content).hexdigest(),
                content=content,
            )
            write_poll_record(
                data_dir=data_dir,
                stored=stored,
                url="http://fixture/status",
                latency_ms=1,
                attempts=1,
                status_code=200,
                valid_record_count=len(station_ids),
                error_count=0,
                error_codes=[],
            )
            last_receipt = received
    return last_receipt


def test_frozen_export_writes_reproducible_full_and_enriched_artifacts(tmp_path: Path) -> None:
    cutoff = _write_export_fixture(tmp_path)
    validation = START + timedelta(hours=2)
    test = START + timedelta(hours=4)
    output = export_frozen_dataset(
        tmp_path,
        cutoff=cutoff,
        validation_start=validation,
        test_start=test,
    )
    full = pl.read_parquet(output / "full_labeled.parquet")
    enriched = pl.read_parquet(output / "enriched_train.parquet")
    manifest = json.loads((output / "manifest.json").read_text())
    report = json.loads((output / "quality.json").read_text())

    assert set(full["split"]) == {"train", "validation", "test"}
    class_counts = full.group_by("split").agg(pl.col("label").n_unique().alias("classes"))
    assert dict(class_counts.iter_rows()) == {"train": 2, "validation": 2, "test": 2}
    positives = enriched.filter(pl.col("label") == 1).height
    assert enriched.filter(pl.col("label") == 0).height == positives * 9
    assert manifest["cutoff_received_at_utc"] == cutoff.isoformat()
    assert manifest["source_snapshot_count"] == 33
    assert len(json.loads((output / "sources.json").read_text())) == 33
    assert report["complete_label_count"] == full.height
    assert report["gaps"]
    assert "unusable_future" in {count["rejection_reason"] for count in report["rejection_counts"]}
    first_hashes = manifest["artifacts"]

    earlier_cutoff = cutoff - timedelta(minutes=5)
    earlier = export_frozen_dataset(
        tmp_path,
        cutoff=earlier_cutoff,
        validation_start=validation,
        test_start=test,
    )
    earlier_manifest = json.loads((earlier / "manifest.json").read_text())
    earlier_full = pl.read_parquet(earlier / "full_labeled.parquet")
    assert earlier_manifest["source_snapshot_count"] == 32
    assert earlier_full["label_end_utc"].max() <= START + timedelta(hours=4, minutes=45)

    repeated = export_frozen_dataset(
        tmp_path,
        cutoff=cutoff,
        validation_start=validation,
        test_start=test,
    )
    assert repeated == output
    assert json.loads((repeated / "manifest.json").read_text())["artifacts"] == first_hashes
