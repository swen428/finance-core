"""Ledger-managed migration execution for temporary Finance databases.

The migration boundary owns schema DDL, exact-byte checksums, deterministic
ordering, verified legacy adoption, and one transaction per migration.  Normal
application services never call this module implicitly and must not create
schema at runtime.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, overload

from finance_core.resources import (
    migration_preflight_path,
    migration_resource_paths,
    migrations_dir,
)
from finance_core.runtime_paths import require_runtime_root

PROJECT_ROOT = require_runtime_root()
MIGRATIONS_DIR = migrations_dir()
LIVE_DB_PATH = PROJECT_ROOT / "database" / "finance.db"
FINANCE_APPLICATION_ID = 0x46494E33

MIGRATION_007_RECONCILIATION_REVIEW_RESOLUTION = (
    MIGRATIONS_DIR / "007_reconciliation_review_resolution_persistence.sql"
)
MIGRATION_012_RECONCILIATION_APPLY_STATE = (
    MIGRATIONS_DIR / "012_reconciliation_apply_state_persistence.sql"
)
MIGRATION_022_DATABASE_CONFLICT_FINGERPRINTS = (
    MIGRATIONS_DIR / "022_database_conflict_fingerprints.sql"
)
MIGRATION_029_ID = "029"
MIGRATION_029_FILENAME = "029_authoritative_proof_evidence_integrity.sql"
MIGRATION_029_AUTHORITATIVE_PROOF_EVIDENCE_INTEGRITY = MIGRATIONS_DIR / MIGRATION_029_FILENAME
MIGRATION_029_PREFLIGHT_ARTIFACT = migration_preflight_path()
MIGRATION_029_CHECKSUM_CONTRACT_VERSION = "finance-migration-029-artifact-checksum-v1"


@dataclass(frozen=True)
class MigrationPreflightArtifact:
    """Explicit stable-identity binding for a checksum-bound preflight file."""

    migration_id: str
    migration_filename: str
    path: Path


@dataclass(frozen=True)
class MigrationPathManifest(Sequence[Path]):
    """Ordered migration paths plus explicit immutable-artifact bindings."""

    paths: tuple[Path, ...]
    preflight_artifacts: tuple[MigrationPreflightArtifact, ...] = ()

    def __iter__(self) -> Iterator[Path]:
        return iter(self.paths)

    def __len__(self) -> int:
        return len(self.paths)

    @overload
    def __getitem__(self, index: int) -> Path: ...

    @overload
    def __getitem__(self, index: slice) -> MigrationPathManifest: ...

    def __getitem__(self, index: int | slice) -> Path | MigrationPathManifest:
        if isinstance(index, int):
            return self.paths[index]
        selected = self.paths[index]
        selected_filenames = {path.name for path in selected}
        return MigrationPathManifest(
            paths=selected,
            preflight_artifacts=tuple(
                artifact
                for artifact in self.preflight_artifacts
                if artifact.migration_filename in selected_filenames
            ),
        )


TEMP_DB_MIGRATION_PATHS = MigrationPathManifest(
    paths=migration_resource_paths(),
    preflight_artifacts=(
        MigrationPreflightArtifact(
            migration_id=MIGRATION_029_ID,
            migration_filename=MIGRATION_029_FILENAME,
            path=MIGRATION_029_PREFLIGHT_ARTIFACT,
        ),
    ),
)

MIGRATION_RUNNER_VERSION = "finance-migration-ledger-v1"
MIGRATION_LEDGER_TABLE = "schema_migrations"
_MIGRATION_FILENAME = re.compile(r"^(?P<identifier>\d{3,})_[a-z0-9][a-z0-9_]*\.sql$")
_FOREIGN_KEYS_PRAGMA = re.compile(
    r"\bPRAGMA\s+foreign_keys\s*=\s*(?P<value>ON|OFF|1|0)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_INTERNAL_SCHEMA_OBJECTS = frozenset({MIGRATION_LEDGER_TABLE, "_staging_authorization"})

_CREATE_LEDGER_SQL = """
CREATE TABLE schema_migrations (
    migration_id TEXT PRIMARY KEY,
    migration_filename TEXT NOT NULL UNIQUE,
    migration_sequence INTEGER NOT NULL UNIQUE CHECK (migration_sequence > 0),
    checksum_sha256 TEXT NOT NULL CHECK (length(checksum_sha256) = 64),
    schema_fingerprint TEXT NOT NULL CHECK (length(schema_fingerprint) = 64),
    applied_at TEXT NOT NULL,
    runner_version TEXT NOT NULL CHECK (runner_version != ''),
    application_version TEXT,
    adoption_mode TEXT NOT NULL CHECK (adoption_mode IN ('applied', 'verified_legacy'))
)
"""


class MigrationExecutionError(RuntimeError):
    """Stable error raised by the explicit migration-only execution boundary."""


class MigrationHistoryError(MigrationExecutionError):
    """Recorded migration history or the supplied manifest is unsafe."""


class MigrationChecksumError(MigrationHistoryError):
    """An applied migration no longer matches its immutable file bytes."""


class MigrationSchemaDriftError(MigrationHistoryError):
    """The current SQLite schema differs from the recorded deterministic schema."""


@dataclass(frozen=True)
class MigrationSpec:
    migration_id: str
    filename: str
    sequence: int
    checksum_sha256: str
    path: Path
    sql_bytes: bytes
    preflight_path: Path | None = None
    preflight_bytes: bytes | None = None


def migration_file_checksum(
    path: Path,
    *,
    preflight_path: Path | None = None,
) -> str:
    """Return the exact-byte migration checksum.

    Migrations 001--028 retain their historical SQL-only SHA-256 semantics.
    Migration 029 is a versioned bundle whose checksum binds both its exact SQL
    bytes and its exact immutable preflight-artifact bytes.
    """
    migration_id, sequence, filename = _migration_identity(path)
    sql_bytes = _read_artifact_bytes(path, label="migration")
    artifact_bytes = (
        _read_artifact_bytes(preflight_path, label="migration preflight artifact")
        if preflight_path is not None
        else None
    )
    return _migration_checksum(
        migration_id=migration_id,
        sequence=sequence,
        filename=filename,
        sql_bytes=sql_bytes,
        preflight_bytes=artifact_bytes,
    )


def build_migration_manifest(migration_paths: Sequence[Path]) -> tuple[MigrationSpec, ...]:
    """Validate and return an immutable, caller-ordered migration manifest."""
    preflight_bindings = _validated_preflight_bindings(migration_paths)
    specs: list[MigrationSpec] = []
    identifiers: set[str] = set()
    sequences: set[int] = set()
    filenames: set[str] = set()
    previous_sequence = 0
    used_bindings: set[tuple[str, str]] = set()
    for raw_path in migration_paths:
        path = Path(raw_path)
        migration_id, sequence, filename = _migration_identity(path)
        if migration_id in identifiers:
            raise MigrationHistoryError(f"Duplicate migration identifier: {migration_id}")
        if sequence in sequences:
            raise MigrationHistoryError(f"Duplicate migration sequence: {sequence}")
        if filename in filenames:
            raise MigrationHistoryError(f"Duplicate migration filename: {filename}")
        if sequence <= previous_sequence:
            raise MigrationHistoryError("Migration manifest order is not strictly increasing")
        identifiers.add(migration_id)
        sequences.add(sequence)
        filenames.add(filename)
        previous_sequence = sequence
        binding_key = (migration_id, filename)
        binding = preflight_bindings.get(binding_key)
        if sequence == 29 and (
            migration_id != MIGRATION_029_ID or filename != MIGRATION_029_FILENAME
        ):
            raise MigrationHistoryError(
                f"Migration 029 must use exact filename {MIGRATION_029_FILENAME}"
            )
        if sequence == 29 and binding is None:
            raise MigrationHistoryError(
                "Migration 029 requires an explicitly bound preflight artifact"
            )
        preflight_path = binding.path if binding is not None else None
        if binding is not None:
            used_bindings.add(binding_key)
        sql_bytes = _read_artifact_bytes(path, label="migration")
        preflight_bytes = (
            _read_artifact_bytes(preflight_path, label="migration preflight artifact")
            if preflight_path is not None
            else None
        )
        specs.append(
            MigrationSpec(
                migration_id=migration_id,
                filename=filename,
                sequence=sequence,
                checksum_sha256=_migration_checksum(
                    migration_id=migration_id,
                    sequence=sequence,
                    filename=filename,
                    sql_bytes=sql_bytes,
                    preflight_bytes=preflight_bytes,
                ),
                path=path,
                sql_bytes=sql_bytes,
                preflight_path=preflight_path,
                preflight_bytes=preflight_bytes,
            )
        )
    unused_bindings = set(preflight_bindings) - used_bindings
    if unused_bindings:
        raise MigrationHistoryError("Preflight artifact binding does not match the manifest")
    return tuple(specs)


def _migration_identity(path: Path) -> tuple[str, int, str]:
    match = _MIGRATION_FILENAME.fullmatch(path.name)
    if match is None:
        raise MigrationHistoryError(
            f"Migration filename must use a numeric identifier and snake_case name: {path.name}"
        )
    migration_id = match.group("identifier")
    sequence = int(migration_id)
    _require_canonical_migration_identifier(migration_id, sequence)
    return migration_id, sequence, path.name


def _require_canonical_migration_identifier(migration_id: str, sequence: int) -> None:
    canonical_id = _canonical_migration_identifier(sequence)
    if migration_id != canonical_id:
        raise MigrationHistoryError(
            "Migration identifier must use its canonical numeric representation: "
            f"{migration_id} (expected {canonical_id})"
        )


def _canonical_migration_identifier(sequence: int) -> str:
    if sequence < 1:
        raise MigrationHistoryError("Migration identifier must represent a positive sequence")
    return f"{sequence:03d}" if sequence < 1000 else str(sequence)


def _validated_preflight_bindings(
    migration_paths: Sequence[Path],
) -> dict[tuple[str, str], MigrationPreflightArtifact]:
    artifacts = (
        migration_paths.preflight_artifacts
        if isinstance(migration_paths, MigrationPathManifest)
        else ()
    )
    bindings: dict[tuple[str, str], MigrationPreflightArtifact] = {}
    for artifact in artifacts:
        key = (artifact.migration_id, artifact.migration_filename)
        if key != (MIGRATION_029_ID, MIGRATION_029_FILENAME):
            raise MigrationHistoryError(
                "Preflight artifact binding must use migration id 029 and exact filename "
                f"{MIGRATION_029_FILENAME}"
            )
        if key in bindings:
            raise MigrationHistoryError("Duplicate preflight artifact binding for migration 029")
        bindings[key] = artifact
    return bindings


def _read_artifact_bytes(path: Path, *, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise MigrationExecutionError(f"Unable to read {label}: {path}") from exc


def _migration_checksum(
    *,
    migration_id: str,
    sequence: int,
    filename: str,
    sql_bytes: bytes,
    preflight_bytes: bytes | None,
) -> str:
    _require_canonical_migration_identifier(migration_id, sequence)
    if sequence != 29:
        if preflight_bytes is not None:
            raise MigrationHistoryError(
                "Only migration 029 may bind a preflight artifact under this contract"
            )
        return hashlib.sha256(sql_bytes).hexdigest()
    if migration_id != MIGRATION_029_ID or filename != MIGRATION_029_FILENAME:
        raise MigrationHistoryError(
            f"Migration 029 must use exact filename {MIGRATION_029_FILENAME}"
        )
    if preflight_bytes is None:
        raise MigrationHistoryError("Migration 029 requires an explicitly bound preflight artifact")
    digest = hashlib.sha256()
    digest.update(MIGRATION_029_CHECKSUM_CONTRACT_VERSION.encode("ascii") + b"\x00")
    for label, value in ((b"sql", sql_bytes), (b"preflight", preflight_bytes)):
        digest.update(len(label).to_bytes(4, "big"))
        digest.update(label)
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def canonical_schema_payload(conn: sqlite3.Connection) -> dict[str, object]:
    """Return stable SQLite metadata for supported schema verification."""
    objects = conn.execute(
        """SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name"""
    ).fetchall()
    tables: list[dict[str, object]] = []
    schema_objects: list[dict[str, object]] = []
    for raw_row in objects:
        row = _row_dict(raw_row, ("type", "name", "tbl_name", "sql"))
        name = str(row["name"])
        if name in _INTERNAL_SCHEMA_OBJECTS:
            continue
        object_type = str(row["type"])
        if object_type == "table":
            tables.append(
                {
                    "name": name,
                    "columns": _pragma_payload(conn, "table_xinfo", name),
                    "foreign_keys": _pragma_payload(conn, "foreign_key_list", name),
                    "indexes": _index_payload(conn, name),
                    "sql": _normalize_sql(row["sql"]),
                }
            )
        elif object_type in {"index", "view", "trigger"}:
            schema_objects.append(
                {
                    "type": object_type,
                    "name": name,
                    "table": row["tbl_name"],
                    "sql": _normalize_sql(row["sql"]),
                }
            )
    return {
        "contract_version": "sqlite-schema-fingerprint-v1",
        "tables": tables,
        "objects": schema_objects,
    }


def schema_fingerprint(conn: sqlite3.Connection) -> str:
    payload = json.dumps(
        canonical_schema_payload(conn),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def apply_migration_paths(
    conn: sqlite3.Connection,
    migration_paths: Sequence[Path],
    *,
    clock: Callable[[], str] | None = None,
    application_version: str | None = None,
) -> None:
    """Verify history, apply pending migrations atomically, and record them."""
    _require_migration_connection(conn)
    specs = build_migration_manifest(migration_paths)
    _ensure_ledger(conn, specs, clock=clock, application_version=application_version)
    applied = _load_ledger(conn)
    _validate_recorded_history(conn, specs, applied)

    applied_by_id = {str(row["migration_id"]): row for row in applied}
    max_sequence = max((int(str(row["migration_sequence"])) for row in applied), default=0)
    for spec in specs:
        if spec.migration_id in applied_by_id:
            continue
        if spec.sequence <= max_sequence:
            raise MigrationHistoryError(
                f"Cannot apply historical migration {spec.filename} after sequence {max_sequence}"
            )
        _apply_spec(
            conn,
            spec,
            clock=clock,
            application_version=application_version,
        )
        max_sequence = spec.sequence

    _validate_recorded_history(conn, specs, _load_ledger(conn))


def apply_migration_path(
    conn: sqlite3.Connection,
    migration_path: Path,
    *,
    clock: Callable[[], str] | None = None,
    application_version: str | None = None,
) -> None:
    apply_migration_paths(
        conn,
        (migration_path,),
        clock=clock,
        application_version=application_version,
    )


def verify_migration_history(
    conn: sqlite3.Connection,
    migration_paths: Sequence[Path],
    *,
    require_complete: bool = True,
) -> None:
    """Read and verify ledger metadata, checksums, order, and schema state."""
    specs = build_migration_manifest(migration_paths)
    if not _table_exists(conn, MIGRATION_LEDGER_TABLE):
        raise MigrationHistoryError("Migration ledger is missing")
    applied = _load_ledger(conn)
    _validate_recorded_history(conn, specs, applied)
    if require_complete:
        if not _is_authoritative_manifest(specs):
            raise MigrationHistoryError(
                "Complete migration verification requires a contiguous manifest starting at 001"
            )
        if len(applied) != len(specs):
            raise MigrationHistoryError("Migration ledger is incomplete for the supplied manifest")


def migration_ledger_rows(conn: sqlite3.Connection) -> tuple[dict[str, object], ...]:
    """Return deterministic sequence-ordered ledger rows for audit tooling."""
    if not _table_exists(conn, MIGRATION_LEDGER_TABLE):
        raise MigrationHistoryError("Migration ledger is missing")
    return tuple(_load_ledger(conn))


def identify_legacy_migration_prefix(
    conn: sqlite3.Connection,
    migration_paths: Sequence[Path],
) -> tuple[MigrationSpec, ...]:
    """Read-only proof that a pre-ledger schema is a supported known prefix."""
    specs = build_migration_manifest(migration_paths)
    if not _is_authoritative_manifest(specs):
        raise MigrationHistoryError(
            "Legacy identification requires a contiguous manifest starting at 001"
        )
    if _table_exists(conn, MIGRATION_LEDGER_TABLE):
        raise MigrationHistoryError("Legacy identification requires a database without a ledger")
    if not canonical_schema_payload(conn)["tables"]:
        return ()
    matched = _match_legacy_prefix(schema_fingerprint(conn), specs)
    if not matched:
        raise MigrationSchemaDriftError(
            "Pre-ledger database schema does not match a supported migration prefix"
        )
    return tuple(spec for spec, _ in matched)


def _ensure_ledger(
    conn: sqlite3.Connection,
    specs: tuple[MigrationSpec, ...],
    *,
    clock: Callable[[], str] | None,
    application_version: str | None,
) -> None:
    if _table_exists(conn, MIGRATION_LEDGER_TABLE):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _table_exists(conn, MIGRATION_LEDGER_TABLE):
            conn.commit()
            return
        current_fingerprint = schema_fingerprint(conn)
        current_has_schema = bool(canonical_schema_payload(conn)["tables"])
        adopted: list[tuple[MigrationSpec, str]] = []
        if current_has_schema:
            if not _is_authoritative_manifest(specs):
                raise MigrationHistoryError(
                    "A pre-ledger database can be adopted only with a contiguous "
                    "manifest starting at 001"
                )
            adopted = _match_legacy_prefix(current_fingerprint, specs)
            if not adopted:
                raise MigrationSchemaDriftError(
                    "Pre-ledger database schema does not match a supported migration prefix"
                )
            for spec, _fingerprint in adopted:
                if spec.preflight_bytes is not None:
                    _run_migration_preflight(conn, spec)

        applied_at = _now(clock)
        conn.execute(_CREATE_LEDGER_SQL)
        for spec, fingerprint in adopted:
            _insert_ledger_row(
                conn,
                spec,
                fingerprint=fingerprint,
                applied_at=applied_at,
                application_version=application_version,
                adoption_mode="verified_legacy",
            )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _match_legacy_prefix(
    expected_fingerprint: str, specs: tuple[MigrationSpec, ...]
) -> list[tuple[MigrationSpec, str]]:
    scratch = sqlite3.connect(":memory:")
    scratch.row_factory = sqlite3.Row
    scratch.execute("PRAGMA foreign_keys = ON")
    matched: list[tuple[MigrationSpec, str]] = []
    try:
        for spec in specs:
            _execute_sql_script(scratch, spec)
            fingerprint = schema_fingerprint(scratch)
            matched.append((spec, fingerprint))
            if fingerprint == expected_fingerprint:
                return matched
    finally:
        scratch.close()
    return []


def _validate_recorded_history(
    conn: sqlite3.Connection,
    specs: tuple[MigrationSpec, ...],
    applied: list[dict[str, object]],
) -> None:
    by_id = {spec.migration_id: spec for spec in specs}
    if _is_authoritative_manifest(specs):
        for index, row in enumerate(applied[: len(specs)]):
            expected = specs[index]
            if str(row["migration_id"]) != expected.migration_id:
                raise MigrationHistoryError("Recorded migration order does not match the manifest")

    for row in applied:
        migration_id = str(row["migration_id"])
        spec = by_id.get(migration_id)
        if spec is None:
            if (
                _is_authoritative_manifest(specs)
                and specs
                and int(str(row["migration_sequence"])) <= specs[-1].sequence
            ):
                raise MigrationHistoryError(
                    f"Previously applied migration is missing from the manifest: {migration_id}"
                )
            continue
        if (
            str(row["migration_filename"]) != spec.filename
            or int(str(row["migration_sequence"])) != spec.sequence
        ):
            raise MigrationHistoryError(f"Recorded metadata changed for migration {migration_id}")
        if str(row["checksum_sha256"]) != spec.checksum_sha256:
            raise MigrationChecksumError(f"Applied migration checksum changed: {spec.filename}")

    if applied:
        recorded = str(applied[-1]["schema_fingerprint"])
        actual = schema_fingerprint(conn)
        if actual != recorded:
            raise MigrationSchemaDriftError(
                "Current schema fingerprint does not match the latest recorded migration"
            )


def _apply_spec(
    conn: sqlite3.Connection,
    spec: MigrationSpec,
    *,
    clock: Callable[[], str] | None,
    application_version: str | None,
) -> None:
    original_fk = int(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    try:
        content = spec.sql_bytes.decode("utf-8")
        statements = _split_sql_statements(content)
        pragmas = [
            statement for statement in statements if _foreign_keys_value(statement) is not None
        ]
        sql_statements = [
            statement for statement in statements if _foreign_keys_value(statement) is None
        ]
        sql_statements = _target_compatible_statements(conn, spec, sql_statements)
        for pragma in pragmas:
            value = _foreign_keys_value(pragma)
            if value == 0:
                conn.execute("PRAGMA foreign_keys = OFF")

        conn.execute("BEGIN IMMEDIATE")
        try:
            if spec.preflight_bytes is not None:
                _run_migration_preflight(conn, spec)
            for statement in sql_statements:
                conn.execute(statement)
            foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise MigrationExecutionError(
                    f"Migration {spec.migration_id} left foreign-key violations"
                )
            fingerprint = schema_fingerprint(conn)
            _insert_ledger_row(
                conn,
                spec,
                fingerprint=fingerprint,
                applied_at=_now(clock),
                application_version=application_version,
                adoption_mode="applied",
            )
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    except (OSError, UnicodeDecodeError, sqlite3.Error) as exc:
        raise MigrationExecutionError(f"Unable to apply migration: {spec.path}") from exc
    finally:
        if not conn.in_transaction:
            conn.execute(f"PRAGMA foreign_keys = {1 if original_fk else 0}")


def _run_migration_preflight(conn: sqlite3.Connection, spec: MigrationSpec) -> None:
    """Execute the exact checksum-bound verifier under the migration write lock."""
    source_bytes = spec.preflight_bytes
    if source_bytes is None:
        return
    artifact = spec.preflight_path or Path(f"<migration-{spec.migration_id}-preflight>")
    try:
        source = source_bytes.decode("utf-8")
        namespace: dict[str, object] = {"__name__": "migration_029_preflight_v1"}
        exec(compile(source, str(artifact), "exec"), namespace)
        verifier = namespace.get("verify_authoritative_state")
        if not callable(verifier):
            raise MigrationExecutionError(
                "Migration preflight artifact has no verify_authoritative_state callable"
            )
        verifier(conn)
    except MigrationExecutionError:
        raise
    except Exception as exc:
        raise MigrationExecutionError(
            f"Migration {spec.migration_id} authoritative integrity preflight failed"
        ) from exc


def _execute_sql_script(conn: sqlite3.Connection, spec: MigrationSpec) -> None:
    """Apply a spec to a scratch database without ledger state."""
    original_fk = int(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    try:
        content = spec.sql_bytes.decode("utf-8")
        statements = _split_sql_statements(content)
        for statement in statements:
            value = _foreign_keys_value(statement)
            if value is not None:
                conn.execute(f"PRAGMA foreign_keys = {value}")
                continue
            conn.execute(statement)
        conn.commit()
    except (OSError, UnicodeDecodeError, sqlite3.Error) as exc:
        if conn.in_transaction:
            conn.rollback()
        raise MigrationExecutionError(f"Unable to inspect legacy migration: {spec.path}") from exc
    finally:
        conn.execute(f"PRAGMA foreign_keys = {1 if original_fk else 0}")


def _target_compatible_statements(
    conn: sqlite3.Connection,
    spec: MigrationSpec,
    statements: list[str],
) -> list[str]:
    """Preserve migration 022 support for PDF-only persistence fixtures."""
    if spec.filename != "022_database_conflict_fingerprints.sql":
        return statements
    if _table_exists(conn, "raw_intake_records"):
        return statements
    return [statement for statement in statements if "raw_intake_records" not in statement]


def _split_sql_statements(script: str) -> tuple[str, ...]:
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    if _strip_sql_comments(buffer).strip():
        raise MigrationExecutionError("Migration SQL ends with an incomplete statement")
    return tuple(statements)


def _strip_sql_comments(value: str) -> str:
    without_blocks = re.sub(r"/\*.*?\*/", "", value, flags=re.DOTALL)
    return re.sub(r"--[^\n]*(?:\n|$)", "", without_blocks)


def _foreign_keys_value(statement: str) -> int | None:
    match = _FOREIGN_KEYS_PRAGMA.search(statement)
    if match is None:
        return None
    return 1 if match.group("value").upper() in {"ON", "1"} else 0


def _insert_ledger_row(
    conn: sqlite3.Connection,
    spec: MigrationSpec,
    *,
    fingerprint: str,
    applied_at: str,
    application_version: str | None,
    adoption_mode: str,
) -> None:
    conn.execute(
        """INSERT INTO schema_migrations (
        migration_id, migration_filename, migration_sequence, checksum_sha256,
        schema_fingerprint, applied_at, runner_version, application_version, adoption_mode
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            spec.migration_id,
            spec.filename,
            spec.sequence,
            spec.checksum_sha256,
            fingerprint,
            applied_at,
            MIGRATION_RUNNER_VERSION,
            application_version,
            adoption_mode,
        ),
    )


