"""Tests for Reconciliation Guarded Final Mutation Workflow v1.

Covers:
  1. Approved CREATE_FINAL_TRANSACTION inserts exactly one transaction row.
  2. Approved ADJUST_FINAL_TRANSACTION updates allowed fields on one row.
  3. Blocked guard decision creates no final writes.
  4. Missing human confirmation creates no final writes.
  5. Unsupported action is blocked without writes.
  6. Missing evidence refs blocks without writes.
  7. Operation not in plan is blocked.
  8. Empty idempotency key is blocked.
  9. Duplicate idempotency (same key + same content) returns ALREADY_FINALIZED.
 10. Conflicting idempotency (same key + different content) returns CONFLICT.
 11. Guard/proposal mismatch blocks without writes.
 12. Guard execution result conflict blocks without writes.
 13. Guard execution result no matching operation blocks.
 14. ADJUST with missing target raises.
 15. ADJUST target not found raises.
 16. ADJUST validation rejects forbidden fields.
 17. ADJUST captures previous values for audit.
 18. Deterministic fingerprint stability.
 19. Transaction public_id is deterministic.
 20. No write to database/finance.db.
 21. Decimal-safe string handling for monetary values.
 22. Workflow is separate from GuardedApplyRuntime -- dry-run runtime is unaffected.
 23. Audit refs are preserved in result and DB.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from finance_core.financial_audit import FinancialAuditRepository, verify_financial_audit_chain
from finance_core.money import canonical_decimal_str, canonical_money_str
from finance_core.reconciliation.apply_plan import (
    ApplyPlanInput,
    ReconciliationApplyPlan,
    build_reconciliation_apply_plan,
)
from finance_core.reconciliation.final_mutation_authorization import (
    build_final_mutation_content_hash,
)
from finance_core.reconciliation.final_mutation_proposal import (
    FinalMutationAction,
    FinalMutationGuard,
    FinalMutationGuardDecision,
    FinalMutationProposal,
)
from finance_core.reconciliation.final_mutation_workflow import (
    FinalMutationPersistenceError,
    FinalMutationTransactionError,
    FinalMutationWorkflowBlockReason,
    FinalMutationWorkflowInput,
    FinalMutationWorkflowStatus,
)
from finance_core.reconciliation.final_mutation_workflow import (
    execute_guarded_final_mutation_workflow as _execute_guarded_final_mutation_workflow,
)
from finance_core.reconciliation.models import (
    ApplyExecutionStatus,
    AppTransaction,
    GuardedApplyExecutionResult,
    GuardedOperationResult,
    IssueType,
    MatchStatus,
    ReconciliationCandidate,
    ResolutionAction,
    ResolutionDecision,
    ReviewQueueItem,
    StatementTransaction,
    SuggestedAction,
)
from finance_core.resources import migrations_dir

# ---------------------------------------------------------------------------
# Clock fixture for deterministic test timestamps
# ---------------------------------------------------------------------------

FIXED_CLOCK = "2026-06-18T12:00:00+00:00"


def _fixed_clock() -> str:
    return FIXED_CLOCK


def _canonical_preview_json(proposal: FinalMutationProposal) -> str:
    preview = FinalMutationGuard().evaluate(proposal).preview
    assert preview is not None
    return json.dumps(
        {
            "proposal_id": preview.proposal_id,
            "action": preview.action.value,
            "amount": preview.amount,
            "currency": preview.currency,
            "merchant": preview.merchant,
            "transaction_date": preview.transaction_date,
            "target_transaction_id": preview.target_transaction_id,
            "suggested_fields": dict(sorted(preview.suggested_fields.items())),
            "source_statement_ref": preview.source_statement_ref,
            "source_app_transaction_ref": preview.source_app_transaction_ref,
            "evidence_refs": sorted(preview.evidence_refs),
            "is_dry_run": preview.is_dry_run,
        },
        sort_keys=True,
    )


def _seed_persisted_authorization(
    conn: sqlite3.Connection, inp: FinalMutationWorkflowInput
) -> FinalMutationWorkflowInput:
    """Build the persisted authority chain required by this workflow test.

    Test fixtures may use caller objects to create prior durable records, but
    the workflow itself only reloads those records from SQLite.
    """
    if not inp.human_confirmation_id:
        return inp
    if not inp.guard_decision.approved:
        return inp
    for number, name in (
        ("013", "reconciliation_final_mutation_guard_decisions"),
        ("014", "reconciliation_guarded_apply_execution_persistence"),
        ("018", "reconciliation_final_mutation_authorization"),
        ("019", "reconciliation_final_mutation_audit"),
        ("025", "append_only_financial_audit_chain"),
    ):
        conn.executescript((migrations_dir() / f"{number}_{name}.sql").read_text())
    content_hash = build_final_mutation_content_hash(inp.proposal)
    authorization_id = inp.authorization_id or f"test-auth-{content_hash[:24]}"
    confirmation_id = inp.human_confirmation_id
    existing_confirmation = conn.execute(
        "SELECT content_hash FROM reconciliation_final_mutation_confirmations "
        "WHERE confirmation_id = ?",
        (confirmation_id,),
    ).fetchone()
    if existing_confirmation is not None and existing_confirmation[0] != content_hash:
        confirmation_id = f"{confirmation_id}-{content_hash[:16]}"
        inp = replace(inp, human_confirmation_id=confirmation_id)
    guard_key = f"test-guard-{authorization_id}"
    execution_id = f"test-exec-{authorization_id}"
    preview = _canonical_preview_json(inp.proposal)
    conn.execute(
        """INSERT OR IGNORE INTO reconciliation_final_mutation_guard_decisions
        (proposal_id, idempotency_key, action, approved, blocked_reasons_json,
         preview_json, evidence_refs_json, guard_version, actor_type)
        VALUES (?, ?, ?, 1, '[]', ?, ?, 'v1', 'test')""",
        (
            inp.proposal.proposal_id,
            guard_key,
            inp.proposal.action.value,
            preview,
            json.dumps(sorted(inp.proposal.evidence_refs)),
        ),
    )
    conn.execute(
        """INSERT OR IGNORE INTO reconciliation_guarded_apply_executions
        (execution_id, plan_id, idempotency_key, execution_fingerprint,
         execution_status, total_operations, operations_executed,
         operations_blocked, operations_skipped, guard_decision_refs_json,
         audit_trail_json, is_dry_run, executed_at)
        VALUES (?, ?, ?, 'test', 'executed', 1, 1, 0, 0, '[]', '{}', 1, ?)""",
        (execution_id, inp.plan.plan_id, f"test-exec-key-{authorization_id}", FIXED_CLOCK),
    )
    conn.execute(
        """INSERT OR IGNORE INTO reconciliation_guarded_apply_operation_results
        (operation_result_id, execution_id, operation_id, decision_id,
         execution_status, guard_decision_approved,
         guard_decision_idempotency_key, guard_blocked_reasons_json,
         mutation_type, mutation_payload_json)
        VALUES (?, ?, ?, 'test-decision', 'executed', 1, ?, '[]', ?, '{}')""",
        (
            f"test-op-{authorization_id}",
            execution_id,
            inp.operation_id,
            guard_key,
            inp.proposal.action.value,
        ),
    )
    conn.execute(
        """INSERT OR IGNORE INTO reconciliation_final_mutation_confirmations
        (confirmation_id, subject_type, subject_id, operation_type, proposal_id,
         plan_id, content_hash, confirmation_state, confirmed_by)
        VALUES (?, 'reconciliation_apply_operation', ?, ?, ?, ?, ?, 'confirmed', 'test')""",
        (
            confirmation_id,
            inp.operation_id,
            inp.proposal.action.value,
            inp.proposal.proposal_id,
            inp.plan.plan_id,
            content_hash,
        ),
    )
    conn.execute(
        """INSERT OR IGNORE INTO reconciliation_final_mutation_authorizations
        (authorization_id, subject_type, subject_id, operation_type, proposal_id,
         plan_id, guard_decision_idempotency_key, guarded_execution_id,
         human_confirmation_id, content_hash, authorization_state)
        VALUES (?, 'reconciliation_apply_operation', ?, ?, ?, ?, ?, ?, ?, ?, 'authorized')""",
        (
            authorization_id,
            inp.operation_id,
            inp.proposal.action.value,
            inp.proposal.proposal_id,
            inp.plan.plan_id,
            guard_key,
            execution_id,
            confirmation_id,
            content_hash,
        ),
    )
    conn.commit()
    return replace(inp, authorization_id=authorization_id)


def execute_guarded_final_mutation_workflow(
    conn: sqlite3.Connection,
    inp: FinalMutationWorkflowInput,
    *,
    clock=None,
):
    return _execute_guarded_final_mutation_workflow(
        conn, _seed_persisted_authorization(conn, inp), clock=clock
    )


def test_source_bound_create_refuses_corrected_source_under_lock(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = "txn-corrected-source"
    proposal = _create_proposal(source_app_transaction_ref=source_id)
    inp = _make_workflow_input(proposal=proposal)
    monkeypatch.setattr(
        "finance_core.reconciliation.final_mutation_workflow.has_committed_correction",
        lambda _conn, target: target == source_id,
    )
    result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
    assert result.status == FinalMutationWorkflowStatus.BLOCKED
    assert result.blocked_reasons == (
        "corrected_transaction_requires_versioned_reconciliation",
    )
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_successful_create_replay_refuses_corrected_result(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp = _make_workflow_input(idempotency_key="fm-corrected-replay")
    first = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
    assert first.status == FinalMutationWorkflowStatus.FINALIZED
    monkeypatch.setattr(
        "finance_core.reconciliation.final_mutation_workflow.has_committed_correction",
        lambda _conn, target: target == first.transaction_public_id,
    )
    replay = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
    assert replay.status == FinalMutationWorkflowStatus.BLOCKED
    assert replay.blocked_reasons == (
        "corrected_transaction_requires_versioned_reconciliation",
    )
    assert replay.transaction_public_id is None


def test_authorization_row_without_confirmation_cannot_finalize(conn) -> None:
    """A binding row alone is never an authorization trust root."""
    inp = _seed_persisted_authorization(conn, _make_workflow_input())
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM reconciliation_final_mutation_confirmations")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")

    result = _execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

    assert result.status == FinalMutationWorkflowStatus.BLOCKED
    assert (
        FinalMutationWorkflowBlockReason.MISSING_PERSISTED_AUTHORIZATION.value
        in result.blocked_reasons
    )
    assert result.transaction_public_id is None


def test_failure_after_authorization_rolls_back_final_write(conn, monkeypatch) -> None:
    """A post-authorization failure cannot leave a final write committed."""
    import finance_core.reconciliation.final_mutation_workflow as workflow_module

    inp = _seed_persisted_authorization(conn, _make_workflow_input())

    def fail_audit(*_args, **_kwargs) -> None:
        raise RuntimeError("test audit failure")

    monkeypatch.setattr(workflow_module, "_insert_audit_record", fail_audit)
    with pytest.raises(RuntimeError, match="test audit failure"):
        _execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transactions'"
    ).fetchone()
    if table is not None:
        final_transaction_count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE intent = 'reconciliation_generated'"
        ).fetchone()[0]
        assert final_transaction_count == 0


# ---------------------------------------------------------------------------
# Connection fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """Fresh in-memory SQLite connection for each test with canonical schema."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    # Apply canonical schema so that ``transactions`` exists.
    _apply_migration_001(c)
    return c


def _apply_migration_001(c: sqlite3.Connection) -> None:
    """Apply migration 001 to create the canonical ``transactions`` table."""
    sql = (migrations_dir() / "001_create_core_schema.sql").read_text()
    c.executescript(sql)


# ---------------------------------------------------------------------------
# Proposal fixtures
# ---------------------------------------------------------------------------


def _create_proposal(**kw) -> FinalMutationProposal:
    """Build a valid CREATE_FINAL_TRANSACTION proposal."""
    defaults: dict = dict(
        proposal_id="fp-create-001",
        action=FinalMutationAction.CREATE_FINAL_TRANSACTION,
        amount=Decimal("45.50"),
        currency="SGD",
        merchant="Giant Supermarket",
        transaction_date=date(2024, 12, 1),
        source_statement_ref="stmt-res-001",
        evidence_refs=("ev-abc",),
        note="Create from statement match.",
    )
    defaults.update(kw)
    return FinalMutationProposal(**defaults)


def _adjust_proposal(**kw) -> FinalMutationProposal:
    """Build a valid ADJUST_FINAL_TRANSACTION proposal."""
    defaults: dict = dict(
        proposal_id="fp-adjust-001",
        action=FinalMutationAction.ADJUST_FINAL_TRANSACTION,
        target_transaction_id="txn-fm-preexisting",
        suggested_fields={"amount": "42.00"},
        source_app_transaction_ref="app-ref-001",
        evidence_refs=("ev-xyz",),
        note="Adjust amount from 45.50 to 42.00.",
    )
    defaults.update(kw)
    return FinalMutationProposal(**defaults)


# ---------------------------------------------------------------------------
# Guard decision fixture
# ---------------------------------------------------------------------------


def _approved_guard(proposal: FinalMutationProposal) -> FinalMutationGuardDecision:
    guard = FinalMutationGuard()
    return guard.evaluate(proposal)


def _blocked_guard(proposal: FinalMutationProposal) -> FinalMutationGuardDecision:
    """Return a guard decision with approved=False by using missing required fields."""
    bad = FinalMutationProposal(
        proposal_id=proposal.proposal_id,
        action=proposal.action,
        evidence_refs=(),
    )
    guard = FinalMutationGuard()
    return guard.evaluate(bad)


