PRAGMA foreign_keys = ON;

-- D2 durable one-confirmation posting authority.
--
-- Reviews are non-authoritative, immutable projections.  A human action
-- reference must be bound to exactly one review before it leaves Finance.
-- Only the redemption transaction may create a decision and its posting
-- attempt.  Downstream financial services retain ownership of their facts;
-- the tables below bind those facts to the single accepted decision and make
-- crash recovery observable without treating coordination state as authority.

CREATE TABLE d2_posting_reviews (
    review_public_id TEXT PRIMARY KEY CHECK (
        length(review_public_id) = 36
        AND review_public_id GLOB 'd2rev_[0-9a-f]*'
        AND review_public_id NOT GLOB 'd2rev_*[^0-9a-f]*'
    ),
    review_idempotency_key TEXT NOT NULL UNIQUE CHECK (length(trim(review_idempotency_key)) > 0),
    card_generation_public_id TEXT NOT NULL UNIQUE,
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
    FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id),
    CHECK (
        (posting_path = 'text' AND receipt_fact_candidate_json IS NULL)
        OR (posting_path = 'personal_receipt' AND receipt_fact_candidate_json IS NOT NULL)
    )
) STRICT;

CREATE TABLE d2_posting_review_action_bindings (
    review_public_id TEXT PRIMARY KEY REFERENCES d2_posting_reviews(review_public_id),
    reference_id INTEGER NOT NULL UNIQUE REFERENCES openclaw_human_action_references(id),
    bound_at TEXT NOT NULL
) STRICT;

CREATE TABLE d2_posting_attempts (
    attempt_public_id TEXT PRIMARY KEY CHECK (
        length(attempt_public_id) = 36
        AND attempt_public_id GLOB 'd2att_[0-9a-f]*'
        AND attempt_public_id NOT GLOB 'd2att_*[^0-9a-f]*'
    ),
    review_public_id TEXT NOT NULL UNIQUE REFERENCES d2_posting_reviews(review_public_id),
    reference_id INTEGER NOT NULL UNIQUE REFERENCES openclaw_human_action_references(id),
    posting_path TEXT NOT NULL CHECK (posting_path IN ('text', 'personal_receipt')),
    stage TEXT NOT NULL CHECK (stage IN (
        'accepted', 'conversion_persisted', 'fact_set_persisted',
        'snapshot_persisted', 'conditional_authorization_persisted',
        'finalized', 'needs_attention'
    )),
    row_version INTEGER NOT NULL DEFAULT 0 CHECK (row_version >= 0),
    transaction_public_id TEXT,
    attention_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((stage = 'finalized') = (transaction_public_id IS NOT NULL)),
    CHECK ((stage = 'needs_attention') = (attention_reason IS NOT NULL))
) STRICT;

