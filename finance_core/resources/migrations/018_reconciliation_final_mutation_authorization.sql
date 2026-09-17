PRAGMA foreign_keys = ON;

-- Persisted authority for the existing guarded reconciliation final-write
-- boundary.  Confirmation is a separate durable trust record; authorization
-- only binds that record to guard and execution evidence.  Neither table
-- creates final financial records or enables a new write path.
CREATE TABLE IF NOT EXISTS reconciliation_final_mutation_confirmations (
    confirmation_id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK (subject_type = 'reconciliation_apply_operation'),
    subject_id TEXT NOT NULL,
    operation_type TEXT NOT NULL CHECK (operation_type IN (
        'create_final_transaction_proposal',
        'adjust_final_transaction_proposal'
    )),
    proposal_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    confirmation_state TEXT NOT NULL CHECK (confirmation_state IN (
        'confirmed', 'rejected', 'revoked', 'superseded', 'expired', 'cancelled'
    )),
    confirmed_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_reconciliation_final_mutation_confirmations_subject
    ON reconciliation_final_mutation_confirmations(subject_type, subject_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reconciliation_final_mutation_active_confirmation
    ON reconciliation_final_mutation_confirmations(
        subject_type, subject_id, operation_type, content_hash
    ) WHERE confirmation_state = 'confirmed';

CREATE TABLE IF NOT EXISTS reconciliation_final_mutation_authorizations (
    authorization_id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK (subject_type = 'reconciliation_apply_operation'),
    subject_id TEXT NOT NULL,
    operation_type TEXT NOT NULL CHECK (operation_type IN (
        'create_final_transaction_proposal',
        'adjust_final_transaction_proposal'
    )),
    proposal_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    guard_decision_idempotency_key TEXT NOT NULL,
    guarded_execution_id TEXT NOT NULL,
    human_confirmation_id TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    authorization_state TEXT NOT NULL CHECK (authorization_state IN (
        'authorized', 'rejected', 'revoked', 'superseded', 'expired'
    )),
    authorization_version TEXT NOT NULL DEFAULT 'v1' CHECK (authorization_version != ''),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (guard_decision_idempotency_key)
        REFERENCES reconciliation_final_mutation_guard_decisions(idempotency_key),
    FOREIGN KEY (guarded_execution_id)
        REFERENCES reconciliation_guarded_apply_executions(execution_id),
    FOREIGN KEY (human_confirmation_id)
        REFERENCES reconciliation_final_mutation_confirmations(confirmation_id)
);

CREATE INDEX IF NOT EXISTS idx_reconciliation_final_mutation_auth_subject
    ON reconciliation_final_mutation_authorizations(subject_type, subject_id);
CREATE INDEX IF NOT EXISTS idx_reconciliation_final_mutation_auth_confirmation
    ON reconciliation_final_mutation_authorizations(human_confirmation_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reconciliation_final_mutation_active_auth
    ON reconciliation_final_mutation_authorizations(
        subject_type, subject_id, operation_type, content_hash
    ) WHERE authorization_state = 'authorized';
