from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, field_validator


class PathSettings(BaseModel):
    data_dir: Path = Path("data")

    @field_validator("data_dir")
    @classmethod
    def data_dir_is_not_file(cls, value: Path) -> Path:
        if value.exists() and value.is_file():
            raise ValueError("data_dir must be a directory")
        return value


class GbfsSettings(BaseModel):
    status_url: HttpUrl
    information_url: HttpUrl
    user_agent: str = Field(min_length=8)
    timeout_seconds: float = Field(gt=0, le=60)
    max_retries: int = Field(ge=0, le=5)
    backoff_seconds: float = Field(ge=0, le=30)


class CollectionSettings(BaseModel):
    max_station_age_seconds: int = Field(gt=0, le=3600)
    expected_interval_minutes: int = Field(gt=0, le=60)
    daily_station_refresh_hour_utc: int = Field(ge=0, le=23)


class AppSettings(BaseModel):
    paths: PathSettings
    gbfs: GbfsSettings
    collection: CollectionSettings


def _apply_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    data_dir = os.getenv("BIKEPULSE_DATA_DIR")
    if data_dir:
        raw.setdefault("paths", {})["data_dir"] = data_dir
    return raw


def load_settings(path: str | Path) -> AppSettings:
    config_path = Path(path)
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    settings = AppSettings.model_validate(_apply_env_overrides(raw))
    base = config_path.parent
    data_dir = settings.paths.data_dir
    if not data_dir.is_absolute():
        settings.paths.data_dir = (base / data_dir).resolve()
    return settings
