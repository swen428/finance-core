"""IAF.7 fact-set -> calculation -> finalization bridge tests.

Covers the locked design
``docs/design/receipt_fact_set_calculation_finalization_bridge_v1.md``
(Section 10) and the IAF boundary Section 18.8 matrix for the bridge:

- prepare projects/calculates and persists an authoritative snapshot bound to
  the active fact-set four-tuple, with a receipt-scoped historical run;
- the full prepare -> authorize -> finalize path reconciles exactly across
  calculator, snapshot, authorization, and finalization;
- a superseded (drifted) active fact set fails finalization closed with zero
  transactions, settlement rows, authorization consumption, or group side
  effects, and leaves byte-identical fact-set history;
- a two-connection supersession committed after authorization but before
  finalize is rejected;
- the finalizer's own in-transaction four-tuple re-check rejects drift;
- prepare and finalize are idempotent; a re-prepared v2 finalizes cleanly;
- failure injected at a finalizer seam rolls back with no partial authority;
- every stage fails closed on a non-staging database.

Fixtures are built through the public B1 -> confirmation -> B4.1 -> IAF
boundaries.  Only disposable staging databases are used; ``database/finance.db``
and seed data are untouched.
"""

from __future__ import annotations

import shutil
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import finance_core.receipt_finalization.finalizer as finalizer
from finance_core.parser_proposals.receipt_item_allocation_facts import (
    persist_receipt_item_allocation_facts,
    supersede_receipt_item_allocation_facts,
)
from finance_core.receipt_finalization import (
    FinalizationAuthorizationError,
    FinalizationIdempotencyError,
    FinalizationInput,
    PreparedReceiptCalculation,
    ReceiptFinalizationAuthorization,
    authorize_receipt_finalization,
    finalize_prepared_receipt,
    finalize_receipt_split,
    prepare_receipt_calculation,
)
from finance_core.receipt_finalization.fact_set_bridge import _build_finalization_input
from finance_core.receipt_finalization.finalizer import _require_active_fact_set_binding
from finance_core.receipt_finalization.models import (
    ActiveFactSetBinding,
    ConfirmedReceiptIdentity,
    FinalizationBlockReason,
)
from finance_core.staging_guard import StagingDatabaseError
from tests.conftest import LIVE_DB_PATH, connect_temp_db
from tests.test_receipt_item_allocation_facts_service_v1 import (
    iaf_command,
    setup_receipt,
)
from tests.test_receipt_item_allocation_facts_supersession_v1 import correction_command

FINANCIAL_FACT_TABLES = (
    "transactions",
    "settlement_obligations",
    "receipt_groups",
    "receipt_group_receipts",
    "calculation_runs",
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _setup_active_fact_set(
    conn: sqlite3.Connection, tmp_path: Path, suffix: str
) -> tuple[Any, Any]:
    """B1 -> confirmation -> B4.1 -> IAF persist, all through public boundaries."""
    ctx = setup_receipt(conn, tmp_path, suffix)
    result = persist_receipt_item_allocation_facts(conn, iaf_command(suffix, ctx))
    conn.commit()
    return ctx, result


def _fact_set_registry(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM receipt_item_allocation_fact_sets ORDER BY receipt_id, version"
        ).fetchall()
    ]


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in FINANCIAL_FACT_TABLES
    }


def _authorization_state(conn: sqlite3.Connection, authorization_id: str) -> str | None:
    row = conn.execute(
        "SELECT authorization_state FROM receipt_finalization_authorizations "
        "WHERE authorization_id = ?",
        (authorization_id,),
    ).fetchone()
    return None if row is None else str(row["authorization_state"])


# ---------------------------------------------------------------------------
# Happy path + prepare binding
# ---------------------------------------------------------------------------


def test_prepare_authorize_finalize_happy_path(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "happy")

    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id, actor_type="cli")
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    output = finalize_prepared_receipt(conn, authorization)

    assert output.status == "finalized"
    assert output.obligations_created == 1
    assert output.transaction_public_id
    assert output.finalization_public_id == f"fin_{prepared.calculation_run_public_id}"

    assert int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]) == 1
    assert int(conn.execute("SELECT COUNT(*) FROM settlement_obligations").fetchone()[0]) == 1
    assert _authorization_state(conn, prepared.authorization_id) == "consumed"


