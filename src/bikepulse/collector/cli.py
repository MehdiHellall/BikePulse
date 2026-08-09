from __future__ import annotations

import argparse

from bikepulse.collector.service import run_once
from bikepulse.config import load_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.example.toml")
    return parser


def collect_main() -> None:
    args = _parser().parse_args()
    stored = run_once("status", load_settings(args.config))
    print(stored.path)


def stations_main() -> None:
    args = _parser().parse_args()
    stored = run_once("information", load_settings(args.config))
    print(stored.path)
