import json
import sqlite3
from pathlib import Path

from finance_core.intake.raw_text_repository import create_raw_intake_record
from finance_core.intake.raw_text_service import process_raw_text_input

TELEGRAM_METADATA = {
    "chat_id": "chat-123",
    "message_id": "message-456",
    "sender_id": "owner",
}


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]


def test_raw_text_record_writes_source_metadata_and_initial_evidence(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    raw_input = "Lunch SGD 12.50 at ExampleCafe paid by Owner"

    intake = create_raw_intake_record(
        conn,
        raw_input,
        received_at="2026-06-04T10:00:00+00:00",
        source_metadata=TELEGRAM_METADATA,
    )

    row = conn.execute(
        """
        SELECT source_type, source_channel, raw_input, external_source_id,
               source_message_id, idempotency_key, source_content_hash,
               parser_output_id
        FROM raw_intake_records
        WHERE id = ?
        """,
        (intake["id"],),
    ).fetchone()
    assert row["source_type"] == "telegram_text"
    assert row["source_channel"] == "telegram"
    assert row["raw_input"] == raw_input
    assert row["external_source_id"] == "telegram:chat-123:message-456"
    assert row["source_message_id"] == "message-456"
    assert row["idempotency_key"] == "raw-intake:telegram:chat-123:message-456"
    assert row["source_content_hash"].startswith("sha256:")
    assert row["parser_output_id"] is None

    evidence = conn.execute(
        """
        SELECT evidence_type, raw_intake_record_id, source_payload,
               parser_output_id, parser_name, parser_version, evidence_reference
        FROM raw_intake_evidence
        WHERE raw_intake_record_id = ?
        """,
        (intake["id"],),
    ).fetchone()
    assert evidence["evidence_type"] == "raw_input"
    assert evidence["raw_intake_record_id"] == intake["id"]
    assert json.loads(evidence["source_payload"]) == TELEGRAM_METADATA
    assert evidence["parser_output_id"] is None
    assert evidence["parser_name"] is None
    assert evidence["parser_version"] is None
    assert evidence["evidence_reference"] == intake["public_id"]
    assert count_rows(conn, "parser_outputs") == 0
    assert count_rows(conn, "transactions") == 0
    assert count_rows(conn, "shared_expense_obligations") == 0
    assert count_rows(conn, "settlement_obligations") == 0
    assert count_rows(conn, "reconciliation_records") == 0
    assert count_rows(conn, "calculation_runs") == 0


def test_process_raw_text_input_writes_source_evidence_without_final_facts(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    raw_input = "Coffee SGD 6.40 at Starbucks"

    result = process_raw_text_input(
        conn,
        raw_input,
        received_at="2026-06-04T10:00:00+00:00",
        source_metadata=TELEGRAM_METADATA,
    )

    intake = result["intake"]
    evidence = conn.execute(
        """
        SELECT evidence_type, evidence_reference, parser_output_id
        FROM raw_intake_evidence
        WHERE raw_intake_record_id = ?
        """,
        (intake["id"],),
    ).fetchone()

    assert intake["source_type"] == "telegram_text"
    assert intake["source_channel"] == "telegram"
    assert intake["raw_input"] == raw_input
    assert intake["idempotency_key"] == "raw-intake:telegram:chat-123:message-456"
    assert evidence["evidence_type"] == "raw_input"
    assert evidence["evidence_reference"] == intake["public_id"]
    assert evidence["parser_output_id"] is None
    assert result["parser_output"]["source_public_id"] == intake["public_id"]
    assert count_rows(conn, "parser_outputs") == 1
    assert count_rows(conn, "transactions") == 0
    assert count_rows(conn, "shared_expense_obligations") == 0
    assert count_rows(conn, "settlement_obligations") == 0
    assert count_rows(conn, "reconciliation_records") == 0
    assert count_rows(conn, "calculation_runs") == 0


def test_duplicate_telegram_message_returns_existing_raw_intake_record(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection

    first = process_raw_text_input(
        conn,
        "Coffee SGD 6.40 at Starbucks",
        received_at="2026-06-04T10:00:00+00:00",
        source_metadata=TELEGRAM_METADATA,
    )
    duplicate = process_raw_text_input(
        conn,
        "Coffee SGD 6.40 at Starbucks",
        received_at="2026-06-04T10:00:01+00:00",
        source_metadata=TELEGRAM_METADATA,
    )

    assert duplicate["intake"]["id"] == first["intake"]["id"]
    assert duplicate["parser_output"]["id"] == first["parser_output"]["id"]
    assert count_rows(conn, "raw_intake_records") == 1
    assert count_rows(conn, "raw_intake_evidence") == 1
    assert count_rows(conn, "parser_outputs") == 1
    assert count_rows(conn, "transactions") == 0


def test_repeated_generic_text_without_metadata_creates_distinct_raw_intake_records(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    raw_input = "Coffee SGD 6.40 at Starbucks"

    first = process_raw_text_input(conn, raw_input, received_at="2026-06-04T10:00:00+00:00")
    second = process_raw_text_input(conn, raw_input, received_at="2026-06-04T10:00:01+00:00")

    assert second["intake"]["id"] != first["intake"]["id"]
    assert first["intake"]["idempotency_key"] is None
    assert second["intake"]["idempotency_key"] is None
    assert count_rows(conn, "raw_intake_records") == 2
    assert count_rows(conn, "raw_intake_evidence") == 2
    assert count_rows(conn, "parser_outputs") == 2
    assert count_rows(conn, "transactions") == 0


def test_runtime_source_evidence_uses_temporary_database_only(
    migrated_temp_db_connection: sqlite3.Connection,
    temp_db_path: Path,
) -> None:
    database_path = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()["file"]

    assert Path(database_path) == temp_db_path
