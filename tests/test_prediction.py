from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import degrees
from zoneinfo import ZoneInfo

import pytest

from bikepulse.app.live_data import StationMapRow, StationMapSnapshot
from bikepulse.prediction.rules import HORIZON, forecast_station, is_peak_period
from bikepulse.prediction.weather import WeatherConditions
from bikepulse.prediction.zones import (
    BUSY_ZONES,
    EARTH_RADIUS_METRES,
    BusyZone,
    matching_zones,
)

NOW = datetime(2026, 10, 19, 10, tzinfo=UTC)


def station_and_snapshot(**changes):
    station = StationMapRow(
        station_id="1",
        name="Example",
        latitude=0,
        longitude=0,
        capacity=20,
        available_bikes=10,
        mechanical_bikes=7,
        electric_bikes=3,
        available_docks=10,
        is_installed=True,
        is_renting=True,
        is_returning=True,
        station_last_reported_utc=NOW,
    )
    station = replace(station, **changes)
    snapshot = StationMapSnapshot(NOW, NOW, NOW, (station,), 0, 0, 0)
    return station, snapshot


def weather(**changes):
    return replace(WeatherConditions(NOW, NOW, 16, 10, 12), **changes)


@pytest.mark.parametrize("mode", ["bikes", "docks"])
@pytest.mark.parametrize(
    "count,capacity,expected",
    [
        (1, 20, "High"),
        (3, 20, "High"),
        (4, 20, "Medium"),
        (6, 20, "Medium"),
        (7, 20, "Low"),
        (10, 50, "Medium"),
        (11, 50, "Low"),
    ],
)
def test_inventory_and_capacity_rules(mode, count, capacity, expected) -> None:
    station, snapshot = station_and_snapshot(
        available_bikes=count, available_docks=count, capacity=capacity
    )
    outlook = forecast_station(station, snapshot, mode=mode, now_utc=NOW)
    assert outlook.status == "estimated"
    assert outlook.risk == expected
    assert outlook.target_utc == NOW + HORIZON


def test_busy_area_and_peak_period_adjust_once() -> None:
    peak_now = datetime(2026, 10, 19, 5, tzinfo=UTC)
    station, snapshot = station_and_snapshot(
        latitude=48.8788, longitude=2.357, available_bikes=6, station_last_reported_utc=peak_now
    )
    snapshot = replace(snapshot, fetched_at_utc=peak_now, status_source_updated_at_utc=peak_now)
    outlook = forecast_station(station, snapshot, mode="bikes", now_utc=peak_now)
    assert outlook.risk == "High"
    assert "Nord" in outlook.area and "Est" in outlook.area
    # A large buffer gets just two context points, even when zones overlap.
    station = replace(station, available_bikes=10)
    assert forecast_station(station, snapshot, mode="bikes", now_utc=peak_now).risk == "Medium"


def test_weather_adjustment_is_simple_and_never_required() -> None:
    station, snapshot = station_and_snapshot(available_bikes=6)
    without_weather = forecast_station(station, snapshot, mode="bikes", now_utc=NOW)
    assert without_weather.risk == "Medium"
    assert without_weather.weather is None
    assert any("Weather is unavailable" in reason for reason in without_weather.reasons)

    normal = forecast_station(
        station,
        snapshot,
        mode="bikes",
        now_utc=NOW,
        weather=weather(),
    )
    assert normal.risk == "Medium"
    assert normal.weather is not None

    adverse = forecast_station(
        station,
        snapshot,
        mode="bikes",
        now_utc=NOW,
        weather=weather(precipitation_probability=60),
    )
    assert adverse.risk == "Low"
    assert any("may reduce demand" in reason for reason in adverse.reasons)

    station = replace(station, available_bikes=3)
    assert (
        forecast_station(
            station,
            snapshot,
            mode="bikes",
            now_utc=NOW,
            weather=weather(precipitation_probability=60),
        ).risk
        == "High"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"temperature_c": 4.9},
        {"temperature_c": 30.1},
        {"precipitation_probability": 60},
        {"wind_kmh": 30},
    ],
)
def test_each_poor_weather_condition_reduces_demand_once(changes) -> None:
    station, snapshot = station_and_snapshot(available_bikes=6)
    outlook = forecast_station(
        station,
        snapshot,
        mode="bikes",
        now_utc=NOW,
        weather=weather(**changes),
    )
    assert outlook.risk == "Low"
    assert sum("may reduce demand" in reason for reason in outlook.reasons) == 1


def test_expired_weather_is_ignored_without_losing_the_prediction() -> None:
    station, snapshot = station_and_snapshot(available_bikes=6)
    outlook = forecast_station(
        station,
        snapshot,
        mode="bikes",
        now_utc=NOW,
        weather=weather(fetched_at_utc=NOW - timedelta(minutes=15)),
    )
    assert outlook.status == "estimated"
    assert outlook.risk == "Medium"
    assert outlook.weather is None


