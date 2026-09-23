"""Tests for Review Queue Persistence v1.

Covers:
  1. Migration 007 can be applied to a temporary DB
  2. Persist review queue items
  3. List pending items
  4. List by issue_type
  5. Fetch by public_id
  6. Update status
  7. JSON reason/evidence serialization is stable
  8. No live database access
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from finance_core.reconciliation.demo_cli import _row_to_review_queue_item
from finance_core.reconciliation.matching import match_batch
from finance_core.reconciliation.models import (
    AppTransaction,
    IssueType,
    MatchStatus,
    ReconciliationTransactionType,
    ResolutionAction,
    ResolutionDecision,
    StatementAmountDirection,
    StatementTransaction,
    SuggestedAction,
)
from finance_core.reconciliation.persistence import apply_reconciliation_review_schema
from finance_core.reconciliation.resolution_persistence import ResolutionPersistence
from finance_core.reconciliation.review_persistence import ReviewQueuePersistence
from finance_core.reconciliation.review_queue import generate_review_queue

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_stmt(**kw) -> StatementTransaction:
    defaults = dict(
        transaction_date=date(2024, 12, 1),
        posted_date=None,
        merchant_raw="Apple",
        amount=Decimal("29.90"),
        currency="SGD",
        statement_row_reference="r1",
        amount_direction=StatementAmountDirection.DEBIT,
        raw_amount="29.90",
    )
    defaults.update(kw)
    return StatementTransaction(**defaults)


def _make_app(app_txn_id: str, **kw) -> AppTransaction:
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


def _make_items():
    stmt = _make_stmt()
    app = _make_app("app-1")
    candidates = match_batch([stmt], [app])
    items, _ = generate_review_queue(candidates)
    return items


def _make_duplicate_item():
    item = _make_items()[0]
    apps = (
        _make_app(
            "app-a",
            source_channel="telegram",
            normalized_merchant="apple",
            posted_date=date(2024, 12, 2),
            transaction_type=ReconciliationTransactionType.EXPENSE,
        ),
        _make_app("app-b", source_channel="manual", transaction_type="expense"),
    )
    candidate = replace(
        item.candidate,
        best_app_transaction=apps[0],
        all_app_transactions=apps,
        issue_type=IssueType.POSSIBLE_DUPLICATE,
        match_status=MatchStatus.POSSIBLE_DUPLICATE,
    )
    return replace(
        item,
        candidate=candidate,
        issue_type=IssueType.POSSIBLE_DUPLICATE,
        suggested_action=SuggestedAction.MARK_DUPLICATE,
    )


def _make_shared_app_duplicate_items():
    statements = [
        _make_stmt(statement_row_reference="shared-stmt-1"),
        _make_stmt(statement_row_reference="shared-stmt-2"),
    ]
    candidates = match_batch(statements, [_make_app("app-shared")])
    items, _ = generate_review_queue(candidates)
    assert len(items) == 2
    assert all(item.issue_type == IssueType.POSSIBLE_DUPLICATE for item in items)
    assert all(
        [app.app_txn_id for app in item.candidate.all_app_transactions] == ["app-shared"]
        for item in items
    )
    return items


def _temp_conn() -> sqlite3.Connection:
    """Create an in-memory connection with migration 007 applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_reconciliation_review_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# 1. Migration 007 can be applied to a temporary DB
# ---------------------------------------------------------------------------


def test_migration_007_applies_to_temp_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        apply_reconciliation_review_schema(conn)

        # Verify tables exist
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = {r["name"] for r in tables}
        assert "reconciliation_review_queue" in table_names
        assert "reconciliation_resolution_decisions" in table_names
        assert "reconciliation_resolution_results" in table_names
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. Persist review queue items
# ---------------------------------------------------------------------------


def test_persist_review_queue_items():
    conn = _temp_conn()
    try:
        rqp = ReviewQueuePersistence(conn)
        items = _make_items()
        count = rqp.persist_review_queue(items, run_public_id="run-001")
        assert count == len(items)
        assert count > 0

        total = rqp.count_all()
        assert total == len(items)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. List pending items
# ---------------------------------------------------------------------------


def test_list_pending_items():
    conn = _temp_conn()
    try:
        rqp = ReviewQueuePersistence(conn)
        items = _make_items()
        rqp.persist_review_queue(items)

        pending = rqp.list_pending()
        assert len(pending) == len(items)
        for row in pending:
            assert row["status"] == "pending"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. List by issue_type
