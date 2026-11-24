import sqlite3
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from types import SimpleNamespace

import pytest

from finance_core.instance_admission import acquire_production_owner
from finance_core.lifecycle_v2 import LifecycleError
from finance_core.lifecycle_v2.contracts import (
    RuntimeBinding,
    VerifiedAction,
    VerifiedSource,
    canonical,
    digest,
)
from finance_core.lifecycle_v2.daily_v1.accounts import DailyAccountServiceV1
from finance_core.lifecycle_v2.daily_v1.contracts import (
    BUNDLE_FORMAT,
    BundleFactRefV1,
    instruction_ref,
    strict_json,
)
from finance_core.lifecycle_v2.daily_v1.ledger_reader import DailyLedgerReaderV1
from finance_core.lifecycle_v2.daily_v1.movement_contracts import MOVEMENT_FORMAT
from finance_core.lifecycle_v2.daily_v1.refund_allowance_v1 import (
    ActiveRefundAllocationV1,
    RefundExpenseV1,
    compile_refund_allowance_v1,
)
from finance_core.lifecycle_v2.daily_v1.refund_dependencies_v1 import (
    RefundPostingStateV1,
    refund_posting_state_v1,
)
from finance_core.lifecycle_v2.daily_v1.refund_drafts_v1 import (
    DailyRefundServiceV1,
    RefundAlreadyReversedV1,
)
from finance_core.lifecycle_v2.daily_v1.refund_evidence_v1 import (
    OPENING_QUESTION_FORMAT,
    TARGET_FORMAT,
    VerifiedRefundOpeningReplyV1,
    VerifiedRefundTargetV1,
    verify_refund_evidence_v1,
)
from finance_core.lifecycle_v2.daily_v1.refund_facts_v1 import (
    REFUND_FORMAT,
    project_refund_fact_v1,
    require_refund_predecessor_v1,
)
from finance_core.lifecycle_v2.daily_v1.refund_inventory_v1 import (
    LinkedRefundConflictV1,
    active_refund_allocations_v1,
)
from finance_core.lifecycle_v2.daily_v1.refund_review_v1 import (
    _compact_dependencies,
    read_refund_review_v1,
)
from finance_core.lifecycle_v2.effective_v1 import EffectiveLedgerReaderV1
from finance_core.lifecycle_v2.recovery import verify_snapshot_evidence
from finance_core.lifecycle_v2.service import LedgerService
from tests.test_daily_movements_lifecycle_v1 import fields, movement, prepare
from tests.test_daily_opening_changes_v1 import (
    finish,
    opening_command,
    opening_proposal,
    prepare_opening,
    row_reply,
)
from tests.test_daily_transfer_facts_v1 import instruction
from tests.test_lifecycle_v2 import live as live
from tests.test_lifecycle_v2 import response


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
    future = deepcopy(refund)
    future["expense"]["fact_ref"] = asdict(current)
    future_credit = future["credit"]
    future_credit["opening_ref"] = None
    future_credit["coverage"].update(
        treatment="no_baseline", basis="no_active_opening", reply_ref=None
    )
    _bind_opening(future)
    receiving_account = {
        "format": BUNDLE_FORMAT,
        "account": {**future_credit["account_ref"], "currency": future_credit["currency"]},
        "opening": None,
    }

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
                ("card", 1, "9" * 64, 10, "card-event", canonical(receiving_account)),
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

        before_second = refund_posting_state_v1(conn, future, before_seq=40)
        assert before_second.remaining_original_after == "200"
        with pytest.raises(LifecycleError, match="exceed the expense"):
            refund_posting_state_v1(conn, future, before_seq=50)
        after_reversal = refund_posting_state_v1(conn, future)
        assert after_reversal.remaining_original_after == "0"
        assert before_second.state_sha256 != after_reversal.state_sha256
        with pytest.raises(LifecycleError, match="expense version changed"):
            refund_posting_state_v1(conn, refund)
        reversal = refund_posting_state_v1(
            conn, reversed_first, BundleFactRefV1("refund-a", 1, "1" * 64, 30), before_seq=50
        )
        assert reversal.remaining_original_after == "400"

        second["expense"]["fact_ref"]["fact_sha256"] = "f" * 64
        _bind_opening(second)
        conn.execute(
            "UPDATE l2_facts SET values_json=? WHERE record_id='refund-b'", (canonical(second),)
        )
        with pytest.raises(LifecycleError, match="lacks its exact observed expense fact"):
            active_refund_allocations_v1(conn, expense)


