# Past-only feature catalog

This catalog defines every feature added by the temporal-history experiment. `t` is the
prediction boundary. All lags are exact canonical five-minute boundaries and must be usable;
no value is forward-filled. A missing exact observation produces a missing numeric feature and
an explicit observed/completeness indicator. All rolling windows end at `t`.

The authoritative ordered lists are `FEATURE_GROUPS` in
`src/bikepulse/ml/temporal_features.py` and the generated study `protocol.json`.

## Existing core

| Feature | Definition available at `t` |
|---|---|
| `available_bikes` | Published available-bike count in the selected snapshot at `t`. |
| `available_docks` | Published available-dock count at `t`. |
| `capacity` | Station capacity metadata effective when the snapshot was received. |
| `bike_ratio` | `available_bikes / capacity` at `t`. |
| `bike_change_15m` | Bikes at `t` minus bikes in the exact usable `t-15m` observation. |
| `hour_sin`, `hour_cos` | Cyclic Europe/Paris local clock encoding at `t`. |
| `weekend` | Europe/Paris Saturday/Sunday indicator at `t`. |

## Inventory composition, exact lags, and depletion

`h` is each of 5, 10, 15, 30, 45, and 60 minutes unless a row says otherwise.

| Feature or expansion | Definition available at `t` |
|---|---|
| `mechanical_bikes`, `electric_bikes` | Published type-specific counts at `t`. |
| `mechanical_bike_ratio`, `electric_bike_ratio` | Type count divided by capacity at `t`. |
| `lag_observed_{h}m` | One only when the exact `t-h` row exists and is usable. |
| `lag_bikes_{h}m` | Available bikes at exact `t-h`; missing when not observed. |
| `lag_bike_ratio_{h}m` | Bike ratio at exact `t-h`; missing when not observed. |
| `bike_delta_{h}m` | Bikes at `t` minus exact bikes at `t-h`. |
| `bike_delta_capacity_{h}m` | `bike_delta_{h}m / capacity`; avoids division by lag stock. |

`w` is each of 10, 15, 30, and 60 minutes. These features require every five-minute point
from `t-w` through `t`; otherwise their numeric value is missing.

| Feature expansion | Definition available at `t` |
|---|---|
| `history_complete_{w}m` | One when the entire trailing window is usable. |
| `bike_slope_{w}m` | Ordinary least-squares bikes-per-minute slope over the trailing window. |
| `bike_robust_slope_{w}m` | Median signed five-minute interval rate in bikes per minute. |
| `max_depletion_rate_{w}m` | Largest negative stock change expressed as positive bikes depleted per minute. |
| `min_depletion_rate_{w}m` | Smallest observed depletion rate; gains are negative depletion. |
| `declining_interval_count_{w}m` | Number of five-minute intervals whose stock declined. |
| `declining_interval_fraction_{w}m` | Declining intervals divided by all intervals in the window. |
| `consecutive_declining_intervals_{w}m` | Declining intervals ending at `t`, counted backward. |
| `time_to_zero_{w}m` | Current stock divided by `-slope` when slope is negative, clipped to 240 minutes; otherwise missing. |
| `stock_depletion_interaction_{w}m` | Current bikes multiplied by the positive part of `-slope`. |

| Feature | Definition available at `t` |
|---|---|
| `depletion_acceleration_15m` | Depletion slope over `t-15m..t` minus depletion slope over `t-30m..t-15m`. |
| `station_rebalance_jump_{h}m` | For `h` in 5, 15, 30: one when the station's absolute change is at least five bikes or 30% of capacity. |

## Volatility, persistence, and empty history

`w` is each of 15, 30, and 60 minutes. Window summaries require complete exact five-minute
history.

