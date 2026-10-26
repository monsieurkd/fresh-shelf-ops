# Data dictionary

What each table is, what grain it has, and where a column is easy to
misunderstand.

Three loss concepts recur and are never summed:

- Markdown erosion: margin given away to sell stock before it expires. Valued at
  margin.
- Recorded waste: scanned disposals. Valued at cost. A floor, not the loss.
- Unrecorded shrink: loss that no record explains. A bound, not a measurement.

## Schemas

| Schema | Holds | Written by |
|---|---|---|
| `raw` | Landed source feeds, typed and de-duplicated | Python loader |
| `ops` | Run metadata: ingested files, quarantined rows | Python loader |
| `seeds` | Curated reference data (holidays, reason map) | `dbt seed` |
| `staging` | One view per feed, cleaned | dbt |
| `intermediate` | The ledger and its reconciliations | dbt |
| `marts` | Star schema: `dim_*`, `fct_*` | dbt |
| `reporting` | Aggregates the report reads | dbt |
| `analytics` | Forecasts, plans, impact | Decision layer |

## Landing zone

`data/landing/<source>/<landing_date>/`, partitioned by arrival date rather than
business date. That is what makes a late file land in the right place, and what
forces the loader to read the business date from the file contents.

| Source | One file per | Notes |
|---|---|---|
| `pos_sales` | store-day | Header carries `money_unit` and the timezone |
| `goods_received` | day | Delivery lines |
| `waste_events` | device-day | Header carries `tz=UTC` |
| `stock_counts` | day | Cycle and full counts |
| `markdown_events` | day | Printed tickets |
| `promotions` | extract | Date ranges, published ahead |
| `weather` | extract | Includes the as-of day, as a forecast |
| `item_master`, `store_master`, `supplier_master` | extract | Reference data |
| `trading_calendar` | extract | Which days the doors were open |

`data/truth/` holds the sealed answer key and pipeline metadata
(`landing_gaps.csv`, `landing_manifest.csv`). The pipeline never reads the sealed
tables. `tests/test_boundaries.py` enforces that.

## `raw`

| Table | Grain | Key columns |
|---|---|---|
| `pos_sales` | row per store-day-SKU per file | `business_date`, `store_code`, `sku`, `units`, `gross_sales`, `discount_amount`, `money_unit_as_filed` |
| `goods_received` | row per delivery line | `business_date`, `qty_ordered`, `qty_received`, `qty_rejected`, `unit_cost`, `expiry_date` |
| `waste_events` | row per scanned disposal | `event_ts_utc`, `event_ts_local`, `business_date`, `qty`, `reason_code_raw` |
| `stock_counts` | row per count | `business_date`, `counted_qty`, `count_type`, `unit_of_measure` |
| `markdown_events` | row per ticket | `business_date`, `markdown_pct`, `qty_ticketed`, `qty_honoured`, `qty_sold_at_markdown` |
| `promotions` | row per promo period | `sku`, `start_date`, `end_date`, `discount_pct`, `demand_uplift_factor` |
| `weather` | store-day | `weather_date`, `max_temp_c`, `rainfall_mm`, `is_heat_day` |
| `trading_calendar` | day | `trading_date`, `is_closed`, `holiday_name`, `footfall_index` |
| `item_master` | SKU | `sku`, `shelf_life_days`, `life_at_delivery_days`, `standard_cost`, `retail_price` |
| `store_master` | store | `store_code`, `store_format`, `size_index` |
| `supplier_master` | supplier | `supplier_code`, `delivery_weekdays`, `reliability` |

Every raw row also carries `_source_file`, `_file_checksum`, `_landing_date` and
`_ingested_at`, so a figure can be traced back to its file.

### `ops`

| Table | Holds |
|---|---|
| `ingest_files` | One row per landed file: checksum, rows loaded, `lag_days`, status (`loaded`, `duplicate` or `failed`), notes |
| `raw_quarantine` | Rows a loader refused, with the raw text and the reason |

## `intermediate`

### `int_stock_ledger`, grain store x item x trading day

The spine. Every loss number in the report is a term here.

| Column group | Columns |
|---|---|
| Dimensions | `store_code`, `sku`, `business_date`, `department_code`, `shelf_life_days`, `standard_cost`, `retail_price` |
| Movement in | `qty_ordered`, `qty_received`, `qty_rejected`, `received_cost_value`, `delivery_lines`, `min_life_at_delivery` |
| Movement out | `units_sold`, `net_sales_value`, `markdown_erosion_value`, `transaction_count` |
| Loss, scanned | `qty_waste_scanned`, `qty_prep_scanned`, `recorded_waste_cost`, `prep_loss_cost` |
| Markdown | `qty_ticketed`, `qty_honoured`, `qty_ticket_not_honoured` |
| Position | `counted_qty`, `was_counted`, `count_residue_qty` |
| Quality | `has_pos_data`, `is_pos_gap`, `had_mixed_money_units`, `had_voids`, `has_unmapped_reason` |

