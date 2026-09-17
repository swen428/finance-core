"""IAF.5-IAF.8 finalization invariant regression tests (remediation v1).

Freezes the financial-correctness and audit invariants that independent
verification found unenforced in the IAF.5-IAF.8 slices:

- the authoritative snapshot, the human authorization, and the finalizer must
  consume one single, inseparable active fact-set binding, so old snapshot /
  calculation material can never be finalized against a newer active binding;
- receipt group materialization, canonical writes, settlement, audit, and
  authorization consumption must succeed or roll back in one Unit of Work, so a
  finalizer failure can never leave a group behind and can never block a later
  fact-set supersession;
- an existing, conflicting, or foreign receipt group must never be silently
  adopted;
- durable calculation-run and authorization replay must compare complete
  material, never accept a conflicting row as idempotent;
- ``is_calculator_ready`` must guarantee the deterministic calculator accepts
  the supported projection (including negative-share fact sets);
- canonical transaction metadata (merchant, receipt date), audit totals, and
  settlement totals must be exact;
- a legitimate single-owner receipt with zero settlement obligations must still
  finalize through the full guarded chain.

Fixtures are built through the public B1 -> confirmation -> B4.1 -> IAF
boundaries.  Direct SQL appears only in explicitly marked negative/corruption
fixtures and SELECT assertions.  Only disposable staging databases are used;
``database/finance.db`` and seed data are untouched.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import finance_core.receipt_finalization.finalizer as finalizer
from finance_core.calculators.receipt_calculator_readiness import (
    report_receipt_calculator_readiness,
)
from finance_core.parser_proposals.receipt_item_allocation_facts import (
    persist_receipt_item_allocation_facts,
    supersede_receipt_item_allocation_facts,
)
from finance_core.receipt_finalization import (
    PreparedReceiptCalculation,
    authorize_receipt_finalization,
    finalize_prepared_receipt,
    prepare_receipt_calculation,
)
from finance_core.receipt_finalization.fact_set_bridge import _derive_ids
from finance_core.receipt_finalization.models import ActiveFactSetBinding
from tests.test_receipt_facts_conversion_v1 import entries
from tests.test_receipt_item_allocation_facts_service_v1 import (
    ConvertedReceipt,
    iaf_command,
    setup_receipt,
)
from tests.test_receipt_item_allocation_facts_supersession_v1 import correction_command

# Tables only a successful finalization may populate.
CANONICAL_FACT_TABLES = (
    "transactions",
    "calculation_runs",
    "calculation_participant_shares",
    "settlement_obligations",
    "receipt_groups",
    "receipt_group_receipts",
    "receipt_finalization_audit",
)

# The remediated boundaries fail closed with typed errors from two distinct
# hierarchies (bridge errors derive from ``RuntimeError``, finalizer errors from
# ``ValueError``); these regressions assert *that* the path fails closed with no
# financial facts, and the focused suites assert the exact typed reasons.
FAIL_CLOSED_ERRORS = (RuntimeError, ValueError)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _setup_active_fact_set(
    conn: sqlite3.Connection,
    tmp_path: Path,
    suffix: str,
    **command_overrides: Any,
) -> tuple[ConvertedReceipt, Any]:
    """B1 -> confirmation -> B4.1 -> IAF persist, all through public boundaries."""
    ctx = setup_receipt(conn, tmp_path, suffix)
    result = persist_receipt_item_allocation_facts(
        conn, iaf_command(suffix, ctx, **command_overrides)
    )
    conn.commit()
    return ctx, result


def _counts(conn: sqlite3.Connection, tables: tuple[str, ...] = CANONICAL_FACT_TABLES) -> dict:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables
    }


def _authorization_state(conn: sqlite3.Connection, authorization_id: str) -> str | None:
    row = conn.execute(
        "SELECT authorization_state FROM receipt_finalization_authorizations "
        "WHERE authorization_id = ?",
        (authorization_id,),
    ).fetchone()
    return None if row is None else str(row["authorization_state"])


def _active_binding(conn: sqlite3.Connection, ctx: ConvertedReceipt) -> ActiveFactSetBinding:
    row = conn.execute(
        "SELECT fs.fact_set_public_id, fs.version, fs.fact_set_input_hash, "
        "fs.fact_set_result_hash "
        "FROM receipt_item_allocation_fact_sets fs "
        "JOIN receipts r ON r.id = fs.receipt_id "
        "WHERE r.public_id = ? AND fs.superseded_by_fact_set_public_id IS NULL",
        (ctx.receipt_public_id,),
    ).fetchone()
    assert row is not None
    return ActiveFactSetBinding(
        receipt_public_id=ctx.receipt_public_id,
        fact_set_public_id=str(row["fact_set_public_id"]),
        fact_set_version=int(row["version"]),
        fact_set_input_hash=str(row["fact_set_input_hash"]),
        fact_set_result_hash=str(row["fact_set_result_hash"]),
    )


def _negative_share_facts() -> dict[str, Any]:
    """A human-authored fact set whose subtract adjustment drives a share negative.

    Reconciles exactly (20.34 items - 8.00 discount = 12.34 authoritative net
    paid) and satisfies every fact-set content rule, but Alice's 3.00 item share
    minus her 8.00 manual discount share is -5.00, which the deterministic
    calculator refuses.
    """
    return {
        "items": [
            {
                "line_number": 1,
                "item_name": "Alice snack",
                "line_amount": "3.00",
                "currency": "SGD",
            },
            {
                "line_number": 2,
                "item_name": "Owner meal",
                "line_amount": "17.34",
                "currency": "SGD",
            },
        ],
        "allocations": [
            {
                "line_number": 1,
                "allocation_method": "manual",
                "participants": [
                    {
                        "participant_public_id": "person_alice",
                        "share_amount": "3.00",
                        "currency": "SGD",
                    },
                ],
            },
            {
                "line_number": 2,
                "allocation_method": "manual",
                "participants": [
                    {
                        "participant_public_id": "person_owner",
                        "share_amount": "17.34",
                        "currency": "SGD",
                    },
                ],
            },
        ],
        "adjustments": [
            {
                "adjustment_index": 1,
                "adjustment_type": "discount",
                "amount": "8.00",
                "currency": "SGD",
                "direction": "subtract",
                "allocation_method": "manual",
                "description": "Alice-only voucher larger than her share",
                "participants": [
                    {
                        "participant_public_id": "person_alice",
                        "share_amount": "8.00",
                        "currency": "SGD",
                    },
                ],
            }
        ],
    }


def _single_owner_facts() -> dict[str, Any]:
    """A payer-only receipt: every item is consumed by the payer alone."""
    return {
        "items": [
            {
                "line_number": 1,
                "item_name": "Solo groceries",
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
                        "participant_public_id": "person_owner",
                        "share_amount": "12.34",
                        "currency": "SGD",
                    },
                ],
            }
        ],
        "adjustments": [],
    }


# ---------------------------------------------------------------------------
# Single binding truth chain
# ---------------------------------------------------------------------------


def test_old_snapshot_material_with_new_active_binding_is_refused(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A v1 snapshot/calculation may never be finalized under the v2 binding.

    The authoritative snapshot, the human authorization, and the live active
    fact set must be one inseparable binding: substituting the newer active
    four-tuple into older hash-bound calculation material must fail closed with
    zero financial facts.
    """
    conn = migrated_temp_db_connection
    ctx, result_v1 = _setup_active_fact_set(conn, tmp_path, "mixbind")

    prepared_v1 = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    supersede_receipt_item_allocation_facts(conn, correction_command("mixbind", ctx, result_v1))
    conn.commit()
    prepared_v2 = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    assert prepared_v2.active_fact_set_binding.fact_set_version == 2

    # v1 snapshot identity, v1 calculation result, v2 active binding + evidence.
    forged = dataclasses.replace(
        prepared_v1,
        active_fact_set_binding=prepared_v2.active_fact_set_binding,
        source_evidence_refs=prepared_v2.source_evidence_refs,
    )

    before = _counts(conn)
    with pytest.raises(FAIL_CLOSED_ERRORS):
        authorization = authorize_receipt_finalization(conn, forged, actor_id="owner")
        finalize_prepared_receipt(conn, authorization)

    assert _counts(conn) == before
    assert before["transactions"] == 0
    assert before["settlement_obligations"] == 0
    assert not conn.in_transaction


