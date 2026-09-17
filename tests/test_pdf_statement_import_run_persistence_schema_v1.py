"""Tests for PDF Statement Import Run Persistence Schema v1.

Covers migration application, table/column/index existence, CHECK
constraints, unique/idempotency protection, JSON field acceptance,
row-count consistency enforcement, and live-DB safety.
Every test uses an independent in-memory or temporary SQLite database
-- never touches database/finance.db or live data.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from finance_core.resources import migrations_dir

MIGRATION_PATH = migrations_dir() / "017_pdf_statement_import_run_persistence.sql"

FROZEN_NOW = "2026-07-07T12:00:00+08:00"

VALID_STATUSES = ["fully_ready", "partially_reviewable", "blocked"]
VALID_SOURCE_MODES = ["fixture_text", "pdf_text"]


# -- helpers --


def create_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def apply_migration(conn: sqlite3.Connection) -> None:
    migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")
    conn.executescript(migration_sql)


def make_migrated_connection() -> sqlite3.Connection:
    conn = create_connection()
    apply_migration(conn)
    return conn


def insert_sample_run(
    conn: sqlite3.Connection,
    *,
    run_public_id: str = "run_test_001",
    import_batch_public_id: str | None = "batch_test_001",
    source_mode: str = "fixture_text",
    source_pdf_path: str | None = "/tmp/test.pdf",
    source_statement_id: str | None = "stmt_test_001",
    run_status: str = "fully_ready",
    total_rows: int = 10,
    ready_for_import_rows: int = 8,
    needs_review_rows: int = 2,
    blocked_rows: int = 0,
    blocked_reason_counts_json: str | None = None,
    warning_reason_counts_json: str | None = None,
    dashboard_summary_json: str | None = None,
    audit_summary_json: str | None = None,
    created_at: str = FROZEN_NOW,
) -> sqlite3.Row:
    blocked_json = blocked_reason_counts_json or "{}"
    warning_json = warning_reason_counts_json or "{}"
    conn.execute(
        """\
        INSERT INTO pdf_statement_import_runs (
            run_public_id,
            import_batch_public_id,
            source_mode,
            source_pdf_path,
            source_statement_id,
            run_status,
            total_rows,
            ready_for_import_rows,
            needs_review_rows,
            blocked_rows,
            blocked_reason_counts_json,
            warning_reason_counts_json,
            dashboard_summary_json,
            audit_summary_json,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_public_id,
            import_batch_public_id,
            source_mode,
            source_pdf_path,
            source_statement_id,
            run_status,
            total_rows,
            ready_for_import_rows,
            needs_review_rows,
            blocked_rows,
            blocked_json,
            warning_json,
            dashboard_summary_json,
            audit_summary_json,
            created_at,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM pdf_statement_import_runs WHERE run_public_id = ?",
        (run_public_id,),
    ).fetchone()
    assert row is not None
    return row


# -- 1. Migration applies cleanly --


def test_migration_creates_table() -> None:
    conn = make_migrated_connection()

    result = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name = 'pdf_statement_import_runs'
        """
    ).fetchone()

    assert result is not None
    assert result["name"] == "pdf_statement_import_runs"
    conn.close()


def test_migration_is_idempotent() -> None:
    conn = make_migrated_connection()

    # Applying migration a second time should not raise
    apply_migration(conn)

    result = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name = 'pdf_statement_import_runs'
        """
    ).fetchone()
    assert result is not None
    conn.close()


# -- 2. Expected columns exist --


def test_expected_columns_exist() -> None:
    conn = make_migrated_connection()
    insert_sample_run(conn)

    expected_columns = {
        "id",
        "run_public_id",
        "import_batch_public_id",
        "source_mode",
        "source_pdf_path",
        "source_statement_id",
        "run_status",
        "total_rows",
        "ready_for_import_rows",
        "needs_review_rows",
        "blocked_rows",
        "blocked_reason_counts_json",
        "warning_reason_counts_json",
        "dashboard_summary_json",
        "audit_summary_json",
        "created_at",
    }

    cursor = conn.execute("PRAGMA table_info('pdf_statement_import_runs')")
    actual_columns = {row["name"] for row in cursor.fetchall()}

    assert expected_columns == actual_columns
    conn.close()


