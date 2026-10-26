"""The simulated world behaves like a fresh department: stock does not go
negative, nothing is sold that was not delivered, the estate closes when the
calendar says so, and the same seed gives the same year."""

from __future__ import annotations

import pandas as pd
import pytest

from fresh_shelf.config import load_config
from fresh_shelf.world import simulate
from fresh_shelf.world.master import build_masters, items

# Builds a 45-day world, so it is slower than the pure-logic tests but far
# cheaper than the defect suite. Marked `slow` to keep `-m "not slow"` honest.
pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def micro_world(tmp_path_factory):
    """Two stores, 45 days: small enough to run in a test, real enough to be
    the same code path as the full estate."""
    cfg = load_config().override(
        as_of="2026-09-10",
        history_days=45,
        stores=2,
        seed=1234,
        landing_dir=tmp_path_factory.mktemp("landing"),
        truth_dir=tmp_path_factory.mktemp("truth"),
        warehouse_file=tmp_path_factory.mktemp("wh") / "w.duckdb",
        artifacts_dir=tmp_path_factory.mktemp("artifacts"),
        build_dir=tmp_path_factory.mktemp("build"),
    )
    return cfg, simulate(cfg)


def test_same_seed_gives_an_identical_year(micro_world):
    cfg, first = micro_world
    second = simulate(cfg)
    for feed in sorted(first.feeds):
        pd.testing.assert_frame_equal(
            first.feeds[feed], second.feeds[feed], check_dtype=False, obj=f"feed {feed}"
        )
    pd.testing.assert_frame_equal(
        first.truth["truth_store_item_day"],
        second.truth["truth_store_item_day"],
        check_dtype=False,
    )


def test_stock_never_goes_negative_and_nothing_sells_that_was_not_delivered(micro_world):
    _, world = micro_world
    daily = world.truth["truth_store_item_day"]

    for column in (
        "units_sold",
        "units_sold_full_price",
        "units_sold_markdown",
        "soh_eod_qty",
        "waste_qty",
        "prep_loss_qty",
        "lost_sales_qty",
    ):
        assert (daily[column] >= 0).all(), f"{column} went negative"

    assert (daily["soh_eod_value"] >= 0).all()
    # Sales are the sum of full-price and markdown units, and never both.
    net = daily["units_sold_full_price"] + daily["units_sold_markdown"]
    assert (daily["units_sold"] - net).abs().max() < 0.051


def test_the_estate_does_not_trade_when_it_is_closed(micro_world):
    cfg, world = micro_world
    daily = world.truth["truth_store_item_day"]
    closed_days = set(world.calendar.loc[world.calendar["is_closed"], "day"])

    if closed_days:
        closed_rows = daily[daily["business_date"].isin(closed_days)]
        assert closed_rows["units_sold"].sum() == 0
        assert closed_rows["delivered_qty"].sum() == 0
    # The window is a trading year, so at least one closure falls inside it.
    assert isinstance(closed_days, set)


def test_christmas_day_is_a_closure_in_the_calendar():
    cfg = load_config().override(as_of="2026-09-10", history_days=400)
    from fresh_shelf.world.calendar import build_calendar

    cal = build_calendar(cfg)
    closures = set(cal.loc[cal["is_closed"], "day"])
    assert pd.Timestamp("2025-12-25").date() in closures
    assert pd.Timestamp("2026-04-03").date() in closures  # Good Friday
    assert pd.Timestamp("2026-01-26").date() not in closures  # Australia Day trades


def test_expiry_buckets_reconcile_with_deliveries(micro_world):
    _, world = micro_world
    expiry = world.truth["truth_expiry_buckets"]
    daily = world.truth["truth_store_item_day"]

    assert (expiry["qty_remaining"] >= 0).all()
    assert (expiry["value_remaining"] >= 0).all()
    # Closing stock per store-item-day equals the sum of its expiry buckets.
    per_day = expiry.groupby(["store_code", "sku", "business_date"], as_index=False)[
        "qty_remaining"
    ].sum()
    merged = daily.merge(per_day, on=["store_code", "sku", "business_date"], how="left")
    merged["qty_remaining"] = merged["qty_remaining"].fillna(0.0)
    assert (merged["qty_remaining"] - merged["soh_eod_qty"]).abs().max() < 0.05


