-- Independent pre-confirmation human edits. Historical migrations remain immutable.
ALTER TABLE receipts ADD COLUMN description TEXT;
ALTER TABLE receipts ADD COLUMN category TEXT;
ALTER TABLE receipts ADD COLUMN bookkeeping_metadata_version TEXT CHECK(bookkeeping_metadata_version IS NULL OR bookkeeping_metadata_version='application_bookkeeping_metadata_v1');
ALTER TABLE transactions ADD COLUMN description TEXT;
CREATE TABLE application_amendment_reviews (
 review_id TEXT PRIMARY KEY,
 parser_output_id INTEGER NOT NULL REFERENCES parser_outputs(id),
 intake_public_id TEXT NOT NULL REFERENCES raw_intake_records(public_id),
 source_event_key TEXT NOT NULL CHECK(length(source_event_key)=64),
 base_version INTEGER NOT NULL CHECK(base_version>=0),
 base_content_hash TEXT NOT NULL CHECK(length(base_content_hash)=64),
 material_json TEXT NOT NULL CHECK(json_valid(material_json)),
 review_hash TEXT NOT NULL UNIQUE CHECK(length(review_hash)=64),
 created_at INTEGER NOT NULL CHECK(created_at>0),
 expires_at INTEGER NOT NULL CHECK(expires_at>created_at)
);
CREATE TABLE application_amendment_records (
 amendment_id TEXT PRIMARY KEY,
 amendment_namespace TEXT NOT NULL,
 evidence_id TEXT NOT NULL,
 review_id TEXT NOT NULL REFERENCES application_amendment_reviews(review_id),
 base_parser_output_id INTEGER NOT NULL REFERENCES parser_outputs(id),
 base_version INTEGER NOT NULL CHECK(base_version>=0),
 base_content_hash TEXT NOT NULL CHECK(length(base_content_hash)=64),
 resulting_parser_output_id INTEGER NOT NULL REFERENCES parser_outputs(id),
 resulting_version INTEGER NOT NULL CHECK(resulting_version>=0),
 resulting_content_hash TEXT NOT NULL CHECK(length(resulting_content_hash)=64),
 publication_kind TEXT NOT NULL CHECK(publication_kind IN ('completion','text_supersession','receipt_supersession')),
 publication_public_id TEXT NOT NULL UNIQUE,
 source_event_key TEXT NOT NULL CHECK(length(source_event_key)=64),
 accepted_at INTEGER NOT NULL CHECK(accepted_at>0),
 material_json TEXT NOT NULL CHECK(json_valid(material_json)),
 record_hash TEXT NOT NULL UNIQUE CHECK(length(record_hash)=64),
 UNIQUE(amendment_namespace,evidence_id),
 UNIQUE(base_parser_output_id,base_version,base_content_hash),
 UNIQUE(resulting_parser_output_id,resulting_version)
);
CREATE TABLE parser_text_amendment_revisions (
 publication_public_id TEXT PRIMARY KEY,
 amendment_id TEXT NOT NULL UNIQUE REFERENCES application_amendment_records(amendment_id) DEFERRABLE INITIALLY DEFERRED,
 parent_parser_output_id INTEGER NOT NULL UNIQUE REFERENCES parser_outputs(id),
 child_parser_output_id INTEGER NOT NULL UNIQUE REFERENCES parser_outputs(id),
 child_payload_json TEXT NOT NULL CHECK(json_valid(child_payload_json)),
 CHECK(parent_parser_output_id!=child_parser_output_id)
);
CREATE TABLE application_amendment_invalidations (
 amendment_id TEXT PRIMARY KEY REFERENCES application_amendment_records(amendment_id),
 base_parser_output_id INTEGER NOT NULL REFERENCES parser_outputs(id),
 base_version INTEGER NOT NULL CHECK(base_version>=0),
 base_content_hash TEXT NOT NULL CHECK(length(base_content_hash)=64)
);
CREATE TABLE application_amendment_receipt_metadata (
 receipt_id INTEGER PRIMARY KEY REFERENCES receipts(id),
 conversion_public_id TEXT NOT NULL UNIQUE REFERENCES receipt_proposal_conversions(command_public_id),
 attempt_id TEXT NOT NULL UNIQUE REFERENCES application_posting_attempts(attempt_id),
 metadata_version TEXT NOT NULL CHECK(metadata_version='application_bookkeeping_metadata_v1'),
 description TEXT,
 category TEXT,
 material_hash TEXT NOT NULL CHECK(length(material_hash)=64)
);
CREATE TRIGGER application_amendment_receipt_metadata_freeze BEFORE UPDATE OF description,category,bookkeeping_metadata_version ON receipts
WHEN EXISTS(SELECT 1 FROM receipt_proposal_conversions WHERE receipt_id=OLD.id)
BEGIN SELECT RAISE(ABORT,'converted receipt metadata immutable'); END;
CREATE TRIGGER application_amendment_transaction_metadata_freeze BEFORE UPDATE OF description,category ON transactions
WHEN EXISTS(SELECT 1 FROM parser_proposal_conversion_audit WHERE transaction_id=OLD.id)
 OR EXISTS(SELECT 1 FROM application_posting_attempts WHERE transaction_public_id=OLD.public_id)
