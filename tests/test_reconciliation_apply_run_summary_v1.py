"""Tests for Reconciliation Run Summary v1.

Covers:
  1. Run summary counts actions correctly
  2. Run summary distinguishes proposal-only vs audit-only actions
  3. Run summary handles empty results
  4. Format run summary produces non-empty output
  5. Unresolved queue items detected
  6. Reviewer counts
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from finance_core.reconciliation.apply import ResolutionApplyRuntime
from finance_core.reconciliation.apply_persistence import ApplyPersistence
from finance_core.reconciliation.matching import match_batch
from finance_core.reconciliation.models import (
    AppTransaction,
    IssueType,
    MatchStatus,
    ReconciliationCandidate,
    ResolutionAction,
    ResolutionDecision,
    ReviewQueueItem,
    StatementTransaction,
    SuggestedAction,
)
from finance_core.reconciliation.review_queue import generate_review_queue
from finance_core.reconciliation.run_summary import (
    RunSummary,
    format_run_summary,
    summarize_apply_results,
)


def _stmt(**kw) -> StatementTransaction:
    defaults = dict(
        transaction_date=date(2024, 12, 1),
        posted_date=None,
        merchant_raw="Apple",
        amount=Decimal("29.90"),
        currency="SGD",
        statement_row_reference="rs-stmt-001",
    )
    defaults.update(kw)
    return StatementTransaction(**defaults)


def _app(app_txn_id: str, **kw) -> AppTransaction:
    defaults = dict(
        app_txn_id=app_txn_id,
        transaction_date=date(2024, 12, 1),
        merchant="Apple",
        amount=Decimal("29.90"),
        currency="SGD",
    )
    defaults.update(kw)
    return AppTransaction(**defaults)


# ============================================================================
# 1. Run summary counts actions correctly
# ============================================================================


def test_summary_counts_actions(migrated_temp_db_connection):
    """Persist multiple results with different actions and verify counts."""
    conn = migrated_temp_db_connection

    stmt_a = _stmt(merchant_raw="Apple", amount=Decimal("29.90"), statement_row_reference="s1")
    stmt_b = _stmt(merchant_raw="Netflix", amount=Decimal("19.90"), statement_row_reference="s2")
    app_a = _app("app-s-a", merchant="Apple", amount=Decimal("29.90"))
    app_b = _app("app-s-b", merchant="Netflix", amount=Decimal("19.90"))
    candidates = match_batch([stmt_a, stmt_b], [app_a, app_b])
    items, _ = generate_review_queue(candidates)

    runtime = ResolutionApplyRuntime()
    ap = ApplyPersistence(conn)

    actions_applied = {}
    for it in items:
        if it.issue_type == IssueType.MATCHED:
            action = ResolutionAction.CONFIRM_MATCH
        else:
            action = ResolutionAction.IGNORE

        decision = ResolutionDecision(
            decision_id=f"dec-s-{it.queue_item_id}",
            queue_item_id=it.queue_item_id,
            action=action,
        )
        result = runtime.apply(it, decision)
        if result.success:
            ap.save_apply_result(result)
            actions_applied[action.value] = actions_applied.get(action.value, 0) + 1

    summary = summarize_apply_results(conn)
    assert summary.total_apply_results > 0
    for action, count in actions_applied.items():
        assert summary.count_by_action.get(action, 0) == count


# ============================================================================
# 2. Run summary distinguishes proposal-only vs audit-only actions
# ============================================================================


def test_summary_distinguishes_proposal_vs_audit(migrated_temp_db_connection):
    """Persist both proposal (create_missing_app) and audit (mark_duplicate)
    results and verify classification."""
    conn = migrated_temp_db_connection
    runtime = ResolutionApplyRuntime()
    ap = ApplyPersistence(conn)

    # -- Audit-only: mark_duplicate (manually constructed POSSIBLE_DUPLICATE) --
    stmt_d = _stmt(merchant_raw="Grab", amount=Decimal("8.50"), statement_row_reference="aud-s1")
    app_da = _app("app-aud-a", merchant="Grab", amount=Decimal("8.50"))
    app_db = _app("app-aud-b", merchant="Grab", amount=Decimal("8.50"))
    cand_d = ReconciliationCandidate(
        statement=stmt_d,
        best_app_transaction=app_da,
        all_app_transactions=(app_da, app_db),
        match_status=MatchStatus.POSSIBLE_DUPLICATE,
        issue_type=IssueType.POSSIBLE_DUPLICATE,
        confidence_score=Decimal("0.85"),
        candidate_id="cand-aud-dup",
    )
    dup_item = ReviewQueueItem(
        queue_item_id="q-audit-dup",
        candidate=cand_d,
        issue_type=IssueType.POSSIBLE_DUPLICATE,
        suggested_action=SuggestedAction.MARK_DUPLICATE,
        priority=2,
    )

    # -- Proposal: create_missing_app (manually constructed MISSING_IN_APP) --
    stmt_m = _stmt(merchant_raw="Giant", amount=Decimal("45.50"), statement_row_reference="prp-s1")
    cand_m = ReconciliationCandidate(
        statement=stmt_m,
        best_app_transaction=None,
        all_app_transactions=(),
        match_status=MatchStatus.NO_MATCH,
        issue_type=IssueType.MISSING_IN_APP,
        confidence_score=Decimal("0.0"),
        candidate_id="cand-prp-miss",
    )
    missing_item = ReviewQueueItem(
        queue_item_id="q-proposal-miss",
        candidate=cand_m,
        issue_type=IssueType.MISSING_IN_APP,
        suggested_action=SuggestedAction.CREATE_MISSING_APP_TRANSACTION,
        priority=3,
    )

    # Persist audit-only: mark_duplicate
    dec_d = ResolutionDecision(
        decision_id="dec-aud-dup-001",
        queue_item_id=dup_item.queue_item_id,
        action=ResolutionAction.MARK_DUPLICATE,
    )
    result_d = runtime.apply(dup_item, dec_d)
    assert result_d.success
    ap.save_apply_result(result_d)

    # Persist proposal: create_missing_app_transaction
    dec_m = ResolutionDecision(
        decision_id="dec-prp-miss-001",
        queue_item_id=missing_item.queue_item_id,
        action=ResolutionAction.CREATE_MISSING_APP_TRANSACTION,
    )
    result_m = runtime.apply(missing_item, dec_m)
    assert result_m.success
    ap.save_apply_result(result_m)

    summary = summarize_apply_results(conn)
    assert summary.total_apply_results > 0
    assert summary.count_audit_only_actions >= 1
    assert summary.count_proposal_actions >= 1


# ============================================================================
# 3. Run summary handles empty results
# ============================================================================


def test_summary_empty_results(migrated_temp_db_connection):
    """Summarize an empty apply results table."""
    summary = summarize_apply_results(migrated_temp_db_connection)
    assert summary.total_apply_results == 0
    assert summary.count_by_action == {}
    assert summary.count_proposal_actions == 0
    assert summary.count_audit_only_actions == 0


# ============================================================================
# 4. Format run summary produces non-empty output
# ============================================================================


def test_format_run_summary_non_empty():
    """format_run_summary returns a non-empty string."""
    summary = RunSummary(
        total_apply_results=5,
        count_by_action={"confirm_match": 3, "ignore": 2},
        count_by_success={"success": 5, "failed": 0},
        count_proposal_actions=0,
        count_audit_only_actions=5,
        earliest_applied_at="2024-12-01T00:00:00",
        latest_applied_at="2024-12-01T00:00:05",
    )
    output = format_run_summary(summary)
    assert "Reconciliation Apply Run Summary" in output
    assert "confirm_match" in output
    assert "5" in output


# ============================================================================
# 5. Unresolved queue items detected
# ============================================================================


def test_summary_unresolved_queue_items(migrated_temp_db_connection):
    """When known queue items are supplied, unresolved ones are reported."""
    conn = migrated_temp_db_connection
    item, decision = _make_matched_item_and_decision()
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(item, decision)

    ap = ApplyPersistence(conn)
    ap.save_apply_result(result)

    # Pass an extra queue item ID that was NOT resolved
    known = [item.queue_item_id, "unresolved-extra"]
    summary = summarize_apply_results(conn, known_queue_item_ids=known)
    assert "unresolved-extra" in summary.unresolved_queue_items


# ============================================================================
# 6. Reviewer counts
# ============================================================================


def test_summary_reviewer_counts(migrated_temp_db_connection):
    """Multiple reviewers should be counted separately."""
    conn = migrated_temp_db_connection
    item, decision = _make_matched_item_and_decision()
    # Modify reviewer
    custom_decision = ResolutionDecision(
        decision_id=decision.decision_id,
        queue_item_id=decision.queue_item_id,
        action=decision.action,
        reviewer="owner",
    )
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(item, custom_decision)

    ap = ApplyPersistence(conn)
    ap.save_apply_result(result)

    summary = summarize_apply_results(conn)
    assert summary.count_by_reviewer.get("owner", 0) == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_matched_item_and_decision():
    stmt = _stmt()
    app = _app("app-rs-match")
    candidates = match_batch([stmt], [app])
    items, _ = generate_review_queue(candidates)
    item = items[0]
    decision = ResolutionDecision(
        decision_id="dec-rs-001",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.CONFIRM_MATCH,
        note="Test summary.",
    )
    return item, decision
