"""Transfer coverage can be read only from the exact delivered role question."""

from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256

import pytest

from finance_core.instance_admission import InstanceAdmissionError
from finance_core.lifecycle_v2 import LifecycleError, VerifiedSource
from finance_core.lifecycle_v2.contracts import canonical, digest
from finance_core.lifecycle_v2.daily_v1 import DailyTransferServiceV1, transfer_drafts_v1
from finance_core.lifecycle_v2.daily_v1.contracts import (
    BundleFactRefV1,
    instruction_ref,
    strict_json,
)
from finance_core.lifecycle_v2.daily_v1.ledger_reader import (
    DailyLedgerReaderV1,
    _resolve_current_transfer,
)
from finance_core.lifecycle_v2.daily_v1.movements import DailyMovementServiceV1
from finance_core.lifecycle_v2.daily_v1.opening_changes_v1 import DailyOpeningChangeServiceV1
from finance_core.lifecycle_v2.daily_v1.transfer_evidence_v1 import (
    CHANGE_TARGET_FORMAT,
    QUESTION_FORMAT,
    VerifiedTransferChangeTargetV1,
    VerifiedTransferOpeningReplyV1,
    _transfer_terms_digest,
    require_transfer_opening_replies_v1,
)
from finance_core.lifecycle_v2.daily_v1.transfer_review_v1 import read_transfer_create_preview_v1
from tests.daily_account_support_v1 import OpeningPort, command, unknown_time
from tests.test_daily_accounts_lifecycle_v1 import change, create, setup
from tests.test_daily_opening_changes_v1 import finish, opening_command, opening_proposal, row_reply
from tests.test_daily_transfer_facts_v1 import transfer as transfer
from tests.test_daily_transfer_opening_v1 import _account, _on
from tests.test_lifecycle_v2 import live as live


def _retained_questions(
    ledger,
    port,
    transfer,
    *,
    raw_instruction="Transfer actual SGD 100.00 to MYR 330.00 with an additional SGD 1.00 fee",
):
    original = transfer["instruction_ref"]
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

    prior = getattr(port, "transfer_opening_reply_v1", None)

    def read_question(conn, identity):
        if identity not in questions and prior is not None:
            return prior(conn, identity)
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


def _transfer_with_account_snapshots(live, transfer, *, opening_port=False):
    ledger, port, daily, reader, _ = setup(live)
    if opening_port:
        port = ledger.port = OpeningPort()
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
    service = DailyTransferServiceV1(ledger)
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


def test_commits_one_confirmed_transfer_with_both_legs_and_fee(
    live, transfer, tmp_path, monkeypatch
):
    ledger, port, daily, _, out = _transfer_with_account_snapshots(live, transfer)
    capture = ledger.capture(transfer["instruction_ref"]["evidence_id"], "transfer-capture")
    service = DailyTransferServiceV1(ledger)
    proposed = service.propose_create(
        capture.source_id, transfer, "transfer-result", "transfer-draft"
    )
    review = service.prepare_review(proposed.proposal_id, proposed.version)

    def confirm(action_id="transfer-action", operation_id="transfer-confirm"):
        return service.confirm(review.review_id, action_id, operation_id)

    before = ledger.reader().full_audit()["fact_version_count"]
    with monkeypatch.context() as denied_paths:
        denied_paths.setattr(
            port,
            "transfer_opening_reply_v1",
            lambda *_: pytest.fail("Native path reached a transfer evidence callback"),
        )
        with pytest.raises(LifecycleError, match="unsupported_record_format"):
            ledger.propose(capture.source_id, "transfer-result", 0, "transfer-draft")
        with pytest.raises(LifecycleError, match="unsupported_record_format"):
            ledger.propose(capture.source_id, "native-result", 1, "native-draft")
    with pytest.raises(LifecycleError, match="current delivery"):
        confirm()
    service.record_delivery(review.review_id, "transfer-display")
    for reused in ("answer-transfer_in", "delivery-answer-transfer_in"):
        with pytest.raises(LifecycleError, match="separate human action"):
            confirm(reused)
    port.bad_actor = True
    with pytest.raises(LifecycleError, match="action differs"):
        confirm("other-actor-action")
    port.bad_actor = False
    assert ledger.reader().full_audit()["fact_version_count"] == before
    before_append = ledger.reader().full_audit()

    def partial_append(conn, plan):
        conn.execute("INSERT INTO l2_events VALUES(?,?,?,?,?,?,?)", plan.event)
        raise RuntimeError("synthetic interruption after event append")

    with monkeypatch.context() as failed_commit:
        failed_commit.setattr(transfer_drafts_v1, "_append_finalization", partial_append)
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            confirm()
    assert ledger.reader().full_audit() == before_append

    def persistent_action_write(conn, identity, expected):
        with pytest.raises(InstanceAdmissionError):
            conn.execute("CREATE TABLE transfer_test_forbidden(value INTEGER)")

    with monkeypatch.context() as denied_action:
        denied_action.setattr(port, "on_action", persistent_action_write)
        with pytest.raises(InstanceAdmissionError):
            confirm("callback-write")
    assert ledger.reader().full_audit() == before_append

    receipt = confirm()
    assert receipt.version == 1
    with monkeypatch.context() as denied_paths:
        denied_paths.setattr(
            port,
            "transfer_opening_reply_v1",
            lambda *_: pytest.fail("Native correction reached a transfer evidence callback"),
        )
        with pytest.raises(LifecycleError, match="unsupported_record_format"):
            ledger.prepare_correction(
                receipt.record_id, 1, "native-correction", "native-correction"
            )
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


