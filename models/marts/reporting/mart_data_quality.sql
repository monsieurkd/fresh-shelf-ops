{{ config(materialized='table') }}

{#-
  Data quality: what the pipeline could not trust, quantified in business terms.

  Every row here is a defect from `config/defect_catalogue.yml` made *visible and
  priced*, so the report can say "these figures exclude $X of revenue the item
  master does not recognise" rather than quietly dropping it. That distinction is
  the reason the pipeline has a quality mart at all: a defect that is not
  measured turns into an unexplained variance somebody has to chase later.

  Three classes are reported:

    * **Coverage** — store-days the estate traded but no feed arrived for (D03).
      These are EXCLUDED from sell-through, not read as zero sales, which is why
      the exclusion has to be counted.
    * **Item-master gaps** — revenue on SKUs the master does not carry (D09).
      Preserved in the ledger, quantified here.
    * **Feed hygiene** — late files, redeliveries, mixed money units, unmapped
      loss reasons (D01, D02, D05, D08). Not wrong, but worth watching, because
      each one is a warning sign that a source system changed.
-#}
with fct as (

    select *
    from {{ ref('fct_store_item_day') }}

),

coverage as (

    select
        store_code,
        count(distinct business_date) filter (where is_pos_gap)      as days_with_no_feed,
        count(distinct business_date)                                as days_observed,
        count(*) filter (where is_pos_gap)                           as store_item_days_with_no_feed,
        count(*)                                                     as store_item_days
    from fct
    group by 1

),

unmapped as (

    -- Revenue on SKUs the item master does not know about. The rows survive the
    -- load (they must), so this measures what would have been lost by an inner
    -- join.
    select
        p.store_code,
        count(distinct p.sku)                                        as unmapped_skus,
        sum(p.units_sold)                                            as unmapped_units,
        cast(sum(p.net_sales_value) as decimal(14, 2))               as unmapped_net_sales_value,
        count(*)                                                     as unmapped_rows
    from {{ ref('stg_pos_sales') }} p
    left join {{ ref('stg_items') }} i on p.sku = i.sku
    where i.sku is null
    group by 1

),

file_hygiene as (

    select
        source,
        count(*) filter (where status = 'duplicate')                  as duplicate_files,
        count(*) filter (where status = 'loaded')                     as loaded_files,
        count(*) filter (where lag_days > 0)                          as late_files,
        max(lag_days)                                                 as max_lag_days,
        sum(rows_quarantined)                                         as quarantined_rows,
        sum(rows_loaded)                                              as rows_loaded
    from ops.ingest_files
    group by 1

),

schedule_health as (

    -- How late the estate's feeds typically run, and whether any store is
    -- persistently behind rather than occasionally.
    select
        source,
        count(*)                                                      as files,
        count(*) filter (where lag_days > 0)                          as late,
        cast(avg(lag_days) as decimal(8, 4))                          as avg_lag_days,
        cast(max(lag_days) as integer)                                as worst_lag_days
    from ops.ingest_files
    where status = 'loaded'
    group by 1

)

select
    c.store_code,
    c.days_observed,
    c.days_with_no_feed,
    case
        when c.days_observed > 0
            then cast(round(c.days_with_no_feed::double / c.days_observed, 4) as decimal(6, 4))
    end                                                              as feed_gap_rate,
    c.store_item_days_with_no_feed,
    c.store_item_days,
    coalesce(u.unmapped_skus, 0)                                     as unmapped_skus,
    coalesce(u.unmapped_units, 0)                                    as unmapped_units,
    coalesce(u.unmapped_net_sales_value, 0)                          as unmapped_net_sales_value,
    coalesce(u.unmapped_rows, 0)                                     as unmapped_rows,
    -- Mixed money units and void lines are counted where they happen (they are
    -- per store-day), so they are aggregated from the fact table.
    (select count(*) from fct where store_code = c.store_code and had_mixed_money_units)
                                                                     as days_with_mixed_money_units,
    (select count(*) from fct where store_code = c.store_code and had_voids)
                                                                     as days_with_voids,
    (select count(*) from fct where store_code = c.store_code and has_unmapped_reason)
                                                                     as days_with_unmapped_reason,
    (select count(*) from fct where store_code = c.store_code and not has_pos_data and not is_closed)
                                                                     as store_item_days_unconfirmed,
    -- Was this store's record thin enough that its numbers deserve a caveat?
    (c.days_with_no_feed > 0
     or coalesce(u.unmapped_net_sales_value, 0) > 0
     or exists (select 1 from fct where store_code = c.store_code and had_mixed_money_units)
    )                                                                as has_reported_caveat
from coverage c
left join unmapped u on c.store_code = u.store_code

union all

-- One row per source, so a feed that starts arriving late or quarantining rows
-- shows up as a trend rather than as a mystery variance. These rows carry no
-- store and are keyed by source instead.
select
    'SOURCE:' || f.source                                              as store_code,
    f.loaded_files                                                     as days_observed,
    f.duplicate_files                                                  as days_with_no_feed,
    case
        when f.loaded_files > 0
            then cast(round(f.duplicate_files::double / f.loaded_files, 4) as decimal(6, 4))
    end                                                                as feed_gap_rate,
    f.quarantined_rows                                                 as store_item_days_with_no_feed,
    f.rows_loaded                                                      as store_item_days,
    f.duplicate_files                                                  as unmapped_skus,
    f.late_files                                                       as unmapped_units,
    f.rows_loaded * 0                                                  as unmapped_net_sales_value,
    f.late_files                                                       as unmapped_rows,
    f.max_lag_days                                                     as days_with_mixed_money_units,
    0                                                                  as days_with_voids,
    0                                                                  as days_with_unmapped_reason,
    sh.worst_lag_days                                                  as store_item_days_unconfirmed,
    (f.duplicate_files > 0 or f.late_files > 0 or coalesce(f.quarantined_rows, 0) > 0)
                                                                       as has_reported_caveat
from file_hygiene f
left join schedule_health sh on f.source = sh.source
