-- Migration 037: IAF fact-set binding evidence (IAF.5-IAF.8 remediation)
--
-- Records the active IAF fact-set four-tuple
-- (fact_set_public_id, fact_set_version, fact_set_input_hash,
-- fact_set_result_hash) immutably and machine-verifiably for every downstream
-- authority record that consumed it: the historical calculation run
-- (calc_audit_runs), the authoritative calculation snapshot, the human
-- finalization authorization, and the finalization audit.
--
-- Rationale: before this migration the four-tuple was only expressible inside
-- hash-anchored JSON payloads, and calc_audit_runs.source_reference carried the
-- fact-set result hash alone.  That is not a complete, queryable, per-record
-- binding contract: neither the fact-set public ID, the version, nor the input
-- hash could be verified against a persisted authority row without re-parsing
-- payload JSON.
--
-- Additive only:
--   * one new append-only table receipt_fact_set_binding_evidence;
--   * one real foreign key per bound authority record type, with an
--     exactly-one-target CHECK so the discriminator can never disagree with
--     the populated FK column;
--   * per-target partial unique indexes (one binding row per authority record);
--   * append-only no-insert-collision / no-update / no-delete triggers.  The
--     insert-collision guard is required in addition to the delete guard
--     because SQLite runs `INSERT OR REPLACE`'s implicit row removal without
--     firing DELETE triggers unless `recursive_triggers` is ON, which is OFF by
--     default and is a per-connection setting this schema must not depend on;
--   * a four-tuple collision backstop trigger: the same fact_set_public_id may
--     never be bound with a different receipt, version, or hash pair.
-- Migrations 001-036 are untouched.  Legacy rows and non-IAF finalizations are
-- unaffected: absence of a binding evidence row means "no IAF fact set was
-- consumed", never "the binding is unknown".

CREATE TABLE IF NOT EXISTS receipt_fact_set_binding_evidence (
    binding_public_id TEXT PRIMARY KEY NOT NULL CHECK (
        substr(binding_public_id, 1, 5) = 'rfsb_'
        AND length(binding_public_id) BETWEEN 6 AND 200
        AND binding_public_id NOT GLOB '*[^A-Za-z0-9_-]*'
    ),
    bound_record_type TEXT NOT NULL CHECK (
        bound_record_type IN (
            'calculation_run',
            'calculation_snapshot',
            'finalization_authorization',
            'finalization_audit'
        )
    ),
    receipt_public_id TEXT NOT NULL CHECK (
        length(receipt_public_id) BETWEEN 1 AND 200
        AND receipt_public_id NOT GLOB '*[^A-Za-z0-9_-]*'
    ),
    fact_set_public_id TEXT NOT NULL CHECK (
        substr(fact_set_public_id, 1, 4) = 'rfs_'
        AND length(fact_set_public_id) BETWEEN 5 AND 200
        AND fact_set_public_id NOT GLOB '*[^A-Za-z0-9_-]*'
    ),
    fact_set_version INTEGER NOT NULL CHECK (
        typeof(fact_set_version) = 'integer' AND fact_set_version >= 1
    ),
    fact_set_input_hash TEXT NOT NULL CHECK (
        length(fact_set_input_hash) = 64
        AND fact_set_input_hash NOT GLOB '*[^0-9a-f]*'
    ),
    fact_set_result_hash TEXT NOT NULL CHECK (
        length(fact_set_result_hash) = 64
        AND fact_set_result_hash NOT GLOB '*[^0-9a-f]*'
    ),
    calculation_run_id TEXT REFERENCES calc_audit_runs(run_id),
    calculation_snapshot_public_id TEXT
        REFERENCES authoritative_calculation_snapshots(snapshot_public_id),
    finalization_authorization_id TEXT
        REFERENCES receipt_finalization_authorizations(authorization_id),
    finalization_audit_id TEXT REFERENCES receipt_finalization_audit(finalization_id),
    schema_version TEXT NOT NULL CHECK (schema_version = 'v1'),
    created_at TEXT NOT NULL CHECK (length(created_at) >= 20),
    -- Exactly one authority target per binding evidence row.
    CHECK (
        (CASE WHEN calculation_run_id IS NULL THEN 0 ELSE 1 END)
        + (CASE WHEN calculation_snapshot_public_id IS NULL THEN 0 ELSE 1 END)
        + (CASE WHEN finalization_authorization_id IS NULL THEN 0 ELSE 1 END)
        + (CASE WHEN finalization_audit_id IS NULL THEN 0 ELSE 1 END)
        = 1
    ),
    -- The discriminator can never disagree with the populated FK column.
    CHECK (
        (bound_record_type = 'calculation_run')
        = (calculation_run_id IS NOT NULL)
    ),
    CHECK (
        (bound_record_type = 'calculation_snapshot')
        = (calculation_snapshot_public_id IS NOT NULL)
    ),
    CHECK (
        (bound_record_type = 'finalization_authorization')
        = (finalization_authorization_id IS NOT NULL)
    ),
    CHECK (
        (bound_record_type = 'finalization_audit')
        = (finalization_audit_id IS NOT NULL)
    )
);

