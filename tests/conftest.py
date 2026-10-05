from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def live_gbfs_server() -> Iterator[str]:
    reported = int(datetime.now(UTC).timestamp())
    information = {
        "last_updated": reported,
        "ttl": 86400,
        "data": {
            "stations": [
                {"station_id": "1", "name": "Alpha", "lat": 48.8566, "lon": 2.3522, "capacity": 20},
                {"station_id": "2", "name": "Beta", "lat": 48.857, "lon": 2.35, "capacity": 20},
            ]
        },
    }
    status = {
        "last_updated": reported,
        "ttl": 60,
        "data": {
            "stations": [
                {
                    "station_id": "1",
                    "is_installed": 1,
                    "is_renting": 1,
                    "is_returning": 1,
                    "num_bikes_available": 7,
                    "num_bikes_available_types": [{"mechanical": 5}, {"ebike": 2}],
                    "num_docks_available": 13,
                    "last_reported": reported,
                },
                {
                    "station_id": "2",
                    "is_installed": 1,
                    "is_renting": 1,
                    "is_returning": 1,
                    "num_bikes_available": 2,
                    "num_bikes_available_types": [{"mechanical": 1}, {"ebike": 1}],
                    "num_docks_available": 18,
                    "last_reported": reported,
                },
            ]
        },
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = information if self.path == "/information.json" else status
            content = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.fixture
def live_config(tmp_path: Path, live_gbfs_server: str) -> Path:
    config = tmp_path / "live-config.toml"
    config.write_text(
        f"""
[paths]
data_dir = "data"

[gbfs]
status_url = "{live_gbfs_server}/status.json"
information_url = "{live_gbfs_server}/information.json"
user_agent = "BikePulse tests"
timeout_seconds = 2.0
max_retries = 0
backoff_seconds = 0.0

[collection]
max_station_age_seconds = 300
expected_interval_minutes = 5
daily_station_refresh_hour_utc = 3
""",
        encoding="utf-8",
    )
    return config


@pytest.fixture
def settings_config(tmp_path: Path) -> Path:
    config = tmp_path / "settings-config.toml"
    config.write_text(
        """
[paths]
data_dir = "data"

[gbfs]
status_url = "https://example.test/status.json"
information_url = "https://example.test/information.json"
user_agent = "BikePulse tests"
timeout_seconds = 2.0
max_retries = 0
backoff_seconds = 0.0

[collection]
max_station_age_seconds = 300
expected_interval_minutes = 5
daily_station_refresh_hour_utc = 3
""",
        encoding="utf-8",
    )
    return config
