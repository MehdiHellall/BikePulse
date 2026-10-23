# Past-only temporal stockout study

Decision: **A. Strong signal improvement**

Carry the winning past-only configuration into a prospectively collected study; do not reopen the spent frozen test period.

This is a development-only rolling-validation study. The frozen final test begins at `2026-10-14T00:00:00+00:00` and was neither loaded into the feature cache nor scored.

## Phase 1 audit

- Current feature and label generation: `src/bikepulse/ml/dataset.py`. The legacy core uses   current bikes/docks/capacity, bike ratio, an exact usable t-15m bike change, Paris time   sine/cosine, and weekend. Labels require usable exact observations at +5 through +30m.
- Frozen-study modelling: `src/bikepulse/ml/frozen_study.py`. Its HGBM uses learning rate   0.1, 100 iterations, 15 leaf nodes, minimum leaf size 100, L2=1, no early stopping,   no class weighting, and seed 42.
- Frozen boundaries: validation starts `2026-10-13T15:00:00+00:00`; the spent test   starts `2026-10-14T00:00:00+00:00`. Earlier labels whose 30-minute windows touch a   boundary are purged.
- Missing snapshots are not imputed: the canonical grid selects the latest whole snapshot   already received within five minutes; stale/inactive/missing counts are unusable. Any   unusable future makes a label null, and already-empty starting stations are excluded.
- Archived status payloads contain mechanical and electric bike counts, `is_renting`,   `is_returning`, `is_installed`, feed/source timestamps, receipt timestamps, station report   timestamps, and derived report age. These fields were confirmed in the normalized schema   and a raw archived payload.

## Validation design

Training is expanding and strictly earlier than validation. Every network timestamp stays in one fold. A 30-minute purge is applied before each validation block; validation labels must also finish before that block ends. Evaluation retains natural prevalence.

| Fold | Validation UTC | Train rows | Validation rows | Positives | Prevalence |
|---|---|---:|---:|---:|---:|
| oct12_evening | 2026-10-12T20:00:00+00:00 to 2026-10-13T00:00:00+00:00 | 83293 | 60543 | 840 | 1.39% |
| oct13_morning | 2026-10-13T06:00:00+00:00 to 2026-10-13T10:55:00+00:00 | 256228 | 71951 | 4818 | 6.70% |
| oct13_evening | 2026-10-13T19:00:00+00:00 to 2026-10-14T00:00:00+00:00 | 391336 | 77240 | 1626 | 2.11% |

Only three defensible blocks exist before the frozen test. They span three collection sessions but only three dates, so regime and seasonal uncertainty remains high.

## Feature availability and leakage safeguards

- `core`: Current published inventory, capacity, exact t-15m change, and calendar values; all are known at t.
- `multi_lag_depletion`: Exact t-h observations and trailing differences/slopes ending at t. Missing exact history remains missing; no forward filling or centered window is used.
- `volatility_state`: Trailing complete 15/30/60-minute windows and state-machine history updated only after each observation. Gaps longer than five minutes reset episode continuity.
- `station_priors`: Fold-specific expanding training encodings. Training rows see only earlier timestamps; validation rows use frozen training aggregates and never validation outcomes.
- `signed_network`: Contemporaneous neighbor inventory and exact trailing signed neighbor changes ending at t.
- The [feature catalog](temporal-feature-catalog.md) defines every generated column,   formula, horizon expansion, missing-state rule, and causal availability.
- Exact lag joins expose explicit observed/missing flags. Slopes and rolling summaries   require every five-minute point in their trailing window; gaps are never filled.
- Time-to-zero exists only for a negative trailing slope and is clipped at 240 minutes.
- Rebalancing flags mark station or synchronized neighbor jumps of at least five bikes   or 30% of capacity; they do not assume those jumps are rider demand.
- Shrunk station priors use a fixed Beta prior and fold-training history. A training row   is encoded before its timestamp is added; its outcome is added only after label-end.   Validation outcomes never update validation features.
- Feature cache: `data/ml/temporal_cache/9ff27c84473ebb06.parquet` (reused), with 510536 eligible rows and 360 timestamps. Complete 60-minute history rate: 90.92%.

## Feature ablation — original 30-minute target

Precision-at-recall values are points on the pooled out-of-fold PR frontier. The F1 threshold is shown only as the requested legacy reference; it is selected and described on the same development predictions and is not an independent policy estimate.

| Candidate | Features | AP | P@R20 | P@R30 | P@R40 | P@R50 | F1-ref precision | F1-ref recall | Alerts | Alert rate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline | 8 | 0.3231 | 0.4062 | 0.3885 | 0.3638 | 0.3413 | 0.3214 | 0.5656 | 12818 | 6.11% |
| lag_depletion | 86 | 0.3716 | 0.4825 | 0.4437 | 0.4137 | 0.3851 | 0.3504 | 0.5891 | 12245 | 5.84% |
| volatility_state | 122 | 0.3844 | 0.5252 | 0.4763 | 0.4359 | 0.3895 | 0.3688 | 0.5615 | 11089 | 5.29% |
| station_priors | 127 | 0.3821 | 0.5125 | 0.4618 | 0.4189 | 0.3871 | 0.3470 | 0.6177 | 12967 | 6.18% |
| signed_network | 160 | 0.3494 | 0.4473 | 0.4411 | 0.3957 | 0.3671 | 0.3363 | 0.5950 | 12887 | 6.14% |

