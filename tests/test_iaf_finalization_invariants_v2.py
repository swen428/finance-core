"""IAF.5-IAF.8 remediation regression tests v2 (PR #248 review blockers).

Freezes the merge blockers independently confirmed on the PR #248 review head
(REM-01..REM-07 of the remediation continuation task):

- REM-01: source evidence must be snapshot-derived single truth -- a caller
  DTO that omits, substitutes, or reorders the hash-bound snapshot source
  references must never reach authorization or finalization, and the exact
  bundle must survive into the authorization and audit records;
- REM-02: a legitimately excluded payer (``is_included=0``, D5 payer role, in
  no allocation) must finalize, while excluded consumers/debtors stay refused;
- REM-03: prepare must be one atomic Unit of Work -- snapshot, snapshot audit,
  calculation run, and both binding evidence rows commit or roll back
  together, and a retry after an injected failure self-heals;
- REM-05: the finalizer must re-verify the durable calculation-run truth
  (row material and binding evidence) inside its own write transaction;
- REM-06: idempotent replay must verify the complete durable truth --
  canonical transaction, shares, settlement rows, and canonical JSON -- and
  refuse forged or missing material instead of reporting success;
- REM-07: a persisted active fact set the deterministic calculator rejects is
  an IA-D10 integrity failure, never an ordinary not-ready reason.

Also carries the REM-09 two-connection concurrency races, the four-stage
failure-injection matrix, and the binding-evidence drift matrix.

Fixtures are built through the public B1 -> confirmation -> B4.1 -> IAF
boundaries.  Direct SQL appears only in explicitly marked forged-negative /
drift / failure-injection fixtures and SELECT assertions.  Only disposable
staging databases are used; ``database/finance.db`` and seed data are
untouched.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import finance_core.receipt_finalization.fact_set_bridge as fact_set_bridge
import finance_core.receipt_finalization.finalizer as finalizer
from finance_core.calculators.receipt_calculator_readiness import (
    ReceiptFactsIntegrityError,
    report_receipt_calculator_readiness,
)
from finance_core.parser_proposals.receipt_item_allocation_facts import (
    persist_receipt_item_allocation_facts,
    supersede_receipt_item_allocation_facts,
)
from finance_core.receipt_finalization import (
    authorize_receipt_finalization,
    finalize_prepared_receipt,
    prepare_receipt_calculation,
)
from tests.conftest import connect_temp_db
from tests.test_iaf_finalization_invariants_v1 import (
    CANONICAL_FACT_TABLES,
    FAIL_CLOSED_ERRORS,
    _counts,
    _negative_share_facts,
    _setup_active_fact_set,
)
from tests.test_receipt_facts_conversion_v1 import entries
from tests.test_receipt_item_allocation_facts_service_v1 import (
    iaf_command,
    setup_receipt,
)
from tests.test_receipt_item_allocation_facts_supersession_v1 import correction_command

EVIDENCE_TABLE = "receipt_fact_set_binding_evidence"

PREPARE_TABLES = (
    "authoritative_calculation_snapshots",
    "calc_audit_runs",
    EVIDENCE_TABLE,
)


def _evidence_rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in conn.execute(
            f"SELECT * FROM {EVIDENCE_TABLE} ORDER BY binding_public_id"
        ).fetchall()
    ]


def _drop_evidence_append_only_triggers(conn: sqlite3.Connection) -> None:
    """FORGED-CORRUPTION FIXTURE ONLY: bypass the migration 037 append-only guards.

    Production code can never do this; the drift matrix needs rows that the
    schema itself refuses to produce, to prove the service layer still fails
    closed on already-corrupt durable state.
    """
    conn.execute("DROP TRIGGER IF EXISTS trg_fact_set_binding_evidence_no_update")
    conn.execute("DROP TRIGGER IF EXISTS trg_fact_set_binding_evidence_no_delete")
    conn.execute("DROP TRIGGER IF EXISTS trg_fact_set_binding_evidence_no_insert_collision")


def _alice_only_facts() -> dict[str, Any]:
    """Every item is consumed by Alice alone; the payer consumes nothing."""
    return {
        "items": [
            {
                "line_number": 1,
                "item_name": "Alice dinner",
                "line_amount": "12.34",
                "currency": "SGD",
            }
        ],
        "allocations": [
            {
                "line_number": 1,
                "allocation_method": "manual",
                "participants": [
                    {
                        "participant_public_id": "person_alice",
                        "share_amount": "12.34",
                        "currency": "SGD",
                    },
                ],
            }
        ],
        "adjustments": [],
    }


# ---------------------------------------------------------------------------
# REM-01: snapshot-derived source evidence single truth
# ---------------------------------------------------------------------------


def test_authorization_with_stripped_evidence_refs_is_refused(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A DTO that drops the snapshot's hash-bound evidence bundle is refused."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "evstrip")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    assert len(prepared.source_evidence_refs) >= 10

    forged = dataclasses.replace(prepared, source_evidence_refs=())
    with pytest.raises(FAIL_CLOSED_ERRORS):
        authorize_receipt_finalization(conn, forged, actor_id="owner")

    rows = conn.execute("SELECT COUNT(*) AS n FROM receipt_finalization_authorizations").fetchone()
    assert int(rows["n"]) == 0
    assert not conn.in_transaction


def test_authorization_with_substituted_evidence_ref_is_refused(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Substituting one evidence reference for another must fail closed."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "evsub")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)

    substituted = tuple(
        sorted([*prepared.source_evidence_refs[:-1], "iaf.attachment_content_hash=" + "0" * 64])
    )
    assert substituted != prepared.source_evidence_refs
    forged = dataclasses.replace(prepared, source_evidence_refs=substituted)
    with pytest.raises(FAIL_CLOSED_ERRORS):
        authorize_receipt_finalization(conn, forged, actor_id="owner")
    assert not conn.in_transaction


def test_evidence_bundle_is_identical_across_snapshot_authorization_and_audit(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """One evidence bundle: snapshot == authorization == finalization audit."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "evchain")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    output = finalize_prepared_receipt(conn, authorization)

    snapshot_refs = json.loads(
        conn.execute(
            "SELECT source_references_json FROM authoritative_calculation_snapshots "
            "WHERE snapshot_public_id = ?",
            (prepared.calculation_snapshot_id,),
        ).fetchone()["source_references_json"]
    )
    auth_refs = json.loads(
        conn.execute(
            "SELECT source_evidence_refs_json FROM receipt_finalization_authorizations "
            "WHERE authorization_id = ?",
            (prepared.authorization_id,),
        ).fetchone()["source_evidence_refs_json"]
    )
    audit_refs = json.loads(
        conn.execute(
            "SELECT evidence_refs_json FROM receipt_finalization_audit WHERE finalization_id = ?",
            (output.finalization_public_id,),
        ).fetchone()["evidence_refs_json"]
    )
    assert snapshot_refs == sorted(prepared.source_evidence_refs)
    assert sorted(auth_refs) == snapshot_refs
    assert sorted(audit_refs) == snapshot_refs
    assert any(ref.startswith("iaf.attachment_content_hash=") for ref in snapshot_refs)