@pytest.mark.parametrize(
    "changes",
    [
        {"available_bikes": None},
        {"available_bikes": -1},
        {"capacity": 0},
        {"is_installed": False},
        {"is_installed": None},
        {"is_renting": False},
        {"is_renting": None},
        {"station_last_reported_utc": None},
        {"station_last_reported_utc": NOW - timedelta(seconds=7201)},
        {"station_last_reported_utc": NOW + timedelta(seconds=1)},
    ],
)
def test_invalid_station_has_no_risk(changes) -> None:
    station, snapshot = station_and_snapshot(**changes)
    outlook = forecast_station(station, snapshot, mode="bikes", now_utc=NOW)
    assert outlook.status == "unavailable"
    assert outlook.risk is None
    assert outlook.reasons


@pytest.mark.parametrize(
    "changes",
    [
        {"is_last_known": True},
        {"fetched_at_utc": NOW - timedelta(seconds=301)},
        {"status_source_updated_at_utc": NOW - timedelta(seconds=301)},
        {"status_source_updated_at_utc": NOW + timedelta(seconds=1)},
    ],
)
def test_invalid_snapshot_has_no_risk(changes) -> None:
    station, snapshot = station_and_snapshot()
    outlook = forecast_station(station, replace(snapshot, **changes), mode="bikes", now_utc=NOW)
    assert outlook.status == "unavailable"
    assert outlook.risk is None


def test_mode_specific_operations_and_zero_inventory() -> None:
    station, snapshot = station_and_snapshot(available_bikes=0, available_docks=0)
    assert forecast_station(station, snapshot, mode="bikes", now_utc=NOW).status == "already_empty"
    assert forecast_station(station, snapshot, mode="docks", now_utc=NOW).status == "already_full"
    station = replace(station, is_returning=False)
    assert forecast_station(station, snapshot, mode="docks", now_utc=NOW).status == "unavailable"
    assert forecast_station(station, snapshot, mode="bikes", now_utc=NOW).status == "already_empty"


def test_freshness_limit_is_inclusive() -> None:
    station, snapshot = station_and_snapshot(
        station_last_reported_utc=NOW - timedelta(seconds=7200)
    )
    snapshot = replace(
        snapshot,
        fetched_at_utc=NOW - timedelta(seconds=300),
        status_source_updated_at_utc=NOW - timedelta(seconds=300),
    )
    assert forecast_station(station, snapshot, mode="bikes", now_utc=NOW).status == "estimated"


def test_delayed_station_report_remains_eligible_with_a_warning() -> None:
    station, snapshot = station_and_snapshot(
        station_last_reported_utc=NOW - timedelta(seconds=5400)
    )
    outlook = forecast_station(station, snapshot, mode="bikes", now_utc=NOW)
    assert outlook.status == "estimated"
    assert any("report is delayed" in reason for reason in outlook.reasons)


@pytest.mark.parametrize(
    "local,expected",
    [
        ("2026-10-19T06:59", False),
        ("2026-10-19T07:00", True),
        ("2026-10-19T09:59", True),
        ("2026-10-19T10:00", False),
        ("2026-10-19T16:59", False),
        ("2026-10-19T17:00", True),
        ("2026-10-19T19:59", True),
        ("2026-10-19T20:00", False),
        ("2026-10-24T10:59", False),
        ("2026-10-24T11:00", True),
        ("2026-10-24T18:59", True),
        ("2026-10-24T19:00", False),
    ],
)
def test_peak_period_boundaries(local, expected) -> None:
    target = datetime.fromisoformat(local).replace(tzinfo=ZoneInfo("Europe/Paris"))
    assert is_peak_period(target.astimezone(UTC)) is expected


@pytest.mark.parametrize(
    "target,expected",
    [
        ("2026-10-25T00:30+00:00", False),
        ("2026-10-25T01:30+00:00", False),
        ("2026-10-26T06:00+00:00", True),
        ("2026-03-30T05:00+00:00", True),
    ],
)
def test_daylight_saving_conversion(target, expected) -> None:
    assert is_peak_period(datetime.fromisoformat(target)) is expected


def test_geographic_boundaries_and_catalog() -> None:
    zone = BusyZone("Test", 0, 0, 750, "https://example.test")
    boundary = degrees(750 / EARTH_RADIUS_METRES)
    assert matching_zones(0, 0, (zone, zone)) == ("Test", "Test")
    assert matching_zones(boundary - 1e-8, 0, (zone,)) == ("Test",)
    assert matching_zones(boundary + 1e-8, 0, (zone,)) == ()
    assert len(BUSY_ZONES) == 8
    for item in BUSY_ZONES:
        assert item.name in matching_zones(item.latitude, item.longitude)
    assert matching_zones(0, 0) == ()
