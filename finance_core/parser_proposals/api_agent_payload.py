from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

INTENT_TYPES = frozenset(
    {"expense", "receipt_split", "settlement", "income", "adjustment", "unknown"}
)
SOURCE_CHANNELS = frozenset({"telegram", "api", "cli", "web", "manual"})
SUGGESTED_NEXT_ACTIONS = frozenset({"confirm", "auto_finalize", "request_clarification", "reject"})
SPLIT_METHODS = frozenset({"equal", "weighted", "itemized", "percentage", "custom"})

_SOURCE_TYPE_CHANNEL_MAP = MappingProxyType(
    {
        "telegram_text": "telegram",
        "telegram_image": "telegram",
        "telegram_pdf": "telegram",
        "api_text": "api",
        "api_image": "api",
        "api_pdf": "api",
        "manual_entry": "manual",
        "cli_text": "cli",
        "web_text": "web",
    }
)

_PRESERVED_FIELDS = frozenset(
    {"raw_input", "attachment_path", "source_type", "source_channel", "external_message_id"}
)


class ApiAgentPayloadValidationError(ValueError):
    """Raised when an API Agent proposal payload fails validation."""


@dataclass(frozen=True)
class ValidatedPayload:
    """Immutable validated API Agent proposal payload.

    This is a lightweight boundary object that carries normalised validated
    fields alongside preserved source-evidence fields.  It does not create
    transactions, write to databases, run calculations, or auto-finalise
    anything.
    """

    intent_type: str
    source_type: str
    source_channel: str
    needs_confirmation: bool
    amount: Decimal | None = None
    currency: str | None = None
    merchant: str | None = None
    transaction_date: str | None = None
    paid_by: str | None = None
    participants: Any = None
    split_method: str | None = None
    category: str | None = None
    confidence: float | None = None
    evidence_refs: Any = None
    ambiguity_flags: Any = None
    suggested_next_action: str | None = None
    raw_input: str | None = None
    attachment_path: str | None = None
    external_message_id: str | None = None


def validate_api_agent_payload(payload: Mapping[str, Any]) -> ValidatedPayload:
    """Validate and normalise an API Agent proposal payload.

    Returns a ``ValidatedPayload`` when every check passes and raises
    ``ApiAgentPayloadValidationError`` for the first failure encountered.

    This validator is a pure-validation boundary.  It does **not**:

    * create transactions or write database records
    * run calculations or create calculation snapshots
    * create settlement obligations or reconciliation records
    * auto-confirm proposals or auto-finalise transactions

    ``suggested_next_action`` values such as ``auto_finalize`` are accepted
    only as suggested actions — the validator does not perform finalisation.
    """
    if not isinstance(payload, Mapping):
        raise ApiAgentPayloadValidationError("Payload must be a mapping")

    intent_type = _required_controlled(payload, "intent_type", INTENT_TYPES)
    source_type = _validate_source_type(payload)
    source_channel = _controlled_source_channel(payload)
    needs_confirmation = _required_bool(payload, "needs_confirmation")

    _validate_source_consistency(source_type, source_channel)

    raw_input = _preserved_text(payload, "raw_input")
    attachment_path = _preserved_text(payload, "attachment_path")
    if not raw_input and not attachment_path:
        raise ApiAgentPayloadValidationError(
            "At least one of raw_input or attachment_path is required"
        )

    amount = _optional_decimal(payload, "amount")
    currency = _optional_currency(payload, "currency")
    merchant = _preserved_text(payload, "merchant")
    transaction_date = _optional_iso_date(payload, "transaction_date")
    paid_by = _preserved_text(payload, "paid_by")
    participants = _optional_structured(payload, "participants")
    split_method = _optional_controlled(payload, "split_method", SPLIT_METHODS)
    category = _preserved_text(payload, "category")
    confidence = _optional_confidence(payload, "confidence")
    evidence_refs = _optional_structured(payload, "evidence_refs")
    ambiguity_flags = _optional_structured(payload, "ambiguity_flags")
    suggested_next_action = _optional_controlled(
        payload, "suggested_next_action", SUGGESTED_NEXT_ACTIONS
    )
    external_message_id = _preserved_text(payload, "external_message_id")

    return ValidatedPayload(
        intent_type=intent_type,
        source_type=source_type,
        source_channel=source_channel,
        needs_confirmation=needs_confirmation,
        amount=amount,
        currency=currency,
        merchant=merchant,
        transaction_date=transaction_date,
        paid_by=paid_by,
        participants=participants,
        split_method=split_method,
        category=category,
        confidence=confidence,
        evidence_refs=evidence_refs,
        ambiguity_flags=ambiguity_flags,
        suggested_next_action=suggested_next_action,
        raw_input=raw_input,
        attachment_path=attachment_path,
        external_message_id=external_message_id,
    )


