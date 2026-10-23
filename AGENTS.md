# Repository Guidelines

## Start a session

Run `git status --short` before editing. Preserve existing modifications and untracked
work; never use a blanket reset or `git clean -fdx` for housekeeping. Read `README.md`,
the documentation index at `docs/README.md`, and the latest dated project audit under
`docs/audits/` before choosing work. Report relevant existing failures separately from
regressions introduced by your changes.

## Project structure and current scope

Application code lives in `src/bikepulse/`:

- `collector/`: GBFS fetching, validation, immutable raw storage, and user service.
- `config/`: typed TOML settings and environment overrides.
- `app/`: live Streamlit map, cached feeds, and availability presentation.
- `data/`: rebuildable bronze/silver Parquet history and quality reports.
- `ml/`: canonical observations, labels, readiness, the original 14-day study,
  frozen exports/model studies, historical replay, and temporal-feature research.
- `io.py`: atomic artifact writes shared by those packages.

Tests and controlled feed responses live in `tests/` and `tests/fixtures/`; browser
checks live in `tests/e2e/`. `experiments/` contains versioned research configuration,
not disposable output. Architecture, data contracts, and operations are indexed in
`docs/README.md`; plans live in `docs/plans/`, research reports and published plots in
`docs/research/`, and point-in-time evidence in `docs/audits/`. Preserve earlier audits.
Runtime observations, caches for research, exports, and models belong under ignored
root `data/`.

The live map and offline studies are implemented. Historical replay is available
through the frozen-study CLI; live forecasts and replay in the map remain roadmap
work. Collection was frozen on 15 October 2026; inspect service state before assuming
it is running. Starting collection or deploying forecasts requires a task that asks
for it.

## Environment and commands

Use Python 3.12 and the locked environment. `make setup` installs local `uv` under
`.tools/` and syncs `.venv` from `uv.lock`; reuse them when already available. Never
install project packages into system Python.

- `make run`: launch the Streamlit map.
- `.venv/bin/bikepulse-collect --config config.example.toml`: fetch and store one
  status snapshot; this contacts the public feed and writes observations.
- `make collect-start`, `make collect-status`, `make collect-stop`: manage the Linux
  user service. `collect-status` also rebuilds derived history and writes reports.
- `make ml-readiness HOURS=24` (or `48`): rebuild and inspect label readiness.
- `make ml-poc START=YYYY-MM-DD END=YYYY-MM-DD`: original study, exactly 14 completed
  UTC days, END exclusive. `make ml-report` and `make ml-predict STATION_ID=...` use
  this study, not a frozen export.
- `make ml-dataset CUTOFF=<UTC-timestamp>`: frozen export; existing frozen evidence
  should be verified rather than regenerated during routine housekeeping.
- `make ml-frozen-report STUDY_ID=...`: read existing frozen-study results. Other
  `ml-frozen-*` stages and their parameters are described in `docs/operations.md`
  and `docs/plans/frozen-model-study.md`; respect that plan's review checkpoints
  when executing a new study.
- `make ml-temporal-study`: development-only research configured by
  `experiments/temporal-history.toml`. It writes ignored artifacts and refreshes the
  published research report and plots; it is not a read-only inspection command.
- `make clean`: remove tool caches, coverage, package builds, and source/test
  bytecode while preserving `.venv/`, `.tools/`, and `data/`.

Start configuration from `config.example.toml`. `BIKEPULSE_CONFIG` selects another
app/collector/ML CLI TOML file; `BIKEPULSE_DATA_DIR` overrides its data path. The separate
temporal-study runner takes `--config` and resolves its data/output paths from its
experiment TOML rather than these environment overrides.

## Validation and style

Before a pull request, run `make verify`: Ruff formatting/lint, strict mypy, pytest
with at least 80% coverage, dependency audit, package build, and Playwright smoke.
`make check` omits the browser step but still needs vulnerability-service access.
Both test paths need local sockets for fixture servers. `make smoke` installs Chromium
and runs the browser check. Report unavailable checks and failing audits explicitly;
do not weaken checks to obtain a green result. Use `make format` for formatting and
safe lint fixes.

Use four-space indentation, type annotations, and `from __future__ import annotations`.
Ruff enforces 100-character lines and import ordering; mypy runs in strict mode.
Use `snake_case` for functions/modules, `PascalCase` for classes, and `UPPER_SNAKE_CASE`
for constants. Keep collection, validation, storage, UI, and research concerns in their
own packages.

Add focused `test_*.py` cases for behavior changes and failure paths. Reuse fixtures
and `tmp_path`; tests must never call public GBFS feeds or consume the real study
artifacts. Synthetic results establish engineering behavior, not model quality.

## Data and research safeguards

- Missing bike/dock counts stay unknown; stale or last-known data must be labeled.
- Keep collection start, receipt, feed publication, and station report timestamps
  distinct. Feature inputs must be available at the prediction boundary.
- Preserve whole-snapshot selection, exact lag joins, strict incomplete-label
  rejection, chronological splits, and label-window purges.
- Fit preprocessing and sampling on training only. Validation/test retain natural
  prevalence. Describe scores as uncalibrated unless separately validated.
- The frozen final test starts at `2026-10-14T00:00:00Z` and has already been used.
  Do not reopen it for selection or repeat evaluation during cleanup. Temporal research
  stays strictly before that boundary; further model-quality claims need new evidence.
- Preserve raw feeds, poll receipts, station metadata, frozen exports, model packages,
  and experiment provenance. A directory being ignored does not make it disposable.
- Never commit secrets, raw archives, or generated model artifacts. Load only trusted
  locally produced joblib models. Keep dependency changes explicit in `uv.lock`.

## Commits and pull requests

Use short, descriptive commit subjects, optionally prefixed with the relevant milestone.
There is no mandatory formal convention. Do not stage or commit unrelated existing work.
Pull requests should explain the behavior change, link the relevant plan/issue, report
checks and known failures, and include screenshots for UI changes.
