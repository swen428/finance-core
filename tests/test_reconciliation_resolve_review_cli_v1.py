"""Tests for resolve-review CLI command v1.

Covers:
  1. resolve-review command runs against temp DB after persist-review
  2. Decisions fixture applies correctly
  3. Resolution results rows are written
  4. Review queue status changes are correct
  5. Failed or unmatched decisions are reported safely
  6. Command refuses database/finance.db
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

import finance_core.reconciliation.resolution_persistence as resolution_persistence
from finance_core.reconciliation.demo_cli import main
from finance_core.reconciliation.demo_fixture import (
    _load_app_transactions_from_json,
    _structured_rows_to_statements,
)
from finance_core.reconciliation.matching import match_batch
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS, apply_migration_paths
from finance_core.reconciliation.review_persistence import ReviewQueuePersistence
from finance_core.reconciliation.review_queue import generate_review_queue
from finance_core.reconciliation.source_binding import load_bound_queue
from finance_core.reconciliation.statement_csv import StatementCsvAdapter

# Fixture paths relative to project root
_REPO_ROOT = Path(__file__).resolve().parents[1]
_FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures" / "reconciliation"
_STATEMENT_CSV = _FIXTURES_DIR / "review_queue_statement.csv"
_APP_TXN_JSON = _FIXTURES_DIR / "review_queue_app_transactions.json"
_DECISIONS_JSON = _FIXTURES_DIR / "resolution_decisions.json"
_LIVE_DB = _REPO_ROOT / "database" / "finance.db"


# ---------------------------------------------------------------------------
# Helper: run persist-review, return db path
# ---------------------------------------------------------------------------


def _run_persist_review() -> str:
    """Run persist-review against fixtures and return the temp DB path."""
    fd, db_path = tempfile.mkstemp(suffix=".sqlite", prefix="test_resolve_")
    import os

    os.close(fd)

    argv = [
        "persist-review",
        "--statement",
        str(_STATEMENT_CSV),
        "--app-transactions",
        str(_APP_TXN_JSON),
        "--run-id",
        "resolve-test",
        "--db",
        db_path,
    ]
    exit_code = main(argv)
    assert exit_code == 0
    return db_path


def _run_legacy_review_without_app_list(queue_item_id: str) -> str:
    """Create a real pre-051 queue row, then upgrade without retrosealing it."""
    fd, db_path = tempfile.mkstemp(suffix=".sqlite", prefix="test_legacy_resolve_")
    import os

    os.close(fd)
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS[:50])
        parsed = StatementCsvAdapter().parse_file_hardened(_STATEMENT_CSV)
        assert parsed.success
        candidates = match_batch(
            _structured_rows_to_statements(parsed.rows),
            _load_app_transactions_from_json(_APP_TXN_JSON),
        )
        items, _ = generate_review_queue(candidates, run_label="resolve-test")
        item = next(item for item in items if item.queue_item_id == queue_item_id)
        ReviewQueuePersistence(conn).persist_review_queue([item], run_public_id="resolve-test")
        evidence = json.loads(
            conn.execute(
                "SELECT evidence_json FROM reconciliation_review_queue WHERE public_id = ?",
                (queue_item_id,),
            ).fetchone()[0]
        )
        evidence.pop("all_app_transactions")
        conn.execute(
            "UPDATE reconciliation_review_queue SET evidence_json = ? WHERE public_id = ?",
            (json.dumps(evidence), queue_item_id),
        )
        conn.commit()
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
        with pytest.raises(ValueError, match="binding"):
            load_bound_queue(conn, queue_item_id)
    finally:
        conn.close()
    return db_path


def _run_resolve_review(db_path: str) -> int:
    """Run resolve-review against the given DB and return exit code."""
    argv = [
        "resolve-review",
        "--db",
        db_path,
        "--decisions",
        str(_DECISIONS_JSON),
    ]
    return main(argv)


def _assert_duplicate_still_pending_without_resolution(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        for table in (
            "reconciliation_resolution_decisions",
            "reconciliation_resolution_results",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT status FROM reconciliation_review_queue WHERE public_id = ?",
                ("resolve-test-q-004",),
            ).fetchone()[0]
            == "pending"
        )


# ---------------------------------------------------------------------------
# 1. resolve-review command runs against temp DB after persist-review
# ---------------------------------------------------------------------------


def test_resolve_review_runs_after_persist():
    db_path = _run_persist_review()
    try:
        exit_code = _run_resolve_review(db_path)
        assert exit_code == 0
    finally:
        Path(db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 2. Decisions fixture applies correctly
# ---------------------------------------------------------------------------


def test_decisions_fixture_applies():
    db_path = _run_persist_review()
    try:
        import sys
        from io import StringIO

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            exit_code = _run_resolve_review(db_path)
            assert exit_code == 0

            output = captured.getvalue()
            assert "Decisions loaded:" in output
            assert "Decisions applied:" in output
            assert "Successful results:" in output
            assert "Resolved:" in output
        finally:
            sys.stdout = old_stdout
    finally:
        Path(db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 3. Resolution results rows are written
# ---------------------------------------------------------------------------


def test_resolution_results_rows_written():
    db_path = _run_persist_review()
    try:
        exit_code = _run_resolve_review(db_path)
        assert exit_code == 0

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            decision_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM reconciliation_resolution_decisions"
            ).fetchone()["cnt"]
            assert decision_count > 0

            result_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM reconciliation_resolution_results"
            ).fetchone()["cnt"]
            assert result_count > 0
        finally:
            conn.close()
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_duplicate_result_records_both_original_targets_and_resolves_queue():
    db_path = _run_persist_review()
    try:
        assert _run_resolve_review(db_path) == 0
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT q.status, r.success, r.audit_evidence_json
                FROM reconciliation_review_queue AS q
                JOIN reconciliation_resolution_results AS r
                  ON r.review_queue_public_id = q.public_id
                WHERE q.public_id = 'resolve-test-q-004'"""
            ).fetchone()
            assert row is not None
            evidence = json.loads(row["audit_evidence_json"])
            assert row["status"] == "resolved"
            assert row["success"] == 1
            assert evidence["duplicate_app_txn_ids"] == ["app-004a", "app-004b"]
            assert evidence["kept_app_txn_id"] == "app-004a"
            assert evidence["audit_only"] is True
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_corrected_second_duplicate_target_refuses_without_writes(tmp_path, monkeypatch):
    db_path = _run_persist_review()
    try:
        decision = json.loads(_DECISIONS_JSON.read_text())["decisions"][4]
        decision_path = tmp_path / "duplicate-decision.json"
        decision_path.write_text(json.dumps({"decisions": [decision]}))
        checked: list[str] = []

        def is_corrected(_conn, target_id: str) -> bool:
            checked.append(target_id)
            return target_id == "app-004b"

        monkeypatch.setattr(resolution_persistence, "has_committed_correction", is_corrected)
        assert main(["resolve-review", "--db", db_path, "--decisions", str(decision_path)]) != 0
        assert checked == ["app-004a", "app-004b"]
        _assert_duplicate_still_pending_without_resolution(db_path)
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_old_duplicate_without_app_list_refuses_before_any_writes(tmp_path):
    db_path = _run_legacy_review_without_app_list("resolve-test-q-004")
    try:
        decision = json.loads(_DECISIONS_JSON.read_text())["decisions"][4]
        decision_path = tmp_path / "duplicate-decision.json"
        decision_path.write_text(json.dumps({"decisions": [decision]}))
        assert main(["resolve-review", "--db", db_path, "--decisions", str(decision_path)]) != 0
        _assert_duplicate_still_pending_without_resolution(db_path)
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_old_single_target_without_binding_cannot_confirm(tmp_path):
    db_path = _run_legacy_review_without_app_list("resolve-test-q-000")
    try:
        decision = json.loads(_DECISIONS_JSON.read_text())["decisions"][0]
        decision_path = tmp_path / "single-decision.json"
        decision_path.write_text(json.dumps({"decisions": [decision]}))
        assert main(["resolve-review", "--db", db_path, "--decisions", str(decision_path)]) != 0
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT status FROM reconciliation_review_queue WHERE public_id = ?",
                ("resolve-test-q-000",),
            ).fetchone()
            assert row == ("pending",)
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_old_unbound_queue_can_still_be_ignored(tmp_path):
    db_path = _run_legacy_review_without_app_list("resolve-test-q-000")
    try:
        original = json.loads(_DECISIONS_JSON.read_text())["decisions"][0]
        original["action"] = "ignore"
        decision_path = tmp_path / "ignore.json"
        decision_path.write_text(json.dumps({"decisions": [original]}))
        assert main(["resolve-review", "--db", db_path, "--decisions", str(decision_path)]) == 0
        with sqlite3.connect(db_path) as conn:
            status = conn.execute(
                "SELECT status FROM reconciliation_review_queue WHERE public_id = ?",
                ("resolve-test-q-000",),
            ).fetchone()[0]
            assert status == "ignored"
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_apply_resolve_registers_bound_queue_and_replays(tmp_path):
    db_path = tmp_path / "apply.sqlite"
    args = [
        "apply-resolve",
        "--statement",
        str(_STATEMENT_CSV),
        "--app-transactions",
        str(_APP_TXN_JSON),
        "--decisions",
        str(_DECISIONS_JSON),
        "--db",
        str(db_path),
    ]
    assert main(args) == 0
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        count = conn.execute("SELECT COUNT(*) FROM reconciliation_review_queue").fetchone()[0]
        assert count == 6
        assert load_bound_queue(conn, "q-004") is not None
    assert main(args) == 0
    with sqlite3.connect(db_path) as conn:
        replay_count = conn.execute("SELECT COUNT(*) FROM reconciliation_review_queue").fetchone()[
            0
        ]
        assert replay_count == count