def test_prepare_binds_active_fact_set_four_tuple(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, result = _setup_active_fact_set(conn, tmp_path, "bind")

    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)

    binding = prepared.active_fact_set_binding
    assert binding.receipt_public_id == ctx.receipt_public_id
    assert binding.fact_set_public_id == result.fact_set_public_id
    assert binding.fact_set_version == 1
    assert binding.fact_set_result_hash == result.fact_set_result_hash

    # The snapshot input payload and source references carry the four-tuple.
    row = conn.execute(
        "SELECT input_payload_json, source_references_json FROM "
        "authoritative_calculation_snapshots WHERE snapshot_public_id = ?",
        (prepared.calculation_snapshot_id,),
    ).fetchone()
    assert result.fact_set_result_hash in row["input_payload_json"]
    assert result.fact_set_result_hash in row["source_references_json"]
    # And the human authorization evidence records the binding four-tuple.
    assert any(binding.fact_set_result_hash in ref for ref in prepared.source_evidence_refs)


def test_prepare_is_idempotent(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "idem")

    first = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    second = prepare_receipt_calculation(conn, ctx.receipt_public_id)

    assert first.calculation_snapshot_id == second.calculation_snapshot_id
    assert first.calculation_snapshot_hash == second.calculation_snapshot_hash
    assert first.authorization_id == second.authorization_id
    assert (
        int(conn.execute("SELECT COUNT(*) FROM authoritative_calculation_snapshots").fetchone()[0])
        == 1
    )
    assert (
        int(
            conn.execute(
                "SELECT COUNT(*) FROM calc_audit_runs WHERE run_id = ?",
                (first.calculation_run_public_id,),
            ).fetchone()[0]
        )
        == 1
    )


# ---------------------------------------------------------------------------
# Staleness: supersession after authorization
# ---------------------------------------------------------------------------


def test_supersede_after_authorization_then_finalize_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, result = _setup_active_fact_set(conn, tmp_path, "stale")

    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")

    # Human supersedes the active fact set (v2) before finalize.
    supersede_receipt_item_allocation_facts(conn, correction_command("stale", ctx, result))
    conn.commit()

    before = _counts(conn)
    before_registry = _fact_set_registry(conn)

    with pytest.raises(FinalizationAuthorizationError) as excinfo:
        finalize_prepared_receipt(conn, authorization)
    assert excinfo.value.reason == FinalizationBlockReason.STALE_ACTIVE_FACT_SET

    # Zero financial facts, zero group side effects, authorization intact.
    assert _counts(conn) == before
    assert _fact_set_registry(conn) == before_registry
    assert _authorization_state(conn, prepared.authorization_id) == "authorized"
    assert not conn.in_transaction


def test_stale_rejection_creates_no_receipt_group(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, result = _setup_active_fact_set(conn, tmp_path, "nogrp")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    supersede_receipt_item_allocation_facts(conn, correction_command("nogrp", ctx, result))
    conn.commit()

    with pytest.raises(FinalizationAuthorizationError):
        finalize_prepared_receipt(conn, authorization)

    group_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM receipt_groups WHERE public_id = ?",
            (prepared.receipt_group_public_id,),
        ).fetchone()[0]
    )
    assert group_count == 0


