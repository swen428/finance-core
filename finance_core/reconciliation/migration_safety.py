"""Verified backup and integrity boundary for Finance SQLite migrations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from finance_core.reconciliation.migrations import (
    FINANCE_APPLICATION_ID,
    LIVE_DB_PATH,
    TEMP_DB_MIGRATION_PATHS,
    MigrationExecutionError,
    MigrationHistoryError,
    apply_migration_paths,
    identify_legacy_migration_prefix,
    migration_ledger_rows,
    schema_fingerprint,
    verify_migration_history,
)
from finance_core.sqlite_connection import ConnectionMode, SQLiteConnectionError, connect_sqlite

SQLITE_HEADER = b"SQLite format 3\x00"
MIGRATION_SAFETY_VERSION = "finance-migration-safety-v1"


class MigrationSafetyError(RuntimeError):
    """Base error with a stable operational failure phase."""

    phase = "migration_safety"


class MigrationTargetError(MigrationSafetyError):
    phase = "target_validation"


class MigrationPreflightError(MigrationSafetyError):
    phase = "preflight"


class MigrationBackupError(MigrationSafetyError):
    phase = "backup"


class MigrationPhaseError(MigrationSafetyError):
    phase = "migration"


class MigrationPostflightError(MigrationSafetyError):
    phase = "postflight"


@dataclass(frozen=True)
class MigrationPreflightReport:
    target_path: str
    application_id: int
    supported_legacy_identity: bool
    ledger_present: bool
    ledger_count: int
    legacy_prefix: tuple[str, ...]
    schema_fingerprint: str
    page_count: int
    page_size: int
    data_version: int
    integrity_result: str
    foreign_key_violation_count: int
    foreign_keys_enabled: bool


@dataclass(frozen=True)
class MigrationBackupMetadata:
    safety_version: str
    source_database_path: str
    source_application_id: int
    source_schema_fingerprint: str
    source_logical_fingerprint: str
    source_file_size: int
    source_mtime_ns: int
    source_page_count: int
    source_page_size: int
    source_ledger_rows: tuple[dict[str, object], ...]
    backup_destination: str
    backup_created_at: str
    backup_file_size: int
    backup_sha256: str
    backup_integrity_result: str
    backup_foreign_key_violation_count: int
    backup_verified: bool


@dataclass(frozen=True)
class MigrationPostflightReport:
    application_id: int
    ledger_count: int
    schema_fingerprint: str
    integrity_result: str
    foreign_key_violation_count: int
    foreign_keys_enabled: bool


@dataclass(frozen=True)
class SafeMigrationResult:
    target_path: str
    backup_path: str
    backup_metadata_path: str
    preflight: MigrationPreflightReport
    backup: MigrationBackupMetadata
    postflight: MigrationPostflightReport
    completed_at: str


def migrate_database_safely(
    target_path: str | Path,
    *,
    migration_paths: Sequence[Path] = TEMP_DB_MIGRATION_PATHS,
    backup_path: str | Path | None = None,
    backup_directory: str | Path | None = None,
    expected_application_id: int = FINANCE_APPLICATION_ID,
    allow_legacy_application_id: bool = True,
    clock: Callable[[], str] | None = None,
    application_version: str | None = None,
) -> SafeMigrationResult:
    """Back up, migrate, and verify an explicitly approved SQLite target.

    The function never restores or replaces the source database. Any failure
    leaves the verified backup and metadata available for manual recovery.
    """
    target = validate_migration_target(target_path)
    timestamp = _now(clock)
    destination = _resolve_backup_destination(
        target,
        timestamp=timestamp,
        backup_path=backup_path,
        backup_directory=backup_directory,
    )
    metadata_path = Path(f"{destination}.metadata.json")
    _require_unused_backup_paths(destination, metadata_path)

    try:
        conn = connect_sqlite(target, mode=ConnectionMode.MIGRATION)
    except SQLiteConnectionError as exc:
        raise MigrationPreflightError("Unable to open the SQLite target safely") from exc
    try:
        preflight = inspect_migration_preflight(
            conn,
            target,
            migration_paths=migration_paths,
            expected_application_id=expected_application_id,
            allow_legacy_application_id=allow_legacy_application_id,
        )
        backup = create_verified_backup(
            conn,
            target,
            destination,
            metadata_path,
            preflight=preflight,
            created_at=timestamp,
        )
        if int(conn.execute("PRAGMA data_version").fetchone()[0]) != preflight.data_version:
            raise MigrationBackupError(
                "Source database changed after preflight and backup; migration was not started"
            )
        try:
            apply_migration_paths(
                conn,
                migration_paths,
                clock=clock,
                application_version=application_version,
            )
        except (MigrationExecutionError, sqlite3.Error) as exc:
            raise MigrationPhaseError(
                "Migration failed after a verified backup; no automatic restore was attempted"
            ) from exc
        postflight = inspect_migration_postflight(
            conn,
            migration_paths=migration_paths,
            expected_application_id=expected_application_id,
        )
    finally:
        conn.close()

    return SafeMigrationResult(
        target_path=str(target),
        backup_path=str(destination),
        backup_metadata_path=str(metadata_path),
        preflight=preflight,
        backup=backup,
        postflight=postflight,
        completed_at=_now(clock),
    )


def validate_migration_target(target_path: str | Path) -> Path:
    raw = Path(target_path).expanduser()
    if _has_symlink_component(raw.absolute()):
        raise MigrationTargetError("Migration target path must not contain symbolic links")
    try:
        target = raw.resolve(strict=True)
    except OSError as exc:
        raise MigrationTargetError("Migration target does not exist") from exc
    if not target.is_file():
        raise MigrationTargetError("Migration target must be a regular SQLite file")
    if _same_file_if_available(target, LIVE_DB_PATH):
        raise MigrationTargetError("Migration safety boundary refuses database/finance.db")
    try:
        with target.open("rb") as handle:
            header = handle.read(len(SQLITE_HEADER))
    except OSError as exc:
        raise MigrationTargetError("Migration target is not readable") from exc
    if header != SQLITE_HEADER:
        raise MigrationTargetError("Migration target does not have a valid SQLite header")
    return target


def inspect_migration_preflight(
    conn: sqlite3.Connection,
    target: Path,
    *,
    migration_paths: Sequence[Path],
    expected_application_id: int,
    allow_legacy_application_id: bool,
) -> MigrationPreflightReport:
    try:
        integrity = _integrity_result(conn)
        if integrity != "ok":
            raise MigrationPreflightError(f"PRAGMA integrity_check failed: {integrity}")
        foreign_key_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_violations:
            raise MigrationPreflightError("PRAGMA foreign_key_check reported violations")
        foreign_keys_enabled = bool(int(conn.execute("PRAGMA foreign_keys").fetchone()[0]))
        if not foreign_keys_enabled:
            raise MigrationPreflightError("SQLite foreign-key enforcement is disabled")
        application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
        supported_legacy = application_id == 0 and allow_legacy_application_id
        if application_id != expected_application_id and not supported_legacy:
            raise MigrationPreflightError(f"Unexpected SQLite application_id: {application_id}")

        ledger_present = _table_exists(conn, "schema_migrations")
        legacy_prefix: tuple[str, ...] = ()
        if ledger_present:
            verify_migration_history(conn, migration_paths, require_complete=False)
            ledger = migration_ledger_rows(conn)
            if any(row["migration_id"] == "023" for row in ledger) and supported_legacy:
                raise MigrationPreflightError(
                    "Ledger records Finance application identity but application_id is legacy"
                )
        else:
            legacy_prefix = tuple(
                spec.migration_id
                for spec in identify_legacy_migration_prefix(conn, migration_paths)
            )
            ledger = ()

        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        return MigrationPreflightReport(
            target_path=str(target),
            application_id=application_id,
            supported_legacy_identity=supported_legacy,
            ledger_present=ledger_present,
            ledger_count=len(ledger),
            legacy_prefix=legacy_prefix,
            schema_fingerprint=schema_fingerprint(conn),
            page_count=page_count,
            page_size=page_size,
            data_version=data_version,
            integrity_result=integrity,
            foreign_key_violation_count=0,
            foreign_keys_enabled=True,
        )
    except MigrationPreflightError:
        if conn.in_transaction:
            conn.rollback()
        raise
    except (MigrationHistoryError, sqlite3.Error) as exc:
        if conn.in_transaction:
            conn.rollback()
        raise MigrationPreflightError("Migration preflight verification failed") from exc


def create_verified_backup(
    source: sqlite3.Connection,
    source_path: Path,
    destination: Path,
    metadata_path: Path,
    *,
    preflight: MigrationPreflightReport,
    created_at: str,
) -> MigrationBackupMetadata:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MigrationBackupError("Backup directory is not writable") from exc
    _require_unused_backup_paths(destination, metadata_path)
    try:
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        backup_conn = sqlite3.connect(destination)
        try:
            source.backup(backup_conn)
            backup_conn.commit()
        finally:
            backup_conn.close()
    except (OSError, sqlite3.Error) as exc:
        raise MigrationBackupError(
            "SQLite backup creation failed; source was not migrated"
        ) from exc

    verification = _verify_backup(source, destination, preflight)
    source_stat = source_path.stat()
    source_ledger = migration_ledger_rows(source) if preflight.ledger_present else ()
    logical_payload = json.dumps(
        {
            "application_id": preflight.application_id,
            "schema_fingerprint": preflight.schema_fingerprint,
            "page_count": preflight.page_count,
            "page_size": preflight.page_size,
            "ledger": source_ledger,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    metadata = MigrationBackupMetadata(
        safety_version=MIGRATION_SAFETY_VERSION,
        source_database_path=str(source_path),
        source_application_id=preflight.application_id,
        source_schema_fingerprint=preflight.schema_fingerprint,
        source_logical_fingerprint=hashlib.sha256(logical_payload).hexdigest(),
        source_file_size=source_stat.st_size,
        source_mtime_ns=source_stat.st_mtime_ns,
        source_page_count=preflight.page_count,
        source_page_size=preflight.page_size,
        source_ledger_rows=source_ledger,
        backup_destination=str(destination),
        backup_created_at=created_at,
        backup_file_size=destination.stat().st_size,
        backup_sha256=_file_sha256(destination),
        backup_integrity_result=verification[0],
        backup_foreign_key_violation_count=verification[1],
        backup_verified=True,
    )
    try:
        _write_json_exclusive(metadata_path, asdict(metadata))
    except OSError as exc:
        raise MigrationBackupError(
            "Backup was verified but immutable metadata could not be recorded; "
            "migration was not started"
        ) from exc
    return metadata


def inspect_migration_postflight(
    conn: sqlite3.Connection,
    *,
    migration_paths: Sequence[Path],
    expected_application_id: int,
) -> MigrationPostflightReport:
    try:
        integrity = _integrity_result(conn)
        if integrity != "ok":
            raise MigrationPostflightError(f"PRAGMA integrity_check failed: {integrity}")
        foreign_key_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_violations:
            raise MigrationPostflightError("PRAGMA foreign_key_check reported violations")
        foreign_keys_enabled = bool(int(conn.execute("PRAGMA foreign_keys").fetchone()[0]))
        if not foreign_keys_enabled:
            raise MigrationPostflightError("SQLite foreign-key enforcement is disabled")
        application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
        if application_id != expected_application_id:
            raise MigrationPostflightError(
                f"Unexpected postflight application_id: {application_id}"
            )
        verify_migration_history(conn, migration_paths, require_complete=True)
        ledger = migration_ledger_rows(conn)
        if conn.in_transaction:
            raise MigrationPostflightError("Migration left an unfinished transaction")
        return MigrationPostflightReport(
            application_id=application_id,
            ledger_count=len(ledger),
            schema_fingerprint=schema_fingerprint(conn),
            integrity_result=integrity,
            foreign_key_violation_count=0,
            foreign_keys_enabled=True,
        )
    except MigrationPostflightError:
        raise
    except (MigrationHistoryError, sqlite3.Error) as exc:
        raise MigrationPostflightError("Migration postflight verification failed") from exc


def _verify_backup(
    source: sqlite3.Connection,
    backup_path: Path,
    preflight: MigrationPreflightReport,
) -> tuple[str, int]:
    backup = connect_sqlite(backup_path, mode=ConnectionMode.READ_ONLY)
    try:
        integrity = _integrity_result(backup)
        violations = backup.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or violations:
            raise MigrationBackupError("Backup integrity verification failed")
        if int(backup.execute("PRAGMA application_id").fetchone()[0]) != preflight.application_id:
            raise MigrationBackupError("Backup application identity differs from source")
        if schema_fingerprint(backup) != preflight.schema_fingerprint:
            raise MigrationBackupError("Backup schema fingerprint differs from source")
        if preflight.ledger_present:
            if migration_ledger_rows(backup) != migration_ledger_rows(source):
                raise MigrationBackupError("Backup migration ledger differs from source")
        elif _table_exists(backup, "schema_migrations"):
            raise MigrationBackupError("Backup unexpectedly contains a migration ledger")
        return integrity, len(violations)
    finally:
        backup.close()


def _resolve_backup_destination(
    target: Path,
    *,
    timestamp: str,
    backup_path: str | Path | None,
    backup_directory: str | Path | None,
) -> Path:
    if backup_path is not None and backup_directory is not None:
        raise MigrationTargetError("Specify backup_path or backup_directory, not both")
    if backup_path is not None:
        destination = Path(backup_path).expanduser().absolute()
    else:
        directory = (
            Path(backup_directory).expanduser().absolute()
            if backup_directory is not None
            else target.parent / "migration_backups"
        )
        safe_timestamp = timestamp.replace(":", "-").replace("+", "_")
        destination = directory / f"{target.stem}.{safe_timestamp}.backup.sqlite"
    if destination.resolve(strict=False) == target:
        raise MigrationTargetError("Backup destination must differ from migration target")
    return destination


def _require_unused_backup_paths(destination: Path, metadata_path: Path) -> None:
    if _has_symlink_component(destination.absolute()) or _has_symlink_component(
        metadata_path.absolute()
    ):
        raise MigrationBackupError("Backup paths must not contain symbolic links")
    if destination.exists() or metadata_path.exists():
        raise MigrationBackupError("Backup destination or metadata already exists")


def _integrity_result(conn: sqlite3.Connection) -> str:
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    if len(rows) != 1:
        return "; ".join(str(row[0]) for row in rows)
    return str(rows[0][0])


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_exclusive(path: Path, payload: dict[str, object]) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        handle.write("\n")


def _same_file_if_available(first: Path, second: Path) -> bool:
    try:
        return first.resolve() == second.resolve() or os.path.samefile(first, second)
    except OSError:
        return first.resolve() == second.resolve()


def _has_symlink_component(path: Path) -> bool:
    return any(candidate.is_symlink() for candidate in (path, *path.parents))


def _now(clock: Callable[[], str] | None) -> str:
    return clock() if clock is not None else datetime.now(timezone.utc).isoformat()


__all__ = [
    "MIGRATION_SAFETY_VERSION",
    "MigrationBackupError",
    "MigrationBackupMetadata",
    "MigrationPhaseError",
    "MigrationPostflightError",
    "MigrationPostflightReport",
    "MigrationPreflightError",
    "MigrationPreflightReport",
    "MigrationSafetyError",
    "MigrationTargetError",
    "SQLITE_HEADER",
    "SafeMigrationResult",
    "create_verified_backup",
    "inspect_migration_postflight",
    "inspect_migration_preflight",
    "migrate_database_safely",
    "validate_migration_target",
]
