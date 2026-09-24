-- D3 local processing identities. Existing captured jobs remain resumable.
-- A nullable binding is set only by the leased processor; source evidence in
-- migration 052 and all financial authority remain unchanged.
ALTER TABLE finance_capture_jobs ADD COLUMN ocr_extraction_public_id TEXT;
ALTER TABLE finance_capture_jobs ADD COLUMN proposal_public_id TEXT;
ALTER TABLE finance_capture_jobs ADD COLUMN proposal_link_public_id TEXT;
ALTER TABLE finance_capture_jobs ADD COLUMN ai_attempt_public_id TEXT;

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
