{{ config(materialized='table') }}

{#-
  Store dimension.

  The operational attributes come from `int_store_portfolio`, which derives them
  from observed behaviour rather than from a config. A store's scan compliance
  and markdown discipline are measured, not self-reported.
-#}
select
    p.store_code                                             as store_key,
    p.store_code,
    p.store_name,
    p.suburb,
    p.postcode,
    p.store_format,
    p.store_type,
    p.is_small_format,
    p.size_index,
    p.opened_date,
    p.region,
    p.timezone,
    p.open_days,
    -- Loss-scan behaviour. A store at 0.80 is telling you a fifth of its loss is
    -- invisible to the scan file, which is why recorded waste is a floor.
    p.scan_events,
    p.qty_scanned,
    p.days_with_a_scan,
    p.scan_day_coverage,
    p.unmapped_reason_events,
    -- Markdown behaviour.
    p.markdown_tickets,
    p.qty_ticketed,
    p.qty_honoured,
    p.qty_not_honoured,
    p.days_with_a_markdown,
    p.markdown_discipline_observed,
    p.markdown_honour_rate,
    p.avg_markdown_depth,
    -- The single number the report uses to say "this store's records are
    -- thinner than that store's": share of open days with no loss scan at all.
    case
        when p.open_days > 0
            then cast(round(1 - (p.days_with_a_scan::double / p.open_days), 4) as decimal(6, 4))
    end                                                      as days_with_no_scan_share,
    (p.scan_day_coverage is null or p.scan_day_coverage < 0.9) as has_thin_scan_records
from {{ ref('int_store_portfolio') }} p
