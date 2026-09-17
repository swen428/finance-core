PRAGMA foreign_keys = OFF;

-- Raw intake source and evidence foundation.
-- This migration preserves existing raw_intake_records rows, expands the
-- source model beyond telegram_text, and adds a separate evidence table for
-- attachment/OCR/PDF/statement-row traceability. It does not create final
-- financial records or modify live data.

CREATE TABLE IF NOT EXISTS raw_intake_records_v2 (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  source_type TEXT NOT NULL CHECK (
    source_type IN (
      'telegram_text',
      'telegram_image',
      'telegram_pdf',
      'uploaded_pdf',
      'bank_statement_pdf',
      'credit_card_statement_pdf',
      'statement_row',
      'manual_entry',
      'system_generated'
    )
  ),
  source_channel TEXT CHECK (
    source_channel IS NULL OR source_channel IN (
      'telegram',
      'manual',
      'upload',
      'system',
      'imported_statement'
    )
  ),
  raw_input TEXT NOT NULL,
  normalized_text TEXT,
  received_at TEXT NOT NULL,
  source_received_at TEXT,
  external_source_id TEXT,
  source_message_id TEXT,
  idempotency_key TEXT,
  source_content_hash TEXT,
  attachment_hash TEXT,
  attachment_path TEXT,
  attachment_id INTEGER,
  status TEXT NOT NULL DEFAULT 'pending_parse'
    CHECK (status IN ('pending_parse', 'parsed_pending_confirmation', 'confirmed', 'rejected')),
  parser_output_id INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (attachment_id) REFERENCES attachments(id),
  FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id)
);

INSERT INTO raw_intake_records_v2 (
  id,
  public_id,
  source_type,
  source_channel,
  raw_input,
  received_at,
  source_received_at,
  status,
  parser_output_id,
  created_at,
  updated_at
)
SELECT
  id,
  public_id,
  source_type,
  CASE
    WHEN source_type LIKE 'telegram_%' THEN 'telegram'
    ELSE NULL
  END,
  raw_input,
  received_at,
  received_at,
  status,
  parser_output_id,
  created_at,
  updated_at
FROM raw_intake_records;

DROP TABLE raw_intake_records;
ALTER TABLE raw_intake_records_v2 RENAME TO raw_intake_records;

CREATE TABLE IF NOT EXISTS raw_intake_evidence (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  raw_intake_record_id INTEGER NOT NULL,
  attachment_id INTEGER,
  parser_output_id INTEGER,
  statement_batch_id INTEGER,
  evidence_type TEXT NOT NULL CHECK (
    evidence_type IN (
      'raw_input',
      'attachment',
      'image',
      'pdf',
      'ocr_text',
      'pdf_page',
      'statement_row',
      'statement_file',
      'imported_row',
      'user_message',
      'system'
    )
  ),
  attachment_path TEXT,
  source_file_hash TEXT,
  attachment_hash TEXT,
  ocr_text TEXT,
  pdf_page_number INTEGER CHECK (pdf_page_number IS NULL OR pdf_page_number > 0),
  ocr_bounding_box TEXT,
  statement_row_index INTEGER CHECK (statement_row_index IS NULL OR statement_row_index >= 0),
  statement_transaction_date TEXT,
  statement_posted_date TEXT,
  source_payload TEXT,
  parser_name TEXT,
  parser_version TEXT,
  confidence_score REAL CHECK (
    confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)
  ),
  extraction_method TEXT,
  evidence_reference TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (raw_intake_record_id) REFERENCES raw_intake_records(id),
  FOREIGN KEY (attachment_id) REFERENCES attachments(id),
  FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id),
  FOREIGN KEY (statement_batch_id) REFERENCES statement_batches(id)
);

CREATE INDEX IF NOT EXISTS idx_raw_intake_records_public_id
  ON raw_intake_records(public_id);
CREATE INDEX IF NOT EXISTS idx_raw_intake_records_source_type
  ON raw_intake_records(source_type);
CREATE INDEX IF NOT EXISTS idx_raw_intake_records_source_channel
  ON raw_intake_records(source_channel);
CREATE INDEX IF NOT EXISTS idx_raw_intake_records_status
  ON raw_intake_records(status);
CREATE INDEX IF NOT EXISTS idx_raw_intake_records_parser_output_id
  ON raw_intake_records(parser_output_id);
CREATE INDEX IF NOT EXISTS idx_raw_intake_records_attachment_id
  ON raw_intake_records(attachment_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_intake_records_idempotency_key_unique
  ON raw_intake_records(idempotency_key)
  WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_raw_intake_records_external_source
  ON raw_intake_records(source_type, source_channel, external_source_id)
  WHERE external_source_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_raw_intake_records_source_message_id
  ON raw_intake_records(source_message_id)
  WHERE source_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_raw_intake_records_source_content_hash
  ON raw_intake_records(source_content_hash)
  WHERE source_content_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_raw_intake_records_attachment_hash
  ON raw_intake_records(attachment_hash)
  WHERE attachment_hash IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_raw_intake_evidence_raw_intake_record_id
  ON raw_intake_evidence(raw_intake_record_id);
CREATE INDEX IF NOT EXISTS idx_raw_intake_evidence_attachment_id
  ON raw_intake_evidence(attachment_id);
CREATE INDEX IF NOT EXISTS idx_raw_intake_evidence_parser_output_id
  ON raw_intake_evidence(parser_output_id);
CREATE INDEX IF NOT EXISTS idx_raw_intake_evidence_statement_batch_id
  ON raw_intake_evidence(statement_batch_id);
CREATE INDEX IF NOT EXISTS idx_raw_intake_evidence_type
  ON raw_intake_evidence(evidence_type);
CREATE INDEX IF NOT EXISTS idx_raw_intake_evidence_reference
  ON raw_intake_evidence(evidence_reference);
CREATE INDEX IF NOT EXISTS idx_raw_intake_evidence_statement_row
  ON raw_intake_evidence(statement_row_index)
  WHERE statement_row_index IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_raw_intake_evidence_created_at
  ON raw_intake_evidence(created_at);

PRAGMA foreign_keys = ON;
