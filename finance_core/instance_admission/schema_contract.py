"""Pinned trusted application schema, compiled independently of any target DB.

No existing-database adoption or migration-history exception is introduced.
Authorizers run only during fixed DDL compilation/initialization and EXPLAIN
trigger checks; ordinary production connections never receive an authorizer.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from finance_core.staging_guard import StagingDatabaseError


class SchemaContractError(StagingDatabaseError):
    """The pinned application extension or native Core proof is invalid."""


@dataclass(frozen=True)
class ApplicationSchemaContract:
    """Fixed trusted declarations for new ordinary tables and their objects."""

    schema_id: str
    ddl_statements: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}", self.schema_id)
            or type(self.ddl_statements) is not tuple
            or len(self.ddl_statements) > 256
            or any(not isinstance(sql, str) or not sql.strip() for sql in self.ddl_statements)
            or sum(len(sql.encode("utf-8")) for sql in self.ddl_statements) > 262144
        ):
            raise ValueError(
                "Application schema requires bounded fixed ID/ordered DDL declarations"
            )


def _json(value: object) -> bytes:
    # Schema payloads can exceed authority JSON's 64 KiB bound. They remain
    # independently canonical and are stored in trusted composition as hashes.
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


class _SchemaReader:
    """Internal fixed metadata reads on the already owned SQLite handle.

    This object is never issued to an application. Call sites below and the
    unchanged migration schema/ledger helpers submit only metadata SELECT and
    PRAGMA reads. Normal production SQL keeps full per-statement admission.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._connection = conn

    def execute(self, sql: str, parameters=()):
        return sqlite3.Connection.execute(self._connection, sql, parameters)


