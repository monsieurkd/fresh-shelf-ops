# Decisions

Why the project is shaped the way it is. Each decision records what was chosen,
what it cost, and how to reverse it — because a decision nobody can reverse is
not a decision, it is a trap.

The ones marked **reversed** were got wrong first and corrected. They are kept
because the reasoning that failed is more instructive than the reasoning that
worked.

---

## AD-01 — Pin the clock in config; never call `date.today()`

**Decision.** Every layer reads its as-of date from `config/project.yml`
(`clock.as_of_date: 2026-09-10`). Nothing in the codebase reads the wall clock.
`--as-of` overrides it for a backfill.

**Why.** A pipeline that calls `date.today()` cannot be replayed. "The last 12
months" means something different every run, a backfill cannot be re-run, and a
reviewer cannot check a number in the report against the data that produced it.

**Knock-on.** A `crc32`-based stable index replaced `hash()` in the world
generator, because Python salts `hash()` per process and every store would have
got a different local demand index on every run.

**Reverse.** Set `clock.as_of_date` in config. Nothing else changes.

---

## AD-02 — Catalogue every defect, and name the test that proves the handling

**Decision.** `config/defect_catalogue.yml` describes all thirteen defects: what
the source does, which stores it affects, how the pipeline must handle it, and
the test that proves it. Tests read the catalogue and assert every entry names a
test that exists.

**Why.** "We handle messy data" is not a claim anyone can check. A catalogue that
each entry can point at a failing test is. It also means a defect whose injection
stops firing is caught — several tests assert the defect is *still being
produced*, so they cannot pass vacuously.

**Cost.** The catalogue can drift from the code. Mitigated by asserting the named
tests exist, and by splitting D01–D06 (ingest-level) from D07–D13
(warehouse-level) so each names a test where it can actually be proven.

---

## AD-03 — Normalise units, timezone and duplicates exactly once

**Decision.** The loader converts cents to dollars, decimal commas to decimals,
UTC to Adelaide time, and de-duplicates by sha256. Nothing downstream re-derives
any of it.

**Why.** These are the errors that do not raise. A cents file read as dollars is
a 100× error that renders perfectly; a UTC timestamp dated by its UTC date files a
7pm disposal under the wrong trading day. Doing each conversion in one place makes
it testable; doing it in two places makes it a race between them.

**Enforcement.** `tests/test_ingest_defects.py` — D04, D05 and D06 each have a
test that asserts the raw layer recovers the sealed truth.

**Reverse.** Move a conversion into a dbt model. Don't — the test suite is built
around the current boundary.

---

## AD-04 — Partition landing files by *arrival* date, not business date

**Decision.** `data/landing/<source>/<landing_date>/` where `landing_date` is when
the file appeared, not what it contains.

**Why.** This is what makes D02 a real problem rather than a theoretical one. If
the folder told you the business date, a late file would be trivial. Because it
does not, the loader must read the file — which is what a real pipeline has to do.

**Reverse.** Change `landing_day_dir()` in `config.py`. The loader already ignores
folder names for business dates.

---

## AD-05 — The warehouse never reads `data/truth`

**Decision.** `data/truth/` holds the sealed answer key. dbt reads `raw` and
`seeds` only.

**Why.** A warehouse that validates itself against a file only it can see is
measuring nothing. This one is easy to violate by accident — the trading calendar
originally existed *only* in the truth directory, so `stg_trading_calendar` would
have had to read it. It now lands as a normal source feed with its own loader.

**Enforcement.** `tests/test_boundaries.py` scans the source and fails on any
read of a `truth_*` table or of parquet from the truth directory. It distinguishes
writers (legal), docstrings (prose), and `landing_gaps.csv` (pipeline metadata a
real system would know).

**Reverse.** Don't. If dbt needs data that only exists in the truth, land it as a
source instead — as was done for the calendar.

---

## AD-06 — Recorded waste is a floor; publish the gap

**Decision.** Loss scans are treated as a lower bound. `int_loss_reconciliation`
measures the residue rather than trusting the scan file.

**Why.** Roughly a fifth of disposals are never scanned, and scanning compliance
varies by store. Quoting the scan total as "the waste" understates it and hides
the operational problem causing the shortfall.

**Cost.** The residue absorbs three things at once — genuine unscanned loss, count
error, booking mistakes. Count error dominates on this estate, so the residue
overstates unscanned loss several-fold. Handled by AD-07.

---

## AD-07 — Publish the residue as a bound, with a confidence score

