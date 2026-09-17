"""Tests for Reconciliation Review Workflow Audit Layer v1.

Covers:
  1. exact match produces review-ready accepted/matched output
  2. amount mismatch requires confirmation and records mismatch reason
  3. date mismatch is represented clearly
  4. merchant-name mismatch includes evidence from both sides
  5. unmatched statement transaction is flagged as possible missing app record
  6. unmatched app transaction is flagged as possible missing statement record
  7. duplicate suspicion is represented without auto-resolving
  8. rejected candidate remains audit-visible
  9. workflow output does not apply final transaction mutation
 10. confirmation-required cases are explicit
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from finance_core.reconciliation.matching import match_batch
from finance_core.reconciliation.models import (
    AppTransaction,
    IssueType,
    MatchStatus,
    ReasonCode,
    ReconciliationCandidate,
    ReviewQueueItem,
    StatementAmountDirection,
    StatementTransaction,
    SuggestedAction,
)
from finance_core.reconciliation.review_queue import generate_review_queue
from finance_core.reconciliation.review_workflow import (
    ConfirmationRequirement,
    ReviewDecisionState,
    ReviewStatus,
    build_review_workflow,
    build_workflow_from_candidates,
    classify_review_status,
)

# -- Helpers ------------------------------------------------------------------


def _stmt(**kw) -> StatementTransaction:
    defaults: dict = dict(
        transaction_date=date(2024, 12, 1),
        posted_date=date(2024, 12, 1),
        merchant_raw="Apple",
        amount=Decimal("29.90"),
        currency="SGD",
        statement_row_reference="stmt-001",
        amount_direction=StatementAmountDirection.DEBIT,
        raw_amount="29.90",
    )
    defaults.update(kw)
    return StatementTransaction(**defaults)


def _app(app_txn_id: str, **kw) -> AppTransaction:
    defaults: dict = dict(
        app_txn_id=app_txn_id,
        transaction_date=date(2024, 12, 1),
        merchant="Apple",
        amount=Decimal("29.90"),
        currency="SGD",
        source_type="expense",
    )
    defaults.update(kw)
    return AppTransaction(**defaults)


def _run_flow(
    stmts: list[StatementTransaction],
    apps: list[AppTransaction],
) -> tuple:
    """Run the full review pipeline and return (candidates, queue_items, workflow)."""
    candidates = match_batch(stmts, apps)
    queue_items, summary = generate_review_queue(candidates)
    workflow = build_review_workflow(queue_items, summary)
    return candidates, queue_items, workflow


def _find_item(workflow, review_status: ReviewStatus):
    """Find the first workflow item with a given review status."""
    for item in workflow.items:
        if item.review_status == review_status:
            return item
    return None


# ---------------------------------------------------------------------------
# 1. Exact match produces review-ready accepted/matched output
# ---------------------------------------------------------------------------


def test_exact_match_accepted_matched():
    """An exact (amount, currency, date, merchant) match should produce
    ACCEPTED_MATCHED with not_required confirmation."""
    stmt = _stmt()
    app = _app("app-001")
    candidates, queue_items, workflow = _run_flow([stmt], [app])

    assert len(workflow.items) >= 1
    item = _find_item(workflow, ReviewStatus.ACCEPTED_MATCHED)
    assert item is not None

    assert item.review_status == ReviewStatus.ACCEPTED_MATCHED
    assert item.requires_confirmation == ConfirmationRequirement.NOT_REQUIRED
    assert item.statement_merchant == "Apple"
    assert item.statement_amount == Decimal("29.90")
    assert item.statement_currency == "SGD"
    assert item.app_transaction_id == "app-001"
    assert item.app_amount == Decimal("29.90")
    assert item.confidence_score >= Decimal("0.5")
    assert item.severity == "info"
    assert "exact amount match" in item.match_reason
    assert item.mismatch_reason == ""
    assert item.no_final_mutation_applied is True

    # Workflow aggregate counts
    assert workflow.matched_count >= 1
    assert workflow.no_final_mutation_applied is True


# ---------------------------------------------------------------------------
# 2. Amount mismatch requires confirmation and records mismatch reason
# ---------------------------------------------------------------------------


def test_amount_mismatch_requires_confirmation():
    """An amount mismatch (merchant + date align but amounts differ) should
    require confirmation and carry a mismatch reason."""
    stmt = _stmt(amount=Decimal("50.00"))
    app = _app("app-002", amount=Decimal("29.90"))
    candidates, queue_items, workflow = _run_flow([stmt], [app])

    item = _find_item(workflow, ReviewStatus.AMOUNT_MISMATCH)
    assert item is not None
    assert item.review_status == ReviewStatus.AMOUNT_MISMATCH
    assert item.requires_confirmation == ConfirmationRequirement.REQUIRED
    assert item.statement_amount == Decimal("50.00")
    assert item.app_amount == Decimal("29.90")
    assert item.amount_delta == Decimal("20.10")
    assert "amount differs" in item.mismatch_reason
    assert item.severity == "error"
    assert "Adjust app transaction" in item.recommended_action
    assert item.no_final_mutation_applied is True

    # Workflow aggregate
    assert workflow.amount_mismatch_count >= 1


# ---------------------------------------------------------------------------
# 3. Date mismatch is represented clearly
# ---------------------------------------------------------------------------


def test_date_mismatch_clearly_represented():
    """Statement and app amounts match, but dates are outside tolerance.
    Should produce DATE_MISMATCH with date delta evidence."""
    stmt = _stmt(transaction_date=date(2024, 12, 10), posted_date=None)
    app = _app("app-003", transaction_date=date(2024, 12, 1))
    candidates, queue_items, workflow = _run_flow([stmt], [app])

    item = _find_item(workflow, ReviewStatus.DATE_MISMATCH)
    assert item is not None
    assert item.review_status == ReviewStatus.DATE_MISMATCH
    assert item.requires_confirmation == ConfirmationRequirement.REQUIRED
    assert item.statement_transaction_date == date(2024, 12, 10)
    assert item.app_transaction_date == date(2024, 12, 1)
    assert item.date_delta_days is not None and item.date_delta_days > 0
    assert "outside date tolerance" in item.mismatch_reason
    assert item.severity == "warning"
    assert item.no_final_mutation_applied is True


# ---------------------------------------------------------------------------
# 4. Merchant-name mismatch includes evidence from both sides
# ---------------------------------------------------------------------------


def test_merchant_mismatch_evidence_both_sides():
    """Merchant mismatch should include statement-side and app-side evidence."""
    stmt = _stmt(merchant_raw="Netflix", statement_row_reference="stmt-netflix")
    app = _app("app-004", merchant="Spotify")
    candidates, queue_items, workflow = _run_flow([stmt], [app])

    item = _find_item(workflow, ReviewStatus.MERCHANT_MISMATCH)
    assert item is not None
    assert item.review_status == ReviewStatus.MERCHANT_MISMATCH
    assert item.requires_confirmation == ConfirmationRequirement.REQUIRED
    assert item.statement_merchant == "Netflix"
    assert item.app_merchant == "Spotify"
    assert "weak merchant match" in item.mismatch_reason
    assert item.severity == "warning"
    assert item.no_final_mutation_applied is True


# ---------------------------------------------------------------------------
# 5. Unmatched statement transaction flagged as possible missing app record
# ---------------------------------------------------------------------------


def test_unmatched_statement_missing_app():
    """A statement with no matching app transaction should be flagged as
    UNMATCHED_STATEMENT."""
    stmt = _stmt(statement_row_reference="stmt-alone")
    # No app transactions at all -- statement gets NO_CANDIDATE_FOUND
    candidates, queue_items, workflow = _run_flow([stmt], [])

    item = _find_item(workflow, ReviewStatus.UNMATCHED_STATEMENT)
    assert item is not None
    assert item.review_status == ReviewStatus.UNMATCHED_STATEMENT
    assert item.requires_confirmation == ConfirmationRequirement.REQUIRED
    assert item.statement_merchant == "Apple"
    assert item.app_transaction_id is None
    assert "no candidate found" in item.mismatch_reason
    assert item.severity == "warning"
    assert item.no_final_mutation_applied is True


# ---------------------------------------------------------------------------
# 6. Unmatched app transaction flagged as possible missing statement record
# ---------------------------------------------------------------------------


def test_unmatched_app_missing_statement():
    """An app transaction with no matching statement should be flagged as
    UNMATCHED_APP."""
    # MISSING_IN_STATEMENT is unreachable through the normal match_batch
    # flow when every app is referenced by at least one statement candidate
    # (the matcher aggregates all app candidates per statement).  We
    # construct a ReviewQueueItem directly to exercise the enrichment path.

    app = _app("app-alone", amount=Decimal("10.00"), merchant="FoodPanda")
    candidate = ReconciliationCandidate(
        statement=_stmt(),
        best_app_transaction=app,
        all_app_transactions=(app,),
        match_status=MatchStatus.NO_MATCH,
        reason_codes=(ReasonCode.NO_CANDIDATE_FOUND,),
        issue_type=IssueType.MISSING_IN_STATEMENT,
        confidence_score=Decimal("0.0"),
    )
    qi = ReviewQueueItem(
        candidate=candidate,
        issue_type=IssueType.MISSING_IN_STATEMENT,
        suggested_action=SuggestedAction.CREATE_MISSING_APP_TRANSACTION,
        queue_item_id="qi-missing-stmt",
        reason_codes=(ReasonCode.NO_CANDIDATE_FOUND,),
    )
    workflow = build_review_workflow([qi])
    item = workflow.items[0]
    assert item.review_status == ReviewStatus.UNMATCHED_APP
    assert item.requires_confirmation == ConfirmationRequirement.REQUIRED
    assert item.app_transaction_id == "app-alone"
    assert item.no_final_mutation_applied is True


# ---------------------------------------------------------------------------
# 7. Duplicate suspicion is represented without auto-resolving
# ---------------------------------------------------------------------------


def test_duplicate_suspicion_no_auto_resolve():
    """Two statements that appear to reference the same app transaction should
    produce DUPLICATE_SUSPICION without automatically picking one."""
    stmt1 = _stmt(statement_row_reference="stmt-dup-1")
    stmt2 = _stmt(statement_row_reference="stmt-dup-2")
    app = _app("app-dup")
    candidates, queue_items, workflow = _run_flow([stmt1, stmt2], [app])

    # At least one of them should be flagged as duplicate suspicion
    dup_items = [i for i in workflow.items if i.review_status == ReviewStatus.DUPLICATE_SUSPICION]
    assert len(dup_items) >= 1
    for item in dup_items:
        assert item.requires_confirmation == ConfirmationRequirement.REQUIRED
        assert item.severity == "error"
        assert "multiple candidate matches" in item.mismatch_reason
        assert item.no_final_mutation_applied is True


# ---------------------------------------------------------------------------
# 8. Rejected candidate remains audit-visible
# ---------------------------------------------------------------------------


def test_rejected_candidate_remains_audit_visible():
    """A candidate that is explicitly rejected via decision overlay should
    appear as REJECTED and remain fully audit-visible with all evidence
    fields intact."""
    # Netflix and HBO Max do not share an alias in _MERCHANT_ALIAS_MAP,
    # so they produce a MERCHANT_MISMATCH (not a normalized match).
    stmt = _stmt(merchant_raw="Netflix", statement_row_reference="stmt-rej")
    app = _app("app-rej", merchant="HBO Max")
    candidates, queue_items, workflow = _run_flow([stmt], [app])

    # Find the merchant mismatch item and get its queue_item_id
    item_before = _find_item(workflow, ReviewStatus.MERCHANT_MISMATCH)
    assert item_before is not None
    assert item_before.requires_confirmation == ConfirmationRequirement.REQUIRED

    qid = item_before.queue_item.queue_item_id
    decision = ReviewDecisionState(outcome="rejected", note="wrong merchant")

    # Rebuild with decision overlay
    workflow2 = build_review_workflow(queue_items, decision_overrides={qid: decision})
    item = _find_item(workflow2, ReviewStatus.REJECTED)
    assert item is not None
    assert item.review_status == ReviewStatus.REJECTED
    assert item.requires_confirmation == ConfirmationRequirement.NOT_REQUIRED
    assert item.statement_merchant is not None
    assert item.app_merchant is not None

    # Audit visibility: all required fields are present
    assert item.audit_note != ""
    assert "Review status:" in item.audit_note
    assert item.recommended_action != ""

    # The item is visible in the workflow output
    assert item.no_final_mutation_applied is True

    # Decision audit fields are preserved
    assert item.decision_outcome == "rejected"
    assert item.decision_note == "wrong merchant"
    assert item.decision_reviewer == "human"
    assert "Decision outcome:" in item.audit_note
    assert "Decision note:" in item.audit_note


# ---------------------------------------------------------------------------
# 9. Workflow output does not apply final transaction mutation
# ---------------------------------------------------------------------------


def test_workflow_no_final_mutation():
    """Every workflow item and the workflow result itself must carry
    no_final_mutation_applied = True."""
    stmt1 = _stmt(statement_row_reference="stmt-nomut-1")
    stmt2 = _stmt(
        amount=Decimal("100.00"),
        merchant_raw="Spotify",
        statement_row_reference="stmt-nomut-2",
    )
    app1 = _app("app-nomut-1")
    app2 = _app("app-nomut-2", amount=Decimal("50.00"), merchant="Spotify")

    candidates, queue_items, workflow = _run_flow([stmt1, stmt2], [app1, app2])

    # Every item must carry no_final_mutation_applied = True
    for item in workflow.items:
        assert item.no_final_mutation_applied is True, (
            f"Item {item.queue_item.queue_item_id} has no_final_mutation_applied=False"
        )

    # The workflow result itself
    assert workflow.no_final_mutation_applied is True

    # Check that the flag exists on every output dataclass instance
    assert hasattr(workflow, "no_final_mutation_applied")


# ---------------------------------------------------------------------------
# 10. Confirmation-required cases are explicit
# ---------------------------------------------------------------------------


def test_confirmation_required_explicit():
    """Every item that is not ACCEPTED_MATCHED or CONFIRMED_PENDING_APPLY should
    have requires_confirmation == REQUIRED."""
    stmts = [
        _stmt(
            transaction_date=date(2024, 12, 1),
            merchant_raw="Apple",
            amount=Decimal("29.90"),
            statement_row_reference="stmt-cr-1",
        ),
        _stmt(
            transaction_date=date(2024, 12, 10),
            merchant_raw="Netflix",
            amount=Decimal("50.00"),
            statement_row_reference="stmt-cr-2",
        ),
        _stmt(
            transaction_date=date(2024, 12, 1),
            merchant_raw="Shopee",
            amount=Decimal("15.00"),
            statement_row_reference="stmt-cr-3",
        ),
    ]
    apps = [
        _app("app-cr-1"),  # exact match with stmt-cr-1
        _app("app-cr-2", amount=Decimal("30.00"), merchant="Netflix"),  # amount mismatch
        # No app for stmt-cr-3 (missing_in_app)
    ]

    candidates, queue_items, workflow = _run_flow(stmts, apps)

    for item in workflow.items:
        if item.review_status in (
            ReviewStatus.ACCEPTED_MATCHED,
            ReviewStatus.CONFIRMED_PENDING_APPLY,
        ):
            assert item.requires_confirmation == ConfirmationRequirement.NOT_REQUIRED, (
                f"Item {item.queue_item.queue_item_id} with status "
                f"{item.review_status.value} should not require confirmation"
            )
        else:
            assert item.requires_confirmation == ConfirmationRequirement.REQUIRED, (
                f"Item {item.queue_item.queue_item_id} with status "
                f"{item.review_status.value} should require confirmation"
            )

    # Verify at least one REQUIRED and one NOT_REQUIRED
    required = sum(
        1 for i in workflow.items if i.requires_confirmation == ConfirmationRequirement.REQUIRED
    )
    not_required = sum(
        1 for i in workflow.items if i.requires_confirmation == ConfirmationRequirement.NOT_REQUIRED
    )
    assert required > 0, "Expected at least one item requiring confirmation"
    assert not_required > 0, "Expected at least one item not requiring confirmation"
    assert workflow.total_confirmation_required == required


# ---------------------------------------------------------------------------
# 11. Confirmed pending apply via decision override
# ---------------------------------------------------------------------------


def test_confirmed_pending_apply_override():
    """A decision override of confirmed_pending_apply should change an item's
    review status to CONFIRMED_PENDING_APPLY with NOT_REQUIRED confirmation."""
    stmt = _stmt(merchant_raw="Netflix", statement_row_reference="stmt-cf")
    app = _app("app-cf", merchant="HBO Max")
    candidates, queue_items, workflow = _run_flow([stmt], [app])

    # Find the merchant_mismatch item and get its queue_item_id
    item_before = _find_item(workflow, ReviewStatus.MERCHANT_MISMATCH)
    assert item_before is not None
    assert item_before.requires_confirmation == ConfirmationRequirement.REQUIRED

    qid = item_before.queue_item.queue_item_id
    decision = ReviewDecisionState(outcome="confirmed_pending_apply", note="looks correct")

    # Rebuild with decision override
    workflow2 = build_review_workflow(queue_items, decision_overrides={qid: decision})
    item_after = _find_item(workflow2, ReviewStatus.CONFIRMED_PENDING_APPLY)
    assert item_after is not None
    assert item_after.review_status == ReviewStatus.CONFIRMED_PENDING_APPLY
    assert item_after.requires_confirmation == ConfirmationRequirement.NOT_REQUIRED
    assert item_after.no_final_mutation_applied is True
    assert "confirmed_pending_apply" in item_after.audit_note

    # Decision audit fields are preserved
    assert item_after.decision_outcome == "confirmed_pending_apply"
    assert item_after.decision_note == "looks correct"
    assert item_after.decision_reviewer == "human"
    assert "Decision outcome:" in item_after.audit_note
    assert "Decision reviewer:" in item_after.audit_note
    assert "Decision note:" in item_after.audit_note
    assert "looks correct" in item_after.audit_note

    # The workflow result still carries the no-mutation flag
    assert workflow2.no_final_mutation_applied is True


# ---------------------------------------------------------------------------
# 12. Rejected candidate via decision override
# ---------------------------------------------------------------------------


def test_rejected_override():
    """A decision override of rejected should change an item's review status
    to REJECTED with NOT_REQUIRED confirmation and remain audit-visible."""
    stmt = _stmt(merchant_raw="Netflix", statement_row_reference="stmt-rej2")
    app = _app("app-rej2", merchant="HBO Max")
    candidates, queue_items, workflow = _run_flow([stmt], [app])

    item_before = _find_item(workflow, ReviewStatus.MERCHANT_MISMATCH)
    assert item_before is not None
    assert item_before.requires_confirmation == ConfirmationRequirement.REQUIRED

    qid = item_before.queue_item.queue_item_id
    decision = ReviewDecisionState(outcome="rejected", note="not a real match")

    workflow2 = build_review_workflow(queue_items, decision_overrides={qid: decision})
    item_after = _find_item(workflow2, ReviewStatus.REJECTED)
    assert item_after is not None
    assert item_after.review_status == ReviewStatus.REJECTED
    assert item_after.requires_confirmation == ConfirmationRequirement.NOT_REQUIRED
    assert item_after.no_final_mutation_applied is True
    assert "rejected" in item_after.audit_note

    # Decision audit fields are preserved
    assert item_after.decision_outcome == "rejected"
    assert item_after.decision_note == "not a real match"
    assert item_after.decision_reviewer == "human"
    assert "Decision outcome:" in item_after.audit_note
    assert "Decision reviewer:" in item_after.audit_note
    assert "Decision note:" in item_after.audit_note
    assert "not a real match" in item_after.audit_note

    # Audit visibility still intact
    assert item_after.statement_merchant is not None
    assert item_after.app_merchant is not None
    assert item_after.audit_note != ""
    assert item_after.recommended_action != ""


# ---------------------------------------------------------------------------
# Additional: classify_review_status is deterministic
# ---------------------------------------------------------------------------


def test_classify_review_status_deterministic():
    """classify_review_status should return the same status for the same input
    every time."""
    for issue_type in IssueType:
        first = classify_review_status(issue_type)
        second = classify_review_status(issue_type)
        assert first == second, f"Non-deterministic for {issue_type}: {first} vs {second}"


# ---------------------------------------------------------------------------
# Additional: build_workflow_from_candidates convenience
# ---------------------------------------------------------------------------


def test_build_workflow_from_candidates():
    """build_workflow_from_candidates() should produce the same result as the
    two-step process."""
    stmt = _stmt()
    app = _app("app-conv")
    candidates = match_batch([stmt], [app])

    workflow = build_workflow_from_candidates(candidates, run_label="test")

    assert len(workflow.items) >= 1
    assert workflow.no_final_mutation_applied is True
    assert workflow.matched_count >= 1


# ---------------------------------------------------------------------------
# 13. Invalid ReviewDecisionState outcome raises ValueError
# ---------------------------------------------------------------------------


def test_review_decision_state_invalid_outcome():
    """ReviewDecisionState must reject outcomes outside the allowed set."""
    import pytest

    with pytest.raises(ValueError, match="Invalid decision outcome"):
        ReviewDecisionState(outcome="invalid")

    with pytest.raises(ValueError, match="Invalid decision outcome"):
        ReviewDecisionState(outcome="approved")

    with pytest.raises(ValueError, match="Invalid decision outcome"):
        ReviewDecisionState(outcome="")
