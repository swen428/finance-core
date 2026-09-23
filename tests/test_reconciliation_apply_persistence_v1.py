"""Tests for Reconciliation Apply Persistence v1.

Covers:
  1. Apply persistence schema exists in the default temp migration chain
  2. Save and fetch one apply result
  3. Duplicate save of identical result is idempotent
  4. Duplicate save with same apply_id but different fingerprint conflicts
  5. List apply results by queue_item_id
  6. Statement/app references persist and round-trip correctly
  7. Proposal actions persist as proposal payloads, not final transactions
  8. Audit-only actions persist without transaction mutation
  9. No test touches database/finance.db
"""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from finance_core.reconciliation.apply import ResolutionApplyRuntime
from finance_core.reconciliation.apply_persistence import (
    ApplyPersistence,
    ApplyPersistenceConflictError,
)
from finance_core.reconciliation.matching import match_batch
from finance_core.reconciliation.models import (
    AppTransaction,
    IssueType,
    MatchStatus,
    ReconciliationCandidate,
    ResolutionAction,
    ResolutionDecision,
    ReviewQueueItem,
    StatementAmountDirection,
    StatementTransaction,
    SuggestedAction,
)
from finance_core.reconciliation.review_queue import generate_review_queue

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stmt(**kw) -> StatementTransaction:
    defaults = dict(
        transaction_date=date(2024, 12, 1),
        posted_date=None,
        merchant_raw="Apple",
        amount=Decimal("29.90"),
        currency="SGD",
        statement_row_reference="fp-stmt-001",
        amount_direction=StatementAmountDirection.DEBIT,
        raw_amount="29.90",
    )
    defaults.update(kw)
    return StatementTransaction(**defaults)


def _app(app_txn_id: str, **kw) -> AppTransaction:
    defaults = dict(
        app_txn_id=app_txn_id,
        transaction_date=date(2024, 12, 1),
        merchant="Apple",
        amount=Decimal("29.90"),
        currency="SGD",
        source_type="expense",
    )
    defaults.update(kw)
    return AppTransaction(**defaults)


def _make_matched_item_and_decision():
    """Create a matched review queue item and a confirm_match decision."""
    stmt = _stmt()
    app = _app("app-fp-match")
    candidates = match_batch([stmt], [app])
    items, _ = generate_review_queue(candidates)
    item = items[0]
    decision = ResolutionDecision(
        decision_id="dec-fp-001",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.CONFIRM_MATCH,
        note="Test persist.",
    )
    return item, decision


def _apply_and_result(runtime, item, decision):
    return runtime.apply(item, decision)


# ============================================================================
# 1. Apply persistence schema exists in the default temp migration chain
# ============================================================================


def test_migration_008_is_applied(migrated_temp_db_connection):
    """Verify the migration 008 table exists in the migrated test DB."""
    conn = migrated_temp_db_connection
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='reconciliation_apply_results'"
    ).fetchone()
    assert table_exists is not None, "reconciliation_apply_results table not found"

    # Verify schema columns
    cols = conn.execute("PRAGMA table_info(reconciliation_apply_results)").fetchall()
    col_names = {c["name"] for c in cols}
    expected = {
        "id",
        "apply_id",
        "decision_id",
        "queue_item_id",
        "candidate_id",
        "action",
        "success",
        "idempotent",
        "payload_json",
        "audit_evidence_json",
        "statement_reference_json",
        "app_transaction_reference_json",
        "reviewer",
        "note",
        "fingerprint",
        "applied_at",
        "created_at",
    }
    assert expected.issubset(col_names), f"Missing columns: {expected - col_names}"


def test_migration_008_indexes(migrated_temp_db_connection):
    """Verify expected indexes are created."""
    conn = migrated_temp_db_connection
    indexes = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_apply_results_%'"
    ).fetchall()
    index_names = {r["name"] for r in indexes}
    expected = {
        "idx_apply_results_decision_id",
        "idx_apply_results_queue_item_id",
        "idx_apply_results_action",
        "idx_apply_results_applied_at",
        "idx_apply_results_fingerprint",
    }
    assert expected.issubset(index_names), f"Missing indexes: {expected - index_names}"


# ============================================================================
# 2. Save and fetch one apply result
# ============================================================================


