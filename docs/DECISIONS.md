# Decisions

Why the project is shaped this way. Each entry records what was chosen, what it
cost, and how it could be reversed. A decision that cannot be reversed is not a
decision.

Entries marked **reversed** were got wrong first and corrected. They are kept
because the failed reasoning is easier to learn from than the reasoning that
worked.

---

## AD-01: pin the clock in config

**Decision.** Every layer reads its as-of date from `config/project.yml`
(`clock.as_of_date: 2026-09-10`). Nothing reads the wall clock. `--as-of`
overrides it for a backfill.

**Why.** A pipeline that calls `date.today()` cannot be replayed. "The last 12
months" means something different every run, a backfill cannot be repeated, and
a reviewer cannot check a report figure against the data that produced it.

**Knock-on.** The world generator uses a `crc32` index instead of `hash()`,
because Python salts `hash()` per process and every store would get a different
local demand index on every run.

**Reverse.** Set `clock.as_of_date` in config. Nothing else changes.

---

## AD-02: catalogue every defect and name the test that proves it

**Decision.** `config/defect_catalogue.yml` describes all thirteen defects: what
the source does, which stores it affects, how the pipeline must handle it, and
the test that proves the handling. Tests read the catalogue and assert that every
entry names a test which exists.

**Why.** "We handle messy data" cannot be checked. A catalogue where each entry
points at a test can. It also catches a defect whose injection stops firing,
because several tests assert that the defect is still being produced.

**Cost.** The catalogue can drift from the code. Two things limit the damage:
the named tests must exist, and the defects are split into D01 to D06
(ingest-level) and D07 to D13 (warehouse-level) so each names a test that can
actually prove it.

---

## AD-03: normalise units, timezone and duplicates once

**Decision.** The loader converts cents to dollars, decimal commas to decimals,
UTC to Adelaide time, and de-duplicates by sha256. Nothing downstream repeats
any of it.

**Why.** These are the errors that do not raise. A cents file read as dollars is
a 100x error that renders perfectly. A UTC timestamp dated by its UTC date files
a 7pm disposal under the wrong trading day. One conversion site is testable; two
sites race each other.

**Enforcement.** `tests/test_ingest_defects.py`. D04, D05 and D06 each assert that
the raw layer recovers the sealed truth.

**Reverse.** Moving a conversion into dbt would break the test suite, which is
built around the current boundary.

---

## AD-04: partition landing files by arrival date

**Decision.** Files live at `data/landing/<source>/<landing_date>/`, where
`landing_date` is when the file appeared, not what it contains.

**Why.** This is what makes D02 a real problem. If the folder named the business
date, a late file would be trivial. Because it does not, the loader has to read
the file, which is what a real pipeline has to do.

**Reverse.** Change `landing_day_dir()` in `config.py`. The loader already
ignores folder names when reading business dates.

---

## AD-05: the warehouse never reads the answer key

**Decision.** `data/truth/` holds the sealed answer key. dbt reads `raw` and
`seeds` only.

**Why.** A warehouse that validates itself against a file only it can see is
measuring nothing. It is easy to violate by accident. The trading calendar once
existed only in the truth directory, so `stg_trading_calendar` would have had to
read it. It now lands as a normal source feed with its own loader.

**Enforcement.** `tests/test_boundaries.py` scans the source and fails on any read
of a `truth_*` table or of parquet from the truth directory. It distinguishes
writers (legal), docstrings (prose) and `landing_gaps.csv` (pipeline metadata a
real system would know).

**Reverse.** Do not. If dbt needs truth-only data, land it as a source, as was
done for the calendar.

---

## AD-06: recorded waste is a floor

**Decision.** Loss scans are a lower bound. `int_loss_reconciliation` measures the
residue instead of trusting the scan file.

**Why.** About a fifth of disposals are never scanned, and compliance varies by
store. Quoting the scan total as "the waste" understates it and hides the
operational problem behind the shortfall.

**Cost.** The residue absorbs three things: genuine unscanned loss, count error
and booking mistakes. Count error dominates on this estate, so the residue
overstates unscanned loss several-fold. AD-07 handles that.

---