def test_v2_reprepare_reauthorize_finalize_succeeds(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, result = _setup_active_fact_set(conn, tmp_path, "v2ok")

    prepared_v1 = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization_v1 = authorize_receipt_finalization(conn, prepared_v1, actor_id="owner")
    supersede_receipt_item_allocation_facts(conn, correction_command("v2ok", ctx, result))
    conn.commit()
    with pytest.raises(FinalizationAuthorizationError):
        finalize_prepared_receipt(conn, authorization_v1)

    # Re-prepare against the now-active v2 fact set and finalize cleanly.
    prepared_v2 = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    assert prepared_v2.active_fact_set_binding.fact_set_version == 2
    assert prepared_v2.calculation_snapshot_id != prepared_v1.calculation_snapshot_id
    authorization_v2 = authorize_receipt_finalization(conn, prepared_v2, actor_id="owner")
    output = finalize_prepared_receipt(conn, authorization_v2)
    assert output.status == "finalized"


# ---------------------------------------------------------------------------
# Two-connection race
# ---------------------------------------------------------------------------


def test_two_connection_supersession_before_finalize_rejected(
    migrated_temp_db_path: Path, tmp_path: Path
) -> None:
    writer = connect_temp_db(migrated_temp_db_path)
    other = connect_temp_db(migrated_temp_db_path)
    try:
        ctx, result = _setup_active_fact_set(writer, tmp_path, "race")
        prepared = prepare_receipt_calculation(writer, ctx.receipt_public_id)
        authorization = authorize_receipt_finalization(writer, prepared, actor_id="owner")

        # A competing connection supersedes the active fact set and commits.
        supersede_receipt_item_allocation_facts(other, correction_command("race", ctx, result))
        other.commit()

        with pytest.raises(FinalizationAuthorizationError) as excinfo:
            finalize_prepared_receipt(writer, authorization)
        assert excinfo.value.reason == FinalizationBlockReason.STALE_ACTIVE_FACT_SET
        assert int(writer.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]) == 0
        assert _authorization_state(writer, prepared.authorization_id) == "authorized"
    finally:
        writer.close()
        other.close()


# ---------------------------------------------------------------------------
# Finalizer in-transaction four-tuple re-check (defense in depth, white-box)
# ---------------------------------------------------------------------------


def test_finalizer_active_binding_recheck_rejects_drift(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, result = _setup_active_fact_set(conn, tmp_path, "recheck")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    live = prepared.active_fact_set_binding

    # A matching binding is a no-op.
    _require_active_fact_set_binding(conn, live)

    for drifted in (
        ActiveFactSetBinding(
            receipt_public_id=live.receipt_public_id,
            fact_set_public_id=live.fact_set_public_id,
            fact_set_version=live.fact_set_version,
            fact_set_input_hash=live.fact_set_input_hash,
            fact_set_result_hash="f" * 64,
        ),
        ActiveFactSetBinding(
            receipt_public_id=live.receipt_public_id,
            fact_set_public_id=live.fact_set_public_id,
            fact_set_version=live.fact_set_version + 1,
            fact_set_input_hash=live.fact_set_input_hash,
            fact_set_result_hash=live.fact_set_result_hash,
        ),
        ActiveFactSetBinding(
            receipt_public_id=live.receipt_public_id,
            fact_set_public_id="rfs_" + "0" * 32,
            fact_set_version=live.fact_set_version,
            fact_set_input_hash=live.fact_set_input_hash,
            fact_set_result_hash=live.fact_set_result_hash,
        ),
    ):
        with pytest.raises(FinalizationAuthorizationError) as excinfo:
            _require_active_fact_set_binding(conn, drifted)
        assert excinfo.value.reason == FinalizationBlockReason.STALE_ACTIVE_FACT_SET


def test_finalizer_active_binding_recheck_rejects_missing_active_set(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, result = _setup_active_fact_set(conn, tmp_path, "missing")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    # Supersede so the bound fact set is no longer the unique active set.
    supersede_receipt_item_allocation_facts(conn, correction_command("missing", ctx, result))
    conn.commit()

    with pytest.raises(FinalizationAuthorizationError) as excinfo:
        _require_active_fact_set_binding(conn, prepared.active_fact_set_binding)
    assert excinfo.value.reason == FinalizationBlockReason.STALE_ACTIVE_FACT_SET


# ---------------------------------------------------------------------------
# Idempotency + reconciliation
# ---------------------------------------------------------------------------


def test_finalize_idempotent_replay_no_duplicate_facts(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "replay")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")

    first = finalize_prepared_receipt(conn, authorization)
    txn_after_first = int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
    settle_after_first = int(
        conn.execute("SELECT COUNT(*) FROM settlement_obligations").fetchone()[0]
    )

    second = finalize_prepared_receipt(conn, authorization)
    assert second.finalization_public_id == first.finalization_public_id
    assert second.transaction_public_id == first.transaction_public_id
    assert int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]) == txn_after_first
    assert (
        int(conn.execute("SELECT COUNT(*) FROM settlement_obligations").fetchone()[0])
        == settle_after_first
    )


