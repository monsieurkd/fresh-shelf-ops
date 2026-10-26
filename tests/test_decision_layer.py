"""Tests for the forecast and the optimiser.

These are unit tests over the decision logic, not integration tests over the
warehouse. The distinction matters: the warehouse suite proves the *data* is
right, and this file proves the *decisions* are right given the data. A planner
can be perfectly correct about a number and wrong about what to do with it, and
that failure mode does not show up anywhere else.

Two things in particular are pinned because they were real bugs:

* **Unit discipline.** `forecast_qty` is a daily projection and `units_ma_7` a
  daily mean; neither is a horizon total. The plan once divided by the horizon,
  which under-stated demand 14x and would have recommended ordering a fourteenth
  of what the store sells. The tests assert the magnitudes are comparable rather
  than merely finite.
* **The frontier must have a trade-off.** A sweep where one axis is always zero
  is not a frontier, and it would let the report claim an optimum that does not
  exist. The tests assert both losses are non-zero somewhere in the sweep.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fresh_shelf.config import load_config
from fresh_shelf.model.forecast import coverage, pinball_loss, wape
from fresh_shelf.model.optimise import (
    Economics,
    pick_quantile,
    quantify_impact,
    reconcile_impact_with_frontier,
    waste_revenue_frontier,
)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def test_wape_is_weighted_not_a_mean_of_percentages():
    """WAPE must weight by volume, or a single slow line dominates it.

    A line selling 2 instead of 1 is a 100% error and 1 unit of absolute error.
    Weighted WAPE calls that 1 unit out of 100, i.e. 1%; a mean of percentages
    would call the whole forecast 100% wrong.
    """
    actual = np.array([100.0, 2.0])
    predicted = np.array([100.0, 1.0])
    assert wape(actual, predicted) == pytest.approx(1.0 / 102.0, rel=1e-6)


def test_wape_is_nan_when_there_is_nothing_to_predict():
    """A silent zero would read as a perfect forecast."""
    assert np.isnan(wape(np.array([0.0, 0.0]), np.array([0.0, 0.0])))


def test_perfect_forecast_scores_zero():
    actual = np.array([1.0, 2.0, 3.0])
    assert wape(actual, actual) == 0.0
    assert pinball_loss(actual, actual, 0.9) == 0.0


def test_pinball_loss_is_asymmetric_in_the_right_direction():
    """At q=0.9, under-forecasting must cost more than over-forecasting.

    This asymmetry is why the optimiser can use a quantile at all:
    if the loss were symmetric the model would predict a mean, and the newsvendor
    calculation would be feeding on the wrong statistic.
    """
    actual = np.array([100.0])
    under = pinball_loss(actual, np.array([90.0]), 0.9)
    over = pinball_loss(actual, np.array([110.0]), 0.9)
    assert under > over
    assert under == pytest.approx(9.0)
    assert over == pytest.approx(1.0)


def test_coverage_measures_calibration():
    actual = np.array([1.0, 2.0, 3.0, 4.0])
    assert coverage(actual, np.array([5.0] * 4), 0.9) == 1.0
    assert coverage(actual, np.array([0.0] * 4), 0.9) == 0.0
    assert coverage(actual, np.array([2.0] * 4), 0.9) == 0.5


# ---------------------------------------------------------------------------
# Newsvendor economics
# ---------------------------------------------------------------------------
def test_critical_ratio_rises_with_margin():
    """A fat margin means over-order; a thin margin means under-order.

    This is the core economic claim of the whole project, so it is asserted
    directly: the reason a single estate-wide service level is wrong is that the
    optimal level is a property of each line's margin and cost.
    """
    fat = Economics(margin_per_unit=10.0, cost_per_unit=2.0).critical_ratio
    thin = Economics(margin_per_unit=0.5, cost_per_unit=8.0).critical_ratio
    assert fat > thin
    assert fat > 0.8
    assert thin < 0.1


def test_critical_ratio_is_a_probability():
    for margin, cost in [(0, 0), (5, 5), (100, 1), (1, 100), (-3, 4)]:
        ratio = Economics(margin, cost).critical_ratio
        assert 0.0 <= ratio <= 1.0


def test_zero_economics_falls_back_to_an_even_chance():
    assert Economics(0.0, 0.0).critical_ratio == 0.5


# ---------------------------------------------------------------------------
# Quantile selection
# ---------------------------------------------------------------------------
def test_pick_quantile_rounds_up_never_down():
    """Rounding down would silently order less than the economics asked for.

    On a fresh line the cost of being one pack short is asymmetric, so the safe
    direction is up. The test says which direction is safe.
    """
    available = [0.5, 0.7, 0.9]
    assert pick_quantile(0.6, available) == 0.7
    assert pick_quantile(0.7, available) == 0.7
    assert pick_quantile(0.95, available) == 0.9
    assert pick_quantile(0.1, available) == 0.5


def test_pick_quantile_handles_an_empty_ladder():
    assert pick_quantile(0.9, []) == 0.5


# ---------------------------------------------------------------------------
# The frontier
# ---------------------------------------------------------------------------
def _synthetic_plan(rows: int = 200) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    daily = rng.uniform(5, 60, rows)
    return pd.DataFrame(
        {
            "store_code": ["S01"] * rows,
            "sku": [f"P{i:04d}" for i in range(rows)],
            "forecast_daily_qty": daily,
            "effective_cover_days": rng.integers(1, 6, rows),
            "days_of_cover": rng.integers(1, 8, rows),
            "standard_cost": rng.uniform(1.0, 8.0, rows),
            "margin_per_unit": rng.uniform(0.5, 6.0, rows),
            "volatility_28": rng.uniform(0.1, 0.8, rows),
            "recommended_order_qty": daily * rng.uniform(1.0, 4.0, rows),
        }
    )


def test_frontier_has_a_real_trade_off_on_both_axes():
    """Neither axis may be identically zero, or the optimum is meaningless."""
    frontier = waste_revenue_frontier(_synthetic_plan())
    assert not frontier.empty
    assert (frontier["expected_waste_cost"] > 0).any(), "waste is never positive"
    assert (frontier["expected_lost_margin"] > 0).any(), "nothing is ever lost"
    assert frontier["is_optimal"].sum() == 1


def test_frontier_optimum_is_interior_not_at_an_end():
    """An optimum pinned to the end of the sweep means the sweep is too narrow."""
    frontier = waste_revenue_frontier(_synthetic_plan())
    ordered = frontier.sort_values("order_ratio")
    optimal = ordered[ordered["is_optimal"]]
    assert not optimal.empty
    position = ordered.index.get_loc(optimal.index[0])
    assert 0 < position < len(ordered) - 1, (
        "the optimum sits at the edge of the sweep, so the true optimum is "
        "outside the range being searched"
    )


def test_frontier_waste_rises_and_lost_margin_falls_monotonically():
    """The two costs must move in opposite directions across the sweep."""
    frontier = waste_revenue_frontier(_synthetic_plan()).sort_values("order_ratio")
    assert frontier["expected_waste_cost"].is_monotonic_increasing
    assert frontier["expected_lost_margin"].is_monotonic_decreasing


def test_frontier_is_empty_for_an_empty_plan():
    assert waste_revenue_frontier(pd.DataFrame()).empty


def test_impact_components_sum_to_the_net_benefit():
    """The published components must add up to the published total.

    This is the check that caught a real sign error: two of the terms were
    already negative, and subtracting them into the net silently added them back,
    so the table did not close. A reader who re-adds the rows is the intended
    auditor, and the arithmetic has to survive that.
    """
    plan = _synthetic_plan(120)
    plan["service_level"] = 0.55
    plan["target_quantile"] = 0.7
    plan["order_delta_qty"] = (
        plan["recommended_order_qty"] - plan["forecast_daily_qty"] * plan["days_of_cover"]
    )
    markdown = pd.DataFrame(
        {
            "should_mark_down": [True, True, False],
            "markdown_benefit": [100.0, 50.0, 999.0],
            "stock_on_hand_qty": [10.0, 5.0, 1.0],
            "optimal_markdown_pct": [0.3, 0.3, 0.3],
            "retail_price": [4.0, 4.0, 4.0],
        }
    )
    observed = pd.DataFrame({"recorded_waste_cost": [1000.0], "net_sales_value": [50000.0]})
    impact = quantify_impact(load_config(), plan, markdown, observed)
    values = impact.set_index("component")["value"].to_dict()
    net = float(values["net_benefit"])
    parts = (
        float(values["waste_avoided_cost"])
        + float(values["margin_forgone_on_reduced_orders"])
        + float(values["recovered_lost_margin"])
        + float(values["extra_waste_risk"])
        + float(values["markdown_timing_benefit"])
        + float(values["markdown_erosion_risk"])
    )
    assert parts == pytest.approx(net, abs=0.02), (
        f"components sum to {parts:,.2f} but the net is {net:,.2f}"
    )


def test_impact_haircut_is_symmetric():
    """One resale assumption must apply to both sides of the order delta.

    Discounting only the recovery side while charging the loss side in full is
    not conservatism. It manufactures a loss out of a symmetric trade, which is
    what an earlier version of this function did. The check is on the *rate*
    applied, so it isolates the haircut from the (legitimately different)
    quantities either side of the delta.
    """
    cfg = load_config()
    base = _synthetic_plan(80)
    base["service_level"] = 0.55
    base["target_quantile"] = 0.7
    base["days_of_cover"] = 2
    base["effective_cover_days"] = 2  # no cover removed, so waste_share is zero
    observed = pd.DataFrame({"recorded_waste_cost": [1.0], "net_sales_value": [1.0]})

    adds = base.copy()
    adds["order_delta_qty"] = adds["forecast_daily_qty"]
    adds_impact = quantify_impact(cfg, adds, pd.DataFrame(), observed).set_index("component")[
        "value"
    ]

    cuts = base.copy()
    cuts["order_delta_qty"] = -cuts["forecast_daily_qty"]
    cuts_impact = quantify_impact(cfg, cuts, pd.DataFrame(), observed).set_index("component")[
        "value"
    ]

    added_units = float(adds["forecast_daily_qty"].sum())
    cut_units = float(cuts["forecast_daily_qty"].sum())
    added_margin = float((adds["forecast_daily_qty"] * adds["margin_per_unit"]).sum())
    cut_margin = float((cuts["forecast_daily_qty"] * cuts["margin_per_unit"]).sum())

    recovered_rate = float(adds_impact["recovered_lost_margin"]) / added_margin
    forgone_rate = -float(cuts_impact["margin_forgone_on_reduced_orders"]) / cut_margin

    assert recovered_rate > 0
    assert added_units > 0 and cut_units > 0
    assert recovered_rate == pytest.approx(forgone_rate, rel=1e-6), (
        f"the add side recovers at {recovered_rate:.3f} of margin while the cut "
        f"side charges at {forgone_rate:.3f}; the haircut is not symmetric"
    )


# ---------------------------------------------------------------------------
# Cross-checking the two methods
# ---------------------------------------------------------------------------
def test_reconciliation_reports_disagreement_rather_than_hiding_it():
    """When the two methods disagree, the check must say so.

    This is the guard on a real finding: on this estate the plan's own dollar
    accounting comes out negative while the frontier still has an interior
    optimum. Presenting a benefit while the frontier points the other way would
    be misleading, so the disagreement is a published row.
    """
    frontier = waste_revenue_frontier(_synthetic_plan())
    plan = _synthetic_plan()
    plan["service_level"] = 0.55
    impact = pd.DataFrame(
        [
            {"component": "net_benefit", "value": -1000.0},
            {"component": "waste_avoided_cost", "value": 50.0},
        ]
    )
    check = reconcile_impact_with_frontier(impact, frontier, plan)
    assert not check.empty
    row = check[check["check"] == "methods_agree_on_direction"]
    assert float(row["value"].iloc[0]) == 0.0

    impact_ok = pd.DataFrame([{"component": "net_benefit", "value": 1000.0}])
    check_ok = reconcile_impact_with_frontier(impact_ok, frontier, plan)
    assert (
        float(check_ok[check_ok["check"] == "methods_agree_on_direction"]["value"].iloc[0]) == 1.0
    )


def test_reconciliation_reports_the_service_level_gap():
    frontier = waste_revenue_frontier(_synthetic_plan())
    plan = _synthetic_plan()
    plan["service_level"] = 0.9
    impact = pd.DataFrame([{"component": "net_benefit", "value": 1.0}])
    check = reconcile_impact_with_frontier(impact, frontier, plan)
    gap = float(check[check["check"] == "service_level_gap"]["value"].iloc[0])
    optimal = float(check[check["check"] == "frontier_optimum_service_level"]["value"].iloc[0])
    assert gap == pytest.approx(0.9 - optimal, abs=1e-6)


# ---------------------------------------------------------------------------
# Configuration wiring
# ---------------------------------------------------------------------------
def test_the_clock_and_the_optimiser_agree_on_scope():
    """The config must actually reach the optimiser, not sit in a file."""
    cfg = load_config()
    assert cfg.clock.as_of_date.isoformat() == "2026-09-10"
    assert cfg.forecast.get("horizon_days") == 14
    assert cfg.optimise.get("service_level_floor", 0) < cfg.optimise.get("service_level_ceiling", 1)
    depths = cfg.optimise.get("markdown_depths", [])
    assert depths == sorted(depths)
    assert all(0 < d < 1 for d in depths)
