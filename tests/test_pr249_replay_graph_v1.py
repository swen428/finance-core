"""PR #249 acceptance-gap regressions: complete durable replay-graph verification.

PR #249 (merge commit ``936eb1b``) claimed complete replay truth verification
(R3-01..R3-05) but verified only a subset of the durable financial graph.
These regressions freeze the closed contract: an idempotent replay must walk
and verify the complete durable graph -- idempotency record, finalization
audit, authorization (consumed), confirmation, calculation run, authoritative
snapshot, fact-set binding evidence, confirmed receipt identity, receipt
group and its exact membership, receipt-scoped participant membership,
canonical transaction, participant shares, settlement obligations, and the
financial audit-chain events -- through every replay entry path, under one
coherent read snapshot, strictly zero-write, and fail closed with a typed
error on any missing, forged, retargeted, or semantically drifted node or
edge, even when hashes still verify.

Direct SQL appears only in explicitly marked forged-corruption fixtures and
SELECT assertions.  Only disposable staging databases are used;
``database/finance.db`` and seed data are untouched.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from finance_core.receipt_finalization import (
    authorize_receipt_finalization,
    finalize_prepared_receipt,
    finalize_receipt_split,
    prepare_receipt_calculation,
)
from finance_core.receipt_finalization.models import (
    FinalizationBlockReason,
    FinalizationIdempotencyError,
)
from tests.conftest import connect_temp_db
from tests.test_iaf_finalization_invariants_v1 import (
    FAIL_CLOSED_ERRORS,
    _counts,
    _setup_active_fact_set,
)
from tests.test_pr249_receipt_scoped_membership_v1 import (
    REPLAY_WRITE_TABLES,
    _lifecycle_state,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _finalized_receipt(
    conn: sqlite3.Connection, tmp_path: Path, suffix: str
) -> tuple[Any, Any, Any]:
    ctx, _ = _setup_active_fact_set(conn, tmp_path, suffix)
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    output = finalize_prepared_receipt(conn, authorization)
    assert output.status == "finalized"
    return ctx, authorization, output


def _assert_replay_refused_zero_write(conn: sqlite3.Connection, authorization: Any) -> None:
    before = _counts(conn, REPLAY_WRITE_TABLES)
    before_state = _lifecycle_state(conn)
    with pytest.raises(FAIL_CLOSED_ERRORS):
        finalize_prepared_receipt(conn, authorization)
    assert _counts(conn, REPLAY_WRITE_TABLES) == before
    # Row counts cannot detect an in-place lifecycle UPDATE.
    assert _lifecycle_state(conn) == before_state
    assert not conn.in_transaction


def _drop_audit_immutability_triggers(conn: sqlite3.Connection) -> None:
    """FORGED-CORRUPTION FIXTURE ONLY: bypass the migration 038 guards."""
    for name in (
        "trg_receipt_finalization_audit_no_update",
        "trg_receipt_finalization_audit_no_delete",
        "trg_receipt_finalization_idempotency_no_update",
        "trg_receipt_finalization_idempotency_no_delete",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def _drop_financial_audit_triggers(conn: sqlite3.Connection) -> None:
    """FORGED-CORRUPTION FIXTURE ONLY: bypass the migration 025 guards."""
    conn.execute("DROP TRIGGER IF EXISTS trg_financial_audit_events_no_update")
    conn.execute("DROP TRIGGER IF EXISTS trg_financial_audit_events_no_delete")


# ---------------------------------------------------------------------------
# Replay entry paths
# ---------------------------------------------------------------------------


def test_same_key_replay_returns_identical_output_with_zero_writes(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    _ctx, authorization, output = _finalized_receipt(conn, tmp_path, "rgsame")
    before = _counts(conn, REPLAY_WRITE_TABLES)
    before_state = _lifecycle_state(conn)
    replay = finalize_prepared_receipt(conn, authorization)
    assert replay.status == "already_finalized"
    assert replay.finalization_public_id == output.finalization_public_id
    assert replay.transaction_public_id == output.transaction_public_id
    assert sorted(replay.settlement_public_ids) == sorted(output.settlement_public_ids)
    assert _counts(conn, REPLAY_WRITE_TABLES) == before
    # An accepted replay is also strictly zero-write, lifecycle state included.
    assert _lifecycle_state(conn) == before_state


def test_different_key_same_content_replay_returns_already_finalized(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """The already-finalized/different-key path must replay, not fail on the key.

    The audit's idempotency key belongs to the durable idempotency record that
    created it, never to the incoming request; a second command with a fresh
    key and identical content must reach the same complete verifier and
    return the durable result.
    """
    conn = migrated_temp_db_connection
    _ctx, authorization, output = _finalized_receipt(conn, tmp_path, "rgkey")
    rebuilt = _rebuild_finalization_input(authorization, idempotency_key="idem_rgkey_fresh_key")
    before = _counts(conn, REPLAY_WRITE_TABLES)
    before_state = _lifecycle_state(conn)
    replay = finalize_receipt_split(conn, rebuilt)
    assert replay.status == "already_finalized"
    assert replay.finalization_public_id == output.finalization_public_id
    assert _counts(conn, REPLAY_WRITE_TABLES) == before
    assert _lifecycle_state(conn) == before_state


def _rebuild_finalization_input(authorization: Any, *, idempotency_key: str) -> Any:
    """Rebuild the bridge's exact FinalizationInput under a fresh idempotency key.

    The content fingerprint deliberately excludes the idempotency key, so a
    fresh key over this identical input is the different-key replay case.
    """
    from finance_core.receipt_finalization.fact_set_bridge import (
        _build_finalization_input as bridge_build_input,
    )

    prepared = dataclasses.replace(authorization.prepared, idempotency_key=idempotency_key)
    return bridge_build_input(
        prepared,
        actor_type=authorization.actor_type,
        actor_id=authorization.actor_id,
    )


def test_replay_after_reconnect_returns_already_finalized(tmp_path: Path) -> None:
    """A replay on a brand-new connection must verify and return the durable result."""
    from tests.conftest import apply_migrations

    db_path = tmp_path / "reconnect.db"
    conn = connect_temp_db(db_path)
    try:
        apply_migrations(conn)
        conn.commit()
        _ctx, authorization, output = _finalized_receipt(conn, tmp_path, "rgconn")
        conn.commit()
    finally:
        conn.close()
    conn2 = connect_temp_db(db_path)
    try:
        replay = finalize_prepared_receipt(conn2, authorization)
        assert replay.status == "already_finalized"
        assert replay.finalization_public_id == output.finalization_public_id
    finally:
        conn2.close()


def test_concurrent_replays_are_coherent(tmp_path: Path) -> None:
    """Two concurrent same-key replays both verify one coherent durable snapshot."""
    from tests.conftest import apply_migrations

    db_path = tmp_path / "concurrent.db"
    conn = connect_temp_db(db_path)
    try:
        apply_migrations(conn)
        conn.commit()
        _ctx, authorization, output = _finalized_receipt(conn, tmp_path, "rgconc")
        conn.commit()
    finally:
        conn.close()

    results: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def replay() -> None:
        worker = connect_temp_db(db_path)
        try:
            barrier.wait(timeout=10)
            out = finalize_prepared_receipt(worker, authorization)
            results.append(out.finalization_public_id)
        except BaseException as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)
        finally:
            worker.close()

    threads = [threading.Thread(target=replay) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    assert results == [output.finalization_public_id, output.finalization_public_id]


# ---------------------------------------------------------------------------
# Consumed authorization and settled group state
# ---------------------------------------------------------------------------


def test_replay_refuses_unconsumed_authorization_state(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: reverting the consumed authorization breaks replay."""
    conn = migrated_temp_db_connection
    _ctx, authorization, _output = _finalized_receipt(conn, tmp_path, "rgauthstate")
    conn.execute(
        "UPDATE receipt_finalization_authorizations SET authorization_state = 'authorized'"
        " WHERE authorization_id = ?",
        (authorization.authorization_id,),
    )
    conn.commit()
    _assert_replay_refused_zero_write(conn, authorization)


