"""Authoritative SQLite connection policy for Finance application code.

The caller owns the returned connection and must close it.  This module never
commits, rolls back, begins a transaction, creates schema, or runs migrations.
It supports ordinary filesystem paths, ``:memory:``, and explicit SQLite URI
paths (including read-only ``file:...?mode=ro`` URIs).

Every configured connection uses ``sqlite3.Row``, a five-second busy timeout,
and verified foreign-key enforcement.  Journal mode and synchronous are
deliberately left unchanged: changing either can persist state on file-backed
databases, is not valid for every in-memory database, and is not authorised by
every caller.  Migration code owns schema changes separately.
"""

from __future__ import annotations

import sqlite3
from enum import Enum
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0
DEFAULT_BUSY_TIMEOUT_MILLISECONDS = int(DEFAULT_BUSY_TIMEOUT_SECONDS * 1000)


class ConnectionMode(str, Enum):
    """Declared purpose of a connection; no mode runs migrations implicitly."""

    APPLICATION = "application"
    MIGRATION = "migration"
    READ_ONLY = "read_only"


class SQLiteConnectionError(RuntimeError):
    """Stable base error for an unsatisfied Finance SQLite connection contract."""


class ForeignKeysDisabledError(SQLiteConnectionError):
    """Raised when a connection cannot prove foreign-key enforcement is active."""


class SQLiteConnectionConfigurationError(SQLiteConnectionError):
    """Raised when required connection configuration cannot be applied or verified."""


class UnsupportedSQLiteConnectionInputError(SQLiteConnectionError):
    """Raised for unsafe or unsupported connection targets."""


def connect_sqlite(
    database: str | Path,
    *,
    mode: ConnectionMode = ConnectionMode.APPLICATION,
    timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
) -> sqlite3.Connection:
    """Open and configure a caller-owned SQLite connection.

    ``database`` may be a filesystem path, ``:memory:``, or a ``file:`` URI.
    URI targets must carry their own access mode; callers use
    ``ConnectionMode.READ_ONLY`` to document read-only intent.  The mode does
    not alter schema, journal mode, synchronous, or transaction state.
    """
    _validate_target(database, mode=mode, timeout_seconds=timeout_seconds)
    target = _connection_target(database, mode=mode)
    uses_uri = target.startswith("file:")
    try:
        conn = sqlite3.connect(target, timeout=timeout_seconds, uri=uses_uri)
    except sqlite3.Error as exc:
        raise SQLiteConnectionConfigurationError(
            "Unable to open a SQLite connection that satisfies the Finance connection contract."
        ) from exc

    try:
        configure_sqlite_connection(conn, timeout_seconds=timeout_seconds)
    except Exception:
        conn.close()
        raise
    return conn


def configure_sqlite_connection(
    conn: sqlite3.Connection,
    *,
    timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
) -> None:
    """Configure a newly opened connection without committing or rolling back.

    This is intentionally reusable by the staging guard after it has safely
    performed exclusive creation and inode verification.  Configuring foreign
    keys inside an existing transaction is unsafe because SQLite ignores that
    change; an already-transactional connection therefore must already have
    foreign keys enabled or fails closed.
    """
    if timeout_seconds <= 0:
        raise UnsupportedSQLiteConnectionInputError("timeout_seconds must be greater than zero.")
    timeout_ms = int(timeout_seconds * 1000)
    if timeout_ms <= 0:
        raise UnsupportedSQLiteConnectionInputError("timeout_seconds is too small to configure.")

    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout = {timeout_ms}")
        configured_timeout = conn.execute("PRAGMA busy_timeout").fetchone()
    except sqlite3.Error as exc:
        raise SQLiteConnectionConfigurationError(
            "Unable to configure the required SQLite busy timeout."
        ) from exc
    if configured_timeout is None or int(configured_timeout[0]) != timeout_ms:
        raise SQLiteConnectionConfigurationError(
            "SQLite did not retain the required busy timeout configuration."
        )

    if not conn.in_transaction:
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as exc:
            raise ForeignKeysDisabledError(
                "Unable to enable mandatory SQLite foreign keys."
            ) from exc
    require_foreign_keys_enabled(conn)


def require_foreign_keys_enabled(conn: sqlite3.Connection) -> None:
    """Fail closed unless SQLite reports foreign-key enforcement as enabled."""
    try:
        row = conn.execute("PRAGMA foreign_keys").fetchone()
    except sqlite3.Error as exc:
        raise ForeignKeysDisabledError("Unable to verify mandatory SQLite foreign keys.") from exc
    if row is None or int(row[0]) != 1:
        raise ForeignKeysDisabledError("Mandatory SQLite foreign-key enforcement is disabled.")


def _validate_target(
    database: str | Path,
    *,
    mode: ConnectionMode,
    timeout_seconds: float,
) -> None:
    if not isinstance(mode, ConnectionMode):
        raise UnsupportedSQLiteConnectionInputError("Unsupported SQLite connection mode.")
    if timeout_seconds <= 0:
        raise UnsupportedSQLiteConnectionInputError("timeout_seconds must be greater than zero.")
    target = str(database)
    if not target:
        raise UnsupportedSQLiteConnectionInputError(
            "An empty SQLite database target is not supported."
        )
    if target.startswith("file:") or target == ":memory:":
        return
    if "?" in target:
        raise UnsupportedSQLiteConnectionInputError(
            "SQLite URI options require an explicit file: URI target."
        )


def _connection_target(database: str | Path, *, mode: ConnectionMode) -> str:
    """Return the SQLite target, enforcing a true read-only path when requested."""
    target = str(database)
    if mode != ConnectionMode.READ_ONLY:
        return target
    if target == ":memory:":
        raise UnsupportedSQLiteConnectionInputError(
            "Read-only SQLite connections cannot target :memory:."
        )
    if target.startswith("file:"):
        _require_read_only_uri_mode(target)
        return target
    return f"{Path(target).resolve().as_uri()}?mode=ro"


def _require_read_only_uri_mode(target: str) -> None:
    """Require exactly one structurally valid SQLite ``mode=ro`` parameter."""
    try:
        query_items = parse_qsl(
            urlsplit(target).query,
            keep_blank_values=True,
            strict_parsing=True,
            errors="strict",
        )
    except ValueError as exc:
        raise UnsupportedSQLiteConnectionInputError(
            "Read-only SQLite URI query parameters are malformed."
        ) from exc

    mode_values = [value for name, value in query_items if name == "mode"]
    if len(mode_values) != 1 or mode_values[0] != "ro":
        raise UnsupportedSQLiteConnectionInputError(
            "Read-only SQLite URI connections require exactly one mode=ro query parameter."
        )


__all__ = [
    "ConnectionMode",
    "DEFAULT_BUSY_TIMEOUT_MILLISECONDS",
    "DEFAULT_BUSY_TIMEOUT_SECONDS",
    "ForeignKeysDisabledError",
    "SQLiteConnectionConfigurationError",
    "SQLiteConnectionError",
    "UnsupportedSQLiteConnectionInputError",
    "configure_sqlite_connection",
    "connect_sqlite",
    "require_foreign_keys_enabled",
]
