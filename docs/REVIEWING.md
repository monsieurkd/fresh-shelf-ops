# Reviewing this project

A ten-minute path through the result, the code and the claims worth checking.

## 1. See the result (30 seconds)

```bash
make setup
make world-small && make daily
open build/report/index.html
```

No credentials, no container, no network beyond the first dependency install.
The report is one file with no CDN and no build step.

## 2. Read the finding (2 minutes)

Open `build/report/report.md` instead. Waste is 1.71% of sales and margin is 41%,
so a unit not stocked costs about 24 times a unit wasted. That ratio explains why
the optimiser's accounting comes out negative even though the frontier has an
interior optimum. Both numbers are on the page.

## 3. Three files that carry the engineering

| File | Why it matters |
|---|---|
| `models/intermediate/int_stock_ledger.sql` | Every loss term stated separately, with the reasoning for not collapsing them |
| `models/intermediate/int_loss_reconciliation.sql` | Measures the loss no record explains and publishes it as a bound with a confidence score |
| `src/fresh_shelf/ingest/__init__.py` | Where units, timezones and duplicates are fixed, once |

## 4. Tests that would catch a regression

```bash
make test-unit     # 44 pure-logic tests, none may skip
make test          # all 72, including world generation
```

Two suites are worth reading:

- `tests/test_boundaries.py` enforces the property the project rests on: the
  pipeline never reads its own answer key. It scans the source and separates
  writers from readers from prose.
- `tests/test_ingest_defects.py` has one test per catalogued defect. Each asserts
  that the defect is still being produced and that the loader recovers the
  truth, so none can pass vacuously.

## 5. Claims worth checking

| Claim | The catch |
|---|---|
| "It handles messy data" | True, and simulated. The world is synthetic; the pipeline is not |
| "Waste is measured" | It is a floor. A fifth of disposals are never scanned, and the gap is measured rather than assumed |
| "Shrink is quantified" | It is a bound, dominated by plus or minus 3% count error. The published figure is confidence-weighted, not raw |
| "The forecast is accurate" | WAPE 0.269 on perfect future weather. A real deployment would do worse |
| "The plan pays for itself" | It does not on this estate, and the report says so |
| "The loader quarantines bad rows" | It does, and no test exercises that path, because no simulated world produces an unparseable value. This is the largest real gap |

## 6. Where the bugs are recorded

`docs/DECISIONS.md` has eighteen decisions with reversal paths. Two are marked
reversed. They were got wrong first, and the failed reasoning is kept because it
is more useful than the reasoning that worked:

- AD-11a: the impact haircut was applied at 40% on the recovery side while the
  loss side was charged in full. That manufactured about $157k of apparent loss.
- AD-11b: two already-negative terms were subtracted into the net, so the
  published components did not add up to the published total.

Both now have tests, because both are the kind of error that renders perfectly.

AD-16 is the one to read if you read only one. Three bugs in this project,
including a six-figure one, were the same mistake: two pipeline stages
disagreeing about which world they described. Every symptom was a number quietly
too big, and no error.

---

## If you want to change something

```bash
git config core.hooksPath .githooks   # optional fast tier
make check                            # lint and full tests
make idempotency                      # after any loader change
```

Run `make idempotency` before touching the loader. A loader that appends instead
of replacing doubles every figure in the report, and nothing else notices.
