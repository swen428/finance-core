"""Test-only immutable migrated staging database snapshots.

The template is never returned for writes.  Every consumer receives a fresh
staging-authorised database whose schema and migration ledger are restored
with SQLite's backup API while retaining the target file's own authorization.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from finance_core.reconciliation.migrations import (
    TEMP_DB_MIGRATION_PATHS,
    MigrationPathManifest,
    verify_migration_history,
)
from finance_core.staging_guard import create_staging_database, require_staging_database

_AUTHORIZATION_COLUMNS = (
    "token_hash",
    "authorization_version",
    "db_identity",
    "created_at",
)


def _backup_database(source: sqlite3.Connection, destination: sqlite3.Connection) -> None:
    """Restore source into destination through SQLite's online backup API."""
    source.backup(destination)


def _invalidate_staging_authorization(conn: sqlite3.Connection) -> None:
    """Best-effort fail-closed cleanup for a database that failed verification."""
    try:
        conn.execute("DELETE FROM _staging_authorization")
        conn.commit()
    except sqlite3.Error:
        conn.rollback()


@dataclass(frozen=True)
class MigratedStagingTemplate:
    """Location and authoritative migration manifest for a read-only template."""

    path: Path
    migration_paths: MigrationPathManifest | tuple[Path, ...]


def create_migrated_staging_template(
    path: Path,
    *,
    migration_paths: Sequence[Path] = TEMP_DB_MIGRATION_PATHS,
) -> MigratedStagingTemplate:
    """Create, verify, close, and make a migrated template read-only."""
    requested_path = Path(path)
    resolved_path = requested_path.resolve()
    resolved_migrations: Sequence[Path]
    if isinstance(migration_paths, MigrationPathManifest):
        resolved_migrations = migration_paths
    else:
        resolved_migrations = tuple(migration_paths)
    conn = create_staging_database(
        requested_path,
        migration_paths=resolved_migrations,
    )
    try:
        require_staging_database(conn)
        verify_migration_history(conn, resolved_migrations)
        resolved_path.chmod(0o400)
    except Exception:
        _invalidate_staging_authorization(conn)
        raise
    finally:
        conn.close()

    return MigratedStagingTemplate(
        path=resolved_path,
        migration_paths=resolved_migrations,
    )


def clone_migrated_staging_template(
    template: MigratedStagingTemplate,
    target_path: str | Path,
) -> sqlite3.Connection:
    """Restore a template into a fresh path-bound staging database.

    The target is first created by the trusted staging factory.  Its unique
    authorization row is saved, the complete template is restored with
    SQLite backup, and then the target's own authorization is put back before
    both staging identity and migration history are verified fail-closed.
    """
    template_path = template.path.resolve()
    requested_target_path = Path(target_path)
    resolved_target_path = requested_target_path.resolve()
    if template_path == resolved_target_path:
        raise ValueError("Snapshot target must differ from the template path")
    if template_path.stat().st_mode & 0o222:
        raise PermissionError("Migrated staging template must remain read-only")

    source = sqlite3.connect(f"{template_path.as_uri()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    target: sqlite3.Connection | None = None
    try:
        require_staging_database(source)
        verify_migration_history(source, template.migration_paths)

        target = create_staging_database(requested_target_path, migration_paths=None)
        authorization = target.execute(
            f"SELECT {', '.join(_AUTHORIZATION_COLUMNS)} FROM _staging_authorization"
        ).fetchone()
        if authorization is None:
            raise RuntimeError("Fresh staging target is missing its authorization")
        saved_authorization = tuple(authorization[column] for column in _AUTHORIZATION_COLUMNS)

        target.execute("DELETE FROM _staging_authorization")
        target.commit()

        _backup_database(source, target)
        target.execute("DELETE FROM _staging_authorization")
        target.execute(
            "INSERT INTO _staging_authorization "
            f"({', '.join(_AUTHORIZATION_COLUMNS)}) VALUES (?, ?, ?, ?)",
            saved_authorization,
        )
        require_staging_database(target)
        verify_migration_history(target, template.migration_paths)
        target.commit()
        return target
    except Exception:
        if target is not None:
            _invalidate_staging_authorization(target)
            target.close()
        raise
    finally:
        source.close()
