"""Full protected transfer registration across all funded fee routes."""

from copy import deepcopy
from dataclasses import asdict

import pytest

from finance_core.lifecycle_v2 import LifecycleError
from finance_core.lifecycle_v2.contracts import digest
from finance_core.lifecycle_v2.daily_v1 import DailyLedgerReaderV1, DailyTransferServiceV1
from finance_core.lifecycle_v2.daily_v1.contracts import instruction_ref
from tests.test_daily_accounts_lifecycle_v1 import setup
from tests.test_daily_transfer_facts_v1 import transfer as transfer
from tests.test_daily_transfer_opening_v1 import _account, _on
from tests.test_lifecycle_v2 import live as live


@pytest.mark.parametrize(
    ("mode", "fee_currency"),
    [("included_in_out", "SGD"), ("withheld_from_in", "MYR"), ("separate_debit", "USD")],
)
def test_one_confirmed_transfer_debits_actual_legs_and_counts_each_fee_once(
    live, transfer, mode, fee_currency
):
    ledger, port, daily, accounts, _ = setup(live)
    date = _on("2026-11-21")
    outgoing = _account(ledger, port, daily, accounts, "SGD", "1", date)
    incoming = _account(ledger, port, daily, accounts, "MYR", "2", date)
    fee_role = (
        _account(ledger, port, daily, accounts, "USD", "3", date)
        if mode == "separate_debit"
        else outgoing
        if mode == "included_in_out"
        else incoming
    )
    raw = f"Debit SGD 100.00 and credit MYR 330.00, fee {fee_currency} 1.00 {mode}"
    source = port.add_source("fee-transfer", raw)
    capture = ledger.capture(source.evidence_id, "fee-transfer-capture")
    ref = instruction_ref(asdict(source), ledger.binding)
    transfer["instruction_ref"] = ref
    if mode == "separate_debit":
        charge = transfer["fee"]["charge"]
        charge["currency"] = fee_currency
    else:
        charge = {
            "account_ref": asdict(fee_role.account_ref),
            "currency": fee_currency,
            "amount": "1.00",
        }
    transfer["fee"] = {"mode": mode, "charge": charge}
    legs = [(transfer["outgoing"], outgoing), (transfer["incoming"], incoming)]
    if mode == "separate_debit":
        legs.append((charge, fee_role))
    for leg, account in legs:
        leg.update(account_ref=asdict(account.account_ref), opening_ref=asdict(account.opening_ref))
        coverage = leg["coverage"]
        coverage["binding"].update(
            instruction_ref=ref,
            **{key: leg[key] for key in ("account_ref", "opening_ref", "currency")},
        )
        coverage.update(
            treatment="excluded",
            basis="strictly_later",
            reply_ref=None,
            binding_sha256=digest(coverage["binding"]),
        )
    service = DailyTransferServiceV1(ledger)
    before = ledger.reader().full_audit()
    incomplete = deepcopy(transfer)
    incomplete["fee"]["charge"] = None
    with pytest.raises(LifecycleError):
        service.propose_create(capture.source_id, incomplete, "fee-unknown", "fee-unknown")
    assert ledger.reader().full_audit() == before
    assert ledger.reader().source(capture.source_id).raw_input == raw
    proposed = service.propose_create(capture.source_id, transfer, "fee-values", "fee-proposal")
    review = service.prepare_review(proposed.proposal_id, proposed.version)
    assert review.material["expense_by_currency"] == [[fee_currency, "1.00"]]
    service.record_delivery(review.review_id, "fee-display")
    service.confirm(review.review_id, "fee-action", "fee-confirm")
    result = DailyLedgerReaderV1(ledger).collect_with_transfers_v2()
    assert result.collection.income_by_currency == ()
    assert result.collection.expense_by_currency == ((fee_currency, "1.00"),)
    assert len(result.contributions) == 1 and len(result.transfers) == 1
    expected = {"SGD": "0.00", "MYR": "430.00"}
    if mode == "separate_debit":
        expected["USD"] = "99.00"
    assert {row.currency: row.balance_amount for row in result.collection.balances} == expected
    assert result.collection.fact_version_count == before["fact_version_count"] + 1


def test_confirmation_serializes_against_a_competing_opening_change(live, transfer):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from finance_core.instance_admission import InstanceAdmissionError
    from finance_core.lifecycle_v2.daily_v1 import DailyOpeningChangeServiceV1
    from tests.test_daily_opening_changes_v1 import opening_command, opening_proposal
    from tests.test_daily_transfer_evidence_v1 import _transfer_with_account_snapshots

    ledger, port, daily, _, outgoing = _transfer_with_account_snapshots(
        live, transfer, opening_port=True
    )
    account_source = ledger.capture("source1", "capture1")
    account_proposal = daily.propose_account_create(account_source.source_id, "obs1", 0, "propose1")
    opening = DailyOpeningChangeServiceV1(ledger)
    proposed = opening_proposal(
        ledger,
        port,
        opening,
        account_source,
        account_proposal,
        outgoing,
        "competing-opening",
        opening_command(amount="125", boundary=_on("2026-11-22")),
    )
    opening_review = opening.prepare_review(proposed.proposal_id, proposed.version)
    opening.record_delivery(opening_review.review_id, "opening-display")
    service = DailyTransferServiceV1(ledger)
    capture = ledger.capture(
        transfer["instruction_ref"]["evidence_id"], "capture-competing-transfer"
    )
    proposed = service.propose_create(
        capture.source_id, transfer, "transfer-model", "transfer-draft"
    )
    transfer_review = service.prepare_review(proposed.proposal_id, proposed.version)
    service.record_delivery(transfer_review.review_id, "transfer-display")
    ready = Barrier(2, timeout=20)
    before = ledger.reader().full_audit()

    def attempt(command):
        api, review, label = command
        try:
            return api.confirm(review.review_id, label + "-action", label + "-confirm")
        except (InstanceAdmissionError, LifecycleError) as error:
            return error

    def submit(command):
        ready.wait()
        return attempt(command)

    commands = ((opening, opening_review, "opening"), (service, transfer_review, "transfer"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        initial = list(pool.map(submit, commands))
    results = []
    for command, result in zip(commands, initial, strict=True):
        if isinstance(result, InstanceAdmissionError):
            assert "Nested/opening production operations are forbidden" in str(result)
            result = attempt(command)
        results.append(result)
    opening_result, transfer_result = results
    assert ledger.reader().full_audit()["fact_version_count"] == before["fact_version_count"] + 1
    result = DailyLedgerReaderV1(ledger).collect_with_transfers_v2()
    if isinstance(opening_result, LifecycleError):
        assert "stale_opening_dependency_closure" in str(opening_result)
        assert (
            len(result.transfers) == 1
            and result.transfers[0].record_id == transfer_result.record_id
        )
    else:
        assert isinstance(transfer_result, LifecycleError)
        assert "opening reference is stale" in str(transfer_result)
        assert not result.transfers
        assert {b.currency: b.balance_amount for b in result.collection.balances}["SGD"] == "125.00"
