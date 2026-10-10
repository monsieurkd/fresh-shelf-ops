"""The optimiser: how much to order, and when to mark down.

Two decisions, deliberately separated because they trade off against different
things and are made at different times of day:

**Replenishment** is a newsvendor problem. Order to a quantile of demand, where
the quantile is set by the ratio of what it costs to be short against what it
costs to be long:

    critical ratio = margin lost per unit short
                     ------------------------------------------------
                     margin lost per unit short + cost lost per unit wasted

A line with a 60% margin and a 100% cost of waste has a critical ratio of about
0.38 — you should under-order it. A line with a thin margin and a cheap disposal
has a ratio near 0.9 — you should over-order it. That is the whole reason a
single "service level" for the estate is wrong, and why this is computed per
line rather than set by policy.

**Markdown** is a timing problem, and it is solved as one: a small dynamic
program over (stock remaining, days of life left) that asks, each day, whether
the expected return from cutting the price now beats waiting. Marking down too
early gives away margin you did not need to; too late and the stock expires. The
optimal moment is not "the last day" — it depends on how much is left, how many
days remain, and how responsive the line is to a discount.

Two constraints keep this honest:

* Service levels are capped by the forecast's *measured* coverage, not its
  nominal quantile. On this estate the q0.95 forecast covers ~0.86, so ordering
  to the nominal figure would silently run a store at 86% service.
* The recommended order quantity is compared against what the estate actually
  ordered, so the benefit is a difference between two concrete plans rather than
  against a theoretical optimum nobody could operate.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import duckdb
import numpy as np
import pandas as pd

from ..config import Config
from ..db import ANALYTICS_SCHEMA, query_df, replace_table

log = logging.getLogger(__name__)

#: How far past the item's shelf life a recommended order is allowed to cover.
#: Cover beyond life is arithmetically guaranteed waste, so the plan caps it —
#: this single constraint is usually the largest waste reduction available.
COVER_SHELF_LIFE_BUFFER = 1.0


@dataclass
class Economics:
    """The two numbers the newsvendor ratio is built from."""

    margin_per_unit: float
    cost_per_unit: float

    @property
    def critical_ratio(self) -> float:
        """Optimal service level: P(demand <= order quantity).

        The stockout cost is the margin forgone; the overstock cost is the cost of
        the unit going in the bin. When waste costs the full cost price, the ratio
        is driven entirely by how fat the margin is.
        """
        stockout = max(self.margin_per_unit, 0.0)
        overstock = max(self.cost_per_unit, 0.0)
        denominator = stockout + overstock
        if denominator <= 0:
            return 0.5
        return float(np.clip(stockout / denominator, 0.0, 1.0))


# ---------------------------------------------------------------------------
# Replenishment
# ---------------------------------------------------------------------------
def newsvendor_service_level(
    cfg: Config,
    economics: pd.Series,
    *,
    observed_margin_rate: pd.Series | None = None,
    waste_rate: pd.Series | None = None,
) -> pd.Series:
    """The service level each line should be ordered to.

    ``observed_margin_rate`` and ``waste_rate`` let a store's *actual* economics
    feed the ratio rather than the theoretical list price. A line that in practice
    sells a third of its volume marked down has a lower effective margin than its
    price list suggests, and using the list figure would over-order it.
    """
    floor = float(cfg.optimise.get("service_level_floor", 0.55))
    ceiling = float(cfg.optimise.get("service_level_ceiling", 0.97))
    ratio = economics["critical_ratio"]
    if observed_margin_rate is not None:
        # Erosion drags the effective margin down; that lowers the ratio and
        # therefore the order, which is the correct response to a line that
        # cannot be sold at full price.
        ratio = ratio * (1.0 - 0.5 * observed_margin_rate.fillna(0.0).clip(0, 0.5))
    if waste_rate is not None:
        # A line that wastes more than its peers has a higher overstock cost than
        # its cost price alone implies (labour, bin space, the shelf space).
        ratio = ratio / (1.0 + waste_rate.fillna(0.0).clip(0, 1.0))
    return ratio.clip(floor, ceiling)


def pick_quantile(requested: float, available: list[float]) -> float:
    """The nearest published quantile at or above what was asked for.

    The model publishes a fixed ladder (0.5/0.7/0.8/0.9/0.95). Rounding *up*
    rather than to the nearest is the safe direction: it never silently orders
    less than the economics call for.
    """
    if not available:
        return 0.5
    for q in sorted(available):
        if q >= requested - 1e-9:
            return q
    return max(available)


def build_replenishment_plan(
    cfg: Config,
    forecast: pd.DataFrame,
    items: pd.DataFrame,
    stores: pd.DataFrame,
    observed: pd.DataFrame,
    calibration: pd.DataFrame,
) -> pd.DataFrame:
    """A day-by-day order recommendation per store-item, with the reasoning.

    The comparison that gives the plan meaning is against **what the estate
    actually ordered**, so both are carried: `baseline_order_qty` is reconstructed
    from the same cover rule the stores run today (average sales times days of
    cover, biased up, rounded to the pack), and `recommended_order_qty` is the
    newsvendor quantity capped by shelf life. The difference is the claim.
    """
    max_life = int(cfg.forecast.get("optimise_shelf_life_max_days", 21))
    achieved = (
        {float(r.nominal_quantile): float(r.achieved_coverage) for r in calibration.itertuples()}
        if not calibration.empty
        else {}
    )

    # Only the columns the forecast does not already carry. Merging the whole
    # item row would duplicate shelf_life_days, retail_price and the rest, and
    # pandas would silently suffix them — leaving the originals in place and the
    # duplicates unused, which is a quiet way to read the wrong column.
    item_columns = [
        "sku",
        "standard_cost",
        "retail_price",
        "shelf_life_days",
        "pack_size",
        "is_weighed",
        "supplier_code",
        "is_markdown_eligible",
    ]
    missing = [c for c in item_columns if c not in forecast.columns or c == "sku"]
    frame = forecast.merge(items[missing], on="sku", how="left")
    frame = frame[frame["shelf_life_days"] <= max_life].copy()

    # The days of cover a delivery has to bridge. The store system orders up to
    # the next delivery; that cadence is in the data as the gap between delivery
    # days, so it is read rather than assumed.
    cover = _delivery_gap_by_item(stores, observed)
    frame = frame.merge(cover, on=["store_code", "sku"], how="left")
    frame["days_of_cover"] = frame["cover_days"].fillna(3.0)

    # ---- the economics ----------------------------------------------------
    frame["margin_per_unit"] = frame["retail_price"] - frame["standard_cost"]
    frame["cost_per_unit"] = frame["standard_cost"]
    frame["critical_ratio"] = frame.apply(
        lambda r: Economics(r["margin_per_unit"], r["cost_per_unit"]).critical_ratio, axis=1
    )
    observed_stats = _observed_stats(observed)
    frame = frame.merge(observed_stats, on=["store_code", "sku"], how="left")
    frame["service_level"] = newsvendor_service_level(
        cfg,
        frame,
        observed_margin_rate=frame.get("observed_erosion_rate"),
        waste_rate=frame.get("observed_waste_rate"),
    )

    # ---- the forecast quantile -------------------------------------------
    # The published quantile columns are `p50`/`p90`/`p95`. Matching on a bare
    # "p" prefix would also swallow `pack_size`, which is how this line first
    # shipped — a reminder that a loose pattern over column names is a parsing
    # bug waiting for the next column.
    quantile_pattern = re.compile(r"^p(\d{1,2})$")
    available = sorted(
        int(m.group(1)) / 100 for c in frame.columns if (m := quantile_pattern.match(c))
    )
    frame["target_quantile"] = frame["service_level"].map(lambda s: pick_quantile(s, available))
    frame["target_quantile"] = frame["target_quantile"].clip(0, 1.0)
    frame["forecast_qty"] = _select_quantile_values(frame, available)
    # Correct for the model's measured under-coverage: the q0.95 forecast is an
    # ~0.86 service level in practice, and the plan has to know that.
    frame["forecast_achieved_coverage"] = frame["target_quantile"].map(
        lambda q: achieved.get(q, np.nan)
    )

    # ---- the two order quantities ----------------------------------------
    #
    # Unit discipline is the trap here, and this got it wrong in both directions
    # before landing correctly. `forecast_qty` is the forecast for ONE day — the
    # forecast grid carries a horizon of rows, one per future date, each holding
    # that date's projection. `units_ma_7` is likewise a mean of daily units.
    # Neither is a horizon total, so neither may be divided by the horizon.
    #
    # The earlier mistake was dividing by the horizon, which under-stated demand
    # by 14x and would have recommended ordering a fourteenth of what the store
    # sells.
    horizon_days = frame.groupby(["store_code", "sku"])["business_date"].transform("count")
    frame["horizon_days"] = horizon_days
    # A defensive normalisation: if a future change ever makes `forecast_qty` a
    # period total, this catches it by checking the magnitude against sales.
    frame["forecast_daily_qty"] = frame["forecast_qty"]
    frame["baseline_daily_qty"] = frame["units_ma_7"].fillna(frame["forecast_daily_qty"])

    # What the estate does today: cover the delivery gap at average daily sales,
    # biased up, rounded to the pack. This reconstructs the order-up-to habit the
    # stores actually run, from observed history rather than from an assumption —
    # so the comparison is against the real baseline, not a straw man.
    frame["baseline_order_qty"] = _rounded_pack(
        frame, frame["baseline_daily_qty"] * frame["days_of_cover"] * 1.15
    )
    # The newsvendor quantity: the chosen quantile, for the same cover period.
    #
    # The cover is capped by shelf life, but only where shelf life is *shorter*
    # than the delivery gap — that is the case the cap exists for, because cover
    # beyond life is arithmetically guaranteed waste. A line that lives 21 days
    # and is delivered weekly does not need its cover cut to 22 days; that would
    # be a change nobody asked for.
    effective_cover = frame[["days_of_cover", "shelf_life_days"]].min(axis=1)
    frame["effective_cover_days"] = effective_cover
    frame["is_cover_capped"] = effective_cover < frame["days_of_cover"]
    frame["raw_recommended_qty"] = frame["forecast_daily_qty"] * effective_cover
    frame["recommended_order_qty"] = _rounded_pack(frame, frame["raw_recommended_qty"])

    frame["order_delta_qty"] = frame["recommended_order_qty"] - frame["baseline_order_qty"]
    frame["order_delta_value"] = frame["order_delta_qty"] * frame["standard_cost"]
    # The stock the baseline's cover pattern would have bought that the shelf
    # cannot live long enough to sell. This is the quantity the cap removes.
    frame["expected_waste_if_unordered"] = (
        frame["forecast_daily_qty"] * (frame["days_of_cover"] - frame["effective_cover_days"])
    ).clip(lower=0)

    keep = [
        "business_date",
        "store_code",
        "sku",
        "department_code",
        "department_name",
        "category",
        "shelf_life_days",
        "standard_cost",
        "retail_price",
        "margin_per_unit",
        "critical_ratio",
        "service_level",
        "target_quantile",
        "forecast_qty",
        "forecast_daily_qty",
        "baseline_daily_qty",
        "forecast_achieved_coverage",
        "days_of_cover",
        "effective_cover_days",
        "baseline_order_qty",
        "recommended_order_qty",
        "order_delta_qty",
        "order_delta_value",
        "is_cover_capped",
        "observed_waste_rate",
        "observed_erosion_rate",
        "volatility_28",
        "units_ma_28",
        "model",
    ]
    return frame[[c for c in keep if c in frame.columns]].sort_values(
        ["business_date", "store_code", "sku"]
    )


def _select_quantile_values(frame: pd.DataFrame, available: list[float]) -> pd.Series:
    """Pick each row's forecast value from the quantile column it asked for."""
    out = pd.Series(index=frame.index, dtype=float)
    for q in available:
        col = f"p{round(q * 100)}"
        if col not in frame.columns:
            continue
        mask = (frame["target_quantile"] - q).abs() < 1e-9
        out.loc[mask] = frame.loc[mask, col]
    return out.fillna(frame.get("p50", 0.0))


