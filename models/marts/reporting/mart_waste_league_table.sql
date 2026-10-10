{{ config(materialized='table') }}

{#-
  The waste league table: which lines cost the most money, and why.

  `waste_pressure_reason` is the operative column, because a line can be a
  problem for four different reasons and each has a different fix:

    * Ordered too much   — deliveries outrun sales. Fix: forecast better.
    * Too much cover     — the store holds several days of a 2-day product.
    * Life at delivery   — the stock arrives already short of life (D12), which
                           is a *supplier* conversation, not a store one.
    * Slow seller        — it simply does not sell at that store. Fix: range.

  Attributing a line to the right one is the difference between a report that
  says "you waste $40k in produce" and one that says what to do about it.
-#}
with fct as (

    select *
    from {{ ref('fct_store_item_day') }}
    where is_forecast_eligible

),

by_item as (

    select
        f.store_code,
        f.sku,
        f.department_code,
        max(f.department_name)                                as department_name,
        max(f.category)                                       as category,
        max(f.shelf_life_days)                                as shelf_life_days,
        max(f.standard_cost)                                  as standard_cost,
        max(f.retail_price)                                   as retail_price,

        sum(f.units_sold)                                     as units_sold,
        sum(f.qty_received)                                   as units_received,
        sum(f.recorded_waste_cost)                            as recorded_waste_cost,
        sum(f.markdown_erosion_value)                         as markdown_erosion_value,
        cast(sum(f.net_sales_value) as decimal(14, 2))         as net_sales_value,
        cast(sum(f.received_cost_value) as decimal(14, 2))     as received_cost_value,
        cast(sum(f.weighted_shrink_cost) as decimal(14, 2))    as estimated_shrink_cost,
        avg(f.min_life_at_delivery)                           as avg_min_life_at_delivery,
        avg(f.days_to_expiry)                                 as avg_days_to_expiry,
        avg(f.sell_through_rate)                              as avg_sell_through,
        count(*) filter (where f.is_expired_at_close)          as days_expired_at_close,
        count(*)                                              as active_days,

        -- Ordered volume against sold volume, over the period. The single
        -- clearest signal of an ordering problem.
        case
            when sum(f.units_sold) > 0
                then cast(sum(f.qty_received) / sum(f.units_sold) as decimal(8, 4))
        end                                                   as receive_to_sell_ratio,
        -- Days of stock delivered per day of sales. Above the shelf life, the
        -- excess is arithmetically guaranteed to be thrown away.
        case
            when sum(f.units_sold) > 0 and count(*) > 0
                then cast(sum(f.qty_received) / (sum(f.units_sold) / count(*)) as decimal(10, 4))
        end                                                   as days_of_cover_received,
        case
            when sum(f.net_sales_value) > 0
                then cast(sum(f.recorded_waste_cost) / sum(f.net_sales_value) as decimal(8, 4))
        end                                                   as waste_rate_of_sales
    from fct f
    group by 1, 2, 3

)

select
    store_code,
    sku,
    department_code,
    department_name,
    category,
    shelf_life_days,
    standard_cost,
    retail_price,
    units_sold,
    units_received,
    recorded_waste_cost,
    markdown_erosion_value,
    net_sales_value,
    received_cost_value,
    estimated_shrink_cost,
    receive_to_sell_ratio,
    days_of_cover_received,
    waste_rate_of_sales,
    avg_min_life_at_delivery,
    avg_days_to_expiry,
    avg_sell_through,
    days_expired_at_close,
    active_days,

    -- The combined cost of getting this line wrong: thrown away plus given away.
    cast(recorded_waste_cost + markdown_erosion_value as decimal(14, 2)) as total_loss_cost,

    -- Why it is a problem. Ordered by how actionable the fix is, so the first
    -- matching reason is the one to act on.
    case
        -- Cover beyond shelf life is arithmetic, not judgement: the stock cannot
        -- survive to be sold.
        when days_of_cover_received > shelf_life_days * 1.5
            then 'Ordered too much'
        -- Stock arrives with too little life left, whatever the store does.
        when avg_min_life_at_delivery is not null
             and avg_min_life_at_delivery < shelf_life_days * 0.6
            then 'Short life at delivery'
        -- Cover is reasonable but the stock still expires: it does not sell.
        when days_expired_at_close > active_days * 0.15
            then 'Slow seller'
        when markdown_erosion_value > recorded_waste_cost * 2
            then 'Marked down too deep'
        else 'Within tolerance'
    end                                                       as waste_pressure_reason,
    -- Ranked by money, so the report leads with the lines that matter.
    rank() over (order by recorded_waste_cost desc)            as waste_cost_rank
from by_item
