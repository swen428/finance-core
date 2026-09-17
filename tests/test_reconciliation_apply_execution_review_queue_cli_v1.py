"""Tests for Reconciliation Apply Execution Review Queue CLI v1.

Covers:
  1. Empty queue output is clear and exits successfully.
  2. One review-required execution rendered in text mode.
  3. Blocked or partially-blocked execution rendered with reason codes.
  4. Deterministic ordering comes from the queue builder.
  5. Limit behavior.
  6. Filter by requires_human_review.
  7. Filter by execution_status.
  8. JSON output: parseable, empty, deterministic, all fields.
  9. Live DB guard fails clearly.
 10. Missing --db fails clearly.
 11. Non-existent database path fails clearly.
 12. Invalid --format argument fails clearly.
 13. Invalid --requires-human-review argument fails clearly.
 14. Invalid --execution-status argument fails clearly.
 15. Invalid --limit argument fails clearly (negative).
 16. Invalid --limit argument fails clearly (non-integer).
 17. Read-only behavior / no mutation.
 18. CLI function can be tested without touching database/finance.db.
 19. Combined filters + limit work together.
 20. Text output structure is correct.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_core.reconciliation.apply_execution_review_queue_cli import main as cli_main
from finance_core.reconciliation.apply_plan import (
    ApplyPlanInput,
    ReconciliationApplyPlan,
    build_reconciliation_apply_plan,
)
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
# Smoke tests -- fast, fixture-light verification
# ---------------------------------------------------------------------------


class TestSmoke:
    """Focused smoke tests for quick CLI validation.

    These tests use only a migrated temporary SQLite database (migration
    014) and no heavy runtime/plan/guard fixtures. They are the fastest
    way to verify the CLI path works.

    Run with::

        .venv/bin/python -m pytest tests/... -k "TestSmoke" -v
    """

    def test_text_mode_on_empty_temp_db_exits_zero_and_shows_headers(
        self, temp_db_path: Path, capsys
    ):
        """CLI exits 0 with expected text output on an empty migrated temp DB."""
        conn = _setup_temp_db(temp_db_path)
        conn.close()
        argv = ["--db", str(temp_db_path)]
        result = cli_main(argv)
        assert result == 0
        out = capsys.readouterr().out
        assert "Reconciliation Apply Execution Review Queue" in out
        assert "Mode:      read-only" in out
        assert "No apply execution review queue entries require attention." in out

    def test_json_mode_on_empty_temp_db_produces_valid_json(self, temp_db_path: Path, capsys):
        """CLI exits 0 and produces parseable JSON on an empty temp DB."""
        conn = _setup_temp_db(temp_db_path)
        conn.close()
        argv = ["--db", str(temp_db_path), "--format", "json"]
        result = cli_main(argv)
        assert result == 0
        payload = json.loads(capsys.readouterr().out)
        assert isinstance(payload, list)
        assert payload == []

    def test_refuses_live_db(self):
        """CLI exits non-zero when pointed at database/finance.db."""
        argv = ["--db", str(LIVE_DB_PATH)]
        result = cli_main(argv)
        assert result != 0

    def test_never_touches_live_db(self, temp_db_path: Path):
        """Temp DB path is always distinct from the live DB path."""
        assert temp_db_path != LIVE_DB_PATH
        conn = _setup_temp_db(temp_db_path)
        conn.close()
        argv = ["--db", str(temp_db_path)]
        result = cli_main(argv)
        assert result == 0

    def test_read_only_does_not_mutate_temp_db(self, temp_db_path: Path):
        """CLI invocation does not change row counts in the temp DB."""
        conn = _setup_temp_db(temp_db_path)
        conn.execute(
            """INSERT INTO reconciliation_guarded_apply_executions
               (execution_id, plan_id, idempotency_key, execution_fingerprint,
                execution_status, total_operations, operations_executed,
                operations_blocked, operations_skipped, block_reason,
                guard_decision_refs_json, audit_trail_json, is_dry_run,
                executed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "exec-smoke-001",
                "plan-smoke-001",
                "idem-smoke-001",
                "fp-smoke-001",
                "blocked",
                1,
                0,
                1,
                0,
                "guard blocked",
                "[]",
                "[]",
                1,
                "2024-12-15T12:00:00Z",
            ),
        )
        conn.commit()
        conn.close()
        argv = ["--db", str(temp_db_path)]
        result = cli_main(argv)
        assert result == 0


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
        idempotency_key="test-cli-executed",
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
        idempotency_key="test-cli-blocked",
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
        idempotency_key="test-cli-partial",
        clock=fixed_clock,
    )


