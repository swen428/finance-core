import json
import sqlite3
from pathlib import Path

import pytest

from finance_core.financial_audit import FinancialAuditRepository, verify_financial_audit_chain
from finance_core.intake.raw_text_service import process_raw_text_input
from finance_core.parser_proposals.confirmation import confirm_proposal, reject_proposal
from finance_core.parser_proposals.conversion import (
    CONVERTED_TRANSACTION_PUBLIC_ID_PREFIX,
    InvalidProposalStatusError,
    MissingConfirmationRecordError,
    MissingRequiredTransactionFieldError,
    ProposalConversionError,
    UnsupportedProposalTypeError,
    convert_confirmed_proposal_to_transaction,
)


@pytest.fixture()
def conversion_db(migrated_temp_db_connection: sqlite3.Connection) -> sqlite3.Connection:
    return migrated_temp_db_connection


def test_confirmed_simple_proposal_can_be_converted(
    conversion_db: sqlite3.Connection,
) -> None:
    parser_output_id = create_confirmed_simple_proposal(conversion_db)

    result = convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    transaction = fetch_one(
        conversion_db,
        "SELECT * FROM transactions WHERE id = ?",
        (result["transaction_id"],),
    )
    notes = json.loads(transaction["notes"])
    assert result["final_transaction_created"] is True
    assert result["transaction_public_id"] == transaction["public_id"]
    assert transaction["public_id"].startswith(
        f"{CONVERTED_TRANSACTION_PUBLIC_ID_PREFIX}_po{parser_output_id}_pc"
    )
    assert transaction["intent"] == "personal_expense_log"
    assert transaction["intent_type"] == "Generated"
    assert transaction["source_channel"] == "telegram"
    assert transaction["transaction_date"] == "2026-06-01"
    assert transaction["amount"] == 6.4
    assert transaction["total_amount"] == 6.4
    assert transaction["currency"] == "SGD"
    assert transaction["merchant"] == "Starbucks"
    assert transaction["raw_input"] == "Coffee SGD 6.40 at Starbucks"
    assert transaction["parser_output_id"] == parser_output_id
    assert notes["parser_proposal_confirmation_id"] == result["confirmation_id"]
    assert notes["parser_output_id"] == parser_output_id
    assert notes["source_evidence"]["raw_text_preserved"] is True


def test_confirmation_and_conversion_share_one_valid_audit_chain(
    conversion_db: sqlite3.Connection,
) -> None:
    parser_output_id = create_confirmed_simple_proposal(conversion_db)
    proposal_public_id = fetch_one(
        conversion_db,
        "SELECT public_id FROM parser_outputs WHERE id = ?",
        (parser_output_id,),
    )["public_id"]
    conversion = convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)
    chain = verify_financial_audit_chain(
        conversion_db,
        aggregate_type="parser_proposal",
        aggregate_public_id=proposal_public_id,
    )
    assert chain.valid is True
    assert chain.event_count == 2
    events = FinancialAuditRepository(conversion_db).list_chain(
        "parser_proposal", proposal_public_id
    )
    assert [event.event_type for event in events] == [
        "parser_proposal_confirmed",
        "parser_proposal_converted",
    ]
    assert events[0].authorization_public_id == conversion["confirmation_id"]
    assert events[1].authorization_public_id == conversion["confirmation_id"]
    assert events[1].previous_state_hash == events[0].new_state_hash


def test_rejection_creates_audited_terminal_state(
    conversion_db: sqlite3.Connection,
) -> None:
    result = process_raw_text_input(conversion_db, "Coffee SGD 6.40 at Starbucks")
    parser_output_id = result["parser_output"]["id"]
    proposal_public_id = result["parser_output"]["public_id"]
    rejection = reject_proposal(
        conversion_db,
        parser_output_id,
        actor="person-owner",
        reason="not mine",
    )
    chain = FinancialAuditRepository(conversion_db).list_chain(
        "parser_proposal", proposal_public_id
    )
    assert rejection["to_status"] == "rejected"
    assert len(chain) == 1
    assert chain[0].event_type == "parser_proposal_rejected"
    assert chain[0].actor_public_id == "person-owner"


