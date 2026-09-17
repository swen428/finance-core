-- Reconciliation Reporting Views v1
-- Read-only reporting layer that classifies the reconciliation state of every
-- persisted statement row into a single reporting bucket, and joins in the
-- downstream review, guard, apply, and execution layers where they exist.
--
-- This is a Metabase-ready query file. It is consumed directly (executed as a
-- SELECT by tests and by the future Metabase bridge) -- no CREATE VIEW, no
-- migration, no table mutation. The project deliberately uses plain read-only
-- SELECT files rather than SQL views, so this file follows that convention.
--
-- Read-only safety boundary:
--   - No INSERT / UPDATE / DELETE anywhere in this file.
--   - Only SELECTs against existing persisted reconciliation tables.
--   - Never touches database/finance.db or live data.
--
-- Reporting buckets (one row per statement match result unless the row is
-- promoted into an apply-driven bucket):
--   reconciled               - matcher reached a definitive matched outcome
--   unmatched_statement      - statement row had no matching candidate (no_match)
--   amount_mismatch          - amount / currency / date / merchant mismatch
--   needs_review             - matcher flagged needs_review, OR review queue is
--                              still pending for this row
--   blocked                  - a guard decision blocked final mutation, OR a
--                              guarded apply execution was fully blocked
--   apply_partially_blocked - a guarded apply execution was partially blocked
--   apply_failed             - apply result success=0, OR guard execution in a
--                              conflict / unsupported state, OR apply batch in
--                              'failed' state for this review item
--   pending_apply            - review queue item resolved/ignored but no apply
--                              result recorded yet (approved-not-applied), where
--                              no blocking / failed signal exists
--
-- Determinism:
--   ORDER BY run_id, statement_transaction_id, bucket -- stable across runs.
--   JSON fields are emitted as stored; deterministic ordering is the job of the
--   persistence layer (sort_keys=True), not this query.
--
-- Notes on what is deliberately NOT conflated:
--   - blocked and apply_failed are reported as distinct buckets; a blocked
--     guard/safe-execution result is not hidden behind "unmatched".
--   - needs_review is kept distinct from amount_mismatch where the matcher set
--     needs_review=1 with a mismatch status (the most-specific bucket wins per
--     the precedence below).
--   - pending_apply (approved-not-applied) is only assigned when no blocking or
--     failed signal is present, so an unresolved-but-blocked item is never
--     labelled "pending_apply".
--
-- See docs/design/reconciliation_reporting_views_v1.md for the full design.

