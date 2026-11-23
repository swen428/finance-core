"""Opening requirements on an actual audited and admitted daily ledger."""

from dataclasses import asdict

import pytest

from finance_core.instance_admission.sessions import lifecycle_read_operation
from finance_core.lifecycle_v2 import LifecycleError
from finance_core.lifecycle_v2.contracts import digest
from finance_core.lifecycle_v2.daily_v1.contracts import AccountDefinitionRefV1
from finance_core.lifecycle_v2.daily_v1.transfer_amounts_v1 import (
    TransferAccountAmountV1,
    TransferAmountsV1,
    TransferFeeV1,
)
from finance_core.lifecycle_v2.daily_v1.transfer_opening_v1 import (
    read_transfer_opening_requirements_v1,
    require_active_transfer_openings_v1,
)
from tests.daily_account_support_v1 import command, unknown_time
from tests.test_daily_accounts_lifecycle_v1 import change, create, setup
from tests.test_daily_transfer_facts_v1 import instruction
from tests.test_daily_transfer_facts_v1 import transfer as transfer
from tests.test_lifecycle_v2 import live as live


def _on(day):
    return {**unknown_time(), "precision": "date", "local_date": day}


def _account(ledger, port, daily, reader, currency, suffix, as_of):
    port.add_source("source" + suffix, "Create a " + currency + " bank account")
    _, _, _, receipt = create(
        ledger,
        port,
        daily,
        command(
            fields={
                "name": currency,
                "account_type": "bank",
                "currency": currency,
                "opening": None if as_of is None else {"amount": "100.00", "as_of": as_of},
            }
        ),
        suffix=suffix,
    )
    return reader.get_account(receipt.account_ref.account_id)


def test_one_protected_snapshot_requires_separate_answers_for_debit_credit_and_fee(live):
    ledger, port, daily, reader, _ = setup(live)
    boundary = _on("2025-01-02")
    debit = _account(ledger, port, daily, reader, "SGD", "1", boundary)
    credit = _account(ledger, port, daily, reader, "MYR", "2", unknown_time())
    charge = TransferAccountAmountV1(debit.account_ref, "SGD", "1")
    amounts = TransferAmountsV1(
        TransferAccountAmountV1(debit.account_ref, "SGD", "10"),
        TransferAccountAmountV1(credit.account_ref, "MYR", "31"),
        TransferFeeV1("separate_debit", charge),
    )
    before = reader.collect_accounts()
    result = read_transfer_opening_requirements_v1(
        ledger,
        amounts,
        {
            "transfer_out": _on("2025-01-03"),
            "transfer_in": _on("2025-01-03"),
            "fee_debit": boundary,
        },
    )
    assert result.read_identity == before.read_identity
    assert [(item.impact.role, item.opening_ref, item.requirement) for item in result.roles] == [
        ("transfer_out", debit.opening_ref, "strictly_later"),
        ("transfer_in", credit.opening_ref, "authenticated_reply_required"),
        ("fee_debit", debit.opening_ref, "authenticated_reply_required"),
    ]
    assert reader.collect_accounts() == before
    assert ledger.reader().full_audit()["fact_version_count"] == 2


def test_snapshots_reject_stale_account_and_treat_void_opening_as_no_baseline(live):
    ledger, port, daily, reader, _ = setup(live)
    debit = _account(ledger, port, daily, reader, "SGD", "1", unknown_time())
    credit = _account(ledger, port, daily, reader, "MYR", "2", None)
    change(port, daily, debit, "account_rename", {"name": "Renamed"}, "rename")
    current = reader.get_account(debit.account_id)
    change(port, daily, current, "opening_void", {}, "void", "wrong opening")
    current = reader.get_account(debit.account_id)
    target = TransferAccountAmountV1(credit.account_ref, "MYR", "31")
    fee = TransferFeeV1("none")
    times = {"transfer_out": unknown_time(), "transfer_in": unknown_time()}
    with pytest.raises(LifecycleError, match="missing or stale"):
        read_transfer_opening_requirements_v1(
            ledger,
            TransferAmountsV1(TransferAccountAmountV1(debit.account_ref, "SGD", "10"), target, fee),
            times,
        )
    result = read_transfer_opening_requirements_v1(
        ledger,
        TransferAmountsV1(TransferAccountAmountV1(current.account_ref, "SGD", "10"), target, fee),
        times,
    )
    assert current.opening_ref is not None
    assert [
        (role.opening_ref, role.opening_as_of_json, role.requirement) for role in result.roles
    ] == [
        (None, None, "no_active_opening"),
        (None, None, "no_active_opening"),
    ]


