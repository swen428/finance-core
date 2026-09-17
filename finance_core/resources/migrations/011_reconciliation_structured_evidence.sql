PRAGMA foreign_keys = OFF;

-- Reconciliation Structured Evidence Persistence Schema v1
-- Persists structured reconciliation evidence records for audit,
-- review-queue explainability, and future Metabase reporting.
--
-- This migration is additive on top of migrations 001-010.
-- No existing tables are altered.
--
-- Design properties:
-- - Never touches database/finance.db or live data.
-- - Does NOT create, update, delete, merge, or overwrite final financial
--   transaction records.
-- - TEXT public_id references (not integer foreign keys) for related tables,
--   following the pattern established in migrations 007 and 008.
-- - No restrictive CHECK constraint on evidence_type or source_type, since
--   the set of types is expected to expand as more reconciliation layers
--   come online.
-- - confidence_score stored as TEXT (Decimal-safe), following the pattern
--   in reconciliation_review_queue.confidence_score (migration 007).
-- - evidence_payload stored as TEXT (JSON) for structured payload bodies.

CREATE TABLE IF NOT EXISTS reconciliation_structured_evidence (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    review_queue_public_id TEXT,
    statement_transaction_id TEXT,
    app_transaction_id TEXT,
    evidence_type TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT,
    source_path TEXT,
    source_page INTEGER,
    source_row INTEGER,
    source_field TEXT,
    confidence_score TEXT NOT NULL,
    evidence_payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- Link evidence to a review queue item for explainability UIs.
CREATE INDEX IF NOT EXISTS idx_structured_evidence_review_queue
    ON reconciliation_structured_evidence(review_queue_public_id)
    WHERE review_queue_public_id IS NOT NULL;

-- Link evidence to a statement transaction.
CREATE INDEX IF NOT EXISTS idx_structured_evidence_statement_txn
    ON reconciliation_structured_evidence(statement_transaction_id)
    WHERE statement_transaction_id IS NOT NULL;

-- Link evidence to an app transaction.
CREATE INDEX IF NOT EXISTS idx_structured_evidence_app_txn
    ON reconciliation_structured_evidence(app_transaction_id)
    WHERE app_transaction_id IS NOT NULL;

-- Filter by evidence type for dashboards and reporting.
CREATE INDEX IF NOT EXISTS idx_structured_evidence_type
    ON reconciliation_structured_evidence(evidence_type);

-- Filter by source type for dashboards and reporting.
CREATE INDEX IF NOT EXISTS idx_structured_evidence_source_type
    ON reconciliation_structured_evidence(source_type);

PRAGMA foreign_keys = ON;

-- Validation guidance:
-- This migration defines the reconciliation structured evidence persistence
-- schema v1. Validate schema changes against temporary/test SQLite
-- databases, not database/finance.db or live data.
-- Tests must not modify database/finance.db.
-- Do not seed or change live database data unless Wen explicitly
-- requests it.
