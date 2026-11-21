"""Ordinary admitted public movement flows over genuine synthetic evidence."""

from dataclasses import asdict

import pytest

from finance_core.lifecycle_v2 import LifecycleError
from finance_core.lifecycle_v2.daily_v1 import (
    DailyLedgerReaderV1,
    DailyMovementServiceV1,
)
from tests.daily_account_support_v1 import command, unknown_time
from tests.test_daily_accounts_lifecycle_v1 import create, setup
from tests.test_lifecycle_v2 import live as live


def prepare(live, opening="1000", account_type="bank", currency="MYR", *, opening_as_of=None):
    ledger, port, daily, accounts, _ = setup(live)
    _, _, _, receipt = create(
        ledger,
        port,
        daily,
        command(
            fields={
                "name": "Bank",
                "account_type": account_type,
                "currency": currency,
                "opening": {"amount": opening, "as_of": opening_as_of or unknown_time()}
                if opening is not None
                else None,
            }
        ),
    )
    return (
        ledger,
        port,
        daily,
        DailyMovementServiceV1(ledger),
        DailyLedgerReaderV1(ledger),
        receipt.account_ref.account_id,
    )


def fields(account, amount="9", kind="expense", original="9", currency="MYR", coverage="excluded"):
    return {
        "kind": kind,
        "business_date": "2026-11-17",
        "description": "Coffee" if kind == "expense" else "Salary",
        "category": "food" if kind == "expense" else "salary",
        "account_id": account,
        "account_amount": amount,
        "original_amount": amount if kind == "income" else original,
        "original_currency": currency,
        "funds_time": unknown_time(),
        "coverage": {"answer": coverage, "span": [0, len(coverage)]},
    }


def opening_subject(values, account):
    from finance_core.lifecycle_v2.daily_v1.contracts import _money
    from finance_core.lifecycle_v2.daily_v1.movement_contracts import coverage_subject

    currency = account.currency
    original_currency = values["original_currency"] if values["kind"] == "expense" else currency
    actual = _money(values["account_amount"], currency)
    normalized = {
        **values,
        "account_currency": currency,
        "account_amount": actual,
        "original_currency": original_currency,
        "original_amount": _money(values["original_amount"], original_currency)
        if values["kind"] == "expense"
        else actual,
    }
    return coverage_subject(normalized, account.values)


def movement(
    ledger,
    port,
    api,
    account,
    identity,
    values=None,
    target=None,
    kind="movement_create",
    reason="wrong amount",
):
    raw = "excluded; " + reason
    port.add_source(identity, raw)
    cmd = {
        "format": "finance-daily-movement-command-v1",
        "command_kind": kind,
        "fields": fields(account) if values is None else values,
        "reason_span": [raw.index(reason), len(raw)] if kind != "movement_create" else None,
    }
    port.commands[identity] = cmd
    if target is None:
        source = ledger.capture(identity, "capture-" + identity)
        port.remember_movement_source(source, identity)
        port.register_movement_instruction(ledger.binding, source.source_id, identity)
        proposal = api.propose_movement_create(source.source_id, identity, 0, "propose-" + identity)
    else:
        port.register_movement_instruction(
            ledger.binding, target.source_id, identity, kind, target.fact_ref
        )
        proposal = api.propose_movement_change(
            target.record_id,
            identity,
            target.fact_ref,
            "propose-" + identity,
            reply_evidence_id=identity,
            command_kind=kind,
        )
    port.remember_movement_proposal(proposal)
    review = api.prepare_review(proposal.proposal_id, proposal.version)
    api.record_delivery(review.review_id, "delivery-" + identity)
    receipt = api.confirm(review.review_id, "action-" + identity, "confirm-" + identity)
    return proposal, review, receipt


def test_normal_income_expense_one_balance_and_original_receipts(live):
    ledger, port, _, api, reader, account = prepare(live)
    _, review, first = movement(
        ledger, port, api, account, "income", fields(account, "200", "income")
    )
    _, _, expense = movement(ledger, port, api, account, "expense")
    original_read = reader.collect_with_contributions_v1()
    result = original_read.collection
    assert result.balances[0].balance_amount == "1191.00"
    assert {(part.kind, part.currency, part.amount) for part in original_read.contributions} == {
        ("income", "MYR", "200.00"),
        ("expense", "MYR", "9.00"),
    }
    assert {
        (part.fact_ref.record_id, part.fact_ref.version) for part in original_read.contributions
    } == {(first.receipt.record_id, 1), (expense.receipt.record_id, 1)}
    assert result.income_by_currency == (("MYR", "200.00"),)
    assert result.expense_by_currency == (("MYR", "9.00"),)
    assert result.read_identity.event_seq > 0 and result.complete
    assert api.confirm(review.review_id, "action-income", "confirm-income") == first
    current = reader.detail(expense.receipt.record_id)
    _, _, corrected = movement(
        ledger, port, api, account, "correct", fields(account, "10"), current, "movement_correct"
    )
    corrected_read = reader.collect_with_contributions_v1()
    assert corrected_read.collection.balances[0].balance_amount == "1190.00"
    repaired = [detail for detail in corrected_read.contributions if detail.kind == "expense"]
    assert len(repaired) == 1
    assert repaired[0].fact_ref.version == corrected.receipt.version == 2
    assert repaired[0].fact_ref.fact_sha256 == corrected.receipt.fact_sha256
    assert repaired[0].amount == "9.00"
    assert corrected_read.collection.expense_by_currency == (("MYR", "9.00"),)
    assert corrected_read.read_identity != original_read.read_identity
    current = reader.detail(corrected.receipt.record_id)
    movement(ledger, port, api, account, "void", {}, current, "movement_void", "duplicate coffee")
    voided_read = reader.collect_with_contributions_v1()
    result = voided_read.collection
    assert result.balances[0].balance_amount == "1200.00"
    assert voided_read.contributions == tuple(
        detail for detail in original_read.contributions if detail.kind == "income"
    )
    assert voided_read.read_identity != corrected_read.read_identity
    assert result.expense_by_currency == ()
    assert len(reader.history(current.record_id)) == 3
    assert api.confirm(review.review_id, "action-income", "confirm-income") == first
    with pytest.raises(LifecycleError, match="already_void"):
        movement(
            ledger,
            port,
            api,
            account,
            "again",
            {},
            reader.detail(current.record_id),
            "movement_void",
        )


def test_held_copy_verifies_typed_history_with_exact_source_binding(live, tmp_path, monkeypatch):
    from dataclasses import replace

    from finance_core.instance_admission import (
        InstanceAdmissionError,
        acquire_production_owner,
        inspect_instance_admission,
    )
    from finance_core.lifecycle_v2 import RuntimeBinding
    from finance_core.lifecycle_v2.recovery import (
        prepare_restored_profile,
        verify_held_profile_evidence,
        verify_snapshot_evidence,
    )
    from tests.instance_admission_support_v1 import synthetic_config

    ledger, port, _, api, reader, account = prepare(live, opening="100", currency="SGD")
    movement(
        ledger, port, api, account, "expense", fields(account, "8", original="8", currency="SGD")
    )
    assert reader.collect().balances[0].balance_amount == "92.00"
    snapshot = tmp_path / "typed-ledger.sqlite"
    cut = ledger.seal_snapshot(snapshot)
    expected = verify_snapshot_evidence(snapshot, port, ledger.binding, cut)
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    with synthetic_config(other, monkeypatch, instance_id="new-typed-instance") as config:
        target = RuntimeBinding(
            config.instance_id,
            "new-generation",
            ledger.binding.principal_id,
            ledger.binding.client_id,
        )
        restored = prepare_restored_profile(
            config,
            snapshot,
            cut,
            new_binding=target,
            operation_id="typed-restore",
            approval_ref="approved",
        )
        with pytest.raises(LifecycleError):
            verify_held_profile_evidence(restored, port, target)
        for source in (
            replace(ledger.binding, generation="unrelated"),
            replace(ledger.binding, client_id="unrelated"),
            replace(ledger.binding, instance_id=target.instance_id),
        ):
            with pytest.raises(LifecycleError):
                verify_held_profile_evidence(restored, port, target, source_runtime_binding=source)
        with pytest.raises(LifecycleError):
            verify_held_profile_evidence(
                restored,
                port,
                replace(target, generation="different-target"),
                source_runtime_binding=ledger.binding,
            )
        with pytest.raises((LifecycleError, InstanceAdmissionError)):
            verify_held_profile_evidence(
                replace(restored, restore_prepare_operation_id="different-preparation"),
                port,
                target,
                source_runtime_binding=ledger.binding,
            )
        assert (
            verify_held_profile_evidence(
                restored, port, target, source_runtime_binding=ledger.binding
            )
            == expected
        )
        assert inspect_instance_admission(config)["state"] == "RESTORE_HELD"
        with pytest.raises(InstanceAdmissionError):
            acquire_production_owner(config)


