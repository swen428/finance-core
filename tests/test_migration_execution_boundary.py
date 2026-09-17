from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from finance_core.reconciliation.migrations import (
    MigrationExecutionError,
    apply_migration_path,
    apply_migration_paths,
)


def test_single_migration_rejects_an_active_caller_transaction(tmp_path: Path) -> None:
    migration_path = _migration_path(tmp_path)
    conn = _connection_with_pending_caller_work()
    try:
        with pytest.raises(MigrationExecutionError, match="active transaction"):
            apply_migration_path(conn, migration_path)

        assert conn.in_transaction is True
        assert conn.execute("SELECT id FROM caller_work").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'migration_owned'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


def test_migration_sequence_rejects_an_active_caller_transaction(tmp_path: Path) -> None:
    migration_path = _migration_path(tmp_path)
    conn = _connection_with_pending_caller_work()
    try:
        with pytest.raises(MigrationExecutionError, match="active transaction"):
            apply_migration_paths(conn, (migration_path,))

        assert conn.in_transaction is True
        assert conn.execute("SELECT id FROM caller_work").fetchone()[0] == 1
    finally:
        conn.close()


def _connection_with_pending_caller_work() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE caller_work (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.execute("INSERT INTO caller_work (id) VALUES (1)")
    return conn


def _migration_path(tmp_path: Path) -> Path:
    migration_path = tmp_path / "migration.sql"
    migration_path.write_text(
        "CREATE TABLE migration_owned (id INTEGER PRIMARY KEY);",
        encoding="utf-8",
    )
    return migration_path
