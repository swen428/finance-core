"""Canonical serialization and authoritative calculation snapshot tests."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest

from finance_core.calculation.authoritative_snapshot import (
    AuthoritativeSnapshotRepository,
    CanonicalSerializationError,
    LegacySnapshotUnverifiedError,
    SnapshotConflictError,
    SnapshotVerificationError,
    UnsupportedSnapshotVersionError,
    build_authoritative_snapshot,
    canonical_json_bytes,
    load_snapshot_for_authoritative_use,
    persist_authoritative_snapshot,
    verify_snapshot_binding,
)
from finance_core.financial_audit import FinancialAuditRepository, verify_financial_audit_chain
from finance_core.sqlite_connection import connect_sqlite

CREATED_AT = "2026-07-13T02:00:00+00:00"


def _snapshot(**changes: object):
    values: dict[str, object] = {
        "snapshot_public_id": "snapshot-v1",
        "calculation_type": "receipt_split",
        "aggregate_public_id": "receipt-group-1",
        "input_payload": {
            "participants": ["person-owner", "person-a"],
            "amount": Decimal("12.30"),
        },
        "output_payload": {
            "total_paid": Decimal("12.30"),
            "participant_shares": {"person-owner": Decimal("6.15"), "person-a": "6.15"},
        },
        "rules_payload": {"rounding": "ROUND_HALF_UP", "minor_units": 2},
        "money_contract_version": "money-v1",
        "currency_contract_version": "currency-SGD-v1",
        "algorithm_version": "receipt-split-v3",
        "source_references": ("receipt:r1", "attachment:a1"),
        "actor_type": "system",
        "actor_public_id": "calculator-service",
        "authorization_reference": "auth-snapshot-1",
        "finalization_status": "finalized",
        "created_at": CREATED_AT,
    }
    values.update(changes)
    return build_authoritative_snapshot(**values)  # type: ignore[arg-type]


def test_canonical_mapping_order_and_nested_order_are_deterministic() -> None:
    first = {"b": [{"z": Decimal("2.00"), "a": 1}], "a": {"y": True, "x": None}}
    second = {"a": {"x": None, "y": True}, "b": [{"a": 1, "z": Decimal("2")}]}
    assert canonical_json_bytes(first) == canonical_json_bytes(second)


def test_lists_and_tuples_share_ordered_array_policy() -> None:
    assert canonical_json_bytes([1, "2", Decimal("3.0")]) == canonical_json_bytes(
        (1, "2", Decimal("3"))
    )
    assert canonical_json_bytes([1, 2]) != canonical_json_bytes([2, 1])


def test_decimal_zero_and_negative_zero_are_canonical() -> None:
    assert canonical_json_bytes(Decimal("0.00")) == canonical_json_bytes(Decimal("-0.000"))
    assert canonical_json_bytes(Decimal("12.300")) == canonical_json_bytes(Decimal("12.3"))


def test_unicode_uses_nfc_normalization() -> None:
    assert canonical_json_bytes({"café": "Å"}) == canonical_json_bytes({"cafe\u0301": "A\u030a"})


def test_unicode_key_collision_after_normalization_is_rejected() -> None:
    with pytest.raises(CanonicalSerializationError, match="duplicate keys"):
        canonical_json_bytes({"café": 1, "cafe\u0301": 2})


def test_timezone_aware_datetimes_normalize_to_utc() -> None:
    local = datetime(2026, 7, 13, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    utc = datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc)
    assert canonical_json_bytes(local) == canonical_json_bytes(utc)


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(CanonicalSerializationError, match="timezone-aware"):
        canonical_json_bytes(datetime(2026, 7, 13, 4, 0))


@pytest.mark.parametrize("value", [1.5, float("nan"), float("inf"), float("-inf")])
def test_all_float_values_are_rejected(value: float) -> None:
    with pytest.raises(CanonicalSerializationError, match="float"):
        canonical_json_bytes(value)


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_non_finite_decimal_is_rejected(value: Decimal) -> None:
    with pytest.raises(CanonicalSerializationError, match="finite"):
        canonical_json_bytes(value)


def test_null_boolean_string_and_number_remain_distinct() -> None:
    encodings = {
        canonical_json_bytes(None),
        canonical_json_bytes(False),
        canonical_json_bytes("0"),
        canonical_json_bytes(0),
        canonical_json_bytes(Decimal("0")),
    }
    assert len(encodings) == 5


def test_unsupported_values_and_non_string_keys_are_rejected() -> None:
    with pytest.raises(CanonicalSerializationError, match="unsupported"):
        canonical_json_bytes({1, 2})
    with pytest.raises(CanonicalSerializationError, match="keys must be strings"):
        canonical_json_bytes({1: "value"})


def test_identical_snapshot_material_produces_identical_hashes() -> None:
    first = _snapshot()
    second = _snapshot(created_at="2027-01-01T00:00:00+00:00")
    assert first.input_hash == second.input_hash
    assert first.output_hash == second.output_hash
    assert first.rules_hash == second.rules_hash
    assert first.combined_snapshot_hash == second.combined_snapshot_hash


def test_changed_input_output_and_rules_change_their_domain_hashes() -> None:
    base = _snapshot()
    changed_input = _snapshot(input_payload={"amount": Decimal("12.31")})
    changed_output = _snapshot(output_payload={"total_paid": Decimal("12.31")})
    changed_rules = _snapshot(rules_payload={"rounding": "ROUND_DOWN"})
    assert changed_input.input_hash != base.input_hash
    assert changed_output.output_hash != base.output_hash
    assert changed_rules.rules_hash != base.rules_hash
    assert (
        len(
            {
                base.combined_snapshot_hash,
                changed_input.combined_snapshot_hash,
                changed_output.combined_snapshot_hash,
                changed_rules.combined_snapshot_hash,
            }
        )
        == 4
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("money_contract_version", "money-v2"),
        ("currency_contract_version", "currency-SGD-v2"),
        ("algorithm_version", "receipt-split-v4"),
        ("calculation_type", "settlement"),
        ("aggregate_public_id", "receipt-group-2"),
        ("source_references", ("receipt:r2",)),
        ("authorization_reference", "auth-other"),
    ],
)
def test_material_contract_or_source_change_changes_combined_hash(
    field: str, value: object
) -> None:
    assert _snapshot(**{field: value}).combined_snapshot_hash != _snapshot().combined_snapshot_hash


def test_source_references_reject_bare_string_and_self_reference() -> None:
    with pytest.raises(SnapshotVerificationError, match="sequence of strings"):
        _snapshot(source_references="receipt:r1")
    with pytest.raises(SnapshotVerificationError, match="cannot reference itself"):
        _snapshot(previous_snapshot_public_id="snapshot-v1")


def test_snapshot_creation_timestamp_must_be_timezone_aware_iso_8601() -> None:
    with pytest.raises(SnapshotVerificationError, match="timezone-aware"):
        _snapshot(created_at="2026-07-13T02:00:00")
    with pytest.raises(SnapshotVerificationError, match="ISO 8601"):
        _snapshot(created_at="not-a-timestamp")


def test_unsupported_snapshot_version_fails_closed() -> None:
    with pytest.raises(UnsupportedSnapshotVersionError):
        _snapshot(snapshot_schema_version="v999")


def test_persist_and_reload_verifies_all_hashes(migrated_temp_db_connection) -> None:
    snapshot = _snapshot()
    persisted, idempotent = persist_authoritative_snapshot(migrated_temp_db_connection, snapshot)
    assert idempotent is False
    assert persisted == snapshot
    loaded = load_snapshot_for_authoritative_use(
        migrated_temp_db_connection, snapshot.snapshot_public_id
    )
    assert loaded == snapshot


def test_identical_replay_is_idempotent(migrated_temp_db_connection) -> None:
    snapshot = _snapshot()
    persist_authoritative_snapshot(migrated_temp_db_connection, snapshot)
    replayed, idempotent = persist_authoritative_snapshot(migrated_temp_db_connection, snapshot)
    assert idempotent is True
    assert replayed == snapshot
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM authoritative_calculation_snapshots"
        ).fetchone()[0]
        == 1
    )


def test_finalized_snapshot_and_audit_event_commit_together(
    migrated_temp_db_connection,
) -> None:
    snapshot = _snapshot()
    persist_authoritative_snapshot(migrated_temp_db_connection, snapshot)
    rows = migrated_temp_db_connection.execute(
        "SELECT event_public_id FROM financial_audit_events "
        "WHERE aggregate_type = 'calculation_snapshot' AND aggregate_public_id = ?",
        (snapshot.snapshot_public_id,),
    ).fetchall()
    assert len(rows) == 1
    event = FinancialAuditRepository(migrated_temp_db_connection).fetch(rows[0]["event_public_id"])
    assert event is not None
    assert event.event_type == "calculation_snapshot_finalized"
    assert event.authorization_public_id == snapshot.authorization_reference
    assert event.calculation_snapshot_hash == snapshot.combined_snapshot_hash
    verification = verify_financial_audit_chain(
        migrated_temp_db_connection,
        aggregate_type="calculation_snapshot",
        aggregate_public_id=snapshot.snapshot_public_id,
    )
    assert verification.valid is True
    assert verification.event_count == 1


def test_draft_snapshot_is_not_a_finalization_event(migrated_temp_db_connection) -> None:
    snapshot = _snapshot(finalization_status="draft")
    persist_authoritative_snapshot(migrated_temp_db_connection, snapshot)
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM financial_audit_events WHERE aggregate_public_id = ?",
            (snapshot.snapshot_public_id,),
        ).fetchone()[0]
        == 0
    )


def test_audit_insert_failure_rolls_back_finalized_snapshot(
    migrated_temp_db_connection,
) -> None:
    snapshot = _snapshot()
    migrated_temp_db_connection.execute(
        """CREATE TRIGGER test_fail_snapshot_audit
        BEFORE INSERT ON financial_audit_events
        BEGIN SELECT RAISE(ABORT, 'injected snapshot audit failure'); END"""
    )
    migrated_temp_db_connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected snapshot audit failure"):
        persist_authoritative_snapshot(migrated_temp_db_connection, snapshot)
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM authoritative_calculation_snapshots"
        ).fetchone()[0]
        == 0
    )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM financial_audit_events"
        ).fetchone()[0]
        == 0
    )


def test_same_public_id_changed_content_is_conflict(migrated_temp_db_connection) -> None:
    persist_authoritative_snapshot(migrated_temp_db_connection, _snapshot())
    with pytest.raises(SnapshotConflictError, match="public ID"):
        persist_authoritative_snapshot(
            migrated_temp_db_connection,
            _snapshot(output_payload={"total_paid": Decimal("99.00")}),
        )


def test_same_content_with_different_public_id_is_conflict(
    migrated_temp_db_connection,
) -> None:
    persist_authoritative_snapshot(migrated_temp_db_connection, _snapshot())
    with pytest.raises(SnapshotConflictError, match="different public ID"):
        persist_authoritative_snapshot(
            migrated_temp_db_connection,
            _snapshot(snapshot_public_id="snapshot-other"),
        )


def test_changed_snapshot_appends_with_previous_reference(migrated_temp_db_connection) -> None:
    first = _snapshot()
    second = _snapshot(
        snapshot_public_id="snapshot-v2",
        previous_snapshot_public_id=first.snapshot_public_id,
        rules_payload={"rounding": "ROUND_DOWN"},
    )
    persist_authoritative_snapshot(migrated_temp_db_connection, first)
    persist_authoritative_snapshot(migrated_temp_db_connection, second)
    loaded = AuthoritativeSnapshotRepository(migrated_temp_db_connection).fetch(
        second.snapshot_public_id
    )
    assert loaded is not None
    assert loaded.previous_snapshot_public_id == first.snapshot_public_id
    assert loaded.rules_hash != first.rules_hash


def test_database_prevents_update_and_delete(migrated_temp_db_connection) -> None:
    snapshot = _snapshot()
    persist_authoritative_snapshot(migrated_temp_db_connection, snapshot)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        migrated_temp_db_connection.execute(
            "UPDATE authoritative_calculation_snapshots SET actor_type = 'human' "
            "WHERE snapshot_public_id = ?",
            (snapshot.snapshot_public_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        migrated_temp_db_connection.execute(
            "DELETE FROM authoritative_calculation_snapshots WHERE snapshot_public_id = ?",
            (snapshot.snapshot_public_id,),
        )


def test_modified_stored_payload_fails_reload_verification(migrated_temp_db_connection) -> None:
    snapshot = _snapshot()
    persist_authoritative_snapshot(migrated_temp_db_connection, snapshot)
    migrated_temp_db_connection.execute("DROP TRIGGER trg_authoritative_snapshots_no_update")
    migrated_temp_db_connection.execute(
        "UPDATE authoritative_calculation_snapshots SET output_payload_json = ? "
        "WHERE snapshot_public_id = ?",
        (
            '{"contract_version":"finance-canonical-json-v1","value":{"changed":true}}',
            snapshot.snapshot_public_id,
        ),
    )
    migrated_temp_db_connection.commit()
    with pytest.raises(SnapshotVerificationError, match="hash mismatch"):
        AuthoritativeSnapshotRepository(migrated_temp_db_connection).fetch(
            snapshot.snapshot_public_id
        )


def test_modified_stored_hash_fails_reload_verification(migrated_temp_db_connection) -> None:
    snapshot = _snapshot()
    persist_authoritative_snapshot(migrated_temp_db_connection, snapshot)
    migrated_temp_db_connection.execute("DROP TRIGGER trg_authoritative_snapshots_no_update")
    migrated_temp_db_connection.execute(
        "UPDATE authoritative_calculation_snapshots SET input_hash = ? "
        "WHERE snapshot_public_id = ?",
        ("f" * 64, snapshot.snapshot_public_id),
    )
    migrated_temp_db_connection.commit()
    with pytest.raises(SnapshotVerificationError, match="hash mismatch"):
        AuthoritativeSnapshotRepository(migrated_temp_db_connection).fetch(
            snapshot.snapshot_public_id
        )


def test_modified_canonical_contract_version_fails_reload_verification(
    migrated_temp_db_connection,
) -> None:
    snapshot = _snapshot()
    persist_authoritative_snapshot(migrated_temp_db_connection, snapshot)
    migrated_temp_db_connection.execute("DROP TRIGGER trg_authoritative_snapshots_no_update")
    migrated_temp_db_connection.execute(
        "UPDATE authoritative_calculation_snapshots SET input_payload_json = ? "
        "WHERE snapshot_public_id = ?",
        (
            '{"contract_version":"finance-canonical-json-v999","value":{}}',
            snapshot.snapshot_public_id,
        ),
    )
    migrated_temp_db_connection.commit()
    with pytest.raises(SnapshotVerificationError, match="unsupported canonical JSON contract"):
        AuthoritativeSnapshotRepository(migrated_temp_db_connection).fetch(
            snapshot.snapshot_public_id
        )


def test_legacy_snapshot_is_explicitly_unverified(migrated_temp_db_connection) -> None:
    migrated_temp_db_connection.execute(
        """INSERT INTO calc_audit_runs
        (run_id, run_type, entity_type, entity_id, rule_version, status, created_at)
        VALUES ('legacy-run', 'receipt_split', 'receipt_group', 'legacy-group',
                'legacy-rules', 'calculated', ?)""",
        (CREATED_AT,),
    )
    migrated_temp_db_connection.execute(
        """INSERT INTO calculation_snapshots
        (snapshot_id, run_id, snapshot_type, snapshot_data, created_at)
        VALUES ('legacy-snapshot', 'legacy-run', 'output_result', '{}', ?)""",
        (CREATED_AT,),
    )
    migrated_temp_db_connection.commit()
    with pytest.raises(LegacySnapshotUnverifiedError):
        load_snapshot_for_authoritative_use(migrated_temp_db_connection, "legacy-snapshot")


def test_finalization_binding_accepts_exact_snapshot(migrated_temp_db_connection) -> None:
    snapshot = _snapshot()
    persist_authoritative_snapshot(migrated_temp_db_connection, snapshot)
    bound = verify_snapshot_binding(
        migrated_temp_db_connection,
        snapshot_public_id=snapshot.snapshot_public_id,
        expected_combined_hash=snapshot.combined_snapshot_hash,
        expected_calculation_type=snapshot.calculation_type,
        expected_aggregate_public_id=snapshot.aggregate_public_id,
        expected_currency_contract_version=snapshot.currency_contract_version,
        expected_authorization_reference=snapshot.authorization_reference,
        expected_output_payload={
            "total_paid": Decimal("12.30"),
            "participant_shares": {"person-owner": Decimal("6.15"), "person-a": "6.15"},
        },
    )
    assert bound == snapshot


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_combined_hash": "f" * 64},
        {"expected_calculation_type": "settlement"},
        {"expected_aggregate_public_id": "other-group"},
        {"expected_currency_contract_version": "currency-SGD-v2"},
        {"expected_authorization_reference": "other-auth"},
        {"expected_output_payload": {"total_paid": Decimal("99.00")}},
    ],
)
def test_finalization_binding_rejects_material_mismatch(
    migrated_temp_db_connection, changes: dict[str, object]
) -> None:
    snapshot = _snapshot()
    persist_authoritative_snapshot(migrated_temp_db_connection, snapshot)
    arguments: dict[str, object] = {
        "snapshot_public_id": snapshot.snapshot_public_id,
        "expected_combined_hash": snapshot.combined_snapshot_hash,
        "expected_calculation_type": snapshot.calculation_type,
        "expected_aggregate_public_id": snapshot.aggregate_public_id,
        "expected_currency_contract_version": snapshot.currency_contract_version,
        "expected_authorization_reference": snapshot.authorization_reference,
        "expected_output_payload": {
            "total_paid": Decimal("12.30"),
            "participant_shares": {"person-owner": Decimal("6.15"), "person-a": "6.15"},
        },
    }
    arguments.update(changes)
    with pytest.raises(SnapshotVerificationError):
        verify_snapshot_binding(migrated_temp_db_connection, **arguments)  # type: ignore[arg-type]


def test_concurrent_snapshot_creation_has_one_insert_and_one_replay(
    migrated_temp_db_path: Path,
) -> None:
    snapshot = _snapshot()
    barrier = Barrier(2)

    def persist() -> tuple[bool | None, BaseException | None]:
        conn = connect_sqlite(migrated_temp_db_path)
        try:
            barrier.wait(timeout=10)
            _, idempotent = persist_authoritative_snapshot(conn, snapshot)
            return idempotent, None
        except BaseException as exc:
            return None, exc
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result(timeout=15)
            for future in (executor.submit(persist), executor.submit(persist))
        ]
    assert [error for _, error in outcomes] == [None, None]
    assert sorted(idempotent for idempotent, _ in outcomes if idempotent is not None) == [
        False,
        True,
    ]

    restarted = connect_sqlite(migrated_temp_db_path)
    try:
        loaded = load_snapshot_for_authoritative_use(restarted, snapshot.snapshot_public_id)
        assert loaded.combined_snapshot_hash == snapshot.combined_snapshot_hash
        assert (
            restarted.execute(
                "SELECT COUNT(*) FROM authoritative_calculation_snapshots"
            ).fetchone()[0]
            == 1
        )
    finally:
        restarted.close()
