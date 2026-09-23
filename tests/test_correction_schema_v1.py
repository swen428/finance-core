"""Synthetic migration 051 integrity and partial-commit checks."""

from __future__ import annotations

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
        CREATE TABLE schema_migrations(migration_filename TEXT);
        INSERT INTO schema_migrations VALUES ('051_controlled_corrections.sql');
        INSERT INTO transactions VALUES ('txn-1');"""
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
