-- Reconciliation Candidate Audit Evidence v1
-- Metabase-ready query: full audit trail for each match result.
--
-- Returns one row per match result with statement details, match outcome,
-- reason codes (parsed from JSON), and key evidence fields.  This is the
-- most detailed reconciliation audit view.
--
-- Note: reason_codes_json is stored as a JSON array.  Metabase can display
-- it as-is, or the JSON can be unpacked with json_each() if needed.

SELECT
  mr.id AS match_result_id,
  mr.public_id AS match_public_id,
  mr.run_id,
  rr.public_id AS run_public_id,
  rr.run_status,
  rr.matcher_version,
  rr.started_at,
  rr.completed_at,
  mr.statement_transaction_id,
  st.public_id AS statement_public_id,
  st.merchant_raw,
  st.amount AS statement_amount,
  st.currency AS statement_currency,
  st.transaction_date AS statement_txn_date,
  st.posted_date AS statement_posted_date,
  mr.match_status,
  mr.internal_candidate_id,
  mr.reason_codes_json,
  mr.evidence_json,
  mr.amount_delta,
  mr.date_delta_days,
  mr.merchant_similarity,
  mr.needs_review,
  mr.created_at
FROM reconciliation_match_results mr
JOIN reconciliation_runs rr ON mr.run_id = rr.id
JOIN statement_transactions st ON mr.statement_transaction_id = st.id
ORDER BY mr.run_id ASC, mr.id ASC;
