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
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS, apply_migration_paths

MIGRATION = Path(__file__).resolve().parents[1] / (
    "finance_core/resources/migrations/051_controlled_corrections.sql"
)
MIGRATIONS = MIGRATION.parent


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """CREATE TABLE transactions(id INTEGER PRIMARY KEY, public_id TEXT UNIQUE, amount NUMERIC);
        CREATE TABLE authoritative_calculation_snapshots(
            snapshot_public_id TEXT PRIMARY KEY,combined_snapshot_hash TEXT,
            created_at TEXT,authorization_reference TEXT,previous_snapshot_public_id TEXT);
        CREATE TABLE d2_posting_attempts(transaction_public_id TEXT,stage TEXT);
        CREATE TABLE parser_proposal_conversion_audit(transaction_id INTEGER);
        CREATE TABLE receipt_finalization_audit(transaction_public_id TEXT,status TEXT);
        CREATE TABLE reconciliation_review_queue(
            id INTEGER PRIMARY KEY, public_id TEXT NOT NULL UNIQUE,
            run_public_id TEXT, candidate_id TEXT NOT NULL, issue_type TEXT NOT NULL,
            suggested_action TEXT NOT NULL, priority INTEGER NOT NULL,
            statement_transaction_ref TEXT, app_transaction_ref TEXT,
            confidence_score TEXT NOT NULL, reason_codes_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE schema_migrations(
            migration_id TEXT, migration_filename TEXT,
            migration_sequence INTEGER, checksum_sha256 TEXT);
        CREATE TABLE financial_audit_events(
            aggregate_type TEXT, aggregate_public_id TEXT, event_type TEXT);
        INSERT INTO transactions VALUES (1,'txn-1',10);"""
    )
    # The 051 triggers protect the real prerequisite audit tables from
    # migrations 007, 008 and 019, even in this reduced schema fixture.
    for name in (
        "007_reconciliation_review_resolution_persistence.sql",
        "008_reconciliation_apply_results.sql",
        "019_reconciliation_final_mutation_audit.sql",
    ):
        conn.executescript((MIGRATIONS / name).read_text("utf-8"))
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


