from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("BIKEPULSE_RUN_E2E") != "1",
        reason="set BIKEPULSE_RUN_E2E=1 to run browser tests",
    ),
]


def _available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _streamlit_app(config: Path) -> Iterator[str]:
    repository = Path(__file__).parents[2]
    port = _available_port()
    url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment["BIKEPULSE_CONFIG"] = str(config)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            "src/bikepulse/app/main.py",
            "--server.headless=true",
            f"--server.port={port}",
        ],
        cwd=repository,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/_stcore/health", timeout=1) as response:
                if response.status == 200:
                    break
        except OSError:
            time.sleep(0.2)
    else:
        process.terminate()
        raise RuntimeError("Streamlit did not become healthy")
    try:
        yield url
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def running_app(live_config: Path) -> Iterator[str]:
    with _streamlit_app(live_config) as url:
        yield url


def test_live_app_smoke(page: Page, running_app: str, tmp_path: Path) -> None:
    page.set_viewport_size({"width": 1440, "height": 2000})
    page.goto(running_app)
    expect(page.get_by_role("heading", name="BikePulse")).to_be_visible()
    expect(page.get_by_text("Live feed connected")).to_be_visible()
    expect(page.get_by_text("Stations on map")).to_be_visible()
    expect(page.get_by_text("Select a point on the map or search for a station")).to_be_visible()

    # Alpha is positioned at the initial Paris center, so this exercises the
    # real PyDeck selection event rather than reaching into Streamlit state.
    canvas = page.locator("canvas").first
    expect(canvas).to_be_visible()
    box = canvas.bounding_box()
    assert box is not None
    canvas.click(
        position={"x": box["width"] / 2, "y": box["height"] / 2},
        force=True,
    )
    expect(page.get_by_role("heading", name="Alpha", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Current availability")).to_be_visible()
    expect(page.get_by_role("heading", name="Next 30 minutes")).to_be_visible()
    expect(page.get_by_text("Rule-based estimate — not validated")).to_be_visible()
    expect(page.get_by_text("Paris weather:")).to_be_visible()
    expect(page.get_by_text("and weather.")).to_be_visible()
    page.get_by_text("Why this outlook?", exact=True).click()
    expect(page.get_by_text("This station is in a busy area.")).to_be_visible()
    page.get_by_role("heading", name="BikePulse", exact=True).scroll_into_view_if_needed()
    page.screenshot(
        path=str(tmp_path / "live-bike-outlook.png"), full_page=True, animations="disabled"
    )

    page.get_by_role("radio", name="Open docks").click()
    expect(page.get_by_role("heading", name="Alpha", exact=True)).to_be_visible()
    expect(page.get_by_text("risk of no standard docks")).to_be_visible()
    page.get_by_role("button", name="Refresh data").click()
    expect(page.get_by_text("Live feed connected")).to_be_visible()
    page.screenshot(
        path=str(tmp_path / "live-dock-outlook.png"), full_page=True, animations="disabled"
    )


def test_delayed_station_report_keeps_live_prediction(
    page: Page, live_config: Path, tmp_path: Path
) -> None:
    live_config.write_text(live_config.read_text().replace("/status.json", "/status-delayed.json"))
    with _streamlit_app(live_config) as url:
        page.set_viewport_size({"width": 1440, "height": 2000})
        page.goto(url)
        page.get_by_role("combobox").click()
        page.get_by_text("Beta · 2", exact=True).click()
        expect(page.get_by_role("heading", name="Beta", exact=True)).to_be_visible()
        expect(page.get_by_text("High risk of no bikes")).to_be_visible()
        expect(page.get_by_text("Delayed report")).to_be_visible()
        expect(page.get_by_text("Paris weather:")).to_be_visible()
        page.get_by_text("Why this outlook?", exact=True).click()
        expect(page.get_by_text("The station report is delayed")).to_be_visible()
        expect(page.get_by_text("Live feed connected")).to_be_visible()
        page.screenshot(
            path=str(tmp_path / "live-delayed-station-outlook.png"),
            full_page=True,
            animations="disabled",
        )


def test_weather_failure_keeps_live_prediction(
    page: Page, live_config: Path, tmp_path: Path
) -> None:
    live_config.write_text(
        live_config.read_text().replace("/weather.json", "/weather-unavailable.json")
    )
    with _streamlit_app(live_config) as url:
        page.set_viewport_size({"width": 1440, "height": 2000})
        page.goto(url)
        page.get_by_role("combobox").click()
        page.get_by_text("Beta · 2", exact=True).click()
        expect(page.get_by_role("heading", name="Beta", exact=True)).to_be_visible()
        expect(page.get_by_text("High risk of no bikes")).to_be_visible()
        expect(
            page.get_by_text("Weather unavailable — the station estimate still works.")
        ).to_be_visible()
        page.screenshot(
            path=str(tmp_path / "live-weather-fallback.png"),
            full_page=True,
            animations="disabled",
        )
