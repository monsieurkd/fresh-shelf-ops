# Runbook

How to run the pipeline, how to tell whether it worked, and what to do when it
does not.

## Daily run

```bash
make daily
```

Six steps in order, about two and a half minutes on the full estate:

| Step | Time | What it does |
|---|---:|---|
| `world` | 12s | Simulates the sources into `data/landing` |
| `ingest` | 92s | Loads them into `raw`, with dedupe and quarantine |
| `build` | 12s | `dbt deps`, seed and build, 69 nodes |
| `forecast` | 110s | Backtest and forward forecast |
| `optimise` | 1s | Plans, frontier, dollar impact |
| `report` | 0.1s | `build/report/index.html` |

In production, drop `world`. It simulates the source systems. Real extracts land
in `data/landing` instead and the rest of the run is unchanged.

## Checking a run

```bash
make status              # latest run manifest and table counts
make status --json       # machine-readable
make status --assert-idempotent   # fails if any store-day loaded twice
```

`status` reads `data/artifacts/runs/latest.json`, which every command writes. It
reports per-step timing, status and detail, plus ingest tallies by source:

```
latest run: run at 2026-10-10T08:29:04Z (195.2s, 6 steps)
  world              12.0s  ok   landed 4386 files (63 redeliveries)
  ingest             92.1s  ok   files=4386 loaded=4323 duplicate=63 late=168
  ...
  ingest pos_sales       loaded=4315 duplicate=63 quarantined=0 max_lag=1
```

Signals worth watching:

| Signal | Healthy | What a bad value means |
|---|---|---|
| `quarantined_rows` | 0 | Above zero is not automatically bad. Read the reasons in `ops.raw_quarantine` |
| late files | only the regional stores | Other stores late means the integration is drifting |
| duplicate files | non-zero and stable | Zero means the redelivery defect stopped firing, so dedupe is untested |
| `methods_agree_on_direction` | 0 on this estate | 1 means the accounting turned positive. Investigate before celebrating |

## When something breaks

### A step failed

The run log names the step. Read `data/artifacts/runs/latest.json` for the
exception, then re-run that step alone:

```bash
fresh_shelf.cli ingest --stores 8 --days 364 --as-of 2026-09-10
fresh_shelf.cli build  --stores 8 --days 364 --as-of 2026-09-10
```

Every step is idempotent, so re-running one is safe.

### A store-day is missing from `raw`

That is D03, a link outage. The source never sent it and the pipeline does not
invent data. Check:

```sql
select * from ops.ingest_files where source = 'pos_sales' and status <> 'loaded';
select * from raw.pos_sales where store_code = 'S07' and business_date = '2026-08-27';
```

`reporting.mart_data_quality` lists the affected store-days. They are excluded
from sell-through rather than read as zero sales.

### A dbt test failed

`dbt build` stops downstream of the failure, so a failing test can look like
missing tables. Run the models alone to separate the two:

```bash
dbt run --select marts
dbt test
```

Each singular test in `dbt_tests/` asserts two things: that the defect is still
being produced, and that the model handles it. If the first assertion fails, the
data changed and the test needs re-pointing. That is maintenance, not an
incident.

### Numbers look wrong

Work backwards from the report:

```bash
cat build/report/metrics.json | head -40      # what the page quotes
fresh_shelf.cli status --json | head -60      # what loaded
```

Then check the reconciliation from the other end:

```sql
-- Does the impact table close? A test enforces it, but check the live value.
select component, value from analytics.optimisation_impact order by component;
select "check", value from analytics.impact_reconciliation;
```

If `impact_net_benefit` and the frontier optimum disagree in direction, that is
expected on this estate. See AD-11 in `docs/DECISIONS.md`.

## Backfills

`--as-of` moves the whole clock, so a backfill re-runs every layer against a past
date:

```bash
fresh_shelf.cli world --as-of 2026-06-30
fresh_shelf.cli run   --as-of 2026-06-30
```

`ingest` also takes `--from` and `--to` to reload a landing window, and `--force`
to reprocess files whose checksum is already known. Use `--force` after a loader
change.

```bash
make backfill FROM=2026-08-01 TO=2026-08-31
```

## Idempotency

```bash
make idempotency
```

Loads the landing zone twice and fails if any source and business-day pair was
loaded more than once. This works because the loader replaces rows by
`(business_date, store_code)` instead of appending.

Run it after any loader change. A loader that appends will double every figure in
the report and nothing else will notice.

## CI

`make ci` runs lint, then the CI world end to end (four stores, 180 days), then
the full test suite, then the idempotency check.

The CI world is sized to cover the defects, not to run fast. Those four stores
are the ones the defect catalogue singles out. A two-store, 90-day world is
quicker but produces no waste events, so the tests that assert a defect is still
being produced fail for the right reason and the wrong cause.

The tests run after the world is built. Five report tests need a warehouse, and a
green run with skipped tests is worse than a red one: it reads as coverage that
is not there. The report fixture fails rather than skipping when there is no
warehouse, and `make test-unit` asserts that the fast tier skipped nothing.

```bash
make test-unit    # pure-logic tests, zero skips allowed
make test         # everything, including world generation
```

To run the static checks only, which is what the pre-commit hook does:

```bash
git config core.hooksPath .githooks    # opt in once
```

The hook runs ruff and refuses to commit generated files under `data/`, `build/`,
`target/` or `dbt_packages/`. One accidental `git add -A` would otherwise put
hundreds of megabytes of reproducible data into history.

### One rule about scope

Every stage must be given the same world scope. The Makefile passes
`--stores $(STORES) --days $(DAYS)` to `world`, `ingest`, `build`, `forecast`,
`optimise` and `report`. It did not always. `world` got the scope and nothing else
did, so the loader defaulted to all stores and loaded 2 files out of 1,444 while
every command reported success.

For the same reason, never call `dbt` directly in a recipe. `profiles.yml` has a
default warehouse path, so a bare `dbt build` can build a different database from
the one `make ingest` populated. Use the passthrough, which exports the project's
clock and warehouse:

```bash
fresh_shelf.cli dbt build
```

## Known gaps

1. Quarantine is untested. `ops.raw_quarantine` is implemented and every loader
   can populate it, but no simulated world produces an unparseable value, so the
   branch has never run. A deliberately corrupt fixture would close it. Until
   then, treat "quarantine works" as unverified.
2. The warehouse is rebuilt, not incremental. `dbt build` takes about 12 seconds
   at this scale. At ten times the estate, incremental models would be the first
   thing to add.
3. The weather input is a perfect forecast. The simulation gives the model exact
   future temperatures, which flatters the backtest.
4. `dbt deps` needs network access on first run to fetch `dbt_utils`. After that
   it is cached in `dbt_packages/`.

## Environment

| | |
|---|---|
| Python | 3.12 (`.python-version`; the code targets `>=3.12,<3.13`) |
| Warehouse | `data/warehouse/fresh_shelf.duckdb`, one file, no server |
| Clock | `config/project.yml`, overridable with `--as-of` or `FRESH_SHELF_AS_OF` |
| dbt profile | `profiles.yml` at the repo root, `DBT_PROFILES_DIR=.` |
| Dependencies | managed by `uv`; `uv.lock` is committed |

There are no credentials. The warehouse is a local file and the sources are
simulated, so `make setup` is the only setup step.
