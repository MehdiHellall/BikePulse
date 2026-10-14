# Vélib Stockout Predictor — MVP execution plan

This is the future product roadmap. The implemented system is documented in
[Architecture](../architecture.md), [Current data contract](../data-contract.md),
and [Operations](../operations.md).

## 1. MVP outcome

Deliver one deployed application with two tightly connected modes:

1. **Live:** current station status plus probability of becoming empty or full within 30 minutes.
2. **Replay:** select a past prediction time and compare the stored forecast with the outcome observed during the following 30 minutes.

The replay is not a second modeling project. It is a visual surface over stored out-of-sample predictions and the same delayed labels used by evaluation.

## 2. Scope lock

### Required

- Official Vélib GBFS collection every five minutes.
- Immutable raw snapshots and validated Parquet history.
- Exact timestamp-based empty/full incident labels.
- Inventory and trend baselines.
- Logistic-regression baseline and LightGBM main model.
- Purged chronological evaluation and probability calibration.
- Live Streamlit/PyDeck risk map.
- Station detail with current state and previous-hour inventory.
- Empty/full top-20 tables.
- Minimal evaluation page.
- 24-hour replay with forecast/outcome classifications.
- Tests, CI, Docker, structured logs, and clear stale-data states.

### Deferred

- Weather and event data.
- Neighbor/network-pressure features and historical station/time priors.
- Separate electric-bike prediction target.
- Other prediction horizons.
- Route planning or station recommendations.
- User accounts, favorites, alerts, and notifications.
- Playback animation or adjustable replay speed.
- More than 24 hours of interactive replay.
- SHAP or per-row model explanations.
- Large hyperparameter searches and automatic retraining.
- Large interactive evaluation slice dashboards.
- Truck-routing or rebalancing optimization.
- Graph neural networks, survival models, and online learning.
- PostgreSQL, Kafka, Airflow, or a feature store.
- Mobile application.

Scope changes require removing or deferring an item of similar size.

## 3. Technology decisions

| Concern | Choice | Reason |
|---|---|---|
| Language | Python 3.12 | One language for collection, ML, evaluation, and app |
| Dataframes | Polars | Efficient timestamp and grouped feature operations |
| Query layer | DuckDB | Direct querying of Parquet and replay snapshots |
| Storage | JSON.gz raw + date/hour Parquet | Recoverable source plus efficient analysis |
| Models | scikit-learn + LightGBM | Strong, interpretable baselines and tabular model |
| App | Streamlit | Fast implementation with no separate API required |
| Map | PyDeck | Complete-network rendering in Streamlit |
| Charts | Altair | Lightweight station and calibration charts |
| Validation | Pydantic models plus explicit data checks | Boundary validation and readable failures |
| Tests | Pytest + coverage | Unit and integration tests |
| Quality | Ruff + type checking | Fast automated checks |
| Packaging | `pyproject.toml` + lock file | Reproducible environment |
| Delivery | Docker + CI | Reproducible build and deployment |
| Runtime | One persistent host/VM with a mounted volume | Enough for scheduled jobs and the read-only app without distributed infrastructure |

Avoid introducing a second frontend or web API for the MVP. Streamlit can query DuckDB directly for one replay timestamp.

## 4. System boundaries

External inputs:

```text
station status, every 5 minutes
https://velib-metropole-opendata.smovengo.cloud/opendata/Velib_Metropole/station_status.json

station information, at startup and daily
https://velib-metropole-opendata.smovengo.cloud/opendata/Velib_Metropole/station_information.json
```

```text
Official GBFS
    │
    ▼
collector ──► raw JSON.gz ──► hourly Parquet
                                  │
                    ┌─────────────┴─────────────┐
                    ▼                           ▼
             feature/label build        data-quality report
                    │
                    ▼
          training + calibration
                    │
                    ▼
             versioned models
                    │
                    ▼
live scorer ──► stored predictions ──► delayed outcome resolver
                    │                           │
                    └─────────────┬─────────────┘
                                  ▼
                            DuckDB views
                                  │
                   ┌──────────────┴──────────────┐
                   ▼                             ▼
                Live map                    Replay mode
```

The app is read-only. Collection, training, scoring, and outcome resolution run independently of page views.

## 5. Critical data contracts

### Station observation

Primary key: `(snapshot_id, station_id)`.

```text
snapshot_id
collected_at_utc
received_at_utc
feed_updated_at_utc
station_id
station_last_reported_utc
data_age_seconds
is_installed
is_renting
is_returning
available_bikes
mechanical_bikes
electric_bikes
available_docks
capacity
latitude
longitude
```

