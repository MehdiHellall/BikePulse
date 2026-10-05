from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bikepulse.config import load_settings
from bikepulse.data.compaction import load_history, rebuild_history
from bikepulse.data.quality import build_daily_quality_report
from bikepulse.io import atomic_write_text

SERVICE = "bikepulse-collector.service"


def service_text(repository: Path, config: Path, data_dir: Path) -> str:
    def quote(value: Path) -> str:
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'

    return f"""[Unit]
Description=BikePulse five-minute GBFS collection

[Service]
Type=simple
WorkingDirectory={str(repository).replace("%", "%%")}
ExecStart={quote(repository / ".venv/bin/python")} -m bikepulse.collector.worker \
--config {quote(config)}
Environment={quote(Path("BIKEPULSE_DATA_DIR=" + str(data_dir)))}
Restart=on-failure
RestartSec=30
Environment=PYTHONUNBUFFERED=1
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "status", "stop"])
    parser.add_argument(
        "--config", type=Path, default=Path(os.getenv("BIKEPULSE_CONFIG", "config.example.toml"))
    )
    args = parser.parse_args()
    config = args.config.resolve()
    settings = load_settings(config)
    if args.action == "start":
        repository = Path.cwd().resolve()
        unit = Path.home() / ".config/systemd/user" / SERVICE
        atomic_write_text(unit, service_text(repository, config, settings.paths.data_dir))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "start", SERVICE], check=True)
        print("Collection started. It pauses while the laptop sleeps or shuts down.")
    elif args.action == "stop":
        subprocess.run(["systemctl", "--user", "stop", SERVICE], check=True)
    else:
        subprocess.run(["systemctl", "--user", "status", SERVICE, "--no-pager"], check=False)
        now = datetime.now(UTC)
        rebuild_history(settings.paths.data_dir, now - timedelta(hours=48), now)
        for day in ((now - timedelta(days=1)).date(), now.date()):
            print(
                json.dumps(
                    build_daily_quality_report(
                        data_dir=settings.paths.data_dir,
                        day=day,
                        max_station_age_seconds=settings.collection.max_station_age_seconds,
                    ),
                    indent=2,
                )
            )
        history = load_history(settings.paths.data_dir, now - timedelta(hours=48), now)
        fresh = history.filter(
            history["data_age_seconds"] <= settings.collection.max_station_age_seconds
        ).height
        print(f"48h audit: {history.height} observations; {fresh} fresh at receipt.")
        print(f"Logs: journalctl --user -u {SERVICE} -n 50")


if __name__ == "__main__":
    main()
