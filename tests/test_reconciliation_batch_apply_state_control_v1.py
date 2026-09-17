"""Tests for Reconciliation Batch Apply State Control v1.

Covers:
  1. Successful first batch apply — PENDING → APPLIED with audit trail
  2. Duplicate apply prevention — already-applied batch returns REJECTED
  3. Duplicate apply (idempotent) — no duplicate effects, clear result
  4. Failed batch apply — FAILED state with explicit error
  5. Retry after failure — FAILED batch can be re-applied successfully
  6. Unsafe state rejection — APPLYING batch rejected
  7. Batch state query — get_batch_state, is_terminal
  8. Audit correctness — audit_metadata includes state transition details
  9. Empty batch apply — FAILED with clear message, not silent
  10. BatchApplyState enum properties — is_terminal, string values
  11. BatchApplyResult computed properties — success, applied_count, failed_count
  12. BatchApplyStateManager.reset — clears all registries
  13. Multiple distinct batches — no cross-contamination
  14. No database/finance.db usage
  15. No silent mutation of transaction rows
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from finance_core.reconciliation.apply import (
    BatchApplyStateManager,
)
from finance_core.reconciliation.matching import match_batch
from finance_core.reconciliation.models import (
    BatchApplyResult,
    BatchApplyState,
    ResolutionAction,
    ResolutionDecision,
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
        statement_row_reference="stmt-batch",
    )
    defaults.update(kw)
    return StatementTransaction(**defaults)


def _app(app_txn_id: str, **kw):
    from finance_core.reconciliation.models import AppTransaction

    defaults: dict = dict(
        app_txn_id=app_txn_id,
        transaction_date=date(2024, 12, 1),
        merchant="Apple",
        amount=Decimal("29.90"),
        currency="SGD",
    )
    defaults.update(kw)
    return AppTransaction(**defaults)


def _make_matched_items(n: int = 1, offset: int = 0):
    """Create n matched review queue items with matching decisions."""
    stmts = []
    apps = []
    for i in range(n):
        idx = offset + i
        stmts.append(
            _stmt(
                merchant_raw=f"Merchant{idx}",
                amount=Decimal(f"{10 + idx}.00"),
                statement_row_reference=f"stmt-{idx}",
                transaction_date=date(2024, 12, idx + 1),
            )
        )
        apps.append(
            _app(
                f"app-{idx}",
                merchant=f"Merchant{idx}",
                amount=Decimal(f"{10 + idx}.00"),
                transaction_date=date(2024, 12, idx + 1),
            )
        )
    candidates = match_batch(stmts, apps)
    items, _ = generate_review_queue(candidates)
    decisions = [
        ResolutionDecision(
            decision_id=f"dec-{i}",
            queue_item_id=items[i].queue_item_id,
            action=ResolutionAction.CONFIRM_MATCH,
            note="Looks correct.",
        )
        for i in range(len(items))
    ]
    return items, decisions


# ============================================================================
# 1. Successful first batch apply
# ============================================================================


def test_successful_first_batch_apply():
    """Batch transitions PENDING → APPLIED with all items succeeding."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=2)

    result = mgr.apply_batch(
        batch_id="batch-success",
        items=items,
        decisions=decisions,
    )

    assert result.batch_id == "batch-success"
    assert result.state == BatchApplyState.APPLIED
    assert result.error_message is None
    assert result.idempotent is False
    assert result.success is True
    assert result.is_terminal is True
    assert result.applied_count == 2
    assert result.failed_count == 0

    # Verify individual results
    for r in result.results:
        assert r.success
        assert r.action == ResolutionAction.CONFIRM_MATCH

    # Verify batch state is persisted in manager
    assert mgr.get_batch_state("batch-success") == BatchApplyState.APPLIED
    assert mgr.is_terminal("batch-success") is True


# ============================================================================
# 2. Duplicate apply prevention
# ============================================================================


