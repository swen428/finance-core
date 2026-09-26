"""S5e-B migration 043 upgrade, replay, and sealing tests."""

from __future__ import annotations

import sqlite3

import pytest

from finance_core.reconciliation.migrations import (
    TEMP_DB_MIGRATION_PATHS,
    apply_migration_paths,
    migration_ledger_rows,
    verify_migration_history,
)
from tests.test_migration_042_s5e_ai_fallback_provenance_foundation_v1 import (
    insert_attempt,
    insert_claim,
    insert_result,
    seed_parent,
)

PATHS_THROUGH_042 = TEMP_DB_MIGRATION_PATHS[:42]
PATHS_BEFORE_D3_ROUTE = TEMP_DB_MIGRATION_PATHS[:-1]
HASH = "a" * 64


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_fresh_manifest_apply_and_replay_preserves_043_through_046() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)

        assert (
            conn.execute("SELECT COUNT(*) FROM ai_model_compatibility_receipts").fetchone()[0] == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ai_fallback_attempt_compatibility_receipts"
            ).fetchone()[0]
            == 0
        )
        first_rows = migration_ledger_rows(conn)

        migration_ids = [row["migration_id"] for row in first_rows]
        migration_043_index = migration_ids.index("043")
        assert migration_ids[migration_043_index : migration_043_index + 4] == [
            "043",
            "044",
            "045",
            "046",
        ]
        assert first_rows[migration_043_index]["migration_filename"] == (
            "043_s5e_ai_fallback_lineage_sealing.sql"
        )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
        assert migration_ledger_rows(conn) == first_rows
        verify_migration_history(conn, TEMP_DB_MIGRATION_PATHS)
    finally:
        conn.close()