def test_conflicting_durable_authorization_material_is_refused(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Authorization replay must compare complete durable material.

    A durable authorization row whose material fields drifted from the exact
    request must never be reused, even when its content hash, actor, and state
    still look acceptable.
    """
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "authdrift")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorize_receipt_finalization(conn, prepared, actor_id="owner")

    # Fixture surgery: drift one material authorization field only.
    conn.execute(
        "UPDATE receipt_finalization_authorizations "
        "SET payer_participant_public_id = 'person_bob' WHERE authorization_id = ?",
        (prepared.authorization_id,),
    )
    conn.commit()

    with pytest.raises(FAIL_CLOSED_ERRORS):
        authorize_receipt_finalization(conn, prepared, actor_id="owner")
    assert not conn.in_transaction


def test_conflicting_durable_calculation_run_is_refused(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A conflicting durable calculation run is a conflict, never idempotent."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "runconf")
    ids = _derive_ids(ctx.receipt_public_id, _active_binding(conn, ctx))

    # Fixture surgery: a pre-existing historical run with conflicting content.
    conn.execute(
        """
        INSERT INTO calc_audit_runs (
            run_id, run_type, entity_type, entity_id, rule_version,
            status, source_type, source_reference, created_at
        ) VALUES (?, 'receipt_split', 'receipt', ?, 'receipt-split-v1',
                  'calculated', 'iaf_active_fact_set', ?, '2026-07-30T00:00:00+00:00')
        """,
        (ids["calculation_run_public_id"], ctx.receipt_public_id, "f" * 64),
    )
    conn.commit()

    with pytest.raises(FAIL_CLOSED_ERRORS):
        prepare_receipt_calculation(conn, ctx.receipt_public_id)
    assert not conn.in_transaction


# ---------------------------------------------------------------------------
# Atomic group materialization
# ---------------------------------------------------------------------------


def test_finalizer_failure_leaves_no_receipt_group_or_membership(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Group materialization must roll back with the finalizer's own writes."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "grpatomic")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected finalizer seam failure")

    monkeypatch.setattr(finalizer, "_insert_settlement_obligations", _boom)
    with pytest.raises(RuntimeError, match="injected finalizer seam failure"):
        finalize_prepared_receipt(conn, authorization)

    assert _counts(conn) == dict.fromkeys(CANONICAL_FACT_TABLES, 0)
    assert _authorization_state(conn, prepared.authorization_id) == "authorized"
    assert not conn.in_transaction


def test_fact_set_supersession_still_possible_after_finalizer_failure(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed finalization must not permanently freeze fact-set correction."""
    conn = migrated_temp_db_connection
    ctx, result = _setup_active_fact_set(conn, tmp_path, "supafter")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected finalizer seam failure")

    monkeypatch.setattr(finalizer, "_insert_settlement_obligations", _boom)
    with pytest.raises(RuntimeError, match="injected finalizer seam failure"):
        finalize_prepared_receipt(conn, authorization)
    monkeypatch.undo()

    superseded = supersede_receipt_item_allocation_facts(
        conn, correction_command("supafter", ctx, result)
    )
    conn.commit()
    assert superseded.fact_set_version == 2
    assert superseded.supersedes_fact_set_public_id == result.fact_set_public_id


def test_foreign_receipt_group_is_not_silently_adopted(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A pre-existing group with a different identity must never be adopted."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "foreigngrp")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")

    # Fixture surgery: a foreign group already owns the deterministic public ID.
    conn.execute(
        """
        INSERT INTO receipt_groups (public_id, group_type, currency, status, source)
        VALUES (?, 'trip', 'SGD', 'active', 'legacy_import')
        """,
        (prepared.receipt_group_public_id,),
    )
    conn.commit()

    with pytest.raises(FAIL_CLOSED_ERRORS):
        finalize_prepared_receipt(conn, authorization)

    counts = _counts(conn)
    assert counts["transactions"] == 0
    assert counts["settlement_obligations"] == 0
    assert counts["receipt_group_receipts"] == 0
    assert _authorization_state(conn, prepared.authorization_id) == "authorized"
    assert not conn.in_transaction


# ---------------------------------------------------------------------------
# Canonical metadata and monetary totals
# ---------------------------------------------------------------------------


def test_canonical_transaction_preserves_confirmed_receipt_merchant_and_date(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """The canonical transaction must carry the confirmed receipt's identity."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "txnmeta")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    finalize_prepared_receipt(conn, authorization)

    receipt = dict(
        conn.execute(
            "SELECT merchant, receipt_datetime FROM receipts WHERE public_id = ?",
            (ctx.receipt_public_id,),
        ).fetchone()
    )
    txn = dict(conn.execute("SELECT merchant, transaction_date FROM transactions").fetchone())
    assert receipt["merchant"] == "COLD STORAGE"
    assert txn["merchant"] == receipt["merchant"]
    assert txn["transaction_date"] == receipt["receipt_datetime"]


def test_finalization_audit_total_to_collect_matches_calculator_and_settlement(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Audit totals must equal the calculator total and the persisted obligations."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "audittotal")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    calculator_total = Decimal(str(prepared.calculation_result["total_to_collect"]))
    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    output = finalize_prepared_receipt(conn, authorization)

    audit = dict(
        conn.execute(
            "SELECT total_paid, total_to_collect FROM receipt_finalization_audit "
            "WHERE finalization_id = ?",
            (output.finalization_public_id,),
        ).fetchone()
    )
    obligations_total = sum(
        (
            Decimal(str(row["amount"]))
            for row in conn.execute("SELECT amount FROM settlement_obligations").fetchall()
        ),
        Decimal("0.00"),
    )
    assert calculator_total == Decimal("6.17")
    assert Decimal(str(audit["total_paid"])) == Decimal("12.34")
    assert Decimal(str(audit["total_to_collect"])) == calculator_total
    assert obligations_total == calculator_total


# ---------------------------------------------------------------------------
# Readiness soundness
# ---------------------------------------------------------------------------


def test_negative_participant_share_fact_set_is_not_calculator_ready(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Positive readiness must guarantee the calculator accepts the projection.

    A persisted fact set whose exact shares fail the deterministic calculator's
    preconditions is an IA-D10 integrity failure, not an ordinary not-ready
    outcome.  Attempting readiness on such a fact set raises the typed error.
    """
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "negshare", **_negative_share_facts())

    from finance_core.calculators.receipt_calculator_readiness import ReceiptFactsIntegrityError

    with pytest.raises(ReceiptFactsIntegrityError):
        report_receipt_calculator_readiness(conn, ctx.receipt_public_id)

    before = _counts(conn, ("authoritative_calculation_snapshots", "calc_audit_runs"))
    with pytest.raises(FAIL_CLOSED_ERRORS):
        prepare_receipt_calculation(conn, ctx.receipt_public_id)
    assert _counts(conn, ("authoritative_calculation_snapshots", "calc_audit_runs")) == before


# ---------------------------------------------------------------------------
# Single-owner zero-obligation path
# ---------------------------------------------------------------------------


def test_single_owner_receipt_finalizes_with_zero_settlement_obligations(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A payer-only receipt finalizes through every guard with zero obligations."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(
        conn,
        tmp_path,
        "solo",
        membership=entries(("person_owner", 1), ("person_alice", 0), ("person_bob", 0)),
    )
    persist_receipt_item_allocation_facts(conn, iaf_command("solo", ctx, **_single_owner_facts()))
    conn.commit()

    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)
    assert prepared.payer_participant_public_id == "person_owner"
    assert prepared.calculation_result["settlement_obligations"] == []

    authorization = authorize_receipt_finalization(conn, prepared, actor_id="owner")
    output = finalize_prepared_receipt(conn, authorization)

    assert output.status == "finalized"
    assert output.obligations_created == 0
    counts = _counts(conn)
    assert counts["transactions"] == 1
    assert counts["calculation_runs"] == 1
    assert counts["settlement_obligations"] == 0
    assert counts["calculation_participant_shares"] == 1
    txn = dict(conn.execute("SELECT amount, currency FROM transactions").fetchone())
    assert Decimal(str(txn["amount"])) == Decimal("12.34")
    assert txn["currency"] == "SGD"
    assert _authorization_state(conn, prepared.authorization_id) == "consumed"


def test_prepared_calculation_dto_cannot_replace_snapshot_authority(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A caller DTO must not be able to substitute snapshot-bound material."""
    conn = migrated_temp_db_connection
    ctx, _ = _setup_active_fact_set(conn, tmp_path, "dtoauth")
    prepared = prepare_receipt_calculation(conn, ctx.receipt_public_id)

    tampered_result = dict(prepared.calculation_result)
    tampered_result["total_paid"] = "11.00"
    tampered: PreparedReceiptCalculation = dataclasses.replace(
        prepared, calculation_result=tampered_result
    )

    before = _counts(conn)
    with pytest.raises(FAIL_CLOSED_ERRORS):
        authorization = authorize_receipt_finalization(conn, tampered, actor_id="owner")
        finalize_prepared_receipt(conn, authorization)
    assert _counts(conn) == before
    assert not conn.in_transaction