### Target eligibility

A row at canonical time `t` is eligible only when the station is installed, renting,
returning, has positive capacity, was received in the preceding five minutes, has
a feed publication age at or below 300 seconds, and has all six usable future
observations at `t+5, …, t+30`. Per-station report age remains a separate diagnostic.

```text
empty target: bikes_t > 0 and bikes reach 0 in one of the next six rows
full target:  docks_t > 0 and standard docks reach 0 in one of the next six rows
```

Already-empty/full rows are not future-risk examples. Missing, stale, inactive, or unresolved future rows produce a null label and an invalid replay outcome, never a negative. “Full” means zero standard docks; Station+ behavior remains a stated limitation.

### Immutable prediction run

Primary key: `prediction_run_id`.

```text
prediction_run_id
as_of_utc
generated_at_utc
source_snapshot_id
source_collected_at_utc
empty_model_version
full_model_version
empty_decision_threshold
full_decision_threshold
feature_schema_version
prediction_origin: live | backtest
```

`as_of_utc` is the canonical grid time. Labels and replay always inspect `as_of_utc + 5` through `as_of_utc + 30`. A live run is immutable after it is written; corrections create a new versioned record.

### Station prediction

Primary key: `(prediction_run_id, station_id)`.

```text
prediction_run_id
station_id
available_bikes
mechanical_bikes
electric_bikes
available_docks
capacity
source_received_at_utc
feed_updated_at_utc
data_age_seconds
station_last_reported_utc
empty_probability
full_probability
empty_risk_band
full_risk_band
empty_prediction_status
full_prediction_status
```

The run-level model versions and thresholds are the only source for classification. `prediction_origin` prevents retrospectively generated backtest predictions from being presented as forecasts produced live at that time. Replay uses the threshold stored with the original run, never the current model threshold.

### Delayed outcome

Primary key: `(prediction_run_id, station_id, target)`.

```text
prediction_run_id
station_id
target: empty | full
outcome_status: valid | invalid
event_observed
first_event_time_utc
evaluated_at_utc
invalid_reason
```

The finalizer writes an outcome only after the window matures. Until then, `pending` is derived by the replay view from the absence of a final outcome. Outcomes are deterministically rebuildable from immutable predictions and observations; the resolved replay table is a convenience join, not a source of truth.

### Resolved replay row

```text
prediction_run_id
as_of_utc
outcome_window_end_utc
station_id
target
probability
decision_threshold
predicted_alert
outcome_status: pending | valid | invalid
event_observed
first_event_time_utc
lead_time_minutes
confusion_class: TP | FP | FN | TN | INVALID | PENDING
model_version
prediction_origin
```

Definitions:

```text
predicted_alert = probability >= decision_threshold
event_observed = boundary reached at least once in (t, t+30]
lead_time = first_event_time - as_of_utc
```

Only `valid` outcomes enter precision, recall, confusion, and lead-time metrics. Valid-forecast rate uses all matured predictions as its denominator.

### Evaluation artifact

```text
model_version
target
evaluation_start_utc
evaluation_end_utc
event_prevalence
average_precision
best_baseline_average_precision
precision
recall
precision_at_20
recall_at_20
lift_at_20
brier_score
log_loss
event_recall
median_warning_lead_time
alert_episodes_per_day
false_alert_episodes_per_day
valid_prediction_rate
decision_threshold
threshold_selection_rule
threshold_status: qualified | constraint_unmet
```

### Model manifest

```text
model_version
target
predictor_type
created_at_utc
training_interval
calibration_interval
validation_interval
feature_names_and_types
feature_schema_hash
hyperparameters
calibration_method
decision_threshold
threshold_selection_rule
threshold_status
risk_band_thresholds
dataset_fingerprint
code_revision
library_versions
artifact_sha256
```

Start live display bands at `LOW < 0.30`, `MEDIUM 0.30–0.70`, and `HIGH >= 0.70`. Keep them only if validation calibration makes those labels meaningful; otherwise choose and document alternatives before opening the final test. Replay classification always uses `decision_threshold`, not the visual band boundary.

## 6. Replay MVP behavior

### Controls

- Target toggle: empty/full.
- Timestamp selector: mature five-minute points from the last 24 hours; station outcomes may be valid or invalid.
- Result filter: all, correct warnings, missed events, false warnings.
- Badge: `LIVE FORECAST` or `BACKTEST` according to prediction origin.

Newest selectable time:

```text
current time - 30-minute outcome window - collection tolerance
```