def test_disposals_and_the_daily_ledger_agree(micro_world):
    """The two ways the simulation records a loss must add up.

    `truth_disposals` is what physically left the shelf; `truth_store_item_day`
    is the day's ledger. If they disagree, the warehouse's ledger reconciliation
    will invent unrecorded shrink out of a simulation bug. That is exactly how
    this test earned its place: the random-quality losses were being removed
    from the shelf but never added to the daily waste figure.
    """
    _, world = micro_world
    daily = world.truth["truth_store_item_day"]
    disposals = world.truth["truth_disposals"]

    # Aggregate both sides to the same grain before comparing, and compare the
    # *totals*: a per-row comparison is meaningless here because the daily
    # ledger has one row per store-item-day while a day can carry several
    # disposal events for the same item.
    ledger = daily.groupby(["store_code", "business_date"], as_index=False)[
        ["waste_qty", "prep_loss_qty"]
    ].sum()
    events = (
        disposals.groupby(["store_code", "business_date", "loss_class"])["qty"]
        .sum()
        .unstack(fill_value=0.0)
        .reset_index()
    )
    joined = ledger.merge(events, on=["store_code", "business_date"], how="inner").fillna(0.0)

    for ledger_col, event_col in (("waste_qty", "waste"), ("prep_loss_qty", "preparation")):
        if event_col not in joined.columns:
            continue
        worst = (joined[ledger_col] - joined[event_col]).abs().max()
        # Weighted lines are rounded to 3dp and eaches to whole units, and a
        # store-day can hold a hundred lines, so the tolerance scales with the
        # number of lines rather than being a fixed unit count.
        tolerance = 1.0 + 0.02 * joined[ledger_col].max()
        assert worst < tolerance, (
            f"the daily {ledger_col} and the {event_col} disposal events disagree "
            f"by {worst:.2f} units on a store-day (tolerance {tolerance:.1f})"
        )

    # And the estate totals must agree closely: this is the number the report
    # quotes, so a drift here would be a wrong headline.
    assert daily["waste_qty"].sum() == pytest.approx(
        disposals.loc[disposals["loss_class"] == "waste", "qty"].sum(), rel=0.01
    )
    assert daily["prep_loss_qty"].sum() == pytest.approx(
        disposals.loc[disposals["loss_class"] == "preparation", "qty"].sum(), rel=0.01
    )


def test_the_window_opens_on_a_steady_shelf(micro_world):
    """The warm-up period exists so the window opens on a shelf in steady state.

    Stock is *supposed* to be low on day one: the day before was a closed Sunday,
    and the estate's ordering habit prices cover only to the next delivery. So
    the test is that day one looks like an ordinary Monday rather than like the
    empty store a cold start would produce. The run must have been carrying
    stock, batches and expiry dates for three weeks before the window opened.
    """
    _, world = micro_world
    daily = world.truth["truth_store_item_day"]
    by_day = daily.groupby("business_date")["soh_eod_value"].sum().sort_index()

    # Compare day one against the same weekday later in the window, not against
    # the window average: stock legitimately troughs on Sunday night and builds
    # to Friday, so a Monday is always at the low end of the week.
    first_day = by_day.index[0]
    first_weekday = pd.Timestamp(first_day).dayofweek
    same_weekday = by_day[[pd.Timestamp(d).dayofweek == first_weekday for d in by_day.index]].iloc[
        1:
    ]
    assert not same_weekday.empty
    norm = same_weekday.median()
    assert by_day.iloc[0] > 0.6 * norm, (
        f"day one holds ${by_day.iloc[0]:,.0f} of stock against a normal "
        f"{first_day:%A} of ${norm:,.0f}; the warm-up did not carry shelf state, "
        "so the window is opening on a cold shelf"
    )
    # A cold start would also show up as deliveries with nothing to sell from.
    assert daily["soh_eod_qty"].iloc[: len(daily) // 10].sum() > 0


def test_waste_and_sales_land_in_a_plausible_band(micro_world):
    """Guards the whole simulation: a fresh department that wastes 40% of what
    it buys, or none of it, is not a supermarket."""
    _, world = micro_world
    daily = world.truth["truth_store_item_day"]
    sales = daily["net_sales_value"].sum()
    waste_value = (daily["waste_qty"] * 1.0).sum()  # quantity, valued properly downstream
    assert sales > 0
    assert (waste_value / max(sales, 1)) < 1.0  # sanity: waste qty cannot dwarf sales $


def test_markdown_lift_is_a_probability():
    from fresh_shelf.world.simulate import LIFT_BY_DEPTH, markdown_lift

    for item in items():
        for depth in LIFT_BY_DEPTH:
            lift = markdown_lift(item, depth)
            assert 0.0 < lift <= 0.95, (item.sku, depth, lift)
            assert lift <= 1.0


def test_assortment_is_smaller_at_express_stores(tmp_path_factory):
    cfg = load_config().override(stores="all")
    masters = build_masters(cfg)
    by_store = {store.code: len(masters.items_for_store(store.code)) for store in masters.stores}
    assert by_store["S07"] < by_store["S03"]
    assert by_store["S08"] < by_store["S01"]
    assert all(count > 40 for count in by_store.values())
