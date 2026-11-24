import sqlite3
from copy import deepcopy
from hashlib import sha256

import pytest

from finance_core.lifecycle_v2 import LifecycleError
from finance_core.lifecycle_v2.contracts import canonical, digest
from finance_core.lifecycle_v2.daily_v1.contracts import BundleFactRefV1
from finance_core.lifecycle_v2.daily_v1.movement_contracts import MOVEMENT_FORMAT
from finance_core.lifecycle_v2.daily_v1.refund_allowance_v1 import (
    RefundExpenseV1,
    compile_refund_allowance_v1,
)
from finance_core.lifecycle_v2.daily_v1.refund_facts_v1 import (
    REFUND_FORMAT,
    project_refund_fact_v1,
    require_refund_predecessor_v1,
)
from finance_core.lifecycle_v2.daily_v1.refund_inventory_v1 import active_refund_allocations_v1
from tests.test_daily_transfer_facts_v1 import instruction


def _bind_opening(value):
    credit = value["credit"]
    coverage = credit["coverage"]
    binding = {
        "received_date": value["received_date"],
        "expense": value["expense"],
        "original": value["original"],
        "credit_account": credit["account_ref"],
        "credit_currency": credit["currency"],
        "credit_amount": credit["amount"],
        "opening_ref": credit["opening_ref"],
        "instruction_ref": value["instruction_ref"],
    }
    coverage.update(binding=deepcopy(binding), binding_sha256=digest(binding))


@pytest.fixture
def refund():
    value = {
        "format": REFUND_FORMAT,
        "status": "ACTIVE",
        "transition": "refund_create",
        "received_date": "2026-11-24",
        "description": "Original purchase refunded",
        "instruction_ref": instruction("original-refund"),
        "reason_ref": None,
        "expense": {
            "fact_ref": {
                "record_id": "original-purchase",
                "version": 1,
                "fact_sha256": "c" * 64,
                "commit_seq": 18,
            },
            "economic_event_id": "purchase-event",
        },
        "original": {"currency": "JPY", "amount": "1000"},
        "credit": {
            "account_ref": {
                "account_id": "card",
                "definition_version": 1,
                "definition_sha256": "b" * 64,
            },
            "currency": "SGD",
            "amount": "9.20",
            "opening_ref": {
                "account_id": "card",
                "opening_event_id": "opening-card",
                "opening_version": 1,
                "opening_sha256": "d" * 64,
            },
            "coverage": {
                "treatment": "included",
                "basis": "authenticated_reply",
                "reply_ref": instruction("opening-answer"),
            },
        },
    }
    _bind_opening(value)
    return value


def _change(before, transition):
    value = deepcopy(before)
    signed = instruction(transition)
    quote = "Merchant confirmed the refund"
    value.update(
        transition=transition,
        instruction_ref=signed,
        reason_ref={
            "format": "finance-daily-reason-ref-v1",
            "instruction_ref": signed,
            "span": [0, len(quote)],
            "quote": quote,
            "quote_sha256": sha256(quote.encode()).hexdigest(),
        },
    )
    return value


def test_refund_fact_projects_the_actual_credit_and_keeps_the_original_amount(refund):
    require_refund_predecessor_v1(None, refund)
    result = project_refund_fact_v1(refund)
    assert result.observed_expense.record_id == "original-purchase"
    assert result.expense_economic_event_id == "purchase-event"
    assert (result.terms.original_currency, result.terms.original_amount) == ("JPY", "1000")
    assert (result.terms.credit_currency, result.terms.credit_amount) == ("SGD", "9.20")
    credit = result.credit_effect
    assert (credit["role"], credit["account_id"], credit["amount"]) == (
        "refund_credit",
        "card",
        "9.20",
    )
    assert credit["funds_time"]["local_date"] == "2026-11-24"
    assert credit["coverage"]["reply_ref"] == instruction("opening-answer")


@pytest.mark.parametrize("changed", ["credit_amount", "receipt_date", "original_amount"])
def test_old_opening_confirmation_cannot_authorize_changed_refund_terms(refund, changed):
    if changed == "credit_amount":
        refund["credit"]["amount"] = "9.40"
    elif changed == "receipt_date":
        refund["received_date"] = "2026-11-25"
    else:
        refund["original"]["amount"] = "800"
    with pytest.raises(LifecycleError, match="coverage differs from its exact credit"):
        project_refund_fact_v1(refund)


