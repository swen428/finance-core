PRAGMA foreign_keys = OFF;

-- Reconciliation Review Queue + Resolution Persistence Schema v1
-- Persists review queue items, resolution decisions, and resolution
-- results into SQLite for audit workflow and future Metabase reporting.
--
-- This migration does not implement PDF parsing, OCR, Telegram integration,
-- Metabase dashboards, or AI-driven resolution. It provides the persistence
-- foundation for the review/resolution audit lifecycle.
--
-- All tables are additive on top of migrations 001-006. Existing tables are
-- not altered.

-- ---------------------------------------------------------------------------
-- reconciliation_review_queue
-- ---------------------------------------------------------------------------
-- Each row is a single reconciliation candidate that needs human review.
-- Rows are created by the persistence adapter after the deterministic
-- matching engine and review queue generator have run.

CREATE TABLE IF NOT EXISTS reconciliation_review_queue (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    run_public_id TEXT,
    candidate_id TEXT NOT NULL,
    issue_type TEXT NOT NULL,
    suggested_action TEXT NOT NULL,
    priority INTEGER NOT NULL,
    statement_transaction_ref TEXT,
    app_transaction_ref TEXT,
    confidence_score TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'resolved', 'ignored', 'needs_more_info')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- reconciliation_resolution_decisions
-- ---------------------------------------------------------------------------
-- Each row records a human reviewer's decision for a single review queue
-- item. A queue item may have multiple decisions over its lifetime (e.g.,
-- a decision was revised), but the latest decision is authoritative.

CREATE TABLE IF NOT EXISTS reconciliation_resolution_decisions (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    review_queue_public_id TEXT NOT NULL,
    decision_action TEXT NOT NULL,
    decision_note TEXT,
    reviewer TEXT NOT NULL,
    resolved_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (review_queue_public_id)
        REFERENCES reconciliation_review_queue(public_id)
);

-- ---------------------------------------------------------------------------
-- reconciliation_resolution_results
-- ---------------------------------------------------------------------------
-- Each row records the outcome of applying a resolution decision to a
-- review queue item through the ResolutionRuntime. A successful result
-- means the runtime validated and applied the decision; a failed result
-- means the decision was incompatible or otherwise rejected.

CREATE TABLE IF NOT EXISTS reconciliation_resolution_results (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    review_queue_public_id TEXT NOT NULL,
    decision_public_id TEXT NOT NULL,
    success INTEGER NOT NULL CHECK (success IN (0, 1)),
    error_message TEXT,
    audit_evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (review_queue_public_id)
        REFERENCES reconciliation_review_queue(public_id),
    FOREIGN KEY (decision_public_id)
        REFERENCES reconciliation_resolution_decisions(public_id)
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- Review queue: status is the most frequently filtered column.
CREATE INDEX IF NOT EXISTS idx_review_queue_status
    ON reconciliation_review_queue(status);

-- Review queue: filter by issue type for dashboards.
CREATE INDEX IF NOT EXISTS idx_review_queue_issue_type
    ON reconciliation_review_queue(issue_type);

-- Review queue: link to a reconciliation run.
CREATE INDEX IF NOT EXISTS idx_review_queue_run_public_id
    ON reconciliation_review_queue(run_public_id)
    WHERE run_public_id IS NOT NULL;

-- Decisions: link to review queue item.
CREATE INDEX IF NOT EXISTS idx_resolution_decisions_queue_public_id
    ON reconciliation_resolution_decisions(review_queue_public_id);

-- Results: link to review queue item.
CREATE INDEX IF NOT EXISTS idx_resolution_results_queue_public_id
    ON reconciliation_resolution_results(review_queue_public_id);

-- Results: link to the decision that produced this result.
CREATE INDEX IF NOT EXISTS idx_resolution_results_decision_public_id
    ON reconciliation_resolution_results(decision_public_id);

PRAGMA foreign_keys = ON;

-- Validation guidance:
-- This migration defines the review queue and resolution persistence
-- schema v1. Validate schema changes against temporary/test SQLite
-- databases, not database/finance.db or live data.
-- Tests must not modify database/finance.db.
-- Do not seed or change live database data unless Wen explicitly
-- requests it.