# ---------------------------------------------------------------------------
# REM-02: excluded payer finalization
# ---------------------------------------------------------------------------


def test_excluded_payer_receipt_finalizes_end_to_end(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A non-consuming excluded payer must still finalize as settlement creditor."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(
        conn,
        tmp_path,
        "explpayer",
        membership=entries(("person_owner", 0), ("person_alice", 1)),
    )
    persist_receipt_item_allocation_facts(
        conn, iaf_command("explpayer", ctx, **_alice_only_facts())
    )
    conn.commit()

    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    assert prepared.payer_participant_public_id == "person_owner"
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    output = finalize_prepared_receipt(conn, authorization)

    assert output.status == "finalized"
    assert output.obligations_created == 1
    txn = dict(conn.execute("SELECT amount, currency, merchant FROM transactions").fetchone())
    assert Decimal(str(txn["amount"])) == Decimal("12.34")
    assert txn["currency"] == "SGD"
    assert txn["merchant"] == "COLD STORAGE"

    obligation = dict(
        conn.execute(
            "SELECT d.public_id AS debtor, c.public_id AS creditor, so.amount "
            "FROM settlement_obligations so "
            "JOIN participants d ON d.id = so.debtor_id "
            "JOIN participants c ON c.id = so.creditor_id"
        ).fetchone()
    )
    assert obligation["debtor"] == "person_alice"
    assert obligation["creditor"] == "person_owner"
    assert Decimal(str(obligation["amount"])) == Decimal("12.34")

    shares = {
        row["public_id"]: Decimal(str(row["final_share_amount"]))
        for row in conn.execute(
            "SELECT p.public_id, cps.final_share_amount "
            "FROM calculation_participant_shares cps "
            "JOIN participants p ON p.id = cps.participant_id"
        ).fetchall()
    }
    assert shares == {"person_owner": Decimal("0.00"), "person_alice": Decimal("12.34")}


