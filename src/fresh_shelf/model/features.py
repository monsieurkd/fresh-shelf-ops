"""Feature engineering for the demand model.

One row per store-item-day, with every feature built so it could have been known
*before* the day it describes. That constraint is the whole discipline of this
file, and it is easy to violate by accident in ways that never raise:

* A `rolling(window).mean()` that includes the current row leaks the answer.
  Every rolling feature here is shifted by one day first, via the window frame
  `rows between N preceding and 1 preceding`.
* A promotion flag is fine as a feature because the plan is published in advance
  and loaded as date ranges. The flag for a day is knowable before that day.
* Weather is a known-future variable in reality (a 7-day forecast is good), but
  the simulation gives the model *perfect* knowledge of it. That makes the
  backtest a little flattering, and the report says so rather than hiding it.

Features are computed in DuckDB rather than pandas because the window functions
are the same ones the warehouse already uses, so a lag convention cannot differ
between the two layers.
"""

from __future__ import annotations

import duckdb

#: Days of history a forecast needs before the model is trusted. Below this the
#: model falls back to the day-of-week mean, which is the honest thing to do with
#: three weeks of data.
MIN_HISTORY_DAYS = 56

#: Rolling windows used for momentum. 7 captures the week, 28 the month; the
#: ratio between them is what detects a line that is rising or dying.
ROLLING_WINDOWS = (7, 14, 28)

#: The features the model may use. Listed explicitly so a new column cannot
#: silently join the model, and so a reviewer can see the whole input surface.
FEATURE_COLUMNS = (
    "isodow",
    "is_weekend",
    "month_number",
    "days_before_christmas",
    "max_temp_c",
    "rainfall_mm",
    "degrees_over_25c",
    "is_heat_day",
    "discount_pct",
    "is_on_promo",
    "is_catalogue_feature",
    "units_ma_7",
    "units_ma_14",
    "units_ma_28",
    "units_sd_7",
    "units_sd_28",
    "momentum_7_28",
    "volatility_28",
    "sell_through_rate",
    "days_to_expiry",
)

TARGET = "units_sold"


def _window_defs() -> str:
    return ",\n            ".join(
        f"w{n} as (partition by store_code, sku order by business_date "
        f"rows between {n} preceding and 1 preceding)"
        for n in ROLLING_WINDOWS
    )


def _window_aggs() -> str:
    parts = []
    for n in ROLLING_WINDOWS:
        parts.append(f"avg(units_sold) over w{n}                         as units_ma_{n}")
        parts.append(f"stddev_pop(units_sold) over w{n}                  as units_sd_{n}")
    return ",\n            ".join(parts)


def build_features_sql() -> str:
    """The feature query, as SQL.

    Returned as a string so the same text can be run, explained, or printed in a
    review. A feature definition buried under a chain of pandas calls is much
    harder to argue about.
    """
    return f"""
    with base as (

        select
            f.store_code,
            f.sku,
            f.business_date,
            f.department_code,
            f.department_name,
            f.category,
            f.standard_cost,
            f.retail_price,
            f.shelf_life_days,
            f.is_weighed,
            f.units_sold,
            f.markdown_erosion_value,
            f.recorded_waste_cost,
            f.qty_received,
            f.sell_through_rate,
            f.days_to_expiry,
            f.is_closed,
            f.is_weekend,
            d.isodow,
            d.month_number,
            f.trading_context,
            f.days_before_christmas,
            count(*) over (partition by f.store_code, f.sku) as history_days
        from marts.fct_store_item_day f
        join marts.dim_date d on f.business_date = d.date_key
        where f.is_forecast_eligible
          and not f.is_closed
    ),

    -- Weather and promotions joined at their natural grain. Both are known in
    -- advance in reality, a forecast and a published plan, which is why they
    -- may sit on the current row rather than being lagged.
    enriched as (

        select
            b.*,
            w.max_temp_c,
            w.rainfall_mm,
            w.degrees_over_25c,
            w.is_heat_day,
            p.discount_pct,
            p.is_catalogue_feature,
            (p.sku is not null)                              as is_on_promo
        from base b
        left join staging.stg_weather w
            on b.store_code = w.store_code
            and b.business_date = w.weather_date
        left join staging.stg_promotions p
            on b.sku = p.sku
            and b.business_date between p.start_date and p.end_date
    ),

    -- Every rolling statistic looks strictly backwards: `1 preceding` means the
    -- current row's own outcome is never in its own features.
    lagged as (

        select
            e.*,
            {_window_aggs()}
        from enriched e
        window
            {_window_defs()}
    )

    select
        *,
        -- Momentum: this week against this month. Above 1 the line is growing,
        -- below 1 it is dying. A dying line is where waste concentrates.
        case
            when units_ma_28 > 0 then units_ma_7 / units_ma_28
        end                                                  as momentum_7_28,
        -- Volatility against level. A slow mover with the same mean is far harder
        -- to order than a fast one, and this is the feature that says so.
        case
            when units_ma_28 > 0 then units_sd_28 / units_ma_28
        end                                                  as volatility_28
    from lagged
    """


def feature_frame(con: duckdb.DuckDBPyConnection):
    """Materialise the feature frame as a pandas DataFrame."""
    return con.execute(build_features_sql()).fetch_df()


def history_days_column() -> str:
    return "history_days"
