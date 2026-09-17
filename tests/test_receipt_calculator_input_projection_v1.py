"""IAF.6 read-only calculator-input projection tests.

Covers Section 16 of
``docs/design/receipt_item_allocation_facts_boundary_v1.md``: a
calculator-ready IAF fact set is deterministically mapped onto the
``calculate_receipt_split`` input shape (SELECT-only), the immutable
active-fact-set binding four-tuple and source evidence are carried, the
projection is byte-stable across reconnects and row reorderings, and every
unmappable / not-ready / drifted / non-staging state fails closed with a
typed error and zero persisted effect.

Fixtures are built through public boundaries (B1 ingestion -> confirmation
-> B4.1 conversion -> IAF persist/supersede).  Direct SQL appears only in
clearly marked forged-drift/backstop fixtures.  Only disposable staging
databases are used; ``database/finance.db`` and seed data are untouched.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import finance_core.calculators.receipt_calculator_input_projection as projection
from finance_core.calculators.receipt_calculator_input_mapping import (
    CalculatorInputMappingError,
    project_adjustments,
    project_items,
)
from finance_core.calculators.receipt_calculator_input_projection import (
    ProjectionIntegrityError,
    ProjectionStagingDatabaseRejectedError,
    ReceiptCalculatorInputProjection,
    ReceiptCalculatorInputProjectionError,
    ReceiptNotCalculatorReadyError,
    project_receipt_calculator_input,
)
from finance_core.calculators.receipt_calculator_readiness import (
    ReceiptCalculatorReadinessError,
    report_receipt_calculator_readiness,
)
from finance_core.calculators.receipt_split_calculator import calculate_receipt_split
from finance_core.parser_proposals.receipt_item_allocation_facts import (
    derive_item_public_id,
    persist_receipt_item_allocation_facts,
    supersede_receipt_item_allocation_facts,
)
from tests.conftest import LIVE_DB_PATH, connect_temp_db
from tests.test_receipt_calculator_readiness_v1 import forge
from tests.test_receipt_facts_conversion_v1 import command as b41_command
from tests.test_receipt_facts_conversion_v1 import (
    convert,
    entries,
    seed_confirmed_receipt_proposal,
    seed_people,
    table_counts,
)
from tests.test_receipt_item_allocation_facts_service_v1 import (
    ConvertedReceipt,
    iaf_command,
    setup_receipt,
)
from tests.test_receipt_item_allocation_facts_supersession_v1 import (
    correction_command,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def all_rows(conn: sqlite3.Connection) -> dict[str, list[tuple[str, ...]]]:
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    return {
        table: [
            tuple(repr(value) for value in row)
            for row in conn.execute(f"SELECT rowid, * FROM {table} ORDER BY rowid").fetchall()
        ]
        for table in tables
    }


def project_with_zero_effects(
    conn: sqlite3.Connection, receipt_public_id: str
) -> ReceiptCalculatorInputProjection:
    """Project once and assert zero persisted effects and a released tx."""
    before_counts = table_counts(conn)
    before_rows = all_rows(conn)
    try:
        return project_receipt_calculator_input(conn, receipt_public_id)
    finally:
        assert not conn.in_transaction
        assert table_counts(conn) == before_counts
        assert all_rows(conn) == before_rows


def persist_default(
    conn: sqlite3.Connection, tmp_path: Path, suffix: str, **overrides: Any
) -> tuple[ConvertedReceipt, Any]:
    ctx = setup_receipt(conn, tmp_path, suffix)
    result = persist_receipt_item_allocation_facts(conn, iaf_command(suffix, ctx, **overrides))
    return ctx, result


# ---------------------------------------------------------------------------
# Happy paths + calculator contract
# ---------------------------------------------------------------------------


def test_projection_maps_default_fact_set_and_binding(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, result = persist_default(conn, tmp_path, "proj1")

    projection = project_with_zero_effects(conn, ctx.receipt_public_id)

    # Binding four-tuple is exact and drawn from the active fact set.
    assert projection.fact_set_public_id == result.fact_set_public_id
    assert projection.fact_set_version == 1
    assert projection.fact_set_input_hash == result.fact_set_input_hash
    assert projection.fact_set_result_hash == result.fact_set_result_hash
    assert projection.receipt_public_id == ctx.receipt_public_id
    assert projection.conversion_command_public_id == ctx.conversion_command_public_id
    assert projection.conversion_result_hash == ctx.conversion_result_hash
    assert projection.currency == "SGD"
    assert projection.net_paid_amount_canonical_text == "12.34"

    case = projection.calculator_input
    assert case["currency"] == "SGD"
    assert case["payer"] == "person_owner"
    assert case["participants"] == ["person_alice", "person_owner"]
    receipt = case["receipts"][0]
    assert receipt["paid_by"] == "person_owner"
    assert receipt["net_paid"] == "12.34"
    assert receipt["rounding_policy"] == "payer"
    assert [item["allocation_method"] for item in receipt["items"]] == ["manual", "equal"]
    assert receipt["items"][0]["allocations"] == {"person_owner": "2.50", "person_alice": "2.50"}
    assert receipt["items"][1]["participants"] == ["person_alice", "person_owner"]
    assert receipt["adjustments"][0]["allocation_method"] == "proportional_by_item_amount"
    assert receipt["adjustments"][0]["amount"] == "1.12"
    assert "participants" not in receipt["adjustments"][0]

    # Source evidence references are populated and hash-anchored.
    ev = projection.source_evidence
    assert ev.receipt_public_id == ctx.receipt_public_id
    assert ev.conversion_command_public_id == ctx.conversion_command_public_id
    assert ev.conversion_result_hash == ctx.conversion_result_hash
    assert ev.proposal_content_hash == ctx.proposal_content_hash
    assert ev.attachment_content_hash == ctx.attachment_content_hash
    assert ev.confirmation_public_id


def test_calculator_accepts_projection_and_reconciles(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """The calculator (invoked by the TEST, not the projection) reconciles."""
    conn = migrated_temp_db_connection
    ctx, _ = persist_default(conn, tmp_path, "projcalc")

    projection = project_with_zero_effects(conn, ctx.receipt_public_id)
    result = calculate_receipt_split(projection.calculator_input)

    assert result["currency"] == "SGD"
    assert result["payer"] == "person_owner"
    total = sum(result["participant_shares"].values(), Decimal(0))
    assert total == Decimal("12.34")
    assert set(result["participant_shares"]) == {"person_owner", "person_alice"}


def test_single_owner_equal_amount_projection(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = persist_default(
        conn,
        tmp_path,
        "projsingle",
        items=[{"line_number": 1, "item_name": "Solo", "line_amount": "12.34", "currency": "SGD"}],
        allocations=[
            {
                "line_number": 1,
                "allocation_method": "equal_amount",
                "participants": [{"participant_public_id": "person_owner"}],
            }
        ],
        adjustments=[],
    )

    projection = project_with_zero_effects(conn, ctx.receipt_public_id)
    receipt = projection.calculator_input["receipts"][0]
    assert receipt["items"][0]["allocation_method"] == "equal"
    assert receipt["items"][0]["participants"] == ["person_owner"]
    assert receipt["adjustments"] == []

    result = calculate_receipt_split(projection.calculator_input)
    assert result["participant_shares"]["person_owner"] == Decimal("12.34")


def test_multi_owner_equal_amount_consumer_order_deterministic(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = persist_default(
        conn,
        tmp_path,
        "projmulti",
        items=[
            {"line_number": 1, "item_name": "Shared", "line_amount": "12.34", "currency": "SGD"}
        ],
        allocations=[
            {
                "line_number": 1,
                "allocation_method": "equal_amount",
                "participants": [
                    {"participant_public_id": "person_alice"},
                    {"participant_public_id": "person_owner"},
                ],
            }
        ],
        adjustments=[],
    )

    projection = project_with_zero_effects(conn, ctx.receipt_public_id)
    # Consumer order follows the persisted allocation order, deterministically.
    assert projection.calculator_input["receipts"][0]["items"][0]["participants"] == [
        "person_alice",
        "person_owner",
    ]


def test_payer_excluded_as_consumer_still_in_participant_set(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Payer excluded as a consumer (is_included=0) still projects as payer."""
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "projpayer")
    # person_owner pays but is excluded as a consumer; alice + bob consume.
    membership = entries(("person_owner", 0), ("person_alice", 1), ("person_bob", 1))
    conv = convert(conn, b41_command("projpayer", public_id, expected, participants=membership))
    ctx = ConvertedReceipt(
        receipt_public_id=conv.receipt_public_id,
        receipt_id=conv.receipt_id,
        conversion_command_public_id="rpfc_projpayer",
        conversion_result_hash=conv.conversion_result_hash,
        proposal_content_hash=conv.proposal_content_hash,
        attachment_content_hash="",
    )
    persist_receipt_item_allocation_facts(
        conn,
        iaf_command(
            "projpayer",
            ctx,
            items=[
                {"line_number": 1, "item_name": "Shared", "line_amount": "12.34", "currency": "SGD"}
            ],
            allocations=[
                {
                    "line_number": 1,
                    "allocation_method": "equal_amount",
                    "participants": [
                        {"participant_public_id": "person_alice"},
                        {"participant_public_id": "person_bob"},
                    ],
                }
            ],
            adjustments=[],
        ),
    )

    projection = project_with_zero_effects(conn, ctx.receipt_public_id)
    case = projection.calculator_input
    assert case["payer"] == "person_owner"
    assert case["participants"] == ["person_alice", "person_bob", "person_owner"]

    result = calculate_receipt_split(case)
    assert result["participant_shares"]["person_owner"] == Decimal("0.00")
    assert result["participant_shares"]["person_alice"] == Decimal("6.17")
    assert result["participant_shares"]["person_bob"] == Decimal("6.17")


