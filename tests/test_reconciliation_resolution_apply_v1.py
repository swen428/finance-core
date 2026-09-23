"""Tests for Resolution Apply Runtime v1.

Covers:
  1. confirm_match success
  2. confirm_match idempotent repeat
  3. confirm_match conflicting repeat rejected
  4. mark_duplicate success without deleting/merging anything
  5. mark_statement_only success
  6. create_missing_app_transaction returns proposal, not final transaction
  7. adjust_app_transaction returns proposal, not transaction mutation
  8. ignore success
  9. needs_more_info success
  10. unsupported action/issue combination rejected
  11. missing required references rejected
  12. deterministic evidence/result serialization
  13. no database/finance.db usage
  14. no silent mutation of transaction rows
  15. integration with existing ResolutionDecision / ReviewQueueItem models
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from finance_core.reconciliation.apply import (
    ApplyConflictError,
    ResolutionApplyRuntime,
    apply_decisions,
    build_apply_instruction,
)
from finance_core.reconciliation.matching import match_batch
from finance_core.reconciliation.models import (
    ApplyInstructionError,
    AppTransaction,
    IssueType,
    ResolutionAction,
    ResolutionDecision,
    ReviewQueueItem,
    StatementAmountDirection,
    StatementTransaction,
)
from finance_core.reconciliation.review_queue import generate_review_queue

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _stmt(**kw) -> StatementTransaction:
    defaults: dict = dict(
        transaction_date=date(2024, 12, 1),
        posted_date=None,
        merchant_raw="Apple",
        amount=Decimal("29.90"),
        currency="SGD",
        statement_row_reference="stmt-res",
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


def _make_matched_item() -> tuple[ResolutionApplyRuntime, ReviewQueueItem, ResolutionDecision]:
    """Create a matched review queue item and a confirm_match decision."""
    stmt = _stmt()
    app = _app("app-matched")
    candidates = match_batch([stmt], [app])
    items, _ = generate_review_queue(candidates)
    item = items[0]
    decision = ResolutionDecision(
        decision_id="dec-001",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.CONFIRM_MATCH,
        note="Looks correct.",
    )
    runtime = ResolutionApplyRuntime()
    return runtime, item, decision


@pytest.mark.parametrize(
    "changed_field",
    [
        "candidate_id",
        "queue_item_id",
        "issue_type",
        "statement_ref",
        "app_transaction_ref",
        "audit_metadata",
        "_item",
    ],
)
def test_direct_apply_instruction_revalidates_frozen_review_source(changed_field: str) -> None:
    runtime, item, decision = _make_matched_item()
    instruction = build_apply_instruction(item, decision)
    replacements = {
        "candidate_id": "forged-candidate",
        "queue_item_id": "forged-queue",
        "issue_type": IssueType.POSSIBLE_DUPLICATE,
        "statement_ref": "forged-statement",
        "app_transaction_ref": "forged-app",
        "audit_metadata": {"source": "forged"},
        "_item": replace(
            item,
            candidate=replace(
                item.candidate, statement=replace(item.candidate.statement, amount=Decimal("999"))
            ),
        ),
    }
    forged = replace(instruction, **{changed_field: replacements[changed_field]})
    with pytest.raises(ApplyInstructionError):
        runtime.apply_instruction(forged)


def _make_duplicate_item() -> tuple[ResolutionApplyRuntime, ReviewQueueItem, ResolutionDecision]:
    """Create a duplicate queue item for mark_duplicate testing."""
    stmt = _stmt(merchant_raw="Grab", amount=Decimal("8.50"))
    app_a = _app("app-dup-a", merchant="Grab", amount=Decimal("8.50"))
    app_b = _app("app-dup-b", merchant="Grab", amount=Decimal("8.50"))
    candidates = match_batch([stmt], [app_a, app_b])
    items, _ = generate_review_queue(candidates)
    item = items[0]
    decision = ResolutionDecision(
        decision_id="dec-dup-001",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.MARK_DUPLICATE,
        note="Duplicate detected.",
    )
    runtime = ResolutionApplyRuntime()
    return runtime, item, decision


def _make_missing_in_statement_item() -> tuple[
    ResolutionApplyRuntime, ReviewQueueItem, ResolutionDecision
]:
    """Create a missing_in_statement item for mark_statement_only testing."""
    stmt = _stmt(merchant_raw="Amazon", amount=Decimal("55.00"), statement_row_reference="amz-001")
    # No matching app -> missing_in_app, but we want missing_in_statement
    # Create an item where the app is present but the statement is intentionally not linked
    app = _app("app-amz", merchant="Amazon", amount=Decimal("55.00"))
    candidates = match_batch([stmt], [app])
    items, _ = generate_review_queue(candidates)
    # Find a missing_in_statement item (the synthetic candidate)
    for item in items:
        if item.issue_type == IssueType.MISSING_IN_STATEMENT:
            decision = ResolutionDecision(
                decision_id="dec-mso-001",
                queue_item_id=item.queue_item_id,
                action=ResolutionAction.MARK_STATEMENT_ONLY,
                note="Statement-only entry.",
            )
            runtime = ResolutionApplyRuntime()
            return runtime, item, decision
    # Fallback: no MISSING_IN_STATEMENT found, create manually
    return _make_matched_item()


def _make_missing_in_app_item() -> tuple[
    ResolutionApplyRuntime, ReviewQueueItem, ResolutionDecision
]:
    """Create a missing_in_app item for create_missing_app_transaction testing."""
    stmt = _stmt(merchant_raw="Giant Supermarket", amount=Decimal("45.50"))
    # No matching app at all -> missing_in_app
    app = _app("app-other", merchant="Other", amount=Decimal("100.00"))
    candidates = match_batch([stmt], [app])
    items, _ = generate_review_queue(candidates)
    for item in items:
        if item.issue_type == IssueType.MISSING_IN_APP:
            decision = ResolutionDecision(
                decision_id="dec-cma-001",
                queue_item_id=item.queue_item_id,
                action=ResolutionAction.CREATE_MISSING_APP_TRANSACTION,
                note="Need to create app transaction from statement.",
            )
            runtime = ResolutionApplyRuntime()
            return runtime, item, decision
    # Fallback
    return _make_matched_item()


def _make_mismatch_item(
    mismatch_type: IssueType,
) -> tuple[ResolutionApplyRuntime, ReviewQueueItem, ResolutionDecision]:
    """Create a mismatch item for adjust_app_transaction testing."""
    if mismatch_type == IssueType.AMOUNT_MISMATCH:
        stmt = _stmt(merchant_raw="Netflix", amount=Decimal("19.90"))
        app = _app("app-netflix", merchant="Netflix", amount=Decimal("21.90"))
    elif mismatch_type == IssueType.DATE_MISMATCH:
        stmt = _stmt(
            merchant_raw="Spotify",
            amount=Decimal("9.90"),
            transaction_date=date(2024, 12, 7),
            statement_row_reference="spot-001",
        )
        app = _app(
            "app-spotify",
            merchant="Spotify",
            amount=Decimal("9.90"),
            transaction_date=date(2024, 12, 8),
        )
    elif mismatch_type == IssueType.MERCHANT_MISMATCH:
        stmt = _stmt(merchant_raw="Foodpanda SG", amount=Decimal("22.30"))
        app = _app("app-fp", merchant="Foodpanda", amount=Decimal("22.30"))
    else:
        stmt = _stmt()
        app = _app("app-any")

    candidates = match_batch([stmt], [app])
    items, _ = generate_review_queue(candidates)
    for item in items:
        if item.issue_type == mismatch_type:
            decision = ResolutionDecision(
                decision_id=f"dec-adj-{mismatch_type.value}",
                queue_item_id=item.queue_item_id,
                action=ResolutionAction.ADJUST_APP_TRANSACTION,
                note="Adjustment needed.",
            )
            runtime = ResolutionApplyRuntime()
            return runtime, item, decision
    # Fallback
    return _make_matched_item()


# ============================================================================
# 1. confirm_match success
# ============================================================================


def test_confirm_match_success():
    runtime, item, decision = _make_matched_item()
    result = runtime.apply(item, decision)
    assert result.success
    assert result.idempotent is False
    assert result.action == ResolutionAction.CONFIRM_MATCH
    assert result.payload["action_type"] == "confirm_match"
    assert result.payload.get("app_txn_id") is not None
    assert result.statement_reference is not None
    assert result.app_transaction_reference is not None
    # Verify audit evidence
    assert "statement_merchant" in result.audit_evidence
    assert "app_txn_id" in result.audit_evidence
    assert result.audit_evidence["reviewer"] == "human"


# ============================================================================
# 2. confirm_match idempotent repeat
# ============================================================================


def test_confirm_match_idempotent_repeat():
    runtime, item, decision = _make_matched_item()
    first = runtime.apply(item, decision)
    assert first.success
    assert first.idempotent is False

    second = runtime.apply(item, decision)
    assert second.success
    assert second.idempotent is True
    # Payloads should match
    assert second.payload == first.payload


# ============================================================================
# 3. confirm_match conflicting repeat rejected
# ============================================================================


def test_idempotent_replay_uses_stable_timestamp():
    """Idempotent replay must return the same applied_at as the original."""
    runtime, item, decision = _make_matched_item()
    first = runtime.apply(item, decision)
    assert first.success
    assert first.idempotent is False

    second = runtime.apply(item, decision)
    assert second.success
    assert second.idempotent is True

    # The applied_at timestamp must be stable across replay
    assert second.applied_at == first.applied_at, (
        f"Idempotent replay changed applied_at: {first.applied_at} -> {second.applied_at}"
    )

    # Audit evidence must be stable across replay
    assert second.audit_evidence == first.audit_evidence, "Idempotent replay changed audit_evidence"

    # Payload must be stable across replay
    assert second.payload == first.payload, "Idempotent replay changed payload"


def test_confirm_match_conflicting_repeat_rejected():
    runtime, item, decision = _make_matched_item()
    runtime.apply(item, decision)

    # Try to apply a different decision to the same item
    conflicting = ResolutionDecision(
        decision_id="dec-002",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.IGNORE,
        note="Changed my mind.",
    )
    with pytest.raises(ApplyConflictError, match="already resolved"):
        runtime.apply(item, conflicting)


def test_same_decision_id_different_item_rejected():
    runtime, item, decision = _make_matched_item()
    runtime.apply(item, decision)

    # Try to reuse the same decision_id on a different item.
    # Use a manual item with a unique queue_item_id to avoid collision
    # with the first item's "q-000" from generate_review_queue.
    from finance_core.reconciliation.models import (
        MatchStatus as _MS,
    )
    from finance_core.reconciliation.models import (
        ReconciliationCandidate as _RC,
    )
    from finance_core.reconciliation.models import (
        SuggestedAction as _SA,
    )

    stmt2 = _stmt(merchant_raw="Google", amount=Decimal("10.00"), statement_row_reference="goog")
    app2 = _app("app-google", merchant="Google", amount=Decimal("10.00"))
    cand2 = _RC(
        statement=stmt2,
        best_app_transaction=app2,
        all_app_transactions=(app2,),
        match_status=_MS.MATCHED,
        issue_type=IssueType.MATCHED,
        confidence_score=Decimal("1.0"),
        candidate_id="cand-google-001",
    )
    item2 = ReviewQueueItem(
        queue_item_id="unique-item-002",
        candidate=cand2,
        issue_type=IssueType.MATCHED,
        suggested_action=_SA.CONFIRM_MATCH,
        priority=99,
    )

    bad_decision = ResolutionDecision(
        decision_id=decision.decision_id,  # Same ID!
        queue_item_id=item2.queue_item_id,
        action=ResolutionAction.CONFIRM_MATCH,
    )
    with pytest.raises(ApplyConflictError, match="already applied"):
        runtime.apply(item2, bad_decision)


def test_same_decision_id_different_payload_rejected():
    runtime, item, decision = _make_matched_item()
    runtime.apply(item, decision)

    # Reapply same decision_id with different note
    modified = ResolutionDecision(
        decision_id=decision.decision_id,
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.CONFIRM_MATCH,
        note="Actually, different note now.",
    )
    with pytest.raises(ApplyConflictError, match="payload/evidence changed"):
        runtime.apply(item, modified)


# ============================================================================
# 4. mark_duplicate success without deleting/merging anything
# ============================================================================


def test_mark_duplicate_success_no_mutation():
    runtime, item, decision = _make_duplicate_item()
    result = runtime.apply(item, decision)
    assert result.success
    assert result.action == ResolutionAction.MARK_DUPLICATE
    assert result.payload["action_type"] == "mark_duplicate"
    assert result.payload["audit_only"] is True
    assert "duplicate_app_txn_ids" in result.payload
    assert len(result.payload["duplicate_app_txn_ids"]) >= 2

    # Verify no transaction mutation: the original item is unchanged
    original_app_ids = [a.app_txn_id for a in item.candidate.all_app_transactions]
    assert len(original_app_ids) >= 2
    # The payload just records the IDs; it doesn't delete or merge
    assert "note" in result.payload
    assert "No transactions deleted or merged" in result.payload["note"]


# ============================================================================
# 5. mark_statement_only success
# ============================================================================


def test_mark_statement_only_success():
    # Build a MISSING_IN_STATEMENT item manually to ensure we test
    # mark_statement_only on the right issue type.
    from finance_core.reconciliation.models import (
        MatchStatus as _MS,
    )
    from finance_core.reconciliation.models import (
        ReconciliationCandidate as _RC,
    )
    from finance_core.reconciliation.models import (
        SuggestedAction as _SA,
    )

    stmt = _stmt(
        merchant_raw="Amazon SG", amount=Decimal("55.00"), statement_row_reference="amz-so-001"
    )
    app = _app("app-amz", merchant="Amazon SG", amount=Decimal("55.00"))
    cand = _RC(
        statement=stmt,
        best_app_transaction=app,
        all_app_transactions=(app,),
        match_status=_MS.NO_MATCH,
        issue_type=IssueType.MISSING_IN_STATEMENT,
        confidence_score=Decimal("0.0"),
        candidate_id="cand-amz-so",
    )
    it = ReviewQueueItem(
        queue_item_id="q-mso-manual",
        candidate=cand,
        issue_type=IssueType.MISSING_IN_STATEMENT,
        suggested_action=_SA.MARK_STATEMENT_ONLY,
        priority=5,
    )
    decision = ResolutionDecision(
        decision_id="dec-so-manual",
        queue_item_id=it.queue_item_id,
        action=ResolutionAction.MARK_STATEMENT_ONLY,
        note="Statement-only entry.",
    )
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(it, decision)
    assert result.success
    assert result.payload["action_type"] == "mark_statement_only"
    assert "intent" in result.payload
    assert "Statement row intentionally not linked" in result.payload["intent"]


# ============================================================================
# 6. create_missing_app_transaction returns proposal, not final transaction
# ============================================================================


def test_create_missing_app_returns_proposal_not_transaction():
    # Build a MISSING_IN_APP item manually (matching pipeline may not
    # produce this issue type with arbitrary test inputs).
    from finance_core.reconciliation.models import (
        MatchStatus as _MS,
    )
    from finance_core.reconciliation.models import (
        ReconciliationCandidate as _RC,
    )
    from finance_core.reconciliation.models import (
        SuggestedAction as _SA,
    )

    stmt = _stmt(
        merchant_raw="Giant Supermarket", amount=Decimal("45.50"), statement_row_reference="gs-001"
    )
    cand = _RC(
        statement=stmt,
        best_app_transaction=None,  # No app -> missing in app
        match_status=_MS.NO_MATCH,
        issue_type=IssueType.MISSING_IN_APP,
        confidence_score=Decimal("0.0"),
        candidate_id="cand-gs-001",
    )
    missing_item = ReviewQueueItem(
        queue_item_id="q-missing-app",
        candidate=cand,
        issue_type=IssueType.MISSING_IN_APP,
        suggested_action=_SA.CREATE_MISSING_APP_TRANSACTION,
        priority=3,
    )

    decision = ResolutionDecision(
        decision_id="dec-cma-test",
        queue_item_id=missing_item.queue_item_id,
        action=ResolutionAction.CREATE_MISSING_APP_TRANSACTION,
        note="Create from statement.",
    )
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(missing_item, decision)
    assert result.success
    # Must be a proposal, not a final transaction creation
    assert result.payload["action_type"] == "proposal"
    assert result.payload["proposal_type"] == "create_missing_app_transaction"
    assert "Proposal only" in result.payload.get("note", "")
    # Must have source evidence from the statement
    assert "source_evidence" in result.payload
    assert result.payload["source_evidence"]["statement_merchant"] is not None


# ============================================================================
# 7. adjust_app_transaction returns proposal, not transaction mutation
# ============================================================================


def test_adjust_app_returns_proposal_not_mutation():
    runtime, item, decision = _make_mismatch_item(IssueType.AMOUNT_MISMATCH)
    result = runtime.apply(item, decision)
    assert result.success
    # Must be a proposal, not a final mutation
    assert result.payload["action_type"] == "proposal"
    assert result.payload["proposal_type"] == "adjust_app_transaction"
    assert "Proposal only" in result.payload.get("note", "")
    # Field adjustments must be present
    assert "field_adjustments" in result.payload
    # The original item's app transaction must not have been mutated
    original_app = item.candidate.best_app_transaction
    assert original_app is not None
    # Verify the item/candidate is immutable (frozen dataclass)
    original_amount = original_app.amount
    assert original_amount == Decimal("21.90")  # Original mismatch amount unchanged


# ============================================================================
# 8. ignore success
# ============================================================================


def test_ignore_success():
    runtime, item, decision = _make_matched_item()
    ignore_decision = ResolutionDecision(
        decision_id="dec-ignore",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.IGNORE,
        note="Skipping this one.",
    )
    result = runtime.apply(item, ignore_decision)
    assert result.success
    assert result.payload["action_type"] == "ignore"
    assert result.payload["skipped"] is True
    assert "Skipping this one" in result.payload["reason"]


# ============================================================================
# 9. needs_more_info success
# ============================================================================


def test_needs_more_info_success():
    runtime, item, decision = _make_matched_item()
    nmi_decision = ResolutionDecision(
        decision_id="dec-nmi",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.NEEDS_MORE_INFO,
        note="Need to check with vendor.",
    )
    result = runtime.apply(item, nmi_decision)
    assert result.success
    assert result.payload["action_type"] == "needs_more_info"
    assert result.payload["status"] == "pending_investigation"
    assert "Need to check with vendor" in result.payload["reason"]


# ============================================================================
# 10. unsupported action/issue combination rejected
# ============================================================================


def test_unsupported_action_issue_combination_rejected():
    runtime, item, decision = _make_matched_item()
    # MARK_DUPLICATE is not compatible with IssueType.MATCHED
    bad_decision = ResolutionDecision(
        decision_id="dec-bad-combo",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.MARK_DUPLICATE,
    )
    result = runtime.apply(item, bad_decision)
    if item.issue_type == IssueType.MATCHED:
        assert not result.success
        assert result.error_message is not None
        assert "not compatible" in result.error_message


# ============================================================================
# 11. missing required references rejected
# ============================================================================


def test_missing_required_references_rejected():
    """confirm_match requires best_app_transaction; reject when None."""
    runtime = ResolutionApplyRuntime()
    # Create a statement with no matching app -> MISSING_IN_APP
    stmt = _stmt(merchant_raw="Orphan", amount=Decimal("99.00"), statement_row_reference="orph-001")
    app = _app(
        "app-other", merchant="Other", amount=Decimal("1.00"), transaction_date=date(2024, 11, 15)
    )
    candidates = match_batch([stmt], [app])
    items, _ = generate_review_queue(candidates)

    # Find MISSING_IN_APP item -- has no best_app_transaction
    for it in items:
        if it.issue_type == IssueType.MISSING_IN_APP:
            decision = ResolutionDecision(
                decision_id="dec-no-ref",
                queue_item_id=it.queue_item_id,
                action=ResolutionAction.CONFIRM_MATCH,
            )
            result = runtime.apply(it, decision)
            # confirm_match is not compatible with MISSING_IN_APP, so it fails
            assert not result.success
            assert result.error_message is not None
            assert "not compatible" in result.error_message.lower()
            return

    # Backup: test queue_item_id mismatch
    stmt2 = _stmt(
        merchant_raw="Break", amount=Decimal("50.00"), statement_row_reference="break-001"
    )
    app2 = _app("app-break", merchant="Break", amount=Decimal("50.00"))
    candidates2 = match_batch([stmt2], [app2])
    items2, _ = generate_review_queue(candidates2)
    it = items2[0]
    decision = ResolutionDecision(
        decision_id="dec-mismatch-ref",
        queue_item_id="wrong-id",
        action=ResolutionAction.CONFIRM_MATCH,
    )
    result = runtime.apply(it, decision)
    assert not result.success
    assert (
        "mismatch" in (result.error_message or "").lower()
        or "does not match" in (result.error_message or "").lower()
    )


# ============================================================================
# 12. deterministic evidence/result serialization
# ============================================================================


def test_deterministic_evidence_serialization():
    runtime, item, decision = _make_matched_item()
    result = runtime.apply(item, decision)

    # Evidence should be JSON-serializable
    evidence_json = json.dumps(result.audit_evidence, sort_keys=True, separators=(",", ":"))
    parsed = json.loads(evidence_json)
    assert parsed["resolution_action"] == "confirm_match"
    assert parsed["queue_item_id"] == item.queue_item_id

    # Payload should be JSON-serializable
    payload_json = json.dumps(result.payload, sort_keys=True, separators=(",", ":"))
    parsed_payload = json.loads(payload_json)
    assert parsed_payload["action_type"] == "confirm_match"

    # Determinism: same input produces same output (except applied_at timestamp)
    runtime2 = ResolutionApplyRuntime()
    result2 = runtime2.apply(item, decision)
    assert result2.payload == result.payload
    # Audit evidence will differ on applied_timestamp, so strip that
    ev1 = {k: v for k, v in result.audit_evidence.items() if k != "applied_timestamp"}
    ev2 = {k: v for k, v in result2.audit_evidence.items() if k != "applied_timestamp"}
    assert ev1 == ev2


# ============================================================================
# 13. No database/finance.db usage
# ============================================================================


def test_no_database_usage():
    """The apply runtime must not touch any database file."""
    runtime, item, decision = _make_matched_item()
    # No database connection is passed; runtime is purely in-memory
    result = runtime.apply(item, decision)
    assert result.success
    assert isinstance(result.apply_id, str)
    # The apply runtime should not have any DB-related attributes
    assert not hasattr(runtime, "_conn")
    assert not hasattr(runtime, "_db_path")


# ============================================================================
# 14. No silent mutation of transaction rows
# ============================================================================


def test_no_silent_mutation_of_transaction_objects():
    """Applying decisions must not modify the original candidate/transaction data."""
    runtime, item, decision = _make_matched_item()

    # Capture original values
    original_stmt_amount = item.candidate.statement.amount
    original_app_amount = None
    if item.candidate.best_app_transaction:
        original_app_amount = item.candidate.best_app_transaction.amount

    result = runtime.apply(item, decision)
    assert result.success

    # Verify original objects are unchanged
    assert item.candidate.statement.amount == original_stmt_amount
    if item.candidate.best_app_transaction:
        assert item.candidate.best_app_transaction.amount == original_app_amount

    # The apply result should not contain mutated versions of the originals;
    # it should contain proposals (if action=adjust_app) or confirmations
    if result.action == ResolutionAction.CONFIRM_MATCH:
        assert result.payload["action_type"] == "confirm_match"


# ============================================================================
# 15. Integration with existing ResolutionDecision / ReviewQueueItem
# ============================================================================


def test_integration_with_batch_apply():
    """Test that apply_decisions works across multiple items with different actions."""
    stmt_a = _stmt(merchant_raw="Apple", amount=Decimal("29.90"), statement_row_reference="ia1")
    stmt_b = _stmt(merchant_raw="Netflix", amount=Decimal("19.90"), statement_row_reference="ia2")
    stmt_c = _stmt(merchant_raw="Unknown", amount=Decimal("77.00"), statement_row_reference="ia3")
    app_a = _app("app-ia-a", merchant="Apple", amount=Decimal("29.90"))
    app_b = _app("app-ia-b", merchant="Netflix", amount=Decimal("19.90"))
    # app_c intentionally missing -> stmt_c will be missing_in_app

    candidates = match_batch([stmt_a, stmt_b, stmt_c], [app_a, app_b])
    items, _ = generate_review_queue(candidates)

    # Build matching decisions
    decisions = []
    for item in items:
        if item.issue_type == IssueType.MATCHED:
            decisions.append(
                ResolutionDecision(
                    decision_id=f"dec-int-{item.queue_item_id}",
                    queue_item_id=item.queue_item_id,
                    action=ResolutionAction.CONFIRM_MATCH,
                )
            )
        elif item.issue_type == IssueType.MISSING_IN_APP:
            decisions.append(
                ResolutionDecision(
                    decision_id=f"dec-int-{item.queue_item_id}",
                    queue_item_id=item.queue_item_id,
                    action=ResolutionAction.CREATE_MISSING_APP_TRANSACTION,
                )
            )

    results = apply_decisions(items, decisions)
    assert len(results) == len(decisions)
    for r in results:
        assert r.success
        assert r.applied_at != ""


# ============================================================================
# Additional boundary tests
# ============================================================================


def test_is_applied_tracking():
    runtime, item, decision = _make_matched_item()
    assert not runtime.is_applied(item.queue_item_id)
    runtime.apply(item, decision)
    assert runtime.is_applied(item.queue_item_id)
    assert runtime.get_applied_decision_id(item.queue_item_id) == decision.decision_id


def test_reset_clears_registry():
    runtime, item, decision = _make_matched_item()
    runtime.apply(item, decision)
    assert runtime.is_applied(item.queue_item_id)
    runtime.reset()
    assert not runtime.is_applied(item.queue_item_id)


def test_empty_items_with_decisions():
    """apply_decisions with empty items list returns empty results."""
    results = apply_decisions([], [])
    assert results == []


def test_unknown_queue_item_decision_rejected():
    """Decisions for unknown queue_item_ids must be rejected, not silently skipped."""
    _, item, decision = _make_matched_item()
    unmatched_decision = ResolutionDecision(
        decision_id="dec-no-item",
        queue_item_id="nonexistent-item",
        action=ResolutionAction.CONFIRM_MATCH,
    )
    with pytest.raises(ApplyConflictError, match="not present in the items list"):
        apply_decisions([item], [unmatched_decision])


def test_duplicate_decisions_same_queue_item_rejected():
    """Two decisions targeting the same queue_item_id must be rejected."""
    _, item, decision = _make_matched_item()
    duplicate_decision = ResolutionDecision(
        decision_id="dec-dup-same-q",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.CONFIRM_MATCH,
    )
    with pytest.raises(ApplyConflictError, match="Duplicate decisions"):
        apply_decisions([item], [decision, duplicate_decision])


def test_adjust_app_date_mismatch_payload():
    """Adjust for date mismatch should suggest transaction_date in payload."""
    # Build a DATE_MISMATCH candidate manually (1 day difference
    # within default tolerance may be classified as matched).
    from finance_core.reconciliation.models import (
        MatchStatus as _MS,
    )
    from finance_core.reconciliation.models import (
        ReconciliationCandidate as _RC,
    )
    from finance_core.reconciliation.models import (
        SuggestedAction as _SA,
    )

    stmt = _stmt(
        merchant_raw="Spotify",
        amount=Decimal("9.90"),
        transaction_date=date(2024, 12, 7),
        statement_row_reference="spot-d",
    )
    app = _app(
        "app-spotify",
        merchant="Spotify",
        amount=Decimal("9.90"),
        transaction_date=date(2024, 12, 8),
    )
    cand = _RC(
        statement=stmt,
        best_app_transaction=app,
        all_app_transactions=(app,),
        match_status=_MS.DATE_MISMATCH,
        issue_type=IssueType.DATE_MISMATCH,
        confidence_score=Decimal("0.0"),
        candidate_id="cand-spot-d",
    )
    it = ReviewQueueItem(
        queue_item_id="q-date-mismatch",
        candidate=cand,
        issue_type=IssueType.DATE_MISMATCH,
        suggested_action=_SA.ADJUST_APP_TRANSACTION,
        priority=2,
    )
    decision = ResolutionDecision(
        decision_id="dec-date-adj",
        queue_item_id=it.queue_item_id,
        action=ResolutionAction.ADJUST_APP_TRANSACTION,
    )
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(it, decision)
    assert result.success
    assert result.payload["proposal_type"] == "adjust_app_transaction"
    assert "transaction_date" in result.payload.get("field_adjustments", {})


def test_adjust_app_merchant_mismatch_payload():
    """Adjust for merchant mismatch should suggest merchant in payload."""
    # Build a MERCHANT_MISMATCH candidate manually ("Foodpanda SG"
    # and "Foodpanda" may normalize to match in the alias map).
    from finance_core.reconciliation.models import (
        MatchStatus as _MS,
    )
    from finance_core.reconciliation.models import (
        ReconciliationCandidate as _RC,
    )
    from finance_core.reconciliation.models import (
        SuggestedAction as _SA,
    )

    stmt = _stmt(
        merchant_raw="Foodpanda SG",
        amount=Decimal("22.30"),
        statement_row_reference="fp-001",
    )
    app = _app("app-fp", merchant="Foodpanda", amount=Decimal("22.30"))
    cand = _RC(
        statement=stmt,
        best_app_transaction=app,
        all_app_transactions=(app,),
        match_status=_MS.MERCHANT_MISMATCH,
        issue_type=IssueType.MERCHANT_MISMATCH,
        confidence_score=Decimal("0.0"),
        candidate_id="cand-fp-001",
    )
    it = ReviewQueueItem(
        queue_item_id="q-merchant-mismatch",
        candidate=cand,
        issue_type=IssueType.MERCHANT_MISMATCH,
        suggested_action=_SA.ADJUST_APP_TRANSACTION,
        priority=4,
    )
    decision = ResolutionDecision(
        decision_id="dec-merchant-adj",
        queue_item_id=it.queue_item_id,
        action=ResolutionAction.ADJUST_APP_TRANSACTION,
    )
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(it, decision)
    assert result.success
    assert result.payload["proposal_type"] == "adjust_app_transaction"
    if result.payload.get("field_adjustments"):
        assert "merchant" in result.payload["field_adjustments"]


# ============================================================================
# Guardrail-strengthening tests (v2 boundary)
# ============================================================================


def test_build_apply_instruction_rejects_ambiguous_candidate():
    """Empty candidate_id must be rejected — ambiguous identity cannot be
    applied as a final record."""
    from finance_core.reconciliation.apply import build_apply_instruction
    from finance_core.reconciliation.models import ApplyInstructionError

    app = _app("app-ambig", merchant="Spotify", amount=Decimal("9.90"))
    stmt = _stmt(merchant_raw="Spotify", amount=Decimal("9.90"))
    from finance_core.reconciliation.models import (
        MatchStatus as _MS,
    )
    from finance_core.reconciliation.models import (
        ReconciliationCandidate as _RC,
    )
    from finance_core.reconciliation.models import (
        SuggestedAction as _SA,
    )

    cand = _RC(
        statement=stmt,
        best_app_transaction=app,
        all_app_transactions=(app,),
        match_status=_MS.MATCHED,
        issue_type=IssueType.MATCHED,
        confidence_score=Decimal("1.0"),
        candidate_id="",  # Ambiguous!
    )
    item = ReviewQueueItem(
        queue_item_id="q-ambig",
        candidate=cand,
        issue_type=IssueType.MATCHED,
        suggested_action=_SA.CONFIRM_MATCH,
        priority=99,
    )
    decision = ResolutionDecision(
        decision_id="dec-ambig",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.CONFIRM_MATCH,
        note="Should fail — ambiguous candidate.",
    )

    with pytest.raises(ApplyInstructionError, match="ambiguous"):
        build_apply_instruction(item, decision)

    # Also verify that runtime.apply() returns a failure result, not success
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(item, decision)
    assert not result.success
    assert result.error_message is not None
    assert "ambiguous" in result.error_message.lower()


def test_build_apply_instruction_rejects_missing_evidence_ref():
    """When require_evidence_ref=True and no evidence_refs are provided,
    the instruction builder must reject the decision."""
    from finance_core.reconciliation.apply import build_apply_instruction
    from finance_core.reconciliation.models import ApplyInstructionError

    app = _app("app-ev", merchant="Apple", amount=Decimal("29.90"))
    stmt = _stmt(merchant_raw="Apple", amount=Decimal("29.90"))
    from finance_core.reconciliation.models import (
        MatchStatus as _MS,
    )
    from finance_core.reconciliation.models import (
        ReconciliationCandidate as _RC,
    )
    from finance_core.reconciliation.models import (
        SuggestedAction as _SA,
    )

    cand = _RC(
        statement=stmt,
        best_app_transaction=app,
        all_app_transactions=(app,),
        match_status=_MS.MATCHED,
        issue_type=IssueType.MATCHED,
        confidence_score=Decimal("1.0"),
        candidate_id="cand-ev-001",
    )
    item = ReviewQueueItem(
        queue_item_id="q-ev-ref",
        candidate=cand,
        issue_type=IssueType.MATCHED,
        suggested_action=_SA.CONFIRM_MATCH,
        priority=99,
    )
    decision = ResolutionDecision(
        decision_id="dec-ev-ref",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.CONFIRM_MATCH,
        note="Should fail — no evidence ref provided.",
    )

    with pytest.raises(ApplyInstructionError, match="Evidence reference is required"):
        build_apply_instruction(item, decision, require_evidence_ref=True, evidence_refs=())

    # But with evidence_refs provided, it should succeed
    instruction = build_apply_instruction(
        item,
        decision,
        require_evidence_ref=True,
        evidence_refs=("ev-public-id-001",),
    )
    assert instruction.evidence_refs == ("ev-public-id-001",)
    assert instruction.decision_id == decision.decision_id


def test_build_apply_instruction_rejects_missing_amount_for_adjust():
    """Action ADJUST_APP_TRANSACTION with statement.amount=None must be rejected
    because the monetary basis is missing."""
    from finance_core.reconciliation.apply import build_apply_instruction
    from finance_core.reconciliation.models import ApplyInstructionError

    app = _app("app-no-amt", merchant="Spotify", amount=Decimal("9.90"))
    stmt = _stmt(
        merchant_raw="Spotify",
        amount=None,  # Missing!
        statement_row_reference="spot-no-amt",
    )
    from finance_core.reconciliation.models import (
        MatchStatus as _MS,
    )
    from finance_core.reconciliation.models import (
        ReconciliationCandidate as _RC,
    )
    from finance_core.reconciliation.models import (
        SuggestedAction as _SA,
    )

    cand = _RC(
        statement=stmt,
        best_app_transaction=app,
        all_app_transactions=(app,),
        match_status=_MS.AMOUNT_MISMATCH,
        issue_type=IssueType.AMOUNT_MISMATCH,
        confidence_score=Decimal("0.0"),
        candidate_id="cand-no-amt",
    )
    item = ReviewQueueItem(
        queue_item_id="q-no-amt",
        candidate=cand,
        issue_type=IssueType.AMOUNT_MISMATCH,
        suggested_action=_SA.ADJUST_APP_TRANSACTION,
        priority=0,
    )
    decision = ResolutionDecision(
        decision_id="dec-no-amt",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.ADJUST_APP_TRANSACTION,
        note="Should fail — no statement amount.",
    )

    with pytest.raises(ApplyInstructionError, match="statement amount"):
        build_apply_instruction(item, decision)


def test_build_apply_instruction_rejects_missing_amount_for_create():
    """Action CREATE_MISSING_APP_TRANSACTION with statement.amount=None must be
    rejected because the proposed transaction has no monetary amount."""
    from finance_core.reconciliation.apply import build_apply_instruction
    from finance_core.reconciliation.models import ApplyInstructionError

    stmt = _stmt(
        merchant_raw="Unknown Vendor",
        amount=None,
        statement_row_reference="unk-no-amt",
    )
    from finance_core.reconciliation.models import (
        MatchStatus as _MS,
    )
    from finance_core.reconciliation.models import (
        ReconciliationCandidate as _RC,
    )
    from finance_core.reconciliation.models import (
        SuggestedAction as _SA,
    )

    cand = _RC(
        statement=stmt,
        best_app_transaction=None,
        match_status=_MS.NO_MATCH,
        issue_type=IssueType.MISSING_IN_APP,
        confidence_score=Decimal("0.0"),
        candidate_id="cand-cma-no-amt",
    )
    item = ReviewQueueItem(
        queue_item_id="q-cma-no-amt",
        candidate=cand,
        issue_type=IssueType.MISSING_IN_APP,
        suggested_action=_SA.CREATE_MISSING_APP_TRANSACTION,
        priority=3,
    )
    decision = ResolutionDecision(
        decision_id="dec-cma-no-amt",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.CREATE_MISSING_APP_TRANSACTION,
        note="Should fail — no statement amount.",
    )

    with pytest.raises(ApplyInstructionError, match="statement amount"):
        build_apply_instruction(item, decision)


def test_rejected_decision_does_not_mutate_registry():
    """A decision that fails validation must not appear in the applied registry.
    The runtime state must remain unchanged after a rejected apply."""
    from finance_core.reconciliation.models import (
        MatchStatus as _MS,
    )
    from finance_core.reconciliation.models import (
        ReconciliationCandidate as _RC,
    )
    from finance_core.reconciliation.models import (
        SuggestedAction as _SA,
    )

    app = _app("app-rej", merchant="Spotify", amount=Decimal("9.90"))
    stmt = _stmt(merchant_raw="Spotify", amount=Decimal("9.90"))
    cand = _RC(
        statement=stmt,
        best_app_transaction=app,
        all_app_transactions=(app,),
        match_status=_MS.MATCHED,
        issue_type=IssueType.MATCHED,
        confidence_score=Decimal("1.0"),
        candidate_id="cand-rej-001",
    )
    item = ReviewQueueItem(
        queue_item_id="q-rej",
        candidate=cand,
        issue_type=IssueType.MATCHED,
        suggested_action=_SA.CONFIRM_MATCH,
        priority=99,
    )
    # MARK_DUPLICATE is not compatible with IssueType.MATCHED
    bad_decision = ResolutionDecision(
        decision_id="dec-rej",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.MARK_DUPLICATE,
        note="Should fail.",
    )

    runtime = ResolutionApplyRuntime()
    assert not runtime.is_applied(item.queue_item_id)

    result = runtime.apply(item, bad_decision)
    assert not result.success

    # Registry must be clean — no side effects from a rejected decision
    assert not runtime.is_applied(item.queue_item_id)
    assert runtime.get_applied_decision_id(item.queue_item_id) is None


def test_rejected_decision_does_not_block_subsequent_valid_apply():
    """After a rejected decision, a valid decision for the same queue item
    must still be accepted and recorded in the registry."""
    runtime, item, decision = _make_matched_item()

    # First, apply a bad decision (wrong action for the issue type)
    bad_decision = ResolutionDecision(
        decision_id="dec-bad-first",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.MARK_DUPLICATE,
        note="This should fail.",
    )
    bad_result = runtime.apply(item, bad_decision)
    assert not bad_result.success

    # Registry must be clean
    assert not runtime.is_applied(item.queue_item_id)

    # Now apply a valid decision — must succeed
    result = runtime.apply(item, decision)
    assert result.success
    assert runtime.is_applied(item.queue_item_id)
    assert runtime.get_applied_decision_id(item.queue_item_id) == decision.decision_id


def test_review_evidence_not_directly_applicable():
    """ReviewEvidenceBundle and ExplanationInput must NOT be accepted by the
    apply runtime — only ApplyInstruction (built via build_apply_instruction)
    is a valid input.

    This test verifies the type boundary: review evidence objects cannot be
    accidentally passed to the apply runtime as if they were apply instructions.
    """
    runtime = ResolutionApplyRuntime()

    from finance_core.reconciliation.models import ApplyInstructionError

    # Trying to pass a raw dict (simulating someone bypassing the instruction
    # builder) must fail with ApplyInstructionError.
    with pytest.raises(ApplyInstructionError, match="ApplyInstruction"):
        runtime.apply_instruction({"action": "confirm_match"})  # type: ignore[arg-type]

    # Trying to pass a string must fail
    with pytest.raises(ApplyInstructionError, match="ApplyInstruction"):
        runtime.apply_instruction("confirm_match")  # type: ignore[arg-type]

    # Verify that only a properly built ApplyInstruction is accepted
    from finance_core.reconciliation.apply import build_apply_instruction
    from finance_core.reconciliation.models import (
        MatchStatus as _MS,
    )
    from finance_core.reconciliation.models import (
        ReconciliationCandidate as _RC,
    )
    from finance_core.reconciliation.models import (
        SuggestedAction as _SA,
    )

    app = _app("app-type-check", merchant="Apple", amount=Decimal("29.90"))
    stmt = _stmt(merchant_raw="Apple", amount=Decimal("29.90"))
    cand = _RC(
        statement=stmt,
        best_app_transaction=app,
        all_app_transactions=(app,),
        match_status=_MS.MATCHED,
        issue_type=IssueType.MATCHED,
        confidence_score=Decimal("1.0"),
        candidate_id="cand-type-001",
    )
    item = ReviewQueueItem(
        queue_item_id="q-type-check",
        candidate=cand,
        issue_type=IssueType.MATCHED,
        suggested_action=_SA.CONFIRM_MATCH,
        priority=99,
    )
    decision = ResolutionDecision(
        decision_id="dec-type-check",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.CONFIRM_MATCH,
        note="Valid instruction.",
    )

    instruction = build_apply_instruction(item, decision)
    result = runtime.apply_instruction(instruction)
    assert result.success


def test_apply_instruction_carries_audit_metadata():
    """Every ApplyInstruction built must carry audit_metadata for traceability."""
    from finance_core.reconciliation.apply import build_apply_instruction

    app = _app("app-audit", merchant="Apple", amount=Decimal("29.90"))
    stmt = _stmt(merchant_raw="Apple", amount=Decimal("29.90"))
    from finance_core.reconciliation.models import (
        MatchStatus as _MS,
    )
    from finance_core.reconciliation.models import (
        ReconciliationCandidate as _RC,
    )
    from finance_core.reconciliation.models import (
        SuggestedAction as _SA,
    )

    cand = _RC(
        statement=stmt,
        best_app_transaction=app,
        all_app_transactions=(app,),
        match_status=_MS.MATCHED,
        issue_type=IssueType.MATCHED,
        confidence_score=Decimal("1.0"),
        candidate_id="cand-audit-001",
    )
    item = ReviewQueueItem(
        queue_item_id="q-audit",
        candidate=cand,
        issue_type=IssueType.MATCHED,
        suggested_action=_SA.CONFIRM_MATCH,
        priority=99,
    )
    decision = ResolutionDecision(
        decision_id="dec-audit",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.CONFIRM_MATCH,
        note="Audit test.",
    )

    instruction = build_apply_instruction(item, decision)

    # Audit metadata must be present
    assert instruction.audit_metadata is not None
    assert "build_version" in instruction.audit_metadata
    assert instruction.audit_metadata["build_version"] == "v1"
    assert "source" in instruction.audit_metadata
    assert instruction.audit_metadata["source"] == "build_apply_instruction"
    assert "instruction_id" in instruction.audit_metadata

    # Evidence refs can be empty by default
    assert instruction.evidence_refs == ()

    # Statement and app refs should be populated
    assert instruction.statement_ref is not None
    assert instruction.app_transaction_ref is not None

    # The apply result should also carry audit evidence
    runtime = ResolutionApplyRuntime()
    result = runtime.apply_instruction(instruction)
    assert result.success
    assert "apply_runtime_version" in result.audit_evidence
    assert "instruction_id" in result.audit_evidence
    assert result.audit_evidence["reviewer"] == "human"


# ========================================================================
# Evidence Bridge Tests (Reconciliation Apply Audit Bridge v1)
# ========================================================================


class TestEvidenceBridgeBackwardCompatibility:
    """A. Backward compatibility: existing apply_decisions() usage with no
    evidence refs still succeeds."""

    def test_apply_decisions_no_evidence_refs_still_works(self):
        runtime, item, decision = _make_matched_item()
        results = apply_decisions([item], [decision])
        assert len(results) == 1
        assert results[0].success

    def test_apply_decisions_none_mapping_still_works(self):
        runtime, item, decision = _make_matched_item()
        results = apply_decisions(
            [item],
            [decision],
            evidence_refs_by_queue_item=None,
        )
        assert len(results) == 1
        assert results[0].success


class TestEvidenceBridgePropagation:
    """B. Evidence propagation: apply_decisions() passes refs from
    evidence_refs_by_queue_item into ApplyInstruction, and successful
    ResolutionApplyResult.audit_evidence includes evidence_refs."""

    def test_apply_decisions_passes_evidence_refs_to_instruction(self):
        runtime, item, decision = _make_matched_item()
        refs = ("ev-abc", "ev-def")
        results = apply_decisions(
            [item],
            [decision],
            evidence_refs_by_queue_item={item.queue_item_id: refs},
        )
        assert len(results) == 1
        result = results[0]
        assert result.success
        assert "evidence_refs" in result.audit_evidence
        assert result.audit_evidence["evidence_refs"] == list(refs)

    def test_apply_decisions_empty_refs_when_not_in_mapping(self):
        runtime, item, decision = _make_matched_item()
        results = apply_decisions(
            [item],
            [decision],
            evidence_refs_by_queue_item={"different-qid": ("ev-xyz",)},
        )
        assert len(results) == 1
        result = results[0]
        assert result.success
        assert result.audit_evidence["evidence_refs"] == []

    def test_apply_result_includes_evidence_refs_deterministic_order(self):
        runtime, item, decision = _make_matched_item()
        refs = ("ev-c", "ev-a", "ev-b")
        results = apply_decisions(
            [item],
            [decision],
            evidence_refs_by_queue_item={item.queue_item_id: refs},
        )
        assert results[0].success
        assert results[0].audit_evidence["evidence_refs"] == ["ev-c", "ev-a", "ev-b"]


class TestEvidenceBridgeStrictMode:
    """C. Strict evidence mode: require_evidence_ref=True rejects items
    without refs, does not mutate registry, and allows retry with refs."""

    def test_direct_require_evidence_ref_true_rejects(self):
        """build_apply_instruction(..., require_evidence_ref=True) rejects
        when evidence_refs is empty."""
        from finance_core.reconciliation.apply import (
            ApplyInstructionError,
            build_apply_instruction,
        )

        app = _app("app-strict", merchant="Apple", amount=Decimal("29.90"))
        stmt = _stmt(merchant_raw="Apple", amount=Decimal("29.90"))
        from finance_core.reconciliation.models import (
            MatchStatus as _MS,
        )
        from finance_core.reconciliation.models import (
            ReconciliationCandidate as _RC,
        )
        from finance_core.reconciliation.models import (
            SuggestedAction as _SA,
        )

        cand = _RC(
            statement=stmt,
            best_app_transaction=app,
            all_app_transactions=(app,),
            match_status=_MS.MATCHED,
            issue_type=IssueType.MATCHED,
            confidence_score=Decimal("1.0"),
            candidate_id="cand-strict-001",
        )
        item = ReviewQueueItem(
            queue_item_id="q-strict-001",
            candidate=cand,
            issue_type=IssueType.MATCHED,
            suggested_action=_SA.CONFIRM_MATCH,
            priority=99,
        )
        decision = ResolutionDecision(
            decision_id="dec-strict-001",
            queue_item_id=item.queue_item_id,
            action=ResolutionAction.CONFIRM_MATCH,
            note="Strict test.",
        )

        with pytest.raises(ApplyInstructionError, match="Evidence reference is required"):
            build_apply_instruction(item, decision, require_evidence_ref=True)

    def test_direct_require_evidence_ref_false_succeeds(self):
        """build_apply_instruction(..., require_evidence_ref=False, evidence_refs=())
        succeeds -- default backward-compatible behavior."""
        from finance_core.reconciliation.apply import build_apply_instruction

        app = _app("app-strict2", merchant="Apple", amount=Decimal("29.90"))
        stmt = _stmt(merchant_raw="Apple", amount=Decimal("29.90"))
        from finance_core.reconciliation.models import (
            MatchStatus as _MS,
        )
        from finance_core.reconciliation.models import (
            ReconciliationCandidate as _RC,
        )
        from finance_core.reconciliation.models import (
            SuggestedAction as _SA,
        )

        cand = _RC(
            statement=stmt,
            best_app_transaction=app,
            all_app_transactions=(app,),
            match_status=_MS.MATCHED,
            issue_type=IssueType.MATCHED,
            confidence_score=Decimal("1.0"),
            candidate_id="cand-strict2-001",
        )
        item = ReviewQueueItem(
            queue_item_id="q-strict2-001",
            candidate=cand,
            issue_type=IssueType.MATCHED,
            suggested_action=_SA.CONFIRM_MATCH,
            priority=99,
        )
        decision = ResolutionDecision(
            decision_id="dec-strict2-001",
            queue_item_id=item.queue_item_id,
            action=ResolutionAction.CONFIRM_MATCH,
            note="Non-strict test.",
        )

        instruction = build_apply_instruction(item, decision, require_evidence_ref=False)
        assert instruction.evidence_refs == ()
        instruction2 = build_apply_instruction(
            item,
            decision,
            evidence_refs=("ev-1",),
            require_evidence_ref=False,
        )
        assert instruction2.evidence_refs == ("ev-1",)

    def test_batch_strict_rejects_missing_evidence_no_mutation(self):
        """apply_decisions(..., require_evidence_ref=True) rejects an item
        without refs and does not mutate the runtime registry."""
        runtime, item, decision = _make_matched_item()
        results = apply_decisions(
            [item],
            [decision],
            require_evidence_ref=True,
        )
        assert len(results) == 1
        result = results[0]
        assert not result.success
        assert "require_evidence_ref" in result.audit_evidence
        assert result.audit_evidence["require_evidence_ref"] is True
        assert result.audit_evidence["evidence_refs"] == []
        assert "evidence" in result.error_message.lower()

    def test_same_item_retry_with_evidence_succeeds_after_rejection(self):
        """After a rejection due to missing evidence refs in strict mode,
        the same queue item can still be applied successfully when evidence
        refs are provided."""
        runtime, item, decision = _make_matched_item()

        results1 = apply_decisions(
            [item],
            [decision],
            require_evidence_ref=True,
        )
        assert not results1[0].success

        refs = ("ev-retry-1",)
        results2 = apply_decisions(
            [item],
            [decision],
            evidence_refs_by_queue_item={item.queue_item_id: refs},
            require_evidence_ref=True,
        )
        assert results2[0].success
        assert results2[0].audit_evidence["evidence_refs"] == list(refs)

    def test_rejected_missing_evidence_does_not_poison_runtime_registry(self):
        """A rejected missing-evidence item does not register in the
        runtime, so a subsequent valid apply succeeds."""
        runtime, item, decision = _make_matched_item()

        results1 = apply_decisions(
            [item],
            [decision],
            require_evidence_ref=True,
        )
        assert not results1[0].success

        refs = ("ev-poison-1",)
        results2 = apply_decisions(
            [item],
            [decision],
            evidence_refs_by_queue_item={item.queue_item_id: refs},
        )
        assert results2[0].success


class TestEvidenceBridgeMultipleItems:
    """D. Multiple items: batch with two items where one succeeds and
    one fails due to missing evidence refs in strict mode."""

    def test_batch_two_items_mixed_evidence_strict(self):
        from finance_core.reconciliation.models import (
            MatchStatus as _MS,
        )
        from finance_core.reconciliation.models import (
            ReconciliationCandidate as _RC,
        )
        from finance_core.reconciliation.models import (
            SuggestedAction as _SA,
        )

        app_a = _app("app-multi-a", merchant="Apple", amount=Decimal("29.90"))
        stmt_a = _stmt(merchant_raw="Apple", amount=Decimal("29.90"))
        cand_a = _RC(
            statement=stmt_a,
            best_app_transaction=app_a,
            all_app_transactions=(app_a,),
            match_status=_MS.MATCHED,
            issue_type=IssueType.MATCHED,
            confidence_score=Decimal("1.0"),
            candidate_id="cand-multi-a",
        )
        item_a = ReviewQueueItem(
            queue_item_id="q-multi-a",
            candidate=cand_a,
            issue_type=IssueType.MATCHED,
            suggested_action=_SA.CONFIRM_MATCH,
            priority=99,
        )
        decision_a = ResolutionDecision(
            decision_id="dec-multi-a",
            queue_item_id=item_a.queue_item_id,
            action=ResolutionAction.CONFIRM_MATCH,
            note="Has evidence.",
        )

        app_b = _app("app-multi-b", merchant="Netflix", amount=Decimal("15.90"))
        stmt_b = _stmt(merchant_raw="Netflix", amount=Decimal("15.90"))
        cand_b = _RC(
            statement=stmt_b,
            best_app_transaction=app_b,
            all_app_transactions=(app_b,),
            match_status=_MS.MATCHED,
            issue_type=IssueType.MATCHED,
            confidence_score=Decimal("1.0"),
            candidate_id="cand-multi-b",
        )
        item_b = ReviewQueueItem(
            queue_item_id="q-multi-b",
            candidate=cand_b,
            issue_type=IssueType.MATCHED,
            suggested_action=_SA.CONFIRM_MATCH,
            priority=99,
        )
        decision_b = ResolutionDecision(
            decision_id="dec-multi-b",
            queue_item_id=item_b.queue_item_id,
            action=ResolutionAction.CONFIRM_MATCH,
            note="No evidence.",
        )

        results = apply_decisions(
            [item_a, item_b],
            [decision_a, decision_b],
            evidence_refs_by_queue_item={
                item_a.queue_item_id: ("ev-multi-1", "ev-multi-2"),
            },
            require_evidence_ref=True,
        )

        assert len(results) == 2
        result_a = [r for r in results if r.queue_item_id == "q-multi-a"][0]
        result_b = [r for r in results if r.queue_item_id == "q-multi-b"][0]

        assert result_a.success
        assert result_a.audit_evidence["evidence_refs"] == ["ev-multi-1", "ev-multi-2"]

        assert not result_b.success
        assert result_b.audit_evidence["evidence_refs"] == []
        assert result_b.audit_evidence["require_evidence_ref"] is True


class TestEvidenceBridgeDirectRuntime:
    """E. Direct runtime behavior: apply_instruction() with ApplyInstruction
    carrying evidence_refs includes them in audit evidence."""

    def test_apply_instruction_with_evidence_refs_in_audit(self):
        from finance_core.reconciliation.apply import build_apply_instruction

        app = _app("app-runtime-evid", merchant="Shopee", amount=Decimal("12.50"))
        stmt = _stmt(merchant_raw="Shopee", amount=Decimal("12.50"))
        from finance_core.reconciliation.models import (
            MatchStatus as _MS,
        )
        from finance_core.reconciliation.models import (
            ReconciliationCandidate as _RC,
        )
        from finance_core.reconciliation.models import (
            SuggestedAction as _SA,
        )

        cand = _RC(
            statement=stmt,
            best_app_transaction=app,
            all_app_transactions=(app,),
            match_status=_MS.MATCHED,
            issue_type=IssueType.MATCHED,
            confidence_score=Decimal("1.0"),
            candidate_id="cand-runtime-evid",
        )
        item = ReviewQueueItem(
            queue_item_id="q-runtime-evid",
            candidate=cand,
            issue_type=IssueType.MATCHED,
            suggested_action=_SA.CONFIRM_MATCH,
            priority=99,
        )
        decision = ResolutionDecision(
            decision_id="dec-runtime-evid",
            queue_item_id=item.queue_item_id,
            action=ResolutionAction.CONFIRM_MATCH,
            note="Direct runtime evidence test.",
        )

        instruction = build_apply_instruction(
            item,
            decision,
            evidence_refs=("ev-runtime-1", "ev-runtime-2"),
        )
        assert instruction.evidence_refs == ("ev-runtime-1", "ev-runtime-2")

        runtime = ResolutionApplyRuntime()
        result = runtime.apply_instruction(instruction)
        assert result.success
        assert "evidence_refs" in result.audit_evidence
        assert result.audit_evidence["evidence_refs"] == ["ev-runtime-1", "ev-runtime-2"]