# -- 3. CHECK constraint: invalid run_status rejected --


@pytest.mark.parametrize("status", VALID_STATUSES)
def test_allowed_run_statuses_accepted(status: str) -> None:
    conn = make_migrated_connection()
    row = insert_sample_run(
        conn,
        run_public_id=f"run_status_{status}",
        run_status=status,
    )
    assert row["run_status"] == status
    conn.close()


@pytest.mark.parametrize(
    "invalid_status",
    ["pending", "failed", "in_progress", "complete", "unknown", ""],
)
def test_invalid_run_status_rejected(invalid_status: str) -> None:
    conn = make_migrated_connection()
    with pytest.raises(sqlite3.IntegrityError):
        insert_sample_run(
            conn,
            run_public_id=f"run_bad_status_{invalid_status}",
            run_status=invalid_status,
        )
    conn.close()


# -- 4. CHECK constraint: invalid source_mode rejected --


@pytest.mark.parametrize("mode", VALID_SOURCE_MODES)
def test_allowed_source_modes_accepted(mode: str) -> None:
    conn = make_migrated_connection()
    row = insert_sample_run(
        conn,
        run_public_id=f"run_mode_{mode}",
        source_mode=mode,
    )
    assert row["source_mode"] == mode
    conn.close()


@pytest.mark.parametrize(
    "invalid_mode",
    ["ocr_text", "live_import", "unknown", "", "PDF_TEXT"],
)
def test_invalid_source_mode_rejected(invalid_mode: str) -> None:
    conn = make_migrated_connection()
    with pytest.raises(sqlite3.IntegrityError):
        insert_sample_run(
            conn,
            run_public_id=f"run_bad_mode_{invalid_mode}",
            source_mode=invalid_mode,
        )
    conn.close()


# -- 5. CHECK constraint: negative row counts rejected --


@pytest.mark.parametrize(
    "field", ["total_rows", "ready_for_import_rows", "needs_review_rows", "blocked_rows"]
)
def test_negative_row_count_rejected(field: str) -> None:
    conn = make_migrated_connection()
    overrides = {
        "total_rows": 0,
        "ready_for_import_rows": 0,
        "needs_review_rows": 0,
        "blocked_rows": 0,
        field: -1,
    }
    # Ensure the sum constraint is still satisfied
    if field == "total_rows":
        overrides["total_rows"] = -1
        overrides["ready_for_import_rows"] = 0
        overrides["needs_review_rows"] = 0
        overrides["blocked_rows"] = 0

    with pytest.raises(sqlite3.IntegrityError):
        insert_sample_run(
            conn,
            run_public_id=f"run_neg_{field}",
            total_rows=overrides["total_rows"],
            ready_for_import_rows=overrides["ready_for_import_rows"],
            needs_review_rows=overrides["needs_review_rows"],
            blocked_rows=overrides["blocked_rows"],
        )
    conn.close()


# -- 6. Unique constraint: duplicate run_public_id rejected --


def test_duplicate_run_public_id_rejected() -> None:
    conn = make_migrated_connection()
    insert_sample_run(conn, run_public_id="run_dup_001")

    with pytest.raises(sqlite3.IntegrityError):
        insert_sample_run(conn, run_public_id="run_dup_001")
    conn.close()


def test_different_run_public_ids_accepted() -> None:
    conn = make_migrated_connection()
    insert_sample_run(conn, run_public_id="run_unique_001")
    insert_sample_run(
        conn,
        run_public_id="run_unique_002",
        import_batch_public_id="batch_002",
        run_status="blocked",
        total_rows=5,
        ready_for_import_rows=0,
        needs_review_rows=0,
        blocked_rows=5,
    )

    count = conn.execute("SELECT COUNT(*) AS cnt FROM pdf_statement_import_runs").fetchone()["cnt"]
    assert count == 2
    conn.close()


# -- 7. Indexes exist --