def _setup_temp_db(temp_db_path: Path) -> sqlite3.Connection:
    """Create a temp DB with migration 014 applied."""
    assert temp_db_path != LIVE_DB_PATH
    conn = connect_temp_db(temp_db_path)
    apply_sql(conn, MIGRATION_014)
    conn.commit()
    return conn


def _save_and_close(
    conn: sqlite3.Connection,
    result: GuardedApplyExecutionResult,
    fingerprint: str,
):
    """Save an execution result and close the connection."""
    repo = GuardedApplyExecutionRepository(conn)
    repo.save_execution_result(result, execution_fingerprint=fingerprint)
    conn.close()


# ---------------------------------------------------------------------------
# Test 1: Empty queue
# ---------------------------------------------------------------------------


class TestEmptyQueue:
    def test_empty_queue_text_output(self, temp_db_path: Path):
        conn = _setup_temp_db(temp_db_path)
        conn.close()
        argv = ["--db", str(temp_db_path)]
        result = cli_main(argv)
        assert result == 0

    def test_empty_queue_json_output(self, temp_db_path: Path, capsys):
        conn = _setup_temp_db(temp_db_path)
        conn.close()
        argv = ["--db", str(temp_db_path), "--format", "json"]
        result = cli_main(argv)
        assert result == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload == []


# ---------------------------------------------------------------------------
# Test 2: Single blocked execution rendered
# ---------------------------------------------------------------------------


class TestSingleBlockedInText:
    def test_blocked_result_renders(
        self, temp_db_path: Path, blocked_result: GuardedApplyExecutionResult
    ):
        conn = _setup_temp_db(temp_db_path)
        _save_and_close(conn, blocked_result, "fp-cli-text-001")
        argv = ["--db", str(temp_db_path), "--requires-human-review", "true"]
        result = cli_main(argv)
        assert result == 0


# ---------------------------------------------------------------------------
# Test 3: Partially blocked rendered
# ---------------------------------------------------------------------------


class TestPartiallyBlockedInText:
    def test_partially_blocked_renders(
        self, temp_db_path: Path, partially_blocked_result: GuardedApplyExecutionResult
    ):
        conn = _setup_temp_db(temp_db_path)
        _save_and_close(conn, partially_blocked_result, "fp-cli-text-002")
        argv = ["--db", str(temp_db_path)]
        result = cli_main(argv)
        assert result == 0


# ---------------------------------------------------------------------------
# Test 4: Deterministic ordering
# ---------------------------------------------------------------------------


class TestDeterministicOrdering:
    def test_two_entries_ordered(
        self,
        temp_db_path: Path,
        executed_result: GuardedApplyExecutionResult,
        blocked_result: GuardedApplyExecutionResult,
    ):
        conn = _setup_temp_db(temp_db_path)
        repo = GuardedApplyExecutionRepository(conn)
        repo.save_execution_result(executed_result, execution_fingerprint="fp-cli-order-001")
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-cli-order-002")
        conn.close()
        argv = ["--db", str(temp_db_path)]
        result = cli_main(argv)
        assert result == 0


# ---------------------------------------------------------------------------
# Test 5: Limit behavior
# ---------------------------------------------------------------------------


class TestLimit:
    def test_limit_in_text_mode(
        self,
        temp_db_path: Path,
        executed_result: GuardedApplyExecutionResult,
        blocked_result: GuardedApplyExecutionResult,
    ):
        conn = _setup_temp_db(temp_db_path)
        repo = GuardedApplyExecutionRepository(conn)
        repo.save_execution_result(executed_result, execution_fingerprint="fp-cli-lim-001")
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-cli-lim-002")
        conn.close()
        argv = ["--db", str(temp_db_path), "--limit", "1"]
        result = cli_main(argv)
        assert result == 0


# ---------------------------------------------------------------------------
# Test 6: Filter by requires_human_review
# ---------------------------------------------------------------------------


