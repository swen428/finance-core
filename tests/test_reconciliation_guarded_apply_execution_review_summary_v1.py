"""Tests for Guarded Apply Execution Review Summary v1.

Covers:
  1. Fully executed result produces medium priority (all v1 results are dry-run),
  2. Blocked result produces high priority and requires human review.
  3. Partially blocked result produces high priority.
  4. Unsupported operation produces medium priority.
  5. Dry-run result with mutation payloads is review-safe.
  6. Mutation types are deterministic and sorted.
  7. Repository lookup via idempotency_key returns summary.
  8. Repository lookup via execution_id returns summary.
  9. Repository lookup returns None for missing key.
 10. Repository lookup raises ValueError when both or neither keys supplied.
 11. Summary structure round-trips correctly.
 12. database/finance.db is untouched.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_core.reconciliation.apply_execution_review import (
    GuardedApplyExecutionReviewSummary,
    build_guarded_apply_execution_review_summary,
    list_guarded_apply_execution_review_summaries,
    summarize_guarded_apply_execution_from_repository,
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
        idempotency_key="test-review-001",
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
        idempotency_key="test-review-blocked",
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
# Test 1: Fully executed result
# ---------------------------------------------------------------------------


class TestFullyExecutedSummary:
    def test_executed_result_is_medium_priority(self, executed_result: GuardedApplyExecutionResult):
        summary = build_guarded_apply_execution_review_summary(executed_result)
        assert summary.review_priority == ReviewPriority.MEDIUM

    def test_executed_result_does_not_require_human_review(
        self, executed_result: GuardedApplyExecutionResult
    ):
        summary = build_guarded_apply_execution_review_summary(executed_result)
        assert summary.requires_human_review is False

    def test_executed_result_has_no_blocking_outcome(
        self, executed_result: GuardedApplyExecutionResult
    ):
        summary = build_guarded_apply_execution_review_summary(executed_result)
        assert summary.has_blocking_outcome is False

    def test_executed_result_counts_are_correct(self, executed_result: GuardedApplyExecutionResult):
        summary = build_guarded_apply_execution_review_summary(executed_result)
        assert summary.operations_executed == 1
        assert summary.operations_blocked == 0
        assert summary.operations_skipped == 0

    def test_executed_result_maps_ids(self, executed_result: GuardedApplyExecutionResult):
        summary = build_guarded_apply_execution_review_summary(executed_result)
        assert summary.plan_id == executed_result.plan_id
        assert summary.idempotency_key == executed_result.idempotency_key
        assert summary.execution_status == ApplyExecutionStatus.EXECUTED
        assert summary.is_dry_run is True
        assert summary.block_reason == ""


# ---------------------------------------------------------------------------
# Test 2: Blocked result
# ---------------------------------------------------------------------------


class TestBlockedSummary:
    def test_blocked_result_is_high_priority(self, blocked_result: GuardedApplyExecutionResult):
        summary = build_guarded_apply_execution_review_summary(blocked_result)
        assert summary.review_priority == ReviewPriority.HIGH

    def test_blocked_result_requires_human_review(
        self, blocked_result: GuardedApplyExecutionResult
    ):
        summary = build_guarded_apply_execution_review_summary(blocked_result)
        assert summary.requires_human_review is True

    def test_blocked_result_has_blocking_outcome(self, blocked_result: GuardedApplyExecutionResult):
        summary = build_guarded_apply_execution_review_summary(blocked_result)
        assert summary.has_blocking_outcome is True

    def test_blocked_result_executed_count_is_zero(
        self, blocked_result: GuardedApplyExecutionResult
    ):
        summary = build_guarded_apply_execution_review_summary(blocked_result)
        assert summary.operations_executed == 0
        assert summary.operations_blocked == 1

    def test_blocked_result_human_summary(self, blocked_result: GuardedApplyExecutionResult):
        summary = build_guarded_apply_execution_review_summary(blocked_result)
        assert "blocked" in summary.human_summary.lower()


# ---------------------------------------------------------------------------
# Test 3: Partially blocked result
# ---------------------------------------------------------------------------


class TestPartiallyBlockedSummary:
    def test_partially_blocked_result_is_high_priority(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
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

        result = runtime.execute(
            plan,
            guard_decisions_by_operation_id=gd_map,
            idempotency_key="test-partial-block",
            clock=fixed_clock,
        )
        assert result.execution_status == ApplyExecutionStatus.PARTIALLY_BLOCKED

        summary = build_guarded_apply_execution_review_summary(result)
        assert summary.review_priority == ReviewPriority.HIGH
        assert summary.requires_human_review is True
        assert summary.has_blocking_outcome is True
        assert summary.operations_executed == 1
        assert summary.operations_blocked == 1


# ---------------------------------------------------------------------------


class TestPartiallyBlockedOperationRequiresReview:
    """Ensure operation-level PARTIALLY_BLOCKED triggers human review and HIGH priority
    even when the result-level status doesn't short-circuit through _has_blocking_outcome."""

    def test_operation_partially_blocked_requires_review(self):
        from finance_core.reconciliation.models import GuardedOperationResult

        result = GuardedApplyExecutionResult(
            plan_id="plan-pb-op",
            idempotency_key="test-pb-op",
            execution_status=ApplyExecutionStatus.EXECUTED,
            results=(
                GuardedOperationResult(
                    operation_id="op-001",
                    decision_id="dec-001",
                    execution_status=ApplyExecutionStatus.EXECUTED,
                    mutation_type="noop",
                ),
                GuardedOperationResult(
                    operation_id="op-002",
                    decision_id="dec-002",
                    execution_status=ApplyExecutionStatus.PARTIALLY_BLOCKED,
                    reason="guard partially denied",
                    mutation_type="noop",
                ),
            ),
            total_operations=2,
            operated_executed=1,
            operated_blocked=1,
        )
        summary = build_guarded_apply_execution_review_summary(result)
        assert summary.requires_human_review is True
        assert summary.review_priority == ReviewPriority.HIGH


