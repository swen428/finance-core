"""IAF.3 complete-replacement fact-set supersession boundary tests.

Only disposable staging databases are used.  The suite proves that a
correction creates a complete new version under the frozen transition-first
ordering, preserves every predecessor content row, binds both versions in
the audit chain, rejects stale expectations, and rolls back every injected
failure without repair.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

import finance_core.parser_proposals.receipt_item_allocation_facts as iaf_module
from finance_core.financial_audit import verify_financial_audit_chain
from finance_core.parser_proposals.receipt_item_allocation_facts import (
    RECEIPT_ITEM_ALLOCATION_FACTS_SUPERSEDED_EVENT_TYPE,
    RECEIPT_ITEM_ALLOCATION_FACTS_SUPERSEDED_PAYLOAD_FIELDS,
    SUPERSESSION_FAILURE_INJECTION_STAGES,
    InvalidItemFactsCommandError,
    ItemFactsCallerOwnedTransactionError,
    ItemFactsForeignKeysDisabledError,
    ItemFactsIdempotencyConflictError,
    ItemFactsPersistenceError,
    ItemFactsStagingDatabaseRejectedError,
    ReceiptItemAllocationFactsSupersessionCommand,
    StaleItemFactSetVersionError,
    StaleItemFactsReceiptBindingError,
    derive_fact_set_public_id,
    persist_receipt_item_allocation_facts,
    supersede_receipt_item_allocation_facts,
)
from tests.conftest import connect_temp_db
from tests.test_receipt_facts_conversion_v1 import (
    count_diff,
    evidence_rows,
    table_counts,
)
from tests.test_receipt_item_allocation_facts_service_v1 import (
    ConvertedReceipt,
    default_adjustments,
    default_allocations,
    default_items,
    event_payload_value,
    iaf_command,
    receipt_state,
    setup_receipt,
)


def replacement_items(label: str = "Corrected total") -> list[dict[str, Any]]:
    return [
        {
            "line_number": 1,
            "item_name": label,
            "line_amount": "12.34",
            "currency": "SGD",
        }
    ]


def replacement_allocations() -> list[dict[str, Any]]:
    return [
        {
            "line_number": 1,
            "allocation_method": "manual",
            "participants": [
                {
                    "participant_public_id": "person_owner",
                    "share_amount": "4.34",
                    "currency": "SGD",
                },
                {
                    "participant_public_id": "person_alice",
                    "share_amount": "8.00",
                    "currency": "SGD",
                },
            ],
        }
    ]


def correction_command(
    suffix: str,
    ctx: ConvertedReceipt,
    predecessor: Any,
    **overrides: Any,
) -> ReceiptItemAllocationFactsSupersessionCommand:
    fields: dict[str, Any] = {
        "command_public_id": f"riafc_{suffix}",
        "receipt_public_id": ctx.receipt_public_id,
        "expected_conversion_command_public_id": ctx.conversion_command_public_id,
        "expected_conversion_result_hash": ctx.conversion_result_hash,
        "expected_current_fact_set_public_id": predecessor.fact_set_public_id,
        "expected_current_fact_set_result_hash": predecessor.fact_set_result_hash,
        "items": replacement_items(),
        "allocations": replacement_allocations(),
        "adjustments": [],
        "authenticated_actor_id": "owner",
        "channel": "cli",
        "actor_type": "human",
        "reason": "human reviewed correction",
    }
    fields.update(overrides)
    return ReceiptItemAllocationFactsSupersessionCommand(**fields)


def snapshot_fact_set_rows(
    conn: sqlite3.Connection, fact_set_public_id: str
) -> dict[str, list[dict[str, Any]]]:
    return {
        "items": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM receipt_items WHERE fact_set_id = ? ORDER BY line_number",
                (fact_set_public_id,),
            ).fetchall()
        ],
        "allocations": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM receipt_item_allocation_facts "
                "WHERE fact_set_id = ? ORDER BY allocation_public_id",
                (fact_set_public_id,),
            ).fetchall()
        ],
        "adjustments": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM receipt_adjustments WHERE fact_set_id = ? ORDER BY adjustment_index",
                (fact_set_public_id,),
            ).fetchall()
        ],
    }


def assert_zero_write_rejection(
    conn: sqlite3.Connection,
    command: ReceiptItemAllocationFactsSupersessionCommand,
    error_type: type[Exception],
) -> Exception:
    before_counts = table_counts(conn)
    before_receipt = receipt_state(conn)
    before_evidence = evidence_rows(conn)
    before_registry = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM receipt_item_allocation_fact_sets ORDER BY receipt_id, version"
        ).fetchall()
    ]
    with pytest.raises(error_type) as excinfo:
        supersede_receipt_item_allocation_facts(conn, command)
    assert not conn.in_transaction
    assert count_diff(before_counts, table_counts(conn)) == {}
    assert receipt_state(conn) == before_receipt
    assert evidence_rows(conn) == before_evidence
    assert [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM receipt_item_allocation_fact_sets ORDER BY receipt_id, version"
        ).fetchall()
    ] == before_registry
    return excinfo.value


def test_complete_replacement_preserves_history_and_freezes_audit_contract(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "suphappy")
    initial = persist_receipt_item_allocation_facts(conn, iaf_command("suphappy", ctx))
    predecessor_rows = snapshot_fact_set_rows(conn, initial.fact_set_public_id)
    before_receipt = receipt_state(conn)
    before_evidence = evidence_rows(conn)
    before_counts = table_counts(conn)

    result = supersede_receipt_item_allocation_facts(
        conn, correction_command("suphappy_v2", ctx, initial)
    )

    assert result.idempotent is False
    assert result.fact_set_version == 2
    assert result.fact_set_public_id == derive_fact_set_public_id("riafc_suphappy_v2")
    assert result.supersedes_fact_set_public_id == initial.fact_set_public_id
    assert result.superseded_fact_set_result_hash == initial.fact_set_result_hash
    assert result.item_count == 1
    assert result.allocation_count == 2
    assert result.adjustment_count == 0
    assert not conn.in_transaction

    registry = conn.execute(
        "SELECT * FROM receipt_item_allocation_fact_sets WHERE receipt_id = ? ORDER BY version",
        (ctx.receipt_id,),
    ).fetchall()
    assert len(registry) == 2
    assert registry[0]["fact_set_public_id"] == initial.fact_set_public_id
    assert registry[0]["superseded_by_fact_set_public_id"] == result.fact_set_public_id
    assert registry[1]["supersedes_fact_set_public_id"] == initial.fact_set_public_id
    assert registry[1]["superseded_by_fact_set_public_id"] is None
    assert snapshot_fact_set_rows(conn, initial.fact_set_public_id) == predecessor_rows
    successor_rows = snapshot_fact_set_rows(conn, result.fact_set_public_id)
    assert [row["item_name"] for row in successor_rows["items"]] == ["Corrected total"]
    assert len(successor_rows["allocations"]) == 2
    assert successor_rows["adjustments"] == []

    event = conn.execute(
        "SELECT * FROM financial_audit_events WHERE event_public_id = ?",
        (result.audit_event_public_id,),
    ).fetchone()
    assert event["event_type"] == RECEIPT_ITEM_ALLOCATION_FACTS_SUPERSEDED_EVENT_TYPE
    payload = event_payload_value(event["event_payload_json"])
    assert tuple(sorted(payload)) == (RECEIPT_ITEM_ALLOCATION_FACTS_SUPERSEDED_PAYLOAD_FIELDS)
    assert payload == {
        "actor_type": "human",
        "adjustment_count": 0,
        "allocation_count": 2,
        "authenticated_actor_id": "owner",
        "channel": "cli",
        "command_material_hash": result.command_material_hash,
        "command_public_id": "riafc_suphappy_v2",
        "conversion_command_public_id": ctx.conversion_command_public_id,
        "conversion_result_hash": ctx.conversion_result_hash,
        "fact_set_input_hash": result.fact_set_input_hash,
        "fact_set_public_id": result.fact_set_public_id,
        "fact_set_result_hash": result.fact_set_result_hash,
        "fact_set_version": 2,
        "item_count": 1,
        "receipt_public_id": ctx.receipt_public_id,
        "superseded_fact_set_result_hash": initial.fact_set_result_hash,
        "supersedes_fact_set_public_id": initial.fact_set_public_id,
    }
    assert json.loads(event["source_evidence_refs_json"]) == sorted(
        [
            f"receipt:{ctx.receipt_public_id}",
            f"conversion:{ctx.conversion_command_public_id}",
            f"conversion-result-hash:{ctx.conversion_result_hash}",
            f"attachment-content-hash:{ctx.attachment_content_hash}",
            f"proposal-content-hash:{ctx.proposal_content_hash}",
            f"fact-set:{result.fact_set_public_id}",
            f"superseded-fact-set:{initial.fact_set_public_id}",
        ]
    )
    new_state = event_payload_value(event["new_state_json"])
    assert new_state == {
        "fact_set_status": "active",
        "receipt_public_id": ctx.receipt_public_id,
        "fact_set_public_id": result.fact_set_public_id,
        "fact_set_version": 2,
        "fact_set_input_hash": result.fact_set_input_hash,
        "fact_set_result_hash": result.fact_set_result_hash,
    }
    chain = verify_financial_audit_chain(
        conn, aggregate_type="receipt", aggregate_public_id=ctx.receipt_public_id
    )
    assert chain.valid and chain.event_count == 3

    assert count_diff(before_counts, table_counts(conn)) == {
        "financial_audit_events": 1,
        "receipt_item_allocation_fact_sets": 1,
        "receipt_item_allocation_facts": 2,
        "receipt_items": 1,
    }
    assert receipt_state(conn) == before_receipt
    assert evidence_rows(conn) == before_evidence


def test_supersession_replay_and_historical_create_replay_are_zero_write(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "supreplay")
    create_command = iaf_command("supreplay", ctx)
    initial = persist_receipt_item_allocation_facts(conn, create_command)
    command = correction_command("supreplay_v2", ctx, initial)
    first = supersede_receipt_item_allocation_facts(conn, command)
    before_counts = table_counts(conn)

    replay = supersede_receipt_item_allocation_facts(
        conn, dataclasses.replace(command, reason="different metadata")
    )
    historical = persist_receipt_item_allocation_facts(conn, create_command)

    assert replay == dataclasses.replace(first, idempotent=True)
    assert historical.idempotent is True
    assert historical.fact_set_public_id == initial.fact_set_public_id
    assert count_diff(before_counts, table_counts(conn)) == {}


def test_correction_item_array_order_is_nonsemantic_but_line_assignment_is_semantic(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Explicit line_number defines order; list presentation alone does not."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "suporder")
    initial = persist_receipt_item_allocation_facts(conn, iaf_command("suporder", ctx))
    command = correction_command(
        "suporder_v2",
        ctx,
        initial,
        items=default_items(),
        allocations=default_allocations(),
        adjustments=default_adjustments(),
    )
    first = supersede_receipt_item_allocation_facts(conn, command)

    reordered_allocations = list(reversed(default_allocations()))
    for entry in reordered_allocations:
        entry["participants"] = list(reversed(entry["participants"]))
    replay = supersede_receipt_item_allocation_facts(
        conn,
        dataclasses.replace(
            command,
            items=list(reversed(default_items())),
            allocations=reordered_allocations,
        ),
    )
    assert replay == dataclasses.replace(first, idempotent=True)

    reassigned_items = [
        {
            "line_number": 1,
            "item_name": "Kopi",
            "line_amount": "6.22",
            "currency": "SGD",
        },
        {
            "line_number": 2,
            "item_name": "Chicken Rice",
            "line_amount": "5.00",
            "currency": "SGD",
        },
    ]
    reassigned_allocations = [
        {
            "line_number": 1,
            "allocation_method": "equal_amount",
            "participants": [
                {"participant_public_id": "person_owner"},
                {"participant_public_id": "person_alice"},
            ],
        },
        {
            "line_number": 2,
            "allocation_method": "manual",
            "participants": [
                {
                    "participant_public_id": "person_owner",
                    "share_amount": "2.50",
                    "currency": "SGD",
                },
                {
                    "participant_public_id": "person_alice",
                    "share_amount": "2.50",
                    "currency": "SGD",
                },
            ],
        },
    ]
    assert_zero_write_rejection(
        conn,
        dataclasses.replace(
            command,
            items=reassigned_items,
            allocations=reassigned_allocations,
        ),
        ItemFactsIdempotencyConflictError,
    )


def test_malformed_numeric_predecessor_payload_maps_to_typed_persistence_error(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Persisted structural drift never leaks raw ValueError from correction."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "supdrift")
    initial = persist_receipt_item_allocation_facts(conn, iaf_command("supdrift", ctx))
    malformed_payload = {
        "schema_version": "v1",
        "receipt_public_id": ctx.receipt_public_id,
        "currency": "SGD",
        "net_paid_amount": "12.34",
        "items": [{"line_number": "x"}],
        "allocations": [],
        "adjustments": [],
    }
    payload_text = json.dumps(
        malformed_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    input_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    conn.execute("DROP TRIGGER trg_receipt_item_allocation_fact_sets_single_transition")
    conn.execute(
        "UPDATE receipt_item_allocation_fact_sets "
        "SET canonical_fact_set_payload = ?, fact_set_input_hash = ? "
        "WHERE fact_set_public_id = ?",
        (payload_text, input_hash, initial.fact_set_public_id),
    )
    conn.commit()

    error = assert_zero_write_rejection(
        conn,
        correction_command("supdrift_v2", ctx, initial),
        ItemFactsPersistenceError,
    )
    assert isinstance(error.__cause__, ValueError)
    assert "predecessor fact set failed" in str(error)


@pytest.mark.parametrize("field", ["public_id", "result_hash"])
def test_stale_predecessor_expectation_is_typed_and_zero_write(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    field: str,
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, f"supstale_{field}")
    initial = persist_receipt_item_allocation_facts(conn, iaf_command(f"supstale_{field}", ctx))
    overrides = (
        {"expected_current_fact_set_public_id": "rfs_stale"}
        if field == "public_id"
        else {"expected_current_fact_set_result_hash": "0" * 64}
    )
    assert_zero_write_rejection(
        conn,
        correction_command(f"supstale_{field}_v2", ctx, initial, **overrides),
        StaleItemFactSetVersionError,
    )


def test_old_expectation_cannot_supersede_the_winner(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "suploser")
    initial = persist_receipt_item_allocation_facts(conn, iaf_command("suploser", ctx))
    supersede_receipt_item_allocation_facts(
        conn, correction_command("suploser_winner", ctx, initial)
    )
    assert_zero_write_rejection(
        conn,
        correction_command("suploser_loser", ctx, initial),
        StaleItemFactSetVersionError,
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM receipt_item_allocation_fact_sets WHERE receipt_id = ?",
            (ctx.receipt_id,),
        ).fetchone()[0]
        == 2
    )


def test_second_correction_forms_gapless_three_version_chain(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "supv3")
    v1 = persist_receipt_item_allocation_facts(conn, iaf_command("supv3", ctx))
    v2 = supersede_receipt_item_allocation_facts(conn, correction_command("supv3_v2", ctx, v1))
    v3 = supersede_receipt_item_allocation_facts(
        conn,
        correction_command(
            "supv3_v3",
            ctx,
            v2,
            items=replacement_items("Reviewed again"),
        ),
    )
    rows = conn.execute(
        "SELECT version, fact_set_public_id, supersedes_fact_set_public_id, "
        "superseded_by_fact_set_public_id "
        "FROM receipt_item_allocation_fact_sets WHERE receipt_id = ? "
        "ORDER BY version",
        (ctx.receipt_id,),
    ).fetchall()
    assert [row["version"] for row in rows] == [1, 2, 3]
    assert rows[0]["superseded_by_fact_set_public_id"] == v2.fact_set_public_id
    assert rows[1]["supersedes_fact_set_public_id"] == v1.fact_set_public_id
    assert rows[1]["superseded_by_fact_set_public_id"] == v3.fact_set_public_id
    assert rows[2]["supersedes_fact_set_public_id"] == v2.fact_set_public_id
    assert rows[2]["superseded_by_fact_set_public_id"] is None


def test_correction_after_calculation_preserves_historical_artifacts(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """IA-D11: calculation history is allowed and never mutated by IAF.3."""
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "supcalc")
    initial = persist_receipt_item_allocation_facts(conn, iaf_command("supcalc", ctx))
    conn.execute(
        "INSERT INTO calculation_runs (public_id, calculation_version, scope_type, "
        "receipt_id, currency, input_hash) "
        "VALUES ('calc_iaf_supcalc', 'v1', 'receipt', ?, 'SGD', ?)",
        (ctx.receipt_id, initial.fact_set_input_hash),
    )
    conn.execute(
        """
        INSERT INTO authoritative_calculation_snapshots (
            snapshot_public_id, snapshot_schema_version, calculation_type,
            aggregate_public_id, input_payload_json, output_payload_json,
            rules_payload_json, input_hash, output_hash, rules_hash,
            combined_snapshot_hash, money_contract_version,
            currency_contract_version, algorithm_version, actor_type,
            finalization_status, created_at
        ) VALUES ('snap_iaf_supcalc', 'v1', 'receipt_split', ?, '{}', '{}', '{}',
                  ?, ?, ?, ?, 'v1', 'v1', 'v1', 'system', 'draft',
                  '2026-07-20T00:00:00Z')
        """,
        (ctx.receipt_public_id, "1" * 64, "2" * 64, "3" * 64, "4" * 64),
    )
    conn.commit()
    run_before = dict(
        conn.execute(
            "SELECT * FROM calculation_runs WHERE public_id = 'calc_iaf_supcalc'"
        ).fetchone()
    )
    snapshot_before = dict(
        conn.execute(
            "SELECT * FROM authoritative_calculation_snapshots "
            "WHERE snapshot_public_id = 'snap_iaf_supcalc'"
        ).fetchone()
    )

    result = supersede_receipt_item_allocation_facts(
        conn, correction_command("supcalc_v2", ctx, initial)
    )

    assert result.fact_set_version == 2
    assert (
        dict(
            conn.execute(
                "SELECT * FROM calculation_runs WHERE public_id = 'calc_iaf_supcalc'"
            ).fetchone()
        )
        == run_before
    )
    assert (
        dict(
            conn.execute(
                "SELECT * FROM authoritative_calculation_snapshots "
                "WHERE snapshot_public_id = 'snap_iaf_supcalc'"
            ).fetchone()
        )
        == snapshot_before
    )


def test_same_correction_id_with_changed_material_conflicts(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "supconflict")
    initial = persist_receipt_item_allocation_facts(conn, iaf_command("supconflict", ctx))
    command = correction_command("supconflict_v2", ctx, initial)
    supersede_receipt_item_allocation_facts(conn, command)
    assert_zero_write_rejection(
        conn,
        dataclasses.replace(command, items=replacement_items("Different correction")),
        ItemFactsIdempotencyConflictError,
    )


def test_correction_rejects_foreign_keys_disabled_before_any_write(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "supfk")
    initial = persist_receipt_item_allocation_facts(conn, iaf_command("supfk", ctx))
    command = correction_command("supfk_v2", ctx, initial)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        assert_zero_write_rejection(conn, command, ItemFactsForeignKeysDisabledError)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def test_correction_rejects_caller_transaction_without_rolling_it_back(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "suptxn")
    initial = persist_receipt_item_allocation_facts(conn, iaf_command("suptxn", ctx))
    conn.execute("BEGIN")
    with pytest.raises(ItemFactsCallerOwnedTransactionError):
        supersede_receipt_item_allocation_facts(conn, correction_command("suptxn_v2", ctx, initial))
    assert conn.in_transaction
    conn.rollback()
    assert (
        conn.execute(
            "SELECT superseded_by_fact_set_public_id "
            "FROM receipt_item_allocation_fact_sets "
            "WHERE fact_set_public_id = ?",
            (initial.fact_set_public_id,),
        ).fetchone()[0]
        is None
    )


def test_correction_busy_lock_maps_typed_then_succeeds(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "supbusy")
    initial = persist_receipt_item_allocation_facts(conn, iaf_command("supbusy", ctx))
    command = correction_command("supbusy_v2", ctx, initial)
    holder = connect_temp_db(migrated_temp_db_path)
    conn.execute("PRAGMA busy_timeout = 100")
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(ItemFactsPersistenceError) as excinfo:
            supersede_receipt_item_allocation_facts(conn, command)
        assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)
        assert not conn.in_transaction
    finally:
        holder.rollback()
        holder.close()
    assert supersede_receipt_item_allocation_facts(conn, command).fact_set_version == 2


def test_correction_rejects_unregistered_database_before_schema_access(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "supstaging")
    initial = persist_receipt_item_allocation_facts(conn, iaf_command("supstaging", ctx))
    plain = sqlite3.connect(str(tmp_path / "plain_iaf3_untrusted.sqlite"))
    try:
        with pytest.raises(ItemFactsStagingDatabaseRejectedError):
            supersede_receipt_item_allocation_facts(
                plain, correction_command("supstaging_v2", ctx, initial)
            )
    finally:
        plain.close()


class _InjectedFailure(RuntimeError):
    pass


def test_every_supersession_failure_stage_restores_active_predecessor(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "supinject")
    initial = persist_receipt_item_allocation_facts(conn, iaf_command("supinject", ctx))
    command = correction_command("supinject_v2", ctx, initial)
    before_counts = table_counts(conn)
    predecessor_rows = snapshot_fact_set_rows(conn, initial.fact_set_public_id)

    assert SUPERSESSION_FAILURE_INJECTION_STAGES == (
        "before_supersession_transition",
        "before_fact_set_registry_insert",
        "before_items_insert",
        "before_allocations_insert",
        "before_adjustments_insert",
        "before_audit_append",
        "before_persisted_verification",
        "before_commit",
    )
    try:
        for stage in SUPERSESSION_FAILURE_INJECTION_STAGES:

            def hook(current: str, *, target: str = stage) -> None:
                if current == target:
                    raise _InjectedFailure(target)

            iaf_module._failure_injection_hook = hook
            with pytest.raises(_InjectedFailure, match=stage):
                supersede_receipt_item_allocation_facts(conn, command)
            assert not conn.in_transaction
            predecessor = conn.execute(
                "SELECT superseded_by_fact_set_public_id "
                "FROM receipt_item_allocation_fact_sets "
                "WHERE fact_set_public_id = ?",
                (initial.fact_set_public_id,),
            ).fetchone()
            assert predecessor[0] is None
            assert count_diff(before_counts, table_counts(conn)) == {}
            assert snapshot_fact_set_rows(conn, initial.fact_set_public_id) == predecessor_rows
    finally:
        iaf_module._failure_injection_hook = None

    result = supersede_receipt_item_allocation_facts(conn, command)
    assert result.fact_set_version == 2


def test_finalized_receipt_rejected_before_transition(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "supfinal")
    initial = persist_receipt_item_allocation_facts(conn, iaf_command("supfinal", ctx))
    transaction = conn.execute(
        "INSERT INTO transactions (public_id, intent, intent_type, transaction_date) "
        "VALUES ('txn_iaf_supfinal', 'Expense', 'Generated', '2026-07-20')"
    )
    conn.execute("DROP TRIGGER trg_receipts_conversion_bound_freeze")
    conn.execute(
        "UPDATE receipts SET transaction_id = ? WHERE id = ?",
        (transaction.lastrowid, ctx.receipt_id),
    )
    conn.commit()
    assert_zero_write_rejection(
        conn,
        correction_command("supfinal_v2", ctx, initial),
        StaleItemFactsReceiptBindingError,
    )


def test_from_mapping_rejects_create_only_assertion_field() -> None:
    with pytest.raises(InvalidItemFactsCommandError, match="Unknown"):
        ReceiptItemAllocationFactsSupersessionCommand.from_mapping(
            {"expected_current_fact_set": "none"}
        )


def test_true_competing_corrections_have_one_winner(
    migrated_temp_db_connection: sqlite3.Connection,
    migrated_temp_db_path: Path,
    tmp_path: Path,
) -> None:
    conn = migrated_temp_db_connection
    ctx = setup_receipt(conn, tmp_path, "suprace")
    initial = persist_receipt_item_allocation_facts(conn, iaf_command("suprace", ctx))
    barrier = threading.Barrier(2, timeout=10)
    results: dict[str, object] = {}

    def racer(key: str) -> None:
        local = connect_temp_db(migrated_temp_db_path)
        try:
            local.execute("PRAGMA busy_timeout = 10000")
            barrier.wait()
            results[key] = supersede_receipt_item_allocation_facts(
                local, correction_command(f"suprace_{key}", ctx, initial)
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced in test thread
            results[key] = exc
        finally:
            local.close()

    threads = [threading.Thread(target=racer, args=(key,)) for key in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not any(thread.is_alive() for thread in threads)

    winners = [value for value in results.values() if not isinstance(value, BaseException)]
    losers = [value for value in results.values() if isinstance(value, BaseException)]
    assert len(winners) == 1 and len(losers) == 1
    assert isinstance(losers[0], (StaleItemFactSetVersionError, ItemFactsPersistenceError))
    rows = conn.execute(
        "SELECT version, superseded_by_fact_set_public_id "
        "FROM receipt_item_allocation_fact_sets WHERE receipt_id = ? ORDER BY version",
        (ctx.receipt_id,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["superseded_by_fact_set_public_id"] is not None
    assert rows[1]["superseded_by_fact_set_public_id"] is None
