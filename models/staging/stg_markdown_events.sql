{{ config(materialized='view') }}

{#-
  Markdown tickets.

  Used for *depth and timing* only. The money never comes from here: a printed
  ticket is not always honoured at the till (D13), so erosion is measured from
  the realised discount in POS. The gap between the two is a compliance problem
  worth its own number, which is why both survive to the marts.
-#}
select
    cast(m.business_date as date)                        as business_date,
    m.store_code,
    m.sku,
    cast(m.printed_ts_local as timestamp)                as printed_ts_local,
    extract(hour from cast(m.printed_ts_local as timestamp)) as printed_hour,
    cast(m.markdown_pct as decimal(6, 4))                as markdown_pct,
    cast(m.base_price as decimal(10, 4))                 as base_price,
    cast(m.markdown_price as decimal(10, 4))             as markdown_price,
    cast(m.qty_ticketed as decimal(12, 3))               as qty_ticketed,
    cast(m.qty_honoured as decimal(12, 3))               as qty_honoured,
    cast(m.qty_sold_at_markdown as decimal(12, 3))       as qty_sold_at_markdown,
    m.pass_name,
    m.trigger,
    m.unit_of_measure,
    -- The compliance gap: units that carried a ticket but were not rung at the
    -- marked price.
    cast(m.qty_ticketed - m.qty_honoured as decimal(12, 3)) as qty_ticket_not_honoured,
    case
        when m.qty_ticketed > 0
            then cast(m.qty_honoured / m.qty_ticketed as decimal(8, 4))
    end                                                  as honour_rate,
    -- The depth ladder is a fixed set of printed discounts; anything else is a
    -- manual override and worth seeing.
    (round(m.markdown_pct, 2) in (
        {%- for depth in var('markdown_depths') -%}
        {{ depth }}{{ ", " if not loop.last }}
        {%- endfor -%}
    ))                                                    as is_standard_depth
from {{ source('raw', 'markdown_events') }} m
