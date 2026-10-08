-- Independent, staging-only decision consumption and recoverable posting.
CREATE TABLE application_posting_reviews (
 review_id TEXT PRIMARY KEY,
 parser_output_id INTEGER NOT NULL REFERENCES parser_outputs(id),
 material_json TEXT NOT NULL CHECK(json_valid(material_json)),
 review_hash TEXT NOT NULL UNIQUE CHECK(length(review_hash)=64)
);
CREATE TABLE application_posting_attempts (
 attempt_id TEXT PRIMARY KEY,
 review_id TEXT NOT NULL UNIQUE REFERENCES application_posting_reviews(review_id),
 source_event_key TEXT NOT NULL UNIQUE,
 intake_public_id TEXT NOT NULL UNIQUE REFERENCES raw_intake_records(public_id),
 stage TEXT NOT NULL CHECK(stage IN ('accepted','conversion_persisted','fact_set_persisted','snapshot_persisted','conditional_authorization_persisted','finalized','needs_attention')),
 transaction_public_id TEXT REFERENCES transactions(public_id),
 attention_reason TEXT,
 CHECK((stage='needs_attention')=(attention_reason IS NOT NULL)),
 CHECK(stage!='finalized' OR transaction_public_id IS NOT NULL)
);
CREATE TABLE application_posting_decisions (
 attempt_id TEXT PRIMARY KEY REFERENCES application_posting_attempts(attempt_id),
 decision_namespace TEXT NOT NULL,
 decision_id TEXT NOT NULL,
 decision_digest TEXT NOT NULL CHECK(length(decision_digest)=64),
 confirmation_public_id TEXT NOT NULL UNIQUE REFERENCES parser_proposal_authorizations(confirmation_public_id) DEFERRABLE INITIALLY DEFERRED,
 material_json TEXT NOT NULL CHECK(json_valid(material_json)),
 accepted_at INTEGER NOT NULL CHECK(accepted_at>0),
 UNIQUE(decision_namespace,decision_id)
);
CREATE TABLE application_posting_events (
 id INTEGER PRIMARY KEY,
 attempt_id TEXT NOT NULL REFERENCES application_posting_attempts(attempt_id),
 from_stage TEXT,
 to_stage TEXT NOT NULL,
 evidence_public_id TEXT,
 created_at INTEGER NOT NULL CHECK(created_at>0),
 UNIQUE(attempt_id,to_stage)
);
CREATE TABLE application_posting_receipt_evidence (
 attempt_id TEXT NOT NULL REFERENCES application_posting_attempts(attempt_id),
 evidence_type TEXT NOT NULL CHECK(evidence_type IN ('conversion','fact_set','snapshot','authorization')),
 evidence_public_id TEXT NOT NULL,
 evidence_hash TEXT NOT NULL CHECK(length(evidence_hash)=64),
 material_json TEXT NOT NULL CHECK(json_valid(material_json)),
 PRIMARY KEY(attempt_id,evidence_type)
);
CREATE TRIGGER application_posting_reviews_no_update BEFORE UPDATE ON application_posting_reviews BEGIN SELECT RAISE(ABORT,'immutable posting review'); END;
CREATE TRIGGER application_posting_reviews_no_delete BEFORE DELETE ON application_posting_reviews BEGIN SELECT RAISE(ABORT,'immutable posting review'); END;
CREATE TRIGGER application_posting_decisions_no_update BEFORE UPDATE ON application_posting_decisions BEGIN SELECT RAISE(ABORT,'immutable posting decision'); END;
CREATE TRIGGER application_posting_decisions_no_delete BEFORE DELETE ON application_posting_decisions BEGIN SELECT RAISE(ABORT,'immutable posting decision'); END;
CREATE TRIGGER application_posting_events_no_update BEFORE UPDATE ON application_posting_events BEGIN SELECT RAISE(ABORT,'immutable posting event'); END;
CREATE TRIGGER application_posting_events_no_delete BEFORE DELETE ON application_posting_events BEGIN SELECT RAISE(ABORT,'immutable posting event'); END;
CREATE TRIGGER application_posting_evidence_no_update BEFORE UPDATE ON application_posting_receipt_evidence BEGIN SELECT RAISE(ABORT,'immutable posting evidence'); END;
CREATE TRIGGER application_posting_evidence_no_delete BEFORE DELETE ON application_posting_receipt_evidence BEGIN SELECT RAISE(ABORT,'immutable posting evidence'); END;
CREATE TRIGGER application_posting_attempts_no_delete BEFORE DELETE ON application_posting_attempts BEGIN SELECT RAISE(ABORT,'posting attempt cannot be deleted'); END;
CREATE TRIGGER application_posting_attempts_guard_update BEFORE UPDATE ON application_posting_attempts
WHEN NEW.attempt_id!=OLD.attempt_id OR NEW.review_id!=OLD.review_id OR NEW.source_event_key!=OLD.source_event_key
 OR NEW.intake_public_id!=OLD.intake_public_id
 OR OLD.stage IN ('finalized','needs_attention')
 OR NOT (NEW.stage='needs_attention' OR (OLD.stage='accepted' AND NEW.stage IN ('conversion_persisted','finalized'))
 OR (OLD.stage='conversion_persisted' AND NEW.stage='fact_set_persisted')
 OR (OLD.stage='fact_set_persisted' AND NEW.stage='snapshot_persisted')
 OR (OLD.stage='snapshot_persisted' AND NEW.stage='conditional_authorization_persisted')
 OR (OLD.stage='conditional_authorization_persisted' AND NEW.stage='finalized'))
