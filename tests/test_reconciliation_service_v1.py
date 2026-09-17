"""Tests for Reconciliation Service Runtime v1.

Covers:
  1. service runs reconciliation for a batch with one exact match
  2. service persists matched result
  3. service summary has correct matched_count
  4. service handles no_match and persists needs_review
  5. service handles amount mismatch and persists amount_mismatch
  6. service handles duplicate/ambiguous candidates without silently choosing
  7. service creates run_status started then completed
  8. service marks run failed when matcher/repository operation raises
  9. service summary status counts are deterministic
 10. service does not touch live DB
 11. posted_date fallback still persists correct reason code
 12. evidence_json includes date_delta_days / merchant_similarity
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from finance_core.reconciliation.models import (
    InternalCandidate,
    StatementAmountDirection,
)
from finance_core.reconciliation.repository import ReconciliationRepository
from finance_core.reconciliation.service import (
    ReconciliationService,
)

if TYPE_CHECKING:
    import sqlite3

if not TYPE_CHECKING:
    from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_batch(
    conn: "sqlite3.Connection",
    public_id: str = "batch-svc",
    source_type: str = "bank_statement",
) -> int:
    conn.execute(
        """
        INSERT INTO statement_import_batches (public_id, source_type, currency)
        VALUES (?, ?, 'SGD')
        """,
        (public_id, source_type),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM statement_import_batches WHERE public_id = ?",
        (public_id,),
    ).fetchone()["id"]


def _seed_stmt(
    conn: "sqlite3.Connection",
    public_id: str,
    batch_id: int,
    merchant_raw: str = "Apple",
    amount: Decimal = Decimal("29.90"),
    currency: str = "SGD",
    transaction_date: str | None = "2024-12-01",
    posted_date: str | None = None,
) -> int:
    conn.execute(
        """
        INSERT INTO statement_transactions (
          public_id, batch_id, merchant_raw, amount, currency,
          transaction_date, posted_date, amount_direction, raw_amount
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            public_id,
            batch_id,
            merchant_raw,
            str(amount),
            currency,
            transaction_date,
            posted_date,
            StatementAmountDirection.DEBIT.value,
            str(amount),
        ),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM statement_transactions WHERE public_id = ?",
        (public_id,),
    ).fetchone()["id"]


def _make_candidate(
    internal_id: str,
    merchant: str = "Apple",
    amount: Decimal = Decimal("29.90"),
    currency: str = "SGD",
    transaction_date: date = date(2024, 12, 1),
) -> InternalCandidate:
    return InternalCandidate(
        internal_id=internal_id,
        transaction_date=transaction_date,
        merchant=merchant,
        amount=amount,
        currency=currency,
        source_type="expense",
    )


def _make_svc(conn: "sqlite3.Connection") -> ReconciliationService:
    return ReconciliationService(ReconciliationRepository(conn))


# ---------------------------------------------------------------------------
# 1. service runs reconciliation for a batch with one exact match
# ---------------------------------------------------------------------------


