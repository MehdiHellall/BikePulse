from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl

from bikepulse.data.compaction import OBSERVATION_SCHEMA

FEATURES = [
    "available_bikes",
    "available_docks",
    "capacity",
    "bike_ratio",
    "bike_change_15m",
    "hour_sin",
    "hour_cos",
    "weekend",
]
FEATURE_SCHEMA_VERSION = "empty-poc-v1"
GRID_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    **OBSERVATION_SCHEMA,
    "as_of_utc": pl.Datetime("us", "UTC"),
    "usable": pl.Boolean,
}


def canonical_grid(
    history: pl.DataFrame,
    start: datetime,
    end: datetime,
    *,
    max_age_seconds: int = 300,
) -> pl.DataFrame:
    """Select the latest whole snapshot known at each boundary, never a future poll."""
    proven = history.filter(pl.col("received_at_utc").is_not_null())
    if proven.is_empty():
        return pl.DataFrame(schema=GRID_SCHEMA)
    if proven.select(pl.struct("snapshot_id", "station_id").is_duplicated().any()).item():
        raise ValueError("duplicate observation keys")
    polls = (
        proven.select("snapshot_id", "received_at_utc")
        .unique()
        .sort("received_at_utc", "snapshot_id")
    )
    slots = pl.DataFrame(
        {
            "as_of_utc": pl.datetime_range(
                start, end, interval="5m", closed="left", eager=True, time_zone="UTC"
            )
        }
    )
    chosen = (
        slots.join_asof(
            polls,
            left_on="as_of_utc",
            right_on="received_at_utc",
            strategy="backward",
            tolerance="5m",
        )
        .drop("received_at_utc")
        .filter(pl.col("snapshot_id").is_not_null())
    )
    grid = chosen.join(proven, on="snapshot_id", how="inner")
    age = (
        pl.col("as_of_utc") - pl.col("station_last_reported_utc")
    ).dt.total_microseconds() / 1_000_000
    return grid.with_columns(
        (
            (age >= 0)
            & (age <= max_age_seconds)
            & (pl.col("received_at_utc") >= pl.col("collected_at_utc"))
            & pl.col("is_installed")
            & pl.col("is_renting")
            & pl.col("is_returning")
            & (pl.col("capacity") > 0)
            & pl.col("available_bikes").is_not_null()
            & pl.col("available_docks").is_not_null()
        )
        .fill_null(False)
        .alias("usable")
    ).sort("as_of_utc", "station_id")


def feature_rows(grid: pl.DataFrame) -> pl.DataFrame:
    """Features depend only on the current and exact t-15 minute observations."""
    lag = grid.select(
        "station_id",
        (pl.col("as_of_utc") + pl.duration(minutes=15)).alias("as_of_utc"),
        pl.col("available_bikes").alias("lag_bikes"),
        pl.col("usable").alias("lag_usable"),
    )
    frame = grid.join(lag, on=["station_id", "as_of_utc"], how="left")
    local = pl.col("as_of_utc").dt.convert_time_zone("Europe/Paris")
    angle = (local.dt.hour() + local.dt.minute() / 60) * (2 * 3.141592653589793 / 24)
    return frame.with_columns(
        (pl.col("available_bikes") / pl.col("capacity")).alias("bike_ratio"),
        (pl.col("available_bikes") - pl.col("lag_bikes")).alias("bike_change_15m"),
        angle.sin().alias("hour_sin"),
        angle.cos().alias("hour_cos"),
        (local.dt.weekday() >= 6).cast(pl.Int8).alias("weekend"),
        pl.when(~pl.col("usable"))
        .then(pl.lit("unusable_current"))
        .when(pl.col("available_bikes") == 0)
        .then(pl.lit("already_empty"))
        .when(~pl.col("lag_usable").fill_null(False))
        .then(pl.lit("insufficient_history"))
        .otherwise(pl.lit(None, dtype=pl.String))
        .alias("feature_rejection"),
    )


def build_dataset(grid: pl.DataFrame) -> pl.DataFrame:
    frame = feature_rows(grid)
    future_columns = []
    for minutes in range(5, 31, 5):
        valid = f"future_usable_{minutes}"
        count = f"future_bikes_{minutes}"
        future = grid.select(
            "station_id",
            (pl.col("as_of_utc") - pl.duration(minutes=minutes)).alias("as_of_utc"),
            pl.col("usable").alias(valid),
            pl.col("available_bikes").alias(count),
        )
        frame = frame.join(future, on=["station_id", "as_of_utc"], how="left")
        future_columns.extend([valid, count])
    complete = pl.all_horizontal(
        pl.col(f"future_usable_{m}").fill_null(False) for m in range(5, 31, 5)
    )
    event = pl.any_horizontal(pl.col(f"future_bikes_{m}") == 0 for m in range(5, 31, 5))
    return frame.with_columns(
        pl.when(pl.col("feature_rejection").is_not_null())
        .then(pl.col("feature_rejection"))
        .when(~complete)
        .then(pl.lit("unusable_future"))
        .otherwise(pl.lit(None, dtype=pl.String))
        .alias("rejection_reason"),
        pl.when(pl.col("feature_rejection").is_null() & complete)
        .then(event.cast(pl.Int8))
        .otherwise(pl.lit(None, dtype=pl.Int8))
        .alias("label"),
        (pl.col("as_of_utc") + pl.duration(minutes=30)).alias("label_end_utc"),
    ).drop(future_columns)


def partition_dataset(dataset: pl.DataFrame, start: datetime) -> pl.DataFrame:
    validation_start = start + timedelta(days=8)
    test_start = start + timedelta(days=11)
    end = start + timedelta(days=14)
    return dataset.with_columns(
        pl.when(pl.col("label").is_null())
        .then(pl.lit("rejected"))
        .when(
            (pl.col("as_of_utc") < validation_start) & (pl.col("label_end_utc") < validation_start)
        )
        .then(pl.lit("train"))
        .when((pl.col("as_of_utc") >= validation_start) & (pl.col("label_end_utc") < test_start))
        .then(pl.lit("validation"))
        .when((pl.col("as_of_utc") >= test_start) & (pl.col("label_end_utc") < end))
        .then(pl.lit("test"))
        .otherwise(pl.lit("purged"))
        .alias("partition")
    )


def utc_midnight(value: str) -> datetime:
    return datetime.combine(date.fromisoformat(value), datetime.min.time(), UTC)
