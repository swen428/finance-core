PRAGMA foreign_keys = ON;

-- Append-only receipt proposal monetary-correction (supersession) evidence.
--
-- One row binds one authenticated human correction command to exactly one
-- superseded receipt total proposal and exactly one replacement child
-- proposal.  The revision is evidence only: it never confirms a proposal,
-- converts it to a transaction, or creates receipt facts, calculations,
-- settlement obligations, or any other final financial state.
--
-- Deterministic replay/conflict contract:
--   * correction_public_id is caller-owned and unique, so the same command
--     replays against the persisted row instead of writing twice;
--   * superseded_parser_output_id is unique, so one proposal can be
--     superseded at most once (chained corrections supersede the child);
--   * replacement_parser_output_id is unique, so each revision has exactly
--     one replacement child and no forked replacement chains.

CREATE TABLE IF NOT EXISTS receipt_proposal_revisions (
    id INTEGER PRIMARY KEY,
    correction_public_id TEXT NOT NULL UNIQUE CHECK (
        length(correction_public_id) BETWEEN 6 AND 200
        AND substr(correction_public_id, 1, 5) = 'rcor_'
        AND correction_public_id NOT GLOB '*[^A-Za-z0-9_-]*'
    ),
    superseded_parser_output_id INTEGER NOT NULL UNIQUE,
    replacement_parser_output_id INTEGER NOT NULL UNIQUE,
    superseded_content_hash TEXT NOT NULL CHECK (
        length(superseded_content_hash) = 64
        AND lower(superseded_content_hash) = superseded_content_hash
        AND superseded_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    replacement_content_hash TEXT NOT NULL CHECK (
        length(replacement_content_hash) = 64
        AND lower(replacement_content_hash) = replacement_content_hash
        AND replacement_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    -- Original parent status at command creation time, persisted so that
    -- idempotent replay returns the deterministic creation-time result
    -- regardless of later lifecycle changes.  Only the statuses that are
    -- eligible for supersession may ever appear here.
    superseded_from_status TEXT NOT NULL CHECK (
        superseded_from_status IN (
            'parsed_pending_confirmation',
            'edited_pending_confirmation',
            'confirmed'
        )
    ),
    -- Canonical caller-supplied command fields (deterministic replay and
    -- conflict identity, echoed no-op values included).
    field_updates_json TEXT NOT NULL CHECK (
        json_valid(field_updates_json) = 1
    ),
    -- Strictly material changes actually applied to the replacement child
    -- (creation-time changed_fields; never includes echoed no-op values).
    applied_field_updates_json TEXT NOT NULL CHECK (
        json_valid(applied_field_updates_json) = 1
    ),
    replacement_payload_json TEXT NOT NULL CHECK (
        json_valid(replacement_payload_json) = 1
    ),
    actor_type TEXT NOT NULL CHECK (actor_type = 'human'),
    authenticated_actor_id TEXT NOT NULL CHECK (length(trim(authenticated_actor_id)) > 0),
    correction_channel TEXT NOT NULL CHECK (length(trim(correction_channel)) > 0),
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (superseded_parser_output_id) REFERENCES parser_outputs(id),
    FOREIGN KEY (replacement_parser_output_id) REFERENCES parser_outputs(id),
    CHECK (superseded_parser_output_id != replacement_parser_output_id),
    CHECK (superseded_content_hash != replacement_content_hash)
);

CREATE INDEX IF NOT EXISTS idx_receipt_proposal_revisions_superseded
    ON receipt_proposal_revisions(superseded_parser_output_id);
CREATE INDEX IF NOT EXISTS idx_receipt_proposal_revisions_replacement
    ON receipt_proposal_revisions(replacement_parser_output_id);
CREATE INDEX IF NOT EXISTS idx_receipt_proposal_revisions_superseded_hash
    ON receipt_proposal_revisions(superseded_content_hash);

-- -----------------------------------------------------------------------
-- Append-only enforcement
-- -----------------------------------------------------------------------

CREATE TRIGGER IF NOT EXISTS trg_receipt_proposal_revisions_no_update
    BEFORE UPDATE ON receipt_proposal_revisions
BEGIN
    SELECT RAISE(ABORT, 'receipt_proposal_revisions rows are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_proposal_revisions_no_delete
    BEFORE DELETE ON receipt_proposal_revisions
BEGIN
    SELECT RAISE(ABORT, 'receipt_proposal_revisions rows are append-only');
END;
