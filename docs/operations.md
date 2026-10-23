# Operations

## Collector lifecycle

The experiment uses a manually started systemd user service. It is intentionally
independent of the Streamlit process.

Collection was frozen at `2026-10-15T07:29:30.349112+00:00`; the service was confirmed
inactive during the [23 October project audit](audits/2026-10-23-project.md).
Start it only for an intentional new collection window.

```bash
make collect-start
make collect-status
journalctl --user -u bikepulse-collector.service -n 50
make collect-stop
```

The service is not enabled at boot. After a reboot, run `make collect-start` once.
Laptop sleep, shutdown, network loss, or a stopped user session produces a visible
collection gap; the pipeline does not invent observations to fill it.

## Checks while an experiment is running

These commands leave the collector running:

```bash
make collect-status
journalctl --user -u bikepulse-collector.service -n 50
make ml-readiness HOURS=24
make ml-readiness HOURS=48
make check
make verify
```

`collect-status` reports recent service state and collection quality. Readiness
commands rebuild derived partitions atomically and report whether both label
classes have complete windows. They do not train a model or call the public feed.

Avoid changing `config.example.toml`, replacing `.venv`, or invoking the stop/start
lifecycle during an uninterrupted collection window. Prepare and verify code and
documentation changes first; schedule any service restart separately.

## Automatic reports

At startup the collector backfills readiness for the two preceding completed UTC
days. While running, it writes one report per completed day and retries failed
report dates. Outputs are stored under:

```text
data/ml/readiness/daily/YYYY-MM-DD.{json,md}
data/ml/readiness/rolling/{24h,48h}.{json,md}
data/ml/readiness/latest.json
```

`labels_forming` means at least one complete positive and negative label exists in
the report window. It is a pipeline-readiness result, not evidence of model quality.

## Fourteen-day study

`END` is exclusive and must be exactly 14 UTC days after `START`. It must also be a
completed date.

```bash
make ml-poc START=YYYY-MM-DD END=YYYY-MM-DD
make ml-report
make ml-predict STATION_ID=...
```

The study uses eight training days, three validation days, and three test days with
30-minute purges at split boundaries. Every partition needs both target classes.
The earliest preliminary window using the current October collection is:

```bash
make ml-poc START=2026-10-12 END=2026-10-26
```

Run it after `2026-10-26 00:00 UTC`. Retain coverage gaps and readiness warnings in
the interpretation of the result.

## Frozen partial dataset

After stopping collection, export all complete labels through the last stored status receipt:

```bash
make ml-dataset
```

To reproduce a previous export, pass its exact UTC receipt cutoff:

```bash
make ml-dataset CUTOFF=2026-10-15T07:29:30.349112+00:00
```

The command writes a full dataset at observed prevalence, a training-only 1:9 enriched
sample, and provenance and quality reports under `data/ml/exports/<run_id>/`. Missing
collection intervals remain gaps. Validation and test data are never resampled.

## Frozen model study

Prepare a reproducible offline study from a verified frozen export, then run its selected
stages. The commands never fetch the public feed.

```bash
make ml-frozen-prepare EXPORT_ID=5d53d536cfc4304a
make ml-frozen-study STUDY_ID=9c00dbb1f375405f MODE=smoke
make ml-frozen-study STUDY_ID=9c00dbb1f375405f MODE=full
make ml-frozen-report STUDY_ID=9c00dbb1f375405f
make ml-frozen-lock STUDY_ID=9c00dbb1f375405f CANDIDATE=hist_core
make ml-frozen-test STUDY_ID=9c00dbb1f375405f
make ml-frozen-replay STUDY_ID=9c00dbb1f375405f STATION_ID=54001024 AS_OF=2026-10-14T07:40:00Z
make ml-frozen-package STUDY_ID=9c00dbb1f375405f
```

Artifacts live under `data/ml/frozen_studies/<study_id>/`. `frozen-test` requires a
recorded selection and uses the saved validation threshold. It evaluates no deployment
target. The release package contains the selected model, reports, checksums, and a
prediction-reproduction check.

These commands describe the original execution sequence. The final test has already
been evaluated; use `ml-frozen-report` to inspect its results. New managed studies move
forward through `prepared`, validation complete, `locked`, test complete, and `packaged`.
Smoke fitting is optional before locking. Each completed stage has an immutable manifest
under `stages/` that binds its request, prerequisites, and output checksums.

Repeating the exact request verifies those manifests and returns the existing result
without fitting, scoring, changing state, or rewriting files. A conflicting selection,
changed evidence, or out-of-order command fails. Once locked, candidates cannot be fitted
again or changed. Final-test execution records `stages/test_attempt.json` before scoring;
if the process is interrupted, automatic rescoring remains blocked so an operator can
inspect and preserve the incomplete attempt. A per-study filesystem lock rejects
concurrent mutations.

Studies created before stage manifests are legacy evidence. Their reports and historical
replay remain readable, but mutation commands fail because the lifecycle prerequisites
cannot be verified. Do not retrofit stage manifests or regenerate their model, selection,
test, or package artifacts.

## Temporal research cache

New temporal-study runs store feature caches under `data/ml/temporal_cache/v2/`. A cache
identity covers the frozen export manifest, every consumed development-history partition,
station metadata, transformation settings and source, Python, and relevant installed
dependencies. The adjacent manifest records the complete provenance, Parquet checksum,
schema, row count, and time bounds.

Reuse requires the manifest and Parquet file to agree and the data to have the expected
columns and dtypes, unique station-time keys, and development-only bounds. Missing,
obsolete, incomplete, or corrupt cache pairs are rebuilt atomically with the reason
recorded in the new manifest and study report. Existing pre-v2 caches and recorded studies
are retained as historical artifacts.

## Session housekeeping

Inspect `git status --short` and the latest project audit before editing. Existing
untracked research code, tests, experiment configuration, reports, and published plots
are work to preserve. Use `make clean` to remove disposable tool/build artifacts;
it keeps `.venv/`, `.tools/`, and all of `data/`.

Dependency advisories can change between sessions. The findings in the 23 October project
audit were remediated later that day; the dated remediation record documents the resolved
versions and verification. Continue to require the live `pip-audit` in `make verify`.

## Recovery

1. Inspect the service and recent logs.
2. Confirm the configured data directory still contains raw snapshots and polls.
3. Restart only when the process is inactive or intentionally scheduled for an update.
4. Run `make collect-status` after recovery and record unexplained gaps in a dated audit.

Raw snapshots are immutable, so derived observation partitions and reports can be
rebuilt after code changes. Never delete raw feeds, poll receipts, or generated
experiment evidence during an active run.

## Troubleshooting

- `externally-managed-environment`: use `make setup`; do not install into system Python.
- Live map unavailable: check both feed URLs and network access, then use **Try again**.
- Grey map markers: inspect the station timestamp and `max_station_age_seconds`.
- Missing map tiles: station data may still be valid; base tiles need browser network access.
- Collection will not start: inspect `systemctl --user status bikepulse-collector.service`.
- Insufficient ML data: inspect the 48-hour readiness report for current, lag, future, class, and gap rejections.
