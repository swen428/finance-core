import sqlite3
from pathlib import Path

import pytest

LIFECYCLE_STATUSES = [
    "parsed_pending_confirmation",
    "confirmed",
    "rejected",
    "edited_pending_confirmation",
    "superseded",
    "expired",
]

CONFIRMATION_DECISIONS = [
    "confirmed",
    "rejected",
    "edited",
    "superseded",
    "expired",
]


@pytest.fixture()
def proposal_db(migrated_temp_db_connection: sqlite3.Connection) -> sqlite3.Connection:
    conn = migrated_temp_db_connection
    conn.execute(
        """
        INSERT INTO parser_outputs (
          public_id,
          source_type,
          parser_name,
          parsed_payload,
          parse_status
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "parse_test_confirmation_001",
            "telegram_text",
            "test_parser",
            '{"amount":"12.50","currency":"SGD"}',
            "parsed_pending_confirmation",
        ),
    )
    conn.commit()
    return conn


def test_new_tables_exist(proposal_db: sqlite3.Connection) -> None:
    rows = proposal_db.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name IN (
            'parser_proposal_events',
            'parser_proposal_confirmations',
            'parser_proposal_field_evidence'
          )
        """
    ).fetchall()

    assert {row["name"] for row in rows} == {
        "parser_proposal_events",
        "parser_proposal_confirmations",
        "parser_proposal_field_evidence",
    }


@pytest.mark.parametrize("status", LIFECYCLE_STATUSES)
def test_allowed_lifecycle_statuses_are_accepted(
    proposal_db: sqlite3.Connection,
    status: str,
) -> None:
    proposal_db.execute(
        """
        INSERT INTO parser_proposal_events (
          parser_output_id,
          from_status,
          to_status,
          event_type,
          actor_type
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (1, None, status, "created", "test"),
    )


def test_invalid_lifecycle_status_is_rejected(
    proposal_db: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        proposal_db.execute(
            """
            INSERT INTO parser_proposal_events (
              parser_output_id,
              to_status,
              event_type,
              actor_type
            )
            VALUES (?, ?, ?, ?)
            """,
            (1, "finalized", "created", "test"),
        )


@pytest.mark.parametrize("decision", CONFIRMATION_DECISIONS)
def test_allowed_confirmation_decisions_are_accepted(
    proposal_db: sqlite3.Connection,
    decision: str,
) -> None:
    proposal_db.execute(
        """
        INSERT INTO parser_proposal_confirmations (
          parser_output_id,
          decision,
          decided_by
        )
        VALUES (?, ?, ?)
        """,
        (1, decision, "pytest"),
    )


def test_invalid_confirmation_decision_is_rejected(
    proposal_db: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        proposal_db.execute(
            """
            INSERT INTO parser_proposal_confirmations (
              parser_output_id,
              decision,
              decided_by
            )
            VALUES (?, ?, ?)
            """,
            (1, "finalized", "pytest"),
        )


@pytest.mark.parametrize("confidence_score", [None, 0, 1, 0.75])
def test_confidence_score_accepts_null_and_zero_to_one_values(
    proposal_db: sqlite3.Connection,
    confidence_score: float | None,
) -> None:
    proposal_db.execute(
        """
        INSERT INTO parser_proposal_field_evidence (
          parser_output_id,
          field_name,
          proposed_value,
          confidence_score,
          evidence_source_type
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (1, "amount", "12.50", confidence_score, "raw_input"),
    )


@pytest.mark.parametrize("confidence_score", [-0.01, 1.01])
def test_confidence_score_rejects_values_outside_zero_to_one(
    proposal_db: sqlite3.Connection,
    confidence_score: float,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        proposal_db.execute(
            """
            INSERT INTO parser_proposal_field_evidence (
              parser_output_id,
              field_name,
              confidence_score
            )
            VALUES (?, ?, ?)
            """,
            (1, "amount", confidence_score),
        )


def test_foreign_key_enforcement_rejects_missing_parser_output(
    proposal_db: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        proposal_db.execute(
            """
            INSERT INTO parser_proposal_events (
              parser_output_id,
              to_status,
              event_type,
              actor_type
            )
            VALUES (?, ?, ?, ?)
            """,
            (999, "confirmed", "confirmed", "test"),
        )


def test_confirmation_audit_rows_do_not_create_transactions(
    proposal_db: sqlite3.Connection,
) -> None:
    before = transaction_count(proposal_db)

    proposal_db.execute(
        """
        INSERT INTO parser_proposal_events (
          parser_output_id,
          from_status,
          to_status,
          event_type,
          actor_type
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (1, "parsed_pending_confirmation", "confirmed", "confirmed", "test"),
    )
    proposal_db.execute(
        """
        INSERT INTO parser_proposal_confirmations (
          parser_output_id,
          decision,
          decided_by
        )
        VALUES (?, ?, ?)
        """,
        (1, "confirmed", "pytest"),
    )
    proposal_db.execute(
        """
        INSERT INTO parser_proposal_field_evidence (
          parser_output_id,
          field_name,
          proposed_value,
          confidence_score,
          evidence_source_type
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (1, "amount", "12.50", 0.75, "raw_input"),
    )

    assert before == 0
    assert transaction_count(proposal_db) == 0


def test_migration_is_applied_to_temporary_database_only(
    proposal_db: sqlite3.Connection,
    temp_db_path: Path,
) -> None:
    database_path = proposal_db.execute("PRAGMA database_list").fetchone()["file"]

    assert database_path
    assert Path(database_path) == temp_db_path


def transaction_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM transactions").fetchone()
    return row["count"]
