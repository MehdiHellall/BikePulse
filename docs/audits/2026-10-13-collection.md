# Collection and label audit — 13 October 2026

This is a point-in-time audit of local raw status snapshots and their rebuilt silver
observations. The cutoff is **2026-10-13 18:47:57 UTC** (20:47:57 in Paris). No public
feed was called for this audit. All times below are UTC.

## Poll coverage

A poll received in the five minutes before a canonical boundary covers that boundary.
Several snapshots can cover the same slot. The last 2026-10-12 poll was received at
23:59:30 and covers 2026-10-13 00:00; the earlier quality report incorrectly called
that slot a gap. The daily gap calculation now includes the preceding five minutes.

| UTC day | Status snapshots | Covered / expected slots | Missing intervals |
|---|---:|---:|---|
| 5 Oct | 33 | 31 / 288 | 00:00–20:15; 22:50–24:00 |
| 6–11 Oct | 0 each day | 0 / 288 each day | All six days |
| 12 Oct | 95 | 93 / 288 | 00:00–16:15 |
| 13 Oct, through cutoff | 187 | 186 / 226 | 10:55–11:35; 12:20–15:00 |

The collector has recent successful polls, but the six-day gap and two interruptions
on 13 October are real. A new complete 14-day study window cannot start on 5 October.
The service pauses during laptop sleep or shutdown and is restarted manually after
reboot, so collection continuity needs daily review. Poll records alone do not
establish the cause of each interruption. Every captured status snapshot in this
audit has 1,519 station rows; no receipt timestamps are missing and no station
timestamps lie in the future.

## Initial station-timestamp interpretation

The rebuilt observations contain 50,127 rows on 5 October, 144,305 on 12 October,
and 284,053 through the cutoff on 13 October. At receipt, respectively 1,273, 9,848,
and 8,624 rows have station timestamps no more than 300 seconds old. A current
feed header or HTTP 200 response does not establish station freshness.

The canonical five-minute grid has **470,890 station rows** for the collected
period. Of these, **16,000** have a usable current observation. The strict dataset
builder classifies them as follows:

| Result | Rows |
|---|---:|
| Unusable current observation | 454,890 |
| Fresh, nonempty current observation; no usable exact 15-minute lag | 15,687 |
| Already empty at a usable current observation | 313 |
| Valid positive or negative label | **0** |

These zero-label results came from treating `last_reported` as the observation time
for every published count. Further inspection showed that this interpretation is
not suitable for this feed: its header stayed within 1.53 minutes of collection,
and 87.6% of adjacent bike/dock count changes occurred without a corresponding
change to `last_reported`.

## Raw-snapshot spot checks

These were checked against the stored JSON.gz snapshots and the derived grid:

| Station / boundary | Current observation | Required neighboring observations | Result |
|---|---|---|---|
| `1002059045`, 12 Oct 16:45 | 13 bikes; station report 16:43:02; fresh | Exact 16:30 lag has 9 bikes but report time 15:42:06; 16:50 report time is still 16:43:02 | `insufficient_history`; label null |
| `102304517`, 12 Oct 16:45 | 0 bikes; station report 16:42:31; fresh | A starting station must have bikes | `already_empty`; excluded |
| `101008598`, 13 Oct 10:50 | 5 bikes; station report 10:45:00; usable at the 300-second boundary | Exact 10:35 lag is stale; +5 through +30 minutes have no selected poll during the gap | `insufficient_history`; label null |

For station `1002059045`, the raw bike count changes from 13 at the 16:44:30
poll to 14 at 16:49:30, while `last_reported` stays at 16:43:02. This makes the
provider's station timestamp semantics unsuitable as the only age of the published
count.

## Resolution and revised readiness check

The offline study now defines an observation by the feed update timestamp and the
local receipt timestamp. Both must be recent enough for the canonical five-minute
boundary. `last_reported` remains in silver history and quality reports as a
separate diagnostic. The live map continues to use station-reported age.

Rebuilding the immutable snapshots with this rule produced complete labels. The
24-hour readiness window from 12 October 19:20 through 13 October 19:20 contains:

| Check | Result |
|---|---:|
| Covered poll slots | 248 / 288 (86.11%) |
| Feed-fresh station rows | 376,712 |
| Feature-eligible rows | 334,201 |
| Complete labels | 316,615 |
| Positive labels | 10,671 |
| Negative labels | 305,944 |

The 98% seven-day poll-coverage target remains separately reported. Missing slots
invalidate affected label windows; they do not convert them into negative labels.

Two real windows were traced back to raw snapshot IDs:

| Station / boundary | Counts through +30 minutes | Checked label |
|---|---|---:|
| `1057203651`, 12 Oct 19:20 | 2 at `t`, then 2, 0, 0, 0, 0, 0 | 1 |
| `100227241`, 12 Oct 19:20 | 7 at `t`, then 8, 8, 8, 7, 7, 7 | 0 |

Both have a usable exact 15-minute lag and six feed-fresh future observations.
Their station timestamps were old for part of the window, which is the condition
the revised feed-observation contract addresses.

The controlled fixture in `tests/test_ml_dataset.py` independently checks the
positive, negative, and invalid-window rules. These are engineering checks, not
evidence about real model quality:

| Fixture starting at `t` | Six future bike counts / condition | Checked result |
|---|---|---|
| 4 bikes, usable exact `t-15` | 3, 2, 0, 2, 3, 4; all usable | Positive label `1` |
| 4 bikes, usable exact `t-15` | 4, 4, 4, 4, 4, 4; all usable | Negative label `0` |
| 4 bikes, usable exact `t-15` | Zero observed at `t+15`, but `t+25` missing | Null label |
| 4 bikes, usable exact `t-15` | Zero observed at `t+15`, but `t+25` unusable | Null label |
| 0 bikes at the starting boundary | Future values do not matter | `already_empty`; excluded |

## Outcome and next gate

Complete real label windows are now forming, but there is not yet a completed
14-day chronological split. The existing 5–19 October experiment was run before
its end date and remains an `insufficient_data` diagnostic. The earliest preliminary
run that reuses the 12–13 October snapshots is 12–26 October, after 26 October
00:00 UTC. Its report must retain the documented partial coverage and gaps.

Verification: `make verify` passed after the feed-observation change: 51 unit/app
tests passed with 94.89% coverage, the dependency audit found no known
vulnerabilities, the package built, and the Playwright browser smoke test passed.
