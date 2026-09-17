-- Reconciliation Review Queue v1
-- Metabase-ready query: all match results flagged for human review.
--
-- Returns every row where the matcher could not reach a definitive MATCHED
-- outcome, including no_match, amount/currency/date/merchant mismatch,
-- ambiguous, possible duplicate, and explicit needs_review.
--
-- Optional: add a date filter on mr.created_at for time-range reporting.

SELECT
  mr.id,
  mr.public_id,
  mr.run_id,
  rr.public_id AS run_public_id,
  rr.run_status,
  mr.statement_transaction_id,
  st.public_id AS statement_public_id,
  st.merchant_raw,
  st.amount AS statement_amount,
  st.currency AS statement_currency,
  st.transaction_date AS statement_txn_date,
  st.posted_date AS statement_posted_date,
  mr.match_status,
  mr.internal_candidate_id,
  mr.amount_delta,
  mr.date_delta_days,
  mr.merchant_similarity,
  mr.reason_codes_json,
  mr.evidence_json,
  mr.needs_review,
  mr.created_at
FROM reconciliation_match_results mr
JOIN reconciliation_runs rr ON mr.run_id = rr.id
JOIN statement_transactions st ON mr.statement_transaction_id = st.id
WHERE mr.needs_review = 1
ORDER BY mr.run_id ASC, mr.id ASC;
