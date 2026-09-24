PRAGMA foreign_keys = ON;

-- The durable processing identity is created only after source evidence is
-- committed. An image job always names its immutable attachment evidence.
CREATE TABLE finance_capture_jobs (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    raw_intake_record_id INTEGER NOT NULL UNIQUE REFERENCES raw_intake_records(id),
    capture_kind TEXT NOT NULL CHECK (capture_kind IN ('text', 'receipt_image')),
    intake_fingerprint TEXT NOT NULL CHECK (length(intake_fingerprint) = 64),
    attachment_evidence_id INTEGER REFERENCES telegram_attachment_source(id),
    attachment_content_hash TEXT,
    ingress_identity_digest TEXT CHECK (ingress_identity_digest IS NULL OR
        (length(ingress_identity_digest) = 64 AND
         ingress_identity_digest NOT GLOB '*[^0-9a-f]*')),
    status TEXT NOT NULL DEFAULT 'captured' CHECK (status IN
        ('captured', 'processing', 'awaiting_user', 'needs_attention', 'result_ready')),
    ai_status TEXT NOT NULL DEFAULT 'not_started' CHECK (ai_status IN
        ('not_started', 'in_flight', 'completed', 'outcome_unknown')),
    reply_status TEXT NOT NULL DEFAULT 'pending' CHECK (reply_status IN
        ('pending', 'sent', 'outcome_unknown')),
    lease_epoch INTEGER NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
    lease_owner TEXT,
    lease_expires_at INTEGER,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((capture_kind = 'text' AND attachment_evidence_id IS NULL
            AND attachment_content_hash IS NULL)
        OR (capture_kind = 'receipt_image' AND attachment_evidence_id IS NOT NULL
            AND attachment_content_hash IS NOT NULL
            AND length(attachment_content_hash) = 64)),
    CHECK ((lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))
) STRICT;

CREATE INDEX finance_capture_jobs_status_idx
    ON finance_capture_jobs(status, id);

-- SQLite CHECK treats NULL as passing. The explicit non-NULL check above and
-- this join require a receipt job to name the source for its own intake,
-- canonical attachment, and original content hash at insert time.
CREATE TRIGGER trg_finance_capture_jobs_require_receipt_source
BEFORE INSERT ON finance_capture_jobs
FOR EACH ROW
WHEN NEW.capture_kind = 'receipt_image'
AND NOT EXISTS (
    SELECT 1 FROM telegram_attachment_source AS source
    JOIN raw_intake_records AS intake ON intake.id = NEW.raw_intake_record_id
    WHERE source.id = NEW.attachment_evidence_id
      AND source.raw_intake_record_id = intake.id
      AND source.attachment_id = intake.attachment_id
      AND source.content_hash = NEW.attachment_content_hash
      AND intake.attachment_hash = source.content_hash
)
BEGIN
    SELECT RAISE(ABORT, 'receipt capture job source linkage mismatch');
END;

-- A capture job may advance its status and lease, but its adopted source
-- identity must remain the one that was committed with the original intake.
CREATE TRIGGER trg_finance_capture_jobs_freeze_identity
BEFORE UPDATE ON finance_capture_jobs
FOR EACH ROW
WHEN NEW.id IS NOT OLD.id
  OR NEW.public_id IS NOT OLD.public_id
  OR NEW.raw_intake_record_id IS NOT OLD.raw_intake_record_id
  OR NEW.capture_kind IS NOT OLD.capture_kind
  OR NEW.intake_fingerprint IS NOT OLD.intake_fingerprint
  OR NEW.attachment_evidence_id IS NOT OLD.attachment_evidence_id
  OR NEW.attachment_content_hash IS NOT OLD.attachment_content_hash
  OR NEW.ingress_identity_digest IS NOT OLD.ingress_identity_digest
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'capture job source identity is immutable');
END;