def test_opening_revision_treats_two_transfer_roles_separately_and_survives_rename(
    live, transfer, tmp_path
):
    ledger, port, daily, reader, out = _transfer_with_account_snapshots(
        live, transfer, opening_port=True
    )
    account_source = ledger.capture("source1", "capture1")
    account_proposal = daily.propose_account_create(account_source.source_id, "obs1", 0, "propose1")
    captured = ledger.capture(transfer["instruction_ref"]["evidence_id"], "payment-capture")
    transfers = DailyTransferServiceV1(ledger)
    draft = transfers.propose_create(
        captured.source_id, transfer, "payment-draft", "payment-propose"
    )
    review = transfers.prepare_review(draft.proposal_id, draft.version)
    transfers.record_delivery(review.review_id, "payment-card")
    transfer_receipt = transfers.confirm(
        review.review_id, "payment-confirmation", "payment-confirm"
    )
    opening = DailyOpeningChangeServiceV1(ledger)
    boundary = _on("2026-11-22")
    pending = opening_proposal(
        ledger,
        port,
        opening,
        account_source,
        account_proposal,
        out,
        "opening-correction",
        opening_command(amount="125", boundary=boundary),
    )
    packet = opening.opening_questions_v1(pending.proposal_id)
    assert len(packet["subjects"]) == 2
    roles = {subject["impact_role"]: subject for subject in packet["subjects"]}
    assert set(roles) == {"transfer_out", "fee_debit"}
    assert all(
        subject["movement_fact_ref"]["fact_sha256"] == transfer_receipt.fact_sha256
        and subject["account_currency"] == "SGD"
        for subject in roles.values()
    )
    assert {key: subject["account_amount"] for key, subject in roles.items()} == {
        "transfer_out": "100.00",
        "fee_debit": "1.00",
    }
    with pytest.raises(LifecycleError, match="incomplete_proposal"):
        opening.prepare_review(pending.proposal_id, pending.version)
    original_question = port.deliver_opening_question(packet, "transfer-roles")
    answers = row_reply(
        port, "role-reply", (roles["transfer_out"], "included"), (roles["fee_debit"], "excluded")
    )
    complete = opening_proposal(
        ledger,
        port,
        opening,
        account_source,
        pending,
        out,
        "role-reply",
        opening_command(amount="125", boundary=boundary, answers=answers),
        "opening-correction",
        original_question,
    )
    card, amended = finish(opening, complete, "role-reply")
    effects = card.material["opening_effects"]
    assert card.material["format"] == "finance-daily-opening-review-v3"
    assert effects["before_balance"] == "0.00" and effects["after_balance"] == "124.00"
    assert effects["statistics_changed"] is False
    mapping = card.material["after_values"]["opening_dependency_map"]
    assert mapping["format"] == "finance-daily-opening-dependency-map-v2"
    assert {entry["impact_role"]: entry["treatment"] for entry in mapping["entries"]} == {
        "transfer_out": "included",
        "fee_debit": "excluded",
    }
    renamed = change(
        port,
        daily,
        reader.get_account(out.account_id),
        "account_rename",
        {"name": "Wallet"},
        "wallet",
    )
    assert renamed.receipt.version == amended.receipt.version + 1
    combined = DailyLedgerReaderV1(ledger).collect_with_transfers_v2()
    assert {b.currency: b.balance_amount for b in combined.collection.balances} == {
        "SGD": "124.00",
        "MYR": "100.00",
    }
    assert combined.collection.expense_by_currency == (("SGD", "1.00"),)
    mapped = {
        row.values["impact_role"]: row.values
        for row in combined.effects
        if row.values["source"] == "map"
    }
    assert {role: row["balance_impact"] for role, row in mapped.items()} == {
        "transfer_out": "0.00",
        "fee_debit": "-1.00",
    }
    assert all(
        row["map_owner_fact_ref"]["fact_sha256"] == amended.receipt.fact_sha256
        for row in mapped.values()
    )
    assert (
        transfers.confirm(review.review_id, "payment-confirmation", "payment-confirm")
        == transfer_receipt
    )
    assert len(ledger.seal_snapshot(tmp_path / "opening-map-transfer.sqlite").facts) == 3


