# BikePulse

BikePulse is a local Streamlit proof of concept for exploring current Vélib’ station availability across Greater Paris. It loads the official GBFS feeds, displays every valid station on a PyDeck map, and supports station search, bike/dock modes, and station details.

The live-map scope is in `POC_PLAN.md`. The offline empty-bike ML study is in
`ML_PLAN.md`; live forecast integration and replay remain on the roadmap in `MVP_PLAN.md`.

## Setup

Install Python 3.12 with virtual-environment support, then use these commands:

```bash
make setup
make run
```

`make setup` installs `uv` into the repository-local `.tools/uv` environment and
creates `.venv` from the committed lockfile. It does not modify the system Python.
Open the local URL printed by Streamlit.

## Run the station map

The app reads `config.example.toml` by default. To use another configuration:

```bash
BIKEPULSE_CONFIG=/path/to/config.toml make run
```

The status feed is cached for 60 seconds and station information for 24 hours. The app refreshes while it is open. A failed refresh preserves the last successful snapshot, labels it clearly, and disables normal availability colors.

## Verify the prototype

```bash
make verify
```

This runs formatting, lint, type checks, unit and app tests, the dependency audit,
the package build, and the Playwright browser smoke test. For a faster check that
does not install Chromium, run:

```bash
make check
```

Tests use controlled local feeds and never call the public GBFS service.

## Offline empty-bike ML POC

Start real collection independently of the map:

```bash
make setup
make collect-start
make collect-status
```

The manually started Linux user service polls every five minutes. It saves raw
responses, receipt times, failures, hourly Parquet, and quality reports under
`data/`. It pauses while the laptop sleeps or shuts down. After reboot, run
`make collect-start` again. Stop with `make collect-stop`. Read logs with
`journalctl --user -u bikepulse-collector.service -n 50`.

Inspect the first 48 hours of freshness before expecting usable training data.
The provider previously returned old station timestamps; fresh downloads do not
make those stations fresh. The 300-second station-age rule remains enforced.

After collecting 14 days, choose a UTC date range where END is exclusive and
exactly 14 days after START. For collection starting 5 October 2026:

```bash
make ml-poc START=2026-10-05 END=2026-10-19
make ml-report
make ml-predict STATION_ID=48800374
```

The study builds eight features, trains standardized logistic regression, and
compares it with `1/(bikes+1)` on the same test rows. The split is eight training
days, three validation days, and three test days, with 30-minute boundary purging.
Model thresholds use validation F1; the inventory cutoff is fixed at three bikes.
No test labels are used for fitting or threshold selection.

Reports, manifests, test predictions, and the model are in
`data/ml/experiments/<run_id>/`. Dataset/source fingerprints identify the experiment.
Fitting is capped at one million reproducibly sampled training rows for laptop
memory; evaluation uses all eligible test rows. Results are **preliminary,
research only, and uncalibrated**. Missing or single-class data yields an
`insufficient_data` report and no model. Predictions use collected observations
only and report unavailable for missing, stale, inactive, or insufficient-history
inputs. Already-empty stations return **Empty now**.

`make verify` exercises the full raw-to-model path on synthetic test fixtures,
including serialization and prediction. These fixtures are not real-data evidence.

## Troubleshooting

- **`externally-managed-environment`:** do not use `--break-system-packages`; run `make setup`, which installs tools inside local virtual environments.
- **The map reports that live data is unavailable:** verify internet access and both URLs in the selected TOML file, then use **Try again**.
- **All points are grey:** check the feed timestamp. Data older than `max_station_age_seconds` is intentionally marked stale.
- **Map tiles do not load:** station data can still be valid; the CARTO base map requires network access in the browser.
- **Collection will not start:** a Linux user systemd session is required; inspect `systemctl --user status bikepulse-collector.service`.
- **ML reports insufficient data:** inspect `make collect-status`; all six future observations and the exact 15-minute lag must be usable. Collect longer if a partition lacks positive or negative examples.
