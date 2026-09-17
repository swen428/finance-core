"""FIN-REM-05 raw-text intake staging-database boundary tests.

The refusal fixture has the complete production-shaped Finance schema but is
created directly, without the staging authorization record.  It is disposable
and never opens or copies ``database/finance.db``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from finance_core.intake.raw_text_repository import create_raw_intake_record, save_parser_proposal
from finance_core.intake.raw_text_service import process_raw_text_input
from finance_core.intake.telegram_text_adapter import (
    process_openclaw_telegram_text_message,
    process_telegram_text_update,
)
from finance_core.parsers.text_expense_parser import parse_text_expense
from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS, apply_migration_paths
from finance_core.staging_guard import StagingDatabaseError


@pytest.fixture()
def production_shaped_untrusted_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db_path = tmp_path / "production-shaped-untrusted.sqlite"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _telegram_update() -> dict[str, Any]:
    return {
        "update_id": 123,
        "message": {
            "message_id": 456,
            "date": 1_717_171_200,
            "chat": {"id": 789},
            "from": {"id": 42},
            "text": "Lunch SGD 12.50 at Example Cafe",
        },
    }


def _openclaw_message() -> dict[str, Any]:
    message = _telegram_update()["message"]
    assert isinstance(message, dict)
    return message


@pytest.mark.parametrize(
    "write_entry",
    [
        pytest.param(
            lambda conn: create_raw_intake_record(conn, "Lunch SGD 12.50 at Example Cafe"),
            id="repository_choke_point",
        ),
        pytest.param(
            lambda conn: process_raw_text_input(conn, "Lunch SGD 12.50 at Example Cafe"),
            id="raw_text_service",
        ),
        pytest.param(
            lambda conn: process_telegram_text_update(conn, _telegram_update()),
            id="telegram_update_adapter",
        ),
        pytest.param(
            lambda conn: process_openclaw_telegram_text_message(conn, _openclaw_message()),
            id="openclaw_message_adapter",
        ),
    ],
)
def test_public_raw_text_writers_refuse_untrusted_production_shaped_database_before_insert(
    production_shaped_untrusted_db: sqlite3.Connection,
    write_entry: Callable[[sqlite3.Connection], object],
) -> None:
    conn = production_shaped_untrusted_db

    with pytest.raises(StagingDatabaseError):
        write_entry(conn)

    assert conn.in_transaction is False
    for table in ("raw_intake_records", "raw_intake_evidence", "parser_outputs"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_repository_choke_point_accepts_authorized_staging_database(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    intake = create_raw_intake_record(
        migrated_temp_db_connection,
        "Lunch SGD 12.50 at Example Cafe",
    )

    assert intake["raw_input"] == "Lunch SGD 12.50 at Example Cafe"
    assert (
        migrated_temp_db_connection.execute("SELECT COUNT(*) FROM raw_intake_records").fetchone()[0]
        == 1
    )


def test_parser_proposal_writer_refuses_copied_staging_database_before_mutation(
    migrated_temp_db_connection: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    raw_input = "Lunch SGD 12.50 at Example Cafe"
    intake = create_raw_intake_record(migrated_temp_db_connection, raw_input)
    migrated_temp_db_connection.commit()

    copied_db_path = tmp_path / "copied-staging.sqlite"
    copied_conn = sqlite3.connect(copied_db_path)
    try:
        migrated_temp_db_connection.backup(copied_conn)
        copied_conn.row_factory = sqlite3.Row
        copied_conn.execute("PRAGMA foreign_keys = ON")
        proposal = parse_text_expense(
            raw_input,
            raw_input_reference=intake["public_id"],
            source_type=intake["source_type"],
        )
        before = {
            table: copied_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "raw_intake_records",
                "raw_intake_evidence",
                "parser_outputs",
                "parser_proposal_field_evidence",
            )
        }

        with pytest.raises(StagingDatabaseError):
            save_parser_proposal(copied_conn, intake["id"], proposal)

        assert copied_conn.in_transaction is False
        after = {
            table: copied_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
        assert after == before
        stored_intake = copied_conn.execute(
            "SELECT status, parser_output_id FROM raw_intake_records WHERE id = ?",
            (intake["id"],),
        ).fetchone()
        assert stored_intake["status"] == "pending_parse"
        assert stored_intake["parser_output_id"] is None
    finally:
        copied_conn.close()
