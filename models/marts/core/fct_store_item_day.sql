{{ config(materialized='table') }}

{#-
  The fact table: one row per store-item-day, with every loss term named.

  This is the model every number in the report traces back to. The design choice
  worth stating is that the three loss concepts are kept *apart* rather than
  summed into one "shrink" figure, because they have three different fixes:

    * markdown erosion: margin given away to sell something before it expires.
                        Fixed by marking down earlier and shallower.
    * recorded waste: scanned disposals at cost. Fixed by ordering less.
    * unrecorded shrink: loss no record explains. Fixed by better process.

  A single "loss" number would average those into something nobody can act on,
  and would hide which of the three a store actually has a problem with.

  `is_forecast_eligible` exists so the decision layer and the benchmarks use the
  same population. A line only enters the optimiser if it is a genuine waste
  risk: short life, markdown-eligible, and actually ranged at that store.
-#}
with ledger as (

    select *
    from {{ ref('int_stock_ledger') }}

),

fefo as (

    select
        store_code,
        sku,
        trading_date,
        days_to_expiry,
        stock_on_hand_qty                    as fefo_stock_on_hand_qty,
        sell_through_rate,
        is_expired_at_close,
        is_waste_risk_at_close
    from {{ ref('int_fefo_depletion') }}

),

-- Residue is a segment figure (between counts), not a daily one. Spreading it
-- back onto days by segment length is a simplification: it makes the
-- number usable per day without pretending that a daily split actually exists.
shrink as (

    select
        store_code,
        sku,
        count_date,
        unrecorded_shrink_qty,
        unrecorded_shrink_cost,
        shrink_confidence,
        segment_days,
        cast(unrecorded_shrink_qty / nullif(segment_days, 0) as decimal(12, 4))
                                                                  as shrink_qty_per_day,
        cast(unrecorded_shrink_cost / nullif(segment_days, 0) as decimal(12, 2))
                                                                  as shrink_cost_per_day
    from {{ ref('int_loss_reconciliation') }}

)

select
    -- ---- keys --------------------------------------------------------------
    l.store_code                                              as store_key,
    l.sku                                                     as item_key,
    l.business_date                                           as date_key,
    l.store_code,
    l.sku,
    l.business_date,

    -- ---- dimensions carried for query convenience --------------------------
    l.department_code,
    l.department_name,
    l.category,
    l.is_weighed,
    l.shelf_life_days,
    l.is_waste_risk,
    l.is_markdown_eligible,
    l.is_closed,
    l.is_weekend,
    l.trading_context,
    l.days_before_christmas,
    l.standard_cost,
    l.retail_price,
    l.gross_margin_per_unit,
    -- Margin this line earns per unit sold, which is the value a markdown puts
    -- at risk. Quoting waste in cost and markdown in margin keeps them apart:
    -- they are different kinds of money and adding them would be wrong.
    cast(l.retail_price - l.standard_cost as decimal(10, 4))  as unit_margin_value,

    -- ---- what came in ------------------------------------------------------
    l.qty_ordered,
    l.qty_received,
    l.qty_rejected,
    l.received_cost_value,
    l.delivery_lines,
    l.min_life_at_delivery,
    l.max_life_at_delivery,
    l.had_substitution,
    l.avg_unit_cost,
    -- Delivery failures are their own problem: a short delivery turns into
    -- lost sales, not waste, and the two need separate fixes.
    cast(l.qty_ordered - l.qty_received as decimal(12, 3))     as qty_short_delivered,

    -- ---- what went out -----------------------------------------------------
    l.units_sold,
    l.net_sales_value,
    l.gross_sales_value,
    l.transaction_count,
    l.avg_unit_price,
    l.money_unit_as_filed,
    l.had_mixed_money_units,
    l.had_voids,
    l.has_pos_data,
    l.is_pos_gap,

    -- Margin actually earned on the units sold, after the markdown given away.
    cast(l.units_sold * (l.retail_price - l.standard_cost) - l.markdown_erosion_value
         as decimal(12, 2))                                    as realised_margin_value,
    -- Budgeted margin at full price: the benchmark markdown erosion is measured
    -- against. Without this baseline, "erosion" has nothing to be a fraction of.
    cast(l.units_sold * l.retail_price as decimal(12, 2))      as full_price_sales_value,

    -- ---- loss term 1: markdown erosion -------------------------------------
    -- Margin given away at the till, measured from realised POS discount rather
    -- than from printed tickets (D13). Tickets record intent; POS records money.
    l.markdown_erosion_value,
    case
        when l.units_sold > 0
            then cast(l.markdown_erosion_value / (l.units_sold * l.retail_price) as decimal(8, 4))
    end                                                        as markdown_erosion_rate,
    -- The share of the estate's markdown depth applied on this day, from the
    -- printed tickets. Depth and erosion are different questions: how deep did
    -- we cut, versus how much did it cost.
    l.avg_markdown_pct,
    l.max_markdown_pct,
    l.markdown_tickets,

    -- ---- loss term 2: recorded waste ---------------------------------------
    -- Scanned disposals, valued at standard cost. A FLOOR on true loss (D07).
    l.qty_waste_scanned,
    l.recorded_waste_cost,
    l.qty_prep_scanned,
    l.prep_loss_cost,
    l.qty_other_scanned,
    l.scan_events,
    l.has_unmapped_reason,
    -- Total *recorded* loss at cost: the two things the P&L already sees.
    cast(l.recorded_waste_cost + l.prep_loss_cost as decimal(12, 2)) as total_recorded_loss_cost,

    -- ---- loss term 3: unrecorded shrink ------------------------------------
    -- Spreading a segment figure onto its days. Read the confidence: this
    -- number is a bound, not a measurement.
    s.shrink_confidence,
    s.segment_days,
    s.shrink_qty_per_day,
    s.shrink_cost_per_day,
    -- The share that may be quoted as *estimated* loss. The remainder is count
    -- error, which is a measurement problem rather than a loss problem.
    cast(s.shrink_qty_per_day * s.shrink_confidence as decimal(12, 2)) as weighted_shrink_qty,
    cast(s.shrink_cost_per_day * s.shrink_confidence as decimal(12, 2)) as weighted_shrink_cost,
    l.was_counted,
    l.counted_qty,

    -- ---- shelf life --------------------------------------------------------
    f.days_to_expiry,
    f.fefo_stock_on_hand_qty,
    f.sell_through_rate,
    f.is_expired_at_close,
    f.is_waste_risk_at_close,

    -- ---- eligibility -------------------------------------------------------
    -- One definition, used by the forecast, the optimiser and the benchmarks,
    -- so a line cannot be in scope for one and out for another. Note it requires
    -- *activity*: a line with no deliveries, no sales and no stock on hand on a
    -- given day is in the dimension but contributes nothing here.
    (    l.shelf_life_days <= {{ var('short_life_max_days') }}
     and l.is_markdown_eligible
     and (l.qty_received > 0 or l.units_sold > 0 or coalesce(f.fefo_stock_on_hand_qty, 0) > 0)
    )                                                          as is_forecast_eligible,
    l.is_waste_risk
from ledger l
left join fefo f
    on l.store_code = f.store_code
    and l.sku = f.sku
    and l.business_date = f.trading_date
left join shrink s
    on l.store_code = s.store_code
    and l.sku = s.sku
    and l.business_date = s.count_date
