from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from bikepulse.data.compaction import OBSERVATION_SCHEMA
from bikepulse.io import atomic_write_parquet, atomic_write_text
from bikepulse.ml.temporal_study import (
    Fold,
    _block_analysis,
    _cache_key,
    _cache_provenance,
    _evaluate_predictions,
    _fit_score,
    _fold_frames,
    _svg_pr_plot,
    load_experiment_config,
    run_temporal_study,
)

START = datetime(2026, 10, 1, tzinfo=UTC)


def _label_frame() -> pl.DataFrame:
    rows = []
    for offset in range(0, 180, 5):
        for station_index, station in enumerate(("a", "b")):
            rows.append(
                {
                    "station_id": station,
                    "as_of_utc": START + timedelta(minutes=offset),
                    "label_end": START + timedelta(minutes=offset + 30),
                    "label": int((offset // 5 + station_index) % 7 == 0),
                    "available_bikes": 1 + (offset // 5 + station_index) % 9,
                    "bike_ratio": (1 + (offset // 5 + station_index) % 9) / 10,
                }
            )
    return pl.DataFrame(rows)


def test_temporal_config_records_three_development_only_folds() -> None:
    config = load_experiment_config(Path("experiments/temporal-history.toml"))

    assert config.development_end == datetime(2026, 10, 14, tzinfo=UTC)
    assert config.purge_minutes == 30
    assert len(config.folds) == 3
    assert "class_weight" not in config.model_parameters
    assert all(fold.validation_end <= config.development_end for fold in config.folds)


def test_fold_selection_purges_boundaries_and_keeps_snapshots_together() -> None:
    frame = _label_frame()
    fold = Fold(
        "example",
        START + timedelta(minutes=60),
        START + timedelta(minutes=150),
    )
    train, validation = _fold_frames(
        frame,
        fold,
        label_column="label",
        label_end_column="label_end",
        purge_minutes=30,
    )

    assert train["as_of_utc"].max() < fold.validation_start - timedelta(minutes=30)
    assert train["label_end"].max() < fold.validation_start
    assert validation["label_end"].max() < fold.validation_end
    assert validation.group_by("as_of_utc").len()["len"].unique().to_list() == [2]


def test_hgbm_and_metrics_preserve_natural_validation_rows() -> None:
    frame = _label_frame()
    train = frame.head(44)
    validation = frame.tail(20)
    scores, fit_seconds, score_seconds = _fit_score(
        train,
        validation,
        ["available_bikes", "bike_ratio"],
        "label",
        {
            "max_iter": 5,
            "max_leaf_nodes": 3,
            "min_samples_leaf": 2,
            "early_stopping": False,
            "random_state": 42,
        },
    )
    predictions = validation.select("station_id", "as_of_utc", "label").with_columns(
        pl.Series("score", scores),
        pl.lit("ablation").alias("analysis"),
        pl.lit("baseline").alias("candidate"),
        pl.lit("stockout_30m").alias("target"),
        pl.lit("fold").alias("fold"),
    )
    summaries, curves = _evaluate_predictions(predictions)
    metrics = summaries["ablation:stockout_30m:baseline"]

    assert fit_seconds >= 0
    assert score_seconds >= 0
    assert len(scores) == validation.height
    assert metrics["row_count"] == validation.height
    assert metrics["positive_count"] == validation["label"].sum()
    assert metrics["alert_budgets"]["0.010"]["alert_count"] == 1
    assert curves.select("recall").min().item() == pytest.approx(0)
    assert curves.select("recall").max().item() == pytest.approx(1)


def _paired_predictions() -> pl.DataFrame:
    frame = _label_frame().filter(pl.col("as_of_utc") < START + timedelta(minutes=120))
    labels = np.asarray(frame["label"].to_numpy(), dtype=float)
    parts = []
    for candidate, scores in (
        ("baseline", 0.1 + labels * 0.4),
        ("winner", 0.05 + labels * 0.8),
    ):
        parts.append(
            frame.select("station_id", "as_of_utc", "label").with_columns(
                pl.Series("score", scores),
                pl.lit("ablation").alias("analysis"),
                pl.lit(candidate).alias("candidate"),
                pl.lit("stockout_30m").alias("target"),
                pl.lit("fold").alias("fold"),
            )
        )
    return pl.concat(parts)


def test_block_uncertainty_resamples_whole_network_time_blocks() -> None:
    blocks, uncertainty = _block_analysis(
        _paired_predictions(),
        "baseline",
        "winner",
        block_minutes=60,
        draws=20,
        seed=42,
    )

    assert blocks["candidate"].n_unique() == 2
    assert blocks["block"].n_unique() == 2
    assert uncertainty["bootstrap_draws"] == 20
    assert uncertainty["average_precision_difference"]["mean"] >= 0


def test_pr_plot_is_a_standalone_svg(tmp_path: Path) -> None:
    _, curves = _evaluate_predictions(_paired_predictions())
    output = tmp_path / "curve.svg"

    _svg_pr_plot(curves, output, title="Synthetic PR")

    content = output.read_text()
    assert content.startswith("<svg")
    assert "Synthetic PR" in content
    assert "winner" in content


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def temporal_project(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    export_id = "synthetic-export"
    development_end = START + timedelta(hours=6)
    first_boundary = START + timedelta(minutes=65)
    cycle = [5, 4, 3, 2, 1, 0, 0, 5, 4, 3, 2, 1]
    rows: list[dict[str, object]] = []
    for offset in range(0, 361, 5):
        stamp = START + timedelta(minutes=offset)
        slot = offset // 5
        for station_index in range(4):
            bikes = cycle[(slot + station_index * 3) % len(cycle)]
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
                    "mechanical_bikes": max(0, bikes - 1),
                    "electric_bikes": min(1, bikes),
                    "available_docks": 20 - bikes,
                    "capacity": 20,
                    "latitude": 48.85 + station_index * 0.001,
                    "longitude": 2.35,
                }
            )
    history_path = data_dir / "silver" / "observations" / "date=2026-10-01" / "hour=00.parquet"
    atomic_write_parquet(history_path, pl.DataFrame(rows, schema=OBSERVATION_SCHEMA))
    station_dimension = pl.DataFrame(
        {
            "station_id": [str(index) for index in range(4)],
            "name": [f"Station {index}" for index in range(4)],
            "latitude": [48.85 + index * 0.001 for index in range(4)],
            "longitude": [2.35] * 4,
            "capacity": [20] * 4,
            "valid_from_utc": [START - timedelta(days=1)] * 4,
            "valid_to_utc": [None] * 4,
            "is_current": [True] * 4,
        }
    )
    atomic_write_parquet(data_dir / "silver" / "station_dimension.parquet", station_dimension)

    export = data_dir / "ml" / "exports" / export_id
    atomic_write_parquet(export / "full_labeled.parquet", pl.DataFrame({"row": [1]}))
    atomic_write_text(export / "sources.json", "[]\n")
    artifacts = {name: _sha256(export / name) for name in ("full_labeled.parquet", "sources.json")}
    atomic_write_text(
        export / "manifest.json",
        json.dumps(
            {
                "run_id": export_id,
                "source_sha256": "synthetic-source",
                "first_boundary_utc": first_boundary.isoformat(),
                "cutoff_received_at_utc": (development_end - timedelta(seconds=5)).isoformat(),
                "validation_start_utc": (START + timedelta(hours=2)).isoformat(),
                "test_start_utc": development_end.isoformat(),
                "artifacts": artifacts,
            },
            indent=2,
        ),
    )
    config = tmp_path / "experiments" / "synthetic.toml"
    atomic_write_text(
        config,
        f"""[experiment]
name = "synthetic-temporal-study"
data_dir = "data"
export_id = "{export_id}"
development_end_utc = "{development_end.isoformat()}"
purge_minutes = 30
neighbor_radius_meters = 1000.0
seed = 42
bootstrap_draws = 10
block_minutes = 30

[model]
learning_rate = 0.1
max_iter = 5
max_leaf_nodes = 3
min_samples_leaf = 2
l2_regularization = 1.0
early_stopping = false
random_state = 42

[[folds]]
name = "fold_one"
validation_start_utc = "{(START + timedelta(minutes=135)).isoformat()}"
validation_end_utc = "{(START + timedelta(minutes=205)).isoformat()}"

[[folds]]
name = "fold_two"
validation_start_utc = "{(START + timedelta(minutes=215)).isoformat()}"
validation_end_utc = "{(START + timedelta(minutes=285)).isoformat()}"

[[folds]]
name = "fold_three"
validation_start_utc = "{(START + timedelta(minutes=295)).isoformat()}"
validation_end_utc = "{development_end.isoformat()}"

[output]
report_path = "docs/research/synthetic-report.md"
plot_directory = "docs/research/assets"
""",
    )
    return config


def test_temporal_orchestration_builds_and_reuses_verified_cache(
    temporal_project: Path,
) -> None:
    first = run_temporal_study(temporal_project)
    first_protocol = json.loads((first / "protocol.json").read_text())
    cache_path = Path(first_protocol["feature_cache"])
    cache_manifest = Path(first_protocol["feature_cache_manifest"])

    assert cache_path.parent.name == "v2"
    assert cache_path.exists()
    assert cache_manifest.exists()
    assert "missing" in first_protocol["feature_cache_status"]
    assert pl.read_parquet(first / "oof_predictions.parquet")[
        "as_of_utc"
    ].max() < START + timedelta(hours=6)

    second = run_temporal_study(temporal_project)
    second_protocol = json.loads((second / "protocol.json").read_text())
    assert second == first
    assert second_protocol["feature_cache_status"] == "verified cache reused"

    cache_path.write_bytes(b"corrupt")
    rebuilt = run_temporal_study(temporal_project)
    rebuilt_protocol = json.loads((rebuilt / "protocol.json").read_text())
    assert "checksum mismatch" in rebuilt_protocol["feature_cache_status"]
    assert pl.read_parquet(cache_path).height > 0

    incomplete_manifest = json.loads(cache_manifest.read_text())
    del incomplete_manifest["artifact"]["schema"]
    atomic_write_text(cache_manifest, json.dumps(incomplete_manifest))
    rebuilt = run_temporal_study(temporal_project)
    rebuilt_protocol = json.loads((rebuilt / "protocol.json").read_text())
    assert "schema does not match" in rebuilt_protocol["feature_cache_status"]

    invalid_frame = pl.read_parquet(cache_path).with_columns(pl.col("station_id").cast(pl.Int64))
    atomic_write_parquet(cache_path, invalid_frame)
    invalid_manifest = json.loads(cache_manifest.read_text())
    invalid_manifest["artifact"]["sha256"] = _sha256(cache_path)
    invalid_manifest["artifact"]["schema"] = {
        name: str(dtype) for name, dtype in invalid_frame.schema.items()
    }
    atomic_write_text(cache_manifest, json.dumps(invalid_manifest))
    rebuilt = run_temporal_study(temporal_project)
    rebuilt_protocol = json.loads((rebuilt / "protocol.json").read_text())
    assert "station_id must use String dtype" in rebuilt_protocol["feature_cache_status"]


@pytest.mark.parametrize(
    ("field", "mutate"),
    [
        ("source", lambda value: value.update(package_source_sha256="changed")),
        (
            "history",
            lambda value: value["history_partitions"][0].update(sha256="changed"),
        ),
        (
            "metadata",
            lambda value: value["station_dimension"].update(sha256="changed"),
        ),
        (
            "settings",
            lambda value: value["transformations"].update(neighbor_radius_meters=2000.0),
        ),
        ("runtime", lambda value: value["runtime"].update(polars="changed")),
    ],
)
def test_temporal_cache_identity_covers_all_provenance_inputs(
    temporal_project: Path,
    field: str,
    mutate,
) -> None:
    config = load_experiment_config(temporal_project)
    manifest = json.loads(
        (config.data_dir / "ml" / "exports" / config.export_id / "manifest.json").read_text()
    )
    provenance = _cache_provenance(config, manifest)
    changed = copy.deepcopy(provenance)
    mutate(changed)

    assert field
    assert _cache_key(changed) != _cache_key(provenance)