def test_equal_expenses_contribute_separate_record_versions_to_one_total(live):
    ledger, port, _, api, reader, account = prepare(live)
    _, _, first = movement(ledger, port, api, account, "first-coffee")
    _, _, second = movement(ledger, port, api, account, "second-coffee")
    snapshot = reader.collect_with_contributions_v1()

    assert snapshot.collection.expense_by_currency == (("MYR", "18.00"),)
    assert snapshot.collection.balances[0].balance_amount == "982.00"
    assert len(snapshot.contributions) == 2
    assert {item.fact_ref.record_id for item in snapshot.contributions} == {
        first.receipt.record_id,
        second.receipt.record_id,
    }
    assert all(
        (item.kind, item.amount, item.currency) == ("expense", "9.00", "MYR")
        for item in snapshot.contributions
    )


def test_account_and_movement_share_confirmation_capacity(live, monkeypatch):
    from finance_core.lifecycle_v2.daily_v1 import accounts

    monkeypatch.setattr(accounts, "MAX_RECORDS", 2)
    ledger, port, daily, api, reader, account = prepare(live)
    _, _, expense = movement(ledger, port, api, account, "expense")

    with pytest.raises(LifecycleError, match="complete_read_limit_exceeded"):
        movement(ledger, port, api, account, "over-capacity")
    port.add_source("source2", "Create another bank account")
    with pytest.raises(LifecycleError, match="complete_read_limit_exceeded"):
        create(ledger, port, daily, suffix="2")

    current = reader.detail(expense.receipt.record_id)
    assert len(reader.collect().accounts) == 1
    assert reader.collect().balances[0].balance_amount == "991.00"

    movement(
        ledger,
        port,
        api,
        account,
        "correct-at-capacity",
        fields(account, "10"),
        current,
        "movement_correct",
    )
    assert len(reader.history(current.record_id)) == 2
    assert reader.collect().balances[0].balance_amount == "990.00"


def test_synthetic_capacity_profile_covers_write_lookup_and_snapshot(live, monkeypatch, tmp_path):
    from finance_core.lifecycle_v2.contracts import DailyCapacityV2
    from finance_core.lifecycle_v2.daily_v1 import accounts
    from finance_core.lifecycle_v2.recovery import verify_snapshot_evidence
    from finance_core.lifecycle_v2.service import LedgerService

    monkeypatch.setattr(accounts, "MAX_RECORDS", 2)
    legacy, port, _, _, legacy_reader, account = prepare(live)
    capacity = DailyCapacityV2.SYNTHETIC_512
    ledger = LedgerService(
        legacy.owner,
        port,
        legacy.binding,
        legacy.clock,
        legacy_verifier=legacy.legacy_verifier,
        runtime_admission=legacy.runtime_admission,
        daily_capacity=capacity,
    )
    api = DailyMovementServiceV1(ledger)
    movement(ledger, port, api, account, "first")
    proposal, _, _ = movement(ledger, port, api, account, "second")

    assert api.pending_for_source(proposal.source_id) is None
    reader = DailyLedgerReaderV1(ledger)
    assert reader.collect().balances[0].balance_amount == "982.00"
    query = reader.collect_with_contributions_v1()
    assert query.collection.balances[0].balance_amount == "982.00"
    assert len(query.contributions) == 2
    with pytest.raises(LifecycleError, match="complete_read_limit_exceeded"):
        legacy_reader.collect_with_contributions_v1()

    database = tmp_path.resolve() / "snapshot.sqlite"
    cut = ledger.seal_snapshot(database)
    current = ledger.reader().full_audit()
    copied = verify_snapshot_evidence(database, port, ledger.binding, cut, daily_capacity=capacity)
    assert current == copied and len(cut.facts) == 3


def test_unknown_opening_reports_net_change_without_balance(live):
    ledger, port, _, api, reader, account = prepare(live, None)
    movement(ledger, port, api, account, "income", fields(account, "200", "income"))
    movement(ledger, port, api, account, "expense")
    result = reader.collect()
    assert result.balances[0].balance_amount is None
    assert result.balances[0].recorded_net_change == "191.00"
    assert "unknown_opening" in result.completeness_gaps


def test_dual_amount_and_credit_debt(live):
    ledger, port, _, api, reader, account = prepare(live, "-300", "credit_card", currency="SGD")
    movement(
        ledger, port, api, account, "jpy", fields(account, "9", original="1000", currency="JPY")
    )
    snapshot = reader.collect_with_contributions_v1()
    (contribution,) = snapshot.contributions
    assert (contribution.kind, contribution.amount, contribution.currency) == (
        "expense",
        "1000",
        "JPY",
    )
    result = snapshot.collection
    assert result.expense_by_currency == (("JPY", "1000"),)
    assert result.balances[0].balance_amount == "-309.00"
    assert result.balances[0].credit_card_debt == "309.00"
    assert result.balances[0].currency == "SGD"


def test_opening_coverage_draft_then_separate_complete_card_and_nonempty_refusal(live):
    from tests.test_daily_accounts_lifecycle_v1 import change

    ledger, port, daily, api, reader, account = prepare(live)
    port.add_source("backfill", "Older cash purchase, not sure if included")
    source = ledger.capture("backfill", "capture-backfill")
    port.remember_movement_source(source, "backfill")
    port.register_movement_instruction(ledger.binding, source.source_id, "backfill")
    pending_fields = fields(account, coverage="unknown")
    pending_fields["coverage"]["span"] = None
    port.commands["draft"] = {
        "format": "finance-daily-movement-command-v1",
        "command_kind": "movement_create",
        "fields": pending_fields,
        "reason_span": None,
    }
    draft = api.propose_movement_create(source.source_id, "draft", 0, "propose-draft")
    port.remember_movement_proposal(draft)
    assert api.pending()[0]["values"]["missing_fields"] == ["opening_coverage"]
    with pytest.raises(LifecycleError, match="incomplete_proposal"):
        api.prepare_review(draft.proposal_id, draft.version)
    assert reader.collect().balances[0].balance_amount == "1000.00"
    question = port.register_opening_question(
        ledger.binding,
        source.source_id,
        "coverage-question",
        opening_subject(fields(account), reader.get_account(account)),
    )
    port.add_source("answer", "included")
    port.register_opening_reply(question, "answer")
    complete_fields = fields(account, coverage="included")
    port.commands["answer"] = {
        "format": "finance-daily-movement-command-v1",
        "command_kind": "movement_create",
        "fields": complete_fields,
        "reason_span": None,
    }
    proposal = api.propose_movement_create(
        source.source_id, "answer", 1, "propose-answer", reply_evidence_id="answer"
    )
    port.remember_movement_proposal(proposal)
    card = api.prepare_review(proposal.proposal_id, proposal.version)
    assert card.material["account_impact_changes"][0]["balance_delta"] == "0.00"
    api.record_delivery(card.review_id, "delivery-answer")
    with pytest.raises(LifecycleError):
        api.confirm(card.review_id, "answer", "cannot-mix-reply-and-confirm")
    api.confirm(card.review_id, "separate-confirm", "confirm-answer")
    assert reader.collect().expense_by_currency == (("MYR", "9.00"),)
    assert reader.collect().balances[0].balance_amount == "1000.00"
    with pytest.raises(LifecycleError, match="nonempty_opening_dependencies"):
        change(
            port,
            daily,
            reader.get_account(account),
            "opening_correct",
            {"amount": "900", "as_of": unknown_time()},
            "opening-change",
            "wrong opening",
        )
    assert reader.collect().balances[0].balance_amount == "1000.00"