WITH
-- Latest review queue status per statement transaction, joined through the
-- review queue's loose TEXT statement_transaction_ref. The matcher layer keys
-- match results by integer statement_transaction_id; the review queue stores a
-- humane TEXT ref (statement_row_reference or merchant_raw). We join the review
-- queue back to statement rows by that TEXT ref against statement_transactions.
latest_review AS (
    SELECT
        rq.id AS review_queue_id,
        rq.public_id AS review_queue_public_id,
        rq.run_public_id,
        rq.candidate_id,
        rq.statement_transaction_ref,
        rq.app_transaction_ref,
        rq.issue_type,
        rq.status AS review_status,
        rq.priority,
        rq.created_at AS review_created_at,
        ROW_NUMBER() OVER (
            PARTITION BY rq.public_id
            ORDER BY rq.updated_at DESC, rq.id DESC
        ) AS rn
    FROM reconciliation_review_queue rq
),
review_per_statement AS (
    SELECT
        st.id AS statement_transaction_id,
        lr.review_queue_public_id,
        lr.review_status,
        lr.issue_type,
        lr.app_transaction_ref
    FROM latest_review lr
    JOIN statement_transactions st
      ON lr.statement_transaction_ref IS NOT NULL
     AND lr.statement_transaction_ref = COALESCE(
            st.statement_row_reference,
            st.merchant_raw
        )
    WHERE lr.rn = 1
),
-- Latest apply result per review queue item (migration 008 keys by decision_id,
-- which is itself keyed by review_queue_public_id via resolution_decisions).
decision_per_queue AS (
    SELECT
        d.review_queue_public_id,
        d.public_id AS decision_public_id,
        d.decision_action,
        d.resolved_at,
        ROW_NUMBER() OVER (
            PARTITION BY d.review_queue_public_id
            ORDER BY d.id DESC
        ) AS rn
    FROM reconciliation_resolution_decisions d
),
latest_apply AS (
    SELECT
        dpq.review_queue_public_id,
        dpq.decision_public_id,
        dpq.decision_action,
        ar.id AS apply_result_id,
        ar.apply_id,
        ar.success AS apply_success,
        ar.idempotent AS apply_idempotent,
        ar.action AS apply_action,
        ar.applied_at
    FROM decision_per_queue dpq
    LEFT JOIN reconciliation_apply_results ar
      ON ar.decision_id = dpq.decision_public_id
    WHERE dpq.rn = 1
),
-- Latest guarded apply execution outcome per decision (migration 014 operation
-- results are keyed by decision_id). Aggregate each decision's operation
-- outcomes into one combined execution signal.
exec_per_decision AS (
    SELECT
        op.decision_id,
        MIN(op.execution_status) AS exec_min_status,
        CASE
            WHEN SUM(CASE WHEN op.execution_status = 'blocked' THEN 1 ELSE 0 END) > 0
                AND SUM(CASE WHEN op.execution_status = 'executed' THEN 1 ELSE 0 END) = 0
                THEN 'blocked'
            WHEN SUM(CASE WHEN op.execution_status = 'blocked' THEN 1 ELSE 0 END) > 0
                AND SUM(CASE WHEN op.execution_status = 'executed' THEN 1 ELSE 0 END) > 0
                THEN 'partially_blocked'
            WHEN SUM(CASE WHEN op.execution_status = 'conflict' THEN 1 ELSE 0 END) > 0
                THEN 'conflict'
            WHEN SUM(CASE WHEN op.execution_status = 'unsupported' THEN 1 ELSE 0 END) > 0
                THEN 'unsupported'
            WHEN SUM(CASE WHEN op.execution_status = 'executed' THEN 1 ELSE 0 END) > 0
                THEN 'executed'
            ELSE 'none'
        END AS exec_combined_status,
        COUNT(op.operation_result_id) AS exec_op_count
    FROM reconciliation_guarded_apply_operation_results op
    GROUP BY op.decision_id
),
-- Latest final-mutation guard decision keyed by source statement text ref
-- (migration 013). Blocked guard decisions (approved=0) are surfaced here.
guard_per_statement AS (
    SELECT
        g.id AS guard_id,
        g.proposal_id,
        g.action AS guard_action,
        g.approved AS guard_approved,
        g.source_statement_ref,
        g.blocked_reasons_json,
        g.evidence_refs_json,
        ROW_NUMBER() OVER (
            PARTITION BY g.source_statement_ref
            ORDER BY g.created_at DESC, g.id DESC
        ) AS rn
    FROM reconciliation_final_mutation_guard_decisions g
    WHERE g.source_statement_ref IS NOT NULL
)
-- The reconciliation_apply_batches table (migration 012) keys apply batch
-- lifecycle state by an opaque `batch_id` TEXT. There is no persisted foreign
-- key from a batch back to a specific review queue item or statement row, so a
-- failed apply batch cannot be reliably attributed to an individual row in v1.
-- We therefore do NOT derive a per-row bucket from apply_batches.state and any
-- persist a row as `apply_failed` only when an apply result (migration 008) or
-- guarded execution outcome (migration 014) provides a per-decision signal.
-- This limitation is documented in the design doc.