def test_reconciliation_across_calculator_snapshot_authorization_finalization(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "recon")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)

    calc = prepared.calculation_result
    assert str(calc["total_paid"]) == "12.34"
    assert prepared.payer_participant_public_id == "person_owner"
    shares = {k: str(v) for k, v in calc["participant_shares"].items()}
    assert shares == {"person_owner": "6.17", "person_alice": "6.17"}
    obligations = [
        (o["debtor"], o["creditor"], str(o["amount"]), o["currency"])
        for o in calc["settlement_obligations"]
    ]
    assert obligations == [("person_alice", "person_owner", "6.17", "SGD")]

    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    auth_row = conn.execute(
        "SELECT final_total, currency, payer_participant_public_id "
        "FROM receipt_finalization_authorizations WHERE authorization_id = ?",
        (prepared.authorization_id,),
    ).fetchone()
    assert auth_row["final_total"] == "12.34"
    assert auth_row["currency"] == "SGD"
    assert auth_row["payer_participant_public_id"] == "person_owner"

    output = finalize_prepared_receipt(conn, authorization)
    audit_row = conn.execute(
        "SELECT total_paid, currency, status "
        "FROM receipt_finalization_audit WHERE finalization_id = ?",
        (output.finalization_public_id,),
    ).fetchone()
    assert audit_row["total_paid"] == "12.34"
    assert audit_row["currency"] == "SGD"
    assert audit_row["status"] == "finalized"

    # The authoritative persisted collectable is the single alice->owner row.
    obligation_rows = conn.execute(
        """
        SELECT d.public_id AS debtor, c.public_id AS creditor,
               so.amount AS amount, so.currency AS currency
        FROM settlement_obligations so
        JOIN participants d ON d.id = so.debtor_id
        JOIN participants c ON c.id = so.creditor_id
        """
    ).fetchall()
    assert len(obligation_rows) == 1
    row = obligation_rows[0]
    assert (row["debtor"], row["creditor"], row["currency"]) == (
        "person_alice",
        "person_owner",
        "SGD",
    )
    assert Decimal(str(row["amount"])) == Decimal("6.17")


def test_same_idempotency_key_different_material_is_conflict(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Section 10: same idempotency key + different material -> conflict."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "keyconf")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    finalize_prepared_receipt(conn, authorization)

    base = _build_finalization_input(prepared, actor_type="human", actor_id="owner")
    variant = FinalizationInput(
        calculation_run_public_id=prepared.calculation_run_public_id,
        receipt_group_public_id=prepared.receipt_group_public_id,
        currency=prepared.currency,
        payer_participant_public_id=prepared.payer_participant_public_id,
        settlement_obligations=base.settlement_obligations,
        calculation_snapshot=prepared.calculation_result,
        authorization_id=prepared.authorization_id,
        confirmation_id=prepared.confirmation_id,
        idempotency_key=prepared.idempotency_key,
        calculation_snapshot_id=prepared.calculation_snapshot_id,
        calculation_snapshot_hash=prepared.calculation_snapshot_hash,
        currency_contract_version=prepared.currency_contract_version,
        actor_type="human",
        actor_id="owner",
        # Different material under the SAME durable idempotency key.
        source_evidence_refs=prepared.source_evidence_refs[:-1],
        active_fact_set_binding=prepared.active_fact_set_binding,
    )
    txn_before = int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
    with pytest.raises(FinalizationIdempotencyError) as excinfo:
        finalize_receipt_split(conn, variant)
    assert excinfo.value.reason == FinalizationBlockReason.CONTENT_CONFLICT
    assert int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]) == txn_before


# ---------------------------------------------------------------------------
# Failure injection at a finalizer seam
# ---------------------------------------------------------------------------


