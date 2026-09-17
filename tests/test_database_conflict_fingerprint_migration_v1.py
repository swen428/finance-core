import sqlite3

from finance_core.reconciliation.migrations import (
    MIGRATION_022_DATABASE_CONFLICT_FINGERPRINTS,
    TEMP_DB_MIGRATION_PATHS,
    apply_migration_path,
    apply_migration_paths,
)


def test_actual_migration_022_upgrades_schema_021_without_integrity_errors() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS[:-1])
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS[-1:])

        raw_columns = {row["name"] for row in conn.execute("PRAGMA table_info(raw_intake_records)")}
        pdf_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(pdf_statement_import_runs)")
        }
        indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(pdf_statement_import_runs)")
        }
        assert {"content_fingerprint", "fingerprint_version"} <= raw_columns
        assert {"content_fingerprint", "fingerprint_version"} <= pdf_columns
        assert "idx_pdf_statement_import_runs_content_fingerprint" in indexes
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_migration_022_can_be_replayed_after_its_columns_exist() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
        apply_migration_path(conn, MIGRATION_022_DATABASE_CONFLICT_FINGERPRINTS)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
