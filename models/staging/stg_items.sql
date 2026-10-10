{{ config(materialized='view') }}

{#-
  The fresh range.

  `shelf_life_days` and `life_at_delivery_days` are the two columns that decide
  whether a line can become waste at all, so they are the columns this model
  protects. `is_waste_risk` marks the lines whose life is short enough that a
  bad forecast turns directly into a bin — which is the population the
  optimiser works on.
-#}
select
    sku,
    item_name,
    department_code,
    department_name,
    category,
    unit_of_measure,
    (unit_of_measure = 'kg')                              as is_weighed,
    cast(pack_size as decimal(8, 3))                      as pack_size,
    cast(retail_price as decimal(10, 4))                  as retail_price,
    cast(standard_cost as decimal(10, 4))                 as standard_cost,
    cast(retail_price - standard_cost as decimal(10, 4))  as gross_margin_per_unit,
    case
        when retail_price > 0
            then cast((retail_price - standard_cost) / retail_price as decimal(6, 4))
    end                                                    as gross_margin_rate,
    cast(shelf_life_days as integer)                      as shelf_life_days,
    cast(life_at_delivery_days as integer)                as life_at_delivery_days,
    supplier_code,
    storage_condition,
    is_markdown_eligible,
    is_short_life,
    -- The lines this project exists for: short life AND markdown-eligible.
    -- Hot food is same-day disposal and is never re-priced, so it is excluded
    -- from markdown scope even though its life is one day.
    (is_short_life and is_markdown_eligible
        and cast(shelf_life_days as integer) <= {{ var('short_life_max_days') }})
                                                          as is_waste_risk,
    cast(master_published_date as date)                   as master_published_date
from {{ source('raw', 'item_master') }}