def _inventory(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Seal every main object, including native SQLite internal structures.

    Data such as sqlite_sequence counters is not schema. Unknown statistics or
    changed internal table/index shape cannot inherit the Core projection.
    """

    def quoted(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    inventory = []
    for row in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"):
        structure: Any = None
        if row[0] == "table":
            indexes = []
            for index in conn.execute(f"PRAGMA index_list({quoted(row[1])})"):
                indexes.append(
                    {
                        "metadata": tuple(index),
                        "columns": [
                            tuple(info)
                            for info in conn.execute(f"PRAGMA index_xinfo({quoted(index[1])})")
                        ],
                    }
                )
            structure = {
                "columns": [
                    tuple(info) for info in conn.execute(f"PRAGMA table_xinfo({quoted(row[1])})")
                ],
                "foreign_keys": [
                    tuple(info)
                    for info in conn.execute(f"PRAGMA foreign_key_list({quoted(row[1])})")
                ],
                "indexes": indexes,
            }
        elif row[0] == "index":
            structure = [
                tuple(info) for info in conn.execute(f"PRAGMA index_xinfo({quoted(row[1])})")
            ]
        inventory.append(
            {"type": row[0], "name": row[1], "table": row[2], "sql": row[3], "structure": structure}
        )
    return inventory


def _ledger_structure(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "columns": [tuple(row) for row in conn.execute("PRAGMA table_xinfo(schema_migrations)")],
        "indexes": [tuple(row) for row in conn.execute("PRAGMA index_list(schema_migrations)")],
        "foreign_keys": [
            tuple(row) for row in conn.execute("PRAGMA foreign_key_list(schema_migrations)")
        ],
    }


def _payload(conn: sqlite3.Connection) -> dict[str, Any]:
    from finance_core.reconciliation.migrations import canonical_schema_payload

    return {
        "schema": canonical_schema_payload(conn),
        "inventory": _inventory(conn),
        "ledger_structure": _ledger_structure(conn),
    }


def _projection(payload: dict[str, Any], names: frozenset[str]) -> dict[str, Any]:
    schema = payload["schema"]
    return {
        "schema": {
            "contract_version": schema["contract_version"],
            "tables": [table for table in schema["tables"] if table["name"] in names],
            "objects": [obj for obj in schema["objects"] if obj["name"] in names],
        },
        "inventory": [obj for obj in payload["inventory"] if obj["name"] in names],
        "ledger_structure": payload["ledger_structure"],
    }


def _deterministic_ledger(conn: sqlite3.Connection) -> tuple[dict[str, object], ...]:
    from finance_core.reconciliation.migrations import migration_ledger_rows

    return tuple(
        {key: value for key, value in row.items() if key != "applied_at"}
        for row in migration_ledger_rows(conn)
    )


def ledger_sha256(conn: sqlite3.Connection) -> str:
    """Seal every ledger field/row, including the original application timestamps."""
    from finance_core.reconciliation.migrations import migration_ledger_rows

    return _hash(migration_ledger_rows(conn))


@dataclass(frozen=True)
class _Expected:
    binding: dict[str, Any]
    core_json: bytes
    full_json: bytes
    core_names: frozenset[str]
    extension_tables: frozenset[str]
    ledger_json: bytes


def _apply_declarations(
    conn: sqlite3.Connection, schema: ApplicationSchemaContract, core_names: frozenset[str]
) -> frozenset[str]:
    tables: set[str] = set()
    indexes: set[str] = set()
    core_folded = {name.casefold() for name in core_names}
    statement_kind = ""
    create_actions = {
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TRIGGER,
    }

    def authorizer(action, first, second, database, source):
        if database not in {None, "main"}:
            return sqlite3.SQLITE_DENY
        if action in create_actions:
            if first.casefold() in core_folded or first.casefold() == "_staging_authorization":
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_CREATE_TABLE:
                if statement_kind != "TABLE":
                    return sqlite3.SQLITE_DENY
                tables.add(first)
            elif second.casefold() not in {table.casefold() for table in tables}:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_CREATE_INDEX:
                indexes.add(first)
            return sqlite3.SQLITE_OK
        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            # SQLite internally updates its catalog for CREATE. Trigger bodies
            # may write only extension tables; Core writes are always denied.
            if first == "sqlite_master" and source is None:
                return sqlite3.SQLITE_OK
            return (
                sqlite3.SQLITE_OK
                if statement_kind == "EXPLAIN"
                and first.casefold() in {table.casefold() for table in tables}
                else sqlite3.SQLITE_DENY
            )
        if action == sqlite3.SQLITE_REINDEX:
            return (
                sqlite3.SQLITE_OK
                if first.casefold() in {index.casefold() for index in indexes}
                else sqlite3.SQLITE_DENY
            )
        if action == sqlite3.SQLITE_SELECT and statement_kind == "TABLE":
            return sqlite3.SQLITE_DENY
        if action in {
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_RECURSIVE,
        }:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_TRANSACTION:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    conn.set_authorizer(authorizer)
    try:
        for statement in schema.ddl_statements:
            # sqlite3.execute enforces exactly one statement. Declarations have
            # no IF NOT EXISTS exemption, callbacks, PRAGMA or request SQL.
            match = re.match(
                r"\s*CREATE\s+(TABLE|(?:UNIQUE\s+)?INDEX|TRIGGER)\s+", statement, re.IGNORECASE
            )
            if match is None:
                raise SchemaContractError(
                    "Only new ordinary table/index/trigger declarations allowed"
                )
            statement_kind = match.group(1).upper().replace("UNIQUE ", "")
            conn.execute(statement)
        # SQLite defers compiling trigger bodies until a triggering statement.
        # EXPLAIN compiles all three events and column updates without effects.
        statement_kind = "EXPLAIN"
        conn.set_authorizer(None)
        table_columns = {
            table: [
                row[1]
                for row in conn.execute(
                    "PRAGMA table_xinfo(" + '"' + table.replace('"', '""') + '"' + ")"
                )
                if row[6] == 0
            ]
            for table in sorted(tables)
        }
        for table in sorted(tables):
            quoted = '"' + table.replace('"', '""') + '"'
            if any(
                row[2].casefold() not in {table.casefold() for table in tables}
                for row in conn.execute(f"PRAGMA foreign_key_list({quoted})")
            ):
                raise SchemaContractError(
                    "Application foreign keys must reference declared extension tables"
                )
        conn.set_authorizer(authorizer)
        for table in sorted(tables):
            quoted = '"' + table.replace('"', '""') + '"'
            columns = table_columns[table]
            names = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
            placeholders = ",".join("NULL" for _ in columns)
            for sql in (
                f"INSERT INTO {quoted}({names}) VALUES({placeholders})",
                f"UPDATE {quoted} SET "
                + ",".join('"' + name.replace('"', '""') + '"=NULL' for name in columns),
                f"DELETE FROM {quoted}",
            ):
                conn.execute("EXPLAIN " + sql).fetchall()
        return frozenset(tables)
    except sqlite3.Error as exc:
        raise SchemaContractError(
            "Application declarations violate immutable Core boundary"
        ) from exc
    finally:
        conn.set_authorizer(None)


@lru_cache(maxsize=16)
def _compile(migration_identity: str, schema: ApplicationSchemaContract | None) -> _Expected:
    from finance_core.reconciliation.migrations import (
        TEMP_DB_MIGRATION_PATHS,
        apply_migration_paths,
        verify_migration_history,
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
        verify_migration_history(conn, TEMP_DB_MIGRATION_PATHS)
        core = _payload(conn)
        core_names = frozenset(obj["name"] for obj in core["inventory"])
        ledger = _json(_deterministic_ledger(conn))
        extension_tables: frozenset[str] = frozenset()
        if schema is not None:
            conn.execute("BEGIN IMMEDIATE")
            extension_tables = _apply_declarations(conn, schema, core_names)
            conn.commit()
        full = _payload(conn)
        if _json(_projection(full, core_names)) != _json(core):
            raise SchemaContractError("Application declarations changed exact Core projection")
        if _json(_deterministic_ledger(conn)) != ledger:
            raise SchemaContractError("Application declarations changed Core migration ledger")
        return _Expected(
            {
                "schema_id": schema.schema_id if schema else None,
                "ddl_sha256": _hash(schema.ddl_statements if schema else ()),
                "expected_schema_sha256": _hash(full),
            },
            _json(core),
            _json(full),
            core_names,
            extension_tables,
            ledger,
        )
    finally:
        conn.close()


def expected_schema(migration_identity: str, schema: ApplicationSchemaContract | None) -> _Expected:
    if schema is not None and type(schema) is not ApplicationSchemaContract:
        raise SchemaContractError("An immutable ApplicationSchemaContract is required")
    return _compile(migration_identity, schema)


def initialize_application_schema(
    conn: sqlite3.Connection, migration_identity: str, schema: ApplicationSchemaContract | None
) -> None:
    expected = expected_schema(migration_identity, schema)
    if schema is not None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _apply_declarations(conn, schema, expected.core_names)
            validate_application_database(conn, migration_identity, schema, ledger_sha256(conn))
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise


def validate_application_database(
    conn: sqlite3.Connection,
    migration_identity: str,
    schema: ApplicationSchemaContract | None,
    sealed_ledger_sha256: str,
) -> None:
    expected = expected_schema(migration_identity, schema)
    reader: Any = _SchemaReader(conn)
    databases = reader.execute("PRAGMA database_list").fetchall()
    if (
        len([row for row in databases if row[1] == "main"]) != 1
        or any(row[1] not in {"main", "temp"} for row in databases)
        or reader.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE name NOT GLOB 'sqlite_*'"
        ).fetchone()
    ):
        raise SchemaContractError("Application schema forbids attachments and TEMP shadows")
    actual = _payload(reader)
    if (
        _json(actual) != expected.full_json
        or _json(_projection(actual, expected.core_names)) != expected.core_json
        or _json(_deterministic_ledger(reader)) != expected.ledger_json
        or ledger_sha256(reader) != sealed_ledger_sha256
    ):
        raise SchemaContractError("Native Core/application schema or sealed full ledger changed")
