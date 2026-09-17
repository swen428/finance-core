"""Tests for Metabase Read-only SQL Pack v1.

Validates that every SQL file in docs/reporting/sql/ is read-only,
parses successfully against a temp SQLite DB seeded by the PDF
statement review queue bridge with persistence, and returns expected
columns and data shapes.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from finance_core.reconciliation.migrations import LIVE_DB_PATH
from finance_core.reconciliation.pdf_statement_review_queue_bridge import (
    run_pdf_statement_review_queue_bridge,
)
from finance_core.reconciliation.pdf_statement_temp_db_import_fixture import (
    DEFAULT_PDF_FIXTURE_PATH,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = REPO_ROOT / "docs" / "reporting" / "sql"
LIVE_DB_PATH_RESOLVED = LIVE_DB_PATH.resolve()

WRITE_RISK_KEYWORDS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "ATTACH",
    "DETACH",
    "REINDEX",
    "VACUUM",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _discover_sql_files() -> list[Path]:
    """Return alphabetically sorted list of .sql files in docs/reporting/sql/."""
    assert SQL_DIR.is_dir(), f"SQL directory not found: {SQL_DIR}"
    files = sorted(SQL_DIR.glob("*.sql"))
    assert len(files) > 0, f"No .sql files found in {SQL_DIR}"
    return files


def _read_sql(path: Path) -> str:
    """Read a SQL file's full text."""
    return path.read_text(encoding="utf-8")


def _strip_sql_comments(sql: str) -> str:
    """Remove single-line (--) comments and multi-line (/* */) comments.

    This is a best-effort strip for keyword scanning.  It does not handle
    comment-like tokens inside string literals.
    """
    # Remove multi-line comments first
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    # Remove single-line comments (from -- to end of line)
    sql = re.sub(r"--[^\n]*", "", sql)
    return sql


def _read_only_audit(path: Path, sql_text: str) -> None:
    """Assert the SQL file is read-only (no write-risk keywords outside comments)."""
    stripped = _strip_sql_comments(sql_text)
    upper = stripped.upper()
    for kw in WRITE_RISK_KEYWORDS:
        assert kw not in upper, f"Write-risk keyword '{kw}' found in {path.name} (outside comments)"


def _ensure_columns(
    rows: list[sqlite3.Row],
    expected_cols: tuple[str, ...],
    sql_file_name: str,
) -> None:
    """Assert that at least one row has all expected column keys."""
    assert len(rows) >= 1, f"Expected at least one row from {sql_file_name}, got 0"
    first = rows[0]
    for col in expected_cols:
        assert col in first.keys(), (
            f"Expected column '{col}' in {sql_file_name} result, got columns: {list(first.keys())}"
        )


def _seed_temp_db(db_path: Path) -> str:
    """Seed a temp DB using the bridge with persistence enabled.

    Returns the resolved DB path as a string.
    """
    result = run_pdf_statement_review_queue_bridge(
        db_path=db_path,
        persist_review_queue=True,
    )
    # Sanity: persisted review queue must have at least one non-matched item.
    # The Grocer row (45.67) has no matching app transaction, so the
    # deterministic matcher flags it as amount_mismatch in the review queue.
    assert result.persisted_count >= 1, (
        "Fixture bridge must produce at least one persisted review queue item"
    )
    conn = sqlite3.connect(str(db_path.resolve()))
    conn.row_factory = sqlite3.Row
    try:
        non_matched = conn.execute(
            "SELECT COUNT(*) AS cnt FROM reconciliation_review_queue WHERE issue_type != 'matched'"
        ).fetchone()["cnt"]
    finally:
        conn.close()
    assert non_matched >= 1, "Persisted review queue must have at least one non-matched item"
    return str(db_path.resolve())


# ---------------------------------------------------------------------------
# Fixture: temp DB seeded via bridge
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded_db_path(tmp_path: Path) -> str:
    """Create a temp DB, seed it with bridge data, and return the path."""
    db_file = tmp_path / "metabase_sql_pack_test.sqlite"
    # Safety: must be inside tmp_path, not the live DB
    assert db_file.resolve() != LIVE_DB_PATH_RESOLVED, (
        "Temp DB resolved to live database/finance.db -- aborting"
    )
    assert db_file.is_relative_to(tmp_path), f"Temp DB {db_file} must be under tmp_path {tmp_path}"
    return _seed_temp_db(db_file)


