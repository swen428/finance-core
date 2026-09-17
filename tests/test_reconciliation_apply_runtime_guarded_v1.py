"""Tests for Guarded Reconciliation Apply Runtime v1.

Covers:
  1. Runtime blocks execution when guard decision is missing
  2. Runtime blocks execution when guard decision is rejected / not approved
  3. Runtime executes supported approved apply plan once
  4. Re-running same idempotency key is safe (idempotent replay)
  5. Same idempotency key with conflicting plan content fails safely
  6. Unsupported mutation type is blocked with explicit reason
  7. Execution result contains useful audit fields
  8. Live database file is not touched
  9. Deterministic output: same inputs produce same result
 10. Empty idempotency key raises ValueError
 11. Empty plan (no operations) returns UNSUPPORTED
 12. Guard decision cross-wiring is detected and blocked
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from finance_core.reconciliation.apply_plan import (
    ApplyPlanInput,
    ReconciliationApplyPlan,
    build_reconciliation_apply_plan,
)
from finance_core.reconciliation.apply_runtime import (
    GuardedApplyRuntime,
    execute_apply_plan_guarded,
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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
def fixed_clock():
    """Deterministic clock for test assertions."""
    return lambda: "2024-12-15T12:00:00.000000+00:00"


# ---------------------------------------------------------------------------
# Test 1: Runtime blocks when guard decision is missing
# ---------------------------------------------------------------------------


class TestMissingGuardDecision:
    def test_missing_guard_decision_blocks_execution(
        self,
        sample_plan: ReconciliationApplyPlan,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime()
        result = runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={},  # No guard decisions
            idempotency_key="test-missing-guard-001",
            clock=fixed_clock,
        )

        assert result.execution_status == ApplyExecutionStatus.BLOCKED
        assert result.total_operations == 1
        assert result.operated_blocked == 1
        assert result.operated_executed == 0
        assert "Missing guard decisions" in result.block_reason
        assert result.is_dry_run is True

    def test_missing_guard_decision_sets_op_level_block(
        self,
        sample_plan: ReconciliationApplyPlan,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime()
        result = runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={},
            idempotency_key="test-missing-guard-002",
            clock=fixed_clock,
        )

        op_result = result.results[0]
        assert op_result.execution_status == ApplyExecutionStatus.BLOCKED
        assert "Missing guard decisions" in op_result.reason
        assert op_result.guard_decision_approved is False


# ---------------------------------------------------------------------------
# Test 2: Runtime blocks when guard decision is rejected
# ---------------------------------------------------------------------------


class TestRejectedGuardDecision:
    def test_blocked_guard_decision_blocks_execution(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=queue_item,
                guard_decision=blocked_guard_decision,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)

        runtime = GuardedApplyRuntime()
        op_id = plan.operations[0].operation_id
        result = runtime.execute(
            plan,
            guard_decisions_by_operation_id={op_id: blocked_guard_decision},
            idempotency_key="test-rejected-guard-001",
            clock=fixed_clock,
        )

        assert result.execution_status == ApplyExecutionStatus.BLOCKED
        assert result.operated_blocked == 1
        assert result.operated_executed == 0

        op_result = result.results[0]
        assert op_result.execution_status == ApplyExecutionStatus.BLOCKED
        assert op_result.guard_decision_approved is False
        assert "not approved" in op_result.reason.lower()
        assert op_result.guard_blocked_reasons == blocked_guard_decision.blocked_reasons

    def test_blocked_guard_preserves_blocked_reasons_in_audit(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=queue_item,
                guard_decision=blocked_guard_decision,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)

        runtime = GuardedApplyRuntime()
        op_id = plan.operations[0].operation_id
        result = runtime.execute(
            plan,
            guard_decisions_by_operation_id={op_id: blocked_guard_decision},
            idempotency_key="test-rejected-guard-002",
            clock=fixed_clock,
        )

        assert result.block_reason != ""
        assert result.audit_trail["operations_blocked"] == 1


# ---------------------------------------------------------------------------
# Test 3: Runtime executes supported approved apply plan once
# ---------------------------------------------------------------------------


class TestExecuteApprovedPlan:
    def test_approved_plan_executes_successfully(
        self,
        sample_plan: ReconciliationApplyPlan,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime()
        op_id = sample_plan.operations[0].operation_id
        result = runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={op_id: approved_guard_decision},
            idempotency_key="test-execute-approved-001",
            clock=fixed_clock,
        )

        assert result.execution_status == ApplyExecutionStatus.EXECUTED
        assert result.total_operations == 1
        assert result.operated_executed == 1
        assert result.operated_blocked == 0
        assert result.operated_skipped == 0
        assert result.is_dry_run is True
        assert result.executed_at == "2024-12-15T12:00:00.000000+00:00"

    def test_executed_operation_has_executed_status(
        self,
        sample_plan: ReconciliationApplyPlan,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime()
        op_id = sample_plan.operations[0].operation_id
        result = runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={op_id: approved_guard_decision},
            idempotency_key="test-execute-approved-002",
            clock=fixed_clock,
        )

        op_result = result.results[0]
        assert op_result.execution_status == ApplyExecutionStatus.EXECUTED
        assert op_result.guard_decision_approved is True
        assert "executed successfully" in op_result.reason
        assert op_result.mutation_type == "confirm_match"
        assert "operation_id" in op_result.mutation_payload


# ---------------------------------------------------------------------------
# Test 4: Re-running same idempotency key is safe (idempotent replay)
# ---------------------------------------------------------------------------


class TestIdempotentReplay:
    def test_same_key_returns_previous_result(
        self,
        sample_plan: ReconciliationApplyPlan,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime()
        op_id = sample_plan.operations[0].operation_id

        first = runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={op_id: approved_guard_decision},
            idempotency_key="test-idem-replay-001",
            clock=fixed_clock,
        )
        assert first.execution_status == ApplyExecutionStatus.EXECUTED

        # Replay with same key, same plan, same guard decisions
        second = runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={op_id: approved_guard_decision},
            idempotency_key="test-idem-replay-001",
            clock=fixed_clock,
        )

        # The idempotent replay returns the original result (EXECUTED).
        # V1 idempotent replay returns the cached original result with its
        # original status -- there is no separate "replay" status in v1.
        assert second.execution_status == ApplyExecutionStatus.EXECUTED
        assert second.operated_executed == first.operated_executed
        assert second.executed_at == first.executed_at

    def test_runtime_is_executed_returns_true_after_execution(
        self,
        sample_plan: ReconciliationApplyPlan,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime()
        op_id = sample_plan.operations[0].operation_id

        assert runtime.is_executed("test-is-executed-001") is False
        runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={op_id: approved_guard_decision},
            idempotency_key="test-is-executed-001",
            clock=fixed_clock,
        )
        assert runtime.is_executed("test-is-executed-001") is True


# ---------------------------------------------------------------------------
# Test 5: Same idempotency key with conflicting plan content fails safely
# ---------------------------------------------------------------------------


class TestConflictingIdempotency:
    def test_different_plan_same_key_is_conflict(
        self,
        sample_plan: ReconciliationApplyPlan,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime()
        op_id = sample_plan.operations[0].operation_id

        # First execution succeeds
        runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={op_id: approved_guard_decision},
            idempotency_key="test-conflict-001",
            clock=fixed_clock,
        )

        # Build a different plan (different decision)
        other_decision = ResolutionDecision(
            decision_id="dec-999",
            queue_item_id="q-001",
            action=ResolutionAction.IGNORE,
            note="Different decision.",
            reviewer="human",
        )
        other_inputs = [
            ApplyPlanInput(
                decision=other_decision,
                queue_item=queue_item,
                guard_decision=approved_guard_decision,
            )
        ]
        other_plan = build_reconciliation_apply_plan(other_inputs)
        other_op_id = other_plan.operations[0].operation_id

        # Different plan, same key -> CONFLICT
        result = runtime.execute(
            other_plan,
            guard_decisions_by_operation_id={
                other_op_id: approved_guard_decision,
            },
            idempotency_key="test-conflict-001",
            clock=fixed_clock,
        )

        assert result.execution_status == ApplyExecutionStatus.CONFLICT
        assert "already used" in result.block_reason.lower()
        assert "same key" in result.block_reason.lower()

    def test_same_plan_different_guard_is_conflict(
        self,
        sample_plan: ReconciliationApplyPlan,
        approved_guard_decision: FinalMutationGuardDecision,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime()
        op_id = sample_plan.operations[0].operation_id

        # First execution with approved guard
        runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={op_id: approved_guard_decision},
            idempotency_key="test-conflict-002",
            clock=fixed_clock,
        )

        # Same plan, same key, different guard -> CONFLICT
        result = runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={op_id: blocked_guard_decision},
            idempotency_key="test-conflict-002",
            clock=fixed_clock,
        )

        assert result.execution_status == ApplyExecutionStatus.CONFLICT
        assert "fingerprint mismatch" in result.block_reason.lower()


# ---------------------------------------------------------------------------
# Test 6: Unsupported mutation type is blocked with explicit reason
# ---------------------------------------------------------------------------


class TestUnsupportedMutationType:
    def test_create_missing_app_transaction_is_blocked(
        self,
        sample_statement: StatementTransaction,
        sample_app_txn: AppTransaction,
        fixed_clock,
    ):
        """create_missing_app_transaction is not in v1 supported actions."""
        decision = ResolutionDecision(
            decision_id="dec-unsup-001",
            queue_item_id="q-001",
            action=ResolutionAction.CREATE_MISSING_APP_TRANSACTION,
            note="Should create missing txns.",
            reviewer="human",
        )
        cand = ReconciliationCandidate(
            statement=sample_statement,
            best_app_transaction=None,
            match_status=MatchStatus.NO_MATCH,
            candidate_id="cand-unsup-001",
            review_priority=ReviewPriority.HIGH,
            issue_type=IssueType.MISSING_IN_APP,
        )
        item = ReviewQueueItem(
            candidate=cand,
            issue_type=IssueType.MISSING_IN_APP,
            suggested_action=SuggestedAction.CREATE_MISSING_APP_TRANSACTION,
            queue_item_id="q-001",
        )
        approved_gd = FinalMutationGuardDecision(
            proposal_id="fp-unsup-001",
            approved=True,
            action=FinalMutationAction.CREATE_FINAL_TRANSACTION,
        )
        inputs = [
            ApplyPlanInput(
                decision=decision,
                queue_item=item,
                guard_decision=approved_gd,
                proposal=FinalMutationProposal(
                    proposal_id="fp-unsup-001",
                    action=FinalMutationAction.CREATE_FINAL_TRANSACTION,
                ),
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)

        runtime = GuardedApplyRuntime()
        op_id = plan.operations[0].operation_id
        result = runtime.execute(
            plan,
            guard_decisions_by_operation_id={op_id: approved_gd},
            idempotency_key="test-unsupported-001",
            clock=fixed_clock,
        )

        assert result.execution_status == ApplyExecutionStatus.BLOCKED
        op_result = result.results[0]
        assert op_result.execution_status == ApplyExecutionStatus.BLOCKED
        assert "not a supported execution type" in op_result.reason.lower()

    def test_adjust_app_transaction_is_blocked_in_v1(
        self,
        sample_statement: StatementTransaction,
        sample_app_txn: AppTransaction,
        fixed_clock,
    ):
        """adjust_app_transaction is not in v1 supported actions."""
        decision = ResolutionDecision(
            decision_id="dec-unsup-002",
            queue_item_id="q-001",
            action=ResolutionAction.ADJUST_APP_TRANSACTION,
            note="Adjust amount.",
            reviewer="human",
        )
        cand = ReconciliationCandidate(
            statement=sample_statement,
            best_app_transaction=sample_app_txn,
            match_status=MatchStatus.AMOUNT_MISMATCH,
            candidate_id="cand-unsup-002",
            review_priority=ReviewPriority.HIGH,
            issue_type=IssueType.AMOUNT_MISMATCH,
        )
        item = ReviewQueueItem(
            candidate=cand,
            issue_type=IssueType.AMOUNT_MISMATCH,
            suggested_action=SuggestedAction.ADJUST_APP_TRANSACTION,
            queue_item_id="q-001",
        )
        approved_gd = FinalMutationGuardDecision(
            proposal_id="fp-unsup-002",
            approved=True,
            action=FinalMutationAction.ADJUST_FINAL_TRANSACTION,
        )
        inputs = [
            ApplyPlanInput(
                decision=decision,
                queue_item=item,
                guard_decision=approved_gd,
                proposal=FinalMutationProposal(
                    proposal_id="fp-unsup-002",
                    action=FinalMutationAction.ADJUST_FINAL_TRANSACTION,
                ),
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)

        runtime = GuardedApplyRuntime()
        op_id = plan.operations[0].operation_id
        result = runtime.execute(
            plan,
            guard_decisions_by_operation_id={op_id: approved_gd},
            idempotency_key="test-unsupported-002",
            clock=fixed_clock,
        )

        assert result.execution_status == ApplyExecutionStatus.BLOCKED


# ---------------------------------------------------------------------------
# Test 7: Execution result contains useful audit fields
# ---------------------------------------------------------------------------


class TestAuditFields:
    def test_execution_result_has_all_expected_fields(
        self,
        sample_plan: ReconciliationApplyPlan,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime()
        op_id = sample_plan.operations[0].operation_id
        result = runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={op_id: approved_guard_decision},
            idempotency_key="test-audit-001",
            clock=fixed_clock,
        )

        # Plan-level fields
        assert result.plan_id == sample_plan.plan_id
        assert result.idempotency_key == "test-audit-001"
        assert result.execution_status == ApplyExecutionStatus.EXECUTED
        assert result.total_operations == 1
        assert result.operated_executed == 1
        assert result.block_reason == ""
        assert result.executed_at == "2024-12-15T12:00:00.000000+00:00"
        assert result.is_dry_run is True

        # Guard decision refs
        assert result.guard_decision_refs == (approved_guard_decision.proposal_id,)

        # Audit trail
        audit = result.audit_trail
        assert audit["plan_id"] == sample_plan.plan_id
        assert audit["idempotency_key"] == "test-audit-001"
        assert audit["execution_status"] == "executed"
        assert audit["runtime_version"] == "v1"
        assert audit["is_dry_run"] is True
        assert audit["operations_executed"] == 1
        assert audit["operations_blocked"] == 0

    def test_blocked_result_has_block_reason(
        self,
        sample_plan: ReconciliationApplyPlan,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime()
        result = runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={},
            idempotency_key="test-audit-block-001",
            clock=fixed_clock,
        )

        assert result.execution_status == ApplyExecutionStatus.BLOCKED
        assert result.block_reason != ""
        assert result.operated_blocked == 1
        assert result.operated_executed == 0


# ---------------------------------------------------------------------------
# Test 8: Live database file is not touched
# ---------------------------------------------------------------------------


class TestLiveDbNotTouched:
    def test_runtime_never_opens_live_db(self):
        """The runtime has no database connection path -- it is purely
        in-memory and cannot touch the live database."""
        runtime = GuardedApplyRuntime()
        assert not hasattr(runtime, "_conn")
        assert not hasattr(runtime, "_db_path")


# ---------------------------------------------------------------------------
# Test 9: Deterministic output
# ---------------------------------------------------------------------------


class TestDeterministicOutput:
    def test_same_inputs_produce_same_result(
        self,
        sample_plan: ReconciliationApplyPlan,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime()
        op_id = sample_plan.operations[0].operation_id

        r1 = runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={op_id: approved_guard_decision},
            idempotency_key="test-det-001",
            clock=fixed_clock,
        )
        runtime.reset()

        r2 = runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={op_id: approved_guard_decision},
            idempotency_key="test-det-001",
            clock=fixed_clock,
        )

        assert r1.execution_status == r2.execution_status
        assert r1.operated_executed == r2.operated_executed
        assert r1.plan_id == r2.plan_id
        assert r1.executed_at == r2.executed_at


# ---------------------------------------------------------------------------
# Test 10: Empty idempotency key raises ValueError
# ---------------------------------------------------------------------------


class TestEmptyIdempotencyKey:
    def test_empty_key_raises_value_error(
        self,
        sample_plan: ReconciliationApplyPlan,
        approved_guard_decision: FinalMutationGuardDecision,
    ):
        runtime = GuardedApplyRuntime()
        op_id = sample_plan.operations[0].operation_id
        with pytest.raises(ValueError, match="idempotency_key must not be empty"):
            runtime.execute(
                sample_plan,
                guard_decisions_by_operation_id={op_id: approved_guard_decision},
                idempotency_key="",
            )

    def test_empty_key_raises_in_convenience_function(
        self,
        sample_plan: ReconciliationApplyPlan,
        approved_guard_decision: FinalMutationGuardDecision,
    ):
        op_id = sample_plan.operations[0].operation_id
        with pytest.raises(ValueError, match="idempotency_key must not be empty"):
            execute_apply_plan_guarded(
                sample_plan,
                guard_decisions_by_operation_id={op_id: approved_guard_decision},
                idempotency_key="",
            )


# ---------------------------------------------------------------------------
# Test 11: Empty plan returns UNSUPPORTED
# ---------------------------------------------------------------------------


class TestEmptyPlan:
    def test_plan_with_no_operations_returns_unsupported(self, fixed_clock):
        runtime = GuardedApplyRuntime()
        # Build a plan from empty inputs to trigger the empty-operations path.
        # Since the builder refuses empty inputs, we construct a plan directly.
        empty_plan = ReconciliationApplyPlan(
            plan_id="recon-apply-plan-empty-test",
            operations=(),
            risks=(),
            requires_human_confirmation=False,
            is_dry_run=True,
            total_operations=0,
            blocked_operations=0,
            approved_operations=0,
            pending_guard_operations=0,
            note="Empty plan.",
        )

        result = runtime.execute(
            empty_plan,
            guard_decisions_by_operation_id={},
            idempotency_key="test-empty-plan-001",
            clock=fixed_clock,
        )

        assert result.execution_status == ApplyExecutionStatus.UNSUPPORTED
        assert result.total_operations == 0
        assert result.operated_executed == 0
        assert "no operations" in result.block_reason.lower()


# ---------------------------------------------------------------------------
# Test 12: Guard decision cross-wiring is detected and blocked
# ---------------------------------------------------------------------------


class TestGuardCrossWiring:
    def test_mismatched_proposal_id_is_blocked(
        self,
        sample_plan: ReconciliationApplyPlan,
        approved_guard_decision: FinalMutationGuardDecision,
        sample_candidate: ReconciliationCandidate,
        fixed_clock,
    ):
        """Guard decision with a different proposal_id than the operation
        source_apply_result_ref should be blocked."""
        # Build a plan that actually has source_apply_result_ref set,
        # then use a guard decision with a mismatched proposal_id.
        from finance_core.reconciliation.apply_plan import ApplyPlanInput
        from finance_core.reconciliation.models import ResolutionDecision

        decision = ResolutionDecision(
            decision_id="dec-cross-001",
            queue_item_id="q-001",
            action=ResolutionAction.CONFIRM_MATCH,
            note="Cross-wire test.",
            reviewer="human",
        )
        proposal = FinalMutationProposal(
            proposal_id="fp-cross-001",
            action=FinalMutationAction.NO_FINAL_MUTATION,
        )
        # Build queue item using fixtures (avoiding name collisions)
        qitem = ReviewQueueItem(
            candidate=sample_candidate,
            issue_type=IssueType.MATCHED,
            suggested_action=SuggestedAction.CONFIRM_MATCH,
            queue_item_id="q-001",
        )
        guard = FinalMutationGuard()
        approved_gd = guard.evaluate(proposal)
        inputs = [
            ApplyPlanInput(
                decision=decision,
                queue_item=qitem,
                guard_decision=approved_gd,
                proposal=proposal,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)

        # Guard decision with mismatched proposal_id
        mismatched_gd = FinalMutationGuardDecision(
            proposal_id="fp-wrong-999",
            approved=True,
            action=FinalMutationAction.NO_FINAL_MUTATION,
        )

        runtime = GuardedApplyRuntime()
        op_id = plan.operations[0].operation_id
        result = runtime.execute(
            plan,
            guard_decisions_by_operation_id={op_id: mismatched_gd},
            idempotency_key="test-cross-wire-001",
            clock=fixed_clock,
        )

        assert result.execution_status == ApplyExecutionStatus.BLOCKED
        op_result = result.results[0]
        assert op_result.execution_status == ApplyExecutionStatus.BLOCKED
        assert "cross-wiring" in op_result.reason.lower()
        assert "does not match" in op_result.reason.lower()


# ---------------------------------------------------------------------------
# Test: Convenience function produces same result as class-based usage
# ---------------------------------------------------------------------------


class TestConvenienceFunction:
    def test_convenience_function_produces_valid_result(
        self,
        sample_plan: ReconciliationApplyPlan,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        op_id = sample_plan.operations[0].operation_id
        result = execute_apply_plan_guarded(
            sample_plan,
            guard_decisions_by_operation_id={op_id: approved_guard_decision},
            idempotency_key="test-convenience-001",
            clock=fixed_clock,
        )

        assert result.execution_status == ApplyExecutionStatus.EXECUTED
        assert result.plan_id == sample_plan.plan_id


# ---------------------------------------------------------------------------
# Test: Partially blocked plan (mixed approved + blocked ops)
# ---------------------------------------------------------------------------


class TestPartiallyBlocked:
    def test_mixed_approved_and_blocked_ops(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        blocked_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        """A plan with two operations: one approved, one blocked."""
        # First operation: approved
        inputs_approved = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=queue_item,
                guard_decision=approved_guard_decision,
            )
        ]
        # Second operation: blocked (different decision to avoid ID conflict)
        blocked_decision = ResolutionDecision(
            decision_id="dec-blocked-001",
            queue_item_id="q-001",
            action=ResolutionAction.CONFIRM_MATCH,
            note="Blocked op.",
            reviewer="human",
        )
        inputs_blocked = [
            ApplyPlanInput(
                decision=blocked_decision,
                queue_item=queue_item,
                guard_decision=blocked_guard_decision,
            )
        ]

        plan = build_reconciliation_apply_plan(inputs_approved + inputs_blocked)
        op_ids = [op.operation_id for op in plan.operations]

        gd_map = {
            op_ids[0]: approved_guard_decision,
            op_ids[1]: blocked_guard_decision,
        }

        runtime = GuardedApplyRuntime()
        result = runtime.execute(
            plan,
            guard_decisions_by_operation_id=gd_map,
            idempotency_key="test-partial-001",
            clock=fixed_clock,
        )

        assert result.execution_status == ApplyExecutionStatus.PARTIALLY_BLOCKED
        assert result.total_operations == 2
        assert result.operated_executed == 1
        assert result.operated_blocked == 1
        assert result.block_reason != ""
