from __future__ import annotations

import fcntl
import hashlib
import io
import json
import platform
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import joblib  # type: ignore[import-untyped]
import numpy as np
import polars as pl
import sklearn  # type: ignore[import-untyped]
from numpy.typing import NDArray
from sklearn.ensemble import HistGradientBoostingClassifier  # type: ignore[import-untyped]
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]
from sklearn.metrics import (  # type: ignore[import-untyped]
    average_precision_score,
    brier_score_loss,
    f1_score,
    log_loss,
    precision_score,
    recall_score,
)
from sklearn.pipeline import Pipeline  # type: ignore[import-untyped]
from sklearn.preprocessing import StandardScaler  # type: ignore[import-untyped]
from threadpoolctl import threadpool_limits  # type: ignore[import-untyped]

from bikepulse.data.compaction import load_history, rebuild_history
from bikepulse.io import atomic_write_bytes, atomic_write_parquet, atomic_write_text
from bikepulse.ml.dataset import FEATURES, canonical_grid, feature_rows
from bikepulse.ml.export import GEOGRAPHIC_FEATURES, geographic_features
from bikepulse.ml.study import choose_threshold

FROZEN_STUDY_SCHEMA_VERSION = "frozen-empty-bike-study-v1"
FROZEN_STAGE_SCHEMA_VERSION = "frozen-stage-v1"
SEED = 42
THREAD_LIMIT = 4
TOP_BUDGET_FRACTION = 0.05
PRACTICAL_AP_GAIN = 0.01
BOOTSTRAP_DRAWS = 1_000
BOOTSTRAP_BLOCK_MINUTES = 60

CORE_FEATURES = list(FEATURES)
ALL_FEATURES = [*CORE_FEATURES, *GEOGRAPHIC_FEATURES]
MODEL_CANDIDATES = ("logistic_core", "logistic_geo", "hist_core", "hist_geo")
BASELINE_CANDIDATES = ("constant", "inventory_baseline", "bike_ratio_baseline")


@dataclass(frozen=True)
class FrozenExport:
    directory: Path
    manifest: dict[str, Any]
    full: pl.DataFrame
    enriched: pl.DataFrame


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str) + "\n"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _request_sha256(request: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True, default=str).encode()).hexdigest()


def _stage_manifest_path(output: Path, stage: str) -> Path:
    return output / "stages" / f"{stage}.json"


def _verify_completed_stage(output: Path, stage: str, request: dict[str, Any]) -> bool:
    path = _stage_manifest_path(output, stage)
    if not path.exists():
        return False
    manifest = _read_json(path)
    if manifest.get("schema_version") != FROZEN_STAGE_SCHEMA_VERSION:
        raise ValueError(f"Frozen-study {stage} manifest uses an unsupported schema.")
    if manifest.get("stage") != stage:
        raise ValueError(f"Frozen-study {stage} manifest has the wrong stage.")
    if manifest.get("request_sha256") != _request_sha256(request):
        raise ValueError(f"Frozen-study {stage} request conflicts with completed evidence.")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError(f"Frozen-study {stage} manifest has no artifact hashes.")
    for relative, expected in artifacts.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise ValueError(f"Frozen-study {stage} manifest has invalid artifact metadata.")
        artifact = output / relative
        if not artifact.is_file() or _sha256(artifact) != expected:
            raise ValueError(f"Frozen-study {stage} evidence changed: {artifact}")
    return True


def _complete_stage(
    output: Path, stage: str, request: dict[str, Any], artifacts: list[Path]
) -> None:
    hashes: dict[str, str] = {}
    for artifact in artifacts:
        if not artifact.is_file() or not artifact.is_relative_to(output):
            raise ValueError(f"Frozen-study {stage} output is missing: {artifact}")
        hashes[artifact.relative_to(output).as_posix()] = _sha256(artifact)
    manifest = {
        "schema_version": FROZEN_STAGE_SCHEMA_VERSION,
        "stage": stage,
        "request": request,
        "request_sha256": _request_sha256(request),
        "artifacts": hashes,
    }
    atomic_write_bytes(
        _stage_manifest_path(output, stage),
        _json_text(manifest).encode(),
        overwrite=False,
    )


def _require_completed_stage(output: Path, stage: str, request: dict[str, Any]) -> None:
    if _verify_completed_stage(output, stage, request):
        return
    stage_directory = output / "stages"
    if (output / "protocol.json").exists() and not (
        stage_directory.exists() and any(stage_directory.glob("*.json"))
    ):
        raise ValueError(
            f"Frozen study has no verified {stage} stage manifest. Existing legacy or "
            "interrupted studies are read-only; inspect their saved report."
        )
    raise ValueError(f"Frozen study must complete {stage} before this command.")


def _ensure_stage_outputs_absent(stage: str, artifacts: list[Path]) -> None:
    existing = [str(path) for path in artifacts if path.exists()]
    if existing:
        raise ValueError(
            f"Frozen-study {stage} has incomplete unverified outputs; refusing to overwrite: "
            + ", ".join(existing)
        )


@contextmanager
def _study_lock(output: Path) -> Iterator[None]:
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".lifecycle.lock").open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another frozen-study mutation is already in progress.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _prepared_request(protocol: dict[str, Any], study_id: str) -> dict[str, Any]:
    return {
        "study_id": study_id,
        "export_id": protocol["export_id"],
        "export_artifacts": protocol["export_artifacts"],
        "source_code_sha256": protocol["source_code_sha256"],
    }


def _protocol_request(output: Path, study_id: str, **values: Any) -> dict[str, Any]:
    return {
        "study_id": study_id,
        "protocol_sha256": _sha256(output / "protocol.json"),
        **values,
    }


def _source_fingerprint() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).parents[1]
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _frozen_root(data_dir: Path) -> Path:
    return data_dir / "ml" / "frozen_studies"


def _study_directory(data_dir: Path, study_id: str) -> Path:
    if not study_id or Path(study_id).name != study_id:
        raise ValueError("Study ID must be a single directory name.")
    return _frozen_root(data_dir) / study_id


def _export_directory(data_dir: Path, export_id: str) -> Path:
    if not export_id or Path(export_id).name != export_id:
        raise ValueError("Export ID must be a single directory name.")
    return data_dir / "ml" / "exports" / export_id


def _parse_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} is missing from the export manifest.")
    parsed = datetime.fromisoformat(value).astimezone(UTC)
    return parsed


def _artifact_hashes(manifest: dict[str, Any]) -> dict[str, str]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not all(
        isinstance(name, str) and isinstance(digest, str) for name, digest in artifacts.items()
    ):
        raise ValueError("Export manifest does not contain valid artifact hashes.")
    return artifacts


def _matrix(frame: pl.DataFrame, features: list[str]) -> NDArray[np.float64]:
    return np.asarray(frame.select(features).to_numpy(), dtype=np.float64)


def _scalar_float(value: Any, field: str) -> float:
    if not isinstance(value, (int, float, np.integer, np.floating)):
        raise ValueError(f"Expected numeric value for {field}.")
    return float(value)


def _finite_features(frame: pl.DataFrame, features: list[str]) -> bool:
    return bool(np.isfinite(_matrix(frame, features)).all())


