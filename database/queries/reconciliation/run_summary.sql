-- Reconciliation Run Summary v1
-- Metabase-ready query: one row per reconciliation run with status breakdowns.
--
-- Usage: connect to finance.db and run as a Metabase SQL question.
-- Optional: add a date filter on rr.started_at for time-range reporting.

SELECT
  rr.id AS run_id,
  rr.public_id AS run_public_id,
  rr.run_status,
  rr.matcher_version,
  rr.batch_id,
  rr.started_at,
  rr.completed_at,
  COUNT(mr.id) AS total_match_results,
  SUM(CASE WHEN mr.match_status = 'matched' THEN 1 ELSE 0 END) AS matched_count,
  SUM(CASE WHEN mr.match_status = 'no_match' THEN 1 ELSE 0 END) AS no_match_count,
  SUM(CASE WHEN mr.match_status = 'amount_mismatch' THEN 1 ELSE 0 END) AS amount_mismatch_count,
  SUM(CASE WHEN mr.match_status = 'currency_mismatch' THEN 1 ELSE 0 END) AS currency_mismatch_count,
  SUM(CASE WHEN mr.match_status = 'date_mismatch' THEN 1 ELSE 0 END) AS date_mismatch_count,
  SUM(CASE WHEN mr.match_status = 'merchant_mismatch' THEN 1 ELSE 0 END) AS merchant_mismatch_count,
  SUM(CASE WHEN mr.match_status = 'possible_duplicate' THEN 1 ELSE 0 END) AS possible_duplicate_count,
  SUM(CASE WHEN mr.match_status = 'ambiguous' THEN 1 ELSE 0 END) AS ambiguous_count,
  SUM(CASE WHEN mr.needs_review = 1 THEN 1 ELSE 0 END) AS needs_review_count,
  ROUND(
    CASE WHEN COUNT(mr.id) > 0
      THEN 100.0 * SUM(CASE WHEN mr.match_status = 'matched' THEN 1 ELSE 0 END) / COUNT(mr.id)
      ELSE 0
    END,
    1
  ) AS match_rate_pct
FROM reconciliation_runs rr
LEFT JOIN reconciliation_match_results mr ON mr.run_id = rr.id
GROUP BY rr.id
ORDER BY rr.id ASC;
