"""Read a completed receipt's missing payment fields and current account choices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from finance_core.money import SUPPORTED_CURRENCIES

from .daily_v1._impacts_v1 import account_inventory
from .daily_v1.contracts import AccountDefinitionRefV1, BundleFactRefV1, account_ref
from .daily_v1.reader import DailyReadIdentityV1
from .receipt_draft_v1 import ReceiptManualDraftV1, _receipt_amount_is_valid


@dataclass(frozen=True)
class ReceiptPaymentAccountOptionV1:
    account_ref: AccountDefinitionRefV1
    bundle_ref: BundleFactRefV1
    name: str
    account_type: str
    currency: str


@dataclass(frozen=True)
class ReceiptPaymentHandoffV1:
    draft: ReceiptManualDraftV1
    read_identity: DailyReadIdentityV1
    account_options: tuple[ReceiptPaymentAccountOptionV1, ...]
    state: Literal[
        "ACCOUNT_REQUIRED",
        "NO_ACCOUNT_OPTIONS",
        "UNSUPPORTED_RECEIPT_CURRENCY",
        "INVALID_RECEIPT_AMOUNT",
    ]
    missing_payment_fields: tuple[str, ...] = ("account_id", "account_amount")


def _receipt_payment_handoff(conn, draft, read_identity):
    """Project payment choices after the caller authenticates this exact ledger snapshot."""
    fields = {field.name: field.value for field in draft.fields}
    receipt_currency = fields["currency"]
    if receipt_currency not in SUPPORTED_CURRENCIES:
        return ReceiptPaymentHandoffV1(draft, read_identity, (), "UNSUPPORTED_RECEIPT_CURRENCY")
    if not _receipt_amount_is_valid(fields["amount"], receipt_currency):
        return ReceiptPaymentHandoffV1(draft, read_identity, (), "INVALID_RECEIPT_AMOUNT", ())
    options = []
    for item in account_inventory(conn):
        bundle = item["values"]
        definition = bundle["account"]
        options.append(
            ReceiptPaymentAccountOptionV1(
                account_ref(bundle),
                BundleFactRefV1(**item["fact_ref"]),
                definition["name"],
                definition["account_type"],
                definition["currency"],
            )
        )
    ordered = tuple(
        sorted(
            options,
            key=lambda item: (
                item.currency != receipt_currency,
                item.name.casefold(),
                item.account_ref.account_id,
            ),
        )
    )
    return ReceiptPaymentHandoffV1(
        draft, read_identity, ordered, "ACCOUNT_REQUIRED" if ordered else "NO_ACCOUNT_OPTIONS"
    )