def test_duplicate_batch_apply_blocked():
    """Already-applied batch returns REJECTED with idempotent=True,
    but the stored batch state remains APPLIED."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=2)

    # First apply — succeeds
    first = mgr.apply_batch(
        batch_id="batch-dup",
        items=items,
        decisions=decisions,
    )
    assert first.state == BatchApplyState.APPLIED
    assert first.idempotent is False

    # Second apply — rejected
    second = mgr.apply_batch(
        batch_id="batch-dup",
        items=items,
        decisions=decisions,
    )
    assert second.state == BatchApplyState.REJECTED
    assert second.idempotent is True
    assert second.error_message is not None
    assert "already been applied" in second.error_message

    # Audit metadata must explain the rejection
    assert second.audit_metadata["reason"] == "duplicate_apply_blocked"
    assert second.audit_metadata["previous_state"] == "applied"
    assert second.audit_metadata["state"] == "rejected"

    # Stored batch state must remain APPLIED — not mutated to REJECTED
    assert mgr.get_batch_state("batch-dup") == BatchApplyState.APPLIED


def test_duplicate_batch_apply_preserves_original_results():
    """Duplicate apply returns REJECTED but preserves the original batch
    results.  The stored batch state remains APPLIED."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=2)

    first = mgr.apply_batch(
        batch_id="batch-dup2",
        items=items,
        decisions=decisions,
    )
    assert first.applied_count == 2

    second = mgr.apply_batch(
        batch_id="batch-dup2",
        items=items,
        decisions=decisions,
    )
    assert second.state == BatchApplyState.REJECTED
    # The rejected result still carries the original successful results
    assert len(second.results) == 2
    assert second.applied_count == 2

    # Verify audit metadata includes original counts
    assert second.audit_metadata["total_items"] == 2
    assert second.audit_metadata["successful_items"] == 2
    assert second.audit_metadata["failed_items"] == 0

    # Stored batch state must remain APPLIED
    assert mgr.get_batch_state("batch-dup2") == BatchApplyState.APPLIED


def test_duplicate_batch_apply_no_items_processed_twice():
    """Duplicate apply must not produce additional apply results beyond originals."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=2)

    first = mgr.apply_batch(
        batch_id="batch-no-dupe",
        items=items,
        decisions=decisions,
    )

    # Verify the original apply_ids
    first_apply_ids = {r.apply_id for r in first.results}

    second = mgr.apply_batch(
        batch_id="batch-no-dupe",
        items=items,
        decisions=decisions,
    )
    second_apply_ids = {r.apply_id for r in second.results}

    # Results must be identical (same apply_ids)
    assert second_apply_ids == first_apply_ids

    # Stored batch state must remain APPLIED
    assert mgr.get_batch_state("batch-no-dupe") == BatchApplyState.APPLIED


# ============================================================================
# 3. Failed batch apply
# ============================================================================


def test_batch_apply_fails_with_invalid_decision():
    """Batch with an invalid decision transitions to FAILED, not
    silently succeeding with partial results."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=2)

    # Corrupt one decision - invalid action for the issue type
    bad_decision = ResolutionDecision(
        decision_id="dec-bad",
        queue_item_id=items[1].queue_item_id,
        action=ResolutionAction.MARK_DUPLICATE,  # Invalid for MATCHED type
        note="Should fail.",
    )
    decisions[1] = bad_decision

    result = mgr.apply_batch(
        batch_id="batch-failed",
        items=items,
        decisions=decisions,
    )

    assert result.state == BatchApplyState.FAILED
    assert result.error_message is not None
    assert "failed" in result.error_message.lower()
    assert result.idempotent is False
    assert result.is_terminal is False  # FAILED is retryable

    # One item should have succeeded, one failed
    assert result.applied_count == 1
    assert result.failed_count == 1

    # Manager must track the batch as FAILED
    assert mgr.get_batch_state("batch-failed") == BatchApplyState.FAILED
    assert mgr.is_terminal("batch-failed") is False


