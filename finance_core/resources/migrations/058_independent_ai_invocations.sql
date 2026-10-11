-- Independent text-only interpretation and immutable proposal publication.
PRAGMA foreign_keys = OFF;
ALTER TABLE receipt_ocr_proposal_links RENAME TO receipt_ocr_proposal_links_legacy_058;

CREATE TABLE receipt_ocr_proposal_links (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE CHECK (
        length(public_id) BETWEEN 6 AND 200
        AND substr(public_id, 1, 5) = 'ropl_'
        AND public_id NOT GLOB '*[^A-Za-z0-9_-]*'
    ),
    extraction_id INTEGER NOT NULL,
    parser_output_id INTEGER NOT NULL,
    proposal_input_hash TEXT NOT NULL CHECK (
        length(proposal_input_hash) = 64
        AND lower(proposal_input_hash) = proposal_input_hash
        AND proposal_input_hash NOT GLOB '*[^0-9a-f]*'
    ),
    proposal_result_hash TEXT NOT NULL CHECK (
        length(proposal_result_hash) = 64
        AND lower(proposal_result_hash) = proposal_result_hash
        AND proposal_result_hash NOT GLOB '*[^0-9a-f]*'
    ),
    parser_contract_version TEXT NOT NULL CHECK (
        length(parser_contract_version) BETWEEN 1 AND 128
        AND parser_contract_version NOT GLOB '*[^A-Za-z0-9._-]*'
    ),
    link_role TEXT NOT NULL CHECK (
        link_role IN ('initial', 'superseding_correction', 'ai_fallback', 'independent_ai')
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (extraction_id) REFERENCES receipt_ocr_extractions(id),
    FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id),
    UNIQUE (parser_output_id)
);

INSERT INTO receipt_ocr_proposal_links (
    id, public_id, extraction_id, parser_output_id, proposal_input_hash,
    proposal_result_hash, parser_contract_version, link_role, created_at
)
SELECT
    id, public_id, extraction_id, parser_output_id, proposal_input_hash,
    proposal_result_hash, parser_contract_version, link_role, created_at
FROM receipt_ocr_proposal_links_legacy_058;

DROP TABLE receipt_ocr_proposal_links_legacy_058;

CREATE UNIQUE INDEX idx_receipt_ocr_proposal_links_initial_unique
    ON receipt_ocr_proposal_links(extraction_id, parser_contract_version)
    WHERE link_role = 'initial';

CREATE INDEX idx_receipt_ocr_proposal_links_extraction_id
    ON receipt_ocr_proposal_links(extraction_id);

CREATE INDEX idx_receipt_ocr_proposal_links_parser_output_id
    ON receipt_ocr_proposal_links(parser_output_id);

CREATE TRIGGER trg_receipt_ocr_proposal_links_no_update
    BEFORE UPDATE ON receipt_ocr_proposal_links
BEGIN
    SELECT RAISE(ABORT, 'receipt_ocr_proposal_links rows are append-only');
END;

CREATE TRIGGER trg_receipt_ocr_proposal_links_no_delete
    BEFORE DELETE ON receipt_ocr_proposal_links
BEGIN
    SELECT RAISE(ABORT, 'receipt_ocr_proposal_links rows are append-only');
END;

CREATE TRIGGER trg_receipt_ocr_proposal_links_no_insert_collision
    BEFORE INSERT ON receipt_ocr_proposal_links
    WHEN EXISTS (
        SELECT 1
        FROM receipt_ocr_proposal_links
        WHERE id = NEW.id
           OR public_id = NEW.public_id
           OR parser_output_id = NEW.parser_output_id
           OR (
               NEW.link_role = 'initial'
               AND link_role = 'initial'
               AND extraction_id = NEW.extraction_id
               AND parser_contract_version = NEW.parser_contract_version
           )
    )
BEGIN
    SELECT RAISE(ABORT, 'receipt_ocr_proposal_links rows are append-only');
END;

