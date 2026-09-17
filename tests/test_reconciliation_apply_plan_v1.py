"""Tests for Reconciliation Apply Plan Builder v1.

Covers:
- Successful apply plan generation for a safe / allowed review decision.
- Blocked apply plan when guard decision blocks mutation.
- Plan explicitly requires human confirmation before final mutation.
- Plan does not mutate final transaction data.
- Plan preserves relevant evidence / source references.
- Plan output is deterministic.
- Plan distinguishes dry-run operation from executed apply.
- Empty inputs raise ValueError.
- Multiple operations in a single plan.
- Mixed approved, blocked, and pending-guard operations.
- Plan-level risks for all-blocked, majority-blocked, pending guard,
  and missing evidence.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from finance_core.reconciliation.apply_plan import (
    ApplyPlanInput,
    ApplyPlanOperation,
    ReconciliationApplyPlan,
    build_reconciliation_apply_plan,
)
from finance_core.reconciliation.final_mutation_proposal import (
    FinalMutationAction,
    FinalMutationBlockedReason,
    FinalMutationGuard,
    FinalMutationGuardDecision,
    FinalMutationProposal,
)
from finance_core.reconciliation.models import (
    AppTransaction,
    IssueType,
    MatchStatus,
    ReasonCode,
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
def sample_candidate(sample_statement, sample_app_txn) -> ReconciliationCandidate:
    return ReconciliationCandidate(
        statement=sample_statement,
        best_app_transaction=sample_app_txn,
        match_status=MatchStatus.MATCHED,
        reason_codes=(ReasonCode.EXACT_AMOUNT_MATCH,),
        issue_type=IssueType.MATCHED,
        candidate_id="cand-001",
        review_priority=ReviewPriority.LOW,
    )


@pytest.fixture
def sample_queue_item(sample_candidate) -> ReviewQueueItem:
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
        evidence_refs=("ev-abc", "ev-def"),
        note="No mutation needed for confirmed match.",
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


# ---------------------------------------------------------------------------
# Test: successful apply plan generation for a safe / allowed review decision
# ---------------------------------------------------------------------------


class TestSuccessfulApplyPlanGeneration:
    def test_plan_with_approved_guard_decision(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)

        assert plan.is_dry_run is True
        assert plan.total_operations == 1
        assert plan.blocked_operations == 0
        assert plan.approved_operations == 1
        assert plan.pending_guard_operations == 0
        assert plan.requires_human_confirmation is True
        assert len(plan.operations) == 1

        op = plan.operations[0]
        assert op.operation_id == "op-dec-001"
        assert op.decision_id == "dec-001"
        assert op.queue_item_id == "q-001"
        assert op.candidate_id == "cand-001"
        assert op.action == ResolutionAction.CONFIRM_MATCH
        assert op.guard_decision_approved is True
        assert op.guard_blocked_reasons == ()
        assert op.is_blocked is False
        assert op.requires_human_confirmation is True
        assert "Dry-run plan only" in op.dry_run_statement

    def test_plan_preserves_evidence_refs_from_proposal(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
                proposal=FinalMutationProposal(
                    proposal_id="fp-evid",
                    action=FinalMutationAction.NO_FINAL_MUTATION,
                    evidence_refs=("ev-abc", "ev-def"),
                ),
                evidence_refs=("ev-123",),
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)

        op = plan.operations[0]
        # Guard decision's proposal evidence refs take priority
        assert "ev-abc" in op.evidence_refs
        assert "ev-def" in op.evidence_refs


# ---------------------------------------------------------------------------
# Test: blocked apply plan when guard decision blocks mutation
# ---------------------------------------------------------------------------


class TestBlockedApplyPlan:
    def test_plan_with_blocked_guard_decision(
        self, sample_decision, sample_queue_item, blocked_guard_decision
    ):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=blocked_guard_decision,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)

        assert plan.is_dry_run is True
        assert plan.total_operations == 1
        assert plan.blocked_operations == 1
        assert plan.approved_operations == 0
        assert plan.pending_guard_operations == 0
        assert plan.requires_human_confirmation is False  # blocked => no confirmation needed
        assert len(plan.operations) == 1

        op = plan.operations[0]
        assert op.guard_decision_approved is False
        assert op.is_blocked is True
        assert op.requires_human_confirmation is False
        assert len(op.guard_blocked_reasons) > 0
        assert FinalMutationBlockedReason.MISSING_MERCHANT.value in op.guard_blocked_reasons

    def test_blocked_plan_has_critical_risk(
        self, sample_decision, sample_queue_item, blocked_guard_decision
    ):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=blocked_guard_decision,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)

        critical_risks = [r for r in plan.risks if r.severity == "critical"]
        assert len(critical_risks) >= 1

        # Should have guard_blocked_operation risk
        blocked_risks = [r for r in critical_risks if r.risk_code == "guard_blocked_operation"]
        assert len(blocked_risks) == 1
        assert "blocked by guard" in blocked_risks[0].risk_message


# ---------------------------------------------------------------------------
# Test: plan explicitly requires human confirmation before final mutation
# ---------------------------------------------------------------------------


class TestHumanConfirmationRequired:
    def test_approved_operation_requires_confirmation(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)

        assert plan.requires_human_confirmation is True
        op = plan.operations[0]
        assert op.requires_human_confirmation is True

        # Should have human_confirmation_required risk
        conf_risks = [r for r in plan.risks if r.risk_code == "human_confirmation_required"]
        assert len(conf_risks) == 1

    def test_pending_guard_operation_requires_confirmation(
        self, sample_decision, sample_queue_item
    ):
        # No guard decision => pending guard => requires human confirmation
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=None,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)

        assert plan.requires_human_confirmation is True
        op = plan.operations[0]
        assert op.requires_human_confirmation is True
        assert op.guard_decision_approved is False
        assert op.is_blocked is False
        assert plan.pending_guard_operations == 1

    def test_blocked_operation_does_not_require_confirmation(
        self, sample_decision, sample_queue_item, blocked_guard_decision
    ):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=blocked_guard_decision,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)

        assert plan.requires_human_confirmation is False
        op = plan.operations[0]
        assert op.requires_human_confirmation is False


# ---------------------------------------------------------------------------
# Test: plan does not mutate final transaction data
# ---------------------------------------------------------------------------


class TestNoMutation:
    def test_plan_is_always_dry_run(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)

        assert plan.is_dry_run is True

        # Verify every operation confirms dry-run
        for op in plan.operations:
            assert "Dry-run plan only" in op.dry_run_statement

    def test_plan_does_not_mutate_sqlite(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        """Verify that building a plan does not touch any database."""
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
            )
        ]
        # No connection, no database path -- must succeed without any DB access
        plan = build_reconciliation_apply_plan(inputs)
        assert plan is not None
        assert plan.is_dry_run is True

    def test_plan_does_not_modify_live_data(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        """Verify that the plan builder does not write to database/finance.db."""
        import os

        db_path = os.path.join(os.path.dirname(__file__), "..", "database", "finance.db")
        if os.path.exists(db_path):
            mtime_before = os.path.getmtime(db_path)

            inputs = [
                ApplyPlanInput(
                    decision=sample_decision,
                    queue_item=sample_queue_item,
                    guard_decision=approved_guard_decision,
                )
            ]
            build_reconciliation_apply_plan(inputs)

            mtime_after = os.path.getmtime(db_path)
            assert mtime_before == mtime_after, (
                f"database/finance.db was modified (mtime changed): {mtime_before} -> {mtime_after}"
            )


# ---------------------------------------------------------------------------
# Test: plan preserves relevant evidence / source references
# ---------------------------------------------------------------------------


class TestEvidenceAndSourceReferences:
    def test_statement_ref_preserved(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)
        op = plan.operations[0]
        assert op.statement_ref is not None
        # statement_row_reference takes priority over merchant_raw
        assert "stmt-row-001" in op.statement_ref or "Giant" in op.statement_ref

    def test_app_transaction_ref_preserved(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)
        op = plan.operations[0]
        assert op.app_transaction_ref == "app-txn-001"

    def test_evidence_refs_from_proposal(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        proposal = FinalMutationProposal(
            proposal_id="fp-003",
            action=FinalMutationAction.NO_FINAL_MUTATION,
            evidence_refs=("ev-proposal-1", "ev-proposal-2"),
        )
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
                proposal=proposal,
                evidence_refs=("ev-caller-1",),
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)
        op = plan.operations[0]
        # Proposal evidence refs take priority over caller-supplied
        assert op.evidence_refs == ("ev-proposal-1", "ev-proposal-2")

    def test_evidence_refs_from_caller_when_no_proposal(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
                proposal=None,
                evidence_refs=("ev-caller-1",),
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)
        op = plan.operations[0]
        assert op.evidence_refs == ("ev-caller-1",)

    def test_source_apply_result_ref_from_proposal(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        proposal = FinalMutationProposal(
            proposal_id="fp-link-001",
            action=FinalMutationAction.NO_FINAL_MUTATION,
        )
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
                proposal=proposal,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)
        op = plan.operations[0]
        assert op.source_apply_result_ref == "fp-link-001"


# ---------------------------------------------------------------------------
# Test: plan output is deterministic
# ---------------------------------------------------------------------------


class TestDeterministicOutput:
    def test_same_inputs_produce_same_plan(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        def build() -> ReconciliationApplyPlan:
            inputs = [
                ApplyPlanInput(
                    decision=sample_decision,
                    queue_item=sample_queue_item,
                    guard_decision=approved_guard_decision,
                )
            ]
            return build_reconciliation_apply_plan(inputs)

        plan1 = build()
        plan2 = build()

        # Plan ID is deterministic
        assert plan1.plan_id == plan2.plan_id

        # All fields identical
        assert plan1.total_operations == plan2.total_operations
        assert plan1.blocked_operations == plan2.blocked_operations
        assert plan1.approved_operations == plan2.approved_operations
        assert plan1.requires_human_confirmation == plan2.requires_human_confirmation
        assert plan1.is_dry_run == plan2.is_dry_run

        for op1, op2 in zip(plan1.operations, plan2.operations):
            assert op1.operation_id == op2.operation_id
            assert op1.decision_id == op2.decision_id
            assert op1.is_blocked == op2.is_blocked
            assert op1.requires_human_confirmation == op2.requires_human_confirmation

    def test_plan_id_includes_label(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs, plan_label="December Batch")
        assert "december-batch" in plan.plan_id.lower() or "december" in plan.plan_id.lower()
        assert plan.plan_id.startswith("recon-apply-plan-")


# ---------------------------------------------------------------------------
# Test: plan distinguishes dry-run operation from executed apply
# ---------------------------------------------------------------------------


class TestDryRunDistinction:
    def test_plan_is_dry_run_always(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        for guard in [approved_guard_decision, None]:
            inputs = [
                ApplyPlanInput(
                    decision=sample_decision,
                    queue_item=sample_queue_item,
                    guard_decision=guard,
                )
            ]
            plan = build_reconciliation_apply_plan(inputs)
            assert plan.is_dry_run is True, (
                f"plan.is_dry_run should be True even with guard={guard}"
            )

    def test_plan_note_explicitly_states_dry_run(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)
        assert "Dry-run only" in plan.note
        assert "no final financial records have been created or modified" in plan.note


# ---------------------------------------------------------------------------
# Test: empty inputs raise ValueError
# ---------------------------------------------------------------------------


class TestEmptyInputs:
    def test_empty_inputs_raises_value_error(self):
        with pytest.raises(ValueError, match="at least one input"):
            build_reconciliation_apply_plan([])


# ---------------------------------------------------------------------------
# Test: multiple operations in a single plan
# ---------------------------------------------------------------------------


class TestMultipleOperations:
    def test_multiple_operations(self, sample_decision, sample_queue_item, approved_guard_decision):
        decision2 = ResolutionDecision(
            decision_id="dec-002",
            queue_item_id="q-002",
            action=ResolutionAction.MARK_STATEMENT_ONLY,
            note="No matching app transaction.",
            reviewer="human",
        )
        # Build a second queue item with a different statement
        stmt2 = StatementTransaction(
            transaction_date=date(2024, 12, 3),
            posted_date=date(2024, 12, 4),
            merchant_raw="Unknown Vendor",
            amount=Decimal("12.00"),
            currency="SGD",
            statement_row_reference="stmt-row-002",
        )
        cand2 = ReconciliationCandidate(
            statement=stmt2,
            match_status=MatchStatus.NO_MATCH,
            issue_type=IssueType.MISSING_IN_STATEMENT,
            candidate_id="cand-002",
            review_priority=ReviewPriority.LOW,
        )
        item2 = ReviewQueueItem(
            candidate=cand2,
            issue_type=IssueType.MISSING_IN_STATEMENT,
            suggested_action=SuggestedAction.MARK_STATEMENT_ONLY,
            queue_item_id="q-002",
        )

        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
            ),
            ApplyPlanInput(
                decision=decision2,
                queue_item=item2,
                guard_decision=None,  # no guard => pending
            ),
        ]
        plan = build_reconciliation_apply_plan(inputs)

        assert plan.total_operations == 2
        assert plan.approved_operations == 1
        assert plan.blocked_operations == 0
        assert plan.pending_guard_operations == 1
        assert plan.requires_human_confirmation is True

        ops_by_id = {op.operation_id: op for op in plan.operations}
        assert "op-dec-001" in ops_by_id
        assert "op-dec-002" in ops_by_id

        op1 = ops_by_id["op-dec-001"]
        assert op1.guard_decision_approved is True
        assert op1.is_blocked is False

        op2 = ops_by_id["op-dec-002"]
        assert op2.guard_decision_approved is False
        assert op2.is_blocked is False
        assert op2.requires_human_confirmation is True


# ---------------------------------------------------------------------------
# Test: mixed approved, blocked, and pending-guard operations
# ---------------------------------------------------------------------------


class TestMixedOperations:
    def test_mixed_approved_blocked_pending(
        self, sample_decision, sample_queue_item, approved_guard_decision, blocked_guard_decision
    ):
        # Third input: no guard decision
        decision3 = ResolutionDecision(
            decision_id="dec-003",
            queue_item_id="q-003",
            action=ResolutionAction.CONFIRM_MATCH,
        )
        stmt3 = StatementTransaction(
            transaction_date=date(2024, 12, 5),
            posted_date=None,
            merchant_raw="Pending Vendor",
            amount=Decimal("30.00"),
            currency="SGD",
        )
        cand3 = ReconciliationCandidate(
            statement=stmt3,
            match_status=MatchStatus.MATCHED,
            issue_type=IssueType.MATCHED,
            candidate_id="cand-003",
            review_priority=ReviewPriority.LOW,
        )
        item3 = ReviewQueueItem(
            candidate=cand3,
            issue_type=IssueType.MATCHED,
            suggested_action=SuggestedAction.CONFIRM_MATCH,
            queue_item_id="q-003",
        )

        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
            ),
            ApplyPlanInput(
                decision=sample_decision,  # same decision, different queue item
                queue_item=sample_queue_item,
                guard_decision=blocked_guard_decision,
            ),
            ApplyPlanInput(
                decision=decision3,
                queue_item=item3,
                guard_decision=None,
            ),
        ]
        plan = build_reconciliation_apply_plan(inputs)

        assert plan.total_operations == 3
        assert plan.approved_operations == 1
        assert plan.blocked_operations == 1
        assert plan.pending_guard_operations == 1

        # All three risk categories present
        risk_codes = {r.risk_code for r in plan.risks}
        assert "guard_blocked_operation" in risk_codes  # blocked
        assert "human_confirmation_required" in risk_codes  # approved
        assert "pending_guard_evaluation" in risk_codes  # pending


# ---------------------------------------------------------------------------
# Test: plan-level risks
# ---------------------------------------------------------------------------


class TestPlanLevelRisks:
    def test_all_operations_blocked_risk(
        self, sample_decision, sample_queue_item, blocked_guard_decision
    ):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=blocked_guard_decision,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)
        all_blocked = [r for r in plan.risks if r.risk_code == "all_operations_blocked"]
        assert len(all_blocked) == 1
        assert all_blocked[0].severity == "critical"

    def test_majority_blocked_risk(
        self, sample_decision, sample_queue_item, approved_guard_decision, blocked_guard_decision
    ):
        # 1 approved, 2 blocked = majority blocked
        decision2 = ResolutionDecision(
            decision_id="dec-002", queue_item_id="q-002", action=ResolutionAction.CONFIRM_MATCH
        )
        decision3 = ResolutionDecision(
            decision_id="dec-003", queue_item_id="q-003", action=ResolutionAction.CONFIRM_MATCH
        )

        # Create 2 additional blocked inputs (same blocked guard)
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
            ),
            ApplyPlanInput(
                decision=decision2,
                queue_item=sample_queue_item,
                guard_decision=blocked_guard_decision,
            ),
            ApplyPlanInput(
                decision=decision3,
                queue_item=sample_queue_item,
                guard_decision=blocked_guard_decision,
            ),
        ]
        plan = build_reconciliation_apply_plan(inputs)
        assert plan.blocked_operations == 2

        maj_blocked = [r for r in plan.risks if r.risk_code == "majority_operations_blocked"]
        assert len(maj_blocked) == 1
        assert maj_blocked[0].severity == "warning"

    def test_missing_evidence_risk(self, sample_decision, sample_queue_item):
        # Guard-less, no evidence refs
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=None,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)
        missing_ev = [r for r in plan.risks if r.risk_code == "missing_evidence_refs"]
        assert len(missing_ev) == 1
        assert missing_ev[0].severity == "warning"

    def test_pending_guard_plan_level_risk(self, sample_decision, sample_queue_item):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=None,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)
        pending = [r for r in plan.risks if r.risk_code == "pending_guard_evaluations"]
        # Should have both operation-level and plan-level pending guard risks
        assert len(pending) >= 1
        # Plan-level risk should exist when all operations are pending
        plan_pending = [r for r in pending if r.operation_id is None]
        assert len(plan_pending) >= 1


# ---------------------------------------------------------------------------
# Test: ApplyPlanInput and ApplyPlanOperation dataclass properties
# ---------------------------------------------------------------------------


class TestDataclassProperties:
    def test_apply_plan_input_is_frozen(self):
        inp = ApplyPlanInput(
            decision=ResolutionDecision(
                decision_id="d", queue_item_id="q", action=ResolutionAction.CONFIRM_MATCH
            ),
            queue_item=ReviewQueueItem(
                candidate=ReconciliationCandidate(
                    statement=StatementTransaction(
                        transaction_date=date(2024, 1, 1),
                        posted_date=None,
                        merchant_raw="X",
                        amount=Decimal("1"),
                        currency="SGD",
                    ),
                    candidate_id="c",
                ),
                issue_type=IssueType.MATCHED,
                suggested_action=SuggestedAction.CONFIRM_MATCH,
                queue_item_id="q",
            ),
        )
        with pytest.raises(Exception):
            inp.decision = None  # type: ignore[misc]

    def test_apply_plan_operation_is_frozen(self):
        op = ApplyPlanOperation(
            operation_id="op-1",
            decision_id="d",
            queue_item_id="q",
            candidate_id="c",
            action=ResolutionAction.CONFIRM_MATCH,
            guard_decision_approved=True,
        )
        with pytest.raises(Exception):
            op.is_blocked = True  # type: ignore[misc]

    def test_reconciliation_apply_plan_is_frozen(self):
        plan = ReconciliationApplyPlan(
            plan_id="test-plan",
            operations=(),
            risks=(),
            requires_human_confirmation=False,
            is_dry_run=True,
            total_operations=0,
            blocked_operations=0,
            approved_operations=0,
            pending_guard_operations=0,
            note="test",
        )
        with pytest.raises(Exception):
            plan.total_operations = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test: plan ID determinism with different labels
# ---------------------------------------------------------------------------


class TestPlanIdDeterminism:
    def test_different_labels_produce_different_ids(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
            )
        ]
        plan1 = build_reconciliation_apply_plan(inputs, plan_label="Batch A")
        plan2 = build_reconciliation_apply_plan(inputs, plan_label="Batch B")
        assert plan1.plan_id != plan2.plan_id

    def test_same_label_produces_same_id(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
            )
        ]
        plan1 = build_reconciliation_apply_plan(inputs, plan_label="Consistent Label")
        plan2 = build_reconciliation_apply_plan(inputs, plan_label="Consistent Label")
        assert plan1.plan_id == plan2.plan_id

    def test_different_guard_status_changes_plan_id(
        self, sample_decision, sample_queue_item, approved_guard_decision, blocked_guard_decision
    ):
        """Plan ID changes when guard blocks vs approves."""
        inputs_approved = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
            )
        ]
        inputs_blocked = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=blocked_guard_decision,
            )
        ]
        plan1 = build_reconciliation_apply_plan(inputs_approved)
        plan2 = build_reconciliation_apply_plan(inputs_blocked)
        assert plan1.plan_id != plan2.plan_id

    def test_different_evidence_refs_changes_plan_id(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        """Plan ID changes when evidence refs differ."""
        inputs_a = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
                evidence_refs=("ev-aaa",),
            )
        ]
        inputs_b = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
                evidence_refs=("ev-bbb", "ev-ccc"),
            )
        ]
        plan1 = build_reconciliation_apply_plan(inputs_a)
        plan2 = build_reconciliation_apply_plan(inputs_b)
        assert plan1.plan_id != plan2.plan_id

    def test_different_candidate_id_changes_plan_id(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        """Plan ID changes when candidate_id differs."""
        inputs_a = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
            )
        ]
        # Build a second input with a different candidate
        stmt2 = StatementTransaction(
            transaction_date=sample_queue_item.candidate.statement.transaction_date,
            posted_date=None,
            merchant_raw="Other Merchant",
            amount=sample_queue_item.candidate.statement.amount,
            currency="SGD",
            statement_row_reference="stmt-row-999",
        )
        cand2 = ReconciliationCandidate(
            statement=stmt2,
            match_status=sample_queue_item.candidate.match_status,
            issue_type=sample_queue_item.candidate.issue_type,
            candidate_id="cand-different",
            review_priority=sample_queue_item.candidate.review_priority,
        )
        from finance_core.reconciliation.models import ReviewQueueItem

        item2 = ReviewQueueItem(
            candidate=cand2,
            issue_type=sample_queue_item.issue_type,
            suggested_action=sample_queue_item.suggested_action,
            queue_item_id="q-diff",
        )
        dec2 = ResolutionDecision(
            decision_id="dec-diff",
            queue_item_id="q-diff",
            action=ResolutionAction.CONFIRM_MATCH,
        )
        inputs_b = [
            ApplyPlanInput(
                decision=dec2,
                queue_item=item2,
                guard_decision=approved_guard_decision,
            )
        ]
        plan1 = build_reconciliation_apply_plan(inputs_a)
        plan2 = build_reconciliation_apply_plan(inputs_b)
        assert plan1.plan_id != plan2.plan_id

    def test_different_action_changes_plan_id(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        """Plan ID changes when resolution action differs."""
        inputs_confirm = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
            )
        ]
        # Build a decision with a different action
        dec_mark = ResolutionDecision(
            decision_id="dec-alt-action",
            queue_item_id=sample_decision.queue_item_id,
            action=ResolutionAction.MARK_STATEMENT_ONLY,
        )
        inputs_mark = [
            ApplyPlanInput(
                decision=dec_mark,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
            )
        ]
        plan1 = build_reconciliation_apply_plan(inputs_confirm)
        plan2 = build_reconciliation_apply_plan(inputs_mark)
        assert plan1.plan_id != plan2.plan_id

    def test_same_inputs_produce_same_plan_id_strengthened(
        self, sample_decision, sample_queue_item, approved_guard_decision
    ):
        """Repeated builds with identical material inputs produce the same plan_id."""
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=approved_guard_decision,
            )
        ]
        plan1 = build_reconciliation_apply_plan(inputs)
        plan2 = build_reconciliation_apply_plan(inputs)
        assert plan1.plan_id == plan2.plan_id


# ---------------------------------------------------------------------------
# Test: guard_decision_none yields correct pending state
# ---------------------------------------------------------------------------


class TestPendingGuardState:
    def test_no_guard_decision_yields_pending(self, sample_decision, sample_queue_item):
        inputs = [
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=sample_queue_item,
                guard_decision=None,
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)
        assert plan.approved_operations == 0
        assert plan.blocked_operations == 0
        assert plan.pending_guard_operations == 1

        op = plan.operations[0]
        assert op.guard_decision_approved is False
        assert op.is_blocked is False
        assert op.guard_blocked_reasons == ()
        assert op.requires_human_confirmation is True