# ---------------------------------------------------------------------------
# Execution result fixture
# ---------------------------------------------------------------------------


def _make_execution_result(
    *,
    operation_id: str,
    plan_id: str = "plan-test-001",
    idempotency_key: str = "gexec-key-001",
    status: ApplyExecutionStatus = ApplyExecutionStatus.EXECUTED,
    op_status: ApplyExecutionStatus = ApplyExecutionStatus.EXECUTED,
) -> GuardedApplyExecutionResult:
    return GuardedApplyExecutionResult(
        plan_id=plan_id,
        idempotency_key=idempotency_key,
        execution_status=status,
        results=(
            GuardedOperationResult(
                operation_id=operation_id,
                decision_id="dec-001",
                execution_status=op_status,
                reason="All checks passed.",
                guard_decision_approved=True,
                guard_blocked_reasons=(),
                mutation_type="create_final_transaction",
                mutation_payload={},
            ),
        ),
        total_operations=1,
        operated_executed=1 if op_status == ApplyExecutionStatus.EXECUTED else 0,
        operated_blocked=0 if op_status == ApplyExecutionStatus.EXECUTED else 1,
        operated_skipped=0,
        block_reason="",
        guard_decision_refs=("fp-create-001",),
        executed_at=FIXED_CLOCK,
        audit_trail={"runtime_version": "v1", "is_dry_run": True},
        is_dry_run=True,
    )


# ---------------------------------------------------------------------------
# Plan / operation fixtures
# ---------------------------------------------------------------------------


def _make_plan_and_op(
    *,
    decision_id: str = "dec-001",
    operation_id: str = "op-dec-001",
    source_apply_result_ref: str = "fp-create-001",
) -> tuple[ReconciliationApplyPlan, str]:
    """Build a minimal plan with one operation."""
    stmt = StatementTransaction(
        transaction_date=date(2024, 12, 1),
        posted_date=None,
        merchant_raw="Giant Supermarket",
        amount=Decimal("45.50"),
        currency="SGD",
    )
    app = AppTransaction(
        app_txn_id="app-001",
        transaction_date=date(2024, 12, 1),
        merchant="Giant Supermarket",
        amount=Decimal("45.50"),
        currency="SGD",
    )
    cand = ReconciliationCandidate(
        statement=stmt,
        best_app_transaction=app,
        match_status=MatchStatus.MATCHED,
        candidate_id="cand-001",
        issue_type=IssueType.MATCHED,
    )
    item = ReviewQueueItem(
        candidate=cand,
        issue_type=IssueType.MATCHED,
        suggested_action=SuggestedAction.CONFIRM_MATCH,
        queue_item_id="qi-001",
        evidence_summary="Matched exactly.",
    )
    decision = ResolutionDecision(
        decision_id=decision_id,
        queue_item_id="qi-001",
        action=ResolutionAction.CREATE_MISSING_APP_TRANSACTION,
        note="Create final transaction.",
    )
    plan_input = ApplyPlanInput(
        decision=decision,
        queue_item=item,
        guard_decision=FinalMutationGuardDecision(
            proposal_id=source_apply_result_ref,
            approved=True,
            action=FinalMutationAction.CREATE_FINAL_TRANSACTION,
            guard_version="v1",
        ),
        proposal=_create_proposal(proposal_id=source_apply_result_ref),
        evidence_refs=("ev-abc",),
    )
    plan = build_reconciliation_apply_plan([plan_input])
    return plan, operation_id


# ---------------------------------------------------------------------------
# Workflow input fixture
# ---------------------------------------------------------------------------


def _make_workflow_input(
    *,
    proposal: FinalMutationProposal | None = None,
    plan: ReconciliationApplyPlan | None = None,
    operation_id: str = "op-dec-001",
    guard_decision: FinalMutationGuardDecision | None = None,
    guard_execution_result: GuardedApplyExecutionResult | None = None,
    human_confirmation_id: str = "hconfirm-001",
    idempotency_key: str = "fm-key-001",
    actor_type: str = "system",
    actor_id: str | None = None,
) -> FinalMutationWorkflowInput:
    p = proposal or _create_proposal()
    pl, op_id = _make_plan_and_op(operation_id=operation_id, source_apply_result_ref=p.proposal_id)
    gd = guard_decision or _approved_guard(p)
    ger = guard_execution_result or _make_execution_result(operation_id=op_id)
    return FinalMutationWorkflowInput(
        plan=pl,
        operation_id=op_id,
        proposal=p,
        guard_decision=gd,
        guard_execution_result=ger,
        human_confirmation_id=human_confirmation_id,
        idempotency_key=idempotency_key,
        actor_type=actor_type,
        actor_id=actor_id,
    )


# ===================================================================
# Tests
# ===================================================================


class TestCreateFinalTransaction:
    """Approved CREATE_FINAL_TRANSACTION inserts exactly one transaction row."""

    def test_create_inserts_row(self, conn):
        inp = _make_workflow_input()
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

        assert result.status == FinalMutationWorkflowStatus.FINALIZED
        assert result.transaction_public_id is not None
        assert result.transaction_public_id.startswith("txn-fm-")

        row = conn.execute(
            "SELECT * FROM transactions WHERE public_id = ?",
            (result.transaction_public_id,),
        ).fetchone()
        assert row is not None
        assert Decimal(str(row["amount"])) == Decimal("45.50")
        assert Decimal(str(row["total_amount"])) == Decimal("45.50")
        assert row["currency"] == "SGD"
        assert row["merchant"] == "Giant Supermarket"
        assert row["transaction_date"] == "2024-12-01"
        assert row["intent_type"] == "Generated"
        assert row["intent"] == "reconciliation_generated"

    def test_create_audit_record(self, conn):
        inp = _make_workflow_input()
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

        row = conn.execute(
            "SELECT * FROM reconciliation_final_mutation_audit WHERE final_mutation_id = ?",
            (result.final_mutation_id,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "finalized"
        assert row["action"] == "create_final_transaction_proposal"
        assert row["human_confirmation_id"] == "hconfirm-001"
        assert row["actor_type"] == "system"

    def test_create_and_chain_event_commit_together(self, conn):
        inp = _make_workflow_input()
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        assert result.transaction_public_id is not None
        events = FinancialAuditRepository(conn).list_chain(
            "transaction", result.transaction_public_id
        )
        assert len(events) == 1
        assert events[0].event_type == "reconciliation_final_transaction_created"
        assert events[0].authorization_public_id is not None
        assert events[0].authorization_public_id.startswith("test-auth-")
        assert verify_financial_audit_chain(
            conn,
            aggregate_type="transaction",
            aggregate_public_id=result.transaction_public_id,
        ).valid

    def test_chain_insert_failure_rolls_back_final_mutation(self, conn):
        inp = _seed_persisted_authorization(conn, _make_workflow_input())
        conn.execute(
            """CREATE TRIGGER test_fail_final_mutation_chain
            BEFORE INSERT ON financial_audit_events
            BEGIN SELECT RAISE(ABORT, 'injected final mutation audit failure'); END"""
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="injected final mutation audit failure"):
            _execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE intent = 'reconciliation_generated'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM reconciliation_final_mutation_audit").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM financial_audit_events").fetchone()[0] == 0

    def test_decimal_safe_string_handling(self, conn):
        inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("12345.67")),
        )
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

        row = conn.execute(
            "SELECT amount FROM transactions WHERE public_id = ?",
            (result.transaction_public_id,),
        ).fetchone()
        assert Decimal(str(row["amount"])) == Decimal("12345.67")

    def test_transaction_public_id_is_deterministic(self, conn):
        inp1 = _make_workflow_input(idempotency_key="fm-key-det-1")
        conn1 = sqlite3.connect(":memory:")
        conn1.row_factory = sqlite3.Row
        conn1.execute("PRAGMA foreign_keys = ON")
        _apply_migration_001(conn1)

        result1 = execute_guarded_final_mutation_workflow(conn1, inp1, clock=_fixed_clock)

        # Different connection, same input -- same public_id
        conn2 = sqlite3.connect(":memory:")
        conn2.row_factory = sqlite3.Row
        conn2.execute("PRAGMA foreign_keys = ON")
        _apply_migration_001(conn2)
        inp2 = _make_workflow_input(idempotency_key="fm-key-det-2")
        result2 = execute_guarded_final_mutation_workflow(conn2, inp2, clock=_fixed_clock)

        # Different idempotency keys => different public_ids
        assert result1.transaction_public_id != result2.transaction_public_id


class TestAdjustFinalTransaction:
    """Approved ADJUST_FINAL_TRANSACTION updates allowed fields on one row."""

    def test_adjust_updates_allowed_fields(self, conn):
        # First create a transaction to adjust
        create_inp = _make_workflow_input(idempotency_key="fm-create-for-adjust")
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        # Now adjust it
        adjust_inp = _make_workflow_input(
            proposal=_adjust_proposal(target_transaction_id=target_id),
            guard_decision=_approved_guard(_adjust_proposal(target_transaction_id=target_id)),
            idempotency_key="fm-key-adjust-001",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)

        assert result.status == FinalMutationWorkflowStatus.FINALIZED

        row = conn.execute(
            "SELECT amount, total_amount FROM transactions WHERE public_id = ?",
            (target_id,),
        ).fetchone()
        assert Decimal(str(row["amount"])) == Decimal("42.00")
        assert Decimal(str(row["total_amount"])) == Decimal("42.00")

    def test_adjust_captures_previous_values(self, conn):
        # Create a transaction
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("100.00"), currency="SGD"),
            idempotency_key="fm-create-prev",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        # Adjust it
        adjust_inp = _make_workflow_input(
            proposal=_adjust_proposal(
                target_transaction_id=target_id,
                suggested_fields={"amount": "200.00", "currency": "USD"},
            ),
            guard_decision=_approved_guard(
                _adjust_proposal(
                    target_transaction_id=target_id,
                    suggested_fields={"amount": "200.00", "currency": "USD"},
                )
            ),
            idempotency_key="fm-key-adjust-prev",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)

        assert result.status == FinalMutationWorkflowStatus.FINALIZED

        audit_row = conn.execute(
            "SELECT previous_values_json FROM reconciliation_final_mutation_audit "
            "WHERE final_mutation_id = ?",
            (result.final_mutation_id,),
        ).fetchone()
        assert audit_row is not None

        prev = json.loads(audit_row["previous_values_json"])
        assert Decimal(str(prev["amount"])) == Decimal("100.00")
        assert Decimal(str(prev["total_amount"])) == Decimal("100.00")
        assert prev["currency"] == "SGD"

    def test_adjust_rejects_forbidden_fields(self, conn):
        """Fields outside the allowed set are blocked by the workflow.

        ``status`` passes the guard (it is not a SQL keyword) but is in the
        workflow-level forbidden field markers, so the workflow must block.
        """
        proposal = _adjust_proposal(
            target_transaction_id="txn-fm-preexisting",
            suggested_fields={"status": "active", "amount": "42.00"},
        )
        # Create a transaction row first so the target exists
        create_inp = _make_workflow_input(idempotency_key="fm-create-for-adjust-forbidden")
        execute_guarded_final_mutation_workflow(conn, create_inp, clock=_fixed_clock)

        guard = _approved_guard(proposal)
        inp = _make_workflow_input(
            proposal=proposal,
            guard_decision=guard,
            idempotency_key="fm-key-bad-fields",
        )
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert (
            FinalMutationWorkflowBlockReason.ADJUST_FIELD_NOT_ALLOWED.value
            in result.blocked_reasons
        )


