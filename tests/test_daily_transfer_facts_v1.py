from copy import deepcopy
from hashlib import sha256

import pytest

from finance_core.lifecycle_v2 import LifecycleError
from finance_core.lifecycle_v2.contracts import digest
from finance_core.lifecycle_v2.daily_v1.transfer_facts_v1 import (
    TRANSFER_FORMAT,
    project_transfer_fact_v1,
)


def instruction(evidence_id):
    return {
        "format": "finance-daily-instruction-ref-v1",
        "evidence_id": evidence_id,
        "principal_id": "synthetic-owner",
        "client_id": "synthetic-chat",
        "source_key": "synthetic-source-" + evidence_id,
        "original_sha256": "a" * 64,
        "source_timestamp": 1780000000,
    }


def account_role(role, account_id, currency, amount, signed):
    fields = {
        "account_ref": {
            "account_id": account_id,
            "definition_version": 1,
            "definition_sha256": "b" * 64,
        },
        "currency": currency,
        "amount": amount,
        "funds_time": {
            "format": "finance-daily-as-of-v1",
            "precision": "date",
            "timestamp": None,
            "local_date": "2026-11-22",
            "timezone": "Asia/Singapore",
        },
        "opening_ref": {
            "account_id": account_id,
            "opening_event_id": "opening-" + account_id,
            "opening_version": 1,
            "opening_sha256": "c" * 64,
        },
    }
    binding = {
        **fields,
        "role": role,
        "business_date": "2026-11-22",
        "instruction_ref": signed,
    }
    return {
        **fields,
        "coverage": {
            "treatment": "excluded" if role == "transfer_out" else "included",
            "basis": "strictly_later" if role == "transfer_out" else "authenticated_reply",
            "reply_ref": None if role == "transfer_out" else instruction("answer-" + role),
            "binding": binding,
            "binding_sha256": digest(binding),
        },
    }


@pytest.fixture
def transfer():
    signed = instruction("original-transfer")
    return {
        "format": TRANSFER_FORMAT,
        "status": "ACTIVE",
        "transition": "transfer_create",
        "business_date": "2026-11-22",
        "description": "Transfer to another account",
        "instruction_ref": signed,
        "reason_ref": None,
        "outgoing": account_role("transfer_out", "out", "SGD", "100.00", signed),
        "incoming": account_role("transfer_in", "in", "MYR", "330.00", signed),
        "fee": {
            "mode": "separate_debit",
            "charge": account_role("fee_debit", "out", "SGD", "1.00", signed),
        },
    }


def test_transfer_fact_retains_one_opening_answer_for_each_role(transfer):
    projection = project_transfer_fact_v1(transfer)
    assert [
        (effect["role"], effect["amount"], effect["coverage"]["treatment"])
        for effect in projection.effects
    ] == [
        ("transfer_out", "-100.00", "excluded"),
        ("transfer_in", "330.00", "included"),
        ("fee_debit", "-1.00", "included"),
    ]
    assert projection.effects[0]["opening_ref"] == projection.effects[2]["opening_ref"]
    assert projection.effects[2]["coverage"]["reply_ref"]["evidence_id"] == "answer-fee_debit"
    assert projection.expenses_by_currency == (("SGD", "1.00"),)


def test_transfer_fact_disallows_reusing_coverage_for_a_different_role(transfer):
    fee = transfer["fee"]["charge"]
    fee["coverage"]["binding"]["role"] = "transfer_out"
    fee["coverage"]["binding_sha256"] = digest(fee["coverage"]["binding"])
    with pytest.raises(LifecycleError, match="exact account role"):
        project_transfer_fact_v1(transfer)


def test_transfer_fact_refuses_inconsistent_opening_for_the_same_account(transfer):
    fee = transfer["fee"]["charge"]
    fee["opening_ref"] = {**fee["opening_ref"], "opening_version": 2}
    fee["coverage"]["binding"] = {**fee["coverage"]["binding"], "opening_ref": fee["opening_ref"]}
    fee["coverage"]["binding_sha256"] = digest(fee["coverage"]["binding"])
    with pytest.raises(LifecycleError, match="same account require the same opening"):
        project_transfer_fact_v1(transfer)


def test_transfer_fact_void_has_no_effect_but_retains_each_original_role(transfer):
    void = deepcopy(transfer)
    signed = instruction("reverse-transfer")
    quote = "Duplicate transfer"
    void.update(
        status="VOID",
        transition="transfer_void",
        instruction_ref=signed,
        reason_ref={
            "format": "finance-daily-reason-ref-v1",
            "instruction_ref": signed,
            "span": [0, len(quote)],
            "quote": quote,
            "quote_sha256": sha256(quote.encode()).hexdigest(),
        },
    )
    projection = project_transfer_fact_v1(void)
    assert projection.effects == () and projection.expenses_by_currency == ()
    assert all(void[role] == transfer[role] for role in ("outgoing", "incoming", "fee"))
