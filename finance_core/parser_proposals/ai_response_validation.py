"""Pure response admission shared by the staging service and Gate 5.

Admission validates observations and evidence; it never authorizes confirmation,
creates a child, touches SQLite or verifies a runtime compatibility receipt.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date
from typing import Any

from finance_core.money import (
    SignPolicy,
    canonical_money_str,
    money_decimal,
    normalize_currency,
    validate_amount_for_currency,
)
from finance_core.parser_proposals.ai_fact_observations import normalize_fact_observations
from finance_core.parser_proposals.ai_source_assessment import (
    _money_pair_candidates,
    _ocr_field_evidence_refs,
    _payload_field,
    assess_source,
)
from finance_core.parser_proposals.receipt_total_parser import OcrLayoutContext

MAX_CHILD_RESPONSE_BYTES = 16_384
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
_FIELDS = (
    "amount",
    "currency",
    "transaction_date",
    "merchant",
    "description",
    "account",
    "category",
)


_RESPONSE_FIELDS = {
    "schema_version",
    "intent_type",
    "amount",
    "currency",
    "transaction_date",
    "merchant",
    "description",
    "account",
    "category",
    "field_confidence_bps",
    "ambiguity_flags",
    "field_evidence_refs",
}


_AMBIGUITY_FLAGS = frozenset(
    {
        "missing_amount",
        "missing_currency",
        "missing_date",
        "missing_merchant_or_description",
        "ambiguous_amount",
        "ambiguous_currency",
        "ambiguous_date",
        "ambiguous_merchant",
        "source_conflict",
        "unsupported_intent",
    }
)


def _field_value_supported_by_evidence(
    field: str,
    value: str,
    refs: list[str],
    catalog: Mapping[str, str],
) -> bool:
    evidence = "\n".join(catalog[ref] for ref in refs if ref in catalog)
    if field == "amount":
        return re.search(rf"(?<![0-9.]){re.escape(value)}(?![0-9.])", evidence) is not None
    if field == "transaction_date":
        return re.search(rf"(?<![0-9]){re.escape(value)}(?![0-9])", evidence) is not None
    if field == "currency":
        return re.search(rf"(?<![A-Za-z]){re.escape(value)}(?![A-Za-z])", evidence) is not None
    return (
        re.search(rf"(?<![\w]){re.escape(value)}(?![\w])", evidence, flags=re.UNICODE) is not None
    )


def _money_pair_supported_by_evidence(
    amount: str,
    currency: str,
    amount_refs: list[str],
    currency_refs: list[str],
    catalog: Mapping[str, str],
    *,
    source_kind: str = "telegram_text",
    parent_payload: Mapping[str, Any] | None = None,
) -> bool:
    """Require exactly one Finance-derived source money pair."""
    shared_refs = set(amount_refs) & set(currency_refs)
    matches = [
        candidate
        for candidate in _money_pair_candidates(
            catalog,
            source_kind=source_kind,
            parent_payload=parent_payload,
        )
        if shared_refs.intersection(candidate["refs"])
        and candidate["amount"] == amount
        and candidate["currency"] == currency
    ]
    return (
        len(matches) == 1
        and bool(amount_refs)
        and bool(currency_refs)
        and set(amount_refs).union(currency_refs).issubset(matches[0]["refs"])
    )


def _strict_json_object(response_bytes: bytes) -> dict[str, Any]:
    def pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        seen: set[str] = set()
        for key, value in pairs:
            if key in seen:
                raise ValueError("duplicate JSON key")
            seen.add(key)
            result[key] = value
        return result

    value = json.loads(response_bytes.decode("utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("response must be a JSON object")
    return value


def validate_ai_response(
    response_bytes: bytes,
    catalog_refs: set[str],
    *,
    parent_payload: Mapping[str, Any],
    catalog: Mapping[str, str],
    source_kind: str = "telegram_text",
    ocr_layout: OcrLayoutContext | None = None,
) -> dict[str, Any]:
    if len(response_bytes) > MAX_CHILD_RESPONSE_BYTES:
        raise ValueError("response is above child size")
    response = _strict_json_object(response_bytes)
    response = normalize_fact_observations(
        response,
        catalog=catalog,
        assessment=assess_source(
            catalog=catalog,
            parent_payload=parent_payload,
            source_kind=source_kind,
            ocr_layout=ocr_layout,
        ),
    )
    if set(response) != _RESPONSE_FIELDS:
        raise ValueError("response fields are not exact")
    if response["schema_version"] != "finance-ai-proposal-v1":
        raise ValueError("response schema_version is invalid")
    if response["intent_type"] not in {"personal_expense", "unknown"}:
        raise ValueError("response intent_type is invalid")
    for field in (
        "amount",
        "currency",
        "transaction_date",
        "merchant",
        "description",
        "account",
        "category",
    ):
        value = response[field]
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{field} must be string or null")
        if isinstance(value, str) and not value:
            raise ValueError(f"{field} must not be empty")
        if isinstance(value, str) and len(value.encode("utf-8")) > (
            128 if field == "category" else 1024
        ):
            raise ValueError(f"{field} is oversized")
    if (
        response["description"] is not None
        or response["account"] is not None
        or response["category"] is not None
    ):
        raise ValueError("unsupported destination field")
    if response["transaction_date"] is not None:
        date.fromisoformat(response["transaction_date"])
    flags = response["ambiguity_flags"]
    if (
        not isinstance(flags, list)
        or len(flags) > 10
        or any(flag not in _AMBIGUITY_FLAGS for flag in flags)
        or len(flags) != len(set(flags))
    ):
        raise ValueError("ambiguity_flags are invalid")
    if flags != sorted(flags):
        raise ValueError("ambiguity_flags must be lexicographically sorted")
    parent_date = _payload_field(parent_payload, "transaction_date")
    missing_date_expected = response["transaction_date"] is None and parent_date in (None, "")
    if ("missing_date" in flags) != missing_date_expected:
        raise ValueError("missing_date does not match source-supported transaction_date")
    if "source_conflict" in flags and not any(
        flag in flags
        for flag in {
            "ambiguous_amount",
            "ambiguous_currency",
            "ambiguous_date",
            "ambiguous_merchant",
        }
    ):
        raise ValueError("source_conflict requires a field-specific ambiguity")
    confidence = response["field_confidence_bps"]
    if not isinstance(confidence, dict) or set(confidence) != set(_FIELDS):
        raise ValueError("field_confidence_bps is invalid")
    for value in confidence.values():
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000
        ):
            raise ValueError("confidence must be integer basis points")
    refs = response["field_evidence_refs"]
    if not isinstance(refs, dict) or set(refs) != set(_FIELDS):
        raise ValueError("field_evidence_refs is invalid")
    for field in _FIELDS:
        field_refs = refs[field]
        if (
            not isinstance(field_refs, list)
            or len(field_refs) > 16
            or len(field_refs) != len(set(field_refs))
        ):
            raise ValueError("field evidence refs are invalid")
        if any(
            not isinstance(ref, str)
            or _SAFE_REF_RE.fullmatch(ref) is None
            or ref not in catalog_refs
            for ref in field_refs
        ):
            raise ValueError("field evidence ref is outside the projection")
        if response[field] is None and field_refs:
            raise ValueError("null fields cannot carry evidence refs")
        if response[field] is not None and not field_refs:
            raise ValueError("present fields require evidence refs")
    amount = response["amount"]
    currency = response["currency"]
    if (amount is None) != (currency is None):
        raise ValueError("amount and currency must be paired")
    if amount is not None:
        normalized_currency = normalize_currency(currency)
        if currency != normalized_currency:
            raise ValueError("currency is not canonical")
        normalized_amount = SignPolicy.STRICTLY_POSITIVE.enforce(  # type: ignore[attr-defined]
            validate_amount_for_currency(
                money_decimal(amount, label="AI amount"), normalized_currency, label="AI amount"
            ),
            label="AI amount",
        )
        if canonical_money_str(normalized_amount, normalized_currency) != amount:
            raise ValueError("amount is not canonical")
        if not _money_pair_supported_by_evidence(
            amount,
            normalized_currency,
            response["field_evidence_refs"]["amount"],
            response["field_evidence_refs"]["currency"],
            catalog,
            source_kind=source_kind,
            parent_payload=parent_payload,
        ):
            raise ValueError("amount and currency are not supported as one evidence pair")
    for field in ("amount", "currency", "transaction_date", "merchant"):
        model_value = response[field]
        parent_value = _payload_field(parent_payload, field)
        if model_value is not None and parent_value is not None and model_value != parent_value:
            raise ValueError(f"{field} conflicts with the deterministic parent value")
        if model_value is None:
            continue
        field_refs = response["field_evidence_refs"][field]
        if field in {"amount", "currency"}:
            # The unique source pair above validates every monetary ref, including
            # canonical values whose OCR spelling differs (for example S$).
            continue
        if source_kind == "receipt_local_ocr_text" and parent_value is not None:
            supported = set(field_refs).issubset(
                _ocr_field_evidence_refs(parent_payload, field, catalog)
            )
        else:
            supported = all(
                _field_value_supported_by_evidence(field, model_value, [ref], catalog)
                for ref in field_refs
            )
        if not supported:
            raise ValueError(f"{field} is not supported by its field evidence")
    if response["intent_type"] == "unknown":
        return response
    if response["merchant"] is not None and response["merchant"] != response["merchant"].strip():
        raise ValueError("merchant must not be normalized")
    return response