def test_upgrade_from_042_preserves_legacy_rows_and_adds_043_contract() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, PATHS_THROUGH_042)
        parent_id, intake_id = seed_parent(conn, "upgrade_043")
        attempt_id = insert_attempt(conn, parent_id, intake_id, "a")
        claim_id = insert_claim(conn, attempt_id, "b")
        child_id = int(
            conn.execute(
                """
                INSERT INTO parser_outputs (
                    public_id, source_type, source_public_id, parser_name,
                    parser_version, raw_text, parsed_payload, normalized_payload,
                    parse_status, parent_parser_output_id
                ) VALUES (?, 'telegram_text', ?, 'ai-fallback', 'v1', ?, '{}', '{}',
                          'parsed_pending_confirmation', ?)
                """,
                ("prop_s5e_upgrade_child_043", "raw_s5e_upgrade_child_043", "child", parent_id),
            ).lastrowid
        )
        result_id = insert_result(
            conn,
            attempt_id,
            claim_id,
            "c",
            transport_outcome="response_received",
            result_status="proposal_created",
            recovery_disposition=None,
        )
        conn.execute(
            """
            INSERT INTO ai_fallback_proposal_links (
                link_public_id, link_material_hash, result_id, parser_output_id,
                effective_content_hash
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("aipl_" + "d" * 64, "e" * 64, result_id, child_id, HASH),
        )
        conn.commit()

        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)

        assert (
            conn.execute("SELECT COUNT(*) FROM ai_model_compatibility_receipts").fetchone()[0] == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ai_fallback_attempt_compatibility_receipts"
            ).fetchone()[0]
            == 0
        )
        result_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(ai_fallback_results)")
        }
        link_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(ai_fallback_proposal_links)")
        }
        assert "result_arguments_hash" in result_columns
        assert "proposal_version" in link_columns
        assert (
            conn.execute(
                "SELECT result_arguments_hash FROM ai_fallback_results WHERE id = ?",
                (result_id,),
            ).fetchone()[0]
            is None
        )
        assert (
            conn.execute(
                "SELECT proposal_version FROM ai_fallback_proposal_links WHERE result_id = ?",
                (result_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND name = 'trg_ai_fallback_evidence_no_update'"
            ).fetchone()
            is not None
        )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        verify_migration_history(conn, TEMP_DB_MIGRATION_PATHS)
    finally:
        conn.close()


def test_ai_child_evidence_remains_append_only_after_043() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, PATHS_BEFORE_D3_ROUTE)
        parent_id, intake_id = seed_parent(conn, "sealing_043")
        conn.commit()
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
        attempt_id = insert_attempt(conn, parent_id, intake_id, "1")
        claim_id = insert_claim(conn, attempt_id, "2")
        result_id = insert_result(
            conn,
            attempt_id,
            claim_id,
            "3",
            transport_outcome="response_received",
            result_status="proposal_created",
            recovery_disposition=None,
        )
        child_id = int(
            conn.execute(
                """
                INSERT INTO parser_outputs (
                    public_id, source_type, source_public_id, parser_name,
                    parser_version, raw_text, parsed_payload, normalized_payload,
                    parse_status, parent_parser_output_id
                ) VALUES (?, 'telegram_text', ?, 'ai-fallback', 'v1', ?, '{}', '{}',
                          'parsed_pending_confirmation', ?)
                """,
                ("prop_s5e_child_043", "raw_s5e_child_043", "child", parent_id),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO attachments (
                public_id, attachment_type, file_path, mime_type, file_hash
            ) VALUES ('att_s5e_043', 'receipt_image', '/tmp/s5e-043.jpg',
                      'image/jpeg', ?)
            """,
            (HASH,),
        )
        extraction_id = int(
            conn.execute(
                """
                INSERT INTO receipt_ocr_extractions (
                    public_id, attachment_id, source_attachment_hash,
                    source_attachment_size, source_mime_type, engine_name,
                    engine_version, engine_binary_sha256, engine_configuration_hash,
                    extraction_fingerprint, extraction_status, block_count,
                    total_normalized_text_length, normalized_result_hash,
                    sanitized_outcome_code
                ) VALUES (
                    'rocr_s5e_043', 1, ?, 1, 'image/jpeg', 'synthetic',
                    'v1', ?, ?, ?, 'succeeded', 1, 1, ?, 'ok'
                )
                """,
                (HASH, HASH, HASH, HASH, HASH),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO receipt_ocr_proposal_links (
                public_id, extraction_id, parser_output_id, proposal_input_hash,
                proposal_result_hash, parser_contract_version, link_role
            ) VALUES (?, ?, ?, ?, ?, ?, 'ai_fallback')
            """,
            (
                "ropl_" + "1" * 64,
                extraction_id,
                child_id,
                HASH,
                HASH,
                "ai-fallback-v1",
            ),
        )
        conn.execute(
            """
            INSERT INTO parser_proposal_field_evidence (
                parser_output_id, field_name, proposed_value, evidence_source_type,
                evidence_reference
            ) VALUES (?, 'merchant', 'Example', 'ai_model', 'model:merchant')
            """,
            (child_id,),
        )
        evidence_id = int(
            conn.execute(
                "SELECT id FROM parser_proposal_field_evidence WHERE parser_output_id = ?",
                (child_id,),
            ).fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO ai_fallback_proposal_links (
                link_public_id, link_material_hash, result_id, parser_output_id,
                proposal_version, effective_content_hash
            ) VALUES (?, ?, ?, ?, 0, ?)
            """,
            ("aipl_" + "4" * 64, "5" * 64, result_id, child_id, HASH),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE parser_proposal_field_evidence SET proposed_value = 'Changed' "
                "WHERE parser_output_id = ?",
                (child_id,),
            )
        for column in ("ai_provider", "ai_model", "prompt_version"):
            with pytest.raises(sqlite3.IntegrityError, match="sealed"):
                conn.execute(
                    f"UPDATE parser_outputs SET {column} = 'forged' WHERE id = ?",
                    (child_id,),
                )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM parser_proposal_field_evidence WHERE parser_output_id = ?",
                (child_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="sealed"):
            conn.execute(
                """
                INSERT INTO parser_proposal_field_evidence (
                    parser_output_id, field_name, proposed_value,
                    evidence_source_type, evidence_reference
                ) VALUES (?, 'currency', 'SGD', 'ai_model', 'model:currency')
                """,
                (child_id,),
            )
        ordinary_id = int(
            conn.execute(
                """
                INSERT INTO parser_outputs (
                    public_id, source_type, parsed_payload, normalized_payload,
                    parse_status
                ) VALUES ('prop_ordinary_043', 'telegram_text', '{}', '{}',
                          'parsed_pending_confirmation')
                """
            ).lastrowid
        )
        reparent_id = int(
            conn.execute(
                """
                INSERT INTO parser_proposal_field_evidence (
                    parser_output_id, field_name, proposed_value,
                    evidence_source_type, evidence_reference
                ) VALUES (?, 'merchant', 'Other', 'raw_input', 'ordinary:merchant')
                """,
                (ordinary_id,),
            ).lastrowid
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE parser_proposal_field_evidence SET parser_output_id = ? WHERE id = ?",
                (child_id, reparent_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="sealed"):
            conn.execute(
                """
                INSERT OR REPLACE INTO parser_proposal_field_evidence (
                    id, parser_output_id, field_name, proposed_value,
                    evidence_source_type, evidence_reference
                ) VALUES (?, ?, 'merchant', 'FORGED', 'raw_input', 'forged:merchant')
                """,
                (evidence_id, ordinary_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                """
                UPDATE OR REPLACE parser_proposal_field_evidence
                SET id = ?
                WHERE id = ?
                """,
                (evidence_id, reparent_id),
            )
        preserved = conn.execute(
            """
            SELECT parser_output_id, proposed_value
            FROM parser_proposal_field_evidence
            WHERE id = ?
            """,
            (evidence_id,),
        ).fetchone()
        assert preserved["parser_output_id"] == child_id
        assert preserved["proposed_value"] == "Example"
        assert evidence_id > 0
        assert result_id > 0
    finally:
        conn.close()


def test_raw_intake_pointer_collision_is_refused_after_fallback_sealing() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, PATHS_BEFORE_D3_ROUTE)
        parent_id, intake_id = seed_parent(conn, "pointer_collision_043")
        conn.commit()
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
        attempt_id = insert_attempt(conn, parent_id, intake_id, "d")
        claim_id = insert_claim(conn, attempt_id, "e")
        result_id = insert_result(
            conn,
            attempt_id,
            claim_id,
            "f",
            transport_outcome="response_received",
            result_status="proposal_created",
            recovery_disposition=None,
        )
        child_id = int(
            conn.execute(
                """
                INSERT INTO parser_outputs (
                    public_id, source_type, source_public_id, parser_name,
                    parser_version, raw_text, parsed_payload, normalized_payload,
                    parse_status, parent_parser_output_id
                ) VALUES ('prop_pointer_child_043', 'telegram_text', 'raw_pointer',
                          'ai-fallback', 'v1', 'child', '{}', '{}',
                          'parsed_pending_confirmation', ?)
                """,
                (parent_id,),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO ai_fallback_proposal_links (
                link_public_id, link_material_hash, result_id,
                parser_output_id, proposal_version, effective_content_hash
            ) VALUES (?, ?, ?, ?, 0, ?)
            """,
            ("aipl_" + "a" * 64, "b" * 64, result_id, child_id, HASH),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="raw-intake binding"):
            conn.execute(
                """
                INSERT INTO raw_intake_records (
                    public_id, source_type, source_channel, raw_input,
                    received_at, status, parser_output_id, idempotency_key
                ) VALUES (
                    'raw_pointer_duplicate_043', 'telegram_text', 'telegram',
                    'duplicate', '2026-01-01T00:00:00Z',
                    'parsed_pending_confirmation', ?, 'pointer-duplicate'
                )
                """,
                (parent_id,),
            )
        conn.execute(
            """
            INSERT INTO raw_intake_records (
                public_id, source_type, source_channel, raw_input,
                received_at, status, parser_output_id, idempotency_key
            ) VALUES (
                'raw_pointer_ordinary_043', 'telegram_text', 'telegram',
                'ordinary', '2026-01-01T00:00:00Z',
                'parsed_pending_confirmation', NULL, 'pointer-ordinary'
            )
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="raw-intake binding"):
            conn.execute(
                """
                UPDATE raw_intake_records
                SET parser_output_id = ?
                WHERE id <> ?
                """,
                (child_id, intake_id),
            )
    finally:
        conn.close()


def test_raw_intake_lineage_escape_is_blocked_and_only_trigger_bypass_allows_it() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, PATHS_BEFORE_D3_ROUTE)
        parent_id, intake_id = seed_parent(conn, "lineage_escape_043")
        conn.commit()
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
        attempt_id = insert_attempt(conn, parent_id, intake_id, "7")
        claim_id = insert_claim(conn, attempt_id, "8")
        result_id = insert_result(
            conn,
            attempt_id,
            claim_id,
            "9",
            transport_outcome="response_received",
            result_status="proposal_created",
            recovery_disposition=None,
        )
        child_id = int(
            conn.execute(
                """
                INSERT INTO parser_outputs (
                    public_id, source_type, source_public_id, parser_name,
                    parser_version, raw_text, parsed_payload, normalized_payload,
                    parse_status, parent_parser_output_id
                ) VALUES ('prop_escape_child_043', 'telegram_text', 'raw_escape',
                          'ai-fallback', 'v1', 'child', '{}', '{}',
                          'parsed_pending_confirmation', ?)
                """,
                (parent_id,),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO ai_fallback_proposal_links (
                link_public_id, link_material_hash, result_id,
                parser_output_id, proposal_version, effective_content_hash
            ) VALUES (?, ?, ?, ?, 0, ?)
            """,
            ("aipl_" + "8" * 64, HASH, result_id, child_id, HASH),
        )
        conn.commit()

        # The one valid transition is parent -> the already-linked child.
        conn.execute(
            "UPDATE raw_intake_records SET parser_output_id = ? WHERE id = ?",
            (child_id, intake_id),
        )
        conn.commit()
        conn.execute("DROP TRIGGER trg_raw_intake_records_pointer_lineage_control")
        with pytest.raises(sqlite3.IntegrityError, match="lineage cannot be escaped"):
            conn.execute(
                "UPDATE raw_intake_records SET parser_output_id = ? WHERE id = ?",
                (parent_id, intake_id),
            )

        # A generic reparse can bypass SQLite triggers only when an operator
        # explicitly removes the guard; the service reader must still refuse it.
        conn.execute("DROP TRIGGER trg_ai_fallback_raw_intake_no_lineage_escape")
        conn.execute("DROP TRIGGER trg_ai_fallback_raw_intake_no_update_pointer_collision")
        conn.execute(
            "UPDATE raw_intake_records SET parser_output_id = ? WHERE id = ?",
            (parent_id, intake_id),
        )
        conn.commit()
        assert (
            conn.execute(
                "SELECT parser_output_id FROM raw_intake_records WHERE id = ?", (intake_id,)
            ).fetchone()[0]
            == parent_id
        )
    finally:
        conn.close()


def test_ocr_link_insert_or_replace_cannot_replace_an_append_only_row() -> None:
    conn = _connection()
    try:
        apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
        parent_id, _intake_id = seed_parent(conn, "insert_collision_043")
        conn.execute(
            """
            INSERT INTO attachments (
                public_id, attachment_type, file_path, mime_type, file_hash
            ) VALUES ('att_s5e_collision_043', 'receipt_image', '/tmp/collision.jpg',
                      'image/jpeg', ?)
            """,
            (HASH,),
        )
        extraction_id = int(
            conn.execute(
                """
                INSERT INTO receipt_ocr_extractions (
                    public_id, attachment_id, source_attachment_hash,
                    source_attachment_size, source_mime_type, engine_name,
                    engine_version, engine_binary_sha256, engine_configuration_hash,
                    extraction_fingerprint, extraction_status, block_count,
                    total_normalized_text_length, normalized_result_hash,
                    sanitized_outcome_code
                ) VALUES (
                    'rocr_s5e_collision_043', 1, ?, 1, 'image/jpeg', 'synthetic',
                    'v1', ?, ?, ?, 'succeeded', 1, 1, ?, 'ok'
                )
                """,
                (HASH, HASH, HASH, HASH, HASH),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO receipt_ocr_proposal_links (
                public_id, extraction_id, parser_output_id, proposal_input_hash,
                proposal_result_hash, parser_contract_version, link_role
            ) VALUES ('ropl_collision_original_043', ?, ?, ?, ?, 'initial-v1', 'initial')
            """,
            (extraction_id, parent_id, HASH, HASH),
        )
        conn.commit()
        conn.execute("PRAGMA recursive_triggers = OFF")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                """
                INSERT OR REPLACE INTO receipt_ocr_proposal_links (
                    public_id, extraction_id, parser_output_id, proposal_input_hash,
                    proposal_result_hash, parser_contract_version, link_role
                ) VALUES ('ropl_collision_replacement_043', ?, ?, ?, ?, 'initial-v1', 'initial')
                """,
                (extraction_id, parent_id, "b" * 64, "b" * 64),
            )
        row = conn.execute(
            "SELECT public_id, proposal_input_hash FROM receipt_ocr_proposal_links"
        ).fetchone()
        assert row["public_id"] == "ropl_collision_original_043"
        assert row["proposal_input_hash"] == HASH
    finally:
        conn.close()
