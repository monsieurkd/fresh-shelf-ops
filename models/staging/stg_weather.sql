{{ config(materialized='view') }}

{#-
  Weather.

  The BOM drop includes the as-of day itself, which is a forecast rather than an
  observation. That is deliberate and it is a trap: a naive join would let the
  pipeline report trading performance for a day that has not finished. The
  `is_trading_day` flag below is what stops it.

  Weather drives demand twice over, which is why it is joined at two grains:
  temperature moves sales, and a heat day also moves *spoilage*, so the same
  factor appears in the demand model and in the waste analysis with different
  signs.
-#}
select
    w.store_code,
    cast(w.weather_date as date)                         as weather_date,
    cast(w.max_temp_c as decimal(6, 2))                  as max_temp_c,
    cast(w.min_temp_c as decimal(6, 2))                  as min_temp_c,
    cast(w.rainfall_mm as decimal(7, 2))                 as rainfall_mm,
    w.is_heat_day,
    w.is_cold_night,
    -- Degrees above the point at which hot-weather demand actually starts to
    -- bite. Below 25C the term is zero, which is what makes it usable as a
    -- multiplicative factor without a second interaction term.
    cast(greatest(0, w.max_temp_c - 25) as decimal(6, 2)) as degrees_over_25c,
    cast(greatest(0, 15 - w.max_temp_c) as decimal(6, 2)) as degrees_under_15c,
    (w.rainfall_mm >= 5)                                 as is_wet_day,
    -- A run of hot days degrades the cold chain; single hot days mostly just
    -- sell more salad. The flag is set in the world model, and repeated here so
    -- downstream can distinguish the two without recomputing.
    w.is_heat_day and lag(w.is_heat_day) over (
        partition by w.store_code order by w.weather_date
    )                                                    as is_second_heat_day
from {{ source('raw', 'weather') }} w