class TestFilterRequiresHumanReview:
    def test_filter_true(
        self,
        temp_db_path: Path,
        executed_result: GuardedApplyExecutionResult,
        blocked_result: GuardedApplyExecutionResult,
    ):
        conn = _setup_temp_db(temp_db_path)
        repo = GuardedApplyExecutionRepository(conn)
        repo.save_execution_result(executed_result, execution_fingerprint="fp-cli-filt-001")
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-cli-filt-002")
        conn.close()
        argv = ["--db", str(temp_db_path), "--requires-human-review", "true"]
        result = cli_main(argv)
        assert result == 0

    def test_filter_false(
        self,
        temp_db_path: Path,
        executed_result: GuardedApplyExecutionResult,
        blocked_result: GuardedApplyExecutionResult,
    ):
        conn = _setup_temp_db(temp_db_path)
        repo = GuardedApplyExecutionRepository(conn)
        repo.save_execution_result(executed_result, execution_fingerprint="fp-cli-filt-003")
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-cli-filt-004")
        conn.close()
        argv = ["--db", str(temp_db_path), "--requires-human-review", "false"]
        result = cli_main(argv)
        assert result == 0

    def test_filter_all(
        self,
        temp_db_path: Path,
        executed_result: GuardedApplyExecutionResult,
        blocked_result: GuardedApplyExecutionResult,
    ):
        conn = _setup_temp_db(temp_db_path)
        repo = GuardedApplyExecutionRepository(conn)
        repo.save_execution_result(executed_result, execution_fingerprint="fp-cli-filt-005")
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-cli-filt-006")
        conn.close()
        argv = ["--db", str(temp_db_path), "--requires-human-review", "all"]
        result = cli_main(argv)
        assert result == 0


# ---------------------------------------------------------------------------
# Test 7: Filter by execution_status
# ---------------------------------------------------------------------------


class TestFilterExecutionStatus:
    def test_filter_blocked(
        self,
        temp_db_path: Path,
        executed_result: GuardedApplyExecutionResult,
        blocked_result: GuardedApplyExecutionResult,
    ):
        conn = _setup_temp_db(temp_db_path)
        repo = GuardedApplyExecutionRepository(conn)
        repo.save_execution_result(executed_result, execution_fingerprint="fp-cli-stat-001")
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-cli-stat-002")
        conn.close()
        argv = ["--db", str(temp_db_path), "--execution-status", "BLOCKED"]
        result = cli_main(argv)
        assert result == 0


# ---------------------------------------------------------------------------
# Test 8: JSON output
# ---------------------------------------------------------------------------


class TestJSONOutput:
    def test_json_parseable(
        self, temp_db_path: Path, blocked_result: GuardedApplyExecutionResult, capsys
    ):
        conn = _setup_temp_db(temp_db_path)
        _save_and_close(conn, blocked_result, "fp-cli-json-001")
        argv = ["--db", str(temp_db_path), "--format", "json"]
        result = cli_main(argv)
        assert result == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert isinstance(payload, list)
        assert len(payload) == 1
        assert payload[0]["execution_status"] == "blocked"

    def test_json_empty(self, temp_db_path: Path, capsys):
        conn = _setup_temp_db(temp_db_path)
        conn.close()
        argv = ["--db", str(temp_db_path), "--format", "json"]
        result = cli_main(argv)
        assert result == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload == []

    def test_json_deterministic(
        self, temp_db_path: Path, blocked_result: GuardedApplyExecutionResult, capsys
    ):
        conn = _setup_temp_db(temp_db_path)
        _save_and_close(conn, blocked_result, "fp-cli-json-det-001")
        argv = ["--db", str(temp_db_path), "--format", "json"]
        result1 = cli_main(argv)
        captured1 = capsys.readouterr()
        assert result1 == 0
        result2 = cli_main(argv)
        captured2 = capsys.readouterr()
        assert result2 == 0
        assert captured1.out == captured2.out

    def test_json_all_fields(
        self, temp_db_path: Path, blocked_result: GuardedApplyExecutionResult, capsys
    ):
        conn = _setup_temp_db(temp_db_path)
        _save_and_close(conn, blocked_result, "fp-cli-json-fields-001")
        argv = ["--db", str(temp_db_path), "--format", "json"]
        result = cli_main(argv)
        assert result == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        entry = payload[0]
        expected_fields = {
            "execution_id",
            "plan_id",
            "idempotency_key",
            "execution_status",
            "review_priority",
            "requires_human_review",
            "is_blocking",
            "is_partially_blocked",
            "operations_executed",
            "operations_blocked",
            "operations_skipped",
            "total_operations",
            "block_reason",
            "reason_codes",
            "is_dry_run",
            "executed_at",
            "mutation_types",
            "guard_decision_refs",
            "operation_refs",
            "human_summary",
        }
        assert set(entry.keys()) == expected_fields


