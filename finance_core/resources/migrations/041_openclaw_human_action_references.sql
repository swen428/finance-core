-- S5c durable OpenClaw direct-human action references.
--
-- The opaque Telegram callback value is never stored.  Finance persists only
-- its SHA-256 digest plus the exact proposal, action, actor, conversation,
-- core binding, and expiry it authorizes.  Redemption is a separate
-- append-only row with one-per-reference and one-per-callback uniqueness.

CREATE TABLE openclaw_human_action_references (
    id INTEGER PRIMARY KEY,
    reference_public_id TEXT NOT NULL UNIQUE
        CHECK (
            length(reference_public_id) = 38
            AND reference_public_id GLOB 'haref_[0-9a-f]*'
            AND reference_public_id NOT GLOB 'haref_*[^0-9a-f]*'
        ),
    reference_sha256 TEXT NOT NULL UNIQUE
        CHECK (
            length(reference_sha256) = 64
            AND reference_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    issuance_idempotency_key TEXT NOT NULL
        CHECK (
            length(issuance_idempotency_key) = 58
            AND substr(issuance_idempotency_key, 1, 26) = 'bridge-human-action-issue:'
            AND substr(issuance_idempotency_key, 27) NOT GLOB '*[^0-9a-f]*'
        ),
    parser_output_id INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('confirm', 'edit', 'reject')),
    proposal_version INTEGER NOT NULL CHECK (proposal_version >= 0),
    proposal_content_hash TEXT NOT NULL
        CHECK (
            length(proposal_content_hash) = 64
            AND proposal_content_hash NOT GLOB '*[^0-9a-f]*'
        ),
    authenticated_actor_id TEXT NOT NULL
        CHECK (
            length(authenticated_actor_id) BETWEEN 1 AND 32
            AND authenticated_actor_id NOT GLOB '*[^0-9]*'
            AND authenticated_actor_id NOT LIKE '0%'
        ),
    channel TEXT NOT NULL CHECK (channel = 'telegram'),
    channel_account_id TEXT NOT NULL
        CHECK (
            length(channel_account_id) BETWEEN 1 AND 200
            AND channel_account_id NOT GLOB '*[^!-~]*'
        ),
    channel_conversation_id TEXT NOT NULL
        CHECK (
            length(channel_conversation_id) BETWEEN 1 AND 32
            AND channel_conversation_id NOT GLOB '*[^0-9]*'
            AND channel_conversation_id NOT LIKE '0%'
        ),
    conversation_binding_id TEXT NOT NULL
        CHECK (
            length(conversation_binding_id) BETWEEN 1 AND 200
            AND conversation_binding_id NOT GLOB '*[^!-~]*'
        ),
    ttl_seconds INTEGER NOT NULL CHECK (ttl_seconds BETWEEN 60 AND 3600),
    expires_at INTEGER NOT NULL CHECK (expires_at > 0),
    issued_at TEXT NOT NULL,
    FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id),
    CHECK (authenticated_actor_id = channel_conversation_id),
    UNIQUE (issuance_idempotency_key, action)
);

CREATE INDEX idx_openclaw_human_action_reference_proposal
    ON openclaw_human_action_references(parser_output_id, proposal_version, action);
CREATE INDEX idx_openclaw_human_action_reference_expiry
    ON openclaw_human_action_references(expires_at);

CREATE TABLE openclaw_human_action_redemptions (
    id INTEGER PRIMARY KEY,
    reference_id INTEGER NOT NULL UNIQUE,
    callback_id_sha256 TEXT NOT NULL UNIQUE
        CHECK (
            length(callback_id_sha256) = 64
            AND callback_id_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
    callback_message_id INTEGER NOT NULL CHECK (callback_message_id > 0),
    redeemed_at TEXT NOT NULL,
    FOREIGN KEY (reference_id) REFERENCES openclaw_human_action_references(id)
);

CREATE TRIGGER trg_openclaw_human_action_references_no_update
    BEFORE UPDATE ON openclaw_human_action_references
    FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'OpenClaw human-action references are append-only');
END;

CREATE TRIGGER trg_openclaw_human_action_references_no_delete
    BEFORE DELETE ON openclaw_human_action_references
    FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'OpenClaw human-action references are append-only');
END;

CREATE TRIGGER trg_openclaw_human_action_redemptions_no_update
    BEFORE UPDATE ON openclaw_human_action_redemptions
    FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'OpenClaw human-action redemptions are append-only');
END;

CREATE TRIGGER trg_openclaw_human_action_redemptions_no_delete
    BEFORE DELETE ON openclaw_human_action_redemptions
    FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'OpenClaw human-action redemptions are append-only');
END;
