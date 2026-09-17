"""Tests for Reconciliation Apply Idempotency / Duplicate Prevention v1.

Covers the ``classify_reconciliation_apply_idempotency`` read-only helper
and the durable idempotency / duplicate-prevention behaviour of the
orchestrator + guarded runtime + repository across process restarts.

Covered:

  1. First apply records the execution fingerprint normally
     (``NO_PRIOR_EXECUTION`` before; one row after).
  2. Repeated apply with identical plan/fingerprint is idempotently
     recognized as the same recorded execution (``IDEMPOTENT_REPLAY``) and
     does not insert a duplicate execution or operation row.
  3. Repeated apply across a NEW SQLite connection (process restart) is
     still protected (classification + runtime replay survive restart).
  4. Same idempotency key/fingerprint with changed material operation
     payload is rejected as a conflict (``CONFLICT``), never silently
     accepted, and does not insert a duplicate row.
  5. Prior blocked execution stays ``PRIOR_BLOCKED`` on retry with the same
     inputs; it does not become eligible without new valid inputs.
  6. Prior partially-blocked execution stays ``PRIOR_PARTIALLY_BLOCKED`` on
     retry; a prior review-required (unsupported) execution stays
     ``PRIOR_REQUIRES_REVIEW``.
  7. No access to ``database/finance.db`` (temp DBs only).
  8. No import path that executes the final mutation workflow.
  9. Audit/reference information for the original execution is preserved on
     the classification (execution id, status, reason, fingerprint,
     executed_at).
 10. Public API export test for the new symbols.
 11. Invalid input (empty key, empty plan inputs) raises ``ValueError``.
 12. Determinism: same inputs produce the same classification.
 13. Candidate fingerprint matches the runtime's execution fingerprint.
 14. ``is_same_recorded_execution`` / ``is_duplicate_prevention_rejection`` /
     ``is_completed_idempotent_replay`` / ``fingerprint_matches_prior`` helpers.
"""

from __future__ import annotations

import ast
import sqlite3
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_core.reconciliation.apply_orchestrator import (
    ApplyIdempotencyOutcome,
    ApplyOrchestrationInput,
    ApplyOrchestrationStatus,
    classify_reconciliation_apply_idempotency,
    orchestrate_reconciliation_apply,
)
from finance_core.reconciliation.apply_plan import ApplyPlanInput
from finance_core.reconciliation.apply_runtime import (
    build_guarded_apply_execution_fingerprint,
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

ORCHESTRATOR_PATH = (
    Path(__file__).resolve().parents[1]
    / "finance_core"
    / "reconciliation"
    / "apply_orchestrator.py"
)


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


def _approved_orch_input(
    sample_decision: ResolutionDecision,
    queue_item: ReviewQueueItem,
    approved_guard_decision: FinalMutationGuardDecision,
    *,
    idempotency_key: str,
) -> ApplyOrchestrationInput:
    """A single-operation, guard-approved orchestration input."""
    plan_input = ApplyPlanInput(
        decision=sample_decision,
        queue_item=queue_item,
        guard_decision=approved_guard_decision,
    )
    op_id = "op-dec-001"
    return ApplyOrchestrationInput(
        plan_inputs=(plan_input,),
        guard_decisions_by_operation_id={op_id: approved_guard_decision},
        idempotency_key=idempotency_key,
    )


def _blocked_orch_input(
    sample_decision: ResolutionDecision,
    queue_item: ReviewQueueItem,
    blocked_guard_decision: FinalMutationGuardDecision,
    *,
    idempotency_key: str,
) -> ApplyOrchestrationInput:
    """A single-operation, guard-blocked orchestration input."""
    plan_input = ApplyPlanInput(
        decision=sample_decision,
        queue_item=queue_item,
        guard_decision=blocked_guard_decision,
    )
    op_id = "op-dec-001"
    return ApplyOrchestrationInput(
        plan_inputs=(plan_input,),
        guard_decisions_by_operation_id={op_id: blocked_guard_decision},
        idempotency_key=idempotency_key,
    )


def _partial_orch_input(
    sample_decision: ResolutionDecision,
    queue_item: ReviewQueueItem,
    approved_guard_decision: FinalMutationGuardDecision,
    blocked_guard_decision: FinalMutationGuardDecision,
    *,
    idempotency_key: str,
) -> ApplyOrchestrationInput:
    """A two-operation input: one approved, one blocked -> partially blocked."""
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
        idempotency_key=idempotency_key,
    )