### Map classification

| Class | Meaning | Display |
|---|---|---|
| TP | alert and event | blue |
| FP | alert, no event | orange |
| FN | no alert, event | red |
| TN | no alert, no event | muted green/grey |
| INVALID | missing, stale, or inactive outcome | grey hatch/disabled |

Pair colors with labels/tooltips so result meaning does not depend on color alone. Do not animate stations between timestamps in the MVP.

### Station panel

```text
Forecast as of: 08:10
Generated at: 08:11
Target: empty within 30 minutes
Probability: 84%
Alert threshold: 63%
Observed window: 08:15–08:40
Reached zero: 08:32
Result: correct warning
Lead time: 22 minutes
Model: empty-lgbm-2026-09-01
Origin: live forecast
```

Include the inventory series from `t-30` through `t+30` with a vertical line at prediction time. Future observations are visible only in replay, never in live mode.

### Replay scoreboard

For the selected target and rolling 24-hour replay window:

```text
valid predictions
distinct events observed
distinct events detected
missed events
false alert episodes
event recall
median first-warning lead time
valid forecast rate
```

Repeated warnings for the same station/target are consolidated into alert episodes for the scoreboard. The map classification remains specific to the selected run. This is an operational window, not the formal holdout evaluation; label it accordingly.

Event accounting is deterministic:

```text
detected event = at least one valid alert in the 30 minutes before event start
warning lead time = event start - earliest matched alert in that 30-minute window
alert episode = consecutive five-minute alerts for one station/target
false alert episode = alert episode with no event in the union of its forecast windows
```

One event can be detected only once in the scoreboard, even when several predictions warned about it.

## 7. Milestones

| Milestone | Outcome | Gate |
|---|---|---|
| M0 — Runnable skeleton | Package, configuration, CI, fixtures | Clean checkout passes CI and one command starts app |
| M1 — Trustworthy history | Collector, raw storage, compaction, quality report | Collector runs for 24 hours and the quality report explains gaps and stale data |
| M2 — Correct targets | Grid, labels, events, base features | Hand-worked fixtures and leakage tests pass |
| M3 — Honest model | Baselines, models, calibration, walk-forward report | Main model compared with every baseline on chronological data |
| M4 — Live product | Scheduled scoring and live risk UI | Stale data suppresses predictions; full network renders |
| M5 — Replay | Resolved outcomes and 24-hour replay | Every map class traces to a stored prediction and future window |
| M6 — Release | Docker, deployment, documentation, final QA | Definition of done passes from clean environment |

Do not start model-polishing before M2. Do not start replay UI before the replay table is correct.

## 8. Issue-level work breakdown

Estimates are focused engineering time for one person. They exclude the calendar time needed to accumulate real observations.

### M0 — Runnable skeleton

| ID | Task | Depends on | Estimate | Acceptance check |
|---|---|---|---:|---|
| FND-01 | Create the package, app, test, config, data, model, and documentation directories from the agreed repository tree | — | 0.5 day | Package imports; `pytest` discovers tests; Streamlit opens a placeholder page |
| FND-02 | Implement typed TOML settings; reserve environment overrides for deployment paths and credentials | FND-01 | 0.5 day | Invalid horizon, timeout, or path configuration fails at startup with a useful message |
| FND-03 | Pin dependencies, add Ruff, type checking, coverage, and CI | FND-01 | 0.5 day | Clean checkout passes format, lint, types, tests, and dependency scan |
| FND-04 | Build versioned fixtures: GBFS error cases plus a six-station time series, expected labels/replay states, and a test-time model/manifest factory | FND-01 | 0.75 day | Fixtures contain no production-sized dump or committed model binary and can drive both app modes offline |

Deliverable: a boring but dependable skeleton. No model or UI work starts before this gate passes.

### M1 — Trustworthy history

| ID | Task | Depends on | Estimate | Acceptance check |
|---|---|---|---:|---|
| COL-01 | Define Pydantic schemas and normalize status and information responses | FND-02, FND-04 | 0.5 day | Valid fixture becomes the normalized contract; invalid records produce typed error codes |
| COL-02 | Implement HTTP client: timeouts, retry/backoff, user agent, hash, latency, and safe error logging | COL-01 | 0.5 day | Retryable and non-retryable failures behave differently in deterministic tests |
| COL-03 | Implement one-shot collector and immutable atomic `json.gz` writes | COL-02 | 0.75 day | Interrupted write cannot expose a partial snapshot; repeated content still creates an auditable poll record |
| COL-04 | Refresh the slowly changing station dimension at startup and daily | COL-01 | 0.5 day | A changed name, position, or capacity closes the old record and opens a new one |
| DAT-01 | Compact one UTC hour of raw snapshots into bronze Parquet | COL-03 | 0.75 day | Re-running a partition is idempotent and row uniqueness is enforced |
| DAT-02 | Create silver observations and daily quality report | DAT-01, COL-04 | 0.75 day | Report exposes gaps, freshness, station-count drift, warnings, and storage totals |
| COL-05 | Run the collector on durable scheduled infrastructure | COL-03 | 0.5 day | At least 24 hours of snapshots survive a worker/container restart and can be rebuilt into Parquet |

