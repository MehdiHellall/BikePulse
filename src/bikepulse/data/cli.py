from __future__ import annotations

import argparse
import json
from datetime import date, datetime

from bikepulse.config import load_settings
from bikepulse.data.compaction import compact_status_hour
from bikepulse.data.quality import build_daily_quality_report


def compact_hour_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.example.toml")
    parser.add_argument("--hour", required=True, help="UTC hour, e.g. 2026-10-05T12:00:00+00:00")
    args = parser.parse_args()
    print(
        compact_status_hour(
            load_settings(args.config).paths.data_dir, datetime.fromisoformat(args.hour)
        ).height
    )


def quality_report_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.example.toml")
    parser.add_argument("--day", required=True)
    args = parser.parse_args()
    settings = load_settings(args.config)
    print(
        json.dumps(
            build_daily_quality_report(
                data_dir=settings.paths.data_dir,
                day=date.fromisoformat(args.day),
                max_station_age_seconds=settings.collection.max_station_age_seconds,
                expected_interval_minutes=settings.collection.expected_interval_minutes,
            ),
            indent=2,
        )
    )
