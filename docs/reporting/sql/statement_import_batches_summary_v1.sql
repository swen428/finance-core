-- statement_import_batches_summary_v1
-- Read-only summary of statement import batches.
-- Each row = one import batch with transaction counts.
SELECT
    sib.public_id AS batch_public_id,
    sib.source_type,
    sib.account_name,
    sib.currency,
    sib.source_file_path,
    sib.imported_at,
    COUNT(st.id) AS transaction_count,
    SUM(st.amount) AS total_amount,
    MIN(st.transaction_date) AS earliest_transaction_date,
    MAX(st.transaction_date) AS latest_transaction_date
FROM statement_import_batches sib
LEFT JOIN statement_transactions st ON st.batch_id = sib.id
GROUP BY sib.id
ORDER BY sib.imported_at DESC
