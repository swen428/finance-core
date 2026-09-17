-- reconciliation_review_required_summary_v1
-- Read-only summary of review-required reconciliation items.
-- Excludes resolved/ignored items (same as the Python preview queue).
-- Groups by issue_type and priority for operator triage.
SELECT
    rq.issue_type,
    rq.priority,
    rq.suggested_action,
    COUNT(*) AS item_count,
    GROUP_CONCAT(rq.statement_transaction_ref, ', ') AS statement_refs,
    GROUP_CONCAT(rq.app_transaction_ref, ', ') AS app_refs
FROM reconciliation_review_queue rq
WHERE rq.status NOT IN ('resolved', 'ignored')
  AND rq.issue_type != 'matched'
GROUP BY rq.issue_type, rq.priority, rq.suggested_action
ORDER BY rq.priority ASC, item_count DESC
