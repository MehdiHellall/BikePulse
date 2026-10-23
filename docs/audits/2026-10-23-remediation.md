# Project audit remediation — 23 October 2026

## Scope

This record closes the four findings and the temporal-orchestration coverage gap in the
[23 October project audit](2026-10-23-project.md). Work used synthetic fixtures and the
existing locked environment. The collector remained stopped; no public feed was queried,
and no frozen export, recorded study, model package, published research result, or spent
final test was regenerated or evaluated.

## Dependency remediation

Raised the application requirement to Streamlit 1.66.1 and performed a targeted lock
update. The synchronized environment contains:

| Package | Previous lock | Remediated lock |
|---|---:|---:|
| Streamlit | 1.65.0 | 1.66.1 |
| Pillow | 12.3.0 | 13.0.0 |
| certifi | 2026.7.22 | 2026.10.19 |

No other package version changed. `httptools` and `toml` left the lock because Streamlit
1.66.1 no longer depends on them. The installed modeling versions were preserved,
including scikit-learn 1.9.1, Polars 1.44.2, and NumPy 2.5.3. The live dependency audit
now reports no known vulnerabilities.

## Frozen-study lifecycle

New studies now enforce the forward sequence `prepared` → validation complete → `locked`
→ test complete → `packaged`, with optional smoke fitting before locking. Each completion
publishes an immutable, versioned stage manifest after its outputs. The manifest records
the request identity, protocol or prerequisite hashes, and output checksums.

Exact completed requests verify and return existing artifacts without fitting, scoring,
rewriting files, or resetting state. Conflicting selection, changed evidence, fitting after
lock, repeated scoring with a different request, out-of-order stages, and concurrent
mutation fail with explicit errors. Final-test execution writes an immutable attempt record
before scoring; an interrupted attempt blocks automatic rescoring. Test and package stages
are bound to the recorded selection, threshold, model metadata, and preceding manifests.

Lifecycle decisions no longer trust `state.json`, so a stale state file cannot roll a study
back. Reports and replay for pre-manifest legacy studies remain readable. Mutating legacy
commands fail rather than inventing integrity evidence or retraining historical work.

## Feed-record isolation

Both station normalizers now pass each member to record validation before accessing its
fields. A null, scalar, string, or list member produces one existing `RecordError` with
station ID `unknown` and the feed-specific validation code. Valid neighboring stations
continue through the unchanged normalized-batch interface.

Synthetic tests cover both feeds, live-map joining, and compaction. They confirm that valid
rows remain available and compaction quality records the rejected malformed member.

## Temporal-cache provenance

Subsequent temporal runs use cache namespace `data/ml/temporal_cache/v2/`. Cache identity
now includes the verified export manifest, all consumed development-history partitions,
station metadata, transformation settings and source files, a package source fingerprint,
Python, NumPy, Polars, and scikit-learn. Study identity combines that provenance with the
experiment configuration.

Each Parquet cache has a sidecar manifest with complete provenance, checksum, schema, row
count, and time bounds. Reuse independently verifies required columns and semantic dtypes,
unique station-time keys, development-only bounds, and every recorded artifact property.
Missing, obsolete, incomplete, corrupt, or invalid cache pairs rebuild atomically with an
explanation. Existing cache namespaces and recorded studies remain untouched.

The new synthetic integration fixture runs real feature generation, chronological folds,
fitting, evaluation, reports, plots, and cache reuse entirely below `tmp_path`. It exercises
checksum corruption, incomplete sidecar metadata, invalid dtypes, provenance changes, fold
purges, and exclusion of final-test timestamps. `ml/temporal_study.py` coverage increased
from 54% in the audit to 91%.

## Verification and cleanup

| Check | Command | Result |
|---|---|---|
| Locked install | `.tools/uv/bin/uv sync --locked --extra dev` | Passed |
| Formatting | `.venv/bin/ruff format --check .` | Passed; 62 Python files formatted |
| Lint | `.venv/bin/ruff check .` | Passed |
| Types | `.venv/bin/mypy src/bikepulse` | Passed; 29 source files |
| Tests/coverage | `.venv/bin/pytest` | 86 passed, 1 opt-in browser test skipped; 91.81% coverage |
| Dependency audit | `XDG_CACHE_HOME=.cache .venv/bin/pip-audit` | Passed; no known vulnerabilities |
| Package | `.venv/bin/python -m build --no-isolation` | Source and wheel builds passed |
| Browser | `BIKEPULSE_RUN_E2E=1 .venv/bin/pytest tests/e2e --no-cov` | 1 passed with Chromium |
| Complete gate | `make verify` | Passed |
| Safe cleanup | `make clean` | Removed 80 disposable files totaling 39,371,613 bytes |

The cleanup retained `.venv/`, `.tools/`, all ignored `data/` evidence, and the existing
modified and untracked project work. No collection, deployment, model selection, or
real-data study rerun was performed.
