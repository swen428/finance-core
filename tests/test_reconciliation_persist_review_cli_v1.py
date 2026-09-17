"""Tests for persist-review CLI command v1.

Covers:
  1. persist-review command runs against temporary DB
  2. Command output includes stable summary counts
  3. Review queue rows are written
  4. database/finance.db is not touched
  5. Command refuses database/finance.db
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
_LIVE_DB = _REPO_ROOT / "database" / "finance.db"


# ---------------------------------------------------------------------------
# 1. persist-review command runs against temporary DB
# ---------------------------------------------------------------------------


def test_persist_review_runs_against_temp_db():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", prefix="test_persist_", delete=False) as f:
        db_path = f.name

    try:
        argv = [
            "persist-review",
            "--statement",
            str(_STATEMENT_CSV),
            "--app-transactions",
            str(_APP_TXN_JSON),
            "--db",
            db_path,
        ]
        exit_code = main(argv)
        assert exit_code == 0
    finally:
        Path(db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 2. Command output includes stable summary counts
# ---------------------------------------------------------------------------


def test_persist_review_output_includes_summary():
    import sys
    from io import StringIO

    with tempfile.NamedTemporaryFile(suffix=".sqlite", prefix="test_summary_", delete=False) as f:
        db_path = f.name

    try:
        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            argv = [
                "persist-review",
                "--statement",
                str(_STATEMENT_CSV),
                "--app-transactions",
                str(_APP_TXN_JSON),
                "--db",
                db_path,
            ]
            exit_code = main(argv)
            assert exit_code == 0

            output = captured.getvalue()
            assert "Run ID:" in output
            assert "Statement transactions:" in output
            assert "App transactions:" in output
            assert "Matched:" in output
            assert "Needs review:" in output
            assert "Persisted review queue:" in output
            assert "Pending review:" in output
        finally:
            sys.stdout = old_stdout
    finally:
        Path(db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 3. Review queue rows are written
# ---------------------------------------------------------------------------


def test_persist_review_writes_review_queue_rows():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", prefix="test_write_", delete=False) as f:
        db_path = f.name

    try:
        argv = [
            "persist-review",
            "--statement",
            str(_STATEMENT_CSV),
            "--app-transactions",
            str(_APP_TXN_JSON),
            "--db",
            db_path,
        ]
        exit_code = main(argv)
        assert exit_code == 0

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM reconciliation_review_queue"
            ).fetchone()["cnt"]
            assert count > 0

            pending = conn.execute(
                "SELECT COUNT(*) AS cnt FROM reconciliation_review_queue WHERE status = 'pending'"
            ).fetchone()["cnt"]
            assert pending > 0
        finally:
            conn.close()
    finally:
        Path(db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 4. database/finance.db is not touched
# ---------------------------------------------------------------------------


def test_persist_review_does_not_touch_live_db():
    """Verify that running persist-review on a temp DB does not modify
    database/finance.db."""
    from pathlib import Path as _Path

    # Get the live DB path
    live_db = _LIVE_DB

    if live_db.exists():
        original_mtime = live_db.stat().st_mtime

        with tempfile.NamedTemporaryFile(
            suffix=".sqlite", prefix="test_notouch_", delete=False
        ) as f:
            db_path = f.name

        try:
            argv = [
                "persist-review",
                "--statement",
                str(_STATEMENT_CSV),
                "--app-transactions",
                str(_APP_TXN_JSON),
                "--db",
                db_path,
            ]
            exit_code = main(argv)
            assert exit_code == 0

            if live_db.exists():
                assert live_db.stat().st_mtime == original_mtime, (
                    "Live database should not be touched!"
                )
        finally:
            _Path(db_path).unlink(missing_ok=True)
    else:
        # If live DB doesn't exist, just verify the command works on temp
        with tempfile.NamedTemporaryFile(
            suffix=".sqlite", prefix="test_notouch_", delete=False
        ) as f:
            db_path = f.name

        try:
            argv = [
                "persist-review",
                "--statement",
                str(_STATEMENT_CSV),
                "--app-transactions",
                str(_APP_TXN_JSON),
                "--db",
                db_path,
            ]
            exit_code = main(argv)
            assert exit_code == 0
        finally:
            _Path(db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 5. Command refuses database/finance.db
# ---------------------------------------------------------------------------


def test_persist_review_refuses_live_db():
    argv = [
        "persist-review",
        "--statement",
        str(_STATEMENT_CSV),
        "--app-transactions",
        str(_APP_TXN_JSON),
        "--db",
        str(_LIVE_DB),
    ]
    exit_code = main(argv)
    assert exit_code != 0


def test_persist_review_fails_without_db():
    argv = [
        "persist-review",
        "--statement",
        str(_STATEMENT_CSV),
        "--app-transactions",
        str(_APP_TXN_JSON),
    ]
    exit_code = main(argv)
    assert exit_code != 0


# ---------------------------------------------------------------------------
# 7. Two runs with different --run-id do not collide
# ---------------------------------------------------------------------------


def test_two_runs_different_run_ids_no_collision():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", prefix="test_twice_", delete=False) as f:
        db_path = f.name

    try:
        # First run
        argv1 = [
            "persist-review",
            "--statement",
            str(_STATEMENT_CSV),
            "--app-transactions",
            str(_APP_TXN_JSON),
            "--db",
            db_path,
            "--run-id",
            "run-alpha",
        ]
        exit_code = main(argv1)
        assert exit_code == 0

        # Second run with different --run-id
        argv2 = [
            "persist-review",
            "--statement",
            str(_STATEMENT_CSV),
            "--app-transactions",
            str(_APP_TXN_JSON),
            "--db",
            db_path,
            "--run-id",
            "run-beta",
        ]
        exit_code = main(argv2)
        assert exit_code == 0

        # Verify both runs are present
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            runs = conn.execute(
                "SELECT DISTINCT run_public_id FROM reconciliation_review_queue"
            ).fetchall()
            run_ids = {r["run_public_id"] for r in runs}
            assert "run-alpha" in run_ids
            assert "run-beta" in run_ids

            # All public_ids must be unique across both runs
            all_ids = conn.execute("SELECT public_id FROM reconciliation_review_queue").fetchall()
            id_list = [r["public_id"] for r in all_ids]
            assert len(id_list) == len(set(id_list)), f"Duplicate public_ids detected: {id_list}"

            # Alpha run items should have run-alpha prefix
            alpha_items = conn.execute(
                "SELECT public_id "
                "FROM reconciliation_review_queue "
                "WHERE run_public_id = 'run-alpha'"
            ).fetchall()
            for row in alpha_items:
                assert row["public_id"].startswith("run-alpha-"), (
                    f"Alpha item {row['public_id']} lacks run-alpha prefix"
                )

            # Beta run items should have run-beta prefix
            beta_items = conn.execute(
                "SELECT public_id FROM reconciliation_review_queue WHERE run_public_id = 'run-beta'"
            ).fetchall()
            for row in beta_items:
                assert row["public_id"].startswith("run-beta-"), (
                    f"Beta item {row['public_id']} lacks run-beta prefix"
                )
        finally:
            conn.close()
    finally:
        Path(db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 8. Omitted --run-id generates a non-empty run_public_id
# ---------------------------------------------------------------------------


def test_omitted_run_id_generates_nonempty_run_id():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", prefix="test_no_run_", delete=False) as f:
        db_path = f.name

    try:
        argv = [
            "persist-review",
            "--statement",
            str(_STATEMENT_CSV),
            "--app-transactions",
            str(_APP_TXN_JSON),
            "--db",
            db_path,
        ]
        exit_code = main(argv)
        assert exit_code == 0

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            runs = conn.execute(
                "SELECT DISTINCT run_public_id FROM reconciliation_review_queue"
            ).fetchall()
            assert len(runs) == 1
            run_id = runs[0]["run_public_id"]
            assert run_id, "run_public_id should not be empty"
            assert run_id.startswith("run-"), (
                f"Auto-generated run_id should start with 'run-', got: {run_id}"
            )

            # Queue items should be prefixed with the run id
            items = conn.execute("SELECT public_id FROM reconciliation_review_queue").fetchall()
            for row in items:
                assert row["public_id"].startswith(run_id + "-"), (
                    f"Item {row['public_id']} should be prefixed with run_id {run_id}"
                )
        finally:
            conn.close()
    finally:
        Path(db_path).unlink(missing_ok=True)
