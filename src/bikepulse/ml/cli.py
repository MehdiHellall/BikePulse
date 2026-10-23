from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta

from bikepulse.config import load_settings
from bikepulse.ml.dataset import utc_midnight
from bikepulse.ml.export import export_frozen_dataset
from bikepulse.ml.frozen_study import (
    frozen_report,
    lock_frozen_study,
    package_frozen_study,
    prepare_frozen_study,
    replay_frozen_study,
    run_frozen_study,
    run_frozen_test,
)
from bikepulse.ml.readiness import write_rolling_readiness_report
from bikepulse.ml.study import predict_station, run_study


def _utc_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use an ISO 8601 UTC timestamp") from exc
    if parsed.utcoffset() != timedelta(0):
        raise argparse.ArgumentTypeError("cutoff must use UTC (Z or +00:00)")
    return parsed.astimezone(UTC)


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline empty-bike ML POC")
    parser.add_argument("--config", default=os.getenv("BIKEPULSE_CONFIG", "config.example.toml"))
    commands = parser.add_subparsers(dest="command", required=True)
    study = commands.add_parser("study")
    study.add_argument("--start", required=True)
    study.add_argument("--end", required=True)
    predict = commands.add_parser("predict")
    predict.add_argument("--station-id", required=True)
    readiness = commands.add_parser("readiness")
    readiness.add_argument("--hours", required=True, type=int, choices=(24, 48))
    export = commands.add_parser("export-dataset")
    export.add_argument("--cutoff", type=_utc_timestamp)
    frozen_prepare = commands.add_parser("frozen-prepare")
    frozen_prepare.add_argument("--export-id", required=True)
    frozen_study = commands.add_parser("frozen-study")
    frozen_study.add_argument("--study-id", required=True)
    frozen_study.add_argument("--mode", required=True, choices=("smoke", "full"))
    frozen_read = commands.add_parser("frozen-report")
    frozen_read.add_argument("--study-id", required=True)
    frozen_lock = commands.add_parser("frozen-lock")
    frozen_lock.add_argument("--study-id", required=True)
    frozen_lock.add_argument("--candidate", required=True)
    frozen_test = commands.add_parser("frozen-test")
    frozen_test.add_argument("--study-id", required=True)
    frozen_replay = commands.add_parser("frozen-replay")
    frozen_replay.add_argument("--study-id", required=True)
    frozen_replay.add_argument("--station-id", required=True)
    frozen_replay.add_argument("--as-of", type=_utc_timestamp, required=True)
    frozen_package = commands.add_parser("frozen-package")
    frozen_package.add_argument("--study-id", required=True)
    commands.add_parser("report")
    args = parser.parse_args()
    settings = load_settings(args.config)
    try:
        if args.command == "study":
            output = run_study(
                settings.paths.data_dir,
                utc_midnight(args.start),
                utc_midnight(args.end),
                max_feed_age_seconds=settings.collection.max_feed_age_seconds,
                max_station_age_seconds=settings.collection.max_station_age_seconds,
            )
            print((output / "report.md").read_text())
            print(f"Artifacts: {output}")
        elif args.command == "predict":
            print(
                json.dumps(
                    predict_station(
                        settings.paths.data_dir,
                        args.station_id,
                        max_feed_age_seconds=settings.collection.max_feed_age_seconds,
                    ),
                    indent=2,
                )
            )
        elif args.command == "readiness":
            output = write_rolling_readiness_report(
                settings.paths.data_dir,
                args.hours,
                max_feed_age_seconds=settings.collection.max_feed_age_seconds,
                max_station_age_seconds=settings.collection.max_station_age_seconds,
            )
            print(output.read_text())
            print(f"Artifact: {output}")
        elif args.command == "export-dataset":
            output = export_frozen_dataset(settings.paths.data_dir, cutoff=args.cutoff)
            print((output / "quality.md").read_text())
            print(f"Artifacts: {output}")
        elif args.command == "frozen-prepare":
            output = prepare_frozen_study(settings.paths.data_dir, args.export_id)
            print((output / "data_review.md").read_text())
            print(f"Study: {output.name}")
            print(f"Artifacts: {output}")
        elif args.command == "frozen-study":
            output = run_frozen_study(settings.paths.data_dir, args.study_id, mode=args.mode)
            if args.mode == "smoke":
                print((output / "smoke" / "report.json").read_text())
            else:
                print((output / "validation_report.md").read_text())
            print(f"Artifacts: {output}")
        elif args.command == "frozen-report":
            print(frozen_report(settings.paths.data_dir, args.study_id).read_text())
        elif args.command == "frozen-lock":
            output = lock_frozen_study(settings.paths.data_dir, args.study_id, args.candidate)
            print((output / "selection.json").read_text())
        elif args.command == "frozen-test":
            output = run_frozen_test(settings.paths.data_dir, args.study_id)
            print((output / "test_report.md").read_text())
            print(f"Artifacts: {output}")
        elif args.command == "frozen-replay":
            print(
                json.dumps(
                    replay_frozen_study(
                        settings.paths.data_dir, args.study_id, args.station_id, args.as_of
                    ),
                    indent=2,
                )
            )
        elif args.command == "frozen-package":
            output = package_frozen_study(settings.paths.data_dir, args.study_id)
            print((output / "package_check.json").read_text())
            print(f"Release package: {output}")
        else:
            latest = settings.paths.data_dir / "ml" / "latest.json"
            if not latest.exists():
                parser.exit(1, "No study yet. Run make ml-poc START=... END=... first.\n")
            run_id = json.loads(latest.read_text())["run_id"]
            print(
                (settings.paths.data_dir / "ml" / "experiments" / run_id / "report.md").read_text()
            )
    except (ValueError, OSError) as exc:
        parser.exit(1, f"ML POC could not run: {exc}\n")


if __name__ == "__main__":
    main()
