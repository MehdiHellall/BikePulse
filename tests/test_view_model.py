from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from bikepulse.app.live_data import StationMapRow
from bikepulse.app.view_model import AMBER, GREEN, GREY, RED, availability_view, map_rows

NOW = datetime(2026, 8, 9, 21, tzinfo=UTC)


@pytest.fixture
def station() -> StationMapRow:
    return StationMapRow(
        station_id="1",
        name="Alpha",
        latitude=48.8566,
        longitude=2.3522,
        capacity=20,
        available_bikes=7,
        mechanical_bikes=5,
        electric_bikes=2,
        available_docks=13,
        is_installed=True,
        is_renting=True,
        is_returning=True,
        station_last_reported_utc=NOW,
    )


@pytest.mark.parametrize(
    ("count", "color", "label"),
    [
        (0, RED, "None available"),
        (1, AMBER, "Low availability"),
        (3, AMBER, "Low availability"),
        (4, GREEN, "Available"),
    ],
)
def test_availability_color_boundaries(
    station: StationMapRow,
    count: int,
    color: tuple[int, int, int, int],
    label: str,
) -> None:
    view = availability_view(
        replace(station, available_bikes=count),
        mode="bikes",
        now_utc=NOW,
        max_age_seconds=300,
    )
    assert view.color == color
    assert view.label == label


def test_freshness_boundary_recalculates_from_report_time(station: StationMapRow) -> None:
    fresh = availability_view(
        replace(station, station_last_reported_utc=NOW - timedelta(seconds=300)),
        mode="bikes",
        now_utc=NOW,
        max_age_seconds=300,
    )
    stale = availability_view(
        replace(station, station_last_reported_utc=NOW - timedelta(seconds=301)),
        mode="bikes",
        now_utc=NOW,
        max_age_seconds=300,
    )
    assert fresh.color == GREEN
    assert stale.color == GREY
    assert stale.label == "Stale data"


def test_missing_is_unknown_and_never_zero(station: StationMapRow) -> None:
    view = availability_view(
        replace(station, available_bikes=None),
        mode="bikes",
        now_utc=NOW,
        max_age_seconds=300,
    )
    assert view.count is None
    assert view.label == "Status unavailable"
    assert view.color == GREY


def test_operating_flags_are_specific_to_selected_mode(station: StationMapRow) -> None:
    asymmetric = replace(station, is_renting=False, is_returning=True)
    bikes = availability_view(asymmetric, mode="bikes", now_utc=NOW, max_age_seconds=300)
    docks = availability_view(asymmetric, mode="docks", now_utc=NOW, max_age_seconds=300)
    assert bikes.label == "Rentals unavailable"
    assert bikes.color == GREY
    assert docks.label == "Available"
    assert docks.color == GREEN


def test_last_known_snapshot_suppresses_availability_colors(station: StationMapRow) -> None:
    rows = map_rows(
        (station,),
        mode="bikes",
        now_utc=NOW,
        max_age_seconds=300,
        last_known=True,
    )
    assert rows[0]["availability"] == 7
    assert rows[0]["status_label"] == "Last known data"
    assert rows[0]["color"] == list(GREY)
