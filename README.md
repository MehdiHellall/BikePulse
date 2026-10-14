# BikePulse

BikePulse is a Python 3.12 proof of concept for exploring current Vélib’ station
availability and evaluating whether a station with bikes will be observed empty
within the next 30 minutes.

The repository currently contains two working paths:

- a local Streamlit map backed by the official GBFS feeds;
- an offline collector and reproducible logistic-regression study backed by local history.

Live forecast display and replay are planned work. See the [documentation index](docs/README.md)
for the implemented contracts, operating guide, audits, and plans.

## Quick start

Install Python 3.12 with virtual-environment support, then run:

```bash
make setup
make run
```

`make setup` installs `uv` under `.tools/` and creates `.venv` from `uv.lock`. It
does not modify the system Python. The app reads `config.example.toml` by default;
select another file with `BIKEPULSE_CONFIG=/path/to/config.toml make run`.

The map displays all valid geocoded stations, supports bike and dock modes, and
provides station search and details. Failed refreshes retain the last successful
snapshot, label it clearly, and suppress normal availability colors.

## Project layout

```text
src/bikepulse/
  app/        Streamlit map and view model
  collector/  GBFS client, validation, storage, and user service
  config/     typed TOML settings
  data/       raw-to-Parquet compaction and quality reports
  ml/         labels, features, readiness, study, and prediction
tests/        controlled fixtures, unit/integration tests, and browser smoke test
docs/         architecture, contracts, operations, plans, and dated audits
data/         ignored runtime observations and generated ML artifacts
```

The detailed layout and data flow are in [Architecture](docs/architecture.md).

## Running collection experiment

The collector runs independently of the map as a manually started Linux user
service and polls every five minutes:

```bash
make collect-start
make collect-status
journalctl --user -u bikepulse-collector.service -n 50
```

It writes immutable raw responses, poll receipts, rebuildable Parquet partitions,
quality reports, and daily label-readiness reports under ignored `data/`. Laptop
sleep, shutdown, and connectivity loss create explicit gaps. After a reboot, start
the service manually.

The current experiment is active. Status checks, log reads, tests, documentation
edits, and readiness reports can run without stopping it. The full operating and
recovery procedure is in [Operations](docs/operations.md).

## Label readiness and model study

The ML path treats a station count as observed when a recently updated feed is
received. Feed update time and local receipt must be no more than 300 seconds old
at the canonical boundary. The per-station `last_reported` timestamp remains a
diagnostic and continues to drive freshness on the live map.

Check whether complete positive and negative label windows are forming:

```bash
make ml-readiness HOURS=24
make ml-readiness HOURS=48
```

The earliest preliminary 14-day window reusing the 12–13 October observations
ends on 26 October 2026. Run it after `2026-10-26 00:00 UTC`, provided each
chronological partition contains both label classes:

```bash
make ml-poc START=2026-10-12 END=2026-10-26
make ml-report
make ml-predict STATION_ID=48800374
```

Results are preliminary, research only, and uncalibrated. The complete target,
split, feature, and claim rules are in the [ML POC plan](docs/plans/ml-poc.md) and
[data contract](docs/data-contract.md).

## Verification

```bash
make verify
```

This runs formatting, lint, strict type checks, tests with coverage, the dependency
audit, package build, and Playwright smoke test. Use `make check` when Chromium is
unavailable. Tests use controlled local fixtures and never call the public feed.

## Data guarantees

- Missing bike or dock counts remain unknown and never become zero.
- Raw snapshots are immutable; derived Parquet partitions are rebuildable.
- Receipt, feed publication, and station report times remain separate.
- Collection gaps invalidate affected label windows instead of creating negatives.
- Synthetic tests establish engineering behavior, not real model quality.

The [13 October collection audit](docs/audits/2026-10-13-collection.md) records the
observed gaps, timestamp diagnosis, hand-checked labels, and first readiness result.