Deliverable: raw evidence plus rebuildable tables. A dashboard that merely shows fresh GBFS data is not M1 completion.

### M2 — Correct targets and features

| ID | Task | Depends on | Estimate | Acceptance check |
|---|---|---|---:|---|
| GRID-01 | Map observations onto the canonical five-minute UTC grid | DAT-02 | 1 day | Boundary, tolerance, duplicate-poll, and missing-poll tests pass |
| LAB-01 | Implement independent empty/full 30-minute eligibility and labels | GRID-01 | 1 day | Hand-worked six-step cases match exactly; incomplete or stale futures yield null |
| EVT-01 | Consolidate zero-inventory runs into stockout events | LAB-01 | 0.5 day | One continuous stockout creates one event, not one event per row |
| FEAT-01 | Implement current state, lags, deltas, rolling values, slopes, and local calendar features | GRID-01 | 1 day | Every feature has an “available at prediction time” test |
| DATASET-01 | Join features, labels, eligibility, station metadata, and row provenance | LAB-01, FEAT-01 | 0.5 day | Each training row can be traced back to source snapshots; keys are unique |

Deliverable: one deterministic command rebuilds a modeling dataset for a date range. Leakage tests are the M2 gate.

#### Frozen first-stage feature set

```text
current bikes, docks, capacity, and inventory ratios
mechanical/electric counts
feed age and station-report age diagnostic
5/15/30/60-minute lags and changes
30/60-minute rolling mean, min, max, and standard deviation
15/30-minute slope
consecutive increasing/decreasing steps
local hour, weekday, weekend, and cyclic time encodings
station ID as a LightGBM categorical feature only
```

Missing values remain explicit until fold-local preprocessing. No weather, neighbor aggregation, historical station/time prior, or future-filled value enters this version.

### M3 — Honest model and evaluation

| ID | Task | Depends on | Estimate | Acceptance check |
|---|---|---|---:|---|
| EVAL-01 | Implement chronological folds with a purge of at least the 30-minute label horizon | DATASET-01 | 0.5 day | No training label window overlaps validation or test time |
| BASE-01 | Implement inventory and trend baseline scores | EVAL-01 | 0.5 day | Both baselines emit reproducible scores through the same evaluator contract |
| MOD-01 | Train regularized logistic regression with imputation and encoding inside the fold | EVAL-01 | 0.5 day | Preprocessing is fitted on training data only; artifact reload reproduces predictions |
| MOD-02 | Train one fixed, regularized LightGBM configuration with early stopping | EVAL-01 | 0.5 day | Configuration and seed are recorded; test set is untouched until final evaluation |
| CAL-01 | Calibrate every deployable candidate, including baselines, and select alert thresholds on validation data | BASE-01, MOD-01, MOD-02 | 0.5 day | Calibration and threshold selection never inspect the test labels |
| EVAL-02 | Produce row-level, event-level, ranking, calibration, a small QA slice table, and bootstrap reports | BASE-01, CAL-01, EVT-01 | 1 day | Report compares every candidate on the same out-of-sample rows |
| MOD-03 | Select the target-specific predictor and write its artifact plus manifest atomically | EVAL-02 | 0.25 day | Logistic/LightGBM is promoted only when qualified; otherwise the strongest calibrated baseline is selected and the reason recorded |

Deliverable: separate empty and full artifacts with a reproducible report. LightGBM is not automatically the winner.

Baseline definitions are fixed:

```text
inventory score, empty = 1 / (available_bikes + 1)
inventory score, full  = 1 / (available_docks + 1)
trend score            = estimated time to boundary from a validation-selected 15/30-minute slope
```

For alert thresholds, test inventory cutoffs `k ∈ {1,2,3,4,5}` on validation only. Flat or improving trends produce no projected crossing.