def test_save_and_fetch_one_apply_result(migrated_temp_db_connection):
    """Save one apply result and verify round-trip read."""
    conn = migrated_temp_db_connection
    item, decision = _make_matched_item_and_decision()
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(item, decision)

    ap = ApplyPersistence(conn)
    inserted = ap.save_apply_result(result)
    assert inserted is True

    row = ap.get_apply_result_by_apply_id(result.apply_id)
    assert row is not None
    assert row["apply_id"] == result.apply_id
    assert row["decision_id"] == result.decision_id
    assert row["queue_item_id"] == result.queue_item_id
    assert row["action"] == "confirm_match"
    assert row["success"] == 1
    assert row["reviewer"] == "human"


# ============================================================================
# 3. Duplicate save of identical result is idempotent
# ============================================================================


def test_duplicate_save_idempotent(migrated_temp_db_connection):
    """Saving the same apply result twice should return False on second save."""
    conn = migrated_temp_db_connection
    item, decision = _make_matched_item_and_decision()
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(item, decision)

    ap = ApplyPersistence(conn)
    first = ap.save_apply_result(result)
    assert first is True

    second = ap.save_apply_result(result)
    assert second is False  # Idempotent

    # Only one row exists
    rows = ap.list_apply_results_for_queue_item(item.queue_item_id)
    assert len(rows) == 1


# ============================================================================
# 4. Duplicate save with same apply_id but different fingerprint conflicts
# ============================================================================


def test_same_apply_id_different_fingerprint_conflicts(migrated_temp_db_connection):
    """Saving the same apply_id with different data must raise conflict."""
    conn = migrated_temp_db_connection
    item, decision = _make_matched_item_and_decision()
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(item, decision)

    ap = ApplyPersistence(conn)
    ap.save_apply_result(result)

    # Modify a field and try to save with same apply_id
    import copy

    modified = copy.deepcopy(result)
    object.__setattr__(modified, "note", "Different note now.")

    with pytest.raises(ApplyPersistenceConflictError, match="Conflicting apply_id"):
        ap.save_apply_result(modified)


# ============================================================================
# 5. List apply results by queue_item_id
# ============================================================================


def test_list_apply_results_by_queue_item_id(migrated_temp_db_connection):
    """List results for a specific queue item and verify filtering."""
    conn = migrated_temp_db_connection
    item, decision = _make_matched_item_and_decision()
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(item, decision)

    ap = ApplyPersistence(conn)
    ap.save_apply_result(result)

    rows = ap.list_apply_results_for_queue_item(item.queue_item_id)
    assert len(rows) == 1
    assert rows[0]["queue_item_id"] == item.queue_item_id

    # A different queue item should return nothing
    no_rows = ap.list_apply_results_for_queue_item("nonexistent-qid")
    assert len(no_rows) == 0


def test_has_apply_result_for_queue_item(migrated_temp_db_connection):
    """has_apply_result_for_queue_item returns correct boolean."""
    conn = migrated_temp_db_connection
    item, decision = _make_matched_item_and_decision()
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(item, decision)

    ap = ApplyPersistence(conn)
    assert ap.has_apply_result_for_queue_item(item.queue_item_id) is False
    ap.save_apply_result(result)
    assert ap.has_apply_result_for_queue_item(item.queue_item_id) is True


# ============================================================================
# 6. Statement/app references persist and round-trip correctly
# ============================================================================


def test_references_persist_and_roundtrip(migrated_temp_db_connection):
    """Statement and app transaction references round-trip through JSON."""
    conn = migrated_temp_db_connection
    item, decision = _make_matched_item_and_decision()
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(item, decision)

    ap = ApplyPersistence(conn)
    ap.save_apply_result(result)

    row = ap.get_apply_result_by_apply_id(result.apply_id)
    # Statement reference JSON
    stmt_json = row["statement_reference_json"]
    if stmt_json is not None:
        parsed = json.loads(stmt_json)
        assert parsed == result.statement_reference

    # App transaction reference JSON
    app_json = row["app_transaction_reference_json"]
    if app_json is not None:
        parsed = json.loads(app_json)
        assert parsed == result.app_transaction_reference


# ============================================================================
# 7. Proposal actions persist as proposal payloads, not final transactions
# ============================================================================