**Decision.** `int_loss_reconciliation` emits `shrink_confidence` per segment; the
report quotes `weighted_shrink_cost` and shows the raw residue beside it.

**Why.** A count eyeballed to ±3% on a book that moves ~1.2k units per segment
cannot resolve a 1% loss. Measured against the simulator's own records the residue
runs about 8× the true unscanned loss. Presenting it as a measurement would make
every other number less trustworthy by association.

**Reverse.** Drop the weighting; the raw column is still there. Doing so would
overstate shrink by roughly eightfold.

---

## AD-08 — Keep the three losses apart, and never sum them

**Decision.** Markdown erosion (margin), recorded waste (cost) and unrecorded
shrink (cost) are separate columns, separate report sections, and never totalled.

**Why.** They have three different fixes. A single "loss" number averages a
pricing decision, a buying decision and a process problem into something nobody
can act on. `tests/test_report.py` asserts the page never introduces a total.

**Reverse.** Trivial — but it would make the report worse.

---

## AD-09 — Measure the forecast's calibration; do not assume it

**Decision.** The backtest publishes `forecast_calibration`: what coverage each
nominal quantile actually achieved. The optimiser consumes the *achieved* level.

**Why.** The q0.95 forecast covers ~0.87 of outcomes on this estate. A plan that
sized orders to the nominal figure believing it was a 95% service level would
under-order, lose sales, and never see it — the error is in the safe-looking
direction.

**Reverse.** Ignore the calibration table; the plan then runs at whatever the
model actually delivers.

---

## AD-10 — Forecast selection is by quantile loss, not by WAPE

**Decision.** The forward forecast uses whichever model has the best mean pinball
loss at the top quantile, not the best WAPE.

**Why.** The optimiser consumes a quantile. A model with a slightly worse mean but
well-calibrated tails is more useful, and selecting on WAPE would select against
exactly that. On this estate they happen to agree (`hgb_quantile` wins both), but
the rule matters when they do not.

---

## AD-11 — Publish the disagreement between the two methods

**Decision.** `impact_reconciliation` reports whether the frontier and the
dollar-impact accounting agree. On this estate they do not, and both are shown.

**Why.** The two answer the same question from different directions. The frontier
sweeps an order ratio and prices waste against lost margin, giving an interior
optimum at a service level of ~0.41. The impact function differences two concrete
order plans and nets negative.

The reason is structural, not a bug: **waste is 1.71% of sales while margin is
41%**, so a unit not stocked costs roughly 24× a unit wasted. Ordering down cannot
pay for itself on margin alone. The recommended plan also averages a service level
above the unconstrained optimum, because pack sizes and shelf-life caps mean you
cannot order three-quarters of a tray.

**Consequence for the headline.** The defensible claims are markdown *timing* and
the specific lines whose cover exceeds their shelf life — not a blanket reduction
in ordering. Quoting a benefit while the frontier points the other way would be
misleading.

**Reverse.** `methods_agree_on_direction` flips to 1 if the accounting is ever
positive; the note changes automatically.

### AD-11a — reversed: the impact haircut was one-sided

**What was wrong.** The recovery side of the order delta was discounted to 40%
while the loss side was charged in full. That is not conservatism; it is a thumb
on the scale. It manufactured roughly $157k of the apparent loss.

**Fix.** One constant, `lost_sale_share = 0.60`, applied in both directions. A
test asserts the *rate* applied is identical on both sides, so the asymmetry
cannot return.

### AD-11b — reversed: the impact table did not add up

**What was wrong.** Two terms (`margin_forgone`, `extra_waste_risk`) were already
negative, and the net subtracted them, silently adding them back. A reader
re-adding the published rows got a different total — which is how it was caught.

**Fix.** Every term carries its own sign; the net sums them. A test asserts the
components close on the published net.

---

## AD-12 — Markdown erosion comes from the till, not from the ticket

**Decision.** Erosion is measured from realised POS discount. Printed tickets are
used for *depth and timing only*.

**Why.** A printed ticket records an intention; the till records money. Roughly 9%
of ticketed units ring at full price because nobody stuck the sticker. Pricing
erosion off tickets would create a loss that never happened, and would hide the
compliance problem inside a pricing number.

**Enforcement.** `test_markdown_erosion_uses_realised_discount` asserts erosion
never *exceeds* what the tickets could have cost — the failure mode being someone
switching to pricing intention.

---

## AD-13 — Pack rounding rounds up

**Decision.** Order quantities round up to the supplier's order multiple.

