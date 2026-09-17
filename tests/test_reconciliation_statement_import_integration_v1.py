"""Integration tests: Statement Import -> ReconciliationService -> Review Queue.

Covers:
  1. imported rows can feed ReconciliationService
  2. review queue / reporting SQL can see resulting match results
  3. full pipeline: CSV -> Importer -> Adapter -> Service -> Review Queue
  4. no database/finance.db access
  5. no seed data mutation
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from finance_core.reconciliation.adapter import InternalCandidateAdapter
from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.repository import ReconciliationRepository
from finance_core.reconciliation.review_queue import ReconciliationReviewQueue
from finance_core.reconciliation.service import ReconciliationService
from finance_core.reconciliation.statement_import import StatementImporter, StructuredStatementRow

if TYPE_CHECKING:
    import sqlite3

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(
    merchant: str,
    amount: Decimal,
    currency: str = "SGD",
    txn_date: date | None = date(2024, 12, 1),
) -> StructuredStatementRow:
    return StructuredStatementRow(
        merchant_raw=merchant,
        amount=amount,
        currency=currency,
        transaction_date=txn_date,
        amount_direction=StatementAmountDirection.DEBIT,
        raw_amount=str(amount),
    )


def _seed_internal_transaction(
    conn: "sqlite3.Connection",
    *,
    public_id: str,
    transaction_date: str,
    merchant: str,
    amount: str,
    currency: str = "SGD",
) -> None:
    conn.execute(
        """
        INSERT INTO transactions (
            public_id, transaction_date, merchant, amount, currency,
            status, intent, intent_type, source_channel
        )
        VALUES (?, ?, ?, ?, ?, 'active', 'expense', 'Manual', 'manual')
        """,
        (public_id, transaction_date, merchant, amount, currency),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 1. imported rows can feed ReconciliationService
# ---------------------------------------------------------------------------


def test_imported_rows_feed_reconciliation_service(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    # Seed an internal transaction that should match
    _seed_internal_transaction(
        migrated_temp_db_connection,
        public_id="internal-001",
        transaction_date="2024-12-01",
        merchant="Apple",
        amount="10.00",
    )

    # Import a matching statement row
    importer = StatementImporter(migrated_temp_db_connection)
    row = _make_row("Apple", Decimal("10.00"), "SGD", date(2024, 12, 1))
    batch = importer.import_rows([row], source_type="bank_statement", public_id="batch-integ-1")

    # Fetch internal candidates
    adapter = InternalCandidateAdapter(migrated_temp_db_connection)
    candidates = adapter.fetch_candidates()
    assert len(candidates) == 1
    assert candidates[0].internal_id == "internal-001"

    # Run reconciliation
    repo = ReconciliationRepository(migrated_temp_db_connection)
    service = ReconciliationService(repo)
    summary = service.run_reconciliation_for_batch(
        batch_id=batch.batch_id,
        candidates=candidates,
        run_public_id="run-integ-1",
    )

    assert summary.total_statement_transactions == 1
    assert summary.matched_count == 1
    assert summary.no_match_count == 0
    assert not summary.failed


# ---------------------------------------------------------------------------
# 2. review queue / reporting SQL can see resulting match results
# ---------------------------------------------------------------------------


def test_review_queue_sees_match_results(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    # Import a statement row with no matching internal candidate
    importer = StatementImporter(migrated_temp_db_connection)
    row = _make_row("Unknown Merchant", Decimal("99.99"), "SGD", date(2024, 12, 1))
    batch = importer.import_rows(
        [row], source_type="bank_statement", public_id="batch-review-integ"
    )

    # Internal candidates: empty (no matches possible)
    adapter = InternalCandidateAdapter(migrated_temp_db_connection)
    candidates = adapter.fetch_candidates()

    # Run reconciliation
    repo = ReconciliationRepository(migrated_temp_db_connection)
    service = ReconciliationService(repo)
    summary = service.run_reconciliation_for_batch(
        batch_id=batch.batch_id,
        candidates=candidates,
        run_public_id="run-review-integ",
    )

    assert summary.no_match_count == 1
    assert summary.total_statement_transactions == 1

    # Review queue should contain the unmatched entry
    review = ReconciliationReviewQueue(migrated_temp_db_connection)
    entries = review.get_review_required()
    assert len(entries) >= 1

    # Find the entry for our unmatchable row
    unmatched = [e for e in entries if e.match_status == "no_match"]
    assert len(unmatched) >= 1


# ---------------------------------------------------------------------------
# 3. full pipeline: CSV -> Importer -> Adapter -> Service
# ---------------------------------------------------------------------------


def test_full_pipeline_csv_to_service(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    # Seed some internal transactions
    _seed_internal_transaction(
        migrated_temp_db_connection,
        public_id="internal-full-001",
        transaction_date="2024-12-01",
        merchant="Apple",
        amount="10.00",
    )
    _seed_internal_transaction(
        migrated_temp_db_connection,
        public_id="internal-full-002",
        transaction_date="2024-12-02",
        merchant="Netflix",
        amount="15.99",
    )

    # Use dict input (simulating CSV adapter output)
    rows = [
        {
            "merchant_raw": "Apple",
            "amount": Decimal("10.00"),
            "currency": "SGD",
            "transaction_date": date(2024, 12, 1),
            "amount_direction": StatementAmountDirection.DEBIT,
            "raw_amount": "10.00",
        },
        {
            "merchant_raw": "Netflix",
            "amount": Decimal("15.99"),
            "currency": "SGD",
            "transaction_date": date(2024, 12, 2),
            "amount_direction": StatementAmountDirection.DEBIT,
            "raw_amount": "15.99",
        },
    ]
    importer = StatementImporter(migrated_temp_db_connection)
    batch = importer.import_rows(
        rows, source_type="structured_csv", public_id="batch-full-pipeline"
    )

    # Fetch candidates
    adapter = InternalCandidateAdapter(migrated_temp_db_connection)
    candidates = adapter.fetch_candidates()
    assert len(candidates) == 2

    # Reconcile
    repo = ReconciliationRepository(migrated_temp_db_connection)
    service = ReconciliationService(repo)
    summary = service.run_reconciliation_for_batch(
        batch_id=batch.batch_id,
        candidates=candidates,
        run_public_id="run-full-pipeline",
    )

    assert summary.total_statement_transactions == 2
    assert summary.matched_count == 2
    assert not summary.failed

    # Verify run summary view
    review = ReconciliationReviewQueue(migrated_temp_db_connection)
    run_summaries = review.get_all_run_summaries()
    our_run = [s for s in run_summaries if s.run_public_id == "run-full-pipeline"]
    assert len(our_run) == 1
    assert our_run[0].matched_count == 2


# ---------------------------------------------------------------------------
# 4. no database/finance.db access
# ---------------------------------------------------------------------------


def test_integration_does_not_touch_live_db(
    migrated_temp_db_connection: "sqlite3.Connection",
    temp_db_path: Path,
) -> None:
    database_path = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()["file"]
    assert Path(database_path) == temp_db_path
    assert Path(database_path) != LIVE_DB_PATH


# ---------------------------------------------------------------------------
# 5. no seed data mutation
# ---------------------------------------------------------------------------


def test_no_seed_data_mutation(
    migrated_temp_db_connection: "sqlite3.Connection",
    temp_db_path: Path,
) -> None:
    database_path = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()["file"]
    db_path = Path(database_path)
    seed_dir = REPO_ROOT / "database" / "seed"
    # Assert the test DB is not inside the seed directory
    assert not str(db_path).startswith(str(seed_dir))
    # Assert it's in the temp directory
    assert db_path == temp_db_path
