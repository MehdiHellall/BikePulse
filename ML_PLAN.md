# BikePulse: Simple ML POC

## Outcome

Predict whether a station with available bikes will be observed empty within the next
30 minutes. Deliver an offline collection/history pipeline, inventory baseline,
logistic-regression model, preliminary report, and station prediction command.
Collect 14 calendar days on the Linux laptop. Review budget: six hours per week.

Keep map forecast integration, dock prediction, LightGBM, calibration, replay,
weather, and automatic retraining for later work. MVP_PLAN.md remains the roadmap.

## Data and target

The user service polls every five minutes, 30 seconds before the UTC boundary.
It refreshes station information at startup and daily. It preserves raw JSON.gz,
receipt timestamps, station report timestamps, and failure records. Laptop sleep,
shutdown, or loss of connectivity creates explicit gaps.

At each canonical boundary, use the latest whole snapshot already received, at
most five minutes earlier. Metadata joins use the version known at receipt time.
Raw history without receipt timestamps is retained for diagnostics but excluded
from modeling. Hourly bronze/silver Parquet is rebuildable.

A starting station must have bikes, positive capacity, enabled installation/rental/
return flags, and station age at most 300 seconds. Every future observation at
+5 through +30 minutes must be usable. Label 1 when at least one is empty, otherwise
0. Any unusable future makes the label null, even after an observed zero.
Already-empty stations are excluded. Missing counts never mean zero.

Inspect freshness during the first 48 hours with make collect-status. If fresh
windows remain unavailable, investigate the provider and report insufficient data.
Never loosen freshness to manufacture training examples.

## Features and model

Eight features: bikes, docks, capacity, bike/capacity ratio, bike-count change
over exactly 15 minutes, Paris hour sine/cosine, and weekend indicator. Require
a usable exact t-15 observation. No future interpolation or missing-value imputation.

Compare inventory score 1/(bikes+1) with standardized logistic regression:
C=1, seed 42, max_iter=1000, no class balancing or parameter search.
To bound laptop memory, fit at most one million uniformly sampled training rows;
record the actual number. Evaluate all eligible test rows in batches.

Use first eight UTC days for training, next three for threshold selection, last
three for testing. END is exclusive and must equal START + 14 days. Purge earlier
rows whose 30-minute label windows reach the next block. Fit scaling on training
only. Select the model threshold by validation F1, ties by precision then threshold.
The inventory warning cutoff is fixed at bikes <= 3.

## Delivery tasks

1. S1: receipt timestamps, collection service, Parquet, freshness/gap reports.
   Manually inspect three stations and check restart/sleep behavior.
2. S2: canonical observations, labels, features, purged splits. Hand-check five
   positive/negative/invalid windows and test future-data isolation.
3. S3: train, compare, serialize, reload, and write the report. Review mistakes;
   do not tune against the final test.
4. S4: commands, docs, and release checks. Reproduce from the locked environment.

Run tasks in order while collection proceeds. Engineering tests use synthetic
fixtures; they are not model-quality evidence or a user-facing demo dataset.

## Commands and artifacts

Run make setup, make collect-start, and make collect-status. Stop with
make collect-stop. Restart collection manually after reboot; no boot service
is enabled. After 14 days:

    make ml-poc START=YYYY-MM-DD END=YYYY-MM-DD
    make ml-report
    make ml-predict STATION_ID=48800374
    make verify

Artifacts under ignored data/ml include daily datasets, split/model manifest,
dataset and source fingerprints, model.joblib, test_predictions.parquet,
report.json, and report.md. Repeated inputs reproduce the same experiment.
Latest prediction reads collected history only; it never fetches public feeds.

The report states AP versus the baseline, precision, recall, prevalence, dates,
usable/rejected counts, collection gaps, and limitations. Alert thresholds differ,
so precision/recall are not comparisons at equal alert load.
Probabilities are uncalibrated and research only.

## Completion and tests

Test receipt boundaries, stale/future timestamps, missing futures after zero,
exact lags, station isolation, Paris DST, split purging, deterministic training,
artifact reload/checksum, and missing/stale/inactive/already-empty prediction states.
Compaction tests are active; retain the 80% coverage gate and existing map tests.

Software completion means the pipeline and commands pass verification.
Real-data completion requires a reproducible preliminary evaluation. Insufficient
or single-class data writes an explicit blocker report without inventing a model.