PRAGMA foreign_keys = ON;
CREATE TABLE independent_ai_attempts (
 attempt_id TEXT PRIMARY KEY,
 operation_id TEXT NOT NULL UNIQUE,
 intake_public_id TEXT NOT NULL REFERENCES raw_intake_records(public_id),
 parent_parser_output_id INTEGER NOT NULL REFERENCES parser_outputs(id),
 parent_version INTEGER NOT NULL CHECK(parent_version>=0),
 parent_content_hash TEXT NOT NULL CHECK(length(parent_content_hash)=64),
 source_event_key TEXT NOT NULL CHECK(length(source_event_key)=64),
 kind TEXT NOT NULL CHECK(kind IN ('initial','edit')),
 input_identity TEXT NOT NULL,
 material_json TEXT NOT NULL CHECK(json_valid(material_json)),
 material_hash TEXT NOT NULL UNIQUE CHECK(length(material_hash)=64),
 created_at INTEGER NOT NULL CHECK(created_at>0),
 deadline_at INTEGER NOT NULL CHECK(deadline_at>created_at),
 UNIQUE(parent_parser_output_id,parent_version,parent_content_hash,kind,input_identity),
 UNIQUE(source_event_key,kind,input_identity)
);
CREATE TABLE independent_ai_claims (
 attempt_id TEXT PRIMARY KEY REFERENCES independent_ai_attempts(attempt_id),
 claim_id TEXT NOT NULL UNIQUE,
 claimed_at INTEGER NOT NULL CHECK(claimed_at>0),
 request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
 material_json TEXT NOT NULL CHECK(json_valid(material_json)),
 material_hash TEXT NOT NULL CHECK(length(material_hash)=64)
);
CREATE TABLE independent_ai_events (
 event_id TEXT PRIMARY KEY,
 attempt_id TEXT NOT NULL REFERENCES independent_ai_attempts(attempt_id),
 kind TEXT NOT NULL CHECK(kind IN ('CANCELLED','CANCEL_REQUESTED','UNKNOWN')),
 observed_at INTEGER NOT NULL CHECK(observed_at>0),
 UNIQUE(attempt_id,kind)
);
CREATE TABLE independent_ai_results (
 result_id TEXT PRIMARY KEY,
 attempt_id TEXT NOT NULL REFERENCES independent_ai_attempts(attempt_id),
 observation_record_id TEXT NOT NULL UNIQUE,
 raw_response TEXT,
 raw_response_hash TEXT NOT NULL CHECK(length(raw_response_hash)=64),
 material_json TEXT NOT NULL CHECK(json_valid(material_json)),
 material_hash TEXT NOT NULL UNIQUE CHECK(length(material_hash)=64),
 validation_json TEXT NOT NULL CHECK(json_valid(validation_json)),
 observed_at INTEGER NOT NULL CHECK(observed_at>0)
);
CREATE TABLE independent_ai_publications (
 publication_id TEXT PRIMARY KEY,
 attempt_id TEXT NOT NULL UNIQUE REFERENCES independent_ai_attempts(attempt_id),
 result_id TEXT NOT NULL UNIQUE REFERENCES independent_ai_results(result_id),
 parent_parser_output_id INTEGER NOT NULL UNIQUE REFERENCES parser_outputs(id),
 child_parser_output_id INTEGER NOT NULL UNIQUE REFERENCES parser_outputs(id),
 child_content_hash TEXT NOT NULL CHECK(length(child_content_hash)=64),
 material_json TEXT NOT NULL CHECK(json_valid(material_json)),
 material_hash TEXT NOT NULL UNIQUE CHECK(length(material_hash)=64),
 published_at INTEGER NOT NULL CHECK(published_at>0),
 CHECK(parent_parser_output_id!=child_parser_output_id)
);
CREATE TRIGGER independent_ai_attempts_no_update BEFORE UPDATE ON independent_ai_attempts BEGIN SELECT RAISE(ABORT,'immutable independent AI evidence'); END;
CREATE TRIGGER independent_ai_attempts_no_delete BEFORE DELETE ON independent_ai_attempts BEGIN SELECT RAISE(ABORT,'immutable independent AI evidence'); END;
CREATE TRIGGER independent_ai_attempts_no_collision BEFORE INSERT ON independent_ai_attempts WHEN EXISTS(SELECT 1 FROM independent_ai_attempts WHERE attempt_id=NEW.attempt_id OR operation_id=NEW.operation_id OR material_hash=NEW.material_hash OR (parent_parser_output_id=NEW.parent_parser_output_id AND parent_version=NEW.parent_version AND parent_content_hash=NEW.parent_content_hash AND kind=NEW.kind AND input_identity=NEW.input_identity) OR (source_event_key=NEW.source_event_key AND kind=NEW.kind AND input_identity=NEW.input_identity)) BEGIN SELECT RAISE(ABORT,'independent AI evidence collision'); END;
CREATE TRIGGER independent_ai_claims_no_update BEFORE UPDATE ON independent_ai_claims BEGIN SELECT RAISE(ABORT,'immutable independent AI evidence'); END;
CREATE TRIGGER independent_ai_claims_no_delete BEFORE DELETE ON independent_ai_claims BEGIN SELECT RAISE(ABORT,'immutable independent AI evidence'); END;
CREATE TRIGGER independent_ai_claims_no_collision BEFORE INSERT ON independent_ai_claims WHEN EXISTS(SELECT 1 FROM independent_ai_claims WHERE attempt_id=NEW.attempt_id OR claim_id=NEW.claim_id) BEGIN SELECT RAISE(ABORT,'independent AI evidence collision'); END;
CREATE TRIGGER independent_ai_events_no_update BEFORE UPDATE ON independent_ai_events BEGIN SELECT RAISE(ABORT,'immutable independent AI evidence'); END;
CREATE TRIGGER independent_ai_events_no_delete BEFORE DELETE ON independent_ai_events BEGIN SELECT RAISE(ABORT,'immutable independent AI evidence'); END;
CREATE TRIGGER independent_ai_events_no_collision BEFORE INSERT ON independent_ai_events WHEN EXISTS(SELECT 1 FROM independent_ai_events WHERE event_id=NEW.event_id OR (attempt_id=NEW.attempt_id AND kind=NEW.kind)) BEGIN SELECT RAISE(ABORT,'independent AI evidence collision'); END;
CREATE TRIGGER independent_ai_results_no_update BEFORE UPDATE ON independent_ai_results BEGIN SELECT RAISE(ABORT,'immutable independent AI evidence'); END;
CREATE TRIGGER independent_ai_results_no_delete BEFORE DELETE ON independent_ai_results BEGIN SELECT RAISE(ABORT,'immutable independent AI evidence'); END;
CREATE TRIGGER independent_ai_results_no_collision BEFORE INSERT ON independent_ai_results WHEN EXISTS(SELECT 1 FROM independent_ai_results WHERE result_id=NEW.result_id OR observation_record_id=NEW.observation_record_id OR material_hash=NEW.material_hash) BEGIN SELECT RAISE(ABORT,'independent AI evidence collision'); END;
CREATE TRIGGER independent_ai_publications_no_update BEFORE UPDATE ON independent_ai_publications BEGIN SELECT RAISE(ABORT,'immutable independent AI evidence'); END;
CREATE TRIGGER independent_ai_publications_no_delete BEFORE DELETE ON independent_ai_publications BEGIN SELECT RAISE(ABORT,'immutable independent AI evidence'); END;
CREATE TRIGGER independent_ai_publications_no_collision BEFORE INSERT ON independent_ai_publications WHEN EXISTS(SELECT 1 FROM independent_ai_publications WHERE publication_id=NEW.publication_id OR attempt_id=NEW.attempt_id OR result_id=NEW.result_id OR parent_parser_output_id=NEW.parent_parser_output_id OR child_parser_output_id=NEW.child_parser_output_id OR material_hash=NEW.material_hash) BEGIN SELECT RAISE(ABORT,'independent AI evidence collision'); END;
CREATE TRIGGER independent_ai_parser_no_hash_update BEFORE UPDATE ON parser_outputs
WHEN (EXISTS(SELECT 1 FROM independent_ai_attempts WHERE parent_parser_output_id=OLD.id)
 OR EXISTS(SELECT 1 FROM independent_ai_publications WHERE child_parser_output_id=OLD.id))