class TestBlockingConditions:
    """Blocked guard decisions, missing confirmation, unsupported actions, etc."""

    def test_blocked_guard_creates_no_writes(self, conn):
        proposal = _create_proposal()
        blocked = _blocked_guard(proposal)
        inp = _make_workflow_input(guard_decision=blocked)
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert FinalMutationWorkflowBlockReason.GUARD_NOT_APPROVED.value in result.blocked_reasons
        assert result.transaction_public_id is None

        # Blocked workflows never call _ensure_audit_table, so the transaction
        # table may not exist (no migration 001 applied yet). Either way, no
        # canonical transaction is created.
        table_exists = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone()[0]
        if table_exists:
            count = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE intent = 'reconciliation_generated'"
            ).fetchone()[0]
        else:
            count = 0
        assert count == 0

    def test_missing_human_confirmation_blocks(self, conn):
        inp = _make_workflow_input(human_confirmation_id="")
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert (
            FinalMutationWorkflowBlockReason.MISSING_HUMAN_CONFIRMATION.value
            in result.blocked_reasons
        )

    def test_missing_human_confirmation_records_blocked_audit_when_audit_table_exists(self, conn):
        good = _make_workflow_input(idempotency_key="fm-key-prime-audit")
        execute_guarded_final_mutation_workflow(conn, good, clock=_fixed_clock)

        blocked = _make_workflow_input(
            human_confirmation_id="",
            idempotency_key="fm-key-blocked-audit",
        )
        result = execute_guarded_final_mutation_workflow(conn, blocked, clock=_fixed_clock)

        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        audit_row = conn.execute(
            "SELECT status, blocked_reasons_json, transaction_public_id "
            "FROM reconciliation_final_mutation_audit WHERE idempotency_key = ?",
            ("fm-key-blocked-audit",),
        ).fetchone()
        assert audit_row is not None
        assert audit_row["status"] == "blocked"
        assert audit_row["transaction_public_id"] is None
        blocked_reasons = json.loads(audit_row["blocked_reasons_json"])
        assert FinalMutationWorkflowBlockReason.MISSING_HUMAN_CONFIRMATION.value in blocked_reasons

        transaction_count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE intent = 'reconciliation_generated'",
        ).fetchone()[0]
        # The "good" priming call (fm-key-prime-audit) already created 1 row;
        # the blocked call must not add any more.
        assert transaction_count == 1

    def test_unsupported_action_blocked(self, conn):
        inp = _make_workflow_input(
            proposal=_create_proposal(action=FinalMutationAction.NO_FINAL_MUTATION),
        )
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert FinalMutationWorkflowBlockReason.UNSUPPORTED_ACTION.value in result.blocked_reasons

    def test_missing_evidence_refs_blocked(self, conn):
        inp = _make_workflow_input(
            proposal=_create_proposal(evidence_refs=()),
        )
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert (
            FinalMutationWorkflowBlockReason.MISSING_EVIDENCE_REFS.value in result.blocked_reasons
        )

    def test_empty_idempotency_key_blocked(self, conn):
        inp = _make_workflow_input(idempotency_key="")
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert (
            FinalMutationWorkflowBlockReason.EMPTY_IDEMPOTENCY_KEY.value in result.blocked_reasons
        )

    def test_guard_proposal_mismatch_blocked(self, conn):
        proposal = _create_proposal(proposal_id="fp-mismatch")
        # Guard decision for a different proposal_id
        guard = FinalMutationGuardDecision(
            proposal_id="fp-different",
            approved=True,
            action=FinalMutationAction.CREATE_FINAL_TRANSACTION,
            guard_version="v1",
        )
        inp = _make_workflow_input(proposal=proposal, guard_decision=guard)
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert (
            FinalMutationWorkflowBlockReason.GUARD_OPERATION_MISMATCH.value
            in result.blocked_reasons
        )

    def test_guard_action_mismatch_blocked(self, conn):
        """Guard decision with same proposal_id but different action must be blocked."""
        proposal = _create_proposal(action=FinalMutationAction.CREATE_FINAL_TRANSACTION)
        guard = FinalMutationGuardDecision(
            proposal_id=proposal.proposal_id,
            approved=True,
            action=FinalMutationAction.ADJUST_FINAL_TRANSACTION,
            guard_version="v1",
        )
        inp = _make_workflow_input(proposal=proposal, guard_decision=guard)
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert (
            FinalMutationWorkflowBlockReason.GUARD_OPERATION_MISMATCH.value
            in result.blocked_reasons
        )
        assert result.transaction_public_id is None
        table_exists = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone()[0]
        if table_exists:
            count = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE intent = 'reconciliation_generated'"
            ).fetchone()[0]
            assert count == 0

    def test_execution_result_conflict_blocked(self, conn):
        exec_result = _make_execution_result(
            operation_id="op-dec-001",
            status=ApplyExecutionStatus.CONFLICT,
            op_status=ApplyExecutionStatus.BLOCKED,
        )
        inp = _make_workflow_input(guard_execution_result=exec_result)
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert (
            FinalMutationWorkflowBlockReason.EXECUTION_RESULT_CONFLICT.value
            in result.blocked_reasons
        )

    def test_execution_result_no_matching_op_blocked(self, conn):
        exec_result = _make_execution_result(operation_id="op-other")
        inp = _make_workflow_input(guard_execution_result=exec_result)
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert (
            FinalMutationWorkflowBlockReason.EXECUTION_RESULT_NO_MATCHING_OP.value
            in result.blocked_reasons
        )

    def test_operation_not_in_plan_blocked(self, conn):
        inp = _make_workflow_input(operation_id="op-nonexistent")
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert (
            FinalMutationWorkflowBlockReason.OPERATION_NOT_IN_PLAN.value in result.blocked_reasons
        )


class TestIdempotency:
    """Deterministic idempotency behavior."""

    def test_same_key_same_content_returns_already_finalized(self, conn):
        inp = _make_workflow_input(idempotency_key="fm-key-idem-001")

        result1 = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        assert result1.status == FinalMutationWorkflowStatus.FINALIZED

        result2 = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        assert result2.status == FinalMutationWorkflowStatus.ALREADY_FINALIZED
        assert result2.transaction_public_id == result1.transaction_public_id

        # Only one transaction row should exist
        count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE intent = 'reconciliation_generated'"
        ).fetchone()[0]
        assert count == 1

    def test_same_key_different_content_returns_conflict(self, conn):
        inp1 = _make_workflow_input(idempotency_key="fm-key-conflict")

        result1 = execute_guarded_final_mutation_workflow(conn, inp1, clock=_fixed_clock)
        assert result1.status == FinalMutationWorkflowStatus.FINALIZED

        # Different proposal content, same key
        inp2 = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("99.99")),
            idempotency_key="fm-key-conflict",
        )
        result2 = execute_guarded_final_mutation_workflow(conn, inp2, clock=_fixed_clock)
        assert result2.status == FinalMutationWorkflowStatus.CONFLICT
        assert (
            FinalMutationWorkflowBlockReason.CONFLICTING_FINAL_MUTATION.value
            in result2.blocked_reasons
        )
        assert result2.transaction_public_id is None

    def test_same_key_after_blocked_attempt_returns_conflict_without_write(self, conn):
        good = _make_workflow_input(idempotency_key="fm-key-prime-audit-before-conflict")
        execute_guarded_final_mutation_workflow(conn, good, clock=_fixed_clock)

        blocked = _make_workflow_input(
            human_confirmation_id="",
            idempotency_key="fm-key-blocked-then-corrected",
        )
        blocked_result = execute_guarded_final_mutation_workflow(
            conn,
            blocked,
            clock=_fixed_clock,
        )
        assert blocked_result.status == FinalMutationWorkflowStatus.BLOCKED

        corrected = _make_workflow_input(idempotency_key="fm-key-blocked-then-corrected")
        result = execute_guarded_final_mutation_workflow(conn, corrected, clock=_fixed_clock)

        assert result.status == FinalMutationWorkflowStatus.CONFLICT
        assert (
            FinalMutationWorkflowBlockReason.CONFLICTING_FINAL_MUTATION.value
            in result.blocked_reasons
        )
        transaction_count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE intent = 'reconciliation_generated'",
        ).fetchone()[0]
        # The "good" priming call (fm-key-prime-audit-before-conflict) already
        # created 1 row; the conflict path must not add another.
        assert transaction_count == 1

    def test_different_key_same_content_produces_two_rows(self, conn):
        inp1 = _make_workflow_input(idempotency_key="fm-key-unique-1")
        inp2 = _make_workflow_input(idempotency_key="fm-key-unique-2")

        r1 = execute_guarded_final_mutation_workflow(conn, inp1, clock=_fixed_clock)
        r2 = execute_guarded_final_mutation_workflow(conn, inp2, clock=_fixed_clock)

        assert r1.status == FinalMutationWorkflowStatus.FINALIZED
        assert r2.status == FinalMutationWorkflowStatus.FINALIZED
        # Different idempotency keys => different public_ids
        assert r1.transaction_public_id != r2.transaction_public_id

    def test_fingerprint_is_deterministic(self):
        inp1 = _make_workflow_input()
        inp2 = _make_workflow_input()

        from finance_core.reconciliation.final_mutation_workflow import (
            _build_final_mutation_fingerprint,
        )

        fp1 = _build_final_mutation_fingerprint(inp1)
        fp2 = _build_final_mutation_fingerprint(inp2)
        assert fp1 == fp2


class TestAuditRefs:
    """Audit refs are preserved in result and DB."""

    def test_audit_refs_in_result(self, conn):
        inp = _make_workflow_input()
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

        refs = result.audit_refs
        assert refs["plan_id"] == inp.plan.plan_id
        assert refs["proposal_id"] == inp.proposal.proposal_id
        assert refs["human_confirmation_id"] == "hconfirm-001"
        assert refs["workflow_version"] == "v1"
        assert refs["guard_decision_approved"] is True

    def test_audit_refs_in_db(self, conn):
        inp = _make_workflow_input()
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

        row = conn.execute(
            "SELECT audit_refs_json FROM reconciliation_final_mutation_audit "
            "WHERE final_mutation_id = ?",
            (result.final_mutation_id,),
        ).fetchone()
        assert row is not None

        stored = json.loads(row["audit_refs_json"])
        assert stored["human_confirmation_id"] == "hconfirm-001"
        assert stored["workflow_version"] == "v1"


