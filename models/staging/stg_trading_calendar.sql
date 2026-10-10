{{ config(materialized='view') }}

{#-
  The trading calendar, from the store system's own feed.

  This model deliberately reads the *landed* calendar rather than any file in
  `data/truth`. That distinction is the whole point of the project: a warehouse
  that checks itself against the simulator's answer key is not measuring
  anything. Every number here has to come from something a real source system
  would have published.

  The model is what makes D03 visible. The cross product of (store, trading day)
  is the set of store-days a complete feed *should* contain, so a coverage gap
  is the difference between this and what POS actually delivered — and it is
  computable without anyone telling the pipeline a file went missing.
-#}
select
    cast(c.trading_date as date)                         as trading_date,
    extract(isodow from cast(c.trading_date as date))    as isodow,
    dayname(cast(c.trading_date as date))                as day_name,
    (extract(isodow from cast(c.trading_date as date)) >= 6) as is_weekend,
    cast(c.month_number as integer)                      as month_number,
    date_trunc('month', cast(c.trading_date as date))    as month_start,
    date_trunc('week', cast(c.trading_date as date))     as week_start,
    c.is_closed,
    c.holiday_name,
    c.special_day,
    c.is_school_holiday,
    c.school_break,
    c.day_type,
    cast(c.footfall_index as decimal(6, 4))              as footfall_index,
    -- Name the trading context once, here, so every downstream model and the
    -- report describe the same day the same way.
    case
        when c.holiday_name is not null      then cast(c.holiday_name as varchar)
        when c.special_day is not null       then cast(c.special_day as varchar)
        when c.is_school_holiday             then 'School holidays'
        else 'Ordinary trading'
    end                                                  as trading_context,
    -- Days to Christmas. The run-up, not the day, is what moves a fresh
    -- department, so this is a countdown rather than a flag.
    cast(date_diff('day', cast(c.trading_date as date), date '2025-12-25') as integer)
                                                         as days_to_christmas,
    -- The countdown matters only in one direction; negative values are the
    -- months after, which behave like any other period.
    cast(greatest(0, date_diff('day', cast(c.trading_date as date), date '2025-12-25')) as integer)
                                                         as days_before_christmas
from {{ source('raw', 'trading_calendar') }} c
