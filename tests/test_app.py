from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_app_renders_live_network_and_station_search(live_config: Path, monkeypatch) -> None:
    monkeypatch.setenv("BIKEPULSE_CONFIG", str(live_config))
    app_path = Path(__file__).parents[1] / "src" / "bikepulse" / "app" / "main.py"

    app = AppTest.from_file(app_path, default_timeout=10).run()

    assert not app.exception
    assert app.title[0].value == "BikePulse"
    assert app.success[0].value == "Live feed connected"
    assert app.selectbox[0].options == [
        "Search by station name or ID",
        "Alpha · 1",
        "Beta · 2",
    ]

    app.selectbox[0].select("Alpha · 1").run()
    assert not app.exception
    assert any("Alpha" in element.value for element in app.markdown)
