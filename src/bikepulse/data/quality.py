from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from math import ceil
from pathlib import Path
from typing import Any

import polars as pl

from bikepulse.data.compaction import load_history
from bikepulse.io import atomic_write_text


def build_daily_quality_report(
    *,
    data_dir: Path,
    day: date,
    max_station_age_seconds: int = 300,
    expected_interval_minutes: int = 5,
) -> dict[str, Any]:
    start = datetime.combine(day, datetime.min.time(), UTC)
    end = start + timedelta(days=1)
    frame = load_history(data_dir, start, end)
    snapshots = frame.select("snapshot_id", "collected_at_utc", "received_at_utc").unique()
    interval_seconds = expected_interval_minutes * 60
    known_at = snapshots.select(
        pl.coalesce("received_at_utc", "collected_at_utc").alias("known_at")
    )["known_at"].to_list()
    actual = {ceil((t - start).total_seconds() / interval_seconds) for t in known_at}
    gaps: list[dict[str, str]] = []
    gap_start: int | None = None
    report_end = max(start, min(end, datetime.now(UTC)))
    elapsed_seconds = (report_end - start).total_seconds()
    slots = min(
        1440 // expected_interval_minutes,
        ceil(elapsed_seconds / interval_seconds),
    )
    actual = {slot for slot in actual if 0 <= slot < slots}
    for slot in range(slots + 1):
        if slot < slots and slot not in actual:
            if gap_start is None:
                gap_start = slot
        elif gap_start is not None:
            gaps.append(
                {
                    "start_utc": (
                        start + timedelta(minutes=gap_start * expected_interval_minutes)
                    ).isoformat(),
                    "end_utc": min(
                        report_end, start + timedelta(minutes=slot * expected_interval_minutes)
                    ).isoformat(),
                }
            )
            gap_start = None
    counts = frame.group_by("snapshot_id").len()["len"].to_list()
    stale = frame.filter(pl.col("data_age_seconds") > max_station_age_seconds).height
    missing_receipt = frame.filter(pl.col("received_at_utc").is_null()).height
    schema_errors = sum(
        json.loads(path.read_text())["schema_error_count"]
        for path in (data_dir / "quality" / "compaction" / f"date={day}").glob("*.json")
    )
    warnings = []
    for condition, warning in [
        (bool(gaps), "collection_gaps"),
        (stale > 0, "stale_station_data"),
        (len(set(counts)) > 1, "station_count_drift"),
        (schema_errors > 0, "schema_validation_errors"),
        (missing_receipt > 0, "missing_receipt_times"),
    ]:
        if condition:
            warnings.append(warning)
    report = {
        "day": str(day),
        "snapshot_count": snapshots.height,
        "window_end_utc": report_end.isoformat(),
        "complete_day": report_end == end,
        "gap_count": len(gaps),
        "gaps": gaps,
        "warnings": warnings,
        "expected_poll_count": slots,
        "observed_slot_count": len(actual),
        "row_count": frame.height,
        "stale_row_count": stale,
        "fresh_row_count": frame.height - stale,
        "missing_receipt_count": missing_receipt,
        "schema_error_count": schema_errors,
        "failed_poll_count": sum(
            json.loads(path.read_text()).get("kind") == "status"
            for path in (data_dir / "failures" / str(day)).glob("*.json")
        ),
        "storage_bytes": sum(p.stat().st_size for p in data_dir.rglob("*") if p.is_file()),
    }
    atomic_write_text(data_dir / "quality" / f"{day}.json", json.dumps(report, indent=2))
    return report