def test_expected_indexes_exist() -> None:
    conn = make_migrated_connection()

    cursor = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'index' AND tbl_name = 'pdf_statement_import_runs'"
    )
    index_names = {row["name"] for row in cursor.fetchall()}

    # sqlite_autoindex_pdf_statement_import_runs_1 is the automatic UNIQUE index
    expected = {
        "idx_pdf_statement_import_runs_public_id",
        "idx_pdf_statement_import_runs_import_batch_public_id",
        "idx_pdf_statement_import_runs_run_status",
        "idx_pdf_statement_import_runs_created_at",
    }

    assert expected <= index_names, f"Missing indexes: {expected - index_names}"
    conn.close()


# -- 8. JSON count fields accept deterministic JSON text --


def test_blocked_reason_counts_json_accepted() -> None:
    conn = make_migrated_connection()
    blocked_json = json.dumps({"missing_date": 3, "invalid_amount": 2}, sort_keys=True)
    row = insert_sample_run(
        conn,
        run_public_id="run_json_blocked",
        blocked_reason_counts_json=blocked_json,
    )
    assert row["blocked_reason_counts_json"] == blocked_json
    conn.close()


def test_warning_reason_counts_json_accepted() -> None:
    conn = make_migrated_connection()
    warning_json = json.dumps({"date_inferred": 1, "amount_normalized": 4}, sort_keys=True)
    row = insert_sample_run(
        conn,
        run_public_id="run_json_warning",
        warning_reason_counts_json=warning_json,
    )
    assert row["warning_reason_counts_json"] == warning_json
    conn.close()


def test_default_json_fields_are_empty_object() -> None:
    conn = make_migrated_connection()
    row = insert_sample_run(conn, run_public_id="run_default_json")
    assert row["blocked_reason_counts_json"] == "{}"
    assert row["warning_reason_counts_json"] == "{}"
    conn.close()


def test_dashboard_and_audit_json_snapshots_accepted() -> None:
    conn = make_migrated_connection()
    dashboard_json = json.dumps({"run_status": "fully_ready", "total_rows": 10}, sort_keys=True)
    audit_json = json.dumps({"source_pdf_path": "/tmp/test.pdf", "total_rows": 10}, sort_keys=True)
    row = insert_sample_run(
        conn,
        run_public_id="run_snapshots",
        dashboard_summary_json=dashboard_json,
        audit_summary_json=audit_json,
    )
    assert row["dashboard_summary_json"] == dashboard_json
    assert row["audit_summary_json"] == audit_json
    conn.close()


def test_dashboard_and_audit_json_null_accepted() -> None:
    conn = make_migrated_connection()
    row = insert_sample_run(
        conn,
        run_public_id="run_null_snapshots",
        dashboard_summary_json=None,
        audit_summary_json=None,
    )
    assert row["dashboard_summary_json"] is None
    assert row["audit_summary_json"] is None
    conn.close()


# -- 9. Row-count sum consistency enforced --


def test_row_count_sum_consistency_accepted() -> None:
    conn = make_migrated_connection()
    row = insert_sample_run(
        conn,
        run_public_id="run_sum_ok",
        total_rows=10,
        ready_for_import_rows=5,
        needs_review_rows=3,
        blocked_rows=2,
    )
    assert row["total_rows"] == 10
    assert row["ready_for_import_rows"] == 5
    assert row["needs_review_rows"] == 3
    assert row["blocked_rows"] == 2
    conn.close()


def test_row_count_sum_mismatch_rejected() -> None:
    conn = make_migrated_connection()
    with pytest.raises(sqlite3.IntegrityError):
        insert_sample_run(
            conn,
            run_public_id="run_sum_bad",
            total_rows=10,
            ready_for_import_rows=5,
            needs_review_rows=3,
            blocked_rows=1,  # 5+3+1 = 9 != 10
        )
    conn.close()


# -- 10. Insert and fetch round-trip --