def test_replay_refuses_authorization_version_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: a drifted authorization_version breaks replay."""
    conn = migrated_temp_db_connection
    _ctx, authorization, _output = _finalized_receipt(conn, tmp_path, "rgauthver")
    conn.execute(
        "UPDATE receipt_finalization_authorizations SET authorization_version = 'v999'"
        " WHERE authorization_id = ?",
        (authorization.authorization_id,),
    )
    conn.commit()
    _assert_replay_refused_zero_write(conn, authorization)


def test_replay_refuses_authorization_content_hash_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: a drifted authorization content hash breaks replay."""
    conn = migrated_temp_db_connection
    _ctx, authorization, _output = _finalized_receipt(conn, tmp_path, "rgauthhash")
    conn.execute(
        "UPDATE receipt_finalization_authorizations SET content_hash = ?"
        " WHERE authorization_id = ?",
        ("f" * 64, authorization.authorization_id),
    )
    conn.commit()
    _assert_replay_refused_zero_write(conn, authorization)


def test_replay_refuses_authorization_final_total_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: a drifted authorization final_total breaks replay."""
    conn = migrated_temp_db_connection
    _ctx, authorization, _output = _finalized_receipt(conn, tmp_path, "rgauthtotal")
    conn.execute(
        "UPDATE receipt_finalization_authorizations SET final_total = '999.99'"
        " WHERE authorization_id = ?",
        (authorization.authorization_id,),
    )
    conn.commit()
    _assert_replay_refused_zero_write(conn, authorization)


def test_replay_refuses_reverted_receipt_group_status(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: a settled group reverted to 'active' breaks replay."""
    conn = migrated_temp_db_connection
    ctx, authorization, _output = _finalized_receipt(conn, tmp_path, "rggroupstate")
    conn.execute(
        "UPDATE receipt_groups SET status = 'active' WHERE public_id = ?",
        (f"rgrp_{ctx.receipt_public_id}",),
    )
    conn.commit()
    _assert_replay_refused_zero_write(conn, authorization)