def test_failure_injection_at_finalizer_seam_rolls_back(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "inject")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected finalizer seam failure")

    monkeypatch.setattr(finalizer, "_insert_settlement_obligations", _boom)

    with pytest.raises(RuntimeError, match="injected finalizer seam failure"):
        finalize_prepared_receipt(conn, authorization)

    assert not conn.in_transaction
    assert int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]) == 0
    assert int(conn.execute("SELECT COUNT(*) FROM settlement_obligations").fetchone()[0]) == 0
    assert _authorization_state(conn, prepared.authorization_id) == "authorized"


# ---------------------------------------------------------------------------
# Staging / live database rejection
# ---------------------------------------------------------------------------


def _dummy_prepared() -> PreparedReceiptCalculation:
    binding = ActiveFactSetBinding(
        receipt_public_id="rcpt_x",
        fact_set_public_id="rfs_x",
        fact_set_version=1,
        fact_set_input_hash="1" * 64,
        fact_set_result_hash="2" * 64,
    )
    return PreparedReceiptCalculation(
        receipt_public_id="rcpt_x",
        receipt_group_public_id="rgrp_rcpt_x",
        receipt_group_receipt_public_id="rgrpr_rcpt_x",
        calculation_run_public_id="calc_x",
        calculation_snapshot_id="snap_x",
        calculation_snapshot_hash="3" * 64,
        authorization_id="authz_x",
        confirmation_id="conf_x",
        idempotency_key="idem_x",
        currency="SGD",
        currency_contract_version="currency-SGD-v1",
        payer_participant_public_id="person_owner",
        active_fact_set_binding=binding,
        source_evidence_refs=("iaf.receipt_public_id=rcpt_x",),
        calculation_result={"total_paid": "0.00", "settlement_obligations": []},
        confirmed_receipt_identity=ConfirmedReceiptIdentity(
            receipt_public_id="rcpt_x",
            merchant="COLD STORAGE",
            receipt_date="2026-01-02",
            source_channel="telegram",
            currency="SGD",
        ),
        idempotent_replay=False,
    )


def test_prepare_rejects_plain_database(tmp_path: Path) -> None:
    plain = sqlite3.connect(str(tmp_path / "plain.db"))
    try:
        with pytest.raises(StagingDatabaseError):
            prepare_receipt_calculation(plain, "rcpt_anything")
    finally:
        plain.close()


def test_authorize_rejects_plain_database(tmp_path: Path) -> None:
    plain = sqlite3.connect(str(tmp_path / "plain.db"))
    try:
        with pytest.raises(StagingDatabaseError):
            authorize_receipt_finalization(plain, _dummy_prepared(), actor_id="owner")
    finally:
        plain.close()


def test_finalize_rejects_plain_database(tmp_path: Path) -> None:
    plain = sqlite3.connect(str(tmp_path / "plain.db"))
    authorization = ReceiptFinalizationAuthorization(
        authorization_id="authz_x",
        confirmation_id="conf_x",
        content_hash="4" * 64,
        actor_type="human",
        actor_id="owner",
        prepared=_dummy_prepared(),
    )
    try:
        with pytest.raises(StagingDatabaseError):
            finalize_prepared_receipt(plain, authorization)
    finally:
        plain.close()


def test_copied_staging_database_rejected(migrated_temp_db_path: Path, tmp_path: Path) -> None:
    copy_path = tmp_path / "copied.db"
    shutil.copyfile(migrated_temp_db_path, copy_path)
    copied = sqlite3.connect(str(copy_path))
    try:
        with pytest.raises(StagingDatabaseError):
            prepare_receipt_calculation(copied, "rcpt_anything")
    finally:
        copied.close()


@pytest.mark.skipif(not LIVE_DB_PATH.exists(), reason="live database not present")
def test_live_database_rejected_via_readonly_connection() -> None:
    conn = sqlite3.connect(f"file:{LIVE_DB_PATH}?mode=ro", uri=True)
    try:
        with pytest.raises(StagingDatabaseError):
            prepare_receipt_calculation(conn, "rcpt_anything")
    finally:
        conn.close()