def _queue_row(conn: sqlite3.Connection) -> tuple[object, ...]:
    conn.execute(
        """INSERT INTO reconciliation_review_queue (
            id, public_id, run_public_id, candidate_id, issue_type,
            suggested_action, priority, statement_transaction_ref,
            app_transaction_ref, confidence_score, reason_codes_json,
            evidence_json, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            1,
            "queue-1",
            None,
            "candidate-1",
            "unmatched",
            "review",
            2,
            None,
            None,
            "0.75",
            '["reason"]',
            '{"source":"original"}',
            "2026-01-01 00:00:00",
            "2026-01-01 00:00:00",
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM reconciliation_review_queue WHERE id = 1").fetchone()
    assert row is not None
    return tuple(row)


def _assert_queue_row(conn: sqlite3.Connection, expected: tuple[object, ...]) -> None:
    rows = conn.execute("SELECT * FROM reconciliation_review_queue ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [expected]


_AUDIT_INSERTS: dict[str, str] = {
    "reconciliation_resolution_decisions": (
        "INSERT INTO reconciliation_resolution_decisions "
        "(id, public_id, review_queue_public_id, decision_action, reviewer) "
        "VALUES (1, 'decision-1', 'queue-1', 'ignore', 'human')"
    ),
    "reconciliation_resolution_results": (
        "INSERT INTO reconciliation_resolution_results "
        "(id, public_id, review_queue_public_id, decision_public_id, success, audit_evidence_json) "
        "VALUES (1, 'result-1', 'queue-1', 'decision-1', 1, '{}')"
    ),
    "reconciliation_apply_results": (
        "INSERT INTO reconciliation_apply_results "
        "(id, apply_id, decision_id, queue_item_id, action, success, payload_json, "
        "audit_evidence_json, fingerprint, applied_at) "
        "VALUES (1, 'apply-1', 'apply-decision-1', 'queue-1', 'ignore', 1, "
        "'{}', '{}', 'fp', '2026-01-01')"
    ),
    "reconciliation_final_mutation_audit": (
        "INSERT INTO reconciliation_final_mutation_audit "
        "(rowid, final_mutation_id, idempotency_key, idempotency_fingerprint, operation_id, "
        "plan_id, proposal_id, guard_decision_proposal_id, guarded_execution_id, "
        "guarded_execution_idempotency_key, human_confirmation_id, actor_type, action, "
        "status, created_at) VALUES (1, 'final-1', 'final-key-1', 'fp', 'op-1', 'plan-1', "
        "'proposal-1', 'guard-1', 'execution-1', 'exec-key-1', 'confirm-1', 'human', "
        "'apply', 'applied', '2026-01-01')"
    ),
}


def _audit_rows(conn: sqlite3.Connection) -> None:
    _queue_row(conn)
    for statement in _AUDIT_INSERTS.values():
        conn.execute(statement)
    conn.commit()


@pytest.mark.parametrize("table", tuple(_AUDIT_INSERTS))
def test_051_reconciliation_audit_rows_are_append_only_and_ignore_replace_safe(
    table: str,
) -> None:
    conn = _db()
    try:
        _audit_rows(conn)
        original = conn.execute(f"SELECT rowid, * FROM {table}").fetchall()
        for statement in (
            f"UPDATE {table} SET rowid = rowid WHERE rowid = 1",
            f"DELETE FROM {table} WHERE rowid = 1",
            _AUDIT_INSERTS[table].replace("INSERT INTO", "INSERT OR REPLACE INTO", 1),
            _AUDIT_INSERTS[table].replace("INSERT INTO", "INSERT OR IGNORE INTO", 1),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only|identity collision"):
                conn.execute(statement)
            assert conn.execute(f"SELECT rowid, * FROM {table}").fetchall() == original
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("table", "replacement"),
    [
        ("reconciliation_resolution_decisions", "'decision-1'"),
        ("reconciliation_resolution_results", "'result-1'"),
        ("reconciliation_apply_results", "'apply-1'"),
        ("reconciliation_final_mutation_audit", "'final-1'"),
    ],
)
def test_051_reconciliation_audit_unique_identity_collision_before_replace(
    table: str, replacement: str
) -> None:
    conn = _db()
    try:
        _audit_rows(conn)
        before = conn.execute(f"SELECT rowid, * FROM {table}").fetchall()
        # A second logical row with the same public identity cannot use OR
        # REPLACE to delete the original even when its rowid is different.
        statement = _AUDIT_INSERTS[table].replace("INSERT INTO", "INSERT OR REPLACE INTO", 1)
        statement = statement.replace("VALUES (1,", "VALUES (2,", 1)
        assert replacement in statement
        with pytest.raises(sqlite3.IntegrityError, match="identity collision"):
            conn.execute(statement)
        assert conn.execute(f"SELECT rowid, * FROM {table}").fetchall() == before
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("table", "substitutions"),
    [
        ("reconciliation_resolution_decisions", (("decision-1", "decision-2"),)),
        ("reconciliation_resolution_results", (("result-1", "result-2"),)),
        (
            "reconciliation_apply_results",
            (("apply-1", "apply-2"), ("apply-decision-1", "apply-decision-2")),
        ),
        (
            "reconciliation_final_mutation_audit",
            (("final-1", "final-2"), ("final-key-1", "final-key-2")),
        ),
    ],
)
def test_051_reconciliation_audit_explicit_rowid_collision_is_rejected(
    table: str, substitutions: tuple[tuple[str, str], ...]
) -> None:
    conn = _db()
    try:
        _audit_rows(conn)
        before = conn.execute(f"SELECT rowid, * FROM {table}").fetchall()
        for mode in ("REPLACE", "IGNORE"):
            statement = _AUDIT_INSERTS[table].replace("INSERT INTO", f"INSERT OR {mode} INTO", 1)
            for original, changed in substitutions:
                statement = statement.replace(original, changed)
            with pytest.raises(sqlite3.IntegrityError, match="identity collision"):
                conn.execute(statement)
            assert conn.execute(f"SELECT rowid, * FROM {table}").fetchall() == before
    finally:
        conn.close()


@pytest.mark.parametrize(
    "trigger",
    [
        f"trg_correction_{name}_{operation}"
        for name in (
            "resolution_decisions", "resolution_results", "apply_results", "final_mutation_audit"
        )
        for operation in ("no_update", "no_delete", "no_insert_collision")
    ],
)
def test_051_schema_inventory_requires_each_reconciliation_audit_guard(trigger: str) -> None:
    conn = _db()
    try:
        conn.execute(f"DROP TRIGGER {trigger}")
        with pytest.raises(CorrectionSchemaError, match="inventory"):
            verify_correction_schema(conn)
    finally:
        conn.close()


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
    "trigger",
    (
        "trg_correction_review_queue_source_no_update",
        "trg_correction_review_queue_no_delete",
        "trg_correction_review_queue_no_insert_collision",
    ),
)
def test_schema_requires_review_queue_guards(trigger: str) -> None:
    conn = _db()
    try:
        assert verify_correction_schema(conn)
        conn.execute(f"DROP TRIGGER {trigger}")
        with pytest.raises(CorrectionSchemaError, match="inventory"):
            verify_correction_schema(conn)
    finally:
        conn.close()


def test_schema_refuses_weakened_review_queue_guard() -> None:
    conn = _db()
    try:
        trigger = "trg_correction_review_queue_source_no_update"
        original_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (trigger,),
        ).fetchone()[0]
        assert "OLD.evidence_json IS NOT NEW.evidence_json" in original_sql
        conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute(
            original_sql.replace(
                "OLD.evidence_json IS NOT NEW.evidence_json",
                "OLD.evidence_json IS NOT OLD.evidence_json",
            )
        )
        with pytest.raises(CorrectionSchemaError, match=trigger):
            verify_correction_schema(conn)
    finally:
        conn.close()


def test_review_queue_source_fields_frozen_on_fresh_temp_db(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    assert verify_correction_schema(conn)
    original = _queue_row(conn)
    changes: tuple[tuple[str, object], ...] = (
        ("id", 2),
        ("public_id", "queue-2"),
        ("run_public_id", "run-1"),
        ("candidate_id", "candidate-2"),
        ("issue_type", "ambiguous"),
        ("suggested_action", "ignore"),
        ("priority", 3),
        ("statement_transaction_ref", "statement-1"),
        ("app_transaction_ref", "txn-1"),
        ("confidence_score", "0.90"),
        ("reason_codes_json", '["changed"]'),
        ("evidence_json", '{"source":"changed"}'),
        ("created_at", "2026-01-02 00:00:00"),
    )
    for column, changed in changes:
        with pytest.raises(sqlite3.IntegrityError, match="source fields are immutable"):
            conn.execute(
                f"UPDATE reconciliation_review_queue SET {column} = ? WHERE id = 1",
                (changed,),
            )
        _assert_queue_row(conn, original)

    conn.execute(
        """UPDATE reconciliation_review_queue
           SET status = 'resolved', updated_at = '2026-01-02 00:00:00'
           WHERE id = 1"""
    )
    assert tuple(
        conn.execute(
            "SELECT status, updated_at FROM reconciliation_review_queue WHERE id = 1"
        ).fetchone()
    ) == ("resolved", "2026-01-02 00:00:00")


def test_review_queue_guards_existing_row_on_050_to_051_upgrade(
    temp_db_connection: sqlite3.Connection,
) -> None:
    conn = temp_db_connection
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS[:50])
    original = _queue_row(conn)
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    assert verify_correction_schema(conn)
    _assert_queue_row(conn, original)

    with pytest.raises(sqlite3.IntegrityError, match="source rows are immutable"):
        conn.execute("DELETE FROM reconciliation_review_queue WHERE id = 1")
    _assert_queue_row(conn, original)

    for conflict_mode in ("REPLACE", "IGNORE"):
        for row_id, public_id in ((1, "other-queue"), (2, "queue-1")):
            with pytest.raises(sqlite3.IntegrityError, match="identity collision"):
                conn.execute(
                    f"""INSERT OR {conflict_mode} INTO reconciliation_review_queue (
                        id, public_id, candidate_id, issue_type, suggested_action,
                        priority, confidence_score, reason_codes_json, evidence_json
                    ) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        row_id,
                        public_id,
                        "candidate-2",
                        "unmatched",
                        "review",
                        2,
                        "0.50",
                        "[]",
                        "{}",
                    ),
                )
            _assert_queue_row(conn, original)

    conn.execute("UPDATE reconciliation_review_queue SET status = 'ignored' WHERE id = 1")
    assert (
        conn.execute("SELECT status FROM reconciliation_review_queue WHERE id = 1").fetchone()[0]
        == "ignored"
    )


