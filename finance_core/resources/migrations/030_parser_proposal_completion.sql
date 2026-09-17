PRAGMA foreign_keys = ON;

-- Authenticated human parser-proposal completion boundary.
--
-- This table stores append-only completion versions created by
-- authenticated human actors.  The original parser_outputs.parsed_payload
-- and raw_intake_records.raw_input remain immutable source evidence.
-- Completion versions are versioned per proposal and cannot be updated
-- or deleted.
--
-- Effective proposal payload resolution:
--   original immutable parser payload
--   + latest valid append-only authenticated completion version
--   = effective proposal payload

CREATE TABLE IF NOT EXISTS parser_proposal_completions (
    id INTEGER PRIMARY KEY,
    completion_public_id TEXT NOT NULL CHECK (length(trim(completion_public_id)) > 0),
    parser_output_id INTEGER NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    base_content_hash TEXT NOT NULL CHECK (
        length(base_content_hash) = 64
        AND lower(base_content_hash) = base_content_hash
        AND base_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    completed_content_hash TEXT NOT NULL CHECK (
        length(completed_content_hash) = 64
        AND lower(completed_content_hash) = completed_content_hash
        AND completed_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    completed_payload_json TEXT NOT NULL CHECK (
        json_valid(completed_payload_json) = 1
    ),
    field_updates_json TEXT NOT NULL CHECK (
        json_valid(field_updates_json) = 1
    ),
    actor_type TEXT NOT NULL CHECK (actor_type = 'human'),
    authenticated_actor_id TEXT NOT NULL CHECK (length(trim(authenticated_actor_id)) > 0),
    completion_channel TEXT NOT NULL CHECK (length(trim(completion_channel)) > 0),
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id),
    UNIQUE (completion_public_id),
    UNIQUE (parser_output_id, version_number)
);

CREATE INDEX IF NOT EXISTS idx_parser_proposal_completions_output
    ON parser_proposal_completions(parser_output_id, version_number);
CREATE INDEX IF NOT EXISTS idx_parser_proposal_completions_hash
    ON parser_proposal_completions(completed_content_hash);

-- Append-only enforcement: completion rows cannot be updated or deleted.
CREATE TRIGGER IF NOT EXISTS trg_parser_proposal_completions_no_update
BEFORE UPDATE ON parser_proposal_completions
BEGIN
    SELECT RAISE(ABORT, 'parser proposal completions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_parser_proposal_completions_no_delete
BEFORE DELETE ON parser_proposal_completions
BEGIN
    SELECT RAISE(ABORT, 'parser proposal completions are append-only');
END;

-- Completed content hash must differ from base hash (material change required).
CREATE TRIGGER IF NOT EXISTS trg_parser_proposal_completions_material_change
BEFORE INSERT ON parser_proposal_completions
WHEN NEW.base_content_hash = NEW.completed_content_hash
BEGIN
    SELECT RAISE(ABORT, 'parser proposal completion must produce a material content change');
END;

-- Add edited event type to parser_proposal_events if not already supported.
-- Migration 004 already allows 'edited' in the event_type CHECK constraint,
-- so no ALTER is needed for the event table itself.  We only need the
-- lifecycle status 'edited_pending_confirmation' which is already present.