@pytest.mark.parametrize("scenario", ["refund_void", "refund_correct", "reassociation"])
def test_refund_confirmation_dependencies_use_current_verified_opening_date(
    live, refund, tmp_path, scenario, monkeypatch
):
    transition = "refund_correct" if scenario == "reassociation" else scenario
    opening = {
        "format": "finance-daily-as-of-v1",
        "precision": "date",
        "timestamp": None,
        "local_date": "2026-11-18",
        "timezone": "Asia/Singapore",
    }
    ledger, port, _, movements, reader, account = prepare(
        live, opening="100", opening_as_of=opening, currency="SGD"
    )
    values = fields(account, amount="9", original="1000", currency="JPY")
    _, _, receipt = movement(ledger, port, movements, account, "purchase", values)
    original = reader.detail(receipt.receipt.record_id)
    real_expense = None
    if scenario == "reassociation":
        _, _, actual_charge = movement(ledger, port, movements, account, "actual-purchase", values)
        real_expense = reader.detail(actual_charge.receipt.record_id)
    refund["expense"] = {
        "fact_ref": asdict(original.fact_ref),
        "economic_event_id": original.economic_event_id,
    }
    refund["received_date"] = "2026-11-19"
    credit = refund["credit"]
    credit["account_ref"] = original.values["account_ref"]
    credit["opening_ref"] = original.values["opening_ref"]
    credit["coverage"].update(treatment="excluded", basis="strictly_later", reply_ref=None)
    _bind_opening(refund)

    with DailyAccountServiceV1(ledger)._read() as (_, conn):
        reviewed = refund_posting_state_v1(conn, refund)
        assert reviewed.remaining_original_after == "0"
        assert reviewed.credit_account_fact.record_id == account
        refund["received_date"] = opening["local_date"]
        _bind_opening(refund)
        with pytest.raises(LifecycleError, match="actual opening time"):
            refund_posting_state_v1(conn, refund)

    raw = "The shop refunded my earlier purchase"
    actual = VerifiedSource(
        "refund-capture",
        "user",
        "chat",
        "refund-source-key",
        raw,
        sha256(raw.encode()).hexdigest(),
        1792937000,
    )
    port.sources[actual.evidence_id] = actual
    captured = ledger.capture(actual.evidence_id, "capture-refund-preview")

    refund["received_date"] = "2026-11-19"
    refund["instruction_ref"] = instruction_ref(asdict(actual), ledger.binding)
    if scenario == "refund_void":
        refund["received_date"] = opening["local_date"]
        raw_opening = "未包含"
        reply = VerifiedSource(
            "refund-actual-opening-answer",
            "user",
            "chat",
            "refund-opening-message",
            raw_opening,
            sha256(raw_opening.encode()).hexdigest(),
            actual.source_timestamp + 2,
        )
        port.sources[reply.evidence_id] = reply
        coverage = refund["credit"]["coverage"]
        coverage.update(
            basis="authenticated_reply",
            reply_ref=instruction_ref(asdict(reply), ledger.binding),
        )
    _bind_opening(refund)
    if scenario == "refund_void":
        opening_question = canonical(
            {
                "format": OPENING_QUESTION_FORMAT,
                "delivery_id": "refund-actual-opening-question",
                "delivered_at": actual.source_timestamp + 1,
                "credit_sha256": refund["credit"]["coverage"]["binding_sha256"],
            }
        )

        def retained_opening(conn, identity):
            assert identity == reply.evidence_id
            return VerifiedRefundOpeningReplyV1(reply.evidence_id, opening_question)

        port.refund_opening_reply_v1 = retained_opening
    retained = {
        "format": TARGET_FORMAT,
        "transition": "refund_create",
        "instruction_ref": refund["instruction_ref"],
        "original_expense": deepcopy(refund["expense"]),
        "refund_predecessor": None,
        "reason_span": None,
    }

    def retained_target(conn, identity):
        assert identity == actual.evidence_id
        return VerifiedRefundTargetV1("chosen-original-expense", canonical(retained))

    port.refund_target_v1 = retained_target
    audit = ledger.reader().full_audit()
    draft = read_refund_review_v1(ledger, captured.source_id, refund)
    material = strict_json(draft.material_json)
    assert draft.material_sha256 == digest(material)
    assert material["after_values"]["original"] == {"currency": "JPY", "amount": "1000"}
    assert material["dependencies"]["remaining_original_after"] == "0"
    assert material["account_impact_changes"] == [
        {
            "account_id": account,
            "currency": "SGD",
            "recorded_change": "9.20",
            "balance_change": "9.20",
        }
    ]
    assert ledger.reader().full_audit() == audit
    retained["original_expense"]["fact_ref"]["record_id"] = "wrong-expense"
    with pytest.raises(LifecycleError, match="retained expense"):
        read_refund_review_v1(ledger, captured.source_id, refund)
    assert ledger.reader().full_audit() == audit

    retained["original_expense"] = deepcopy(refund["expense"])
    drafts = DailyRefundServiceV1(ledger)
    proposal = drafts.propose(captured.source_id, refund, "refund-observation", "propose-refund")
    assert proposal.version == 1
    after = ledger.reader().full_audit()
    assert after["event_count"] == audit["event_count"] + 1
    assert after["fact_version_count"] == audit["fact_version_count"]
    assert (
        drafts.propose(captured.source_id, refund, "refund-observation", "propose-refund")
        == proposal
    )
    with pytest.raises(LifecycleError, match="replay mismatch"):
        drafts.propose(captured.source_id, refund, "refund-observation", "different-operation")
    assert ledger.reader().full_audit() == after

    if scenario == "refund_void":
        from finance_core.lifecycle_v2.daily_v1 import refund_drafts_v1

        normal = refund_drafts_v1._review_material
        with monkeypatch.context() as scoped:
            scoped.setattr(
                refund_drafts_v1,
                "_review_material",
                lambda *args, **kwargs: {**normal(*args, **kwargs), "large": "x" * 132000},
            )
            with pytest.raises(LifecycleError, match="Bounded exact JSON"):
                drafts.prepare_review(proposal.proposal_id, proposal.version)
        assert ledger.reader().full_audit() == after
    review = drafts.prepare_review(proposal.proposal_id, proposal.version)
    assert {
        **review.material["refund"],
        "after_values": review.material["after_values"],
    } == material
    assert review.delivery_evidence_id is None
    delivered = drafts.record_delivery(review.review_id, "actual-refund-card-delivery")
    assert delivered.delivery_evidence_id == "actual-refund-card-delivery"
    assert delivered.review_sha256 == review.review_sha256
    assert drafts.record_delivery(review.review_id, "actual-refund-card-delivery") == delivered
    with pytest.raises(LifecycleError, match="delivery replay mismatch"):
        drafts.record_delivery(review.review_id, "different-card-delivery")
    with pytest.raises(LifecycleError, match="use the explicit daily API"):
        ledger.confirm(review.review_id, "legacy-confirm", "legacy-confirm-refund")
    after_delivery = ledger.reader().full_audit()
    assert after_delivery["event_count"] == after["event_count"] + 2
    assert after_delivery["fact_version_count"] == audit["fact_version_count"]

    assert (
        drafts.lookup_commit_receipt(
            "confirm-first-refund", review_id=review.review_id, action_evidence_id="refund-confirm"
        )
        is None
    )
    with pytest.raises(LifecycleError, match="separate human action"):
        drafts.confirm(review.review_id, actual.evidence_id, "reuse-refund-instruction")
    assert ledger.reader().full_audit() == after_delivery
    if scenario == "refund_void":
        invalid_actions = {
            "old-card-action": VerifiedAction(
                "old-card-action",
                "older-review",
                "d" * 64,
                delivered.delivery_evidence_id,
                "user",
                "chat",
            ),
            "wrong-actor-action": VerifiedAction(
                "wrong-actor-action",
                review.review_id,
                review.review_sha256,
                delivered.delivery_evidence_id,
                "wrong-user",
                "chat",
            ),
            "wrong-delivery-action": VerifiedAction(
                "wrong-delivery-action",
                review.review_id,
                review.review_sha256,
                "wrong-card-delivery",
                "user",
                "chat",
            ),
        }
        previous_action = port.action

        def retained_action(conn, identity, expected):
            if identity in invalid_actions:
                return invalid_actions[identity]
            return previous_action(conn, identity, expected)

        with monkeypatch.context() as scoped:
            scoped.setattr(port, "action", retained_action)
            for identity in invalid_actions:
                with pytest.raises(LifecycleError, match="action differs"):
                    drafts.confirm(review.review_id, identity, identity + "-operation")
        with pytest.raises(LifecycleError, match="separate human action"):
            drafts.confirm(review.review_id, reply.evidence_id, "reuse-reply")
        with monkeypatch.context() as scoped:
            scoped.setattr(ledger, "clock", lambda: review.material["expires_at"])
            with pytest.raises(LifecycleError, match="unexpired authenticated delivery"):
                drafts.confirm(review.review_id, "later-action", "late-operation")
        assert ledger.reader().full_audit() == after_delivery
    posted = drafts.confirm(review.review_id, "refund-confirm", "confirm-first-refund")
    assert posted.version == 1
    assert drafts.confirm(review.review_id, "refund-confirm", "confirm-first-refund") == posted
    assert (
        drafts.lookup_commit_receipt(
            "confirm-first-refund", review_id=review.review_id, action_evidence_id="refund-confirm"
        )
        == posted
    )
    after_confirmation = ledger.reader().full_audit()
    assert after_confirmation["fact_version_count"] == audit["fact_version_count"] + 1
    assert after_confirmation["event_count"] == after_delivery["event_count"] + 1
    with pytest.raises(LifecycleError, match="exceed the expense"):
        read_refund_review_v1(ledger, captured.source_id, refund)
    assert ledger.reader().full_audit() == after_confirmation

    snapshot = reader.collect_with_refunds_v3()
    assert snapshot.collection.income_by_currency == ()
    assert snapshot.collection.expense_by_currency == (
        ("JPY", "2000" if real_expense is not None else "1000"),
    )
    assert snapshot.refund_by_currency == (("JPY", "1000"),)
    assert snapshot.net_expense_by_currency == (
        ("JPY", "1000" if real_expense is not None else "0"),
    )
    starting_balance = "91.20" if real_expense is not None else "100.20"
    assert snapshot.collection.balances[0].balance_amount == starting_balance
    assert reader.collect_accounts().unassigned_personal_expense_count == 0
    assert reader.detail(original.record_id).fact_ref == original.fact_ref
    assert reader.history(original.record_id)[-1].fact_ref == original.fact_ref
    EffectiveLedgerReaderV1.for_daily_ledger(ledger).collect()
    assert snapshot.refunds[0].fact_ref.record_id == posted.record_id

    with pytest.raises(LinkedRefundConflictV1, match="active linked refunds") as blocked:
        movement(
            ledger,
            port,
            movements,
            account,
            "void-original-purchase",
            {},
            target=original,
            kind="movement_void",
        )
    assert blocked.value.refund_refs == (snapshot.refunds[0].fact_ref,)
    assert (
        reader.collect_with_refunds_v3().collection.balances[0].balance_amount == starting_balance
    )

    reverse_source = VerifiedSource(
        "refund-reverse-source",
        "user",
        "chat",
        "separate-refund-reverse-source-key",
        "Merchant confirmed the refund",
        sha256(b"Merchant confirmed the refund").hexdigest(),
        1792937010,
    )
    port.sources[reverse_source.evidence_id] = reverse_source
    reverse = _change(refund, transition)
    if transition == "refund_void":
        reverse["status"] = "VOID"
    elif real_expense is not None:
        reverse["expense"] = {
            "fact_ref": asdict(real_expense.fact_ref),
            "economic_event_id": real_expense.economic_event_id,
        }
    else:
        reverse["original"]["amount"] = "900"
        reverse["credit"]["amount"] = "8.24"
    reverse_instruction = instruction_ref(asdict(reverse_source), ledger.binding)
    reverse["instruction_ref"] = reverse_instruction
    reverse["reason_ref"]["instruction_ref"] = reverse_instruction
    if transition == "refund_correct":
        _bind_opening(reverse)
    original_refund = snapshot.refunds[0].fact_ref

    def target_for_both(conn, identity):
        if identity == actual.evidence_id:
            return retained_target(conn, identity)
        assert identity == reverse_source.evidence_id
        return VerifiedRefundTargetV1(
            "proof-of-refund-reversal-target",
            canonical(
                {
                    "format": TARGET_FORMAT,
                    "transition": transition,
                    "instruction_ref": reverse_instruction,
                    "original_expense": reverse["expense"],
                    "refund_predecessor": asdict(original_refund),
                    "reason_span": reverse["reason_ref"]["span"],
                }
            ),
        )

    port.refund_target_v1 = target_for_both
    reverse_proposal = drafts.propose(
        captured.source_id,
        reverse,
        "reverse-observation",
        "propose-reversal",
        expected_proposal_version=1,
        replaced=original_refund,
    )
    reverse_review = drafts.prepare_review(reverse_proposal.proposal_id, reverse_proposal.version)
    if real_expense is not None:
        relink = reverse_review.material["refund"]
        assert relink["dependencies"]["previous_allocation"]["remaining_original_after"] == "1000"
        assert relink["account_impact_changes"][0]["balance_change"] == "0.00"
    drafts.record_delivery(reverse_review.review_id, "refund-reversal-card")
    reversed_posting = drafts.confirm(
        reverse_review.review_id, "refund-reversal-confirmation", "confirm-reversal"
    )
    assert (reversed_posting.record_id, reversed_posting.version) == (posted.record_id, 2)
    after_reversal = reader.collect_with_refunds_v3()
    history = reader.refund_history_v1(posted.record_id)
    assert [view.fact_ref.version for view in history] == [1, 2]
    assert history[0].values["credit"] == refund["credit"]
    assert history[1].values["credit"] == reverse["credit"]
    if transition == "refund_void":
        assert after_reversal.refund_by_currency == ()
        assert after_reversal.net_expense_by_currency == (("JPY", "1000"),)
        assert after_reversal.collection.balances[0].balance_amount == "91.00"
        assert history[1].values["status"] == "VOID"
        with pytest.raises(RefundAlreadyReversedV1) as repeat:
            drafts.propose(
                captured.source_id,
                reverse,
                "fresh-second-reversal",
                "fresh-reversal-op",
                expected_proposal_version=2,
                replaced=history[1].fact_ref,
            )
        assert repeat.value.current == history[1].fact_ref
        assert (
            drafts.lookup_commit_receipt(
                "confirm-reversal",
                review_id=reverse_review.review_id,
                action_evidence_id="refund-reversal-confirmation",
            )
            == reversed_posting
        )
    elif real_expense is not None:
        assert history[1].values["expense"]["fact_ref"] == asdict(real_expense.fact_ref)
        assert after_reversal.collection.balances[0].balance_amount == starting_balance
        movement(
            ledger,
            port,
            movements,
            account,
            "void-duplicate-purchase",
            {},
            target=original,
            kind="movement_void",
        )
        after_reversal = reader.collect_with_refunds_v3()
        assert after_reversal.collection.balances[0].balance_amount == "100.20"
        assert after_reversal.net_expense_by_currency == (("JPY", "0"),)
    else:
        assert after_reversal.refund_by_currency == (("JPY", "900"),)
        assert after_reversal.net_expense_by_currency == (("JPY", "100"),)
        assert after_reversal.collection.balances[0].balance_amount == "99.24"
        assert history[1].values["status"] == "ACTIVE"
    snapshot_path = tmp_path / "verified-refund.sqlite"
    cut = ledger.seal_snapshot(snapshot_path)
    audited = verify_snapshot_evidence(snapshot_path, port, ledger.binding, cut)
    assert cut.logical_database_sha256 == after_reversal.read_identity.logical_database_sha256
    assert audited["fact_version_count"] == after_reversal.collection.fact_version_count