AND (NEW.id IS NOT OLD.id OR NEW.public_id IS NOT OLD.public_id OR NEW.source_type IS NOT OLD.source_type
 OR NEW.source_public_id IS NOT OLD.source_public_id OR NEW.statement_batch_id IS NOT OLD.statement_batch_id
 OR NEW.attachment_id IS NOT OLD.attachment_id OR NEW.parser_name IS NOT OLD.parser_name
 OR NEW.parser_version IS NOT OLD.parser_version OR NEW.ai_provider IS NOT OLD.ai_provider
 OR NEW.ai_model IS NOT OLD.ai_model OR NEW.prompt_version IS NOT OLD.prompt_version
 OR NEW.raw_text IS NOT OLD.raw_text OR NEW.parsed_payload IS NOT OLD.parsed_payload
 OR NEW.normalized_payload IS NOT OLD.normalized_payload OR NEW.confidence_score IS NOT OLD.confidence_score
 OR NEW.parent_parser_output_id IS NOT OLD.parent_parser_output_id OR NEW.notes IS NOT OLD.notes
 OR NEW.reprocessed_at IS NOT OLD.reprocessed_at)
BEGIN SELECT RAISE(ABORT,'independent AI source and payload sealed'); END;
CREATE TRIGGER independent_ai_parser_no_delete BEFORE DELETE ON parser_outputs
WHEN EXISTS(SELECT 1 FROM independent_ai_attempts WHERE parent_parser_output_id=OLD.id)
 OR EXISTS(SELECT 1 FROM independent_ai_publications WHERE child_parser_output_id=OLD.id)
