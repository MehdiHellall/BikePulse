from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from math import isfinite
from threading import Lock

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from bikepulse.config.settings import WeatherSettings

WEATHER_CACHE_TTL = timedelta(minutes=15)
WEATHER_RETRY_COOLDOWN = timedelta(seconds=60)
WEATHER_TIMEOUT_SECONDS = 3.0
PARIS_LATITUDE = 48.8566
PARIS_LONGITUDE = 2.3522


@dataclass(frozen=True)
class WeatherConditions:
    forecast_at_utc: datetime
    fetched_at_utc: datetime
    temperature_c: float
    precipitation_probability: float
    wind_kmh: float

    def is_usable(self, now_utc: datetime, target_utc: datetime) -> bool:
        return (
            all(
                isfinite(value)
                for value in (self.temperature_c, self.precipitation_probability, self.wind_kmh)
            )
            and -100 <= self.temperature_c <= 70
            and 0 <= self.precipitation_probability <= 100
            and self.wind_kmh >= 0
            and timedelta(0) <= now_utc - self.fetched_at_utc < WEATHER_CACHE_TTL
            and self.forecast_at_utc <= target_utc < self.forecast_at_utc + timedelta(hours=1)
        )


class _HourlyForecast(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    time: list[datetime]
    temperature_2m: list[float | None]
    precipitation_probability: list[float | None]
    wind_speed_10m: list[float | None]

    @model_validator(mode="after")
    def validate_columns(self) -> _HourlyForecast:
        if not self.time or any(
            len(column) != len(self.time)
            for column in (self.temperature_2m, self.precipitation_probability, self.wind_speed_10m)
        ):
            raise ValueError("Weather columns must have matching, nonzero lengths")
        times = [
            value.replace(tzinfo=UTC) if value.tzinfo is None else value for value in self.time
        ]
        if any(
            value.utcoffset() != timedelta(0) or value.minute or value.second or value.microsecond
            for value in times
        ):
            raise ValueError("Weather timestamps must be UTC hour boundaries")
        if any(right - left != timedelta(hours=1) for left, right in pairwise(times)):
            raise ValueError("Weather timestamps must be unique and hourly")
        self.time = times
        return self


class _WeatherResponse(BaseModel):
    utc_offset_seconds: int = Field(ge=0, le=0)
    hourly_units: dict[str, str]
    hourly: _HourlyForecast

    @model_validator(mode="after")
    def validate_units(self) -> _WeatherResponse:
        for name, unit in (
            ("temperature_2m", "°C"),
            ("precipitation_probability", "%"),
            ("wind_speed_10m", "km/h"),
        ):
            if self.hourly_units.get(name) != unit:
                raise ValueError(f"Unexpected unit for {name}")
        return self

    def conditions(
        self, target_utc: datetime, fetched_at_utc: datetime
    ) -> WeatherConditions | None:
        for index, forecast_at in enumerate(self.hourly.time):
            if not forecast_at <= target_utc < forecast_at + timedelta(hours=1):
                continue
            temperature = self.hourly.temperature_2m[index]
            rain = self.hourly.precipitation_probability[index]
            wind = self.hourly.wind_speed_10m[index]
            if (
                temperature is None
                or rain is None
                or wind is None
                or not -100 <= temperature <= 70
                or not 0 <= rain <= 100
                or wind < 0
            ):
                return None
            return WeatherConditions(forecast_at, fetched_at_utc, temperature, rain, wind)
        return None


class WeatherService:
    """One shared regional forecast; successful and failed requests stay in memory."""

    def __init__(
        self, settings: WeatherSettings, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._client = httpx.Client(timeout=WEATHER_TIMEOUT_SECONDS, transport=transport)
        self._cached: _WeatherResponse | None = None
        self._fetched_at: datetime | None = None
        self._retry_at: datetime | None = None
        self._lock = Lock()

    def get_conditions(
        self, *, target_utc: datetime, now_utc: datetime
    ) -> WeatherConditions | None:
        if not self._settings.enabled:
            return None
        with self._lock:
            if (
                self._cached is not None
                and self._fetched_at is not None
                and timedelta(0) <= now_utc - self._fetched_at < WEATHER_CACHE_TTL
            ):
                return self._cached.conditions(target_utc, self._fetched_at)
            if self._retry_at is not None and now_utc < self._retry_at:
                return None
            self._cached = None
            self._fetched_at = None
            try:
                response = self._client.get(
                    str(self._settings.forecast_url),
                    params={
                        "latitude": PARIS_LATITUDE,
                        "longitude": PARIS_LONGITUDE,
                        "hourly": "temperature_2m,precipitation_probability,wind_speed_10m",
                        "timezone": "UTC",
                        "temperature_unit": "celsius",
                        "wind_speed_unit": "kmh",
                        "forecast_days": 2,
                    },
                )
                response.raise_for_status()
                forecast = _WeatherResponse.model_validate(response.json())
                conditions = forecast.conditions(target_utc, now_utc)
                if conditions is None:
                    raise ValueError("No valid weather for forecast target")
            except (httpx.HTTPError, ValidationError, ValueError):
                self._retry_at = now_utc + WEATHER_RETRY_COOLDOWN
                return None
            self._cached = forecast
            self._fetched_at = now_utc
            self._retry_at = None
            return conditions

    def close(self) -> None:
        self._client.close()