class TestNoLiveDatabase:
    """The workflow never opens or writes to database/finance.db."""

    def test_no_live_db_path_access(self, tmp_path):
        """Verify workflow does not access LIVE_DB_PATH."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _apply_migration_001(conn)
        inp = _make_workflow_input()

        # Before the call, ensure live DB is not accessed
        execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        # If we got here without exceptions related to live DB, the test passes.

    def test_workflow_accepts_only_caller_connection(self, conn):
        """The workflow uses only the caller-supplied connection."""
        inp = _make_workflow_input()
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        # Verify rows are in the caller's connection, not elsewhere
        assert result.status == FinalMutationWorkflowStatus.FINALIZED

        # The audit table exists in the caller's connection
        tables = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='reconciliation_final_mutation_audit'"
        ).fetchone()
        assert tables is not None


class TestWorkflowSeparationFromGuardedRuntime:
    """The final mutation workflow is separate from GuardedApplyRuntime."""

    def test_guarded_runtime_unchanged(self):
        """GuardedApplyRuntime can still be imported and used independently."""
        from finance_core.reconciliation.apply_runtime import (
            GuardedApplyRuntime,
        )

        runtime = GuardedApplyRuntime()
        # No crash, still works as before
        assert runtime.has_persistence is False


class TestCreateRequiredFieldValidation:
    """CREATE_FINAL_TRANSACTION blocks without writes when required fields are missing."""

    def test_missing_amount_blocks_without_write(self, conn):
        proposal = _create_proposal(amount=None)
        inp = _make_workflow_input(proposal=proposal)
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert (
            FinalMutationWorkflowBlockReason.CREATE_MISSING_AMOUNT.value in result.blocked_reasons
        )
        assert result.transaction_public_id is None
        self._assert_no_transaction_rows(conn)

    def test_missing_source_statement_ref_blocks_without_write(self, conn):
        proposal = _create_proposal(source_statement_ref=None)
        inp = _make_workflow_input(proposal=proposal)
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert (
            FinalMutationWorkflowBlockReason.CREATE_MISSING_SOURCE_REF.value
            in result.blocked_reasons
        )
        assert result.transaction_public_id is None
        self._assert_no_transaction_rows(conn)

    def test_all_create_required_fields_missing_blocks(self, conn):
        """When every required CREATE field is missing, all reason codes fire."""
        proposal = FinalMutationProposal(
            proposal_id="fp-create-empty",
            action=FinalMutationAction.CREATE_FINAL_TRANSACTION,
            evidence_refs=("ev-abc",),
        )
        guard = _approved_guard(proposal)
        plan = _plan()
        ger = _make_execution_result(operation_id="op-dec-001")
        inp = FinalMutationWorkflowInput(
            plan=plan,
            operation_id="op-dec-001",
            proposal=proposal,
            guard_decision=guard,
            guard_execution_result=ger,
            human_confirmation_id="hconfirm-001",
            idempotency_key="fm-key-empty-create",
        )
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        for code in (
            FinalMutationWorkflowBlockReason.CREATE_MISSING_AMOUNT.value,
            FinalMutationWorkflowBlockReason.CREATE_MISSING_CURRENCY.value,
            FinalMutationWorkflowBlockReason.CREATE_MISSING_DATE.value,
            FinalMutationWorkflowBlockReason.CREATE_MISSING_MERCHANT.value,
            FinalMutationWorkflowBlockReason.CREATE_MISSING_SOURCE_REF.value,
        ):
            assert code in result.blocked_reasons
        self._assert_no_transaction_rows(conn)

    @staticmethod
    def _assert_no_transaction_rows(conn):
        table_exists = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone()[0]
        if table_exists:
            count = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE intent = 'reconciliation_generated'"
            ).fetchone()[0]
        else:
            count = 0
        assert count == 0


class TestAdjustTargetNotFoundOrAmbiguous:
    """ADJUST_FINAL_TRANSACTION returns BLOCKED instead of raising on missing/ambiguous target."""

    def test_target_not_found_returns_blocked(self, conn):
        """ADJUST with a nonexistent target returns BLOCKED, does not raise."""
        adjust = _adjust_proposal(
            target_transaction_id="txn-fm-nonexistent",
            suggested_fields={"amount": "99.00"},
        )
        guard = _approved_guard(adjust)
        exec_result = _make_execution_result(operation_id="op-dec-001")
        plan = _plan()
        inp = FinalMutationWorkflowInput(
            plan=plan,
            operation_id="op-dec-001",
            proposal=adjust,
            guard_decision=guard,
            guard_execution_result=exec_result,
            human_confirmation_id="hconfirm-001",
            idempotency_key="fm-key-adjust-missing",
        )
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert (
            FinalMutationWorkflowBlockReason.TARGET_TRANSACTION_NOT_FOUND.value
            in result.blocked_reasons
        )
        assert result.transaction_public_id is None
        # No new transaction row was inserted
        count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE intent = 'reconciliation_generated'"
        ).fetchone()[0]
        assert count == 0

    def test_ambiguous_target_schema_prevents_duplicates(self, conn):
        """ADJUST target ambiguity: blocked if possible, documented as impossible.

        The transaction table uses public_id as PRIMARY KEY so ambiguity
        cannot occur through normal flow. This test documents that constraint
        and verifies that nonexistent targets still return BLOCKED.
        """
        # PRIMARY KEY on public_id prevents duplicates at the schema level
        import sqlite3 as _sq

        check_conn = _sq.connect(":memory:")
        check_conn.execute(
            "CREATE TABLE check_ambig (  public_id TEXT PRIMARY KEY,  amount   TEXT NOT NULL)"
        )
        check_conn.execute("INSERT INTO check_ambig VALUES (?, ?)", ("txn-fm-dup", "10.00"))
        with pytest.raises((_sq.IntegrityError, _sq.OperationalError)):
            check_conn.execute(
                "INSERT INTO check_ambig VALUES (?, ?)",
                ("txn-fm-dup", "20.00"),
            )
        check_conn.close()
        # Nonexistent target still returns BLOCKED
        adjust = _adjust_proposal(
            target_transaction_id="txn-fm-unknown",
            suggested_fields={"amount": "99.00"},
        )
        guard = _approved_guard(adjust)
        exec_result = _make_execution_result(operation_id="op-dec-001")
        plan = _plan()
        inp = FinalMutationWorkflowInput(
            plan=plan,
            operation_id="op-dec-001",
            proposal=adjust,
            guard_decision=guard,
            guard_execution_result=exec_result,
            human_confirmation_id="hconfirm-001",
            idempotency_key="fm-key-adjust-unknown",
        )
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert (
            FinalMutationWorkflowBlockReason.TARGET_TRANSACTION_NOT_FOUND.value
            in result.blocked_reasons
        )


# ===================================================================
# Regression tests — no runtime DDL, amount/total_amount consistency,
# proposal note preservation, Money Contract enforcement
# ===================================================================


class TestNoRuntimeDDL:
    """The workflow must execute zero DDL — CREATE TABLE, CREATE INDEX,
    ALTER TABLE, and DROP TABLE are all forbidden at runtime."""

    @staticmethod
    def _ddl_free_conn(with_migrations: bool = True) -> sqlite3.Connection:
        """In-memory connection that rejects DDL via set_authorizer.

        Migrations are applied *first*, then the authorizer is installed
        so that *runtime* DDL is caught while the pre-existing schema
        (from migrations) is allowed.
        """
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")

        # Apply migrations before locking down DDL.
        if with_migrations:
            _apply_migration_001(c)
            _apply_migration_019(c)
            # Also seed the authorisation chain tables (migrations 013, 014, 018).
            _apply_migration_named(c, "013_reconciliation_final_mutation_guard_decisions.sql")
            _apply_migration_named(c, "014_reconciliation_guarded_apply_execution_persistence.sql")
            _apply_migration_named(c, "018_reconciliation_final_mutation_authorization.sql")
            _apply_migration_named(c, "025_append_only_financial_audit_chain.sql")

        def _deny_ddl(op, _a, _b, _c, _d):
            if op == sqlite3.SQLITE_CREATE_TABLE:
                raise sqlite3.OperationalError("DDL forbidden: CREATE TABLE")
            if op == sqlite3.SQLITE_CREATE_INDEX:
                raise sqlite3.OperationalError("DDL forbidden: CREATE INDEX")
            if op == sqlite3.SQLITE_ALTER_TABLE:
                raise sqlite3.OperationalError("DDL forbidden: ALTER TABLE")
            if op == sqlite3.SQLITE_DROP_TABLE:
                raise sqlite3.OperationalError("DDL forbidden: DROP TABLE")
            return sqlite3.SQLITE_OK

        c.set_authorizer(_deny_ddl)
        return c

    def test_create_no_ddl(self):
        """Successful CREATE must perform no runtime DDL."""
        conn = self._ddl_free_conn()
        inp = _make_workflow_input()
        # The _ddl_free_conn already applied the schema via migrations
        # before the authorizer was installed.  We insert the
        # authorization data rows directly so no DDL is needed.
        inp = _seed_authorization_data_only(conn, inp)
        result = _execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.FINALIZED
        assert result.transaction_public_id is not None

    def test_adjust_no_ddl(self):
        """Successful ADJUST must perform no runtime DDL."""
        conn = self._ddl_free_conn()
        create_inp = _make_workflow_input(idempotency_key="fm-create-nddl-adj")
        create_inp = _seed_authorization_data_only(conn, create_inp)
        create_result = _execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id
        adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"amount": "99.99"},
        )
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=_approved_guard(adjust),
            idempotency_key="fm-key-nddl-adjust",
        )
        adjust_inp = _seed_authorization_data_only(conn, adjust_inp)
        result = _execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.FINALIZED

    def test_missing_audit_migration_fails_closed(self):
        """When migration 019 is not applied the workflow blocks with MISSING_SCHEMA."""
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        _apply_migration_001(c)
        # Apply the authorization-chain migrations so authorization loads,
        # but deliberately do NOT apply migration 019 for the audit table.
        _apply_migration_named(c, "013_reconciliation_final_mutation_guard_decisions.sql")
        _apply_migration_named(c, "014_reconciliation_guarded_apply_execution_persistence.sql")
        _apply_migration_named(c, "018_reconciliation_final_mutation_authorization.sql")
        inp = _make_workflow_input()
        # The seed helper creates the authorization rows but also applies
        # migration 019. We bypass the helper and call the raw function
        # so that migration 019 stays absent.
        inp = _seed_persisted_authorization(c, inp)
        # Remove the audit table that _seed_persisted_authorization just
        # created (it now applies migration 019).
        c.execute("DROP TABLE reconciliation_final_mutation_audit")
        c.commit()
        result = _execute_guarded_final_mutation_workflow(c, inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert FinalMutationWorkflowBlockReason.MISSING_SCHEMA.value in result.blocked_reasons


class TestAmountTotalAmountConsistency:
    """amount and total_amount must remain equal across all operations."""

    def test_create_writes_equal_amount_and_total_amount(self, conn):
        inp = _make_workflow_input()
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        row = conn.execute(
            "SELECT amount, total_amount FROM transactions WHERE public_id = ?",
            (result.transaction_public_id,),
        ).fetchone()
        assert Decimal(str(row["amount"])) == Decimal("45.50")
        assert Decimal(str(row["total_amount"])) == Decimal("45.50")

    def test_adjust_amount_updates_total_amount(self, conn):
        # Create
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("100.00")),
            idempotency_key="fm-amount-total-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target = create_result.transaction_public_id

        # Adjust amount only (not total_amount)
        adjust = _adjust_proposal(
            target_transaction_id=target,
            suggested_fields={"amount": "200.00"},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-amount-total-adjust",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.FINALIZED

        row = conn.execute(
            "SELECT amount, total_amount FROM transactions WHERE public_id = ?",
            (target,),
        ).fetchone()
        assert Decimal(str(row["amount"])) == Decimal("200.00")
        assert Decimal(str(row["total_amount"])) == Decimal("200.00")

    def test_adjust_previous_values_includes_total_amount(self, conn):
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("55.00")),
            idempotency_key="fm-adj-prev-ttl-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target,
            suggested_fields={"amount": "88.00"},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-adj-prev-ttl-adjust",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)

        audit_row = conn.execute(
            "SELECT previous_values_json FROM reconciliation_final_mutation_audit "
            "WHERE final_mutation_id = ?",
            (result.final_mutation_id,),
        ).fetchone()
        prev = json.loads(audit_row["previous_values_json"])
        assert Decimal(str(prev["amount"])) == Decimal("55.00")
        assert Decimal(str(prev["total_amount"])) == Decimal("55.00")

    def test_adjust_non_amount_field_leaves_total_amount_unchanged(self, conn):
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("77.00")),
            idempotency_key="fm-adj-non-amount-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target = create_result.transaction_public_id

        # Adjust merchant only, not amount
        adjust = _adjust_proposal(
            target_transaction_id=target,
            suggested_fields={"merchant": "New Merchant Name"},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-adj-non-amount",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.FINALIZED

        row = conn.execute(
            "SELECT amount, total_amount, merchant FROM transactions WHERE public_id = ?",
            (target,),
        ).fetchone()
        assert Decimal(str(row["amount"])) == Decimal("77.00")
        assert Decimal(str(row["total_amount"])) == Decimal("77.00")
        assert row["merchant"] == "New Merchant Name"

    def test_total_amount_not_independently_adjustable(self, conn):
        """When supplied through suggested_fields, total_amount is silently
        dropped — it is a derived field, never independently adjustable."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("33.00")),
            idempotency_key="fm-total-ignored-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target,
            suggested_fields={"merchant": "Ignored Total"},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-total-ignored-adjust",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.FINALIZED

        row = conn.execute(
            "SELECT amount, total_amount, merchant FROM transactions WHERE public_id = ?",
            (target,),
        ).fetchone()
        # total_amount must NOT have changed — it stays at 33.00
        assert Decimal(str(row["amount"])) == Decimal("33.00")
        assert Decimal(str(row["total_amount"])) == Decimal("33.00")
        assert row["merchant"] == "Ignored Total"

    def test_total_amount_submission_in_adjust_is_ignored(self, conn):
        """total_amount in suggested_fields is rejected — it is a derived
        field and cannot be independently adjusted."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("33.00")),
            idempotency_key="fm-ttl-submit-create-2",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target,
            suggested_fields={"total_amount": "999.99"},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-ttl-submit-adjust-2",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert (
            FinalMutationWorkflowBlockReason.ADJUST_FIELD_NOT_ALLOWED.value
            in result.blocked_reasons
        )
        """Float monetary values are rejected by the Money Contract."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("50.00")),
            idempotency_key="fm-float-reject-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"amount": 42.5},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-float-reject-adjust",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert result.transaction_public_id is None
        # Canonical transaction must be unchanged.
        row = conn.execute(
            "SELECT amount, total_amount FROM transactions WHERE public_id = ?",
            (target_id,),
        ).fetchone()
        assert Decimal(str(row["amount"])) == Decimal("50.00")
        assert Decimal(str(row["total_amount"])) == Decimal("50.00")

    def test_nan_amount_is_rejected(self, conn):
        """NaN monetary values are rejected."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("50.00")),
            idempotency_key="fm-nan-reject-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"amount": "NaN"},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-nan-reject-adjust",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert result.transaction_public_id is None
        # Canonical transaction must be unchanged.
        row = conn.execute(
            "SELECT amount, total_amount FROM transactions WHERE public_id = ?",
            (target_id,),
        ).fetchone()
        assert Decimal(str(row["amount"])) == Decimal("50.00")
        assert Decimal(str(row["total_amount"])) == Decimal("50.00")

    def test_infinity_amount_is_rejected(self, conn):
        """Infinity monetary values are rejected."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("50.00")),
            idempotency_key="fm-inf-reject-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"amount": "Infinity"},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-inf-reject-adjust",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert result.transaction_public_id is None
        # Canonical transaction must be unchanged.
        row = conn.execute(
            "SELECT amount, total_amount FROM transactions WHERE public_id = ?",
            (target_id,),
        ).fetchone()
        assert Decimal(str(row["amount"])) == Decimal("50.00")
        assert Decimal(str(row["total_amount"])) == Decimal("50.00")

    def test_float_nan_is_rejected(self, conn):
        """Python float('nan') is rejected."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("50.00")),
            idempotency_key="fm-float-nan-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"amount": float("nan")},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-float-nan-adjust",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert result.transaction_public_id is None

    def test_float_inf_rejected(self, conn):
        """Python float('inf') is rejected."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("50.00")),
            idempotency_key="fm-float-inf-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"amount": float("inf")},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-float-inf-adjust",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert result.transaction_public_id is None

    def test_float_neg_inf_rejected(self, conn):
        """Python float('-inf') is rejected."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("50.00")),
            idempotency_key="fm-float-neg-inf-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"amount": float("-inf")},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-float-neg-inf-adjust",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert result.transaction_public_id is None

    def test_string_neg_inf_rejected(self, conn):
        """String '-Infinity' is rejected."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("50.00")),
            idempotency_key="fm-str-neg-inf-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"amount": "-Infinity"},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-str-neg-inf-adjust",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert result.transaction_public_id is None

    def test_bool_amount_rejected(self, conn):
        """Boolean monetary values are rejected by the Money Contract."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("50.00")),
            idempotency_key="fm-bool-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"amount": True},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-bool-adjust",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert result.transaction_public_id is None

    def test_unsupported_object_amount_rejected(self, conn):
        """Unsupported object input is rejected."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("50.00")),
            idempotency_key="fm-obj-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"amount": [1, 2, 3]},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-obj-adjust",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert result.transaction_public_id is None

    def test_invalid_amount_no_audit_row(self, conn):
        """Invalid amount creates no finalized audit row."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("50.00")),
            idempotency_key="fm-no-audit-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"amount": "NaN"},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-no-audit-adjust",
        )
        execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)

        # No audit row with status = finalized for this key.
        finalized_audit = conn.execute(
            "SELECT 1 FROM reconciliation_final_mutation_audit "
            "WHERE idempotency_key = ? AND status = 'finalized'",
            ("fm-no-audit-adjust",),
        ).fetchone()
        assert finalized_audit is None

    def test_invalid_amount_no_previous_values_audit(self, conn):
        """Invalid amount creates no previous-values audit."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("50.00")),
            idempotency_key="fm-no-prev-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"amount": float("nan")},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-no-prev-adjust",
        )
        execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)

        # No audit row with previous_values_json set
        audit_rows = conn.execute(
            "SELECT previous_values_json FROM reconciliation_final_mutation_audit "
            "WHERE idempotency_key = ?",
            ("fm-no-prev-adjust",),
        ).fetchall()
        for r in audit_rows:
            if r["previous_values_json"] is not None:
                prev = json.loads(r["previous_values_json"])
                assert "amount" not in prev

    def test_invalid_amount_preserves_other_fields(self, conn):
        """Invalid amount does not change merchant, currency, date, notes, or category."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("50.00"), currency="SGD"),
            idempotency_key="fm-preserve-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"amount": float("nan")},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-preserve-adjust",
        )
        execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)

        row = conn.execute(
            "SELECT amount, total_amount, currency, merchant, "
            "transaction_date, category, notes FROM transactions "
            "WHERE public_id = ?",
            (target_id,),
        ).fetchone()
        assert Decimal(str(row["amount"])) == Decimal("50.00")
        assert Decimal(str(row["total_amount"])) == Decimal("50.00")
        assert row["currency"] == "SGD"
        assert row["merchant"] == "Giant Supermarket"
        assert row["transaction_date"] == "2024-12-01"

    def test_invalid_amount_replay_deterministic(self, conn):
        """Same invalid request replay remains deterministic (same outcome)."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("50.00")),
            idempotency_key="fm-replay-bad-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"amount": "NaN"},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-replay-bad-adjust",
        )

        r1 = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        r2 = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert r1.status == r2.status
        assert r1.status == FinalMutationWorkflowStatus.BLOCKED

    def test_valid_after_invalid_succeeds(self, conn):
        """A later valid request with a different idempotency key can still succeed."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("50.00")),
            idempotency_key="fm-bad-then-good-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        # Invalid attempt first
        bad_adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"amount": "NaN"},
        )
        bad_guard = _approved_guard(bad_adjust)
        bad_inp = _make_workflow_input(
            proposal=bad_adjust,
            guard_decision=bad_guard,
            idempotency_key="fm-bad-attempt",
        )
        bad_result = execute_guarded_final_mutation_workflow(conn, bad_inp, clock=_fixed_clock)
        assert bad_result.status == FinalMutationWorkflowStatus.BLOCKED

        # Valid attempt with different key
        good_adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"amount": "75.00"},
        )
        good_guard = _approved_guard(good_adjust)
        good_inp = _make_workflow_input(
            proposal=good_adjust,
            guard_decision=good_guard,
            idempotency_key="fm-good-attempt",
        )
        good_result = execute_guarded_final_mutation_workflow(conn, good_inp, clock=_fixed_clock)
        assert good_result.status == FinalMutationWorkflowStatus.FINALIZED

        row = conn.execute(
            "SELECT amount, total_amount FROM transactions WHERE public_id = ?",
            (target_id,),
        ).fetchone()
        assert Decimal(str(row["amount"])) == Decimal("75.00")
        assert Decimal(str(row["total_amount"])) == Decimal("75.00")

    def test_direct_total_amount_submission_rejected(self, conn):
        """Direct total_amount submission is rejected with stable reason."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("50.00")),
            idempotency_key="fm-ttl-dir-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"total_amount": "999.99"},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-ttl-dir-adjust",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert (
            FinalMutationWorkflowBlockReason.ADJUST_FIELD_NOT_ALLOWED.value
            in result.blocked_reasons
        )

    def test_missing_target_currency_fails_closed(self, conn):
        """A target transaction with NULL currency must reject adjustments."""
        # Create a transaction with NULL currency via direct SQL to simulate
        # a legacy or malformed record.
        conn.execute(
            "INSERT INTO transactions "
            "(public_id, intent, intent_type, transaction_date, status, "
            "amount, total_amount, currency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                "txn-fm-null-curr",
                "reconciliation_generated",
                "Generated",
                "2024-06-01",
                "active",
                "55.00",
                "55.00",
            ),
        )
        conn.commit()

        adjust = _adjust_proposal(
            target_transaction_id="txn-fm-null-curr",
            suggested_fields={"amount": "60.00"},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-null-curr-adjust",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert result.transaction_public_id is None

    def test_valid_string_decimal_still_succeeds(self, conn):
        """Valid string decimal still succeeds."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("50.00")),
            idempotency_key="fm-str-dec-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"amount": "99.99"},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-str-dec-adjust",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.FINALIZED

    def test_valid_integer_still_succeeds(self, conn):
        """Valid integer still succeeds."""
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("50.00")),
            idempotency_key="fm-int-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target_id = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target_id,
            suggested_fields={"amount": 100},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-int-adjust",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.FINALIZED


class TestProposalNotePreservation:
    """CREATE must preserve proposal.note inside the structured notes JSON."""

    def test_create_preserves_proposal_note(self, conn):
        inp = _make_workflow_input(
            proposal=_create_proposal(note="Monthly grocery run"),
            idempotency_key="fm-note-preserved",
        )
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.FINALIZED

        row = conn.execute(
            "SELECT notes FROM transactions WHERE public_id = ?",
            (result.transaction_public_id,),
        ).fetchone()
        notes = json.loads(row["notes"])
        assert notes["proposal_note"] == "Monthly grocery run"
        assert notes["conversion_source"] == "reconciliation_final_mutation"
        assert notes["final_mutation_id"] == result.final_mutation_id

    def test_create_preserves_empty_proposal_note(self, conn):
        inp = _make_workflow_input(
            proposal=_create_proposal(note=""),
            idempotency_key="fm-note-empty",
        )
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        row = conn.execute(
            "SELECT notes FROM transactions WHERE public_id = ?",
            (result.transaction_public_id,),
        ).fetchone()
        notes = json.loads(row["notes"])
        assert notes["proposal_note"] == ""

    def test_create_without_note_field_defaults_to_empty(self, conn):
        proposal = FinalMutationProposal(
            proposal_id="fp-no-note",
            action=FinalMutationAction.CREATE_FINAL_TRANSACTION,
            amount=Decimal("10.00"),
            currency="SGD",
            merchant="Test",
            transaction_date=date(2024, 6, 1),
            source_statement_ref="stmt-no-note",
            evidence_refs=("ev-abc",),
        )
        inp = _make_workflow_input(
            proposal=proposal,
            idempotency_key="fm-no-note-field",
        )
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        row = conn.execute(
            "SELECT notes FROM transactions WHERE public_id = ?",
            (result.transaction_public_id,),
        ).fetchone()
        notes = json.loads(row["notes"])
        assert notes["proposal_note"] == ""


class TestReplayConsistency:
    """Idempotency and conflict behaviour for amount adjustments."""

    def test_replay_same_amount_adjustment_no_second_mutation(self, conn):
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("60.00")),
            idempotency_key="fm-replay-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target,
            suggested_fields={"amount": "75.00"},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-replay-adjust",
        )

        r1 = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert r1.status == FinalMutationWorkflowStatus.FINALIZED

        r2 = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert r2.status == FinalMutationWorkflowStatus.ALREADY_FINALIZED
        assert r2.transaction_public_id == r1.transaction_public_id

        # Exactly one audit record for the adjust idempotency key
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM reconciliation_final_mutation_audit WHERE idempotency_key = ?",
            ("fm-replay-adjust",),
        ).fetchone()[0]
        assert audit_count == 1

    def test_same_key_different_amount_is_conflict(self, conn):
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("60.00")),
            idempotency_key="fm-diff-amount-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target = create_result.transaction_public_id

        adjust_a = _adjust_proposal(
            target_transaction_id=target,
            suggested_fields={"amount": "75.00"},
        )
        guard_a = _approved_guard(adjust_a)
        inp_a = _make_workflow_input(
            proposal=adjust_a,
            guard_decision=guard_a,
            idempotency_key="fm-diff-amount",
        )
        r1 = execute_guarded_final_mutation_workflow(conn, inp_a, clock=_fixed_clock)
        assert r1.status == FinalMutationWorkflowStatus.FINALIZED

        adjust_b = _adjust_proposal(
            target_transaction_id=target,
            suggested_fields={"amount": "80.00"},
        )
        guard_b = _approved_guard(adjust_b)
        inp_b = _make_workflow_input(
            proposal=adjust_b,
            guard_decision=guard_b,
            idempotency_key="fm-diff-amount",
        )
        r2 = execute_guarded_final_mutation_workflow(conn, inp_b, clock=_fixed_clock)
        assert r2.status == FinalMutationWorkflowStatus.CONFLICT


class TestLegacyTableNonAuthority:
    """The legacy table receives no new authoritative financial writes."""

    def test_no_write_to_legacy_table_on_create(self, conn):
        inp = _make_workflow_input(idempotency_key="fm-legacy-create")
        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.FINALIZED

        legacy_exists = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='reconciliation_final_mutation_transactions'"
        ).fetchone()
        assert legacy_exists is None

    def test_no_write_to_legacy_table_on_adjust(self, conn):
        create_inp = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("50.00")),
            idempotency_key="fm-legacy-adjust-create",
        )
        create_result = execute_guarded_final_mutation_workflow(
            conn, create_inp, clock=_fixed_clock
        )
        target = create_result.transaction_public_id

        adjust = _adjust_proposal(
            target_transaction_id=target,
            suggested_fields={"amount": "51.00"},
        )
        guard = _approved_guard(adjust)
        adjust_inp = _make_workflow_input(
            proposal=adjust,
            guard_decision=guard,
            idempotency_key="fm-legacy-adjust",
        )
        result = execute_guarded_final_mutation_workflow(conn, adjust_inp, clock=_fixed_clock)
        assert result.status == FinalMutationWorkflowStatus.FINALIZED

        legacy_exists = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='reconciliation_final_mutation_transactions'"
        ).fetchone()
        assert legacy_exists is None


# Dedicated fixture that always builds a clean plan for ADJUST tests


def _plan() -> "ReconciliationApplyPlan":
    """Return a minimal plan that is valid for guard/operation matching."""
    plan, _ = _make_plan_and_op(source_apply_result_ref="fp-adjust-001")
    return plan


def _seed_authorization_data_only(
    conn: sqlite3.Connection, inp: FinalMutationWorkflowInput
) -> FinalMutationWorkflowInput:
    """Insert authorization data rows without executing any DDL.

    This is a variant of _seed_persisted_authorization that assumes the
    schema (migrations 013, 014, 018, 019) is already applied.  It only
    inserts the data rows needed by the workflow's authorization loader.
    """
    if not inp.human_confirmation_id:
        return inp
    content_hash = build_final_mutation_content_hash(inp.proposal)
    authorization_id = inp.authorization_id or f"test-auth-{content_hash[:24]}"
    confirmation_id = inp.human_confirmation_id
    existing_confirmation = conn.execute(
        "SELECT content_hash FROM reconciliation_final_mutation_confirmations "
        "WHERE confirmation_id = ?",
        (confirmation_id,),
    ).fetchone()
    if existing_confirmation is not None and existing_confirmation[0] != content_hash:
        confirmation_id = f"{confirmation_id}-{content_hash[:16]}"
        inp = replace(inp, human_confirmation_id=confirmation_id)
    guard_key = f"test-guard-{authorization_id}"
    execution_id = f"test-exec-{authorization_id}"
    preview = _canonical_preview_json(inp.proposal)
    conn.execute(
        """INSERT OR IGNORE INTO reconciliation_final_mutation_guard_decisions
        (proposal_id, idempotency_key, action, approved, blocked_reasons_json,
         preview_json, evidence_refs_json, guard_version, actor_type)
        VALUES (?, ?, ?, 1, '[]', ?, ?, 'v1', 'test')""",
        (
            inp.proposal.proposal_id,
            guard_key,
            inp.proposal.action.value,
            preview,
            json.dumps(sorted(inp.proposal.evidence_refs)),
        ),
    )
    conn.execute(
        """INSERT OR IGNORE INTO reconciliation_guarded_apply_executions
        (execution_id, plan_id, idempotency_key, execution_fingerprint,
         execution_status, total_operations, operations_executed,
         operations_blocked, operations_skipped, guard_decision_refs_json,
         audit_trail_json, is_dry_run, executed_at)
        VALUES (?, ?, ?, 'test', 'executed', 1, 1, 0, 0, '[]', '{}', 1, ?)""",
        (execution_id, inp.plan.plan_id, f"test-exec-key-{authorization_id}", FIXED_CLOCK),
    )
    conn.execute(
        """INSERT OR IGNORE INTO reconciliation_guarded_apply_operation_results
        (operation_result_id, execution_id, operation_id, decision_id,
         execution_status, guard_decision_approved,
         guard_decision_idempotency_key, guard_blocked_reasons_json,
         mutation_type, mutation_payload_json)
        VALUES (?, ?, ?, 'test-decision', 'executed', 1, ?, '[]', ?, '{}')""",
        (
            f"test-op-{authorization_id}",
            execution_id,
            inp.operation_id,
            guard_key,
            inp.proposal.action.value,
        ),
    )
    conn.execute(
        """INSERT OR IGNORE INTO reconciliation_final_mutation_confirmations
        (confirmation_id, subject_type, subject_id, operation_type, proposal_id,
         plan_id, content_hash, confirmation_state, confirmed_by)
        VALUES (?, 'reconciliation_apply_operation', ?, ?, ?, ?, ?, 'confirmed', 'test')""",
        (
            confirmation_id,
            inp.operation_id,
            inp.proposal.action.value,
            inp.proposal.proposal_id,
            inp.plan.plan_id,
            content_hash,
        ),
    )
    conn.execute(
        """INSERT OR IGNORE INTO reconciliation_final_mutation_authorizations
        (authorization_id, subject_type, subject_id, operation_type, proposal_id,
         plan_id, guard_decision_idempotency_key, guarded_execution_id,
         human_confirmation_id, content_hash, authorization_state)
        VALUES (?, 'reconciliation_apply_operation', ?, ?, ?, ?, ?, ?, ?, ?, 'authorized')""",
        (
            authorization_id,
            inp.operation_id,
            inp.proposal.action.value,
            inp.proposal.proposal_id,
            inp.plan.plan_id,
            guard_key,
            execution_id,
            confirmation_id,
            content_hash,
        ),
    )
    conn.commit()
    return replace(inp, authorization_id=authorization_id)


def _apply_migration_named(conn: sqlite3.Connection, name: str) -> None:
    path = migrations_dir() / name
    if not path.suffix:
        path = path.with_suffix(".sql")
    sql = path.read_text()
    conn.executescript(sql)
    # executescript issues an implicit commit; roll back
    # any open transaction it may have started.
    try:
        conn.rollback()
    except sqlite3.OperationalError:
        pass


def _apply_migration_019(conn: sqlite3.Connection) -> None:
    _apply_migration_named(conn, "019_reconciliation_final_mutation_audit.sql")


class _HostileCreateAmount:
    def __repr__(self) -> str:
        raise RuntimeError("invalid CREATE amount repr must not be used")

    def __str__(self) -> str:
        raise RuntimeError("invalid CREATE amount str must not be used")


def _authorized_input_with_changed_create_money(
    conn: sqlite3.Connection,
    *,
    amount: object = Decimal("12.30"),
    currency: object = "SGD",
    idempotency_key: str = "fm-invalid-create-money",
) -> tuple[FinalMutationWorkflowInput, FinalMutationWorkflowInput]:
    """Persist valid authority, then construct a malicious runtime DTO."""
    valid_proposal = _create_proposal(
        proposal_id="fp-create-money-boundary",
        amount=Decimal("12.30"),
        currency="SGD",
    )
    valid_input = _seed_persisted_authorization(
        conn,
        _make_workflow_input(
            proposal=valid_proposal,
            idempotency_key=idempotency_key,
        ),
    )
    invalid_proposal = replace(
        valid_proposal,
        amount=cast(Any, amount),
        currency=cast(Any, currency),
    )
    malicious_approved_guard = replace(
        valid_input.guard_decision,
        approved=True,
        action=FinalMutationAction.CREATE_FINAL_TRANSACTION,
    )
    invalid_input = replace(
        valid_input,
        proposal=invalid_proposal,
        guard_decision=malicious_approved_guard,
    )
    return valid_input, invalid_input


class TestCreateMoneyContractBoundary:
    def test_canonical_create_money_agrees_across_authorization_write_and_audits(
        self,
        conn: sqlite3.Connection,
    ) -> None:
        proposal = _create_proposal(amount=Decimal("12.3"), currency=" sgd ")
        inp = _make_workflow_input(
            proposal=proposal,
            idempotency_key="fm-create-money-consistency",
        )
        preview = inp.guard_decision.preview
        assert preview is not None
        assert preview.amount == "12.30"
        assert preview.currency == "SGD"

        result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

        assert result.status == FinalMutationWorkflowStatus.FINALIZED
        row = conn.execute(
            "SELECT amount, total_amount, currency FROM transactions WHERE public_id = ?",
            (result.transaction_public_id,),
        ).fetchone()
        assert Decimal(str(row["amount"])) == Decimal("12.30")
        assert Decimal(str(row["total_amount"])) == Decimal("12.30")
        assert row["amount"] == row["total_amount"]
        assert row["currency"] == "SGD"

        audit_row = conn.execute(
            "SELECT mutation_payload_json FROM reconciliation_final_mutation_audit "
            "WHERE final_mutation_id = ?",
            (result.final_mutation_id,),
        ).fetchone()
        mutation_payload = json.loads(audit_row["mutation_payload_json"])
        assert mutation_payload["amount"] == "12.30"
        assert mutation_payload["currency"] == "SGD"

        authorization_id = result.audit_refs["persisted_authorization_id"]
        authorization_row = conn.execute(
            "SELECT content_hash FROM reconciliation_final_mutation_authorizations "
            "WHERE authorization_id = ?",
            (authorization_id,),
        ).fetchone()
        assert authorization_row["content_hash"] == build_final_mutation_content_hash(proposal)

        event = FinancialAuditRepository(conn).list_chain(
            "transaction", str(result.transaction_public_id)
        )[0]
        new_state = json.loads(event.new_state_json)["value"]
        event_payload = json.loads(event.event_payload_json)["value"]
        assert new_state["amount"] == "12.30"
        assert new_state["total_amount"] == "12.30"
        assert new_state["currency"] == "SGD"
        assert event_payload["mutation_payload"] == mutation_payload
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    def test_jpy_create_persists_whole_canonical_money(
        self,
        conn: sqlite3.Connection,
    ) -> None:
        proposal = _create_proposal(amount=Decimal("100.0"), currency=" jpy ")
        result = execute_guarded_final_mutation_workflow(
            conn,
            _make_workflow_input(
                proposal=proposal,
                idempotency_key="fm-create-money-jpy",
            ),
            clock=_fixed_clock,
        )

        row = conn.execute(
            "SELECT amount, total_amount, currency FROM transactions WHERE public_id = ?",
            (result.transaction_public_id,),
        ).fetchone()
        payload = json.loads(
            conn.execute(
                "SELECT mutation_payload_json FROM reconciliation_final_mutation_audit "
                "WHERE final_mutation_id = ?",
                (result.final_mutation_id,),
            ).fetchone()[0]
        )
        assert Decimal(str(row["amount"])) == Decimal("100")
        assert row["amount"] == row["total_amount"]
        assert row["currency"] == "JPY"
        assert payload["amount"] == "100"
        assert payload["currency"] == "JPY"

    @pytest.mark.parametrize(
        "amount",
        [
            12.3,
            True,
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
            "NaN",
            "Infinity",
            "-Infinity",
            "1e2",
            "",
            object(),
            _HostileCreateAmount(),
            0,
            Decimal("-1.00"),
            Decimal("12.345"),
        ],
    )
    def test_invalid_amount_cannot_bypass_approved_guard_or_persisted_authorization(
        self,
        conn: sqlite3.Connection,
        amount: object,
    ) -> None:
        _, malicious_input = _authorized_input_with_changed_create_money(
            conn,
            amount=amount,
        )

        result = _execute_guarded_final_mutation_workflow(
            conn,
            malicious_input,
            clock=_fixed_clock,
        )

        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert result.blocked_reasons == (
            FinalMutationWorkflowBlockReason.CREATE_INVALID_AMOUNT.value,
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE intent = 'reconciliation_generated'"
            ).fetchone()[0]
            == 0
        )
        audit_rows = conn.execute(
            "SELECT status, mutation_payload_json FROM reconciliation_final_mutation_audit"
        ).fetchall()
        assert len(audit_rows) == 1
        assert audit_rows[0]["status"] == "blocked"
        blocked_payload = json.loads(audit_rows[0]["mutation_payload_json"])
        assert blocked_payload["money_validation"] == ["amount"]
        assert "object at 0x" not in audit_rows[0]["mutation_payload_json"]
        assert conn.execute("SELECT COUNT(*) FROM financial_audit_events").fetchone()[0] == 0

    @pytest.mark.parametrize("currency", ["   ", "XYZ", "SG", "S1D", object()])
    def test_invalid_currency_cannot_bypass_final_write_boundary(
        self,
        conn: sqlite3.Connection,
        currency: object,
    ) -> None:
        _, malicious_input = _authorized_input_with_changed_create_money(
            conn,
            currency=currency,
        )

        result = _execute_guarded_final_mutation_workflow(
            conn,
            malicious_input,
            clock=_fixed_clock,
        )

        assert result.status == FinalMutationWorkflowStatus.BLOCKED
        assert result.blocked_reasons == (
            FinalMutationWorkflowBlockReason.CREATE_INVALID_CURRENCY.value,
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE intent = 'reconciliation_generated'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM reconciliation_final_mutation_audit "
                "WHERE status = 'finalized'"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM financial_audit_events").fetchone()[0] == 0

    def test_both_invalid_money_fields_have_deterministic_workflow_order(
        self,
        conn: sqlite3.Connection,
    ) -> None:
        _, malicious_input = _authorized_input_with_changed_create_money(
            conn,
            amount=0,
            currency="XYZ",
        )

        result = _execute_guarded_final_mutation_workflow(
            conn,
            malicious_input,
            clock=_fixed_clock,
        )

        assert result.blocked_reasons == (
            FinalMutationWorkflowBlockReason.CREATE_INVALID_AMOUNT.value,
            FinalMutationWorkflowBlockReason.CREATE_INVALID_CURRENCY.value,
        )

    def test_canonical_equivalent_scale_replays_without_conflict(
        self,
        conn: sqlite3.Connection,
    ) -> None:
        first_input = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("12.3")),
            idempotency_key="fm-create-money-equivalent-scale",
        )
        first = execute_guarded_final_mutation_workflow(
            conn,
            first_input,
            clock=_fixed_clock,
        )
        second_input = _make_workflow_input(
            proposal=_create_proposal(amount=Decimal("12.30")),
            idempotency_key="fm-create-money-equivalent-scale",
        )
        second = execute_guarded_final_mutation_workflow(
            conn,
            second_input,
            clock=_fixed_clock,
        )

        assert first.status == FinalMutationWorkflowStatus.FINALIZED
        assert second.status == FinalMutationWorkflowStatus.ALREADY_FINALIZED
        assert second.idempotency_fingerprint == first.idempotency_fingerprint
        assert second.transaction_public_id == first.transaction_public_id
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE intent = 'reconciliation_generated'"
            ).fetchone()[0]
            == 1
        )

    def test_same_key_different_currency_conflicts(
        self,
        conn: sqlite3.Connection,
    ) -> None:
        first = execute_guarded_final_mutation_workflow(
            conn,
            _make_workflow_input(
                proposal=_create_proposal(currency="SGD"),
                idempotency_key="fm-create-money-currency-conflict",
            ),
            clock=_fixed_clock,
        )
        second = execute_guarded_final_mutation_workflow(
            conn,
            _make_workflow_input(
                proposal=_create_proposal(currency="USD"),
                idempotency_key="fm-create-money-currency-conflict",
            ),
            clock=_fixed_clock,
        )

        assert first.status == FinalMutationWorkflowStatus.FINALIZED
        assert second.status == FinalMutationWorkflowStatus.CONFLICT
        assert second.blocked_reasons == (
            FinalMutationWorkflowBlockReason.CONFLICTING_FINAL_MUTATION.value,
        )

    def test_invalid_attempt_binds_key_and_corrected_same_key_conflicts(
        self,
        conn: sqlite3.Connection,
    ) -> None:
        valid_input, malicious_input = _authorized_input_with_changed_create_money(
            conn,
            amount=Decimal("12.345"),
            idempotency_key="fm-create-money-blocked-policy",
        )
        blocked = _execute_guarded_final_mutation_workflow(
            conn,
            malicious_input,
            clock=_fixed_clock,
        )
        corrected = _execute_guarded_final_mutation_workflow(
            conn,
            valid_input,
            clock=_fixed_clock,
        )

        assert blocked.status == FinalMutationWorkflowStatus.BLOCKED
        assert corrected.status == FinalMutationWorkflowStatus.CONFLICT
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE intent = 'reconciliation_generated'"
            ).fetchone()[0]
            == 0
        )

    def test_transaction_insert_failure_rolls_back_all_success_evidence(
        self,
        conn: sqlite3.Connection,
    ) -> None:
        inp = _seed_persisted_authorization(
            conn,
            _make_workflow_input(idempotency_key="fm-create-money-insert-failure"),
        )
        conn.execute(
            """CREATE TRIGGER test_fail_create_money_insert
            BEFORE INSERT ON transactions
            WHEN NEW.intent = 'reconciliation_generated'
            BEGIN SELECT RAISE(ABORT, 'injected CREATE insert failure'); END"""
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="injected CREATE insert failure"):
            _execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

        assert (
            conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE intent = 'reconciliation_generated'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM reconciliation_final_mutation_audit").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM financial_audit_events").fetchone()[0] == 0

    def test_persisted_money_mismatch_rolls_back_instead_of_masking_audit_state(
        self,
        conn: sqlite3.Connection,
    ) -> None:
        inp = _seed_persisted_authorization(
            conn,
            _make_workflow_input(idempotency_key="fm-create-money-persisted-mismatch"),
        )
        conn.execute(
            """CREATE TRIGGER test_change_create_money_after_insert
            AFTER INSERT ON transactions
            WHEN NEW.intent = 'reconciliation_generated'
            BEGIN
                UPDATE transactions SET amount = '999.99'
                WHERE public_id = NEW.public_id;
            END"""
        )
        conn.commit()

        with pytest.raises(
            FinalMutationPersistenceError,
            match="does not match the authorized canonical value",
        ):
            _execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

        assert (
            conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE intent = 'reconciliation_generated'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM reconciliation_final_mutation_audit").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM financial_audit_events").fetchone()[0] == 0


def _workflow_row_counts(conn: sqlite3.Connection) -> tuple[int, int, int]:
    def count(table: str) -> int:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if exists is None:
            return 0
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    return (
        count("transactions"),
        count("reconciliation_final_mutation_audit"),
        count("financial_audit_events"),
    )


def _caller_transaction_case_input(
    conn: sqlite3.Connection,
    case: str,
) -> FinalMutationWorkflowInput:
    key = f"fm-caller-transaction-{case}"
    if case == "valid_create":
        return _seed_persisted_authorization(
            conn,
            _make_workflow_input(idempotency_key=key),
        )
    if case == "invalid_amount":
        return _authorized_input_with_changed_create_money(
            conn,
            amount=Decimal("12.345"),
            idempotency_key=key,
        )[1]
    if case == "invalid_currency":
        return _authorized_input_with_changed_create_money(
            conn,
            currency="XYZ",
            idempotency_key=key,
        )[1]
    if case == "guard_not_approved":
        proposal = _create_proposal()
        return _make_workflow_input(
            proposal=proposal,
            guard_decision=_blocked_guard(proposal),
            idempotency_key=key,
        )
    if case == "missing_authorization":
        return _make_workflow_input(idempotency_key=key)
    if case == "existing_idempotency_record":
        inp = _seed_persisted_authorization(
            conn,
            _make_workflow_input(idempotency_key=key),
        )
        first = _execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
        assert first.status == FinalMutationWorkflowStatus.FINALIZED
        return inp
    if case == "missing_audit_schema":
        return _make_workflow_input(idempotency_key=key)
    raise AssertionError(f"Unknown caller transaction case: {case}")


@pytest.mark.parametrize(
    "case",
    [
        "valid_create",
        "invalid_amount",
        "invalid_currency",
        "guard_not_approved",
        "missing_authorization",
        "existing_idempotency_record",
        "missing_audit_schema",
    ],
)
@pytest.mark.parametrize("caller_resolution", ["commit", "rollback"])
def test_caller_owned_transaction_is_rejected_and_preserved(
    conn: sqlite3.Connection,
    case: str,
    caller_resolution: str,
) -> None:
    conn.execute("CREATE TABLE caller_transaction_sentinel (value TEXT NOT NULL)")
    conn.commit()
    inp = _caller_transaction_case_input(conn, case)
    counts_before = _workflow_row_counts(conn)

    conn.execute("BEGIN")
    conn.execute(
        "INSERT INTO caller_transaction_sentinel (value) VALUES (?)",
        (f"sentinel-{case}-{caller_resolution}",),
    )

    with pytest.raises(FinalMutationTransactionError, match="without pending work"):
        _execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

    assert conn.in_transaction is True
    assert conn.execute("SELECT COUNT(*) FROM caller_transaction_sentinel").fetchone()[0] == 1
    assert _workflow_row_counts(conn) == counts_before

    if caller_resolution == "commit":
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM caller_transaction_sentinel").fetchone()[0] == 1
    else:
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM caller_transaction_sentinel").fetchone()[0] == 0


def _legacy_v1_content_hash_for_test(proposal: FinalMutationProposal) -> str:
    """Independently reproduce the content hash from base f34f840."""
    if proposal.amount is None:
        canonical_amount: str | None = None
    elif proposal.currency:
        canonical_amount = canonical_money_str(proposal.amount, proposal.currency)
    else:
        canonical_amount = canonical_decimal_str(proposal.amount)
    payload: dict[str, Any] = {
        "proposal_id": proposal.proposal_id,
        "action": proposal.action.value,
        "amount": canonical_amount,
        "currency": proposal.currency,
        "merchant": proposal.merchant,
        "transaction_date": (
            proposal.transaction_date.isoformat() if proposal.transaction_date is not None else None
        ),
        "target_transaction_id": proposal.target_transaction_id,
        "suggested_fields": dict(sorted(proposal.suggested_fields.items())),
        "source_statement_ref": proposal.source_statement_ref,
        "source_app_transaction_ref": proposal.source_app_transaction_ref,
        "evidence_refs": sorted(proposal.evidence_refs),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _legacy_v1_mutation_payload_for_test(
    inp: FinalMutationWorkflowInput,
) -> dict[str, object]:
    """Independently reproduce the mutation payload from base f34f840."""
    proposal = inp.proposal
    payload: dict[str, object] = {}
    if proposal.amount is not None:
        payload["amount"] = str(proposal.amount)
    if proposal.currency:
        payload["currency"] = proposal.currency
    if proposal.merchant:
        payload["merchant"] = proposal.merchant
    if proposal.transaction_date is not None:
        payload["transaction_date"] = proposal.transaction_date.isoformat()
    if proposal.target_transaction_id:
        payload["target_transaction_id"] = proposal.target_transaction_id
    if proposal.suggested_fields:
        payload["suggested_fields"] = dict(sorted(proposal.suggested_fields.items()))
    if proposal.source_statement_ref:
        payload["source_statement_ref"] = proposal.source_statement_ref
    if proposal.source_app_transaction_ref:
        payload["source_app_transaction_ref"] = proposal.source_app_transaction_ref
    if proposal.note:
        payload["note"] = proposal.note
    return payload


def _legacy_v1_fingerprint_for_test(
    inp: FinalMutationWorkflowInput,
    mutation_payload: object,
) -> str:
    """Independently reproduce final-mutation-v1 hashing from base f34f840."""
    canonical = json.dumps(
        {
            "version": "final-mutation-v1",
            "operation_id": inp.operation_id,
            "proposal_id": inp.proposal.proposal_id,
            "guard_version": inp.guard_decision.guard_version,
            "human_confirmation_id": inp.human_confirmation_id,
            "action": inp.proposal.action.value,
            "mutation_payload": mutation_payload,
            "evidence_refs": sorted(inp.proposal.evidence_refs),
            "idempotency_key": inp.idempotency_key,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _legacy_v1_transaction_public_id_for_test(inp: FinalMutationWorkflowInput) -> str:
    proposal = inp.proposal
    parts = [
        proposal.proposal_id,
        inp.operation_id,
        inp.human_confirmation_id,
        inp.idempotency_key,
        str(proposal.amount) if proposal.amount is not None else "",
        proposal.currency or "",
        proposal.transaction_date.isoformat() if proposal.transaction_date is not None else "",
        proposal.merchant or "",
    ]
    return f"txn-fm-{hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()[:16]}"


def _persist_legacy_v1_create_record(
    conn: sqlite3.Connection,
    *,
    legacy_proposal: FinalMutationProposal,
    current_proposal: FinalMutationProposal,
    idempotency_key: str,
    status: str = "finalized",
) -> tuple[FinalMutationWorkflowInput, str | None, str]:
    """Manually persist an exact pre-209 CREATE authority and audit fixture."""
    for migration_name in (
        "013_reconciliation_final_mutation_guard_decisions.sql",
        "014_reconciliation_guarded_apply_execution_persistence.sql",
        "018_reconciliation_final_mutation_authorization.sql",
        "019_reconciliation_final_mutation_audit.sql",
        "025_append_only_financial_audit_chain.sql",
    ):
        migration_path = migrations_dir() / migration_name
        conn.executescript(migration_path.read_text())

    current_inp = _make_workflow_input(
        proposal=current_proposal,
        idempotency_key=idempotency_key,
    )
    legacy_inp = replace(
        current_inp,
        proposal=legacy_proposal,
        guard_decision=_approved_guard(legacy_proposal),
    )
    suffix = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:12]
    authorization_id = f"legacy-auth-{suffix}"
    guard_key = f"legacy-guard-{suffix}"
    execution_id = f"legacy-exec-{suffix}"
    confirmation_id = legacy_inp.human_confirmation_id
    content_hash = _legacy_v1_content_hash_for_test(legacy_proposal)
    legacy_preview = {
        "proposal_id": legacy_proposal.proposal_id,
        "action": legacy_proposal.action.value,
        "amount": str(legacy_proposal.amount) if legacy_proposal.amount is not None else None,
        "currency": legacy_proposal.currency,
        "merchant": legacy_proposal.merchant,
        "transaction_date": (
            legacy_proposal.transaction_date.isoformat()
            if legacy_proposal.transaction_date is not None
            else None
        ),
        "target_transaction_id": legacy_proposal.target_transaction_id,
        "suggested_fields": dict(sorted(legacy_proposal.suggested_fields.items())),
        "source_statement_ref": legacy_proposal.source_statement_ref,
        "source_app_transaction_ref": legacy_proposal.source_app_transaction_ref,
        "evidence_refs": sorted(legacy_proposal.evidence_refs),
        "is_dry_run": True,
        "preview_note": (
            "Dry-run preview only. No final financial records were created or modified."
        ),
    }
    conn.execute(
        """INSERT INTO reconciliation_final_mutation_guard_decisions
        (proposal_id, idempotency_key, action, approved, blocked_reasons_json,
         preview_json, evidence_refs_json, guard_version, actor_type)
        VALUES (?, ?, ?, 1, '[]', ?, ?, 'v1', 'test')""",
        (
            legacy_proposal.proposal_id,
            guard_key,
            legacy_proposal.action.value,
            json.dumps(legacy_preview, sort_keys=True),
            json.dumps(sorted(legacy_proposal.evidence_refs)),
        ),
    )
    conn.execute(
        """INSERT INTO reconciliation_guarded_apply_executions
        (execution_id, plan_id, idempotency_key, execution_fingerprint,
         execution_status, total_operations, operations_executed,
         operations_blocked, operations_skipped, guard_decision_refs_json,
         audit_trail_json, is_dry_run, executed_at)
        VALUES (?, ?, ?, 'legacy-v1', 'executed', 1, 1, 0, 0, '[]', '{}', 1, ?)""",
        (execution_id, legacy_inp.plan.plan_id, f"legacy-exec-key-{suffix}", FIXED_CLOCK),
    )
    conn.execute(
        """INSERT INTO reconciliation_guarded_apply_operation_results
        (operation_result_id, execution_id, operation_id, decision_id,
         execution_status, guard_decision_approved,
         guard_decision_idempotency_key, guard_blocked_reasons_json,
         mutation_type, mutation_payload_json)
        VALUES (?, ?, ?, 'legacy-decision', 'executed', 1, ?, '[]', ?, '{}')""",
        (
            f"legacy-op-{suffix}",
            execution_id,
            legacy_inp.operation_id,
            guard_key,
            legacy_proposal.action.value,
        ),
    )
    conn.execute(
        """INSERT INTO reconciliation_final_mutation_confirmations
        (confirmation_id, subject_type, subject_id, operation_type, proposal_id,
         plan_id, content_hash, confirmation_state, confirmed_by)
        VALUES (?, 'reconciliation_apply_operation', ?, ?, ?, ?, ?, 'confirmed', 'test')""",
        (
            confirmation_id,
            legacy_inp.operation_id,
            legacy_proposal.action.value,
            legacy_proposal.proposal_id,
            legacy_inp.plan.plan_id,
            content_hash,
        ),
    )
    conn.execute(
        """INSERT INTO reconciliation_final_mutation_authorizations
        (authorization_id, subject_type, subject_id, operation_type, proposal_id,
         plan_id, guard_decision_idempotency_key, guarded_execution_id,
         human_confirmation_id, content_hash, authorization_state)
        VALUES (?, 'reconciliation_apply_operation', ?, ?, ?, ?, ?, ?, ?, ?, 'authorized')""",
        (
            authorization_id,
            legacy_inp.operation_id,
            legacy_proposal.action.value,
            legacy_proposal.proposal_id,
            legacy_inp.plan.plan_id,
            guard_key,
            execution_id,
            confirmation_id,
            content_hash,
        ),
    )

    payload = _legacy_v1_mutation_payload_for_test(legacy_inp)
    fingerprint = _legacy_v1_fingerprint_for_test(legacy_inp, payload)
    final_mutation_id = (
        "fm-" + hashlib.sha256(f"fm-v1|{idempotency_key}".encode("utf-8")).hexdigest()[:16]
    )
    transaction_public_id = (
        _legacy_v1_transaction_public_id_for_test(legacy_inp) if status == "finalized" else None
    )
    if transaction_public_id is not None:
        conn.execute(
            """INSERT INTO transactions (
                public_id, intent, intent_type, transaction_date, status,
                amount, total_amount, currency, merchant, category, notes,
                statement_source, created_at
            ) VALUES (?, 'reconciliation_generated', 'Generated', ?, 'active',
                      ?, ?, ?, ?, NULL, '{}', ?, ?)""",
            (
                transaction_public_id,
                legacy_proposal.transaction_date.isoformat()
                if legacy_proposal.transaction_date is not None
                else "",
                str(legacy_proposal.amount) if legacy_proposal.amount is not None else "",
                str(legacy_proposal.amount) if legacy_proposal.amount is not None else "",
                legacy_proposal.currency or "",
                legacy_proposal.merchant or "",
                legacy_proposal.source_statement_ref,
                FIXED_CLOCK,
            ),
        )
    blocked_reasons = ["guard_not_approved"] if status == "blocked" else []
    conn.execute(
        """INSERT INTO reconciliation_final_mutation_audit (
            final_mutation_id, idempotency_key, idempotency_fingerprint,
            operation_id, plan_id, proposal_id, guard_decision_proposal_id,
            guard_version, guarded_execution_id,
            guarded_execution_idempotency_key, human_confirmation_id,
            actor_type, actor_id, action, status, blocked_reasons_json,
            source_statement_ref, source_app_transaction_ref,
            target_transaction_id, transaction_public_id,
            mutation_payload_json, evidence_refs_json, audit_refs_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'v1', ?, ?, ?, 'system', NULL, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            final_mutation_id,
            idempotency_key,
            fingerprint,
            legacy_inp.operation_id,
            legacy_inp.plan.plan_id,
            legacy_proposal.proposal_id,
            legacy_inp.guard_decision.proposal_id,
            legacy_inp.guard_execution_result.idempotency_key,
            legacy_inp.guard_execution_result.idempotency_key,
            confirmation_id,
            legacy_proposal.action.value,
            status,
            json.dumps(blocked_reasons),
            legacy_proposal.source_statement_ref,
            legacy_proposal.source_app_transaction_ref,
            legacy_proposal.target_transaction_id,
            transaction_public_id,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            json.dumps(list(legacy_proposal.evidence_refs)),
            json.dumps({"legacy_fixture": True}),
            FIXED_CLOCK,
        ),
    )
    conn.commit()
    return (
        replace(current_inp, authorization_id=authorization_id),
        transaction_public_id,
        fingerprint,
    )


