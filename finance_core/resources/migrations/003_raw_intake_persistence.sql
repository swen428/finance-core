PRAGMA foreign_keys = ON;

-- Raw intake persistence layer.
-- This migration is additive only. It preserves raw text input before parser
-- interpretation and links parser proposals through the existing parser_outputs
-- table.

CREATE TABLE IF NOT EXISTS raw_intake_records (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  source_type TEXT NOT NULL CHECK (source_type IN ('telegram_text')),
  raw_input TEXT NOT NULL,
  received_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending_parse'
    CHECK (status IN ('pending_parse', 'parsed_pending_confirmation', 'confirmed', 'rejected')),
  parser_output_id INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id)
);

CREATE INDEX IF NOT EXISTS idx_raw_intake_records_public_id ON raw_intake_records(public_id);
CREATE INDEX IF NOT EXISTS idx_raw_intake_records_source_type ON raw_intake_records(source_type);
CREATE INDEX IF NOT EXISTS idx_raw_intake_records_status ON raw_intake_records(status);
CREATE INDEX IF NOT EXISTS idx_raw_intake_records_parser_output_id ON raw_intake_records(parser_output_id);
