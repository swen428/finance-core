-- Migration 038: finalization audit and idempotency immutability
-- (IAF.5-IAF.8 remediation, REM-06)
--
-- ``receipt_finalization_audit`` and ``receipt_finalization_idempotency``
-- (migration 020) are semantically immutable: the audit row is the durable
-- record of one completed finalization, and the idempotency row is the durable
-- proof that a given content fingerprint was already finalized.  The finalizer
-- inserts each row exactly once inside its single ``BEGIN IMMEDIATE`` Unit of
-- Work and never revises it, so an UPDATE, DELETE, or conflict-resolution
-- re-INSERT of either row can only be corruption or an attempt to make a
-- forged replay look authentic.
--
-- Migration 020 expressed that intent only in comments.  This migration
-- enforces it at the schema level, which is what the idempotent-replay
-- verifier needs in order to treat the audit row as durable truth rather than
-- as a mutable cache.
--
-- Additive only:
--   * BEFORE UPDATE / BEFORE DELETE guards on both tables;
--   * BEFORE INSERT identity-collision guards, required in addition to the
--     DELETE guards because SQLite performs `INSERT OR REPLACE`'s implicit row
--     removal without firing DELETE triggers while `PRAGMA recursive_triggers`
--     is OFF -- the default, and a per-connection setting this schema must not
--     depend on.
--
-- Migrations 001-037 are untouched.  No table, column, index, or constraint is
-- modified, and legitimate first-time finalization inserts are unaffected: no
-- code path in this repository has ever updated or deleted either row, so
-- there is no legacy behaviour to preserve.  Authorization and confirmation
-- rows keep their existing mutable lifecycle (``authorized`` -> ``consumed``)
-- and are deliberately not covered here.

CREATE TRIGGER IF NOT EXISTS trg_receipt_finalization_audit_no_update
BEFORE UPDATE ON receipt_finalization_audit
BEGIN
    SELECT RAISE(
        ABORT,
        'receipt_finalization_audit rows are immutable finalization evidence'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_finalization_audit_no_delete
BEFORE DELETE ON receipt_finalization_audit
BEGIN
    SELECT RAISE(
        ABORT,
        'receipt_finalization_audit rows are immutable finalization evidence'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_finalization_audit_no_insert_collision
BEFORE INSERT ON receipt_finalization_audit
WHEN EXISTS (
    SELECT 1
    FROM receipt_finalization_audit AS existing
    WHERE existing.finalization_id = NEW.finalization_id
       OR existing.idempotency_key = NEW.idempotency_key
       OR (
           existing.status = 'finalized'
           AND existing.receipt_group_public_id = NEW.receipt_group_public_id
           AND NEW.status = 'finalized'
       )
)
BEGIN
    SELECT RAISE(
        ABORT,
        'UNIQUE finalization audit identity collision: receipt_finalization_audit rows are append-only and cannot be replaced'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_finalization_idempotency_no_update
BEFORE UPDATE ON receipt_finalization_idempotency
BEGIN
    SELECT RAISE(
        ABORT,
        'receipt_finalization_idempotency rows are immutable replay evidence'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_finalization_idempotency_no_delete
BEFORE DELETE ON receipt_finalization_idempotency
BEGIN
    SELECT RAISE(
        ABORT,
        'receipt_finalization_idempotency rows are immutable replay evidence'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_finalization_idempotency_no_insert_collision
BEFORE INSERT ON receipt_finalization_idempotency
WHEN EXISTS (
    SELECT 1
    FROM receipt_finalization_idempotency AS existing
    WHERE existing.idempotency_key = NEW.idempotency_key
)
BEGIN
    SELECT RAISE(
        ABORT,
        'UNIQUE finalization idempotency identity collision: receipt_finalization_idempotency rows are append-only and cannot be replaced'
    );
END;
