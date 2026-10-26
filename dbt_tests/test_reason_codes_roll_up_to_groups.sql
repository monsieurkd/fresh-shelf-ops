-- D08: reason codes drift by firmware, and every code must roll up to a group.
--
-- The failure this guards against is a *silent* one. A new firmware label that
-- is not in `seeds/waste_reason_map.csv` arrives as 'unmapped', which is
-- correct and intentional: it is the alert. What is not acceptable is a code
-- that maps to a group AND a loss class that disagree with each other, or a
-- mapping where expiry-class events have been classified as shrinkage, because
-- that turns an ordering problem into a theft problem in the report.
--
-- It also asserts that the drift is still happening. If every raw code were
-- identical to its mapped group, the model would be testing nothing.
with codes as (

    select
        reason_code_raw,
        reason_code_key,
        reason_group,
        loss_class,
        count(*)                                                 as events,
        sum(qty_disposed)                                        as qty
    from {{ ref('stg_waste_events') }}
    group by 1, 2, 3, 4

),

groups as (

    select
        reason_group,
        count(distinct loss_class)                               as distinct_loss_classes
    from codes
    group by 1

),

summary as (

    select
        (select count(*) from codes)                             as distinct_raw_codes,
        (select count(*) from codes where reason_group = 'unmapped') as unmapped_codes,
        (select count(*) from codes where loss_class = 'waste' and reason_group ilike '%theft%')
                                                                 as expiry_as_theft,
        (select max(distinct_loss_classes) from groups)          as max_classes_per_group,
        -- Are the raw codes actually messier than the mapped groups?
        (select count(distinct reason_group) from codes)         as distinct_groups,
        (select count(*) from {{ ref('stg_waste_events') }} where is_unmapped_reason)
                                                                 as unmapped_events

)

select *
from summary
where
    -- The mapping has to be doing work: more raw spellings than groups.
    distinct_raw_codes <= distinct_groups
    -- A reason group must not span both waste and shrink: those are different
    -- problems and pooling them misdirects the fix.
    or max_classes_per_group > 1
    -- Nothing expiry-related may be classified as theft.
    or expiry_as_theft > 0
    -- Every code must land in some group, even if that group is 'unmapped'.
    or (select count(*) from codes where reason_group is null) > 0
    -- And the drift must still be present, or this test proves nothing.
    or unmapped_codes = distinct_raw_codes
