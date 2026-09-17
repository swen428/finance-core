"""Tests for Guarded Apply Execution Persistence v1.

Covers:
  1. Migration 014 creates expected tables.
  2. Repository saves an executed result.
  3. Repository loads result by idempotency key.
  4. Loaded result reconstructs GuardedApplyExecutionResult.
  5. Operation results are persisted and loaded correctly.
  6. Same key + different fingerprint raises conflict.
  7. Same key + same fingerprint returns cached result (idempotent).
  8. Persistent runtime blocks duplicate conflicting replay safely.
  9. Missing guard blocked result can be persisted.
 10. Unsupported mutation blocked result can be persisted.
 11. JSON fields round-trip correctly.
 12. database/finance.db is untouched.
 13. has_idempotency_key works.
 14. get_idempotency_fingerprint works.
 15. Runtime without repository behaves same as before.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_core.reconciliation.apply_plan import (
    ApplyPlanInput,
    ReconciliationApplyPlan,
    build_reconciliation_apply_plan,
)
from finance_core.reconciliation.apply_runtime import (
    GuardedApplyRuntime,
    build_guarded_apply_execution_fingerprint,
)
from finance_core.reconciliation.apply_runtime_persistence import (
    GuardedApplyExecutionConflictError,
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

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

MIGRATION_014 = migrations_dir() / "014_reconciliation_guarded_apply_execution_persistence.sql"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    return lambda: "2024-12-15T12:00:00.000000+00:00"


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
        idempotency_key="test-persist-001",
        clock=fixed_clock,
    )


# ---------------------------------------------------------------------------
# Test 1: Migration 014 creates expected tables
# ---------------------------------------------------------------------------


class TestMigration014:
    def test_tables_exist(self, migrated_conn: sqlite3.Connection):
        rows = migrated_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert "reconciliation_guarded_apply_executions" in names
        assert "reconciliation_guarded_apply_operation_results" in names

    def test_executions_columns(self, migrated_conn: sqlite3.Connection):
        rows = migrated_conn.execute(
            "PRAGMA table_info('reconciliation_guarded_apply_executions')"
        ).fetchall()
        cols = {r["name"] for r in rows}
        expected = {
            "execution_id",
            "plan_id",
            "idempotency_key",
            "execution_fingerprint",
            "execution_status",
            "total_operations",
            "operations_executed",
            "operations_blocked",
            "operations_skipped",
            "block_reason",
            "guard_decision_refs_json",
            "audit_trail_json",
            "is_dry_run",
            "executed_at",
            "created_at",
        }
        assert cols == expected

    def test_operation_results_columns(self, migrated_conn: sqlite3.Connection):
        rows = migrated_conn.execute(
            "PRAGMA table_info('reconciliation_guarded_apply_operation_results')"
        ).fetchall()
        cols = {r["name"] for r in rows}
        expected = {
            "operation_result_id",
            "execution_id",
            "operation_id",
            "decision_id",
            "execution_status",
            "reason",
            "guard_decision_approved",
            "guard_decision_idempotency_key",
            "guard_blocked_reasons_json",
            "mutation_type",
            "mutation_payload_json",
            "created_at",
        }
        assert cols == expected

    def test_execution_status_check_constraint(self, migrated_conn: sqlite3.Connection):
        invalid_statuses = ["unknown", "", " EXECUTED", "executed "]
        for bad in invalid_statuses:
            with pytest.raises(sqlite3.IntegrityError):
                migrated_conn.execute(
                    "INSERT INTO reconciliation_guarded_apply_executions "
                    "(execution_id, plan_id, idempotency_key, execution_fingerprint, "
                    "execution_status, total_operations, operations_executed, "
                    "operations_blocked, operations_skipped, guard_decision_refs_json, "
                    "audit_trail_json, executed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"exec-{bad}",
                        "plan-1",
                        f"key-{bad}",
                        "fp",
                        bad,
                        0,
                        0,
                        0,
                        0,
                        "[]",
                        "{}",
                        "2024-01-01T00:00:00Z",
                    ),
                )

    def test_count_consistency_check(self, migrated_conn: sqlite3.Connection):
        """CHECK constraint: operations_executed + blocked + skipped = total."""
        with pytest.raises(sqlite3.IntegrityError):
            migrated_conn.execute(
                "INSERT INTO reconciliation_guarded_apply_executions "
                "(execution_id, plan_id, idempotency_key, execution_fingerprint, "
                "execution_status, total_operations, operations_executed, "
                "operations_blocked, operations_skipped, guard_decision_refs_json, "
                "audit_trail_json, executed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "exec-count-bad",
                    "plan-1",
                    "key-count",
                    "fp",
                    "executed",
                    3,
                    1,
                    1,
                    0,
                    "[]",
                    "{}",
                    "2024-01-01T00:00:00Z",
                ),
            )

    def test_idempotency_key_uniqueness(self, migrated_conn: sqlite3.Connection):
        migrated_conn.execute(
            "INSERT INTO reconciliation_guarded_apply_executions "
            "(execution_id, plan_id, idempotency_key, execution_fingerprint, "
            "execution_status, total_operations, operations_executed, "
            "operations_blocked, operations_skipped, guard_decision_refs_json, "
            "audit_trail_json, executed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "exec-1",
                "plan-1",
                "key-uniq",
                "fp-1",
                "executed",
                1,
                1,
                0,
                0,
                "[]",
                "{}",
                "2024-01-01T00:00:00Z",
            ),
        )
        migrated_conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            migrated_conn.execute(
                "INSERT INTO reconciliation_guarded_apply_executions "
                "(execution_id, plan_id, idempotency_key, execution_fingerprint, "
                "execution_status, total_operations, operations_executed, "
                "operations_blocked, operations_skipped, guard_decision_refs_json, "
                "audit_trail_json, executed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "exec-2",
                    "plan-2",
                    "key-uniq",
                    "fp-2",
                    "executed",
                    1,
                    1,
                    0,
                    0,
                    "[]",
                    "{}",
                    "2024-01-01T00:00:00Z",
                ),
            )


# ---------------------------------------------------------------------------
# Test 2: Repository saves an executed result
# ---------------------------------------------------------------------------


class TestSaveExecutionResult:
    def test_save_returns_true_for_first_save(
        self, repo: GuardedApplyExecutionRepository, executed_result: GuardedApplyExecutionResult
    ):
        ok = repo.save_execution_result(executed_result, execution_fingerprint="fp-test-001")
        assert ok is True

    def test_save_persists_row_in_db(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
        migrated_conn: sqlite3.Connection,
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-test-002")
        row = migrated_conn.execute(
            "SELECT * FROM reconciliation_guarded_apply_executions WHERE idempotency_key = ?",
            ("test-persist-001",),
        ).fetchone()
        assert row is not None
        assert row["plan_id"] == executed_result.plan_id
        assert row["execution_status"] == "executed"
        assert row["is_dry_run"] == 1


# ---------------------------------------------------------------------------
# Test 3: Repository loads result by idempotency key
# ---------------------------------------------------------------------------


class TestGetByIdempotencyKey:
    def test_loads_previously_saved_result(
        self, repo: GuardedApplyExecutionRepository, executed_result: GuardedApplyExecutionResult
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-load-001")
        loaded = repo.get_by_idempotency_key("test-persist-001")
        assert loaded is not None
        assert loaded.plan_id == executed_result.plan_id
        assert loaded.idempotency_key == "test-persist-001"
        assert loaded.execution_status == ApplyExecutionStatus.EXECUTED

    def test_returns_none_for_unknown_key(self, repo: GuardedApplyExecutionRepository):
        loaded = repo.get_by_idempotency_key("nonexistent-key")
        assert loaded is None


# ---------------------------------------------------------------------------
# Test 4: Loaded result reconstructs GuardedApplyExecutionResult
# ---------------------------------------------------------------------------


class TestReconstructResult:
    def test_loaded_result_matches_original_structure(
        self, repo: GuardedApplyExecutionRepository, executed_result: GuardedApplyExecutionResult
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-recon-001")
        loaded = repo.get_by_idempotency_key("test-persist-001")
        assert loaded is not None

        # Plan-level fields
        assert loaded.plan_id == executed_result.plan_id
        assert loaded.total_operations == executed_result.total_operations
        assert loaded.operated_executed == executed_result.operated_executed
        assert loaded.operated_blocked == executed_result.operated_blocked
        assert loaded.operated_skipped == executed_result.operated_skipped
        assert loaded.execution_status == executed_result.execution_status
        assert loaded.block_reason == executed_result.block_reason
        assert loaded.executed_at == executed_result.executed_at
        assert loaded.is_dry_run == executed_result.is_dry_run

    def test_loaded_result_has_guard_refs(
        self, repo: GuardedApplyExecutionRepository, executed_result: GuardedApplyExecutionResult
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-recon-002")
        loaded = repo.get_by_idempotency_key("test-persist-001")
        assert loaded is not None
        assert loaded.guard_decision_refs == executed_result.guard_decision_refs

    def test_loaded_result_has_audit_trail(
        self, repo: GuardedApplyExecutionRepository, executed_result: GuardedApplyExecutionResult
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-recon-003")
        loaded = repo.get_by_idempotency_key("test-persist-001")
        assert loaded is not None
        assert loaded.audit_trail["plan_id"] == executed_result.plan_id
        assert loaded.audit_trail["runtime_version"] == "v1"
        assert loaded.audit_trail["is_dry_run"] is True


# ---------------------------------------------------------------------------
# Test 5: Operation results are persisted and loaded correctly
# ---------------------------------------------------------------------------


class TestOperationResultsPersistence:
    def test_operation_results_persisted(
        self,
        repo: GuardedApplyExecutionRepository,
        executed_result: GuardedApplyExecutionResult,
        migrated_conn: sqlite3.Connection,
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-op-001")

        rows = migrated_conn.execute(
            "SELECT * FROM reconciliation_guarded_apply_operation_results"
        ).fetchall()
        assert len(rows) == 1
        op_row = rows[0]
        assert op_row["operation_id"] == executed_result.results[0].operation_id
        assert op_row["execution_status"] == "executed"
        assert op_row["guard_decision_approved"] == 1

    def test_list_operation_results(
        self, repo: GuardedApplyExecutionRepository, executed_result: GuardedApplyExecutionResult
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-op-002")
        loaded = repo.get_by_idempotency_key("test-persist-001")
        assert loaded is not None

        # Derive execution_id the same way the repo does
        import hashlib

        raw = f"{executed_result.plan_id}|test-persist-001"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        exec_id = f"exec-{digest[:16]}"

        ops = repo.list_operation_results(exec_id)
        assert len(ops) == 1
        assert ops[0].operation_id == executed_result.results[0].operation_id
        assert ops[0].execution_status == ApplyExecutionStatus.EXECUTED


# ---------------------------------------------------------------------------
# Test 6: Same key + different fingerprint raises conflict
# ---------------------------------------------------------------------------


class TestConflictOnDifferentFingerprint:
    def test_save_with_different_fingerprint_raises(
        self, repo: GuardedApplyExecutionRepository, executed_result: GuardedApplyExecutionResult
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-original")
        with pytest.raises(GuardedApplyExecutionConflictError):
            repo.save_execution_result(executed_result, execution_fingerprint="fp-different")

    def test_save_with_same_fingerprint_returns_false(
        self, repo: GuardedApplyExecutionRepository, executed_result: GuardedApplyExecutionResult
    ):
        ok1 = repo.save_execution_result(executed_result, execution_fingerprint="fp-same")
        assert ok1 is True
        ok2 = repo.save_execution_result(executed_result, execution_fingerprint="fp-same")
        assert ok2 is False


# ---------------------------------------------------------------------------
# Test 7: Same key + same fingerprint returns cached result (idempotent)
# ---------------------------------------------------------------------------


class TestIdempotentReplay:
    def test_persistent_idempotency_with_repository(
        self,
        sample_plan: ReconciliationApplyPlan,
        approved_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime(repository=repo)
        op_id = sample_plan.operations[0].operation_id
        gd_map = {op_id: approved_guard_decision}

        first = runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id=gd_map,
            idempotency_key="test-idem-persist-001",
            clock=fixed_clock,
        )
        assert first.execution_status == ApplyExecutionStatus.EXECUTED
        assert runtime.is_executed("test-idem-persist-001") is True

        # Replay: same key, same plan, should return cached result
        second = runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id=gd_map,
            idempotency_key="test-idem-persist-001",
            clock=fixed_clock,
        )
        assert second.execution_status == ApplyExecutionStatus.EXECUTED
        assert second.executed_at == first.executed_at


# ---------------------------------------------------------------------------
# Test 8: Persistent runtime blocks duplicate conflicting replay safely
# ---------------------------------------------------------------------------


class TestPersistentConflict:
    def test_persistent_runtime_blocks_different_plan_same_key(
        self,
        sample_plan: ReconciliationApplyPlan,
        sample_decision: ResolutionDecision,
        queue_item: ReviewQueueItem,
        approved_guard_decision: FinalMutationGuardDecision,
        blocked_guard_decision: FinalMutationGuardDecision,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime(repository=repo)
        op_id = sample_plan.operations[0].operation_id
        gd_map = {op_id: approved_guard_decision}

        # First execution succeeds and persists
        first = runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id=gd_map,
            idempotency_key="test-conflict-persist-001",
            clock=fixed_clock,
        )
        assert first.execution_status == ApplyExecutionStatus.EXECUTED

        # Build different plan with different guard
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
                guard_decision=blocked_guard_decision,
            )
        ]
        other_plan = build_reconciliation_apply_plan(other_inputs)
        other_op_id = other_plan.operations[0].operation_id

        # Same key, different plan -> CONFLICT from persistent state
        result = runtime.execute(
            other_plan,
            guard_decisions_by_operation_id={other_op_id: blocked_guard_decision},
            idempotency_key="test-conflict-persist-001",
            clock=fixed_clock,
        )
        assert result.execution_status == ApplyExecutionStatus.CONFLICT
        assert "fingerprint mismatch" in result.block_reason.lower()


# ---------------------------------------------------------------------------
# Test 9: Missing guard blocked result can be persisted
# ---------------------------------------------------------------------------


class TestPersistBlockedByMissingGuard:
    def test_missing_guard_blocked_result_persisted(
        self,
        sample_plan: ReconciliationApplyPlan,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime(repository=repo)
        result = runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={},
            idempotency_key="test-block-missing-guard",
            clock=fixed_clock,
        )
        assert result.execution_status == ApplyExecutionStatus.BLOCKED

        # Verify it was persisted
        loaded = repo.get_by_idempotency_key("test-block-missing-guard")
        assert loaded is not None
        assert loaded.execution_status == ApplyExecutionStatus.BLOCKED
        assert loaded.operated_blocked == 1


# ---------------------------------------------------------------------------
# Test 10: Unsupported mutation blocked result can be persisted
# ---------------------------------------------------------------------------


class TestPersistUnsupportedMutation:
    def test_unsupported_mutation_blocked_result_persisted(
        self,
        sample_statement: StatementTransaction,
        sample_app_txn: AppTransaction,
        repo: GuardedApplyExecutionRepository,
        fixed_clock,
    ):
        decision = ResolutionDecision(
            decision_id="dec-unsup-p-001",
            queue_item_id="q-001",
            action=ResolutionAction.CREATE_MISSING_APP_TRANSACTION,
            note="Should create.",
            reviewer="human",
        )
        cand = ReconciliationCandidate(
            statement=sample_statement,
            best_app_transaction=None,
            match_status=MatchStatus.NO_MATCH,
            candidate_id="cand-unsup-p-001",
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
            proposal_id="fp-unsup-p-001",
            approved=True,
            action=FinalMutationAction.CREATE_FINAL_TRANSACTION,
        )
        inputs = [
            ApplyPlanInput(
                decision=decision,
                queue_item=item,
                guard_decision=approved_gd,
                proposal=FinalMutationProposal(
                    proposal_id="fp-unsup-p-001",
                    action=FinalMutationAction.CREATE_FINAL_TRANSACTION,
                ),
            )
        ]
        plan = build_reconciliation_apply_plan(inputs)

        runtime = GuardedApplyRuntime(repository=repo)
        op_id = plan.operations[0].operation_id
        result = runtime.execute(
            plan,
            guard_decisions_by_operation_id={op_id: approved_gd},
            idempotency_key="test-unsup-persist-001",
            clock=fixed_clock,
        )
        assert result.execution_status == ApplyExecutionStatus.BLOCKED

        loaded = repo.get_by_idempotency_key("test-unsup-persist-001")
        assert loaded is not None
        assert loaded.execution_status == ApplyExecutionStatus.BLOCKED
        assert loaded.results[0].execution_status == ApplyExecutionStatus.BLOCKED
        assert "not a supported execution type" in loaded.results[0].reason.lower()


# ---------------------------------------------------------------------------
# Test 11: JSON fields round-trip correctly
# ---------------------------------------------------------------------------


class TestJsonRoundTrip:
    def test_guard_decision_refs_round_trip(
        self, repo: GuardedApplyExecutionRepository, executed_result: GuardedApplyExecutionResult
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-json-001")
        loaded = repo.get_by_idempotency_key("test-persist-001")
        assert loaded is not None
        assert loaded.guard_decision_refs == executed_result.guard_decision_refs

    def test_audit_trail_round_trip(
        self, repo: GuardedApplyExecutionRepository, executed_result: GuardedApplyExecutionResult
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-json-002")
        loaded = repo.get_by_idempotency_key("test-persist-001")
        assert loaded is not None
        assert loaded.audit_trail["plan_id"] == executed_result.audit_trail["plan_id"]
        assert loaded.audit_trail["runtime_version"] == "v1"
        assert loaded.audit_trail["is_dry_run"] is True
        assert loaded.audit_trail["dry_run_note"] == executed_result.audit_trail["dry_run_note"]

    def test_mutation_payload_round_trip(
        self, repo: GuardedApplyExecutionRepository, executed_result: GuardedApplyExecutionResult
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-json-003")
        loaded = repo.get_by_idempotency_key("test-persist-001")
        assert loaded is not None
        op = loaded.results[0]
        assert "operation_id" in op.mutation_payload
        assert "action" in op.mutation_payload

    def test_empty_guard_blocked_reasons_round_trip(
        self, repo: GuardedApplyExecutionRepository, executed_result: GuardedApplyExecutionResult
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-json-004")
        loaded = repo.get_by_idempotency_key("test-persist-001")
        assert loaded is not None
        assert loaded.results[0].guard_blocked_reasons == ()


# ---------------------------------------------------------------------------
# Test 12: database/finance.db is untouched
# ---------------------------------------------------------------------------


class TestLiveDbNotTouched:
    def test_persistence_never_opens_live_db(self):
        """Repository takes an explicit connection; it never opens a db path."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        repo = GuardedApplyExecutionRepository(conn)
        assert not hasattr(repo, "_db_path")
        conn.close()

    def test_migration_014_is_not_live_db(self, migrated_conn: sqlite3.Connection):
        db_file = migrated_conn.execute("PRAGMA database_list").fetchone()
        assert db_file is not None
        file_path = db_file["file"] if db_file["file"] else ""
        assert str(LIVE_DB_PATH) not in file_path


