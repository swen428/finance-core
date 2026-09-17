PRAGMA foreign_keys = ON;

-- New PDF rows use a versioned JSON evidence contract stored in the existing
-- raw_row_payload_json column. Historical rows are not reclassified.
CREATE TRIGGER trg_pdf_statement_evidence_insert
BEFORE INSERT ON statement_transactions
WHEN NEW.row_fingerprint_version = 'pdf-row-fingerprint-v2' AND (
  NEW.raw_row_payload_json IS NULL
  OR json_valid(NEW.raw_row_payload_json) != 1
  OR json_extract(NEW.raw_row_payload_json, '$.evidence_contract_version')
       != 'pdf-row-evidence-v2'
  OR json_extract(NEW.raw_row_payload_json, '$.row_fingerprint') != NEW.row_fingerprint
  OR json_extract(NEW.raw_row_payload_json, '$.row_fingerprint_version')
       != NEW.row_fingerprint_version
  OR json_extract(NEW.raw_row_payload_json, '$.review_status') != 'authoritative'
  OR json_extract(NEW.raw_row_payload_json, '$.source_content_hash') IS NULL
  OR length(json_extract(NEW.raw_row_payload_json, '$.source_content_hash')) != 64
  OR json_extract(NEW.raw_row_payload_json, '$.source_content_hash') GLOB '*[^0-9a-f]*'
  OR json_extract(NEW.raw_row_payload_json, '$.source_content_hash')
       != (SELECT source_file_hash FROM statement_import_batches WHERE id = NEW.batch_id)
  OR json_extract(NEW.raw_row_payload_json, '$.source_page_number') IS NULL
  OR json_extract(NEW.raw_row_payload_json, '$.source_page_number') < 1
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.stable_row_locator'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.attachment_path'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.source_text_excerpt'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.original_line_text'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.parser_name'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.parser_version'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.template_name'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.template_version'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.extraction_version'), '') = ''
  OR json_extract(NEW.raw_row_payload_json, '$.direction_source')
       NOT IN ('explicit_token', 'explicit_column')
  OR json_extract(NEW.raw_row_payload_json, '$.direction_confidence') != 'high'
  OR json_extract(NEW.raw_row_payload_json, '$.direction') != NEW.amount_direction
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.original_amount_token'), '') = ''
  OR json_extract(NEW.raw_row_payload_json, '$.original_amount_token') != NEW.raw_amount
  OR json_extract(NEW.raw_row_payload_json, '$.original_amount_sign')
       NOT IN ('positive', 'negative')
  OR json_extract(NEW.raw_row_payload_json, '$.amount_sign_convention')
       NOT IN ('unsigned_explicit', 'outflow_positive', 'outflow_negative')
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.normalized_amount'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.currency_token'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.currency_source'), '') = ''
  OR (
    NEW.transaction_date IS NOT NULL
    AND coalesce(json_extract(NEW.raw_row_payload_json, '$.transaction_date_token'), '') = ''
  )
  OR (
    NEW.posted_date IS NOT NULL
    AND coalesce(json_extract(NEW.raw_row_payload_json, '$.posted_date_token'), '') = ''
  )
  OR NEW.amount_direction IS NULL
  OR NEW.amount_direction IN ('unknown', 'interest')
  OR NEW.statement_row_reference IS NULL
  OR NEW.statement_row_reference = ''
  OR NEW.statement_row_reference
       != json_extract(NEW.raw_row_payload_json, '$.stable_row_locator')
)
BEGIN
  SELECT RAISE(ABORT, 'invalid authoritative PDF statement evidence');
END;

CREATE TRIGGER trg_pdf_statement_evidence_update
BEFORE UPDATE OF batch_id, raw_row_payload_json, amount_direction, raw_amount,
  statement_row_reference, row_fingerprint, row_fingerprint_version ON statement_transactions
WHEN NEW.row_fingerprint_version = 'pdf-row-fingerprint-v2' AND (
  NEW.raw_row_payload_json IS NULL
  OR json_valid(NEW.raw_row_payload_json) != 1
  OR json_extract(NEW.raw_row_payload_json, '$.evidence_contract_version')
       != 'pdf-row-evidence-v2'
  OR json_extract(NEW.raw_row_payload_json, '$.row_fingerprint') != NEW.row_fingerprint
  OR json_extract(NEW.raw_row_payload_json, '$.row_fingerprint_version')
       != NEW.row_fingerprint_version
  OR json_extract(NEW.raw_row_payload_json, '$.review_status') != 'authoritative'
  OR json_extract(NEW.raw_row_payload_json, '$.source_content_hash') IS NULL
  OR length(json_extract(NEW.raw_row_payload_json, '$.source_content_hash')) != 64
  OR json_extract(NEW.raw_row_payload_json, '$.source_content_hash') GLOB '*[^0-9a-f]*'
  OR json_extract(NEW.raw_row_payload_json, '$.source_content_hash')
       != (SELECT source_file_hash FROM statement_import_batches WHERE id = NEW.batch_id)
  OR json_extract(NEW.raw_row_payload_json, '$.source_page_number') IS NULL
  OR json_extract(NEW.raw_row_payload_json, '$.source_page_number') < 1
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.stable_row_locator'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.attachment_path'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.source_text_excerpt'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.original_line_text'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.parser_name'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.parser_version'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.template_name'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.template_version'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.extraction_version'), '') = ''
  OR json_extract(NEW.raw_row_payload_json, '$.direction_source')
       NOT IN ('explicit_token', 'explicit_column')
  OR json_extract(NEW.raw_row_payload_json, '$.direction_confidence') != 'high'
  OR json_extract(NEW.raw_row_payload_json, '$.direction') != NEW.amount_direction
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.original_amount_token'), '') = ''
  OR json_extract(NEW.raw_row_payload_json, '$.original_amount_token') != NEW.raw_amount
  OR json_extract(NEW.raw_row_payload_json, '$.original_amount_sign')
       NOT IN ('positive', 'negative')
  OR json_extract(NEW.raw_row_payload_json, '$.amount_sign_convention')
       NOT IN ('unsigned_explicit', 'outflow_positive', 'outflow_negative')
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.normalized_amount'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.currency_token'), '') = ''
  OR coalesce(json_extract(NEW.raw_row_payload_json, '$.currency_source'), '') = ''
  OR (
    NEW.transaction_date IS NOT NULL
    AND coalesce(json_extract(NEW.raw_row_payload_json, '$.transaction_date_token'), '') = ''
  )
  OR (
    NEW.posted_date IS NOT NULL
    AND coalesce(json_extract(NEW.raw_row_payload_json, '$.posted_date_token'), '') = ''
  )
  OR NEW.amount_direction IS NULL
  OR NEW.amount_direction IN ('unknown', 'interest')
  OR NEW.statement_row_reference IS NULL
  OR NEW.statement_row_reference = ''
  OR NEW.statement_row_reference
       != json_extract(NEW.raw_row_payload_json, '$.stable_row_locator')
)
BEGIN
  SELECT RAISE(ABORT, 'invalid authoritative PDF statement evidence');
END;