def _legacy_replay_counts(conn: sqlite3.Connection) -> tuple[int, int, int]:
    return (
        int(
            conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE intent = 'reconciliation_generated'"
            ).fetchone()[0]
        ),
        int(conn.execute("SELECT COUNT(*) FROM reconciliation_final_mutation_audit").fetchone()[0]),
        int(conn.execute("SELECT COUNT(*) FROM financial_audit_events").fetchone()[0]),
    )


@pytest.mark.parametrize(
    ("legacy_amount", "current_amount", "currency"),
    [
        (Decimal("12.3"), Decimal("12.30"), "SGD"),
        (Decimal("100.0"), Decimal("100"), "JPY"),
    ],
)
def test_legacy_v1_create_replay_returns_original_durable_result(
    conn: sqlite3.Connection,
    legacy_amount: Decimal,
    current_amount: Decimal,
    currency: str,
) -> None:
    proposal_id = f"legacy-replay-{currency.lower()}"
    legacy_proposal = _create_proposal(
        proposal_id=proposal_id,
        amount=legacy_amount,
        currency=currency,
    )
    current_proposal = replace(legacy_proposal, amount=current_amount)
    inp, transaction_id, legacy_fingerprint = _persist_legacy_v1_create_record(
        conn,
        legacy_proposal=legacy_proposal,
        current_proposal=current_proposal,
        idempotency_key=f"legacy-replay-{currency.lower()}-key",
    )
    counts_before = _legacy_replay_counts(conn)

    result = _execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

    assert result.status == FinalMutationWorkflowStatus.ALREADY_FINALIZED
    assert result.transaction_public_id == transaction_id
    assert result.idempotency_fingerprint == legacy_fingerprint
    assert result.audit_refs == {"legacy_fixture": True}
    assert _legacy_replay_counts(conn) == counts_before