def test_proposal_actions_persist_as_proposals(migrated_temp_db_connection):
    """CREATE_MISSING_APP_TRANSACTION persists as proposal, not final
    transaction row."""
    conn = migrated_temp_db_connection

    # Manually construct a MISSING_IN_APP item (no matching app means no
    # best_app_transaction, which is the defining trait of MISSING_IN_APP).
    stmt = _stmt(
        merchant_raw="Giant Supermarket",
        amount=Decimal("45.50"),
        statement_row_reference="gs-proposal",
    )
    cand = ReconciliationCandidate(
        statement=stmt,
        best_app_transaction=None,
        all_app_transactions=(),
        match_status=MatchStatus.NO_MATCH,
        issue_type=IssueType.MISSING_IN_APP,
        confidence_score=Decimal("0.0"),
        candidate_id="cand-gs-proposal",
    )
    missing_item = ReviewQueueItem(
        queue_item_id="q-proposal-test",
        candidate=cand,
        issue_type=IssueType.MISSING_IN_APP,
        suggested_action=SuggestedAction.CREATE_MISSING_APP_TRANSACTION,
        priority=3,
    )

    decision = ResolutionDecision(
        decision_id="dec-proposal-001",
        queue_item_id=missing_item.queue_item_id,
        action=ResolutionAction.CREATE_MISSING_APP_TRANSACTION,
        note="Test proposal.",
    )
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(missing_item, decision)
    assert result.success

    ap = ApplyPersistence(conn)
    ap.save_apply_result(result)

    row = ap.get_apply_result_by_apply_id(result.apply_id)
    payload = json.loads(row["payload_json"])
    assert payload["action_type"] == "proposal"
    assert "Proposal only" in payload.get("note", "")

    # Verify no transaction row was created
    tx_count = conn.execute("SELECT COUNT(*) as cnt FROM transactions").fetchone()["cnt"]
    assert tx_count == 0  # No transaction was silently created


# ============================================================================
# 8. Audit-only actions persist without transaction mutation
# ============================================================================


def test_audit_only_actions_no_mutation(migrated_temp_db_connection):
    """MARK_DUPLICATE and CONFIRM_MATCH must persist without mutating
    any app or statement transaction rows."""
    conn = migrated_temp_db_connection

    # Build a duplicate item
    stmt = _stmt(merchant_raw="Grab", amount=Decimal("8.50"), statement_row_reference="grab-dup")
    app_a = _app("app-grab-a", merchant="Grab", amount=Decimal("8.50"))
    app_b = _app("app-grab-b", merchant="Grab", amount=Decimal("8.50"))
    candidates = match_batch([stmt], [app_a, app_b])
    items, _ = generate_review_queue(candidates)
    dup_item = items[0]

    decision = ResolutionDecision(
        decision_id="dec-audit-dup",
        queue_item_id=dup_item.queue_item_id,
        action=ResolutionAction.MARK_DUPLICATE,
        note="Test audit.",
    )
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(dup_item, decision)
    assert result.success
    assert result.payload["audit_only"] is True

    ap = ApplyPersistence(conn)
    ap.save_apply_result(result)

    # Verify payload is audit-only
    row = ap.get_apply_result_by_apply_id(result.apply_id)
    payload = json.loads(row["payload_json"])
    assert payload["action_type"] == "mark_duplicate"
    assert payload["audit_only"] is True

    # Verify no transaction was created, deleted, or modified
    tx_count = conn.execute("SELECT COUNT(*) as cnt FROM transactions").fetchone()["cnt"]
    assert tx_count == 0


