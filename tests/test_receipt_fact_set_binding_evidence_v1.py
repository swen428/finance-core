"""Migration 037 fact-set binding evidence: schema, triggers, and boundary.

Covers the additive append-only ``receipt_fact_set_binding_evidence`` table and
the ``finance_core.receipt_finalization.persistence`` boundary that owns it:

- the exactly-one-target and discriminator CHECK constraints;
- one binding evidence row per authority record (partial unique indexes);
- append-only no-update / no-delete triggers;
- the four-tuple stability backstop and the IAF registry backstop;
- the boundary's deterministic identity, complete durable field comparison, and
  caller-owned transaction contract;
- the full IAF pipeline binding every authority record (run, snapshot,
  authorization, finalization audit) to the same four-tuple.

Only disposable staging databases are used; ``database/finance.db`` and seed
data are untouched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from finance_core.receipt_finalization import (
    authorize_receipt_finalization,
    finalize_prepared_receipt,
    prepare_receipt_calculation,
)
from finance_core.receipt_finalization.models import ActiveFactSetBinding
from finance_core.receipt_finalization.persistence import (
    BINDING_EVIDENCE_SCHEMA_VERSION,
    BOUND_RECORD_COLUMNS,
    FactSetBindingEvidenceError,
    append_fact_set_binding_evidence,
    derive_binding_evidence_public_id,
    read_fact_set_binding_evidence,
)
from tests.test_iaf_finalization_invariants_v1 import _active_binding, _setup_active_fact_set

TABLE = "receipt_fact_set_binding_evidence"
CREATED_AT = "2026-07-30T00:00:00+00:00"


def _insert_raw(conn: sqlite3.Connection, **overrides: Any) -> None:
    """Direct INSERT used only to prove schema-level constraints."""
    row: dict[str, Any] = {
        "binding_public_id": "rfsb_" + "a" * 32,
        "bound_record_type": "calculation_run",
        "receipt_public_id": "rcpt_x",
        "fact_set_public_id": "rfs_x",
        "fact_set_version": 1,
        "fact_set_input_hash": "1" * 64,
        "fact_set_result_hash": "2" * 64,
        "calculation_run_id": None,
        "calculation_snapshot_public_id": None,
        "finalization_authorization_id": None,
        "finalization_audit_id": None,
        "schema_version": BINDING_EVIDENCE_SCHEMA_VERSION,
        "created_at": CREATED_AT,
    }
    row.update(overrides)
    columns = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO {TABLE} ({columns}) VALUES ({placeholders})",
        tuple(row.values()),
    )


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------


def test_migration_037_creates_append_only_binding_evidence_table(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
        is not None
    )
    triggers = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (TABLE,)
        ).fetchall()
    }
    assert triggers == {
        "trg_fact_set_binding_evidence_no_update",
        "trg_fact_set_binding_evidence_no_delete",
        "trg_fact_set_binding_evidence_no_insert_collision",
        "trg_fact_set_binding_evidence_four_tuple_stable",
        "trg_fact_set_binding_evidence_registry_binding",
    }
    indexes = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND name IS NOT NULL",
            (TABLE,),
        ).fetchall()
    }
    for expected in (
        "idx_fact_set_binding_evidence_run",
        "idx_fact_set_binding_evidence_snapshot",
        "idx_fact_set_binding_evidence_authorization",
        "idx_fact_set_binding_evidence_audit",
        "idx_fact_set_binding_evidence_fact_set",
        "idx_fact_set_binding_evidence_receipt",
    ):
        assert expected in indexes


def test_bound_record_columns_cover_every_schema_discriminator(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """The Python boundary and the SQL CHECK must agree on the target set."""
    conn = migrated_temp_db_connection
    sql = str(
        conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()[0]
    )
    for bound_record_type, column in BOUND_RECORD_COLUMNS.items():
        assert f"'{bound_record_type}'" in sql
        assert column in sql


# ---------------------------------------------------------------------------
# Constraints and triggers (direct SQL: schema-level proof)
# ---------------------------------------------------------------------------


def test_binding_evidence_requires_exactly_one_authority_target(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(conn)  # discriminator set, no FK column populated
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(conn, calculation_run_id="run_x", calculation_snapshot_public_id="snap_x")


def test_binding_evidence_discriminator_must_match_populated_column(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(
            conn,
            bound_record_type="calculation_snapshot",
            calculation_run_id="run_x",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"binding_public_id": "bad_prefix", "calculation_run_id": "run_x"},
        {"fact_set_public_id": "not_rfs", "calculation_run_id": "run_x"},
        {"fact_set_version": 0, "calculation_run_id": "run_x"},
        {"fact_set_version": "1", "calculation_run_id": "run_x"},
        {"fact_set_input_hash": "Z" * 64, "calculation_run_id": "run_x"},
        {"fact_set_result_hash": "2" * 63, "calculation_run_id": "run_x"},
        {"schema_version": "v2", "calculation_run_id": "run_x"},
        {"created_at": "2026-07-30", "calculation_run_id": "run_x"},
    ],
)
def test_binding_evidence_rejects_malformed_material(
    migrated_temp_db_connection: sqlite3.Connection, overrides: dict[str, Any]
) -> None:
    conn = migrated_temp_db_connection
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(conn, **overrides)


def test_binding_evidence_requires_a_persisted_registry_fact_set(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """The registry backstop refuses a four-tuple that was never persisted."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "bindreg")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    binding = _active_binding(conn, ctx)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("BEGIN IMMEDIATE")
        try:
            _insert_raw(
                conn,
                binding_public_id="rfsb_" + "b" * 32,
                bound_record_type="calculation_snapshot",
                calculation_snapshot_public_id=prepared.calculation_snapshot_id,
                receipt_public_id=binding.receipt_public_id,
                fact_set_public_id=binding.fact_set_public_id,
                fact_set_version=binding.fact_set_version + 41,
                fact_set_input_hash=binding.fact_set_input_hash,
                fact_set_result_hash=binding.fact_set_result_hash,
            )
        finally:
            if conn.in_transaction:
                conn.rollback()


