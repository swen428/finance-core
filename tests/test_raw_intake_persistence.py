import sqlite3
from pathlib import Path

import pytest

from finance_core.intake.raw_text_repository import (
    RawIntakeIdempotencyConflictError,
    save_parser_proposal,
)
from finance_core.intake.raw_text_service import process_raw_text_input
from finance_core.parser_proposals.lifecycle import (
    CONFIRMED,
    EDITED_PENDING_CONFIRMATION,
    EXPIRED,
    PARSED_PENDING_CONFIRMATION,
    REJECTED,
    SUPERSEDED,
    raw_intake_status_for_proposal_status,
)
from finance_core.parsers.text_expense_parser import parse_text_expense


@pytest.fixture()
def raw_intake_db(legacy_temp_db_connection: sqlite3.Connection) -> sqlite3.Connection:
    return legacy_temp_db_connection


def transaction_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()
    return row["count"]


def test_raw_input_is_preserved_in_sqlite(raw_intake_db: sqlite3.Connection) -> None:
    conn = raw_intake_db
    raw_input = "  NTUC   SGD 28.60   groceries  "

    result = process_raw_text_input(conn, raw_input)

    assert result["intake"]["raw_input"] == raw_input
    assert result["intake"]["source_type"] == "telegram_text"
    assert result["intake"]["status"] != "confirmed"
    assert transaction_count(conn) == 0


def test_raw_intake_same_key_with_different_content_is_conflict(
    raw_intake_db: sqlite3.Connection,
) -> None:
    metadata = {"chat_id": "chat-1", "message_id": "message-1"}
    process_raw_text_input(raw_intake_db, "Coffee SGD 6.40", source_metadata=metadata)

    with pytest.raises(RawIntakeIdempotencyConflictError) as exc_info:
        process_raw_text_input(raw_intake_db, "Coffee SGD 7.40", source_metadata=metadata)

    assert exc_info.value.reason_code == "IDEMPOTENCY_KEY_CONTENT_CONFLICT"
    assert raw_intake_db.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0] == 1


def test_parser_proposal_is_saved_as_json_and_pending_confirmation(
    raw_intake_db: sqlite3.Connection,
) -> None:
    conn = raw_intake_db

    result = process_raw_text_input(conn, "NTUC SGD 28.60 groceries")

    proposal = result["parser_output"]["proposal"]
    assert proposal["amount"] == "28.60"
    assert proposal["currency"] == "SGD"
    assert proposal["confirmation_required"] is True
    assert proposal["is_final"] is False
    assert result["intake"]["status"] == "parsed_pending_confirmation"
    assert result["parser_output"]["parse_status"] == "parsed_pending_confirmation"
    assert transaction_count(conn) == 0


def test_parser_confidence_and_field_evidence_are_persisted(
    raw_intake_db: sqlite3.Connection,
) -> None:
    conn = raw_intake_db

    result = process_raw_text_input(conn, "Coffee SGD 6.40 at Starbucks")

    parser_output = result["parser_output"]
    proposal = parser_output["proposal"]
    evidence_rows = conn.execute(
        """
        SELECT field_name, proposed_value, confidence_score, evidence_source_type,
               evidence_reference, notes
        FROM parser_proposal_field_evidence
        WHERE parser_output_id = ?
        ORDER BY id
        """,
        (parser_output["id"],),
    ).fetchall()

    assert parser_output["confidence_score"] == float(proposal["confidence"])
    assert proposal["confidence_metadata"]["field_confidence"]["amount"] == "0.95"
    assert proposal["source_tracking"]["source_type"] == "telegram_text"
    assert proposal["source_tracking"]["source_reference"] == result["intake"]["public_id"]
    assert parser_output["source_type"] == "telegram_text"
    assert parser_output["source_public_id"] == result["intake"]["public_id"]
    assert parser_output["raw_text"] == "Coffee SGD 6.40 at Starbucks"
    assert result["intake"]["raw_input"] == "Coffee SGD 6.40 at Starbucks"
    assert {row["field_name"] for row in evidence_rows} >= {
        "amount",
        "currency",
        "merchant",
        "description",
    }
    amount_row = next(row for row in evidence_rows if row["field_name"] == "amount")
    assert amount_row["proposed_value"] == "6.40"
    assert amount_row["confidence_score"] == 0.95
    assert amount_row["evidence_source_type"] == "raw_input"
    assert amount_row["evidence_reference"] == result["intake"]["public_id"]
    assert "substring=SGD 6.40" in amount_row["notes"]
    assert parser_output["field_evidence"][0]["field_name"] == "amount"
    assert transaction_count(conn) == 0


def test_missing_amount_remains_pending(raw_intake_db: sqlite3.Connection) -> None:
    conn = raw_intake_db

    result = process_raw_text_input(conn, "Coffee at Starbucks")

    proposal = result["proposal"]
    assert "amount" in proposal["missing_fields"]
    assert result["intake"]["status"] == "parsed_pending_confirmation"
    assert proposal["status"] == "parsed_pending_confirmation"
    assert transaction_count(conn) == 0


def test_shared_expense_remains_parser_proposal(raw_intake_db: sqlite3.Connection) -> None:
    conn = raw_intake_db

    result = process_raw_text_input(
        conn,
        "I paid SGD 9.00 for stationery, shared equally with B",
    )

    proposal = result["proposal"]
    assert proposal["transaction_type"] == "shared_expense"
    assert proposal["intent"] == "shared_expense_log"
    assert "B" in proposal["participants"]
    assert proposal["split_type"] == "equal"
    assert proposal["confirmation_required"] is True
    assert result["intake"]["status"] == "parsed_pending_confirmation"
    assert proposal["status"] == "parsed_pending_confirmation"
    assert transaction_count(conn) == 0


