-- S5e-A AI fallback provenance foundation.
--
-- This migration is additive and staging-only.  It records no model call,
-- exposes no bridge command, and selects no provider, model, prompt, or agent.
-- The later S5e-B service is the only scope allowed to write these tables.

CREATE TABLE ai_fallback_attempts (
    id INTEGER PRIMARY KEY,
    attempt_public_id TEXT NOT NULL UNIQUE CHECK (
        typeof(attempt_public_id) = 'text'
        AND length(attempt_public_id) = 69
        AND attempt_public_id GLOB 'aifa_[0-9a-f]*'
        AND attempt_public_id NOT GLOB 'aifa_*[^0-9a-f]*'
    ),
    preparation_material_hash TEXT NOT NULL UNIQUE CHECK (
        typeof(preparation_material_hash) = 'text'
        AND length(preparation_material_hash) = 64
        AND preparation_material_hash NOT GLOB '*[^0-9a-f]*'
    ),
    raw_intake_record_id INTEGER NOT NULL UNIQUE,
    parent_parser_output_id INTEGER NOT NULL UNIQUE,
    parent_proposal_version INTEGER NOT NULL CHECK (typeof(parent_proposal_version) = 'integer' AND parent_proposal_version >= 0),
    parent_effective_content_hash TEXT NOT NULL CHECK (
        typeof(parent_effective_content_hash) = 'text'
        AND length(parent_effective_content_hash) = 64
        AND parent_effective_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    source_kind TEXT NOT NULL CHECK (typeof(source_kind) = 'text' AND source_kind IN ('telegram_raw_text', 'receipt_local_ocr_text')),
    source_projection_hash TEXT NOT NULL CHECK (
        typeof(source_projection_hash) = 'text'
        AND length(source_projection_hash) = 64
        AND source_projection_hash NOT GLOB '*[^0-9a-f]*'
    ),
    source_projection_byte_count INTEGER NOT NULL CHECK (
        typeof(source_projection_byte_count) = 'integer'
        AND source_projection_byte_count BETWEEN 0 AND 24576
    ),
    source_selection_manifest_hash TEXT NOT NULL CHECK (
        typeof(source_selection_manifest_hash) = 'text'
        AND length(source_selection_manifest_hash) = 64
        AND source_selection_manifest_hash NOT GLOB '*[^0-9a-f]*'
    ),
    source_field_state_hash TEXT NOT NULL CHECK (
        typeof(source_field_state_hash) = 'text'
        AND length(source_field_state_hash) = 64
        AND source_field_state_hash NOT GLOB '*[^0-9a-f]*'
    ),
    fallback_mode TEXT NOT NULL CHECK (typeof(fallback_mode) = 'text' AND fallback_mode IN ('child_eligible', 'classification_only')),
    eligibility_reasons_json TEXT NOT NULL CHECK (
        typeof(eligibility_reasons_json) = 'text'
        AND length(eligibility_reasons_json) BETWEEN 2 AND 8192
        AND json_valid(eligibility_reasons_json) = 1
        AND json_type(eligibility_reasons_json) = 'array'
    ),
    runtime_policy_version TEXT NOT NULL CHECK (typeof(runtime_policy_version) = 'text'),
    runtime_policy_hash TEXT NOT NULL CHECK (typeof(runtime_policy_hash) = 'text' AND length(runtime_policy_hash) = 64 AND runtime_policy_hash NOT GLOB '*[^0-9a-f]*'),
    prompt_version TEXT NOT NULL CHECK (typeof(prompt_version) = 'text'),
    prompt_template_hash TEXT NOT NULL CHECK (typeof(prompt_template_hash) = 'text' AND length(prompt_template_hash) = 64 AND prompt_template_hash NOT GLOB '*[^0-9a-f]*'),
    intent_policy_version TEXT NOT NULL CHECK (typeof(intent_policy_version) = 'text'),
    intent_policy_hash TEXT NOT NULL CHECK (typeof(intent_policy_hash) = 'text' AND length(intent_policy_hash) = 64 AND intent_policy_hash NOT GLOB '*[^0-9a-f]*'),
    intent_policy_result TEXT NOT NULL CHECK (typeof(intent_policy_result) = 'text'),
    intent_evidence_hash TEXT NOT NULL CHECK (typeof(intent_evidence_hash) = 'text' AND length(intent_evidence_hash) = 64 AND intent_evidence_hash NOT GLOB '*[^0-9a-f]*'),
    default_policy_version TEXT NOT NULL CHECK (typeof(default_policy_version) = 'text'),
    default_policy_hash TEXT NOT NULL CHECK (typeof(default_policy_hash) = 'text' AND length(default_policy_hash) = 64 AND default_policy_hash NOT GLOB '*[^0-9a-f]*'),
    default_evidence_hash TEXT CHECK (default_evidence_hash IS NULL OR (typeof(default_evidence_hash) = 'text' AND length(default_evidence_hash) = 64 AND default_evidence_hash NOT GLOB '*[^0-9a-f]*')),
    sensitive_text_policy_version TEXT NOT NULL CHECK (typeof(sensitive_text_policy_version) = 'text'),
    sensitive_text_policy_hash TEXT NOT NULL CHECK (typeof(sensitive_text_policy_hash) = 'text' AND length(sensitive_text_policy_hash) = 64 AND sensitive_text_policy_hash NOT GLOB '*[^0-9a-f]*'),
    sensitive_text_scan_hash TEXT NOT NULL CHECK (typeof(sensitive_text_scan_hash) = 'text' AND length(sensitive_text_scan_hash) = 64 AND sensitive_text_scan_hash NOT GLOB '*[^0-9a-f]*'),
    deadline_policy_version TEXT NOT NULL CHECK (typeof(deadline_policy_version) = 'text'),
    deadline_policy_hash TEXT NOT NULL CHECK (typeof(deadline_policy_hash) = 'text' AND length(deadline_policy_hash) = 64 AND deadline_policy_hash NOT GLOB '*[^0-9a-f]*'),
    sqlite_money_policy_version TEXT NOT NULL CHECK (typeof(sqlite_money_policy_version) = 'text'),
    sqlite_money_policy_hash TEXT NOT NULL CHECK (typeof(sqlite_money_policy_hash) = 'text' AND length(sqlite_money_policy_hash) = 64 AND sqlite_money_policy_hash NOT GLOB '*[^0-9a-f]*'),
    expected_provider TEXT NOT NULL CHECK (typeof(expected_provider) = 'text'),
    expected_model TEXT NOT NULL CHECK (typeof(expected_model) = 'text'),
    expected_agent_id TEXT NOT NULL CHECK (typeof(expected_agent_id) = 'text'),
    expected_audit_caller_kind TEXT NOT NULL CHECK (typeof(expected_audit_caller_kind) = 'text'),
    expected_audit_caller_id TEXT NOT NULL CHECK (typeof(expected_audit_caller_id) = 'text'),
    expected_audit_caller_name TEXT CHECK (expected_audit_caller_name IS NULL OR typeof(expected_audit_caller_name) = 'text'),
    expected_audit_purpose TEXT NOT NULL CHECK (typeof(expected_audit_purpose) = 'text'),
    expected_audit_session_key_sha256 TEXT CHECK (expected_audit_session_key_sha256 IS NULL OR (typeof(expected_audit_session_key_sha256) = 'text' AND length(expected_audit_session_key_sha256) = 64 AND expected_audit_session_key_sha256 NOT GLOB '*[^0-9a-f]*')),
    request_blob BLOB NOT NULL CHECK (typeof(request_blob) = 'blob' AND length(request_blob) <= 65536),
    request_sha256 TEXT NOT NULL CHECK (typeof(request_sha256) = 'text' AND length(request_sha256) = 64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    request_byte_count INTEGER NOT NULL CHECK (typeof(request_byte_count) = 'integer' AND request_byte_count BETWEEN 0 AND 65536 AND length(request_blob) = request_byte_count),
    prepared_at_ms INTEGER NOT NULL CHECK (typeof(prepared_at_ms) = 'integer' AND prepared_at_ms >= 0),
    invoke_not_after_ms INTEGER NOT NULL CHECK (typeof(invoke_not_after_ms) = 'integer' AND invoke_not_after_ms > prepared_at_ms),
    result_not_after_ms INTEGER NOT NULL CHECK (typeof(result_not_after_ms) = 'integer' AND result_not_after_ms > invoke_not_after_ms),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (typeof(created_at) = 'text'),
    FOREIGN KEY (raw_intake_record_id) REFERENCES raw_intake_records(id),
    FOREIGN KEY (parent_parser_output_id) REFERENCES parser_outputs(id)
);

CREATE TABLE ai_fallback_invocation_claims (
    id INTEGER PRIMARY KEY,
    claim_public_id TEXT NOT NULL UNIQUE CHECK (typeof(claim_public_id) = 'text' AND length(claim_public_id) = 69 AND claim_public_id GLOB 'aicl_[0-9a-f]*' AND claim_public_id NOT GLOB 'aicl_*[^0-9a-f]*'),
    claim_material_hash TEXT NOT NULL UNIQUE CHECK (typeof(claim_material_hash) = 'text' AND length(claim_material_hash) = 64 AND claim_material_hash NOT GLOB '*[^0-9a-f]*'),
    attempt_id INTEGER NOT NULL UNIQUE,
    invocation_claimed_at_ms INTEGER NOT NULL CHECK (typeof(invocation_claimed_at_ms) = 'integer' AND invocation_claimed_at_ms >= 0),
    call_start_not_after_ms INTEGER NOT NULL CHECK (typeof(call_start_not_after_ms) = 'integer' AND call_start_not_after_ms > invocation_claimed_at_ms),
    invocation_disposition TEXT NOT NULL CHECK (typeof(invocation_disposition) = 'text' AND invocation_disposition = 'invoke_once'),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (typeof(created_at) = 'text'),
    FOREIGN KEY (attempt_id) REFERENCES ai_fallback_attempts(id)
);

CREATE TABLE ai_fallback_results (
    id INTEGER PRIMARY KEY,
    result_public_id TEXT NOT NULL UNIQUE CHECK (typeof(result_public_id) = 'text' AND length(result_public_id) = 68 AND result_public_id GLOB 'air_[0-9a-f]*' AND result_public_id NOT GLOB 'air_*[^0-9a-f]*'),
    result_material_hash TEXT NOT NULL UNIQUE CHECK (typeof(result_material_hash) = 'text' AND length(result_material_hash) = 64 AND result_material_hash NOT GLOB '*[^0-9a-f]*'),
    attempt_id INTEGER NOT NULL UNIQUE,
    claim_id INTEGER NOT NULL UNIQUE,
    transport_outcome TEXT NOT NULL CHECK (typeof(transport_outcome) = 'text' AND transport_outcome IN ('response_received', 'response_oversize', 'response_unencodable', 'response_resource_refused', 'response_metadata_refused', 'provider_error', 'local_preinvocation_refused', 'timeout', 'cancelled')),
    result_status TEXT NOT NULL CHECK (typeof(result_status) = 'text' AND result_status IN ('proposal_created', 'classification_only', 'response_refused', 'attribution_refused', 'provider_error', 'preinvocation_refused', 'timeout', 'cancelled', 'stale_parent', 'late_result', 'response_oversize', 'response_unencodable', 'response_resource_refused')),
    retention_state TEXT NOT NULL CHECK (typeof(retention_state) = 'text' AND retention_state IN ('blob_retained', 'unretained_oversize', 'unretained_unencodable', 'unretained_resource_refused', 'none')),
    normal_attribution_hash TEXT CHECK (normal_attribution_hash IS NULL OR (typeof(normal_attribution_hash) = 'text' AND length(normal_attribution_hash) = 64 AND normal_attribution_hash NOT GLOB '*[^0-9a-f]*')),
    metadata_refusal_hash TEXT CHECK (metadata_refusal_hash IS NULL OR (typeof(metadata_refusal_hash) = 'text' AND length(metadata_refusal_hash) = 64 AND metadata_refusal_hash NOT GLOB '*[^0-9a-f]*')),
    usage_hash TEXT CHECK (usage_hash IS NULL OR (typeof(usage_hash) = 'text' AND length(usage_hash) = 64 AND usage_hash NOT GLOB '*[^0-9a-f]*')),
    result_received_at_ms INTEGER NOT NULL CHECK (typeof(result_received_at_ms) = 'integer' AND result_received_at_ms >= 0),
    post_lock_at_ms INTEGER NOT NULL CHECK (typeof(post_lock_at_ms) = 'integer' AND post_lock_at_ms >= result_received_at_ms),
    decision_at_ms INTEGER NOT NULL CHECK (typeof(decision_at_ms) = 'integer' AND decision_at_ms >= post_lock_at_ms),
    deadline_policy_version TEXT NOT NULL CHECK (typeof(deadline_policy_version) = 'text'),
    deadline_policy_hash TEXT NOT NULL CHECK (typeof(deadline_policy_hash) = 'text' AND length(deadline_policy_hash) = 64 AND deadline_policy_hash NOT GLOB '*[^0-9a-f]*'),
    deadline_disposition TEXT NOT NULL CHECK (typeof(deadline_disposition) = 'text'),
    response_body_state TEXT NOT NULL CHECK (typeof(response_body_state) = 'text' AND response_body_state IN ('retained', 'oversize', 'unencodable', 'resource_refused', 'none')),
    response_blob BLOB CHECK (response_blob IS NULL OR (typeof(response_blob) = 'blob' AND length(response_blob) <= 65536)),
    response_sha256 TEXT CHECK (response_sha256 IS NULL OR (typeof(response_sha256) = 'text' AND length(response_sha256) = 64 AND response_sha256 NOT GLOB '*[^0-9a-f]*')),
    response_byte_count INTEGER CHECK (response_byte_count IS NULL OR (typeof(response_byte_count) = 'integer' AND response_byte_count >= 0)),
    response_code_unit_count INTEGER CHECK (response_code_unit_count IS NULL OR (typeof(response_code_unit_count) = 'integer' AND response_code_unit_count >= 0)),
    response_utf16_sha256 TEXT CHECK (response_utf16_sha256 IS NULL OR (typeof(response_utf16_sha256) = 'text' AND length(response_utf16_sha256) = 64 AND response_utf16_sha256 NOT GLOB '*[^0-9a-f]*')),
    failure_code TEXT CHECK (failure_code IS NULL OR typeof(failure_code) = 'text'),
    non_child_reason TEXT CHECK (non_child_reason IS NULL OR (typeof(non_child_reason) = 'text' AND non_child_reason IN ('missing_merchant', 'forbidden_field', 'source_unresolved', 'intent_unproven', 'validation_refused'))),
    recovery_disposition TEXT CHECK (recovery_disposition IS NULL OR (typeof(recovery_disposition) = 'text' AND recovery_disposition IN ('resend_new_intake_after_provider_failure', 'resend_new_intake_after_not_invoked', 'operator_runtime_review', 'resend_new_intake_after_timeout', 'resend_new_intake_after_cancellation', 'review_current_parent_state', 'resend_new_intake_after_late_result', 'use_manual_intake'))),
    normalized_payload_hash TEXT CHECK (normalized_payload_hash IS NULL OR (typeof(normalized_payload_hash) = 'text' AND length(normalized_payload_hash) = 64 AND normalized_payload_hash NOT GLOB '*[^0-9a-f]*')),
    source_field_state_hash TEXT NOT NULL CHECK (typeof(source_field_state_hash) = 'text' AND length(source_field_state_hash) = 64 AND source_field_state_hash NOT GLOB '*[^0-9a-f]*'),
    ambiguity_hash TEXT CHECK (ambiguity_hash IS NULL OR (typeof(ambiguity_hash) = 'text' AND length(ambiguity_hash) = 64 AND ambiguity_hash NOT GLOB '*[^0-9a-f]*')),
    evidence_set_hash TEXT CHECK (evidence_set_hash IS NULL OR (typeof(evidence_set_hash) = 'text' AND length(evidence_set_hash) = 64 AND evidence_set_hash NOT GLOB '*[^0-9a-f]*')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (typeof(created_at) = 'text'),
    FOREIGN KEY (attempt_id) REFERENCES ai_fallback_attempts(id),
    FOREIGN KEY (claim_id) REFERENCES ai_fallback_invocation_claims(id),
    CHECK (
        (response_body_state = 'retained' AND retention_state = 'blob_retained' AND typeof(response_blob) = 'blob' AND response_sha256 IS NOT NULL AND response_byte_count = length(response_blob) AND response_code_unit_count IS NULL AND response_utf16_sha256 IS NULL)
        OR (response_body_state = 'oversize' AND retention_state = 'unretained_oversize' AND response_blob IS NULL AND response_sha256 IS NOT NULL AND response_byte_count >= 65537 AND response_code_unit_count BETWEEN 0 AND 131072 AND response_utf16_sha256 IS NULL)
        OR (response_body_state = 'unencodable' AND retention_state = 'unretained_unencodable' AND response_blob IS NULL AND response_sha256 IS NULL AND response_byte_count IS NULL AND response_code_unit_count >= 0 AND response_utf16_sha256 IS NOT NULL)
        OR (response_body_state = 'resource_refused' AND retention_state = 'unretained_resource_refused' AND response_blob IS NULL AND response_sha256 IS NULL AND response_byte_count IS NULL AND response_code_unit_count >= 131073 AND response_utf16_sha256 IS NULL)
        OR (response_body_state = 'none' AND retention_state = 'none' AND response_blob IS NULL AND response_sha256 IS NULL AND response_byte_count IS NULL AND response_code_unit_count IS NULL AND response_utf16_sha256 IS NULL)
    ),
    CHECK (
        (transport_outcome = 'response_received' AND response_body_state = 'retained')
        OR (transport_outcome = 'response_oversize' AND response_body_state = 'oversize')
        OR (transport_outcome = 'response_unencodable' AND response_body_state = 'unencodable')
        OR (transport_outcome = 'response_resource_refused' AND response_body_state = 'resource_refused')
        OR (transport_outcome = 'response_metadata_refused' AND result_status IN ('attribution_refused', 'stale_parent', 'late_result'))
        OR (transport_outcome IN ('provider_error', 'local_preinvocation_refused', 'timeout', 'cancelled') AND response_body_state = 'none')
    ),
    CHECK (transport_outcome <> 'provider_error' OR result_status IN ('provider_error', 'late_result')),
    CHECK (result_status <> 'provider_error' OR transport_outcome = 'provider_error'),
    CHECK (transport_outcome <> 'local_preinvocation_refused' OR result_status IN ('preinvocation_refused', 'late_result')),
    CHECK (result_status <> 'preinvocation_refused' OR transport_outcome = 'local_preinvocation_refused'),
    CHECK (transport_outcome <> 'timeout' OR result_status IN ('timeout', 'late_result')),
    CHECK (result_status <> 'timeout' OR transport_outcome = 'timeout'),
    CHECK (transport_outcome <> 'cancelled' OR result_status IN ('cancelled', 'late_result')),
    CHECK (result_status <> 'cancelled' OR transport_outcome = 'cancelled'),
    CHECK (transport_outcome <> 'response_oversize' OR result_status IN ('response_oversize', 'stale_parent', 'attribution_refused', 'late_result')),
    CHECK (result_status <> 'response_oversize' OR transport_outcome IN ('response_received', 'response_oversize')),
    CHECK (result_status <> 'proposal_created' OR (transport_outcome = 'response_received' AND response_byte_count BETWEEN 0 AND 16384)),
    CHECK (transport_outcome <> 'response_received' OR response_byte_count <= 16384 OR result_status IN ('response_oversize', 'stale_parent', 'attribution_refused', 'late_result')),
    CHECK (NOT (transport_outcome = 'response_received' AND result_status = 'response_oversize') OR response_byte_count BETWEEN 16385 AND 65536),
    CHECK (transport_outcome <> 'response_unencodable' OR result_status IN ('response_unencodable', 'stale_parent', 'attribution_refused', 'late_result')),
    CHECK (result_status <> 'response_unencodable' OR transport_outcome = 'response_unencodable'),
    CHECK (transport_outcome <> 'response_resource_refused' OR result_status IN ('response_resource_refused', 'stale_parent', 'attribution_refused', 'late_result')),
    CHECK (result_status <> 'response_resource_refused' OR transport_outcome = 'response_resource_refused'),
    CHECK (
        (transport_outcome IN ('response_received', 'response_oversize', 'response_unencodable', 'response_resource_refused') AND normal_attribution_hash IS NOT NULL AND usage_hash IS NOT NULL AND metadata_refusal_hash IS NULL)
        OR (transport_outcome = 'response_metadata_refused' AND normal_attribution_hash IS NULL AND usage_hash IS NULL AND metadata_refusal_hash IS NOT NULL)
        OR (transport_outcome IN ('provider_error', 'local_preinvocation_refused', 'timeout', 'cancelled') AND normal_attribution_hash IS NULL AND usage_hash IS NULL AND metadata_refusal_hash IS NULL)
    ),
    CHECK (
        (transport_outcome = 'provider_error' AND failure_code = 'host_llm_failed')
        OR (transport_outcome = 'local_preinvocation_refused' AND failure_code IN ('request_integrity_refused', 'runtime_policy_refused', 'call_start_deadline_exceeded'))
        OR (transport_outcome = 'timeout' AND failure_code = 'deadline_exceeded')
        OR (transport_outcome = 'cancelled' AND failure_code = 'cancelled')
        OR (transport_outcome IN ('response_received', 'response_oversize', 'response_unencodable', 'response_resource_refused', 'response_metadata_refused') AND failure_code IS NULL)
    ),
    CHECK (
        (result_status = 'proposal_created' AND non_child_reason IS NULL AND recovery_disposition IS NULL)
        OR (result_status = 'classification_only' AND non_child_reason = 'intent_unproven' AND recovery_disposition IS NULL)
        OR (result_status = 'response_refused' AND non_child_reason IN ('missing_merchant', 'forbidden_field', 'source_unresolved', 'intent_unproven', 'validation_refused') AND recovery_disposition IS NULL)
        OR (result_status = 'provider_error' AND non_child_reason IS NULL AND recovery_disposition = 'resend_new_intake_after_provider_failure')
        OR (result_status = 'preinvocation_refused' AND non_child_reason IS NULL AND ((failure_code = 'call_start_deadline_exceeded' AND recovery_disposition = 'resend_new_intake_after_not_invoked') OR (failure_code IN ('request_integrity_refused', 'runtime_policy_refused') AND recovery_disposition = 'operator_runtime_review')))
        OR (result_status = 'timeout' AND non_child_reason IS NULL AND recovery_disposition = 'resend_new_intake_after_timeout')
        OR (result_status = 'cancelled' AND non_child_reason IS NULL AND recovery_disposition = 'resend_new_intake_after_cancellation')
        OR (result_status = 'stale_parent' AND non_child_reason IS NULL AND recovery_disposition = 'review_current_parent_state')
        OR (result_status = 'late_result' AND non_child_reason IS NULL AND recovery_disposition = 'resend_new_intake_after_late_result')
        OR (result_status = 'attribution_refused' AND non_child_reason IS NULL AND recovery_disposition = 'operator_runtime_review')
        OR (result_status IN ('response_oversize', 'response_unencodable', 'response_resource_refused') AND non_child_reason IS NULL AND recovery_disposition = 'use_manual_intake')
    )
);

CREATE TABLE ai_fallback_proposal_links (
    id INTEGER PRIMARY KEY,
    link_public_id TEXT NOT NULL UNIQUE CHECK (typeof(link_public_id) = 'text' AND length(link_public_id) = 69 AND link_public_id GLOB 'aipl_[0-9a-f]*' AND link_public_id NOT GLOB 'aipl_*[^0-9a-f]*'),
    link_material_hash TEXT NOT NULL UNIQUE CHECK (typeof(link_material_hash) = 'text' AND length(link_material_hash) = 64 AND link_material_hash NOT GLOB '*[^0-9a-f]*'),
    result_id INTEGER NOT NULL UNIQUE,
    parser_output_id INTEGER NOT NULL UNIQUE,
    effective_content_hash TEXT NOT NULL CHECK (typeof(effective_content_hash) = 'text' AND length(effective_content_hash) = 64 AND effective_content_hash NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK (typeof(created_at) = 'text'),
    FOREIGN KEY (result_id) REFERENCES ai_fallback_results(id),
    FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id)
);

CREATE INDEX idx_ai_fallback_attempts_parent ON ai_fallback_attempts(parent_parser_output_id);
CREATE INDEX idx_ai_fallback_results_status ON ai_fallback_results(result_status, created_at);

-- Guard all declared identity routes before SQLite's conflict resolution.  This
-- is intentionally independent of foreign_keys and recursive_triggers.
CREATE TRIGGER trg_ai_fallback_attempts_no_insert_collision
BEFORE INSERT ON ai_fallback_attempts
WHEN EXISTS (SELECT 1 FROM ai_fallback_attempts AS existing WHERE existing.id = NEW.id OR existing.attempt_public_id = NEW.attempt_public_id OR existing.preparation_material_hash = NEW.preparation_material_hash OR existing.raw_intake_record_id = NEW.raw_intake_record_id OR existing.parent_parser_output_id = NEW.parent_parser_output_id)
BEGIN SELECT RAISE(ABORT, 'AI fallback attempts are immutable and cannot be replaced'); END;

CREATE TRIGGER trg_ai_fallback_attempts_intake_parent_match
BEFORE INSERT ON ai_fallback_attempts
WHEN NOT EXISTS (
    SELECT 1
    FROM raw_intake_records AS intake
    JOIN parser_outputs AS parent ON parent.id = NEW.parent_parser_output_id
    WHERE intake.id = NEW.raw_intake_record_id
      AND intake.parser_output_id = parent.id
)
BEGIN SELECT RAISE(ABORT, 'AI fallback attempt intake does not bind its parent proposal'); END;

CREATE TRIGGER trg_ai_fallback_attempts_eligibility_reasons_valid
BEFORE INSERT ON ai_fallback_attempts
WHEN CASE
    WHEN json_valid(NEW.eligibility_reasons_json) <> 1 THEN 1
    WHEN json_type(NEW.eligibility_reasons_json) <> 'array' THEN 1
    WHEN json_array_length(NEW.eligibility_reasons_json) = 0 THEN 1
    WHEN EXISTS (
        SELECT 1
        FROM json_each(NEW.eligibility_reasons_json) AS reason
        WHERE reason.type <> 'text'
           OR reason.value NOT IN (
               'unsupported_language',
               'mixed_language_incomplete',
               'deterministic_fields_incomplete',
               'conflicting_text_candidates',
               'receipt_ocr_fields_incomplete',
               'intent_classification_required'
           )
    ) THEN 1
    WHEN EXISTS (
        SELECT 1
        FROM json_each(NEW.eligibility_reasons_json) AS reason
        WHERE CAST(reason.key AS INTEGER) > 0
          AND reason.value <= json_extract(
              NEW.eligibility_reasons_json,
              '$[' || (CAST(reason.key AS INTEGER) - 1) || ']'
          )
    ) THEN 1
    WHEN NEW.fallback_mode = 'classification_only'
         AND NOT EXISTS (
             SELECT 1
             FROM json_each(NEW.eligibility_reasons_json)
             WHERE value = 'intent_classification_required'
         ) THEN 1
    WHEN NEW.fallback_mode = 'child_eligible'
         AND EXISTS (
             SELECT 1
             FROM json_each(NEW.eligibility_reasons_json)
             WHERE value = 'intent_classification_required'
         ) THEN 1
    ELSE 0
END
BEGIN SELECT RAISE(ABORT, 'AI fallback eligibility reasons must be a canonical closed taxonomy'); END;

CREATE TRIGGER trg_ai_fallback_claims_no_insert_collision
BEFORE INSERT ON ai_fallback_invocation_claims
WHEN EXISTS (SELECT 1 FROM ai_fallback_invocation_claims AS existing WHERE existing.id = NEW.id OR existing.claim_public_id = NEW.claim_public_id OR existing.claim_material_hash = NEW.claim_material_hash OR existing.attempt_id = NEW.attempt_id)
BEGIN SELECT RAISE(ABORT, 'AI invocation claims are immutable and cannot be replaced'); END;

CREATE TRIGGER trg_ai_fallback_claims_require_attempt
BEFORE INSERT ON ai_fallback_invocation_claims
WHEN NOT EXISTS (SELECT 1 FROM ai_fallback_attempts WHERE id = NEW.attempt_id)
BEGIN SELECT RAISE(ABORT, 'AI invocation claim requires its attempt'); END;

CREATE TRIGGER trg_ai_fallback_results_no_insert_collision
BEFORE INSERT ON ai_fallback_results
WHEN EXISTS (SELECT 1 FROM ai_fallback_results AS existing WHERE existing.id = NEW.id OR existing.result_public_id = NEW.result_public_id OR existing.result_material_hash = NEW.result_material_hash OR existing.attempt_id = NEW.attempt_id OR existing.claim_id = NEW.claim_id)
BEGIN SELECT RAISE(ABORT, 'AI fallback results are immutable and cannot be replaced'); END;

CREATE TRIGGER trg_ai_fallback_links_no_insert_collision
BEFORE INSERT ON ai_fallback_proposal_links
WHEN EXISTS (SELECT 1 FROM ai_fallback_proposal_links AS existing WHERE existing.id = NEW.id OR existing.link_public_id = NEW.link_public_id OR existing.link_material_hash = NEW.link_material_hash OR existing.result_id = NEW.result_id OR existing.parser_output_id = NEW.parser_output_id)
BEGIN SELECT RAISE(ABORT, 'AI proposal links are immutable and cannot be replaced'); END;

-- Enforce cross-table lineage even if foreign_keys is disabled.  The result
-- and link cannot be made durable unless their immutable parents agree.
CREATE TRIGGER trg_ai_fallback_results_claim_matches_attempt
BEFORE INSERT ON ai_fallback_results
WHEN NOT EXISTS (
    SELECT 1
    FROM ai_fallback_invocation_claims AS claim
    JOIN ai_fallback_attempts AS attempt ON attempt.id = claim.attempt_id
    WHERE claim.id = NEW.claim_id
      AND attempt.id = NEW.attempt_id
)
BEGIN SELECT RAISE(ABORT, 'AI fallback result claim does not belong to its attempt'); END;

CREATE TRIGGER trg_ai_fallback_links_require_created_child_lineage
BEFORE INSERT ON ai_fallback_proposal_links
WHEN NOT EXISTS (
    SELECT 1
    FROM ai_fallback_results AS result
    JOIN ai_fallback_attempts AS attempt ON attempt.id = result.attempt_id
    JOIN parser_outputs AS child ON child.id = NEW.parser_output_id
    WHERE result.id = NEW.result_id
      AND result.result_status = 'proposal_created'
      AND result.response_byte_count BETWEEN 0 AND 16384
      AND child.parent_parser_output_id = attempt.parent_parser_output_id
)
BEGIN SELECT RAISE(ABORT, 'AI fallback link requires its attempt child and a proposal-created result'); END;

CREATE TRIGGER trg_ai_fallback_attempts_no_update BEFORE UPDATE ON ai_fallback_attempts BEGIN SELECT RAISE(ABORT, 'AI fallback attempts are append-only'); END;
CREATE TRIGGER trg_ai_fallback_attempts_no_delete BEFORE DELETE ON ai_fallback_attempts BEGIN SELECT RAISE(ABORT, 'AI fallback attempts are append-only'); END;
CREATE TRIGGER trg_ai_fallback_claims_no_update BEFORE UPDATE ON ai_fallback_invocation_claims BEGIN SELECT RAISE(ABORT, 'AI invocation claims are append-only'); END;
CREATE TRIGGER trg_ai_fallback_claims_no_delete BEFORE DELETE ON ai_fallback_invocation_claims BEGIN SELECT RAISE(ABORT, 'AI invocation claims are append-only'); END;
CREATE TRIGGER trg_ai_fallback_results_no_update BEFORE UPDATE ON ai_fallback_results BEGIN SELECT RAISE(ABORT, 'AI fallback results are append-only'); END;
CREATE TRIGGER trg_ai_fallback_results_no_delete BEFORE DELETE ON ai_fallback_results BEGIN SELECT RAISE(ABORT, 'AI fallback results are append-only'); END;
CREATE TRIGGER trg_ai_fallback_links_no_update BEFORE UPDATE ON ai_fallback_proposal_links BEGIN SELECT RAISE(ABORT, 'AI proposal links are append-only'); END;
CREATE TRIGGER trg_ai_fallback_links_no_delete BEFORE DELETE ON ai_fallback_proposal_links BEGIN SELECT RAISE(ABORT, 'AI proposal links are append-only'); END;

-- SQLite does not execute DELETE triggers for REPLACE's implicit delete when
-- recursive_triggers is OFF.  These insert-time guards close that route for
-- a sealed parent, raw intake, or linked child without freezing historical
-- proposals that never received an AI attempt/link.
CREATE TRIGGER trg_ai_fallback_parser_outputs_no_insert_collision
BEFORE INSERT ON parser_outputs
WHEN EXISTS (
    SELECT 1
    FROM parser_outputs AS existing
    WHERE (existing.id = NEW.id OR existing.public_id = NEW.public_id)
      AND (
          EXISTS (
              SELECT 1 FROM ai_fallback_attempts AS attempt
              WHERE attempt.parent_parser_output_id = existing.id
          )
          OR EXISTS (
              SELECT 1 FROM ai_fallback_proposal_links AS link
              WHERE link.parser_output_id = existing.id
          )
      )
)
BEGIN SELECT RAISE(ABORT, 'AI fallback sealed parser output cannot be replaced'); END;

-- UPDATE OR REPLACE can implicitly delete another unique target while its
-- DELETE trigger is bypassed with recursive_triggers OFF.  Inspect the target
-- row, not only OLD, before conflict resolution so an unsealed row cannot
-- steal a sealed parent or linked child's primary/public identity.
CREATE TRIGGER trg_ai_fallback_parser_outputs_no_update_collision
BEFORE UPDATE ON parser_outputs
WHEN EXISTS (
    SELECT 1
    FROM parser_outputs AS existing
    WHERE existing.id <> OLD.id
      AND (existing.id = NEW.id OR existing.public_id = NEW.public_id)
      AND (
          EXISTS (
              SELECT 1 FROM ai_fallback_attempts AS attempt
              WHERE attempt.parent_parser_output_id = existing.id
          )
          OR EXISTS (
              SELECT 1 FROM ai_fallback_proposal_links AS link
              WHERE link.parser_output_id = existing.id
          )
      )
)
BEGIN SELECT RAISE(ABORT, 'AI fallback sealed parser output cannot be replaced'); END;

CREATE TRIGGER trg_ai_fallback_raw_intake_no_insert_collision
BEFORE INSERT ON raw_intake_records
WHEN EXISTS (
    SELECT 1
    FROM raw_intake_records AS existing
    WHERE (existing.id = NEW.id OR existing.public_id = NEW.public_id)
      AND EXISTS (
          SELECT 1 FROM ai_fallback_attempts AS attempt
          WHERE attempt.raw_intake_record_id = existing.id
      )
)
BEGIN SELECT RAISE(ABORT, 'AI fallback sealed raw intake cannot be replaced'); END;

-- An attempt seals only source/hash-affecting parent columns.  Existing human
-- lifecycle state transitions remain available; S5e-B will re-read them and
-- refuse a stale result rather than blocking a human decision.
CREATE TRIGGER trg_ai_fallback_parent_no_hash_update
BEFORE UPDATE ON parser_outputs
WHEN (
    EXISTS (SELECT 1 FROM ai_fallback_attempts WHERE parent_parser_output_id = OLD.id)
    OR EXISTS (SELECT 1 FROM ai_fallback_proposal_links WHERE parser_output_id = OLD.id)
)
 AND (NEW.id IS NOT OLD.id OR NEW.public_id IS NOT OLD.public_id OR NEW.source_type IS NOT OLD.source_type OR NEW.source_public_id IS NOT OLD.source_public_id OR NEW.attachment_id IS NOT OLD.attachment_id OR NEW.parser_name IS NOT OLD.parser_name OR NEW.parser_version IS NOT OLD.parser_version OR NEW.ai_provider IS NOT OLD.ai_provider OR NEW.ai_model IS NOT OLD.ai_model OR NEW.prompt_version IS NOT OLD.prompt_version OR NEW.raw_text IS NOT OLD.raw_text OR NEW.parsed_payload IS NOT OLD.parsed_payload OR NEW.normalized_payload IS NOT OLD.normalized_payload OR NEW.parent_parser_output_id IS NOT OLD.parent_parser_output_id)
BEGIN SELECT RAISE(ABORT, 'AI fallback parent proposal source and payload are sealed'); END;

CREATE TRIGGER trg_ai_fallback_parent_no_delete
BEFORE DELETE ON parser_outputs
WHEN EXISTS (SELECT 1 FROM ai_fallback_attempts WHERE parent_parser_output_id = OLD.id) OR EXISTS (SELECT 1 FROM ai_fallback_proposal_links WHERE parser_output_id = OLD.id)
BEGIN SELECT RAISE(ABORT, 'AI fallback linked parser outputs cannot be deleted'); END;

CREATE TRIGGER trg_ai_fallback_raw_intake_no_source_update
BEFORE UPDATE ON raw_intake_records
WHEN EXISTS (SELECT 1 FROM ai_fallback_attempts WHERE raw_intake_record_id = OLD.id)
 AND (NEW.id IS NOT OLD.id OR NEW.public_id IS NOT OLD.public_id OR NEW.source_type IS NOT OLD.source_type OR NEW.source_channel IS NOT OLD.source_channel OR NEW.raw_input IS NOT OLD.raw_input OR NEW.source_received_at IS NOT OLD.source_received_at OR NEW.external_source_id IS NOT OLD.external_source_id OR NEW.source_message_id IS NOT OLD.source_message_id OR NEW.source_content_hash IS NOT OLD.source_content_hash OR NEW.attachment_hash IS NOT OLD.attachment_hash OR NEW.attachment_path IS NOT OLD.attachment_path OR NEW.attachment_id IS NOT OLD.attachment_id)
BEGIN SELECT RAISE(ABORT, 'AI fallback raw intake source identity is sealed'); END;

CREATE TRIGGER trg_ai_fallback_raw_intake_no_delete
BEFORE DELETE ON raw_intake_records
WHEN EXISTS (SELECT 1 FROM ai_fallback_attempts WHERE raw_intake_record_id = OLD.id)
BEGIN SELECT RAISE(ABORT, 'AI fallback raw intake cannot be deleted'); END;