-- One binding evidence row per authority record.
CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_set_binding_evidence_run
    ON receipt_fact_set_binding_evidence(calculation_run_id)
    WHERE calculation_run_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_set_binding_evidence_snapshot
    ON receipt_fact_set_binding_evidence(calculation_snapshot_public_id)
    WHERE calculation_snapshot_public_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_set_binding_evidence_authorization
    ON receipt_fact_set_binding_evidence(finalization_authorization_id)
    WHERE finalization_authorization_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_set_binding_evidence_audit
    ON receipt_fact_set_binding_evidence(finalization_audit_id)
    WHERE finalization_audit_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_fact_set_binding_evidence_fact_set
    ON receipt_fact_set_binding_evidence(fact_set_public_id, fact_set_version);
CREATE INDEX IF NOT EXISTS idx_fact_set_binding_evidence_receipt
    ON receipt_fact_set_binding_evidence(receipt_public_id, created_at);

CREATE TRIGGER IF NOT EXISTS trg_fact_set_binding_evidence_no_update
BEFORE UPDATE ON receipt_fact_set_binding_evidence
BEGIN
    SELECT RAISE(ABORT, 'receipt_fact_set_binding_evidence rows are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_fact_set_binding_evidence_no_delete
BEFORE DELETE ON receipt_fact_set_binding_evidence
BEGIN
    SELECT RAISE(ABORT, 'receipt_fact_set_binding_evidence rows are append-only');
END;

-- Identity collision backstop (mirrors the migration 035/036 pattern).  A
-- conflict-resolution INSERT (`INSERT OR REPLACE`, `INSERT OR IGNORE`) must
-- abort *before* SQLite silently removes or skips the existing row: the
-- implicit REPLACE deletion does not fire the BEFORE DELETE trigger while
-- `PRAGMA recursive_triggers` is OFF (the default), and this schema must not
-- rely on any per-connection pragma or foreign-key setting for immutability.
-- Legitimate non-colliding appends are unaffected.
CREATE TRIGGER IF NOT EXISTS trg_fact_set_binding_evidence_no_insert_collision
BEFORE INSERT ON receipt_fact_set_binding_evidence
WHEN EXISTS (
    SELECT 1
    FROM receipt_fact_set_binding_evidence AS existing
    WHERE existing.binding_public_id = NEW.binding_public_id
       OR (
           NEW.calculation_run_id IS NOT NULL
           AND existing.calculation_run_id = NEW.calculation_run_id
       )
       OR (
           NEW.calculation_snapshot_public_id IS NOT NULL
           AND existing.calculation_snapshot_public_id = NEW.calculation_snapshot_public_id
       )
       OR (
           NEW.finalization_authorization_id IS NOT NULL
           AND existing.finalization_authorization_id = NEW.finalization_authorization_id
       )
       OR (
           NEW.finalization_audit_id IS NOT NULL
           AND existing.finalization_audit_id = NEW.finalization_audit_id
       )
)
BEGIN
    SELECT RAISE(
        ABORT,
        'UNIQUE fact-set binding evidence identity collision: receipt_fact_set_binding_evidence rows are append-only and cannot be replaced'
    );
END;

-- Four-tuple collision backstop: one fact_set_public_id has exactly one
-- receipt, version, input hash, and result hash across every authority record
-- that ever consumed it.  A contradictory binding is refused at the schema
-- level even if a caller bypasses the service boundary.
CREATE TRIGGER IF NOT EXISTS trg_fact_set_binding_evidence_four_tuple_stable
BEFORE INSERT ON receipt_fact_set_binding_evidence
WHEN EXISTS (
    SELECT 1
    FROM receipt_fact_set_binding_evidence AS existing
    WHERE existing.fact_set_public_id = NEW.fact_set_public_id
      AND (
          existing.receipt_public_id <> NEW.receipt_public_id
          OR existing.fact_set_version <> NEW.fact_set_version
          OR existing.fact_set_input_hash <> NEW.fact_set_input_hash
          OR existing.fact_set_result_hash <> NEW.fact_set_result_hash
      )
)
BEGIN
    SELECT RAISE(
        ABORT,
        'fact-set binding evidence contradicts an existing four-tuple for this fact set'
    );
END;

-- Registry backstop: the bound fact set must exist in the IAF registry with the
-- exact same four-tuple.  Binding evidence can never reference a fact set that
-- was never persisted through the guarded IAF boundary.
CREATE TRIGGER IF NOT EXISTS trg_fact_set_binding_evidence_registry_binding
BEFORE INSERT ON receipt_fact_set_binding_evidence
WHEN NOT EXISTS (
    SELECT 1
    FROM receipt_item_allocation_fact_sets AS fs
    JOIN receipts AS r ON r.id = fs.receipt_id
    WHERE fs.fact_set_public_id = NEW.fact_set_public_id
      AND fs.version = NEW.fact_set_version
      AND fs.fact_set_input_hash = NEW.fact_set_input_hash
      AND fs.fact_set_result_hash = NEW.fact_set_result_hash
      AND r.public_id = NEW.receipt_public_id
)
BEGIN
    SELECT RAISE(
        ABORT,
        'fact-set binding evidence does not match a persisted IAF fact-set registry row'
    );
END;