CREATE TABLE d2_posting_decisions (
    decision_public_id TEXT PRIMARY KEY CHECK (
        length(decision_public_id) = 36
        AND decision_public_id GLOB 'd2dec_[0-9a-f]*'
        AND decision_public_id NOT GLOB 'd2dec_*[^0-9a-f]*'
    ),
    review_public_id TEXT NOT NULL UNIQUE REFERENCES d2_posting_reviews(review_public_id),
    attempt_public_id TEXT NOT NULL UNIQUE REFERENCES d2_posting_attempts(attempt_public_id),
    reference_id INTEGER NOT NULL UNIQUE REFERENCES openclaw_human_action_references(id),
    confirmation_public_id TEXT NOT NULL UNIQUE
        REFERENCES parser_proposal_authorizations(confirmation_public_id),
    accepted_at INTEGER NOT NULL CHECK (accepted_at > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (reference_id) REFERENCES openclaw_human_action_redemptions(reference_id)
) STRICT;

CREATE TABLE d2_posting_receipt_evidence (
    decision_public_id TEXT NOT NULL REFERENCES d2_posting_decisions(decision_public_id),
    evidence_type TEXT NOT NULL CHECK (evidence_type IN ('conversion', 'fact_set')),
    conversion_command_public_id TEXT
        REFERENCES receipt_proposal_conversions(command_public_id),
    fact_set_public_id TEXT
        REFERENCES receipt_item_allocation_fact_sets(fact_set_public_id),
    evidence_hash TEXT NOT NULL CHECK (
        length(evidence_hash) = 64 AND evidence_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    PRIMARY KEY (decision_public_id, evidence_type),
    CHECK (
        (evidence_type = 'conversion' AND conversion_command_public_id IS NOT NULL
            AND fact_set_public_id IS NULL)
        OR (evidence_type = 'fact_set' AND fact_set_public_id IS NOT NULL
            AND conversion_command_public_id IS NULL)
    )
) STRICT;

CREATE TABLE d2_conditional_authorization_proofs (
    authorization_id TEXT PRIMARY KEY
        REFERENCES receipt_finalization_authorizations(authorization_id),
    decision_public_id TEXT NOT NULL UNIQUE
        REFERENCES d2_posting_decisions(decision_public_id),
    review_public_id TEXT NOT NULL UNIQUE REFERENCES d2_posting_reviews(review_public_id),
    fact_set_public_id TEXT NOT NULL UNIQUE
        REFERENCES receipt_item_allocation_fact_sets(fact_set_public_id),
    calculation_snapshot_id TEXT NOT NULL UNIQUE
        REFERENCES authoritative_calculation_snapshots(snapshot_public_id),
    reviewed_projection_hash TEXT NOT NULL CHECK (
        length(reviewed_projection_hash) = 64
        AND reviewed_projection_hash NOT GLOB '*[^0-9a-f]*'
    ),
    snapshot_projection_hash TEXT NOT NULL CHECK (
        length(snapshot_projection_hash) = 64
        AND snapshot_projection_hash NOT GLOB '*[^0-9a-f]*'
    ),
    equality_proof_hash TEXT NOT NULL UNIQUE CHECK (
        length(equality_proof_hash) = 64
        AND equality_proof_hash NOT GLOB '*[^0-9a-f]*'
    ),
    proof_version TEXT NOT NULL CHECK (proof_version = 'd2_conditional_v1'),
    created_at TEXT NOT NULL,
    CHECK (reviewed_projection_hash = snapshot_projection_hash)
) STRICT;

CREATE TABLE d2_posting_attempt_events (
    event_public_id TEXT PRIMARY KEY CHECK (
        length(event_public_id) = 36
        AND event_public_id GLOB 'd2evt_[0-9a-f]*'
        AND event_public_id NOT GLOB 'd2evt_*[^0-9a-f]*'
    ),
    attempt_public_id TEXT NOT NULL REFERENCES d2_posting_attempts(attempt_public_id),
    from_stage TEXT,
    to_stage TEXT NOT NULL,
    row_version INTEGER NOT NULL CHECK (row_version >= 0),
    evidence_public_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (attempt_public_id, row_version)
) STRICT;

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
       OR existing.card_generation_public_id = NEW.card_generation_public_id
) BEGIN
    SELECT RAISE(ABORT, 'D2 posting review identity collision');
END;
CREATE TRIGGER trg_d2_posting_reviews_no_delete
BEFORE DELETE ON d2_posting_reviews BEGIN
    SELECT RAISE(ABORT, 'D2 posting reviews are append-only');
END;

CREATE TRIGGER trg_d2_review_action_bindings_no_update
BEFORE UPDATE ON d2_posting_review_action_bindings BEGIN
    SELECT RAISE(ABORT, 'D2 review action bindings are append-only');
END;
CREATE TRIGGER trg_d2_review_action_bindings_no_insert_collision
BEFORE INSERT ON d2_posting_review_action_bindings
WHEN EXISTS (
    SELECT 1 FROM d2_posting_review_action_bindings AS existing
    WHERE existing.review_public_id = NEW.review_public_id
       OR existing.reference_id = NEW.reference_id
) BEGIN
    SELECT RAISE(ABORT, 'D2 review action binding identity collision');
END;
CREATE TRIGGER trg_d2_review_action_bindings_no_delete
BEFORE DELETE ON d2_posting_review_action_bindings BEGIN
    SELECT RAISE(ABORT, 'D2 review action bindings are append-only');
END;

CREATE TRIGGER trg_d2_review_action_binding_is_confirm
BEFORE INSERT ON d2_posting_review_action_bindings
WHEN NOT EXISTS (
    SELECT 1 FROM openclaw_human_action_references AS refs
    WHERE refs.id = NEW.reference_id AND refs.action = 'confirm'
) BEGIN
    SELECT RAISE(ABORT, 'D2 review may bind only a confirm reference');
END;

CREATE TRIGGER trg_d2_posting_decisions_no_update
BEFORE UPDATE ON d2_posting_decisions BEGIN
    SELECT RAISE(ABORT, 'D2 posting decisions are append-only');
END;
CREATE TRIGGER trg_d2_posting_decisions_no_insert_collision
BEFORE INSERT ON d2_posting_decisions
WHEN EXISTS (
    SELECT 1 FROM d2_posting_decisions AS existing
    WHERE existing.decision_public_id = NEW.decision_public_id
       OR existing.review_public_id = NEW.review_public_id
       OR existing.attempt_public_id = NEW.attempt_public_id
       OR existing.reference_id = NEW.reference_id
       OR existing.confirmation_public_id = NEW.confirmation_public_id
) BEGIN
    SELECT RAISE(ABORT, 'D2 posting decision identity collision');
END;
CREATE TRIGGER trg_d2_posting_decisions_no_delete
BEFORE DELETE ON d2_posting_decisions BEGIN
    SELECT RAISE(ABORT, 'D2 posting decisions are append-only');
END;

CREATE TRIGGER trg_d2_receipt_evidence_no_update
BEFORE UPDATE ON d2_posting_receipt_evidence BEGIN
    SELECT RAISE(ABORT, 'D2 receipt evidence is append-only');
END;
CREATE TRIGGER trg_d2_receipt_evidence_no_insert_collision
BEFORE INSERT ON d2_posting_receipt_evidence
WHEN EXISTS (
    SELECT 1 FROM d2_posting_receipt_evidence AS existing
    WHERE existing.decision_public_id = NEW.decision_public_id
      AND existing.evidence_type = NEW.evidence_type
) BEGIN
    SELECT RAISE(ABORT, 'D2 receipt evidence identity collision');
END;
CREATE TRIGGER trg_d2_receipt_evidence_no_delete
BEFORE DELETE ON d2_posting_receipt_evidence BEGIN
    SELECT RAISE(ABORT, 'D2 receipt evidence is append-only');
END;

CREATE TRIGGER trg_d2_conditional_proofs_no_update
BEFORE UPDATE ON d2_conditional_authorization_proofs BEGIN
    SELECT RAISE(ABORT, 'D2 conditional authorization proofs are append-only');
END;
CREATE TRIGGER trg_d2_conditional_proofs_no_insert_collision
BEFORE INSERT ON d2_conditional_authorization_proofs
WHEN EXISTS (
    SELECT 1 FROM d2_conditional_authorization_proofs AS existing
    WHERE existing.authorization_id = NEW.authorization_id
       OR existing.decision_public_id = NEW.decision_public_id
       OR existing.review_public_id = NEW.review_public_id
       OR existing.fact_set_public_id = NEW.fact_set_public_id
       OR existing.calculation_snapshot_id = NEW.calculation_snapshot_id
       OR existing.equality_proof_hash = NEW.equality_proof_hash
) BEGIN
    SELECT RAISE(ABORT, 'D2 conditional proof identity collision');
END;
CREATE TRIGGER trg_d2_conditional_proofs_no_delete
BEFORE DELETE ON d2_conditional_authorization_proofs BEGIN
    SELECT RAISE(ABORT, 'D2 conditional authorization proofs are append-only');
END;

CREATE TRIGGER trg_d2_attempt_events_no_update
BEFORE UPDATE ON d2_posting_attempt_events BEGIN
    SELECT RAISE(ABORT, 'D2 posting attempt events are append-only');
END;
CREATE TRIGGER trg_d2_attempt_events_no_insert_collision
BEFORE INSERT ON d2_posting_attempt_events
WHEN EXISTS (
    SELECT 1 FROM d2_posting_attempt_events AS existing
    WHERE existing.event_public_id = NEW.event_public_id
       OR (existing.attempt_public_id = NEW.attempt_public_id
           AND existing.row_version = NEW.row_version)
) BEGIN
    SELECT RAISE(ABORT, 'D2 posting attempt event identity collision');
END;
CREATE TRIGGER trg_d2_attempt_events_no_delete
BEFORE DELETE ON d2_posting_attempt_events BEGIN
    SELECT RAISE(ABORT, 'D2 posting attempt events are append-only');
END;

CREATE TRIGGER trg_d2_posting_attempts_no_delete
BEFORE DELETE ON d2_posting_attempts BEGIN
    SELECT RAISE(ABORT, 'D2 posting attempts cannot be deleted');
END;
CREATE TRIGGER trg_d2_posting_attempts_no_insert_collision
BEFORE INSERT ON d2_posting_attempts
WHEN EXISTS (
    SELECT 1 FROM d2_posting_attempts AS existing
    WHERE existing.attempt_public_id = NEW.attempt_public_id
       OR existing.review_public_id = NEW.review_public_id
       OR existing.reference_id = NEW.reference_id
) BEGIN
    SELECT RAISE(ABORT, 'D2 posting attempt identity collision');
END;

CREATE TRIGGER trg_d2_posting_attempts_guarded_update
BEFORE UPDATE ON d2_posting_attempts
WHEN NOT (
    NEW.attempt_public_id IS OLD.attempt_public_id
    AND NEW.review_public_id IS OLD.review_public_id
    AND NEW.reference_id IS OLD.reference_id
    AND NEW.posting_path IS OLD.posting_path
    AND NEW.created_at IS OLD.created_at
    AND NEW.row_version = OLD.row_version + 1
    AND (
        (OLD.stage = 'accepted' AND NEW.stage IN ('conversion_persisted', 'finalized', 'needs_attention'))
        OR (OLD.stage = 'conversion_persisted' AND NEW.stage IN ('fact_set_persisted', 'needs_attention'))
        OR (OLD.stage = 'fact_set_persisted' AND NEW.stage IN ('snapshot_persisted', 'needs_attention'))
        OR (OLD.stage = 'snapshot_persisted' AND NEW.stage IN ('conditional_authorization_persisted', 'needs_attention'))
        OR (OLD.stage = 'conditional_authorization_persisted' AND NEW.stage IN ('finalized', 'needs_attention'))
        OR (OLD.stage = 'needs_attention' AND NEW.stage = 'needs_attention')
    )
) BEGIN
    SELECT RAISE(ABORT, 'invalid D2 posting attempt transition');
END;
