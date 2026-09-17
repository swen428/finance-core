PRAGMA foreign_keys = OFF;

-- Reconciliation Final Mutation Guard Decision Persistence Schema v1
-- Persists guard decisions and dry-run previews for final mutation proposals
-- into SQLite for auditability and idempotency across process restarts.
--
-- This migration is additive on top of migrations 001-012.
-- No existing tables are altered.
--
-- Design properties:
-- - Never touches database/finance.db or live data.
-- - Stores guard decisions only -- does NOT create, update, delete, or
--   mutate final financial records.
-- - idempotency_key is UNIQUE to prevent duplicate guard decisions from
--   being persisted for the same proposal context.
-- - preview_json stores a serialised FinalMutationPreview for approved
--   decisions; blocked decisions have preview_json NULL.
-- - blocked_reasons_json uses deterministic ordering (sort_keys=True).
-- - evidence_refs_json uses deterministic ordering.
-- - guard_version is persisted for future guard-version-aware replay.
-- - actor_type and actor_id record which component or person triggered
--   the guard evaluation.

CREATE TABLE IF NOT EXISTS reconciliation_final_mutation_guard_decisions (
    id INTEGER PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL
        CHECK (action IN (
            'create_final_transaction_proposal',
            'adjust_final_transaction_proposal',
            'no_final_mutation',
            'blocked'
        )),
    approved INTEGER NOT NULL CHECK (approved IN (0, 1)),
    blocked_reasons_json TEXT NOT NULL,
    preview_json TEXT,
    evidence_refs_json TEXT NOT NULL,
    source_statement_ref TEXT,
    source_app_transaction_ref TEXT,
    target_transaction_id TEXT,
    guard_version TEXT NOT NULL CHECK (guard_version != ''),
    actor_type TEXT NOT NULL DEFAULT 'system',
    actor_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- Lookup by proposal_id.
CREATE INDEX IF NOT EXISTS idx_final_mutation_decisions_proposal_id
    ON reconciliation_final_mutation_guard_decisions(proposal_id);

-- Filter by action for audit queries.
CREATE INDEX IF NOT EXISTS idx_final_mutation_decisions_action
    ON reconciliation_final_mutation_guard_decisions(action);

-- Filter by approved status.
CREATE INDEX IF NOT EXISTS idx_final_mutation_decisions_approved
    ON reconciliation_final_mutation_guard_decisions(approved);

-- Order by created_at for timeline views.
CREATE INDEX IF NOT EXISTS idx_final_mutation_decisions_created_at
    ON reconciliation_final_mutation_guard_decisions(created_at);

-- Lookup by source_statement_ref.
CREATE INDEX IF NOT EXISTS idx_final_mutation_decisions_stmt_ref
    ON reconciliation_final_mutation_guard_decisions(source_statement_ref)
    WHERE source_statement_ref IS NOT NULL;

-- Lookup by source_app_transaction_ref.
CREATE INDEX IF NOT EXISTS idx_final_mutation_decisions_app_ref
    ON reconciliation_final_mutation_guard_decisions(source_app_transaction_ref)
    WHERE source_app_transaction_ref IS NOT NULL;

-- Lookup by target_transaction_id.
CREATE INDEX IF NOT EXISTS idx_final_mutation_decisions_target_id
    ON reconciliation_final_mutation_guard_decisions(target_transaction_id)
    WHERE target_transaction_id IS NOT NULL;

PRAGMA foreign_keys = ON;

-- Validation guidance:
-- This migration defines the reconciliation final mutation guard decision
-- persistence schema v1. Validate schema changes against temporary/test SQLite
-- databases, not database/finance.db or live data.
-- Tests must not modify database/finance.db.
-- Do not seed or change live database data unless Wen explicitly
-- requests it.
