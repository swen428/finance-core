PRAGMA foreign_keys = ON;

-- Versioned, hash-verifiable, append-only calculation snapshots.
CREATE TABLE IF NOT EXISTS authoritative_calculation_snapshots (
    snapshot_public_id TEXT PRIMARY KEY,
    snapshot_schema_version TEXT NOT NULL CHECK (snapshot_schema_version = 'v1'),
    calculation_type TEXT NOT NULL CHECK (calculation_type != ''),
    aggregate_public_id TEXT NOT NULL CHECK (aggregate_public_id != ''),
    input_payload_json TEXT NOT NULL,
    output_payload_json TEXT NOT NULL,
    rules_payload_json TEXT NOT NULL,
    input_hash TEXT NOT NULL CHECK (
        length(input_hash) = 64 AND input_hash NOT GLOB '*[^0-9a-f]*'
    ),
    output_hash TEXT NOT NULL CHECK (
        length(output_hash) = 64 AND output_hash NOT GLOB '*[^0-9a-f]*'
    ),
    rules_hash TEXT NOT NULL CHECK (
        length(rules_hash) = 64 AND rules_hash NOT GLOB '*[^0-9a-f]*'
    ),
    combined_snapshot_hash TEXT NOT NULL UNIQUE CHECK (
        length(combined_snapshot_hash) = 64
        AND combined_snapshot_hash NOT GLOB '*[^0-9a-f]*'
    ),
    money_contract_version TEXT NOT NULL CHECK (money_contract_version != ''),
    currency_contract_version TEXT NOT NULL CHECK (currency_contract_version != ''),
    algorithm_version TEXT NOT NULL CHECK (algorithm_version != ''),
    source_references_json TEXT NOT NULL DEFAULT '[]',
    previous_snapshot_public_id TEXT,
    actor_type TEXT NOT NULL CHECK (actor_type != ''),
    actor_public_id TEXT,
    authorization_reference TEXT,
    finalization_status TEXT NOT NULL CHECK (
        finalization_status IN ('draft', 'finalized', 'invalidated')
    ),
    created_at TEXT NOT NULL CHECK (created_at != ''),
    FOREIGN KEY (previous_snapshot_public_id)
        REFERENCES authoritative_calculation_snapshots(snapshot_public_id)
);

CREATE INDEX IF NOT EXISTS idx_authoritative_snapshots_aggregate
    ON authoritative_calculation_snapshots(calculation_type, aggregate_public_id);
CREATE INDEX IF NOT EXISTS idx_authoritative_snapshots_previous
    ON authoritative_calculation_snapshots(previous_snapshot_public_id)
    WHERE previous_snapshot_public_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_authoritative_snapshots_created
    ON authoritative_calculation_snapshots(created_at, snapshot_public_id);

CREATE TRIGGER IF NOT EXISTS trg_authoritative_snapshots_no_update
BEFORE UPDATE ON authoritative_calculation_snapshots
BEGIN
    SELECT RAISE(ABORT, 'authoritative calculation snapshots are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_authoritative_snapshots_no_delete
BEFORE DELETE ON authoritative_calculation_snapshots
BEGIN
    SELECT RAISE(ABORT, 'authoritative calculation snapshots are append-only');
END;
