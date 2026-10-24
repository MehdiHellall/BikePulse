from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bikepulse.app.outlook_presentation import present_outlook
from bikepulse.prediction.movement import InventoryMovement
from bikepulse.prediction.rules import StationOutlook
from bikepulse.prediction.weather import WeatherConditions

NOW = datetime(2026, 10, 23, 18, tzinfo=UTC)
NORMAL_WEATHER = WeatherConditions(NOW, NOW, 16, 10, 12)
POOR_WEATHER = WeatherConditions(NOW, NOW, 16, 70, 12)


def outlook(
    *,
    status="estimated",
    risk="Low",
    area="Busy area: Central Paris",
    period="Off-peak period",
    weather=NORMAL_WEATHER,
    reasons=("Current availability provides a useful buffer.",),
    movement=None,
    nearby_pressure_count=0,
) -> StationOutlook:
    return StationOutlook(
        status,
        risk,
        NOW,
        7,
        area,
        period,
        weather,
        reasons,
        movement=movement,
        nearby_pressure_count=nearby_pressure_count,
    )


@pytest.mark.parametrize(
    "mode,risk,headline,tone",
    [
        ("bikes", "Low", "Looks stable", "positive"),
        ("bikes", "Medium", "May run low", "caution"),
        ("bikes", "High", "May run out", "critical"),
        ("docks", "Low", "Space looks stable", "positive"),
        ("docks", "Medium", "Space may tighten", "caution"),
        ("docks", "High", "May fill up", "critical"),
    ],
)
def test_estimated_outlook_copy(mode, risk, headline, tone) -> None:
    presentation = present_outlook(
        outlook=outlook(risk=risk),
        mode=mode,
        report_age_seconds=30,
        warning_age_seconds=3600,
    )
    assert presentation.headline == headline
    assert presentation.secondary == f"{risk} risk"
    assert presentation.tone == tone
    assert presentation.factors == ("Central Paris", "Off-peak", "Weather neutral")


@pytest.mark.parametrize(
    "status,headline",
    [("already_empty", "Empty now"), ("already_full", "Full now")],
)
def test_current_terminal_states_do_not_claim_a_forecast(status, headline) -> None:
    presentation = present_outlook(
        outlook=outlook(status=status, risk=None),
        mode="bikes",
        report_age_seconds=30,
        warning_age_seconds=3600,
    )
    assert presentation.headline == headline
    assert presentation.secondary == "No forecast needed"
    assert presentation.factors == ()


def test_unavailable_outlook_uses_only_the_first_blocking_reason() -> None:
    presentation = present_outlook(
        outlook=outlook(
            status="unavailable",
            risk=None,
            weather=None,
            reasons=("Station report time is stale or in the future.", "Unused reason."),
        ),
        mode="bikes",
        report_age_seconds=8000,
        warning_age_seconds=3600,
    )
    assert presentation.headline == "No outlook"
    assert presentation.secondary == "Station report time is stale or in the future."
    assert presentation.factors == ()


def test_delayed_report_and_weather_fallback_stay_visible() -> None:
    presentation = present_outlook(
        outlook=outlook(weather=None),
        mode="bikes",
        report_age_seconds=5400,
        warning_age_seconds=3600,
    )
    assert presentation.factors == (
        "Report 1h 30m old",
        "Central Paris",
        "Off-peak",
        "Weather not included",
    )


def test_poor_weather_factor_describes_its_adjustment() -> None:
    presentation = present_outlook(
        outlook=outlook(weather=POOR_WEATHER),
        mode="bikes",
        report_age_seconds=30,
        warning_age_seconds=3600,
    )
    assert presentation.factors[-1] == "Poor weather lowers demand"


def test_recent_movement_and_nearby_pressure_are_visible_factors() -> None:
    recent = InventoryMovement("falling", -0.4, 0.9, 1, 5, 15)
    presentation = present_outlook(
        outlook=outlook(movement=recent, nearby_pressure_count=3),
        mode="bikes",
        report_age_seconds=30,
        warning_age_seconds=3600,
    )
    assert presentation.factors[:2] == (
        "Bikes falling 0.4/min",
        "3 nearby stations under pressure",
    )
