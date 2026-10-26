-- D09: SKUs the item master does not carry must be quantified, not dropped.
--
-- The join to the item master is a LEFT join everywhere on purpose. An inner
-- join would silently delete real revenue. The tills know a seasonal line
-- before the master does, and the loss would be invisible because the report
-- would simply cover a smaller population.
--
-- So this test asserts the opposite of "no unmapped SKUs": it asserts that
-- unmapped SKUs EXIST (the defect is still being produced, so the test is
-- meaningful) and that the rows carrying them survived the load with their
-- money intact. A test that demanded zero unmapped SKUs would force somebody to
-- start filtering, which is exactly the bug.
with unmapped as (

    select
        p.sku,
        p.store_code,
        count(*)                             as rows,
        sum(p.units_sold)                    as units,
        sum(p.net_sales_value)               as net_sales_value
    from {{ ref('stg_pos_sales') }} p
    left join {{ ref('stg_items') }} i on p.sku = i.sku
    where i.sku is null
    group by 1, 2

),

known as (

    select count(*) as known_skus from {{ ref('stg_items') }}

)

select
    u.sku,
    u.store_code,
    u.rows,
    u.units,
    u.net_sales_value,
    k.known_skus
from unmapped u
cross join known k
where
    -- The defect must still be produced, or nothing is being proved.
    (select count(*) from unmapped) = 0
    -- The rows must be present and must carry real revenue. A zero here means
    -- the money was zeroed on the way in, which is the failure mode.
    or u.net_sales_value = 0
    or u.units = 0
    -- And the master must actually be missing them: if every sold SKU were in
    -- the master, the join would not be under test.
    or k.known_skus = 0
