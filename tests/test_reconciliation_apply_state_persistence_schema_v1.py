"""Tests for Reconciliation Apply State Persistence Schema v1."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from finance_core.resources import migrations_dir
from tests.conftest import LIVE_DB_PATH, apply_sql, connect_temp_db

MIGRATION_012 = migrations_dir() / "012_reconciliation_apply_state_persistence.sql"


@pytest.fixture()
def migrated_conn(temp_db_path: Path) -> sqlite3.Connection:
    assert temp_db_path != LIVE_DB_PATH
    conn = connect_temp_db(temp_db_path)
    apply_sql(conn, MIGRATION_012)
    conn.commit()
    return conn


def test_tables_exist(migrated_conn: sqlite3.Connection):
    rows = migrated_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert "reconciliation_apply_batches" in names
    assert "reconciliation_apply_batch_state_transitions" in names


def test_apply_batches_columns(migrated_conn: sqlite3.Connection):
    rows = migrated_conn.execute("PRAGMA table_info('reconciliation_apply_batches')").fetchall()
    cols = {r["name"] for r in rows}
    expected = {
        "id",
        "batch_id",
        "current_state",
        "idempotency_key",
        "source",
        "runtime_version",
        "batch_state_control_version",
        "applied_result_json",
        "error_message",
        "created_at",
        "updated_at",
    }
    assert cols == expected


def test_apply_batch_transitions_columns(migrated_conn: sqlite3.Connection):
    rows = migrated_conn.execute(
        "PRAGMA table_info('reconciliation_apply_batch_state_transitions')"
    ).fetchall()
    cols = {r["name"] for r in rows}
    expected = {
        "id",
        "batch_id",
        "previous_state",
        "new_state",
        "reason",
        "transition_at",
        "audit_metadata_json",
        "actor_type",
        "actor_ref",
        "source_ref",
        "created_at",
    }
    assert cols == expected


def test_current_state_rejects_invalid(migrated_conn: sqlite3.Connection):
    allowed = {"pending", "applying", "applied", "failed", "rejected"}
    for state in sorted(allowed):
        migrated_conn.execute(
            "INSERT INTO reconciliation_apply_batches (batch_id, current_state) VALUES (?, ?)",
            (f"batch-{state}", state),
        )
    migrated_conn.rollback()

    invalid_states = ["unknown", "in_progress", "", " APPLIED", "applied "]
    for bad in invalid_states:
        with pytest.raises(sqlite3.IntegrityError):
            migrated_conn.execute(
                "INSERT INTO reconciliation_apply_batches (batch_id, current_state) VALUES (?, ?)",
                (f"batch-{bad}", bad),
            )


def test_new_state_check_constraint_rejects_invalid(migrated_conn: sqlite3.Connection):
    migrated_conn.execute(
        "INSERT INTO reconciliation_apply_batches (batch_id, current_state) "
        "VALUES ('batch-check', 'pending')"
    )
    allowed = {"pending", "applying", "applied", "failed", "rejected"}
    for state in sorted(allowed):
        migrated_conn.execute(
            "INSERT INTO reconciliation_apply_batch_state_transitions "
            "(batch_id, new_state, reason) VALUES (?, ?, ?)",
            ("batch-check", state, f"test-{state}"),
        )
    migrated_conn.rollback()

    invalid_states = ["unknown", "in_progress", "", " APPLIED"]
    for bad in invalid_states:
        with pytest.raises(sqlite3.IntegrityError):
            migrated_conn.execute(
                "INSERT INTO reconciliation_apply_batch_state_transitions "
                "(batch_id, new_state, reason) VALUES (?, ?, ?)",
                ("batch-check", bad, f"test-{bad}"),
            )


def test_previous_state_allows_null(migrated_conn: sqlite3.Connection):
    """previous_state TEXT CHECK allows NULL for first transitions."""
    migrated_conn.execute(
        "INSERT INTO reconciliation_apply_batches (batch_id, current_state) "
        "VALUES ('batch-ps-null', 'pending')"
    )
    migrated_conn.execute(
        "INSERT INTO reconciliation_apply_batch_state_transitions "
        "(batch_id, previous_state, new_state, reason) "
        "VALUES ('batch-ps-null', NULL, 'applying', 'First transition')"
    )
    migrated_conn.commit()

    row = migrated_conn.execute(
        "SELECT previous_state FROM "
        "reconciliation_apply_batch_state_transitions WHERE batch_id = 'batch-ps-null'"
    ).fetchone()
    assert row is not None
    assert row["previous_state"] is None


def test_previous_state_allows_valid_states(migrated_conn: sqlite3.Connection):
    """previous_state CHECK allows all five valid states when non-NULL."""
    migrated_conn.execute(
        "INSERT INTO reconciliation_apply_batches (batch_id, current_state) "
        "VALUES ('batch-ps-valid', 'applied')"
    )
    valid_states = ["pending", "applying", "applied", "failed", "rejected"]
    for state in valid_states:
        migrated_conn.execute(
            "INSERT INTO reconciliation_apply_batch_state_transitions "
            "(batch_id, previous_state, new_state, reason) "
            "VALUES ('batch-ps-valid', ?, ?, ?)",
            (state, "applied" if state != "applied" else "rejected", f"from-{state}"),
        )
    migrated_conn.commit()

    rows = migrated_conn.execute(
        "SELECT previous_state FROM "
        "reconciliation_apply_batch_state_transitions WHERE batch_id = 'batch-ps-valid'"
    ).fetchall()
    assert len(rows) == 5
    seen = {r["previous_state"] for r in rows}
    assert seen == set(valid_states)


def test_previous_state_rejects_invalid(migrated_conn: sqlite3.Connection):
    """previous_state CHECK rejects invalid values."""
    migrated_conn.execute(
        "INSERT INTO reconciliation_apply_batches (batch_id, current_state) "
        "VALUES ('batch-ps-bad', 'pending')"
    )
    invalid_states = ["unknown", "in_progress", "", " APPLIED", "applied "]
    for bad in invalid_states:
        with pytest.raises(sqlite3.IntegrityError):
            migrated_conn.execute(
                "INSERT INTO reconciliation_apply_batch_state_transitions "
                "(batch_id, previous_state, new_state, reason) "
                "VALUES ('batch-ps-bad', ?, 'applying', ?)",
                (bad, f"bad-ps-{bad}"),
            )


def test_batch_id_uniqueness(migrated_conn: sqlite3.Connection):
    migrated_conn.execute(
        "INSERT INTO reconciliation_apply_batches (batch_id, current_state) "
        "VALUES ('batch-uniq', 'pending')"
    )
    migrated_conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        migrated_conn.execute(
            "INSERT INTO reconciliation_apply_batches (batch_id, current_state) "
            "VALUES ('batch-uniq', 'applying')"
        )


def test_idempotency_key_uniqueness_when_present(migrated_conn: sqlite3.Connection):
    """Duplicate idempotency_key (non-NULL) is rejected."""
    migrated_conn.execute(
        "INSERT INTO reconciliation_apply_batches "
        "(batch_id, current_state, idempotency_key) "
        "VALUES ('batch-idem-1', 'pending', 'idem-key-001')"
    )
    migrated_conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        migrated_conn.execute(
            "INSERT INTO reconciliation_apply_batches "
            "(batch_id, current_state, idempotency_key) "
            "VALUES ('batch-idem-2', 'pending', 'idem-key-001')"
        )


def test_idempotency_key_allows_multiple_nulls(migrated_conn: sqlite3.Connection):
    """Multiple rows with NULL idempotency_key are allowed (partial unique index)."""
    migrated_conn.execute(
        "INSERT INTO reconciliation_apply_batches (batch_id, current_state) "
        "VALUES ('batch-null-1', 'pending')"
    )
    migrated_conn.execute(
        "INSERT INTO reconciliation_apply_batches (batch_id, current_state) "
        "VALUES ('batch-null-2', 'applied')"
    )
    migrated_conn.commit()


def test_transition_references_valid_batch(migrated_conn: sqlite3.Connection):
    """A transition row can reference an existing batch_id."""
    migrated_conn.execute(
        "INSERT INTO reconciliation_apply_batches (batch_id, current_state) "
        "VALUES ('batch-ref', 'pending')"
    )
    migrated_conn.execute(
        "INSERT INTO reconciliation_apply_batch_state_transitions "
        "(batch_id, previous_state, new_state, reason) "
        "VALUES ('batch-ref', NULL, 'applying', 'First transition')"
    )
    migrated_conn.commit()

    rows = migrated_conn.execute(
        "SELECT * FROM reconciliation_apply_batch_state_transitions WHERE batch_id = 'batch-ref'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["previous_state"] is None
    assert rows[0]["new_state"] == "applying"


def test_transition_rejects_missing_batch_id(migrated_conn: sqlite3.Connection):
    """Inserting a transition with a non-existent batch_id is rejected
    when foreign keys are enforced."""
    fk = migrated_conn.execute("PRAGMA foreign_keys").fetchone()
    assert fk is not None and fk[0] == 1, "Foreign key enforcement must be ON"

    with pytest.raises(sqlite3.IntegrityError):
        migrated_conn.execute(
            "INSERT INTO reconciliation_apply_batch_state_transitions "
            "(batch_id, new_state, reason) "
            "VALUES ('batch-does-not-exist', 'applying', 'Orphan transition')"
        )


def test_applied_batch_with_applied_result_json(migrated_conn: sqlite3.Connection):
    """An APPLIED batch row can store a serialized result payload."""
    payload = '{"batch_id":"batch-abc","state":"applied","results":[]}'

    migrated_conn.execute(
        "INSERT INTO reconciliation_apply_batches "
        "(batch_id, current_state, applied_result_json) "
        "VALUES ('batch-abc', 'applied', ?)",
        (payload,),
    )
    migrated_conn.commit()

    row = migrated_conn.execute(
        "SELECT * FROM reconciliation_apply_batches WHERE batch_id = 'batch-abc'"
    ).fetchone()
    assert row is not None
    assert row["current_state"] == "applied"
    assert row["applied_result_json"] == payload


def test_duplicate_rejected_transition_preserves_batch_state(
    migrated_conn: sqlite3.Connection,
):
    """A rejected duplicate attempt can be recorded as a transition
    without mutating the stored batch current_state from applied."""
    migrated_conn.execute(
        "INSERT INTO reconciliation_apply_batches "
        "(batch_id, current_state) VALUES ('batch-dup', 'applied')"
    )
    migrated_conn.execute(
        "INSERT INTO reconciliation_apply_batch_state_transitions "
        "(batch_id, previous_state, new_state, reason) "
        "VALUES ('batch-dup', 'pending', 'applied', 'Batch applied successfully')"
    )
    migrated_conn.execute(
        "INSERT INTO reconciliation_apply_batch_state_transitions "
        "(batch_id, previous_state, new_state, reason, audit_metadata_json) "
        "VALUES ('batch-dup', 'applied', 'rejected', "
        "'Duplicate apply blocked', "
        '\'{"idempotent":true,"reason":"duplicate_apply_blocked"}\')'
    )
    migrated_conn.commit()

    batch_row = migrated_conn.execute(
        "SELECT current_state FROM reconciliation_apply_batches WHERE batch_id = 'batch-dup'"
    ).fetchone()
    assert batch_row is not None
    assert batch_row["current_state"] == "applied"

    transitions = migrated_conn.execute(
        "SELECT new_state, reason FROM "
        "reconciliation_apply_batch_state_transitions "
        "WHERE batch_id = 'batch-dup' ORDER BY id"
    ).fetchall()
    assert len(transitions) == 2
    assert transitions[0]["new_state"] == "applied"
    assert transitions[1]["new_state"] == "rejected"
    assert transitions[1]["reason"] == "Duplicate apply blocked"


def test_no_live_database_accessed(migrated_conn: sqlite3.Connection):
    """The test connection must not be the live database."""
    db_file = migrated_conn.execute("PRAGMA database_list").fetchone()
    assert db_file is not None
    file_path = db_file["file"] if db_file["file"] else ""
    assert str(LIVE_DB_PATH) not in file_path
