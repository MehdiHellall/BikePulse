# Current data contract

This document describes the implemented live-map and offline empty-bike POC. The
future product contract is tracked separately in the [MVP plan](plans/mvp.md).

## Time fields

| Field | Meaning | Current use |
|---|---|---|
| `collected_at_utc` | Scheduled/local collection time recorded for the snapshot | Partitioning and provenance |
| `received_at_utc` | Time the complete HTTP response was received locally | Proof that data was available before a grid boundary |
| `feed_updated_at_utc` | Provider timestamp from the status-feed header | ML observation freshness |
| `station_last_reported_utc` | Provider timestamp attached to one station record | Live-map freshness and diagnostics |
| `data_age_seconds` | Station-report age at collection | Map and quality diagnostics |

These fields are not interchangeable. Stored history without a receipt or feed
timestamp remains readable for diagnostics but is ineligible for ML labels.

## Observation table

Primary key: `(snapshot_id, station_id)`.

```text
snapshot_id
collected_at_utc
received_at_utc
feed_updated_at_utc
station_id
station_last_reported_utc
data_age_seconds
is_installed
is_renting
is_returning
available_bikes
mechanical_bikes
electric_bikes
available_docks
capacity
latitude
longitude
```

Counts and metadata that are missing remain null. A null count never means zero.

## Storage layout

```text
data/
├── raw/status/YYYY/MM/DD/HH/*.json.gz
├── raw/information/YYYY/MM/DD/HH/*.json.gz
├── polls/YYYY-MM-DD/*.json
├── bronze/observations/date=YYYY-MM-DD/hour=HH.parquet
├── silver/observations/date=YYYY-MM-DD/hour=HH.parquet
├── silver/station_dimension.parquet
├── quality/
└── ml/
    ├── readiness/{daily,rolling}/
    ├── datasets/
    └── experiments/
```

Raw responses and poll receipts are evidence. Bronze, silver, quality, readiness,
dataset, and experiment files are derived artifacts and can be rebuilt.

## Canonical observation

At each five-minute UTC boundary, the ML pipeline selects the latest whole snapshot
received in the preceding five minutes. The row is usable when:

- receipt and feed update timestamps exist and are not in the future;
- both timestamps are at most `max_feed_age_seconds` old;
- capacity is positive and bike/dock counts are known;
- the station is installed, renting, and returning.

`max_station_age_seconds` does not decide ML usability. It controls live-map
freshness and remains a separate quality diagnostic.

## Empty-bike target

A starting row must be usable and have at least one bike. The exact observation
at `t-15 minutes` must also be usable for the change feature. All observations at
`t+5, +10, +15, +20, +25, +30 minutes` must be usable.

- label `1`: bikes reach zero in at least one future observation;
- label `0`: bikes remain above zero in all six future observations;
- null: any required current, lag, or future observation is unusable;
- `already_empty`: the starting count is zero and the row is excluded.

An observed zero followed by a missing future is still null. Collection gaps never
become negative labels.

## Study artifacts

The current feature contract is `empty-poc-v2-feed-observation`. A successful
14-day study writes a source fingerprint, dataset fingerprint, report, model,
manifest, and test predictions under `data/ml/experiments/<run_id>/`. Single-class
or insufficient partitions write an explicit `insufficient_data` report and no
model artifact.