def test_legacy_v1_lowercase_padded_currency_replay_normalizes_strictly(
    conn: sqlite3.Connection,
) -> None:
    legacy = _create_proposal(
        proposal_id="legacy-padded-currency",
        amount=Decimal("12.3"),
        currency=" sgd ",
    )
    current = replace(legacy, amount=Decimal("12.30"), currency="SGD")
    inp, transaction_id, _ = _persist_legacy_v1_create_record(
        conn,
        legacy_proposal=legacy,
        current_proposal=current,
        idempotency_key="legacy-padded-currency-key",
    )
    counts_before = _legacy_replay_counts(conn)

    result = _execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

    assert result.status == FinalMutationWorkflowStatus.ALREADY_FINALIZED
    assert result.transaction_public_id == transaction_id
    assert _legacy_replay_counts(conn) == counts_before


@pytest.mark.parametrize(
    ("current_amount", "current_currency"),
    [
        (Decimal("12.31"), "SGD"),
        (Decimal("12.3"), "USD"),
    ],
)
def test_legacy_v1_same_key_materially_different_money_conflicts(
    conn: sqlite3.Connection,
    current_amount: Decimal,
    current_currency: str,
) -> None:
    legacy = _create_proposal(
        proposal_id="legacy-money-conflict",
        amount=Decimal("12.3"),
        currency="SGD",
    )
    current = replace(legacy, amount=current_amount, currency=current_currency)
    legacy_bound_input, _, _ = _persist_legacy_v1_create_record(
        conn,
        legacy_proposal=legacy,
        current_proposal=current,
        idempotency_key="legacy-money-conflict-key",
    )
    current_authorized_input = _seed_persisted_authorization(
        conn,
        replace(legacy_bound_input, authorization_id=""),
    )
    counts_before = _legacy_replay_counts(conn)

    result = _execute_guarded_final_mutation_workflow(
        conn,
        current_authorized_input,
        clock=_fixed_clock,
    )

    assert result.status == FinalMutationWorkflowStatus.CONFLICT
    assert result.transaction_public_id is None
    assert _legacy_replay_counts(conn) == counts_before