SELECT
    mr.id AS match_result_id,
    mr.public_id AS match_public_id,
    mr.run_id,
    rr.public_id AS run_public_id,
    rr.run_status,
    mr.statement_transaction_id,
    st.public_id AS statement_public_id,
    st.merchant_raw,
    st.amount AS statement_amount,
    st.currency AS statement_currency,
    st.transaction_date AS statement_txn_date,
    mr.match_status,
    mr.internal_candidate_id,
    mr.needs_review,
    mr.reason_codes_json,
    mr.evidence_json,
    mr.amount_delta,
    mr.date_delta_days,
    mr.merchant_similarity,
    rps.review_queue_public_id,
    rps.review_status,
    rps.issue_type AS review_issue_type,
    rps.app_transaction_ref AS review_app_transaction_ref,
    la.decision_public_id,
    la.decision_action,
    la.apply_result_id,
    la.apply_id,
    la.apply_success,
    la.apply_idempotent,
    la.apply_action,
    la.applied_at,
    epd.exec_combined_status,
    epd.exec_op_count,
    gps.guard_action,
    gps.guard_approved,
    gps.blocked_reasons_json,
    gps.evidence_refs_json AS guard_evidence_refs_json,
    CASE
        -- Apply-driven buckets take precedence over matcher-only buckets so
        -- blocked / failed items are never hidden behind "needs_review" or
        -- "unmatched". Each bucket is a distinct reporting state.
        WHEN la.apply_success = 0 THEN 'apply_failed'
        WHEN epd.exec_combined_status = 'blocked' THEN 'blocked'
        WHEN epd.exec_combined_status = 'partially_blocked' THEN 'apply_partially_blocked'
        WHEN epd.exec_combined_status IN ('conflict', 'unsupported') THEN 'apply_failed'
        WHEN gps.guard_approved = 0 THEN 'blocked'
        WHEN epd.exec_combined_status = 'executed' AND la.apply_success = 1 THEN 'reconciled'
        WHEN epd.exec_combined_status = 'executed' THEN 'reconciled'
        WHEN la.apply_result_id IS NOT NULL AND la.apply_success = 1
            AND COALESCE(epd.exec_combined_status, 'none') = 'none' THEN 'pending_apply'
        WHEN rps.review_queue_public_id IS NOT NULL
            AND rps.review_status IN ('resolved', 'ignored')
            AND la.apply_result_id IS NULL
            AND COALESCE(epd.exec_combined_status, 'none') = 'none'
            AND gps.guard_approved IS NULL THEN 'pending_apply'
        WHEN rps.review_status = 'needs_more_info' THEN 'needs_review'
        WHEN rps.review_status = 'pending' THEN 'needs_review'
        WHEN mr.needs_review = 1 AND mr.match_status = 'matched' THEN 'needs_review'
        WHEN mr.match_status = 'matched' THEN 'reconciled'
        WHEN mr.match_status = 'no_match' THEN 'unmatched_statement'
        WHEN mr.match_status IN (
            'amount_mismatch', 'currency_mismatch', 'date_mismatch',
            'merchant_mismatch', 'possible_duplicate', 'ambiguous', 'needs_review'
        ) THEN 'amount_mismatch'
        ELSE 'needs_review'
    END AS reporting_bucket,
    CASE
        WHEN la.apply_success = 0 THEN 'apply result recorded as failed'
        WHEN epd.exec_combined_status = 'blocked' THEN 'guarded apply execution fully blocked'
        WHEN epd.exec_combined_status = 'partially_blocked'
            THEN 'guarded apply execution partially blocked'
        WHEN epd.exec_combined_status = 'conflict'
            THEN 'guarded apply execution idempotency conflict'
        WHEN epd.exec_combined_status = 'unsupported'
            THEN 'guarded apply execution unsupported action'
        WHEN gps.guard_approved = 0 THEN 'final mutation guard blocked'
        WHEN epd.exec_combined_status = 'executed' AND la.apply_success = 1
            THEN 'matched and applied'
        WHEN la.apply_result_id IS NOT NULL AND la.apply_success = 1
            AND COALESCE(epd.exec_combined_status, 'none') = 'none'
            THEN 'apply result recorded, no guarded execution recorded'
        WHEN rps.review_queue_public_id IS NOT NULL
            AND rps.review_status IN ('resolved', 'ignored')
            AND la.apply_result_id IS NULL
            AND COALESCE(epd.exec_combined_status, 'none') = 'none'
            AND gps.guard_approved IS NULL
            THEN 'resolved but not yet applied'
        WHEN rps.review_status = 'needs_more_info' THEN 'review needs more info'
        WHEN rps.review_status = 'pending' THEN 'review pending'
        WHEN mr.needs_review = 1 AND mr.match_status = 'matched'
            THEN 'matched but flagged needs_review'
        WHEN mr.match_status = 'matched' THEN 'matched by matcher'
        WHEN mr.match_status = 'no_match' THEN 'no matching candidate'
        WHEN mr.match_status IN (
            'amount_mismatch', 'currency_mismatch', 'date_mismatch',
            'merchant_mismatch', 'possible_duplicate', 'ambiguous', 'needs_review'
        ) THEN mr.match_status
        ELSE 'uncategorised'
    END AS reporting_reason,
    mr.created_at
FROM reconciliation_match_results mr
JOIN reconciliation_runs rr ON mr.run_id = rr.id
JOIN statement_transactions st ON mr.statement_transaction_id = st.id
LEFT JOIN review_per_statement rps ON rps.statement_transaction_id = st.id
LEFT JOIN latest_apply la
    ON la.review_queue_public_id = rps.review_queue_public_id
LEFT JOIN exec_per_decision epd
    ON epd.decision_id = la.decision_public_id
LEFT JOIN guard_per_statement gps
    ON gps.rn = 1
   AND gps.source_statement_ref = COALESCE(
        st.statement_row_reference, st.merchant_raw
    )
ORDER BY mr.run_id ASC, mr.statement_transaction_id ASC, reporting_bucket ASC;