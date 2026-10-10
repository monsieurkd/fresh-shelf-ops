# Runbook

Operating the pipeline: how to run it, how to tell whether it worked, and what to
do when it does not.

---

## Daily run

```bash
make daily
```

Six steps in order. The whole thing takes about **2.5 minutes** on the full
estate:

| Step | Time | What it does |
|---|---:|---|
| `world` | 12s | Simulates the sources into `data/landing` |
| `ingest` | 92s | Loads them into `raw`, with dedupe and quarantine |
| `build` | 12s | `dbt deps && seed && build` — 69 nodes |
| `forecast` | 110s | Backtest + forward forecast |
| `optimise` | 1s | Plans, frontier, dollar impact |
| `report` | 0.1s | `build/report/index.html` |

**In production, drop `world`.** It simulates the source systems. Real extracts
land in `data/landing` instead, and the rest of the run is unchanged — which is
the point of the landing-zone contract.

---

## Checking a run

```bash
make status              # latest run manifest + table counts
make status --json       # machine-readable
make status --assert-idempotent   # fails if any store-day loaded twice
```

`status` reads `data/artifacts/runs/latest.json`, which every command writes. It
reports per-step timing, status and detail, plus the ingest tallies by source:

```
latest run: run at 2026-10-10T08:29:04Z (195.2s, 6 steps)
  world              12.0s  ok   landed 4386 files (63 redeliveries)
  ingest             92.1s  ok   files=4386 loaded=4323 duplicate=63 late=168
  ...
  ingest pos_sales       loaded=4315 duplicate=63 quarantined=0 max_lag=1
```

### The numbers that should be true

| Signal | Healthy | What a bad value means |
|---|---|---|
| `quarantined_rows` | 0 | >0 is not automatically bad — *read the reasons in `ops.raw_quarantine`* |
| `late` files | only the regional stores | Other stores late means the integration is drifting |
| `duplicate` files | non-zero, and stable | Zero means the redelivery defect stopped firing, so the dedupe is untested |
| `methods_agree_on_direction` | 0 on this estate | 1 would mean the accounting turned positive — investigate, don't celebrate |

---

## When something breaks

### A step failed

The run log names the step. Read `data/artifacts/runs/latest.json` for the
exception, then re-run just that step:

```bash
fresh_shelf.cli ingest --stores 8 --days 364 --as-of 2026-09-10
fresh_shelf.cli build  --stores 8 --days 364 --as-of 2026-09-10
```

Every step is idempotent, so re-running one is always safe.

### A store-day is missing from `raw`

That is D03 — a link outage. The source never sent it, and the pipeline refuses
to invent data. Check:

```sql
select * from ops.ingest_files where source = 'pos_sales' and status <> 'loaded';
select * from raw.pos_sales where store_code = 'S07' and business_date = '2026-08-27';
```

`reporting.mart_data_quality` lists the affected store-days, and they are excluded
from sell-through rather than read as zero sales.

### A dbt test failed

`dbt build` stops downstream of the failure, so a test failing looks like missing
tables. Run the models alone to separate the two:

```bash
dbt run --select marts
dbt test
```

Each singular test in `dbt_tests/` asserts two things: that the defect is still
being produced, and that the model handles it. If the *first* assertion fails, the
data changed and the test needs re-pointing — that is a maintenance task, not an
incident.

### Numbers look wrong

Work backwards from the report:

```bash
cat build/report/metrics.json | head -40      # what the page quotes
fresh_shelf.cli status --json | head -60      # what loaded
```

Then check the reconciliation from the other end:

```sql
-- Does the impact table close? (A test enforces this, but check the live value.)
select component, value from analytics.optimisation_impact order by component;
select "check", value from analytics.impact_reconciliation;
```

If `impact_net_benefit` and the frontier optimum disagree in direction, that is
expected on this estate — see `docs/DECISIONS.md` AD-11. It is not a bug.

---

## Backfills

`--as-of` moves the whole clock, so a backfill re-runs every layer against a past
date:

```bash
fresh_shelf.cli world --as-of 2026-06-30
fresh_shelf.cli run   --as-of 2026-06-30
```

`ingest` also takes `--from`/`--to` to reload a landing window and `--force` to
reprocess files whose checksum is already known — which is what you want after a
loader change:

```bash
make backfill FROM=2026-08-01 TO=2026-08-31
```

---

## Idempotency

```bash
make idempotency
```

Loads the landing zone twice and fails if any source/business-day pair was loaded
more than once. Safe because the loader replaces rows by
`(business_date, store_code)` rather than appending.

**This is worth re-running after any loader change.** A loader that appends
instead of replacing will double every figure in the report, and nothing else will
notice.

---

## CI

`make ci` mirrors the pipeline: lint, then the **CI world end to end** (four
stores — S01/S03/S05/S07 — over 180 days), then the full test suite, then the
idempotency check.

**The CI world is sized to cover the defects, not to be fast.** Those four stores
are the ones `config/defect_catalogue.yml` singles out. A two-store, 90-day world
is quicker but exercises nothing: there were **no waste events at all**, so the
tests asserting "the defect is still being produced" had nothing to assert
against and failed for the right reason but the wrong cause.

**The tests run *after* the world is built, deliberately.** Five report tests need
a warehouse, and a green run with skipped tests is worse than a red one — it
reads as coverage that is not there. The report fixture therefore *fails* rather
than skipping when there is no warehouse, and `make test-unit` asserts that the
fast tier skipped nothing:

```bash
make test-unit    # pure-logic tests, zero skips allowed
make test         # everything, including world generation
```

To run the static checks only (what the pre-commit hook does):

```bash
git config core.hooksPath .githooks    # opt in once
```

The hook runs ruff and refuses to commit generated files under `data/`, `build/`,
`target/` or `dbt_packages/` — one accidental `git add -A` would otherwise put
hundreds of megabytes of reproducible data into history.

### One rule about scope

Every stage must be given the **same** world scope. The Makefile passes
`--stores $(STORES) --days $(DAYS)` to `world`, `ingest`, `build`, `forecast`,
`optimise` and `report`. It did not always: `world` got the scope and nothing else
did, so the loader defaulted to all stores and loaded 2 files out of 1,444 while
every command reported success.

For the same reason, never invoke `dbt` directly in a recipe. `profiles.yml` has a
default warehouse path, so a bare `dbt build` will happily build a different
database than `make ingest` populated. Use the passthrough, which exports the
project's clock and warehouse:

```bash
fresh_shelf.cli dbt build
```

---

## Known operational gaps

These are real and recorded rather than hidden:

1. **Quarantine is untested.** `ops.raw_quarantine` is implemented, reachable, and
   every loader can populate it — but no simulated world produces an unparseable
   value, so the branch has never run. A deliberately corrupt fixture file would
   close it. Until then, treat "quarantine works" as unverified.

2. **The warehouse is full-rebuild, not incremental.** `dbt build` takes 12s at
   this scale, so incremental models would be complexity for no benefit. At ten
   times the estate they would be the first thing to add.

3. **The weather input is a perfect forecast.** The simulation gives the model
   future temperatures exactly. A real deployment would not have that, and the
   backtest is flattered by it.

4. **`dbt deps` needs network access** on first run, to fetch `dbt_utils`. After
   that it is cached in `dbt_packages/`.

---

## Environment

| | |
|---|---|
| Python | 3.12 (`.python-version`; the code targets `>=3.12,<3.13`) |
| Warehouse | `data/warehouse/fresh_shelf.duckdb` — one file, no server |
| Clock | `config/project.yml`, overridable with `--as-of` or `FRESH_SHELF_AS_OF` |
| dbt profile | `profiles.yml` at the repo root, `DBT_PROFILES_DIR=.` |
| Dependencies | managed by `uv`; `uv.lock` is committed |

There are no credentials anywhere in this project. The warehouse is a local file
and the sources are simulated, so `make setup` is the only setup step.