def test_binding_evidence_rows_are_append_only(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "bindappend")
    prepare_receipt_calculation(conn, ctx.receipt_public_id)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE {TABLE} SET fact_set_version = fact_set_version + 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"DELETE FROM {TABLE}")


def test_binding_evidence_four_tuple_is_stable_per_fact_set(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A contradictory four-tuple for the same fact set is refused."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "bindstable")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    binding = _active_binding(conn, ctx)

    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_raw(
                conn,
                binding_public_id="rfsb_" + "c" * 32,
                bound_record_type="finalization_authorization",
                finalization_authorization_id=prepared.authorization_id,
                receipt_public_id=binding.receipt_public_id,
                fact_set_public_id=binding.fact_set_public_id,
                fact_set_version=binding.fact_set_version,
                fact_set_input_hash=binding.fact_set_input_hash,
                fact_set_result_hash="9" * 64,
            )
    finally:
        if conn.in_transaction:
            conn.rollback()


# ---------------------------------------------------------------------------
# Persistence boundary
# ---------------------------------------------------------------------------


def test_derive_binding_evidence_public_id_is_deterministic_and_typed() -> None:
    first = derive_binding_evidence_public_id("calculation_run", "calc_x")
    assert first == derive_binding_evidence_public_id("calculation_run", "calc_x")
    assert first != derive_binding_evidence_public_id("calculation_snapshot", "calc_x")
    assert first.startswith("rfsb_")
    with pytest.raises(FactSetBindingEvidenceError):
        derive_binding_evidence_public_id("unknown_record", "calc_x")
    with pytest.raises(FactSetBindingEvidenceError):
        derive_binding_evidence_public_id("calculation_run", "   ")


def test_read_binding_evidence_rejects_unknown_record_type(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    with pytest.raises(FactSetBindingEvidenceError):
        read_fact_set_binding_evidence(
            conn, bound_record_type="unknown_record", bound_record_public_id="x"
        )
    with pytest.raises(FactSetBindingEvidenceError):
        append_fact_set_binding_evidence(
            conn,
            binding=ActiveFactSetBinding(
                receipt_public_id="rcpt_x",
                fact_set_public_id="rfs_x",
                fact_set_version=1,
                fact_set_input_hash="1" * 64,
                fact_set_result_hash="2" * 64,
            ),
            bound_record_type="unknown_record",
            bound_record_public_id="x",
            created_at=CREATED_AT,
        )


def test_append_binding_evidence_is_idempotent_and_conflict_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "bindidem")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    binding = _active_binding(conn, ctx)

    durable = read_fact_set_binding_evidence(
        conn,
        bound_record_type="calculation_snapshot",
        bound_record_public_id=prepared.calculation_snapshot_id,
    )
    assert durable == binding

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-appending identical evidence is a no-op, not a conflict.
        assert append_fact_set_binding_evidence(
            conn,
            binding=binding,
            bound_record_type="calculation_snapshot",
            bound_record_public_id=prepared.calculation_snapshot_id,
            created_at="2027-01-01T00:00:00+00:00",
        ) == derive_binding_evidence_public_id(
            "calculation_snapshot", prepared.calculation_snapshot_id
        )

        # A different four-tuple for the same authority record is a conflict.
        conflicting = ActiveFactSetBinding(
            receipt_public_id=binding.receipt_public_id,
            fact_set_public_id=binding.fact_set_public_id,
            fact_set_version=binding.fact_set_version + 1,
            fact_set_input_hash=binding.fact_set_input_hash,
            fact_set_result_hash=binding.fact_set_result_hash,
        )
        with pytest.raises(FactSetBindingEvidenceError):
            append_fact_set_binding_evidence(
                conn,
                binding=conflicting,
                bound_record_type="calculation_snapshot",
                bound_record_public_id=prepared.calculation_snapshot_id,
                created_at=CREATED_AT,
            )
    finally:
        if conn.in_transaction:
            conn.rollback()


# ---------------------------------------------------------------------------
# End-to-end authority binding
# ---------------------------------------------------------------------------


def test_full_pipeline_binds_every_authority_record_to_one_four_tuple(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "binde2e")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    output = finalize_prepared_receipt(conn, authorization)
    binding = _active_binding(conn, ctx)

    bound = {
        "calculation_run": prepared.calculation_run_public_id,
        "calculation_snapshot": prepared.calculation_snapshot_id,
        "finalization_authorization": prepared.authorization_id,
        "finalization_audit": output.finalization_public_id,
    }
    for bound_record_type, bound_record_public_id in bound.items():
        durable = read_fact_set_binding_evidence(
            conn,
            bound_record_type=bound_record_type,
            bound_record_public_id=bound_record_public_id,
        )
        assert durable == binding, bound_record_type

    assert int(conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]) == len(bound)
    assert (
        int(conn.execute(f"SELECT COUNT(DISTINCT fact_set_result_hash) FROM {TABLE}").fetchone()[0])
        == 1
    )
