# Guided modeling study using the frozen dataset

Status: executed offline on `2026-10-15`; deployment remains deferred. The commands and
artifacts described below are implemented for the frozen dataset.

## Outcome and working agreement

Find out whether a model predicts a non-empty station becoming empty within 30 minutes
better than a simple inventory rule, and whether geographic features add useful signal.
Deliver reproducible offline results, a saved candidate, and instructions for a later
deployment that you control.

We work through one stage at a time. I implement and run the offline tools for that stage,
explain their outputs, and stop at the review points in this document. You edit the choices
here or tell me what to change. The final test runs after you accept the model selection;
deployment requires a separate instruction from you.

## Recorded execution

Study `9c00dbb1f375405f` verified export `5d53d536cfc4304a`, trained the four fixed
candidates on the natural training split, and chose `hist_core` before test evaluation.
The default incremental validation rule selected it because its AP was 0.3227, above the
inventory baseline's 0.2475 and 0.0197 above `logistic_core`. Geographic candidates did
not meet the 0.01 incremental validation margin.

On the untouched test split, `hist_core` achieved AP 0.3559 versus 0.1884 for inventory.
At the validation-selected threshold of 0.1571, it issued 10,561 warnings (5.88% of test
rows) with 29.68% precision and 67.50% recall. The 1,000-draw, 60-minute temporal-block
bootstrap estimated an AP advantage over inventory of 0.1020 to 0.2286 (95% interval).
`hist_geo` had a higher descriptive test AP of 0.3675, but its geographic AP difference
interval crossed zero and it was not selected after validation.

The release package reproduces the first 100 saved test scores within `1e-12`. A replay
for station `54001024` at `2026-10-14T07:40:00Z` reproduced the exported features and
score from stored snapshots. Five validation examples were checked against their complete
stored observation windows: two missed stockouts, one false warning, one true warning,
and one true negative. Deployment is not part of this execution.

## Starting evidence

Use export `5d53d536cfc4304a` under `data/ml/exports/`, frozen at
`2026-10-15T07:29:30.349112+00:00`. Keep its files and split assignments unchanged.

| Partition | Rows | Positives | Use |
|---|---:|---:|---|
| Train | 336,840 | 9,167 | Fit preprocessing and candidate models |
| Validation | 140,274 | 5,734 | Compare candidates and choose the warning threshold |
| Test | 179,694 | 4,643 | Final evaluation after selection is recorded |
| Purged | 8,594 | 64 | Retain for provenance; exclude from all fitting and metrics |
| Enriched train | 91,670 | 9,167 | Development runs and an optional sampling experiment |

The source spans 5–15 October with major gaps. Large row counts include neighboring
stations and overlapping label windows; they are not independent observations. The
`weekend` feature is constant in the collected training rows, and all collected periods
are weekdays. The study cannot establish weekend or seasonal performance. Known test
counts are already documented; test predictions and model metrics must stay out of
model selection.

