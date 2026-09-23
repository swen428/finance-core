"""Read-only integrity gate for the append-only correction schema."""

from __future__ import annotations

import re
import sqlite3
from importlib.resources import files

MIGRATION = "051_controlled_corrections.sql"
TABLES = frozenset(
    {
        "correction_targets",
        "correction_plans",
        "correction_versions",
        "correction_authorities",
        "correction_receipt_facts",
    }
)


class CorrectionSchemaError(RuntimeError):
    """The recorded correction schema is incomplete or changed."""


def _expected_objects() -> dict[tuple[str, str], str]:
    sql = files("finance_core.resources.migrations").joinpath(MIGRATION).read_text("utf-8")
    expected: dict[tuple[str, str], str] = {}
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if not sqlite3.complete_statement(statement):
            continue
        match = re.search(
            r"\bCREATE\s+(TABLE|TRIGGER)\s+([a-z][a-z0-9_]*)\b",
            statement,
            flags=re.IGNORECASE,
        )
        if match:
            kind, name = match.group(1).lower(), match.group(2).lower()
            expected[(kind, name)] = statement.strip().rstrip(";")
        statement = ""
    return expected


def _normalize(sql: str) -> str:
    uncommented = re.sub(r"(?m)^\s*--[^\n]*", "", sql)
    return re.sub(r"\s+", " ", uncommented.strip().rstrip(";")).lower()


def verify_correction_schema(conn: sqlite3.Connection) -> bool:
    """Return False before 051; fail closed on any recorded schema drift."""
    ledger = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if ledger is None:
        return False
    recorded = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_filename = ?", (MIGRATION,)
    ).fetchone()
    if recorded is None:
        return False
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise CorrectionSchemaError("Correction foreign-key enforcement is disabled")
    expected = _expected_objects()
    observed = {
        (kind, name): sql
        for kind, name, sql in conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE (type = 'table' AND name LIKE 'correction_%') "
            "OR (type = 'trigger' AND name LIKE 'trg_correction_%')"
        )
    }
    if set(observed) != set(expected):
        raise CorrectionSchemaError("Correction table/trigger inventory has changed")
    for key, sql in expected.items():
        if _normalize(sql) != _normalize(observed[key]):
            raise CorrectionSchemaError(f"Correction schema object changed: {key[1]}")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise CorrectionSchemaError("Correction ledger has a foreign-key violation")
    return True


def has_committed_correction(conn: sqlite3.Connection, target_id: str) -> bool:
    """Check a public transaction ID after verifying the full correction schema."""
    if not verify_correction_schema(conn):
        return False
    return (
        conn.execute(
            "SELECT 1 FROM correction_versions WHERE target_id = ? LIMIT 1", (target_id,)
        ).fetchone()
        is not None
    )


def has_correction_history(conn: sqlite3.Connection, target_id: str) -> bool:
    """Compatibility alias for the same committed-history predicate."""
    return has_committed_correction(conn, target_id)
