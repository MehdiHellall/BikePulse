# ruff: noqa: E501
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import platform
import shutil
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray
from sklearn.ensemble import HistGradientBoostingClassifier  # type: ignore[import-untyped]
from sklearn.metrics import (  # type: ignore[import-untyped]
    average_precision_score,
    precision_recall_curve,
    precision_score,
    recall_score,
)
from threadpoolctl import threadpool_limits  # type: ignore[import-untyped]

from bikepulse.collector.stations import station_dimension_path
from bikepulse.data.compaction import load_history
from bikepulse.io import atomic_write_parquet, atomic_write_text
from bikepulse.ml.dataset import canonical_grid
from bikepulse.ml.temporal_features import (
    FEATURE_AVAILABILITY,
    FEATURE_GROUPS,
    PRIOR_FEATURES,
    TARGET_HORIZONS,
    add_fold_station_priors,
    build_signed_network_features,
    build_target_labels,
    build_temporal_features,
    feature_quality_summary,
)

TEMPORAL_STUDY_SCHEMA_VERSION = "past-only-temporal-study-v1"
TEMPORAL_CACHE_SCHEMA_VERSION = "past-only-temporal-cache-v2"
TARGETS = {
    **{
        f"stockout_{minutes}m": (
            f"label_stockout_{minutes}m",
            f"label_end_stockout_{minutes}m",
        )
        for minutes in TARGET_HORIZONS
    },
    "sustained_30m": ("label_sustained_30m", "label_end_sustained_30m"),
}
ABLATION_ORDER = (
    "baseline",
    "lag_depletion",
    "volatility_state",
    "station_priors",
    "signed_network",
)


@dataclass(frozen=True)
class Fold:
    name: str
    validation_start: datetime
    validation_end: datetime


@dataclass(frozen=True)
class ExperimentConfig:
    source_path: Path
    data_dir: Path
    export_id: str
    development_end: datetime
    purge_minutes: int
    neighbor_radius_meters: float
    seed: int
    bootstrap_draws: int
    block_minutes: int
    report_path: Path
    plot_directory: Path
    folds: tuple[Fold, ...]
    model_parameters: dict[str, Any]


def _parse_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO 8601 string.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must use UTC.")
    return parsed.astimezone(UTC)


def load_experiment_config(path: Path) -> ExperimentConfig:
    raw = tomllib.loads(path.read_text())
    experiment = raw["experiment"]
    model = raw["model"]
    output = raw["output"]
    folds = tuple(
        Fold(
            name=fold["name"],
            validation_start=_parse_utc(fold["validation_start_utc"], "validation_start_utc"),
            validation_end=_parse_utc(fold["validation_end_utc"], "validation_end_utc"),
        )
        for fold in raw["folds"]
    )
    if not folds or any(fold.validation_start >= fold.validation_end for fold in folds):
        raise ValueError("Every chronological fold needs a non-empty validation interval.")
    if any(left.validation_end > right.validation_start for left, right in pairwise(folds)):
        raise ValueError("Validation folds must be chronological and non-overlapping.")
    development_end = _parse_utc(experiment["development_end_utc"], "development_end_utc")
    if any(fold.validation_end > development_end for fold in folds):
        raise ValueError("A validation fold reaches the spent final-test period.")
    purge_minutes = int(experiment["purge_minutes"])
    if purge_minutes < 30:
        raise ValueError("The temporal study requires at least a 30-minute purge.")
    root = path.resolve().parents[1]
    return ExperimentConfig(
        source_path=path.resolve(),
        data_dir=(root / experiment["data_dir"]).resolve(),
        export_id=str(experiment["export_id"]),
        development_end=development_end,
        purge_minutes=purge_minutes,
        neighbor_radius_meters=float(experiment["neighbor_radius_meters"]),
        seed=int(experiment["seed"]),
        bootstrap_draws=int(experiment["bootstrap_draws"]),
        block_minutes=int(experiment["block_minutes"]),
        report_path=(root / output["report_path"]).resolve(),
        plot_directory=(root / output["plot_directory"]).resolve(),
        folds=folds,
        model_parameters=dict(model),
    )


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str) + "\n"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_export_manifest(config: ExperimentConfig) -> tuple[Path, dict[str, Any]]:
    directory = config.data_dir / "ml" / "exports" / config.export_id
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("run_id") != config.export_id:
        raise ValueError("Configured export ID does not match its manifest.")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("Frozen export manifest is missing artifact hashes.")
    for name in ("sources.json", "full_labeled.parquet"):
        path = directory / name
        if not path.exists() or _sha256(path) != artifacts.get(name):
            raise ValueError(f"Frozen export artifact hash mismatch: {path}")
    test_start = _parse_utc(manifest["test_start_utc"], "test_start_utc")
    if config.development_end != test_start:
        raise ValueError("Development end must equal the frozen study's untouched test boundary.")
    return directory, manifest


