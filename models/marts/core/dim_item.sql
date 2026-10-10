{{ config(materialized='table') }}

{#-
  Item dimension.

  Shelf life is the attribute that makes a fresh line a fresh line, so it is
  surfaced here as a *band* rather than only a number: "2-3 days" is the mental
  model a store manager uses, and it is also how the report groups things.
-#}
select
    i.sku                                                    as item_key,
    i.sku,
    i.item_name,
    i.department_code,
    i.department_name,
    i.category,
    i.unit_of_measure,
    i.is_weighed,
    i.pack_size,
    i.retail_price,
    i.standard_cost,
    i.gross_margin_per_unit,
    i.gross_margin_rate,
    i.shelf_life_days,
    i.life_at_delivery_days,
    i.supplier_code,
    i.is_markdown_eligible,
    i.is_short_life,
    i.is_waste_risk,
    -- The band a store manager thinks in.
    case
        when i.shelf_life_days <= 1 then 'Same day'
        when i.shelf_life_days <= 3 then '2-3 days'
        when i.shelf_life_days <= 7 then '4-7 days'
        when i.shelf_life_days <= 21 then '1-3 weeks'
        else '3 weeks+'
    end                                                      as shelf_life_band,
    -- Ordering must be sensitive to this: a line with three days of life cannot
    -- be covered for a week, however cheap it is.
    case
        when i.shelf_life_days <= 3 then 'High'
        when i.shelf_life_days <= 10 then 'Medium'
        else 'Low'
    end                                                      as waste_risk_band,
    i.master_published_date
from {{ ref('stg_items') }} i