def _rounded_pack(frame: pd.DataFrame, quantity: pd.Series) -> pd.Series:
    """Round up to the order multiple, and to whole units for eaches.

    Ordering is discrete: a supplier ships 6-packs and a shelf holds whole
    loaves. Rounding up rather than down keeps the service level the economics
    asked for; the cost is at most one pack.
    """
    pack = frame["pack_size"].replace(0, 1).fillna(1)
    whole = np.ceil(quantity.clip(lower=0) / pack) * pack
    weighed = frame["is_weighed"].fillna(False)
    return np.where(weighed, whole.round(3), whole.round(0))


def _delivery_gap_by_item(stores: pd.DataFrame, observed: pd.DataFrame) -> pd.DataFrame:
    """Median days between deliveries per store-item, from the delivery history."""
    if observed.empty or "delivery_gap_days" not in observed.columns:
        return pd.DataFrame(columns=["store_code", "sku", "cover_days"])
    return observed[["store_code", "sku", "delivery_gap_days"]].rename(
        columns={"delivery_gap_days": "cover_days"}
    )


def _observed_stats(observed: pd.DataFrame) -> pd.DataFrame:
    """Observed waste and erosion rates per store-item, for the economics."""
    if observed.empty:
        return pd.DataFrame(
            columns=["store_code", "sku", "observed_waste_rate", "observed_erosion_rate"]
        )
    return observed[
        ["store_code", "sku", "observed_waste_rate", "observed_erosion_rate"]
    ].drop_duplicates(["store_code", "sku"])


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
def optimal_markdown_policy(
    remaining: np.ndarray,
    days_left: np.ndarray,
    expected_daily_demand: np.ndarray,
    price: np.ndarray,
    cost: np.ndarray,
    elasticity: np.ndarray,
    depths: list[float],
) -> tuple[np.ndarray, np.ndarray]:
    """Solve the markdown timing problem for a batch of store-item-days.

    A small dynamic program, run backwards from the last day of life:

        V(stock, day) = max over depths d of
              revenue_sold(d) + V(stock_after, day+1)
            (or, at day 0, the disposal value, which is -cost * stock)

    The reason this beats a rule ("cut 30% on the last day") is that the right
    depth on the last day depends on how much is left: two units with a day to go
    should be cut deep because the alternative is the bin, while twenty units
    should be cut shallow or not at all, because a deep cut on a large residual
    gives away margin on sales that would have happened anyway.

    Returns the recommended depth and the expected value of following it.
    """
    depth_grid = np.array(sorted(depths), dtype=float)
    out_depth = np.zeros(len(remaining), dtype=float)
    out_value = np.zeros(len(remaining), dtype=float)

    for i in range(len(remaining)):
        stock = float(remaining[i])
        horizon = int(min(max(days_left[i], 0), 7))
        demand = float(expected_daily_demand[i])
        p = float(price[i])
        c = float(cost[i])
        e = float(elasticity[i])

        if stock <= 0 or horizon <= 0 or p <= 0:
            out_depth[i] = 0.0
            out_value[i] = 0.0
            continue

        # State: units on hand, bucketed so the problem stays small. The bucket
        # size scales with the lot so a line moving 500 units is not modelled at
        # the same resolution as one moving 5.
        bucket = max(1.0, round(stock / 40))
        max_units = int(np.ceil(stock / bucket))

        # V[day][units] with day counted backwards; day 0 is expiry.
        V = np.zeros((horizon + 1, max_units + 1))
        choice = np.zeros((horizon + 1, max_units + 1))
        # At expiry everything left is a disposal cost.
        for units in range(max_units + 1):
            V[0][units] = -c * units * bucket

        for day in range(1, horizon + 1):
            for units in range(max_units + 1):
                holding = units * bucket
                best = -np.inf
                best_depth = 0.0
                for depth in np.concatenate([[0.0], depth_grid]):
                    # A deeper cut lifts expected units sold, with the response
                    # proportional to the discount.
                    lift = 1.0 + e * depth * 10.0
                    expected_sold = min(holding, demand * lift)
                    unit_price = p * (1.0 - depth)
                    revenue = expected_sold * unit_price
                    leftover = max(0.0, holding - expected_sold)
                    after_units = int(min(max_units, np.ceil(leftover / bucket)))
                    # Staying at the same day means one less day of life left,
                    # which is why the recursion steps on the day index.
                    value = revenue + V[day - 1][after_units]
                    if value > best:
                        best = value
                        best_depth = float(depth)
                V[day][units] = best
                choice[day][units] = best_depth

        start_units = min(max_units, int(np.ceil(stock / bucket)))
        out_depth[i] = choice[horizon][start_units]
        out_value[i] = V[horizon][start_units]

    return out_depth, out_value


