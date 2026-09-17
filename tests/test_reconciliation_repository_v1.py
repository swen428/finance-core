"""Tests for Reconciliation Repository v1.

Covers:
  1. create statement import batch
  2. create statement transaction with dates
  3. create statement transaction with reference and payload
  4. bulk insert statement transactions
  5. create reconciliation run
  6. complete reconciliation run
  7. fail reconciliation run
  8. save matched result from MatchResult
  9. save no_match result with needs_review = 1
 10. save amount_mismatch result with reason_codes and evidence
 11. fetch all results for run
 12. fetch review-required results
 13. fetch unmatched/unresolved statement transactions
 14. duplicate public_id raises clear error
 15. repository does not touch database/finance.db
 16. reason_codes_json and evidence_json are valid JSON
 17. Decimal amount storage is deterministic and round-trips
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
    MatchEvidence,
    MatchResult,
    MatchStatus,
    ReasonCode,
)
from finance_core.reconciliation.repository import (
    DuplicatePublicIdError,
    MatchResultRecord,
    ReconciliationRepository,
)

if TYPE_CHECKING:
    import sqlite3

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TODAY = date(2024, 12, 1)


def _seed_batch(conn: "sqlite3.Connection", public_id: str = "batch-test") -> int:
    conn.execute(
        """
        INSERT INTO statement_import_batches (public_id, source_type, currency)
        VALUES (?, 'bank_statement', 'SGD')
        """,
        (public_id,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM statement_import_batches WHERE public_id = ?",
        (public_id,),
    ).fetchone()
    return row["id"]


def _seed_stmt(
    conn: "sqlite3.Connection",
    public_id: str,
    batch_id: int,
    amount: Decimal = Decimal("10.00"),
    currency: str = "SGD",
    **kw,
) -> int:
    conn.execute(
        """
        INSERT INTO statement_transactions (
          public_id, batch_id, merchant_raw, amount, currency
        )
        VALUES (?, ?, 'Apple', ?, ?)
        """,
        (public_id, batch_id, str(amount), currency),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM statement_transactions WHERE public_id = ?",
        (public_id,),
    ).fetchone()["id"]


def _make_repo(conn: "sqlite3.Connection") -> ReconciliationRepository:
    return ReconciliationRepository(conn)


def _make_match_result(status=MatchStatus.MATCHED) -> MatchResult:
    return MatchResult(
        status=status,
        reasons=(ReasonCode.EXACT_AMOUNT_MATCH, ReasonCode.SAME_CURRENCY),
        evidence=MatchEvidence(
            statement_amount=Decimal("10.00"),
            candidate_amount=Decimal("10.00"),
            statement_currency="SGD",
            candidate_currency="SGD",
            statement_txn_date=date(2024, 12, 1),
            candidate_txn_date=date(2024, 12, 1),
            date_delta_days=0,
            date_tolerance_days=3,
            statement_merchant="Apple",
            candidate_merchant="Apple",
            merchant_similarity=1.0,
            candidate_count=1,
        ),
        best_candidate=InternalCandidate(
            internal_id="cand-001",
            transaction_date=date(2024, 12, 1),
            merchant="Apple",
            amount=Decimal("10.00"),
            currency="SGD",
        ),
    )


# ---------------------------------------------------------------------------
# 1. create statement import batch
# ---------------------------------------------------------------------------


def test_create_statement_import_batch(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    batch_id = repo.create_statement_import_batch(
        public_id="batch-001",
        source_type="bank_statement",
        currency="SGD",
    )
    assert isinstance(batch_id, int)
    assert batch_id > 0

    row = migrated_temp_db_connection.execute(
        "SELECT * FROM statement_import_batches WHERE id = ?", (batch_id,)
    ).fetchone()
    assert row["public_id"] == "batch-001"
    assert row["source_type"] == "bank_statement"
    assert row["currency"] == "SGD"


# ---------------------------------------------------------------------------
# 2. create statement transaction with dates
# ---------------------------------------------------------------------------


def test_create_statement_transaction_with_dates(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection)

    stmt_id = repo.create_statement_transaction(
        public_id="stmt-dates",
        batch_id=batch_id,
        merchant_raw="Apple",
        amount=Decimal("10.00"),
        currency="SGD",
        transaction_date="2024-12-01",
        posted_date="2024-12-03",
    )
    assert isinstance(stmt_id, int)

    row = migrated_temp_db_connection.execute(
        "SELECT * FROM statement_transactions WHERE id = ?", (stmt_id,)
    ).fetchone()
    assert row["transaction_date"] == "2024-12-01"
    assert row["posted_date"] == "2024-12-03"


# ---------------------------------------------------------------------------
# 3. create statement transaction with reference and payload
# ---------------------------------------------------------------------------


def test_create_statement_transaction_with_reference_and_payload(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection)

    stmt_id = repo.create_statement_transaction(
        public_id="stmt-ref",
        batch_id=batch_id,
        merchant_raw="Netflix",
        amount=Decimal("15.99"),
        currency="SGD",
        statement_row_reference="page=1,row=3",
        raw_row_payload_json=json.dumps({"source_row": 3}),
    )
    row = migrated_temp_db_connection.execute(
        "SELECT * FROM statement_transactions WHERE id = ?", (stmt_id,)
    ).fetchone()
    assert row["statement_row_reference"] == "page=1,row=3"
    assert row["raw_row_payload_json"] is not None
    assert json.loads(row["raw_row_payload_json"]) == {"source_row": 3}


# ---------------------------------------------------------------------------
# 4. bulk insert statement transactions
# ---------------------------------------------------------------------------


def test_bulk_insert_statement_transactions(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection)

    rows = [
        {
            "public_id": "bulk-1",
            "merchant_raw": "Apple",
            "amount": Decimal("10.00"),
            "currency": "SGD",
        },
        {
            "public_id": "bulk-2",
            "merchant_raw": "Netflix",
            "amount": Decimal("15.99"),
            "currency": "SGD",
        },
        {
            "public_id": "bulk-3",
            "merchant_raw": "Spotify",
            "amount": Decimal("9.99"),
            "currency": "SGD",
            "transaction_date": "2024-12-01",
            "raw_row_payload_json": '{"source_row":5}',
        },
    ]
    ids = repo.create_statement_transactions(batch_id, rows)
    assert len(ids) == 3
    assert all(isinstance(i, int) for i in ids)
    assert len(set(ids)) == 3

    fetched = migrated_temp_db_connection.execute(
        "SELECT public_id FROM statement_transactions WHERE batch_id = ? ORDER BY id",
        (batch_id,),
    ).fetchall()
    assert [r["public_id"] for r in fetched] == ["bulk-1", "bulk-2", "bulk-3"]


# ---------------------------------------------------------------------------
# 5. create reconciliation run
# ---------------------------------------------------------------------------


def test_create_reconciliation_run(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    run_id = repo.create_reconciliation_run(
        public_id="run-001",
        matcher_version="v1",
    )
    assert isinstance(run_id, int)

    row = migrated_temp_db_connection.execute(
        "SELECT * FROM reconciliation_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row["public_id"] == "run-001"
    assert row["run_status"] == "started"
    assert row["matcher_version"] == "v1"
    assert row["completed_at"] is None


# ---------------------------------------------------------------------------
# 6. complete reconciliation run
# ---------------------------------------------------------------------------


def test_complete_reconciliation_run(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    run_id = repo.create_reconciliation_run(public_id="run-complete")

    repo.complete_reconciliation_run(run_id)

    row = migrated_temp_db_connection.execute(
        "SELECT * FROM reconciliation_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row["run_status"] == "completed"
    assert row["completed_at"] is not None


# ---------------------------------------------------------------------------
# 7. fail reconciliation run
# ---------------------------------------------------------------------------


def test_fail_reconciliation_run(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    run_id = repo.create_reconciliation_run(public_id="run-fail")

    repo.fail_reconciliation_run(run_id)

    row = migrated_temp_db_connection.execute(
        "SELECT * FROM reconciliation_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row["run_status"] == "failed"
    assert row["completed_at"] is not None


# ---------------------------------------------------------------------------
# 8. save matched result from MatchResult
# ---------------------------------------------------------------------------


def test_save_matched_result(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-match")
    stmt_id = _seed_stmt(migrated_temp_db_connection, "stmt-match", batch_id)
    run_id = repo.create_reconciliation_run(public_id="run-match")

    result = _make_match_result(MatchStatus.MATCHED)
    mr_id = repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=result,
    )

    row = migrated_temp_db_connection.execute(
        "SELECT * FROM reconciliation_match_results WHERE id = ?", (mr_id,)
    ).fetchone()
    assert row["match_status"] == "matched"
    assert row["needs_review"] == 0
    assert row["internal_candidate_id"] == "cand-001"
    assert row["merchant_similarity"] == 1.0
    assert row["date_delta_days"] == 0
    assert row["amount_delta"] is not None


# ---------------------------------------------------------------------------
# 9. save no_match result with needs_review = 1
# ---------------------------------------------------------------------------


def test_save_no_match_result_needs_review(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-nomatch")
    stmt_id = _seed_stmt(migrated_temp_db_connection, "stmt-nomatch", batch_id)
    run_id = repo.create_reconciliation_run(public_id="run-nomatch")

    result = MatchResult(
        status=MatchStatus.NO_MATCH,
        reasons=(ReasonCode.NO_CANDIDATE_FOUND,),
        evidence=MatchEvidence(candidate_count=0),
    )
    mr_id = repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=result,
    )

    row = migrated_temp_db_connection.execute(
        "SELECT * FROM reconciliation_match_results WHERE id = ?", (mr_id,)
    ).fetchone()
    assert row["match_status"] == "no_match"
    assert row["needs_review"] == 1
    assert row["internal_candidate_id"] is None


# ---------------------------------------------------------------------------
# 10. save amount_mismatch result with reason_codes_json and evidence_json
# ---------------------------------------------------------------------------


def test_save_amount_mismatch_with_reason_codes_and_evidence(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-amt")
    stmt_id = _seed_stmt(migrated_temp_db_connection, "stmt-amt", batch_id, amount=Decimal("50.00"))
    run_id = repo.create_reconciliation_run(public_id="run-amt")

    result = MatchResult(
        status=MatchStatus.AMOUNT_MISMATCH,
        reasons=(ReasonCode.AMOUNT_DIFFERS,),
        evidence=MatchEvidence(
            statement_amount=Decimal("50.00"),
            candidate_count=1,
        ),
    )
    mr_id = repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=result,
    )

    row = migrated_temp_db_connection.execute(
        "SELECT * FROM reconciliation_match_results WHERE id = ?", (mr_id,)
    ).fetchone()
    assert row["match_status"] == "amount_mismatch"
    assert row["needs_review"] == 1

    reason_codes = json.loads(row["reason_codes_json"])
    assert "amount_differs" in reason_codes

    evidence = json.loads(row["evidence_json"])
    assert evidence["statement_amount"] == "50.00"


def test_review_status_evidence_preserves_candidate_ids(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-candidate-ids")
    stmt_id = _seed_stmt(migrated_temp_db_connection, "stmt-candidate-ids", batch_id)
    run_id = repo.create_reconciliation_run(public_id="run-candidate-ids")

    result = MatchResult(
        status=MatchStatus.AMBIGUOUS,
        reasons=(ReasonCode.MULTIPLE_CANDIDATE_MATCHES,),
        evidence=MatchEvidence(
            statement_amount=Decimal("10.00"),
            statement_currency="SGD",
            candidate_count=2,
        ),
        candidates=(
            InternalCandidate(
                internal_id="cand-a",
                transaction_date=date(2024, 12, 1),
                merchant="Apple",
                amount=Decimal("10.00"),
                currency="SGD",
            ),
            InternalCandidate(
                internal_id="cand-b",
                transaction_date=date(2024, 12, 1),
                merchant="Apple",
                amount=Decimal("10.00"),
                currency="SGD",
            ),
        ),
    )
    mr_id = repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=result,
    )

    row = migrated_temp_db_connection.execute(
        """
        SELECT internal_candidate_id, evidence_json
        FROM reconciliation_match_results
        WHERE id = ?
        """,
        (mr_id,),
    ).fetchone()
    evidence = json.loads(row["evidence_json"])
    assert row["internal_candidate_id"] is None
    assert evidence["candidate_ids"] == ["cand-a", "cand-b"]


# ---------------------------------------------------------------------------
# 11. fetch all results for run
# ---------------------------------------------------------------------------


def test_get_match_results_for_run(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-fetch")
    stmt_id = _seed_stmt(migrated_temp_db_connection, "stmt-fetch", batch_id)
    run_id = repo.create_reconciliation_run(public_id="run-fetch")

    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=_make_match_result(MatchStatus.MATCHED),
    )

    results = repo.get_match_results_for_run(run_id)
    assert len(results) == 1
    assert isinstance(results[0], MatchResultRecord)
    assert results[0].match_status == "matched"
    assert results[0].needs_review is False


# ---------------------------------------------------------------------------
# 12. fetch review-required results
# ---------------------------------------------------------------------------


def test_get_review_required_results(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-review")
    stmt1 = _seed_stmt(migrated_temp_db_connection, "stmt-review-1", batch_id)
    stmt2 = _seed_stmt(migrated_temp_db_connection, "stmt-review-2", batch_id)
    run_id = repo.create_reconciliation_run(public_id="run-review")

    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt1,
        match_result=_make_match_result(MatchStatus.MATCHED),
    )
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt2,
        match_result=MatchResult(
            status=MatchStatus.NO_MATCH,
            reasons=(ReasonCode.NO_CANDIDATE_FOUND,),
            evidence=MatchEvidence(),
        ),
    )

    review = repo.get_review_required_results(run_id=run_id)
    assert len(review) == 1
    assert review[0].match_status == "no_match"
    assert review[0].needs_review is True

    all_review = repo.get_review_required_results()
    assert len(all_review) == 1


# ---------------------------------------------------------------------------
# 13. fetch unmatched/unresolved statement transactions
# ---------------------------------------------------------------------------


def test_get_unmatched_statement_transactions(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-unmatch")
    stmt1 = _seed_stmt(migrated_temp_db_connection, "stmt-unmatch-1", batch_id)
    stmt2 = _seed_stmt(migrated_temp_db_connection, "stmt-unmatch-2", batch_id)
    run_id = repo.create_reconciliation_run(public_id="run-unmatch")

    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt1,
        match_result=_make_match_result(MatchStatus.MATCHED),
    )
    repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt2,
        match_result=MatchResult(
            status=MatchStatus.NO_MATCH,
            reasons=(ReasonCode.NO_CANDIDATE_FOUND,),
            evidence=MatchEvidence(),
        ),
    )

    unmatched = repo.get_unmatched_statement_transactions(run_id)
    assert len(unmatched) == 1
    assert unmatched[0].match_status == "no_match"


# ---------------------------------------------------------------------------
# 14. duplicate public_id raises clear error
# ---------------------------------------------------------------------------


def test_duplicate_public_id_raises(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)

    repo.create_statement_import_batch(
        public_id="dup-test",
        source_type="bank_statement",
    )

    with pytest.raises(DuplicatePublicIdError, match="dup-test"):
        repo.create_statement_import_batch(
            public_id="dup-test",
            source_type="bank_statement",
        )


def test_duplicate_run_public_id_raises(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)

    repo.create_reconciliation_run(public_id="run-dup")
    with pytest.raises(DuplicatePublicIdError, match="run-dup"):
        repo.create_reconciliation_run(public_id="run-dup")


# ---------------------------------------------------------------------------
# 18. deterministic public_id fallback without UUID
# ---------------------------------------------------------------------------


def test_save_match_result_public_id_deterministic(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-det-pid")
    stmt_id = _seed_stmt(migrated_temp_db_connection, "stmt-det-pid", batch_id)
    run_id = repo.create_reconciliation_run(public_id="run-det-pid")

    result = _make_match_result(MatchStatus.MATCHED)
    mr_id = repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=result,
    )

    row = migrated_temp_db_connection.execute(
        "SELECT public_id FROM reconciliation_match_results WHERE id = ?", (mr_id,)
    ).fetchone()
    assert row["public_id"] == f"match-{run_id}-{stmt_id}"


# ---------------------------------------------------------------------------
# 15. repository does not touch database/finance.db
# ---------------------------------------------------------------------------


def test_repository_does_not_touch_live_db(
    migrated_temp_db_connection: "sqlite3.Connection",
    temp_db_path: Path,
) -> None:
    database_path = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()["file"]
    assert Path(database_path) == temp_db_path
    assert Path(database_path) != LIVE_DB_PATH


# ---------------------------------------------------------------------------
# 16. reason_codes_json and evidence_json are valid JSON
# ---------------------------------------------------------------------------


def test_reason_codes_and_evidence_are_valid_json(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-json")
    stmt_id = _seed_stmt(migrated_temp_db_connection, "stmt-json", batch_id)
    run_id = repo.create_reconciliation_run(public_id="run-json")

    result = _make_match_result(MatchStatus.MATCHED)
    mr_id = repo.save_match_result(
        run_id=run_id,
        statement_transaction_id=stmt_id,
        match_result=result,
    )

    row = migrated_temp_db_connection.execute(
        "SELECT * FROM reconciliation_match_results WHERE id = ?", (mr_id,)
    ).fetchone()

    reason_codes = json.loads(row["reason_codes_json"])
    assert isinstance(reason_codes, list)
    assert len(reason_codes) > 0
    assert all(isinstance(r, str) for r in reason_codes)

    evidence = json.loads(row["evidence_json"])
    assert isinstance(evidence, dict)
    assert "candidate_count" in evidence


# ---------------------------------------------------------------------------
# 17. Decimal amount storage is deterministic and round-trips
# ---------------------------------------------------------------------------


def test_decimal_amount_round_trips(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    repo = _make_repo(migrated_temp_db_connection)
    batch_id = _seed_batch(migrated_temp_db_connection, "batch-decimal")

    amount = Decimal("29.90")
    stmt_id = repo.create_statement_transaction(
        public_id="stmt-decimal",
        batch_id=batch_id,
        merchant_raw="Apple",
        amount=amount,
        currency="SGD",
    )

    row = migrated_temp_db_connection.execute(
        "SELECT amount FROM statement_transactions WHERE id = ?", (stmt_id,)
    ).fetchone()

    # NUMERIC stores as text when inserted as string; verify round-trip
    stored = Decimal(str(row["amount"]))
    assert stored == amount