@pytest.mark.parametrize(("answer", "closing"), [("尚未包含", "90.00"), ("已包含", "100.00")])
def test_correction_keeps_reason_from_before_the_separate_opening_reply(
    live, tmp_path, answer, closing
):
    from finance_core.lifecycle_v2.daily_v1.contracts import _reason
    from finance_core.lifecycle_v2.recovery import verify_snapshot_evidence

    ledger, port, _, api, reader, account = prepare(live, "100", currency="SGD")
    _, _, receipt = movement(
        ledger, port, api, account, "original", fields(account, "8", original="8", currency="SGD")
    )
    record = reader.detail(receipt.receipt.record_id)
    reason = "收据金额有误"
    port.add_source("correction-request", reason)
    port.register_movement_instruction(
        ledger.binding, record.source_id, "correction-request", "movement_correct", record.fact_ref
    )
    original = api.expected_movement_change(
        record.record_id, record.fact_ref, "correction-request", "movement_correct"
    )
    corrected_fields = fields(account, "10", original="10", currency="SGD")
    del corrected_fields["coverage"]
    draft_command = {
        "format": "finance-daily-movement-command-v1",
        "command_kind": "movement_correct",
        "fields": corrected_fields,
        "reason_span": [0, len(reason)],
    }
    port.commands["correction-request"] = draft_command
    draft = api.propose_movement_change(
        record.record_id,
        "correction-request",
        record.fact_ref,
        "submit-correction",
        reply_evidence_id="correction-request",
        command_kind="movement_correct",
    )
    port.remember_movement_proposal(draft)
    assert api.pending_for_source(record.source_id)["values"]["missing_fields"] == [
        "opening_coverage"
    ]
    with pytest.raises(LifecycleError, match="incomplete_proposal: opening_coverage"):
        api.prepare_review(draft.proposal_id, draft.version)

    delivered = port.register_opening_question(
        ledger.binding,
        record.source_id,
        "corrected-debit-coverage",
        opening_subject(corrected_fields, reader.get_account(account)),
        kind="movement_correct",
        fact_ref=record.fact_ref,
    )
    correction = {
        "fields": corrected_fields,
        "reason_ref": _reason(draft_command["reason_span"], original, True),
    }
    if answer == "尚未包含":

        def submit_check(evidence_id, raw, quote, link_correction, *, changed=None):
            port.add_source(evidence_id, raw)
            port.register_movement_reply(delivered, evidence_id, correction=link_correction)
            start = raw.index(quote)
            span = [start, start + len(quote)]
            port.commands[evidence_id] = {
                **draft_command,
                "fields": {
                    **corrected_fields,
                    **(changed or {}),
                    "coverage": {
                        "answer": "included" if quote == "已包含" else "excluded",
                        "span": span,
                    },
                },
                "reason_span": span if link_correction is None else None,
            }
            return api.propose_movement_change(
                record.record_id,
                evidence_id,
                record.fact_ref,
                "submit-" + evidence_id,
                reply_evidence_id=evidence_id,
                command_kind="movement_correct",
            )

        with pytest.raises(LifecycleError, match="complete coverage choice"):
            submit_check("uncertain-coverage", "我不确定是否已包含", "已包含", correction)
        with pytest.raises(LifecycleError, match="original correction evidence"):
            submit_check("unversioned-coverage", "尚未包含", "尚未包含", None)
        with pytest.raises(LifecycleError, match="only supply coverage"):
            submit_check(
                "other-category",
                "尚未包含",
                "尚未包含",
                correction,
                changed={"category": "different"},
            )
        assert api.pending_for_source(record.source_id)["proposal"] == asdict(draft)
        assert reader.collect().balances[0].balance_amount == "92.00"

    port.add_source("later-coverage-reply", answer)
    port.register_movement_reply(
        delivered,
        "later-coverage-reply",
        correction=correction,
    )
    port.commands["later-coverage-reply"] = {
        **draft_command,
        "fields": {
            **corrected_fields,
            "coverage": {
                "answer": "included" if answer == "已包含" else "excluded",
                "span": [0, len(answer)],
            },
        },
        "reason_span": None,
    }
    submission = (record.record_id, "later-coverage-reply", record.fact_ref, "submit-coverage")
    first = api.propose_movement_change(
        *submission, reply_evidence_id="later-coverage-reply", command_kind="movement_correct"
    )
    assert first == api.propose_movement_change(
        *submission, reply_evidence_id="later-coverage-reply", command_kind="movement_correct"
    )
    assert first.version == draft.version + 1
    card = api.prepare_review(first.proposal_id, first.version)
    assert card.material["reason_ref"]["quote"] == reason
    assert card.material["reason_ref"]["instruction_ref"]["evidence_id"] == "correction-request"
    coverage = card.material["after_values"]["coverage"]["reply_ref"]
    assert coverage["quote"] == answer
    assert coverage["instruction_ref"]["evidence_id"] == "later-coverage-reply"
    assert reader.collect().balances[0].balance_amount == "92.00"

    api.record_delivery(card.review_id, "delivery-corrected-after-coverage")
    for evidence_id in ("correction-request", "later-coverage-reply"):
        with pytest.raises(LifecycleError, match="Daily action"):
            api.confirm(card.review_id, evidence_id, "not-confirm-" + evidence_id)
    confirmation = (card.review_id, "confirm-both-sources", "confirm-corrected")
    posted = api.confirm(*confirmation)
    assert api.confirm(*confirmation) == posted
    assert posted.receipt.record_id == record.record_id
    assert posted.receipt.version == 2
    result = reader.collect()
    assert result.balances[0].balance_amount == closing
    assert result.expense_by_currency == (("SGD", "10.00"),)
    assert len(reader.history(record.record_id)) == 2
    path = tmp_path.resolve() / "correction-with-separate-opening-reply.sqlite"
    cut = ledger.seal_snapshot(path)
    assert verify_snapshot_evidence(path, port, ledger.binding, cut) == ledger.reader().full_audit()


def test_exact_source_pending_lookup_survives_owner_reopen(live):
    from finance_core.instance_admission import acquire_production_owner
    from finance_core.lifecycle_v2 import LedgerService

    ledger, port, _, api, _, account = prepare(live)
    _, _, cfg = live
    drafts = []
    for name, missing in (("coffee", "account_id"), ("lunch", "account_amount")):
        port.add_source(name, "excluded; record " + name)
        source = ledger.capture(name, "capture-" + name)
        port.remember_movement_source(source, name)
        port.register_movement_instruction(ledger.binding, source.source_id, name)
        values = fields(account)
        del values[missing]
        port.commands[name] = {
            "format": "finance-daily-movement-command-v1",
            "command_kind": "movement_create",
            "fields": values,
            "reason_span": None,
        }
        proposal = api.propose_movement_create(source.source_id, name, 0, "propose-" + name)
        port.remember_movement_proposal(proposal)
        result = api.pending_for_source(source.source_id)
        assert result["proposal"]["proposal_id"] == proposal.proposal_id
        assert result["values"]["missing_fields"] == [missing]
        drafts.append((source.source_id, result))

    (coffee_id, coffee), (lunch_id, lunch) = drafts
    assert len(api.pending(limit=1)) == 1
    assert api.pending_for_source("no-captured-movement") is None
    ledger.owner.close()
    with acquire_production_owner(cfg) as owner:
        reopened = LedgerService(owner, port, ledger.binding, clock=ledger.clock)
        api = DailyMovementServiceV1(reopened)
        assert api.pending_for_source(coffee_id) == coffee
        assert api.pending_for_source(lunch_id) == lunch
        assert DailyLedgerReaderV1(reopened).collect().expense_by_currency == ()

        reply = "coffee-account-reply"
        port.add_source(reply, "excluded; use my bank account")
        port.register_movement_instruction(reopened.binding, coffee_id, reply)
        port.commands[reply] = {
            "format": "finance-daily-movement-command-v1",
            "command_kind": "movement_create",
            "fields": fields(account),
            "reason_span": None,
        }
        proposal = api.propose_movement_create(
            coffee_id,
            reply,
            coffee["proposal"]["version"],
            "propose-coffee-v2",
            reply_evidence_id=reply,
        )
        port.remember_movement_proposal(proposal)
        current = api.pending_for_source(coffee_id)
        assert current["proposal"]["proposal_id"] == proposal.proposal_id
        assert current["proposal"]["version"] == 2
        assert api.pending_for_source(lunch_id) == lunch
        card = api.prepare_review(proposal.proposal_id, proposal.version)
        api.record_delivery(card.review_id, "coffee-v2-delivery")
        assert api.pending_for_source(coffee_id) == current
        api.confirm(card.review_id, "coffee-v2-confirm", "confirm-coffee-v2")
        assert api.pending_for_source(coffee_id) is None
        assert api.pending_for_source(lunch_id) == lunch
        assert DailyLedgerReaderV1(reopened).collect().expense_by_currency == (("MYR", "9.00"),)


def save_accountless_expense(ledger, port, api, draft_fields):
    original = "coffee-without-account"
    port.add_source(original, "Coffee MYR 9 on November 17")
    source = ledger.capture(original, "capture-coffee-without-account")
    port.remember_movement_source(source, original)
    port.register_movement_instruction(ledger.binding, source.source_id, original)
    draft_command = {
        "format": "finance-daily-movement-command-v1",
        "command_kind": "movement_create",
        "fields": draft_fields,
        "reason_span": None,
    }
    port.commands[original] = draft_command
    proposal = api.propose_movement_create(source.source_id, original, 0, "propose-no-account")
    port.remember_movement_proposal(proposal)
    draft = api.pending_for_source(source.source_id)
    assert draft["values"]["missing_fields"] == ["account_id"]
    return source, draft_command, draft


