PRAGMA foreign_keys = ON;

-- Reconciliation Final Mutation Audit v1
-- Persists the audit trail for every guarded final-mutation workflow attempt.
-- Each row records the complete authorization chain, mutation payload, and
-- outcome so that the workflow can enforce durable idempotency without
-- runtime DDL.
CREATE TABLE IF NOT EXISTS reconciliation_final_mutation_audit (
    final_mutation_id       TEXT PRIMARY KEY,
    idempotency_key         TEXT NOT NULL UNIQUE,
    idempotency_fingerprint TEXT NOT NULL,
    operation_id            TEXT NOT NULL,
    plan_id                 TEXT NOT NULL,
    proposal_id             TEXT NOT NULL,
    guard_decision_proposal_id TEXT NOT NULL,
    guard_version           TEXT NOT NULL DEFAULT 'v1',
    guarded_execution_id    TEXT NOT NULL,
    guarded_execution_idempotency_key TEXT NOT NULL,
    human_confirmation_id   TEXT NOT NULL,
    actor_type              TEXT NOT NULL,
    actor_id                TEXT,
    action                  TEXT NOT NULL,
    status                  TEXT NOT NULL,
    blocked_reasons_json    TEXT NOT NULL DEFAULT '[]',
    source_statement_ref    TEXT,
    source_app_transaction_ref TEXT,
    target_transaction_id   TEXT,
    transaction_public_id   TEXT,
    mutation_payload_json   TEXT NOT NULL DEFAULT '{}',
    previous_values_json    TEXT,
    evidence_refs_json      TEXT NOT NULL DEFAULT '[]',
    audit_refs_json         TEXT NOT NULL DEFAULT '{}',
    created_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_final_mutation_audit_status
    ON reconciliation_final_mutation_audit(status);
CREATE INDEX IF NOT EXISTS idx_final_mutation_audit_created_at
    ON reconciliation_final_mutation_audit(created_at);
