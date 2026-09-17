PRAGMA foreign_keys = OFF;

-- Calculation Run Persistence Schema v1
-- Creates a durable record for every deterministic calculation run.
-- This is the first persistence layer for the Finance V3 Calculation
-- Audit architecture.  It records run metadata only -- it does NOT
-- implement settlement or receipt finalization runtime.
--
-- This migration is additive on top of migrations 001-014.
-- No existing tables are altered.
--
-- Design properties:
-- - Never touches database/finance.db or live data.
-- - Stores calculation run metadata only.
-- - run_id is TEXT PRIMARY KEY for deterministic caller-supplied
--   identifiers (not auto-generated).
-- - run_type, entity_type, and status each have explicit CHECK
--   constraints to reject invalid enum values at the schema level.
-- - source_type and source_reference are optional metadata fields
--   for traceability back to the originating system or input.
-- - rule_version captures which business-rule version was applied.
-- - created_at is a required ISO 8601 timestamp for audit ordering.

CREATE TABLE IF NOT EXISTS calc_audit_runs (
    run_id TEXT PRIMARY KEY,
    run_type TEXT NOT NULL
        CHECK (run_type IN (
            'receipt_split',
            'settlement',
            'reconciliation',
            'parser_proposal',
            'confirmation',
            'finalization',
            'audit_snapshot'
        )),
    entity_type TEXT NOT NULL
        CHECK (entity_type IN (
            'receipt_group',
            'receipt',
            'statement',
            'reconciliation_batch',
            'reconciliation_review',
            'parser_output',
            'calculation_case'
        )),
    entity_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN (
            'pending',
            'calculated',
            'confirmed',
            'finalized',
            'failed',
            'superseded',
            'pending_confirmation'
        )),
    source_type TEXT,
    source_reference TEXT,
    created_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- Lookup runs by entity for audit trail.
CREATE INDEX IF NOT EXISTS idx_calc_audit_runs_entity
    ON calc_audit_runs(entity_type, entity_id);

-- Filter by status for lifecycle queries.
CREATE INDEX IF NOT EXISTS idx_calc_audit_runs_status
    ON calc_audit_runs(status);

-- Order by created_at for timeline views.
CREATE INDEX IF NOT EXISTS idx_calc_audit_runs_created_at
    ON calc_audit_runs(created_at);

PRAGMA foreign_keys = ON;

-- Validation guidance:
-- This migration defines the calculation run persistence schema v1.
-- Validate schema changes against temporary/test SQLite databases,
-- not database/finance.db or live data.
-- Tests must not modify database/finance.db.
-- Do not seed or change live database data unless Wen explicitly
-- requests it.
