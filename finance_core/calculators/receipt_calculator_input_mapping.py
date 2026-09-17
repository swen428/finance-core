"""Pure IAF fact-set -> calculator-input mapping (shared, SELECT-free).

The single implementation of the deterministic payload-to-calculator-input
mapping rules (design Section 16): approved allocation/adjustment methods,
included-member scoping, canonical monetary strings carried verbatim, and the
approved payer-following rounding policy.

Two boundaries need this mapping and must not disagree:

* the IAF.6 projection boundary, which produces the calculator input consumed
  by ``calculate_receipt_split``;
* the B4.2 readiness boundary, which must not report a receipt as
  calculator-ready when the deterministic calculator would reject its
  projection.

Having the mapping here keeps one implementation without the readiness
boundary importing the projection boundary (which imports readiness).  This
module performs no database access, no writes, and no monetary arithmetic.
"""

from __future__ import annotations

from typing import Any

from finance_core.parser_proposals.receipt_item_allocation_facts import (
    ADJUSTMENT_ALLOCATION_METHODS,
    ITEM_ALLOCATION_METHODS,
)

# The approved payer-following rounding policy (business rule); the
# calculator implements it as ``rounding_policy = "payer"``.
APPROVED_ROUNDING_POLICY = "payer"

# Approved IAF -> calculator method mapping (IA-D7). ``equal_amount`` maps
# onto the calculator's equal-among-consumers path; ``manual`` maps onto the
# calculator's manual item-allocation path.
ITEM_METHOD_TO_CALCULATOR = {
    "equal_amount": "equal",
    "manual": "manual",
}


class CalculatorInputMappingError(ValueError):
    """A persisted fact cannot map one-to-one onto the calculator input."""


def build_participant_list(included: set[str], payer_public_id: str) -> list[str]:
    """Included consumers plus the canonical payer, in deterministic order.

    The payer advanced the money and therefore always participates in the
    calculation even when excluded as a consumer.
    """
    participants = set(included)
    participants.add(payer_public_id)
    return sorted(participants)


def build_calculator_receipt(
    *,
    receipt_public_id: str,
    merchant: str,
    currency: str,
    payer_public_id: str,
    net_paid_text: str,
    payload: dict[str, Any],
    included: set[str],
) -> dict[str, Any]:
    """Map one active fact-set payload onto a calculator receipt entry."""
    return {
        "receipt_id": receipt_public_id,
        "merchant": merchant,
        "currency": currency,
        "paid_by": payer_public_id,
        "net_paid": net_paid_text,
        "rounding_policy": APPROVED_ROUNDING_POLICY,
        "items": project_items(payload, included),
        "adjustments": project_adjustments(payload, included, currency),
    }


def project_items(payload: dict[str, Any], included: set[str]) -> list[dict[str, Any]]:
    allocations_by_line: dict[int, dict[str, Any]] = {}
    for entry in payload["allocations"]:
        line = int(entry["line_number"])
        if line in allocations_by_line:
            raise CalculatorInputMappingError(
                f"Item line {line} carries more than one allocation entry"
            )
        allocations_by_line[line] = entry

    items: list[dict[str, Any]] = []
    for item in sorted(payload["items"], key=lambda entry: int(entry["line_number"])):
        line = int(item["line_number"])
        allocation = allocations_by_line.get(line)
        if allocation is None:
            raise CalculatorInputMappingError(f"Item line {line} has no allocation entry")
        method = allocation["allocation_method"]
        if method not in ITEM_ALLOCATION_METHODS:
            raise CalculatorInputMappingError(
                f"Item line {line} uses unsupported allocation method {method!r}"
            )
        projected: dict[str, Any] = {
            "line_number": line,
            "item_name": str(item["item_name"]),
            "amount": str(item["line_amount"]),
            "allocation_method": ITEM_METHOD_TO_CALCULATOR[method],
        }
        consumers = allocation_participants(allocation, included, f"item line {line}")
        if method == "equal_amount":
            projected["participants"] = consumers
        else:  # manual
            projected["allocations"] = manual_share_map(allocation, included, f"item line {line}")
        items.append(projected)
    return items


def project_adjustments(
    payload: dict[str, Any], included: set[str], currency: str
) -> list[dict[str, Any]]:
    adjustments: list[dict[str, Any]] = []
    for entry in sorted(payload["adjustments"], key=lambda a: int(a["adjustment_index"])):
        index = int(entry["adjustment_index"])
        method = entry["allocation_method"]
        if method not in ADJUSTMENT_ALLOCATION_METHODS:
            raise CalculatorInputMappingError(
                f"Adjustment {index} uses unsupported allocation method {method!r}"
            )
        projected: dict[str, Any] = {
            "adjustment_index": index,
            "type": str(entry["adjustment_type"]),
            "direction": str(entry["direction"]),
            "allocation_method": method,
            "amount": str(entry["amount"]),
            "currency": currency,
        }
        if method == "manual":
            projected["allocations"] = manual_share_map(entry, included, f"adjustment {index}")
        elif entry.get("participants") is not None:
            raise CalculatorInputMappingError(
                f"Adjustment {index}: per-participant shares are only meaningful "
                "for the manual method"
            )
        adjustments.append(projected)
    return adjustments


def allocation_participants(
    allocation: dict[str, Any], included: set[str], label: str
) -> list[str]:
    participants = allocation.get("participants")
    if not isinstance(participants, list) or not participants:
        raise CalculatorInputMappingError(f"{label}: allocation has no participants")
    result: list[str] = []
    seen: set[str] = set()
    for participant in participants:
        public_id = str(participant["participant_public_id"])
        if public_id in seen:
            raise CalculatorInputMappingError(f"{label}: participant {public_id!r} is duplicated")
        seen.add(public_id)
        if public_id not in included:
            raise CalculatorInputMappingError(
                f"{label}: participant {public_id!r} is not an included receipt member"
            )
        result.append(public_id)
    return result


def manual_share_map(entry: dict[str, Any], included: set[str], label: str) -> dict[str, str]:
    participants = entry.get("participants")
    if not isinstance(participants, list) or not participants:
        raise CalculatorInputMappingError(f"{label}: manual allocation has no participants")
    shares: dict[str, str] = {}
    for participant in participants:
        public_id = str(participant["participant_public_id"])
        if public_id in shares:
            raise CalculatorInputMappingError(f"{label}: participant {public_id!r} is duplicated")
        if public_id not in included:
            raise CalculatorInputMappingError(
                f"{label}: participant {public_id!r} is not an included receipt member"
            )
        share = participant.get("share_amount")
        if not isinstance(share, str) or not share:
            raise CalculatorInputMappingError(
                f"{label}: manual participant {public_id!r} is missing an explicit share amount"
            )
        shares[public_id] = share
    return shares


__all__ = [
    "APPROVED_ROUNDING_POLICY",
    "ITEM_METHOD_TO_CALCULATOR",
    "CalculatorInputMappingError",
    "allocation_participants",
    "build_calculator_receipt",
    "build_participant_list",
    "manual_share_map",
    "project_adjustments",
    "project_items",
]
