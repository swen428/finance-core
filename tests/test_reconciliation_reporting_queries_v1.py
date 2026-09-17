"""Tests for Reconciliation Reporting Queries v1.

Covers:
  1. run_summary.sql executes without error
  2. review_queue.sql executes without error
  3. unmatched_rows.sql executes without error
  4. status_breakdown.sql executes without error
  5. candidate_audit.sql executes without error
  6. run_summary returns correct counts for seeded data
  7. review_queue returns only needs_review rows
  8. unmatched_rows excludes matched rows
  9. status_breakdown aggregates across statuses
 10. candidate_audit includes all expected fields
 11. queries are read-only (no INSERT/UPDATE/DELETE)
 12. queries do not touch database/finance.db
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from finance_core.reconciliation.models import (
    InternalCandidate,
    MatchEvidence,
    MatchResult,
    MatchStatus,
    ReasonCode,
)
from finance_core.reconciliation.repository import ReconciliationRepository

if TYPE_CHECKING:
    import sqlite3

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"
QUERIES_DIR = REPO_ROOT / "database" / "queries" / "reconciliation"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_batch(conn: sqlite3.Connection, public_id: str = "batch-sql") -> int:
    conn.execute(
        """
        INSERT INTO statement_import_batches (public_id, source_type, currency)
        VALUES (?, 'bank_statement', 'SGD')
        """,
        (public_id,),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM statement_import_batches WHERE public_id = ?", (public_id,)
    ).fetchone()["id"]


def _seed_stmt(
    conn: sqlite3.Connection,
    public_id: str,
    batch_id: int,
    merchant_raw: str = "Apple",
    amount: Decimal = Decimal("29.90"),
    currency: str = "SGD",
    transaction_date: str | None = "2024-12-01",
) -> int:
    conn.execute(
        """
        INSERT INTO statement_transactions (
          public_id, batch_id, merchant_raw, amount, currency, transaction_date
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (public_id, batch_id, merchant_raw, str(amount), currency, transaction_date),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM statement_transactions WHERE public_id = ?", (public_id,)
    ).fetchone()["id"]


def _make_repo(conn: sqlite3.Connection) -> ReconciliationRepository:
    return ReconciliationRepository(conn)


def _make_match_result(status: MatchStatus) -> MatchResult:
    if status == MatchStatus.MATCHED:
        return MatchResult(
            status=status,
            reasons=(ReasonCode.EXACT_AMOUNT_MATCH,),
            evidence=MatchEvidence(
                statement_amount=Decimal("29.90"),
                candidate_amount=Decimal("29.90"),
                merchant_similarity=1.0,
                candidate_count=1,
            ),
            best_candidate=InternalCandidate(
                internal_id="cand-001",
                transaction_date=date(2024, 12, 1),
                merchant="Apple",
                amount=Decimal("29.90"),
                currency="SGD",
            ),
        )
    return MatchResult(
        status=status,
        reasons=(ReasonCode.NO_CANDIDATE_FOUND,),
        evidence=MatchEvidence(candidate_count=0),
    )


def _seeded_run(conn: sqlite3.Connection) -> int:
    """Seed a run with 3 match results: matched, no_match, amount_mismatch."""
    repo = _make_repo(conn)
    batch_id = _seed_batch(conn, "batch-sql-seeded")
    stmts = [
        _seed_stmt(conn, "stmt-sql-1", batch_id),
        _seed_stmt(conn, "stmt-sql-2", batch_id),
        _seed_stmt(conn, "stmt-sql-3", batch_id),
    ]
    run_id = repo.create_reconciliation_run(public_id="run-sql-seeded")
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmts[0],
        match_result=_make_match_result(MatchStatus.MATCHED),
    )
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmts[1],
        match_result=_make_match_result(MatchStatus.NO_MATCH),
    )
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmts[2],
        match_result=_make_match_result(MatchStatus.AMOUNT_MISMATCH),
    )
    return run_id


# ---------------------------------------------------------------------------
# 1-5. All queries execute without error
# ---------------------------------------------------------------------------


