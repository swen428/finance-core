"""Migration 045 forward-safety and no-backfill contract."""

from __future__ import annotations

import sqlite3

from finance_core.reconciliation.migrations import (
    TEMP_DB_MIGRATION_PATHS,
    apply_migration_paths,
    migration_ledger_rows,
    verify_migration_history,
)
from tests.test_migration_042_s5e_ai_fallback_provenance_foundation_v1 import (
    insert_attempt,
    seed_parent,
)

PATHS_THROUGH_044 = TEMP_DB_MIGRATION_PATHS[:44]


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_fresh_045_apply_and_replay_are_deterministic() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
        first = migration_ledger_rows(conn)
        migration_045 = next(row for row in first if row["migration_id"] == "045")
        assert migration_045["migration_filename"] == ("045_nomi_ai_model_admission_decisions.sql")
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
        assert migration_ledger_rows(conn) == first
        verify_migration_history(conn, TEMP_DB_MIGRATION_PATHS)
    finally:
        conn.close()


def test_upgrade_from_044_preserves_attempt_without_inventing_decision() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, PATHS_THROUGH_044)
        parent_id, intake_id = seed_parent(conn, "upgrade_045")
        insert_attempt(conn, parent_id, intake_id, "8")
        conn.commit()

        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)

        assert conn.execute("SELECT COUNT(*) FROM ai_fallback_attempts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ai_model_admission_decisions").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        verify_migration_history(conn, TEMP_DB_MIGRATION_PATHS)
    finally:
        conn.close()
