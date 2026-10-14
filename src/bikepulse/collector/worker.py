from __future__ import annotations

import argparse
import json
import logging
import shutil
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from bikepulse.collector.service import run_once
from bikepulse.config import AppSettings, load_settings
from bikepulse.data.compaction import compact_status_hour
from bikepulse.data.quality import build_daily_quality_report
from bikepulse.io import atomic_write_text
from bikepulse.ml.readiness import write_daily_readiness_report

LOG = logging.getLogger(__name__)


def seconds_until_poll(now: datetime) -> float:
    """Poll 30 seconds before the next five-minute boundary; never catch up gaps."""
    elapsed = now.timestamp()
    return (int((elapsed + 30) // 300) + 1) * 300 - 30 - elapsed


def collect_cycle(settings: AppSettings, *, refresh_information: bool) -> bool:
    data_dir = settings.paths.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(data_dir).free < 5 * 1024**3:
        LOG.warning("Less than 5 GiB disk space remains; raw history is not automatically deleted")
    kinds: list[Literal["information", "status"]] = (
        ["information", "status"] if refresh_information else ["status"]
    )
    information_ok = True
    for kind in kinds:
        try:
            stored = run_once(kind, settings)
            LOG.info("Collected %s: %s", kind, stored.snapshot_id)
        except Exception as exc:
            stamp = datetime.now(UTC)
            failure = {"kind": kind, "failed_at_utc": stamp.isoformat(), "error": str(exc)}
            atomic_write_text(
                data_dir / "failures" / f"{stamp:%Y-%m-%d}" / f"{stamp:%H%M%S%f}-{kind}.json",
                json.dumps(failure),
            )
            LOG.exception("Collection failed for %s", kind)
            if kind == "information":
                information_ok = False
    return information_ok


def refresh_readiness_reports(settings: AppSettings, days: list[date]) -> set[date]:
    completed: set[date] = set()
    for day in days:
        try:
            output = write_daily_readiness_report(
                settings.paths.data_dir,
                day,
                max_feed_age_seconds=settings.collection.max_feed_age_seconds,
                max_station_age_seconds=settings.collection.max_station_age_seconds,
            )
            LOG.info("Wrote ML readiness report: %s", output)
            completed.add(day)
        except Exception:
            LOG.exception("ML readiness rebuild failed for %s; raw snapshots are preserved", day)
    return completed


def run_worker(config: Path) -> None:
    settings = load_settings(config)
    if settings.collection.expected_interval_minutes != 5:
        raise ValueError("ML POC collection requires a five-minute interval")
    refreshed_day = None
    compacted_hour = None
    readiness_days: set[date] = set()
    required_readiness_days: set[date] = set()
    readiness_backfill_attempted = False
    while True:
        now = datetime.now(UTC)
        if collect_cycle(settings, refresh_information=refreshed_day != now.date()):
            refreshed_day = now.date()
        hour = now.replace(minute=0, second=0, microsecond=0)
        if hour != compacted_hour:
            try:
                compact_status_hour(settings.paths.data_dir, hour - timedelta(hours=1))
                build_daily_quality_report(
                    data_dir=settings.paths.data_dir,
                    day=(now - timedelta(days=1)).date(),
                    max_station_age_seconds=settings.collection.max_station_age_seconds,
                    max_feed_age_seconds=settings.collection.max_feed_age_seconds,
                )
                compacted_hour = hour
            except Exception:
                LOG.exception("History/quality rebuild failed; raw snapshots are preserved")
            yesterday = now.date() - timedelta(days=1)
            required_readiness_days.add(yesterday)
            if not readiness_backfill_attempted:
                required_readiness_days.add(now.date() - timedelta(days=2))
                readiness_backfill_attempted = True
            pending_readiness = sorted(required_readiness_days - readiness_days)
            if pending_readiness:
                readiness_days.update(refresh_readiness_reports(settings, pending_readiness))
        time.sleep(seconds_until_poll(datetime.now(UTC)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.example.toml"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_worker(args.config)


if __name__ == "__main__":
    main()