def test_refund_selection_and_opening_answer_are_reconstructed_by_actual_identity(refund):
    binding = RuntimeBinding("synthetic", "generation", "synthetic-owner", "synthetic-chat")

    def source(evidence_id, raw, timestamp):
        return VerifiedSource(
            evidence_id,
            binding.principal_id,
            binding.client_id,
            "source-" + evidence_id,
            raw,
            sha256(raw.encode()).hexdigest(),
            timestamp,
        )

    written = source("refund-input", "Refund for my old purchase", 1780000000)
    answer = source("opening-answer", "已包含", 1780000002)
    refund["instruction_ref"] = instruction_ref(asdict(written), binding)
    coverage = refund["credit"]["coverage"]
    coverage["reply_ref"] = instruction_ref(asdict(answer), binding)
    _bind_opening(refund)

    selected = {
        "format": TARGET_FORMAT,
        "transition": "refund_create",
        "instruction_ref": refund["instruction_ref"],
        "original_expense": deepcopy(refund["expense"]),
        "refund_predecessor": None,
        "reason_span": None,
    }
    question = {
        "format": OPENING_QUESTION_FORMAT,
        "delivery_id": "real-opening-question",
        "delivered_at": 1780000001,
        "credit_sha256": coverage["binding_sha256"],
    }
    with sqlite3.connect(":memory:") as conn:
        conn.execute("BEGIN")
        chosen = VerifiedRefundTargetV1("retained-selection", canonical(selected))

        def get_selection(actual_conn, identity):
            assert (actual_conn, identity) == (conn, written.evidence_id)
            return chosen

        def get_reply(actual_conn, identity):
            assert (actual_conn, identity) == (conn, answer.evidence_id)
            return answer

        def get_question(actual_conn, identity):
            assert (actual_conn, identity) == (conn, answer.evidence_id)
            return VerifiedRefundOpeningReplyV1(identity, canonical(question))

        service = SimpleNamespace(
            port=SimpleNamespace(
                refund_target_v1=get_selection,
                source=get_reply,
                refund_opening_reply_v1=get_question,
            ),
        )
        result = verify_refund_evidence_v1(service, conn, binding, written, refund, None)
        assert result["selection"] == asdict(chosen)
        assert result["opening_question"] == {
            "reply_evidence_id": answer.evidence_id,
            "question": question,
        }

        chosen = VerifiedRefundTargetV1(answer.evidence_id, canonical(selected))
        with pytest.raises(LifecycleError, match="require separate proof"):
            verify_refund_evidence_v1(service, conn, binding, written, refund, None)

        wrong = deepcopy(selected)
        wrong["original_expense"]["fact_ref"]["record_id"] = "another-purchase"
        chosen = VerifiedRefundTargetV1("retained-selection", canonical(wrong))
        with pytest.raises(LifecycleError, match="retained expense, target or reason"):
            verify_refund_evidence_v1(service, conn, binding, written, refund, None)

        for bad_version in (True, 1.0):
            wrong_type = deepcopy(selected)
            wrong_type["original_expense"]["fact_ref"]["version"] = bad_version
            chosen = VerifiedRefundTargetV1("retained-selection", canonical(wrong_type))
            with pytest.raises(LifecycleError, match="retained expense, target or reason"):
                verify_refund_evidence_v1(service, conn, binding, written, refund, None)

        chosen = VerifiedRefundTargetV1("retained-selection", canonical(selected))
        refund["credit"]["amount"] = "9.50"
        _bind_opening(refund)
        with pytest.raises(LifecycleError, match="exact delivered question"):
            verify_refund_evidence_v1(service, conn, binding, written, refund, None)