@pytest.mark.parametrize("altered_literal", ("'FINALIZED'", "'finalized '"))
def test_schema_refuses_changed_trigger_literal(altered_literal: str) -> None:
    conn = _db()
    try:
        conn.execute("INSERT INTO d2_posting_attempts VALUES ('txn-1', 'finalized')")
        conn.commit()
        assert verify_correction_schema(conn)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE transactions SET public_id = 'changed' WHERE public_id = 'txn-1'")
        conn.rollback()

        trigger = "trg_correction_transactions_no_update"
        original_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (trigger,),
        ).fetchone()[0]
        assert original_sql.count("a.stage = 'finalized'") == 2
        conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute(original_sql.replace("a.stage = 'finalized'", f"a.stage = {altered_literal}"))

        with pytest.raises(CorrectionSchemaError, match=trigger):
            verify_correction_schema(conn)
        conn.execute("UPDATE transactions SET public_id = 'changed' WHERE public_id = 'txn-1'")
        assert conn.execute("SELECT public_id FROM transactions").fetchone()[0] == "changed"
    finally:
        conn.close()


@pytest.mark.parametrize("evidence", ("d2", "text", "receipt", "receipt_replay"))
def test_committed_original_rejects_mutation_and_identity_collisions(evidence: str) -> None:
    conn = _db()
    try:
        conn.execute("PRAGMA recursive_triggers = OFF")
        if evidence == "d2":
            conn.execute("INSERT INTO d2_posting_attempts VALUES ('txn-1', 'finalized')")
        elif evidence == "text":
            conn.execute("INSERT INTO parser_proposal_conversion_audit VALUES (1)")
        else:
            status = "already_finalized" if evidence == "receipt_replay" else "finalized"
            conn.execute("INSERT INTO receipt_finalization_audit VALUES ('txn-1', ?)", (status,))
        conn.execute("INSERT INTO transactions VALUES (2,'other',20)")
        conn.commit()

        statements = (
            "UPDATE transactions SET amount=99 WHERE id=1",
            "DELETE FROM transactions WHERE id=1",
            "INSERT OR REPLACE INTO transactions VALUES (1,'replacement-id',99)",
            "INSERT OR REPLACE INTO transactions VALUES (3,'txn-1',99)",
            "INSERT OR IGNORE INTO transactions VALUES (1,'replacement-id',99)",
            "INSERT OR IGNORE INTO transactions VALUES (3,'txn-1',99)",
            "UPDATE OR REPLACE transactions SET id=1 WHERE id=2",
            "UPDATE OR REPLACE transactions SET public_id='txn-1' WHERE id=2",
        )
        for statement in statements:
            with pytest.raises(sqlite3.IntegrityError, match="immutable|identity collision"):
                conn.execute(statement)
            assert conn.execute(
                "SELECT id,public_id,amount FROM transactions ORDER BY id"
            ).fetchall() == [(1, "txn-1", 10), (2, "other", 20)]

        conn.execute("UPDATE transactions SET amount=21 WHERE id=2")
        assert conn.execute("SELECT amount FROM transactions WHERE id=2").fetchone()[0] == 21
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
def test_schema_refuses_tampered_051_ledger_identity(column: str, value: str | int) -> None:
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