def test_confirmation_rolls_back_when_audit_insert_fails(
    conversion_db: sqlite3.Connection,
) -> None:
    result = process_raw_text_input(conversion_db, "Coffee SGD 6.40 at Starbucks")
    parser_output_id = result["parser_output"]["id"]
    conversion_db.execute(
        """CREATE TRIGGER test_fail_parser_audit
        BEFORE INSERT ON financial_audit_events
        BEGIN SELECT RAISE(ABORT, 'injected parser audit failure'); END"""
    )
    conversion_db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected parser audit failure"):
        confirm_proposal(conversion_db, parser_output_id, actor="person-owner")
    assert (
        fetch_one(
            conversion_db,
            "SELECT parse_status FROM parser_outputs WHERE id = ?",
            (parser_output_id,),
        )["parse_status"]
        == "parsed_pending_confirmation"
    )
    assert count_rows(conversion_db, "parser_proposal_authorizations") == 0
    assert count_rows(conversion_db, "financial_audit_events") == 0


@pytest.mark.parametrize(
    "status",
    [
        "parsed_pending_confirmation",
        "edited_pending_confirmation",
        "rejected",
        "superseded",
        "expired",
    ],
)
def test_non_confirmed_proposals_cannot_be_converted(
    conversion_db: sqlite3.Connection,
    status: str,
) -> None:
    parser_output_id = insert_parser_output(
        conversion_db,
        status=status,
        payload=valid_payload(),
    )

    with pytest.raises(InvalidProposalStatusError):
        convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    assert count_rows(conversion_db, "transactions") == 0


@pytest.mark.parametrize(
    ("field_name", "expected_error"),
    [
        ("amount", "amount"),
        ("currency", "currency"),
        ("transaction_date", "transaction_date"),
    ],
)
def test_missing_required_scalar_fields_are_rejected(
    conversion_db: sqlite3.Connection,
    field_name: str,
    expected_error: str,
) -> None:
    payload = valid_payload()
    payload.pop(field_name)
    parser_output_id = insert_confirmed_parser_output(conversion_db, payload)

    with pytest.raises(MissingRequiredTransactionFieldError, match=expected_error):
        convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    assert count_rows(conversion_db, "transactions") == 0


def test_missing_description_and_merchant_is_rejected(
    conversion_db: sqlite3.Connection,
) -> None:
    payload = valid_payload()
    payload.pop("description")
    payload.pop("merchant")
    parser_output_id = insert_confirmed_parser_output(conversion_db, payload)

    with pytest.raises(MissingRequiredTransactionFieldError, match="merchant_or_description"):
        convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    assert count_rows(conversion_db, "transactions") == 0


@pytest.mark.parametrize(
    "proposal_type",
    [
        "shared_expense",
        "receipt_split",
        "transfer",
        "income",
        "investment",
        "reimbursement",
        "settlement",
    ],
)
def test_unsupported_proposal_types_are_rejected(
    conversion_db: sqlite3.Connection,
    proposal_type: str,
) -> None:
    payload = valid_payload()
    payload["transaction_type"] = proposal_type
    parser_output_id = insert_confirmed_parser_output(conversion_db, payload)

    with pytest.raises(UnsupportedProposalTypeError, match="Only simple expense"):
        convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    assert count_rows(conversion_db, "transactions") == 0


@pytest.mark.parametrize(
    "amount",
    ["not-a-number", "6.40 SGD", "6,40", "NaN", "Infinity", [], {}],
)
def test_invalid_amount_formats_are_rejected(
    conversion_db: sqlite3.Connection,
    amount: object,
) -> None:
    payload = valid_payload()
    payload["amount"] = amount
    parser_output_id = insert_confirmed_parser_output(conversion_db, payload)

    with pytest.raises(ProposalConversionError):
        convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    assert count_rows(conversion_db, "transactions") == 0


