PRAGMA foreign_keys = OFF;

-- S5e-B additive lineage/sealing migration. Migration 042 is immutable.
-- Rebuild the OCR link table only to extend its closed role set; all existing
-- rows and identities are copied byte-for-byte.

ALTER TABLE receipt_ocr_proposal_links RENAME TO receipt_ocr_proposal_links_legacy_043;

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
        link_role IN ('initial', 'superseding_correction', 'ai_fallback')
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
FROM receipt_ocr_proposal_links_legacy_043;

DROP TABLE receipt_ocr_proposal_links_legacy_043;

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

CREATE TRIGGER trg_ai_fallback_child_no_hash_update
    BEFORE UPDATE ON parser_outputs
    FOR EACH ROW
    WHEN EXISTS (
        SELECT 1
        FROM ai_fallback_proposal_links
        WHERE parser_output_id = OLD.id
    )
    AND (
        NEW.id IS NOT OLD.id
        OR NEW.public_id IS NOT OLD.public_id
        OR NEW.source_type IS NOT OLD.source_type
        OR NEW.source_public_id IS NOT OLD.source_public_id
        OR NEW.statement_batch_id IS NOT OLD.statement_batch_id
        OR NEW.attachment_id IS NOT OLD.attachment_id
        OR NEW.parser_name IS NOT OLD.parser_name
        OR NEW.parser_version IS NOT OLD.parser_version
        OR NEW.ai_provider IS NOT OLD.ai_provider
        OR NEW.ai_model IS NOT OLD.ai_model
        OR NEW.prompt_version IS NOT OLD.prompt_version
        OR NEW.raw_text IS NOT OLD.raw_text
        OR NEW.parsed_payload IS NOT OLD.parsed_payload
        OR NEW.normalized_payload IS NOT OLD.normalized_payload
        OR NEW.confidence_score IS NOT OLD.confidence_score
        OR NEW.parent_parser_output_id IS NOT OLD.parent_parser_output_id
        OR NEW.reprocessed_at IS NOT OLD.reprocessed_at
        OR NEW.notes IS NOT OLD.notes
    )
BEGIN
    SELECT RAISE(ABORT, 'AI fallback child source and payload are sealed');
END;

DROP TRIGGER IF EXISTS trg_ai_fallback_parent_no_hash_update;

CREATE TRIGGER trg_ai_fallback_parent_no_hash_update
    BEFORE UPDATE ON parser_outputs
    FOR EACH ROW
    WHEN (
        EXISTS (
            SELECT 1
            FROM ai_fallback_attempts
            WHERE parent_parser_output_id = OLD.id
        )
        OR EXISTS (
            SELECT 1
            FROM ai_fallback_proposal_links
            WHERE parser_output_id = OLD.id
        )
    )
    AND (
        NEW.id IS NOT OLD.id
        OR NEW.public_id IS NOT OLD.public_id
        OR NEW.source_type IS NOT OLD.source_type
        OR NEW.source_public_id IS NOT OLD.source_public_id
        OR NEW.statement_batch_id IS NOT OLD.statement_batch_id
        OR NEW.attachment_id IS NOT OLD.attachment_id
        OR NEW.parser_name IS NOT OLD.parser_name
        OR NEW.parser_version IS NOT OLD.parser_version
        OR NEW.ai_provider IS NOT OLD.ai_provider
        OR NEW.ai_model IS NOT OLD.ai_model
        OR NEW.prompt_version IS NOT OLD.prompt_version
        OR NEW.raw_text IS NOT OLD.raw_text
        OR NEW.parsed_payload IS NOT OLD.parsed_payload
        OR NEW.normalized_payload IS NOT OLD.normalized_payload
        OR NEW.parent_parser_output_id IS NOT OLD.parent_parser_output_id
        OR NEW.reprocessed_at IS NOT OLD.reprocessed_at
        OR NEW.notes IS NOT OLD.notes
    )
BEGIN
    SELECT RAISE(ABORT, 'AI fallback parent source and payload are sealed');
END;

CREATE TRIGGER trg_ai_fallback_child_no_delete
    BEFORE DELETE ON parser_outputs
    FOR EACH ROW
    WHEN EXISTS (
        SELECT 1
        FROM ai_fallback_proposal_links
        WHERE parser_output_id = OLD.id
    )
BEGIN
    SELECT RAISE(ABORT, 'AI fallback child cannot be deleted');
END;

-- Field evidence is also rebuilt additively.  Existing evidence rows retain
-- their integer identity and values; S5e-B may use the new ai_model source
-- kind for child evidence, while the existing source kinds remain valid.
ALTER TABLE parser_proposal_field_evidence
    RENAME TO parser_proposal_field_evidence_legacy_043;

CREATE TABLE parser_proposal_field_evidence (
    id INTEGER PRIMARY KEY,
    parser_output_id INTEGER NOT NULL,
    field_name TEXT NOT NULL,
    proposed_value TEXT,
    confidence_score REAL CHECK (
        confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)
    ),
    evidence_source_type TEXT CHECK (
        evidence_source_type IS NULL OR evidence_source_type IN (
            'raw_input',
            'attachment',
            'ocr',
            'pdf',
            'user_message',
            'system',
            'ai_model'
        )
    ),
    evidence_reference TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id)
);

INSERT INTO parser_proposal_field_evidence (
    id, parser_output_id, field_name, proposed_value, confidence_score,
    evidence_source_type, evidence_reference, notes, created_at
)
SELECT
    id, parser_output_id, field_name, proposed_value, confidence_score,
    evidence_source_type, evidence_reference, notes, created_at
FROM parser_proposal_field_evidence_legacy_043;

DROP TABLE parser_proposal_field_evidence_legacy_043;

