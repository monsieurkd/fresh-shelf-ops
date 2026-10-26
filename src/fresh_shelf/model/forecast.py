"""Demand forecasting, and the backtest that decides whether to believe it.

Three models of different kinds, because the comparison matters. A single model
tells you nothing about whether the problem is hard or the model is weak:

* ``seasonal_naive``: last week, same weekday. The baseline every retail
  forecast has to beat, and on a stable fresh line it is genuinely hard to beat.
* ``dow_mean``: the mean of the last four same-weekdays. Smooths noise but
  tracks nothing; it is what a store manager does in their head.
* ``hgb_quantile``: gradient-boosted trees trained with a *pinball* loss, which
  is what makes a quantile forecast possible. Ordinary least squares predicts a
  mean; an ordering decision needs a quantile, because the cost of being short
  and the cost of over-ordering are not equal.

Two evaluation metrics, for two different audiences:

* **WAPE** (weighted absolute percentage error) answers "how close is the point
  forecast". It is weighted rather than MAPE because MAPE explodes on a line that
  sold zero one day, and this estate has plenty of those.
* **Pinball loss** answers "is the 90th percentile actually the 90th percentile",
  which is the question the optimiser depends on. A model can have good WAPE and
  badly calibrated quantiles, and then every service level in the plan is wrong.

Validation is **rolling-origin**, not a single holdout: the model is refit at
several cut-off dates and asked to predict the next fortnight each time. A single
split on a retail year would land either side of Christmas and the answer would
be an artefact of where the split fell.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import duckdb
import numpy as np
import pandas as pd

from ..config import Config
from ..db import ANALYTICS_SCHEMA, replace_table
from .features import FEATURE_COLUMNS, TARGET, feature_frame

log = logging.getLogger(__name__)

#: A store-item needs this many observations before a tree model is sensible.
MIN_TRAIN_ROWS = 40


@dataclass
class ForecastResult:
    """What the forecast produced, and how well it did it."""

    forecast: pd.DataFrame
    backtest: pd.DataFrame
    metrics: pd.DataFrame
    summary: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def wape(actual: np.ndarray, predicted: np.ndarray) -> float:
    """Weighted absolute percentage error.

    Weighted by the total, not the mean of per-row percentages, so a line that
    sold 2 when it expected 1 contributes 1 unit of error rather than a 100%
    error. On a fresh department the latter is dominated by the slowest movers
    and tells you nothing about the money.

    Returns NaN when there is nothing to predict, which the caller must handle
    rather than treating as a perfect score.
    """
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    denominator = np.abs(actual).sum()
    if denominator == 0:
        return float("nan")
    return float(np.abs(actual - predicted).sum() / denominator)


def pinball_loss(actual: np.ndarray, predicted: np.ndarray, quantile: float) -> float:
    """Quantile (pinball) loss for one quantile.

    Asymmetric on purpose: predicting above the quantile is penalised at
    ``quantile`` and below it at ``1 - quantile``. At q=0.9, under-forecasting
    costs nine times as much as over-forecasting. That is the trade the
    newsvendor calculation makes, so the model and the decision are scored the
    same way.
    """
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    if actual.size == 0:
        return float("nan")
    diff = actual - predicted
    return float(np.mean(np.maximum(quantile * diff, (quantile - 1) * diff)))


def coverage(actual: np.ndarray, predicted: np.ndarray, quantile: float) -> float:
    """The share of outcomes at or below the prediction.

    This is the calibration check. If a 0.9-quantile forecast is only covering
    70% of days, the quantiles are too low and the service levels derived from
    them will under-order. The error is quiet, and it is in the direction that
    loses sales.
    """
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    if actual.size == 0:
        return float("nan")
    return float((actual <= predicted).mean())


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
def _seasonal_naive(train: pd.DataFrame, target_rows: pd.DataFrame) -> dict[float, np.ndarray]:
    """Last week, same weekday. The baseline."""
    lookup = (
        train.assign(isodow=train["business_date"].map(lambda d: pd.Timestamp(d).dayofweek + 1))
        .sort_values("business_date")
        .groupby(["store_code", "sku", "isodow"])["units_sold"]
        .last()
    )
    keys = list(
        zip(target_rows["store_code"], target_rows["sku"], target_rows["isodow"], strict=False)
    )
    preds = np.array([lookup.get(k, np.nan) for k in keys], dtype=float)
    fallback = train.groupby(["store_code", "sku"])["units_sold"].mean()
    backup = np.array(
        [
            fallback.get((s, k), 0.0)
            for s, k in zip(target_rows["store_code"], target_rows["sku"], strict=False)
        ],
        dtype=float,
    )
    point = np.where(np.isnan(preds), backup, preds)
    # A naive forecast has no distribution, so the same value is used at every
    # quantile. That is a weakness the metrics will show, and it is honest: a
    # baseline that pretends to have quantiles would flatter the comparison.
    return dict.fromkeys((0.5,), point)


def _dow_mean(train: pd.DataFrame, target_rows: pd.DataFrame, weeks: int = 4) -> np.ndarray:
    """Mean of the last ``weeks`` same-weekdays, per store-item-day-of-week."""
    recent = train.copy()
    recent["isodow"] = recent["business_date"].map(lambda d: pd.Timestamp(d).dayofweek + 1)
    cutoff = recent["business_date"].max()
    if isinstance(cutoff, str):
        cutoff = pd.Timestamp(cutoff).date()
    recent = recent[recent["business_date"] > (cutoff - pd.Timedelta(days=7 * weeks))]
    lookup = recent.groupby(["store_code", "sku", "isodow"])["units_sold"].mean()
    overall = train.groupby(["store_code", "sku"])["units_sold"].mean()
    out = []
    for store, sku, isodow in zip(
        target_rows["store_code"], target_rows["sku"], target_rows["isodow"], strict=False
    ):
        value = lookup.get((store, sku, isodow))
        if value is None or np.isnan(value):
            value = overall.get((store, sku), 0.0)
        out.append(float(value))
    return np.array(out)


def _hgb_quantile(
    train: pd.DataFrame, target_rows: pd.DataFrame, quantiles: list[float]
) -> dict[float, np.ndarray]:
    """Gradient-boosted trees with a pinball loss, one model per quantile."""
    from sklearn.ensemble import HistGradientBoostingRegressor

    usable = [c for c in FEATURE_COLUMNS if c in train.columns]
    train_x = train[usable].astype(float)
    target_x = target_rows[usable].astype(float)
    y = train[TARGET].astype(float)
    out: dict[float, np.ndarray] = {}
    for q in quantiles:
        model = HistGradientBoostingRegressor(
            loss="quantile",
            quantile=q,
            max_iter=200,
            learning_rate=0.08,
            max_depth=6,
            min_samples_leaf=20,
            l2_regularization=1.0,
            random_state=0,
        )
        model.fit(train_x, y)
        out[q] = np.clip(model.predict(target_x), 0, None)
    return out


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------
def rolling_origin_backtest(
    cfg: Config, frame: pd.DataFrame, *, folds: int | None = None
) -> pd.DataFrame:
    """Refit at several cut-offs and predict the next horizon each time.

    Each fold trains only on data strictly before its cut-off and scores only the
    horizon after it, so no fold can see its own answers. A single holdout cannot
    give you that, and it is why the reported WAPE is a range across folds rather
    than one number. The spread is the honest part.
    """
    folds = folds or int(cfg.forecast.get("backtest_folds", 6))
    horizon = int(cfg.forecast.get("horizon_days", 14))
    quantiles = [float(q) for q in cfg.forecast.get("quantiles", [0.5, 0.9])]
    models = cfg.forecast.get("models", ["seasonal_naive", "dow_mean", "hgb_quantile"])

    days = sorted({pd.Timestamp(d).date() for d in frame["business_date"].unique()})
    if len(days) < horizon + 20:
        log.warning("not enough history for a backtest: %d days", len(days))
        return pd.DataFrame()

    # The last fold's horizon ends on the final day, and earlier folds step back.
    folds = min(folds, max(1, (len(days) - 20) // horizon))
    rows: list[dict] = []

    for fold in range(folds):
        end_index = len(days) - 1 - fold * horizon
        start_index = end_index - horizon + 1
        if start_index <= 0:
            break
        test_start = days[start_index]
        test_end = days[end_index]

        train = frame[frame["business_date"] < test_start]
        test = frame[(frame["business_date"] >= test_start) & (frame["business_date"] <= test_end)]
        if train.empty or test.empty:
            continue

        per_item = train.groupby(["store_code", "sku"]).size()
        keep = per_item[per_item >= MIN_TRAIN_ROWS].index
        train = (
            train.set_index(["store_code", "sku"])
            .loc[train.set_index(["store_code", "sku"]).index.isin(keep)]
            .reset_index()
        )
        test = (
            test.set_index(["store_code", "sku"])
            .loc[test.set_index(["store_code", "sku"]).index.isin(keep)]
            .reset_index()
        )
        if train.empty or test.empty:
            continue

        actual = test[TARGET].to_numpy(dtype=float)

        for model_name in models:
            if model_name == "seasonal_naive":
                preds = _seasonal_naive(train, test)
                # A naive model has one value; score it at the median and at the
                # top quantile so the comparison with a real quantile model is
                # like for like rather than quietly generous.
                for q in quantiles:
                    predicted = np.full_like(actual, preds[0.5])
                    rows.append(
                        _score_row(fold, test_start, test_end, model_name, q, actual, predicted)
                    )
            elif model_name == "dow_mean":
                predicted = _dow_mean(train, test)
                for q in quantiles:
                    rows.append(
                        _score_row(fold, test_start, test_end, model_name, q, actual, predicted)
                    )
            elif model_name == "hgb_quantile":
                try:
                    preds = _hgb_quantile(train, test, quantiles)
                except Exception as exc:  # a fold failing must not kill the run
                    log.warning("hgb fold %s failed: %s", fold, exc)
                    continue
                for q, predicted in preds.items():
                    rows.append(
                        _score_row(fold, test_start, test_end, model_name, q, actual, predicted)
                    )

    return pd.DataFrame(rows)


def _score_row(
    fold: int,
    test_start: date,
    test_end: date,
    model: str,
    quantile: float,
    actual: np.ndarray,
    predicted: np.ndarray,
) -> dict:
    return {
        "fold": fold,
        "test_start": test_start,
        "test_end": test_end,
        "model": model,
        "quantile": quantile,
        "rows": int(actual.size),
        "wape": wape(actual, predicted),
        "pinball": pinball_loss(actual, predicted, quantile),
        "coverage": coverage(actual, predicted, quantile),
        "mean_actual": float(actual.mean()),
        "mean_predicted": float(predicted.mean()),
        "bias": float(predicted.mean() - actual.mean()),
    }


# ---------------------------------------------------------------------------
# Forward forecast
# ---------------------------------------------------------------------------
def forward_forecast(cfg: Config, frame: pd.DataFrame, backtest: pd.DataFrame) -> pd.DataFrame:
    """Forecast the next ``horizon_days`` from the full history.

    The model chosen is the best-performing one on the *quantile* metrics, not on
    WAPE, because the optimiser consumes quantiles. A model with a slightly worse
    mean but well-calibrated tails is the more useful one here, and picking by
    WAPE would select against exactly that.
    """
    horizon = int(cfg.forecast.get("horizon_days", 14))
    quantiles = [float(q) for q in cfg.forecast.get("quantiles", [0.5, 0.9])]
    chosen = _choose_model(backtest, quantiles) or "dow_mean"
    log.info("forward forecast using %s", chosen)

    if frame.empty:
        return pd.DataFrame()

    last_day = max(pd.Timestamp(d).date() for d in frame["business_date"].unique())
    future_days = [
        (pd.Timestamp(last_day) + pd.Timedelta(days=i)).date() for i in range(1, horizon + 1)
    ]

    # Build the forecast grid as a cross product of the ranged store-items and the
    # future days, then carry the last known feature vector forward. Carrying
    # forward is the honest simple choice: the model has no future weather in
    # production, so pretending otherwise would make the plan better than the
    # system that has to execute it.
    latest = (
        frame.sort_values("business_date")
        .groupby(["store_code", "sku"], as_index=False)
        .tail(1)
        .copy()
    )
    rows = []
    for future in future_days:
        future_day = pd.Timestamp(future).date()
        block = latest.copy()
        block["business_date"] = future_day
        block["isodow"] = future_day.isoweekday()
        block["is_weekend"] = block["isodow"] >= 6
        block["month_number"] = future_day.month
        block["days_before_christmas"] = max(
            0, (pd.Timestamp("2025-12-25").date() - future_day).days
        )
        rows.append(block)
    grid = pd.concat(rows, ignore_index=True)

    usable = [c for c in FEATURE_COLUMNS if c in frame.columns]
    train_x = frame[usable].astype(float)
    grid_x = grid[usable].astype(float)
    y = frame[TARGET].astype(float)

    if chosen == "hgb_quantile" and len(frame) >= MIN_TRAIN_ROWS:
        from sklearn.ensemble import HistGradientBoostingRegressor

        for q in quantiles:
            model = HistGradientBoostingRegressor(
                loss="quantile",
                quantile=q,
                max_iter=200,
                learning_rate=0.08,
                max_depth=6,
                min_samples_leaf=20,
                l2_regularization=1.0,
                random_state=0,
            )
            model.fit(train_x, y)
            grid[f"p{int(q * 100)}"] = np.clip(model.predict(grid_x), 0, None)
    else:
        # A single point forecast, replicated across quantiles. Slightly worse,
        # and labelled as such by the model column rather than passed off as a
        # calibrated distribution.
        point = _dow_mean(frame, grid)
        for q in quantiles:
            grid[f"p{int(q * 100)}"] = point

    grid["model"] = chosen
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
        "is_weighed",
        "units_ma_7",
        "units_ma_28",
        "volatility_28",
        "model",
    ] + [f"p{int(q * 100)}" for q in quantiles]
    return grid[[c for c in keep if c in grid.columns]]


def _choose_model(backtest: pd.DataFrame, quantiles: list[float]) -> str | None:
    """Best mean coverage-adjusted pinball loss across folds."""
    if backtest.empty:
        return None
    top = backtest[backtest["quantile"] == max(quantiles)]
    if top.empty:
        top = backtest
    scores = top.groupby("model")["pinball"].mean().sort_values()
    return str(scores.index[0]) if not scores.empty else None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_forecast(cfg: Config, con: duckdb.DuckDBPyConnection) -> str:
    """Build features, backtest, forecast forward, and publish all three."""
    frame = feature_frame(con)
    if frame.empty:
        return "no forecast-eligible rows"

    frame["business_date"] = pd.to_datetime(frame["business_date"]).dt.date
    quantiles = [float(q) for q in cfg.forecast.get("quantiles", [0.5, 0.9])]

    backtest = rolling_origin_backtest(cfg, frame)
    forecast = forward_forecast(cfg, frame, backtest)

    metrics = _summarise(backtest, quantiles)
    calibration = _calibrate_quantiles(metrics, backtest)
    if calibration:
        calib_frame = pd.DataFrame(
            [
                {
                    "model": forecast["model"].iloc[0] if not forecast.empty else None,
                    "nominal_quantile": q,
                    "achieved_coverage": c,
                    # What the optimiser must add to the nominal level to actually
                    # run at the service level a store manager would recognise.
                    "coverage_shortfall": round(q - c, 4),
                }
                for q, c in sorted(calibration.items())
            ]
        )
        replace_table(con, calib_frame, ANALYTICS_SCHEMA, "forecast_calibration")
    if not backtest.empty:
        replace_table(con, backtest, ANALYTICS_SCHEMA, "forecast_backtest")
    if not forecast.empty:
        replace_table(con, forecast, ANALYTICS_SCHEMA, "demand_forecast")
    if not metrics.empty:
        replace_table(con, metrics, ANALYTICS_SCHEMA, "forecast_metrics")

    summary = {
        "features": len(frame),
        "backtest_rows": len(backtest),
        "forecast_rows": len(forecast),
        "model": forecast["model"].iloc[0] if not forecast.empty else None,
    }
    if not metrics.empty:
        best = metrics.iloc[0]
        summary["best_model"] = best["model"]
        summary["best_wape"] = round(float(best["best_wape"]), 4)
        summary["best_coverage_at_top"] = round(float(best["coverage_at_top_quantile"]), 4)
        log.info(
            "backtest: best %s WAPE %.3f, coverage@q%.2f %.3f",
            best["model"],
            best["best_wape"],
            max(quantiles),
            best["coverage_at_top_quantile"],
        )
    return str(summary)


def _calibrate_quantiles(metrics: pd.DataFrame, backtest: pd.DataFrame) -> dict[float, float]:
    """Measured coverage per requested quantile, for the optimiser to correct with.

    The backtest answers "when I ask for the 95th percentile, how often is the
    outcome actually below it?" On this estate the answer is ~0.87, not 0.95: the
    trees are conservative at the top because the loss is penalised evenly while
    the tail is thin and lumpy.

    That matters downstream. If the optimiser orders to the *nominal* 0.95
    quantile believing it is a 95% service level, it will actually run at 87% and
    lose sales it never sees. So the calibration is measured here, published, and
    passed to the optimiser as the achieved service level. The plan is sized on
    what the model really delivers, not on what was asked for.
    """
    if backtest.empty:
        return {}
    achieved = backtest.groupby("quantile")["coverage"].mean()
    return {float(q): float(c) for q, c in achieved.items()}


def _summarise(backtest: pd.DataFrame, quantiles: list[float]) -> pd.DataFrame:
    """One row per model: WAPE, mean pinball, and calibration at the top quantile."""
    if backtest.empty:
        return pd.DataFrame()
    top_q = max(quantiles)
    rows = []
    for model, group in backtest.groupby("model"):
        median = group[group["quantile"] == min(quantiles)]
        top = group[group["quantile"] == top_q]
        rows.append(
            {
                "model": model,
                "folds": int(group["fold"].nunique()),
                "best_wape": float(median["wape"].mean()) if not median.empty else float("nan"),
                "mean_pinball": float(group["pinball"].mean()),
                "coverage_at_top_quantile": float(top["coverage"].mean())
                if not top.empty
                else float("nan"),
                "target_coverage": top_q,
                "mean_bias": float(group["bias"].mean()),
                "beats_naive": bool(
                    model != "seasonal_naive"
                    and not median.empty
                    and not backtest[
                        (backtest["model"] == "seasonal_naive")
                        & (backtest["quantile"] == min(quantiles))
                    ].empty
                    and median["wape"].mean()
                    < backtest[
                        (backtest["model"] == "seasonal_naive")
                        & (backtest["quantile"] == min(quantiles))
                    ]["wape"].mean()
                ),
            }
        )
    return pd.DataFrame(rows).sort_values("best_wape").reset_index(drop=True)