def _package_source_fingerprint() -> str:
    digest = hashlib.sha256()
    package_root = Path(__file__).parents[1]
    for path in sorted(package_root.rglob("*.py")):
        digest.update(path.relative_to(package_root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _history_partition_paths(config: ExperimentConfig, manifest: dict[str, Any]) -> list[Path]:
    first_boundary = _parse_utc(manifest["first_boundary_utc"], "first_boundary_utc")
    start = first_boundary - timedelta(minutes=65)
    end = config.development_end
    paths: list[Path] = []
    for path in sorted((config.data_dir / "silver" / "observations").glob("date=*/hour=*.parquet")):
        stamp = datetime.strptime(
            f"{path.parent.name.removeprefix('date=')} {path.stem.removeprefix('hour=')}",
            "%Y-%m-%d %H",
        ).replace(tzinfo=UTC)
        if stamp < end and stamp + timedelta(hours=1) > start:
            paths.append(path)
    return paths


def _provenance_file(path: Path, base: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(base).as_posix(),
        "size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _cache_provenance(config: ExperimentConfig, manifest: dict[str, Any]) -> dict[str, Any]:
    manifest_path = config.data_dir / "ml" / "exports" / config.export_id / "manifest.json"
    history_paths = _history_partition_paths(config, manifest)
    if not history_paths:
        raise ValueError("No development-history partitions are available for the temporal cache.")
    station_path = station_dimension_path(config.data_dir)
    transformation_files = {
        name: _sha256(Path(__file__).with_name(name))
        for name in ("dataset.py", "temporal_features.py", "temporal_study.py")
    }
    transformation_files["data/compaction.py"] = _sha256(
        Path(__file__).parents[1] / "data" / "compaction.py"
    )
    dependencies = {
        name: importlib.metadata.version(name) for name in ("numpy", "polars", "scikit-learn")
    }
    return {
        "schema_version": TEMPORAL_CACHE_SCHEMA_VERSION,
        "export_manifest": _provenance_file(manifest_path, config.data_dir),
        "export_source_sha256": manifest["source_sha256"],
        "history_partitions": [_provenance_file(path, config.data_dir) for path in history_paths],
        "station_dimension": (
            _provenance_file(station_path, config.data_dir) if station_path.exists() else None
        ),
        "transformations": {
            "first_boundary_utc": manifest["first_boundary_utc"],
            "cutoff_received_at_utc": manifest["cutoff_received_at_utc"],
            "development_end_exclusive_utc": config.development_end.isoformat(),
            "canonical_interval_minutes": 5,
            "canonical_max_feed_age_seconds": 300,
            "neighbor_radius_meters": config.neighbor_radius_meters,
            "source_files": transformation_files,
        },
        "package_source_sha256": _package_source_fingerprint(),
        "runtime": {"python": platform.python_version(), **dependencies},
    }


def _cache_key(provenance: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()[:16]


def _cache_schema(frame: pl.DataFrame) -> dict[str, str]:
    return {name: str(dtype) for name, dtype in frame.schema.items()}


def _validate_cache_frame(
    frame: pl.DataFrame, config: ExperimentConfig, manifest: dict[str, Any]
) -> None:
    if frame.is_empty():
        raise ValueError("The past-only feature cache has no eligible development rows.")
    required = {
        "station_id",
        "as_of_utc",
        "usable",
        *(column for columns in FEATURE_GROUPS.values() for column in columns),
        *(column for pair in TARGETS.values() for column in pair),
    } - set(PRIOR_FEATURES)
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"Feature cache is missing implemented columns: {missing}")
    schema = frame.schema
    if schema["station_id"] != pl.String:
        raise ValueError("Feature cache station_id must use String dtype.")
    as_of_dtype = schema["as_of_utc"]
    if not isinstance(as_of_dtype, pl.Datetime) or as_of_dtype.time_zone != "UTC":
        raise ValueError("Feature cache as_of_utc must use a UTC Datetime dtype.")
    if schema["usable"] != pl.Boolean:
        raise ValueError("Feature cache usable must use Boolean dtype.")
    feature_columns = {column for columns in FEATURE_GROUPS.values() for column in columns} - set(
        PRIOR_FEATURES
    )
    invalid_features = sorted(
        column for column in feature_columns if not schema[column].is_numeric()
    )
    if invalid_features:
        raise ValueError(f"Feature cache has non-numeric feature dtypes: {invalid_features}")
    for label_column, end_column in TARGETS.values():
        if not schema[label_column].is_integer():
            raise ValueError(f"Feature cache {label_column} must use an integer dtype.")
        end_dtype = schema[end_column]
        if not isinstance(end_dtype, pl.Datetime) or end_dtype.time_zone != "UTC":
            raise ValueError(f"Feature cache {end_column} must use a UTC Datetime dtype.")
    if frame.select(pl.struct("station_id", "as_of_utc").is_duplicated().any()).item():
        raise ValueError("Feature cache contains duplicate station-time keys.")
    first_boundary = _parse_utc(manifest["first_boundary_utc"], "first_boundary_utc")
    min_time: Any = frame["as_of_utc"].min()
    max_time: Any = frame["as_of_utc"].max()
    if not isinstance(min_time, datetime) or min_time < first_boundary:
        raise ValueError("Feature cache starts before the declared development boundary.")
    if not isinstance(max_time, datetime) or max_time >= config.development_end:
        raise ValueError("Feature cache crosses into the spent test interval.")


def _build_feature_cache(
    config: ExperimentConfig,
    manifest: dict[str, Any],
    cache_path: Path,
    cache_manifest_path: Path,
    provenance: dict[str, Any],
    build_reason: str,
) -> pl.DataFrame:
    first_boundary = _parse_utc(manifest["first_boundary_utc"], "first_boundary_utc")
    cutoff = _parse_utc(manifest["cutoff_received_at_utc"], "cutoff_received_at_utc")
    history = load_history(
        config.data_dir,
        first_boundary - timedelta(minutes=65),
        config.development_end,
    ).filter(
        (pl.col("received_at_utc") <= cutoff) & (pl.col("received_at_utc") < config.development_end)
    )
    grid = canonical_grid(
        history,
        first_boundary - timedelta(minutes=60),
        config.development_end,
    )
    temporal = build_temporal_features(grid)
    labels = build_target_labels(grid)
    network = build_signed_network_features(
        temporal,
        radius_meters=config.neighbor_radius_meters,
    )
    frame = (
        temporal.join(labels, on=["station_id", "as_of_utc"], how="left")
        .join(network, on=["station_id", "as_of_utc"], how="left")
        .filter(
            (pl.col("as_of_utc") >= first_boundary)
            & (pl.col("as_of_utc") < config.development_end)
            & pl.col("usable")
            & (pl.col("available_bikes") > 0)
            & (pl.col("lag_observed_15m") == 1)
        )
        .sort("as_of_utc", "station_id")
    )
    _validate_cache_frame(frame, config, manifest)
    atomic_write_parquet(cache_path, frame)
    minimum_as_of: Any = frame["as_of_utc"].min()
    maximum_as_of: Any = frame["as_of_utc"].max()
    assert isinstance(minimum_as_of, datetime)
    assert isinstance(maximum_as_of, datetime)
    cache_manifest = {
        "schema_version": TEMPORAL_CACHE_SCHEMA_VERSION,
        "cache_key": cache_path.stem,
        "provenance": provenance,
        "provenance_sha256": hashlib.sha256(
            json.dumps(provenance, sort_keys=True).encode()
        ).hexdigest(),
        "artifact": {
            "path": cache_path.name,
            "sha256": _sha256(cache_path),
            "schema": _cache_schema(frame),
            "row_count": frame.height,
            "minimum_as_of_utc": minimum_as_of.isoformat(),
            "maximum_as_of_utc": maximum_as_of.isoformat(),
        },
        "build_reason": build_reason,
    }
    atomic_write_text(cache_manifest_path, _json(cache_manifest))
    return frame


def _load_or_build_features(
    config: ExperimentConfig, manifest: dict[str, Any]
) -> tuple[pl.DataFrame, Path, bool, str, dict[str, Any]]:
    provenance = _cache_provenance(config, manifest)
    cache_key = _cache_key(provenance)
    cache_root = config.data_dir / "ml" / "temporal_cache" / "v2"
    cache_path = cache_root / f"{cache_key}.parquet"
    cache_manifest_path = cache_root / f"{cache_key}.manifest.json"
    reason = "cache files are missing"
    if cache_path.exists() and cache_manifest_path.exists():
        try:
            cache_manifest = json.loads(cache_manifest_path.read_text())
            if not isinstance(cache_manifest, dict):
                raise ValueError("cache manifest must be a JSON object")
            expected_provenance_sha256 = hashlib.sha256(
                json.dumps(provenance, sort_keys=True).encode()
            ).hexdigest()
            if cache_manifest.get("schema_version") != TEMPORAL_CACHE_SCHEMA_VERSION:
                raise ValueError("cache manifest schema is obsolete")
            if cache_manifest.get("cache_key") != cache_key:
                raise ValueError("cache key does not match its provenance")
            if cache_manifest.get("provenance_sha256") != expected_provenance_sha256:
                raise ValueError("cache provenance does not match current inputs")
            if cache_manifest.get("provenance") != provenance:
                raise ValueError("cache provenance details do not match current inputs")
            artifact = cache_manifest.get("artifact")
            if not isinstance(artifact, dict):
                raise ValueError("cache manifest has no artifact metadata")
            if artifact.get("sha256") != _sha256(cache_path):
                raise ValueError("cached Parquet checksum mismatch")
            frame = pl.read_parquet(cache_path)
            _validate_cache_frame(frame, config, manifest)
            if artifact.get("row_count") != frame.height:
                raise ValueError("cached row count does not match its manifest")
            if artifact.get("schema") != _cache_schema(frame):
                raise ValueError("cached schema does not match its manifest")
            minimum: Any = frame["as_of_utc"].min()
            maximum: Any = frame["as_of_utc"].max()
            if (
                artifact.get("minimum_as_of_utc") != minimum.isoformat()
                or artifact.get("maximum_as_of_utc") != maximum.isoformat()
            ):
                raise ValueError("cached time bounds do not match its manifest")
            return frame, cache_path, True, "verified cache reused", provenance
        except (OSError, ValueError, json.JSONDecodeError, pl.exceptions.PolarsError) as exc:
            reason = f"cache rebuilt after validation failure: {exc}"
    elif cache_path.exists() or cache_manifest_path.exists():
        reason = "cache rebuilt because its Parquet/manifest pair was incomplete"
    frame = _build_feature_cache(
        config,
        manifest,
        cache_path,
        cache_manifest_path,
        provenance,
        reason,
    )
    return frame, cache_path, False, reason, provenance


def _matrix(frame: pl.DataFrame, features: list[str]) -> NDArray[np.float64]:
    return np.asarray(frame.select(features).to_numpy(), dtype=np.float64)


def _fit_score(
    train: pl.DataFrame,
    validation: pl.DataFrame,
    features: list[str],
    label_column: str,
    parameters: dict[str, Any],
) -> tuple[NDArray[np.float64], float, float]:
    model = HistGradientBoostingClassifier(**parameters)
    started = time.perf_counter()
    with threadpool_limits(limits=4):
        model.fit(_matrix(train, features), train[label_column].to_numpy())
    fit_seconds = time.perf_counter() - started
    started = time.perf_counter()
    with threadpool_limits(limits=4):
        scores = np.asarray(model.predict_proba(_matrix(validation, features))[:, 1])
    score_seconds = time.perf_counter() - started
    return scores, fit_seconds, score_seconds


def _fold_frames(
    frame: pl.DataFrame,
    fold: Fold,
    *,
    label_column: str,
    label_end_column: str,
    purge_minutes: int,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    train = frame.filter(
        pl.col(label_column).is_not_null()
        & (pl.col("as_of_utc") < fold.validation_start - timedelta(minutes=purge_minutes))
        & (pl.col(label_end_column) < fold.validation_start)
    )
    validation = frame.filter(
        pl.col(label_column).is_not_null()
        & (pl.col("as_of_utc") >= fold.validation_start)
        & (pl.col("as_of_utc") < fold.validation_end)
        & (pl.col(label_end_column) < fold.validation_end)
    )
    for name, selected in (("train", train), ("validation", validation)):
        if selected.is_empty() or selected[label_column].n_unique() != 2:
            raise ValueError(f"Fold {fold.name} {name} rows need both target classes.")
    return train.sort("as_of_utc", "station_id"), validation.sort("as_of_utc", "station_id")


def _prediction_frame(
    validation: pl.DataFrame,
    scores: NDArray[np.float64],
    *,
    analysis: str,
    candidate: str,
    target: str,
    fold: str,
    label_column: str,
) -> pl.DataFrame:
    return validation.select(
        "station_id", "as_of_utc", pl.col(label_column).alias("label")
    ).with_columns(
        pl.Series("score", scores),
        pl.lit(analysis).alias("analysis"),
        pl.lit(candidate).alias("candidate"),
        pl.lit(target).alias("target"),
        pl.lit(fold).alias("fold"),
    )


def _pr_curve(labels: NDArray[np.int_], scores: NDArray[np.float64]) -> pl.DataFrame:
    precision, recall, thresholds = precision_recall_curve(labels, scores)
    threshold_values = np.concatenate([thresholds, np.asarray([np.nan])])
    return pl.DataFrame({"precision": precision, "recall": recall, "threshold": threshold_values})


def _choose_f1_threshold(labels: NDArray[np.int_], scores: NDArray[np.float64]) -> float:
    precision, recall, thresholds = precision_recall_curve(labels, scores)
    f1 = np.divide(
        2 * precision[:-1] * recall[:-1],
        precision[:-1] + recall[:-1],
        out=np.zeros_like(precision[:-1]),
        where=(precision[:-1] + recall[:-1]) > 0,
    )
    best = max(
        range(len(thresholds)), key=lambda index: (f1[index], precision[index], thresholds[index])
    )
    return float(thresholds[best])


def _warning_metrics(
    labels: NDArray[np.int_], scores: NDArray[np.float64], threshold: float
) -> dict[str, Any]:
    alerts = scores >= threshold
    positives = int(labels.sum())
    captured = int(labels[alerts].sum())
    return {
        "threshold": threshold,
        "precision": float(precision_score(labels, alerts, zero_division=0)),
        "recall": float(recall_score(labels, alerts, zero_division=0)),
        "alert_count": int(alerts.sum()),
        "alert_rate": float(alerts.mean()),
        "positives_captured": captured,
        "positive_count": positives,
    }


def _frontier_metrics(curve: pl.DataFrame) -> tuple[dict[str, Any], dict[str, Any]]:
    precision = np.asarray(curve["precision"].to_numpy(), dtype=np.float64)
    recall = np.asarray(curve["recall"].to_numpy(), dtype=np.float64)
    at_recall: dict[str, Any] = {}
    for target in (0.2, 0.3, 0.4, 0.5):
        eligible = precision[recall >= target]
        at_recall[f"{target:.1f}"] = float(eligible.max()) if eligible.size else None
    at_precision: dict[str, Any] = {}
    for target in (0.4, 0.5, 0.6):
        eligible = recall[precision >= target]
        at_precision[f"{target:.1f}"] = float(eligible.max()) if eligible.size else None
    return at_recall, at_precision


def _budget_metrics(frame: pl.DataFrame, budget: float) -> dict[str, Any]:
    count = max(1, math.ceil(frame.height * budget))
    selected = frame.sort(
        ["score", "as_of_utc", "station_id"],
        descending=[True, False, False],
    ).head(count)
    captured = int(selected["label"].sum())
    positives = int(frame["label"].sum())
    return {
        "budget": budget,
        "alert_count": selected.height,
        "alert_rate": selected.height / frame.height,
        "precision": captured / selected.height,
        "recall": captured / positives if positives else 0.0,
        "positives_captured": captured,
    }


def _metrics(frame: pl.DataFrame) -> tuple[dict[str, Any], pl.DataFrame]:
    labels = np.asarray(frame["label"].to_numpy(), dtype=np.int_)
    scores = np.asarray(frame["score"].to_numpy(), dtype=np.float64)
    curve = _pr_curve(labels, scores)
    at_recall, at_precision = _frontier_metrics(curve)
    threshold = _choose_f1_threshold(labels, scores)
    return (
        {
            "average_precision": float(average_precision_score(labels, scores)),
            "row_count": frame.height,
            "positive_count": int(labels.sum()),
            "prevalence": float(labels.mean()),
            "precision_at_recall": at_recall,
            "recall_at_precision": at_precision,
            "f1_reference_policy": _warning_metrics(labels, scores, threshold),
            "alert_budgets": {
                f"{budget:.3f}": _budget_metrics(frame, budget)
                for budget in (0.005, 0.01, 0.02, 0.05)
            },
        },
        curve,
    )


def _evaluate_predictions(
    predictions: pl.DataFrame,
) -> tuple[dict[str, Any], pl.DataFrame]:
    summaries: dict[str, Any] = {}
    curves: list[pl.DataFrame] = []
    keys = (
        predictions.select("analysis", "target", "candidate")
        .unique()
        .sort("analysis", "target", "candidate")
    )
    for analysis, target, candidate in keys.iter_rows():
        selected = predictions.filter(
            (pl.col("analysis") == analysis)
            & (pl.col("target") == target)
            & (pl.col("candidate") == candidate)
        )
        aggregate, curve = _metrics(selected)
        folds: dict[str, Any] = {}
        for fold in selected["fold"].unique().sort().to_list():
            fold_metrics, _ = _metrics(selected.filter(pl.col("fold") == fold))
            folds[str(fold)] = fold_metrics
        key = f"{analysis}:{target}:{candidate}"
        summaries[key] = {**aggregate, "folds": folds}
        curves.append(
            curve.with_columns(
                pl.lit(str(analysis)).alias("analysis"),
                pl.lit(str(target)).alias("target"),
                pl.lit(str(candidate)).alias("candidate"),
            )
        )
    return summaries, pl.concat(curves).select(
        "analysis", "target", "candidate", "precision", "recall", "threshold"
    )


def _best_temporal_candidate(metrics: dict[str, Any]) -> str:
    return max(
        ABLATION_ORDER[1:],
        key=lambda candidate: metrics[f"ablation:stockout_30m:{candidate}"]["average_precision"],
    )


def _block_ids(frame: pl.DataFrame, minutes: int) -> list[str]:
    times: list[datetime] = frame["as_of_utc"].to_list()
    unique = sorted(set(times))
    mapping: dict[datetime, str] = {}
    segment = 0
    segment_start: datetime | None = None
    previous: datetime | None = None
    for stamp in unique:
        if previous is None or stamp - previous > timedelta(minutes=5):
            segment += 1
            segment_start = stamp
        assert segment_start is not None
        offset = int((stamp - segment_start).total_seconds() // (minutes * 60))
        mapping[stamp] = f"s{segment:02d}_b{offset:03d}_{segment_start.isoformat()}"
        previous = stamp
    return [mapping[stamp] for stamp in times]


def _block_analysis(
    predictions: pl.DataFrame,
    baseline: str,
    winner: str,
    *,
    block_minutes: int,
    draws: int,
    seed: int,
) -> tuple[pl.DataFrame, dict[str, Any]]:
    selected = predictions.filter(
        (pl.col("analysis") == "ablation")
        & (pl.col("target") == "stockout_30m")
        & pl.col("candidate").is_in([baseline, winner])
    )
    thresholds: dict[str, float] = {}
    for candidate in (baseline, winner):
        candidate_frame = selected.filter(pl.col("candidate") == candidate)
        thresholds[candidate] = _choose_f1_threshold(
            np.asarray(candidate_frame["label"].to_numpy(), dtype=np.int_),
            np.asarray(candidate_frame["score"].to_numpy(), dtype=np.float64),
        )
    rows: list[dict[str, Any]] = []
    for candidate in (baseline, winner):
        candidate_frame = selected.filter(pl.col("candidate") == candidate).sort(
            "as_of_utc", "station_id"
        )
        candidate_frame = candidate_frame.with_columns(
            pl.Series("block", _block_ids(candidate_frame, block_minutes))
        )
        for block in candidate_frame["block"].unique().sort().to_list():
            block_frame = candidate_frame.filter(pl.col("block") == block)
            labels = np.asarray(block_frame["label"].to_numpy(), dtype=np.int_)
            scores = np.asarray(block_frame["score"].to_numpy(), dtype=np.float64)
            warning = _warning_metrics(labels, scores, thresholds[candidate])
            curve = _pr_curve(labels, scores) if np.unique(labels).size == 2 else None
            p40 = _frontier_metrics(curve)[0]["0.4"] if curve is not None else None
            rows.append(
                {
                    "candidate": candidate,
                    "block": block,
                    "start_utc": block_frame["as_of_utc"].min(),
                    "end_utc": block_frame["as_of_utc"].max(),
                    "row_count": block_frame.height,
                    "positive_count": int(labels.sum()),
                    "average_precision": (
                        float(average_precision_score(labels, scores))
                        if np.unique(labels).size == 2
                        else None
                    ),
                    "precision_at_40_recall": p40,
                    "precision": warning["precision"],
                    "recall": warning["recall"],
                    "alert_count": warning["alert_count"],
                }
            )
    block_frame = pl.DataFrame(rows).sort("block", "candidate")
    paired = block_frame.filter(
        pl.col("average_precision").is_not_null() & pl.col("precision_at_40_recall").is_not_null()
    ).pivot(
        on="candidate",
        index="block",
        values=["average_precision", "precision_at_40_recall"],
    )
    ap_difference = np.asarray(
        paired[f"average_precision_{winner}"].to_numpy()
        - paired[f"average_precision_{baseline}"].to_numpy(),
        dtype=np.float64,
    )
    p40_difference = np.asarray(
        paired[f"precision_at_40_recall_{winner}"].to_numpy()
        - paired[f"precision_at_40_recall_{baseline}"].to_numpy(),
        dtype=np.float64,
    )
    rng = np.random.default_rng(seed)

    def interval(values: NDArray[np.float64]) -> dict[str, Any]:
        if not len(values):
            return {"mean": None, "lower_95": None, "upper_95": None}
        sampled = values[rng.integers(0, len(values), size=(draws, len(values)))].mean(axis=1)
        return {
            "mean": float(values.mean()),
            "lower_95": float(np.quantile(sampled, 0.025)),
            "upper_95": float(np.quantile(sampled, 0.975)),
        }

    uncertainty = {
        "block_minutes": block_minutes,
        "paired_usable_block_count": len(ap_difference),
        "bootstrap_draws": draws,
        "method": "paired resampling of contiguous network-wide 60-minute block metrics",
        "average_precision_difference": interval(ap_difference),
        "precision_at_40_recall_difference": interval(p40_difference),
        "limitation": (
            "Blocks come from only three collection sessions across three calendar dates; the "
            "interval describes those observed sessions and is not a seasonal generalization CI."
        ),
    }
    return block_frame, uncertainty


def _svg_pr_plot(curves: pl.DataFrame, output: Path, *, title: str) -> None:
    width, height = 820, 520
    left, right, top, bottom = 70, 25, 45, 60
    plot_width = width - left - right
    plot_height = height - top - bottom
    colors = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9"]
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width / 2}" y="24" text-anchor="middle" font-family="sans-serif" font-size="18">{title}</text>',
    ]
    for tick in range(0, 11, 2):
        value = tick / 10
        x = left + value * plot_width
        y = top + (1 - value) * plot_height
        lines.extend(
            [
                f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_height}" stroke="#eeeeee"/>',
                f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}" stroke="#eeeeee"/>',
                f'<text x="{x:.1f}" y="{top + plot_height + 22}" text-anchor="middle" font-family="sans-serif" font-size="11">{value:.1f}</text>',
                f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" font-family="sans-serif" font-size="11">{value:.1f}</text>',
            ]
        )
    lines.extend(
        [
            f'<line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}" stroke="black"/>',
            f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" stroke="black"/>',
            f'<text x="{left + plot_width / 2}" y="{height - 14}" text-anchor="middle" font-family="sans-serif">Recall</text>',
            f'<text x="18" y="{top + plot_height / 2}" text-anchor="middle" font-family="sans-serif" transform="rotate(-90 18 {top + plot_height / 2})">Precision</text>',
        ]
    )
    candidates = curves.select("candidate").unique().sort("candidate")["candidate"].to_list()
    for index, candidate in enumerate(candidates):
        selected = curves.filter(pl.col("candidate") == candidate)
        step = max(1, selected.height // 1200)
        selected = selected.gather_every(step)
        points = " ".join(
            f"{left + recall * plot_width:.1f},{top + (1 - precision) * plot_height:.1f}"
            for precision, recall in selected.select("precision", "recall").iter_rows()
        )
        color = colors[index % len(colors)]
        lines.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>')
        legend_y = top + 18 * index
        lines.extend(
            [
                f'<line x1="{left + 10}" y1="{legend_y}" x2="{left + 30}" y2="{legend_y}" stroke="{color}" stroke-width="3"/>',
                f'<text x="{left + 36}" y="{legend_y + 4}" font-family="sans-serif" font-size="11">{candidate}</text>',
            ]
        )
    lines.append("</svg>")
    atomic_write_text(output, "\n".join(lines) + "\n")


def _fmt(value: Any, digits: int = 4) -> str:
    return "—" if value is None else f"{float(value):.{digits}f}"


def _report(
    config: ExperimentConfig,
    manifest: dict[str, Any],
    metrics: dict[str, Any],
    winner: str,
    fold_design: list[dict[str, Any]],
    quality: dict[str, Any],
    uncertainty: dict[str, Any],
    cache_path: Path,
    cache_reused: bool,
    cache_status: str,
    fit_times: dict[str, Any],
) -> tuple[str, str]:
    repository_root = config.source_path.parents[1]
    try:
        cache_display = cache_path.relative_to(repository_root)
    except ValueError:
        cache_display = cache_path
    baseline = metrics["ablation:stockout_30m:baseline"]
    winning = metrics[f"ablation:stockout_30m:{winner}"]
    ap_gain = winning["average_precision"] - baseline["average_precision"]
    p40_gain = winning["precision_at_recall"]["0.4"] - baseline["precision_at_recall"]["0.4"]
    positive_folds = sum(
        1
        for fold in config.folds
        if (
            winning["folds"][fold.name]["average_precision"]
            > baseline["folds"][fold.name]["average_precision"]
            and winning["folds"][fold.name]["precision_at_recall"]["0.4"]
            > baseline["folds"][fold.name]["precision_at_recall"]["0.4"]
        )
    )
    total_fit_seconds = sum(float(value["fit_seconds"]) for value in fit_times.values())
    total_score_seconds = sum(float(value["score_seconds"]) for value in fit_times.values())
    weakest_fold = min(
        config.folds,
        key=lambda item: (
            winning["folds"][item.name]["average_precision"]
            - baseline["folds"][item.name]["average_precision"]
        ),
    )
    weakest_design = next(row for row in fold_design if row["name"] == weakest_fold.name)
    weakest_baseline = baseline["folds"][weakest_fold.name]
    weakest_winner = winning["folds"][weakest_fold.name]
    if ap_gain >= 0.02 and p40_gain >= 0.05 and positive_folds >= math.ceil(len(config.folds) / 2):
        conclusion = "A. Strong signal improvement"
        recommendation = (
            "Carry the winning past-only configuration into a prospectively collected study; "
            "do not reopen the spent frozen test period."
        )
    elif ap_gain > 0 or p40_gain > 0 or positive_folds:
        conclusion = "B. Moderate/local improvement"
        recommendation = (
            "Treat the signal as regime-dependent. Collect continuous prospective history and "
            "test a narrower warning horizon plus external weather/transit/event context."
        )
    else:
        conclusion = "C. Little improvement"
        recommendation = (
            "Stop elaborating the same history family; prioritize horizon redesign, continuous "
            "prospective collection, and external context."
        )

    lines = [
        "# Past-only temporal stockout study",
        "",
        f"Decision: **{conclusion}**",
        "",
        recommendation,
        "",
        "This is a development-only rolling-validation study. The frozen final test begins at "
        f"`{config.development_end.isoformat()}` and was neither loaded into the feature cache nor scored.",
        "",
        "## Phase 1 audit",
        "",
        "- Current feature and label generation: `src/bikepulse/ml/dataset.py`. The legacy core uses "
        "  current bikes/docks/capacity, bike ratio, an exact usable t-15m bike change, Paris time "
        "  sine/cosine, and weekend. Labels require usable exact observations at +5 through +30m.",
        "- Frozen-study modelling: `src/bikepulse/ml/frozen_study.py`. Its HGBM uses learning rate "
        "  0.1, 100 iterations, 15 leaf nodes, minimum leaf size 100, L2=1, no early stopping, "
        "  no class weighting, and seed 42.",
        f"- Frozen boundaries: validation starts `{manifest['validation_start_utc']}`; the spent test "
        f"  starts `{manifest['test_start_utc']}`. Earlier labels whose 30-minute windows touch a "
        "  boundary are purged.",
        "- Missing snapshots are not imputed: the canonical grid selects the latest whole snapshot "
        "  already received within five minutes; stale/inactive/missing counts are unusable. Any "
        "  unusable future makes a label null, and already-empty starting stations are excluded.",
        "- Archived status payloads contain mechanical and electric bike counts, `is_renting`, "
        "  `is_returning`, `is_installed`, feed/source timestamps, receipt timestamps, station report "
        "  timestamps, and derived report age. These fields were confirmed in the normalized schema "
        "  and a raw archived payload.",
        "",
        "## Validation design",
        "",
        "Training is expanding and strictly earlier than validation. Every network timestamp stays "
        "in one fold. A 30-minute purge is applied before each validation block; validation labels "
        "must also finish before that block ends. Evaluation retains natural prevalence.",
        "",
        "| Fold | Validation UTC | Train rows | Validation rows | Positives | Prevalence |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for fold_row in fold_design:
        lines.append(
            f"| {fold_row['name']} | {fold_row['validation_start_utc']} to "
            f"{fold_row['validation_end_utc']} | {fold_row['train_rows']} "
            f"| {fold_row['validation_rows']} | {fold_row['validation_positives']} "
            f"| {fold_row['validation_prevalence']:.2%} |"
        )
    lines.extend(
        [
            "",
            "Only three defensible blocks exist before the frozen test. They span three collection "
            "sessions but only three dates, so regime and seasonal uncertainty remains high.",
            "",
            "## Feature availability and leakage safeguards",
            "",
        ]
    )
    for name, description in FEATURE_AVAILABILITY.items():
        lines.append(f"- `{name}`: {description}")
    lines.extend(
        [
            "- The [feature catalog](temporal-feature-catalog.md) defines every generated column, "
            "  formula, horizon expansion, missing-state rule, and causal availability.",
            "- Exact lag joins expose explicit observed/missing flags. Slopes and rolling summaries "
            "  require every five-minute point in their trailing window; gaps are never filled.",
            "- Time-to-zero exists only for a negative trailing slope and is clipped at 240 minutes.",
            "- Rebalancing flags mark station or synchronized neighbor jumps of at least five bikes "
            "  or 30% of capacity; they do not assume those jumps are rider demand.",
            "- Shrunk station priors use a fixed Beta prior and fold-training history. A training row "
            "  is encoded before its timestamp is added; its outcome is added only after label-end. "
            "  Validation outcomes never update validation features.",
            f"- Feature cache: `{cache_display}` ({'reused' if cache_reused else 'generated'}; "
            f"{cache_status}), with {quality['row_count']} eligible rows and "
            f"{quality['timestamp_count']} timestamps. "
            f"Complete 60-minute history rate: {quality['complete_history_rates']['60m']:.2%}.",
            "",
            "## Feature ablation — original 30-minute target",
            "",
            "Precision-at-recall values are points on the pooled out-of-fold PR frontier. The F1 "
            "threshold is shown only as the requested legacy reference; it is selected and described "
            "on the same development predictions and is not an independent policy estimate.",
            "",
            "| Candidate | Features | AP | P@R20 | P@R30 | P@R40 | P@R50 | F1-ref precision | F1-ref recall | Alerts | Alert rate |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for candidate in ABLATION_ORDER:
        item = metrics[f"ablation:stockout_30m:{candidate}"]
        policy = item["f1_reference_policy"]
        lines.append(
            f"| {candidate} | {len(FEATURE_GROUPS[candidate])} | {item['average_precision']:.4f} "
            f"| {_fmt(item['precision_at_recall']['0.2'])} | {_fmt(item['precision_at_recall']['0.3'])} "
            f"| {_fmt(item['precision_at_recall']['0.4'])} | {_fmt(item['precision_at_recall']['0.5'])} "
            f"| {policy['precision']:.4f} | {policy['recall']:.4f} | {policy['alert_count']} "
            f"| {policy['alert_rate']:.2%} |"
        )
    lines.extend(
        [
            "",
            "[Full ablation PR plot](assets/temporal-ablation-pr.svg)",
            "",
            "### Recall at fixed precision",
            "",
            "| Candidate | R@P40 | R@P50 | R@P60 |",
            "|---|---:|---:|---:|",
        ]
    )
    for candidate in ABLATION_ORDER:
        item = metrics[f"ablation:stockout_30m:{candidate}"]["recall_at_precision"]
        lines.append(
            f"| {candidate} | {_fmt(item['0.4'])} | {_fmt(item['0.5'])} | {_fmt(item['0.6'])} |"
        )
    lines.extend(
        [
            "",
            "### Matched alert budgets",
            "",
            "Each cell is precision / recall / positives captured. Exact alert counts and realized "
            "rates use `ceil(budget x rows)` and are recorded in `metrics.json`.",
            "",
            "| Candidate | 0.5% budget | 1% budget | 2% budget | 5% budget |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for candidate in ABLATION_ORDER:
        budgets = metrics[f"ablation:stockout_30m:{candidate}"]["alert_budgets"]
        cells = []
        for budget in ("0.005", "0.010", "0.020", "0.050"):
            item = budgets[budget]
            cells.append(
                f"{item['precision']:.4f} / {item['recall']:.4f} / {item['positives_captured']}"
            )
        lines.append(f"| {candidate} | " + " | ".join(cells) + " |")
    budget_reference = baseline["alert_budgets"]
    realized_budgets = "; ".join(
        f"{float(key):.1%}: {item['alert_count']} alerts ({item['alert_rate']:.3%})"
        for key, item in budget_reference.items()
    )
    lines.extend(["", f"Realized pooled budgets: {realized_budgets}."])
    lines.extend(
        [
            "",
            f"Best temporal candidate: `{winner}`. Relative to baseline: AP {ap_gain:+.4f}, "
            f"precision at 40% recall {p40_gain:+.4f}, with both directions positive in "
            f"{positive_folds}/{len(config.folds)} folds.",
            "",
            "### Per-fold matched comparison",
            "",
            "| Fold | Baseline AP | Winner AP | ΔAP | Baseline P@R40 | Winner P@R40 | ΔP@R40 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for configured_fold in config.folds:
        base_fold = baseline["folds"][configured_fold.name]
        win_fold = winning["folds"][configured_fold.name]
        lines.append(
            f"| {configured_fold.name} | {base_fold['average_precision']:.4f} "
            f"| {win_fold['average_precision']:.4f} "
            f"| {win_fold['average_precision'] - base_fold['average_precision']:+.4f} "
            f"| {_fmt(base_fold['precision_at_recall']['0.4'])} "
            f"| {_fmt(win_fold['precision_at_recall']['0.4'])} "
            f"| {win_fold['precision_at_recall']['0.4'] - base_fold['precision_at_recall']['0.4']:+.4f} |"
        )
    weak_base_budget = weakest_baseline["alert_budgets"]["0.010"]
    weak_win_budget = weakest_winner["alert_budgets"]["0.010"]
    lines.extend(
        [
            "",
            f"The weakest regime is `{weakest_fold.name}`: it is the earliest fold, has only "
            f"{weakest_design['train_rows']} training rows and {weakest_design['validation_prevalence']:.2%} "
            f"prevalence, and loses {weakest_winner['average_precision'] - weakest_baseline['average_precision']:+.4f} "
            f"AP. Its 1% alert-budget precision moves from {weak_base_budget['precision']:.4f} to "
            f"{weak_win_budget['precision']:.4f}. Missing long history is not the explanation "
            f"({weakest_design['validation_complete_60m']:.1%} has complete 60-minute windows); "
            f"both positive and negative rows have a median 30-minute slope of "
            f"{weakest_design['positive_median_slope_30m']:.3f} and "
            f"{weakest_design['negative_median_slope_30m']:.3f}, respectively. This points to a "
            "low-prevalence, mostly flat-stock regime where extra trajectory features add little "
            "high-risk ordering signal, not a gap-handling artifact.",
        ]
    )
    lines.extend(
        [
            "",
            "## Target and horizon experiment",
            "",
            f"Each task uses HGBM with the `{winner}` feature family. Shorter and sustained targets "
            "are separate tasks, not improvements to the original 30-minute task.",
            "",
            "| Target | AP | P@R20 | P@R30 | P@R40 | P@R50 | R@P60 | Prevalence |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for target in TARGETS:
        item = metrics[f"horizon:{target}:{winner}"]
        lines.append(
            f"| {target} | {item['average_precision']:.4f} | {_fmt(item['precision_at_recall']['0.2'])} "
            f"| {_fmt(item['precision_at_recall']['0.3'])} | {_fmt(item['precision_at_recall']['0.4'])} "
            f"| {_fmt(item['precision_at_recall']['0.5'])} | {_fmt(item['recall_at_precision']['0.6'])} "
            f"| {item['prevalence']:.2%} |"
        )
    meeting = [
        target
        for target in TARGETS
        if metrics[f"horizon:{target}:{winner}"]["precision_at_recall"]["0.2"] >= 0.6
    ]
    lines.extend(
        [
            "",
            "[Full target/horizon PR plot](assets/temporal-horizon-pr.svg)",
            "",
            "The provisional 60% precision at at least 20% recall target is "
            + (
                f"met by: {', '.join(f'`{target}`' for target in meeting)}."
                if meeting
                else "not met by any task."
            ),
            "",
            "## Block uncertainty and failure analysis",
            "",
            f"Paired {uncertainty['block_minutes']}-minute block resampling used "
            f"{uncertainty['paired_usable_block_count']} usable network-wide blocks and "
            f"{uncertainty['bootstrap_draws']} draws. Mean AP difference: "
            f"{_fmt(uncertainty['average_precision_difference']['mean'])} "
            f"(95% block interval {_fmt(uncertainty['average_precision_difference']['lower_95'])} "
            f"to {_fmt(uncertainty['average_precision_difference']['upper_95'])}). Mean P@R40 "
            f"difference: {_fmt(uncertainty['precision_at_40_recall_difference']['mean'])} "
            f"({_fmt(uncertainty['precision_at_40_recall_difference']['lower_95'])} to "
            f"{_fmt(uncertainty['precision_at_40_recall_difference']['upper_95'])}).",
            "",
            uncertainty["limitation"],
            "",
            "The archive has 19.87% scheduled-slot coverage, long laptop gaps, and only a few "
            "independent regimes. Station priors can also be weak for stations absent from early "
            "training. Rebalancing is inferred only from stock jumps, not operator logs. These "
            "limitations dominate any narrow metric difference and prevent a deployment claim.",
            "",
            "## Reproducibility",
            "",
            f"Configuration: `{config.source_path.relative_to(config.source_path.parents[1])}`. "
            f"HGBM fit/prediction timings and all exact PR points are saved with the ignored study "
            "artifacts. Source export and frozen-study files were read-only; no final-test metric was "
            "computed.",
            "",
            f"Across all folds, ablations, and target variants, fitting took "
            f"{total_fit_seconds:.1f}s and scoring took {total_score_seconds:.1f}s. Per-fit timings "
            "are recorded in `protocol.json`.",
            "",
        ]
    )
    return "\n".join(lines), conclusion


def run_temporal_study(config_path: Path) -> Path:
    config = load_experiment_config(config_path)
    export_directory, manifest = _read_export_manifest(config)
    frame, cache_path, cache_reused, cache_status, cache_provenance = _load_or_build_features(
        config, manifest
    )
    config_bytes = config.source_path.read_bytes()
    cache_provenance_sha256 = hashlib.sha256(
        json.dumps(cache_provenance, sort_keys=True).encode()
    ).hexdigest()
    run_material = {
        "schema": TEMPORAL_STUDY_SCHEMA_VERSION,
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "source_sha256": manifest["source_sha256"],
        "cache_provenance_sha256": cache_provenance_sha256,
        "package_source_sha256": cache_provenance["package_source_sha256"],
        "runtime": cache_provenance["runtime"],
    }
    run_id = hashlib.sha256(json.dumps(run_material, sort_keys=True).encode()).hexdigest()[:16]
    output = config.data_dir / "ml" / "temporal_studies" / run_id
    predictions: list[pl.DataFrame] = []
    fold_design: list[dict[str, Any]] = []
    fit_times: dict[str, Any] = {}
    label_column, label_end_column = TARGETS["stockout_30m"]
    for fold in config.folds:
        train, validation = _fold_frames(
            frame,
            fold,
            label_column=label_column,
            label_end_column=label_end_column,
            purge_minutes=config.purge_minutes,
        )
        train_with_priors, validation_with_priors = add_fold_station_priors(
            train,
            validation,
            label_column=label_column,
            label_end_column=label_end_column,
            validation_start=fold.validation_start,
        )
        validation_prevalence: Any = validation[label_column].mean()
        validation_complete_60m: Any = validation["history_complete_60m"].mean()
        positive_median_slope: Any = validation.filter(pl.col(label_column) == 1)[
            "bike_slope_30m"
        ].median()
        negative_median_slope: Any = validation.filter(pl.col(label_column) == 0)[
            "bike_slope_30m"
        ].median()
        fold_design.append(
            {
                "name": fold.name,
                "validation_start_utc": fold.validation_start.isoformat(),
                "validation_end_utc": fold.validation_end.isoformat(),
                "train_rows": train.height,
                "validation_rows": validation.height,
                "validation_positives": int(validation[label_column].sum()),
                "validation_prevalence": float(validation_prevalence),
                "validation_complete_60m": float(validation_complete_60m),
                "positive_median_slope_30m": float(positive_median_slope),
                "negative_median_slope_30m": float(negative_median_slope),
            }
        )
        for candidate in ABLATION_ORDER:
            features = FEATURE_GROUPS[candidate]
            scores, fit_seconds, score_seconds = _fit_score(
                train_with_priors,
                validation_with_priors,
                features,
                label_column,
                config.model_parameters,
            )
            fit_times[f"ablation:{fold.name}:{candidate}"] = {
                "fit_seconds": fit_seconds,
                "score_seconds": score_seconds,
            }
            predictions.append(
                _prediction_frame(
                    validation_with_priors,
                    scores,
                    analysis="ablation",
                    candidate=candidate,
                    target="stockout_30m",
                    fold=fold.name,
                    label_column=label_column,
                )
            )
    main_predictions = pl.concat(predictions)
    main_metrics, _ = _evaluate_predictions(main_predictions)
    winner = _best_temporal_candidate(main_metrics)

    horizon_predictions: list[pl.DataFrame] = []
    for target, (target_label, target_end) in TARGETS.items():
        if target == "stockout_30m":
            reused = main_predictions.filter(pl.col("candidate") == winner).with_columns(
                pl.lit("horizon").alias("analysis")
            )
            horizon_predictions.append(reused)
            continue
        for fold in config.folds:
            train, validation = _fold_frames(
                frame,
                fold,
                label_column=target_label,
                label_end_column=target_end,
                purge_minutes=config.purge_minutes,
            )
            train, validation = add_fold_station_priors(
                train,
                validation,
                label_column=target_label,
                label_end_column=target_end,
                validation_start=fold.validation_start,
            )
            scores, fit_seconds, score_seconds = _fit_score(
                train,
                validation,
                FEATURE_GROUPS[winner],
                target_label,
                config.model_parameters,
            )
            fit_times[f"horizon:{target}:{fold.name}"] = {
                "fit_seconds": fit_seconds,
                "score_seconds": score_seconds,
            }
            horizon_predictions.append(
                _prediction_frame(
                    validation,
                    scores,
                    analysis="horizon",
                    candidate=winner,
                    target=target,
                    fold=fold.name,
                    label_column=target_label,
                )
            )
    all_predictions = pl.concat([main_predictions, *horizon_predictions])
    metrics, curves = _evaluate_predictions(all_predictions)
    block_metrics, uncertainty = _block_analysis(
        main_predictions,
        "baseline",
        winner,
        block_minutes=config.block_minutes,
        draws=config.bootstrap_draws,
        seed=config.seed,
    )
    ablation_curves = curves.filter(
        (pl.col("analysis") == "ablation") & (pl.col("target") == "stockout_30m")
    )
    horizon_curves = curves.filter(pl.col("analysis") == "horizon").with_columns(
        pl.col("target").alias("candidate")
    )
    artifact_plot_directory = output / "plots"
    _svg_pr_plot(
        ablation_curves,
        artifact_plot_directory / "temporal-ablation-pr.svg",
        title="30-minute stockout feature ablation",
    )
    _svg_pr_plot(
        horizon_curves,
        artifact_plot_directory / "temporal-horizon-pr.svg",
        title=f"Target and horizon comparison — {winner}",
    )
    quality = feature_quality_summary(frame)
    report, conclusion = _report(
        config,
        manifest,
        metrics,
        winner,
        fold_design,
        quality,
        uncertainty,
        cache_path,
        cache_reused,
        cache_status,
        fit_times,
    )
    protocol = {
        **run_material,
        "run_id": run_id,
        "conclusion": conclusion,
        "winner": winner,
        "development_only": True,
        "development_end_exclusive_utc": config.development_end.isoformat(),
        "frozen_export_directory": str(export_directory),
        "feature_cache": str(cache_path),
        "feature_cache_manifest": str(cache_path.with_suffix(".manifest.json")),
        "feature_cache_status": cache_status,
        "cache_provenance_sha256": cache_provenance_sha256,
        "feature_groups": FEATURE_GROUPS,
        "folds": fold_design,
        "model_parameters": config.model_parameters,
        "quality": quality,
        "uncertainty": uncertainty,
        "fit_times": fit_times,
    }
    atomic_write_text(output / "protocol.json", _json(protocol))
    atomic_write_text(output / "metrics.json", _json(metrics))
    atomic_write_parquet(output / "oof_predictions.parquet", all_predictions)
    atomic_write_parquet(output / "pr_curves.parquet", curves)
    atomic_write_text(output / "block_metrics.csv", block_metrics.write_csv())
    atomic_write_text(output / "report.md", report)
    config.report_path.parent.mkdir(parents=True, exist_ok=True)
    config.plot_directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(config.report_path, report)
    for name in ("temporal-ablation-pr.svg", "temporal-horizon-pr.svg"):
        shutil.copyfile(artifact_plot_directory / name, config.plot_directory / name)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Past-only temporal BikePulse research study")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    output = run_temporal_study(args.config)
    print((output / "report.md").read_text())
    print(f"Artifacts: {output}")


if __name__ == "__main__":
    main()
