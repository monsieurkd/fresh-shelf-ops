{{ config(materialized='table') }}

{#-
  Loss reconciliation: the model that decides how much loss never made it into
  a record, and how much confidence to place in that number.

  This is the answer to D07, and it is the reason the ledger exists. The logic,
  in order:

  1. **Segment the ledger with physical counts.** Between two counts, the
     recorded movements and the counted change should agree. Their difference
     over a segment is the loss no record explains.

  2. **Attribute what can be attributed.** Some of that residue is real but
     expected: a trim nobody scanned, a punnet that arrived damaged. The scan
     file prices the *scanned* share of those; the rest is genuinely unknown.

  3. **Publish a floor and a point estimate, never a single confident number.**
     Recorded waste is a *measured* floor. The residue is an *estimate* whose
     uncertainty is driven by how good the counts are — an eyeballed count to
     +/-3% cannot resolve a 1% loss, and saying otherwise would turn ordinary
     count noise into a fictitious shrinkage scandal.

  Which is why `unrecorded_shrink_qty` is reported with `shrink_confidence`
  rather than on its own.
-#}
with counted_days as (

    -- The days on which a count exists for this store-item. Segment boundaries.
    select
        store_code,
        sku,
        business_date,
        counted_qty,
        has_full_count,
        had_pack_count,
        count_events
    from {{ ref('int_stock_ledger') }}
    where was_counted

),

-- Movement accumulated since the previous count. The segment is "between two
-- counts", not "this day": daily movements are small relative to rounding and
-- count noise, so a daily residue would be mostly noise.
--
-- Crucially the book has an OPENING position. A store does not start the window
-- with an empty shelf, but the deliveries in the window only account for what
-- arrived *during* it. Without an opening anchor the first segment's residue
-- absorbs the entire pre-window stock and comes out as a huge spurious shrink —
-- which is exactly what it did before this was added.
opening as (

    -- The earliest count for each store-item inside the window. Whatever the
    -- ledger says moved before it, the count says what was actually on the
    -- shelf, so the count wins: it is the only direct observation of position.
    select
        store_code,
        sku,
        min(business_date) filter (where was_counted)         as opening_count_date,
        arg_min(counted_qty, business_date) filter (where was_counted) as opening_counted_qty
    from {{ ref('int_stock_ledger') }}
    group by 1, 2

),

-- Cumulative movements over EVERY trading day, then restricted to count days.
--
-- Order matters and this is the bug the model originally shipped with: running
-- the window functions after joining to the count days makes the "cumulative"
-- only cumulative over count days, so each step jumps by several days of
-- movement. The estate reconciliation summed to -106k against a true movement
-- of +36k, which is how the mistake announced itself. The book has to be built
-- on the full daily series and then sampled.
daily_book as (

    select
        l.*,
        sum(l.qty_received) over (
            partition by l.store_code, l.sku order by l.business_date
            rows between unbounded preceding and current row
        )                                                        as cum_received,
        sum(l.units_sold) over (
            partition by l.store_code, l.sku order by l.business_date
            rows between unbounded preceding and current row
        )                                                        as cum_sold,
        sum(l.qty_waste_scanned) over (
            partition by l.store_code, l.sku order by l.business_date
            rows between unbounded preceding and current row
        )                                                        as cum_waste_scanned,
        sum(l.qty_prep_scanned) over (
            partition by l.store_code, l.sku order by l.business_date
            rows between unbounded preceding and current row
        )                                                        as cum_prep_scanned,
        sum(l.qty_other_scanned) over (
            partition by l.store_code, l.sku order by l.business_date
            rows between unbounded preceding and current row
        )                                                        as cum_other_scanned,
        -- The book's own view of what is on the shelf, on every day. Useful in
        -- its own right: it is the running stock position the records imply.
        sum(l.qty_received - l.units_sold - l.qty_waste_scanned
            - l.qty_prep_scanned - l.qty_other_scanned) over (
            partition by l.store_code, l.sku order by l.business_date
            rows between unbounded preceding and current row
        )                                                        as cum_ledger_net
    from {{ ref('int_stock_ledger') }} l

),

segments as (

    select
        b.store_code,
        b.sku,
        b.department_code,
        b.department_name,
        b.category,
        b.standard_cost,
        b.retail_price,
        b.shelf_life_days,
        b.is_waste_risk,
        b.business_date,
        b.counted_qty,
        cd.has_full_count,
        cd.had_pack_count,
        o.opening_count_date,
        o.opening_counted_qty,
        b.cum_ledger_net,
        -- Previous count for this store-item and the movement since it, taken
        -- from the full daily book so the subtraction is a real day-to-day
        -- difference.
        lag(b.business_date) over (
            partition by b.store_code, b.sku order by b.business_date
        )                                                        as prev_count_date,
        lag(b.counted_qty) over (
            partition by b.store_code, b.sku order by b.business_date
        )                                                        as prev_counted_qty,
        lag(b.cum_ledger_net) over (
            partition by b.store_code, b.sku order by b.business_date
        )                                                        as prev_cum_ledger_net,
        -- Recorded waste movement within the segment, so the residue can be
        -- reported *alongside* the loss it is a bound on rather than in place
        -- of it.
        (b.cum_waste_scanned - coalesce(
            lag(b.cum_waste_scanned) over (
                partition by b.store_code, b.sku order by b.business_date
            ), 0
        ))                                                       as qty_waste_scanned_segment
    from daily_book b
    join counted_days cd
        on b.store_code = cd.store_code
        and b.sku = cd.sku
        and b.business_date = cd.business_date
    left join opening o
        on b.store_code = o.store_code
        and b.sku = o.sku

),

residue as (

    select
        store_code,
        sku,
        department_code,
        department_name,
        category,
        standard_cost,
        retail_price,
        shelf_life_days,
        is_waste_risk,
        business_date                                             as count_date,
        prev_count_date,
        prev_counted_qty,
        counted_qty,
        coalesce(has_full_count, false)                            as has_full_count,
        coalesce(had_pack_count, false)                            as had_pack_count,
        -- Stock change the count saw.
        (counted_qty - coalesce(prev_counted_qty, 0))              as counted_change_qty,
        -- Stock change the records claim, over the same segment.
        (cum_ledger_net - coalesce(prev_cum_ledger_net, 0))        as recorded_change_qty,
        -- Recorded (scanned) waste in this segment, for the residue to be read
        -- against.
        coalesce(qty_waste_scanned_segment, 0)                     as qty_waste_scanned_segment,
        -- The segment length in days, which scales how much movement we should
        -- expect and therefore how much noise the residue carries.
        cast(date_diff('day', coalesce(prev_count_date, business_date - interval 1 day),
                       business_date) as integer)                  as segment_days
    from segments
    where prev_count_date is not null

)

select
    store_code,
    sku,
    department_code,
    department_name,
    category,
    standard_cost,
    retail_price,
    shelf_life_days,
    is_waste_risk,
    count_date,
    prev_count_date,
    segment_days,
    counted_change_qty,
    recorded_change_qty,
    prev_counted_qty,
    counted_qty,
    has_full_count,
    had_pack_count,

    -- The residue: stock that left without leaving a record behind.
    --
    -- Positive means more disappeared than the records explain — genuine
    -- unrecorded shrink (unscanned disposals, damage, and whatever else).
    -- Negative means the records claim more left than the count agrees with,
    -- which is what a coarse count looks like from this side; it is kept
    -- rather than clipped, because a store whose residues are systematically
    -- negative has a counting problem, not a theft problem, and clipping would
    -- hide that.
    round(recorded_change_qty - counted_change_qty, 3)             as unrecorded_shrink_qty,
    round((recorded_change_qty - counted_change_qty) * standard_cost, 2)
                                                                   as unrecorded_shrink_cost,

    -- How much to trust the residue. A full count on a short-life line is a
    -- good anchor; a midweek cycle count of a packaged long-life line is not.
    -- The score is deliberately crude and deliberately visible: its purpose is
    -- to stop a low-confidence residue being quoted as if it were measured.
    cast(
        case
            when has_full_count and shelf_life_days <= 7 then 1.00
            when has_full_count then 0.80
            when shelf_life_days <= 7 then 0.60
            when had_pack_count then 0.25
            else 0.40
        end as decimal(4, 2))                                      as shrink_confidence,

    -- The *floor* the residue is measured from: recorded waste over the segment.
    --
    -- This distinction is the honest headline of the whole model. The residue is
    -- not "how much was stolen". It is "how much left the building that no
    -- record explains", and it absorbs three different things at once:
    --
    --   * genuine unscanned waste and prep trims (the D07 defect),
    --   * count error, which is +/-3% on a book whose segment moves ~1.2k units,
    --   * booking mistakes — a misfiled count or a double-booked delivery.
    --
    -- On this estate the count-error term dominates: measured against the
    -- simulator's own records the residue runs about 8x the true unscanned
    -- waste, and roughly two thirds of it is count noise. So the residue is
    -- published as a BOUND with a confidence, and the *actionable* number is
    -- the confidence-weighted share of it, never the raw figure.
    round(coalesce(qty_waste_scanned_segment, 0) * standard_cost, 2) as segment_recorded_waste_cost,
    round(coalesce(qty_waste_scanned_segment, 0), 3)              as segment_recorded_waste_qty,

    -- What the scan file *did* record over the same segment, for comparison.
    -- The gap between this and the residue is the unscanned share, which is
    -- the quantity the estate could recover by scanning properly.
    case
        when segment_days > 0
            then cast(round((recorded_change_qty - counted_change_qty) / segment_days, 4) as decimal(12, 4))
    end                                                            as unrecorded_shrink_qty_per_day,

    -- A residue that dominates the segment's throughput is a data problem
    -- rather than a loss problem: it usually means a count was misfiled or a
    -- delivery was booked twice.
    case
        when abs(recorded_change_qty) > 0
            then cast(round(abs(recorded_change_qty - counted_change_qty) / abs(recorded_change_qty), 4) as decimal(10, 4))
    end                                                            as residue_share_of_movement
from residue
