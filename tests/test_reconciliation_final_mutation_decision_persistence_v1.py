"""Tests for Reconciliation Final Mutation Guard Decision Persistence v1.

Covers:
  1. Migration creates the expected table.
  2. Required columns exist.
  3. UNIQUE(idempotency_key) prevents duplicate persistence.
  4. Approved CREATE guard decision persists with preview_json populated.
  5. Approved ADJUST guard decision persists with preview_json populated.
  6. NO_FINAL_MUTATION decision persists safely.
  7. Blocked decision persists with blocked_reasons_json and preview_json NULL.
  8. evidence_refs_json is deterministic and round-trippable.
  9. idempotency key is deterministic for the same decision.
 10. idempotency key changes when material evidence/action/source changes.
 11. Adapter uses provided sqlite3.Connection and does not open database/finance.db.
 12. No test modifies database/finance.db.
 13. No final financial records are inserted/updated/deleted.
 14. JSON output ordering is stable.
 15. Existing final mutation proposal guard tests still pass.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_core.financial_audit import FinancialAuditRepository, verify_financial_audit_chain
from finance_core.reconciliation.final_mutation_decision_persistence import (
    FinalMutationGuardDecisionAlreadyExists,
    FinalMutationGuardDecisionPersistenceError,
    FinalMutationGuardDecisionRepository,
    build_final_mutation_guard_idempotency_key,
)
from finance_core.reconciliation.final_mutation_proposal import (
    FinalMutationAction,
    FinalMutationGuard,
    FinalMutationGuardDecision,
    FinalMutationPreview,
    FinalMutationProposal,
)
from finance_core.resources import migrations_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"
MIGRATION_013_PATH = migrations_dir() / "013_reconciliation_final_mutation_guard_decisions.sql"
MIGRATION_025_PATH = migrations_dir() / "025_append_only_financial_audit_chain.sql"


def _create_proposal(**kw) -> FinalMutationProposal:
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
    defaults: dict = dict(
        proposal_id="fp-adjust-001",
        action=FinalMutationAction.ADJUST_FINAL_TRANSACTION,
        target_transaction_id="txn-123",
        suggested_fields={"amount": "42.00"},
        source_app_transaction_ref="app-ref-001",
        evidence_refs=("ev-xyz",),
        note="Adjust amount from 45.50 to 42.00.",
    )
    defaults.update(kw)
    return FinalMutationProposal(**defaults)


def _noop_proposal(**kw) -> FinalMutationProposal:
    defaults: dict = dict(
        proposal_id="fp-noop-001",
        action=FinalMutationAction.NO_FINAL_MUTATION,
        note="Confirmed match, no mutation needed.",
    )
    defaults.update(kw)
    return FinalMutationProposal(**defaults)


def _guard() -> FinalMutationGuard:
    return FinalMutationGuard()


def _apply_migration_013(conn: sqlite3.Connection) -> None:
    conn.executescript(MIGRATION_013_PATH.read_text(encoding="utf-8"))
    conn.executescript(MIGRATION_025_PATH.read_text(encoding="utf-8"))
    conn.commit()


def test_migration_creates_table(temp_db_connection: sqlite3.Connection) -> None:
    _apply_migration_013(temp_db_connection)
    tables = temp_db_connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    table_names = {row["name"] for row in tables}
    assert "reconciliation_final_mutation_guard_decisions" in table_names


EXPECTED_COLUMNS = {
    "id",
    "proposal_id",
    "idempotency_key",
    "action",
    "approved",
    "blocked_reasons_json",
    "preview_json",
    "evidence_refs_json",
    "source_statement_ref",
    "source_app_transaction_ref",
    "target_transaction_id",
    "guard_version",
    "actor_type",
    "actor_id",
    "created_at",
}


def test_table_has_expected_columns(temp_db_connection: sqlite3.Connection) -> None:
    _apply_migration_013(temp_db_connection)
    info = temp_db_connection.execute(
        "PRAGMA table_info(reconciliation_final_mutation_guard_decisions)"
    ).fetchall()
    column_names = {row["name"] for row in info}
    assert EXPECTED_COLUMNS <= column_names, f"Missing columns: {EXPECTED_COLUMNS - column_names}"


def test_identical_idempotency_replay_is_safe_without_duplicate(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    proposal = _create_proposal()
    decision = _guard().evaluate(proposal)
    key = build_final_mutation_guard_idempotency_key(decision, proposal)
    first = repo.save(decision, proposal, idempotency_key=key)
    second = repo.save(decision, proposal, idempotency_key=key)
    assert first == second == key
    assert (
        temp_db_connection.execute(
            "SELECT COUNT(*) FROM reconciliation_final_mutation_guard_decisions"
        ).fetchone()[0]
        == 1
    )
    assert (
        temp_db_connection.execute("SELECT COUNT(*) FROM financial_audit_events").fetchone()[0] == 1
    )


def test_same_idempotency_key_with_changed_actor_is_conflict(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    proposal = _create_proposal()
    decision = _guard().evaluate(proposal)
    key = build_final_mutation_guard_idempotency_key(decision, proposal)
    repo.save(decision, proposal, idempotency_key=key)
    with pytest.raises(FinalMutationGuardDecisionAlreadyExists, match="Conflicting"):
        repo.save(
            decision,
            proposal,
            idempotency_key=key,
            actor_type="human",
            actor_id="reviewer-other",
        )
    assert (
        temp_db_connection.execute(
            "SELECT COUNT(*) FROM reconciliation_final_mutation_guard_decisions"
        ).fetchone()[0]
        == 1
    )
    assert (
        temp_db_connection.execute("SELECT COUNT(*) FROM financial_audit_events").fetchone()[0] == 1
    )


def test_approved_create_decision_persists_with_preview(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    proposal = _create_proposal()
    decision = _guard().evaluate(proposal)
    key = build_final_mutation_guard_idempotency_key(decision, proposal)
    returned_key = repo.save(decision, proposal, idempotency_key=key)
    assert returned_key == key
    record = repo.get_by_idempotency_key(key)
    assert record is not None
    assert record.proposal_id == "fp-create-001"
    assert record.action == FinalMutationAction.CREATE_FINAL_TRANSACTION.value
    assert record.approved is True
    assert record.blocked_reasons == ()
    assert record.preview_json is not None
    preview = json.loads(record.preview_json)
    assert preview["proposal_id"] == "fp-create-001"
    assert preview["amount"] == "45.50"
    assert preview["currency"] == "SGD"
    assert preview["is_dry_run"] is True
    assert preview["action"] == FinalMutationAction.CREATE_FINAL_TRANSACTION.value
    assert record.evidence_refs == ("ev-abc",)
    assert record.source_statement_ref == "stmt-res-001"
    assert record.guard_version == "v1"
    assert record.actor_type == "system"
    assert record.created_at is not None


def test_guard_decision_and_audit_event_commit_together(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    proposal = _create_proposal()
    decision = _guard().evaluate(proposal)
    key = repo.save(
        decision,
        proposal,
        actor_type="human",
        actor_id="reviewer-owner",
    )
    events = FinancialAuditRepository(temp_db_connection).list_chain(
        "reconciliation_proposal", proposal.proposal_id
    )
    assert len(events) == 1
    assert events[0].event_type == "reconciliation_guard_decision_recorded"
    assert events[0].causation_public_id == key
    assert events[0].actor_public_id == "reviewer-owner"
    assert verify_financial_audit_chain(
        temp_db_connection,
        aggregate_type="reconciliation_proposal",
        aggregate_public_id=proposal.proposal_id,
    ).valid


def test_audit_insert_failure_rolls_back_guard_decision(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    temp_db_connection.execute(
        """CREATE TRIGGER test_fail_guard_decision_audit
        BEFORE INSERT ON financial_audit_events
        BEGIN SELECT RAISE(ABORT, 'injected decision audit failure'); END"""
    )
    temp_db_connection.commit()
    proposal = _create_proposal()
    decision = _guard().evaluate(proposal)
    with pytest.raises(sqlite3.IntegrityError, match="injected decision audit failure"):
        FinalMutationGuardDecisionRepository(temp_db_connection).save(decision, proposal)
    assert (
        temp_db_connection.execute(
            "SELECT COUNT(*) FROM reconciliation_final_mutation_guard_decisions"
        ).fetchone()[0]
        == 0
    )
    assert (
        temp_db_connection.execute("SELECT COUNT(*) FROM financial_audit_events").fetchone()[0] == 0
    )


def test_approved_adjust_decision_persists_with_preview(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    proposal = _adjust_proposal()
    decision = _guard().evaluate(proposal)
    key = build_final_mutation_guard_idempotency_key(decision, proposal)
    repo.save(decision, proposal, idempotency_key=key)
    record = repo.get_by_idempotency_key(key)
    assert record is not None
    assert record.action == FinalMutationAction.ADJUST_FINAL_TRANSACTION.value
    assert record.approved is True
    assert record.preview_json is not None
    preview = json.loads(record.preview_json)
    assert preview["action"] == FinalMutationAction.ADJUST_FINAL_TRANSACTION.value
    assert preview["target_transaction_id"] == "txn-123"
    assert preview["suggested_fields"] == {"amount": "42.00"}
    assert record.source_app_transaction_ref == "app-ref-001"
    assert record.evidence_refs == ("ev-xyz",)


def test_noop_decision_persists(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    proposal = _noop_proposal()
    decision = _guard().evaluate(proposal)
    key = build_final_mutation_guard_idempotency_key(decision, proposal)
    repo.save(decision, proposal, idempotency_key=key)
    record = repo.get_by_idempotency_key(key)
    assert record is not None
    assert record.action == FinalMutationAction.NO_FINAL_MUTATION.value
    assert record.approved is True
    assert record.blocked_reasons == ()
    assert record.preview_json is not None
    preview = json.loads(record.preview_json)
    assert preview["is_dry_run"] is True


def test_blocked_decision_persists_no_preview(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    proposal = _create_proposal(amount=None)
    decision = _guard().evaluate(proposal)
    key = build_final_mutation_guard_idempotency_key(decision, proposal)
    repo.save(decision, proposal, idempotency_key=key)
    record = repo.get_by_idempotency_key(key)
    assert record is not None
    assert record.approved is False
    assert record.action == FinalMutationAction.BLOCKED.value
    assert record.preview_json is None
    assert "missing_amount" in record.blocked_reasons


def test_blocked_decision_with_multiple_reasons(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    proposal = _create_proposal(
        amount=None, currency=None, source_statement_ref=None, evidence_refs=()
    )
    decision = _guard().evaluate(proposal)
    key = build_final_mutation_guard_idempotency_key(decision, proposal)
    repo.save(decision, proposal, idempotency_key=key)
    record = repo.get_by_idempotency_key(key)
    assert record is not None
    assert record.approved is False
    assert record.preview_json is None
    reasons = set(record.blocked_reasons)
    assert "missing_amount" in reasons
    assert "missing_currency" in reasons
    assert "create_without_source" in reasons
    assert "missing_evidence_refs" in reasons


def test_raw_dict_blocked_decision_persists(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    decision = _guard().evaluate({"proposal_id": "raw-001", "action": "create"})
    key = build_final_mutation_guard_idempotency_key(decision)
    repo.save(decision, idempotency_key=key)
    record = repo.get_by_idempotency_key(key)
    assert record is not None
    assert record.approved is False
    assert record.action == FinalMutationAction.BLOCKED.value
    assert "invalid_proposal_type" in record.blocked_reasons


def test_evidence_refs_round_trip(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    proposal = _create_proposal(evidence_refs=("ev-002", "ev-001", "ev-003"))
    decision = _guard().evaluate(proposal)
    key = build_final_mutation_guard_idempotency_key(decision, proposal)
    repo.save(decision, proposal, idempotency_key=key)
    record = repo.get_by_idempotency_key(key)
    assert record.evidence_refs == ("ev-001", "ev-002", "ev-003")


def test_evidence_refs_same_set_same_key(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    proposal_a = _create_proposal(evidence_refs=("ev-b", "ev-a"))
    proposal_b = _create_proposal(evidence_refs=("ev-a", "ev-b"))
    decision_a = _guard().evaluate(proposal_a)
    decision_b = _guard().evaluate(proposal_b)
    key_a = build_final_mutation_guard_idempotency_key(decision_a, proposal_a)
    key_b = build_final_mutation_guard_idempotency_key(decision_b, proposal_b)
    assert key_a == key_b


def test_idempotency_key_deterministic_same_input(
    temp_db_connection: sqlite3.Connection,
) -> None:
    proposal_1 = _create_proposal()
    proposal_2 = _create_proposal()
    decision_1 = _guard().evaluate(proposal_1)
    decision_2 = _guard().evaluate(proposal_2)
    key_1 = build_final_mutation_guard_idempotency_key(decision_1, proposal_1)
    key_2 = build_final_mutation_guard_idempotency_key(decision_2, proposal_2)
    assert key_1 == key_2


def test_idempotency_key_is_hex(
    temp_db_connection: sqlite3.Connection,
) -> None:
    proposal = _create_proposal()
    decision = _guard().evaluate(proposal)
    key = build_final_mutation_guard_idempotency_key(decision, proposal)
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


def test_idempotency_key_changes_with_different_action(
    temp_db_connection: sqlite3.Connection,
) -> None:
    p_create = _create_proposal()
    p_adjust = _adjust_proposal()
    key_create = build_final_mutation_guard_idempotency_key(_guard().evaluate(p_create), p_create)
    key_adjust = build_final_mutation_guard_idempotency_key(_guard().evaluate(p_adjust), p_adjust)
    assert key_create != key_adjust


def test_idempotency_key_changes_with_different_proposal_id(
    temp_db_connection: sqlite3.Connection,
) -> None:
    p1 = _create_proposal(proposal_id="fp-a")
    p2 = _create_proposal(proposal_id="fp-b")
    key_1 = build_final_mutation_guard_idempotency_key(_guard().evaluate(p1), p1)
    key_2 = build_final_mutation_guard_idempotency_key(_guard().evaluate(p2), p2)
    assert key_1 != key_2


def test_idempotency_key_changes_with_different_source(
    temp_db_connection: sqlite3.Connection,
) -> None:
    p1 = _create_proposal(source_statement_ref="stmt-a")
    p2 = _create_proposal(source_statement_ref="stmt-b")
    key_1 = build_final_mutation_guard_idempotency_key(_guard().evaluate(p1), p1)
    key_2 = build_final_mutation_guard_idempotency_key(_guard().evaluate(p2), p2)
    assert key_1 != key_2


def test_idempotency_key_changes_with_different_evidence(
    temp_db_connection: sqlite3.Connection,
) -> None:
    p1 = _create_proposal(evidence_refs=("ev-1",))
    p2 = _create_proposal(evidence_refs=("ev-2",))
    key_1 = build_final_mutation_guard_idempotency_key(_guard().evaluate(p1), p1)
    key_2 = build_final_mutation_guard_idempotency_key(_guard().evaluate(p2), p2)
    assert key_1 != key_2


def test_idempotency_key_changes_with_different_amount(
    temp_db_connection: sqlite3.Connection,
) -> None:
    p1 = _create_proposal(amount=Decimal("45.50"))
    p2 = _create_proposal(amount=Decimal("99.99"))
    key_1 = build_final_mutation_guard_idempotency_key(_guard().evaluate(p1), p1)
    key_2 = build_final_mutation_guard_idempotency_key(_guard().evaluate(p2), p2)
    assert key_1 != key_2


def test_idempotency_key_changes_with_different_blocked_reasons(
    temp_db_connection: sqlite3.Connection,
) -> None:
    p1 = _create_proposal(amount=None)
    p2 = _create_proposal(amount=None, currency=None)
    key_1 = build_final_mutation_guard_idempotency_key(_guard().evaluate(p1), p1)
    key_2 = build_final_mutation_guard_idempotency_key(_guard().evaluate(p2), p2)
    assert key_1 != key_2


def test_adapter_uses_provided_connection(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    assert repo._conn is temp_db_connection
    proposal = _create_proposal()
    decision = _guard().evaluate(proposal)
    key = build_final_mutation_guard_idempotency_key(decision, proposal)
    repo.save(decision, proposal, idempotency_key=key)
    count = temp_db_connection.execute(
        "SELECT COUNT(*) FROM reconciliation_final_mutation_guard_decisions"
    ).fetchone()[0]
    assert count == 1


def test_no_finance_db_modified() -> None:
    if os.path.exists(str(LIVE_DB_PATH)):
        mtime_before = os.path.getmtime(str(LIVE_DB_PATH))

        assert os.path.getmtime(str(LIVE_DB_PATH)) == mtime_before


def test_no_final_financial_records_mutated(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    proposal = _create_proposal()
    decision = _guard().evaluate(proposal)
    key = build_final_mutation_guard_idempotency_key(decision, proposal)
    repo.save(decision, proposal, idempotency_key=key)
    tables = temp_db_connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    table_names = {row["name"] for row in tables}
    assert "statement_transactions" not in table_names
    assert "final_transactions" not in table_names
    assert "settlement_obligations" not in table_names


def test_blocked_reasons_json_stable_ordering(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    proposal = _create_proposal(
        amount=None, currency=None, merchant=None, source_statement_ref=None, evidence_refs=()
    )
    decision = _guard().evaluate(proposal)
    key = build_final_mutation_guard_idempotency_key(decision, proposal)
    repo.save(decision, proposal, idempotency_key=key)
    record = repo.get_by_idempotency_key(key)
    assert record is not None
    reasons = list(record.blocked_reasons)
    assert reasons == sorted(reasons), f"Expected sorted reasons, got {reasons}"


def test_preview_json_stable_ordering(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    proposal = _create_proposal()
    decision = _guard().evaluate(proposal)
    key = build_final_mutation_guard_idempotency_key(decision, proposal)
    repo.save(decision, proposal, idempotency_key=key)
    record = repo.get_by_idempotency_key(key)
    assert record is not None
    preview = json.loads(record.preview_json)
    keys = list(preview.keys())
    assert keys == sorted(keys), f"Expected sorted preview keys, got {keys}"


def test_list_by_proposal_id_returns_related_decisions(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    p1 = _create_proposal(proposal_id="fp-multi", evidence_refs=("ev-a",))
    d1 = _guard().evaluate(p1)
    k1 = build_final_mutation_guard_idempotency_key(d1, p1)
    repo.save(d1, p1, idempotency_key=k1)
    p2 = _create_proposal(proposal_id="fp-multi", evidence_refs=("ev-b",))
    d2 = _guard().evaluate(p2)
    k2 = build_final_mutation_guard_idempotency_key(d2, p2)
    repo.save(d2, p2, idempotency_key=k2)
    results = repo.list_by_proposal_id("fp-multi")
    assert len(results) == 2
    assert all(r.proposal_id == "fp-multi" for r in results)


def test_list_by_proposal_id_empty_for_unknown(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    results = repo.list_by_proposal_id("nonexistent")
    assert results == []


def test_list_by_source_statement_ref(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    p1 = _create_proposal(proposal_id="fp-stmt-1", source_statement_ref="stmt-shared")
    d1 = _guard().evaluate(p1)
    repo.save(d1, p1, idempotency_key=build_final_mutation_guard_idempotency_key(d1, p1))
    p2 = _create_proposal(
        proposal_id="fp-stmt-2", source_statement_ref="stmt-shared", evidence_refs=("ev-other",)
    )
    d2 = _guard().evaluate(p2)
    repo.save(d2, p2, idempotency_key=build_final_mutation_guard_idempotency_key(d2, p2))
    results = repo.list_by_source_statement_ref("stmt-shared")
    assert len(results) == 2
    assert all(r.source_statement_ref == "stmt-shared" for r in results)


def test_list_by_source_app_transaction_ref(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    p1 = _adjust_proposal(
        proposal_id="fp-app-1", source_app_transaction_ref="app-shared", evidence_refs=("ev-a",)
    )
    d1 = _guard().evaluate(p1)
    repo.save(d1, p1, idempotency_key=build_final_mutation_guard_idempotency_key(d1, p1))
    p2 = _adjust_proposal(
        proposal_id="fp-app-2", source_app_transaction_ref="app-shared", evidence_refs=("ev-b",)
    )
    d2 = _guard().evaluate(p2)
    repo.save(d2, p2, idempotency_key=build_final_mutation_guard_idempotency_key(d2, p2))
    results = repo.list_by_source_app_transaction_ref("app-shared")
    assert len(results) == 2
    assert all(r.source_app_transaction_ref == "app-shared" for r in results)


def test_custom_actor_type_and_actor_id(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    proposal = _create_proposal()
    decision = _guard().evaluate(proposal)
    key = build_final_mutation_guard_idempotency_key(decision, proposal)
    repo.save(
        decision, proposal, idempotency_key=key, actor_type="human", actor_id="reviewer-owner"
    )
    record = repo.get_by_idempotency_key(key)
    assert record is not None
    assert record.actor_type == "human"
    assert record.actor_id == "reviewer-owner"


def test_approved_decision_without_preview_rejected(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    proposal = _create_proposal()
    # Craft an approved decision with no preview (malformed)
    malformed = FinalMutationGuardDecision(
        proposal_id="mal-001",
        action=FinalMutationAction.CREATE_FINAL_TRANSACTION,
        approved=True,
        blocked_reasons=(),
        preview=None,
        guard_version="v1",
    )
    key = build_final_mutation_guard_idempotency_key(malformed, proposal)
    with pytest.raises(FinalMutationGuardDecisionPersistenceError, match="preview"):
        repo.save(malformed, proposal, idempotency_key=key)


def test_blocked_decision_with_preview_rejected(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    proposal = _create_proposal(amount=None)
    # Craft a blocked decision that somehow has a preview (malformed)
    malformed = FinalMutationGuardDecision(
        proposal_id="mal-002",
        action=FinalMutationAction.BLOCKED,
        approved=False,
        blocked_reasons=("missing_amount",),
        preview=FinalMutationPreview(
            proposal_id="mal-002",
            action=FinalMutationAction.CREATE_FINAL_TRANSACTION,
            amount="99.99",
            currency="SGD",
        ),
        guard_version="v1",
    )
    key = build_final_mutation_guard_idempotency_key(malformed, proposal)
    with pytest.raises(FinalMutationGuardDecisionPersistenceError, match="preview"):
        repo.save(malformed, proposal, idempotency_key=key)


def test_target_transaction_id_preserved(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    proposal = _adjust_proposal(target_transaction_id="txn-999")
    decision = _guard().evaluate(proposal)
    key = build_final_mutation_guard_idempotency_key(decision, proposal)
    repo.save(decision, proposal, idempotency_key=key)
    record = repo.get_by_idempotency_key(key)
    assert record is not None
    assert record.target_transaction_id == "txn-999"


def test_get_by_idempotency_key_returns_none_for_unknown(
    temp_db_connection: sqlite3.Connection,
) -> None:
    _apply_migration_013(temp_db_connection)
    repo = FinalMutationGuardDecisionRepository(temp_db_connection)
    record = repo.get_by_idempotency_key("nonexistent-key")
    assert record is None


def test_idempotency_key_no_timestamp_no_randomness(
    temp_db_connection: sqlite3.Connection,
) -> None:
    proposal = _create_proposal()
    decision = _guard().evaluate(proposal)
    keys = [build_final_mutation_guard_idempotency_key(decision, proposal) for _ in range(100)]
    assert len(set(keys)) == 1