@pytest.mark.parametrize("amount", ["0", "0.00", "-0.01", "-6.40"])
def test_zero_or_negative_amounts_are_rejected(
    conversion_db: sqlite3.Connection,
    amount: str,
) -> None:
    payload = valid_payload()
    payload["amount"] = amount
    parser_output_id = insert_confirmed_parser_output(conversion_db, payload)

    with pytest.raises(ProposalConversionError):
        convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    assert count_rows(conversion_db, "transactions") == 0


@pytest.mark.parametrize("currency", ["SG", "SGDD", "S1D", "XXX"])
def test_invalid_currency_values_are_rejected(
    conversion_db: sqlite3.Connection,
    currency: str,
) -> None:
    payload = valid_payload()
    payload["currency"] = currency
    parser_output_id = insert_confirmed_parser_output(conversion_db, payload)

    with pytest.raises(ProposalConversionError):
        convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    assert count_rows(conversion_db, "transactions") == 0


@pytest.mark.parametrize("currency", ["", "   "])
def test_empty_currency_values_are_rejected(
    conversion_db: sqlite3.Connection,
    currency: str,
) -> None:
    payload = valid_payload()
    payload["currency"] = currency
    parser_output_id = insert_confirmed_parser_output(conversion_db, payload)

    with pytest.raises((MissingRequiredTransactionFieldError, ProposalConversionError)):
        convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    assert count_rows(conversion_db, "transactions") == 0


@pytest.mark.parametrize(
    "transaction_date",
    ["2026/06/01", "01-06-2026", "2026-6-1", "2026-02-30", "2026-06-01T00:00:00"],
)
def test_invalid_date_formats_are_rejected(
    conversion_db: sqlite3.Connection,
    transaction_date: str,
) -> None:
    payload = valid_payload()
    payload["transaction_date"] = transaction_date
    parser_output_id = insert_confirmed_parser_output(conversion_db, payload)

    with pytest.raises(ProposalConversionError, match="transaction_date"):
        convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    assert count_rows(conversion_db, "transactions") == 0


def test_missing_confirmation_audit_record_is_rejected(
    conversion_db: sqlite3.Connection,
) -> None:
    parser_output_id = insert_parser_output(
        conversion_db,
        status="confirmed",
        payload=valid_payload(),
    )

    with pytest.raises(MissingConfirmationRecordError):
        convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    assert count_rows(conversion_db, "transactions") == 0


def test_rejected_confirmation_decision_cannot_be_converted(
    conversion_db: sqlite3.Connection,
) -> None:
    parser_output_id = insert_parser_output(
        conversion_db,
        status="confirmed",
        payload=valid_payload(),
    )
    insert_confirmation(conversion_db, parser_output_id, decision="rejected")

    with pytest.raises(MissingConfirmationRecordError):
        convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    assert count_rows(conversion_db, "transactions") == 0


def test_rejected_proposal_decision_cannot_be_converted(
    conversion_db: sqlite3.Connection,
) -> None:
    result = process_raw_text_input(conversion_db, "Wrong parse SGD 99.99")
    parser_output_id = result["parser_output"]["id"]
    reject_proposal(conversion_db, parser_output_id, actor="owner", reason="wrong parse")

    with pytest.raises(InvalidProposalStatusError, match="rejected"):
        convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    assert count_rows(conversion_db, "transactions") == 0


def test_confirmation_record_must_match_same_parser_output(
    conversion_db: sqlite3.Connection,
) -> None:
    parser_output_id = insert_parser_output(
        conversion_db,
        status="confirmed",
        payload=valid_payload(),
    )
    other_parser_output_id = insert_parser_output(
        conversion_db,
        status="confirmed",
        payload=valid_payload(),
    )
    insert_confirmation(conversion_db, other_parser_output_id, decision="confirmed")

    with pytest.raises(MissingConfirmationRecordError):
        convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    assert count_rows(conversion_db, "transactions") == 0


