# Repository Guidelines

## Project Structure & Module Organization

Application code lives in `src/bikepulse/`: `collector/` ingests GBFS, `config/` loads TOML settings, `app/` contains the Streamlit map, `data/` rebuilds history, and `ml/` implements the offline empty-bike study. Tests and captured responses live in `tests/` and `tests/fixtures/`. Follow `POC_PLAN.md` and `ML_PLAN.md`; `MVP_PLAN.md` is the longer roadmap. Generated artifacts belong under ignored root `data/`.

## Build, Test, and Development Commands

Use Python 3.12. Run `make setup` to create the locked environment and `make run` for the live map. Run `.venv/bin/bikepulse-collect --config config.example.toml` for one stored status snapshot. Set `BIKEPULSE_CONFIG` to select another TOML file.

Use `make collect-start`, `make collect-status`, and `make collect-stop` for the user service. Run `make ml-poc START=YYYY-MM-DD END=YYYY-MM-DD` for exactly 14 UTC days (END exclusive), `make ml-report` to read results, and `make ml-predict STATION_ID=...` for a collected station.

Before a pull request, run `make verify`; it covers formatting, lint, types, tests, dependency audit, package build, and the Playwright browser check. Use `make check` when Chromium is unavailable. Use `make format` to apply formatting and safe lint fixes.

## Coding Style & Naming Conventions

Use four-space indentation, type annotations, and the existing `from __future__ import annotations` convention. Ruff enforces a 100-character line length and import ordering; mypy runs in strict mode. Name modules and functions in `snake_case`, classes in `PascalCase`, and constants in `UPPER_SNAKE_CASE`. Keep collection, validation, storage, and UI concerns in their respective packages.

## Testing Guidelines

Add focused `test_*.py` cases for behavior and failure paths. Reuse fixtures and `tmp_path`; tests must never call public feeds. Pytest enforces 80% coverage. Browser tests run with `make smoke`. Compaction tests are active. ML tests cover timestamp leakage, strict invalid labels, purged splits, and artifact reload. Synthetic results never establish model quality.

## Commit & Pull Request Guidelines

The history currently has one milestone-prefixed commit (`M1 history pipeline and project skeleton`), so there is no established formal convention. Use short, descriptive subjects, preferably with the relevant milestone (for example, `M1 add hourly compaction`). Pull requests should explain the change, link the relevant issue or plan item, report the checks run, and include screenshots for UI changes.

## Configuration & Data Safety

Start from `config.example.toml`; `BIKEPULSE_DATA_DIR` overrides its data path. Never present missing counts as zero or stale data as current. Do not commit secrets, raw feed archives, or generated model artifacts.
