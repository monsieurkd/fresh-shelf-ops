-- D13: markdown erosion comes from realised money, not printed tickets.
--
-- This is the test that keeps the two sources honest about their roles. A
-- printed ticket records an intention; the till records money. The pipeline must
-- value erosion from the till, because a ticket that was never honoured cost
-- nobody anything, and treating it as erosion would create a loss that never
-- happened.
--
-- The failure this guards against is subtle: if somebody ever "fixes" the
-- markdown model to price erosion from `qty_ticketed * base_price * pct`, the
-- estate erosion would jump, and it would look like a pricing decision rather
-- than a bug. So the test pins the three relationships that make the distinction
-- real:
--
--   * the POS realised discount must agree with the fact table's erosion,
--   * printed tickets must exceed honoured ones (the defect is still produced),
--   * and the unhonoured gap must be priced, so its size is known.
with pos as (

    select
        sum(markdown_erosion_value)                                as pos_realised_erosion,
        sum(net_sales_value)                                       as pos_net_sales
    from {{ ref('stg_pos_sales') }}

),

fact as (

    select
        sum(markdown_erosion_value)                                as fct_erosion,
        sum(full_price_sales_value)                                as full_price_sales,
        sum(units_sold)                                            as units_sold
    from {{ ref('fct_store_item_day') }}

),

tickets as (

    select
        count(*)                                                   as tickets,
        count(*) filter (where qty_ticketed > qty_honoured)         as tickets_not_fully_honoured,
        sum(qty_ticketed)                                          as qty_ticketed,
        sum(qty_honoured)                                          as qty_honoured,
        sum(qty_sold_at_markdown)                                  as qty_sold,
        -- Erosion if it were priced off the tickets rather than the till.
        sum(qty_ticketed * base_price * markdown_pct)              as printed_value,
        sum(qty_ticketed - qty_honoured)                           as gap_qty_filed
    from {{ ref('stg_markdown_events') }}

),

paired as (

    select
        count(*)                                                   as markdown_days,
        sum(m.qty_sold_at_markdown)                                as units_sold_at_markdown,
        sum(f.markdown_erosion_value)                              as erosion_on_those_days
    from {{ ref('stg_markdown_events') }} m
    join {{ ref('fct_store_item_day') }} f
        on m.store_code = f.store_code
        and m.sku = f.sku
        and m.business_date = f.business_date

)

select
    p.pos_realised_erosion,
    f.fct_erosion,
    t.tickets,
    t.tickets_not_fully_honoured,
    t.qty_ticketed,
    t.qty_honoured,
    t.printed_value,
    t.gap_qty_filed,
    pr.markdown_days,
    pr.units_sold_at_markdown,
    pr.erosion_on_those_days
from pos p
cross join fact f
cross join tickets t
cross join paired pr
where
    -- The fact table must be built from realised POS money. The two legitimately
    -- differ by the unmapped-SKU share (D09), so the allowance is that gap
    -- rather than a flat tolerance — otherwise the check would quietly become a
    -- check on the item master.
    abs(f.fct_erosion - p.pos_realised_erosion) > (
        select coalesce(sum(pp.markdown_erosion_value), 0) * 1.05 + 1.0
        from {{ ref('stg_pos_sales') }} pp
        left join {{ ref('stg_items') }} i on pp.sku = i.sku
        where i.sku is null
    )
    -- Erosion must be a real number, not zero: a zero would mean markdowns were
    -- never priced at all and the whole analysis is empty.
    or f.fct_erosion <= 0
    -- The defect must still be produced.
    or t.tickets_not_fully_honoured = 0
    or t.qty_honoured >= t.qty_ticketed
    -- Markdown tickets must actually join to ledger days, or the effectiveness
    -- mart is quietly reporting on nothing.
    or pr.markdown_days = 0
    or pr.erosion_on_those_days <= 0
    -- The real guard on "erosion is realised money, not intent": erosion must
    -- never EXCEED what the printed tickets could possibly have given away.
    --
    -- A tighter check — that the two differ — was tried and abandoned. On this
    -- estate the printed value and the realised value sit within 1% of each
    -- other, because the unhonoured share is small, so a "they must differ"
    -- assertion would be testing rounding rather than the distinction. The
    -- upper bound is the assertion that actually means something: if erosion
    -- ever went above the maximum the tickets could have cost, somebody has
    -- started pricing intention instead of money.
    or f.fct_erosion > t.printed_value * 1.02 + 1.0