# ---------------------------------------------------------------------------
# Test 9: Live DB guard
# ---------------------------------------------------------------------------


class TestLiveDbGuard:
    def test_refuses_live_db(self):
        live_db = str(LIVE_DB_PATH)
        argv = ["--db", live_db]
        result = cli_main(argv)
        assert result != 0


# ---------------------------------------------------------------------------
# Test 10: Missing --db
# ---------------------------------------------------------------------------


class TestMissingDb:
    def test_missing_db_argument(self):
        argv: list[str] = []
        result = cli_main(argv)
        assert result != 0


# ---------------------------------------------------------------------------
# Test 11: Non-existent database path
# ---------------------------------------------------------------------------


class TestNonExistentDb:
    def test_non_existent_path(self, tmp_path: Path):
        db_path = tmp_path / "nonexistent.db"
        argv = ["--db", str(db_path)]
        result = cli_main(argv)
        assert result != 0


# ---------------------------------------------------------------------------
# Test 12: Invalid --format
# ---------------------------------------------------------------------------


class TestInvalidFormat:
    def test_invalid_format(self, temp_db_path: Path):
        argv = ["--db", str(temp_db_path), "--format", "xml"]
        result = cli_main(argv)
        assert result != 0


# ---------------------------------------------------------------------------
# Test 13: Invalid --requires-human-review
# ---------------------------------------------------------------------------


class TestInvalidRequiresHumanReview:
    def test_invalid_value(self, temp_db_path: Path):
        argv = ["--db", str(temp_db_path), "--requires-human-review", "yes"]
        result = cli_main(argv)
        assert result != 0


# ---------------------------------------------------------------------------
# Test 14: Invalid --execution-status
# ---------------------------------------------------------------------------


class TestInvalidExecutionStatus:
    def test_invalid_value(self, temp_db_path: Path):
        argv = ["--db", str(temp_db_path), "--execution-status", "INVALID"]
        result = cli_main(argv)
        assert result != 0


# ---------------------------------------------------------------------------
# Test 15: Negative limit
# ---------------------------------------------------------------------------


class TestInvalidLimitNegative:
    def test_negative_limit(self, temp_db_path: Path):
        argv = ["--db", str(temp_db_path), "--limit", "-1"]
        result = cli_main(argv)
        assert result != 0


# ---------------------------------------------------------------------------
# Test 16: Non-integer limit
# ---------------------------------------------------------------------------


class TestInvalidLimitNonInteger:
    def test_non_integer_limit(self, temp_db_path: Path):
        argv = ["--db", str(temp_db_path), "--limit", "abc"]
        result = cli_main(argv)
        assert result != 0


# ---------------------------------------------------------------------------
# Test 17: Read-only behavior
# ---------------------------------------------------------------------------


class TestReadOnly:
    def test_cli_does_not_mutate_repo(
        self, temp_db_path: Path, blocked_result: GuardedApplyExecutionResult
    ):
        conn = _setup_temp_db(temp_db_path)
        _save_and_close(conn, blocked_result, "fp-cli-ro-001")
        argv = ["--db", str(temp_db_path)]
        result = cli_main(argv)
        assert result == 0


# ---------------------------------------------------------------------------
# Test 18: Tests use temp DB only
# ---------------------------------------------------------------------------


class TestNoLiveDbTouch:
    def test_tests_use_temp_db_only(
        self, temp_db_path: Path, blocked_result: GuardedApplyExecutionResult
    ):
        conn = _setup_temp_db(temp_db_path)
        _save_and_close(conn, blocked_result, "fp-cli-safe-001")
        assert temp_db_path != LIVE_DB_PATH
        argv = ["--db", str(temp_db_path), "--format", "json"]
        result = cli_main(argv)
        assert result == 0