BEGIN SELECT RAISE(ABORT,'independent AI proposal immutable'); END;
CREATE TRIGGER independent_ai_field_no_insert BEFORE INSERT ON parser_proposal_field_evidence
WHEN EXISTS(SELECT 1 FROM independent_ai_publications WHERE child_parser_output_id=NEW.parser_output_id)
 OR EXISTS(SELECT 1 FROM parser_proposal_field_evidence e JOIN independent_ai_publications p ON p.child_parser_output_id=e.parser_output_id WHERE e.id=NEW.id)
BEGIN SELECT RAISE(ABORT,'independent AI field evidence sealed'); END;
CREATE TRIGGER independent_ai_field_no_update BEFORE UPDATE ON parser_proposal_field_evidence
WHEN EXISTS(SELECT 1 FROM independent_ai_publications WHERE child_parser_output_id=OLD.parser_output_id OR child_parser_output_id=NEW.parser_output_id)
BEGIN SELECT RAISE(ABORT,'independent AI field evidence sealed'); END;
CREATE TRIGGER independent_ai_field_no_delete BEFORE DELETE ON parser_proposal_field_evidence
WHEN EXISTS(SELECT 1 FROM independent_ai_publications WHERE child_parser_output_id=OLD.parser_output_id)
BEGIN SELECT RAISE(ABORT,'independent AI field evidence sealed'); END;
CREATE TRIGGER independent_ai_intake_no_insert BEFORE INSERT ON raw_intake_records
WHEN EXISTS(SELECT 1 FROM independent_ai_attempts WHERE parent_parser_output_id=NEW.parser_output_id)
 OR EXISTS(SELECT 1 FROM independent_ai_publications WHERE child_parser_output_id=NEW.parser_output_id)
BEGIN SELECT RAISE(ABORT,'independent AI source pointer collision'); END;
CREATE TRIGGER independent_ai_intake_no_escape BEFORE UPDATE OF parser_output_id ON raw_intake_records
WHEN OLD.parser_output_id IS NOT NEW.parser_output_id
AND (EXISTS(SELECT 1 FROM independent_ai_attempts WHERE parent_parser_output_id=OLD.parser_output_id)
 OR EXISTS(SELECT 1 FROM independent_ai_publications WHERE child_parser_output_id=OLD.parser_output_id))
