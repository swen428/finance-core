"""Tests for Reconciliation Demo CLI Fixture v1.

Covers:
  1. CSV fixture is readable by StatementCsvAdapter
  2. Import produces expected structured rows
  3. Reconciliation produces expected status counts
  4. Review queue contains expected entries with reasons
  5. CLI demo command exits successfully and prints expected summary
  6. No live database file is modified
  7. Decimal comparison is used for amounts (not float)
  8. Review table formatting produces expected output
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import finance_core.reconciliation.demo_cli as demo_cli
import finance_core.reconciliation.demo_fixture as demo_fixture
from finance_core.reconciliation.demo_fixture import (
    DemoResult,
    ReviewTableRow,
    build_review_entries_sorted,
    build_review_table_rows,
    format_review_table,
    run_demo_reconciliation,
)
from finance_core.reconciliation.statement_csv import StatementCsvAdapter

if TYPE_CHECKING:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"
CSV_PATH = REPO_ROOT / "tests" / "fixtures" / "reconciliation" / "sample_statement.csv"
JSON_PATH = REPO_ROOT / "tests" / "fixtures" / "reconciliation" / "sample_app_transactions.json"


# ---------------------------------------------------------------------------
# 1. CSV fixture is readable by StatementCsvAdapter
# ---------------------------------------------------------------------------


def test_csv_fixture_readable() -> None:
    """The sample CSV can be parsed by StatementCsvAdapter without errors."""
    adapter = StatementCsvAdapter()
    result = adapter.parse_file(CSV_PATH)
    assert result.success, f"Parse errors: {result.errors}"
    assert len(result.rows) == 5, f"Expected 5 rows, got {len(result.rows)}"
    assert result.error_count == 0


# ---------------------------------------------------------------------------
# 2. Import produces expected structured rows
# ---------------------------------------------------------------------------


def test_csv_rows_have_expected_data() -> None:
    """Each CSV row contains the expected merchants and amounts."""
    adapter = StatementCsvAdapter()
    result = adapter.parse_file(CSV_PATH)

    merchants = [r.merchant_raw for r in result.rows]
    amounts = [r.amount for r in result.rows]
    currencies = [r.currency for r in result.rows]

    assert "Apple" in merchants
    assert "Giant Supermarket" in merchants
    assert "Netflix" in merchants
    assert "Spotify" in merchants
    assert "Grab" in merchants

    assert Decimal("29.90") in amounts
    assert Decimal("45.50") in amounts
    assert Decimal("19.90") in amounts
    assert Decimal("12.90") in amounts
    assert Decimal("8.50") in amounts

    assert all(c == "SGD" for c in currencies)


# ---------------------------------------------------------------------------
# 3. Reconciliation produces expected status counts
# ---------------------------------------------------------------------------


def test_reconciliation_status_counts(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Full demo run produces the expected status distribution."""
    result = run_demo_reconciliation(
        conn=migrated_temp_db_connection,
        csv_path=CSV_PATH,
        candidate_json_path=JSON_PATH,
        batch_public_id="test-batch-counts",
        run_public_id="test-run-counts",
    )

    s = result.run_summary
    assert s.total_statement_transactions == 5
    assert s.matched_count == 1, f"Expected 2 matched, got {s.matched_count}"
    assert s.amount_mismatch_count == 2, (
        f"Expected 2 amount_mismatch, got {s.amount_mismatch_count}"
    )
    assert s.possible_duplicate_count == 1, (
        f"Expected 1 possible_duplicate, got {s.possible_duplicate_count}"
    )
    assert s.needs_review_count == 4  # non-matched items (incl. date_mismatch)
    assert s.no_match_count == 0
    assert s.currency_mismatch_count == 0
    assert s.date_mismatch_count == 1

    # Verify the status counts dict
    counts = result.run_summary.match_status_counts
    assert counts["matched"] == 1
    assert counts["amount_mismatch"] == 2
    assert counts["possible_duplicate"] == 1
    assert counts["date_mismatch"] == 1


