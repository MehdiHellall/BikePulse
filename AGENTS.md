# Repository Guidelines

## Start a session

Run `git status --short` before editing. Preserve existing modifications and untracked
work; never use a blanket reset or `git clean -fdx` for housekeeping. Read `README.md`
and `docs/README.md` before choosing work. Report existing failures separately from regressions.

## Project structure

Application code lives in `src/bikepulse/`:

- `app/`: live Streamlit map, in-memory snapshots and availability presentation.
- `config/`: typed TOML settings.
- `feeds/`: GBFS HTTP fetching and per-record validation.
- `prediction/`: explicit station-outlook rules, geographic catalog and regional weather.

Tests and controlled responses live in `tests/` and `tests/fixtures/`; browser checks live
in `tests/e2e/`. Architecture, contracts, rules and operations are indexed in `docs/README.md`.
The app has one live availability page. It stores feed, weather and outlook state in memory.
Do not introduce persistent observation or prediction history without an explicit task.

## Environment and commands

Use Python 3.12 and the locked environment. Reuse `.tools/` and `.venv/` when available.
`make setup` installs local uv under `.tools/` and syncs `.venv` from `uv.lock`.
Never install project packages into system Python.

- `make run`: launch Streamlit.
- `make verify`: formatting/lint, strict mypy, pytest with at least 80% coverage,
  dependency audit, source/wheel builds and Playwright smoke tests.
- `make check`: the same checks except browser smoke.
- `make smoke`: install Chromium and run browser checks.
- `make format`: format and apply safe lint fixes.
- `make clean`: remove disposable caches, coverage, builds and bytecode, preserving
  `.venv/`, `.tools/` and source files.

Start configuration from `config.example.toml`. `BIKEPULSE_CONFIG` selects another TOML.
The supported sections are `[gbfs]`, `[live]` and `[weather]`; unknown settings are rejected.
Complete verification requires local fixture sockets, Chromium and vulnerability-service
access. Report unavailable checks explicitly; do not weaken checks to obtain a green result.

## Style and validation

Use four-space indentation, type annotations and `from __future__ import annotations`.
Ruff enforces 100-character lines and import ordering; mypy runs in strict mode.
Use snake_case functions/modules, PascalCase classes and UPPER_SNAKE_CASE constants.
Keep feed validation, UI, weather transport and pure prediction rules separate.

Add focused `test_*.py` cases for changed behavior and failure paths. Reuse fixtures and
`tmp_path`. Tests must never contact public feeds or weather services. Browser screenshots
must identify controlled fixtures. Synthetic tests verify engineering behavior, not forecast
quality. Keep dependency changes explicit in `uv.lock`.

## Data and prediction safeguards

- Missing counts remain unknown. Label stale and last-known availability explicitly.
- Keep feed publication, receipt, station-report and weather-valid times distinct.
- Withhold outlooks for stale/missing station data or unavailable required operations.
- Only fresh, valid, target-hour weather may adjust the rule result. Weather failure is
  neutral and visible, with the configured cooldown.
- Rules are unvalidated assumptions. Do not display invented probabilities or accuracy.
- Keep thresholds and geographic definitions explicit and versioned; explain adjustments.
- Both modes share turnover assumptions. Do not silently add directional travel claims.
- Never commit secrets. Preserve normal Git history unless explicitly asked to rewrite it.

## Commits and pull requests

Use short, descriptive commit subjects. Do not stage unrelated existing work.
PRs should explain behavior, report checks and known failures, and include UI screenshots.
