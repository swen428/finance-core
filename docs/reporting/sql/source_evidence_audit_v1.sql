-- source_evidence_audit_v1
-- Read-only source/attachment path audit trail.
-- Traces statement transactions back to their source import batch
-- and the attached source file path for evidence traceability.
SELECT
    st.public_id AS statement_public_id,
    st.transaction_date,
    st.merchant_raw,
    st.amount,
    st.currency,
    st.statement_row_reference,
    sib.public_id AS batch_public_id,
    sib.source_type,
    sib.source_file_path,
    sib.imported_at,
    CASE
        WHEN sib.source_file_path IS NOT NULL THEN 'source_file_available'
        ELSE 'source_file_missing'
    END AS source_evidence_status
FROM statement_transactions st
JOIN statement_import_batches sib ON sib.id = st.batch_id
ORDER BY st.id ASC