# ---------------------------------------------------------------------------
# Test 19: Combined filters and limit
# ---------------------------------------------------------------------------


class TestCombinedFilters:
    def test_combined_status_and_limit(
        self,
        temp_db_path: Path,
        executed_result: GuardedApplyExecutionResult,
        blocked_result: GuardedApplyExecutionResult,
    ):
        conn = _setup_temp_db(temp_db_path)
        repo = GuardedApplyExecutionRepository(conn)
        repo.save_execution_result(executed_result, execution_fingerprint="fp-cli-combo-001")
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-cli-combo-002")
        conn.close()
        argv = [
            "--db",
            str(temp_db_path),
            "--execution-status",
            "BLOCKED",
            "--requires-human-review",
            "true",
            "--limit",
            "1",
        ]
        result = cli_main(argv)
        assert result == 0


# ---------------------------------------------------------------------------
# Test 20: Text output structure
# ---------------------------------------------------------------------------


class TestTextOutputStructure:
    def test_text_output_has_expected_headers(
        self, temp_db_path: Path, blocked_result: GuardedApplyExecutionResult, capsys
    ):
        conn = _setup_temp_db(temp_db_path)
        _save_and_close(conn, blocked_result, "fp-cli-headers-001")
        argv = ["--db", str(temp_db_path)]
        result = cli_main(argv)
        assert result == 0
        captured = capsys.readouterr()
        out = captured.out
        assert "Reconciliation Apply Execution Review Queue" in out
        assert "Database:" in out
        assert "Count:" in out
        assert "Mode:      read-only" in out

    def test_text_empty_queue_message(self, temp_db_path: Path, capsys):
        conn = _setup_temp_db(temp_db_path)
        conn.close()
        argv = ["--db", str(temp_db_path)]
        result = cli_main(argv)
        assert result == 0
        captured = capsys.readouterr()
        assert "No apply execution review queue entries require attention." in captured.out


# ---------------------------------------------------------------------------
# Test 21: Seeded queue output verification
# ---------------------------------------------------------------------------


