{{ config(materialized='table') }}

{#-
  Date dimension.

  Carries the retail attributes that actually move fresh demand, not just the
  calendar ones. `trading_context` and `days_before_christmas` are here because
  the run-up to Christmas is the single largest demand event in an Australian
  supermarket and it is not recoverable from the date alone.
-#}
select
    trading_date                                             as date_key,
    isodow,
    day_name,
    is_weekend,
    month_number,
    month_start,
    week_start,
    date_trunc('quarter', trading_date)                      as quarter_start,
    year(trading_date)                                       as calendar_year,
    (month_number in (12, 1, 2))                             as is_summer,
    (month_number in (6, 7, 8))                              as is_winter,
    not is_closed                                            as is_trading_day,
    is_closed,
    holiday_name,
    special_day,
    is_school_holiday,
    school_break,
    day_type,
    trading_context,
    days_to_christmas,
    days_before_christmas,
    footfall_index
from {{ ref('stg_trading_calendar') }}
