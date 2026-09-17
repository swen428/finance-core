"""Focused tests for the B3 receipt total-proposal supersession boundary.

These tests exercise migration 034 constraints and the guarded
``supersede_receipt_total_proposal()`` public API: Money Contract
enforcement, effective-payload derivation, evidence inheritance, the
``superseding_correction`` link, idempotent replay, typed conflicts,
concurrency, chained corrections, confirmation invalidation, failure
injection with full rollback, and the staging-database guard.  All fixtures
are synthetic and privacy-safe; only temporary migrated SQLite databases are
used.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

import finance_core.parser_proposals.receipt_supersession as supersession_module
from finance_core.parser_proposals import (
    complete_proposal,
    confirm_proposal,
    supersede_receipt_total_proposal,
)
from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash
from finance_core.parser_proposals.receipt_supersession import (
    InvalidCorrectionIdError,
    InvalidSupersessionFieldValueError,
    InvalidSupersessionStatusError,
    NoMaterialSupersessionChangeError,
    NonMonetarySupersessionError,
    RawIntakeBindingError,
    ReceiptSupersessionError,
    StaleSupersessionContentError,
    StaleSupersessionTargetError,
    SupersessionConflictError,
    SupersessionPersistenceError,
    UnauthorizedSupersessionActorError,
    UnknownSupersessionFieldError,
    UnsupportedSupersessionProposalError,
)
from finance_core.parser_proposals.service import (
    InvalidProposalStatusError,
    convert_confirmed_parser_proposal,
)
from finance_core.staging_guard import StagingDatabaseError
from tests.conftest import connect_temp_db
from tests.test_receipt_ocr_proposal_ingestion_v1 import (
    _ingest,
    _prepare_extraction,
    _sgd_blocks,
)

_PENDING = "parsed_pending_confirmation"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _seed_receipt_proposal(
    conn: sqlite3.Connection, tmp_path: Path, suffix: str
) -> tuple[int, str]:
    """Create one B1 receipt total proposal; return (parser_output_id, hash)."""
    extraction_public_id = _prepare_extraction(conn, tmp_path, suffix=suffix, blocks=_sgd_blocks())
    result = _ingest(
        conn,
        extraction_public_id,
        proposal=f"prop_{suffix}",
        link=f"ropl_{suffix}",
    )
    return result.parser_output_id, _hash_of(conn, result.parser_output_id)


def _seed_cross_currency_proposal(
    conn: sqlite3.Connection,
    tmp_path: Path,
    suffix: str,
    *,
    total_marker: str,
    total_value: str,
) -> tuple[int, str]:
    """Create a receipt total proposal whose OCR total uses a custom currency marker."""
    extraction_public_id = _prepare_extraction(
        conn,
        tmp_path,
        suffix=suffix,
        blocks=_sgd_blocks(total_marker=total_marker, total_value=total_value),
    )
    result = _ingest(
        conn,
        extraction_public_id,
        proposal=f"prop_{suffix}",
        link=f"ropl_{suffix}",
    )
    return result.parser_output_id, _hash_of(conn, result.parser_output_id)


def _hash_of(conn: sqlite3.Connection, parser_output_id: int) -> str:
    return compute_effective_proposal_content_hash(conn, {"id": parser_output_id})


def _supersede(
    conn: sqlite3.Connection,
    parser_output_id: int,
    expected_hash: str,
    field_updates: dict[str, Any],
    *,
    cid: str = "rcor_test_1",
    actor: str = "owner",
    actor_type: str = "human",
    channel: str = "cli",
    reason: str | None = None,
) -> dict[str, Any]:
    return supersede_receipt_total_proposal(
        conn,
        parser_output_id,
        actor=actor,
        expected_content_hash=expected_hash,
        field_updates=field_updates,
        correction_public_id=cid,
        actor_type=actor_type,
        correction_channel=channel,
        reason=reason,
    )


def _row(conn: sqlite3.Connection, sql: str, *params: Any) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _payload(conn: sqlite3.Connection, parser_output_id: int) -> dict[str, Any]:
    row = _row(conn, "SELECT parsed_payload FROM parser_outputs WHERE id = ?", parser_output_id)
    assert row is not None
    return json.loads(row["parsed_payload"])


# ---------------------------------------------------------------------------
# Migration 034 schema and append-only enforcement
# ---------------------------------------------------------------------------


def test_revision_table_and_indexes_exist(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    table = _row(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='receipt_proposal_revisions'",
    )
    assert table is not None
    indexes = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='receipt_proposal_revisions'"
        ).fetchall()
    }
    assert "idx_receipt_proposal_revisions_superseded" in indexes
    assert "idx_receipt_proposal_revisions_replacement" in indexes
    assert "idx_receipt_proposal_revisions_superseded_hash" in indexes


def test_revision_rows_are_append_only(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "append")
    _supersede(conn, pid, expected, {"amount": "20.00"}, cid="rcor_append_1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE receipt_proposal_revisions SET reason = 'x' "
            "WHERE correction_public_id = 'rcor_append_1'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "DELETE FROM receipt_proposal_revisions WHERE correction_public_id = 'rcor_append_1'"
        )


# ---------------------------------------------------------------------------
# Successful monetary corrections
# ---------------------------------------------------------------------------


def test_amount_only_correction(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "amt")
    result = _supersede(conn, pid, expected, {"amount": "45.60"}, cid="rcor_amt_1")

    assert result["idempotent"] is False
    assert result["superseded_parser_output_id"] == pid
    assert result["parent_from_status"] == _PENDING
    assert result["parent_to_status"] == "superseded"
    assert result["replacement_parse_status"] == _PENDING
    assert result["changed_fields"] == ["amount"]
    assert result["actor_type"] == "human"
    assert result["superseded_content_hash"] == expected
    assert result["replacement_content_hash"] != expected

    child_payload = _payload(conn, result["replacement_parser_output_id"])
    assert child_payload["amount"] == "45.60"
    assert child_payload["currency"] == "SGD"
    assert child_payload["is_final"] is False
    assert child_payload["confirmation_required"] is True

    parent = _row(conn, "SELECT parse_status FROM parser_outputs WHERE id = ?", pid)
    assert parent is not None and parent["parse_status"] == "superseded"


def test_currency_only_correction_revalidates_existing_amount(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "cur")
    result = _supersede(conn, pid, expected, {"currency": "usd"}, cid="rcor_cur_1")
    child_payload = _payload(conn, result["replacement_parser_output_id"])
    assert child_payload["currency"] == "USD"
    assert child_payload["amount"] == "12.34"
    assert result["changed_fields"] == ["currency"]


def test_amount_and_currency_correction(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "both")
    result = _supersede(
        conn, pid, expected, {"amount": "1000", "currency": "JPY"}, cid="rcor_both_1"
    )
    child_payload = _payload(conn, result["replacement_parser_output_id"])
    assert child_payload["amount"] == "1000"
    assert child_payload["currency"] == "JPY"
    assert result["changed_fields"] == ["amount", "currency"]


def test_monetary_correction_carrying_non_monetary_fields(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "mixed")
    result = _supersede(
        conn,
        pid,
        expected,
        {
            "amount": "99.90",
            "transaction_date": "2026-07-21",
            "merchant": "FairPrice",
            "description": "groceries",
            "category": "food",
        },
        cid="rcor_mixed_1",
    )
    child_payload = _payload(conn, result["replacement_parser_output_id"])
    assert child_payload["amount"] == "99.90"
    assert child_payload["transaction_date"] == "2026-07-21"
    assert child_payload["merchant"] == "FairPrice"
    assert child_payload["description"] == "groceries"
    assert child_payload["category"] == "food"
    assert result["changed_fields"] == [
        "amount",
        "category",
        "description",
        "merchant",
        "transaction_date",
    ]


# ---------------------------------------------------------------------------
# Command validation and boundary routing
# ---------------------------------------------------------------------------


def test_non_monetary_only_request_is_rejected_and_completion_still_works(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "nonmon")
    with pytest.raises(NonMonetarySupersessionError):
        _supersede(conn, pid, expected, {"merchant": "Sheng Siong"}, cid="rcor_nm_1")
    assert _count(conn, "receipt_proposal_revisions") == 0
    # The existing completion boundary still handles non-monetary edits.
    completion = complete_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=expected,
        field_updates={"merchant": "Sheng Siong"},
        completion_public_id="pco_nm_1",
    )
    assert completion["to_status"] == "edited_pending_confirmation"


def test_unknown_and_nested_fields_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "unknown")
    with pytest.raises(UnknownSupersessionFieldError):
        _supersede(conn, pid, expected, {"amount": "5.00", "intent": "expense"})
    with pytest.raises(UnknownSupersessionFieldError):
        _supersede(conn, pid, expected, {"amount": {"value": "5.00"}})
    assert _count(conn, "receipt_proposal_revisions") == 0


def test_actor_authentication_rules(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "actor")
    for bad_type in ("system", "ai", "cli", ""):
        with pytest.raises(UnauthorizedSupersessionActorError):
            _supersede(conn, pid, expected, {"amount": "5.00"}, actor_type=bad_type)
    with pytest.raises(UnauthorizedSupersessionActorError):
        _supersede(conn, pid, expected, {"amount": "5.00"}, actor="")
    with pytest.raises(UnauthorizedSupersessionActorError):
        _supersede(conn, pid, expected, {"amount": "5.00"}, actor=" owner ")
    with pytest.raises(ReceiptSupersessionError):
        _supersede(conn, pid, expected.upper(), {"amount": "5.00"})
    # ``user`` is a compatibility alias normalized to the persisted ``human``.
    result = _supersede(
        conn, pid, expected, {"amount": "5.00"}, cid="rcor_actor_1", actor_type="user"
    )
    assert result["actor_type"] == "human"
    revision = _row(
        conn,
        "SELECT actor_type FROM receipt_proposal_revisions WHERE correction_public_id = ?",
        "rcor_actor_1",
    )
    assert revision is not None and revision["actor_type"] == "human"


def test_correction_id_format_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "cid")
    for bad in ("", "rcor_", "cid_x", "rcor_has space", "rcor_" + "x" * 196):
        with pytest.raises(InvalidCorrectionIdError):
            _supersede(conn, pid, expected, {"amount": "5.00"}, cid=bad)


# ---------------------------------------------------------------------------
# Money Contract
# ---------------------------------------------------------------------------


def test_money_contract_invalid_values_fail_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "money")
    invalid_updates: list[dict[str, Any]] = [
        {"amount": "-5.00"},
        {"amount": "0"},
        {"amount": "12.345"},
        {"amount": "abc"},
        {"amount": True},
        {"currency": "XXX_NOT_REAL"},
        {"currency": 123},
        {"amount": "12.34", "currency": "JPY"},  # JPY forbids decimals
    ]
    for updates in invalid_updates:
        with pytest.raises(InvalidSupersessionFieldValueError):
            _supersede(conn, pid, expected, updates)
    assert _count(conn, "receipt_proposal_revisions") == 0
    assert _hash_of(conn, pid) == expected


def test_invalid_non_monetary_values_fail_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "nonmonval")
    with pytest.raises(InvalidSupersessionFieldValueError):
        _supersede(conn, pid, expected, {"amount": "5.00", "transaction_date": "21/07/2026"})
    with pytest.raises(InvalidSupersessionFieldValueError):
        _supersede(conn, pid, expected, {"amount": "5.00", "transaction_date": "2026-02-30"})
    with pytest.raises(InvalidSupersessionFieldValueError):
        _supersede(conn, pid, expected, {"amount": "5.00", "merchant": "   "})


# ---------------------------------------------------------------------------
# Stale hash / no material change
# ---------------------------------------------------------------------------


def test_stale_hash_rejected_without_writes(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, _expected = _seed_receipt_proposal(conn, tmp_path, "stale")
    with pytest.raises(StaleSupersessionContentError):
        _supersede(conn, pid, "0" * 64, {"amount": "20.00"})
    assert _count(conn, "receipt_proposal_revisions") == 0
    parent = _row(conn, "SELECT parse_status FROM parser_outputs WHERE id = ?", pid)
    assert parent is not None and parent["parse_status"] == _PENDING


def test_no_material_change_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "nochange")
    with pytest.raises(NoMaterialSupersessionChangeError):
        _supersede(conn, pid, expected, {"amount": "12.34"})
    with pytest.raises(NoMaterialSupersessionChangeError):
        _supersede(conn, pid, expected, {"amount": "12.34", "currency": "sgd"})
    assert _count(conn, "receipt_proposal_revisions") == 0


# ---------------------------------------------------------------------------
# Receipt-only boundary
# ---------------------------------------------------------------------------


def test_proposal_without_ocr_link_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    cursor = conn.execute(
        """
        INSERT INTO parser_outputs (public_id, source_type, parser_name, parser_version,
                                    parsed_payload, parse_status)
        VALUES ('po_text_1', 'telegram_text', 'text', 'v1',
                '{"amount": "5.00", "currency": "SGD"}', 'parsed_pending_confirmation')
        """
    )
    conn.commit()
    assert cursor.lastrowid is not None
    pid = int(cursor.lastrowid)
    with pytest.raises(UnsupportedSupersessionProposalError):
        _supersede(conn, pid, _hash_of(conn, pid), {"amount": "6.00"})


def test_missing_proposal_rejected(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    with pytest.raises(ReceiptSupersessionError):
        _supersede(migrated_temp_db_connection, 999999, "0" * 64, {"amount": "6.00"})


# ---------------------------------------------------------------------------
# Parent immutability, child linkage, inheritance, evidence
# ---------------------------------------------------------------------------


def test_parent_payload_immutable_and_child_parent_relationship(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "immut")
    before = _row(conn, "SELECT parsed_payload, public_id FROM parser_outputs WHERE id = ?", pid)
    assert before is not None
    result = _supersede(conn, pid, expected, {"amount": "77.70"}, cid="rcor_immut_1")

    after = _row(conn, "SELECT parsed_payload FROM parser_outputs WHERE id = ?", pid)
    assert after is not None
    assert after["parsed_payload"] == before["parsed_payload"]

    child = _row(
        conn,
        "SELECT parent_parser_output_id, public_id FROM parser_outputs WHERE id = ?",
        result["replacement_parser_output_id"],
    )
    assert child is not None
    assert child["parent_parser_output_id"] == pid
    assert child["public_id"] == result["replacement_proposal_public_id"]
    assert child["public_id"].startswith("po_rev_")


def test_child_inherits_raw_intake_attachment_source_and_ocr_identity(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "inherit")
    result = _supersede(conn, pid, expected, {"amount": "88.80"}, cid="rcor_inh_1")
    parent = _row(conn, "SELECT * FROM parser_outputs WHERE id = ?", pid)
    child = _row(
        conn,
        "SELECT * FROM parser_outputs WHERE id = ?",
        result["replacement_parser_output_id"],
    )
    assert parent is not None and child is not None
    for column in (
        "source_type",
        "source_public_id",
        "statement_batch_id",
        "attachment_id",
        "parser_name",
        "parser_version",
        "raw_text",
        "confidence_score",
    ):
        assert child[column] == parent[column]

    parent_link = _row(
        conn,
        "SELECT extraction_id, parser_contract_version FROM receipt_ocr_proposal_links "
        "WHERE parser_output_id = ?",
        pid,
    )
    child_link = _row(
        conn,
        "SELECT extraction_id, parser_contract_version, link_role, public_id "
        "FROM receipt_ocr_proposal_links WHERE parser_output_id = ?",
        result["replacement_parser_output_id"],
    )
    assert parent_link is not None and child_link is not None
    assert child_link["extraction_id"] == parent_link["extraction_id"]
    assert child_link["parser_contract_version"] == parent_link["parser_contract_version"]
    assert child_link["link_role"] == "superseding_correction"
    assert child_link["public_id"] == result["link_public_id"]
    assert child_link["public_id"].startswith("ropl_rev_")

    child_payload = _payload(conn, result["replacement_parser_output_id"])
    assert child_payload["ocr_evidence"]["extraction_public_id"].startswith("rocr_")


def test_corrected_fields_not_attributed_to_ocr(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "evid")
    result = _supersede(conn, pid, expected, {"amount": "33.30"}, cid="rcor_evid_1")
    child_id = result["replacement_parser_output_id"]

    child_payload = _payload(conn, child_id)
    assert child_payload["field_confidence"]["amount"] is None
    amount_evidence = [
        item for item in child_payload["field_evidence"] if item.get("field_name") == "amount"
    ]
    assert len(amount_evidence) == 1
    assert amount_evidence[0]["evidence_source_type"] == "user_message"
    assert amount_evidence[0]["correction_public_id"] == "rcor_evid_1"

    rows = conn.execute(
        "SELECT field_name, evidence_source_type FROM parser_proposal_field_evidence "
        "WHERE parser_output_id = ?",
        (child_id,),
    ).fetchall()
    by_field = {row["field_name"]: row["evidence_source_type"] for row in rows}
    assert by_field["amount"] == "user_message"
    # Uncorrected fields keep their inherited OCR provenance.
    assert by_field["currency"] == "ocr"


# ---------------------------------------------------------------------------
# Completion interplay
# ---------------------------------------------------------------------------


def test_completion_before_supersession_flows_into_child(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "compbefore")
    complete_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=expected,
        field_updates={"merchant": "Don Don Donki"},
        completion_public_id="pco_before_1",
    )
    new_hash = _hash_of(conn, pid)
    assert new_hash != expected
    result = _supersede(conn, pid, new_hash, {"amount": "50.00"}, cid="rcor_cb_1")
    child_payload = _payload(conn, result["replacement_parser_output_id"])
    assert child_payload["merchant"] == "Don Don Donki"
    assert child_payload["amount"] == "50.00"
    assert result["parent_from_status"] == "edited_pending_confirmation"


def test_completion_after_supersession_works_on_child(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "compafter")
    result = _supersede(conn, pid, expected, {"amount": "60.00"}, cid="rcor_ca_1")
    child_id = result["replacement_parser_output_id"]
    completion = complete_proposal(
        conn,
        child_id,
        actor="owner",
        expected_content_hash=result["replacement_content_hash"],
        field_updates={"category": "food"},
        completion_public_id="pco_after_1",
    )
    assert completion["to_status"] == "edited_pending_confirmation"


# ---------------------------------------------------------------------------
# Idempotency, conflicts, concurrency, chains
# ---------------------------------------------------------------------------


def test_same_key_replay_is_idempotent(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "replay")
    first = _supersede(conn, pid, expected, {"amount": "21.00"}, cid="rcor_rep_1")
    second = _supersede(conn, pid, expected, {"amount": "21.00"}, cid="rcor_rep_1", reason="retry")
    assert second["idempotent"] is True
    assert second["replacement_parser_output_id"] == first["replacement_parser_output_id"]
    assert second["replacement_content_hash"] == first["replacement_content_hash"]
    assert second["link_public_id"] == first["link_public_id"]
    assert _count(conn, "receipt_proposal_revisions") == 1


def test_same_key_replay_survives_reconnect(migrated_temp_db_path: Path, tmp_path: Path) -> None:
    conn = connect_temp_db(migrated_temp_db_path)
    try:
        pid, expected = _seed_receipt_proposal(conn, tmp_path, "restart")
        first = _supersede(conn, pid, expected, {"amount": "22.00"}, cid="rcor_rst_1")
    finally:
        conn.close()
    conn2 = connect_temp_db(migrated_temp_db_path)
    try:
        replay = _supersede(conn2, pid, expected, {"amount": "22.00"}, cid="rcor_rst_1")
        assert replay["idempotent"] is True
        assert replay["replacement_parser_output_id"] == first["replacement_parser_output_id"]
    finally:
        conn2.close()


def test_same_key_changed_material_conflicts(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "conflict")
    _supersede(conn, pid, expected, {"amount": "23.00"}, cid="rcor_conf_1")
    with pytest.raises(SupersessionConflictError):
        _supersede(conn, pid, expected, {"amount": "24.00"}, cid="rcor_conf_1")
    with pytest.raises(SupersessionConflictError):
        _supersede(conn, pid, expected, {"amount": "23.00"}, cid="rcor_conf_1", actor="other")
    assert _count(conn, "receipt_proposal_revisions") == 1


def test_concurrent_identical_commands_one_write_one_replay(
    migrated_temp_db_path: Path,
) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as work_dir:
        setup = connect_temp_db(migrated_temp_db_path)
        try:
            pid, expected = _seed_receipt_proposal(setup, Path(work_dir), "race")
        finally:
            setup.close()

        results: list[dict[str, Any]] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def worker() -> None:
            conn = connect_temp_db(migrated_temp_db_path)
            conn.execute("PRAGMA busy_timeout = 5000")
            try:
                barrier.wait(timeout=5)
                results.append(
                    _supersede(conn, pid, expected, {"amount": "30.00"}, cid="rcor_race_1")
                )
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                errors.append(exc)
            finally:
                assert conn.in_transaction is False
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert errors == []
        assert sorted(result["idempotent"] for result in results) == [False, True]

        check = connect_temp_db(migrated_temp_db_path)
        try:
            assert _count(check, "receipt_proposal_revisions") == 1
        finally:
            check.close()


def test_chained_corrections_and_stale_branch_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "chain")
    first = _supersede(conn, pid, expected, {"amount": "40.00"}, cid="rcor_chain_1")
    child_id = first["replacement_parser_output_id"]

    # Chained correction supersedes the current child, not the original.
    second = _supersede(
        conn,
        child_id,
        first["replacement_content_hash"],
        {"amount": "41.00"},
        cid="rcor_chain_2",
    )
    assert second["superseded_parser_output_id"] == child_id
    grandchild_payload = _payload(conn, second["replacement_parser_output_id"])
    assert grandchild_payload["amount"] == "41.00"

    # Branching from the already-superseded original is stale and rejected.
    with pytest.raises(StaleSupersessionTargetError):
        _supersede(conn, pid, expected, {"amount": "42.00"}, cid="rcor_chain_3")
    assert _count(conn, "receipt_proposal_revisions") == 2


# ---------------------------------------------------------------------------
# Raw intake pointer, lifecycle, and audit evidence
# ---------------------------------------------------------------------------


def test_raw_intake_repointed_and_lifecycle_audit_evidence(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "evidence")
    result = _supersede(conn, pid, expected, {"amount": "70.00"}, cid="rcor_ev_1")
    child_id = result["replacement_parser_output_id"]

    intake = _row(
        conn,
        "SELECT status FROM raw_intake_records WHERE parser_output_id = ?",
        child_id,
    )
    assert intake is not None and intake["status"] == _PENDING
    old_pointer = _row(conn, "SELECT 1 FROM raw_intake_records WHERE parser_output_id = ?", pid)
    assert old_pointer is None

    events = conn.execute(
        "SELECT parser_output_id, event_type, from_status, to_status, actor_identifier "
        "FROM parser_proposal_events WHERE parser_output_id IN (?, ?) "
        "ORDER BY id",
        (pid, child_id),
    ).fetchall()
    parent_events = [e for e in events if e["parser_output_id"] == pid]
    child_events = [e for e in events if e["parser_output_id"] == child_id]
    assert any(
        e["event_type"] == "superseded" and e["to_status"] == "superseded" for e in parent_events
    )
    assert any(e["event_type"] == "created" and e["to_status"] == _PENDING for e in child_events)

    audit = _row(
        conn,
        "SELECT aggregate_public_id, event_type, causation_public_id "
        "FROM financial_audit_events WHERE event_type = 'receipt_proposal_superseded'",
    )
    assert audit is not None
    assert audit["causation_public_id"] == "rcor_ev_1"

    revision = _row(
        conn,
        "SELECT superseded_content_hash, replacement_content_hash "
        "FROM receipt_proposal_revisions WHERE correction_public_id = 'rcor_ev_1'",
    )
    assert revision is not None
    assert revision["superseded_content_hash"] == expected
    assert revision["replacement_content_hash"] == result["replacement_content_hash"]


# ---------------------------------------------------------------------------
# Confirmation boundary
# ---------------------------------------------------------------------------


def test_fresh_confirmation_succeeds_on_child(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "freshcfm")
    result = _supersede(conn, pid, expected, {"amount": "80.00"}, cid="rcor_fc_1")
    child_id = result["replacement_parser_output_id"]
    confirmation = confirm_proposal(
        conn, child_id, actor="owner", confirmation_public_id="pca_fresh_child_1"
    )
    assert confirmation["to_status"] == "confirmed"
    assert confirmation["final_transaction_created"] is False
    assert _count(conn, "transactions") == 0


def test_parent_confirmation_identity_cannot_confirm_child(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "parentcfm")
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_parent_1")
    result = _supersede(conn, pid, expected, {"amount": "81.00"}, cid="rcor_pc_1")
    child_id = result["replacement_parser_output_id"]

    # The child has no authorization of its own.
    authorization = _row(
        conn,
        "SELECT 1 FROM parser_proposal_authorizations WHERE parser_output_id = ?",
        child_id,
    )
    assert authorization is None

    # Reusing the parent's confirmation identity must fail on the primary-key
    # uniqueness of parser_proposal_authorizations and confirm nothing.
    with pytest.raises(sqlite3.IntegrityError):
        confirm_proposal(conn, child_id, actor="owner", confirmation_public_id="pca_parent_1")
    child = _row(conn, "SELECT parse_status FROM parser_outputs WHERE id = ?", child_id)
    assert child is not None and child["parse_status"] == _PENDING

    # A fresh confirmation with the child's own identity still succeeds.
    fresh = confirm_proposal(
        conn, child_id, actor="owner", confirmation_public_id="pca_child_own_1"
    )
    assert fresh["to_status"] == "confirmed"


def test_confirmed_but_unconverted_proposal_can_be_superseded(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "cfmsup")
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_cfmsup_1")
    result = _supersede(conn, pid, expected, {"amount": "82.00"}, cid="rcor_cs_1")
    assert result["parent_from_status"] == "confirmed"
    assert result["parent_to_status"] == "superseded"
    assert result["replacement_parse_status"] == _PENDING
    assert _count(conn, "transactions") == 0

    # The stale confirmed decision must no longer be convertible: the parent
    # is superseded and only its fresh-confirmed replacement could convert.
    with pytest.raises(InvalidProposalStatusError):
        convert_confirmed_parser_proposal(conn, pid)
    assert _count(conn, "transactions") == 0


def test_converted_proposal_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "converted")
    confirm_proposal(conn, pid, actor="owner", confirmation_public_id="pca_conv_1")
    # B4.0 blocks receipt proposals from the legacy converter, so a historical
    # legacy conversion is simulated with direct test-only audit inserts.
    _insert_legacy_conversion_audit(conn, pid, "pca_conv_1")
    with pytest.raises(InvalidSupersessionStatusError):
        _supersede(conn, pid, _hash_of(conn, pid), {"amount": "83.00"})
    assert _count(conn, "receipt_proposal_revisions") == 0


def _insert_legacy_conversion_audit(
    conn: sqlite3.Connection, parser_output_id: int, confirmation_public_id: str
) -> None:
    """Simulate a pre-B4.0 legacy conversion audit trail (test-only inserts)."""
    cursor = conn.execute(
        "INSERT INTO transactions (public_id, intent, intent_type, transaction_date) "
        "VALUES (?, 'personal_expense_log', 'Generated', '2026-07-20')",
        (f"txn_legacy_test_{parser_output_id}",),
    )
    conn.execute(
        "INSERT INTO parser_proposal_conversion_audit ("
        "parser_output_id, transaction_id, confirmation_public_id, "
        "proposal_content_hash, authenticated_actor_id) VALUES (?, ?, ?, ?, 'owner')",
        (
            parser_output_id,
            cursor.lastrowid,
            confirmation_public_id,
            _hash_of(conn, parser_output_id),
        ),
    )
    conn.commit()


def test_other_terminal_statuses_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "rejected")
    from finance_core.parser_proposals import reject_proposal

    reject_proposal(conn, pid, actor="owner", confirmation_public_id="pca_rej_1")
    with pytest.raises(InvalidSupersessionStatusError):
        _supersede(conn, pid, expected, {"amount": "84.00"})


# ---------------------------------------------------------------------------
# Failure injection: every write boundary rolls back completely
# ---------------------------------------------------------------------------


_FAILURE_STAGES = (
    "before_child_insert",
    "before_field_evidence_insert",
    "before_link_insert",
    "before_revision_insert",
    "before_parent_status_update",
    "before_raw_intake_repoint",
    "before_audit_append",
    "before_commit",
)


@pytest.mark.parametrize("stage", _FAILURE_STAGES)
def test_failure_injection_rolls_back_completely(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, f"fail_{stage}")
    before_outputs = _count(conn, "parser_outputs")
    before_links = _count(conn, "receipt_ocr_proposal_links")
    before_events = _count(conn, "parser_proposal_events")
    before_audit = _count(conn, "financial_audit_events")
    before_evidence = _count(conn, "parser_proposal_field_evidence")
    intake_before = _row(
        conn, "SELECT status FROM raw_intake_records WHERE parser_output_id = ?", pid
    )
    assert intake_before is not None

    class InjectedFailure(RuntimeError):
        pass

    def hook(current: str) -> None:
        if current == stage:
            raise InjectedFailure(stage)

    monkeypatch.setattr(supersession_module, "_failure_injection_hook", hook)
    with pytest.raises(InjectedFailure):
        _supersede(conn, pid, expected, {"amount": "90.00"}, cid=f"rcor_f_{stage}")

    assert conn.in_transaction is False
    assert _count(conn, "receipt_proposal_revisions") == 0
    assert _count(conn, "parser_outputs") == before_outputs
    assert _count(conn, "receipt_ocr_proposal_links") == before_links
    assert _count(conn, "parser_proposal_events") == before_events
    assert _count(conn, "financial_audit_events") == before_audit
    assert _count(conn, "parser_proposal_field_evidence") == before_evidence
    parent = _row(conn, "SELECT parse_status FROM parser_outputs WHERE id = ?", pid)
    assert parent is not None and parent["parse_status"] == _PENDING
    intake = _row(conn, "SELECT status FROM raw_intake_records WHERE parser_output_id = ?", pid)
    assert intake is not None
    assert intake["status"] == intake_before["status"]

    # The same command succeeds after the transient failure clears.
    monkeypatch.setattr(supersession_module, "_failure_injection_hook", None)
    result = _supersede(conn, pid, expected, {"amount": "90.00"}, cid=f"rcor_f_{stage}")
    assert result["idempotent"] is False


# ---------------------------------------------------------------------------
# Staging guard and no-final-fact invariants
# ---------------------------------------------------------------------------


def test_staging_database_guard_rejects_unauthorized_database(tmp_path: Path) -> None:
    plain = sqlite3.connect(str(tmp_path / "plain.db"))
    plain.row_factory = sqlite3.Row
    try:
        with pytest.raises(StagingDatabaseError):
            supersede_receipt_total_proposal(
                plain,
                1,
                actor="owner",
                expected_content_hash="0" * 64,
                field_updates={"amount": "1.00"},
                correction_public_id="rcor_guard_1",
            )
    finally:
        plain.close()


def test_supersession_creates_no_final_financial_facts(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "nofacts")
    result = _supersede(conn, pid, expected, {"amount": "95.00"}, cid="rcor_nf_1")
    confirm_proposal(
        conn,
        result["replacement_parser_output_id"],
        actor="owner",
        confirmation_public_id="pca_nf_1",
    )
    assert _count(conn, "transactions") == 0
    assert _count(conn, "parser_proposal_conversion_audit") == 0


# ---------------------------------------------------------------------------
# Material monetary delta gate: echoed no-op monetary values
# ---------------------------------------------------------------------------


def test_echoed_amount_with_merchant_change_rejected_without_writes(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "echoamt")
    before_outputs = _count(conn, "parser_outputs")
    with pytest.raises(NonMonetarySupersessionError):
        _supersede(conn, pid, expected, {"amount": "12.34", "merchant": "New Merchant"})
    assert _count(conn, "receipt_proposal_revisions") == 0
    assert _count(conn, "parser_outputs") == before_outputs
    assert _hash_of(conn, pid) == expected
    parent = _row(conn, "SELECT parse_status FROM parser_outputs WHERE id = ?", pid)
    assert parent is not None and parent["parse_status"] == _PENDING


def test_echoed_currency_with_non_monetary_changes_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "echocur")
    # "sgd" normalizes to the unchanged effective currency SGD.
    with pytest.raises(NonMonetarySupersessionError):
        _supersede(
            conn,
            pid,
            expected,
            {"currency": "sgd", "category": "groceries", "transaction_date": "2026-07-21"},
        )
    assert _count(conn, "receipt_proposal_revisions") == 0
    assert _hash_of(conn, pid) == expected


def test_echoed_monetary_pair_with_description_change_rejected(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "echopair")
    with pytest.raises(NonMonetarySupersessionError):
        _supersede(
            conn,
            pid,
            expected,
            {"amount": "12.34", "currency": "SGD", "description": "dinner"},
        )
    assert _count(conn, "receipt_proposal_revisions") == 0
    assert _hash_of(conn, pid) == expected


def test_amount_change_with_echoed_currency_records_only_material_change(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "echomix")
    parent_payload = _payload(conn, pid)
    result = _supersede(
        conn, pid, expected, {"amount": "45.60", "currency": "sgd"}, cid="rcor_echomix_1"
    )
    assert result["changed_fields"] == ["amount"]

    revision = _row(
        conn,
        "SELECT field_updates_json, applied_field_updates_json "
        "FROM receipt_proposal_revisions WHERE correction_public_id = 'rcor_echomix_1'",
    )
    assert revision is not None
    # Supplied command fields (replay identity) keep the echoed currency ...
    assert json.loads(revision["field_updates_json"]) == {"amount": "45.60", "currency": "SGD"}
    # ... but only the material change is recorded as applied.
    assert json.loads(revision["applied_field_updates_json"]) == {"amount": "45.60"}

    child_payload = _payload(conn, result["replacement_parser_output_id"])
    assert child_payload["field_confidence"]["amount"] is None
    # The echoed currency is never re-attributed: it keeps OCR provenance.
    assert (
        child_payload["field_confidence"]["currency"]
        == parent_payload["field_confidence"]["currency"]
    )
    assert child_payload["field_confidence"]["currency"] is not None
    currency_evidence = [
        item for item in child_payload["field_evidence"] if item.get("field_name") == "currency"
    ]
    assert len(currency_evidence) == 1
    assert currency_evidence[0]["evidence_source_type"] == "ocr"
    rows = conn.execute(
        "SELECT field_name, evidence_source_type FROM parser_proposal_field_evidence "
        "WHERE parser_output_id = ?",
        (result["replacement_parser_output_id"],),
    ).fetchall()
    by_field = {row["field_name"]: row["evidence_source_type"] for row in rows}
    assert by_field["currency"] == "ocr"
    assert by_field["amount"] == "user_message"


def test_currency_change_with_echoed_amount_keeps_amount_ocr_provenance(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "echoamt2")
    parent_payload = _payload(conn, pid)
    result = _supersede(
        conn, pid, expected, {"currency": "USD", "amount": "12.34"}, cid="rcor_echoamt2_1"
    )
    assert result["changed_fields"] == ["currency"]

    revision = _row(
        conn,
        "SELECT applied_field_updates_json FROM receipt_proposal_revisions "
        "WHERE correction_public_id = 'rcor_echoamt2_1'",
    )
    assert revision is not None
    assert json.loads(revision["applied_field_updates_json"]) == {"currency": "USD"}

    child_payload = _payload(conn, result["replacement_parser_output_id"])
    assert child_payload["field_confidence"]["currency"] is None
    assert (
        child_payload["field_confidence"]["amount"] == parent_payload["field_confidence"]["amount"]
    )
    assert child_payload["field_confidence"]["amount"] is not None
    amount_evidence = [
        item for item in child_payload["field_evidence"] if item.get("field_name") == "amount"
    ]
    assert len(amount_evidence) == 1
    assert amount_evidence[0]["evidence_source_type"] == "ocr"


# ---------------------------------------------------------------------------
# Chained corrections preserve each field's original correction provenance
# ---------------------------------------------------------------------------


def test_chained_corrections_preserve_original_correction_provenance(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "provchain")
    parent_public = _row(conn, "SELECT public_id FROM parser_outputs WHERE id = ?", pid)
    assert parent_public is not None

    first = _supersede(conn, pid, expected, {"amount": "40.00"}, cid="rcor_provA_1")
    child_id = first["replacement_parser_output_id"]
    second = _supersede(
        conn,
        child_id,
        first["replacement_content_hash"],
        {"currency": "USD"},
        cid="rcor_provB_1",
    )
    grandchild_id = second["replacement_parser_output_id"]

    grandchild_payload = _payload(conn, grandchild_id)
    amount_items = [
        item for item in grandchild_payload["field_evidence"] if item.get("field_name") == "amount"
    ]
    currency_items = [
        item
        for item in grandchild_payload["field_evidence"]
        if item.get("field_name") == "currency"
    ]
    assert len(amount_items) == 1 and len(currency_items) == 1
    # Correction A's evidence keeps correction A's identity ...
    assert amount_items[0]["correction_public_id"] == "rcor_provA_1"
    assert amount_items[0]["superseded_proposal_public_id"] == parent_public["public_id"]
    # ... while only the newly corrected field carries correction B's identity.
    assert currency_items[0]["correction_public_id"] == "rcor_provB_1"
    assert (
        currency_items[0]["superseded_proposal_public_id"]
        == first["replacement_proposal_public_id"]
    )

    # Relational evidence agrees exactly with the payload evidence.
    rows = conn.execute(
        "SELECT field_name, proposed_value, evidence_source_type, evidence_reference "
        "FROM parser_proposal_field_evidence WHERE parser_output_id = ?",
        (grandchild_id,),
    ).fetchall()
    by_field = {row["field_name"]: row for row in rows}
    amount_reference = json.loads(by_field["amount"]["evidence_reference"])
    assert by_field["amount"]["evidence_source_type"] == "user_message"
    assert by_field["amount"]["proposed_value"] == "40.00"
    assert amount_reference["correction_public_id"] == "rcor_provA_1"
    assert amount_reference["superseded_proposal_public_id"] == parent_public["public_id"]
    currency_reference = json.loads(by_field["currency"]["evidence_reference"])
    assert by_field["currency"]["evidence_source_type"] == "user_message"
    assert by_field["currency"]["proposed_value"] == "USD"
    assert currency_reference["correction_public_id"] == "rcor_provB_1"
    assert (
        currency_reference["superseded_proposal_public_id"]
        == first["replacement_proposal_public_id"]
    )


# ---------------------------------------------------------------------------
# Completion provenance reconstruction
# ---------------------------------------------------------------------------


def test_completed_field_evidence_bound_to_completion_identity(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "compprov")
    complete_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=expected,
        field_updates={"merchant": "Don Don Donki"},
        completion_public_id="pco_prov_1",
    )
    result = _supersede(conn, pid, _hash_of(conn, pid), {"amount": "50.00"}, cid="rcor_comp_1")
    child_id = result["replacement_parser_output_id"]

    child_payload = _payload(conn, child_id)
    assert child_payload["merchant"] == "Don Don Donki"
    assert child_payload["field_confidence"]["merchant"] is None
    merchant_items = [
        item for item in child_payload["field_evidence"] if item.get("field_name") == "merchant"
    ]
    assert len(merchant_items) == 1
    item = merchant_items[0]
    assert item["evidence_source_type"] == "user_message"
    assert item["proposed_value"] == "Don Don Donki"
    assert item["completion_public_id"] == "pco_prov_1"
    assert item["completion_version"] == 1
    assert item["authenticated_actor_id"] == "owner"
    # The displaced OCR material survives only as historical source evidence.
    historical = item["historical_ocr_evidence"]
    assert len(historical) == 1
    assert historical[0]["evidence_source_type"] == "ocr"
    assert historical[0]["proposed_value"] == "COLD STORAGE"

    rows = conn.execute(
        "SELECT evidence_source_type, evidence_reference FROM parser_proposal_field_evidence "
        "WHERE parser_output_id = ? AND field_name = 'merchant'",
        (child_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["evidence_source_type"] == "user_message"
    reference = json.loads(rows[0]["evidence_reference"])
    assert reference["completion_public_id"] == "pco_prov_1"
    assert reference["completion_version"] == 1
    assert reference["authenticated_actor_id"] == "owner"


def test_multiple_completion_versions_rebuilt_field_by_field(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "compmulti")
    complete_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=expected,
        field_updates={"merchant": "First Mart", "category": "food"},
        completion_public_id="pco_multi_1",
    )
    complete_proposal(
        conn,
        pid,
        actor="owner",
        expected_content_hash=_hash_of(conn, pid),
        field_updates={"merchant": "Second Mart"},
        completion_public_id="pco_multi_2",
    )
    result = _supersede(conn, pid, _hash_of(conn, pid), {"amount": "60.00"}, cid="rcor_multi_1")
    child_payload = _payload(conn, result["replacement_parser_output_id"])
    assert child_payload["merchant"] == "Second Mart"
    assert child_payload["category"] == "food"

    by_field = {
        item["field_name"]: item
        for item in child_payload["field_evidence"]
        if isinstance(item, dict) and item.get("evidence_source_type") == "user_message"
    }
    # The latest completion version wins per field, not per record.
    assert by_field["merchant"]["completion_public_id"] == "pco_multi_2"
    assert by_field["merchant"]["completion_version"] == 2
    assert by_field["merchant"]["proposed_value"] == "Second Mart"
    assert by_field["category"]["completion_public_id"] == "pco_multi_1"
    assert by_field["category"]["completion_version"] == 1
    assert by_field["category"]["proposed_value"] == "food"


# ---------------------------------------------------------------------------
# Transaction acquisition boundary
# ---------------------------------------------------------------------------


def test_locked_database_fails_typed_without_leaked_transaction(
    migrated_temp_db_path: Path, tmp_path: Path
) -> None:
    setup = connect_temp_db(migrated_temp_db_path)
    try:
        pid, expected = _seed_receipt_proposal(setup, tmp_path, "busylock")
    finally:
        setup.close()

    blocker = connect_temp_db(migrated_temp_db_path)
    victim = connect_temp_db(migrated_temp_db_path)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        victim.execute("PRAGMA busy_timeout = 100")
        with pytest.raises(SupersessionPersistenceError) as excinfo:
            _supersede(victim, pid, expected, {"amount": "20.00"}, cid="rcor_lock_1")
        assert isinstance(excinfo.value.__cause__, sqlite3.Error)
        assert victim.in_transaction is False
        blocker.rollback()
        assert _count(victim, "receipt_proposal_revisions") == 0
        # The same command succeeds once the writer lock clears.
        result = _supersede(victim, pid, expected, {"amount": "20.00"}, cid="rcor_lock_1")
        assert result["idempotent"] is False
    finally:
        blocker.close()
        victim.close()


def test_caller_pending_transaction_rejected_without_rollback(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "callertx")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO raw_intake_records (public_id, source_type, raw_input, received_at) "
        "VALUES ('raw_caller_marker', 'telegram_text', 'marker', '2026-07-25T00:00:00Z')"
    )
    with pytest.raises(ReceiptSupersessionError, match="pending work"):
        _supersede(conn, pid, expected, {"amount": "21.00"}, cid="rcor_callertx_1")
    # The caller's transaction and its uncommitted work are untouched.
    assert conn.in_transaction is True
    marker = _row(conn, "SELECT 1 FROM raw_intake_records WHERE public_id = 'raw_caller_marker'")
    assert marker is not None
    conn.rollback()
    assert _count(conn, "receipt_proposal_revisions") == 0
    marker_after = _row(
        conn, "SELECT 1 FROM raw_intake_records WHERE public_id = 'raw_caller_marker'"
    )
    assert marker_after is None


# ---------------------------------------------------------------------------
# Concurrency: distinct corrections racing for one proposal
# ---------------------------------------------------------------------------


def test_concurrent_distinct_corrections_exactly_one_wins(
    migrated_temp_db_path: Path,
) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as work_dir:
        setup = connect_temp_db(migrated_temp_db_path)
        try:
            pid, expected = _seed_receipt_proposal(setup, Path(work_dir), "race2")
        finally:
            setup.close()

        results: list[dict[str, Any]] = []
        typed_failures: list[BaseException] = []
        unexpected: list[BaseException] = []
        barrier = threading.Barrier(2)

        def worker(cid: str, amount: str) -> None:
            conn = connect_temp_db(migrated_temp_db_path)
            conn.execute("PRAGMA busy_timeout = 5000")
            try:
                barrier.wait(timeout=5)
                results.append(_supersede(conn, pid, expected, {"amount": amount}, cid=cid))
            except (
                StaleSupersessionTargetError,
                StaleSupersessionContentError,
                InvalidSupersessionStatusError,
                SupersessionConflictError,
                RawIntakeBindingError,
            ) as exc:
                typed_failures.append(exc)
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                unexpected.append(exc)
            finally:
                assert conn.in_transaction is False
                conn.close()

        threads = [
            threading.Thread(target=worker, args=("rcor_race2_a", "31.00")),
            threading.Thread(target=worker, args=("rcor_race2_b", "32.00")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert unexpected == []
        assert len(results) == 1
        assert len(typed_failures) == 1

        check = connect_temp_db(migrated_temp_db_path)
        try:
            assert _count(check, "receipt_proposal_revisions") == 1
        finally:
            check.close()


# ---------------------------------------------------------------------------
# Raw-intake binding cardinality fails closed
# ---------------------------------------------------------------------------


def test_ambiguous_raw_intake_binding_fails_closed(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "dupbind")
    conn.execute(
        "INSERT INTO raw_intake_records "
        "(public_id, source_type, raw_input, received_at, status, parser_output_id) "
        "VALUES ('raw_dup_bind', 'telegram_image', 'dup', '2026-07-25T00:00:00Z', "
        "'parsed_pending_confirmation', ?)",
        (pid,),
    )
    conn.commit()
    before_outputs = _count(conn, "parser_outputs")
    with pytest.raises(RawIntakeBindingError):
        _supersede(conn, pid, expected, {"amount": "25.00"}, cid="rcor_dupbind_1")
    assert conn.in_transaction is False
    assert _count(conn, "receipt_proposal_revisions") == 0
    assert _count(conn, "parser_outputs") == before_outputs
    parent = _row(conn, "SELECT parse_status FROM parser_outputs WHERE id = ?", pid)
    assert parent is not None and parent["parse_status"] == _PENDING
    pointers = conn.execute(
        "SELECT parser_output_id FROM raw_intake_records WHERE parser_output_id = ?",
        (pid,),
    ).fetchall()
    assert len(pointers) == 2


# ---------------------------------------------------------------------------
# Deterministic replay results
# ---------------------------------------------------------------------------


def test_replay_after_child_confirmation_returns_original_result(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "detcfm")
    first = _supersede(conn, pid, expected, {"amount": "70.00"}, cid="rcor_detcfm_1")
    confirm_proposal(
        conn,
        first["replacement_parser_output_id"],
        actor="owner",
        confirmation_public_id="pca_detcfm_1",
    )
    replay = _supersede(conn, pid, expected, {"amount": "70.00"}, cid="rcor_detcfm_1")
    # The replay reports the original creation-time result, not the
    # replacement's later confirmed lifecycle state.
    assert replay == {**first, "idempotent": True}
    child = _row(
        conn,
        "SELECT parse_status FROM parser_outputs WHERE id = ?",
        first["replacement_parser_output_id"],
    )
    assert child is not None and child["parse_status"] == "confirmed"


def test_replay_after_chained_supersession_returns_original_result(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "detchain")
    first = _supersede(conn, pid, expected, {"amount": "71.00"}, cid="rcor_detchain_1")
    child_id = first["replacement_parser_output_id"]
    second = _supersede(
        conn,
        child_id,
        first["replacement_content_hash"],
        {"amount": "72.00"},
        cid="rcor_detchain_2",
    )
    replay_first = _supersede(conn, pid, expected, {"amount": "71.00"}, cid="rcor_detchain_1")
    assert replay_first == {**first, "idempotent": True}
    replay_second = _supersede(
        conn,
        child_id,
        first["replacement_content_hash"],
        {"amount": "72.00"},
        cid="rcor_detchain_2",
    )
    assert replay_second == {**second, "idempotent": True}
    child = _row(conn, "SELECT parse_status FROM parser_outputs WHERE id = ?", child_id)
    assert child is not None and child["parse_status"] == "superseded"


def test_replay_result_matches_initial_result_except_idempotent_flag(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "detsame")
    first = _supersede(
        conn, pid, expected, {"amount": "73.00", "currency": "sgd"}, cid="rcor_detsame_1"
    )
    replay = _supersede(
        conn,
        pid,
        expected,
        {"amount": "73.00", "currency": "sgd"},
        cid="rcor_detsame_1",
        reason="retry",
    )
    assert replay == {**first, "idempotent": True}


# ---------------------------------------------------------------------------
# Cross-currency minor-unit contract
# ---------------------------------------------------------------------------


def test_jpy_to_usd_currency_only_normalizes_amount_without_reattribution(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_cross_currency_proposal(
        conn, tmp_path, "jpy2usd", total_marker="JPY", total_value="12"
    )
    parent_payload = _payload(conn, pid)
    assert parent_payload["amount"] == "12"
    assert parent_payload["currency"] == "JPY"
    parent_amount_confidence = parent_payload["field_confidence"]["amount"]
    assert parent_amount_confidence is not None

    result = _supersede(conn, pid, expected, {"currency": "USD"}, cid="rcor_jpy2usd_1")
    assert result["changed_fields"] == ["currency"]

    revision = _row(
        conn,
        "SELECT field_updates_json, applied_field_updates_json "
        "FROM receipt_proposal_revisions WHERE correction_public_id = 'rcor_jpy2usd_1'",
    )
    assert revision is not None
    assert json.loads(revision["field_updates_json"]) == {"currency": "USD"}
    assert json.loads(revision["applied_field_updates_json"]) == {"currency": "USD"}

    child_id = result["replacement_parser_output_id"]
    child_payload = _payload(conn, child_id)
    # The amount moves to USD's canonical minor-unit scale as a system
    # normalization, not as a human amount correction.
    assert child_payload["amount"] == "12.00"
    assert child_payload["currency"] == "USD"
    assert child_payload["field_confidence"]["amount"] == parent_amount_confidence
    assert child_payload["field_confidence"]["currency"] is None

    amount_items = [
        item for item in child_payload["field_evidence"] if item.get("field_name") == "amount"
    ]
    assert len(amount_items) == 1
    assert amount_items[0]["evidence_source_type"] == "ocr"
    assert amount_items[0]["proposed_value"] == "12.00"
    assert amount_items[0]["confidence"] == parent_amount_confidence
    assert "correction_public_id" not in amount_items[0]

    row = _row(
        conn,
        "SELECT proposed_value, evidence_source_type, evidence_reference "
        "FROM parser_proposal_field_evidence "
        "WHERE parser_output_id = ? AND field_name = 'amount'",
        child_id,
    )
    assert row is not None
    assert row["proposed_value"] == "12.00"
    assert row["evidence_source_type"] == "ocr"
    assert "rcor_jpy2usd_1" not in row["evidence_reference"]


def test_jpy_to_usd_with_echoed_amount_keeps_replay_identity_only(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_cross_currency_proposal(
        conn, tmp_path, "jpy2usde", total_marker="JPY", total_value="12"
    )
    result = _supersede(
        conn, pid, expected, {"amount": "12", "currency": "USD"}, cid="rcor_jpy2usde_1"
    )
    assert result["changed_fields"] == ["currency"]

    revision = _row(
        conn,
        "SELECT field_updates_json, applied_field_updates_json "
        "FROM receipt_proposal_revisions WHERE correction_public_id = 'rcor_jpy2usde_1'",
    )
    assert revision is not None
    # The echoed amount joins the canonical replay identity at the target
    # scale, but the numerically unchanged amount is never an applied change.
    assert json.loads(revision["field_updates_json"]) == {"amount": "12.00", "currency": "USD"}
    assert json.loads(revision["applied_field_updates_json"]) == {"currency": "USD"}

    child_payload = _payload(conn, result["replacement_parser_output_id"])
    assert child_payload["amount"] == "12.00"
    assert child_payload["currency"] == "USD"
    amount_items = [
        item for item in child_payload["field_evidence"] if item.get("field_name") == "amount"
    ]
    assert len(amount_items) == 1
    assert amount_items[0]["evidence_source_type"] == "ocr"
    assert "correction_public_id" not in amount_items[0]


def test_usd_to_jpy_currency_only_normalizes_amount_scale(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_cross_currency_proposal(
        conn, tmp_path, "usd2jpy", total_marker="US$", total_value="12.00"
    )
    parent_payload = _payload(conn, pid)
    assert parent_payload["amount"] == "12.00"
    assert parent_payload["currency"] == "USD"
    parent_amount_confidence = parent_payload["field_confidence"]["amount"]
    assert parent_amount_confidence is not None

    result = _supersede(conn, pid, expected, {"currency": "JPY"}, cid="rcor_usd2jpy_1")
    assert result["changed_fields"] == ["currency"]

    revision = _row(
        conn,
        "SELECT field_updates_json, applied_field_updates_json "
        "FROM receipt_proposal_revisions WHERE correction_public_id = 'rcor_usd2jpy_1'",
    )
    assert revision is not None
    assert json.loads(revision["field_updates_json"]) == {"currency": "JPY"}
    assert json.loads(revision["applied_field_updates_json"]) == {"currency": "JPY"}

    child_id = result["replacement_parser_output_id"]
    child_payload = _payload(conn, child_id)
    assert child_payload["amount"] == "12"
    assert child_payload["currency"] == "JPY"
    assert child_payload["field_confidence"]["amount"] == parent_amount_confidence

    amount_items = [
        item for item in child_payload["field_evidence"] if item.get("field_name") == "amount"
    ]
    assert len(amount_items) == 1
    assert amount_items[0]["evidence_source_type"] == "ocr"
    assert amount_items[0]["proposed_value"] == "12"
    assert "correction_public_id" not in amount_items[0]

    row = _row(
        conn,
        "SELECT proposed_value, evidence_source_type FROM parser_proposal_field_evidence "
        "WHERE parser_output_id = ? AND field_name = 'amount'",
        child_id,
    )
    assert row is not None
    assert row["proposed_value"] == "12"
    assert row["evidence_source_type"] == "ocr"


def test_usd_to_jpy_with_echoed_amount_keeps_replay_identity_only(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_cross_currency_proposal(
        conn, tmp_path, "usd2jpye", total_marker="US$", total_value="12.00"
    )
    result = _supersede(
        conn, pid, expected, {"amount": "12.00", "currency": "JPY"}, cid="rcor_usd2jpye_1"
    )
    assert result["changed_fields"] == ["currency"]

    revision = _row(
        conn,
        "SELECT field_updates_json, applied_field_updates_json "
        "FROM receipt_proposal_revisions WHERE correction_public_id = 'rcor_usd2jpye_1'",
    )
    assert revision is not None
    assert json.loads(revision["field_updates_json"]) == {"amount": "12", "currency": "JPY"}
    assert json.loads(revision["applied_field_updates_json"]) == {"currency": "JPY"}

    child_payload = _payload(conn, result["replacement_parser_output_id"])
    assert child_payload["amount"] == "12"
    amount_items = [
        item for item in child_payload["field_evidence"] if item.get("field_name") == "amount"
    ]
    assert len(amount_items) == 1
    assert amount_items[0]["evidence_source_type"] == "ocr"
    assert "correction_public_id" not in amount_items[0]


def test_cross_scale_amount_and_currency_material_change(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_cross_currency_proposal(
        conn, tmp_path, "xmat", total_marker="JPY", total_value="12"
    )
    result = _supersede(
        conn, pid, expected, {"amount": "15.50", "currency": "USD"}, cid="rcor_xmat_1"
    )
    assert result["changed_fields"] == ["amount", "currency"]

    revision = _row(
        conn,
        "SELECT field_updates_json, applied_field_updates_json "
        "FROM receipt_proposal_revisions WHERE correction_public_id = 'rcor_xmat_1'",
    )
    assert revision is not None
    assert json.loads(revision["field_updates_json"]) == {"amount": "15.50", "currency": "USD"}
    assert json.loads(revision["applied_field_updates_json"]) == {
        "amount": "15.50",
        "currency": "USD",
    }

    child_id = result["replacement_parser_output_id"]
    child_payload = _payload(conn, child_id)
    assert child_payload["amount"] == "15.50"
    assert child_payload["currency"] == "USD"
    assert child_payload["field_confidence"]["amount"] is None
    assert child_payload["field_confidence"]["currency"] is None

    for field in ("amount", "currency"):
        items = [
            item for item in child_payload["field_evidence"] if item.get("field_name") == field
        ]
        assert len(items) == 1
        assert items[0]["evidence_source_type"] == "user_message"
        assert items[0]["correction_public_id"] == "rcor_xmat_1"

    rows = conn.execute(
        "SELECT field_name, proposed_value, evidence_source_type, evidence_reference "
        "FROM parser_proposal_field_evidence WHERE parser_output_id = ?",
        (child_id,),
    ).fetchall()
    by_field = {row["field_name"]: row for row in rows}
    assert by_field["amount"]["evidence_source_type"] == "user_message"
    assert by_field["amount"]["proposed_value"] == "15.50"
    assert json.loads(by_field["amount"]["evidence_reference"])["correction_public_id"] == (
        "rcor_xmat_1"
    )
    assert by_field["currency"]["evidence_source_type"] == "user_message"
    assert by_field["currency"]["proposed_value"] == "USD"


def test_sgd_to_jpy_currency_only_rejected_by_money_contract_with_zero_writes(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_receipt_proposal(conn, tmp_path, "sgd2jpy")
    before_outputs = _count(conn, "parser_outputs")
    before_links = _count(conn, "receipt_ocr_proposal_links")
    before_events = _count(conn, "parser_proposal_events")
    before_audit = _count(conn, "financial_audit_events")
    before_evidence = _count(conn, "parser_proposal_field_evidence")

    # SGD 12.34 cannot be represented at JPY's zero-decimal minor-unit scale;
    # the Money Contract rejects instead of silently rounding.
    with pytest.raises(InvalidSupersessionFieldValueError):
        _supersede(conn, pid, expected, {"currency": "JPY"}, cid="rcor_sgd2jpy_1")

    assert conn.in_transaction is False
    assert _count(conn, "receipt_proposal_revisions") == 0
    assert _count(conn, "parser_outputs") == before_outputs
    assert _count(conn, "receipt_ocr_proposal_links") == before_links
    assert _count(conn, "parser_proposal_events") == before_events
    assert _count(conn, "financial_audit_events") == before_audit
    assert _count(conn, "parser_proposal_field_evidence") == before_evidence
    parent = _row(conn, "SELECT parse_status FROM parser_outputs WHERE id = ?", pid)
    assert parent is not None and parent["parse_status"] == _PENDING
    assert _hash_of(conn, pid) == expected
    pointer = _row(
        conn, "SELECT parser_output_id FROM raw_intake_records WHERE parser_output_id = ?", pid
    )
    assert pointer is not None


def test_cross_scale_replay_identity_and_conflicts(
    migrated_temp_db_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = migrated_temp_db_connection
    pid, expected = _seed_cross_currency_proposal(
        conn, tmp_path, "xreplay", total_marker="JPY", total_value="12"
    )
    first = _supersede(
        conn, pid, expected, {"amount": "12", "currency": "USD"}, cid="rcor_xreplay_1"
    )
    assert first["changed_fields"] == ["currency"]

    replay = _supersede(
        conn, pid, expected, {"amount": "12", "currency": "USD"}, cid="rcor_xreplay_1"
    )
    assert replay == {**first, "idempotent": True}

    # A materially different caller amount under the same correction ID is a
    # typed conflict, never a silent overwrite.
    with pytest.raises(SupersessionConflictError):
        _supersede(conn, pid, expected, {"amount": "13", "currency": "USD"}, cid="rcor_xreplay_1")
    # Dropping the echoed amount changes the canonical command identity.
    with pytest.raises(SupersessionConflictError):
        _supersede(conn, pid, expected, {"currency": "USD"}, cid="rcor_xreplay_1")

    confirm_proposal(
        conn,
        first["replacement_parser_output_id"],
        actor="owner",
        confirmation_public_id="pca_xreplay_1",
    )
    replay_after = _supersede(
        conn, pid, expected, {"amount": "12", "currency": "USD"}, cid="rcor_xreplay_1"
    )
    assert replay_after == {**first, "idempotent": True}
    assert _count(conn, "receipt_proposal_revisions") == 1
