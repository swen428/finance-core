"""Validate and project one typed transfer fact with per-role opening evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..contracts import LifecycleError, digest
from .contracts import AccountDefinitionRefV1, OpeningRefV1, _reference, exact, text, validate_as_of
from .movement_contracts import _day, _validate_reason_ref
from .transfer_amounts_v1 import (
    TransferAccountAmountV1,
    TransferAmountsV1,
    TransferFeeV1,
    TransferRole,
    _fee_mode,
)

TRANSFER_FORMAT = "finance-daily-transfer-v1"
_TRANSITIONS = {"transfer_create", "transfer_correct", "transfer_void"}
_AMOUNT_FIELDS = {"account_ref", "currency", "amount"}
_LEG_FIELDS = _AMOUNT_FIELDS | {"funds_time", "opening_ref", "coverage"}


@dataclass(frozen=True)
class TransferProjectionV1:
    effects: tuple[dict[str, Any], ...]
    expenses_by_currency: tuple[tuple[str, str], ...]


def _amount(value: dict[str, Any]) -> TransferAccountAmountV1:
    raw_account = value["account_ref"]
    exact(raw_account, AccountDefinitionRefV1.__dataclass_fields__, "transfer account reference")
    amount = TransferAccountAmountV1(
        AccountDefinitionRefV1(**raw_account), value["currency"], value["amount"]
    )
    if amount.amount != value["amount"]:
        raise LifecycleError("Noncanonical actual transfer amount")
    return amount


def _require_coverage(
    leg: dict[str, Any],
    role: TransferRole,
    fact: dict[str, Any],
) -> None:
    coverage = leg["coverage"]
    exact(
        coverage,
        {"treatment", "basis", "reply_ref", "binding", "binding_sha256"},
        "transfer coverage",
    )
    binding = coverage["binding"]
    exact(
        binding,
        {
            "role",
            "business_date",
            "account_ref",
            "opening_ref",
            "funds_time",
            "currency",
            "amount",
            "instruction_ref",
        },
        "transfer coverage binding",
    )
    instruction = binding["instruction_ref"]
    _reference(instruction)
    if (
        (fact["status"] == "ACTIVE" and instruction != fact["instruction_ref"])
        or any(
            instruction[key] != fact["instruction_ref"][key]
            for key in ("principal_id", "client_id")
        )
        or binding
        != {
            "role": role,
            "business_date": fact["business_date"],
            "account_ref": leg["account_ref"],
            "opening_ref": leg["opening_ref"],
            "funds_time": leg["funds_time"],
            "currency": leg["currency"],
            "amount": leg["amount"],
            "instruction_ref": instruction,
        }
        or digest(binding) != coverage["binding_sha256"]
    ):
        raise LifecycleError("Transfer coverage differs from its exact account role")
    basis, treatment, reply = coverage["basis"], coverage["treatment"], coverage["reply_ref"]
    if type(basis) is not str or type(treatment) is not str:
        raise LifecycleError("Exact transfer coverage treatment required")
    if leg["opening_ref"] is None:
        if (basis, treatment, reply) != ("no_active_opening", "no_baseline", None):
            raise LifecycleError("Transfer coverage requires its referenced opening")
    elif basis == "strictly_later":
        if (
            treatment != "excluded"
            or reply is not None
            or leg["funds_time"]["precision"] == "unknown"
        ):
            raise LifecycleError("A later transfer role must carry independently known funds time")
    elif basis == "authenticated_reply":
        _reference(reply)
        if (
            treatment not in {"included", "excluded"}
            or any(reply[key] != instruction[key] for key in ("principal_id", "client_id"))
            or reply["evidence_id"] == instruction["evidence_id"]
            or reply["source_timestamp"] < instruction["source_timestamp"]
        ):
            raise LifecycleError("Transfer coverage must retain an independent authenticated reply")
    else:
        raise LifecycleError("Transfer role with an opening requires exact coverage")


def _leg(
    value: dict[str, Any],
    role: TransferRole,
    fact: dict[str, Any],
) -> TransferAccountAmountV1:
    exact(value, _LEG_FIELDS, "transfer account role")
    amount = _amount(value)
    validate_as_of(value["funds_time"])
    raw_opening = value["opening_ref"]
    if raw_opening is not None:
        exact(raw_opening, OpeningRefV1.__dataclass_fields__, "transfer opening reference")
        if OpeningRefV1(**raw_opening).account_id != amount.account.account_id:
            raise LifecycleError("Transfer opening belongs to a different account")
    _require_coverage(value, role, fact)
    return amount


def project_transfer_fact_v1(value: dict[str, Any]) -> TransferProjectionV1:
    """Validate the fact shape and project its active roles without acquiring write authority."""
    exact(
        value,
        {
            "format",
            "status",
            "transition",
            "business_date",
            "description",
            "instruction_ref",
            "reason_ref",
            "outgoing",
            "incoming",
            "fee",
        },
        "transfer fact",
    )
    transition = value["transition"]
    if (
        type(value["format"]) is not str
        or value["format"] != TRANSFER_FORMAT
        or type(transition) is not str
        or transition not in _TRANSITIONS
    ):
        raise LifecycleError("Invalid transfer fact format or transition")
    wanted_status = "VOID" if transition == "transfer_void" else "ACTIVE"
    if type(value["status"]) is not str or value["status"] != wanted_status:
        raise LifecycleError("Invalid transfer fact status")
    _day(value["business_date"])
    text(value["description"], "description", 1024)
    _reference(value["instruction_ref"])
    reason = value["reason_ref"]
    _validate_reason_ref(reason)
    if (reason is None) != (transition == "transfer_create") or (
        reason is not None and reason["instruction_ref"] != value["instruction_ref"]
    ):
        raise LifecycleError("Transfer correction or reversal needs its own genuine reason")

    roles = {"transfer_out": value["outgoing"], "transfer_in": value["incoming"]}
    outgoing = _leg(value["outgoing"], "transfer_out", value)
    incoming = _leg(value["incoming"], "transfer_in", value)
    fee = value["fee"]
    exact(fee, {"mode", "charge"}, "transfer fee")
    mode = _fee_mode(fee["mode"])
    charge = None
    charge_data = fee["charge"]
    if charge_data is not None:
        if mode == "separate_debit":
            charge = _leg(charge_data, "fee_debit", value)
            roles["fee_debit"] = charge_data
            for account, leg in (
                (outgoing.account, value["outgoing"]),
                (incoming.account, value["incoming"]),
            ):
                if charge.account == account and charge_data["opening_ref"] != leg["opening_ref"]:
                    raise LifecycleError(
                        "Transfer roles on the same account require the same opening"
                    )
        else:
            exact(charge_data, _AMOUNT_FIELDS, "transfer charge in an existing account role")
            charge = _amount(charge_data)
    amounts = TransferAmountsV1(outgoing, incoming, TransferFeeV1(mode, charge))
    if wanted_status == "VOID":
        return TransferProjectionV1((), ())
    effects = tuple(
        {
            "role": impact.role,
            "account_id": impact.account.account_id,
            "currency": impact.currency,
            "amount": impact.amount,
            "funds_time": roles[impact.role]["funds_time"],
            "opening_ref": roles[impact.role]["opening_ref"],
            "coverage": roles[impact.role]["coverage"],
        }
        for impact in amounts.account_impacts()
    )
    return TransferProjectionV1(effects, amounts.expense_by_currency())
