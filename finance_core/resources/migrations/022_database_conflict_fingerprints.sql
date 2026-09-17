PRAGMA foreign_keys = ON;

-- Database Conflict Fingerprints v1.  Nullable additions preserve historical
-- rows; all new authoritative writers populate the versioned columns.
ALTER TABLE raw_intake_records ADD COLUMN content_fingerprint TEXT;
ALTER TABLE raw_intake_records ADD COLUMN fingerprint_version TEXT;
CREATE INDEX idx_raw_intake_records_content_fingerprint
  ON raw_intake_records(content_fingerprint)
  WHERE content_fingerprint IS NOT NULL;

ALTER TABLE pdf_statement_import_runs ADD COLUMN content_fingerprint TEXT;
ALTER TABLE pdf_statement_import_runs ADD COLUMN fingerprint_version TEXT;

-- run_public_id is the logical key.  The existing UNIQUE constraint remains
-- authoritative; these columns let the service distinguish replay/conflict.
CREATE INDEX idx_pdf_statement_import_runs_content_fingerprint
  ON pdf_statement_import_runs(content_fingerprint)
  WHERE content_fingerprint IS NOT NULL;
