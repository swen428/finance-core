"""Tests for Reconciliation Review Queue Preview v1.

Covers the read-only preview builder
``summarize_reconciliation_review_queue_from_repository`` and the
``review_queue_cli`` preview surface.

Coverage:
  1. Empty repository returns an empty queue.
  2. A blocking / review-required reconciliation item appears in the queue.
  3. Resolved / ignored records are excluded; pending + needs_more_info surface.
  4. Queue ordering is stable and priority-aware (high -> medium -> low).
  5. evidence_count is calculated from reconciliation_structured_evidence.
  6. blocking_count is calculated from guarded apply operation results.
  7. CLI preview renders stable output.
  8. Read-only behaviour: builder + CLI do not change table row counts or DB
     state; database/finance.db is never touched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from finance_core.reconciliation.migrations import LIVE_DB_PATH
from finance_core.reconciliation.review_queue import (
    ReconciliationReviewQueueItem,
    summarize_reconciliation_review_queue_from_repository,
)
from finance_core.reconciliation.review_queue_cli import main as cli_main
from tests.conftest import connect_temp_db

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB = LIVE_DB_PATH

# Tables whose row counts are tracked for the read-only assertion.
_REVIEW_QUEUE_TABLE = "reconciliation_review_queue"
_EVIDENCE_TABLE = "reconciliation_structured_evidence"
_DECISIONS_TABLE = "reconciliation_resolution_decisions"
_OP_RESULTS_TABLE = "reconciliation_guarded_apply_operation_results"


# ---------------------------------------------------------------------------
# Seed helpers -- direct, deterministic SQL inserts into temp databases only.
# ---------------------------------------------------------------------------


def _seed_review_queue_row(
    conn: sqlite3.Connection,
    *,
    public_id: str,
    candidate_id: str = "cand-1",
    run_public_id: str = "run-1",
    issue_type: str = "amount_mismatch",
    suggested_action: str = "review_amount",
    priority: int = 1,
    status: str = "pending",
    reason_codes_json: str = "[]",
    evidence_json: str = "{}",
) -> None:
    conn.execute(
        """
        INSERT INTO reconciliation_review_queue (
            public_id, run_public_id, candidate_id, issue_type,
            suggested_action, priority, statement_transaction_ref,
            app_transaction_ref, confidence_score, reason_codes_json,
            evidence_json, status
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
        """,
        (
            public_id,
            run_public_id,
            candidate_id,
            issue_type,
            suggested_action,
            priority,
            "1.0",
            reason_codes_json,
            evidence_json,
            status,
        ),
    )
    conn.commit()


def _seed_evidence_row(
    conn: sqlite3.Connection,
    *,
    public_id: str,
    review_queue_public_id: str,
    evidence_type: str = "matching_decision",
    source_type: str = "matching_engine",
) -> None:
    conn.execute(
        """
        INSERT INTO reconciliation_structured_evidence (
            public_id, review_queue_public_id, statement_transaction_id,
            app_transaction_id, evidence_type, source_type, source_id,
            source_path, source_page, source_row, source_field,
            confidence_score, evidence_payload, created_at, updated_at
        ) VALUES (?, ?, NULL, NULL, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, ?, ?)
        """,
        (
            public_id,
            review_queue_public_id,
            evidence_type,
            source_type,
            "1.0",
            "{}",
            "2024-12-01T00:00:00Z",
            "2024-12-01T00:00:00Z",
        ),
    )
    conn.commit()


def _seed_decision(
    conn: sqlite3.Connection,
    *,
    decision_public_id: str,
    review_queue_public_id: str,
    decision_action: str = "confirm_match",
    reviewer: str = "tester",
) -> None:
    conn.execute(
        """
        INSERT INTO reconciliation_resolution_decisions (
            public_id, review_queue_public_id, decision_action,
            decision_note, reviewer, resolved_at
        ) VALUES (?, ?, ?, NULL, ?, NULL)
        """,
        (decision_public_id, review_queue_public_id, decision_action, reviewer),
    )
    conn.commit()


def _seed_operation_result(
    conn: sqlite3.Connection,
    *,
    operation_result_id: str,
    execution_id: str,
    operation_id: str,
    decision_id: str,
    execution_status: str,
    mutation_type: str = "noop",
) -> None:
    conn.execute(
        """
        INSERT INTO reconciliation_guarded_apply_operation_results (
            operation_result_id, execution_id, operation_id, decision_id,
            execution_status, reason, guard_decision_approved,
            guard_decision_idempotency_key, guard_blocked_reasons_json,
            mutation_type, mutation_payload_json
        ) VALUES (?, ?, ?, ?, ?, '', ?, NULL, '[]', ?, '{}')
        """,
        (
            operation_result_id,
            execution_id,
            operation_id,
            decision_id,
            execution_status,
            1 if execution_status == "executed" else 0,
            mutation_type,
        ),
    )
    conn.commit()


def _seed_execution(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    plan_id: str = "plan-1",
    idempotency_key: str = "idem-1",
) -> None:
    conn.execute(
        """
        INSERT INTO reconciliation_guarded_apply_executions (
            execution_id, plan_id, idempotency_key, execution_fingerprint,
            execution_status, total_operations, operations_executed,
            operations_blocked, operations_skipped, block_reason,
            guard_decision_refs_json, audit_trail_json, is_dry_run, executed_at
        ) VALUES (?, ?, ?, 'fp', 'executed', 1, 1, 0, 0, '', '[]', '[]', 1, '2024-12-01T00:00:00Z')
        """,
        (execution_id, plan_id, idempotency_key),
    )
    conn.commit()


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _snapshot_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        _REVIEW_QUEUE_TABLE: _row_count(conn, _REVIEW_QUEUE_TABLE),
        _EVIDENCE_TABLE: _row_count(conn, _EVIDENCE_TABLE),
        _DECISIONS_TABLE: _row_count(conn, _DECISIONS_TABLE),
        _OP_RESULTS_TABLE: _row_count(conn, _OP_RESULTS_TABLE),
    }


# ---------------------------------------------------------------------------
# 1. Empty repository returns an empty queue
# ---------------------------------------------------------------------------


def test_empty_repository_returns_empty_queue(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    items = summarize_reconciliation_review_queue_from_repository(migrated_temp_db_connection)
    assert items == []


def test_empty_database_returns_empty_queue(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.sqlite"
    assert db_path != LIVE_DB
    conn = connect_temp_db(db_path)
    try:
        # Schema present (tables exist) but no rows anywhere.
        from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS

        for migration in TEMP_DB_MIGRATION_PATHS:
            conn.executescript(migration.read_text(encoding="utf-8"))
        conn.commit()
        items = summarize_reconciliation_review_queue_from_repository(conn)
        assert items == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. A blocking / review-required item appears in the queue
# ---------------------------------------------------------------------------


def test_review_required_item_appears(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seed_review_queue_row(
        migrated_temp_db_connection,
        public_id="rq-001",
        issue_type="amount_mismatch",
        status="pending",
    )
    items = summarize_reconciliation_review_queue_from_repository(migrated_temp_db_connection)

    assert len(items) == 1
    item = items[0]
    assert isinstance(item, ReconciliationReviewQueueItem)
    assert item.review_id == "rq-001"
    assert item.source_type == "reconciliation_review_queue"
    assert item.source_id == "cand-1"
    assert item.status == "pending"
    assert item.priority == "high"  # amount_mismatch -> HIGH
    assert item.reason == "amount_mismatch"
    assert item.evidence_count == 0
    assert item.blocking_count == 0
    assert item.created_at is not None
    assert "amount_mismatch" in item.summary


def test_needs_more_info_item_appears(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seed_review_queue_row(
        migrated_temp_db_connection,
        public_id="rq-nmi",
        issue_type="date_mismatch",
        status="needs_more_info",
    )
    items = summarize_reconciliation_review_queue_from_repository(migrated_temp_db_connection)
    assert len(items) == 1
    assert items[0].status == "needs_more_info"
    assert items[0].priority == "medium"  # date_mismatch -> MEDIUM


# ---------------------------------------------------------------------------
# 3. Resolved / ignored records are excluded
# ---------------------------------------------------------------------------


def test_resolved_and_ignored_are_excluded(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seed_review_queue_row(migrated_temp_db_connection, public_id="rq-pending", status="pending")
    _seed_review_queue_row(migrated_temp_db_connection, public_id="rq-resolved", status="resolved")
    _seed_review_queue_row(migrated_temp_db_connection, public_id="rq-ignored", status="ignored")
    _seed_review_queue_row(
        migrated_temp_db_connection, public_id="rq-nmi", status="needs_more_info"
    )

    items = summarize_reconciliation_review_queue_from_repository(migrated_temp_db_connection)
    review_ids = [item.review_id for item in items]
    assert review_ids == ["rq-nmi", "rq-pending"]
    assert "rq-resolved" not in review_ids
    assert "rq-ignored" not in review_ids


# ---------------------------------------------------------------------------
# 4. Queue ordering is stable and priority-aware
# ---------------------------------------------------------------------------


def test_ordering_is_priority_aware_and_stable(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    # Insert in mixed priority + id order; expect high -> medium -> low,
    # then ascending review_id within a tier.
    _seed_review_queue_row(
        migrated_temp_db_connection,
        public_id="rq-low-a",
        issue_type="missing_in_statement",  # MEDIUM
    )
    _seed_review_queue_row(
        migrated_temp_db_connection,
        public_id="rq-high-b",
        issue_type="amount_mismatch",  # HIGH
    )
    _seed_review_queue_row(
        migrated_temp_db_connection,
        public_id="rq-high-a",
        issue_type="possible_duplicate",  # HIGH
    )
    _seed_review_queue_row(
        migrated_temp_db_connection,
        public_id="rq-med-a",
        issue_type="date_mismatch",  # MEDIUM
    )

    items = summarize_reconciliation_review_queue_from_repository(migrated_temp_db_connection)
    priorities = [item.priority for item in items]
    review_ids = [item.review_id for item in items]

    # Priority tiers grouped high -> medium.
    assert priorities == ["high", "high", "medium", "medium"]
    # Within-tier stable ascending review_id.
    assert review_ids == ["rq-high-a", "rq-high-b", "rq-low-a", "rq-med-a"]


def test_ordering_is_deterministic_across_calls(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seed_review_queue_row(
        migrated_temp_db_connection, public_id="rq-1", issue_type="amount_mismatch"
    )
    _seed_review_queue_row(
        migrated_temp_db_connection, public_id="rq-2", issue_type="date_mismatch"
    )

    first = summarize_reconciliation_review_queue_from_repository(migrated_temp_db_connection)
    second = summarize_reconciliation_review_queue_from_repository(migrated_temp_db_connection)
    assert first == second


# ---------------------------------------------------------------------------
# 5. evidence_count is calculated correctly
# ---------------------------------------------------------------------------


def test_evidence_count_is_calculated(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seed_review_queue_row(migrated_temp_db_connection, public_id="rq-ev")
    _seed_evidence_row(
        migrated_temp_db_connection,
        public_id="ev-1",
        review_queue_public_id="rq-ev",
    )
    _seed_evidence_row(
        migrated_temp_db_connection,
        public_id="ev-2",
        review_queue_public_id="rq-ev",
    )
    _seed_evidence_row(
        migrated_temp_db_connection,
        public_id="ev-3",
        review_queue_public_id="rq-other",  # belongs to a different item
    )

    items = summarize_reconciliation_review_queue_from_repository(migrated_temp_db_connection)
    by_id = {item.review_id: item for item in items}
    assert by_id["rq-ev"].evidence_count == 2
    assert "2 evidence" in by_id["rq-ev"].summary


def test_evidence_count_zero_when_none_linked(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seed_review_queue_row(migrated_temp_db_connection, public_id="rq-noev")
    items = summarize_reconciliation_review_queue_from_repository(migrated_temp_db_connection)
    assert items[0].evidence_count == 0
    assert "0 evidence" in items[0].summary


# ---------------------------------------------------------------------------
# 6. blocking_count is calculated correctly
# ---------------------------------------------------------------------------


def test_blocking_count_counts_blocked_operation_results(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seed_review_queue_row(migrated_temp_db_connection, public_id="rq-blk")
    _seed_execution(
        migrated_temp_db_connection,
        execution_id="exec-1",
        idempotency_key="idem-blk",
    )
    _seed_decision(
        migrated_temp_db_connection,
        decision_public_id="dec-1",
        review_queue_public_id="rq-blk",
    )
    # One blocked + one partially_blocked + one conflict -> 3 blocking.
    _seed_operation_result(
        migrated_temp_db_connection,
        operation_result_id="op-res-1",
        execution_id="exec-1",
        operation_id="op-1",
        decision_id="dec-1",
        execution_status="blocked",
    )
    _seed_operation_result(
        migrated_temp_db_connection,
        operation_result_id="op-res-2",
        execution_id="exec-1",
        operation_id="op-2",
        decision_id="dec-1",
        execution_status="partially_blocked",
    )
    _seed_operation_result(
        migrated_temp_db_connection,
        operation_result_id="op-res-3",
        execution_id="exec-1",
        operation_id="op-3",
        decision_id="dec-1",
        execution_status="conflict",
    )
    # An executed operation does NOT count as blocking.
    _seed_operation_result(
        migrated_temp_db_connection,
        operation_result_id="op-res-4",
        execution_id="exec-1",
        operation_id="op-4",
        decision_id="dec-1",
        execution_status="executed",
    )

    items = summarize_reconciliation_review_queue_from_repository(migrated_temp_db_connection)
    assert len(items) == 1
    assert items[0].blocking_count == 3
    assert "3 blocking" in items[0].summary


def test_blocking_count_zero_when_no_blocked_operations(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seed_review_queue_row(migrated_temp_db_connection, public_id="rq-noblk")
    _seed_execution(
        migrated_temp_db_connection,
        execution_id="exec-2",
        idempotency_key="idem-noblk",
    )
    _seed_decision(
        migrated_temp_db_connection,
        decision_public_id="dec-2",
        review_queue_public_id="rq-noblk",
    )
    _seed_operation_result(
        migrated_temp_db_connection,
        operation_result_id="op-res-ok",
        execution_id="exec-2",
        operation_id="op-ok",
        decision_id="dec-2",
        execution_status="executed",
    )

    items = summarize_reconciliation_review_queue_from_repository(migrated_temp_db_connection)
    assert items[0].blocking_count == 0
    assert "blocking" not in items[0].summary


def test_blocking_count_isolated_per_review_item(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seed_review_queue_row(migrated_temp_db_connection, public_id="rq-a")
    _seed_review_queue_row(migrated_temp_db_connection, public_id="rq-b")
    _seed_execution(
        migrated_temp_db_connection,
        execution_id="exec-3",
        idempotency_key="idem-split",
    )
    _seed_decision(
        migrated_temp_db_connection,
        decision_public_id="dec-a",
        review_queue_public_id="rq-a",
    )
    _seed_decision(
        migrated_temp_db_connection,
        decision_public_id="dec-b",
        review_queue_public_id="rq-b",
    )
    _seed_operation_result(
        migrated_temp_db_connection,
        operation_result_id="op-a-1",
        execution_id="exec-3",
        operation_id="op-a-1",
        decision_id="dec-a",
        execution_status="blocked",
    )
    _seed_operation_result(
        migrated_temp_db_connection,
        operation_result_id="op-a-2",
        execution_id="exec-3",
        operation_id="op-a-2",
        decision_id="dec-a",
        execution_status="blocked",
    )
    _seed_operation_result(
        migrated_temp_db_connection,
        operation_result_id="op-b-1",
        execution_id="exec-3",
        operation_id="op-b-1",
        decision_id="dec-b",
        execution_status="executed",
    )

    items = summarize_reconciliation_review_queue_from_repository(migrated_temp_db_connection)
    by_id = {item.review_id: item for item in items}
    assert by_id["rq-a"].blocking_count == 2
    assert by_id["rq-b"].blocking_count == 0


# ---------------------------------------------------------------------------
# 7. CLI preview renders stable output
# ---------------------------------------------------------------------------


def test_cli_renders_stable_output(
    migrated_temp_db_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Seed via a writable connection, then preview via the read-only CLI.
    conn = connect_temp_db(migrated_temp_db_path)
    try:
        _seed_review_queue_row(
            conn,
            public_id="rq-cli-1",
            candidate_id="cand-cli",
            issue_type="amount_mismatch",
            status="pending",
            reason_codes_json='["AMOUNT_DIFFERS"]',
        )
        _seed_evidence_row(
            conn,
            public_id="ev-cli-1",
            review_queue_public_id="rq-cli-1",
        )
    finally:
        conn.close()

    assert migrated_temp_db_path != LIVE_DB
    exit_code = cli_main(["--db", str(migrated_temp_db_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Reconciliation Review Queue Preview" in captured.out
    assert "read-only" in captured.out
    assert "Count:     1" in captured.out
    assert "rq-cli-1" in captured.out
    assert "source=reconciliation_review_queue:cand-cli" in captured.out
    assert "status=pending" in captured.out
    assert "priority=high" in captured.out
    assert "reason=amount_mismatch (AMOUNT_DIFFERS)" in captured.out
    assert "evidence_count=1" in captured.out
    assert "blocking_count=0" in captured.out


def test_cli_empty_queue_renders_stable_output(
    migrated_temp_db_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert migrated_temp_db_path != LIVE_DB
    exit_code = cli_main(["--db", str(migrated_temp_db_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Count:     0" in captured.out
    assert "No review queue items require attention." in captured.out


def test_cli_json_output_is_stable(
    migrated_temp_db_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    conn = connect_temp_db(migrated_temp_db_path)
    try:
        _seed_review_queue_row(conn, public_id="rq-json", issue_type="date_mismatch")
    finally:
        conn.close()

    exit_code = cli_main(["--db", str(migrated_temp_db_path), "--json"])
    captured = capsys.readouterr()

    import json

    assert exit_code == 0
    payload: list[dict[str, Any]] = json.loads(captured.out)
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["review_id"] == "rq-json"
    assert payload[0]["priority"] == "medium"
    assert set(payload[0]) == {
        "review_id",
        "source_type",
        "source_id",
        "status",
        "priority",
        "reason",
        "evidence_count",
        "blocking_count",
        "created_at",
        "summary",
    }


def test_cli_refuses_live_db(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli_main(["--db", str(LIVE_DB)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "refusing to use live database" in captured.err


def test_cli_missing_db_returns_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "does-not-exist.sqlite"
    assert missing != LIVE_DB
    exit_code = cli_main(["--db", str(missing)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "database not found" in captured.err


def test_cli_requires_db_arg(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_main([])
    assert exc.value.code == 2  # argparse required-arg failure


# ---------------------------------------------------------------------------
# 8. Read-only behaviour
# ---------------------------------------------------------------------------


def test_builder_does_not_change_row_counts(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seed_review_queue_row(migrated_temp_db_connection, public_id="rq-ro")
    _seed_evidence_row(
        migrated_temp_db_connection,
        public_id="ev-ro",
        review_queue_public_id="rq-ro",
    )
    before = _snapshot_counts(migrated_temp_db_connection)

    _ = summarize_reconciliation_review_queue_from_repository(migrated_temp_db_connection)
    _ = summarize_reconciliation_review_queue_from_repository(migrated_temp_db_connection)

    after = _snapshot_counts(migrated_temp_db_connection)
    assert before == after


def test_builder_does_not_introduce_pending_transactions(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seed_review_queue_row(migrated_temp_db_connection, public_id="rq-tx")
    # Commit so there is no pending transaction from seeding.
    migrated_temp_db_connection.commit()

    _ = summarize_reconciliation_review_queue_from_repository(migrated_temp_db_connection)

    # A read-only connection flow must leave no open write transaction.
    # in_transaction is False after a pure SELECT flow with autocommit selects.
    assert migrated_temp_db_connection.in_transaction is False


def test_cli_does_not_change_row_counts(
    migrated_temp_db_path: Path,
) -> None:
    conn = connect_temp_db(migrated_temp_db_path)
    try:
        _seed_review_queue_row(conn, public_id="rq-cli-ro")
        _seed_evidence_row(conn, public_id="ev-cli-ro", review_queue_public_id="rq-cli-ro")
        before = _snapshot_counts(conn)
    finally:
        conn.close()

    assert cli_main(["--db", str(migrated_temp_db_path)]) == 0

    conn = connect_temp_db(migrated_temp_db_path)
    try:
        after = _snapshot_counts(conn)
    finally:
        conn.close()

    assert before == after


def test_preview_uses_temp_db_not_live_db(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    db_file = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()["file"]
    assert Path(db_file) != LIVE_DB
    assert str(LIVE_DB) not in str(db_file)

    _ = summarize_reconciliation_review_queue_from_repository(migrated_temp_db_connection)


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------


def test_preview_item_is_frozen(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seed_review_queue_row(migrated_temp_db_connection, public_id="rq-frozen")
    items = summarize_reconciliation_review_queue_from_repository(migrated_temp_db_connection)
    assert len(items) == 1
    with pytest.raises(Exception):
        items[0].status = "resolved"  # type: ignore[misc]
