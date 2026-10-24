from __future__ import annotations

from pathlib import Path

import pytest

from bikepulse.config import load_settings


def test_load_live_defaults_without_creating_directories(settings_config: Path) -> None:
    before = set(settings_config.parent.iterdir())
    settings = load_settings(settings_config)
    assert settings.live.station_warning_age_seconds == 3600
    assert settings.live.max_station_age_seconds == 7200
    assert settings.live.max_feed_age_seconds == 300
    assert settings.movement.observation_window_seconds == 1200
    assert settings.movement.max_observations_per_station == 21
    assert settings.weather.enabled
    assert set(settings_config.parent.iterdir()) == before


@pytest.mark.parametrize("section", ["live", "movement", "weather"])
def test_settings_reject_unknown_options(settings_config: Path, section: str) -> None:
    with settings_config.open("a") as handle:
        handle.write(f"\n[{section}.unexpected]\nvalue = 1\n")
    with pytest.raises(ValueError, match="Extra inputs"):
        load_settings(settings_config)


def test_settings_reject_invalid_freshness(settings_config: Path) -> None:
    settings_config.write_text(
        settings_config.read_text().replace(
            "max_station_age_seconds = 7200", "max_station_age_seconds = 0"
        )
    )
    with pytest.raises(ValueError):
        load_settings(settings_config)


def test_settings_reject_warning_at_or_above_hard_limit(settings_config: Path) -> None:
    settings_config.write_text(
        settings_config.read_text().replace(
            "station_warning_age_seconds = 3600",
            "station_warning_age_seconds = 7200",
        )
    )
    with pytest.raises(ValueError, match="warning age"):
        load_settings(settings_config)


def test_settings_reject_movement_limits_that_cannot_fit_the_window(
    settings_config: Path,
) -> None:
    with settings_config.open("a") as handle:
        handle.write("\n[movement]\nmin_span_seconds = 1200\nobservation_window_seconds = 600\n")
    with pytest.raises(ValueError, match="trend span"):
        load_settings(settings_config)


@pytest.mark.parametrize(
    "setting",
    ["observation_window_seconds = 1201", "max_observations_per_station = 22"],
)
def test_settings_enforce_bounded_movement_storage(
    settings_config: Path,
    setting: str,
) -> None:
    with settings_config.open("a") as handle:
        handle.write(f"\n[movement]\n{setting}\n")
    with pytest.raises(ValueError):
        load_settings(settings_config)