| Feature expansion | Definition available at `t` |
|---|---|
| `inventory_mean_{w}m`, `inventory_min_{w}m`, `inventory_max_{w}m` | Mean, minimum, and maximum stock in the trailing window. |
| `inventory_std_{w}m`, `inventory_range_{w}m` | Population standard deviation and max-minus-min stock. |
| `mean_absolute_change_{w}m` | Mean absolute five-minute bike-count change. |
| `direction_changes_{w}m` | Changes between non-zero gain and loss directions in adjacent intervals. |
| `fraction_bikes_le_{k}_{w}m` | Fraction of window observations with bikes at most `k`, for `k` in 1, 2, 3. |

| Feature | Definition available at `t` |
|---|---|
| `occupancy_state` | Low (`0`, bikes ≤3), medium (`1`), or high (`2`, bike ratio ≥0.7) at `t`. |
| `occupancy_state_duration_minutes` | Consecutive exact minutes in the current state, capped by the 60-minute lookback. |
| `minutes_since_empty` | Minutes since the most recent observed zero in the current gap-free segment. |
| `empty_episode_count_6h` | Empty-episode starts in the trailing six hours of the current gap-free segment. |
| `minutes_since_recovery` | Minutes since the first non-empty observation following an empty episode. |
| `previous_empty_episode_minutes` | Observed duration of the last completed empty episode. |

A gap longer than five minutes or an unusable row resets episode continuity. The code never
infers that an unobserved station stayed empty or recovered.

## Fold-specific station priors

These transformations are rebuilt for every fold and target. Training rows are encoded before
their timestamp's occupancy is added. Labels are added only when their label-end time is known.
Validation encodings use aggregates frozen at the validation boundary; validation rows and
outcomes never update them. Stockout rates shrink toward an expanding global Beta(1, 19) rate;
station/time cells shrink toward their station estimate. Occupancy means use analogous partial
pooling. Time-of-day is a four-hour Europe/Paris bucket.

| Feature | Definition available at `t` |
|---|---|
| `station_stockout_propensity` | Shrunk historical target rate for the station. |
| `station_hour_stockout_propensity` | Shrunk station × four-hour-bucket historical target rate. |
| `station_typical_bike_ratio` | Shrunk historical mean bike ratio for the station. |
| `station_time_typical_bike_ratio` | Shrunk station × four-hour-bucket mean bike ratio. |
| `bike_ratio_deviation_from_station_time` | Current ratio minus the station/time typical ratio. |

## Signed network context

Neighbors are other stations no farther than 1 km using metadata effective at `t`. Current local
aggregates use usable neighbor rows at `t`. Change features require the neighbor's current row and
exact usable `t-h` row. `h` is each of 5, 15, and 30 minutes.

| Feature | Definition available at `t` |
|---|---|
| `nearby_observed_current` | Number of usable neighbors at `t`. |
| `nearby_total_bikes`, `nearby_total_capacity` | Sum of current neighbor bikes and capacity. |
| `nearby_local_bike_ratio` | Nearby total bikes divided by nearby total capacity. |
| `nearby_occupancy_deficit` | Sum of `capacity - bikes` over usable neighbors. |
| `nearby_min_bike_ratio`, `nearby_mean_bike_ratio` | Minimum and mean usable-neighbor bike ratio. |
| `nearby_empty_count`, `nearby_near_empty_count` | Neighbors with zero bikes and with at most three bikes. |
| `nearby_observed_change_{h}m` | Neighbors with a valid exact change over horizon `h`. |
| `nearby_signed_bike_change_{h}m` | Sum of current-minus-lag bike changes; negative means net depletion. |
| `nearby_losing_count_{h}m`, `nearby_gaining_count_{h}m` | Counts of neighbors with negative and positive signed change. |
| `nearby_fraction_depleting_{h}m` | Losing neighbors divided by neighbors with valid change. |
| `nearby_weighted_signed_change_{h}m` | Signed changes weighted by `exp(-distance / 1000m)`. |
| `nearby_depletion_spreading_{h}m` | One with at least three observed changes, at least half losing, and negative total signed change. |
| `nearby_rebalancing_signature_{h}m` | One when at least two large jumps occur and at least 60% share a sign. A large jump is at least five bikes or 30% of capacity. |
