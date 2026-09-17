-- Durable Telegram guided-edit state for the isolated Finance bridge.
--
-- Sessions are operational authorization state rooted in one redeemed S5c
-- edit reference. Financial edits remain in the existing append-only
-- completion / receipt-supersession records. Session events preserve the
-- acknowledgement and recovery trail; no callback token or signing key is
-- persisted here.

CREATE TABLE openclaw_guided_edit_sessions (
    id INTEGER PRIMARY KEY,
    session_public_id TEXT NOT NULL UNIQUE
        CHECK (
            length(session_public_id) = 38
            AND session_public_id GLOB 'gedit_[0-9a-f]*'
            AND session_public_id NOT GLOB 'gedit_*[^0-9a-f]*'
        ),
    source_reference_id INTEGER NOT NULL UNIQUE,
    current_parser_output_id INTEGER NOT NULL,
    current_proposal_version INTEGER NOT NULL CHECK (current_proposal_version >= 0),
    current_content_hash TEXT NOT NULL
        CHECK (
            length(current_content_hash) = 64
            AND current_content_hash NOT GLOB '*[^0-9a-f]*'
        ),
    authenticated_actor_id TEXT NOT NULL,
    channel_account_id TEXT NOT NULL,
    channel_conversation_id TEXT NOT NULL,
    conversation_binding_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'completed', 'superseded', 'abandoned')),
    expires_at INTEGER NOT NULL CHECK (expires_at > 0),
    last_claimed_message_id INTEGER NOT NULL CHECK (last_claimed_message_id > 0),
    pending_message_id INTEGER CHECK (pending_message_id > 0),
    pending_operation_key TEXT,
    pending_field_name TEXT CHECK (
        pending_field_name IS NULL OR pending_field_name IN (
            'amount', 'currency', 'transaction_date', 'merchant', 'description', 'category'
        )
    ),
    pending_field_value_json TEXT,
    completed_message_id INTEGER CHECK (completed_message_id > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_reference_id) REFERENCES openclaw_human_action_references(id),
    FOREIGN KEY (current_parser_output_id) REFERENCES parser_outputs(id),
    CHECK (
        (pending_message_id IS NULL AND pending_operation_key IS NULL
            AND pending_field_name IS NULL AND pending_field_value_json IS NULL)
        OR
        (pending_message_id IS NOT NULL AND pending_operation_key IS NOT NULL
            AND pending_field_name IS NOT NULL AND pending_field_value_json IS NOT NULL)
    ),
    CHECK (pending_message_id IS NULL OR pending_message_id = last_claimed_message_id),
    CHECK (status = 'active' OR pending_message_id IS NULL),
    CHECK (status = 'completed' OR completed_message_id IS NULL),
    CHECK (status != 'completed' OR completed_message_id = last_claimed_message_id)
);

CREATE UNIQUE INDEX idx_openclaw_guided_edit_active_context
    ON openclaw_guided_edit_sessions(
        channel_account_id, channel_conversation_id, conversation_binding_id
    )
    WHERE status = 'active';

CREATE INDEX idx_openclaw_guided_edit_current_proposal
    ON openclaw_guided_edit_sessions(current_parser_output_id, status);

-- Completion reply recovery may outlive the short-lived Telegram action
-- references.  Each append-only generation binds one completed guided-edit
-- session to one issuance batch.  A later generation is created when the
-- previous batch is consumed, expired, or near expiry; concurrent recovery attempts
-- converge on the same currently usable batch.
CREATE TABLE openclaw_guided_edit_review_generations (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    generation INTEGER NOT NULL CHECK (generation > 0),
    reference_batch_id TEXT NOT NULL UNIQUE
        CHECK (
            length(reference_batch_id) = 32
            AND reference_batch_id NOT GLOB '*[^0-9a-f]*'
        ),
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES openclaw_guided_edit_sessions(id),
    UNIQUE (session_id, generation)
);

