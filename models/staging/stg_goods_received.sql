{{ config(materialized='view') }}

{#-
  Deliveries, with the three quantities kept apart.

  `qty_ordered`, `qty_received` and `qty_rejected` are deliberately not
  collapsed into a fill rate. "We ordered less", "the supplier shorted us" and
  "it arrived warm and we refused it" are three different problems with three
  different fixes, and a single ratio hides which one you have.

  `days_of_life_at_delivery` is computed per line rather than read from the item
  master, because a substitution arrives with its own, shorter, life — and that
  shorter life is what makes the substitution a waste risk.
-#}
select
    cast(g.business_date as date)                        as delivery_date,
    g.store_code,
    g.sku,
    g.supplier_code,
    g.invoice_no,
    g.purchase_order,
    cast(g.qty_ordered as decimal(12, 3))                as qty_ordered,
    cast(g.qty_received as decimal(12, 3))               as qty_received,
    cast(g.qty_rejected as decimal(12, 3))               as qty_rejected,
    cast(g.unit_cost as decimal(10, 4))                  as unit_cost,
    cast(g.extended_cost as decimal(12, 2))              as received_cost_value,
    cast(g.expiry_date as date)                          as expiry_date,
    g.unit_of_measure,
    g.was_substituted,
    cast(g.delivery_sequence as integer)                 as delivery_sequence,
    -- The number that matters for waste: how long this stock lives from today.
    case
        when g.expiry_date is not null and g.business_date is not null
            then cast(date_diff('day', cast(g.business_date as date), cast(g.expiry_date as date)) as integer)
    end                                                  as days_of_life_at_delivery,
    -- Fill is measured against what was ordered, and only counts stock that was
    -- actually accepted into the store.
    case
        when g.qty_ordered > 0
            then cast(g.qty_received / g.qty_ordered as decimal(8, 4))
    end                                                  as fill_rate,
    case
        when g.qty_received > 0
            then cast(g.qty_rejected / g.qty_received as decimal(8, 4))
        else 0
    end                                                  as reject_rate,
    g._source_file
from {{ source('raw', 'goods_received') }} g
