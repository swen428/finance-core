"""Read-only integrity gate for the append-only correction schema."""

from __future__ import annotations

import hashlib
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


def _has_correction_objects(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name LIKE 'correction_%' "
        "OR name LIKE 'trg_correction_%' LIMIT 1"
    ).fetchone() is not None


def verify_correction_schema(conn: sqlite3.Connection) -> bool:
    """Return False before 051; fail closed on any recorded schema drift."""
    ledger = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if ledger is None:
        if _has_correction_objects(conn):
            raise CorrectionSchemaError("Correction schema exists without migration ledger")
        return False
    try:
        recorded = conn.execute(
            """SELECT migration_id, migration_filename, migration_sequence,
                      checksum_sha256
               FROM schema_migrations
               WHERE migration_id = '051' OR migration_filename = ?
                  OR migration_sequence = 51""",
            (MIGRATION,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise CorrectionSchemaError("Correction migration ledger is malformed") from exc
    if not recorded:
        if _has_correction_objects(conn):
            raise CorrectionSchemaError("Correction schema exists without migration 051")
        return False
    try:
        sql_bytes = files("finance_core.resources.migrations").joinpath(MIGRATION).read_bytes()
    except OSError as exc:
        raise CorrectionSchemaError("Installed correction migration is missing") from exc
    # Migration 051 uses the ledger's ordinary exact-SQL-byte SHA-256 contract.
    expected_ledger = ("051", MIGRATION, 51, hashlib.sha256(sql_bytes).hexdigest())
    if len(recorded) != 1 or tuple(recorded[0]) != expected_ledger:
        raise CorrectionSchemaError("Correction migration 051 identity or checksum changed")
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
    """Refuse orphan audit evidence; never expose an older value as current."""
    audit_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='financial_audit_events'"
    ).fetchone() is not None
    if audit_exists:
        try:
            audit_present = conn.execute(
                """SELECT 1 FROM financial_audit_events
                   WHERE aggregate_type = 'transaction' AND aggregate_public_id = ?
                     AND event_type = 'transaction_correction_applied' LIMIT 1""",
                (target_id,),
            ).fetchone() is not None
        except sqlite3.Error as exc:
            raise CorrectionSchemaError("Financial audit table is malformed") from exc
    else:
        audit_present = False
    if not verify_correction_schema(conn):
        if audit_present:
            raise CorrectionSchemaError("Correction audit exists without migration 051")
        return False
    if not audit_exists:
        raise CorrectionSchemaError("Financial audit table is missing")
    if conn.execute(
        "SELECT 1 FROM correction_versions WHERE target_id = ? LIMIT 1", (target_id,)
    ).fetchone():
        return True
    return audit_present


def has_correction_history(conn: sqlite3.Connection, target_id: str) -> bool:
    """Compatibility alias for the same committed-history predicate."""
    return has_committed_correction(conn, target_id)