CREATE TRIGGER trg_finance_capture_jobs_no_delete
BEFORE DELETE ON finance_capture_jobs
BEGIN
    SELECT RAISE(ABORT, 'capture job cannot be deleted');
END;

-- INSERT OR REPLACE may skip DELETE triggers unless recursive triggers are
-- enabled. Refuse collisions before SQLite can replace a sealed row.
CREATE TRIGGER trg_finance_capture_jobs_no_insert_collision
BEFORE INSERT ON finance_capture_jobs
WHEN EXISTS (
    SELECT 1 FROM finance_capture_jobs AS existing
    WHERE existing.id = NEW.id
       OR existing.public_id = NEW.public_id
       OR existing.raw_intake_record_id = NEW.raw_intake_record_id
)
BEGIN
    SELECT RAISE(ABORT, 'capture job identity collision cannot replace evidence');
END;

-- Receipt intake can precede the job in a crash window. Once the job exists,
-- all source fields, including the original caption, are sealed. Processing
-- status, parser pointer and updated_at can still advance normally.
CREATE TRIGGER trg_capture_raw_intake_freeze_source
BEFORE UPDATE ON raw_intake_records
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM finance_capture_jobs WHERE raw_intake_record_id = OLD.id
)
AND (
    NEW.id IS NOT OLD.id
    OR NEW.public_id IS NOT OLD.public_id
    OR NEW.source_type IS NOT OLD.source_type
    OR NEW.source_channel IS NOT OLD.source_channel
    OR NEW.raw_input IS NOT OLD.raw_input
    OR NEW.normalized_text IS NOT OLD.normalized_text
    OR NEW.received_at IS NOT OLD.received_at
    OR NEW.source_received_at IS NOT OLD.source_received_at
    OR NEW.external_source_id IS NOT OLD.external_source_id
    OR NEW.source_message_id IS NOT OLD.source_message_id
    OR NEW.idempotency_key IS NOT OLD.idempotency_key
    OR NEW.source_content_hash IS NOT OLD.source_content_hash
    OR NEW.attachment_hash IS NOT OLD.attachment_hash
    OR NEW.attachment_path IS NOT OLD.attachment_path
    OR NEW.attachment_id IS NOT OLD.attachment_id
    OR NEW.content_fingerprint IS NOT OLD.content_fingerprint
    OR NEW.fingerprint_version IS NOT OLD.fingerprint_version
    OR NEW.created_at IS NOT OLD.created_at
)
BEGIN
    SELECT RAISE(ABORT, 'capture raw intake source identity is immutable');
END;

CREATE TRIGGER trg_capture_raw_intake_no_delete
BEFORE DELETE ON raw_intake_records
WHEN EXISTS (
    SELECT 1 FROM finance_capture_jobs WHERE raw_intake_record_id = OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'capture raw intake cannot be deleted');
END;

CREATE TRIGGER trg_capture_raw_intake_no_insert_collision
BEFORE INSERT ON raw_intake_records
WHEN EXISTS (
    SELECT 1 FROM raw_intake_records AS existing
    JOIN finance_capture_jobs AS job ON job.raw_intake_record_id = existing.id
    WHERE existing.id = NEW.id
       OR existing.public_id = NEW.public_id
       OR (NEW.idempotency_key IS NOT NULL
           AND existing.idempotency_key = NEW.idempotency_key)
)
BEGIN
    SELECT CASE
        WHEN NEW.idempotency_key IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM raw_intake_records AS existing
              WHERE existing.id = NEW.id OR existing.public_id = NEW.public_id
          )
          AND EXISTS (
              SELECT 1 FROM raw_intake_records AS existing
              JOIN finance_capture_jobs AS job ON job.raw_intake_record_id = existing.id
              WHERE existing.idempotency_key = NEW.idempotency_key
          )
        THEN RAISE(ABORT, 'capture raw intake idempotency_key collision cannot replace evidence')
        ELSE RAISE(ABORT, 'capture raw intake identity collision cannot replace evidence')
    END;
END;
