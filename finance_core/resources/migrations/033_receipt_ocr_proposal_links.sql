PRAGMA foreign_keys = ON;

-- Append-only OCR-to-proposal link evidence.
--
-- This table binds one persisted receipt OCR extraction (migration 032) to
-- one deterministic total-level parser proposal (parser_outputs, migration
-- 001).  The link is evidence only: it never confirms a proposal, converts
-- it to a transaction, or creates receipt facts, calculations, settlement
-- obligations, or any other final financial state.
--
-- B1 creates only `initial` links.  The `superseding_correction` role is
-- reserved for the already-approved B3 follow-up and must not be produced
-- by B1 behaviour.

CREATE TABLE IF NOT EXISTS receipt_ocr_proposal_links (
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
        link_role IN ('initial', 'superseding_correction')
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (extraction_id) REFERENCES receipt_ocr_extractions(id),
    FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id),
    -- Each parser proposal may be bound by at most one OCR link.
    UNIQUE (parser_output_id)
);

-- At most one `initial` proposal per extraction and parser contract version.
CREATE UNIQUE INDEX IF NOT EXISTS idx_receipt_ocr_proposal_links_initial_unique
    ON receipt_ocr_proposal_links(extraction_id, parser_contract_version)
    WHERE link_role = 'initial';

CREATE INDEX IF NOT EXISTS idx_receipt_ocr_proposal_links_extraction_id
    ON receipt_ocr_proposal_links(extraction_id);

CREATE INDEX IF NOT EXISTS idx_receipt_ocr_proposal_links_parser_output_id
    ON receipt_ocr_proposal_links(parser_output_id);

-- -----------------------------------------------------------------------
-- Append-only enforcement
-- -----------------------------------------------------------------------

CREATE TRIGGER IF NOT EXISTS trg_receipt_ocr_proposal_links_no_update
    BEFORE UPDATE ON receipt_ocr_proposal_links
BEGIN
    SELECT RAISE(ABORT, 'receipt_ocr_proposal_links rows are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_ocr_proposal_links_no_delete
    BEFORE DELETE ON receipt_ocr_proposal_links
BEGIN
    SELECT RAISE(ABORT, 'receipt_ocr_proposal_links rows are append-only');
END;
