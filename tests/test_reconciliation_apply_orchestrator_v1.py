"""Tests for Reconciliation Apply Orchestrator v1.

Covers:
  1. Happy path: orchestration coordinates existing apply behavior and returns
     a COMPLETED result with a review summary.
  2. Blocked guard outcome requires review and does not pretend success.
  3. Partially blocked operation requires review.
  4. Persistence path is called only when a repository is supplied.
  5. No-persistence / dry-run path when no repository is supplied.
  6. Idempotency / duplicate-protection behavior is preserved (replay returns
     the same result; conflicting replay surfaces CONFLICT, not success).
  7. Review summary is included and derivable from the orchestration result.
  8. Orchestrator does not mutate final transactions when the guard blocks
     execution (and never calls the final mutation workflow).
  9. Operation-level statuses are not lost in the aggregated result.
 10. Invalid input (empty key, empty plan inputs) raises ValueError.
 11. database/finance.db is never opened or modified.
 12. Determinism: same inputs produce the same orchestration result.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_core.reconciliation.apply_orchestrator import (
    ApplyOrchestrationInput,
    ApplyOrchestrationResult,
    ApplyOrchestrationStatus,
    orchestrate_reconciliation_apply,
)
from finance_core.reconciliation.apply_plan import ApplyPlanInput
from finance_core.reconciliation.apply_runtime import GuardedApplyRuntime
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
    """An approved NO_FINAL_MUTATION guard decision (confirm_match path)."""
    proposal = FinalMutationProposal(
        proposal_id="fp-001",
        action=FinalMutationAction.NO_FINAL_MUTATION,
        evidence_refs=("ev-abc",),
    )
    return FinalMutationGuard().evaluate(proposal)


@pytest.fixture
def blocked_guard_decision() -> FinalMutationGuardDecision:
    """A blocked CREATE guard decision (missing merchant + source + evidence)."""
    proposal = FinalMutationProposal(
        proposal_id="fp-002",
        action=FinalMutationAction.CREATE_FINAL_TRANSACTION,
        amount=Decimal("100.00"),
        currency="SGD",
        merchant="",
        transaction_date=date(2024, 12, 1),
    )
    return FinalMutationGuard().evaluate(proposal)


@pytest.fixture
def happy_input(
    sample_decision: ResolutionDecision,
    queue_item: ReviewQueueItem,
    approved_guard_decision: FinalMutationGuardDecision,
) -> ApplyOrchestrationInput:
    """A single-operation, guard-approved orchestration input."""
    plan_input = ApplyPlanInput(
        decision=sample_decision,
        queue_item=queue_item,
        guard_decision=approved_guard_decision,
    )
    # Operation id emitted by the plan builder is "op-<decision_id>".
    op_id = "op-dec-001"
    return ApplyOrchestrationInput(
        plan_inputs=(plan_input,),
        guard_decisions_by_operation_id={op_id: approved_guard_decision},
        idempotency_key="orch-happy-001",
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
# Test 1: Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_completed_status_on_guard_approved_execution(
        self, happy_input: ApplyOrchestrationInput, fixed_clock
    ):
        result = orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)
        assert isinstance(result, ApplyOrchestrationResult)
        assert result.status is ApplyOrchestrationStatus.COMPLETED
        assert result.execution_result.execution_status is ApplyExecutionStatus.EXECUTED

    def test_review_summary_included_and_consistent(
        self, happy_input: ApplyOrchestrationInput, fixed_clock
    ):
        result = orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)
        assert result.review_summary is not None
        assert result.review_summary.idempotency_key == happy_input.idempotency_key
        assert result.review_summary.execution_status is ApplyExecutionStatus.EXECUTED
        assert result.review_summary.requires_human_review is False

    def test_plan_is_dry_run(self, happy_input: ApplyOrchestrationInput, fixed_clock):
        result = orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)
        assert result.plan.is_dry_run is True
        assert result.plan.total_operations == 1
        assert result.plan.approved_operations == 1
        assert result.plan.blocked_operations == 0

    def test_no_persistence_by_default(self, happy_input: ApplyOrchestrationInput, fixed_clock):
        result = orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)
        assert result.persistence_enabled is False
        assert result.persisted is False

    def test_orchestration_note_mentions_dry_run(
        self, happy_input: ApplyOrchestrationInput, fixed_clock
    ):
        result = orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)
        assert "completed" in result.orchestration_note
        assert "no final financial records" in result.orchestration_note.lower()

    def test_idempotency_key_echoed(self, happy_input: ApplyOrchestrationInput, fixed_clock):
        result = orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)
        assert result.idempotency_key == "orch-happy-001"


# ---------------------------------------------------------------------------
# Test 2: Blocked guard outcome requires review, never success
# ---------------------------------------------------------------------------


class TestBlockedGuardOutcome:
    def test_blocked_status_not_success(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        plan_input = ApplyPlanInput(
            decision=sample_decision,
            queue_item=queue_item,
            guard_decision=blocked_guard_decision,
        )
        op_id = "op-dec-001"
        orch_input = ApplyOrchestrationInput(
            plan_inputs=(plan_input,),
            guard_decisions_by_operation_id={op_id: blocked_guard_decision},
            idempotency_key="orch-blocked-001",
        )
        result = orchestrate_reconciliation_apply(orch_input, clock=fixed_clock)

        assert result.status is ApplyOrchestrationStatus.BLOCKED
        assert result.status is not ApplyOrchestrationStatus.COMPLETED
        assert result.execution_result.execution_status is ApplyExecutionStatus.BLOCKED

    def test_blocked_requires_human_review(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        plan_input = ApplyPlanInput(
            decision=sample_decision,
            queue_item=queue_item,
            guard_decision=blocked_guard_decision,
        )
        op_id = "op-dec-001"
        orch_input = ApplyOrchestrationInput(
            plan_inputs=(plan_input,),
            guard_decisions_by_operation_id={op_id: blocked_guard_decision},
            idempotency_key="orch-blocked-review",
        )
        result = orchestrate_reconciliation_apply(orch_input, clock=fixed_clock)

        assert result.review_summary.requires_human_review is True
        assert result.review_summary.review_priority is ReviewPriority.HIGH
        assert result.review_summary.has_blocking_outcome is True

    def test_blocked_does_not_pretend_success_in_note(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        plan_input = ApplyPlanInput(
            decision=sample_decision,
            queue_item=queue_item,
            guard_decision=blocked_guard_decision,
        )
        op_id = "op-dec-001"
        orch_input = ApplyOrchestrationInput(
            plan_inputs=(plan_input,),
            guard_decisions_by_operation_id={op_id: blocked_guard_decision},
            idempotency_key="orch-blocked-note",
        )
        result = orchestrate_reconciliation_apply(orch_input, clock=fixed_clock)
        assert "blocked" in result.orchestration_note.lower()
        assert "completed" not in result.orchestration_note.lower()


# ---------------------------------------------------------------------------
# Test 3: Partially blocked operation requires review
# ---------------------------------------------------------------------------


class TestPartiallyBlocked:
    def _build_partial_input(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        blocked_guard_decision: FinalMutationGuardDecision,
    ) -> ApplyOrchestrationInput:
        other_decision = ResolutionDecision(
            decision_id="dec-002",
            queue_item_id="q-001",
            action=ResolutionAction.CONFIRM_MATCH,
            note="Second decision.",
            reviewer="human",
        )
        inputs = (
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
        )
        gd_map = {
            "op-dec-001": approved_guard_decision,
            "op-dec-002": blocked_guard_decision,
        }
        return ApplyOrchestrationInput(
            plan_inputs=inputs,
            guard_decisions_by_operation_id=gd_map,
            idempotency_key="orch-partial-001",
        )

    def test_partially_blocked_status(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_input = self._build_partial_input(
            sample_decision, queue_item, approved_guard_decision, blocked_guard_decision
        )
        result = orchestrate_reconciliation_apply(orch_input, clock=fixed_clock)

        assert result.status is ApplyOrchestrationStatus.PARTIALLY_BLOCKED
        assert result.execution_result.execution_status is ApplyExecutionStatus.PARTIALLY_BLOCKED

    def test_partially_blocked_requires_review(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_input = self._build_partial_input(
            sample_decision, queue_item, approved_guard_decision, blocked_guard_decision
        )
        result = orchestrate_reconciliation_apply(orch_input, clock=fixed_clock)
        assert result.review_summary.requires_human_review is True
        assert result.review_summary.review_priority is ReviewPriority.HIGH
        assert result.review_summary.operations_executed == 1
        assert result.review_summary.operations_blocked == 1

    def test_partially_blocked_not_completed(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_input = self._build_partial_input(
            sample_decision, queue_item, approved_guard_decision, blocked_guard_decision
        )
        result = orchestrate_reconciliation_apply(orch_input, clock=fixed_clock)
        assert result.status is not ApplyOrchestrationStatus.COMPLETED


# ---------------------------------------------------------------------------
# Test 4: Persistence path is called only when a repository is supplied
# ---------------------------------------------------------------------------


class TestPersistenceBoundary:
    def test_no_repository_means_no_persistence(
        self, happy_input: ApplyOrchestrationInput, fixed_clock
    ):
        result = orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)
        assert result.persistence_enabled is False
        assert result.persisted is False

    def test_repository_persists_execution_result(
        self,
        happy_input: ApplyOrchestrationInput,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        result = orchestrate_reconciliation_apply(happy_input, repository=repo, clock=fixed_clock)
        assert result.persistence_enabled is True
        assert result.persisted is True

        # The execution result is durably retrievable by the idempotency key.
        cached = repo.get_by_idempotency_key(happy_input.idempotency_key)
        assert cached is not None
        assert cached.execution_status is ApplyExecutionStatus.EXECUTED

    def test_persistence_does_not_touch_live_db(
        self,
        happy_input: ApplyOrchestrationInput,
        migrated_conn: sqlite3.Connection,
        fixed_clock,
    ):
        repo = GuardedApplyExecutionRepository(migrated_conn)
        orchestrate_reconciliation_apply(happy_input, repository=repo, clock=fixed_clock)

        db_file = migrated_conn.execute("PRAGMA database_list").fetchone()
        file_path = db_file["file"] if db_file["file"] else ""
        assert str(LIVE_DB_PATH) not in file_path


# ---------------------------------------------------------------------------
# Test 5: Idempotency / duplicate-protection behavior preserved
# ---------------------------------------------------------------------------


class TestIdempotencyPreserved:
    def test_replay_with_same_key_returns_same_result(
        self,
        happy_input: ApplyOrchestrationInput,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        first = orchestrate_reconciliation_apply(happy_input, repository=repo, clock=fixed_clock)
        second = orchestrate_reconciliation_apply(happy_input, repository=repo, clock=fixed_clock)
        assert first.status is ApplyOrchestrationStatus.COMPLETED
        assert second.status is ApplyOrchestrationStatus.COMPLETED
        # Idempotent replay: the repository does not insert a new row.
        assert first.persisted is True
        assert second.persisted is False
        assert second.persistence_enabled is True

    def test_conflicting_replay_surfaces_conflict_not_success(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        first_input = ApplyOrchestrationInput(
            plan_inputs=(
                ApplyPlanInput(
                    decision=sample_decision,
                    queue_item=queue_item,
                    guard_decision=approved_guard_decision,
                ),
            ),
            guard_decisions_by_operation_id={"op-dec-001": approved_guard_decision},
            idempotency_key="orch-conflict-key",
        )
        first = orchestrate_reconciliation_apply(first_input, repository=repo, clock=fixed_clock)
        assert first.status is ApplyOrchestrationStatus.COMPLETED

        # Build a materially different plan that reuses the same idempotency key.
        other_decision = ResolutionDecision(
            decision_id="dec-other",
            queue_item_id="q-001",
            action=ResolutionAction.MARK_STATEMENT_ONLY,
            note="Different action.",
            reviewer="human",
        )
        other_input = ApplyOrchestrationInput(
            plan_inputs=(
                ApplyPlanInput(
                    decision=other_decision,
                    queue_item=queue_item,
                    guard_decision=approved_guard_decision,
                ),
            ),
            guard_decisions_by_operation_id={"op-dec-other": approved_guard_decision},
            idempotency_key="orch-conflict-key",
        )
        second = orchestrate_reconciliation_apply(other_input, repository=repo, clock=fixed_clock)
        assert second.status is ApplyOrchestrationStatus.CONFLICT
        assert second.status is not ApplyOrchestrationStatus.COMPLETED
        assert second.persisted is False
        assert second.review_summary.requires_human_review is True

    def test_in_memory_runtime_idempotency_without_repo(
        self, happy_input: ApplyOrchestrationInput, fixed_clock
    ):
        # Without a repository, a fresh runtime per call has no cross-call
        # memory; but a shared caller-supplied runtime preserves in-memory
        # idempotency. The orchestrator must honor a caller-supplied runtime.
        runtime = GuardedApplyRuntime()
        first = orchestrate_reconciliation_apply(happy_input, runtime=runtime, clock=fixed_clock)
        second = orchestrate_reconciliation_apply(happy_input, runtime=runtime, clock=fixed_clock)
        assert first.status is ApplyOrchestrationStatus.COMPLETED
        assert second.status is ApplyOrchestrationStatus.COMPLETED
        # Shared in-memory runtime: second call is an idempotent replay.
        assert second.execution_result is first.execution_result
        assert second.persistence_enabled is False


# ---------------------------------------------------------------------------
# Test 6: Review summary is included and derivable
# ---------------------------------------------------------------------------


class TestReviewSummaryIncluded:
    def test_summary_matches_direct_build(self, happy_input: ApplyOrchestrationInput, fixed_clock):
        from finance_core.reconciliation.apply_execution_review import (
            build_guarded_apply_execution_review_summary,
        )

        result = orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)
        direct = build_guarded_apply_execution_review_summary(result.execution_result)
        assert result.review_summary == direct

    def test_blocked_summary_carries_block_reason(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        plan_input = ApplyPlanInput(
            decision=sample_decision,
            queue_item=queue_item,
            guard_decision=blocked_guard_decision,
        )
        orch_input = ApplyOrchestrationInput(
            plan_inputs=(plan_input,),
            guard_decisions_by_operation_id={"op-dec-001": blocked_guard_decision},
            idempotency_key="orch-summary-blocked",
        )
        result = orchestrate_reconciliation_apply(orch_input, clock=fixed_clock)
        assert "blocked" in result.review_summary.human_summary.lower()
        assert result.review_summary.block_reason != ""


# ---------------------------------------------------------------------------
# Test 7: No final transaction mutation when guard blocks
# ---------------------------------------------------------------------------


class TestNoFinalMutationOnBlock:
    def test_blocked_result_has_no_final_mutation(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        blocked_guard_decision: FinalMutationGuardDecision,
        migrated_conn: sqlite3.Connection,
        fixed_clock,
    ):
        # The final-mutation workflow writes to these tables when it executes.
        # The orchestrator must never create them, even when persistence is on.
        repo = GuardedApplyExecutionRepository(migrated_conn)
        plan_input = ApplyPlanInput(
            decision=sample_decision,
            queue_item=queue_item,
            guard_decision=blocked_guard_decision,
        )
        orch_input = ApplyOrchestrationInput(
            plan_inputs=(plan_input,),
            guard_decisions_by_operation_id={"op-dec-001": blocked_guard_decision},
            idempotency_key="orch-no-mut-block",
        )
        result = orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        assert result.status is ApplyOrchestrationStatus.BLOCKED

        tables = {
            row[0]
            for row in migrated_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "reconciliation_final_mutation_transactions" not in tables

    def test_completed_result_still_dry_run_no_final_mutation(
        self,
        happy_input: ApplyOrchestrationInput,
        migrated_conn: sqlite3.Connection,
        fixed_clock,
    ):
        repo = GuardedApplyExecutionRepository(migrated_conn)
        result = orchestrate_reconciliation_apply(happy_input, repository=repo, clock=fixed_clock)
        assert result.status is ApplyOrchestrationStatus.COMPLETED
        assert result.execution_result.is_dry_run is True

        tables = {
            row[0]
            for row in migrated_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "reconciliation_final_mutation_transactions" not in tables


# ---------------------------------------------------------------------------
# Test 8: Operation-level statuses are not lost
# ---------------------------------------------------------------------------


class TestOperationLevelStatusesPreserved:
    def test_per_operation_results_carried(
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
            note="Second.",
            reviewer="human",
        )
        orch_input = ApplyOrchestrationInput(
            plan_inputs=(
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
            ),
            guard_decisions_by_operation_id={
                "op-dec-001": approved_guard_decision,
                "op-dec-002": blocked_guard_decision,
            },
            idempotency_key="orch-oplevel-001",
        )
        result = orchestrate_reconciliation_apply(orch_input, clock=fixed_clock)

        op_results = result.execution_result.results
        assert len(op_results) == 2
        statuses = {r.operation_id: r.execution_status for r in op_results}
        assert statuses["op-dec-001"] is ApplyExecutionStatus.EXECUTED
        assert statuses["op-dec-002"] is ApplyExecutionStatus.BLOCKED

        # The review summary also preserves per-status counts.
        assert result.review_summary.operations_executed == 1
        assert result.review_summary.operations_blocked == 1

    def test_summary_counts_match_execution_result(
        self, happy_input: ApplyOrchestrationInput, fixed_clock
    ):
        result = orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)
        assert result.review_summary.total_operations == result.execution_result.total_operations
        assert (
            result.review_summary.operations_executed == result.execution_result.operated_executed
        )
        assert result.review_summary.operations_blocked == result.execution_result.operated_blocked


# ---------------------------------------------------------------------------
# Test 9: Invalid input raises ValueError
# ---------------------------------------------------------------------------


class TestInvalidInput:
    def test_empty_idempotency_key_raises(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
    ):
        plan_input = ApplyPlanInput(
            decision=sample_decision,
            queue_item=queue_item,
            guard_decision=approved_guard_decision,
        )
        orch_input = ApplyOrchestrationInput(
            plan_inputs=(plan_input,),
            guard_decisions_by_operation_id={"op-dec-001": approved_guard_decision},
            idempotency_key="",
        )
        with pytest.raises(ValueError, match="idempotency_key"):
            orchestrate_reconciliation_apply(orch_input)

    def test_empty_plan_inputs_raises(self, approved_guard_decision: FinalMutationGuardDecision):
        orch_input = ApplyOrchestrationInput(
            plan_inputs=(),
            guard_decisions_by_operation_id={},
            idempotency_key="orch-empty",
        )
        with pytest.raises(ValueError, match="plan input"):
            orchestrate_reconciliation_apply(orch_input)


# ---------------------------------------------------------------------------
# Test 10: database/finance.db is never opened or modified
# ---------------------------------------------------------------------------


class TestLiveDbNotTouched:
    def test_finance_db_hash_unchanged(self, happy_input: ApplyOrchestrationInput, fixed_clock):
        import hashlib

        def _sha(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        # The live DB file may not exist in a fresh CI checkout (it is not
        # committed). Guard the hash check with an existence test so the
        # assertion holds in both environments: when the file is absent, the
        # invariant is that the orchestrator never creates it; when present,
        # the invariant is that its bytes are unchanged. This matches the
        # ``.exists()``-guarded live-DB pattern used elsewhere in the suite.
        existed_before = LIVE_DB_PATH.exists()
        before = _sha(LIVE_DB_PATH) if existed_before else None
        orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)
        existed_after = LIVE_DB_PATH.exists()
        after = _sha(LIVE_DB_PATH) if existed_after else None

        if existed_before:
            assert existed_after, "orchestrator must not delete database/finance.db"
            assert before == after, "orchestrator must not modify database/finance.db"
        else:
            assert not existed_after, "orchestrator must not create database/finance.db"

    def test_persistence_uses_temp_db_only(
        self,
        happy_input: ApplyOrchestrationInput,
        repo: GuardedApplyExecutionRepository,
        migrated_conn: sqlite3.Connection,
        fixed_clock,
    ):
        orchestrate_reconciliation_apply(happy_input, repository=repo, clock=fixed_clock)
        db_file = migrated_conn.execute("PRAGMA database_list").fetchone()
        file_path = db_file["file"] if db_file["file"] else ""
        assert str(LIVE_DB_PATH) not in file_path


# ---------------------------------------------------------------------------
# Test 11: Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_inputs_same_result(self, happy_input: ApplyOrchestrationInput, fixed_clock):
        first = orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)
        second = orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)
        assert first.status == second.status
        assert first.orchestration_note == second.orchestration_note
        assert first.review_summary == second.review_summary
        assert first.execution_result.executed_at == second.execution_result.executed_at

    def test_result_is_immutable(self, happy_input: ApplyOrchestrationInput, fixed_clock):
        result = orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)
        with pytest.raises(Exception):
            result.status = ApplyOrchestrationStatus.BLOCKED  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test 12: Caller-supplied runtime is honored
# ---------------------------------------------------------------------------


class TestCallerSuppliedRuntime:
    def test_caller_runtime_used_as_is(self, happy_input: ApplyOrchestrationInput, fixed_clock):
        runtime = GuardedApplyRuntime()
        result = orchestrate_reconciliation_apply(happy_input, runtime=runtime, clock=fixed_clock)
        assert result.status is ApplyOrchestrationStatus.COMPLETED
        # Caller runtime has no repository -> persistence disabled.
        assert result.persistence_enabled is False
        # The same runtime instance now has the key cached in-memory.
        assert runtime.is_executed(happy_input.idempotency_key) is True

    def test_caller_runtime_with_repo_persists(
        self,
        happy_input: ApplyOrchestrationInput,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime(repository=repo)
        result = orchestrate_reconciliation_apply(happy_input, runtime=runtime, clock=fixed_clock)
        assert result.persistence_enabled is True
        assert result.persisted is True
        assert repo.has_idempotency_key(happy_input.idempotency_key) is True


# ---------------------------------------------------------------------------
# Test 13: Evidence refs and audit refs survive the orchestration path
# ---------------------------------------------------------------------------


class TestEvidenceRefsAndAuditRefs:
    def test_evidence_refs_survive_in_plan_operations(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        plan_input = ApplyPlanInput(
            decision=sample_decision,
            queue_item=queue_item,
            guard_decision=approved_guard_decision,
            evidence_refs=("ev-abc",),
        )
        orch_input = ApplyOrchestrationInput(
            plan_inputs=(plan_input,),
            guard_decisions_by_operation_id={"op-dec-001": approved_guard_decision},
            idempotency_key="orch-ev-refs-001",
        )
        result = orchestrate_reconciliation_apply(orch_input, clock=fixed_clock)
        op = result.plan.operations[0]
        assert "ev-abc" in op.evidence_refs

    def test_evidence_refs_visible_in_plan_operations_tuple(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        plan_input = ApplyPlanInput(
            decision=sample_decision,
            queue_item=queue_item,
            guard_decision=approved_guard_decision,
            evidence_refs=("ev-abc",),
        )
        orch_input = ApplyOrchestrationInput(
            plan_inputs=(plan_input,),
            guard_decisions_by_operation_id={"op-dec-001": approved_guard_decision},
            idempotency_key="orch-ev-refs-002",
        )
        result = orchestrate_reconciliation_apply(orch_input, clock=fixed_clock)
        assert len(result.plan.operations) == 1
        assert result.plan.operations[0].evidence_refs == ("ev-abc",)

    def test_guard_decision_refs_survive_in_execution_result(
        self, happy_input: ApplyOrchestrationInput, fixed_clock
    ):
        result = orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)
        assert len(result.execution_result.guard_decision_refs) == 1
        assert "fp-001" in result.execution_result.guard_decision_refs

    def test_guard_decision_refs_propagate_to_review_summary(
        self, happy_input: ApplyOrchestrationInput, fixed_clock
    ):
        result = orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)
        assert result.review_summary.guard_decision_refs == ("fp-001",)

    def test_audit_trail_populated_after_execution(
        self, happy_input: ApplyOrchestrationInput, fixed_clock
    ):
        result = orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)
        assert isinstance(result.execution_result.audit_trail, dict)

    def test_evidence_and_audit_refs_survive_with_persistence(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        plan_input = ApplyPlanInput(
            decision=sample_decision,
            queue_item=queue_item,
            guard_decision=approved_guard_decision,
            evidence_refs=("ev-abc",),
        )
        orch_input = ApplyOrchestrationInput(
            plan_inputs=(plan_input,),
            guard_decisions_by_operation_id={"op-dec-001": approved_guard_decision},
            idempotency_key="orch-ev-refs-persist",
        )
        result = orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        assert result.plan.operations[0].evidence_refs == ("ev-abc",)
        assert len(result.execution_result.guard_decision_refs) == 1
        assert "fp-001" in result.execution_result.guard_decision_refs
        cached = repo.get_by_idempotency_key(orch_input.idempotency_key)
        assert cached is not None
        assert cached.guard_decision_refs == ("fp-001",)
        assert isinstance(cached.audit_trail, dict)

    def test_evidence_refs_are_deterministic(
        self, happy_input: ApplyOrchestrationInput, fixed_clock
    ):
        first = orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)
        second = orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)
        assert first.plan.operations[0].evidence_refs == second.plan.operations[0].evidence_refs
        assert (
            first.execution_result.guard_decision_refs
            == second.execution_result.guard_decision_refs
        )
        assert first.review_summary.guard_decision_refs == second.review_summary.guard_decision_refs

    def test_evidence_refs_empty_when_no_proposal(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        plan_input = ApplyPlanInput(
            decision=sample_decision,
            queue_item=queue_item,
            guard_decision=approved_guard_decision,
        )
        orch_input = ApplyOrchestrationInput(
            plan_inputs=(plan_input,),
            guard_decisions_by_operation_id={"op-dec-001": approved_guard_decision},
            idempotency_key="orch-no-evidence",
        )
        result = orchestrate_reconciliation_apply(orch_input, clock=fixed_clock)
        assert result.plan.operations[0].evidence_refs == ()