# ---------------------------------------------------------------------------
# Test: SQL file discovery
# ---------------------------------------------------------------------------


class TestSqlFileDiscovery:
    """All SQL files in docs/reporting/sql/ are discovered."""

    def test_all_sql_files_discovered(self) -> None:
        files = _discover_sql_files()
        names = {f.name for f in files}
        expected = {
            "statement_import_batches_summary_v1.sql",
            "statement_transactions_recent_v1.sql",
            "reconciliation_review_required_summary_v1.sql",
            "reconciliation_matched_vs_unmatched_v1.sql",
            "source_evidence_audit_v1.sql",
        }
        assert names == expected, f"Expected SQL files {expected}, got {names}"

    def test_sql_dir_contains_only_sql_files(self) -> None:
        found = list(SQL_DIR.iterdir())
        for f in found:
            assert f.suffix == ".sql", f"Non-SQL file found in {SQL_DIR}: {f.name}"


# ---------------------------------------------------------------------------
# Test: read-only keyword audit
# ---------------------------------------------------------------------------


class TestReadOnlyAudit:
    """Every SQL file is read-only (no write-risk keywords)."""

    @pytest.mark.parametrize("sql_path", _discover_sql_files(), ids=lambda p: p.name)
    def test_sql_file_is_read_only(self, sql_path: Path) -> None:
        text = _read_sql(sql_path)
        _read_only_audit(sql_path, text)


# ---------------------------------------------------------------------------
# Test: SQL parsing and execution against seeded temp DB
# ---------------------------------------------------------------------------


