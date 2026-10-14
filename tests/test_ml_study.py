from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from bikepulse.collector.schemas import NormalizedInformation
from bikepulse.collector.stations import refresh_station_dimension
from bikepulse.collector.storage import store_snapshot, write_poll_record
from bikepulse.ml.readiness import build_readiness_report, write_daily_readiness_report
from bikepulse.ml.study import choose_threshold, predict_station, run_study

START = datetime(2026, 8, 1, tzinfo=UTC)


@pytest.fixture
def study_history(tmp_path: Path) -> Path:
    refresh_station_dimension(
        data_dir=tmp_path,
        observed_at_utc=START,
        rows=[
            NormalizedInformation(
                station_id=str(i), name=f"Test {i}", capacity=20, latitude=48.85, longitude=2.35
            )
            for i in (1, 2)
        ],
    )
    for day in range(14):
        for tick in range(25):
            boundary = START + timedelta(days=day, hours=12, minutes=tick * 5)
            collected = boundary - timedelta(seconds=10)
            payload = {
                "last_updated": int(collected.timestamp()),
                "ttl": 60,
                "data": {
                    "stations": [
                        {
                            "station_id": str(i),
                            "is_installed": 1,
                            "is_renting": 1,
                            "is_returning": 1,
                            "num_bikes_available": tick % 12 if i == 1 else 10,
                            "num_docks_available": 20 - (tick % 12 if i == 1 else 10),
                            "last_reported": int(collected.timestamp()),
                        }
                        for i in (1, 2)
                    ]
                },
            }
            stored = store_snapshot(
                data_dir=tmp_path,
                kind="status",
                collected_at_utc=collected,
                received_at_utc=boundary - timedelta(seconds=5),
                payload=payload,
                content_sha256=f"{day * 25 + tick:064x}",
            )
            write_poll_record(
                data_dir=tmp_path,
                stored=stored,
                url="http://fixture/status",
                latency_ms=1,
                attempts=1,
                status_code=200,
                valid_record_count=2,
                error_count=0,
                error_codes=[],
            )
    return tmp_path


def test_full_raw_to_model_study_is_reproducible_and_predicts(study_history: Path) -> None:
    end = START + timedelta(days=14)
    output = run_study(study_history, START, end)
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "complete"
    assert report["logistic_regression"]["row_count"] == report["inventory_baseline"]["row_count"]
    assert report["logistic_regression"]["average_precision"] > 0
    assert report["config"]["feature_schema_version"] == "empty-poc-v2-feed-observation"
    assert report["config"]["freshness_basis"] == "published feed update and local receipt time"
    assert (output / "model.joblib").exists()
    first = pl.read_parquet(output / "test_predictions.parquet")
    again = run_study(study_history, START, end)
    assert output == again
    assert first.equals(pl.read_parquet(again / "test_predictions.parquet"))
    now = START + timedelta(days=13, hours=14)
    result = predict_station(study_history, "2", now=now)
    assert result["status"] == "experimental prediction"
    assert 0 <= result["empty_within_30m_probability"] <= 1
    assert predict_station(study_history, "1", now=now)["status"] == "Empty now"
    assert (
        predict_station(study_history, "1", now=now + timedelta(seconds=295))["status"]
        == "unavailable"
    )
    assert predict_station(study_history, "unknown", now=now)["status"] == "unavailable"
    assert (
        predict_station(study_history, "2", now=now + timedelta(minutes=10))["status"]
        == "unavailable"
    )
    binary = output / "model.joblib"
    binary.write_bytes(b"corrupt")
    assert "checksum" in predict_station(study_history, "2", now=now)["reason"]


def test_empty_history_reports_blocker_and_does_not_fabricate_model(tmp_path: Path) -> None:
    output = run_study(tmp_path, START, START + timedelta(days=14))
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "insufficient_data"
    assert not (output / "model.joblib").exists()
    assert predict_station(tmp_path, "1", now=START)["status"] == "unavailable"
    with pytest.raises(ValueError, match="14"):
        run_study(tmp_path, START, START + timedelta(days=13))
    future = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=1
    )
    with pytest.raises(ValueError, match="completed"):
        run_study(tmp_path, future, future + timedelta(days=14))


def test_readiness_reports_complete_positive_and_negative_windows(study_history: Path) -> None:
    report = build_readiness_report(study_history, START, START + timedelta(days=1))

    assert report["status"] == "labels_forming"
    assert report["complete_label_count"] > 0
    assert report["positive_label_count"] > 0
    assert report["negative_label_count"] > 0
    assert report["feed_fresh_row_count"] > report["station_fresh_row_count"] - 1
    output = write_daily_readiness_report(study_history, START.date())
    assert output == study_history / "ml/readiness/daily/2026-08-01.md"
    assert output.exists()


def test_threshold_uses_f1_and_breaks_ties_with_precision() -> None:
    assert choose_threshold(np.array([1, 0, 0, 1]), np.array([0.9, 0.8, 0.7, 0.6])) == 0.9


def test_prediction_with_usable_data_requires_a_model(study_history: Path) -> None:
    now = START + timedelta(days=13, hours=14)
    result = predict_station(study_history, "2", now=now)
    assert result["reason"] == "no completed model study"
    latest = study_history / "ml/latest.json"
    latest.parent.mkdir(exist_ok=True)
    latest.write_text('{"run_id": "missing"}')
    assert (
        predict_station(study_history, "2", now=now)["reason"] == "model report missing or invalid"
    )
    output = study_history / "ml/experiments/missing"
    output.mkdir(parents=True)
    (output / "report.json").write_text('{"status": "complete"}')
    assert (
        predict_station(study_history, "2", now=now)["reason"]
        == "model artifact missing or unreadable"
    )
