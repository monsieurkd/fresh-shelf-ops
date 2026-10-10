{{ config(materialized='view') }}

{#-
  Catalogue promotions.

  Kept as date *ranges* rather than exploded to days, because the source
  publishes a plan and the expansion belongs where it is consumed. The estate
  runs one plan; every store that ranges the line is affected, which is why
  there is no store column here.

  `is_future` is the reason this model exists at all: planning publishes a month
  ahead, so this table covers dates the trading data cannot yet show. A naive
  `where start_date <= today` would drop the very promotions the forecast most
  needs.
-#}
select
    p.sku,
    p.promo_name,
    cast(p.start_date as date)                           as start_date,
    cast(p.end_date as date)                             as end_date,
    p.promo_type,
    cast(p.discount_pct as decimal(6, 4))                as discount_pct,
    cast(p.demand_uplift_factor as decimal(8, 4))        as demand_uplift_factor,
    p.catalogue_week,
    cast(date_diff('day', cast(p.start_date as date), cast(p.end_date as date)) + 1 as integer)
                                                         as promo_days,
    -- Published after the last closed trading day: the plan, not the history.
    (cast(p.start_date as date) > date '{{ var("as_of_date") }}' - interval 1 day)
                                                         as is_future,
    -- A catalogue feature is a volume play; a weekly special is a price play.
    -- The two move units by very different amounts.
    (p.promo_type = 'catalogue feature')                 as is_catalogue_feature
from {{ source('raw', 'promotions') }} p