def test_replay_refuses_missing_receipt_group_membership_row(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: deleting the group's receipt link breaks replay."""
    conn = migrated_temp_db_connection
    ctx, authorization, _output = _finalized_receipt(conn, tmp_path, "rggrouplink")
    conn.execute(
        "DELETE FROM receipt_group_receipts WHERE receipt_group_id ="
        " (SELECT id FROM receipt_groups WHERE public_id = ?)",
        (f"rgrp_{ctx.receipt_public_id}",),
    )
    conn.commit()
    _assert_replay_refused_zero_write(conn, authorization)


def test_replay_refuses_extra_foreign_receipt_in_group(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: an extra receipt forged into the settled single-receipt
    group breaks replay -- unexpected extra graph rows are corruption."""
    conn = migrated_temp_db_connection
    ctx, authorization, _output = _finalized_receipt(conn, tmp_path, "rgextra")
    conn.execute(
        "INSERT INTO receipts (public_id, merchant, receipt_datetime, gross_amount,"
        " subtotal_amount, net_paid_amount, currency, payer_participant_id,"
        " source_channel, raw_input, status)"
        " VALUES ('r_rgextra_forged', 'Forged', '2026-01-02 12:00:00', 9.99, 9.99, 9.99,"
        " 'SGD', (SELECT id FROM participants WHERE public_id = 'person_owner'),"
        " 'manual_test_case', 'forged', 'confirmed')"
    )
    conn.execute(
        "INSERT INTO receipt_group_receipts (public_id, receipt_group_id, receipt_id,"
        " sequence_number)"
        " VALUES ('rgr_rgextra_forged',"
        " (SELECT id FROM receipt_groups WHERE public_id = ?),"
        " (SELECT id FROM receipts WHERE public_id = 'r_rgextra_forged'), 2)",
        (f"rgrp_{ctx.receipt_public_id}",),
    )
    conn.commit()
    _assert_replay_refused_zero_write(conn, authorization)


# ---------------------------------------------------------------------------
# Confirmation, receipt identity, and calculation-run truth (R3-01, R3-05)
# ---------------------------------------------------------------------------


def test_replay_refuses_confirmation_state_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: a revoked confirmation breaks replay (R3-01)."""
    conn = migrated_temp_db_connection
    _ctx, authorization, _output = _finalized_receipt(conn, tmp_path, "rgconfstate")
    conn.execute(
        "UPDATE receipt_finalization_confirmations SET confirmation_state = 'revoked'"
        " WHERE confirmation_id = ?",
        (authorization.confirmation_id,),
    )
    conn.commit()
    _assert_replay_refused_zero_write(conn, authorization)


def test_replay_refuses_confirmation_content_hash_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: a drifted confirmation content hash breaks replay (R3-01)."""
    conn = migrated_temp_db_connection
    _ctx, authorization, _output = _finalized_receipt(conn, tmp_path, "rgconfhash")
    conn.execute(
        "UPDATE receipt_finalization_confirmations SET content_hash = ? WHERE confirmation_id = ?",
        ("e" * 64, authorization.confirmation_id),
    )
    conn.commit()
    _assert_replay_refused_zero_write(conn, authorization)


def test_replay_refuses_missing_confirmation_row(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: a deleted confirmation row breaks replay (R3-01)."""
    conn = migrated_temp_db_connection
    _ctx, authorization, _output = _finalized_receipt(conn, tmp_path, "rgconfdel")
    # FK enforcement must be bypassed to forge this orphaned-authorization state.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "DELETE FROM receipt_finalization_confirmations WHERE confirmation_id = ?",
        (authorization.confirmation_id,),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    _assert_replay_refused_zero_write(conn, authorization)


def test_replay_refuses_confirmed_receipt_identity_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: a drifted live receipt merchant breaks replay."""
    conn = migrated_temp_db_connection
    ctx, authorization, _output = _finalized_receipt(conn, tmp_path, "rgident")
    # Bypass the migration 035 conversion-bound receipt freeze guards --
    # production code can never do this.
    for name in (
        "trg_receipts_conversion_bound_freeze",
        "trg_receipts_conversion_bound_no_delete",
        "trg_receipts_conversion_bound_no_insert_collision",
        "trg_receipts_conversion_bound_no_update_collision",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute(
        "UPDATE receipts SET merchant = 'FORGED MART' WHERE public_id = ?",
        (ctx.receipt_public_id,),
    )
    conn.commit()
    _assert_replay_refused_zero_write(conn, authorization)


def test_replay_refuses_durable_calculation_run_created_at_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: a drifted calc_audit_runs created_at breaks replay (R3-05)."""
    conn = migrated_temp_db_connection
    _ctx, authorization, _output = _finalized_receipt(conn, tmp_path, "rgruncat")
    conn.execute(
        "UPDATE calc_audit_runs SET created_at = '1999-01-01T00:00:00+00:00' WHERE run_id = ?",
        (authorization.prepared.calculation_run_public_id,),
    )
    conn.commit()
    _assert_replay_refused_zero_write(conn, authorization)


def test_replay_refuses_missing_finalizer_calculation_run_row(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: deleting the finalizer's calculation run breaks replay."""
    conn = migrated_temp_db_connection
    _ctx, authorization, _output = _finalized_receipt(conn, tmp_path, "rgrundel")
    conn.execute("DELETE FROM calculation_participant_shares")
    conn.execute("DELETE FROM settlement_obligations")
    conn.execute(
        "DELETE FROM calculation_runs WHERE public_id = ?",
        (authorization.prepared.calculation_run_public_id,),
    )
    conn.commit()
    _assert_replay_refused_zero_write(conn, authorization)


# ---------------------------------------------------------------------------
# Audit-chain events and idempotency edge (R3-02)
# ---------------------------------------------------------------------------


def test_replay_refuses_missing_financial_audit_events(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: deleting the finalization audit-chain events breaks replay."""
    conn = migrated_temp_db_connection
    _ctx, authorization, output = _finalized_receipt(conn, tmp_path, "rgevents")
    _drop_financial_audit_triggers(conn)
    conn.execute(
        "DELETE FROM financial_audit_events WHERE correlation_public_id = ?",
        (output.finalization_public_id,),
    )
    conn.commit()
    _assert_replay_refused_zero_write(conn, authorization)


def test_replay_refuses_forged_audit_chain_event_identity(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: event presence is not identity.

    Each finalization audit-chain event has a deterministically derived public
    ID.  An event whose ID was retargeted still satisfies a type-existence
    check, so replay must verify the derived identities themselves.
    """
    conn = migrated_temp_db_connection
    _ctx, authorization, output = _finalized_receipt(conn, tmp_path, "rgevid")
    _drop_financial_audit_triggers(conn)
    conn.execute(
        "UPDATE financial_audit_events SET event_public_id = 'fae-forged-identity'"
        " WHERE correlation_public_id = ? AND event_type = 'receipt_finalized'",
        (output.finalization_public_id,),
    )
    conn.commit()
    before = _counts(conn, REPLAY_WRITE_TABLES)
    before_state = _lifecycle_state(conn)
    with pytest.raises(FinalizationIdempotencyError) as excinfo:
        finalize_prepared_receipt(conn, authorization)
    assert excinfo.value.reason == FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value
    assert _counts(conn, REPLAY_WRITE_TABLES) == before
    assert _lifecycle_state(conn) == before_state
    assert not conn.in_transaction


def test_replay_refuses_forged_participant_share_currency(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: a share relabelled into another currency breaks replay.

    A share row whose currency was tampered to another 2-decimal currency keeps
    the same canonical amount string, so amount comparison alone cannot detect
    it.  The currency of every durable share must equal the finalized currency.
    """
    conn = migrated_temp_db_connection
    _ctx, authorization, _output = _finalized_receipt(conn, tmp_path, "rgshcur")
    conn.execute(
        "UPDATE calculation_participant_shares SET currency = 'EUR'"
        " WHERE calculation_run_id = (SELECT id FROM calculation_runs WHERE public_id = ?)",
        (authorization.prepared.calculation_run_public_id,),
    )
    conn.commit()
    before = _counts(conn, REPLAY_WRITE_TABLES)
    before_state = _lifecycle_state(conn)
    with pytest.raises(FinalizationIdempotencyError) as excinfo:
        finalize_prepared_receipt(conn, authorization)
    assert excinfo.value.reason == FinalizationBlockReason.REPLAY_TRUTH_MISMATCH.value
    assert _counts(conn, REPLAY_WRITE_TABLES) == before
    assert _lifecycle_state(conn) == before_state
    assert not conn.in_transaction


def test_replay_refuses_forged_audit_idempotency_key(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: a retargeted audit idempotency_key breaks replay (R3-02)."""
    conn = migrated_temp_db_connection
    _ctx, authorization, output = _finalized_receipt(conn, tmp_path, "rgidem")
    _drop_audit_immutability_triggers(conn)
    conn.execute(
        "UPDATE receipt_finalization_audit SET idempotency_key = 'idem_forged_key'"
        " WHERE finalization_id = ?",
        (output.finalization_public_id,),
    )
    conn.commit()
    _assert_replay_refused_zero_write(conn, authorization)


def test_replay_refuses_forged_audit_totals(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """FORGED-CORRUPTION: forged audit totals break replay even with a valid key
    (R3-02: totals are re-derived independently, never trusted)."""
    conn = migrated_temp_db_connection
    _ctx, authorization, output = _finalized_receipt(conn, tmp_path, "rgtotals")
    _drop_audit_immutability_triggers(conn)
    conn.execute(
        "UPDATE receipt_finalization_audit SET total_to_collect = '999.99'"
        " WHERE finalization_id = ?",
        (output.finalization_public_id,),
    )
    conn.commit()
    _assert_replay_refused_zero_write(conn, authorization)


def test_rejected_replay_returns_typed_failure_and_writes_nothing(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Every rejected replay is a typed failure with zero new rows anywhere."""
    conn = migrated_temp_db_connection
    ctx, authorization, _output = _finalized_receipt(conn, tmp_path, "rgtyped")
    conn.execute(
        "UPDATE receipt_groups SET status = 'active' WHERE public_id = ?",
        (f"rgrp_{ctx.receipt_public_id}",),
    )
    conn.commit()
    all_tables = tuple(
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    )
    before = _counts(conn, all_tables)
    before_state = _lifecycle_state(conn)
    with pytest.raises(FAIL_CLOSED_ERRORS):
        finalize_prepared_receipt(conn, authorization)
    assert _counts(conn, all_tables) == before
    assert _lifecycle_state(conn) == before_state
    assert not conn.in_transaction
