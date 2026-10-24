# Prediction rules — version 5

The station card estimates risk of no bikes or no standard docks over the next 30 minutes.
It displays **Rule-based estimate — not validated**. These are explicit product assumptions,
not measured station travel patterns. The result is a category, not a probability.

## Eligibility and current state

The [data contract](data-contract.md) defines freshness and operating checks. An ineligible
station has no risk category. An eligible station with zero inventory shows Already empty
or Already full. Otherwise the selected mode's inventory enters the following rules.

A station report is fresh for one hour, delayed from one to two hours, and stale after two
hours by default. Delayed reports remain eligible but are labeled in the station card and
explanation. Stale reports still withhold the estimate. The feed publication itself must
remain no more than five minutes old.

## Scoring

| Input | Rule |
|---|---|
| 1–3 available | Always High, regardless of context. |
| Otherwise ≤6 available or inventory/capacity ≤20% | Start with 2 points. |
| Otherwise | Start with 0 points. |
| At least two pressured stations within 500 m | Add 1 point, once. |
| Inside one or more configured busy zones | Add 1 point, once. |
| Forecast target in a configured peak period | Add 1 point. |
| Consistent recent movement projects 0–3 available | Add 4 points and require High. |
| Consistent falling movement projects ≤6 or ≤20% available | Add 3 points. |
| Other consistent falling movement | Add 2 points. |
| Consistent rising movement | Subtract 3 points, stopping at zero. |
| Stable, mixed or unavailable movement | No movement adjustment. |
| Rain probability ≥60%, wind ≥30 km/h, temperature <5°C or >30°C | Subtract 1 point, once. |
| Missing, invalid or expired weather | No weather adjustment; keep the station estimate. |

Apply current inventory, nearby pressure, area and time before the movement adjustment, then
apply weather. Clamp points to zero. 0–1 is Low, 2–3 Medium, 4+ High. The current 1–3 override
and a movement projection of 0–3 always require High, regardless of later adjustments. Ten of
50 docks starts with 2 points because it is exactly 20% of capacity. Overlapping zones and
multiple nearby stations never multiply their respective adjustments.

Nearby pressure means at least two usable stations within 500 metres have 0–3 available or
no more than 20% of capacity in the selected mode. Bike pressure checks bikes and rental
operation; dock pressure checks standard docks and return operation. Stale, invalid and
inactive neighbors are ignored.

Busy areas and peak periods remain nondirectional activity assumptions. Recent bikes and
recent docks are analyzed independently, so their directional adjustments can oppose one
another. Low risk is not an assurance that inventory will remain available.

## Recent movement

The live service retains only the latest 20 minutes and at most 21 observations per station.
Observations are keyed and ordered by the individual station-report timestamp. Cached
duplicates, out-of-order reports, future reports, failed refreshes and last-known snapshots
do not extend the window. The window is in memory only and disappears on process restart.

A usable trend needs at least three operating reports spanning five minutes, with the newest
report no more than five minutes old. The calculation uses all pairwise inventory slopes and
takes a median weighted toward pairs ending more recently. This limits the effect of a single
noisy count while giving recent movement greater influence. At least 60% of recency-weighted
consecutive movement must agree on rising, falling or stable direction; otherwise movement is
mixed and neutral.

A change is treated as probable rebalancing when it is both at least eight units and at least
35% of station capacity. Reports before the latest such jump are excluded. Capacity changes
and unavailable required operations also reset the usable sequence. The 30-minute movement
projection is always clamped between zero and station capacity. If movement cannot be trusted,
the baseline inventory, network, area, time and weather rules still return the outlook; there
is no separate warm-up status.

## Time

The target is current UTC time plus 30 minutes, converted to Europe/Paris before applying
weekday/weekend and hour rules. Start boundaries are inclusive; end boundaries are exclusive.
Weekdays: 07:00–10:00 and 17:00–20:00. Saturdays/Sundays: 11:00–19:00. Other times are off-peak.
Timezone conversion uses the timezone database, including daylight-saving changes.

## Busy-zone catalog — version 1

Zone membership uses great-circle distance, including the circle boundary. Coordinates are
approximate public landmark centers. The choice of areas and radii is a product assumption;
being outside a configured zone does not establish that an area is quiet.

| Zone | Latitude | Longitude | Radius | Coordinate reference |
|---|---:|---:|---:|---|
| Central Paris | 48.8566 | 2.3522 | 1500 m | Existing app map center; [Paris](https://en.wikipedia.org/wiki/Paris) |
| La Défense | 48.89278 | 2.23583 | 1500 m | [Grande Arche](https://en.wikipedia.org/wiki/Grande_Arche) |
| Gare du Nord | 48.88111 | 2.35528 | 750 m | [Station](https://en.wikipedia.org/wiki/Gare_du_Nord) |
| Gare de l’Est | 48.87694 | 2.35917 | 750 m | [Station](https://en.wikipedia.org/wiki/Gare_de_l%27Est) |
| Gare Saint-Lazare | 48.87694 | 2.32444 | 750 m | [Station](https://en.wikipedia.org/wiki/Gare_Saint-Lazare) |
| Gare Montparnasse | 48.84000 | 2.31861 | 750 m | [Station](https://en.wikipedia.org/wiki/Gare_Montparnasse) |
| Gare de Lyon | 48.8448 | 2.3735 | 750 m | [Station](https://en.wikipedia.org/wiki/Gare_de_Lyon) |
| Gare d’Austerlitz | 48.84222 | 2.36583 | 750 m | [Station](https://en.wikipedia.org/wiki/Gare_d%27Austerlitz) |

The catalog is packaged in `prediction/zones.py`; changing rule thresholds or zone definitions
requires updating the corresponding version and documentation.

## Regional weather

Version 3 uses one Open-Meteo hourly forecast at the Paris map center. The interface shows
temperature, rain chance and wind in one line. Poor riding weather can reduce assumed demand
by one point; normal weather makes no adjustment. Missing weather is neutral, clearly labeled
and never removes an otherwise eligible station estimate.

Successful forecasts cache in memory for 15 minutes. Failed requests use a 60-second retry
cooldown and never reuse expired values. Requests time out after three seconds. Weather is a
regional approximation rather than a station-level observation. Attribution: Open-Meteo,
CC BY 4.0.
