from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from bikepulse.config import AppSettings
from bikepulse.config.settings import MovementSettings
from bikepulse.feeds.client import FetchResult, GbfsClient
from bikepulse.feeds.schemas import (
    GbfsPayload,
    NormalizedInformation,
    NormalizedStatus,
    normalize_information_payload,
    normalize_status_payload,
    unix_to_utc,
)
from bikepulse.prediction.movement import StationObservation, StationObservationTracker

STATUS_CACHE_TTL = timedelta(seconds=60)
INFORMATION_CACHE_TTL = timedelta(hours=24)


class LiveDataUnavailableError(RuntimeError):
    """Raised when no live or last-known snapshot can be displayed."""


class JsonClient(Protocol):
    def fetch_json(self, url: str) -> FetchResult: ...


@dataclass(frozen=True)
class StationMapRow:
    station_id: str
    name: str
    latitude: float
    longitude: float
    capacity: int
    available_bikes: int | None
    mechanical_bikes: int | None
    electric_bikes: int | None
    available_docks: int | None
    is_installed: bool | None
    is_renting: bool | None
    is_returning: bool | None
    station_last_reported_utc: datetime | None


@dataclass(frozen=True)
class StationMapSnapshot:
    fetched_at_utc: datetime
    status_source_updated_at_utc: datetime
    information_source_updated_at_utc: datetime
    stations: tuple[StationMapRow, ...]
    rejected_record_count: int
    excluded_station_count: int
    missing_status_count: int
    is_last_known: bool = False
    error_message: str | None = None


@dataclass(frozen=True)
class _CachedFeed:
    result: FetchResult
    fetched_at_utc: datetime


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _duplicates(values: list[str]) -> set[str]:
    return {value for value, count in Counter(values).items() if count > 1}


def build_station_snapshot(
    *,
    information_result: FetchResult,
    status_result: FetchResult,
    fetched_at_utc: datetime,
) -> StationMapSnapshot:
    """Normalize and join a pair of GBFS responses without any UI dependency."""
    information_payload = GbfsPayload.model_validate(information_result.payload)
    status_payload = GbfsPayload.model_validate(status_result.payload)
    information_batch = normalize_information_payload(information_result.payload)
    status_batch = normalize_status_payload(
        status_result.payload,
        fetched_at_utc=fetched_at_utc,
    )

    information_rows = [
        row for row in information_batch.rows if isinstance(row, NormalizedInformation)
    ]
    status_rows = [row for row in status_batch.rows if isinstance(row, NormalizedStatus)]
    duplicate_information_ids = _duplicates([row.station_id for row in information_rows])
    duplicate_status_ids = _duplicates([row.station_id for row in status_rows])

    valid_information = [
        row for row in information_rows if row.station_id not in duplicate_information_ids
    ]
    valid_status = [row for row in status_rows if row.station_id not in duplicate_status_ids]
    status_by_id = {row.station_id: row for row in valid_status}

    stations: list[StationMapRow] = []
    missing_status_count = 0
    for information in valid_information:
        status = status_by_id.get(information.station_id)
        if status is None:
            missing_status_count += 1
        stations.append(
            StationMapRow(
                station_id=information.station_id,
                name=information.name,
                latitude=information.latitude,
                longitude=information.longitude,
                capacity=information.capacity,
                available_bikes=status.available_bikes if status else None,
                mechanical_bikes=status.mechanical_bikes if status else None,
                electric_bikes=status.electric_bikes if status else None,
                available_docks=status.available_docks if status else None,
                is_installed=status.is_installed if status else None,
                is_renting=status.is_renting if status else None,
                is_returning=status.is_returning if status else None,
                station_last_reported_utc=(status.station_last_reported_utc if status else None),
            )
        )

    excluded_station_count = len(information_batch.errors) + len(duplicate_information_ids)
    rejected_record_count = (
        len(information_batch.errors)
        + len(status_batch.errors)
        + sum(row.station_id in duplicate_information_ids for row in information_rows)
        + sum(row.station_id in duplicate_status_ids for row in status_rows)
    )
    return StationMapSnapshot(
        fetched_at_utc=fetched_at_utc,
        status_source_updated_at_utc=unix_to_utc(status_payload.last_updated),
        information_source_updated_at_utc=unix_to_utc(information_payload.last_updated),
        stations=tuple(sorted(stations, key=lambda row: (row.name.casefold(), row.station_id))),
        rejected_record_count=rejected_record_count,
        excluded_station_count=excluded_station_count,
        missing_status_count=missing_status_count,
    )


