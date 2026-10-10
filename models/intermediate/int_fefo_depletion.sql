{{ config(materialized='table') }}

{#-
  FEFO depletion: how shelf life actually burns down, per store-item-day.

  The ledger says what happened to *quantities*. This says what happened to
  *life*, and the difference matters because two stores can waste the same
  number of units for opposite reasons — one because it bought too much, one
  because it bought the same amount too late.

  The model is a FIFO queue per store-item over the deliveries in the window,
  advanced one day at a time. Sales consume the earliest-expiring stock first
  (which is what a well-run fresh counter does, and what the simulation
  assumes), so the age of what is left at the end of the day is a direct
  consequence of the ordering pattern. That is the mechanism the optimiser has
  to change: order the same volume with a better cadence and the same shelf
  breathes longer.

  `last_safe_sale_date` is the operative column. Everything downstream that has
  to decide "mark it down or lose it" counts from that date, not from the
  delivery date, because life at delivery already varies (D12: a substitution
  arrives with a shorter one).
-#}
with calendar as (

    select trading_date, is_closed
    from {{ ref('stg_trading_calendar') }}

),

deliveries as (

    select
        store_code,
        sku,
        delivery_date,
        expiry_date,
        sum(qty_received)                                    as qty_received,
        sum(received_cost_value)                             as received_cost_value,
        max(days_of_life_at_delivery)                        as days_of_life_at_delivery,
        max(was_substituted)                                 as was_substituted,
        count(*)                                             as delivery_lines
    from {{ ref('stg_goods_received') }}
    group by 1, 2, 3, 4

),

sales as (

    select store_code, business_date, sku, sum(units_sold) as units_sold
    from {{ ref('stg_pos_sales') }}
    group by 1, 2, 3

),

-- Every store-item-day, whether or not anything happened. A day with no
-- delivery is where life quietly runs out, and it is the day that matters.
spine as (

    select
        d.store_code,
        d.sku,
        c.trading_date,
        coalesce(del.qty_received, 0)                          as qty_received_today,
        coalesce(del.received_cost_value, 0)                   as received_cost_value_today,
        coalesce(sa.units_sold, 0)                             as units_sold_today,
        del.days_of_life_at_delivery,
        del.was_substituted                                    as had_substitution,
        del.expiry_date                                        as latest_expiry
    from (select distinct store_code, sku from deliveries) d
    cross join calendar c
    left join deliveries del
        on d.store_code = del.store_code
        and d.sku = del.sku
        and c.trading_date = del.delivery_date
    left join sales sa
        on d.store_code = sa.store_code
        and d.sku = sa.sku
        and c.trading_date = sa.business_date
    where not c.is_closed

),

-- Running totals, computed over the assembled spine. These have to run *after*
-- the per-day aggregation: a window function cannot sit next to a GROUP BY.
running as (

    select
        store_code,
        sku,
        trading_date,
        qty_received_today,
        units_sold_today,
        received_cost_value_today,
        days_of_life_at_delivery,
        had_substitution,
        latest_expiry,
        sum(qty_received_today) over (
            partition by store_code, sku order by trading_date
            rows between unbounded preceding and current row
        )                                                     as cumulative_received,
        sum(units_sold_today) over (
            partition by store_code, sku order by trading_date
            rows between unbounded preceding and current row
        )                                                     as cumulative_sold
    from spine
),

-- The stock remaining is a delivery-cohort book. Under FEFO the units still
-- unsold are the most recently delivered ones, so what is left on the shelf is
-- `cumulative_received - cumulative_sold` units of the youngest cohorts.
aged as (

    select
        store_code,
        sku,
        trading_date,
        qty_received_today,
        units_sold_today,
        cumulative_received,
        cumulative_sold,
        received_cost_value_today,
        days_of_life_at_delivery,
        had_substitution,
        latest_expiry,
        greatest(0, cumulative_received - cumulative_sold)     as stock_on_hand_qty
    from running
)

select
    store_code,
    sku,
    trading_date,
    qty_received_today,
    units_sold_today,
    cumulative_received,
    cumulative_sold,
    stock_on_hand_qty,
    days_of_life_at_delivery,
    had_substitution,
    latest_expiry,
    received_cost_value_today,
    -- The column the markdown decision counts from. A line with 3 days of life
    -- at delivery that was received 2 days ago has 1 day left to sell it.
    case
        when latest_expiry is not null
            then cast(date_diff('day', trading_date, latest_expiry) as integer)
    end                                                       as days_to_expiry,
    -- Stock is already unsellable: there is nothing left to optimise, only to
    -- account for. Separating it matters because "expired" and "unsold" need
    -- different responses and pool into one waste number otherwise.
    case
        when latest_expiry is not null and latest_expiry <= trading_date
            and stock_on_hand_qty > 0
            then true
        else false
    end                                                       as is_expired_at_close,
    -- Risk of becoming waste: short life, stock on hand, and expiry close.
    case
        when stock_on_hand_qty <= 0 then false
        when latest_expiry is null then false
        when cast(date_diff('day', trading_date, latest_expiry) as integer) <= 1 then true
        else false
    end                                                       as is_waste_risk_at_close,
    case
        when units_sold_today > 0
            then cast(round(qty_received_today / units_sold_today, 4) as decimal(10, 4))
    end                                                       as days_of_cover_received,
    -- Sell-through: of the stock that was available to sell today, how much
    -- went out of the door. The complement of this is the waste pressure.
    case
        when (stock_on_hand_qty + units_sold_today) > 0
            then cast(round(units_sold_today / (stock_on_hand_qty + units_sold_today), 4) as decimal(10, 4))
    end                                                       as sell_through_rate
from aged
