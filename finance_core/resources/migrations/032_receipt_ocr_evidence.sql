PRAGMA foreign_keys = ON;

-- Append-only receipt OCR extraction evidence.
--
-- OCR output is untrusted source evidence only. These tables do not create
-- parser proposals, receipt facts, calculations, transactions, settlement
-- obligations, or any other final financial state.

CREATE TABLE IF NOT EXISTS receipt_ocr_extractions (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE CHECK (
        length(public_id) BETWEEN 6 AND 200
        AND substr(public_id, 1, 5) = 'rocr_'
        AND public_id NOT GLOB '*[^A-Za-z0-9_-]*'
    ),
    attachment_id INTEGER NOT NULL,
    source_attachment_hash TEXT NOT NULL CHECK (
        length(source_attachment_hash) = 64
        AND lower(source_attachment_hash) = source_attachment_hash
        AND source_attachment_hash NOT GLOB '*[^0-9a-f]*'
    ),
    source_attachment_size INTEGER NOT NULL CHECK (source_attachment_size >= 0),
    source_mime_type TEXT NOT NULL CHECK (
        source_mime_type IN ('image/jpeg', 'image/png', 'application/pdf')
    ),
    engine_name TEXT NOT NULL CHECK (length(engine_name) BETWEEN 1 AND 128),
    engine_version TEXT NOT NULL CHECK (length(engine_version) BETWEEN 1 AND 128),
    engine_binary_sha256 TEXT NOT NULL CHECK (
        length(engine_binary_sha256) = 64
        AND lower(engine_binary_sha256) = engine_binary_sha256
        AND engine_binary_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    engine_configuration_hash TEXT NOT NULL CHECK (
        length(engine_configuration_hash) = 64
        AND lower(engine_configuration_hash) = engine_configuration_hash
        AND engine_configuration_hash NOT GLOB '*[^0-9a-f]*'
    ),
    extraction_fingerprint TEXT NOT NULL UNIQUE CHECK (
        length(extraction_fingerprint) = 64
        AND lower(extraction_fingerprint) = extraction_fingerprint
        AND extraction_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    extraction_status TEXT NOT NULL CHECK (
        extraction_status IN (
            'succeeded',
            'no_text',
            'unsupported_input',
            'engine_failed',
            'resource_rejected'
        )
    ),
    block_count INTEGER NOT NULL CHECK (block_count >= 0),
    total_normalized_text_length INTEGER NOT NULL CHECK (
        total_normalized_text_length >= 0
    ),
    normalized_result_hash TEXT NOT NULL CHECK (
        length(normalized_result_hash) = 64
        AND lower(normalized_result_hash) = normalized_result_hash
        AND normalized_result_hash NOT GLOB '*[^0-9a-f]*'
    ),
    sanitized_outcome_code TEXT NOT NULL CHECK (
        length(sanitized_outcome_code) BETWEEN 1 AND 64
        AND sanitized_outcome_code NOT GLOB '*[^a-z0-9_]*'
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (attachment_id) REFERENCES attachments(id),
    CHECK (
        (extraction_status = 'succeeded' AND block_count > 0
         AND total_normalized_text_length > 0)
        OR
        (extraction_status != 'succeeded' AND block_count = 0
         AND total_normalized_text_length = 0)
    ),
    CHECK (
        (extraction_status = 'unsupported_input'
         AND source_mime_type = 'application/pdf'
         AND sanitized_outcome_code = 'pdf_unsupported')
        OR
        (extraction_status != 'unsupported_input'
         AND sanitized_outcome_code != 'pdf_unsupported')
    ),
    CHECK (
        (extraction_status = 'resource_rejected'
         AND sanitized_outcome_code = 'attachment_size_limit')
        OR
        (extraction_status != 'resource_rejected'
         AND sanitized_outcome_code != 'attachment_size_limit')
    )
);

CREATE TABLE IF NOT EXISTS receipt_ocr_blocks (
    id INTEGER PRIMARY KEY,
    extraction_id INTEGER NOT NULL,
    sequence_index INTEGER NOT NULL CHECK (sequence_index >= 0),
    page_index INTEGER NOT NULL CHECK (page_index >= 0),
    engine_block_index INTEGER CHECK (engine_block_index IS NULL OR engine_block_index >= 0),
    engine_paragraph_index INTEGER CHECK (
        engine_paragraph_index IS NULL OR engine_paragraph_index >= 0
    ),
    engine_line_index INTEGER CHECK (engine_line_index IS NULL OR engine_line_index >= 0),
    engine_word_index INTEGER CHECK (engine_word_index IS NULL OR engine_word_index >= 0),
    normalized_text TEXT NOT NULL CHECK (
        length(normalized_text) > 0 AND instr(normalized_text, char(0)) = 0
    ),
    coordinate_left INTEGER NOT NULL CHECK (coordinate_left >= 0),
    coordinate_top INTEGER NOT NULL CHECK (coordinate_top >= 0),
    coordinate_width INTEGER NOT NULL CHECK (coordinate_width >= 0),
    coordinate_height INTEGER NOT NULL CHECK (coordinate_height >= 0),
    page_width INTEGER NOT NULL CHECK (page_width > 0),
    page_height INTEGER NOT NULL CHECK (page_height > 0),
    confidence_scaled INTEGER CHECK (
        confidence_scaled IS NULL
        OR confidence_scaled BETWEEN 0 AND 10000
    ),
    FOREIGN KEY (extraction_id) REFERENCES receipt_ocr_extractions(id),
    UNIQUE (extraction_id, sequence_index),
    CHECK (coordinate_left + coordinate_width <= page_width),
    CHECK (coordinate_top + coordinate_height <= page_height)
);

CREATE INDEX IF NOT EXISTS idx_receipt_ocr_extractions_attachment_id
    ON receipt_ocr_extractions(attachment_id);

CREATE INDEX IF NOT EXISTS idx_receipt_ocr_blocks_extraction_id
    ON receipt_ocr_blocks(extraction_id);

CREATE TRIGGER IF NOT EXISTS trg_receipt_ocr_extractions_no_update
    BEFORE UPDATE ON receipt_ocr_extractions
BEGIN
    SELECT RAISE(ABORT, 'receipt_ocr_extractions rows are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_ocr_extractions_no_delete
    BEFORE DELETE ON receipt_ocr_extractions
BEGIN
    SELECT RAISE(ABORT, 'receipt_ocr_extractions rows are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_ocr_blocks_no_update
    BEFORE UPDATE ON receipt_ocr_blocks
BEGIN
    SELECT RAISE(ABORT, 'receipt_ocr_blocks rows are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_ocr_blocks_no_delete
    BEFORE DELETE ON receipt_ocr_blocks
BEGIN
    SELECT RAISE(ABORT, 'receipt_ocr_blocks rows are append-only');
END;

-- OCR evidence remains bound to the canonical attachment identity that was
-- revalidated immediately before persistence.
CREATE TRIGGER IF NOT EXISTS trg_attachments_no_update_identity_when_ocr_referenced
    BEFORE UPDATE ON attachments
    FOR EACH ROW
    WHEN EXISTS (
        SELECT 1 FROM receipt_ocr_extractions WHERE attachment_id = OLD.id
    )
BEGIN
    SELECT CASE
        WHEN OLD.file_path IS NOT NEW.file_path
          OR OLD.file_hash IS NOT NEW.file_hash
          OR OLD.mime_type IS NOT NEW.mime_type
          OR OLD.original_filename IS NOT NEW.original_filename
        THEN RAISE(ABORT, 'OCR-referenced attachment identity is immutable')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_attachments_no_delete_when_ocr_referenced
    BEFORE DELETE ON attachments
    FOR EACH ROW
    WHEN EXISTS (
        SELECT 1 FROM receipt_ocr_extractions WHERE attachment_id = OLD.id
    )
BEGIN
    SELECT RAISE(ABORT, 'OCR-referenced attachments cannot be deleted');
END;
