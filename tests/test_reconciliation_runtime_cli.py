"""Tests for Reconciliation Runtime CLI v1.

Covers: runtime execution, output formatting, CLI subprocess exit codes,
deterministic output, dry-run safety, and error cases.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from finance_core.reconciliation.runtime_cli import (
    format_runtime_summary,
    run_reconciliation_runtime,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"
CSV_PATH = REPO_ROOT / "tests" / "fixtures" / "reconciliation" / "sample_statement.csv"
JSON_PATH = REPO_ROOT / "tests" / "fixtures" / "reconciliation" / "sample_app_transactions.json"


class TestRunReconciliationRuntime:
    """Tests for the run_reconciliation_runtime() callable function."""

    def test_run_with_demo_fixtures_dry_run(self) -> None:
        result = run_reconciliation_runtime(
            statement_csv_path=CSV_PATH,
            app_transactions_json_path=JSON_PATH,
            dry_run=True,
        )
        assert result.exit_code == 0
        assert result.dry_run is True
        assert result.statement_rows_count > 0
        assert result.app_transactions_count > 0
        assert len(result.statements) == result.statement_rows_count
        assert len(result.app_transactions) == result.app_transactions_count
        assert len(result.candidates) > 0
        assert len(result.queue_items) > 0
        assert result.error_message == ""

    def test_run_with_demo_fixtures_no_dry_run(self) -> None:
        result = run_reconciliation_runtime(
            statement_csv_path=CSV_PATH,
            app_transactions_json_path=JSON_PATH,
            dry_run=False,
        )
        assert result.exit_code == 0
        assert result.dry_run is False
        assert result.statement_rows_count > 0
        assert result.db_path.exists()
        try:
            os.unlink(result.db_path)
        except OSError:
            pass

    def test_result_structure_has_all_fields(self) -> None:
        result = run_reconciliation_runtime(
            statement_csv_path=CSV_PATH,
            app_transactions_json_path=JSON_PATH,
            dry_run=True,
        )
        assert result.exit_code == 0
        assert len(result.candidates) > 0
        assert len(result.queue_items) > 0
        # Verify queue items can be inspected
        matched = sum(1 for q in result.queue_items if q.issue_type.value == "matched")
        assert matched >= 0
        assert len(result.queue_items) >= len(result.candidates)

    def test_live_database_not_modified(self) -> None:
        mtime_before = os.path.getmtime(LIVE_DB_PATH) if LIVE_DB_PATH.exists() else -1
        run_reconciliation_runtime(
            statement_csv_path=CSV_PATH,
            app_transactions_json_path=JSON_PATH,
            dry_run=True,
        )
        if LIVE_DB_PATH.exists():
            assert os.path.getmtime(LIVE_DB_PATH) == mtime_before

    def test_deterministic_output(self) -> None:
        result1 = run_reconciliation_runtime(
            statement_csv_path=CSV_PATH,
            app_transactions_json_path=JSON_PATH,
            dry_run=True,
        )
        result2 = run_reconciliation_runtime(
            statement_csv_path=CSV_PATH,
            app_transactions_json_path=JSON_PATH,
            dry_run=True,
        )
        assert result1.exit_code == result2.exit_code
        assert result1.statement_rows_count == result2.statement_rows_count
        assert len(result1.candidates) == len(result2.candidates)
        assert len(result1.queue_items) == len(result2.queue_items)
        for c1, c2 in zip(result1.candidates, result2.candidates):
            assert c1.match_status == c2.match_status
            assert c1.issue_type == c2.issue_type

    def test_refuses_live_db_path(self) -> None:
        result = run_reconciliation_runtime(
            statement_csv_path=CSV_PATH,
            app_transactions_json_path=JSON_PATH,
            db_path=LIVE_DB_PATH,
            dry_run=True,
        )
        assert result.exit_code == 1
        assert "Refusing to use live database" in result.error_message

    def test_nonexistent_statement_csv(self) -> None:
        result = run_reconciliation_runtime(
            statement_csv_path="/nonexistent/statement.csv",
            app_transactions_json_path=JSON_PATH,
            dry_run=True,
        )
        assert result.exit_code == 1
        assert "Failed to load statement CSV" in result.error_message

    def test_nonexistent_app_transactions_json(self) -> None:
        result = run_reconciliation_runtime(
            statement_csv_path=CSV_PATH,
            app_transactions_json_path="/nonexistent/app.json",
            dry_run=True,
        )
        assert result.exit_code == 1
        assert "Failed to load app transactions" in result.error_message


class TestFormatRuntimeSummary:
    """Tests for the format_runtime_summary() output formatter."""

    def test_output_contains_all_sections(self) -> None:
        result = run_reconciliation_runtime(
            statement_csv_path=CSV_PATH,
            app_transactions_json_path=JSON_PATH,
            dry_run=True,
        )
        output = format_runtime_summary(result)
        assert "Reconciliation Runtime v1" in output
        assert "Imported / Loaded Records" in output
        assert "Statement-Side Transactions" in output
        assert "App-Side Candidate Transactions" in output
        assert "Match Results" in output
        assert "Unmatched Records" in output
        assert "Review Queue" in output
        assert "Guarded Apply Plan" in output
        assert "Safety" in output

    def test_output_shows_dry_run_mode(self) -> None:
        result = run_reconciliation_runtime(
            statement_csv_path=CSV_PATH,
            app_transactions_json_path=JSON_PATH,
            dry_run=True,
        )
        output = format_runtime_summary(result)
        assert "dry-run" in output.lower()

    def test_output_shows_no_dry_run_mode(self) -> None:
        result = run_reconciliation_runtime(
            statement_csv_path=CSV_PATH,
            app_transactions_json_path=JSON_PATH,
            dry_run=False,
        )
        output = format_runtime_summary(result)
        assert "Database:" in output or "database" in output.lower()
        try:
            os.unlink(result.db_path)
        except OSError:
            pass

    def test_safety_footers_present(self) -> None:
        result = run_reconciliation_runtime(
            statement_csv_path=CSV_PATH,
            app_transactions_json_path=JSON_PATH,
            dry_run=True,
        )
        output = format_runtime_summary(result)
        assert "Final transactions mutated:   0" in output
        assert "Settlement obligations created:  0" in output
        assert "Live database touched:         No" in output


class TestRuntimeCLI:
    """Tests for the CLI entry point via subprocess."""

    def test_cli_exits_zero_dry_run(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "finance_core.reconciliation.runtime_cli",
                "--statement",
                str(CSV_PATH),
                "--app-transactions",
                str(JSON_PATH),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 0, f"stderr: {proc.stderr}"
        assert "Reconciliation Runtime v1" in proc.stdout

    def test_cli_exits_zero_no_dry_run(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "finance_core.reconciliation.runtime_cli",
                "--statement",
                str(CSV_PATH),
                "--app-transactions",
                str(JSON_PATH),
                "--no-dry-run",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 0, f"stderr: {proc.stderr}"
        assert "Database:" in proc.stdout or "database" in proc.stdout.lower()

    def test_cli_exits_nonzero_on_missing_file(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "finance_core.reconciliation.runtime_cli",
                "--statement",
                "/nonexistent/file.csv",
                "--app-transactions",
                str(JSON_PATH),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 1

    def test_cli_exits_nonzero_on_live_db(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "finance_core.reconciliation.runtime_cli",
                "--statement",
                str(CSV_PATH),
                "--app-transactions",
                str(JSON_PATH),
                "--db",
                str(LIVE_DB_PATH),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 1
        assert "Refusing to use live database" in proc.stderr
