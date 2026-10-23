from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from bikepulse.app.live_data import StationMapRow, StationMapSnapshot
from bikepulse.app.view_model import AvailabilityMode
from bikepulse.prediction.weather import WeatherConditions
from bikepulse.prediction.zones import matching_zones

RULES_VERSION = "4"
HORIZON = timedelta(minutes=30)
PARIS = ZoneInfo("Europe/Paris")
LOW_INVENTORY = 3
LIMITED_INVENTORY = 6
LIMITED_SHARE = 0.20
RAIN_THRESHOLD = 60
WIND_THRESHOLD_KMH = 30
COLD_THRESHOLD_C = 5
HOT_THRESHOLD_C = 30
Risk = Literal["Low", "Medium", "High"]
PredictionStatus = Literal["estimated", "already_empty", "already_full", "unavailable"]


@dataclass(frozen=True)
class StationOutlook:
    status: PredictionStatus
    risk: Risk | None
    target_utc: datetime
    inventory: int | None
    area: str
    period: str
    weather: WeatherConditions | None
    reasons: tuple[str, ...]
    rules_version: str = RULES_VERSION


def is_peak_period(target_utc: datetime) -> bool:
    local = target_utc.astimezone(PARIS)
    minute = local.hour * 60 + local.minute
    if local.weekday() >= 5:
        return 11 * 60 <= minute < 19 * 60
    return 7 * 60 <= minute < 10 * 60 or 17 * 60 <= minute < 20 * 60


def forecast_station(
    station: StationMapRow,
    snapshot: StationMapSnapshot,
    *,
    mode: AvailabilityMode,
    now_utc: datetime,
    weather: WeatherConditions | None = None,
    station_warning_age_seconds: int = 3600,
    max_station_age_seconds: int = 7200,
    max_feed_age_seconds: int = 300,
) -> StationOutlook:
    now = now_utc.astimezone(UTC)
    target = now + HORIZON
    count = station.available_bikes if mode == "bikes" else station.available_docks
    zones = matching_zones(station.latitude, station.longitude)
    area = "Busy area: " + ", ".join(zones) if zones else "Outside configured busy zones"
    peak = is_peak_period(target)
    period = "Peak period" if peak else "Off-peak period"
    usable_weather = weather if weather is not None and weather.is_usable(now, target) else None

    def result(
        status: PredictionStatus, risk: Risk | None, reasons: tuple[str, ...]
    ) -> StationOutlook:
        return StationOutlook(status, risk, target, count, area, period, usable_weather, reasons)

    if snapshot.is_last_known:
        return result("unavailable", None, ("Last known data; waiting for a successful refresh.",))
    station_age_seconds: float | None = None
    for timestamp, limit, name in (
        (snapshot.fetched_at_utc, max_feed_age_seconds, "Feed receipt"),
        (snapshot.status_source_updated_at_utc, max_feed_age_seconds, "Feed publication"),
        (station.station_last_reported_utc, max_station_age_seconds, "Station report"),
    ):
        if timestamp is None:
            return result("unavailable", None, (f"{name} time is unknown.",))
        age = (now - timestamp).total_seconds()
        if age < 0 or age > limit:
            return result("unavailable", None, (f"{name} time is stale or in the future.",))
        if name == "Station report":
            station_age_seconds = age
    if count is None or count < 0:
        return result("unavailable", None, ("Current inventory is unknown or invalid.",))
    if station.capacity <= 0:
        return result("unavailable", None, ("Station capacity is unavailable or invalid.",))
    if not station.is_installed:
        return result(
            "unavailable", None, ("Station is offline or its operating state is unknown.",)
        )
    operating = station.is_renting if mode == "bikes" else station.is_returning
    if not operating:
        operation = "Rentals" if mode == "bikes" else "Returns"
        return result(
            "unavailable", None, (f"{operation} unavailable or operating state unknown.",)
        )
    if count == 0:
        return result(
            "already_empty" if mode == "bikes" else "already_full",
            None,
            ("No bikes available now." if mode == "bikes" else "No standard docks open now.",),
        )

    limited = count <= LIMITED_INVENTORY or count / station.capacity <= LIMITED_SHARE
    points = 2 if limited else 0
    reasons = []
    if station_age_seconds is not None and station_age_seconds > station_warning_age_seconds:
        reasons.append("The station report is delayed but still within the allowed age.")
    reasons.append(
        "Low current availability increases the risk."
        if limited
        else "Current availability provides a useful buffer."
    )
    if zones:
        points += 1
        reasons.append("This station is in a busy area.")
    else:
        reasons.append("This station is outside the busiest configured areas.")
    if peak:
        points += 1
        reasons.append("The next 30 minutes fall in a busy travel period.")
    else:
        reasons.append("The next 30 minutes are outside the busiest travel periods.")
    if usable_weather is None:
        reasons.append("Weather is unavailable, so it did not change the estimate.")
    elif (
        usable_weather.precipitation_probability >= RAIN_THRESHOLD
        or usable_weather.wind_kmh >= WIND_THRESHOLD_KMH
        or usable_weather.temperature_c < COLD_THRESHOLD_C
        or usable_weather.temperature_c > HOT_THRESHOLD_C
    ):
        points = max(0, points - 1)
        reasons.append("Poor riding weather may reduce demand.")
    else:
        reasons.append("Current weather does not change the estimate.")
    risk: Risk = "High" if points >= 4 else "Medium" if points >= 2 else "Low"
    if count <= LOW_INVENTORY:
        risk = "High"
        reasons.insert(0, "Only 1-3 available: high risk regardless of contextual adjustments.")
    return result("estimated", risk, tuple(reasons))
