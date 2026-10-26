{{ config(materialized='view') }}

{#-
  Sales, netted to one row per store-day-SKU.

  Three things happen here, and all three are corrections rather than
  transformations:

  1. **Voids are netted, not dropped (D10).** A refund arrives as a negative
     line in the same extract as the sale it reverses. Summing keeps the day
     honest; filtering negatives out would overstate sales by exactly the
     refunds, which is the quiet way a report gets a number wrong.

  2. **The money unit is carried through, not re-derived (D05).** The loader
     already normalised cents to dollars using the source file's own header
     marker. This column is kept so the conversion stays auditable and a
     surprise is visible rather than assumed away.

  3. **Unmapped SKUs are left in (D09).** The join to the item master happens
     downstream as a LEFT join. Dropping a SKU the master has not caught up with
     would silently delete revenue; the quality mart quantifies it instead.
-#}
with netted as (

    select
        cast(business_date as date)                      as business_date,
        store_code,
        sku,
        sum(cast(units as decimal(14, 3)))               as units_sold,
        sum(cast(gross_sales as decimal(14, 2)))         as gross_sales_value,
        sum(cast(discount_amount as decimal(14, 2)))     as markdown_erosion_value,
        sum(cast(net_sales as decimal(14, 2)))           as net_sales_value,
        sum(cast(transaction_count as integer))          as transaction_count,
        count(*)                                         as pos_rows,
        count(*) filter (where cast(units as decimal(14, 3)) < 0) as void_rows,
        -- A day's file for a store can only have claimed one money unit; if it
        -- claimed two, the source is confused about what it sent.
        count(distinct money_unit_as_filed)              as money_unit_variants,
        max(money_unit_as_filed)                         as money_unit_as_filed,
        max(unit_of_measure)                             as unit_of_measure,
        count(distinct promo_name)                       as promo_variants
    from {{ source('raw', 'pos_sales') }}
    group by 1, 2, 3

)

select
    business_date,
    store_code,
    sku,
    units_sold,
    gross_sales_value,
    markdown_erosion_value,
    net_sales_value,
    transaction_count,
    pos_rows,
    void_rows,
    (void_rows > 0)                                      as had_voids,
    (money_unit_variants > 1)                            as had_mixed_money_units,
    money_unit_as_filed,
    unit_of_measure,
    promo_variants,
    -- Average selling price actually achieved. For a weighed line this is
    -- $/kg; for an each it is $/unit. It is the cleanest single check that
    -- units and money agree, because a unit error moves it by orders of
    -- magnitude while a genuine price change moves it by cents.
    case
        when units_sold > 0
            then cast(net_sales_value / units_sold as decimal(12, 4))
    end                                                  as avg_unit_price,
    -- Sold below the item's list price. Evidence a markdown was honoured.
    (markdown_erosion_value > 0)                         as sold_with_markdown
from netted
