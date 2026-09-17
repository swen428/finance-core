"""Centralized staging-database guard for high-risk financial write paths.

This module provides a fail-closed staging guard that rejects writes against
untrusted SQLite databases. Only ``:memory:`` databases and explicitly
authorised staging databases created through the trusted factory are allowed.

Trusted staging databases are authorised at creation time via a
``_staging_authorization`` table that binds a cryptographic token to the
resolved filesystem path. A copied or renamed authorised database is
rejected because the stored identity no longer matches.

Usage::

    from finance_core.staging_guard import require_staging_database

    def my_write_fn(conn):
        require_staging_database(conn)
        ...

Tests create staging databases via the ``create_staging_database`` factory::

    from finance_core.staging_guard import create_staging_database

    conn = create_staging_database(tmp_path / "test.sqlite")

Do not add a bypass flag, environment-variable override, or ``force=True``
parameter. This guard must fail closed.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from finance_core.runtime_paths import RuntimePathConfigurationError, live_database_path
from finance_core.sqlite_connection import configure_sqlite_connection

# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class StagingDatabaseError(RuntimeError):
    """Raised when a high-risk write path is invoked against a non-staging database.

    The operation requires a verifiably temporary staging database created
    through the project's trusted staging factory, or an in-memory
    ``:memory:`` database. A live or arbitrary existing SQLite database is
    never accepted.
    """


def _configured_live_database_path() -> Path:
    try:
        return live_database_path()
    except RuntimePathConfigurationError as exc:
        raise StagingDatabaseError(f"Private runtime root is not safely configured: {exc}") from exc


# ---------------------------------------------------------------------------
# Internal staging-authorisation table
# ---------------------------------------------------------------------------

_STAGING_TOKEN_TABLE = "_staging_authorization"
_AUTHORIZATION_VERSION = 1

_STAGING_TOKEN_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_STAGING_TOKEN_TABLE} ("
    "token_hash TEXT NOT NULL,"
    "authorization_version INTEGER NOT NULL,"
    "db_identity TEXT NOT NULL,"
    "created_at TEXT NOT NULL"
    ")"
)

_EXPECTED_COLUMNS: list[tuple[str, str, bool]] = [
    ("token_hash", "TEXT", True),
    ("authorization_version", "INTEGER", True),
    ("db_identity", "TEXT", True),
    ("created_at", "TEXT", True),
]

# ---------------------------------------------------------------------------
# File identity helpers
# ---------------------------------------------------------------------------


def _file_identity(path: str) -> tuple[int, int]:
    """Return (st_dev, st_ino) for *path*."""
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


def _is_live_database_file(path: Path, configured_live_database: Path) -> bool:
    """Return whether *path* names the live database by path or inode.

    ``Path.resolve`` detects aliases through symbolic links, but two hard links
    to the same inode retain different resolved path strings. Existing files
    therefore also require an inode identity comparison.
    """
    if path == configured_live_database:
        return True
    try:
        return os.path.samefile(path, configured_live_database)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise StagingDatabaseError(
            "Database identity could not be compared safely with the live database."
        ) from exc


# ---------------------------------------------------------------------------
# Test-only hook (private — never a production bypass)
# ---------------------------------------------------------------------------

_test_path_exchange_hook: Callable[[], None] | None = None
"""Private test hook invoked between exclusive creation and SQLite open.

Set to a no-argument callable in test code only. The factory calls this
hook while the exclusive file descriptor remains open and before calling
``sqlite3.connect``, giving tests a deterministic injection point
to simulate a path-replacement race.

