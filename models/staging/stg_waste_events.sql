{{ config(materialized='view') }}

{#-
  Loss scans, with the reason code mapped to a group (D08).

  The raw code is kept alongside the group on purpose. A firmware update that
  introduces a new label must show up as `unmapped` and set off an alert —
  defaulting it to 'Other' would make a new code indistinguishable from the
  codes that genuinely mean "other", and the first anyone would know about it is
  a loss report that quietly stopped adding up.

  The timestamp is carried in both zones so the conversion stays auditable: a
  scan filed at 14:30 UTC is a 00:00 ACDT disposal, and an off-by-a-day filing
  is exactly what D06 exists to catch.
-#}
select
    w.store_code,
    w.sku,
    cast(w.business_date as date)                        as business_date,
    cast(w.event_ts_utc as timestamp)                    as event_ts_utc,
    cast(w.event_ts_local as timestamp)                  as event_ts_local,
    extract(hour from cast(w.event_ts_local as timestamp))    as local_hour,
    extract(isodow from cast(w.event_ts_local as timestamp))  as local_isodow,
    cast(w.qty as decimal(12, 3))                        as qty_disposed,
    w.reason_code_raw,
    -- Trim and case-fold before matching: the same firmware writes
    -- 'EXPIRED', 'expired ' and 'expired' for one event.
    upper(trim(w.reason_code_raw))                       as reason_code_key,
    coalesce(m.reason_group, 'unmapped')                 as reason_group,
    coalesce(m.loss_class, 'unknown')                    as loss_class,
    (m.raw_code is null)                                 as is_unmapped_reason,
    w.unit_of_measure,
    w.staff_id,
    w.device_id,
    w.disposal_method,
    -- Afternoon/evening disposals are the ones that can cross a UTC day
    -- boundary; flag them so the boundary case is countable, not theoretical.
    (extract(hour from cast(w.event_ts_local as timestamp)) >= 14) as is_late_day_scan
from {{ source('raw', 'waste_events') }} w
left join {{ ref('waste_reason_map') }} m
    on upper(trim(w.reason_code_raw)) = upper(trim(m.raw_code))
