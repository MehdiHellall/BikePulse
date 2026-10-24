# BikePulse

BikePulse displays live Vélib’ station availability across Greater Paris and an
explainable, rule-based outlook for the next 30 minutes.

Search for a station or select it on the map. Switch between available bikes and
open docks. Station details show current inventory and a Low, Medium or High risk
of running out, based on inventory, recent live movement, nearby station pressure,
configured busy areas, local time and Paris-area weather when available. These estimates
are **not validated** and do not represent probabilities.

When the selected station may run out or fill up, BikePulse offers the closest safer
station within two kilometres as a Plan B. The map connects the two stations and the
recommendation shows straight-line distance, current inventory and its own outlook.

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
history. It retains at most 21 distinct reports per station from the last 20 minutes in
memory, solely to measure recent movement; the window is discarded on restart.

Individual station reports are labeled delayed after one hour but remain usable for up to
two hours. Older reports are marked stale and do not receive a prediction.

## Deploy on Streamlit Community Cloud

Create a Community Cloud app from this repository with `streamlit_app.py` as the main
file and Python 3.12 as the runtime. The checked-in `requirements.txt` installs the
project from its `src/` layout; no secrets or database are required. The default
configuration uses the public Vélib GBFS feeds and Open-Meteo. If you need a different
configuration, add a TOML file to the repository and set the `BIKEPULSE_CONFIG` environment
variable in the app's Advanced settings to its repository-relative path.

## Rules and limitations

Critically low current inventory dominates the outlook: 1–3 bikes or docks always gives
High risk. A robust recent trend is the strongest contextual adjustment and is calculated
independently for bikes and docks. Concentrated pressure at nearby stations, busy areas and
peak periods can increase risk; poor riding weather may reduce it. Suspected rebalancing
jumps are excluded. If recent movement cannot be trusted, the existing live context still
produces an outlook without a separate warm-up state.

Plan B requires at least four bikes or docks, a usable outlook that is safer than the
selected station, and a straight-line distance of no more than two kilometres. It is a
nearby alternative, not a walking or cycling route.

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
