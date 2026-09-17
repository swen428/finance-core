"""Pure durable-state projection for Nomi AI processing status v2."""

from __future__ import annotations

from typing import Any, Mapping

PROCESSING_PATHS = frozenset(
    {
        "no_model",
        "local_model",
        "cloud_projection",
        "model_denied",
        "model_failed",
        "outcome_unknown",
    }
)

_PREINVOCATION_REASONS = {
    "request_integrity_refused": "configuration_not_accepted",
    "runtime_policy_refused": "policy_denied",
    "call_start_deadline_exceeded": "deadline_exceeded",
}
_FAILED_REASONS = {
    "attribution_refused": "attribution_mismatch",
    "timeout": "deadline_exceeded",
    "late_result": "deadline_exceeded",
    "provider_error": "provider_unavailable",
    "response_oversize": "output_invalid",
    "response_unencodable": "output_invalid",
    "response_resource_refused": "output_invalid",
    "response_refused": "output_invalid",
    "cancelled": "outcome_unknown",
}


def derive_ai_processing_status_v2(
    state: Mapping[str, Any],
    *,
    claim_exists: bool,
    result_status: str | None,
    failure_code: str | None,
) -> dict[str, Any]:
    """Map immutable receipt/attempt state to closed, presentation-free codes."""
    intake_public_id = state["intake_public_id"]
    attempt_public_id = state.get("attempt_public_id")
    receipt_public_id = state.get("receipt_public_id")
    if attempt_public_id is None:
        admission_decision_public_id = state.get("admission_decision_public_id")
        if admission_decision_public_id is not None:
            return {
                "intake_public_id": intake_public_id,
                "attempt_public_id": None,
                "receipt_public_id": None,
                "admission_decision_public_id": admission_decision_public_id,
                "processing_path": "model_denied",
                "safe_reason_code": state.get("admission_safe_reason_code"),
                "canonical_attribution": None,
                "display_alias": None,
                "attribution_match": None,
            }
        return {
            "intake_public_id": intake_public_id,
            "attempt_public_id": None,
            "receipt_public_id": None,
            "admission_decision_public_id": None,
            "processing_path": "no_model",
            "safe_reason_code": None,
            "canonical_attribution": None,
            "display_alias": None,
            "attribution_match": None,
        }
    if receipt_public_id is None:
        return {
            "intake_public_id": intake_public_id,
            "attempt_public_id": attempt_public_id,
            "receipt_public_id": None,
            "admission_decision_public_id": None,
            "processing_path": "model_denied",
            "safe_reason_code": "status_unavailable",
            "canonical_attribution": None,
            "display_alias": None,
            "attribution_match": None,
        }

    safe_reason: str | None = None
    attribution_match: bool | None = None
    if claim_exists and result_status is None:
        processing_path = "outcome_unknown"
        safe_reason = "outcome_unknown"
    elif result_status in {"proposal_created", "classification_only"}:
        execution_class = state.get("execution_class")
        processing_path = (
            execution_class
            if execution_class in {"local_model", "cloud_projection"}
            else "model_failed"
        )
        safe_reason = None if processing_path != "model_failed" else "status_unavailable"
        attribution_match = processing_path != "model_failed"
    elif result_status == "preinvocation_refused":
        processing_path = "model_denied"
        safe_reason = (
            _PREINVOCATION_REASONS.get(failure_code, "status_unavailable")
            if isinstance(failure_code, str)
            else "status_unavailable"
        )
    elif result_status == "stale_parent":
        processing_path = "model_failed"
        safe_reason = "status_unavailable"
    elif result_status is not None:
        processing_path = "model_failed"
        safe_reason = _FAILED_REASONS.get(result_status, "status_unavailable")
        attribution_match = False if result_status == "attribution_refused" else None
    else:
        processing_path = "no_model"

    return {
        "intake_public_id": intake_public_id,
        "attempt_public_id": attempt_public_id,
        "receipt_public_id": receipt_public_id,
        "admission_decision_public_id": None,
        "processing_path": processing_path,
        "safe_reason_code": safe_reason,
        "canonical_attribution": {
            "provider": state["canonical_provider"],
            "model": state["canonical_model"],
            "agent_id": state["agent_id"],
        },
        "display_alias": state["display_alias"],
        "attribution_match": attribution_match,
    }


__all__ = ["PROCESSING_PATHS", "derive_ai_processing_status_v2"]
