"""Versioned v1 request/response envelope contract for the bridge CLI.

One request on stdin, one response on stdout.  Strict field validation,
no unknown fields, byte and depth limits, and stable request identities
(design §5.1–§5.3).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from finance_core.openclaw_staging_bridge import errors

ENVELOPE_VERSION = "v1"

MAX_REQUEST_BYTES = 262_144  # 256 KiB
MAX_RESPONSE_BYTES = 1_048_576  # 1 MiB
MAX_ENVELOPE_DEPTH = 8
MAX_IDEMPOTENCY_KEY_LENGTH = 200

COMMAND_HEALTH = "health"
COMMAND_GET_STATUS = "get_status"
COMMAND_CAPTURE = "capture"
COMMAND_PROPOSE = "propose"
COMMAND_GET_REVIEW = "get_review"
COMMAND_CONFIRM = "confirm"
COMMAND_EDIT = "edit"
COMMAND_REJECT = "reject"
COMMAND_ISSUE_HUMAN_ACTIONS = "issue_human_actions"
COMMAND_REDEEM_HUMAN_ACTION = "redeem_human_action"
COMMAND_GET_GUIDED_EDIT_SESSION = "get_guided_edit_session"
COMMAND_APPLY_GUIDED_EDIT_UPDATE = "apply_guided_edit_update"
COMMAND_COMPLETE_GUIDED_EDIT = "complete_guided_edit"
COMMAND_APPLY_HUMAN_DRAFT_CARD = "apply_human_draft_card"
COMMAND_GET_HUMAN_DRAFT_CARD = "get_human_draft_card"
COMMAND_BEGIN_HUMAN_DRAFT_CARD_DELIVERY = "begin_human_draft_card_delivery"
COMMAND_RECORD_HUMAN_DRAFT_CARD_DELIVERY_OUTCOME = "record_human_draft_card_delivery_outcome"
COMMAND_REISSUE_HUMAN_DRAFT_CARD = "reissue_human_draft_card"
COMMAND_FINALIZE = "finalize"
COMMAND_PREPARE_RECEIPT_COMPLETION = "prepare_receipt_completion"
COMMAND_GET_FINALIZATION_SNAPSHOT_REVIEW = "get_finalization_snapshot_review"
COMMAND_AUTHORIZE_FINALIZATION = "authorize_finalization"
COMMAND_APPLY_FACT_SET = "apply_fact_set"
COMMAND_PREPARE_AI_FALLBACK = "prepare_ai_fallback"
COMMAND_CLAIM_AI_FALLBACK_INVOCATION = "claim_ai_fallback_invocation"
COMMAND_RECORD_AI_FALLBACK_RESULT = "record_ai_fallback_result"
COMMAND_VERIFY_AI_MODEL_COMPATIBILITY_CASE_V2 = "verify_ai_model_compatibility_case_v2"
COMMAND_REGISTER_AI_MODEL_COMPATIBILITY_RECEIPT_V2 = "register_ai_model_compatibility_receipt_v2"
COMMAND_PREPARE_AI_FALLBACK_V2 = "prepare_ai_fallback_v2"
COMMAND_CLAIM_AI_FALLBACK_INVOCATION_V2 = "claim_ai_fallback_invocation_v2"
COMMAND_RECORD_AI_FALLBACK_RESULT_V2 = "record_ai_fallback_result_v2"
COMMAND_GET_AI_PROCESSING_STATUS_V2 = "get_ai_processing_status_v2"

ALLOWED_COMMANDS = frozenset(
    {
        COMMAND_HEALTH,
        COMMAND_GET_STATUS,
        COMMAND_CAPTURE,
        COMMAND_PROPOSE,
        COMMAND_GET_REVIEW,
        COMMAND_CONFIRM,
        COMMAND_EDIT,
        COMMAND_REJECT,
        COMMAND_ISSUE_HUMAN_ACTIONS,
        COMMAND_REDEEM_HUMAN_ACTION,
        COMMAND_GET_GUIDED_EDIT_SESSION,
        COMMAND_APPLY_GUIDED_EDIT_UPDATE,
        COMMAND_COMPLETE_GUIDED_EDIT,
        COMMAND_APPLY_HUMAN_DRAFT_CARD,
        COMMAND_GET_HUMAN_DRAFT_CARD,
        COMMAND_BEGIN_HUMAN_DRAFT_CARD_DELIVERY,
        COMMAND_RECORD_HUMAN_DRAFT_CARD_DELIVERY_OUTCOME,
        COMMAND_REISSUE_HUMAN_DRAFT_CARD,
        COMMAND_FINALIZE,
        COMMAND_PREPARE_RECEIPT_COMPLETION,
        COMMAND_GET_FINALIZATION_SNAPSHOT_REVIEW,
        COMMAND_AUTHORIZE_FINALIZATION,
        COMMAND_APPLY_FACT_SET,
        COMMAND_PREPARE_AI_FALLBACK,
        COMMAND_CLAIM_AI_FALLBACK_INVOCATION,
        COMMAND_RECORD_AI_FALLBACK_RESULT,
        COMMAND_VERIFY_AI_MODEL_COMPATIBILITY_CASE_V2,
        COMMAND_REGISTER_AI_MODEL_COMPATIBILITY_RECEIPT_V2,
        COMMAND_PREPARE_AI_FALLBACK_V2,
        COMMAND_CLAIM_AI_FALLBACK_INVOCATION_V2,
        COMMAND_RECORD_AI_FALLBACK_RESULT_V2,
        COMMAND_GET_AI_PROCESSING_STATUS_V2,
    }
)

MUTATING_COMMANDS = frozenset(
    {
        COMMAND_CAPTURE,
        COMMAND_PROPOSE,
        COMMAND_CONFIRM,
        COMMAND_EDIT,
        COMMAND_REJECT,
        COMMAND_ISSUE_HUMAN_ACTIONS,
        COMMAND_REDEEM_HUMAN_ACTION,
        COMMAND_APPLY_GUIDED_EDIT_UPDATE,
        COMMAND_COMPLETE_GUIDED_EDIT,
        COMMAND_APPLY_HUMAN_DRAFT_CARD,
        COMMAND_BEGIN_HUMAN_DRAFT_CARD_DELIVERY,
        COMMAND_RECORD_HUMAN_DRAFT_CARD_DELIVERY_OUTCOME,
        COMMAND_REISSUE_HUMAN_DRAFT_CARD,
        COMMAND_FINALIZE,
        COMMAND_PREPARE_RECEIPT_COMPLETION,
        COMMAND_GET_FINALIZATION_SNAPSHOT_REVIEW,
        COMMAND_AUTHORIZE_FINALIZATION,
        COMMAND_APPLY_FACT_SET,
        COMMAND_PREPARE_AI_FALLBACK,
        COMMAND_CLAIM_AI_FALLBACK_INVOCATION,
        COMMAND_RECORD_AI_FALLBACK_RESULT,
        COMMAND_REGISTER_AI_MODEL_COMPATIBILITY_RECEIPT_V2,
        COMMAND_PREPARE_AI_FALLBACK_V2,
        COMMAND_CLAIM_AI_FALLBACK_INVOCATION_V2,
        COMMAND_RECORD_AI_FALLBACK_RESULT_V2,
    }
)

_REQUEST_ID_RE = re.compile(r"^req_[0-9a-f]{32}$")
_TOP_LEVEL_FIELDS = frozenset(
    {"envelope_version", "command", "request_id", "idempotency_key", "arguments"}
)


@dataclass(frozen=True)
class BridgeRequest:
    """A fully validated v1 request envelope."""

    envelope_version: str
    command: str
    request_id: str
    idempotency_key: str | None
    arguments: dict[str, Any]


def _reject(code: str, message: str) -> errors.BridgeError:
    return errors.bridge_error(code, message, errors.EXIT_MALFORMED_ENVELOPE, retryable=False)


def check_depth(value: Any, *, maximum: int = MAX_ENVELOPE_DEPTH) -> None:
    """Fail closed when the parsed envelope nests deeper than the limit."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > maximum:
            raise _reject(errors.ENVELOPE_TOO_DEEP, "Envelope nesting depth exceeds the limit.")
        if isinstance(node, dict):
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)


