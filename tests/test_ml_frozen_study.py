from __future__ import annotations

import fcntl
import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

import bikepulse.ml.frozen_study as frozen_module
from bikepulse.io import atomic_write_parquet, atomic_write_text
from bikepulse.ml.export import GEOGRAPHIC_FEATURES
from bikepulse.ml.frozen_study import (
    ALL_FEATURES,
    lock_frozen_study,
    package_frozen_study,
    prepare_frozen_study,
    replay_frozen_study,
    run_frozen_study,
    run_frozen_test,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
VALIDATION = START + timedelta(days=1)
TEST = START + timedelta(days=2)


def _row(station_id: str, stamp: datetime, label: int, split: str, index: int) -> dict[str, object]:
    bikes = 1 + (index % 8)
    return {
        "station_id": station_id,
        "as_of_utc": stamp,
        "label_end_utc": stamp + timedelta(minutes=30),
        "snapshot_id": f"snapshot-{split}-{index}",
        "label": label,
        "available_bikes": bikes,
        "available_docks": 20 - bikes,
        "capacity": 20,
        "bike_ratio": bikes / 20,
        "bike_change_15m": -1 if label else 1,
        "hour_sin": float(index % 3),
        "hour_cos": float(index % 5),
        "weekend": 0,
        "latitude": 48.85 + index / 10_000,
        "longitude": 2.35,
        "neighbor_station_count_1km": 3 + index % 3,
        "neighbor_capacity_1km": 60 + index % 10,
        "nearby_abs_net_bike_change_15m": 4 + index % 7,
        "nearby_observed_count_15m": 2 + index % 2,
        "split": split,
    }


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def frozen_export(tmp_path: Path) -> tuple[Path, str]:
    export_id = "fixture-export"
    directory = tmp_path / "ml" / "exports" / export_id
    rows: list[dict[str, object]] = []
    for index in range(50):
        rows.append(
            _row(
                str(index % 4),
                START + timedelta(minutes=index * 5),
                int(index % 10 == 0),
                "train",
                index,
            )
        )
    for index in range(30):
        rows.append(
            _row(
                str(index % 4),
                VALIDATION + timedelta(minutes=index * 5),
                int(index % 6 == 0),
                "validation",
                100 + index,
            )
        )
    for index in range(30):
        rows.append(
            _row(
                str(index % 4),
                TEST + timedelta(minutes=index * 5),
                int(index % 7 == 0),
                "test",
                200 + index,
            )
        )
    rows.append(_row("0", VALIDATION - timedelta(minutes=20), 0, "purged", 300))
    full = pl.DataFrame(rows)
    enriched = (
        full.filter(pl.col("split") == "train")
        .filter((pl.col("label") == 1) | (pl.col("label") == 0))
        .sort("label", descending=True)
        .head(50)
    )
    positives = enriched.filter(pl.col("label") == 1)
    negatives = enriched.filter(pl.col("label") == 0).head(positives.height * 9)
    enriched = pl.concat([positives, negatives]).sort("as_of_utc", "station_id")
    atomic_write_parquet(directory / "full_labeled.parquet", full)
    atomic_write_parquet(directory / "enriched_train.parquet", enriched)
    atomic_write_text(directory / "sources.json", "[]\n")
    artifacts = {
        name: _hash(directory / name)
        for name in ("full_labeled.parquet", "enriched_train.parquet", "sources.json")
    }
    atomic_write_text(
        directory / "manifest.json",
        json.dumps(
            {
                "run_id": export_id,
                "source_sha256": "source",
                "validation_start_utc": VALIDATION.isoformat(),
                "test_start_utc": TEST.isoformat(),
                "features": ALL_FEATURES,
                "artifacts": artifacts,
            },
            indent=2,
        ),
    )
    return tmp_path, export_id


def test_frozen_study_prepares_validates_and_runs_without_test_leakage(
    frozen_export: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir, export_id = frozen_export
    output = prepare_frozen_study(data_dir, export_id)
    study_id = output.name
    review = json.loads((output / "data_review.json").read_text())
    assert review["train_prevalence"] == pytest.approx(0.1)
    assert (output / "protocol.json").exists()
    prepared_mtime = (output / "protocol.json").stat().st_mtime_ns
    assert prepare_frozen_study(data_dir, export_id) == output
    assert (output / "protocol.json").stat().st_mtime_ns == prepared_mtime

    assert run_frozen_study(data_dir, study_id, mode="smoke") == output
    assert (output / "smoke" / "model.joblib").exists()
    assert run_frozen_study(data_dir, study_id, mode="full") == output
    assert not (output / "test_predictions.parquet").exists()
    validation_mtime = (output / "validation_results.json").stat().st_mtime_ns
    monkeypatch.setattr(
        frozen_module,
        "_fit_model",
        lambda *_args, **_kwargs: pytest.fail("completed validation must not fit again"),
    )
    assert run_frozen_study(data_dir, study_id, mode="full") == output
    assert (output / "validation_results.json").stat().st_mtime_ns == validation_mtime

    validation = json.loads((output / "validation_results.json").read_text())
    assert set(validation["metrics"]) == {
        "constant",
        "inventory_baseline",
        "bike_ratio_baseline",
        "logistic_core",
        "logistic_geo",
        "hist_core",
        "hist_geo",
    }
    assert (output / "validation_errors.csv").exists()
    assert (output / "subgroups.csv").exists()

    lock_frozen_study(data_dir, study_id, "logistic_core")
    assert lock_frozen_study(data_dir, study_id, "logistic_core") == output
    with pytest.raises(ValueError, match="conflicts with completed evidence"):
        lock_frozen_study(data_dir, study_id, "hist_core")
    run_frozen_test(data_dir, study_id)
    test_metrics = json.loads((output / "test_metrics.json").read_text())
    assert test_metrics["selection"]["candidate"] == "logistic_core"
    assert test_metrics["metrics"]["logistic_core"]["row_count"] == 30
    assert (output / "test_predictions.parquet").exists()
    test_mtime = (output / "test_metrics.json").stat().st_mtime_ns
    monkeypatch.setattr(
        frozen_module,
        "_candidate_test_score",
        lambda *_args, **_kwargs: pytest.fail("completed final test must not score again"),
    )
    assert run_frozen_test(data_dir, study_id) == output
    assert (output / "test_metrics.json").stat().st_mtime_ns == test_mtime

    release = package_frozen_study(data_dir, study_id)
    check = json.loads((release / "package_check.json").read_text())
    assert check["reproduced"]
    release_mtime = (release / "release_manifest.json").stat().st_mtime_ns
    assert package_frozen_study(data_dir, study_id) == release
    assert (release / "release_manifest.json").stat().st_mtime_ns == release_mtime
    missing = replay_frozen_study(data_dir, study_id, "missing", START)
    assert missing["status"] == "unavailable"


def test_frozen_study_rejects_tampered_export(frozen_export: tuple[Path, str]) -> None:
    data_dir, export_id = frozen_export
    full_path = data_dir / "ml" / "exports" / export_id / "full_labeled.parquet"
    full_path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        prepare_frozen_study(data_dir, export_id)


def test_frozen_study_schema_lists_geographic_features() -> None:
    assert ALL_FEATURES[-len(GEOGRAPHIC_FEATURES) :] == GEOGRAPHIC_FEATURES


def test_frozen_study_rejects_tampered_completed_stage(
    frozen_export: tuple[Path, str],
) -> None:
    data_dir, export_id = frozen_export
    output = prepare_frozen_study(data_dir, export_id)
    run_frozen_study(data_dir, output.name, mode="full")
    (output / "validation_report.md").write_text("tampered\n")

    with pytest.raises(ValueError, match="evidence changed"):
        run_frozen_study(data_dir, output.name, mode="full")


def test_frozen_study_blocks_interrupted_final_test(
    frozen_export: tuple[Path, str],
) -> None:
    data_dir, export_id = frozen_export
    output = prepare_frozen_study(data_dir, export_id)
    run_frozen_study(data_dir, output.name, mode="full")
    lock_frozen_study(data_dir, output.name, "logistic_core")
    with pytest.raises(ValueError, match="fitting is forbidden"):
        run_frozen_study(data_dir, output.name, mode="smoke")
    atomic_write_text(output / "stages" / "test_attempt.json", "{}\n")

    with pytest.raises(ValueError, match="attempt exists without verified completion"):
        run_frozen_test(data_dir, output.name)


def test_frozen_study_rejects_concurrent_mutation(
    frozen_export: tuple[Path, str],
) -> None:
    data_dir, export_id = frozen_export
    output = prepare_frozen_study(data_dir, export_id)

    with pytest.raises(ValueError, match="must complete validation"):
        lock_frozen_study(data_dir, output.name, "logistic_core")

    with (output / ".lifecycle.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="mutation is already in progress"):
            run_frozen_study(data_dir, output.name, mode="full")


def test_legacy_frozen_study_is_readable_but_not_mutable(
    frozen_export: tuple[Path, str],
) -> None:
    data_dir, export_id = frozen_export
    output = prepare_frozen_study(data_dir, export_id)
    shutil.rmtree(output / "stages")

    assert frozen_module.frozen_report(data_dir, output.name) == output / "data_review.md"
    with pytest.raises(ValueError, match="legacy or interrupted studies are read-only"):
        run_frozen_study(data_dir, output.name, mode="full")
