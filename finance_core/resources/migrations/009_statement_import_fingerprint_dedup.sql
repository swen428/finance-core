PRAGMA foreign_keys = OFF;

-- Statement Import Fingerprint Dedup v1
-- Adds row_fingerprint column and a batch-level uniqueness index
-- to the statement_transactions table so repeated imports of the
-- same statement rows are deterministic and idempotent.
--
-- Design:
--   row_fingerprint:  SHA-256 over the raw CSV row's canonical columns,
--                     computed by statement_csv._csv_row_fingerprint().
--                     Format: csv-<sha256[:16]>
--   Unique index:     (batch_id, row_fingerprint) WHERE row_fingerprint IS NOT NULL
--                     This prevents the same raw CSV row content from being
--                     inserted twice within a single import batch.
--                     Cross-batch dedup (same file imported again) is handled
--                     by the existing public_id UNIQUE constraint, which
--                     includes source_file_hash in its derivation.
--
--   NULL fingerprints are allowed: non-CSV adapters (future PDF/OCR) may
--   not compute fingerprints.  The public_id UNIQUE constraint remains the
--   primary cross-batch dedup guard for all sources.
--
-- This migration is additive on top of migration 006.
-- No existing columns, indexes, or data are altered or dropped.

ALTER TABLE statement_transactions
ADD COLUMN row_fingerprint TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_stmt_txns_batch_fingerprint
  ON statement_transactions(batch_id, row_fingerprint)
  WHERE row_fingerprint IS NOT NULL;

PRAGMA foreign_keys = ON;
