"""A 055 upgrade preserves established text work without reopening old ingress."""

from __future__ import annotations

import sqlite3

import pytest

from finance_core.intake.raw_text_repository import (
    create_raw_intake_record,
    save_parser_proposal,
)
from finance_core.intake.raw_text_service import process_raw_text_input
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS, apply_migration_paths
from tests.test_migration_042_s5e_ai_fallback_provenance_foundation_v1 import insert_attempt


def _unrouted_parser_insert(
    conn: sqlite3.Connection,
    source_public_id: str,
    name: str,
    *,
    source_type: str = "telegram_text",
) -> None:
    conn.execute(
        "INSERT INTO parser_outputs "
        "(public_id, source_type, source_public_id, parse_status) "
        "VALUES (?, ?, ?, 'parsed_pending_confirmation')",
        (name, source_type, source_public_id),
    )


def test_055_cutover_preserves_admitted_history_but_blocks_new_unrouted_text(
    temp_db_connection: sqlite3.Connection,
) -> None:
    conn = temp_db_connection
    assert TEMP_DB_MIGRATION_PATHS[-1].name == "055_d3_interaction_routes.sql"
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS[:-1])

    old = process_raw_text_input(conn, "Coffee SGD 6.40 at Cafe")
    old_intake = old["intake"]
    old_proposal = old["parser_output"]
    unparsed = create_raw_intake_record(
        conn,
        "old unparsed message",
        source_type="telegram_text",
        source_channel="telegram",
    )
    conn.commit()

    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    admissions = conn.execute(
        "SELECT raw_intake_record_id, source_public_id, admitted_parser_output_id "
        "FROM finance_legacy_text_lineage_admissions"
    ).fetchall()
    assert len(admissions) == 1
    assert tuple(admissions[0]) == (
        old_intake["id"],
        old_intake["public_id"],
        old_proposal["id"],
    )

    child = save_parser_proposal(conn, old_intake["id"], old_proposal["proposal"])
    assert child["id"] != old_proposal["id"]
    assert (
        conn.execute(
            "SELECT parent_parser_output_id FROM parser_outputs WHERE id = ?", (child["id"],)
        ).fetchone()[0]
        == old_proposal["id"]
    )
    insert_attempt(conn, child["id"], old_intake["id"], "a")

    fresh = create_raw_intake_record(
        conn,
        "new unrouted message",
        source_type="telegram_text",
        source_channel="telegram",
    )
    for index, source in enumerate((unparsed, fresh)):
        with pytest.raises(sqlite3.IntegrityError, match="route before parser"):
            _unrouted_parser_insert(conn, source["public_id"], f"forbidden_{index}")
        with pytest.raises(sqlite3.IntegrityError, match="route before parser"):
            _unrouted_parser_insert(
                conn,
                source["public_id"],
                f"disguised_{index}",
                source_type="telegram_image",
            )

    with pytest.raises(sqlite3.IntegrityError, match="route before parser"):
        _unrouted_parser_insert(conn, old_intake["public_id"], "unrelated_old_parent")
    with pytest.raises(sqlite3.IntegrityError, match="migration-only"):
        conn.execute(
            "INSERT INTO finance_legacy_text_lineage_admissions "
            "(raw_intake_record_id, source_public_id, admitted_parser_output_id) "
            "VALUES (?, ?, ?)",
            (fresh["id"], fresh["public_id"], child["id"]),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE finance_legacy_text_lineage_admissions "
            "SET admitted_parser_output_id = ? WHERE raw_intake_record_id = ?",
            (child["id"], old_intake["id"]),
        )
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        conn.execute("DELETE FROM finance_legacy_text_lineage_admissions")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