BEGIN SELECT RAISE(ABORT,'invalid posting attempt transition'); END;
CREATE TABLE application_conditional_authorization_proofs (
 authorization_id TEXT PRIMARY KEY REFERENCES receipt_finalization_authorizations(authorization_id),
 attempt_id TEXT NOT NULL UNIQUE REFERENCES application_posting_attempts(attempt_id),
 review_id TEXT NOT NULL UNIQUE REFERENCES application_posting_reviews(review_id),
 confirmation_public_id TEXT NOT NULL UNIQUE REFERENCES parser_proposal_authorizations(confirmation_public_id),
 fact_set_public_id TEXT NOT NULL UNIQUE REFERENCES receipt_item_allocation_fact_sets(fact_set_public_id),
 fact_set_version INTEGER NOT NULL CHECK(fact_set_version>0),
 fact_set_input_hash TEXT NOT NULL CHECK(length(fact_set_input_hash)=64),
 fact_set_result_hash TEXT NOT NULL CHECK(length(fact_set_result_hash)=64),
 calculation_snapshot_id TEXT NOT NULL UNIQUE REFERENCES authoritative_calculation_snapshots(snapshot_public_id),
 calculation_snapshot_hash TEXT NOT NULL CHECK(length(calculation_snapshot_hash)=64),
 reviewed_projection_hash TEXT NOT NULL CHECK(length(reviewed_projection_hash)=64),
 snapshot_projection_hash TEXT NOT NULL CHECK(length(snapshot_projection_hash)=64),
 payer_participant_public_id TEXT NOT NULL REFERENCES participants(public_id),
 payer_was_active_self INTEGER NOT NULL CHECK(payer_was_active_self=1),
 active_self_count INTEGER NOT NULL CHECK(active_self_count=1),
 participant_authority_hash TEXT NOT NULL UNIQUE CHECK(length(participant_authority_hash)=64),
 equality_proof_hash TEXT NOT NULL UNIQUE CHECK(length(equality_proof_hash)=64),
 proof_version TEXT NOT NULL CHECK(proof_version='application_conditional_v1'),
 created_at TEXT NOT NULL
);
CREATE TRIGGER application_conditional_proofs_no_update BEFORE UPDATE ON application_conditional_authorization_proofs BEGIN SELECT RAISE(ABORT,'application conditional proofs immutable'); END;
CREATE TRIGGER application_conditional_proofs_no_delete BEFORE DELETE ON application_conditional_authorization_proofs BEGIN SELECT RAISE(ABORT,'application conditional proofs immutable'); END;
CREATE TRIGGER application_posting_reviews_no_collision BEFORE INSERT ON application_posting_reviews
WHEN EXISTS(SELECT 1 FROM application_posting_reviews WHERE review_id=NEW.review_id OR review_hash=NEW.review_hash)
BEGIN SELECT RAISE(ABORT,'posting review collision'); END;
CREATE TRIGGER application_posting_attempts_no_collision BEFORE INSERT ON application_posting_attempts
WHEN EXISTS(SELECT 1 FROM application_posting_attempts WHERE attempt_id=NEW.attempt_id OR review_id=NEW.review_id OR source_event_key=NEW.source_event_key OR intake_public_id=NEW.intake_public_id)
BEGIN SELECT RAISE(ABORT,'posting attempt collision'); END;
CREATE TRIGGER application_posting_decisions_no_collision BEFORE INSERT ON application_posting_decisions
WHEN EXISTS(SELECT 1 FROM application_posting_decisions WHERE attempt_id=NEW.attempt_id OR confirmation_public_id=NEW.confirmation_public_id OR (decision_namespace=NEW.decision_namespace AND decision_id=NEW.decision_id))
BEGIN SELECT RAISE(ABORT,'posting decision collision'); END;
CREATE TRIGGER application_posting_evidence_no_collision BEFORE INSERT ON application_posting_receipt_evidence
WHEN EXISTS(SELECT 1 FROM application_posting_receipt_evidence WHERE attempt_id=NEW.attempt_id AND evidence_type=NEW.evidence_type)
BEGIN SELECT RAISE(ABORT,'posting evidence collision'); END;
CREATE TRIGGER application_posting_events_no_collision BEFORE INSERT ON application_posting_events
WHEN EXISTS(SELECT 1 FROM application_posting_events WHERE id=NEW.id OR (attempt_id=NEW.attempt_id AND to_stage=NEW.to_stage))
BEGIN SELECT RAISE(ABORT,'posting event collision'); END;
CREATE TRIGGER application_conditional_proofs_no_collision BEFORE INSERT ON application_conditional_authorization_proofs
WHEN EXISTS(SELECT 1 FROM application_conditional_authorization_proofs WHERE authorization_id=NEW.authorization_id OR attempt_id=NEW.attempt_id OR review_id=NEW.review_id OR confirmation_public_id=NEW.confirmation_public_id OR fact_set_public_id=NEW.fact_set_public_id OR calculation_snapshot_id=NEW.calculation_snapshot_id OR participant_authority_hash=NEW.participant_authority_hash OR equality_proof_hash=NEW.equality_proof_hash)
BEGIN SELECT RAISE(ABORT,'application conditional proof collision'); END;
