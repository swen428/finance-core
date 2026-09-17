-- B5.1b local receipt source evidence foundation.
--
-- This migration:
-- 1. Recreates raw_intake_records with expanded source_type/source_channel
--    CHECK constraints to admit 'local_image' and 'local_file'.
-- 2. Creates local_attachment_source with append-only immutability,
--    cardinality uniqueness, and cross-table exclusion triggers.
-- 3. Adds local-channel raw-intake freeze triggers mirroring 031.
--
-- The table recreation follows the established migration-005 pattern.
-- All existing columns, indexes, FKs, and triggers are preserved exactly
-- as they exist after migration 039. No historical migration is modified.
-- No financial semantics, settlement, or reconciliation behavior changes.

PRAGMA foreign_keys = OFF;

-- ============================================================================
-- 1. Recreate raw_intake_records with expanded CHECK constraints
-- ============================================================================

CREATE TABLE raw_intake_records_v2 (
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
      'system_generated',
      'local_image'
    )
  ),
  source_channel TEXT CHECK (
    source_channel IS NULL OR source_channel IN (
      'telegram',
      'manual',
      'upload',
      'system',
      'imported_statement',
      'local_file'
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
  content_fingerprint TEXT,
  fingerprint_version TEXT,
  FOREIGN KEY (attachment_id) REFERENCES attachments(id),
  FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id)
);

INSERT INTO raw_intake_records_v2 (
  id, public_id, source_type, source_channel, raw_input, normalized_text,
  received_at, source_received_at, external_source_id, source_message_id,
  idempotency_key, source_content_hash, attachment_hash, attachment_path,
  attachment_id, status, parser_output_id, created_at, updated_at,
  content_fingerprint, fingerprint_version
)
SELECT
  id, public_id, source_type, source_channel, raw_input, normalized_text,
  received_at, source_received_at, external_source_id, source_message_id,
  idempotency_key, source_content_hash, attachment_hash, attachment_path,
  attachment_id, status, parser_output_id, created_at, updated_at,
  content_fingerprint, fingerprint_version
FROM raw_intake_records;

DROP TABLE raw_intake_records;

ALTER TABLE raw_intake_records_v2 RENAME TO raw_intake_records;

-- ============================================================================
-- 2. Recreate all indexes (from migrations 005, 022)
-- ============================================================================

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
CREATE INDEX IF NOT EXISTS idx_raw_intake_records_content_fingerprint
  ON raw_intake_records(content_fingerprint)
  WHERE content_fingerprint IS NOT NULL;

-- ============================================================================
-- 3. Recreate all triggers (from migrations 031, 035)
-- ============================================================================

-- 031: Telegram attachment freeze triggers
CREATE TRIGGER IF NOT EXISTS trg_raw_intake_no_detach_attachment_when_telegram_source
    BEFORE UPDATE OF attachment_id ON raw_intake_records
    FOR EACH ROW
    WHEN (OLD.attachment_id IS NOT NEW.attachment_id)
       AND EXISTS (
           SELECT 1 FROM telegram_attachment_source
           WHERE raw_intake_record_id = OLD.id
       )
