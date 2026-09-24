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
            AND length(attachment_content_hash) = 64)),
    CHECK ((lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))
) STRICT;

CREATE INDEX finance_capture_jobs_status_idx
    ON finance_capture_jobs(status, id);