def test_two_delivered_refunds_cannot_spend_the_same_original_allowance(live, refund):
    (
        ledger,
        port,
        _,
        movements,
        reader,
        openings,
        opening_source,
        opening_draft,
        account,
        config,
    ) = prepare_opening(live, opening=None, currency="SGD")
    original_values = fields(account, amount="9", original="1000", currency="JPY")
    original_values["coverage"] = None
    _, _, purchase = movement(ledger, port, movements, account, "purchase-race", original_values)
    original = reader.detail(purchase.receipt.record_id)
    refund["expense"] = {
        "fact_ref": asdict(original.fact_ref),
        "economic_event_id": original.economic_event_id,
    }
    credit = refund["credit"]
    credit["account_ref"] = original.values["account_ref"]
    credit["opening_ref"] = None
    credit["coverage"].update(treatment="no_baseline", basis="no_active_opening", reply_ref=None)

    targets = {}
    port.refund_target_v1 = lambda conn, identity: targets[identity]
    refunds = DailyRefundServiceV1(ledger)
    delivered = []
    for name, original_amount, credited in (("first", "600", "5.52"), ("second", "500", "4.61")):
        text = f"Received {name} refund"
        source = VerifiedSource(
            f"{name}-refund",
            "user",
            "chat",
            f"{name}-refund-message",
            text,
            sha256(text.encode()).hexdigest(),
            1792937010,
        )
        port.sources[source.evidence_id] = source
        captured = ledger.capture(source.evidence_id, f"capture-{name}-refund")
        values = deepcopy(refund)
        values["original"]["amount"] = original_amount
        values["credit"]["amount"] = credited
        instruction = instruction_ref(asdict(source), ledger.binding)
        values["instruction_ref"] = instruction
        _bind_opening(values)
        targets[source.evidence_id] = VerifiedRefundTargetV1(
            f"{name}-selection",
            canonical(
                {
                    "format": TARGET_FORMAT,
                    "transition": "refund_create",
                    "instruction_ref": instruction,
                    "original_expense": refund["expense"],
                    "refund_predecessor": None,
                    "reason_span": None,
                }
            ),
        )
        draft = refunds.propose(
            captured.source_id, values, f"{name}-observation", f"propose-{name}-refund"
        )
        review = refunds.prepare_review(draft.proposal_id, draft.version)
        delivered.append(refunds.record_delivery(review.review_id, f"card-{name}"))

    before = ledger.reader().full_audit()
    first = refunds.confirm(delivered[0].review_id, "approve-first", "post-first")
    with pytest.raises(LifecycleError, match="exceed the expense"):
        refunds.confirm(delivered[1].review_id, "approve-second", "post-second")
    assert (
        refunds.lookup_commit_receipt(
            "post-second", review_id=delivered[1].review_id, action_evidence_id="approve-second"
        )
        is None
    )
    after = ledger.reader().full_audit()
    assert after["fact_version_count"] == before["fact_version_count"] + 1
    assert after["event_count"] == before["event_count"] + 1
    pending_refund = refunds.propose(
        captured.source_id,
        values,
        "second-oversized",
        "recheck-second-oversized",
        expected_proposal_version=1,
    )
    state = refunds.proposal_state_v1(pending_refund.proposal_id)
    assert (state["status"], state["available_original"], state["requested_original"]) == (
        "pending_allowance",
        "400",
        "500",
    )
    assert state["original_expense"] == asdict(original.fact_ref)
    with pytest.raises(LifecycleError, match="requires a new eligible proposal"):
        refunds.prepare_review(pending_refund.proposal_id, pending_refund.version)
    assert ledger.reader().full_audit()["fact_version_count"] == after["fact_version_count"]
    snapshot = reader.collect_with_refunds_v3()
    assert snapshot.refunds[0].record_id == first.record_id
    assert snapshot.net_expense_by_currency == (("JPY", "400"),)
    assert snapshot.collection.balances[0].recorded_net_change == "-3.48"

    boundary = {
        "format": "finance-daily-as-of-v1",
        "precision": "date",
        "timestamp": None,
        "local_date": refund["received_date"],
        "timezone": "Asia/Singapore",
    }
    account_view = reader.collect_accounts().accounts[0]
    pending = opening_proposal(
        ledger,
        port,
        openings,
        opening_source,
        opening_draft,
        account_view,
        "opening-with-refund",
        opening_command(kind="opening_set", amount="110", boundary=boundary),
    )
    question = openings.opening_questions_v1(pending.proposal_id)
    subjects = {item["impact_role"]: item for item in question["subjects"]}
    assert set(subjects) == {"debit", "refund_credit"}
    assert subjects["refund_credit"]["original_amount"] == "600"
    assert subjects["refund_credit"]["account_amount"] == "5.52"
    delivered_opening = port.deliver_opening_question(question, "opening-with-refund-delivery")
    answers = row_reply(
        port,
        "opening-with-refund-answer",
        (subjects["debit"], "excluded"),
        (subjects["refund_credit"], "included"),
    )
    ready = opening_proposal(
        ledger,
        port,
        openings,
        opening_source,
        pending,
        account_view,
        "opening-with-refund-answer",
        opening_command(kind="opening_set", amount="110", boundary=boundary, answers=answers),
        "opening-with-refund",
        delivered_opening,
    )
    finish(openings, ready, "opening-with-refund-answer")
    mapped = reader.collect_with_refunds_v3()
    assert mapped.collection.balances[0].balance_amount == "101.00"
    assert mapped.net_expense_by_currency == snapshot.net_expense_by_currency
    effects = {effect.values["impact_role"]: effect.values for effect in mapped.effects}
    assert effects["refund_credit"]["treatment"] == "included"
    assert effects["refund_credit"]["balance_impact"] == "0.00"
    assert effects["refund_credit"]["recorded_change"] == "5.52"

    ledger.owner.close()
    with acquire_production_owner(config) as owner:
        reopened = LedgerService(owner, port, ledger.binding, ledger.clock)
        reopened_refunds = DailyRefundServiceV1(reopened)
        assert reopened_refunds.proposal_state_v1(pending_refund.proposal_id) == state
        with pytest.raises(LifecycleError, match="requires a new eligible proposal"):
            reopened_refunds.prepare_review(pending_refund.proposal_id, pending_refund.version)


