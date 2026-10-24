# Operations

## Setup and launch

Use Python 3.12. Run `make setup` to synchronize the local locked environment, then `make run`.
Do not install application packages into system Python. Only the Streamlit process is needed.

## Streamlit Community Cloud

Set the app's main file to `streamlit_app.py` and select Python 3.12. Community Cloud reads
`requirements.txt`, which installs the local package and its runtime dependencies. The
repository's `.streamlit/config.toml` enables headless mode, disables usage statistics and
selects the light theme. No Streamlit secrets are required by the default configuration.

The app reads `config.example.toml` from the repository root by default. To use a deployment-
specific TOML file, commit that file and set `BIKEPULSE_CONFIG` under the app's Advanced
settings to its repository-relative path. Do not put credentials in TOML or commit secrets.

The app defaults to `config.example.toml`; `BIKEPULSE_CONFIG` selects another TOML file.
Configuration sections are:

- `[gbfs]`: station status/information URLs, user agent, HTTP timeout, retry count and backoff.
- `[live]`: `station_warning_age_seconds` defaults to 3600, the hard
  `max_station_age_seconds` cutoff to 7200, and status-feed freshness to 300 seconds.
- `[movement]`: controls the bounded 20-minute observation window, 21-report hard limit,
  minimum usable span, recent-sample age, rebalancing thresholds and stable-rate threshold.
- `[weather]`: `enabled` defaults to true and `forecast_url` defaults to Open-Meteo.

Unknown settings are rejected. Custom configurations must follow the example schema.
No data directory or background service is required. Recent movement observations exist only
inside the process and are discarded on restart.

## Refresh and failures

The open page refreshes each minute. **Refresh data** bypasses the status cache; station
information retains its 24-hour cache and weather its 15-minute cache. Restart Streamlit to
reload changed settings or clear shared in-memory resources, including recent observations.

If startup cannot load station feeds, the page shows an error and **Try again**. Later feed
failures retain a visibly labeled last-known snapshot, disable normal availability colors
and withhold predictions. Individual station reports are labeled delayed after one hour,
remain eligible for two hours, then become stale. The overall status feed must still be no
more than five minutes old.

Weather requests time out after three seconds. Failures wait 60 seconds before retrying and
leave the station estimate available with a short fallback message. Set `enabled = false` to
run without weather; this has the same neutral effect as a temporary weather failure.

## Verification

`make verify` runs Ruff, strict mypy, pytest with an 80% coverage gate, the dependency audit,
source/wheel builds, Chromium installation and Playwright smoke tests. `make check` omits
the browser stage. Tests use local HTTP fixtures and never need public GBFS or weather APIs.
Local sockets, browser process permissions and package/vulnerability-service access are
needed for the complete gate; report unavailable checks instead of weakening them.

`make clean` removes disposable tool caches, bytecode, coverage and build output while
preserving the locked virtual environment and local tooling.

UI screenshots under `docs/assets/` use controlled synthetic station and weather fixtures.