## AD-07: publish the residue as a bound

**Decision.** `int_loss_reconciliation` emits `shrink_confidence` per segment.
The report quotes `weighted_shrink_cost` and shows the raw residue beside it.

**Why.** A count eyeballed to plus or minus 3% on a book that moves about 1,200
units per segment cannot resolve a 1% loss. Measured against the simulator's own
records, the residue runs about eight times the true unscanned loss. Presenting
it as a measurement would weaken every other number by association.

**Reverse.** Drop the weighting. The raw column is still there, and the figure
would then overstate shrink by roughly eightfold.

---

## AD-08: keep the three losses apart

**Decision.** Markdown erosion (margin), recorded waste (cost) and unrecorded
shrink (cost) are separate columns and separate report sections. They are never
totalled.

**Why.** They have different fixes. A single loss number averages a pricing
decision, a buying decision and a process problem into something nobody can act
on. `tests/test_report.py` checks that the page never introduces a total.

**Reverse.** Trivial, but it would make the report worse.

---

## AD-09: measure the forecast's calibration

**Decision.** The backtest publishes `forecast_calibration`, which records the
coverage each nominal quantile actually achieved. The optimiser consumes the
achieved level.

**Why.** The q0.95 forecast covers about 0.87 of outcomes on this estate. A plan
that sized orders to the nominal figure, believing it was a 95% service level,
would under-order, lose sales, and never see the error, because the mistake sits
in the safe-looking direction.

**Reverse.** Ignore the calibration table. The plan then runs at whatever the
model actually delivers.

---

## AD-10: select the forecast on quantile loss

**Decision.** The forward forecast uses the model with the best mean pinball loss
at the top quantile, not the best WAPE.

**Why.** The optimiser consumes a quantile. A model with a slightly worse mean
but well-calibrated tails is more useful, and selecting on WAPE would select
against exactly that. On this estate the two rules agree (`hgb_quantile` wins
both), but the distinction matters when they do not.

---

## AD-11: publish the disagreement between the two methods

**Decision.** `impact_reconciliation` reports whether the frontier and the
dollar-impact accounting agree. On this estate they do not, and both are shown.

**Why.** The two answer the same question from different directions. The frontier
sweeps an order ratio and prices waste against lost margin, giving an interior
optimum at a service level of about 0.41. The impact function differences two
concrete order plans and comes out negative.

The cause is structural. Waste is 1.71% of sales and margin is 41%, so a unit not
stocked costs about 24 times a unit wasted. Ordering down cannot pay for itself
on margin alone. The recommended plan also averages a service level above the
unconstrained optimum, because pack sizes and shelf-life caps make it impossible
to order three-quarters of a tray.

**Consequence.** The defensible claims are markdown timing and the lines whose
cover exceeds their shelf life, not a blanket reduction in ordering. Quoting a
benefit while the frontier points the other way would be misleading.

**Reverse.** `methods_agree_on_direction` flips to 1 if the accounting becomes
positive, and the note changes with it.

### AD-11a (reversed): the impact haircut was one-sided

**What was wrong.** The recovery side of the order delta was discounted to 40%
while the loss side was charged in full. That is not conservatism. It is a thumb
on the scale, and it manufactured about $157k of the apparent loss.

**Fix.** One constant, `lost_sale_share = 0.60`, applied in both directions. A
test asserts that the rate is identical on both sides.

### AD-11b (reversed): the impact table did not add up

**What was wrong.** Two terms (`margin_forgone`, `extra_waste_risk`) were already
negative, and the net subtracted them, silently adding them back. A reader who
re-added the published rows got a different total, which is how it was caught.

**Fix.** Every term carries its own sign and the net sums them. A test asserts
that the components close on the published net.

---

## AD-12: markdown erosion comes from the till

**Decision.** Erosion is measured from the realised POS discount. Printed
tickets are used for depth and timing only.

**Why.** A ticket records an intention; the till records money. About 9% of
ticketed units ring at full price because nobody applied the sticker. Pricing
erosion from tickets would invent a loss that never happened and would hide the
compliance problem inside a pricing number.