def validate_idempotency_key(value: Any) -> str:
    if not isinstance(value, str):
        raise _reject(errors.MALFORMED_ENVELOPE, "idempotency_key must be a string.")
    if not value or not value.strip():
        raise _reject(errors.MALFORMED_ENVELOPE, "idempotency_key must not be empty.")
    if len(value) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise _reject(errors.MALFORMED_ENVELOPE, "idempotency_key exceeds 200 characters.")
    if not value.isascii() or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        raise _reject(errors.MALFORMED_ENVELOPE, "idempotency_key contains unsafe characters.")
    return value


def parse_request(raw: bytes) -> BridgeRequest:
    """Parse and validate one bounded request envelope.

    Raises BridgeError with stable codes for malformed, oversized, too-deep,
    or unknown-command envelopes.
    """
    if len(raw) > MAX_REQUEST_BYTES:
        raise errors.bridge_error(
            errors.OVERSIZED_ENVELOPE,
            "Request envelope exceeds the 256 KiB stdin limit.",
            errors.EXIT_MALFORMED_ENVELOPE,
            retryable=False,
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _reject(
            errors.MALFORMED_ENVELOPE, f"Request envelope is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise _reject(errors.MALFORMED_ENVELOPE, "Request envelope must be a JSON object.")

    check_depth(payload)

    unknown = frozenset(payload) - _TOP_LEVEL_FIELDS
    if unknown:
        raise _reject(errors.MALFORMED_ENVELOPE, f"Unknown envelope fields: {sorted(unknown)}")
    missing = {"envelope_version", "command", "request_id", "arguments"} - frozenset(payload)
    if missing:
        raise _reject(errors.MALFORMED_ENVELOPE, f"Missing envelope fields: {sorted(missing)}")

    envelope_version = payload["envelope_version"]
    if envelope_version != ENVELOPE_VERSION:
        raise _reject(
            errors.MALFORMED_ENVELOPE, f"Unsupported envelope_version: {envelope_version!r}"
        )

    command = payload["command"]
    if not isinstance(command, str) or command not in ALLOWED_COMMANDS:
        raise errors.bridge_error(
            errors.UNKNOWN_COMMAND,
            f"Command is not allowlisted: {command!r}",
            errors.EXIT_UNKNOWN_COMMAND,
            retryable=False,
        )

    request_id = payload["request_id"]
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.match(request_id):
        raise _reject(errors.MALFORMED_ENVELOPE, "request_id must match req_<32 lowercase hex>.")

    idempotency_key: str | None = None
    if "idempotency_key" in payload:
        idempotency_key = validate_idempotency_key(payload["idempotency_key"])
    elif command in MUTATING_COMMANDS:
        raise errors.bridge_error(
            errors.MISSING_IDEMPOTENCY_KEY,
            "Mutating commands require idempotency_key.",
            errors.EXIT_MALFORMED_ENVELOPE,
            retryable=False,
        )

    arguments = payload["arguments"]
    if not isinstance(arguments, dict):
        raise _reject(errors.MALFORMED_ENVELOPE, "arguments must be a JSON object.")

    return BridgeRequest(
        envelope_version=envelope_version,
        command=command,
        request_id=request_id,
        idempotency_key=idempotency_key,
        arguments=arguments,
    )


def success_response(
    *,
    request_id: str | None,
    operation_id: str | None,
    result: dict[str, Any],
    idempotent_replay: bool,
) -> dict[str, Any]:
    return {
        "envelope_version": ENVELOPE_VERSION,
        "request_id": request_id,
        "operation_id": operation_id,
        "status": "ok",
        "result": result,
        "idempotent_replay": idempotent_replay,
    }


def error_response(
    *,
    request_id: str | None,
    operation_id: str | None,
    error: errors.BridgeError,
) -> dict[str, Any]:
    error_payload: dict[str, Any] = {
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
    }
    if error.details:
        # Bounded machine-readable refusal context (identities, counts,
        # reason discriminators only — never amounts, key material, or
        # database contents).
        error_payload["details"] = dict(error.details)
    return {
        "envelope_version": ENVELOPE_VERSION,
        "request_id": request_id,
        "operation_id": operation_id,
        "status": "error",
        "error": error_payload,
    }


def serialize_response(response: dict[str, Any]) -> str:
    """Serialize the single response envelope with a bounded size guard."""
    serialized = json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(serialized.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise errors.bridge_error(
            errors.RESPONSE_TOO_LARGE,
            "Response envelope exceeds the 1 MiB stdout limit.",
            errors.EXIT_INTERNAL,
            retryable=False,
        )
    return serialized


__all__ = [
    "ALLOWED_COMMANDS",
    "BridgeRequest",
    "COMMAND_APPLY_FACT_SET",
    "COMMAND_APPLY_HUMAN_DRAFT_CARD",
    "COMMAND_AUTHORIZE_FINALIZATION",
    "COMMAND_CAPTURE",
    "COMMAND_BEGIN_HUMAN_DRAFT_CARD_DELIVERY",
    "COMMAND_CLAIM_AI_FALLBACK_INVOCATION",
    "COMMAND_CONFIRM",
    "COMMAND_EDIT",
    "COMMAND_FINALIZE",
    "COMMAND_GET_FINALIZATION_SNAPSHOT_REVIEW",
    "COMMAND_PREPARE_RECEIPT_COMPLETION",
    "COMMAND_GET_REVIEW",
    "COMMAND_GET_GUIDED_EDIT_SESSION",
    "COMMAND_GET_HUMAN_DRAFT_CARD",
    "COMMAND_GET_STATUS",
    "COMMAND_HEALTH",
    "COMMAND_ISSUE_HUMAN_ACTIONS",
    "COMMAND_PROPOSE",
    "COMMAND_PREPARE_AI_FALLBACK",
    "COMMAND_RECORD_AI_FALLBACK_RESULT",
    "COMMAND_REJECT",
    "COMMAND_RECORD_HUMAN_DRAFT_CARD_DELIVERY_OUTCOME",
    "COMMAND_REISSUE_HUMAN_DRAFT_CARD",
    "COMMAND_REDEEM_HUMAN_ACTION",
    "COMMAND_APPLY_GUIDED_EDIT_UPDATE",
    "COMMAND_COMPLETE_GUIDED_EDIT",
    "COMMAND_VERIFY_AI_MODEL_COMPATIBILITY_CASE_V2",
    "ENVELOPE_VERSION",
    "MAX_ENVELOPE_DEPTH",
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "MUTATING_COMMANDS",
    "check_depth",
    "error_response",
    "parse_request",
    "serialize_response",
    "success_response",
    "validate_idempotency_key",
]