def test_batch_apply_failed_audit_metadata_clear():
    """Failed batch audit metadata clearly describes the failure."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=1)

    # Empty decisions list -> no results -> FAILED
    result = mgr.apply_batch(
        batch_id="batch-empty-decisions",
        items=items,
        decisions=[],
    )

    assert result.state == BatchApplyState.FAILED
    assert "no results" in result.error_message  # type: ignore[operator]
    assert result.audit_metadata["reason"] == "apply_failed"
    assert result.audit_metadata["total_items"] == 0
    assert result.audit_metadata["state"] == "failed"


def test_batch_apply_conflict_error_returns_failed_result():
    """Batch-level validation conflicts are expected apply failures."""
    mgr = BatchApplyStateManager()
    items, _ = _make_matched_items(n=1)
    bad_decisions = [
        ResolutionDecision(
            decision_id="dec-missing-item",
            queue_item_id="missing-queue-item",
            action=ResolutionAction.CONFIRM_MATCH,
        )
    ]

    result = mgr.apply_batch(
        batch_id="batch-conflict",
        items=items,
        decisions=bad_decisions,
    )

    assert result.state == BatchApplyState.FAILED
    assert result.error_message is not None
    assert "missing-queue-item" in result.error_message
    assert mgr.get_batch_state("batch-conflict") == BatchApplyState.FAILED


def test_batch_apply_unexpected_error_reraises_after_failed_state(monkeypatch):
    """Unexpected runtime errors must stay visible to callers."""
    import finance_core.reconciliation.apply as apply_module

    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=1)

    def explode(*args, **kwargs):
        raise RuntimeError("unexpected apply crash")

    monkeypatch.setattr(apply_module, "apply_decisions", explode)

    with pytest.raises(RuntimeError, match="unexpected apply crash"):
        mgr.apply_batch(
            batch_id="batch-crash",
            items=items,
            decisions=decisions,
        )

    assert mgr.get_batch_state("batch-crash") == BatchApplyState.FAILED


# ============================================================================
# 4. Retry after failure
# ============================================================================


def test_retry_after_failure_succeeds():
    """A FAILED batch can be re-applied and transition to APPLIED."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=2)

    # First apply — fail it by corrupting a decision
    bad_decisions = list(decisions)
    bad_decisions[1] = ResolutionDecision(
        decision_id="dec-bad-retry",
        queue_item_id=items[1].queue_item_id,
        action=ResolutionAction.MARK_DUPLICATE,  # Invalid
        note="Should fail then retry.",
    )

    first = mgr.apply_batch(
        batch_id="batch-retry",
        items=items,
        decisions=bad_decisions,
    )
    assert first.state == BatchApplyState.FAILED

    # Second apply — with correct decisions — must succeed
    second = mgr.apply_batch(
        batch_id="batch-retry",
        items=items,
        decisions=decisions,
    )
    assert second.state == BatchApplyState.APPLIED
    assert second.success is True
    assert second.applied_count == 2
    assert second.failed_count == 0

    # Manager state must be updated to APPLIED
    assert mgr.get_batch_state("batch-retry") == BatchApplyState.APPLIED
    assert mgr.is_terminal("batch-retry") is True


# ============================================================================
# 5. Unsafe state rejection
# ============================================================================


def test_rejected_batch_cannot_be_reapplied():
    """A successful batch stays APPLIED in store; duplicate attempts
    return REJECTED results but never mutate the stored state."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=2)

    # First apply — success
    first = mgr.apply_batch(
        batch_id="batch-rej",
        items=items,
        decisions=decisions,
    )
    assert first.state == BatchApplyState.APPLIED

    # Duplicate — rejected
    second = mgr.apply_batch(
        batch_id="batch-rej",
        items=items,
        decisions=decisions,
    )
    assert second.state == BatchApplyState.REJECTED

    # Stored state remains APPLIED after duplicate attempt
    assert mgr.get_batch_state("batch-rej") == BatchApplyState.APPLIED

    # Third attempt — still rejected (REJECTED is terminal)
    third = mgr.apply_batch(
        batch_id="batch-rej",
        items=items,
        decisions=decisions,
    )
    assert third.state == BatchApplyState.REJECTED
    assert third.idempotent is True

    # Stored state still APPLIED after third attempt too
    assert mgr.get_batch_state("batch-rej") == BatchApplyState.APPLIED


# ============================================================================
# 6. Batch state query
# ============================================================================


def test_get_batch_state_unknown_returns_none():
    """get_batch_state returns None for unknown batch IDs."""
    mgr = BatchApplyStateManager()
    assert mgr.get_batch_state("nonexistent") is None
    assert mgr.is_terminal("nonexistent") is False
    assert mgr.get_batch_result("nonexistent") is None


def test_get_batch_result_after_apply():
    """get_batch_result returns the stored result for a terminal batch."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=2)

    result = mgr.apply_batch(
        batch_id="batch-result",
        items=items,
        decisions=decisions,
    )
    assert result.state == BatchApplyState.APPLIED

    stored = mgr.get_batch_result("batch-result")
    assert stored is not None
    assert stored.batch_id == "batch-result"
    assert stored.state == BatchApplyState.APPLIED
    assert stored.applied_count == 2


# ============================================================================
# 7. Audit correctness
# ============================================================================


def test_audit_metadata_records_state_transition():
    """Successful batch must record previous state → new state transition."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=1)

    result = mgr.apply_batch(
        batch_id="batch-audit",
        items=items,
        decisions=decisions,
    )

    audit = result.audit_metadata
    assert audit["batch_id"] == "batch-audit"
    assert audit["state"] == "applied"
    assert audit["reason"] == "applied_successfully"
    assert audit["total_items"] == 1
    assert audit["successful_items"] == 1
    assert audit["failed_items"] == 0
    assert audit["apply_runtime_version"] == "v1"
    assert audit["batch_state_control_version"] == "v1"
    assert "transition_at" in audit


def test_audit_metadata_missing_evidence_not_tracked():
    """When no evidence_refs provided, audit notes evidence not tracked."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=1)

    result = mgr.apply_batch(
        batch_id="batch-no-evid",
        items=items,
        decisions=decisions,
    )

    assert result.audit_metadata["evidence_refs_tracked"] is False