def _validate_export(data_dir: Path, export_id: str) -> FrozenExport:
    directory = _export_directory(data_dir, export_id)
    manifest = _read_json(directory / "manifest.json")
    for name, digest in _artifact_hashes(manifest).items():
        artifact = directory / name
        if not artifact.exists() or _sha256(artifact) != digest:
            raise ValueError(f"Export artifact hash mismatch: {artifact}")
    required = {"full_labeled.parquet", "enriched_train.parquet", "sources.json"}
    if not required.issubset(_artifact_hashes(manifest)):
        raise ValueError("Export manifest is missing required source artifacts.")
    full = pl.read_parquet(directory / "full_labeled.parquet")
    enriched = pl.read_parquet(directory / "enriched_train.parquet")
    features = manifest.get("features")
    if features != ALL_FEATURES:
        raise ValueError(
            "Frozen export feature schema does not match the geographic study contract."
        )
    required_columns = {
        "station_id",
        "as_of_utc",
        "label_end_utc",
        "snapshot_id",
        "label",
        "split",
        *ALL_FEATURES,
    }
    if not required_columns.issubset(full.columns):
        raise ValueError("Full frozen export is missing required columns.")
    if full.is_empty() or full.select(pl.col("label").is_null().any()).item():
        raise ValueError("Full frozen export must contain complete labels.")
    if not set(full["label"].unique().to_list()).issubset({0, 1}):
        raise ValueError("Frozen labels must be binary.")
    if full.select(pl.struct("station_id", "as_of_utc").is_duplicated().any()).item():
        raise ValueError("Frozen export contains duplicate station-time keys.")
    if not _finite_features(full, ALL_FEATURES):
        raise ValueError("Frozen export contains missing or non-finite feature values.")
    expected_splits = {"train", "validation", "test", "purged"}
    if set(full["split"].unique().to_list()) != expected_splits:
        raise ValueError("Frozen export split assignments are incomplete.")
    validation_start = _parse_utc(manifest.get("validation_start_utc"), "validation_start_utc")
    test_start = _parse_utc(manifest.get("test_start_utc"), "test_start_utc")
    valid_train = (pl.col("as_of_utc") < validation_start) & (
        pl.col("label_end_utc") < validation_start
    )
    valid_validation = (pl.col("as_of_utc") >= validation_start) & (
        pl.col("label_end_utc") < test_start
    )
    valid_test = pl.col("as_of_utc") >= test_start
    violations = full.filter(
        ((pl.col("split") == "train") & ~valid_train)
        | ((pl.col("split") == "validation") & ~valid_validation)
        | ((pl.col("split") == "test") & ~valid_test)
        | ((pl.col("split") == "purged") & (valid_train | valid_validation | valid_test))
    )
    if not violations.is_empty():
        raise ValueError("Frozen export violates the declared chronological split contract.")
    train = full.filter(pl.col("split") == "train")
    key_columns = ["station_id", "as_of_utc"]
    if not enriched.filter(pl.col("split") != "train").is_empty():
        raise ValueError("Enriched training view includes non-training rows.")
    if not enriched.join(train.select(key_columns), on=key_columns, how="anti").is_empty():
        raise ValueError("Enriched training view contains rows outside the full training split.")
    all_positives = train.filter(pl.col("label") == 1).select(key_columns)
    enriched_positives = enriched.filter(pl.col("label") == 1).select(key_columns)
    if not all_positives.join(enriched_positives, on=key_columns, how="anti").is_empty():
        raise ValueError("Enriched training view is missing a positive training row.")
    positive_count = enriched.filter(pl.col("label") == 1).height
    negative_count = enriched.filter(pl.col("label") == 0).height
    if positive_count == 0 or negative_count != positive_count * 9:
        raise ValueError("Enriched training view does not preserve the declared 1:9 ratio.")
    for split in ("train", "validation", "test"):
        if full.filter(pl.col("split") == split)["label"].n_unique() != 2:
            raise ValueError(f"Frozen {split} split requires both label classes.")
    return FrozenExport(directory=directory, manifest=manifest, full=full, enriched=enriched)


def _candidate_definitions() -> dict[str, dict[str, Any]]:
    return {
        "logistic_core": {
            "kind": "logistic",
            "features": CORE_FEATURES,
            "parameters": {"C": 1.0, "max_iter": 1000, "random_state": SEED, "class_weight": None},
        },
        "logistic_geo": {
            "kind": "logistic",
            "features": ALL_FEATURES,
            "parameters": {"C": 1.0, "max_iter": 1000, "random_state": SEED, "class_weight": None},
        },
        "hist_core": {
            "kind": "histogram_gradient_boosting",
            "features": CORE_FEATURES,
            "parameters": {
                "learning_rate": 0.1,
                "max_iter": 100,
                "max_leaf_nodes": 15,
                "min_samples_leaf": 100,
                "l2_regularization": 1.0,
                "early_stopping": False,
                "random_state": SEED,
                "class_weight": None,
            },
        },
        "hist_geo": {
            "kind": "histogram_gradient_boosting",
            "features": ALL_FEATURES,
            "parameters": {
                "learning_rate": 0.1,
                "max_iter": 100,
                "max_leaf_nodes": 15,
                "min_samples_leaf": 100,
                "l2_regularization": 1.0,
                "early_stopping": False,
                "random_state": SEED,
                "class_weight": None,
            },
        },
    }


def _protocol(export: FrozenExport) -> dict[str, Any]:
    return {
        "schema_version": FROZEN_STUDY_SCHEMA_VERSION,
        "export_id": export.manifest["run_id"],
        "export_artifacts": _artifact_hashes(export.manifest),
        "export_source_sha256": export.manifest["source_sha256"],
        "features": {"core": CORE_FEATURES, "geographic": GEOGRAPHIC_FEATURES},
        "candidates": _candidate_definitions(),
        "baselines": {
            "constant": "natural training prevalence",
            "inventory_baseline": "1 / (available_bikes + 1), fixed bikes <= 3 warning",
            "bike_ratio_baseline": "1 - available_bikes / capacity",
        },
        "selection_rule": {
            "main_metric": "average_precision",
            "minimum_incremental_ap_gain": PRACTICAL_AP_GAIN,
            "top_budget_fraction": TOP_BUDGET_FRACTION,
            "threshold_rule": "validation F1 maximum, ties by precision then higher threshold",
            "default_training_data": "full natural train split",
            "probability_treatment": "uncalibrated; report reliability diagnostics",
        },
        "test_protocol": {
            "bootstrap_draws": BOOTSTRAP_DRAWS,
            "temporal_block_minutes": BOOTSTRAP_BLOCK_MINUTES,
            "bootstrap_seed": SEED,
        },
        "source_code_sha256": _source_fingerprint(),
        "python_version": platform.python_version(),
        "numpy_version": np.__version__,
        "polars_version": pl.__version__,
        "sklearn_version": sklearn.__version__,
        "joblib_version": joblib.__version__,
    }


def _count_labels(frame: pl.DataFrame) -> list[dict[str, Any]]:
    return frame.group_by("split", "label").len().sort("split", "label").to_dicts()