def test_large_valid_refund_inventory_binds_exactly_without_overflowing_a_review():
    original = BundleFactRefV1("movement-" + "a" * 64, 1, "b" * 64, 2)
    expense = RefundExpenseV1(original, "source-a", "JPY", "999")
    active = tuple(
        ActiveRefundAllocationV1(
            BundleFactRefV1(f"refund-{index:064x}", 1, f"{index:064x}", index + 3),
            original,
            "source-a",
            "JPY",
            "1",
        )
        for index in range(500)
    )
    allowance = compile_refund_allowance_v1(expense, active)
    state = RefundPostingStateV1(allowance, None, None, "498", "c" * 64)
    assert len(canonical(asdict(state)).encode()) > 131072
    summary = strict_json(canonical(_compact_dependencies(state)))
    assert summary["allocation_count"] == 500
    assert summary["refunded_original"] == "500"
    assert summary["remaining_original_before"] == "499"
    assert summary["allowance_sha256"] == allowance.allowance_sha256
    amended = compile_refund_allowance_v1(expense, active[:-1])
    assert amended.allowance_sha256 != summary["allowance_sha256"]


def _refund_instruction(
    port, ledger, value, targets, identity, *, preparation_id=None, previous=None
):
    reason = value["reason_ref"]
    original = reason["quote"] if reason else "Refund received for the selected purchase"
    source = VerifiedSource(
        identity,
        "user",
        "chat",
        "message-" + identity,
        original,
        sha256(original.encode()).hexdigest(),
        1792937020,
    )
    port.sources[identity] = source
    value["instruction_ref"] = instruction_ref(asdict(source), ledger.binding)
    if reason is not None:
        reason["instruction_ref"] = value["instruction_ref"]
    if value["status"] != "VOID":
        _bind_opening(value)
    selection = {
        "format": TARGET_FORMAT,
        "transition": value["transition"],
        "instruction_ref": value["instruction_ref"],
        "original_expense": value["expense"],
        "refund_predecessor": asdict(previous) if previous is not None else None,
        "reason_span": reason["span"] if reason else None,
    }
    if preparation_id is not None:
        selection["preparation_id"] = preparation_id
    targets[identity] = VerifiedRefundTargetV1("selection-" + identity, canonical(selection))
    return source


