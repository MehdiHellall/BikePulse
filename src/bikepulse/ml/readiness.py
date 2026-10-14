from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from bikepulse.data.compaction import load_history, rebuild_history
from bikepulse.io import atomic_write_text
from bikepulse.ml.dataset import build_dataset, canonical_grid


def completed_boundary(now: datetime | None = None) -> datetime:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    return current.replace(second=0, microsecond=0, minute=(current.minute // 5) * 5)


def build_readiness_report(
    data_dir: Path,
    start: datetime,
    end: datetime,
    *,
    max_feed_age_seconds: int = 300,
    max_station_age_seconds: int = 300,
) -> dict[str, Any]:
    if start.utcoffset() != timedelta(0) or end.utcoffset() != timedelta(0) or end <= start:
        raise ValueError("Readiness range must be a positive UTC interval.")
    if (end - start).total_seconds() % 300:
        raise ValueError("Readiness boundaries must align to five-minute UTC slots.")

    rebuild_history(data_dir, start - timedelta(minutes=20), end)
    history = load_history(data_dir, start - timedelta(minutes=20), end)
    grid = canonical_grid(
        history,
        start - timedelta(minutes=15),
        end,
        max_feed_age_seconds=max_feed_age_seconds,
    )
    scoped_grid = grid.filter((pl.col("as_of_utc") >= start) & (pl.col("as_of_utc") < end))
    dataset = build_dataset(grid).filter(
        (pl.col("as_of_utc") >= start) & (pl.col("label_end_utc") <= end)
    )

    expected_slots = int((end - start).total_seconds() // 300)
    observed_slots = scoped_grid.select("as_of_utc").unique().height
    labeled = dataset.filter(pl.col("label").is_not_null())
    positive = labeled.filter(pl.col("label") == 1).height
    negative = labeled.filter(pl.col("label") == 0).height
    rejections = (
        dataset.group_by("rejection_reason").len().sort("len", descending=True).to_dicts()
        if not dataset.is_empty()
        else []
    )

    feed_age = (
        pl.col("as_of_utc") - pl.col("feed_updated_at_utc")
    ).dt.total_microseconds() / 1_000_000
    feed_fresh = scoped_grid.filter((feed_age >= 0) & (feed_age <= max_feed_age_seconds)).height
    station_age = (
        pl.col("as_of_utc") - pl.col("station_last_reported_utc")
    ).dt.total_microseconds() / 1_000_000
    station_fresh = scoped_grid.filter(
        (station_age >= 0) & (station_age <= max_station_age_seconds)
    ).height
    feature_eligible = dataset.filter(pl.col("feature_rejection").is_null()).height
    coverage_rate = observed_slots / expected_slots if expected_slots else 0.0

    return {
        "status": "labels_forming" if positive > 0 and negative > 0 else "insufficient_labels",
        "claim": "published-feed stockout readiness; not a model-quality result",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "start_utc": start.isoformat(),
        "end_exclusive_utc": end.isoformat(),
        "window_hours": (end - start).total_seconds() / 3600,
        "freshness_basis": "published feed update and local receipt time",
        "max_feed_age_seconds": max_feed_age_seconds,
        "max_station_age_seconds_diagnostic": max_station_age_seconds,
        "expected_poll_slots": expected_slots,
        "observed_poll_slots": observed_slots,
        "poll_coverage_rate": coverage_rate,
        "seven_day_poll_coverage_target": 0.98,
        "poll_coverage_target_met": coverage_rate >= 0.98,
        "grid_row_count": scoped_grid.height,
        "feed_fresh_row_count": feed_fresh,
        "station_fresh_row_count": station_fresh,
        "feature_eligible_row_count": feature_eligible,
        "complete_label_count": labeled.height,
        "positive_label_count": positive,
        "negative_label_count": negative,
        "rejection_counts": rejections,
    }


def readiness_markdown(report: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# ML label readiness",
            "",
            f"Status: **{report['status']}**",
            "",
            f"UTC window: {report['start_utc']} through {report['end_exclusive_utc']} (exclusive).",
            "",
            "This checks published-feed label formation; it is not a model-quality result.",
            "",
            "| Check | Value |",
            "|---|---:|",
            f"| Poll slots | {report['observed_poll_slots']} / {report['expected_poll_slots']} |",
            f"| Poll coverage | {report['poll_coverage_rate']:.2%} |",
            f"| Feed-fresh station rows | {report['feed_fresh_row_count']} |",
            f"| Station-fresh rows (diagnostic) | {report['station_fresh_row_count']} |",
            f"| Feature-eligible rows | {report['feature_eligible_row_count']} |",
            f"| Complete labels | {report['complete_label_count']} |",
            f"| Positive labels | {report['positive_label_count']} |",
            f"| Negative labels | {report['negative_label_count']} |",
            "",
            "The 98% poll-coverage target is reported separately and is not required for a "
            "preliminary label check.",
            "",
            "## Rejections",
            "",
            "```json",
            json.dumps(report["rejection_counts"], indent=2),
            "```",
            "",
        ]
    )


def _write_report(data_dir: Path, report: dict[str, Any], stem: Path) -> Path:
    root = data_dir / "ml" / "readiness"
    json_path = root / f"{stem}.json"
    markdown_path = root / f"{stem}.md"
    atomic_write_text(json_path, json.dumps(report, indent=2))
    atomic_write_text(markdown_path, readiness_markdown(report))
    atomic_write_text(
        root / "latest.json",
        json.dumps({"report": str(json_path.relative_to(root))}, indent=2),
    )
    return markdown_path


def write_daily_readiness_report(
    data_dir: Path,
    day: date,
    *,
    max_feed_age_seconds: int = 300,
    max_station_age_seconds: int = 300,
) -> Path:
    start = datetime.combine(day, datetime.min.time(), UTC)
    report = build_readiness_report(
        data_dir,
        start,
        start + timedelta(days=1),
        max_feed_age_seconds=max_feed_age_seconds,
        max_station_age_seconds=max_station_age_seconds,
    )
    return _write_report(data_dir, report, Path("daily") / str(day))


def write_rolling_readiness_report(
    data_dir: Path,
    hours: int,
    *,
    now: datetime | None = None,
    max_feed_age_seconds: int = 300,
    max_station_age_seconds: int = 300,
) -> Path:
    if hours not in (24, 48):
        raise ValueError("Readiness HOURS must be 24 or 48.")
    end = completed_boundary(now)
    report = build_readiness_report(
        data_dir,
        end - timedelta(hours=hours),
        end,
        max_feed_age_seconds=max_feed_age_seconds,
        max_station_age_seconds=max_station_age_seconds,
    )
    return _write_report(data_dir, report, Path("rolling") / f"{hours}h")
