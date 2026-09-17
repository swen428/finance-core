"""Tests for finance_core.parser_proposals.api_agent_payload."""

from decimal import Decimal

import pytest

from finance_core.parser_proposals.api_agent_payload import (
    ApiAgentPayloadValidationError,
    ValidatedPayload,
    validate_api_agent_payload,
)

# ------------------------------------------------------------------ helpers


def _base_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "intent_type": "expense",
        "source_type": "api_text",
        "source_channel": "api",
        "needs_confirmation": True,
        "raw_input": "Lunch at Din Tai Fung $42.50",
        "amount": "42.50",
        "currency": "SGD",
        "merchant": "Din Tai Fung",
        "transaction_date": "2026-06-04",
        "confidence": 0.95,
        "evidence_refs": ["ref_001"],
        "participants": ["Alice", "Bob"],
        "ambiguity_flags": {"amount": "handwriting_unclear"},
        "suggested_next_action": "confirm",
        "external_message_id": "msg_abc123",
    }
    payload.update(overrides)
    return payload


# ----------------------------------------------------------------- 1 — valid API text expense


def test_valid_api_text_expense() -> None:
    payload = _base_payload()
    result = validate_api_agent_payload(payload)

    assert isinstance(result, ValidatedPayload)
    assert result.intent_type == "expense"
    assert result.source_type == "api_text"
    assert result.source_channel == "api"
    assert result.needs_confirmation is True
    assert result.amount == Decimal("42.50")
    assert result.currency == "SGD"
    assert result.merchant == "Din Tai Fung"
    assert result.transaction_date == "2026-06-04"
    assert result.confidence == 0.95
    assert result.raw_input == "Lunch at Din Tai Fung $42.50"
    assert result.evidence_refs == ["ref_001"]
    assert result.participants == ["Alice", "Bob"]
    assert isinstance(result.ambiguity_flags, dict)
    assert result.suggested_next_action == "confirm"
    assert result.external_message_id == "msg_abc123"


# --------------------------------------------------- 2 — valid Telegram shared expense


def test_valid_telegram_shared_expense() -> None:
    payload = _base_payload(
        intent_type="receipt_split",
        source_type="telegram_image",
        source_channel="telegram",
        split_method="equal",
        paid_by="Alice",
        participants=["Alice", "Bob", "Charlie"],
        amount="128.40",
        currency="USD",
        merchant=None,
    )
    result = validate_api_agent_payload(payload)

    assert result.intent_type == "receipt_split"
    assert result.source_type == "telegram_image"
    assert result.source_channel == "telegram"
    assert result.split_method == "equal"
    assert result.amount == Decimal("128.40")
    assert result.currency == "USD"
    assert result.participants == ["Alice", "Bob", "Charlie"]


# ------------------------------------------------- 3 — image-only (no raw_input)


def test_valid_image_only_attachment_no_raw_input() -> None:
    payload = _base_payload(raw_input=None, attachment_path="/receipts/img_001.jpg")
    result = validate_api_agent_payload(payload)

    assert result.raw_input is None
    assert result.attachment_path == "/receipts/img_001.jpg"


# --------------------------------------------------------- 4 — missing both evidence


def test_invalid_missing_both_raw_input_and_attachment_path() -> None:
    payload = _base_payload(raw_input=None, attachment_path=None)
    with pytest.raises(ApiAgentPayloadValidationError, match="raw_input or attachment_path"):
        validate_api_agent_payload(payload)


def test_invalid_empty_both_raw_input_and_attachment_path() -> None:
    payload = _base_payload(raw_input="", attachment_path="")
    with pytest.raises(ApiAgentPayloadValidationError, match="raw_input or attachment_path"):
        validate_api_agent_payload(payload)

    # With empty dict and missing attachment_path
    payload["attachment_path"] = None
    with pytest.raises(ApiAgentPayloadValidationError, match="raw_input or attachment_path"):
        validate_api_agent_payload(payload)


# -------------------------------------- 5 — source_type / source_channel mismatch


def test_invalid_source_type_channel_mismatch() -> None:
    payload = _base_payload(source_type="telegram_text", source_channel="api")
    with pytest.raises(ApiAgentPayloadValidationError, match="does not match source_channel"):
        validate_api_agent_payload(payload)


# ------------------------------------------------------- 6 — unknown source_type


def test_invalid_unknown_source_type() -> None:
    payload = _base_payload(
        source_type="imported_row",
        source_channel="api",
    )
    with pytest.raises(ApiAgentPayloadValidationError, match="Unknown source_type"):
        validate_api_agent_payload(payload)


# -------------------------------------------------- 7 — confidence below 0.0


def test_invalid_confidence_below_zero() -> None:
    payload = _base_payload(confidence=-0.1)
    with pytest.raises(ApiAgentPayloadValidationError, match="confidence must be between"):
        validate_api_agent_payload(payload)


# --------------------------------------------------- 8 — confidence above 1.0


def test_invalid_confidence_above_one() -> None:
    payload = _base_payload(confidence=1.5)
    with pytest.raises(ApiAgentPayloadValidationError, match="confidence must be between"):
        validate_api_agent_payload(payload)


# --------------------------------------------------------- 9 — negative amount


def test_invalid_negative_amount() -> None:
    payload = _base_payload(amount="-5.00")
    with pytest.raises(ApiAgentPayloadValidationError, match="non-negative decimal"):
        validate_api_agent_payload(payload)


# ------------------------------------------------------ 10 — non-decimal amount


def test_invalid_non_decimal_amount() -> None:
    payload = _base_payload(amount="not_a_number")
    with pytest.raises(ApiAgentPayloadValidationError, match="Invalid decimal field"):
        validate_api_agent_payload(payload)