BEGIN SELECT RAISE(ABORT,'posted transaction metadata immutable'); END;
CREATE TRIGGER application_amendment_reviews_no_update BEFORE UPDATE ON application_amendment_reviews BEGIN SELECT RAISE(ABORT,'immutable amendment evidence'); END;
CREATE TRIGGER application_amendment_reviews_no_delete BEFORE DELETE ON application_amendment_reviews BEGIN SELECT RAISE(ABORT,'immutable amendment evidence'); END;
CREATE TRIGGER application_amendment_reviews_no_collision BEFORE INSERT ON application_amendment_reviews WHEN EXISTS(SELECT 1 FROM application_amendment_reviews WHERE review_id=NEW.review_id OR review_hash=NEW.review_hash) BEGIN SELECT RAISE(ABORT,'amendment evidence collision'); END;
CREATE TRIGGER application_amendment_records_no_update BEFORE UPDATE ON application_amendment_records BEGIN SELECT RAISE(ABORT,'immutable amendment evidence'); END;
CREATE TRIGGER application_amendment_records_no_delete BEFORE DELETE ON application_amendment_records BEGIN SELECT RAISE(ABORT,'immutable amendment evidence'); END;
CREATE TRIGGER application_amendment_records_no_collision BEFORE INSERT ON application_amendment_records WHEN EXISTS(SELECT 1 FROM application_amendment_records WHERE amendment_id=NEW.amendment_id OR publication_public_id=NEW.publication_public_id OR record_hash=NEW.record_hash OR (amendment_namespace=NEW.amendment_namespace AND evidence_id=NEW.evidence_id) OR (base_parser_output_id=NEW.base_parser_output_id AND base_version=NEW.base_version AND base_content_hash=NEW.base_content_hash) OR (resulting_parser_output_id=NEW.resulting_parser_output_id AND resulting_version=NEW.resulting_version)) BEGIN SELECT RAISE(ABORT,'amendment evidence collision'); END;
CREATE TRIGGER parser_text_amendment_revisions_no_update BEFORE UPDATE ON parser_text_amendment_revisions BEGIN SELECT RAISE(ABORT,'immutable amendment evidence'); END;
CREATE TRIGGER parser_text_amendment_revisions_no_delete BEFORE DELETE ON parser_text_amendment_revisions BEGIN SELECT RAISE(ABORT,'immutable amendment evidence'); END;
CREATE TRIGGER parser_text_amendment_revisions_no_collision BEFORE INSERT ON parser_text_amendment_revisions WHEN EXISTS(SELECT 1 FROM parser_text_amendment_revisions WHERE publication_public_id=NEW.publication_public_id OR parent_parser_output_id=NEW.parent_parser_output_id OR child_parser_output_id=NEW.child_parser_output_id OR amendment_id=NEW.amendment_id) BEGIN SELECT RAISE(ABORT,'amendment evidence collision'); END;
CREATE TRIGGER application_amendment_invalidations_no_update BEFORE UPDATE ON application_amendment_invalidations BEGIN SELECT RAISE(ABORT,'immutable amendment evidence'); END;
CREATE TRIGGER application_amendment_invalidations_no_delete BEFORE DELETE ON application_amendment_invalidations BEGIN SELECT RAISE(ABORT,'immutable amendment evidence'); END;
CREATE TRIGGER application_amendment_invalidations_no_collision BEFORE INSERT ON application_amendment_invalidations WHEN EXISTS(SELECT 1 FROM application_amendment_invalidations WHERE amendment_id=NEW.amendment_id) BEGIN SELECT RAISE(ABORT,'amendment evidence collision'); END;
CREATE TRIGGER application_amendment_receipt_metadata_no_update BEFORE UPDATE ON application_amendment_receipt_metadata BEGIN SELECT RAISE(ABORT,'immutable amendment evidence'); END;
CREATE TRIGGER application_amendment_receipt_metadata_no_delete BEFORE DELETE ON application_amendment_receipt_metadata BEGIN SELECT RAISE(ABORT,'immutable amendment evidence'); END;
CREATE TRIGGER application_amendment_receipt_metadata_no_collision BEFORE INSERT ON application_amendment_receipt_metadata WHEN EXISTS(SELECT 1 FROM application_amendment_receipt_metadata WHERE receipt_id=NEW.receipt_id OR conversion_public_id=NEW.conversion_public_id OR attempt_id=NEW.attempt_id) BEGIN SELECT RAISE(ABORT,'amendment evidence collision'); END;

-- Retain the 048 D1 exception and admit actual independently sealed owner effects.
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