The alert-threshold rule is frozen before the final test: choose the validation threshold with the highest recall subject to precision of at least 60%. If no threshold satisfies the constraint, use the validation F1 maximum for replay continuity, store `constraint_unmet` in the manifest, and do not describe that target as operationally qualified.

### M4 — Live product

| ID | Task | Depends on | Estimate | Acceptance check |
|---|---|---|---:|---|
| SCORE-01 | Build the five-minute scorer and persist immutable run/station predictions | FEAT-01, FND-04 | 0.75 day | Fixture artifact works; schema mismatch, stale input, or missing model fails closed; writes are atomic |
| SCORE-02 | Materialize latest predictions, recent station history, and collection status for the app | SCORE-01, DAT-02 | 0.5 day | App inputs rebuild deterministically and contain no raw-history scan requirement |
| UI-01 | Build the full-network map with empty/full target switch and honest state legend | SCORE-02 | 1 day | Every active station appears; stale predictions cannot look live |
| UI-02 | Add station detail and top-20 risk tables | UI-01 | 0.75 day | Panel shows inventory, probability, band, source time, and model version |
| UI-03 | Add the evaluation page from versioned report artifacts | EVAL-02 | 0.5 day | Core metrics, one reliability plot, baselines, dates, and limitations are visible without retraining |

Deliverable: the live path reads immutable artifacts and never trains, mutates history, or calls the feed from a user session. Development uses a fixture model artifact; operational activation requires MOD-03 promotion.

Live display precedence is fixed:

```text
stale/unavailable
  → inactive
  → empty now or full now
  → future risk probability and band
```

No probability is shown when a higher-precedence state applies.

### M5 — 24-hour prediction replay

| ID | Task | Depends on | Estimate | Acceptance check |
|---|---|---|---:|---|
| RPL-01 | Resolve immutable prediction runs after their 30-minute outcome windows | SCORE-01, LAB-01 | 0.75 day | Pending, valid, and invalid outcomes are distinguished; the resolver is idempotent |
| RPL-02 | Expose DuckDB queries for selectable timestamps, map rows, station context, and scoreboard | RPL-01 | 0.5 day | Queries return only resolved rows and complete within the UI budget on 24 hours of data |
| RPL-03 | Add replay controls, outcome coloring, station evidence panel, and operational scoreboard | RPL-02, UI-01 | 1 day | A selected station’s class can be recomputed from the displayed probability and observations |
| RPL-04 | Add origin labels and replay end-to-end tests | RPL-03 | 0.5 day | Live and backtest rows cannot be visually confused; changing time/target updates every panel consistently |

Deliverable: a user can inspect what the system predicted, what happened next, and where it was wrong. Estimated replay increment: 2–3 focused days once M4 exists.

### M6 — Release

| ID | Task | Depends on | Estimate | Acceptance check |
|---|---|---|---:|---|
| OPS-01 | Emit structured collection, scoring, resolution, and model-health logs | M4, M5 | 0.5 day | Freshness and consecutive-failure thresholds are visible and actionable |
| REL-01 | Add a non-root Docker image and production startup commands | M4, M5 | 0.5 day | Image builds in CI and the app starts with read-only data/model mounts |
| REL-02 | Create a captured and hand-worked fixture mode for deterministic local use | M5 | 0.5 day | Both app modes work offline and fixture metrics are visibly marked non-evaluative |
| DOC-01 | Finish README, data contract, labeling note, and model card | M6 code complete | 0.75 day | A new machine can run the sample path and understand claims and limits without tribal knowledge |
| QA-01 | Run clean-environment, responsive-layout, failure-mode, and accessibility smoke tests | All | 0.5 day | Definition of done passes and no critical or high-severity review issue remains |

## 9. Test strategy

Use a red-green-refactor loop for each deterministic module. Tests should describe the business rule, not mirror implementation details.

### Test layers

| Layer | Purpose | Minimum contents |
|---|---|---|
| Unit | Prove boundary logic | schemas, freshness, grid, labels, rolling features, purged splits, metrics, thresholds, replay classification |
| Contract | Freeze file and model interfaces | observation, prediction, manifest, evaluation, and resolved-outcome schema compatibility |
| Integration | Prove stage handoffs | GBFS → raw → Parquet; Parquet → grid → dataset; artifact → scorer; prediction → resolver → replay query |
| End-to-end | Protect the two user paths | live map opens and station is inspectable; replay time changes and outcome evidence agrees |
| Failure-mode | Ensure honest degradation | unavailable feed, truncated raw file, stale feed, incomplete horizon, old prediction, schema mismatch |

Coverage gate: at least 80% for deterministic core modules. UI line coverage is less useful than a few strong behavioral tests. No test should call the live feed.

