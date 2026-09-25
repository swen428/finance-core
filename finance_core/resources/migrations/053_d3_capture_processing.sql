-- D3 local processing identities. Existing captured jobs remain resumable.
-- A nullable binding is set only by the leased processor; source evidence in
-- migration 052 and all financial authority remain unchanged.
ALTER TABLE finance_capture_jobs ADD COLUMN ocr_extraction_public_id TEXT;
ALTER TABLE finance_capture_jobs ADD COLUMN proposal_public_id TEXT;
ALTER TABLE finance_capture_jobs ADD COLUMN proposal_link_public_id TEXT;
ALTER TABLE finance_capture_jobs ADD COLUMN ai_attempt_public_id TEXT;
-- Only a bounded local OCR deadline may defer retry. A claim clears the
-- not-before time; the monotonically increasing count remains as evidence.
ALTER TABLE finance_capture_jobs ADD COLUMN ocr_retry_count INTEGER NOT NULL DEFAULT 0
    CHECK (ocr_retry_count BETWEEN 0 AND 3);
ALTER TABLE finance_capture_jobs ADD COLUMN ocr_retry_not_before_ms INTEGER NOT NULL DEFAULT 0
    CHECK (ocr_retry_not_before_ms >= 0);

CREATE UNIQUE INDEX finance_capture_jobs_ocr_identity_idx
    ON finance_capture_jobs(ocr_extraction_public_id)
    WHERE ocr_extraction_public_id IS NOT NULL;
CREATE UNIQUE INDEX finance_capture_jobs_proposal_identity_idx
    ON finance_capture_jobs(proposal_public_id)
    WHERE proposal_public_id IS NOT NULL;
CREATE UNIQUE INDEX finance_capture_jobs_proposal_link_identity_idx
    ON finance_capture_jobs(proposal_link_public_id)
    WHERE proposal_link_public_id IS NOT NULL;
CREATE UNIQUE INDEX finance_capture_jobs_ai_attempt_identity_idx
    ON finance_capture_jobs(ai_attempt_public_id)
    WHERE ai_attempt_public_id IS NOT NULL;
