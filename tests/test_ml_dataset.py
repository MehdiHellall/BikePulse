from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from bikepulse.data.compaction import OBSERVATION_SCHEMA
from bikepulse.ml.dataset import (
    FEATURES,
    build_dataset,
    canonical_grid,
    feature_rows,
    partition_dataset,
)

START = datetime(2026, 8, 1, tzinfo=UTC)


def history_at(times: list[datetime], counts: list[int], station_id: str = "1") -> pl.DataFrame:
    rows = [
        {
            "snapshot_id": f"poll-{t.isoformat()}",
            "station_id": station_id,
            "collected_at_utc": t - timedelta(seconds=10),
            "received_at_utc": t - timedelta(seconds=5),
            "feed_updated_at_utc": t - timedelta(seconds=10),
            "station_last_reported_utc": t - timedelta(seconds=10),
            "data_age_seconds": 5,
            "is_installed": True,
            "is_renting": True,
            "is_returning": True,
            "available_bikes": count,
            "available_docks": 20 - count,
            "capacity": 20,
            "mechanical_bikes": count,
            "electric_bikes": 0,
            "latitude": 48.85,
            "longitude": 2.35,
        }
        for t, count in zip(times, counts, strict=True)
    ]
    return pl.DataFrame(rows, schema=OBSERVATION_SCHEMA)


def example_grid() -> pl.DataFrame:
    times = [START + timedelta(minutes=i) for i in range(-15, 36, 5)]
    history = history_at(times, [5, 5, 5, 4, 3, 2, 0, 2, 3, 4, 5])
    return canonical_grid(history, times[0], times[-1] + timedelta(minutes=5))


def row_at(frame: pl.DataFrame, stamp: datetime = START) -> dict:
    return frame.filter(pl.col("as_of_utc") == stamp).row(0, named=True)


def test_grid_uses_receipt_not_request_time_and_excludes_legacy() -> None:
    earlier = history_at([START], [4])
    late = history_at([START + timedelta(minutes=5)], [1]).with_columns(
        pl.lit(START + timedelta(seconds=1)).alias("received_at_utc"),
        pl.lit(START - timedelta(seconds=1)).alias("collected_at_utc"),
    )
    grid = canonical_grid(pl.concat([earlier, late]), START, START + timedelta(minutes=10))
    assert row_at(grid)["available_bikes"] == 4
    assert row_at(grid, START + timedelta(minutes=5))["available_bikes"] == 1
    legacy = earlier.with_columns(
        pl.lit(None, dtype=pl.Datetime("us", "UTC")).alias("received_at_utc")
    )
    assert canonical_grid(legacy, START, START + timedelta(minutes=5)).is_empty()


def test_feed_freshness_boundaries_and_unknown_metadata() -> None:
    history = history_at([START], [2])
    for age, usable in [(300, True), (300.001, False), (301, False)]:
        altered = history.with_columns(
            pl.lit(START - timedelta(seconds=age)).alias("feed_updated_at_utc")
        )
        assert (
            row_at(canonical_grid(altered, START, START + timedelta(minutes=5)))["usable"] is usable
        )
    unknown = history.with_columns(pl.lit(None, dtype=pl.Int64).alias("capacity"))
    assert not row_at(canonical_grid(unknown, START, START + timedelta(minutes=5)))["usable"]
    with pytest.raises(ValueError, match="duplicate"):
        canonical_grid(pl.concat([history, history]), START, START + timedelta(minutes=5))


def test_old_station_timestamp_does_not_invalidate_fresh_published_count() -> None:
    history = history_at([START], [2]).with_columns(
        pl.lit(START - timedelta(hours=1)).alias("station_last_reported_utc")
    )

    assert row_at(canonical_grid(history, START, START + timedelta(minutes=5)))["usable"]


def test_missing_or_future_feed_timestamp_is_unusable() -> None:
    history = history_at([START], [2])
    missing = history.with_columns(
        pl.lit(None, dtype=pl.Datetime("us", "UTC")).alias("feed_updated_at_utc")
    )
    future = history.with_columns(pl.lit(START + timedelta(seconds=1)).alias("feed_updated_at_utc"))

    assert not row_at(canonical_grid(missing, START, START + timedelta(minutes=5)))["usable"]
    assert not row_at(canonical_grid(future, START, START + timedelta(minutes=5)))["usable"]


def test_labels_require_every_future_even_after_observed_zero() -> None:
    grid = example_grid()
    assert row_at(build_dataset(grid))["label"] == 1
    positive = grid.with_columns(
        pl.when(pl.col("as_of_utc") == START + timedelta(minutes=25))
        .then(False)
        .otherwise(pl.col("usable"))
        .alias("usable")
    )
    assert row_at(build_dataset(positive))["label"] is None
    missing = grid.filter(pl.col("as_of_utc") != START + timedelta(minutes=25))
    assert row_at(build_dataset(missing))["rejection_reason"] == "unusable_future"
    negative = grid.with_columns(pl.lit(4).alias("available_bikes"))
    assert row_at(build_dataset(negative))["label"] == 0
    assert (
        row_at(build_dataset(grid), START + timedelta(minutes=15))["rejection_reason"]
        == "already_empty"
    )


def test_lags_are_exact_and_future_mutation_cannot_change_features() -> None:
    grid = example_grid()
    features = row_at(feature_rows(grid))
    assert features["bike_change_15m"] == -1
    changed = grid.with_columns(
        pl.when(pl.col("as_of_utc") > START)
        .then(19)
        .otherwise(pl.col("available_bikes"))
        .alias("available_bikes")
    )
    other = row_at(feature_rows(changed))
    assert [features[f] for f in FEATURES] == [other[f] for f in FEATURES]
    no_lag = grid.filter(pl.col("as_of_utc") != START - timedelta(minutes=15))
    assert row_at(feature_rows(no_lag))["feature_rejection"] == "insufficient_history"


def test_station_lags_never_mix_and_calendar_handles_dst() -> None:
    stamp = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
    times = [stamp - timedelta(minutes=15), stamp]
    history = pl.concat([history_at(times, [10, 8], "1"), history_at(times, [1, 4], "2")])
    features = feature_rows(canonical_grid(history, times[0], stamp + timedelta(minutes=5)))
    selected = features.filter(pl.col("as_of_utc") == stamp).sort("station_id")
    assert selected["bike_change_15m"].to_list() == [-2, 3]
    assert selected["weekend"].to_list() == [1, 1]
    assert selected["hour_sin"][0] == pytest.approx(0.5)


def test_partition_purges_by_time_not_station_row_counts() -> None:
    boundary = START + timedelta(days=8)
    times = [boundary - timedelta(minutes=35), boundary - timedelta(minutes=30), boundary]
    data = pl.DataFrame(
        {
            "as_of_utc": times,
            "label_end_utc": [t + timedelta(minutes=30) for t in times],
            "label": [0, 1, 0],
        }
    )
    assert partition_dataset(data, START)["partition"].to_list() == [
        "train",
        "purged",
        "validation",
    ]
