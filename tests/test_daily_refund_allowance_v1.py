"""Original-currency limits for full, partial and corrected refunds."""

from dataclasses import replace
from decimal import Context, Inexact, localcontext

import pytest

from finance_core.lifecycle_v2 import LifecycleError
from finance_core.lifecycle_v2.daily_v1.contracts import BundleFactRefV1
from finance_core.lifecycle_v2.daily_v1.refund_allowance_v1 import (
    ActiveRefundAllocationV1,
    RefundExpenseV1,
    compile_refund_allowance_v1,
    read_refundable_expense_v1,
)
from tests.test_daily_movements_lifecycle_v1 import fields, movement, prepare
from tests.test_lifecycle_v2 import live as live


def test_two_partial_refunds_consume_the_exact_original_expense_allowance():
    original = BundleFactRefV1("original", 1, "1" * 64, 10)
    first_fact = BundleFactRefV1("refund-first", 1, "2" * 64, 20)
    second_fact = BundleFactRefV1("refund-second", 1, "3" * 64, 30)
    expense = RefundExpenseV1(original, "expense-event", "JPY", "1000")
    first = ActiveRefundAllocationV1(first_fact, original, "expense-event", "JPY", "400")
    second = ActiveRefundAllocationV1(second_fact, original, "expense-event", "JPY", "600")

    before = compile_refund_allowance_v1(expense, (first,))
    assert (before.refunded_original, before.remaining_original) == ("400", "600")
    complete = compile_refund_allowance_v1(expense, (second, first))
    assert (complete.refunded_original, complete.remaining_original) == ("1000", "0")
    assert complete == compile_refund_allowance_v1(expense, (first, second))
    assert complete.allowance_sha256 != before.allowance_sha256
    with pytest.raises(LifecycleError, match="original currency allowance"):
        compile_refund_allowance_v1(expense, (first, replace(second, original_amount="601")))
    with pytest.raises(LifecycleError, match="one current version per refund"):
        compile_refund_allowance_v1(expense, (first, first))


def test_retained_relationship_tracks_original_observation_and_current_expense_limit():
    observed = BundleFactRefV1("original", 1, "4" * 64, 10)
    current = BundleFactRefV1("original", 2, "5" * 64, 30)
    refund = ActiveRefundAllocationV1(
        BundleFactRefV1("refund", 1, "6" * 64, 20), observed, "expense-event", "MYR", "10.37"
    )
    expense = RefundExpenseV1(current, "expense-event", "MYR", "20.00")
    with localcontext(Context(prec=2, traps=[Inexact])):
        allowance = compile_refund_allowance_v1(expense, (refund,))
    assert allowance.active_refunds[0].observed_expense == observed
    assert allowance.expense.current_fact == current
    assert (allowance.refunded_original, allowance.remaining_original) == ("10.37", "9.63")
    for wrong in (
        replace(refund, original_currency="SGD"),
        replace(refund, expense_economic_event_id="unrelated"),
    ):
        with pytest.raises(LifecycleError, match="current expense relationship"):
            compile_refund_allowance_v1(expense, (wrong,))
    with pytest.raises(LifecycleError, match="original currency allowance"):
        compile_refund_allowance_v1(replace(expense, original_amount="10.36"), (refund,))
    with pytest.raises(LifecycleError, match="refund amount required"):
        replace(refund, original_amount="10.3")


def test_only_the_exact_current_original_consumption_is_refundable(live):
    ledger, port, _, movements, reader, account = prepare(live, opening=None, currency="SGD")
    values = fields(account, amount="9", original="1000", currency="JPY")
    values["coverage"] = None
    _, _, receipt = movement(ledger, port, movements, account, "outstanding-purchase", values)
    original = reader.detail(receipt.receipt.record_id)
    before = ledger.reader().full_audit()
    target = read_refundable_expense_v1(ledger, original.fact_ref)
    assert (target.original_currency, target.original_amount) == ("JPY", "1000")
    assert target.economic_event_id == original.economic_event_id
    assert ledger.reader().full_audit() == before
    movement(
        ledger, port, movements, account, "void-purchase", {}, target=original, kind="movement_void"
    )
    with pytest.raises(LifecycleError, match="no longer the selected expense version"):
        read_refundable_expense_v1(ledger, original.fact_ref)
    closed = reader.detail(receipt.receipt.record_id)
    with pytest.raises(LifecycleError, match="current active expense"):
        read_refundable_expense_v1(ledger, closed.fact_ref)


def test_verified_legacy_personal_expense_provides_only_its_original_amount(live):
    ledger, _, _ = live
    source = ledger.capture("source1", "legacy-capture")
    draft = ledger.propose(source.source_id, "legacy-observation", 0, "legacy-draft")
    review = ledger.prepare_review(draft.proposal_id, draft.version)
    ledger.record_delivery(review.review_id, "legacy-delivery")
    receipt = ledger.confirm(review.review_id, "legacy-action", "legacy-confirm")
    ref = BundleFactRefV1(
        receipt.record_id, receipt.version, receipt.fact_sha256, receipt.commit_seq
    )
    target = read_refundable_expense_v1(ledger, ref)
    assert (target.original_currency, target.original_amount) == ("SGD", "12.00")
    assert compile_refund_allowance_v1(target, ()).remaining_original == "12.00"
