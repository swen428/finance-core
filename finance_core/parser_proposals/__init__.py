"""Compatibility exports, loaded only when explicitly requested.

Importing a neutral submodule must not eagerly import unrelated platform
adapters or authority services. Export names and resolved object identities
remain unchanged; no wrapper implementation replaces the underlying service.
"""

from importlib import import_module
from typing import Any

_LAZY_EXPORTS = {
    "ReceiptItemAllocationFactsCommand": (
        "finance_core.parser_proposals.receipt_item_allocation_facts",
        "ReceiptItemAllocationFactsCommand",
    ),
    "ReceiptItemAllocationFactsResult": (
        "finance_core.parser_proposals.receipt_item_allocation_facts",
        "ReceiptItemAllocationFactsResult",
    ),
    "ReceiptItemAllocationFactsSupersessionCommand": (
        "finance_core.parser_proposals.receipt_item_allocation_facts",
        "ReceiptItemAllocationFactsSupersessionCommand",
    ),
    "UnsupportedReceiptFactsMetadataError": (
        "finance_core.parser_proposals.receipt_facts_conversion",
        "UnsupportedReceiptFactsMetadataError",
    ),
    "complete_proposal": ("finance_core.parser_proposals.completion", "complete_proposal"),
    "confirm_proposal": ("finance_core.parser_proposals.confirmation", "confirm_proposal"),
    "convert_confirmed_proposal_to_transaction": (
        "finance_core.parser_proposals.conversion",
        "convert_confirmed_proposal_to_transaction",
    ),
    "convert_confirmed_receipt_proposal_to_facts": (
        "finance_core.parser_proposals.receipt_facts_conversion",
        "convert_confirmed_receipt_proposal_to_facts",
    ),
    "persist_receipt_item_allocation_facts": (
        "finance_core.parser_proposals.receipt_item_allocation_facts",
        "persist_receipt_item_allocation_facts",
    ),
    "reject_proposal": ("finance_core.parser_proposals.confirmation", "reject_proposal"),
    "resolve_effective_proposal_payload": (
        "finance_core.parser_proposals.completion",
        "resolve_effective_proposal_payload",
    ),
    "supersede_receipt_item_allocation_facts": (
        "finance_core.parser_proposals.receipt_item_allocation_facts",
        "supersede_receipt_item_allocation_facts",
    ),
    "supersede_receipt_total_proposal": (
        "finance_core.parser_proposals.receipt_supersession",
        "supersede_receipt_total_proposal",
    ),
    "verify_receipt_item_allocation_fact_set_for_review": (
        "finance_core.parser_proposals.receipt_item_allocation_facts",
        "verify_receipt_item_allocation_fact_set_for_review",
    ),
}

__all__ = [
    "ReceiptItemAllocationFactsCommand",
    "ReceiptItemAllocationFactsResult",
    "ReceiptItemAllocationFactsSupersessionCommand",
    "UnsupportedReceiptFactsMetadataError",
    "complete_proposal",
    "confirm_proposal",
    "convert_confirmed_proposal_to_transaction",
    "convert_confirmed_receipt_proposal_to_facts",
    "persist_receipt_item_allocation_facts",
    "reject_proposal",
    "resolve_effective_proposal_payload",
    "supersede_receipt_total_proposal",
    "supersede_receipt_item_allocation_facts",
    "verify_receipt_item_allocation_fact_set_for_review",
]


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