def test_run_summary_sql_executes(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seeded_run(migrated_temp_db_connection)
    sql = (QUERIES_DIR / "run_summary.sql").read_text(encoding="utf-8")
    rows = migrated_temp_db_connection.execute(sql).fetchall()
    assert len(rows) >= 1


def test_review_queue_sql_executes(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seeded_run(migrated_temp_db_connection)
    sql = (QUERIES_DIR / "review_queue.sql").read_text(encoding="utf-8")
    rows = migrated_temp_db_connection.execute(sql).fetchall()
    # no_match and amount_mismatch -> needs_review = 1
    assert len(rows) == 2


def test_unmatched_rows_sql_executes(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seeded_run(migrated_temp_db_connection)
    sql = (QUERIES_DIR / "unmatched_rows.sql").read_text(encoding="utf-8")
    rows = migrated_temp_db_connection.execute(sql).fetchall()
    # matched excluded; no_match and amount_mismatch remain
    assert len(rows) == 2


def test_status_breakdown_sql_executes(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seeded_run(migrated_temp_db_connection)
    sql = (QUERIES_DIR / "status_breakdown.sql").read_text(encoding="utf-8")
    rows = migrated_temp_db_connection.execute(sql).fetchall()
    # 3 distinct statuses
    assert len(rows) == 3


def test_candidate_audit_sql_executes(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seeded_run(migrated_temp_db_connection)
    sql = (QUERIES_DIR / "candidate_audit.sql").read_text(encoding="utf-8")
    rows = migrated_temp_db_connection.execute(sql).fetchall()
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# 6. run_summary returns correct counts
# ---------------------------------------------------------------------------


def test_run_summary_correct_counts(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seeded_run(migrated_temp_db_connection)
    sql = (QUERIES_DIR / "run_summary.sql").read_text(encoding="utf-8")
    row = migrated_temp_db_connection.execute(sql).fetchone()

    assert row["total_match_results"] == 3
    assert row["matched_count"] == 1
    assert row["no_match_count"] == 1
    assert row["amount_mismatch_count"] == 1
    assert row["needs_review_count"] == 2
    assert row["match_rate_pct"] is not None


# ---------------------------------------------------------------------------
# 7. review_queue returns only needs_review rows
# ---------------------------------------------------------------------------


def test_review_queue_only_needs_review(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seeded_run(migrated_temp_db_connection)
    sql = (QUERIES_DIR / "review_queue.sql").read_text(encoding="utf-8")
    rows = migrated_temp_db_connection.execute(sql).fetchall()

    for row in rows:
        assert row["needs_review"] == 1
        assert row["match_status"] != "matched"


# ---------------------------------------------------------------------------
# 8. unmatched_rows excludes matched
# ---------------------------------------------------------------------------


def test_unmatched_rows_excludes_matched(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seeded_run(migrated_temp_db_connection)
    sql = (QUERIES_DIR / "unmatched_rows.sql").read_text(encoding="utf-8")
    rows = migrated_temp_db_connection.execute(sql).fetchall()

    for row in rows:
        assert row["match_status"] != "matched"


# ---------------------------------------------------------------------------
# 9. status_breakdown aggregates across statuses
# ---------------------------------------------------------------------------


def test_status_breakdown_aggregates(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seeded_run(migrated_temp_db_connection)
    sql = (QUERIES_DIR / "status_breakdown.sql").read_text(encoding="utf-8")
    rows = migrated_temp_db_connection.execute(sql).fetchall()

    statuses = {r["match_status"] for r in rows}
    assert "matched" in statuses
    assert "no_match" in statuses
    assert "amount_mismatch" in statuses

    for row in rows:
        assert row["status_count"] >= 1
        assert row["status_pct"] is not None


# ---------------------------------------------------------------------------
# 10. candidate_audit includes all expected fields
# ---------------------------------------------------------------------------


def test_candidate_audit_fields(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seeded_run(migrated_temp_db_connection)
    sql = (QUERIES_DIR / "candidate_audit.sql").read_text(encoding="utf-8")
    rows = migrated_temp_db_connection.execute(sql).fetchall()

    for row in rows:
        assert row["match_result_id"] is not None
        assert row["match_public_id"] is not None
        assert row["run_id"] is not None
        assert row["run_public_id"] is not None
        assert row["statement_transaction_id"] is not None
        assert row["match_status"] is not None
        assert row["reason_codes_json"] is not None
        assert row["evidence_json"] is not None
        # Verify JSON fields are valid
        json.loads(row["reason_codes_json"])
        json.loads(row["evidence_json"])


# ---------------------------------------------------------------------------
# 11. queries are read-only
# ---------------------------------------------------------------------------


def test_queries_are_read_only(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    _seeded_run(migrated_temp_db_connection)

    before = migrated_temp_db_connection.execute(
        "SELECT COUNT(*) AS cnt FROM reconciliation_match_results"
    ).fetchone()["cnt"]

    for sql_file in QUERIES_DIR.glob("*.sql"):
        sql = sql_file.read_text(encoding="utf-8")
        migrated_temp_db_connection.execute(sql).fetchall()

    after = migrated_temp_db_connection.execute(
        "SELECT COUNT(*) AS cnt FROM reconciliation_match_results"
    ).fetchone()["cnt"]

    assert after == before


# ---------------------------------------------------------------------------
# 12. queries do not touch database/finance.db
# ---------------------------------------------------------------------------


def test_queries_do_not_touch_live_db(
    migrated_temp_db_connection: sqlite3.Connection,
    temp_db_path: Path,
) -> None:
    database_path = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()["file"]
    assert Path(database_path) == temp_db_path
    assert Path(database_path) != LIVE_DB_PATH

    _seeded_run(migrated_temp_db_connection)
    sql = (QUERIES_DIR / "run_summary.sql").read_text(encoding="utf-8")
    rows = migrated_temp_db_connection.execute(sql).fetchall()
    assert len(rows) >= 1