# --------------------------------------------------- 11 — lowercase currency


def test_invalid_lowercase_currency() -> None:
    payload = _base_payload(currency="sgd")
    with pytest.raises(ApiAgentPayloadValidationError, match="Invalid currency"):
        validate_api_agent_payload(payload)


# --------------------------------------------------------- 12 — bad currency length


@pytest.mark.parametrize("bad_currency", ["SG", "SGDD", "S", "USDX"])
def test_invalid_currency_length(bad_currency: str) -> None:
    payload = _base_payload(currency=bad_currency)
    with pytest.raises(ApiAgentPayloadValidationError, match="Invalid currency"):
        validate_api_agent_payload(payload)


# ------------------------------------------- 13 — needs_confirmation missing


def test_invalid_needs_confirmation_missing() -> None:
    payload = _base_payload()
    del payload["needs_confirmation"]
    with pytest.raises(ApiAgentPayloadValidationError, match="non-boolean"):
        validate_api_agent_payload(payload)


# ------------------------------------------- 14 — needs_confirmation non-boolean


@pytest.mark.parametrize("bad_value", ["yes", 1, 0, None, "true"])
def test_invalid_needs_confirmation_non_boolean(bad_value: object) -> None:
    payload = _base_payload(needs_confirmation=bad_value)
    with pytest.raises(ApiAgentPayloadValidationError, match="non-boolean"):
        validate_api_agent_payload(payload)


# ---------------------------------------------- 15 — invalid suggested_next_action


def test_invalid_suggested_next_action() -> None:
    payload = _base_payload(suggested_next_action="auto_commit")
    with pytest.raises(ApiAgentPayloadValidationError, match="Invalid suggested_next_action"):
        validate_api_agent_payload(payload)


# ------------------------------------------------------ 16 — invalid split_method


def test_invalid_split_method() -> None:
    payload = _base_payload(split_method="random")
    with pytest.raises(ApiAgentPayloadValidationError, match="Invalid split_method"):
        validate_api_agent_payload(payload)


# ------------------------------- 17 — structured fields passed as plain strings


@pytest.mark.parametrize("field", ["evidence_refs", "participants", "ambiguity_flags"])
def test_invalid_structured_fields_as_strings(field: str) -> None:
    payload = _base_payload(**{field: "not_a_list"})
    with pytest.raises(ApiAgentPayloadValidationError, match="must be a list or dict"):
        validate_api_agent_payload(payload)


# ------------------ 18 — auto_finalize accepted as suggestion only (no finalisation)


def test_auto_finalize_accepted_only_as_suggestion() -> None:
    payload = _base_payload(suggested_next_action="auto_finalize")
    result = validate_api_agent_payload(payload)

    # Accepted as a suggested_next_action value.
    assert result.suggested_next_action == "auto_finalize"

    # The validator must not have performed any side-effects.  Since it is a
    # pure function with no DB or network access, this is true by construction,
    # but we assert explicitly to document the safety guarantee.
    assert result.needs_confirmation is True
    assert isinstance(result, ValidatedPayload)
    # No transaction IDs, DB handles, or API keys exist on the payload.
    assert not hasattr(result, "transaction_id")
    assert not hasattr(result, "db_connection")


# ------------------------------ 19 — raw_input & attachment_path preserved exactly


def test_raw_input_and_attachment_path_preserved_exactly() -> None:
    raw = "  Cash payment $10   \n"
    att = "  /tmp/scan.pdf  "
    payload = _base_payload(raw_input=raw, attachment_path=att)

    result = validate_api_agent_payload(payload)

    # Exact preservation — no trimming, no normalisation.
    assert result.raw_input == raw
    assert result.attachment_path == att


# ------------------------------------ 20 — external_message_id preserved exactly


def test_external_message_id_preserved_exactly() -> None:
    eid = "  ext-id-with-spaces  "
    payload = _base_payload(external_message_id=eid)
    result = validate_api_agent_payload(payload)
    assert result.external_message_id == eid


# ------------------------------------------------------- edge — unknown intent_type


def test_invalid_unknown_intent_type() -> None:
    payload = _base_payload(intent_type="refund")
    with pytest.raises(ApiAgentPayloadValidationError, match="Invalid intent_type"):
        validate_api_agent_payload(payload)


# --------------------------------------------------- edge — non-mapping payload


def test_invalid_non_mapping_payload() -> None:
    with pytest.raises(ApiAgentPayloadValidationError, match="must be a mapping"):
        validate_api_agent_payload("not a mapping")  # type: ignore[arg-type]


# ------------------------------------------------ edge — missing required fields


@pytest.mark.parametrize("drop_field", ["intent_type", "source_type", "source_channel"])
def test_invalid_missing_required_fields(drop_field: str) -> None:
    payload = _base_payload()
    del payload[drop_field]
    with pytest.raises(ApiAgentPayloadValidationError):
        validate_api_agent_payload(payload)


# ----------------------------------- edge — invalid transaction_date format


def test_invalid_transaction_date_format() -> None:
    payload = _base_payload(transaction_date="04-06-2026")
    with pytest.raises(ApiAgentPayloadValidationError, match="Invalid transaction_date"):
        validate_api_agent_payload(payload)


def test_invalid_transaction_date_calendar_value() -> None:
    payload = _base_payload(transaction_date="2026-02-30")
    with pytest.raises(ApiAgentPayloadValidationError, match="Invalid transaction_date"):
        validate_api_agent_payload(payload)


# --------------------------------------- edge — confidence bool rejected


def test_invalid_confidence_boolean() -> None:
    payload = _base_payload(confidence=True)
    with pytest.raises(ApiAgentPayloadValidationError, match="must be numeric, not boolean"):
        validate_api_agent_payload(payload)
