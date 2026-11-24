"""Deterministic expense candidate from a complete authenticated receipt preparation."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from .contracts import LifecycleError, VerifiedSource, canonical, digest
from .daily_v1.contracts import instruction_ref, validate_as_of
from .daily_v1.movement_contracts import MOVEMENT_FORMAT, validate_movement
from .receipt_payment_handoff_v1 import ReceiptPaymentAccountOptionV1

if TYPE_CHECKING:
    from .receipt_review_fields_v1 import ReceiptReviewFieldsV1


@dataclass(frozen=True)
class ReceiptExpenseCandidateV1:
    source_id: str
    source_sha256: str
    original_message_sha256: str
    original_media_sha256: str
    preparation_sha256: str
    fields_reply_sha256: str
    values_json: str
    format: str = "finance-receipt-expense-candidate-v1"


@dataclass(frozen=True)
class ReceiptExpenseConfirmationPreviewV1:
    """Display material from one authenticated receipt read."""

    candidate: ReceiptExpenseCandidateV1
    account: ReceiptPaymentAccountOptionV1
    format: str = "finance-receipt-expense-confirmation-preview-v1"

    @property
    def preview_sha256(self) -> str:
        return digest(asdict(self))


def _coverage_reply(raw, treatment, instruction):
    quote = "期初余额：" + {"included": "已包含", "excluded": "未包含"}[treatment]
    offset = len(raw) - len(raw.lstrip())
    for line in raw.strip().splitlines(keepends=True):
        if line.splitlines() == [quote]:
            return {
                "format": "finance-daily-reason-ref-v1",
                "instruction_ref": instruction,
                "span": [offset, offset + len(quote)],
                "quote": quote,
                "quote_sha256": hashlib.sha256(quote.encode()).hexdigest(),
            }
        offset += len(line)
    raise LifecycleError("Receipt opening choice lacks its original exact text")


def _compile_receipt_expense_candidate(fields: ReceiptReviewFieldsV1) -> ReceiptExpenseCandidateV1:
    preparation = fields.preparation
    source = preparation.source
    reply = fields.evidence.reply
    source_sha256 = digest(asdict(source))
    reply_sha256 = digest(asdict(fields.evidence))
    # A poll response may contain several independently authenticated updates.
    # Keep this actor's exact update and the original media in the instruction identity.
    identity = digest(["finance-receipt-expense-instruction-v1", source_sha256, reply_sha256])
    instruction = instruction_ref(
        asdict(
            VerifiedSource(
                "receipt-expense-instruction-" + identity,
                fields.evidence.principal_id,
                fields.evidence.client_id,
                "receipt-expense-instruction-v1:" + identity,
                reply.raw_text,
                hashlib.sha256(reply.raw_text.encode()).hexdigest(),
                reply.reply_timestamp,
            )
        ),
        preparation.binding,
    )
    day = fields.actual_debit_date
    funds_time = validate_as_of(
        {
            "format": "finance-daily-as-of-v1",
            "precision": "date" if day is not None else "unknown",
            "timestamp": None,
            "local_date": day,
            "timezone": "Asia/Singapore",
        }
    )
    account = preparation.payment.account.account_ref
    opening = preparation.active_opening_ref
    coverage = {
        "account_ref": asdict(account),
        "opening_ref": asdict(opening) if opening is not None else None,
        "kind": "expense",
        "business_date": preparation.business_date,
        "original_amount": preparation.original_amount,
        "original_currency": preparation.original_currency,
        "account_amount": preparation.account_amount,
        "funds_time": funds_time,
        "instruction_ref": instruction,
    }
    reply_ref = (
        _coverage_reply(reply.raw_text, fields.opening_treatment, instruction)
        if fields.opening_basis == "authenticated_reply"
        else None
    )
    values = validate_movement(
        {
            **coverage,
            "format": MOVEMENT_FORMAT,
            "status": "ACTIVE",
            "transition": "movement_create",
            "description": preparation.merchant,
            "category": fields.category,
            "account_id": account.account_id,
            "account_currency": preparation.account_currency,
            "coverage": {
                "treatment": fields.opening_treatment,
                "basis": fields.opening_basis,
                "reply_ref": reply_ref,
                "binding": coverage,
                "binding_sha256": digest(coverage),
            },
            "reason_ref": None,
        }
    )
    return ReceiptExpenseCandidateV1(
        preparation.draft.source_id,
        source_sha256,
        source.original_message_sha256,
        source.original_sha256,
        preparation.preparation_sha256,
        reply_sha256,
        canonical(values),
    )