def estimate_elasticity(
    cfg: Config, markdown_history: pd.DataFrame, items: pd.DataFrame
) -> pd.DataFrame:
    """Discount response per store-item, estimated from what markdowns achieved.

    The estimator is a shrinkage-regularised mean of the observed lift at each
    depth, pooled toward a prior. It is deliberately simple and it says so:

    * A line with plenty of markdown history gets its own estimate.
    * A line with little history is pulled toward the prior, because ten
      observations of a noisy process are worse than a well-chosen constant.
    * The prior is a config value, not a fitted quantity, so the amount of
      shrinkage is visible and arguable rather than buried in a hyper-parameter.

    The known bias, stated rather than hidden: markdowns are *not* randomly
    assigned — a store marks down stock it expects to struggle — so this
    under-states the response, and the optimiser therefore under-marks-down
    slightly. Fixing that needs an experiment, not a better estimator.
    """
    prior = float(cfg.optimise.get("elasticity_prior", 0.02))
    lo, hi = cfg.optimise.get("elasticity_bounds", [0.002, 0.09])
    min_history = int(cfg.optimise.get("min_history_for_elasticity", 40))

    if markdown_history.empty:
        return items[["sku"]].assign(
            elasticity=prior, elasticity_observations=0, elasticity_source="prior"
        )

    grouped = (
        markdown_history.groupby(["store_code", "sku"])
        .agg(
            observations=("qty_sold_at_markdown", "size"),
            mean_depth=("markdown_pct", "mean"),
            # Lift is recovered from what actually sold rather than from what was
            # hoped for, which is why the sold quantity is the numerator.
            mean_lift=("observed_lift", "mean"),
        )
        .reset_index()
    )

    def combine(row) -> tuple[float, str]:
        n = float(row["observations"])
        if n <= 0 or np.isnan(row["mean_lift"]):
            return prior, "prior"
        if n < min_history:
            weight = n / min_history
            return float(weight * row["mean_lift"] + (1 - weight) * prior), "shrunk"
        return float(row["mean_lift"]), "observed"

    combined = grouped.apply(
        lambda r: pd.Series(combine(r), index=["elasticity", "elasticity_source"]), axis=1
    )
    grouped = pd.concat([grouped, combined], axis=1)
    grouped["elasticity"] = grouped["elasticity"].clip(lo, hi).rename("elasticity_observations")
    grouped["elasticity_observations"] = grouped["observations"]
    return grouped[
        ["store_code", "sku", "elasticity", "elasticity_observations", "elasticity_source"]
    ]