CREATE INDEX idx_parser_proposal_field_evidence_parser_output_id
    ON parser_proposal_field_evidence(parser_output_id);
CREATE INDEX idx_parser_proposal_field_evidence_field_name
    ON parser_proposal_field_evidence(field_name);
CREATE INDEX idx_parser_proposal_field_evidence_source_type
    ON parser_proposal_field_evidence(evidence_source_type);

CREATE TRIGGER trg_ai_fallback_evidence_no_insert_collision
    BEFORE INSERT ON parser_proposal_field_evidence
    WHEN EXISTS (
        SELECT 1
        FROM ai_fallback_proposal_links
        WHERE parser_output_id = NEW.parser_output_id
    )
    OR EXISTS (
        SELECT 1
        FROM parser_proposal_field_evidence AS existing
        JOIN ai_fallback_proposal_links AS sealed
          ON sealed.parser_output_id = existing.parser_output_id
        WHERE existing.id = NEW.id
    )
BEGIN
    SELECT RAISE(ABORT, 'AI fallback evidence set is sealed');
END;

CREATE TRIGGER trg_ai_fallback_evidence_no_update
    BEFORE UPDATE ON parser_proposal_field_evidence
    FOR EACH ROW
    WHEN EXISTS (
        SELECT 1
        FROM ai_fallback_proposal_links
        WHERE parser_output_id = OLD.parser_output_id
    )
    OR EXISTS (
        SELECT 1
        FROM ai_fallback_proposal_links
        WHERE parser_output_id = NEW.parser_output_id
    )
    OR EXISTS (
        SELECT 1
        FROM parser_proposal_field_evidence AS existing
        JOIN ai_fallback_proposal_links AS sealed
          ON sealed.parser_output_id = existing.parser_output_id
        WHERE existing.id = NEW.id
          AND existing.id <> OLD.id
    )
BEGIN
    SELECT RAISE(ABORT, 'AI fallback evidence is append-only');
END;

CREATE TRIGGER trg_ai_fallback_evidence_no_delete
    BEFORE DELETE ON parser_proposal_field_evidence
    FOR EACH ROW
    WHEN EXISTS (
        SELECT 1
        FROM ai_fallback_proposal_links
        WHERE parser_output_id = OLD.parser_output_id
    )
BEGIN
    SELECT RAISE(ABORT, 'AI fallback evidence is append-only');
END;

-- A fallback parent/child may have one current raw-intake route only.  The
-- original intake is allowed to transition from parent to child in-place;
-- another identity cannot be inserted or moved onto either sealed proposal.
CREATE TRIGGER trg_ai_fallback_raw_intake_no_insert_pointer_collision
    BEFORE INSERT ON raw_intake_records
    WHEN (
        EXISTS (
            SELECT 1
            FROM ai_fallback_attempts
            WHERE parent_parser_output_id = NEW.parser_output_id
        )
        OR EXISTS (
            SELECT 1
            FROM ai_fallback_proposal_links
            WHERE parser_output_id = NEW.parser_output_id
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'AI fallback proposal already has a raw-intake binding');
END;

CREATE TRIGGER trg_ai_fallback_raw_intake_no_update_pointer_collision
    BEFORE UPDATE ON raw_intake_records
    WHEN (
        OLD.parser_output_id IS NOT NEW.parser_output_id
    )
    AND (
        EXISTS (
            SELECT 1
            FROM ai_fallback_attempts
            WHERE parent_parser_output_id = NEW.parser_output_id
        )
        OR EXISTS (
            SELECT 1
            FROM ai_fallback_proposal_links
            WHERE parser_output_id = NEW.parser_output_id
        )
    )
    AND NOT EXISTS (
        SELECT 1
        FROM ai_fallback_attempts AS attempt
        WHERE attempt.raw_intake_record_id = NEW.id
          AND (
              (
                  OLD.parser_output_id = NEW.parser_output_id
                  AND attempt.parent_parser_output_id = NEW.parser_output_id
              )
              OR (
                  OLD.parser_output_id = attempt.parent_parser_output_id
                  AND EXISTS (
                  SELECT 1
                  FROM ai_fallback_proposal_links AS link
                  WHERE link.parser_output_id = NEW.parser_output_id
                    AND link.result_id IN (
                        SELECT result.id
                        FROM ai_fallback_results AS result
                        WHERE result.attempt_id = attempt.id
                    )
                  )
              )
          )
    )
BEGIN
    SELECT RAISE(ABORT, 'AI fallback proposal already has a raw-intake binding');
END;

-- Once an intake is the source of a fallback attempt, its pointer may only
-- make the one in-transaction parent-to-linked-child transition.  In
-- particular, generic reparse must not move away from a sealed AI child and
-- shed the provenance root.
CREATE TRIGGER trg_ai_fallback_raw_intake_no_lineage_escape
    BEFORE UPDATE OF parser_output_id ON raw_intake_records
    WHEN OLD.parser_output_id IS NOT NEW.parser_output_id
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

PRAGMA foreign_keys = ON;

-- S5e-B result replay binds the exact transport union arguments.  Legacy 042
-- rows remain readable with NULL because that migration had no writer.
ALTER TABLE ai_fallback_results
    ADD COLUMN result_arguments_hash TEXT CHECK (
        result_arguments_hash IS NULL OR (
            typeof(result_arguments_hash) = 'text'
            AND length(result_arguments_hash) = 64
            AND result_arguments_hash NOT GLOB '*[^0-9a-f]*'
        )
    );

ALTER TABLE ai_fallback_proposal_links
    ADD COLUMN proposal_version INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(proposal_version) = 'integer' AND proposal_version >= 0
    );
