PRAGMA foreign_keys = OFF;

-- Reconciliation Apply Results Persistence Schema v1
-- Persists resolution apply results from the ResolutionApplyRuntime into
-- a dedicated audit table. Each row records the outcome of applying a human
-- resolution decision: success/failure, idempotent flag, action-specific
-- payload, audit evidence, and statement/app references.
--
-- This migration is additive on top of migrations 001-007.
-- No existing tables are altered.
--
-- Design properties:
-- - Never touches database/finance.db or live data.
-- - Does NOT create, update, delete, merge, or overwrite final financial
--   transaction records.
-- - Proposal actions are persisted as proposals only; actual mutation
--   requires a guarded conversion step in a future version.
-- - Audit-only actions are persisted without mutating transactions.

CREATE TABLE IF NOT EXISTS reconciliation_apply_results (
    id INTEGER PRIMARY KEY,
    apply_id TEXT NOT NULL UNIQUE,
    decision_id TEXT NOT NULL,
    queue_item_id TEXT NOT NULL,
    candidate_id TEXT,
    action TEXT NOT NULL CHECK (
        action IN (
            'confirm_match',
            'adjust_app_transaction',
            'create_missing_app_transaction',
            'mark_statement_only',
            'mark_duplicate',
            'ignore',
            'needs_more_info'
        )
    ),
    success INTEGER NOT NULL CHECK (success IN (0, 1)),
    idempotent INTEGER NOT NULL DEFAULT 0 CHECK (idempotent IN (0, 1)),
    payload_json TEXT NOT NULL,
    audit_evidence_json TEXT NOT NULL,
    statement_reference_json TEXT,
    app_transaction_reference_json TEXT,
    reviewer TEXT,
    note TEXT,
    fingerprint TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- Unique apply_id (enforced by UNIQUE constraint on column above):
-- same apply_id with identical fingerprint is idempotent;
-- same apply_id with different fingerprint or payload is a conflict.

-- Unique decision_id: each resolution decision can have at most one
-- persisted apply result. Repeated identical saves are idempotent;
-- a changed payload or fingerprint for the same decision_id is a conflict.
CREATE UNIQUE INDEX IF NOT EXISTS idx_apply_results_decision_id
    ON reconciliation_apply_results(decision_id);

-- Lookup by queue_item_id for re-resolution / audit.
CREATE INDEX IF NOT EXISTS idx_apply_results_queue_item_id
    ON reconciliation_apply_results(queue_item_id);

-- Filter by action for reporting / Metabase readiness.
CREATE INDEX IF NOT EXISTS idx_apply_results_action
    ON reconciliation_apply_results(action);

-- Filter by applied_at range for time-based reporting.
CREATE INDEX IF NOT EXISTS idx_apply_results_applied_at
    ON reconciliation_apply_results(applied_at);

-- Lookup by fingerprint for idempotency guard.
CREATE INDEX IF NOT EXISTS idx_apply_results_fingerprint
    ON reconciliation_apply_results(fingerprint);

PRAGMA foreign_keys = ON;

-- Validation guidance:
-- This migration defines the reconciliation apply results persistence
-- schema v1. Validate schema changes against temporary/test SQLite
-- databases, not database/finance.db or live data.
-- Tests must not modify database/finance.db.
-- Do not seed or change live database data unless Wen explicitly
-- requests it.