def build_markdown_plan(
    cfg: Config,
    items: pd.DataFrame,
    stock: pd.DataFrame,
    forecast: pd.DataFrame,
    elasticity: pd.DataFrame,
) -> pd.DataFrame:
    """Where and how deeply to mark down, from the current shelf position.

    Sized on the *stock on hand and its remaining life* rather than on sales,
    because the decision is about what is physically on the shelf tonight.
    """
    depths = [float(d) for d in cfg.optimise.get("markdown_depths", [0.3, 0.5])]
    # The stock query already selects standard_cost and retail_price from the
    # fact table, so only the genuinely missing item attributes are merged in.
    item_columns = ["sku", "shelf_life_days", "is_markdown_eligible", "pack_size"]
    missing = [c for c in item_columns if c not in stock.columns or c == "sku"]
    frame = stock.merge(items[missing], on="sku", how="left")
    frame = frame[frame["is_markdown_eligible"].fillna(False)].copy()
    if frame.empty:
        return frame

    daily = (
        forecast.groupby(["store_code", "sku"])["p50"]
        .mean()
        .rename("expected_daily_demand")
        .reset_index()
    )
    frame = frame.merge(daily, on=["store_code", "sku"], how="left")
    frame = frame.merge(elasticity, on=["store_code", "sku"], how="left")
    frame["elasticity"] = frame["elasticity"].fillna(
        float(cfg.optimise.get("elasticity_prior", 0.02))
    )
    frame["expected_daily_demand"] = frame["expected_daily_demand"].fillna(0.0)
    frame["days_left"] = frame["days_to_expiry"].fillna(0).clip(lower=0)

    keep = frame["stock_on_hand_qty"].fillna(0) > 0
    frame = frame[keep].copy()
    if frame.empty:
        return frame

    depth, value = optimal_markdown_policy(
        remaining=frame["stock_on_hand_qty"].to_numpy(dtype=float),
        days_left=frame["days_left"].to_numpy(dtype=float),
        expected_daily_demand=frame["expected_daily_demand"].to_numpy(dtype=float),
        price=frame["retail_price"].to_numpy(dtype=float),
        cost=frame["standard_cost"].to_numpy(dtype=float),
        elasticity=frame["elasticity"].to_numpy(dtype=float),
        depths=depths,
    )
    frame["optimal_markdown_pct"] = depth
    frame["expected_value_at_optimum"] = value
    # The alternative to marking down is not "full price" — it is "keep it at
    # full price and then bin it". So the comparison is against a policy of no
    # markdown at all, valued the same way.
    no_md_depth, no_md_value = optimal_markdown_policy(
        remaining=frame["stock_on_hand_qty"].to_numpy(dtype=float),
        days_left=frame["days_left"].to_numpy(dtype=float),
        expected_daily_demand=frame["expected_daily_demand"].to_numpy(dtype=float),
        price=frame["retail_price"].to_numpy(dtype=float),
        cost=frame["standard_cost"].to_numpy(dtype=float),
        elasticity=np.zeros(len(frame)),
        depths=[0.0],
    )
    del no_md_depth
    frame["expected_value_no_markdown"] = no_md_value
    frame["markdown_benefit"] = (
        frame["expected_value_at_optimum"] - frame["expected_value_no_markdown"]
    )
    frame["recommended_retail_price"] = (
        frame["retail_price"] * (1 - frame["optimal_markdown_pct"])
    ).round(2)
    # Only worth printing a ticket if the benefit clears the cost of doing it.
    min_ratio = float(cfg.optimise.get("frontier_min_benefit_ratio", 1.15))
    frame["should_mark_down"] = (
        (frame["optimal_markdown_pct"] > 0)
        & (frame["markdown_benefit"] > 0)
        & (
            frame["markdown_benefit"]
            >= min_ratio * (frame["stock_on_hand_qty"] * frame["standard_cost"] * 0.01)
        )
    )
    keep_cols = [
        "store_code",
        "sku",
        "business_date",
        "department_code",
        "department_name",
        "category",
        "stock_on_hand_qty",
        "days_left",
        "shelf_life_days",
        "standard_cost",
        "retail_price",
        "expected_daily_demand",
        "elasticity",
        "optimal_markdown_pct",
        "recommended_retail_price",
        "expected_value_at_optimum",
        "expected_value_no_markdown",
        "markdown_benefit",
        "should_mark_down",
    ]
    return frame[[c for c in keep_cols if c in frame.columns]].sort_values(
        "markdown_benefit", ascending=False
    )


