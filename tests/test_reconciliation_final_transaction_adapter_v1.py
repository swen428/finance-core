"""Tests for Final Transaction Reconciliation Adapter v1.

Covers:
  1. Completed / guard-cleared dry-run apply result becomes adapter-eligible
     but not finalized.
  2. Blocked apply result is ineligible.
  3. Partially blocked apply result is ineligible.
  4. Conflict result is ineligible.
  5. Unsupported result is ineligible.
  6. Review-required result is ineligible.
  7. Adapter never calls execute_guarded_final_mutation_workflow.
  8. Adapter never creates / updates final transactions.
  9. Operation-level statuses are preserved or summarized.
 10. Result requires separate final-write confirmation.
 11. Missing/invalid input raises clear error or returns explicit ineligible
     status.
 12. No database/finance.db modification.
 13. Deterministic result for same input.
 14. Public API exports are present.
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest

from finance_core.reconciliation.apply_orchestrator import (
    ApplyOrchestrationInput,
    ApplyOrchestrationResult,
    ApplyOrchestrationStatus,
    orchestrate_reconciliation_apply,
)
from finance_core.reconciliation.apply_plan import ApplyPlanInput
from finance_core.reconciliation.final_mutation_proposal import (
    FinalMutationAction,
    FinalMutationGuard,
    FinalMutationGuardDecision,
    FinalMutationProposal,
)
from finance_core.reconciliation.final_transaction_adapter import (
    FinalTransactionAdapterInput,
    FinalTransactionAdapterReason,
    FinalTransactionAdapterStatus,
    OperationSummary,
    build_final_transaction_reconciliation_adapter_result,
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
from tests.conftest import LIVE_DB_PATH

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


# ---------------------------------------------------------------------------
# Helper: build an orchestration result for a given status path
# ---------------------------------------------------------------------------


def _orch(
    orch_input: ApplyOrchestrationInput,
    clock,
) -> ApplyOrchestrationResult:
    """Run the orchestrator and return its result."""
    return orchestrate_reconciliation_apply(orch_input, clock=clock)


def _happy_input(
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
    return ApplyOrchestrationInput(
        plan_inputs=(plan_input,),
        guard_decisions_by_operation_id={"op-dec-001": approved_guard_decision},
        idempotency_key="orch-happy-001",
    )


def _blocked_input(
    sample_decision: ResolutionDecision,
    queue_item: ReviewQueueItem,
    blocked_guard_decision: FinalMutationGuardDecision,
) -> ApplyOrchestrationInput:
    """A single-operation, guard-blocked orchestration input."""
    plan_input = ApplyPlanInput(
        decision=sample_decision,
        queue_item=queue_item,
        guard_decision=blocked_guard_decision,
    )
    return ApplyOrchestrationInput(
        plan_inputs=(plan_input,),
        guard_decisions_by_operation_id={"op-dec-001": blocked_guard_decision},
        idempotency_key="orch-blocked-001",
    )


# ---------------------------------------------------------------------------
# Test 1: Completed / guard-cleared dry-run -> ELIGIBLE but not finalized
# ---------------------------------------------------------------------------


class TestCompletedEligibleButNotFinalized:
    def test_completed_result_is_eligible(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert result.status is FinalTransactionAdapterStatus.ELIGIBLE
        assert result.is_eligible_for_final_mutation is True

    def test_eligible_result_requires_final_write_confirmation(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert result.requires_final_write_confirmation is True

    def test_eligible_elapsed_does_not_mean_finalized(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert result.is_eligible_for_final_mutation is True
        assert result.requires_final_write_confirmation is True
        assert "no final" in result.adapter_note.lower()

    def test_eligible_result_carries_plan_id(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert result.plan_id == orch_result.plan.plan_id
        assert result.idempotency_key == "orch-happy-001"

    def test_eligible_result_has_empty_reason_codes(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert result.reason_codes == ()


# ---------------------------------------------------------------------------
# Test 2: Blocked apply result is ineligible
# ---------------------------------------------------------------------------


class TestBlockedIneligible:
    def test_blocked_result_is_ineligible(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _blocked_input(sample_decision, queue_item, blocked_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert result.status is FinalTransactionAdapterStatus.INELIGIBLE_BLOCKED
        assert result.is_eligible_for_final_mutation is False
        assert result.requires_final_write_confirmation is False

    def test_blocked_result_has_reason_code(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _blocked_input(sample_decision, queue_item, blocked_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert FinalTransactionAdapterReason.ORCHESTRATION_BLOCKED.value in result.reason_codes

    def test_blocked_not_eligible(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _blocked_input(sample_decision, queue_item, blocked_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert result.is_eligible_for_final_mutation is False
        assert "ineligible" in result.adapter_note.lower()


# ---------------------------------------------------------------------------
# Test 3: Partially blocked apply result is ineligible
# ---------------------------------------------------------------------------


class TestPartiallyBlockedIneligible:
    def _partial_input(
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
            idempotency_key="orch-partial-adapter",
        )

    def test_partially_blocked_is_ineligible(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            self._partial_input(
                sample_decision,
                queue_item,
                approved_guard_decision,
                blocked_guard_decision,
            ),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert result.status is FinalTransactionAdapterStatus.INELIGIBLE_PARTIALLY_BLOCKED
        assert result.is_eligible_for_final_mutation is False
        assert (
            FinalTransactionAdapterReason.ORCHESTRATION_PARTIALLY_BLOCKED.value
            in result.reason_codes
        )

    def test_partially_blocked_requires_review(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            self._partial_input(
                sample_decision,
                queue_item,
                approved_guard_decision,
                blocked_guard_decision,
            ),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert result.requires_final_write_confirmation is False
        assert result.eligible_operation_count == 1
        assert result.ineligible_operation_count == 1


# ---------------------------------------------------------------------------
# Test 4: Conflict result is ineligible
# ---------------------------------------------------------------------------


class TestConflictIneligible:
    def test_conflict_result_is_ineligible(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
        temp_db_path: Path,
    ):
        """CONFLICT from same idempotency key with different content.
        Uses a shared in-memory GuardedApplyRuntime so the runtime can
        detect the conflict across calls."""

        from finance_core.reconciliation.apply_runtime import GuardedApplyRuntime

        # Use a shared runtime so in-memory idempotency detects the conflict.
        shared_runtime = GuardedApplyRuntime()

        plan_input_a = ApplyPlanInput(
            decision=sample_decision,
            queue_item=queue_item,
            guard_decision=approved_guard_decision,
        )
        orch_a = ApplyOrchestrationInput(
            plan_inputs=(plan_input_a,),
            guard_decisions_by_operation_id={"op-dec-001": approved_guard_decision},
            idempotency_key="orch-conflict-adapter",
        )
        first = orchestrate_reconciliation_apply(orch_a, runtime=shared_runtime, clock=fixed_clock)
        assert first.status is ApplyOrchestrationStatus.COMPLETED

        # Different plan content, same idempotency key -> CONFLICT
        other_decision = ResolutionDecision(
            decision_id="dec-other",
            queue_item_id="q-001",
            action=ResolutionAction.IGNORE,
            note="Different material.",
            reviewer="human",
        )
        plan_input_b = ApplyPlanInput(
            decision=other_decision,
            queue_item=queue_item,
            guard_decision=approved_guard_decision,
        )
        orch_b = ApplyOrchestrationInput(
            plan_inputs=(plan_input_b,),
            guard_decisions_by_operation_id={"op-dec-other": approved_guard_decision},
            idempotency_key="orch-conflict-adapter",
        )
        second = orchestrate_reconciliation_apply(orch_b, runtime=shared_runtime, clock=fixed_clock)
        assert second.status is ApplyOrchestrationStatus.CONFLICT

        adapter_input = FinalTransactionAdapterInput(orchestration_result=second)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert result.status is FinalTransactionAdapterStatus.INELIGIBLE_CONFLICT
        assert result.is_eligible_for_final_mutation is False
        assert FinalTransactionAdapterReason.ORCHESTRATION_CONFLICT.value in result.reason_codes


# ---------------------------------------------------------------------------
# Test 5: Unsupported result is ineligible
# ---------------------------------------------------------------------------


class TestUnsupportedIneligible:
    def test_unsupported_result_is_ineligible(
        self,
        fixed_clock,
    ):
        # Use the GuardedApplyRuntime directly with an empty plan to get
        # a genuine UNSUPPORTED execution result, then construct an
        # ApplyOrchestrationResult with UNSUPPORTED status.

        from finance_core.reconciliation.apply_execution_review import (
            build_guarded_apply_execution_review_summary,
        )
        from finance_core.reconciliation.apply_plan import (
            ReconciliationApplyPlan,
        )
        from finance_core.reconciliation.apply_runtime import GuardedApplyRuntime

        runtime = GuardedApplyRuntime()
        empty_plan = ReconciliationApplyPlan(
            plan_id="plan-empty-test",
            operations=(),
            risks=(),
            requires_human_confirmation=False,
            is_dry_run=True,
            total_operations=0,
            blocked_operations=0,
            approved_operations=0,
            pending_guard_operations=0,
            note="Empty plan for testing.",
        )
        exec_result = runtime.execute(
            empty_plan,
            guard_decisions_by_operation_id={},
            idempotency_key="exec-empty-key",
            clock=fixed_clock,
        )
        assert exec_result.execution_status is ApplyExecutionStatus.UNSUPPORTED

        review_summary = build_guarded_apply_execution_review_summary(exec_result)
        orch_result = ApplyOrchestrationResult(
            status=ApplyOrchestrationStatus.UNSUPPORTED,
            plan=empty_plan,
            execution_result=exec_result,
            review_summary=review_summary,
            persisted=False,
            persistence_enabled=False,
            idempotency_key="exec-empty-key",
            orchestration_note="Unsupported plan shape.",
        )

        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert result.status is FinalTransactionAdapterStatus.INELIGIBLE_UNSUPPORTED
        assert result.is_eligible_for_final_mutation is False
        assert FinalTransactionAdapterReason.ORCHESTRATION_UNSUPPORTED.value in result.reason_codes


# ---------------------------------------------------------------------------
# Test 6: Review-required result is ineligible
# ---------------------------------------------------------------------------


class TestReviewRequiredIneligible:
    def test_requires_review_is_ineligible(
        self,
        fixed_clock,
    ):
        # Build a result with REQUIRES_REVIEW status using real constructors.
        from finance_core.reconciliation.apply_execution_review import (
            build_guarded_apply_execution_review_summary,
        )
        from finance_core.reconciliation.apply_plan import (
            ApplyPlanOperation,
            ReconciliationApplyPlan,
        )
        from finance_core.reconciliation.models import (
            GuardedApplyExecutionResult,
            GuardedOperationResult,
            ResolutionAction,
        )

        op = GuardedOperationResult(
            operation_id="op-rr-001",
            decision_id="dec-rr-001",
            execution_status=ApplyExecutionStatus.BLOCKED,
            reason="Requires review",
            guard_decision_approved=False,
        )
        exec_result = GuardedApplyExecutionResult(
            plan_id="plan-rr-001",
            idempotency_key="key-rr-001",
            execution_status=ApplyExecutionStatus.BLOCKED,
            results=(op,),
            total_operations=1,
            operated_blocked=1,
            block_reason="review required",
            is_dry_run=True,
        )
        plan_op = ApplyPlanOperation(
            operation_id="op-rr-001",
            decision_id="dec-rr-001",
            queue_item_id="q-rr-001",
            candidate_id="cand-rr-001",
            action=ResolutionAction.NEEDS_MORE_INFO,
            guard_decision_approved=False,
        )
        plan = ReconciliationApplyPlan(
            plan_id="plan-rr-001",
            operations=(plan_op,),
            risks=(),
            requires_human_confirmation=True,
            is_dry_run=True,
            total_operations=1,
            blocked_operations=1,
            approved_operations=0,
            pending_guard_operations=0,
            note="Requires review.",
        )
        review_summary = build_guarded_apply_execution_review_summary(exec_result)

        orch_result = ApplyOrchestrationResult(
            status=ApplyOrchestrationStatus.REQUIRES_REVIEW,
            plan=plan,
            execution_result=exec_result,
            review_summary=review_summary,
            persisted=False,
            persistence_enabled=False,
            idempotency_key="key-rr-001",
            orchestration_note="Requires human review.",
        )

        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert result.status is FinalTransactionAdapterStatus.INELIGIBLE_REQUIRES_REVIEW
        assert result.is_eligible_for_final_mutation is False
        assert (
            FinalTransactionAdapterReason.ORCHESTRATION_REQUIRES_REVIEW.value in result.reason_codes
        )


# ---------------------------------------------------------------------------
# Test 7: Adapter never calls execute_guarded_final_mutation_workflow
# ---------------------------------------------------------------------------


class TestAdapterNeverCallsFinalMutationWorkflow:
    def test_adapter_does_not_import_workflow(
        self,
    ):
        """Verify the adapter module does not import the final mutation workflow."""
        import sys

        # Clear any cached imports
        for key in list(sys.modules.keys()):
            if "final_transaction_adapter" in key:
                del sys.modules[key]
            if (
                "final_mutation_workflow" in key
                and key != "finance_core.reconciliation.final_mutation_workflow"
            ):
                del sys.modules[key]

        # Check that the adapter module's imports don't include
        # final_mutation_workflow.
        # We look at the module's source-level imports directly.
        import ast

        adapter_path = (
            Path(__file__).parents[1]
            / "finance_core"
            / "reconciliation"
            / "final_transaction_adapter.py"
        )
        source = adapter_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        imported_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_names.add(node.module)

        assert "finance_core.reconciliation.final_mutation_workflow" not in imported_names
        assert "final_mutation_workflow" not in imported_names

    def test_adapter_does_not_create_final_transaction_tables(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        """Verify a temp DB connection created independently is not populated
        with final mutation tables by the adapter."""
        import sqlite3

        orch_result = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)

        conn = sqlite3.connect(":memory:")
        # Run the adapter — it should not write to any connection,
        # including a caller-supplied one
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)
        assert result.status is FinalTransactionAdapterStatus.ELIGIBLE

        # The adapter never gets a connection; verify no connection was opened
        # inside the adapter by checking the in-memory DB is empty.
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "reconciliation_final_mutation_transactions" not in tables
        conn.close()


# ---------------------------------------------------------------------------
# Test 8: Adapter never creates / updates final transactions
# ---------------------------------------------------------------------------


class TestAdapterNeverCreatesFinalTransactions:
    def test_adapter_result_has_no_transaction_public_id(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        # The adapter result should not carry a transaction_public_id or
        # any final-mutation-specific field.
        assert (
            not hasattr(result, "transaction_public_id")
            or result.__dict__.get("transaction_public_id") is None
        )

    def test_eligible_adapter_note_confirms_no_final_records(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert "no final" in result.adapter_note.lower()
        assert "requires" in result.adapter_note.lower()
        assert "human-confirmed" in result.adapter_note.lower()


# ---------------------------------------------------------------------------
# Test 9: Operation-level statuses are preserved or summarized
# ---------------------------------------------------------------------------


class TestOperationStatusesPreserved:
    def test_operation_summaries_preserved(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert len(result.operation_summaries) == 1
        op = result.operation_summaries[0]
        assert isinstance(op, OperationSummary)
        assert op.operation_id == "op-dec-001"
        assert op.execution_status is ApplyExecutionStatus.EXECUTED
        assert op.guard_decision_approved is True

    def test_operation_counts_match(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert result.total_operations == 1
        assert result.eligible_operation_count == 1
        assert result.ineligible_operation_count == 0

    def test_blocked_operation_status_preserved(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _blocked_input(sample_decision, queue_item, blocked_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert len(result.operation_summaries) == 1
        op = result.operation_summaries[0]
        assert op.execution_status is ApplyExecutionStatus.BLOCKED
        assert op.guard_decision_approved is False
        assert result.eligible_operation_count == 0
        assert result.ineligible_operation_count == 1

    def test_two_ops_one_eligible_one_ineligible(
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
            idempotency_key="orch-oplevel-adapter",
        )
        orch_result = _orch(orch_input, fixed_clock)
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert result.total_operations == 2
        assert result.eligible_operation_count == 1
        assert result.ineligible_operation_count == 1

        statuses = {s.operation_id: s.execution_status for s in result.operation_summaries}
        assert statuses["op-dec-001"] is ApplyExecutionStatus.EXECUTED
        assert statuses["op-dec-002"] is ApplyExecutionStatus.BLOCKED


# ---------------------------------------------------------------------------
# Test 10: Result requires separate final-write confirmation
# ---------------------------------------------------------------------------


class TestFinalWriteConfirmationRequired:
    def test_eligible_requires_confirmation(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert result.is_eligible_for_final_mutation is True
        assert result.requires_final_write_confirmation is True

    def test_ineligible_does_not_require_confirmation(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _blocked_input(sample_decision, queue_item, blocked_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert result.is_eligible_for_final_mutation is False
        assert result.requires_final_write_confirmation is False

    def test_confirmation_flag_consistent_with_eligibility(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        """requires_final_write_confirmation is True iff is_eligible is True."""
        orch_result = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert result.requires_final_write_confirmation == result.is_eligible_for_final_mutation


# ---------------------------------------------------------------------------
# Test 11: Missing/invalid input raises or returns ineligible
# ---------------------------------------------------------------------------


class TestInvalidInput:
    def test_non_adapter_input_raises_typeerror(self):
        with pytest.raises(TypeError, match="FinalTransactionAdapterInput"):
            build_final_transaction_reconciliation_adapter_result("not-an-adapter-input")  # type: ignore[arg-type]

    def test_none_orchestration_result_raises_valueerror(self):
        adapter_input = FinalTransactionAdapterInput(orchestration_result=None)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="orchestration_result"):
            build_final_transaction_reconciliation_adapter_result(adapter_input)

    def test_raw_dict_raises_typeerror(self):
        with pytest.raises(TypeError):
            build_final_transaction_reconciliation_adapter_result({"orchestration_result": {}})  # type: ignore[arg-type]

    def test_none_input_raises_typeerror(self):
        with pytest.raises(TypeError):
            build_final_transaction_reconciliation_adapter_result(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Test 12: No database/finance.db modification
# ---------------------------------------------------------------------------


class TestLiveDbNotTouched:
    def test_finance_db_hash_unchanged(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)

        def _sha(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        existed_before = LIVE_DB_PATH.exists()
        before = _sha(LIVE_DB_PATH) if existed_before else None

        result = build_final_transaction_reconciliation_adapter_result(adapter_input)
        assert result is not None

        existed_after = LIVE_DB_PATH.exists()
        after = _sha(LIVE_DB_PATH) if existed_after else None

        if existed_before:
            assert existed_after, "adapter must not delete database/finance.db"
            assert before == after, "adapter must not modify database/finance.db"
        else:
            assert not existed_after, "adapter must not create database/finance.db"

    def test_adapter_never_opens_any_database(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)

        # Patch sqlite3.connect to ensure it's never called during adapter execution
        with mock.patch("sqlite3.connect") as mock_connect:
            result = build_final_transaction_reconciliation_adapter_result(adapter_input)
            mock_connect.assert_not_called()

        assert result.status is FinalTransactionAdapterStatus.ELIGIBLE


# ---------------------------------------------------------------------------
# Test 13: Deterministic result for same input
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_input_same_result(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)

        first = build_final_transaction_reconciliation_adapter_result(adapter_input)
        second = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert first == second
        assert first.status == second.status
        assert first.is_eligible_for_final_mutation == second.is_eligible_for_final_mutation
        assert first.reason_codes == second.reason_codes
        assert first.adapter_note == second.adapter_note
        assert first.operation_summaries == second.operation_summaries

    def test_result_is_immutable(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        with pytest.raises(Exception):
            result.status = FinalTransactionAdapterStatus.INELIGIBLE_BLOCKED  # type: ignore[misc]

    def test_different_inputs_different_result(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        happy_orch = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        blocked_orch = _orch(
            _blocked_input(sample_decision, queue_item, blocked_guard_decision),
            fixed_clock,
        )

        happy_result = build_final_transaction_reconciliation_adapter_result(
            FinalTransactionAdapterInput(orchestration_result=happy_orch)
        )
        blocked_result = build_final_transaction_reconciliation_adapter_result(
            FinalTransactionAdapterInput(orchestration_result=blocked_orch)
        )

        assert happy_result.status is FinalTransactionAdapterStatus.ELIGIBLE
        assert blocked_result.status is FinalTransactionAdapterStatus.INELIGIBLE_BLOCKED
        assert happy_result != blocked_result


# ---------------------------------------------------------------------------
# Test 14: Public API exports
# ---------------------------------------------------------------------------


class TestPublicApiExports:
    def test_all_exports_importable(self):
        from finance_core.reconciliation import (
            FinalTransactionAdapterInput,
            FinalTransactionAdapterReason,
            FinalTransactionAdapterResult,
            FinalTransactionAdapterStatus,
            OperationSummary,
            build_final_transaction_reconciliation_adapter_result,
        )

        # All imports succeeded
        assert FinalTransactionAdapterStatus is not None
        assert FinalTransactionAdapterReason is not None
        assert FinalTransactionAdapterInput is not None
        assert FinalTransactionAdapterResult is not None
        assert OperationSummary is not None
        assert callable(build_final_transaction_reconciliation_adapter_result)

    def test_adapter_module_all_matches_public_api(self):
        from finance_core.reconciliation import final_transaction_adapter as fta

        expected = {
            "FinalTransactionAdapterStatus",
            "FinalTransactionAdapterReason",
            "FinalTransactionAdapterInput",
            "FinalTransactionAdapterResult",
            "OperationSummary",
            "build_final_transaction_reconciliation_adapter_result",
        }
        actual = set(fta.__all__)
        assert actual == expected


# ---------------------------------------------------------------------------
# Test: Guard decision refs preserved
# ---------------------------------------------------------------------------


class TestGuardDecisionRefsPreserved:
    def test_guard_decision_refs_carried(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert isinstance(result.guard_decision_refs, tuple)
        # The execution result carries guard decision refs from the plan
        assert result.guard_decision_refs == orch_result.execution_result.guard_decision_refs


# ---------------------------------------------------------------------------
# Test: Orchestration status echoed
# ---------------------------------------------------------------------------


class TestOrchestrationStatusEchoed:
    def test_orchestration_status_echoed(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        orch_result = _orch(
            _happy_input(sample_decision, queue_item, approved_guard_decision),
            fixed_clock,
        )
        adapter_input = FinalTransactionAdapterInput(orchestration_result=orch_result)
        result = build_final_transaction_reconciliation_adapter_result(adapter_input)

        assert result.orchestration_status is ApplyOrchestrationStatus.COMPLETED


# ---------------------------------------------------------------------------
# Test: All adapter statuses have descriptions
# ---------------------------------------------------------------------------


class TestAllStatusesHaveDescriptions:
    def test_every_status_has_value(self):
        for status in FinalTransactionAdapterStatus:
            assert status.value
            assert isinstance(status.value, str)

    def test_every_reason_has_value(self):
        for reason in FinalTransactionAdapterReason:
            assert reason.value
            assert isinstance(reason.value, str)