@pytest.mark.parametrize(
    "proposal_status",
    [
        PARSED_PENDING_CONFIRMATION,
        EDITED_PENDING_CONFIRMATION,
        CONFIRMED,
        REJECTED,
        SUPERSEDED,
        EXPIRED,
    ],
)
def test_raw_intake_accepts_mapped_proposal_lifecycle_statuses(
    raw_intake_db: sqlite3.Connection,
    proposal_status: str,
) -> None:
    conn = raw_intake_db
    result = process_raw_text_input(conn, "Coffee SGD 6.40 at Starbucks")
    raw_intake_status = raw_intake_status_for_proposal_status(proposal_status)

    conn.execute(
        "UPDATE raw_intake_records SET status = ? WHERE id = ?",
        (raw_intake_status, result["intake"]["id"]),
    )

    row = conn.execute(
        "SELECT status FROM raw_intake_records WHERE id = ?",
        (result["intake"]["id"],),
    ).fetchone()
    assert row["status"] == raw_intake_status


def test_persistence_uses_temporary_database_only(
    raw_intake_db: sqlite3.Connection,
    temp_db_path: Path,
) -> None:
    conn = raw_intake_db

    database_path = conn.execute("PRAGMA database_list").fetchone()["file"]

    assert Path(database_path) == temp_db_path


# ---------------------------------------------------------------------------
# Round 4 fix R4-F6: repeating save_parser_proposal on the same intake must
# form a typed supersession lineage (new output is a direct child of the
# current pointer) instead of tripping migration 035's pointer lineage
# trigger with a bare sqlite3.IntegrityError.
# ---------------------------------------------------------------------------


def _reparse(result: dict, raw_input: str) -> dict:
    return parse_text_expense(
        raw_input,
        raw_input_reference=result["intake"]["public_id"],
        source_type="telegram_text",
    )


def test_repeat_save_parser_proposal_forms_supersession_lineage(
    raw_intake_db: sqlite3.Connection,
) -> None:
    conn = raw_intake_db
    result = process_raw_text_input(conn, "Coffee SGD 6.40 at Starbucks")
    intake_id = result["intake"]["id"]
    first_output_id = result["parser_output"]["id"]

    second = save_parser_proposal(conn, intake_id, _reparse(result, "Coffee SGD 7.40 at Starbucks"))
    conn.commit()

    assert second["id"] != first_output_id
    child = conn.execute(
        "SELECT parent_parser_output_id, parse_status FROM parser_outputs WHERE id = ?",
        (second["id"],),
    ).fetchone()
    assert child["parent_parser_output_id"] == first_output_id
    assert child["parse_status"] == PARSED_PENDING_CONFIRMATION
    prior = conn.execute(
        "SELECT parse_status FROM parser_outputs WHERE id = ?", (first_output_id,)
    ).fetchone()
    assert prior["parse_status"] == SUPERSEDED
    intake = conn.execute(
        "SELECT parser_output_id, status FROM raw_intake_records WHERE id = ?", (intake_id,)
    ).fetchone()
    assert intake["parser_output_id"] == second["id"]
    assert intake["status"] == "parsed_pending_confirmation"
    assert transaction_count(conn) == 0


def test_third_save_extends_supersession_chain_from_current_pointer(
    raw_intake_db: sqlite3.Connection,
) -> None:
    conn = raw_intake_db
    result = process_raw_text_input(conn, "Coffee SGD 6.40 at Starbucks")
    intake_id = result["intake"]["id"]

    second = save_parser_proposal(conn, intake_id, _reparse(result, "Coffee SGD 7.40 at Starbucks"))
    third = save_parser_proposal(conn, intake_id, _reparse(result, "Coffee SGD 8.40 at Starbucks"))
    conn.commit()

    grandchild = conn.execute(
        "SELECT parent_parser_output_id, parse_status FROM parser_outputs WHERE id = ?",
        (third["id"],),
    ).fetchone()
    assert grandchild["parent_parser_output_id"] == second["id"]
    assert grandchild["parse_status"] == PARSED_PENDING_CONFIRMATION
    assert (
        conn.execute(
            "SELECT parse_status FROM parser_outputs WHERE id = ?", (second["id"],)
        ).fetchone()["parse_status"]
        == SUPERSEDED
    )
    intake = conn.execute(
        "SELECT parser_output_id FROM raw_intake_records WHERE id = ?", (intake_id,)
    ).fetchone()
    assert intake["parser_output_id"] == third["id"]


@pytest.mark.parametrize("terminal_status", [CONFIRMED, REJECTED, SUPERSEDED, EXPIRED])
def test_repeat_save_on_terminal_proposal_raises_typed_lifecycle_error(
    raw_intake_db: sqlite3.Connection,
    terminal_status: str,
) -> None:
    conn = raw_intake_db
    result = process_raw_text_input(conn, "Coffee SGD 6.40 at Starbucks")
    first_output_id = result["parser_output"]["id"]
    conn.execute(
        "UPDATE parser_outputs SET parse_status = ? WHERE id = ?",
        (terminal_status, first_output_id),
    )
    conn.commit()

    with pytest.raises(ValueError, match="Terminal parser proposal status cannot transition"):
        save_parser_proposal(
            conn, result["intake"]["id"], _reparse(result, "Coffee SGD 7.40 at Starbucks")
        )

    # The typed refusal writes nothing: one proposal, unchanged pointer.
    assert conn.execute("SELECT COUNT(*) FROM parser_outputs").fetchone()[0] == 1
    intake = conn.execute(
        "SELECT parser_output_id FROM raw_intake_records WHERE id = ?",
        (result["intake"]["id"],),
    ).fetchone()
    assert intake["parser_output_id"] == first_output_id
