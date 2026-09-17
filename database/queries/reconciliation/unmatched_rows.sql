-- Reconciliation Unmatched Rows v1
-- Metabase-ready query: statement transactions that were NOT matched.
--
-- Returns every statement transaction in a run whose match_status is not
-- 'matched'.  This is the primary "what still needs attention" view for
-- reconciliation operators.
--
-- Optional: filter by run_id or date range.

SELECT
  st.id AS statement_transaction_id,
  st.public_id AS statement_public_id,
  st.merchant_raw,
  st.amount,
  st.currency,
  st.transaction_date,
  st.posted_date,
  st.batch_id,
  mr.id AS match_result_id,
  mr.public_id AS match_result_public_id,
  mr.match_status,
  mr.internal_candidate_id,
  mr.reason_codes_json,
  mr.amount_delta,
  mr.date_delta_days,
  mr.merchant_similarity,
  mr.needs_review,
  mr.run_id,
  rr.public_id AS run_public_id
FROM statement_transactions st
JOIN reconciliation_match_results mr ON st.id = mr.statement_transaction_id
JOIN reconciliation_runs rr ON mr.run_id = rr.id
WHERE mr.match_status != 'matched'
ORDER BY st.id ASC;