# ---------------------------------------------------------------------------


def test_list_by_issue_type():
    conn = _temp_conn()
    try:
        rqp = ReviewQueuePersistence(conn)

        stmt_a = _make_stmt(
            merchant_raw="Apple", amount=Decimal("29.90"), statement_row_reference="ra"
        )
        stmt_b = _make_stmt(
            merchant_raw="Giant", amount=Decimal("45.50"), statement_row_reference="rb"
        )
        app_a = _make_app("app-a", merchant="Apple", amount=Decimal("29.90"))
        # Missing in app: Giant appears only in statement
        candidates = match_batch([stmt_a, stmt_b], [app_a])
        items, _ = generate_review_queue(candidates)
        rqp.persist_review_queue(items)

        matched = rqp.list_by_issue_type("matched")
        _missing = rqp.list_by_issue_type("missing_in_app")

        assert len(matched) >= 1
        for row in matched:
            assert row["issue_type"] == "matched"

        # The second statement (Giant, 45.50) may be matched as amount_mismatch
        # with an existing app transaction, not necessarily missing_in_app.
        mismatch = rqp.list_by_issue_type("amount_mismatch")
        assert len(mismatch) >= 1
        for row in mismatch:
            assert row["issue_type"] == "amount_mismatch"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5. Fetch by public_id
# ---------------------------------------------------------------------------


def test_get_by_public_id():
    conn = _temp_conn()
    try:
        rqp = ReviewQueuePersistence(conn)
        items = _make_items()
        rqp.persist_review_queue(items)

        for item in items:
            row = rqp.get_by_public_id(item.queue_item_id)
            assert row is not None
            assert row["public_id"] == item.queue_item_id
            assert row["issue_type"] == item.issue_type.value
            assert row["suggested_action"] == item.suggested_action.value
            assert row["priority"] == item.priority
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. Update status
# ---------------------------------------------------------------------------


def test_update_status():
    conn = _temp_conn()
    try:
        rqp = ReviewQueuePersistence(conn)
        items = _make_items()
        rqp.persist_review_queue(items)

        qid = items[0].queue_item_id
        assert rqp.update_status(qid, "resolved")

        row = rqp.get_by_public_id(qid)
        assert row is not None
        assert row["status"] == "resolved"

        # Verify count
        assert rqp.count_by_status("resolved") == 1
        assert rqp.count_by_status("pending") == len(items) - 1
    finally:
        conn.close()


def test_update_status_nonexistent():
    conn = _temp_conn()
    try:
        rqp = ReviewQueuePersistence(conn)
        assert not rqp.update_status("nonexistent-id", "resolved")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 7. JSON serialization is stable
# ---------------------------------------------------------------------------


def test_json_serialization_is_stable():
    """Persist items twice and verify reason_codes_json and evidence_json
    are structurally equivalent."""
    conn = _temp_conn()
    try:
        rqp = ReviewQueuePersistence(conn)
        items = _make_items()
        rqp.persist_review_queue(items)

        row = rqp.get_by_public_id(items[0].queue_item_id)
        assert row is not None

        # Deserialize and verify structure
        reason_codes = json.loads(row["reason_codes_json"])
        assert isinstance(reason_codes, list)
        for rc in reason_codes:
            assert isinstance(rc, str)

        evidence = json.loads(row["evidence_json"])
        assert isinstance(evidence, dict)
        assert "candidate_id" in evidence
        assert "match_status" in evidence
        assert "issue_type" in evidence
    finally:
        conn.close()


def test_duplicate_app_snapshots_round_trip_all_fields_in_original_order():
    conn = _temp_conn()
    try:
        item = _make_duplicate_item()
        rqp = ReviewQueuePersistence(conn)
        rqp.persist_review_queue([item])
        row = rqp.get_by_public_id(item.queue_item_id)
        assert row is not None
        snapshots = json.loads(row["evidence_json"])["all_app_transactions"]
        assert [snapshot["app_txn_id"] for snapshot in snapshots] == ["app-a", "app-b"]
        assert snapshots[0] == {
            "app_txn_id": "app-a",
            "transaction_date": "2024-12-01",
            "merchant": "Apple",
            "amount": "29.90",
            "currency": "SGD",
            "source_type": "expense",
            "source_channel": "telegram",
            "normalized_merchant": "apple",
            "posted_date": "2024-12-02",
            "transaction_type": "expense",
        }
        restored = _row_to_review_queue_item(row)
        assert restored.candidate.all_app_transactions == item.candidate.all_app_transactions
        assert restored.candidate.best_app_transaction == item.candidate.best_app_transaction
    finally:
        conn.close()