# ------------------------------------------------------------------ private helpers


def _validate_source_type(payload: Mapping[str, Any]) -> str:
    value = payload.get("source_type")
    if not isinstance(value, str) or not value.strip():
        raise ApiAgentPayloadValidationError("Missing required field: source_type")
    source_type = value.strip()
    if source_type not in _SOURCE_TYPE_CHANNEL_MAP:
        raise ApiAgentPayloadValidationError(f"Unknown source_type: {source_type}")
    return source_type


def _validate_source_consistency(source_type: str, source_channel: str) -> None:
    expected = _SOURCE_TYPE_CHANNEL_MAP[source_type]
    if expected != source_channel:
        raise ApiAgentPayloadValidationError(
            f"source_type '{source_type}' does not match source_channel "
            f"'{source_channel}'; expected '{expected}'"
        )


def _required_controlled(payload: Mapping[str, Any], field: str, allowed: frozenset[str]) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ApiAgentPayloadValidationError(f"Missing required field: {field}")
    cleaned = value.strip()
    if cleaned not in allowed:
        raise ApiAgentPayloadValidationError(f"Invalid {field}: {cleaned}")
    return cleaned


def _controlled_source_channel(payload: Mapping[str, Any]) -> str:
    value = payload.get("source_channel")
    if not isinstance(value, str) or not value.strip():
        raise ApiAgentPayloadValidationError("Missing required field: source_channel")
    cleaned = value.strip().lower()
    if cleaned not in SOURCE_CHANNELS:
        raise ApiAgentPayloadValidationError(f"Invalid source_channel: {cleaned}")
    return cleaned


def _required_bool(payload: Mapping[str, Any], field: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise ApiAgentPayloadValidationError(f"Missing or non-boolean field: {field}")
    return value


def _preserved_text(payload: Mapping[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise ApiAgentPayloadValidationError(
        f"Field '{field}' must be a string or None, got {type(value).__name__}"
    )


def _optional_controlled(
    payload: Mapping[str, Any], field: str, allowed: frozenset[str]
) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    if cleaned not in allowed:
        raise ApiAgentPayloadValidationError(f"Invalid {field}: {cleaned}")
    return cleaned


def _optional_decimal(payload: Mapping[str, Any], field: str) -> Decimal | None:
    value = payload.get(field)
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ApiAgentPayloadValidationError(f"Invalid decimal field: {field}") from exc
    if not d.is_finite() or d < 0:
        raise ApiAgentPayloadValidationError(f"Invalid non-negative decimal field: {field}")
    return d


def _optional_currency(payload: Mapping[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    if ISO_CURRENCY_RE.fullmatch(cleaned) is None:
        raise ApiAgentPayloadValidationError(
            f"Invalid currency: {cleaned}; expected 3-letter uppercase ISO code"
        )
    return cleaned


def _optional_iso_date(payload: Mapping[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    if ISO_DATE_RE.fullmatch(cleaned) is None:
        raise ApiAgentPayloadValidationError(f"Invalid {field}: {cleaned}; expected YYYY-MM-DD")
    try:
        date.fromisoformat(cleaned)
    except ValueError as exc:
        raise ApiAgentPayloadValidationError(
            f"Invalid {field}: {cleaned}; expected valid YYYY-MM-DD date"
        ) from exc
    return cleaned


def _optional_confidence(payload: Mapping[str, Any], field: str) -> float | None:
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ApiAgentPayloadValidationError(f"{field} must be numeric, not boolean")
    if not isinstance(value, (int, float)):
        raise ApiAgentPayloadValidationError(f"{field} must be numeric if present")
    f = float(value)
    if f < 0.0 or f > 1.0:
        raise ApiAgentPayloadValidationError(f"{field} must be between 0.0 and 1.0; got {f}")
    return f


def _optional_structured(payload: Mapping[str, Any], field: str) -> Any:
    """Accept list or dict; reject plain strings for structured fields."""
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, str):
        raise ApiAgentPayloadValidationError(
            f"Field '{field}' must be a list or dict, not a plain string"
        )
    return value
