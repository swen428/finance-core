PRAGMA foreign_keys = ON;

-- Parser proposal confirmation audit layer.
-- This migration is additive only. It keeps parser_outputs as the proposal
-- anchor and does not create final transaction rows.

CREATE TABLE IF NOT EXISTS parser_proposal_events (
  id INTEGER PRIMARY KEY,
  parser_output_id INTEGER NOT NULL,
  from_status TEXT CHECK (
    from_status IS NULL OR from_status IN (
      'parsed_pending_confirmation',
      'confirmed',
      'rejected',
      'edited_pending_confirmation',
      'superseded',
      'expired'
    )
  ),
  to_status TEXT NOT NULL CHECK (
    to_status IN (
      'parsed_pending_confirmation',
      'confirmed',
      'rejected',
      'edited_pending_confirmation',
      'superseded',
      'expired'
    )
  ),
  event_type TEXT NOT NULL CHECK (
    event_type IN (
      'created',
      'confirmed',
      'rejected',
      'edited',
      'superseded',
      'expired',
      'validation_failed',
      'validation_passed'
    )
  ),
  event_reason TEXT,
  actor_type TEXT NOT NULL CHECK (actor_type IN ('user', 'system', 'ai', 'cli', 'test')),
  actor_identifier TEXT,
  event_payload TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id)
);

CREATE TABLE IF NOT EXISTS parser_proposal_confirmations (
  id INTEGER PRIMARY KEY,
  parser_output_id INTEGER NOT NULL,
  decision TEXT NOT NULL CHECK (
    decision IN (
      'confirmed',
      'rejected',
      'edited',
      'superseded',
      'expired'
    )
  ),
  decided_by TEXT NOT NULL,
  decision_reason TEXT,
  decision_payload TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id)
);

CREATE TABLE IF NOT EXISTS parser_proposal_field_evidence (
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
      'system'
    )
  ),
  evidence_reference TEXT,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id)
);

CREATE INDEX IF NOT EXISTS idx_parser_proposal_events_parser_output_id
  ON parser_proposal_events(parser_output_id);
CREATE INDEX IF NOT EXISTS idx_parser_proposal_events_to_status
  ON parser_proposal_events(to_status);
CREATE INDEX IF NOT EXISTS idx_parser_proposal_events_event_type
  ON parser_proposal_events(event_type);
CREATE INDEX IF NOT EXISTS idx_parser_proposal_events_created_at
  ON parser_proposal_events(created_at);

CREATE INDEX IF NOT EXISTS idx_parser_proposal_confirmations_parser_output_id
  ON parser_proposal_confirmations(parser_output_id);
CREATE INDEX IF NOT EXISTS idx_parser_proposal_confirmations_decision
  ON parser_proposal_confirmations(decision);
CREATE INDEX IF NOT EXISTS idx_parser_proposal_confirmations_created_at
  ON parser_proposal_confirmations(created_at);

CREATE INDEX IF NOT EXISTS idx_parser_proposal_field_evidence_parser_output_id
  ON parser_proposal_field_evidence(parser_output_id);
CREATE INDEX IF NOT EXISTS idx_parser_proposal_field_evidence_field_name
  ON parser_proposal_field_evidence(field_name);
CREATE INDEX IF NOT EXISTS idx_parser_proposal_field_evidence_source_type
  ON parser_proposal_field_evidence(evidence_source_type);