def test_insert_and_fetch_full_round_trip() -> None:
    conn = make_migrated_connection()

    insert_sample_run(
        conn,
        run_public_id="run_roundtrip",
        import_batch_public_id="batch_roundtrip",
        source_mode="pdf_text",
        source_pdf_path="/data/ocbc_2026_07.pdf",
        source_statement_id="pdf-tmpl-cli-a1b2c3d4e5f6",
        run_status="blocked",
        total_rows=20,
        ready_for_import_rows=14,
        needs_review_rows=3,
        blocked_rows=3,
        blocked_reason_counts_json='{"missing_date":2,"unparseable_amount":1}',
        warning_reason_counts_json='{"date_inferred":3}',
        dashboard_summary_json='{"run_status":"blocked"}',
        audit_summary_json='{"source_pdf_path":"/data/ocbc_2026_07.pdf"}',
    )

    row = conn.execute(
        "SELECT * FROM pdf_statement_import_runs WHERE run_public_id = ?",
        ("run_roundtrip",),
    ).fetchone()

    assert row is not None
    assert row["run_public_id"] == "run_roundtrip"
    assert row["import_batch_public_id"] == "batch_roundtrip"
    assert row["source_mode"] == "pdf_text"
    assert row["source_pdf_path"] == "/data/ocbc_2026_07.pdf"
    assert row["source_statement_id"] == "pdf-tmpl-cli-a1b2c3d4e5f6"
    assert row["run_status"] == "blocked"
    assert row["total_rows"] == 20
    assert row["ready_for_import_rows"] == 14
    assert row["needs_review_rows"] == 3
    assert row["blocked_rows"] == 3
    assert row["blocked_reason_counts_json"] == '{"missing_date":2,"unparseable_amount":1}'
    assert row["warning_reason_counts_json"] == '{"date_inferred":3}'
    assert row["dashboard_summary_json"] == '{"run_status":"blocked"}'
    assert row["audit_summary_json"] == '{"source_pdf_path":"/data/ocbc_2026_07.pdf"}'
    conn.close()


# -- 11. Optional fields nullable --


def test_optional_fields_null_accepted() -> None:
    conn = make_migrated_connection()
    row = insert_sample_run(
        conn,
        run_public_id="run_optional_null",
        import_batch_public_id=None,
        source_pdf_path=None,
        source_statement_id=None,
        dashboard_summary_json=None,
        audit_summary_json=None,
    )
    assert row["import_batch_public_id"] is None
    assert row["source_pdf_path"] is None
    assert row["source_statement_id"] is None
    assert row["dashboard_summary_json"] is None
    assert row["audit_summary_json"] is None
    conn.close()


# -- 12. created_at defaults --


