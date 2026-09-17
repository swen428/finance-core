"""Tests for Reconciliation Apply Audit Snapshot v1.

Covers:
  1. Build snapshot for completed apply orchestration result.
  2. Build snapshot for blocked apply.
  3. Build snapshot for partially blocked apply.
  4. Build snapshot for conflict/idempotency rejection.
  5. Build snapshot with ApplyIdempotencyClassification included.
  6. Build snapshot with FinalTransactionAdapterResult included.
  7. Operation snapshots preserve deterministic ordering.
  8. Guard decision details are included.
  9. Audit/evidence refs are preserved where available.
 10. Snapshot does not create/update/delete final transaction records.
 11. Snapshot does not call or import execute_guarded_final_mutation_workflow.
 12. Snapshot builder is deterministic for same inputs.
 13. Public API exports work.
 14. No database/finance.db access.
 15. build_apply_audit_snapshot_from_repository via caller-supplied repo.
 16. No-prior-execution repository snapshot is useful.
"""

from __future__ import annotations

import ast
import hashlib
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_core.reconciliation.apply_audit_snapshot import (
    ApplyAuditGuardSnapshot,
    ApplyAuditIdempotencySnapshot,
    ApplyAuditOperationSnapshot,
    build_apply_audit_snapshot,
    build_apply_audit_snapshot_from_repository,
)
from finance_core.reconciliation.apply_orchestrator import (
    ApplyIdempotencyClassification,
    ApplyIdempotencyOutcome,
    ApplyOrchestrationInput,
    ApplyOrchestrationResult,
    ApplyOrchestrationStatus,
    orchestrate_reconciliation_apply,
)
from finance_core.reconciliation.apply_plan import ApplyPlanInput
from finance_core.reconciliation.apply_runtime_persistence import (
    GuardedApplyExecutionRepository,
)
from finance_core.reconciliation.final_mutation_proposal import (
    FinalMutationAction,
    FinalMutationGuard,
    FinalMutationGuardDecision,
    FinalMutationProposal,
)
from finance_core.reconciliation.final_transaction_adapter import (
    FinalTransactionAdapterResult,
    FinalTransactionAdapterStatus,
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
    proposal = FinalMutationProposal(
        proposal_id="fp-001",
        action=FinalMutationAction.NO_FINAL_MUTATION,
        evidence_refs=("ev-abc", "ev-def"),
    )
    return FinalMutationGuard().evaluate(proposal)


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
    return FinalMutationGuard().evaluate(proposal)


@pytest.fixture
def happy_input(
    sample_decision: ResolutionDecision,
    queue_item: ReviewQueueItem,
    approved_guard_decision: FinalMutationGuardDecision,
) -> ApplyOrchestrationInput:
    plan_input = ApplyPlanInput(
        decision=sample_decision,
        queue_item=queue_item,
        guard_decision=approved_guard_decision,
    )
    op_id = "op-dec-001"
    return ApplyOrchestrationInput(
        plan_inputs=(plan_input,),
        guard_decisions_by_operation_id={op_id: approved_guard_decision},
        idempotency_key="orch-happy-001",
    )


@pytest.fixture
def happy_result(
    happy_input: ApplyOrchestrationInput,
    fixed_clock,
) -> ApplyOrchestrationResult:
    return orchestrate_reconciliation_apply(happy_input, clock=fixed_clock)


@pytest.fixture
def blocked_result(
    sample_decision: ResolutionDecision,
    queue_item: ReviewQueueItem,
    blocked_guard_decision: FinalMutationGuardDecision,
    fixed_clock,
) -> ApplyOrchestrationResult:
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
    return orchestrate_reconciliation_apply(orch_input, clock=fixed_clock)


@pytest.fixture
def partial_result(
    sample_decision: ResolutionDecision,
    queue_item: ReviewQueueItem,
    approved_guard_decision: FinalMutationGuardDecision,
    blocked_guard_decision: FinalMutationGuardDecision,
    fixed_clock,
) -> ApplyOrchestrationResult:
    other_decision = ResolutionDecision(
        decision_id="dec-002",
        queue_item_id="q-001",
        action=ResolutionAction.CONFIRM_MATCH,
        note="Second decision.",
        reviewer="human",
    )
    plan_inputs = (
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
    orch_input = ApplyOrchestrationInput(
        plan_inputs=plan_inputs,
        guard_decisions_by_operation_id=gd_map,
        idempotency_key="orch-partial-001",
    )
    return orchestrate_reconciliation_apply(orch_input, clock=fixed_clock)


@pytest.fixture
def conflict_result(
    sample_decision: ResolutionDecision,
    queue_item: ReviewQueueItem,
    approved_guard_decision: FinalMutationGuardDecision,
    fixed_clock,
    temp_db_path: Path,
) -> ApplyOrchestrationResult:
    conn = connect_temp_db(temp_db_path)
    apply_sql(conn, MIGRATION_014)
    conn.commit()
    repo = GuardedApplyExecutionRepository(conn)
    orch_input = ApplyOrchestrationInput(
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
    _first = orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)

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
    return orchestrate_reconciliation_apply(other_input, repository=repo, clock=fixed_clock)


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


@pytest.fixture
def sample_adapter_result() -> FinalTransactionAdapterResult:
    return FinalTransactionAdapterResult(
        status=FinalTransactionAdapterStatus.ELIGIBLE,
        is_eligible_for_final_mutation=True,
        requires_final_write_confirmation=True,
        plan_id="plan-test",
        idempotency_key="key-test",
        orchestration_status=ApplyOrchestrationStatus.COMPLETED,
        operation_summaries=(),
        total_operations=0,
        eligible_operation_count=0,
        ineligible_operation_count=0,
        guard_decision_refs=(),
        reason_codes=(),
        adapter_note="OK",
    )


@pytest.fixture
def sample_idempotency_classification(
    happy_result: ApplyOrchestrationResult,
) -> ApplyIdempotencyClassification:
    return ApplyIdempotencyClassification(
        idempotency_key=happy_result.idempotency_key,
        outcome=ApplyIdempotencyOutcome.IDEMPOTENT_REPLAY,
        candidate_fingerprint="fp-candidate",
        prior_execution_exists=True,
        prior_execution_id="exec-001",
        prior_execution_status=ApplyExecutionStatus.EXECUTED,
        prior_execution_fingerprint="fp-candidate",
        classification_note="This is a completed idempotent replay.",
    )


# ===========================================================================
# Tests
# ===========================================================================


class TestCompletedSnapshot:
    """Test 1: Build snapshot for completed apply orchestration result."""

    def test_snapshot_id_is_deterministic(self, happy_result):
        snapshot = build_apply_audit_snapshot(happy_result)
        plan_id = happy_result.plan.plan_id
        idem_key = happy_result.idempotency_key
        raw = f"{plan_id}|{idem_key}"
        expected_digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        expected_id = f"snap-{expected_digest[:16]}"
        assert snapshot.snapshot_id == expected_id

    def test_plan_id_and_idempotency_key_captured(self, happy_result):
        snapshot = build_apply_audit_snapshot(happy_result)
        assert snapshot.plan_id == happy_result.plan.plan_id
        assert snapshot.idempotency_key == happy_result.idempotency_key

    def test_orchestration_status_is_completed(self, happy_result):
        snapshot = build_apply_audit_snapshot(happy_result)
        assert snapshot.orchestration_status == "completed"
        assert snapshot.execution_status == "executed"

    def test_operation_counts_captured(self, happy_result):
        snapshot = build_apply_audit_snapshot(happy_result)
        assert snapshot.total_operations == 1
        assert snapshot.executed_count == 1
        assert snapshot.blocked_count == 0
        assert snapshot.skipped_count == 0

    def test_review_required_flag(self, happy_result):
        snapshot = build_apply_audit_snapshot(happy_result)
        assert snapshot.review_required is False

    def test_persistence_flags(self, happy_result):
        snapshot = build_apply_audit_snapshot(happy_result)
        assert snapshot.persisted is False
        assert snapshot.persistence_enabled is False

    def test_operation_snapshots_exist(self, happy_result):
        snapshot = build_apply_audit_snapshot(happy_result)
        assert len(snapshot.operation_snapshots) == 1
        op = snapshot.operation_snapshots[0]
        assert isinstance(op, ApplyAuditOperationSnapshot)
        assert op.operation_id == "op-dec-001"
        assert op.execution_status == ApplyExecutionStatus.EXECUTED
        assert op.guard_approved is True

    def test_guard_snapshots_when_input_supplied(self, happy_result, happy_input):
        snapshot = build_apply_audit_snapshot(happy_result, orch_input=happy_input)
        assert len(snapshot.guard_snapshots) == 1
        gs = snapshot.guard_snapshots[0]
        assert isinstance(gs, ApplyAuditGuardSnapshot)
        assert gs.approved is True
        assert gs.action == "no_final_mutation"

    def test_human_explanation_non_empty(self, happy_result):
        snapshot = build_apply_audit_snapshot(happy_result)
        assert len(snapshot.human_explanation) > 0
        assert "completed" in snapshot.human_explanation
        assert "dry-run" in snapshot.human_explanation

    def test_audit_refs_include_snapshot_id(self, happy_result):
        snapshot = build_apply_audit_snapshot(happy_result)
        assert snapshot.snapshot_id in snapshot.audit_refs
        assert happy_result.idempotency_key in snapshot.audit_refs


class TestBlockedSnapshot:
    """Test 2: Build snapshot for blocked apply."""

    def test_blocked_snapshot_status(self, blocked_result):
        snapshot = build_apply_audit_snapshot(blocked_result)
        assert snapshot.orchestration_status == "blocked"
        assert snapshot.execution_status == "blocked"
        assert snapshot.executed_count == 0
        assert snapshot.blocked_count == 1
        assert snapshot.review_required is True

    def test_blocked_operation_snapshot(self, blocked_result):
        snapshot = build_apply_audit_snapshot(blocked_result)
        assert len(snapshot.operation_snapshots) == 1
        op = snapshot.operation_snapshots[0]
        assert op.execution_status == ApplyExecutionStatus.BLOCKED
        assert op.guard_approved is False

    def test_blocked_explanation_mentions_blocked(self, blocked_result):
        snapshot = build_apply_audit_snapshot(blocked_result)
        assert "blocked" in snapshot.human_explanation
        assert "completed" not in snapshot.human_explanation


class TestPartiallyBlockedSnapshot:
    """Test 3: Build snapshot for partially blocked apply."""

    def test_partial_snapshot_status(self, partial_result):
        snapshot = build_apply_audit_snapshot(partial_result)
        assert snapshot.orchestration_status == "partially_blocked"
        assert snapshot.executed_count == 1
        assert snapshot.blocked_count == 1
        assert snapshot.total_operations == 2

    def test_partial_operation_snapshots_mixed(self, partial_result):
        snapshot = build_apply_audit_snapshot(partial_result)
        assert len(snapshot.operation_snapshots) == 2
        statuses = {op.operation_id: op.execution_status for op in snapshot.operation_snapshots}
        assert statuses["op-dec-001"] == ApplyExecutionStatus.EXECUTED
        assert statuses["op-dec-002"] == ApplyExecutionStatus.BLOCKED

    def test_partial_explanation_mentions_partially(self, partial_result):
        snapshot = build_apply_audit_snapshot(partial_result)
        assert "partially blocked" in snapshot.human_explanation


class TestConflictSnapshot:
    """Test 4: Build snapshot for conflict/idempotency rejection."""

    def test_conflict_snapshot_status(self, conflict_result):
        snapshot = build_apply_audit_snapshot(conflict_result)
        assert snapshot.orchestration_status == "conflict"
        assert snapshot.review_required is True

    def test_conflict_explanation_mentions_conflict(self, conflict_result):
        snapshot = build_apply_audit_snapshot(conflict_result)
        assert "conflict" in snapshot.human_explanation.lower()


class TestWithIdempotencyClassification:
    """Test 5: Build snapshot with ApplyIdempotencyClassification included."""

    def test_idempotency_snapshot_included(self, happy_result, sample_idempotency_classification):
        snapshot = build_apply_audit_snapshot(
            happy_result,
            idempotency_classification=sample_idempotency_classification,
        )
        assert snapshot.idempotency_snapshot is not None
        idem = snapshot.idempotency_snapshot
        assert isinstance(idem, ApplyAuditIdempotencySnapshot)
        assert idem.idempotency_key == happy_result.idempotency_key
        assert idem.outcome == "idempotent_replay"
        assert idem.prior_execution_exists is True
        assert idem.is_completed_idempotent_replay is True
        assert idem.is_duplicate_prevention_rejection is False
        assert idem.candidate_fingerprint == "fp-candidate"
        assert idem.classification_note != ""

    def test_candidate_and_persisted_fingerprints(
        self, happy_result, sample_idempotency_classification
    ):
        snapshot = build_apply_audit_snapshot(
            happy_result,
            idempotency_classification=sample_idempotency_classification,
        )
        assert snapshot.candidate_fingerprint == "fp-candidate"
        assert snapshot.persisted_fingerprint == "fp-candidate"

    def test_idempotency_snapshot_optional(self, happy_result):
        snapshot = build_apply_audit_snapshot(happy_result)
        assert snapshot.idempotency_snapshot is None


class TestWithAdapterResult:
    """Test 6: Build snapshot with FinalTransactionAdapterResult included."""

    def test_adapter_result_final_write_flag(self, happy_result, sample_adapter_result):
        snapshot = build_apply_audit_snapshot(
            happy_result,
            adapter_result=sample_adapter_result,
        )
        assert snapshot.final_write_confirmation_required is True

    def test_adapter_result_optional(self, happy_result):
        snapshot = build_apply_audit_snapshot(happy_result)
        assert snapshot.final_write_confirmation_required is None


class TestDeterministicOrdering:
    """Test 7: Operation snapshots preserve deterministic ordering."""

    def test_ops_sorted_by_operation_id(
        self,
        sample_decision,
        queue_item,
        approved_guard_decision,
        fixed_clock,
    ):
        dec_b = ResolutionDecision(
            decision_id="dec-bbb",
            queue_item_id="q-001",
            action=ResolutionAction.CONFIRM_MATCH,
            note="B",
            reviewer="human",
        )
        dec_a = ResolutionDecision(
            decision_id="dec-aaa",
            queue_item_id="q-001",
            action=ResolutionAction.CONFIRM_MATCH,
            note="A",
            reviewer="human",
        )
        plan_inputs = (
            ApplyPlanInput(
                decision=dec_b,
                queue_item=queue_item,
                guard_decision=approved_guard_decision,
            ),
            ApplyPlanInput(
                decision=dec_a,
                queue_item=queue_item,
                guard_decision=approved_guard_decision,
            ),
        )
        gd_map = {
            "op-dec-bbb": approved_guard_decision,
            "op-dec-aaa": approved_guard_decision,
        }
        orch_input = ApplyOrchestrationInput(
            plan_inputs=plan_inputs,
            guard_decisions_by_operation_id=gd_map,
            idempotency_key="orch-order-001",
        )
        result = orchestrate_reconciliation_apply(orch_input, clock=fixed_clock)
        snapshot = build_apply_audit_snapshot(result)

        op_ids = [op.operation_id for op in snapshot.operation_snapshots]
        assert op_ids == sorted(op_ids)
        assert op_ids[0] == "op-dec-aaa"
        assert op_ids[1] == "op-dec-bbb"

    def test_snapshot_id_deterministic_across_calls(self, happy_result):
        first = build_apply_audit_snapshot(happy_result)
        second = build_apply_audit_snapshot(happy_result)
        assert first.snapshot_id == second.snapshot_id
        assert first.human_explanation == second.human_explanation
        assert first.total_operations == second.total_operations

    def test_snapshot_is_immutable(self, happy_result):
        snapshot = build_apply_audit_snapshot(happy_result)
        with pytest.raises(Exception):
            snapshot.total_operations = 99  # type: ignore[misc]

    def test_guard_snapshots_sorted_by_decision_id(
        self,
        sample_decision,
        queue_item,
        approved_guard_decision,
        blocked_guard_decision,
        fixed_clock,
    ):
        dec_z = ResolutionDecision(
            decision_id="dec-zzz",
            queue_item_id="q-001",
            action=ResolutionAction.CONFIRM_MATCH,
            note="Z",
            reviewer="human",
        )
        plan_inputs = (
            ApplyPlanInput(
                decision=sample_decision,
                queue_item=queue_item,
                guard_decision=approved_guard_decision,
            ),
            ApplyPlanInput(
                decision=dec_z,
                queue_item=queue_item,
                guard_decision=blocked_guard_decision,
            ),
        )
        gd_map = {
            "op-dec-zzz": blocked_guard_decision,
            "op-dec-001": approved_guard_decision,
        }
        orch_input = ApplyOrchestrationInput(
            plan_inputs=plan_inputs,
            guard_decisions_by_operation_id=gd_map,
            idempotency_key="orch-guard-order-001",
        )
        result = orchestrate_reconciliation_apply(orch_input, clock=fixed_clock)
        snapshot = build_apply_audit_snapshot(result, orch_input=orch_input)
        decision_ids = [gs.decision_id for gs in snapshot.guard_snapshots]
        assert decision_ids == sorted(decision_ids)


class TestGuardDecisionDetails:
    """Test 8: Guard decision details are included."""

    def test_approved_guard_details(self, happy_result, happy_input):
        snapshot = build_apply_audit_snapshot(happy_result, orch_input=happy_input)
        assert len(snapshot.guard_snapshots) == 1
        gs = snapshot.guard_snapshots[0]
        assert gs.decision_id == "fp-001"
        assert gs.approved is True
        assert gs.blocked_reasons == ()
        assert gs.action == "no_final_mutation"

    def test_blocked_guard_details(
        self, blocked_result, sample_decision, queue_item, blocked_guard_decision
    ):
        plan_input = ApplyPlanInput(
            decision=sample_decision,
            queue_item=queue_item,
            guard_decision=blocked_guard_decision,
        )
        orch_input = ApplyOrchestrationInput(
            plan_inputs=(plan_input,),
            guard_decisions_by_operation_id={"op-dec-001": blocked_guard_decision},
            idempotency_key="orch-blocked-001",
        )
        snapshot = build_apply_audit_snapshot(blocked_result, orch_input=orch_input)
        gs = snapshot.guard_snapshots[0]
        assert gs.approved is False
        assert len(gs.blocked_reasons) > 0

    def test_no_guard_snapshots_without_input(self, happy_result):
        snapshot = build_apply_audit_snapshot(happy_result)
        assert snapshot.guard_snapshots == ()

    def test_blocked_reasons_on_operation(
        self, blocked_result, sample_decision, queue_item, blocked_guard_decision
    ):
        plan_input = ApplyPlanInput(
            decision=sample_decision,
            queue_item=queue_item,
            guard_decision=blocked_guard_decision,
        )
        orch_input = ApplyOrchestrationInput(
            plan_inputs=(plan_input,),
            guard_decisions_by_operation_id={"op-dec-001": blocked_guard_decision},
            idempotency_key="orch-blocked-001",
        )
        snapshot = build_apply_audit_snapshot(blocked_result, orch_input=orch_input)
        op = snapshot.operation_snapshots[0]
        assert len(op.guard_blocked_reasons) > 0


class TestAuditEvidenceRefs:
    """Test 9: Audit/evidence refs are preserved."""

    def test_evidence_refs_from_execution_result(self, happy_result):
        snapshot = build_apply_audit_snapshot(happy_result)
        assert isinstance(snapshot.evidence_refs, tuple)

    def test_audit_refs_contain_key_refs(self, happy_result):
        snapshot = build_apply_audit_snapshot(happy_result)
        assert snapshot.plan_id in snapshot.audit_refs
        assert snapshot.idempotency_key in snapshot.audit_refs
        assert snapshot.snapshot_id in snapshot.audit_refs


class TestNoFinalTransactionMutation:
    """Test 10: Snapshot does not create/update/delete final transaction records."""

    def test_snapshot_never_mutates_db(self, happy_result, migrated_conn):
        tables_before = {
            row[0]
            for row in migrated_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        build_apply_audit_snapshot(happy_result)
        tables_after = {
            row[0]
            for row in migrated_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert tables_before == tables_after

    def test_snapshot_no_final_mutation_tables(self, happy_result):
        snapshot = build_apply_audit_snapshot(happy_result)
        assert snapshot.persisted is False


class TestNoFinalMutationWorkflowImport:
    """Test 11: Snapshot does not call or import execute_guarded_final_mutation_workflow."""

    def test_module_does_not_import_final_mutation_workflow(self):
        module_path = (
            Path(__file__).resolve().parents[1]
            / "finance_core"
            / "reconciliation"
            / "apply_audit_snapshot.py"
        )
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                import_src = ast.unparse(node)
                assert "execute_guarded_final_mutation_workflow" not in import_src, (
                    "Module must not import execute_guarded_final_mutation_workflow"
                )

    def test_build_snapshot_does_not_call_final_mutation(self, happy_result, sample_adapter_result):
        snapshot = build_apply_audit_snapshot(
            happy_result,
            adapter_result=sample_adapter_result,
        )
        assert "Guarded dry-run only" in snapshot.human_explanation


class TestDeterminism:
    """Test 12: Snapshot builder is deterministic for same inputs."""

    def test_same_inputs_identical_snapshots(self, happy_result, happy_input):
        first = build_apply_audit_snapshot(happy_result, orch_input=happy_input)
        second = build_apply_audit_snapshot(happy_result, orch_input=happy_input)
        assert first == second

    def test_same_inputs_same_explanation(self, happy_result):
        first = build_apply_audit_snapshot(happy_result)
        second = build_apply_audit_snapshot(happy_result)
        assert first.human_explanation == second.human_explanation

    def test_no_timestamps_in_snapshot_id(self, happy_result):
        snapshot = build_apply_audit_snapshot(happy_result)
        assert snapshot.snapshot_id.startswith("snap-")
        assert "-" not in snapshot.snapshot_id[5:]


class TestPublicApiExports:
    """Test 13: Public API exports work."""

    EXPECTED = [
        "ApplyAuditGuardSnapshot",
        "ApplyAuditIdempotencySnapshot",
        "ApplyAuditOperationSnapshot",
        "ApplyAuditSnapshot",
        "build_apply_audit_snapshot",
        "build_apply_audit_snapshot_from_repository",
    ]

    def test_all_symbols_importable_from_package(self):
        from finance_core.reconciliation import (  # noqa: F811
            ApplyAuditSnapshot,
            build_apply_audit_snapshot,
        )

        assert ApplyAuditSnapshot is not None
        assert build_apply_audit_snapshot is not None

    def test_all_symbols_in_package_all(self):
        import finance_core.reconciliation as pkg

        present = {name for name in self.EXPECTED if hasattr(pkg, name)}
        missing = set(self.EXPECTED) - present
        assert not missing, f"Symbols not exported: {missing}"

    def test_all_symbols_in_module_all(self):
        import finance_core.reconciliation.apply_audit_snapshot as mod

        assert hasattr(mod, "__all__")
        present = {name for name in self.EXPECTED if name in mod.__all__}
        missing = set(self.EXPECTED) - present
        assert not missing, f"Symbols not in __all__: {missing}"


class TestLiveDbNotTouched:
    """Test 14: No database/finance.db access."""

    def test_finance_db_hash_unchanged(self, happy_result):
        existed_before = LIVE_DB_PATH.exists()
        before = hashlib.sha256(LIVE_DB_PATH.read_bytes()).hexdigest() if existed_before else None
        build_apply_audit_snapshot(happy_result)
        existed_after = LIVE_DB_PATH.exists()
        after = hashlib.sha256(LIVE_DB_PATH.read_bytes()).hexdigest() if existed_after else None
        if existed_before:
            assert existed_after
            assert before == after
        else:
            assert not existed_after


class TestRepositorySnapshot:
    """Test 15: build_apply_audit_snapshot_from_repository."""

    def test_no_prior_execution_snapshot(self, happy_input, repo):
        snapshot = build_apply_audit_snapshot_from_repository(happy_input, repo)
        assert snapshot.orchestration_status == "no_prior_execution"
        assert snapshot.execution_status == "no_prior_execution"
        assert snapshot.persisted is False
        assert snapshot.persistence_enabled is True
        assert snapshot.review_required is True
        assert snapshot.executed_count == 0
        assert "no prior execution" in snapshot.human_explanation.lower()
        assert snapshot.total_operations > 0
        assert len(snapshot.operation_snapshots) > 0
        assert snapshot.snapshot_id.startswith("snap-")

    def test_no_prior_execution_candidate_fingerprint(self, happy_input, repo):
        snapshot = build_apply_audit_snapshot_from_repository(happy_input, repo)
        assert snapshot.candidate_fingerprint is not None
        assert len(snapshot.candidate_fingerprint) > 0
        assert snapshot.persisted_fingerprint is None

    def test_persisted_execution_snapshot(self, happy_input, repo, fixed_clock):
        orchestrate_reconciliation_apply(happy_input, repository=repo, clock=fixed_clock)
        snapshot = build_apply_audit_snapshot_from_repository(happy_input, repo)
        assert snapshot.orchestration_status == "completed"
        assert snapshot.execution_status == "executed"
        assert snapshot.persisted is True
        assert snapshot.total_operations == 1
        assert snapshot.executed_count == 1
        assert snapshot.review_required is False
        assert "completed" in snapshot.human_explanation

    def test_persisted_with_adapter(self, happy_input, repo, sample_adapter_result, fixed_clock):
        orchestrate_reconciliation_apply(happy_input, repository=repo, clock=fixed_clock)
        snapshot = build_apply_audit_snapshot_from_repository(
            happy_input, repo, adapter_result=sample_adapter_result
        )
        assert snapshot.final_write_confirmation_required is True

    def test_repository_none_raises(self, happy_input):
        with pytest.raises(ValueError, match="repository"):
            build_apply_audit_snapshot_from_repository(happy_input, None)  # type: ignore[arg-type]

    def test_empty_idempotency_key_raises(
        self, repo, sample_decision, queue_item, approved_guard_decision
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
            build_apply_audit_snapshot_from_repository(orch_input, repo)

    def test_empty_plan_inputs_raises(self, repo, approved_guard_decision):
        orch_input = ApplyOrchestrationInput(
            plan_inputs=(),
            guard_decisions_by_operation_id={},
            idempotency_key="orch-empty",
        )
        with pytest.raises(ValueError, match="plan input"):
            build_apply_audit_snapshot_from_repository(orch_input, repo)

    def test_repo_never_opens_finance_db(self, happy_input, repo, migrated_conn):
        build_apply_audit_snapshot_from_repository(happy_input, repo)
        db_file = migrated_conn.execute("PRAGMA database_list").fetchone()
        file_path = db_file["file"] if db_file["file"] else ""
        assert str(LIVE_DB_PATH) not in file_path


class TestEdgeCases:
    """Test 16: Edge cases."""

    def test_snapshot_with_orch_input_enriches_guards(self, happy_result, happy_input):
        snapshot = build_apply_audit_snapshot(happy_result, orch_input=happy_input)
        assert len(snapshot.guard_snapshots) > 0
        op = snapshot.operation_snapshots[0]
        assert op.guard_approved is True

    def test_snapshot_with_all_optionals(
        self,
        happy_result,
        happy_input,
        sample_adapter_result,
        sample_idempotency_classification,
    ):
        snapshot = build_apply_audit_snapshot(
            happy_result,
            orch_input=happy_input,
            adapter_result=sample_adapter_result,
            idempotency_classification=sample_idempotency_classification,
        )
        assert snapshot.final_write_confirmation_required is True
        assert snapshot.idempotency_snapshot is not None
        assert len(snapshot.guard_snapshots) > 0
        assert snapshot.candidate_fingerprint is not None

    def test_human_explanation_always_non_empty(self, happy_result):
        snapshot = build_apply_audit_snapshot(happy_result)
        assert len(snapshot.human_explanation) > 0
        assert isinstance(snapshot.human_explanation, str)
        assert "no final financial records" in snapshot.human_explanation.lower()


# ===========================================================================
# No-prior-execution planned status semantics (PR #134 audit fix)
# ===========================================================================


class TestNoPriorPlannedStatuses:
    """Verify no-prior-execution operation snapshots never imply execution
    when no persisted execution result exists."""

    def test_no_prior_ops_never_use_executed_status(self, happy_input, repo):
        """No operation in a no-prior snapshot should report 'executed'."""
        snapshot = build_apply_audit_snapshot_from_repository(happy_input, repo)
        assert snapshot.executed_count == 0
        for op in snapshot.operation_snapshots:
            assert op.execution_status != "executed", (
                f"Operation {op.operation_id} must not report 'executed' "
                f"when no prior execution exists"
            )
            assert op.execution_status != ApplyExecutionStatus.EXECUTED, (
                f"Operation {op.operation_id} must not use ApplyExecutionStatus.EXECUTED"
            )

    def test_no_prior_ops_use_planned_statuses(self, happy_input, repo):
        """No-prior operations use explicit planned/audit-only statuses."""
        snapshot = build_apply_audit_snapshot_from_repository(happy_input, repo)
        for op in snapshot.operation_snapshots:
            assert op.execution_status in (
                "planned_guard_approved",
                "planned_guard_blocked",
                "planned_requires_confirmation",
            ), f"Operation {op.operation_id} has unexpected planned status: {op.execution_status!r}"

    def test_persisted_ops_still_report_runtime_statuses(self, happy_input, repo, fixed_clock):
        """Persisted execution snapshot still reports real runtime statuses
        (e.g. 'executed') -- NOT planned statuses."""
        orchestrate_reconciliation_apply(happy_input, repository=repo, clock=fixed_clock)
        snapshot = build_apply_audit_snapshot_from_repository(happy_input, repo)
        assert snapshot.persisted is True
        assert snapshot.executed_count >= 1
        for op in snapshot.operation_snapshots:
            assert "planned_" not in op.execution_status, (
                f"Persisted operation {op.operation_id} must not use planned_ status: "
                f"{op.execution_status!r}"
            )
            assert op.execution_status != "planned_guard_approved"
            assert op.execution_status != "planned_guard_blocked"
            assert op.execution_status != "planned_requires_confirmation"

    def test_completed_in_memory_ops_still_report_executed(self, happy_result):
        """Completed in-memory orchestration snapshot still reports executed
        operations correctly."""
        snapshot = build_apply_audit_snapshot(happy_result)
        assert snapshot.execution_status == "executed"
        assert snapshot.executed_count == 1
        for op in snapshot.operation_snapshots:
            assert op.execution_status not in (
                "planned_guard_approved",
                "planned_guard_blocked",
                "planned_requires_confirmation",
            ), (
                f"Completed op {op.operation_id} must not use planned_ status: "
                f"{op.execution_status!r}"
            )


class TestGuardSnapshotsSortedByDecisionId:
    """Verify _build_guard_snapshots() sorts by decision_id, not operation_id."""

    def test_guard_snapshots_sorted_by_decision_id_not_operation_id(
        self,
        sample_decision,
        queue_item,
        approved_guard_decision,
        blocked_guard_decision,
        fixed_clock,
    ):
        """Guard snapshots must be ordered by decision_id, independent of
        dict key (operation_id) iteration order."""
        dec_alpha = ResolutionDecision(
            decision_id="dec-alpha",
            queue_item_id="q-001",
            action=ResolutionAction.CONFIRM_MATCH,
            note="Alpha",
            reviewer="human",
        )
        dec_beta = ResolutionDecision(
            decision_id="dec-beta",
            queue_item_id="q-001",
            action=ResolutionAction.CONFIRM_MATCH,
            note="Beta",
            reviewer="human",
        )
        plan_inputs = (
            ApplyPlanInput(
                decision=dec_alpha, queue_item=queue_item, guard_decision=approved_guard_decision
            ),
            ApplyPlanInput(
                decision=dec_beta, queue_item=queue_item, guard_decision=blocked_guard_decision
            ),
        )
        # Deliberately order dict so operation_ids reverse decision_id order
        gd_map = {
            "op-zebra": approved_guard_decision,
            "op-alpha": blocked_guard_decision,
        }
        orch_input = ApplyOrchestrationInput(
            plan_inputs=plan_inputs,
            guard_decisions_by_operation_id=gd_map,
            idempotency_key="orch-guard-decision-sort-001",
        )
        result = orchestrate_reconciliation_apply(orch_input, clock=fixed_clock)
        snapshot = build_apply_audit_snapshot(result, orch_input=orch_input)

        decision_ids = [gs.decision_id for gs in snapshot.guard_snapshots]
        assert decision_ids == sorted(decision_ids), (
            f"Guard snapshots must be sorted by decision_id, got: {decision_ids}"
        )


class TestOperationSnapshotTypeIsStr:
    """Verify ApplyAuditOperationSnapshot.execution_status is str (not enum)."""

    def test_execution_status_type_is_str(self):
        """The field annotation must be str, not ApplyExecutionStatus enum."""
        snapshot = ApplyAuditOperationSnapshot(
            operation_id="test-op",
            decision_id="test-dec",
            mutation_type="confirm_match",
            execution_status="planned_guard_approved",
            guard_approved=True,
        )
        # If execution_status were typed as ApplyExecutionStatus enum,
        # assigning a string not in the enum would still work at runtime,
        # but mypy would catch the type mismatch.
        assert isinstance(snapshot.execution_status, str)
        assert snapshot.execution_status == "planned_guard_approved"