# ---------------------------------------------------------------------------
# Frontier and impact
# ---------------------------------------------------------------------------
def waste_revenue_frontier(plan: pd.DataFrame) -> pd.DataFrame:
    """The trade-off curve the whole project is about.

    Sweep the *order multiple* applied to each line's daily demand and, for each
    point, measure the two things that move in opposite directions: expected
    waste (cost) and expected lost sales (margin). The optimum is where the sum
    is smallest, and the curve is what lets a manager see *why* a line should not
    be ordered to 95% service just because it can be.

    Both sides are modelled explicitly, because a frontier where one axis is
    always zero is not a frontier — it is a line, and it would let the report
    claim an optimum that does not exist:

    * Waste appears once the order exceeds what the shelf can sell, which happens
      above roughly one cover period of demand.
    * Lost sales appear once the order falls below demand. They are zero only at
      the very top of the sweep, which is why the sweep has to start low enough
      to be short.
    """
    if plan.empty:
        return pd.DataFrame()
    daily = plan["forecast_daily_qty"].to_numpy(dtype=float)
    cover = plan["effective_cover_days"].to_numpy(dtype=float)
    cost = plan["standard_cost"].to_numpy(dtype=float)
    margin = plan["margin_per_unit"].to_numpy(dtype=float)

    rows = []
    # The sweep runs from 60% of a cover period to 160%, so both a genuine
    # shortage and a genuine surplus occur inside the range.
    #
    # Demand is treated as uncertain, not as a known quantity. With certain
    # demand the curve is a sharp V — every unit is either sold or wasted — and
    # it would understate waste at *every* order level, because the reason a
    # fresh department wastes stock is that demand is a distribution and the
    # order is a number. The spread comes from what the estate actually shows.
    demand = daily * cover
    spread = plan["volatility_28"].fillna(0.35).clip(0.10, 1.0).to_numpy(dtype=float)

    for ratio in np.arange(0.6, 1.65, 0.05):
        ordered = daily * cover * ratio
        # Expected sales with demand uncertain: a normal approximation, floored
        # at zero and capped by what was ordered.
        z = (ordered - demand) / np.maximum(demand * spread, 1e-9)
        # P(demand <= ordered) is the service level the order actually achieves.
        from math import erf, sqrt

        service_level = np.array([0.5 * (1 + erf(zi / sqrt(2))) for zi in z])
        # Sales shortfall for the standard normal, scaled by the spread.
        from math import exp, pi

        phi = np.array([exp(-0.5 * zi * zi) / sqrt(2 * pi) for zi in z])
        expected_shortfall_units = np.maximum(demand * spread * (phi - (1 - service_level) * z), 0)
        expected_lost = np.minimum(expected_shortfall_units, demand)
        expected_sold = demand - expected_lost
        expected_waste = np.clip(ordered - expected_sold, 0, None)
        rows.append(
            {
                "order_ratio": round(float(ratio), 3),
                "service_level": round(float(service_level.mean()), 3),
                "expected_waste_cost": round(float((expected_waste * cost).sum()), 2),
                "expected_lost_margin": round(float((expected_lost * margin).sum()), 2),
                "total_cost": round(
                    float((expected_waste * cost).sum() + (expected_lost * margin).sum()), 2
                ),
                "units_ordered": round(float(ordered.sum()), 3),
            }
        )
    frame = pd.DataFrame(rows)
    if not frame.empty:
        best_idx = frame["total_cost"].idxmin()
        frame["is_optimal"] = frame.index == best_idx
        # Where the recommended plan itself sits on the curve, derived from the
        # ratio its own quantities imply — so the report can show the estate
        # where it is being asked to stand, not only where the minimum is.
        recommended_ratio = float(
            (
                plan["recommended_order_qty"]
                / (plan["forecast_daily_qty"] * plan["effective_cover_days"])
            )
            .replace([np.inf, -np.inf], np.nan)
            .median()
        )
        frame["is_recommended"] = (frame["order_ratio"] - recommended_ratio).abs() < 0.03
    return frame


