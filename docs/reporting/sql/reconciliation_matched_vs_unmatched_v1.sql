-- reconciliation_matched_vs_unmatched_v1
-- Read-only matched vs unmatched reconciliation summary.
-- One row per batch with match/unmatch/review-required counts.
-- Suitable for a bar-chart or summary-table dashboard card.
SELECT
    sib.public_id AS batch_public_id,
    sib.source_type,
    sib.account_name,
    sib.currency,
    sib.source_file_path,
    COUNT(rq.id) AS total_review_items,
    SUM(CASE WHEN rq.issue_type = 'matched' THEN 1 ELSE 0 END) AS matched_count,
    SUM(CASE WHEN rq.issue_type != 'matched' THEN 1 ELSE 0 END) AS unmatched_count,
    SUM(CASE WHEN rq.priority = 0 THEN 1 ELSE 0 END) AS high_priority_count,
    SUM(CASE WHEN rq.priority = 1 THEN 1 ELSE 0 END) AS medium_priority_count,
    SUM(CASE WHEN rq.priority = 99 THEN 1 ELSE 0 END) AS low_priority_count
FROM statement_import_batches sib
JOIN statement_transactions st ON st.batch_id = sib.id
JOIN reconciliation_review_queue rq ON rq.statement_transaction_ref = st.public_id
WHERE rq.status NOT IN ('resolved', 'ignored')
GROUP BY sib.id
ORDER BY sib.imported_at DESC
