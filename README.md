# BikePulse

BikePulse displays live Vélib’ station availability across Greater Paris and an
explainable, rule-based outlook for the next 30 minutes.

Search for a station or select it on the map. Switch between available bikes and
open docks. Station details show current inventory and a Low, Medium or High risk
of running out, based on inventory, configured busy areas, local time and Paris-area
weather when available. These estimates are **not validated** and do not represent
probabilities.

## Run locally

Use Python 3.12, then run:

```bash
make setup
make run
```

Setup installs local tooling under `.tools/` and synchronizes `.venv` from `uv.lock`.
The default configuration is `config.example.toml`. Choose another configuration with
`BIKEPULSE_CONFIG=/path/to/config.toml make run`.

The map refreshes every minute while open. Station information is cached for 24 hours,
status for 60 seconds and weather for 15 minutes, all in memory. A failed status refresh
shows clearly marked last-known availability and withholds the outlook. A weather failure
does not withhold the station estimate. The app does not persist feed, weather or prediction
history.

Individual station reports are labeled delayed after one hour but remain usable for up to
two hours. Older reports are marked stale and do not receive a prediction.

## Rules and limitations

Current inventory dominates the outlook: 1–3 bikes or docks always gives High risk.
Busy areas and peak periods increase assumed turnover pressure. Poor riding weather may
reduce the score by one point. Both modes use the same turnover assumptions. Departures,
returns and rebalancing can produce different outcomes.

[Prediction rules](docs/prediction-rules.md) documents every threshold, geographic zone,
time boundary and fallback. Missing availability stays unknown. Already empty or full
stations display their current state rather than a future-risk category.

Weather comes from [Open-Meteo](https://open-meteo.com/en/docs), with CC BY 4.0 attribution.
One forecast at the Paris map center is shared across the region and is never required for
an estimate.

## Development

```bash
make verify
```

Verification runs Ruff formatting/lint, strict mypy, tests with at least 80% coverage,
a dependency audit, package builds and Playwright browser smoke tests. Tests use controlled
local feed fixtures. `make clean` removes disposable caches and build output,
preserving `.venv/`, `.tools/` and the source tree.

See the [documentation index](docs/README.md) for architecture, feed contracts and operations.
