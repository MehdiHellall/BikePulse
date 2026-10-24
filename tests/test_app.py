from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest


def test_app_renders_live_network_and_station_search(live_config: Path, monkeypatch) -> None:
    monkeypatch.setenv("BIKEPULSE_CONFIG", str(live_config))
    app_path = Path(__file__).parents[1] / "src" / "bikepulse" / "app" / "main.py"

    app = AppTest.from_file(app_path, default_timeout=10).run()

    assert not app.exception
    assert app.title[0].value == "BikePulse"
    assert any(">Live<" in element.value for element in app.markdown)

    assert app.selectbox[0].options == [
        "Find a station by name or ID",
        "Alpha · 1",
        "Beta · 2",
    ]

    app.selectbox[0].select("Alpha · 1").run()
    assert not app.exception
    assert any("<h2>Alpha</h2>" in element.value for element in app.markdown)
    assert any(
        outcome in element.value
        for element in app.markdown
        for outcome in ("Looks stable", "May run low")
    )
    assert any("Rule estimate · not validated" in element.value for element in app.markdown)
    assert any("Paris weather:" in element.value for element in app.markdown)
    assert any("Weather neutral" in element.value for element in app.markdown)
    assert not (Path(__file__).parents[1] / "data").exists()

    app.segmented_control[0].set_value("docks").run()
    assert not app.exception
    assert any(
        outcome in element.value
        for element in app.markdown
        for outcome in ("Space looks stable", "Space may tighten")
    )


@pytest.mark.parametrize("state", ["empty", "stale"])
def test_current_and_unavailable_states(live_config: Path, monkeypatch, state) -> None:
    def unexpected_weather(*args, **kwargs):
        pytest.fail("Weather must not be fetched without an eligible prediction")

    monkeypatch.setattr(
        "bikepulse.prediction.weather.WeatherService.get_conditions",
        unexpected_weather,
    )
    live_config.write_text(live_config.read_text().replace("/status.json", f"/status-{state}.json"))
    monkeypatch.setenv("BIKEPULSE_CONFIG", str(live_config))
    app_path = Path(__file__).parents[1] / "src" / "bikepulse" / "app" / "main.py"
    app = AppTest.from_file(app_path, default_timeout=10).run()
    assert not app.exception
    app.selectbox[0].select("Alpha · 1").run()
    assert not app.exception
    if state == "empty":
        assert any("Empty now" in element.value for element in app.markdown)
        app.segmented_control[0].set_value("docks").run()
        assert not app.exception
        assert any("Full now" in element.value for element in app.markdown)
    else:
        assert any("No outlook" in element.value for element in app.markdown)
        assert any("Feed publication time is stale" in element.value for element in app.markdown)


def test_delayed_station_report_still_gets_an_outlook(live_config: Path, monkeypatch) -> None:
    live_config.write_text(live_config.read_text().replace("/status.json", "/status-delayed.json"))
    monkeypatch.setenv("BIKEPULSE_CONFIG", str(live_config))
    app_path = Path(__file__).parents[1] / "src" / "bikepulse" / "app" / "main.py"
    app = AppTest.from_file(app_path, default_timeout=10).run()
    app.selectbox[0].select("Beta · 2").run()
    assert not app.exception
    assert any("May run out" in element.value for element in app.markdown)
    assert any("Reported " in element.value for element in app.markdown)
    assert any("Report " in element.value and " old" in element.value for element in app.markdown)
    assert any("Plan B" in element.value and "Alpha" in element.value for element in app.markdown)
    plan_b_button = next(button for button in app.button if button.label == "Choose this station")
    plan_b_button.click().run()
    assert not app.exception
    assert any("<h2>Alpha</h2>" in element.value for element in app.markdown)
    assert any("Paris weather:" in element.value for element in app.markdown)
    assert any("report is delayed" in element.value for element in app.markdown)
    assert any(">Live<" in element.value for element in app.markdown)


def test_weather_failure_keeps_the_station_prediction(live_config: Path, monkeypatch) -> None:
    live_config.write_text(
        live_config.read_text().replace("/weather.json", "/weather-unavailable.json")
    )
    monkeypatch.setenv("BIKEPULSE_CONFIG", str(live_config))
    app_path = Path(__file__).parents[1] / "src" / "bikepulse" / "app" / "main.py"
    app = AppTest.from_file(app_path, default_timeout=10).run()
    app.selectbox[0].select("Beta · 2").run()
    assert not app.exception
    assert any("May run out" in element.value for element in app.markdown)
    assert any("Weather not included" in element.value for element in app.markdown)
