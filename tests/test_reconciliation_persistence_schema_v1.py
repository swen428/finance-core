"""Tests for Reconciliation Persistence Schema v1.

Covers:
  1. Migration applies cleanly on top of existing migrations.
  2. All four new tables exist.
  3. Required columns exist with correct types and constraints.
  4. Foreign keys are present where expected.
  5. Indexes exist for important query paths.
  6. statement_transactions supports both transaction_date and posted_date.
  7. reconciliation_match_results can store match_status, reason_codes_json,
     evidence_json, and needs_review.
  8. No live database is touched.
  9. Existing migrations still apply in order with the new migration.
 10. Minimal insert flow across all four tables in a temporary DB only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import sqlite3

# Uses shared fixtures from conftest.py:
#   migrated_temp_db_connection -- temp DB with all migrations applied
#   temp_db_path               -- path to the temp DB file

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"


# ---------------------------------------------------------------------------
# 1. Migration applies cleanly on top of existing migrations
# ---------------------------------------------------------------------------


def test_migration_006_applies_cleanly(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """All migrations including 006 should apply without errors."""
    tables = migrated_temp_db_connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    table_names = {row["name"] for row in tables}
    assert "statement_import_batches" in table_names
    assert "statement_transactions" in table_names
    assert "reconciliation_runs" in table_names
    assert "reconciliation_match_results" in table_names


# ---------------------------------------------------------------------------
# 2. All four new tables exist
# ---------------------------------------------------------------------------

EXPECTED_TABLES = {
    "statement_import_batches",
    "statement_transactions",
    "reconciliation_runs",
    "reconciliation_match_results",
}


def test_all_reconciliation_tables_exist(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    tables = migrated_temp_db_connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    table_names = {row["name"] for row in tables}
    assert EXPECTED_TABLES <= table_names


# ---------------------------------------------------------------------------
# 3. Required columns exist with correct NOT NULL constraints
# ---------------------------------------------------------------------------


def _get_columns(db: "sqlite3.Connection", table: str) -> dict[str, dict]:
    rows = db.execute(f"PRAGMA table_info('{table}')").fetchall()
    return {row["name"]: dict(row) for row in rows}


def test_statement_import_batches_columns(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    cols = _get_columns(migrated_temp_db_connection, "statement_import_batches")
    assert cols["public_id"]["notnull"] == 1
    assert cols["source_type"]["notnull"] == 1
    assert cols["source_type"]["type"] == "TEXT"
    assert cols["account_id"]["notnull"] == 0  # nullable
    assert cols["account_name"]["notnull"] == 0
    assert cols["statement_period_start"]["notnull"] == 0
    assert cols["statement_period_end"]["notnull"] == 0
    assert cols["currency"]["notnull"] == 0
    assert cols["source_file_path"]["notnull"] == 0
    assert cols["source_file_hash"]["notnull"] == 0
    assert cols["imported_at"]["notnull"] == 1


def test_statement_transactions_columns(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    cols = _get_columns(migrated_temp_db_connection, "statement_transactions")
    assert cols["batch_id"]["notnull"] == 1
    assert cols["merchant_raw"]["notnull"] == 1
    assert cols["amount"]["notnull"] == 1
    assert cols["currency"]["notnull"] == 1
    assert cols["amount"]["type"] == "NUMERIC"
    assert cols["transaction_date"]["notnull"] == 0
    assert cols["posted_date"]["notnull"] == 0
    assert cols["merchant_normalized"]["notnull"] == 0
    assert cols["raw_row_payload_json"]["notnull"] == 0
    assert cols["statement_row_reference"]["notnull"] == 0


def test_reconciliation_runs_columns(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    cols = _get_columns(migrated_temp_db_connection, "reconciliation_runs")
    assert cols["run_status"]["notnull"] == 1
    assert cols["started_at"]["notnull"] == 1
    assert cols["batch_id"]["notnull"] == 0  # nullable
    assert cols["completed_at"]["notnull"] == 0


def test_reconciliation_match_results_columns(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    cols = _get_columns(migrated_temp_db_connection, "reconciliation_match_results")
    assert cols["run_id"]["notnull"] == 1
    assert cols["statement_transaction_id"]["notnull"] == 1
    assert cols["match_status"]["notnull"] == 1
    assert cols["reason_codes_json"]["notnull"] == 1
    assert cols["evidence_json"]["notnull"] == 1
    assert cols["needs_review"]["notnull"] == 1
    assert cols["needs_review"]["type"] == "INTEGER"


# ---------------------------------------------------------------------------
# 4. Foreign keys are present
# ---------------------------------------------------------------------------


def _get_foreign_keys(db: "sqlite3.Connection", table: str) -> list[dict]:
    return db.execute(f"PRAGMA foreign_key_list('{table}')").fetchall()


def test_foreign_keys_statement_import_batches(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    fks = _get_foreign_keys(migrated_temp_db_connection, "statement_import_batches")
    fk_tables = {fk["table"] for fk in fks}
    assert "accounts" in fk_tables


def test_foreign_keys_statement_transactions(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    fks = _get_foreign_keys(migrated_temp_db_connection, "statement_transactions")
    fk_tables = {fk["table"] for fk in fks}
    assert "statement_import_batches" in fk_tables
    assert "accounts" in fk_tables


def test_foreign_keys_reconciliation_runs(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    fks = _get_foreign_keys(migrated_temp_db_connection, "reconciliation_runs")
    fk_tables = {fk["table"] for fk in fks}
    assert "statement_import_batches" in fk_tables


def test_foreign_keys_reconciliation_match_results(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    fks = _get_foreign_keys(migrated_temp_db_connection, "reconciliation_match_results")
    fk_tables = {fk["table"] for fk in fks}
    assert "reconciliation_runs" in fk_tables
    assert "statement_transactions" in fk_tables


def test_foreign_key_enforcement_rejects_orphan_transaction(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """statement_transactions with non-existent batch_id should fail."""
    with pytest.raises(migrated_temp_db_connection.IntegrityError):
        migrated_temp_db_connection.execute(
            """
            INSERT INTO statement_transactions (
              public_id, batch_id, merchant_raw, amount, currency
            )
            VALUES ('stmt-orphan', 99999, 'Merchant', 10.00, 'SGD')
            """
        )


# ---------------------------------------------------------------------------
# 5. Indexes exist for important query paths
# ---------------------------------------------------------------------------


EXPECTED_INDEXES = {
    # statement_import_batches
    "idx_import_batches_source_type",
    "idx_import_batches_account_id",
    "idx_import_batches_file_hash",
    "idx_import_batches_period",
    # statement_transactions
    "idx_stmt_txns_batch_id",
    "idx_stmt_txns_transaction_date",
    "idx_stmt_txns_posted_date",
    "idx_stmt_txns_amount_currency",
    "idx_stmt_txns_account_id",
    # reconciliation_runs
    "idx_recon_runs_batch_id",
    "idx_recon_runs_status",
    # reconciliation_match_results
    "idx_match_results_run_id",
    "idx_match_results_stmt_txn_id",
    "idx_match_results_internal_candidate_id",
    "idx_match_results_match_status",
    "idx_match_results_needs_review",
}


def test_all_expected_indexes_exist(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    rows = migrated_temp_db_connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' ORDER BY name"
    ).fetchall()
    index_names = {row["name"] for row in rows}
    missing = EXPECTED_INDEXES - index_names
    assert not missing, f"Missing indexes: {missing}"


# ---------------------------------------------------------------------------
# 6. statement_transactions supports both dates
# ---------------------------------------------------------------------------


def test_statement_transactions_supports_both_dates(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    _seed_minimal_batch(migrated_temp_db_connection)
    batch_id = _batch_id(migrated_temp_db_connection, "batch-dates")

    migrated_temp_db_connection.execute(
        """
        INSERT INTO statement_transactions (
          public_id, batch_id, merchant_raw, amount, currency,
          transaction_date, posted_date
        )
        VALUES ('stmt-dates', ?, 'Merchant', 10.00, 'SGD',
                '2024-12-01', '2024-12-03')
        """,
        (batch_id,),
    )
    migrated_temp_db_connection.commit()

    row = migrated_temp_db_connection.execute(
        "SELECT transaction_date, posted_date "
        "FROM statement_transactions WHERE public_id = 'stmt-dates'"
    ).fetchone()
    assert row["transaction_date"] == "2024-12-01"
    assert row["posted_date"] == "2024-12-03"


def test_statement_transactions_null_dates_allowed(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    _seed_minimal_batch(migrated_temp_db_connection)
    batch_id = _batch_id(migrated_temp_db_connection, "batch-dates")

    migrated_temp_db_connection.execute(
        """
        INSERT INTO statement_transactions (
          public_id, batch_id, merchant_raw, amount, currency
        )
        VALUES ('stmt-no-dates', ?, 'Merchant', 10.00, 'SGD')
        """,
        (batch_id,),
    )
    migrated_temp_db_connection.commit()

    row = migrated_temp_db_connection.execute(
        "SELECT transaction_date, posted_date "
        "FROM statement_transactions WHERE public_id = 'stmt-no-dates'"
    ).fetchone()
    assert row["transaction_date"] is None
    assert row["posted_date"] is None


# ---------------------------------------------------------------------------
# 7. reconciliation_match_results stores JSON fields and needs_review
# ---------------------------------------------------------------------------


def test_match_results_stores_reason_codes_and_evidence(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    _seed_minimal_flow(migrated_temp_db_connection)

    result_id = "match-result-001"
    reason_codes = json.dumps(["exact_amount_match", "same_currency"])
    evidence = json.dumps(
        {
            "statement_amount": "10.00",
            "candidate_amount": "10.00",
            "statement_currency": "SGD",
            "candidate_currency": "SGD",
            "date_delta_days": 1,
            "date_tolerance_days": 3,
            "merchant_similarity": 1.0,
            "candidate_count": 3,
        }
    )

    migrated_temp_db_connection.execute(
        """
        INSERT INTO reconciliation_match_results (
          public_id, run_id, statement_transaction_id, internal_candidate_id,
          match_status, reason_codes_json, evidence_json,
          amount_delta, date_delta_days, merchant_similarity, needs_review
        )
        VALUES (
          ?, (SELECT id FROM reconciliation_runs WHERE public_id = 'run-flow'),
          (SELECT id FROM statement_transactions WHERE public_id = 'stmt-flow'),
          'candidate-001', 'matched', ?, ?, 0.0, 1, 1.0, 0
        )
        """,
        (result_id, reason_codes, evidence),
    )
    migrated_temp_db_connection.commit()

    row = migrated_temp_db_connection.execute(
        """
        SELECT match_status, reason_codes_json, evidence_json, needs_review,
               amount_delta, date_delta_days, merchant_similarity
        FROM reconciliation_match_results WHERE public_id = ?
        """,
        (result_id,),
    ).fetchone()

    assert row["match_status"] == "matched"
    assert json.loads(row["reason_codes_json"]) == [
        "exact_amount_match",
        "same_currency",
    ]
    parsed_evidence = json.loads(row["evidence_json"])
    assert parsed_evidence["statement_amount"] == "10.00"
    assert row["needs_review"] == 0
    assert row["amount_delta"] == 0.0
    assert row["date_delta_days"] == 1
    assert row["merchant_similarity"] == 1.0


def test_match_results_needs_review_flag(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    _seed_minimal_flow(migrated_temp_db_connection)

    migrated_temp_db_connection.execute(
        """
        INSERT INTO reconciliation_match_results (
          public_id, run_id, statement_transaction_id,
          match_status, needs_review
        )
        VALUES (
          'match-needs-review',
          (SELECT id FROM reconciliation_runs WHERE public_id = 'run-flow'),
          (SELECT id FROM statement_transactions WHERE public_id = 'stmt-flow'),
          'needs_review', 1
        )
        """
    )
    migrated_temp_db_connection.commit()

    row = migrated_temp_db_connection.execute(
        "SELECT match_status, needs_review "
        "FROM reconciliation_match_results WHERE public_id = 'match-needs-review'"
    ).fetchone()
    assert row["match_status"] == "needs_review"
    assert row["needs_review"] == 1


# ---------------------------------------------------------------------------
# 8. No live database is touched
# ---------------------------------------------------------------------------


def test_temp_db_is_not_live_db(
    migrated_temp_db_connection: "sqlite3.Connection", temp_db_path: Path
) -> None:
    database_path = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()["file"]
    assert Path(database_path) == temp_db_path
    assert Path(database_path) != LIVE_DB_PATH


# ---------------------------------------------------------------------------
# 9. Existing migrations still apply in order
# ---------------------------------------------------------------------------


def test_existing_migrations_still_apply_with_006(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """Verify that core tables from earlier migrations exist alongside the new ones."""
    tables = migrated_temp_db_connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    table_names = {row["name"] for row in tables}

    # Core tables from 001
    assert "accounts" in table_names
    assert "transactions" in table_names
    assert "participants" in table_names
    assert "statement_batches" in table_names
    assert "parser_outputs" in table_names

    # Table from 003
    assert "raw_intake_records" in table_names

    # Table from 004
    assert "parser_proposal_events" in table_names

    # Tables from 005
    assert "raw_intake_evidence" in table_names


# ---------------------------------------------------------------------------
# 10. Minimal insert flow across all four tables
# ---------------------------------------------------------------------------


def test_minimal_insert_flow(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """End-to-end insert: batch -> transaction -> run -> match result."""
    _seed_minimal_flow(migrated_temp_db_connection)

    # Verify batch
    batch = migrated_temp_db_connection.execute(
        "SELECT public_id, source_type FROM statement_import_batches WHERE public_id = 'batch-flow'"
    ).fetchone()
    assert batch["source_type"] == "bank_statement"

    # Verify transaction
    stmt = migrated_temp_db_connection.execute(
        "SELECT public_id, merchant_raw, amount, currency "
        "FROM statement_transactions WHERE public_id = 'stmt-flow'"
    ).fetchone()
    assert stmt["merchant_raw"] == "Apple"
    assert stmt["amount"] == 10.00
    assert stmt["currency"] == "SGD"

    # Verify run
    run = migrated_temp_db_connection.execute(
        "SELECT public_id, run_status FROM reconciliation_runs WHERE public_id = 'run-flow'"
    ).fetchone()
    assert run["run_status"] == "completed"

    # Verify match result
    match_row = migrated_temp_db_connection.execute(
        "SELECT public_id, match_status "
        "FROM reconciliation_match_results WHERE public_id = 'match-flow'"
    ).fetchone()
    assert match_row["match_status"] == "matched"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_minimal_batch(db: "sqlite3.Connection") -> None:
    """Insert a minimal import batch (no account FK needed)."""
    db.execute(
        """
        INSERT OR IGNORE INTO statement_import_batches (
          public_id, source_type
        )
        VALUES ('batch-dates', 'bank_statement')
        """
    )
    db.commit()


def _seed_minimal_flow(db: "sqlite3.Connection") -> None:
    """Insert one row in each of the four tables for end-to-end testing."""
    db.execute(
        """
        INSERT OR IGNORE INTO statement_import_batches (
          public_id, source_type, currency
        )
        VALUES ('batch-flow', 'bank_statement', 'SGD')
        """
    )
    batch_id = db.execute(
        "SELECT id FROM statement_import_batches WHERE public_id = 'batch-flow'"
    ).fetchone()["id"]

    db.execute(
        """
        INSERT OR IGNORE INTO statement_transactions (
          public_id, batch_id, transaction_date, posted_date,
          merchant_raw, merchant_normalized, amount, currency,
          raw_row_payload_json
        )
        VALUES (
          'stmt-flow', ?, '2024-12-01', '2024-12-02',
          'Apple', 'apple', 10.00, 'SGD',
          '{"source_row":1,"raw_text":"Apple SGD 10.00"}'
        )
        """,
        (batch_id,),
    )

    db.execute(
        """
        INSERT OR IGNORE INTO reconciliation_runs (
          public_id, batch_id, run_status, matcher_version, completed_at
        )
        VALUES ('run-flow', ?, 'completed', 'v1', CURRENT_TIMESTAMP)
        """,
        (batch_id,),
    )
    run_id = db.execute(
        "SELECT id FROM reconciliation_runs WHERE public_id = 'run-flow'"
    ).fetchone()["id"]

    db.execute(
        """
        INSERT OR IGNORE INTO reconciliation_match_results (
          public_id, run_id, statement_transaction_id, internal_candidate_id,
          match_status, reason_codes_json, evidence_json, needs_review
        )
        VALUES (
          'match-flow', ?,
          (SELECT id FROM statement_transactions WHERE public_id = 'stmt-flow'),
          'candidate-001', 'matched',
          '["exact_amount_match","same_currency"]',
          '{"amount":"10.00","currency":"SGD"}',
          0
        )
        """,
        (run_id,),
    )

    db.commit()


def _batch_id(db: "sqlite3.Connection", public_id: str) -> int:
    row = db.execute(
        "SELECT id FROM statement_import_batches WHERE public_id = ?", (public_id,)
    ).fetchone()
    assert row is not None, f"Batch '{public_id}' not found"
    return row["id"]
