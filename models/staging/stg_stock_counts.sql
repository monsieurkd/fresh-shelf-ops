{{ config(materialized='view') }}

{#-
  Physical counts, normalised to eaches.

  Two corrections, both from D11:

  * A count recorded in packs is multiplied by the pack size, because the ledger
    is kept in eaches. Leaving it in packs puts a single line out by up to 12x,
    which then reads as enormous unrecorded shrink.
  * The count type is preserved, because a Sunday full count and a midweek
    high-risk cycle count are not equally reliable anchors for reconciliation.
-#}
select
    c.store_code,
    c.sku,
    cast(c.business_date as date)                        as count_date,
    cast(c.counted_qty as decimal(14, 3))                as counted_qty_as_filed,
    c.count_type,
    c.counted_by,
    c.unit_of_measure,
    c.count_reference,
    coalesce(cast(i.pack_size as decimal(8, 3)), 1)      as pack_size,
    -- Normalise to eaches: a pack count is a quantity in packs.
    case
        when c.unit_of_measure = 'pack'
            then cast(c.counted_qty * coalesce(cast(i.pack_size as decimal(8, 3)), 1) as decimal(14, 3))
        else cast(c.counted_qty as decimal(14, 3))
    end                                                  as counted_qty,
    (c.unit_of_measure = 'pack')                         as was_counted_in_packs,
    (c.count_type = 'weekly_full')                       as is_full_count,
    -- Counts on short-life lines are the ones the reconciliation can trust most,
    -- because those lines are counted most often and are least likely to have
    -- drifted for reasons nobody noticed.
    coalesce(cast(i.shelf_life_days as integer), 0) <= 7  as is_high_risk_line,
    c._source_file
from {{ source('raw', 'stock_counts') }} c
left join {{ source('raw', 'item_master') }} i
    on c.sku = i.sku
