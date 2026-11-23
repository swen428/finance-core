"""Transfer coverage can be read only from the exact delivered role question."""

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256

import pytest

from finance_core.lifecycle_v2 import LifecycleError, VerifiedSource
from finance_core.lifecycle_v2.contracts import canonical, digest
from finance_core.lifecycle_v2.daily_v1.contracts import BundleFactRefV1, instruction_ref
from finance_core.lifecycle_v2.daily_v1.ledger_reader import (
    DailyLedgerReaderV1,
    _resolve_current_transfer,
)
from finance_core.lifecycle_v2.daily_v1.movements import DailyMovementServiceV1
from finance_core.lifecycle_v2.daily_v1.transfer_drafts_v1 import DailyTransferDraftServiceV1
from finance_core.lifecycle_v2.daily_v1.transfer_evidence_v1 import (
    QUESTION_FORMAT,
    VerifiedTransferOpeningReplyV1,
    _transfer_terms_digest,
    require_transfer_opening_replies_v1,
)
from finance_core.lifecycle_v2.daily_v1.transfer_review_v1 import read_transfer_create_preview_v1
from tests.daily_account_support_v1 import unknown_time
from tests.test_daily_accounts_lifecycle_v1 import change, setup
from tests.test_daily_transfer_facts_v1 import transfer as transfer
from tests.test_daily_transfer_opening_v1 import _account, _on
from tests.test_lifecycle_v2 import live as live


def _retained_questions(ledger, port, transfer):
    original = transfer["instruction_ref"]
    raw_instruction = "Transfer actual SGD 100.00 to MYR 330.00 with an additional SGD 1.00 fee"
    source = VerifiedSource(
        original["evidence_id"],
        ledger.binding.principal_id,
        ledger.binding.client_id,
        original["source_key"],
        raw_instruction,
        sha256(raw_instruction.encode()).hexdigest(),
        original["source_timestamp"],
        "Asia/Singapore",
    )
    port.sources[source.evidence_id] = source
    original.update(instruction_ref(asdict(source), ledger.binding))
    legs = (transfer["outgoing"], transfer["incoming"], transfer["fee"]["charge"])
    for leg in legs:
        coverage = leg["coverage"]
        coverage["binding"]["instruction_ref"] = original
        coverage["binding_sha256"] = digest(coverage["binding"])
        reply = coverage["reply_ref"]
        if reply:
            raw = "已包含"
            source = VerifiedSource(
                reply["evidence_id"],
                ledger.binding.principal_id,
                ledger.binding.client_id,
                reply["source_key"],
                raw,
                sha256(raw.encode()).hexdigest(),
                original["source_timestamp"] + 5,
                "Asia/Singapore",
            )
            port.sources[source.evidence_id] = source
            coverage["reply_ref"] = instruction_ref(asdict(source), ledger.binding)

    terms = _transfer_terms_digest(transfer)
    questions = {}
    for leg in legs:
        coverage = leg["coverage"]
        if coverage["reply_ref"]:
            identity = coverage["reply_ref"]["evidence_id"]
            questions[identity] = {
                "format": QUESTION_FORMAT,
                "delivery_id": "delivery-" + identity,
                "delivered_at": original["source_timestamp"] + 1,
                "transfer_sha256": terms,
                "role_binding_sha256": coverage["binding_sha256"],
            }

    def read_question(_conn, identity):
        return VerifiedTransferOpeningReplyV1(identity, canonical(questions[identity]))

    port.transfer_opening_reply_v1 = read_question
    return questions


def test_checks_actual_replies_and_rejects_reusing_a_question_for_the_other_role(live, transfer):
    ledger, port, daily, _, _ = setup(live)
    questions = _retained_questions(ledger, port, transfer)
    with daily._read() as (service, conn):
        require_transfer_opening_replies_v1(service, conn, transfer)
        questions["answer-fee_debit"] = questions["answer-transfer_in"]
        with pytest.raises(LifecycleError, match="exact delivered question"):
            require_transfer_opening_replies_v1(service, conn, transfer)


def test_exact_role_reply_cannot_be_reused_after_other_transfer_terms_change(live, transfer):
    ledger, port, daily, _, _ = setup(live)
    _retained_questions(ledger, port, transfer)
    changed = deepcopy(transfer)
    fee = changed["fee"]["charge"]
    fee["amount"] = "2.00"
    fee["coverage"]["binding"]["amount"] = "2.00"
    fee["coverage"]["binding_sha256"] = digest(fee["coverage"]["binding"])
    with daily._read() as (service, conn):
        with pytest.raises(LifecycleError, match="exact delivered question"):
            require_transfer_opening_replies_v1(service, conn, changed)