def test_refund_credit_preserves_canonical_money_and_independent_opening_reply(refund):
    refund["credit"]["amount"] = "9.2"
    with pytest.raises(LifecycleError, match="Canonical positive"):
        project_refund_fact_v1(refund)
    refund["credit"]["amount"] = "9.20"
    refund["credit"]["coverage"]["reply_ref"]["source_key"] = refund["instruction_ref"][
        "source_key"
    ]
    with pytest.raises(LifecycleError, match="independent authenticated reply"):
        project_refund_fact_v1(refund)


def test_corrected_refund_keeps_the_same_expense_identity_and_currency(refund):
    corrected = _change(refund, "refund_correct")
    corrected["credit"]["amount"] = "9.40"
    corrected["expense"]["fact_ref"].update(version=2, fact_sha256="e" * 64, commit_seq=30)
    _bind_opening(corrected)
    require_refund_predecessor_v1(refund, corrected)
    assert project_refund_fact_v1(corrected).terms.credit_amount == "9.40"

    corrected["expense"]["fact_ref"]["record_id"] = "unrelated-purchase"
    _bind_opening(corrected)
    with pytest.raises(LifecycleError, match="retain its original expense relationship"):
        require_refund_predecessor_v1(refund, corrected)


def test_reversed_refund_retains_the_original_credit_evidence_without_effect(refund):
    reverse = _change(refund, "refund_void")
    reverse["status"] = "VOID"
    require_refund_predecessor_v1(refund, reverse)
    result = project_refund_fact_v1(reverse)
    assert result.credit_effect is None
    assert result.terms.credit_amount == "9.20"
    with pytest.raises(LifecycleError, match="requires its exact active predecessor"):
        require_refund_predecessor_v1(None, reverse)

    reverse["description"] = "Changed description"
    with pytest.raises(LifecycleError, match="retain all original business values"):
        require_refund_predecessor_v1(refund, reverse)


def test_refund_inventory_uses_the_latest_versions_at_the_requested_ledger_cut(refund):
    observed = refund["expense"]["fact_ref"]
    current = BundleFactRefV1(observed["record_id"], 2, "e" * 64, 35)
    expense = RefundExpenseV1(current, "purchase-event", "JPY", "1000")
    original = {
        "format": MOVEMENT_FORMAT,
        "kind": "expense",
        "status": "ACTIVE",
        "original_amount": "1000",
        "original_currency": "JPY",
    }
    refund["original"]["amount"] = "400"
    _bind_opening(refund)
    second = deepcopy(refund)
    second["original"]["amount"] = "600"
    second["instruction_ref"] = instruction("second-original-refund")
    _bind_opening(second)
    reversed_first = _change(refund, "refund_void")
    reversed_first["status"] = "VOID"

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE l2_facts (record_id TEXT, version INTEGER, fact_sha256 TEXT, "
            "commit_seq INTEGER, economic_event_id TEXT, values_json TEXT, "
            "PRIMARY KEY(record_id,version))"
        )
        with pytest.raises(LifecycleError, match="one protected ledger transaction"):
            active_refund_allocations_v1(conn, expense)
        conn.executemany(
            "INSERT INTO l2_facts VALUES (?,?,?,?,?,?)",
            [
                (
                    observed["record_id"],
                    1,
                    observed["fact_sha256"],
                    18,
                    "purchase-event",
                    canonical(original),
                ),
                (
                    current.record_id,
                    2,
                    current.fact_sha256,
                    35,
                    "purchase-event",
                    canonical(original),
                ),
                ("refund-a", 1, "1" * 64, 30, "event-a", canonical(refund)),
                ("refund-b", 1, "2" * 64, 40, "event-b", canonical(second)),
                ("refund-a", 2, "3" * 64, 50, "event-a", canonical(reversed_first)),
            ],
        )
        historical = compile_refund_allowance_v1(
            expense, active_refund_allocations_v1(conn, expense, 50)
        )
        assert (historical.refunded_original, historical.remaining_original) == ("1000", "0")
        restored = compile_refund_allowance_v1(expense, active_refund_allocations_v1(conn, expense))
        assert (restored.refunded_original, restored.remaining_original) == ("600", "400")
        assert [entry.fact.record_id for entry in restored.active_refunds] == ["refund-b"]

        second["expense"]["fact_ref"]["fact_sha256"] = "f" * 64
        _bind_opening(second)
        conn.execute(
            "UPDATE l2_facts SET values_json=? WHERE record_id='refund-b'", (canonical(second),)
        )
        with pytest.raises(LifecycleError, match="lacks its exact observed expense fact"):
            active_refund_allocations_v1(conn, expense)