`qty_waste_scanned` is what was scanned, not what was thrown away. `is_pos_gap`
marks a store-day that traded with no feed. Exclude it from sell-through; never
read it as zero sales.

### `int_loss_reconciliation`, grain store x item x count-to-count segment

The model that decides how much loss never made it into a record.

| Column | Means |
|---|---|
| `counted_change_qty` | Stock change the physical count saw |
| `recorded_change_qty` | Stock change the records claim over the same segment |
| `unrecorded_shrink_qty` | The residue, `recorded - counted` |
| `unrecorded_shrink_cost` | The residue at standard cost |
| `shrink_confidence` | 0.25 to 1.00. How much to trust the residue, from count quality and line life |
| `residue_share_of_movement` | A residue that dominates the segment means a data problem, not a loss problem |

The residue is a bound and count error dominates it. Measured against the
simulator's records it runs several times the true unscanned loss. Quote the
confidence-weighted figure, never the raw residue.

### `int_fefo_depletion`, grain store x item x trading day

How shelf life burns down: `days_to_expiry`, `stock_on_hand_qty`,
`is_waste_risk_at_close`, `is_expired_at_close`, `sell_through_rate`.

### `int_store_portfolio`, grain store

Operational behaviour measured from the data rather than self-reported:
`scan_day_coverage`, `markdown_discipline_observed`, `markdown_honour_rate`.

## `marts`

| Table | Grain | Notes |
|---|---|---|
| `dim_date` | day | Carries `trading_context` and `days_before_christmas` |
| `dim_item` | SKU | `shelf_life_band`, `waste_risk_band`, `is_waste_risk` |
| `dim_store` | store | `has_thin_scan_records` |
| `fct_store_item_day` | store x item x day | Every loss term, plus `is_forecast_eligible` |

### `fct_store_item_day`

| Column | Means |
|---|---|
| `markdown_erosion_value` | Margin given away, from the realised POS discount rather than printed tickets |
| `recorded_waste_cost` | Scanned disposals at standard cost. A floor |
| `weighted_shrink_cost` | `unrecorded_shrink` multiplied by confidence. An estimate |
| `full_price_sales_value` | Budgeted sales, so erosion has a denominator |
| `realised_margin_value` | Margin actually earned after markdown |
| `is_forecast_eligible` | Short life, markdown-eligible and active. The population every consumer uses |
| `is_pos_gap` | The estate traded and no feed arrived. Exclude, do not zero |

## `reporting`

| Table | Grain | Answers |
|---|---|---|
| `mart_loss_performance` | store x department x month | Where the loss is |
| `mart_waste_league_table` | store x item | Which lines cost most, and why (`waste_pressure_reason`) |
| `mart_markdown_effectiveness` | ticket | Was the markdown worth its erosion? |
| `mart_data_quality` | store, plus one row per source | What could not be trusted, priced |

`waste_pressure_reason` takes one of: `Ordered too much`, `Short life at
delivery`, `Slow seller`, `Marked down too deep`, `Within tolerance`. The order
matches how actionable the fix is, so the first match is the one to act on.

## `analytics`

| Table | Grain | Notes |
|---|---|---|
| `forecast_backtest` | fold x model x quantile | WAPE, pinball, coverage |
| `forecast_metrics` | model | One row per model |
| `forecast_calibration` | nominal quantile | What q0.95 actually covers |
| `demand_forecast` | store x item x future day | `p50` to `p95` |
| `replenishment_plan` | store x item x future day | `service_level`, `baseline_order_qty`, `recommended_order_qty` |
| `markdown_plan` | store x item, latest | `optimal_markdown_pct`, `should_mark_down` |
| `waste_revenue_frontier` | order ratio | Waste cost against lost margin, with the optimum marked |
| `optimisation_impact` | component | The dollar claim, each row carrying its own `basis` |
| `impact_reconciliation` | check | Whether the two methods agree |

`optimisation_impact` rows already carry their sign, with savings positive and
costs negative, and must sum to `net_benefit`. A test enforces that, because an
earlier version subtracted two already-negative terms and the table did not
close.

`impact_reconciliation` is not decoration. On this estate
`methods_agree_on_direction` is 0: the frontier has an interior optimum while the
plan's own accounting nets negative. Both are published.

## Units and money

| | |
|---|---|
| Currency | AUD, GST-inclusive |
| GST | Fresh food is GST-free, so no GST appears in any loss figure |
| Waste, shrink | Valued at cost |
| Markdown | Valued at margin |
| Timezone | `Australia/Adelaide`, +9:30 ACST and +10:30 ACDT |
| Quantities | Eaches for countable lines, kilograms to 3 dp for weighed lines |
| Counts | Normalised to eaches; a pack count is multiplied by `pack_size` |
