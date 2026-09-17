PRAGMA foreign_keys = OFF;

-- Reconciliation Guarded Apply Execution Persistence Schema v1
-- Persists guarded apply execution results and per-operation outcomes
-- into SQLite for durable idempotency and audit evidence across process
-- restarts.
--
-- This migration is additive on top of migrations 001-013.
-- No existing tables are altered.
--
-- Design properties:
-- - Never touches database/finance.db or live data.
-- - Stores execution results only -- does NOT create, update, or delete
--   final financial records.
-- - idempotency_key is UNIQUE to prevent conflicting replays of the same
--   key with different plan content.
-- - execution_fingerprint is stored alongside each execution so the
--   repository can detect fingerprint mismatches without recomputing.
-- - JSON fields (guard_decision_refs_json, audit_trail_json,
--   guard_blocked_reasons_json, mutation_payload_json) use deterministic
--   ordering (sort_keys=True).
-- - CHECK constraints enforce execution_status validity, dry-run flag,
--   non-negative operation counts, and count consistency.
-- - FK from operation_results to executions ensures referential integrity
--   when foreign keys are enabled.

CREATE TABLE IF NOT EXISTS reconciliation_guarded_apply_executions (
    execution_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    execution_fingerprint TEXT NOT NULL,
    execution_status TEXT NOT NULL
        CHECK (execution_status IN (
            'executed',
            'blocked',
            'partially_blocked',
            'unsupported',
            'conflict'
        )),
    total_operations INTEGER NOT NULL CHECK (total_operations >= 0),
    operations_executed INTEGER NOT NULL CHECK (operations_executed >= 0),
    operations_blocked INTEGER NOT NULL CHECK (operations_blocked >= 0),
    operations_skipped INTEGER NOT NULL CHECK (operations_skipped >= 0),
    block_reason TEXT NOT NULL DEFAULT '',
    guard_decision_refs_json TEXT NOT NULL,
    audit_trail_json TEXT NOT NULL,
    is_dry_run INTEGER NOT NULL CHECK (is_dry_run IN (0, 1)) DEFAULT 1,
    executed_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (operations_executed + operations_blocked + operations_skipped = total_operations)
);

CREATE TABLE IF NOT EXISTS reconciliation_guarded_apply_operation_results (
    operation_result_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    execution_status TEXT NOT NULL
        CHECK (execution_status IN (
            'executed',
            'blocked',
            'partially_blocked',
            'unsupported',
            'conflict'
        )),
    reason TEXT NOT NULL DEFAULT '',
    guard_decision_approved INTEGER NOT NULL CHECK (guard_decision_approved IN (0, 1)),
    guard_decision_idempotency_key TEXT,
    guard_blocked_reasons_json TEXT NOT NULL,
    mutation_type TEXT NOT NULL,
    mutation_payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (execution_id)
        REFERENCES reconciliation_guarded_apply_executions(execution_id),
    UNIQUE (execution_id, operation_id)
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- Lookup executions by plan_id.
CREATE INDEX IF NOT EXISTS idx_recon_guarded_apply_exec_plan_id
    ON reconciliation_guarded_apply_executions(plan_id);

-- Filter by execution_status for audit queries.
CREATE INDEX IF NOT EXISTS idx_recon_guarded_apply_exec_status
    ON reconciliation_guarded_apply_executions(execution_status);

-- Order by executed_at for timeline views.
CREATE INDEX IF NOT EXISTS idx_recon_guarded_apply_exec_executed_at
    ON reconciliation_guarded_apply_executions(executed_at);

-- Lookup operation results by execution_id.
CREATE INDEX IF NOT EXISTS idx_recon_guarded_apply_op_execution_id
    ON reconciliation_guarded_apply_operation_results(execution_id);

-- Lookup by operation_id for cross-reference.
CREATE INDEX IF NOT EXISTS idx_recon_guarded_apply_op_operation_id
    ON reconciliation_guarded_apply_operation_results(operation_id);

-- Filter operation results by status.
CREATE INDEX IF NOT EXISTS idx_recon_guarded_apply_op_status
    ON reconciliation_guarded_apply_operation_results(execution_status);

PRAGMA foreign_keys = ON;

-- Validation guidance:
-- This migration defines the reconciliation guarded apply execution
-- persistence schema v1. Validate schema changes against temporary/test
-- SQLite databases, not database/finance.db or live data.
-- Tests must not modify database/finance.db.
-- Do not seed or change live database data unless Wen explicitly
-- requests it.