### Required invariant tests

- A feature at `t` cannot read a row after `t`.
- A label at `t` is null if any required future row is missing, stale, or inactive.
- A threshold is selected without test labels.
- A replay class uses the threshold stored with that prediction, not today’s threshold.
- A backtest prediction is never labeled as a live forecast.
- A live prediction is metric-eligible only when `as_of_utc <= generated_at_utc < as_of_utc + 5 minutes`.
- Invalid outcomes never enter precision, recall, or lead-time denominators.
- Re-running compaction, scoring, or outcome resolution produces the same logical rows.

## 10. Modeling and data maturity protocol

Engineering can proceed on fixtures immediately; credible claims must wait for enough real chronological data.

| Collected history | Permitted use | Claim level |
|---|---|---|
| 0–7 days | Validate collection, schemas, grid, labels, UI, and replay mechanics | No model-quality claim |
| 2–3 weeks | First end-to-end baseline/model rehearsal and bug discovery | Preliminary only; expect unstable event counts |
| 4–6 weeks | Initial chronological comparison and calibration | MVP evaluation if both targets have enough events |
| 8–12 weeks | Stronger seasonal/time-of-week slices and narrower confidence intervals | Preferred follow-up evaluation |

The report must publish the exact date range, eligible-row count, event count, prevalence, and confidence interval. If either target has too few events for a meaningful test, release the working system but label that target’s evaluation preliminary.

### Fixed evaluation protocol

1. Reserve the newest contiguous block as the untouched test period.
2. Use earlier expanding or rolling chronological folds for training and validation.
3. Purge at least 30 minutes at every train→calibration, calibration→test, and development→final-holdout boundary so no label window crosses a later partition.
4. Fit imputers, encoders, model parameters, calibration, and thresholds inside their allowed training/validation partitions.
5. Produce out-of-fold probabilities for comparisons.
6. Open the test block once for the selected candidates and baselines.
7. Save the split boundaries and data fingerprint with every result.

### Promotion rule

Promote a target-specific model only if it:

- exceeds the strongest baseline in average precision;
- improves event recall or median first-warning lead time at an acceptable false-warning load;
- has usable calibration rather than only good ranking;
- behaves acceptably on peak/off-peak and high/low-volume slices;
- passes schema, freshness, reproducibility, and latency gates.

If it fails, serve the strongest baseline through the exact same product path. That is a valid system outcome.

## 11. Runtime schedule and materialized app inputs

### Scheduled jobs

```text
every 5 minutes
  1. collect and validate station status
  2. normalize the latest snapshot
  3. score if source/model contracts are valid
  4. finalize older prediction runs whose 30-minute windows have matured
  5. refresh the small latest/replay app artifacts atomically

hourly
  compact raw snapshots into an atomic Parquet partition

daily
  refresh station metadata
  produce the collection-quality report
  fully rebuild and verify the 24-hour replay artifact and operational summary
  back up raw snapshots, manifests, and immutable prediction runs

manual for MVP
  build dataset, train, calibrate, evaluate, and promote
```

Automatic retraining is deliberately excluded. Promotion is an explicit command that records the previous and new artifact versions.

### App inputs

The app reads only small materialized artifacts:

```text
latest_predictions.parquet
recent_station_history.parquet
replay_24h.parquet
replay_summary.json
evaluation_summary.json
model_manifest.json
collection_status.json
```

It does not scan raw history, build features, call GBFS, resolve outcomes, or load training data during a page render.

## 12. Delivery sequence

Collection is the first deployed vertical slice because elapsed history cannot be compressed later.

| Focused days | Main work | Observable checkpoint |
|---|---|---|
| 1–2 | M0 contracts, fixtures, settings, CI | Fixture can express every label and replay state |
| 3–6 | M1 collector, raw storage, compaction, quality | Unattended durable collection is running |
| 7–10 | M2 grid, labels, events, features | One command produces traceable training rows |
| 11–15 | M3 evaluation framework, baselines, models | Provisional chronological report runs end to end |
| 16–18 | M4 scorer and live app | Fresh, stale, empty-now, full-now, and risk states render correctly |
| 19–21 | M5 outcome resolution and replay | Fixture plus available live predictions replay correctly |
| 22–23 | M6 hardening, sample mode, documentation | Clean-environment release gate passes |

This is approximately 18–23 focused days, depending on deployment friction and UI polish. The elapsed calendar is longer because data collection runs in parallel.

### Two independent release gates