Never set in production, never exported, and never a bypass.
"""


_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")


def _generate_staging_token() -> str:
    random_bytes = os.urandom(32)
    return hashlib.sha256(random_bytes).hexdigest()


# ---------------------------------------------------------------------------
# Row-value access helper (works with sqlite3.Row and plain tuples)
# ---------------------------------------------------------------------------


def _col(row: sqlite3.Row | tuple, name: str, col_idx: int) -> object:
    """Access a column value from either a sqlite3.Row or plain tuple."""
    if isinstance(row, sqlite3.Row):
        return row[name]
    return row[col_idx]


# ---------------------------------------------------------------------------
# Database file path extraction
# ---------------------------------------------------------------------------


def _get_db_file_path(conn: sqlite3.Connection) -> str:
    rows = conn.execute("PRAGMA database_list").fetchall()
    for row in rows:
        name = _col(row, "name", 1)
        if name == "main":
            file_val = _col(row, "file", 2)
            return str(file_val) if file_val else ""
    return ""


# ---------------------------------------------------------------------------
# In-memory detection
# ---------------------------------------------------------------------------


def _is_in_memory(conn: sqlite3.Connection) -> bool:
    """Check that the connection is a genuine in-memory database.

    ``PRAGMA database_list`` reports an empty file path for both
    ``:memory:`` and unnamed temporary databases.  We distinguish them
    via ``PRAGMA journal_mode``, which returns ``memory`` for in-memory
    databases and ``delete`` (or ``wal``) for file-backed databases.
    """
    db_file = _get_db_file_path(conn)
    if db_file:
        return False
    journal_row = conn.execute("PRAGMA journal_mode").fetchone()
    if journal_row is None:
        return False
    journal_val = _col(journal_row, "journal_mode", 0)
    return str(journal_val).lower() == "memory"


# ---------------------------------------------------------------------------
# Authorization validation
# ---------------------------------------------------------------------------


def _validate_authorization_table(conn: sqlite3.Connection) -> str:
    """Validate the ``_staging_authorization`` table schema and contents.

    Returns the ``db_identity`` stored in the authorization record so
    the caller can verify it matches the connection's filesystem path.

    Raises ``StagingDatabaseError`` for any of:
    - Table does not exist
    - Schema does not match expected columns
    - Zero or more than one authorization row
    - Invalid token format or length
    - Unsupported authorization version
    - Missing or malformed ``db_identity`` / ``created_at``
    """
    # -- table existence --------------------------------------------------
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (_STAGING_TOKEN_TABLE,),
    ).fetchone()
    if row is None:
        raise StagingDatabaseError(
            "This database is not an authorised staging database. "
            "High-risk writes require a staging database created through "
            "the project's trusted staging factory."
        )

    # -- schema validation ------------------------------------------------
    table_info = conn.execute(f"PRAGMA table_info({_STAGING_TOKEN_TABLE})").fetchall()
    if len(table_info) != len(_EXPECTED_COLUMNS):
        raise StagingDatabaseError(
            f"Staging authorization table has unexpected schema "
            f"({len(table_info)} columns, expected {len(_EXPECTED_COLUMNS)})."
        )

    for i, (col_name, col_type, not_null) in enumerate(_EXPECTED_COLUMNS):
        info = table_info[i]
        name = str(_col(info, "name", 1))
        ctype = str(_col(info, "type", 2)).upper()
        nn = bool(_col(info, "notnull", 3))

        if name != col_name or ctype != col_type or nn != not_null:
            raise StagingDatabaseError(
                f"Staging authorization table has unexpected schema "
                f"(column {i}: expected {col_name} {col_type} "
                f"{'NOT NULL' if not_null else ''})."
            )

    # -- row count --------------------------------------------------------
    rows = conn.execute(f"SELECT * FROM {_STAGING_TOKEN_TABLE}").fetchall()

    if len(rows) == 0:
        raise StagingDatabaseError(
            "Staging authorization table is empty. "
            "A valid staging database must have an authorization record."
        )
    if len(rows) > 1:
        raise StagingDatabaseError(
            f"Staging authorization table has {len(rows)} rows (expected exactly 1)."
        )

    # -- row content validation -------------------------------------------
    record = rows[0]
    token_hash = str(_col(record, "token_hash", 0))
    auth_version = _col(record, "authorization_version", 1)
    db_identity = str(_col(record, "db_identity", 2))
    created_at = str(_col(record, "created_at", 3))

    if not _TOKEN_RE.match(token_hash):
        raise StagingDatabaseError(
            "Staging authorization token is invalid or has an unexpected format."
        )

    if auth_version != _AUTHORIZATION_VERSION:
        raise StagingDatabaseError(
            f"Unsupported staging authorization version {auth_version} "
            f"(expected {_AUTHORIZATION_VERSION})."
        )

    if not db_identity:
        raise StagingDatabaseError("Staging authorization record has missing or empty db_identity.")

    if not created_at:
        raise StagingDatabaseError("Staging authorization record has missing or empty created_at.")

    return db_identity


# ---------------------------------------------------------------------------
# Trusted staging factory
# ---------------------------------------------------------------------------


def create_staging_database(
    path: str | Path,
    *,
    migration_paths: Sequence[Path] | None = None,
) -> sqlite3.Connection:
    """Create a trusted staging database and return an open connection.

    Authorises the database with a staging token bound to the resolved
    filesystem path, applies optional migrations, and configures the
    connection with ``PRAGMA foreign_keys`` and ``Row`` factory.

    The database file is created exclusively.  Any pre-existing
    filesystem entry (file, directory, or symlink) at the requested path
    is rejected.  An ``os.O_CREAT | os.O_EXCL`` open guards against
    TOCTOU races between the existence check and file creation.

    If initialization or migration fails, the created path is left in place
    but is never authorised.  Path-based conditional deletion is deliberately
    avoided because a replacement between identity verification and ``unlink``
    could otherwise delete an attacker-controlled file.

    Parameters
    ----------
    path:
        Filesystem path for the new staging database.  Must not resolve
        to the repository live database path, and must not already exist.
    migration_paths:
        Optional ordered sequence of SQL migration file paths to apply before
        the staging token is written.

    Returns
    -------
    sqlite3.Connection
        Open, authorised staging connection ready for writes.

    Raises
    ------
    StagingDatabaseError
        If the path is the live database, already exists, is a symlink,
        or cannot be created exclusively.
    """
    db_path = Path(path).resolve()
    configured_live_database = _configured_live_database_path()

    if _is_live_database_file(db_path, configured_live_database):
        raise StagingDatabaseError(
            "Refusing to create a staging database at the live database path: "
            f"'{configured_live_database}'. Use a temporary database path instead."
        )

    if os.path.islink(str(path)):
        raise StagingDatabaseError(
            f"Refusing to create a staging database at a symlink: '{path}'. "
            "Use a direct file path instead."
        )

    if db_path.exists():
        raise StagingDatabaseError(
            f"Refusing to create a staging database at an existing path: "
            f"'{db_path}'. A staging database must be created fresh."
        )

    # Atomic exclusive creation -------------------------------------------
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW  # type: ignore[attr-defined]

    try:
        fd = os.open(str(db_path), flags)
    except FileExistsError:
        raise StagingDatabaseError(f"Path '{db_path}' already exists (race detected).") from None
    except OSError as exc:
        if hasattr(errno, "ELOOP") and exc.errno == errno.ELOOP:  # type: ignore[attr-defined]
            raise StagingDatabaseError(f"Path '{path}' is a symlink (detected at open).") from None
        raise StagingDatabaseError(
            f"Cannot create staging database at '{db_path}': {exc}"
        ) from None

    # Keep fd open as an inode anchor until all identity checks complete.
    # The fd pins the original file inode so that inode reuse (common on
    # Linux tmpfs) cannot defeat the identity check.  We compare
    # os.fstat(fd) against os.stat(path) after sqlite3.connect.
    conn: sqlite3.Connection | None = None
    try:
        created_st = os.fstat(fd)

        # Test-only injection point (no-op in production) -----------------
        if _test_path_exchange_hook is not None:
            _test_path_exchange_hook()

        # Open with SQLite and verify identity ----------------------------
        conn = sqlite3.connect(str(db_path))

        # Verify the fd anchor and path both match the created file.
        # Using os.fstat(fd) instead of a saved inode pair protects
        # against inode-number reuse on tmpfs / ext4.
        anchor_st = os.fstat(fd)
        path_st = os.stat(str(db_path))
        if (anchor_st.st_dev, anchor_st.st_ino) != (created_st.st_dev, created_st.st_ino):
            raise StagingDatabaseError(
                "The staging database file identity changed during "
                "verification. This should not happen."
            )
        if (path_st.st_dev, path_st.st_ino) != (created_st.st_dev, created_st.st_ino):
            raise StagingDatabaseError(
                "The staging database file was replaced between exclusive "
                "creation and SQLite open. The opened file is not the "
                "originally created file."
            )

        configure_sqlite_connection(conn)

        # Apply migrations before authorization. A failed migration leaves
        # an unauthorised database that the guard will reject.
        if migration_paths:
            _apply_migrations(conn, migration_paths)

        # Write authorization only after successful initialization. -------
        now = datetime.now(timezone.utc).isoformat()
        identity_string = str(db_path)
        token = _generate_staging_token()

        conn.execute(_STAGING_TOKEN_DDL)
        conn.execute(
            f"INSERT INTO {_STAGING_TOKEN_TABLE} "
            "(token_hash, authorization_version, db_identity, created_at) "
            "VALUES (?, ?, ?, ?)",
            (token, _AUTHORIZATION_VERSION, identity_string, now),
        )
        conn.commit()

        return conn
    except Exception:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        raise
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Fail-closed guard
# ---------------------------------------------------------------------------


def require_staging_database(
    conn: sqlite3.Connection,
) -> None:
    """Fail-closed guard: raise ``StagingDatabaseError`` unless the
    connection is an authorised staging database or ``:memory:`` connection.

    This guard must be called at the outermost write boundary of every
    high-risk financial write path **before** any mutation.

    Verification steps:

    1. Genuine ``:memory:`` databases (verified via journal mode) are
       accepted unconditionally.
    2. Unidentified connections (empty file path that is not in-memory)
       are rejected.
    3. The resolved filesystem path is compared against the live database
       path; a match is rejected unconditionally.
    4. The ``_staging_authorization`` table schema, row count, token
       format, version, and identity binding are validated.
    5. The stored ``db_identity`` must match the connection's resolved
       path; copied or renamed databases are rejected.

    This function is **read-only**: it performs no writes, commits, or
    rollbacks, and preserves any caller-owned pending transaction.
    """
    if _is_in_memory(conn):
        return

    db_file = _get_db_file_path(conn)
    if not db_file:
        raise StagingDatabaseError(
            "Cannot verify database identity. This database is not a "
            "recognised in-memory database and has no filesystem path. "
            "High-risk writes are not permitted."
        )

    resolved = Path(db_file).resolve()
    configured_live_database = _configured_live_database_path()
    if _is_live_database_file(resolved, configured_live_database):
        raise StagingDatabaseError(
            "High-risk writes are forbidden against the live database: "
            f"'{configured_live_database}'. Use a staging database instead."
        )

    stored_identity = _validate_authorization_table(conn)
    if stored_identity != str(resolved):
        raise StagingDatabaseError(
            "Staging authorization is not bound to this database file. "
            f"The authorization was issued for '{stored_identity}' "
            f"but this database is at '{resolved}'. "
            "A copied or renamed authorised database is not trusted."
        )


# ---------------------------------------------------------------------------
# Migration helper
# ---------------------------------------------------------------------------


def _apply_migrations(
    conn: sqlite3.Connection,
    migration_paths: Sequence[Path],
) -> None:
    # Import locally to keep the staging guard independent of the
    # reconciliation package at import time.  Migration execution remains
    # owned by its explicit migration-only boundary.
    from finance_core.reconciliation.migrations import apply_migration_paths

    for migration_path in migration_paths:
        if not migration_path.exists():
            # Preserve the staging factory's established public contract.
            raise FileNotFoundError(f"Migration not found: {migration_path}")
    apply_migration_paths(conn, migration_paths)


# ---------------------------------------------------------------------------
# Trusted staging reopen (B5.1a)
# ---------------------------------------------------------------------------


def open_staging_database(
    path: str | Path,
    *,
    migration_paths: Sequence[Path] | None = None,
) -> sqlite3.Connection:
    """Reopen an existing trusted staging database and verify its identity.

    This is the public reopen/recovery boundary for staging databases that
    were previously created through :func:`create_staging_database`.  It:

    1. Rejects missing paths, directories, symlinks, and the live DB path.
    2. Opens the database with the centralized SQLite connection policy.
    3. Calls :func:`require_staging_database` to validate the authorization
       token and identity binding.
    4. Optionally verifies the complete migration ledger when
       ``migration_paths`` is supplied.

    The function is strictly read-only with respect to application data: it
    does not commit, roll back, create schema, or run migrations.  On any
    failure the connection is closed and no open handle escapes.

    Parameters
    ----------
    path:
        Filesystem path to an existing staging database file.
    migration_paths:
        Optional ordered migration manifest to verify via
        ``verify_migration_history``.  When supplied, the ledger must be
        complete and checksum-correct.

    Returns
    -------
    sqlite3.Connection
        Open, verified staging connection ready for reads.

    Raises
    ------
    StagingDatabaseError
        If the path is missing, is a symlink, is the live database, fails
        identity verification, or has an incomplete/tampered migration ledger.
    """
    db_path = Path(path).resolve()
    configured_live_database = _configured_live_database_path()

    if _is_live_database_file(db_path, configured_live_database):
        raise StagingDatabaseError(
            "Refusing to open the live database as a staging database: "
            f"'{configured_live_database}'."
        )

    if os.path.islink(str(path)):
        raise StagingDatabaseError(f"Refusing to open a staging database at a symlink: '{path}'.")

    if not db_path.exists():
        raise StagingDatabaseError(f"Staging database does not exist: '{db_path}'.")

    if db_path.is_dir():
        raise StagingDatabaseError(f"Staging database path is a directory: '{db_path}'.")

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path))
        configure_sqlite_connection(conn)

        # Validate staging authorization token and identity binding.
        require_staging_database(conn)

        # Verify migration ledger if a manifest is supplied.
        if migration_paths is not None:
            from finance_core.reconciliation.migrations import verify_migration_history

            verify_migration_history(conn, migration_paths)

        return conn
    except Exception:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        raise


__all__ = [
    "StagingDatabaseError",
    "create_staging_database",
    "open_staging_database",
    "require_staging_database",
]
