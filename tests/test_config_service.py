from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from conftest import load_json

from bikepulse.collector.client import FetchResult
from bikepulse.collector.service import collect_status_once, refresh_stations_once
from bikepulse.config import load_settings


class FakeClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def fetch_json(self, url: str) -> FetchResult:
        return FetchResult(
            url=url,
            payload=self.payload,
            content=b"{}",
            content_sha256="f" * 64,
            latency_ms=1,
            attempts=1,
            status_code=200,
        )


def write_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[paths]
data_dir = "pipeline-data"

[gbfs]
status_url = "https://example.test/status.json"
information_url = "https://example.test/information.json"
user_agent = "BikePulse tests"
timeout_seconds = 1.0
max_retries = 1
backoff_seconds = 0.0

[collection]
max_station_age_seconds = 300
expected_interval_minutes = 5
daily_station_refresh_hour_utc = 3
""",
        encoding="utf-8",
    )
    return config


def test_load_settings_resolves_relative_data_dir(tmp_path) -> None:
    settings = load_settings(write_config(tmp_path))

    assert settings.paths.data_dir == (tmp_path / "pipeline-data").resolve()
    assert settings.collection.max_station_age_seconds == 300
    assert settings.collection.max_feed_age_seconds == 300


def test_service_collects_status_snapshot(fixture_dir, tmp_path, monkeypatch) -> None:
    settings = load_settings(write_config(tmp_path))
    monkeypatch.setattr(
        "bikepulse.collector.service.datetime",
        FrozenDateTime,
    )

    stored = collect_status_once(
        settings,
        FakeClient(load_json(fixture_dir / "gbfs_status_valid.json")),  # type: ignore[arg-type]
    )

    assert stored.path.exists()
    assert stored.kind == "status"
    poll_files = list((settings.paths.data_dir / "polls" / "2026-08-09").glob("*.json"))
    assert len(poll_files) == 1


def test_service_refreshes_station_dimension(fixture_dir, tmp_path, monkeypatch) -> None:
    settings = load_settings(write_config(tmp_path))
    monkeypatch.setattr(
        "bikepulse.collector.service.datetime",
        FrozenDateTime,
    )

    stored = refresh_stations_once(
        settings,
        FakeClient(load_json(fixture_dir / "gbfs_information_valid.json")),  # type: ignore[arg-type]
    )

    assert stored.path.exists()
    assert (settings.paths.data_dir / "silver" / "station_dimension.parquet").exists()


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[no-untyped-def]
        return datetime(2026, 8, 9, 21, 0, tzinfo=UTC)