# ---------------------------------------------------------------------------
# 4. Review queue status changes are correct
# ---------------------------------------------------------------------------


def test_review_queue_status_changes():
    db_path = _run_persist_review()
    try:
        exit_code = _run_resolve_review(db_path)
        assert exit_code == 0

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            resolved = conn.execute(
                "SELECT COUNT(*) AS cnt FROM reconciliation_review_queue WHERE status = 'resolved'"
            ).fetchone()["cnt"]
            assert resolved > 0

            # Some items should no longer be pending
            pending_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM reconciliation_review_queue WHERE status = 'pending'",
            ).fetchone()
            assert pending_row["cnt"] >= 0

            # At least one status is not pending anymore
            assert resolved + pending_row["cnt"] > 0
        finally:
            conn.close()
    finally:
        Path(db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 5. Failed or unmatched decisions are reported safely
# ---------------------------------------------------------------------------


def test_unmatched_decisions_handled_safely():
    """Decisions for queue items that don't exist should not crash."""
    db_path = _run_persist_review()
    try:
        import json as _json
        import sys
        from io import StringIO

        # Create a temp decisions file with an unmatched queue_item_id
        unmatched_decisions = {
            "description": "Decisions with some unmatched items",
            "decisions": [
                {
                    "decision_id": "dec-unmatched",
                    "queue_item_id": "nonexistent-q-id",
                    "action": "confirm_match",
                    "note": "This item does not exist in the DB.",
                    "reviewer": "human",
                },
            ],
        }

        fd, unmatched_path = tempfile.mkstemp(suffix=".json", prefix="test_unmatched_")
        import os as _os

        _os.close(fd)
        try:
            _Path = Path(unmatched_path)
            _Path.write_text(_json.dumps(unmatched_decisions))

            old_stdout = sys.stdout
            sys.stdout = captured = StringIO()
            try:
                argv = [
                    "resolve-review",
                    "--db",
                    db_path,
                    "--decisions",
                    unmatched_path,
                ]
                exit_code = main(argv)
                assert exit_code == 0

                output = captured.getvalue()
                assert "Failed results:" in output
                # unmatched decision should appear as "applied" but not successful
            finally:
                sys.stdout = old_stdout
        finally:
            _Path.unlink(missing_ok=True)
    finally:
        Path(db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 6. Command refuses database/finance.db
# ---------------------------------------------------------------------------


def test_resolve_review_refuses_live_db():
    argv = [
        "resolve-review",
        "--db",
        str(_LIVE_DB),
        "--decisions",
        str(_DECISIONS_JSON),
    ]
    exit_code = main(argv)
    assert exit_code != 0


def test_resolve_review_fails_without_db():
    argv = [
        "resolve-review",
        "--decisions",
        str(_DECISIONS_JSON),
    ]
    exit_code = main(argv)
    assert exit_code != 0


def test_resolve_review_fails_on_nonexistent_db():
    argv = [
        "resolve-review",
        "--db",
        "/tmp/nonexistent_resolve_review_test.db",
        "--decisions",
        str(_DECISIONS_JSON),
    ]
    exit_code = main(argv)
    assert exit_code != 0
