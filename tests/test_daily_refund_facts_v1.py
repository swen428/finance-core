import sqlite3
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from types import SimpleNamespace

import pytest

from finance_core.lifecycle_v2 import LifecycleError
from finance_core.lifecycle_v2.contracts import RuntimeBinding, VerifiedSource, canonical, digest
from finance_core.lifecycle_v2.daily_v1.accounts import DailyAccountServiceV1
from finance_core.lifecycle_v2.daily_v1.contracts import (
    BUNDLE_FORMAT,
    BundleFactRefV1,
    instruction_ref,
    strict_json,
)
from finance_core.lifecycle_v2.daily_v1.movement_contracts import MOVEMENT_FORMAT
from finance_core.lifecycle_v2.daily_v1.refund_allowance_v1 import (
    RefundExpenseV1,
    compile_refund_allowance_v1,
)
from finance_core.lifecycle_v2.daily_v1.refund_dependencies_v1 import refund_posting_state_v1
from finance_core.lifecycle_v2.daily_v1.refund_drafts_v1 import DailyRefundServiceV1
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
from finance_core.lifecycle_v2.daily_v1.refund_inventory_v1 import active_refund_allocations_v1
from finance_core.lifecycle_v2.daily_v1.refund_review_v1 import read_refund_review_v1
from tests.test_daily_movements_lifecycle_v1 import fields, movement, prepare
from tests.test_daily_transfer_facts_v1 import instruction
from tests.test_lifecycle_v2 import live as live


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


def test_refund_confirmation_dependencies_use_current_verified_opening_date(live, refund):
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
    _bind_opening(refund)
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
    assert snapshot.collection.expense_by_currency == (("JPY", "1000"),)
    assert snapshot.refund_by_currency == (("JPY", "1000"),)
    assert snapshot.net_expense_by_currency == (("JPY", "0"),)
    assert snapshot.collection.balances[0].balance_amount == "100.20"
    assert snapshot.refunds[0].fact_ref.record_id == posted.record_id

    with pytest.raises(LifecycleError, match="active linked refunds cannot be reversed"):
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
    assert reader.collect_with_refunds_v3().collection.balances[0].balance_amount == "100.20"

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
    reverse = _change(refund, "refund_void")
    reverse["status"] = "VOID"
    reverse_instruction = instruction_ref(asdict(reverse_source), ledger.binding)
    reverse["instruction_ref"] = reverse_instruction
    reverse["reason_ref"]["instruction_ref"] = reverse_instruction
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
                    "transition": "refund_void",
                    "instruction_ref": reverse_instruction,
                    "original_expense": refund["expense"],
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
    drafts.record_delivery(reverse_review.review_id, "refund-reversal-card")
    reversed_posting = drafts.confirm(
        reverse_review.review_id, "refund-reversal-confirmation", "confirm-reversal"
    )
    assert (reversed_posting.record_id, reversed_posting.version) == (posted.record_id, 2)
    after_reversal = reader.collect_with_refunds_v3()
    assert after_reversal.refund_by_currency == ()
    assert after_reversal.net_expense_by_currency == (("JPY", "1000"),)
    assert after_reversal.collection.balances[0].balance_amount == "91.00"
    history = reader.refund_history_v1(posted.record_id)
    assert [view.values["status"] for view in history] == ["ACTIVE", "VOID"]
    assert history[0].values["credit"] == history[1].values["credit"]


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

        chosen = VerifiedRefundTargetV1("retained-selection", canonical(selected))
        refund["credit"]["amount"] = "9.50"
        _bind_opening(refund)
        with pytest.raises(LifecycleError, match="exact delivered question"):
            verify_refund_evidence_v1(service, conn, binding, written, refund, None)
