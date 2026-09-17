-- Nomi OpenClaw Finance Agent v2 compatibility receipts.
--
-- Additive, staging-only authority.  No historical S5e row is backfilled and
-- no runtime route is enabled by this migration.

CREATE TABLE ai_model_compatibility_receipts (
    id INTEGER PRIMARY KEY,
    receipt_public_id TEXT NOT NULL UNIQUE CHECK (
        typeof(receipt_public_id) = 'text'
        AND length(receipt_public_id) = 69
        AND receipt_public_id GLOB 'aimr_[0-9a-f]*'
        AND receipt_public_id NOT GLOB 'aimr_*[^0-9a-f]*'
    ),
    receipt_material_hash TEXT NOT NULL UNIQUE CHECK (
        typeof(receipt_material_hash) = 'text'
        AND length(receipt_material_hash) = 64
        AND receipt_material_hash NOT GLOB '*[^0-9a-f]*'
    ),
    schema_version TEXT NOT NULL CHECK (
        typeof(schema_version) = 'text'
        AND schema_version = 'finance-ai-model-compatibility-receipt-v2'
    ),
    config_projection_json TEXT NOT NULL CHECK (
        typeof(config_projection_json) = 'text'
        AND length(config_projection_json) BETWEEN 2 AND 32768
        AND json_valid(config_projection_json) = 1
        AND json_type(config_projection_json) = 'object'
    ),
    config_projection_hash TEXT NOT NULL UNIQUE CHECK (
        typeof(config_projection_hash) = 'text'
        AND length(config_projection_hash) = 64
        AND config_projection_hash NOT GLOB '*[^0-9a-f]*'
    ),
    openclaw_version TEXT NOT NULL CHECK (typeof(openclaw_version) = 'text' AND length(openclaw_version) BETWEEN 1 AND 128),
    openclaw_package_sha256 TEXT NOT NULL CHECK (typeof(openclaw_package_sha256) = 'text' AND length(openclaw_package_sha256) = 64 AND openclaw_package_sha256 NOT GLOB '*[^0-9a-f]*'),
    finance_commit TEXT NOT NULL CHECK (typeof(finance_commit) = 'text' AND length(finance_commit) = 40 AND finance_commit NOT GLOB '*[^0-9a-f]*'),
    plugin_build_sha256 TEXT NOT NULL CHECK (typeof(plugin_build_sha256) = 'text' AND length(plugin_build_sha256) = 64 AND plugin_build_sha256 NOT GLOB '*[^0-9a-f]*'),
    agent_id TEXT NOT NULL CHECK (typeof(agent_id) = 'text' AND agent_id = 'finance'),
    canonical_provider TEXT NOT NULL CHECK (typeof(canonical_provider) = 'text' AND length(canonical_provider) BETWEEN 1 AND 128),
    canonical_model TEXT NOT NULL CHECK (typeof(canonical_model) = 'text' AND length(canonical_model) BETWEEN 1 AND 256),
    display_alias TEXT NOT NULL CHECK (typeof(display_alias) = 'text' AND length(display_alias) BETWEEN 1 AND 64),
    execution_class TEXT NOT NULL CHECK (typeof(execution_class) = 'text' AND execution_class IN ('local_model', 'cloud_projection')),
    fallbacks_json TEXT NOT NULL CHECK (typeof(fallbacks_json) = 'text' AND fallbacks_json = '[]'),
    effective_max_retries INTEGER NOT NULL CHECK (typeof(effective_max_retries) = 'integer' AND effective_max_retries = 0),
    tool_policy_sha256 TEXT NOT NULL CHECK (typeof(tool_policy_sha256) = 'text' AND length(tool_policy_sha256) = 64 AND tool_policy_sha256 NOT GLOB '*[^0-9a-f]*'),
    memory_policy_sha256 TEXT NOT NULL CHECK (typeof(memory_policy_sha256) = 'text' AND length(memory_policy_sha256) = 64 AND memory_policy_sha256 NOT GLOB '*[^0-9a-f]*'),
    plugin_binding_policy_sha256 TEXT NOT NULL CHECK (typeof(plugin_binding_policy_sha256) = 'text' AND length(plugin_binding_policy_sha256) = 64 AND plugin_binding_policy_sha256 NOT GLOB '*[^0-9a-f]*'),
    projection_policy_version TEXT NOT NULL CHECK (typeof(projection_policy_version) = 'text' AND length(projection_policy_version) BETWEEN 1 AND 128),
    projection_policy_sha256 TEXT NOT NULL CHECK (typeof(projection_policy_sha256) = 'text' AND length(projection_policy_sha256) = 64 AND projection_policy_sha256 NOT GLOB '*[^0-9a-f]*'),
    prompt_version TEXT NOT NULL CHECK (typeof(prompt_version) = 'text' AND length(prompt_version) BETWEEN 1 AND 128),
    prompt_sha256 TEXT NOT NULL CHECK (typeof(prompt_sha256) = 'text' AND length(prompt_sha256) = 64 AND prompt_sha256 NOT GLOB '*[^0-9a-f]*'),
    fixture_set_version TEXT NOT NULL CHECK (typeof(fixture_set_version) = 'text' AND length(fixture_set_version) BETWEEN 1 AND 128),
    fixture_set_sha256 TEXT NOT NULL CHECK (typeof(fixture_set_sha256) = 'text' AND length(fixture_set_sha256) = 64 AND fixture_set_sha256 NOT GLOB '*[^0-9a-f]*'),
    fixture_results_json TEXT NOT NULL CHECK (
        typeof(fixture_results_json) = 'text'
        AND length(fixture_results_json) BETWEEN 2 AND 65536
        AND json_valid(fixture_results_json) = 1
        AND json_type(fixture_results_json) = 'array'
    ),
    fixture_results_sha256 TEXT NOT NULL CHECK (typeof(fixture_results_sha256) = 'text' AND length(fixture_results_sha256) = 64 AND fixture_results_sha256 NOT GLOB '*[^0-9a-f]*'),
    verification_method TEXT NOT NULL CHECK (typeof(verification_method) = 'text' AND verification_method = 'python_recomputed_fixed_harness_v1'),
    freshness_rule TEXT NOT NULL CHECK (typeof(freshness_rule) = 'text' AND freshness_rule = 'hash_bound_no_calendar_ttl_v1'),
    operator_workflow_version TEXT NOT NULL CHECK (typeof(operator_workflow_version) = 'text' AND operator_workflow_version = 'finance-model-compatibility-operator-workflow-v1'),
    issued_at_ms INTEGER NOT NULL CHECK (typeof(issued_at_ms) = 'integer' AND issued_at_ms >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (typeof(created_at) = 'text')
);

CREATE TABLE ai_fallback_attempt_compatibility_receipts (
    id INTEGER PRIMARY KEY,
    link_public_id TEXT NOT NULL UNIQUE CHECK (
        typeof(link_public_id) = 'text'
        AND length(link_public_id) = 69
        AND link_public_id GLOB 'aiml_[0-9a-f]*'
        AND link_public_id NOT GLOB 'aiml_*[^0-9a-f]*'
    ),
    link_material_hash TEXT NOT NULL UNIQUE CHECK (
        typeof(link_material_hash) = 'text'
        AND length(link_material_hash) = 64
        AND link_material_hash NOT GLOB '*[^0-9a-f]*'
    ),
    attempt_id INTEGER NOT NULL UNIQUE,
    receipt_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (typeof(created_at) = 'text'),
    FOREIGN KEY (attempt_id) REFERENCES ai_fallback_attempts(id),
    FOREIGN KEY (receipt_id) REFERENCES ai_model_compatibility_receipts(id)
);

CREATE INDEX idx_ai_model_compatibility_receipts_projection
ON ai_model_compatibility_receipts(config_projection_hash);
CREATE INDEX idx_ai_fallback_attempt_receipts_receipt
ON ai_fallback_attempt_compatibility_receipts(receipt_id);

CREATE TRIGGER trg_ai_model_compatibility_receipts_no_insert_collision
BEFORE INSERT ON ai_model_compatibility_receipts
WHEN EXISTS (
    SELECT 1 FROM ai_model_compatibility_receipts AS existing
    WHERE existing.id = NEW.id
       OR existing.receipt_public_id = NEW.receipt_public_id
       OR existing.receipt_material_hash = NEW.receipt_material_hash
       OR existing.config_projection_hash = NEW.config_projection_hash
)
BEGIN SELECT RAISE(ABORT, 'AI model compatibility receipts are immutable and cannot be replaced'); END;

CREATE TRIGGER trg_ai_model_compatibility_receipts_no_update
BEFORE UPDATE ON ai_model_compatibility_receipts
BEGIN SELECT RAISE(ABORT, 'AI model compatibility receipts are append-only'); END;

CREATE TRIGGER trg_ai_model_compatibility_receipts_no_delete
BEFORE DELETE ON ai_model_compatibility_receipts
BEGIN SELECT RAISE(ABORT, 'AI model compatibility receipts are append-only'); END;

CREATE TRIGGER trg_ai_fallback_attempt_receipts_no_insert_collision
BEFORE INSERT ON ai_fallback_attempt_compatibility_receipts
WHEN EXISTS (
    SELECT 1 FROM ai_fallback_attempt_compatibility_receipts AS existing
    WHERE existing.id = NEW.id
       OR existing.link_public_id = NEW.link_public_id
       OR existing.link_material_hash = NEW.link_material_hash
       OR existing.attempt_id = NEW.attempt_id
)
BEGIN SELECT RAISE(ABORT, 'AI fallback receipt links are immutable and cannot be replaced'); END;

CREATE TRIGGER trg_ai_fallback_attempt_receipts_attribution_match
BEFORE INSERT ON ai_fallback_attempt_compatibility_receipts
WHEN NOT EXISTS (
    SELECT 1
    FROM ai_fallback_attempts AS attempt
    JOIN ai_model_compatibility_receipts AS receipt ON receipt.id = NEW.receipt_id
    WHERE attempt.id = NEW.attempt_id
      AND attempt.expected_provider = receipt.canonical_provider
      AND attempt.expected_model = receipt.canonical_model
      AND attempt.expected_agent_id = receipt.agent_id
      AND attempt.expected_audit_caller_kind = 'plugin'
      AND attempt.expected_audit_caller_id = 'finance-bridge'
      AND attempt.expected_audit_caller_name IS NULL
      AND attempt.expected_audit_purpose = 'finance-bridge.ai-proposal-v2'
      AND attempt.expected_audit_session_key_sha256 IS NULL
      AND attempt.runtime_policy_version = receipt.projection_policy_version
      AND attempt.runtime_policy_hash = receipt.config_projection_hash
      AND attempt.prompt_version = receipt.prompt_version
      AND attempt.prompt_template_hash = receipt.prompt_sha256
)
BEGIN SELECT RAISE(ABORT, 'AI fallback receipt attribution does not match the attempt'); END;

CREATE TRIGGER trg_ai_fallback_attempt_receipts_no_update
BEFORE UPDATE ON ai_fallback_attempt_compatibility_receipts
BEGIN SELECT RAISE(ABORT, 'AI fallback receipt links are append-only'); END;

CREATE TRIGGER trg_ai_fallback_attempt_receipts_no_delete
BEFORE DELETE ON ai_fallback_attempt_compatibility_receipts
BEGIN SELECT RAISE(ABORT, 'AI fallback receipt links are append-only'); END;