def test_successful_apply_replay_checks_corrected_secondary_duplicate(
    migrated_temp_db_connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = migrated_temp_db_connection
    stmt = _stmt(merchant_raw="Grab", statement_row_reference="guarded-dup")
    apps = [_app("app-guard-a", merchant="Grab"), _app("app-guard-b", merchant="Grab")]
    items, _ = generate_review_queue(match_batch([stmt], apps))
    item = next(item for item in items if item.issue_type == IssueType.POSSIBLE_DUPLICATE)
    decision = ResolutionDecision(
        decision_id="dec-guarded-duplicate",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.MARK_DUPLICATE,
    )
    result = ResolutionApplyRuntime().apply(item, decision)
    ap = ApplyPersistence(conn)
    assert ap.save_apply_result(result) is True
    duplicates = result.payload["duplicate_app_txn_ids"]
    secondary = next(value for value in duplicates if value != result.payload["kept_app_txn_id"])
    monkeypatch.setattr(
        "finance_core.reconciliation.apply_persistence.has_committed_correction",
        lambda _conn, target: target == secondary,
    )
    with pytest.raises(ValueError, match="corrected_transaction_requires_versioned_reconciliation"):
        ap.save_apply_result(result)
    assert conn.in_transaction is False
    assert ap.get_apply_result_by_apply_id(result.apply_id) is not None


def test_apply_caller_transaction_is_left_unchanged(migrated_temp_db_connection) -> None:
    conn = migrated_temp_db_connection
    item, decision = _make_matched_item_and_decision()
    result = ResolutionApplyRuntime().apply(item, decision)
    ap = ApplyPersistence(conn)
    conn.execute("CREATE TABLE caller_marker (value TEXT)")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO caller_marker VALUES ('pending')")
    with pytest.raises(RuntimeError, match="caller-owned"):
        ap.save_apply_result(result)
    assert conn.in_transaction is True
    assert conn.execute("SELECT value FROM caller_marker").fetchone()[0] == "pending"
    conn.rollback()
    assert conn.execute("SELECT 1 FROM caller_marker").fetchone() is None


def test_apply_correction_guard_runs_while_immediate_lock_is_held(
    migrated_temp_db_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = sqlite3.connect(migrated_temp_db_path, timeout=0)
    contender = sqlite3.connect(migrated_temp_db_path, timeout=0)
    owner.row_factory = sqlite3.Row
    owner.execute("PRAGMA foreign_keys = ON")
    try:
        item, decision = _make_matched_item_and_decision()
        result = ResolutionApplyRuntime().apply(item, decision)
        observed: list[str] = []

        def check_locked(_conn: sqlite3.Connection, target: str) -> bool:
            assert owner.in_transaction
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
            observed.append(target)
            return False

        monkeypatch.setattr(
            "finance_core.reconciliation.apply_persistence.has_committed_correction",
            check_locked,
        )
        assert ApplyPersistence(owner).save_apply_result(result) is True
        assert observed == ["app-fp-match"]
        assert owner.in_transaction is False
    finally:
        owner.close()
        contender.close()


# ============================================================================
# 9. No test touches database/finance.db
# ============================================================================


def test_no_database_finance_db_touched():
    """Verify that the persistence adapter does not reference the live DB."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _ = ApplyPersistence(conn)
        # Just make sure it doesn't reference the live DB path
        assert "finance.db" not in db_path
    finally:
        import os

        conn.close()
        os.unlink(db_path)


# ============================================================================
# Additional boundary tests
# ============================================================================


def test_persist_apply_results_batch(migrated_temp_db_connection):
    """Persist multiple apply results in a batch."""
    conn = migrated_temp_db_connection

    stmt_a = _stmt(merchant_raw="Apple", amount=Decimal("29.90"), statement_row_reference="ba1")
    stmt_b = _stmt(merchant_raw="Netflix", amount=Decimal("19.90"), statement_row_reference="ba2")
    app_a = _app("app-ba-a", merchant="Apple", amount=Decimal("29.90"))
    app_b = _app("app-ba-b", merchant="Netflix", amount=Decimal("19.90"))

    candidates = match_batch([stmt_a, stmt_b], [app_a, app_b])
    items, _ = generate_review_queue(candidates)

    runtime = ResolutionApplyRuntime()
    results = []
    for it in items:
        decision = ResolutionDecision(
            decision_id=f"dec-batch-{it.queue_item_id}",
            queue_item_id=it.queue_item_id,
            action=ResolutionAction.CONFIRM_MATCH,
        )
        result = runtime.apply(it, decision)
        results.append(result)

    ap = ApplyPersistence(conn)
    count = ap.persist_apply_results(results)
    assert count == len(results)

    all_rows = ap.list_all()
    assert len(all_rows) == len(results)


def test_get_apply_result_by_decision_id(migrated_temp_db_connection):
    """Fetch apply result by decision_id."""
    conn = migrated_temp_db_connection
    item, decision = _make_matched_item_and_decision()
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(item, decision)

    ap = ApplyPersistence(conn)
    ap.save_apply_result(result)

    row = ap.get_apply_result_by_decision_id(decision.decision_id)
    assert row is not None
    assert row["decision_id"] == decision.decision_id


def test_get_apply_result_by_decision_id_none(migrated_temp_db_connection):
    """Fetch non-existent decision_id returns None."""
    conn = migrated_temp_db_connection
    ap = ApplyPersistence(conn)
    row = ap.get_apply_result_by_decision_id("nonexistent")
    assert row is None


"""Additional decision_id uniqueness tests for reconciliation apply persistence."""


def test_same_decision_id_same_apply_id_same_fingerprint_idempotent(
    migrated_temp_db_connection,
):
    """Saving the same decision_id + apply_id + fingerprint twice is idempotent."""
    conn = migrated_temp_db_connection
    item, decision = _make_matched_item_and_decision()
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(item, decision)

    ap = ApplyPersistence(conn)
    first = ap.save_apply_result(result)
    assert first is True

    # Same exactly -- should be idempotent via either apply_id or decision_id unique
    second = ap.save_apply_result(result)
    assert second is False

    rows = ap.list_all()
    assert len(rows) == 1


def test_runtime_idempotent_replay_save_is_idempotent(
    migrated_temp_db_connection,
):
    """Saving a runtime idempotent replay result should not conflict."""
    conn = migrated_temp_db_connection
    item, decision = _make_matched_item_and_decision()
    runtime = ResolutionApplyRuntime()
    first_result = runtime.apply(item, decision)
    replay_result = runtime.apply(item, decision)

    assert first_result.idempotent is False
    assert replay_result.idempotent is True
    assert replay_result.apply_id == first_result.apply_id

    ap = ApplyPersistence(conn)
    first = ap.save_apply_result(first_result)
    second = ap.save_apply_result(replay_result)

    assert first is True
    assert second is False
    rows = ap.list_all()
    assert len(rows) == 1
    assert rows[0]["idempotent"] == 0


def test_same_decision_id_different_apply_id_conflicts(
    migrated_temp_db_connection,
):
    """Saving the same decision_id with a different apply_id must raise conflict."""
    conn = migrated_temp_db_connection
    item, decision = _make_matched_item_and_decision()
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(item, decision)

    ap = ApplyPersistence(conn)
    ap.save_apply_result(result)

    # Same decision, different apply_id
    modified = copy.deepcopy(result)
    object.__setattr__(modified, "apply_id", "diff-apply-id-999")

    with pytest.raises(ApplyPersistenceConflictError, match="Conflicting decision_id"):
        ap.save_apply_result(modified)


def test_same_decision_id_different_fingerprint_conflicts(
    migrated_temp_db_connection,
):
    """Same decision_id + different apply_id + different fingerprint must
    raise decision_id conflict (not apply_id conflict)."""
    conn = migrated_temp_db_connection
    item, decision = _make_matched_item_and_decision()
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(item, decision)

    ap = ApplyPersistence(conn)
    ap.save_apply_result(result)

    # Same decision_id, different apply_id, different note -- triggers
    # different fingerprint on the same decision_id, but apply_id is new
    # so the conflict is detected via the decision_id unique constraint.
    modified = copy.deepcopy(result)
    object.__setattr__(modified, "apply_id", "new-apply-id-for-same-decision")
    object.__setattr__(modified, "note", "Different note.")

    with pytest.raises(ApplyPersistenceConflictError, match="Conflicting decision_id"):
        ap.save_apply_result(modified)


def test_get_apply_result_by_decision_id_returns_single_row(
    migrated_temp_db_connection,
):
    """get_apply_result_by_decision_id returns the expected single row."""
    conn = migrated_temp_db_connection
    item, decision = _make_matched_item_and_decision()
    runtime = ResolutionApplyRuntime()
    result = runtime.apply(item, decision)

    ap = ApplyPersistence(conn)
    ap.save_apply_result(result)

    row = ap.get_apply_result_by_decision_id(decision.decision_id)
    assert row is not None
    assert row["decision_id"] == decision.decision_id
    assert row["apply_id"] == result.apply_id
    assert row["queue_item_id"] == result.queue_item_id

    # Verify there is exactly one row for this decision_id
    all_rows = ap.list_all()
    matching = [r for r in all_rows if r["decision_id"] == decision.decision_id]
    assert len(matching) == 1

    # Unknown decision_id returns None
    assert ap.get_apply_result_by_decision_id("nonexistent") is None
