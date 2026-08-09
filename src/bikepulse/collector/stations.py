from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from bikepulse.collector.schemas import NormalizedInformation
from bikepulse.io import atomic_write_parquet

STATION_DIM_COLUMNS = [
    "station_id",
    "name",
    "latitude",
    "longitude",
    "capacity",
    "valid_from_utc",
    "valid_to_utc",
    "is_current",
]


def station_dimension_path(data_dir: Path) -> Path:
    return data_dir / "silver" / "station_dimension.parquet"


def refresh_station_dimension(
    *, data_dir: Path, rows: list[NormalizedInformation], observed_at_utc: datetime
) -> pl.DataFrame:
    path = station_dimension_path(data_dir)
    current = _read_dimension(path)
    incoming = _incoming_frame(rows, observed_at_utc)
    if current.is_empty():
        updated = incoming
    else:
        updated = _merge_dimension(current, incoming, observed_at_utc)
    atomic_write_parquet(path, updated)
    return updated


def _read_dimension(path: Path) -> pl.DataFrame:
    if not path.exists():
        return _empty_dimension()
    return pl.read_parquet(path)


def _empty_dimension() -> pl.DataFrame:
    return pl.DataFrame(schema=dict.fromkeys(STATION_DIM_COLUMNS, pl.Utf8)).with_columns(
        pl.col("latitude").cast(pl.Float64),
        pl.col("longitude").cast(pl.Float64),
        pl.col("capacity").cast(pl.Int64),
        pl.col("valid_from_utc").cast(pl.Datetime(time_zone="UTC")),
        pl.col("valid_to_utc").cast(pl.Datetime(time_zone="UTC")),
        pl.col("is_current").cast(pl.Boolean),
    )


def _incoming_frame(rows: list[NormalizedInformation], observed_at_utc: datetime) -> pl.DataFrame:
    dictionaries = [
        {
            "station_id": row.station_id,
            "name": row.name,
            "latitude": row.latitude,
            "longitude": row.longitude,
            "capacity": row.capacity,
            "valid_from_utc": observed_at_utc.astimezone(UTC),
            "valid_to_utc": None,
            "is_current": True,
        }
        for row in rows
    ]
    if not dictionaries:
        return _empty_dimension()
    return pl.DataFrame(dictionaries, schema=STATION_DIM_COLUMNS).with_columns(
        pl.col("latitude").cast(pl.Float64),
        pl.col("longitude").cast(pl.Float64),
        pl.col("capacity").cast(pl.Int64),
        pl.col("valid_from_utc").cast(pl.Datetime(time_zone="UTC")),
        pl.col("valid_to_utc").cast(pl.Datetime(time_zone="UTC")),
        pl.col("is_current").cast(pl.Boolean),
    )


def _merge_dimension(
    current: pl.DataFrame, incoming: pl.DataFrame, observed_at_utc: datetime
) -> pl.DataFrame:
    if incoming.is_empty():
        return current
    active = current.filter(pl.col("is_current"))
    historical = current.filter(~pl.col("is_current"))
    comparable = ["name", "latitude", "longitude", "capacity"]
    changed_ids: set[str] = set()
    active_by_id = {row["station_id"]: row for row in active.to_dicts()}
    for row in incoming.to_dicts():
        old = active_by_id.get(row["station_id"])
        if old is None or any(old[column] != row[column] for column in comparable):
            changed_ids.add(row["station_id"])
    closed = active.with_columns(
        pl.when(pl.col("station_id").is_in(changed_ids))
        .then(pl.lit(observed_at_utc.astimezone(UTC)))
        .otherwise(pl.col("valid_to_utc"))
        .alias("valid_to_utc"),
        pl.when(pl.col("station_id").is_in(changed_ids))
        .then(pl.lit(False))
        .otherwise(pl.col("is_current"))
        .alias("is_current"),
    )
    additions = incoming.filter(pl.col("station_id").is_in(changed_ids))
    unchanged_current = closed.filter(pl.col("is_current"))
    closed_history = closed.filter(~pl.col("is_current"))
    return pl.concat(
        [historical, closed_history, unchanged_current, additions],
        how="diagonal_relaxed",
    )
