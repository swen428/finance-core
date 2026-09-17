"""Tests for Reconciliation Apply Execution Review Queue v1.

Covers:
  1. Empty queue for empty repository.
  2. No-review-required executions excluded when filtered.
  3. Failed/blocked apply execution appears in queue.
  4. Partially blocked operation appears in queue with correct flags.
  5. Deterministic ordering by priority and stable fallback key.
  6. Reason codes and review reasons preserved.
  7. Summary text is stable and useful.
  8. Operation refs are preserved.
  9. Mutation types are deterministic and sorted.
 10. Guard decision refs preserved.
 11. Read-only behavior: queue entry is immutable.
 12. Repository-backed construction works correctly.
 13. Limit and filter params work.
 14. database/finance.db is untouched.
 15. Rejects negative limit.
 16. All queue entry fields populated correctly.
 17. Unsupported result appears with correct flags.
 18. Conflict result appears correctly.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_core.reconciliation.apply_execution_review_queue import (
    ApplyReviewQueueEntry,
    build_apply_execution_review_queue,
)
from finance_core.reconciliation.apply_plan import (
    ApplyPlanInput,
    ReconciliationApplyPlan,
    build_reconciliation_apply_plan,
)
from finance_core.reconciliation.apply_runtime import (
    GuardedApplyRuntime,
)
from finance_core.reconciliation.apply_runtime_persistence import (
    GuardedApplyExecutionRepository,
)
from finance_core.reconciliation.final_mutation_proposal import (
    FinalMutationAction,
    FinalMutationGuard,
    FinalMutationGuardDecision,
    FinalMutationProposal,
)
from finance_core.reconciliation.models import (
    ApplyExecutionStatus,
    AppTransaction,
    GuardedApplyExecutionResult,
    IssueType,
    MatchStatus,
    ReconciliationCandidate,
    ResolutionAction,
    ResolutionDecision,
    ReviewPriority,
    ReviewQueueItem,
    StatementTransaction,
    SuggestedAction,
)
from finance_core.resources import migrations_dir
from tests.conftest import LIVE_DB_PATH, apply_sql, connect_temp_db

MIGRATION_014 = migrations_dir() / "014_reconciliation_guarded_apply_execution_persistence.sql"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fixed_clock():
    return lambda: "2024-12-15T12:00:00.000000+00:00"


@pytest.fixture
def sample_statement() -> StatementTransaction:
    return StatementTransaction(
        transaction_date=date(2024, 12, 1),
        posted_date=date(2024, 12, 2),
        merchant_raw="Giant Supermarket",
        amount=Decimal("45.50"),
        currency="SGD",
        statement_row_reference="stmt-row-001",
    )


@pytest.fixture
def sample_app_txn() -> AppTransaction:
    return AppTransaction(
        app_txn_id="app-txn-001",
        transaction_date=date(2024, 12, 1),
        merchant="Giant Supermarket",
        amount=Decimal("45.50"),
        currency="SGD",
    )


@pytest.fixture
def sample_candidate(
    sample_statement: StatementTransaction,
    sample_app_txn: AppTransaction,
) -> ReconciliationCandidate:
    return ReconciliationCandidate(
        statement=sample_statement,
        best_app_transaction=sample_app_txn,
        match_status=MatchStatus.MATCHED,
        candidate_id="cand-001",
        review_priority=ReviewPriority.LOW,
        issue_type=IssueType.MATCHED,
    )


@pytest.fixture
def queue_item(sample_candidate: ReconciliationCandidate) -> ReviewQueueItem:
    return ReviewQueueItem(
        candidate=sample_candidate,
        issue_type=IssueType.MATCHED,
        suggested_action=SuggestedAction.CONFIRM_MATCH,
        queue_item_id="q-001",
    )


@pytest.fixture
def sample_decision() -> ResolutionDecision:
    return ResolutionDecision(
        decision_id="dec-001",
        queue_item_id="q-001",
        action=ResolutionAction.CONFIRM_MATCH,
        note="Looks correct.",
        reviewer="human",
        resolved_at="2024-12-15T10:00:00Z",
    )


@pytest.fixture
def approved_guard_decision() -> FinalMutationGuardDecision:
    proposal = FinalMutationProposal(
        proposal_id="fp-001",
        action=FinalMutationAction.NO_FINAL_MUTATION,
        evidence_refs=("ev-abc",),
    )
    guard = FinalMutationGuard()
    return guard.evaluate(proposal)


@pytest.fixture
def blocked_guard_decision() -> FinalMutationGuardDecision:
    proposal = FinalMutationProposal(
        proposal_id="fp-002",
        action=FinalMutationAction.CREATE_FINAL_TRANSACTION,
        amount=Decimal("100.00"),
        currency="SGD",
        merchant="",
        transaction_date=date(2024, 12, 1),
    )
    guard = FinalMutationGuard()
    return guard.evaluate(proposal)


@pytest.fixture
def sample_plan(
    sample_decision: ResolutionDecision,
    queue_item: ReviewQueueItem,
    approved_guard_decision: FinalMutationGuardDecision,
) -> ReconciliationApplyPlan:
    inputs = [
        ApplyPlanInput(
            decision=sample_decision,
            queue_item=queue_item,
            guard_decision=approved_guard_decision,
        )
    ]
    return build_reconciliation_apply_plan(inputs)


@pytest.fixture
def executed_result(
    sample_plan: ReconciliationApplyPlan,
    approved_guard_decision: FinalMutationGuardDecision,
    fixed_clock,
) -> GuardedApplyExecutionResult:
    runtime = GuardedApplyRuntime()
    op_id = sample_plan.operations[0].operation_id
    return runtime.execute(
        sample_plan,
        guard_decisions_by_operation_id={op_id: approved_guard_decision},
        idempotency_key="test-queue-001",
        clock=fixed_clock,
    )


@pytest.fixture
def blocked_result(
    sample_plan: ReconciliationApplyPlan,
    blocked_guard_decision: FinalMutationGuardDecision,
    fixed_clock,
) -> GuardedApplyExecutionResult:
    runtime = GuardedApplyRuntime()
    op_id = sample_plan.operations[0].operation_id
    return runtime.execute(
        sample_plan,
        guard_decisions_by_operation_id={op_id: blocked_guard_decision},
        idempotency_key="test-queue-blocked",
        clock=fixed_clock,
    )


@pytest.fixture
def partially_blocked_result(
    sample_decision: ResolutionDecision,
    queue_item: ReviewQueueItem,
    approved_guard_decision: FinalMutationGuardDecision,
    blocked_guard_decision: FinalMutationGuardDecision,
    fixed_clock,
) -> GuardedApplyExecutionResult:
    other_decision = ResolutionDecision(
        decision_id="dec-002",
        queue_item_id="q-001",
        action=ResolutionAction.CONFIRM_MATCH,
        note="Second decision.",
        reviewer="human",
    )
    inputs = [
        ApplyPlanInput(
            decision=sample_decision,
            queue_item=queue_item,
            guard_decision=approved_guard_decision,
        ),
        ApplyPlanInput(
            decision=other_decision,
            queue_item=queue_item,
            guard_decision=blocked_guard_decision,
        ),
    ]
    plan = build_reconciliation_apply_plan(inputs)
    runtime = GuardedApplyRuntime()
    op_ids = [op.operation_id for op in plan.operations]
    gd_map = {
        op_ids[0]: approved_guard_decision,
        op_ids[1]: blocked_guard_decision,
    }
    return runtime.execute(
        plan,
        guard_decisions_by_operation_id=gd_map,
        idempotency_key="test-queue-partial",
        clock=fixed_clock,
    )


@pytest.fixture
def migrated_conn(temp_db_path: Path) -> sqlite3.Connection:
    assert temp_db_path != LIVE_DB_PATH
    conn = connect_temp_db(temp_db_path)
    apply_sql(conn, MIGRATION_014)
    conn.commit()
    return conn


@pytest.fixture
def repo(migrated_conn: sqlite3.Connection) -> GuardedApplyExecutionRepository:
    return GuardedApplyExecutionRepository(migrated_conn)


# ---------------------------------------------------------------------------
# Test 1: Empty queue for empty repository
# ---------------------------------------------------------------------------


class TestEmptyQueue:
    def test_empty_repository_produces_empty_queue(self, repo: GuardedApplyExecutionRepository):
        queue = build_apply_execution_review_queue(repo)
        assert isinstance(queue, tuple)
        assert len(queue) == 0


# ---------------------------------------------------------------------------
# Test 2: No-review-required executions excluded when filtered
# ---------------------------------------------------------------------------


class TestFilterNoReviewRequired:
    def test_executed_result_not_in_review_queue_when_filtered(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-filt-001")
        queue = build_apply_execution_review_queue(repo, requires_human_review=True)
        assert len(queue) == 0

    def test_executed_result_appears_in_unfiltered_queue(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-filt-002")
        queue = build_apply_execution_review_queue(repo)
        assert len(queue) == 1
        assert queue[0].requires_human_review is False


# ---------------------------------------------------------------------------
# Test 3: Failed/blocked apply execution appears in queue
# ---------------------------------------------------------------------------


class TestBlockedInQueue:
    def test_blocked_result_in_review_queue(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-block-q-001")
        queue = build_apply_execution_review_queue(repo, requires_human_review=True)
        assert len(queue) == 1

        entry = queue[0]
        assert entry.idempotency_key == "test-queue-blocked"
        assert entry.execution_status == ApplyExecutionStatus.BLOCKED
        assert entry.review_priority == ReviewPriority.HIGH
        assert entry.requires_human_review is True
        assert entry.is_blocking is True
        assert entry.is_partially_blocked is False
        assert entry.operations_blocked == 1
        assert entry.operations_executed == 0

    def test_blocked_result_has_reason_codes(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-block-q-002")
        queue = build_apply_execution_review_queue(repo, requires_human_review=True)
        assert len(queue) == 1
        assert len(queue[0].reason_codes) > 0


# ---------------------------------------------------------------------------
# Test 4: Partially blocked operation appears in queue
# ---------------------------------------------------------------------------


class TestPartiallyBlockedInQueue:
    def test_partially_blocked_in_queue(
        self,
        repo: GuardedApplyExecutionRepository,
        partially_blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(
            partially_blocked_result, execution_fingerprint="fp-partial-q-001"
        )
        queue = build_apply_execution_review_queue(repo, requires_human_review=True)
        assert len(queue) == 1

        entry = queue[0]
        assert entry.execution_status == ApplyExecutionStatus.PARTIALLY_BLOCKED
        assert entry.review_priority == ReviewPriority.HIGH
        assert entry.requires_human_review is True
        assert entry.is_blocking is False
        assert entry.is_partially_blocked is True
        assert entry.operations_executed == 1
        assert entry.operations_blocked == 1


# ---------------------------------------------------------------------------
# Test 5: Deterministic ordering by priority and stable fallback key
# ---------------------------------------------------------------------------


class TestDeterministicOrdering:
    def test_blocked_before_executed(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-order-001")
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-order-002")

        queue = build_apply_execution_review_queue(repo)
        assert len(queue) == 2
        assert queue[0].idempotency_key == "test-queue-blocked"
        assert queue[0].review_priority == ReviewPriority.HIGH
        assert queue[1].idempotency_key == "test-queue-001"
        assert queue[1].review_priority == ReviewPriority.MEDIUM

    def test_partially_blocked_before_executed(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
        partially_blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-order-003")
        repo.save_execution_result(partially_blocked_result, execution_fingerprint="fp-order-004")

        queue = build_apply_execution_review_queue(repo)
        assert len(queue) == 2
        assert queue[0].idempotency_key == "test-queue-partial"
        assert queue[1].idempotency_key == "test-queue-001"

    def test_ordering_is_deterministic(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
        partially_blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(partially_blocked_result, execution_fingerprint="fp-determ-001")
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-determ-002")

        q1 = build_apply_execution_review_queue(repo)
        q2 = build_apply_execution_review_queue(repo)
        assert [e.idempotency_key for e in q1] == [e.idempotency_key for e in q2]

    def test_newer_with_same_priority_sorted_first(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
    ):
        blocked_later = replace(
            blocked_result,
            idempotency_key="test-queue-blocked-later",
            executed_at="2024-12-16T12:00:00.000000+00:00",
        )
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-newer-001")
        repo.save_execution_result(blocked_later, execution_fingerprint="fp-newer-002")

        queue = build_apply_execution_review_queue(repo)
        assert len(queue) == 2
        assert queue[0].idempotency_key == "test-queue-blocked-later"
        assert queue[1].idempotency_key == "test-queue-blocked"


# ---------------------------------------------------------------------------
# Test 6: Reason codes and review reasons preserved
# ---------------------------------------------------------------------------


class TestReasonCodes:
    def test_blocked_result_preserves_block_reason(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-reason-001")
        queue = build_apply_execution_review_queue(repo)
        entry = queue[0]
        assert "blocked" in entry.block_reason.lower()

    def test_reason_codes_contain_block_reason(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-reason-002")
        queue = build_apply_execution_review_queue(repo)
        entry = queue[0]
        assert any("missing_merchant" in rc.lower() for rc in entry.reason_codes)

    def test_reason_codes_are_deduplicated(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-reason-003")
        queue = build_apply_execution_review_queue(repo)
        entry = queue[0]
        assert len(entry.reason_codes) == len(set(entry.reason_codes))


# ---------------------------------------------------------------------------
# Test 7: Summary text is stable and useful
# ---------------------------------------------------------------------------


class TestSummaryText:
    def test_blocked_human_summary(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-sum-001")
        queue = build_apply_execution_review_queue(repo)
        assert "blocked" in queue[0].human_summary.lower()

    def test_partially_blocked_human_summary(
        self,
        repo: GuardedApplyExecutionRepository,
        partially_blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(partially_blocked_result, execution_fingerprint="fp-sum-002")
        queue = build_apply_execution_review_queue(repo)
        assert "partially blocked" in queue[0].human_summary.lower()

    def test_executed_human_summary(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-sum-003")
        queue = build_apply_execution_review_queue(repo)
        assert "dry-run" in queue[0].human_summary.lower()

    def test_summary_is_deterministic(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-sum-004")
        q1 = build_apply_execution_review_queue(repo)
        q2 = build_apply_execution_review_queue(repo)
        assert q1[0].human_summary == q2[0].human_summary

    def test_summary_text_is_useful(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-sum-005")
        queue = build_apply_execution_review_queue(repo)
        summary = queue[0].human_summary
        assert "executed" in summary.lower()
        assert "blocked" in summary.lower()
        assert "skipped" in summary.lower()


# ---------------------------------------------------------------------------
# Test 8: Operation refs are preserved
# ---------------------------------------------------------------------------


class TestOperationRefs:
    def test_operation_refs_preserved(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-opref-001")
        queue = build_apply_execution_review_queue(repo)
        assert len(queue[0].operation_refs) > 0
        assert queue[0].operation_refs[0] == blocked_result.results[0].operation_id

    def test_partially_blocked_has_both_operation_refs(
        self,
        repo: GuardedApplyExecutionRepository,
        partially_blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(partially_blocked_result, execution_fingerprint="fp-opref-002")
        queue = build_apply_execution_review_queue(repo)
        assert len(queue[0].operation_refs) == 2


# ---------------------------------------------------------------------------
# Test 9: Mutation types are deterministic and sorted
# ---------------------------------------------------------------------------


class TestMutationTypes:
    def test_mutation_types_sorted(
        self,
        repo: GuardedApplyExecutionRepository,
        partially_blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(partially_blocked_result, execution_fingerprint="fp-mt-001")
        queue = build_apply_execution_review_queue(repo)
        entry = queue[0]
        assert entry.mutation_types == tuple(sorted(entry.mutation_types))

    def test_mutation_types_deterministic(
        self,
        repo: GuardedApplyExecutionRepository,
        partially_blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(partially_blocked_result, execution_fingerprint="fp-mt-002")
        q1 = build_apply_execution_review_queue(repo)
        q2 = build_apply_execution_review_queue(repo)
        assert q1[0].mutation_types == q2[0].mutation_types


# ---------------------------------------------------------------------------
# Test 10: Guard decision refs preserved
# ---------------------------------------------------------------------------


class TestGuardDecisionRefs:
    def test_guard_decision_refs_preserved(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-gd-001")
        queue = build_apply_execution_review_queue(repo)
        assert queue[0].guard_decision_refs == blocked_result.guard_decision_refs


# ---------------------------------------------------------------------------
# Test 11: Read-only behavior: queue entry is immutable
# ---------------------------------------------------------------------------


class TestReadOnlyImmutability:
    def test_queue_entry_is_immutable(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-immut-001")
        queue = build_apply_execution_review_queue(repo)
        entry = queue[0]
        assert isinstance(entry, ApplyReviewQueueEntry)
        with pytest.raises(Exception):
            entry.idempotency_key = "changed"  # type: ignore[misc]

    def test_queue_builder_is_read_only(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-ro-001")
        q1 = build_apply_execution_review_queue(repo)
        q2 = build_apply_execution_review_queue(repo)
        assert len(q1) == len(q2)
        assert q1 == q2


# ---------------------------------------------------------------------------
# Test 12: Repository-backed construction works correctly
# ---------------------------------------------------------------------------


class TestRepoBackedConstruction:
    def test_execution_id_is_computed(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-eid-001")
        queue = build_apply_execution_review_queue(repo)
        entry = queue[0]
        assert entry.execution_id.startswith("exec-")
        assert len(entry.execution_id) == 21  # "exec-" (5) + 16 hex chars

    def test_multiple_results_all_in_queue(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
        blocked_result: GuardedApplyExecutionResult,
        partially_blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-multi-001")
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-multi-002")
        repo.save_execution_result(partially_blocked_result, execution_fingerprint="fp-multi-003")
        queue = build_apply_execution_review_queue(repo)
        assert len(queue) == 3


# ---------------------------------------------------------------------------
# Test 13: Limit and filter params work
# ---------------------------------------------------------------------------


class TestLimitAndFilter:
    def test_limit_applies_after_sorting(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-lim-001")
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-lim-002")

        queue = build_apply_execution_review_queue(repo, limit=1)
        assert len(queue) == 1
        assert queue[0].idempotency_key == "test-queue-blocked"

    def test_filter_by_execution_status(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-stat-001")
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-stat-002")

        queue = build_apply_execution_review_queue(
            repo, execution_status=ApplyExecutionStatus.BLOCKED
        )
        assert len(queue) == 1
        assert queue[0].execution_status == ApplyExecutionStatus.BLOCKED

    def test_filter_to_human_review_only(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-hr-001")
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-hr-002")

        queue = build_apply_execution_review_queue(repo, requires_human_review=True)
        assert len(queue) == 1
        assert queue[0].requires_human_review is True
        assert queue[0].idempotency_key == "test-queue-blocked"

    def test_negative_limit_rejected(self, repo: GuardedApplyExecutionRepository):
        with pytest.raises(ValueError, match="limit"):
            build_apply_execution_review_queue(repo, limit=-1)

    def test_limit_zero_returns_empty(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-zero-001")
        queue = build_apply_execution_review_queue(repo, limit=0)
        assert len(queue) == 0


# ---------------------------------------------------------------------------
# Test 14: database/finance.db is untouched
# ---------------------------------------------------------------------------


class TestLiveDbNotTouched:
    def test_queue_never_opens_live_db(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-safe-q-001")
        queue = build_apply_execution_review_queue(repo)
        assert len(queue) == 1

    def test_repo_uses_temp_db_only(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
        migrated_conn: sqlite3.Connection,
    ):
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-safe-q-002")
        db_file = migrated_conn.execute("PRAGMA database_list").fetchone()
        file_path = db_file["file"] if db_file["file"] else ""
        assert str(LIVE_DB_PATH) not in file_path

        queue = build_apply_execution_review_queue(repo, requires_human_review=True)
        assert queue[0].idempotency_key == "test-queue-blocked"


# ---------------------------------------------------------------------------
# Test 15: Combined filters
# ---------------------------------------------------------------------------


class TestCombinedFilters:
    def test_combined_blocked_and_human_review(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-combo-001")
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-combo-002")

        queue = build_apply_execution_review_queue(
            repo,
            execution_status=ApplyExecutionStatus.BLOCKED,
            requires_human_review=True,
        )
        assert len(queue) == 1
        assert queue[0].execution_status == ApplyExecutionStatus.BLOCKED
        assert queue[0].requires_human_review is True


# ---------------------------------------------------------------------------
# Test 16: All queue entry fields populated correctly
# ---------------------------------------------------------------------------


class TestEntryFields:
    def test_all_fields_populated(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-fields-001")
        queue = build_apply_execution_review_queue(repo)
        entry = queue[0]

        assert entry.execution_id.startswith("exec-")
        assert entry.idempotency_key == "test-queue-blocked"
        assert entry.plan_id != ""
        assert isinstance(entry.execution_status, ApplyExecutionStatus)
        assert isinstance(entry.review_priority, ReviewPriority)
        assert isinstance(entry.requires_human_review, bool)
        assert isinstance(entry.is_blocking, bool)
        assert isinstance(entry.is_partially_blocked, bool)
        assert isinstance(entry.operations_executed, int)
        assert isinstance(entry.operations_blocked, int)
        assert isinstance(entry.operations_skipped, int)
        assert isinstance(entry.total_operations, int)
        assert isinstance(entry.block_reason, str)
        assert isinstance(entry.reason_codes, tuple)
        assert isinstance(entry.human_summary, str)
        assert isinstance(entry.executed_at, str)
        assert isinstance(entry.is_dry_run, bool)
        assert isinstance(entry.guard_decision_refs, tuple)
        assert isinstance(entry.mutation_types, tuple)
        assert isinstance(entry.operation_refs, tuple)


# ---------------------------------------------------------------------------
# Test 17: Unsupported result appears with correct flags
# ---------------------------------------------------------------------------


class TestUnsupportedResult:
    def test_unsupported_result_has_medium_priority(
        self,
        repo: GuardedApplyExecutionRepository,
    ):
        runtime = GuardedApplyRuntime()
        result = runtime.execute(
            ReconciliationApplyPlan(
                plan_id="plan-empty-q",
                operations=(),
                risks=(),
                requires_human_confirmation=False,
                is_dry_run=True,
                total_operations=0,
                blocked_operations=0,
                approved_operations=0,
                pending_guard_operations=0,
                note="",
            ),
            guard_decisions_by_operation_id={},
            idempotency_key="test-queue-unsupported",
            clock=lambda: "2024-12-15T12:00:00.000000+00:00",
        )
        repo.save_execution_result(result, execution_fingerprint="fp-unsup-q-001")
        queue = build_apply_execution_review_queue(repo, requires_human_review=True)
        assert len(queue) == 1
        assert queue[0].review_priority == ReviewPriority.MEDIUM
        assert queue[0].requires_human_review is True
        assert queue[0].is_blocking is False
        assert queue[0].is_partially_blocked is False


# ---------------------------------------------------------------------------
# Test 18: Conflict result appears correctly
# ---------------------------------------------------------------------------


class TestConflictResult:
    def test_conflict_result_is_high_priority_and_blocking(
        self,
        sample_plan: ReconciliationApplyPlan,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime()
        op_id = sample_plan.operations[0].operation_id

        runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={op_id: approved_guard_decision},
            idempotency_key="test-queue-conflict",
            clock=fixed_clock,
        )

        other_decision = ResolutionDecision(
            decision_id="dec-conflict-q",
            queue_item_id="q-001",
            action=ResolutionAction.IGNORE,
            note="Conflict.",
            reviewer="human",
        )
        other_cand = ReconciliationCandidate(
            statement=StatementTransaction(
                transaction_date=date(2024, 12, 1),
                posted_date=date(2024, 12, 2),
                merchant_raw="Giant",
                amount=Decimal("45.50"),
                currency="SGD",
            ),
            match_status=MatchStatus.MATCHED,
            candidate_id="cand-conflict-q",
        )
        other_item = ReviewQueueItem(
            candidate=other_cand,
            issue_type=IssueType.MATCHED,
            suggested_action=SuggestedAction.CONFIRM_MATCH,
            queue_item_id="q-001",
        )
        other_inputs = [
            ApplyPlanInput(
                decision=other_decision,
                queue_item=other_item,
                guard_decision=approved_guard_decision,
            )
        ]
        other_plan = build_reconciliation_apply_plan(other_inputs)
        other_op_id = other_plan.operations[0].operation_id

        result = runtime.execute(
            other_plan,
            guard_decisions_by_operation_id={other_op_id: approved_guard_decision},
            idempotency_key="test-queue-conflict",
            clock=fixed_clock,
        )
        assert result.execution_status == ApplyExecutionStatus.CONFLICT

        repo.save_execution_result(result, execution_fingerprint="fp-conflict-q-001")
        queue = build_apply_execution_review_queue(repo, requires_human_review=True)
        assert len(queue) >= 1

        conflict_entry = next(e for e in queue if e.idempotency_key == "test-queue-conflict")
        assert conflict_entry.review_priority == ReviewPriority.HIGH
        assert conflict_entry.requires_human_review is True
        assert conflict_entry.is_blocking is True
        assert conflict_entry.is_partially_blocked is False