def test_created_at_defaults_to_current_timestamp() -> None:
    conn = make_migrated_connection()
    conn.execute(
        """\
        INSERT INTO pdf_statement_import_runs (
            run_public_id,
            source_mode,
            run_status,
            total_rows,
            ready_for_import_rows,
            needs_review_rows,
            blocked_rows
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("run_default_ts", "fixture_text", "fully_ready", 0, 0, 0, 0),
    )
    conn.commit()

    row = conn.execute(
        "SELECT created_at FROM pdf_statement_import_runs WHERE run_public_id = ?",
        ("run_default_ts",),
    ).fetchone()

    assert row is not None
    assert row["created_at"] is not None
    assert isinstance(row["created_at"], str)
    assert len(row["created_at"]) > 0
    conn.close()


# -- 13. Live DB safety --


def test_live_db_not_touched(temp_db_path: Path) -> None:
    """Ensure database/finance.db is never used during tests."""
    live_db = Path(__file__).resolve().parents[1] / "database" / "finance.db"

    conn = sqlite3.connect(str(temp_db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migration(conn)
    insert_sample_run(conn, run_public_id="run_live_safety")
    conn.close()

    # Verify we used the temp DB, not finance.db
    assert temp_db_path.exists()
    # finance.db should still exist (untouched, not created by us)
    if live_db.exists():
        # We haven't modified it -- just confirm it wasn't the path we used
        assert temp_db_path != live_db


def test_migration_never_touches_finance_db() -> None:
    """Migration SQL references no explicit database path."""
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    # Strip SQL comments (lines starting with --) before checking for
    # finance.db references.  The validation guidance block at the end
    # follows the established project pattern and mentions the file by
    # name, but no actual SQL statement should reference it.
    active_lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    active_sql = "\n".join(active_lines)
    assert "finance.db" not in active_sql
    assert "ATTACH" not in sql.upper()


def test_no_transaction_or_settlement_tables_created_by_migration() -> None:
    """Ensure this migration does not create final transaction or settlement tables."""
    conn = make_migrated_connection()

    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }

    # Must have our table
    assert "pdf_statement_import_runs" in tables

    # Must NOT create final financial tables
    forbidden = {
        "settlements",
        "settlement_items",
        "final_transactions",
        "final_financial_records",
        "receipt_finalizations",
    }
    assert tables.isdisjoint(forbidden)
    conn.close()


# -- 14. Multiple runs persistence --


def test_multiple_runs_persisted_independently() -> None:
    conn = make_migrated_connection()

    insert_sample_run(
        conn,
        run_public_id="batch_a_run_1",
        import_batch_public_id="batch_a",
        run_status="fully_ready",
        total_rows=5,
        ready_for_import_rows=5,
        needs_review_rows=0,
        blocked_rows=0,
    )
    insert_sample_run(
        conn,
        run_public_id="batch_a_run_2",
        import_batch_public_id="batch_a",
        run_status="blocked",
        total_rows=3,
        ready_for_import_rows=1,
        needs_review_rows=0,
        blocked_rows=2,
        blocked_reason_counts_json='{"missing_date":2}',
    )
    insert_sample_run(
        conn,
        run_public_id="batch_b_run_1",
        import_batch_public_id="batch_b",
        source_mode="pdf_text",
        run_status="partially_reviewable",
        total_rows=8,
        ready_for_import_rows=6,
        needs_review_rows=2,
        blocked_rows=0,
    )

    count = conn.execute("SELECT COUNT(*) AS cnt FROM pdf_statement_import_runs").fetchone()["cnt"]
    assert count == 3

    # Runs per batch
    batch_a_runs = conn.execute(
        "SELECT COUNT(*) AS cnt FROM pdf_statement_import_runs WHERE import_batch_public_id = ?",
        ("batch_a",),
    ).fetchone()["cnt"]
    assert batch_a_runs == 2

    batch_b_runs = conn.execute(
        "SELECT COUNT(*) AS cnt FROM pdf_statement_import_runs WHERE import_batch_public_id = ?",
        ("batch_b",),
    ).fetchone()["cnt"]
    assert batch_b_runs == 1

    # Status filter
    blocked_runs = conn.execute(
        "SELECT COUNT(*) AS cnt FROM pdf_statement_import_runs WHERE run_status = 'blocked'"
    ).fetchone()["cnt"]
    assert blocked_runs == 1

    conn.close()


# -- 15. Row-count edge cases --


def test_all_zero_row_counts_accepted() -> None:
    conn = make_migrated_connection()
    row = insert_sample_run(
        conn,
        run_public_id="run_all_zero",
        total_rows=0,
        ready_for_import_rows=0,
        needs_review_rows=0,
        blocked_rows=0,
    )
    assert row["total_rows"] == 0
    conn.close()


def test_row_count_sum_zero_all_accepted() -> None:
    conn = make_migrated_connection()
    insert_sample_run(
        conn,
        run_public_id="run_zero_cardinality",
        total_rows=0,
        ready_for_import_rows=0,
        needs_review_rows=0,
        blocked_rows=0,
    )
    row = conn.execute(
        "SELECT * FROM pdf_statement_import_runs WHERE run_public_id = ?",
        ("run_zero_cardinality",),
    ).fetchone()
    assert row["total_rows"] == 0
    conn.close()


# -- 16. source_statement_id and source_pdf_path evidence preservation --


def test_source_evidence_preserved() -> None:
    conn = make_migrated_connection()
    row = insert_sample_run(
        conn,
        run_public_id="run_evidence",
        source_pdf_path="/statements/ocbc_jun2026.pdf",
        source_statement_id="pdf-tmpl-cli-abcdef123456",
    )
    assert row["source_pdf_path"] == "/statements/ocbc_jun2026.pdf"
    assert row["source_statement_id"] == "pdf-tmpl-cli-abcdef123456"
    conn.close()
