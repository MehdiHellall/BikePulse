# Live data contract

## Sources and validation

Station information and status use the configured official Vélib’ GBFS feeds. Valid station
coordinates are required for map placement. Duplicate information/status IDs are rejected;
malformed records are isolated without dropping valid neighboring stations. A station with
information but no usable status remains visible with unknown inventory.

Missing bike or dock counts stay unknown. Unknown counts never become zero. Mechanical and
electric counts follow the feed's type breakdown; total availability drives the bike outlook.
“Full” refers to zero standard docks, without a separate Station+ interpretation.

## Time and freshness

All internal timestamps are UTC; displayed times use Europe/Paris with date and timezone.
Keep these timestamps distinct:

- `fetched_at_utc`: local status receipt used by the in-memory snapshot service.
- `status_source_updated_at_utc`: status feed publication time.
- `information_source_updated_at_utc`: station-information publication time.
- `station_last_reported_utc`: individual station report time.

The outlook requires receipt and status publication ages between zero and 300 seconds.
Station reports are fresh through 3600 seconds, visibly delayed from 3600 to 7200 seconds,
and stale after 7200 seconds by default. Delayed reports remain eligible; stale reports do
not. This two-tier rule accommodates normal upstream station-report lag without accepting
arbitrarily old counts. Both thresholds are configurable under `[live]`.

Unknown or future timestamps, missing/negative inventory, nonpositive capacity, last-known
snapshots and unavailable/unknown required operating flags withhold the risk estimate. Bike
mode requires rentals enabled; dock mode requires returns enabled; both require installation.

Eligibility is checked before zero inventory: stale zero counts do not produce an
“Already empty” or “Already full” current-state claim.

The map retains its availability convention: zero red, 1–3 amber, 4+ green. Delayed reports
retain those colors but include a delayed label; unknown, stale, inactive and last-known
states are grey. The outlook uses the same station thresholds.

## Bounded recent observations

Each distinct successful station report may enter an in-memory rolling window. Records keep
only station ID, station-report time, capacity, bike/dock counts and required operating flags.
The default window is 20 minutes with a hard limit of 21 observations per station. Cached
duplicates, out-of-order or future reports, last-known snapshots and failed refreshes do not
add observations. Expired records and stations absent from a later successful snapshot are
removed. Nothing in this window is persisted.

Movement requires at least three usable reports spanning five minutes, with the newest no
more than five minutes old. Counts must remain within capacity and the required operation
must remain available. A large jump of at least eight units and 35% of capacity resets the
trend as probable rebalancing. When movement is unavailable or inconsistent, eligibility and
the baseline outlook are unchanged.

## Weather and outlook output

Weather is an optional hourly regional forecast, not an observation at the station. Forecast
arrays must have matching lengths and consecutive UTC hours. Temperature is Celsius, rain
probability percent and wind km/h. Missing, invalid or expired weather is neutral and never
withholds an otherwise eligible estimate.

A `StationOutlook` has status `estimated`, `already_empty`, `already_full` or `unavailable`.
Only `estimated` has a Low/Medium/High risk. An estimate may carry its robust movement summary
and the count of nearby pressured stations. No numerical risk probability or accuracy claim
is returned. Synthetic tests verify engineering behavior, not forecast quality.
