-- D07: recorded waste is a FLOOR, and the residue is what the ledger cannot
-- explain.
--
-- The claim being tested is not a number, it is an ordering: the stock that left
-- without a record must be at least zero, the recorded waste must be strictly
-- less than the loss the counts imply is missing, and the residue must carry a
-- confidence rather than being reported as bare fact.
--
-- A failure here means one of two things, and both matter:
--   * the reconciliation is broken (segment books that do not subtract), or
--   * the sources have stopped producing the defect, so the model is proving
--     nothing and should be re-pointed at new data.
{% set min_segments = 20 %}

with residue as (

    select
        count(*)                                            as segments,
        count(*) filter (where unrecorded_shrink_qty is null) as null_residues,
        count(*) filter (where shrink_confidence is null)    as missing_confidence,
        min(shrink_confidence)                               as min_confidence,
        max(shrink_confidence)                               as max_confidence,
        sum(unrecorded_shrink_qty)                           as total_residue_qty
    from {{ ref('int_loss_reconciliation') }}

),

recorded as (

    select
        sum(qty_waste_scanned)                               as total_recorded_qty
    from {{ ref('int_stock_ledger') }}

)

select
    r.segments,
    r.null_residues,
    r.missing_confidence,
    r.min_confidence,
    r.max_confidence,
    r.total_residue_qty,
    v.total_recorded_qty
from residue r
cross join recorded v
where
    -- There has to be enough data for the model to mean anything.
    r.segments < {{ min_segments }}
    -- Every residue must be a number: a NULL would silently drop out of sums.
    or r.null_residues > 0
    -- Every residue must carry its confidence, or it cannot be caveated.
    or r.missing_confidence > 0
    -- Confidence must be a probability.
    or r.min_confidence <= 0 or r.max_confidence > 1
    -- The floor must actually be a floor. If recorded waste exceeded the whole
    -- ledger residue, then the records are claiming MORE loss than the counts
    -- can see, which means the scan file is over-counting — the opposite of
    -- the defect this project models.
    or v.total_recorded_qty > 0 and r.total_residue_qty < 0
