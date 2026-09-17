"""Characterization tests for immutable migrated staging snapshots."""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import migrated_staging_snapshot_v1 as snapshot
import pytest

from finance_core.reconciliation.migrations import (
    TEMP_DB_MIGRATION_PATHS,
    migration_ledger_rows,
    schema_fingerprint,
    verify_migration_history,
)
from finance_core.staging_guard import (
    StagingDatabaseError,
    create_staging_database,
    require_staging_database,
)


def _authorization(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT token_hash, authorization_version, db_identity, created_at
        FROM _staging_authorization
        """
    ).fetchone()
    assert row is not None
    return row


def test_template_is_read_only_and_has_complete_verified_history(tmp_path: Path) -> None:
    template = snapshot.create_migrated_staging_template(tmp_path / "template.sqlite")

    assert template.path == (tmp_path / "template.sqlite").resolve()
    assert template.migration_paths == TEMP_DB_MIGRATION_PATHS
    assert template.path.stat().st_mode & 0o222 == 0

    conn = sqlite3.connect(f"file:{template.path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        require_staging_database(conn)
        verify_migration_history(conn, template.migration_paths)
    finally:
        conn.close()


def test_template_factory_rejects_dangling_symlink_without_creating_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "template-target.sqlite"
    alias = tmp_path / "template-alias.sqlite"
    alias.symlink_to(target)

    with pytest.raises(StagingDatabaseError, match="symlink"):
        snapshot.create_migrated_staging_template(alias)

    assert alias.is_symlink()
    assert not target.exists()


def test_failed_template_verification_leaves_database_unauthorized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "template.sqlite"
    real_verify = verify_migration_history

    def fail_verification(
        conn: sqlite3.Connection,
        migration_paths: tuple[Path, ...],
    ) -> None:
        real_verify(conn, migration_paths)
        raise RuntimeError("injected template verification failure")

    monkeypatch.setattr(snapshot, "verify_migration_history", fail_verification)

    with pytest.raises(RuntimeError, match="injected template verification failure"):
        snapshot.create_migrated_staging_template(target)

    reopened = sqlite3.connect(target)
    try:
        with pytest.raises(StagingDatabaseError, match="authorization"):
            require_staging_database(reopened)
    finally:
        reopened.close()


def test_failed_template_read_only_transition_leaves_database_unauthorized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = (tmp_path / "template.sqlite").resolve()
    real_chmod = Path.chmod

    def fail_template_chmod(path: Path, mode: int) -> None:
        if path == target and mode == 0o400:
            raise PermissionError("injected read-only transition failure")
        real_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", fail_template_chmod)

    with pytest.raises(PermissionError, match="injected read-only transition failure"):
        snapshot.create_migrated_staging_template(target)

    reopened = sqlite3.connect(target)
    try:
        with pytest.raises(StagingDatabaseError, match="authorization"):
            require_staging_database(reopened)
    finally:
        reopened.close()


def test_clone_restores_full_schema_and_ledger_with_its_own_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = snapshot.create_migrated_staging_template(tmp_path / "template.sqlite")
    target = tmp_path / "clone.sqlite"
    factory_paths: list[Path] = []
    factory_authorizations: list[tuple[object, ...]] = []
    real_factory = create_staging_database

    def recording_factory(
        path: str | Path,
        *,
        migration_paths: tuple[Path, ...] | None = None,
    ) -> sqlite3.Connection:
        factory_paths.append(Path(path).resolve())
        conn = real_factory(path, migration_paths=migration_paths)
        factory_authorizations.append(tuple(_authorization(conn)))
        return conn

    monkeypatch.setattr(snapshot, "create_staging_database", recording_factory)

    template_conn = sqlite3.connect(f"file:{template.path}?mode=ro", uri=True)
    template_conn.row_factory = sqlite3.Row
    try:
        template_authorization = _authorization(template_conn)
        template_schema = schema_fingerprint(template_conn)
        template_ledger = migration_ledger_rows(template_conn)
    finally:
        template_conn.close()

    clone_conn = snapshot.clone_migrated_staging_template(template, str(target))
    try:
        require_staging_database(clone_conn)
        verify_migration_history(clone_conn, template.migration_paths)
        clone_authorization = _authorization(clone_conn)

        assert factory_paths == [target.resolve()]
        assert schema_fingerprint(clone_conn) == template_schema
        assert migration_ledger_rows(clone_conn) == template_ledger
        assert clone_authorization["db_identity"] == str(target.resolve())
        assert clone_authorization["token_hash"] != template_authorization["token_hash"]
        assert tuple(clone_authorization) == factory_authorizations[0]
    finally:
        clone_conn.close()

    with pytest.raises(StagingDatabaseError, match="existing path"):
        snapshot.clone_migrated_staging_template(template, target)


def test_clone_factory_rejects_dangling_symlink_without_creating_target(
    tmp_path: Path,
) -> None:
    template = snapshot.create_migrated_staging_template(tmp_path / "template.sqlite")
    target = tmp_path / "clone-target.sqlite"
    alias = tmp_path / "clone-alias.sqlite"
    alias.symlink_to(target)

    with pytest.raises(StagingDatabaseError, match="symlink"):
        snapshot.clone_migrated_staging_template(template, alias)

    assert alias.is_symlink()
    assert not target.exists()


def test_failed_final_verification_never_commits_target_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = snapshot.create_migrated_staging_template(tmp_path / "template.sqlite")
    target = tmp_path / "clone.sqlite"
    real_verify = verify_migration_history
    verification_count = 0

    def fail_target_verification(
        conn: sqlite3.Connection,
        migration_paths: tuple[Path, ...],
    ) -> None:
        nonlocal verification_count
        verification_count += 1
        real_verify(conn, migration_paths)
        if verification_count == 2:
            raise RuntimeError("injected final verification failure")

    monkeypatch.setattr(snapshot, "verify_migration_history", fail_target_verification)

    with pytest.raises(RuntimeError, match="injected final verification failure"):
        snapshot.clone_migrated_staging_template(template, target)

    assert verification_count == 2
    reopened = sqlite3.connect(target)
    try:
        with pytest.raises(StagingDatabaseError, match="authorization"):
            require_staging_database(reopened)
    finally:
        reopened.close()


def test_failed_backup_leaves_fresh_target_unauthorized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = snapshot.create_migrated_staging_template(tmp_path / "template.sqlite")
    target = tmp_path / "clone.sqlite"

    def fail_backup(source: sqlite3.Connection, destination: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("injected backup failure")

    monkeypatch.setattr(snapshot, "_backup_database", fail_backup, raising=False)

    created: sqlite3.Connection | None = None
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected backup failure"):
            created = snapshot.clone_migrated_staging_template(template, target)
    finally:
        if created is not None:
            created.close()

    reopened = sqlite3.connect(target)
    try:
        with pytest.raises(StagingDatabaseError, match="authorization"):
            require_staging_database(reopened)
    finally:
        reopened.close()


def test_each_clone_is_isolated_from_template_and_other_clones(tmp_path: Path) -> None:
    template = snapshot.create_migrated_staging_template(tmp_path / "template.sqlite")
    first = snapshot.clone_migrated_staging_template(template, tmp_path / "first.sqlite")
    second = snapshot.clone_migrated_staging_template(template, tmp_path / "second.sqlite")

    try:
        first.execute("CREATE TABLE clone_only (value TEXT NOT NULL)")
        first.execute("INSERT INTO clone_only (value) VALUES ('first')")
        first.commit()

        assert (
            second.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'clone_only'"
            ).fetchone()
            is None
        )
        require_staging_database(second)
        verify_migration_history(second, template.migration_paths)
    finally:
        first.close()
        second.close()

    template_conn = sqlite3.connect(f"file:{template.path}?mode=ro", uri=True)
    try:
        assert (
            template_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'clone_only'"
            ).fetchone()
            is None
        )
    finally:
        template_conn.close()


def test_copied_or_renamed_clone_remains_rejected(tmp_path: Path) -> None:
    template = snapshot.create_migrated_staging_template(tmp_path / "template.sqlite")
    original = tmp_path / "original.sqlite"
    conn = snapshot.clone_migrated_staging_template(template, original)
    conn.close()

    copied = tmp_path / "copied.sqlite"
    shutil.copy2(original, copied)
    copied_conn = sqlite3.connect(copied)
    try:
        with pytest.raises(StagingDatabaseError, match="not bound to this database file"):
            require_staging_database(copied_conn)
    finally:
        copied_conn.close()

    renamed = tmp_path / "renamed.sqlite"
    os.rename(original, renamed)
    renamed_conn = sqlite3.connect(renamed)
    try:
        with pytest.raises(StagingDatabaseError, match="not bound to this database file"):
            require_staging_database(renamed_conn)
    finally:
        renamed_conn.close()
