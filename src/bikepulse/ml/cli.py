from __future__ import annotations

import argparse
import json
import os

from bikepulse.config import load_settings
from bikepulse.ml.dataset import utc_midnight
from bikepulse.ml.readiness import write_rolling_readiness_report
from bikepulse.ml.study import predict_station, run_study


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
