from __future__ import annotations

import gzip
import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from bikepulse.collector.storage import raw_snapshot_path
from bikepulse.data.compaction import load_history, rebuild_history
from bikepulse.io import atomic_write_parquet, atomic_write_text
from bikepulse.ml.dataset import FEATURES, build_dataset, canonical_grid, feature_rows

FROZEN_DATASET_SCHEMA_VERSION = "empty-poc-v3-geographic-proxy"
FROZEN_VALIDATION_START = datetime(2026, 10, 13, 15, tzinfo=UTC)
FROZEN_TEST_START = datetime(2026, 10, 14, tzinfo=UTC)
EARTH_RADIUS_METERS = 6_371_000.0
NEIGHBOR_RADIUS_METERS = 1_000.0
NEGATIVES_PER_POSITIVE = 9
SAMPLING_SEED = 42
GEOGRAPHIC_FEATURES = [
    "latitude",
    "longitude",
    "neighbor_station_count_1km",
    "neighbor_capacity_1km",
    "nearby_abs_net_bike_change_15m",
    "nearby_observed_count_15m",
]
FEATURE_DEFINITIONS = {
    "available_bikes": "Published available-bike count at the observation boundary t.",
    "available_docks": "Published available-dock count at t.",
    "capacity": "Station capacity metadata effective when the snapshot was received.",
    "bike_ratio": "available_bikes divided by capacity at t.",
    "bike_change_15m": "Available bikes at t minus the exact usable observation at t-15m.",
    "hour_sin": "Sine encoding of the Europe/Paris local time at t.",
    "hour_cos": "Cosine encoding of the Europe/Paris local time at t.",
    "weekend": "One when t is Saturday or Sunday in Europe/Paris, otherwise zero.",
    "latitude": "Station latitude metadata effective when the snapshot was received.",
    "longitude": "Station longitude metadata effective when the snapshot was received.",
    "neighbor_station_count_1km": (
        "Other stations at t no farther than one kilometre from the station."
    ),
    "neighbor_capacity_1km": "Capacity at t summed over the one-kilometre neighbors.",
    "nearby_abs_net_bike_change_15m": (
        "Sum of absolute t versus t-15m bike-count changes over usable neighbors."
    ),
    "nearby_observed_count_15m": ("Neighbors contributing a usable exact 15-minute change at t."),
}
LABEL_DEFINITION = (
    "One when a non-empty usable station at t has zero available bikes in at least one "
    "usable observation at t+5m through t+30m; zero when all six observations are usable "
    "and none is empty."
)


def _ceil_five_minutes(value: datetime) -> datetime:
    seconds = value.astimezone(UTC).timestamp()
    return datetime.fromtimestamp(math.ceil(seconds / 300) * 300, UTC)


