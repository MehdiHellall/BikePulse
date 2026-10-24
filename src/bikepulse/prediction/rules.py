from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from bikepulse.app.live_data import StationMapRow, StationMapSnapshot
from bikepulse.app.view_model import AvailabilityMode
from bikepulse.prediction.movement import InventoryMovement, MovementAnalysis
from bikepulse.prediction.weather import WeatherConditions
from bikepulse.prediction.zones import coordinate_distance_metres, matching_zones

RULES_VERSION = "5"
HORIZON = timedelta(minutes=30)
PARIS = ZoneInfo("Europe/Paris")
LOW_INVENTORY = 3
LIMITED_INVENTORY = 6
LIMITED_SHARE = 0.20
RAIN_THRESHOLD = 60
WIND_THRESHOLD_KMH = 30
COLD_THRESHOLD_C = 5
HOT_THRESHOLD_C = 30
NEARBY_RADIUS_METRES = 500
NEARBY_PRESSURE_MIN_STATIONS = 2
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
    movement: InventoryMovement | None = None
    nearby_pressure_count: int = 0


def is_peak_period(target_utc: datetime) -> bool:
    local = target_utc.astimezone(PARIS)
    minute = local.hour * 60 + local.minute
    if local.weekday() >= 5:
        return 11 * 60 <= minute < 19 * 60
    return 7 * 60 <= minute < 10 * 60 or 17 * 60 <= minute < 20 * 60


def _nearby_pressure_count(
    station: StationMapRow,
    snapshot: StationMapSnapshot,
    *,
    mode: AvailabilityMode,
    now_utc: datetime,
    max_station_age_seconds: int,
) -> int:
    pressured = 0
    for candidate in snapshot.stations:
        if candidate.station_id == station.station_id:
            continue
        if (
            coordinate_distance_metres(
                station.latitude,
                station.longitude,
                candidate.latitude,
                candidate.longitude,
            )
            > NEARBY_RADIUS_METRES
        ):
            continue
        timestamp = candidate.station_last_reported_utc
        count = candidate.available_bikes if mode == "bikes" else candidate.available_docks
        operating = candidate.is_renting if mode == "bikes" else candidate.is_returning
        if (
            timestamp is None
            or count is None
            or candidate.capacity <= 0
            or not 0 <= count <= candidate.capacity
            or candidate.is_installed is not True
            or operating is not True
        ):
            continue
        age = (now_utc - timestamp.astimezone(UTC)).total_seconds()
        if not 0 <= age <= max_station_age_seconds:
            continue
        if count <= LOW_INVENTORY or count / candidate.capacity <= LIMITED_SHARE:
            pressured += 1
    return pressured


def _movement_adjustment(
    points: int,
    *,
    analysis: MovementAnalysis | None,
    capacity: int,
    mode: AvailabilityMode,
) -> tuple[int, InventoryMovement | None, str, bool]:
    if analysis is None or analysis.estimate is None:
        detail = (
            analysis.reason
            if analysis is not None
            else "No trustworthy recent movement is available."
        )
        return points, None, f"{detail} Current conditions drive the estimate.", False

    movement = analysis.estimate
    unit = "bikes" if mode == "bikes" else "open docks"
    if movement.direction == "falling":
        projected_limited = (
            movement.projected_count <= LIMITED_INVENTORY
            or movement.projected_count / capacity <= LIMITED_SHARE
        )
        if movement.projected_count <= LOW_INVENTORY:
            points += 4
            high_floor = True
        elif projected_limited:
            points += 3
            high_floor = False
        else:
            points += 2
            high_floor = False
        reason = (
            f"Recent reports show {unit} falling by about "
            f"{abs(movement.rate_per_minute):.1f} per minute."
        )
        if high_floor:
            reason += " That movement projects critically low availability within 30 minutes."
        elif projected_limited:
            reason += " That movement projects limited availability within 30 minutes."
        return points, movement, reason, high_floor
    if movement.direction == "rising":
        points = max(0, points - 3)
        reason = (
            f"Recent reports show {unit} rising by about {movement.rate_per_minute:.1f} per minute, "
            "which lowers the contextual risk."
        )
        return points, movement, reason, False
    if movement.direction == "stable":
        return points, movement, f"Recent {unit} availability is stable.", False
    return (
        points,
        movement,
        f"Recent {unit} movement is mixed, so it did not change the estimate.",
        False,
    )


def forecast_station(
    station: StationMapRow,
    snapshot: StationMapSnapshot,
    *,
    mode: AvailabilityMode,
    now_utc: datetime,
    weather: WeatherConditions | None = None,
    movement_analysis: MovementAnalysis | None = None,
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
        status: PredictionStatus,
        risk: Risk | None,
        reasons: tuple[str, ...],
        *,
        movement: InventoryMovement | None = None,
        nearby_pressure_count: int = 0,
    ) -> StationOutlook:
        return StationOutlook(
            status,
            risk,
            target,
            count,
            area,
            period,
            usable_weather,
            reasons,
            movement=movement,
            nearby_pressure_count=nearby_pressure_count,
        )

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
    if count > station.capacity:
        return result("unavailable", None, ("Current inventory exceeds station capacity.",))
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
    nearby_pressure_count = _nearby_pressure_count(
        station,
        snapshot,
        mode=mode,
        now_utc=now,
        max_station_age_seconds=max_station_age_seconds,
    )
    if nearby_pressure_count >= NEARBY_PRESSURE_MIN_STATIONS:
        points += 1
        reasons.append(f"{nearby_pressure_count} nearby stations have low {mode} availability.")
    else:
        reasons.append("Nearby stations do not add concentrated availability pressure.")
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
    points, movement, movement_reason, movement_high_floor = _movement_adjustment(
        points,
        analysis=movement_analysis,
        capacity=station.capacity,
        mode=mode,
    )
    reasons.append(movement_reason)
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
    if count <= LOW_INVENTORY or movement_high_floor:
        risk = "High"
        if count <= LOW_INVENTORY:
            reasons.insert(0, "Only 1-3 available: high risk regardless of contextual adjustments.")
    return result(
        "estimated",
        risk,
        tuple(reasons),
        movement=movement,
        nearby_pressure_count=(
            nearby_pressure_count if nearby_pressure_count >= NEARBY_PRESSURE_MIN_STATIONS else 0
        ),
    )
