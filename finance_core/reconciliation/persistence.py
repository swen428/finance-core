"""Reconciliation Persistence Helpers v1 -- thin combined helpers for
applying the review/resolution persistence schema and initializing
temporary reconciliation databases in tests and CLI.

This is a convenience module. It does not introduce new abstractions;
it reduces duplication between test fixtures and CLI bootstrap code.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Sequence

from finance_core.reconciliation.migrations import (
    MIGRATION_007_RECONCILIATION_REVIEW_RESOLUTION,
    PROJECT_ROOT,
    TEMP_DB_MIGRATION_PATHS,
    apply_migration_paths,
)
from finance_core.staging_guard import create_staging_database

_PROJECT_ROOT = PROJECT_ROOT
_MIGRATION_007_PATH = MIGRATION_007_RECONCILIATION_REVIEW_RESOLUTION


def apply_reconciliation_review_schema(conn: sqlite3.Connection) -> None:
    """Apply migration 007 to the given connection."""
    apply_migration_paths(conn, (_MIGRATION_007_PATH,))


def apply_all_migrations(
    conn: sqlite3.Connection,
    migration_paths: Sequence[Path],
) -> None:
    """Apply a sequence of migration SQL files to the connection."""
    apply_migration_paths(conn, migration_paths)


def initialize_temp_reconciliation_db(
    db_path: str | Path,
    *,
    migration_paths: Sequence[Path] | None = None,
) -> sqlite3.Connection:
    """Create and initialize a temp staging database with all migrations."""
    if migration_paths is None:
        migration_paths = TEMP_DB_MIGRATION_PATHS

    conn = create_staging_database(db_path, migration_paths=migration_paths)
    return conn


__all__ = [
    "apply_reconciliation_review_schema",
    "apply_all_migrations",
    "initialize_temp_reconciliation_db",
]
