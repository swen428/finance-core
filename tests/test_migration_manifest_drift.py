"""Tests for canonical temporary database migration manifest drift."""

from __future__ import annotations

from pathlib import Path

from finance_core.reconciliation.migrations import MIGRATIONS_DIR, TEMP_DB_MIGRATION_PATHS


def test_temp_db_migration_manifest_includes_all_migrations() -> None:
    migration_files = tuple(sorted(MIGRATIONS_DIR.glob("*.sql")))
    manifest_files = tuple(TEMP_DB_MIGRATION_PATHS)

    missing_from_manifest = sorted(set(migration_files) - set(manifest_files))
    extra_in_manifest = sorted(set(manifest_files) - set(migration_files))

    assert missing_from_manifest == []
    assert extra_in_manifest == []


def test_temp_db_migration_manifest_is_in_filename_order() -> None:
    manifest_names = [Path(path).name for path in TEMP_DB_MIGRATION_PATHS]

    assert manifest_names == sorted(manifest_names)
