from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GbfsSettings(SettingsModel):
    status_url: HttpUrl
    information_url: HttpUrl
    user_agent: str = Field(min_length=8)
    timeout_seconds: float = Field(gt=0, le=60)
    max_retries: int = Field(ge=0, le=5)
    backoff_seconds: float = Field(ge=0, le=30)


class LiveSettings(SettingsModel):
    station_warning_age_seconds: int = Field(default=3600, gt=0, le=21600)
    max_station_age_seconds: int = Field(default=7200, gt=0, le=21600)
    max_feed_age_seconds: int = Field(default=300, gt=0, le=3600)

    @model_validator(mode="after")
    def validate_station_age_limits(self) -> LiveSettings:
        if self.station_warning_age_seconds >= self.max_station_age_seconds:
            raise ValueError("station warning age must be lower than maximum station age")
        return self


class WeatherSettings(SettingsModel):
    enabled: bool = True
    forecast_url: HttpUrl = HttpUrl("https://api.open-meteo.com/v1/forecast")


class MovementSettings(SettingsModel):
    observation_window_seconds: int = Field(default=1200, ge=300, le=1200)
    max_observations_per_station: int = Field(default=21, ge=3, le=21)
    min_observations: int = Field(default=3, ge=3, le=21)
    min_span_seconds: int = Field(default=300, ge=60, le=1200)
    max_sample_age_seconds: int = Field(default=300, ge=60, le=1200)
    rebalancing_min_count: int = Field(default=8, ge=1, le=100)
    rebalancing_min_capacity_share: float = Field(default=0.35, gt=0, le=1)
    stable_rate_per_minute: float = Field(default=0.1, gt=0, le=2)

    @model_validator(mode="after")
    def validate_movement_limits(self) -> MovementSettings:
        if self.min_observations > self.max_observations_per_station:
            raise ValueError("minimum observations cannot exceed the per-station limit")
        if self.min_span_seconds > self.observation_window_seconds:
            raise ValueError("minimum trend span cannot exceed the observation window")
        if self.max_sample_age_seconds > self.observation_window_seconds:
            raise ValueError("maximum sample age cannot exceed the observation window")
        return self


class AppSettings(SettingsModel):
    gbfs: GbfsSettings
    live: LiveSettings = Field(default_factory=LiveSettings)
    movement: MovementSettings = Field(default_factory=MovementSettings)
    weather: WeatherSettings = Field(default_factory=WeatherSettings)


def load_settings(path: str | Path) -> AppSettings:
    with Path(path).open("rb") as handle:
        return AppSettings.model_validate(tomllib.load(handle))