def test_raw_input_and_original_parser_json_remain_preserved(
    conversion_db: sqlite3.Connection,
) -> None:
    parser_output_id = create_confirmed_simple_proposal(conversion_db)
    before = fetch_one(
        conversion_db,
        """
        SELECT parse_status, raw_text, parsed_payload, normalized_payload
        FROM parser_outputs
        WHERE id = ?
        """,
        (parser_output_id,),
    )

    convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    after = fetch_one(
        conversion_db,
        """
        SELECT parse_status, raw_text, parsed_payload, normalized_payload
        FROM parser_outputs
        WHERE id = ?
        """,
        (parser_output_id,),
    )
    assert dict(after) == dict(before)


def test_conversion_does_not_modify_proposal_audit_tables(
    conversion_db: sqlite3.Connection,
) -> None:
    parser_output_id = create_confirmed_simple_proposal(conversion_db)
    events_before = snapshot_table(conversion_db, "parser_proposal_events")
    confirmations_before = snapshot_table(conversion_db, "parser_proposal_confirmations")

    convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    assert snapshot_table(conversion_db, "parser_proposal_events") == events_before
    assert snapshot_table(conversion_db, "parser_proposal_confirmations") == confirmations_before


def test_conversion_creates_only_one_transaction_and_no_other_final_records(
    conversion_db: sqlite3.Connection,
) -> None:
    parser_output_id = create_confirmed_simple_proposal(conversion_db)

    convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    assert count_rows(conversion_db, "transactions") == 1
    transaction = fetch_one(
        conversion_db,
        "SELECT public_id, parser_output_id FROM transactions",
    )
    assert transaction["public_id"].startswith(
        f"{CONVERTED_TRANSACTION_PUBLIC_ID_PREFIX}_po{parser_output_id}_pc"
    )
    assert transaction["parser_output_id"] == parser_output_id
    assert count_rows(conversion_db, "shared_expense_obligations") == 0
    assert count_rows(conversion_db, "settlement_obligations") == 0
    assert count_rows(conversion_db, "reconciliation_records") == 0
    assert count_rows(conversion_db, "calculation_runs") == 0


def test_confirmed_shared_expense_proposal_does_not_create_final_facts(
    conversion_db: sqlite3.Connection,
) -> None:
    payload = valid_payload()
    payload["transaction_type"] = "shared_expense"
    parser_output_id = insert_parser_output(
        conversion_db,
        status="parsed_pending_confirmation",
        payload=payload,
    )

    result = confirm_proposal(
        conversion_db,
        parser_output_id,
        actor="owner",
        reason="proposal reviewed only",
    )

    assert result["final_transaction_created"] is False
    assert count_rows(conversion_db, "transactions") == 0
    assert count_rows(conversion_db, "shared_expense_obligations") == 0
    assert count_rows(conversion_db, "settlement_obligations") == 0
    assert count_rows(conversion_db, "reconciliation_records") == 0
    assert count_rows(conversion_db, "calculation_runs") == 0
    with pytest.raises(UnsupportedProposalTypeError, match="Only simple expense"):
        convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)
    assert count_rows(conversion_db, "transactions") == 0


def test_already_converted_proposal_is_rejected(
    conversion_db: sqlite3.Connection,
) -> None:
    parser_output_id = create_confirmed_simple_proposal(conversion_db)
    convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    replay = convert_confirmed_proposal_to_transaction(conversion_db, parser_output_id)

    assert count_rows(conversion_db, "transactions") == 1
    assert replay["idempotent"] is True
    transaction = fetch_one(
        conversion_db,
        "SELECT public_id, parser_output_id FROM transactions",
    )
    assert transaction["public_id"].startswith(
        f"{CONVERTED_TRANSACTION_PUBLIC_ID_PREFIX}_po{parser_output_id}_pc"
    )
    assert transaction["parser_output_id"] == parser_output_id


