-- Migration 039: receipt-scoped finalization membership evidence
-- (PR #249 acceptance closure, R3-07-1)
--
-- PR #249 (merge commit 936eb1b) resolved finalization membership with a
-- group-wide "most included wins" collapse keyed by participant public ID
-- alone and deferred the receipt-scoped membership contract as R3-07-1.
-- Participant public ID alone is an insufficient durable identity: the same
-- participant may be included on receipt A and excluded on receipt B of the
-- same group, carry different roles per receipt, or be a valid payer on only
-- one receipt.  Nothing durable recorded WHICH receipt's membership
-- authorized a finalization, so a replay could not detect membership rows
-- that were flipped, retargeted to a foreign receipt, deleted, or duplicated
-- after the finalization committed.
--
-- Scope of the evidence: one row per participant the finalization actually
-- referenced (payer, obligation counterparties, share holders).  Membership
-- rows for participants the finalization never referenced carry no money and
-- are deliberately outside this evidence set.
--
-- This migration adds the durable receipt-scoped membership evidence the
-- closed contract requires:
--   * one append-only row per (finalization, participant) recording the
--     exact receipt identity, role, and inclusion the finalizer verified and
--     consumed;
--   * membership_scope discriminates the IAF path ('receipt': membership is
--     authoritative on exactly the snapshot-bound receipt) from the legacy
--     group-scoped path ('receipt_group': no receipt in the group
--     contradicted the consumed membership identity, so no single receipt
--     owns it);
--   * real foreign keys to receipt_finalization_audit, receipt_groups,
--     receipts, and participants;
--   * explicit uniqueness: one membership decision per participant per
--     finalization, with the receipt dimension recorded in the row;
--   * append-only no-update / no-delete / no-insert-collision triggers, the
--     collision guard being required because SQLite performs
--     `INSERT OR REPLACE`'s implicit row removal without firing DELETE
--     triggers while `PRAGMA recursive_triggers` is OFF (the default).
--
-- Deliberately additive and fail-closed:
--   * migrations 001-038 are untouched;
--   * no backfill: pre-039 finalizations keep no membership evidence, and
--     absence means "finalized before this contract existed", never "the
--     membership is unknown".  Ambiguous historical membership is NOT
--     silently reinterpreted; the replay verifier falls back to live
--     receipt-scoped verification for those rows;
--   * CREATE statements intentionally omit IF NOT EXISTS so a pre-existing
--     squatter object with the same name fails this migration closed inside
--     the runner's per-migration transaction instead of being adopted.

CREATE TABLE receipt_finalization_membership_evidence (
    membership_evidence_public_id TEXT PRIMARY KEY NOT NULL CHECK (
        substr(membership_evidence_public_id, 1, 5) = 'rfme_'
        AND length(membership_evidence_public_id) BETWEEN 6 AND 200
        AND membership_evidence_public_id NOT GLOB '*[^A-Za-z0-9_-]*'
    ),
    finalization_id TEXT NOT NULL
        REFERENCES receipt_finalization_audit(finalization_id),
    receipt_group_public_id TEXT NOT NULL
        REFERENCES receipt_groups(public_id),
    membership_scope TEXT NOT NULL CHECK (
        membership_scope IN ('receipt', 'receipt_group')
    ),
    receipt_public_id TEXT
        REFERENCES receipts(public_id)
        CHECK (
            (membership_scope = 'receipt' AND receipt_public_id IS NOT NULL)
            OR (membership_scope = 'receipt_group' AND receipt_public_id IS NULL)
        ),
    participant_public_id TEXT NOT NULL
        REFERENCES participants(public_id),
    role TEXT NOT NULL CHECK (
        role IN ('payer', 'participant', 'excluded', 'observer')
    ),
    is_included INTEGER NOT NULL CHECK (is_included IN (0, 1)),
    created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
    UNIQUE (finalization_id, participant_public_id)
);

CREATE INDEX idx_rfme_finalization_id
    ON receipt_finalization_membership_evidence(finalization_id);

CREATE INDEX idx_rfme_receipt_public_id
    ON receipt_finalization_membership_evidence(receipt_public_id)
    WHERE receipt_public_id IS NOT NULL;

CREATE TRIGGER trg_rfme_no_update
BEFORE UPDATE ON receipt_finalization_membership_evidence
BEGIN
    SELECT RAISE(
        ABORT,
        'receipt_finalization_membership_evidence rows are immutable finalization evidence'
    );
END;

CREATE TRIGGER trg_rfme_no_delete
BEFORE DELETE ON receipt_finalization_membership_evidence
BEGIN
    SELECT RAISE(
        ABORT,
        'receipt_finalization_membership_evidence rows are immutable finalization evidence'
    );
END;

CREATE TRIGGER trg_rfme_no_insert_collision
BEFORE INSERT ON receipt_finalization_membership_evidence
WHEN EXISTS (
    SELECT 1
    FROM receipt_finalization_membership_evidence AS existing
    WHERE existing.membership_evidence_public_id = NEW.membership_evidence_public_id
       OR (
           existing.finalization_id = NEW.finalization_id
           AND existing.participant_public_id = NEW.participant_public_id
       )
)
BEGIN
    SELECT RAISE(
        ABORT,
        'UNIQUE membership evidence identity collision: receipt_finalization_membership_evidence rows are append-only and cannot be replaced'
    );
END;