class LiveStationService:
    """Fetch live GBFS data with short status and long information caches."""

    def __init__(self, settings: AppSettings, client: JsonClient) -> None:
        self._settings = settings
        self._client = client
        self._observation_tracker = StationObservationTracker(settings.movement)
        self._information: _CachedFeed | None = None
        self._status: _CachedFeed | None = None
        self._last_successful: StationMapSnapshot | None = None

    @property
    def max_station_age_seconds(self) -> int:
        return self._settings.live.max_station_age_seconds

    @property
    def station_warning_age_seconds(self) -> int:
        return self._settings.live.station_warning_age_seconds

    @property
    def max_feed_age_seconds(self) -> int:
        return self._settings.live.max_feed_age_seconds

    @property
    def movement_settings(self) -> MovementSettings:
        return self._settings.movement

    def station_observations(
        self, station_id: str, *, now_utc: datetime
    ) -> tuple[StationObservation, ...]:
        return self._observation_tracker.observations_for(station_id, now_utc=now_utc)

    def get_snapshot(
        self, *, force_status: bool = False, now_utc: datetime | None = None
    ) -> StationMapSnapshot:
        now = (now_utc or _utc_now()).astimezone(UTC)
        try:
            information = self._get_information(now)
            status = self._get_status(now, force=force_status)
            snapshot = build_station_snapshot(
                information_result=information.result,
                status_result=status.result,
                fetched_at_utc=status.fetched_at_utc,
            )
        except Exception as exc:
            if self._last_successful is None:
                raise LiveDataUnavailableError(str(exc)) from exc
            return replace(
                self._last_successful,
                is_last_known=True,
                error_message=str(exc),
            )
        self._observation_tracker.record_batch(
            (
                StationObservation(
                    station_id=station.station_id,
                    reported_at_utc=station.station_last_reported_utc,
                    capacity=station.capacity,
                    available_bikes=station.available_bikes,
                    available_docks=station.available_docks,
                    is_installed=station.is_installed,
                    is_renting=station.is_renting,
                    is_returning=station.is_returning,
                )
                for station in snapshot.stations
                if station.station_last_reported_utc is not None
            ),
            active_station_ids={station.station_id for station in snapshot.stations},
            observed_at_utc=snapshot.fetched_at_utc,
        )
        self._last_successful = snapshot
        return snapshot

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def _get_information(self, now: datetime) -> _CachedFeed:
        if (
            self._information is None
            or now - self._information.fetched_at_utc >= INFORMATION_CACHE_TTL
        ):
            result = self._client.fetch_json(str(self._settings.gbfs.information_url))
            self._information = _CachedFeed(result=result, fetched_at_utc=now)
        return self._information

    def _get_status(self, now: datetime, *, force: bool) -> _CachedFeed:
        if force or self._status is None or now - self._status.fetched_at_utc >= STATUS_CACHE_TTL:
            result = self._client.fetch_json(str(self._settings.gbfs.status_url))
            self._status = _CachedFeed(result=result, fetched_at_utc=now)
        return self._status


def make_live_station_service(settings: AppSettings) -> LiveStationService:
    client = GbfsClient(
        timeout_seconds=settings.gbfs.timeout_seconds,
        max_retries=settings.gbfs.max_retries,
        backoff_seconds=settings.gbfs.backoff_seconds,
        user_agent=settings.gbfs.user_agent,
    )
    return LiveStationService(settings, client)
