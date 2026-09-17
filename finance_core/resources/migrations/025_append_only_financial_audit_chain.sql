PRAGMA foreign_keys = ON;

-- Versioned, tamper-evident, append-only audit chains for financial state.
CREATE TABLE IF NOT EXISTS financial_audit_events (
    event_public_id TEXT PRIMARY KEY CHECK (event_public_id != ''),
    audit_schema_version TEXT NOT NULL CHECK (audit_schema_version = 'v1'),
    aggregate_type TEXT NOT NULL CHECK (aggregate_type != ''),
    aggregate_public_id TEXT NOT NULL CHECK (aggregate_public_id != ''),
    event_type TEXT NOT NULL CHECK (event_type != ''),
    event_payload_json TEXT NOT NULL,
    previous_state_json TEXT NOT NULL,
    new_state_json TEXT NOT NULL,
    previous_state_hash TEXT NOT NULL CHECK (
        length(previous_state_hash) = 64
        AND previous_state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    new_state_hash TEXT NOT NULL CHECK (
        length(new_state_hash) = 64
        AND new_state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    previous_event_hash TEXT NOT NULL CHECK (
        length(previous_event_hash) = 64
        AND previous_event_hash NOT GLOB '*[^0-9a-f]*'
    ),
    event_hash TEXT NOT NULL UNIQUE CHECK (
        length(event_hash) = 64
        AND event_hash NOT GLOB '*[^0-9a-f]*'
    ),
    actor_type TEXT NOT NULL CHECK (actor_type != ''),
    actor_public_id TEXT NOT NULL CHECK (actor_public_id != ''),
    authorization_public_id TEXT,
    calculation_snapshot_public_id TEXT,
    calculation_snapshot_hash TEXT,
    source_evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    correlation_public_id TEXT NOT NULL CHECK (correlation_public_id != ''),
    causation_public_id TEXT NOT NULL CHECK (causation_public_id != ''),
    sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    created_at TEXT NOT NULL CHECK (created_at != ''),
    CHECK (
        (calculation_snapshot_public_id IS NULL AND calculation_snapshot_hash IS NULL)
        OR (
            calculation_snapshot_public_id IS NOT NULL
            AND calculation_snapshot_public_id != ''
            AND calculation_snapshot_hash IS NOT NULL
            AND length(calculation_snapshot_hash) = 64
            AND calculation_snapshot_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    UNIQUE (aggregate_type, aggregate_public_id, sequence_number),
    UNIQUE (aggregate_type, aggregate_public_id, previous_event_hash),
    UNIQUE (
        aggregate_type,
        aggregate_public_id,
        event_type,
        causation_public_id
    )
);

CREATE INDEX IF NOT EXISTS idx_financial_audit_events_aggregate
    ON financial_audit_events(
        aggregate_type,
        aggregate_public_id,
        sequence_number
    );
CREATE INDEX IF NOT EXISTS idx_financial_audit_events_correlation
    ON financial_audit_events(correlation_public_id);
CREATE INDEX IF NOT EXISTS idx_financial_audit_events_authorization
    ON financial_audit_events(authorization_public_id)
    WHERE authorization_public_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_financial_audit_events_created
    ON financial_audit_events(created_at, event_public_id);

CREATE TRIGGER IF NOT EXISTS trg_financial_audit_events_no_update
BEFORE UPDATE ON financial_audit_events
BEGIN
    SELECT RAISE(ABORT, 'financial audit events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_financial_audit_events_no_delete
BEFORE DELETE ON financial_audit_events
BEGIN
    SELECT RAISE(ABORT, 'financial audit events are append-only');
END;
