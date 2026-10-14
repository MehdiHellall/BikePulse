PYTHON ?= python3.12
UV_ENV ?= .tools/uv
UV := $(UV_ENV)/bin/uv

.PHONY: setup run check smoke verify format clean
.PHONY: collect-start collect-status collect-stop
.PHONY: ml-poc ml-predict ml-report ml-readiness

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
