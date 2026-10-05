from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from bikepulse.collector.client import GbfsClient
from bikepulse.collector.schemas import (
    NormalizedInformation,
    normalize_information_payload,
    normalize_status_payload,
)
from bikepulse.collector.stations import refresh_station_dimension
from bikepulse.collector.storage import StoredSnapshot, store_snapshot, write_poll_record
from bikepulse.config import AppSettings


def collect_status_once(settings: AppSettings, client: GbfsClient) -> StoredSnapshot:
    collected_at = datetime.now(UTC)
    result = client.fetch_json(str(settings.gbfs.status_url))
    received_at = datetime.now(UTC)
    stored = store_snapshot(
        data_dir=settings.paths.data_dir,
        kind="status",
        collected_at_utc=collected_at,
        payload=result.payload,
        content_sha256=result.content_sha256,
        content=result.content,
        received_at_utc=received_at,
    )
    batch = normalize_status_payload(
        result.payload, snapshot_id=stored.snapshot_id, collected_at_utc=received_at
    )
    write_poll_record(
        data_dir=settings.paths.data_dir,
        stored=stored,
        url=result.url,
        latency_ms=result.latency_ms,
        attempts=result.attempts,
        status_code=result.status_code,
        valid_record_count=len(batch.rows),
        error_count=len(batch.errors),
        error_codes=sorted({error.code for error in batch.errors}),
    )
    return stored


def refresh_stations_once(settings: AppSettings, client: GbfsClient) -> StoredSnapshot:
    collected_at = datetime.now(UTC)
    result = client.fetch_json(str(settings.gbfs.information_url))
    received_at = datetime.now(UTC)
    stored = store_snapshot(
        data_dir=settings.paths.data_dir,
        kind="information",
        collected_at_utc=collected_at,
        payload=result.payload,
        content_sha256=result.content_sha256,
        content=result.content,
        received_at_utc=received_at,
    )
    batch = normalize_information_payload(result.payload)
    rows = [row for row in batch.rows if isinstance(row, NormalizedInformation)]
    refresh_station_dimension(
        data_dir=settings.paths.data_dir,
        rows=rows,
        observed_at_utc=received_at,
    )
    write_poll_record(
        data_dir=settings.paths.data_dir,
        stored=stored,
        url=result.url,
        latency_ms=result.latency_ms,
        attempts=result.attempts,
        status_code=result.status_code,
        valid_record_count=len(batch.rows),
        error_count=len(batch.errors),
        error_codes=sorted({error.code for error in batch.errors}),
    )
    return stored


def make_client(settings: AppSettings) -> GbfsClient:
    return GbfsClient(
        timeout_seconds=settings.gbfs.timeout_seconds,
        max_retries=settings.gbfs.max_retries,
        backoff_seconds=settings.gbfs.backoff_seconds,
        user_agent=settings.gbfs.user_agent,
    )


def run_once(kind: Literal["status", "information"], settings: AppSettings) -> StoredSnapshot:
    with make_client(settings) as client:
        if kind == "status":
            return collect_status_once(settings, client)
        return refresh_stations_once(settings, client)