# ---------------------------------------------------------------------------
# Test 13: has_idempotency_key works
# ---------------------------------------------------------------------------


class TestHasIdempotencyKey:
    def test_has_key_after_save(
        self, repo: GuardedApplyExecutionRepository, executed_result: GuardedApplyExecutionResult
    ):
        assert repo.has_idempotency_key("test-persist-001") is False
        repo.save_execution_result(executed_result, execution_fingerprint="fp-has-001")
        assert repo.has_idempotency_key("test-persist-001") is True

    def test_has_key_unknown(self, repo: GuardedApplyExecutionRepository):
        assert repo.has_idempotency_key("does-not-exist") is False


# ---------------------------------------------------------------------------
# Test 14: get_idempotency_fingerprint works
# ---------------------------------------------------------------------------


class TestGetIdempotencyFingerprint:
    def test_get_fingerprint_after_save(
        self, repo: GuardedApplyExecutionRepository, executed_result: GuardedApplyExecutionResult
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-getfp-001")
        fp = repo.get_idempotency_fingerprint("test-persist-001")
        assert fp == "fp-getfp-001"

    def test_get_fingerprint_unknown(self, repo: GuardedApplyExecutionRepository):
        fp = repo.get_idempotency_fingerprint("does-not-exist")
        assert fp is None


# ---------------------------------------------------------------------------
# Test 15: Runtime without repository behaves same as before
# ---------------------------------------------------------------------------


class TestNoRepositoryBackwardCompat:
    def test_no_repo_runtime_works(
        self,
        sample_plan: ReconciliationApplyPlan,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime()  # No repository
        assert runtime.has_persistence is False
        op_id = sample_plan.operations[0].operation_id
        result = runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id={op_id: approved_guard_decision},
            idempotency_key="test-no-repo-001",
            clock=fixed_clock,
        )
        assert result.execution_status == ApplyExecutionStatus.EXECUTED
        assert result.is_dry_run is True

    def test_no_repo_idempotency_in_memory_only(
        self,
        sample_plan: ReconciliationApplyPlan,
        approved_guard_decision: FinalMutationGuardDecision,
        fixed_clock,
    ):
        runtime = GuardedApplyRuntime()
        op_id = sample_plan.operations[0].operation_id
        gd_map = {op_id: approved_guard_decision}

        first = runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id=gd_map,
            idempotency_key="test-no-repo-idem-001",
            clock=fixed_clock,
        )
        assert first.execution_status == ApplyExecutionStatus.EXECUTED

        second = runtime.execute(
            sample_plan,
            guard_decisions_by_operation_id=gd_map,
            idempotency_key="test-no-repo-idem-001",
            clock=fixed_clock,
        )
        # Idempotent replay: returns same result, same timestamp
        assert second.executed_at == first.executed_at

    def test_build_fingerprint_is_deterministic(
        self,
        sample_plan: ReconciliationApplyPlan,
        approved_guard_decision: FinalMutationGuardDecision,
    ):
        op_id = sample_plan.operations[0].operation_id
        fp1 = build_guarded_apply_execution_fingerprint(
            sample_plan, {op_id: approved_guard_decision}, "test-fp-det-001"
        )
        fp2 = build_guarded_apply_execution_fingerprint(
            sample_plan, {op_id: approved_guard_decision}, "test-fp-det-001"
        )
        assert fp1 == fp2
        assert len(fp1) == 64  # SHA-256 hex digest


