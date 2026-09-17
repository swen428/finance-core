-- Reconciliation Status Breakdown v1
-- Metabase-ready query: match_status distribution across runs.
--
-- Returns one row per (run_id, match_status) pair with count and percentage
-- of total match results for that run.  Useful for stacked bar charts or
-- status distribution tables in Metabase.

SELECT
  rr.id AS run_id,
  rr.public_id AS run_public_id,
  rr.run_status,
  rr.started_at,
  mr.match_status,
  COUNT(*) AS status_count,
  ROUND(
    100.0 * COUNT(*) / (
      SELECT COUNT(*)
      FROM reconciliation_match_results mr2
      WHERE mr2.run_id = rr.id
    ),
    1
  ) AS status_pct
FROM reconciliation_runs rr
JOIN reconciliation_match_results mr ON mr.run_id = rr.id
GROUP BY rr.id, mr.match_status
ORDER BY rr.id ASC, status_count DESC;
