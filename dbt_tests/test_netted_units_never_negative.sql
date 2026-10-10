-- D10: voided and refunded lines net out, and netting never goes negative.
--
-- The subtlety this test exists for: after netting, a voided line is a *positive*
-- net row. `stg_pos_sales` has one row per store-day-SKU, already summed, so
-- "units_sold < 0" is the wrong place to look for a void — the reversal is a
-- fraction of the line it reverses, so the net stays positive by design.
--
-- So the test asserts on the two layers that can actually see a void:
--
--  1. **The raw layer still carries the negative rows.** If it did not, the
--     defect would have stopped being produced and this test would prove
--     nothing more than that nothing happens.
--
--  2. **Netting recovers the truth.** Summed over the estate, the netted units
--     must equal the raw units summed — because netting is a sum. If the
--     negatives were dropped, the netted total would sit *below* the raw total
--     (or the raw below it if the reversal were double counted). A gross figure
--     would also disagree, so the comparison has to be against the raw sum.
with raw_layer as (

    select
        count(*) filter (where units < 0)                        as raw_negative_rows,
        sum(units)                                               as raw_total_units,
        count(distinct store_code || '|' || business_date)        as raw_store_days
    from {{ source('raw', 'pos_sales') }}

),

netted as (

    select
        count(*)                                                 as netted_rows,
        sum(units_sold)                                          as netted_total_units,
        count(*) filter (where void_rows > 0)                     as rows_that_had_a_void,
        sum(void_rows)                                           as void_rows_carried,
        count(*) filter (where units_sold < -0.001)              as netted_negative_rows
    from {{ ref('stg_pos_sales') }}

),

ledger as (

    select sum(units_sold) as ledger_units
    from {{ ref('int_stock_ledger') }}

)

select
    r.raw_negative_rows,
    r.raw_total_units,
    n.netted_rows,
    n.netted_total_units,
    n.rows_that_had_a_void,
    n.void_rows_carried,
    n.netted_negative_rows,
    l.ledger_units
from raw_layer r
cross join netted n
cross join ledger l
where
    -- The defect must still be produced, or nothing is under test.
    r.raw_negative_rows = 0
    -- Every raw negative row must have been noticed and counted as a void.
    or n.rows_that_had_a_void = 0
    or n.void_rows_carried < r.raw_negative_rows
    -- Netting is a sum, so the two totals must agree.
    or abs(n.netted_total_units - r.raw_total_units) > 0.5
    -- A netted store-day-SKU must never end up negative. That is the invariant
    -- the injector is built to respect: a reversal is always bounded by the
    -- line it reverses.
    or n.netted_negative_rows > 0
    -- And the ledger must agree with the netted sales, within the gap the
    -- unmapped SKUs explain (D09). The ledger spans the ranged assortment; POS
    -- also carries seasonal lines the item master has not caught up with, so the
    -- two totals are legitimately different — and the *size* of the difference
    -- has to stay inside what the unmapped rows account for. If the gap grew
    -- beyond that, the ledger would be dropping real sales rather than simply
    -- not ranging them.
    or abs(n.netted_total_units - l.ledger_units) > (
        select coalesce(sum(p.units_sold), 0) * 1.05 + 1.0
        from {{ ref('stg_pos_sales') }} p
        left join {{ ref('stg_items') }} i on p.sku = i.sku
        where i.sku is null
    )