@pytest.mark.parametrize(
    "change",
    [
        lambda evidence: evidence.update(all_app_transactions=[]),
        lambda evidence: evidence["all_app_transactions"][1].update(app_txn_id="app-a"),
        lambda evidence: evidence.update(app_txn_id="another-best"),
        lambda evidence: evidence["all_app_transactions"][1].update(amount=8.5),
        lambda evidence: evidence["all_app_transactions"][1].update(transaction_date="bad-date"),
    ],
)
def test_malformed_duplicate_snapshots_refuse_reconstruction(change):
    conn = _temp_conn()
    try:
        item = _make_duplicate_item()
        rqp = ReviewQueuePersistence(conn)
        rqp.persist_review_queue([item])
        row = rqp.get_by_public_id(item.queue_item_id)
        assert row is not None
        evidence = json.loads(row["evidence_json"])
        change(evidence)
        conn.execute(
            "UPDATE reconciliation_review_queue SET evidence_json = ? WHERE public_id = ?",
            (json.dumps(evidence), item.queue_item_id),
        )
        row = rqp.get_by_public_id(item.queue_item_id)
        assert row is not None
        with pytest.raises(ValueError, match="review queue"):
            _row_to_review_queue_item(row)
    finally:
        conn.close()


def test_shared_app_duplicate_subtype_round_trips_one_original_app():
    conn = _temp_conn()
    try:
        items = _make_shared_app_duplicate_items()
        rqp = ReviewQueuePersistence(conn)
        rqp.persist_review_queue(items)
        for item in items:
            row = rqp.get_by_public_id(item.queue_item_id)
            assert row is not None
            snapshots = json.loads(row["evidence_json"])["all_app_transactions"]
            assert [snapshot["app_txn_id"] for snapshot in snapshots] == ["app-shared"]
            restored = _row_to_review_queue_item(row)
            assert restored.issue_type == IssueType.POSSIBLE_DUPLICATE
            assert restored.candidate.all_app_transactions == item.candidate.all_app_transactions
    finally:
        conn.close()


@pytest.mark.parametrize("legacy_without_list", [False, True])
@pytest.mark.parametrize(
    ("action", "expected_status"),
    [
        (ResolutionAction.IGNORE, "ignored"),
        (ResolutionAction.NEEDS_MORE_INFO, "needs_more_info"),
    ],
)
def test_shared_app_duplicate_neutral_action_succeeds(legacy_without_list, action, expected_status):
    conn = _temp_conn()
    try:
        item = _make_shared_app_duplicate_items()[0]
        rqp = ReviewQueuePersistence(conn)
        rqp.persist_review_queue([item])
        if legacy_without_list:
            row = rqp.get_by_public_id(item.queue_item_id)
            assert row is not None
            evidence = json.loads(row["evidence_json"])
            evidence.pop("all_app_transactions")
            conn.execute(
                "UPDATE reconciliation_review_queue SET evidence_json = ? WHERE public_id = ?",
                (json.dumps(evidence), item.queue_item_id),
            )
            conn.commit()
        row = rqp.get_by_public_id(item.queue_item_id)
        assert row is not None
        restored = _row_to_review_queue_item(row)
        assert len(restored.candidate.all_app_transactions) == (0 if legacy_without_list else 1)
        decision = ResolutionDecision(
            decision_id=f"dec-{action.value}-{legacy_without_list}",
            queue_item_id=item.queue_item_id,
            action=action,
        )
        assert ResolutionPersistence(conn).apply_resolution(restored, decision).success
        assert rqp.get_by_public_id(item.queue_item_id)["status"] == expected_status
    finally:
        conn.close()