def test_excluded_payer_named_in_allocation_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """An excluded payer listed as a consumer must never produce facts."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(
        conn,
        tmp_path,
        "explpayerc",
        membership=entries(("person_owner", 0), ("person_alice", 1)),
    )
    facts = _alice_only_facts()
    facts["allocations"][0]["participants"] = [
        {
            "participant_public_id": "person_owner",
            "share_amount": "12.34",
            "currency": "SGD",
        }
    ]
    with pytest.raises(FAIL_CLOSED_ERRORS):
        persist_receipt_item_allocation_facts(conn, iaf_command("explpayerc", ctx, **facts))
    assert _counts(conn) == dict.fromkeys(CANONICAL_FACT_TABLES, 0)


def test_excluded_consumer_membership_drift_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A consumer/debtor whose membership drifted to excluded is refused."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "exclcons")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")

    # FORGED-DRIFT FIXTURE: exclude the consuming debtor after authorization.
    # The migration 035 receipt-participants immutability trigger must be bypassed.
    conn.execute("DROP TRIGGER IF EXISTS trg_receipt_participants_conversion_bound_freeze")
    conn.execute(
        "UPDATE receipt_participants SET is_included = 0, role = 'excluded' "
        "WHERE participant_id = (SELECT id FROM participants WHERE public_id = ?)",
        ("person_alice",),
    )
    conn.commit()

    before = _counts(conn)
    with pytest.raises(FAIL_CLOSED_ERRORS):
        finalize_prepared_receipt(conn, authorization)
    assert _counts(conn) == before
    assert before["transactions"] == 0
    assert not conn.in_transaction


# ---------------------------------------------------------------------------
# REM-03: atomic prepare
# ---------------------------------------------------------------------------


def test_prepare_failure_leaves_no_partial_authority(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot, run, and evidence must roll back together on any failure."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "prepatomic")

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected prepare evidence failure")

    monkeypatch.setattr(fact_set_bridge, "append_fact_set_binding_evidence", _boom)
    with pytest.raises(RuntimeError, match="injected prepare evidence failure"):
        prepare_receipt_calculation(conn, ctx.receipt_public_id)

    assert _counts(conn, PREPARE_TABLES) == dict.fromkeys(PREPARE_TABLES, 0)
    assert not conn.in_transaction


def test_prepare_retry_after_injected_failure_self_heals(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed prepare must not block the next legitimate prepare."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "prepheal")

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected prepare evidence failure")

    monkeypatch.setattr(fact_set_bridge, "append_fact_set_binding_evidence", _boom)
    with pytest.raises(RuntimeError):
        prepare_receipt_calculation(conn, ctx.receipt_public_id)
    monkeypatch.undo()

    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    counts = _counts(conn, PREPARE_TABLES)
    assert counts["authoritative_calculation_snapshots"] == 1
    assert counts["calc_audit_runs"] == 1
    assert counts[EVIDENCE_TABLE] == 2
    assert prepared.calculation_snapshot_hash


def test_prepare_snapshot_stage_failure_leaves_no_partial_authority(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure at the snapshot persistence stage itself leaves zero authority."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "prepsnap")

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected snapshot persistence failure")

    monkeypatch.setattr(fact_set_bridge, "persist_authoritative_snapshot_in_transaction", _boom)
    with pytest.raises(RuntimeError, match="injected snapshot persistence failure"):
        prepare_receipt_calculation(conn, ctx.receipt_public_id)

    assert _counts(conn, PREPARE_TABLES) == dict.fromkeys(PREPARE_TABLES, 0)
    assert not conn.in_transaction


# ---------------------------------------------------------------------------
# REM-05: durable calculation-run truth at finalize time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "drift_sql",
    [
        pytest.param(
            "UPDATE calc_audit_runs SET entity_id = 'r_other_receipt' WHERE run_id = ?",
            id="entity-drift",
        ),
        pytest.param(
            "UPDATE calc_audit_runs SET source_reference = '" + "e" * 64 + "' WHERE run_id = ?",
            id="source-reference-drift",
        ),
    ],
)
def test_finalization_refuses_drifted_calculation_run_truth(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path, drift_sql: str
) -> None:
    """The finalizer must re-verify the run's complete durable material."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "rundrift")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")

    # FORGED-DRIFT FIXTURE: mutate the historical run behind the finalizer.
    conn.execute(drift_sql, (prepared.calculation_run_public_id,))
    conn.commit()

    before = _counts(conn)
    with pytest.raises(FAIL_CLOSED_ERRORS):
        finalize_prepared_receipt(conn, authorization)
    assert _counts(conn) == before
    assert before["transactions"] == 0
    assert not conn.in_transaction


