from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

from bikepulse.app.live_data import StationMapRow
from bikepulse.app.view_model import AvailabilityMode
from bikepulse.prediction.rules import StationOutlook

MAX_PLAN_B_DISTANCE_METERS = 2_000
MINIMUM_PLAN_B_INVENTORY = 4
EARTH_RADIUS_METERS = 6_371_000


@dataclass(frozen=True)
class PlanBRecommendation:
    station: StationMapRow
    outlook: StationOutlook
    distance_meters: int


def distance_meters(origin: StationMapRow, destination: StationMapRow) -> int:
    """Return rounded straight-line distance between two stations."""
    latitude_delta = radians(destination.latitude - origin.latitude)
    longitude_delta = radians(destination.longitude - origin.longitude)
    origin_latitude = radians(origin.latitude)
    destination_latitude = radians(destination.latitude)
    haversine = sin(latitude_delta / 2) ** 2 + (
        cos(origin_latitude) * cos(destination_latitude) * sin(longitude_delta / 2) ** 2
    )
    return round(2 * EARTH_RADIUS_METERS * asin(sqrt(haversine)))


def find_plan_b(
    selected: StationMapRow,
    selected_outlook: StationOutlook,
    candidates: Iterable[tuple[StationMapRow, StationOutlook]],
    *,
    mode: AvailabilityMode,
) -> PlanBRecommendation | None:
    """Return the closest nearby station with a safer, usable outlook."""
    if selected_outlook.status == "estimated":
        if selected_outlook.risk not in {"Medium", "High"}:
            return None
        selected_rank = {"Medium": 2, "High": 3}[selected_outlook.risk]
    elif selected_outlook.status in {"already_empty", "already_full"}:
        selected_rank = 4
    else:
        return None

    recommendations: list[PlanBRecommendation] = []
    for station, outlook in candidates:
        if station.station_id == selected.station_id or outlook.status != "estimated":
            continue
        inventory = station.available_bikes if mode == "bikes" else station.available_docks
        if inventory is None or inventory < MINIMUM_PLAN_B_INVENTORY:
            continue
        if outlook.risk is None:
            continue
        if {"Low": 1, "Medium": 2, "High": 3}[outlook.risk] >= selected_rank:
            continue
        distance = distance_meters(selected, station)
        if distance > MAX_PLAN_B_DISTANCE_METERS:
            continue
        recommendations.append(PlanBRecommendation(station, outlook, distance))

    return min(recommendations, key=lambda item: item.distance_meters, default=None)


def connector_points(
    origin: StationMapRow,
    destination: StationMapRow,
    *,
    point_count: int = 13,
) -> list[dict[str, float]]:
    """Create evenly spaced map points for the Plan B dotted connection."""
    if point_count < 2:
        raise ValueError("point_count must be at least 2")
    return [
        {
            "longitude": origin.longitude
            + (destination.longitude - origin.longitude) * index / (point_count - 1),
            "latitude": origin.latitude
            + (destination.latitude - origin.latitude) * index / (point_count - 1),
        }
        for index in range(point_count)
    ]
