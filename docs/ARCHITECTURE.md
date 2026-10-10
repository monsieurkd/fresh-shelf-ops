# Architecture

How the pieces fit, and — more usefully — where the seams are and why they were
put there.

## The layers

```
simulated sources  ->  landing zone  ->  raw  ->  dbt  ->  decision layer  ->  report
     (Python)           (files)      (Python)  (SQL)       (Python)          (static)
```

Five boundaries, each with one job:

| Boundary | Contract | Enforced by |
|---|---|---|
| sources → landing | Each feed lands as the file a real system would write: one per store-day, named by the writer, in a folder named by *arrival* | `world/`, `landing/writer.py` |
| landing → raw | After loading, every value is in dollars, in eaches, in Adelaide time, de-duplicated, and traceable to its file | `tests/test_ingest_defects.py` |
| raw → dbt | The warehouse never reads `data/truth` | `test_the_warehouse_never_reads_the_truth` |
| dbt → decision layer | The models publish one forecast-eligible population that every consumer uses | `dbt_tests/`, `features.py` |
| decision layer → report | No number on the page is entered by hand | `tests/test_report.py` |

## Why the warehouse is one DuckDB file

A file, not a server. The whole project has to be reviewable by someone who has
cloned it and run `make setup`: no container, no credentials, no network. DuckDB
gives columnar performance on a laptop and lets the report's every figure be
traced to a query a reader can run themselves.

The cost is a single writer at a time, so the pipeline is a *sequence*, not a
swarm — which is also what makes `make daily` safe to re-run and the lock
behaviour obvious rather than surprising.

## The stock ledger is the spine

`int_stock_ledger` is one row per store, item and trading day, and every loss
number the report quotes is a term in it. It is built this way because both
obvious shortcuts are wrong:

- **"Waste = what the scan file says" is wrong.** Scans are filed when a handheld
  is to hand (D07), so the scan total is a floor. `int_loss_reconciliation`
  measures the gap rather than assuming it away.
- **"Sales + waste = what we bought" is wrong.** It ignores preparation loss, a
  rejected delivery and counting error, and turns ordinary operational noise into
  a fictitious shrinkage scandal.

So the ledger states each movement separately and reconciles against physical
counts:

```
opening + received − sold − prep loss − recorded waste = expected closing
expected closing vs the last count                     = the residue
```

The residue is where unrecorded shrink lives. It is reported, not hidden, and it
carries a confidence score — because a count eyeballed to ±3% cannot resolve a 1%
loss, and saying otherwise would present count noise as a finding.

## Why the loss concepts are never summed

Three losses, three fixes, so they stay separate all the way to the page:

| Concept | Valued at | Fixed by |
|---|---|---|
| **Markdown erosion** | the margin given away | marking down later and shallower |
| **Recorded waste** | the cost of the stock | ordering less, or more often |
| **Unrecorded shrink** | the cost of the stock | a process problem, not a buying one |

A single "total loss" figure would average three different conversations into a
number nobody can act on. `tests/test_report.py` asserts the page never
introduces one.

## The decision layer

```
marts.fct_store_item_day
        |
        v
  features (strictly trailing windows)
        |
        v
  rolling-origin backtest -----> calibration (what q0.95 REALLY covers)
        |                                    |
        v                                    v
  forward forecast  ------------>  replenishment plan (newsvendor, capped by shelf life)
        |                                    |
        v                                    v
  markdown plan (DP over stock x days left)  waste-vs-revenue frontier
        |                                    |
        +---------------> dollar impact <----+
                              |
                              v
                   reconciliation (do the two methods agree?)
```

Three design rules, each of which has a corresponding test:

1. **Nothing looks at the future.** Feature windows end at `1 preceding`; the
   backtest refits at each cut-off and scores only the horizon after it.
2. **The model's weakness is measured, not assumed.** q0.95 delivers ~0.87
   coverage on this estate, so the optimiser is told the *achieved* level rather
   than the nominal one.
3. **Two methods, and their disagreement is published.** The frontier and the
   dollar impact answer the same question from different directions. When they
   disagree, that is the most informative thing on the page, so
   `impact_reconciliation` exists to say so.

## Testing shape

| Suite | Proves |
|---|---|
| `test_world_generator.py` | The simulated world is self-consistent: deterministic, non-negative, ledger closure |
| `test_ingest_defects.py` | The loader recovers the sealed truth from dirty files, one test per defect |
| `dbt_tests/` | The warehouse's business meaning, one singular test per defect D07–D13 |
| `test_decision_layer.py` | The decisions are right given the data — including that the impact table adds up |
| `test_report.py` | The report is self-contained, well-formed, and does not overstate |

The split matters. A world test cannot prove a redelivery was de-duplicated, and
an ingest test cannot prove the ledger reconciles.

## Known limitations

- **Quarantine is implemented but unexercised.** `ops.raw_quarantine` is reachable
  and every loader can populate it, but no simulated world produces an
  unparseable value, so the branch has no test. A deliberately corrupt fixture
  file would close this.
- **The warehouse is rebuilt, not incrementally maintained.** `dbt build` runs
  the full DAG in ~12s at this scale, so incremental models would be complexity
  for no benefit. At ten times the estate they would be the first thing to add.
- **Weather is a perfect forecast.** Stated in the report; the fix would be to
  introduce forecast error into the simulation.
