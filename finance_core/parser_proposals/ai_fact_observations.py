"""Versioned model observations -> Python-owned internal ambiguity vocabulary.

Python resolves only layout-proven item/total comparisons; all other conflict
observations add restrictions. Callers must still validate values,
money pairs, confidence, source evidence and lifecycle before accepting a proposal.
"""

import re
from collections.abc import Mapping
from typing import Any

from finance_core.parser_proposals.ai_source_assessment import SourceAssessment

FACT_SCHEMA = "finance-ai-facts-v2"
FACT_FIELDS = ("amount", "currency", "transaction_date", "merchant")
_MISSING = dict(
    zip(
        FACT_FIELDS,
        ("missing_amount", "missing_currency", "missing_date", "missing_merchant_or_description"),
        strict=True,
    )
)
_AMBIGUOUS = dict(
    zip(
        FACT_FIELDS,
        ("ambiguous_amount", "ambiguous_currency", "ambiguous_date", "ambiguous_merchant"),
        strict=True,
    )
)


_CONFIDENCE_FIELDS = {*FACT_FIELDS, "description", "account", "category"}
_CANONICAL_BPS_DIGITS = re.compile(r"(?:0|[1-9][0-9]{0,3}|10000)")


def _decode_confidence_bps(value: Any) -> Any:
    """Lossless metadata decoding only; invalid values stay invalid downstream.

    No whitespace, signs, leading zeros, floats, exponents, percentages or
    Unicode digits. No money field uses this decoder; no source bytes mutate.
    """
    if isinstance(value, str) and _CANONICAL_BPS_DIGITS.fullmatch(value):
        return int(value)
    return value


def _money_value_refs(response: Mapping[str, Any]) -> frozenset[str]:
    """Read actual model value citations, never invent or borrow other refs."""
    evidence = response["field_evidence_refs"]
    if not isinstance(evidence, dict):
        return frozenset()
    combined: set[str] = set()
    for field in ("amount", "currency"):
        refs = evidence.get(field)
        if (
            not isinstance(refs, list)
            or not 1 <= len(refs) <= 16
            or any(not isinstance(ref, str) for ref in refs)
            or len(refs) != len(set(refs))
        ):
            return frozenset()
        combined.update(refs)
    return frozenset(combined)


def normalize_fact_observations(
    response: Mapping[str, Any],
    *,
    catalog: Mapping[str, str],
    assessment: SourceAssessment,
) -> dict[str, Any]:
    """Translate facts-v2 only; never silently accept model-authored flags."""
    expected = {
        "schema_version",
        "intent_type",
        *FACT_FIELDS,
        "description",
        "account",
        "category",
        "field_confidence_bps",
        "field_evidence_refs",
        "field_conflicts",
    }
    if set(response) != expected or response["schema_version"] != FACT_SCHEMA:
        raise ValueError("facts response schema is invalid")
    conflicts = response["field_conflicts"]
    if not isinstance(conflicts, dict) or not set(conflicts).issubset(FACT_FIELDS):
        raise ValueError("field_conflicts shape is invalid")
    flags: set[str] = set(assessment.ambiguity_flags)
    for field in FACT_FIELDS:
        # Omitted null observations equal the existing empty model list;
        # neither representation clears independently assessed source flags.
        if field not in conflicts and response[field] is not None:
            raise ValueError("present model field requires conflict observations")
        refs = conflicts.get(field, [])
        if (
            not isinstance(refs, list)
            or len(refs) > 16
            or any(not isinstance(ref, str) or ref not in catalog for ref in refs)
            or len(refs) != len(set(refs))
        ):
            raise ValueError("field_conflicts evidence is invalid")
        if refs:
            compared = set(refs)
            cited_items = tuple(
                group for group in assessment.proven_item_groups if group <= compared
            )
            covered = assessment.proven_total_refs.union(*cited_items)
            value_refs = _money_value_refs(response)
            proven_item_total_comparison = (
                field == "amount"
                and bool(cited_items)
                and bool(assessment.proven_total_refs)
                and value_refs == assessment.proven_total_refs
                and compared.union(value_refs) == covered
                and response["amount"] is not None
                and response["amount"] == assessment.parent_values.get("amount")
                and response["currency"] == assessment.parent_values.get("currency")
            )
            if proven_item_total_comparison:
                continue
            if response[field] is not None:
                raise ValueError("conflicting observed field must be null")
            flags.update({_AMBIGUOUS[field], "source_conflict"})
        if response[field] is None and assessment.parent_values.get(field) in (None, ""):
            flags.add(_MISSING[field])

    if response["intent_type"] == "unknown":
        flags.add("unsupported_intent")
    confidence = response["field_confidence_bps"]
    if isinstance(confidence, dict) and set(confidence) == _CONFIDENCE_FIELDS:
        confidence = {field: _decode_confidence_bps(value) for field, value in confidence.items()}
    return {
        **{key: value for key, value in response.items() if key != "field_conflicts"},
        "field_confidence_bps": confidence,
        "schema_version": "finance-ai-proposal-v1",
        "ambiguity_flags": sorted(flags),
    }