BEGIN
    SELECT RAISE(
        ABORT,
        'Raw-intake attachment relationship is immutable once Telegram source evidence exists'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_raw_intake_no_change_path_when_telegram_source
    BEFORE UPDATE OF attachment_path ON raw_intake_records
    FOR EACH ROW
    WHEN (OLD.attachment_path IS NOT NEW.attachment_path)
       AND EXISTS (
           SELECT 1 FROM telegram_attachment_source
           WHERE raw_intake_record_id = OLD.id
       )
BEGIN
    SELECT RAISE(
        ABORT,
        'Raw-intake attachment_path is immutable once Telegram source evidence exists'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_raw_intake_no_change_hash_when_telegram_source
    BEFORE UPDATE OF attachment_hash ON raw_intake_records
    FOR EACH ROW
    WHEN (OLD.attachment_hash IS NOT NEW.attachment_hash)
       AND EXISTS (
           SELECT 1 FROM telegram_attachment_source
           WHERE raw_intake_record_id = OLD.id
       )
BEGIN
    SELECT RAISE(
        ABORT,
        'Raw-intake attachment_hash is immutable once Telegram source evidence exists'
    );
END;

-- 035: Lineage freeze and collision triggers
CREATE TRIGGER IF NOT EXISTS trg_raw_intake_records_freeze_source_identity
    BEFORE UPDATE
    ON raw_intake_records
    FOR EACH ROW
    WHEN (
        NEW.id IS NOT OLD.id
        OR NEW.public_id IS NOT OLD.public_id
        OR NEW.raw_input IS NOT OLD.raw_input
        OR NEW.source_content_hash IS NOT OLD.source_content_hash
        OR NEW.content_fingerprint IS NOT OLD.content_fingerprint
        OR NEW.fingerprint_version IS NOT OLD.fingerprint_version
    )
    AND (
        OLD.parser_output_id IS NOT NULL
        OR EXISTS (
            SELECT 1 FROM parser_outputs
            WHERE source_public_id = OLD.public_id
        )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'raw intake source identity is frozen once receipt proposal lineage is established'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_raw_intake_records_pointer_lineage_control
    BEFORE UPDATE OF parser_output_id ON raw_intake_records
    FOR EACH ROW
    WHEN OLD.parser_output_id IS NOT NULL
        AND NEW.parser_output_id IS NOT OLD.parser_output_id
        AND NOT EXISTS (
            SELECT 1 FROM parser_outputs
            WHERE id = NEW.parser_output_id
              AND parent_parser_output_id = OLD.parser_output_id
        )
BEGIN
    SELECT RAISE(
        ABORT,
        'raw intake proposal lineage pointer cannot be detached or retargeted once established'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_raw_intake_records_no_insert_collision
    BEFORE INSERT ON raw_intake_records
    WHEN EXISTS (
        SELECT 1 FROM raw_intake_records AS existing
        WHERE (
            existing.id = NEW.id
            OR existing.public_id = NEW.public_id
            OR (
                NEW.idempotency_key IS NOT NULL
                AND existing.idempotency_key = NEW.idempotency_key
            )
        )
        AND (
            existing.parser_output_id IS NOT NULL
            OR EXISTS (
                SELECT 1 FROM parser_outputs
                WHERE source_public_id = existing.public_id
            )
        )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'UNIQUE raw intake identity collision (id, public_id, idempotency_key): lineage-bound raw_intake_records rows cannot be replaced'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_raw_intake_records_no_delete_lineage_bound
    BEFORE DELETE ON raw_intake_records
    WHEN OLD.parser_output_id IS NOT NULL
        OR EXISTS (
            SELECT 1 FROM parser_outputs
            WHERE source_public_id = OLD.public_id
        )
BEGIN
    SELECT RAISE(
        ABORT,
        'lineage-bound raw_intake_records rows cannot be deleted'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_raw_intake_records_no_update_collision
    BEFORE UPDATE ON raw_intake_records
    FOR EACH ROW
    WHEN EXISTS (
        SELECT 1 FROM raw_intake_records AS existing
        WHERE existing.id <> OLD.id
        AND (
            existing.id = NEW.id
            OR existing.public_id = NEW.public_id
            OR (
                NEW.idempotency_key IS NOT NULL
                AND existing.idempotency_key = NEW.idempotency_key
            )
        )
        AND (
            existing.parser_output_id IS NOT NULL
            OR EXISTS (
                SELECT 1 FROM parser_outputs
                WHERE source_public_id = existing.public_id
            )
        )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'UNIQUE raw intake identity collision (id, public_id, idempotency_key): lineage-bound raw_intake_records rows cannot be replaced'
    );
END;

-- ============================================================================
-- 4. Create local_attachment_source table
-- ============================================================================

CREATE TABLE IF NOT EXISTS local_attachment_source (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE CHECK (public_id GLOB 'lae_*'),
    attachment_id INTEGER NOT NULL REFERENCES attachments(id),
    raw_intake_record_id INTEGER NOT NULL REFERENCES raw_intake_records(id),
    original_filename TEXT NOT NULL CHECK (length(trim(original_filename)) > 0),
    declared_mime_type TEXT NOT NULL CHECK (declared_mime_type IN ('image/jpeg', 'image/png')),
    workspace_copy_path TEXT NOT NULL CHECK (length(trim(workspace_copy_path)) > 0),
    observed_file_size INTEGER NOT NULL CHECK (observed_file_size > 0),
    content_hash TEXT NOT NULL CHECK (
        length(content_hash) = 64
        AND lower(content_hash) = content_hash
        AND content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    workspace_identity TEXT NOT NULL CHECK (length(trim(workspace_identity)) > 0),
    operator_actor_id TEXT NOT NULL CHECK (length(trim(operator_actor_id)) > 0),
    source_evidence_payload TEXT NOT NULL CHECK (json_valid(source_evidence_payload) = 1),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Cardinality uniqueness constraints
CREATE UNIQUE INDEX IF NOT EXISTS idx_local_attachment_source_attachment_id
  ON local_attachment_source(attachment_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_local_attachment_source_raw_intake
  ON local_attachment_source(raw_intake_record_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_local_attachment_source_content_hash
  ON local_attachment_source(content_hash);

-- ============================================================================
-- 5. Append-only immutability triggers for local_attachment_source
-- ============================================================================

CREATE TRIGGER IF NOT EXISTS trg_local_attachment_source_no_update
  BEFORE UPDATE ON local_attachment_source
  BEGIN
    SELECT RAISE(ABORT, 'local_attachment_source rows are immutable append-only evidence');
  END;

CREATE TRIGGER IF NOT EXISTS trg_local_attachment_source_no_delete
  BEFORE DELETE ON local_attachment_source
  BEGIN
    SELECT RAISE(ABORT, 'local_attachment_source rows are immutable append-only evidence');
  END;

-- ============================================================================
-- 6. Cross-table exclusion: prohibit dual Telegram + local source
-- ============================================================================

CREATE TRIGGER IF NOT EXISTS trg_local_attachment_source_no_telegram_coexist
  BEFORE INSERT ON local_attachment_source
  WHEN EXISTS (SELECT 1 FROM telegram_attachment_source WHERE attachment_id = NEW.attachment_id)
  BEGIN
    SELECT RAISE(ABORT, 'attachment already has telegram source evidence; cannot add local source');
  END;

CREATE TRIGGER IF NOT EXISTS trg_telegram_attachment_source_no_local_coexist
  BEFORE INSERT ON telegram_attachment_source
  WHEN EXISTS (SELECT 1 FROM local_attachment_source WHERE attachment_id = NEW.attachment_id)
  BEGIN
    SELECT RAISE(ABORT, 'attachment already has local source evidence; cannot add telegram source');
  END;

-- ============================================================================
-- 7. Raw intake freeze triggers for local channel (mirrors 031)
-- ============================================================================

CREATE TRIGGER IF NOT EXISTS trg_raw_intake_no_detach_attachment_when_local_source
  BEFORE UPDATE OF attachment_id ON raw_intake_records
  FOR EACH ROW
  WHEN (OLD.attachment_id IS NOT NEW.attachment_id)
     AND EXISTS (
         SELECT 1 FROM local_attachment_source
         WHERE raw_intake_record_id = OLD.id
     )
BEGIN
    SELECT RAISE(
        ABORT,
        'Raw-intake attachment relationship is immutable once local source evidence exists'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_raw_intake_no_change_path_when_local_source
  BEFORE UPDATE OF attachment_path ON raw_intake_records
  FOR EACH ROW
  WHEN (OLD.attachment_path IS NOT NEW.attachment_path)
     AND EXISTS (
         SELECT 1 FROM local_attachment_source
         WHERE raw_intake_record_id = OLD.id
     )
BEGIN
    SELECT RAISE(
        ABORT,
        'Raw-intake attachment_path is immutable once local source evidence exists'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_raw_intake_no_change_hash_when_local_source
  BEFORE UPDATE OF attachment_hash ON raw_intake_records
  FOR EACH ROW
  WHEN (OLD.attachment_hash IS NOT NEW.attachment_hash)
     AND EXISTS (
         SELECT 1 FROM local_attachment_source
         WHERE raw_intake_record_id = OLD.id
     )
BEGIN
    SELECT RAISE(
        ABORT,
        'Raw-intake attachment_hash is immutable once local source evidence exists'
    );
END;

-- ============================================================================
-- 8. AFTER INSERT trigger: write raw_intake_evidence for local source
-- ============================================================================

CREATE TRIGGER IF NOT EXISTS trg_local_attachment_source_write_evidence
  AFTER INSERT ON local_attachment_source
  FOR EACH ROW
BEGIN
    INSERT INTO raw_intake_evidence (
        public_id,
        raw_intake_record_id,
        attachment_id,
        evidence_type,
        attachment_path,
        source_file_hash,
        attachment_hash,
        source_payload,
        extraction_method,
        evidence_reference,
        created_at
    ) VALUES (
        'rie_' || NEW.public_id,
        NEW.raw_intake_record_id,
        NEW.attachment_id,
        'attachment',
        NEW.workspace_copy_path,
        NEW.content_hash,
        NEW.content_hash,
        NEW.source_evidence_payload,
        'local_file_import',
        NEW.public_id,
        NEW.created_at
    );
END;

-- ============================================================================
-- 9. Attachment identity freeze triggers for local source
-- ============================================================================

CREATE TRIGGER IF NOT EXISTS trg_attachments_no_update_key_fields_when_local_referenced
  BEFORE UPDATE ON attachments
  FOR EACH ROW
  WHEN (
      OLD.file_path IS NOT NEW.file_path
      OR OLD.file_hash IS NOT NEW.file_hash
      OR OLD.original_filename IS NOT NEW.original_filename
      OR OLD.mime_type IS NOT NEW.mime_type
  )
  AND EXISTS (
      SELECT 1 FROM local_attachment_source
      WHERE attachment_id = OLD.id
  )
BEGIN
    SELECT RAISE(
        ABORT,
        'Attachment key fields are immutable once local source evidence exists'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_attachments_no_delete_when_local_referenced
  BEFORE DELETE ON attachments
  FOR EACH ROW
  WHEN EXISTS (
      SELECT 1 FROM local_attachment_source
      WHERE attachment_id = OLD.id
  )
BEGIN
    SELECT RAISE(
        ABORT,
        'Cannot delete attachment referenced by local source evidence'
    );
END;

PRAGMA foreign_keys = ON;
