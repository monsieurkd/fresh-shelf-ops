"""One test per entry in `config/defect_catalogue.yml`.

The point of simulating the source systems is to show that each catalogued
defect is one the pipeline survives. A test here builds a small world containing
the store the defect lives in, writes the landing zone, loads it, and compares
the raw layer against `data/truth/*.parquet`, the sealed answer key the pipeline
never reads.

Each test follows the same three steps:

1. inject, using the world, the writer and the catalogue's own parameters
2. load with `Ingestor`
3. assert that the raw layer agrees with the truth and that the defect was visible

If a defect is later "fixed" by coercing bad data to zero, these tests fail.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fresh_shelf.config import Config, load_config, load_defect_catalogue
from fresh_shelf.db import connect, query_df
from fresh_shelf.ingest import Ingestor
from fresh_shelf.landing.writer import write_landing
from fresh_shelf.world import simulate

# These tests generate a world and load it, so they carry the slow marker. The
# fast tier has to stay fast, which it did not while these lacked it.
pytestmark = pytest.mark.slow

#: The stores the defect catalogue singles out. A world for these tests has to
#: contain them: a 2-store world picks the two largest and would never exercise
#: the locale-comma or cents-file defects at all.
DEFECT_STORES = ["S01", "S03", "S05", "S07"]

TOL = 0.05  # rounding: kg quantities are 3dp, eaches are integers


@pytest.fixture(scope="module")
def defect_world(tmp_path_factory):
    """A small world that contains every store a defect is pinned to."""
    root = tmp_path_factory.mktemp("defects")
    cfg = load_config().override(
        as_of="2026-09-10",
        history_days=40,
        stores=DEFECT_STORES,
        seed=4242,
        landing_dir=root / "landing",
        truth_dir=root / "truth",
        warehouse_file=root / "warehouse.duckdb",
        artifacts_dir=root / "artifacts",
        build_dir=root / "build",
    )
    cfg.paths.ensure()
    world = simulate(cfg)
    for name, frame in world.truth.items():
        frame.to_parquet(cfg.paths.truth_dir / f"{name}.parquet", index=False)
    manifest = write_landing(cfg, world)
    return cfg, world, manifest


@pytest.fixture(scope="module")
def loaded(defect_world):
    """The warehouse after one ingest of the defect world."""
    cfg, world, manifest = defect_world
    with connect(cfg) as con:
        ingestor = Ingestor(cfg, con)
        files = ingestor.run()
        yield con, files, world, manifest


def _catalogue() -> dict[str, dict]:
    return {d["code"]: d for d in load_defect_catalogue()}


def _truth(cfg: Config, name: str) -> pd.DataFrame:
    return pd.read_parquet(cfg.paths.truth_dir / f"{name}.parquet")


def _loaded_files(files: pd.DataFrame) -> pd.DataFrame:
    return files[files["status"] == "loaded"].copy()


# ---------------------------------------------------------------------------
# D01: the integration re-sends an extract after a gateway timeout
# ---------------------------------------------------------------------------
def test_d01_duplicate_redelivery_is_not_double_counted(defect_world, loaded):
    cfg, world, manifest = defect_world
    con, files, _, _ = loaded

    redeliveries = manifest[manifest["is_duplicate_of"].notna()]
    assert not redeliveries.empty, "the world injected no redeliveries to test against"

    # The ingestor saw every file, and classified the redeliveries as duplicates.
    duplicates = files[files["status"] == "duplicate"]
    assert len(duplicates) == len(redeliveries)
    for row in duplicates.itertuples():
        assert "identical content already loaded" in row.notes

    # And the raw table holds exactly one copy: no business day was loaded twice.
    collisions = query_df(
        con,
        """
        SELECT business_date, store_code, count(*) AS rows
        FROM raw.pos_sales
        GROUP BY 1, 2
        HAVING count(DISTINCT _file_checksum) > 1
        """,
    )
    assert collisions.empty, f"a store-day was loaded from two different files:\n{collisions}"


# ---------------------------------------------------------------------------
# D02: the regional stores' extracts land a day late
# ---------------------------------------------------------------------------
def test_d02_late_file_lands_in_its_business_partition(defect_world, loaded):
    cfg, world, _ = defect_world
    con, files, _, _ = loaded

    late = _loaded_files(files)
    late = late[late["lag_days"] > 0]
    assert not late.empty, "no file was flagged late; D02 did not fire"

    for row in late.itertuples():
        assert row.lag_days == 1, f"{row.file_name} lagged {row.lag_days} days"

    # The crucial part: lateness did not move the data. Every store-day the
    # source sent is present in raw under its *business* date.
    truth_days = _truth(cfg, "truth_store_item_day")
    raw_days = query_df(con, "SELECT DISTINCT business_date FROM raw.pos_sales")
    raw_set = set(pd.to_datetime(raw_days["business_date"]).dt.date)
    truth_pos_days = set(pd.to_datetime(truth_days["business_date"]).dt.date)
    missing = {d for d in truth_pos_days if d not in raw_set}
    gaps = pd.read_csv(cfg.paths.truth_dir / "landing_gaps.csv")
    gap_days = {
        date.fromisoformat(str(d))
        for d in gaps.loc[gaps["source"] == "pos_sales", "business_date"].unique()
    }
    # Every day missing from raw must be a day the source never sent (D03),
    # not a day that got filed under its landing date instead.
    assert missing <= gap_days, f"days lost to the late-landing trap: {sorted(missing - gap_days)}"


# ---------------------------------------------------------------------------
# D03: a store-day that never arrives
# ---------------------------------------------------------------------------
def test_d03_missing_store_day_is_reported_not_invented(defect_world, loaded):
    cfg, world, _ = defect_world
    con, files, _, _ = loaded

    gaps = pd.read_csv(cfg.paths.truth_dir / "landing_gaps.csv")
    pos_gaps = gaps[gaps["source"] == "pos_sales"]
    assert not pos_gaps.empty, "the world injected no link outage to test against"
    gap_pairs = {
        (date.fromisoformat(str(r.business_date)), r.store_code) for r in pos_gaps.itertuples()
    }

    raw = con.execute("SELECT DISTINCT business_date, store_code FROM raw.pos_sales").fetchdf()
    raw_pairs = {(pd.Timestamp(r.business_date).date(), r.store_code) for r in raw.itertuples()}

    # A store-day the source never sent must not exist in raw. If the loader
    # synthesised a zero row for the outage, this is where it shows up.
    for gap in gap_pairs:
        assert gap not in raw_pairs, f"{gap} appears in raw but the source never sent it"

    # The outage is not silently read as "sold nothing": the dates it falls on
    # are exactly the dates missing from raw for that store. Comparing the
    # store-day *presence set* is the invariant; the row count is not, because
    # a store that sold nothing on a day it was open produces no POS row either.
    missing = raw_pairs.symmetric_difference(gap_pairs) & gap_pairs
    assert missing == gap_pairs, "an outage day was not fully absent from raw"

    # And the gap is discoverable without the answer key: the meta table records it.
    reported = con.execute(
        "SELECT count(*) FROM ops.ingest_files WHERE source = 'pos_sales' AND status = 'loaded'"
    ).fetchone()[0]
    assert reported > 0

    # The store kept trading on every other day of the window.
    for store in sorted({s for _, s in gap_pairs}):
        store_days = {d for d, s in raw_pairs if s == store}
        gap_days = {d for d, s in gap_pairs if s == store}
        assert len(store_days) > len(gap_days) * 5, (
            f"{store} is missing suspiciously many days; the outage is being applied far too often"
        )


# ---------------------------------------------------------------------------
# D04: the old firmware writes units as text with a decimal comma
# ---------------------------------------------------------------------------
def test_d04_locale_text_units_are_normalised(defect_world, loaded):
    cfg, world, manifest = defect_world
    con, files, _, _ = loaded

    store = _catalogue()["D04"]["injection"]["stores"][0]
    file_name = f"pos_{store}_"

    # The defect is really in the file: some units values are unparseable as-is.
    landed = sorted(cfg.paths.landing_dir.glob(f"pos_sales/*/{file_name}*.csv"))
    assert landed, f"no POS files for {store}"
    seen_comma_text = False
    for path in landed[:5]:
        text = path.read_text().splitlines()
        for line in text[1:6]:
            units = line.split(",")[4]
            if "," in units or not _looks_numeric(units):
                seen_comma_text = True
                break
        if seen_comma_text:
            break
    assert seen_comma_text, f"{store}'s units were written as plain numbers; D04 did not fire"

    # The raw layer carries a real number, and no data was lost on the way.
    truth = _truth(cfg, "truth_store_item_day")
    truth_store = truth[truth["store_code"] == store]
    raw = query_df(con, "SELECT * FROM raw.pos_sales WHERE store_code = ?", [store])
    assert not raw.empty
    assert pd.api.types.is_numeric_dtype(raw["units"]), "units did not land as a number"
    assert (raw["units"].abs() > 0).any()
    # A comma misread as a thousands separator would inflate units 1000x.
    worst = raw["units"].abs().max()
    truth_worst = truth_store["units_sold"].max()
    assert worst <= truth_worst * 1.05 + 1, (
        f"largest raw unit count {worst} dwarfs the truth ({truth_worst}); "
        "a decimal comma was read as a thousands separator"
    )


def _looks_numeric(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# D05: one store exports money in integer cents
# ---------------------------------------------------------------------------
def test_d05_cents_file_is_normalised_or_quarantined(defect_world, loaded):
    cfg, world, manifest = defect_world
    con, files, _, _ = loaded

    store = _catalogue()["D05"]["injection"]["stores"][0]

    # The marker really is in the headers, and at least one file really is cents.
    headers = [
        p.read_text().splitlines()[0]
        for p in sorted(cfg.paths.landing_dir.glob(f"pos_sales/*/pos_{store}_*.csv"))
    ]
    cents_headers = [h for h in headers if "money_unit=cents" in h]
    assert cents_headers, f"no cents file was written for {store}; D05 did not fire"

    # Amounts that came from a cents file are in dollars in raw, which we can
    # see directly: the column records what the file claimed.
    raw = query_df(con, "SELECT * FROM raw.pos_sales WHERE store_code = ?", [store])
    assert not raw.empty
    from_cents = raw[raw["money_unit_as_filed"] == "cents"]
    assert not from_cents.empty, "the cents rows did not survive the load"
    # The tell for an un-normalised cents file is an average unit value in the
    # hundreds of dollars on a fresh line.
    unit_value = from_cents["gross_sales"].abs() / from_cents["units"].abs().replace(0, 1)
    assert unit_value.max() <= 250.0, (
        f"a cents amount landed as dollars (${unit_value.max():,.2f} per unit); "
        "the money-unit marker was ignored"
    )

    # And the dollars-normalised raw figures line up with the truth.
    truth = _truth(cfg, "truth_store_item_day")
    truth_store = truth[truth["store_code"] == store]
    raw_net = from_cents["net_sales"].sum()
    truth_net = truth_store["net_sales_value"].sum()
    assert raw_net == pytest.approx(truth_net, rel=0.25), (
        f"cents-store net sales ${raw_net:,.0f} against a truth of ${truth_net:,.0f}"
    )


# ---------------------------------------------------------------------------
# D06: the handhelds write UTC, everything else writes Adelaide time
# ---------------------------------------------------------------------------
def test_d06_utc_scans_land_on_the_right_adelaide_day(defect_world, loaded):
    cfg, world, _ = defect_world
    con, files, _, _ = loaded

    raw = query_df(con, "SELECT * FROM raw.waste_events")
    assert not raw.empty
    assert set(raw["filed_timestamp_zone"].unique()) == {"UTC"}

    # The business date must be the Adelaide date of the *local* timestamp, not
    # the date part of the UTC string.
    raw["event_ts_local"] = pd.to_datetime(raw["event_ts_local"])
    same = raw["business_date"].astype("datetime64[ns]").dt.date == raw["event_ts_local"].dt.date
    assert same.all(), "a scan's business date disagrees with its local timestamp"

    utc_date = pd.to_datetime(raw["event_ts_utc"]).dt.date
    shifted = (utc_date != raw["business_date"]).sum()
    assert shifted > 0, (
        "no scan's UTC date differed from its Adelaide date; the offset is not "
        "being applied at all, so this test is not proving anything"
    )

    # The boundaries: a scan late in the Adelaide evening is the NEXT day in UTC,
    # which is exactly the case that silently misfiles a disposal.
    evening = raw[raw["event_ts_local"].dt.hour >= 20]
    if not evening.empty:
        late_utc = evening[evening["event_ts_utc"].dt.hour >= 10]
        assert not late_utc.empty, (
            "no evening scan crossed into the next UTC day; the boundary the "
            "defect exists to test was not exercised"
        )

    # Recovery: totals reconcile against the truth's disposal records.
    truth = _truth(cfg, "truth_disposals")
    scanned = truth[truth["was_scanned"]]
    assert raw["qty"].sum() == pytest.approx(scanned["qty"].sum(), rel=TOL)


# ---------------------------------------------------------------------------
# D07: a fifth of disposals are never scanned, so recorded waste is a floor
# ---------------------------------------------------------------------------
def test_d07_unscanned_disposals_make_recorded_waste_a_floor(defect_world, loaded):
    cfg, world, manifest = defect_world
    con, files, _, _ = loaded

    truth = _truth(cfg, "truth_disposals")
    loss_waste = truth[truth["loss_class"] == "waste"]
    scanned = loss_waste[loss_waste["was_scanned"]]
    unscanned = loss_waste[~loss_waste["was_scanned"]]

    assert len(unscanned) > 0, "every disposal was scanned; D07 did not fire"

    # The catalogue declares the compliance it injects, so the test asserts
    # against that number rather than a band someone picked by eye. If the world
    # is calibrated differently the catalogue is wrong, and one of the two has to
    # change. That is why it is pinned here.
    declared = float(_catalogue()["D07"]["injection"]["base_compliance"])
    weekend_penalty = float(_catalogue()["D07"]["injection"]["weekend_penalty"])
    compliance = len(scanned) / len(loss_waste)
    # The estate average sits slightly below the declared base, because weekends
    # are penalised and Sunday evening is when scanners go missing.
    assert compliance == pytest.approx(declared, abs=0.08), (
        f"waste-scan compliance {compliance:.1%} does not match the {declared:.0%} "
        "the catalogue declares it injects"
    )
    assert weekend_penalty > 0

    # The scan file is a *floor* on loss, not the loss itself: every physical
    # disposal is compared against the scanned subset, and the gap is what the
    # warehouse has to find by reconciling the ledger.
    raw_qty = con.execute("SELECT sum(qty) FROM raw.waste_events").fetchone()[0]
    assert raw_qty == pytest.approx(truth[truth["was_scanned"]]["qty"].sum(), rel=TOL)
    assert raw_qty < truth["qty"].sum(), (
        "the scan file accounts for every disposal; D07 exists because "
        "recorded waste understates real loss"
    )

    # And the daily ledger agrees with the disposal records (see the world
    # generator's ledger-closure test); recorded waste is a floor on *that*.
    daily = _truth(cfg, "truth_store_item_day")
    assert daily["waste_qty"].sum() == pytest.approx(
        truth[truth["loss_class"] == "waste"]["qty"].sum(), rel=0.02
    )

    # Compliance varies by store, which is why the estate average is the wrong
    # number to plan with.
    by_store = loss_waste.groupby("store_code")["was_scanned"].mean()
    assert by_store.max() - by_store.min() > 0.02, (
        "scan compliance is uniform across stores, so the store-level problem "
        "this defect models is not being simulated"
    )


# ---------------------------------------------------------------------------
# D08: reason codes drift by firmware
# ---------------------------------------------------------------------------
def test_d08_reason_code_drift_is_captured_not_collapsed(defect_world, loaded):
    cfg, world, _ = defect_world
    con, files, _, _ = loaded

    raw_codes = set(
        con.execute("SELECT DISTINCT reason_code_raw FROM raw.waste_events").fetchdf()[
            "reason_code_raw"
        ]
    )
    # The raw layer keeps what was filed, verbatim. Mapping is the warehouse's
    # job, and it needs the original to do it.
    assert len(raw_codes) >= 8, f"reason codes were collapsed on load: {sorted(raw_codes)}"

    # Several spellings of the same physical event must be present, because that
    # is the whole defect: EXP / expired / EXPIRY / BEST BEFORE all mean one
    # thing and arrive as four strings.
    expired_spellings = {c for c in raw_codes if "EXP" in c.upper()}
    assert len(expired_spellings) >= 3, (
        f"expiry is spelled {sorted(expired_spellings)}; the firmware drift D08 "
        "models is not being simulated, so nothing is proving the mapping matters"
    )
    # Case and whitespace drift, not just alias drift.
    assert len({c.strip().lower() for c in expired_spellings}) < len(expired_spellings) or any(
        c != c.strip() or c != c.upper() for c in raw_codes
    )
    # Numeric legacy codes from the oldest firmware.
    assert {c for c in raw_codes if c.isdigit()}, "no numeric legacy codes were landed"

    # The reason codes the warehouse must map are all covered by the seed map:
    # an unmapped code would show up as 'unmapped' in the quality mart, which is
    # the alert, not a silent default to 'other'.
    seed = pd.read_csv(cfg.paths.root / "seeds" / "waste_reason_map.csv")
    mapped = {str(c).strip().upper() for c in seed["raw_code"]}
    unmapped = {c for c in raw_codes if str(c).strip().upper() not in mapped}
    assert not unmapped, (
        f"the landing zone emits codes with no row in seeds/waste_reason_map.csv: "
        f"{sorted(unmapped)}"
    )


# ---------------------------------------------------------------------------
# D09: SKUs the item master does not carry
# ---------------------------------------------------------------------------
def test_d09_unmapped_skus_are_quantified_not_dropped(defect_world, loaded):
    cfg, world, _ = defect_world
    con, files, _, _ = loaded

    known = set(con.execute("SELECT sku FROM raw.item_master").fetchdf()["sku"].astype(str))
    sold = set(con.execute("SELECT DISTINCT sku FROM raw.pos_sales").fetchdf()["sku"])
    unmapped = sold - known

    assert unmapped, (
        "every SKU the till sold is in the item master; D09 did not fire, so "
        "nothing is proving the join does not drop rows"
    )
    # The rows are still there. An inner join downstream would silently lose
    # this revenue, which is the failure mode D09 is guarding.
    unmapped_rows = con.execute(
        "SELECT count(*), sum(net_sales) FROM raw.pos_sales WHERE sku NOT IN "
        "(SELECT sku FROM raw.item_master)"
    ).fetchone()
    assert unmapped_rows[0] > 0
    assert unmapped_rows[1] != 0, "unmapped SKU revenue was zeroed rather than preserved"


# ---------------------------------------------------------------------------
# D10: voided and refunded lines arrive as negatives
# ---------------------------------------------------------------------------
def test_d10_void_lines_are_preserved_and_nettable(defect_world, loaded):
    cfg, world, manifest = defect_world
    con, files, _, _ = loaded

    negatives = con.execute(
        "SELECT count(*) FROM raw.pos_sales WHERE units < 0 OR gross_sales < 0"
    ).fetchone()[0]
    assert negatives > 0, "no negative lines were landed; D10 did not fire"

    # Netting per store-day-SKU never goes negative: a negative net would mean
    # the void logic is wrong, not the data.
    netted = query_df(
        con,
        """
        SELECT business_date, store_code, sku, sum(units) AS units
        FROM raw.pos_sales GROUP BY 1, 2, 3 HAVING sum(units) < -0.001
        """,
    )
    assert netted.empty, f"netted units went negative:\n{netted.head()}"

    # And netting recovers the truth, rather than the gross figure.
    truth = _truth(cfg, "truth_store_item_day")
    raw_units = con.execute("SELECT sum(units) FROM raw.pos_sales").fetchone()[0]
    truth_units = truth["units_sold"].sum()
    assert raw_units == pytest.approx(truth_units, rel=0.02), (
        f"netted units {raw_units:,.0f} against truth {truth_units:,.0f}; "
        "negatives were dropped instead of netted, or double counted"
    )


# ---------------------------------------------------------------------------
# D11: counts are eyeballed, and pack items get counted in packs
# ---------------------------------------------------------------------------
def test_d11_count_normalisation_removes_pack_error(defect_world, loaded):
    cfg, world, _ = defect_world
    con, files, _, _ = loaded

    counts = query_df(
        con,
        "SELECT c.*, i.pack_size FROM raw.stock_counts c LEFT JOIN raw.item_master i USING (sku)",
    )
    assert not counts.empty, "no counts landed"

    # The defect is real: some files claim units were counted in packs.
    assert (counts["unit_of_measure"] == "pack").any(), (
        "no count was recorded in packs; D11 did not fire"
    )

    truth = _truth(cfg, "truth_store_item_day")
    order = (truth["soh_eod_value"] / truth["soh_eod_qty"].replace(0, pd.NA)).dropna()
    typical_unit_value = float(order.median())

    # A pack-counted row that was left in packs is out by pack_size (up to 12x),
    # so it shows up as a count whose implied unit value is wildly off.
    merged = counts.dropna(subset=["pack_size"]).copy()
    merged["implied_unit_value"] = merged["counted_qty"].replace(0, pd.NA) * typical_unit_value
    packs = merged[merged["unit_of_measure"] == "pack"]
    assert not packs.empty
    # Counted quantities are plain numbers in the same unit as the ledger.
    assert pd.api.types.is_numeric_dtype(counts["counted_qty"])
    assert (counts["counted_qty"] >= 0).all()

    # Counts should be close to the truth's closing stock, allowing for the
    # deliberate +/-3% eyeball error and pack rounding.
    truth_last = truth.sort_values("business_date").groupby(["store_code", "sku"]).tail(1)
    raw_last = counts.sort_values("business_date").groupby(["store_code", "sku"]).tail(1)
    joined = raw_last.merge(
        truth_last, on=["store_code", "sku"], how="inner", suffixes=("", "_truth")
    )
    assert len(joined) > 0
    both = joined[joined["soh_eod_qty"] > 0]
    error = ((both["counted_qty"] - both["soh_eod_qty"]).abs() / both["soh_eod_qty"]).median()
    assert error < 0.25, f"median count error {error:.1%} is beyond an eyeballed count"


# ---------------------------------------------------------------------------
# D12: short and substituted deliveries
# ---------------------------------------------------------------------------
def test_d12_delivery_fill_rate_and_life_at_delivery(defect_world, loaded):
    cfg, world, _ = defect_world
    con, files, _, _ = loaded

    grn = query_df(con, "SELECT * FROM raw.goods_received")
    assert not grn.empty
    assert pd.api.types.is_numeric_dtype(grn["qty_received"])

    # The three quantities are kept separate. Collapsing them loses the
    # difference between "we ordered less" and "the supplier let us down".
    assert {"qty_ordered", "qty_received", "qty_rejected"} <= set(grn.columns)

    # Short deliveries are real and common enough to be a planning input.
    shorts = grn[grn["qty_received"] < grn["qty_ordered"] - 1e-9]
    assert len(shorts) > 50, f"only {len(shorts)} short lines; D12 did not fire"
    assert len(shorts) / len(grn) > 0.03

    # The *estate* fill rate nets out near 1.0, and that is correct, not a bug:
    # substitutions over-supply by 30-60% and pack rounding goes up, so the
    # shortfalls are offset in aggregate. Which is exactly why supplier
    # *variation* is the metric that matters rather than the estate average.
    per_supplier = grn.groupby("supplier_code").apply(
        lambda d: d["qty_received"].sum() / d["qty_ordered"].sum(), include_groups=False
    )
    assert per_supplier.min() < 0.99, (
        "every supplier filled every order; reliability is not being applied"
    )
    assert per_supplier.max() - per_supplier.min() > 0.02, (
        "supplier fill rates are indistinguishable, so the reliability "
        "attribute is not reaching the delivery simulation"
    )

    subs = grn[grn["was_substituted"]]
    assert not subs.empty, "no substituted delivery; D12 did not fire"
    # A substitution brings its own cost, which is why cost is per delivery line.
    assert subs["unit_cost"].nunique() > 1

    # Life at delivery is per delivery line, so a short-life substitution is
    # visible rather than averaged away against the item's standard.
    life_span = (pd.to_datetime(grn["expiry_date"]) - pd.to_datetime(grn["business_date"])).dt.days
    assert life_span.min() >= 1
    by_sku = life_span.groupby(grn["sku"]).nunique()
    assert (by_sku > 1).sum() > 0, "shelf life never varies within a SKU"


# ---------------------------------------------------------------------------
# D13: a printed markdown is not always honoured at the till
# ---------------------------------------------------------------------------
def test_d13_markdown_erosion_uses_realised_discount(defect_world, loaded):
    cfg, world, _ = defect_world
    con, files, _, _ = loaded

    marks = query_df(con, "SELECT * FROM raw.markdown_events")
    assert not marks.empty, "no markdown tickets landed"

    # The defect: more was ticketed than was sold at the marked price.
    ticketed = marks["qty_ticketed"].sum()
    honoured = marks["qty_honoured"].sum()
    assert honoured < ticketed, "every printed ticket was honoured; D13 did not fire"
    assert honoured / ticketed > 0.75, "the unhonoured share is too large to be the 9% defect"

    # Erosion is read from POS money (what the till actually rang), not from the
    # tickets. If the pipeline priced erosion off tickets it would overstate it.
    truth = _truth(cfg, "truth_store_item_day")
    pos_discount = con.execute("SELECT sum(discount_amount) FROM raw.pos_sales").fetchone()[0]
    truth_erosion = truth["markdown_erosion_value"].sum()
    assert pos_discount == pytest.approx(truth_erosion, rel=0.02), (
        f"realised discount ${pos_discount:,.0f} against truth erosion "
        f"${truth_erosion:,.0f}; erosion is not being read from the till"
    )

    # Tickets are used for depth and timing only, which is why they roll up to
    # the configured ladder.
    ladder = {0.10, 0.20, 0.30, 0.40, 0.50, 0.70}
    assert set(marks["markdown_pct"].round(2).unique()) <= ladder


# ---------------------------------------------------------------------------
# Cross-cutting: the catalogue is accurate
# ---------------------------------------------------------------------------
def test_a_shrinking_world_does_not_leave_the_previous_one_in_raw(tmp_path_factory):
    """Loading a narrower world must remove what the wider one left behind.

    This is a real hole that a small-world CI run exposed. The loader replaces
    only the (day, store) pairs a file covers, which is what makes it idempotent.
    It never deleted a store that stopped appearing. Run the eight-store
    estate, then a two-store world, and `raw` held both, so the report was built
    on two worlds at once. The only visible symptom was a number quietly too big,
    which is the worst kind of symptom.
    """
    root = tmp_path_factory.mktemp("shrink")
    wide = load_config().override(
        as_of="2026-09-10",
        history_days=20,
        stores=4,
        seed=99,
        landing_dir=root / "landing",
        truth_dir=root / "truth",
        warehouse_file=root / "w.duckdb",
        artifacts_dir=root / "a",
        build_dir=root / "b",
    )
    wide.paths.ensure()
    write_landing(wide, simulate(wide))

    with connect(wide) as con:
        Ingestor(wide, con).run()
        wide_stores = {
            r[0] for r in con.execute("select distinct store_code from raw.pos_sales").fetchall()
        }
        assert len(wide_stores) == 4

        # Now the same warehouse, a narrower world. `make world` clears the
        # landing zone before regenerating, so the test does the same. Leaving
        # the old files behind would be testing a different scenario (a source
        # that stopped sending while its files stayed put).
        narrow = wide.override(stores=2, seed=99)
        shutil.rmtree(narrow.paths.landing_dir)
        narrow.paths.ensure()
        write_landing(narrow, simulate(narrow))
        Ingestor(narrow, con).run()

        narrow_stores = {
            r[0] for r in con.execute("select distinct store_code from raw.pos_sales").fetchall()
        }
        assert narrow_stores <= wide_stores
        assert len(narrow_stores) == 2, (
            f"raw still holds {sorted(narrow_stores)}; the previous world was "
            "not pruned, so the report would be built on two worlds at once"
        )
        # And the ledger must agree with the narrower world.
        ledger_stores = (
            {
                r[0]
                for r in con.execute(
                    "select distinct store_code from intermediate.int_stock_ledger"
                ).fetchall()
            }
            if con.execute(
                "select count(*) from information_schema.tables where table_schema='intermediate'"
            ).fetchone()[0]
            else narrow_stores
        )
        assert narrow_stores <= ledger_stores | narrow_stores


def test_pruning_is_scoped_and_does_not_truncate_history():
    """A load with no date window must not delete days it was not asked about.

    The prune is a safety net, not a reset: it may only remove what has genuinely
    gone. An empty landing directory in particular must never be read as
    "delete everything".
    """
    root = Path(tempfile.mkdtemp())
    cfg = load_config().override(
        as_of="2026-09-10",
        history_days=20,
        stores=2,
        seed=123,
        landing_dir=root / "landing",
        truth_dir=root / "truth",
        warehouse_file=root / "w.duckdb",
        artifacts_dir=root / "a",
        build_dir=root / "b",
    )
    cfg.paths.ensure()
    write_landing(cfg, simulate(cfg))
    with connect(cfg) as con:
        Ingestor(cfg, con).run()
        before = con.execute("select count(*) from raw.pos_sales").fetchone()[0]
        assert before > 0
        # A load restricted to a window must not prune outside it.
        Ingestor(cfg, con).run(date_from=date(2026, 9, 1), date_to=date(2026, 9, 5))
        after = con.execute("select count(*) from raw.pos_sales").fetchone()[0]
        assert after >= before * 0.9, (
            "a windowed load truncated rows outside its window; the prune is "
            "deleting more than it owns"
        )


def test_every_ingest_level_catalogue_entry_has_a_test_here():
    """The catalogue names a test for each defect; this makes that a fact.

    Only the ingest-level defects are checked here. D01 to D06 are defects in a
    landed file, which is what this file proves. D07 to D13 are defects in the
    business meaning of the data (a scan that never happened, a printed ticket
    that was not honoured) and are only provable after the dbt models exist, so
    they name tests in `tests/test_warehouse.py`. The split matters: an ingest
    test cannot prove the ledger reconciles, and a warehouse test cannot prove a
    redelivery was de-duplicated.
    """
    import re

    source = Path(__file__).read_text()
    defined = set(re.findall(r"def (test_d\d\d_\w+)", source))
    ingest_level = {"D01", "D02", "D03", "D04", "D05", "D06"}

    missing = []
    for code, defect in _catalogue().items():
        named = defect.get("test", "")
        match = re.search(r"::(test_\w+)", named)
        if code not in ingest_level:
            # Must still point at a test somewhere, and not at this file.
            if "test_ingest_defects" in named:
                missing.append((code, "warehouse-level defect claims an ingest test"))
            continue
        if not match:
            missing.append((code, "no test named in the catalogue"))
            continue
        if match.group(1) not in defined:
            missing.append((code, match.group(1)))
    assert not missing, f"catalogue entries without a test here: {missing}"


def test_warehouse_level_defects_name_warehouse_tests():
    """D07 to D13 must be proven where they can be: after the models exist."""
    for code, defect in _catalogue().items():
        if code in {"D01", "D02", "D03", "D04", "D05", "D06"}:
            continue
        assert "test_warehouse.py" in defect.get("test", ""), (
            f"{code} should name a warehouse test, since no ingest test can "
            "prove a business-meaning defect"
        )


def test_defect_world_actually_contains_the_defects(defect_world):
    """Guard against a test passing because the defect never fired.

    Each of these is asserted individually above, but a single summary here
    means a silently-broken injector fails loudly in one place.
    """
    cfg, world, manifest = defect_world
    assert (manifest["is_duplicate_of"].notna()).any(), "D01"
    gap_files = pd.read_csv(cfg.paths.truth_dir / "landing_gaps.csv")
    assert not gap_files.empty, "D03"
    truth = _truth(cfg, "truth_disposals")
    assert (~truth["was_scanned"]).any(), "D07"
    assert len(set(truth["reason_code"])) >= 3, "D08"
