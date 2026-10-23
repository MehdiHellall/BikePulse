from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from bikepulse.config.settings import WeatherSettings
from bikepulse.prediction.weather import WeatherService

NOW = datetime(2026, 10, 23, 23, 40, tzinfo=UTC)
TARGET = NOW + timedelta(minutes=30)


def weather_payload():
    return {
        "utc_offset_seconds": 0,
        "hourly_units": {
            "temperature_2m": "°C",
            "precipitation_probability": "%",
            "wind_speed_10m": "km/h",
        },
        "hourly": {
            "time": ["2026-10-23T23:00", "2026-10-24T00:00", "2026-10-24T01:00"],
            "temperature_2m": [18, 10, 8],
            "precipitation_probability": [0, 65, 10],
            "wind_speed_10m": [10, 30, 12],
        },
    }


def test_regional_request_selects_target_hour_and_reuses_cache() -> None:
    calls = []

    def handler(request):
        calls.append(request)
        assert request.extensions["timeout"]["read"] == 3.0
        return httpx.Response(200, json=weather_payload())

    service = WeatherService(WeatherSettings(enabled=True), transport=httpx.MockTransport(handler))
    first = service.get_conditions(target_utc=TARGET, now_utc=NOW)
    assert first is not None
    assert first.temperature_c == 10
    assert first.precipitation_probability == 65
    assert first.forecast_at_utc == datetime(2026, 10, 24, tzinfo=UTC)
    assert service.get_conditions(target_utc=TARGET, now_utc=NOW + timedelta(minutes=14)) == first
    assert len(calls) == 1
    assert calls[0].url.params["latitude"] == "48.8566"
    assert calls[0].url.params["longitude"] == "2.3522"
    assert calls[0].url.params["timezone"] == "UTC"
    assert calls[0].url.params["forecast_days"] == "2"
    assert (
        service.get_conditions(target_utc=TARGET, now_utc=NOW + timedelta(minutes=15)) is not None
    )
    assert len(calls) == 2
    service.close()


def test_expired_weather_is_discarded_and_failure_cooldown_recovers() -> None:
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls == 2 else httpx.Response(200, json=weather_payload())

    service = WeatherService(WeatherSettings(enabled=True), transport=httpx.MockTransport(handler))
    assert service.get_conditions(target_utc=TARGET, now_utc=NOW) is not None
    failed_at = NOW + timedelta(minutes=15)
    assert service.get_conditions(target_utc=TARGET, now_utc=failed_at) is None
    assert (
        service.get_conditions(target_utc=TARGET, now_utc=failed_at + timedelta(seconds=59)) is None
    )
    assert calls == 2
    assert (
        service.get_conditions(target_utc=TARGET, now_utc=failed_at + timedelta(seconds=60))
        is not None
    )
    assert calls == 3


@pytest.mark.parametrize("failure", ["timeout", "connection", "http", "json"])
def test_weather_failures_return_none_and_are_not_retried_immediately(failure) -> None:
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if failure == "connection":
            raise httpx.ConnectError("offline", request=request)
        return (
            httpx.Response(503) if failure == "http" else httpx.Response(200, content=b"not json")
        )

    service = WeatherService(WeatherSettings(enabled=True), transport=httpx.MockTransport(handler))
    assert service.get_conditions(target_utc=TARGET, now_utc=NOW) is None
    assert service.get_conditions(target_utc=TARGET, now_utc=NOW + timedelta(seconds=1)) is None
    assert calls == 1


@pytest.mark.parametrize(
    "change",
    [
        "offset",
        "units",
        "empty",
        "mismatch",
        "duplicate",
        "gap",
        "offset_time",
        "missing",
        "nan",
        "null",
        "rain",
        "wind",
        "temperature",
        "target_missing",
        "half_hour",
    ],
)
def test_malformed_weather_has_neutral_fallback(change) -> None:
    payload = copy.deepcopy(weather_payload())
    hourly = payload["hourly"]
    if change == "offset":
        payload["utc_offset_seconds"] = 3600
    elif change == "units":
        payload["hourly_units"]["wind_speed_10m"] = "mph"
    elif change == "empty":
        hourly["time"] = []
    elif change == "mismatch":
        hourly["temperature_2m"] = []
    elif change == "duplicate":
        hourly["time"][1] = hourly["time"][0]
    elif change == "gap":
        hourly["time"][2] = "2026-10-24T03:00"
    elif change == "offset_time":
        hourly["time"][1] = "2026-10-24T00:00+02:00"
    elif change == "missing":
        del hourly["wind_speed_10m"]
    elif change == "nan":
        hourly["temperature_2m"][1] = "NaN"
    elif change == "null":
        hourly["temperature_2m"][1] = None
    elif change == "rain":
        hourly["precipitation_probability"][1] = 101
    elif change == "wind":
        hourly["wind_speed_10m"][1] = -1
    elif change == "temperature":
        hourly["temperature_2m"][1] = -101
    elif change == "half_hour":
        hourly["time"] = ["2026-10-23T23:30", "2026-10-24T00:30", "2026-10-24T01:30"]
    else:
        hourly["time"] = ["2026-10-22T00:00", "2026-10-22T01:00", "2026-10-22T02:00"]
    service = WeatherService(
        WeatherSettings(enabled=True),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    assert service.get_conditions(target_utc=TARGET, now_utc=NOW) is None


def test_disabled_weather_makes_no_request() -> None:
    def handler(_request):
        pytest.fail("Disabled weather must not request the network")

    service = WeatherService(WeatherSettings(enabled=False), transport=httpx.MockTransport(handler))
    assert service.get_conditions(target_utc=TARGET, now_utc=NOW) is None


def test_cached_forecast_with_missing_target_is_unavailable() -> None:
    service = WeatherService(
        WeatherSettings(enabled=True),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=weather_payload())),
    )
    assert service.get_conditions(target_utc=TARGET, now_utc=NOW) is not None
    assert service.get_conditions(target_utc=TARGET + timedelta(days=1), now_utc=NOW) is None