def test_whole_transfer_reversal_preserves_original_and_booking_once(live, transfer, tmp_path):
    ledger, port, daily, _, out = _transfer_with_account_snapshots(live, transfer)
    capture = ledger.capture(transfer["instruction_ref"]["evidence_id"], "capture-transfer")
    service = DailyTransferServiceV1(ledger)
    proposed = service.propose_create(
        capture.source_id, transfer, "proposed-transfer", "propose-op"
    )
    first_card = service.prepare_review(proposed.proposal_id, proposed.version)
    service.record_delivery(first_card.review_id, "original-display")
    posted = service.confirm(first_card.review_id, "original-action", "original-confirm")
    previous = BundleFactRefV1(
        posted.record_id, posted.version, posted.fact_sha256, posted.commit_seq
    )
    ledger_reader = DailyLedgerReaderV1(ledger)
    original = ledger_reader.collect_with_transfers_v2().transfers[0]

    reason = "duplicate transfer"
    raw = "Reverse both legs and fee because of a " + reason
    quote = [raw.index(reason), len(raw)]
    response = port.add_source("requested-reversal", raw)
    original_ref = instruction_ref(asdict(response), ledger.binding)
    values = {
        **original.values,
        "instruction_ref": original_ref,
        "transition": "transfer_void",
        "status": "VOID",
        "reason_ref": {
            "format": "finance-daily-reason-ref-v1",
            "instruction_ref": original_ref,
            "span": quote,
            "quote": reason,
            "quote_sha256": sha256(reason.encode()).hexdigest(),
        },
    }
    association = {
        "format": CHANGE_TARGET_FORMAT,
        "instruction_ref": original_ref,
        "target_fact_ref": asdict(previous),
        "transition": "transfer_void",
        "reason_span": quote,
    }
    port.transfer_change_target_v1 = lambda _conn, eid: VerifiedTransferChangeTargetV1(
        "selected-" + eid, canonical(association)
    )
    association["transition"] = "transfer_correct"
    with pytest.raises(LifecycleError, match="retained target or reason"):
        service.propose_change(
            capture.source_id,
            previous,
            values,
            "void",
            "propose-void",
            expected_version=proposed.version,
        )
    association["transition"] = "transfer_void"
    proposed = service.propose_change(
        capture.source_id,
        previous,
        values,
        "void",
        "propose-void",
        expected_version=proposed.version,
    )
    card = service.prepare_review(proposed.proposal_id, proposed.version)
    changes = card.material["account_impact_changes"]
    assert {row["currency"]: row["balance_change"] for row in changes} == {
        "SGD": "100.00",
        "MYR": "0.00",
    }
    assert card.material["before_expense_by_currency"] == [["SGD", "1.00"]]
    assert card.material["expense_by_currency"] == []
    service.record_delivery(card.review_id, "void-display")
    with pytest.raises(LifecycleError, match="separate human action"):
        service.confirm(card.review_id, "selected-requested-reversal", "void-confirm")
    rename = change(port, daily, out, "account_rename", {"name": "Updated bank"}, "renamed-bank")
    with pytest.raises(
        LifecycleError, match="review no longer describes the exact account effects"
    ):
        service.confirm(card.review_id, "stale-void-action", "stale-void-confirm")
    assert ledger_reader.collect_with_transfers_v2().transfers[0].values == original.values
    proposal_after_rename = service.propose_change(
        capture.source_id,
        previous,
        values,
        "new-void",
        "propose-new-void",
        expected_version=proposed.version,
    )
    card = service.prepare_review(proposal_after_rename.proposal_id, proposal_after_rename.version)
    assert card.material["before_account_roles"][0]["account_ref"] == asdict(rename.account_ref)
    service.record_delivery(card.review_id, "new-void-display")
    voided = service.confirm(card.review_id, "void-action", "void-confirm")
    assert (voided.record_id, voided.version) == (posted.record_id, 2)
    assert service.confirm(card.review_id, "void-action", "void-confirm") == voided
    assert service.confirm(first_card.review_id, "original-action", "original-confirm") == posted
    assert (
        service.lookup_commit_receipt(
            "original-confirm", review_id=first_card.review_id, action_evidence_id="original-action"
        )
        == posted
    )
    history = ledger_reader.transfer_history_v1(posted.record_id)
    assert [view.fact_ref.fact_sha256 for view in history] == [
        posted.fact_sha256,
        voided.fact_sha256,
    ]
    assert [view.values["status"] for view in history] == ["ACTIVE", "VOID"]
    result = ledger_reader.collect_with_transfers_v2()
    assert len(result.transfers) == 1
    assert result.transfers[0].economic_event_id == original.economic_event_id
    assert result.transfers[0].source_id == original.source_id
    assert result.transfers[0].values == values
    assert not result.effects and not result.collection.expense_by_currency
    assert {b.currency: b.balance_amount for b in result.collection.balances} == {
        "SGD": "100.00",
        "MYR": "100.00",
    }
    snapshot = ledger.seal_snapshot(tmp_path / "voided-transfer.sqlite")
    assert (voided.record_id, 2, voided.fact_sha256) in snapshot.facts