def _status_poll_records(data_dir: Path, cutoff: datetime | None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((data_dir / "polls").glob("*/status-*.json")):
        record: dict[str, Any] = json.loads(path.read_text())
        received_value = record.get("received_at_utc")
        if not isinstance(received_value, str):
            continue
        received = datetime.fromisoformat(received_value).astimezone(UTC)
        if cutoff is None or received <= cutoff:
            records.append({**record, "_received_at": received})
    return sorted(records, key=lambda item: (item["_received_at"], item["snapshot_id"]))


def latest_status_receipt(data_dir: Path) -> datetime:
    records = _status_poll_records(data_dir, None)
    if not records:
        raise ValueError("No status poll receipts are available.")
    received: datetime = records[-1]["_received_at"]
    return received


def _raw_path(data_dir: Path, record: dict[str, Any]) -> Path:
    recorded = record.get("path")
    if isinstance(recorded, str) and Path(recorded).exists():
        return Path(recorded)
    collected = datetime.fromisoformat(record["collected_at_utc"]).astimezone(UTC)
    return raw_snapshot_path(data_dir, "status", collected, record["snapshot_id"])


def _source_fingerprint(
    data_dir: Path, records: list[dict[str, Any]]
) -> tuple[str, list[dict[str, str]]]:
    digest = hashlib.sha256()
    sources: list[dict[str, str]] = []
    for record in records:
        path = _raw_path(data_dir, record)
        if not path.exists():
            raise ValueError(f"Raw snapshot is missing: {record['snapshot_id']}")
        content = gzip.decompress(path.read_bytes())
        content_sha256 = hashlib.sha256(content).hexdigest()
        if content_sha256 != record.get("content_sha256"):
            raise ValueError(f"Raw snapshot checksum mismatch: {record['snapshot_id']}")
        digest.update(record["snapshot_id"].encode())
        digest.update(record["received_at_utc"].encode())
        digest.update(content_sha256.encode())
        sources.append(
            {
                "snapshot_id": record["snapshot_id"],
                "received_at_utc": record["received_at_utc"],
                "content_sha256": content_sha256,
            }
        )
    return digest.hexdigest(), sources


def _neighbor_pairs(features: pl.DataFrame, radius_meters: float) -> pl.DataFrame:
    positions = (
        features.select("station_id", "latitude", "longitude")
        .filter(pl.col("latitude").is_not_null() & pl.col("longitude").is_not_null())
        .unique()
        .sort("station_id", "latitude", "longitude")
    )
    schema = {
        "target_station_id": pl.String,
        "target_latitude": pl.Float64,
        "target_longitude": pl.Float64,
        "neighbor_station_id": pl.String,
        "neighbor_latitude": pl.Float64,
        "neighbor_longitude": pl.Float64,
    }
    if positions.is_empty():
        return pl.DataFrame(schema=schema)
    latitude = np.radians(positions["latitude"].to_numpy())
    longitude = np.radians(positions["longitude"].to_numpy())
    delta_latitude = latitude[:, None] - latitude[None, :]
    delta_longitude = longitude[:, None] - longitude[None, :]
    haversine = np.sin(delta_latitude / 2) ** 2 + (
        np.cos(latitude[:, None]) * np.cos(latitude[None, :]) * np.sin(delta_longitude / 2) ** 2
    )
    distances = EARTH_RADIUS_METERS * 2 * np.arcsin(np.sqrt(np.clip(haversine, 0, 1)))
    station_ids = positions["station_id"].to_numpy()
    target, neighbor = np.where(
        (distances <= radius_meters) & (station_ids[:, None] != station_ids[None, :])
    )
    if not len(target):
        return pl.DataFrame(schema=schema)
    latitudes = positions["latitude"].to_numpy()
    longitudes = positions["longitude"].to_numpy()
    return pl.DataFrame(
        {
            "target_station_id": station_ids[target].tolist(),
            "target_latitude": latitudes[target].tolist(),
            "target_longitude": longitudes[target].tolist(),
            "neighbor_station_id": station_ids[neighbor].tolist(),
            "neighbor_latitude": latitudes[neighbor].tolist(),
            "neighbor_longitude": longitudes[neighbor].tolist(),
        },
        schema=schema,
    )


def _geographic_batch(frame: pl.DataFrame, pairs: pl.DataFrame) -> pl.DataFrame:
    targets = frame.select(
        "as_of_utc",
        pl.col("station_id").alias("target_station_id"),
        pl.col("latitude").alias("target_latitude"),
        pl.col("longitude").alias("target_longitude"),
    )
    neighbors = frame.select(
        "as_of_utc",
        pl.col("station_id").alias("neighbor_station_id"),
        pl.col("latitude").alias("neighbor_latitude"),
        pl.col("longitude").alias("neighbor_longitude"),
        pl.col("capacity").alias("neighbor_capacity"),
        pl.lit(True).alias("neighbor_present"),
        (
            pl.col("usable")
            & pl.col("lag_usable").fill_null(False)
            & pl.col("bike_change_15m").is_not_null()
        ).alias("neighbor_observed"),
        pl.col("bike_change_15m").abs().alias("neighbor_abs_change"),
    )
    candidates = targets.join(
        pairs,
        on=["target_station_id", "target_latitude", "target_longitude"],
        how="left",
    ).join(
        neighbors,
        on=[
            "as_of_utc",
            "neighbor_station_id",
            "neighbor_latitude",
            "neighbor_longitude",
        ],
        how="left",
    )
    observed = pl.col("neighbor_observed").fill_null(False)
    aggregate = candidates.group_by("as_of_utc", "target_station_id").agg(
        pl.col("neighbor_present")
        .fill_null(False)
        .cast(pl.UInt32)
        .sum()
        .alias("neighbor_station_count_1km"),
        pl.col("neighbor_capacity").sum().fill_null(0).alias("neighbor_capacity_1km"),
        pl.when(observed)
        .then(pl.col("neighbor_abs_change"))
        .otherwise(0)
        .sum()
        .alias("nearby_abs_net_bike_change_15m"),
        observed.cast(pl.Int64).sum().alias("nearby_observed_count_15m"),
    )
    result = (
        frame.select("station_id", "as_of_utc", "latitude", "longitude")
        .join(
            aggregate,
            left_on=["as_of_utc", "station_id"],
            right_on=["as_of_utc", "target_station_id"],
            how="left",
        )
        .with_columns(
            pl.when(pl.col("latitude").is_null() | pl.col("longitude").is_null())
            .then(None)
            .otherwise(pl.col(column))
            .alias(column)
            for column in GEOGRAPHIC_FEATURES[2:]
        )
    )
    return result


def geographic_features(
    features: pl.DataFrame, *, radius_meters: float = NEIGHBOR_RADIUS_METERS
) -> pl.DataFrame:
    """Build station-density and nearby stock-movement features known at each boundary."""
    pairs = _neighbor_pairs(features, radius_meters)
    times = features["as_of_utc"].unique().sort().to_list()
    batches = [
        _geographic_batch(
            features.filter(pl.col("as_of_utc").is_in(times[offset : offset + 96])), pairs
        )
        for offset in range(0, len(times), 96)
    ]
    if not batches:
        return pl.DataFrame(
            schema={
                "station_id": pl.String,
                "as_of_utc": pl.Datetime("us", "UTC"),
                "latitude": pl.Float64,
                "longitude": pl.Float64,
                "neighbor_station_count_1km": pl.UInt32,
                "neighbor_capacity_1km": pl.Int64,
                "nearby_abs_net_bike_change_15m": pl.Int64,
                "nearby_observed_count_15m": pl.Int64,
            }
        )
    return pl.concat(batches).sort("as_of_utc", "station_id")


def assign_frozen_splits(
    frame: pl.DataFrame,
    *,
    validation_start: datetime,
    test_start: datetime,
) -> pl.DataFrame:
    if validation_start.utcoffset() != timedelta(0) or test_start.utcoffset() != timedelta(0):
        raise ValueError("Split boundaries must be UTC.")
    if validation_start >= test_start:
        raise ValueError("Validation must start before test.")
    return frame.with_columns(
        pl.when(
            (pl.col("as_of_utc") < validation_start) & (pl.col("label_end_utc") < validation_start)
        )
        .then(pl.lit("train"))
        .when((pl.col("as_of_utc") >= validation_start) & (pl.col("label_end_utc") < test_start))
        .then(pl.lit("validation"))
        .when(pl.col("as_of_utc") >= test_start)
        .then(pl.lit("test"))
        .otherwise(pl.lit("purged"))
        .alias("split")
    )


def enriched_training_view(
    full: pl.DataFrame,
    *,
    negatives_per_positive: int = NEGATIVES_PER_POSITIVE,
    seed: int = SAMPLING_SEED,
) -> pl.DataFrame:
    if negatives_per_positive < 1:
        raise ValueError("At least one negative per positive is required.")
    train = full.filter(pl.col("split") == "train")
    positive = train.filter(pl.col("label") == 1)
    negative = train.filter(pl.col("label") == 0)
    if positive.is_empty():
        raise ValueError("The training split has no positive labels.")
    required = positive.height * negatives_per_positive
    if negative.height < required:
        raise ValueError(
            f"The training split needs {required} negatives for a 1:{negatives_per_positive} view."
        )
    sampled = negative.sample(n=required, shuffle=True, seed=seed)
    return pl.concat([positive, sampled]).sort("as_of_utc", "station_id")


def _gap_ranges(start: datetime, end: datetime, observed: set[datetime]) -> list[dict[str, str]]:
    gaps: list[dict[str, str]] = []
    cursor = start
    gap_start: datetime | None = None
    while cursor <= end:
        if cursor not in observed and gap_start is None:
            gap_start = cursor
        if cursor in observed and gap_start is not None:
            gaps.append({"start_utc": gap_start.isoformat(), "end_utc": cursor.isoformat()})
            gap_start = None
        cursor += timedelta(minutes=5)
    if gap_start is not None:
        gaps.append(
            {
                "start_utc": gap_start.isoformat(),
                "end_utc": (end + timedelta(minutes=5)).isoformat(),
            }
        )
    return gaps


def _count_rows(frame: pl.DataFrame, *columns: str) -> list[dict[str, Any]]:
    return frame.group_by(*columns).len().sort(*columns).to_dicts()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _quality_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Frozen ML dataset quality",
        "",
        f"Source cutoff: {report['cutoff_received_at_utc']}.",
        "",
        "This export preserves observed labels and collection gaps. The enriched training file "
        "is sampled and does not represent real stockout prevalence.",
        "",
        "| Check | Value |",
        "|---|---:|",
        f"| Source snapshots | {report['source_snapshot_count']} |",
        f"| Poll coverage | {report['observed_poll_slots']} / {report['expected_poll_slots']} "
        f"({report['poll_coverage_rate']:.2%}) |",
        f"| Complete labels | {report['complete_label_count']} |",
        f"| Positive labels | {report['positive_label_count']} |",
        f"| Observed prevalence | {report['observed_prevalence']:.2%} |",
        f"| Enriched training rows | {report['enriched_training_row_count']} |",
        f"| Enriched prevalence | {report['enriched_training_prevalence']:.2%} |",
        f"| Training negatives retained | {report['training_negative_sampling_fraction']:.2%} |",
        "",
        "## Split counts",
        "",
        "```json",
        json.dumps(report["split_class_counts"], indent=2),
        "```",
        "",
        "## Collection gaps",
        "",
        "```json",
        json.dumps(report["gaps"], indent=2),
        "```",
        "",
        "## Highest nearby stock-movement proxies",
        "",
        "The proxy sums absolute 15-minute bike-count changes within 1 km. It can include "
        "operator rebalancing and is not a count of rides.",
        "",
        "```json",
        json.dumps(report["activity_proxy_top_areas"], indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def export_frozen_dataset(
    data_dir: Path,
    *,
    cutoff: datetime | None = None,
    validation_start: datetime = FROZEN_VALIDATION_START,
    test_start: datetime = FROZEN_TEST_START,
) -> Path:
    if cutoff is not None and cutoff.utcoffset() != timedelta(0):
        raise ValueError("Cutoff must be UTC.")
    frozen_cutoff = cutoff or latest_status_receipt(data_dir)
    records = _status_poll_records(data_dir, frozen_cutoff)
    if not records:
        raise ValueError("No status snapshots exist at or before the cutoff.")
    source_sha256, sources = _source_fingerprint(data_dir, records)
    first_receipt: datetime = records[0]["_received_at"]
    start_boundary = _ceil_five_minutes(first_receipt)
    final_boundary = _ceil_five_minutes(frozen_cutoff)
    if final_boundary < test_start:
        raise ValueError("The frozen source does not reach the test split.")

    rebuild_history(
        data_dir,
        start_boundary - timedelta(minutes=20),
        final_boundary + timedelta(minutes=5),
    )
    history = load_history(
        data_dir,
        start_boundary - timedelta(minutes=20),
        final_boundary + timedelta(minutes=5),
    ).filter(pl.col("received_at_utc") <= frozen_cutoff)
    grid = canonical_grid(
        history,
        start_boundary - timedelta(minutes=15),
        final_boundary + timedelta(minutes=5),
    )
    features = feature_rows(grid)
    geography = geographic_features(features)
    scoped = (
        build_dataset(grid)
        .filter(
            (pl.col("as_of_utc") >= start_boundary)
            & (pl.col("as_of_utc") <= final_boundary)
            & (pl.col("label_end_utc") <= final_boundary)
        )
        .join(
            geography.drop("latitude", "longitude"),
            on=["station_id", "as_of_utc"],
            how="left",
        )
    )
    full = (
        assign_frozen_splits(
            scoped.filter(pl.col("label").is_not_null()),
            validation_start=validation_start,
            test_start=test_start,
        )
        .select(
            "station_id",
            "as_of_utc",
            "label_end_utc",
            "snapshot_id",
            "label",
            *FEATURES,
            *GEOGRAPHIC_FEATURES,
            "split",
        )
        .sort("as_of_utc", "station_id")
    )
    if full.select(pl.struct("station_id", "as_of_utc").is_duplicated().any()).item():
        raise ValueError("Frozen dataset contains duplicate station-time keys.")
    enriched = enriched_training_view(full)

    config = {
        "schema_version": FROZEN_DATASET_SCHEMA_VERSION,
        "cutoff_received_at_utc": frozen_cutoff.isoformat(),
        "first_boundary_utc": start_boundary.isoformat(),
        "final_boundary_utc": final_boundary.isoformat(),
        "validation_start_utc": validation_start.isoformat(),
        "test_start_utc": test_start.isoformat(),
        "source_snapshot_count": len(records),
        "source_sha256": source_sha256,
        "features": [*FEATURES, *GEOGRAPHIC_FEATURES],
        "feature_definitions": FEATURE_DEFINITIONS,
        "label_definition": LABEL_DEFINITION,
        "neighbor_radius_meters": NEIGHBOR_RADIUS_METERS,
        "negative_sampling_ratio": NEGATIVES_PER_POSITIVE,
        "sampling_seed": SAMPLING_SEED,
    }
    run_id = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
    output = data_dir / "ml" / "exports" / run_id
    full_path = output / "full_labeled.parquet"
    enriched_path = output / "enriched_train.parquet"
    sources_path = output / "sources.json"
    atomic_write_parquet(full_path, full)
    atomic_write_parquet(enriched_path, enriched)
    atomic_write_text(sources_path, json.dumps(sources, indent=2))

    observed_slots = set(
        grid.filter(
            (pl.col("as_of_utc") >= start_boundary) & (pl.col("as_of_utc") <= final_boundary)
        )["as_of_utc"]
        .unique()
        .to_list()
    )
    expected_slots = int((final_boundary - start_boundary).total_seconds() // 300) + 1
    positive = full.filter(pl.col("label") == 1).height
    enriched_positive = enriched.filter(pl.col("label") == 1).height
    training_negative = full.filter((pl.col("split") == "train") & (pl.col("label") == 0)).height
    enriched_negative = enriched.height - enriched_positive
    activity = (
        full.group_by("station_id", "latitude", "longitude")
        .agg(
            pl.len().alias("observation_count"),
            pl.col("nearby_abs_net_bike_change_15m").mean().alias("mean_nearby_abs_change_15m"),
            pl.col("nearby_observed_count_15m").mean().alias("mean_observed_neighbors_15m"),
        )
        .filter(pl.col("mean_nearby_abs_change_15m").is_not_null())
        .sort(
            [
                "mean_nearby_abs_change_15m",
                "station_id",
                "latitude",
                "longitude",
            ],
            descending=[True, False, False, False],
        )
        .head(20)
        .to_dicts()
    )
    report: dict[str, Any] = {
        **config,
        "run_id": run_id,
        "claim": "published-feed stockout dataset; partial temporal coverage",
        "expected_poll_slots": expected_slots,
        "observed_poll_slots": len(observed_slots),
        "poll_coverage_rate": len(observed_slots) / expected_slots,
        "gaps": _gap_ranges(start_boundary, final_boundary, observed_slots),
        "complete_label_count": full.height,
        "positive_label_count": positive,
        "negative_label_count": full.height - positive,
        "observed_prevalence": positive / full.height,
        "split_class_counts": _count_rows(full, "split", "label"),
        "rejection_counts": _count_rows(
            scoped.filter(pl.col("rejection_reason").is_not_null()), "rejection_reason"
        ),
        "enriched_training_row_count": enriched.height,
        "enriched_positive_count": enriched_positive,
        "enriched_negative_count": enriched_negative,
        "enriched_training_prevalence": enriched_positive / enriched.height,
        "training_negative_sampling_fraction": enriched_negative / training_negative,
        "activity_proxy_top_areas": activity,
    }
    atomic_write_text(output / "quality.json", json.dumps(report, indent=2))
    atomic_write_text(output / "quality.md", _quality_markdown(report))
    manifest = {
        **config,
        "run_id": run_id,
        "artifacts": {
            "full_labeled.parquet": _sha256(full_path),
            "enriched_train.parquet": _sha256(enriched_path),
            "sources.json": _sha256(sources_path),
            "quality.json": _sha256(output / "quality.json"),
            "quality.md": _sha256(output / "quality.md"),
        },
    }
    atomic_write_text(output / "manifest.json", json.dumps(manifest, indent=2))
    atomic_write_text(data_dir / "ml" / "latest_export.json", json.dumps({"run_id": run_id}))
    return output