1. **Software gate:** the full pipeline works against deterministic fixtures and available collected data.
2. **Evidence gate:** enough real history exists for a chronological test with a useful number of events.

A practical target is a preliminary report after four weeks and a stronger first release after six to eight weeks. Do not fill the waiting period with extra features. Use it to audit collection gaps, inspect labels, and fix pipeline defects. After promoting a model, allow at least 24 completed hours of immutable live predictions before presenting operational replay metrics.

M4 and M5 can therefore be software-complete against the deterministic fixture before M3 passes its real-data promotion gate. The public live risk state remains disabled or explicitly provisional until a target-specific artifact is promoted.

### Critical path

```text
contracts
  → collector deployed
  → canonical grid
  → strict labels
  → chronological evaluator
  → target-specific promotion
  → live immutable predictions
  → delayed outcomes
  → replay
```

UI work may proceed from the fixture after its input artifacts are frozen. Model training may be rerun as history grows without changing the app contract.

## 13. Quality, performance, and security budgets

These are engineering gates, not model-performance claims.

| Area | Budget or gate |
|---|---|
| Collection | Seven-day successful-poll rate reported; target at least 98%; no silent empty or partial-network success |
| Freshness | Suppress forecast when its source observation or prediction exceeds the configured age limit |
| Scoring | Complete comfortably inside the five-minute interval; initial budget under 60 seconds |
| App | Warm initial render near three seconds; timestamp change should not scan raw history |
| Replay | At most one row per run/station/target; newest exposed run has a finalized outcome window |
| Determinism | Same data fingerprint, config, code, and seed reproduce saved probabilities within numeric tolerance |
| Coverage | At least 80% on validation, grid, labels, features, splits, metrics, scorer, and replay resolver |
| Secrets | No credentials in code, fixtures, logs, model metadata, or committed environment files |
| Container | Non-root process, pinned direct dependencies, vulnerability scan, read-only app inputs where practical |
| External data | Timeouts, bounded retries, schema validation, payload-size awareness, and safe logged errors |

## 14. Risk register

| Risk | Early signal | Containment | Decision if it persists |
|---|---|---|---|
| History is too short | Few positive events or unstable metrics | Start collection first; show counts and intervals; keep report preliminary | Wait for more data; do not invent a result |
| Collection gaps corrupt targets | Falling valid-label rate or gaps over 10 minutes | Strict null labels, quality report, durable scheduler, backups | Exclude affected windows and fix collection before tuning |
| Feed/schema drift | Parser failures or sudden station-count change | Preserve raw payloads, validate boundaries, alert on drift | Fail closed and add a versioned normalizer |
| Feed and station timestamps diverge | Fresh feed publications but old per-station reports | Keep receipt, feed publication, and station report age separate | Gate predictions on feed/receipt freshness and expose station age as a diagnostic |
| Model only learns inventory | LightGBM barely beats inventory baseline | Keep features small and compare on identical rows | Deploy the baseline for that target |
| Probabilities are misleading | Reliability curve deviates materially | Temporal calibration and Brier/log-loss gate | Show risk rank/band only until calibrated |
| Rare events distort accuracy | High accuracy with poor event recall | Use average precision, lift@20, event recall, and alert episodes | Never use accuracy as the headline metric |
| Replay leaks future knowledge | Old row changes after retraining or uses new threshold | Immutable runs; store threshold/model/source; explicit origin | Block replay release until traceability passes |
| Backtest looks live | Retrospective predictions lack an origin badge | Persist `prediction_origin`; separate filters and copy | Do not mix origins in one operational scorecard |
| Rebalancing confounds labels | Abrupt inventory jumps around events | Flag likely rebalancing and report as a limitation | Analyze sensitivity later; no causal claim |
| “Full” overstates return failure | Station+ accepts returns despite zero standard docks | Define target as standard-dock stockout everywhere | Keep limitation visible; defer Station+ modeling |
| Host loses data | History disappears after restart/redeploy | Persistent volume plus daily object backup and restore test | Deployment is not acceptable without recovery |
| App becomes slow | Raw Parquet scans or repeated joins on interaction | Materialize small app artifacts and cache read-only loads | Reduce replay payload before adding infrastructure |
| Scope expands | Work begins on routing, weather, or animation | Enforce the scope lock and one-in/one-out rule | Cut the new item or move it after M6 |

## 15. Scope cut order

If the schedule slips, preserve the truthful end-to-end path. Cut in this order:

1. Extra UI polish and nonessential filters.
2. Bootstrap confidence intervals; keep event counts and exact date ranges.
3. Logistic-regression display in the app; retain it in offline comparison.
4. Secondary QA slices beyond peak/off-peak.
5. Full-target deployment if it lacks enough valid events; keep its pipeline tested and label evaluation preliminary.

Do not cut immutable raw data, strict invalid labels, chronological evaluation, baseline comparison, stale-data handling, stored prediction versions, or replay traceability.

## 16. Documentation package

### README narrative

1. One-sentence problem and exact 30-minute targets.
2. Live and replay screenshots or a short recording.
3. One-command sample run.
4. Small architecture diagram.
5. Data source, five-minute collection, and attribution.
6. Chronological evaluation table with period, prevalence, baselines, and confidence.
7. How replay classifications are derived.
8. Reproducible commands and repository map.
9. Known limitations and explicitly deferred work.

### Supporting documents

- `docs/data-contract.md`: keys, timestamps, schemas, partitions, and freshness.
- `docs/labeling.md`: eligibility, future window, invalid reasons, and worked examples.
- `docs/model-card.md`: data range, features, splits, calibration, thresholds, results, slices, and limits.
- `docs/operations.md`: schedules, artifact lifecycle, alerts, backup, recovery, and model promotion.

Keep production logic out of notebooks. One exploratory notebook may show data checks, but every reported transformation and metric must also run from package code.

## 17. Final verification path

### Functional walkthrough

1. Open Live mode and confirm the data timestamp and freshness state.
2. Toggle empty/full risk and verify current boundary states override forecasts.
3. Select a high-risk station and inspect its last hour of inventory plus stored probability.
4. Open the top-20 table and confirm it matches the map artifact.
5. Switch to Replay and select the newest finalized timestamp.
6. Filter correct warnings, false warnings, and misses.
7. Open one station and verify the probability, stored threshold, observed path, event time, and class.
8. Open Evaluation and compare the deployed model with both baselines on the stated test period.

### Definition of done

The MVP is complete only when all of the following are true:

#### Data

- Five-minute collection is durable, monitored, and recoverable.
- Raw snapshots are immutable; processed partitions are idempotently rebuildable.
- Station metadata changes and receipt, feed, and station timestamps are represented explicitly.
- Quality and valid-label rates are reported for the evaluation period.

#### Modeling

- Empty and full targets pass hand-checked boundary and invalid-window tests.
- Evaluation is purged, chronological, and based on saved out-of-sample probabilities.
- Both targets are compared with the same inventory and trend baselines.
- Each deployed target uses a calibrated promoted model or an explicitly selected baseline.
- Every metric identifies target, data dates, event prevalence, model version, and threshold.

#### Application

- Live mode distinguishes current state, forecast risk, stale data, and unavailable prediction.
- Replay covers the latest finalized 24 hours and exposes invalid outcomes honestly.
- Every replay result traces to an immutable run, source snapshot, model version, and stored threshold.
- Evaluation is loaded from a versioned artifact and makes no unsupported claim.

#### Engineering

- CI passes format, lint, type, test, coverage, dependency, and container checks.
- Critical integration and end-to-end paths pass from a clean environment.
- The sample mode exercises live and replay without network access.
- Restart, stale-feed, corrupt-artifact, and missing-model behavior are tested.
- No secret, large raw dataset, or trained binary is committed.

#### Documentation

- Setup and sample run are reproducible from the README.
- Data contract, labels, model behavior, operational schedule, and limitations are documented.
- Screenshots and reported metrics were generated by the shipped pipeline.

## 18. First three work sessions

### Session 1 — freeze the contracts

- Create FND-01 through FND-04.
- Add the six-station/six-future-step fixture.
- Write contract tests first, then implement the minimum schemas needed to make them green.
- Record golden expected results for valid empty, valid full, no event, missing interval, stale interval, and already-at-boundary cases; activate each behavior test when its M2 slice starts.
- Freeze `as_of_utc`, prediction-run, station-prediction, and delayed-outcome schemas.

Exit: CI is green and the entire product behavior can be described using fixture rows.

### Session 2 — get collection running

- Implement COL-01 through COL-03 against captured fixtures.
- Run one manual live fetch and inspect the raw artifact and poll record.
- Deploy the five-minute job to durable storage as early as possible.

Exit: valid raw snapshots accumulate without depending on the app.

### Session 3 — make history trustworthy

- Implement COL-04 and DAT-01/DAT-02.
- Reprocess the first captured day twice and compare keys and hashes.
- Review freshness, gaps, station count, and schema warnings before proceeding.

Exit: the first daily quality report explains exactly what was collected and what cannot be used.