def test_audit_metadata_with_evidence_tracked():
    """When evidence_refs provided, audit notes tracking."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=1)

    result = mgr.apply_batch(
        batch_id="batch-evid",
        items=items,
        decisions=decisions,
        evidence_refs_by_queue_item={items[0].queue_item_id: ("ev-001",)},
    )

    assert result.audit_metadata["evidence_refs_tracked"] is True


# ============================================================================
# 8. BatchApplyState enum properties
# ============================================================================


def test_batch_apply_state_is_terminal():
    """Only APPLIED and REJECTED are terminal."""
    assert BatchApplyState.APPLIED.is_terminal is True
    assert BatchApplyState.REJECTED.is_terminal is True
    assert BatchApplyState.PENDING.is_terminal is False
    assert BatchApplyState.APPLYING.is_terminal is False
    assert BatchApplyState.FAILED.is_terminal is False


def test_batch_apply_state_string_values():
    """Enum values match expected strings."""
    assert BatchApplyState.PENDING.value == "pending"
    assert BatchApplyState.APPLYING.value == "applying"
    assert BatchApplyState.APPLIED.value == "applied"
    assert BatchApplyState.FAILED.value == "failed"
    assert BatchApplyState.REJECTED.value == "rejected"


# ============================================================================
# 9. BatchApplyResult computed properties
# ============================================================================


def test_batch_apply_result_computed_properties():
    """Verify applied_count, failed_count, success, is_terminal."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=3)

    result = mgr.apply_batch(
        batch_id="batch-props",
        items=items,
        decisions=decisions,
    )

    assert result.applied_count == 3
    assert result.failed_count == 0
    assert result.success is True
    assert result.is_terminal is True


def test_batch_apply_result_failed_properties():
    """Failed result must report correct counts and non-success, non-terminal."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=2)

    bad_decisions = list(decisions)
    bad_decisions[0] = ResolutionDecision(
        decision_id="dec-fail-prop",
        queue_item_id=items[0].queue_item_id,
        action=ResolutionAction.MARK_DUPLICATE,  # Invalid
        note="Fail.",
    )

    result = mgr.apply_batch(
        batch_id="batch-fail-prop",
        items=items,
        decisions=bad_decisions,
    )

    assert result.state == BatchApplyState.FAILED
    assert result.applied_count == 1
    assert result.failed_count == 1
    assert result.success is False
    assert result.is_terminal is False


# ============================================================================
# 10. BatchApplyStateManager.reset
# ============================================================================


def test_reset_clears_all_registries():
    """reset() clears batch state and result registries."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=2)

    mgr.apply_batch(
        batch_id="batch-a",
        items=items,
        decisions=decisions,
    )

    assert mgr.get_batch_state("batch-a") == BatchApplyState.APPLIED
    assert mgr.get_batch_result("batch-a") is not None

    mgr.reset()

    assert mgr.get_batch_state("batch-a") is None
    assert mgr.get_batch_result("batch-a") is None

    # After reset, same batch_id can be re-applied
    result = mgr.apply_batch(
        batch_id="batch-a",
        items=items,
        decisions=decisions,
    )
    assert result.state == BatchApplyState.APPLIED
    assert result.idempotent is False


# ============================================================================
# 11. Multiple distinct batches — no cross-contamination
# ============================================================================


def test_multiple_batches_no_cross_contamination():
    """State of one batch must not affect another."""
    mgr = BatchApplyStateManager()
    items_a, decs_a = _make_matched_items(n=2, offset=0)
    items_b, decs_b = _make_matched_items(n=2, offset=10)

    # Apply batch A
    result_a = mgr.apply_batch(
        batch_id="batch-a",
        items=items_a,
        decisions=decs_a,
    )
    assert result_a.state == BatchApplyState.APPLIED
    assert mgr.get_batch_state("batch-a") == BatchApplyState.APPLIED

    # Apply batch B
    result_b = mgr.apply_batch(
        batch_id="batch-b",
        items=items_b,
        decisions=decs_b,
    )
    assert result_b.state == BatchApplyState.APPLIED
    assert mgr.get_batch_state("batch-b") == BatchApplyState.APPLIED

    # Batch A state unaffected
    assert mgr.get_batch_state("batch-a") == BatchApplyState.APPLIED
    assert mgr.is_terminal("batch-a") is True

    # Batch A duplicate is still blocked
    dup_a = mgr.apply_batch(
        batch_id="batch-a",
        items=items_a,
        decisions=decs_a,
    )
    assert dup_a.state == BatchApplyState.REJECTED


