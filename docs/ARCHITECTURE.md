# Architecture

How the pieces fit, where the seams are, and why they sit there.

## Layers

```
simulated sources  ->  landing zone  ->  raw  ->  dbt  ->  decision layer  ->  report
     (Python)           (files)      (Python)  (SQL)       (Python)          (static)
```

Five boundaries, each with one job:

| Boundary | Contract | Enforced by |
|---|---|---|
| sources to landing | Each feed lands as the file a real system would write: one per store-day, named by the writer, in a folder named by arrival date | `world/`, `landing/writer.py` |
| landing to raw | Every value is in dollars, in eaches, in Adelaide time, de-duplicated and traceable to its file | `tests/test_ingest_defects.py` |
| raw to dbt | The warehouse never reads `data/truth` | `test_the_warehouse_never_reads_the_truth` |
| dbt to decision layer | The models publish one forecast-eligible population that every consumer uses | `dbt_tests/`, `features.py` |
| decision layer to report | No number on the page is entered by hand | `tests/test_report.py` |

## One DuckDB file

The warehouse is a file, not a server. Someone who clones the repo can run
`make setup` and get the whole thing: no container, no credentials, no network.
DuckDB is fast enough on a laptop, and every figure in the report can be traced
to a query a reader can run.

The cost is a single writer, so the pipeline runs as a sequence rather than in
parallel. That also keeps `make daily` safe to re-run.

## The stock ledger

`int_stock_ledger` has one row per store, item and trading day. Every loss
figure in the report is a term in it. The two obvious shortcuts are both wrong:

- Trusting the scan file. Scans only happen when a handheld is to hand (D07), so
  the scan total is a floor. `int_loss_reconciliation` measures the gap instead
  of assuming it away.
- Treating sales plus waste as purchases. That ignores preparation loss,
  rejected deliveries and counting error, and turns ordinary operational noise
  into a fictitious shrinkage scandal.

The ledger states each movement separately and reconciles against physical
counts:

```
opening + received - sold - prep loss - recorded waste = expected closing
expected closing vs the last count                     = the residue
```

The residue is where unrecorded shrink lives. It is reported with a confidence
score, because a count eyeballed to plus or minus 3% cannot resolve a 1% loss.

## Three losses, three fixes

The loss concepts stay separate all the way to the page:

| Concept | Valued at | Fixed by |
|---|---|---|
| Markdown erosion | the margin given away | marking down later and shallower |
| Recorded waste | the cost of the stock | ordering less, or more often |
| Unrecorded shrink | the cost of the stock | a process problem, not a buying one |

A single total loss figure would average three different conversations into a
number nobody can act on. `tests/test_report.py` checks that the page does not
introduce one.

## Decision layer

```
marts.fct_store_item_day
        |
        v
  features (trailing windows)
        |
        v
  rolling-origin backtest -----> calibration (what q0.95 actually covers)
        |                                    |
        v                                    v
  forward forecast  ------------>  replenishment plan (newsvendor, capped by shelf life)
        |                                    |
        v                                    v
  markdown plan (DP over stock x days left)  waste and revenue frontier
        |                                    |
        +---------------> dollar impact <----+
                              |
                              v
                   reconciliation (do the two methods agree?)
```

Three rules, each with a test:

1. Nothing looks at the future. Feature windows end at one preceding day; the
   backtest refits at each cut-off and scores only the horizon after it.
2. The model's weakness is measured. q0.95 delivers about 0.87 coverage on this
   estate, so the optimiser uses the achieved level rather than the nominal one.
3. Two methods are published, including their disagreement. The frontier and the
   dollar impact answer the same question from different directions, and
   `impact_reconciliation` reports where they diverge.

## Tests

| Suite | Proves |
|---|---|
| `test_world_generator.py` | The simulated world is deterministic, non-negative, and its ledger closes |
| `test_ingest_defects.py` | The loader recovers the sealed truth from dirty files, one test per defect |
| `dbt_tests/` | The warehouse's business meaning, one singular test per defect D07 to D13 |
| `test_decision_layer.py` | The decisions are right given the data, including that the impact table adds up |
| `test_report.py` | The report is self-contained, well-formed, and does not overstate |

The split matters. A world test cannot prove a redelivery was de-duplicated, and
an ingest test cannot prove the ledger reconciles.

## Known limitations

- Quarantine is implemented but not exercised. `ops.raw_quarantine` is reachable
  and every loader can write to it, but no simulated world produces an
  unparseable value, so the branch has no test. A deliberately corrupt fixture
  would close it.
- The warehouse is rebuilt, not maintained incrementally. `dbt build` takes about
  12 seconds at this scale. At ten times the estate, incremental models would be
  the first change to make.
- Weather is a perfect forecast. The simulation gives the model exact future
  temperatures. The fix would be to add forecast error.
