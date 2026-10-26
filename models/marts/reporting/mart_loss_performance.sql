{{ config(materialized='table') }}

{#-
  Loss performance by department, store and month. This is the report's headline
  table.

  The three loss concepts are reported side by side with their own bases:

    * markdown erosion as a share of full-price sales (margin given away)
    * recorded waste as a share of cost received (stock thrown out)
    * unrecorded shrink as a confidence-weighted estimate, flagged as such

  Reporting waste as a share of *cost received* rather than of *sales* is
  intentional. A store can have high sales and high waste at once. That is what
  over-ordering looks like, and a waste-to-sales ratio mathematically punishes
  a store for selling well.
-#}
with fct as (

    select *
    from {{ ref('fct_store_item_day') }}

),

stores as (

    select store_code, store_name, store_format, store_type, size_index, region
    from {{ ref('dim_store') }}

)

select
    f.store_code,
    s.store_name,
    s.store_format,
    s.store_type,
    s.size_index,
    s.region,
    f.department_code,
    f.department_name,
    date_trunc('month', f.business_date)                     as month_start,

    -- Volume
    count(*)                                                 as store_item_days,
    count(distinct f.sku)                                    as distinct_items,
    sum(f.units_sold)                                        as units_sold,
    sum(f.qty_received)                                      as units_received,
    sum(f.delivery_lines)                                    as delivery_lines,

    -- Value
    cast(sum(f.full_price_sales_value) as decimal(14, 2))     as full_price_sales_value,
    cast(sum(f.net_sales_value) as decimal(14, 2))            as net_sales_value,
    cast(sum(f.realised_margin_value) as decimal(14, 2))      as realised_margin_value,
    cast(sum(f.received_cost_value) as decimal(14, 2))        as received_cost_value,

    -- Loss 1: markdown erosion (margin given away)
    cast(sum(f.markdown_erosion_value) as decimal(14, 2))     as markdown_erosion_value,
    case
        when sum(f.full_price_sales_value) > 0
            then cast(sum(f.markdown_erosion_value) / sum(f.full_price_sales_value) as decimal(8, 4))
    end                                                       as markdown_erosion_rate,

    -- Loss 2: recorded waste (cost of stock thrown out). A FLOOR.
    cast(sum(f.recorded_waste_cost) as decimal(14, 2))        as recorded_waste_cost,
    cast(sum(f.prep_loss_cost) as decimal(14, 2))             as prep_loss_cost,
    case
        when sum(f.received_cost_value) > 0
            then cast(sum(f.recorded_waste_cost) / sum(f.received_cost_value) as decimal(8, 4))
    end                                                       as recorded_waste_rate_of_cost,
    case
        when sum(f.net_sales_value) > 0
            then cast(sum(f.recorded_waste_cost) / sum(f.net_sales_value) as decimal(8, 4))
    end                                                       as recorded_waste_rate_of_sales,

    -- Loss 3: unrecorded shrink. Reported weighted, and with its confidence, so
    -- a low-confidence estimate cannot be mistaken for a measurement.
    cast(sum(f.weighted_shrink_cost) as decimal(14, 2))       as estimated_unrecorded_shrink_cost,
    cast(avg(f.shrink_confidence) as decimal(6, 2))           as avg_shrink_confidence,
    count(*) filter (where f.shrink_confidence is not null)    as segments_with_shrink_estimate,

    -- Everything the P&L currently sees, in one line, clearly labelled as an
    -- *accounted* total rather than the true one.
    cast(sum(f.recorded_waste_cost) + sum(f.prep_loss_cost)
         + sum(f.markdown_erosion_value) as decimal(14, 2))    as total_accounted_loss_value,

    -- Data quality, carried into the numbers so a thin month is visible.
    count(*) filter (where f.is_pos_gap)                       as pos_gap_store_item_days,
    count(*) filter (where f.had_mixed_money_units)            as mixed_money_unit_days,
    count(*) filter (where f.had_voids)                        as days_with_voids,
    count(*) filter (where f.has_unmapped_reason)              as days_with_unmapped_reason
from fct f
join stores s on f.store_code = s.store_code
group by 1, 2, 3, 4, 5, 6, 7, 8, 9
