{{ config(materialized='view') }}

{#-
  The store estate. Small, static, and every fact joins to it, so it is the
  first thing built.
-#}
select
    store_code,
    store_name,
    suburb,
    postcode,
    state,
    latitude,
    longitude,
    store_format,
    cast(size_index as decimal(6, 4))          as size_index,
    cast(opened_date as date)                  as opened_date,
    region,
    timezone,
    -- Groupings the report leans on. An Express store is a different operation
    -- from a Super store: less range, fewer deliveries, less markdown discipline.
    case store_format
        when 'Super' then 'Supermarket'
        when 'Metro' then 'Metro'
        else 'Express'
    end                                        as store_type,
    (store_format = 'Express')                 as is_small_format,
    (_source_file is not null)                 as is_loaded
from {{ source('raw', 'store_master') }}