CREATE TABLE openclaw_guided_edit_events (
    id INTEGER PRIMARY KEY,
    event_public_id TEXT NOT NULL UNIQUE
        CHECK (
            length(event_public_id) = 40
            AND event_public_id GLOB 'geditev_[0-9a-f]*'
            AND event_public_id NOT GLOB 'geditev_*[^0-9a-f]*'
        ),
    session_id INTEGER NOT NULL,
    sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'started', 'update_requested', 'update_applied', 'update_refused',
            'completed', 'superseded', 'abandoned'
        )
    ),
    telegram_message_id INTEGER CHECK (telegram_message_id > 0),
    operation_key TEXT,
    field_name TEXT,
    field_value_json TEXT,
    before_parser_output_id INTEGER,
    before_proposal_version INTEGER CHECK (
        before_proposal_version IS NULL OR before_proposal_version >= 0
    ),
    before_content_hash TEXT CHECK (
        before_content_hash IS NULL OR (
            length(before_content_hash) = 64
            AND before_content_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    after_parser_output_id INTEGER,
    after_proposal_version INTEGER CHECK (
        after_proposal_version IS NULL OR after_proposal_version >= 0
    ),
    after_content_hash TEXT CHECK (
        after_content_hash IS NULL OR (
            length(after_content_hash) = 64
            AND after_content_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    refusal_code TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES openclaw_guided_edit_sessions(id),
    FOREIGN KEY (before_parser_output_id) REFERENCES parser_outputs(id),
    FOREIGN KEY (after_parser_output_id) REFERENCES parser_outputs(id),
    UNIQUE (session_id, sequence_number)
);

CREATE INDEX idx_openclaw_guided_edit_event_message
    ON openclaw_guided_edit_events(session_id, telegram_message_id, event_type);

CREATE UNIQUE INDEX idx_openclaw_guided_edit_message_claim
    ON openclaw_guided_edit_events(session_id, telegram_message_id)
    WHERE event_type IN ('update_requested', 'completed');

CREATE TRIGGER trg_openclaw_guided_edit_sessions_no_delete
    BEFORE DELETE ON openclaw_guided_edit_sessions
    FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'OpenClaw guided-edit sessions cannot be deleted');
END;

CREATE TRIGGER trg_openclaw_guided_edit_sessions_identity_immutable
    BEFORE UPDATE ON openclaw_guided_edit_sessions
    FOR EACH ROW
    WHEN NEW.session_public_id != OLD.session_public_id
      OR NEW.source_reference_id != OLD.source_reference_id
      OR NEW.authenticated_actor_id != OLD.authenticated_actor_id
      OR NEW.channel_account_id != OLD.channel_account_id
      OR NEW.channel_conversation_id != OLD.channel_conversation_id
      OR NEW.conversation_binding_id != OLD.conversation_binding_id
      OR NEW.expires_at != OLD.expires_at
      OR NEW.created_at != OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'OpenClaw guided-edit session identity is immutable');
END;

CREATE TRIGGER trg_openclaw_guided_edit_sessions_terminal_immutable
    BEFORE UPDATE ON openclaw_guided_edit_sessions
    FOR EACH ROW
    WHEN OLD.status != 'active'
BEGIN
    SELECT RAISE(ABORT, 'OpenClaw guided-edit terminal session is immutable');
END;

CREATE TRIGGER trg_openclaw_guided_edit_sessions_message_high_water
    BEFORE UPDATE ON openclaw_guided_edit_sessions
    FOR EACH ROW
    WHEN NEW.last_claimed_message_id < OLD.last_claimed_message_id
BEGIN
    SELECT RAISE(ABORT, 'OpenClaw guided-edit message high-water cannot decrease');
END;

CREATE TRIGGER trg_openclaw_guided_edit_events_no_update
    BEFORE UPDATE ON openclaw_guided_edit_events
    FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'OpenClaw guided-edit events are append-only');
END;

CREATE TRIGGER trg_openclaw_guided_edit_events_no_delete
    BEFORE DELETE ON openclaw_guided_edit_events
    FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'OpenClaw guided-edit events are append-only');
END;

CREATE TRIGGER trg_openclaw_guided_edit_review_generations_no_update
    BEFORE UPDATE ON openclaw_guided_edit_review_generations
    FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'OpenClaw guided-edit review generations are append-only');
END;

CREATE TRIGGER trg_openclaw_guided_edit_review_generations_no_delete
    BEFORE DELETE ON openclaw_guided_edit_review_generations
    FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'OpenClaw guided-edit review generations are append-only');
END;
