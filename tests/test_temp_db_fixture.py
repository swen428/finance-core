import sqlite3
from pathlib import Path

import pytest

from finance_core.reconciliation.persistence import initialize_temp_reconciliation_db

ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = ROOT / "database" / "finance.db"


@pytest.fixture()
def live_db_guard() -> tuple[int, int] | None:
    before = live_db_snapshot()

    yield before

    assert live_db_snapshot() == before


@pytest.fixture()
def guarded_migrated_temp_db_connection(
    live_db_guard: tuple[int, int] | None,
    migrated_temp_db_connection: sqlite3.Connection,
) -> tuple[sqlite3.Connection, tuple[int, int] | None]:
    return migrated_temp_db_connection, live_db_guard


def test_migrated_temp_db_fixture_is_isolated_and_migrated(
    guarded_migrated_temp_db_connection: tuple[sqlite3.Connection, tuple[int, int] | None],
    temp_db_path: Path,
) -> None:
    migrated_temp_db_connection, live_db_guard = guarded_migrated_temp_db_connection
    database_path = Path(
        migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()["file"]
    )

    assert database_path == temp_db_path
    assert database_path != LIVE_DB_PATH
    assert database_path.exists()

    if live_db_guard is None:
        assert not LIVE_DB_PATH.exists()
    else:
        assert live_db_snapshot() == live_db_guard

    table_rows = migrated_temp_db_connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name IN (
            'transactions',
            'receipt_groups',
            'raw_intake_records',
            'parser_proposal_events',
            'reconciliation_apply_batches',
            'reconciliation_apply_batch_state_transitions'
          )
        """
    ).fetchall()

    assert {row["name"] for row in table_rows} == {
        "transactions",
        "receipt_groups",
        "raw_intake_records",
        "parser_proposal_events",
        "reconciliation_apply_batches",
        "reconciliation_apply_batch_state_transitions",
    }


def test_initialize_temp_reconciliation_db_applies_full_temp_chain(tmp_path: Path) -> None:
    db_path = tmp_path / "reconciliation_helper.sqlite"

    conn = initialize_temp_reconciliation_db(db_path)
    try:
        table_rows = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name IN (
                'reconciliation_match_results',
                'reconciliation_structured_evidence',
                'reconciliation_apply_batches',
                'reconciliation_apply_batch_state_transitions'
              )
            """
        ).fetchall()
    finally:
        conn.close()

    assert {row["name"] for row in table_rows} == {
        "reconciliation_match_results",
        "reconciliation_structured_evidence",
        "reconciliation_apply_batches",
        "reconciliation_apply_batch_state_transitions",
    }


def live_db_snapshot() -> tuple[int, int] | None:
    if not LIVE_DB_PATH.exists():
        return None

    stat = LIVE_DB_PATH.stat()
    return (stat.st_size, stat.st_mtime_ns)
