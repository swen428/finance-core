PRAGMA foreign_keys = OFF;

-- Calculation Snapshot Persistence Schema v1
-- Records the content of a calculation run: inputs, rules, intermediate
-- values, and outputs for explainability and audit purposes.
--
-- This is the second layer of the Finance V3 Calculation Audit
-- architecture, sitting below calc_audit_runs.
--
-- Relationship:
--   calc_audit_runs (metadata: when, what type, which entity)
--       |
--   calculation_snapshots (content: inputs, rules, values, outputs)
--
-- This migration is additive on top of migrations 001-015.
-- No existing tables are altered.
--
-- Design properties:
-- - Never touches database/finance.db or live data.
-- - Stores calculation content as JSON text in snapshot_data.
-- - snapshot_id is TEXT PRIMARY KEY for deterministic caller-supplied
--   identifiers (not auto-generated).
-- - FOREIGN KEY run_id REFERENCES calc_audit_runs(run_id) ensures
--   every snapshot is linked to a valid calculation run.
-- - CHECK constraint on snapshot_type rejects invalid enum values
--   at the schema level.
-- - created_at is a required ISO 8601 timestamp for audit ordering.

CREATE TABLE IF NOT EXISTS calculation_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    snapshot_type TEXT NOT NULL
        CHECK (snapshot_type IN (
            'input_facts',
            'rules',
            'intermediate_values',
            'output_result'
        )),
    snapshot_data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES calc_audit_runs(run_id)
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- Lookup all snapshots for a calculation run.
CREATE INDEX IF NOT EXISTS idx_calculation_snapshots_run_id
    ON calculation_snapshots(run_id);

-- Filter snapshots by type within a run.
CREATE INDEX IF NOT EXISTS idx_calculation_snapshots_type
    ON calculation_snapshots(snapshot_type);

-- Composite index for common query: snapshots of a given type for a run.
CREATE INDEX IF NOT EXISTS idx_calculation_snapshots_run_type
    ON calculation_snapshots(run_id, snapshot_type);

PRAGMA foreign_keys = ON;

-- Validation guidance:
-- This migration defines the calculation snapshot persistence schema v1.
-- Validate schema changes against temporary/test SQLite databases,
-- not database/finance.db or live data.
-- Tests must not modify database/finance.db.
-- Do not seed or change live database data unless Wen explicitly
-- requests it.
