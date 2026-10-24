from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from bikepulse.app.alternatives import connector_points, distance_meters, find_plan_b
from bikepulse.app.live_data import StationMapRow
from bikepulse.prediction.rules import StationOutlook

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


@pytest.fixture
def station() -> StationMapRow:
    return StationMapRow(
        station_id="selected",
        name="Selected",
        latitude=48.8566,
        longitude=2.3522,
        capacity=20,
        available_bikes=2,
        mechanical_bikes=1,
        electric_bikes=1,
        available_docks=18,
        is_installed=True,
        is_renting=True,
        is_returning=True,
        station_last_reported_utc=NOW,
    )


def outlook(risk: str | None, *, status: str = "estimated") -> StationOutlook:
    return StationOutlook(
        status, risk, NOW, 7, "Central Paris", "Off-peak period", None, ("Reason",)
    )


def test_plan_b_is_closest_safer_station_with_inventory(station: StationMapRow) -> None:
    close = replace(
        station,
        station_id="close",
        name="Close",
        longitude=2.353,
        available_bikes=7,
    )
    farther = replace(
        station,
        station_id="farther",
        name="Farther",
        longitude=2.36,
        available_bikes=9,
    )

    result = find_plan_b(
        station,
        outlook("High"),
        [(farther, outlook("Low")), (close, outlook("Medium"))],
        mode="bikes",
    )

    assert result is not None
    assert result.station.station_id == "close"
    assert result.distance_meters == distance_meters(station, close)


@pytest.mark.parametrize(
    "selected_outlook",
    [outlook("Low"), outlook(None, status="unavailable")],
)
def test_plan_b_is_hidden_when_selected_station_is_not_at_risk(
    station: StationMapRow, selected_outlook: StationOutlook
) -> None:
    candidate = replace(station, station_id="candidate", available_bikes=9)
    assert (
        find_plan_b(
            station,
            selected_outlook,
            [(candidate, outlook("Low"))],
            mode="bikes",
        )
        is None
    )


def test_plan_b_rejects_low_inventory_and_distant_stations(station: StationMapRow) -> None:
    low_inventory = replace(station, station_id="low", longitude=2.353, available_bikes=3)
    distant = replace(station, station_id="distant", longitude=2.5, available_bikes=12)
    result = find_plan_b(
        station,
        outlook("High"),
        [(low_inventory, outlook("Low")), (distant, outlook("Low"))],
        mode="bikes",
    )
    assert result is None


def test_connector_points_include_both_station_coordinates(station: StationMapRow) -> None:
    destination = replace(station, latitude=48.86, longitude=2.36)
    points = connector_points(station, destination, point_count=5)
    assert points[0] == {"longitude": station.longitude, "latitude": station.latitude}
    assert points[-1] == {
        "longitude": destination.longitude,
        "latitude": destination.latitude,
    }
    assert len(points) == 5