def test_finalization_refuses_missing_calculation_run(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A deleted calculation run must fail closed with zero canonical writes."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "runmiss")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")

    # FORGED-CORRUPTION FIXTURE: delete both the evidence FK row and the run.
    _drop_evidence_append_only_triggers(conn)
    conn.execute(
        f"DELETE FROM {EVIDENCE_TABLE} WHERE calculation_run_id = ?",
        (prepared.calculation_run_public_id,),
    )
    conn.execute(
        "DELETE FROM calc_audit_runs WHERE run_id = ?",
        (prepared.calculation_run_public_id,),
    )
    conn.commit()

    before = _counts(conn)
    with pytest.raises(FAIL_CLOSED_ERRORS):
        finalize_prepared_receipt(conn, authorization)
    assert _counts(conn) == before
    assert before["transactions"] == 0
    assert not conn.in_transaction


# ---------------------------------------------------------------------------
# REM-06: complete replay verification
# ---------------------------------------------------------------------------


def _finalized_pipeline(
    conn: sqlite3.Connection, tmp_path: Path, suffix: str
) -> tuple[Any, Any, Any]:
    ctx, _ = _setup_active_fact_set(conn, tmp_path, suffix)
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    output = finalize_prepared_receipt(conn, authorization)
    assert output.status == "finalized"
    return prepared, authorization, output