One correction to the earlier proposal: 2.95% is the prevalence across the complete
export. A constant prediction must be fitted from training alone: **2.72147%** for the
full training partition. The constant-score AP reference is the prevalence of the
partition being evaluated, calculated only when that partition is evaluated. See
[scikit-learn's AP definition](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.average_precision_score.html).

## Choices you can edit first — review A

These are proposed starting choices. Record changes before running comparisons.

| Choice | Proposed value | Your intervention |
|---|---|---|
| Initial use | Research ranking and offline warning experiments | Describe the decision you want the prediction to support |
| Main comparison | Average precision (AP) against the inventory ranking | Change the goal if missing an empty station matters more than false warnings |
| Equal alert budget | Highest-scoring 5% of eligible stations per observation time | Choose another fraction or a fixed station count |
| Warning threshold | Maximum validation F1; ties by precision, then higher threshold | Set a minimum precision or maximum warning rate instead, before selection |
| Meaningful AP gain | 0.01 absolute over the simpler alternative | Change this proposed practical margin before seeing results |
| Final fitting data | Full natural training partition | Request the optional enriched-training comparison below |
| Probability treatment | Report calibration diagnostics; describe scores as uncalibrated | Request the separate calibration design below if percentage risk is required |
| Compute budget | Four candidate fits, at most four CPU threads | Reduce the candidate set if laptop resources require it |
| External data | Stored snapshots and existing station metadata | Add road traffic, weather, or transit only in a later study |

A suggested instruction to start implementation is:

> Implement stages 1 and 2 of docs/plans/frozen-model-study.md, then stop for review A.

## Tools and commands

Use Python 3.12 and the locked environment: Polars for tables, scikit-learn for models
and metrics, NumPy for arrays and resampling, and joblib for saved pipelines. Use the
terminal for commands and the IDE to inspect Markdown reports and edit choices. Local
CSV tables make mistakes and subgroup results easy to inspect. No new hosted service is
needed for the study.

Available today:

```bash
make setup
make ml-dataset CUTOFF=2026-10-15T07:29:30.349112+00:00
make verify
```

The export already exists; regenerate it only if verification finds a missing or
changed artifact. `make ml-poc`, `make ml-report`, and `make ml-predict` belong to the
existing 14-day study and do not operate on this frozen export.

The following commands are implemented. Replace `STUDY_ID` with the identifier printed
by preparation; `EXPORT_ID` identifies the dataset, not the study.

| Proposed command | Output and stopping point |
|---|---|
| `make ml-frozen-prepare EXPORT_ID=5d53d536cfc4304a` | Verify inputs; write the protocol and data review |
| `make ml-frozen-study STUDY_ID=<id> MODE=smoke` | Check fit, score, save, and reload on the enriched training data; no quality claims |
| `make ml-frozen-study STUDY_ID=<id> MODE=full` | Fit the fixed candidates on natural training data and write validation results |
| `make ml-frozen-report STUDY_ID=<id>` | Read the current report without fitting or evaluating again |
| `make ml-frozen-lock STUDY_ID=<id> CANDIDATE=logistic_core` | Record your chosen candidate, threshold policy, and artifact hashes |
| `make ml-frozen-test STUDY_ID=<id>` | Evaluate the recorded selection and fixed comparisons on test |
| `make ml-frozen-replay STUDY_ID=<id> STATION_ID=48800374 AS_OF=<UTC-time>` | Reproduce an offline station prediction at your chosen historical time |
| `make ml-frozen-package STUDY_ID=<id>` | Build and verify a local release directory for your review |

Implement shared behavior under `src/bikepulse/ml/`, including a frozen-study runner
and evaluation helpers. Reuse the existing dataset feature definitions and threshold
helper where their contracts match. Extend focused ML tests. Keep the current 14-day
study interface available for its existing purpose.

## Stage 1 — Prepare a protocol and validate the input

Before fitting, verify every artifact hash against the export manifest, check unique
station-time keys, finite feature values, label types, split boundaries, and label-window
purges. Confirm the enriched view is a subset of training, includes every positive
training row, and has nine negatives per positive.

Write `protocol.json` with the export hashes, exact feature lists and order, candidate
parameters, training policy, selection margin, threshold rule, metric definitions, seed
42, dependency versions, and a source-code fingerprint. Derive the study ID from these
inputs. Changes to the protocol or code create a new study ID.

Write `data_review.md` showing train and validation coverage by hour, station, and
collection segment. Summarize constant features, missingness, rejected windows, and
training feature ranges. Use the already published test counts only for integrity
checks at this stage. Do not produce test scores or error examples.

The preparation result must say whether the data can support the planned comparisons.
If a partition lacks a class or a contract check fails, write a clear blocker report
and fix that issue before fitting.

## Stage 2 — Implement the baselines and four fixed candidates

Evaluate three simple references:

- Constant probability from natural training prevalence, for probabilistic metrics.
- Inventory score `1 / (available_bikes + 1)`, with a fixed `available_bikes <= 3`
  warning rule matching the existing study.
- Bike-ratio ranking `1 - available_bikes / capacity`, for an additional simple
  capacity-aware comparison.

Use these candidate fits; the geographic comparison must hold the model family and
parameters fixed.

| Candidate ID | Estimator | Features |
|---|---|---|
| `logistic_core` | StandardScaler + LogisticRegression | Existing eight features |
| `logistic_geo` | Same pipeline | Eight existing + six geographic features |
| `hist_core` | HistGradientBoostingClassifier | Existing eight features |
| `hist_geo` | Same tree estimator | Eight existing + six geographic features |

Logistic parameters: `C=1`, `max_iter=1000`, `random_state=42`, `class_weight=None`.
Fit scaling on training only and report convergence failures.

Tree parameters: `learning_rate=0.1`, `max_iter=100`, `max_leaf_nodes=15`,
`min_samples_leaf=100`, `l2_regularization=1.0`, `early_stopping=False`,
`random_state=42`, `class_weight=None`. Record all resolved parameters. Explicitly
disable automatic early stopping so it cannot introduce a random internal validation
split into this chronological experiment; this behavior is described in the
[estimator documentation](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingClassifier.html).

The six geographic fields are latitude, longitude, station count and capacity within
one kilometre, absolute nearby 15-minute bike-count movement, and the number of
neighbors contributing that movement. Station ID and snapshot ID remain identifiers,
not input features. Nearby movement can include rebalancing and is not road traffic.

Run the enriched view once to catch engineering problems. Do not use smoke-run metrics
to choose the winner. Then fit the four candidates on all 336,840 natural training
rows and compare them on identical natural validation rows. This is small enough to
attempt without discarding further negatives; record runtime and memory, and process
fits sequentially.

**Optional sampling comparison, chosen at review A:** add `logistic_core_enriched`,
a matched logistic fit on the enriched view. Compare it on natural validation data and label its raw
probabilities as affected by sampling. Record the retained-negative fraction of
25.18%. If this comparison is selected for serious use, decide its weighting or
calibration policy before fitting; do not treat its 10% training prevalence as reality.
The full-data fit remains the default candidate for the release.

Before the full run, finish review A: accept or edit the protocol and candidate budget.
After acceptance, a suitable instruction is:

> Run the smoke checks and full candidate study, then stop with the validation report.

## Stage 3 — Review validation evidence together — review B

Write a candidate comparison table with:

- AP, inventory AP, and the validation prevalence reference.
- Precision and recall among the top 5% at each observation time, plus realized counts.
- Precision, recall, F1, warning rate, and the proposed validation-selected threshold.
- Brier score and log loss for probability estimates, with a reliability table containing
  predicted risk, observed frequency, and support counts in each bin.
- Fit duration and prediction time, including the cost of geographic feature generation.

Use `ceil(0.05 * eligible_station_count)` per time for the top-budget metric. Resolve
equal scores by station ID and disclose this tie rule, especially for the inventory
baseline. Report pooled selected-row precision and recall, and variation between times.
Fixed warning thresholds and equal-budget comparisons answer different questions;
label both explicitly. Do not use accuracy as the primary measure.

Brier score combines several aspects of prediction quality; use the reliability
table to inspect calibration rather than interpreting Brier score alone as a
calibration test. See the
[calibration guide](https://scikit-learn.org/stable/modules/calibration.html).

Write `validation_errors.csv` with 10 examples each of true positives, false positives,
false negatives, and true negatives for the proposed selection. Include station/time,
bikes, docks, recent change, neighborhood observations, model score, and label. Use
stored snapshots to hand-check at least five examples, including missed stockouts and
false warnings. Pick examples across collection segments rather than adjacent copies.

Report slices by Paris local hour, starting bike count, station density, and activity
proxy. Define numeric slice boundaries from training only, and always show rows and
positive counts. Flag sparse slices; a single-class slice has no useful AP comparison.

Use matched core-versus-geographic results to answer whether geography helps. If the
gain is smaller than the recorded practical margin or too uncertain, retain the simpler
candidate. Do not add more geographic data after inspecting the test results.
Use the paired block procedure in stage 4 for validation differences as well, with
the same cautions about the small number of observed periods.

**Your intervention:** inspect errors and decide whether the practical tradeoff is
useful. Accept the candidate and threshold, change the warning objective and rerun
validation, or stop with the baseline. Any revised selection rules must be recorded
before the final test, with the number of validation comparisons disclosed.

Suggested instruction:

> Review the validation errors with me. Explain why the proposed winner helps and
> what I lose by choosing the simpler model. Wait for my model and threshold choice.

### If calibrated percentage risk is required

At review A, reserve a chronological calibration interval within the original training
period. Fit the model on earlier rows, purging labels that reach calibration. Fit a
sigmoid calibrator on the reserved natural rows; assess the pair on original validation.
Record exact times, require both classes in each interval, and use the same intervals
for matched comparisons. This changes the protocol and training counts.

The calibration rows must be excluded from fitting the base model; the
[scikit-learn calibration guide](https://scikit-learn.org/stable/modules/calibration.html)
explains why. If support is inadequate, deliver ranking scores. Keep validation and
test excluded from fitting both the model and calibrator.

## Stage 4 — Record the choice, then evaluate test — review C

After your selection, write `selection.json`: candidate ID, fitted pipeline hash,
threshold value and rule, feature schema, protocol hash, and your decision notes.
Freeze any calibrator with that pipeline. Do not refit on train plus validation before
this test; that would change the evaluated model and its threshold behavior.

Run the test command only after you explicitly ask for the final evaluation. The
command requires a recorded selection. Compute the selected candidate and the baseline
metrics once; also report the already declared core/geographic pairs as fixed
comparisons. Test outcomes cannot change the selected candidate or threshold.

Use paired temporal block resampling for uncertainty: 60-minute blocks within
contiguous collection segments, all stations in a sampled time block kept together,
1,000 draws with seed 42. Never bridge collection gaps or resample individual rows as
independent examples. Apply the same draws to baseline/model and core/geographic
differences. Record the number of blocks, valid draws, and sensitivity to block length.
If fewer than 10 usable blocks or too many single-class draws remain, show descriptive
segment results and state that a reliable interval could not be estimated. Even a
computed interval describes these observed periods, not generalization across seasons.

Write `test_predictions.parquet`, `test_metrics.json`, `subgroups.csv`, and `report.md`.
A repeat of the same test command should read and verify those artifacts; changing
the candidate after test requires a new study and later observations for an independent
assessment.

**Your intervention:** accept a research candidate, retain the inventory baseline, or
request more collection. There is no requirement to produce a winning learned model.
Test failure should be reported plainly and should not launch another tuning cycle.

Suggested instruction:

> Record my selected candidate and threshold, run the final test, and explain whether
> the evidence is strong enough for an offline demo. Stop before deployment.

## Stage 5 — Prepare a local release for your review

Serialize the exact tested pipeline, including preprocessing, feature order, and any
calibrator. Package the selection, dataset and code fingerprints, locked dependency
versions, target definition, coverage limitations, and model report. Keep generated
artifacts under ignored `data/ml/frozen_studies/<study_id>/`.

The package and replay tools must verify artifact checksums and schema before loading.
Loading should reproduce saved predictions within a recorded numeric tolerance.
Archive one release directory rather than silently redirecting an existing prediction
command to a new model.

Implement an offline replay using an explicit historical observation time with a
usable exact 15-minute lag. Generate the same features from the stored history and
compare with the export. For geographic candidates, load the full contemporaneous
station set before calculating neighborhoods. A single-station row cannot provide
those features. Return explicit missing, stale, inactive, or already-empty states.

The collector is stopped, so replay must display its historical timestamp. A future
live forecast needs a fresh network snapshot, valid metadata, and a matching usable
snapshot 15 minutes earlier. The existing map's current feed fetch alone does not
provide that history.

Finish the offline implementation with tests for training-only preprocessing,
exclusion of purged/test rows from selection, candidate/threshold recording, exact
feature parity in replay, deterministic sampling, artifact validation, and missing
input states. Run `make verify`, including the browser check if app code changes.

**Your intervention:** choose a few stations and times for the replay, inspect the
reported state and mistakes, and accept or reject the release package.

## Deployment guide — future work with your interventions

This section is a handoff to prepare with you. Execute it only after a later request
to deploy and after the release review above. Hosting commands depend on the destination
you choose; do not invent a provider configuration in advance.

| Step | What you do | Tools and evidence we prepare together |
|---|---|---|
| 1. Choose the experience | Select historical demo, local live forecast, or hosted live forecast | IDE review of the model report, a screen sketch, and a destination-specific checklist |
| 2. Accept the claim | Approve ranking or warning language, historical timestamps, and any probability wording | Release report, calibration evidence, agreed UI text |
| 3. Choose runtime inputs | For live forecasts, decide where continuous network snapshots and metadata will come from | Feature-parity replay, freshness rules, collector configuration, history storage design |
| 4. Review the app change locally | Inspect a preview using a selected release and controlled snapshots | Future Streamlit integration, `make run`, local browser, existing Playwright tools |
| 5. Choose host and access | Select a destination, budget, storage, and who can view it | Host documentation, dependency and storage requirements, secret configuration you control |
| 6. Review launch and rollback | Approve exact deployment commands and a way to restore the previous release | Versioned release directory, checksum checks, feature flag, previous model or inventory fallback |
| 7. Launch yourself or authorize it | Run the reviewed commands or explicitly request execution | Destination-specific CLI; verify timestamps, unavailable states, and logs after launch |
| 8. Review operation | Decide what to do if data gaps or prediction drift appear | Coverage, feature-range, score-distribution, latency, and error reports; later labels when available |

Starting a collector, enabling live forecasts, uploading a model, configuring a host,
or publishing the app belongs to that later launch request. An offline package and a
historical replay can be completed during the modeling work without any of these actions.

## Progress and decision record

Check boxes only when the corresponding work and review are complete.

- [x] Stage 1: input verification and protocol prepared.
- [x] Stage 2 / review A: default choices accepted and implementation checked.
- [x] Stage 3 / review B: validation table and representative mistakes reviewed.
- [x] Stage 4 / review C: `hist_core` recorded and final test completed.
- [x] Stage 5: release package and historical replay completed.
- [ ] Separate deployment request received; destination and launch commands reviewed.

| Review | Your decision | Evidence or file | Date |
|---|---|---|---|
| A: objective, candidates, sampling, calibration | Default plan values | `protocol.json`, `data_review.md` | 2026-10-15 |
| B: model and threshold | `hist_core`, threshold 0.1571 | `validation_report.md`, `validation_errors.csv` | 2026-10-15 |
| C: final assessment | Offline candidate retained | `selection.json`, `test_report.md` | 2026-10-15 |
| Release: replay and package | Package and replay verified | `release/`, replay output | 2026-10-15 |
| Deployment | Deferred | Destination-specific runbook | |
