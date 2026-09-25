PRAGMA foreign_keys = ON;

-- A reply is delivery coordination for an existing, verified result.  No
-- financial authority or rendered monetary projection is stored here.
CREATE TABLE finance_capture_reply_outbox (
    public_id TEXT PRIMARY KEY,
    job_public_id TEXT NOT NULL REFERENCES finance_capture_jobs(public_id),
    result_kind TEXT NOT NULL CHECK (result_kind IN ('posting', 'correction')),
    result_public_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK
        (status IN ('pending', 'outcome_unknown', 'sent')),
    send_attempt_nonce TEXT,
    send_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (send_attempt_count >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (job_public_id, result_kind, result_public_id),
    CHECK ((send_attempt_count = 0 AND send_attempt_nonce IS NULL AND status = 'pending')
        OR (send_attempt_count > 0 AND send_attempt_nonce IS NOT NULL
            AND status IN ('outcome_unknown', 'sent')))
) STRICT;

CREATE INDEX finance_capture_reply_outbox_job_idx
    ON finance_capture_reply_outbox(job_public_id, created_at);
