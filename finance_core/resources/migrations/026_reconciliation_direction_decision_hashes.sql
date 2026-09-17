PRAGMA foreign_keys = ON;

-- Additive reconciliation decision proof.  Historical rows remain nullable and
-- are explicitly unverifiable; every new service-owned decision populates all
-- version, material, and hash fields.
ALTER TABLE reconciliation_match_results
ADD COLUMN decision_contract_version TEXT;

ALTER TABLE reconciliation_match_results
ADD COLUMN matcher_version TEXT;

ALTER TABLE reconciliation_match_results
ADD COLUMN compatibility_version TEXT;

ALTER TABLE reconciliation_match_results
ADD COLUMN merchant_normalization_version TEXT;

ALTER TABLE reconciliation_match_results
ADD COLUMN candidate_set_fingerprint TEXT CHECK (
  candidate_set_fingerprint IS NULL OR (
    length(candidate_set_fingerprint) = 64
    AND candidate_set_fingerprint NOT GLOB '*[^0-9a-f]*'
  )
);

ALTER TABLE reconciliation_match_results
ADD COLUMN decision_hash TEXT CHECK (
  decision_hash IS NULL OR (
    length(decision_hash) = 64
    AND decision_hash NOT GLOB '*[^0-9a-f]*'
  )
);

ALTER TABLE reconciliation_match_results
ADD COLUMN decision_material_json TEXT;

ALTER TABLE reconciliation_match_results
ADD COLUMN authorization_public_id TEXT;

CREATE INDEX idx_reconciliation_match_results_decision_hash
  ON reconciliation_match_results(decision_hash)
  WHERE decision_hash IS NOT NULL;

CREATE INDEX idx_reconciliation_match_results_candidate_set_fingerprint
  ON reconciliation_match_results(candidate_set_fingerprint)
  WHERE candidate_set_fingerprint IS NOT NULL;