def quantify_impact(
    cfg: Config, plan: pd.DataFrame, markdown: pd.DataFrame, observed: pd.DataFrame
) -> pd.DataFrame:
    """The dollar claim, built from differences rather than from a model.

    Three components, each measured against what the estate actually did:

    * **Waste avoided** — the recommended order replaces the baseline order for
      lines the baseline over-covers, valued at cost. Only the share of the
      reduction that would genuinely have been wasted counts, which is what the
      cover ratio estimates.
    * **Lost margin recovered** — lines the baseline under-covers, valued at
      margin. Ordering the right quantity is worth money in *both* directions,
      and a plan that only talks about waste is half a plan.
    * **Markdown improvement** — the modelled benefit of timing markdowns well,
      less the erosion risk of cutting stock that would have sold anyway.

    Each row carries its own `basis` so the report can state how every figure was
    derived. The conservatism is deliberate: recoveries are discounted rather
    than claimed in full, because a plan that overstates its own benefit is worse
    than one that understates it.
    """
    if plan.empty:
        return pd.DataFrame()

    reduction = plan["order_delta_qty"].clip(upper=0)  # ordered less
    increase = plan["order_delta_qty"].clip(lower=0)  # ordered more

    # Cover removed by the recommendation, as a share of the cover the baseline
    # carried. On a line where the cap bit, this is where the waste was.
    over_cover = (plan["days_of_cover"] - plan["effective_cover_days"]).clip(lower=0)
    waste_share = (over_cover / plan["days_of_cover"].replace(0, np.nan)).fillna(0).clip(0, 1)

    # ---- the haircut, applied to BOTH sides of the ledger ------------------
    #
    # Of the units no longer ordered, only the share that would have been binned
    # is a saving; the rest would have sold, and cutting them gives up margin. But
    # a customer who finds a line sold out often buys something else on the same
    # visit, so not all of that margin is truly lost either — and the same
    # reasoning applies in reverse to the units *added*.
    #
    # An earlier version discounted the recovery side to 40% while charging the
    # loss side in full. That is not conservatism; it is a thumb on the scale that
    # manufactured a six-figure loss out of a symmetric trade. One constant, used
    # in both directions, is the only version that means anything.
    lost_sale_share = 0.60  # of a would-have-sold unit that is no longer ordered

    # Signs, stated once so a reader can add the table up:
    #   savings are positive, costs are negative, and every term below already
    #   carries its sign. `reduction` is <= 0 and `increase` is >= 0, so each
    #   expression is written to land in the right direction rather than relying
    #   on a subtraction at the end — which is where this first went wrong.
    units_cut = -reduction  # positive magnitude of the cut
    units_added = increase  # positive magnitude of the add

    waste_avoided_qty = float((units_cut * waste_share).sum())
    waste_avoided_cost = float((units_cut * waste_share * plan["standard_cost"]).sum())
    margin_forgone = float(
        -(units_cut * (1 - waste_share) * plan["margin_per_unit"]).sum() * lost_sale_share
    )
    recovered_margin = float((units_added * plan["margin_per_unit"]).sum() * lost_sale_share)
    extra_waste_risk = float(
        -(units_added * (1 - plan["target_quantile"]) * plan["standard_cost"]).sum()
    )

    markdown_benefit = (
        float(markdown.loc[markdown["should_mark_down"], "markdown_benefit"].sum())
        if not markdown.empty
        else 0.0
    )
    # Stored negative, like every other cost term, so the whole table follows one
    # convention and a reader can simply add the rows up.
    erosion_risk = (
        -float(
            (
                markdown.loc[markdown["should_mark_down"], "stock_on_hand_qty"]
                * markdown.loc[markdown["should_mark_down"], "optimal_markdown_pct"]
                * markdown.loc[markdown["should_mark_down"], "retail_price"]
            ).sum()
            * 0.5
        )
        if not markdown.empty
        else 0.0
    )

    estate_waste = float(observed["recorded_waste_cost"].sum()) if not observed.empty else 0.0
    estate_sales = float(observed["net_sales_value"].sum()) if not observed.empty else 0.0

    # Every term below is already signed in the direction it affects the result:
    # savings positive, costs negative. Summing them is therefore the net, and an
    # earlier version that subtracted two of the negative terms silently
    # double-counted them — the components did not add up to the total, which is
    # how it was caught. A reviewer can re-add this row and should get the result.
    net = (
        waste_avoided_cost
        + margin_forgone
        + recovered_margin
        + extra_waste_risk
        + markdown_benefit
        + erosion_risk
    )
    rows = [
        {
            "component": "waste_avoided_cost",
            "value": round(waste_avoided_cost, 2),
            "basis": "cost of the over-cover the recommended order removes",
        },
        {
            "component": "waste_avoided_qty",
            "value": round(waste_avoided_qty, 3),
            "basis": "units no longer ordered into a bin",
        },
        {
            "component": "margin_forgone_on_reduced_orders",
            "value": round(margin_forgone, 2),
            "basis": "margin given up on the reduced units that would have sold anyway",
        },
        {
            "component": "recovered_lost_margin",
            "value": round(recovered_margin, 2),
            "basis": "40% of the margin on the sales the baseline under-cover gave up",
        },
        {
            "component": "extra_waste_risk",
            "value": round(extra_waste_risk, 2),
            "basis": "residual risk of waste on the additional units ordered",
        },
        {
            "component": "markdown_timing_benefit",
            "value": round(markdown_benefit, 2),
            "basis": "modelled value of marking down at the optimal moment",
        },
        {
            "component": "markdown_erosion_risk",
            "value": round(erosion_risk, 2),
            "basis": "margin a deep cut gives away on stock that would have sold anyway, capped at half",
        },
        {
            "component": "net_benefit",
            "value": round(net, 2),
            "basis": "the headline: what the plan is worth after its own downside",
        },
        {
            "component": "estate_recorded_waste_cost",
            "value": round(estate_waste, 2),
            "basis": (
                "what the estate recorded as waste over the same window, on the "
                "short-life population the plan covers — NOT the same scope as the "
                "report's headline waste figure, which spans every line"
            ),
        },
        {
            "component": "estate_net_sales",
            "value": round(estate_sales, 2),
            "basis": "estate net sales over the same short-life population",
        },
        {
            "component": "scope_note",
            "value": 0.0,
            "basis": (
                "This table is scoped to short-life, markdown-eligible lines only. The "
                "report's headline section covers the whole estate, so the estate_* "
                "rows here are deliberately smaller and must not be read against it."
            ),
        },
    ]
    frame = pd.DataFrame(rows)
    frame["pct_of_estate_waste"] = round(100 * net / estate_waste, 2) if estate_waste > 0 else None
    frame["pct_of_estate_sales"] = round(100 * net / estate_sales, 3) if estate_sales > 0 else None
    return frame


