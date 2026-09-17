"""Append-only financial audit-chain contract and concurrency tests."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from finance_core.financial_audit import (
    ZERO_AUDIT_HASH,
    AuditChainConflictError,
    AuditEventCommand,
    AuditVerificationError,
    FinancialAuditRepository,
    UnsupportedAuditVersionError,
    append_financial_audit_event,
    append_financial_audit_event_atomically,
    verify_financial_audit_chain,
)
from finance_core.sqlite_connection import connect_sqlite

CREATED_AT = "2026-07-13T04:00:00+00:00"


def _command(**changes: object) -> AuditEventCommand:
    values: dict[str, object] = {
        "event_public_id": "fae-test-1",
        "aggregate_type": "receipt_group",
        "aggregate_public_id": "receipt-group-1",
        "event_type": "receipt_confirmed",
        "event_payload": {"decision": "confirmed", "amount": "12.30"},
        "previous_state": {"status": "pending_confirmation"},
        "new_state": {"status": "confirmed"},
        "actor_type": "human",
        "actor_public_id": "person-owner",
        "authorization_public_id": "auth-1",
        "calculation_snapshot_public_id": "snapshot-1",
        "calculation_snapshot_hash": "a" * 64,
        "source_evidence_references": ("receipt:r1", "attachment:a1"),
        "correlation_public_id": "correlation-1",
        "causation_public_id": "confirmation-1",
        "created_at": CREATED_AT,
    }
    values.update(changes)
    return AuditEventCommand(**values)  # type: ignore[arg-type]


def _append_in_open_transaction(
    conn: sqlite3.Connection,
    command: AuditEventCommand,
):
    conn.execute("BEGIN IMMEDIATE")
    try:
        return append_financial_audit_event(conn, command)
    except Exception:
        conn.rollback()
        raise


def _event_hash_without_persisting(
    conn: sqlite3.Connection,
    command: AuditEventCommand,
) -> str:
    event, _ = _append_in_open_transaction(conn, command)
    conn.rollback()
    return event.event_hash


def test_valid_genesis_event_is_hash_verified_and_persisted(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    event, idempotent = append_financial_audit_event_atomically(
        migrated_temp_db_connection, _command()
    )
    assert idempotent is False
    assert event.sequence_number == 1
    assert event.previous_event_hash == ZERO_AUDIT_HASH
    event.verify()
    result = verify_financial_audit_chain(
        migrated_temp_db_connection,
        aggregate_type=event.aggregate_type,
        aggregate_public_id=event.aggregate_public_id,
    )
    assert result.valid is True
    assert result.event_count == 1
    assert result.legacy_without_chain is False


def test_valid_multi_event_chain_binds_state_and_predecessor(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    first, _ = append_financial_audit_event_atomically(migrated_temp_db_connection, _command())
    second, _ = append_financial_audit_event_atomically(
        migrated_temp_db_connection,
        _command(
            event_public_id="fae-test-2",
            event_type="receipt_finalized",
            event_payload={"transaction_public_id": "txn-1"},
            previous_state={"status": "confirmed"},
            new_state={"status": "settled", "transaction_public_id": "txn-1"},
            causation_public_id="finalization-1",
        ),
    )
    assert second.sequence_number == 2
    assert second.previous_event_hash == first.event_hash
    assert second.previous_state_hash == first.new_state_hash
    result = verify_financial_audit_chain(
        migrated_temp_db_connection,
        aggregate_type="receipt_group",
        aggregate_public_id="receipt-group-1",
    )
    assert result.valid is True
    assert result.event_count == 2


def test_identical_event_material_produces_identical_hash(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    first = _event_hash_without_persisting(migrated_temp_db_connection, _command())
    second = _event_hash_without_persisting(migrated_temp_db_connection, _command())
    assert first == second


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_payload", {"decision": "rejected"}),
        ("actor_public_id", "person-other"),
        ("authorization_public_id", "auth-other"),
        ("source_evidence_references", ("receipt:r2",)),
        ("previous_state", {"status": "draft"}),
        ("new_state", {"status": "rejected"}),
        ("correlation_public_id", "correlation-other"),
        ("causation_public_id", "confirmation-other"),
    ],
)
def test_material_field_change_changes_event_hash(
    migrated_temp_db_connection: sqlite3.Connection,
    field: str,
    value: object,
) -> None:
    base = _event_hash_without_persisting(migrated_temp_db_connection, _command())
    changed = _event_hash_without_persisting(
        migrated_temp_db_connection, _command(**{field: value})
    )
    assert changed != base


def test_changed_previous_event_hash_changes_successor_hash(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    first, _ = append_financial_audit_event_atomically(migrated_temp_db_connection, _command())
    second_command = _command(
        event_public_id="fae-successor",
        event_type="receipt_finalized",
        previous_state={"status": "confirmed"},
        new_state={"status": "settled"},
        causation_public_id="finalization-1",
    )
    successor, _ = _append_in_open_transaction(migrated_temp_db_connection, second_command)
    first_successor_hash = successor.event_hash
    migrated_temp_db_connection.rollback()
    migrated_temp_db_connection.execute("DROP TRIGGER trg_financial_audit_events_no_delete")
    migrated_temp_db_connection.execute(
        "DELETE FROM financial_audit_events WHERE event_public_id = ?",
        (first.event_public_id,),
    )
    migrated_temp_db_connection.commit()
    replacement, _ = append_financial_audit_event_atomically(
        migrated_temp_db_connection,
        _command(
            event_public_id="fae-replacement",
            event_payload={"decision": "confirmed", "note": "changed"},
        ),
    )
    assert replacement.event_hash != first.event_hash
    changed_successor, _ = _append_in_open_transaction(migrated_temp_db_connection, second_command)
    assert changed_successor.event_hash != first_successor_hash
    migrated_temp_db_connection.rollback()


def test_same_operation_replay_is_idempotent(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    first, first_replay = append_financial_audit_event_atomically(
        migrated_temp_db_connection, _command()
    )
    second, second_replay = append_financial_audit_event_atomically(
        migrated_temp_db_connection, _command(created_at="2027-01-01T00:00:00+00:00")
    )
    assert first_replay is False
    assert second_replay is True
    assert second == first
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM financial_audit_events"
        ).fetchone()[0]
        == 1
    )


def test_conflicting_operation_replay_fails_explicitly(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    append_financial_audit_event_atomically(migrated_temp_db_connection, _command())
    with pytest.raises(AuditChainConflictError, match="public ID"):
        append_financial_audit_event_atomically(
            migrated_temp_db_connection,
            _command(event_payload={"decision": "rejected"}),
        )


def test_duplicate_event_id_is_database_enforced(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    event, _ = append_financial_audit_event_atomically(migrated_temp_db_connection, _command())
    migrated_temp_db_connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        FinancialAuditRepository(migrated_temp_db_connection).insert(event)
    migrated_temp_db_connection.rollback()


def test_stale_predecessor_cannot_create_second_successor(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    first, _ = append_financial_audit_event_atomically(migrated_temp_db_connection, _command())
    append_financial_audit_event_atomically(
        migrated_temp_db_connection,
        _command(
            event_public_id="fae-successor-a",
            event_type="receipt_finalized",
            previous_state={"status": "confirmed"},
            new_state={"status": "settled"},
            causation_public_id="finalization-a",
            expected_previous_event_hash=first.event_hash,
        ),
    )
    with pytest.raises(AuditChainConflictError, match="predecessor changed"):
        append_financial_audit_event_atomically(
            migrated_temp_db_connection,
            _command(
                event_public_id="fae-successor-b",
                event_type="receipt_finalized",
                previous_state={"status": "confirmed"},
                new_state={"status": "settled-other"},
                causation_public_id="finalization-b",
                expected_previous_event_hash=first.event_hash,
            ),
        )


def test_database_prevents_update_and_delete(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    event, _ = append_financial_audit_event_atomically(migrated_temp_db_connection, _command())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        migrated_temp_db_connection.execute(
            "UPDATE financial_audit_events SET actor_public_id = 'other' WHERE event_public_id = ?",
            (event.event_public_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        migrated_temp_db_connection.execute(
            "DELETE FROM financial_audit_events WHERE event_public_id = ?",
            (event.event_public_id,),
        )


def test_modified_payload_reports_first_invalid_event(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    first, _ = append_financial_audit_event_atomically(migrated_temp_db_connection, _command())
    migrated_temp_db_connection.execute("DROP TRIGGER trg_financial_audit_events_no_update")
    migrated_temp_db_connection.execute(
        "UPDATE financial_audit_events SET event_payload_json = ? WHERE event_public_id = ?",
        (
            '{"contract_version":"finance-canonical-json-v1","value":{"changed":true}}',
            first.event_public_id,
        ),
    )
    migrated_temp_db_connection.commit()
    result = verify_financial_audit_chain(
        migrated_temp_db_connection,
        aggregate_type="receipt_group",
        aggregate_public_id="receipt-group-1",
    )
    assert result.valid is False
    assert result.first_invalid_event_public_id == first.event_public_id
    assert result.first_invalid_sequence_number == 1
    assert result.reason == "Audit event hash mismatch"


def test_deleted_middle_event_is_detected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    append_financial_audit_event_atomically(migrated_temp_db_connection, _command())
    second, _ = append_financial_audit_event_atomically(
        migrated_temp_db_connection,
        _command(
            event_public_id="fae-middle",
            event_type="receipt_confirmed_v2",
            previous_state={"status": "confirmed"},
            new_state={"status": "reviewed"},
            causation_public_id="review-1",
        ),
    )
    third, _ = append_financial_audit_event_atomically(
        migrated_temp_db_connection,
        _command(
            event_public_id="fae-third",
            event_type="receipt_finalized",
            previous_state={"status": "reviewed"},
            new_state={"status": "settled"},
            causation_public_id="finalization-1",
        ),
    )
    migrated_temp_db_connection.execute("DROP TRIGGER trg_financial_audit_events_no_delete")
    migrated_temp_db_connection.execute(
        "DELETE FROM financial_audit_events WHERE event_public_id = ?",
        (second.event_public_id,),
    )
    migrated_temp_db_connection.commit()
    deleted = verify_financial_audit_chain(
        migrated_temp_db_connection,
        aggregate_type="receipt_group",
        aggregate_public_id="receipt-group-1",
    )
    assert deleted.valid is False
    assert deleted.first_invalid_event_public_id == third.event_public_id
    assert deleted.reason == "Audit sequence is not contiguous"
    with pytest.raises(AuditChainConflictError, match="Cannot append to invalid audit chain"):
        append_financial_audit_event_atomically(
            migrated_temp_db_connection,
            _command(
                event_public_id="fae-after-corruption",
                event_type="receipt_reopened",
                new_state={"status": "reopened"},
                causation_public_id="reopen-1",
            ),
        )


def test_reordered_sequence_is_detected_at_first_changed_event(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    append_financial_audit_event_atomically(migrated_temp_db_connection, _command())
    second, _ = append_financial_audit_event_atomically(
        migrated_temp_db_connection,
        _command(
            event_public_id="fae-second",
            event_type="receipt_finalized",
            previous_state={"status": "confirmed"},
            new_state={"status": "settled"},
            causation_public_id="finalization-1",
        ),
    )
    migrated_temp_db_connection.execute("DROP TRIGGER trg_financial_audit_events_no_update")
    migrated_temp_db_connection.execute(
        "UPDATE financial_audit_events SET sequence_number = 3 WHERE event_public_id = ?",
        (second.event_public_id,),
    )
    migrated_temp_db_connection.commit()
    result = verify_financial_audit_chain(
        migrated_temp_db_connection,
        aggregate_type="receipt_group",
        aggregate_public_id="receipt-group-1",
    )
    assert result.valid is False
    assert result.first_invalid_event_public_id == second.event_public_id


def test_unsupported_audit_version_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    event, _ = append_financial_audit_event_atomically(migrated_temp_db_connection, _command())
    with pytest.raises(UnsupportedAuditVersionError):
        replace(event, audit_schema_version="v999").verify()


def test_stored_unsupported_audit_version_is_first_invalid_event(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    event, _ = append_financial_audit_event_atomically(migrated_temp_db_connection, _command())
    migrated_temp_db_connection.execute("DROP TRIGGER trg_financial_audit_events_no_update")
    migrated_temp_db_connection.execute("PRAGMA ignore_check_constraints = ON")
    migrated_temp_db_connection.execute(
        "UPDATE financial_audit_events SET audit_schema_version = 'v999' WHERE event_public_id = ?",
        (event.event_public_id,),
    )
    migrated_temp_db_connection.commit()
    result = verify_financial_audit_chain(
        migrated_temp_db_connection,
        aggregate_type="receipt_group",
        aggregate_public_id="receipt-group-1",
    )
    assert result.valid is False
    assert result.first_invalid_event_public_id == event.event_public_id
    assert result.reason == "Unsupported audit schema version: v999"


def test_legacy_aggregate_without_chain_is_explicit(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    result = verify_financial_audit_chain(
        migrated_temp_db_connection,
        aggregate_type="transaction",
        aggregate_public_id="legacy-transaction",
    )
    assert result.valid is True
    assert result.event_count == 0
    assert result.legacy_without_chain is True


def test_audit_insert_failure_rolls_back_financial_state_change(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    migrated_temp_db_connection.execute(
        "INSERT INTO receipt_groups (public_id, currency, status) VALUES (?, 'SGD', 'active')",
        ("receipt-group-1",),
    )
    migrated_temp_db_connection.execute(
        """CREATE TRIGGER test_fail_audit_insert
        BEFORE INSERT ON financial_audit_events
        BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"""
    )
    migrated_temp_db_connection.commit()
    migrated_temp_db_connection.execute("BEGIN IMMEDIATE")
    migrated_temp_db_connection.execute(
        "UPDATE receipt_groups SET status = 'calculated' WHERE public_id = ?",
        ("receipt-group-1",),
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected audit failure"):
        append_financial_audit_event(migrated_temp_db_connection, _command())
    migrated_temp_db_connection.rollback()
    assert (
        migrated_temp_db_connection.execute(
            "SELECT status FROM receipt_groups WHERE public_id = ?",
            ("receipt-group-1",),
        ).fetchone()[0]
        == "active"
    )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM financial_audit_events"
        ).fetchone()[0]
        == 0
    )


def test_concurrent_append_has_one_insert_and_one_replay(
    migrated_temp_db_path: Path,
) -> None:
    barrier = Barrier(2)

    def append() -> tuple[bool | None, BaseException | None]:
        conn = connect_sqlite(migrated_temp_db_path)
        try:
            barrier.wait(timeout=10)
            _, idempotent = append_financial_audit_event_atomically(conn, _command())
            return idempotent, None
        except BaseException as exc:
            return None, exc
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result(timeout=15)
            for future in (executor.submit(append), executor.submit(append))
        ]
    assert [error for _, error in outcomes] == [None, None]
    assert sorted(replay for replay, _ in outcomes if replay is not None) == [False, True]

    restarted = connect_sqlite(migrated_temp_db_path)
    try:
        chain = verify_financial_audit_chain(
            restarted,
            aggregate_type="receipt_group",
            aggregate_public_id="receipt-group-1",
        )
        assert chain.valid is True
        assert chain.event_count == 1
        loaded = FinancialAuditRepository(restarted).fetch("fae-test-1")
        assert loaded is not None
        loaded.verify()
    finally:
        restarted.close()


def test_noncanonical_source_references_and_timestamps_are_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    with pytest.raises(AuditVerificationError, match="sequence"):
        append_financial_audit_event_atomically(
            migrated_temp_db_connection,
            _command(source_evidence_references="receipt:r1"),
        )
    with pytest.raises(AuditVerificationError, match="timezone-aware"):
        append_financial_audit_event_atomically(
            migrated_temp_db_connection,
            _command(created_at="2026-07-13T04:00:00"),
        )


# ---------------------------------------------------------------------------
# Round 3 fix F5: the migration 035 append-only collision backstop must keep
# the shared audit API's typed conflict contract for every audit client.
# ---------------------------------------------------------------------------


def test_migration_035_identity_collision_maps_to_typed_conflict(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """A different event ID reusing the aggregate/type/causation identity
    raises AuditChainConflictError with the exact sqlite trigger cause."""
    conn = migrated_temp_db_connection
    append_financial_audit_event_atomically(conn, _command())
    before = conn.execute(
        "SELECT * FROM financial_audit_events ORDER BY sequence_number"
    ).fetchall()

    with pytest.raises(AuditChainConflictError) as excinfo:
        append_financial_audit_event_atomically(
            conn,
            _command(
                event_public_id="fae-test-035-collision",
                event_payload={"decision": "confirmed", "amount": "99.99"},
                previous_state={"status": "confirmed"},
                new_state={"status": "confirmed", "note": "colliding identity"},
            ),
        )

    cause = excinfo.value.__cause__
    assert isinstance(cause, sqlite3.IntegrityError)
    assert getattr(cause, "sqlite_errorname", "") == "SQLITE_CONSTRAINT_TRIGGER"
    assert str(cause) == (
        "UNIQUE audit event identity collision: financial_audit_events "
        "rows are append-only and cannot be replaced"
    )
    assert not conn.in_transaction
    after = conn.execute("SELECT * FROM financial_audit_events ORDER BY sequence_number").fetchall()
    assert after == before


def test_unrelated_trigger_integrity_errors_stay_unmapped(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Only the exact 035 collision message maps to the typed conflict;
    other trigger-raised IntegrityErrors keep their original behavior."""
    conn = migrated_temp_db_connection
    conn.execute(
        """
        CREATE TEMP TRIGGER trg_test_unrelated_audit_block
            BEFORE INSERT ON financial_audit_events
        BEGIN
            SELECT RAISE(ABORT, 'unrelated trigger rejection for testing');
        END
        """
    )
    with pytest.raises(sqlite3.IntegrityError, match="unrelated trigger rejection"):
        append_financial_audit_event_atomically(conn, _command())
    conn.execute("DROP TRIGGER temp.trg_test_unrelated_audit_block")
    assert not conn.in_transaction
