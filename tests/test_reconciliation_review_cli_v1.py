"""Tests for Reconciliation Review CLI v1.

Covers:
  1. CLI review command runs successfully
  2. CLI output includes stable summary counts
  3. CLI output includes priority review items
  4. CLI output does not require live DB
  5. CLI handles missing files gracefully
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "reconciliation"
STMT_CSV = FIXTURES / "review_queue_statement.csv"
APP_JSON = FIXTURES / "review_queue_app_transactions.json"
DEMO_CLI = REPO_ROOT / "finance_core" / "reconciliation" / "demo_cli.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_review(args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "finance_core.reconciliation.demo_cli", "review"]
    if args:
        cmd.extend(args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


# ---------------------------------------------------------------------------
# 1. CLI runs successfully
# ---------------------------------------------------------------------------


def test_review_command_runs():
    result = _run_review(
        [
            "--statement",
            str(STMT_CSV),
            "--app-transactions",
            str(APP_JSON),
        ]
    )
    assert result.returncode == 0
    assert "Reconciliation Review Queue" in result.stdout


# ---------------------------------------------------------------------------
# 2. Output includes stable summary counts
# ---------------------------------------------------------------------------


def test_summary_counts_in_output():
    result = _run_review(
        [
            "--statement",
            str(STMT_CSV),
            "--app-transactions",
            str(APP_JSON),
        ]
    )
    assert "Summary:" in result.stdout
    assert "review required:" in result.stdout
    assert "matched:" in result.stdout
    assert "high priority:" in result.stdout
    assert "medium priority:" in result.stdout
    assert "low priority:" in result.stdout


# ---------------------------------------------------------------------------
# 3. CLI includes priority review items
# ---------------------------------------------------------------------------


def test_priority_items_in_output():
    result = _run_review(
        [
            "--statement",
            str(STMT_CSV),
            "--app-transactions",
            str(APP_JSON),
        ]
    )
    # Should list priority-tier items or note all matched
    assert ("High Priority" in result.stdout) or ("No review items" in result.stdout)
    # The output should be non-empty
    assert len(result.stdout.strip()) > 50


# ---------------------------------------------------------------------------
# 4. No live DB requirement
# ---------------------------------------------------------------------------


def test_review_cli_no_db_required():
    """The in-memory review command should not touch database/finance.db."""
    result = _run_review(
        [
            "--statement",
            str(STMT_CSV),
            "--app-transactions",
            str(APP_JSON),
        ]
    )
    assert result.returncode == 0
    # No DB-related output should appear (not "sqlite", not "database")
    stderr_lower = result.stderr.lower()
    assert "sqlite" not in stderr_lower
    assert "database" not in stderr_lower or "database" not in result.stderr


# ---------------------------------------------------------------------------
# 5. Missing file handling
# ---------------------------------------------------------------------------


def test_missing_statement_file():
    result = _run_review(
        [
            "--statement",
            "/nonexistent/path.csv",
            "--app-transactions",
            str(APP_JSON),
        ]
    )
    assert result.returncode == 1
    assert "not found" in result.stderr.lower() or "error" in result.stderr.lower()


def test_missing_app_file():
    result = _run_review(
        [
            "--statement",
            str(STMT_CSV),
            "--app-transactions",
            "/nonexistent/path.json",
        ]
    )
    assert result.returncode == 1
    assert "not found" in result.stderr.lower() or "error" in result.stderr.lower()
