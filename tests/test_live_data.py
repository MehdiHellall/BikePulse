from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import load_json

from bikepulse.app.live_data import (
    LiveDataUnavailableError,
    LiveStationService,
    build_station_snapshot,
)
from bikepulse.collector.client import FetchResult
from bikepulse.config import load_settings


def result(url: str, payload: dict[str, Any], marker: str) -> FetchResult:
    content = json.dumps(payload).encode("utf-8")
    return FetchResult(
        url=url,
        payload=payload,
        content=content,
        content_sha256=marker * 64,
        latency_ms=1,
        attempts=1,
        status_code=200,
    )


def test_snapshot_joins_by_station_id_and_preserves_unknown_status(fixture_dir: Path) -> None:
    information = load_json(fixture_dir / "gbfs_information_valid.json")
    status = load_json(fixture_dir / "gbfs_status_valid.json")
    status["data"]["stations"] = status["data"]["stations"][:1]

    snapshot = build_station_snapshot(
        information_result=result("information", information, "a"),
        status_result=result("status", status, "b"),
        fetched_at_utc=datetime(2026, 8, 9, 21, tzinfo=UTC),
    )

    alpha, beta = snapshot.stations
    assert alpha.station_id == "1"
    assert alpha.available_bikes == 7
    assert beta.station_id == "2"
    assert beta.available_bikes is None
    assert snapshot.missing_status_count == 1


def test_snapshot_rejects_duplicate_ids_and_invalid_coordinates(fixture_dir: Path) -> None:
    information = load_json(fixture_dir / "gbfs_information_valid.json")
    information["data"]["stations"].append(copy.deepcopy(information["data"]["stations"][0]))
    information["data"]["stations"].append(
        {"station_id": "bad", "name": "Invalid", "lat": 120, "lon": 2.3, "capacity": 10}
    )
    status = load_json(fixture_dir / "gbfs_status_valid.json")

    snapshot = build_station_snapshot(
        information_result=result("information", information, "a"),
        status_result=result("status", status, "b"),
        fetched_at_utc=datetime(2026, 8, 9, 21, tzinfo=UTC),
    )

    assert [row.station_id for row in snapshot.stations] == ["2"]
    assert snapshot.excluded_station_count == 2
    assert snapshot.rejected_record_count == 3


class SequencedClient:
    def __init__(self, information: dict[str, Any], status: dict[str, Any]) -> None:
        self.information = information
        self.status = status
        self.calls: list[str] = []
        self.fail_status = False

    def fetch_json(self, url: str) -> FetchResult:
        self.calls.append(url)
        if "status" in url:
            if self.fail_status:
                raise RuntimeError("status feed offline")
            return result(url, self.status, "b")
        return result(url, self.information, "a")


def test_service_caches_feeds_and_recovers_with_last_known_data(
    fixture_dir: Path, settings_config: Path
) -> None:
    client = SequencedClient(
        load_json(fixture_dir / "gbfs_information_valid.json"),
        load_json(fixture_dir / "gbfs_status_valid.json"),
    )
    service = LiveStationService(load_settings(settings_config), client)
    now = datetime(2026, 8, 9, 21, tzinfo=UTC)

    first = service.get_snapshot(now_utc=now)
    cached = service.get_snapshot(now_utc=now + timedelta(seconds=30))
    assert cached == first
    assert len(client.calls) == 2

    client.fail_status = True
    fallback = service.get_snapshot(force_status=True, now_utc=now + timedelta(seconds=31))
    assert fallback.is_last_known is True
    assert fallback.error_message == "status feed offline"
    assert fallback.stations == first.stations

    client.fail_status = False
    recovered = service.get_snapshot(force_status=True, now_utc=now + timedelta(seconds=32))
    assert recovered.is_last_known is False


def test_service_raises_clear_error_when_startup_has_no_data(
    fixture_dir: Path, settings_config: Path
) -> None:
    client = SequencedClient(
        load_json(fixture_dir / "gbfs_information_valid.json"),
        load_json(fixture_dir / "gbfs_status_valid.json"),
    )
    client.fail_status = True
    service = LiveStationService(load_settings(settings_config), client)

    with pytest.raises(LiveDataUnavailableError, match="status feed offline"):
        service.get_snapshot(now_utc=datetime(2026, 8, 9, 21, tzinfo=UTC))
