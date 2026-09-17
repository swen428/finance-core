PRAGMA foreign_keys = ON;

-- Telegram attachment source identity and content evidence.
--
-- This migration records Telegram-specific source identity and immutable
-- operation-time observed facts for an attachment that has already been
-- acquired and stored locally.  The canonical attachment metadata lives
-- in the `attachments` table (created by migration 001); this table is
-- the Telegram-specific extension linked to that authority.
--
-- No network acquisition is performed here (see PR #217).

CREATE TABLE IF NOT EXISTS telegram_attachment_source (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL CHECK (length(trim(public_id)) > 0),
    attachment_id INTEGER NOT NULL,
    raw_intake_record_id INTEGER NOT NULL,
    telegram_file_id TEXT CHECK (
        telegram_file_id IS NULL OR length(trim(telegram_file_id)) > 0
    ),
    telegram_file_unique_id TEXT CHECK (
        telegram_file_unique_id IS NULL
        OR length(trim(telegram_file_unique_id)) > 0
    ),
    original_filename TEXT CHECK (
        original_filename IS NULL OR length(trim(original_filename)) > 0
    ),
    declared_mime_type TEXT CHECK (
        declared_mime_type IS NULL OR length(trim(declared_mime_type)) > 0
    ),
    original_attachment_path TEXT NOT NULL CHECK (
        length(trim(original_attachment_path)) > 0
    ),
    observed_file_size INTEGER NOT NULL CHECK (observed_file_size >= 0),
    content_hash TEXT NOT NULL CHECK (
        length(content_hash) = 64
        AND lower(content_hash) = content_hash
        AND content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    source_evidence_payload TEXT NOT NULL CHECK (
        json_valid(source_evidence_payload) = 1
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (attachment_id) REFERENCES attachments(id),
    FOREIGN KEY (raw_intake_record_id) REFERENCES raw_intake_records(id),
    UNIQUE (public_id),
    UNIQUE (attachment_id, raw_intake_record_id)
);

-- -----------------------------------------------------------------------
-- Append-only enforcement
-- -----------------------------------------------------------------------

CREATE TRIGGER IF NOT EXISTS trg_telegram_attachment_source_no_update
    BEFORE UPDATE ON telegram_attachment_source
BEGIN
    SELECT RAISE(
        ABORT,
        'telegram_attachment_source rows are append-only'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_telegram_attachment_source_no_delete
    BEFORE DELETE ON telegram_attachment_source
BEGIN
    SELECT RAISE(
        ABORT,
        'telegram_attachment_source rows are append-only'
    );
END;

-- -----------------------------------------------------------------------
-- Unique Telegram file identity (partial; skips NULLs)
-- -----------------------------------------------------------------------

CREATE UNIQUE INDEX IF NOT EXISTS
    idx_telegram_attachment_source_file_unique_id
    ON telegram_attachment_source(telegram_file_unique_id)
    WHERE telegram_file_unique_id IS NOT NULL;

-- -----------------------------------------------------------------------
-- Lookup indexes
-- -----------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_telegram_attachment_source_attachment_id
    ON telegram_attachment_source(attachment_id);

CREATE INDEX IF NOT EXISTS idx_telegram_attachment_source_raw_intake_id
    ON telegram_attachment_source(raw_intake_record_id);

CREATE INDEX IF NOT EXISTS idx_telegram_attachment_source_content_hash
    ON telegram_attachment_source(content_hash);

-- -----------------------------------------------------------------------
-- Evidence trigger — insert a matching raw_intake_evidence row
-- -----------------------------------------------------------------------

CREATE TRIGGER IF NOT EXISTS trg_telegram_attachment_source_evidence
    AFTER INSERT ON telegram_attachment_source
BEGIN
    INSERT INTO raw_intake_evidence (
        public_id,
        raw_intake_record_id,
        attachment_id,
        evidence_type,
        attachment_path,
        source_file_hash,
        attachment_hash,
        evidence_reference,
        created_at
    ) VALUES (
        'evidence_' || NEW.public_id,
        NEW.raw_intake_record_id,
        NEW.attachment_id,
        'attachment',
        NEW.original_attachment_path,
        NEW.content_hash,
        NEW.content_hash,
        NEW.public_id,
        NEW.created_at
    );
END;

-- -----------------------------------------------------------------------
-- Canonical attachment immutability for rows referenced by Telegram source
-- -----------------------------------------------------------------------

-- Reject changes to key fields on attachments rows that are referenced
-- by telegram_attachment_source.  Once a Telegram source row exists the
-- attachment's identity facts are frozen.
CREATE TRIGGER IF NOT EXISTS trg_attachments_no_update_key_fields_when_referenced
    BEFORE UPDATE ON attachments
    FOR EACH ROW
    WHEN EXISTS (
        SELECT 1 FROM telegram_attachment_source
        WHERE attachment_id = OLD.id
    )
BEGIN
    SELECT CASE
        WHEN OLD.file_path != NEW.file_path
           OR OLD.file_hash != NEW.file_hash
           OR OLD.original_filename IS NOT NEW.original_filename
           OR OLD.mime_type IS NOT NEW.mime_type
        THEN RAISE(
            ABORT,
            'Referenced attachment key fields are immutable'
        )
    END;
END;

-- Reject deletion of attachments rows that are referenced by
-- telegram_attachment_source.
CREATE TRIGGER IF NOT EXISTS trg_attachments_no_delete_when_referenced
    BEFORE DELETE ON attachments
    FOR EACH ROW
    WHEN EXISTS (
        SELECT 1 FROM telegram_attachment_source
        WHERE attachment_id = OLD.id
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'Referenced attachment rows cannot be deleted'
    );
END;


-- Freeze raw-intake attachment_id, attachment_path, and attachment_hash
-- once Telegram source evidence exists for that raw-intake record.
-- Uses NULL-safe comparisons with IS NOT.
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

-- Freeze raw-intake attachment_path once Telegram source evidence exists.
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

-- Freeze raw-intake attachment_hash once Telegram source evidence exists.
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
