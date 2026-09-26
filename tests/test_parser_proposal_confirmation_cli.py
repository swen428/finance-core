import sqlite3
from io import StringIO
from pathlib import Path

from finance_core.intake.raw_intake_review_cli import main
from finance_core.intake.raw_text_service import process_raw_text_input
from finance_core.parser_proposals.confirmation import confirm_proposal, reject_proposal


def insert_proposal(db_path: Path, raw_input: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    result = process_raw_text_input(conn, raw_input, source_type="manual_entry")
    conn.commit()
    conn.close()
    return result


def run_cli_success(*args: str) -> str:
    output = StringIO()
    error = StringIO()
    exit_code = main(list(args), out=output, err=error)
    assert exit_code == 0, error.getvalue()
    return output.getvalue()


def run_cli_failure(*args: str) -> str:
    output = StringIO()
    error = StringIO()
    exit_code = main(list(args), out=output, err=error)
    assert exit_code != 0
    return error.getvalue()


def fetch_one(db_path: Path, query: str, params: tuple = ()) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(query, params).fetchone()
    conn.close()
    assert row is not None
    return row


def count_rows(db_path: Path, table: str) -> int:
    return fetch_one(db_path, f"SELECT COUNT(*) AS count FROM {table}")["count"]


def test_cli_can_list_pending_proposals(migrated_temp_db_path: Path) -> None:
    db_path = migrated_temp_db_path
    result = insert_proposal(db_path, "NTUC SGD 28.60 groceries")

    output = run_cli_success("list", "--db", str(db_path))

    assert "parsed_pending_confirmation" in output
    assert f"parser_output_id={result['parser_output']['id']}" in output
    assert "NTUC SGD 28.60 groceries" in output


def test_cli_can_show_pending_proposal_details(migrated_temp_db_path: Path) -> None:
    db_path = migrated_temp_db_path
    result = insert_proposal(db_path, "Lunch SGD 12.50 at ExampleCafe paid by Owner")

    output = run_cli_success(
        "show-proposal",
        "--db",
        str(db_path),
        "--parser-output-id",
        str(result["parser_output"]["id"]),
    )

    assert "Parser proposal" in output
    assert "raw_text_exact: >>>Lunch SGD 12.50 at ExampleCafe paid by Owner<<<" in output
    assert "parse_status: parsed_pending_confirmation" in output
    assert "amount: 12.50" in output
    assert "is_final: false" in output


def test_cli_confirm_writes_event_and_confirmation_rows(
    migrated_temp_db_path: Path,
) -> None:
    db_path = migrated_temp_db_path
    result = insert_proposal(db_path, "Coffee SGD 6.40 at Starbucks")
    parser_output_id = result["parser_output"]["id"]

    output = run_cli_success(
        "confirm",
        "--db",
        str(db_path),
        "--parser-output-id",
        str(parser_output_id),
        "--actor",
        "owner",
        "--reason",
        "looks correct",
    )

    event = fetch_one(db_path, "SELECT * FROM parser_proposal_events")
    confirmation = fetch_one(db_path, "SELECT * FROM parser_proposal_confirmations")
    parser_output = fetch_one(
        db_path, "SELECT * FROM parser_outputs WHERE id = ?", (parser_output_id,)
    )

    assert "to_status: confirmed" in output
    assert "actor_type: human" in output
    assert "raw_intake_status: confirmed" in output
    assert event["parser_output_id"] == parser_output_id
    assert event["from_status"] == "parsed_pending_confirmation"
    assert event["to_status"] == "confirmed"
    assert event["event_type"] == "confirmed"
    assert event["actor_type"] == "user"
    assert event["actor_identifier"] == "owner"
    assert event["event_reason"] == "looks correct"
    assert confirmation["parser_output_id"] == parser_output_id
    assert confirmation["decision"] == "confirmed"
    assert confirmation["decided_by"] == "owner"
    assert parser_output["parse_status"] == "confirmed"


def test_confirmation_service_accepts_explicit_non_cli_actor_type(
    migrated_temp_db_path: Path,
) -> None:
    db_path = migrated_temp_db_path
    result = insert_proposal(db_path, "Coffee SGD 6.40 at Starbucks")
    parser_output_id = result["parser_output"]["id"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        service_result = confirm_proposal(
            conn,
            parser_output_id,
            actor="owner",
            actor_type="user",
            reason="approved in app",
        )
    finally:
        conn.close()

    event = fetch_one(db_path, "SELECT * FROM parser_proposal_events")
    intake = fetch_one(
        db_path,
        "SELECT status FROM raw_intake_records WHERE parser_output_id = ?",
        (parser_output_id,),
    )
    assert service_result["actor_type"] == "human"
    assert service_result["raw_intake_status"] == "confirmed"
    assert event["actor_type"] == "user"
    assert event["actor_identifier"] == "owner"
    assert intake["status"] == "confirmed"


def test_invalid_confirmation_actor_type_is_rejected(
    migrated_temp_db_path: Path,
) -> None:
    db_path = migrated_temp_db_path
    result = insert_proposal(db_path, "Coffee SGD 6.40 at Starbucks")
    parser_output_id = result["parser_output"]["id"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        try:
            confirm_proposal(
                conn,
                parser_output_id,
                actor="owner",
                actor_type="browser",
            )
        except ValueError as exc:
            assert "Only authenticated human actors" in str(exc)
        else:
            raise AssertionError("invalid actor_type was accepted")
    finally:
        conn.close()

    parser_output = fetch_one(
        db_path,
        "SELECT parse_status FROM parser_outputs WHERE id = ?",
        (parser_output_id,),
    )
    intake = fetch_one(
        db_path,
        "SELECT status FROM raw_intake_records WHERE parser_output_id = ?",
        (parser_output_id,),
    )
    assert parser_output["parse_status"] == "parsed_pending_confirmation"
    assert intake["status"] == "parsed_pending_confirmation"
    assert count_rows(db_path, "parser_proposal_events") == 0
    assert count_rows(db_path, "parser_proposal_confirmations") == 0


def test_cli_reject_writes_event_and_confirmation_rows(
    migrated_temp_db_path: Path,
) -> None:
    db_path = migrated_temp_db_path
    result = insert_proposal(db_path, "Wrong amount SGD 99.99")
    parser_output_id = result["parser_output"]["id"]

    output = run_cli_success(
        "reject",
        "--db",
        str(db_path),
        "--parser-output-id",
        str(parser_output_id),
        "--reason",
        "wrong merchant",
    )

    event = fetch_one(db_path, "SELECT * FROM parser_proposal_events")
    confirmation = fetch_one(db_path, "SELECT * FROM parser_proposal_confirmations")
    parser_output = fetch_one(
        db_path, "SELECT * FROM parser_outputs WHERE id = ?", (parser_output_id,)
    )

    assert "to_status: rejected" in output
    assert event["to_status"] == "rejected"
    assert event["event_type"] == "rejected"
    assert event["event_reason"] == "wrong merchant"
    assert confirmation["decision"] == "rejected"
    assert parser_output["parse_status"] == "rejected"


def test_cli_history_displays_lifecycle_events(migrated_temp_db_path: Path) -> None:
    db_path = migrated_temp_db_path
    result = insert_proposal(db_path, "Dinner SGD 18.20")
    parser_output_id = result["parser_output"]["id"]
    run_cli_success(
        "confirm",
        "--db",
        str(db_path),
        "--parser-output-id",
        str(parser_output_id),
        "--reason",
        "approved",
    )

    output = run_cli_success(
        "history",
        "--db",
        str(db_path),
        "--parser-output-id",
        str(parser_output_id),
    )

    assert "event_type=confirmed" in output
    assert "from_status=parsed_pending_confirmation" in output
    assert "to_status=confirmed" in output
    assert "actor_type=user" in output
    assert "reason=approved" in output
    assert "payload=" in output


def test_confirm_and_reject_unknown_parser_output_id_fail_safely(
    migrated_temp_db_path: Path,
) -> None:
    db_path = migrated_temp_db_path

    confirm_error = run_cli_failure(
        "confirm",
        "--db",
        str(db_path),
        "--parser-output-id",
        "999",
    )
    reject_error = run_cli_failure(
        "reject",
        "--db",
        str(db_path),
        "--parser-output-id",
        "999",
    )

    assert "parser output not found: 999" in confirm_error
    assert "parser output not found: 999" in reject_error
    assert count_rows(db_path, "parser_proposal_events") == 0
    assert count_rows(db_path, "parser_proposal_confirmations") == 0


def test_confirm_and_reject_invalid_transition_fail_safely(
    migrated_temp_db_path: Path,
) -> None:
    db_path = migrated_temp_db_path
    result = insert_proposal(db_path, "Taxi SGD 14.00")
    parser_output_id = result["parser_output"]["id"]
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE parser_outputs SET parse_status = ? WHERE id = ?",
        ("edited", parser_output_id),
    )
    conn.commit()
    conn.close()

    confirm_error = run_cli_failure(
        "confirm",
        "--db",
        str(db_path),
        "--parser-output-id",
        str(parser_output_id),
    )
    reject_error = run_cli_failure(
        "reject",
        "--db",
        str(db_path),
        "--parser-output-id",
        str(parser_output_id),
    )

    assert "Unknown parser proposal status: edited" in confirm_error
    assert "Unknown parser proposal status: edited" in reject_error
    assert count_rows(db_path, "parser_proposal_events") == 0
    assert count_rows(db_path, "parser_proposal_confirmations") == 0


def test_repeated_confirm_returns_existing_decision_without_duplicate_rows(
    migrated_temp_db_path: Path,
) -> None:
    db_path = migrated_temp_db_path
    result = insert_proposal(db_path, "Tea SGD 3.20")
    parser_output_id = result["parser_output"]["id"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        first = confirm_proposal(
            conn,
            parser_output_id,
            actor="owner",
            actor_type="user",
            reason="approved once",
        )
        second = confirm_proposal(conn, parser_output_id, actor="owner", actor_type="human")
    finally:
        conn.close()

    event = fetch_one(db_path, "SELECT * FROM parser_proposal_events")
    confirmation = fetch_one(db_path, "SELECT * FROM parser_proposal_confirmations")
    assert second["event_id"] is None
    assert second["confirmation_id"] == first["confirmation_id"]
    assert second["idempotent"] is True
    assert second["actor_type"] == "human"
    assert event["actor_type"] == "user"
    assert event["actor_identifier"] == "owner"
    assert event["event_reason"] == "approved once"
    assert confirmation["decided_by"] == "owner"
    assert confirmation["decision_reason"] == "approved once"
    assert count_rows(db_path, "parser_proposal_events") == 1
    assert count_rows(db_path, "parser_proposal_confirmations") == 1


def test_repeated_reject_returns_existing_decision_without_duplicate_rows(
    migrated_temp_db_path: Path,
) -> None:
    db_path = migrated_temp_db_path
    result = insert_proposal(db_path, "Wrong receipt SGD 99.99")
    parser_output_id = result["parser_output"]["id"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        first = reject_proposal(
            conn,
            parser_output_id,
            actor="owner",
            actor_type="user",
            reason="wrong amount",
        )
        second = reject_proposal(conn, parser_output_id, actor="owner", actor_type="human")
    finally:
        conn.close()

    event = fetch_one(db_path, "SELECT * FROM parser_proposal_events")
    confirmation = fetch_one(db_path, "SELECT * FROM parser_proposal_confirmations")
    assert second["event_id"] is None
    assert second["confirmation_id"] == first["confirmation_id"]
    assert second["idempotent"] is True
    assert second["actor_type"] == "human"
    assert event["actor_type"] == "user"
    assert event["actor_identifier"] == "owner"
    assert event["event_reason"] == "wrong amount"
    assert confirmation["decided_by"] == "owner"
    assert confirmation["decision_reason"] == "wrong amount"
    assert count_rows(db_path, "parser_proposal_events") == 1
    assert count_rows(db_path, "parser_proposal_confirmations") == 1


def test_confirmed_proposal_later_reject_is_explicitly_blocked(
    migrated_temp_db_path: Path,
) -> None:
    db_path = migrated_temp_db_path
    result = insert_proposal(db_path, "Tea SGD 3.20")
    parser_output_id = result["parser_output"]["id"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        confirm_proposal(conn, parser_output_id, actor="owner", reason="approved")
        try:
            reject_proposal(conn, parser_output_id, actor="owner", reason="changed mind")
        except ValueError as exc:
            assert "Conflicting parser proposal confirmation replay" in str(exc)
        else:
            raise AssertionError("conflicting reject was accepted")
    finally:
        conn.close()

    parser_output = fetch_one(
        db_path,
        "SELECT parse_status FROM parser_outputs WHERE id = ?",
        (parser_output_id,),
    )
    assert parser_output["parse_status"] == "confirmed"
    assert count_rows(db_path, "parser_proposal_events") == 1
    assert count_rows(db_path, "parser_proposal_confirmations") == 1


def test_rejected_proposal_later_confirm_is_explicitly_blocked(
    migrated_temp_db_path: Path,
) -> None:
    db_path = migrated_temp_db_path
    result = insert_proposal(db_path, "Wrong receipt SGD 99.99")
    parser_output_id = result["parser_output"]["id"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        reject_proposal(conn, parser_output_id, actor="owner", reason="wrong amount")
        try:
            confirm_proposal(conn, parser_output_id, actor="owner", reason="changed mind")
        except ValueError as exc:
            assert "Conflicting parser proposal confirmation replay" in str(exc)
        else:
            raise AssertionError("conflicting confirm was accepted")
    finally:
        conn.close()

    parser_output = fetch_one(
        db_path,
        "SELECT parse_status FROM parser_outputs WHERE id = ?",
        (parser_output_id,),
    )
    assert parser_output["parse_status"] == "rejected"
    assert count_rows(db_path, "parser_proposal_events") == 1
    assert count_rows(db_path, "parser_proposal_confirmations") == 1


def test_confirm_and_reject_terminal_proposals_are_idempotent_for_same_decision(
    migrated_temp_db_path: Path,
) -> None:
    db_path = migrated_temp_db_path
    confirmed = insert_proposal(db_path, "Tea SGD 3.20")
    rejected = insert_proposal(db_path, "Snack SGD 4.80")
    run_cli_success(
        "confirm",
        "--db",
        str(db_path),
        "--parser-output-id",
        str(confirmed["parser_output"]["id"]),
    )
    run_cli_success(
        "reject",
        "--db",
        str(db_path),
        "--parser-output-id",
        str(rejected["parser_output"]["id"]),
    )

    confirm_output = run_cli_success(
        "confirm",
        "--db",
        str(db_path),
        "--parser-output-id",
        str(confirmed["parser_output"]["id"]),
    )
    reject_output = run_cli_success(
        "reject",
        "--db",
        str(db_path),
        "--parser-output-id",
        str(rejected["parser_output"]["id"]),
    )

    assert "to_status: confirmed" in confirm_output
    assert "to_status: rejected" in reject_output
    assert count_rows(db_path, "parser_proposal_events") == 2
    assert count_rows(db_path, "parser_proposal_confirmations") == 2


def test_confirm_and_reject_preserve_raw_input_and_parser_output_json(
    migrated_temp_db_path: Path,
) -> None:
    db_path = migrated_temp_db_path
    confirmed = insert_proposal(db_path, "Book SGD 21.00 at Kinokuniya")
    rejected = insert_proposal(db_path, "Bad parse SGD 8.00")
    confirmed_id = confirmed["parser_output"]["id"]
    rejected_id = rejected["parser_output"]["id"]
    before_confirmed = fetch_one(
        db_path,
        "SELECT raw_text, parsed_payload FROM parser_outputs WHERE id = ?",
        (confirmed_id,),
    )
    before_rejected = fetch_one(
        db_path,
        "SELECT raw_text, parsed_payload FROM parser_outputs WHERE id = ?",
        (rejected_id,),
    )

    run_cli_success("confirm", "--db", str(db_path), "--parser-output-id", str(confirmed_id))
    run_cli_success("reject", "--db", str(db_path), "--parser-output-id", str(rejected_id))

    after_confirmed = fetch_one(
        db_path,
        "SELECT raw_text, parsed_payload FROM parser_outputs WHERE id = ?",
        (confirmed_id,),
    )
    after_rejected = fetch_one(
        db_path,
        "SELECT raw_text, parsed_payload FROM parser_outputs WHERE id = ?",
        (rejected_id,),
    )
    assert after_confirmed["raw_text"] == before_confirmed["raw_text"]
    assert after_confirmed["parsed_payload"] == before_confirmed["parsed_payload"]
    assert after_rejected["raw_text"] == before_rejected["raw_text"]
    assert after_rejected["parsed_payload"] == before_rejected["parsed_payload"]


def test_confirm_and_reject_do_not_create_transactions_rows(
    migrated_temp_db_path: Path,
) -> None:
    db_path = migrated_temp_db_path
    confirmed = insert_proposal(db_path, "Groceries SGD 30.00")
    rejected = insert_proposal(db_path, "Duplicate SGD 30.00")

    run_cli_success(
        "confirm",
        "--db",
        str(db_path),
        "--parser-output-id",
        str(confirmed["parser_output"]["id"]),
    )
    run_cli_success(
        "reject",
        "--db",
        str(db_path),
        "--parser-output-id",
        str(rejected["parser_output"]["id"]),
    )

    assert count_rows(db_path, "transactions") == 0


def test_confirmation_cli_uses_temporary_database(
    migrated_temp_db_path: Path,
    temp_db_path: Path,
) -> None:
    db_path = migrated_temp_db_path
    result = insert_proposal(db_path, "Temporary DB SGD 1.00")

    output = run_cli_success(
        "history",
        "--db",
        str(db_path),
        "--parser-output-id",
        str(result["parser_output"]["id"]),
    )

    assert db_path == temp_db_path
    assert "parser output not found" not in output