def test_run_reconciliation_exact_match(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    svc = _make_svc(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-exact")
    _seed_stmt(migrated_temp_db_connection, "stmt-exact", batch_id)

    candidates = [_make_candidate("cand-001")]
    summary = svc.run_reconciliation_for_batch(
        batch_id=batch_id,
        candidates=candidates,
        run_public_id="run-exact",
        matcher_version="v1",
    )

    assert summary.total_statement_transactions == 1
    assert summary.matched_count == 1
    assert summary.failed is False


# ---------------------------------------------------------------------------
# 2. service persists matched result
# ---------------------------------------------------------------------------


def test_service_persists_matched_result(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    svc = _make_svc(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-persist")
    _seed_stmt(migrated_temp_db_connection, "stmt-persist", batch_id)

    candidates = [_make_candidate("cand-001")]
    svc.run_reconciliation_for_batch(
        batch_id=batch_id,
        candidates=candidates,
        run_public_id="run-persist",
    )

    results = migrated_temp_db_connection.execute(
        """
        SELECT * FROM reconciliation_match_results
        WHERE run_id = (SELECT id FROM reconciliation_runs WHERE public_id = 'run-persist')
        """
    ).fetchall()
    assert len(results) == 1
    assert results[0]["match_status"] == "matched"
    assert results[0]["needs_review"] == 0


# ---------------------------------------------------------------------------
# 3. service summary has correct matched_count
# ---------------------------------------------------------------------------


def test_service_summary_matched_count(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    svc = _make_svc(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-count")
    _seed_stmt(migrated_temp_db_connection, "stmt-count-1", batch_id)
    _seed_stmt(
        migrated_temp_db_connection,
        "stmt-count-2",
        batch_id,
        merchant_raw="Apple",
        amount=Decimal("29.90"),
    )

    candidates = [_make_candidate("cand-001")]
    summary = svc.run_reconciliation_for_batch(
        batch_id=batch_id,
        candidates=candidates,
        run_public_id="run-count",
    )

    assert summary.total_statement_transactions == 2
    assert summary.matched_count == 0
    assert summary.possible_duplicate_count == 2
    assert summary.needs_review_count == 2

    results = migrated_temp_db_connection.execute(
        """
        SELECT match_status, needs_review
        FROM reconciliation_match_results
        WHERE run_id = (SELECT id FROM reconciliation_runs WHERE public_id = 'run-count')
        ORDER BY id ASC
        """
    ).fetchall()
    assert [row["match_status"] for row in results] == [
        "possible_duplicate",
        "possible_duplicate",
    ]
    assert all(row["needs_review"] == 1 for row in results)


# ---------------------------------------------------------------------------
# 4. service handles no_match and persists needs_review
# ---------------------------------------------------------------------------


def test_service_no_match_persists_needs_review(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    svc = _make_svc(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-nomatch")
    _seed_stmt(migrated_temp_db_connection, "stmt-nomatch", batch_id)

    # No candidates at all -> should be no_match
    summary = svc.run_reconciliation_for_batch(
        batch_id=batch_id,
        candidates=[],
        run_public_id="run-nomatch",
    )

    assert summary.no_match_count == 1
    assert summary.needs_review_count == 1

    results = migrated_temp_db_connection.execute(
        """
        SELECT * FROM reconciliation_match_results
        WHERE run_id = (SELECT id FROM reconciliation_runs WHERE public_id = 'run-nomatch')
        """
    ).fetchall()
    assert len(results) == 1
    assert results[0]["match_status"] == "no_match"
    assert results[0]["needs_review"] == 1


# ---------------------------------------------------------------------------
# 5. service handles amount mismatch and persists amount_mismatch
# ---------------------------------------------------------------------------


def test_service_amount_mismatch(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    svc = _make_svc(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-amt")
    _seed_stmt(migrated_temp_db_connection, "stmt-amt", batch_id, amount=Decimal("50.00"))

    candidates = [_make_candidate("cand-001", amount=Decimal("29.90"))]
    summary = svc.run_reconciliation_for_batch(
        batch_id=batch_id,
        candidates=candidates,
        run_public_id="run-amt",
    )

    assert summary.amount_mismatch_count == 1
    assert summary.needs_review_count == 1

    results = migrated_temp_db_connection.execute(
        """
        SELECT * FROM reconciliation_match_results
        WHERE run_id = (SELECT id FROM reconciliation_runs WHERE public_id = 'run-amt')
        """
    ).fetchall()
    assert results[0]["match_status"] == "amount_mismatch"
    assert results[0]["needs_review"] == 1


# ---------------------------------------------------------------------------
# 6. service handles duplicate/ambiguous candidates
# ---------------------------------------------------------------------------


def test_service_ambiguous_candidates(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    svc = _make_svc(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-amb")
    _seed_stmt(migrated_temp_db_connection, "stmt-amb", batch_id)

    # Two different merchants -> ambiguous
    candidates = [
        _make_candidate("cand-a", merchant="Apple Store"),
        _make_candidate("cand-b", merchant="Apple Online"),
    ]
    summary = svc.run_reconciliation_for_batch(
        batch_id=batch_id,
        candidates=candidates,
        run_public_id="run-amb",
    )

    assert summary.ambiguous_count == 1
    assert summary.matched_count == 0


# ---------------------------------------------------------------------------
# 7. service creates run_status started then completed
# ---------------------------------------------------------------------------


def test_service_creates_run_started_then_completed(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    svc = _make_svc(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-started")
    _seed_stmt(migrated_temp_db_connection, "stmt-started", batch_id)

    svc.run_reconciliation_for_batch(
        batch_id=batch_id,
        candidates=[_make_candidate("cand-001")],
        run_public_id="run-started",
    )

    run = migrated_temp_db_connection.execute(
        "SELECT run_status, completed_at FROM reconciliation_runs WHERE public_id = 'run-started'"
    ).fetchone()
    assert run["run_status"] == "completed"
    assert run["completed_at"] is not None


# ---------------------------------------------------------------------------
# 8. service marks run failed when matcher/repository raises
# ---------------------------------------------------------------------------


def test_service_marks_run_failed_on_error(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    svc = _make_svc(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-fail")
    _seed_stmt(migrated_temp_db_connection, "stmt-fail", batch_id)

    # Patch save_match_result to raise, triggering the fail path.
    # The service must re-raise the original exception after marking the run failed.
    with patch.object(svc._repo, "save_match_result", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            svc.run_reconciliation_for_batch(
                batch_id=batch_id,
                candidates=[_make_candidate("cand-001")],
                run_public_id="run-fail",
            )

    # After re-raise, run should still be marked failed.
    run = migrated_temp_db_connection.execute(
        "SELECT run_status, completed_at FROM reconciliation_runs WHERE public_id = 'run-fail'"
    ).fetchone()
    assert run is not None
    assert run["run_status"] == "failed"
    assert run["completed_at"] is not None


# ---------------------------------------------------------------------------
# 9. service summary status counts are deterministic
# ---------------------------------------------------------------------------


def test_service_summary_deterministic(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    svc = _make_svc(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-det")
    _seed_stmt(
        migrated_temp_db_connection,
        "stmt-det-1",
        batch_id,
        merchant_raw="Apple",
        amount=Decimal("29.90"),
    )
    _seed_stmt(
        migrated_temp_db_connection,
        "stmt-det-2",
        batch_id,
        merchant_raw="Netflix",
        amount=Decimal("15.99"),
    )

    candidates = [_make_candidate("cand-001", merchant="Apple", amount=Decimal("29.90"))]
    summary1 = svc.run_reconciliation_for_batch(
        batch_id=batch_id,
        candidates=candidates,
        run_public_id="run-det-1",
    )
    # identical run
    svc2 = _make_svc(migrated_temp_db_connection)
    summary2 = svc2.run_reconciliation_for_batch(
        batch_id=batch_id,
        candidates=candidates,
        run_public_id="run-det-2",
    )

    assert summary1.matched_count == summary2.matched_count
    assert summary1.merchant_mismatch_count == summary2.merchant_mismatch_count
    assert summary1.match_status_counts == summary2.match_status_counts


# ---------------------------------------------------------------------------
# 13. deterministic match result public_id
# ---------------------------------------------------------------------------


def test_service_match_result_public_id_is_deterministic(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    svc = _make_svc(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-det-pid")
    _seed_stmt(migrated_temp_db_connection, "stmt-det-pid", batch_id)

    candidates = [_make_candidate("cand-001")]
    svc.run_reconciliation_for_batch(
        batch_id=batch_id,
        candidates=candidates,
        run_public_id="run-det-pid",
    )

    results = migrated_temp_db_connection.execute(
        """
        SELECT public_id FROM reconciliation_match_results
        WHERE run_id = (SELECT id FROM reconciliation_runs WHERE public_id = 'run-det-pid')
        """
    ).fetchall()
    assert len(results) == 1
    assert results[0]["public_id"] == "match-run-det-pid-stmt-det-pid"


# ---------------------------------------------------------------------------
# 14. service re-raises original exception after marking run failed
# ---------------------------------------------------------------------------


def test_service_reraises_original_error(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    svc = _make_svc(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-reraise")
    _seed_stmt(migrated_temp_db_connection, "stmt-reraise", batch_id)

    with patch.object(svc._repo, "save_match_result", side_effect=ValueError("db error")):
        with pytest.raises(ValueError, match="db error"):
            svc.run_reconciliation_for_batch(
                batch_id=batch_id,
                candidates=[_make_candidate("cand-001")],
                run_public_id="run-reraise",
            )

    # Run was marked failed.
    run = migrated_temp_db_connection.execute(
        "SELECT run_status FROM reconciliation_runs WHERE public_id = 'run-reraise'"
    ).fetchone()
    assert run["run_status"] == "failed"


def test_service_preserves_fail_run_error_context(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    svc = _make_svc(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-fail-note")
    _seed_stmt(migrated_temp_db_connection, "stmt-fail-note", batch_id)

    with (
        patch.object(svc._repo, "save_match_result", side_effect=ValueError("db error")),
        patch.object(
            svc._repo,
            "fail_reconciliation_run",
            side_effect=RuntimeError("status update failed"),
        ),
    ):
        with pytest.raises(ValueError, match="db error") as exc_info:
            svc.run_reconciliation_for_batch(
                batch_id=batch_id,
                candidates=[_make_candidate("cand-001")],
                run_public_id="run-fail-note",
            )

    notes = getattr(exc_info.value, "__notes__", [])
    assert any("status update failed" in note for note in notes)


# ---------------------------------------------------------------------------
# 15. failed runs roll back every decision atomically
# ---------------------------------------------------------------------------


def test_service_rolls_back_all_results_on_failure(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    svc = _make_svc(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-partial")
    _seed_stmt(migrated_temp_db_connection, "stmt-partial-1", batch_id)
    _seed_stmt(migrated_temp_db_connection, "stmt-partial-2", batch_id)
    _seed_stmt(migrated_temp_db_connection, "stmt-partial-3", batch_id)

    candidates = [_make_candidate("cand-001")]

    original = svc._repo.save_match_result
    call_count = [0]

    def failing_save(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 2:
            raise RuntimeError("mid-run failure")
        return original(*args, **kwargs)

    with patch.object(svc._repo, "save_match_result", side_effect=failing_save):
        with pytest.raises(RuntimeError, match="mid-run failure"):
            svc.run_reconciliation_for_batch(
                batch_id=batch_id,
                candidates=candidates,
                run_public_id="run-partial",
            )

    # A service-owned transaction prevents partial durable decisions.  The
    # separate failed run record is operational state, not a financial result.
    results = migrated_temp_db_connection.execute(
        """
        SELECT * FROM reconciliation_match_results
        WHERE run_id = (SELECT id FROM reconciliation_runs WHERE public_id = 'run-partial')
        ORDER BY id ASC
        """
    ).fetchall()
    assert results == []
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM financial_audit_events "
            "WHERE aggregate_type = 'reconciliation_match_result'"
        ).fetchone()[0]
        == 0
    )


# ---------------------------------------------------------------------------
# 10. service does not touch live DB
# ---------------------------------------------------------------------------


def test_service_does_not_touch_live_db(
    migrated_temp_db_connection: "sqlite3.Connection",
    temp_db_path: Path,
) -> None:
    database_path = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()["file"]
    assert Path(database_path) == temp_db_path
    assert Path(database_path) != LIVE_DB_PATH


# ---------------------------------------------------------------------------
# 11. posted_date fallback still persists correct reason code
# ---------------------------------------------------------------------------


def test_service_posted_date_fallback(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    svc = _make_svc(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-posted")
    _seed_stmt(
        migrated_temp_db_connection,
        "stmt-posted",
        batch_id,
        transaction_date=None,
        posted_date="2024-12-01",
    )

    candidates = [_make_candidate("cand-001", transaction_date=date(2024, 12, 1))]
    svc.run_reconciliation_for_batch(
        batch_id=batch_id,
        candidates=candidates,
        run_public_id="run-posted",
    )

    results = migrated_temp_db_connection.execute(
        """
        SELECT * FROM reconciliation_match_results
        WHERE run_id = (SELECT id FROM reconciliation_runs WHERE public_id = 'run-posted')
        """
    ).fetchall()
    reason_codes = json.loads(results[0]["reason_codes_json"])
    assert "posted_date_within_tolerance" in reason_codes


# ---------------------------------------------------------------------------
# 12. evidence_json includes date_delta_days / merchant_similarity
# ---------------------------------------------------------------------------


def test_service_evidence_includes_date_delta_and_similarity(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    svc = _make_svc(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-evidence")
    _seed_stmt(migrated_temp_db_connection, "stmt-evidence", batch_id)

    candidates = [_make_candidate("cand-001")]
    svc.run_reconciliation_for_batch(
        batch_id=batch_id,
        candidates=candidates,
        run_public_id="run-evidence",
    )

    results = migrated_temp_db_connection.execute(
        """
        SELECT * FROM reconciliation_match_results
        WHERE run_id = (SELECT id FROM reconciliation_runs WHERE public_id = 'run-evidence')
        """
    ).fetchall()
    evidence = json.loads(results[0]["evidence_json"])
    assert "date_delta_days" in evidence
    assert "merchant_similarity" in evidence
    assert results[0]["date_delta_days"] is not None
    assert results[0]["merchant_similarity"] is not None
