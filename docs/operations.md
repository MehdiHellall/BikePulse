# Operations

## Collector lifecycle

The experiment uses a manually started systemd user service. It is intentionally
independent of the Streamlit process.

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
