{% macro tz_name() -%}
    {{ return(var('timezone', 'Australia/Adelaide')) }}
{%- endmacro %}

{#
  Local business time.

  The loss scanners write UTC while every other feed writes Adelaide local
  time, and Adelaide is +9:30 ACST / +10:30 ACDT — a 30-minute offset, not an
  hour, and the DST switch lands mid-service. So the conversion is done in the
  warehouse with the IANA zone (DuckDB bundles ICU) rather than with an offset
  someone hard-coded, and the *business date* is always derived from the local
  timestamp — never from the date part of the raw string.
#}
{% macro local_ts(utc_ts) -%}
    ({{ utc_ts }} AT TIME ZONE '{{ tz_name() }}')
{%- endmacro %}

{% macro local_business_date(utc_ts, as_date=true) -%}
    {%- if as_date -%}
        CAST(({{ utc_ts }} AT TIME ZONE '{{ tz_name() }}') AS DATE)
    {%- else -%}
        ({{ utc_ts }} AT TIME ZONE '{{ tz_name() }}')
    {%- endif -%}
{%- endmacro %}

{#
  Value of a quantity at a unit cost, rounded to cents. Money is kept in
  DECIMAL(12,2) end to end so a sum of 1.2M line values does not accumulate
  float error in a report that quotes dollars.
#}
{% macro money(expression) -%}
    CAST(ROUND({{ expression }}, 2) AS DECIMAL(12, 2))
{%- endmacro %}
