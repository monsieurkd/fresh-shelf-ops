# fresh-shelf-ops — every task is idempotent and re-runnable.
#
#   make setup      one-time: toolchain + dependencies
#   make world      simulate the source systems into data/landing (dirty)
#   make daily      ingest -> dbt build -> forecast -> optimise -> report
#   make check      what CI runs: lint + tests + dbt build
#
# The clock is pinned in config/project.yml (as_of_date). Nothing here writes
# a date: `--as-of`/`--from`/`--to` override it, everything else reads it.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

PY  := .venv/bin/python
DBT := .venv/bin/dbt

# Small-world knobs for CI / quick loops.
STORES ?= 2
DAYS   ?= 90

.PHONY: help setup world world-small daily ingest backfill build forecast optimise \
        report test test-unit lint fmt check idempotency docs status reset clean ci

help: ## Show the available tasks
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Create .venv, install dependencies (uv), install git hooks
	uv python install 3.12
	uv sync --all-groups
	.venv/bin/pre-commit install --install-hooks
	@echo "ready — try: make world && make daily"

world: ## Simulate the full estate into the landing zone (dirty source files + sealed truth)
	$(PY) -m fresh_shelf.cli world

world-small: ## Simulate a small world (STORES=2 DAYS=90) — used by CI
	$(PY) -m fresh_shelf.cli world --stores $(STORES) --days $(DAYS)

ingest: ## Load every landed file into the raw schema (idempotent, manifest-logged)
	$(PY) -m fresh_shelf.cli ingest

backfill: ## Re-process a landing window: make backfill FROM=2026-08-01 TO=2026-08-31
	$(PY) -m fresh_shelf.cli ingest --from $(FROM) --to $(TO) --force
	$(DBT) build

build: ## dbt deps + seed + build (the transform DAG is dbt's)
	$(DBT) deps
	$(DBT) seed
	$(DBT) build

forecast: ## Rolling-origin backtest + forward forecast into analytics_py
	$(PY) -m fresh_shelf.cli forecast

optimise: ## Replenishment + markdown plans, waste-vs-revenue frontier, $ impact
	$(PY) -m fresh_shelf.cli optimise

report: ## Render build/report/report.html (+ report.md, metrics.json)
	$(PY) -m fresh_shelf.cli report

daily: ## The recurring run: land -> raw -> warehouse -> forecast -> decisions -> report
	$(PY) -m fresh_shelf.cli run

status: ## Show the latest run manifest and table row counts
	$(PY) -m fresh_shelf.cli status

test: ## Full pytest suite
	$(PY) -m pytest

test-unit: ## Fast tests only (no dbt/world generation)
	$(PY) -m pytest -m "not slow"

lint: ## ruff check + dbt parse
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .
	$(DBT) parse

fmt: ## ruff check --fix + format
	.venv/bin/ruff check --fix .
	.venv/bin/ruff format .

check: lint test ## Everything CI runs on a pull request

idempotency: ## Prove the recurring run is repeatable: run the loader twice, compare manifests
	$(PY) -m fresh_shelf.cli ingest --force
	$(PY) -m fresh_shelf.cli ingest --force
	$(PY) -m fresh_shelf.cli status --assert-idempotent

docs: ## Generate the dbt docs site into build/dbt-docs
	$(DBT) docs generate
	@rm -rf build/dbt-docs && mkdir -p build/dbt-docs
	@# `target/index.html` is the docs entry point; older dbt versions named it
	@# `static_index.html`. Copying only the old name silently produced an empty
	@# directory on this version, so both are checked and a miss is an error.
	@if [ -f target/index.html ]; then \
		cp target/index.html build/dbt-docs/; \
	elif [ -f target/static_index.html ]; then \
		cp target/static_index.html build/dbt-docs/index.html; \
	else \
		echo "docs generation produced no index.html — check the dbt version" >&2; exit 1; \
	fi
	@cp target/catalog.json target/manifest.json build/dbt-docs/
	@echo "dbt docs in build/dbt-docs/index.html"

reset: ## Delete the landing zone, warehouse, truth and artifacts
	rm -rf data build

clean: ## Delete caches and dbt artifacts
	rm -rf .pytest_cache .ruff_cache target logs
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

ci: ## Parity with .github/workflows/ci.yml
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .
	$(PY) -m pytest -m "not slow"
	$(MAKE) world-small
	$(MAKE) build
	$(MAKE) forecast
	$(MAKE) optimise
	$(MAKE) report
	$(MAKE) idempotency