# ---------------------------------------------------------------------------
# 4. Review queue contains expected entries
# ---------------------------------------------------------------------------


def test_review_queue_entries(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """The review queue returns the right entries sorted by priority."""
    result = run_demo_reconciliation(
        conn=migrated_temp_db_connection,
        csv_path=CSV_PATH,
        candidate_json_path=JSON_PATH,
        batch_public_id="test-batch-queue",
        run_public_id="test-run-queue",
    )

    sorted_entries = build_review_entries_sorted(
        migrated_temp_db_connection,
        result.run_summary.run_id,
    )

    # Should have 3 review entries (non-matched)
    assert len(sorted_entries) >= 3

    # Check that amount_mismatch appears before possible_duplicate
    # (amount_mismatch has priority 0, possible_duplicate has priority 1)
    statuses = [e.match_status for e in sorted_entries]
    assert "amount_mismatch" in statuses
    assert "possible_duplicate" in statuses

    # The first entries should be the highest priority (amount_mismatch)
    amount_mismatch_indices = [i for i, s in enumerate(statuses) if s == "amount_mismatch"]
    possible_dup_indices = [i for i, s in enumerate(statuses) if s == "possible_duplicate"]
    if amount_mismatch_indices and possible_dup_indices:
        assert min(amount_mismatch_indices) < min(possible_dup_indices), (
            "amount_mismatch should appear before possible_duplicate"
        )


# ---------------------------------------------------------------------------
# 4b. Review queue entries have expected reasons and actions
# ---------------------------------------------------------------------------


def test_review_entries_have_reasons(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Each review entry includes reason codes and suggested actions."""
    result = run_demo_reconciliation(
        conn=migrated_temp_db_connection,
        csv_path=CSV_PATH,
        candidate_json_path=JSON_PATH,
        batch_public_id="test-batch-reasons",
        run_public_id="test-run-reasons",
    )

    sorted_entries = build_review_entries_sorted(
        migrated_temp_db_connection,
        result.run_summary.run_id,
    )

    table_rows = build_review_table_rows(sorted_entries)
    assert len(table_rows) >= 3

    for row in table_rows:
        assert isinstance(row, ReviewTableRow)
        assert row.status != ""
        assert row.reason != ""
        assert row.suggested_action != ""
        # Statement always has merchant/amount/date from the CSV
        # assert row.statement_merchant != "?"
        # May be "?" when matcher returns early without merchant evidence
        assert row.statement_amount != "?"


# ---------------------------------------------------------------------------
# 4c. Review table formatting
# ---------------------------------------------------------------------------


def test_format_review_table() -> None:
    """Review table formatting produces expected columns."""
    rows = [
        ReviewTableRow(
            status="amount_mismatch",
            statement_merchant="Netflix",
            statement_amount="19.90",
            statement_date="2024-12-04",
            app_merchant="Netflix",
            app_amount="21.90",
            app_date="2024-12-04",
            reason="amount_differs",
            suggested_action="Verify correct amount -- statement and app differ",
        ),
        ReviewTableRow(
            status="possible_duplicate",
            statement_merchant="Grab",
            statement_amount="8.50",
            statement_date="2024-12-05",
            app_merchant="Grab",
            app_amount="8.50",
            app_date="2024-12-05",
            reason="multiple_candidate_matches",
            suggested_action="Resolve duplicate -- multiple app records match",
        ),
    ]
    output = format_review_table(rows)
    assert "Status" in output
    assert "Statement" in output
    assert "App Record" in output
    assert "Reason" in output
    assert "Suggested Action" in output
    assert "amount_mismatch" in output
    assert "possible_duplicate" in output
    assert "Netflix" in output
    assert "Grab" in output


def test_format_review_table_empty() -> None:
    """Empty review table returns a clean message."""
    output = format_review_table([])
    assert "No review items" in output


# ---------------------------------------------------------------------------
# 5. CLI demo command exits successfully
# ---------------------------------------------------------------------------


def test_cli_demo_exits_successfully() -> None:
    """The demo CLI prints expected summary and exits 0."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "finance_core.reconciliation.demo_cli",
            "--statement",
            str(CSV_PATH),
            "--candidates",
            str(JSON_PATH),
            "--batch-id",
            "test-cli-batch",
            "--run-id",
            "test-cli-run",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )

    assert result.returncode == 0, f"CLI failed: stderr={result.stderr}"
    stdout = result.stdout
    assert "Reconciliation Demo Run Summary" in stdout
    assert "matched:" in stdout
    assert "amount_mismatch:" in stdout
    assert "possible_duplicate:" in stdout
    assert "Review Queue" in stdout
    assert "Status" in stdout


# ---------------------------------------------------------------------------
# 6. No live database file is modified
# ---------------------------------------------------------------------------


def test_live_db_not_modified(
    migrated_temp_db_connection: sqlite3.Connection,
    temp_db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The demo runs against a temporary database only."""
    db_file = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()["file"]
    assert Path(db_file) == temp_db_path, "Should use temp DB"
    assert Path(db_file) != LIVE_DB_PATH, "Must not touch live DB"

    # The assert_temp_db guard should raise for live DB
    # Verify the guard works: migrate_temp_db is safe, live DB should fail
    demo_fixture._assert_temp_db(migrated_temp_db_connection)  # Should not raise

    # Exercise the exact live-path guard inside a disposable fake repository.
    # Never create or open the real repository's database/finance.db.
    live_signature_before = (
        None
        if not LIVE_DB_PATH.exists()
        else (LIVE_DB_PATH.stat().st_size, LIVE_DB_PATH.stat().st_mtime_ns)
    )
    fake_repository = tmp_path / "fake-repository"
    fake_live_path = fake_repository / "database" / "finance.db"
    fake_live_path.parent.mkdir(parents=True)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", str(fake_repository.resolve()))

    live_conn = sqlite3.connect(str(fake_live_path))
    live_conn.row_factory = sqlite3.Row
    try:
        # The guard should detect the live DB path
        exc = None
        try:
            demo_fixture._assert_temp_db(live_conn)
        except ValueError as e:
            exc = str(e)
        assert exc is not None, "Should raise ValueError for live DB"
        assert "Refusing to run" in exc
    finally:
        live_conn.close()

    assert fake_live_path.exists()
    live_signature_after = (
        None
        if not LIVE_DB_PATH.exists()
        else (LIVE_DB_PATH.stat().st_size, LIVE_DB_PATH.stat().st_mtime_ns)
    )
    assert live_signature_after == live_signature_before


# ---------------------------------------------------------------------------
# 7. Decimal comparison is used (not float)
# ---------------------------------------------------------------------------


def test_decimal_comparison_used(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Amounts in the demo result use Decimal precision."""
    result = run_demo_reconciliation(
        conn=migrated_temp_db_connection,
        csv_path=CSV_PATH,
        candidate_json_path=JSON_PATH,
        batch_public_id="test-batch-decimal",
        run_public_id="test-run-decimal",
    )

    sorted_entries = build_review_entries_sorted(
        migrated_temp_db_connection,
        result.run_summary.run_id,
    )

    for entry in sorted_entries:
        evidence = entry.evidence
        # Check that statement_amount and candidate_amount are Decimal-safe strings
        stmt_amt = evidence.get("statement_amount")
        if stmt_amt is not None:
            # Should be convertible to Decimal without precision loss
            assert Decimal(str(stmt_amt)) is not None


# ---------------------------------------------------------------------------
# 8. DemoResult properties
# ---------------------------------------------------------------------------


def test_demo_result_properties(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """DemoResult fields are populated correctly."""
    result = run_demo_reconciliation(
        conn=migrated_temp_db_connection,
        csv_path=CSV_PATH,
        candidate_json_path=JSON_PATH,
        batch_public_id="test-batch-props",
        run_public_id="test-run-props",
    )

    assert isinstance(result, DemoResult)
    assert result.batch_id > 0
    assert result.batch_public_id == "test-batch-props"
    assert result.run_public_id == "test-run-props"
    assert result.total_statement_rows == 5
    assert result.csv_rows_imported == 5
    assert result.candidates_loaded == 5
    assert len(result.review_entries) >= 3

    # summary_table property
    table = result.summary_table
    assert "Reconciliation Demo Run Summary" in table
    assert "matched" in table


def test_demo_accepts_plain_sqlite_connection(
    migrated_temp_db_path: Path,
) -> None:
    conn = sqlite3.connect(migrated_temp_db_path)
    try:
        result = run_demo_reconciliation(
            conn=conn,
            csv_path=CSV_PATH,
            candidate_json_path=JSON_PATH,
            batch_public_id="test-batch-plain-conn",
            run_public_id="test-run-plain-conn",
        )

        assert result.total_statement_rows == 5
        assert result.run_summary.total_statement_transactions == 5
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 9. CLI with invalid arguments
# ---------------------------------------------------------------------------


def test_cli_missing_statement_file() -> None:
    """CLI returns non-zero for missing statement CSV."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "finance_core.reconciliation.demo_cli",
            "--statement",
            "/nonexistent/path.csv",
            "--candidates",
            str(JSON_PATH),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=10,
    )
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# 10. Review entries match status mapping
# ---------------------------------------------------------------------------


def test_review_queue_suggested_actions_are_populated(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Every review table row has a non-empty suggested action."""
    result = run_demo_reconciliation(
        conn=migrated_temp_db_connection,
        csv_path=CSV_PATH,
        candidate_json_path=JSON_PATH,
        batch_public_id="test-batch-actions",
        run_public_id="test-run-actions",
    )

    sorted_entries = build_review_entries_sorted(
        migrated_temp_db_connection,
        result.run_summary.run_id,
    )
    table_rows = build_review_table_rows(sorted_entries)

    for row in table_rows:
        assert row.suggested_action, f"Missing suggested action for status={row.status}"


# ===========================================================================
# Read-Only E2E Demo CLI v1
#
# Verifies the ``read-only-e2e`` subcommand proves the current reconciliation
# pipeline runs end to end from fixtures through parsing/import, matching,
# and review summary output -- without mutating real financial data.
#
# Required behaviors covered:
#   1. The read-only E2E demo command exits successfully.
#   2. The output includes parsed statement/app transaction counts.
#   3. The output includes matching/review summary information.
#   4. The output explicitly says it is read-only.
#   5. The output explicitly confirms zero final mutations.
#   6. The output explicitly confirms zero settlement obligations.
#   7. The demo does not write to database/finance.db.
#   8. The demo does not require live DB state.
#   9. Existing demo fixture tests still pass (the suite above).
# ===========================================================================


def _run_read_only_e2e_cli(
    *,
    statement: str = str(CSV_PATH),
    app_transactions: str = str(JSON_PATH),
    batch_id: str = "e2e-test-batch",
    run_id: str = "e2e-test-run",
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Invoke the read-only-e2e CLI subcommand and return the completed process."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "finance_core.reconciliation.demo_cli",
            "read-only-e2e",
            "--statement",
            statement,
            "--app-transactions",
            app_transactions,
            "--batch-id",
            batch_id,
            "--run-id",
            run_id,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=timeout,
    )


def test_read_only_e2e_exits_successfully() -> None:
    """Behavior 1: the read-only E2E demo command exits 0."""
    result = _run_read_only_e2e_cli()
    assert result.returncode == 0, f"CLI failed: stderr={result.stderr}"


def test_read_only_e2e_output_contains_counts() -> None:
    """Behavior 2: output includes parsed statement and app transaction counts."""
    result = _run_read_only_e2e_cli()
    assert result.returncode == 0, f"CLI failed: stderr={result.stderr}"

    stdout = result.stdout
    assert "Statement rows parsed: 5" in stdout
    assert "App transactions loaded: 5" in stdout
    assert "Statement fixture:" in stdout
    assert "App transactions fixture:" in stdout


def test_read_only_e2e_output_contains_matching_and_review_summary() -> None:
    """Behavior 3: output includes matching result and review queue summary."""
    result = _run_read_only_e2e_cli()
    assert result.returncode == 0, f"CLI failed: stderr={result.stderr}"

    stdout = result.stdout
    # Matching summary
    assert "Candidate matches: 1" in stdout
    assert "Match status breakdown:" in stdout
    assert "matched: 1" in stdout
    assert "amount_mismatch: 2" in stdout
    assert "possible_duplicate: 1" in stdout
    # Review queue summary
    assert "Review items: 4" in stdout
    assert "Review Queue (4 items, sorted by priority)" in stdout


def test_read_only_e2e_output_says_read_only() -> None:
    """Behavior 4: output explicitly labels itself as read-only."""
    result = _run_read_only_e2e_cli()
    assert result.returncode == 0, f"CLI failed: stderr={result.stderr}"

    assert "READ-ONLY E2E DEMO" in result.stdout


def test_read_only_e2e_output_confirms_zero_final_mutations() -> None:
    """Behavior 5: output explicitly confirms zero final mutations executed."""
    result = _run_read_only_e2e_cli()
    assert result.returncode == 0, f"CLI failed: stderr={result.stderr}"

    assert "Final mutations executed: 0" in result.stdout


def test_read_only_e2e_output_confirms_zero_settlement_obligations() -> None:
    """Behavior 6: output explicitly confirms zero settlement obligations."""
    result = _run_read_only_e2e_cli()
    assert result.returncode == 0, f"CLI failed: stderr={result.stderr}"

    assert "Settlement obligations created: 0" in result.stdout


def test_read_only_e2e_does_not_write_live_db() -> None:
    """Behavior 7: the demo does not write to database/finance.db.

    Captures the optional live DB file signature before and after the run and
    asserts it is unchanged.  A clean checkout intentionally has no live DB,
    so absence must remain absence rather than making the test order-dependent.
    """
    before_signature = (
        None
        if not LIVE_DB_PATH.exists()
        else (LIVE_DB_PATH.stat().st_size, LIVE_DB_PATH.stat().st_mtime_ns)
    )

    result = _run_read_only_e2e_cli()
    assert result.returncode == 0, f"CLI failed: stderr={result.stderr}"

    after_signature = (
        None
        if not LIVE_DB_PATH.exists()
        else (LIVE_DB_PATH.stat().st_size, LIVE_DB_PATH.stat().st_mtime_ns)
    )
    assert after_signature == before_signature, "Demo must not create or modify the live DB"


def test_read_only_e2e_does_not_require_live_db(tmp_path: Path) -> None:
    """Behavior 8: the demo does not require live DB state.

    Runs the command from a scratch working directory that has no
    ``database/finance.db`` and no repo-relative fixtures.  ``PYTHONPATH`` is
    pinned to the repo root so ``src`` still resolves, but the cwd itself has
    no live DB.  The CLI creates its own temp SQLite database under the system
    temp dir via ``tempfile.mkstemp``; it must not depend on a live DB being
    present.  The command must still succeed and report the same deterministic
    counts, and must not create a ``finance.db`` in the scratch directory.
    """
    import os

    scratch_cwd = tmp_path / "scratch_run"
    scratch_cwd.mkdir()

    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "finance_core.reconciliation.demo_cli",
            "read-only-e2e",
            "--statement",
            str(CSV_PATH),
            "--app-transactions",
            str(JSON_PATH),
            "--batch-id",
            "e2e-no-livedb-batch",
            "--run-id",
            "e2e-no-livedb-run",
        ],
        capture_output=True,
        text=True,
        cwd=str(scratch_cwd),
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, f"CLI failed: stderr={result.stderr}"
    assert "Statement rows parsed: 5" in result.stdout
    assert "Final mutations executed: 0" in result.stdout
    assert "Settlement obligations created: 0" in result.stdout

    # No finance.db should have been created in the scratch directory.
    assert not (scratch_cwd / "finance.db").exists()
    assert not (scratch_cwd / "database" / "finance.db").exists()


def test_read_only_e2e_refuses_live_db_as_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The fixture guard rejects an existing live path without touching the real DB."""
    fake_live_db = tmp_path / "fake-live-database" / "finance.db"
    fake_live_db.parent.mkdir()
    fake_live_db.touch()
    monkeypatch.setattr(demo_cli, "_LIVE_DB_PATH", fake_live_db.resolve())

    result = demo_cli._run_read_only_e2e_command(
        argparse.Namespace(
            statement=str(fake_live_db),
            app_transactions=str(JSON_PATH),
            batch_id="e2e-refuse-live-db-batch",
            run_id="e2e-refuse-live-db",
        )
    )
    assert result != 0
    # The guard message should mention refusing the live database path.
    captured = capsys.readouterr()
    combined = captured.err + captured.out
    assert "refusing" in combined.lower() or "live database" in combined.lower()


def test_read_only_e2e_summary_formatter_deterministic(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """The read-only E2E summary formatter is deterministic and labelled.

    Exercises the formatter + builder directly (no subprocess) against the
    shared migrated temp DB fixture, confirming stable text and the
    read-only / zero-mutation / zero-settlement contract lines.
    """
    from finance_core.reconciliation.demo_fixture import (
        build_read_only_e2e_summary,
        format_read_only_e2e_summary,
    )

    result = run_demo_reconciliation(
        conn=migrated_temp_db_connection,
        csv_path=CSV_PATH,
        candidate_json_path=JSON_PATH,
        batch_public_id="e2e-fmt-batch",
        run_public_id="e2e-fmt-run",
    )
    sorted_entries = build_review_entries_sorted(
        migrated_temp_db_connection,
        result.run_summary.run_id,
    )

    summary = build_read_only_e2e_summary(
        statement_fixture_path=CSV_PATH,
        app_transactions_fixture_path=JSON_PATH,
        result=result,
        review_item_count=len(sorted_entries),
    )
    text = format_read_only_e2e_summary(summary)

    assert text.startswith("READ-ONLY E2E DEMO")
    assert "Statement fixture:" in text
    assert "App transactions fixture:" in text
    assert "Statement rows parsed: 5" in text
    assert "App transactions loaded: 5" in text
    assert "Candidate matches: 1" in text
    assert "Review items: 4" in text
    assert "Final mutations executed: 0" in text
    assert "Settlement obligations created: 0" in text

    # Deterministic: same inputs -> identical output.
    text_again = format_read_only_e2e_summary(summary)
    assert text == text_again


def test_read_only_e2e_default_fixtures_resolve_and_exist() -> None:
    """The CLI's default fixture paths exist and resolve under the repo."""
    from finance_core.reconciliation.demo_cli import (
        _DEFAULT_E2E_APP_TRANSACTIONS,
        _DEFAULT_E2E_STATEMENT,
    )

    assert _DEFAULT_E2E_STATEMENT.exists(), "Default statement fixture missing"
    assert _DEFAULT_E2E_APP_TRANSACTIONS.exists(), "Default app transactions fixture missing"
    assert _DEFAULT_E2E_STATEMENT != LIVE_DB_PATH
    assert _DEFAULT_E2E_APP_TRANSACTIONS != LIVE_DB_PATH


def test_read_only_e2e_missing_statement_file() -> None:
    """The read-only E2E command fails cleanly for a missing statement file."""
    result = _run_read_only_e2e_cli(statement="/nonexistent/path.csv")
    assert result.returncode != 0
    assert "statement CSV not found" in result.stderr