def test_exact_replay_returns_identical_ids_and_row_counts(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A verified replay reports already_finalized with the first run's IDs."""
    conn = migrated_temp_db_connection
    prepared, authorization, output = _finalized_pipeline(conn, tmp_path, "replayok")

    before = _counts(conn)
    replay = finalize_prepared_receipt(conn, authorization)
    assert replay.status == "already_finalized"
    assert replay.transaction_public_id == output.transaction_public_id
    assert replay.finalization_public_id == output.finalization_public_id
    assert sorted(replay.settlement_public_ids) == sorted(output.settlement_public_ids)
    assert _counts(conn) == before


def test_replay_refuses_drifted_canonical_transaction(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Replay must compare the durable transaction row, not just the audit row."""
    conn = migrated_temp_db_connection
    prepared, authorization, output = _finalized_pipeline(conn, tmp_path, "replaytxn")

    # FORGED-DRIFT FIXTURE: corrupt the canonical monetary fact.
    conn.execute(
        "UPDATE transactions SET amount = '999.99' WHERE public_id = ?",
        (output.transaction_public_id,),
    )
    conn.commit()

    with pytest.raises(FAIL_CLOSED_ERRORS):
        finalize_prepared_receipt(conn, authorization)
    assert not conn.in_transaction


def test_replay_refuses_missing_settlement_rows(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Replay must prove the settlement rows still exist and match."""
    conn = migrated_temp_db_connection
    prepared, authorization, output = _finalized_pipeline(conn, tmp_path, "replayset")
    assert output.obligations_created == 1

    # FORGED-DRIFT FIXTURE: remove the persisted settlement obligation.
    conn.execute("DELETE FROM settlement_obligations")
    conn.commit()

    with pytest.raises(FAIL_CLOSED_ERRORS):
        finalize_prepared_receipt(conn, authorization)
    assert not conn.in_transaction


def test_replay_refuses_drifted_participant_share(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Replay must prove the persisted shares still match the snapshot."""
    conn = migrated_temp_db_connection
    prepared, authorization, _ = _finalized_pipeline(conn, tmp_path, "replayshare")

    # FORGED-DRIFT FIXTURE: corrupt one persisted participant share.
    conn.execute("UPDATE calculation_participant_shares SET final_share_amount = '999.99'")
    conn.commit()

    with pytest.raises(FAIL_CLOSED_ERRORS):
        finalize_prepared_receipt(conn, authorization)
    assert not conn.in_transaction


def test_replay_refuses_malformed_settlement_public_ids_json(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Malformed durable JSON must be a typed error, never an empty success."""
    conn = migrated_temp_db_connection
    prepared, authorization, output = _finalized_pipeline(conn, tmp_path, "replayjson")

    # FORGED-CORRUPTION FIXTURE: the audit table is schema-immutable after
    # migration 038, so deliberately drop its guards to plant corrupt JSON the
    # replay verifier must still refuse.
    conn.execute("DROP TRIGGER IF EXISTS trg_receipt_finalization_audit_no_update")
    conn.execute(
        "UPDATE receipt_finalization_audit SET settlement_public_ids_json = '{not json' "
        "WHERE finalization_id = ?",
        (output.finalization_public_id,),
    )
    conn.commit()

    with pytest.raises(FAIL_CLOSED_ERRORS):
        finalize_prepared_receipt(conn, authorization)
    assert not conn.in_transaction


def test_finalization_audit_and_idempotency_rows_are_schema_immutable(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Migration 038: audit and idempotency rows refuse UPDATE/DELETE/REPLACE."""
    conn = migrated_temp_db_connection
    prepared, _, output = _finalized_pipeline(conn, tmp_path, "auditlock")

    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(
            "UPDATE receipt_finalization_audit SET transaction_public_id = 'txn_forged' "
            "WHERE finalization_id = ?",
            (output.finalization_public_id,),
        )
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(
            "DELETE FROM receipt_finalization_audit WHERE finalization_id = ?",
            (output.finalization_public_id,),
        )
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(
            "UPDATE receipt_finalization_idempotency SET finalization_audit_id = 'forged' "
            "WHERE idempotency_key = ?",
            (prepared.idempotency_key,),
        )
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(
            "DELETE FROM receipt_finalization_idempotency WHERE idempotency_key = ?",
            (prepared.idempotency_key,),
        )
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(
            "INSERT OR REPLACE INTO receipt_finalization_idempotency ("
            "idempotency_key, content_fingerprint, status, finalization_audit_id, created_at"
            ") VALUES (?, ?, 'finalized', ?, '2026-07-30T00:00:00+00:00')",
            (prepared.idempotency_key, "f" * 64, output.finalization_public_id),
        )
    conn.rollback()


# ---------------------------------------------------------------------------
# REM-07: IA-D10 readiness contract restored
# ---------------------------------------------------------------------------


def test_negative_share_active_fact_set_is_an_integrity_failure(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A persisted fact set the calculator rejects raises the IA-D10 typed error."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "negready", **_negative_share_facts())

    with pytest.raises(ReceiptFactsIntegrityError):
        report_receipt_calculator_readiness(conn, ctx.receipt_public_id)

    before = _counts(conn, ("authoritative_calculation_snapshots", "calc_audit_runs"))
    with pytest.raises(FAIL_CLOSED_ERRORS):
        prepare_receipt_calculation(conn, ctx.receipt_public_id)
    assert _counts(conn, ("authoritative_calculation_snapshots", "calc_audit_runs")) == before


def test_no_fact_set_reasons_stay_backward_compatible(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A valid B4.1 total-only receipt keeps the two approved not-ready reasons."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "noready")

    report = report_receipt_calculator_readiness(conn, ctx.receipt_public_id)
    assert report.is_calculator_ready is False
    assert report.not_ready_reasons == (
        "no_authoritative_item_facts",
        "no_authoritative_allocation_facts",
    )


# ---------------------------------------------------------------------------
# REM-09.2: authorization / finalization evidence failure injection
# ---------------------------------------------------------------------------


def test_authorization_evidence_stage_failure_rolls_back(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed authorization evidence write leaves no authorization rows."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "authinj")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    evidence_before = _evidence_rows(conn)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected authorization evidence failure")

    monkeypatch.setattr(fact_set_bridge, "append_fact_set_binding_evidence", _boom)
    with pytest.raises(RuntimeError, match="injected authorization evidence failure"):
        authorize_receipt_finalization(conn, prepared, actor_id="owner")

    assert (
        int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM receipt_finalization_authorizations"
            ).fetchone()["n"]
        )
        == 0
    )
    assert (
        int(
            conn.execute("SELECT COUNT(*) AS n FROM receipt_finalization_confirmations").fetchone()[
                "n"
            ]
        )
        == 0
    )
    assert _evidence_rows(conn) == evidence_before
    assert not conn.in_transaction


def test_finalization_audit_evidence_stage_failure_rolls_back(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed audit evidence write rolls back every canonical write."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "auditinj")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    evidence_before = _evidence_rows(conn)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected audit evidence failure")

    monkeypatch.setattr(finalizer, "append_fact_set_binding_evidence", _boom)
    with pytest.raises(FAIL_CLOSED_ERRORS):
        finalize_prepared_receipt(conn, authorization)

    assert _counts(conn) == dict.fromkeys(CANONICAL_FACT_TABLES, 0)
    assert _evidence_rows(conn) == evidence_before
    row = conn.execute(
        "SELECT authorization_state FROM receipt_finalization_authorizations "
        "WHERE authorization_id = ?",
        (prepared.authorization_id,),
    ).fetchone()
    assert str(row["authorization_state"]) == "authorized"
    assert not conn.in_transaction


# ---------------------------------------------------------------------------
# REM-09.3: binding-evidence drift matrix
# ---------------------------------------------------------------------------


def test_binding_evidence_drift_matrix_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Missing or conflicting evidence rows refuse finalization, zero writes."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "evdrift")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")

    # FORGED-CORRUPTION FIXTURE: drop the append-only guards, then corrupt the
    # snapshot evidence row's result hash.  The service must fail closed on
    # the conflict even though the schema could no longer prevent it.
    _drop_evidence_append_only_triggers(conn)
    untouched_before = [row for row in _evidence_rows(conn) if "calculation_run" in str(row)]
    conn.execute(
        f"UPDATE {EVIDENCE_TABLE} SET fact_set_result_hash = '"
        + "d" * 64
        + "' WHERE calculation_snapshot_public_id = ?",
        (prepared.calculation_snapshot_id,),
    )
    conn.commit()

    before = _counts(conn)
    with pytest.raises(FAIL_CLOSED_ERRORS):
        finalize_prepared_receipt(conn, authorization)
    assert _counts(conn) == before
    assert before["transactions"] == 0

    # Untouched historical evidence rows stayed byte-identical.
    untouched_after = [row for row in _evidence_rows(conn) if "calculation_run" in str(row)]
    assert untouched_after == untouched_before
    assert not conn.in_transaction


def test_missing_authorization_evidence_row_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """An authorization without durable binding evidence must not finalize."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "evmissing")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")

    # FORGED-CORRUPTION FIXTURE: remove the authorization's evidence row.
    _drop_evidence_append_only_triggers(conn)
    conn.execute(
        f"DELETE FROM {EVIDENCE_TABLE} WHERE finalization_authorization_id = ?",
        (prepared.authorization_id,),
    )
    conn.commit()

    before = _counts(conn)
    with pytest.raises(FAIL_CLOSED_ERRORS):
        finalize_prepared_receipt(conn, authorization)
    assert _counts(conn) == before
    assert not conn.in_transaction


def test_stale_input_hash_is_refused_at_finalize(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Input-hash-only drift of the live fact set is stale, not finalizable."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "evinput")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")

    # FORGED-CORRUPTION FIXTURE: the registry is schema-frozen, so drop its
    # transition guard to drift only the input hash of the live fact set.
    conn.execute("DROP TRIGGER IF EXISTS trg_receipt_item_allocation_fact_sets_single_transition")
    conn.execute(
        "UPDATE receipt_item_allocation_fact_sets SET fact_set_input_hash = '"
        + "c" * 64
        + "' WHERE fact_set_public_id = ?",
        (prepared.active_fact_set_binding.fact_set_public_id,),
    )
    conn.commit()

    before = _counts(conn)
    with pytest.raises(FAIL_CLOSED_ERRORS):
        finalize_prepared_receipt(conn, authorization)
    assert _counts(conn) == before
    assert not conn.in_transaction


# ---------------------------------------------------------------------------
# REM-09.1: real two-connection concurrency
# ---------------------------------------------------------------------------


def test_two_connection_race_finalize_vs_supersession(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    """Overlapping finalize and supersession never yield split authority.

    Two real SQLite connections start behind one barrier; whichever acquires
    the write lock first wins, and the loser must fail closed: either the
    finalization sees a superseded fact set (stale) or the supersession sees
    a finalized receipt (guarded).  Exactly one side may succeed.
    """
    setup_conn = migrated_temp_db_connection
    ctx, result = _setup_active_fact_set(setup_conn, tmp_path, "race1")
    prepared = prepare_receipt_calculation(setup_conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(setup_conn, prepared, actor_id="owner")
    correction = correction_command("race1", ctx, result)

    barrier = threading.Barrier(2, timeout=30)
    outcomes: dict[str, Any] = {}

    def _finalize() -> None:
        conn = connect_temp_db(migrated_temp_db_path)
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            barrier.wait()
            outcomes["finalize"] = finalize_prepared_receipt(conn, authorization)
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            outcomes["finalize"] = exc
        finally:
            conn.close()

    def _supersede() -> None:
        conn = connect_temp_db(migrated_temp_db_path)
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            barrier.wait()
            outcomes["supersede"] = supersede_receipt_item_allocation_facts(conn, correction)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            outcomes["supersede"] = exc
        finally:
            conn.close()

    threads = [threading.Thread(target=_finalize), threading.Thread(target=_supersede)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)

    finalize_ok = not isinstance(outcomes["finalize"], Exception)
    supersede_ok = not isinstance(outcomes["supersede"], Exception)
    assert finalize_ok != supersede_ok, outcomes

    check = connect_temp_db(migrated_temp_db_path)
    try:
        txn_count = int(check.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        active = check.execute(
            "SELECT version FROM receipt_item_allocation_fact_sets "
            "WHERE superseded_by_fact_set_public_id IS NULL"
        ).fetchall()
        if finalize_ok:
            # Finalization won: canonical facts exist and v1 is still active.
            assert txn_count == 1
            assert [int(row[0]) for row in active] == [1]
        else:
            # Supersession won: v2 is active and zero canonical facts exist.
            assert txn_count == 0
            assert [int(row[0]) for row in active] == [2]
        obligation_count = int(
            check.execute("SELECT COUNT(*) FROM settlement_obligations").fetchone()[0]
        )
        assert obligation_count == (1 if finalize_ok else 0)
    finally:
        check.close()


def test_two_connection_identical_prepare_is_single_canonical_set(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    """Concurrent identical prepares produce exactly one set of canonical rows."""
    setup_conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(setup_conn, tmp_path, "race2")

    barrier = threading.Barrier(2, timeout=30)
    outcomes: dict[str, Any] = {}

    def _prepare(name: str) -> None:
        conn = connect_temp_db(migrated_temp_db_path)
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            barrier.wait()
            outcomes[name] = prepare_receipt_calculation(conn, ctx.receipt_public_id)
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            outcomes[name] = exc
        finally:
            conn.close()

    threads = [
        threading.Thread(target=_prepare, args=("a",)),
        threading.Thread(target=_prepare, args=("b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)

    assert not isinstance(outcomes["a"], Exception), outcomes["a"]
    assert not isinstance(outcomes["b"], Exception), outcomes["b"]
    assert outcomes["a"] == outcomes["b"]

    counts = _counts(setup_conn, PREPARE_TABLES)
    assert counts["authoritative_calculation_snapshots"] == 1
    assert counts["calc_audit_runs"] == 1
    assert counts[EVIDENCE_TABLE] == 2