def test_correcting_both_transfer_legs_requires_new_opening_replies(live, transfer):
    ledger, port, _, _, _ = _transfer_with_account_snapshots(live, transfer)
    capture = ledger.capture(transfer["instruction_ref"]["evidence_id"], "transfer-origin")
    service = DailyTransferServiceV1(ledger)
    initial = service.propose_create(capture.source_id, transfer, "initial-model", "initial-draft")
    first_card = service.prepare_review(initial.proposal_id, initial.version)
    service.record_delivery(first_card.review_id, "initial-display")
    first = service.confirm(first_card.review_id, "initial-action", "initial-confirm")
    previous = BundleFactRefV1(first.record_id, first.version, first.fact_sha256, first.commit_seq)
    reason = "wrong amounts"
    raw = "Correct the transfer to SGD 115.00 and MYR 348.00 due to " + reason
    response = port.add_source("transfer-correction", raw)
    ref = instruction_ref(asdict(response), ledger.binding)
    span = [raw.index(reason), len(raw)]
    changed = {**deepcopy(transfer), "transition": "transfer_correct", "instruction_ref": ref}
    changed["reason_ref"] = {
        "format": "finance-daily-reason-ref-v1",
        "instruction_ref": ref,
        "span": span,
        "quote": reason,
        "quote_sha256": sha256(reason.encode()).hexdigest(),
    }
    changed["outgoing"]["amount"] = "115.00"
    changed["incoming"]["amount"] = "348.00"
    legs = (changed["outgoing"], changed["incoming"], changed["fee"]["charge"])
    for leg in legs:
        coverage = leg["coverage"]
        coverage["binding"].update(amount=leg["amount"], instruction_ref=ref)
        coverage["binding_sha256"] = digest(coverage["binding"])
    with pytest.raises(LifecycleError, match="independent authenticated reply"):
        service.propose_change(
            capture.source_id,
            previous,
            changed,
            "old-answers",
            "old-answers",
            expected_version=initial.version,
        )
    for leg in legs:
        prior = leg["coverage"]["reply_ref"]
        if prior is not None:
            prior["evidence_id"] += "-corrected"
            prior["source_key"] += "-corrected"
    _retained_questions(ledger, port, changed, raw_instruction=raw)
    actual = VerifiedTransferChangeTargetV1(
        "corrected-transfer-target",
        canonical(
            {
                "format": CHANGE_TARGET_FORMAT,
                "instruction_ref": ref,
                "target_fact_ref": asdict(previous),
                "transition": "transfer_correct",
                "reason_span": span,
            }
        ),
    )
    correction_links = {response.evidence_id: actual}
    port.transfer_change_target_v1 = lambda _conn, eid: correction_links[eid]
    proposed = service.propose_change(
        capture.source_id,
        previous,
        changed,
        "corrected-model",
        "corrected-draft",
        expected_version=initial.version,
    )
    card = service.prepare_review(proposed.proposal_id, proposed.version)
    assert {
        row["currency"]: row["balance_change"] for row in card.material["account_impact_changes"]
    } == {"SGD": "-15.00", "MYR": "0.00"}
    assert card.material["before_expense_by_currency"] == card.material["expense_by_currency"]
    service.record_delivery(card.review_id, "corrected-display")
    receipt = service.confirm(card.review_id, "corrected-action", "corrected-confirm")
    assert (receipt.record_id, receipt.version) == (first.record_id, 2)
    assert service.confirm(card.review_id, "corrected-action", "corrected-confirm") == receipt
    result = DailyLedgerReaderV1(ledger).collect_with_transfers_v2()
    assert len(result.transfers) == 1 and result.transfers[0].values == changed
    assert result.collection.expense_by_currency == (("SGD", "1.00"),)
    assert {b.currency: b.balance_amount for b in result.collection.balances} == {
        "SGD": "-15.00",
        "MYR": "100.00",
    }

    old_read_target = port.transfer_change_target_v1
    reason = "duplicated entry"
    response = port.add_source("second-change", "Reverse this " + reason)
    ref = instruction_ref(asdict(response), ledger.binding)
    span = [len("Reverse this "), len(response.raw_input)]
    reversal = {
        **changed,
        "transition": "transfer_void",
        "status": "VOID",
        "instruction_ref": ref,
        "reason_ref": {
            "format": "finance-daily-reason-ref-v1",
            "instruction_ref": ref,
            "span": span,
            "quote": reason,
            "quote_sha256": sha256(reason.encode()).hexdigest(),
        },
    }
    target = result.transfers[0].fact_ref
    association = VerifiedTransferChangeTargetV1(
        "second-change-selection",
        canonical(
            {
                "format": CHANGE_TARGET_FORMAT,
                "instruction_ref": ref,
                "target_fact_ref": asdict(target),
                "transition": "transfer_void",
                "reason_span": span,
            }
        ),
    )

    def both_targets(conn, evidence_id):
        return (
            association
            if evidence_id == response.evidence_id
            else old_read_target(conn, evidence_id)
        )

    port.transfer_change_target_v1 = both_targets
    reverse = service.propose_change(
        capture.source_id,
        target,
        reversal,
        "last-draft",
        "last-propose",
        expected_version=proposed.version,
    )
    card = service.prepare_review(reverse.proposal_id, reverse.version)
    service.record_delivery(card.review_id, "last-display")
    for earlier in ("original-transfer", "answer-transfer_in", "delivery-answer-fee_debit"):
        with pytest.raises(LifecycleError, match="separate human action"):
            service.confirm(card.review_id, earlier, "last-confirm")
    last = service.confirm(card.review_id, "last-action", "last-confirm")
    assert (last.record_id, last.version) == (first.record_id, 3)
    history = DailyLedgerReaderV1(ledger).transfer_history_v1(last.record_id)
    assert [row.fact_ref.fact_sha256 for row in history] == [
        first.fact_sha256,
        receipt.fact_sha256,
        last.fact_sha256,
    ]