@pytest.mark.parametrize(
    ("opening_known", "account_currency"),
    [(True, "MYR"), (False, "MYR"), (True, "SGD")],
    ids=["known-opening", "needs-coverage", "different-currency"],
)
def test_delivered_account_selection_reopens_draft_and_only_fills_chosen_account(
    live, opening_known, account_currency
):
    from dataclasses import asdict

    from finance_core.instance_admission import acquire_production_owner
    from finance_core.lifecycle_v2 import LedgerService

    opening_date = {**unknown_time(), "precision": "date", "local_date": "2026-11-16"}
    ledger, port, _, api, reader, account = prepare(
        live, currency=account_currency, opening_as_of=opening_date if opening_known else None
    )
    _, _, cfg = live
    draft_fields = fields(None)
    draft_fields["funds_time"] = {
        **unknown_time(),
        "precision": "date",
        "local_date": "2026-11-17",
    }
    if opening_known:
        del draft_fields["coverage"]
    else:
        draft_fields["coverage"] = {"answer": "unknown", "span": None}
    source, draft_command, draft = save_accountless_expense(ledger, port, api, draft_fields)
    account_ref = asdict(reader.get_account(account).account_ref)
    question = port.register_movement_question(
        ledger.binding,
        source.source_id,
        "coffee-account-question",
        "account_selection",
        {"draft_fields": draft_fields, "offered_accounts": [account_ref]},
    )

    ledger.owner.close()
    with acquire_production_owner(cfg) as owner:
        reopened = LedgerService(owner, port, ledger.binding, clock=ledger.clock)
        api = DailyMovementServiceV1(reopened)
        reader = DailyLedgerReaderV1(reopened)
        assert api.pending_for_source(source.source_id) == draft
        reply = "genuine-coffee-account-answer"
        port.add_source(reply, "Use Bank")
        port.register_movement_reply(question, reply, selected_account=account_ref)
        chosen_fields = {**draft_fields, "account_id": account}
        port.commands["changed-answer"] = {
            **draft_command,
            "fields": {**chosen_fields, "account_amount": "90"},
        }
        with pytest.raises(LifecycleError, match="can only set the account"):
            api.propose_movement_create(
                source.source_id, "changed-answer", 1, "bad-account-answer", reply_evidence_id=reply
            )
        assert api.pending_for_source(source.source_id) == draft
        assert reader.collect().balances[0].balance_amount == "1000.00"
        port.commands[reply] = {**draft_command, "fields": chosen_fields}
        if account_currency != draft_fields["original_currency"]:
            with pytest.raises(LifecycleError, match="same-currency account"):
                api.propose_movement_create(
                    source.source_id, reply, 1, "cross-currency-account", reply_evidence_id=reply
                )
            assert api.pending_for_source(source.source_id) == draft
            assert reader.collect().expense_by_currency == ()
            return
        proposal = api.propose_movement_create(
            source.source_id, reply, 1, "propose-selected-account", reply_evidence_id=reply
        )
        port.remember_movement_proposal(proposal)
        if not opening_known:
            pending = api.pending_for_source(source.source_id)
            assert pending["values"]["missing_fields"] == ["opening_coverage"]
            assert pending["values"]["command"]["fields"] == chosen_fields
            with pytest.raises(LifecycleError, match="incomplete"):
                api.prepare_review(proposal.proposal_id, proposal.version)
            assert reader.collect().balances[0].balance_amount == "1000.00"
            return
        card = api.prepare_review(proposal.proposal_id, proposal.version)
        assert card.material["after_values"]["account_id"] == account
        assert card.material["after_values"]["account_amount"] == "9.00"
        api.record_delivery(card.review_id, "selected-account-card-delivery")
        assert reader.collect().balances[0].balance_amount == "1000.00"
        receipt = api.confirm(card.review_id, "confirm-separate", "confirm-coffee-once")
        assert api.confirm(card.review_id, "confirm-separate", "confirm-coffee-once") == receipt
        result = reader.collect()
        assert result.balances[0].balance_amount == "991.00"
        assert result.expense_by_currency == (("MYR", "9.00"),)
        assert api.pending_for_source(source.source_id) is None
    with acquire_production_owner(cfg) as owner:
        reopened = LedgerService(owner, port, ledger.binding, clock=ledger.clock)
        assert DailyLedgerReaderV1(reopened).collect() == result


@pytest.mark.parametrize("changed_question", [True, False], ids=["question", "proposal"])
def test_account_selection_preserves_draft_field_types(live, changed_question):
    from dataclasses import asdict

    ledger, port, _, api, reader, account = prepare(live, opening=None)
    draft_fields = fields(None)
    del draft_fields["coverage"]
    funds_time = {
        **unknown_time(),
        "precision": "instant",
        "local_date": "2026-11-17",
        "timestamp": 1794877210.0,
    }
    draft_fields["funds_time"] = funds_time
    source, command, draft = save_accountless_expense(ledger, port, api, draft_fields)
    corrected_fields = {
        **draft_fields,
        "funds_time": {**funds_time, "timestamp": 1794877210},
    }
    account_ref = asdict(reader.get_account(account).account_ref)
    question = port.register_movement_question(
        ledger.binding,
        source.source_id,
        "coffee-account-question",
        "account_selection",
        {
            "draft_fields": corrected_fields if changed_question else draft_fields,
            "offered_accounts": [account_ref],
        },
    )
    reply = "coffee-account-choice"
    port.add_source(reply, "Use Bank")
    port.register_movement_reply(question, reply, selected_account=account_ref)
    port.commands[reply] = {**command, "fields": {**corrected_fields, "account_id": account}}
    expected = "previous incomplete draft" if changed_question else "can only set the account"
    with pytest.raises(LifecycleError, match=expected):
        api.propose_movement_create(
            source.source_id, reply, 1, "propose-selected-account", reply_evidence_id=reply
        )
    assert api.pending_for_source(source.source_id) == draft
    assert reader.collect().expense_by_currency == ()