def _transfer_with_account_snapshots(live, transfer):
    ledger, port, daily, reader, _ = setup(live)
    boundary = _on("2026-11-21")
    out = _account(ledger, port, daily, reader, "SGD", "1", boundary)
    incoming = _account(ledger, port, daily, reader, "MYR", "2", unknown_time())
    fee = transfer["fee"]["charge"]
    fee["funds_time"] = boundary
    for leg, account in ((transfer["outgoing"], out), (transfer["incoming"], incoming), (fee, out)):
        leg["account_ref"] = asdict(account.account_ref)
        leg["opening_ref"] = asdict(account.opening_ref)
        leg["coverage"]["binding"].update(
            {key: leg[key] for key in ("account_ref", "opening_ref", "funds_time")}
        )
    _retained_questions(ledger, port, transfer)
    return ledger, port, daily, reader, out


def test_complete_transfer_preview_shows_per_currency_deltas_without_writing(live, transfer):
    ledger, port, _, reader, _ = _transfer_with_account_snapshots(live, transfer)
    before = reader.collect_accounts()
    preview = read_transfer_create_preview_v1(ledger, transfer)
    assert preview.read_identity == before.read_identity
    assert {
        change.currency: (change.recorded_change, change.balance_change)
        for change in preview.account_changes
    } == {"SGD": ("-101.00", "-100.00"), "MYR": ("330.00", "0.00")}
    assert preview.expense_by_currency == (("SGD", "1.00"),)
    assert ledger.reader().full_audit()["fact_version_count"] == 2

    original = port.sources[transfer["instruction_ref"]["evidence_id"]]
    port.add_source(original.evidence_id, "Different transfer")
    with pytest.raises(LifecycleError, match="authenticated source"):
        read_transfer_create_preview_v1(ledger, transfer)


def test_transfer_draft_keeps_the_original_values_and_replays_without_posting(
    live, transfer, tmp_path
):
    ledger, port, daily, reader, out = _transfer_with_account_snapshots(live, transfer)
    captured = ledger.capture(transfer["instruction_ref"]["evidence_id"], "capture-transfer")
    service = DailyTransferDraftServiceV1(ledger)
    proposal = service.propose_create(captured.source_id, transfer, "result-1", "draft-transfer-1")
    assert proposal.version == 1
    assert (
        service.propose_create(captured.source_id, transfer, "result-1", "draft-transfer-1")
        == proposal
    )
    with pytest.raises(LifecycleError, match="replay mismatch"):
        service.propose_create(captured.source_id, transfer, "result-1", "other-operation")
    with pytest.raises(LifecycleError):
        service.propose_create(captured.source_id, transfer, "result-2", "draft-transfer-2")
    with pytest.raises(LifecycleError, match="Unsupported daily observation"):
        DailyMovementServiceV1(ledger).prepare_review(proposal.proposal_id, proposal.version)
    revised = service.propose_create(
        captured.source_id, transfer, "result-2", "draft-transfer-2", expected_version=1
    )
    with pytest.raises(LifecycleError):
        service.prepare_review(proposal.proposal_id, proposal.version)
    review = service.prepare_review(revised.proposal_id, revised.version)
    assert service.prepare_review(revised.proposal_id, revised.version) == review
    assert review.material["after_values"] == transfer
    assert {role["role"] for role in review.material["account_roles"]} == {
        "transfer_out",
        "transfer_in",
        "fee_debit",
    }
    assert {
        row["currency"]: row["balance_change"] for row in review.material["account_impact_changes"]
    } == {"SGD": "-100.00", "MYR": "0.00"}
    assert review.material["expense_by_currency"] == [["SGD", "1.00"]]
    with pytest.raises(LifecycleError):
        ledger.record_delivery(review.review_id, "legacy-delivery")
    with pytest.raises(LifecycleError):
        ledger.confirm(review.review_id, "legacy-action", "legacy-transfer-confirm")
    with pytest.raises(LifecycleError, match="authenticated review"):
        service.record_delivery(review.review_id, "delivery1")
    delivered = service.record_delivery(review.review_id, "transfer-display")
    assert delivered.delivery_evidence_id == "transfer-display"
    assert service.record_delivery(review.review_id, "transfer-display") == delivered
    with pytest.raises(LifecycleError, match="replay mismatch"):
        service.record_delivery(review.review_id, "other-display")
    assert ledger.reader().full_audit()["fact_version_count"] == 2

    # A later account change does not invalidate the historical proposal in the full audit.
    change(port, daily, out, "account_rename", {"name": "Renamed"}, "rename")
    assert reader.collect_accounts().accounts
    assert service.prepare_review(revised.proposal_id, revised.version) == delivered
    with pytest.raises(LifecycleError, match="account reference is missing or stale"):
        service.confirm(review.review_id, "stale-action", "stale-confirm")
    assert ledger.reader().full_audit()["fact_version_count"] == 3
    snapshot = ledger.seal_snapshot(tmp_path / "transfer-draft.sqlite")
    assert len(snapshot.facts) == 2
    assert {item[0] for item in snapshot.facts} == {
        item.account_id for item in reader.collect_accounts().accounts
    }