def test_credit_card_repayment_moves_cash_and_reduces_debt_without_an_expense(live, transfer):
    ledger, port, daily, reader, _ = setup(live)
    boundary = _on("2026-11-21")
    bank = _account(ledger, port, daily, reader, "SGD", "1", boundary)
    port.add_source("source2", "Credit card opening debt of SGD 80.00")
    _, _, _, card_receipt = create(
        ledger,
        port,
        daily,
        command(
            fields={
                "name": "Credit card",
                "account_type": "credit_card",
                "currency": "SGD",
                "opening": {"amount": "-80.00", "as_of": boundary},
            }
        ),
        suffix="2",
    )
    card = reader.get_account(card_receipt.account_ref.account_id)
    source = port.add_source("repayment", "Pay SGD 50.00 from the bank to the credit card")
    ref = instruction_ref(asdict(source), ledger.binding)
    transfer.update(instruction_ref=ref, fee={"mode": "none", "charge": None})
    for leg, account in ((transfer["outgoing"], bank), (transfer["incoming"], card)):
        leg.update(
            currency="SGD",
            amount="50.00",
            account_ref=asdict(account.account_ref),
            opening_ref=asdict(account.opening_ref),
        )
        coverage = leg["coverage"]
        coverage["binding"].update(
            {key: leg[key] for key in ("currency", "amount", "account_ref", "opening_ref")},
            instruction_ref=ref,
        )
        coverage.update(
            treatment="excluded",
            basis="strictly_later",
            reply_ref=None,
            binding_sha256=digest(coverage["binding"]),
        )
    capture = ledger.capture(source.evidence_id, "capture-repayment")
    transfers = DailyTransferServiceV1(ledger)
    draft = transfers.propose_create(
        capture.source_id, transfer, "repayment-values", "repayment-draft"
    )
    review = transfers.prepare_review(draft.proposal_id, draft.version)
    transfers.record_delivery(review.review_id, "repayment-display")
    transfers.confirm(review.review_id, "repayment-action", "repayment-confirm")
    result = DailyLedgerReaderV1(ledger).collect_with_transfers_v2()
    balances = {item.account_id: item for item in result.collection.balances}
    assert balances[bank.account_id].balance_amount == "50.00"
    assert balances[card.account_id].balance_amount == "-30.00"
    assert balances[card.account_id].credit_card_debt == "30.00"
    assert not result.collection.income_by_currency and not result.collection.expense_by_currency


