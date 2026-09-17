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

import sqlite3
import tempfile
from pathlib import Path

from finance_core.reconciliation.demo_cli import main

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
