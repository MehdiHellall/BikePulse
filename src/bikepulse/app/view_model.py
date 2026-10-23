from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from bikepulse.app.live_data import StationMapRow

AvailabilityMode = Literal["bikes", "docks"]

RED = (220, 53, 69, 220)
AMBER = (245, 166, 35, 225)
GREEN = (24, 151, 104, 220)
GREY = (128, 138, 150, 190)


@dataclass(frozen=True)
class AvailabilityView:
    count: int | None
    label: str
    color: tuple[int, int, int, int]
    age_seconds: int | None
    is_fresh: bool


def station_age_seconds(station: StationMapRow, now_utc: datetime) -> int | None:
    if station.station_last_reported_utc is None:
        return None
    return max(
        0,
        int(
            (
                now_utc.astimezone(UTC) - station.station_last_reported_utc.astimezone(UTC)
            ).total_seconds()
        ),
    )


def availability_view(
    station: StationMapRow,
    *,
    mode: AvailabilityMode,
    now_utc: datetime,
    max_age_seconds: int,
    warning_age_seconds: int | None = None,
    last_known: bool = False,
) -> AvailabilityView:
    count = station.available_bikes if mode == "bikes" else station.available_docks
    age = station_age_seconds(station, now_utc)
    if last_known:
        return AvailabilityView(count, "Last known data", GREY, age, False)
    if count is None or age is None:
        return AvailabilityView(None, "Status unavailable", GREY, age, False)
    if age > max_age_seconds:
        return AvailabilityView(count, "Stale data", GREY, age, False)
    if not station.is_installed:
        return AvailabilityView(count, "Station offline", GREY, age, True)
    if mode == "bikes" and not station.is_renting:
        return AvailabilityView(count, "Rentals unavailable", GREY, age, True)
    if mode == "docks" and not station.is_returning:
        return AvailabilityView(count, "Returns unavailable", GREY, age, True)
    is_delayed = warning_age_seconds is not None and age > warning_age_seconds
    delayed_suffix = " · Delayed report" if is_delayed else ""
    if count == 0:
        return AvailabilityView(count, "None available" + delayed_suffix, RED, age, not is_delayed)
    if count <= 3:
        return AvailabilityView(
            count,
            "Low availability" + delayed_suffix,
            AMBER,
            age,
            not is_delayed,
        )
    return AvailabilityView(count, "Available" + delayed_suffix, GREEN, age, not is_delayed)


def map_rows(
    stations: tuple[StationMapRow, ...],
    *,
    mode: AvailabilityMode,
    now_utc: datetime,
    max_age_seconds: int,
    last_known: bool,
    warning_age_seconds: int | None = None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for station in stations:
        view = availability_view(
            station,
            mode=mode,
            now_utc=now_utc,
            max_age_seconds=max_age_seconds,
            warning_age_seconds=warning_age_seconds,
            last_known=last_known,
        )
        rows.append(
            {
                "station_id": station.station_id,
                "name": station.name,
                "latitude": station.latitude,
                "longitude": station.longitude,
                "availability": view.count,
                "availability_text": "Unknown" if view.count is None else str(view.count),
                "status_label": view.label,
                "color": list(view.color),
            }
        )
    return rows