@pytest.mark.parametrize("field", ["delivery_id", "delivered_at"])
def test_full_audit_refuses_changed_opening_question_after_transfer_proposal(
    live, transfer, monkeypatch, field
):
    ledger, port, _, _, _ = _transfer_with_account_snapshots(live, transfer)
    capture = ledger.capture(transfer["instruction_ref"]["evidence_id"], "sealed-transfer")
    service = DailyTransferServiceV1(ledger)
    service.propose_create(capture.source_id, transfer, "sealed-values", "sealed-draft")
    verified = ledger.reader().full_audit()
    original = port.transfer_opening_reply_v1

    def rewritten(conn, identity):
        answer = original(conn, identity)
        if identity == "answer-transfer_in":
            question = strict_json(answer.question_json)
            question[field] = (
                "different-earlier-question" if field == "delivery_id" else question[field] + 1
            )
            return replace(answer, question_json=canonical(question))
        return answer

    with monkeypatch.context() as replaced:
        replaced.setattr(port, "transfer_opening_reply_v1", rewritten)
        with pytest.raises(LifecycleError, match="questions differ from their original deliveries"):
            ledger.reader().full_audit()
    assert ledger.reader().full_audit() == verified


def test_one_delivered_transfer_question_cannot_cover_multiple_roles(live, transfer):
    ledger, port, daily, _, _ = setup(live)
    questions = _retained_questions(ledger, port, transfer)
    questions["answer-fee_debit"]["delivery_id"] = questions["answer-transfer_in"]["delivery_id"]
    with daily._read() as (service, conn):
        with pytest.raises(LifecycleError, match="distinct original question deliveries"):
            require_transfer_opening_replies_v1(service, conn, transfer)