AND NOT EXISTS(SELECT 1 FROM independent_ai_publications p JOIN independent_ai_attempts a ON a.attempt_id=p.attempt_id JOIN parser_outputs child ON child.id=p.child_parser_output_id JOIN parser_outputs parent ON parent.id=p.parent_parser_output_id WHERE p.parent_parser_output_id=OLD.parser_output_id AND p.child_parser_output_id=NEW.parser_output_id AND a.intake_public_id=OLD.public_id AND child.parent_parser_output_id=parent.id AND parent.parse_status='superseded' AND child.parser_name='independent_finance_ai' AND child.parse_status='parsed_pending_confirmation')
AND NOT EXISTS(SELECT 1 FROM application_amendment_records a JOIN parser_outputs child ON child.id=a.resulting_parser_output_id WHERE a.base_parser_output_id=OLD.parser_output_id AND a.resulting_parser_output_id=NEW.parser_output_id AND child.parent_parser_output_id=OLD.parser_output_id)
BEGIN SELECT RAISE(ABORT,'independent AI lineage cannot be escaped'); END;
DROP TRIGGER trg_ai_fallback_raw_intake_no_lineage_escape;
CREATE TRIGGER trg_ai_fallback_raw_intake_no_lineage_escape
    BEFORE UPDATE OF parser_output_id ON raw_intake_records
    WHEN OLD.parser_output_id IS NOT NEW.parser_output_id
      AND NOT EXISTS (
          SELECT 1
          FROM parser_human_draft_publications AS publication
          JOIN parser_human_draft_operations AS operation
            ON operation.id = publication.operation_id
           AND operation.draft_id = publication.draft_id
          JOIN parser_outputs AS child
            ON child.id = publication.parser_output_id
          WHERE publication.parser_output_id = NEW.parser_output_id
            AND child.parent_parser_output_id = OLD.parser_output_id
            AND operation.operation_type = 'accepted'
            AND operation.operation_outcome = 'accepted'
            AND operation.result_completeness = 'complete'
            AND operation.publication_parser_output_id = NEW.parser_output_id
      )
      AND NOT EXISTS (
          SELECT 1
          FROM application_amendment_records AS amendment
          JOIN application_amendment_reviews AS review ON review.review_id=amendment.review_id
          JOIN parser_outputs AS child ON child.id=amendment.resulting_parser_output_id
          JOIN parser_outputs AS parent ON parent.id=amendment.base_parser_output_id
          WHERE amendment.base_parser_output_id=OLD.parser_output_id
            AND amendment.resulting_parser_output_id=NEW.parser_output_id
            AND amendment.resulting_version=0
            AND child.parent_parser_output_id=parent.id
            AND parent.parse_status='superseded'
            AND child.parse_status='parsed_pending_confirmation'
            AND parent.source_public_id=OLD.public_id
            AND child.source_public_id=OLD.public_id
            AND child.source_type=parent.source_type
            AND child.raw_text=parent.raw_text
            AND child.attachment_id IS parent.attachment_id
            AND child.statement_batch_id IS parent.statement_batch_id
            AND review.parser_output_id=parent.id
            AND review.intake_public_id=OLD.public_id
            AND review.base_version=amendment.base_version
            AND review.base_content_hash=amendment.base_content_hash
            AND review.source_event_key=amendment.source_event_key
            AND (
                (amendment.publication_kind='text_supersession'
                 AND child.parser_name='application_human_amendment'
                 AND child.parser_version='v1'
                 AND EXISTS(SELECT 1 FROM parser_text_amendment_revisions AS edge
                            WHERE edge.amendment_id=amendment.amendment_id
                              AND edge.publication_public_id=amendment.publication_public_id
                              AND edge.parent_parser_output_id=parent.id
                              AND edge.child_parser_output_id=child.id))
                OR
                (amendment.publication_kind='receipt_supersession'
                 AND EXISTS(SELECT 1 FROM receipt_proposal_revisions AS revision
                            WHERE revision.correction_public_id=amendment.publication_public_id
                              AND revision.superseded_parser_output_id=parent.id
                              AND revision.replacement_parser_output_id=child.id
                              AND revision.superseded_content_hash=amendment.base_content_hash
                              AND revision.replacement_content_hash=amendment.resulting_content_hash))
            )
      )
      AND NOT EXISTS(SELECT 1 FROM independent_ai_publications p JOIN independent_ai_attempts a ON a.attempt_id=p.attempt_id JOIN parser_outputs child ON child.id=p.child_parser_output_id JOIN parser_outputs parent ON parent.id=p.parent_parser_output_id WHERE p.parent_parser_output_id=OLD.parser_output_id AND p.child_parser_output_id=NEW.parser_output_id AND a.intake_public_id=OLD.public_id AND child.parent_parser_output_id=parent.id AND parent.parse_status='superseded' AND child.parser_name='independent_finance_ai' AND child.parse_status='parsed_pending_confirmation')
      AND (
          EXISTS (
              SELECT 1
              FROM ai_fallback_proposal_links
              WHERE parser_output_id = OLD.parser_output_id
          )
          OR EXISTS (
              SELECT 1
              FROM ai_fallback_attempts AS attempt
              WHERE attempt.parent_parser_output_id = OLD.parser_output_id
                AND NOT EXISTS (
                    SELECT 1
                    FROM ai_fallback_proposal_links AS link
                    JOIN ai_fallback_results AS result ON result.id = link.result_id
                    WHERE result.attempt_id = attempt.id
                      AND link.parser_output_id = NEW.parser_output_id
                )
          )
      )
BEGIN
    SELECT RAISE(ABORT, 'AI fallback raw-intake lineage cannot be escaped');
END;

CREATE TRIGGER independent_ai_intake_pointer_collision BEFORE UPDATE OF parser_output_id ON raw_intake_records
WHEN OLD.parser_output_id IS NOT NEW.parser_output_id
AND EXISTS(SELECT 1 FROM independent_ai_publications WHERE child_parser_output_id=NEW.parser_output_id)
AND NOT EXISTS(SELECT 1 FROM independent_ai_publications p JOIN independent_ai_attempts a ON a.attempt_id=p.attempt_id WHERE p.child_parser_output_id=NEW.parser_output_id AND p.parent_parser_output_id=OLD.parser_output_id AND a.intake_public_id=OLD.public_id AND OLD.public_id=NEW.public_id)
BEGIN SELECT RAISE(ABORT,'independent AI source pointer collision'); END;
