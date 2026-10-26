{{ config(materialized='table') }}

{#-
  The daily stock ledger: one row per store, item and trading day.

  This is the spine of the project. Every loss number the report quotes is a
  term in this ledger, and the reason it is built this way is that the two
  obvious shortcuts are both wrong:

  * **"Waste = what the scan file says" is wrong (D07).** Scans are filed when
    a handheld is to hand; a busy Sunday evening loses a fifth of them. So
    recorded waste is a *floor*, and the size of the gap has to be measured, not
    assumed.
  * **"Sales + waste = what we bought" is wrong.** It ignores preparation loss
    (a trim is a real loss but not waste), rejected deliveries, and counting
    error. Assuming it holds turns ordinary operational noise into a fictitious
    theft problem.

  So the ledger states each movement separately and then reconciles:

      opening + received - items sold - prep loss - recorded waste
        = expected closing

      expected closing vs the last physical count = the residue

  The residue is where unrecorded shrink lives. It is reported, not hidden, and
  it is *bounded* by the count's own reliability. A count eyeballed to +/-3%
  cannot resolve a 1% loss, which is why the reconciliation carries the count
  error forward rather than pretending the closing number is exact.
-#}
with calendar as (

    select trading_date, is_closed, is_weekend, isodow, month_number,
           holiday_name, is_school_holiday, trading_context, days_before_christmas
    from {{ ref('stg_trading_calendar') }}

),

stores as (

    select store_code, store_format, size_index, is_small_format
    from {{ ref('stg_stores') }}

),

items as (

    select
        sku, item_name, department_code, department_name, category,
        unit_of_measure, is_weighed, pack_size, retail_price, standard_cost,
        gross_margin_per_unit, gross_margin_rate, shelf_life_days,
        life_at_delivery_days, supplier_code, is_markdown_eligible,
        is_short_life, is_waste_risk
    from {{ ref('stg_items') }}

),

-- The set of store-days the estate was open. This cross product is also the
-- completeness check for D03: a store-day that is open but has no POS row is a
-- feed gap, not a day with no sales.
open_store_days as (

    select
        s.store_code,
        c.trading_date,
        c.is_closed,
        c.is_weekend,
        c.isodow,
        c.month_number,
        c.holiday_name,
        c.is_school_holiday,
        c.trading_context,
        c.days_before_christmas
    from stores s
    cross join calendar c

),

sales as (

    select
        store_code,
        business_date,
        sku,
        sum(units_sold)                     as units_sold,
        sum(net_sales_value)                as net_sales_value,
        sum(gross_sales_value)              as gross_sales_value,
        sum(markdown_erosion_value)         as markdown_erosion_value,
        sum(transaction_count)              as transaction_count,
        max(money_unit_as_filed)            as money_unit_as_filed,
        max(had_mixed_money_units)          as had_mixed_money_units,
        max(had_voids)                      as had_voids,
        avg(avg_unit_price)                 as avg_unit_price
    from {{ ref('stg_pos_sales') }}
    group by 1, 2, 3

),

received as (

    select
        store_code,
        delivery_date    as business_date,
        sku,
        sum(qty_ordered)             as qty_ordered,
        sum(qty_received)            as qty_received,
        sum(qty_rejected)            as qty_rejected,
        sum(received_cost_value)     as received_cost_value,
        count(*)                     as delivery_lines,
        max(days_of_life_at_delivery) as max_life_at_delivery,
        min(days_of_life_at_delivery) as min_life_at_delivery,
        max(was_substituted)         as had_substitution,
        avg(unit_cost)               as avg_unit_cost
    from {{ ref('stg_goods_received') }}
    group by 1, 2, 3

),

-- Loss scans, split by what the loss actually was. A preparation trim is a
-- cost of doing business; waste is what the project is trying to remove.
--
-- The scan's VALUE is computed at the item's standard cost rather than at a
-- cost reconstructed from the delivery history. That is a simplification with a
-- known direction: standard cost lags seasonal cost movements, so a produce scan
-- in a dear season is slightly under-valued.
scanned_loss as (

    select
        w.store_code,
        w.business_date,
        w.sku,
        sum(case when w.loss_class = 'waste' then w.qty_disposed else 0 end)       as qty_waste_scanned,
        sum(case when w.loss_class = 'preparation' then w.qty_disposed else 0 end) as qty_prep_scanned,
        sum(case when w.loss_class not in ('waste', 'preparation')
                 then w.qty_disposed else 0 end)                                   as qty_other_scanned,
        count(*)                                                                   as scan_events,
        max(w.is_unmapped_reason)                                                  as has_unmapped_reason
    from {{ ref('stg_waste_events') }} w
    group by 1, 2, 3

),

counts as (

    select
        store_code,
        sku,
        count_date as business_date,
        sum(counted_qty)                 as counted_qty,
        count(*)                         as count_events,
        max(is_full_count)               as has_full_count,
        max(was_counted_in_packs)        as had_pack_count,
        max(pack_size)                   as counted_pack_size
    from {{ ref('stg_stock_counts') }}
    group by 1, 2, 3

),

markdown as (

    select
        store_code,
        business_date,
        sku,
        sum(qty_ticketed)            as qty_ticketed,
        sum(qty_honoured)            as qty_honoured,
        sum(qty_sold_at_markdown)    as qty_sold_at_markdown,
        sum(qty_ticket_not_honoured) as qty_not_honoured,
        max(markdown_pct)            as max_markdown_pct,
        avg(markdown_pct)            as avg_markdown_pct,
        count(*)                     as markdown_tickets
    from {{ ref('stg_markdown_events') }}
    group by 1, 2, 3

)

select
    osd.store_code,
    s.size_index,
    s.store_format,
    s.is_small_format,
    i.sku,
    i.item_name,
    i.department_code,
    i.department_name,
    i.category,
    i.unit_of_measure,
    i.is_weighed,
    i.pack_size,
    i.retail_price,
    i.standard_cost,
    i.gross_margin_per_unit,
    i.gross_margin_rate,
    i.shelf_life_days,
    i.life_at_delivery_days,
    i.supplier_code,
    i.is_markdown_eligible,
    i.is_short_life,
    i.is_waste_risk,
    osd.trading_date                                             as business_date,
    osd.is_closed,
    osd.is_weekend,
    osd.isodow,
    osd.month_number,
    osd.holiday_name,
    osd.is_school_holiday,
    osd.trading_context,
    osd.days_before_christmas,

    -- ---- what came in ------------------------------------------------------
    coalesce(r.qty_ordered, 0)                                   as qty_ordered,
    coalesce(r.qty_received, 0)                                  as qty_received,
    coalesce(r.qty_rejected, 0)                                  as qty_rejected,
    coalesce(r.received_cost_value, 0)                           as received_cost_value,
    coalesce(r.delivery_lines, 0)                                as delivery_lines,
    r.min_life_at_delivery,
    r.max_life_at_delivery,
    coalesce(r.had_substitution, false)                          as had_substitution,
    r.avg_unit_cost,

    -- ---- what went out -----------------------------------------------------
    coalesce(sa.units_sold, 0)                                   as units_sold,
    coalesce(sa.net_sales_value, 0)                              as net_sales_value,
    coalesce(sa.gross_sales_value, 0)                            as gross_sales_value,
    coalesce(sa.markdown_erosion_value, 0)                       as markdown_erosion_value,
    coalesce(sa.transaction_count, 0)                            as transaction_count,
    sa.avg_unit_price,
    sa.money_unit_as_filed,
    coalesce(sa.had_mixed_money_units, false)                    as had_mixed_money_units,
    coalesce(sa.had_voids, false)                                as had_voids,
    -- Was a POS row present at all? False on a day the store was open means a
    -- missing extract (D03), which is a data gap and not a zero-sales day.
    (sa.sku is not null)                                         as has_pos_data,
    (not osd.is_closed and sa.sku is null)                       as is_pos_gap,

    -- Loss. These are the SCANNED quantities: a floor, not the truth.
    coalesce(sl.qty_waste_scanned, 0)                            as qty_waste_scanned,
    coalesce(sl.qty_prep_scanned, 0)                             as qty_prep_scanned,
    coalesce(sl.qty_other_scanned, 0)                            as qty_other_scanned,
    coalesce(sl.scan_events, 0)                                  as scan_events,
    coalesce(sl.has_unmapped_reason, false)                      as has_unmapped_reason,
    -- Recorded waste at standard cost. This is the number the P&L sees, and it
    -- is smaller than the real one by exactly the unscanned share.
    round(coalesce(sl.qty_waste_scanned, 0) * i.standard_cost, 2) as recorded_waste_cost,
    round(coalesce(sl.qty_prep_scanned, 0) * i.standard_cost, 2)  as prep_loss_cost,

    -- Markdown: what was PRINTED versus what was HONOURED at the till. The gap
    -- is the compliance problem (D13), and it is priced so it can be managed.
    coalesce(md.qty_ticketed, 0)                                 as qty_ticketed,
    coalesce(md.qty_honoured, 0)                                 as qty_honoured,
    coalesce(md.qty_sold_at_markdown, 0)                         as qty_sold_at_markdown,
    coalesce(md.qty_not_honoured, 0)                             as qty_ticket_not_honoured,
    md.max_markdown_pct,
    md.avg_markdown_pct,
    coalesce(md.markdown_tickets, 0)                             as markdown_tickets,

    -- ---- what was left -----------------------------------------------------
    coalesce(ct.counted_qty, null)                               as counted_qty,
    coalesce(ct.count_events, 0)                                 as count_events,
    coalesce(ct.has_full_count, false)                           as has_full_count,
    coalesce(ct.had_pack_count, false)                           as had_pack_count,
    (ct.sku is not null)                                         as was_counted,

    -- ---- the reconciliation ------------------------------------------------
    -- Expected closing stock from the ledger alone: what we had, plus what
    -- arrived, less everything the records say left. Counts are NOT used here;
    -- the point is to compare the record against the count.
    --
    -- Note what is *excluded*: unrecorded shrink is by definition absent from
    -- every term, so it lands in the difference. That is the design.
    round(
        coalesce(r.qty_received, 0)
        - coalesce(sa.units_sold, 0)
        - coalesce(sl.qty_waste_scanned, 0)
        - coalesce(sl.qty_prep_scanned, 0)
        - coalesce(sl.qty_other_scanned, 0)
    , 3)                                                         as ledger_net_movement,
    -- Only comparable on a day that was actually counted.
    case
        when ct.sku is not null
            then round(coalesce(ct.counted_qty, 0) - (
                coalesce(r.qty_received, 0)
                - coalesce(sa.units_sold, 0)
                - coalesce(sl.qty_waste_scanned, 0)
                - coalesce(sl.qty_prep_scanned, 0)
                - coalesce(sl.qty_other_scanned, 0)
            ), 3)
    end                                                          as count_residue_qty
from open_store_days osd
join stores s on osd.store_code = s.store_code
cross join items i
left join received r
    on osd.store_code = r.store_code and osd.trading_date = r.business_date and i.sku = r.sku
left join sales sa
    on osd.store_code = sa.store_code and osd.trading_date = sa.business_date and i.sku = sa.sku
left join scanned_loss sl
    on osd.store_code = sl.store_code and osd.trading_date = sl.business_date and i.sku = sl.sku
left join counts ct
    on osd.store_code = ct.store_code and osd.trading_date = ct.business_date and i.sku = ct.sku
left join markdown md
    on osd.store_code = md.store_code and osd.trading_date = md.business_date and i.sku = md.sku
-- A closed day still costs money: the stock ages and is disposed of when the
-- doors reopen. Keeping closed days in the ledger is what stops a Christmas
-- closure reading as a suspiciously clean week.