def _coverage(frame: pl.DataFrame) -> list[dict[str, Any]]:
    return (
        frame.filter(pl.col("split").is_in(["train", "validation"]))
        .with_columns(pl.col("as_of_utc").dt.truncate("1h").alias("hour_utc"))
        .group_by("split", "hour_utc")
        .agg(pl.len().alias("row_count"), pl.col("label").sum().alias("positive_count"))
        .sort("split", "hour_utc")
        .to_dicts()
    )


def _data_review(export: FrozenExport) -> dict[str, Any]:
    full = export.full
    train = full.filter(pl.col("split") == "train")
    validation = full.filter(pl.col("split") == "validation")
    feature_ranges = {
        feature: {
            "minimum": _scalar_float(train[feature].min(), f"{feature} minimum"),
            "maximum": _scalar_float(train[feature].max(), f"{feature} maximum"),
            "unique_count": int(train[feature].n_unique()),
        }
        for feature in ALL_FEATURES
    }
    return {
        "export_id": export.manifest["run_id"],
        "partition_counts": _count_labels(full),
        "train_station_count": train["station_id"].n_unique(),
        "validation_station_count": validation["station_id"].n_unique(),
        "train_prevalence": _scalar_float(train["label"].mean(), "train prevalence"),
        "validation_prevalence": _scalar_float(validation["label"].mean(), "validation prevalence"),
        "null_counts": full.select(pl.all().null_count()).to_dicts()[0],
        "feature_ranges": feature_ranges,
        "constant_features": [
            feature for feature, values in feature_ranges.items() if values["unique_count"] <= 1
        ],
        "coverage_by_hour": _coverage(full),
        "test_counts_for_integrity_only": _count_labels(full.filter(pl.col("split") == "test")),
        "limitations": [
            "Collection gaps remain gaps and the rows are temporally dependent.",
            "Collected periods are weekdays, so weekend and seasonal performance are unknown.",
            "Geographic bike-count movement can reflect operator rebalancing as well as cycling "
            "activity.",
        ],
    }


def _review_markdown(review: dict[str, Any]) -> str:
    rows = review["partition_counts"]
    lines = [
        "# Frozen study data review",
        "",
        f"Export: `{review['export_id']}`.",
        "",
        "| Partition | Label | Rows |",
        "|---|---:|---:|",
    ]
    lines.extend(f"| {row['split']} | {row['label']} | {row['len']} |" for row in rows)
    lines.extend(
        [
            "",
            f"Training prevalence: {review['train_prevalence']:.5%}.",
            f"Validation prevalence: {review['validation_prevalence']:.5%}.",
            "",
            "## Constant training features",
            "",
            ", ".join(review["constant_features"]) or "None.",
            "",
            "## Limitations",
            "",
            *[f"- {item}" for item in review["limitations"]],
            "",
        ]
    )
    return "\n".join(lines)


def prepare_frozen_study(data_dir: Path, export_id: str) -> Path:
    export = _validate_export(data_dir, export_id)
    protocol = _protocol(export)
    study_id = hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()[:16]
    output = _study_directory(data_dir, study_id)
    request = _prepared_request(protocol, study_id)
    if _verify_completed_stage(output, "prepared", request):
        return output
    if output.exists() and any(output.iterdir()):
        raise ValueError(
            "Frozen study directory already contains legacy or interrupted evidence; "
            "refusing to overwrite it."
        )
    with _study_lock(output):
        if _verify_completed_stage(output, "prepared", request):
            return output
        artifacts = [
            output / "protocol.json",
            output / "data_review.json",
            output / "data_review.md",
        ]
        _ensure_stage_outputs_absent("prepared", artifacts)
        review = _data_review(export)
        atomic_write_text(artifacts[0], _json_text({**protocol, "study_id": study_id}))
        atomic_write_text(artifacts[1], _json_text(review))
        atomic_write_text(artifacts[2], _review_markdown(review))
        atomic_write_text(
            output / "state.json",
            _json_text({"status": "prepared", "study_id": study_id, "export_id": export_id}),
        )
        atomic_write_text(
            data_dir / "ml" / "latest_frozen.json", _json_text({"study_id": study_id})
        )
        _complete_stage(output, "prepared", request, artifacts)
    return output


def _load_study(data_dir: Path, study_id: str) -> tuple[Path, dict[str, Any], FrozenExport]:
    output = _study_directory(data_dir, study_id)
    protocol = _read_json(output / "protocol.json")
    if protocol.get("study_id") != study_id:
        raise ValueError("Frozen study protocol does not match the requested study ID.")
    export_id = protocol.get("export_id")
    if not isinstance(export_id, str):
        raise ValueError("Frozen study protocol has no export ID.")
    export = _validate_export(data_dir, export_id)
    if protocol.get("export_artifacts") != _artifact_hashes(export.manifest):
        raise ValueError("Frozen study input artifacts differ from the recorded protocol.")
    return output, protocol, export


def _fit_model(definition: dict[str, Any], train: pl.DataFrame) -> Any:
    features = definition["features"]
    parameters = definition["parameters"]
    if definition["kind"] == "logistic":
        model: Any = Pipeline(
            [
                ("scale", StandardScaler()),
                ("classifier", LogisticRegression(**parameters)),
            ]
        )
    else:
        model = HistGradientBoostingClassifier(**parameters)
    with threadpool_limits(limits=THREAD_LIMIT):
        model.fit(_matrix(train, features), train["label"].to_numpy())
    return model


def _score_model(model: Any, frame: pl.DataFrame, features: list[str]) -> NDArray[np.float64]:
    with threadpool_limits(limits=THREAD_LIMIT):
        batches = [
            model.predict_proba(_matrix(batch, features))[:, 1]
            for batch in frame.iter_slices(n_rows=100_000)
        ]
    return np.concatenate(batches).astype(np.float64)


def _dump_model(path: Path, value: dict[str, Any]) -> str:
    buffer = io.BytesIO()
    joblib.dump(value, buffer)
    content = buffer.getvalue()
    atomic_write_bytes(path, content, overwrite=True)
    return hashlib.sha256(content).hexdigest()


def _load_model(path: Path, expected_hash: str) -> dict[str, Any]:
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise ValueError(f"Model artifact checksum mismatch: {path}")
    artifact = joblib.load(io.BytesIO(content))
    if not isinstance(artifact, dict):
        raise ValueError(f"Model artifact is invalid: {path}")
    return artifact


