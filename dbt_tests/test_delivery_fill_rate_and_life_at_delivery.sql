-- D12: deliveries are short and substituted, and each line carries its own life.
--
-- Two claims:
--
--  1. **The three quantities stay apart.** `qty_ordered`, `qty_received` and
--     `qty_rejected` must not be collapsed: "we ordered less", "the supplier
--     shorted us" and "it arrived warm and we refused it" are three different
--     problems. Short deliveries must actually occur (or the fill-rate analysis
--     is untested), and received must never exceed what was ordered by more than
--     a substitution can explain.
--
--  2. **Shelf life is per delivery, not per item.** A substitution arrives with
--     a shorter life, so `days_of_life_at_delivery` must vary *within* a SKU. If
--     it were constant, the model would be reading the item master's standard
--     rather than the delivery line, and every life-based decision downstream
--     would inherit that error.
with lines as (

    select
        store_code,
        sku,
        delivery_date,
        qty_ordered,
        qty_received,
        qty_rejected,
        was_substituted,
        days_of_life_at_delivery,
        fill_rate
    from {{ ref('stg_goods_received') }}

),

summary as (

    select
        count(*)                                                        as lines,
        count(*) filter (where qty_received < qty_ordered - 0.001)      as short_lines,
        count(*) filter (where was_substituted)                         as substituted_lines,
        count(*) filter (where days_of_life_at_delivery is null)        as null_life,
        count(*) filter (where days_of_life_at_delivery is not null
                           and days_of_life_at_delivery < 1)            as non_positive_life,
        count(*) filter (where qty_rejected > qty_received)              as reject_exceeds_received,
        count(*) filter (where qty_ordered <= 0)                         as zero_ordered,
        -- Fill rate must be a ratio, not a percentage or a raw quantity.
        count(*) filter (where fill_rate is not null
                           and (fill_rate < 0 or fill_rate > 3))         as implausible_fill_rate,
        (select count(*) from (
            select sku from lines
            where days_of_life_at_delivery is not null
            group by sku having count(distinct days_of_life_at_delivery) > 1
        ))                                                              as skus_with_varying_life
    from lines

)

select *
from summary
where
    lines = 0
    -- The short-delivery defect must still occur.
    or short_lines = 0
    -- So must substitutions, which are what make life vary within a SKU.
    or substituted_lines = 0
    -- Life must be present and positive on every delivery line.
    or null_life > 0
    or non_positive_life > 0
    -- A rejection cannot exceed what arrived.
    or reject_exceeds_received > 0
    or zero_ordered > 0
    or implausible_fill_rate > 0
    -- The crucial one: life must vary within a SKU, or the model is reading the
    -- standard rather than the delivery.
    or skus_with_varying_life = 0