@pytest.mark.parametrize("path", ["direct", "reselect", "both_pending"])
def test_same_day_opening_draft_resumes_after_restart_with_exact_new_reply(
    live, refund, path, monkeypatch
):
    boundary = {
        "format": "finance-daily-as-of-v1",
        "precision": "date",
        "timestamp": None,
        "local_date": "2026-11-24",
        "timezone": "Asia/Singapore",
    }
    ledger, port, _, movements, reader, account = prepare(
        live, opening="100", opening_as_of=boundary, currency="SGD"
    )
    values = fields(account, amount="9", original="1000", currency="JPY")
    _, _, purchase = movement(ledger, port, movements, account, "initial-purchase", values)
    original = reader.detail(purchase.receipt.record_id)
    refund["expense"] = {
        "fact_ref": asdict(original.fact_ref),
        "economic_event_id": original.economic_event_id,
    }
    refund["received_date"] = boundary["local_date"]
    credit = refund["credit"]
    credit["account_ref"] = original.values["account_ref"]
    credit["opening_ref"] = original.values["opening_ref"]
    credit["coverage"].update(treatment="unknown", basis="awaiting_reply", reply_ref=None)
    targets = {}
    port.refund_target_v1 = lambda conn, identity: targets[identity]
    source = _refund_instruction(port, ledger, refund, targets, "coverage-pending")
    captured = ledger.capture(source.evidence_id, "capture-pending-coverage")
    service = DailyRefundServiceV1(ledger)
    competing = None
    if path == "both_pending":
        other = deepcopy(refund)
        other["original"]["amount"] = "200"
        other["received_date"] = "2026-11-25"
        other["credit"]["amount"] = "1.84"
        other["credit"]["coverage"].update(treatment="excluded", basis="strictly_later")
        other_input = _refund_instruction(port, ledger, other, targets, "other-actual-refund")
        other_capture = ledger.capture(other_input.evidence_id, "capture-other-actual-refund")
        other_draft = service.propose(
            other_capture.source_id, other, "other-observation", "propose-other"
        )
        other_card = service.prepare_review(other_draft.proposal_id, other_draft.version)
        service.record_delivery(other_card.review_id, "deliver-other-card")
        other_receipt = service.confirm(other_card.review_id, "accept-other", "confirm-other")
        competing = BundleFactRefV1(
            other_receipt.record_id,
            other_receipt.version,
            other_receipt.fact_sha256,
            other_receipt.commit_seq,
        )
    before = ledger.reader().full_audit()
    pending = service.propose(captured.source_id, refund, "pending-coverage", "save-pending")
    state = service.proposal_state_v1(pending.proposal_id)
    assert state["status"] == "pending_opening"
    assert state["pending_opening_sha256"] == credit["coverage"]["binding_sha256"]
    assert ledger.reader().full_audit()["fact_version_count"] == before["fact_version_count"]
    with pytest.raises(LifecycleError, match="requires a new eligible proposal"):
        service.prepare_review(pending.proposal_id, pending.version)

    if path == "reselect":
        replacement = fields(account, amount="9", original="1200", currency="JPY")
        _, _, amended = movement(
            ledger,
            port,
            movements,
            account,
            "amend-before-open-answer",
            replacement,
            target=original,
            kind="movement_correct",
        )
        current = reader.detail(amended.receipt.record_id)
        refund["expense"] = {
            "fact_ref": asdict(current.fact_ref),
            "economic_event_id": current.economic_event_id,
        }
        source = _refund_instruction(
            port,
            ledger,
            refund,
            targets,
            "choose-new-version",
            preparation_id=pending.proposal_id,
        )
        pending = service.propose(
            captured.source_id,
            refund,
            "reselected",
            "save-reselected",
            expected_proposal_version=pending.version,
        )
        state = service.proposal_state_v1(pending.proposal_id)
        assert state["status"] == "pending_opening"
        assert state["original_expense"] == asdict(current.fact_ref)

    before_confirmation = ledger.reader().full_audit()["fact_version_count"]
    ledger.owner.close()
    with acquire_production_owner(live[2]) as owner:
        reopened = LedgerService(owner, port, ledger.binding, ledger.clock)
        service = DailyRefundServiceV1(reopened)
        assert service.proposal_state_v1(pending.proposal_id) == state
        reply_text = "已包含"
        reply = VerifiedSource(
            "coverage-reply",
            "user",
            "chat",
            "message-opening-coverage",
            reply_text,
            sha256(reply_text.encode()).hexdigest(),
            source.source_timestamp + 2,
        )
        port.sources[reply.evidence_id] = reply
        credit["coverage"].update(
            treatment="included",
            basis="authenticated_reply",
            reply_ref=instruction_ref(asdict(reply), ledger.binding),
        )
        _bind_opening(refund)
        question = {
            "format": OPENING_QUESTION_FORMAT,
            "delivery_id": "coverage-card",
            "delivered_at": source.source_timestamp + 1,
            "credit_sha256": state["pending_opening_sha256"],
            "preparation_id": "old-question-for-another-preparation",
        }

        def retained_question(conn, identity):
            assert identity == reply.evidence_id
            return VerifiedRefundOpeningReplyV1(identity, canonical(question))

        port.refund_opening_reply_v1 = retained_question
        with pytest.raises(LifecycleError, match="exact delivered question"):
            service.propose(
                captured.source_id,
                refund,
                "wrong-opening",
                "wrong-opening-proposal",
                expected_proposal_version=pending.version,
            )
        question["preparation_id"] = pending.proposal_id
        ready = service.propose(
            captured.source_id,
            refund,
            "right-opening",
            "right-opening-proposal",
            expected_proposal_version=pending.version,
        )
        if competing is not None:
            assert service.proposal_state_v1(ready.proposal_id)["status"] == "pending_allowance"
            with pytest.raises(LifecycleError, match="requires a new eligible proposal"):
                service.prepare_review(ready.proposal_id, ready.version)
            void = _change(other, "refund_void")
            void["status"] = "VOID"
            _refund_instruction(port, reopened, void, targets, "void-other", previous=competing)
            release = service.propose(
                other_capture.source_id,
                void,
                "release-other",
                "release-other-proposal",
                expected_proposal_version=1,
                replaced=competing,
            )
            release_card = service.prepare_review(release.proposal_id, release.version)
            service.record_delivery(release_card.review_id, "deliver-release-card")
            service.confirm(release_card.review_id, "accept-release", "confirm-release")
            before_confirmation += 1
        if path == "direct":
            unissued = service.prepare_review(ready.proposal_id, ready.version)
            retained_audit = reopened.reader().full_audit()
            with monkeypatch.context() as scoped:
                scoped.setattr(reopened, "clock", lambda: unissued.material["expires_at"])
                with pytest.raises(LifecycleError, match="requires a fresh proposal"):
                    service.prepare_review(ready.proposal_id, ready.version)
            assert reopened.reader().full_audit() == retained_audit
        ready = service.propose(
            captured.source_id,
            refund,
            "reuse-exact-proof",
            "reuse-exact-proof-op",
            expected_proposal_version=ready.version,
        )
        assert service.proposal_state_v1(ready.proposal_id)["status"] == "eligible"
        review = service.prepare_review(ready.proposal_id, ready.version)
        service.record_delivery(review.review_id, "new-refund-card")
        service.confirm(review.review_id, "confirm-new-refund", "post-new-refund")
        assert reopened.reader().full_audit()["fact_version_count"] == before_confirmation + 1
        with pytest.raises(LifecycleError, match="stale_bundle_ref|stale_proposal"):
            service.prepare_review(pending.proposal_id, pending.version)