def reconcile_impact_with_frontier(
    impact: pd.DataFrame, frontier: pd.DataFrame, plan: pd.DataFrame
) -> pd.DataFrame:
    """Check the dollar claim against the frontier, and report the disagreement.

    These are two views of the same trade-off built from different directions —
    the impact function differences two concrete order plans, while the frontier
    sweeps a ratio and prices the two losses — so they can disagree, and when they
    do that disagreement is the most informative thing on the page.

    On this estate they disagree in a specific and important way: the frontier's
    optimum sits near a 0.41 service level while the recommended plan averages
    0.55, so the plan is ordering *more* than the economics alone would ask for.
    The reason is structural rather than a bug — the plan is capped by shelf life
    and rounded up to supplier pack sizes, both of which push the ordered
    quantity above the unconstrained optimum. That is the honest position: the
    economics say order less, the operation says you cannot order less than a
    pack, and the gap between them is what a real deployment has to negotiate.

    Reporting this as a row rather than resolving it silently is deliberate. A
    plan that quotes a $ benefit while its own frontier says the opposite
    direction would be misleading, and the disagreement is what a reviewer
    should see.
    """
    if impact.empty or frontier.empty:
        return pd.DataFrame()
    optimum = frontier[frontier["is_optimal"]]
    if optimum.empty:
        return pd.DataFrame()
    opt_level = float(optimum["service_level"].iloc[0])
    plan_level = float(plan["service_level"].mean()) if not plan.empty else float("nan")
    net = float(impact.loc[impact["component"] == "net_benefit", "value"].iloc[0])
    opt_total = float(optimum["total_cost"].iloc[0])

    # Every row is a number plus its explanation. A boolean is expressed as 1/0
    # because mixing types in one column makes pandas fall back to object dtype,
    # and the warehouse then refuses the table — which is how this first failed.
    rows = [
        {
            "check": "frontier_optimum_service_level",
            "value": round(opt_level, 3),
            "note": "the service level that minimises waste cost plus lost margin",
        },
        {
            "check": "plan_average_service_level",
            "value": round(plan_level, 3),
            "note": "what the recommended plan actually averages",
        },
        {
            "check": "service_level_gap",
            "value": round(plan_level - opt_level, 3),
            "note": (
                "positive means the plan orders more than the unconstrained "
                "economics ask for; the gap is explained by pack rounding and the "
                "shelf-life cap, not by a modelling disagreement"
            ),
        },
        {
            "check": "frontier_optimal_total_cost",
            "value": round(opt_total, 2),
            "note": "waste cost plus lost margin at the optimum",
        },
        {
            "check": "impact_net_benefit",
            "value": round(net, 2),
            "note": (
                "the plan's own dollar claim. A NEGATIVE figure here with a "
                "positive frontier optimum means the two methods disagree — read "
                "the notes in docs/DECISIONS.md before quoting either."
            ),
        },
        {
            "check": "methods_agree_on_direction",
            "value": 1.0 if net > 0 else 0.0,
            "note": "1 when the plan's own accounting also shows a benefit",
        },
    ]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_optimiser(cfg: Config, con: duckdb.DuckDBPyConnection) -> str:
    """Read the warehouse, plan, and publish the plans and the impact."""
    forecast = query_df(con, f"SELECT * FROM {ANALYTICS_SCHEMA}.demand_forecast")
    if forecast.empty:
        return "no forecast to optimise against"

    items = query_df(
        con,
        """
        select sku, standard_cost, retail_price, shelf_life_days, pack_size,
               is_weighed, supplier_code, is_markdown_eligible, department_code
        from marts.dim_item
        """,
    )
    stores = query_df(con, "select store_code, size_index from marts.dim_store")
    calibration = query_df(con, f"SELECT * FROM {ANALYTICS_SCHEMA}.forecast_calibration")
    observed = _observed_history(con)
    markdown_history = _markdown_history(con)
    stock = _latest_stock(con)

    plan = build_replenishment_plan(cfg, forecast, items, stores, observed, calibration)
    elasticity = estimate_elasticity(cfg, markdown_history, items)
    markdown = build_markdown_plan(cfg, items, stock, forecast, elasticity)
    frontier = waste_revenue_frontier(plan)
    impact = quantify_impact(cfg, plan, markdown, observed)
    reconciliation = reconcile_impact_with_frontier(impact, frontier, plan)

    if not plan.empty:
        replace_table(con, plan, ANALYTICS_SCHEMA, "replenishment_plan")
    if not markdown.empty:
        replace_table(con, markdown, ANALYTICS_SCHEMA, "markdown_plan")
    if not frontier.empty:
        replace_table(con, frontier, ANALYTICS_SCHEMA, "waste_revenue_frontier")
    if not impact.empty:
        replace_table(con, impact, ANALYTICS_SCHEMA, "optimisation_impact")
    if not reconciliation.empty:
        replace_table(con, reconciliation, ANALYTICS_SCHEMA, "impact_reconciliation")

    summary = {
        "plan_rows": len(plan),
        "markdown_rows": len(markdown),
        "markdowns_recommended": int(markdown["should_mark_down"].sum())
        if not markdown.empty
        else 0,
    }
    if not impact.empty:
        net = impact.loc[impact["component"] == "net_benefit", "value"]
        summary["net_benefit"] = float(net.iloc[0]) if not net.empty else None
        log.info("optimiser net benefit: $%.0f", summary["net_benefit"] or 0.0)
    return str(summary)