def test_transfer_account_effects_report_fee_once_and_require_current_opening(live, transfer):
    ledger, port, daily, reader, out = _transfer_with_account_snapshots(live, transfer)
    ledger_reader = DailyLedgerReaderV1(ledger)
    prior = ledger_reader.collect_with_contributions_v1()
    with_transfers = ledger_reader.collect_with_transfers_v2()
    assert with_transfers.transfers == ()
    assert with_transfers.collection.balances == prior.collection.balances
    assert with_transfers.contributions == prior.contributions
    assert with_transfers.read_identity == prior.read_identity

    def accounts():
        return {account.account_id: account for account in reader.collect_accounts().accounts}

    # The pure projector consumes a synthetic reviewed reference and real protected account views.
    # No transfer fact is inserted before the guarded writer is available.
    ref = BundleFactRefV1("test-transfer", 1, digest(transfer), 20)
    cost, effects = _resolve_current_transfer(transfer, ref, accounts())
    assert cost == (("SGD", "1.00"),)
    assert [
        (row["impact_role"], row["recorded_change"], row["balance_impact"]) for row in effects
    ] == [
        ("transfer_out", "-100.00", "-100.00"),
        ("transfer_in", "330.00", "0.00"),
        ("fee_debit", "-1.00", "0.00"),
    ]
    assert all(row["transfer_fact_ref"] == asdict(ref) for row in effects)

    change(
        port,
        daily,
        out,
        "opening_correct",
        {"amount": "15.50", "as_of": _on("2026-11-22")},
        "fix",
        reason="Correct opening date and amount",
    )
    with pytest.raises(LifecycleError, match="coverage for its current opening"):
        _resolve_current_transfer(transfer, ref, accounts())


def test_commits_one_confirmed_transfer_with_both_legs_and_fee(live, transfer, tmp_path):
    ledger, port, daily, _, out = _transfer_with_account_snapshots(live, transfer)
    capture = ledger.capture(transfer["instruction_ref"]["evidence_id"], "transfer-capture")
    service = DailyTransferDraftServiceV1(ledger)
    proposed = service.propose_create(
        capture.source_id, transfer, "transfer-result", "transfer-draft"
    )
    review = service.prepare_review(proposed.proposal_id, proposed.version)

    def confirm(action_id="transfer-action", operation_id="transfer-confirm"):
        return service.confirm(review.review_id, action_id, operation_id)

    before = ledger.reader().full_audit()["fact_version_count"]
    with pytest.raises(LifecycleError, match="current delivery"):
        confirm()
    service.record_delivery(review.review_id, "transfer-display")
    with pytest.raises(LifecycleError, match="separate human action"):
        confirm("answer-transfer_in")
    port.bad_actor = True
    with pytest.raises(LifecycleError, match="action differs"):
        confirm("other-actor-action")
    port.bad_actor = False
    assert ledger.reader().full_audit()["fact_version_count"] == before

    receipt = confirm()
    assert receipt.version == 1
    assert confirm() == receipt
    with pytest.raises(LifecycleError, match="replay mismatch"):
        confirm("another-action")
    with pytest.raises(LifecycleError):
        confirm("another-action", "another-operation")
    assert ledger.reader().full_audit()["fact_version_count"] == before + 1

    ledger_reader = DailyLedgerReaderV1(ledger)
    with pytest.raises(LifecycleError, match="use the transfer ledger"):
        ledger_reader.collect_with_contributions_v1()
    result = ledger_reader.collect_with_transfers_v2()
    assert len(result.transfers) == 1 and result.transfers[0].values == transfer
    assert result.collection.income_by_currency == ()
    assert result.collection.expense_by_currency == (("SGD", "1.00"),)
    assert len(result.contributions) == 1
    assert {
        b.currency: (b.recorded_net_change, b.balance_amount) for b in result.collection.balances
    } == {"MYR": ("330.00", "100.00"), "SGD": ("-101.00", "0.00")}
    with pytest.raises(LifecycleError, match="unsupported_transfer_opening_dependencies"):
        change(
            port,
            daily,
            out,
            "opening_correct",
            {"amount": "125.00", "as_of": _on("2026-11-22")},
            "later-opening",
            reason="Correct the opening amount",
        )
    assert (
        ledger_reader.collect_with_transfers_v2().collection.balances == result.collection.balances
    )
    snapshot = ledger.seal_snapshot(tmp_path / "confirmed-transfer.sqlite")
    assert len(snapshot.facts) == before + 1
    assert any(
        row[0] == receipt.record_id and row[2] == receipt.fact_sha256 for row in snapshot.facts
    )
