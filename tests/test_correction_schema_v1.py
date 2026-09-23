"""Synthetic migration 051 integrity and partial-commit checks."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from finance_core.application.correction_schema import (
    CorrectionSchemaError,
    has_committed_correction,
    verify_correction_schema,
)

MIGRATION = Path(__file__).resolve().parents[1] / (
    "finance_core/resources/migrations/051_controlled_corrections.sql"
)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """CREATE TABLE transactions(public_id TEXT PRIMARY KEY);
        CREATE TABLE authoritative_calculation_snapshots(
            snapshot_public_id TEXT PRIMARY KEY,combined_snapshot_hash TEXT,
            created_at TEXT,authorization_reference TEXT,previous_snapshot_public_id TEXT);
        CREATE TABLE d2_posting_attempts(transaction_public_id TEXT,stage TEXT);
        CREATE TABLE schema_migrations(
            migration_id TEXT, migration_filename TEXT,
            migration_sequence INTEGER, checksum_sha256 TEXT);
        CREATE TABLE financial_audit_events(
            aggregate_type TEXT, aggregate_public_id TEXT, event_type TEXT);
        INSERT INTO transactions VALUES ('txn-1');"""
    )
    conn.execute(
        "INSERT INTO schema_migrations VALUES (?,?,?,?)",
        ("051", MIGRATION.name, 51, hashlib.sha256(MIGRATION.read_bytes()).hexdigest()),
    )
    conn.executescript(MIGRATION.read_text("utf-8"))
    return conn


def _anchor(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO correction_targets (
            target_id,route,actor,realm,key_id,source_json,source_hash,original_hash,
            projection_json,projection_hash,created_at_epoch
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "txn-1",
            "text",
            "actor",
            "realm",
            "a" * 64,
            "{}",
            "b" * 64,
            "c" * 64,
            "{}",
            "d" * 64,
            100,
        ),
    )


def test_schema_requires_all_recorded_objects_and_foreign_keys() -> None:
    conn = _db()
    try:
        assert verify_correction_schema(conn)
        assert not has_committed_correction(conn, "txn-1")
        conn.execute("DROP TRIGGER trg_correction_versions_no_update")
        with pytest.raises(CorrectionSchemaError):
            verify_correction_schema(conn)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "column,value",
    (
        ("migration_id", "050"),
        ("migration_filename", "050_wrong.sql"),
        ("migration_sequence", 50),
        ("checksum_sha256", "0" * 64),
    ),
)
def test_schema_refuses_tampered_051_ledger_identity(
    column: str, value: str | int
) -> None:
    conn = _db()
    try:
        conn.execute(f"UPDATE schema_migrations SET {column} = ?", (value,))
        with pytest.raises(CorrectionSchemaError, match="051 identity or checksum"):
            verify_correction_schema(conn)
    finally:
        conn.close()


def test_orphan_correction_audit_still_marks_target_as_corrected() -> None:
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO financial_audit_events VALUES (?,?,?)",
            ("transaction", "txn-1", "transaction_correction_applied"),
        )
        assert not conn.execute("SELECT 1 FROM correction_versions").fetchone()
        assert has_committed_correction(conn, "txn-1")
        assert not has_committed_correction(conn, "other")
    finally:
        conn.close()


def test_missing_051_with_retained_audit_refuses_current_claim() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(
            """CREATE TABLE schema_migrations(
                   migration_id TEXT, migration_filename TEXT,
                   migration_sequence INTEGER, checksum_sha256 TEXT);
               CREATE TABLE financial_audit_events(
                   aggregate_type TEXT, aggregate_public_id TEXT, event_type TEXT);
               INSERT INTO financial_audit_events VALUES
                   ('transaction','txn-1','transaction_correction_applied');"""
        )
        assert not verify_correction_schema(conn)
        with pytest.raises(CorrectionSchemaError, match="audit exists without migration 051"):
            has_committed_correction(conn, "txn-1")
        assert not has_committed_correction(conn, "other")
    finally:
        conn.close()


def test_deleted_051_ledger_row_with_retained_audit_refuses() -> None:
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO financial_audit_events VALUES (?,?,?)",
            ("transaction", "txn-1", "transaction_correction_applied"),
        )
        conn.execute("DELETE FROM schema_migrations WHERE migration_id = '051'")
        with pytest.raises(CorrectionSchemaError, match="without migration 051"):
            has_committed_correction(conn, "txn-1")
    finally:
        conn.close()


def test_missing_ledger_with_correction_schema_refuses() -> None:
    conn = _db()
    try:
        conn.execute("DROP TABLE schema_migrations")
        with pytest.raises(CorrectionSchemaError, match="without migration ledger"):
            verify_correction_schema(conn)
    finally:
        conn.close()


def test_anchor_rejects_replace_ignore_collision_and_update() -> None:
    conn = _db()
    try:
        _anchor(conn)
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO correction_targets SELECT * FROM correction_targets "
                "WHERE target_id = 'txn-1'"
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR IGNORE INTO correction_targets SELECT * FROM correction_targets "
                "WHERE target_id = 'txn-1'"
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE correction_targets SET actor='other' WHERE target_id='txn-1'")
        conn.rollback()
    finally:
        conn.close()


def test_version_cannot_commit_without_matching_authority() -> None:
    conn = _db()
    try:
        _anchor(conn)
        conn.execute(
            """INSERT INTO correction_plans (
                plan_id,target_id,correction_id,authority_id,route,expected_version,
                predecessor_id,predecessor_hash,before_json,before_hash,after_json,after_hash,
                source_hash,reason,actor,realm,key_id,instance_id,created_at_epoch,
                expires_at_epoch,plan_hash
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "plan-1",
                "txn-1",
                "corr-1",
                "auth-1",
                "text",
                0,
                None,
                "d" * 64,
                "{}",
                "e" * 64,
                "{}",
                "f" * 64,
                "b" * 64,
                "fix",
                "actor",
                "realm",
                "a" * 64,
                "instance",
                100,
                700,
                "0" * 64,
            ),
        )
        conn.execute(
            """INSERT INTO correction_versions (
                correction_id,target_id,version,predecessor_version,predecessor_id,
                predecessor_hash,plan_id,authority_id,route,source_hash,before_json,before_hash,
                after_json,after_hash,result_core_json,result_hash,reason,actor,apply_epoch,
                apply_utc,instance_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "corr-1",
                "txn-1",
                1,
                None,
                None,
                "d" * 64,
                "plan-1",
                "auth-1",
                "text",
                "b" * 64,
                "{}",
                "e" * 64,
                "{}",
                "f" * 64,
                "{}",
                "1" * 64,
                "fix",
                "actor",
                110,
                "1970-01-01T00:01:50.000000Z",
                "instance",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.commit()
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM correction_versions").fetchone()[0] == 0
    finally:
        conn.close()
