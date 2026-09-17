-- D1 append-only human proposal drafts and card-delivery recovery.
--
-- These records are proposal/evidence state only.  They never confirm or
-- convert a proposal and never create a final financial fact.

CREATE TABLE parser_human_drafts (
    id INTEGER PRIMARY KEY,
    draft_public_id TEXT NOT NULL UNIQUE CHECK (
        length(draft_public_id) = 40
        AND draft_public_id GLOB 'd1draft_[0-9a-f]*'
        AND draft_public_id NOT GLOB 'd1draft_*[^0-9a-f]*'
    ),
    source_parser_output_id INTEGER NOT NULL,
    source_raw_intake_id INTEGER,
    source_edit_reference_id INTEGER NOT NULL UNIQUE,
    source_reference_public_id TEXT NOT NULL,
    start_redemption_public_id TEXT NOT NULL UNIQUE CHECK (
        length(start_redemption_public_id) BETWEEN 1 AND 200
    ),
    start_redemption_material_hash TEXT NOT NULL CHECK (
        length(start_redemption_material_hash) = 64
        AND start_redemption_material_hash NOT GLOB '*[^0-9a-f]*'
    ),
    current_draft_version INTEGER NOT NULL CHECK (current_draft_version >= 0),
    current_draft_content_hash TEXT NOT NULL CHECK (
        length(current_draft_content_hash) = 64
        AND current_draft_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    current_payload_json TEXT NOT NULL CHECK (json_valid(current_payload_json) = 1),
    field_values_json TEXT NOT NULL CHECK (json_valid(field_values_json) = 1),
    reason_policy_version TEXT NOT NULL CHECK (reason_policy_version = 'd1-reason-policy-v1'),
    reason_contributors_json TEXT NOT NULL CHECK (json_valid(reason_contributors_json) = 1),
    reason_contributors_hash TEXT NOT NULL CHECK (
        length(reason_contributors_hash) = 64
        AND reason_contributors_hash NOT GLOB '*[^0-9a-f]*'
    ),
    unresolved_flags_json TEXT NOT NULL CHECK (json_valid(unresolved_flags_json) = 1),
    unresolved_flags_hash TEXT NOT NULL CHECK (
        length(unresolved_flags_hash) = 64
        AND unresolved_flags_hash NOT GLOB '*[^0-9a-f]*'
    ),
    current_parser_output_id INTEGER,
    current_proposal_version INTEGER CHECK (current_proposal_version IS NULL OR current_proposal_version >= 0),
    current_proposal_content_hash TEXT CHECK (
        current_proposal_content_hash IS NULL OR (
            length(current_proposal_content_hash) = 64
            AND current_proposal_content_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    decision_target_parser_output_id INTEGER NOT NULL,
    decision_target_proposal_version INTEGER NOT NULL CHECK (decision_target_proposal_version >= 0),
    decision_target_proposal_content_hash TEXT NOT NULL CHECK (
        length(decision_target_proposal_content_hash) = 64
        AND decision_target_proposal_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    current_card_generation_public_id TEXT NOT NULL CHECK (
        length(current_card_generation_public_id) = 39
        AND current_card_generation_public_id GLOB 'd1card_[0-9a-f]*'
        AND current_card_generation_public_id NOT GLOB 'd1card_*[^0-9a-f]*'
    ),
    authenticated_actor_id TEXT NOT NULL CHECK (length(authenticated_actor_id) BETWEEN 1 AND 200),
    telegram_account_id TEXT NOT NULL CHECK (length(telegram_account_id) BETWEEN 1 AND 200),
    telegram_conversation_id TEXT NOT NULL CHECK (length(telegram_conversation_id) BETWEEN 1 AND 200),
    conversation_binding_id TEXT NOT NULL CHECK (length(conversation_binding_id) BETWEEN 1 AND 200),
    state TEXT NOT NULL CHECK (state IN ('active', 'expired', 'confirmed', 'rejected', 'superseded')),
    expires_at INTEGER NOT NULL CHECK (expires_at > 0),
    last_claimed_message_id INTEGER NOT NULL CHECK (last_claimed_message_id > 0),
    last_claimed_at INTEGER NOT NULL CHECK (last_claimed_at > 0),
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= created_at),
    FOREIGN KEY (source_parser_output_id) REFERENCES parser_outputs(id),
    FOREIGN KEY (source_raw_intake_id) REFERENCES raw_intake_records(id),
    FOREIGN KEY (source_edit_reference_id) REFERENCES openclaw_human_action_references(id),
    FOREIGN KEY (current_parser_output_id) REFERENCES parser_outputs(id),
    FOREIGN KEY (decision_target_parser_output_id) REFERENCES parser_outputs(id),
    UNIQUE (
        id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ),
    UNIQUE (
        id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id, state
    ),
    CHECK (
        (current_parser_output_id IS NULL AND current_proposal_version IS NULL AND current_proposal_content_hash IS NULL)
        OR
        (current_parser_output_id IS NOT NULL AND current_proposal_version IS NOT NULL AND current_proposal_content_hash IS NOT NULL)
    ),
    CHECK (last_claimed_at >= created_at)
) STRICT;

CREATE UNIQUE INDEX idx_parser_human_drafts_active_source_context
    ON parser_human_drafts(
        source_parser_output_id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ) WHERE state = 'active';

CREATE TABLE parser_human_draft_reply_evidence (
    id INTEGER PRIMARY KEY,
    evidence_public_id TEXT NOT NULL UNIQUE CHECK (
        length(evidence_public_id) = 43
        AND evidence_public_id GLOB 'd1evidence_[0-9a-f]*'
        AND evidence_public_id NOT GLOB 'd1evidence_*[^0-9a-f]*'
    ),
    draft_id INTEGER NOT NULL,
    raw_utf8 BLOB NOT NULL CHECK (typeof(raw_utf8) = 'blob'),
    encoding TEXT NOT NULL CHECK (encoding = 'UTF-8'),
    format_version TEXT NOT NULL CHECK (format_version = 'd1-human-reply-v1'),
    byte_length INTEGER NOT NULL CHECK (byte_length BETWEEN 1 AND 16384),
    sha256 TEXT NOT NULL CHECK (
        length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    authenticated_actor_id TEXT NOT NULL,
    telegram_account_id TEXT NOT NULL,
    telegram_conversation_id TEXT NOT NULL,
    conversation_binding_id TEXT NOT NULL,
    telegram_message_id INTEGER NOT NULL CHECK (telegram_message_id > 0),
    received_at INTEGER NOT NULL CHECK (received_at > 0),
    FOREIGN KEY (draft_id) REFERENCES parser_human_drafts(id),
    FOREIGN KEY (
        draft_id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ) REFERENCES parser_human_drafts(
        id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ),
    CHECK (length(raw_utf8) = byte_length),
    UNIQUE (
        id, draft_id, telegram_message_id, authenticated_actor_id,
        telegram_account_id, telegram_conversation_id, conversation_binding_id
    ),
    UNIQUE (
        authenticated_actor_id, telegram_account_id, telegram_conversation_id,
        conversation_binding_id, telegram_message_id
    )
) STRICT;

CREATE TABLE parser_human_draft_operations (
    id INTEGER PRIMARY KEY,
    operation_public_id TEXT NOT NULL UNIQUE CHECK (length(operation_public_id) BETWEEN 1 AND 200),
    draft_id INTEGER NOT NULL,
    operation_type TEXT NOT NULL CHECK (
        operation_type IN ('start', 'accepted', 'refused', 'noop', 'confirmed', 'rejected')
    ),
    terminal_head_state TEXT GENERATED ALWAYS AS (
        CASE WHEN operation_type IN ('confirmed', 'rejected') THEN operation_type END
    ) STORED,
    operation_outcome TEXT NOT NULL CHECK (operation_outcome IN ('started', 'accepted', 'refused', 'noop')),
    result_completeness TEXT NOT NULL CHECK (result_completeness IN ('complete', 'incomplete')),
    telegram_message_id INTEGER CHECK (telegram_message_id IS NULL OR telegram_message_id > 0),
    request_material_hash TEXT NOT NULL CHECK (
        length(request_material_hash) = 64 AND request_material_hash NOT GLOB '*[^0-9a-f]*'
    ),
    human_reply_evidence_id INTEGER UNIQUE,
    action_reference_id INTEGER,
    decision_public_id TEXT,
    before_draft_version INTEGER NOT NULL CHECK (before_draft_version >= 0),
    before_draft_content_hash TEXT NOT NULL CHECK (
        length(before_draft_content_hash) = 64 AND before_draft_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    after_draft_version INTEGER NOT NULL CHECK (after_draft_version >= 0),
    after_draft_content_hash TEXT NOT NULL CHECK (
        length(after_draft_content_hash) = 64 AND after_draft_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    canonical_supplied_fields_json TEXT NOT NULL CHECK (json_valid(canonical_supplied_fields_json) = 1),
    material_changes_json TEXT NOT NULL CHECK (json_valid(material_changes_json) = 1),
    explicit_clears_json TEXT NOT NULL CHECK (json_valid(explicit_clears_json) = 1),
    reason_policy_version TEXT NOT NULL CHECK (reason_policy_version = 'd1-reason-policy-v1'),
    reason_contributors_before_json TEXT NOT NULL CHECK (json_valid(reason_contributors_before_json) = 1),
    reason_contributors_before_hash TEXT NOT NULL CHECK (
        length(reason_contributors_before_hash) = 64 AND reason_contributors_before_hash NOT GLOB '*[^0-9a-f]*'
    ),
    reason_contributors_after_json TEXT NOT NULL CHECK (json_valid(reason_contributors_after_json) = 1),
    reason_contributors_after_hash TEXT NOT NULL CHECK (
        length(reason_contributors_after_hash) = 64 AND reason_contributors_after_hash NOT GLOB '*[^0-9a-f]*'
    ),
    unresolved_flags_json TEXT NOT NULL CHECK (json_valid(unresolved_flags_json) = 1),
    refusal_code TEXT,
    result_card_generation_public_id TEXT NOT NULL,
    publication_parser_output_id INTEGER,
    authenticated_actor_id TEXT NOT NULL,
    telegram_account_id TEXT NOT NULL,
    telegram_conversation_id TEXT NOT NULL,
    conversation_binding_id TEXT NOT NULL,
    correction_channel TEXT NOT NULL CHECK (correction_channel = 'telegram'),
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    FOREIGN KEY (draft_id) REFERENCES parser_human_drafts(id),
    FOREIGN KEY (
        draft_id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ) REFERENCES parser_human_drafts(
        id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ),
    FOREIGN KEY (
        draft_id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id, terminal_head_state
    ) REFERENCES parser_human_drafts(
        id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id, state
    ) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (human_reply_evidence_id) REFERENCES parser_human_draft_reply_evidence(id),
    FOREIGN KEY (
        human_reply_evidence_id, draft_id, telegram_message_id,
        authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ) REFERENCES parser_human_draft_reply_evidence(
        id, draft_id, telegram_message_id, authenticated_actor_id,
        telegram_account_id, telegram_conversation_id, conversation_binding_id
    ),
    FOREIGN KEY (action_reference_id) REFERENCES openclaw_human_action_references(id),
    FOREIGN KEY (decision_public_id)
        REFERENCES parser_proposal_authorizations(confirmation_public_id),
    FOREIGN KEY (publication_parser_output_id) REFERENCES parser_outputs(id),
    CHECK (
        (operation_type IN ('accepted', 'refused', 'noop') AND human_reply_evidence_id IS NOT NULL AND telegram_message_id IS NOT NULL)
        OR
        (operation_type NOT IN ('accepted', 'refused', 'noop') AND human_reply_evidence_id IS NULL)
    ),
    CHECK (operation_type != 'start' OR action_reference_id IS NOT NULL),
    CHECK (
        (operation_type IN ('confirmed', 'rejected')) = (decision_public_id IS NOT NULL)
    ),
    CHECK ((operation_outcome = 'refused') = (refusal_code IS NOT NULL)),
    CHECK (after_draft_version >= before_draft_version),
    UNIQUE (id, draft_id),
    UNIQUE (
        id, draft_id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ),
    FOREIGN KEY (
        result_card_generation_public_id, draft_id, authenticated_actor_id,
        telegram_account_id, telegram_conversation_id, conversation_binding_id
    ) REFERENCES parser_human_draft_cards(
        card_generation_public_id, draft_id, authenticated_actor_id,
        telegram_account_id, telegram_conversation_id, conversation_binding_id
    ) DEFERRABLE INITIALLY DEFERRED
) STRICT;

CREATE UNIQUE INDEX idx_parser_human_draft_operation_message_claim
    ON parser_human_draft_operations(draft_id, telegram_message_id)
    WHERE telegram_message_id IS NOT NULL;

CREATE TABLE parser_human_draft_cards (
    id INTEGER PRIMARY KEY,
    card_generation_public_id TEXT NOT NULL UNIQUE CHECK (
        length(card_generation_public_id) = 39
        AND card_generation_public_id GLOB 'd1card_[0-9a-f]*'
        AND card_generation_public_id NOT GLOB 'd1card_*[^0-9a-f]*'
    ),
    draft_id INTEGER NOT NULL,
    draft_version INTEGER NOT NULL CHECK (draft_version >= 0),
    draft_content_hash TEXT NOT NULL CHECK (
        length(draft_content_hash) = 64 AND draft_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    field_values_json TEXT NOT NULL CHECK (json_valid(field_values_json) = 1),
    parser_output_id INTEGER,
    proposal_version INTEGER CHECK (proposal_version IS NULL OR proposal_version >= 0),
    proposal_content_hash TEXT CHECK (
        proposal_content_hash IS NULL OR (
            length(proposal_content_hash) = 64 AND proposal_content_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    decision_target_parser_output_id INTEGER NOT NULL,
    decision_target_proposal_version INTEGER NOT NULL CHECK (decision_target_proposal_version >= 0),
    decision_target_proposal_content_hash TEXT NOT NULL CHECK (
        length(decision_target_proposal_content_hash) = 64
        AND decision_target_proposal_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    language TEXT NOT NULL CHECK (language IN ('zh', 'en', 'mixed')),
    format_version TEXT NOT NULL CHECK (format_version = 'd1-human-card-v1'),
    action_issue_batch_id TEXT NOT NULL CHECK (length(action_issue_batch_id) = 64),
    authenticated_actor_id TEXT NOT NULL,
    telegram_account_id TEXT NOT NULL,
    telegram_conversation_id TEXT NOT NULL,
    conversation_binding_id TEXT NOT NULL,
    predecessor_card_id INTEGER UNIQUE,
    original_operation_id INTEGER NOT NULL,
    recovery_public_id TEXT UNIQUE CHECK (
        recovery_public_id IS NULL OR (
            length(recovery_public_id) = 64 AND recovery_public_id NOT GLOB '*[^0-9a-f]*'
        )
    ),
    recovery_material_hash TEXT CHECK (
        recovery_material_hash IS NULL OR (
            length(recovery_material_hash) = 64 AND recovery_material_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    recovery_delivery_state_hash TEXT CHECK (
        recovery_delivery_state_hash IS NULL OR (
            length(recovery_delivery_state_hash) = 64
            AND recovery_delivery_state_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    recovery_reason TEXT CHECK (
        recovery_reason IS NULL OR recovery_reason IN ('failure', 'expiry', 'unknown_after_query')
    ),
    expires_at INTEGER NOT NULL CHECK (expires_at > 0),
    issued_at INTEGER NOT NULL CHECK (issued_at > 0),
    FOREIGN KEY (draft_id) REFERENCES parser_human_drafts(id),
    FOREIGN KEY (
        draft_id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ) REFERENCES parser_human_drafts(
        id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ),
    FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id),
    FOREIGN KEY (decision_target_parser_output_id) REFERENCES parser_outputs(id),
    FOREIGN KEY (
        predecessor_card_id, draft_id, authenticated_actor_id,
        telegram_account_id, telegram_conversation_id, conversation_binding_id
    ) REFERENCES parser_human_draft_cards(
        id, draft_id, authenticated_actor_id,
        telegram_account_id, telegram_conversation_id, conversation_binding_id
    ),
    FOREIGN KEY (
        original_operation_id, draft_id, authenticated_actor_id,
        telegram_account_id, telegram_conversation_id, conversation_binding_id
    ) REFERENCES parser_human_draft_operations(
        id, draft_id, authenticated_actor_id,
        telegram_account_id, telegram_conversation_id, conversation_binding_id
    ) DEFERRABLE INITIALLY DEFERRED,
    CHECK (
        (parser_output_id IS NULL AND proposal_version IS NULL AND proposal_content_hash IS NULL)
        OR
        (parser_output_id IS NOT NULL AND proposal_version IS NOT NULL AND proposal_content_hash IS NOT NULL)
    ),
    CHECK (
        (recovery_public_id IS NULL AND recovery_material_hash IS NULL
            AND recovery_delivery_state_hash IS NULL AND recovery_reason IS NULL)
        OR
        (recovery_public_id IS NOT NULL AND recovery_material_hash IS NOT NULL
            AND recovery_delivery_state_hash IS NOT NULL AND recovery_reason IS NOT NULL
            AND predecessor_card_id IS NOT NULL)
    ),
    UNIQUE (
        card_generation_public_id, draft_id, decision_target_parser_output_id,
        decision_target_proposal_version, decision_target_proposal_content_hash,
        authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ),
    UNIQUE (
        id, draft_id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ),
    UNIQUE (
        card_generation_public_id, draft_id, authenticated_actor_id,
        telegram_account_id, telegram_conversation_id, conversation_binding_id
    ),
    UNIQUE (
        card_generation_public_id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    )
) STRICT;

CREATE TABLE parser_human_draft_card_delivery_attempts (
    id INTEGER PRIMARY KEY,
    attempt_public_id TEXT NOT NULL UNIQUE CHECK (
        length(attempt_public_id) = 64 AND attempt_public_id NOT GLOB '*[^0-9a-f]*'
    ),
    card_generation_public_id TEXT NOT NULL,
    delivery_identity TEXT NOT NULL UNIQUE CHECK (
        length(delivery_identity) = 64 AND delivery_identity NOT GLOB '*[^0-9a-f]*'
    ),
    delivery_material_hash TEXT NOT NULL CHECK (
        length(delivery_material_hash) = 64 AND delivery_material_hash NOT GLOB '*[^0-9a-f]*'
    ),
    authenticated_actor_id TEXT NOT NULL,
    telegram_account_id TEXT NOT NULL,
    telegram_conversation_id TEXT NOT NULL,
    conversation_binding_id TEXT NOT NULL,
    transport_mode TEXT NOT NULL CHECK (transport_mode IN ('replace', 'reply')),
    outbound_target_message_id TEXT,
    attempted_at INTEGER NOT NULL CHECK (attempted_at > 0),
    FOREIGN KEY (card_generation_public_id)
        REFERENCES parser_human_draft_cards(card_generation_public_id),
    FOREIGN KEY (
        card_generation_public_id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ) REFERENCES parser_human_draft_cards(
        card_generation_public_id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ),
    UNIQUE (card_generation_public_id, transport_mode)
) STRICT;

CREATE TABLE parser_human_draft_card_delivery_outcomes (
    id INTEGER PRIMARY KEY,
    observation_public_id TEXT NOT NULL UNIQUE CHECK (
        length(observation_public_id) = 64 AND observation_public_id NOT GLOB '*[^0-9a-f]*'
    ),
    attempt_public_id TEXT NOT NULL,
    observation_slot TEXT NOT NULL CHECK (observation_slot IN ('initial', 'resolution')),
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure', 'unknown')),
    error_code TEXT,
    outbound_message_id TEXT,
    trusted_receipt_hash TEXT CHECK (
        trusted_receipt_hash IS NULL OR (
            length(trusted_receipt_hash) = 64 AND trusted_receipt_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    observed_at INTEGER NOT NULL CHECK (observed_at > 0),
    FOREIGN KEY (attempt_public_id)
        REFERENCES parser_human_draft_card_delivery_attempts(attempt_public_id),
    UNIQUE (attempt_public_id, observation_slot),
    CHECK (outbound_message_id IS NULL OR trusted_receipt_hash IS NOT NULL),
    CHECK (outcome != 'success' OR (outbound_message_id IS NOT NULL AND trusted_receipt_hash IS NOT NULL)),
    CHECK (outcome != 'failure' OR error_code IS NOT NULL)
) STRICT;

CREATE TABLE parser_human_draft_publications (
    id INTEGER PRIMARY KEY,
    publication_public_id TEXT NOT NULL UNIQUE CHECK (length(publication_public_id) BETWEEN 1 AND 200),
    draft_id INTEGER NOT NULL,
    operation_id INTEGER NOT NULL UNIQUE,
    draft_version INTEGER NOT NULL CHECK (draft_version > 0),
    draft_content_hash TEXT NOT NULL CHECK (
        length(draft_content_hash) = 64 AND draft_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    parser_output_id INTEGER NOT NULL UNIQUE,
    proposal_public_id TEXT NOT NULL,
    proposal_version INTEGER NOT NULL CHECK (proposal_version >= 0),
    proposal_content_hash TEXT NOT NULL CHECK (
        length(proposal_content_hash) = 64 AND proposal_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    published_at INTEGER NOT NULL CHECK (published_at > 0),
    FOREIGN KEY (draft_id) REFERENCES parser_human_drafts(id),
    FOREIGN KEY (operation_id, draft_id)
        REFERENCES parser_human_draft_operations(id, draft_id),
    FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id),
    UNIQUE (draft_id, draft_version)
) STRICT;

CREATE TABLE parser_human_draft_action_bindings (
    id INTEGER PRIMARY KEY,
    reference_id INTEGER NOT NULL UNIQUE,
    card_generation_public_id TEXT NOT NULL,
    draft_id INTEGER NOT NULL,
    parser_output_id INTEGER NOT NULL,
    proposal_version INTEGER NOT NULL CHECK (proposal_version >= 0),
    proposal_content_hash TEXT NOT NULL CHECK (
        length(proposal_content_hash) = 64
        AND proposal_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    authenticated_actor_id TEXT NOT NULL,
    telegram_account_id TEXT NOT NULL,
    telegram_conversation_id TEXT NOT NULL,
    conversation_binding_id TEXT NOT NULL,
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    FOREIGN KEY (reference_id) REFERENCES openclaw_human_action_references(id),
    FOREIGN KEY (
        draft_id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ) REFERENCES parser_human_drafts(
        id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ),
    FOREIGN KEY (card_generation_public_id)
        REFERENCES parser_human_draft_cards(card_generation_public_id),
    FOREIGN KEY (
        reference_id, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id, parser_output_id,
        proposal_version, proposal_content_hash
    ) REFERENCES openclaw_human_action_references(
        id, authenticated_actor_id, channel_account_id,
        channel_conversation_id, conversation_binding_id, parser_output_id,
        proposal_version, proposal_content_hash
    ),
    FOREIGN KEY (
        card_generation_public_id, draft_id, parser_output_id, proposal_version,
        proposal_content_hash, authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ) REFERENCES parser_human_draft_cards(
        card_generation_public_id, draft_id, decision_target_parser_output_id,
        decision_target_proposal_version, decision_target_proposal_content_hash,
        authenticated_actor_id, telegram_account_id,
        telegram_conversation_id, conversation_binding_id
    ),
    UNIQUE (reference_id, card_generation_public_id)
) STRICT;

CREATE UNIQUE INDEX idx_openclaw_human_action_reference_d1_closure
    ON openclaw_human_action_references(
        id, authenticated_actor_id, channel_account_id, channel_conversation_id,
        conversation_binding_id, parser_output_id, proposal_version,
        proposal_content_hash
    );

CREATE TRIGGER trg_parser_human_draft_delivery_resolution_time
    BEFORE INSERT ON parser_human_draft_card_delivery_outcomes
    WHEN NEW.observation_slot = 'resolution'
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM parser_human_draft_card_delivery_outcomes AS initial
        WHERE initial.attempt_public_id = NEW.attempt_public_id
          AND initial.observation_slot = 'initial'
          AND NEW.observed_at >= initial.observed_at
    ) THEN RAISE(ABORT, 'delivery resolution time precedes initial observation') END;
END;

CREATE TRIGGER trg_parser_human_draft_delivery_attempt_time
    BEFORE INSERT ON parser_human_draft_card_delivery_attempts
    WHEN EXISTS (
        SELECT 1
        FROM parser_human_draft_cards AS card
        WHERE card.card_generation_public_id = NEW.card_generation_public_id
          AND NEW.attempted_at < card.issued_at
    )
BEGIN
    SELECT RAISE(ABORT, 'delivery attempt time precedes card issue');
END;

CREATE TRIGGER trg_parser_human_draft_delivery_observation_time
    BEFORE INSERT ON parser_human_draft_card_delivery_outcomes
    WHEN EXISTS (
        SELECT 1
        FROM parser_human_draft_card_delivery_attempts AS attempt
        WHERE attempt.attempt_public_id = NEW.attempt_public_id
          AND NEW.observed_at < attempt.attempted_at
    )
BEGIN
    SELECT RAISE(ABORT, 'delivery observation time precedes attempt');
END;

CREATE TRIGGER trg_parser_human_draft_recovery_card_time
    BEFORE INSERT ON parser_human_draft_cards
    WHEN NEW.predecessor_card_id IS NOT NULL
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM parser_human_draft_cards AS predecessor
        WHERE predecessor.id = NEW.predecessor_card_id
          AND (
              NEW.issued_at < predecessor.issued_at
              OR EXISTS (
                  SELECT 1
                  FROM parser_human_draft_card_delivery_attempts AS attempt
                  WHERE attempt.card_generation_public_id = predecessor.card_generation_public_id
                    AND NEW.issued_at < attempt.attempted_at
              )
              OR EXISTS (
                  SELECT 1
                  FROM parser_human_draft_card_delivery_outcomes AS outcome
                  JOIN parser_human_draft_card_delivery_attempts AS attempt
                    ON attempt.attempt_public_id = outcome.attempt_public_id
                  WHERE attempt.card_generation_public_id = predecessor.card_generation_public_id
                    AND NEW.issued_at < outcome.observed_at
              )
          )
    ) THEN RAISE(ABORT, 'recovery card time precedes delivery evidence') END;
END;

CREATE TRIGGER trg_parser_human_draft_publication_lineage
    BEFORE INSERT ON parser_human_draft_publications
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM parser_human_draft_operations AS operation
        JOIN parser_human_draft_cards AS card
          ON card.original_operation_id = operation.id
         AND card.draft_id = operation.draft_id
         AND card.draft_version = operation.after_draft_version
         AND card.draft_content_hash = operation.after_draft_content_hash
         AND card.parser_output_id = operation.publication_parser_output_id
        JOIN parser_outputs AS proposal
          ON proposal.id = card.parser_output_id
         AND proposal.public_id = NEW.proposal_public_id
        WHERE operation.id = NEW.operation_id
          AND operation.draft_id = NEW.draft_id
          AND operation.operation_type = 'accepted'
          AND operation.operation_outcome = 'accepted'
          AND operation.after_draft_version = NEW.draft_version
          AND operation.after_draft_content_hash = NEW.draft_content_hash
          AND operation.publication_parser_output_id = NEW.parser_output_id
          AND card.proposal_version = NEW.proposal_version
          AND card.proposal_content_hash = NEW.proposal_content_hash
          AND NEW.parser_output_id = card.parser_output_id
    ) THEN RAISE(ABORT, 'publication lacks exact accepted operation lineage') END;
END;

CREATE TRIGGER trg_parser_human_drafts_no_delete
    BEFORE DELETE ON parser_human_drafts
BEGIN
    SELECT RAISE(ABORT, 'parser human drafts cannot be deleted');
END;

CREATE TRIGGER trg_parser_human_drafts_guarded_update
    BEFORE UPDATE ON parser_human_drafts
    WHEN NEW.draft_public_id != OLD.draft_public_id
      OR NEW.source_parser_output_id != OLD.source_parser_output_id
      OR NEW.source_raw_intake_id IS NOT OLD.source_raw_intake_id
      OR NEW.source_edit_reference_id != OLD.source_edit_reference_id
      OR NEW.source_reference_public_id != OLD.source_reference_public_id
      OR NEW.start_redemption_public_id != OLD.start_redemption_public_id
      OR NEW.start_redemption_material_hash != OLD.start_redemption_material_hash
      OR NEW.authenticated_actor_id != OLD.authenticated_actor_id
      OR NEW.telegram_account_id != OLD.telegram_account_id
      OR NEW.telegram_conversation_id != OLD.telegram_conversation_id
      OR NEW.conversation_binding_id != OLD.conversation_binding_id
      OR NEW.reason_policy_version != OLD.reason_policy_version
      OR NEW.expires_at != OLD.expires_at
      OR NEW.created_at != OLD.created_at
      OR OLD.state != 'active'
      OR NEW.current_draft_version < OLD.current_draft_version
      OR NEW.current_draft_version > OLD.current_draft_version + 1
      OR NEW.last_claimed_message_id < OLD.last_claimed_message_id
      OR NEW.last_claimed_at < OLD.last_claimed_at
      OR NEW.updated_at < OLD.updated_at
BEGIN
    SELECT RAISE(ABORT, 'parser human draft update violates guarded head contract');
END;

CREATE TRIGGER trg_parser_human_drafts_mutation_shape
    BEFORE UPDATE ON parser_human_drafts
    WHEN NOT (
        (
            NEW.state = 'active'
            AND NEW.current_draft_version = OLD.current_draft_version + 1
            AND NEW.last_claimed_message_id > OLD.last_claimed_message_id
            AND NEW.current_card_generation_public_id != OLD.current_card_generation_public_id
            AND EXISTS (
                SELECT 1
                FROM parser_human_draft_operations AS operation
                JOIN parser_human_draft_cards AS card
                  ON card.card_generation_public_id = operation.result_card_generation_public_id
                WHERE operation.draft_id = OLD.id
                  AND operation.operation_type = 'accepted'
                  AND operation.before_draft_version = OLD.current_draft_version
                  AND operation.before_draft_content_hash = OLD.current_draft_content_hash
                  AND operation.after_draft_version = NEW.current_draft_version
                  AND operation.after_draft_content_hash = NEW.current_draft_content_hash
                  AND operation.telegram_message_id = NEW.last_claimed_message_id
                  AND NEW.last_claimed_at = max(OLD.last_claimed_at, operation.created_at)
                  AND NEW.updated_at = max(OLD.updated_at, operation.created_at)
                  AND operation.result_card_generation_public_id = NEW.current_card_generation_public_id
                  AND card.draft_id = OLD.id
                  AND card.draft_version = NEW.current_draft_version
                  AND card.draft_content_hash = NEW.current_draft_content_hash
                  AND card.field_values_json = NEW.field_values_json
                  AND card.parser_output_id IS NEW.current_parser_output_id
                  AND card.proposal_version IS NEW.current_proposal_version
                  AND card.proposal_content_hash IS NEW.current_proposal_content_hash
                  AND card.decision_target_parser_output_id = NEW.decision_target_parser_output_id
                  AND card.decision_target_proposal_version = NEW.decision_target_proposal_version
                  AND card.decision_target_proposal_content_hash = NEW.decision_target_proposal_content_hash
                  AND card.authenticated_actor_id = NEW.authenticated_actor_id
                  AND card.telegram_account_id = NEW.telegram_account_id
                  AND card.telegram_conversation_id = NEW.telegram_conversation_id
                  AND card.conversation_binding_id = NEW.conversation_binding_id
                  AND (
                      NEW.current_parser_output_id IS NULL
                      OR EXISTS (
                          SELECT 1 FROM parser_human_draft_publications AS publication
                          WHERE publication.operation_id = operation.id
                            AND publication.draft_id = OLD.id
                            AND publication.draft_version = NEW.current_draft_version
                            AND publication.draft_content_hash = NEW.current_draft_content_hash
                            AND publication.parser_output_id = NEW.current_parser_output_id
                            AND publication.proposal_version = NEW.current_proposal_version
                            AND publication.proposal_content_hash = NEW.current_proposal_content_hash
                      )
                  )
            )
        )
        OR
        (
            NEW.state = 'active'
            AND NEW.current_draft_version = OLD.current_draft_version
            AND NEW.current_draft_content_hash = OLD.current_draft_content_hash
            AND NEW.current_payload_json = OLD.current_payload_json
            AND NEW.field_values_json = OLD.field_values_json
            AND NEW.reason_contributors_json = OLD.reason_contributors_json
            AND NEW.reason_contributors_hash = OLD.reason_contributors_hash
            AND NEW.unresolved_flags_json = OLD.unresolved_flags_json
            AND NEW.unresolved_flags_hash = OLD.unresolved_flags_hash
            AND NEW.current_parser_output_id IS OLD.current_parser_output_id
            AND NEW.current_proposal_version IS OLD.current_proposal_version
            AND NEW.current_proposal_content_hash IS OLD.current_proposal_content_hash
            AND NEW.decision_target_parser_output_id = OLD.decision_target_parser_output_id
            AND NEW.decision_target_proposal_version = OLD.decision_target_proposal_version
            AND NEW.decision_target_proposal_content_hash = OLD.decision_target_proposal_content_hash
            AND NEW.current_card_generation_public_id = OLD.current_card_generation_public_id
            AND NEW.last_claimed_message_id > OLD.last_claimed_message_id
            AND EXISTS (
                SELECT 1 FROM parser_human_draft_operations AS operation
                WHERE operation.draft_id = OLD.id
                  AND operation.operation_type IN ('refused', 'noop')
                  AND operation.telegram_message_id = NEW.last_claimed_message_id
                  AND NEW.last_claimed_at = max(OLD.last_claimed_at, operation.created_at)
                  AND NEW.updated_at = max(OLD.updated_at, operation.created_at)
                  AND operation.before_draft_version = OLD.current_draft_version
                  AND operation.after_draft_version = OLD.current_draft_version
                  AND operation.before_draft_content_hash = OLD.current_draft_content_hash
                  AND operation.after_draft_content_hash = OLD.current_draft_content_hash
                  AND operation.result_card_generation_public_id = OLD.current_card_generation_public_id
            )
        )
        OR
        (
            NEW.state = 'active'
            AND NEW.current_draft_version = OLD.current_draft_version
            AND NEW.current_draft_content_hash = OLD.current_draft_content_hash
            AND NEW.current_payload_json = OLD.current_payload_json
            AND NEW.field_values_json = OLD.field_values_json
            AND NEW.reason_contributors_json = OLD.reason_contributors_json
            AND NEW.reason_contributors_hash = OLD.reason_contributors_hash
            AND NEW.unresolved_flags_json = OLD.unresolved_flags_json
            AND NEW.unresolved_flags_hash = OLD.unresolved_flags_hash
            AND NEW.current_parser_output_id IS OLD.current_parser_output_id
            AND NEW.current_proposal_version IS OLD.current_proposal_version
            AND NEW.current_proposal_content_hash IS OLD.current_proposal_content_hash
            AND NEW.decision_target_parser_output_id = OLD.decision_target_parser_output_id
            AND NEW.decision_target_proposal_version = OLD.decision_target_proposal_version
            AND NEW.decision_target_proposal_content_hash = OLD.decision_target_proposal_content_hash
            AND NEW.last_claimed_message_id = OLD.last_claimed_message_id
            AND NEW.last_claimed_at = OLD.last_claimed_at
            AND NEW.current_card_generation_public_id != OLD.current_card_generation_public_id
            AND EXISTS (
                SELECT 1 FROM parser_human_draft_cards AS card
                JOIN parser_human_draft_cards AS predecessor
                  ON predecessor.id = card.predecessor_card_id
                WHERE card.card_generation_public_id = NEW.current_card_generation_public_id
                  AND card.draft_id = OLD.id
                  AND predecessor.card_generation_public_id = OLD.current_card_generation_public_id
                  AND card.recovery_public_id IS NOT NULL
                  AND card.draft_version = OLD.current_draft_version
                  AND card.draft_content_hash = OLD.current_draft_content_hash
                  AND card.field_values_json = OLD.field_values_json
                  AND card.parser_output_id IS OLD.current_parser_output_id
                  AND card.proposal_version IS OLD.current_proposal_version
                  AND card.proposal_content_hash IS OLD.current_proposal_content_hash
                  AND card.decision_target_parser_output_id = OLD.decision_target_parser_output_id
                  AND card.decision_target_proposal_version = OLD.decision_target_proposal_version
                  AND card.decision_target_proposal_content_hash = OLD.decision_target_proposal_content_hash
                  AND card.authenticated_actor_id = OLD.authenticated_actor_id
                  AND card.telegram_account_id = OLD.telegram_account_id
                  AND card.telegram_conversation_id = OLD.telegram_conversation_id
                  AND card.conversation_binding_id = OLD.conversation_binding_id
                  AND NEW.updated_at = max(OLD.updated_at, card.issued_at)
            )
        )
        OR
        (
            OLD.state = 'active'
            AND NEW.state IN ('confirmed', 'rejected')
            AND NEW.current_draft_version = OLD.current_draft_version
            AND NEW.current_draft_content_hash = OLD.current_draft_content_hash
            AND NEW.current_payload_json = OLD.current_payload_json
            AND NEW.field_values_json = OLD.field_values_json
            AND NEW.reason_contributors_json = OLD.reason_contributors_json
            AND NEW.reason_contributors_hash = OLD.reason_contributors_hash
            AND NEW.unresolved_flags_json = OLD.unresolved_flags_json
            AND NEW.unresolved_flags_hash = OLD.unresolved_flags_hash
            AND NEW.current_parser_output_id IS OLD.current_parser_output_id
            AND NEW.current_proposal_version IS OLD.current_proposal_version
            AND NEW.current_proposal_content_hash IS OLD.current_proposal_content_hash
            AND NEW.decision_target_parser_output_id = OLD.decision_target_parser_output_id
            AND NEW.decision_target_proposal_version = OLD.decision_target_proposal_version
            AND NEW.decision_target_proposal_content_hash = OLD.decision_target_proposal_content_hash
            AND NEW.current_card_generation_public_id = OLD.current_card_generation_public_id
            AND NEW.last_claimed_message_id = OLD.last_claimed_message_id
            AND NEW.last_claimed_at = OLD.last_claimed_at
            AND EXISTS (
                SELECT 1 FROM parser_human_draft_operations AS operation
                WHERE operation.draft_id = OLD.id
                  AND operation.operation_type IN ('confirmed', 'rejected')
                  AND (
                      (NEW.state = 'confirmed' AND operation.operation_type = 'confirmed')
                      OR (NEW.state = 'rejected' AND operation.operation_type = 'rejected')
                  )
                  AND operation.before_draft_version = OLD.current_draft_version
                  AND operation.after_draft_version = OLD.current_draft_version
                  AND operation.before_draft_content_hash = OLD.current_draft_content_hash
                  AND operation.after_draft_content_hash = OLD.current_draft_content_hash
                  AND operation.result_card_generation_public_id = OLD.current_card_generation_public_id
                  AND NEW.updated_at = max(OLD.updated_at, operation.created_at)
            )
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'parser human draft update violates mutation shape');
END;

CREATE TRIGGER trg_parser_human_draft_card_expiry
    BEFORE INSERT ON parser_human_draft_cards
    WHEN NEW.expires_at != min(
        (SELECT expires_at FROM parser_human_drafts WHERE id = NEW.draft_id),
        NEW.issued_at + 300
    )
BEGIN
    SELECT RAISE(ABORT, 'parser human draft card expiry must be bounded');
END;

CREATE TRIGGER trg_parser_human_draft_terminal_decision_evidence
    BEFORE INSERT ON parser_human_draft_operations
    WHEN NEW.operation_type IN ('confirmed', 'rejected')
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM parser_human_drafts AS draft
        JOIN parser_proposal_authorizations AS decision
          ON decision.confirmation_public_id = NEW.decision_public_id
         AND decision.parser_output_id = draft.decision_target_parser_output_id
         AND decision.proposal_content_hash = draft.decision_target_proposal_content_hash
         AND decision.authenticated_actor_id = draft.authenticated_actor_id
         AND decision.confirmation_state = NEW.operation_type
        JOIN parser_outputs AS proposal
          ON proposal.id = decision.parser_output_id
         AND proposal.parse_status = NEW.operation_type
        JOIN parser_human_draft_cards AS result_card
          ON result_card.card_generation_public_id = NEW.result_card_generation_public_id
         AND result_card.draft_id = draft.id
         AND result_card.authenticated_actor_id = draft.authenticated_actor_id
         AND result_card.telegram_account_id = draft.telegram_account_id
         AND result_card.telegram_conversation_id = draft.telegram_conversation_id
         AND result_card.conversation_binding_id = draft.conversation_binding_id
        WHERE draft.id = NEW.draft_id
          AND draft.state = 'active'
          AND draft.current_card_generation_public_id = NEW.result_card_generation_public_id
          AND draft.authenticated_actor_id = NEW.authenticated_actor_id
          AND draft.telegram_account_id = NEW.telegram_account_id
          AND draft.telegram_conversation_id = NEW.telegram_conversation_id
          AND draft.conversation_binding_id = NEW.conversation_binding_id
    ) THEN RAISE(ABORT, 'terminal operation lacks exact decision evidence') END;

    SELECT CASE WHEN NEW.operation_type = 'confirmed' AND (
        NEW.action_reference_id IS NULL OR NOT EXISTS (
            SELECT 1
            FROM parser_human_drafts AS draft
            JOIN parser_human_draft_action_bindings AS binding
              ON binding.reference_id = NEW.action_reference_id
             AND binding.draft_id = draft.id
             AND binding.card_generation_public_id = draft.current_card_generation_public_id
             AND binding.parser_output_id = draft.decision_target_parser_output_id
             AND binding.proposal_version = draft.decision_target_proposal_version
             AND binding.proposal_content_hash = draft.decision_target_proposal_content_hash
             AND binding.authenticated_actor_id = draft.authenticated_actor_id
             AND binding.telegram_account_id = draft.telegram_account_id
             AND binding.telegram_conversation_id = draft.telegram_conversation_id
             AND binding.conversation_binding_id = draft.conversation_binding_id
            JOIN openclaw_human_action_references AS reference
              ON reference.id = binding.reference_id
             AND reference.action = 'confirm'
             AND reference.expires_at > NEW.created_at
            JOIN openclaw_human_action_redemptions AS redemption
              ON redemption.reference_id = reference.id
            JOIN parser_human_draft_cards AS bound_card
              ON bound_card.card_generation_public_id = binding.card_generation_public_id
             AND bound_card.expires_at > NEW.created_at
            WHERE draft.id = NEW.draft_id
              AND NEW.result_completeness = 'complete'
              AND draft.expires_at > NEW.created_at
              AND draft.current_parser_output_id = draft.decision_target_parser_output_id
              AND EXISTS (
                  SELECT 1 FROM parser_human_draft_publications AS publication
                  WHERE publication.draft_id = draft.id
                    AND publication.draft_version = draft.current_draft_version
                    AND publication.parser_output_id = draft.decision_target_parser_output_id
                    AND publication.proposal_version = draft.decision_target_proposal_version
                    AND publication.proposal_content_hash = draft.decision_target_proposal_content_hash
              )
        )
    ) THEN RAISE(ABORT, 'confirmed operation requires redeemed current Confirm reference') END;

    SELECT CASE WHEN NEW.operation_type = 'rejected'
      AND NEW.action_reference_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1
        FROM parser_human_drafts AS draft
        JOIN parser_human_draft_action_bindings AS binding
          ON binding.reference_id = NEW.action_reference_id
         AND binding.draft_id = draft.id
         AND binding.parser_output_id = draft.decision_target_parser_output_id
         AND binding.proposal_version = draft.decision_target_proposal_version
         AND binding.proposal_content_hash = draft.decision_target_proposal_content_hash
         AND binding.authenticated_actor_id = draft.authenticated_actor_id
         AND binding.telegram_account_id = draft.telegram_account_id
         AND binding.telegram_conversation_id = draft.telegram_conversation_id
         AND binding.conversation_binding_id = draft.conversation_binding_id
        JOIN openclaw_human_action_references AS reference
          ON reference.id = binding.reference_id
         AND reference.action = 'reject'
         AND reference.expires_at > NEW.created_at
        JOIN openclaw_human_action_redemptions AS redemption
          ON redemption.reference_id = reference.id
        JOIN parser_human_draft_cards AS bound_card
          ON bound_card.card_generation_public_id = binding.card_generation_public_id
         AND bound_card.expires_at > NEW.created_at
        WHERE draft.id = NEW.draft_id
      ) THEN RAISE(ABORT, 'rejected operation has invalid Reject reference') END;

    SELECT CASE WHEN NEW.operation_type = 'rejected'
      AND NEW.action_reference_id IS NULL
      AND (
        SELECT COUNT(*)
        FROM parser_human_drafts AS candidate
        WHERE candidate.state = 'active'
          AND candidate.authenticated_actor_id = NEW.authenticated_actor_id
          AND candidate.decision_target_parser_output_id = (
              SELECT parser_output_id
              FROM parser_proposal_authorizations
              WHERE confirmation_public_id = NEW.decision_public_id
          )
          AND candidate.decision_target_proposal_content_hash = (
              SELECT proposal_content_hash
              FROM parser_proposal_authorizations
              WHERE confirmation_public_id = NEW.decision_public_id
          )
      ) != 1 THEN RAISE(ABORT, 'legacy Reject draft ownership is ambiguous') END;
END;

CREATE TRIGGER trg_parser_human_draft_start_action_evidence
    BEFORE INSERT ON parser_human_draft_operations
    WHEN NEW.operation_type = 'start'
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM parser_human_drafts AS draft
        JOIN openclaw_human_action_references AS reference
          ON reference.id = draft.source_edit_reference_id
         AND reference.id = NEW.action_reference_id
         AND reference.action = 'edit'
         AND reference.authenticated_actor_id = draft.authenticated_actor_id
         AND reference.channel_account_id = draft.telegram_account_id
         AND reference.channel_conversation_id = draft.telegram_conversation_id
         AND reference.conversation_binding_id = draft.conversation_binding_id
        JOIN openclaw_human_action_redemptions AS redemption
          ON redemption.reference_id = reference.id
         AND redemption.callback_message_id = NEW.telegram_message_id
        WHERE draft.id = NEW.draft_id
          AND draft.start_redemption_public_id = NEW.operation_public_id
          AND draft.start_redemption_material_hash = NEW.request_material_hash
          AND draft.current_card_generation_public_id = NEW.result_card_generation_public_id
          AND draft.authenticated_actor_id = NEW.authenticated_actor_id
          AND draft.telegram_account_id = NEW.telegram_account_id
          AND draft.telegram_conversation_id = NEW.telegram_conversation_id
          AND draft.conversation_binding_id = NEW.conversation_binding_id
    ) THEN RAISE(ABORT, 'start operation lacks original redeemed Edit evidence') END;
END;

-- SQLite's REPLACE conflict handler deletes colliding rows before inserting
-- their replacement.  DELETE triggers do not fire for that implicit delete
-- unless recursive_triggers is enabled, so every protected unique identity
-- must reject its collision on the BEFORE INSERT path as well.
CREATE TRIGGER trg_parser_human_drafts_no_insert_collision
    BEFORE INSERT ON parser_human_drafts
    WHEN EXISTS (
        SELECT 1 FROM parser_human_drafts AS existing
        WHERE (NEW.id IS NOT NULL AND existing.id = NEW.id)
           OR existing.draft_public_id = NEW.draft_public_id
           OR existing.source_edit_reference_id = NEW.source_edit_reference_id
           OR existing.start_redemption_public_id = NEW.start_redemption_public_id
           OR (
               NEW.state = 'active'
               AND existing.state = 'active'
               AND existing.source_parser_output_id = NEW.source_parser_output_id
               AND existing.authenticated_actor_id = NEW.authenticated_actor_id
               AND existing.telegram_account_id = NEW.telegram_account_id
               AND existing.telegram_conversation_id = NEW.telegram_conversation_id
               AND existing.conversation_binding_id = NEW.conversation_binding_id
           )
    )
BEGIN
    SELECT RAISE(ABORT, 'parser human draft insert collision violates append-only identity');
END;

CREATE TRIGGER trg_parser_human_draft_reply_evidence_no_insert_collision
    BEFORE INSERT ON parser_human_draft_reply_evidence
    WHEN EXISTS (
        SELECT 1 FROM parser_human_draft_reply_evidence AS existing
        WHERE (NEW.id IS NOT NULL AND existing.id = NEW.id)
           OR existing.evidence_public_id = NEW.evidence_public_id
           OR (
               existing.authenticated_actor_id = NEW.authenticated_actor_id
               AND existing.telegram_account_id = NEW.telegram_account_id
               AND existing.telegram_conversation_id = NEW.telegram_conversation_id
               AND existing.conversation_binding_id = NEW.conversation_binding_id
               AND existing.telegram_message_id = NEW.telegram_message_id
           )
    )
BEGIN
    SELECT RAISE(ABORT, 'parser human draft reply evidence insert collision violates append-only identity');
END;

CREATE TRIGGER trg_parser_human_draft_operations_no_insert_collision
    BEFORE INSERT ON parser_human_draft_operations
    WHEN EXISTS (
        SELECT 1 FROM parser_human_draft_operations AS existing
        WHERE (NEW.id IS NOT NULL AND existing.id = NEW.id)
           OR existing.operation_public_id = NEW.operation_public_id
           OR (
               NEW.human_reply_evidence_id IS NOT NULL
               AND existing.human_reply_evidence_id = NEW.human_reply_evidence_id
           )
           OR (
               NEW.telegram_message_id IS NOT NULL
               AND existing.draft_id = NEW.draft_id
               AND existing.telegram_message_id = NEW.telegram_message_id
           )
    )
BEGIN
    SELECT RAISE(ABORT, 'parser human draft operation insert collision violates append-only identity');
END;

CREATE TRIGGER trg_parser_human_draft_cards_no_insert_collision
    BEFORE INSERT ON parser_human_draft_cards
    WHEN EXISTS (
        SELECT 1 FROM parser_human_draft_cards AS existing
        WHERE (NEW.id IS NOT NULL AND existing.id = NEW.id)
           OR existing.card_generation_public_id = NEW.card_generation_public_id
           OR (
               NEW.predecessor_card_id IS NOT NULL
               AND existing.predecessor_card_id = NEW.predecessor_card_id
           )
           OR (
               NEW.recovery_public_id IS NOT NULL
               AND existing.recovery_public_id = NEW.recovery_public_id
           )
    )
BEGIN
    SELECT RAISE(ABORT, 'parser human draft card insert collision violates append-only identity');
END;

CREATE TRIGGER trg_parser_human_draft_delivery_attempts_no_insert_collision
    BEFORE INSERT ON parser_human_draft_card_delivery_attempts
    WHEN EXISTS (
        SELECT 1 FROM parser_human_draft_card_delivery_attempts AS existing
        WHERE (NEW.id IS NOT NULL AND existing.id = NEW.id)
           OR existing.attempt_public_id = NEW.attempt_public_id
           OR existing.delivery_identity = NEW.delivery_identity
           OR (
               existing.card_generation_public_id = NEW.card_generation_public_id
               AND existing.transport_mode = NEW.transport_mode
           )
    )
BEGIN
    SELECT RAISE(ABORT, 'parser human draft delivery attempt insert collision violates append-only identity');
END;

CREATE TRIGGER trg_parser_human_draft_delivery_outcomes_no_insert_collision
    BEFORE INSERT ON parser_human_draft_card_delivery_outcomes
    WHEN EXISTS (
        SELECT 1 FROM parser_human_draft_card_delivery_outcomes AS existing
        WHERE (NEW.id IS NOT NULL AND existing.id = NEW.id)
           OR existing.observation_public_id = NEW.observation_public_id
           OR (
               existing.attempt_public_id = NEW.attempt_public_id
               AND existing.observation_slot = NEW.observation_slot
           )
    )
BEGIN
    SELECT RAISE(ABORT, 'parser human draft delivery outcome insert collision violates append-only identity');
END;

CREATE TRIGGER trg_parser_human_draft_publications_no_insert_collision
    BEFORE INSERT ON parser_human_draft_publications
    WHEN EXISTS (
        SELECT 1 FROM parser_human_draft_publications AS existing
        WHERE (NEW.id IS NOT NULL AND existing.id = NEW.id)
           OR existing.publication_public_id = NEW.publication_public_id
           OR existing.operation_id = NEW.operation_id
           OR existing.parser_output_id = NEW.parser_output_id
           OR (
               existing.draft_id = NEW.draft_id
               AND existing.draft_version = NEW.draft_version
           )
    )
BEGIN
    SELECT RAISE(ABORT, 'parser human draft publication insert collision violates append-only identity');
END;

CREATE TRIGGER trg_parser_human_draft_action_bindings_no_insert_collision
    BEFORE INSERT ON parser_human_draft_action_bindings
    WHEN EXISTS (
        SELECT 1 FROM parser_human_draft_action_bindings AS existing
        WHERE (NEW.id IS NOT NULL AND existing.id = NEW.id)
           OR existing.reference_id = NEW.reference_id
    )
BEGIN
    SELECT RAISE(ABORT, 'parser human draft action binding insert collision violates append-only identity');
END;

CREATE TRIGGER trg_parser_human_draft_reply_evidence_no_update
    BEFORE UPDATE ON parser_human_draft_reply_evidence
BEGIN SELECT RAISE(ABORT, 'parser human draft reply evidence is append-only'); END;
CREATE TRIGGER trg_parser_human_draft_reply_evidence_no_delete
    BEFORE DELETE ON parser_human_draft_reply_evidence
BEGIN SELECT RAISE(ABORT, 'parser human draft reply evidence is append-only'); END;
CREATE TRIGGER trg_parser_human_draft_operations_no_update
    BEFORE UPDATE ON parser_human_draft_operations
BEGIN SELECT RAISE(ABORT, 'parser human draft operations are append-only'); END;
CREATE TRIGGER trg_parser_human_draft_operations_no_delete
    BEFORE DELETE ON parser_human_draft_operations
BEGIN SELECT RAISE(ABORT, 'parser human draft operations are append-only'); END;
CREATE TRIGGER trg_parser_human_draft_cards_no_update
    BEFORE UPDATE ON parser_human_draft_cards
BEGIN SELECT RAISE(ABORT, 'parser human draft cards are append-only'); END;
CREATE TRIGGER trg_parser_human_draft_cards_no_delete
    BEFORE DELETE ON parser_human_draft_cards
BEGIN SELECT RAISE(ABORT, 'parser human draft cards are append-only'); END;
CREATE TRIGGER trg_parser_human_draft_delivery_attempts_no_update
    BEFORE UPDATE ON parser_human_draft_card_delivery_attempts
BEGIN SELECT RAISE(ABORT, 'parser human draft delivery attempts are append-only'); END;
CREATE TRIGGER trg_parser_human_draft_delivery_attempts_no_delete
    BEFORE DELETE ON parser_human_draft_card_delivery_attempts
BEGIN SELECT RAISE(ABORT, 'parser human draft delivery attempts are append-only'); END;
CREATE TRIGGER trg_parser_human_draft_delivery_outcomes_no_update
    BEFORE UPDATE ON parser_human_draft_card_delivery_outcomes
BEGIN SELECT RAISE(ABORT, 'parser human draft delivery outcomes are append-only'); END;
CREATE TRIGGER trg_parser_human_draft_delivery_outcomes_no_delete
    BEFORE DELETE ON parser_human_draft_card_delivery_outcomes
BEGIN SELECT RAISE(ABORT, 'parser human draft delivery outcomes are append-only'); END;
CREATE TRIGGER trg_parser_human_draft_publications_no_update
    BEFORE UPDATE ON parser_human_draft_publications
BEGIN SELECT RAISE(ABORT, 'parser human draft publications are append-only'); END;
CREATE TRIGGER trg_parser_human_draft_publications_no_delete
    BEFORE DELETE ON parser_human_draft_publications
BEGIN SELECT RAISE(ABORT, 'parser human draft publications are append-only'); END;
CREATE TRIGGER trg_parser_human_draft_action_bindings_no_update
    BEFORE UPDATE ON parser_human_draft_action_bindings
BEGIN SELECT RAISE(ABORT, 'parser human draft action bindings are append-only'); END;
CREATE TRIGGER trg_parser_human_draft_action_bindings_no_delete
    BEFORE DELETE ON parser_human_draft_action_bindings
BEGIN SELECT RAISE(ABORT, 'parser human draft action bindings are append-only'); END;