class TestSqlParseAndExecute:
    """Every SQL file parses and executes successfully."""

    @pytest.mark.parametrize("sql_path", _discover_sql_files(), ids=lambda p: p.name)
    def test_sql_parses_and_executes(self, sql_path: Path, seeded_db_path: str) -> None:
        text = _read_sql(sql_path)
        conn = sqlite3.connect(seeded_db_path)
        conn.row_factory = sqlite3.Row
        try:
            # Parse-only first (EXPLAIN confirms SQLite sees it)
            conn.execute(f"EXPLAIN {text}")
            # Actually execute the query
            rows = conn.execute(text).fetchall()
            # Each query should return at least one row
            assert len(rows) >= 1, (
                f"SQL file {sql_path.name} returned 0 rows against seeded temp DB"
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Test: expected columns
# ---------------------------------------------------------------------------


class TestExpectedColumns:
    """Each query exposes the expected column names."""

    def test_statement_import_batches_summary_columns(self, seeded_db_path: str) -> None:
        conn = sqlite3.connect(seeded_db_path)
        conn.row_factory = sqlite3.Row
        try:
            sql = _read_sql(SQL_DIR / "statement_import_batches_summary_v1.sql")
            rows = conn.execute(sql).fetchall()
            _ensure_columns(
                rows,
                (
                    "batch_public_id",
                    "source_type",
                    "account_name",
                    "currency",
                    "source_file_path",
                    "imported_at",
                    "transaction_count",
                    "total_amount",
                    "earliest_transaction_date",
                    "latest_transaction_date",
                ),
                "statement_import_batches_summary_v1.sql",
            )
        finally:
            conn.close()

    def test_statement_transactions_recent_columns(self, seeded_db_path: str) -> None:
        conn = sqlite3.connect(seeded_db_path)
        conn.row_factory = sqlite3.Row
        try:
            sql = _read_sql(SQL_DIR / "statement_transactions_recent_v1.sql")
            rows = conn.execute(sql).fetchall()
            _ensure_columns(
                rows,
                (
                    "public_id",
                    "transaction_date",
                    "merchant_raw",
                    "amount",
                    "currency",
                    "account_name",
                    "statement_row_reference",
                    "batch_public_id",
                    "source_type",
                    "source_file_path",
                ),
                "statement_transactions_recent_v1.sql",
            )
        finally:
            conn.close()

    def test_reconciliation_review_required_summary_columns(self, seeded_db_path: str) -> None:
        conn = sqlite3.connect(seeded_db_path)
        conn.row_factory = sqlite3.Row
        try:
            sql = _read_sql(SQL_DIR / "reconciliation_review_required_summary_v1.sql")
            rows = conn.execute(sql).fetchall()
            _ensure_columns(
                rows,
                (
                    "issue_type",
                    "priority",
                    "suggested_action",
                    "item_count",
                    "statement_refs",
                    "app_refs",
                ),
                "reconciliation_review_required_summary_v1.sql",
            )
        finally:
            conn.close()

    def test_reconciliation_matched_vs_unmatched_columns(self, seeded_db_path: str) -> None:
        conn = sqlite3.connect(seeded_db_path)
        conn.row_factory = sqlite3.Row
        try:
            sql = _read_sql(SQL_DIR / "reconciliation_matched_vs_unmatched_v1.sql")
            rows = conn.execute(sql).fetchall()
            _ensure_columns(
                rows,
                (
                    "batch_public_id",
                    "source_type",
                    "account_name",
                    "currency",
                    "source_file_path",
                    "total_review_items",
                    "matched_count",
                    "unmatched_count",
                    "high_priority_count",
                    "medium_priority_count",
                    "low_priority_count",
                ),
                "reconciliation_matched_vs_unmatched_v1.sql",
            )
        finally:
            conn.close()

    def test_source_evidence_audit_columns(self, seeded_db_path: str) -> None:
        conn = sqlite3.connect(seeded_db_path)
        conn.row_factory = sqlite3.Row
        try:
            sql = _read_sql(SQL_DIR / "source_evidence_audit_v1.sql")
            rows = conn.execute(sql).fetchall()
            _ensure_columns(
                rows,
                (
                    "statement_public_id",
                    "transaction_date",
                    "merchant_raw",
                    "amount",
                    "currency",
                    "statement_row_reference",
                    "batch_public_id",
                    "source_type",
                    "source_file_path",
                    "imported_at",
                    "source_evidence_status",
                ),
                "source_evidence_audit_v1.sql",
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Test: synthetic aggregate counts
# ---------------------------------------------------------------------------


class TestSyntheticAggregateCounts:
    """Verify aggregate counts match known seeded data."""

    def test_import_batches_summary_has_expected_aggregates(self, seeded_db_path: str) -> None:
        conn = sqlite3.connect(seeded_db_path)
        conn.row_factory = sqlite3.Row
        try:
            sql = _read_sql(SQL_DIR / "statement_import_batches_summary_v1.sql")
            rows = conn.execute(sql).fetchall()
            assert len(rows) == 1, f"Expected exactly 1 batch summary row, got {len(rows)}"
            row = rows[0]
            # Seeded bridge creates exactly 3 statement transactions
            assert row["transaction_count"] == 3, (
                f"Expected 3 transactions, got {row['transaction_count']}"
            )
            # source_file_path must be present
            assert row["source_file_path"], "Expected non-empty source_file_path"
        finally:
            conn.close()

    def test_recent_transactions_has_3_rows(self, seeded_db_path: str) -> None:
        conn = sqlite3.connect(seeded_db_path)
        conn.row_factory = sqlite3.Row
        try:
            sql = _read_sql(SQL_DIR / "statement_transactions_recent_v1.sql")
            rows = conn.execute(sql).fetchall()
            assert len(rows) == 3, f"Expected 3 recent transactions, got {len(rows)}"
        finally:
            conn.close()

    def test_matched_vs_unmatched_has_expected_counts(self, seeded_db_path: str) -> None:
        conn = sqlite3.connect(seeded_db_path)
        conn.row_factory = sqlite3.Row
        try:
            sql = _read_sql(SQL_DIR / "reconciliation_matched_vs_unmatched_v1.sql")
            rows = conn.execute(sql).fetchall()
            assert len(rows) == 1, f"Expected exactly 1 matched/unmatched row, got {len(rows)}"
            row = rows[0]
            # 3 total review items (2 matched + 1 amount_mismatch)
            assert row["total_review_items"] == 3, (
                f"Expected 3 total_review_items, got {row['total_review_items']}"
            )
            assert row["matched_count"] >= 1, (
                f"Expected at least 1 matched, got {row['matched_count']}"
            )
            assert row["unmatched_count"] >= 1, (
                f"Expected at least 1 unmatched, got {row['unmatched_count']}"
            )
            assert row["matched_count"] + row["unmatched_count"] == 3, (
                "Sum of matched + unmatched must equal 3"
            )
        finally:
            conn.close()

    def test_matched_vs_unmatched_stays_scoped_per_batch(self, tmp_path: Path) -> None:
        """Rows with the same source row refs in different batches must not cross-join."""
        db_path = tmp_path / "metabase_two_batches.sqlite"
        pdf_a = tmp_path / "statement_a.pdf"
        pdf_b = tmp_path / "statement_b.pdf"
        shutil.copyfile(DEFAULT_PDF_FIXTURE_PATH, pdf_a)
        shutil.copyfile(DEFAULT_PDF_FIXTURE_PATH, pdf_b)
        pdf_b.write_bytes(pdf_b.read_bytes() + b"\n% synthetic distinct statement B\n")

        run_pdf_statement_review_queue_bridge(
            db_path=db_path,
            pdf_path=pdf_a,
            batch_public_id="batch-a",
            persist_review_queue=True,
            persistence_run_public_id="run-a",
        )
        run_pdf_statement_review_queue_bridge(
            db_path=db_path,
            pdf_path=pdf_b,
            batch_public_id="batch-b",
            persist_review_queue=True,
            persistence_run_public_id="run-b",
        )

        conn = sqlite3.connect(str(db_path.resolve()))
        conn.row_factory = sqlite3.Row
        try:
            sql = _read_sql(SQL_DIR / "reconciliation_matched_vs_unmatched_v1.sql")
            rows = conn.execute(sql).fetchall()
            assert len(rows) == 2, f"Expected 2 batch rows, got {len(rows)}"
            counts = {row["batch_public_id"]: row["total_review_items"] for row in rows}
            assert counts == {"batch-a": 3, "batch-b": 3}
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Test: review-required items
# ---------------------------------------------------------------------------


class TestReviewRequiredItems:
    """Review-required query returns expected grouping and counts."""

    def test_review_required_summary_has_at_least_one_group(self, seeded_db_path: str) -> None:
        conn = sqlite3.connect(seeded_db_path)
        conn.row_factory = sqlite3.Row
        try:
            sql = _read_sql(SQL_DIR / "reconciliation_review_required_summary_v1.sql")
            rows = conn.execute(sql).fetchall()
            assert len(rows) >= 1, f"Expected at least one review-required group, got {len(rows)}"
            # The Grocer row has amount_mismatch, which is not 'matched'
            issue_types = {r["issue_type"] for r in rows}
            assert "matched" not in issue_types, (
                "'matched' entries must be excluded from review-required summary"
            )
            # There must be at least one review-required item total
            total_items = sum(r["item_count"] for r in rows)
            assert total_items >= 1, (
                f"Expected at least one review-required item, got {total_items}"
            )
        finally:
            conn.close()

    def test_review_required_includes_statement_ref(self, seeded_db_path: str) -> None:
        conn = sqlite3.connect(seeded_db_path)
        conn.row_factory = sqlite3.Row
        try:
            sql = _read_sql(SQL_DIR / "reconciliation_review_required_summary_v1.sql")
            rows = conn.execute(sql).fetchall()
            for row in rows:
                refs = row["statement_refs"] or ""
                assert refs, f"Expected non-empty statement_refs for issue_type={row['issue_type']}"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Test: source evidence audit
# ---------------------------------------------------------------------------


class TestSourceEvidenceAudit:
    """Source evidence query returns expected columns and rows."""

    def test_source_evidence_has_expected_rows(self, seeded_db_path: str) -> None:
        conn = sqlite3.connect(seeded_db_path)
        conn.row_factory = sqlite3.Row
        try:
            sql = _read_sql(SQL_DIR / "source_evidence_audit_v1.sql")
            rows = conn.execute(sql).fetchall()
            assert len(rows) == 3, f"Expected 3 source evidence rows, got {len(rows)}"
        finally:
            conn.close()

    def test_source_evidence_status_is_file_available(self, seeded_db_path: str) -> None:
        conn = sqlite3.connect(seeded_db_path)
        conn.row_factory = sqlite3.Row
        try:
            sql = _read_sql(SQL_DIR / "source_evidence_audit_v1.sql")
            rows = conn.execute(sql).fetchall()
            for row in rows:
                assert row["source_evidence_status"] == "source_file_available", (
                    f"Expected source_file_available, "
                    f"got {row['source_evidence_status']} for "
                    f"{row.get('merchant_raw')}"
                )
        finally:
            conn.close()

    def test_source_evidence_has_source_file_path(self, seeded_db_path: str) -> None:
        conn = sqlite3.connect(seeded_db_path)
        conn.row_factory = sqlite3.Row
        try:
            sql = _read_sql(SQL_DIR / "source_evidence_audit_v1.sql")
            rows = conn.execute(sql).fetchall()
            for row in rows:
                path_val = row["source_file_path"]
                assert path_val, "Every source evidence row must have non-empty source_file_path"
                assert "pdf" in str(path_val).lower() or ".pdf" in str(path_val).lower(), (
                    f"source_file_path should reference a PDF file, got: {path_val}"
                )
        finally:
            conn.close()

    def test_source_evidence_includes_merchant_data(self, seeded_db_path: str) -> None:
        conn = sqlite3.connect(seeded_db_path)
        conn.row_factory = sqlite3.Row
        try:
            sql = _read_sql(SQL_DIR / "source_evidence_audit_v1.sql")
            rows = conn.execute(sql).fetchall()
            merchants = {r["merchant_raw"] for r in rows}
            assert "CoffeeShop" in merchants, f"Expected CoffeeShop in merchants, got {merchants}"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Test: live DB safety
# ---------------------------------------------------------------------------


class TestLiveDbSafety:
    """Tests must fail if a live DB path is used."""

    def test_seeded_db_is_not_live_db(self, seeded_db_path: str) -> None:
        resolved = Path(seeded_db_path).resolve()
        assert resolved != LIVE_DB_PATH_RESOLVED, f"seeded_db_path resolved to live DB: {resolved}"

    def test_discovered_sql_files_do_not_reference_finance_db(self) -> None:
        for sql_path in _discover_sql_files():
            text = _read_sql(sql_path)
            assert "finance.db" not in text, f"{sql_path.name} references finance.db"
            assert "database/finance" not in text, f"{sql_path.name} references database/finance"

    def test_connects_only_to_temp_path(self, seeded_db_path: str) -> None:
        resolved = Path(seeded_db_path).resolve()
        assert resolved != LIVE_DB_PATH_RESOLVED
        # The path must not live under database/
        db_dir = LIVE_DB_PATH_RESOLVED.parent
        assert not str(resolved).startswith(str(db_dir)), (
            f"seeded DB path is under database/ dir: {resolved}"
        )


# ---------------------------------------------------------------------------
# Test: temp file safety
# ---------------------------------------------------------------------------


class TestTempFileSafety:
    """No test writes outside tmp_path."""

    def test_temp_db_under_pytest_tmp_path(self, seeded_db_path: str, tmp_path: Path) -> None:
        resolved = Path(seeded_db_path).resolve()
        assert resolved.is_relative_to(tmp_path), (
            f"Temp DB {resolved} is not under tmp_path {tmp_path}"
        )

    def test_temp_db_uses_pytest_tmp_path_regardless_of_fixture(self, tmp_path: Path) -> None:
        """Ensure the tmp_path stays absolute and writable with the correct root."""
        db = tmp_path / "explicit_temp.sqlite"
        _seed_temp_db(db)
        resolved = db.resolve()
        assert resolved.is_relative_to(tmp_path), f"{resolved} must be under tmp_path {tmp_path}"
        assert db.exists(), f"Temp DB was not created: {db}"
        conn = sqlite3.connect(str(resolved))
        try:
            row = conn.execute("SELECT COUNT(*) FROM statement_transactions").fetchone()
            assert row[0] >= 1
        finally:
            conn.close()
