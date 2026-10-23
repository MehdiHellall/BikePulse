# Architecture

BikePulse is a Python 3.12 Streamlit app with a PyDeck station map.

```text
src/bikepulse/
  app/         Live snapshot service, availability presentation and Streamlit page
  config/      Typed TOML settings
  feeds/       GBFS HTTP client and record validation
  prediction/  Pure outlook rules, geographic catalog and regional weather service
tests/         Unit/integration fixtures and browser checks
docs/          Contracts, rules, operations and UI screenshots
```

## Request flow

1. Load typed settings from the TOML selected by `BIKEPULSE_CONFIG`.
2. Fetch official station information and status through the GBFS client. Normalize each
   record, isolate invalid records and join valid stations by ID.
3. Keep the latest successful snapshot in memory. Cache status for 60 seconds and station
   information for 24 hours. Failed refreshes expose a labeled last-known snapshot.
4. Render current availability, search and bike/dock modes. A Streamlit fragment reruns
   every 60 seconds; manual refresh bypasses the status cache.
5. Check prediction eligibility, then request one cached Paris-area weather forecast.
6. Pass the selected station, snapshot, mode, current UTC time and optional weather into
   `prediction.rules.forecast_station`. Render its status, category and reasons in details.

## Interfaces

`StationMapRow` holds station identity, coordinates, capacity, optional inventory, operating
flags and station-report time. `StationMapSnapshot` adds receipt/publication times and
last-known/error information. These data classes have no Streamlit dependency.

`forecast_station` returns immutable `StationOutlook`: status, optional risk, target UTC time,
starting inventory, area/period descriptions, optional weather, reasons and rules version.
The rule function makes no network requests or filesystem writes.

`WeatherService` validates Open-Meteo's UTC hourly response, caches successful forecasts for
15 minutes and cools down failed requests for 60 seconds. Missing weather is a neutral rule
input rather than a prediction failure.

All application caches are memory-only. Runtime behavior does not require a writable
application directory. Static geographic definitions ship with the Python package.