def test_native_expense_conflict_retains_preparation_and_denies_stale_delivery(live, refund):
    ledger, port, _, _, reader, _ = prepare(live, opening=None, currency="SGD")
    raw = "2026-10-24 Cafe SGD 12.00"
    port.add_source("native-refund-purchase", raw)
    port.native_raw["native-original-observation"] = response(raw)
    captured = ledger.capture("native-refund-purchase", "capture-native-purchase")
    proposal = ledger.propose(
        captured.source_id, "native-original-observation", 0, "native-propose"
    )
    review = ledger.prepare_review(proposal.proposal_id, proposal.version)
    ledger.record_delivery(review.review_id, "deliver-native-purchase")
    posted = ledger.confirm(review.review_id, "accept-native-purchase", "post-native-purchase")
    original = reader.detail(posted.record_id)

    def native_correction(label, amount, service=ledger):
        command = f"change amount to SGD {amount} reason: original was incorrect"
        reply = f"native-{label}-reply"
        observation = f"native-{label}-observation"
        port.add_source(reply, command)
        port.native_raw[observation] = response(
            command, "reply", {"amount": amount, "currency": "SGD"}
        )
        return service.prepare_correction(
            posted.record_id,
            1,
            observation,
            f"native-{label}-proposal",
            reply_evidence_id=reply,
            reason="original was incorrect",
        )

    old = native_correction("before-refund", "5.00")
    account = reader.collect_accounts().accounts[0]
    refund["expense"] = {
        "fact_ref": asdict(original.fact_ref),
        "economic_event_id": original.economic_event_id,
    }
    refund["original"] = {"currency": "SGD", "amount": "6.00"}
    refund["credit"].update(
        account_ref=asdict(account.account_ref),
        amount="6.00",
        opening_ref=None,
    )
    refund["credit"]["coverage"].update(
        treatment="no_baseline", basis="no_active_opening", reply_ref=None
    )
    targets = {}
    port.refund_target_v1 = lambda conn, identity: targets[identity]
    source = _refund_instruction(port, ledger, refund, targets, "credit-for-native")
    received = ledger.capture(source.evidence_id, "capture-credit-for-native")
    refunds = DailyRefundServiceV1(ledger)
    proposal = refunds.propose(
        received.source_id, refund, "refund-for-native", "propose-credit-native"
    )
    card = refunds.prepare_review(proposal.proposal_id, proposal.version)
    refunds.record_delivery(card.review_id, "deliver-credit-native")
    receipt = refunds.confirm(card.review_id, "accept-credit-native", "post-credit-native")
    audit = ledger.reader().full_audit()
    with pytest.raises(LinkedRefundConflictV1):
        ledger.record_delivery(old.review_id, "attempt-stale-native-delivery")
    assert ledger.reader().full_audit() == audit

    with pytest.raises(LinkedRefundConflictV1) as conflict:
        native_correction("after-refund", "5.00")
    assert conflict.value.refund_refs[0].record_id == receipt.record_id
    saved = conflict.value.pending_proposal
    assert saved is not None
    more = deepcopy(refund)
    more["original"]["amount"] = "7.00"
    more["credit"]["amount"] = "7.00"
    other = _refund_instruction(port, ledger, more, targets, "try-extra-credit")
    extra_source = ledger.capture(other.evidence_id, "capture-extra-credit")
    first_pending = refunds.propose(
        extra_source.source_id, more, "extra-credit", "save-pending-extra-credit"
    )
    assert refunds.proposal_state_v1(first_pending.proposal_id)["status"] == "pending_allowance"
    ledger.owner.close()
    with acquire_production_owner(live[2]) as owner:
        reopen = LedgerService(owner, port, ledger.binding, ledger.clock)
        assert reopen.reader().full_audit()["fact_version_count"] == audit["fact_version_count"]
        with pytest.raises(LinkedRefundConflictV1):
            reopen.prepare_review(saved.proposal_id, saved.version)
        with DailyAccountServiceV1(reopen)._read() as (_, conn):
            assert not conn.execute(
                "SELECT 1 FROM l2_reviews WHERE proposal_id=?", (saved.proposal_id,)
            ).fetchone()
            assert not conn.execute(
                "SELECT 1 FROM l2_deliveries WHERE review_id=?", (old.review_id,)
            ).fetchone()

        updated = native_correction("increase-after-pending", "15.00", reopen)
        reopen.record_delivery(updated.review_id, "deliver-valid-native-correction")
        corrected = reopen.confirm(
            updated.review_id, "accept-valid-native-correction", "post-valid-native-correction"
        )
        assert corrected.version == 2
        current = DailyLedgerReaderV1(reopen).detail(posted.record_id)
        more["expense"] = {
            "fact_ref": asdict(current.fact_ref),
            "economic_event_id": current.economic_event_id,
        }
        _refund_instruction(
            port,
            reopen,
            more,
            targets,
            "reselect-corrected-purchase",
            preparation_id=first_pending.proposal_id,
        )
        refunds = DailyRefundServiceV1(reopen)
        active = refunds.propose(
            extra_source.source_id,
            more,
            "selected-v2",
            "restore-after-eligible-correction",
            expected_proposal_version=1,
        )
        assert refunds.proposal_state_v1(first_pending.proposal_id)["status"] == "pending_allowance"
        assert refunds.proposal_state_v1(active.proposal_id)["status"] == "eligible"
        card = refunds.prepare_review(active.proposal_id, active.version)
        refunds.record_delivery(card.review_id, "deliver-last-refund")
        refunds.confirm(card.review_id, "confirm-last-refund", "post-last-refund")
        final = DailyLedgerReaderV1(reopen).collect_with_refunds_v3()
        assert final.net_expense_by_currency == (("SGD", "2.00"),)
        compatibility = EffectiveLedgerReaderV1.for_daily_ledger(reopen)
        assert compatibility.get(posted.record_id).amount == "15.00"
