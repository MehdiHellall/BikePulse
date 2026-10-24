from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from bikepulse.app.view_model import AvailabilityMode
from bikepulse.prediction.rules import (
    COLD_THRESHOLD_C,
    HOT_THRESHOLD_C,
    RAIN_THRESHOLD,
    WIND_THRESHOLD_KMH,
    StationOutlook,
)

OutlookTone = Literal["positive", "caution", "critical", "neutral"]


@dataclass(frozen=True)
class OutlookPresentation:
    headline: str
    secondary: str
    tone: OutlookTone
    factors: tuple[str, ...]


def _report_age_label(age_seconds: int) -> str:
    minutes = age_seconds // 60
    hours, remaining_minutes = divmod(minutes, 60)
    if hours and remaining_minutes:
        return f"Report {hours}h {remaining_minutes}m old"
    if hours:
        return f"Report {hours}h old"
    return f"Report {minutes}m old"


def _area_label(area: str) -> str:
    prefix = "Busy area: "
    if area.startswith(prefix):
        return area.removeprefix(prefix)
    return "Outside busy zones"


def _period_label(period: str) -> str:
    return period.removesuffix(" period")


def _weather_label(outlook: StationOutlook) -> str:
    weather = outlook.weather
    if weather is None:
        return "Weather not included"
    poor_weather = (
        weather.precipitation_probability >= RAIN_THRESHOLD
        or weather.wind_kmh >= WIND_THRESHOLD_KMH
        or weather.temperature_c < COLD_THRESHOLD_C
        or weather.temperature_c > HOT_THRESHOLD_C
    )
    return "Poor weather lowers demand" if poor_weather else "Weather neutral"


def _movement_label(outlook: StationOutlook, mode: AvailabilityMode) -> str | None:
    movement = outlook.movement
    if movement is None:
        return None
    unit = "Bikes" if mode == "bikes" else "Open docks"
    if movement.direction == "falling":
        return f"{unit} falling {abs(movement.rate_per_minute):.1f}/min"
    if movement.direction == "rising":
        return f"{unit} rising {movement.rate_per_minute:.1f}/min"
    if movement.direction == "stable":
        return f"{unit} stable"
    return f"{unit} movement mixed"


def present_outlook(
    outlook: StationOutlook,
    *,
    mode: AvailabilityMode,
    report_age_seconds: int | None,
    warning_age_seconds: int,
) -> OutlookPresentation:
    if outlook.status == "already_empty":
        return OutlookPresentation("Empty now", "No forecast needed", "critical", ())
    if outlook.status == "already_full":
        return OutlookPresentation("Full now", "No forecast needed", "critical", ())
    if outlook.status == "unavailable":
        reason = outlook.reasons[0] if outlook.reasons else "Required data is unavailable."
        return OutlookPresentation("No outlook", reason, "neutral", ())

    assert outlook.risk is not None
    headlines = {
        "bikes": {
            "Low": "Looks stable",
            "Medium": "May run low",
            "High": "May run out",
        },
        "docks": {
            "Low": "Space looks stable",
            "Medium": "Space may tighten",
            "High": "May fill up",
        },
    }
    tones: dict[str, OutlookTone] = {
        "Low": "positive",
        "Medium": "caution",
        "High": "critical",
    }
    factors: list[str] = []
    if report_age_seconds is not None and report_age_seconds > warning_age_seconds:
        factors.append(_report_age_label(report_age_seconds))
    movement_label = _movement_label(outlook, mode)
    if movement_label is not None:
        factors.append(movement_label)
    if outlook.nearby_pressure_count:
        factors.append(f"{outlook.nearby_pressure_count} nearby stations under pressure")
    factors.extend(
        (_area_label(outlook.area), _period_label(outlook.period), _weather_label(outlook))
    )
    return OutlookPresentation(
        headlines[mode][outlook.risk],
        f"{outlook.risk} risk",
        tones[outlook.risk],
        tuple(factors),
    )
