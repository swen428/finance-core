import sqlite3
from io import StringIO
from pathlib import Path

from finance_core.intake.raw_intake_review_cli import main
from finance_core.intake.raw_text_service import process_raw_text_input


def insert_proposal(db_path: Path, raw_input: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    result = process_raw_text_input(conn, raw_input)
    conn.close()
    return result


def run_cli(*args: str) -> str:
    output = StringIO()
    exit_code = main(list(args), out=output, err=StringIO())
    assert exit_code == 0
    return output.getvalue()


def transaction_count(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    count = conn.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()["count"]
    conn.close()
    return count


def test_list_pending_records(migrated_temp_db_path: Path) -> None:
    db_path = migrated_temp_db_path
    result = insert_proposal(db_path, "NTUC SGD 28.60 groceries")

    output = run_cli("list", "--db", str(db_path))

    assert "parsed_pending_confirmation" in output
    assert "NTUC SGD 28.60 groceries" in output
    assert result["intake"]["public_id"] in output
    assert f"parser_output_id={result['intake']['parser_output_id']}" in output
    assert "Safety: proposal status is parsed_pending_confirmation" in output


def test_show_one_record_includes_raw_input_and_parser_proposal(
    migrated_temp_db_path: Path,
) -> None:
    db_path = migrated_temp_db_path
    result = insert_proposal(db_path, "Lunch SGD 12.50 at ExampleCafe paid by Owner")

    output = run_cli("show", "--db", str(db_path), "--id", str(result["intake"]["id"]))

    assert "raw_input_exact: >>>Lunch SGD 12.50 at ExampleCafe paid by Owner<<<" in output
    assert "Parser proposal" in output
    assert "amount: 12.50" in output
    assert "currency: SGD" in output
    assert "merchant: ExampleCafe" in output
    assert "confirmation_required: true" in output
    assert "is_final: false" in output
    assert "Safety: proposal status is parsed_pending_confirmation" in output


def test_missing_amount_review_includes_missing_fields_and_safety_notice(
    migrated_temp_db_path: Path,
) -> None:
    db_path = migrated_temp_db_path
    result = insert_proposal(db_path, "Coffee at Starbucks")

    output = run_cli("show", "--db", str(db_path), "--id", str(result["intake"]["id"]))

    assert "missing_fields" in output
    assert "amount" in output
    assert "confirmation_required: true" in output
    assert "parsed_pending_confirmation" in output
    assert "Safety: proposal status is parsed_pending_confirmation" in output


def test_cli_list_and_show_do_not_create_final_transaction_records(
    migrated_temp_db_path: Path,
) -> None:
    db_path = migrated_temp_db_path
    result = insert_proposal(db_path, "I paid SGD 9.00 for stationery, shared equally with B")

    before = transaction_count(db_path)
    list_output = run_cli("list", "--db", str(db_path))
    show_output = run_cli("show", "--db", str(db_path), "--id", str(result["intake"]["id"]))
    after = transaction_count(db_path)

    assert before == 0
    assert after == 0
    assert "shared_expense" in show_output
    assert "split_type: equal" in show_output
    assert "does not create final transactions" in list_output
    assert "does not create final transactions" in show_output


def test_review_cli_uses_temporary_database(
    migrated_temp_db_path: Path,
    temp_db_path: Path,
) -> None:
    db_path = migrated_temp_db_path

    output = run_cli("list", "--db", str(db_path))

    assert db_path == temp_db_path
    assert "Safety: proposal status is parsed_pending_confirmation" in output
