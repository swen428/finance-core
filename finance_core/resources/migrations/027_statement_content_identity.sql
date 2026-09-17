PRAGMA foreign_keys = ON;

-- Versioned source-content and row identity. Historical rows remain explicitly
-- legacy_unverified; no content hash is fabricated without source bytes.
ALTER TABLE statement_import_batches
ADD COLUMN source_filename TEXT;

ALTER TABLE statement_import_batches
ADD COLUMN source_hash_verification_status TEXT NOT NULL DEFAULT 'legacy_unverified'
CHECK (source_hash_verification_status IN (
  'verified_from_bytes',
  'provided_unverified',
  'unverified_no_source_bytes',
  'legacy_unverified'
));

ALTER TABLE statement_import_batches
ADD COLUMN import_contract_version TEXT;

ALTER TABLE statement_import_batches
ADD COLUMN import_command_hash TEXT;

ALTER TABLE statement_import_batches
ADD COLUMN row_set_fingerprint TEXT;

ALTER TABLE statement_transactions
ADD COLUMN row_fingerprint_version TEXT;

CREATE UNIQUE INDEX idx_statement_import_batches_command_hash
  ON statement_import_batches(import_command_hash)
  WHERE import_command_hash IS NOT NULL;

CREATE TABLE statement_import_source_evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  source_content_hash TEXT NOT NULL,
  evidence_path TEXT NOT NULL,
  original_filename TEXT NOT NULL,
  verification_status TEXT NOT NULL DEFAULT 'verified_from_bytes'
    CHECK (verification_status = 'verified_from_bytes'),
  observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (batch_id) REFERENCES statement_import_batches(id) ON DELETE RESTRICT,
  UNIQUE (batch_id, evidence_path, source_content_hash),
  CHECK (
    length(source_content_hash) = 64
    AND source_content_hash NOT GLOB '*[^0-9a-f]*'
  )
);

CREATE INDEX idx_statement_import_source_evidence_hash
  ON statement_import_source_evidence(source_content_hash);

CREATE TRIGGER trg_statement_import_identity_insert
BEFORE INSERT ON statement_import_batches
WHEN NEW.import_contract_version IS NOT NULL AND (
  NEW.import_command_hash IS NULL
  OR length(NEW.import_command_hash) != 64
  OR NEW.import_command_hash GLOB '*[^0-9a-f]*'
  OR NEW.row_set_fingerprint IS NULL
  OR length(NEW.row_set_fingerprint) != 64
  OR NEW.row_set_fingerprint GLOB '*[^0-9a-f]*'
  OR (
    NEW.source_file_hash IS NOT NULL AND (
      length(NEW.source_file_hash) != 64
      OR NEW.source_file_hash GLOB '*[^0-9a-f]*'
    )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'invalid authoritative statement import identity');
END;

CREATE TRIGGER trg_statement_import_identity_update
BEFORE UPDATE OF source_file_hash, import_contract_version, import_command_hash,
  row_set_fingerprint ON statement_import_batches
WHEN NEW.import_contract_version IS NOT NULL AND (
  NEW.import_command_hash IS NULL
  OR length(NEW.import_command_hash) != 64
  OR NEW.import_command_hash GLOB '*[^0-9a-f]*'
  OR NEW.row_set_fingerprint IS NULL
  OR length(NEW.row_set_fingerprint) != 64
  OR NEW.row_set_fingerprint GLOB '*[^0-9a-f]*'
  OR (
    NEW.source_file_hash IS NOT NULL AND (
      length(NEW.source_file_hash) != 64
      OR NEW.source_file_hash GLOB '*[^0-9a-f]*'
    )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'invalid authoritative statement import identity');
END;

CREATE TRIGGER trg_statement_row_fingerprint_insert
BEFORE INSERT ON statement_transactions
WHEN NEW.row_fingerprint_version IS NOT NULL AND (
  NEW.row_fingerprint IS NULL
  OR length(NEW.row_fingerprint) != 64
  OR NEW.row_fingerprint GLOB '*[^0-9a-f]*'
)
BEGIN
  SELECT RAISE(ABORT, 'invalid authoritative statement row fingerprint');
END;

CREATE TRIGGER trg_statement_row_fingerprint_update
BEFORE UPDATE OF row_fingerprint, row_fingerprint_version ON statement_transactions
WHEN NEW.row_fingerprint_version IS NOT NULL AND (
  NEW.row_fingerprint IS NULL
  OR length(NEW.row_fingerprint) != 64
  OR NEW.row_fingerprint GLOB '*[^0-9a-f]*'
)
BEGIN
  SELECT RAISE(ABORT, 'invalid authoritative statement row fingerprint');
END;