def _load_ledger(conn: sqlite3.Connection) -> list[dict[str, object]]:
    if not _table_exists(conn, MIGRATION_LEDGER_TABLE):
        return []
    cursor = conn.execute(
        """SELECT migration_id, migration_filename, migration_sequence,
        checksum_sha256, schema_fingerprint, applied_at, runner_version,
        application_version, adoption_mode
        FROM schema_migrations ORDER BY migration_sequence"""
    )
    columns = tuple(column[0] for column in cursor.description)
    return [_row_dict(row, columns) for row in cursor.fetchall()]


def _is_authoritative_manifest(specs: tuple[MigrationSpec, ...]) -> bool:
    sequences = [spec.sequence for spec in specs]
    if not specs or sequences != list(range(1, len(specs) + 1)):
        return False
    return all(
        spec.migration_id == _canonical_migration_identifier(spec.sequence) for spec in specs
    )


def _pragma_payload(
    conn: sqlite3.Connection, pragma: str, object_name: str
) -> list[dict[str, object]]:
    safe_name = object_name.replace('"', '""')
    cursor = conn.execute(f'PRAGMA {pragma}("{safe_name}")')
    columns = tuple(column[0] for column in cursor.description or ())
    return [_row_dict(row, columns) for row in cursor.fetchall()]