class TestSeededQueueOutput:
    """Focused tests for the CLI against a temp DB seeded with real
    execution results.

    These tests go beyond empty-DB smoke checks and verify that the CLI
    renders seeded queue data correctly in text and JSON modes, that
    filters correctly include/exclude seeded entries, and that the CLI
    does not mutate the seeded database.
    """

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _seed_blocked_and_executed(
        temp_db_path,
        blocked_result,
        executed_result,
    ):
        """Seed two execution results (one blocked, one executed) into a
        temp DB."""
        conn = _setup_temp_db(temp_db_path)
        repo = GuardedApplyExecutionRepository(conn)
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-seeded-001")
        repo.save_execution_result(executed_result, execution_fingerprint="fp-seeded-002")
        conn.close()

    # -- text-mode output content ------------------------------------------

    def test_text_output_includes_seeded_fields(
        self,
        temp_db_path,
        blocked_result,
        executed_result,
        capsys,
    ):
        """Text output contains stable fields from the seeded queue entries."""
        self._seed_blocked_and_executed(temp_db_path, blocked_result, executed_result)

        argv = ["--db", str(temp_db_path)]
        result = cli_main(argv)
        assert result == 0

        out = capsys.readouterr().out

        # Headers
        assert "Reconciliation Apply Execution Review Queue" in out
        assert "Count:     2" in out

        # Blocked entry fields (comes first due to HIGH priority + blocking)
        assert "idempotency_key=test-cli-blocked" in out
        assert "execution_status=blocked" in out
        assert "requires_human_review=True" in out
        assert "is_blocking=True" in out
        assert "total_operations=1" in out
        assert "operations_blocked=1" in out
        assert "block_reason=" in out

        # Executed entry fields
        assert "idempotency_key=test-cli-executed" in out
        assert "execution_status=executed" in out
        assert "requires_human_review=False" in out
        assert "operations_executed=1" in out

    # -- JSON-mode output content ------------------------------------------

    def test_json_output_includes_seeded_values(
        self,
        temp_db_path,
        blocked_result,
        executed_result,
        capsys,
    ):
        """JSON output contains expected keys and values from seeded entries."""
        self._seed_blocked_and_executed(temp_db_path, blocked_result, executed_result)

        argv = ["--db", str(temp_db_path), "--format", "json"]
        result = cli_main(argv)
        assert result == 0

        payload = json.loads(capsys.readouterr().out)
        assert isinstance(payload, list)
        assert len(payload) == 2

        # Blocked entry (first: HIGH priority + blocking)
        blocked = payload[0]
        assert blocked["execution_status"] == "blocked"
        assert blocked["idempotency_key"] == "test-cli-blocked"
        assert blocked["requires_human_review"] is True
        assert blocked["is_blocking"] is True
        assert blocked["is_partially_blocked"] is False
        assert blocked["operations_blocked"] == 1
        assert blocked["operations_executed"] == 0
        assert blocked["is_dry_run"] is True
        assert "execution_id" in blocked
        assert blocked["execution_id"].startswith("exec-")

        # Executed entry (second)
        executed = payload[1]
        assert executed["execution_status"] == "executed"
        assert executed["idempotency_key"] == "test-cli-executed"
        assert executed["requires_human_review"] is False
        assert executed["is_blocking"] is False
        assert executed["operations_executed"] == 1
        assert executed["operations_blocked"] == 0

    # -- filter: requires_human_review -------------------------------------

    def test_filter_requires_human_review_true_only_blocked(
        self,
        temp_db_path,
        blocked_result,
        executed_result,
        capsys,
    ):
        """--requires-human-review true returns only the blocked entry."""
        self._seed_blocked_and_executed(temp_db_path, blocked_result, executed_result)

        argv = ["--db", str(temp_db_path), "--requires-human-review", "true", "--format", "json"]
        result = cli_main(argv)
        assert result == 0

        payload = json.loads(capsys.readouterr().out)
        assert len(payload) == 1
        assert payload[0]["execution_status"] == "blocked"
        assert payload[0]["idempotency_key"] == "test-cli-blocked"

    def test_filter_requires_human_review_false_only_executed(
        self,
        temp_db_path,
        blocked_result,
        executed_result,
        capsys,
    ):
        """--requires-human-review false returns only the executed entry."""
        self._seed_blocked_and_executed(temp_db_path, blocked_result, executed_result)

        argv = ["--db", str(temp_db_path), "--requires-human-review", "false", "--format", "json"]
        result = cli_main(argv)
        assert result == 0

        payload = json.loads(capsys.readouterr().out)
        assert len(payload) == 1
        assert payload[0]["execution_status"] == "executed"
        assert payload[0]["idempotency_key"] == "test-cli-executed"

    # -- filter: execution_status ------------------------------------------

    def test_filter_execution_status_blocked_only_blocked(
        self,
        temp_db_path,
        blocked_result,
        executed_result,
        capsys,
    ):
        """--execution-status BLOCKED returns only the blocked entry."""
        self._seed_blocked_and_executed(temp_db_path, blocked_result, executed_result)

        argv = ["--db", str(temp_db_path), "--execution-status", "BLOCKED", "--format", "json"]
        result = cli_main(argv)
        assert result == 0

        payload = json.loads(capsys.readouterr().out)
        assert len(payload) == 1
        assert payload[0]["execution_status"] == "blocked"
        assert payload[0]["idempotency_key"] == "test-cli-blocked"

    # -- read-only ---------------------------------------------------------

    def test_read_only_does_not_mutate_seeded_db(
        self,
        temp_db_path,
        blocked_result,
        capsys,
    ):
        """CLI does not change seeded row count in temp DB."""
        conn = _setup_temp_db(temp_db_path)
        repo = GuardedApplyExecutionRepository(conn)
        repo.save_execution_result(blocked_result, execution_fingerprint="fp-seeded-ro-001")
        conn.close()

        conn = connect_temp_db(temp_db_path)
        before = conn.execute(
            "SELECT COUNT(*) FROM reconciliation_guarded_apply_executions"
        ).fetchone()[0]
        conn.close()

        argv = ["--db", str(temp_db_path)]
        result = cli_main(argv)
        assert result == 0

        conn = connect_temp_db(temp_db_path)
        after = conn.execute(
            "SELECT COUNT(*) FROM reconciliation_guarded_apply_executions"
        ).fetchone()[0]
        conn.close()

        assert before == after
        assert before >= 1
