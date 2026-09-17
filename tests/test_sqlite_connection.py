from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from finance_core.sqlite_connection import (
    ConnectionMode,
    ForeignKeysDisabledError,
    UnsupportedSQLiteConnectionInputError,
    configure_sqlite_connection,
    connect_sqlite,
    require_foreign_keys_enabled,
)


def test_file_connection_enforces_the_authoritative_contract(tmp_path: Path) -> None:
    conn = connect_sqlite(tmp_path / "connection-contract.sqlite")
    try:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_memory_connection_works_without_forcing_file_journal_settings() -> None:
    conn = connect_sqlite(":memory:")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "memory"
    finally:
        conn.close()


def test_foreign_key_violations_are_rejected(tmp_path: Path) -> None:
    conn = connect_sqlite(tmp_path / "foreign-keys.sqlite")
    try:
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE child (parent_id INTEGER NOT NULL REFERENCES parent(id))")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO child (parent_id) VALUES (42)")
    finally:
        conn.close()


def test_connection_setup_never_creates_application_schema_or_runs_migrations(
    tmp_path: Path,
) -> None:
    conn = connect_sqlite(tmp_path / "unmigrated.sqlite")
    try:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == []
    finally:
        conn.close()


def test_configuring_an_existing_connection_preserves_pending_transaction() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("CREATE TABLE pending_work (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.execute("INSERT INTO pending_work (id) VALUES (1)")

        configure_sqlite_connection(conn)

        assert conn.in_transaction is True
        assert conn.execute("SELECT id FROM pending_work").fetchone()[0] == 1
    finally:
        conn.close()


def test_foreign_keys_disabled_in_an_active_transaction_fails_closed() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE pending_work (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.execute("INSERT INTO pending_work (id) VALUES (1)")
        with pytest.raises(ForeignKeysDisabledError, match="foreign-key"):
            configure_sqlite_connection(conn)
        assert conn.in_transaction is True
    finally:
        conn.close()


def test_foreign_key_requirement_has_a_stable_failure() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(ForeignKeysDisabledError, match="foreign-key"):
            require_foreign_keys_enabled(conn)
    finally:
        conn.close()


def test_read_only_mode_uses_a_real_read_only_connection(tmp_path: Path) -> None:
    database = tmp_path / "read-only.sqlite"
    writable = connect_sqlite(database)
    try:
        writable.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")
        writable.commit()
    finally:
        writable.close()

    readonly = connect_sqlite(database, mode=ConnectionMode.READ_ONLY)
    try:
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute("INSERT INTO records (id) VALUES (1)")
    finally:
        readonly.close()


def test_read_only_uri_requires_an_exact_structural_mode_parameter(tmp_path: Path) -> None:
    database = tmp_path / "read-only-uri.sqlite"
    writable = connect_sqlite(database)
    writable.close()

    exact_uri = f"{database.resolve().as_uri()}?cache=shared&mode=ro"
    readonly = connect_sqlite(exact_uri, mode=ConnectionMode.READ_ONLY)
    try:
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute("CREATE TABLE rejected_write (id INTEGER PRIMARY KEY)")
    finally:
        readonly.close()


@pytest.mark.parametrize(
    "query",
    (
        "xmode=ro",
        "some_mode=ro",
        "mode=readonly",
        "mode=rotten",
        "mode=rw",
        "mode=ro&mode=ro",
        "mode=ro&mode=rw",
        "mode=rw&x=mode=ro",
        "mode",
    ),
)
def test_read_only_uri_rejects_misleading_or_conflicting_mode_parameters(
    tmp_path: Path,
    query: str,
) -> None:
    database = tmp_path / "invalid-read-only-uri.sqlite"
    writable = connect_sqlite(database)
    writable.close()

    with pytest.raises(UnsupportedSQLiteConnectionInputError, match="mode=ro|malformed"):
        connect_sqlite(f"{database.resolve().as_uri()}?{query}", mode=ConnectionMode.READ_ONLY)


def test_unsafe_connection_targets_fail_with_stable_error() -> None:
    with pytest.raises(UnsupportedSQLiteConnectionInputError, match="empty"):
        connect_sqlite("")
    with pytest.raises(UnsupportedSQLiteConnectionInputError, match="Read-only"):
        connect_sqlite(":memory:", mode=ConnectionMode.READ_ONLY)
