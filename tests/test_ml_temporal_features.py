from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from bikepulse.data.compaction import OBSERVATION_SCHEMA
from bikepulse.ml.dataset import canonical_grid
from bikepulse.ml.temporal_features import (
    FEATURE_GROUPS,
    NETWORK_FEATURES,
    PRIOR_FEATURES,
    add_fold_station_priors,
    build_signed_network_features,
    build_target_labels,
    build_temporal_features,
)

START = datetime(2026, 9, 1, tzinfo=UTC)


def _history(*, minutes: int = 130, station_count: int = 3) -> pl.DataFrame:
    rows = []
    for offset in range(0, minutes + 1, 5):
        stamp = START + timedelta(minutes=offset)
        for station_index in range(station_count):
            base = 12 - station_index * 2
            bikes = max(0, base - offset // 15 + (station_index if offset % 20 == 0 else 0))
            rows.append(
                {
                    "snapshot_id": f"snapshot-{offset}",
                    "station_id": str(station_index),
                    "collected_at_utc": stamp - timedelta(seconds=10),
                    "received_at_utc": stamp - timedelta(seconds=5),
                    "feed_updated_at_utc": stamp - timedelta(seconds=10),
                    "station_last_reported_utc": stamp - timedelta(seconds=10),
                    "data_age_seconds": 5,
                    "is_installed": True,
                    "is_renting": True,
                    "is_returning": True,
                    "available_bikes": bikes,
                    "available_docks": 20 - bikes,
                    "capacity": 20,
                    "mechanical_bikes": max(0, bikes - 1),
                    "electric_bikes": min(1, bikes),
                    "latitude": 48.85 + station_index * 0.001,
                    "longitude": 2.35,
                }
            )
    return pl.DataFrame(rows, schema=OBSERVATION_SCHEMA)


def _grid(history: pl.DataFrame | None = None) -> pl.DataFrame:
    return canonical_grid(
        history if history is not None else _history(),
        START,
        START + timedelta(minutes=135),
    )


def test_future_mutation_cannot_change_station_or_network_features() -> None:
    boundary = START + timedelta(minutes=60)
    original_grid = _grid()
    changed_grid = original_grid.with_columns(
        pl.when(pl.col("as_of_utc") > boundary)
        .then(pl.col("capacity"))
        .otherwise(pl.col("available_bikes"))
        .alias("available_bikes")
    )

    original_temporal = build_temporal_features(original_grid)
    changed_temporal = build_temporal_features(changed_grid)
    original_network = build_signed_network_features(original_temporal)
    changed_network = build_signed_network_features(changed_temporal)
    station_features = [
        feature for feature in FEATURE_GROUPS["volatility_state"] if feature in original_temporal
    ]
    original_row = original_temporal.filter(pl.col("as_of_utc") == boundary).select(
        "station_id", *station_features
    )
    changed_row = changed_temporal.filter(pl.col("as_of_utc") == boundary).select(
        "station_id", *station_features
    )
    assert original_row.equals(changed_row, null_equal=True)
    assert (
        original_network.filter(pl.col("as_of_utc") == boundary)
        .select("station_id", *NETWORK_FEATURES)
        .equals(
            changed_network.filter(pl.col("as_of_utc") == boundary).select(
                "station_id", *NETWORK_FEATURES
            ),
            null_equal=True,
        )
    )


def test_missing_exact_history_is_flagged_and_not_filled() -> None:
    boundary = START + timedelta(minutes=60)
    history = _history().filter(
        ~pl.col("collected_at_utc").is_between(
            boundary - timedelta(minutes=15, seconds=10),
            boundary - timedelta(minutes=10, seconds=10),
            closed="both",
        )
    )
    row = (
        build_temporal_features(_grid(history))
        .filter((pl.col("station_id") == "0") & (pl.col("as_of_utc") == boundary))
        .row(0, named=True)
    )

    assert row["lag_observed_10m"] == 0
    assert row["lag_bikes_10m"] is None
    assert row["history_complete_15m"] == 0
    assert row["bike_slope_15m"] is None or row["bike_slope_15m"] != row["bike_slope_15m"]


def test_horizon_and_sustained_labels_are_separate_complete_tasks() -> None:
    grid = _grid().with_columns(
        pl.when(
            (pl.col("station_id") == "0")
            & pl.col("as_of_utc").is_in(
                [START + timedelta(minutes=65), START + timedelta(minutes=70)]
            )
        )
        .then(0)
        .otherwise(pl.col("available_bikes"))
        .alias("available_bikes")
    )
    row = (
        build_target_labels(grid)
        .filter(
            (pl.col("station_id") == "0") & (pl.col("as_of_utc") == START + timedelta(minutes=60))
        )
        .row(0, named=True)
    )

    assert row["label_stockout_10m"] == 1
    assert row["label_stockout_15m"] == 1
    assert row["label_stockout_20m"] == 1
    assert row["label_stockout_30m"] == 1
    assert row["label_sustained_30m"] == 1

    missing = grid.filter(pl.col("as_of_utc") != START + timedelta(minutes=70))
    invalid = (
        build_target_labels(missing)
        .filter(
            (pl.col("station_id") == "0") & (pl.col("as_of_utc") == START + timedelta(minutes=60))
        )
        .row(0, named=True)
    )
    assert invalid["label_stockout_10m"] is None
    assert invalid["label_sustained_30m"] is None


def _prior_frames() -> tuple[pl.DataFrame, pl.DataFrame, datetime]:
    validation_start = START + timedelta(minutes=30)
    rows = []
    for offset in (0, 5, 10, 15):
        for station in ("a", "b"):
            rows.append(
                {
                    "station_id": station,
                    "as_of_utc": START + timedelta(minutes=offset),
                    "label_end": START + timedelta(minutes=offset + 5),
                    "label": int(station == "a" and offset in (0, 15)),
                    "bike_ratio": 0.1 + offset / 100 + (0.2 if station == "b" else 0),
                }
            )
    validation = pl.DataFrame(
        {
            "station_id": ["a", "b"],
            "as_of_utc": [validation_start, validation_start],
            "label_end": [validation_start + timedelta(minutes=5)] * 2,
            "label": [0, 1],
            "bike_ratio": [0.2, 0.4],
        }
    )
    return pl.DataFrame(rows), validation, validation_start


def test_station_priors_use_only_known_training_history() -> None:
    train, validation, validation_start = _prior_frames()
    encoded_train, encoded_validation = add_fold_station_priors(
        train,
        validation,
        label_column="label",
        label_end_column="label_end",
        validation_start=validation_start,
    )
    changed_future_training = train.with_columns(
        pl.when(pl.col("as_of_utc") == START + timedelta(minutes=15))
        .then(1 - pl.col("label"))
        .otherwise(pl.col("label"))
        .alias("label")
    )
    other_train, _ = add_fold_station_priors(
        changed_future_training,
        validation,
        label_column="label",
        label_end_column="label_end",
        validation_start=validation_start,
    )
    before_future = pl.col("as_of_utc") <= START + timedelta(minutes=15)
    assert (
        encoded_train.filter(before_future)
        .select(PRIOR_FEATURES)
        .equals(
            other_train.filter(before_future).select(PRIOR_FEATURES),
            null_equal=True,
        )
    )

    changed_validation = validation.with_columns((1 - pl.col("label")).alias("label"))
    _, other_validation = add_fold_station_priors(
        train,
        changed_validation,
        label_column="label",
        label_end_column="label_end",
        validation_start=validation_start,
    )
    assert encoded_validation.select(PRIOR_FEATURES).equals(
        other_validation.select(PRIOR_FEATURES),
        null_equal=True,
    )
