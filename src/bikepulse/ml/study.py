from __future__ import annotations

import hashlib
import io
import json
import platform
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import joblib  # type: ignore[import-untyped]
import numpy as np
import polars as pl
import sklearn  # type: ignore[import-untyped]
from numpy.typing import NDArray
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]
from sklearn.metrics import (  # type: ignore[import-untyped]
    average_precision_score,
    precision_recall_curve,
    precision_score,
    recall_score,
)
from sklearn.pipeline import Pipeline  # type: ignore[import-untyped]
from sklearn.preprocessing import StandardScaler  # type: ignore[import-untyped]
from threadpoolctl import threadpool_limits  # type: ignore[import-untyped]

from bikepulse.data.compaction import load_history, rebuild_history
from bikepulse.data.quality import build_daily_quality_report
from bikepulse.io import atomic_write_bytes, atomic_write_parquet, atomic_write_text
from bikepulse.ml.dataset import (
    FEATURE_SCHEMA_VERSION,
    FEATURES,
    build_dataset,
    canonical_grid,
    feature_rows,
    partition_dataset,
)

TRAINING_LIMIT = 1_000_000


def choose_threshold(labels: NDArray[Any], probabilities: NDArray[Any]) -> float:
    precision, recall, thresholds = precision_recall_curve(labels, probabilities)
    f1 = np.divide(
        2 * precision[:-1] * recall[:-1],
        precision[:-1] + recall[:-1],
        out=np.zeros_like(precision[:-1]),
        where=(precision[:-1] + recall[:-1]) > 0,
    )
    best = max(range(len(thresholds)), key=lambda i: (f1[i], precision[i], thresholds[i]))
    return float(thresholds[best])


def evaluate(labels: NDArray[Any], scores: NDArray[Any], threshold: float) -> dict[str, Any]:
    alerts = scores >= threshold
    return {
        "average_precision": float(average_precision_score(labels, scores))
        if labels.any()
        else None,
        "precision": float(precision_score(labels, alerts, zero_division=0)),
        "recall": float(recall_score(labels, alerts, zero_division=0)),
        "threshold": threshold,
        "row_count": len(labels),
        "positive_row_count": int(labels.sum()),
        "prevalence": float(labels.mean()),
        "alert_count": int(alerts.sum()),
    }


