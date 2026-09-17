PRAGMA foreign_keys = ON;

-- Receipt Finalization Authorization, Idempotency & Audit v2
-- Migration 020: Hardened receipt finalization with persisted authorization,
-- durable idempotency, complete audit trail, and database-level uniqueness
-- constraints.  All schema creation is done here -- no runtime DDL is
-- performed by the finalization workflow.
--
-- This migration is additive on top of migrations 001-019.
-- No existing tables are altered.
--
-- Audit semantics (v2): ``receipt_finalization_audit`` stores successful
-- authoritative finalizations only (status = 'finalized').  Blocked and
-- failed attempts are returned as typed domain errors and are not durably
-- persisted in this release.  The ``receipt_finalization_idempotency``
-- table stores only successful idempotency records linked to the audit.

-- -------------------------------------------------------------------------
-- 1. Receipt Finalization Confirmations
-- -------------------------------------------------------------------------
-- Human confirmation that a calculated receipt split is approved for
-- finalization.  Every material field is validated against the authorization
-- record and the finalization request before any write.
CREATE TABLE IF NOT EXISTS receipt_finalization_confirmations (
    confirmation_id      TEXT PRIMARY KEY,
    receipt_group_public_id TEXT NOT NULL,
    calculation_run_public_id TEXT NOT NULL,
    calculation_snapshot_id TEXT NOT NULL,
    content_hash         TEXT NOT NULL CHECK (length(content_hash) = 64),
    currency             TEXT NOT NULL,
    final_total          TEXT NOT NULL,
    payer_participant_public_id TEXT NOT NULL,
    participant_public_ids_json TEXT NOT NULL DEFAULT '[]',
    settlement_obligations_json TEXT NOT NULL DEFAULT '[]',
    actor_type           TEXT NOT NULL CHECK (actor_type IN ('human', 'cli', 'system')),
    actor_id             TEXT,
    confirmation_state   TEXT NOT NULL CHECK (confirmation_state IN (
        'confirmed', 'rejected', 'revoked', 'superseded', 'expired'
    )),
    created_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_receipt_finalization_confirmations_group
    ON receipt_finalization_confirmations(receipt_group_public_id);
CREATE INDEX IF NOT EXISTS idx_receipt_finalization_confirmations_state
    ON receipt_finalization_confirmations(confirmation_state);
CREATE UNIQUE INDEX IF NOT EXISTS uq_receipt_finalization_active_confirmation
    ON receipt_finalization_confirmations(
        receipt_group_public_id, calculation_run_public_id, content_hash
    ) WHERE confirmation_state = 'confirmed';

-- -------------------------------------------------------------------------
-- 2. Receipt Finalization Authorizations
-- -------------------------------------------------------------------------
-- Persisted authority binding a confirmed calculation to a specific content
-- hash, actor, participant set, and evidence bundle.  The finalization
-- workflow loads and fully validates this record and its linked confirmation
-- before any write.
CREATE TABLE IF NOT EXISTS receipt_finalization_authorizations (
    authorization_id     TEXT PRIMARY KEY,
    receipt_group_public_id TEXT NOT NULL,
    calculation_run_public_id TEXT NOT NULL,
    calculation_snapshot_id TEXT NOT NULL,
    confirmation_id      TEXT NOT NULL,
    content_hash         TEXT NOT NULL CHECK (length(content_hash) = 64),
    currency             TEXT NOT NULL,
    final_total          TEXT NOT NULL,
    payer_participant_public_id TEXT NOT NULL,
    participant_public_ids_json TEXT NOT NULL DEFAULT '[]',
    settlement_obligations_json TEXT NOT NULL DEFAULT '[]',
    source_evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    actor_type           TEXT NOT NULL CHECK (actor_type IN ('human', 'cli', 'system')),
    actor_id             TEXT,
    authorization_state  TEXT NOT NULL CHECK (authorization_state IN (
        'authorized', 'revoked', 'expired', 'consumed'
    )),
    authorization_version TEXT NOT NULL DEFAULT 'v1' CHECK (authorization_version != ''),
    created_at           TEXT NOT NULL,
    FOREIGN KEY (confirmation_id)
        REFERENCES receipt_finalization_confirmations(confirmation_id)
);

CREATE INDEX IF NOT EXISTS idx_receipt_finalization_auth_group
    ON receipt_finalization_authorizations(receipt_group_public_id);
CREATE INDEX IF NOT EXISTS idx_receipt_finalization_auth_confirmation
    ON receipt_finalization_authorizations(confirmation_id);
CREATE INDEX IF NOT EXISTS idx_receipt_finalization_auth_state
    ON receipt_finalization_authorizations(authorization_state);
CREATE UNIQUE INDEX IF NOT EXISTS uq_receipt_finalization_active_auth
    ON receipt_finalization_authorizations(
        receipt_group_public_id, content_hash
    ) WHERE authorization_state = 'authorized';

-- -------------------------------------------------------------------------
-- 3. Receipt Finalization Audit
-- -------------------------------------------------------------------------
-- Immutable audit record for successful authoritative finalizations.
-- One finalized audit row per idempotency key (unique constraint).
-- One finalized audit row per receipt group (partial unique index).
-- Foreign keys enforce referential integrity to authorization and
-- confirmation records.
CREATE TABLE IF NOT EXISTS receipt_finalization_audit (
    finalization_id      TEXT PRIMARY KEY,
    idempotency_key      TEXT NOT NULL,
    content_fingerprint  TEXT NOT NULL,
    authorization_id     TEXT NOT NULL,
    confirmation_id      TEXT,
    receipt_group_public_id TEXT NOT NULL,
    calculation_run_public_id TEXT NOT NULL,
    calculation_snapshot_id TEXT,
    transaction_public_id TEXT,
    participant_public_ids_json TEXT NOT NULL DEFAULT '[]',
    settlement_public_ids_json TEXT NOT NULL DEFAULT '[]',
    currency             TEXT NOT NULL,
    total_paid           TEXT NOT NULL,
    total_to_collect     TEXT NOT NULL,
    payer_participant_public_id TEXT NOT NULL,
    actor_type           TEXT NOT NULL,
    actor_id             TEXT,
    status               TEXT NOT NULL CHECK (status IN (
        'finalized', 'already_finalized'
    )),
    failure_reasons_json TEXT NOT NULL DEFAULT '[]',
    evidence_refs_json   TEXT NOT NULL DEFAULT '[]',
    source_attachment_refs_json TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL,
    FOREIGN KEY (authorization_id)
        REFERENCES receipt_finalization_authorizations(authorization_id),
    FOREIGN KEY (confirmation_id)
        REFERENCES receipt_finalization_confirmations(confirmation_id)
);

CREATE INDEX IF NOT EXISTS idx_receipt_finalization_audit_group
    ON receipt_finalization_audit(receipt_group_public_id);
CREATE INDEX IF NOT EXISTS idx_receipt_finalization_audit_status
    ON receipt_finalization_audit(status);
CREATE INDEX IF NOT EXISTS idx_receipt_finalization_audit_created_at
    ON receipt_finalization_audit(created_at);

-- One idempotency key -> one finalization outcome.
CREATE UNIQUE INDEX IF NOT EXISTS uq_receipt_finalization_audit_idempotency
    ON receipt_finalization_audit(idempotency_key);

-- One successful finalization per receipt group (database-level enforcement).
CREATE UNIQUE INDEX IF NOT EXISTS uq_receipt_finalization_audit_one_per_group
    ON receipt_finalization_audit(receipt_group_public_id)
    WHERE status = 'finalized';

-- -------------------------------------------------------------------------
-- 4. Receipt Finalization Idempotency
-- -------------------------------------------------------------------------
-- Durable idempotency guard for successful finalizations only.
-- The PRIMARY KEY on idempotency_key provides the final conflict authority
-- at the database level.
-- Foreign key ensures every idempotency row references a valid audit row.
CREATE TABLE IF NOT EXISTS receipt_finalization_idempotency (
    idempotency_key      TEXT PRIMARY KEY,
    content_fingerprint  TEXT NOT NULL CHECK (length(content_fingerprint) = 64),
    status               TEXT NOT NULL CHECK (status IN (
        'finalized', 'already_finalized'
    )),
    finalization_audit_id TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    FOREIGN KEY (finalization_audit_id)
        REFERENCES receipt_finalization_audit(finalization_id)
);

CREATE INDEX IF NOT EXISTS idx_receipt_finalization_idempotency_status
    ON receipt_finalization_idempotency(status);

-- -------------------------------------------------------------------------
-- 5. Receipt Group Status
-- -------------------------------------------------------------------------
-- The receipt_groups table (migration 002) already includes 'settled' as a
-- valid CHECK status.  The finalization workflow uses 'settled' after
-- successful finalization.  No DDL change to receipt_groups is needed.

-- Validation guidance:
-- This migration defines the receipt finalization hardening schema v2.
-- Validate schema changes against temporary/test SQLite databases,
-- not database/finance.db or live data.
-- Tests must not modify database/finance.db.
-- Do not seed or change live database data unless Wen explicitly
-- requests it.