def test_missing_role_time_or_account_and_currency_mismatch_fail_closed(live):
    ledger, port, daily, reader, _ = setup(live)
    debit = _account(ledger, port, daily, reader, "SGD", "1", None)
    credit = _account(ledger, port, daily, reader, "MYR", "2", None)
    missing = AccountDefinitionRefV1("nonexistent", 1, "a" * 64)
    out = TransferAccountAmountV1(debit.account_ref, "SGD", "10")
    wrong_currency = TransferAccountAmountV1(credit.account_ref, "USD", "8")
    missing_account = TransferAccountAmountV1(missing, "MYR", "31")
    fee = TransferFeeV1("none")
    times = {"transfer_out": unknown_time(), "transfer_in": unknown_time()}
    with pytest.raises(LifecycleError, match="actual transfer funds times"):
        read_transfer_opening_requirements_v1(
            ledger, TransferAmountsV1(out, wrong_currency, fee), {"transfer_out": unknown_time()}
        )
    with pytest.raises(LifecycleError, match="currency differs"):
        read_transfer_opening_requirements_v1(
            ledger, TransferAmountsV1(out, wrong_currency, fee), times
        )
    with pytest.raises(LifecycleError, match="missing or stale"):
        read_transfer_opening_requirements_v1(
            ledger, TransferAmountsV1(out, missing_account, fee), times
        )


def test_confirmation_checks_opening_time_in_its_current_and_original_prefix(live, transfer):
    ledger, port, daily, reader, _ = setup(live)
    debit = _account(ledger, port, daily, reader, "SGD", "1", _on("2026-11-21"))
    credit = _account(ledger, port, daily, reader, "MYR", "2", unknown_time())
    prior_seq = reader.collect_accounts().read_identity.event_seq + 1
    transfer["fee"] = {"mode": "none", "charge": None}
    for name, current in (("outgoing", debit), ("incoming", credit)):
        leg = transfer[name]
        leg["opening_ref"] = asdict(current.opening_ref)
        leg["account_ref"] = asdict(current.account_ref)
        binding = leg["coverage"]["binding"]
        binding.update(opening_ref=leg["opening_ref"], account_ref=leg["account_ref"])
        leg["coverage"]["binding_sha256"] = digest(binding)

    change(
        port,
        daily,
        debit,
        "opening_correct",
        {"amount": "100", "as_of": _on("2026-11-22")},
        "opening",
        "wrong opening time",
    )
    new_opening = reader.get_account(debit.account_id).opening_ref
    outgoing = transfer["outgoing"]
    with lifecycle_read_operation(ledger.owner) as conn:
        require_active_transfer_openings_v1(conn, transfer, prior_seq)
        with pytest.raises(LifecycleError, match="opening reference is stale"):
            require_active_transfer_openings_v1(conn, transfer)
        outgoing["opening_ref"] = asdict(new_opening)
        binding = outgoing["coverage"]["binding"]
        binding["opening_ref"] = outgoing["opening_ref"]
        outgoing["coverage"]["binding_sha256"] = digest(binding)
        with pytest.raises(LifecycleError, match="actual opening time"):
            require_active_transfer_openings_v1(conn, transfer)
        outgoing["coverage"].update(
            basis="authenticated_reply", reply_ref=instruction("answer-transfer-out")
        )
        require_active_transfer_openings_v1(conn, transfer)
