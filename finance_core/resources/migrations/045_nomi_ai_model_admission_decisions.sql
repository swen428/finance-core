-- Nomi OpenClaw Finance Agent v2 model-admission decisions.
--
-- A rejected Agent projection creates no model attempt, so this additive,
-- intake-scoped record preserves the terminal denial without retaining config,
-- request, conversation, receipt-image, or credential content.

CREATE TABLE ai_model_admission_decisions (
    id INTEGER PRIMARY KEY,
    decision_public_id TEXT NOT NULL UNIQUE CHECK (
        typeof(decision_public_id) = 'text'
        AND length(decision_public_id) = 69
        AND decision_public_id GLOB 'aimd_[0-9a-f]*'
        AND decision_public_id NOT GLOB 'aimd_*[^0-9a-f]*'
    ),
    decision_material_hash TEXT NOT NULL UNIQUE CHECK (
        typeof(decision_material_hash) = 'text'
        AND length(decision_material_hash) = 64
        AND decision_material_hash NOT GLOB '*[^0-9a-f]*'
    ),
    raw_intake_record_id INTEGER NOT NULL UNIQUE,
    config_evidence_sha256 TEXT NOT NULL CHECK (
        typeof(config_evidence_sha256) = 'text'
        AND length(config_evidence_sha256) = 64
        AND config_evidence_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    decision_type TEXT NOT NULL CHECK (
        typeof(decision_type) = 'text' AND decision_type = 'model_denied'
    ),
    safe_reason_code TEXT NOT NULL CHECK (
        typeof(safe_reason_code) = 'text'
        AND safe_reason_code = 'configuration_not_accepted'
    ),
    decided_at_ms INTEGER NOT NULL CHECK (
        typeof(decided_at_ms) = 'integer' AND decided_at_ms >= 0
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (typeof(created_at) = 'text'),
    FOREIGN KEY (raw_intake_record_id) REFERENCES raw_intake_records(id)
);

CREATE INDEX idx_ai_model_admission_decisions_intake
ON ai_model_admission_decisions(raw_intake_record_id);

CREATE TRIGGER trg_ai_model_admission_decisions_no_insert_collision
BEFORE INSERT ON ai_model_admission_decisions
WHEN EXISTS (
    SELECT 1 FROM ai_model_admission_decisions AS existing
    WHERE existing.id = NEW.id
       OR existing.decision_public_id = NEW.decision_public_id
       OR existing.decision_material_hash = NEW.decision_material_hash
       OR existing.raw_intake_record_id = NEW.raw_intake_record_id
)
BEGIN SELECT RAISE(ABORT, 'AI model admission decisions are immutable and cannot be replaced'); END;

CREATE TRIGGER trg_ai_model_admission_decisions_attempt_exclusion
BEFORE INSERT ON ai_model_admission_decisions
WHEN EXISTS (
    SELECT 1 FROM ai_fallback_attempts AS attempt
    WHERE attempt.raw_intake_record_id = NEW.raw_intake_record_id
)
BEGIN SELECT RAISE(ABORT, 'AI model admission denial cannot replace an AI fallback attempt'); END;

CREATE TRIGGER trg_ai_fallback_attempts_admission_exclusion
BEFORE INSERT ON ai_fallback_attempts
WHEN EXISTS (
    SELECT 1 FROM ai_model_admission_decisions AS decision
    WHERE decision.raw_intake_record_id = NEW.raw_intake_record_id
)
BEGIN SELECT RAISE(ABORT, 'AI fallback attempt cannot replace an AI model admission denial'); END;

CREATE TRIGGER trg_ai_model_admission_decisions_no_update
BEFORE UPDATE ON ai_model_admission_decisions
BEGIN SELECT RAISE(ABORT, 'AI model admission decisions are append-only'); END;

CREATE TRIGGER trg_ai_model_admission_decisions_no_delete
BEFORE DELETE ON ai_model_admission_decisions
BEGIN SELECT RAISE(ABORT, 'AI model admission decisions are append-only'); END;
