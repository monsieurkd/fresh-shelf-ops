# Reviewing this project

If you have ten minutes, this is the path that shows the most in the least time.

## 1. See the result (30 seconds)

```bash
make setup
make world-small && make daily
open build/report/index.html
```

No credentials, no container, no network beyond the initial dependency install.
The report is a single file — no CDN, no build step.

## 2. Read the finding (2 minutes)

Open `build/report/report.md` instead. The headline is that **waste is 1.71% of
sales while margin is 41%**, so a unit not stocked costs roughly 24× a unit
wasted. That single ratio is why the optimiser's own accounting comes out
negative even though the frontier has an interior optimum — and why both numbers
are on the page rather than the flattering one.

## 3. The three files that carry the engineering

| File | Why it matters |
|---|---|
| `models/intermediate/int_stock_ledger.sql` | Every loss term stated separately, and the reasoning for not collapsing them |
| `models/intermediate/int_loss_reconciliation.sql` | Measures the loss no record explains, and publishes it as a *bound* with a confidence score |
| `src/fresh_shelf/ingest/__init__.py` | Where units, timezones and duplicates are fixed — exactly once |

## 4. The tests that would catch a regression

```bash
make test-unit     # 44 pure-logic tests, none of which may skip
make test          # all 70, including world generation
```

The two suites worth reading rather than just running:

- **`tests/test_boundaries.py`** — enforces the property the whole project rests
  on: the pipeline never reads its own answer key. It scans the source, and
  distinguishes writers from readers from prose.
- **`tests/test_ingest_defects.py`** — one test per catalogued defect. Each
  asserts *both* that the defect is still being produced and that the loader
  recovers the truth, so none of them can pass vacuously.

## 5. What a reviewer should be sceptical about

Listed here rather than buried, because a project that hides its gaps is not
trustworthy about anything else.

| Claim | The catch |
|---|---|
| "It handles messy data" | True, and *simulated*. The world is synthetic; the pipeline is not. |
| "Waste is measured" | It is a **floor**. A fifth of disposals are never scanned, and the gap is measured rather than assumed. |
| "Shrink is quantified" | It is a **bound**, dominated by ±3% count error. The published figure is confidence-weighted, not raw. |
| "The forecast is accurate" | WAPE 0.269 on **perfect future weather**. A real deployment would do worse. |
| "The plan pays for itself" | It does **not**, on this estate — and the report says so. |
| "The loader quarantines bad rows" | It does, and **no test exercises that path**, because no simulated world produces an unparseable value. This is the biggest real gap. |

## 6. Where the honest bugs are recorded

`docs/DECISIONS.md` has fifteen decisions with reversal paths. Two are marked
**reversed** — they were got wrong first, and the reasoning that failed is kept
because it is more instructive than the reasoning that worked:

- **AD-11a** — the impact haircut was applied at 40% on the recovery side while
  the loss side was charged in full. Not conservatism: a thumb on the scale that
  manufactured ~$157k of apparent loss.
- **AD-11b** — two already-negative terms were subtracted into the net, so the
  published components did not add up to the published total.

Both now have tests, because both are the kind of error that renders perfectly.

---

## If you want to change something

```bash
git config core.hooksPath .githooks   # optional fast tier
make check                            # lint + full tests
make idempotency                      # after any loader change
```

Before touching the loader, run `make idempotency`. A loader that appends instead
of replacing doubles every figure in the report, and nothing else notices.