[Full ablation PR plot](assets/temporal-ablation-pr.svg)

### Recall at fixed precision

| Candidate | R@P40 | R@P50 | R@P60 |
|---|---:|---:|---:|
| baseline | 0.2161 | 0.0262 | 0.0000 |
| lag_depletion | 0.4677 | 0.1638 | 0.0067 |
| volatility_state | 0.4790 | 0.2429 | 0.0139 |
| station_priors | 0.4503 | 0.2180 | 0.0172 |
| signed_network | 0.3870 | 0.0077 | 0.0003 |

### Matched alert budgets

| Candidate | P@0.5% / captured | P@1% / captured | P@2% / captured | P@5% / captured |
|---|---:|---:|---:|---:|
| baseline | 0.4509 / 473 | 0.4295 / 901 | 0.3938 / 1652 | 0.3432 / 3599 |
| lag_depletion | 0.5253 / 551 | 0.5114 / 1073 | 0.4567 / 1916 | 0.3700 / 3880 |
| volatility_state | 0.5338 / 560 | 0.5353 / 1123 | 0.4853 / 2036 | 0.3749 / 3932 |
| station_priors | 0.5434 / 570 | 0.5319 / 1116 | 0.4691 / 1968 | 0.3730 / 3912 |
| signed_network | 0.4480 / 470 | 0.4609 / 967 | 0.4443 / 1864 | 0.3605 / 3781 |

Best temporal candidate: `volatility_state`. Relative to baseline: AP +0.0613, precision at 40% recall +0.0721, with both directions positive in 2/3 folds.

### Per-fold matched comparison

| Fold | Baseline AP | Winner AP | ΔAP | Baseline P@R40 | Winner P@R40 | ΔP@R40 |
|---|---:|---:|---:|---:|---:|---:|
| oct12_evening | 0.2078 | 0.1827 | -0.0251 | 0.2120 | 0.1991 | -0.0129 |
| oct13_morning | 0.3726 | 0.4431 | +0.0705 | 0.3983 | 0.5029 | +0.1046 |
| oct13_evening | 0.2465 | 0.2858 | +0.0393 | 0.2946 | 0.3077 | +0.0131 |

The weakest regime is `oct12_evening`: it is the earliest fold, has only 83293 training rows and 1.39% prevalence, and loses -0.0251 AP. Its 1% alert-budget precision moves from 0.2822 to 0.2063. Missing long history is not the explanation (100.0% has complete 60-minute windows); both positive and negative rows have a median 30-minute slope of 0.000 and 0.000, respectively. This points to a low-prevalence, mostly flat-stock regime where extra trajectory features add little high-risk ordering signal, not a gap-handling artifact.

## Target and horizon experiment

Each task uses HGBM with the `volatility_state` feature family. Shorter and sustained targets are separate tasks, not improvements to the original 30-minute task.

| Target | AP | P@R20 | P@R30 | P@R40 | P@R50 | R@P60 | Prevalence |
|---|---:|---:|---:|---:|---:|---:|---:|
| stockout_10m | 0.2299 | 0.3085 | 0.2723 | 0.2469 | 0.2349 | 0.0028 | 1.40% |
| stockout_15m | 0.2868 | 0.3760 | 0.3388 | 0.3128 | 0.2871 | 0.0075 | 1.97% |
| stockout_20m | 0.3180 | 0.4310 | 0.3934 | 0.3559 | 0.3244 | 0.0018 | 2.50% |
| stockout_30m | 0.3844 | 0.5252 | 0.4763 | 0.4359 | 0.3895 | 0.0139 | 3.47% |
| sustained_30m | 0.3002 | 0.4144 | 0.3793 | 0.3396 | 0.3085 | 0.0000 | 2.50% |

[Full target/horizon PR plot](assets/temporal-horizon-pr.svg)

The provisional 60% precision at at least 20% recall target is not met by any task.

## Block uncertainty and failure analysis

Paired 60-minute block resampling used 14 usable network-wide blocks and 1000 draws. Mean AP difference: 0.0252 (95% block interval -0.0075 to 0.0543). Mean P@R40 difference: 0.0420 (0.0127 to 0.0683).

Blocks come from only three collection sessions across three calendar dates; the interval describes those observed sessions and is not a seasonal generalization CI.

The archive has 19.87% scheduled-slot coverage, long laptop gaps, and only a few independent regimes. Station priors can also be weak for stations absent from early training. Rebalancing is inferred only from stock jumps, not operator logs. These limitations dominate any narrow metric difference and prevent a deployment claim.

## Reproducibility

Configuration: `experiments/temporal-history.toml`. HGBM fit/prediction timings and all exact PR points are saved with the ignored study artifacts. Source export and frozen-study files were read-only; no final-test metric was computed.

Across all folds, ablations, and target variants, fitting took 128.1s and scoring took 3.2s. Per-fit timings are recorded in `protocol.json`.