def _index_payload(conn: sqlite3.Connection, table_name: str) -> list[dict[str, object]]:
    indexes = _pragma_payload(conn, "index_list", table_name)
    payload: list[dict[str, object]] = []
    for index in indexes:
        name = str(index["name"])
        payload.append(
            {
                "name": name,
                "unique": index.get("unique"),
                "origin": index.get("origin"),
                "partial": index.get("partial"),
                "columns": _pragma_payload(conn, "index_xinfo", name),
                "sql": _normalize_sql(
                    conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                        (name,),
                    ).fetchone()[0]
                ),
            }
        )
    return sorted(payload, key=lambda item: str(item["name"]))


def _row_dict(row: object, columns: tuple[str, ...]) -> dict[str, object]:
    if isinstance(row, sqlite3.Row):
        return {column: row[column] for column in columns}
    values: tuple[object, ...] = tuple(row)  # type: ignore[arg-type]
    return dict(zip(columns, values, strict=True))


def _normalize_sql(value: object) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).split())


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
        ).fetchone()
        is not None
    )


def _require_migration_connection(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise MigrationExecutionError(
            "Migration execution requires a connection without an active transaction."
        )


def _now(clock: Callable[[], str] | None) -> str:
    return clock() if clock is not None else datetime.now(timezone.utc).isoformat()


__all__ = [
    "LIVE_DB_PATH",
    "FINANCE_APPLICATION_ID",
    "MIGRATION_LEDGER_TABLE",
    "MIGRATION_RUNNER_VERSION",
    "MIGRATION_007_RECONCILIATION_REVIEW_RESOLUTION",
    "MIGRATION_012_RECONCILIATION_APPLY_STATE",
    "MIGRATION_022_DATABASE_CONFLICT_FINGERPRINTS",
    "MIGRATION_029_AUTHORITATIVE_PROOF_EVIDENCE_INTEGRITY",
    "MIGRATION_029_CHECKSUM_CONTRACT_VERSION",
    "MIGRATION_029_FILENAME",
    "MIGRATION_029_ID",
    "MIGRATION_029_PREFLIGHT_ARTIFACT",
    "MIGRATIONS_DIR",
    "MigrationChecksumError",
    "MigrationExecutionError",
    "MigrationHistoryError",
    "MigrationPathManifest",
    "MigrationPreflightArtifact",
    "MigrationSchemaDriftError",
    "MigrationSpec",
    "PROJECT_ROOT",
    "TEMP_DB_MIGRATION_PATHS",
    "apply_migration_path",
    "apply_migration_paths",
    "build_migration_manifest",
    "canonical_schema_payload",
    "migration_file_checksum",
    "migration_ledger_rows",
    "identify_legacy_migration_prefix",
    "schema_fingerprint",
    "verify_migration_history",
]