def _reliability(labels: NDArray[np.int_], scores: NDArray[np.float64]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(10):
        lower = index / 10
        upper = (index + 1) / 10
        mask = (scores >= lower) & ((scores < upper) if index < 9 else (scores <= upper))
        if mask.any():
            rows.append(
                {
                    "lower": lower,
                    "upper": upper,
                    "row_count": int(mask.sum()),
                    "mean_predicted_probability": float(scores[mask].mean()),
                    "observed_prevalence": float(labels[mask].mean()),
                }
            )
    return rows


def _top_budget_metrics(frame: pl.DataFrame, scores: NDArray[np.float64]) -> dict[str, Any]:
    labels = np.asarray(frame["label"].to_numpy(), dtype=np.int_)
    station_ids = np.asarray(frame["station_id"].to_list(), dtype=str)
    times = frame["as_of_utc"].to_list()
    selected = np.zeros(len(labels), dtype=bool)
    positions_by_time: dict[datetime, list[int]] = {}
    for index, stamp in enumerate(times):
        positions_by_time.setdefault(stamp, []).append(index)
    for positions in positions_by_time.values():
        rows = np.asarray(positions, dtype=int)
        order = np.lexsort((station_ids[rows], -scores[rows]))
        count = int(np.ceil(len(rows) * TOP_BUDGET_FRACTION))
        selected[rows[order[:count]]] = True
    positives = int(labels.sum())
    selected_positive = int(labels[selected].sum())
    return {
        "budget_fraction": TOP_BUDGET_FRACTION,
        "alert_count": int(selected.sum()),
        "positive_alert_count": selected_positive,
        "precision": float(selected_positive / selected.sum()) if selected.any() else 0.0,
        "recall": float(selected_positive / positives) if positives else 0.0,
    }


def _metrics(
    labels: NDArray[np.int_],
    scores: NDArray[np.float64],
    threshold: float | None,
    frame: pl.DataFrame,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "average_precision": float(average_precision_score(labels, scores)),
        "brier_score": float(brier_score_loss(labels, scores)),
        "log_loss": float(log_loss(labels, scores, labels=[0, 1])),
        "row_count": len(labels),
        "positive_row_count": int(labels.sum()),
        "prevalence": float(labels.mean()),
        "top_budget": _top_budget_metrics(frame, scores),
        "reliability": _reliability(labels, scores),
        "threshold": threshold,
    }
    if threshold is None:
        metrics["warning"] = None
    else:
        alerts = scores >= threshold
        metrics["warning"] = {
            "precision": float(precision_score(labels, alerts, zero_division=0)),
            "recall": float(recall_score(labels, alerts, zero_division=0)),
            "f1": float(f1_score(labels, alerts, zero_division=0)),
            "warning_rate": float(alerts.mean()),
            "alert_count": int(alerts.sum()),
        }
    return metrics


def _baseline_scores(
    frame: pl.DataFrame, train_prevalence: float
) -> dict[str, NDArray[np.float64]]:
    bikes = np.asarray(frame["available_bikes"].to_numpy(), dtype=np.float64)
    ratios = np.asarray(frame["bike_ratio"].to_numpy(), dtype=np.float64)
    return {
        "constant": np.full(frame.height, train_prevalence, dtype=np.float64),
        "inventory_baseline": np.clip(1.0 / (bikes + 1.0), 0.0, 1.0),
        "bike_ratio_baseline": np.clip(1.0 - ratios, 0.0, 1.0),
    }


def _recommended_candidate(metrics: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ordered = ["inventory_baseline", "bike_ratio_baseline", *MODEL_CANDIDATES]
    selected = "inventory_baseline"
    decisions: list[dict[str, Any]] = []
    for candidate in ordered[1:]:
        gain = metrics[candidate]["average_precision"] - metrics[selected]["average_precision"]
        accept = gain >= PRACTICAL_AP_GAIN
        decisions.append(
            {
                "candidate": candidate,
                "compared_with": selected,
                "ap_gain": gain,
                "accepted": accept,
            }
        )
        if accept:
            selected = candidate
    return {
        "candidate": selected,
        "threshold": metrics[selected]["threshold"],
        "rule": "incremental validation AP gain of at least 0.01 over the simpler prior candidate",
        "decisions": decisions,
    }


def _segment_ids(times: list[datetime]) -> dict[datetime, int]:
    current = 0
    mapping: dict[datetime, int] = {}
    previous: datetime | None = None
    for stamp in sorted(set(times)):
        if previous is not None and stamp - previous > timedelta(minutes=5):
            current += 1
        mapping[stamp] = current
        previous = stamp
    return mapping


def _validation_errors(
    frame: pl.DataFrame, scores: NDArray[np.float64], threshold: float | None
) -> pl.DataFrame:
    labels = np.asarray(frame["label"].to_numpy(), dtype=np.int_)
    alerts = np.zeros(frame.height, dtype=bool) if threshold is None else scores >= threshold
    outcomes = np.where(
        (labels == 1) & alerts,
        "true_positive",
        np.where(
            (labels == 0) & alerts,
            "false_positive",
            np.where(labels == 1, "false_negative", "true_negative"),
        ),
    )
    selected = frame.select(
        "station_id",
        "as_of_utc",
        "available_bikes",
        "available_docks",
        "bike_change_15m",
        "neighbor_station_count_1km",
        "nearby_abs_net_bike_change_15m",
        "nearby_observed_count_15m",
        "label",
    ).with_columns(
        pl.Series("score", scores),
        pl.Series("alert", alerts),
        pl.Series("outcome", outcomes),
    )
    segment_map = _segment_ids(selected["as_of_utc"].to_list())
    selected = selected.with_columns(
        pl.Series(
            "collection_segment", [segment_map[stamp] for stamp in selected["as_of_utc"].to_list()]
        )
    )
    examples: list[pl.DataFrame] = []
    for outcome in ("true_positive", "false_positive", "false_negative", "true_negative"):
        rows = selected.filter(pl.col("outcome") == outcome)
        by_segment = [
            rows.filter(pl.col("collection_segment") == segment).sort(["as_of_utc", "station_id"])
            for segment in rows["collection_segment"].unique().sort().to_list()
        ]
        chosen: list[pl.DataFrame] = []
        offset = 0
        while len(chosen) < 10:
            added = False
            for segment_rows in by_segment:
                if offset < segment_rows.height:
                    chosen.append(segment_rows.slice(offset, 1))
                    added = True
                    if len(chosen) == 10:
                        break
            if not added:
                break
            offset += 1
        if chosen:
            examples.append(pl.concat(chosen))
    if not examples:
        return selected.head(0)
    return pl.concat(examples).sort(["outcome", "collection_segment", "as_of_utc", "station_id"])


def _numeric_edges(values: NDArray[np.float64]) -> list[float]:
    edges = np.quantile(values, [0.25, 0.5, 0.75]).tolist()
    return [
        float(edge) for index, edge in enumerate(edges) if index == 0 or edge > edges[index - 1]
    ]


def _numeric_bucket(value: float, edges: list[float]) -> str:
    for index, edge in enumerate(edges):
        if value <= edge:
            return f"q{index + 1}_le_{edge:.3f}"
    return f"q{len(edges) + 1}_gt_{edges[-1]:.3f}" if edges else "all"


def _subgroups(
    train: pl.DataFrame,
    validation: pl.DataFrame,
    scores: NDArray[np.float64],
    threshold: float | None,
) -> tuple[pl.DataFrame, dict[str, list[float]]]:
    numeric = ["available_bikes", "neighbor_station_count_1km", "nearby_abs_net_bike_change_15m"]
    edges = {
        feature: _numeric_edges(np.asarray(train[feature].to_numpy(), dtype=np.float64))
        for feature in numeric
    }
    local_hours = validation["as_of_utc"].dt.convert_time_zone("Europe/Paris").dt.hour().to_list()
    labels = np.asarray(validation["label"].to_numpy(), dtype=np.int_)
    rows: list[dict[str, Any]] = []
    slice_values: dict[str, list[str]] = {
        "paris_hour": [f"hour_{hour:02d}" for hour in local_hours],
    }
    for feature in numeric:
        values = np.asarray(validation[feature].to_numpy(), dtype=np.float64)
        slice_values[feature] = [_numeric_bucket(value, edges[feature]) for value in values]
    for slice_name, buckets in slice_values.items():
        for bucket in sorted(set(buckets)):
            mask = np.asarray([value == bucket for value in buckets], dtype=bool)
            slice_labels = labels[mask]
            slice_scores = scores[mask]
            record: dict[str, Any] = {
                "slice": slice_name,
                "bucket": bucket,
                "row_count": int(mask.sum()),
                "positive_row_count": int(slice_labels.sum()),
                "prevalence": float(slice_labels.mean()),
                "average_precision": None,
                "precision": None,
                "recall": None,
            }
            if np.unique(slice_labels).size == 2:
                record["average_precision"] = float(
                    average_precision_score(slice_labels, slice_scores)
                )
                if threshold is not None:
                    alert = slice_scores >= threshold
                    record["precision"] = float(
                        precision_score(slice_labels, alert, zero_division=0)
                    )
                    record["recall"] = float(recall_score(slice_labels, alert, zero_division=0))
            rows.append(record)
    return pl.DataFrame(rows), edges


def _validation_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Frozen study validation report",
        "",
        "Validation results only. Test predictions and metrics have not been generated.",
        "",
        "| Candidate | AP | Brier | Threshold | Top 5% precision | Top 5% recall |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for candidate, metrics in result["metrics"].items():
        budget = metrics["top_budget"]
        threshold = metrics["threshold"]
        lines.append(
            f"| {candidate} | {metrics['average_precision']:.4f} | {metrics['brier_score']:.4f} "
            f"| {'—' if threshold is None else f'{threshold:.4f}'} | "
            f"{budget['precision']:.4f} | {budget['recall']:.4f} |"
        )
    selection = result["recommended_selection"]
    lines.extend(
        [
            "",
            f"Default-rule recommendation: `{selection['candidate']}`.",
            "",
            "See `validation_errors.csv` and `subgroups.csv` before accepting this recommendation.",
            "Scores are uncalibrated and the observed collection periods have major gaps.",
            "",
        ]
    )
    return "\n".join(lines)


def _run_frozen_study_unlocked(data_dir: Path, study_id: str, *, mode: str) -> Path:
    output, protocol, export = _load_study(data_dir, study_id)
    definitions = _candidate_definitions()
    train = export.full.filter(pl.col("split") == "train").sort("as_of_utc", "station_id")
    validation = export.full.filter(pl.col("split") == "validation").sort("as_of_utc", "station_id")
    if mode == "smoke":
        definition = definitions["logistic_core"]
        model = _fit_model(definition, export.enriched)
        artifact = {
            "candidate": "logistic_core_enriched_smoke",
            "features": definition["features"],
            "model": model,
            "protocol_sha256": hashlib.sha256(_json_text(protocol).encode()).hexdigest(),
        }
        model_path = output / "smoke" / "model.joblib"
        digest = _dump_model(model_path, artifact)
        reloaded = _load_model(model_path, digest)
        score = _score_model(reloaded["model"], validation.head(100), reloaded["features"])
        if score.shape != (min(100, validation.height),) or not np.isfinite(score).all():
            raise ValueError("Frozen-study smoke model could not score the validation schema.")
        atomic_write_text(
            output / "smoke" / "report.json",
            _json_text(
                {
                    "status": "complete",
                    "training_rows": export.enriched.height,
                    "positive_rows": export.enriched.filter(pl.col("label") == 1).height,
                    "model_sha256": digest,
                    "validation_rows_scored": len(score),
                    "claim": "engineering smoke check only; no model-quality claim",
                }
            ),
        )
        return output

    labels = np.asarray(validation["label"].to_numpy(), dtype=np.int_)
    train_prevalence = _scalar_float(train["label"].mean(), "train prevalence")
    scores = _baseline_scores(validation, train_prevalence)
    thresholds: dict[str, float | None] = {
        "constant": None,
        "inventory_baseline": 0.25,
        "bike_ratio_baseline": choose_threshold(labels, scores["bike_ratio_baseline"]),
    }
    artifacts: dict[str, dict[str, Any]] = {}
    fit_seconds: dict[str, float] = {}
    for candidate in MODEL_CANDIDATES:
        definition = definitions[candidate]
        started = time.perf_counter()
        model = _fit_model(definition, train)
        fit_seconds[candidate] = time.perf_counter() - started
        model_path = output / "models" / f"{candidate}.joblib"
        artifact = {
            "candidate": candidate,
            "features": definition["features"],
            "model": model,
            "protocol_sha256": hashlib.sha256(_json_text(protocol).encode()).hexdigest(),
        }
        digest = _dump_model(model_path, artifact)
        artifacts[candidate] = {
            "path": model_path.name,
            "sha256": digest,
            "features": definition["features"],
        }
        scores[candidate] = _score_model(model, validation, definition["features"])
        thresholds[candidate] = choose_threshold(labels, scores[candidate])
    metrics = {
        candidate: _metrics(labels, score, thresholds[candidate], validation)
        for candidate, score in scores.items()
    }
    recommendation = _recommended_candidate(metrics)
    selected = recommendation["candidate"]
    selected_scores = scores[selected]
    selected_threshold = recommendation["threshold"]
    errors = _validation_errors(validation, selected_scores, selected_threshold)
    subgroups, edges = _subgroups(train, validation, selected_scores, selected_threshold)
    prediction_frame = validation.select(
        "station_id", "as_of_utc", "snapshot_id", "label", *ALL_FEATURES
    )
    for candidate, score in scores.items():
        prediction_frame = prediction_frame.with_columns(pl.Series(f"score_{candidate}", score))
    result = {
        "status": "validation_complete",
        "study_id": study_id,
        "export_id": protocol["export_id"],
        "training_row_count": train.height,
        "training_prevalence": train_prevalence,
        "validation_row_count": validation.height,
        "metrics": metrics,
        "model_artifacts": artifacts,
        "fit_seconds": fit_seconds,
        "recommended_selection": recommendation,
        "subgroup_training_edges": edges,
        "claim": "validation-only comparison; probabilities are uncalibrated",
    }
    atomic_write_parquet(output / "validation_predictions.parquet", prediction_frame)
    atomic_write_text(output / "validation_results.json", _json_text(result))
    atomic_write_text(output / "validation_report.md", _validation_markdown(result))
    atomic_write_text(output / "validation_errors.csv", errors.write_csv())
    atomic_write_text(output / "subgroups.csv", subgroups.write_csv())
    atomic_write_text(
        output / "state.json",
        _json_text(
            {
                "status": "validation_complete",
                "study_id": study_id,
                "export_id": protocol["export_id"],
            }
        ),
    )
    return output


def run_frozen_study(data_dir: Path, study_id: str, *, mode: str) -> Path:
    if mode not in {"smoke", "full"}:
        raise ValueError("Frozen study mode must be 'smoke' or 'full'.")
    output, protocol, _ = _load_study(data_dir, study_id)
    _require_completed_stage(output, "prepared", _prepared_request(protocol, study_id))
    stage = "smoke" if mode == "smoke" else "validation"
    request = _protocol_request(output, study_id, mode=mode)
    if _verify_completed_stage(output, stage, request):
        return output
    if any(_stage_manifest_path(output, name).exists() for name in ("locked", "test", "packaged")):
        raise ValueError("Frozen-study fitting is forbidden after selection is locked.")
    if mode == "smoke":
        artifacts = [output / "smoke" / "model.joblib", output / "smoke" / "report.json"]
    else:
        artifacts = [
            *(output / "models" / f"{candidate}.joblib" for candidate in MODEL_CANDIDATES),
            output / "validation_predictions.parquet",
            output / "validation_results.json",
            output / "validation_report.md",
            output / "validation_errors.csv",
            output / "subgroups.csv",
        ]
    with _study_lock(output):
        if _verify_completed_stage(output, stage, request):
            return output
        if any(
            _stage_manifest_path(output, name).exists() for name in ("locked", "test", "packaged")
        ):
            raise ValueError("Frozen-study fitting is forbidden after selection is locked.")
        _ensure_stage_outputs_absent(stage, artifacts)
        result = _run_frozen_study_unlocked(data_dir, study_id, mode=mode)
        _complete_stage(output, stage, request, artifacts)
        return result


def frozen_report(data_dir: Path, study_id: str) -> Path:
    output = _study_directory(data_dir, study_id)
    for name in ("test_report.md", "validation_report.md", "data_review.md"):
        candidate = output / name
        if candidate.exists():
            return candidate
    raise ValueError("Frozen study has no readable report yet. Run frozen-prepare first.")


def _lock_frozen_study_unlocked(data_dir: Path, study_id: str, candidate: str) -> Path:
    output, protocol, _ = _load_study(data_dir, study_id)
    results = _read_json(output / "validation_results.json")
    metrics = results.get("metrics")
    if not isinstance(metrics, dict) or candidate not in metrics:
        raise ValueError("Candidate is not available in the frozen validation results.")
    candidate_metrics = metrics[candidate]
    if not isinstance(candidate_metrics, dict):
        raise ValueError("Candidate validation metrics are invalid.")
    model_artifacts = results.get("model_artifacts", {})
    artifact: dict[str, Any] | None = None
    if candidate in model_artifacts:
        current = model_artifacts[candidate]
        if not isinstance(current, dict):
            raise ValueError("Candidate model artifact metadata is invalid.")
        model_path = output / "models" / str(current["path"])
        if _sha256(model_path) != current["sha256"]:
            raise ValueError("Candidate model artifact no longer matches validation evidence.")
        artifact = current
    selection = {
        "study_id": study_id,
        "export_id": protocol["export_id"],
        "candidate": candidate,
        "threshold": candidate_metrics["threshold"],
        "threshold_rule": protocol["selection_rule"]["threshold_rule"],
        "protocol_sha256": hashlib.sha256(_json_text(protocol).encode()).hexdigest(),
        "model_artifact": artifact,
        "selection_basis": "default guided-plan rule recorded after validation",
        "validation_average_precision": candidate_metrics["average_precision"],
    }
    atomic_write_text(output / "selection.json", _json_text(selection))
    atomic_write_text(
        output / "state.json",
        _json_text({"status": "locked", "study_id": study_id, "candidate": candidate}),
    )
    return output


def lock_frozen_study(data_dir: Path, study_id: str, candidate: str) -> Path:
    output, protocol, _ = _load_study(data_dir, study_id)
    _require_completed_stage(output, "prepared", _prepared_request(protocol, study_id))
    validation_request = _protocol_request(output, study_id, mode="full")
    _require_completed_stage(output, "validation", validation_request)
    request = _protocol_request(
        output,
        study_id,
        candidate=candidate,
        validation_manifest_sha256=_sha256(_stage_manifest_path(output, "validation")),
    )
    if _verify_completed_stage(output, "locked", request):
        return output
    if any(_stage_manifest_path(output, name).exists() for name in ("test", "packaged")):
        raise ValueError("Frozen-study selection cannot change after final-test evidence exists.")
    artifacts = [output / "selection.json"]
    with _study_lock(output):
        if _verify_completed_stage(output, "locked", request):
            return output
        if any(_stage_manifest_path(output, name).exists() for name in ("test", "packaged")):
            raise ValueError(
                "Frozen-study selection cannot change after final-test evidence exists."
            )
        _ensure_stage_outputs_absent("locked", artifacts)
        result = _lock_frozen_study_unlocked(data_dir, study_id, candidate)
        _complete_stage(output, "locked", request, artifacts)
        return result


def _candidate_test_score(
    output: Path,
    candidate: str,
    frame: pl.DataFrame,
    train_prevalence: float,
    model_artifacts: dict[str, Any],
) -> NDArray[np.float64]:
    baseline = _baseline_scores(frame, train_prevalence)
    if candidate in baseline:
        return baseline[candidate]
    metadata = model_artifacts.get(candidate)
    if not isinstance(metadata, dict):
        raise ValueError(f"Missing model artifact metadata for {candidate}.")
    artifact = _load_model(output / "models" / str(metadata["path"]), str(metadata["sha256"]))
    features = artifact.get("features")
    if not isinstance(features, list) or any(not isinstance(feature, str) for feature in features):
        raise ValueError(f"Model feature schema is invalid for {candidate}.")
    return _score_model(artifact["model"], frame, features)


def _block_assignments(times: list[datetime], block_minutes: int) -> tuple[NDArray[np.int_], int]:
    positions_by_time: dict[datetime, list[int]] = {}
    for index, stamp in enumerate(times):
        positions_by_time.setdefault(stamp, []).append(index)
    assignments = np.zeros(len(times), dtype=np.int_)
    current_block = -1
    previous: datetime | None = None
    slots = max(1, block_minutes // 5)
    slot_in_segment = 0
    for stamp in sorted(positions_by_time):
        if previous is None or stamp - previous > timedelta(minutes=5):
            slot_in_segment = 0
            current_block += 1
        elif slot_in_segment % slots == 0:
            current_block += 1
        for index in positions_by_time[stamp]:
            assignments[index] = current_block
        slot_in_segment += 1
        previous = stamp
    return assignments, current_block + 1


def _bootstrap_ap_differences(
    labels: NDArray[np.int_],
    scores: dict[str, NDArray[np.float64]],
    assignments: NDArray[np.int_],
    pairs: dict[str, tuple[str, str]],
) -> dict[str, Any]:
    block_rows = [
        np.flatnonzero(assignments == block) for block in range(int(assignments.max()) + 1)
    ]
    if len(block_rows) < 10:
        return {"status": "insufficient_blocks", "block_count": len(block_rows), "pairs": {}}
    generator = np.random.default_rng(SEED)
    values: dict[str, list[float]] = {name: [] for name in pairs}
    invalid = 0
    for _ in range(BOOTSTRAP_DRAWS):
        selected_blocks = generator.integers(0, len(block_rows), size=len(block_rows))
        indexes = np.concatenate([block_rows[int(block)] for block in selected_blocks])
        draw_labels = labels[indexes]
        if draw_labels.min() == draw_labels.max():
            invalid += 1
            continue
        draw_ap = {
            candidate: float(average_precision_score(draw_labels, score[indexes]))
            for candidate, score in scores.items()
        }
        for name, (left, right) in pairs.items():
            values[name].append(draw_ap[left] - draw_ap[right])
    pairs_report = {
        name: {
            "valid_draw_count": len(draws),
            "difference_mean": float(np.mean(draws)) if draws else None,
            "difference_ci_95": [float(np.quantile(draws, 0.025)), float(np.quantile(draws, 0.975))]
            if draws
            else None,
        }
        for name, draws in values.items()
    }
    return {
        "status": "complete" if all(values.values()) else "insufficient_valid_draws",
        "block_count": len(block_rows),
        "draw_count": BOOTSTRAP_DRAWS,
        "invalid_draw_count": invalid,
        "pairs": pairs_report,
    }


def _block_count(times: list[datetime], block_minutes: int) -> int:
    return _block_assignments(times, block_minutes)[1]


def _test_markdown(result: dict[str, Any]) -> str:
    selection = result["selection"]
    selected = result["metrics"][selection["candidate"]]
    inventory = result["metrics"]["inventory_baseline"]
    lines = [
        "# Frozen study final test report",
        "",
        f"Selected candidate: `{selection['candidate']}`.",
        "",
        "| Approach | AP | Precision | Recall | Alert count |",
        "|---|---:|---:|---:|---:|",
    ]
    for candidate in dict.fromkeys(("inventory_baseline", selection["candidate"])):
        metrics = result["metrics"][candidate]
        warning = metrics["warning"] or {"precision": 0, "recall": 0, "alert_count": 0}
        lines.append(
            f"| {candidate} | {metrics['average_precision']:.4f} | {warning['precision']:.4f} "
            f"| {warning['recall']:.4f} | {warning['alert_count']} |"
        )
    lines.extend(
        [
            "",
            f"Selected AP: {selected['average_precision']:.4f}; "
            f"inventory AP: {inventory['average_precision']:.4f}.",
            "",
            "Results describe the observed, gap-affected weekday collection periods. Scores are "
            "uncalibrated.",
            "",
        ]
    )
    return "\n".join(lines)


def _run_frozen_test_unlocked(data_dir: Path, study_id: str) -> Path:
    output, _protocol, export = _load_study(data_dir, study_id)
    selection = _read_json(output / "selection.json")
    validation = _read_json(output / "validation_results.json")
    candidate = selection.get("candidate")
    if not isinstance(candidate, str):
        raise ValueError("Frozen study selection has no candidate.")
    metrics_validation = validation.get("metrics")
    model_artifacts = validation.get("model_artifacts")
    if not isinstance(metrics_validation, dict) or not isinstance(model_artifacts, dict):
        raise ValueError("Frozen study validation artifacts are invalid.")
    test = export.full.filter(pl.col("split") == "test").sort("as_of_utc", "station_id")
    train = export.full.filter(pl.col("split") == "train")
    train_prevalence = _scalar_float(train["label"].mean(), "train prevalence")
    candidate_ids = [*BASELINE_CANDIDATES, *MODEL_CANDIDATES]
    scores = {
        candidate_id: _candidate_test_score(
            output, candidate_id, test, train_prevalence, model_artifacts
        )
        for candidate_id in candidate_ids
    }
    labels = np.asarray(test["label"].to_numpy(), dtype=np.int_)
    test_metrics = {
        candidate_id: _metrics(
            labels,
            scores[candidate_id],
            metrics_validation[candidate_id]["threshold"],
            test,
        )
        for candidate_id in candidate_ids
    }
    prediction = test.select("station_id", "as_of_utc", "snapshot_id", "label", *ALL_FEATURES)
    for candidate_id, score in scores.items():
        prediction = prediction.with_columns(pl.Series(f"score_{candidate_id}", score))
    selected_threshold = selection.get("threshold")
    if selected_threshold is not None and not isinstance(selected_threshold, float):
        raise ValueError("Frozen selection threshold is invalid.")
    selected_score = scores[candidate]
    selected_alert = (
        np.zeros(test.height, dtype=bool)
        if selected_threshold is None
        else selected_score >= selected_threshold
    )
    prediction = prediction.with_columns(
        pl.Series("selected_score", selected_score),
        pl.Series("selected_alert", selected_alert),
    )
    assignments, block_count = _block_assignments(
        test["as_of_utc"].to_list(), BOOTSTRAP_BLOCK_MINUTES
    )
    pairs = {
        "selected_minus_inventory": (candidate, "inventory_baseline"),
        "logistic_geo_minus_logistic_core": ("logistic_geo", "logistic_core"),
        "hist_geo_minus_hist_core": ("hist_geo", "hist_core"),
    }
    bootstrap = _bootstrap_ap_differences(labels, scores, assignments, pairs)
    bootstrap["block_length_sensitivity"] = {
        str(minutes): _block_count(test["as_of_utc"].to_list(), minutes)
        for minutes in (30, 60, 120)
    }
    result = {
        "status": "test_complete",
        "study_id": study_id,
        "selection": selection,
        "metrics": test_metrics,
        "bootstrap": bootstrap,
        "test_row_count": test.height,
        "test_block_count": block_count,
        "claim": "fixed-selection test evaluation; gap-affected weekday collection periods only",
    }
    atomic_write_parquet(output / "test_predictions.parquet", prediction)
    atomic_write_text(output / "test_metrics.json", _json_text(result))
    atomic_write_text(output / "test_report.md", _test_markdown(result))
    atomic_write_text(
        output / "state.json",
        _json_text({"status": "test_complete", "study_id": study_id, "candidate": candidate}),
    )
    return output


def run_frozen_test(data_dir: Path, study_id: str) -> Path:
    output, protocol, _ = _load_study(data_dir, study_id)
    _require_completed_stage(output, "prepared", _prepared_request(protocol, study_id))
    selection = _read_json(output / "selection.json")
    candidate = selection.get("candidate")
    if not isinstance(candidate, str):
        raise ValueError("Frozen study selection has no candidate.")
    locked_request = _protocol_request(
        output,
        study_id,
        candidate=candidate,
        validation_manifest_sha256=_sha256(_stage_manifest_path(output, "validation")),
    )
    _require_completed_stage(output, "locked", locked_request)
    request = _protocol_request(
        output,
        study_id,
        selection_sha256=_sha256(output / "selection.json"),
        locked_manifest_sha256=_sha256(_stage_manifest_path(output, "locked")),
        validation_manifest_sha256=_sha256(_stage_manifest_path(output, "validation")),
    )
    if _verify_completed_stage(output, "test", request):
        return output
    attempt_path = output / "stages" / "test_attempt.json"
    if attempt_path.exists():
        raise ValueError(
            "A final-test attempt exists without verified completion. Refusing to score the "
            "frozen test automatically; inspect and preserve the interrupted evidence."
        )
    artifacts = [
        output / "test_predictions.parquet",
        output / "test_metrics.json",
        output / "test_report.md",
    ]
    with _study_lock(output):
        if _verify_completed_stage(output, "test", request):
            return output
        if attempt_path.exists():
            raise ValueError(
                "A final-test attempt exists without verified completion. Refusing to score the "
                "frozen test automatically; inspect and preserve the interrupted evidence."
            )
        _ensure_stage_outputs_absent("test", artifacts)
        atomic_write_bytes(
            attempt_path,
            _json_text(
                {
                    "schema_version": FROZEN_STAGE_SCHEMA_VERSION,
                    "stage": "test_attempt",
                    "request": request,
                    "request_sha256": _request_sha256(request),
                }
            ).encode(),
            overwrite=False,
        )
        result = _run_frozen_test_unlocked(data_dir, study_id)
        _complete_stage(output, "test", request, artifacts)
        return result


def replay_frozen_study(
    data_dir: Path, study_id: str, station_id: str, as_of: datetime
) -> dict[str, Any]:
    output, _, export = _load_study(data_dir, study_id)
    selection = _read_json(output / "selection.json")
    validation = _read_json(output / "validation_results.json")
    as_of = as_of.astimezone(UTC)
    if as_of.second or as_of.microsecond or as_of.minute % 5:
        raise ValueError("Replay time must be an exact five-minute UTC boundary.")
    rebuild_history(data_dir, as_of - timedelta(minutes=20), as_of + timedelta(minutes=5))
    history = load_history(data_dir, as_of - timedelta(minutes=20), as_of + timedelta(minutes=5))
    grid = canonical_grid(history, as_of - timedelta(minutes=15), as_of + timedelta(minutes=5))
    features = feature_rows(grid)
    geography = geographic_features(features)
    current = features.join(
        geography.drop("latitude", "longitude"), on=["station_id", "as_of_utc"], how="left"
    ).filter((pl.col("station_id") == station_id) & (pl.col("as_of_utc") == as_of))
    result: dict[str, Any] = {
        "study_id": study_id,
        "station_id": station_id,
        "as_of_utc": as_of.isoformat(),
        "status": "unavailable",
    }
    if current.is_empty():
        return {**result, "reason": "missing or stale historical observation"}
    rejection = current["feature_rejection"].item()
    if rejection == "already_empty":
        return {**result, "status": "Empty now"}
    if rejection is not None:
        return {**result, "reason": str(rejection)}
    candidate = selection.get("candidate")
    if not isinstance(candidate, str):
        raise ValueError("Frozen study selection is invalid.")
    model_artifacts = validation.get("model_artifacts")
    if not isinstance(model_artifacts, dict):
        raise ValueError("Frozen study model metadata is invalid.")
    train_prevalence = _scalar_float(
        export.full.filter(pl.col("split") == "train")["label"].mean(), "train prevalence"
    )
    score = float(
        _candidate_test_score(output, candidate, current, train_prevalence, model_artifacts)[0]
    )
    reference = export.full.filter(
        (pl.col("station_id") == station_id) & (pl.col("as_of_utc") == as_of)
    )
    parity: bool | None = None
    if not reference.is_empty():
        parity = bool(
            np.isclose(
                _matrix(current, ALL_FEATURES), _matrix(reference, ALL_FEATURES), rtol=0, atol=1e-12
            ).all()
        )
    threshold = selection.get("threshold")
    return {
        **result,
        "status": "historical replay",
        "candidate": candidate,
        "score": score,
        "threshold": threshold,
        "alert": False if threshold is None else score >= threshold,
        "feature_parity_with_export": parity,
        "source_snapshot_id": current["snapshot_id"].item(),
        "received_at_utc": current["received_at_utc"].item().isoformat(),
        "claim": "historical, uncalibrated research replay",
    }


def _package_frozen_study_unlocked(data_dir: Path, study_id: str) -> Path:
    output, protocol, _ = _load_study(data_dir, study_id)
    selection = _read_json(output / "selection.json")
    validation = _read_json(output / "validation_results.json")
    release = output / "release"
    required = [
        "protocol.json",
        "data_review.md",
        "validation_report.md",
        "test_report.md",
        "selection.json",
    ]
    copied: dict[str, str] = {}
    for name in required:
        source = output / name
        content = source.read_bytes()
        atomic_write_bytes(release / name, content, overwrite=True)
        copied[name] = hashlib.sha256(content).hexdigest()
    artifact = selection.get("model_artifact")
    if isinstance(artifact, dict):
        source = output / "models" / str(artifact["path"])
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != artifact["sha256"]:
            raise ValueError("Selected model changed after test evaluation.")
        atomic_write_bytes(release / "model.joblib", content, overwrite=True)
        copied["model.joblib"] = hashlib.sha256(content).hexdigest()
    prediction = pl.read_parquet(output / "test_predictions.parquet").head(100)
    candidate = selection["candidate"]
    expected = np.asarray(prediction["selected_score"].to_numpy(), dtype=np.float64)
    if isinstance(artifact, dict):
        loaded = _load_model(release / "model.joblib", copied["model.joblib"])
        actual = _score_model(loaded["model"], prediction, loaded["features"])
    else:
        prevalence = _scalar_float(validation.get("training_prevalence"), "training prevalence")
        actual = _candidate_test_score(release, candidate, prediction, prevalence, {})
    if not np.allclose(actual, expected, rtol=0, atol=1e-12):
        raise ValueError("Release package cannot reproduce saved predictions.")
    check = {
        "study_id": study_id,
        "candidate": candidate,
        "checked_prediction_rows": len(expected),
        "prediction_tolerance": 1e-12,
        "reproduced": True,
        "protocol_sha256": hashlib.sha256(_json_text(protocol).encode()).hexdigest(),
    }
    atomic_write_text(release / "package_check.json", _json_text(check))
    copied["package_check.json"] = _sha256(release / "package_check.json")
    atomic_write_text(release / "release_manifest.json", _json_text({"artifacts": copied}))
    atomic_write_text(
        output / "state.json",
        _json_text({"status": "packaged", "study_id": study_id, "candidate": candidate}),
    )
    return release


def package_frozen_study(data_dir: Path, study_id: str) -> Path:
    output, protocol, _ = _load_study(data_dir, study_id)
    _require_completed_stage(output, "prepared", _prepared_request(protocol, study_id))
    selection = _read_json(output / "selection.json")
    candidate = selection.get("candidate")
    if not isinstance(candidate, str):
        raise ValueError("Frozen study selection has no candidate.")
    test_request = _protocol_request(
        output,
        study_id,
        selection_sha256=_sha256(output / "selection.json"),
        locked_manifest_sha256=_sha256(_stage_manifest_path(output, "locked")),
        validation_manifest_sha256=_sha256(_stage_manifest_path(output, "validation")),
    )
    _require_completed_stage(output, "test", test_request)
    request = _protocol_request(
        output,
        study_id,
        selection_sha256=_sha256(output / "selection.json"),
        test_manifest_sha256=_sha256(_stage_manifest_path(output, "test")),
        test_metrics_sha256=_sha256(output / "test_metrics.json"),
    )
    release = output / "release"
    if _verify_completed_stage(output, "packaged", request):
        return release
    artifacts = [
        *(
            release / name
            for name in (
                "protocol.json",
                "data_review.md",
                "validation_report.md",
                "test_report.md",
                "selection.json",
                "package_check.json",
                "release_manifest.json",
            )
        ),
    ]
    if isinstance(selection.get("model_artifact"), dict):
        artifacts.append(release / "model.joblib")
    with _study_lock(output):
        if _verify_completed_stage(output, "packaged", request):
            return release
        _ensure_stage_outputs_absent("packaged", artifacts)
        result = _package_frozen_study_unlocked(data_dir, study_id)
        _complete_stage(output, "packaged", request, artifacts)
        return result
