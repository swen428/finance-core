"""Pure S5e-A provenance primitives.

This module deliberately has no SQLite, bridge, OpenClaw, filesystem, or
network dependency.  It defines the canonical material and durable identity
rules that a later, separately-authorized S5e-B service will use to persist a
model fallback attempt.  S5e-A does not expose a command that can create any
of the records represented here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


class AiFallbackProvenanceValidationError(ValueError):
    """Raised when untrusted provenance material is not canonical."""


_HEX_64 = re.compile(r"^[0-9a-f]{64}$")

PREPARATION_MATERIAL_FIELDS = (
    "schema_version",
    "intake_public_id",
    "parent_public_id",
    "parent_version",
    "parent_effective_content_hash",
    "source_kind",
    "source_projection_hash",
    "source_selection_manifest_hash",
    "source_field_state_hash",
    "eligibility_mode",
    "eligibility_reasons",
    "runtime_policy_version",
    "runtime_policy_hash",
    "prompt_version",
    "prompt_template_hash",
    "intent_policy_version",
    "intent_policy_hash",
    "intent_policy_result",
    "intent_evidence_hash",
    "default_policy_version",
    "default_policy_hash",
    "default_evidence_hash",
    "sensitive_text_policy_version",
    "sensitive_text_policy_hash",
    "sensitive_text_scan_hash",
    "deadline_policy_version",
    "deadline_policy_hash",
    "sqlite_money_policy_version",
    "sqlite_money_policy_hash",
    "expected_provider",
    "expected_model",
    "expected_agent_id",
    "expected_audit_caller_kind",
    "expected_audit_caller_id",
    "expected_audit_caller_name",
    "expected_audit_purpose",
    "expected_audit_session_key_sha256",
    "request_sha256",
    "request_byte_count",
)

CLAIM_MATERIAL_FIELDS = (
    "schema_version",
    "attempt_public_id",
    "invocation_claimed_at_ms",
    "call_start_not_after_ms",
    "request_sha256",
    "invocation_disposition",
)

RESULT_MATERIAL_V1_FIELDS = (
    "schema_version",
    "attempt_public_id",
    "claim_public_id",
    "claim_material_hash",
    "transport_outcome",
    "result_status",
    "retention_state",
    "normal_attribution_hash",
    "metadata_refusal_hash",
    "usage_hash",
    "result_received_at_ms",
    "post_lock_at_ms",
    "decision_at_ms",
    "deadline_policy_version",
    "deadline_policy_hash",
    "deadline_disposition",
    "response_body_state",
    "response_sha256",
    "response_byte_count",
    "response_code_unit_count",
    "response_utf16_sha256",
    "failure_code",
    "non_child_reason",
    "recovery_disposition",
    "normalized_payload_hash",
    "source_field_state_hash",
    "ambiguity_hash",
    "evidence_set_hash",
)
RESULT_MATERIAL_FIELDS = RESULT_MATERIAL_V1_FIELDS
RESULT_MATERIAL_V2_FIELDS = (
    "schema_version",
    "attempt_public_id",
    "claim_public_id",
    "claim_material_hash",
    "result_arguments_hash",
    *RESULT_MATERIAL_V1_FIELDS[4:],
)

LINK_MATERIAL_FIELDS = (
    "schema_version",
    "result_public_id",
    "result_material_hash",
    "proposal_public_id",
    "effective_content_hash",
)

SOURCE_KINDS = frozenset({"telegram_raw_text", "receipt_local_ocr_text"})
FALLBACK_MODES = frozenset({"child_eligible", "classification_only"})
ELIGIBILITY_REASONS = frozenset(
    {
        "unsupported_language",
        "mixed_language_incomplete",
        "deterministic_fields_incomplete",
        "conflicting_text_candidates",
        "receipt_ocr_fields_incomplete",
        "intent_classification_required",
    }
)
INVOCATION_DISPOSITIONS = frozenset({"invoke_once"})
TRANSPORT_OUTCOMES = frozenset(
    {
        "response_received",
        "response_oversize",
        "response_unencodable",
        "response_resource_refused",
        "response_metadata_refused",
        "provider_error",
        "local_preinvocation_refused",
        "timeout",
        "cancelled",
    }
)
RETENTION_STATES = frozenset(
    {
        "blob_retained",
        "unretained_oversize",
        "unretained_unencodable",
        "unretained_resource_refused",
        "none",
    }
)
RESULT_STATUSES = frozenset(
    {
        "proposal_created",
        "classification_only",
        "response_refused",
        "attribution_refused",
        "provider_error",
        "preinvocation_refused",
        "timeout",
        "cancelled",
        "stale_parent",
        "late_result",
        "response_oversize",
        "response_unencodable",
        "response_resource_refused",
    }
)
RESPONSE_BODY_STATES = frozenset(
    {"retained", "oversize", "unencodable", "resource_refused", "none"}
)
NON_CHILD_REASONS = frozenset(
    {
        "missing_merchant",
        "forbidden_field",
        "source_unresolved",
        "intent_unproven",
        "validation_refused",
    }
)
RECOVERY_DISPOSITIONS_BY_RESULT_STATUS: dict[str, frozenset[str]] = {
    "provider_error": frozenset({"resend_new_intake_after_provider_failure"}),
    "preinvocation_refused": frozenset(
        {
            "resend_new_intake_after_not_invoked",
            "operator_runtime_review",
        }
    ),
    "timeout": frozenset({"resend_new_intake_after_timeout"}),
    "cancelled": frozenset({"resend_new_intake_after_cancellation"}),
    "stale_parent": frozenset({"review_current_parent_state"}),
    "late_result": frozenset({"resend_new_intake_after_late_result"}),
    "attribution_refused": frozenset({"operator_runtime_review"}),
    "response_oversize": frozenset({"use_manual_intake"}),
    "response_unencodable": frozenset({"use_manual_intake"}),
    "response_resource_refused": frozenset({"use_manual_intake"}),
}
FAILURE_CODES_BY_TRANSPORT: dict[str, frozenset[str]] = {
    "provider_error": frozenset({"host_llm_failed"}),
    "local_preinvocation_refused": frozenset(
        {
            "request_integrity_refused",
            "runtime_policy_refused",
            "call_start_deadline_exceeded",
        }
    ),
    "timeout": frozenset({"deadline_exceeded"}),
    "cancelled": frozenset({"cancelled"}),
}
RESULT_STATUSES_BY_TRANSPORT = {
    "provider_error": frozenset({"provider_error", "late_result"}),
    "local_preinvocation_refused": frozenset({"preinvocation_refused", "late_result"}),
    "timeout": frozenset({"timeout", "late_result"}),
    "cancelled": frozenset({"cancelled", "late_result"}),
    "response_unencodable": frozenset(
        {"response_unencodable", "stale_parent", "attribution_refused", "late_result"}
    ),
    "response_resource_refused": frozenset(
        {
            "response_resource_refused",
            "stale_parent",
            "attribution_refused",
            "late_result",
        }
    ),
}
RESPONSE_BODY_STATE_BY_TRANSPORT = {
    "response_received": "retained",
    "response_oversize": "oversize",
    "response_unencodable": "unencodable",
    "response_resource_refused": "resource_refused",
    "provider_error": "none",
    "local_preinvocation_refused": "none",
    "timeout": "none",
    "cancelled": "none",
}
RETENTION_STATE_BY_RESPONSE_BODY = {
    "retained": "blob_retained",
    "oversize": "unretained_oversize",
    "unencodable": "unretained_unencodable",
    "resource_refused": "unretained_resource_refused",
    "none": "none",
}

_HASH_FIELDS = frozenset(
    {
        field
        for field in (
            *PREPARATION_MATERIAL_FIELDS,
            *RESULT_MATERIAL_V1_FIELDS,
            *RESULT_MATERIAL_V2_FIELDS,
            *LINK_MATERIAL_FIELDS,
        )
        if field.endswith("_hash") or field == "request_sha256"
    }
    - {
        "normal_attribution_hash",
        "metadata_refusal_hash",
        "usage_hash",
        "response_sha256",
        "response_utf16_sha256",
        "normalized_payload_hash",
        "ambiguity_hash",
        "evidence_set_hash",
        "default_evidence_hash",
        "expected_audit_session_key_sha256",
    }
)
_OPTIONAL_HASH_FIELDS = frozenset(
    {
        "normal_attribution_hash",
        "metadata_refusal_hash",
        "usage_hash",
        "response_sha256",
        "response_utf16_sha256",
        "normalized_payload_hash",
        "ambiguity_hash",
        "evidence_set_hash",
        "default_evidence_hash",
        "expected_audit_session_key_sha256",
    }
)
_OPTIONAL_TEXT_FIELDS = frozenset(
    {
        "expected_audit_caller_name",
        "failure_code",
        "non_child_reason",
        "recovery_disposition",
    }
)


def canonical_json_bytes(material: Mapping[str, Any]) -> bytes:
    """Return the contract's compact, sorted, ASCII-only JSON bytes.

    Python ``float`` values are prohibited before serialization: a floating
    representation cannot be a stable Finance provenance material.
    """

    _validate_json_value(material)
    try:
        return json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:  # defensive: validation is intentionally strict
        raise AiFallbackProvenanceValidationError("material is not canonical JSON") from exc


def canonical_material_hash(domain: str, material: Mapping[str, Any]) -> str:
    """Hash canonical material with the required ASCII domain separator."""

    domain_bytes = _ascii_domain(domain)
    return hashlib.sha256(domain_bytes + b"\x00" + canonical_json_bytes(material)).hexdigest()


def canonical_request_sha256(request_blob: bytes) -> str:
    """Hash a bounded immutable request BLOB without parsing or dispatching it."""

    return _canonical_bounded_blob_sha256(request_blob, "request")


def canonical_response_sha256(response_blob: bytes) -> str:
    """Hash retained response evidence without parsing, retaining, or dispatching it."""

    return _canonical_bounded_blob_sha256(response_blob, "response")


def preparation_material_hash(material: Mapping[str, Any]) -> str:
    _validate_preparation_material(material)
    return canonical_material_hash("finance-ai-preparation-material-v1", material)


def claim_material_hash(material: Mapping[str, Any]) -> str:
    _validate_claim_material(material)
    return canonical_material_hash("finance-ai-claim-material-v1", material)


def result_material_hash(material: Mapping[str, Any]) -> str:
    _validate_result_material(material)
    return canonical_material_hash("finance-ai-result-material-v1", material)


def result_material_v2_hash(material: Mapping[str, Any]) -> str:
    """Seal S5e-B result material including the exact arguments hash."""
    _validate_result_material_v2(material)
    return canonical_material_hash("finance-ai-result-material-v2", material)


def link_material_hash(material: Mapping[str, Any]) -> str:
    _validate_link_material(material)
    return canonical_material_hash("finance-ai-link-material-v1", material)


def derive_attempt_public_id(preparation_hash: str) -> str:
    _require_hash(preparation_hash, "preparation_hash")
    return _derive_durable_id("aifa_", "finance-ai-attempt-id-v1", (preparation_hash,))


def derive_claim_public_id(attempt_public_id: str) -> str:
    _require_public_id(attempt_public_id, "aifa_", "attempt_public_id")
    return _derive_durable_id("aicl_", "finance-ai-claim-id-v1", (attempt_public_id,))


def derive_result_public_id(attempt_public_id: str) -> str:
    _require_public_id(attempt_public_id, "aifa_", "attempt_public_id")
    return _derive_durable_id("air_", "finance-ai-result-id-v1", (attempt_public_id,))


def derive_link_public_id(result_public_id: str, proposal_public_id: str) -> str:
    _require_public_id(result_public_id, "air_", "result_public_id")
    _require_text(proposal_public_id, "proposal_public_id")
    return _derive_durable_id(
        "aipl_", "finance-ai-link-id-v1", (result_public_id, proposal_public_id)
    )


def _derive_durable_id(prefix: str, domain: str, fields: Sequence[str]) -> str:
    payload = bytearray(_ascii_domain(domain))
    payload.extend(b"\x00")
    payload.extend(len(fields).to_bytes(4, "big", signed=False))
    for field in fields:
        _require_text(field, "identity field")
        encoded = field.encode("utf-8", errors="strict")
        payload.extend(len(encoded).to_bytes(8, "big", signed=False))
        payload.extend(encoded)
    return prefix + hashlib.sha256(payload).hexdigest()


def _validate_preparation_material(material: Mapping[str, Any]) -> None:
    _validate_exact_fields(material, PREPARATION_MATERIAL_FIELDS, "preparation material")
    _validate_common_material_values(material)
    if material["schema_version"] != "finance-ai-preparation-material-v1":
        raise AiFallbackProvenanceValidationError("preparation schema_version is invalid")
    _require_nonnegative_int(material["parent_version"], "parent_version")
    _require_nonnegative_int(material["request_byte_count"], "request_byte_count", maximum=65_536)
    _require_enum(material["source_kind"], SOURCE_KINDS, "source_kind")
    _require_enum(material["eligibility_mode"], FALLBACK_MODES, "eligibility_mode")
    reasons = material["eligibility_reasons"]
    if not isinstance(reasons, list) or not reasons:
        raise AiFallbackProvenanceValidationError("eligibility_reasons must be a non-empty list")
    if any(reason not in ELIGIBILITY_REASONS for reason in reasons):
        raise AiFallbackProvenanceValidationError("eligibility_reasons contain an invalid code")
    if reasons != sorted(set(reasons)):
        raise AiFallbackProvenanceValidationError("eligibility_reasons must be sorted and unique")
    is_classification_only = material["eligibility_mode"] == "classification_only"
    has_classification_reason = "intent_classification_required" in reasons
    if is_classification_only != has_classification_reason:
        raise AiFallbackProvenanceValidationError(
            "eligibility_mode and intent_classification_required must agree"
        )


def _validate_claim_material(material: Mapping[str, Any]) -> None:
    _validate_exact_fields(material, CLAIM_MATERIAL_FIELDS, "claim material")
    if material["schema_version"] != "finance-ai-claim-material-v1":
        raise AiFallbackProvenanceValidationError("claim schema_version is invalid")
    _require_text(material["attempt_public_id"], "attempt_public_id")
    _require_nonnegative_int(material["invocation_claimed_at_ms"], "invocation_claimed_at_ms")
    _require_nonnegative_int(material["call_start_not_after_ms"], "call_start_not_after_ms")
    if material["call_start_not_after_ms"] <= material["invocation_claimed_at_ms"]:
        raise AiFallbackProvenanceValidationError(
            "call_start_not_after_ms must be after invocation_claimed_at_ms"
        )
    _require_hash(material["request_sha256"], "request_sha256")
    _require_enum(
        material["invocation_disposition"],
        INVOCATION_DISPOSITIONS,
        "invocation_disposition",
    )


def _validate_result_material(material: Mapping[str, Any]) -> None:
    _validate_result_material_contract(
        material,
        fields=RESULT_MATERIAL_V1_FIELDS,
        schema_version="finance-ai-result-material-v1",
    )


def _validate_result_material_v2(material: Mapping[str, Any]) -> None:
    _validate_result_material_contract(
        material,
        fields=RESULT_MATERIAL_V2_FIELDS,
        schema_version="finance-ai-result-material-v2",
    )


def _validate_result_material_contract(
    material: Mapping[str, Any],
    *,
    fields: Sequence[str],
    schema_version: str,
) -> None:
    _validate_exact_fields(material, fields, "result material")
    _validate_common_material_values(material)
    if material["schema_version"] != schema_version:
        raise AiFallbackProvenanceValidationError("result schema_version is invalid")
    _require_text(material["attempt_public_id"], "attempt_public_id")
    _require_text(material["claim_public_id"], "claim_public_id")
    _require_enum(material["transport_outcome"], TRANSPORT_OUTCOMES, "transport_outcome")
    _require_enum(material["result_status"], RESULT_STATUSES, "result_status")
    _require_enum(material["retention_state"], RETENTION_STATES, "retention_state")
    _require_enum(material["response_body_state"], RESPONSE_BODY_STATES, "response_body_state")
    for field in ("result_received_at_ms", "post_lock_at_ms", "decision_at_ms"):
        _require_nonnegative_int(material[field], field)
    if material["post_lock_at_ms"] < material["result_received_at_ms"]:
        raise AiFallbackProvenanceValidationError(
            "post_lock_at_ms must not precede result_received_at_ms"
        )
    if material["decision_at_ms"] < material["post_lock_at_ms"]:
        raise AiFallbackProvenanceValidationError("decision_at_ms must not precede post_lock_at_ms")
    for field in ("response_byte_count", "response_code_unit_count"):
        if material[field] is not None:
            _require_nonnegative_int(material[field], field)

    _validate_result_transport_and_evidence(material)

    status = material["result_status"]
    non_child_reason = material["non_child_reason"]
    recovery_disposition = material["recovery_disposition"]
    if status == "proposal_created":
        if material["non_child_reason"] is not None or material["recovery_disposition"] is not None:
            raise AiFallbackProvenanceValidationError("proposal_created cannot carry recovery")
    elif status == "classification_only":
        if non_child_reason != "intent_unproven" or recovery_disposition is not None:
            raise AiFallbackProvenanceValidationError(
                "classification_only requires intent_unproven and no recovery disposition"
            )
    elif status == "response_refused":
        if recovery_disposition is not None:
            raise AiFallbackProvenanceValidationError(
                "response_refused cannot carry a recovery disposition"
            )
        _require_enum(non_child_reason, NON_CHILD_REASONS, "non_child_reason")
    elif status in RECOVERY_DISPOSITIONS_BY_RESULT_STATUS:
        if non_child_reason is not None:
            raise AiFallbackProvenanceValidationError(f"{status} cannot carry a non_child_reason")
        _require_enum(
            recovery_disposition,
            RECOVERY_DISPOSITIONS_BY_RESULT_STATUS[status],
            "recovery_disposition",
        )
        if status == "preinvocation_refused":
            expected_recovery = (
                "resend_new_intake_after_not_invoked"
                if material["failure_code"] == "call_start_deadline_exceeded"
                else "operator_runtime_review"
            )
            if recovery_disposition != expected_recovery:
                raise AiFallbackProvenanceValidationError(
                    "preinvocation_refused recovery_disposition does not match failure_code"
                )
    else:  # defensive: RESULT_STATUSES above is the closed source of truth
        raise AiFallbackProvenanceValidationError(
            "result_status does not have a recovery discriminator"
        )


def _validate_result_transport_and_evidence(material: Mapping[str, Any]) -> None:
    transport = material["transport_outcome"]
    status = material["result_status"]
    expected_statuses = RESULT_STATUSES_BY_TRANSPORT.get(transport)
    if expected_statuses is not None and status not in expected_statuses:
        raise AiFallbackProvenanceValidationError(f"{transport} has an invalid result_status")
    if transport == "response_received" and status not in {
        "proposal_created",
        "classification_only",
        "response_refused",
        "attribution_refused",
        "stale_parent",
        "late_result",
        "response_oversize",
    }:
        raise AiFallbackProvenanceValidationError("response_received has an invalid result_status")
    if transport == "response_oversize" and status not in {
        "response_oversize",
        "stale_parent",
        "attribution_refused",
        "late_result",
    }:
        raise AiFallbackProvenanceValidationError("response_oversize has an invalid result_status")
    if status == "response_oversize" and transport not in {
        "response_received",
        "response_oversize",
    }:
        raise AiFallbackProvenanceValidationError(
            "response_oversize result_status requires response transport evidence"
        )
    if transport == "response_metadata_refused" and status not in {
        "attribution_refused",
        "stale_parent",
        "late_result",
    }:
        raise AiFallbackProvenanceValidationError(
            "response_metadata_refused has an invalid result_status"
        )

    expected_body_state = RESPONSE_BODY_STATE_BY_TRANSPORT.get(transport)
    if expected_body_state is not None and material["response_body_state"] != expected_body_state:
        raise AiFallbackProvenanceValidationError(
            f"{transport} requires response_body_state {expected_body_state}"
        )
    _validate_response_evidence(material)
    _validate_result_attribution_evidence(material)
    if (
        transport == "response_received"
        and material["response_byte_count"] > 16_384
        and status
        not in {"response_oversize", "stale_parent", "attribution_refused", "late_result"}
    ):
        raise AiFallbackProvenanceValidationError(
            "retained response exceeds 16,384 bytes without response_oversize status"
        )
    if (
        transport == "response_received"
        and status == "response_oversize"
        and material["response_byte_count"] <= 16_384
    ):
        raise AiFallbackProvenanceValidationError(
            "response_oversize retained response must exceed 16,384 bytes"
        )

    allowed_failure_codes = FAILURE_CODES_BY_TRANSPORT.get(transport)
    if allowed_failure_codes is None:
        if material["failure_code"] is not None:
            raise AiFallbackProvenanceValidationError(
                "response transport outcomes cannot carry failure_code"
            )
    else:
        _require_enum(material["failure_code"], allowed_failure_codes, "failure_code")


def _validate_result_attribution_evidence(material: Mapping[str, Any]) -> None:
    transport = material["transport_outcome"]
    normal_attribution_hash = material["normal_attribution_hash"]
    metadata_refusal_hash = material["metadata_refusal_hash"]
    usage_hash = material["usage_hash"]
    if transport in {
        "response_received",
        "response_oversize",
        "response_unencodable",
        "response_resource_refused",
    }:
        _require_hash(normal_attribution_hash, "normal_attribution_hash")
        _require_hash(usage_hash, "usage_hash")
        if metadata_refusal_hash is not None:
            raise AiFallbackProvenanceValidationError(
                "normal response transport cannot carry metadata_refusal_hash"
            )
    elif transport == "response_metadata_refused":
        _require_hash(metadata_refusal_hash, "metadata_refusal_hash")
        if normal_attribution_hash is not None or usage_hash is not None:
            raise AiFallbackProvenanceValidationError(
                "metadata refusal cannot carry normal attribution or usage evidence"
            )
    elif any(
        value is not None for value in (normal_attribution_hash, metadata_refusal_hash, usage_hash)
    ):
        raise AiFallbackProvenanceValidationError(
            "non-response transport cannot carry attribution or usage evidence"
        )


def _validate_response_evidence(material: Mapping[str, Any]) -> None:
    state = material["response_body_state"]
    if material["retention_state"] != RETENTION_STATE_BY_RESPONSE_BODY[state]:
        raise AiFallbackProvenanceValidationError(
            "retention_state does not match response_body_state"
        )

    response_sha256 = material["response_sha256"]
    byte_count = material["response_byte_count"]
    code_unit_count = material["response_code_unit_count"]
    utf16_sha256 = material["response_utf16_sha256"]
    if state == "retained":
        _require_hash(response_sha256, "response_sha256")
        _require_nonnegative_int(byte_count, "response_byte_count", maximum=65_536)
        if code_unit_count is not None or utf16_sha256 is not None:
            raise AiFallbackProvenanceValidationError(
                "retained response cannot carry UTF-16 evidence"
            )
    elif state == "oversize":
        _require_hash(response_sha256, "response_sha256")
        _require_nonnegative_int(byte_count, "response_byte_count")
        _require_nonnegative_int(code_unit_count, "response_code_unit_count", maximum=131_072)
        if byte_count < 65_537 or utf16_sha256 is not None:
            raise AiFallbackProvenanceValidationError("oversize response evidence is invalid")
    elif state == "unencodable":
        _require_nonnegative_int(code_unit_count, "response_code_unit_count")
        _require_hash(utf16_sha256, "response_utf16_sha256")
        if response_sha256 is not None or byte_count is not None:
            raise AiFallbackProvenanceValidationError(
                "unencodable response cannot carry UTF-8 evidence"
            )
    elif state == "resource_refused":
        _require_nonnegative_int(code_unit_count, "response_code_unit_count")
        if (
            code_unit_count < 131_073
            or response_sha256 is not None
            or byte_count is not None
            or utf16_sha256 is not None
        ):
            raise AiFallbackProvenanceValidationError(
                "resource-refused response evidence is invalid"
            )
    else:  # response_body_state is closed before this helper runs
        if any(
            value is not None
            for value in (response_sha256, byte_count, code_unit_count, utf16_sha256)
        ):
            raise AiFallbackProvenanceValidationError(
                "body-less result cannot carry response evidence"
            )


def _validate_link_material(material: Mapping[str, Any]) -> None:
    _validate_exact_fields(material, LINK_MATERIAL_FIELDS, "link material")
    _validate_common_material_values(material)
    if material["schema_version"] != "finance-ai-link-material-v1":
        raise AiFallbackProvenanceValidationError("link schema_version is invalid")
    _require_text(material["result_public_id"], "result_public_id")
    _require_text(material["proposal_public_id"], "proposal_public_id")


def _validate_common_material_values(material: Mapping[str, Any]) -> None:
    for field, value in material.items():
        if field in _HASH_FIELDS:
            _require_hash(value, field)
        elif field in _OPTIONAL_HASH_FIELDS:
            if value is not None:
                _require_hash(value, field)
        elif field in _OPTIONAL_TEXT_FIELDS:
            if value is not None:
                _require_text(value, field)
        elif field not in {
            "parent_version",
            "request_byte_count",
            "eligibility_reasons",
            "result_received_at_ms",
            "post_lock_at_ms",
            "decision_at_ms",
            "response_byte_count",
            "response_code_unit_count",
        }:
            if value is None:
                raise AiFallbackProvenanceValidationError(f"{field} must not be null")
            _require_text(value, field)


def _validate_exact_fields(
    material: Mapping[str, Any], expected: Sequence[str], label: str
) -> None:
    if not isinstance(material, Mapping):
        raise AiFallbackProvenanceValidationError(f"{label} must be an object")
    actual = frozenset(material)
    required = frozenset(expected)
    if actual != required:
        missing = sorted(required - actual)
        unknown = sorted(actual - required)
        raise AiFallbackProvenanceValidationError(
            f"{label} fields differ; missing={missing}, unknown={unknown}"
        )


def _validate_json_value(value: Any) -> None:
    if value is None or isinstance(value, (bool, str, int)):
        if isinstance(value, str):
            _require_text(value, "JSON string")
        return
    if isinstance(value, float):
        raise AiFallbackProvenanceValidationError("floats are prohibited in provenance material")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise AiFallbackProvenanceValidationError("JSON object keys must be strings")
            _require_text(key, "JSON key")
            _validate_json_value(child)
        return
    if isinstance(value, list):
        for child in value:
            _validate_json_value(child)
        return
    raise AiFallbackProvenanceValidationError("material has an unsupported JSON value")


def _ascii_domain(value: str) -> bytes:
    _require_text(value, "domain")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise AiFallbackProvenanceValidationError("domain must be ASCII") from exc
    if b"\x00" in encoded:
        raise AiFallbackProvenanceValidationError("domain must not contain NUL")
    return encoded


def _canonical_bounded_blob_sha256(blob: bytes, label: str) -> str:
    if not isinstance(blob, bytes) or len(blob) > 65_536:
        raise AiFallbackProvenanceValidationError(
            f"{label} BLOB must be bytes at most 65,536 bytes"
        )
    return hashlib.sha256(blob).hexdigest()


def _require_hash(value: Any, label: str) -> None:
    if not isinstance(value, str) or _HEX_64.fullmatch(value) is None:
        raise AiFallbackProvenanceValidationError(f"{label} must be lowercase SHA-256 hex")


def _require_public_id(value: Any, prefix: str, label: str) -> None:
    _require_text(value, label)
    if (
        len(value) != len(prefix) + 64
        or not value.startswith(prefix)
        or _HEX_64.fullmatch(value[len(prefix) :]) is None
    ):
        raise AiFallbackProvenanceValidationError(
            f"{label} must be a {prefix}-prefixed lowercase SHA-256 identifier"
        )


def _require_text(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise AiFallbackProvenanceValidationError(f"{label} must be a non-empty string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise AiFallbackProvenanceValidationError(f"{label} must contain Unicode scalars") from exc


def _require_nonnegative_int(value: Any, label: str, *, maximum: int | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AiFallbackProvenanceValidationError(f"{label} must be a non-negative integer")
    if maximum is not None and value > maximum:
        raise AiFallbackProvenanceValidationError(f"{label} exceeds the allowed maximum")


def _require_enum(value: Any, choices: frozenset[str], label: str) -> None:
    if value not in choices:
        raise AiFallbackProvenanceValidationError(f"{label} is not an allowed value")