@pytest.fixture
def migrated_conn(temp_db_path: Path) -> sqlite3.Connection:
    """A temp SQLite connection with only migration 014 applied."""
    assert temp_db_path != LIVE_DB_PATH
    conn = connect_temp_db(temp_db_path)
    apply_sql(conn, MIGRATION_014)
    conn.commit()
    return conn


@pytest.fixture
def repo(migrated_conn: sqlite3.Connection) -> GuardedApplyExecutionRepository:
    return GuardedApplyExecutionRepository(migrated_conn)


@pytest.fixture
def temp_db_path_str(temp_db_path: Path) -> str:
    """The temp DB path as a string, for reopening a fresh connection."""
    assert temp_db_path != LIVE_DB_PATH
    return str(temp_db_path)


def _reopen_repo(db_path: str) -> GuardedApplyExecutionRepository:
    """Open a brand-new SQLite connection to the same temp DB file and wrap
    it in a fresh repository. Simulates a process restart: no in-memory
    runtime cache, only durable persisted state is visible.

    The migration is re-applied (it uses ``CREATE TABLE IF NOT EXISTS``) so
    a reopened connection to an already-migrated file is schema-ready
    without seeding or mutating any data.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_sql(conn, MIGRATION_014)
    conn.commit()
    return GuardedApplyExecutionRepository(conn)


def _execution_row_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM reconciliation_guarded_apply_executions").fetchone()[
        0
    ]


def _operation_row_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM reconciliation_guarded_apply_operation_results"
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Test 1: First apply records the execution fingerprint normally
# ---------------------------------------------------------------------------


class TestFirstApplyRecordsFingerprint:
    def test_no_prior_execution_before_first_apply(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
    ):
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-first-001",
        )
        classification = classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        assert classification.outcome is ApplyIdempotencyOutcome.NO_PRIOR_EXECUTION
        assert classification.prior_execution_exists is False
        assert classification.prior_execution_id is None
        assert classification.prior_execution_status is None
        assert classification.prior_execution_fingerprint is None
        assert classification.candidate_fingerprint != ""
        assert classification.fingerprint_matches_prior is False
        assert classification.is_same_recorded_execution is False
        assert classification.is_duplicate_prevention_rejection is False
        assert classification.is_completed_idempotent_replay is False

    def test_first_apply_persists_one_execution_row(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        migrated_conn: sqlite3.Connection,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-first-002",
        )
        assert _execution_row_count(migrated_conn) == 0
        result = orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        assert result.status is ApplyOrchestrationStatus.COMPLETED
        assert result.persisted is True
        assert _execution_row_count(migrated_conn) == 1
        assert _operation_row_count(migrated_conn) == 1

    def test_candidate_fingerprint_matches_runtime(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
    ):
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-fp-match",
        )
        classification = classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        # Build the plan + fingerprint the same way the orchestrator does,
        # then confirm the classification's candidate fingerprint matches.
        from finance_core.reconciliation.apply_plan import build_reconciliation_apply_plan

        plan = build_reconciliation_apply_plan(
            list(orch_input.plan_inputs),
            plan_label=orch_input.plan_label,
        )
        runtime_fp = build_guarded_apply_execution_fingerprint(
            plan,
            orch_input.guard_decisions_by_operation_id,
            orch_input.idempotency_key,
        )
        assert classification.candidate_fingerprint == runtime_fp


# ---------------------------------------------------------------------------
# Test 2: Repeated apply with identical plan/fingerprint is idempotent
# ---------------------------------------------------------------------------


class TestIdempotentReplay:
    def test_replay_classified_as_idempotent_replay(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-replay-001",
        )
        orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        classification = classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        assert classification.outcome is ApplyIdempotencyOutcome.IDEMPOTENT_REPLAY
        assert classification.prior_execution_exists is True
        assert classification.prior_execution_status is ApplyExecutionStatus.EXECUTED
        assert classification.fingerprint_matches_prior is True
        assert classification.is_same_recorded_execution is True
        assert classification.is_duplicate_prevention_rejection is False
        assert classification.is_completed_idempotent_replay is True

    def test_replay_does_not_insert_duplicate_rows(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        migrated_conn: sqlite3.Connection,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-replay-002",
        )
        first = orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        assert first.persisted is True
        assert _execution_row_count(migrated_conn) == 1
        assert _operation_row_count(migrated_conn) == 1

        second = orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        assert second.status is ApplyOrchestrationStatus.COMPLETED
        assert second.persisted is False  # no new row inserted
        # No duplicate execution or operation rows.
        assert _execution_row_count(migrated_conn) == 1
        assert _operation_row_count(migrated_conn) == 1

    def test_replay_returns_same_execution_status(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-replay-003",
        )
        first = orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        second = orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        assert first.execution_result.execution_status is second.execution_result.execution_status
        # Durable replay: the orchestrator owns the repository, so each call
        # builds a fresh runtime and reloads the cached result from the
        # repository. The replayed result is value-equal (same status, same
        # persisted executed_at) but not necessarily the same object
        # identity as the first in-memory result.
        assert second.execution_result == first.execution_result
        assert second.execution_result.executed_at == first.execution_result.executed_at


# ---------------------------------------------------------------------------
# Test 3: Repeated apply across a NEW SQLite connection (process restart)
# ---------------------------------------------------------------------------


class TestCrossConnectionPersistence:
    def test_classification_survives_new_connection(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        temp_db_path_str: str,
        fixed_clock,
    ):
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-restart-001",
        )
        # First process: apply and persist.
        orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        # Simulate a process restart: open a brand-new connection + repo.
        restart_repo = _reopen_repo(temp_db_path_str)
        try:
            classification = classify_reconciliation_apply_idempotency(
                orch_input, repository=restart_repo
            )
            assert classification.outcome is ApplyIdempotencyOutcome.IDEMPOTENT_REPLAY
            assert classification.prior_execution_exists is True
            assert classification.prior_execution_status is ApplyExecutionStatus.EXECUTED
            assert classification.fingerprint_matches_prior is True
        finally:
            restart_repo._conn.close()

    def test_runtime_replay_survives_new_connection(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        temp_db_path_str: str,
        fixed_clock,
    ):
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-restart-002",
        )
        # First process: apply and persist.
        first_repo = _reopen_repo(temp_db_path_str)
        first = orchestrate_reconciliation_apply(
            orch_input, repository=first_repo, clock=fixed_clock
        )
        assert first.persisted is True
        first_repo._conn.close()

        # Restart: fresh runtime + fresh repo (cold in-memory cache).
        restart_repo = _reopen_repo(temp_db_path_str)
        try:
            second = orchestrate_reconciliation_apply(
                orch_input, repository=restart_repo, clock=fixed_clock
            )
            assert second.status is ApplyOrchestrationStatus.COMPLETED
            # Durable idempotency: no new row on replay across restart.
            assert second.persisted is False
        finally:
            restart_repo._conn.close()

    def test_conflict_survives_new_connection(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        temp_db_path_str: str,
        fixed_clock,
    ):
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-restart-conflict",
        )
        first_repo = _reopen_repo(temp_db_path_str)
        orchestrate_reconciliation_apply(orch_input, repository=first_repo, clock=fixed_clock)
        first_repo._conn.close()

        # Build a materially different plan reusing the same idempotency key.
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
            idempotency_key="idem-restart-conflict",
        )

        restart_repo = _reopen_repo(temp_db_path_str)
        try:
            classification = classify_reconciliation_apply_idempotency(
                other_input, repository=restart_repo
            )
            assert classification.outcome is ApplyIdempotencyOutcome.CONFLICT
            assert classification.is_same_recorded_execution is False
            assert classification.is_duplicate_prevention_rejection is True
        finally:
            restart_repo._conn.close()


# ---------------------------------------------------------------------------
# Test 4: Same key + changed material payload -> conflict, no duplicate row
# ---------------------------------------------------------------------------


class TestConflictOnChangedPayload:
    def test_conflict_classification(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        first_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-conflict-001",
        )
        orchestrate_reconciliation_apply(first_input, repository=repo, clock=fixed_clock)

        # Materially different plan content, same idempotency key.
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
            idempotency_key="idem-conflict-001",
        )
        classification = classify_reconciliation_apply_idempotency(other_input, repository=repo)
        assert classification.outcome is ApplyIdempotencyOutcome.CONFLICT
        assert classification.prior_execution_exists is True
        assert classification.prior_execution_status is ApplyExecutionStatus.EXECUTED
        assert classification.fingerprint_matches_prior is False
        assert classification.is_same_recorded_execution is False
        assert classification.is_duplicate_prevention_rejection is True

    def test_conflict_runtime_rejects_and_inserts_no_duplicate(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        migrated_conn: sqlite3.Connection,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        first_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-conflict-002",
        )
        orchestrate_reconciliation_apply(first_input, repository=repo, clock=fixed_clock)
        assert _execution_row_count(migrated_conn) == 1

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
            idempotency_key="idem-conflict-002",
        )
        result = orchestrate_reconciliation_apply(other_input, repository=repo, clock=fixed_clock)
        assert result.status is ApplyOrchestrationStatus.CONFLICT
        assert result.status is not ApplyOrchestrationStatus.COMPLETED
        assert result.persisted is False
        # The conflicting attempt must not insert a duplicate execution row.
        assert _execution_row_count(migrated_conn) == 1
        # And must not insert a duplicate operation row.
        assert _operation_row_count(migrated_conn) == 1

    def test_conflict_runtime_result_preserves_prior_audit(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        first_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-conflict-003",
        )
        orchestrate_reconciliation_apply(first_input, repository=repo, clock=fixed_clock)

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
            idempotency_key="idem-conflict-003",
        )
        classification = classify_reconciliation_apply_idempotency(other_input, repository=repo)
        # The prior (original) execution audit is preserved on the
        # classification even though the new attempt is a conflict.
        assert classification.prior_execution_id is not None
        assert classification.prior_execution_id.startswith("exec-")
        assert classification.prior_execution_fingerprint is not None
        assert classification.prior_executed_at == fixed_clock()


# ---------------------------------------------------------------------------
# Test 5: Prior blocked execution stays blocked on retry (same inputs)
# ---------------------------------------------------------------------------


class TestPriorBlocked:
    def test_prior_blocked_classification(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        blocked_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        orch_input = _blocked_orch_input(
            sample_decision,
            queue_item,
            blocked_guard_decision,
            idempotency_key="idem-blocked-001",
        )
        result = orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        assert result.status is ApplyOrchestrationStatus.BLOCKED
        assert result.persisted is True

        classification = classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        assert classification.outcome is ApplyIdempotencyOutcome.PRIOR_BLOCKED
        assert classification.prior_execution_status is ApplyExecutionStatus.BLOCKED
        assert classification.fingerprint_matches_prior is True
        # A blocked prior is the same recorded execution (same fingerprint),
        # but it is NOT an idempotent replay of a completed execution.
        assert classification.is_same_recorded_execution is True
        assert classification.is_duplicate_prevention_rejection is False
        assert classification.is_completed_idempotent_replay is False

    def test_prior_blocked_retry_stays_blocked_no_new_row(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        blocked_guard_decision: FinalMutationGuardDecision,
        migrated_conn: sqlite3.Connection,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        orch_input = _blocked_orch_input(
            sample_decision,
            queue_item,
            blocked_guard_decision,
            idempotency_key="idem-blocked-002",
        )
        first = orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        assert first.status is ApplyOrchestrationStatus.BLOCKED
        assert _execution_row_count(migrated_conn) == 1

        # Retry with identical inputs: must stay blocked, no new row.
        second = orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        assert second.status is ApplyOrchestrationStatus.BLOCKED
        assert second.status is not ApplyOrchestrationStatus.COMPLETED
        assert second.persisted is False
        assert _execution_row_count(migrated_conn) == 1

    def test_prior_blocked_does_not_become_eligible_without_new_inputs(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        blocked_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        orch_input = _blocked_orch_input(
            sample_decision,
            queue_item,
            blocked_guard_decision,
            idempotency_key="idem-blocked-003",
        )
        orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        # The classification for the SAME inputs must not report a
        # completed/idempotent-replay outcome. The blocked state is
        # preserved, not upgraded to eligible.
        classification = classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        assert classification.outcome is not ApplyIdempotencyOutcome.IDEMPOTENT_REPLAY
        assert classification.outcome is ApplyIdempotencyOutcome.PRIOR_BLOCKED


# ---------------------------------------------------------------------------
# Test 6: Prior partially-blocked / review-required stay review-required
# ---------------------------------------------------------------------------


class TestPriorPartiallyBlockedAndReviewRequired:
    def test_prior_partially_blocked_classification(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        blocked_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        orch_input = _partial_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            blocked_guard_decision,
            idempotency_key="idem-partial-001",
        )
        result = orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        assert result.status is ApplyOrchestrationStatus.PARTIALLY_BLOCKED
        assert result.persisted is True

        classification = classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        assert classification.outcome is ApplyIdempotencyOutcome.PRIOR_PARTIALLY_BLOCKED
        assert classification.prior_execution_status is ApplyExecutionStatus.PARTIALLY_BLOCKED
        assert classification.fingerprint_matches_prior is True
        assert classification.is_same_recorded_execution is True
        assert classification.is_duplicate_prevention_rejection is False

    def test_prior_partially_blocked_retry_remains_review_required(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        blocked_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        orch_input = _partial_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            blocked_guard_decision,
            idempotency_key="idem-partial-002",
        )
        orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        second = orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        assert second.status is ApplyOrchestrationStatus.PARTIALLY_BLOCKED
        assert second.status is not ApplyOrchestrationStatus.COMPLETED
        # Retry is a durable replay: no new row, still review-required.
        assert second.persisted is False
        assert second.review_summary.requires_human_review is True

    def test_prior_unsupported_classification(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        # A prior UNSUPPORTED execution cannot be produced through the
        # orchestrator (the plan builder requires non-empty inputs and the
        # orchestrator requires non-empty plan_inputs), but it can be
        # persisted directly via the public repository boundary -- for
        # example, when a caller records an unsupported empty-plan attempt
        # for audit. Persist such a prior, then confirm the public
        # classification maps a same-fingerprint retry to
        # PRIOR_REQUIRES_REVIEW (remains review-required, not completed).
        from finance_core.reconciliation.apply_plan import build_reconciliation_apply_plan
        from finance_core.reconciliation.models import GuardedApplyExecutionResult

        orch_input = ApplyOrchestrationInput(
            plan_inputs=(
                ApplyPlanInput(
                    decision=sample_decision,
                    queue_item=queue_item,
                    guard_decision=approved_guard_decision,
                ),
            ),
            guard_decisions_by_operation_id={"op-dec-001": approved_guard_decision},
            idempotency_key="idem-unsupported-001",
        )
        plan = build_reconciliation_apply_plan(
            list(orch_input.plan_inputs), plan_label=orch_input.plan_label
        )
        fingerprint = build_guarded_apply_execution_fingerprint(
            plan,
            orch_input.guard_decisions_by_operation_id,
            orch_input.idempotency_key,
        )
        unsupported_result = GuardedApplyExecutionResult(
            plan_id=plan.plan_id,
            idempotency_key=orch_input.idempotency_key,
            execution_status=ApplyExecutionStatus.UNSUPPORTED,
            results=(),
            total_operations=0,
            operated_executed=0,
            operated_blocked=0,
            operated_skipped=0,
            block_reason="Plan contains no operations to execute.",
            guard_decision_refs=(),
            executed_at=fixed_clock(),
            audit_trail={"plan_id": plan.plan_id, "runtime_version": "v1"},
            is_dry_run=True,
        )
        inserted = repo.save_execution_result(unsupported_result, execution_fingerprint=fingerprint)
        assert inserted is True

        classification = classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        assert classification.outcome is ApplyIdempotencyOutcome.PRIOR_REQUIRES_REVIEW
        assert classification.prior_execution_status is ApplyExecutionStatus.UNSUPPORTED
        assert classification.fingerprint_matches_prior is True
        # A review-required prior is the same recorded execution (same
        # fingerprint) but is not an idempotent replay of a completed run.
        assert classification.is_same_recorded_execution is True
        assert classification.is_duplicate_prevention_rejection is False
        assert classification.is_completed_idempotent_replay is False

    def test_prior_conflict_classification_kept_review_required(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        # First apply a normal plan, persisting an EXECUTED execution.
        first_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-prior-conflict",
        )
        orchestrate_reconciliation_apply(first_input, repository=repo, clock=fixed_clock)
        # Re-apply with a materially different payload -> runtime CONFLICT.
        # The runtime does NOT persist CONFLICT results, so the persisted
        # prior remains EXECUTED. Classifying the conflicting payload
        # therefore reports CONFLICT (different fingerprint), and the
        # prior_execution_status remains EXECUTED -- this confirms a
        # conflicting retry is safely rejected and the prior recorded
        # execution is preserved, not overwritten.
        other_decision = ResolutionDecision(
            decision_id="dec-other",
            queue_item_id="q-001",
            action=ResolutionAction.MARK_STATEMENT_ONLY,
            note="Different.",
            reviewer="human",
        )
        conflict_input = ApplyOrchestrationInput(
            plan_inputs=(
                ApplyPlanInput(
                    decision=other_decision,
                    queue_item=queue_item,
                    guard_decision=approved_guard_decision,
                ),
            ),
            guard_decisions_by_operation_id={"op-dec-other": approved_guard_decision},
            idempotency_key="idem-prior-conflict",
        )
        result = orchestrate_reconciliation_apply(
            conflict_input, repository=repo, clock=fixed_clock
        )
        assert result.status is ApplyOrchestrationStatus.CONFLICT
        assert result.review_summary.requires_human_review is True
        assert result.status is not ApplyOrchestrationStatus.COMPLETED

        classification = classify_reconciliation_apply_idempotency(conflict_input, repository=repo)
        assert classification.outcome is ApplyIdempotencyOutcome.CONFLICT
        # The prior recorded execution is preserved and still EXECUTED.
        assert classification.prior_execution_status is ApplyExecutionStatus.EXECUTED


# ---------------------------------------------------------------------------
# Test 7: No access to database/finance.db (temp DBs only)
# ---------------------------------------------------------------------------


class TestNoLiveDbAccess:
    def test_classification_uses_temp_db_only(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        migrated_conn: sqlite3.Connection,
        repo: GuardedApplyExecutionRepository,
    ):
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-nolivedb-001",
        )
        classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        db_file = migrated_conn.execute("PRAGMA database_list").fetchone()
        file_path = db_file["file"] if db_file["file"] else ""
        assert str(LIVE_DB_PATH) not in file_path

    def test_finance_db_not_modified_by_classification(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
    ):
        import hashlib

        def _sha(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-nolivedb-002",
        )
        existed_before = LIVE_DB_PATH.exists()
        before = _sha(LIVE_DB_PATH) if existed_before else None
        # Run several classifications + one apply to exercise both paths.
        classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        orchestrate_reconciliation_apply(
            orch_input,
            repository=repo,
            clock=lambda: "2024-12-15T12:00:00.000000+00:00",
        )
        classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        existed_after = LIVE_DB_PATH.exists()
        after = _sha(LIVE_DB_PATH) if existed_after else None

        if existed_before:
            assert existed_after, "must not delete database/finance.db"
            assert before == after, "must not modify database/finance.db"
        else:
            assert not existed_after, "must not create database/finance.db"


# ---------------------------------------------------------------------------
# Test 8: No import path that executes the final mutation workflow
# ---------------------------------------------------------------------------


class TestNoFinalMutationWorkflowImport:
    def test_orchestrator_does_not_import_workflow(self):
        """The orchestrator module must not import the final mutation
        workflow, so the idempotency classification helper cannot reach it.

        This is a static AST check over the orchestrator source. It does
        not manipulate ``sys.modules['apply_orchestrator']`` (clearing that
        cache would re-create the enum classes and break identity checks
        in other tests in the same session); only the ``final_mutation_
        workflow`` cache is cleared to keep the check hermetic.
        """
        for key in list(sys.modules.keys()):
            if (
                "final_mutation_workflow" in key
                and key != "finance_core.reconciliation.final_mutation_workflow"
            ):
                del sys.modules[key]

        source = ORCHESTRATOR_PATH.read_text(encoding="utf-8")
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
        assert "execute_guarded_final_mutation_workflow" not in imported_names

    def test_classification_does_not_create_final_transaction_tables(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        migrated_conn: sqlite3.Connection,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-notables-001",
        )
        orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        classify_reconciliation_apply_idempotency(orch_input, repository=repo)

        tables = {
            row[0]
            for row in migrated_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "reconciliation_final_mutation_transactions" not in tables

    def test_classification_is_read_only_no_new_rows(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        migrated_conn: sqlite3.Connection,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-readonly-001",
        )
        orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        rows_before = _execution_row_count(migrated_conn)
        op_rows_before = _operation_row_count(migrated_conn)
        # Classification is read-only: it must not insert or change rows.
        classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        assert _execution_row_count(migrated_conn) == rows_before
        assert _operation_row_count(migrated_conn) == op_rows_before


# ---------------------------------------------------------------------------
# Test 9: Audit/reference information for the original execution preserved
# ---------------------------------------------------------------------------


class TestAuditEvidencePreserved:
    def test_prior_execution_id_matches_persisted_row(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        migrated_conn: sqlite3.Connection,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-audit-001",
        )
        orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        classification = classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        persisted_id = migrated_conn.execute(
            "SELECT execution_id FROM reconciliation_guarded_apply_executions "
            "WHERE idempotency_key = ?",
            (orch_input.idempotency_key,),
        ).fetchone()[0]
        assert classification.prior_execution_id == persisted_id

    def test_prior_fingerprint_matches_persisted_fingerprint(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        migrated_conn: sqlite3.Connection,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-audit-002",
        )
        orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        classification = classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        persisted_fp = migrated_conn.execute(
            "SELECT execution_fingerprint FROM reconciliation_guarded_apply_executions "
            "WHERE idempotency_key = ?",
            (orch_input.idempotency_key,),
        ).fetchone()[0]
        assert classification.prior_execution_fingerprint == persisted_fp
        assert classification.candidate_fingerprint == persisted_fp

    def test_prior_executed_at_and_status_preserved(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-audit-003",
        )
        orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        classification = classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        assert classification.prior_executed_at == fixed_clock()
        assert classification.prior_execution_status is ApplyExecutionStatus.EXECUTED

    def test_prior_block_reason_preserved(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        blocked_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        orch_input = _blocked_orch_input(
            sample_decision,
            queue_item,
            blocked_guard_decision,
            idempotency_key="idem-audit-004",
        )
        orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        classification = classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        assert classification.prior_execution_status is ApplyExecutionStatus.BLOCKED
        # The prior blocked execution's block reason is preserved on the
        # classification for audit traceability.
        assert classification.prior_block_reason != ""


# ---------------------------------------------------------------------------
# Test 10: Public API export test
# ---------------------------------------------------------------------------


class TestPublicApiExport:
    def test_symbols_exported_from_barrel(self):
        import finance_core.reconciliation as reconciliation

        assert "ApplyIdempotencyOutcome" in reconciliation.__all__
        assert "ApplyIdempotencyClassification" in reconciliation.__all__
        assert "classify_reconciliation_apply_idempotency" in reconciliation.__all__
        assert hasattr(reconciliation, "ApplyIdempotencyOutcome")
        assert hasattr(reconciliation, "ApplyIdempotencyClassification")
        assert hasattr(reconciliation, "classify_reconciliation_apply_idempotency")

    def test_barrel_identity_matches_module(self):
        import finance_core.reconciliation as reconciliation
        from finance_core.reconciliation.apply_orchestrator import (
            ApplyIdempotencyClassification as ModuleClassification,
        )
        from finance_core.reconciliation.apply_orchestrator import (
            ApplyIdempotencyOutcome as ModuleOutcome,
        )
        from finance_core.reconciliation.apply_orchestrator import (
            classify_reconciliation_apply_idempotency as ModuleClassify,
        )

        assert reconciliation.ApplyIdempotencyOutcome is ModuleOutcome
        assert reconciliation.ApplyIdempotencyClassification is ModuleClassification
        assert reconciliation.classify_reconciliation_apply_idempotency is ModuleClassify

    def test_no_duplicate_or_stale_exports(self):
        import finance_core.reconciliation as reconciliation

        exported = reconciliation.__all__
        assert len(exported) == len(set(exported))
        assert all(not name.startswith("_") for name in exported)


# ---------------------------------------------------------------------------
# Test 11: Invalid input raises ValueError
# ---------------------------------------------------------------------------


class TestInvalidInput:
    def test_empty_idempotency_key_raises(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
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
            classify_reconciliation_apply_idempotency(orch_input, repository=repo)

    def test_empty_plan_inputs_raises(
        self,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
    ):
        orch_input = ApplyOrchestrationInput(
            plan_inputs=(),
            guard_decisions_by_operation_id={},
            idempotency_key="idem-empty",
        )
        with pytest.raises(ValueError, match="plan input"):
            classify_reconciliation_apply_idempotency(orch_input, repository=repo)


# ---------------------------------------------------------------------------
# Test 12: Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_inputs_same_classification(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-det-001",
        )
        orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        first = classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        second = classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        assert first.outcome == second.outcome
        assert first.candidate_fingerprint == second.candidate_fingerprint
        assert first.prior_execution_id == second.prior_execution_id
        assert first.classification_note == second.classification_note
        assert first == second

    def test_classification_is_immutable(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-det-002",
        )
        orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        classification = classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        with pytest.raises(Exception):
            classification.outcome = ApplyIdempotencyOutcome.CONFLICT  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test 13: outcome enum coverage + helper properties
# ---------------------------------------------------------------------------


class TestOutcomeEnumAndHelpers:
    def test_outcome_enum_has_all_required_values(self):
        values = {o.value for o in ApplyIdempotencyOutcome}
        assert values == {
            "idempotent_replay",
            "conflict",
            "prior_blocked",
            "prior_partially_blocked",
            "prior_requires_review",
            "no_prior_execution",
        }

    def test_outcome_enum_is_str_enum(self):
        assert ApplyIdempotencyOutcome.IDEMPOTENT_REPLAY == "idempotent_replay"
        assert isinstance(ApplyIdempotencyOutcome.IDEMPOTENT_REPLAY, str)

    def test_classification_helper_properties(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
    ):
        """NO_PRIOR_EXECUTION: all new-property helpers are False."""
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-helpers-001",
        )
        pre = classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        assert pre.outcome is ApplyIdempotencyOutcome.NO_PRIOR_EXECUTION
        assert pre.is_same_recorded_execution is False
        assert pre.is_duplicate_prevention_rejection is False
        assert pre.is_completed_idempotent_replay is False

    def test_classification_note_is_non_empty_and_deterministic(
        self,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        orch_input = _approved_orch_input(
            sample_decision,
            queue_item,
            approved_guard_decision,
            idempotency_key="idem-note-001",
        )
        pre = classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        assert "no prior execution" in pre.classification_note
        orchestrate_reconciliation_apply(orch_input, repository=repo, clock=fixed_clock)
        post = classify_reconciliation_apply_idempotency(orch_input, repository=repo)
        assert "idempotent replay" in post.classification_note
        assert "no final financial records" in post.classification_note
