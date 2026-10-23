PYTHON ?= python3.12
UV_ENV ?= .tools/uv
UV := $(UV_ENV)/bin/uv

.PHONY: setup run check smoke verify format clean
.PHONY: collect-start collect-status collect-stop
.PHONY: ml-poc ml-predict ml-report ml-readiness ml-dataset
.PHONY: ml-frozen-prepare ml-frozen-study ml-frozen-report ml-frozen-lock
.PHONY: ml-frozen-test ml-frozen-replay ml-frozen-package
.PHONY: ml-temporal-study

$(UV):
	$(PYTHON) -m venv $(UV_ENV)
	$(UV_ENV)/bin/python -m pip install --upgrade uv

setup: $(UV)
	$(UV) sync --locked --extra dev

# Development
run:
	.venv/bin/streamlit run src/bikepulse/app/main.py --server.headless=true

check:
	.venv/bin/ruff format --check .
	.venv/bin/ruff check .
	.venv/bin/mypy src/bikepulse
	.venv/bin/pytest
	XDG_CACHE_HOME=.cache .venv/bin/pip-audit
	.venv/bin/python -m build --no-isolation

smoke:
	.venv/bin/playwright install chromium
	BIKEPULSE_RUN_E2E=1 .venv/bin/pytest tests/e2e --no-cov

verify: check smoke

format:
	.venv/bin/ruff format .
	.venv/bin/ruff check --fix .

clean:
	rm -rf .cache .mypy_cache .pytest_cache .ruff_cache dist htmlcov
	rm -f .coverage
	find src tests -type d -name __pycache__ -prune -exec rm -rf {} +

# Collector service
collect-start:
	.venv/bin/python -m bikepulse.collector.manage start

collect-status:
	.venv/bin/python -m bikepulse.collector.manage status

collect-stop:
	.venv/bin/python -m bikepulse.collector.manage stop

# Offline ML study
ml-poc:
	.venv/bin/python -m bikepulse.ml.cli study --start "$(START)" --end "$(END)"

ml-predict:
	.venv/bin/python -m bikepulse.ml.cli predict --station-id "$(STATION_ID)"

ml-report:
	.venv/bin/python -m bikepulse.ml.cli report

ml-readiness:
	.venv/bin/python -m bikepulse.ml.cli readiness --hours "$(HOURS)"

ml-dataset:
	.venv/bin/python -m bikepulse.ml.cli export-dataset $(if $(CUTOFF),--cutoff "$(CUTOFF)")

# Guided frozen-data ML study
ml-frozen-prepare:
	.venv/bin/python -m bikepulse.ml.cli frozen-prepare --export-id "$(EXPORT_ID)"

ml-frozen-study:
	.venv/bin/python -m bikepulse.ml.cli frozen-study --study-id "$(STUDY_ID)" --mode "$(MODE)"

ml-frozen-report:
	.venv/bin/python -m bikepulse.ml.cli frozen-report --study-id "$(STUDY_ID)"

ml-frozen-lock:
	.venv/bin/python -m bikepulse.ml.cli frozen-lock --study-id "$(STUDY_ID)" --candidate "$(CANDIDATE)"

ml-frozen-test:
	.venv/bin/python -m bikepulse.ml.cli frozen-test --study-id "$(STUDY_ID)"

ml-frozen-replay:
	.venv/bin/python -m bikepulse.ml.cli frozen-replay --study-id "$(STUDY_ID)" --station-id "$(STATION_ID)" --as-of "$(AS_OF)"

ml-frozen-package:
	.venv/bin/python -m bikepulse.ml.cli frozen-package --study-id "$(STUDY_ID)"

# Development-only past-history research; never scores the frozen final test.
ml-temporal-study:
	.venv/bin/python -m bikepulse.ml.temporal_study --config experiments/temporal-history.toml