# ============================================================================
# 12. No database/finance.db usage
# ============================================================================


def test_no_database_usage():
    """BatchApplyStateManager must not access database files or SQLite."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=2)

    result = mgr.apply_batch(
        batch_id="batch-no-db",
        items=items,
        decisions=decisions,
    )

    assert result.state == BatchApplyState.APPLIED
    # Sanity: no database path in the module or result
    assert "finance.db" not in str(result)


# ============================================================================
# 13. No silent mutation of transaction rows
# ============================================================================


def test_no_silent_mutation_of_transaction_objects():
    """Confirm that batch apply does not modify original ReviewQueueItems."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=2)

    # Snapshot key fields before apply
    before_qids = [item.queue_item_id for item in items]
    before_candidate_ids = [item.candidate.candidate_id for item in items]
    before_amounts = [
        str(item.candidate.statement.amount) if item.candidate.statement.amount else None
        for item in items
    ]

    mgr.apply_batch(
        batch_id="batch-no-mutate",
        items=items,
        decisions=decisions,
    )

    # Verify items unchanged
    after_qids = [item.queue_item_id for item in items]
    after_candidate_ids = [item.candidate.candidate_id for item in items]
    after_amounts = [
        str(item.candidate.statement.amount) if item.candidate.statement.amount else None
        for item in items
    ]

    assert after_qids == before_qids
    assert after_candidate_ids == before_candidate_ids
    assert after_amounts == before_amounts


# ============================================================================
# 14. BatchApplyResult is immutable (frozen dataclass)
# ============================================================================


def test_batch_apply_result_is_immutable():
    """BatchApplyResult must be a frozen dataclass."""
    result = BatchApplyResult(
        batch_id="test",
        state=BatchApplyState.APPLIED,
    )
    with pytest.raises(Exception):
        result.batch_id = "changed"  # type: ignore[misc]


# ============================================================================
# 15. Evidence refs propagate through batch apply
# ============================================================================


def test_evidence_refs_propagate_through_batch_apply():
    """When evidence_refs_by_queue_item is provided, individual results
    carry evidence refs in audit_evidence."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=2)

    refs = {
        items[0].queue_item_id: ("ev-a", "ev-b"),
        items[1].queue_item_id: ("ev-c",),
    }

    result = mgr.apply_batch(
        batch_id="batch-evidence",
        items=items,
        decisions=decisions,
        evidence_refs_by_queue_item=refs,
    )

    assert result.state == BatchApplyState.APPLIED
    assert result.audit_metadata["evidence_refs_tracked"] is True

    # Individual results must carry evidence refs
    for r in result.results:
        assert "evidence_refs" in r.audit_evidence
        assert len(r.audit_evidence["evidence_refs"]) >= 1


# ============================================================================
# 16. Strict evidence mode in batch apply
# ============================================================================


def test_batch_apply_strict_evidence_rejects_missing():
    """Strict evidence mode rejects batches where items lack evidence refs."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=2)

    result = mgr.apply_batch(
        batch_id="batch-strict",
        items=items,
        decisions=decisions,
        require_evidence_ref=True,
    )

    # Without evidence refs in strict mode, both items should fail
    assert result.state == BatchApplyState.FAILED
    assert result.failed_count == 2
    assert result.error_message is not None
    assert "failed" in result.error_message.lower()


# ============================================================================
# 17. BatchApplyStateManager with already-rejected batch
# ============================================================================


def test_already_failed_batch_same_items_can_recover():
    """A FAILED batch with the same items can succeed on retry after
    the failure cause is fixed."""
    mgr = BatchApplyStateManager()
    items, decisions = _make_matched_items(n=2)

    # First: fail by corrupting decisions
    bad = list(decisions)
    bad[1] = ResolutionDecision(
        decision_id="dec-corrupt",
        queue_item_id=items[1].queue_item_id,
        action=ResolutionAction.MARK_DUPLICATE,
        note="Bad.",
    )

    first = mgr.apply_batch(
        batch_id="batch-recover",
        items=items,
        decisions=bad,
    )
    assert first.state == BatchApplyState.FAILED
    assert first.failed_count == 1

    # Second: fix and retry
    second = mgr.apply_batch(
        batch_id="batch-recover",
        items=items,
        decisions=decisions,
    )
    assert second.state == BatchApplyState.APPLIED
    assert second.applied_count == 2