def test_account_reassignment_replaces_one_impact_and_duplicate_void_keeps_other(live, tmp_path):
    import sqlite3

    from finance_core.lifecycle_v2.reader import audit_evidence_connection

    ledger, port, daily, api, reader, account = prepare(live)
    port.add_source("source2", "Create another bank with opening MYR 500")
    _, _, _, second = create(
        ledger,
        port,
        daily,
        command(
            fields={
                "name": "Bank",
                "account_type": "bank",
                "currency": "MYR",
                "opening": {"amount": "500", "as_of": unknown_time()},
            }
        ),
        "2",
    )
    account_b = second.account_ref.account_id
    _, _, first = movement(
        ledger, port, api, account, "coffee-one", fields(account, "4", original="4")
    )
    movement(ledger, port, api, account, "coffee-two", fields(account, "4", original="4"))
    target = reader.detail(first.receipt.record_id)
    _, _, corrected = movement(
        ledger,
        port,
        api,
        account_b,
        "assign",
        fields(account_b, "4", original="4"),
        target,
        "movement_correct",
        "wrong account",
    )
    balances = {b.account_id: b.balance_amount for b in reader.collect().balances}
    assert balances == {account: "996.00", account_b: "496.00"}
    assert reader.collect().expense_by_currency == (("MYR", "8.00"),)
    movement(
        ledger,
        port,
        api,
        account_b,
        "void-one",
        {},
        reader.detail(corrected.receipt.record_id),
        "movement_void",
        "duplicate coffee",
    )
    result = reader.collect()
    assert {b.account_id: b.balance_amount for b in result.balances} == {
        account: "996.00",
        account_b: "500.00",
    }
    assert result.expense_by_currency == (("MYR", "4.00"),)
    cut = ledger.seal_snapshot(tmp_path / "mixed.sqlite")
    conn = sqlite3.connect((tmp_path / "mixed.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        copied = audit_evidence_connection(conn, port, ledger.binding)
        assert copied["fact_version_count"] == result.fact_version_count == 6
        from finance_core.instance_admission.authority import _logical_database_digest

        assert _logical_database_digest(conn) == result.read_identity.logical_database_sha256
        expected_current = sorted(
            [
                (a.bundle_ref.record_id, a.bundle_ref.version, a.bundle_ref.fact_sha256)
                for a in result.accounts
            ]
            + [
                (m.fact_ref.record_id, m.fact_ref.version, m.fact_ref.fact_sha256)
                for m in result.movements
            ]
        )
        assert list(cut.facts) == expected_current
    finally:
        conn.close()


def test_legacy_unassigned_is_counted_once_then_controlled_reassignment_keeps_receipt(live):
    from tests.test_lifecycle_v2 import response

    ledger, port, _, api, reader, account = prepare(live)
    raw = "2026-10-24 Cafe SGD 12.00"
    port.add_source("legacy", raw)
    port.native_raw["legacy-observation"] = response(raw)
    source = ledger.capture("legacy", "capture-legacy")
    port.remember_movement_source(source, "legacy")
    proposal = ledger.propose(source.source_id, "legacy-observation", 0, "propose-legacy")
    port.remember_movement_proposal(proposal)
    card = ledger.prepare_review(proposal.proposal_id, proposal.version)
    ledger.record_delivery(card.review_id, "delivery-legacy")
    original = ledger.confirm(card.review_id, "action-legacy", "confirm-legacy")
    result = reader.collect()
    assert result.unassigned_personal_expense_count == 1
    assert result.expense_by_currency == (("SGD", "12.00"),)
    assert result.balances[0].balance_amount == "1000.00"
    movement(
        ledger,
        port,
        api,
        account,
        "assign-legacy",
        fields(account, "12", original="12", currency="SGD"),
        reader.detail(original.record_id),
        "movement_correct",
        "supply actual account",
    )
    result = reader.collect()
    assert result.unassigned_personal_expense_count == 0
    assert result.expense_by_currency == (("SGD", "12.00"),)
    assert result.balances[0].balance_amount == "988.00"
    assert len(reader.history(original.record_id)) == 2
    assert (
        ledger.lookup_commit_receipt(
            "confirm-legacy", review_id=card.review_id, action_evidence_id="action-legacy"
        )
        == original
    )


@pytest.mark.parametrize("raw", ["0", "-1", "NaN", "Infinity", "1.001", 1.0, True, None])
def test_invalid_actual_money_is_refused_as_pure_value(raw):
    from finance_core.lifecycle_v2.daily_v1.movement_contracts import _positive_money

    with pytest.raises((LifecycleError, ValueError, TypeError)):
        _positive_money(raw, "MYR")


def test_stale_cards_separate_confirmation_failure_atomicity_and_restart_replay(live, monkeypatch):
    import importlib

    from finance_core.instance_admission.sessions import lifecycle_read_operation
    from tests.test_daily_accounts_lifecycle_v1 import change

    ledger, port, daily, api, reader, account = prepare(live)
    port.add_source("race-source", "excluded; coffee MYR 9")
    source = ledger.capture("race-source", "capture-race")
    port.remember_movement_source(source, "race-source")

    def propose(identity, version, reply=None):
        port.commands[identity] = {
            "format": "finance-daily-movement-command-v1",
            "command_kind": "movement_create",
            "fields": fields(account),
            "reason_span": None,
        }
        port.register_movement_instruction(ledger.binding, source.source_id, reply or "race-source")
        proposal = api.propose_movement_create(
            source.source_id, identity, version, "propose-" + identity, reply_evidence_id=reply
        )
        port.remember_movement_proposal(proposal)
        card = api.prepare_review(proposal.proposal_id, proposal.version)
        api.record_delivery(card.review_id, "delivery-" + identity)
        return card

    older = propose("older", 0)
    port.add_source("new-reply", "excluded; confirm account and amount")
    newer = propose("newer", 1, "new-reply")
    with pytest.raises(LifecycleError, match="stale"):
        api.confirm(older.review_id, "old-action", "old-confirm")
    port.bad_actor = True
    with pytest.raises(LifecycleError):
        api.confirm(newer.review_id, "wrong-actor", "wrong-actor-op")
    port.bad_actor = False
    module = importlib.import_module("finance_core.lifecycle_v2._finalization_plan_v1")
    original_append = module._append_finalization

    def interrupted_append(conn, plan):
        original_append(conn, plan)
        raise RuntimeError("synthetic failure before protected commit")

    monkeypatch.setattr(module, "_append_finalization", interrupted_append)
    with pytest.raises(RuntimeError, match="before protected commit"):
        api.confirm(newer.review_id, "new-action", "new-confirm")
    with lifecycle_read_operation(ledger.owner) as conn:
        assert conn.execute("SELECT count(*) FROM l2_facts").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM l2_decisions").fetchone()[0] == 1
        assert (
            conn.execute("SELECT count(*) FROM l2_events WHERE kind='confirm'").fetchone()[0] == 1
        )
    monkeypatch.setattr(module, "_append_finalization", original_append)
    receipt = api.confirm(newer.review_id, "new-action", "new-confirm")
    restarted = DailyMovementServiceV1(ledger)
    assert restarted.confirm(newer.review_id, "new-action", "new-confirm") == receipt
    assert reader.collect().balances[0].balance_amount == "991.00"
    # A normal account metadata version change invalidates a prepared new card.
    port.add_source("another-source", "excluded; another coffee")
    other = ledger.capture("another-source", "capture-other")
    port.remember_movement_source(other, "another-source")
    port.register_movement_instruction(ledger.binding, other.source_id, "another-source")
    port.commands["other"] = {
        "format": "finance-daily-movement-command-v1",
        "command_kind": "movement_create",
        "fields": fields(account),
        "reason_span": None,
    }
    proposal = api.propose_movement_create(other.source_id, "other", 0, "propose-other")
    card = api.prepare_review(proposal.proposal_id, proposal.version)
    api.record_delivery(card.review_id, "delivery-other")
    change(
        port,
        daily,
        reader.get_account(account),
        "account_rename",
        {"name": "Renamed"},
        "rename-after-card",
    )
    with pytest.raises(LifecycleError, match="stale_movement_account_or_opening"):
        api.confirm(card.review_id, "other-action", "other-confirm")
    assert reader.collect().balances[0].balance_amount == "991.00"
    original_source = port.source

    def missing_source(conn, identity):
        if identity == "race-source":
            raise LookupError("synthetic actual source unavailable")
        return original_source(conn, identity)

    monkeypatch.setattr(port, "source", missing_source)
    with pytest.raises(LookupError):
        reader.collect()


@pytest.mark.parametrize(
    ("funds", "opening", "later"),
    [
        (("instant", 1794877210, "2026-11-17"), ("instant", 1794877200, "2026-11-17"), True),
        (("instant", 1794877200, "2026-11-17"), ("instant", 1794877200, "2026-11-17"), False),
        (("date", None, "2026-11-17"), ("instant", 1794877200, "2026-11-17"), False),
        (("instant", 1794877200, "2026-11-17"), ("date", None, "2026-11-17"), False),
        (("date", None, "2026-11-18"), ("date", None, "2026-11-17"), True),
        (("date", None, "2026-11-16"), ("date", None, "2026-11-17"), False),
        (("unknown", None, None), ("instant", 1794877200, "2026-11-17"), False),
    ],
)
def test_only_strictly_later_known_funds_can_infer_excluded(funds, opening, later):
    from finance_core.lifecycle_v2.daily_v1.movement_contracts import _later

    def value(parts):
        precision, timestamp, day = parts
        return {**unknown_time(), "precision": precision, "timestamp": timestamp, "local_date": day}

    assert _later(value(funds), value(opening)) is later


def test_missing_void_reason_keeps_durable_pending_without_changing_fact(live):
    ledger, port, _, api, reader, account = prepare(live)
    _, _, first = movement(ledger, port, api, account, "coffee")
    target = reader.detail(first.receipt.record_id)
    port.add_source("unclear-void", "Please undo this")
    port.register_movement_instruction(
        ledger.binding, target.source_id, "unclear-void", "movement_void", target.fact_ref
    )
    port.commands["unclear-void"] = {
        "format": "finance-daily-movement-command-v1",
        "command_kind": "movement_void",
        "fields": {},
        "reason_span": None,
    }
    proposal = api.propose_movement_change(
        target.record_id,
        "unclear-void",
        target.fact_ref,
        "propose-unclear",
        reply_evidence_id="unclear-void",
        command_kind="movement_void",
    )
    assert api.pending()[0]["values"]["missing_fields"] == ["reason_span"]
    with pytest.raises(LifecycleError, match="incomplete_proposal"):
        api.prepare_review(proposal.proposal_id, proposal.version)
    assert reader.detail(target.record_id).values["status"] == "ACTIVE"
    assert reader.collect().balances[0].balance_amount == "991.00"


def test_signed_effect_and_card_delta_ignore_hostile_decimal_context():
    from decimal import Inexact, Rounded, localcontext

    from finance_core.lifecycle_v2.daily_v1._impacts_v1 import effect, review_impact_changes

    value = {
        "format": "finance-daily-movement-v1",
        "status": "ACTIVE",
        "kind": "expense",
        "account_id": "synthetic-account",
        "account_currency": "MYR",
        "account_amount": "123456789012345678901234567890.12",
        "funds_time": unknown_time(),
        "opening_ref": None,
        "coverage": {"treatment": "no_baseline"},
    }
    baseline = review_impact_changes(None, value)
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.Emax = 2
        ctx.Emin = -2
        ctx.traps[Inexact] = True
        ctx.traps[Rounded] = True
        assert effect(value)["amount"] == "-123456789012345678901234567890.12"
        assert review_impact_changes(None, value) == baseline


def test_legacy_unknown_expense_whole_void_preserves_original_receipt_and_cash_balance(live):
    from tests.test_lifecycle_v2 import response

    ledger, port, _, api, reader, account = prepare(live, account_type="cash")
    raw = "2026-10-24 Cafe SGD 12.00"
    port.add_source("legacy-void-source", raw)
    port.native_raw["legacy-void-observation"] = response(raw)
    source = ledger.capture("legacy-void-source", "capture-legacy-void")
    port.remember_movement_source(source, "legacy-void-source")
    proposal = ledger.propose(source.source_id, "legacy-void-observation", 0, "propose-legacy-void")
    port.remember_movement_proposal(proposal)
    original_card = ledger.prepare_review(proposal.proposal_id, proposal.version)
    ledger.record_delivery(original_card.review_id, "delivery-legacy-void")
    original = ledger.confirm(original_card.review_id, "action-legacy-void", "confirm-legacy-void")
    snapshot = reader.collect_with_contributions_v1()
    (detail,) = snapshot.contributions
    assert (detail.amount, detail.currency, detail.business_date) == ("12.00", "SGD", "2026-10-24")
    assert detail.fact_ref.record_id == original.record_id
    assert "unassigned_personal_expense" in snapshot.collection.completeness_gaps
    target = reader.detail(original.record_id)
    movement(
        ledger, port, api, account, "whole-void", {}, target, "movement_void", "duplicate coffee"
    )
    after = reader.collect_with_contributions_v1()
    assert after.contributions == ()
    result = after.collection
    assert result.expense_by_currency == ()
    assert result.unassigned_personal_expense_count == 0
    assert result.balances[0].balance_amount == "1000.00"
    assert reader.detail(original.record_id).values["status"] == "VOID"
    assert (
        ledger.confirm(original_card.review_id, "action-legacy-void", "confirm-legacy-void")
        == original
    )
    assert reader.detail(original.record_id).values["status"] == "VOID"
    assert len(reader.history(original.record_id)) == 2


def linked_draft(ledger, port, api, account, identity):
    port.add_source(identity, "Record coffee MYR 9; opening coverage is unknown")
    source = ledger.capture(identity, "capture-" + identity)
    port.remember_movement_source(source, identity)
    port.register_movement_instruction(ledger.binding, source.source_id, identity)
    pending = fields(account, coverage="unknown")
    pending["coverage"]["span"] = None
    port.commands[identity] = {
        "format": "finance-daily-movement-command-v1",
        "command_kind": "movement_create",
        "fields": pending,
        "reason_span": None,
    }
    proposal = api.propose_movement_create(source.source_id, identity, 0, "propose-" + identity)
    port.remember_movement_proposal(proposal)
    return source, proposal


def test_instruction_link_rejects_other_source_old_draft_question_and_delivery(live):
    ledger, port, _, api, reader, account = prepare(live)
    source_a, _ = linked_draft(ledger, port, api, account, "draft-a")
    source_b, _ = linked_draft(ledger, port, api, account, "draft-b")
    subject = opening_subject(fields(account), reader.get_account(account))
    question_a = port.register_opening_question(ledger.binding, source_a.source_id, "qa", subject)
    port.add_source("answer-a", "included")
    port.register_opening_reply(question_a, "answer-a")
    with pytest.raises(LifecycleError, match="movement_instruction_target_mismatch"):
        api.propose_movement_create(
            source_b.source_id, "wrong-target", 1, "wrong-target", reply_evidence_id="answer-a"
        )
    # A new explicit target selection advances the A draft; its old question
    # keeps its original version/hash and cannot authorize the new draft.
    port.add_source("supplement-a", "Record this coffee, coverage still unknown")
    port.register_movement_instruction(ledger.binding, source_a.source_id, "supplement-a")
    pending = fields(account, coverage="unknown")
    pending["coverage"]["span"] = None
    port.commands["supplement-a"] = {
        "format": "finance-daily-movement-command-v1",
        "command_kind": "movement_create",
        "fields": pending,
        "reason_span": None,
    }
    second = api.propose_movement_create(
        source_a.source_id, "supplement-a", 1, "supplement-a", reply_evidence_id="supplement-a"
    )
    port.remember_movement_proposal(second)
    with pytest.raises(LifecycleError, match="movement_instruction_target_mismatch"):
        api.expected_movement_create(source_a.source_id, 2, reply_evidence_id="answer-a")
    q1 = port.register_opening_question(ledger.binding, source_b.source_id, "qb", subject)
    q2 = port.register_opening_question(
        ledger.binding, source_b.source_id, "qb", subject, version=2
    )
    port.add_source("late-q1", "included")
    port.register_opening_reply(q1, "late-q1")
    with pytest.raises(LifecycleError, match="question/delivery/reply association"):
        api.expected_movement_create(source_b.source_id, 1, reply_evidence_id="late-q1")
    port.add_source("wrong-delivery", "included")
    port.register_opening_reply(
        q2, "wrong-delivery", actual_delivery_id=q1["delivery_ref"]["evidence_id"]
    )
    with pytest.raises(LifecycleError, match="question/delivery/reply association"):
        api.expected_movement_create(source_b.source_id, 1, reply_evidence_id="wrong-delivery")
    # Refusals preserve both sources/drafts and create no financial effect.
    result = reader.collect()
    assert result.fact_version_count == 1 and result.movements == ()
    assert result.balances[0].balance_amount == "1000.00"
    assert len(api.pending()) == 2
    fresh_a = port.register_opening_question(
        ledger.binding, source_a.source_id, "qa", subject, version=2
    )
    port.add_source("independent-a", "included")
    port.register_opening_reply(fresh_a, "independent-a")
    port.add_source("independent-b", "included")
    port.register_opening_reply(q2, "independent-b")
    assert (
        api.expected_movement_create(
            source_a.source_id, 2, reply_evidence_id="independent-a"
        ).source_id
        == source_a.source_id
    )
    assert (
        api.expected_movement_create(
            source_b.source_id, 1, reply_evidence_id="independent-b"
        ).source_id
        == source_b.source_id
    )


def test_question_subject_cannot_promote_9_to_90_and_history_rechecks_link(
    live, tmp_path, monkeypatch
):
    import sqlite3

    from finance_core.lifecycle_v2.reader import audit_evidence_connection

    ledger, port, _, api, reader, account = prepare(live)
    source, _ = linked_draft(ledger, port, api, account, "coffee-draft")
    subject = opening_subject(fields(account), reader.get_account(account))
    q1 = port.register_opening_question(ledger.binding, source.source_id, "coverage", subject)
    port.add_source("answer-9", "included")
    port.register_opening_reply(q1, "answer-9")
    wrong = fields(account, amount="90", original="90", coverage="included")
    port.commands["wrong-90"] = {
        "format": "finance-daily-movement-command-v1",
        "command_kind": "movement_create",
        "fields": wrong,
        "reason_span": None,
    }
    proposal = api.propose_movement_create(
        source.source_id, "wrong-90", 1, "propose-wrong-90", reply_evidence_id="answer-9"
    )
    port.remember_movement_proposal(proposal)
    with pytest.raises(LifecycleError, match="reply_evidence_already_used"):
        api.expected_movement_create(source.source_id, 2, reply_evidence_id="answer-9")
    assert api.pending()[0]["values"]["missing_fields"] == ["opening_coverage"]
    with pytest.raises(LifecycleError, match="incomplete_proposal"):
        api.prepare_review(proposal.proposal_id, proposal.version)
    assert reader.collect().balances[0].balance_amount == "1000.00"
    q2 = port.register_opening_question(
        ledger.binding, source.source_id, "coverage", subject, version=2
    )
    port.add_source("answer-corrected", "included")
    port.register_opening_reply(q2, "answer-corrected")
    port.commands["corrected-9"] = {
        "format": "finance-daily-movement-command-v1",
        "command_kind": "movement_create",
        "fields": fields(account, coverage="included"),
        "reason_span": None,
    }
    good = api.propose_movement_create(
        source.source_id,
        "corrected-9",
        2,
        "propose-corrected",
        reply_evidence_id="answer-corrected",
    )
    port.remember_movement_proposal(good)
    card = api.prepare_review(good.proposal_id, good.version)
    api.record_delivery(card.review_id, "delivery-corrected")
    with pytest.raises(LifecycleError):
        api.confirm(card.review_id, "answer-corrected", "reply-is-not-confirm")
    result = api.confirm(card.review_id, "confirm-corrected-action", "confirm-corrected")
    assert reader.collect().expense_by_currency == (("MYR", "9.00"),)
    assert reader.collect().balances[0].balance_amount == "1000.00"
    # A later genuine new question does not retroactively retire a relation
    # that was valid at the original reply's actual event point.
    current = reader.detail(result.receipt.record_id)
    port.register_opening_question(
        ledger.binding,
        source.source_id,
        "coverage",
        subject,
        version=3,
        kind="movement_correct",
        fact_ref=current.fact_ref,
    )
    assert reader.collect().fact_version_count == 2
    cut = ledger.seal_snapshot(tmp_path / "linked.sqlite")
    conn = sqlite3.connect((tmp_path / "linked.sqlite").as_uri() + "?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        assert audit_evidence_connection(conn, port, ledger.binding)["fact_version_count"] == 2
        assert len(cut.facts) == 2
    finally:
        conn.close()
    original = port.movement_instruction_link

    def unavailable(conn, identity, target):
        if identity == "answer-corrected":
            raise LookupError("actual independent link evidence unavailable")
        return original(conn, identity, target)

    monkeypatch.setattr(port, "movement_instruction_link", unavailable)
    with pytest.raises(LookupError):
        reader.collect()
    with pytest.raises(LookupError):
        ledger.seal_snapshot(tmp_path / "refused.sqlite")


def test_new_instruction_link_callback_keeps_actual_readonly_connection(live, monkeypatch):
    from finance_core.instance_admission import InstanceAdmissionError
    from finance_core.instance_admission.sessions import _witness_for

    ledger, port, _, api, reader, account = prepare(live)
    port.add_source("guard-source", "excluded; Record expense MYR 9")
    source = ledger.capture("guard-source", "capture-guard")
    port.remember_movement_source(source, "guard-source")
    port.register_movement_instruction(ledger.binding, source.source_id, "guard-source")
    original = port.movement_instruction_link

    def swallowed(conn, identity, target):
        assert _witness_for(conn).evidence_callback_token is not None
        try:
            conn.commit()
        except InstanceAdmissionError:
            pass
        return original(conn, identity, target)

    monkeypatch.setattr(port, "movement_instruction_link", swallowed)
    with pytest.raises(InstanceAdmissionError, match="read-only scope"):
        api.expected_movement_create(source.source_id)
    monkeypatch.setattr(port, "movement_instruction_link", original)
    assert reader.collect().fact_version_count == 1
    observed = []

    def inspect(conn, identity, target):
        observed.append(id(conn))
        assert conn.execute("SELECT count(*) FROM l2_facts").fetchone()[0] == 1
        return original(conn, identity, target)

    provider = port.movement_proposal

    def inspect_provider(conn, identity, expected):
        assert id(conn) == observed[-1]
        return provider(conn, identity, expected)

    monkeypatch.setattr(port, "movement_instruction_link", inspect)
    monkeypatch.setattr(port, "movement_proposal", inspect_provider)
    port.commands["guard-proposal"] = {
        "format": "finance-daily-movement-command-v1",
        "command_kind": "movement_create",
        "fields": fields(account),
        "reason_span": None,
    }
    api.propose_movement_create(source.source_id, "guard-proposal", 0, "guard-propose")


def test_unpersisted_link_wrong_purpose_actor_binding_and_subject_values_refuse(live):
    import copy
    import json
    from dataclasses import asdict

    from finance_core.lifecycle_v2.contracts import RuntimeBinding, canonical
    from finance_core.lifecycle_v2.daily_v1 import MovementInstructionTargetV1
    from finance_core.lifecycle_v2.daily_v1.movement_contracts import decode_instruction_link

    ledger, port, _, api, reader, account = prepare(live)
    source, _ = linked_draft(ledger, port, api, account, "pure-link-draft")
    subject = opening_subject(fields(account), reader.get_account(account))
    question = port.register_opening_question(ledger.binding, source.source_id, "pure-q", subject)
    port.add_source("pure-answer", "included")
    port.register_opening_reply(question, "pure-answer")
    retained = port.movement_links["pure-answer"]
    material = json.loads(retained["link_json"])
    raw_target = copy.deepcopy(material["target"])
    raw_target["binding"] = RuntimeBinding(**raw_target["binding"])
    target = MovementInstructionTargetV1(**raw_target)
    for edit in ("purpose", "actor", "binding", "subject_money", "draft_hash", "fact_target"):
        pure = copy.deepcopy(material)
        if edit == "purpose":
            pure["question"]["purpose"] = "future_unsupported_question"
        elif edit == "actor":
            pure["target"]["instruction_ref"]["principal_id"] = "other"
        elif edit == "binding":
            pure["target"]["binding"]["generation"] = "other"
        elif edit == "subject_money":
            pure["question"]["subject"]["account_amount"] = "9.001"
        elif edit == "draft_hash":
            pure["target"]["current_proposal"]["proposal_sha256"] = "0" * 64
        else:
            pure["target"]["target_fact_ref"] = {
                "record_id": target.record_id,
                "version": 1,
                "fact_sha256": "0" * 64,
                "commit_seq": 1,
            }
        with pytest.raises((LifecycleError, ValueError)):
            decode_instruction_link(
                {"evidence_id": retained["evidence_id"], "link_json": canonical(pure)}, target
            )
    assert asdict(target)["instruction_ref"]["principal_id"] == "user"


def test_question_revalidates_all_financial_subject_fields_and_current_opening(live):
    import copy

    from finance_core.lifecycle_v2.daily_v1.movement_contracts import compile_proposal
    from tests.test_daily_accounts_lifecycle_v1 import change

    ledger, port, daily, api, reader, account = prepare(live)
    port.add_source("source2", "Create SGD bank B with opening 500")
    _, _, _, second = create(
        ledger,
        port,
        daily,
        command(
            fields={
                "name": "Bank B",
                "account_type": "bank",
                "currency": "SGD",
                "opening": {"amount": "500", "as_of": unknown_time()},
            }
        ),
        "2",
    )
    source, _ = linked_draft(ledger, port, api, account, "subject-draft")
    subject = opening_subject(fields(account), reader.get_account(account))
    question = port.register_opening_question(
        ledger.binding, source.source_id, "subject-q", subject
    )
    port.add_source("subject-answer", "included")
    port.register_opening_reply(question, "subject-answer")
    expected = api.expected_movement_create(source.source_id, 1, reply_evidence_id="subject-answer")
    original = {
        "format": "finance-daily-movement-command-v1",
        "command_kind": "movement_create",
        "fields": fields(account, coverage="included"),
        "reason_span": None,
    }
    variants = [
        {"account_amount": "90"},
        {"original_amount": "90"},
        {"original_amount": "1000", "original_currency": "JPY"},
        {"account_id": second.account_ref.account_id},
        {"business_date": "2026-11-16"},
        {"kind": "income"},
        {"funds_time": {**unknown_time(), "precision": "date", "local_date": "2026-11-18"}},
    ]
    for changes in variants:
        pure = copy.deepcopy(original)
        pure["fields"].update(changes)
        assert compile_proposal(pure, expected)["missing_fields"] == ["opening_coverage"]
    # No effective movement exists: changing this opening through the normal
    # existing account API is permitted, but its old question cannot cover it.
    change(
        port,
        daily,
        reader.get_account(account),
        "opening_correct",
        {"amount": "999", "as_of": unknown_time()},
        "opening-before-reply",
        "wrong opening",
    )
    port.commands["new-opening-answer"] = original
    pending = api.propose_movement_create(
        source.source_id,
        "new-opening-answer",
        1,
        "propose-new-opening-answer",
        reply_evidence_id="subject-answer",
    )
    assert api.pending()[0]["values"]["missing_fields"] == ["opening_coverage"]
    with pytest.raises(LifecycleError, match="incomplete_proposal"):
        api.prepare_review(pending.proposal_id, pending.version)
    assert {balance.account_id: balance.balance_amount for balance in reader.collect().balances}[
        account
    ] == "999.00"


def test_bare_coverage_cannot_be_laundered_as_initial_instruction(live):
    ledger, port, _, api, reader, account = prepare(live)
    port.add_source("bare-initial", "included")
    source = ledger.capture("bare-initial", "capture-bare")
    port.remember_movement_source(source, "bare-initial")
    port.register_movement_instruction(ledger.binding, source.source_id, "bare-initial")
    port.commands["bare"] = {
        "format": "finance-daily-movement-command-v1",
        "command_kind": "movement_create",
        "fields": fields(account, coverage="included"),
        "reason_span": None,
    }
    with pytest.raises(LifecycleError, match="Bare opening coverage reply"):
        api.propose_movement_create(source.source_id, "bare", 0, "propose-bare")
    assert reader.collect().fact_version_count == 1


@pytest.mark.parametrize("mode", [[], {}, True, None, 7, "future_unknown_mode"])
def test_instruction_link_unknown_mode_pure_values_fail_closed(mode):
    from finance_core.lifecycle_v2.contracts import canonical
    from finance_core.lifecycle_v2.daily_v1.movement_contracts import decode_instruction_link

    raw = {
        "format": "finance-daily-movement-instruction-link-v1",
        "mode": mode,
        "target": None,
        "association_ref": None,
        "question": None,
    }
    with pytest.raises(LifecycleError):
        decode_instruction_link({"evidence_id": "pure-unpersisted", "link_json": canonical(raw)})


def pure_coverage_values(raw):
    """Unpersisted answer-codec values; this is not transport authentication."""
    import hashlib
    from dataclasses import asdict
    from types import SimpleNamespace

    from finance_core.lifecycle_v2 import VerifiedSource
    from finance_core.lifecycle_v2.contracts import canonical
    from finance_core.lifecycle_v2.daily_v1.contracts import compile_bundle, instruction_ref
    from finance_core.lifecycle_v2.daily_v1.movement_contracts import (
        coverage_subject,
        question_target,
    )
    from tests.test_daily_accounts_contracts_v1 import expected as account_expected

    owner = account_expected()
    account = compile_bundle(
        command(
            fields={
                "name": "Bank",
                "account_type": "bank",
                "currency": "MYR",
                "opening": {"amount": "1000", "as_of": unknown_time()},
            }
        ),
        owner,
    )
    instruction = asdict(
        VerifiedSource(
            "pure-answer",
            "user",
            "chat",
            "pure-message",
            raw,
            hashlib.sha256(raw.encode()).hexdigest(),
            1794877200,
        )
    )
    values = {
        "kind": "expense",
        "original_amount": "9.00",
        "original_currency": "MYR",
        "account_amount": "9.00",
        "account_currency": "MYR",
        "business_date": "2026-11-17",
        "funds_time": unknown_time(),
    }
    target = {
        "binding": asdict(owner.binding),
        "source_id": "pure-source",
        "source_sha256": "a" * 64,
        "source_instruction_ref": instruction_ref(owner.instruction),
        "instruction_ref": instruction_ref(instruction),
        "record_id": "pure-record",
        "command_kind": "movement_create",
        "current_proposal": {
            "proposal_id": "pure-proposal",
            "version": 1,
            "proposal_sha256": "b" * 64,
        },
        "target_fact_ref": None,
    }
    link = {
        "format": "finance-daily-movement-instruction-link-v1",
        "mode": "question_reply",
        "target": target,
        "association_ref": {"evidence_id": "pure-reply", "sha256": "c" * 64},
        "question": {
            "question_id": "pure-question",
            "question_version": 1,
            "delivery_ref": {"evidence_id": "pure-delivery", "sha256": "d" * 64},
            "purpose": "opening_coverage",
            "target": question_target(target),
            "subject": coverage_subject(values, account),
        },
    }
    return (
        SimpleNamespace(
            instruction=instruction,
            instruction_link={"evidence_id": "pure-link", "link_json": canonical(link)},
        ),
        values,
        account,
    )


@pytest.mark.parametrize(
    ("raw", "choice", "quote", "valid"),
    [
        ("not included", "included", "included", False),
        ("NOT INCLUDED", "included", "INCLUDED", False),
        ("This debit was not included in opening.", "included", "included", False),
        ("It was not already included.", "included", "already included", False),
        ("It was not\t included.", "included", "included", False),
        ("It was not excluded.", "excluded", "excluded", False),
        ("unincluded", "included", "included", False),
        ("includedness", "included", "included", False),
        ("not included", "excluded", "not included", True),
        ("This debit was not included in opening.", "excluded", "not included", True),
        ("included", "included", "included", True),
        ("already included", "included", "already included", True),
        ("尚未包含", "excluded", "尚未包含", True),
    ],
)
def test_coverage_choice_preserves_whole_answer_and_negative_prefix(raw, choice, quote, valid):
    from finance_core.lifecycle_v2.daily_v1.movement_contracts import (
        _coverage,
        decode_instruction_link,
    )

    expected, values, account = pure_coverage_values(raw)
    start = raw.index(quote)
    cmd = {"fields": {"coverage": {"answer": choice, "span": [start, start + len(quote)]}}}
    question = decode_instruction_link(expected.instruction_link)["question"]
    if valid:
        assert _coverage(cmd, expected, values, account, question)["treatment"] == choice
    else:
        with pytest.raises(LifecycleError):
            _coverage(cmd, expected, values, account, question)


def test_genuine_negative_question_reply_refuses_positive_slice_and_posts_once(live):
    ledger, port, _, api, reader, account = prepare(live)
    source, _ = linked_draft(ledger, port, api, account, "negative-draft")
    q = port.register_opening_question(
        ledger.binding,
        source.source_id,
        "negative-q",
        opening_subject(fields(account), reader.get_account(account)),
    )
    port.add_source("negative-answer", "not included")
    port.register_opening_reply(q, "negative-answer")
    wrong = fields(account, coverage="included")
    wrong["coverage"]["span"] = [4, 12]
    port.commands["wrong-negative"] = {
        "format": "finance-daily-movement-command-v1",
        "command_kind": "movement_create",
        "fields": wrong,
        "reason_span": None,
    }
    with pytest.raises(LifecycleError, match="complete genuine choice"):
        api.propose_movement_create(
            source.source_id,
            "wrong-negative",
            1,
            "propose-wrong-negative",
            reply_evidence_id="negative-answer",
        )
    assert reader.collect().movements == ()
    proper = fields(account, coverage="excluded")
    proper["coverage"]["span"] = [0, 12]
    port.commands["proper-negative"] = {
        "format": "finance-daily-movement-command-v1",
        "command_kind": "movement_create",
        "fields": proper,
        "reason_span": None,
    }
    proposal = api.propose_movement_create(
        source.source_id,
        "proper-negative",
        1,
        "propose-proper-negative",
        reply_evidence_id="negative-answer",
    )
    port.remember_movement_proposal(proposal)
    card = api.prepare_review(proposal.proposal_id, proposal.version)
    assert card.material["after_values"]["coverage"]["treatment"] == "excluded"
    assert reader.collect().balances[0].balance_amount == "1000.00"
    api.record_delivery(card.review_id, "negative-card-delivery")
    with pytest.raises(LifecycleError):
        api.confirm(card.review_id, "negative-answer", "answer-is-not-confirm")
    result = api.confirm(card.review_id, "negative-independent-action", "negative-confirm")
    assert reader.collect().balances[0].balance_amount == "991.00"
    assert api.confirm(card.review_id, "negative-independent-action", "negative-confirm") == result
    assert reader.collect().balances[0].balance_amount == "991.00"


def test_daily_source_uses_one_guarded_transaction_and_exact_original_identity(live, monkeypatch):
    from dataclasses import asdict

    from finance_core.instance_admission.sessions import _witness_for

    ledger, port, _, _, _ = setup(live)
    original = port.add_source("source-read", "Actual captured original")
    captured = ledger.capture("source-read", "capture-source-read")
    observed = []
    callback = port.source

    def inspect(conn, identity):
        witness = _witness_for(conn)
        observed.append(
            (id(conn), conn.in_transaction, witness.evidence_callback_token is not None)
        )
        return callback(conn, identity)

    monkeypatch.setattr(port, "source", inspect)
    returned = DailyLedgerReaderV1(ledger).source(captured.source_id)
    assert asdict(returned) == asdict(original)
    assert len(observed) == 2
    assert len({item[0] for item in observed}) == 1
    assert all(in_transaction and guarded for _, in_transaction, guarded in observed)


@pytest.mark.parametrize("action", ["commit", "rollback", "close"])
def test_daily_source_final_callback_swallowed_control_is_sticky(live, monkeypatch, action):
    from finance_core.instance_admission import InstanceAdmissionError

    ledger, port, _, _, _ = setup(live)
    original = port.add_source("source-control", "Actual original for control proof")
    captured = ledger.capture("source-control", "capture-source-control")
    callback = port.source
    count = 0

    def forbidden(conn, identity):
        nonlocal count
        count += 1
        if count == 2:
            try:
                getattr(conn, action)()
            except InstanceAdmissionError:
                pass
        return callback(conn, identity)

    monkeypatch.setattr(port, "source", forbidden)
    with pytest.raises(InstanceAdmissionError, match="read-only scope"):
        DailyLedgerReaderV1(ledger).source(captured.source_id)
    monkeypatch.setattr(port, "source", callback)
    assert DailyLedgerReaderV1(ledger).source(captured.source_id) == original


@pytest.mark.parametrize("bad", ["subclass", "timestamp_bool", "identity", "hash", "actor", "dict"])
def test_daily_source_final_callback_rejects_wrong_dto_scalar_or_identity(live, monkeypatch, bad):
    from dataclasses import asdict, replace

    from finance_core.lifecycle_v2 import VerifiedSource

    ledger, port, _, _, _ = setup(live)
    port.add_source("source-invalid", "Actual captured identity")
    captured = ledger.capture("source-invalid", "capture-source-invalid")
    callback = port.source
    count = 0

    def invalid(conn, identity):
        nonlocal count
        count += 1
        value = callback(conn, identity)
        if count != 2:
            return value
        if bad == "subclass":

            class DerivedSource(VerifiedSource):
                pass

            return DerivedSource(**asdict(value))
        if bad == "timestamp_bool":
            return replace(value, source_timestamp=True)
        if bad == "identity":
            return replace(value, evidence_id="other-source")
        if bad == "hash":
            return replace(value, original_sha256="0" * 64)
        if bad == "actor":
            return replace(value, principal_id="other")
        return asdict(value)

    monkeypatch.setattr(port, "source", invalid)
    with pytest.raises(LifecycleError):
        DailyLedgerReaderV1(ledger).source(captured.source_id)
