"""Tests for SQLite-backed reconciliation apply batch state adapter v1."""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_core.reconciliation.apply_state_persistence import SQLiteBatchApplyStateManager
from finance_core.reconciliation.matching import match_batch
from finance_core.reconciliation.models import (
    AppTransaction,
    BatchApplyState,
    ResolutionAction,
    ResolutionDecision,
    StatementTransaction,
)
from finance_core.reconciliation.review_queue import generate_review_queue


def _stmt(**kw) -> StatementTransaction:
    defaults: dict = dict(
        transaction_date=date(2024, 12, 1),
        posted_date=None,
        merchant_raw="Apple",
        amount=Decimal("29.90"),
        currency="SGD",
        statement_row_reference="stmt-batch",
    )
    defaults.update(kw)
    return StatementTransaction(**defaults)


def _app(app_txn_id: str, **kw) -> AppTransaction:
    defaults: dict = dict(
        app_txn_id=app_txn_id,
        transaction_date=date(2024, 12, 1),
        merchant="Apple",
        amount=Decimal("29.90"),
        currency="SGD",
    )
    defaults.update(kw)
    return AppTransaction(**defaults)


def _make_matched_items(n: int = 1, offset: int = 0):
    stmts = []
    apps = []
    for i in range(n):
        idx = offset + i
        stmts.append(
            _stmt(
                merchant_raw=f"Merchant{idx}",
                amount=Decimal(f"{10 + idx}.00"),
                statement_row_reference=f"stmt-{idx}",
                transaction_date=date(2024, 12, idx + 1),
            )
        )
        apps.append(
            _app(
                f"app-{idx}",
                merchant=f"Merchant{idx}",
                amount=Decimal(f"{10 + idx}.00"),
                transaction_date=date(2024, 12, idx + 1),
            )
        )
    candidates = match_batch(stmts, apps)
    items, _ = generate_review_queue(candidates)
    decisions = [
        ResolutionDecision(
            decision_id=f"dec-{i}",
            queue_item_id=items[i].queue_item_id,
            action=ResolutionAction.CONFIRM_MATCH,
            note="Looks correct.",
        )
        for i in range(len(items))
    ]
    return items, decisions


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_sqlite_apply_batch_state_success_persists_rows(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    mgr = SQLiteBatchApplyStateManager(migrated_temp_db_connection)
    items, decisions = _make_matched_items(n=2)

    result = mgr.apply_batch("batch-success", items, decisions)

    assert result.state == BatchApplyState.APPLIED
    assert result.applied_count == 2
    assert mgr.get_batch_state("batch-success") == BatchApplyState.APPLIED

    row = migrated_temp_db_connection.execute(
        "SELECT * FROM reconciliation_apply_batches WHERE batch_id = 'batch-success'"
    ).fetchone()
    assert row["current_state"] == "applied"
    assert row["applied_result_json"] is not None

    transitions = mgr.list_transitions("batch-success")
    assert [r["new_state"] for r in transitions] == ["applying", "applied"]


def test_sqlite_apply_batch_duplicate_after_restart_is_rejected(
    migrated_temp_db_path: Path,
) -> None:
    conn = _connect(migrated_temp_db_path)
    try:
        mgr = SQLiteBatchApplyStateManager(conn)
        items, decisions = _make_matched_items(n=1)
        first = mgr.apply_batch("batch-restart", items, decisions)
        assert first.state == BatchApplyState.APPLIED
    finally:
        conn.close()

    conn = _connect(migrated_temp_db_path)
    try:
        mgr = SQLiteBatchApplyStateManager(conn)
        items, decisions = _make_matched_items(n=1)
        second = mgr.apply_batch("batch-restart", items, decisions)

        assert second.state == BatchApplyState.REJECTED
        assert second.idempotent is True
        assert second.applied_count == 1
        assert mgr.get_batch_state("batch-restart") == BatchApplyState.APPLIED
        transitions = mgr.list_transitions("batch-restart")
        assert [r["new_state"] for r in transitions] == ["applying", "applied", "rejected"]
    finally:
        conn.close()


def test_sqlite_apply_batch_failed_can_retry(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    mgr = SQLiteBatchApplyStateManager(migrated_temp_db_connection)
    items, decisions = _make_matched_items(n=2)
    bad_decisions = list(decisions)
    bad_decisions[1] = ResolutionDecision(
        decision_id="dec-bad",
        queue_item_id=items[1].queue_item_id,
        action=ResolutionAction.MARK_DUPLICATE,
        note="Invalid for matched item.",
    )

    first = mgr.apply_batch("batch-retry", items, bad_decisions)
    assert first.state == BatchApplyState.FAILED
    assert mgr.get_batch_state("batch-retry") == BatchApplyState.FAILED

    second = mgr.apply_batch("batch-retry", items, decisions)
    assert second.state == BatchApplyState.APPLIED
    assert second.applied_count == 2
    assert mgr.get_batch_state("batch-retry") == BatchApplyState.APPLIED
    assert [r["new_state"] for r in mgr.list_transitions("batch-retry")] == [
        "applying",
        "failed",
        "applying",
        "applied",
    ]


def test_sqlite_apply_batch_applying_state_rejects_reentry(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    migrated_temp_db_connection.execute(
        """
        INSERT INTO reconciliation_apply_batches (batch_id, current_state)
        VALUES ('batch-applying', 'applying')
        """
    )
    migrated_temp_db_connection.commit()

    mgr = SQLiteBatchApplyStateManager(migrated_temp_db_connection)
    items, decisions = _make_matched_items(n=1)

    result = mgr.apply_batch("batch-applying", items, decisions)

    assert result.state == BatchApplyState.REJECTED
    assert result.error_message is not None
    assert "currently in APPLYING state" in result.error_message
    assert mgr.get_batch_state("batch-applying") == BatchApplyState.APPLYING
    assert [r["new_state"] for r in mgr.list_transitions("batch-applying")] == ["rejected"]


def test_sqlite_apply_batch_reset_clears_adapter_tables(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    mgr = SQLiteBatchApplyStateManager(migrated_temp_db_connection)
    items, decisions = _make_matched_items(n=1)
    mgr.apply_batch("batch-reset", items, decisions)

    mgr.reset()

    assert mgr.get_batch_state("batch-reset") is None
    batch_count = migrated_temp_db_connection.execute(
        "SELECT COUNT(*) AS cnt FROM reconciliation_apply_batches"
    ).fetchone()["cnt"]
    transition_count = migrated_temp_db_connection.execute(
        "SELECT COUNT(*) AS cnt FROM reconciliation_apply_batch_state_transitions"
    ).fetchone()["cnt"]
    assert batch_count == 0
    assert transition_count == 0


def test_sqlite_apply_batch_refuses_live_database_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import finance_core.reconciliation.apply_state_persistence as module

    db_path = tmp_path / "pretend-live.sqlite"
    conn = _connect(db_path)
    monkeypatch.setattr(module, "LIVE_DB_PATH", db_path)

    try:
        with pytest.raises(ValueError, match="Refusing to use live database"):
            SQLiteBatchApplyStateManager(conn)
    finally:
        conn.close()


def test_sqlite_apply_batch_unexpected_error_records_context(
    migrated_temp_db_connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import finance_core.reconciliation.apply_state_persistence as module

    mgr = SQLiteBatchApplyStateManager(migrated_temp_db_connection)
    items, decisions = _make_matched_items(n=1)

    def explode(*args, **kwargs):
        raise RuntimeError("unexpected sqlite apply crash")

    monkeypatch.setattr(module, "apply_decisions", explode)

    with pytest.raises(RuntimeError, match="unexpected sqlite apply crash"):
        mgr.apply_batch("batch-crash", items, decisions)

    assert mgr.get_batch_state("batch-crash") == BatchApplyState.FAILED
    transitions = mgr.list_transitions("batch-crash")
    assert [row["new_state"] for row in transitions] == ["applying", "failed"]
    failed_metadata_json = transitions[-1]["audit_metadata_json"]
    assert "RuntimeError" in failed_metadata_json
    assert "unexpected sqlite apply crash" in failed_metadata_json
