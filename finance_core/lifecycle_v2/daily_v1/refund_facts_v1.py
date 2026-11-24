"""Validate a linked refund payload and its separate account credit."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..contracts import LifecycleError, canonical, digest
from .contracts import (
    AccountDefinitionRefV1,
    BundleFactRefV1,
    OpeningRefV1,
    _reference,
    exact,
    text,
)
from .movement_contracts import _day, _validate_reason_ref
from .refund_allowance_v1 import RefundTermsV1, _refund_funds_time

REFUND_FORMAT = "finance-daily-refund-v1"
_TRANSITIONS = {"refund_create", "refund_correct", "refund_void"}
_BUSINESS_FIELDS = {"received_date", "description", "expense", "original", "credit"}


@dataclass(frozen=True)
class RefundFactProjectionV1:
    observed_expense: BundleFactRefV1
    expense_economic_event_id: str
    terms: RefundTermsV1
    credit_effect: dict[str, Any] | None


def _require_credit_coverage(value: dict[str, Any], allow_pending: bool) -> OpeningRefV1 | None:
    credit = value["credit"]
    coverage = credit["coverage"]
    exact(
        coverage,
        {"treatment", "basis", "reply_ref", "binding", "binding_sha256"},
        "refund opening coverage",
    )
    binding = coverage["binding"]
    exact(
        binding,
        {
            "received_date",
            "expense",
            "original",
            "credit_account",
            "credit_currency",
            "credit_amount",
            "opening_ref",
            "instruction_ref",
        },
        "refund opening binding",
    )
    instruction = binding["instruction_ref"]
    _reference(instruction)
    owner_keys = ("principal_id", "client_id")
    if (
        (value["status"] == "ACTIVE" and instruction != value["instruction_ref"])
        or any(instruction[key] != value["instruction_ref"][key] for key in owner_keys)
        or canonical(binding)
        != canonical(
            {
                "received_date": value["received_date"],
                "expense": value["expense"],
                "original": value["original"],
                "credit_account": credit["account_ref"],
                "credit_currency": credit["currency"],
                "credit_amount": credit["amount"],
                "opening_ref": credit["opening_ref"],
                "instruction_ref": instruction,
            }
        )
        or digest(binding) != coverage["binding_sha256"]
    ):
        raise LifecycleError("Refund opening coverage differs from its exact credit")

    basis, treatment, reply = coverage["basis"], coverage["treatment"], coverage["reply_ref"]
    if type(basis) is not str or type(treatment) is not str:
        raise LifecycleError("Exact refund opening treatment required")
    raw_opening = credit["opening_ref"]
    if raw_opening is None:
        if (basis, treatment, reply) != ("no_active_opening", "no_baseline", None):
            raise LifecycleError("Refund opening treatment requires its opening reference")
        return None

    exact(raw_opening, OpeningRefV1.__dataclass_fields__, "refund opening reference")
    opening = OpeningRefV1(**raw_opening)
    if opening.account_id != credit["account_ref"]["account_id"]:
        raise LifecycleError("Refund opening belongs to a different account")
    if (basis, treatment, reply) == ("awaiting_reply", "unknown", None):
        if not allow_pending or value["status"] != "ACTIVE":
            raise LifecycleError("Pending opening coverage cannot enter a refund fact")
        return opening
    if basis == "strictly_later" and treatment == "excluded" and reply is None:
        return opening
    if basis != "authenticated_reply" or treatment not in {"included", "excluded"}:
        raise LifecycleError("Refund credit requires an independently supported opening treatment")
    _reference(reply)
    if (
        any(reply[key] != instruction[key] for key in owner_keys)
        or any(reply[key] == instruction[key] for key in ("evidence_id", "source_key"))
        or reply["source_timestamp"] < instruction["source_timestamp"]
    ):
        raise LifecycleError("Refund opening requires an independent authenticated reply")
    return opening


def project_refund_fact_v1(
    value: dict[str, Any], *, allow_pending: bool = False
) -> RefundFactProjectionV1:
    """Validate the serialized shape without granting authority to post a refund."""
    exact(
        value,
        _BUSINESS_FIELDS | {"format", "status", "transition", "instruction_ref", "reason_ref"},
        "refund fact",
    )
    transition = value["transition"]
    if (
        value["format"] != REFUND_FORMAT
        or type(transition) is not str
        or transition not in _TRANSITIONS
    ):
        raise LifecycleError("Invalid refund fact format or transition")
    status = "VOID" if transition == "refund_void" else "ACTIVE"
    if value["status"] != status:
        raise LifecycleError("Invalid refund fact status")
    received = _day(value["received_date"])
    text(value["description"], "refund description", 1024)
    _reference(value["instruction_ref"])
    reason = value["reason_ref"]
    _validate_reason_ref(reason)
    if (reason is None) != (transition == "refund_create") or (
        reason is not None and reason["instruction_ref"] != value["instruction_ref"]
    ):
        raise LifecycleError("Refund correction or reversal needs its own genuine reason")

    expense = value["expense"]
    exact(expense, {"fact_ref", "economic_event_id"}, "linked original expense")
    exact(
        expense["fact_ref"], BundleFactRefV1.__dataclass_fields__, "linked expense fact reference"
    )
    observed = BundleFactRefV1(**expense["fact_ref"])
    event_id = text(expense["economic_event_id"], "original expense economic event")
    original, credit = value["original"], value["credit"]
    exact(original, {"currency", "amount"}, "refund original amount")
    exact(
        credit,
        {"account_ref", "currency", "amount", "opening_ref", "coverage"},
        "refund credited amount",
    )
    exact(credit["account_ref"], AccountDefinitionRefV1.__dataclass_fields__, "refund account")
    account = AccountDefinitionRefV1(**credit["account_ref"])
    terms = RefundTermsV1(
        original["currency"], original["amount"], account, credit["currency"], credit["amount"]
    )
    opening = _require_credit_coverage(value, allow_pending)
    effect = None
    if status == "ACTIVE":
        effect = {
            "role": "refund_credit",
            "account_id": account.account_id,
            "currency": terms.credit_currency,
            "amount": terms.credit_amount,
            "funds_time": _refund_funds_time(received),
            "opening_ref": asdict(opening) if opening is not None else None,
            "coverage": credit["coverage"],
        }
    return RefundFactProjectionV1(observed, event_id, terms, effect)


def require_refund_predecessor_v1(
    before: dict[str, Any] | None, after: dict[str, Any], *, allow_pending: bool = False
) -> None:
    """Preserve the original relationship and the immutable values of a reversed refund."""
    current = project_refund_fact_v1(after, allow_pending=allow_pending)
    if before is None:
        if after["transition"] != "refund_create":
            raise LifecycleError("A refund change requires its exact active predecessor")
        return
    previous = project_refund_fact_v1(before)
    if before["status"] != "ACTIVE" or after["transition"] == "refund_create":
        raise LifecycleError("An existing refund requires an active predecessor and a change")
    before_instruction, after_instruction = before["instruction_ref"], after["instruction_ref"]
    if (
        any(
            before_instruction[key] != after_instruction[key]
            for key in ("principal_id", "client_id")
        )
        or any(
            before_instruction[key] == after_instruction[key]
            for key in ("evidence_id", "source_key")
        )
        or after_instruction["source_timestamp"] < before_instruction["source_timestamp"]
    ):
        raise LifecycleError("A refund change requires a new instruction from its owner")
    old_ref, new_ref = previous.observed_expense, current.observed_expense
    same_record = old_ref.record_id == new_ref.record_id
    same_event = previous.expense_economic_event_id == current.expense_economic_event_id
    if same_record != same_event or (
        same_record
        and (
            previous.terms.original_currency != current.terms.original_currency
            or old_ref.version > new_ref.version
            or (old_ref.version == new_ref.version and old_ref != new_ref)
            or (old_ref.version < new_ref.version and old_ref.commit_seq >= new_ref.commit_seq)
        )
    ):
        raise LifecycleError("The refund change must retain its original expense relationship")
    if not same_record and (
        after["transition"] != "refund_correct"
        or before["received_date"] != after["received_date"]
        or before["credit"]["account_ref"]["account_id"]
        != after["credit"]["account_ref"]["account_id"]
        or any(before["credit"][key] != after["credit"][key] for key in ("currency", "amount"))
    ):
        raise LifecycleError("Relinking a refund must preserve the same actual credit and date")
    if after["transition"] == "refund_void" and any(
        before[key] != after[key] for key in _BUSINESS_FIELDS
    ):
        raise LifecycleError("Refund reversal must retain all original business values")