**Why.** Ordering is discrete. Rounding down would silently deliver less than the
service level the economics asked for, and the error would be in the direction
that loses sales.

**Cost.** At most one pack per line, which is why the plan averages slightly above
the unconstrained frontier optimum (AD-11).

---

## AD-14 — Quarantine rows; never coerce them to zero

**Decision.** A row that cannot be typed is written to `ops.raw_quarantine` with
its raw text and a reason. It is never defaulted.

**Why.** A quarantined row is an operational signal. A zero is a lie that reads as
a fact — it flows into a total, and a total that is quietly missing a store's
sales looks like a quiet trading day.

**Known gap.** No simulated world produces an unparseable value, so the quarantine
path is implemented and reachable but **has no test**. A deliberately corrupt
fixture would close it. Recorded in `docs/RUNBOOK.md`.

---

## AD-15 — The ordering baseline is reconstructed, and labelled as such

**Decision.** The comparison plan rebuilds the estate's order-up-to habit from
delivery history rather than from recorded orders (the source does not carry
them), and every impact row states its basis.

**Why.** A recommendation with nothing to compare against is not actionable, and
a baseline nobody can inspect is not credible. The cost is that the baseline is a
model of the habit rather than the habit itself.

**Reverse.** If real order data became available it would replace this directly.

---

## AD-16 — One scope, passed to every stage

**Decision.** The Makefile passes `--stores`/`--days` to `world`, `ingest`,
`build`, `forecast`, `optimise` and `report`. `dbt` is never invoked directly; it
goes through `fresh_shelf.cli dbt`, which exports the project's clock and
warehouse file.

**Why.** A pipeline whose stages disagree about *which world* they are describing
produces numbers that are wrong rather than absent. This was a real bug, present
from the first commit, with three faces:

- `world` received the scope and the other stages did not, so the loader defaulted
  to all stores and loaded 2 files out of 1,444 — while every command printed
  success.
- A bare `dbt build` used `profiles.yml`'s default warehouse path, which could be
  a different database than `make ingest` had populated.
- Shrinking the world left the previous world's rows in `raw`, because the loader
  replaces only the (day, store) pairs a file covers. An eight-store estate
  followed by a two-store run held 7.5M units of sales against a ledger of 807k.

Every symptom was the same shape: **a number quietly too big, and no error.**

**Enforcement.** `ingest` prunes `raw` to the landed window and store set, scoped
so it cannot remove what a caller asked to keep (a windowed backfill skips the
prune entirely). Two tests pin that scope. CI builds the world *before* the tests
so nothing skips.

**Reverse.** Run stages with explicit flags; the CLI takes them individually. The
prune can be disabled with `prune=False` on the ingestor.

---

## AD-17 — The CI world is sized to cover the defects, not to be fast

**Decision.** `make world-small` defaults to **four stores (S01/S03/S05/S07) over
180 days** — the stores `config/defect_catalogue.yml` singles out — rather than the
smallest world that runs.

**Why.** Several dbt tests assert that a defect is *still being produced*, which is
what stops them passing vacuously. At two stores and 90 days those assertions had
nothing to assert against: there were **no waste events at all**, because the
stores carrying the substitution, pack-count and reason-code defects were not in
the world.

The failure was instructive. The tests failed for the *right reason* — the defect
was not being produced — but the *wrong cause*, which sent me looking at the
models rather than at the world's size.

**Cost.** CI is slower. The trade is deliberate: a fast job that cannot exercise
the loss pipeline is not testing the thing the project is about.

**Reverse.** `make world-small STORES=2 DAYS=90`. Expect three dbt tests to fail,
which is the signal that the world no longer covers the catalogue.

---

## AD-18 — A missing warehouse fails the tests; it does not skip them

**Decision.** The report test fixture calls `pytest.fail`, not `pytest.skip`, when
there is no warehouse. `make test-unit` asserts the fast tier skipped nothing.

**Why.** A skip reads as coverage that is not there. Five report tests would have
silently skipped on every fresh checkout while CI reported green, and the two
things they protect — that the charts render and that the caveats are stated —
are exactly the things that break quietly.

**Reverse.** Change `fail` back to `skip` and lose the signal. Not recommended.

**Decision.** The comparison plan rebuilds the estate's order-up-to habit from
delivery history rather than from recorded orders (the source does not carry
them), and every impact row states its basis.

**Why.** A recommendation with nothing to compare against is not actionable, and
a baseline nobody can inspect is not credible. The cost is that the baseline is a
model of the habit rather than the habit itself.

**Reverse.** If real order data became available it would replace this directly.
