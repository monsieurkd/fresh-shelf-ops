-- D11: counts are eyeballed, and pack-counted lines must be normalised.
--
-- The ledger is kept in eaches, so a count recorded in *packs* has to be
-- multiplied by the pack size before it can be compared with anything. Left in
-- packs, a single line is out by up to 12x — which does not look like a unit
-- error, it looks like enormous unrecorded shrink, and somebody spends a week
-- investigating a theft that is a multiplication.
--
-- The test therefore does two things: it confirms the pack-counting defect is
-- still being produced (or the normalisation is untested), and it confirms the
-- normalisation actually ran, by checking that a pack-counted row's stored
-- quantity is consistent with the pack multiple rather than with the raw count.
with pack_counts as (

    select
        c.store_code,
        c.sku,
        c.count_date,
        c.counted_qty_as_filed,
        c.counted_qty,
        c.pack_size,
        c.was_counted_in_packs,
        i.unit_of_measure,
        i.is_weighed
    from {{ ref('stg_stock_counts') }} c
    left join {{ ref('stg_items') }} i on c.sku = i.sku

),
summary as (

    select
        count(*)                                                        as counts,
        count(*) filter (where was_counted_in_packs)                    as pack_counts,
        count(*) filter (where counted_qty is null)                     as null_counts,
        count(*) filter (where counted_qty < 0)                         as negative_counts,
        -- A normalised pack count must have grown by the pack size. If it is
        -- identical to the filed figure, the multiplication did not happen.
        --
        -- Zero is excluded on purpose: a pack count of nothing normalises to
        -- nothing, so equality there proves nothing either way. Two thirds of
        -- the apparent "un-normalised" rows in this estate were zero counts.
        count(*) filter (
            where was_counted_in_packs and pack_size > 1
              and counted_qty_as_filed > 0
              and counted_qty = counted_qty_as_filed
        )                                                               as unnormalised_pack_counts,
        -- A weighed line is counted fractionally; an each line must be whole.
        count(*) filter (
            where not is_weighed and counted_qty is not null
              and counted_qty <> round(counted_qty)
        )                                                               as fractional_each_counts
    from pack_counts

)

select *
from summary
where
    counts = 0
    -- The defect must still be produced, or the normalisation is untested.
    or pack_counts = 0
    or null_counts > 0
    or negative_counts > 0
    -- The normalisation must actually run.
    or unnormalised_pack_counts > 0
    -- And units must stay in the same base: eaches are whole numbers.
    or fractional_each_counts > 0