**Enforcement.** `test_markdown_erosion_uses_realised_discount` asserts that
erosion never exceeds what the tickets could have cost.

---

## AD-13: pack rounding rounds up

**Decision.** Order quantities round up to the supplier's order multiple.

**Why.** Ordering is discrete. Rounding down would silently deliver less than the
service level the economics asked for, and the error would lose sales.

**Cost.** At most one pack per line, which is why the plan averages slightly above
the unconstrained frontier optimum (AD-11).

---

## AD-14: quarantine bad rows, never coerce them

**Decision.** A row that cannot be typed is written to `ops.raw_quarantine` with
its raw text and a reason. It is never defaulted.

**Why.** A quarantined row is an operational signal. A zero is a lie that reads
as a fact: it flows into a total, and a total missing a store's sales looks like
a quiet trading day.

**Known gap.** No simulated world produces an unparseable value, so the
quarantine path is implemented and reachable but untested. A deliberately corrupt
fixture would close it. Also recorded in `docs/RUNBOOK.md`.

---

## AD-15: the ordering baseline is reconstructed

**Decision.** The comparison plan rebuilds the estate's order-up-to habit from
delivery history, because the source does not carry placed orders. Every impact
row states its basis.

**Why.** A recommendation with nothing to compare against is not actionable, and
a baseline nobody can inspect is not credible. The cost is that the baseline
models the habit instead of recording it.

**Reverse.** If real order data becomes available, it replaces this directly.

---

## AD-16: one scope, passed to every stage

**Decision.** The Makefile passes `--stores` and `--days` to `world`, `ingest`,
`build`, `forecast`, `optimise` and `report`. dbt is never invoked directly. It
goes through `fresh_shelf.cli dbt`, which exports the project's clock and
warehouse file.

**Why.** Stages that disagree about which world they are describing produce
numbers that are wrong rather than absent. This was a real bug from the first
commit, with three faces:

- `world` received the scope and the other stages did not. The loader defaulted
  to all stores and loaded 2 files out of 1,444 while every command printed
  success.
- A bare `dbt build` used the default warehouse path from `profiles.yml`, which
  could be a different database from the one `make ingest` had populated.
- Shrinking the world left the previous world's rows in `raw`, because the loader
  replaces only the day and store pairs a file covers. An eight-store estate
  followed by a two-store run held 7.5M units of sales against a ledger of 807k.

Every symptom had the same shape: a number quietly too big, and no error.

**Enforcement.** `ingest` prunes `raw` to the landed window and store set, scoped
so it cannot remove what a caller asked to keep. A windowed backfill skips the
prune entirely. Two tests pin that scope. CI builds the world before the tests,
so nothing skips.

**Reverse.** Run stages with explicit flags; the CLI accepts them individually.
The prune can be disabled with `prune=False` on the ingestor.

---

## AD-17: size the CI world to cover the defects

**Decision.** `make world-small` defaults to four stores (S01, S03, S05 and S07)
over 180 days, rather than the smallest world that runs.

**Why.** Several dbt tests assert that a defect is still being produced, which is
what stops them passing vacuously. At two stores and 90 days those assertions had
nothing to check. There were no waste events at all, because the stores carrying
the substitution, pack-count and reason-code defects were not in the world.

The failure was misleading. The tests failed for the right reason (the defect was
not produced) but for the wrong cause, which sent me looking at the models
instead of the world's size.

**Cost.** CI is slower. That is the trade: a fast job that cannot exercise the
loss pipeline is not testing the thing the project is about.

**Reverse.** `make world-small STORES=2 DAYS=90`. Expect three dbt tests to fail,
which is the signal that the world no longer covers the catalogue.

---

## AD-18: a missing warehouse fails the tests

**Decision.** The report test fixture calls `pytest.fail`, not `pytest.skip`, when
there is no warehouse. `make test-unit` asserts that the fast tier skipped
nothing.

**Why.** A skip reads as coverage that is not there. Five report tests would have
skipped silently on every fresh checkout while CI reported green, and the things
they protect are exactly the things that break quietly: that the charts render
and that the caveats are stated.

**Reverse.** Change `fail` back to `skip` and lose the signal.