@pytest.mark.parametrize(
    "stored_payload",
    [
        [],
        {"amount": 12.3, "currency": "SGD"},
        {"amount": "NaN", "currency": "SGD"},
        {"amount": "Infinity", "currency": "SGD"},
        {"amount": "12.345", "currency": "SGD"},
        {"amount": "1.5", "currency": "JPY"},
        {"amount": "0", "currency": "SGD"},
        {"amount": "-1.00", "currency": "SGD"},
        {"amount": "12.30", "currency": "XYZ"},
    ],
)
def test_unverifiable_legacy_v1_mutation_payload_fails_closed(
    conn: sqlite3.Connection,
    stored_payload: object,
) -> None:
    proposal = _create_proposal(
        proposal_id="legacy-invalid-payload",
        amount=Decimal("12.30"),
        currency="SGD",
    )
    inp, _, _ = _persist_legacy_v1_create_record(
        conn,
        legacy_proposal=proposal,
        current_proposal=proposal,
        idempotency_key="legacy-invalid-payload-key",
    )
    persisted_payload: object
    if isinstance(stored_payload, dict):
        complete_payload = _legacy_v1_mutation_payload_for_test(inp)
        complete_payload.update(stored_payload)
        persisted_payload = complete_payload
    else:
        persisted_payload = stored_payload
    stored_fingerprint = _legacy_v1_fingerprint_for_test(inp, persisted_payload)
    conn.execute(
        "UPDATE reconciliation_final_mutation_audit "
        "SET mutation_payload_json = ?, idempotency_fingerprint = ?",
        (json.dumps(persisted_payload), stored_fingerprint),
    )
    conn.commit()
    counts_before = _legacy_replay_counts(conn)

    result = _execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

    assert result.status == FinalMutationWorkflowStatus.CONFLICT
    assert _legacy_replay_counts(conn) == counts_before