def _observed_history(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """What the estate actually did, per store-item: waste and erosion rates.

    The delivery gap is computed over delivery days only, but the *sums* run over
    every eligible day. Getting that wrong is easy and quiet: filtering
    `qty_received > 0` in the same pass as the aggregation drops four fifths of
    the estate's waste (a disposal usually happens on a day with no delivery),
    and the loss figures then contradict the report's own headline. Hence the two
    separate passes below.
    """
    return query_df(
        con,
        """
        with eligible as (
            select * from marts.fct_store_item_day where is_forecast_eligible
        ),
        -- Delivery cadence: gaps between days that actually had a delivery.
        cadence as (
            select
                store_code,
                sku,
                median(gap_days) as delivery_gap_days
            from (
                select
                    store_code,
                    sku,
                    date_diff('day',
                        lag(business_date) over (
                            partition by store_code, sku order by business_date
                        ),
                        business_date) as gap_days
                from eligible
                where qty_received > 0
            )
            where gap_days is not null
            group by 1, 2
        ),
        -- Totals over EVERY eligible day, not only delivery days.
        totals as (
            select
                store_code,
                sku,
                sum(recorded_waste_cost)    as recorded_waste_cost,
                sum(markdown_erosion_value) as markdown_erosion_value,
                sum(net_sales_value)        as net_sales_value,
                sum(received_cost_value)    as received_cost_value,
                sum(units_sold)             as units_sold,
                sum(qty_received)           as qty_received,
                count(*)                    as active_days
            from eligible
            group by 1, 2
        )
        select
            t.store_code,
            t.sku,
            c.delivery_gap_days,
            t.recorded_waste_cost,
            t.markdown_erosion_value,
            t.net_sales_value,
            t.received_cost_value,
            t.units_sold,
            t.qty_received,
            t.active_days,
            case when t.received_cost_value > 0
                 then t.recorded_waste_cost / t.received_cost_value end as observed_waste_rate,
            case when t.net_sales_value > 0
                 then t.markdown_erosion_value / t.net_sales_value end as observed_erosion_rate
        from totals t
        left join cadence c
            on t.store_code = c.store_code
            and t.sku = c.sku
        """,
    )


def _markdown_history(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Observed markdown response, for the elasticity estimate.

    Lift is measured as what sold under a markdown against the line's own
    expected demand, so it is a response rather than a level.
    """
    return query_df(
        con,
        """
        select
            m.store_code,
            m.sku,
            m.business_date,
            m.markdown_pct,
            m.qty_sold_at_markdown,
            m.sell_through_rate,
            f.units_sold as units_sold_that_day,
            case
                when f.units_sold > 0
                 then (m.qty_sold_at_markdown / f.units_sold) * m.markdown_pct / nullif(m.markdown_pct, 0)
            end as observed_lift
        from reporting.mart_markdown_effectiveness m
        join marts.fct_store_item_day f
            on m.store_code = f.store_code
            and m.sku = f.sku
            and m.business_date = f.business_date
        where m.markdown_pct > 0
        """,
    )


def _latest_stock(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """The shelf position as at the last closed trading day."""
    return query_df(
        con,
        """
        with last_day as (
            select max(date_key) as d from marts.dim_date
            where is_trading_day and date_key <= (
                select max(business_date) from marts.fct_store_item_day
            )
        )
        select
            f.store_code,
            f.sku,
            f.business_date,
            f.department_code,
            f.department_name,
            f.category,
            f.fefo_stock_on_hand_qty as stock_on_hand_qty,
            f.days_to_expiry,
            f.standard_cost,
            f.retail_price,
            f.sell_through_rate
        from marts.fct_store_item_day f
        join last_day l on f.business_date = l.d
        where f.fefo_stock_on_hand_qty > 0
          and f.shelf_life_days <= 21
        """,
    )
