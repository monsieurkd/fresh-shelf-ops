{{ config(materialized='table') }}

{#-
  The store portfolio, with the risk attributes the rest of the project needs.

  Kept deliberately thin. Store attributes that matter for waste are not the
  usual retail ones (size, format, region) but the operational ones: how often
  disposals actually get scanned, and how systematically the store runs its
  markdown pass. Both are derived here from *observed behaviour in the data*
  rather than from a config, because the whole point of measuring a store's
  discipline is that the store does not self-report it.

  `scan_compliance_observed` is the load-bearing one. It is the ratio of loss
  scans the store filed to the loss the ledger says it incurred, and it is what
  turns recorded waste from a number into a lower bound with a known size. A
  store at 0.80 is telling you a fifth of its loss is invisible to the scan file.
-#}
with scans as (

    select
        store_code,
        count(*)                         as scan_events,
        sum(qty_disposed)                as qty_scanned,
        count(distinct business_date)    as days_with_a_scan,
        count(*) filter (where is_unmapped_reason) as unmapped_reason_events
    from {{ ref('stg_waste_events') }}
    group by 1

),

markdown as (

    select
        store_code,
        count(*)                                             as markdown_tickets,
        sum(qty_ticketed)                                    as qty_ticketed,
        sum(qty_honoured)                                    as qty_honoured,
        count(distinct business_date)                        as days_with_a_markdown,
        avg(markdown_pct)                                    as avg_markdown_depth,
        sum(qty_ticket_not_honoured)                         as qty_not_honoured
    from {{ ref('stg_markdown_events') }}
    group by 1

),

calendar as (

    select
        count(distinct trading_date) filter (where not is_closed) as open_days
    from {{ ref('stg_trading_calendar') }}
)

select
    s.store_code,
    s.store_name,
    s.suburb,
    s.postcode,
    s.store_format,
    s.store_type,
    s.is_small_format,
    s.size_index,
    s.opened_date,
    s.region,
    s.timezone,

    -- Trading days the store was actually open over the window.
    c.open_days,

    -- Loss-scan behaviour.
    coalesce(sc.scan_events, 0)                              as scan_events,
    coalesce(sc.qty_scanned, 0)                              as qty_scanned,
    coalesce(sc.days_with_a_scan, 0)                         as days_with_a_scan,
    coalesce(sc.unmapped_reason_events, 0)                   as unmapped_reason_events,
    case
        when c.open_days > 0 then cast(sc.days_with_a_scan as double) / c.open_days
    end                                                      as scan_day_coverage,

    -- Markdown behaviour. `markdown_discipline_observed` is how often the store
    -- ran a pass at all, which is the behaviour that separates a store that
    -- manages its shelf from one that discovers it at the bin.
    coalesce(md.markdown_tickets, 0)                         as markdown_tickets,
    coalesce(md.qty_ticketed, 0)                             as qty_ticketed,
    coalesce(md.qty_honoured, 0)                             as qty_honoured,
    coalesce(md.qty_not_honoured, 0)                         as qty_not_honoured,
    coalesce(md.days_with_a_markdown, 0)                     as days_with_a_markdown,
    case
        when c.open_days > 0
            then cast(md.days_with_a_markdown as double) / c.open_days
    end                                                      as markdown_discipline_observed,
    case
        when md.qty_ticketed > 0
            then cast(md.qty_honoured / md.qty_ticketed as decimal(8, 4))
    end                                                      as markdown_honour_rate,
    cast(md.avg_markdown_depth as decimal(6, 4))             as avg_markdown_depth
from {{ ref('stg_stores') }} s
left join scans sc on s.store_code = sc.store_code
left join markdown md on s.store_code = md.store_code
cross join calendar c
