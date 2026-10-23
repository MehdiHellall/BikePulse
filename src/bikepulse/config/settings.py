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


class AppSettings(SettingsModel):
    gbfs: GbfsSettings
    live: LiveSettings = Field(default_factory=LiveSettings)
    weather: WeatherSettings = Field(default_factory=WeatherSettings)


def load_settings(path: str | Path) -> AppSettings:
    with Path(path).open("rb") as handle:
        return AppSettings.model_validate(tomllib.load(handle))
