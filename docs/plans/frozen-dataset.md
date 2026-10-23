# Build a frozen dataset from the collected snapshots

## Summary

Stop collection, record the last status receipt as a fixed cutoff, and rebuild the
final hour of derived history. Export **every valid labeled window** from the stored
snapshots at its observed class proportion. For later experiments, create a separate
training-only view containing every positive training row and nine sampled negatives
per positive (about 10% positive), using a fixed seed. Treat that view as an enriched
sample, not as a representative estimate of how often stations become empty.

This produces a usable dataset from the partial record; it does not claim 14 days of
coverage or create observations for gaps. A read-only check of the current silver data
found about **665,000 valid labels**, with both classes available in the proposed train,
validation, and test periods.

## Recorded outcome

Collection stopped and the source was frozen at receipt
`2026-10-15T07:29:30.349112+00:00`. Export `5d53d536cfc4304a` contains 665,402 complete
labeled windows from 553 checksum-verified snapshots. The natural positive prevalence is
2.95% (19,608 rows). The training view contains 9,167 positives and 82,503 sampled
negatives, for 91,670 rows at 10% positive prevalence.

The continuous five-minute coverage calculation is 542 of 2,728 slots (19.87%), mainly
because a short 5 October block precedes the sustained collection period. The
quality report records every gap. There are no duplicate station-time keys, geographic
coordinates are present on all exported rows, and train, validation, and test each contain
both classes. Generated artifacts are under `data/ml/exports/5d53d536cfc4304a/`; rerunning
the fixed cutoff reproduces their hashes.

Continue with the [guided frozen model study](frozen-model-study.md) for training,
validation reviews, final evaluation, and a deployment handoff.

## Dataset and split

- Add `make ml-dataset` and an `export-dataset` CLI command. The default cutoff is the
  last stored status receipt; an explicit UTC cutoff permits exact reruns.
- Write `full_labeled.parquet`, `enriched_train.parquet`, and a manifest and quality
  report under a separate `data/ml/exports/<run_id>/` directory. The full file retains
  valid rows assigned `train`, `validation`, `test`, or `purged`; rejected windows are
  counted in the report.
- Use a chronological split: train before **13 October 15:00 UTC**; validation from then
  until **14 October 00:00 UTC**; test from 14 October through the frozen cutoff. Purge
  rows whose 30-minute label window reaches the next split. Keep validation and test at
  their natural class proportions.
- Preserve station ID, observation and label times, snapshot ID, label, the existing
  eight features, geographic features, and split. Record source hashes, feature
  definitions, cutoff, gaps, class counts, original prevalence, sampling fraction, and
  seed. Do not train a model in this work.

## Geographic features

- Use the station coordinates known at each observation to add `latitude`, `longitude`,
  `neighbor_station_count_1km`, and `neighbor_capacity_1km`, excluding the station
  itself.
- Add `nearby_abs_net_bike_change_15m`: the sum of absolute bike-count changes at
  stations within 1 km that have usable observations at both `t` and exactly
  `t−15 minutes`. Add `nearby_observed_count_15m` so zero observed movement can be
  distinguished from zero qualifying neighbors. These features use information
  available by `t`.
- Report which areas show more observed stock movement, together with their observation
  counts. Call this a **cycling activity proxy**: count changes do not measure
  individual rides and can reflect rebalancing. Keep rows with missing coordinates,
  marking geographic fields missing rather than dropping valid labels.

## Verification and assumptions

- Test the cutoff, gap rejection, split purge, 1 km neighbor boundary, versioned
  coordinates, missing neighbors, deterministic 1:9 sample, and the rule that
  changing observations after `t` cannot change geographic features at `t`.
- Verify unique station–time keys, both classes in each full split, exactly nine
  negatives per positive in the enriched training file, reproducible hashes, and the
  final coverage report; then run `make verify`.
- Use only stored BikePulse snapshots and station metadata, as requested. The enriched
  view is for future training experiments; prevalence and evaluation must use the full,
  naturally imbalanced validation and test rows. The existing 14-day study command
  remains separate.
