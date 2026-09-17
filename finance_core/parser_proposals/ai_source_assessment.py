"""Pure source-specific evidence assessment shared by proposals and Gate 5.

Only the persisted OCR total parser evidence defines OCR monetary candidates.
Text candidates retain the existing text parser's currency and span semantics.
No database, provider, clock or asset-registry access belongs in this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from finance_core.money import (
    MoneyValidationError,
    SignPolicy,
    canonical_money_str,
    money_decimal,
    normalize_currency,
    validate_amount_for_currency,
)
from finance_core.parser_proposals.ai_fallback_provenance import canonical_json_bytes
from finance_core.parser_proposals.receipt_total_parser import (
    OcrLayoutContext,
    explicit_item_line_groups,
    parse_receipt_total,
    proven_layout_line_indexes,
)
from finance_core.parsers.text_expense_parser import find_amount_candidates

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
FACT_FIELDS = ("amount", "currency", "transaction_date", "merchant")
_PARENT_FLAGS = {
    "conflicting_total_candidates": "ambiguous_amount",
    "ambiguous_currency_symbol": "ambiguous_currency",
    "unsupported_currency_for_amount": "ambiguous_currency",
    "ambiguous_transaction_date": "ambiguous_date",
    "conflicting_date_candidates": "ambiguous_date",
}


def _hash_material(domain: str, material: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        domain.encode("ascii") + b"\x00" + canonical_json_bytes(material)
    ).hexdigest()


def _parse_ocr_evidence_reference(
    reference_json: Any,
) -> tuple[set[int], str, str] | None:
    try:
        reference = json.loads(str(reference_json))
        indexes = reference["block_sequence_indexes"]
        if (
            not isinstance(indexes, list)
            or not indexes
            or any(
                not isinstance(index, int) or isinstance(index, bool) or index < 0
                for index in indexes
            )
            or len(indexes) != len(set(indexes))
        ):
            return None
        extraction_public_id = reference["extraction_public_id"]
        normalized_result_hash = reference["normalized_result_hash"]
        if (
            not isinstance(extraction_public_id, str)
            or not extraction_public_id
            or not isinstance(normalized_result_hash, str)
            or not _HASH_RE.fullmatch(normalized_result_hash)
        ):
            return None
        return set(indexes), extraction_public_id, normalized_result_hash
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _ocr_evidence_material(row: Mapping[str, Any]) -> tuple[set[int], str, str] | None:
    reference = row.get("evidence_reference")
    if reference is not None:
        return _parse_ocr_evidence_reference(reference)
    indexes = row.get("block_sequence_indexes")
    extraction_public_id = row.get("extraction_public_id")
    normalized_result_hash = row.get("normalized_result_hash")
    if (
        not isinstance(indexes, list)
        or not indexes
        or any(
            not isinstance(index, int) or isinstance(index, bool) or index < 0 for index in indexes
        )
        or len(indexes) != len(set(indexes))
        or not isinstance(extraction_public_id, str)
        or not extraction_public_id
        or not isinstance(normalized_result_hash, str)
        or not _HASH_RE.fullmatch(normalized_result_hash)
    ):
        return None
    return set(indexes), extraction_public_id, normalized_result_hash


def _payload_field(payload: Mapping[str, Any], field: str) -> Any:
    if field == "transaction_date":
        return payload.get("transaction_date", payload.get("date"))
    return payload.get(field)


def _ocr_field_evidence_refs(
    parent_payload: Mapping[str, Any], field: str, catalog: Mapping[str, str]
) -> frozenset[str]:
    """Bind canonical parent fields to their sealed OCR blocks, not raw spelling."""
    evidence = parent_payload.get("field_evidence")
    ocr = parent_payload.get("ocr_evidence")
    if not isinstance(evidence, list) or not isinstance(ocr, Mapping):
        return frozenset()
    rows = [row for row in evidence if isinstance(row, Mapping) and row.get("field_name") == field]
    if len(rows) != 1:
        return frozenset()
    row = rows[0]
    if row.get("evidence_source_type") != "ocr" or row.get("proposed_value") != _payload_field(
        parent_payload, field
    ):
        return frozenset()
    material = _ocr_evidence_material(row)
    if material is None:
        return frozenset()
    indexes, extraction, normalized_hash = material
    if extraction != ocr.get("extraction_public_id") or normalized_hash != ocr.get(
        "normalized_result_hash"
    ):
        return frozenset()
    refs = frozenset(f"e{index + 1:04d}" for index in indexes)
    return refs if refs.issubset(catalog) else frozenset()


def _money_pair_candidates(
    catalog: Mapping[str, str],
    *,
    source_kind: str = "telegram_text",
    parent_payload: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Reuse the source-specific deterministic parser's canonical candidates."""
    if source_kind == "receipt_local_ocr_text":
        return _ocr_money_pair_candidates(catalog, parent_payload)

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int, str, str]] = set()
    for ref, text in catalog.items():
        for candidate in find_amount_candidates(text):
            if candidate.currency is None:
                continue
            try:
                currency = normalize_currency(candidate.currency)
                amount_decimal = SignPolicy.STRICTLY_POSITIVE.enforce(  # type: ignore[attr-defined]
                    validate_amount_for_currency(
                        candidate.amount,
                        currency,
                        label="source money pair",
                    ),
                    label="source money pair",
                )
            except MoneyValidationError:
                continue
            amount = canonical_money_str(amount_decimal, currency)
            key = (ref, candidate.start, candidate.end, amount, currency)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "ref": ref,
                    "refs": [ref],
                    "start": candidate.start,
                    "end": candidate.end,
                    "source_span": [candidate.start, candidate.end],
                    "amount": amount,
                    "currency": currency,
                    "pair_identity": _hash_material(
                        "finance-ai-money-pair-v1",
                        {
                            "amount": amount,
                            "currency": currency,
                            "source_ref": ref,
                            "source_span": [candidate.start, candidate.end],
                        },
                    ),
                }
            )
    return candidates