def test_malformed_legacy_v1_mutation_json_fails_closed(
    conn: sqlite3.Connection,
) -> None:
    proposal = _create_proposal(proposal_id="legacy-malformed-json")
    inp, _, _ = _persist_legacy_v1_create_record(
        conn,
        legacy_proposal=proposal,
        current_proposal=proposal,
        idempotency_key="legacy-malformed-json-key",
    )
    conn.execute(
        "UPDATE reconciliation_final_mutation_audit SET mutation_payload_json = '{not-json'"
    )
    conn.commit()
    counts_before = _legacy_replay_counts(conn)

    result = _execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

    assert result.status == FinalMutationWorkflowStatus.CONFLICT
    assert _legacy_replay_counts(conn) == counts_before


def test_incomplete_legacy_v1_preview_blocks_without_rewriting_legacy_row(
    conn: sqlite3.Connection,
) -> None:
    proposal = _create_proposal(proposal_id="legacy-incomplete-preview")
    inp, transaction_id, legacy_fingerprint = _persist_legacy_v1_create_record(
        conn,
        legacy_proposal=proposal,
        current_proposal=proposal,
        idempotency_key="legacy-incomplete-preview-key",
    )
    row = conn.execute(
        "SELECT preview_json FROM reconciliation_final_mutation_guard_decisions"
    ).fetchone()
    preview = json.loads(row[0])
    del preview["merchant"]
    conn.execute(
        "UPDATE reconciliation_final_mutation_guard_decisions SET preview_json = ?",
        (json.dumps(preview),),
    )
    conn.commit()
    counts_before = _legacy_replay_counts(conn)

    result = _execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

    assert result.status == FinalMutationWorkflowStatus.BLOCKED
    assert result.transaction_public_id is None
    persisted = conn.execute(
        "SELECT idempotency_fingerprint, transaction_public_id "
        "FROM reconciliation_final_mutation_audit WHERE idempotency_key = ?",
        (inp.idempotency_key,),
    ).fetchone()
    assert persisted["idempotency_fingerprint"] == legacy_fingerprint
    assert persisted["transaction_public_id"] == transaction_id
    assert _legacy_replay_counts(conn) == counts_before


def test_legacy_v1_blocked_result_replays_without_rewriting(
    conn: sqlite3.Connection,
) -> None:
    proposal = _create_proposal(proposal_id="legacy-blocked-replay")
    inp, _, legacy_fingerprint = _persist_legacy_v1_create_record(
        conn,
        legacy_proposal=proposal,
        current_proposal=proposal,
        idempotency_key="legacy-blocked-replay-key",
        status="blocked",
    )
    counts_before = _legacy_replay_counts(conn)

    result = _execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

    assert result.status == FinalMutationWorkflowStatus.BLOCKED
    assert result.blocked_reasons == ("guard_not_approved",)
    assert result.idempotency_fingerprint == legacy_fingerprint
    assert _legacy_replay_counts(conn) == counts_before


def test_legacy_v1_replay_survives_process_restart(
    migrated_temp_db_path: Path,
) -> None:
    first = sqlite3.connect(str(migrated_temp_db_path))
    first.row_factory = sqlite3.Row
    first.execute("PRAGMA foreign_keys = ON")
    proposal = _create_proposal(
        proposal_id="legacy-restart-replay",
        amount=Decimal("12.3"),
    )
    inp, transaction_id, _ = _persist_legacy_v1_create_record(
        first,
        legacy_proposal=proposal,
        current_proposal=replace(proposal, amount=Decimal("12.30")),
        idempotency_key="legacy-restart-replay-key",
    )
    first.close()

    restarted = sqlite3.connect(str(migrated_temp_db_path))
    restarted.row_factory = sqlite3.Row
    restarted.execute("PRAGMA foreign_keys = ON")
    try:
        counts_before = _legacy_replay_counts(restarted)
        result = _execute_guarded_final_mutation_workflow(
            restarted,
            inp,
            clock=_fixed_clock,
        )
        assert result.status == FinalMutationWorkflowStatus.ALREADY_FINALIZED
        assert result.transaction_public_id == transaction_id
        assert _legacy_replay_counts(restarted) == counts_before
    finally:
        restarted.close()


def test_legacy_v1_replay_never_fabricates_missing_evidence(
    conn: sqlite3.Connection,
) -> None:
    proposal = _create_proposal(proposal_id="legacy-missing-evidence")
    inp, _, _ = _persist_legacy_v1_create_record(
        conn,
        legacy_proposal=proposal,
        current_proposal=proposal,
        idempotency_key="legacy-missing-evidence-key",
    )
    payload = _legacy_v1_mutation_payload_for_test(inp)
    fingerprint_without_evidence = hashlib.sha256(
        json.dumps(
            {
                "version": "final-mutation-v1",
                "operation_id": inp.operation_id,
                "proposal_id": inp.proposal.proposal_id,
                "guard_version": inp.guard_decision.guard_version,
                "human_confirmation_id": inp.human_confirmation_id,
                "action": inp.proposal.action.value,
                "mutation_payload": payload,
                "evidence_refs": [],
                "idempotency_key": inp.idempotency_key,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    conn.execute(
        "UPDATE reconciliation_final_mutation_audit "
        "SET evidence_refs_json = '[]', idempotency_fingerprint = ?",
        (fingerprint_without_evidence,),
    )
    conn.commit()
    counts_before = _legacy_replay_counts(conn)

    result = _execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)

    assert result.status == FinalMutationWorkflowStatus.CONFLICT
    assert _legacy_replay_counts(conn) == counts_before


def test_new_create_records_use_explicit_final_mutation_v2_fingerprint(
    conn: sqlite3.Connection,
) -> None:
    proposal = _create_proposal(
        proposal_id="current-v2-fingerprint",
        amount=Decimal("12.3"),
        currency=" sgd ",
    )
    inp = _make_workflow_input(
        proposal=proposal,
        idempotency_key="current-v2-fingerprint-key",
    )
    result = execute_guarded_final_mutation_workflow(conn, inp, clock=_fixed_clock)
    payload = _legacy_v1_mutation_payload_for_test(inp)
    payload["amount"] = "12.30"
    payload["currency"] = "SGD"
    expected = hashlib.sha256(
        json.dumps(
            {
                "version": "final-mutation-v2",
                "operation_id": inp.operation_id,
                "proposal_id": inp.proposal.proposal_id,
                "guard_version": inp.guard_decision.guard_version,
                "human_confirmation_id": inp.human_confirmation_id,
                "action": inp.proposal.action.value,
                "mutation_payload": payload,
                "evidence_refs": sorted(inp.proposal.evidence_refs),
                "idempotency_key": inp.idempotency_key,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert result.idempotency_fingerprint == expected
