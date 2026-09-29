"""Verified backup and preflight/postflight migration safety tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import finance_core.reconciliation.migration_safety as migration_safety
from finance_core.reconciliation.migration_safety import (
    MigrationBackupError,
    MigrationPhaseError,
    MigrationPostflightError,
    MigrationPreflightError,
    MigrationPreflightReport,
    MigrationTargetError,
    migrate_database_safely,
    validate_migration_target,
)
from finance_core.reconciliation.migrations import (
    FINANCE_APPLICATION_ID,
    TEMP_DB_MIGRATION_PATHS,
    migration_ledger_rows,
    verify_migration_history,
)
from finance_core.sqlite_connection import ConnectionMode, connect_sqlite
from finance_core.staging_guard import create_staging_database

FROZEN_TIME = "2026-07-13T01:02:03+00:00"


def _clock() -> str:
    return FROZEN_TIME


def _target(
    tmp_path: Path,
    *,
    name: str = "target.sqlite",
    migration_paths: tuple[Path, ...] | None = TEMP_DB_MIGRATION_PATHS[:-1],
) -> Path:
    path = tmp_path / name
    conn = create_staging_database(path, migration_paths=migration_paths)
    conn.close()
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _migration(tmp_path: Path, name: str, sql: str) -> Path:
    path = tmp_path / name
    path.write_text(sql, encoding="utf-8")
    return path


def test_healthy_database_gets_verified_backup_migration_and_postflight(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)
    backup = tmp_path / "backup.sqlite"
    result = migrate_database_safely(
        target,
        backup_path=backup,
        clock=_clock,
        application_version="finance-v3",
    )

    assert result.target_path == str(target.resolve())
    assert result.preflight.application_id == FINANCE_APPLICATION_ID
    assert result.preflight.supported_legacy_identity is False
    assert result.preflight.ledger_count == len(TEMP_DB_MIGRATION_PATHS) - 1
    assert result.backup.backup_verified is True
    assert result.backup.backup_sha256 == _sha256(backup)
    assert result.postflight.application_id == FINANCE_APPLICATION_ID
    assert result.postflight.ledger_count == len(TEMP_DB_MIGRATION_PATHS)
    assert result.postflight.integrity_result == "ok"
    assert result.postflight.foreign_key_violation_count == 0

    metadata = json.loads(Path(result.backup_metadata_path).read_text(encoding="utf-8"))
    assert metadata["backup_verified"] is True
    assert metadata["source_database_path"] == str(target.resolve())
    assert metadata["source_application_id"] == FINANCE_APPLICATION_ID
    assert metadata["source_logical_fingerprint"] == result.backup.source_logical_fingerprint

    conn = connect_sqlite(target)
    try:
        verify_migration_history(conn, TEMP_DB_MIGRATION_PATHS)
        assert conn.execute("PRAGMA application_id").fetchone()[0] == FINANCE_APPLICATION_ID
    finally:
        conn.close()


def test_backup_is_pre_migration_readable_snapshot(tmp_path: Path) -> None:
    target = _target(tmp_path)
    backup = tmp_path / "pre-migration.sqlite"
    migrate_database_safely(target, backup_path=backup, clock=_clock)

    conn = connect_sqlite(backup, mode=ConnectionMode.READ_ONLY)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA application_id").fetchone()[0] == FINANCE_APPLICATION_ID
        assert len(migration_ledger_rows(conn)) == len(TEMP_DB_MIGRATION_PATHS) - 1
    finally:
        conn.close()


def test_empty_staging_database_is_backed_up_then_migrated(tmp_path: Path) -> None:
    target = _target(tmp_path, migration_paths=None)
    result = migrate_database_safely(
        target,
        backup_path=tmp_path / "empty-backup.sqlite",
        clock=_clock,
    )
    assert result.preflight.ledger_present is False
    assert result.preflight.legacy_prefix == ()
    assert result.postflight.ledger_count == len(TEMP_DB_MIGRATION_PATHS)


def test_supported_preledger_database_is_fingerprint_adopted(tmp_path: Path) -> None:
    target = _target(tmp_path, migration_paths=None)
    conn = sqlite3.connect(target)
    try:
        for path in TEMP_DB_MIGRATION_PATHS[:10]:
            conn.executescript(path.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()

    result = migrate_database_safely(
        target,
        backup_path=tmp_path / "legacy-backup.sqlite",
        clock=_clock,
    )
    assert result.preflight.ledger_present is False
    assert result.preflight.legacy_prefix == tuple(f"{number:03d}" for number in range(1, 11))
    assert result.postflight.ledger_count == len(TEMP_DB_MIGRATION_PATHS)


def test_invalid_sqlite_header_fails_before_backup_and_preserves_source(tmp_path: Path) -> None:
    target = tmp_path / "not-sqlite.db"
    target.write_bytes(b"not a sqlite database")
    before = _sha256(target)
    backup = tmp_path / "must-not-exist.sqlite"
    with pytest.raises(MigrationTargetError, match="valid SQLite header"):
        migrate_database_safely(target, backup_path=backup, clock=_clock)
    assert _sha256(target) == before
    assert not backup.exists()


def test_corrupted_sqlite_fails_preflight_without_backup(tmp_path: Path) -> None:
    target = _target(tmp_path)
    data = bytearray(target.read_bytes())
    data[100] = 0xFF  # invalid first-page b-tree page type; SQLite header remains valid
    target.write_bytes(data)
    before = _sha256(target)
    backup = tmp_path / "corrupt-backup.sqlite"
    with pytest.raises(MigrationPreflightError):
        migrate_database_safely(target, backup_path=backup, clock=_clock)
    assert _sha256(target) == before
    assert not backup.exists()


def test_foreign_key_violation_blocks_migration_and_preserves_original(tmp_path: Path) -> None:
    target = _target(tmp_path)
    conn = sqlite3.connect(target)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """INSERT INTO calculation_snapshots
            (snapshot_id, run_id, snapshot_type, snapshot_data, created_at)
            VALUES ('orphan-snapshot', 'missing-run', 'input_facts', '{}', '2026-07-13')"""
        )
        conn.commit()
    finally:
        conn.close()
    before = _sha256(target)
    backup = tmp_path / "fk-backup.sqlite"
    with pytest.raises(MigrationPreflightError, match="foreign_key_check"):
        migrate_database_safely(target, backup_path=backup, clock=_clock)
    assert _sha256(target) == before
    assert not backup.exists()


def test_unexpected_application_id_blocks_migration(tmp_path: Path) -> None:
    target = _target(tmp_path)
    conn = sqlite3.connect(target)
    try:
        conn.execute("PRAGMA application_id = 12345")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(MigrationPreflightError, match="Unexpected SQLite application_id"):
        migrate_database_safely(
            target,
            backup_path=tmp_path / "identity-backup.sqlite",
            clock=_clock,
        )


def test_ledger_claiming_identity_with_legacy_application_id_fails(tmp_path: Path) -> None:
    target = _target(tmp_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
    conn = sqlite3.connect(target)
    try:
        conn.execute("PRAGMA application_id = 0")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(MigrationPreflightError, match="records Finance application identity"):
        migrate_database_safely(
            target,
            backup_path=tmp_path / "tampered-identity-backup.sqlite",
            clock=_clock,
        )


def test_unknown_missing_ledger_schema_fails_without_adoption(tmp_path: Path) -> None:
    target = _target(tmp_path, migration_paths=None)
    conn = sqlite3.connect(target)
    try:
        conn.execute("CREATE TABLE unknown_schema (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(MigrationPreflightError, match="preflight verification"):
        migrate_database_safely(
            target,
            backup_path=tmp_path / "unknown-backup.sqlite",
            clock=_clock,
        )


def test_checksum_mismatch_blocks_before_backup(tmp_path: Path) -> None:
    target = _target(tmp_path)
    conn = sqlite3.connect(target)
    try:
        conn.execute(
            "UPDATE schema_migrations SET checksum_sha256 = ? WHERE migration_id = '001'",
            ("f" * 64,),
        )
        conn.commit()
    finally:
        conn.close()
    backup = tmp_path / "checksum-backup.sqlite"
    with pytest.raises(MigrationPreflightError):
        migrate_database_safely(target, backup_path=backup, clock=_clock)
    assert not backup.exists()


def test_existing_backup_destination_is_never_overwritten(tmp_path: Path) -> None:
    target = _target(tmp_path)
    backup = tmp_path / "existing.sqlite"
    backup.write_bytes(b"existing evidence")
    before = backup.read_bytes()
    with pytest.raises(MigrationBackupError, match="already exists"):
        migrate_database_safely(target, backup_path=backup, clock=_clock)
    assert backup.read_bytes() == before


def test_existing_metadata_destination_is_never_overwritten(tmp_path: Path) -> None:
    target = _target(tmp_path)
    backup = tmp_path / "metadata-collision.sqlite"
    metadata = Path(f"{backup}.metadata.json")
    metadata.write_text("existing metadata", encoding="utf-8")
    with pytest.raises(MigrationBackupError, match="already exists"):
        migrate_database_safely(target, backup_path=backup, clock=_clock)
    assert metadata.read_text(encoding="utf-8") == "existing metadata"
    assert not backup.exists()


def test_backup_write_failure_prevents_migration(tmp_path: Path) -> None:
    target = _target(tmp_path)
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("file", encoding="utf-8")
    with pytest.raises(MigrationBackupError, match="directory is not writable"):
        migrate_database_safely(
            target,
            backup_path=blocked_parent / "backup.sqlite",
            clock=_clock,
        )
    conn = sqlite3.connect(target)
    try:
        assert conn.execute("PRAGMA application_id").fetchone()[0] == FINANCE_APPLICATION_ID
    finally:
        conn.close()


def test_backup_verification_failure_prevents_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(tmp_path)
    backup = tmp_path / "verification-failure.sqlite"

    def fail_verification(*args: object, **kwargs: object) -> tuple[str, int]:
        raise MigrationBackupError("injected backup verification failure")

    monkeypatch.setattr(migration_safety, "_verify_backup", fail_verification)
    with pytest.raises(MigrationBackupError, match="injected"):
        migrate_database_safely(target, backup_path=backup, clock=_clock)
    assert backup.exists()
    assert not Path(f"{backup}.metadata.json").exists()
    conn = sqlite3.connect(target)
    try:
        assert conn.execute("PRAGMA application_id").fetchone()[0] == FINANCE_APPLICATION_ID
    finally:
        conn.close()


def test_migration_failure_occurs_only_after_verified_backup(tmp_path: Path) -> None:
    target = _target(tmp_path, migration_paths=None)
    first = _migration(tmp_path, "001_first.sql", "CREATE TABLE first_ok (id INTEGER);")
    failing = _migration(
        tmp_path,
        "002_failure.sql",
        "CREATE TABLE partial (id INTEGER); INSERT INTO missing VALUES (1);",
    )
    backup = tmp_path / "before-failed-migration.sqlite"
    with pytest.raises(MigrationPhaseError, match="verified backup"):
        migrate_database_safely(
            target,
            migration_paths=(first, failing),
            backup_path=backup,
            expected_application_id=0,
            clock=_clock,
        )
    assert backup.exists()
    assert Path(f"{backup}.metadata.json").exists()
    source = sqlite3.connect(target)
    try:
        assert (
            source.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'first_ok'"
            ).fetchone()
            is not None
        )
        assert (
            source.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'partial'"
            ).fetchone()
            is None
        )
    finally:
        source.close()
    backed_up = sqlite3.connect(backup)
    try:
        assert (
            backed_up.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'first_ok'"
            ).fetchone()
            is None
        )
    finally:
        backed_up.close()


def test_postflight_failure_keeps_migrated_source_and_older_backup_without_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(tmp_path)
    backup = tmp_path / "postflight-failure.sqlite"

    def fail_postflight(*args: object, **kwargs: object) -> object:
        raise MigrationPostflightError("injected postflight failure")

    monkeypatch.setattr(migration_safety, "inspect_migration_postflight", fail_postflight)
    with pytest.raises(MigrationPostflightError, match="injected"):
        migrate_database_safely(target, backup_path=backup, clock=_clock)

    source = sqlite3.connect(target)
    old = sqlite3.connect(backup)
    try:
        assert source.execute("PRAGMA application_id").fetchone()[0] == FINANCE_APPLICATION_ID
        assert old.execute("PRAGMA application_id").fetchone()[0] == FINANCE_APPLICATION_ID
        assert len(migration_ledger_rows(source)) == len(TEMP_DB_MIGRATION_PATHS)
        assert len(migration_ledger_rows(old)) == len(TEMP_DB_MIGRATION_PATHS) - 1
    finally:
        source.close()
        old.close()


def test_restart_after_success_creates_new_noncolliding_verified_backup(tmp_path: Path) -> None:
    target = _target(tmp_path)
    first = migrate_database_safely(
        target,
        backup_path=tmp_path / "restart-first.sqlite",
        clock=_clock,
    )
    second = migrate_database_safely(
        target,
        backup_path=tmp_path / "restart-second.sqlite",
        clock=lambda: "2026-07-13T01:03:04+00:00",
    )
    assert first.backup_path != second.backup_path
    assert second.preflight.application_id == FINANCE_APPLICATION_ID
    assert second.postflight.application_id == FINANCE_APPLICATION_ID


def test_restart_after_failed_migration_can_use_backup_and_corrected_manifest(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path, migration_paths=None)
    first = _migration(tmp_path, "001_first.sql", "CREATE TABLE first_ok (id INTEGER);")
    second = _migration(
        tmp_path,
        "002_second.sql",
        "CREATE TABLE second_ok (id INTEGER); INSERT INTO absent VALUES (1);",
    )
    with pytest.raises(MigrationPhaseError):
        migrate_database_safely(
            target,
            migration_paths=(first, second),
            backup_path=tmp_path / "failed-attempt.sqlite",
            expected_application_id=0,
            clock=_clock,
        )
    second.write_text("CREATE TABLE second_ok (id INTEGER);", encoding="utf-8")
    result = migrate_database_safely(
        target,
        migration_paths=(first, second),
        backup_path=tmp_path / "retry-attempt.sqlite",
        expected_application_id=0,
        clock=lambda: "2026-07-13T01:04:05+00:00",
    )
    assert result.postflight.ledger_count == 2


def test_symlink_target_is_rejected_before_open(tmp_path: Path) -> None:
    target = _target(tmp_path)
    symlink = tmp_path / "target-link.sqlite"
    symlink.symlink_to(target)
    with pytest.raises(MigrationTargetError, match="symbolic links"):
        migrate_database_safely(
            symlink,
            backup_path=tmp_path / "symlink-backup.sqlite",
            clock=_clock,
        )


def test_symlinked_target_parent_is_rejected(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    target = _target(real_parent)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(MigrationTargetError, match="symbolic links"):
        validate_migration_target(linked_parent / target.name)


def test_symlinked_backup_parent_is_rejected(tmp_path: Path) -> None:
    target = _target(tmp_path)
    real_parent = tmp_path / "real-backups"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-backups"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(MigrationBackupError, match="symbolic links"):
        migrate_database_safely(
            target,
            backup_path=linked_parent / "backup.sqlite",
            clock=_clock,
        )


def test_hardlink_to_protected_database_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = _target(tmp_path, name="protected.sqlite", migration_paths=None)
    monkeypatch.setattr(migration_safety, "LIVE_DB_PATH", protected)
    hardlink = tmp_path / "live-hardlink.sqlite"
    try:
        os.link(protected, hardlink)
    except OSError:
        pytest.skip("hard links are unavailable")
    with pytest.raises(MigrationTargetError, match="finance.db"):
        validate_migration_target(hardlink)


def test_protected_database_path_is_rejected_without_opening_or_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = _target(tmp_path, name="protected-direct.sqlite", migration_paths=None)
    monkeypatch.setattr(migration_safety, "LIVE_DB_PATH", protected)
    before = protected.stat()
    with pytest.raises(MigrationTargetError, match="finance.db"):
        validate_migration_target(protected)
    after = protected.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def _managed_staging_path(tmp_path: Path, *, profile_id: str = "synthetic") -> Path:
    database = (
        tmp_path
        / "Application Support"
        / "Finance-Codex"
        / "profiles"
        / profile_id
        / "workspace"
        / "database"
    )
    database.mkdir(parents=True)
    return database / "staging.sqlite"


@pytest.mark.parametrize("profile_id", ["synthetic", "copied-profile"])
def test_legacy_migration_refuses_managed_profile_before_sqlite_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile_id: str
) -> None:
    managed = _managed_staging_path(tmp_path, profile_id=profile_id)
    conn = create_staging_database(managed)
    conn.close()
    before = managed.read_bytes()
    backup = tmp_path / "outside-backup.sqlite"

    def unexpected_open(*_args: object, **_kwargs: object) -> None:
        pytest.fail("managed migration reached SQLite open")

    monkeypatch.setattr(migration_safety, "connect_sqlite", unexpected_open)
    with pytest.raises(MigrationTargetError, match="managed"):
        migrate_database_safely(managed, backup_path=backup)
    assert managed.read_bytes() == before
    assert not backup.exists()


@pytest.mark.parametrize("destination_kind", ["path", "directory", "path_alias", "directory_alias"])
def test_legacy_migration_refuses_managed_backup_namespace_before_sqlite_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, destination_kind: str
) -> None:
    ordinary = _target(tmp_path, migration_paths=None)
    managed = _managed_staging_path(tmp_path)
    managed_backup_directory = managed.parents[2] / "backups"
    if destination_kind.endswith("_alias"):
        profile_alias = tmp_path / "ordinary-looking-profile"
        profile_alias.symlink_to(managed.parents[2], target_is_directory=True)
        assert profile_alias.resolve() == managed.parents[2].resolve()
        backup_directory = profile_alias / "backups"
    else:
        managed_backup_directory.mkdir()
        backup_directory = managed_backup_directory
    before = ordinary.read_bytes()

    def unexpected_open(*_args: object, **_kwargs: object) -> None:
        pytest.fail("managed backup destination reached SQLite open")

    monkeypatch.setattr(migration_safety, "connect_sqlite", unexpected_open)
    is_path = destination_kind in {"path", "path_alias"}
    backup_path = backup_directory / "candidate.sqlite"
    kwargs = {"backup_path": backup_path} if is_path else {"backup_directory": backup_directory}
    with pytest.raises(MigrationBackupError, match="managed"):
        migrate_database_safely(ordinary, **kwargs)
    assert ordinary.read_bytes() == before
    assert not backup_path.exists()
    assert not Path(f"{backup_path}.metadata.json").exists()
    if destination_kind.endswith("_alias"):
        assert not managed_backup_directory.exists()
    else:
        assert list(managed_backup_directory.iterdir()) == []


def test_direct_backup_refuses_managed_metadata_alias_before_output_and_preserves_transaction(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "ordinary.sqlite"
    source = sqlite3.connect(source_path)
    source.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
    source.commit()
    source.execute("BEGIN")
    source.execute("INSERT INTO evidence (value) VALUES ('pending')")

    managed = _managed_staging_path(tmp_path)
    managed_backup_directory = managed.parents[2] / "backups"
    profile_alias = tmp_path / "ordinary-looking-profile"
    profile_alias.symlink_to(managed.parents[2], target_is_directory=True)
    assert profile_alias.resolve() == managed.parents[2].resolve()

    destination = tmp_path / "must-not-exist.sqlite"
    metadata_path = profile_alias / "backups" / "must-not-exist.metadata.json"
    try:
        with pytest.raises(MigrationBackupError, match="managed"):
            migration_safety.create_verified_backup(
                source,
                source_path,
                destination,
                metadata_path,
                preflight=cast(MigrationPreflightReport, None),
                created_at=FROZEN_TIME,
            )
        assert source.in_transaction
        assert source.execute("SELECT value FROM evidence").fetchone()[0] == "pending"
        assert not destination.exists()
        assert not metadata_path.exists()
        assert not managed_backup_directory.exists()
    finally:
        source.rollback()
        source.close()


def test_direct_backup_refuses_actual_managed_source_even_with_ordinary_path_lie(
    tmp_path: Path,
) -> None:
    managed = _managed_staging_path(tmp_path)
    source = create_staging_database(managed)
    ordinary_lie = tmp_path / "ordinary.sqlite"
    destination = tmp_path / "must-not-exist.sqlite"
    try:
        with pytest.raises(MigrationBackupError, match="managed|match"):
            migration_safety.create_verified_backup(
                source,
                ordinary_lie,
                destination,
                Path(f"{destination}.metadata.json"),
                preflight=cast(MigrationPreflightReport, None),
                created_at=FROZEN_TIME,
            )
        assert source.execute("SELECT 1").fetchone()[0] == 1
        assert not source.in_transaction
        assert not destination.exists()
    finally:
        source.close()


def test_direct_backup_refusal_preserves_caller_transaction(tmp_path: Path) -> None:
    ordinary = tmp_path / "ordinary.sqlite"
    source = sqlite3.connect(ordinary)
    source.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
    source.commit()
    source.execute("BEGIN")
    source.execute("INSERT INTO evidence (value) VALUES ('pending')")
    wrong_source = tmp_path / "wrong-source.sqlite"
    other = sqlite3.connect(wrong_source)
    other.close()
    destination = tmp_path / "no-backup.sqlite"
    try:
        with pytest.raises(MigrationBackupError, match="do not match"):
            migration_safety.create_verified_backup(
                source,
                wrong_source,
                destination,
                Path(f"{destination}.metadata.json"),
                preflight=cast(MigrationPreflightReport, None),
                created_at=FROZEN_TIME,
            )
        assert source.in_transaction
        assert source.execute("SELECT value FROM evidence").fetchone()[0] == "pending"
        assert not destination.exists()
    finally:
        source.rollback()
        source.close()


def test_direct_backup_refuses_preflight_for_another_database_without_output(
    tmp_path: Path,
) -> None:
    actual = _target(tmp_path, name="actual.sqlite", migration_paths=None)
    other = _target(tmp_path, name="other.sqlite", migration_paths=None)
    source = connect_sqlite(actual, mode=ConnectionMode.MIGRATION)
    destination = tmp_path / "no-backup.sqlite"
    try:
        preflight = migration_safety.inspect_migration_preflight(
            source,
            actual,
            migration_paths=TEMP_DB_MIGRATION_PATHS,
            expected_application_id=FINANCE_APPLICATION_ID,
            allow_legacy_application_id=True,
        )
        source.execute("BEGIN")
        with pytest.raises(MigrationBackupError, match="preflight target"):
            migration_safety.create_verified_backup(
                source,
                actual,
                destination,
                Path(f"{destination}.metadata.json"),
                preflight=replace(preflight, target_path=str(other)),
                created_at=FROZEN_TIME,
            )
        assert source.in_transaction
        assert source.execute("SELECT 1").fetchone()[0] == 1
        assert not destination.exists()
    finally:
        source.rollback()
        source.close()


def test_direct_backup_refuses_copied_staging_binding_outside_profile(tmp_path: Path) -> None:
    managed = _managed_staging_path(tmp_path)
    source = create_staging_database(managed)
    source.close()
    copied = tmp_path / "ordinary-copied.sqlite"
    shutil.copyfile(managed, copied)
    destination = tmp_path / "must-not-exist.sqlite"
    connection = sqlite3.connect(copied)
    try:
        with pytest.raises(MigrationBackupError, match="binding|copied|refused"):
            migration_safety.create_verified_backup(
                connection,
                copied,
                destination,
                Path(f"{destination}.metadata.json"),
                preflight=cast(MigrationPreflightReport, None),
                created_at=FROZEN_TIME,
            )
        assert connection.execute("SELECT 1").fetchone()[0] == 1
        assert not destination.exists()
    finally:
        connection.close()


def test_legacy_migration_refuses_copied_staging_binding_before_backup_or_schema(
    tmp_path: Path,
) -> None:
    managed = _managed_staging_path(tmp_path)
    source = create_staging_database(managed)
    source.close()
    copied = tmp_path / "ordinary-copied.sqlite"
    shutil.copyfile(managed, copied)
    before = copied.read_bytes()
    destination = tmp_path / "no-backup.sqlite"

    with pytest.raises(MigrationPreflightError, match="Staging source binding refused"):
        migrate_database_safely(copied, backup_path=destination)
    assert copied.read_bytes() == before
    assert not destination.exists()


def test_legacy_migration_refuses_renamed_profile_tree_with_registration_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moved_profile = tmp_path / "moved-profile"
    database = moved_profile / "workspace" / "database"
    database.mkdir(parents=True)
    target = database / "staging.sqlite"
    connection = create_staging_database(target)
    connection.close()
    (moved_profile / ".managed-staging.v1.json").write_text("synthetic copied marker\n")

    def unexpected_open(*_args: object, **_kwargs: object) -> None:
        pytest.fail("renamed profile reached SQLite open")

    monkeypatch.setattr(migration_safety, "connect_sqlite", unexpected_open)
    with pytest.raises(MigrationTargetError, match="managed"):
        migrate_database_safely(target, backup_path=tmp_path / "no-backup.sqlite")
    assert not (tmp_path / "no-backup.sqlite").exists()


def test_legacy_migration_refuses_symlink_alias_into_managed_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed = _managed_staging_path(tmp_path)
    source = create_staging_database(managed)
    source.close()
    alias = tmp_path / "ordinary-looking-directory"
    alias.symlink_to(managed.parent, target_is_directory=True)

    def unexpected_open(*_args: object, **_kwargs: object) -> None:
        pytest.fail("managed alias reached SQLite open")

    monkeypatch.setattr(migration_safety, "connect_sqlite", unexpected_open)
    with pytest.raises(MigrationTargetError, match="managed"):
        migrate_database_safely(alias / managed.name, backup_path=tmp_path / "no-backup.sqlite")
    assert not (tmp_path / "no-backup.sqlite").exists()


@pytest.mark.parametrize("case", ["memory", "closed", "attached", "mismatch"])
def test_direct_backup_refuses_ambiguous_connection_without_changing_it(
    tmp_path: Path, case: str
) -> None:
    source_path = tmp_path / "source.sqlite"
    connection = sqlite3.connect(":memory:" if case == "memory" else source_path)
    if case == "attached":
        connection.execute("ATTACH DATABASE ':memory:' AS extra")
    if case == "closed":
        connection.close()
    destination = tmp_path / "no-backup.sqlite"
    try:
        with pytest.raises(MigrationBackupError, match="source|main|connection"):
            migration_safety.create_verified_backup(
                connection,
                tmp_path / "different.sqlite" if case == "mismatch" else source_path,
                destination,
                Path(f"{destination}.metadata.json"),
                preflight=cast(MigrationPreflightReport, None),
                created_at=FROZEN_TIME,
            )
        assert not destination.exists()
        if case != "closed":
            assert connection.execute("SELECT 1").fetchone()[0] == 1
            assert not connection.in_transaction
    finally:
        if case != "closed":
            connection.close()


@pytest.mark.parametrize("case", ["destination", "metadata", "supplied_source"])
def test_direct_backup_refuses_any_supplied_managed_namespace_before_output(
    tmp_path: Path, case: str
) -> None:
    ordinary = tmp_path / "ordinary.sqlite"
    connection = sqlite3.connect(ordinary)
    managed = _managed_staging_path(tmp_path)
    profile_backups = managed.parents[2] / "backups"
    profile_backups.mkdir()
    destination = tmp_path / "no-backup.sqlite"
    metadata = Path(f"{destination}.metadata.json")
    supplied_source = ordinary
    if case == "destination":
        destination = profile_backups / "no-backup.sqlite"
    elif case == "metadata":
        metadata = profile_backups / "no-backup.metadata.json"
    else:
        supplied_source = managed
    try:
        with pytest.raises(MigrationBackupError, match="managed"):
            migration_safety.create_verified_backup(
                connection,
                supplied_source,
                destination,
                metadata,
                preflight=cast(MigrationPreflightReport, None),
                created_at=FROZEN_TIME,
            )
        assert connection.execute("SELECT 1").fetchone()[0] == 1
        assert not destination.exists()
        assert not metadata.exists()
    finally:
        connection.close()
