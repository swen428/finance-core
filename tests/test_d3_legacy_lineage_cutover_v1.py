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
from tests.test_migration_042_s5e_ai_fallback_provenance_foundation_v1 import (
    HASH,
    insert_attempt,
    insert_claim,
    insert_result,
)

PRE_055_MIGRATION_PATHS = TEMP_DB_MIGRATION_PATHS[
    : next(
        index
        for index, path in enumerate(TEMP_DB_MIGRATION_PATHS)
        if path.name == "055_d3_interaction_routes.sql"
    )
]


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
    apply_migration_paths(conn, PRE_055_MIGRATION_PATHS)

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
    conn.execute("PRAGMA recursive_triggers = OFF")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        conn.execute(
            "INSERT OR REPLACE INTO parser_outputs "
            "(id, public_id, source_type, source_public_id, parse_status) "
            "VALUES (?, 'legacy_replacement', 'manual_entry', 'other', "
            "'parsed_pending_confirmation')",
            (old_proposal["id"],),
        )
    assert (
        conn.execute(
            "SELECT source_public_id FROM parser_outputs WHERE id = ?", (old_proposal["id"],)
        ).fetchone()[0]
        == old_intake["public_id"]
    )

    child = save_parser_proposal(conn, old_intake["id"], old_proposal["proposal"])
    assert child["id"] != old_proposal["id"]
    assert (
        conn.execute(
            "SELECT parent_parser_output_id FROM parser_outputs WHERE id = ?", (child["id"],)
        ).fetchone()[0]
        == old_proposal["id"]
    )
    assert (
        conn.execute(
            "SELECT parser_output_id FROM finance_parser_identity_seals WHERE parser_output_id = ?",
            (child["id"],),
        ).fetchone()[0]
        == child["id"]
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


def test_055_guards_every_new_telegram_text_binding_direction(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    conn.execute(
        "INSERT INTO raw_intake_records "
        "(public_id, source_type, source_channel, raw_input, received_at) "
        "VALUES ('unrouted_flip', 'telegram_text', 'telegram', 'x', '2026-01-01')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="source identity is immutable"):
        conn.execute(
            "UPDATE raw_intake_records SET source_channel = 'manual' "
            "WHERE public_id = 'unrouted_flip'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="source identity is immutable"):
        conn.execute(
            "UPDATE raw_intake_records SET source_type = 'manual_entry' "
            "WHERE public_id = 'unrouted_flip'"
        )

    conn.execute(
        "INSERT INTO parser_outputs "
        "(public_id, source_type, source_public_id, parse_status) "
        "VALUES ('later_bound', 'telegram_text', NULL, 'parsed_pending_confirmation')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="parser source identity is immutable"):
        conn.execute(
            "UPDATE parser_outputs SET source_public_id = 'unrouted_flip' "
            "WHERE public_id = 'later_bound'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="route before parser binding"):
        conn.execute(
            "UPDATE raw_intake_records SET parser_output_id = "
            "(SELECT id FROM parser_outputs WHERE public_id = 'later_bound') "
            "WHERE public_id = 'unrouted_flip'"
        )

    conn.execute(
        "INSERT INTO parser_outputs "
        "(public_id, source_type, source_public_id, parse_status) "
        "VALUES ('future_proposal', 'telegram_text', 'future_raw', "
        "'parsed_pending_confirmation')"
    )
    for pointer in ("NULL", "(SELECT id FROM parser_outputs WHERE public_id = 'future_proposal')"):
        with pytest.raises(sqlite3.IntegrityError, match="prebound parser"):
            conn.execute(
                "INSERT INTO raw_intake_records "
                "(public_id, source_type, source_channel, raw_input, received_at, "
                "parser_output_id) VALUES "
                f"('future_raw', 'telegram_text', 'telegram', 'x', '2026-01-01', {pointer})"
            )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("recursive_triggers", ["OFF", "ON"])
def test_055_seals_negative_parser_ids_and_allows_automatic_child(
    temp_db_connection: sqlite3.Connection, recursive_triggers: str
) -> None:
    conn = temp_db_connection
    apply_migration_paths(conn, PRE_055_MIGRATION_PATHS)
    for parser_id, suffix, bound in ((-1, "bound", True), (-2, "source_only", False)):
        conn.execute(
            "INSERT INTO raw_intake_records "
            "(public_id, source_type, source_channel, raw_input, received_at, "
            "source_received_at, status) VALUES (?, 'telegram_text', 'telegram', "
            "'original source', '2026-08-17T00:00:00+00:00', "
            "'2026-08-17T00:00:00+00:00', 'parsed_pending_confirmation')",
            (f"negative_raw_{suffix}",),
        )
        conn.execute(
            "INSERT INTO parser_outputs "
            "(id, public_id, source_type, source_public_id, raw_text, "
            "parsed_payload, normalized_payload, parse_status) "
            "VALUES (?, ?, 'telegram_text', ?, 'original source', '{}', '{}', "
            "'parsed_pending_confirmation')",
            (parser_id, f"negative_parser_{suffix}", f"negative_raw_{suffix}"),
        )
        if bound:
            conn.execute(
                "UPDATE raw_intake_records SET parser_output_id = ? WHERE public_id = ?",
                (parser_id, f"negative_raw_{suffix}"),
            )
    raw_id = conn.execute(
        "SELECT id FROM raw_intake_records WHERE public_id = 'negative_raw_bound'"
    ).fetchone()[0]
    insert_attempt(conn, -1, raw_id, "b")
    conn.commit()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    conn.execute(f"PRAGMA recursive_triggers = {recursive_triggers}")

    assert {
        row[0] for row in conn.execute("SELECT parser_output_id FROM finance_parser_identity_seals")
    }.issuperset({-1, -2})
    for parser_id, suffix in ((-1, "bound"), (-2, "source_only")):
        for collision_key, collision_value in (
            ("id", parser_id),
            ("rowid", parser_id),
            ("public_id", f"negative_parser_{suffix}"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT OR REPLACE INTO parser_outputs "
                    f"({collision_key}, public_id, source_type, source_public_id, "
                    "parse_status) VALUES (?, ?, 'manual_entry', 'unrelated', "
                    "'parsed_pending_confirmation')",
                    (collision_value, f"replacement_{suffix}_{collision_key}"),
                )
            assert (
                conn.execute(
                    "SELECT id FROM parser_outputs WHERE public_id = ?",
                    (f"negative_parser_{suffix}",),
                ).fetchone()[0]
                == parser_id
            )
    for statement in (
        "DELETE FROM finance_parser_identity_seals WHERE parser_output_id = -1",
        "UPDATE finance_parser_identity_seals SET parser_output_id = 99 "
        "WHERE parser_output_id = -1",
        "INSERT OR REPLACE INTO finance_parser_identity_seals (parser_output_id) VALUES (-1)",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement)

    conn.execute(
        "INSERT INTO parser_outputs "
        "(public_id, source_type, source_public_id, parent_parser_output_id, "
        "parse_status) VALUES ('negative_auto_child', 'telegram_text', "
        "'negative_raw_bound', -1, 'parsed_pending_confirmation')"
    )
    child_id = conn.execute(
        "SELECT id FROM parser_outputs WHERE public_id = 'negative_auto_child'"
    ).fetchone()[0]
    assert child_id not in (-1, -2)
    assert (
        conn.execute(
            "SELECT parser_output_id FROM finance_parser_identity_seals WHERE parser_output_id = ?",
            (child_id,),
        ).fetchone()[0]
        == child_id
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_055_legacy_ai_child_can_advance_pointer_without_losing_seal(
    temp_db_connection: sqlite3.Connection,
) -> None:
    conn = temp_db_connection
    apply_migration_paths(conn, PRE_055_MIGRATION_PATHS)
    old = process_raw_text_input(conn, "Coffee SGD 6.40 at Cafe")
    parent_id = old["parser_output"]["id"]
    raw_id = old["intake"]["id"]
    conn.commit()
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)

    attempt_id = insert_attempt(conn, parent_id, raw_id, "c")
    claim_id = insert_claim(conn, attempt_id, "d")
    result_id = insert_result(
        conn,
        attempt_id,
        claim_id,
        "e",
        transport_outcome="response_received",
        result_status="proposal_created",
        recovery_disposition=None,
    )
    child_id = conn.execute(
        "INSERT INTO parser_outputs "
        "(public_id, source_type, source_public_id, parser_name, "
        "parser_version, raw_text, parsed_payload, normalized_payload, "
        "parse_status, parent_parser_output_id) "
        "VALUES ('legacy_ai_child_055', 'telegram_text', 'ai_child_source_055', "
        "'ai-fallback', 'v1', 'child', '{}', '{}', "
        "'parsed_pending_confirmation', ?)",
        (parent_id,),
    ).lastrowid
    assert (
        conn.execute(
            "SELECT 1 FROM finance_parser_identity_seals WHERE parser_output_id = ?",
            (child_id,),
        ).fetchone()
        is None
    )
    conn.execute(
        "INSERT INTO ai_fallback_proposal_links "
        "(link_public_id, link_material_hash, result_id, parser_output_id, "
        "proposal_version, effective_content_hash) VALUES (?, ?, ?, ?, 0, ?)",
        ("aipl_" + "f" * 64, "a" * 64, result_id, child_id, HASH),
    )
    assert (
        conn.execute(
            "SELECT parser_output_id FROM finance_parser_identity_seals WHERE parser_output_id = ?",
            (child_id,),
        ).fetchone()[0]
        == child_id
    )
    conn.execute(
        "UPDATE raw_intake_records SET parser_output_id = ? WHERE id = ?",
        (child_id, raw_id),
    )
    assert (
        conn.execute(
            "SELECT parser_output_id FROM raw_intake_records WHERE id = ?", (raw_id,)
        ).fetchone()[0]
        == child_id
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
