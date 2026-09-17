-- statement_transactions_recent_v1
-- Read-only listing of the most recent statement transactions.
-- Ordered by transaction date descending for operator scanning.
SELECT
    st.public_id,
    st.transaction_date,
    st.merchant_raw,
    st.amount,
    st.currency,
    st.account_name,
    st.statement_row_reference,
    sib.public_id AS batch_public_id,
    sib.source_type,
    sib.source_file_path
FROM statement_transactions st
JOIN statement_import_batches sib ON sib.id = st.batch_id
ORDER BY st.transaction_date DESC, st.id ASC