def test_projection_deterministic_across_reconnect(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = persist_default(conn, tmp_path, "projdet")

    first = project_with_zero_effects(conn, ctx.receipt_public_id)
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.fact_set_public_id = "x"  # type: ignore[misc]

    fresh = connect_temp_db(migrated_temp_db_path)
    try:
        assert project_with_zero_effects(fresh, ctx.receipt_public_id) == first
    finally:
        fresh.close()


def test_supersession_projects_only_new_active_version(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, v1 = persist_default(conn, tmp_path, "projsup")
    v2 = supersede_receipt_item_allocation_facts(conn, correction_command("projsup_v2", ctx, v1))

    projection = project_with_zero_effects(conn, ctx.receipt_public_id)
    assert projection.fact_set_public_id == v2.fact_set_public_id
    assert projection.fact_set_version == 2
    assert projection.fact_set_result_hash == v2.fact_set_result_hash
    receipt = projection.calculator_input["receipts"][0]
    assert receipt["items"][0]["allocations"] == {"person_owner": "4.34", "person_alice": "8.00"}


# ---------------------------------------------------------------------------
# SELECT-only, snapshot ownership, calculator non-invocation
# ---------------------------------------------------------------------------


def test_projection_preserves_caller_owned_transaction(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = persist_default(conn, tmp_path, "projtx")
    conn.execute("BEGIN")
    try:
        assert conn.in_transaction
        projection = project_receipt_calculator_input(conn, ctx.receipt_public_id)
        # A caller-owned transaction is neither committed nor rolled back.
        assert conn.in_transaction
        assert projection.fact_set_version == 1
    finally:
        conn.rollback()
    assert not conn.in_transaction


def test_projection_owns_and_releases_its_transaction(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = persist_default(conn, tmp_path, "projowntx")
    assert not conn.in_transaction
    project_receipt_calculator_input(conn, ctx.receipt_public_id)
    assert not conn.in_transaction


def test_projection_does_not_invoke_the_calculator(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import finance_core.calculators.receipt_split_calculator as calc

    calls: list[Any] = []
    monkeypatch.setattr(calc, "calculate_receipt_split", lambda case: calls.append(case))
    conn = migrated_temp_db_connection
    ctx, _ = persist_default(conn, tmp_path, "projnospy")

    project_with_zero_effects(conn, ctx.receipt_public_id)
    assert calls == []


def test_reordered_physical_rows_do_not_change_projection(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    """Deleting+reinserting membership in reverse order must not change output."""
    conn = migrated_temp_db_connection
    ctx, _ = persist_default(conn, tmp_path, "projorder")
    baseline = project_with_zero_effects(conn, ctx.receipt_public_id)

    # Reinsert membership rows in reverse rowid order via a guard-dropped forge;
    # projection sorts by public ID and must be unaffected.
    rows = conn.execute(
        "SELECT public_id, receipt_id, participant_id, role, is_included "
        "FROM receipt_participants WHERE receipt_id = ? ORDER BY rowid DESC",
        (ctx.receipt_id,),
    ).fetchall()
    forge(
        conn,
        (
            "trg_receipt_participants_conversion_bound_no_delete",
            "trg_receipt_participants_conversion_bound_freeze",
            "trg_receipt_participants_conversion_bound_no_insert",
            "trg_receipt_participants_no_insert_collision",
        ),
        "DELETE FROM receipt_participants WHERE receipt_id = ?",
        (ctx.receipt_id,),
    )
    for row in rows:
        conn.execute(
            "INSERT INTO receipt_participants "
            "(public_id, receipt_id, participant_id, role, is_included) VALUES (?, ?, ?, ?, ?)",
            tuple(row),
        )
    conn.commit()

    assert project_with_zero_effects(conn, ctx.receipt_public_id) == baseline


# ---------------------------------------------------------------------------
# Fail-closed paths
# ---------------------------------------------------------------------------


def test_not_ready_receipt_is_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    seed_people(conn)
    _pid, public_id, expected = seed_confirmed_receipt_proposal(conn, tmp_path, "projnotready")
    membership = entries(("person_owner", 1), ("person_alice", 1), ("person_bob", 0))
    conv = convert(conn, b41_command("projnotready", public_id, expected, participants=membership))

    with pytest.raises(ReceiptNotCalculatorReadyError) as excinfo:
        project_with_zero_effects(conn, conv.receipt_public_id)
    assert excinfo.value.not_ready_reasons == (
        "no_authoritative_item_facts",
        "no_authoritative_allocation_facts",
    )


def test_forged_result_hash_drift_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, result = persist_default(conn, tmp_path, "projhash")
    forge(
        conn,
        ("trg_receipt_item_allocation_fact_sets_single_transition",),
        "UPDATE receipt_item_allocation_fact_sets SET fact_set_result_hash = ? "
        "WHERE fact_set_public_id = ?",
        ("f" * 64, result.fact_set_public_id),
    )
    # readiness fails closed first (service-depth), surfaced as its own typed error.
    with pytest.raises(ReceiptCalculatorReadinessError):
        project_with_zero_effects(conn, ctx.receipt_public_id)


def test_forged_null_fact_set_id_adhoc_row_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = persist_default(conn, tmp_path, "projadhoc")
    conn.execute(
        "INSERT INTO receipt_items (public_id, receipt_id, item_name, line_amount, currency) "
        "VALUES ('rcit_adhoc_proj', ?, 'AD HOC', 1.00, 'SGD')",
        (ctx.receipt_id,),
    )
    conn.commit()
    # An ad-hoc item row fails closed either at readiness (service depth) or at
    # the projection's own fact-set-bound guard; both are typed boundary errors.
    with pytest.raises((ReceiptCalculatorReadinessError, ReceiptCalculatorInputProjectionError)):
        project_with_zero_effects(conn, ctx.receipt_public_id)


def test_forged_canonical_text_drift_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, result = persist_default(conn, tmp_path, "projtext")
    item_public_id = derive_item_public_id(result.fact_set_public_id, 1)
    forge(
        conn,
        ("trg_receipt_items_fact_set_bound_freeze",),
        "UPDATE receipt_items SET line_amount_canonical_text = '9.99' WHERE public_id = ?",
        (item_public_id,),
    )
    with pytest.raises(ReceiptCalculatorReadinessError):
        project_with_zero_effects(conn, ctx.receipt_public_id)


def test_broken_ocr_evidence_chain_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A missing OCR proposal link (broken evidence chain) fails closed.

    Readiness runs full service-depth persisted-state verification, so a
    broken evidence chain is caught there and surfaced as a typed readiness
    error; the projection never proceeds to build calculator input.
    """
    conn = migrated_temp_db_connection
    ctx, _ = persist_default(conn, tmp_path, "projev")
    forge(
        conn,
        ("trg_receipt_ocr_proposal_links_no_delete",),
        "DELETE FROM receipt_ocr_proposal_links WHERE parser_output_id = ("
        "SELECT parser_output_id FROM receipt_proposal_conversions "
        "WHERE command_public_id = ?)",
        (ctx.conversion_command_public_id,),
    )
    with pytest.raises(ReceiptCalculatorReadinessError):
        project_with_zero_effects(conn, ctx.receipt_public_id)


def test_source_evidence_guard_backstops_broken_chain(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Defense-in-depth (white-box): the projection's own source-evidence
    guard fails closed when the OCR link resolves to no extraction, even
    though the readiness service-depth verifier normally rejects such a fact
    set first.  SELECT-only, so no persisted effect is possible.
    """
    conn = migrated_temp_db_connection
    ctx, _ = persist_default(conn, tmp_path, "projevwb")
    conversion = {
        "parser_output_id": -1,  # resolves to zero OCR proposal links
        "command_public_id": ctx.conversion_command_public_id,
        "conversion_result_hash": ctx.conversion_result_hash,
        "confirmation_public_id": "pca_projevwb",
        "proposal_content_hash": ctx.proposal_content_hash,
    }
    with pytest.raises(ProjectionIntegrityError):
        projection._load_source_evidence(conn, ctx.receipt_public_id, conversion)


def test_unknown_receipt_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    with pytest.raises(ReceiptCalculatorReadinessError):
        project_with_zero_effects(conn, "rcpt_does_not_exist")


def test_blank_receipt_id_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    with pytest.raises(ProjectionIntegrityError):
        project_receipt_calculator_input(conn, "   ")


# ---------------------------------------------------------------------------
# Staging / live database rejection
# ---------------------------------------------------------------------------


def test_plain_database_rejected(tmp_path: Path) -> None:
    plain_path = tmp_path / "plain.db"
    plain = sqlite3.connect(str(plain_path))
    try:
        with pytest.raises(ProjectionStagingDatabaseRejectedError):
            project_receipt_calculator_input(plain, "rcpt_anything")
    finally:
        plain.close()


def test_copied_staging_database_rejected(migrated_temp_db_path: Path, tmp_path: Path) -> None:
    import shutil

    copy_path = tmp_path / "copied.db"
    shutil.copyfile(migrated_temp_db_path, copy_path)
    copied = sqlite3.connect(str(copy_path))
    try:
        with pytest.raises(ProjectionStagingDatabaseRejectedError):
            project_receipt_calculator_input(copied, "rcpt_anything")
    finally:
        copied.close()


@pytest.mark.skipif(not LIVE_DB_PATH.exists(), reason="live database not present")
def test_live_database_rejected_via_readonly_connection() -> None:
    conn = sqlite3.connect(f"file:{LIVE_DB_PATH}?mode=ro", uri=True)
    try:
        with pytest.raises(ProjectionStagingDatabaseRejectedError):
            project_receipt_calculator_input(conn, "rcpt_anything")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Adjustment-method coverage: every approved v1 method maps verbatim and the
# calculator reconciles the projection (Section 16 / IA-D7b).
# ---------------------------------------------------------------------------


def _adjustment(method: str, *, direction: str = "add", **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "adjustment_index": 1,
        "adjustment_type": "service_charge" if direction == "add" else "discount",
        "amount": "1.12",
        "currency": "SGD",
        "direction": direction,
        "allocation_method": method,
        "description": f"{method} {direction}",
    }
    entry.update(extra)
    return entry


@pytest.mark.parametrize(
    "method",
    ["equal_per_participant", "payer_only", "proportional_by_item_amount"],
)
def test_add_adjustment_methods_map_verbatim_and_reconcile(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path, method: str
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = persist_default(conn, tmp_path, f"adjadd_{method}", adjustments=[_adjustment(method)])

    projection = project_with_zero_effects(conn, ctx.receipt_public_id)
    adjustment = projection.calculator_input["receipts"][0]["adjustments"][0]
    assert adjustment["allocation_method"] == method
    assert adjustment["direction"] == "add"
    assert adjustment["amount"] == "1.12"
    assert adjustment["currency"] == "SGD"
    # Only the manual method carries an explicit per-participant share map.
    assert "allocations" not in adjustment
    assert "participants" not in adjustment

    result = calculate_receipt_split(projection.calculator_input)
    assert sum(result["participant_shares"].values(), Decimal(0)) == Decimal("12.34")


def test_manual_adjustment_maps_to_share_map_and_reconciles(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = persist_default(
        conn,
        tmp_path,
        "adjmanual",
        adjustments=[
            _adjustment(
                "manual",
                adjustment_type="manual_adjustment",
                participants=[
                    {
                        "participant_public_id": "person_owner",
                        "share_amount": "0.62",
                        "currency": "SGD",
                    },
                    {
                        "participant_public_id": "person_alice",
                        "share_amount": "0.50",
                        "currency": "SGD",
                    },
                ],
            )
        ],
    )

    projection = project_with_zero_effects(conn, ctx.receipt_public_id)
    adjustment = projection.calculator_input["receipts"][0]["adjustments"][0]
    assert adjustment["allocation_method"] == "manual"
    assert adjustment["allocations"] == {"person_owner": "0.62", "person_alice": "0.50"}
    assert "participants" not in adjustment

    result = calculate_receipt_split(projection.calculator_input)
    assert sum(result["participant_shares"].values(), Decimal(0)) == Decimal("12.34")


def test_subtract_direction_adjustment_maps_and_reconciles(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """A subtract-direction discount (items > net paid) reconciles to net paid."""
    conn = migrated_temp_db_connection
    equal_both = [
        {"participant_public_id": "person_owner"},
        {"participant_public_id": "person_alice"},
    ]
    ctx, _ = persist_default(
        conn,
        tmp_path,
        "adjsub",
        items=[
            {"line_number": 1, "item_name": "A", "line_amount": "7.24", "currency": "SGD"},
            {"line_number": 2, "item_name": "B", "line_amount": "6.22", "currency": "SGD"},
        ],
        allocations=[
            {"line_number": 1, "allocation_method": "equal_amount", "participants": equal_both},
            {"line_number": 2, "allocation_method": "equal_amount", "participants": equal_both},
        ],
        adjustments=[_adjustment("payer_only", direction="subtract")],
    )

    projection = project_with_zero_effects(conn, ctx.receipt_public_id)
    adjustment = projection.calculator_input["receipts"][0]["adjustments"][0]
    assert adjustment["direction"] == "subtract"
    assert adjustment["allocation_method"] == "payer_only"
    assert adjustment["amount"] == "1.12"

    result = calculate_receipt_split(projection.calculator_input)
    assert sum(result["participant_shares"].values(), Decimal(0)) == Decimal("12.34")


# ---------------------------------------------------------------------------
# Mid-read active-binding change: if the active fact set observed by the
# projection read no longer matches the readiness report, fail closed even
# when readiness itself was positive (concurrent-supersession backstop).
# ---------------------------------------------------------------------------


def test_active_binding_change_between_readiness_and_projection_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = migrated_temp_db_connection
    ctx, _ = persist_default(conn, tmp_path, "projbind")

    # A genuinely positive readiness report whose active-binding identity has
    # been tampered to a different fact set simulates a supersession landing
    # between the readiness read and the fact-set read.
    real = report_receipt_calculator_readiness(conn, ctx.receipt_public_id)
    assert real.is_calculator_ready
    tampered = dataclasses.replace(real, active_fact_set_public_id="rias_stale_binding")
    monkeypatch.setattr(projection, "report_receipt_calculator_readiness", lambda c, r: tampered)

    with pytest.raises(ProjectionIntegrityError):
        project_with_zero_effects(conn, ctx.receipt_public_id)


# ---------------------------------------------------------------------------
# Defense-in-depth mapping guards (white-box on the pure mappers): an
# unsupported allocation/adjustment method fails closed even though the
# readiness/service layers normally reject such payloads first, and non-2dp
# currency canonical text is carried byte-verbatim (never reformatted).
# ---------------------------------------------------------------------------


def test_unsupported_item_allocation_method_fails_closed() -> None:
    payload = {
        "items": [{"line_number": 1, "item_name": "X", "line_amount": "1.00"}],
        "allocations": [
            {
                "line_number": 1,
                "allocation_method": "weighted",
                "participants": [{"participant_public_id": "person_owner"}],
            }
        ],
    }
    with pytest.raises(CalculatorInputMappingError):
        project_items(payload, {"person_owner"})


def test_unsupported_adjustment_allocation_method_fails_closed() -> None:
    payload = {
        "adjustments": [
            {
                "adjustment_index": 1,
                "adjustment_type": "service_charge",
                "direction": "add",
                "allocation_method": "weighted",
                "amount": "1.00",
            }
        ]
    }
    with pytest.raises(CalculatorInputMappingError):
        project_adjustments(payload, {"person_owner"}, "SGD")


def test_non_two_dp_currency_amounts_carried_verbatim() -> None:
    included = {"person_owner"}
    item_payload = {
        "items": [
            {"line_number": 1, "item_name": "Ramen", "line_amount": "1000"},
            {"line_number": 2, "item_name": "Gyoza", "line_amount": "500"},
        ],
        "allocations": [
            {
                "line_number": 1,
                "allocation_method": "equal_amount",
                "participants": [{"participant_public_id": "person_owner"}],
            },
            {
                "line_number": 2,
                "allocation_method": "manual",
                "participants": [{"participant_public_id": "person_owner", "share_amount": "500"}],
            },
        ],
    }
    items = project_items(item_payload, included)
    assert items[0]["amount"] == "1000"
    assert items[1]["allocations"] == {"person_owner": "500"}

    adjustment_payload = {
        "adjustments": [
            {
                "adjustment_index": 1,
                "adjustment_type": "service_charge",
                "direction": "add",
                "allocation_method": "equal_per_participant",
                "amount": "100",
            }
        ]
    }
    adjustments = project_adjustments(adjustment_payload, included, "JPY")
    assert adjustments[0]["amount"] == "100"
    assert adjustments[0]["currency"] == "JPY"