def _ocr_money_pair_candidates(
    catalog: Mapping[str, str],
    parent_payload: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Project the persisted OCR parser's pair evidence into catalog references."""
    if parent_payload is None:
        return []
    evidence = parent_payload.get("field_evidence")
    if not isinstance(evidence, list):
        return []
    by_field: dict[str, list[Mapping[str, Any]]] = {"amount": [], "currency": []}
    for row in evidence:
        if not isinstance(row, Mapping):
            continue
        field = row.get("field_name")
        if field in by_field:
            by_field[str(field)].append(row)
    if len(by_field["amount"]) != 1 or len(by_field["currency"]) != 1:
        return []

    amount = _payload_field(parent_payload, "amount")
    currency = _payload_field(parent_payload, "currency")
    if not isinstance(amount, str) or not isinstance(currency, str):
        return []
    ocr_evidence = parent_payload.get("ocr_evidence")
    if not isinstance(ocr_evidence, Mapping):
        return []
    extraction_public_id = ocr_evidence.get("extraction_public_id")
    normalized_result_hash = ocr_evidence.get("normalized_result_hash")
    if (
        not isinstance(extraction_public_id, str)
        or not isinstance(normalized_result_hash, str)
        or not _HASH_RE.fullmatch(normalized_result_hash)
    ):
        return []
    try:
        normalized_currency = normalize_currency(currency)
        normalized_amount = SignPolicy.STRICTLY_POSITIVE.enforce(  # type: ignore[attr-defined]
            validate_amount_for_currency(
                money_decimal(amount, label="OCR source money pair"),
                normalized_currency,
                label="OCR source money pair",
            ),
            label="OCR source money pair",
        )
    except MoneyValidationError:
        return []
    canonical_amount = canonical_money_str(normalized_amount, normalized_currency)
    if canonical_amount != amount or normalized_currency != currency:
        return []

    def evidence_material(
        row: Mapping[str, Any],
        *,
        expected_value: str,
    ) -> tuple[set[int], str, str] | None:
        if row.get("evidence_source_type") != "ocr" or row.get("proposed_value") != expected_value:
            return None
        return _ocr_evidence_material(row)

    amount_material = evidence_material(by_field["amount"][0], expected_value=amount)
    currency_material = evidence_material(by_field["currency"][0], expected_value=currency)
    if amount_material is None or currency_material is None:
        return []
    amount_indexes, amount_extraction, amount_hash = amount_material
    currency_indexes, currency_extraction, currency_hash = currency_material
    if (
        amount_extraction != extraction_public_id
        or currency_extraction != extraction_public_id
        or amount_hash != normalized_result_hash
        or currency_hash != normalized_result_hash
    ):
        return []
    shared_indexes = amount_indexes.intersection(currency_indexes)
    refs = [
        f"e{index + 1:04d}" for index in sorted(shared_indexes) if f"e{index + 1:04d}" in catalog
    ]
    if not refs or len(refs) != len(shared_indexes):
        return []
    source_span = [min(shared_indexes), max(shared_indexes) + 1]
    return [
        {
            "ref": refs[0],
            "refs": refs,
            "start": source_span[0],
            "end": source_span[1],
            "source_span": source_span,
            "amount": canonical_amount,
            "currency": normalized_currency,
            "pair_identity": _hash_material(
                "finance-ai-ocr-money-pair-v1",
                {
                    "amount": canonical_amount,
                    "currency": normalized_currency,
                    "source_refs": refs,
                    "source_span": source_span,
                    "extraction_public_id": extraction_public_id,
                    "normalized_result_hash": normalized_result_hash,
                },
            ),
        }
    ]


@dataclass(frozen=True)
class SourceAssessment:
    """Finance-derived restrictions, never model-authored source authority."""

    parent_values: Mapping[str, Any]
    ambiguity_flags: tuple[str, ...]
    proven_item_groups: tuple[frozenset[str], ...] = ()
    proven_total_refs: frozenset[str] = frozenset()


def assess_source(
    *,
    catalog: Mapping[str, str],
    parent_payload: Mapping[str, Any],
    source_kind: str,
    ocr_layout: OcrLayoutContext | None = None,
) -> SourceAssessment:
    if source_kind not in {"telegram_text", "telegram_raw_text", "receipt_local_ocr_text"}:
        raise ValueError("source kind is unsupported")
    flags: set[str] = set()
    parent_flags = parent_payload.get("ambiguity_flags", [])
    if not isinstance(parent_flags, (list, tuple)) or any(
        not isinstance(flag, str) for flag in parent_flags
    ):
        raise ValueError("parent ambiguity flags are invalid")
    for flag in parent_flags:
        mapped = _PARENT_FLAGS.get(flag)
        if mapped is not None:
            flags.update({mapped, "source_conflict"})
        elif flag in {
            "ambiguous_amount",
            "ambiguous_currency",
            "ambiguous_date",
            "ambiguous_merchant",
        }:
            flags.update({flag, "source_conflict"})
    if source_kind in {"telegram_text", "telegram_raw_text"}:
        pairs = _money_pair_candidates(catalog, source_kind=source_kind)
        if len({(pair["amount"], pair["currency"]) for pair in pairs}) > 1:
            # Preserve the existing text eligibility rule for competing pairs.
            flags.update({"ambiguous_amount", "source_conflict"})
        if len({pair["currency"] for pair in pairs}) > 1:
            flags.update({"ambiguous_currency", "source_conflict"})
        # Bounded explicit ISO date observations apply only to text. OCR dates
        # are governed by the persisted source parser's date and conflict flags.
        dates = set()
        for text in catalog.values():
            for value in re.findall(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", text):
                try:
                    dates.add(date.fromisoformat(value))
                except ValueError:
                    continue
        if len(dates) > 1:
            flags.update({"ambiguous_date", "source_conflict"})
    item_groups: tuple[frozenset[str], ...] = ()
    total_refs: frozenset[str] = frozenset()
    if source_kind == "receipt_local_ocr_text" and ocr_layout is not None and not flags:
        ocr = parent_payload.get("ocr_evidence", {})
        layout_catalog = {f"e{b.sequence_index + 1:04d}": b.text for b in ocr_layout.blocks}
        safe_missing = {"transaction_date_not_found", "merchant_not_determined"}
        if (
            isinstance(ocr, Mapping)
            and ocr_layout.extraction_public_id == ocr.get("extraction_public_id")
            and ocr_layout.normalized_result_hash == ocr.get("normalized_result_hash")
            and layout_catalog == catalog
            and set(parent_flags).issubset(safe_missing)
        ):
            parsed = parse_receipt_total(ocr_layout.blocks, extraction_status="succeeded")
            pairs = _ocr_money_pair_candidates(catalog, parent_payload)
            if (
                len(pairs) == 1
                and parsed.amount.value == pairs[0]["amount"]
                and parsed.currency.value == pairs[0]["currency"]
                and set(parsed.ambiguity_flags).issubset(safe_missing)
                and set(pairs[0]["refs"])
                == {f"e{i + 1:04d}" for i in parsed.amount.block_sequence_indexes}
                == {f"e{i + 1:04d}" for i in parsed.currency.block_sequence_indexes}
            ):
                total_refs = frozenset(pairs[0]["refs"])
                if frozenset(
                    parsed.amount.block_sequence_indexes
                ) not in proven_layout_line_indexes(ocr_layout.blocks):
                    total_refs = frozenset()
                else:
                    item_groups = tuple(
                        frozenset(f"e{i + 1:04d}" for i in group)
                        for group in explicit_item_line_groups(
                            ocr_layout.blocks, currency=pairs[0]["currency"]
                        )
                        if not total_refs.intersection(f"e{i + 1:04d}" for i in group)
                    )

    return SourceAssessment(
        parent_values={field: _payload_field(parent_payload, field) for field in FACT_FIELDS},
        ambiguity_flags=tuple(sorted(flags)),
        proven_item_groups=item_groups,
        proven_total_refs=total_refs,
    )
