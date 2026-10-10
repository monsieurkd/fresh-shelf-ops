{{ config(materialized='table') }}

{#-
  Markdown effectiveness: how much margin each depth bought back.

  The question this answers is not "did we mark down" but "was marking down
  worth it". Three quantities make it answerable, and they are deliberately kept
  separate:

    * `units_recovered`  — stock that sold under a markdown and would otherwise
                           have expired. This is the whole point.
    * `erosion_cost`     — the margin given away to move it.
    * `avoided_waste_cost` — the cost of the stock that was saved from the bin,
                           using each line's own standard cost.
    * `net_benefit`      — recovered value minus erosion. A markdown that loses
                           money is worse than a disposal, because it also costs
                           the labour of printing and sticking.

  `avoided_waste_cost` is an ESTIMATE and is labelled as one: it assumes stock
  that sold under a markdown would otherwise have been disposed of. Where
  sell-through was already high, some of those units would have sold at full
  price anyway, so this overstates the benefit. The `sell_through_rate` column
  is carried alongside so the overstatement is visible rather than hidden.
-#}
with marks as (

    select
        m.store_code,
        m.sku,
        m.business_date,
        m.markdown_pct,
        m.qty_ticketed,
        m.qty_honoured,
        m.qty_sold_at_markdown,
        m.qty_ticket_not_honoured,
        m.honour_rate,
        m.is_standard_depth,
        m.printed_hour,
        m.pass_name
    from {{ ref('stg_markdown_events') }} m

),

fct as (

    select
        store_code,
        sku,
        business_date,
        standard_cost,
        retail_price,
        department_code,
        department_name,
        sell_through_rate,
        markdown_erosion_value,
        units_sold,
        is_forecast_eligible
    from {{ ref('fct_store_item_day') }}

),

joined as (

    select
        m.store_code,
        m.sku,
        m.business_date,
        f.department_code,
        f.department_name,
        f.standard_cost,
        f.retail_price,
        f.sell_through_rate,
        f.is_forecast_eligible,
        m.markdown_pct,
        m.qty_ticketed,
        m.qty_honoured,
        m.qty_sold_at_markdown,
        m.qty_ticket_not_honoured,
        m.honour_rate,
        m.is_standard_depth,
        m.printed_hour,
        m.pass_name,
        f.markdown_erosion_value
    from marks m
    left join fct f
        on m.store_code = f.store_code
        and m.sku = f.sku
        and m.business_date = f.business_date

)

select
    store_code,
    sku,
    business_date,
    department_code,
    department_name,
    standard_cost,
    retail_price,
    markdown_pct,
    qty_ticketed,
    qty_honoured,
    qty_sold_at_markdown,
    qty_ticket_not_honoured,
    honour_rate,
    is_standard_depth,
    printed_hour,
    pass_name,
    sell_through_rate,
    is_forecast_eligible,

    -- Money given away on the units this ticket moved.
    cast(qty_sold_at_markdown * retail_price * markdown_pct as decimal(12, 2))
                                                              as erosion_cost,
    -- Cost of the stock saved from disposal. An estimate: see the model note.
    cast(qty_sold_at_markdown * standard_cost as decimal(12, 2))
                                                              as avoided_waste_cost,
    -- The margin the markdown itself preserved, which is the right lens: the
    -- unit was going to be thrown away at full cost, so what matters is that it
    -- went out at something above cost.
    cast(qty_sold_at_markdown * (retail_price * (1 - markdown_pct) - standard_cost) as decimal(12, 2))
                                                              as recovered_margin_value,
    -- Erosion against recovery, the ratio a buyer judges a markdown by.
    cast(
        (qty_sold_at_markdown * (retail_price * (1 - markdown_pct) - standard_cost))
        - (qty_sold_at_markdown * retail_price * markdown_pct)
    as decimal(12, 2))                                        as net_markdown_benefit,

    -- Compliance: the units that carried a sticker and were still rung full
    -- price. Priced, so the store can see what the till error is worth (D13).
    cast(qty_ticket_not_honoured * retail_price * markdown_pct as decimal(12, 2))
                                                              as unhonoured_erosion_value,

    -- Where this sits on the depth ladder. Depth is the lever; this makes it
    -- comparable across lines and departments.
    case
        when markdown_pct <= 0.10 then '1: shallow (10%)'
        when markdown_pct <= 0.20 then '2: light (20%)'
        when markdown_pct <= 0.30 then '3: standard (30%)'
        when markdown_pct <= 0.40 then '4: deep (40%)'
        when markdown_pct <= 0.50 then '5: half price (50%)'
        else '6: clearance (70%)'
    end                                                       as depth_band,
    -- A markdown printed when stock still had time is a margin decision; one
    -- printed on the last day is a rescue. They should not be pooled.
    case
        when printed_hour >= 17 then 'Evening (last chance)'
        when printed_hour >= 13 then 'Afternoon'
        else 'Morning (planned)'
    end                                                       as pass_timing
from joined
