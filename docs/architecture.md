# Architecture

## Repository structure

```text
BikePulse/
├── README.md
├── AGENTS.md
├── Makefile
├── config.example.toml
├── pyproject.toml
├── uv.lock
├── src/bikepulse/
│   ├── app/        live Streamlit map
│   ├── collector/  fetch, validation, raw storage, and service loop
│   ├── config/     typed configuration
│   ├── data/       compaction and quality reporting
│   ├── ml/         canonical grid, labels, readiness, model study, prediction
│   └── io.py       atomic artifact writes
├── tests/
│   ├── fixtures/   captured, controlled GBFS responses
│   └── e2e/        browser smoke test
├── docs/
│   ├── audits/     dated evidence and findings
│   └── plans/      delivered scopes and future roadmap
└── data/           ignored runtime data and generated artifacts
```

Build output and tool caches (`dist/`, `.cache/`, `.pytest_cache/`, `.mypy_cache/`,
`.ruff_cache/`) are ignored and are not part of the repository contract.
`make clean` removes these generated files while preserving `.venv`, `.tools`, and `data/`.

## Runtime flow

```text
Official GBFS
    │
    ├── station_information ──► versioned station dimension
    │
    └── station_status ──► raw JSON.gz + poll receipt
                               │
                               ▼
                     hourly bronze/silver Parquet
                               │
                    ┌──────────┴──────────┐
                    ▼                     ▼
             quality/readiness      canonical ML grid
                                          │
                                          ▼
                                  labels and features
                                          │
                                          ▼
                                experiment artifacts
```

The collector owns experiment writes and its scheduled feed requests. The live map
separately fetches and caches current GBFS data for interactive display. Offline ML
commands read collected history and do not fetch the public feed.

## Boundaries

- `collector/` validates external data before storage or normalization.
- `data/` converts immutable raw evidence into rebuildable analytical tables.
- `ml/` consumes the analytical history and writes versioned generated artifacts.
- `app/` never trains a model or mutates collected history.
- Configuration enters through typed settings; `BIKEPULSE_DATA_DIR` can relocate
  all runtime artifacts without changing repository files.

The current package layout is intentionally small. New modules should stay inside
the domain that owns their state and tests should mirror public behavior rather
than implementation structure.
