"""Parser proposal lifecycle helpers and authoritative UoW entry points."""

from finance_core.parser_proposals.completion import (
    complete_proposal,
    resolve_effective_proposal_payload,
)
from finance_core.parser_proposals.confirmation import confirm_proposal, reject_proposal
from finance_core.parser_proposals.conversion import convert_confirmed_proposal_to_transaction
from finance_core.parser_proposals.receipt_facts_conversion import (
    UnsupportedReceiptFactsMetadataError,
    convert_confirmed_receipt_proposal_to_facts,
)
from finance_core.parser_proposals.receipt_item_allocation_facts import (
    ReceiptItemAllocationFactsCommand,
    ReceiptItemAllocationFactsResult,
    ReceiptItemAllocationFactsSupersessionCommand,
    persist_receipt_item_allocation_facts,
    supersede_receipt_item_allocation_facts,
    verify_receipt_item_allocation_fact_set_for_review,
)
from finance_core.parser_proposals.receipt_supersession import supersede_receipt_total_proposal

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
