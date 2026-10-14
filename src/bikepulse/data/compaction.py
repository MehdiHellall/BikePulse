from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from bikepulse.collector.schemas import NormalizedStatus, normalize_status_payload
from bikepulse.collector.stations import station_dimension_path
from bikepulse.collector.storage import read_json_gz
from bikepulse.io import atomic_write_parquet, atomic_write_text

UTC_TIME = pl.Datetime("us", "UTC")
OBSERVATION_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "snapshot_id": pl.String,
    "collected_at_utc": UTC_TIME,
    "received_at_utc": UTC_TIME,
    "feed_updated_at_utc": UTC_TIME,
    "station_id": pl.String,
    "station_last_reported_utc": UTC_TIME,
    "data_age_seconds": pl.Int64,
    "is_installed": pl.Boolean,
    "is_renting": pl.Boolean,
    "is_returning": pl.Boolean,
    "available_bikes": pl.Int64,
    "mechanical_bikes": pl.Int64,
    "electric_bikes": pl.Int64,
    "available_docks": pl.Int64,
    "capacity": pl.Int64,
    "latitude": pl.Float64,
    "longitude": pl.Float64,
}


class CompactionContractError(ValueError):
    """A raw partition cannot safely be converted into observations."""


def partition_path(data_dir: Path, hour: datetime, kind: str = "silver") -> Path:
    return data_dir / kind / "observations" / f"date={hour:%Y-%m-%d}" / f"hour={hour:%H}.parquet"


def compact_status_hour(data_dir: Path, hour: datetime) -> pl.DataFrame:
    hour = hour.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    folder = data_dir / "raw" / "status" / f"{hour:%Y/%m/%d/%H}"
    rows: list[dict[str, Any]] = []
    errors = 0
    snapshots = 0
    for path in sorted(folder.glob("*.json.gz")):
        snap_id = path.name.removesuffix(".json.gz")
        collected = datetime.strptime(snap_id.split("-")[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        poll_path = data_dir / "polls" / f"{collected:%Y-%m-%d}" / f"{snap_id}.json"
        poll = json.loads(poll_path.read_text()) if poll_path.exists() else {}
        if poll.get("collected_at_utc"):
            collected = datetime.fromisoformat(poll["collected_at_utc"]).astimezone(UTC)
        receipt = poll.get("received_at_utc")
        received = datetime.fromisoformat(receipt).astimezone(UTC) if receipt else None
        batch = normalize_status_payload(
            read_json_gz(path), snapshot_id=snap_id, collected_at_utc=received or collected
        )
        errors += len(batch.errors)
        snapshots += 1
        for record in batch.rows:
            if isinstance(record, NormalizedStatus):
                row = record.model_dump()
                row["collected_at_utc"] = collected
                row["received_at_utc"] = received
                rows.append(row)
    if rows:
        bronze = pl.DataFrame(rows).cast(
            {key: dtype for key, dtype in OBSERVATION_SCHEMA.items() if key in rows[0]}
        )
        if bronze.select(pl.struct("snapshot_id", "station_id").is_duplicated().any()).item():
            raise CompactionContractError("duplicate (snapshot_id, station_id) in raw history")
        dimension_path = station_dimension_path(data_dir)
        if dimension_path.exists():
            dimension = pl.read_parquet(dimension_path).select(
                "station_id", "valid_from_utc", "capacity", "latitude", "longitude"
            )
            silver = (
                bronze.with_columns(
                    pl.coalesce("received_at_utc", "collected_at_utc").alias("metadata_as_of")
                )
                .sort("metadata_as_of")
                .join_asof(
                    dimension.sort("valid_from_utc"),
                    left_on="metadata_as_of",
                    right_on="valid_from_utc",
                    by="station_id",
                    strategy="backward",
                    check_sortedness=False,
                )
                .drop("metadata_as_of", "valid_from_utc")
            )
        else:
            silver = bronze.with_columns(
                pl.lit(None, dtype=dtype).alias(key)
                for key, dtype in OBSERVATION_SCHEMA.items()
                if key not in bronze.columns
            )
        silver = (
            silver.select(list(OBSERVATION_SCHEMA))
            .cast(pl.Schema(OBSERVATION_SCHEMA))
            .sort("collected_at_utc", "station_id")
        )
    else:
        bronze = pl.DataFrame(schema=OBSERVATION_SCHEMA)
        silver = bronze
    atomic_write_parquet(partition_path(data_dir, hour, "bronze"), bronze)
    atomic_write_parquet(partition_path(data_dir, hour), silver)
    metadata_path = (
        data_dir / "quality" / "compaction" / f"date={hour:%Y-%m-%d}" / f"hour={hour:%H}.json"
    )
    atomic_write_text(
        metadata_path,
        json.dumps(
            {
                "snapshot_count": snapshots,
                "row_count": silver.height,
                "schema_error_count": errors,
                "missing_receipt_count": silver.filter(pl.col("received_at_utc").is_null()).height,
            },
            indent=2,
        ),
    )
    return silver


def rebuild_history(
    data_dir: Path,
    start: datetime | None = None,
    end: datetime | None = None,
) -> None:
    for folder in sorted((data_dir / "raw" / "status").glob("*/*/*/*")):
        if folder.is_dir():
            hour = datetime.strptime(
                folder.relative_to(data_dir / "raw" / "status").as_posix(), "%Y/%m/%d/%H"
            ).replace(tzinfo=UTC)
            if start is not None and hour + timedelta(hours=1) <= start:
                continue
            if end is not None and hour > end:
                continue
            compact_status_hour(data_dir, hour)


def load_history(data_dir: Path, start: datetime, end: datetime) -> pl.DataFrame:
    files = [
        path
        for path in sorted((data_dir / "silver" / "observations").glob("date=*/hour=*.parquet"))
        if start.date() <= datetime.strptime(path.parent.name, "date=%Y-%m-%d").date() <= end.date()
    ]
    if not files:
        return pl.DataFrame(schema=OBSERVATION_SCHEMA)
    return (
        pl.scan_parquet(
            files,
            schema=pl.Schema(OBSERVATION_SCHEMA),
            missing_columns="insert",
            extra_columns="ignore",
        )
        .filter((pl.col("collected_at_utc") >= start) & (pl.col("collected_at_utc") < end))
        .collect()
    )
