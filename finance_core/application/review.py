"""Internal, transaction-neutral financial proposal review application.

A review is a projection, never a posting or confirmation capability. The
prepare/project seam preserves existing adapter validation order around its
own token issuance; direct callers use get_proposal_review without a host,
workspace, clock or signing key. The caller supplies the existing migrated
Finance connection with sqlite3.Row row_factory. No method commits or changes
stored state.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from finance_core.intake.receipt_ocr_proposal import get_receipt_ocr_extraction_status_for_proposal
from finance_core.parser_proposals.ai_fallback import (
    AiFallbackServiceError,
    requires_deterministic_intent_policy,
    verify_ai_fallback_child,
    verify_deterministic_intent_policy,
)
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.conversion_state import has_receipt_ocr_proposal_link
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.receipt_total_parser import RECEIPT_AMBIGUITY_FLAGS
from finance_core.parser_proposals.repository import ParserProposalRepository

MAX_REVIEW_FIELD_LENGTH = 1_024
_MAX_FIELD_VALUE_LENGTH = MAX_REVIEW_FIELD_LENGTH
_MONETARY_REVIEW_FIELDS = frozenset({"amount", "currency", "transaction_date"})
_DISPLAY_TEXT_REVIEW_FIELDS = frozenset({"merchant", "description"})
_HASH_BOUND_STRING_REVIEW_FIELDS = _MONETARY_REVIEW_FIELDS | _DISPLAY_TEXT_REVIEW_FIELDS


class ReviewError(ValueError):
    """A financial review cannot be truthfully produced."""


class ReviewNotFoundError(ReviewError):
    """The requested proposal does not exist."""


class ReviewUnavailableError(ReviewError):
    """Persisted review material is unavailable or cannot be represented safely."""


@dataclass(frozen=True)
class PreparedProposalReview:
    """Internal read snapshot; not an authorization or durable capability."""

    proposal: dict[str, Any]
    payload: dict[str, Any]
    version: int
    content_hash: str
    ai_lineage: dict[str, Any] | None
    account: str | None
    account_status: str
    account_truncated: bool
    classification: str
    classification_unknown: bool
    transaction_date: object
    parse_status: str


def read_proposal(conn: sqlite3.Connection, proposal_public_id: str) -> dict[str, Any]:
    proposal = ParserProposalRepository(conn).get_by_public_id(proposal_public_id)
    if proposal is None:
        raise ReviewNotFoundError("Proposal public ID was not found in the staging database.")
    return proposal


def effective_state(
    conn: sqlite3.Connection, proposal: dict[str, Any]
) -> tuple[dict[str, Any], int, str]:
    effective_payload, _completion_id, version = resolve_effective_payload(conn, proposal)
    content_hash = compute_effective_proposal_content_hash(conn, {"id": proposal["id"]})
    return effective_payload, version, content_hash


def review_ambiguity_indicators(
    conn: sqlite3.Connection,
    proposal: dict[str, Any],
    payload: dict[str, Any],
) -> list[str]:
    indicators: list[str] = []

    def _present(value: object) -> bool:
        return (
            isinstance(value, str)
            and bool(value.strip())
            or (value is not None and not isinstance(value, str))
        )

    if not _present(payload.get("amount")):
        indicators.append("missing_amount")
    if not _present(payload.get("currency")):
        indicators.append("missing_currency")
    if not _present(payload.get("transaction_date", payload.get("date"))):
        indicators.append("missing_date")
    if not _present(payload.get("merchant")) and not _present(payload.get("description")):
        indicators.append("missing_merchant_or_description")
    confidence = proposal.get("confidence_score")
    if confidence is None or float(confidence) < 0.8:
        indicators.append("low_confidence")
    if has_receipt_ocr_proposal_link(conn, int(proposal["id"])):
        receipt_flags = payload.get("ambiguity_flags")
        if (
            not isinstance(receipt_flags, list)
            or len(receipt_flags) > len(RECEIPT_AMBIGUITY_FLAGS)
            or any(
                not isinstance(flag, str) or flag not in RECEIPT_AMBIGUITY_FLAGS
                for flag in receipt_flags
            )
            or len(receipt_flags) != len(set(receipt_flags))
        ):
            raise ReviewUnavailableError(
                "Receipt review requires a bounded, recognized ambiguity flag set."
            )
        indicators.extend(receipt_flags)
        extraction_status = get_receipt_ocr_extraction_status_for_proposal(
            conn, int(proposal["id"])
        )
        if extraction_status is not None:
            if extraction_status == "no_text":
                indicators.append("ocr_no_text")
            elif extraction_status != "succeeded":
                indicators.append("ocr_partial_text")
    return sorted(set(indicators))


def _participants_present(payload: dict[str, Any]) -> bool:
    participants = payload.get("participants")
    if not isinstance(participants, (list, tuple)):
        return False
    return any(_value_present(entry) for entry in participants)


def _split_semantics_present(payload: dict[str, Any]) -> bool:
    if payload.get("split") is True:
        return True
    return _value_present(payload.get("split_type"))


def classify_payload(payload: dict[str, Any]) -> tuple[str, bool]:
    """Return ``(classification, unknown)`` using the parser's controlled semantics.

    The authoritative signal is the parser-controlled ``transaction_type``
    plus the parser's participants/split semantics.  Payer/paid_by presence
    is never used as a personal/shared guess.  Insufficient or conflicting
    signals yield ``unknown`` (fail-closed) with an ambiguity indicator.
    """
    transaction_type = payload.get("transaction_type")
    participants = _participants_present(payload)
    split = _split_semantics_present(payload)
    if transaction_type == "shared_expense":
        return "shared", False
    if transaction_type == "personal_expense":
        if participants or split:
            # Conflicting authoritative signals: refuse to guess.
            return "unknown", True
        return "personal", False
    if participants or split:
        return "shared", False
    return "unknown", True


def _value_present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _bounded_review_scalar(field: str, value: object) -> tuple[str | None, bool]:
    """Return ``(display_value, truncated)`` for one review-card field.

    Monetary-critical fields are never truncated: an oversized persisted
    value would make the displayed card disagree with the full content hash
    the operator's decision binds, so the card is refused fail-closed.
    Display-only fields truncate with a bounded ambiguity indicator.
    """
    if value is None:
        return None, False
    if field in _HASH_BOUND_STRING_REVIEW_FIELDS and not isinstance(value, str):
        raise ReviewUnavailableError(
            f"Review card requires string-valued '{field}' content; refusing to coerce "
            "financial truth."
        )
    if field in _HASH_BOUND_STRING_REVIEW_FIELDS and isinstance(value, str) and not value.strip():
        raise ReviewUnavailableError(
            f"Review card refuses blank '{field}' content because it cannot "
            "display the hash-bound value truthfully."
        )
    if isinstance(value, bool):
        return None, False
    text = str(value)
    if len(text) <= _MAX_FIELD_VALUE_LENGTH:
        return text, False
    if field in _MONETARY_REVIEW_FIELDS:
        raise ReviewUnavailableError(
            f"Review card cannot represent oversized '{field}' content without misleading "
            "the operator; refusing to emit the card."
        )
    return text[:_MAX_FIELD_VALUE_LENGTH], True


def prepare_proposal_review(
    conn: sqlite3.Connection, proposal_public_id: str
) -> PreparedProposalReview:
    proposal = read_proposal(conn, proposal_public_id)
    payload, version, content_hash = effective_state(conn, proposal)
    try:
        ai_lineage = verify_ai_fallback_child(
            conn,
            proposal,
            content_hash=content_hash,
            proposal_version=version,
            require_resolved=False,
        )
        if ai_lineage is None and requires_deterministic_intent_policy(proposal):
            verify_deterministic_intent_policy(proposal)
    except AiFallbackServiceError as exc:
        raise ReviewUnavailableError(str(exc)) from exc

    account_value = payload.get("account", payload.get("account_id"))
    if isinstance(account_value, str) and not account_value.strip():
        raise ReviewUnavailableError(
            "Review card refuses a blank account because it cannot display the "
            "hash-bound value truthfully."
        )
    account_status = "present" if _value_present(account_value) else "absent"
    account: str | None = None
    account_truncated = False
    if account_status == "present":
        if not isinstance(account_value, str):
            raise ReviewUnavailableError(
                "Review card requires a string-valued account; refusing to coerce "
                "hash-bound financial intent."
            )
        account, account_truncated = _bounded_review_scalar("account", account_value)
    classification, classification_unknown = classify_payload(payload)

    transaction_date = payload.get("transaction_date", payload.get("date"))

    return PreparedProposalReview(
        proposal=proposal,
        payload=payload,
        version=version,
        content_hash=content_hash,
        ai_lineage=ai_lineage,
        account=account,
        account_status=account_status,
        account_truncated=account_truncated,
        classification=classification,
        classification_unknown=classification_unknown,
        transaction_date=transaction_date,
        parse_status=str(proposal["parse_status"]),
    )


def project_proposal_review(
    conn: sqlite3.Connection, prepared: PreparedProposalReview
) -> dict[str, Any]:
    proposal, payload = prepared.proposal, prepared.payload
    version, content_hash = prepared.version, prepared.content_hash
    ai_lineage = prepared.ai_lineage
    account, account_status = prepared.account, prepared.account_status
    account_truncated = prepared.account_truncated
    classification = prepared.classification
    classification_unknown = prepared.classification_unknown
    transaction_date, parse_status = prepared.transaction_date, prepared.parse_status
    merchant, merchant_truncated = _bounded_review_scalar("merchant", payload.get("merchant"))
    description, description_truncated = _bounded_review_scalar(
        "description", payload.get("description")
    )
    category, category_truncated = _bounded_review_scalar("category", payload.get("category"))
    ambiguity_indicators = review_ambiguity_indicators(conn, proposal, payload)
    if merchant_truncated or description_truncated or category_truncated or account_truncated:
        ambiguity_indicators = sorted([*ambiguity_indicators, "oversized_display_field"])
    if classification_unknown:
        ambiguity_indicators = sorted([*ambiguity_indicators, "unknown_classification"])
    ai_origin = ai_lineage is not None
    if ai_lineage is not None:
        ambiguity_indicators = sorted(
            set(ambiguity_indicators) | set(ai_lineage["ambiguity_flags"])
        )
        if ai_lineage["requires_resolution"]:
            ambiguity_indicators = sorted(set(ambiguity_indicators) | {"low_confidence"})

    return {
        "proposal_public_id": proposal["public_id"],
        "intake_public_id": proposal["source_public_id"],
        "parse_status": parse_status,
        "proposal_version": version,
        "effective_content_hash": content_hash,
        "amount": _bounded_review_scalar("amount", payload.get("amount"))[0],
        "currency": _bounded_review_scalar("currency", payload.get("currency"))[0],
        "transaction_date": _bounded_review_scalar("transaction_date", transaction_date)[0],
        "merchant": merchant,
        "description": description,
        "category": category,
        "account": account,
        "account_status": account_status,
        "classification": classification,
        "source_type": proposal["source_type"],
        "ambiguity_indicators": ambiguity_indicators,
        "proposal_origin": "ai_fallback" if ai_origin else "deterministic",
        "ai_source_kind": None if ai_lineage is None else ai_lineage["ai_source_kind"],
        "confirm_available": not ai_origin or not ambiguity_indicators,
        "final_transaction_created": False,
    }


def get_proposal_review(conn: sqlite3.Connection, proposal_public_id: str) -> dict[str, Any]:
    """Read and validate review content without any platform interaction."""
    return project_proposal_review(conn, prepare_proposal_review(conn, proposal_public_id))
