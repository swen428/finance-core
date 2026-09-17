"""FIN-REM-04: receipt writers fail closed when SQLite foreign keys are off."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from finance_core.parser_proposals.receipt_item_allocation_facts import (
    persist_receipt_item_allocation_facts,
)
from finance_core.receipt_finalization import (
    ActiveFactSetBinding,
    authorize_receipt_finalization,
    finalize_receipt_split,
    prepare_receipt_calculation,
)
from finance_core.receipt_finalization.fact_set_bridge import (
    _build_finalization_input,
    _persist_prepare_authority,
)
from finance_core.receipt_finalization.finalizer import _verify_replay_in_coherent_snapshot
from finance_core.receipt_finalization.models import FinalizationInput
from finance_core.sqlite_connection import ForeignKeysDisabledError
from tests.test_receipt_item_allocation_facts_service_v1 import (
    iaf_command,
    setup_receipt,
)

_WRITER_TABLES = (
    "authoritative_calculation_snapshots",
    "calc_audit_runs",
    "receipt_fact_set_binding_evidence",
    "receipt_finalization_confirmations",
    "receipt_finalization_authorizations",
    "receipt_groups",
    "receipt_group_receipts",
    "transactions",
    "calculation_runs",
    "calculation_participant_shares",
    "settlement_obligations",
    "receipt_finalization_audit",
    "receipt_finalization_idempotency",
    "receipt_finalization_membership_evidence",
    "financial_audit_events",
)


def _setup_active_fact_set(
    conn: sqlite3.Connection, tmp_path: Path, suffix: str
) -> tuple[Any, Any]:
    ctx = setup_receipt(conn, tmp_path, suffix)
    result = persist_receipt_item_allocation_facts(conn, iaf_command(suffix, ctx))
    conn.commit()
    return ctx, result


def _writer_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in _WRITER_TABLES
    }


def _disable_foreign_keys(conn: sqlite3.Connection) -> None:
    assert not conn.in_transaction
    conn.execute("PRAGMA foreign_keys = OFF")
    assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 0


def _assert_refusal_left_database_clean(conn: sqlite3.Connection, before: dict[str, int]) -> None:
    assert not conn.in_transaction
    assert _writer_counts(conn) == before
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_prepare_receipt_calculation_rejects_fk_off_before_any_write(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "fin_rem_04_prepare")
    before = _writer_counts(conn)
    _disable_foreign_keys(conn)

    with pytest.raises(ForeignKeysDisabledError):
        prepare_receipt_calculation(conn, ctx.receipt_public_id)

    _assert_refusal_left_database_clean(conn, before)


def test_persist_prepare_authority_rejects_fk_off_before_transaction(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    before = _writer_counts(conn)
    _disable_foreign_keys(conn)
    binding = ActiveFactSetBinding(
        receipt_public_id="rcpt_fin_rem_04_private_prepare",
        fact_set_public_id="iafs_fin_rem_04_private_prepare",
        fact_set_version=1,
        fact_set_input_hash="1" * 64,
        fact_set_result_hash="2" * 64,
    )

    with pytest.raises(ForeignKeysDisabledError):
        _persist_prepare_authority(
            conn,
            ids={
                "calculation_run_public_id": "calc_fin_rem_04_private_prepare",
                "calculation_snapshot_id": "snap_fin_rem_04_private_prepare",
                "receipt_group_public_id": "rgrp_fin_rem_04_private_prepare",
                "authorization_id": "authz_fin_rem_04_private_prepare",
            },
            binding=binding,
            input_payload={},
            output_payload={},
            currency_contract_version="currency-SGD-v1",
            source_references=(),
            actor_type="system",
            actor_id=None,
            created_at="2026-08-23T00:00:00+00:00",
        )

    _assert_refusal_left_database_clean(conn, before)


def test_authorize_receipt_finalization_rejects_fk_off_before_any_write(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "fin_rem_04_authorize")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    before = _writer_counts(conn)
    _disable_foreign_keys(conn)

    with pytest.raises(ForeignKeysDisabledError):
        authorize_receipt_finalization(conn, prepared, actor_id="owner")

    _assert_refusal_left_database_clean(conn, before)


def test_finalize_receipt_split_rejects_fk_off_before_any_write(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "fin_rem_04_finalize")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorize_receipt_finalization(conn, prepared, actor_id="owner")
    fin_input = _build_finalization_input(prepared, actor_type="human", actor_id="owner")
    before = _writer_counts(conn)
    _disable_foreign_keys(conn)

    with pytest.raises(ForeignKeysDisabledError):
        finalize_receipt_split(conn, fin_input)

    _assert_refusal_left_database_clean(conn, before)
    state = conn.execute(
        "SELECT authorization_state FROM receipt_finalization_authorizations "
        "WHERE authorization_id = ?",
        (prepared.authorization_id,),
    ).fetchone()
    assert state is not None and state[0] == "authorized"


def test_replay_snapshot_guard_rejects_fk_off_before_begin_or_recheck(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    before = _writer_counts(conn)
    _disable_foreign_keys(conn)
    recheck_called = False

    def recheck() -> None:
        nonlocal recheck_called
        recheck_called = True
        return None

    fin_input = FinalizationInput(
        calculation_run_public_id="calc_fin_rem_04_replay",
        receipt_group_public_id="rgrp_fin_rem_04_replay",
        currency="SGD",
        payer_participant_public_id="person_owner",
        calculation_snapshot={
            "participants": ["person_owner"],
            "payer": "person_owner",
            "participant_shares": {"person_owner": "0.00"},
            "receipts": [],
            "settlement_obligations": [],
        },
    )

    with pytest.raises(ForeignKeysDisabledError):
        _verify_replay_in_coherent_snapshot(
            conn,
            fin_input=fin_input,
            fingerprint="3" * 64,
            recheck=recheck,
        )

    assert not recheck_called
    _assert_refusal_left_database_clean(conn, before)
