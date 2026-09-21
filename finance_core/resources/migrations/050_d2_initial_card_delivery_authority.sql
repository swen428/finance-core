PRAGMA foreign_keys = OFF;

-- D2 initial-card and terminal-delivery authority correction.
--
-- Migration 049 assumed every D2 review came from a published D1 human card.
-- A complete first proposal does not have that evidence.  This migration adds
-- a distinct immutable initial-card source, closed action purposes, and a
-- terminal-delivery activation gate without changing any accepted 049
-- decision, attempt, authorization, or transaction identity.

CREATE TABLE d2_telegram_source_contexts (
    raw_intake_record_id INTEGER PRIMARY KEY,
    authenticated_actor_id TEXT NOT NULL CHECK (length(trim(authenticated_actor_id)) > 0),
    telegram_account_id TEXT NOT NULL CHECK (length(trim(telegram_account_id)) > 0),
    telegram_conversation_id TEXT NOT NULL CHECK (length(trim(telegram_conversation_id)) > 0),
    conversation_binding_id TEXT NOT NULL CHECK (length(trim(conversation_binding_id)) > 0),
    source_message_id TEXT NOT NULL CHECK (length(trim(source_message_id)) > 0),
    source_identity_sha256 TEXT NOT NULL UNIQUE CHECK (
        length(source_identity_sha256) = 64
        AND source_identity_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    captured_at TEXT NOT NULL,
    FOREIGN KEY (raw_intake_record_id) REFERENCES raw_intake_records(id),
    UNIQUE (telegram_account_id, telegram_conversation_id, source_message_id),
    UNIQUE (
        raw_intake_record_id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id, source_message_id,
        source_identity_sha256
    )
) STRICT;

CREATE TRIGGER trg_d2_telegram_source_contexts_no_update
BEFORE UPDATE ON d2_telegram_source_contexts BEGIN
    SELECT RAISE(ABORT, 'D2 Telegram source contexts are append-only');
END;
CREATE TRIGGER trg_d2_telegram_source_contexts_no_insert_collision
BEFORE INSERT ON d2_telegram_source_contexts
WHEN EXISTS (
    SELECT 1 FROM d2_telegram_source_contexts AS existing
    WHERE existing.raw_intake_record_id = NEW.raw_intake_record_id
       OR existing.source_identity_sha256 = NEW.source_identity_sha256
       OR (existing.telegram_account_id = NEW.telegram_account_id
           AND existing.telegram_conversation_id = NEW.telegram_conversation_id
           AND existing.source_message_id = NEW.source_message_id)
) BEGIN
    SELECT RAISE(ABORT, 'D2 Telegram source context identity collision');
END;
CREATE TRIGGER trg_d2_telegram_source_contexts_no_delete
BEFORE DELETE ON d2_telegram_source_contexts BEGIN
    SELECT RAISE(ABORT, 'D2 Telegram source contexts are append-only');
END;

CREATE TABLE d2_initial_proposal_cards (
    initial_card_public_id TEXT PRIMARY KEY CHECK (
        length(initial_card_public_id) = 39
        AND initial_card_public_id GLOB 'd2card_[0-9a-f]*'
        AND initial_card_public_id NOT GLOB 'd2card_*[^0-9a-f]*'
    ),
    card_idempotency_key TEXT NOT NULL UNIQUE CHECK (length(trim(card_idempotency_key)) > 0),
    parser_output_id INTEGER NOT NULL,
    proposal_version INTEGER NOT NULL CHECK (proposal_version >= 0),
    proposal_content_hash TEXT NOT NULL CHECK (
        length(proposal_content_hash) = 64
        AND proposal_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    raw_intake_record_id INTEGER NOT NULL,
    admitted_source_message_id TEXT NOT NULL CHECK (length(trim(admitted_source_message_id)) > 0),
    admitted_source_identity_sha256 TEXT NOT NULL CHECK (
        length(admitted_source_identity_sha256) = 64
        AND admitted_source_identity_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    authenticated_actor_id TEXT NOT NULL CHECK (length(trim(authenticated_actor_id)) > 0),
    telegram_account_id TEXT NOT NULL CHECK (length(trim(telegram_account_id)) > 0),
    telegram_conversation_id TEXT NOT NULL CHECK (length(trim(telegram_conversation_id)) > 0),
    conversation_binding_id TEXT NOT NULL CHECK (length(trim(conversation_binding_id)) > 0),
    visible_projection_json TEXT NOT NULL CHECK (json_valid(visible_projection_json) = 1),
    visible_projection_hash TEXT NOT NULL CHECK (
        length(visible_projection_hash) = 64
        AND visible_projection_hash NOT GLOB '*[^0-9a-f]*'
    ),
    presentation_text TEXT NOT NULL CHECK (length(presentation_text) > 0),
    presentation_text_hash TEXT NOT NULL CHECK (
        length(presentation_text_hash) = 64
        AND presentation_text_hash NOT GLOB '*[^0-9a-f]*'
    ),
    expires_at INTEGER NOT NULL CHECK (expires_at > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id),
    FOREIGN KEY (raw_intake_record_id) REFERENCES raw_intake_records(id),
    FOREIGN KEY (
        raw_intake_record_id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id,
        admitted_source_message_id, admitted_source_identity_sha256
    ) REFERENCES d2_telegram_source_contexts (
        raw_intake_record_id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id,
        source_message_id, source_identity_sha256
    ),
    UNIQUE (
        parser_output_id, proposal_version, proposal_content_hash,
        authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    )
) STRICT;

CREATE TRIGGER trg_d2_initial_cards_no_update
BEFORE UPDATE ON d2_initial_proposal_cards BEGIN
    SELECT RAISE(ABORT, 'D2 initial proposal cards are append-only');
END;
CREATE TRIGGER trg_d2_initial_cards_no_insert_collision
BEFORE INSERT ON d2_initial_proposal_cards
WHEN EXISTS (
    SELECT 1 FROM d2_initial_proposal_cards AS existing
    WHERE existing.initial_card_public_id = NEW.initial_card_public_id
       OR existing.card_idempotency_key = NEW.card_idempotency_key
       OR (
            existing.parser_output_id = NEW.parser_output_id
            AND existing.proposal_version = NEW.proposal_version
            AND existing.proposal_content_hash = NEW.proposal_content_hash
            AND existing.authenticated_actor_id = NEW.authenticated_actor_id
            AND existing.telegram_account_id = NEW.telegram_account_id
            AND existing.telegram_conversation_id = NEW.telegram_conversation_id
            AND existing.conversation_binding_id = NEW.conversation_binding_id
       )
) BEGIN
    SELECT RAISE(ABORT, 'D2 initial proposal card identity collision');
END;
CREATE TRIGGER trg_d2_initial_cards_no_delete
BEFORE DELETE ON d2_initial_proposal_cards BEGIN
    SELECT RAISE(ABORT, 'D2 initial proposal cards are append-only');
END;

-- Rebuild only the review source envelope.  All 049 rows are copied exactly
-- and classified as genuine D1-card reviews.  Dependent accepted chains retain
-- the same review public IDs and therefore the same financial authority.
CREATE TABLE d2_posting_reviews_v2 (
    review_public_id TEXT PRIMARY KEY CHECK (
        length(review_public_id) = 36
        AND review_public_id GLOB 'd2rev_[0-9a-f]*'
        AND review_public_id NOT GLOB 'd2rev_*[^0-9a-f]*'
    ),
    review_idempotency_key TEXT NOT NULL UNIQUE CHECK (length(trim(review_idempotency_key)) > 0),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('d1_human_card', 'initial_proposal_card')),
    source_generation INTEGER NOT NULL CHECK (source_generation > 0),
    card_generation_public_id TEXT,
    initial_card_public_id TEXT,
    predecessor_review_public_id TEXT UNIQUE,
    parser_output_id INTEGER NOT NULL,
    proposal_version INTEGER NOT NULL CHECK (proposal_version >= 0),
    proposal_content_hash TEXT NOT NULL CHECK (
        length(proposal_content_hash) = 64
        AND proposal_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    posting_path TEXT NOT NULL CHECK (posting_path IN ('text', 'personal_receipt')),
    authenticated_actor_id TEXT NOT NULL CHECK (length(trim(authenticated_actor_id)) > 0),
    telegram_account_id TEXT NOT NULL CHECK (length(trim(telegram_account_id)) > 0),
    telegram_conversation_id TEXT NOT NULL CHECK (length(trim(telegram_conversation_id)) > 0),
    conversation_binding_id TEXT NOT NULL CHECK (length(trim(conversation_binding_id)) > 0),
    visible_projection_json TEXT NOT NULL CHECK (json_valid(visible_projection_json) = 1),
    visible_projection_hash TEXT NOT NULL CHECK (
        length(visible_projection_hash) = 64
        AND visible_projection_hash NOT GLOB '*[^0-9a-f]*'
    ),
    receipt_fact_candidate_json TEXT CHECK (
        receipt_fact_candidate_json IS NULL OR json_valid(receipt_fact_candidate_json) = 1
    ),
    expires_at INTEGER NOT NULL CHECK (expires_at > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (card_generation_public_id)
        REFERENCES parser_human_draft_cards(card_generation_public_id),
    FOREIGN KEY (initial_card_public_id)
        REFERENCES d2_initial_proposal_cards(initial_card_public_id),
    FOREIGN KEY (predecessor_review_public_id)
        REFERENCES d2_posting_reviews_v2(review_public_id),
    FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id),
    CHECK (
        (source_kind = 'd1_human_card'
            AND card_generation_public_id IS NOT NULL
            AND initial_card_public_id IS NULL)
        OR
        (source_kind = 'initial_proposal_card'
            AND card_generation_public_id IS NULL
            AND initial_card_public_id IS NOT NULL)
    ),
    CHECK (
        (posting_path = 'text' AND receipt_fact_candidate_json IS NULL)
        OR (posting_path = 'personal_receipt' AND receipt_fact_candidate_json IS NOT NULL)
    ),
    UNIQUE (card_generation_public_id, source_generation),
    UNIQUE (initial_card_public_id, source_generation),
    UNIQUE (
        review_public_id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    )
) STRICT;

INSERT INTO d2_posting_reviews_v2 (
    review_public_id, review_idempotency_key, source_kind, source_generation,
    card_generation_public_id, initial_card_public_id, predecessor_review_public_id,
    parser_output_id, proposal_version, proposal_content_hash, posting_path,
    authenticated_actor_id, telegram_account_id, telegram_conversation_id,
    conversation_binding_id, visible_projection_json, visible_projection_hash,
    receipt_fact_candidate_json, expires_at, created_at
)
SELECT
    review_public_id, review_idempotency_key, 'd1_human_card', 1,
    card_generation_public_id, NULL, NULL,
    parser_output_id, proposal_version, proposal_content_hash, posting_path,
    authenticated_actor_id, telegram_account_id, telegram_conversation_id,
    conversation_binding_id, visible_projection_json, visible_projection_hash,
    receipt_fact_candidate_json, expires_at, created_at
FROM d2_posting_reviews;

DROP TABLE d2_posting_reviews;
ALTER TABLE d2_posting_reviews_v2 RENAME TO d2_posting_reviews;

CREATE TRIGGER trg_d2_posting_reviews_no_update
BEFORE UPDATE ON d2_posting_reviews BEGIN
    SELECT RAISE(ABORT, 'D2 posting reviews are append-only');
END;
CREATE TRIGGER trg_d2_posting_reviews_no_insert_collision
BEFORE INSERT ON d2_posting_reviews
WHEN EXISTS (
    SELECT 1 FROM d2_posting_reviews AS existing
    WHERE existing.review_public_id = NEW.review_public_id
       OR existing.review_idempotency_key = NEW.review_idempotency_key
       OR (NEW.card_generation_public_id IS NOT NULL
           AND existing.card_generation_public_id = NEW.card_generation_public_id
           AND existing.source_generation = NEW.source_generation)
       OR (NEW.initial_card_public_id IS NOT NULL
           AND existing.initial_card_public_id = NEW.initial_card_public_id
           AND existing.source_generation = NEW.source_generation)
       OR (NEW.predecessor_review_public_id IS NOT NULL
           AND existing.predecessor_review_public_id = NEW.predecessor_review_public_id)
) BEGIN
    SELECT RAISE(ABORT, 'D2 posting review identity collision');
END;
CREATE TRIGGER trg_d2_posting_reviews_no_delete
BEFORE DELETE ON d2_posting_reviews BEGIN
    SELECT RAISE(ABORT, 'D2 posting reviews are append-only');
END;

CREATE TABLE openclaw_human_action_reference_purposes (
    reference_id INTEGER PRIMARY KEY,
    purpose TEXT NOT NULL CHECK (purpose IN (
        'd2_post_v1', 'd2_post_accepted_pre050_v1',
        'd2_post_fenced_pre050_v1', 'edit_v1', 'reject_v1',
        'manual_s5d_v1', 'legacy_pre050_v1'
    )),
    classified_at TEXT NOT NULL,
    FOREIGN KEY (reference_id) REFERENCES openclaw_human_action_references(id),
    UNIQUE (reference_id, purpose)
) STRICT;

-- A consumed migration-049 D2 reference is historical recovery authority only.
-- Any unconsumed 049 D2 reference is permanently fenced because no terminal
-- delivery digest was captured before the message left Finance.
INSERT INTO openclaw_human_action_reference_purposes (
    reference_id, purpose, classified_at
)
SELECT
    refs.id,
    CASE
        WHEN bindings.reference_id IS NOT NULL AND redemptions.reference_id IS NOT NULL
            THEN 'd2_post_accepted_pre050_v1'
        WHEN bindings.reference_id IS NOT NULL
            THEN 'd2_post_fenced_pre050_v1'
        WHEN refs.action = 'confirm'
             AND d1_bindings.reference_id IS NULL
             AND redemptions.reference_id IS NULL
            THEN 'd2_post_fenced_pre050_v1'
        WHEN refs.action = 'edit' THEN 'edit_v1'
        WHEN refs.action = 'reject' THEN 'reject_v1'
        ELSE 'legacy_pre050_v1'
    END,
    CURRENT_TIMESTAMP
FROM openclaw_human_action_references AS refs
LEFT JOIN d2_posting_review_action_bindings AS bindings
  ON bindings.reference_id = refs.id
LEFT JOIN openclaw_human_action_redemptions AS redemptions
  ON redemptions.reference_id = refs.id
LEFT JOIN parser_human_draft_action_bindings AS d1_bindings
  ON d1_bindings.reference_id = refs.id;

-- Abort the migration instead of guessing if an already redeemed 049 D2
-- reference lacks any part of its accepted decision/attempt chain.
CREATE TABLE d2_migration_050_guard (value INTEGER NOT NULL CHECK (value = 0)) STRICT;
INSERT INTO d2_migration_050_guard(value)
SELECT 1
WHERE EXISTS (
    SELECT 1
    FROM d2_posting_review_action_bindings AS bindings
    JOIN openclaw_human_action_redemptions AS redemptions
      ON redemptions.reference_id = bindings.reference_id
    LEFT JOIN d2_posting_decisions AS decisions
      ON decisions.reference_id = bindings.reference_id
     AND decisions.review_public_id = bindings.review_public_id
    LEFT JOIN d2_posting_attempts AS attempts
      ON attempts.reference_id = bindings.reference_id
     AND attempts.review_public_id = bindings.review_public_id
    WHERE decisions.reference_id IS NULL
       OR attempts.reference_id IS NULL
       OR decisions.attempt_public_id != attempts.attempt_public_id
);
DROP TABLE d2_migration_050_guard;

CREATE TRIGGER trg_human_action_purposes_no_update
BEFORE UPDATE ON openclaw_human_action_reference_purposes BEGIN
    SELECT RAISE(ABORT, 'human-action purposes are append-only');
END;
CREATE TRIGGER trg_human_action_purposes_no_insert_collision
BEFORE INSERT ON openclaw_human_action_reference_purposes
WHEN EXISTS (
    SELECT 1 FROM openclaw_human_action_reference_purposes AS existing
    WHERE existing.reference_id = NEW.reference_id
) BEGIN
    SELECT RAISE(ABORT, 'human-action purpose identity collision');
END;
CREATE TRIGGER trg_human_action_purposes_no_delete
BEFORE DELETE ON openclaw_human_action_reference_purposes BEGIN
    SELECT RAISE(ABORT, 'human-action purposes are append-only');
END;

CREATE TABLE d2_posting_review_controls (
    review_public_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('confirm', 'edit', 'reject')),
    reference_id INTEGER NOT NULL UNIQUE,
    purpose TEXT NOT NULL CHECK (
        (action = 'confirm' AND purpose = 'd2_post_v1')
        OR (action = 'edit' AND purpose = 'edit_v1')
        OR (action = 'reject' AND purpose = 'reject_v1')
    ),
    row_index INTEGER NOT NULL CHECK (row_index IN (0, 1)),
    column_index INTEGER NOT NULL CHECK (column_index IN (0, 1)),
    label TEXT NOT NULL CHECK (label IN ('Confirm', 'Edit', 'Reject')),
    callback_route TEXT NOT NULL CHECK (callback_route IN ('post:', 'edit:', 'reject:')),
    callback_value_sha256 TEXT NOT NULL CHECK (
        length(callback_value_sha256) = 64
        AND callback_value_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    PRIMARY KEY (review_public_id, action),
    UNIQUE (review_public_id, row_index, column_index),
    FOREIGN KEY (review_public_id) REFERENCES d2_posting_reviews(review_public_id),
    FOREIGN KEY (reference_id) REFERENCES openclaw_human_action_references(id),
    FOREIGN KEY (reference_id, purpose)
        REFERENCES openclaw_human_action_reference_purposes(reference_id, purpose),
    CHECK (
        (action = 'confirm' AND row_index = 0 AND column_index = 0
            AND label = 'Confirm' AND callback_route = 'post:')
        OR (action = 'edit' AND row_index = 1 AND column_index = 0
            AND label = 'Edit' AND callback_route = 'edit:')
        OR (action = 'reject' AND row_index = 1 AND column_index = 1
            AND label = 'Reject' AND callback_route = 'reject:')
    )
) STRICT;

CREATE TABLE d2_posting_review_delivery_attempts (
    delivery_attempt_public_id TEXT PRIMARY KEY CHECK (
        length(delivery_attempt_public_id) = 39
        AND delivery_attempt_public_id GLOB 'd2send_[0-9a-f]*'
        AND delivery_attempt_public_id NOT GLOB 'd2send_*[^0-9a-f]*'
    ),
    review_public_id TEXT NOT NULL UNIQUE,
    manifest_version TEXT NOT NULL CHECK (manifest_version = 'finance_d2_controls_v1'),
    presentation_text TEXT NOT NULL CHECK (length(presentation_text) > 0),
    finance_delivery_material_sha256 TEXT NOT NULL CHECK (
        length(finance_delivery_material_sha256) = 64
        AND finance_delivery_material_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    attempt_nonce_sha256 TEXT NOT NULL UNIQUE CHECK (
        length(attempt_nonce_sha256) = 64
        AND attempt_nonce_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    authenticated_actor_id TEXT NOT NULL,
    telegram_account_id TEXT NOT NULL,
    telegram_conversation_id TEXT NOT NULL,
    conversation_binding_id TEXT NOT NULL,
    attempted_at INTEGER NOT NULL CHECK (attempted_at > 0),
    FOREIGN KEY (review_public_id) REFERENCES d2_posting_reviews(review_public_id),
    FOREIGN KEY (
        review_public_id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ) REFERENCES d2_posting_reviews (
        review_public_id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ),
    UNIQUE (
        delivery_attempt_public_id, review_public_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    )
) STRICT;

CREATE TABLE d2_posting_review_delivery_observations (
    observation_public_id TEXT PRIMARY KEY CHECK (
        length(observation_public_id) = 39
        AND observation_public_id GLOB 'd2dobs_[0-9a-f]*'
        AND observation_public_id NOT GLOB 'd2dobs_*[^0-9a-f]*'
    ),
    delivery_attempt_public_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure', 'unknown', 'conflict')),
    provider_message_id INTEGER CHECK (provider_message_id IS NULL OR provider_message_id > 0),
    finance_delivery_material_sha256 TEXT NOT NULL CHECK (
        length(finance_delivery_material_sha256) = 64
        AND finance_delivery_material_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    receipt_token_sha256 TEXT NOT NULL UNIQUE CHECK (
        length(receipt_token_sha256) = 64
        AND receipt_token_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    source_identity_sha256 TEXT NOT NULL CHECK (
        length(source_identity_sha256) = 64
        AND source_identity_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    channel TEXT NOT NULL CHECK (channel = 'telegram'),
    telegram_account_id TEXT NOT NULL,
    telegram_conversation_id TEXT NOT NULL,
    conversation_binding_id TEXT NOT NULL,
    error_code TEXT,
    observed_at INTEGER NOT NULL CHECK (observed_at > 0),
    FOREIGN KEY (delivery_attempt_public_id)
        REFERENCES d2_posting_review_delivery_attempts(delivery_attempt_public_id),
    UNIQUE (delivery_attempt_public_id, provider_message_id),
    UNIQUE (
        observation_public_id, delivery_attempt_public_id, outcome,
        provider_message_id, finance_delivery_material_sha256,
        receipt_token_sha256, source_identity_sha256, channel,
        telegram_account_id, telegram_conversation_id, conversation_binding_id
    ),
    CHECK (outcome != 'success' OR provider_message_id IS NOT NULL),
    CHECK (outcome != 'failure' OR error_code IS NOT NULL)
) STRICT;

CREATE TABLE d2_posting_review_delivery_activations (
    review_public_id TEXT PRIMARY KEY,
    delivery_attempt_public_id TEXT NOT NULL UNIQUE,
    observation_public_id TEXT NOT NULL UNIQUE,
    observation_outcome TEXT NOT NULL CHECK (observation_outcome = 'success'),
    provider_message_id INTEGER NOT NULL CHECK (provider_message_id > 0),
    channel TEXT NOT NULL CHECK (channel = 'telegram'),
    telegram_account_id TEXT NOT NULL,
    telegram_conversation_id TEXT NOT NULL,
    conversation_binding_id TEXT NOT NULL,
    finance_delivery_material_sha256 TEXT NOT NULL CHECK (
        length(finance_delivery_material_sha256) = 64
        AND finance_delivery_material_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    receipt_token_sha256 TEXT NOT NULL UNIQUE CHECK (
        length(receipt_token_sha256) = 64
        AND receipt_token_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    source_identity_sha256 TEXT NOT NULL CHECK (
        length(source_identity_sha256) = 64
        AND source_identity_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    activated_at INTEGER NOT NULL CHECK (activated_at > 0),
    FOREIGN KEY (review_public_id) REFERENCES d2_posting_reviews(review_public_id),
    FOREIGN KEY (delivery_attempt_public_id)
        REFERENCES d2_posting_review_delivery_attempts(delivery_attempt_public_id),
    FOREIGN KEY (observation_public_id)
        REFERENCES d2_posting_review_delivery_observations(observation_public_id),
    FOREIGN KEY (
        delivery_attempt_public_id, review_public_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ) REFERENCES d2_posting_review_delivery_attempts (
        delivery_attempt_public_id, review_public_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ),
    FOREIGN KEY (
        observation_public_id, delivery_attempt_public_id, observation_outcome,
        provider_message_id, finance_delivery_material_sha256,
        receipt_token_sha256, source_identity_sha256, channel,
        telegram_account_id, telegram_conversation_id, conversation_binding_id
    ) REFERENCES d2_posting_review_delivery_observations (
        observation_public_id, delivery_attempt_public_id, outcome,
        provider_message_id, finance_delivery_material_sha256,
        receipt_token_sha256, source_identity_sha256, channel,
        telegram_account_id, telegram_conversation_id, conversation_binding_id
    ),
    UNIQUE (telegram_account_id, telegram_conversation_id, provider_message_id),
    UNIQUE (
        review_public_id, delivery_attempt_public_id,
        observation_public_id, provider_message_id
    )
) STRICT;

CREATE TABLE d2_posting_review_delivery_conflicts (
    conflict_public_id TEXT PRIMARY KEY CHECK (
        length(conflict_public_id) = 39
        AND conflict_public_id GLOB 'd2dcon_[0-9a-f]*'
        AND conflict_public_id NOT GLOB 'd2dcon_*[^0-9a-f]*'
    ),
    review_public_id TEXT NOT NULL,
    delivery_attempt_public_id TEXT NOT NULL,
    activated_observation_public_id TEXT NOT NULL,
    conflicting_observation_public_id TEXT NOT NULL UNIQUE,
    conflicting_observation_outcome TEXT NOT NULL CHECK (
        conflicting_observation_outcome = 'conflict'
    ),
    activated_provider_message_id INTEGER NOT NULL CHECK (activated_provider_message_id > 0),
    conflicting_provider_message_id INTEGER NOT NULL CHECK (conflicting_provider_message_id > 0),
    finance_delivery_material_sha256 TEXT NOT NULL CHECK (
        length(finance_delivery_material_sha256) = 64
        AND finance_delivery_material_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    receipt_token_sha256 TEXT NOT NULL UNIQUE CHECK (
        length(receipt_token_sha256) = 64
        AND receipt_token_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    source_identity_sha256 TEXT NOT NULL CHECK (
        length(source_identity_sha256) = 64
        AND source_identity_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    channel TEXT NOT NULL CHECK (channel = 'telegram'),
    telegram_account_id TEXT NOT NULL,
    telegram_conversation_id TEXT NOT NULL,
    conversation_binding_id TEXT NOT NULL,
    observed_at INTEGER NOT NULL CHECK (observed_at > 0),
    FOREIGN KEY (review_public_id) REFERENCES d2_posting_reviews(review_public_id),
    FOREIGN KEY (delivery_attempt_public_id)
        REFERENCES d2_posting_review_delivery_attempts(delivery_attempt_public_id),
    FOREIGN KEY (
        review_public_id, delivery_attempt_public_id,
        activated_observation_public_id, activated_provider_message_id
    ) REFERENCES d2_posting_review_delivery_activations (
        review_public_id, delivery_attempt_public_id,
        observation_public_id, provider_message_id
    ),
    FOREIGN KEY (
        conflicting_observation_public_id, delivery_attempt_public_id,
        conflicting_observation_outcome, conflicting_provider_message_id,
        finance_delivery_material_sha256,
        receipt_token_sha256, source_identity_sha256, channel,
        telegram_account_id, telegram_conversation_id, conversation_binding_id
    ) REFERENCES d2_posting_review_delivery_observations (
        observation_public_id, delivery_attempt_public_id, outcome, provider_message_id,
        finance_delivery_material_sha256, receipt_token_sha256,
        source_identity_sha256, channel, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ),
    FOREIGN KEY (activated_observation_public_id)
        REFERENCES d2_posting_review_delivery_observations(observation_public_id),
    UNIQUE (review_public_id, conflicting_provider_message_id),
    CHECK (activated_provider_message_id != conflicting_provider_message_id)
) STRICT;

CREATE TABLE d2_posting_review_supersessions (
    predecessor_review_public_id TEXT PRIMARY KEY,
    successor_review_public_id TEXT NOT NULL UNIQUE,
    replacement_idempotency_key TEXT NOT NULL UNIQUE CHECK (
        length(trim(replacement_idempotency_key)) > 0
    ),
    replacement_material_hash TEXT NOT NULL CHECK (
        length(replacement_material_hash) = 64
        AND replacement_material_hash NOT GLOB '*[^0-9a-f]*'
    ),
    reason TEXT NOT NULL CHECK (reason IN ('delivery_unknown', 'delivery_conflict', 'expired')),
    created_at TEXT NOT NULL,
    FOREIGN KEY (predecessor_review_public_id)
        REFERENCES d2_posting_reviews(review_public_id),
    FOREIGN KEY (successor_review_public_id)
        REFERENCES d2_posting_reviews(review_public_id)
) STRICT;

CREATE TRIGGER trg_d2_controls_no_update
BEFORE UPDATE ON d2_posting_review_controls BEGIN
    SELECT RAISE(ABORT, 'D2 review controls are append-only');
END;
CREATE TRIGGER trg_d2_controls_no_insert_collision
BEFORE INSERT ON d2_posting_review_controls
WHEN EXISTS (
    SELECT 1 FROM d2_posting_review_controls AS existing
    WHERE (existing.review_public_id = NEW.review_public_id
           AND existing.action = NEW.action)
       OR existing.reference_id = NEW.reference_id
       OR (existing.review_public_id = NEW.review_public_id
           AND existing.row_index = NEW.row_index
           AND existing.column_index = NEW.column_index)
) BEGIN
    SELECT RAISE(ABORT, 'D2 review control identity collision');
END;
CREATE TRIGGER trg_d2_controls_no_delete
BEFORE DELETE ON d2_posting_review_controls BEGIN
    SELECT RAISE(ABORT, 'D2 review controls are append-only');
END;
CREATE TRIGGER trg_d2_delivery_attempts_no_update
BEFORE UPDATE ON d2_posting_review_delivery_attempts BEGIN
    SELECT RAISE(ABORT, 'D2 delivery attempts are append-only');
END;
CREATE TRIGGER trg_d2_delivery_attempts_no_insert_collision
BEFORE INSERT ON d2_posting_review_delivery_attempts
WHEN EXISTS (
    SELECT 1 FROM d2_posting_review_delivery_attempts AS existing
    WHERE existing.delivery_attempt_public_id = NEW.delivery_attempt_public_id
       OR existing.review_public_id = NEW.review_public_id
       OR existing.attempt_nonce_sha256 = NEW.attempt_nonce_sha256
) BEGIN
    SELECT RAISE(ABORT, 'D2 delivery attempt identity collision');
END;
CREATE TRIGGER trg_d2_delivery_attempts_no_delete
BEFORE DELETE ON d2_posting_review_delivery_attempts BEGIN
    SELECT RAISE(ABORT, 'D2 delivery attempts are append-only');
END;
CREATE TRIGGER trg_d2_delivery_observations_no_update
BEFORE UPDATE ON d2_posting_review_delivery_observations BEGIN
    SELECT RAISE(ABORT, 'D2 delivery observations are append-only');
END;
CREATE TRIGGER trg_d2_delivery_observations_no_insert_collision
BEFORE INSERT ON d2_posting_review_delivery_observations
WHEN EXISTS (
    SELECT 1 FROM d2_posting_review_delivery_observations AS existing
    WHERE existing.observation_public_id = NEW.observation_public_id
       OR existing.receipt_token_sha256 = NEW.receipt_token_sha256
       OR (existing.delivery_attempt_public_id = NEW.delivery_attempt_public_id
           AND existing.provider_message_id = NEW.provider_message_id)
) BEGIN
    SELECT RAISE(ABORT, 'D2 delivery observation identity collision');
END;
CREATE TRIGGER trg_d2_delivery_observations_no_delete
BEFORE DELETE ON d2_posting_review_delivery_observations BEGIN
    SELECT RAISE(ABORT, 'D2 delivery observations are append-only');
END;
CREATE TRIGGER trg_d2_delivery_activations_no_update
BEFORE UPDATE ON d2_posting_review_delivery_activations BEGIN
    SELECT RAISE(ABORT, 'D2 delivery activations are append-only');
END;
CREATE TRIGGER trg_d2_delivery_activations_no_insert_collision
BEFORE INSERT ON d2_posting_review_delivery_activations
WHEN EXISTS (
    SELECT 1 FROM d2_posting_review_delivery_activations AS existing
    WHERE existing.review_public_id = NEW.review_public_id
       OR existing.delivery_attempt_public_id = NEW.delivery_attempt_public_id
       OR existing.observation_public_id = NEW.observation_public_id
       OR existing.receipt_token_sha256 = NEW.receipt_token_sha256
       OR (existing.telegram_account_id = NEW.telegram_account_id
           AND existing.telegram_conversation_id = NEW.telegram_conversation_id
           AND existing.provider_message_id = NEW.provider_message_id)
) BEGIN
    SELECT RAISE(ABORT, 'D2 delivery activation identity collision');
END;
CREATE TRIGGER trg_d2_delivery_activations_no_delete
BEFORE DELETE ON d2_posting_review_delivery_activations BEGIN
    SELECT RAISE(ABORT, 'D2 delivery activations are append-only');
END;
CREATE TRIGGER trg_d2_delivery_conflicts_no_update
BEFORE UPDATE ON d2_posting_review_delivery_conflicts BEGIN
    SELECT RAISE(ABORT, 'D2 delivery conflicts are append-only');
END;
CREATE TRIGGER trg_d2_delivery_conflicts_no_insert_collision
BEFORE INSERT ON d2_posting_review_delivery_conflicts
WHEN EXISTS (
    SELECT 1 FROM d2_posting_review_delivery_conflicts AS existing
    WHERE existing.conflict_public_id = NEW.conflict_public_id
       OR existing.conflicting_observation_public_id = NEW.conflicting_observation_public_id
       OR existing.receipt_token_sha256 = NEW.receipt_token_sha256
       OR (existing.review_public_id = NEW.review_public_id
           AND existing.conflicting_provider_message_id = NEW.conflicting_provider_message_id)
) BEGIN
    SELECT RAISE(ABORT, 'D2 delivery conflict identity collision');
END;
CREATE TRIGGER trg_d2_delivery_conflicts_no_delete
BEFORE DELETE ON d2_posting_review_delivery_conflicts BEGIN
    SELECT RAISE(ABORT, 'D2 delivery conflicts are append-only');
END;
CREATE TRIGGER trg_d2_supersessions_no_update
BEFORE UPDATE ON d2_posting_review_supersessions BEGIN
    SELECT RAISE(ABORT, 'D2 review supersessions are append-only');
END;
CREATE TRIGGER trg_d2_supersessions_no_insert_collision
BEFORE INSERT ON d2_posting_review_supersessions
WHEN EXISTS (
    SELECT 1 FROM d2_posting_review_supersessions AS existing
    WHERE existing.predecessor_review_public_id = NEW.predecessor_review_public_id
       OR existing.successor_review_public_id = NEW.successor_review_public_id
       OR existing.replacement_idempotency_key = NEW.replacement_idempotency_key
) BEGIN
    SELECT RAISE(ABORT, 'D2 review supersession identity collision');
END;
CREATE TRIGGER trg_d2_supersessions_no_delete
BEFORE DELETE ON d2_posting_review_supersessions BEGIN
    SELECT RAISE(ABORT, 'D2 review supersessions are append-only');
END;

PRAGMA foreign_keys = ON;