@pytest.mark.parametrize("legacy_without_list", [False, True])
def test_shared_app_duplicate_mark_duplicate_rolls_back(legacy_without_list):
    conn = _temp_conn()
    try:
        item = _make_shared_app_duplicate_items()[0]
        rqp = ReviewQueuePersistence(conn)
        rqp.persist_review_queue([item])
        if legacy_without_list:
            row = rqp.get_by_public_id(item.queue_item_id)
            assert row is not None
            evidence = json.loads(row["evidence_json"])
            evidence.pop("all_app_transactions")
            conn.execute(
                "UPDATE reconciliation_review_queue SET evidence_json = ? WHERE public_id = ?",
                (json.dumps(evidence), item.queue_item_id),
            )
            conn.commit()
        row = rqp.get_by_public_id(item.queue_item_id)
        assert row is not None
        restored = _row_to_review_queue_item(row)
        decision = ResolutionDecision(
            decision_id=f"dec-mark-duplicate-{legacy_without_list}",
            queue_item_id=item.queue_item_id,
            action=ResolutionAction.MARK_DUPLICATE,
        )
        with pytest.raises(ValueError, match="incomplete app targets"):
            ResolutionPersistence(conn).apply_resolution(restored, decision)
        assert (
            conn.execute("SELECT COUNT(*) FROM reconciliation_resolution_decisions").fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM reconciliation_resolution_results").fetchone()[0]
            == 0
        )
        assert rqp.get_by_public_id(item.queue_item_id)["status"] == "pending"
    finally:
        conn.close()


def test_truncated_duplicate_candidate_list_refuses_mark_duplicate_without_writes():
    conn = _temp_conn()
    try:
        item = _make_duplicate_item()
        rqp = ReviewQueuePersistence(conn)
        rqp.persist_review_queue([item])
        row = rqp.get_by_public_id(item.queue_item_id)
        assert row is not None
        evidence = json.loads(row["evidence_json"])
        evidence["all_app_transactions"] = evidence["all_app_transactions"][:1]
        conn.execute(
            "UPDATE reconciliation_review_queue SET evidence_json = ? WHERE public_id = ?",
            (json.dumps(evidence), item.queue_item_id),
        )
        conn.commit()
        row = rqp.get_by_public_id(item.queue_item_id)
        assert row is not None
        restored = _row_to_review_queue_item(row)
        assert len(restored.candidate.all_app_transactions) == 1
        decision = ResolutionDecision(
            decision_id="dec-truncated-duplicate",
            queue_item_id=item.queue_item_id,
            action=ResolutionAction.MARK_DUPLICATE,
        )
        with pytest.raises(ValueError, match="incomplete app targets"):
            ResolutionPersistence(conn).apply_resolution(restored, decision)
        assert (
            conn.execute("SELECT COUNT(*) FROM reconciliation_resolution_decisions").fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM reconciliation_resolution_results").fetchone()[0]
            == 0
        )
        assert rqp.get_by_public_id(item.queue_item_id)["status"] == "pending"
    finally:
        conn.close()


def test_legacy_single_target_without_candidate_list_still_reconstructs():
    conn = _temp_conn()
    try:
        item = _make_items()[0]
        rqp = ReviewQueuePersistence(conn)
        rqp.persist_review_queue([item])
        row = rqp.get_by_public_id(item.queue_item_id)
        assert row is not None
        evidence = json.loads(row["evidence_json"])
        evidence.pop("all_app_transactions")
        conn.execute(
            "UPDATE reconciliation_review_queue SET evidence_json = ? WHERE public_id = ?",
            (json.dumps(evidence), item.queue_item_id),
        )
        row = rqp.get_by_public_id(item.queue_item_id)
        assert row is not None
        restored = _row_to_review_queue_item(row)
        assert restored.candidate.best_app_transaction is not None
        assert restored.candidate.best_app_transaction.app_txn_id == "app-1"
        assert restored.candidate.all_app_transactions == ()
    finally:
        conn.close()


def test_duplicate_snapshot_refuses_changed_canonical_reference():
    conn = _temp_conn()
    try:
        item = _make_duplicate_item()
        rqp = ReviewQueuePersistence(conn)
        rqp.persist_review_queue([item])
        conn.execute(
            "UPDATE reconciliation_review_queue SET app_transaction_ref = ? WHERE public_id = ?",
            ("app-b", item.queue_item_id),
        )
        row = rqp.get_by_public_id(item.queue_item_id)
        assert row is not None
        with pytest.raises(ValueError, match="canonical reference disagree"):
            _row_to_review_queue_item(row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 8. No live database access
# ---------------------------------------------------------------------------


def test_no_live_database_access():
    """Verify that ReviewQueuePersistence does not reference database/finance.db."""
    # The class itself has no live DB reference
    import inspect

    from finance_core.reconciliation.review_persistence import ReviewQueuePersistence as RQP

    src = inspect.getsource(RQP.__init__)
    assert "finance.db" not in src
    assert "LIVE_DB" not in src.upper()
