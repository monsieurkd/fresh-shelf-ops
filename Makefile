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

# NB: never call $(DBT) directly from a recipe. It would run without the project's
# clock and warehouse exported, so it would build whichever database
# profiles.yml defaults to — which is how a small world silently produced empty
# staging tables while every command reported success. Use `cli dbt` instead.

# Small world for CI. Sized to cover the stores the defect catalogue singles out
# (S01/S03/S05/S07), not merely to be fast: several dbt tests assert that a defect
# is still *being produced*, and a world that excludes those stores makes those
# assertions fail for the right reason but the wrong cause.
#
# At 2 stores / 90 days there were no waste events at all, so the loss pipeline
# was untested on every push.
STORES ?= S01,S03,S05,S07
DAYS   ?= 180

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

world-small: ## Simulate the CI world (STORES/DAYS override; default covers every defect store)
	$(PY) -m fresh_shelf.cli world --stores $(STORES) --days $(DAYS)

ingest: ## Load every landed file into the raw schema (idempotent, manifest-logged)
	$(PY) -m fresh_shelf.cli ingest --stores $(STORES) --days $(DAYS)

backfill: ## Re-process a landing window: make backfill FROM=2026-08-01 TO=2026-08-31
	$(PY) -m fresh_shelf.cli ingest --from $(FROM) --to $(TO) --force
	$(PY) -m fresh_shelf.cli dbt build

build: ## dbt deps + seed + build (the transform DAG is dbt's)
	@# Routed through the CLI, not bare `dbt`. The CLI exports FRESH_SHELF_AS_OF
	@# and FRESH_SHELF_DB from config/project.yml, so every layer reads the same
	@# clock and the same warehouse file. Calling `dbt` directly here let the
	@# Makefile build a different database than `make ingest` had loaded, which
	@# is how a small world silently produced empty staging tables.
	$(PY) -m fresh_shelf.cli build --stores $(STORES) --days $(DAYS)

forecast: ## Rolling-origin backtest + forward forecast into analytics_py
	$(PY) -m fresh_shelf.cli forecast --stores $(STORES) --days $(DAYS)

optimise: ## Replenishment + markdown plans, waste-vs-revenue frontier, $ impact
	$(PY) -m fresh_shelf.cli optimise --stores $(STORES) --days $(DAYS)

report: ## Render build/report/report.html (+ report.md, metrics.json)
	$(PY) -m fresh_shelf.cli report --stores $(STORES) --days $(DAYS)

daily: ## The recurring run: land -> raw -> warehouse -> forecast -> decisions -> report
	$(PY) -m fresh_shelf.cli run

status: ## Show the latest run manifest and table row counts
	$(PY) -m fresh_shelf.cli status

test: ## Full pytest suite
	$(PY) -m pytest

test-unit: ## Fast tests only — pure logic, no world generation (must not skip)
	@$(PY) -m pytest -m "not slow" -q -rs | tee /tmp/fresh-shelf-fast.log
	@! grep -q skipped /tmp/fresh-shelf-fast.log || \
		{ echo "the fast tier skipped a test — it is meant to need no warehouse"; exit 1; }

test-slow: ## Only the tests that generate a world
	$(PY) -m pytest -m slow

lint: ## ruff check + dbt parse
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .
	$(PY) -m fresh_shelf.cli dbt parse

fmt: ## ruff check --fix + format
	.venv/bin/ruff check --fix .
	.venv/bin/ruff format .

check: lint test ## Everything CI runs on a pull request

idempotency: ## Prove the recurring run is repeatable: run the loader twice, compare manifests
	$(PY) -m fresh_shelf.cli ingest --force
	$(PY) -m fresh_shelf.cli ingest --force
	$(PY) -m fresh_shelf.cli status --assert-idempotent

docs: ## Generate the dbt docs site into build/dbt-docs
	$(PY) -m fresh_shelf.cli dbt docs generate
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

ci: ## Parity with .github/workflows/ci.yml — run before pushing
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .
	$(MAKE) world-small
	$(MAKE) ingest
	$(MAKE) build
	$(MAKE) forecast
	$(MAKE) optimise
	@# Tests run AFTER the world is built: five report tests need a warehouse and
	@# would otherwise skip, and a skip reads as coverage that is not there.
	$(PY) -m pytest --tb=short
	$(MAKE) test-unit
	$(MAKE) report
	$(MAKE) idempotency