def test_different_confirmed_parser_outputs_get_different_public_ids(
    conversion_db: sqlite3.Connection,
) -> None:
    first_parser_output_id = create_confirmed_simple_proposal(conversion_db)
    second_parser_output_id = create_confirmed_simple_proposal(conversion_db)

    first_result = convert_confirmed_proposal_to_transaction(
        conversion_db,
        first_parser_output_id,
    )
    second_result = convert_confirmed_proposal_to_transaction(
        conversion_db,
        second_parser_output_id,
    )

    assert first_result["transaction_public_id"].startswith(
        f"{CONVERTED_TRANSACTION_PUBLIC_ID_PREFIX}_po{first_parser_output_id}_pc"
    )
    assert second_result["transaction_public_id"].startswith(
        f"{CONVERTED_TRANSACTION_PUBLIC_ID_PREFIX}_po{second_parser_output_id}_pc"
    )
    assert first_result["transaction_public_id"] != second_result["transaction_public_id"]


def test_conversion_tests_use_temporary_database_only(
    conversion_db: sqlite3.Connection,
    temp_db_path: Path,
) -> None:
    database_path = Path(conversion_db.execute("PRAGMA database_list").fetchone()["file"])

    assert database_path == temp_db_path


def create_confirmed_simple_proposal(conn: sqlite3.Connection) -> int:
    result = process_raw_text_input(conn, "Coffee SGD 6.40 at Starbucks")
    parser_output_id = result["parser_output"]["id"]
    update_payload(conn, parser_output_id, {"transaction_date": "2026-06-01"})
    confirm_proposal(conn, parser_output_id, actor="owner", reason="looks correct")
    return parser_output_id


def insert_confirmed_parser_output(
    conn: sqlite3.Connection,
    payload: dict,
) -> int:
    parser_output_id = insert_parser_output(
        conn, status="parsed_pending_confirmation", payload=payload
    )
    confirm_proposal(conn, parser_output_id, actor="pytest-human")
    return parser_output_id


def insert_confirmation(
    conn: sqlite3.Connection,
    parser_output_id: int,
    *,
    decision: str,
) -> int:
    conn.execute(
        """
        INSERT INTO parser_proposal_confirmations (
          parser_output_id,
          decision,
          decided_by
        )
        VALUES (?, ?, ?)
        """,
        (parser_output_id, decision, "pytest"),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def insert_parser_output(
    conn: sqlite3.Connection,
    *,
    status: str,
    payload: dict,
) -> int:
    public_id_suffix = count_rows(conn, "parser_outputs") + 1
    cursor = conn.execute(
        """
        INSERT INTO parser_outputs (
          public_id,
          source_type,
          parser_name,
          raw_text,
          parsed_payload,
          normalized_payload,
          parse_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"parser_output_test_{status}_{public_id_suffix}",
            "telegram_text",
            "pytest",
            "Coffee SGD 6.40 at Starbucks",
            json.dumps(payload, sort_keys=True),
            json.dumps(payload, sort_keys=True),
            status,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def update_payload(
    conn: sqlite3.Connection,
    parser_output_id: int,
    updates: dict,
) -> None:
    row = fetch_one(
        conn,
        "SELECT parsed_payload FROM parser_outputs WHERE id = ?",
        (parser_output_id,),
    )
    payload = json.loads(row["parsed_payload"])
    payload.update(updates)
    payload_json = json.dumps(payload, sort_keys=True)
    conn.execute(
        """
        UPDATE parser_outputs
        SET parsed_payload = ?, normalized_payload = ?
        WHERE id = ?
        """,
        (payload_json, payload_json, parser_output_id),
    )
    conn.commit()


def valid_payload() -> dict:
    return {
        "intent": "personal_expense_log",
        "transaction_type": "personal_expense",
        "amount": "6.40",
        "currency": "SGD",
        "transaction_date": "2026-06-01",
        "description": "Coffee",
        "merchant": "Starbucks",
        "category": "coffee",
        "is_final": False,
    }


def fetch_one(
    conn: sqlite3.Connection,
    query: str,
    params: tuple = (),
) -> sqlite3.Row:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    return fetch_one(conn, f"SELECT COUNT(*) AS count FROM {table}")["count"]


def snapshot_table(conn: sqlite3.Connection, table: str) -> list[dict]:
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    return [dict(row) for row in rows]