def _source_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parents[1].rglob("*.py")):
        digest.update(path.relative_to(Path(__file__).parents[1]).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _matrix(frame: pl.DataFrame) -> NDArray[np.float64]:
    return np.asarray(frame.select(FEATURES).to_numpy(), dtype=np.float64)


def _score(model: Any, frame: pl.DataFrame) -> NDArray[np.float64]:
    with threadpool_limits(limits=4):
        return np.concatenate(
            [model.predict_proba(_matrix(batch))[:, 1] for batch in frame.iter_slices(100_000)]
        )


def run_study(
    data_dir: Path,
    start: datetime,
    end: datetime,
    *,
    max_feed_age_seconds: int = 300,
    max_station_age_seconds: int = 300,
) -> Path:
    if (
        start.utcoffset() != timedelta(0)
        or start.hour
        or start.minute
        or start.second
        or start.microsecond
        or end - start != timedelta(days=14)
    ):
        raise ValueError("Choose exactly 14 UTC days; END is exclusive (START + 14 days).")
    if end > datetime.now(UTC):
        raise ValueError("END must be a completed UTC date; future observations are unavailable.")
    rebuild_history(data_dir, start - timedelta(minutes=20), end)
    work = data_dir / "ml" / "datasets" / f"{start:%Y-%m-%d}_{end:%Y-%m-%d}"
    dataset_digest = hashlib.sha256()
    quality: list[dict[str, Any]] = []
    excluded_legacy = 0
    missing_feed_timestamp_rows = 0
    files = []
    for offset in range(14):
        day = start + timedelta(days=offset)
        history = load_history(
            data_dir, day - timedelta(minutes=20), day + timedelta(days=1, minutes=30)
        )
        # Receipt times are required. Legacy history is never silently treated as timely.
        excluded_legacy += history.filter(
            (pl.col("collected_at_utc") >= day)
            & (pl.col("collected_at_utc") < day + timedelta(days=1))
            & pl.col("received_at_utc").is_null()
        ).height
        missing_feed_timestamp_rows += history.filter(
            (pl.col("collected_at_utc") >= day)
            & (pl.col("collected_at_utc") < day + timedelta(days=1))
            & pl.col("feed_updated_at_utc").is_null()
        ).height
        grid = canonical_grid(
            history,
            day - timedelta(minutes=15),
            day + timedelta(days=1, minutes=30),
            max_feed_age_seconds=max_feed_age_seconds,
        )
        dataset = (
            partition_dataset(build_dataset(grid), start)
            .filter((pl.col("as_of_utc") >= day) & (pl.col("as_of_utc") < day + timedelta(days=1)))
            .sort("as_of_utc", "station_id")
        )
        path = work / f"{day:%Y-%m-%d}.parquet"
        atomic_write_parquet(path, dataset)
        files.append(path)
        dataset_digest.update(path.read_bytes())
        quality.append(
            build_daily_quality_report(
                data_dir=data_dir,
                day=day.date(),
                max_station_age_seconds=max_station_age_seconds,
                max_feed_age_seconds=max_feed_age_seconds,
            )
        )
    config = {
        "start_utc": start.isoformat(),
        "end_exclusive_utc": end.isoformat(),
        "validation_start_utc": (start + timedelta(days=8)).isoformat(),
        "test_start_utc": (start + timedelta(days=11)).isoformat(),
        "features": FEATURES,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "C": 1.0,
        "seed": 42,
        "max_iter": 1000,
        "threshold_rule": "validation F1 maximum, ties by precision then threshold",
        "freshness_basis": "published feed update and local receipt time",
        "max_feed_age_seconds": max_feed_age_seconds,
        "max_station_age_seconds_diagnostic": max_station_age_seconds,
        "max_training_rows": TRAINING_LIMIT,
        "dataset_sha256": dataset_digest.hexdigest(),
        "source_sha256": _source_fingerprint(),
        "python_version": platform.python_version(),
        "sklearn_version": sklearn.__version__,
        "numpy_version": np.__version__,
        "polars_version": pl.__version__,
        "joblib_version": joblib.__version__,
    }
    run_id = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
    output = data_dir / "ml" / "experiments" / run_id
    atomic_write_text(output / "manifest.json", json.dumps(config, indent=2))
    lazy = pl.scan_parquet(files)
    summary = (
        lazy.group_by("partition", "rejection_reason")
        .len()
        .sort("partition", "rejection_reason")
        .collect()
    )
    report: dict[str, Any] = {
        "status": "insufficient_data",
        "claim": "published-feed stockout; preliminary; research only; uncalibrated",
        "run_id": run_id,
        "config": config,
        "partition_counts": summary.to_dicts(),
        "excluded_legacy_rows": excluded_legacy,
        "missing_feed_timestamp_rows": missing_feed_timestamp_rows,
        "collection_quality": quality,
        "limitation": (
            "Stockouts are observed in fresh published-feed snapshots, not at the station sensor. "
            "Complete five-minute windows are required; laptop gaps limit generalization."
        ),
    }
    sets = {
        name: lazy.filter(pl.col("partition") == name)
        .select("station_id", "as_of_utc", "snapshot_id", "label", *FEATURES)
        .collect()
        for name in ("train", "validation", "test")
    }
    reasons = [
        f"{name} needs both positive and negative labeled rows"
        for name, frame in sets.items()
        if frame["label"].n_unique() < 2
    ]
    if reasons:
        report["reasons"] = reasons
    else:
        train = sets["train"]
        if train.height > TRAINING_LIMIT:
            train = train.sample(n=TRAINING_LIMIT, seed=42).sort("as_of_utc", "station_id")
        model = Pipeline(
            [
                ("scale", StandardScaler()),
                ("classifier", LogisticRegression(C=1, max_iter=1000, random_state=42)),
            ]
        )
        with threadpool_limits(limits=4):
            model.fit(_matrix(train), train["label"].to_numpy())
        validation = sets["validation"]
        threshold = choose_threshold(validation["label"].to_numpy(), _score(model, validation))
        test = sets["test"]
        predictions = _score(model, test)
        baseline = 1.0 / (test["available_bikes"].to_numpy() + 1)
        labels = test["label"].to_numpy()
        model_metrics = evaluate(labels, predictions, threshold)
        baseline_metrics = evaluate(labels, baseline, 0.25)  # Equivalent to bikes <= 3.
        report.update(
            {
                "status": "complete",
                "training_rows_used": train.height,
                "logistic_regression": model_metrics,
                "inventory_baseline": baseline_metrics,
                "model_beats_baseline_ap": model_metrics["average_precision"]
                > baseline_metrics["average_precision"],
            }
        )
        atomic_write_parquet(
            output / "test_predictions.parquet",
            test.select(
                "station_id", "as_of_utc", "snapshot_id", "label", "available_bikes"
            ).with_columns(
                pl.Series("probability", predictions), pl.Series("baseline_score", baseline)
            ),
        )
        buffer = io.BytesIO()
        joblib.dump(
            {
                "model": model,
                "threshold": threshold,
                "features": FEATURES,
                "schema_version": FEATURE_SCHEMA_VERSION,
            },
            buffer,
        )
        binary = buffer.getvalue()
        atomic_write_bytes(output / "model.joblib", binary, overwrite=True)
        report["model_sha256"] = hashlib.sha256(binary).hexdigest()
    atomic_write_text(output / "report.json", json.dumps(report, indent=2))
    lines = [
        "# Empty-bike ML POC",
        "",
        f"Status: **{report['status']}**",
        "",
        "Preliminary published-feed stockout results. Probabilities are not calibrated.",
        "",
        f"UTC period: {start.date()} through {end.date()} (end exclusive).",
        "",
        f"Legacy rows excluded: {excluded_legacy}.",
        f"Rows missing a feed update timestamp: {missing_feed_timestamp_rows}.",
        "",
        "## Dataset",
        "",
        "```json",
        json.dumps(report["partition_counts"], indent=2),
        "```",
        "",
    ]
    if report["status"] == "complete":
        lines.extend(
            [
                "## Test comparison",
                "",
                "| Approach | Average precision | Precision | Recall |",
                "|---|---:|---:|---:|",
            ]
        )
        for name in ("inventory_baseline", "logistic_regression"):
            metrics = report[name]
            lines.append(
                f"| {name} | {metrics['average_precision']:.4f} "
                f"| {metrics['precision']:.4f} | {metrics['recall']:.4f} |"
            )
        lines.extend(
            [
                "",
                f"Logistic regression exceeds baseline AP: {report['model_beats_baseline_ap']}.",
                "Thresholds differ; alert precision/recall are not compared at equal alert load.",
            ]
        )
    else:
        lines.extend(["## Data blocker", "", *report["reasons"]])
    lines.extend(["", report["limitation"]])
    atomic_write_text(output / "report.md", "\n".join(lines) + "\n")
    atomic_write_text(data_dir / "ml" / "latest.json", json.dumps({"run_id": run_id}))
    return output


def predict_station(
    data_dir: Path,
    station_id: str,
    *,
    now: datetime | None = None,
    max_feed_age_seconds: int = 300,
) -> dict[str, Any]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    boundary = now.replace(second=0, microsecond=0, minute=(now.minute // 5) * 5)
    rebuild_history(data_dir, boundary - timedelta(minutes=20), now)
    history = load_history(
        data_dir, boundary - timedelta(minutes=20), now + timedelta(microseconds=1)
    )
    grid = canonical_grid(
        history,
        boundary - timedelta(minutes=15),
        boundary + timedelta(minutes=5),
        max_feed_age_seconds=max_feed_age_seconds,
    )
    current = feature_rows(grid).filter(
        (pl.col("station_id") == station_id) & (pl.col("as_of_utc") == boundary)
    )
    result: dict[str, Any] = {
        "station_id": station_id,
        "as_of_utc": boundary.isoformat(),
        "status": "unavailable",
    }
    if current.is_empty():
        return {**result, "reason": "missing or old collected observation"}
    # Even an already-empty station must come from a fresh feed at invocation time.
    feed_age = (now - current["feed_updated_at_utc"].item()).total_seconds()
    source_age = (now - current["received_at_utc"].item()).total_seconds()
    if feed_age < 0 or feed_age > max_feed_age_seconds or source_age > max_feed_age_seconds:
        return {**result, "reason": "observation became stale since boundary"}
    rejection = current["feature_rejection"].item()
    if rejection == "already_empty":
        return {**result, "status": "Empty now"}
    if rejection is not None:
        return {**result, "reason": rejection}
    latest = data_dir / "ml" / "latest.json"
    if not latest.exists():
        return {**result, "reason": "no completed model study"}
    try:
        output = data_dir / "ml" / "experiments" / json.loads(latest.read_text())["run_id"]
        report = json.loads((output / "report.json").read_text())
    except (OSError, ValueError, KeyError):
        return {**result, "reason": "model report missing or invalid"}
    if report["status"] != "complete":
        return {**result, "reason": "latest study has insufficient data"}
    try:
        binary = (output / "model.joblib").read_bytes()
    except OSError:
        return {**result, "reason": "model artifact missing or unreadable"}
    if hashlib.sha256(binary).hexdigest() != report.get("model_sha256"):
        return {**result, "reason": "model artifact checksum mismatch"}
    artifact = joblib.load(io.BytesIO(binary))
    if artifact["features"] != FEATURES or artifact["schema_version"] != FEATURE_SCHEMA_VERSION:
        return {**result, "reason": "model feature schema mismatch"}
    if report["config"].get("max_feed_age_seconds") != max_feed_age_seconds:
        return {**result, "reason": "model freshness configuration mismatch"}
    probability = float(_score(artifact["model"], current)[0])
    return {
        **result,
        "status": "experimental prediction",
        "empty_within_30m_probability": probability,
        "outcome_window_end_utc": (boundary + timedelta(minutes=30)).isoformat(),
        "alert_threshold": artifact["threshold"],
        "alert": probability >= artifact["threshold"],
        "run_id": report["run_id"],
        "source_snapshot_id": current["snapshot_id"].item(),
        "received_at_utc": current["received_at_utc"].item().isoformat(),
        "feed_updated_at_utc": current["feed_updated_at_utc"].item().isoformat(),
        "station_last_reported_utc": current["station_last_reported_utc"].item().isoformat(),
        "claim": "published-feed stockout; research only; preliminary; uncalibrated",
    }
