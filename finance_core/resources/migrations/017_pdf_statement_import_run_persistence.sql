PRAGMA foreign_keys = OFF;

-- PDF Statement Import Run Persistence Schema v1
-- Stores metadata and summary snapshots for PDF statement import/review
-- runs.  This is an audit/review persistence layer only -- it does not
-- create final financial transactions, settlement obligations, or
-- mutation of final transaction tables.
--
-- Design properties:
-- - Never touches database/finance.db or live data.
-- - Stores import run metadata and summary snapshots only.
-- - run_public_id is TEXT NOT NULL UNIQUE for idempotency protection.
-- - import_batch_public_id references the import batch that provided
--   the input data, for traceability.
-- - source_mode records whether the run originated from fixture_text
--   (deterministic CI) or pdf_text (real PDF text extraction).
-- - source_pdf_path and source_statement_id preserve audit evidence
--   back to the originating PDF/statement.
-- - run_status CHECK constraint enforces only the three valid review
--   statuses determined by the review summary layer:
--   fully_ready, partially_reviewable, blocked.
-- - Row count columns use CHECK >= 0 guards.
-- - total_rows must equal ready_for_import_rows + needs_review_rows
--   + blocked_rows when the CHECK is applied (enforced at schema level).
-- - blocked_reason_counts_json and warning_reason_counts_json store
--   deterministic JSON text, consistent with existing project JSON
--   storage patterns (reason_codes_json, evidence_json, etc.).
-- - dashboard_summary_json and audit_summary_json capture the review
--   summary snapshots for reporting and traceability.
-- - created_at defaults to CURRENT_TIMESTAMP for audit ordering.
--
-- This migration is additive on top of migrations 001-016.
-- No existing tables are altered.

CREATE TABLE IF NOT EXISTS pdf_statement_import_runs (
    id INTEGER PRIMARY KEY,
    run_public_id TEXT NOT NULL UNIQUE,
    import_batch_public_id TEXT,
    source_mode TEXT NOT NULL
        CHECK (source_mode IN ('fixture_text', 'pdf_text')),
    source_pdf_path TEXT,
    source_statement_id TEXT,
    run_status TEXT NOT NULL
        CHECK (run_status IN (
            'fully_ready',
            'partially_reviewable',
            'blocked'
        )),
    total_rows INTEGER NOT NULL
        CHECK (total_rows >= 0),
    ready_for_import_rows INTEGER NOT NULL
        CHECK (ready_for_import_rows >= 0),
    needs_review_rows INTEGER NOT NULL
        CHECK (needs_review_rows >= 0),
    blocked_rows INTEGER NOT NULL
        CHECK (blocked_rows >= 0),
    blocked_reason_counts_json TEXT NOT NULL DEFAULT '{}',
    warning_reason_counts_json TEXT NOT NULL DEFAULT '{}',
    dashboard_summary_json TEXT,
    audit_summary_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (total_rows = ready_for_import_rows + needs_review_rows + blocked_rows)
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- Idempotency lookup by run_public_id (already covered by UNIQUE constraint,
-- but explicit index ensures the query planner prefers the covering index).
CREATE INDEX IF NOT EXISTS idx_pdf_statement_import_runs_public_id
    ON pdf_statement_import_runs(run_public_id);

-- Filter runs by import batch for cross-batch reporting.
CREATE INDEX IF NOT EXISTS idx_pdf_statement_import_runs_import_batch_public_id
    ON pdf_statement_import_runs(import_batch_public_id)
    WHERE import_batch_public_id IS NOT NULL;

-- Filter runs by status for dashboard/review queue views.
CREATE INDEX IF NOT EXISTS idx_pdf_statement_import_runs_run_status
    ON pdf_statement_import_runs(run_status);

-- Order runs by creation time for audit ordering.
CREATE INDEX IF NOT EXISTS idx_pdf_statement_import_runs_created_at
    ON pdf_statement_import_runs(created_at);

PRAGMA foreign_keys = ON;

-- Validation guidance:
-- This migration defines the PDF statement import run persistence schema v1.
-- Validate schema changes against temporary/test SQLite databases,
-- not database/finance.db or live data.
-- Tests must not modify database/finance.db.
-- Do not seed or change live database data unless Wen explicitly
-- requests it.