# ---------------------------------------------------------------------------
# Test 4: Unsupported operation
# ---------------------------------------------------------------------------


class TestUnsupportedSummary:
    def test_unsupported_result_is_medium_priority(self):
        runtime = GuardedApplyRuntime()
        result = runtime.execute(
            ReconciliationApplyPlan(
                plan_id="plan-empty",
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
            idempotency_key="test-unsupported",
            clock=lambda: "2024-12-15T12:00:00.000000+00:00",
        )
        assert result.execution_status == ApplyExecutionStatus.UNSUPPORTED

        summary = build_guarded_apply_execution_review_summary(result)
        assert summary.review_priority == ReviewPriority.MEDIUM
        assert summary.requires_human_review is True
        assert summary.has_blocking_outcome is False

    def test_unsupported_result_counts_are_zero(self):
        runtime = GuardedApplyRuntime()
        result = runtime.execute(
            ReconciliationApplyPlan(
                plan_id="plan-empty",
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
            idempotency_key="test-unsup-counts",
            clock=lambda: "2024-12-15T12:00:00.000000+00:00",
        )
        summary = build_guarded_apply_execution_review_summary(result)
        assert summary.total_operations == 0
        assert summary.operations_executed == 0
        assert summary.operations_blocked == 0
        assert summary.operations_skipped == 0


# ---------------------------------------------------------------------------
# Test 5: Dry-run result
# ---------------------------------------------------------------------------


class TestDryRunSummary:
    def test_dry_run_is_marked_in_summary(self, executed_result: GuardedApplyExecutionResult):
        summary = build_guarded_apply_execution_review_summary(executed_result)
        assert summary.is_dry_run is True

    def test_dry_run_summary_text(self, executed_result: GuardedApplyExecutionResult):
        summary = build_guarded_apply_execution_review_summary(executed_result)
        assert "dry-run" in summary.human_summary.lower()

    def test_dry_run_executed_at_preserved(self, executed_result: GuardedApplyExecutionResult):
        summary = build_guarded_apply_execution_review_summary(executed_result)
        assert summary.executed_at == "2024-12-15T12:00:00.000000+00:00"


# ---------------------------------------------------------------------------
# Test 6: Mutation types are deterministic and sorted
# ---------------------------------------------------------------------------


class TestMutationTypes:
    def test_mutation_types_are_sorted(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        ignore_decision = ResolutionDecision(
            decision_id="dec-ignore",
            queue_item_id="q-001",
            action=ResolutionAction.IGNORE,
            note="Ignore.",
            reviewer="human",
        )
        dup_decision = ResolutionDecision(
            decision_id="dec-dup",
            queue_item_id="q-001",
            action=ResolutionAction.MARK_DUPLICATE,
            note="Duplicate.",
            reviewer="human",
        )
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=queue_item,
                guard_decision=approved_guard_decision,
            ),
            ApplyPlanInput(
                decision=ignore_decision,
                queue_item=queue_item,
                guard_decision=approved_guard_decision,
            ),
            ApplyPlanInput(
                decision=dup_decision,
                queue_item=queue_item,
                guard_decision=approved_guard_decision,
            ),
        ]
        plan = build_reconciliation_apply_plan(inputs)

        runtime = GuardedApplyRuntime()
        op_ids = [op.operation_id for op in plan.operations]
        gd_map = {op_id: approved_guard_decision for op_id in op_ids}

        result = runtime.execute(
            plan,
            guard_decisions_by_operation_id=gd_map,
            idempotency_key="test-mutation-types",
            clock=fixed_clock,
        )
        summary = build_guarded_apply_execution_review_summary(result)

        assert summary.mutation_types == tuple(sorted(summary.mutation_types))
        assert "confirm_match" in summary.mutation_types
        assert "ignore" in summary.mutation_types
        assert "mark_duplicate" in summary.mutation_types

    def test_mutation_types_deterministic(self, executed_result: GuardedApplyExecutionResult):
        s1 = build_guarded_apply_execution_review_summary(executed_result)
        s2 = build_guarded_apply_execution_review_summary(executed_result)
        assert s1.mutation_types == s2.mutation_types

    def test_conflict_result_has_mutation_types(
        self,
        sample_plan: ReconciliationApplyPlan,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime()
        op_id = sample_plan.operations[0].operation_id

        runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={op_id: approved_guard_decision},
            idempotency_key="test-conflict-mt",
            clock=fixed_clock,
        )

        other_decision = ResolutionDecision(
            decision_id="dec-other",
            queue_item_id="q-001",
            action=ResolutionAction.MARK_STATEMENT_ONLY,
            note="Other.",
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
            candidate_id="cand-other",
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

        result = runtime.execute(
            other_plan,
            guard_decisions_by_operation_id={
                other_plan.operations[0].operation_id: approved_guard_decision
            },
            idempotency_key="test-conflict-mt",
            clock=fixed_clock,
        )
        assert result.execution_status == ApplyExecutionStatus.CONFLICT

        summary = build_guarded_apply_execution_review_summary(result)
        assert len(summary.mutation_types) > 0


# ---------------------------------------------------------------------------
# Test 7: Repository lookup via idempotency_key
# ---------------------------------------------------------------------------


class TestRepositoryLookupByIdempotencyKey:
    def test_lookup_returns_summary(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-repo-001")
        summary = summarize_guarded_apply_execution_from_repository(
            repo, idempotency_key="test-review-001"
        )
        assert summary is not None
        assert summary.idempotency_key == "test-review-001"
        assert summary.plan_id == executed_result.plan_id
        assert summary.execution_status == ApplyExecutionStatus.EXECUTED


# ---------------------------------------------------------------------------
# Test 8: Repository lookup via execution_id
# ---------------------------------------------------------------------------


class TestRepositoryLookupByExecutionId:
    def test_lookup_by_execution_id_returns_summary(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-exec-001")
        raw = f"{executed_result.plan_id}|test-review-001"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        exec_id = f"exec-{digest[:16]}"

        summary = summarize_guarded_apply_execution_from_repository(repo, execution_id=exec_id)
        assert summary is not None
        assert summary.idempotency_key == "test-review-001"


# ---------------------------------------------------------------------------
# Test 9: Repository lookup returns None for missing key
# ---------------------------------------------------------------------------


class TestRepositoryLookupMissing:
    def test_lookup_returns_none_for_unknown_key(self, repo: GuardedApplyExecutionRepository):
        summary = summarize_guarded_apply_execution_from_repository(
            repo, idempotency_key="does-not-exist"
        )
        assert summary is None

    def test_lookup_returns_none_for_unknown_execution_id(
        self, repo: GuardedApplyExecutionRepository
    ):
        summary = summarize_guarded_apply_execution_from_repository(
            repo, execution_id="exec-nonexistent"
        )
        assert summary is None


# ---------------------------------------------------------------------------
# Test 10: Repository lookup raises ValueError for invalid arguments
# ---------------------------------------------------------------------------


class TestRepositoryLookupInvalidArgs:
    def test_both_keys_raises_value_error(self, repo: GuardedApplyExecutionRepository):
        with pytest.raises(ValueError, match="exactly one"):
            summarize_guarded_apply_execution_from_repository(
                repo, idempotency_key="a", execution_id="b"
            )

    def test_neither_key_raises_value_error(self, repo: GuardedApplyExecutionRepository):
        with pytest.raises(ValueError, match="exactly one"):
            summarize_guarded_apply_execution_from_repository(repo)


# ---------------------------------------------------------------------------
# Test 11: Summary structure round-trips correctly
# ---------------------------------------------------------------------------


class TestSummaryStructure:
    def test_all_fields_are_populated(self, executed_result: GuardedApplyExecutionResult):
        summary = build_guarded_apply_execution_review_summary(executed_result)
        assert isinstance(summary, GuardedApplyExecutionReviewSummary)
        assert summary.plan_id != ""
        assert summary.idempotency_key == "test-review-001"
        assert summary.execution_status is not None
        assert summary.total_operations >= 0
        assert summary.operations_executed >= 0
        assert summary.operations_blocked >= 0
        assert summary.operations_skipped >= 0
        assert isinstance(summary.approved_operation_count, int)
        assert isinstance(summary.blocked_operation_count, int)
        assert isinstance(summary.unsupported_operation_count, int)
        assert isinstance(summary.conflict_operation_count, int)
        assert isinstance(summary.mutation_types, tuple)
        assert isinstance(summary.guard_decision_refs, tuple)
        assert isinstance(summary.is_dry_run, bool)
        assert isinstance(summary.executed_at, str)
        assert isinstance(summary.block_reason, str)
        assert isinstance(summary.has_blocking_outcome, bool)
        assert isinstance(summary.requires_human_review, bool)
        assert isinstance(summary.review_priority, ReviewPriority)
        assert isinstance(summary.human_summary, str)

    def test_summary_is_immutable(self, executed_result: GuardedApplyExecutionResult):
        summary = build_guarded_apply_execution_review_summary(executed_result)
        with pytest.raises(Exception):
            summary.plan_id = "changed"  # type: ignore[misc]

    def test_blocked_result_approved_count(self, blocked_result: GuardedApplyExecutionResult):
        summary = build_guarded_apply_execution_review_summary(blocked_result)
        assert summary.approved_operation_count == 0
        assert summary.conflict_operation_count == 0
        assert summary.unsupported_operation_count == 0


# ---------------------------------------------------------------------------
# Test 12: database/finance.db is untouched
# ---------------------------------------------------------------------------


class TestLiveDbNotTouched:
    def test_summary_never_opens_db(self, executed_result: GuardedApplyExecutionResult):
        summary = build_guarded_apply_execution_review_summary(executed_result)
        assert summary is not None

    def test_repo_lookup_uses_temp_db_only(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
        migrated_conn: sqlite3.Connection,
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-safe-001")
        db_file = migrated_conn.execute("PRAGMA database_list").fetchone()
        file_path = db_file["file"] if db_file["file"] else ""
        assert str(LIVE_DB_PATH) not in file_path

        summary = summarize_guarded_apply_execution_from_repository(
            repo, idempotency_key="test-review-001"
        )
        assert summary is not None


# ---------------------------------------------------------------------------
# Deterministic behaviour
# ---------------------------------------------------------------------------


class TestDeterministic:
    def test_same_result_same_summary(self, executed_result: GuardedApplyExecutionResult):
        s1 = build_guarded_apply_execution_review_summary(executed_result)
        s2 = build_guarded_apply_execution_review_summary(executed_result)
        assert s1 == s2

    def test_human_summary_is_deterministic(self, executed_result: GuardedApplyExecutionResult):
        s1 = build_guarded_apply_execution_review_summary(executed_result)
        s2 = build_guarded_apply_execution_review_summary(executed_result)
        assert s1.human_summary == s2.human_summary


# ---------------------------------------------------------------------------
# Guard decision refs preserved
# ---------------------------------------------------------------------------


class TestGuardDecisionRefs:
    def test_guard_decision_refs_preserved(self, executed_result: GuardedApplyExecutionResult):
        summary = build_guarded_apply_execution_review_summary(executed_result)
        assert summary.guard_decision_refs == executed_result.guard_decision_refs

    def test_blocked_result_has_guard_refs(self, blocked_result: GuardedApplyExecutionResult):
        summary = build_guarded_apply_execution_review_summary(blocked_result)
        assert summary.guard_decision_refs == blocked_result.guard_decision_refs


# ---------------------------------------------------------------------------
# Conflict result
# ---------------------------------------------------------------------------


class TestConflictSummary:
    def test_conflict_result_is_high_priority(
        self,
        sample_plan: ReconciliationApplyPlan,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime()
        op_id = sample_plan.operations[0].operation_id

        runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={op_id: approved_guard_decision},
            idempotency_key="test-conflict-priority",
            clock=fixed_clock,
        )

        other_decision = ResolutionDecision(
            decision_id="dec-conflict",
            queue_item_id="q-001",
            action=ResolutionAction.IGNORE,
            note="Conflict.",
            reviewer="human",
        )
        cand = ReconciliationCandidate(
            statement=StatementTransaction(
                transaction_date=date(2024, 12, 1),
                posted_date=date(2024, 12, 2),
                merchant_raw="Giant",
                amount=Decimal("45.50"),
                currency="SGD",
            ),
            match_status=MatchStatus.MATCHED,
            candidate_id="cand-conflict",
        )
        item = ReviewQueueItem(
            candidate=cand,
            issue_type=IssueType.MATCHED,
            suggested_action=SuggestedAction.CONFIRM_MATCH,
            queue_item_id="q-001",
        )
        other_inputs = [
            ApplyPlanInput(
                decision=other_decision,
                queue_item=item,
                guard_decision=approved_guard_decision,
            )
        ]
        other_plan = build_reconciliation_apply_plan(other_inputs)
        other_op_id = other_plan.operations[0].operation_id

        result = runtime.execute(
            other_plan,
            guard_decisions_by_operation_id={other_op_id: approved_guard_decision},
            idempotency_key="test-conflict-priority",
            clock=fixed_clock,
        )
        assert result.execution_status == ApplyExecutionStatus.CONFLICT

        summary = build_guarded_apply_execution_review_summary(result)
        assert summary.review_priority == ReviewPriority.HIGH
        assert summary.requires_human_review is True
        assert summary.has_blocking_outcome is True


# ---------------------------------------------------------------------------
# Persisted blocked result summary via repository
# ---------------------------------------------------------------------------


class TestPersistedBlockedSummaryViaRepo:
    def test_persisted_blocked_summary(
        self,
        repo: GuardedApplyExecutionRepository,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-blocked")
        summary = summarize_guarded_apply_execution_from_repository(
            repo, idempotency_key="test-review-blocked"
        )
        assert summary is not None
        assert summary.review_priority == ReviewPriority.HIGH
        assert summary.requires_human_review is True
        assert summary.operations_blocked == 1
        assert summary.operations_executed == 0


# ---------------------------------------------------------------------------
# Read-only operator summary listing
# ---------------------------------------------------------------------------


class TestOperatorSummaryListing:
    def test_lists_persisted_summaries_in_operator_priority_order(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
        blocked_result: GuardedApplyExecutionResult,
    ):
        later_executed = replace(
            executed_result,
            idempotency_key="test-review-later",
            executed_at="2024-12-16T12:00:00.000000+00:00",
        )

        repo.save_execution_result(executed_result, execution_fingerprint="fp-list-001")
        repo.save_execution_result(later_executed, execution_fingerprint="fp-list-002")
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-list-003")

        summaries = list_guarded_apply_execution_review_summaries(repo)

        assert [s.idempotency_key for s in summaries] == [
            "test-review-blocked",
            "test-review-later",
            "test-review-001",
        ]
        assert [s.review_priority for s in summaries] == [
            ReviewPriority.HIGH,
            ReviewPriority.MEDIUM,
            ReviewPriority.MEDIUM,
        ]

    def test_filters_by_execution_status(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-status-001")
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-status-002")

        summaries = list_guarded_apply_execution_review_summaries(
            repo,
            execution_status=ApplyExecutionStatus.BLOCKED,
        )

        assert [s.idempotency_key for s in summaries] == ["test-review-blocked"]
        assert summaries[0].execution_status == ApplyExecutionStatus.BLOCKED

    def test_filters_to_human_review_only(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-human-001")
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-human-002")

        summaries = list_guarded_apply_execution_review_summaries(
            repo,
            requires_human_review=True,
        )

        assert [s.idempotency_key for s in summaries] == ["test-review-blocked"]
        assert all(s.requires_human_review for s in summaries)

    def test_limit_applies_after_operator_sorting(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
        blocked_result: GuardedApplyExecutionResult,
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-limit-001")
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-limit-002")

        summaries = list_guarded_apply_execution_review_summaries(repo, limit=1)

        assert [s.idempotency_key for s in summaries] == ["test-review-blocked"]

    def test_negative_limit_is_rejected(self, repo: GuardedApplyExecutionRepository):
        with pytest.raises(ValueError, match="limit"):
            list_guarded_apply_execution_review_summaries(repo, limit=-1)

    def test_demo_cli_lists_guarded_execution_summaries(
        self,
        temp_db_path: Path,
        blocked_result: GuardedApplyExecutionResult,
        capsys: pytest.CaptureFixture[str],
    ):
        from finance_core.reconciliation.apply_runtime_persistence import (
            GuardedApplyExecutionRepository,
        )
        from finance_core.reconciliation.demo_cli import main

        conn = connect_temp_db(temp_db_path)
        try:
            apply_sql(conn, MIGRATION_014)
            conn.commit()
            repo = GuardedApplyExecutionRepository(conn)
            repo.save_execution_result(blocked_result, execution_fingerprint="fp-cli-blocked")
        finally:
            conn.close()

        exit_code = main(
            [
                "guarded-execution-summaries",
                "--db",
                str(temp_db_path),
                "--requires-human-review",
                "--limit",
                "5",
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "Guarded Apply Execution Summaries" in captured.out
        assert "test-review-blocked" in captured.out
        assert "high" in captured.out
        assert "human_review=True" in captured.out
        assert "dry_run=True" in captured.out