# ---------------------------------------------------------------------------
# Test: has_persistence property
# ---------------------------------------------------------------------------


class TestHasPersistence:
    def test_has_persistence_false_without_repo(self):
        runtime = GuardedApplyRuntime()
        assert runtime.has_persistence is False

    def test_has_persistence_true_with_repo(self, repo: GuardedApplyExecutionRepository):
        runtime = GuardedApplyRuntime(repository=repo)
        assert runtime.has_persistence is True

    def test_has_persistence_false_with_none_repo(self):
        runtime = GuardedApplyRuntime(repository=None)
        assert runtime.has_persistence is False


# ---------------------------------------------------------------------------
# Test: get_by_execution_id
# ---------------------------------------------------------------------------


class TestGetByExecutionId:
    def test_loads_by_execution_id(
        self, repo: GuardedApplyExecutionRepository, executed_result: GuardedApplyExecutionResult
    ):
        repo.save_execution_result(executed_result, execution_fingerprint="fp-execid-001")
        # Derive execution_id
        import hashlib

        raw = f"{executed_result.plan_id}|test-persist-001"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        exec_id = f"exec-{digest[:16]}"

        loaded = repo.get_by_execution_id(exec_id)
        assert loaded is not None
        assert loaded.idempotency_key == "test-persist-001"

    def test_returns_none_for_unknown_execution_id(self, repo: GuardedApplyExecutionRepository):
        loaded = repo.get_by_execution_id("exec-nonexistent")
        assert loaded is None
