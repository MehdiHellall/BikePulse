from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
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


@pytest.fixture
def running_app(live_config: Path) -> Iterator[str]:
    repository = Path(__file__).parents[2]
    port = _available_port()
    url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment["BIKEPULSE_CONFIG"] = str(live_config)
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


def test_live_app_smoke(page: Page, running_app: str) -> None:
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(running_app)
    expect(page.get_by_role("heading", name="BikePulse")).to_be_visible()
    expect(page.get_by_text("Live feed connected")).to_be_visible()
    expect(page.get_by_text("Stations on map")).to_be_visible()
    expect(page.get_by_text("Station details")).to_be_visible()

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

    page.get_by_role("radio", name="Open docks").click()
    expect(page.get_by_role("heading", name="Alpha", exact=True)).to_be_visible()
