PRAGMA foreign_keys = OFF;

-- Reconciliation Apply State Persistence Schema v1
-- Persists apply batch lifecycle state and state transition audit records
-- into SQLite for future cross-process duplicate apply prevention and
-- retry behavior.
--
-- This migration is additive on top of migrations 001-011.
-- No existing tables are altered.
--
-- Design properties:
-- - Never touches database/finance.db or live data.
-- - Does NOT connect runtime behavior to SQLite yet.
-- - This schema prepares for future cross-process duplicate apply
--   prevention: stored batch state preserves the terminal APPLIED
--   state even when duplicate apply attempts arrive later.
-- - State transition audit records capture every state change for
--   auditability across process restarts.
-- - v1 JSON boundary: applied_result_json stores a serialized
--   BatchApplyResult-style payload until a more normalized schema
--   is needed. audit_metadata_json stores structured transition
--   metadata.

CREATE TABLE IF NOT EXISTS reconciliation_apply_batches (
    id INTEGER PRIMARY KEY,
    batch_id TEXT NOT NULL UNIQUE,
    current_state TEXT NOT NULL
        CHECK (current_state IN (
            'pending',
            'applying',
            'applied',
            'failed',
            'rejected'
        )),
    idempotency_key TEXT,
    source TEXT NOT NULL DEFAULT 'reconciliation',
    runtime_version TEXT NOT NULL DEFAULT 'v1',
    batch_state_control_version TEXT NOT NULL DEFAULT 'v1',
    applied_result_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reconciliation_apply_batch_state_transitions (
    id INTEGER PRIMARY KEY,
    batch_id TEXT NOT NULL,
    previous_state TEXT
        CHECK (
            previous_state IS NULL
            OR previous_state IN (
                'pending',
                'applying',
                'applied',
                'failed',
                'rejected'
            )
        ),
    new_state TEXT NOT NULL
        CHECK (new_state IN (
            'pending',
            'applying',
            'applied',
            'failed',
            'rejected'
        )),
    reason TEXT NOT NULL,
    transition_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    audit_metadata_json TEXT,
    actor_type TEXT NOT NULL DEFAULT 'system',
    actor_ref TEXT,
    source_ref TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (batch_id)
        REFERENCES reconciliation_apply_batches(batch_id)
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- Unique idempotency_key when present (NULLs are not considered duplicates).
CREATE UNIQUE INDEX IF NOT EXISTS idx_apply_batches_idempotency_key
    ON reconciliation_apply_batches(idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- Filter by current_state for status queries.
CREATE INDEX IF NOT EXISTS idx_apply_batches_current_state
    ON reconciliation_apply_batches(current_state);

-- Transition lookups by batch_id for audit UIs and reporting.
CREATE INDEX IF NOT EXISTS idx_apply_batch_transitions_batch_id
    ON reconciliation_apply_batch_state_transitions(batch_id);

-- Transition filter by time range for dashboards.
CREATE INDEX IF NOT EXISTS idx_apply_batch_transitions_transition_at
    ON reconciliation_apply_batch_state_transitions(transition_at);

PRAGMA foreign_keys = ON;

-- Validation guidance:
-- This migration defines the reconciliation apply state persistence
-- schema v1. Validate schema changes against temporary/test SQLite
-- databases, not database/finance.db or live data.
-- Tests must not modify database/finance.db.
-- Do not seed or change live database data unless Wen explicitly
-- requests it.
